"""Scenario logic of common.demo_control with a fake controller and a fake gNB telnet (no OAI needed)."""

from __future__ import annotations

import time

import pytest

import common.demo_control as dc
from common.oai.config import AppConfig
from common.oai.controllers.controller import RuntimeSnapshot


class FakeGnb:
    """'aifb status' / 'aifb decoder' like the OAI gNB (gui-live-switch), committing on the next status."""

    def __init__(self, alt_loaded=True):
        self.frame, self.decoder, self.pending, self.pucch = 100, 0, None, 0
        self.alt_loaded = alt_loaded
        self.log: list[str] = []

    def __call__(self, host, port, command, timeout_s=1.0):
        self.log.append(command)
        self.frame = (self.frame + 20) % 1024
        if command == "aifb status":
            if self.pending is not None:
                self.decoder, self.pending = self.pending, None
            return (f"aifb status live_mode_switch=1 impl_mode=13 pucch_replace={self.pucch} frame={self.frame} "
                    f"pending=0 decoder={self.decoder} decoder_pending={0 if self.pending is None else 1} "
                    f"alt_decoder_loaded={int(self.alt_loaded)}\n")
        if command.startswith("aifb decoder"):
            _, _, slot, target = command.split()
            self.pending = int(slot)
            return f"aifb armed decoder={slot} target_frame={target} now={self.frame} lead=15\n"
        raise AssertionError(command)


class FakeController:
    def __init__(self, gnb: FakeGnb, running=True):
        self.snap = RuntimeSnapshot(gnb_running=running, ue_running=running)
        self.gnb, self.running = gnb, running
        self.csirs_record_root = None
        self.calls: list[str] = []

    def get_snapshot(self):
        return self.snap

    def _softmodems_ready_for_live_mode(self):
        return self.running

    def set_csi_mode(self, mode):
        self.calls.append(f"mode {mode}")
        self.gnb.log.append(f"mode {mode}")
        self.snap.csi_mode = mode
        self.snap.mode_pending = False
        self.snap.note = f"Switched to {mode}"
        self.gnb.pucch = int(mode == "csinet")


def make(monkeypatch, running=True, alt_loaded=True, boot="legacy"):
    gnb = FakeGnb(alt_loaded)
    monkeypatch.setattr(dc, "telnet_command", gnb)
    cfg = AppConfig()
    cfg.csi.default_mode = boot
    cfg.features.live_mode_lead_frames = 15
    ctrl = FakeController(gnb, running)
    ctrl.snap.csi_mode = boot
    gnb.pucch = int(boot == "csinet")
    return dc.DemoController(cfg, controller=ctrl), gnb, ctrl


def wait(demo, timeout=3.0):
    t0 = time.monotonic()
    while demo.view().switching and time.monotonic() - t0 < timeout:
        time.sleep(0.01)
    assert not demo.view().switching


def actions(gnb):
    return [c for c in gnb.log if c != "aifb status"]


def test_legacy_to_cross_swaps_decoder_before_turning_ai_on(monkeypatch):
    demo, gnb, _ = make(monkeypatch)
    assert demo.set_scenario("cross")
    wait(demo)
    acts = actions(gnb)
    assert acts[0].startswith("aifb decoder 1 ") and acts[1] == "mode csinet"
    v = demo.view()
    assert (v.scenario, v.decoder, gnb.decoder, gnb.pucch) == ("cross", 1, 1, 1)
    assert v.events[-1].level == "ok" and "Legacy -> Cross vendor" in v.events[-1].text


def test_same_cross_only_swaps_the_gnb_decoder(monkeypatch):
    demo, gnb, _ = make(monkeypatch, boot="csinet")
    assert demo.view().scenario == "same"
    demo.set_scenario("cross")
    wait(demo)
    demo.set_scenario("same")
    wait(demo)
    acts = actions(gnb)
    assert len(acts) == 2 and acts[0].startswith("aifb decoder 1 ") and acts[1].startswith("aifb decoder 0 ")
    assert demo.view().scenario == "same" and gnb.decoder == 0


def test_ai_to_legacy_keeps_decoder_and_back_to_cross_needs_no_swap(monkeypatch):
    demo, gnb, _ = make(monkeypatch)
    demo.set_scenario("cross")
    wait(demo)
    demo.set_scenario("legacy")
    wait(demo)
    demo.set_scenario("cross")
    wait(demo)
    acts = actions(gnb)
    assert acts[1:] == ["mode csinet", "mode legacy", "mode csinet"]
    assert demo.view().scenario == "cross"


def test_decoder_target_frame_uses_the_lead(monkeypatch):
    demo, gnb, _ = make(monkeypatch, boot="csinet")
    demo.set_scenario("cross")
    wait(demo)
    swap = actions(gnb)[0]
    # the status read just before the swap returned frame f; target = f + 15
    assert swap == "aifb decoder 1 135"


def test_refuses_while_switching_and_unknown_keys(monkeypatch):
    demo, _, _ = make(monkeypatch)
    assert not demo.set_scenario("nope")
    with demo._lock:
        demo._switching = True
    assert not demo.set_scenario("same")
    assert "already in progress" in demo.view().events[-1].text


def test_missing_alt_decoder_fails_cleanly(monkeypatch):
    demo, gnb, _ = make(monkeypatch, alt_loaded=False, boot="csinet")
    demo.set_scenario("cross")
    wait(demo)
    v = demo.view()
    assert v.scenario == "same" and v.target_scenario == "same"
    assert v.events[-1].level == "fail" and "alternative decoder" in v.events[-1].text
    assert not any(c.startswith("aifb decoder") for c in gnb.log)


def test_selection_before_the_system_runs(monkeypatch):
    demo, gnb, ctrl = make(monkeypatch, running=False)
    demo.set_scenario("cross")
    wait(demo)
    v = demo.view()
    # boots with AI on and decoder 0 (Same vendor); the decoder swap follows once traffic runs
    assert (v.scenario, v.target_scenario) == ("same", "cross")
    assert ctrl.calls == ["mode csinet"] and not any(c.startswith("aifb") for c in gnb.log)


@pytest.mark.parametrize("snap, expect", [
    (dict(), ["idle"] * 6),
    (dict(gnb_running=True, gnb_boot_stage="ru_init"), ["ok", "busy", "idle", "idle", "idle", "idle"]),
    (dict(gnb_running=True, gnb_boot_stage="rf_ssb", ue_running=True, ue_bringup_stage="waiting_rrc"),
     ["ok", "ok", "ok", "busy", "idle", "idle"]),  # only the current step is busy
    (dict(gnb_running=True, gnb_boot_stage="rf_ssb", ue_running=True, ue_bringup_stage="ready", ue_attached=True,
          ue_ip="10.0.0.3", traffic_stage="running", iperf_client_running=True, traffic_desired=True), ["ok"] * 6),
])
def test_bringup_steps(snap, expect):
    assert [st for _, st in dc.bringup_steps(RuntimeSnapshot(**snap))] == expect


def test_scenario_timeline(monkeypatch):
    demo, gnb, _ = make(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(dc.time, "time", lambda: clock[0])
    assert demo.scenario_at(999.0) is None  # nothing before the system starts
    demo.ctrl.start_system = lambda: None
    demo._start_worker = lambda: None
    demo.start_system()  # boots in legacy at t=1000
    clock[0] = 1010.0
    demo.set_scenario("cross")
    wait(demo)  # switch marked at 1010 (None) and confirmed at 1010 (cross)
    clock[0] = 1020.0
    assert demo.scenario_at(1005.0) == "legacy"
    assert demo.scenario_at(1015.0) == "cross"
    assert demo.scenario_at(1011.0, settle_s=2.0) is None  # BLER / MCS still settling
    assert demo.scenario_at(1013.0, settle_s=2.0) == "cross"
    demo.ctrl.stop_system = lambda: None
    demo.ctrl.stop_iperf_traffic = lambda: None
    demo.stop_system()
    assert demo.scenario_at(1021.0) is None


def test_failed_switch_counts_for_no_scenario(monkeypatch):
    demo, gnb, _ = make(monkeypatch, alt_loaded=False, boot="csinet")
    demo._mark("same")
    demo.set_scenario("cross")
    wait(demo)
    assert demo.scenario_at(time.time() + 1) is None
