"""Robot control for the UE station: the AgileX Scout Mini that carries the UE (robot/scout_line.py, generalised).

RobotControl runs robot_driver.py as a separate process (with a Python that has pyagxrobots), reads its JSON
status lines, integrates the measured speed / turn rate into the path the robot actually drove, and keeps a log
(data/runs/robot_logs/robot_<time>.jsonl, every status line) so recorded data can later be matched with the
motion. expected_path() gives the planned trajectory for the GUI. Stop always works: SIGINT to the driver, which
commands zero velocity before it exits; SIGKILL after 3 s as a last resort (the robot's own command timeout then
stops it).
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from common.style import REPO

log = logging.getLogger(__name__)
DRIVER = Path(__file__).with_name("robot_driver.py")
MAX_SPEED, MAX_TURN = 0.3, 0.5  # also enforced by the driver


@dataclass
class RobotParams:
    pattern: str = "line"  # line | arc (back and forth, retracing the same path)
    speed: float = 0.15  # m/s
    leg_s: float = 12.0  # seconds per direction
    pause_s: float = 1.0  # stopped at each end
    cycles: int = 100  # 0: until stopped
    turn: float = 0.2  # rad/s while going forward (arc only)

    def clamp(self) -> "RobotParams":
        self.speed = min(max(self.speed, 0.02), MAX_SPEED)
        self.leg_s = min(max(self.leg_s, 1.0), 120.0)
        self.pause_s = min(max(self.pause_s, 0.0), 10.0)
        self.cycles = max(int(self.cycles), 0)
        self.turn = min(max(self.turn, -MAX_TURN), MAX_TURN)
        return self

    @property
    def turn_rate(self) -> float:
        return self.turn if self.pattern == "arc" else 0.0

    @property
    def leg_m(self) -> float:
        return self.speed * self.leg_s

    @property
    def cycle_s(self) -> float:
        return 2 * (self.leg_s + self.pause_s)


def arc_points(v: float, w: float, seconds: float, n: int = 120) -> np.ndarray:
    """Planar path from (0, 0) heading +x, at speed v (m/s) and turn rate w (rad/s): [n, 2] in metres."""
    t = np.linspace(0.0, seconds, n)
    if abs(w) < 1e-6:
        return np.stack([v * t, np.zeros_like(t)], axis=1)
    r = v / w
    return np.stack([r * np.sin(w * t), r * (1 - np.cos(w * t))], axis=1)


def expected_path(p: RobotParams) -> np.ndarray:
    """One forward leg; the backward leg retraces it."""
    return arc_points(p.speed, p.turn_rate, p.leg_s)


def expected_position(p: RobotParams, phase: str, leg_t: float) -> tuple[float, float] | None:
    """Where the plan puts the robot: phase / time in the leg from the driver's status."""
    if phase == "forward":
        return tuple(map(float, arc_points(p.speed, p.turn_rate, leg_t, 2)[-1]))
    if phase == "backward":
        return tuple(map(float, arc_points(p.speed, p.turn_rate, max(p.leg_s - leg_t, 0.0), 2)[-1]))
    if phase == "pause_end":
        return tuple(map(float, expected_path(p)[-1]))
    if phase in ("pause_start", "connected"):
        return (0.0, 0.0)
    return None


def can_status(iface: str = "can0") -> tuple[bool, str]:
    """(up, text) for the USB-to-CAN interface."""
    try:
        out = subprocess.run(["ip", "-details", "link", "show", iface], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"cannot check {iface}: {exc}"
    if out.returncode != 0:
        return False, f"{iface} not found: USB-to-CAN adapter plugged in, gs_usb driver loaded?"
    text = out.stdout
    up = ",UP," in text.split(">")[0] + "," or "state UP" in text
    bitrate = next((tok for tok in text.split() if tok.isdigit() and int(tok) in (125000, 250000, 500000, 1000000)), "")
    if not up:
        return False, f"{iface} is down"
    return True, f"{iface} up" + (f", {int(bitrate) // 1000} kbit/s" if bitrate else "")


def bring_up_can(iface: str = "can0") -> str:
    """modprobe gs_usb + ip link set can0 up type can bitrate 500000, through passwordless sudo (setup_station.sh
    adds exactly these commands); returns an error text, or "" on success."""
    for cmd in (["modprobe", "gs_usb"], ["ip", "link", "set", iface, "up", "type", "can", "bitrate", "500000"]):
        exe = shutil.which(cmd[0], path="/usr/sbin:/usr/bin:/sbin:/bin") or cmd[0]
        res = subprocess.run(["sudo", "-n", exe, *cmd[1:]], capture_output=True, text=True)
        if res.returncode != 0 and "busy" not in res.stderr.lower():
            return (res.stderr.strip() or f"{' '.join(cmd)} failed") + \
                " (as root: modprobe gs_usb; ip link set can0 up type can bitrate 500000)"
    return ""


@dataclass
class RobotView:
    running: bool = False
    phase: str = "idle"  # idle | connecting | connected | forward | pause_end | backward | pause_start | done | stopped | error
    cycle: int = 0
    leg_t: float = 0.0
    v_cmd: float = 0.0
    v_meas: float | None = None
    w_meas: float | None = None
    battery_v: float | None = None
    error: str = ""
    dry_run: bool = False
    expected_xy: tuple[float, float] | None = None
    actual: np.ndarray = None  # [n, 2] odometry path this run (metres, start = (0, 0) heading +x)
    speeds: list = None  # [(t, v_cmd, v_meas)] last 60 s
    started: float = 0.0


class RobotControl:
    def __init__(self, python: str = "", dry_run: bool = False, log_dir: Path | None = None):
        self.python = python or sys.executable
        self.dry_run = dry_run
        self.params = RobotParams()
        self.log_dir = log_dir or REPO / "data/runs/robot_logs"
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._view = RobotView(actual=np.zeros((1, 2)), speeds=[])
        self._pose = [0.0, 0.0, 0.0]  # x, y, heading from measured speed / turn rate
        self._last_t: float | None = None
        self._last_vw = (0.0, 0.0)
        self._path: deque = deque(maxlen=20000)
        self._speeds: deque = deque(maxlen=600)
        self._run_params = RobotParams()

    # ------------------------------------------------------------------ control
    def start(self, params: RobotParams | None = None) -> str:
        """Start the driver with params; returns an error text or ""."""
        if self.running():
            return "already running"
        p = RobotParams(**asdict(params or self.params)).clamp()
        self.params = self._run_params = p
        args = [self.python, "-u", str(DRIVER), "--speed", f"{p.speed}", "--leg", f"{p.leg_s}", "--pause",
                f"{p.pause_s}", "--cycles", str(p.cycles), "--turn", f"{p.turn_rate}"]
        if self.dry_run:
            args.append("--dry-run")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / time.strftime("robot_%Y%m%d_%H%M%S.jsonl")
        try:
            self._proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                          start_new_session=True)
        except OSError as exc:
            return f"cannot start the robot driver with {self.python}: {exc}"
        with self._lock:
            self._pose, self._last_t = [0.0, 0.0, 0.0], None
            self._path.clear()
            self._path.append((0.0, 0.0))
            self._speeds.clear()
            self._view = RobotView(running=True, phase="connecting", dry_run=self.dry_run, actual=np.zeros((1, 2)),
                                   speeds=[], started=time.time())
        threading.Thread(target=self._read, args=(self._proc, log_path, p), name="robot-status", daemon=True).start()
        log.info("robot: %s (log %s)", " ".join(args[2:]), log_path)
        return ""

    def stop(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.kill(proc.pid, signal.SIGINT)  # the driver commands zero velocity, then exits
            proc.wait(3.0)
        except subprocess.TimeoutExpired:
            log.warning("robot driver did not exit: killing it")
            proc.kill()
        except ProcessLookupError:
            pass

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def view(self) -> RobotView:
        with self._lock:
            v = self._view
            v.running = self.running()
            v.actual = np.array(self._path) if self._path else np.zeros((1, 2))
            v.speeds = list(self._speeds)
            return v

    # ------------------------------------------------------------------ driver output
    def _read(self, proc: subprocess.Popen, log_path: Path, p: RobotParams) -> None:
        with log_path.open("w") as out:
            out.write(json.dumps({"params": asdict(p), "dry_run": self.dry_run}) + "\n")
            for line in proc.stdout:
                line = line.strip()
                if not line.startswith("{"):
                    log.info("robot driver: %s", line)
                    if "Error" in line or "error" in line:
                        with self._lock:
                            self._view.error = line[-200:]
                    continue
                out.write(line + "\n")
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                self._update(msg, p)
        rc = proc.wait()
        with self._lock:
            self._view.running = False
            if rc != 0 and not self._view.error:
                self._view.error = f"robot driver exited with code {rc}"
            if self._view.phase not in ("stopped", "done", "error"):
                self._view.phase = "stopped"

    def _update(self, msg: dict, p: RobotParams) -> None:
        with self._lock:
            v = self._view
            phase = msg.get("phase", v.phase)
            if phase == "released":
                return
            v.phase, v.cycle = phase, int(msg.get("cycle", v.cycle))
            v.leg_t = float(msg.get("leg_t", 0.0))
            v.v_cmd = float(msg.get("v_cmd", 0.0))
            for key in ("v_meas", "w_meas", "battery_v"):
                if key in msg:
                    setattr(v, key, msg[key])
            if msg.get("error"):
                v.error = msg["error"]
            v.expected_xy = expected_position(p, phase, v.leg_t)
            t = float(msg.get("t", time.time()))
            vm, wm = msg.get("v_meas"), msg.get("w_meas")
            if vm is not None and wm is not None and phase in ("forward", "backward", "pause_end", "pause_start"):
                if self._last_t is not None:  # the previous reading held until now (left Riemann sum)
                    dt = min(max(t - self._last_t, 0.0), 0.5)
                    pv, pw = self._last_vw
                    x, y, th = self._pose
                    self._pose = [x + pv * math.cos(th) * dt, y + pv * math.sin(th) * dt, th + pw * dt]
                    self._path.append((self._pose[0], self._pose[1]))
                self._last_t, self._last_vw = t, (vm, wm)
                self._speeds.append((t, v.v_cmd, vm))
