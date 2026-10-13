"""Are the UE's two receive paths phase-locked? Measures the RX1-vs-RX0 phase rotation from channel dumps.

    .venv/bin/python -m tools.rx_phase_check [folder with H_*.bin]    # default: the running GUI session
    .venv/bin/python -m tools.rx_phase_check --watch [folder]         # refresh every 2 s (Ctrl+C to stop)

Uses the newest ~60 reports. Both receive paths locked to the same reference: the relative phase stays put
(drift of a few degrees per report at most, mostly from movement). Two oscillators that are not locked
together: the phase turns steadily, e.g. +49 deg per 80 ms report = 1.7 Hz. Precoding does not depend on
this phase, the AI encoder does (it scrambles the latent).
"""

import argparse
import time
from pathlib import Path

import numpy as np

from common.replay.engine import read_h_dump, truth_cfr

SHM = Path("/dev/shm/ai-csi-feedback-demo")


def latest_folder() -> Path:
    runs = sorted(SHM.glob("*/csi_rs_channels"), key=lambda p: p.stat().st_mtime)
    if not runs:
        raise SystemExit("no running GUI session found: give the folder that holds the H_*.bin files")
    return runs[-1]


def measure(folder: Path, n: int = 60) -> str:
    files = sorted(folder.glob("H_*.bin"), key=lambda p: int(p.name.split("_")[1]))[-n - 1:-1]
    rel, ts = [], []
    for f in files:
        d = read_h_dump(f)
        if d is None or int(np.abs(d.iq).max()) < 4:
            continue
        h = truth_cfr(d.iq)
        rel.append(np.angle(np.sum(h[1] * h[0].conj())))  # RX1 vs RX0, summed over TX ports and subcarriers
        ts.append(d.timestamp_utc_us / 1e6)
    if len(rel) < 5:
        return f"{folder}: not enough reports yet ({len(rel)})"
    ph, ts = np.unwrap(np.array(rel)), np.array(ts)
    rate_hz = np.polyfit(ts, ph, 1)[0] / (2 * np.pi)  # steady rotation, from a straight-line fit over time
    per_report = np.degrees(np.median(np.abs(np.diff(ph))))
    verdict = ("LOCKED (relative phase stays put)" if abs(rate_hz) < 0.05 and per_report < 10
               else "NOT LOCKED: the two receive paths rotate against each other")
    return (f"{len(rel)} reports: RX1-vs-RX0 phase turns {per_report:5.1f} deg per report, "
            f"steady rotation {rate_hz:+.3f} Hz  ->  {verdict}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("folder", nargs="?", type=Path)
    ap.add_argument("--watch", action="store_true")
    args = ap.parse_args()
    folder = args.folder or latest_folder()
    if not args.watch:
        print(measure(folder))
        return 0
    try:
        while True:
            print(time.strftime("%H:%M:%S"), measure(folder), flush=True)
            time.sleep(2)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
