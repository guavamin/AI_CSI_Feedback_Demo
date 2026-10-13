"""Scheduled live Legacy/AI switching (mode_control.apply_mode_scheduled) against a fake OAI telnet server.

The fake mimics the OAI side (openairinterface5g gui-live-switch, ai_fb_live_switch.c): no greeting on
connect, reply + prompt per command, "aifb schedule/cancel/status" with the same lead/commit rules, and
a 10 ms frame clock shared by gNB and UE.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from common.oai.net import mode_control as mc

T0 = time.monotonic()


def current_frame() -> int:
    return int((time.monotonic() - T0) / 0.010) % 1024


class FakeOai:
    MIN_LEAD = 3
    MAX_LEAD = 300
    COMMIT = 8

    def __init__(self, name: str, *, refuse_arms: int = 0, ignore_switch_once: bool = False):
        self.name = name
        self.base = 0
        self.armed = False
        self.new_value = 0
        self.target = -1
        self.refuse_arms = refuse_arms  # refuse this many schedule commands ("frames from now")
        self.ignore_switch_once = ignore_switch_once  # arm OK but never apply the first switch
        self.commands: list[str] = []
        self.lock = threading.Lock()
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    # --- OAI semantics -------------------------------------------------
    def _commit(self, now: int) -> None:
        if self.armed and mc.sfn_delta(now, self.target) >= self.COMMIT:
            self.base = self.new_value
            self.armed = False

    def _effective(self, now: int) -> int:
        if self.armed and mc.sfn_delta(now, self.target) >= 0:
            return self.new_value
        return self.base

    def handle(self, line: str) -> str:
        now = current_frame()
        parts = line.split()
        with self.lock:
            self.commands.append(line)
            self._commit(now)
            if parts[:2] == ["aifb", "status"]:
                return (
                    f"aifb status live_mode_switch=1 impl_mode=13 pucch_replace={self._effective(now)} frame={now} "
                    f"pending={int(self.armed)} pending_value={self.new_value if self.armed else -1} "
                    f"target_frame={self.target if self.armed else -1} epoch=0\n"
                )
            if parts[:2] == ["aifb", "schedule"]:
                value, target = int(parts[2]), int(parts[3])
                lead = mc.sfn_delta(target, now)
                if self.refuse_arms > 0:
                    self.refuse_arms -= 1
                    return f"aifb: refusing, target frame {target} is {lead} frames from now ({now}); need 3..300\n"
                if self.armed:
                    if (self.new_value, self.target) == (value, target):
                        return f"aifb armed pucch_replace={value} target_frame={target} now={now} lead={lead} (already armed)\n"
                    return f"aifb: refusing, switch to {self.new_value} at frame {self.target} already pending\n"
                if self.base == value:
                    return f"aifb armed pucch_replace={value} target_frame={target} now={now} lead={lead} (no change needed)\n"
                if not self.MIN_LEAD <= lead <= self.MAX_LEAD:
                    return f"aifb: refusing, target frame {target} is {lead} frames from now ({now}); need 3..300\n"
                if self.ignore_switch_once:
                    self.ignore_switch_once = False
                    return f"aifb armed pucch_replace={value} target_frame={target} now={now} lead={lead}\n"
                self.armed, self.new_value, self.target = True, value, target
                return f"aifb armed pucch_replace={value} target_frame={target} now={now} lead={lead}\n"
            if parts[:2] == ["aifb", "cancel"]:
                if not self.armed:
                    return f"aifb cancelled nothing pending (pucch_replace={self.base} now={now})\n"
                if mc.sfn_delta(self.target, now) < self.MIN_LEAD:
                    return "aifb: refusing to cancel, too close or done\n"
                self.armed = False
                return f"aifb cancelled pending switch to {self.new_value} at frame {self.target} (now {now})\n"
        return f"Error: \n      {line}\n is not a softmodem command\n"

    def value_now(self) -> int:
        with self.lock:
            return self._effective(current_frame())

    # --- telnet transport like OAI's telnetsrv ---------------------------
    def _serve(self) -> None:
        while True:
            conn, _ = self.srv.accept()
            with conn:
                buf = b""
                while True:
                    data = conn.recv(4096)
                    if not data:
                        break
                    buf += data
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        reply = self.handle(raw.decode().strip())
                        conn.sendall(reply.encode() + f"softmodem_{self.name}> ".encode())


def switch(gnb: FakeOai, ue: FakeOai, mode: str, lead: int = 15) -> mc.ScheduledSwitchResult:
    return mc.apply_mode_scheduled(
        mode=mode,
        gnb_host="127.0.0.1",
        gnb_port=gnb.port,
        ue_host="127.0.0.1",
        ue_port=ue.port,
        lead_frames=lead,
    )


def test_sfn_delta_wraps():
    assert mc.sfn_delta(5, 1020) == 9
    assert mc.sfn_delta(1020, 5) == -9
    assert mc.sfn_delta(100, 100) == 0


def test_telnet_command_is_fast_and_strips_prompt():
    gnb = FakeOai("gnb")
    t = time.monotonic()
    reply = mc.telnet_command("127.0.0.1", gnb.port, "aifb status")
    assert time.monotonic() - t < 0.1  # no greeting wait
    assert reply.startswith("aifb status") and "softmodem_gnb>" not in reply
    assert mc.parse_aifb_status(reply)["pucch_replace"] == "0"


def test_scheduled_switch_both_sides_same_frame():
    gnb, ue = FakeOai("gnb"), FakeOai("5Gue")
    res = switch(gnb, ue, "csinet")
    assert gnb.value_now() == 1 and ue.value_now() == 1
    assert res.attempts == 1 and not res.resynced
    targets = {c.split()[3] for c in gnb.commands + ue.commands if c.startswith("aifb schedule")}
    assert targets == {str(res.target_frame)}  # identical target on both sides
    assert 0.12 <= res.switched_after_s < 0.5  # ~150 ms lead, well under the old 0.5 s
    res = switch(gnb, ue, "legacy")
    assert gnb.value_now() == 0 and ue.value_now() == 0


def test_refused_arm_rolls_back_and_retries_with_longer_lead():
    gnb, ue = FakeOai("gnb"), FakeOai("5Gue", refuse_arms=1)
    res = switch(gnb, ue, "csinet")
    assert res.attempts == 2 and res.lead_frames == 30
    assert any(c == "aifb cancel" for c in gnb.commands)  # gNB's first arm was rolled back
    assert gnb.value_now() == 1 and ue.value_now() == 1


def test_persistent_refusal_leaves_system_unchanged():
    gnb, ue = FakeOai("gnb"), FakeOai("5Gue", refuse_arms=99)
    with pytest.raises(RuntimeError, match="could not arm"):
        switch(gnb, ue, "csinet")
    time.sleep(0.4)
    assert gnb.value_now() == 0 and ue.value_now() == 0  # never half-switched
    assert not gnb.armed


def test_disagreement_after_switch_is_resynced():
    gnb, ue = FakeOai("gnb"), FakeOai("5Gue", ignore_switch_once=True)
    res = switch(gnb, ue, "csinet")
    assert res.resynced
    assert gnb.value_now() == 1 and ue.value_now() == 1
