"""gNB softmodem boot stage parsing (cmd → config → RU → RF/SSB TX)."""

from __future__ import annotations

from pathlib import Path

from common.oai.net.modem_boot import GnbBootMonitor, GnbBootStage


def _write(path: Path, text: str, *, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as fh:
        fh.write(text)


def test_gnb_advances_to_ssb_tx(tmp_path):
    log = tmp_path / "gnb.stdout.log"
    mon = GnbBootMonitor()
    mon.reset(log, follow_new_only=False)
    _write(
        log,
        "\n".join(
            [
                "Initializing NR L1",
                "Starting RU 0",
                "Waiting for reference lock",
                "RU 0 RF started",
                "got sync (ru_thread)",
                "",
            ]
        ),
    )
    st = mon.poll(process_running=True, log_path=log)
    assert st.stage == GnbBootStage.RF_SSB
    assert "SSB" in st.detail or "RF" in st.detail
    assert st.label == "SSB TX"


def test_gnb_incremental_stages(tmp_path):
    log = tmp_path / "gnb.stdout.log"
    mon = GnbBootMonitor()
    _write(log, "")
    mon.reset(log, follow_new_only=True)

    st = mon.poll(process_running=True, log_path=log)
    assert st.stage == GnbBootStage.STARTING

    _write(log, "Initialized RAN Context\n", append=True)
    st = mon.poll(process_running=True, log_path=log)
    assert st.stage == GnbBootStage.CONFIG

    _write(log, "Starting RU\n", append=True)
    st = mon.poll(process_running=True, log_path=log)
    assert st.stage == GnbBootStage.RU_INIT

    _write(log, "got sync (ru_thread)\n", append=True)
    st = mon.poll(process_running=True, log_path=log)
    assert st.stage == GnbBootStage.RF_SSB
