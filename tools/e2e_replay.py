"""Live check of the replay worker: the latents it recomputes from the UE's recorded channels must be the
ones the gNB actually received over the air (gNB decode log), i.e. replay == UE encoder, bit for bit.

    .venv/bin/python -u -m tools.e2e_replay configs/demo_rfsim.yaml [seconds]
"""

import re
import statistics
import sys
import time
from pathlib import Path

import numpy as np

from common.demo_control import DemoController
from common.oai.config import load_config
from common.replay.worker import ReplayWorker, engine_from_config


def main() -> int:
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else "configs/demo_rfsim.yaml")
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
    cfg.csi.default_mode = "csinet"
    args = cfg.oai.gnb.base_args
    args[args.index("--print-csi-debug") + 1] = "1"  # log every received latent
    demo = DemoController(cfg)
    demo.start()
    worker = ReplayWorker(lambda: demo.view().csi_record_root, engine_from_config(cfg))
    worker.start()
    payloads, ok = [], True
    try:
        demo.start_system()
        t0 = time.monotonic()
        while not all(s == "ok" for _, s in demo.view().steps) and time.monotonic() - t0 < 240:
            time.sleep(0.5)
        print(f"system up after {time.monotonic() - t0:.0f} s; scenario {demo.view().scenario}", flush=True)
        demo.set_scenario("cross")
        t1 = time.monotonic()
        seen = 0
        while time.monotonic() - t1 < secs:
            v = worker.view()
            if v.last is not None and v.n_reports != seen:
                seen = v.n_reports
                payloads.append(v.last.payload)
            time.sleep(0.05)
        v = worker.view()
        print(f"replayed {v.n_reports} reports in {secs:.0f} s, {v.ms_per_report:.1f} ms each, error={v.error!r}")
        for key, vals in v.beam_cos.items():
            print(f"  beam cosine {key:6s}: mean {np.mean(vals):.3f} over {len(vals)} sub-band samples")
        print(f"  i2 attached to {sum(1 for i in v.hist_i2 if i >= 0)}/{len(v.hist_i2)} recent reports")
        ok &= v.n_reports >= secs * 3 and not v.error and all(i >= 0 for i in v.hist_i2)
    finally:
        worker.stop()
        demo.shutdown()
    logs = sorted((Path(cfg.logging.root_dir) / "softmodem_logs").glob("*"))[-1]
    text = re.sub(r"\x1b\[[0-9;]*m", "", (logs / "gnb.stdout.log").read_text(errors="replace"))
    received = {tuple(int(x) for x in m.split(",")) for m in re.findall(r"PUCCH AI decode .*?latent=\[([-0-9,]+)\]", text)}
    replayed = [tuple(int(b) - 256 if b > 127 else int(b) for b in p) for p in payloads]
    hits = sum(1 for p in replayed if p in received)
    print(f"gNB received {len(received)} distinct latents; replayed latents found among them: {hits}/{len(replayed)}")
    ok &= len(replayed) > 0 and hits >= len(replayed) - 2  # the last ones may not be logged yet
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
