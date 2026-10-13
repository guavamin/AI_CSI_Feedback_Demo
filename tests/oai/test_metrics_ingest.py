"""MacStatsIngest throughput hold / clear behaviour."""

from __future__ import annotations

import time
from pathlib import Path

from common.oai.net.metrics_ingest import MacStatsIngest


def _write_stats(path: Path, tx: int, bler: float = 0.01) -> None:
    path.write_text(
        f"UE RNTI a1b2 in-sync\n"
        f"UE a1b2: CQI 10, RI 1, PMI (0,0)\n"
        f"UE a1b2: dlsch_rounds 1/0/0/0 BLER {bler:.4f} MCS 10\n"
        f"UE a1b2: MAC: TX {tx} RX 0 bytes\n",
        encoding="utf-8",
    )


def test_throughput_hold_expires_to_zero(tmp_path):
    stats = tmp_path / "nrMAC_stats.log"
    ingest = MacStatsIngest(stats)
    ingest.tput_hold_s = 0.08

    _write_stats(stats, tx=1_000_000)
    s1 = ingest.poll()
    assert s1 is not None

    time.sleep(0.05)
    _write_stats(stats, tx=2_000_000)
    s2 = ingest.poll()
    assert s2 is not None
    assert s2.throughput_fresh is True
    assert s2.throughput_mbps > 0

    time.sleep(0.02)
    _write_stats(stats, tx=2_000_000)  # no new bytes
    s3 = ingest.poll()
    assert s3 is not None
    assert s3.throughput_fresh is False
    assert s3.throughput_mbps > 0  # still holding

    time.sleep(0.12)
    _write_stats(stats, tx=2_000_000)
    s4 = ingest.poll()
    assert s4 is not None
    assert s4.throughput_fresh is False
    assert s4.throughput_mbps == 0.0


def test_clear_throughput_hold(tmp_path):
    stats = tmp_path / "nrMAC_stats.log"
    ingest = MacStatsIngest(stats)
    ingest.tput_hold_s = 10.0
    _write_stats(stats, tx=1_000_000)
    ingest.poll()
    time.sleep(0.05)
    _write_stats(stats, tx=2_000_000)
    ingest.poll()
    ingest.clear_throughput_hold()
    _write_stats(stats, tx=2_000_000)
    s = ingest.poll()
    assert s is not None
    assert s.throughput_mbps == 0.0


def test_parse_dl_mcs_ul_snr_rsrp():
    """Fields added for the demo GUIs, on a real gNB dump (RF-sim, 2026-10-13)."""
    from common.oai.net.metrics_ingest import MacStatsIngest

    text = (
        "UE RNTI 2e6f CU-UE-ID 1 in-sync PH 50 dB PCMAX 20 dBm, average RSRP -43 (32 meas)\n"
        "UE 2e6f: CQI 15, RI 1, PMI (0,0)\n"
        "UE 2e6f: dlsch_rounds 197166/7509/0/0, dlsch_errors 0, pucch0_DTX 0, BLER 0.07680 MCS (0) 25 CCE fail 5\n"
        "UE 2e6f: ulsch_rounds 15536/0/0/0, ulsch_errors 0, ulsch_DTX 0, BLER 0.00000 MCS (0) 20 "
        "(Qm 6 deltaMCS 0 dB) NPRB 8  SNR 51.0 dB CCE fail 70\n"
        "UE 2e6f: MAC:    TX      808259737 RX        7192231 bytes\n"
    )
    s = MacStatsIngest._parse(text)
    assert (s.dl_mcs, s.ul_snr_db, s.rsrp_dbm) == (25.0, 51.0, -43.0)
    assert (s.cqi, s.bler, s.tx_bytes) == (15, 0.0768, 808259737)
