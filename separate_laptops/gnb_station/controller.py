"""gNB station: core network check, the gNB softmodem, the traffic sender, and the UE station over the link.

Runs on the gNB laptop (with OAI CN5G in docker). One Start brings everything up:
  1. starts nr-softmodem and follows its boot in the log until the radio is on air (SSB transmitted);
  2. asks the UE station to start its UE (link "start"); the UE station keeps its UE up from then on;
  3. once the UE station reports the UE's IP and its traffic receiver, starts the downlink sender (iperf in
     the core's DN container) to that IP, and restarts it if the UE comes back with another IP.
Stop stops the sender, asks the UE station to stop, and stops the gNB.

Scenarios: GnbStationController is a drop-in for common.oai's Controller under DemoController, so the
three-scenario logic (decoder swap on the gNB, scheduled Legacy / AI switch, timeline) is the same-laptop
code. The scheduled switch reaches the UE through the link (apply_mode_scheduled(ue_send=...)): both sides
are armed for the same radio frame, read from the gNB, far enough ahead to cover the link's delay; if either
side refuses, both are cancelled.
"""

from __future__ import annotations

import logging
import threading
import time
from copy import deepcopy
from pathlib import Path

from common.demo_control import DemoController
from common.oai.config import AppConfig
from common.oai.controllers.controller import RuntimeSnapshot
from common.oai.net.csirs_ingest import ensure_record_tree, wipe_record_dir
from common.oai.net.iperf_control import IperfLauncher
from common.oai.net.metrics_ingest import MacStatsIngest
from common.oai.net.mode_control import (
    MODE_CSINET,
    MODE_LEGACY,
    TelnetProbe,
    apply_mode_scheduled,
    parse_aifb_status,
    telnet_command,
)
from common.oai.net.modem_boot import GnbBootMonitor, GnbBootStage
from common.oai.net.softmodem_launcher import SoftmodemLauncher
from common.station_link import DEFAULT_PORT, LinkClient, LinkError

log = logging.getLogger(__name__)

STEPS = ("Core", "gNB", "USRP", "UE", "Wi-Fi", "Traffic")
_BOOTED = {GnbBootStage.CONFIG.value, GnbBootStage.RU_INIT.value, GnbBootStage.RF_SSB.value}


class _GnbLauncher(SoftmodemLauncher):
    """The gNB also records what it receives (--csi-record-path): the channel tab of this station."""

    def build_argv(self, side, mode):
        argv = super().build_argv(side, mode)
        if side == "gnb" and self.csi_record_path is not None and "--csi-record-path" not in argv:
            argv += ["--csi-record-path", str(Path(self.csi_record_path).resolve())]
        return argv


class GnbStationController:
    """The subset of common.oai's Controller that DemoController uses, for a gNB whose UE is remote."""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        st = cfg.station
        stamp = time.strftime("%Y%m%d_%H%M%S")
        root = Path(cfg.logging.root_dir)
        self.launcher = _GnbLauncher(cfg, root / "softmodem_logs" / f"gnb_{stamp}")
        self.iperf = IperfLauncher(cfg, root / "iperf_logs" / f"gnb_{stamp}")
        self.csirs_record_root = Path(cfg.oai.csi_record_dir or root / "csi_record") / f"gnb_{stamp}"
        ensure_record_tree(self.csirs_record_root)
        self.launcher.csi_record_path = self.csirs_record_root
        self.ingest = MacStatsIngest(Path(cfg.oai.build_dir).expanduser() / cfg.oai.stats_log)
        self.boot = GnbBootMonitor()
        self.link = LinkClient(str(st.get("ue_host", "127.0.0.1")), key=str(st.get("key", "")),
                               port=int(st.get("port", DEFAULT_PORT)), timeout_s=float(st.get("timeout_s", 3.0)),
                               delay_s=float(st.get("test_delay_s", 0.0)))
        self.switch_lead = int(st.get("switch_lead_frames", 100))
        self.snapshot = RuntimeSnapshot(csi_mode=cfg.csi.default_mode or MODE_LEGACY)
        self.ue: dict = {}  # last UE station status ({} = link down)
        self.core_ok = False
        self.target_frame: int | None = None  # frame of the last scheduled Legacy / AI switch
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._desired = False
        self._started_at = 0.0
        self._last_exit = 0.0
        self._client_ip = ""
        self._ue_start_sent = 0.0
        self._threads: list[threading.Thread] = []
        self.on_event = lambda level, text: log.info("[%s] %s", level, text)

    # ------------------------------------------------------------------ Controller interface
    def start_background_workers(self) -> None:
        self.launcher._pkill_named(Path(self.cfg.oai.gnb.binary or "nr-softmodem").name, "TERM")
        self.iperf._kill_container_iperf()
        for target, name in ((self._supervise, "gnb-supervise"), (self._link_loop, "gnb-link")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop_background_workers(self) -> None:
        self._stop.set()
        self.stop_system()
        self.link.close()
        wipe_record_dir(self.csirs_record_root)

    def start_system(self) -> None:
        with self._lock:
            self._desired = True
            self.snapshot.traffic_desired = True
            self.snapshot.status = "System: starting"

    def stop_system(self) -> None:
        with self._lock:
            self._desired = False
            self.snapshot.traffic_desired = False
        self.stop_iperf_traffic()
        try:
            self.link.call("stop")
        except LinkError as exc:
            log.info("UE station stop: %s", exc)
        if self.launcher.is_running("gnb"):
            self.launcher.stop("gnb")
        self.boot.mark_process_down()
        with self._lock:
            self.snapshot.gnb_running = False
            self.snapshot.traffic_stage = "idle"
            self.snapshot.status = "System: stopped"

    def stop_iperf_traffic(self) -> None:
        if self.iperf.is_running("client"):
            self.iperf.stop("client")
        self.iperf._kill_container_iperf()
        self._client_ip = ""
        with self._lock:
            self.snapshot.iperf_client_running = False

    def get_snapshot(self) -> RuntimeSnapshot:
        with self._lock:
            return deepcopy(self.snapshot)

    def _softmodems_ready_for_live_mode(self) -> bool:
        if not (self.launcher.is_running("gnb") and self.ue.get("running")):
            return False
        if not TelnetProbe(self.cfg.network.gnb_host, self.cfg.network.gnb_telnet_port).reachable():
            return False
        try:
            parse_aifb_status(self._ue_send("aifb status", 1.0))
            return True
        except (OSError, RuntimeError):
            return False

    def set_csi_mode(self, mode: str) -> None:
        with self._lock:
            if self.snapshot.csi_mode == mode:
                return
            self.snapshot.csi_mode, self.snapshot.mode_pending = mode, True
        if not self._softmodems_ready_for_live_mode():
            with self._lock:  # applied when the softmodems boot
                self.snapshot.mode_pending = False
                self.snapshot.status = f"Mode -> {mode} (armed for the next start)"
            return
        threading.Thread(target=self._switch_worker, args=(mode,), name="mode-switch", daemon=True).start()

    def _switch_worker(self, mode: str) -> None:
        def armed(target: int, lead: int) -> None:
            self.target_frame = target
            with self._lock:
                self.snapshot.note = f"Switching to {mode} at frame {target} (in {lead * 10} ms)"

        try:
            res = apply_mode_scheduled(
                mode=mode, gnb_host=self.cfg.network.gnb_host, gnb_port=self.cfg.network.gnb_telnet_port,
                ue_host="", ue_port=0, lead_frames=self.switch_lead, timeout_s=1.0, on_armed=armed,
                ue_send=self._ue_send)
            with self._lock:
                self.snapshot.mode_pending = False
                self.snapshot.status = f"Mode {mode} applied (frame {res.target_frame})"
                self.snapshot.note = (f"Switched at frame {res.target_frame}, {res.switched_after_s:.2f} s after "
                                      f"the click (link round trip {self.link.rtt_s * 1000:.0f} ms)")
        except Exception as exc:  # noqa: BLE001
            log.exception("scheduled switch failed")
            actual = None
            try:
                st = parse_aifb_status(telnet_command(self.cfg.network.gnb_host, self.cfg.network.gnb_telnet_port,
                                                      "aifb status"))
                actual = MODE_CSINET if st.get("pucch_replace") == "1" else MODE_LEGACY
            except (OSError, RuntimeError):
                pass
            with self._lock:
                self.snapshot.mode_pending = False
                self.snapshot.status = "Mode switch failed"
                self.snapshot.note = str(exc)
                if actual is not None:
                    self.snapshot.csi_mode = actual

    def _ue_send(self, cmd: str, timeout_s: float) -> str:
        return self.link.call("telnet", timeout_s=timeout_s + 3.0, cmd=cmd, timeout=timeout_s)["reply"]

    # ------------------------------------------------------------------ supervision
    def _link_loop(self) -> None:
        """UE station status twice a second (also the link's heartbeat)."""
        while not self._stop.wait(0.5):
            try:
                ue = self.link.call("status")
            except LinkError as exc:
                if self.ue:
                    self.on_event("fail", f"UE station unreachable: {exc}")
                ue = {}
            else:
                if not self.ue:
                    self.on_event("ok", f"UE station connected ({self.link.host}, round trip "
                                        f"{self.link.rtt_s * 1000:.0f} ms)")
            with self._lock:
                self.ue = ue
                self.snapshot.ue_running = bool(ue.get("running"))
                self.snapshot.ue_bringup_stage = ue.get("stage", "")
                self.snapshot.ue_bringup_label = ue.get("stage_label", "")

    def _supervise(self) -> None:
        while not self._stop.wait(0.5):
            try:
                self._supervise_once()
            except Exception as exc:  # noqa: BLE001
                log.exception("gNB supervisor: %s", exc)

    def _supervise_once(self) -> None:
        now = time.monotonic()
        gnb_run = self.launcher.is_running("gnb")
        boot = self.boot.poll(process_running=gnb_run, log_path=self.launcher.stdout_path("gnb"))
        sample = self.ingest.poll() if gnb_run else None
        with self._lock:
            ue = dict(self.ue)
            desired = self._desired
            self.snapshot.gnb_running = gnb_run
            self.snapshot.gnb_boot_stage, self.snapshot.gnb_boot_label = boot.stage.value, boot.label
            self.snapshot.ue_attached = bool(sample and sample.attached)
            if sample is not None:
                self.snapshot.bler, self.snapshot.cqi = sample.bler, sample.cqi
                if sample.throughput_mbps == sample.throughput_mbps:
                    self.snapshot.throughput_mbps = sample.throughput_mbps
            self.snapshot.ue_ip = ue.get("ue_ip", "")
        if not desired:
            return
        if not gnb_run:
            if self._started_at and not self._last_exit:
                self._last_exit = now
                self.on_event("fail", "gNB exited; restarting it")
                self.stop_iperf_traffic()
            if now - self._last_exit < 3.0:
                return
            self._start_gnb()
            return
        on_air = boot.stage == GnbBootStage.RF_SSB
        if on_air and ue and not ue.get("desired") and now - self._ue_start_sent > 3.0:
            self._ue_start_sent = now
            try:
                self.link.call("start")
                self.on_event("busy", "gNB on air: UE station asked to start its UE")
            except LinkError as exc:
                self.on_event("fail", f"Could not start the UE: {exc}")
        ue_ip = ue.get("ue_ip", "")
        ready = on_air and ue_ip and ue.get("server_running")
        if ready and (self._client_ip != ue_ip or not self.iperf.is_running("client")):
            self.stop_iperf_traffic()
            self._set_stage("starting_iperf")
            time.sleep(max(0.2, float(self.cfg.iperf.client_delay_s)))
            try:
                self.iperf.start("client", ue_ip=ue_ip)
                self._client_ip = ue_ip
                self.on_event("ok", f"Traffic running to UE {ue_ip}")
            except Exception as exc:  # noqa: BLE001
                self.on_event("fail", f"Traffic sender failed: {exc}")
        elif not ready and self.iperf.is_running("client"):
            self.stop_iperf_traffic()
        running = bool(ready and self.iperf.is_running("client"))
        with self._lock:
            self.snapshot.iperf_client_running = self.iperf.is_running("client")
            self.snapshot.traffic_stage = "running" if running else (
                "waiting_gnb" if not on_air else ("waiting_ue_station" if not ue else "waiting_ue_ip"))

    def _start_gnb(self) -> None:
        with self._lock:
            mode = self.snapshot.csi_mode
        try:
            self.launcher.start("gnb", mode)
        except Exception as exc:  # noqa: BLE001
            self._last_exit = time.monotonic()
            self.on_event("fail", f"gNB start failed: {exc}")
            return
        self._started_at, self._last_exit = time.monotonic(), 0.0
        self.boot.reset(self.launcher.stdout_path("gnb"), follow_new_only=True)
        self.on_event("busy", f"gNB starting ({'AI' if mode == MODE_CSINET else 'legacy'} feedback)")

    def _set_stage(self, stage: str) -> None:
        with self._lock:
            self.snapshot.traffic_stage = stage

    def steps(self) -> list[tuple[str, str]]:
        s, link_up = self.get_snapshot(), bool(self.ue)
        done = [self.core_ok, s.gnb_running and s.gnb_boot_stage in _BOOTED,
                s.gnb_running and s.gnb_boot_stage == GnbBootStage.RF_SSB.value,
                s.ue_attached or bool(s.ue_ip), link_up, s.traffic_stage == "running"]
        out, blocked = [], False
        for name, ok in zip(STEPS, done):
            if name in ("Core", "Wi-Fi"):  # independent of the bring-up order
                out.append((name, "ok" if ok else ("fail" if self._desired else "idle")))
                continue
            out.append((name, "ok" if ok and not blocked else ("busy" if self._desired and not blocked else "idle")))
            blocked = blocked or not ok
        return out


class GnbDemo(DemoController):
    """DemoController that also tells the UE station which scenario is on air (its cards and pop-up)."""

    def __init__(self, cfg: AppConfig):
        self.station = GnbStationController(cfg)
        super().__init__(cfg, controller=self.station)
        self.station.on_event = self.event
        self._notices: list[dict] = []
        self._notice_cv = threading.Condition()
        threading.Thread(target=self._notice_loop, name="ue-notices", daemon=True).start()

    def _mark(self, key: str | None) -> None:
        super()._mark(key)
        if key is not None:
            self._notify({"key": key, "state": "live", "frame": self.station.target_frame})
        elif self._switching:
            self._notify({"key": self.target, "state": "switching"})

    def _notify(self, notice: dict) -> None:
        with self._notice_cv:
            self._notices.append(notice)
            self._notice_cv.notify()

    def _notice_loop(self) -> None:
        while True:
            with self._notice_cv:
                while not self._notices:
                    self._notice_cv.wait()
                notice = self._notices.pop(0)
            try:
                self.station.link.call("scenario", **notice)
            except LinkError as exc:
                log.info("scenario notice not delivered: %s", exc)
