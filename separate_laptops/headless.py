"""Run a station without its window (testing, or a laptop without a screen).

    .venv/bin/python -m separate_laptops.headless ue  --profile configs/station_ue_rfsim.yaml
    .venv/bin/python -m separate_laptops.headless gnb --profile configs/station_gnb_rfsim.yaml [--start]

Prints the station's state once a second; Ctrl+C stops it (and its softmodem). The gNB side starts the
system with --start, as its Start button would; the UE side follows the gNB station.
"""

from __future__ import annotations

import argparse
import logging
import signal
import time

from common.oai.config import load_config


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("station", choices=("gnb", "ue"))
    ap.add_argument("--profile", required=True)
    ap.add_argument("--start", action="store_true", help="gNB: start the system right away")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.profile)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    if args.station == "ue":
        from separate_laptops.ue_station.controller import UeStation

        st = UeStation(cfg)
        st.start()
        try:
            while not stop:
                v = st.view()
                print(f"STATUS ue running={v.running} stage={v.stage} ip={v.ue_ip} server={v.server_running} "
                      f"link={v.link_up} scenario={v.scenario} samples={len(v.samples)} "
                      f"steps={','.join(s for _, s in v.steps)}", flush=True)
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        st.shutdown()
        return 0
    from separate_laptops.gnb_station.controller import GnbDemo

    demo = GnbDemo(cfg)
    demo.start()
    if args.start:
        demo.start_system()
    try:
        while not stop:
            v, s = demo.view(), demo.station
            print(f"STATUS gnb running={v.radio.gnb_running} boot={v.radio.gnb_boot_stage} ue={s.ue.get('stage', '-')} "
                  f"ip={v.radio.ue_ip} traffic={v.radio.traffic_stage} scenario={v.scenario} samples={len(v.samples)} "
                  f"steps={','.join(x for _, x in s.steps())}", flush=True)
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    demo.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
