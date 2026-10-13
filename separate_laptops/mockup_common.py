"""Mockup-only plumbing shared by the two station mockups: replay data and the window loop.

Keys: 1 / 2 / 3 change the feedback scenario and show the scenario-change pop-up (on the UE station this
simulates a request arriving from the gNB); Esc hides the pop-up; F11 toggles fullscreen.
"""

from __future__ import annotations

import argparse
import csv

import dearpygui.dearpygui as dpg
import numpy as np

from common.style import MOCKUP_DATA, SCENARIOS

SEG_S = 60
STATE: dict = {}


def load_data() -> dict:
    """Per-second metrics of OTA runs 01 / 02 / 06 back to back (as if switched live), UE reports, channels."""
    runs = {s["key"]: [r for r in csv.DictReader(open(MOCKUP_DATA / "runs" / s["run"] / "metrics.csv"))
                       if float(r["dl_mbps"]) > 0.5] for s in SCENARIOS}
    ue = {s["key"]: list(csv.DictReader(open(MOCKUP_DATA / "runs" / s["run"] / "ue_reports.csv"))) for s in SCENARIOS}
    d = dict(runs=runs, ue=ue, t=[], seg=[], tput=[], mcs=[], bler=[], cqi=[])
    for i, s in enumerate(SCENARIOS):
        for j, r in enumerate(runs[s["key"]][20: 20 + SEG_S]):
            d["t"].append(i * SEG_S + j)
            d["seg"].append(i)
            for key, col in (("tput", "dl_mbps"), ("mcs", "mcs"), ("bler", "bler"), ("cqi", "cqi")):
                d[key].append(float(r[col]))
    d["cdf"] = {col: {s["key"]: [float(r[col]) for r in runs[s["key"]]] for s in SCENARIOS}
                for col in ("dl_mbps", "mcs", "bler")}
    d["ch"] = dict(np.load(MOCKUP_DATA / "mockup_channels.npz"))
    d["cos"] = dict(np.load(MOCKUP_DATA / "mockup_cosine.npz"))
    d["latents"] = np.load(MOCKUP_DATA / "mockup_latents.npz")["latent_codes"]
    return d


def run(build, title: str) -> None:
    """build(args, W, H, data) draws the whole window into the "main" window (and "popup" when shown)."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--active", default="cross", choices=[s["key"] for s in SCENARIOS])
    ap.add_argument("--popup", action="store_true", help="show the scenario-change pop-up")
    ap.add_argument("--fullscreen", action="store_true")
    ap.add_argument("--screenshot", default="")
    args = ap.parse_args()
    data = load_data()
    dpg.create_context()
    dpg.create_viewport(title=title, width=1920, height=1080)
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.toggle_viewport_fullscreen() if args.fullscreen else dpg.maximize_viewport()
    with dpg.handler_registry():
        dpg.add_key_press_handler(dpg.mvKey_F11, callback=lambda: STATE.update(fullscreen=True))
        dpg.add_key_press_handler(dpg.mvKey_Escape, callback=lambda: STATE.update(popup=False))
        for k, key in ((dpg.mvKey_1, "legacy"), (dpg.mvKey_2, "same"), (dpg.mvKey_3, "cross")):
            dpg.add_key_press_handler(k, callback=lambda s_, a_, u: STATE.update(active=u, popup=True), user_data=key)

    def rebuild():
        for tag in ("main", "popup"):
            if dpg.does_item_exist(tag):
                dpg.delete_item(tag)
        build(args, dpg.get_viewport_client_width(), dpg.get_viewport_client_height(), data)

    frame, built, size = 0, False, None
    while dpg.is_dearpygui_running():
        frame += 1
        cur = (dpg.get_viewport_client_width(), dpg.get_viewport_client_height())
        if not built and frame >= 6:
            rebuild()
            built, size = True, cur
        elif built and (STATE or size != cur):
            if STATE.pop("fullscreen", False):
                dpg.toggle_viewport_fullscreen()
            if "active" in STATE:
                args.prev = args.active
            args.active = STATE.pop("active", args.active)
            args.popup = STATE.pop("popup", args.popup)
            rebuild()
            size = (dpg.get_viewport_client_width(), dpg.get_viewport_client_height())
        if args.screenshot and frame == 70:
            dpg.output_frame_buffer(file=args.screenshot)
        if args.screenshot and frame == 100:
            break
        dpg.render_dearpygui_frame()
    dpg.destroy_context()
