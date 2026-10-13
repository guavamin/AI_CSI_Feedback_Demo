"""Orchestration layer — no DearPyGui imports."""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from common.oai.config import AppConfig
from common.oai.data.experiment_logger import ExperimentLogger
from common.oai.net.csirs_ingest import CsirsRecordIngest, ensure_record_tree
from common.oai.net.csirs_udp import CsirsUdpIngest, run_forwarder
from common.oai.net.iperf_control import IperfLauncher
from common.oai.net.metrics_ingest import MacStatsIngest
from common.oai.net.mode_control import (
    TelnetProbe,
    apply_mode_scheduled,
    apply_mode_via_telnet,
    parse_aifb_status,
    relaunch_for_mode,
    telnet_command,
)
from common.oai.net.modem_boot import GnbBootMonitor, GnbBootStage
from common.oai.net.softmodem_launcher import SoftmodemLauncher
from common.oai.net.ue_bringup import BringupStage, BringupTimeouts, UeBringupMonitor
from common.oai.net.ue_ip import discover_ue_ipv4, verify_ue_ping
from common.oai.utils.stats import ModeDistribution

log = logging.getLogger(__name__)

MODE_LEGACY = "legacy"
MODE_CSINET = "csinet"

# UE stages before radio attach / PDU (GUI + traffic waiting_attach)
_UE_PRE_ATTACH = (
    BringupStage.STARTING,
    BringupStage.INIT,
    BringupStage.RADIO,
    BringupStage.SEARCHING_SSB,
    BringupStage.WAITING_RACH,
    BringupStage.WAITING_RRC,
)


@dataclass
class RuntimeSnapshot:
    """Thread-safe GUI-facing state (poll from the UI thread)."""

    status: str = "Idle"
    gnb_running: bool = False
    ue_running: bool = False
    ue_attached: bool = False
    ue_ip: str = ""
    iperf_server_running: bool = False
    iperf_client_running: bool = False
    iperf_console: str = ""
    # Data traffic desired-state (Start traffic = on until Stop traffic)
    traffic_desired: bool = False
    # idle | ensuring_ue | waiting_attach | waiting_ue_ip | starting_iperf | running | restarting_ue | stopping | error
    traffic_stage: str = "idle"
    # Stage-aware UE bring-up (cmd→…→TUN); empty when monitor disabled / process down
    ue_bringup_stage: str = ""
    ue_bringup_label: str = ""
    # gNB boot (cmd→config→RU→SSB TX)
    gnb_boot_stage: str = ""
    gnb_boot_label: str = ""
    csi_mode: str = MODE_LEGACY
    mode_pending: bool = False
    ri: int = 0
    pmi_x1: int = 0
    pmi_x2: int = 0
    cqi: int = 0
    bler: float = float("nan")
    throughput_mbps: float = float("nan")
    last_csi_age_s: float = float("nan")
    t_rel_series: list[float] = field(default_factory=list)
    ri_series: list[float] = field(default_factory=list)
    cqi_series: list[float] = field(default_factory=list)
    pmi_x1_series: list[float] = field(default_factory=list)
    pmi_x2_series: list[float] = field(default_factory=list)
    bler_series: list[float] = field(default_factory=list)
    tput_series: list[float] = field(default_factory=list)
    # UE CSI-RS channel estimates (magnitude vs subcarrier), one series per RX-TX link
    csirs_view_enabled: bool = False
    csirs_n_rx: int = 1
    csirs_n_ports: int = 2
    csirs_n_subc: int = 128
    csirs_n_fft: int = 0  # OFDM size used for signed axis / markers (0 = unknown)
    csirs_freq_axis: str = "native"  # native | signed (mirrors plot config for GUI)
    csirs_subc: list[float] = field(default_factory=list)
    # list of (label, magnitudes[n_subc])
    csirs_links: list[tuple[str, list[float]]] = field(default_factory=list)
    csirs_frame: int = 0
    csirs_slot: int = 0
    # Distribution tab: live mode updates; other mode CDF/avg stays frozen
    dist_legacy: ModeDistribution = field(default_factory=ModeDistribution)
    dist_csinet: ModeDistribution = field(default_factory=ModeDistribution)
    log_path: str = ""
    note: str = ""


class Controller:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self._lock = threading.Lock()
        mode = (cfg.csi.default_mode or MODE_LEGACY).strip().lower()
        if mode not in (MODE_LEGACY, MODE_CSINET):
            mode = MODE_LEGACY
        self.snapshot = RuntimeSnapshot(
            csi_mode=mode,
            csirs_view_enabled=bool(cfg.gui.csirs_view_enabled),
            csirs_n_rx=max(1, int(cfg.csi.n_rx)),
            csirs_n_ports=max(1, int(cfg.csi.n_ports)),
            csirs_n_subc=max(8, int(cfg.csi.n_subc)),
        )
        self._stop = threading.Event()
        self._sim_thread: threading.Thread | None = None
        self._lab_thread: threading.Thread | None = None
        self._t0 = time.monotonic()
        self._last_csi_t = self._t0
        self.logger: ExperimentLogger | None = None
        self.launcher: SoftmodemLauncher | None = None
        self.ingest: MacStatsIngest | None = None
        self.csirs_ingest: CsirsRecordIngest | None = None
        self.csirs_udp: CsirsUdpIngest | None = None
        self.csirs_record_root: Path | None = None
        self._csirs_fwd_stop: threading.Event | None = None
        self._csirs_fwd_thread: threading.Thread | None = None
        self.iperf: IperfLauncher | None = None
        self._mode_lock = threading.Lock()
        self._traffic_desired = False
        self._traffic_thread: threading.Thread | None = None
        self._traffic_lock = threading.Lock()
        self._iperf_bound_ue_ip: str = ""
        self._traffic_ue_start_mono: float = 0.0
        self._traffic_ip_wait_mono: float = 0.0
        self._settle_ue_ip: str = ""
        self._settle_ue_ip_mono: float = 0.0
        # True after iperf server+client have run in this desired-on session.
        self._traffic_streamed: bool = False
        # True once a live PDU/TUN IP was seen (iperf may then be attempted).
        self._pdu_seen: bool = False
        self._pdu_retry_count: int = 0
        self._pdu_retry_deadline_mono: float = 0.0
        self._traffic_ip_lost_mono: float = 0.0
        self._ping_verified_ip: str = ""
        self._ue_bringup = UeBringupMonitor(
            BringupTimeouts(
                ssb_s=float(cfg.iperf.bringup_ssb_timeout_s),
                rach_s=float(cfg.iperf.bringup_rach_timeout_s),
                rrc_s=float(cfg.iperf.bringup_rrc_timeout_s),
                pdu_s=float(cfg.iperf.bringup_pdu_timeout_s),
                tun_s=float(cfg.iperf.bringup_tun_timeout_s),
            )
        )
        self._gnb_boot = GnbBootMonitor()
        self._boot_mon_lock = threading.Lock()
        self._system_thread: threading.Thread | None = None
        self._system_cancel = threading.Event()

        log_root = Path(cfg.logging.root_dir)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        if cfg.features.record_run:
            self.logger = ExperimentLogger(root_dir=cfg.logging.root_dir, stamp=stamp)
            self.snapshot.log_path = str(self.logger.csv_path)
            log.info("Logging to %s", self.snapshot.log_path)

        transport = (cfg.oai.csi_transport or "file").strip().lower()
        use_file = transport in ("file", "both")
        use_udp = transport in ("udp", "both")
        csi_root: Path | None = None

        if cfg.features.softmodem_control:
            run_logs = log_root / "softmodem_logs" / stamp
            self.launcher = SoftmodemLauncher(cfg, run_logs)
            # Drop leftovers from a previous GUI crash so only this session owns gNB/UE
            try:
                self.launcher.kill_stray_softmodems()
            except Exception as exc:  # pragma: no cover
                log.warning("Could not clear stray softmodems at startup: %s", exc)
            stats = Path(cfg.oai.build_dir).expanduser() / cfg.oai.stats_log
            self.ingest = MacStatsIngest(stats)
            log.info("Softmodem launcher ready; stats path %s", stats)
            # Option A always needs on-disk H dumps from the UE, even when the GUI
            # consumes them only via UDP (local or remote forwarder).
            if cfg.oai.csi_record:
                csi_root = (log_root / "csi_record" / stamp).expanduser().resolve()
                ensure_record_tree(csi_root)
                self.csirs_record_root = csi_root
                self.launcher.csi_record_path = csi_root
                if use_file:
                    self.csirs_ingest = CsirsRecordIngest(
                        csi_root, keep_h=cfg.oai.csi_record_keep_h
                    )
                log.info(
                    "UE CSI-RS recording enabled → %s (transport=%s, keep last %d H bins)",
                    csi_root,
                    transport,
                    cfg.oai.csi_record_keep_h,
                )

        if use_udp and (cfg.oai.csi_record or transport == "udp"):
            self.csirs_udp = CsirsUdpIngest(
                host=cfg.network.csirs_udp_bind or "0.0.0.0",
                port=int(cfg.network.csirs_udp_port),
            )
            try:
                self.csirs_udp.start()
            except OSError as exc:
                log.error("CSI-RS UDP ingest bind failed: %s", exc)
                self.csirs_udp = None
            else:
                log.info(
                    "CSI-RS UDP ingest ready on %s",
                    self.csirs_udp.bind_addr,
                )

            # Same-PC (or GUI-owned UE): auto-start forwarder → dest host:port
            if (
                self.csirs_udp is not None
                and csi_root is not None
                and cfg.features.softmodem_control
            ):
                dest_host = (cfg.oai.csi_udp_dest_host or "127.0.0.1").strip()
                dest_port = int(cfg.network.csirs_udp_port)
                self._csirs_fwd_stop = threading.Event()
                self._csirs_fwd_thread = threading.Thread(
                    target=run_forwarder,
                    kwargs={
                        "record_path": csi_root,
                        "dest_host": dest_host,
                        "dest_port": dest_port,
                        "keep_h": cfg.oai.csi_record_keep_h,
                        "poll_ms": 5.0,
                        "follow_latest_under": False,
                        "stop_event": self._csirs_fwd_stop,
                    },
                    name="csirs-udp-fwd",
                    daemon=True,
                )
                self._csirs_fwd_thread.start()
                log.info(
                    "CSI-RS UDP forwarder started → %s:%d (watching %s)",
                    dest_host,
                    dest_port,
                    csi_root,
                )

        if cfg.features.iperf_control:
            iperf_logs = log_root / "iperf_logs" / stamp
            self.iperf = IperfLauncher(cfg, iperf_logs)
            log.info("iperf control ready; logs under %s", iperf_logs)

    def get_snapshot(self) -> RuntimeSnapshot:
        if self.iperf is not None:
            console = self.iperf.console_text()
            with self._lock:
                self.snapshot.iperf_console = console
                return deepcopy(self.snapshot)
        with self._lock:
            return deepcopy(self.snapshot)

    def start_background_workers(self) -> None:
        if self.cfg.features.simulate_metrics:
            self._sim_thread = threading.Thread(
                target=self._simulate_loop, name="sim-metrics", daemon=True
            )
            self._sim_thread.start()
            with self._lock:
                self.snapshot.status = "Idle — start gNB and UE"
                self.snapshot.note = (
                    "Use Modem control to start gNB then UE before telemetry flows"
                )
            log.info("Started offline metrics simulator (waiting for Modem control)")
            return

        if self.cfg.features.softmodem_control:
            self._lab_thread = threading.Thread(
                target=self._lab_loop, name="lab-metrics", daemon=True
            )
            self._lab_thread.start()
            with self._lock:
                self.snapshot.status = "Lab ready — start gNB then UE"
                self.snapshot.note = (
                    "Softmodem control + nrMAC_stats ingest active "
                    "(mode switch relaunches processes)"
                )
            log.info("Started lab softmodem/metrics worker")
            return

        with self._lock:
            self.snapshot.status = "Ready"
            self.snapshot.note = "No simulate_metrics and no softmodem_control"

    def stop_background_workers(self) -> None:
        if getattr(self, "_workers_stopped", False):
            return
        self._workers_stopped = True
        self._stop.set()
        self._system_cancel.set()
        with self._traffic_lock:
            self._traffic_desired = False
        if self._system_thread and self._system_thread.is_alive():
            self._system_thread.join(timeout=2.0)
        if self._traffic_thread and self._traffic_thread.is_alive():
            self._traffic_thread.join(timeout=3.0)
        if self._sim_thread and self._sim_thread.is_alive():
            self._sim_thread.join(timeout=2.0)
        if self._lab_thread and self._lab_thread.is_alive():
            self._lab_thread.join(timeout=2.0)
        if self.launcher:
            try:
                self.launcher.stop_all()
            except Exception as exc:  # pragma: no cover
                log.warning("Error stopping softmodems: %s", exc)
        if self._csirs_fwd_stop is not None:
            self._csirs_fwd_stop.set()
            if self._csirs_fwd_thread is not None:
                self._csirs_fwd_thread.join(timeout=2.0)
            self._csirs_fwd_stop = None
            self._csirs_fwd_thread = None
        if self.csirs_udp is not None:
            try:
                self.csirs_udp.close()
            except Exception as exc:  # pragma: no cover
                log.warning("Error stopping CSI-RS UDP ingest: %s", exc)
            self.csirs_udp = None
        if self.csirs_ingest is not None:
            try:
                self.csirs_ingest.wipe()
            except Exception as exc:  # pragma: no cover
                log.warning("Error wiping CSI record dir: %s", exc)
            self.csirs_ingest = None
        elif self.csirs_record_root is not None:
            try:
                from common.oai.net.csirs_ingest import wipe_record_dir

                wipe_record_dir(self.csirs_record_root)
            except Exception as exc:  # pragma: no cover
                log.warning("Error wiping CSI record dir: %s", exc)
        self.csirs_record_root = None
        if self.iperf:
            try:
                self.iperf.stop_all()
            except Exception as exc:  # pragma: no cover
                log.warning("Error stopping iperf: %s", exc)
        if self.logger:
            self.logger.close()
            self.logger = None

    # --- GUI actions ---------------------------------------------------------

    def _system_sleep(self, seconds: float) -> bool:
        """Sleep during system bring-up; False if cancelled / app stopping."""
        deadline = time.monotonic() + max(0.0, float(seconds))
        while time.monotonic() < deadline:
            if self._stop.is_set() or self._system_cancel.is_set():
                return False
            time.sleep(min(0.25, deadline - time.monotonic()))
        return not (self._stop.is_set() or self._system_cancel.is_set())

    def start_system(self) -> None:
        """One-click demo bring-up: gNB → UE → supervised data traffic.

        After softmodems are up, enables the traffic supervisor (attach + UE IP +
        iperf, with UE re-init as needed). Safe to click while already running
        (no-ops / re-asserts traffic desired).
        """
        if self._system_thread is not None and self._system_thread.is_alive():
            with self._lock:
                self.snapshot.status = "System: bring-up in progress"
                self.snapshot.note = "Wait for gNB/UE/traffic stages to finish"
            return

        self._system_cancel.clear()

        def _run() -> None:
            try:
                self._system_bringup_worker()
            except Exception as exc:  # pragma: no cover
                log.exception("System bring-up failed: %s", exc)
                with self._lock:
                    self.snapshot.status = "System: bring-up failed"
                    self.snapshot.note = str(exc)

        self._system_thread = threading.Thread(
            target=_run, name="system-bringup", daemon=True
        )
        self._system_thread.start()
        log.info("System bring-up started")

    def stop_system(self) -> None:
        """Stop traffic, then UE, then gNB (full teardown)."""
        self._system_cancel.set()
        with self._lock:
            self.snapshot.status = "System: stopping"
            self.snapshot.note = "Stopping traffic, UE, and gNB…"
        if self.cfg.features.iperf_control:
            self.stop_iperf_traffic()
        self.stop_ue()
        self.stop_gnb()
        with self._lock:
            self.snapshot.status = "System: stopped"
            self.snapshot.note = "gNB/UE/traffic cleared — Start system to bring up again"
        log.info("System stopped")

    def _system_bringup_worker(self) -> None:
        with self._lock:
            self.snapshot.status = "System: starting gNB"
            self.snapshot.note = "Bringing up gNB…"

        if not self._gnb_process_running():
            self.start_gnb()
            if not self._gnb_process_running():
                with self._lock:
                    self.snapshot.status = "System: gNB start failed"
                    self.snapshot.note = self.snapshot.note or "gNB did not stay running"
                return
            boot_s = max(0.5, float(self.cfg.oai.gnb_boot_s))
            with self._lock:
                self.snapshot.status = "System: waiting for gNB"
                self.snapshot.note = f"gNB boot settle ({boot_s:.0f}s)…"
            if not self._system_sleep(boot_s):
                with self._lock:
                    self.snapshot.status = "System: cancelled"
                    self.snapshot.note = "Bring-up cancelled during gNB wait"
                return
        else:
            with self._lock:
                self.snapshot.status = "System: gNB already up"
                self.snapshot.note = "Skipping gNB start"

        if self._stop.is_set() or self._system_cancel.is_set():
            return

        with self._lock:
            self.snapshot.status = "System: starting UE"
            self.snapshot.note = "Starting nr-uesoftmodem…"

        if not self._ue_process_running():
            self.start_ue()
            if not self._ue_process_running():
                with self._lock:
                    self.snapshot.status = "System: UE start failed"
                    self.snapshot.note = self.snapshot.note or "UE did not stay running"
                return
        else:
            with self._lock:
                self.snapshot.note = "UE already running; enabling traffic supervise"

        if self._stop.is_set() or self._system_cancel.is_set():
            return

        # Traffic supervisor waits for attach + TUN IP, starts iperf, re-inits UE as needed
        if self.cfg.features.iperf_control and self.iperf is not None:
            with self._lock:
                self.snapshot.status = "System: enabling traffic"
                self.snapshot.note = (
                    "Waiting for Attached + UE IP, then starting iperf (supervised)"
                )
            self.start_iperf_traffic()
            with self._lock:
                self.snapshot.status = "System: running"
                self.snapshot.note = (
                    "gNB + UE up; data traffic ON — supervising attach/IP/iperf"
                )
        else:
            with self._lock:
                self.snapshot.status = "System: softmodems up"
                self.snapshot.note = (
                    "gNB + UE started (iperf_control disabled — start traffic manually if needed)"
                )
        log.info("System bring-up complete")

    def start_gnb(self) -> None:
        if not self.cfg.features.softmodem_control or self.launcher is None:
            self._stub_start_gnb()
            return
        mode = self._current_mode()
        try:
            self.launcher.start("gnb", mode)
            with self._lock:
                self.snapshot.gnb_running = True
                self.snapshot.status = "gNB started"
                self.snapshot.note = f"argv mode={mode}; start UE next"
            self._arm_gnb_boot_watch()
        except Exception as exc:
            log.exception("gNB start failed")
            with self._lock:
                self.snapshot.gnb_running = False
                self.snapshot.status = "gNB start failed"
                self.snapshot.note = str(exc)

    def stop_gnb(self) -> None:
        if not self.cfg.features.softmodem_control or self.launcher is None:
            self._stub_stop_gnb()
            return
        try:
            self.launcher.stop("gnb")
        except Exception as exc:
            log.exception("gNB stop failed")
            with self._lock:
                self.snapshot.status = "gNB stop error"
                self.snapshot.note = str(exc)
            return
        with self._lock:
            self.snapshot.gnb_running = False
            self.snapshot.ue_attached = False
            self.snapshot.gnb_boot_stage = GnbBootStage.PROCESS_DOWN.value
            self.snapshot.gnb_boot_label = "stopped"
            self.snapshot.status = "gNB stopped"
            self.snapshot.note = "Telemetry paused until gNB and UE are both running"
        self._gnb_boot.mark_process_down()

    def start_ue(self) -> None:
        if not self.cfg.features.softmodem_control or self.launcher is None:
            self._stub_start_ue()
            self._arm_ue_bringup_watch()
            return
        mode = self._current_mode()
        try:
            self.launcher.start("ue", mode)
            with self._lock:
                self.snapshot.ue_running = True
                self.snapshot.ue_attached = False
                self.snapshot.ue_ip = ""
                self.snapshot.status = "UE started"
                self.snapshot.note = f"argv mode={mode}; waiting for attach / stats"
            self._arm_ue_bringup_watch()
        except Exception as exc:
            log.exception("UE start failed")
            with self._lock:
                self.snapshot.ue_running = False
                self.snapshot.status = "UE start failed"
                self.snapshot.note = str(exc)

    def stop_ue(self) -> None:
        if not self.cfg.features.softmodem_control or self.launcher is None:
            self._stub_stop_ue()
            return
        try:
            self.launcher.stop("ue")
        except Exception as exc:
            log.exception("UE stop failed")
            with self._lock:
                self.snapshot.status = "UE stop error"
                self.snapshot.note = str(exc)
            return
        if self.csirs_ingest is not None:
            try:
                self.csirs_ingest.wipe()
                ensure_record_tree(self.csirs_ingest.record_root)
            except Exception as exc:  # pragma: no cover
                log.debug("CSI record wipe on UE stop: %s", exc)
        elif self.csirs_record_root is not None:
            try:
                from common.oai.net.csirs_ingest import wipe_record_dir

                wipe_record_dir(self.csirs_record_root)
                ensure_record_tree(self.csirs_record_root)
            except Exception as exc:  # pragma: no cover
                log.debug("CSI record wipe on UE stop: %s", exc)
        if self.csirs_udp is not None:
            self.csirs_udp.wipe()
        with self._lock:
            self.snapshot.ue_running = False
            self.snapshot.ue_attached = False
            self.snapshot.ue_ip = ""
            self.snapshot.ue_bringup_stage = BringupStage.PROCESS_DOWN.value
            self.snapshot.ue_bringup_label = "stopped"
            self.snapshot.csirs_subc = []
            self.snapshot.csirs_links = []
            self.snapshot.status = "UE stopped"
            self.snapshot.note = "Telemetry paused until gNB and UE are both running"
        self._ue_bringup.mark_process_down()

    def check_gnb_status(self) -> None:
        if self.launcher is not None and self.cfg.features.softmodem_control:
            running = self.launcher.is_running("gnb")
            pid = self.launcher.pid("gnb")
            telnet_ok = TelnetProbe(
                self.cfg.network.gnb_host, self.cfg.network.gnb_telnet_port
            ).reachable()
            with self._lock:
                self.snapshot.gnb_running = running
                state = "RUNNING" if running else "stopped"
                self.snapshot.status = f"gNB status: {state}"
                self.snapshot.note = f"pid={pid} telnet={telnet_ok}"
            log.info("Checked gNB status → %s pid=%s telnet=%s", state, pid, telnet_ok)
            return
        with self._lock:
            state = "RUNNING" if self.snapshot.gnb_running else "stopped"
            self.snapshot.status = f"gNB status: {state} (stub)"
            self.snapshot.note = f"Stub gNB is {state}"
        log.info("Checked gNB status → %s", state)

    def check_ue_status(self) -> None:
        if self.launcher is not None and self.cfg.features.softmodem_control:
            running = self.launcher.is_running("ue")
            pid = self.launcher.pid("ue")
            telnet_ok = TelnetProbe(
                self.cfg.network.ue_host, self.cfg.network.ue_telnet_port
            ).reachable()
            with self._lock:
                self.snapshot.ue_running = running
                attached = self.snapshot.ue_attached
                state = "RUNNING" if running else "stopped"
                att = "attached" if attached else "not attached"
                self.snapshot.status = f"UE status: {state}, {att}"
                self.snapshot.note = f"pid={pid} telnet={telnet_ok}"
            log.info(
                "Checked UE status → %s (%s) pid=%s telnet=%s",
                state,
                att,
                pid,
                telnet_ok,
            )
            return
        with self._lock:
            state = "RUNNING" if self.snapshot.ue_running else "stopped"
            attached = "attached" if self.snapshot.ue_attached else "not attached"
            self.snapshot.status = f"UE status: {state}, {attached} (stub)"
            self.snapshot.note = f"Stub UE is {state} ({attached})"
        log.info("Checked UE status → %s (%s)", state, attached)

    def set_csi_mode(self, mode: str) -> None:
        mode = mode.strip().lower()
        if mode not in (MODE_LEGACY, MODE_CSINET):
            log.warning("Ignoring unknown CSI mode %r", mode)
            return
        with self._lock:
            if self.snapshot.csi_mode == mode:
                return
            self.snapshot.csi_mode = mode
            self.snapshot.mode_pending = True
            self.snapshot.status = f"Mode → {mode}"

        # Prefer live telnet flip when softmodems are already up with telnetsrv.
        if self.cfg.features.live_mode_telnet and self.cfg.features.softmodem_control:
            if self._softmodems_ready_for_live_mode():
                with self._lock:
                    self.snapshot.note = "Applying AI-FB mode via telnet (aifb)…"
                threading.Thread(
                    target=self._telnet_mode_worker,
                    args=(mode,),
                    name="mode-telnet",
                    daemon=True,
                ).start()
                return
            # Softmodems not running yet: arm mode for the next Start system / Start gNB+UE.
            with self._lock:
                self.snapshot.mode_pending = False
                self.snapshot.status = f"Mode → {mode} (armed)"
                self.snapshot.note = (
                    "CSI mode saved — will apply when softmodems start "
                    "(Start system). Live telnet switch is used after they are up."
                )
            log.info("CSI mode armed for next softmodem start → %s", mode)
            return

        if (
            self.cfg.features.mode_switch_relaunch
            and self.cfg.features.softmodem_control
            and self.launcher is not None
        ):
            with self._lock:
                self.snapshot.note = "Relaunching softmodems for new AI-FB mode…"
            threading.Thread(
                target=self._relaunch_mode_worker,
                args=(mode,),
                name="mode-relaunch",
                daemon=True,
            ).start()
            return

        with self._lock:
            if self.cfg.features.simulate_metrics:
                self.snapshot.note = "Simulated mode switch"
                self.snapshot.mode_pending = False
            else:
                self.snapshot.note = "Mode updated (no softmodem control path configured)"
                self.snapshot.mode_pending = False
        log.info("CSI mode set to %s", mode)

    def _softmodems_ready_for_live_mode(self) -> bool:
        """True when gNB+UE are running and both telnet ports answer."""
        if self.launcher is None or not self.cfg.features.softmodem_control:
            return False
        if not (
            self.launcher.is_running("gnb") and self.launcher.is_running("ue")
        ):
            return False
        gnb_ok = TelnetProbe(
            self.cfg.network.gnb_host, self.cfg.network.gnb_telnet_port
        ).reachable()
        ue_ok = TelnetProbe(
            self.cfg.network.ue_host, self.cfg.network.ue_telnet_port
        ).reachable()
        return bool(gnb_ok and ue_ok)

    def _telnet_mode_worker(self, mode: str) -> None:
        t0 = time.monotonic()
        lead = int(self.cfg.features.live_mode_lead_frames or 0)
        try:
            if lead > 0:
                # Scheduled switch: both sides change at the same SFN (OAI gui-live-switch branch).
                def _armed(target: int, used_lead: int) -> None:
                    with self._lock:
                        self.snapshot.note = f"Switching to {mode} at frame {target} (in {used_lead * 10} ms)…"

                res = apply_mode_scheduled(
                    mode=mode,
                    gnb_host=self.cfg.network.gnb_host,
                    gnb_port=self.cfg.network.gnb_telnet_port,
                    ue_host=self.cfg.network.ue_host,
                    ue_port=self.cfg.network.ue_telnet_port,
                    lead_frames=lead,
                    on_armed=_armed,
                )
                with self._lock:
                    self.snapshot.mode_pending = False
                    self.snapshot.status = f"Mode {mode} applied (frame {res.target_frame})"
                    self.snapshot.note = (
                        f"Switched at frame {res.target_frame}, {res.switched_after_s:.2f}s after click "
                        f"(armed {res.armed_after_s * 1000:.0f} ms, verified {res.verified_after_s:.2f}s"
                        f"{', resynced' if res.resynced else ''})"
                    )
                return
            apply_mode_via_telnet(
                mode=mode,
                gnb_host=self.cfg.network.gnb_host,
                gnb_port=self.cfg.network.gnb_telnet_port,
                ue_host=self.cfg.network.ue_host,
                ue_port=self.cfg.network.ue_telnet_port,
            )
            dt = time.monotonic() - t0
            with self._lock:
                self.snapshot.mode_pending = False
                self.snapshot.status = f"Mode {mode} applied (telnet)"
                self.snapshot.note = f"Live aifb pucch_replace in {dt:.2f}s (no relaunch)"
            log.info("CSI mode telnet switch → %s in %.2fs", mode, dt)
        except Exception as exc:
            # Softmodems went down mid-switch: arm mode instead of hard failure.
            if not self._softmodems_ready_for_live_mode():
                with self._lock:
                    self.snapshot.mode_pending = False
                    self.snapshot.status = f"Mode → {mode} (armed)"
                    self.snapshot.note = (
                        f"Telnet unavailable ({exc}); mode saved for next Start system"
                    )
                log.info(
                    "CSI mode armed after telnet miss (softmodems down) → %s", mode
                )
                return
            log.exception("Live telnet mode switch failed")
            # Optional fallback: relaunch if configured
            if (
                self.cfg.features.mode_switch_relaunch
                and self.launcher is not None
            ):
                with self._lock:
                    self.snapshot.note = (
                        f"Telnet failed ({exc}); falling back to softmodem relaunch…"
                    )
                self._relaunch_mode_worker(mode)
                return
            # Show the mode the radio is really in, not the one that was clicked.
            actual = self._read_live_mode() if lead > 0 else None
            with self._lock:
                self.snapshot.mode_pending = False
                self.snapshot.status = "Mode telnet failed"
                self.snapshot.note = str(exc)
                if actual is not None:
                    self.snapshot.csi_mode = actual
                    self.snapshot.note = f"{exc} — radio stays in {actual}"

    def _read_live_mode(self) -> str | None:
        """Current CSI mode according to the gNB's 'aifb status' (None if unreadable)."""
        try:
            status = parse_aifb_status(
                telnet_command(self.cfg.network.gnb_host, self.cfg.network.gnb_telnet_port, "aifb status")
            )
            return MODE_CSINET if status.get("pucch_replace") == "1" else MODE_LEGACY
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read live mode from gNB: %s", exc)
            return None

    def set_csirs_view_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.snapshot.csirs_view_enabled = bool(enabled)
            if enabled:
                if self.csirs_udp is not None and self.csirs_ingest is not None:
                    src = f"UDP {self.csirs_udp.bind_addr} + local H_*.bin"
                elif self.csirs_udp is not None:
                    src = f"UDP {self.csirs_udp.bind_addr}"
                elif self.csirs_ingest is not None:
                    src = f"local {self.csirs_ingest.record_root}"
                else:
                    src = "(no CSI-RS source configured)"
                self.snapshot.note = f"CSI-RS view enabled ← {src}"
            else:
                self.snapshot.note = "CSI-RS view disabled"
        if enabled and self.csirs_ingest is not None:
            # Drop backlog so we only keep the live tip of the dump stream
            try:
                from common.oai.net.csirs_ingest import cleanup_old_h_bins

                cleanup_old_h_bins(
                    self.csirs_ingest.record_root,
                    keep=self.cfg.oai.csi_record_keep_h,
                )
            except Exception as exc:  # pragma: no cover
                log.debug("CSI-RS cleanup on enable failed: %s", exc)
        log.info("CSI-RS view enabled=%s", enabled)

    def _apply_csirs_sample(self, sample_h) -> None:
        from common.oai.net.csirs_ingest import remap_csirs_freq_axis

        axis = str(getattr(self.cfg.plot, "csirs_freq_axis", "native") or "native")
        margin = int(getattr(self.cfg.plot, "csirs_filter_margin", 32) or 32)
        subc, links, n_fft = remap_csirs_freq_axis(
            list(sample_h.subc),
            list(sample_h.links),
            axis=axis,
            n_subc_full=int(sample_h.n_subc),
            filter_margin=margin,
        )
        with self._lock:
            self.snapshot.csirs_subc = subc
            self.snapshot.csirs_links = links
            self.snapshot.csirs_frame = int(sample_h.frame)
            self.snapshot.csirs_slot = int(sample_h.slot)
            self.snapshot.csirs_n_rx = int(sample_h.n_rx)
            self.snapshot.csirs_n_ports = int(sample_h.n_ports)
            self.snapshot.csirs_n_subc = int(sample_h.n_subc)
            self.snapshot.csirs_n_fft = int(n_fft)
            self.snapshot.csirs_freq_axis = axis.strip().lower() or "native"

    def _poll_csirs_record(self, *, view_enabled: bool) -> None:
        """Prefer UDP (remote UE); fall back to local H_*.bin when configured."""
        if self.csirs_udp is None and self.csirs_ingest is None:
            return

        if not view_enabled:
            if self.csirs_ingest is not None:
                from common.oai.net.csirs_ingest import cleanup_old_h_bins

                cleanup_old_h_bins(
                    self.csirs_ingest.record_root, keep=self.cfg.oai.csi_record_keep_h
                )
            return

        # UDP first — latest-wins receiver already dropped older datagrams.
        if self.csirs_udp is not None:
            try:
                sample_u = self.csirs_udp.poll()
            except Exception as exc:  # pragma: no cover
                log.debug("CSI-RS UDP poll failed: %s", exc)
                sample_u = None
            if sample_u is not None:
                self._apply_csirs_sample(sample_u)
                return

        if self.csirs_ingest is not None:
            from common.oai.net.csirs_ingest import cleanup_old_h_bins, list_h_bins

            try:
                sample_h = self.csirs_ingest.poll(cleanup=True)
            except Exception as exc:  # pragma: no cover
                log.debug("CSI-RS file poll failed: %s", exc)
                return
            if sample_h is not None:
                self._apply_csirs_sample(sample_h)
                return
            n_bins = len(list_h_bins(self.csirs_ingest.record_root))
            with self._lock:
                if not self.snapshot.csirs_links:
                    self.snapshot.note = (
                        f"Waiting for H_*.bin under {self.csirs_ingest.record_root}"
                        f" (found {n_bins})"
                    )
            return

        with self._lock:
            if not self.snapshot.csirs_links and self.csirs_udp is not None:
                st = self.csirs_udp.stats()
                self.snapshot.note = (
                    f"Waiting for CSI-RS UDP on {self.csirs_udp.bind_addr}"
                    f" (rx={st['rx']})"
                )

    def clear_distributions(self) -> None:
        with self._lock:
            self.snapshot.dist_legacy.clear()
            self.snapshot.dist_csinet.clear()
            self.snapshot.note = "Distribution histories cleared"
        log.info("Cleared legacy/CSINet distribution histories")

    # --- iperf traffic -------------------------------------------------------

    def _set_traffic_stage(self, stage: str, *, status: str | None = None, note: str = "") -> None:
        with self._lock:
            self.snapshot.traffic_stage = stage
            self.snapshot.traffic_desired = bool(self._traffic_desired)
            if status is not None:
                self.snapshot.status = status
            if note:
                self.snapshot.note = note

    def _traffic_still_desired(self) -> bool:
        return bool(self._traffic_desired) and not self._stop.is_set()

    def _traffic_sleep(self, seconds: float) -> bool:
        """Sleep up to ``seconds``; return True if traffic is still desired afterward."""
        deadline = time.monotonic() + max(0.0, float(seconds))
        while time.monotonic() < deadline:
            if not self._traffic_still_desired():
                return False
            time.sleep(min(0.25, deadline - time.monotonic()))
        return self._traffic_still_desired()

    def _stop_iperf_pair(self, *, update_snapshot: bool = True) -> None:
        if self.iperf is None:
            return
        try:
            self.iperf.stop_all()
        except Exception as exc:  # pragma: no cover
            log.warning("iperf stop_all: %s", exc)
        self._iperf_bound_ue_ip = ""
        if update_snapshot:
            with self._lock:
                self.snapshot.iperf_server_running = False
                self.snapshot.iperf_client_running = False

    def _arm_ue_bringup_watch(self) -> None:
        """Start stage timers from this UE boot (follow new log lines only)."""
        self._traffic_ue_start_mono = time.monotonic()
        self._traffic_ip_wait_mono = 0.0
        self._clear_ue_ip_settle()
        self._ue_bringup.reset(self._ue_log_path(), follow_new_only=True)
        st = self._ue_bringup.status()
        with self._lock:
            self.snapshot.ue_bringup_stage = st.stage.value
            self.snapshot.ue_bringup_label = st.label

    def _arm_gnb_boot_watch(self) -> None:
        """Start gNB boot stages from this start (follow new log lines only)."""
        self._gnb_boot.reset(self._gnb_log_path(), follow_new_only=True)
        st = self._gnb_boot.status()
        with self._lock:
            self.snapshot.gnb_boot_stage = st.stage.value
            self.snapshot.gnb_boot_label = st.label

    def _clear_ue_ip_settle(self) -> None:
        self._settle_ue_ip = ""
        self._settle_ue_ip_mono = 0.0
        self._ping_verified_ip = ""

    def _clear_pdu_retry_state(self) -> None:
        self._pdu_retry_count = 0
        self._pdu_retry_deadline_mono = 0.0
        self._traffic_ip_lost_mono = 0.0

    def _reset_traffic_session_flags(self) -> None:
        self._traffic_streamed = False
        self._pdu_seen = False
        self._clear_pdu_retry_state()
        self._clear_ue_ip_settle()

    def _handle_pdu_unavailable(
        self,
        *,
        iface: str,
        now: float,
        detail: str = "",
    ) -> None:
        """Wait/retry for PDU after it was once available; UE re-init only after max fails.

        Sequence after PDU loss (or failed iperf start):
          1) debounce ``ip_lost_grace_s``
          2) wait ``pdu_retry_wait_s`` for PDU (repeat up to ``pdu_retry_max``)
          3) only then re-init the UE
        """
        self._clear_ue_ip_settle()
        grace = max(0.0, float(self.cfg.iperf.ip_lost_grace_s))
        wait_s = max(0.0, float(self.cfg.iperf.pdu_retry_wait_s))
        if wait_s <= 0.0:
            wait_s = 0.5
        max_tries = max(1, int(self.cfg.iperf.pdu_retry_max))

        if self._traffic_ip_lost_mono <= 0.0:
            self._traffic_ip_lost_mono = now
            self._pdu_retry_deadline_mono = 0.0

        lost_for = now - self._traffic_ip_lost_mono
        if lost_for < grace:
            remaining = grace - lost_for
            self._set_traffic_stage(
                "waiting_ue_ip",
                status="Traffic: PDU lost — debounce",
                note=(
                    f"PDU/IP gone on {iface}; debounce {remaining:.1f}s "
                    f"before retry {self._pdu_retry_count + 1}/{max_tries}"
                    + (f" ({detail})" if detail else "")
                ),
            )
            return

        if self._pdu_retry_deadline_mono <= 0.0:
            self._pdu_retry_deadline_mono = now + wait_s

        remaining_wait = self._pdu_retry_deadline_mono - now
        if remaining_wait > 0.0:
            self._set_traffic_stage(
                "waiting_ue_ip",
                status="Traffic: waiting for PDU (retry)",
                note=(
                    f"PDU/IP missing on {iface}; retry "
                    f"{min(self._pdu_retry_count + 1, max_tries)}/{max_tries} "
                    f"— wait {remaining_wait:.1f}s for PDU before counting failure"
                    + (f" ({detail})" if detail else "")
                ),
            )
            return

        # Wait window expired with no PDU → count a failed attempt
        self._pdu_retry_count += 1
        self._pdu_retry_deadline_mono = 0.0
        if self._pdu_retry_count >= max_tries:
            self._traffic_streamed = False
            self._pdu_seen = False
            self._clear_pdu_retry_state()
            self._restart_ue_for_traffic(
                reason=(
                    f"PDU/IP still unavailable on {iface} after "
                    f"{max_tries} waits of {wait_s:.0f}s; re-init UE"
                )
            )
            return

        self._pdu_retry_deadline_mono = now + wait_s
        self._set_traffic_stage(
            "waiting_ue_ip",
            status="Traffic: PDU retry",
            note=(
                f"PDU still down on {iface} "
                f"(failed {self._pdu_retry_count}/{max_tries}); "
                f"waiting another {wait_s:.0f}s before next check"
            ),
        )

    def _note_iperf_start_failure(self, exc: BaseException | str) -> None:
        """Count a failed iperf start toward PDU/iperf retries (no immediate UE restart)."""
        now = time.monotonic()
        max_tries = max(1, int(self.cfg.iperf.pdu_retry_max))
        wait_s = max(0.0, float(self.cfg.iperf.pdu_retry_wait_s))
        if wait_s <= 0.0:
            wait_s = 0.5
        self._pdu_seen = True
        self._pdu_retry_count += 1
        self._traffic_ip_lost_mono = now
        self._pdu_retry_deadline_mono = now + wait_s
        if self._pdu_retry_count >= max_tries:
            self._traffic_streamed = False
            self._pdu_seen = False
            self._clear_pdu_retry_state()
            self._restart_ue_for_traffic(
                reason=(
                    f"iperf start failed {max_tries} times "
                    f"({exc}); re-init UE"
                )
            )
            return
        self._set_traffic_stage(
            "waiting_ue_ip",
            status="Traffic: iperf start failed — retry",
            note=(
                f"iperf start failed ({self._pdu_retry_count}/{max_tries}): {exc}; "
                f"waiting {wait_s:.0f}s for PDU then retry"
            ),
        )

    def _ue_ip_after_settle(self, ip: str) -> str | None:
        """Return ``ip`` only after settle + optional ping verify before iperf.

        Shows the IP in the snapshot immediately. Holds iperf until:
          1) ``ip_settle_s`` of continuous live presence (may be 0)
          2) ``ip_ping_s`` ping of the UE IP succeeds (default 3s; 0 skips)
        """
        ip = (ip or "").strip()
        if not ip:
            self._clear_ue_ip_settle()
            return None
        settle = max(0.0, float(self.cfg.iperf.ip_settle_s))
        now = time.monotonic()
        if ip != self._settle_ue_ip:
            self._settle_ue_ip = ip
            self._settle_ue_ip_mono = now
            self._ping_verified_ip = ""
        with self._lock:
            self.snapshot.ue_ip = ip
        if settle > 0.0:
            waited = now - self._settle_ue_ip_mono
            if waited < settle:
                remaining = settle - waited
                self._set_traffic_stage(
                    "settling_ue_ip",
                    status="Traffic: settling UE IP",
                    note=(
                        f"PDU IP {ip} live on {self.cfg.iperf.ue_iface}; "
                        f"waiting {remaining:.1f}s before ping/iperf"
                    ),
                )
                return None

        if self._ping_verified_ip == ip:
            return ip

        ping_s = max(0.0, float(getattr(self.cfg.iperf, "ip_ping_s", 3.0)))
        if ping_s <= 0.0:
            self._ping_verified_ip = ip
            return ip

        if not self._traffic_still_desired():
            return None
        self._set_traffic_stage(
            "pinging_ue",
            status="Traffic: pinging UE",
            note=(
                f"Verifying link to {ip} for {ping_s:.0f}s "
                f"before iperf (iface {self.cfg.iperf.ue_iface})"
            ),
        )
        dn = (getattr(self.cfg.iperf, "ip_ping_dn_container", None) or "").strip()
        ok, detail = verify_ue_ping(
            ip,
            duration_s=ping_s,
            iface=self.cfg.iperf.ue_iface,
            docker_container=dn or None,
        )
        if not self._traffic_still_desired():
            return None
        if ok:
            self._ping_verified_ip = ip
            log.info("UE ping OK before iperf: %s", detail)
            return ip

        log.warning("UE ping failed before iperf: %s", detail)
        # Treat as unreachable PDU path — retry / eventually UE re-init
        self._clear_ue_ip_settle()
        self._pdu_seen = True
        self._handle_pdu_unavailable(
            iface=self.cfg.iperf.ue_iface,
            now=time.monotonic(),
            detail=f"ping failed: {detail}",
        )
        return None

    def _start_iperf_pair(self, ue_ip: str) -> None:
        if self.iperf is None:
            raise RuntimeError("iperf launcher not available")
        # Always (re)bind to the current UE IP — never keep a stale target.
        self._stop_iperf_pair(update_snapshot=True)
        # Fresh console on every (re)start, including UE IP changes
        self.iperf.clear_console()
        with self._lock:
            self.snapshot.iperf_console = ""
        self.iperf.start("server", ue_ip=ue_ip)
        with self._lock:
            self.snapshot.iperf_server_running = True
            self.snapshot.ue_ip = ue_ip
        if not self._traffic_sleep(max(0.2, float(self.cfg.iperf.client_delay_s))):
            self._stop_iperf_pair()
            return
        self.iperf.start("client", ue_ip=ue_ip)
        self._iperf_bound_ue_ip = ue_ip
        with self._lock:
            self.snapshot.iperf_client_running = True
            self.snapshot.ue_ip = ue_ip
            self.snapshot.iperf_server_running = bool(self.iperf.is_running("server"))
            self.snapshot.iperf_client_running = bool(self.iperf.is_running("client"))

    def _ensure_iperf_pair(self, ue_ip: str) -> None:
        """Start or repair iperf for ``ue_ip`` without needless full restarts."""
        if self.iperf is None:
            raise RuntimeError("iperf launcher not available")
        srv = bool(self.iperf.is_running("server"))
        cli = bool(self.iperf.is_running("client"))
        bound = self._iperf_bound_ue_ip
        if bound == ue_ip and srv and cli:
            return
        # Same IP, server already bound — only (re)start the client
        if bound == ue_ip and srv and not cli:
            self.iperf.start("client", ue_ip=ue_ip)
            with self._lock:
                self.snapshot.iperf_client_running = bool(self.iperf.is_running("client"))
                self.snapshot.ue_ip = ue_ip
            return
        self._start_iperf_pair(ue_ip)

    def _ue_process_running(self) -> bool:
        if self.launcher is not None and self.cfg.features.softmodem_control:
            return bool(self.launcher.is_running("ue"))
        with self._lock:
            return bool(self.snapshot.ue_running)

    def _gnb_process_running(self) -> bool:
        if self.launcher is not None and self.cfg.features.softmodem_control:
            return bool(self.launcher.is_running("gnb"))
        with self._lock:
            return bool(self.snapshot.gnb_running)

    def _ue_is_attached(self) -> bool:
        with self._lock:
            return bool(self.snapshot.ue_attached)

    def _ue_log_path(self) -> Path | None:
        if self.launcher is None:
            return None
        try:
            return self.launcher.stdout_path("ue")
        except Exception:
            return None

    def _gnb_log_path(self) -> Path | None:
        if self.launcher is None:
            return None
        try:
            return self.launcher.stdout_path("gnb")
        except Exception:
            return None

    def _poll_gnb_boot(self, *, process_running: bool) -> object:
        with self._boot_mon_lock:
            return self._gnb_boot.poll(
                process_running=process_running, log_path=self._gnb_log_path()
            )

    def _poll_ue_bringup(self, *, process_running: bool):
        with self._boot_mon_lock:
            return self._ue_bringup.poll(
                process_running=process_running, log_path=self._ue_log_path()
            )

    def _discover_traffic_ue_ip(self) -> str:
        iface = self.cfg.iperf.ue_iface
        ue_log = self._ue_log_path()
        ip = discover_ue_ipv4(iface, ue_log=ue_log) or ""
        if not ip and self.iperf is not None:
            ip = self.iperf.resolve_ue_ip(required=False, ue_log=ue_log) or ""
        return ip

    def _restart_ue_for_traffic(self, *, reason: str = "") -> None:
        """Stop+start UE for traffic recovery only while traffic is still desired.

        Stop traffic must never leave the UE down: if desired flips off mid-restart,
        we restore the UE and return without re-enabling iperf.
        """
        if not self._traffic_still_desired():
            return
        note = reason or "nr-uesoftmodem re-init for data traffic…"
        self._set_traffic_stage(
            "restarting_ue",
            status="Traffic: restarting UE",
            note=note,
        )
        self._stop_iperf_pair()
        self._ue_bringup.mark_process_down()
        ue_was_stopped = False
        if self.launcher is not None and self.cfg.features.softmodem_control:
            try:
                if self.launcher.is_running("ue"):
                    self.launcher.stop("ue")
                    ue_was_stopped = True
            except Exception as exc:
                log.warning("UE stop before traffic restart: %s", exc)
            if not self._traffic_still_desired():
                if ue_was_stopped:
                    # User hit Stop traffic during re-init — bring UE back, no iperf
                    try:
                        mode = self._current_mode()
                        self.launcher.start("ue", mode)
                        with self._lock:
                            self.snapshot.ue_running = True
                            self.snapshot.ue_attached = False
                            self.snapshot.ue_ip = ""
                        self._arm_ue_bringup_watch()
                        log.info("UE restored after Stop traffic cancelled a re-init")
                    except Exception as exc:
                        log.warning("UE restore after cancelled re-init failed: %s", exc)
                return
            mode = self._current_mode()
            self.launcher.start("ue", mode)
            with self._lock:
                self.snapshot.ue_running = True
                self.snapshot.ue_attached = False
                self.snapshot.ue_ip = ""
                self.snapshot.ue_bringup_stage = BringupStage.STARTING.value
                self.snapshot.ue_bringup_label = "starting"
        else:
            if not self._traffic_still_desired():
                return
            self._stub_start_ue()
        if not self._traffic_still_desired():
            return
        self._arm_ue_bringup_watch()

    def _publish_bringup(self, status) -> None:
        with self._lock:
            self.snapshot.ue_bringup_stage = status.stage.value
            self.snapshot.ue_bringup_label = getattr(status, "label", "") or status.stage.value

    def _publish_gnb_boot(self, status) -> None:
        with self._lock:
            self.snapshot.gnb_boot_stage = status.stage.value
            self.snapshot.gnb_boot_label = getattr(status, "label", "") or status.stage.value

    def _ensure_ue_ready_for_traffic(self) -> str | None:
        """Bring UE to a live TUN IP. Return UE IPv4, or None if not ready yet.

        When ``iperf.bringup_monitor`` is enabled, progress comes from UE log
        stages (SSB→RACH→RRC→PDU→TUN) and we restart only on process-down or a
        stalled stage. Iperf starts only after a live iface IP is confirmed.
        """
        if not self._gnb_process_running():
            self._stop_iperf_pair()
            self._set_traffic_stage(
                "waiting_gnb",
                status="Traffic: waiting for gNB",
                note="Start gNB before data traffic can run",
            )
            return None

        if bool(self.cfg.iperf.bringup_monitor):
            return self._ensure_ue_ready_via_bringup()
        return self._ensure_ue_ready_legacy()

    def _ensure_ue_ready_via_bringup(self) -> str | None:
        """Wait for live TUN IP; PDU-loss retries before any UE re-init.

        First Start: wait for UE IP (+ settle), then iperf (no stall→restart).
        After PDU was seen / iperf initiated: if PDU drops, debounce + up to
        ``pdu_retry_max`` waits for PDU/iperf before re-initing the UE.
        """
        cooldown = max(0.0, float(self.cfg.iperf.ue_restart_cooldown_s))
        now = time.monotonic()
        ue_run = self._ue_process_running()
        attached = self._ue_is_attached() if ue_run else False
        iface = self.cfg.iperf.ue_iface
        streamed = bool(self._traffic_streamed)

        if not ue_run:
            self._publish_bringup(self._ue_bringup.mark_process_down())
            self._clear_ue_ip_settle()
            # Process gone: always bring UE back (needed for first start and recovery)
            since = now - self._traffic_ue_start_mono if self._traffic_ue_start_mono else 1e9
            if since < cooldown:
                self._set_traffic_stage(
                    "restarting_ue",
                    status="Traffic: restarting UE",
                    note=f"UE process down; retry in {cooldown - since:.1f}s",
                )
                return None
            self._traffic_streamed = False
            self._pdu_seen = False
            self._clear_pdu_retry_state()
            self._restart_ue_for_traffic(reason="UE process down; re-init")
            return None

        status = self._poll_ue_bringup(process_running=True)

        if attached and status.stage in _UE_PRE_ATTACH:
            with self._boot_mon_lock:
                self._ue_bringup.force_at_least(
                    BringupStage.WAITING_PDU,
                    "Radio attached (gNB MAC) — waiting for PDU / TUN IP",
                )
                status = self._ue_bringup.status()

        self._publish_bringup(status)

        ip = self._discover_traffic_ue_ip()
        if ip:
            self._pdu_seen = True
            self._traffic_ip_lost_mono = 0.0
            self._pdu_retry_deadline_mono = 0.0
            with self._lock:
                self.snapshot.ue_bringup_stage = BringupStage.READY.value
                self.snapshot.ue_bringup_label = "PDU IP ready"
            self._traffic_ip_wait_mono = 0.0
            return self._ue_ip_after_settle(ip)

        # No live IP
        self._clear_ue_ip_settle()

        # After PDU was once up / stream ran / radio attached: wait then UE re-init
        if self._pdu_seen or streamed or attached:
            self._handle_pdu_unavailable(
                iface=iface,
                now=now,
                detail=status.detail,
            )
            return None

        # --- Early bring-up (not yet radio-attached / no PDU seen): wait only ---
        if self._traffic_ip_wait_mono <= 0.0:
            self._traffic_ip_wait_mono = now
        waited = int(now - self._traffic_ip_wait_mono)
        detail = status.detail or "waiting for live PDU / TUN IP"
        if status.stalled:
            detail = (
                f"{detail} (stage slow — still waiting; no UE restart until after attach)"
            )
        if status.stage in _UE_PRE_ATTACH:
            traffic_stage = "waiting_attach"
        else:
            traffic_stage = "waiting_ue_ip"
        self._set_traffic_stage(
            traffic_stage,
            status="Traffic: waiting for UE IP",
            note=f"{detail} (waited {waited}s on {iface})",
        )
        return None

    def _ensure_ue_ready_legacy(self) -> str | None:
        """Blind attach timer + optional TUN IP wait (bringup_monitor=false)."""
        ue_run = self._ue_process_running()
        attached = self._ue_is_attached() if ue_run else False
        boot_s = max(1.0, float(self.cfg.iperf.ue_boot_s))
        cooldown = max(0.0, float(self.cfg.iperf.ue_restart_cooldown_s))
        now = time.monotonic()

        if not ue_run:
            since = now - self._traffic_ue_start_mono if self._traffic_ue_start_mono else 1e9
            if since < cooldown:
                self._set_traffic_stage(
                    "restarting_ue",
                    status="Traffic: restarting UE",
                    note=f"UE process down; retry in {cooldown - since:.1f}s",
                )
                return None
            self._restart_ue_for_traffic()
            return None

        if not attached:
            self._traffic_ip_wait_mono = 0.0
            waited = now - self._traffic_ue_start_mono if self._traffic_ue_start_mono else boot_s
            if self._traffic_ue_start_mono <= 0.0:
                self._traffic_ue_start_mono = now
                waited = 0.0
            remaining = max(0.0, boot_s - waited)
            if waited < boot_s:
                self._set_traffic_stage(
                    "waiting_attach",
                    status="Traffic: waiting for UE attach",
                    note=f"UE running; waiting for Attached=YES ({remaining:.0f}s left of {boot_s:.0f}s)",
                )
                return None
            self._restart_ue_for_traffic()
            return None

        iface = self.cfg.iperf.ue_iface
        ip = self._discover_traffic_ue_ip()
        if not ip:
            self._clear_ue_ip_settle()
            # Attached but no IP: same wait/retry → UE re-init path
            self._handle_pdu_unavailable(iface=iface, now=now)
            return None

        self._pdu_seen = True
        self._traffic_ip_wait_mono = 0.0
        self._traffic_ip_lost_mono = 0.0
        self._pdu_retry_deadline_mono = 0.0
        return self._ue_ip_after_settle(ip)

    def _traffic_supervise_once(self) -> None:
        ue_ip = self._ensure_ue_ready_for_traffic()
        if ue_ip is None:
            # Stale iperf against an old IP / dead UE must not keep running
            if self.iperf and (
                self.iperf.is_running("server") or self.iperf.is_running("client")
            ):
                self._stop_iperf_pair()
            return

        srv = bool(self.iperf and self.iperf.is_running("server"))
        cli = bool(self.iperf and self.iperf.is_running("client"))
        bound = self._iperf_bound_ue_ip
        need_start = (not srv) or (not cli) or (bound and bound != ue_ip)
        if need_start:
            self._set_traffic_stage(
                "starting_iperf",
                status="Traffic: starting iperf",
                note=f"UE IP {ue_ip} live on {self.cfg.iperf.ue_iface}; launching server→client",
            )
            try:
                self._ensure_iperf_pair(ue_ip)
            except Exception as exc:
                log.exception("iperf pair start failed")
                self._stop_iperf_pair()
                self._note_iperf_start_failure(exc)
                return
            if not self._traffic_still_desired():
                self._stop_iperf_pair()
                return

        srv = bool(self.iperf and self.iperf.is_running("server"))
        cli = bool(self.iperf and self.iperf.is_running("client"))
        with self._lock:
            self.snapshot.iperf_server_running = srv
            self.snapshot.iperf_client_running = cli
            self.snapshot.ue_ip = ue_ip
        if srv and cli:
            self._traffic_streamed = True
            self._pdu_seen = True
            self._clear_pdu_retry_state()
            self._set_traffic_stage(
                "running",
                status="Traffic: running",
                note=f"DN → UE ({ue_ip}) UDP; supervising UE IP reachability",
            )
        else:
            self._set_traffic_stage(
                "starting_iperf",
                status="Traffic: iperf not fully up",
                note=f"server={'up' if srv else 'down'} client={'up' if cli else 'down'}; will retry",
            )

    def _traffic_supervise_loop(self) -> None:
        log.info("Data-traffic supervisor started")
        period = max(0.25, float(self.cfg.iperf.supervise_period_s))
        ip_wait = max(0.5, float(self.cfg.iperf.ip_wait_s)) if not self.cfg.iperf.bringup_monitor else 0.5
        while not self._stop.is_set():
            if not self._traffic_desired:
                time.sleep(0.25)
                continue
            try:
                self._traffic_supervise_once()
            except Exception as exc:  # pragma: no cover
                log.exception("traffic supervisor error: %s", exc)
                self._set_traffic_stage(
                    "error",
                    status="Traffic: supervisor error",
                    note=str(exc),
                )
            # While waiting for attach / IP / bring-up stages, poll a bit faster
            with self._lock:
                stage = self.snapshot.traffic_stage
            sleep_s = (
                min(period, 0.5)
                if stage in (
                    "waiting_ue_ip",
                    "waiting_attach",
                    "ensuring_ue",
                    "settling_ue_ip",
                    "pinging_ue",
                )
                else period
            )
            if stage == "waiting_ue_ip" and not self.cfg.iperf.bringup_monitor:
                sleep_s = min(sleep_s, max(0.25, ip_wait / 10.0))
            if stage == "settling_ue_ip":
                sleep_s = min(sleep_s, 0.25)
            if not self._traffic_sleep(sleep_s):
                continue
        log.info("Data-traffic supervisor exiting")

    def _ensure_traffic_thread(self) -> None:
        t = self._traffic_thread
        if t is not None and t.is_alive():
            return
        self._traffic_thread = threading.Thread(
            target=self._traffic_supervise_loop,
            name="iperf-traffic-supervise",
            daemon=True,
        )
        self._traffic_thread.start()

    def start_iperf_server(self) -> None:
        if not self.cfg.features.iperf_control or self.iperf is None:
            with self._lock:
                self.snapshot.status = "iperf control disabled"
                self.snapshot.note = "Enable features.iperf_control in lab config"
            return
        try:
            self.iperf.start("server")
            with self._lock:
                self.snapshot.iperf_server_running = True
                self.snapshot.ue_ip = getattr(self.iperf, "_last_ue_ip", "") or self.snapshot.ue_ip
                self.snapshot.status = "iperf server started"
                self.snapshot.note = (
                    f"UDP server listening on {self.snapshot.ue_ip or 'UE IP'}"
                )
        except Exception as exc:
            log.exception("iperf server start failed")
            with self._lock:
                self.snapshot.iperf_server_running = False
                self.snapshot.status = "iperf server start failed"
                self.snapshot.note = str(exc)

    def stop_iperf_server(self) -> None:
        if self.iperf is None:
            return
        try:
            self.iperf.stop("server")
        except Exception as exc:
            log.exception("iperf server stop failed")
            with self._lock:
                self.snapshot.status = "iperf server stop error"
                self.snapshot.note = str(exc)
            return
        with self._lock:
            self.snapshot.iperf_server_running = False
            self.snapshot.status = "iperf server stopped"

    def start_iperf_client(self) -> None:
        if not self.cfg.features.iperf_control or self.iperf is None:
            with self._lock:
                self.snapshot.status = "iperf control disabled"
                self.snapshot.note = "Enable features.iperf_control in lab config"
            return
        try:
            self.iperf.start("client")
            with self._lock:
                self.snapshot.iperf_client_running = True
                self.snapshot.ue_ip = getattr(self.iperf, "_last_ue_ip", "") or self.snapshot.ue_ip
                self.snapshot.status = "iperf client started"
                self.snapshot.note = (
                    f"DN → UE ({self.snapshot.ue_ip or '?'}) UDP traffic running"
                )
        except Exception as exc:
            log.exception("iperf client start failed")
            with self._lock:
                self.snapshot.iperf_client_running = False
                self.snapshot.status = "iperf client start failed"
                self.snapshot.note = str(exc)

    def stop_iperf_client(self) -> None:
        if self.iperf is None:
            return
        try:
            self.iperf.stop("client")
        except Exception as exc:
            log.exception("iperf client stop failed")
            with self._lock:
                self.snapshot.status = "iperf client stop error"
                self.snapshot.note = str(exc)
            return
        with self._lock:
            self.snapshot.iperf_client_running = False
            self.snapshot.status = "iperf client stopped"

    def start_iperf_traffic(self) -> None:
        """Desired-on: wait for UE IP, start iperf; recover UE only if IP drops later."""
        if not self.cfg.features.iperf_control or self.iperf is None:
            with self._lock:
                self.snapshot.status = "iperf control disabled"
            return
        with self._traffic_lock:
            self._traffic_desired = True
        self._reset_traffic_session_flags()
        with self._lock:
            self.snapshot.traffic_desired = True
            self.snapshot.traffic_stage = "waiting_ue_ip"
            self.snapshot.status = "Traffic: desired ON"
            self.snapshot.note = (
                "Waiting for live UE IP, then iperf — "
                "PDU loss retries before any UE re-init"
            )
        self._ensure_traffic_thread()
        log.info("Data traffic desired ON")

    def stop_iperf_traffic(self) -> None:
        """Desired-off: stop iperf only — never stop/restart gNB or UE.

        Softmodems stay as they are; only Data traffic window state (desired flag,
        server/client, console, held throughput) is cleared.
        """
        with self._traffic_lock:
            self._traffic_desired = False
        self._reset_traffic_session_flags()
        self._set_traffic_stage(
            "stopping",
            status="Traffic: stopping",
            note="Stopping iperf server and client (UE left running)…",
        )
        self._stop_iperf_pair(update_snapshot=True)
        self._iperf_bound_ue_ip = ""
        self._clear_ue_ip_settle()
        # Drop held MAC throughput so KPIs do not look like UDP is still flowing
        if self.ingest is not None:
            try:
                self.ingest.clear_throughput_hold()
            except Exception:  # pragma: no cover
                pass
        if self.iperf is not None:
            self.iperf.clear_console()
        with self._lock:
            self.snapshot.traffic_desired = False
            self.snapshot.traffic_stage = "idle"
            self.snapshot.iperf_console = ""
            self.snapshot.throughput_mbps = 0.0
            self.snapshot.status = "Traffic: stopped"
            self.snapshot.note = (
                "iperf stopped; UE/gNB unchanged — Start traffic to resume load"
            )
        log.info("Data traffic desired OFF (UE left running)")

    def clear_iperf_console(self) -> None:
        if self.iperf is not None:
            self.iperf.clear_console()
        with self._lock:
            self.snapshot.iperf_console = ""
            self.snapshot.note = "iperf console cleared"

    def check_iperf_status(self) -> None:
        if self.iperf is None:
            with self._lock:
                self.snapshot.status = "iperf control disabled"
            return
        srv = self.iperf.is_running("server")
        cli = self.iperf.is_running("client")
        ue_ip = self.iperf.resolve_ue_ip(required=False)
        with self._lock:
            self.snapshot.iperf_server_running = srv
            self.snapshot.iperf_client_running = cli
            if ue_ip:
                self.snapshot.ue_ip = ue_ip
            desired = "ON" if self.snapshot.traffic_desired else "OFF"
            stage = self.snapshot.traffic_stage or "idle"
            self.snapshot.status = (
                f"traffic={desired}/{stage}; "
                f"server={'RUNNING' if srv else 'stopped'}, "
                f"client={'RUNNING' if cli else 'stopped'}"
            )
            self.snapshot.note = (
                f"UE IP={self.snapshot.ue_ip or 'not found on ' + self.cfg.iperf.ue_iface}"
            )
        log.info(
            "iperf status desired=%s stage=%s server=%s client=%s ue_ip=%s",
            desired,
            stage,
            srv,
            cli,
            ue_ip or "-",
        )

    # --- stubs (offline) -----------------------------------------------------

    def _stub_start_gnb(self) -> None:
        with self._lock:
            self.snapshot.gnb_running = True
            self.snapshot.ue_attached = bool(self.snapshot.ue_running)
            if self.snapshot.ue_attached:
                self.snapshot.status = "Attached — simulated metrics streaming"
                self.snapshot.note = "gNB and UE running (stub)"
            else:
                self.snapshot.status = "gNB started (stub)"
                self.snapshot.note = "Start UE to attach and begin telemetry"

    def _stub_stop_gnb(self) -> None:
        with self._lock:
            self.snapshot.gnb_running = False
            self.snapshot.ue_attached = False
            self.snapshot.status = "gNB stopped (stub)"
            self.snapshot.note = "Telemetry paused until gNB and UE are both running"

    def _stub_start_ue(self) -> None:
        with self._lock:
            self.snapshot.ue_running = True
            self.snapshot.ue_attached = bool(self.snapshot.gnb_running)
            if self.snapshot.ue_attached:
                self.snapshot.status = "Attached — simulated metrics streaming"
                self.snapshot.note = "gNB and UE running (stub)"
            else:
                self.snapshot.status = "UE started (stub)"
                self.snapshot.note = "Start gNB to attach and begin telemetry"

    def _stub_stop_ue(self) -> None:
        with self._lock:
            self.snapshot.ue_running = False
            self.snapshot.ue_attached = False
            self.snapshot.status = "UE stopped (stub)"
            self.snapshot.note = "Telemetry paused until gNB and UE are both running"

    def _current_mode(self) -> str:
        with self._lock:
            return self.snapshot.csi_mode

    def _relaunch_mode_worker(self, mode: str) -> None:
        if self.launcher is None:
            return
        if not self._mode_lock.acquire(blocking=False):
            log.warning("Mode relaunch already in progress")
            return
        try:
            gnb_was = self.launcher.is_running("gnb")
            ue_was = self.launcher.is_running("ue")
            relaunch_for_mode(
                mode=mode,
                gnb_was_running=gnb_was,
                ue_was_running=ue_was,
                stop_ue=lambda: self.launcher.stop("ue"),
                stop_gnb=lambda: self.launcher.stop("gnb"),
                start_gnb=lambda m: self.launcher.start("gnb", m),
                start_ue=lambda m: self.launcher.start("ue", m),
                settle_s=self.cfg.oai.mode_settle_s,
                gnb_boot_s=self.cfg.oai.gnb_boot_s,
            )
            with self._lock:
                self.snapshot.gnb_running = self.launcher.is_running("gnb")
                self.snapshot.ue_running = self.launcher.is_running("ue")
                self.snapshot.mode_pending = False
                self.snapshot.status = f"Mode {mode} applied (relaunched)"
                self.snapshot.note = "Softmodems restarted with new AI-FB flags"
        except Exception as exc:
            log.exception("Mode relaunch failed")
            with self._lock:
                self.snapshot.mode_pending = False
                self.snapshot.status = "Mode relaunch failed"
                self.snapshot.note = str(exc)
        finally:
            self._mode_lock.release()

    # --- lab metrics loop ----------------------------------------------------

    def _lab_loop(self) -> None:
        period = max(0.05, float(self.cfg.csi.csi_period_s))
        max_pts = max(32, int(self.cfg.plot.max_points))
        while not self._stop.wait(period):
            now = time.monotonic()
            t_rel = now - self._t0

            gnb_run = bool(self.launcher and self.launcher.is_running("gnb"))
            ue_run = bool(self.launcher and self.launcher.is_running("ue"))
            iperf_srv = bool(self.iperf and self.iperf.is_running("server"))
            iperf_cli = bool(self.iperf and self.iperf.is_running("client"))
            ue_ip = ""
            if self.cfg.features.iperf_control:
                ue_log = None
                if self.launcher is not None:
                    try:
                        ue_log = self.launcher.stdout_path("ue")
                    except Exception:
                        ue_log = None
                ue_ip = discover_ue_ipv4(self.cfg.iperf.ue_iface, ue_log=ue_log) or ""

            # Softmodem boot stages (always — even before radio attach / PDU IP)
            gnb_boot = self._poll_gnb_boot(process_running=gnb_run)
            ue_boot = None
            if bool(self.cfg.iperf.bringup_monitor) or self.cfg.features.softmodem_control:
                ue_boot = self._poll_ue_bringup(process_running=ue_run)

            sample = None
            if gnb_run and self.ingest is not None:
                sample = self.ingest.poll()

            attached = bool(sample.attached) if sample is not None else False
            # Require both processes for "link up" demo semantics
            link_up = gnb_run and ue_run and attached

            with self._lock:
                mode = self.snapshot.csi_mode
                view_csirs = bool(self.snapshot.csirs_view_enabled)
                self.snapshot.gnb_running = gnb_run
                self.snapshot.ue_running = ue_run
                self.snapshot.iperf_server_running = iperf_srv
                self.snapshot.iperf_client_running = iperf_cli
                self.snapshot.gnb_boot_stage = gnb_boot.stage.value
                self.snapshot.gnb_boot_label = gnb_boot.label
                if ue_boot is not None:
                    self.snapshot.ue_bringup_stage = ue_boot.stage.value
                    self.snapshot.ue_bringup_label = ue_boot.label
                if ue_ip:
                    self.snapshot.ue_ip = ue_ip
                    if ue_boot is None or ue_boot.stage != BringupStage.READY:
                        self.snapshot.ue_bringup_stage = BringupStage.READY.value
                        self.snapshot.ue_bringup_label = "PDU IP ready"
                elif not ue_run:
                    self.snapshot.ue_ip = ""
                self.snapshot.ue_attached = attached if (gnb_run and ue_run) else False
                if not link_up:
                    self.snapshot.last_csi_age_s = now - self._last_csi_t

            # CSI-RS H dumps: plot when enabled; always trim old bins
            self._poll_csirs_record(view_enabled=view_csirs)

            with self._lock:
                if not link_up:
                    continue

                if sample is None:
                    continue

                self._last_csi_t = now
                self.snapshot.ri = sample.ri
                self.snapshot.pmi_x1 = sample.pmi_x1
                self.snapshot.pmi_x2 = sample.pmi_x2
                self.snapshot.cqi = sample.cqi
                self.snapshot.bler = sample.bler

                # Throughput: when Data traffic is off, show idle immediately.
                # Residual MAC TX / last-good hold made Stop traffic look "live" for seconds.
                traffic_load = bool(self._traffic_desired) or iperf_srv or iperf_cli
                if traffic_load:
                    if sample.throughput_fresh and sample.throughput_mbps == sample.throughput_mbps:
                        self.snapshot.throughput_mbps = sample.throughput_mbps
                    elif (
                        sample.throughput_mbps == sample.throughput_mbps
                        and self.snapshot.throughput_mbps != self.snapshot.throughput_mbps
                    ):
                        self.snapshot.throughput_mbps = sample.throughput_mbps
                    tput_point = (
                        sample.throughput_mbps
                        if sample.throughput_mbps == sample.throughput_mbps
                        else float("nan")
                    )
                else:
                    self.snapshot.throughput_mbps = 0.0
                    tput_point = 0.0

                self.snapshot.last_csi_age_s = 0.0
                self.snapshot.t_rel_series.append(t_rel)
                self.snapshot.ri_series.append(float(sample.ri + 1))
                self.snapshot.cqi_series.append(float(sample.cqi))
                self.snapshot.pmi_x1_series.append(float(sample.pmi_x1))
                self.snapshot.pmi_x2_series.append(float(sample.pmi_x2))
                self.snapshot.bler_series.append(
                    sample.bler if sample.bler == sample.bler else float("nan")
                )
                self.snapshot.tput_series.append(tput_point)

                dist = (
                    self.snapshot.dist_csinet
                    if mode == MODE_CSINET
                    else self.snapshot.dist_legacy
                )
                # CDF / means: only when BLER is valid and tput is a fresh measurement
                # under active data traffic (idle zeros must not bias the distribution),
                # and not while a live mode switch is still in flight
                if (
                    traffic_load
                    and not self.snapshot.mode_pending
                    and sample.bler == sample.bler
                    and sample.throughput_fresh
                    and sample.throughput_mbps == sample.throughput_mbps
                ):
                    dist.add_sample(
                        t_rel=t_rel,
                        bler=sample.bler,
                        tput=sample.throughput_mbps,
                        cqi=float(sample.cqi),
                        max_samples=self.cfg.plot.dist_max_samples,
                        cdf_points=self.cfg.plot.dist_cdf_points,
                        max_avg_points=self.cfg.plot.dist_avg_max_points,
                    )

                for attr in (
                    "t_rel_series",
                    "ri_series",
                    "cqi_series",
                    "pmi_x1_series",
                    "pmi_x2_series",
                    "bler_series",
                    "tput_series",
                ):
                    series = getattr(self.snapshot, attr)
                    if len(series) > max_pts:
                        setattr(self.snapshot, attr, series[-max_pts:])

                row = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "t_rel_s": f"{t_rel:.3f}",
                    "csi_mode": mode,
                    "ri": sample.ri,
                    "pmi_x1": sample.pmi_x1,
                    "pmi_x2": sample.pmi_x2,
                    "cqi": sample.cqi,
                    "bler": f"{sample.bler:.6f}" if sample.bler == sample.bler else "",
                    "throughput_mbps": (
                        f"{sample.throughput_mbps:.3f}"
                        if sample.throughput_fresh
                        and sample.throughput_mbps == sample.throughput_mbps
                        else ""
                    ),
                    "ue_attached": int(attached),
                }

            if self.logger and link_up and sample is not None:
                self.logger.write(row)

    # --- simulator -----------------------------------------------------------

    def _simulate_csirs_links(self, t_rel: float, mode: str) -> tuple[list[float], list[tuple[str, list[float]]]]:
        n_rx = max(1, int(self.cfg.csi.n_rx))
        n_ports = max(1, int(self.cfg.csi.n_ports))
        n_subc = max(8, int(self.cfg.csi.n_subc))
        subc = [float(i) for i in range(n_subc)]
        links: list[tuple[str, list[float]]] = []
        base = 1.05 if mode == MODE_CSINET else 0.85
        for rx in range(n_rx):
            for port in range(n_ports):
                mag: list[float] = []
                for k in range(n_subc):
                    fade = base * (
                        0.55
                        + 0.45
                        * math.cos(0.09 * k + 0.7 * port + 0.4 * rx + t_rel * 0.4)
                    )
                    fade += 0.05 * random.gauss(0.0, 1.0)
                    mag.append(max(0.0, fade))
                links.append((f"RX{rx}-TX{port}", mag))
        return subc, links

    def _simulate_loop(self) -> None:
        period = max(0.01, float(self.cfg.csi.csi_period_s))
        max_pts = max(32, int(self.cfg.plot.max_points))
        while not self._stop.wait(period):
            now = time.monotonic()
            t_rel = now - self._t0
            with self._lock:
                mode = self.snapshot.csi_mode
                attached = bool(self.snapshot.gnb_running and self.snapshot.ue_running)
                self.snapshot.ue_attached = attached

            if not attached:
                with self._lock:
                    self.snapshot.last_csi_age_s = now - self._last_csi_t
                continue

            if mode == MODE_CSINET:
                ri = 0
                cqi = random.randint(10, 14)
                bler = max(0.0, min(1.0, random.gauss(0.02, 0.01)))
                tput = max(0.0, random.gauss(28.0, 2.5))
                pmi_x1 = random.randint(0, 63)
                pmi_x2 = 0
            else:
                ri = random.choice([0, 1])
                cqi = random.randint(6, 11)
                bler = max(0.0, min(1.0, random.gauss(0.08, 0.03)))
                tput = max(0.0, random.gauss(18.0, 3.0))
                pmi_x1 = random.randint(0, 63)
                pmi_x2 = random.randint(0, 3)

            tput += 1.5 * math.sin(t_rel / 3.0)
            bler = max(0.0, min(1.0, bler))

            csirs_subc: list[float] = []
            csirs_links: list[tuple[str, list[float]]] = []
            if self.cfg.features.simulate_csirs:
                csirs_subc, csirs_links = self._simulate_csirs_links(t_rel, mode)

            row = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "t_rel_s": f"{t_rel:.3f}",
                "csi_mode": mode,
                "ri": ri,
                "pmi_x1": pmi_x1,
                "pmi_x2": pmi_x2,
                "cqi": cqi,
                "bler": f"{bler:.6f}",
                "throughput_mbps": f"{tput:.3f}",
                "ue_attached": int(attached),
            }

            with self._lock:
                self._last_csi_t = now
                self.snapshot.mode_pending = False
                self.snapshot.ri = ri
                self.snapshot.pmi_x1 = pmi_x1
                self.snapshot.pmi_x2 = pmi_x2
                self.snapshot.cqi = cqi
                self.snapshot.bler = bler
                self.snapshot.throughput_mbps = tput
                self.snapshot.last_csi_age_s = 0.0
                self.snapshot.ue_attached = attached
                self.snapshot.t_rel_series.append(t_rel)
                self.snapshot.ri_series.append(float(ri + 1))
                self.snapshot.cqi_series.append(float(cqi))
                self.snapshot.pmi_x1_series.append(float(pmi_x1))
                self.snapshot.pmi_x2_series.append(float(pmi_x2))
                self.snapshot.bler_series.append(bler)
                self.snapshot.tput_series.append(tput)
                if csirs_links:
                    self.snapshot.csirs_subc = csirs_subc
                    self.snapshot.csirs_links = csirs_links
                    self.snapshot.csirs_frame = int(t_rel * 100) % 1024
                    self.snapshot.csirs_slot = int(t_rel * 20) % 20
                dist = (
                    self.snapshot.dist_csinet
                    if mode == MODE_CSINET
                    else self.snapshot.dist_legacy
                )
                dist.add_sample(
                    t_rel=t_rel,
                    bler=bler,
                    tput=tput,
                    cqi=float(cqi),
                    max_samples=self.cfg.plot.dist_max_samples,
                    cdf_points=self.cfg.plot.dist_cdf_points,
                    max_avg_points=self.cfg.plot.dist_avg_max_points,
                )
                for attr in (
                    "t_rel_series",
                    "ri_series",
                    "cqi_series",
                    "pmi_x1_series",
                    "pmi_x2_series",
                    "bler_series",
                    "tput_series",
                ):
                    series = getattr(self.snapshot, attr)
                    if len(series) > max_pts:
                        setattr(self.snapshot, attr, series[-max_pts:])

            if self.logger:
                self.logger.write(row)
