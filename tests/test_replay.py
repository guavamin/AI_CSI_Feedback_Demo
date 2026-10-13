"""Native replay (OAI C Transformer code) and the live replay worker, on real OTA dumps of run 02."""

from __future__ import annotations

import csv
import shutil
import time
from pathlib import Path

import numpy as np
import pytest

from common.oai.config import load_config
from common.replay.engine import ReplayEngine, beam_cosine, codebook_beam, read_h_dump
from common.replay.native import latent_codes
from common.replay.worker import ReplayWorker, engine_from_config
from common.style import REPO

UE_DIR = (REPO.parent / "InterDigital_Interoperability_Implementation/OTA_Experiment_20260922/"
          "ota_experiments_subband_modelB/02_packed_1to1_subband_r1/csi/ue")
CFG = load_config(REPO / "configs/demo_usrp.yaml")
pytestmark = pytest.mark.skipif(not (UE_DIR.is_dir() and Path(CFG.oai.build_dir).is_dir()),
                                reason="needs the OTA run-02 recordings and the OAI gui build")


@pytest.fixture(scope="module")
def engine() -> ReplayEngine:
    return engine_from_config(CFG)()


def dumps(n):
    return sorted((UE_DIR / "csi_rs_channels").glob("H_*.bin"))[1000:1000 + n]


def test_latent_codes_wire_order():
    assert latent_codes(bytes([0b11100100, 0, 0, 0, 0, 0b11000000]))[:4].tolist() == [0, 1, 2, 3]
    assert latent_codes(bytes([0, 0, 0, 0, 0, 0b11000000]))[23] == 3


def test_engine_one_report(engine):
    res = engine.process(read_h_dump(dumps(1)[0]), i2=2)
    assert len(res.payload) == 6 and res.codes.shape == (24,) and res.codes.max() <= 3
    assert res.truth.shape == (2, 2, 612) and set(res.recon) == {"same", "cross"}
    for key in ("legacy", "same", "cross"):
        assert res.beam_cos[key].shape == (13,) and np.all((res.beam_cos[key] >= 0) & (res.beam_cos[key] <= 1 + 1e-9))
    assert not np.allclose(res.recon["same"], res.recon["cross"])  # really two decoders
    assert engine.crc["same"] == engine.crc["encoder"] != engine.crc["cross"]


def test_engine_is_deterministic_and_slot_order_free(engine):
    d = read_h_dump(dumps(1)[0])
    a = engine.process(d, 1)
    engine.tf.use(1)  # leave the library on the other slot: process() must not depend on it
    b = engine.process(d, 1)
    assert a.payload == b.payload and np.array_equal(a.recon["cross"], b.recon["cross"])


def test_beam_cosine_basics():
    v = np.stack([codebook_beam(k % 4) for k in range(13)])
    assert np.allclose(beam_cosine(v, v), 1.0)
    assert np.allclose(beam_cosine(v[:1].repeat(13, 0), codebook_beam(2))[0], 0.0, atol=1e-12)


def test_truncated_dump_is_ignored(tmp_path):
    src = dumps(1)[0]
    bad = tmp_path / src.name
    bad.write_bytes(src.read_bytes()[:5000])
    assert read_h_dump(bad) is None


def test_worker_follows_newest_report(tmp_path):
    root = tmp_path / "rec"
    (root / "csi_rs_channels").mkdir(parents=True)
    rows = {r["H_bin_path"].rsplit("/", 1)[-1]: r for r in csv.DictReader(open(UE_DIR / "csi_reports.csv"))}
    files = dumps(3)
    with open(root / "csi_reports.csv", "w") as f:
        f.write("timestamp_utc_us,frame,slot,H_bin_path,rsrp_dBm,ri,i1_0,i1_1,i1_2,i2,cqi,sinr_dB\n")
        for p in files:
            r = rows[p.name]
            f.write(f'{r["timestamp_utc_us"]},{r["frame"]},{r["slot"]},"{root}/csi_rs_channels/{p.name}",'
                    f'{r["rsrp_dBm"]},{r["ri"]},0,0,0,{r["i2"]},{r["cqi"]},{r["sinr_dB"]}\n')
    w = ReplayWorker(lambda: str(root), engine_from_config(CFG), period_s=0.05)
    w.start()
    try:
        for k, p in enumerate(files):
            shutil.copy(p, root / "csi_rs_channels" / p.name)
            t0 = time.monotonic()
            while w.view().n_reports < k + 1 and time.monotonic() - t0 < 10:
                time.sleep(0.02)
            v = w.view()
            assert v.ready and v.n_reports == k + 1, v.error
            assert v.last.i2 == int(rows[p.name]["i2"]) and v.hist_cqi[-1] == int(rows[p.name]["cqi"])
        assert v.hist_truth.shape == (3, 612) and v.hist_codes.shape == (3, 24)
        assert set(v.beam_cos) == {"legacy", "same", "cross"} and len(v.beam_cos["same"]) == 3 * 13
        assert v.ms_per_report < 200
    finally:
        w.stop()


def test_worker_skips_empty_estimates(tmp_path):
    root = tmp_path / "rec"
    (root / "csi_rs_channels").mkdir(parents=True)
    src = dumps(1)[0]
    raw = bytearray(src.read_bytes())
    raw[28:] = bytes(len(raw) - 28)  # header kept, channel all zero (UE shutting down)
    (root / "csi_rs_channels" / src.name).write_bytes(bytes(raw))
    w = ReplayWorker(lambda: str(root), engine_from_config(CFG), period_s=0.05)
    w.start()
    try:
        time.sleep(1.0)
        assert w.view().ready and w.view().n_reports == 0
    finally:
        w.stop()
