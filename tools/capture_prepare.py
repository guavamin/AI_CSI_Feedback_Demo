"""Check a recorded UE capture and turn it into a training-ready dataset (also after moving it to another laptop).

    .venv/bin/python -m tools.capture_prepare check <run>/csi/ue
    .venv/bin/python -m tools.capture_prepare make  <run>/csi/ue [--auto] [--exclude 8430:8800 ...] [--name ue_train]

check: report rows vs channel files, duration, signal (SINR / RSRP / reports without a measurement) and the motion
       check: how much the measured channel changes over 1 s (1 - correlation of |H|; static ~1e-4, a moving robot
       ~0.1). Stretches of at least --min-still-s seconds that are much stiller than that are listed as stops
       (the robot's short pauses at the route ends are shorter and are not listed).
make:  <run>/csi/<name>/ = csi_reports.csv without the excluded rows, with every H_bin_path pointing to the files
       in THIS folder (paths are absolute: a capture copied from the other laptop works after this), and
       csi_rs_channels/ with links to the original files. --auto excludes the stops found by check (+-2 s).
       The recording itself is never changed. Train on <run>/csi/<name>.
"""

from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path

import numpy as np

from common.replay.engine import read_h_dump, truth_cfr

REPORT_S = 0.08


def load_rows(ue: Path) -> tuple[list[str], list[list[str]]]:
    with open(ue / "csi_reports.csv", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = [r for r in reader if r and r[0] != "timestamp_utc_us"]  # a UE restart repeats the header
    rows.sort(key=lambda r: int(r[0]))
    return header, rows


def motion(ue: Path, header, rows) -> np.ndarray:
    """1 - corr(|H_t|, |H_t-1s|), smoothed over 1 s; NaN where a file is missing."""
    i = header.index("H_bin_path")
    mags = []
    for r in rows:
        d = read_h_dump(ue / "csi_rs_channels" / Path(r[i]).name)
        if d is None:
            mags.append(None)
            continue
        h = np.abs(truth_cfr(d.iq)).astype(np.float32).ravel()
        mags.append(h / (np.linalg.norm(h) + 1e-9))
    lag = int(round(1.0 / REPORT_S))
    out = np.full(len(rows), np.nan)
    for k in range(lag, len(rows)):
        if mags[k] is not None and mags[k - lag] is not None:
            out[k] = 1.0 - float(np.dot(mags[k], mags[k - lag]))
    filled = np.where(np.isnan(out), np.nanmedian(out), out)
    return np.convolve(filled, np.ones(lag) / lag, mode="same")


def stops(change: np.ndarray, min_still_s: float, factor: float = 0.3, merge_s: float = 10.0) -> list[tuple[int, int]]:
    """Row ranges [a, b) much stiller than the run's typical motion: still stretches closer than merge_s are one
    stop (the robot moving briefly in between), kept if the stop lasts at least min_still_s."""
    thr = factor * float(np.median(change))
    low = change < thr
    runs, k, n = [], 0, len(low)
    while k < n:
        if low[k]:
            j = k
            while j < n and low[j]:
                j += 1
            runs.append([k, j])
            k = j
        else:
            k += 1
    merged = []
    for a, b in runs:
        if merged and (a - merged[-1][1]) * REPORT_S < merge_s:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    # a stop is still as a whole: chained route-end pauses (short pauses ~13 s apart) are not
    return [(a, b) for a, b in merged if (b - a) * REPORT_S >= min_still_s and np.median(change[a:b]) < thr]


def check(ue: Path, min_still_s: float) -> list[tuple[int, int]]:
    header, rows = load_rows(ue)
    files = {p.name for p in (ue / "csi_rs_channels").glob("H_*.bin")}
    i = header.index("H_bin_path")
    missing = sum(1 for r in rows if Path(r[i]).name not in files)
    t = np.array([int(r[0]) for r in rows]) / 1e6
    gaps = np.diff(t)
    print(f"{ue}\n  {len(rows)} report rows, {len(files)} channel files, {missing} rows without a file")
    big = gaps > 1.0
    print(f"  {(t[-1] - t[0]) / 60:.1f} min recorded; gaps over 1 s (UE restarts): {int(big.sum())}"
          + (f", longest {gaps.max():.0f} s" if big.any() else ""))
    sinr = np.array([int(r[header.index("sinr_dB")]) for r in rows])
    rsrp = np.array([int(r[header.index("rsrp_dBm")]) for r in rows])
    cqi0 = np.array([r[header.index("cqi")] == "0" for r in rows])
    print(f"  SINR median {np.median(sinr[~cqi0]):.0f} dB (5% {np.percentile(sinr[~cqi0], 5):.0f}), RSRP median "
          f"{np.median(rsrp):.0f} dBm (min {rsrp.min()}), reports without a measurement (CQI 0): {int(cqi0.sum())}")
    change = motion(ue, header, rows)
    found = stops(change, min_still_s)
    print(f"  motion: channel change over 1 s, median {np.median(change):.2e} (static ~1e-4, moving robot ~1e-1)")
    if not found:
        print(f"  no stops of {min_still_s:.0f} s or longer")
    for a, b in found:
        print(f"  STOP rows {a}-{b - 1}: {(b - a) * REPORT_S:.0f} s at {(t[a] - t[0]) / 60:.1f} min "
              f"(change {np.median(change[a:b]):.1e})")
    if missing:
        print("  rows without a file: the copy is incomplete")
    return found


def make(ue: Path, name: str, excludes: list[tuple[int, int]]) -> None:
    header, rows = load_rows(ue)
    i = header.index("H_bin_path")
    drop = np.zeros(len(rows), bool)
    for a, b in excludes:
        drop[max(a, 0):min(b, len(rows))] = True
    out = ue.parent / name
    if out.exists():
        raise SystemExit(f"{out} exists: remove it or choose another --name")
    (out / "csi_rs_channels").mkdir(parents=True)
    channels = (ue / "csi_rs_channels").resolve()
    kept = 0
    with open(out / "csi_reports.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r, d in zip(rows, drop):
            if d:
                continue
            src = channels / Path(r[i]).name
            if not src.exists():
                continue  # never train on a row without its channel
            r = list(r)
            r[i] = str(src)
            w.writerow(r)
            os.symlink(src, out / "csi_rs_channels" / src.name)
            kept += 1
    (out / "README.txt").write_text(
        f"Training view of {ue}: {kept} of {len(rows)} rows; excluded rows (sorted by time, end exclusive): "
        f"{excludes or 'none'}. H_bin_path points to {channels}.\n")
    print(f"{out}: {kept} rows (of {len(rows)}), excluded {[f'{a}-{b - 1}' for a, b in excludes] or 'none'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=("check", "make"))
    ap.add_argument("ue", type=Path, help="<run>/csi/ue")
    ap.add_argument("--auto", action="store_true", help="make: exclude the stops found by check (+-2 s)")
    ap.add_argument("--exclude", action="append", default=[], help="make: rows a:b to leave out (repeatable)")
    ap.add_argument("--name", default="ue_train")
    ap.add_argument("--min-still-s", type=float, default=5.0)
    a = ap.parse_args()
    ue = a.ue.resolve()
    if a.action == "check":
        check(ue, a.min_still_s)
        return 0
    excludes = [tuple(int(x) for x in e.split(":")) for e in a.exclude]
    if a.auto:
        margin = int(round(2.0 / REPORT_S))
        excludes += [(s - margin, e + margin) for s, e in check(ue, a.min_still_s)]
    make(ue, a.name, sorted(excludes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
