"""Three-scenario demo control on top of the vendored OAI controller (common/oai).

Scenarios and what changes on a click (nothing restarts):
  legacy : AI feedback off on gNB and UE (scheduled "aifb schedule 0 <frame>" on both, same SFN)
  same   : AI feedback on, gNB decoder 0 = vendor-1 model (trained end to end with the UE encoder)
  cross  : AI feedback on, gNB decoder 1 = vendor-2 decoder for a vendor-1 source ("aifb decoder 1 <frame>")
Same <-> Cross only swaps the gNB decoder (the UE's encoder is model 1 in both). Going from Legacy to an AI
scenario first selects the decoder (harmless while legacy) and then turns AI on, so the first AI report
is already decoded by the right decoder.

The GUI polls view(): one consistent copy of everything it draws (bring-up steps, scenario, per-second
samples, event log). All OAI work runs on background threads.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from common.oai.config import AppConfig
from common.oai.controllers.controller import Controller, RuntimeSnapshot
from common.oai.net.metrics_ingest import MacStatsIngest
from common.oai.net.mode_control import MODE_CSINET, MODE_LEGACY, parse_aifb_status, telnet_command

log = logging.getLogger(__name__)

SCENARIO_KEYS = ("legacy", "same", "cross")
CSI_MODE = {"legacy": MODE_LEGACY, "same": MODE_CSINET, "cross": MODE_CSINET}
DECODER = {"same": 0, "cross": 1}
STEPS = ("gNB", "USRPs", "UE sync", "Attach", "IP", "Traffic")
_UE_SYNCED = {"waiting_rach", "waiting_rrc", "waiting_pdu", "waiting_tun", "ready"}
_UE_ATTACHED = {"waiting_pdu", "waiting_tun", "ready"}


@dataclass
class Sample:
    """One point per second for the plots, tagged with the scenario in effect."""

    t: float
    scenario: str
    tput_mbps: float
    bler: float
    mcs: float
    cqi: float
    ul_snr_db: float
    rsrp_dbm: float


@dataclass
class Event:
    clock: str  # hh:mm:ss
    level: str  # ok | busy | fail | info
    text: str


@dataclass
class DemoView:
    radio: RuntimeSnapshot
    scenario: str
    target_scenario: str
    switching: bool
    steps: list[tuple[str, str]]  # (name, ok | busy | idle | fail)
    samples: list[Sample]
    events: list[Event]
    csi_record_root: str
    last: Sample | None = None
    decoder: int = -1
    aifb: dict[str, str] = field(default_factory=dict)


def bringup_steps(s: RuntimeSnapshot) -> list[tuple[str, str]]:
    """Map the controller's boot/bring-up state to the GUI's six steps."""
    gnb_stage, ue_stage = s.gnb_boot_stage or "", s.ue_bringup_stage or ""
    done = [
        s.gnb_running and gnb_stage in ("config", "ru_init", "rf_ssb"),
        s.gnb_running and gnb_stage == "rf_ssb",
        s.ue_running and ue_stage in _UE_SYNCED,
        s.ue_running and (s.ue_attached or ue_stage in _UE_ATTACHED),
        bool(s.ue_ip),
        s.traffic_stage == "running" and s.iperf_client_running,
    ]
    active = [s.gnb_running, s.gnb_running, s.ue_running, s.ue_running, s.ue_running,
              s.traffic_desired or s.iperf_client_running]
    out, blocked = [], False
    for name, ok, act in zip(STEPS, done, active):
        state = "ok" if ok and not blocked else ("busy" if act and not blocked else "idle")
        if s.traffic_stage == "error" and name == "Traffic":
            state = "fail"
        out.append((name, state))
        blocked = blocked or not ok
    return out


class DemoController:
    def __init__(self, cfg: AppConfig, controller: Controller | None = None):
        self.cfg = cfg
        self.ctrl = controller or Controller(cfg)
        boot = cfg.csi.default_mode
        self.scenario = "legacy" if boot == MODE_LEGACY else "same"  # modems always boot with decoder 0
        self.target = self.scenario
        self.decoder = 0
        self.lead = max(3, int(cfg.features.live_mode_lead_frames or 15))
        self._lock = threading.RLock()  # event() is also called with the lock held
        self._switch_lock = threading.Lock()
        self._switching = False
        self._samples: deque[Sample] = deque(maxlen=3600)
        self._events: deque[Event] = deque(maxlen=300)
        self._stats = MacStatsIngest(Path(cfg.oai.build_dir).expanduser() / cfg.oai.stats_log)
        self._stop = threading.Event()
        self._sampler: threading.Thread | None = None
        self._t0 = time.monotonic()
        self._aifb: dict[str, str] = {}

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.ctrl.start_background_workers()
        self._sampler = threading.Thread(target=self._sample_loop, name="demo-sampler", daemon=True)
        self._sampler.start()

    def shutdown(self) -> None:
        self._stop.set()
        try:
            self.stop_system()
        finally:
            self.ctrl.stop_background_workers()

    def start_system(self) -> None:
        self.event("busy", "Starting gNB, UE and traffic")
        threading.Thread(target=self._start_worker, name="demo-start", daemon=True).start()

    def stop_system(self) -> None:
        self.event("busy", "Stopping traffic, UE and gNB")
        try:
            self.ctrl.stop_iperf_traffic()
        except Exception as exc:  # noqa: BLE001
            log.warning("stop traffic: %s", exc)
        self.ctrl.stop_system()
        with self._lock:
            self.decoder = 0  # next boot starts on decoder 0
            if self.scenario == "cross":
                self.scenario = "same"
            self.target = self.scenario
        self.event("ok", "System stopped")

    def _start_worker(self) -> None:
        self.ctrl.start_system()
        t0 = time.monotonic()
        while time.monotonic() - t0 < 300 and not self._stop.is_set():
            s = self.ctrl.get_snapshot()
            if s.traffic_stage == "running" and s.iperf_client_running:
                self.event("ok", f"Traffic running to UE {s.ue_ip}")
                with self._lock:
                    want = self.target
                if want != self.scenario:  # a scenario picked before the system was up
                    self.set_scenario(want)
                return
            time.sleep(0.5)

    # ------------------------------------------------------------------ scenarios
    def set_scenario(self, key: str) -> bool:
        """Request a scenario; False if refused (unknown key or a switch already running)."""
        if key not in SCENARIO_KEYS:
            return False
        with self._lock:
            if self._switching:
                self.event("info", "Switch already in progress, try again in a moment")
                return False
            self.target = key
            if key == self.scenario:
                return True
            self._switching = True
        threading.Thread(target=self._switch_worker, args=(key,), name="demo-switch", daemon=True).start()
        return True

    def _switch_worker(self, key: str) -> None:
        prev = self.scenario
        try:
            with self._switch_lock:
                if not self.ctrl._softmodems_ready_for_live_mode():
                    # Not running: Legacy/AI is applied at boot (always with decoder 0, i.e. Same vendor);
                    # Cross vendor's decoder swap follows once traffic runs (_start_worker).
                    self.ctrl.set_csi_mode(CSI_MODE[key])
                    with self._lock:
                        self.scenario = "legacy" if key == "legacy" else "same"
                    self.event("info", f"{_title(key)} selected; applied when the system is running")
                    return
                t0 = time.monotonic()
                if key in DECODER and self.decoder != DECODER[key]:
                    self._swap_decoder(DECODER[key])
                if self.ctrl.get_snapshot().csi_mode != CSI_MODE[key]:
                    self._switch_ai(CSI_MODE[key])
                status = self._aifb_status()
                ai_on = status.get("pucch_replace") == "1"
                if ai_on != (key != "legacy") or (key in DECODER and int(status.get("decoder", -1)) != DECODER[key]):
                    raise RuntimeError(f"gNB reports pucch_replace={status.get('pucch_replace')} "
                                       f"decoder={status.get('decoder')}")
                with self._lock:
                    self.scenario = key
                self.event("ok", f"{_title(prev)} -> {_title(key)} in {time.monotonic() - t0:.2f} s")
        except Exception as exc:  # noqa: BLE001
            log.exception("scenario switch failed")
            self.event("fail", f"Switch to {_title(key)} failed: {exc}")
            with self._lock:
                self.target = self.scenario
        finally:
            with self._lock:
                self._switching = False

    def _aifb_status(self) -> dict[str, str]:
        st = parse_aifb_status(telnet_command(self.cfg.network.gnb_host, self.cfg.network.gnb_telnet_port, "aifb status"))
        with self._lock:
            self._aifb = st
        return st

    def _swap_decoder(self, slot: int) -> None:
        """gNB only: scheduled decoder swap; returns once the gNB has committed it."""
        st = self._aifb_status()
        if st.get("alt_decoder_loaded") != "1":
            raise RuntimeError("the gNB has no alternative decoder (--ai-fb-live-alt-model-path)")
        target = (int(st["frame"]) + self.lead) % 1024
        reply = telnet_command(self.cfg.network.gnb_host, self.cfg.network.gnb_telnet_port, f"aifb decoder {slot} {target}")
        if f"armed decoder={slot}" not in reply:
            raise RuntimeError(reply.strip() or "no reply to aifb decoder")
        self.event("busy", f"gNB decoder {slot} armed for frame {target}")
        deadline = time.monotonic() + max(2.0, (self.lead + 20) * 0.01 * 4)
        while time.monotonic() < deadline:
            time.sleep(0.05)
            st = self._aifb_status()
            if st.get("decoder") == str(slot) and st.get("decoder_pending") == "0":
                with self._lock:
                    self.decoder = slot
                self.event("ok", f"gNB decoder {slot} active from frame {target}")
                return
        raise RuntimeError(f"gNB did not commit decoder {slot} (status {st})")

    def _switch_ai(self, mode: str) -> None:
        """Legacy <-> AI on both sides through the controller's scheduled switch."""
        self.event("busy", f"Feedback {'AI' if mode == MODE_CSINET else 'legacy'}: arming gNB and UE")
        self.ctrl.set_csi_mode(mode)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            s = self.ctrl.get_snapshot()
            if not s.mode_pending:
                if s.csi_mode != mode or "failed" in s.status.lower():
                    raise RuntimeError(s.note or s.status)
                self.event("ok", s.note or f"Feedback {mode} applied")
                return
            time.sleep(0.05)
        raise RuntimeError("Legacy/AI switch timed out")

    # ------------------------------------------------------------------ data for the GUI
    def event(self, level: str, text: str) -> None:
        with self._lock:
            self._events.append(Event(time.strftime("%H:%M:%S"), level, text))
        log.info("[%s] %s", level, text)

    def _sample_loop(self) -> None:
        while not self._stop.wait(1.0):
            s = self.ctrl.get_snapshot()
            if not (s.gnb_running and s.ue_running):
                continue
            st = self._stats.poll()
            with self._lock:
                scen = self.scenario if not self._switching else self.target
                self._samples.append(Sample(
                    t=time.monotonic() - self._t0, scenario=scen, tput_mbps=s.throughput_mbps, bler=s.bler,
                    mcs=st.dl_mcs if st else float("nan"), cqi=float(s.cqi),
                    ul_snr_db=st.ul_snr_db if st else float("nan"), rsrp_dbm=st.rsrp_dbm if st else float("nan")))

    def view(self) -> DemoView:
        radio = self.ctrl.get_snapshot()
        with self._lock:
            samples = list(self._samples)
            return DemoView(radio=radio, scenario=self.scenario, target_scenario=self.target,
                            switching=self._switching, steps=bringup_steps(radio), samples=samples,
                            events=list(self._events), csi_record_root=str(self.ctrl.csirs_record_root or ""),
                            last=samples[-1] if samples else None, decoder=self.decoder, aifb=dict(self._aifb))


def _title(key: str) -> str:
    return {"legacy": "Legacy", "same": "Same vendor", "cross": "Cross vendor"}[key]
