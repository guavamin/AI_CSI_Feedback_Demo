"""End-to-end check of common.demo_control in RF-sim or over the air (no window).

    .venv/bin/python -u -m tools.e2e_scenarios configs/demo_rfsim.yaml [hold_s]

Boots gNB + UE + traffic, walks every scenario transition (legacy -> same -> cross -> legacy -> cross ->
same -> legacy), holds each for hold_s seconds and prints per-scenario throughput / MCS / BLER, the
event log, and the gNB/UE CSI error counters (must stay 0). Never run it while the OTA pipeline runs.
"""

import math
import statistics
import sys
import time

from common.demo_control import DemoController
from common.oai.config import load_config
from common.oai.net.mode_control import parse_aifb_status, telnet_command

PLAN = ["same", "cross", "legacy", "cross", "same", "legacy"]


def mean(vals):
    vals = [v for v in vals if isinstance(v, float) and not math.isnan(v)]
    return f"{statistics.fmean(vals):7.2f}" if vals else "    n/a"


def main() -> int:
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else "configs/demo_rfsim.yaml")
    hold = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0
    demo = DemoController(cfg)
    demo.start()
    t0 = time.monotonic()
    ok = True
    try:
        demo.start_system()
        last = None
        while time.monotonic() - t0 < 300:
            v = demo.view()
            steps = " ".join(f"{n}:{s}" for n, s in v.steps)
            if steps != last:
                print(f"[{time.monotonic() - t0:5.1f}s] {steps}", flush=True)
                last = steps
            if all(s == "ok" for _, s in v.steps):
                break
            time.sleep(0.5)
        else:
            print("bring-up did not finish")
            return 2
        time.sleep(4)
        segments = []
        for key in PLAN:
            t_click = time.monotonic()
            assert demo.set_scenario(key)
            while demo.view().switching and time.monotonic() - t_click < 15:
                time.sleep(0.05)
            v = demo.view()
            ev = v.events[-1]
            print(f"[{time.monotonic() - t0:5.1f}s] -> {key:6s} returned after {time.monotonic() - t_click:4.2f} s: "
                  f"[{ev.level}] {ev.text}", flush=True)
            ok &= v.scenario == key and ev.level == "ok"
            t_start = time.monotonic() - demo._t0 + 2.0  # skip 2 s after the switch
            time.sleep(hold)
            segments.append((key, [s for s in demo.view().samples if s.t >= t_start and s.scenario == key]))
        print("\nscenario   n   Mbps    MCS   BLER  UL SNR")
        for key, ss in segments:
            print(f"{key:8s} {len(ss):3d} {mean([s.tput_mbps for s in ss])} {mean([s.mcs for s in ss])} "
                  f"{mean([s.bler for s in ss])} {mean([s.ul_snr_db for s in ss])}")
            ok &= len(ss) >= hold - 4
        for side, port in (("gnb", cfg.network.gnb_telnet_port), ("ue", cfg.network.ue_telnet_port)):
            st = parse_aifb_status(telnet_command("127.0.0.1", port, "aifb status"))
            cnt = {k: st.get(k) for k in ("ue_pucch_dropped", "gnb_csi_crc_fail", "gnb_csi_bitlen_mismatch")}
            print(side, "pucch_replace", st.get("pucch_replace"), "decoder", st.get("decoder"), "counters", cnt)
            ok &= all(c == "0" for c in cnt.values())
        print("\nevent log:")
        for e in demo.view().events:
            print(f"  {e.clock} [{e.level:4s}] {e.text}")
        print("RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        demo.shutdown()


if __name__ == "__main__":
    sys.exit(main())
