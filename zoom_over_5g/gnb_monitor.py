"""Zoom over 5G, gNB laptop: the downlink the gNB sends to the UE, once a second.

    .venv/bin/python -m zoom_over_5g.gnb_monitor                                  # over the air
    .venv/bin/python -m zoom_over_5g.gnb_monitor --profile configs/station_gnb_rfsim.yaml

The gNB station's plots only run while its iperf traffic runs, and during the Zoom test there is none; this
shows what the Zoom call gets instead. Read-only: it reads the gNB's nrMAC_stats.log and nothing else (no
telnet, so it never gets in the way of the station's Legacy / AI switch).

Columns: DL Mbit/s (MAC bytes the gNB sent to the UE), DL MCS, DL BLER, CQI, RI, UL SNR, RSRP. Type a note and
press Enter (e.g. "AI" right after switching to AI): it goes into the CSV's note column. Ctrl+C stops.
CSV: data/runs/zoom_over_5g/gnb_<time>.csv.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from common.oai.config import load_config
from common.oai.net.metrics_ingest import MacStatsIngest

REPO = Path(__file__).resolve().parents[1]


def _fmt(value: float, spec: str) -> str:
    return "-" if math.isnan(value) else format(value, spec)


def _csv(value: float, spec: str) -> str:
    return "" if math.isnan(value) else format(value, spec)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--profile", default="configs/station_gnb_usrp.yaml")
    ap.add_argument("--period", type=float, default=1.0, help="seconds between rows")
    args = ap.parse_args()
    cfg = load_config(args.profile)
    stats = Path(cfg.oai.build_dir).expanduser() / cfg.oai.stats_log
    ingest = MacStatsIngest(stats)

    out_dir = REPO / "data" / "runs" / "zoom_over_5g"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"gnb_{datetime.now():%Y%m%d_%H%M%S}.csv"

    notes: list[str] = []

    def read_notes() -> None:
        for line in sys.stdin:
            if line.strip():
                notes.append(line.strip())

    threading.Thread(target=read_notes, name="notes", daemon=True).start()
    print(f"gNB downlink from {stats}\nCSV: {out.relative_to(REPO)}   (type a note + Enter; Ctrl+C stops)", flush=True)
    header = f"{'time':<9} {'DL Mbit/s':>9} {'MCS':>4} {'BLER':>6} {'CQI':>4} {'RI':>3} {'UL SNR':>7} {'RSRP':>6}"
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time_utc", "dl_mbps", "dl_mcs", "dl_bler", "cqi", "ri", "ul_snr_db", "rsrp_dbm", "note"])
        ingest.poll()  # first poll: byte counter reference
        rows = 0
        try:
            while True:
                time.sleep(args.period)
                s = ingest.poll()
                note = " / ".join(notes)
                notes.clear()
                if rows % 20 == 0:
                    print(header, flush=True)
                rows += 1
                if s is None:
                    print(f"{datetime.now():%H:%M:%S}  gNB stats not available yet ({stats.name})", flush=True)
                    continue
                print(f"{datetime.now():%H:%M:%S} {_fmt(s.throughput_mbps, '9.2f')} {_fmt(s.dl_mcs, '4.0f')} "
                      f"{_fmt(s.bler, '6.3f')} {s.cqi:>4} {s.ri:>3} {_fmt(s.ul_snr_db, '7.1f')} "
                      f"{_fmt(s.rsrp_dbm, '6.0f')}" + (f"   <- {note}" if note else ""), flush=True)
                w.writerow([datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                            _csv(s.throughput_mbps, ".3f"), _csv(s.dl_mcs, ".0f"), _csv(s.bler, ".4f"),
                            s.cqi, s.ri, _csv(s.ul_snr_db, ".1f"), _csv(s.rsrp_dbm, ".0f"), note])
                fh.flush()
        except KeyboardInterrupt:
            print(f"\nstopped; CSV: {out.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
