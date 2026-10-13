"""Tests for CSI-RS UDP pack/unpack and latest-wins forward path."""

from __future__ import annotations

import socket
import struct
import threading
import time
from pathlib import Path

from common.oai.net.csirs_ingest import parse_h_bin
from common.oai.net.csirs_udp import (
    DEFAULT_MAX_PAYLOAD,
    CsirsUdpIngest,
    choose_stride,
    forward_once,
    pack_sample,
    unpack_datagram,
)
from tests.oai.test_csirs_ingest import _write_fake_h


def test_choose_stride_fits_mtu():
    # 2x2x1056 float16 full = 8448 B body — must decimate
    stride = choose_stride(2, 2, 1056, max_payload=DEFAULT_MAX_PAYLOAD)
    assert stride >= 8
    n_send = (1056 + stride - 1) // stride
    body = 2 * 2 * n_send * 2
    assert 44 + body <= DEFAULT_MAX_PAYLOAD


def test_pack_unpack_roundtrip(tmp_path):
    path = tmp_path / "H_rt.bin"
    _write_fake_h(path, n_rx=2, n_ports=2, n_subc=64)
    sample = parse_h_bin(path, normalize="peak")
    assert sample is not None
    pkt = pack_sample(sample, seq=7, max_payload=DEFAULT_MAX_PAYLOAD)
    assert len(pkt) <= DEFAULT_MAX_PAYLOAD
    out = unpack_datagram(pkt)
    assert out is not None
    sample_out, seq = out
    assert seq == 7
    assert sample_out.frame == sample.frame
    assert sample_out.n_rx == 2 and sample_out.n_ports == 2
    assert sample_out.n_subc == 64  # full size reported
    assert abs(max(m for _, mag in sample_out.links for m in mag) - 1.0) < 1e-2
    assert sample_out.path.name == "udp_seq_7"


def test_udp_ingest_latest_wins():
    # Bind ephemeral port
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    ingest = CsirsUdpIngest(host="127.0.0.1", port=port)
    ingest.start()
    try:
        # Build two packets with different frames
        root = Path("/tmp")  # unused; craft via pack from fake files
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            p1 = td_path / "H_1.bin"
            p2 = td_path / "H_2.bin"
            _write_fake_h(p1, frame=1, n_rx=1, n_ports=1, n_subc=32)
            _write_fake_h(p2, frame=9, n_rx=1, n_ports=1, n_subc=32)
            s1 = parse_h_bin(p1)
            s2 = parse_h_bin(p2)
            assert s1 and s2
            pkt1 = pack_sample(s1, seq=1)
            pkt2 = pack_sample(s2, seq=2)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(pkt1, ("127.0.0.1", port))
            sock.sendto(pkt2, ("127.0.0.1", port))
            sock.close()

        deadline = time.time() + 2.0
        got = None
        while time.time() < deadline:
            got = ingest.poll()
            if got is not None:
                break
            time.sleep(0.02)
        assert got is not None
        assert got.frame == 9
        assert ingest.poll() is None  # no newer seq
    finally:
        ingest.close()


def test_forward_once_sends(tmp_path):
    root = tmp_path / "rec"
    ch = root / "csi_rs_channels"
    ch.mkdir(parents=True)
    _write_fake_h(ch / "H_a.bin", frame=3, n_rx=1, n_ports=2, n_subc=48)

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    port = rx.getsockname()[1]
    rx.settimeout(1.0)

    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    seq, path, _mt = forward_once(
        root, tx, ("127.0.0.1", port), seq=0, keep_h=2
    )
    tx.close()
    assert seq == 1
    assert path is not None
    data, _ = rx.recvfrom(65535)
    rx.close()
    sample = unpack_datagram(data)
    assert sample is not None
    sample_out, _seq = sample
    assert sample_out.frame == 3
