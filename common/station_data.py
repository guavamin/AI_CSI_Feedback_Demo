"""What each station reads on its own laptop: log followers, metric parsers and the channel workers.

UE station  (UeChannelWorker):  the UE's own CSI-RS channel estimates (H_*.bin under --csi-record-path) and the
                                PMI it reports (csi_reports.csv): measured channel, best beam per 4-RB group.
gNB station (GnbChannelWorker): what the gNB received (its --csi-record-path): the AI latents as received on
                                PUCCH (gnb_ai_fb_latents.csv, OAI gui-live-switch) decoded here with the decoder
                                the gNB used, and the legacy PMI reports (gnb_csi_feedback.csv): the channel the
                                gNB reconstructs and the beam it transmits per 4-RB group.
Neither worker sees the other side's data.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from common.oai.net.csirs_ingest import list_h_bins
from common.replay.engine import codebook_beam, dominant_beams, read_h_dump, truth_cfr
from common.replay.native import NativeTransformer, build_library

log = logging.getLogger(__name__)
ANSI = re.compile(r"\x1b\[[0-9;]*m")
HISTORY = 120  # reports kept for the "recent reports" heat maps
AI_NEAR_S = 0.1  # gNB: a PMI line this close to an AI latent belongs to AI feedback, not legacy
MIN_SPACING_S = 0.08  # gNB history: at most one report per 80 ms (the UE dumps about that often)


def beam_phase_deg(beams: np.ndarray) -> np.ndarray:
    """Beam(s) [..., tx=2] -> phase of TX1 relative to TX0 in degrees (what the beam plots draw)."""
    return np.degrees(np.angle(beams[..., 1] * np.conj(beams[..., 0])))


class FileTail:
    """New complete lines of a growing text file; starts over when the file is replaced or truncated."""

    def __init__(self, path: Path | None = None, from_end: bool = False):
        self.path, self.pos, self._from_end = path, 0, from_end

    def follow(self, path: Path | None, from_end: bool = False) -> None:
        if path != self.path:
            self.path, self.pos, self._from_end = path, 0, from_end

    def lines(self) -> list[str]:
        if self.path is None:
            return []
        try:
            size = self.path.stat().st_size
            if self._from_end:
                self.pos, self._from_end = size, False
            if size < self.pos:
                self.pos = 0
            with self.path.open("rb") as fh:
                fh.seek(self.pos)
                chunk = fh.read(4 << 20)
        except OSError:
            return []
        complete = chunk[: chunk.rfind(b"\n") + 1]
        self.pos += len(complete)
        return ANSI.sub("", complete.decode("utf-8", errors="replace")).splitlines()


# ---------------------------------------------------------------------------------------------- metrics
_IPERF_RATE = re.compile(r"\]\s+([\d.]+)-([\d.]+)\s+sec\s+[\d.]+\s+\w?Bytes\s+([\d.]+)\s+([KMG]?)bits/sec")
_UNIT = {"": 1e-6, "K": 1e-3, "M": 1.0, "G": 1e3}
_DL_DECODE = re.compile(r"DL decode:\s*(\d+)/(\d+)")
_CSI_LINE = re.compile(r"RSRP = (-?\d+) dBm, RI = (\d+).*?i2 = (\d+), SINR = (-?\d+) dB, CQI = (\d+)")


def iperf_rate_mbps(line: str) -> float | None:
    """One iperf (2) one-second interval line -> Mbit/s; None for anything else (headers, the summary)."""
    m = _IPERF_RATE.search(line)
    if not m or float(m.group(2)) - float(m.group(1)) > 1.5:
        return None
    return float(m.group(3)) * _UNIT[m.group(4)]


@dataclass
class UeLinkStats:
    """UE-side numbers of the last second (NaN: not seen)."""

    tput_mbps: float = float("nan")
    fail_rate: float = float("nan")
    sinr_db: float = float("nan")
    rsrp_dbm: float = float("nan")
    cqi: float = float("nan")


class UeStatsReader:
    """Reads the UE station's own logs once a second: iperf server (received rate), UE MAC stats (decoded /
    failed DL transport blocks, OAI gui-live-switch) and the UE's CSI report lines (SINR, RSRP, CQI)."""

    def __init__(self):
        self.ue, self.iperf = FileTail(), FileTail()
        self._dec: tuple[int, int] | None = None

    def poll(self, ue_log: Path | None, iperf_log: Path | None) -> UeLinkStats:
        self.ue.follow(ue_log, from_end=True)
        self.iperf.follow(iperf_log, from_end=True)
        out = UeLinkStats()
        rates = [r for r in map(iperf_rate_mbps, self.iperf.lines()) if r is not None]
        if rates:
            out.tput_mbps = rates[-1]
        sinr, rsrp, cqi = [], [], []
        for line in self.ue.lines():
            m = _DL_DECODE.search(line)
            if m:
                ok, fail = int(m.group(1)), int(m.group(2))
                if self._dec is not None and ok >= self._dec[0] and fail >= self._dec[1]:
                    d_ok, d_fail = ok - self._dec[0], fail - self._dec[1]
                    if d_ok + d_fail > 0:
                        out.fail_rate = d_fail / (d_ok + d_fail)
                self._dec = (ok, fail)
                continue
            m = _CSI_LINE.search(line)
            if m and int(m.group(5)) > 0:  # CQI 0: no valid measurement (UE resyncing)
                rsrp.append(int(m.group(1)))
                sinr.append(int(m.group(4)))
                cqi.append(int(m.group(5)))
        if sinr:
            out.sinr_db, out.rsrp_dbm, out.cqi = float(np.mean(sinr)), float(np.mean(rsrp)), float(np.mean(cqi))
        return out

    def reset(self) -> None:
        self._dec = None


# ---------------------------------------------------------------------------------------------- channel views
@dataclass
class ChannelView:
    """Latest channel and the recent history, as one station knows it. cfr: [rx, tx, 612] complex."""

    n_reports: int = 0
    error: str = ""
    cfr: np.ndarray | None = None  # UE: measured; gNB: reconstructed (None: last report was legacy)
    beam_deg: np.ndarray | None = None  # [13] per 4-RB group: UE best beam / gNB transmitted beam
    pmi_deg: float | None = None  # UE: phase of the reported PMI (legacy); gNB: legacy codebook beam
    hist_mag: np.ndarray = field(default_factory=lambda: np.zeros((0, 612)))  # |H| RX0-TX0 (NaN rows: legacy)
    hist_beam: np.ndarray = field(default_factory=lambda: np.zeros((0, 13)))  # per report, oldest first
    hist_t: list = field(default_factory=list)  # wall-clock seconds of each history row
    kind: str = ""  # gNB: legacy | same | cross for the latest report


def _stack(rows, width):
    return np.stack(rows) if rows else np.zeros((0, width))


class UeChannelWorker:
    """UE station: follows its own record dir, keeps only the newest few dumps on disk (RAM disk)."""

    def __init__(self, record_root: Callable[[], str], keep_h: int = 4, period_s: float = 0.1):
        self._root_fn, self._keep, self._period = record_root, keep_h, period_s
        self._lock, self._stop = threading.Lock(), threading.Event()
        self._hist: deque = deque(maxlen=HISTORY)
        self._view = ChannelView()
        self._last: Path | None = None
        self._reports = FileTail()
        self._i2: dict[str, int] = {}
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="ue-channel", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def clear(self) -> None:
        with self._lock:
            self._hist.clear()
            self._view = ChannelView()
            self._i2.clear()

    def view(self) -> ChannelView:
        with self._lock:
            return self._view

    def _run(self) -> None:
        while not self._stop.wait(self._period):
            try:
                self._step()
            except Exception as exc:  # noqa: BLE001 - a half-written file must not stop the view
                log.debug("ue channel step: %s", exc)

    def _step(self) -> None:
        root_s = self._root_fn()
        if not root_s:
            return
        root = Path(root_s)
        bins = list_h_bins(root)
        for old in bins[: -self._keep]:
            old.unlink(missing_ok=True)
        if not bins or bins[-1] == self._last:
            return
        path = bins[-1]
        dump = read_h_dump(path)
        if dump is None or int(np.abs(dump.iq).max()) < 4:
            return
        self._last = path
        self._reports.follow(root / "csi_reports.csv")
        for line in self._reports.lines():
            if '"' in line:
                _, name, tail = line.split('"', 2)
                f = tail.strip(",").split(",")  # rsrp, ri, i1_0, i1_1, i1_2, i2, cqi, sinr
                if len(f) >= 6:
                    self._i2[Path(name).name] = int(f[5])
        if len(self._i2) > 4000:
            self._i2 = dict(list(self._i2.items())[-1000:])
        cfr = truth_cfr(dump.iq)
        beam = beam_phase_deg(dominant_beams(cfr))
        i2 = self._i2.get(path.name)
        with self._lock:
            self._hist.append((np.abs(cfr[0, 0]).astype(np.float32), beam, dump.timestamp_utc_us / 1e6))
            self._view = ChannelView(
                n_reports=self._view.n_reports + 1, cfr=cfr, beam_deg=beam,
                pmi_deg=None if i2 is None else float(beam_phase_deg(codebook_beam(i2))),
                hist_mag=_stack([h[0] for h in self._hist], 612), hist_beam=_stack([h[1] for h in self._hist], 13),
                hist_t=[h[2] for h in self._hist])


class GnbChannelWorker:
    """gNB station: decodes the latents the gNB received with the model the gNB used for each of them."""

    def __init__(self, record_root: Callable[[], str], oai_dir: Path, oai_build: Path,
                 decoders: dict[int, Path], period_s: float = 0.1):
        self._root_fn, self._period = record_root, period_s
        self._oai, self._build, self._decoders = Path(oai_dir), Path(oai_build), decoders
        self._lock, self._stop = threading.Lock(), threading.Event()
        self._hist: deque = deque(maxlen=HISTORY)
        self._view = ChannelView()
        self._latents, self._legacy = FileTail(), FileTail()
        self._ai_times: deque = deque(maxlen=4000)  # wall-clock times of recent AI latents
        self._pending: list = []
        self._last_t = 0.0
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="gnb-channel", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def clear(self) -> None:
        with self._lock:
            self._hist.clear()
            self._view = ChannelView()

    def view(self) -> ChannelView:
        with self._lock:
            return self._view

    def _run(self) -> None:
        try:  # one thread owns the native library (its model slot is thread-local)
            tf = NativeTransformer(build_library(self._oai, self._build))
            for slot, path in self._decoders.items():
                tf.load(slot, path)
        except Exception as exc:  # noqa: BLE001
            log.exception("gNB decoder replay unavailable")
            with self._lock:
                self._view = ChannelView(error=f"decoders unavailable: {exc}")
            return
        while not self._stop.wait(self._period):
            try:
                self._step(tf)
            except Exception as exc:  # noqa: BLE001
                log.debug("gnb channel step: %s", exc)

    def _step(self, tf: NativeTransformer) -> None:
        root_s = self._root_fn()
        if not root_s:
            return
        root = Path(root_s)
        self._latents.follow(root / "gnb_ai_fb_latents.csv")
        self._legacy.follow(root / "gnb_csi_feedback.csv")
        for line in self._latents.lines():  # timestamp_utc_us,frame,slot,rnti,decoder,decoded,cqi,latent_hex
            f = line.split(",")
            if len(f) == 8 and f[0].isdigit() and f[5] == "1":
                self._pending.append(("ai", int(f[0]) / 1e6, int(f[4]), bytes.fromhex(f[7])))
            if len(f) == 8 and f[0].isdigit():
                self._ai_times.append(int(f[0]) / 1e6)
        for line in self._legacy.lines():  # timestamp_utc_us,frame,slot,rnti,cri,ri,li,pmi_x1,pmi_x2,...
            f = line.split(",")
            if len(f) >= 9 and f[0].isdigit():
                self._pending.append(("pmi", int(f[0]) / 1e6, int(f[8]), b""))
        # The gNB writes a PMI line for every CSI report it processes, AI ones included (all zero; over the air
        # also a second one in the next slot). A PMI line is legacy only when no AI latent came within
        # AI_NEAR_S of it (AI latents arrive every 40 ms, in Legacy there are none). Lines are classified once
        # both files have surely been read past them.
        cutoff = time.time() - 0.5
        ready = sorted((r for r in self._pending if r[1] < cutoff), key=lambda r: r[1])
        self._pending = [r for r in self._pending if r[1] >= cutoff]
        ai_t = np.array(self._ai_times)

        def near_ai(t):
            i = np.searchsorted(ai_t, t)
            return any(0 <= j < ai_t.size and abs(ai_t[j] - t) < AI_NEAR_S for j in (i - 1, i))

        rows = [r for r in ready if r[0] == "ai" or not near_ai(r[1])]
        t_from = rows[-1][1] - 1.2 * HISTORY * MIN_SPACING_S if rows else 0.0  # first read of a long file
        for kind, t, value, payload in (r for r in rows if r[1] >= t_from):
            if t - self._last_t < MIN_SPACING_S:
                continue  # the gNB receives reports far more often than the CSI-RS changes: keep ~12 / s
            self._last_t = t
            if kind == "ai":
                tf.use(value if value in self._decoders else 0)
                cfr = tf.reconstruct(payload)
                tf.use(0)
                beam, mag, pmi = beam_phase_deg(dominant_beams(cfr)), np.abs(cfr[0, 0]).astype(np.float32), None
                key = "cross" if value == 1 else "same"
            else:
                cfr, key = None, "legacy"
                pmi = float(beam_phase_deg(codebook_beam(value)))
                beam, mag = np.full(13, pmi), np.full(612, np.nan, dtype=np.float32)
            with self._lock:
                self._hist.append((mag, beam, t))
                self._view = ChannelView(
                    n_reports=self._view.n_reports + 1, cfr=cfr, beam_deg=beam, pmi_deg=pmi, kind=key,
                    hist_mag=_stack([h[0] for h in self._hist], 612), hist_beam=_stack([h[1] for h in self._hist], 13),
                    hist_t=[h[2] for h in self._hist])


def wait_for(cond: Callable[[], bool], timeout_s: float, period_s: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(period_s)
    return cond()
