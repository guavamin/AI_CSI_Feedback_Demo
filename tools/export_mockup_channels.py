"""Export real OTA channels + model-set-B reconstructions for the GUI mockup (read-only on the pipeline).

Reuses interoperability/evaluate_ota_channels.py internals: same caches, same routes.
  same   : run 02, model 1 encoder -> model 1 decoder        ("packed_1_to_1")
  cross  : run 06, model 1 encoder -> gNB-merged map -> model 2 decoder  ("gnb_merged_1_to_2")
  legacy : run 01, only the codebook beam from the PMI the UE reported
Output (npz): |H| of every RX-TX link over the 612 occupied subcarriers for 120 consecutive reports,
per-report channel cosine similarity (AI) and sub-band beam alignment SGCS (all three).
"""

import csv
import json
import sys
from pathlib import Path

import numpy as np
import torch

IMPL = Path(__file__).resolve().parents[2] / "InterDigital_Interoperability_Implementation"
sys.path.insert(0, str(IMPL))
from interoperability import csi_metrics  # noqa: E402
from interoperability import evaluate_ota_channels as ev  # noqa: E402
from interoperability import run_capture_diagnostics as diag  # noqa: E402

OUT = Path(sys.argv[1])
START, N = 1000, 120
ev.PACKED_RUN = ev.STUDY / "runs/main/curriculum/packed" / ev.MODEL_SETS["B"][0]
ev.PLACEMENT = ev.STUDY / "analyses" / ev.MODEL_SETS["B"][1] / "placement_artifacts.pt"
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
packed = diag.load_pair(ev.PACKED_RUN, device)
gmaps = ev.gnb_merged_maps()
CACHE = ev.STUDY / "ota_cache_subband_modelB"
RUNS = {"legacy": "01_legacy_start_r1", "same": "02_packed_1to1_subband_r1", "cross": "06_interop_1to2_gnb_subband_r1"}
OTA = IMPL / "OTA_Experiment_20260922/ota_experiments_subband_modelB"

# Paired: every route scored on the SAME channel reports (run 02, rows START..START+N), so the three
# scenarios are compared on identical channels (comparing runs mixes different channels).
RUN = "02_packed_1to1_subband_r1"
diag.CAPTURE_CACHES[RUN] = CACHE / RUN
native, target = diag.capture_rows(packed, RUN, START, START + N)
true_cfr = ev.to_cfr(np.asarray(target))
out = {"true_mag": np.abs(true_cfr).astype(np.float32)}
lat1 = diag._encode(packed.models[1], (native * packed.multiplier[1]).numpy(), 256, device)
preds = {"same": packed.to_common(1, diag.decode(packed.models[1], diag.hard_quantize(lat1), device))}
with torch.no_grad():
    mapped = gmaps[1].cpu()(diag.hard_quantize(lat1).cpu())
preds["cross"] = packed.to_common(2, diag.decode(packed.models[2], mapped, device))
out["true_cfr"] = true_cfr.astype(np.complex64)
for key, pred in preds.items():
    pred_cfr = ev.to_cfr(np.asarray(pred))
    out[f"{key}_cfr"] = pred_cfr.astype(np.complex64)
    err = np.sum(np.abs(pred_cfr - true_cfr) ** 2, axis=(1, 2, 3)) / np.sum(np.abs(true_cfr) ** 2, axis=(1, 2, 3))
    out[f"{key}_nmse_db"] = (10 * np.log10(err)).astype(np.float32)
    print(key, "NMSE (block, global)", round(float(10 * np.log10(np.sum(np.abs(pred_cfr - true_cfr) ** 2) / np.sum(np.abs(true_cfr) ** 2))), 2), "dB",
          " pipeline metric", round(float(ev._metrics(np.asarray(target), np.asarray(pred), ev.DOMAIN)["global_nmse_db"]), 2), "dB")
    out[f"{key}_pred_mag"] = np.abs(pred_cfr).astype(np.float32)
    out[f"{key}_cos"] = csi_metrics.cosine_similarity(true_cfr, pred_cfr).astype(np.float32)
    out[f"{key}_sgcs"] = csi_metrics.sgcs(true_cfr, pred_cfr, "subband").astype(np.float32)
stamps = np.array(json.loads((CACHE / RUN / "cache_manifest.json").read_text())["timestamps_utc_us"], dtype=np.int64)[START:START + N]
reports = {int(r["timestamp_utc_us"]): int(r["i2"]) for r in csv.DictReader(open(OTA / RUN / "csi/ue/csi_reports.csv"))
           if r["timestamp_utc_us"] != "timestamp_utc_us"}
out["legacy_sgcs"] = csi_metrics.sgcs_of_beams(true_cfr, ev.codebook_beams(np.array([reports[int(s)] for s in stamps])), "subband").astype(np.float32)
for k in ("same_cos", "cross_cos", "same_sgcs", "cross_sgcs", "legacy_sgcs"):
    print(k, round(float(out[k].mean()), 4))
np.savez_compressed(OUT, **out)
print("wrote", OUT)
