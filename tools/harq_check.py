"""Per scenario: does the gNB retransmit more than the UE actually failed? (HARQ feedback check)

    .venv/bin/python -m tools.harq_check [data/runs/gui_logs/gui_<time>.log]     # default: the latest session

Scenario periods come from the GUI log (switch confirmations; 3 s around each switch skipped). For each
period: the gNB's first transmissions and retransmissions (nrMAC_stats in gnb.stdout.log), its BLER and MCS,
and the UE's own decode results ("DL decode: decoded/failed" in ue.stdout.log, OAI gui-live-switch). If the
gNB's retransmission rate is well above the UE's failure rate, the gNB is misreading HARQ ACKs (feedback
channel), not losing data on the downlink. Needs a UE built with the decode counter.
"""

import re
import sys
from datetime import datetime
from pathlib import Path

from common.style import REPO

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def utc_seconds(hms: str) -> float:
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def main() -> int:
    logs = REPO / "data/runs"
    gui = Path(sys.argv[1]) if len(sys.argv) > 1 else max((logs / "gui_logs").glob("gui_*.log"))
    text = gui.read_text(errors="replace")
    # GUI log times are local; softmodem logs are UTC
    events = []
    for line in text.splitlines():
        m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d+) .*?\[ok\] (.*)", line)
        if not m:
            continue
        local = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").astimezone()
        t = local.timestamp() % 86400 + int(m.group(2)) / 1000
        msg = m.group(3)
        if msg.startswith("Traffic running"):
            events.append((t, "legacy?"))
        elif " -> " in msg and " in " in msg:
            events.append((t, msg.split(" -> ")[1].split(" in ")[0].strip()))
        elif msg.startswith("System stopped"):
            events.append((t, None))
    if not events:
        print(f"no scenario events in {gui}")
        return 1
    stamp = re.search(r"softmodem_logs/(\d{8}_\d{6})", text)
    run = logs / "softmodem_logs" / stamp.group(1) if stamp else max((logs / "softmodem_logs").glob("*"))
    gnb = ANSI.sub("", (run / "gnb.stdout.log").read_text(errors="replace"))
    ue = ANSI.sub("", (run / "ue.stdout.log").read_text(errors="replace"))
    g = [(utc_seconds(m.group(1)), int(m.group(2)), int(m.group(3)), float(m.group(4)), int(m.group(5)))
         for m in re.finditer(r"\[(\d\d:\d\d:\d\d\.\d+)\] \[NR_MAC\] Frame\.Slot.*?dlsch_rounds (\d+)/(\d+)/.*?BLER ([0-9.]+) "
                              r"MCS \(\d+\) (\d+)", gnb, re.S)]
    u = [(utc_seconds(m.group(1)), int(m.group(2)), int(m.group(3)))
         for m in re.finditer(r"\[(\d\d:\d\d:\d\d\.\d+)\] \[NR_MAC\] UE \d+ RNTI \w+ stats.*?DL decode: (\d+)/(\d+)", ue, re.S)]
    if not u:
        print("the UE log has no 'DL decode' lines: run with the UE built after OAI commit 'UE MAC: count DL decode results'")
        return 1
    first = events[0][1] == "legacy?"
    print(f"session {run.name} (GUI log {gui.name})")
    print(f"  {'scenario':14s} {'seconds':>7s} {'gNB 1st tx':>10s} {'gNB retx rate':>13s} {'UE decoded':>10s} "
          f"{'UE fail rate':>12s} {'BLER':>6s} {'MCS':>5s}")
    for (t0, key), (t1, _) in zip(events, events[1:] + [(events[-1][0] + 1e9, None)]):
        if key is None:
            continue
        name = "Legacy" if first and key == "legacy?" else key
        a, b = t0 + 3, t1 - 3
        gs = [x for x in g if a <= x[0] <= b]
        us = [x for x in u if a <= x[0] <= b]
        if len(gs) < 3 or len(us) < 3 or gs[-1][1] < gs[0][1] or us[-1][1] < us[0][1]:
            continue  # too short or a UE restart inside the period
        tx, retx = gs[-1][1] - gs[0][1], gs[-1][2] - gs[0][2]
        ok, fail = us[-1][1] - us[0][1], us[-1][2] - us[0][2]
        print(f"  {name:14s} {b - a:7.0f} {tx:10d} {retx / max(tx, 1):13.3f} {ok:10d} {fail / max(ok + fail, 1):12.3f} "
              f"{sum(x[3] for x in gs) / len(gs):6.3f} {sum(x[4] for x in gs) / len(gs):5.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
