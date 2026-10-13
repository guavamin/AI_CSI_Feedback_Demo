"""Parse OAI UE CSI-RS ``H_*.bin`` dumps and keep the record dir tidy."""

from __future__ import annotations

import logging
import math
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# OAI header: 5×int32 (frame,slot,nr_rx,n_ports,n_subc) + int64 timestamp_utc_us
_HDR_FMT = "<5iq"
_HDR_SIZE = struct.calcsize(_HDR_FMT)  # 28 bytes
_C16_SIZE = 4  # int16 r + int16 i


@dataclass(frozen=True)
class CsirsHSample:
    frame: int
    slot: int
    n_rx: int
    n_ports: int
    n_subc: int
    timestamp_utc_us: int
    path: Path
    # List of (label, |H| magnitudes[n_subc]), RX-major then port
    links: list[tuple[str, list[float]]]
    subc: list[float]


def channels_dir(record_root: Path | str) -> Path:
    return Path(record_root) / "csi_rs_channels"


def ensure_record_tree(record_root: Path | str) -> Path:
    root = Path(record_root)
    root.mkdir(parents=True, exist_ok=True)
    channels_dir(root).mkdir(parents=True, exist_ok=True)
    return root


def list_h_bins(record_root: Path | str) -> list[Path]:
    d = channels_dir(record_root)
    if not d.is_dir():
        return []
    # The UE writes and the cleanup deletes H_*.bin concurrently: skip files that vanish mid-listing
    # instead of letting the forwarder thread die.
    stamped = []
    for p in d.glob("H_*.bin"):
        try:
            stamped.append((p.stat().st_mtime, p))
        except FileNotFoundError:
            continue
    return [p for _, p in sorted(stamped)]


def cleanup_old_h_bins(record_root: Path | str, *, keep: int = 2) -> int:
    """Delete older ``H_*.bin`` files; keep the ``keep`` newest. Returns deleted count."""
    keep = max(1, int(keep))
    bins = list_h_bins(record_root)
    if len(bins) <= keep:
        return 0
    deleted = 0
    for path in bins[:-keep]:
        try:
            path.unlink(missing_ok=True)
            deleted += 1
        except OSError as exc:
            log.debug("Could not delete %s: %s", path, exc)
    return deleted


def wipe_record_dir(record_root: Path | str) -> None:
    """Remove the whole CSI record directory (H bins + CSV) after a run."""
    root = Path(record_root)
    if not root.exists():
        return
    try:
        shutil.rmtree(root, ignore_errors=False)
        log.info("Wiped CSI record dir %s", root)
    except OSError as exc:
        log.warning("Failed to wipe CSI record dir %s: %s", root, exc)
        shutil.rmtree(root, ignore_errors=True)


def parse_h_bin(
    path: Path | str,
    *,
    normalize: str = "peak",
) -> CsirsHSample | None:
    """
    Parse one OAI ``H_*.bin`` into per-link magnitude series.

    Layout matches ``csi_record_write`` in ``csi_rx.c``.

    OAI stores LS/interpolated CSI-RS estimates as ``c16_t`` (int16 IQ) that are
    *not* Q15 unit-gain. Dividing by 32768 therefore crushes the plot near 0.
    Default ``normalize="peak"`` scales all links by the sample-wide max ``|H|``
    so the strongest tone is 1.0 (relative RX/TX strengths are preserved).
    Use ``normalize="none"`` to keep raw ``hypot(re, im)``.
    """
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        log.debug("Cannot read %s: %s", p, exc)
        return None
    if len(raw) < _HDR_SIZE:
        return None
    frame, slot, n_rx, n_ports, n_subc, ts = struct.unpack_from(_HDR_FMT, raw, 0)
    if n_rx <= 0 or n_ports <= 0 or n_subc <= 0:
        return None
    n_samp = int(n_rx) * int(n_ports) * int(n_subc)
    need = _HDR_SIZE + n_samp * _C16_SIZE
    if len(raw) < need:
        log.debug(
            "Truncated H bin %s: have %d need %d (rx=%d ports=%d subc=%d)",
            p,
            len(raw),
            need,
            n_rx,
            n_ports,
            n_subc,
        )
        return None

    iq = memoryview(raw)[_HDR_SIZE:need]
    # Unpack as int16 pairs
    vals = struct.unpack(f"<{n_samp * 2}h", iq)
    raw_mags: list[list[float]] = []
    idx = 0
    peak = 0.0
    for _rx in range(n_rx):
        for _port in range(n_ports):
            mag: list[float] = []
            for _k in range(n_subc):
                r = float(vals[idx])
                i = float(vals[idx + 1])
                idx += 2
                m = math.hypot(r, i)
                if m > peak:
                    peak = m
                mag.append(m)
            raw_mags.append(mag)

    mode = (normalize or "peak").strip().lower()
    if mode == "peak" and peak > 0.0:
        inv = 1.0 / peak
        scaled = [[v * inv for v in mag] for mag in raw_mags]
    else:
        scaled = raw_mags

    links: list[tuple[str, list[float]]] = []
    li = 0
    for rx in range(n_rx):
        for port in range(n_ports):
            links.append((f"RX{rx}-TX{port}", scaled[li]))
            li += 1
    subc = [float(k) for k in range(n_subc)]
    return CsirsHSample(
        frame=int(frame),
        slot=int(slot),
        n_rx=int(n_rx),
        n_ports=int(n_ports),
        n_subc=int(n_subc),
        timestamp_utc_us=int(ts),
        path=p,
        links=links,
        subc=subc,
    )


# OAI csi_rx.c FILTER_MARGIN — H dumps are ofdm_symbol_size + margin
OAI_FILTER_MARGIN = 32


def infer_n_fft(n_subc: int, *, filter_margin: int = OAI_FILTER_MARGIN) -> int:
    """Infer OFDM FFT size from dumped length (N_fft + FILTER_MARGIN)."""
    n_subc = int(n_subc)
    margin = max(0, int(filter_margin))
    if margin > 0 and n_subc > margin:
        cand = n_subc - margin
        if cand > 0 and (cand & (cand - 1)) == 0:
            return cand
    if n_subc > 0 and (n_subc & (n_subc - 1)) == 0:
        return n_subc
    return n_subc


def native_bin_to_signed(k: int, n_fft: int) -> float:
    """Map unshifted FFT bin index to signed baseband subcarrier (−N/2 … N/2−1)."""
    n_fft = max(1, int(n_fft))
    k = int(k) % n_fft
    half = n_fft // 2
    return float(k if k < half else k - n_fft)


def remap_csirs_freq_axis(
    subc: list[float],
    links: list[tuple[str, list[float]]],
    *,
    axis: str = "native",
    n_subc_full: int | None = None,
    filter_margin: int = OAI_FILTER_MARGIN,
) -> tuple[list[float], list[tuple[str, list[float]]], int]:
    """
    Remap CSI-RS |H| for plotting.

    ``native``: leave OAI buffer indices as-is (DC near 0, occupied RBs wrap).
    ``signed``: fftshift-style axis with DC at 0 and guards toward ±N/2.

    Returns ``(subc, links, n_fft)``.
    """
    mode = (axis or "native").strip().lower()
    if not subc or not links or mode != "signed":
        n_hint = int(n_subc_full) if n_subc_full else (len(subc) if subc else 0)
        return list(subc), list(links), infer_n_fft(n_hint, filter_margin=filter_margin)

    n_full = int(n_subc_full) if n_subc_full else max(int(round(max(subc))), len(subc) - 1) + 1
    n_fft = infer_n_fft(n_full, filter_margin=filter_margin)

    # Pair each tone with signed freq, drop bins outside 0..N_fft-1 (margin pad)
    pairs: list[tuple[float, list[float]]] = []
    for i, k in enumerate(subc):
        ki = int(round(float(k)))
        if ki < 0 or ki >= n_fft:
            continue
        mags_i = [float(mag[i]) for _label, mag in links if i < len(mag)]
        if len(mags_i) != len(links):
            continue
        pairs.append((native_bin_to_signed(ki, n_fft), mags_i))

    if not pairs:
        return list(subc), list(links), n_fft

    pairs.sort(key=lambda t: t[0])
    out_subc = [p[0] for p in pairs]
    out_links: list[tuple[str, list[float]]] = []
    for li, (label, _old) in enumerate(links):
        out_links.append((label, [p[1][li] for p in pairs]))
    return out_subc, out_links, n_fft


class CsirsRecordIngest:
    """Watch a UE ``--csi-record-path`` tree and expose the newest H dump."""

    def __init__(self, record_root: Path | str, *, keep_h: int = 2):
        self.record_root = Path(record_root)
        self.keep_h = max(1, int(keep_h))
        self._last_path: Path | None = None
        self._last_mtime: float = -1.0
        ensure_record_tree(self.record_root)

    def poll(self, *, cleanup: bool = True) -> CsirsHSample | None:
        """
        Return a new sample when the newest ``H_*.bin`` changes.

        When ``cleanup`` is True, delete older bins after a successful read
        (keeps only ``keep_h`` newest) so long runs do not fill the disk.
        """
        bins = list_h_bins(self.record_root)
        if not bins:
            if cleanup:
                cleanup_old_h_bins(self.record_root, keep=self.keep_h)
            return None
        newest = bins[-1]
        try:
            mtime = newest.stat().st_mtime
        except OSError:
            return None
        if self._last_path == newest and mtime == self._last_mtime:
            if cleanup and len(bins) > self.keep_h:
                cleanup_old_h_bins(self.record_root, keep=self.keep_h)
            return None
        sample = parse_h_bin(newest)
        if sample is None:
            return None
        self._last_path = newest
        self._last_mtime = mtime
        if cleanup:
            cleanup_old_h_bins(self.record_root, keep=self.keep_h)
        return sample

    def wipe(self) -> None:
        wipe_record_dir(self.record_root)
        self._last_path = None
        self._last_mtime = -1.0
