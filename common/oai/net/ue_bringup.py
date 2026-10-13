"""Stage-aware UE bring-up monitor (SSB → RACH → RRC → PDU → TUN).

Parses the UE softmodem stdout log for progress markers and reports when the
current stage has stalled past a configured timeout so traffic supervision can
restart with evidence instead of a blind sleep.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Progress markers observed in nr-uesoftmodem logs (Demo-gui / OAI NR UE).
_RE_INIT = re.compile(
    r"Initializing MAC|SA init parameters|create TASK_RRC_NRUE",
    re.IGNORECASE,
)
_RE_RADIO = re.compile(
    r"Waiting for reference lock|Configuring channel|starting streaming|Initialize CODEC",
    re.IGNORECASE,
)
_RE_SEARCH = re.compile(
    r"Starting sync detection|Running Initial Synch|Starting cell search|Scanning GSCN|SSB position provided",
    re.IGNORECASE,
)
_RE_SSB = re.compile(
    r"Initial sync successful|UE synchronized!|pbch decoded sucessfully|Cell Detected",
    re.IGNORECASE,
)
_RE_RACH = re.compile(
    r"RAR-Msg2 decoded|Got RAPID RAR|Received NR_RRCSetup|RA-Msg3 transmitted",
    re.IGNORECASE,
)
_RE_RRC = re.compile(r"State\s*=\s*NR_RRC_CONNECTED")
_RE_PDU = re.compile(
    r"Received PDU Session Establishment Accept,\s*UE IPv4:\s*"
    r"(\d{1,3}(?:\.\d{1,3}){3})",
    re.IGNORECASE,
)
_RE_TUN = re.compile(
    r"TUN Interface\s+(\S+)\s+successfully configured,\s*IPv4\s+"
    r"(\d{1,3}(?:\.\d{1,3}){3})",
    re.IGNORECASE,
)


class BringupStage(str, Enum):
    PROCESS_DOWN = "process_down"
    STARTING = "starting"
    INIT = "init"
    RADIO = "radio"
    SEARCHING_SSB = "searching_ssb"
    WAITING_RACH = "waiting_rach"
    WAITING_RRC = "waiting_rrc"
    WAITING_PDU = "waiting_pdu"
    WAITING_TUN = "waiting_tun"
    READY = "ready"
    # Historical name for "not yet past SSB sync" (alias of STARTING)
    WAITING_SSB = STARTING


_STAGE_ORDER = [
    BringupStage.STARTING,
    BringupStage.INIT,
    BringupStage.RADIO,
    BringupStage.SEARCHING_SSB,
    BringupStage.WAITING_RACH,
    BringupStage.WAITING_RRC,
    BringupStage.WAITING_PDU,
    BringupStage.WAITING_TUN,
    BringupStage.READY,
]

UE_STAGE_LABEL = {
    BringupStage.PROCESS_DOWN: "stopped",
    BringupStage.STARTING: "starting",
    BringupStage.INIT: "init MAC/PHY",
    BringupStage.RADIO: "radio / clock",
    BringupStage.SEARCHING_SSB: "searching SSB",
    BringupStage.WAITING_RACH: "SSB OK · RACH",
    BringupStage.WAITING_RRC: "RACH · RRC",
    BringupStage.WAITING_PDU: "RRC · PDU",
    BringupStage.WAITING_TUN: "PDU · TUN",
    BringupStage.READY: "PDU IP ready",
}


@dataclass
class BringupTimeouts:
    ssb_s: float = 15.0
    rach_s: float = 20.0
    rrc_s: float = 15.0
    pdu_s: float = 30.0
    tun_s: float = 10.0

    def for_stage(self, stage: BringupStage) -> float:
        return {
            BringupStage.STARTING: self.ssb_s,
            BringupStage.INIT: self.ssb_s,
            BringupStage.RADIO: self.ssb_s,
            BringupStage.SEARCHING_SSB: self.ssb_s,
            BringupStage.WAITING_RACH: self.rach_s,
            BringupStage.WAITING_RRC: self.rrc_s,
            BringupStage.WAITING_PDU: self.pdu_s,
            BringupStage.WAITING_TUN: self.tun_s,
            BringupStage.READY: 0.0,
            BringupStage.PROCESS_DOWN: 0.0,
        }.get(stage, 0.0)


@dataclass
class BringupStatus:
    stage: BringupStage = BringupStage.PROCESS_DOWN
    detail: str = ""
    ue_ip: str = ""
    stalled: bool = False
    stage_age_s: float = 0.0
    timeout_s: float = 0.0
    label: str = "stopped"


class UeBringupMonitor:
    """Incremental UE log scanner with monotonic bring-up stages."""

    def __init__(self, timeouts: BringupTimeouts | None = None):
        self.timeouts = timeouts or BringupTimeouts()
        self._log_path: Path | None = None
        self._offset = 0
        self._stage = BringupStage.PROCESS_DOWN
        self._stage_entered_mono = 0.0
        self._ue_ip = ""
        self._detail = ""

    @property
    def stage(self) -> BringupStage:
        return self._stage

    @property
    def ue_ip(self) -> str:
        return self._ue_ip

    def reset(self, log_path: Path | str | None = None, *, follow_new_only: bool = True) -> None:
        """Reset stages for a new UE boot. Optionally skip existing log bytes."""
        self._stage = BringupStage.STARTING
        self._stage_entered_mono = time.monotonic()
        self._ue_ip = ""
        self._detail = "nr-uesoftmodem started — initializing"
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
        log.info(
            "UE bring-up monitor reset → %s (log=%s offset=%d)",
            self._stage.value,
            self._log_path,
            self._offset,
        )

    def mark_process_down(self) -> BringupStatus:
        self._stage = BringupStage.PROCESS_DOWN
        self._stage_entered_mono = time.monotonic()
        self._ue_ip = ""
        self._detail = "UE process not running"
        return self.status()

    def poll(self, *, process_running: bool, log_path: Path | str | None = None) -> BringupStatus:
        """Advance stages from new log lines; set ``stalled`` if the stage timed out."""
        if log_path is not None:
            path = Path(log_path)
            if self._log_path != path:
                # New log file (new GUI session) — scan this file for markers
                self._log_path = path
                if self._stage == BringupStage.PROCESS_DOWN and process_running:
                    self.reset(path, follow_new_only=False)

        if not process_running:
            return self.mark_process_down()

        if self._stage == BringupStage.PROCESS_DOWN:
            # Process came back without an explicit reset() — scan existing log so an
            # already-booted UE is recognized (SSB…TUN markers already present).
            self.reset(self._log_path, follow_new_only=False)

        self._ingest_new_log()
        return self.status()

    def status(self) -> BringupStatus:
        now = time.monotonic()
        age = max(0.0, now - self._stage_entered_mono) if self._stage_entered_mono else 0.0
        timeout = float(self.timeouts.for_stage(self._stage))
        stalled = False
        detail = self._detail
        if (
            self._stage not in (BringupStage.READY, BringupStage.PROCESS_DOWN)
            and timeout > 0.0
            and age >= timeout
        ):
            stalled = True
            detail = (
                f"Stalled in {self._stage.value} for {age:.0f}s "
                f"(limit {timeout:.0f}s) — UE re-init recommended"
            )
        return BringupStatus(
            stage=self._stage,
            detail=detail,
            ue_ip=self._ue_ip,
            stalled=stalled,
            stage_age_s=age,
            timeout_s=timeout,
            label=UE_STAGE_LABEL.get(self._stage, self._stage.value),
        )

    def force_at_least(self, stage: BringupStage, detail: str = "") -> None:
        """Advance to ``stage`` if we are still behind it (e.g. radio attach evidence)."""
        if self._stage_rank(stage) > self._stage_rank(self._stage):
            self._set_stage(stage, detail or f"Advanced to {stage.value}")

    def _set_stage(self, stage: BringupStage, detail: str) -> None:
        if self._stage_rank(stage) <= self._stage_rank(self._stage):
            # Monotonic: ignore regressions / duplicates
            if stage == self._stage and detail:
                self._detail = detail
            return
        self._stage = stage
        self._stage_entered_mono = time.monotonic()
        self._detail = detail
        log.info("UE bring-up → %s (%s)", stage.value, detail)

    @staticmethod
    def _stage_rank(stage: BringupStage) -> int:
        try:
            return _STAGE_ORDER.index(stage)
        except ValueError:
            return -1

    def _ingest_new_log(self) -> None:
        path = self._log_path
        if path is None or not path.is_file():
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < self._offset:
            # Log truncated (UE restart into same file) — rescan from start
            self._offset = 0
        if size == self._offset:
            return
        try:
            with path.open("rb") as fh:
                fh.seek(self._offset)
                raw = fh.read()
                self._offset = fh.tell()
        except OSError as exc:
            log.debug("UE log read failed: %s", exc)
            return
        if not raw:
            return
        text = _ANSI_RE.sub("", raw.decode("utf-8", errors="replace"))
        for line in text.splitlines():
            self._consume_line(line)

    def _consume_line(self, line: str) -> None:
        if _RE_SSB.search(line):
            self._set_stage(BringupStage.WAITING_RACH, "SSB / initial sync OK — waiting for RACH")
            return
        if _RE_SEARCH.search(line):
            self._set_stage(BringupStage.SEARCHING_SSB, "Looking for SSB / initial synch")
            return
        if _RE_RADIO.search(line):
            self._set_stage(BringupStage.RADIO, "Radio / USRP up — preparing sync")
            return
        if _RE_INIT.search(line):
            self._set_stage(BringupStage.INIT, "Initializing MAC / PHY / RRC")
            return
        if _RE_RACH.search(line):
            self._set_stage(BringupStage.WAITING_RRC, "RACH progress — waiting for RRC Connected")
            return
        if _RE_RRC.search(line):
            self._set_stage(BringupStage.WAITING_PDU, "RRC Connected — waiting for PDU session")
            return
        m_pdu = _RE_PDU.search(line)
        if m_pdu:
            self._ue_ip = m_pdu.group(1)
            self._set_stage(
                BringupStage.WAITING_TUN,
                f"PDU Accept (UE IPv4 {self._ue_ip}) — waiting for TUN iface",
            )
            return
        m_tun = _RE_TUN.search(line)
        if m_tun:
            self._ue_ip = m_tun.group(2)
            self._set_stage(
                BringupStage.READY,
                f"TUN {m_tun.group(1)} up with IPv4 {self._ue_ip}",
            )
