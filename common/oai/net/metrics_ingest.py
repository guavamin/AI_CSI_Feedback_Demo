"""Parse gNB nrMAC_stats.log into GUI metrics samples."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# From openair2/LAYER2/NR_MAC_gNB/main.c dump_mac_stats()
_RE_CSI = re.compile(
    r"UE\s+([0-9a-fA-F]+):\s+CQI\s+(\d+),\s+RI\s+(\d+),\s+PMI\s+\((-?\d+),(-?\d+)\)"
)
_RE_DL_BLER = re.compile(
    r"UE\s+([0-9a-fA-F]+):\s+dlsch_rounds.*?BLER\s+([0-9.]+)\s+MCS"
)
_RE_MAC_BYTES = re.compile(
    r"UE\s+([0-9a-fA-F]+):\s+MAC:\s+TX\s+(\d+)\s+RX\s+(\d+)\s+bytes"
)
_RE_IN_SYNC = re.compile(r"UE RNTI\s+([0-9a-fA-F]+).*?\bin-sync\b", re.IGNORECASE)
# Added for the demo GUIs: DL MCS, UL SNR and the UE-reported RSRP of the same dump
_RE_DL_MCS = re.compile(r"UE\s+[0-9a-fA-F]+:\s+dlsch_rounds.*?BLER\s+[0-9.]+\s+MCS\s+\(\d+\)\s+(\d+)")
_RE_UL_SNR = re.compile(r"UE\s+[0-9a-fA-F]+:\s+ulsch_rounds.*?SNR\s+(-?[0-9.]+)\s+dB")
_RE_RSRP = re.compile(r"UE RNTI\s+[0-9a-fA-F]+.*?average RSRP\s+(-?\d+)")


@dataclass
class MacStatsSample:
    rnti: str = ""
    attached: bool = False
    ri: int = 0
    pmi_x1: int = 0
    pmi_x2: int = 0
    cqi: int = 0
    bler: float = float("nan")
    throughput_mbps: float = float("nan")
    # True only when TX byte counter advanced since the previous poll
    throughput_fresh: bool = False
    tx_bytes: int = 0
    rx_bytes: int = 0
    mtime_s: float = 0.0
    dl_mcs: float = float("nan")
    ul_snr_db: float = float("nan")
    rsrp_dbm: float = float("nan")


class MacStatsIngest:
    """Poll nrMAC_stats.log (rewritten ~10 Hz by gNB when built with short stats sleep)."""

    def __init__(self, stats_path: Path | str):
        self.stats_path = Path(stats_path)
        self._last_tx: int | None = None
        self._last_t: float | None = None
        self._last_fresh_t: float | None = None
        self._last_good_tput: float = float("nan")
        self._last_sample = MacStatsSample()
        # Brief hold on stale polls while traffic is live (avoid 0 flicker between stats dumps)
        self.tput_hold_s: float = 0.25

    def clear_throughput_hold(self) -> None:
        """Drop held throughput so the UI does not look like traffic is still running."""
        self._last_good_tput = float("nan")
        self._last_fresh_t = None

    def poll(self) -> MacStatsSample | None:
        path = self.stats_path
        if not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            mtime = path.stat().st_mtime
        except OSError as exc:
            log.debug("Could not read %s: %s", path, exc)
            return None
        if not text.strip():
            return None

        sample = self._parse(text)
        sample.mtime_s = mtime
        now = time.monotonic()

        # Only treat throughput as a new sample when TX bytes increased.
        # Polling faster than the gNB rewrite used to yield dtx=0 →
        # fake 0 Mbps points that pulled CDF means far below live rates.
        if self._last_tx is not None and self._last_t is not None:
            dt = now - self._last_t
            dtx = sample.tx_bytes - self._last_tx
            if dt > 0.05 and dtx > 0:
                sample.throughput_mbps = (dtx * 8.0) / (dt * 1e6)
                sample.throughput_fresh = True
                self._last_good_tput = sample.throughput_mbps
                self._last_fresh_t = now
            elif dtx < 0:
                # Counter reset (gNB restart)
                sample.throughput_mbps = float("nan")
                sample.throughput_fresh = False
                self._last_good_tput = float("nan")
                self._last_fresh_t = None
            else:
                # No new bytes: hold briefly, then clear so Stop traffic looks idle
                hold = max(0.0, float(self.tput_hold_s))
                age = (
                    now - self._last_fresh_t
                    if self._last_fresh_t is not None
                    else hold + 1.0
                )
                if (
                    hold > 0.0
                    and age <= hold
                    and self._last_good_tput == self._last_good_tput
                ):
                    sample.throughput_mbps = self._last_good_tput
                else:
                    sample.throughput_mbps = 0.0
                    self._last_good_tput = float("nan")
                sample.throughput_fresh = False
        else:
            sample.throughput_mbps = float("nan")
            sample.throughput_fresh = False

        self._last_tx = sample.tx_bytes
        self._last_t = now
        self._last_sample = sample
        return sample

    @staticmethod
    def _parse(text: str) -> MacStatsSample:
        sample = MacStatsSample()
        # Prefer the last UE block in the file
        csi_matches = list(_RE_CSI.finditer(text))
        bler_matches = list(_RE_DL_BLER.finditer(text))
        mac_matches = list(_RE_MAC_BYTES.finditer(text))
        sync_matches = list(_RE_IN_SYNC.finditer(text))

        if sync_matches:
            sample.attached = True
            sample.rnti = sync_matches[-1].group(1).lower()
        elif csi_matches or bler_matches or mac_matches:
            sample.attached = True

        if csi_matches:
            m = csi_matches[-1]
            sample.rnti = m.group(1).lower()
            sample.cqi = int(m.group(2))
            sample.ri = int(m.group(3))
            sample.pmi_x1 = int(m.group(4))
            sample.pmi_x2 = int(m.group(5))

        if bler_matches:
            m = bler_matches[-1]
            if not sample.rnti:
                sample.rnti = m.group(1).lower()
            sample.bler = float(m.group(2))

        if mac_matches:
            m = mac_matches[-1]
            if not sample.rnti:
                sample.rnti = m.group(1).lower()
            sample.tx_bytes = int(m.group(2))
            sample.rx_bytes = int(m.group(3))

        for regex, attr in ((_RE_DL_MCS, "dl_mcs"), (_RE_UL_SNR, "ul_snr_db"), (_RE_RSRP, "rsrp_dbm")):
            found = regex.findall(text)
            if found:
                setattr(sample, attr, float(found[-1]))

        return sample
