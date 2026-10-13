"""Stage-aware UE bring-up log parsing and stall detection."""

from __future__ import annotations

import time
from pathlib import Path

from common.oai.net.ue_bringup import BringupStage, BringupTimeouts, UeBringupMonitor


def _write(path: Path, text: str, *, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as fh:
        fh.write(text)


def test_advances_through_full_bringup(tmp_path):
    log = tmp_path / "ue.stdout.log"
    mon = UeBringupMonitor(BringupTimeouts(ssb_s=5, rach_s=5, rrc_s=5, pdu_s=5, tun_s=5))
    mon.reset(log, follow_new_only=False)

    _write(
        log,
        "\n".join(
            [
                "[NR_PHY] Initial sync successful",
                "[MAC] RAR-Msg2 decoded",
                "State = NR_RRC_CONNECTED",
                "Received PDU Session Establishment Accept, UE IPv4: 10.0.0.2",
                "TUN Interface oaitun_ue1 successfully configured, IPv4 10.0.0.2",
                "",
            ]
        ),
    )
    st = mon.poll(process_running=True, log_path=log)
    assert st.stage == BringupStage.READY
    assert st.ue_ip == "10.0.0.2"
    assert st.stalled is False


def test_incremental_stages_and_stall(tmp_path):
    log = tmp_path / "ue.stdout.log"
    mon = UeBringupMonitor(BringupTimeouts(ssb_s=0.05, rach_s=30, rrc_s=30, pdu_s=30, tun_s=30))
    _write(log, "")
    mon.reset(log, follow_new_only=True)

    st = mon.poll(process_running=True, log_path=log)
    assert st.stage == BringupStage.STARTING
    assert st.stalled is False
    assert st.label == "starting"

    time.sleep(0.08)
    st = mon.poll(process_running=True, log_path=log)
    assert st.stage == BringupStage.STARTING
    assert st.stalled is True

    _write(log, "Starting sync detection\n", append=True)
    st = mon.poll(process_running=True, log_path=log)
    assert st.stage == BringupStage.SEARCHING_SSB
    assert st.label == "searching SSB"
    assert st.stalled is False

    _write(log, "UE synchronized!\n", append=True)
    st = mon.poll(process_running=True, log_path=log)
    assert st.stage == BringupStage.WAITING_RACH
    assert st.stalled is False


def test_early_boot_markers(tmp_path):
    log = tmp_path / "ue.stdout.log"
    mon = UeBringupMonitor()
    mon.reset(log, follow_new_only=False)
    _write(
        log,
        "Initializing MAC layer\n"
        "Waiting for reference lock\n"
        "Running Initial Synch\n",
    )
    st = mon.poll(process_running=True, log_path=log)
    assert st.stage == BringupStage.SEARCHING_SSB
    assert "SSB" in st.label or "search" in st.label.lower()


def test_scan_existing_log_when_process_recovers(tmp_path):
    log = tmp_path / "ue.stdout.log"
    _write(
        log,
        "Initial sync successful\n"
        "RAR-Msg2 decoded\n"
        "State = NR_RRC_CONNECTED\n"
        "Received PDU Session Establishment Accept, UE IPv4: 12.1.1.2\n"
        "TUN Interface oaitun_ue1 successfully configured, IPv4 12.1.1.2\n",
    )
    mon = UeBringupMonitor()
    # PROCESS_DOWN → poll with running process rescans whole log
    st = mon.poll(process_running=True, log_path=log)
    assert st.stage == BringupStage.READY
    assert st.ue_ip == "12.1.1.2"


def test_follow_new_only_ignores_pre_restart_markers(tmp_path):
    log = tmp_path / "ue.stdout.log"
    _write(log, "Initial sync successful\nState = NR_RRC_CONNECTED\n")
    mon = UeBringupMonitor()
    mon.reset(log, follow_new_only=True)
    st = mon.poll(process_running=True, log_path=log)
    assert st.stage == BringupStage.STARTING

    _write(log, "Initial sync successful\n", append=True)
    st = mon.poll(process_running=True, log_path=log)
    assert st.stage == BringupStage.WAITING_RACH
