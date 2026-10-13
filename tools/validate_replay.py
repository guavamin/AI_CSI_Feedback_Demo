"""Check the native replay (OAI C code) against the PyTorch evaluation pipeline on the same OTA reports.

    .venv/bin/python -m tools.validate_replay <run02 csi/ue dir> <cache_manifest.json> [start] [n]

Reference: data/mockups/mockup_channels.npz, exported by tools/export_mockup_channels.py with
interoperability/evaluate_ota_channels.py (model set B) for run 02 rows 1000..1119.
"""

import csv
import json
import sys
from pathlib import Path

import numpy as np

from common.oai.config import load_config
from common.replay.engine import ReplayEngine, read_h_dump
from common.style import MOCKUP_DATA


def main() -> int:
    ue_dir, manifest = Path(sys.argv[1]), Path(sys.argv[2])
    start = int(sys.argv[3]) if len(sys.argv) > 3 else 1000
    n = int(sys.argv[4]) if len(sys.argv) > 4 else 120
    cfg = load_config("configs/demo_usrp.yaml")
    args = cfg.oai.gnb.base_args
    engine = ReplayEngine(Path(cfg.oai.build_dir).parents[2], Path(cfg.oai.build_dir),
                          ue_model=Path(cfg.oai.ue.base_args[cfg.oai.ue.base_args.index("--ai-fb-model-path") + 1]),
                          decoders={"same": Path(args[args.index("--ai-fb-model-path") + 1]),
                                    "cross": Path(args[args.index("--ai-fb-live-alt-model-path") + 1])})
    print("model CRCs:", {k: f"{v:08x}" for k, v in engine.crc.items()})
    stamps = json.loads(manifest.read_text())["timestamps_utc_us"][start:start + n]
    i2 = {int(r["timestamp_utc_us"]): int(r["i2"]) for r in csv.DictReader(open(ue_dir / "csi_reports.csv"))}
    files = {int(p.name.split("_")[1]): p for p in (ue_dir / "csi_rs_channels").glob("H_*.bin")}
    res = [engine.process(read_h_dump(files[int(t)]), i2[int(t)]) for t in stamps]
    ref = dict(np.load(MOCKUP_DATA / "mockup_channels.npz"))
    ok = True
    # ground truth: same channel up to one real scale (the cache is normalized)
    t = np.stack([r.truth for r in res])
    scale = np.vdot(t.ravel(), ref["true_cfr"].ravel()).real / np.vdot(t.ravel(), t.ravel()).real
    rel = np.linalg.norm(scale * t - ref["true_cfr"]) / np.linalg.norm(ref["true_cfr"])
    print(f"ground truth vs pipeline: relative error {rel:.2e} (after one global scale {scale:.3e})")
    ok &= rel < 1e-3
    for key in ("same", "cross"):
        cos = np.array([r.chan_cos[key] for r in res])
        sg = np.array([np.mean(r.beam_cos[key] ** 2) for r in res])
        d_cos = np.abs(cos - ref[f"{key}_cos"]).max()
        d_sg = np.abs(sg - ref[f"{key}_sgcs"]).max()
        print(f"{key:6s} channel cosine: replay {cos.mean():.4f} pipeline {ref[f'{key}_cos'].mean():.4f} "
              f"max |diff| {d_cos:.1e} | SGCS replay {sg.mean():.4f} pipeline {ref[f'{key}_sgcs'].mean():.4f} "
              f"max |diff| {d_sg:.1e}")
        ok &= d_cos < 1e-3 and d_sg < 1e-3
    sg = np.array([np.mean(r.beam_cos["legacy"] ** 2) for r in res])
    d = np.abs(sg - ref["legacy_sgcs"]).max()
    print(f"legacy SGCS: replay {sg.mean():.4f} pipeline {ref['legacy_sgcs'].mean():.4f} max |diff| {d:.1e}")
    ok &= d < 1e-6
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
