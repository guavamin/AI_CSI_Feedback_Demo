"""Replay one UE CSI-RS channel report through all three scenarios.

For a recorded H dump (what the UE measured, i.e. the ground truth) this reproduces what OAI computes:
  - the UE's AI latent (model-1 encoder; bit-exact with the UE because it is the same C code),
  - the gNB's reconstruction with decoder 0 (Same vendor) and decoder 1 (Cross vendor),
  - and scores every scenario on this same channel:
      beam cosine |v^H w| per 4-PRB sub-band (13 sub-bands): v = best rank-1 beam of the true channel,
      w = the beam the gNB gets (AI: from the reconstruction; legacy: Type-I codeword of the UE's PMI i2);
      channel cosine (CsiNet rho) and NMSE for the two AI scenarios.
Definitions follow InterDigital_Interoperability_Implementation/interoperability/csi_metrics.py.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from common.replay.native import ACTIVE, RUNTIME_BINS, NativeTransformer, build_library, latent_codes

PRB_SC, N_PRB, PRG = 12, 51, 4
N_SUBBANDS = math.ceil(N_PRB / PRG)  # 13
_HDR = struct.Struct("<5iq")
# occupied subcarrier t (physical order: negative frequencies first) -> FFT bin of the 768-point grid
NATIVE_BIN = np.array([462 + t if t < 306 else t - 306 for t in range(ACTIVE)])


@dataclass
class HDump:
    frame: int
    slot: int
    timestamp_utc_us: int
    iq: np.ndarray  # int16 [rx][port][768][2]
    path: Path


def read_h_dump(path: Path) -> HDump | None:
    """OAI UE H_*.bin (csi_rx.c csi_record_write): 5 x int32 + int64 header, then int16 IQ [rx][port][bins]."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if len(raw) < _HDR.size:
        return None
    frame, slot, n_rx, n_ports, n_subc, ts = _HDR.unpack_from(raw, 0)
    need = _HDR.size + n_rx * n_ports * n_subc * 4
    if (n_rx, n_ports, n_subc) != (2, 2, RUNTIME_BINS) or len(raw) < need:
        return None
    iq = np.frombuffer(raw, dtype=np.int16, count=n_rx * n_ports * n_subc * 2, offset=_HDR.size)
    return HDump(frame, slot, ts, iq.reshape(n_rx, n_ports, n_subc, 2), Path(path))


def truth_cfr(iq: np.ndarray) -> np.ndarray:
    """int16 [rx][port][768][2] -> complex CFR [rx][tx][612] in the decoder's order and units."""
    h = iq[..., 0].astype(np.float64) + 1j * iq[..., 1].astype(np.float64)
    return h[:, :, NATIVE_BIN]


def band_covariances(cfr: np.ndarray) -> np.ndarray:
    """[rx, tx, 612] -> [13, tx, tx]: sum over each 4-PRB group's subcarriers and rx of h^H h."""
    per_sc = np.einsum("rtk,rsk->kts", cfr.conj(), cfr)
    return np.stack([per_sc[g * PRG * PRB_SC:min((g + 1) * PRG, N_PRB) * PRB_SC].sum(axis=0)
                     for g in range(N_SUBBANDS)])


def dominant_beams(cfr: np.ndarray) -> np.ndarray:
    return np.linalg.eigh(band_covariances(cfr))[1][..., -1]  # [13, tx]


def beam_cosine(true_beams: np.ndarray, beams: np.ndarray) -> np.ndarray:
    """|v^H w| / (|v| |w|) per sub-band; beams [13, tx] or one wideband beam [tx]."""
    if beams.ndim == 1:
        beams = np.broadcast_to(beams, true_beams.shape)
    inner = np.abs(np.einsum("gt,gt->g", true_beams.conj(), beams))
    return inner / np.maximum(np.linalg.norm(true_beams, axis=-1) * np.linalg.norm(beams, axis=-1), 1e-30)


def channel_cosine(true: np.ndarray, pred: np.ndarray) -> float:
    inner = np.abs(np.einsum("rtk,rtk->rk", true.conj(), pred))
    norms = np.linalg.norm(true, axis=1) * np.linalg.norm(pred, axis=1)
    return float((inner / np.maximum(norms, 1e-30)).mean())


def nmse_db(true: np.ndarray, pred: np.ndarray) -> float:
    return float(10 * np.log10(np.sum(np.abs(pred - true) ** 2) / max(np.sum(np.abs(true) ** 2), 1e-30)))


def codebook_beam(i2: int) -> np.ndarray:
    """2-port Type-I single-panel rank-1 codeword (1, j^i2)/sqrt(2)."""
    return np.array([1.0, 1j ** int(i2)]) / math.sqrt(2.0)


@dataclass
class ReplayResult:
    frame: int
    timestamp_utc_us: int
    payload: bytes
    codes: np.ndarray  # 24 two-bit latent codes
    truth: np.ndarray  # [rx, tx, 612]
    recon: dict[str, np.ndarray]  # same / cross -> [rx, tx, 612]
    beam_cos: dict[str, np.ndarray] = field(default_factory=dict)  # legacy / same / cross -> [13]
    chan_cos: dict[str, float] = field(default_factory=dict)  # same / cross
    nmse_db: dict[str, float] = field(default_factory=dict)
    i2: int | None = None


class ReplayEngine:
    """UE encoder (model 1) + the gNB's two decoders, all through OAI's C code. Use from one thread."""

    def __init__(self, oai_dir: Path, oai_build: Path, ue_model: Path, decoders: dict[str, Path]):
        self.tf = NativeTransformer(build_library(Path(oai_dir), Path(oai_build)))
        # slot 0: UE encoder model (it is also the Same-vendor decoder); slot 1: Cross-vendor decoder
        self.slots = {"encoder": 0}
        self.tf.load(0, Path(ue_model))
        for key, path in decoders.items():
            if Path(path) == Path(ue_model):
                self.slots[key] = 0
            else:
                slot = 1 + sum(1 for k, s in self.slots.items() if s > 0)
                self.tf.load(slot, Path(path))
                self.slots[key] = slot
        self.crc = {k: self.tf.meta(s).payload_crc32 for k, s in self.slots.items()}

    def process(self, dump: HDump, i2: int | None = None) -> ReplayResult:
        self.tf.use(self.slots["encoder"])
        payload = self.tf.encode(dump.iq)
        truth = truth_cfr(dump.iq)
        recon = {}
        for key in ("same", "cross"):
            if key in self.slots:
                self.tf.use(self.slots[key])
                recon[key] = self.tf.reconstruct(payload)
        self.tf.use(0)
        v = dominant_beams(truth)
        res = ReplayResult(dump.frame, dump.timestamp_utc_us, payload, latent_codes(payload), truth, recon, i2=i2)
        for key, pred in recon.items():
            res.beam_cos[key] = beam_cosine(v, dominant_beams(pred))
            res.chan_cos[key] = channel_cosine(truth, pred)
            res.nmse_db[key] = nmse_db(truth, pred)
        if i2 is not None:
            res.beam_cos["legacy"] = beam_cosine(v, codebook_beam(i2))
        return res
