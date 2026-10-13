"""Background replay of the UE's live channel reports for the GUI.

Follows the newest H_*.bin in the UE's --csi-record-path (the controller keeps only the last few) and the
UE's csi_reports.csv (PMI i2 per report, for the legacy score), replays each new report through all three
scenarios (common.replay.engine) and keeps what the GUI draws: the latest channel and reconstructions, the
last N reports of RX0-TX0, the latent bits, and the beam-cosine samples behind the CDF. One thread owns the
native library (its model slot is thread-local).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from common.oai.net.csirs_ingest import list_h_bins
from common.replay.engine import ReplayEngine, ReplayResult, read_h_dump

log = logging.getLogger(__name__)
KEYS = ("legacy", "same", "cross")


@dataclass
class ReplayView:
    ready: bool = False
    error: str = ""
    n_reports: int = 0
    last: ReplayResult | None = None
    hist_truth: np.ndarray = field(default_factory=lambda: np.zeros((0, 612)))  # |H| RX0-TX0, oldest first
    hist_recon: dict[str, np.ndarray] = field(default_factory=dict)
    hist_codes: np.ndarray = field(default_factory=lambda: np.zeros((0, 24), dtype=np.uint8))
    hist_cqi: list[int] = field(default_factory=list)
    hist_i2: list[int] = field(default_factory=list)
    beam_cos: dict[str, np.ndarray] = field(default_factory=dict)  # all sub-band samples so far, per scenario
    ms_per_report: float = float("nan")


class ReplayWorker:
    def __init__(self, record_root: Callable[[], str], engine_factory: Callable[[], ReplayEngine],
                 history: int = 120, cdf_samples: int = 13 * 4000, period_s: float = 0.2):
        self._root_fn, self._factory = record_root, engine_factory
        self._period = period_s
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._hist: deque[tuple[np.ndarray, dict[str, np.ndarray], np.ndarray, int, int]] = deque(maxlen=history)
        self._cos = {k: deque(maxlen=cdf_samples) for k in KEYS}
        self._view = ReplayView()
        self._last_path: Path | None = None
        self._csv: Path | None = None
        self._csv_pos = 0
        self._report: dict[str, tuple[int, int]] = {}  # H file name -> (i2, cqi)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="replay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def clear(self) -> None:
        """Forget the history and the CDF samples (e.g. new session)."""
        with self._lock:
            self._hist.clear()
            for d in self._cos.values():
                d.clear()
            self._view = ReplayView(ready=self._view.ready)

    def view(self) -> ReplayView:
        with self._lock:
            return self._view

    # ------------------------------------------------------------------ worker thread
    def _run(self) -> None:
        try:
            engine = self._factory()
        except Exception as exc:  # noqa: BLE001
            log.exception("replay engine unavailable")
            with self._lock:
                self._view = ReplayView(error=f"replay unavailable: {exc}")
            return
        with self._lock:
            self._view = ReplayView(ready=True)
        while not self._stop.wait(self._period):
            try:
                self._step(engine)
            except Exception as exc:  # noqa: BLE001 - keep the GUI alive whatever a file looks like
                log.warning("replay step failed: %s", exc)

    def _read_reports(self, root: Path) -> None:
        """Incrementally read csi_reports.csv: H file name -> (PMI i2, CQI)."""
        csv_path = root / "csi_reports.csv"
        if csv_path != self._csv:
            self._csv, self._csv_pos, self._report = csv_path, 0, {}
        try:
            with csv_path.open("rb") as fh:
                fh.seek(self._csv_pos)
                chunk = fh.read()
        except OSError:
            return
        complete = chunk[: chunk.rfind(b"\n") + 1]  # the UE may be mid-line
        self._csv_pos += len(complete)
        for line in complete.decode("utf-8", errors="replace").splitlines():
            if line.startswith("timestamp_utc_us") or '"' not in line:
                continue
            head, path, tail = line.split('"', 2)
            fields = tail.strip(",").split(",")  # rsrp, ri, i1_0, i1_1, i1_2, i2, cqi, sinr
            if path and len(fields) >= 7:
                self._report[Path(path).name] = (int(fields[5]), int(fields[6]))
        if len(self._report) > 20000:  # keep the map small on long runs
            for name in list(self._report)[:10000]:
                del self._report[name]

    def _step(self, engine: ReplayEngine) -> None:
        root_s = self._root_fn()
        if not root_s:
            return
        root = Path(root_s)
        bins = list_h_bins(root)
        if not bins or bins[-1] == self._last_path:
            return
        path = bins[-1]
        dump = read_h_dump(path)
        if dump is None:
            return  # still being written, or already deleted
        self._last_path = path
        self._read_reports(root)
        i2, cqi = self._report.get(path.name, (None, -1))
        t0 = time.perf_counter()
        res = engine.process(dump, i2)
        ms = (time.perf_counter() - t0) * 1e3
        with self._lock:
            self._hist.append((np.abs(res.truth[0, 0]).astype(np.float32),
                               {k: np.abs(v[0, 0]).astype(np.float32) for k, v in res.recon.items()},
                               res.codes.astype(np.uint8), cqi, -1 if i2 is None else i2))
            for key, vals in res.beam_cos.items():
                self._cos[key].extend(vals.tolist())
            hist = list(self._hist)
            self._view = ReplayView(
                ready=True, n_reports=self._view.n_reports + 1, last=res,
                hist_truth=np.stack([h[0] for h in hist]),
                hist_recon={k: np.stack([h[1][k] for h in hist]) for k in res.recon},
                hist_codes=np.stack([h[2] for h in hist]), hist_cqi=[h[3] for h in hist],
                hist_i2=[h[4] for h in hist], beam_cos={k: np.array(d) for k, d in self._cos.items() if d},
                ms_per_report=ms)


def engine_from_config(cfg) -> Callable[[], ReplayEngine]:
    """Engine factory from a same-laptop profile: UE model and the gNB's two decoders as booted."""
    def arg(args, flag):
        return Path(args[args.index(flag) + 1])

    def make() -> ReplayEngine:
        build = Path(cfg.oai.build_dir)
        return ReplayEngine(build.parents[2], build, ue_model=arg(cfg.oai.ue.base_args, "--ai-fb-model-path"),
                            decoders={"same": arg(cfg.oai.gnb.base_args, "--ai-fb-model-path"),
                                      "cross": arg(cfg.oai.gnb.base_args, "--ai-fb-live-alt-model-path")})
    return make
