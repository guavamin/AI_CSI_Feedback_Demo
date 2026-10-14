"""End-to-end test of the separate-laptops split on one machine: the UE station runs as its own program, the gNB
station is driven from here, and they talk only through the station link (with extra delay, like Tailscale's
relay). Default: RF simulator, no radios needed.

    .venv/bin/python -m tools.e2e_stations                                   # RF sim, 150 ms one-way link delay
    .venv/bin/python -m tools.e2e_stations --radio usrp --delay 0.1           # both B210s on this laptop

Checks, in order: one Start brings up gNB -> UE (asked over the link) -> traffic; the per-second samples on both
sides; Legacy -> Same -> Cross -> Legacy with every switch confirmed on both sides (UE feedback mode via the link,
gNB decoder); what each station's channel view sees (UE: its measured channel; gNB: the latents it received,
decoded with its own decoders); Stop takes down both sides. Exit code 0 = all checks passed.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from common.oai.config import load_config
from common.oai.net.mode_control import parse_aifb_status
from common.station_data import GnbChannelWorker, UeChannelWorker, wait_for
from common.style import REPO

log = logging.getLogger("e2e")
RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, text: str) -> bool:
    RESULTS.append((bool(ok), text))
    print(f"{'PASS' if ok else 'FAIL'}  {text}", flush=True)
    return bool(ok)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--radio", choices=("rfsim", "usrp"), default="rfsim")
    ap.add_argument("--delay", type=float, default=0.15, help="one-way link delay per message (s)")
    ap.add_argument("--dwell", type=float, default=20.0, help="seconds per scenario")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(REPO / "data/runs/e2e_stations.log", mode="w")])
    env = dict(os.environ, UE_STATION="127.0.0.1", GNB_STATION="127.0.0.1")
    os.environ.update(UE_STATION="127.0.0.1", GNB_STATION="127.0.0.1")
    ue_log = open(REPO / "data/runs/e2e_ue_station.log", "w")
    ue_proc = subprocess.Popen([sys.executable, "-m", "separate_laptops.headless", "ue", "--profile",
                                f"configs/station_ue_{args.radio}.yaml"], cwd=REPO, env=env,
                               stdout=ue_log, stderr=subprocess.STDOUT)
    from separate_laptops.gnb_station.controller import GnbDemo

    cfg = load_config(REPO / f"configs/station_gnb_{args.radio}.yaml")
    cfg.station["test_delay_s"] = args.delay
    demo = GnbDemo(cfg)
    st = demo.station
    st.core_ok = True
    try:
        demo.start()
        check(wait_for(lambda: bool(st.ue), 20), f"link up to the UE station (round trip {st.link.rtt_s * 1000:.0f} ms "
                                                  f"with {args.delay * 1000:.0f} ms each way)")
        t0 = time.monotonic()
        demo.start_system()
        ok = wait_for(lambda: demo.view().radio.traffic_stage == "running", 180, 0.5)
        check(ok, f"one Start: gNB on air, UE started over the link, attached, traffic running "
                  f"({time.monotonic() - t0:.0f} s)")
        if not ok:
            return 1
        build = Path(cfg.oai.build_dir)
        args_g = cfg.oai.gnb.base_args
        gw = GnbChannelWorker(lambda: str(st.csirs_record_root), build.parents[2], build,
                              {0: Path(args_g[args_g.index("--ai-fb-model-path") + 1]),
                               1: Path(args_g[args_g.index("--ai-fb-live-alt-model-path") + 1])})
        gw.start()
        ue_roots = sorted(Path(cfg.oai.csi_record_dir).glob("ue_*"), key=lambda p: p.stat().st_mtime)
        uw = UeChannelWorker(lambda: str(ue_roots[-1]) if ue_roots else "")
        uw.start()
        ue_ip = st.ue.get("ue_ip")
        check(bool(ue_ip) and st.ue.get("server_running"), f"UE station reports IP {ue_ip} and its receiver")
        check(all(s == "ok" for _, s in st.steps()), f"gNB station steps {st.steps()}")
        time.sleep(args.dwell)
        n = len(demo.view().samples)
        check(n >= args.dwell - 8, f"gNB samples while Legacy is on air: {n}")
        for key in ("same", "cross", "legacy"):
            t0 = time.monotonic()
            check(demo.set_scenario(key), f"switch to {key} accepted")
            done = wait_for(lambda: demo.state_key() == (key, key, False), 30, 0.2)
            check(done, f"switch to {key} done in {time.monotonic() - t0:.1f} s (events: "
                        f"{[e.text for e in demo.view().events[-3:]]})")
            gnb = parse_aifb_status(st._ue_send("aifb status", 1.0))  # the UE, through the link
            want_ai = "1" if key != "legacy" else "0"
            check(gnb.get("pucch_replace") == want_ai, f"UE feedback mode after {key}: pucch_replace="
                                                       f"{gnb.get('pucch_replace')} (via the link)")
            if key != "legacy":
                check(str(demo.decoder) == str({"same": 0, "cross": 1}[key]), f"gNB decoder {demo.decoder}")
            check(wait_for(lambda: st.ue.get("scenario") == key, 5), f"UE station shows {key} (notice over the link)")
            time.sleep(args.dwell)
            gv, uv = gw.view(), uw.view()
            check(gv.kind == key and (gv.cfr is not None) == (key != "legacy"),
                  f"gNB channel view during {key}: latest report decoded as {gv.kind!r}, "
                  f"{'reconstructed channel' if gv.cfr is not None else 'codebook beam only'}, {gv.n_reports} reports")
            check(uv.n_reports > 0 and uv.beam_deg is not None, f"UE channel view during {key}: {uv.n_reports} "
                                                                 f"measured reports")
        v = demo.view()
        per = {k: sum(1 for s in v.samples if s.scenario == k) for k in ("legacy", "same", "cross")}
        check(all(per[k] >= args.dwell - 10 for k in per), f"gNB samples per scenario {per}")
        check((Path(st.csirs_record_root) / "gnb_ai_fb_latents.csv").exists(), "gNB recorded the latents it received")
        gw.stop()
        uw.stop()
        lines = [ln for ln in open(REPO / "data/runs/e2e_ue_station.log") if ln.startswith("STATUS ue")]
        last = lines[-1].strip() if lines else ""
        samples = int(last.split("samples=")[1].split()[0]) if "samples=" in last else 0
        check(samples >= 2 * args.dwell, f"UE station samples (its own throughput / decode failures / SINR): {samples}")
        # Stop takes down both sides
        demo.stop_system()
        check(wait_for(lambda: not st.ue.get("running") and not st.launcher.is_running("gnb"), 30),
              "Stop: gNB stopped and the UE station stopped its UE")
    finally:
        threading.Thread(target=demo.shutdown, daemon=True).start()
        time.sleep(3)
        ue_proc.terminate()
        try:
            ue_proc.wait(20)
        except subprocess.TimeoutExpired:
            ue_proc.kill()
    left = [pid for name in ("nr-softmodem", "nr-uesoftmodem")
            for pid in subprocess.run(["pgrep", "-x", name], capture_output=True, text=True).stdout.split()]
    check(not left, f"no softmodem left running {left}")
    bad = [t for ok, t in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
