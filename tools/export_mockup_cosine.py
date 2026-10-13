"""Per-sub-band beam cosine similarity of all three scenarios on the SAME channels (all of OTA run 02), for the
GUI mockup's cosine CDF. Read-only on the pipeline caches.

beam cosine = |v^H w| / (|v| |w|), v = dominant eigenvector of the TRUE channel in a sub-band (PRG),
w = the precoder the gNB gets: AI = dominant eigenvector of the reconstructed channel, legacy = the Type-I
codeword of the PMI the UE reported (wideband, repeated over sub-bands). Same definition as csi_metrics.sgcs
without the square. Also stores the AI channel cosine similarity (per report) for reference.
"""

import csv
import json
import sys
from pathlib import Path

import numpy as np
import torch

IMPL = Path(__file__).resolve().parents[2] / "InterDigital_Interoperability_Implementation"
sys.path.insert(0, str(IMPL))
from interoperability import csi_metrics as cm  # noqa: E402
from interoperability import evaluate_ota_channels as ev  # noqa: E402
from interoperability import run_capture_diagnostics as diag  # noqa: E402

OUT = Path(sys.argv[1])
ev.PACKED_RUN = ev.STUDY / "runs/main/curriculum/packed" / ev.MODEL_SETS["B"][0]
ev.PLACEMENT = ev.STUDY / "analyses" / ev.MODEL_SETS["B"][1] / "placement_artifacts.pt"
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
packed = diag.load_pair(ev.PACKED_RUN, device)
gmaps = ev.gnb_merged_maps()
CACHE = ev.STUDY / "ota_cache_subband_modelB"
OTA = IMPL / "OTA_Experiment_20260922/ota_experiments_subband_modelB"
RUN = "02_packed_1to1_subband_r1"
diag.CAPTURE_CACHES[RUN] = CACHE / RUN
stamps_all = np.array(json.loads((CACHE / RUN / "cache_manifest.json").read_text())["timestamps_utc_us"], dtype=np.int64)
reports = {int(r["timestamp_utc_us"]): int(r["i2"]) for r in csv.DictReader(open(OTA / RUN / "csi/ue/csi_reports.csv"))
           if r["timestamp_utc_us"] != "timestamp_utc_us"}


def beam_cos(true_cfr, beams):
    """[N, G] |v^H w| per sub-band."""
    v = cm.dominant_vectors(cm.band_covariances(true_cfr, "subband"))
    if beams.ndim == 2:
        beams = np.repeat(beams[:, None, :], v.shape[1], axis=1)
    inner = np.abs(np.einsum("ngt,ngt->ng", v.conj(), beams))
    return inner / np.maximum(np.linalg.norm(v, axis=-1) * np.linalg.norm(beams, axis=-1), 1e-30)


acc = {k: [] for k in ("same_beamcos", "cross_beamcos", "legacy_beamcos", "same_chcos", "cross_chcos")}
n_all = len(stamps_all)
for a in range(0, n_all, 500):
    b = min(n_all, a + 500)
    native, target = diag.capture_rows(packed, RUN, a, b)
    true_cfr = ev.to_cfr(np.asarray(target))
    lat1 = diag._encode(packed.models[1], (native * packed.multiplier[1]).numpy(), 256, device)
    with torch.no_grad():
        mapped = gmaps[1].cpu()(diag.hard_quantize(lat1).cpu())
    preds = {"same": packed.to_common(1, diag.decode(packed.models[1], diag.hard_quantize(lat1), device)),
             "cross": packed.to_common(2, diag.decode(packed.models[2], mapped, device))}
    for key, pred in preds.items():
        pred_cfr = ev.to_cfr(np.asarray(pred))
        acc[f"{key}_beamcos"].append(beam_cos(true_cfr, cm.dominant_vectors(cm.band_covariances(pred_cfr, "subband"))))
        acc[f"{key}_chcos"].append(cm.cosine_similarity(true_cfr, pred_cfr))
    acc["legacy_beamcos"].append(beam_cos(true_cfr, ev.codebook_beams(np.array([reports[int(s)] for s in stamps_all[a:b]]))))
    print(f"rows {a}-{b}", flush=True)
out = {k: np.concatenate(v).astype(np.float32) for k, v in acc.items()}
for k, v in out.items():
    print(k, v.shape, "mean", round(float(v.mean()), 4), "median", round(float(np.median(v)), 4), "p10", round(float(np.percentile(v, 10)), 4))
np.savez_compressed(OUT, **out)
print("wrote", OUT)
