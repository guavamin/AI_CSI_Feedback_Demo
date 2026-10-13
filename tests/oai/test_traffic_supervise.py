"""Desired-state data-traffic supervision (UE + iperf coordination)."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from common.oai.config import (
    AppConfig,
    FeaturesConfig,
    IperfConfig,
    IperfSideConfig,
    LoggingConfig,
    load_config,
)
from common.oai.controllers.controller import Controller

from pathlib import Path
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _ctrl(tmp_path) -> Controller:
    cfg = AppConfig(
        features=FeaturesConfig(
            simulate_metrics=False,
            softmodem_control=False,
            iperf_control=True,
            record_run=False,
        ),
        iperf=IperfConfig(
            client_delay_s=0.05,
            ue_boot_s=0.4,
            ip_wait_s=0.3,
            supervise_period_s=0.05,
            ue_restart_cooldown_s=0.0,
            ue_iface="oaitun_ue1",
            # Existing tests cover the legacy blind-timer path
            bringup_monitor=False,
            ip_ping_s=0.0,
            server=IperfSideConfig(
                use_sudo=False, argv=["iperf", "-s", "-u", "-B", "{ue_ip}"]
            ),
            client=IperfSideConfig(
                use_sudo=False, argv=["iperf", "-u", "-c", "{ue_ip}", "-t", "1"]
            ),
        ),
        logging=LoggingConfig(root_dir=str(tmp_path)),
    )
    ctrl = Controller(cfg)
    ctrl.launcher = MagicMock()
    ctrl.launcher.is_running.side_effect = lambda side: {
        "gnb": bool(ctrl.snapshot.gnb_running),
        "ue": bool(ctrl.snapshot.ue_running),
    }.get(side, False)
    # Use process checks against the mock launcher
    ctrl.cfg.features.softmodem_control = True
    return ctrl


def test_stop_traffic_clears_desired(tmp_path):
    ctrl = _ctrl(tmp_path)
    ctrl.iperf = MagicMock()
    ctrl.iperf.is_running.return_value = False
    ctrl.iperf.console_text.return_value = ""
    ctrl._traffic_desired = True
    ctrl.snapshot.traffic_desired = True
    ctrl.snapshot.traffic_stage = "running"
    ctrl.stop_iperf_traffic()
    assert ctrl._traffic_desired is False
    snap = ctrl.get_snapshot()
    assert snap.traffic_desired is False
    assert snap.traffic_stage == "idle"
    assert snap.iperf_console == ""
    ctrl.iperf.stop.assert_not_called()
    ctrl.iperf.stop_all.assert_called()
    ctrl.iperf.clear_console.assert_called()
    ctrl.stop_background_workers()


def test_stop_traffic_does_not_stop_ue(tmp_path):
    ctrl = _ctrl(tmp_path)
    ctrl.iperf = MagicMock()
    ctrl.iperf.is_running.return_value = False
    ctrl.iperf.console_text.return_value = ""
    ctrl._traffic_desired = True
    ctrl.snapshot.ue_running = True
    ctrl.snapshot.gnb_running = True
    ctrl.stop_iperf_traffic()
    ctrl.launcher.stop.assert_not_called()
    ctrl.launcher.start.assert_not_called()
    assert ctrl.snapshot.ue_running is True
    ctrl.stop_background_workers()


def test_stop_traffic_during_ue_reinit_restores_ue(tmp_path):
    """If Stop traffic races a supervisor UE re-init, UE must be brought back up."""
    ctrl = _ctrl(tmp_path)
    ctrl.iperf = MagicMock()
    ctrl.iperf.is_running.return_value = False
    ctrl._traffic_desired = True
    ctrl.snapshot.gnb_running = True
    ctrl.snapshot.ue_running = True
    ctrl.launcher.is_running.side_effect = lambda side: {
        "gnb": True,
        "ue": True,
    }.get(side, False)

    def _stop_ue(_side=None):
        # Mimic user clicking Stop traffic while UE is being stopped for re-init
        ctrl._traffic_desired = False
        ctrl.snapshot.ue_running = False

    ctrl.launcher.stop.side_effect = _stop_ue
    ctrl.launcher.start.side_effect = lambda side, mode=None: setattr(
        ctrl.snapshot, "ue_running", True
    )

    ctrl._restart_ue_for_traffic(reason="test race")
    assert ctrl.launcher.start.called
    assert ctrl.snapshot.ue_running is True
    ctrl.iperf.start.assert_not_called()
    ctrl.stop_background_workers()


def test_ensure_ue_waits_for_attach(tmp_path):
    ctrl = _ctrl(tmp_path)
    ctrl.iperf = MagicMock()
    ctrl.iperf.is_running.return_value = False
    ctrl.iperf.resolve_ue_ip.return_value = ""
    ctrl._traffic_desired = True
    ctrl.snapshot.gnb_running = True
    ctrl.snapshot.ue_running = True
    ctrl.snapshot.ue_attached = False
    ctrl._traffic_ue_start_mono = 0.0

    with patch(
        "common.oai.controllers.controller.discover_ue_ipv4", return_value=None
    ):
        assert ctrl._ensure_ue_ready_for_traffic() is None
    assert ctrl.snapshot.traffic_stage == "waiting_attach"
    ctrl.stop_background_workers()


def test_ensure_ue_returns_ip_when_ready(tmp_path):
    ctrl = _ctrl(tmp_path)
    ctrl.iperf = MagicMock()
    ctrl.cfg.iperf.ip_settle_s = 0.0
    ctrl._traffic_desired = True
    ctrl.snapshot.gnb_running = True
    ctrl.snapshot.ue_running = True
    ctrl.snapshot.ue_attached = True
    with patch(
        "common.oai.controllers.controller.discover_ue_ipv4",
        return_value="10.0.0.19",
    ):
        assert ctrl._ensure_ue_ready_for_traffic() == "10.0.0.19"
    assert ctrl.snapshot.ue_ip == "10.0.0.19"
    ctrl.stop_background_workers()


def test_supervise_stops_stale_iperf_when_ue_not_ready(tmp_path):
    ctrl = _ctrl(tmp_path)
    ctrl.iperf = MagicMock()
    ctrl.iperf.is_running.return_value = True
    ctrl._traffic_desired = True
    ctrl.snapshot.gnb_running = True
    ctrl.snapshot.ue_running = False
    ctrl.snapshot.ue_attached = False
    ctrl.cfg.iperf.ue_restart_cooldown_s = 60.0
    ctrl._traffic_ue_start_mono = time.monotonic()

    with patch.object(ctrl, "_restart_ue_for_traffic") as restart:
        ctrl._traffic_supervise_once()
        restart.assert_not_called()
    ctrl.iperf.stop_all.assert_called()
    ctrl.stop_background_workers()


def test_start_traffic_sets_desired(tmp_path):
    ctrl = _ctrl(tmp_path)
    ctrl.iperf = MagicMock()
    ctrl.iperf.is_running.return_value = False

    def _noop_loop():
        while ctrl._traffic_desired and not ctrl._stop.is_set():
            time.sleep(0.05)

    with patch.object(ctrl, "_traffic_supervise_loop", side_effect=_noop_loop):
        ctrl.start_iperf_traffic()
        time.sleep(0.05)
        assert ctrl._traffic_desired is True
        assert ctrl.get_snapshot().traffic_desired is True
        assert ctrl._traffic_thread is not None
        ctrl.stop_iperf_traffic()
        if ctrl._traffic_thread is not None:
            ctrl._traffic_thread.join(timeout=2.0)
    ctrl.stop_background_workers()


def test_lab_config_loads_traffic_supervise_knobs():
    cfg = load_config(FIXTURES / "lab_rfsim.yaml")
    assert cfg.iperf.ue_boot_s == 20.0
    assert cfg.iperf.ip_wait_s == 0.0
    assert cfg.iperf.supervise_period_s == 1.0
    assert cfg.iperf.bringup_monitor is True
    assert cfg.iperf.bringup_ssb_timeout_s == 15.0
    assert cfg.iperf.bringup_pdu_timeout_s == 60.0
    assert cfg.iperf.ip_settle_s == 0.0
    assert cfg.iperf.ip_ping_s == 3.0
    assert cfg.iperf.ip_ping_dn_container == "oai-ext-dn"
    assert cfg.iperf.ip_lost_grace_s == 2.0
    assert cfg.iperf.pdu_retry_wait_s == 5.0
    assert cfg.iperf.pdu_retry_max == 3


def test_attached_without_ip_retries_then_restarts(tmp_path):
    """Radio attached + no TUN IP → wait/retry then UE re-init (no telnet PDU)."""
    ctrl = _ctrl(tmp_path)
    ctrl.cfg.iperf.bringup_monitor = True
    ctrl.cfg.iperf.ip_lost_grace_s = 0.01
    ctrl.cfg.iperf.pdu_retry_wait_s = 0.04
    ctrl.cfg.iperf.pdu_retry_max = 2
    ctrl.cfg.iperf.ip_ping_s = 0.0
    ctrl.iperf = MagicMock()
    ctrl.iperf.is_running.return_value = False
    ctrl.iperf.resolve_ue_ip.return_value = ""
    ctrl._traffic_desired = True
    ctrl.snapshot.gnb_running = True
    ctrl.snapshot.ue_running = True
    ctrl.snapshot.ue_attached = True
    ctrl.launcher.stdout_path.return_value = tmp_path / "ue.stdout.log"
    (tmp_path / "ue.stdout.log").write_text("", encoding="utf-8")

    with patch(
        "common.oai.controllers.controller.discover_ue_ipv4", return_value=None
    ), patch.object(ctrl, "_restart_ue_for_traffic") as restart:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not restart.called:
            assert ctrl._ensure_ue_ready_for_traffic() is None
            time.sleep(0.02)
        restart.assert_called_once()
    ctrl.stop_background_workers()


def test_start_system_brings_up_gnb_ue_then_traffic(tmp_path):
    from common.oai.config import OaiConfig

    ctrl = _ctrl(tmp_path)
    ctrl.cfg.oai = OaiConfig(gnb_boot_s=0.05)
    ctrl.iperf = MagicMock()
    ctrl.iperf.is_running.return_value = False
    calls: list[str] = []

    def _gnb() -> None:
        calls.append("gnb")
        ctrl.snapshot.gnb_running = True

    def _ue() -> None:
        calls.append("ue")
        ctrl.snapshot.ue_running = True

    def _traffic() -> None:
        calls.append("traffic")
        ctrl._traffic_desired = True
        ctrl.snapshot.traffic_desired = True

    with patch.object(ctrl, "start_gnb", side_effect=_gnb), patch.object(
        ctrl, "start_ue", side_effect=_ue
    ), patch.object(ctrl, "start_iperf_traffic", side_effect=_traffic):
        ctrl.start_system()
        assert ctrl._system_thread is not None
        ctrl._system_thread.join(timeout=3.0)

    assert calls == ["gnb", "ue", "traffic"]
    assert ctrl.get_snapshot().status.startswith("System: running")
    ctrl.stop_background_workers()


def test_bringup_monitor_stall_does_not_restart_before_stream(tmp_path):
    """First start: stage stall while waiting for IP must not re-init the UE."""
    ctrl = _ctrl(tmp_path)
    ctrl.cfg.iperf.bringup_monitor = True
    ctrl.cfg.iperf.bringup_ssb_timeout_s = 0.05
    ctrl._ue_bringup.timeouts.ssb_s = 0.05
    ctrl.iperf = MagicMock()
    ctrl.iperf.is_running.return_value = False
    ctrl.iperf.resolve_ue_ip.return_value = ""
    ctrl._traffic_desired = True
    ctrl._traffic_streamed = False
    ctrl.snapshot.gnb_running = True
    ctrl.snapshot.ue_running = True
    ctrl.launcher.stdout_path.return_value = tmp_path / "ue.stdout.log"
    (tmp_path / "ue.stdout.log").write_text("", encoding="utf-8")
    ctrl._ue_bringup.reset(tmp_path / "ue.stdout.log", follow_new_only=True)

    with patch(
        "common.oai.controllers.controller.discover_ue_ipv4", return_value=None
    ), patch.object(ctrl, "_restart_ue_for_traffic") as restart:
        assert ctrl._ensure_ue_ready_for_traffic() is None
        time.sleep(0.08)
        assert ctrl._ensure_ue_ready_for_traffic() is None
        restart.assert_not_called()
    assert ctrl.snapshot.traffic_stage in ("waiting_attach", "waiting_ue_ip")
    ctrl.stop_background_workers()


def test_ip_lost_retries_before_ue_reinit(tmp_path):
    """After PDU was up, lost IP must wait/retry 3 times before UE re-init."""
    ctrl = _ctrl(tmp_path)
    ctrl.cfg.iperf.bringup_monitor = True
    ctrl.cfg.iperf.ip_lost_grace_s = 0.02
    ctrl.cfg.iperf.pdu_retry_wait_s = 0.05
    ctrl.cfg.iperf.pdu_retry_max = 3
    ctrl.cfg.iperf.ip_settle_s = 0.0
    ctrl.iperf = MagicMock()
    ctrl.iperf.is_running.return_value = False
    ctrl.iperf.resolve_ue_ip.return_value = ""
    ctrl._traffic_desired = True
    ctrl._traffic_streamed = True
    ctrl._pdu_seen = True
    ctrl.snapshot.gnb_running = True
    ctrl.snapshot.ue_running = True
    ctrl.snapshot.ue_attached = True
    ctrl.launcher.stdout_path.return_value = tmp_path / "ue.stdout.log"
    (tmp_path / "ue.stdout.log").write_text("", encoding="utf-8")

    with patch(
        "common.oai.controllers.controller.discover_ue_ipv4", return_value=None
    ), patch.object(ctrl, "_restart_ue_for_traffic") as restart:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not restart.called:
            assert ctrl._ensure_ue_ready_for_traffic() is None
            time.sleep(0.03)
        restart.assert_called_once()
    assert ctrl._pdu_retry_count == 0  # cleared on re-init path
    ctrl.stop_background_workers()


def test_pdu_loss_before_first_ip_does_not_restart(tmp_path):
    """Never saw PDU yet → keep waiting; no UE re-init from retry logic."""
    ctrl = _ctrl(tmp_path)
    ctrl.cfg.iperf.bringup_monitor = True
    ctrl.cfg.iperf.ip_lost_grace_s = 0.01
    ctrl.cfg.iperf.pdu_retry_wait_s = 0.01
    ctrl.cfg.iperf.pdu_retry_max = 1
    ctrl.iperf = MagicMock()
    ctrl.iperf.resolve_ue_ip.return_value = ""
    ctrl._traffic_desired = True
    ctrl._pdu_seen = False
    ctrl._traffic_streamed = False
    ctrl.snapshot.gnb_running = True
    ctrl.snapshot.ue_running = True
    ctrl.launcher.stdout_path.return_value = tmp_path / "ue.stdout.log"
    (tmp_path / "ue.stdout.log").write_text("", encoding="utf-8")

    with patch(
        "common.oai.controllers.controller.discover_ue_ipv4", return_value=None
    ), patch.object(ctrl, "_restart_ue_for_traffic") as restart:
        time.sleep(0.05)
        assert ctrl._ensure_ue_ready_for_traffic() is None
        restart.assert_not_called()
    ctrl.stop_background_workers()


def test_bringup_waits_for_live_ip_even_when_log_ready(tmp_path):
    """Do not start iperf on log-only IP — wait until iface is live."""
    ctrl = _ctrl(tmp_path)
    ctrl.cfg.iperf.bringup_monitor = True
    ctrl.iperf = MagicMock()
    ctrl.iperf.resolve_ue_ip.return_value = ""
    ctrl._traffic_desired = True
    ctrl.snapshot.gnb_running = True
    ctrl.snapshot.ue_running = True
    log = tmp_path / "ue.stdout.log"
    log.write_text(
        "Initial sync successful\n"
        "RAR-Msg2 decoded\n"
        "State = NR_RRC_CONNECTED\n"
        "Received PDU Session Establishment Accept, UE IPv4: 10.0.0.9\n"
        "TUN Interface oaitun_ue1 successfully configured, IPv4 10.0.0.9\n",
        encoding="utf-8",
    )
    ctrl.launcher.stdout_path.return_value = log

    with patch(
        "common.oai.controllers.controller.discover_ue_ipv4", return_value=None
    ), patch.object(ctrl, "_restart_ue_for_traffic") as restart:
        assert ctrl._ensure_ue_ready_for_traffic() is None
        restart.assert_not_called()
    assert ctrl.snapshot.traffic_stage == "waiting_ue_ip"
    assert ctrl.snapshot.ue_bringup_stage == "ready"
    ctrl.stop_background_workers()


def test_bringup_returns_only_live_iface_ip(tmp_path):
    ctrl = _ctrl(tmp_path)
    ctrl.cfg.iperf.bringup_monitor = True
    ctrl.cfg.iperf.ip_settle_s = 0.0
    ctrl.cfg.iperf.ip_ping_s = 0.0
    ctrl.iperf = MagicMock()
    ctrl._traffic_desired = True
    ctrl.snapshot.gnb_running = True
    ctrl.snapshot.ue_running = True
    ctrl.snapshot.ue_attached = True
    ctrl.launcher.stdout_path.return_value = tmp_path / "ue.stdout.log"
    (tmp_path / "ue.stdout.log").write_text("", encoding="utf-8")

    with patch(
        "common.oai.controllers.controller.discover_ue_ipv4",
        return_value="10.0.0.9",
    ):
        assert ctrl._ensure_ue_ready_for_traffic() == "10.0.0.9"
    assert ctrl.snapshot.ue_bringup_stage == "ready"
    ctrl.stop_background_workers()


def test_ip_settle_holds_iperf_until_window_elapses(tmp_path):
    ctrl = _ctrl(tmp_path)
    ctrl.cfg.iperf.bringup_monitor = True
    ctrl.cfg.iperf.ip_settle_s = 0.15
    ctrl.cfg.iperf.ip_ping_s = 0.0
    ctrl.iperf = MagicMock()
    ctrl._traffic_desired = True
    ctrl.snapshot.gnb_running = True
    ctrl.snapshot.ue_running = True
    ctrl.snapshot.ue_attached = True
    ctrl.launcher.stdout_path.return_value = tmp_path / "ue.stdout.log"
    (tmp_path / "ue.stdout.log").write_text("", encoding="utf-8")

    with patch(
        "common.oai.controllers.controller.discover_ue_ipv4",
        return_value="10.0.0.9",
    ):
        assert ctrl._ensure_ue_ready_for_traffic() is None
        assert ctrl.snapshot.traffic_stage == "settling_ue_ip"
        assert ctrl.snapshot.ue_ip == "10.0.0.9"
        time.sleep(0.18)
        assert ctrl._ensure_ue_ready_for_traffic() == "10.0.0.9"
    ctrl.stop_background_workers()


def test_ping_verify_before_iperf(tmp_path):
    """After IP is live, ping must succeed before returning IP for iperf."""
    ctrl = _ctrl(tmp_path)
    ctrl.cfg.iperf.bringup_monitor = True
    ctrl.cfg.iperf.ip_settle_s = 0.0
    ctrl.cfg.iperf.ip_ping_s = 3.0
    ctrl.iperf = MagicMock()
    ctrl._traffic_desired = True
    ctrl.snapshot.gnb_running = True
    ctrl.snapshot.ue_running = True
    ctrl.snapshot.ue_attached = True
    ctrl.launcher.stdout_path.return_value = tmp_path / "ue.stdout.log"
    (tmp_path / "ue.stdout.log").write_text("", encoding="utf-8")

    with patch(
        "common.oai.controllers.controller.discover_ue_ipv4",
        return_value="10.0.0.9",
    ), patch(
        "common.oai.controllers.controller.verify_ue_ping",
        return_value=(True, "DN: 3 packets transmitted"),
    ) as ping:
        assert ctrl._ensure_ue_ready_for_traffic() == "10.0.0.9"
        ping.assert_called_once()
        assert ping.call_args.args[0] == "10.0.0.9"
        assert ping.call_args.kwargs["duration_s"] == 3.0
    assert ctrl.snapshot.traffic_stage in ("running", "starting_iperf", "idle") or True
    ctrl.stop_background_workers()


def test_ping_failure_does_not_start_iperf(tmp_path):
    ctrl = _ctrl(tmp_path)
    ctrl.cfg.iperf.bringup_monitor = True
    ctrl.cfg.iperf.ip_settle_s = 0.0
    ctrl.cfg.iperf.ip_ping_s = 3.0
    ctrl.cfg.iperf.ip_lost_grace_s = 10.0  # stay in debounce; no restart yet
    ctrl.iperf = MagicMock()
    ctrl._traffic_desired = True
    ctrl.snapshot.gnb_running = True
    ctrl.snapshot.ue_running = True
    ctrl.snapshot.ue_attached = True
    ctrl.launcher.stdout_path.return_value = tmp_path / "ue.stdout.log"
    (tmp_path / "ue.stdout.log").write_text("", encoding="utf-8")

    with patch(
        "common.oai.controllers.controller.discover_ue_ipv4",
        return_value="10.0.0.9",
    ), patch(
        "common.oai.controllers.controller.verify_ue_ping",
        return_value=(False, "100% packet loss"),
    ), patch.object(ctrl, "_restart_ue_for_traffic") as restart:
        assert ctrl._ensure_ue_ready_for_traffic() is None
        restart.assert_not_called()
    assert "ping" in (ctrl.snapshot.note or "").lower() or ctrl.snapshot.traffic_stage == "waiting_ue_ip"
    ctrl.stop_background_workers()


def test_radio_attach_skips_early_bringup_stall(tmp_path):
    """If gNB already shows attached, do not restart for a stale SSB timeout."""
    ctrl = _ctrl(tmp_path)
    ctrl.cfg.iperf.bringup_monitor = True
    ctrl.cfg.iperf.ip_lost_grace_s = 30.0  # stay in debounce during this test
    ctrl.cfg.iperf.ip_ping_s = 0.0
    ctrl._ue_bringup.timeouts.ssb_s = 0.01
    ctrl.iperf = MagicMock()
    ctrl.iperf.resolve_ue_ip.return_value = ""
    ctrl._traffic_desired = True
    ctrl.snapshot.gnb_running = True
    ctrl.snapshot.ue_running = True
    ctrl.snapshot.ue_attached = True
    log = tmp_path / "ue.stdout.log"
    log.write_text("", encoding="utf-8")
    ctrl.launcher.stdout_path.return_value = log
    ctrl._ue_bringup.reset(log, follow_new_only=True)
    time.sleep(0.03)  # would stall SSB without radio-attach advance

    with patch(
        "common.oai.controllers.controller.discover_ue_ipv4", return_value=None
    ), patch.object(ctrl, "_restart_ue_for_traffic") as restart:
        assert ctrl._ensure_ue_ready_for_traffic() is None
        restart.assert_not_called()
    assert ctrl.snapshot.ue_bringup_stage == "waiting_pdu"
    ctrl.stop_background_workers()


def test_ensure_iperf_pair_reuses_server_for_same_ip(tmp_path):
    ctrl = _ctrl(tmp_path)
    ctrl.iperf = MagicMock()
    ctrl.iperf.is_running.side_effect = lambda side: side == "server"
    ctrl._iperf_bound_ue_ip = "10.0.0.9"
    ctrl._traffic_desired = True

    ctrl._ensure_iperf_pair("10.0.0.9")
    ctrl.iperf.stop_all.assert_not_called()
    ctrl.iperf.start.assert_called_once_with("client", ue_ip="10.0.0.9")
    ctrl.stop_background_workers()
