"""Drive the AgileX Scout Mini back and forth along a line or an arc (from robot/scout_line.py).

    python robot_driver.py --speed 0.15 --leg 12 --pause 1 --cycles 100 [--turn 0.0] [--dry-run]

Each cycle: forward for --leg seconds at --speed m/s (turning at --turn rad/s), stop for --pause seconds, then
backward with the same speed and the opposite turn rate (so it retraces the same path), stop again. --cycles 0:
until stopped. Commands at 10 Hz, as scout_line.py. Prints one JSON line per command: time, phase, cycle,
time in the leg, commanded and (with the robot) measured speed / turn rate, battery voltage.

Stops the robot (zero velocity) on SIGINT / SIGTERM, at the end, and on any error, releases the CAN socket and
exits (a second Ctrl+C exits at once).
--dry-run: no robot, no CAN; the measured values follow the commands (testing the GUI).
Runs with any Python that has pyagxrobots (the station GUI starts it as a separate process).
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import signal
import sys
import time

MAX_SPEED = 0.3  # m/s: the GUI's limit as well
MAX_TURN = 0.5  # rad/s
PERIOD_S = 0.1  # 10 Hz command loop

stop = False


def on_signal(*_):
    """First Ctrl+C / SIGTERM: stop the robot and exit cleanly; a second one: exit at once."""
    global stop
    if stop:
        os._exit(130)
    stop = True


class DryRun:
    """No robot: measured = commanded."""

    def __init__(self):
        self.v = self.w = 0.0

    def SetMotionCommand(self, linear_vel=0.0, angular_vel=0.0):  # noqa: N802 - pyagxrobots' name
        self.v, self.w = linear_vel, angular_vel

    def GetLinearVelocity(self):  # noqa: N802
        return self.v

    def GetAngularVelocity(self):  # noqa: N802
        return self.w

    def GetBatteryVoltage(self):  # noqa: N802
        return float("nan")


def robot_heard(channel: str = "can0", timeout_s: float = 3.0) -> int:
    """Frames received from the robot on channel within timeout_s (the base broadcasts its state every few ms).
    pyagxrobots waits only 10 ms for the first one and then fails with an obscure AttributeError."""
    import can

    try:
        bus = can.Bus(interface="socketcan", channel=channel)
    except OSError as exc:
        raise RuntimeError(f"{channel} not available ({exc.strerror or exc}): USB-to-CAN adapter plugged in and "
                           f"brought up (Robot tab: Bring up CAN)?") from None
    try:
        n, t0 = 0, time.monotonic()
        while time.monotonic() - t0 < timeout_s and n < 5:
            if bus.recv(0.2) is not None:
                n += 1
        return n
    finally:
        bus.shutdown()


def connect():
    if robot_heard() == 0:
        raise RuntimeError("no messages from the robot on can0 within 3 s: is the robot switched on and its CAN "
                           "cable connected to this laptop's USB-to-CAN adapter? (ip -details -statistics link "
                           "show can0: RX packets must grow, state ERROR-ACTIVE, bitrate 500000)")
    import wrapt

    # pyagxrobots needs an older wrapt: the same fix as robot/scout_line.py
    if not hasattr(wrapt, "wrappers"):
        import wrapt.decorators
        wrapt.wrappers = sys.modules.get("wrapt.decorators") or sys.modules.get("wrapt")
    wrapt.wrappers.transient_function_wrapper = wrapt.transient_function_wrapper
    import pyagxrobots

    bot = pyagxrobots.pysdkugv.ScoutMiniBase()
    bot.EnableCAN()
    return bot


def emit(**msg):
    print(json.dumps(msg), flush=True)


def measured(bot):
    out = {}
    for key, getter in (("v_meas", "GetLinearVelocity"), ("w_meas", "GetAngularVelocity"),
                        ("battery_v", "GetBatteryVoltage")):
        try:
            value = float(getattr(bot, getter)())
            out[key] = value if value == value else None
        except Exception:  # noqa: BLE001 - a missing reading must not stop the robot loop
            out[key] = None
    return out


def drive(bot, v, w, seconds, phase, cycle):
    """Send (v, w) at 10 Hz for seconds (or until stopped)."""
    t0 = time.monotonic()
    while not stop:
        t = time.monotonic() - t0
        if t >= seconds:
            return
        bot.SetMotionCommand(linear_vel=v, angular_vel=w)
        emit(t=time.time(), phase=phase, cycle=cycle, leg_t=round(t, 3), v_cmd=v, w_cmd=w, **measured(bot))
        time.sleep(PERIOD_S)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--speed", type=float, default=0.15, help="m/s (<= 0.3)")
    ap.add_argument("--leg", type=float, default=12.0, help="seconds per direction")
    ap.add_argument("--pause", type=float, default=1.0, help="seconds stopped at each end")
    ap.add_argument("--cycles", type=int, default=100, help="0: until stopped")
    ap.add_argument("--turn", type=float, default=0.0, help="rad/s while going forward (arc); 0: straight line")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    speed = max(0.0, min(a.speed, MAX_SPEED))
    turn = max(-MAX_TURN, min(a.turn, MAX_TURN))
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    bot = None
    try:
        emit(t=time.time(), phase="connecting", cycle=0)
        bot = DryRun() if a.dry_run else connect()
        emit(t=time.time(), phase="connected", cycle=0, dry_run=a.dry_run, **measured(bot))
        cycle = 0
        while not stop and (a.cycles <= 0 or cycle < a.cycles):
            cycle += 1
            drive(bot, speed, turn, a.leg, "forward", cycle)
            drive(bot, 0.0, 0.0, a.pause, "pause_end", cycle)
            drive(bot, -speed, -turn, a.leg, "backward", cycle)  # retraces the forward path
            drive(bot, 0.0, 0.0, a.pause, "pause_start", cycle)
        emit(t=time.time(), phase="stopped" if stop else "done", cycle=cycle)
        return 0
    except Exception as exc:  # noqa: BLE001
        emit(t=time.time(), phase="error", cycle=0, error=f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        if bot is not None:  # the robot must never keep moving
            for _ in range(5):
                try:
                    bot.SetMotionCommand(linear_vel=0.0, angular_vel=0.0)
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.1)
            del bot
            gc.collect()  # releases the SocketCAN socket at once (as scout_line.py)
        emit(t=time.time(), phase="released", cycle=0)


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    # pyagxrobots starts a CAN receive thread that never ends (not a daemon): a normal exit would wait for it
    # forever. The robot is stopped and the socket released by now, so leave without waiting.
    os._exit(code)
