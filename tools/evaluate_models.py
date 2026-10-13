"""Score a model set on a recorded capture, the way the demo replays it (OAI's own model code).

    .venv/bin/python -m tools.evaluate_models <run>/csi/ue <models dir> [--rows 15384:18753] [--every 5]

<models dir> holds model1_packed_p16.bin and model2_decoder_for_model1_source.bin (an OAI
models/ai_fb_transformer/<set> folder). The UE encodes with model 1; Same vendor decodes with model 1,
Cross vendor with model 2's decoder carrying the gNB-side translation. For every scored report:
beam cosine and beam gain (dB, 0 = best rank-1 beam) per 4-PRB sub-band for Legacy (the UE's own PMI),
the best codebook beam, Same and Cross vendor, and NMSE of the two reconstructions. Use the rows of the
training capture's final-test block, or a separate capture, never training rows.
"""

import argparse
import csv
from pathlib import Path

import numpy as np

from common.oai.config import load_config
from common.replay.engine import ReplayEngine, beam_cosine, codebook_beam, dominant_beams, read_h_dump
from common.style import REPO
from tools.analyze_session import beam_gain_db


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("capture", type=Path)
    ap.add_argument("models", type=Path)
    ap.add_argument("--rows", default="", help="a:b row range of csi_reports.csv (default: all)")
    ap.add_argument("--every", type=int, default=1, help="score every k-th report")
    args = ap.parse_args()
    cfg = load_config(REPO / "configs/demo_usrp.yaml")
    build = Path(cfg.oai.build_dir)
    m = args.models.resolve()
    engine = ReplayEngine(build.parents[2], build, ue_model=m / "model1_packed_p16.bin",
                          decoders={"same": m / "model1_packed_p16.bin",
                                    "cross": m / "model2_decoder_for_model1_source.bin"})
    rows = [r for r in csv.DictReader(open(args.capture / "csi_reports.csv")) if r.get("H_bin_path")]
    if args.rows:
        a, b = (int(x) if x else None for x in args.rows.split(":"))
        rows = rows[a:b]
    rows = rows[:: args.every]
    cos, gain, nmse = {}, {}, {"same": [], "cross": []}
    for r in rows:
        d = read_h_dump(args.capture / "csi_rs_channels" / Path(r["H_bin_path"]).name)
        if d is None:
            continue
        res = engine.process(d, int(r["i2"]))
        v = dominant_beams(res.truth)
        best = max(range(4), key=lambda i: beam_gain_db(res.truth, codebook_beam(i)).mean())
        for name, w in (("legacy (reported PMI)", codebook_beam(int(r["i2"]))), ("best codebook beam", codebook_beam(best)),
                        ("same vendor", dominant_beams(res.recon["same"])),
                        ("cross vendor", dominant_beams(res.recon["cross"]))):
            cos.setdefault(name, []).append(beam_cosine(v, w).mean())
            gain.setdefault(name, []).append(beam_gain_db(res.truth, w).mean())
        for k in nmse:
            nmse[k].append(res.nmse_db[k])
    print(f"models {m.name}  on  {args.capture}  ({len(cos['same vendor'])} reports)")
    print(f"  {'scheme':24s} {'beam cosine':>12s} {'beam gain dB (0 = ideal)':>26s} {'NMSE dB':>9s}")
    for name in cos:
        key = name.split()[0]
        nm = f"{10 * np.log10(np.mean(10 ** (np.array(nmse[key]) / 10))):9.2f}" if key in nmse else " " * 9
        print(f"  {name:24s} {np.mean(cos[name]):12.4f} {np.mean(gain[name]):14.2f} (p10 {np.percentile(gain[name], 10):6.2f}) {nm}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
