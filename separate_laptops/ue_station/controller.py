"""UE station: the UE softmodem, its bring-up and IP, the traffic receiver, and the link to the gNB station.

Runs on the UE laptop. Start (here, or "start" from the gNB station) keeps the UE up until Stop:
  - starts nr-uesoftmodem and follows its bring-up in the log (sync, RACH, RRC, PDU session, TUN);
  - once the UE has its IP (from the core, over the air), runs the iperf server on it;
  - restarts the UE when it exits (e.g. a USRP overflow abort) or loses its PDU session for good;
  - answers the gNB station: status, start / stop, the scheduled Legacy / AI switch (forwarded to this
    UE's telnet, "aifb" commands only) and scenario notices (pop-up and scenario cards here).
Per second it samples what the UE itself knows: received throughput, DL decode failures, SINR.
"""

from __future__ import annotations

import bisect
import logging
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from common.oai.config import AppConfig
from common.oai.net.csirs_ingest import ensure_record_tree, wipe_record_dir
from common.oai.net.iperf_control import IperfLauncher
from common.oai.net.mode_control import telnet_command
from common.oai.net.softmodem_launcher import SoftmodemLauncher
from common.oai.net.ue_bringup import BringupStage, BringupTimeouts, UeBringupMonitor
from common.oai.net.ue_ip import discover_ue_ipv4
from common.station_data import UeChannelWorker, UeStatsReader
from common.station_link import DEFAULT_PORT, LinkServer

log = logging.getLogger(__name__)

STEPS = ("USRP", "Sync", "Attach", "IP", "Receiver", "Wi-Fi")
_STAGE_RANK = {s: i for i, s in enumerate((
    BringupStage.PROCESS_DOWN, BringupStage.STARTING, BringupStage.INIT, BringupStage.RADIO,
    BringupStage.SEARCHING_SSB, BringupStage.WAITING_RACH, BringupStage.WAITING_RRC, BringupStage.WAITING_PDU,
    BringupStage.WAITING_TUN, BringupStage.READY))}
SETTLE_S = 2.0
ALLOWED_TELNET = ("aifb status", "aifb schedule ", "aifb cancel")


@dataclass
class UeSample:
    t: float
    scenario: str
    tput_mbps: float
    bler: float  # DL decode failure rate at the UE
    sinr: float
    rsrp: float


@dataclass
class Event:
    clock: str
    level: str
    text: str


@dataclass
class UeView:
    running: bool
    desired: bool
    stage: str
    stage_label: str
    ue_ip: str
    server_running: bool
    link_up: bool
    peer: str
    scenario: str
    steps: list[tuple[str, str]]
    samples: list[UeSample]
    events: list[Event]
    session_t0: float | None = None
    popup: dict = field(default_factory=dict)  # the latest scenario notice from the gNB station
    rx_silent_s: float = 0.0  # receiver listening, but no downlink data for this long (0: data arriving / n.a.)
    by_scenario: dict = field(default_factory=dict)  # every sample of the session per scenario (CDFs, averages)


class UeStation:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        st = cfg.station
        stamp = time.strftime("%Y%m%d_%H%M%S")
        root = Path(cfg.logging.root_dir)
        self.launcher = SoftmodemLauncher(cfg, root / "softmodem_logs" / f"ue_{stamp}")
        self.iperf = IperfLauncher(cfg, root / "iperf_logs" / f"ue_{stamp}")
        self.record_root = Path(cfg.oai.csi_record_dir or root / "csi_record") / f"ue_{stamp}"
        ensure_record_tree(self.record_root)
        self.launcher.csi_record_path = self.record_root
        self.bringup = UeBringupMonitor(BringupTimeouts(
            ssb_s=float(cfg.iperf.bringup_ssb_timeout_s), rach_s=float(cfg.iperf.bringup_rach_timeout_s),
            rrc_s=float(cfg.iperf.bringup_rrc_timeout_s), pdu_s=float(cfg.iperf.bringup_pdu_timeout_s),
            tun_s=float(cfg.iperf.bringup_tun_timeout_s)))
        self.channel = UeChannelWorker(lambda: str(self.record_root), keep_h=int(cfg.oai.csi_record_keep_h or 4))
        self.stats = UeStatsReader()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._desired = False
        self._ue_ip = ""
        self._server_ip = ""
        self._ip_seen = False
        self._ip_lost_at = 0.0
        self._started_at = 0.0
        self._last_exit = 0.0
        self._server_since = 0.0  # time.monotonic() the receiver started on the current IP
        self._rx_last = 0.0  # time.monotonic() downlink data last arrived at the receiver
        self._scenario = "legacy"
        self._popup: dict = {}
        self._timeline: list[tuple[float, str | None]] = [(0.0, None)]
        self._samples: deque[UeSample] = deque(maxlen=3600)
        self._by_scenario: dict[str, list[UeSample]] = {}
        self._session_t0: float | None = None
        self._events: deque[Event] = deque(maxlen=300)
        self._t0 = time.monotonic()
        self.server = LinkServer(
            {"hello": self._op_hello, "status": lambda a: self.status(), "start": self._op_start,
             "stop": self._op_stop, "telnet": self._op_telnet, "scenario": self._op_scenario},
            key=str(st.get("key", "")), host=str(st.get("listen", "0.0.0.0")), port=int(st.get("port", DEFAULT_PORT)))
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._kill_own_strays()
        self.server.start()
        self.channel.start()
        for target, name in ((self._supervise, "ue-supervise"), (self._sample_loop, "ue-sampler")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        self.event("info", f"UE station ready, waiting for the gNB station on port {self.server.address[1]}")

    def shutdown(self) -> None:
        self._stop.set()
        self.stop_ue()
        self.server.close()
        self.channel.stop()
        wipe_record_dir(self.record_root)

    def _kill_own_strays(self) -> None:
        """Only this station's leftovers: a gNB on the same machine (testing) must survive."""
        for sig in ("TERM", "KILL"):
            self.launcher._pkill_named(Path(self.cfg.oai.ue.binary or "nr-uesoftmodem").name, sig)
            subprocess.run(["pkill", f"-{sig}", "-f", "iperf -s -u"], check=False, capture_output=True)
            time.sleep(0.3)

    def start_ue(self, source: str = "here") -> None:
        with self._lock:
            if self._desired:
                return
            self._desired = True
            self._samples.clear()
            self._by_scenario = {}
            self._session_t0 = None
            self._started_at, self._last_exit = 0.0, 0.0  # a new session: no "UE exited" for the last one
            self._server_since, self._rx_last = 0.0, 0.0
        self.stats.reset()
        self.channel.clear()
        self.event("busy", "Starting the UE" + (" (requested by the gNB station)" if source == "link" else ""))

    def stop_ue(self, source: str = "here") -> None:
        with self._lock:
            was = self._desired
            self._desired = False
        self._stop_server()
        if self.launcher.is_running("ue"):
            self.launcher.stop("ue")
        self.bringup.mark_process_down()
        self._mark(None)
        with self._lock:
            self._ue_ip, self._ip_seen = "", False
        if was:
            self.event("ok", "UE stopped" + (" (requested by the gNB station)" if source == "link" else ""))

    # ------------------------------------------------------------------ supervisor
    def _supervise(self) -> None:
        while not self._stop.wait(0.5):
            try:
                self._supervise_once()
            except Exception as exc:  # noqa: BLE001
                log.exception("UE supervisor: %s", exc)

    def _supervise_once(self) -> None:
        if not self._desired:
            return
        now = time.monotonic()
        if not self.launcher.is_running("ue"):
            if self._started_at and now - self._started_at > 1.0 and not self._last_exit:
                self._last_exit = now
                self.event("fail", "UE exited; restarting it")
                self._stop_server()
                with self._lock:
                    self._ue_ip, self._ip_seen = "", False
            if now - self._last_exit < float(self.cfg.iperf.ue_restart_cooldown_s or 2.0):
                return
            self._start_process()
            return
        status = self.bringup.poll(process_running=True, log_path=self.launcher.stdout_path("ue"))
        ip = discover_ue_ipv4(self.cfg.iperf.ue_iface, ue_log=self.launcher.stdout_path("ue")) or ""
        if ip:
            if not self._ip_seen:
                self.event("ok", f"UE attached, IP {ip}")
            with self._lock:
                self._ue_ip, self._ip_seen, self._ip_lost_at = ip, True, 0.0
            if self._server_ip != ip or not self.iperf.is_running("server"):
                self._stop_server()
                self.iperf.start("server", ue_ip=ip)
                self._server_ip = ip
                self._server_since = time.monotonic()
                self.event("ok", f"Traffic receiver listening on {ip}")
            return
        with self._lock:
            self._ue_ip = ""
        self._stop_server()
        lost_for = 0.0
        if self._ip_seen:
            self._ip_lost_at = self._ip_lost_at or now
            lost_for = now - self._ip_lost_at
        give_up = float(self.cfg.iperf.pdu_retry_wait_s) * max(1, int(self.cfg.iperf.pdu_retry_max)) + 5.0
        past_attach = _STAGE_RANK.get(status.stage, 0) >= _STAGE_RANK[BringupStage.WAITING_RRC]
        if lost_for > give_up or (status.stalled and past_attach):
            self.event("fail", "UE lost its PDU session" if lost_for else f"UE stuck at {status.label}; restarting")
            self.launcher.stop("ue")
            self._last_exit = time.monotonic()
            with self._lock:
                self._ip_seen = False

    def _start_process(self) -> None:
        try:
            mode = "csinet" if self._scenario != "legacy" else "legacy"
            self.launcher.start("ue", mode)
        except Exception as exc:  # noqa: BLE001
            self._last_exit = time.monotonic()
            self.event("fail", f"UE start failed: {exc}")
            return
        self._started_at, self._last_exit = time.monotonic(), 0.0
        self.bringup.reset(self.launcher.stdout_path("ue"), follow_new_only=True)

    def _stop_server(self) -> None:
        if self.iperf.is_running("server"):
            self.iperf.stop("server")
        self._server_ip = ""

    # ------------------------------------------------------------------ link handlers (gNB station)
    def _op_hello(self, args: dict) -> dict:
        return {"station": "ue", "host": socket.gethostname()}

    def _op_start(self, args: dict) -> dict:
        self.start_ue(source="link")
        return self.status()

    def _op_stop(self, args: dict) -> dict:
        threading.Thread(target=self.stop_ue, kwargs={"source": "link"}, daemon=True).start()
        return {"stopping": True}

    def _op_telnet(self, args: dict) -> dict:
        cmd = str(args.get("cmd", "")).strip()
        if not cmd.startswith(ALLOWED_TELNET):
            raise ValueError(f"not allowed: {cmd!r}")
        reply = telnet_command(self.cfg.network.ue_host, self.cfg.network.ue_telnet_port, cmd,
                               float(args.get("timeout", 1.0)))
        if cmd.startswith("aifb schedule ") and "aifb armed" in reply:
            with self._lock:  # the pop-up shows the frame this UE is armed for
                if self._popup.get("state") == "switching":
                    self._popup["frame"] = int(cmd.split()[3])
        return {"reply": reply}

    def _op_scenario(self, args: dict) -> dict:
        """The gNB station's scenario notices: state switching (armed for a frame) or live (confirmed)."""
        key, state = str(args.get("key", "")), str(args.get("state", ""))
        if key not in ("legacy", "same", "cross"):
            raise ValueError(f"unknown scenario {key!r}")
        with self._lock:
            prev = self._scenario
            if state == "switching":
                self._popup = {"key": key, "prev": prev, "frame": args.get("frame"), "t": time.monotonic(),
                               "state": "switching"}
            elif state == "live":
                self._scenario = key
                if self._popup.get("key") == key:
                    self._popup["state"] = "live"
                    self._popup["t"] = time.monotonic()
        if state == "switching":
            self._mark(None)
        elif state == "live":
            self._mark(key)
            if key != prev:
                self.event("ok", f"Scenario from the gNB station: {_title(prev)} -> {_title(key)}")
        return {"scenario": self._scenario}

    # ------------------------------------------------------------------ data
    def event(self, level: str, text: str) -> None:
        with self._lock:
            self._events.append(Event(time.strftime("%H:%M:%S"), level, text))
        log.info("[%s] %s", level, text)

    def _mark(self, key: str | None) -> None:
        with self._lock:
            self._timeline.append((time.time(), key))

    def scenario_at(self, t_wall: float, settle_s: float = 0.0) -> str | None:
        with self._lock:
            i = bisect.bisect_right([t for t, _ in self._timeline], t_wall) - 1
            t_from, key = self._timeline[max(i, 0)]
        return None if key is None or t_wall - t_from < settle_s else key

    def _sample_loop(self) -> None:
        traffic_since = None
        while not self._stop.wait(1.0):
            iperf_log = self.iperf.log_dir / "iperf_server.log"
            st = self.stats.poll(self.launcher.stdout_path("ue"), iperf_log)
            receiving = self.iperf.is_running("server") and st.tput_mbps == st.tput_mbps and st.tput_mbps > 0.1
            if receiving:
                self._rx_last = time.monotonic()
            if not (self._desired and receiving):
                traffic_since = None
                continue
            now = time.monotonic()
            traffic_since = traffic_since or now
            if now - traffic_since < 5.0:
                continue  # skip the sender's ramp-up
            scen = self.scenario_at(time.time(), SETTLE_S)
            if scen is None:
                continue
            with self._lock:
                if self._session_t0 is None:
                    self._session_t0 = now - self._t0
                sample = UeSample(now - self._t0, scen, st.tput_mbps, st.fail_rate, st.sinr_db, st.rsrp_dbm)
                self._samples.append(sample)  # last hour: time plots
                self._by_scenario.setdefault(scen, []).append(sample)  # whole session: CDFs, averages

    def _rx_silent_s(self, server_running: bool) -> float:
        if not server_running or not self._server_since:
            return 0.0
        silent = time.monotonic() - max(self._server_since, self._rx_last)
        return silent if silent > 10.0 else 0.0

    def status(self) -> dict:
        """What the gNB station polls."""
        running = self.launcher.is_running("ue")
        st = self.bringup.status()
        with self._lock:
            return {"desired": self._desired, "running": running, "stage": st.stage.value if running else "process_down",
                    "stage_label": st.label if running else "stopped", "ue_ip": self._ue_ip,
                    "server_running": bool(self.iperf.is_running("server") and self._server_ip == self._ue_ip
                                           and self._ue_ip),
                    "scenario": self._scenario}

    def steps(self, s: dict, link_up: bool) -> list[tuple[str, str]]:
        rank = _STAGE_RANK.get(BringupStage(s["stage"]), 0) if s["running"] else 0
        if s["ue_ip"]:
            rank = _STAGE_RANK[BringupStage.READY]
        done = [rank >= _STAGE_RANK[BringupStage.SEARCHING_SSB], rank >= _STAGE_RANK[BringupStage.WAITING_RACH],
                rank >= _STAGE_RANK[BringupStage.WAITING_PDU], bool(s["ue_ip"]), bool(s["server_running"]), link_up]
        out, blocked = [], False
        for i, (name, ok) in enumerate(zip(STEPS, done)):
            if name == "Wi-Fi":  # independent of the radio steps
                out.append((name, "ok" if ok else ("busy" if s["desired"] else "idle")))
                continue
            out.append((name, "ok" if ok and not blocked else ("busy" if s["desired"] and not blocked else "idle")))
            blocked = blocked or not ok
        return out

    def view(self) -> UeView:
        s = self.status()
        link_up = self.server.connected()
        with self._lock:
            return UeView(running=s["running"], desired=s["desired"], stage=s["stage"], stage_label=s["stage_label"],
                          ue_ip=s["ue_ip"], server_running=s["server_running"], link_up=link_up, peer=self.server.peer,
                          rx_silent_s=self._rx_silent_s(s["server_running"]),
                          scenario=self._scenario, steps=self.steps(s, link_up), samples=list(self._samples),
                          events=list(self._events), session_t0=self._session_t0, popup=dict(self._popup),
                          by_scenario={k: list(v) for k, v in self._by_scenario.items()})


def _title(key: str) -> str:
    return {"legacy": "Legacy", "same": "Same vendor", "cross": "Cross vendor"}.get(key, key)
