"""Low-latency CSI-RS |H| streaming over UDP (UE forwarder → GUI).

Design goals
------------
* Latest-wins: receiver keeps only the highest ``seq``; slow GUI never queues.
* Single datagram: auto-decimate subcarriers so payload fits one Ethernet MTU
  (no IP fragmentation, no reassembly latency).
* Compact: peak-normalized ``float16`` magnitudes (not raw IQ / not disk poll on GUI).
* Option A: UE host watches ``H_*.bin`` and pushes; no OAI stack change.
"""

from __future__ import annotations

import logging
import math
import socket
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from common.oai.net.csirs_ingest import (
    CsirsHSample,
    cleanup_old_h_bins,
    list_h_bins,
    parse_h_bin,
)

log = logging.getLogger(__name__)

MAGIC = b"CSIR"
VERSION = 1
FLAG_PEAK_NORM = 0x01

# magic(4)+ver(1)+flags(1)+pad(2)+frame(4)+slot(4)+n_rx(2)+n_ports(2)
# +n_subc_full(2)+n_subc_send(2)+stride(2)+pad2(2)+seq(4)+ts(8)+peak(4) = 44
_HDR_FMT = "<4sBBHiiHHHHHHIQf"
_HDR_SIZE = struct.calcsize(_HDR_FMT)
assert _HDR_SIZE == 44

# Stay under typical 1500 MTU with IP/UDP headers (~28–48 B)
DEFAULT_MAX_PAYLOAD = 1200


def choose_stride(n_rx: int, n_ports: int, n_subc: int, *, max_payload: int = DEFAULT_MAX_PAYLOAD) -> int:
    """Pick the smallest stride so float16 payload + header ≤ ``max_payload``."""
    n_links = max(1, int(n_rx) * int(n_ports))
    budget = max(64, int(max_payload) - _HDR_SIZE)
    max_subc = max(1, budget // (n_links * 2))
    n_subc = max(1, int(n_subc))
    if n_subc <= max_subc:
        return 1
    return int(math.ceil(n_subc / max_subc))


def _decimate_links(
    links: list[tuple[str, list[float]]],
    *,
    stride: int,
) -> tuple[list[float], list[tuple[str, list[float]]]]:
    stride = max(1, int(stride))
    if not links:
        return [], []
    n_subc = len(links[0][1])
    idxs = list(range(0, n_subc, stride))
    if not idxs:
        idxs = [0]
    subc = [float(i) for i in idxs]
    out: list[tuple[str, list[float]]] = []
    for label, mag in links:
        out.append((label, [float(mag[i]) for i in idxs]))
    return subc, out


def pack_sample(
    sample: CsirsHSample,
    *,
    seq: int,
    max_payload: int = DEFAULT_MAX_PAYLOAD,
    peak_raw: float = 0.0,
) -> bytes:
    """Encode a peak-normalized sample into one UDP datagram."""
    n_rx = int(sample.n_rx)
    n_ports = int(sample.n_ports)
    n_subc = int(sample.n_subc)
    stride = choose_stride(n_rx, n_ports, n_subc, max_payload=max_payload)
    _subc, links = _decimate_links(list(sample.links), stride=stride)
    n_send = len(links[0][1]) if links else 0
    flat: list[float] = []
    for _label, mag in links:
        flat.extend(mag)
    body = np.asarray(flat, dtype="<f2").tobytes()
    hdr = struct.pack(
        _HDR_FMT,
        MAGIC,
        VERSION,
        FLAG_PEAK_NORM,
        0,
        int(sample.frame),
        int(sample.slot),
        n_rx,
        n_ports,
        n_subc,
        n_send,
        stride,
        0,
        int(seq) & 0xFFFFFFFF,
        int(sample.timestamp_utc_us),
        float(peak_raw),
    )
    pkt = hdr + body
    if len(pkt) > max_payload:
        # Should not happen if choose_stride is correct; harden anyway.
        log.warning("CSI-RS UDP packet %d B exceeds max_payload %d", len(pkt), max_payload)
    return pkt


def unpack_datagram(data: bytes) -> tuple[CsirsHSample, int] | None:
    """Decode one datagram into ``(sample, seq)``."""
    if len(data) < _HDR_SIZE:
        return None
    (
        magic,
        ver,
        flags,
        _pad,
        frame,
        slot,
        n_rx,
        n_ports,
        n_subc_full,
        n_subc_send,
        stride,
        _pad2,
        seq,
        ts,
        _peak,
    ) = struct.unpack_from(_HDR_FMT, data, 0)
    if magic != MAGIC or ver != VERSION:
        return None
    if n_rx <= 0 or n_ports <= 0 or n_subc_send <= 0:
        return None
    n_links = int(n_rx) * int(n_ports)
    n_vals = n_links * int(n_subc_send)
    need = _HDR_SIZE + n_vals * 2
    if len(data) < need:
        return None
    arr = np.frombuffer(data, dtype="<f2", count=n_vals, offset=_HDR_SIZE)
    stride = max(1, int(stride))
    subc = [float(i * stride) for i in range(int(n_subc_send))]
    links: list[tuple[str, list[float]]] = []
    idx = 0
    for rx in range(int(n_rx)):
        for port in range(int(n_ports)):
            chunk = arr[idx : idx + int(n_subc_send)]
            idx += int(n_subc_send)
            links.append((f"RX{rx}-TX{port}", [float(x) for x in chunk]))
    sample = CsirsHSample(
        frame=int(frame),
        slot=int(slot),
        n_rx=int(n_rx),
        n_ports=int(n_ports),
        # Report full FFT size; plotted points are stride-decimated in ``subc``/links.
        n_subc=int(n_subc_full),
        timestamp_utc_us=int(ts),
        path=Path(f"udp_seq_{int(seq)}"),
        links=links,
        subc=subc,
    )
    return sample, int(seq) & 0xFFFFFFFF


@dataclass
class _Latest:
    sample: CsirsHSample
    seq: int


class CsirsUdpIngest:
    """Background UDP receiver; ``poll()`` returns only newer ``seq`` samples."""

    def __init__(self, host: str = "0.0.0.0", port: int = 9101):
        self.host = host
        self.port = int(port)
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest: _Latest | None = None
        self._last_polled_seq: int = -1
        self._rx_count = 0
        self._drop_old = 0

    @property
    def bind_addr(self) -> str:
        return f"{self.host}:{self.port}"

    def start(self) -> None:
        if self._thread is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Larger RCVBUF so bursts of CSI occasions don't drop before we drain.
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:
            pass
        sock.bind((self.host, self.port))
        sock.settimeout(0.2)
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._recv_loop, name="csirs-udp-rx", daemon=True
        )
        self._thread.start()
        log.info("CSI-RS UDP ingest listening on %s", self.bind_addr)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def wipe(self) -> None:
        """No disk state; clear the latest sample."""
        with self._lock:
            self._latest = None
            self._last_polled_seq = -1

    def close(self) -> None:
        self.stop()
        self.wipe()

    def poll(self, *, cleanup: bool = True) -> CsirsHSample | None:  # noqa: ARG002
        with self._lock:
            latest = self._latest
            if latest is None:
                return None
            if latest.seq <= self._last_polled_seq:
                return None
            self._last_polled_seq = latest.seq
            return latest.sample

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "rx": self._rx_count,
                "drop_old": self._drop_old,
                "has_sample": 1 if self._latest else 0,
            }

    def _recv_loop(self) -> None:
        assert self._sock is not None
        sock = self._sock
        while not self._stop.is_set():
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                continue
            # Drain socket: keep only the last datagram in this burst (latest-wins).
            last = data
            sock.setblocking(False)
            try:
                while True:
                    last, _ = sock.recvfrom(65535)
            except BlockingIOError:
                pass
            except OSError:
                pass
            finally:
                sock.settimeout(0.2)

            sample = unpack_datagram(last)
            if sample is None:
                continue
            sample_h, seq = sample
            with self._lock:
                self._rx_count += 1
                if self._latest is not None and seq < self._latest.seq:
                    self._drop_old += 1
                    continue
                self._latest = _Latest(sample=sample_h, seq=seq)


def forward_once(
    record_root: Path | str,
    sock: socket.socket,
    dest: tuple[str, int],
    *,
    seq: int,
    keep_h: int = 2,
    max_payload: int = DEFAULT_MAX_PAYLOAD,
    last_path: Path | None = None,
    last_mtime: float = -1.0,
) -> tuple[int, Path | None, float]:
    """
    Send newest H bin if it changed. Returns ``(seq, last_path, last_mtime)``.

    Skips to the newest file only (never sends a backlog) for low latency.
    """
    root = Path(record_root)
    bins = list_h_bins(root)
    if not bins:
        return seq, last_path, last_mtime
    newest = bins[-1]
    try:
        mtime = newest.stat().st_mtime
    except OSError:
        return seq, last_path, last_mtime
    if newest == last_path and mtime == last_mtime:
        cleanup_old_h_bins(root, keep=keep_h)
        return seq, last_path, last_mtime

    sample = parse_h_bin(newest, normalize="peak")
    if sample is None:
        return seq, last_path, last_mtime

    next_seq = (seq + 1) & 0xFFFFFFFF
    pkt = pack_sample(sample, seq=next_seq, max_payload=max_payload, peak_raw=0.0)
    try:
        sock.sendto(pkt, dest)
    except OSError as exc:
        log.warning("CSI-RS UDP send failed: %s", exc)
        return seq, last_path, last_mtime

    cleanup_old_h_bins(root, keep=keep_h)
    return next_seq, newest, mtime


def run_forwarder(
    record_path: Path | str,
    dest_host: str,
    dest_port: int,
    *,
    keep_h: int = 2,
    poll_ms: float = 5.0,
    max_payload: int = DEFAULT_MAX_PAYLOAD,
    follow_latest_under: bool = False,
    stop_event: threading.Event | None = None,
) -> None:
    """
    Watch ``record_path`` (or newest child if ``follow_latest_under``) and push UDP.

    ``poll_ms`` defaults to 5 ms so disk→wire latency stays low without busy-spin.
    """
    stop = stop_event or threading.Event()
    dest = (dest_host, int(dest_port))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
    except OSError:
        pass

    seq = 0
    last_path: Path | None = None
    last_mtime = -1.0
    period = max(0.001, float(poll_ms) / 1000.0)
    log.info(
        "CSI-RS forwarder → %s:%d watching %s (poll=%.1f ms, max_payload=%d)",
        dest_host,
        dest_port,
        record_path,
        poll_ms,
        max_payload,
    )

    try:
        while not stop.is_set():
            root = Path(record_path)
            if follow_latest_under and root.is_dir():
                children = sorted(
                    (p for p in root.iterdir() if p.is_dir()),
                    key=lambda p: p.stat().st_mtime,
                )
                if children:
                    root = children[-1]
            if root.is_dir():
                seq, last_path, last_mtime = forward_once(
                    root,
                    sock,
                    dest,
                    seq=seq,
                    keep_h=keep_h,
                    max_payload=max_payload,
                    last_path=last_path,
                    last_mtime=last_mtime,
                )
            stop.wait(period)
    finally:
        sock.close()
        log.info("CSI-RS forwarder stopped (last seq=%d)", seq)
