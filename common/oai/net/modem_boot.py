"""gNB softmodem boot stages from stdout (cmd → config → RU → RF/SSB TX)."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_RE_CONFIG = re.compile(
    r"Initializing gNB RAN context|Initializing NR L1|Initialized RAN Context",
    re.IGNORECASE,
)
_RE_RU = re.compile(
    r"Starting RU|Waiting for reference lock|RU clock source|Initialized RU proc",
    re.IGNORECASE,
)
# RF streaming started ⇒ DL (incl. SSB) is on the air
_RE_RF_SSB = re.compile(
    r"RU \d+ RF started|RU \d+ rf device ready|got sync \(ru_thread\)|got sync \(L1_stats_thread\)",
    re.IGNORECASE,
)


class GnbBootStage(str, Enum):
    PROCESS_DOWN = "process_down"
    STARTING = "starting"
    CONFIG = "config"
    RU_INIT = "ru_init"
    RF_SSB = "rf_ssb"  # RF up — SSB burst transmitting


_STAGE_ORDER = [
    GnbBootStage.STARTING,
    GnbBootStage.CONFIG,
    GnbBootStage.RU_INIT,
    GnbBootStage.RF_SSB,
]

# Short GUI labels
GNB_STAGE_LABEL = {
    GnbBootStage.PROCESS_DOWN: "stopped",
    GnbBootStage.STARTING: "starting",
    GnbBootStage.CONFIG: "config L1",
    GnbBootStage.RU_INIT: "RU / clock lock",
    GnbBootStage.RF_SSB: "SSB TX",
}


@dataclass
class GnbBootStatus:
    stage: GnbBootStage = GnbBootStage.PROCESS_DOWN
    detail: str = ""
    label: str = "stopped"


class GnbBootMonitor:
    """Incremental gNB log scanner for boot progress up to RF/SSB TX."""

    def __init__(self) -> None:
        self._log_path: Path | None = None
        self._offset = 0
        self._stage = GnbBootStage.PROCESS_DOWN
        self._detail = ""

    @property
    def stage(self) -> GnbBootStage:
        return self._stage

    def reset(self, log_path: Path | str | None = None, *, follow_new_only: bool = True) -> None:
        self._stage = GnbBootStage.STARTING
        self._detail = "nr-softmodem started — loading config"
        if log_path is not None:
            self._log_path = Path(log_path)
        if self._log_path is not None and self._log_path.is_file():
            try:
                size = self._log_path.stat().st_size
            except OSError:
                size = 0
            self._offset = size if follow_new_only else 0
        else:
            self._offset = 0
        log.info("gNB boot monitor reset → %s", self._stage.value)

    def mark_process_down(self) -> GnbBootStatus:
        self._stage = GnbBootStage.PROCESS_DOWN
        self._detail = "gNB process not running"
        return self.status()

    def poll(self, *, process_running: bool, log_path: Path | str | None = None) -> GnbBootStatus:
        if log_path is not None:
            path = Path(log_path)
            if self._log_path != path:
                self._log_path = path
                if self._stage == GnbBootStage.PROCESS_DOWN and process_running:
                    self.reset(path, follow_new_only=False)

        if not process_running:
            return self.mark_process_down()

        if self._stage == GnbBootStage.PROCESS_DOWN:
            self.reset(self._log_path, follow_new_only=False)

        self._ingest_new_log()
        return self.status()

    def status(self) -> GnbBootStatus:
        return GnbBootStatus(
            stage=self._stage,
            detail=self._detail,
            label=GNB_STAGE_LABEL.get(self._stage, self._stage.value),
        )

    def _set_stage(self, stage: GnbBootStage, detail: str) -> None:
        try:
            new_rank = _STAGE_ORDER.index(stage)
        except ValueError:
            new_rank = -1
        try:
            cur_rank = _STAGE_ORDER.index(self._stage)
        except ValueError:
            cur_rank = -1
        if new_rank <= cur_rank:
            if stage == self._stage and detail:
                self._detail = detail
            return
        self._stage = stage
        self._detail = detail
        log.info("gNB boot → %s (%s)", stage.value, detail)

    def _ingest_new_log(self) -> None:
        path = self._log_path
        if path is None or not path.is_file():
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < self._offset:
            self._offset = 0
        if size == self._offset:
            return
        try:
            with path.open("rb") as fh:
                fh.seek(self._offset)
                raw = fh.read()
                self._offset = fh.tell()
        except OSError as exc:
            log.debug("gNB log read failed: %s", exc)
            return
        if not raw:
            return
        text = _ANSI_RE.sub("", raw.decode("utf-8", errors="replace"))
        for line in text.splitlines():
            self._consume_line(line)

    def _consume_line(self, line: str) -> None:
        if _RE_RF_SSB.search(line):
            self._set_stage(GnbBootStage.RF_SSB, "RF streaming — SSB burst on air")
            return
        if _RE_RU.search(line):
            self._set_stage(GnbBootStage.RU_INIT, "Starting RU / waiting clock lock")
            return
        if _RE_CONFIG.search(line):
            self._set_stage(GnbBootStage.CONFIG, "Loading RAN / L1 config")
