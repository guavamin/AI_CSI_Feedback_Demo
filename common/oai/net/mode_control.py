"""CSI feedback mode control.

Preferred path: live telnet ``aifb pucch_replace 0|1`` on gNB + UE when
``features.live_mode_telnet`` is set (requires OAI ``--ai-fb-live-mode-switch``
and ``--telnetsrv``).

Fallback: ``features.mode_switch_relaunch`` stops and restarts softmodems with
mode-specific argv.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

MODE_LEGACY = "legacy"
MODE_CSINET = "csinet"


class TelnetProbe:
    """Minimal TCP probe / command helper for OAI telnetsrv ports."""

    def __init__(self, host: str, port: int, timeout_s: float = 2.0):
        self.host = host
        self.port = int(port)
        self.timeout_s = float(timeout_s)

    def reachable(self) -> bool:
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout_s):
                return True
        except OSError:
            return False

    def send_command(self, command: str) -> str:
        """Send one line and return raw reply (best-effort).

        OAI telnetsrv keeps the connection open (prints a prompt), so we cannot
        wait for EOF — read briefly then return.
        """
        with socket.create_connection((self.host, self.port), timeout=self.timeout_s) as sock:
            sock.settimeout(0.25)
            # Consume banner / prompt if any
            try:
                sock.recv(4096)
            except OSError:
                pass
            sock.sendall((command.rstrip() + "\n").encode("utf-8", errors="replace"))
            chunks: list[bytes] = []
            deadline = time.monotonic() + min(1.0, max(0.35, self.timeout_s * 0.5))
            while time.monotonic() < deadline:
                try:
                    data = sock.recv(4096)
                except socket.timeout:
                    if chunks:
                        break
                    continue
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
                joined = b"".join(chunks)
                if len(joined) > 65536:
                    break
                # Softmodem prompt usually ends with "> "
                if b"> " in joined or b"\n" in joined:
                    # tiny grace for trailing lines
                    sock.settimeout(0.05)
                    try:
                        more = sock.recv(4096)
                        if more:
                            chunks.append(more)
                    except OSError:
                        pass
                    break
            return b"".join(chunks).decode("utf-8", errors="replace")


def pucch_replace_for_mode(mode: str) -> int:
    """Map GUI mode key → --ai-fb-pucch-replace value."""
    key = (mode or MODE_LEGACY).strip().lower()
    return 1 if key == MODE_CSINET else 0


def apply_mode_via_telnet(
    *,
    mode: str,
    gnb_host: str,
    gnb_port: int,
    ue_host: str,
    ue_port: int,
    timeout_s: float = 2.0,
) -> None:
    """Flip ``ai_fb_pucch_replace`` on gNB and UE via the telnet ``aifb`` module.

    Enabling AI flips the gNB first, disabling AI flips the UE first (see below).

    Raises ``RuntimeError`` if either side is unreachable or the command fails.
    """
    replace = pucch_replace_for_mode(mode)
    cmd = f"aifb pucch_replace {replace}"
    # Order matters because the gNB books HARQ-ACK into a CSI PUCCH only if the CSI payload it
    # expects leaves room for it (AI = 52 bits leaves none, legacy ~8 bits does). The UE must never
    # carry the larger AI payload into a PUCCH the gNB sized for legacy CSI + HARQ:
    #   enabling AI  -> gNB first (it stops booking HARQ into CSI slots), then UE
    #   disabling AI -> UE first (it shrinks its payload), then gNB
    ue_side = ("ue", ue_host, ue_port)
    gnb_side = ("gnb", gnb_host, gnb_port)
    sides = (gnb_side, ue_side) if replace else (ue_side, gnb_side)
    with _SWITCH_LOCK:
        _apply_in_order(sides, cmd, timeout_s)
    log.info("Live CSI mode applied via telnet → %s (pucch_replace=%d)", mode, replace)


# Serialize switches so two quick clicks cannot interleave their gNB/UE commands.
_SWITCH_LOCK = threading.Lock()


def _apply_in_order(sides, cmd: str, timeout_s: float) -> None:
    for name, host, port in sides:
        probe = TelnetProbe(host, port, timeout_s=timeout_s)
        if not probe.reachable():
            raise RuntimeError(
                f"{name} telnet {host}:{port} unreachable — is --telnetsrv running?"
            )
        try:
            reply = probe.send_command(cmd)
        except OSError as exc:
            raise RuntimeError(f"{name} telnet command failed: {exc}") from exc
        low = (reply or "").lower()
        if "refusing" in low or "expected 0 or 1" in low or "couldn't" in low:
            raise RuntimeError(f"{name} rejected '{cmd}': {reply.strip()[:240]}")
        log.info("%s telnet OK → %s (reply=%r)", name, cmd, (reply or "")[:120])


# ---------------------------------------------------------------------------
# Scheduled switching ("aifb schedule <0|1> <frame>", OAI gui-live-switch branch)
# ---------------------------------------------------------------------------

FRAME_S = 0.010  # one NR frame
SFN_MODULO = 1024
OAI_MIN_LEAD_FRAMES = 3  # must match AI_FB_LIVE_MIN_LEAD_FRAMES in OAI


def sfn_delta(a: int, b: int) -> int:
    """a - b on the 1024-frame SFN circle, in [-512, 511]."""
    return ((a - b) % SFN_MODULO + SFN_MODULO + 512) % SFN_MODULO - 512


def telnet_command(host: str, port: int, command: str, timeout_s: float = 1.0) -> str:
    """One short telnet exchange with an OAI softmodem.

    OAI's telnet server sends no greeting on connect and answers each command followed by its prompt
    ("softmodem_gnb> "), so: connect, send immediately, read until the prompt. Takes a few ms locally.
    Short connections (instead of one kept open) matter because the server serves one client at a
    time with a listen backlog of 1.
    """
    deadline = time.monotonic() + timeout_s
    with socket.create_connection((host, port), timeout=timeout_s) as sock:
        sock.sendall((command.rstrip() + "\n").encode("utf-8", errors="replace"))
        buf = b""
        while not buf.endswith(b"> "):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no prompt from {host}:{port} after {command!r}")
            sock.settimeout(remaining)
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    text = buf.decode("utf-8", errors="replace")
    # drop the trailing prompt line
    if text.endswith("> "):
        text = text[: text.rfind("\n") + 1] if "\n" in text else ""
    return text


def parse_aifb_status(text: str) -> dict[str, str]:
    """key=value tokens of the 'aifb status ...' line."""
    for line in text.splitlines():
        if line.startswith("aifb status"):
            return dict(tok.split("=", 1) for tok in line.split()[2:] if "=" in tok)
    raise RuntimeError(f"no 'aifb status' line in reply: {text.strip()[:200]!r}")


@dataclass
class ScheduledSwitchResult:
    mode: str
    value: int
    target_frame: int
    lead_frames: int
    attempts: int
    armed_after_s: float  # click -> both sides armed
    switched_after_s: float  # click -> target frame reached on the gNB
    verified_after_s: float  # click -> both sides confirmed the new value
    resynced: bool = False
    replies: dict[str, str] = field(default_factory=dict)


def _on_both(sides: dict[str, tuple[str, int]], command: str, timeout_s: float) -> dict[str, tuple[bool, str]]:
    """Send the same command to every side in parallel; (ok, reply-or-error) per side."""
    out: dict[str, tuple[bool, str]] = {}

    def run(name: str, host: str, port: int) -> None:
        try:
            out[name] = (True, telnet_command(host, port, command, timeout_s))
        except OSError as exc:
            out[name] = (False, f"{name} telnet {host}:{port} failed: {exc}")

    threads = [threading.Thread(target=run, args=(n, h, p), daemon=True) for n, (h, p) in sides.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout_s + 0.5)
    for name in sides:
        out.setdefault(name, (False, f"{name}: no reply"))
    return out


def _wait_no_pending(sides: dict[str, tuple[str, int]], timeout_s: float, max_wait_s: float = 3.5) -> None:
    """Wait until no side has a switch pending.

    OAI keeps a fired switch pending for AI_FB_LIVE_COMMIT_MARGIN_FRAMES (80 ms) after its target so
    late decodes of earlier slots still use the old payload size; a new switch can only be armed after
    that. A stale future switch (e.g. from a crashed GUI) is simply allowed to fire first.
    """
    deadline = time.monotonic() + max_wait_s
    while True:
        states = _on_both(sides, "aifb status", timeout_s)
        pending = {
            n: parse_aifb_status(t).get("pending", "0") for n, (ok, t) in states.items() if ok
        }
        if len(pending) == len(sides) and all(p == "0" for p in pending.values()):
            return
        if time.monotonic() > deadline:
            raise RuntimeError(f"a previous switch is still pending: {states}")
        time.sleep(FRAME_S)


def _arm_both(
    sides: dict[str, tuple[str, int]], value: int, lead: int, timeout_s: float, max_attempts: int
) -> tuple[int, int, int, float, dict[str, str]]:
    """Arm the same switch on every side. Returns (target, lead, attempts, t_gnb_status, replies).

    All-or-nothing: if any side refuses or cannot be reached, every side is cancelled before retrying
    (a refused target that was too close is retried with twice the lead).
    """
    last_err = ""
    for attempt in range(1, max_attempts + 1):
        status = parse_aifb_status(telnet_command(*sides["gnb"], "aifb status", timeout_s))
        t_status = time.monotonic()
        now = int(status.get("frame", "-1"))
        if now < 0:
            raise RuntimeError("gNB frame timing not running yet")
        target = (now + lead) % SFN_MODULO
        replies = _on_both(sides, f"aifb schedule {value} {target}", timeout_s)
        if all(ok and "aifb armed" in text for ok, text in replies.values()):
            return target, lead, attempt, t_status, {n: t.strip() for n, (_, t) in replies.items()}
        last_err = "; ".join(f"{n}: {t.strip()[:160]}" for n, (ok, t) in replies.items() if not (ok and "aifb armed" in t))
        log.warning("Live switch arm attempt %d failed (%s); cancelling on both sides", attempt, last_err)
        _on_both(sides, "aifb cancel", timeout_s)
        if "frames from now" in last_err or "no reply" in last_err or "failed" in last_err:
            lead = min(lead * 2, 200)
    raise RuntimeError(f"could not arm the switch on both sides: {last_err}")


def apply_mode_scheduled(
    *,
    mode: str,
    gnb_host: str,
    gnb_port: int,
    ue_host: str,
    ue_port: int,
    lead_frames: int = 15,
    timeout_s: float = 1.0,
    max_attempts: int = 3,
    on_armed: Callable[[int, int], None] | None = None,
) -> ScheduledSwitchResult:
    """Switch gNB and UE to ``mode`` at the same SFN (arm -> confirm -> fire -> verify).

    1. read the gNB frame, pick target = now + lead_frames;
    2. arm ``aifb schedule <v> <target>`` on both sides in parallel; if either refuses, cancel both
       and retry (the system never ends up half-switched);
    3. wait until the gNB passes the target frame (both sides switch there on their own);
    4. read ``aifb status`` on both sides; if they disagree, run the procedure once more (a side that
       already has the value answers "no change needed").
    """
    value = pucch_replace_for_mode(mode)
    sides = {"gnb": (gnb_host, gnb_port), "ue": (ue_host, ue_port)}
    lead = max(int(lead_frames), OAI_MIN_LEAD_FRAMES + 2)
    t0 = time.monotonic()
    resynced = False
    with _SWITCH_LOCK:
        for round_ in range(2):
            _wait_no_pending(sides, timeout_s)
            target, used_lead, attempts, t_status, replies = _arm_both(sides, value, lead, timeout_s, max_attempts)
            t_armed = time.monotonic()
            if on_armed is not None:
                on_armed(target, used_lead)
            # sleep until just before the target, then poll the gNB frame
            time.sleep(max(0.0, used_lead * FRAME_S - (time.monotonic() - t_status) - 0.005))
            # RF-sim with a channel model can run several times slower than real time; frames, not
            # wall-clock time, decide when the switch happens, so only bound the wait generously.
            deadline = time.monotonic() + max(2.0, 20 * used_lead * FRAME_S)
            while True:
                now = int(parse_aifb_status(telnet_command(*sides["gnb"], "aifb status", timeout_s)).get("frame", "-1"))
                if now >= 0 and sfn_delta(now, target) >= 1:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError(f"gNB did not reach target frame {target} (now {now})")
                time.sleep(FRAME_S / 2)
            t_switched = time.monotonic()
            states = _on_both(sides, "aifb status", timeout_s)
            values = {}
            for name, (ok, text) in states.items():
                if not ok:
                    raise RuntimeError(f"cannot verify {name} after switch: {text}")
                values[name] = int(parse_aifb_status(text).get("pucch_replace", "-1"))
            if all(v == value for v in values.values()):
                res = ScheduledSwitchResult(
                    mode=mode,
                    value=value,
                    target_frame=target,
                    lead_frames=used_lead,
                    attempts=attempts,
                    armed_after_s=t_armed - t0,
                    switched_after_s=t_switched - t0,
                    verified_after_s=time.monotonic() - t0,
                    resynced=resynced,
                    replies=replies,
                )
                log.info(
                    "Live switch → %s at frame %d: armed %.3fs, switched %.3fs, verified %.3fs (lead %d, attempts %d%s)",
                    mode, target, res.armed_after_s, res.switched_after_s, res.verified_after_s,
                    used_lead, attempts, ", resynced" if resynced else "",
                )
                return res
            log.warning("Live switch → %s: sides disagree after frame %d (%s); resyncing", mode, target, values)
            resynced = True
        raise RuntimeError(f"gNB/UE still disagree after resync: {values}")


def relaunch_for_mode(
    *,
    mode: str,
    gnb_was_running: bool,
    ue_was_running: bool,
    stop_ue: Callable[[], None],
    stop_gnb: Callable[[], None],
    start_gnb: Callable[[str], None],
    start_ue: Callable[[str], None],
    settle_s: float = 1.5,
    gnb_boot_s: float = 3.0,
) -> None:
    """Stop running softmodems and start them again with ``mode`` argv."""
    log.info(
        "Mode relaunch → %s (gnb_was=%s ue_was=%s)",
        mode,
        gnb_was_running,
        ue_was_running,
    )
    if ue_was_running:
        stop_ue()
    if gnb_was_running:
        stop_gnb()
    time.sleep(max(0.2, settle_s))
    if gnb_was_running:
        start_gnb(mode)
        time.sleep(max(0.5, gnb_boot_s))
    if ue_was_running:
        start_ue(mode)
    log.info("Mode relaunch complete → %s", mode)
