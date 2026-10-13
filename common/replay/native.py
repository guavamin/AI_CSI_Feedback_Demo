"""OAI's own Transformer code (ai_fb_transformer.c) as a Python-callable library.

The replay encodes the UE's recorded channel and decodes it with the gNB's decoders using exactly the C
code that runs inside nr-softmodem / nr-uesoftmodem, so the GUI shows what OAI computes (no PyTorch, no
GPU). The library is compiled from the OAI tree on first use into data/native/ and rebuilt when the
sources change.
"""

from __future__ import annotations

import ctypes
import hashlib
import subprocess
from pathlib import Path

import numpy as np

from common.style import REPO

LATENT = 24
PAYLOAD_BYTES = 6
ACTIVE = 612
RUNTIME_BINS = 768

_STUB = r"""
/* Logging stubs: the library runs outside the softmodem (same stubs as tests/ai_fb). */
#include "common/utils/LOG/log.h"
static log_t g_lib_log;
log_t *g_log = &g_lib_log;
int T_stdout = 1;
#if T_TRACER
volatile int *T_freelist_head;
T_cache_t *T_cache;
int *T_active;
#endif
void logRecord_mt(const char *file, const char *func, int line, int comp, int level, const char *format, ...)
{
  (void)file; (void)func; (void)line; (void)comp; (void)level; (void)format;
}
"""


class TfMeta(ctypes.Structure):
    """ai_fb_tf_metadata_t (openair2/LAYER2/NR_MAC_COMMON/ai_fb_transformer.h)."""

    _fields_ = [(name, ctypes.c_uint32) for name in (
        "artifact_version", "header_bytes", "domain", "grid", "runtime_fft_bins", "active_subcarriers",
        "subband_average", "delay_taps", "nr", "nt", "nc", "nrt", "lift_channels", "encoder_num_blocks",
        "decoder_num_blocks", "patch_size", "num_tokens", "patch_feature_dim", "num_heads", "d_model", "d_ff",
        "latent_dim", "quantization_bits", "tensor_count")] + [
        ("normalization_scale", ctypes.c_float), ("payload_crc32", ctypes.c_uint32)]


def build_library(oai_dir: Path, oai_build: Path, out_dir: Path | None = None) -> Path:
    """Compile ai_fb_transformer.c + log stubs into a shared library (cached by source hash)."""
    src = oai_dir / "openair2/LAYER2/NR_MAC_COMMON/ai_fb_transformer.c"
    hdr = oai_dir / "openair2/LAYER2/NR_MAC_COMMON/ai_fb_transformer.h"
    t_dir = oai_build / "common/utils/T"  # generated T_IDs.h (log.h includes the tracer headers)
    for p in (src, hdr, t_dir / "T_IDs.h"):
        if not p.exists():
            raise FileNotFoundError(f"{p} not found: build OAI first (cmake_targets/ran_build_gui)")
    out_dir = out_dir or (REPO / "data/native")
    out_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(src.read_bytes() + hdr.read_bytes() + _STUB.encode()).hexdigest()[:12]
    lib = out_dir / f"libaifb_transformer_{digest}.so"
    if lib.exists():
        return lib
    stub = out_dir / "aifb_log_stub.c"
    stub.write_text(_STUB)
    cmd = ["gcc", "-O2", "-shared", "-fPIC", "-o", str(lib), str(src), str(stub),
           f"-I{oai_dir}", f"-I{oai_dir / 'openair2'}", f"-I{oai_dir / 'common/utils'}",
           f"-I{oai_dir / 'common/utils/T'}", f"-I{t_dir}", f"-I{oai_dir / 'openair2/LAYER2/NR_MAC_COMMON'}", "-lm"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"building {lib.name} failed:\n{res.stderr[-2000:]}")
    return lib


class NativeTransformer:
    """Thin ctypes binding. Slot selection is thread-local in the C code: use one thread per instance."""

    def __init__(self, lib_path: Path):
        self.lib = ctypes.CDLL(str(lib_path))
        L = self.lib
        L.ai_fb_transformer_load_slot.argtypes = [ctypes.c_int, ctypes.c_char_p]
        L.ai_fb_transformer_load_slot.restype = ctypes.c_bool
        L.ai_fb_transformer_use_slot.argtypes = [ctypes.c_int]
        L.ai_fb_transformer_use_slot.restype = ctypes.c_bool
        L.ai_fb_transformer_slot_metadata.argtypes = [ctypes.c_int]
        L.ai_fb_transformer_slot_metadata.restype = ctypes.POINTER(TfMeta)
        L.ai_fb_transformer_preprocess_c16.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                                       ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        L.ai_fb_transformer_preprocess_c16.restype = ctypes.c_bool
        L.ai_fb_transformer_encode_tensor.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        L.ai_fb_transformer_encode_tensor.restype = ctypes.c_bool
        L.ai_fb_transformer_quantize_pack.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        L.ai_fb_transformer_quantize_pack.restype = None
        L.ai_fb_transformer_reconstruct_cfr.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        L.ai_fb_transformer_reconstruct_cfr.restype = ctypes.c_bool
        self._tensor = None

    def load(self, slot: int, path: Path) -> TfMeta:
        if not self.lib.ai_fb_transformer_load_slot(slot, str(path).encode()):
            raise RuntimeError(f"cannot load {path} into slot {slot}")
        meta = self.lib.ai_fb_transformer_slot_metadata(slot).contents
        self._tensor = np.zeros(2 * meta.nc * meta.nrt, dtype=np.float32)
        return meta

    def meta(self, slot: int) -> TfMeta | None:
        ptr = self.lib.ai_fb_transformer_slot_metadata(slot)
        return ptr.contents if ptr else None

    def use(self, slot: int) -> None:
        if not self.lib.ai_fb_transformer_use_slot(slot):
            raise RuntimeError(f"model slot {slot} is not loaded")

    def encode(self, h_iq: np.ndarray) -> bytes:
        """UE side with the current slot: packed recorded H [rx][port][768] int16 IQ -> 6-byte PUCCH latent."""
        iq = np.ascontiguousarray(h_iq, dtype=np.int16)
        assert iq.shape == (2, 2, RUNTIME_BINS, 2), iq.shape
        if not self.lib.ai_fb_transformer_preprocess_c16(iq.ctypes.data, 2 * RUNTIME_BINS, RUNTIME_BINS, RUNTIME_BINS,
                                                         0, self._tensor.ctypes.data):
            raise RuntimeError("preprocess failed")
        latent = np.zeros(LATENT, dtype=np.float32)
        if not self.lib.ai_fb_transformer_encode_tensor(self._tensor.ctypes.data, latent.ctypes.data):
            raise RuntimeError("encode failed")
        payload = (ctypes.c_uint8 * PAYLOAD_BYTES)()
        self.lib.ai_fb_transformer_quantize_pack(latent.ctypes.data, payload)
        return bytes(payload)

    def reconstruct(self, payload: bytes) -> np.ndarray:
        """gNB side with the current slot: 6-byte latent -> CFR [rx][tx][612] (physical order, H units)."""
        buf = (ctypes.c_uint8 * PAYLOAD_BYTES).from_buffer_copy(payload)
        out = np.zeros((2, 2, ACTIVE), dtype=np.complex128)
        if not self.lib.ai_fb_transformer_reconstruct_cfr(buf, out.ctypes.data):
            raise RuntimeError("reconstruct failed")
        return out


def latent_codes(payload: bytes) -> np.ndarray:
    """6-byte payload -> 24 two-bit codes (OAI wire contract: four codes per byte, LSB first)."""
    b = np.frombuffer(payload, dtype=np.uint8)
    return np.stack([(b >> (2 * k)) & 3 for k in range(4)], axis=1).reshape(-1)
