"""Real per-report data for the station mockups (read-only on the pipeline).

  latent_codes [120, 24]: model-1 encoder output of OTA run 02, rows 1000..1119 (the same reports as
                          mockup_channels.npz), 2-bit levels 0..3 -> the 48 latent bits on PUCCH
  <run>/ue_reports.csv  : timestamp, rsrp_dBm, ri, i2 (PMI), cqi, sinr_dB as logged by the UE
Run with the InterDigital_Interoperability_Implementation environment (ID_project/bin/python).
"""

import csv
import sys
from pathlib import Path

import numpy as np
import torch

IMPL = Path(__file__).resolve().parents[2] / "InterDigital_Interoperability_Implementation"
sys.path.insert(0, str(IMPL))
from interoperability import evaluate_ota_channels as ev  # noqa: E402
from interoperability import run_capture_diagnostics as diag  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "data/mockups"
START, N = 1000, 120
RUN = "02_packed_1to1_subband_r1"
OTA = IMPL / "OTA_Experiment_20260922/ota_experiments_subband_modelB"
ev.PACKED_RUN = ev.STUDY / "runs/main/curriculum/packed" / ev.MODEL_SETS["B"][0]
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
packed = diag.load_pair(ev.PACKED_RUN, device)
diag.CAPTURE_CACHES[RUN] = ev.STUDY / "ota_cache_subband_modelB" / RUN
native, _ = diag.capture_rows(packed, RUN, START, START + N)
lat = diag._encode(packed.models[1], (native * packed.multiplier[1]).numpy(), 256, device)
codes = torch.round(torch.as_tensor(lat).clamp(0.0, 1.0) * 3.0).to(torch.uint8).cpu().numpy().reshape(N, -1)
np.savez_compressed(OUT / "mockup_latents.npz", latent_codes=codes)
print("latent codes", codes.shape, "levels", np.unique(codes))
for run in ("01_legacy_start_r1", RUN, "06_interop_1to2_gnb_subband_r1"):
    keep = ("timestamp_utc_us", "rsrp_dBm", "ri", "i2", "cqi", "sinr_dB")
    rows = [r for r in csv.DictReader(open(OTA / run / "csi/ue/csi_reports.csv")) if r["timestamp_utc_us"] != "timestamp_utc_us"]
    with open(OUT / "runs" / run / "ue_reports.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keep)
        w.writeheader()
        w.writerows({k: r[k] for k in keep} for r in rows)
    print(run, len(rows), "UE reports")
