"""Analyse a GUI session's replayed reports (data/runs/replay/<session>/).

    .venv/bin/python -m tools.analyze_session data/runs/replay/<session> [configs/demo_usrp.yaml]

For every scenario that was on air, scores all three schemes on the very same channel reports (paired):
  beam cosine |v^H w| per 4-PRB sub-band (what the GUI's CDF shows),
  beam gain: power the beam captures relative to the best rank-1 beam, per sub-band, in dB (0 dB = ideal;
             this is what precoding gain and throughput follow),
  the best codebook beam the UE could have reported (to separate "codebook too coarse" from "PMI wrong"),
  and the UE's PMI distribution.
"""

import csv
import sys
from collections import Counter
from pathlib import Path

import numpy as np

from common.oai.config import load_config
from common.replay.engine import band_covariances, beam_cosine, codebook_beam, dominant_beams, read_h_dump
from common.replay.worker import engine_from_config


def beam_gain_db(true_cfr, beams):
    """10 log10(w^H R w / lambda_max(R)) per sub-band, beams [13, 2] or [2]."""
    R = band_covariances(true_cfr)
    if beams.ndim == 1:
        beams = np.broadcast_to(beams, (R.shape[0], 2))
    w = beams / np.linalg.norm(beams, axis=-1, keepdims=True)
    captured = np.einsum("gt,gts,gs->g", w.conj(), R, w).real
    return 10 * np.log10(np.maximum(captured, 1e-30) / np.linalg.eigvalsh(R)[:, -1])


def main() -> int:
    folder = Path(sys.argv[1])
    cfg = load_config(sys.argv[2] if len(sys.argv) > 2 else "configs/demo_usrp.yaml")
    engine = engine_from_config(cfg)()
    rows = list(csv.DictReader(open(folder / "reports.csv")))
    acc: dict = {}
    for r in rows:
        on_air = r["on_air"] or "none"
        d = read_h_dump(folder / "dumps" / r["file"])
        if d is None or not r["i2"]:
            continue
        res = engine.process(d, int(r["i2"]))
        v = dominant_beams(res.truth)
        a = acc.setdefault(on_air, {"n": 0, "i2": Counter(), "cos": {}, "gain": {}})
        a["n"] += 1
        a["i2"][int(r["i2"])] += 1
        best_cb = max(range(4), key=lambda i: beam_gain_db(res.truth, codebook_beam(i)).mean())
        beams = {"legacy (reported PMI)": codebook_beam(int(r["i2"])), "best codebook beam": codebook_beam(best_cb),
                 "same vendor": dominant_beams(res.recon["same"]), "cross vendor": dominant_beams(res.recon["cross"])}
        for name, w in beams.items():
            a["cos"].setdefault(name, []).append(beam_cosine(v, w).mean())
            a["gain"].setdefault(name, []).append(beam_gain_db(res.truth, w).mean())
    for on_air, a in acc.items():
        print(f"\n=== on air: {on_air}  ({a['n']} reports; UE PMI i2: {dict(sorted(a['i2'].items()))})")
        print(f"  {'scheme':24s} {'beam cosine':>12s} {'beam gain (dB, 0 = ideal)':>28s}")
        for name in a["cos"]:
            c, g = np.array(a["cos"][name]), np.array(a["gain"][name])
            print(f"  {name:24s} {c.mean():12.4f} {g.mean():16.2f}  (p10 {np.percentile(g, 10):6.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
