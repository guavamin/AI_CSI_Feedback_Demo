"""Tests for CSI-RS H_*.bin ingest and cleanup."""

from __future__ import annotations

import struct
from pathlib import Path

from common.oai.config import load_config
from common.oai.net.csirs_ingest import (
    CsirsRecordIngest,
    cleanup_old_h_bins,
    parse_h_bin,
    wipe_record_dir,
)
from common.oai.net.softmodem_launcher import SoftmodemLauncher

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _write_fake_h(
    path: Path,
    *,
    frame: int = 1,
    slot: int = 2,
    n_rx: int = 2,
    n_ports: int = 2,
    n_subc: int = 8,
) -> None:
    hdr = struct.pack("<5iq", frame, slot, n_rx, n_ports, n_subc, 1234567890)
    # Simple IQ: r=i=1000 → raw |H| = sqrt(2)*1000; peak-norm → 1.0
    samples = []
    for _ in range(n_rx * n_ports * n_subc):
        samples.extend([1000, 1000])
    payload = struct.pack(f"<{len(samples)}h", *samples)
    path.write_bytes(hdr + payload)


def test_parse_h_bin_shape(tmp_path):
    path = tmp_path / "H_1_f1_s2.bin"
    _write_fake_h(path, n_rx=2, n_ports=2, n_subc=8)
    sample = parse_h_bin(path)
    assert sample is not None
    assert sample.n_rx == 2
    assert sample.n_ports == 2
    assert sample.n_subc == 8
    assert len(sample.links) == 4
    assert sample.links[0][0] == "RX0-TX0"
    assert len(sample.links[0][1]) == 8
    assert abs(sample.links[0][1][0] - 1.0) < 1e-6


def test_parse_h_bin_peak_preserves_relative_gain(tmp_path):
    path = tmp_path / "H_rel.bin"
    # One strong tone (3000,0) and one weak (1500,0) → peak-norm 1.0 and 0.5
    hdr = struct.pack("<5iq", 0, 0, 1, 2, 1, 1)
    payload = struct.pack("<4h", 3000, 0, 1500, 0)
    path.write_bytes(hdr + payload)
    sample = parse_h_bin(path, normalize="peak")
    assert sample is not None
    assert abs(sample.links[0][1][0] - 1.0) < 1e-6
    assert abs(sample.links[1][1][0] - 0.5) < 1e-6
    raw = parse_h_bin(path, normalize="none")
    assert raw is not None
    assert abs(raw.links[0][1][0] - 3000.0) < 1e-6


def test_cleanup_keeps_newest(tmp_path):
    root = tmp_path / "rec"
    ch = root / "csi_rs_channels"
    ch.mkdir(parents=True)
    paths = []
    for i in range(5):
        p = ch / f"H_{i}_f0_s0.bin"
        _write_fake_h(p, frame=i)
        paths.append(p)
        # Ensure distinct mtimes
        import os
        import time

        os.utime(p, (time.time() + i, time.time() + i))
    deleted = cleanup_old_h_bins(root, keep=2)
    assert deleted == 3
    remaining = sorted(ch.glob("H_*.bin"))
    assert len(remaining) == 2
    assert remaining[-1].name.startswith("H_4_")


def test_ingest_poll_and_wipe(tmp_path):
    root = tmp_path / "csi_record"
    ingest = CsirsRecordIngest(root, keep_h=2)
    ch = root / "csi_rs_channels"
    p1 = ch / "H_1_f1_s1.bin"
    _write_fake_h(p1, frame=1)
    s1 = ingest.poll(cleanup=True)
    assert s1 is not None
    assert s1.frame == 1
    assert ingest.poll(cleanup=True) is None  # unchanged
    p2 = ch / "H_2_f2_s2.bin"
    _write_fake_h(p2, frame=2)
    s2 = ingest.poll(cleanup=True)
    assert s2 is not None
    assert s2.frame == 2
    ingest.wipe()
    assert not root.exists()


def test_ue_argv_includes_csi_record_path(tmp_path):
    cfg = load_config(FIXTURES / "lab_rfsim.yaml")
    assert cfg.oai.csi_record is True
    cfg.oai.use_sudo = False
    cfg.oai.build_dir = str(tmp_path)
    (tmp_path / "nr-uesoftmodem").write_text("")
    launcher = SoftmodemLauncher(cfg, tmp_path / "logs")
    launcher.csi_record_path = tmp_path / "csi_record"
    argv = launcher.build_argv("ue", "legacy")
    assert "--csi-record-path" in argv
    # Softmodem cwd is build_dir — path must be absolute so GUI and UE share one tree.
    assert argv[argv.index("--csi-record-path") + 1] == str(
        (tmp_path / "csi_record").resolve()
    )


def test_wipe_record_dir_missing_ok(tmp_path):
    wipe_record_dir(tmp_path / "does_not_exist")


def test_remap_signed_puts_dc_at_zero():
    from common.oai.net.csirs_ingest import remap_csirs_freq_axis

    n = 16 + 32  # N_fft + FILTER_MARGIN
    subc = [float(i) for i in range(n)]
    # Peak at native bin 0 (DC) and bin 15 (near Nyquist / negative edge)
    mag = [0.0] * n
    mag[0] = 1.0
    mag[15] = 0.5
    links = [("RX0-TX0", mag)]
    out_x, out_links, n_fft = remap_csirs_freq_axis(
        subc, links, axis="signed", n_subc_full=n, filter_margin=32
    )
    assert n_fft == 16
    assert out_x[0] < 0
    assert out_x[-1] > 0
    # DC (signed 0) present
    assert 0.0 in out_x
    dc_i = out_x.index(0.0)
    assert out_links[0][1][dc_i] == 1.0
    # Native bin 15 → signed −1
    assert -1.0 in out_x
    neg_i = out_x.index(-1.0)
    assert abs(out_links[0][1][neg_i] - 0.5) < 1e-9


def test_remap_native_unchanged():
    from common.oai.net.csirs_ingest import remap_csirs_freq_axis

    subc = [0.0, 1.0, 2.0]
    links = [("a", [0.1, 0.2, 0.3])]
    ox, ol, _n = remap_csirs_freq_axis(subc, links, axis="native")
    assert ox == subc
    assert ol[0][1] == links[0][1]
