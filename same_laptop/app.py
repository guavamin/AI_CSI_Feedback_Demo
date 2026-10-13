"""Same-laptop booth GUI, live: gNB, UE, OAI CN5G and this window on one laptop.

    .venv/bin/python -m same_laptop.app                                  # over the air (configs/demo_usrp.yaml)
    .venv/bin/python -m same_laptop.app --profile configs/demo_rfsim.yaml
Keys: 1 / 2 / 3 or a click on a scenario card switch live; F11 fullscreen. Closing the window stops the
gNB, the UE and the traffic. Never run it while the OTA pipeline is running.

Layout: the approved design of same_laptop/mockup.py. Data: common.demo_control (radio, scenarios,
per-second samples) and common.replay (the UE's channel replayed through all three scenarios with OAI's own
model code). The window is built once (and again when the scenario or the window size changes); live data
is pushed into the existing items about twice a second, and rendering is capped at 30 fps so the GUI leaves
the CPU to the real-time gNB and UE.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import dearpygui.dearpygui as dpg
import numpy as np

from common.demo_control import DemoController, DemoView
from common.oai.config import load_config
from common.replay.worker import ReplayView, ReplayWorker, engine_from_config
from common.style import (BG, BLUE, DIM, FONTS, GREEN, INK, LINE, LOGO_FILES, LOGOS, MUTED, ORANGE, PALE, REPO,
                          SCENARIO, SCENARIOS, SURFACE, TEXT, TITLE, V1_LIGHT, mix, rgb, roll)
from same_laptop.mockup import DIA_DESIGN_H, diagram_scale, draw_diagram

log = logging.getLogger("same_laptop")
WINDOW_S = 180  # time plots show the last 3 minutes
POOL = 16  # line/shade series per time plot (one per scenario segment in the window)
LINKS = ((0, 0), (0, 1), (1, 0), (1, 1))
SUBC = "Subcarrier (occupied, 612)"
HEAT_COLS = 153  # heat maps average 4 subcarriers per cell: ImPlot draws every cell each frame
KEYS = [s["key"] for s in SCENARIOS]
METRICS = (  # name, sample attribute, time-plot y label, CDF x label
    ("THROUGHPUT", "tput_mbps", "Mbps", "Throughput (Mbps)"),
    ("MCS", "mcs", "MCS", "MCS index"),
    ("BLER", "bler", "BLER", "BLER"),
)
RED = "#E5534B"


def tcol(sc):
    return sc.get("text", sc["color"])


def core_network_up() -> bool:
    try:
        out = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=3)
        names = set(out.stdout.split())
        return {"oai-amf", "oai-smf", "oai-upf"} <= names
    except (OSError, subprocess.SubprocessError):
        return False


class App:
    def __init__(self, args):
        self.args = args
        self.cfg = load_config(args.profile)
        self.ota = not self.cfg.oai.rfsim
        self.demo = DemoController(self.cfg)
        self.replay = ReplayWorker(lambda: self.demo.view().csi_record_root, engine_from_config(self.cfg),
                                   scenario_at=self.demo.scenario_at)
        self.res: dict = {}
        self.t: dict = {}  # tags of live items
        self.state: dict = {}  # pending UI actions, applied between frames
        self.cards: dict = {}
        self.core_ok = False
        self._built_key = None
        self._steps_key = None

    # ------------------------------------------------------------------ resources (once)
    def resources(self):
        r = self.res

        def font(name, size):
            return dpg.add_font(str(FONTS / name), size)

        with dpg.font_registry():
            r.update(body=font("Ubuntu-R.ttf", 18), small=font("Ubuntu-R.ttf", 15), med=font("Ubuntu-M.ttf", 18),
                     h2=font("Ubuntu-M.ttf", 22), title=font("Ubuntu-B.ttf", 23), kicker=font("Ubuntu-M.ttf", 14),
                     draw=font("Ubuntu-M.ttf", 20))
        dpg.bind_font(r["body"])
        r["logos"] = []
        with dpg.texture_registry():
            for name, h in LOGO_FILES:
                w_, h_, _, d = dpg.load_image(str(LOGOS / name))
                r["logos"].append((dpg.add_static_texture(w_, h_, d), int(w_ * h / h_), h))
        with dpg.theme() as t_global:
            with dpg.theme_component(dpg.mvAll):
                for col, val in ((dpg.mvThemeCol_WindowBg, rgb(BG)), (dpg.mvThemeCol_ChildBg, rgb(SURFACE)),
                                 (dpg.mvThemeCol_Border, rgb(LINE)), (dpg.mvThemeCol_Text, rgb(TEXT)),
                                 (dpg.mvThemeCol_TextDisabled, rgb(DIM)), (dpg.mvThemeCol_FrameBg, rgb("#2A2927")),
                                 (dpg.mvThemeCol_Button, rgb("#2A2927")), (dpg.mvThemeCol_ButtonHovered, rgb("#34332F")),
                                 (dpg.mvThemeCol_ButtonActive, rgb("#3A3936")), (dpg.mvThemeCol_Tab, rgb(BG)),
                                 (dpg.mvThemeCol_TabHovered, rgb("#2A2927")), (dpg.mvThemeCol_TabActive, rgb(SURFACE)),
                                 (dpg.mvThemeCol_Separator, rgb(LINE)), (dpg.mvThemeCol_PopupBg, rgb(SURFACE))):
                    dpg.add_theme_color(col, val)
                for var, val in ((dpg.mvStyleVar_WindowPadding, (18, 14)), (dpg.mvStyleVar_ChildRounding, 14),
                                 (dpg.mvStyleVar_FrameRounding, 10), (dpg.mvStyleVar_ItemSpacing, (12, 10)),
                                 (dpg.mvStyleVar_TabRounding, 8), (dpg.mvStyleVar_FramePadding, (14, 9)),
                                 (dpg.mvStyleVar_ChildBorderSize, 1), (dpg.mvStyleVar_WindowBorderSize, 0)):
                    dpg.add_theme_style(var, *val) if isinstance(val, tuple) else dpg.add_theme_style(var, val)
                for col, val in ((dpg.mvPlotCol_PlotBg, rgb(SURFACE)), (dpg.mvPlotCol_FrameBg, rgb(SURFACE)),
                                 (dpg.mvPlotCol_PlotBorder, rgb(SURFACE)), (dpg.mvPlotCol_LegendBg, rgb(SURFACE, 225)),
                                 (dpg.mvPlotCol_LegendBorder, rgb(LINE)), (dpg.mvPlotCol_AxisGrid, rgb(LINE, 160)),
                                 (dpg.mvPlotCol_AxisText, rgb(MUTED))):
                    dpg.add_theme_color(col, val, category=dpg.mvThemeCat_Plots)
                dpg.add_theme_style(dpg.mvPlotStyleVar_PlotPadding, 10, 8, category=dpg.mvThemeCat_Plots)
                dpg.add_theme_style(dpg.mvPlotStyleVar_LabelPadding, 4, 2, category=dpg.mvThemeCat_Plots)
                dpg.add_theme_style(dpg.mvPlotStyleVar_LineWeight, 2.0, category=dpg.mvThemeCat_Plots)
        dpg.bind_theme(t_global)
        # series themes per scenario, created once and re-bound when a pooled series changes scenario
        r["line"] = {s["key"]: self.series_theme(dpg.mvLineSeries, tcol(s), 255, 3.0) for s in SCENARIOS}
        r["line_thin"] = {s["key"]: self.series_theme(dpg.mvLineSeries, tcol(s), 255, 1.8) for s in SCENARIOS}
        r["shade"] = {s["key"]: self.series_theme(dpg.mvShadeSeries, s["color"], 40) for s in SCENARIOS}
        r["raw"] = self.series_theme(dpg.mvLineSeries, MUTED, 100, 1.2)

    # ------------------------------------------------------------------ small helpers
    @staticmethod
    def child_theme(bg, border, rounding=14, pad=None, spacing=None):
        with dpg.theme() as th:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, bg)
                dpg.add_theme_color(dpg.mvThemeCol_Border, border)
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, rounding)
                if pad:
                    dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, *pad)
                if spacing:
                    dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, *spacing)
        return th

    @staticmethod
    def series_theme(kind, color, alpha=255, weight=2.0):
        with dpg.theme() as th:
            with dpg.theme_component(kind):
                if kind == dpg.mvShadeSeries:
                    dpg.add_theme_color(dpg.mvPlotCol_Fill, rgb(color, alpha), category=dpg.mvThemeCat_Plots)
                    dpg.add_theme_color(dpg.mvPlotCol_Line, rgb(color, 0), category=dpg.mvThemeCat_Plots)
                else:
                    dpg.add_theme_color(dpg.mvPlotCol_Line, rgb(color, alpha), category=dpg.mvThemeCat_Plots)
                    dpg.add_theme_style(dpg.mvPlotStyleVar_LineWeight, weight, category=dpg.mvThemeCat_Plots)
        return th

    def text(self, s, font=None, color=None, wrap=0, tag=0):
        kw = dict(wrap=wrap)
        if tag:
            kw["tag"] = tag
        if color:
            kw["color"] = rgb(color) if isinstance(color, str) else color
        item = dpg.add_text(s, **kw)
        if font:
            dpg.bind_item_font(item, self.res[font])
        return item

    def axis_font(self, plot):
        for ax in dpg.get_item_children(plot, 1) or []:
            dpg.bind_item_font(ax, self.res["small"])

    # ------------------------------------------------------------------ layout (built once per scenario / size)
    def build(self, W: int, H: int, view: DemoView) -> None:
        self.t, self.cards = {}, {}
        act = SCENARIO[view.target_scenario]
        act_i = KEYS.index(act["key"])
        PAD, GAP = 18, 12
        inner_w = W - 2 * PAD
        HEADER_H = 78
        body_h = H - HEADER_H - 46 - 2 * PAD - 26
        LEFT_W = max(400, int(inner_w * 0.225))
        RIGHT_W = inner_w - LEFT_W - GAP - 4
        DIA_H = int(body_h * 0.43)
        DIA_H = min(DIA_H, int(44 + DIA_DESIGN_H * 0.88 * diagram_scale(RIGHT_W - 34, DIA_H - 44) + 18))
        PLOT_H = body_h - DIA_H - GAP - 4
        CARD_H = 104
        with dpg.window(tag="main", no_scrollbar=True):
            self.build_header(W - 2 * PAD, HEADER_H)
            with dpg.tab_bar(tag="tabbar", callback=lambda s_, a_: self.state.update(tab=a_)):
                with dpg.tab(label="  Live demo  ", tag="tab_demo"):
                    with dpg.group(horizontal=True):
                        with dpg.child_window(width=LEFT_W, height=body_h, border=False, no_scrollbar=True) as left:
                            dpg.bind_item_theme(left, self.child_theme(rgb(BG), rgb(BG)))
                            self.text("CSI FEEDBACK SCENARIOS", "kicker", DIM)
                            for i, sc in enumerate(SCENARIOS):
                                self.build_card(sc, i == act_i, view, LEFT_W, CARD_H)
                                dpg.add_spacer(height=2)
                            self.text("Press 1 · 2 · 3 or click a scenario to switch live.", "small", DIM, wrap=LEFT_W - 10)
                            dpg.add_spacer(height=4)
                            self.build_system(LEFT_W)
                        with dpg.child_window(width=RIGHT_W, height=body_h, border=False, no_scrollbar=True) as right:
                            dpg.bind_item_theme(right, self.child_theme(rgb(BG), rgb(BG)))
                            with dpg.child_window(height=DIA_H, border=True, no_scrollbar=True) as dia:
                                dpg.bind_item_theme(dia, self.child_theme(rgb(SURFACE), rgb(LINE), pad=(16, 12), spacing=(8, 4)))
                                self.text("SYSTEM DIAGRAM  ·  " + act["title"].upper(), "kicker", tcol(act))
                                draw_diagram(act, RIGHT_W - 34, DIA_H - 58, None, self.res["draw"])
                            self.build_plots(RIGHT_W, PLOT_H, GAP)
                with dpg.tab(label="  Channel visualization  ", tag="tab_channel"):
                    self.build_channel_tab(act, inner_w, body_h, GAP)
        if self.state.get("tab_now") == "channel":
            dpg.set_value("tabbar", "tab_channel")
        dpg.set_primary_window("main", True)

    def build_header(self, HEADER_W, HEADER_H):
        logos = self.res["logos"]
        MARGIN, SEP_W = 24, 34
        with dpg.child_window(height=HEADER_H, border=False, no_scrollbar=True) as header:
            dpg.bind_item_theme(header, self.child_theme(rgb(PALE), rgb(PALE), 0, pad=(0, 0), spacing=(0, 0)))
            mid = HEADER_H / 2
            logos_w = sum(w_ for _, w_, _ in logos) + SEP_W * (len(logos) - 1)
            x0 = HEADER_W - MARGIN - logos_w
            with dpg.drawlist(width=HEADER_W, height=HEADER_H):
                x = x0
                for _, w_, _ in logos[:-1]:
                    x += w_
                    dpg.draw_line((x + SEP_W / 2, mid - 21), (x + SEP_W / 2, mid + 21), color=rgb("#D8D4C8"), thickness=1.5)
                    x += SEP_W
            title = self.text(TITLE, "title", INK)
            dpg.set_item_pos(title, (MARGIN, int(mid - 23 * 0.86)))
            x = x0
            for tex, w_, h_ in logos:
                img = dpg.add_image(tex, width=w_, height=h_)
                dpg.set_item_pos(img, (x, int(mid - h_ / 2)))
                x += w_ + SEP_W

    def build_card(self, sc, on, view: DemoView, LEFT_W, CARD_H):
        switching = view.switching and view.target_scenario == sc["key"]
        with dpg.child_window(height=CARD_H, border=True, no_scrollbar=True) as card:
            self.cards[sc["key"]] = card
            dpg.bind_item_theme(card, self.child_theme(mix(sc["color"], SURFACE, 0.22) if on else rgb(SURFACE),
                                                       rgb(sc["color"]) if on else rgb(LINE), pad=(16, 12), spacing=(10, 6)))
            with dpg.group(horizontal=True):
                with dpg.drawlist(width=34, height=34):
                    dpg.draw_circle((16, 16), 15, color=rgb(sc["color"]), fill=rgb(sc["color"]) if on else rgb(SURFACE), thickness=2)
                    dpg.draw_text((11 if sc["n"] != 1 else 12, 6), str(sc["n"]), color=rgb(PALE if on else tcol(sc)), size=19)
                self.text(sc["title"], "h2", TEXT if on else MUTED)
            sub = "Switching live..." if switching else sc["sub"]
            self.text(sub, "small", (ORANGE if switching else MUTED) if on else DIM, wrap=LEFT_W - 50)

    def build_system(self, LEFT_W):
        with dpg.child_window(height=-1, border=True, no_scrollbar=True) as sysc:
            dpg.bind_item_theme(sysc, self.child_theme(rgb(SURFACE), rgb(LINE), pad=(16, 12), spacing=(10, 8)))
            self.text("SYSTEM", "kicker", DIM)
            bw = (LEFT_W - 60) // 2
            with dpg.group(horizontal=True):
                b1 = dpg.add_button(label="Start system", width=bw, height=44, tag="btn_start",
                                    callback=lambda: self.state.update(start=True))
                b2 = dpg.add_button(label="Stop system", width=bw, height=44, tag="btn_stop",
                                    callback=lambda: self.state.update(stop=True))
            for b in (b1, b2):
                dpg.bind_item_font(b, self.res["med"])
            with dpg.theme() as t_run:
                with dpg.theme_component(dpg.mvButton, enabled_state=False):
                    dpg.add_theme_color(dpg.mvThemeCol_Button, rgb("#26332B"))
                    dpg.add_theme_color(dpg.mvThemeCol_Text, rgb(GREEN))
            with dpg.theme() as t_off:
                with dpg.theme_component(dpg.mvButton, enabled_state=False):
                    dpg.add_theme_color(dpg.mvThemeCol_Button, rgb("#242322"))
                    dpg.add_theme_color(dpg.mvThemeCol_Text, rgb("#5E5C57"))
            self.res["t_running"], self.res["t_off"] = t_run, t_off
            sw = LEFT_W - 50
            dpg.add_drawlist(width=sw, height=60, tag="stepper")
            self.t["stepper_w"] = sw
            self.text("", "small", MUTED, wrap=LEFT_W - 40, tag="status_line")
            self.text("", "small", DIM, wrap=LEFT_W - 40, tag="event_line")
            dpg.add_drawlist(width=LEFT_W - 40, height=24, tag="dots")

    def build_plots(self, RIGHT_W, PLOT_H, GAP):
        col_w = (RIGHT_W - GAP) // 2
        ph = (PLOT_H - 2 * 10 + 10) // 3
        plot_h = ph - 2 * 8 - 24
        for k, (name, attr, lab, lab_cdf) in enumerate(METRICS):
            with dpg.group(horizontal=True, horizontal_spacing=GAP):
                with dpg.child_window(width=col_w, height=ph, border=True, no_scrollbar=True) as pc:
                    dpg.bind_item_theme(pc, self.child_theme(rgb(SURFACE), rgb(LINE), pad=(14, 8), spacing=(6, 2)))
                    self.text("DOWNLINK " + name, "kicker", DIM)
                    with dpg.plot(width=-1, height=plot_h, no_menus=True, no_title=True, no_mouse_pos=True) as pl:
                        self.t[f"{attr}_x"] = dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)")
                        with dpg.plot_axis(dpg.mvYAxis, label=lab) as ya:
                            self.t[f"{attr}_y"] = ya
                            self.t[f"{attr}_shade"] = [dpg.add_shade_series([], [], y2=[]) for _ in range(POOL)]
                            self.t[f"{attr}_raw"] = dpg.add_line_series([], [])
                            dpg.bind_item_theme(self.t[f"{attr}_raw"], self.res["raw"])
                            self.t[f"{attr}_line"] = [dpg.add_line_series([], []) for _ in range(POOL)]
                    self.axis_font(pl)
                with dpg.child_window(width=col_w, height=ph, border=True, no_scrollbar=True) as pc:
                    dpg.bind_item_theme(pc, self.child_theme(rgb(SURFACE), rgb(LINE), pad=(14, 8), spacing=(6, 2)))
                    with dpg.group(horizontal=True):
                        self.text(name + " CDF", "kicker", DIM)
                        if k == 0:
                            for sc in SCENARIOS:
                                dpg.add_spacer(width=10)
                                with dpg.drawlist(width=16, height=14):
                                    dpg.draw_line((0, 8), (16, 8), color=rgb(tcol(sc)), thickness=3)
                                self.text(sc["short"], "kicker", tcol(sc))
                    with dpg.plot(width=-1, height=plot_h, no_menus=True, no_title=True, no_mouse_pos=True) as pl:
                        self.t[f"{attr}_cx"] = dpg.add_plot_axis(dpg.mvXAxis, label=lab_cdf)
                        with dpg.plot_axis(dpg.mvYAxis, label="CDF") as ya:
                            dpg.set_axis_limits(ya, 0, 1.02)
                            for sc in SCENARIOS:
                                s = dpg.add_line_series([], [])
                                dpg.bind_item_theme(s, self.res["line"][sc["key"]])
                                self.t[f"{attr}_cdf_{sc['key']}"] = s
                    self.axis_font(pl)

    def build_channel_tab(self, act, inner_w, body_h, GAP):
        key = act["key"]
        self.text("Waiting for the UE's channel reports...", "small", DIM, tag="ch_status")
        body_h -= 34
        r1 = int(body_h * 0.27)
        r3 = int(body_h * 0.32)
        r2 = body_h - r1 - r3 - 2 * GAP - 6
        half = (inner_w - GAP) // 2
        sub_act = f"  ·  {act['title']}"

        @contextmanager
        def box(height):
            with dpg.child_window(width=half, height=height, border=True, no_scrollbar=True) as c:
                dpg.bind_item_theme(c, self.child_theme(rgb(SURFACE), rgb(LINE), pad=(14, 10), spacing=(6, 4)))
                yield c

        def heading(title, sub, sub_col=DIM):
            with dpg.group(horizontal=True):
                self.text(title, "kicker", DIM)
                self.text(sub, "kicker", sub_col)

        def heat(tag, rows, height, ylab="", yticks=None):
            cmap = dpg.mvPlotColormap_Viridis
            with dpg.group(horizontal=True):
                with dpg.plot(width=half - 104, height=height, no_menus=True, no_title=True, no_mouse_pos=True) as pl:
                    dpg.bind_colormap(pl, cmap)
                    xa = dpg.add_plot_axis(dpg.mvXAxis, label=SUBC)
                    with dpg.plot_axis(dpg.mvYAxis, label=ylab) as ya:
                        dpg.add_heat_series([0.0] * (rows * HEAT_COLS), rows, HEAT_COLS, scale_min=0.0, scale_max=1.0,
                                            bounds_min=(0, 0), bounds_max=(612, rows), format="", tag=tag)
                    dpg.set_axis_limits(xa, 0, 612)
                    dpg.set_axis_limits(ya, 0, rows)
                    self.t[tag + "_y"] = ya
                    if yticks:
                        dpg.set_axis_ticks(ya, yticks)
                self.axis_font(pl)
                dpg.add_colormap_scale(min_scale=0.0, max_scale=1.0, colormap=cmap, height=height, width=72, label="")

        def legacy_note(height):
            dpg.add_spacer(height=max(0, height // 2 - 50))
            self.text("Legacy CSI feedback carries only a beam index (PMI).", "body", MUTED)
            self.text("The gNB never reconstructs the channel, so there is nothing to show here.", "body", DIM)

        ticks = tuple((f"RX{r}-TX{c}", 3 - k + 0.5) for k, (r, c) in enumerate(LINKS))
        with dpg.group(horizontal=True, horizontal_spacing=GAP):
            with box(r1):
                heading("GROUND TRUTH  ·  CHANNEL RIGHT NOW", "  ·  |H| measured by the UE, all RX-TX links")
                heat("heat_truth_now", 4, r1 - 46, yticks=ticks)
            with box(r1):
                heading("RECONSTRUCTED AT THE gNB  ·  CHANNEL RIGHT NOW", sub_act, tcol(act))
                legacy_note(r1 - 46) if key == "legacy" else heat("heat_recon_now", 4, r1 - 46, yticks=ticks)
        dpg.add_spacer(height=GAP - 10)
        with dpg.group(horizontal=True, horizontal_spacing=GAP):
            with box(r2):
                heading("GROUND TRUTH  ·  RECENT REPORTS", "  ·  RX0-TX0, newest on top, scaled per report")
                heat("heat_truth_hist", 1, r2 - 46, "CSI report")
            with box(r2):
                heading("RECONSTRUCTED AT THE gNB  ·  RECENT REPORTS", sub_act, tcol(act))
                legacy_note(r2 - 46) if key == "legacy" else heat("heat_recon_hist", 1, r2 - 46, "CSI report")
        dpg.add_spacer(height=GAP - 10)
        with dpg.group(horizontal=True, horizontal_spacing=GAP):
            with box(r3):
                self.text("CSI-RS CHANNEL MAGNITUDE |H|  ·  LATEST REPORT, PER RX-TX LINK", "kicker", DIM)
                with dpg.plot(width=-1, height=r3 - 40, no_menus=True, no_title=True, no_mouse_pos=True) as pl:
                    dpg.add_plot_legend(location=dpg.mvPlot_Location_SouthEast)
                    xa = dpg.add_plot_axis(dpg.mvXAxis, label=SUBC)
                    dpg.set_axis_limits(xa, 0, 612)
                    with dpg.plot_axis(dpg.mvYAxis, label="|H| (normalized)") as ya:
                        for (r, c), col in zip(LINKS, (BLUE, ORANGE, V1_LIGHT, GREEN)):
                            s = dpg.add_line_series([], [], label=f"RX{r}-TX{c}")
                            dpg.bind_item_theme(s, self.series_theme(dpg.mvLineSeries, col, 255, 2.0))
                            self.t[f"link_{r}{c}"] = s
                    dpg.set_axis_limits(ya, 0.0, 1.05)
                self.axis_font(pl)
            with box(r3):
                self.text("CHANNEL COSINE SIMILARITY", "kicker", DIM)
                with dpg.plot(width=-1, height=r3 - 40, no_menus=True, no_title=True, no_mouse_pos=True) as pl:
                    dpg.add_plot_legend(location=dpg.mvPlot_Location_NorthWest)
                    xa = dpg.add_plot_axis(dpg.mvXAxis, label="Cosine similarity")
                    dpg.set_axis_limits(xa, 0.5, 1.0)
                    self.t["cos_x"] = xa
                    with dpg.plot_axis(dpg.mvYAxis, label="CDF") as ya:
                        for sc in SCENARIOS:
                            s = dpg.add_line_series([], [], label=f"  {sc['title']}")
                            th = self.res["line"] if sc["key"] == key else self.res["line_thin"]
                            dpg.bind_item_theme(s, th[sc["key"]])
                            self.t[f"cos_{sc['key']}"] = s
                    dpg.set_axis_limits(ya, 0.0, 1.02)
                self.axis_font(pl)

    # ------------------------------------------------------------------ live updates
    def refresh_system(self, view: DemoView):
        r = view.radio
        steps_key = tuple(view.steps)
        if steps_key != self._steps_key:
            self._steps_key = steps_key
            dpg.delete_item("stepper", children_only=True)
            sw = self.t["stepper_w"]
            step = (sw - 50) / (len(view.steps) - 1)
            col = {"ok": GREEN, "busy": ORANGE, "idle": "#55534E", "fail": RED}
            for k, (name, st) in enumerate(view.steps):
                x = 25 + k * step
                if k:
                    prev_ok = view.steps[k - 1][1] == "ok"
                    dpg.draw_line((x - step + 13, 16), (x - 13, 16), color=rgb(GREEN if prev_ok and st == "ok" else "#55534E"),
                                  thickness=2, parent="stepper")
                filled = st in ("ok", "fail")
                dpg.draw_circle((x, 16), 11, color=rgb(col[st]), fill=rgb(col[st]) if filled else rgb(SURFACE),
                                thickness=2, parent="stepper")
                dpg.draw_text((x - len(name) * 4, 34), name, color=rgb(MUTED if st != "idle" else DIM), size=15, parent="stepper")
        running = r.gnb_running or r.ue_running
        dpg.configure_item("btn_start", label="•  Running" if running else "Start system", enabled=not running)
        dpg.bind_item_theme("btn_start", self.res["t_running"] if running else self.res["t_off"])
        dpg.configure_item("btn_stop", enabled=running)
        dpg.bind_item_theme("btn_stop", self.res["t_off"])
        if not self.core_ok and not running:
            status = "Core network (OAI CN5G) is not running: start it with docker compose first."
        elif r.traffic_stage == "running" and r.ue_ip:
            status = f"Traffic running  ·  UE {r.ue_ip}  ·  UDP downlink"
        elif running:
            status = r.gnb_boot_label or r.ue_bringup_label or r.status
            if r.ue_running and r.ue_bringup_label:
                status = f"UE: {r.ue_bringup_label}"
        else:
            status = "Idle  ·  " + ("over the air (2 x USRP B210)" if self.ota else "RF simulator")
        dpg.set_value("status_line", status)
        ev = view.events[-1] if view.events else None
        dpg.set_value("event_line", f"{ev.clock}  {ev.text}" if ev else "")
        dpg.configure_item("event_line", color=rgb(RED if ev and ev.level == "fail" else DIM))
        dpg.delete_item("dots", children_only=True)
        radio_ok = r.gnb_boot_stage == "rf_ssb" and r.ue_running
        dots = (("Core", self.core_ok), ("gNB", r.gnb_running), ("UE", r.ue_running),
                ("USRP ×2" if self.ota else "RF sim", radio_ok), ("Telnet", bool(view.aifb)))
        x = 6
        for name, ok in dots:
            c = rgb(GREEN if ok else "#55534E")
            dpg.draw_circle((x, 12), 5, color=c, fill=c, parent="dots")
            dpg.draw_text((x + 10, 3), name, color=rgb(MUTED), size=15, parent="dots")
            x += 28 + len(name) * 8

    def refresh_plots(self, view: DemoView):
        if not view.samples:
            return
        t_end = view.samples[-1].t
        win = [s for s in view.samples if s.t >= t_end - WINDOW_S]
        t = np.array([s.t for s in win])
        scen = [s.scenario for s in win]
        segs, start = [], 0  # contiguous runs of one scenario, newest last
        for i in range(1, len(win) + 1):
            if i == len(win) or scen[i] != scen[start]:
                segs.append((start, i, scen[start]))
                start = i
        segs = segs[-POOL:]
        x_lo, x_hi = max(0.0, t_end - WINDOW_S), max(t_end, WINDOW_S)
        for name, attr, lab, lab_cdf in METRICS:
            y = np.array([getattr(s, attr) for s in win], dtype=float)
            finite = y[np.isfinite(y)]
            if attr == "tput_mbps":
                y_hi = max(10.0, float(np.ceil((finite.max() if finite.size else 10) * 1.15 / 5) * 5))
            elif attr == "mcs":
                y_hi = 30.0 if (finite.size and finite.max() > 15) else 16.0
            else:
                y_hi = max(0.25, float(np.ceil((finite.max() if finite.size else 0.25) * 1.2 * 20) / 20))
            dpg.set_axis_limits(self.t[f"{attr}_x"], x_lo, x_hi)
            dpg.set_axis_limits(self.t[f"{attr}_y"], 0.0, y_hi)
            yc = np.where(np.isfinite(y), y, 0.0)
            dpg.set_value(self.t[f"{attr}_raw"], [t.tolist(), yc.tolist()])
            for j in range(POOL):
                shade, line = self.t[f"{attr}_shade"][j], self.t[f"{attr}_line"][j]
                if j < len(segs):
                    a, b, key = segs[j]
                    xs = t[a:b].tolist()
                    x0 = (t[a - 1] + t[a]) / 2 if a > 0 else t[a]
                    x1 = (t[b - 1] + t[b]) / 2 if b < len(t) else t[b - 1]
                    sx = [x0] + xs + [x1]
                    dpg.set_value(shade, [sx, [y_hi] * len(sx), [0.0] * len(sx), [], []])
                    dpg.bind_item_theme(shade, self.res["shade"][key])
                    dpg.set_value(line, [xs, roll(yc[a:b].tolist())])
                    dpg.bind_item_theme(line, self.res["line"][key])
                else:
                    dpg.set_value(shade, [[], [], [], [], []])
                    dpg.set_value(line, [[], []])
            # CDFs over the whole session, per scenario
            hi = 0.0
            for key in KEYS:
                v = np.sort(np.array([getattr(s, attr) for s in view.samples if s.scenario == key], dtype=float))
                v = v[np.isfinite(v)]
                hi = max(hi, float(v.max()) if v.size else 0.0)
                if v.size:
                    idx = np.linspace(0, v.size - 1, min(v.size, 300)).astype(int)
                    dpg.set_value(self.t[f"{attr}_cdf_{key}"], [v[idx].tolist(), ((idx + 1) / v.size).tolist()])
                else:
                    dpg.set_value(self.t[f"{attr}_cdf_{key}"], [[], []])
            if attr == "tput_mbps":
                dpg.set_axis_limits(self.t[f"{attr}_cx"], 0.0, max(10.0, float(np.ceil(hi * 1.1 / 5) * 5)))
            elif attr == "mcs":
                dpg.set_axis_limits(self.t[f"{attr}_cx"], 0.0, 30.0 if hi > 15 else 16.0)
            else:
                dpg.set_axis_limits(self.t[f"{attr}_cx"], 0.0, max(0.25, float(np.ceil(hi * 1.2 * 20) / 20)))

    def refresh_channel(self, view: DemoView, rv: ReplayView):
        key = view.scenario
        if rv.error:
            dpg.set_value("ch_status", rv.error)
            return
        if rv.last is None:
            for k in KEYS:
                dpg.set_value(self.t[f"cos_{k}"], [[], []])
            dpg.set_value("ch_status", "Waiting for the UE's channel reports..." if view.radio.ue_running
                          else "Start the system to see the channel.")
            return
        last = rv.last
        n_live = sum(1 for k in rv.hist_keys if k == key)
        dpg.set_value("ch_status", f"{rv.n_reports} channel reports replayed  ·  frame {last.frame}  ·  "
                                   f"{rv.ms_per_report:.1f} ms each  ·  {SCENARIO[key]['title']}: {n_live} of the "
                                   f"last {len(rv.hist_keys)} reports  ·  each report counts only for the scenario "
                                   f"that was on air")
        tm = np.abs(last.truth)
        scale = float(tm.max()) or 1.0

        def cells(a):  # [..., 612] -> [..., 153]: mean of 4 subcarriers per heat-map cell
            return a.reshape(*a.shape[:-1], HEAT_COLS, -1).mean(axis=-1)

        def links(a):  # row 0 is drawn at the top: RX0-TX0 first, matching the tick labels
            return cells(np.stack([a[r, c] for r, c in LINKS]) / scale)

        dpg.set_value("heat_truth_now", [np.clip(links(tm), 0, 1).ravel().tolist()])
        rows = rv.hist_truth.shape[0]
        row_scale = rv.hist_truth.max(axis=1, keepdims=True)
        row_scale[row_scale == 0] = 1.0

        def hist(tag, a):
            dpg.configure_item(tag, rows=rows, cols=HEAT_COLS, bounds_max=(612, rows))
            dpg.set_axis_limits(self.t[tag + "_y"], 0, rows)
            dpg.set_value(tag, [np.clip(cells(a / row_scale)[::-1], 0, 1).ravel().tolist()])

        hist("heat_truth_hist", rv.hist_truth)
        if key != "legacy" and dpg.does_item_exist("heat_recon_now"):
            live = rv.last_live.get(key)  # latest report decoded while this scenario was on air
            now = links(np.abs(live.recon[key])) if live is not None else np.zeros((4, HEAT_COLS))
            dpg.set_value("heat_recon_now", [np.clip(now, 0, 1).ravel().tolist()])
            on_air = np.array([k == key for k in rv.hist_keys])[:, None]  # other rows stay dark
            hist("heat_recon_hist", np.where(on_air, rv.hist_recon[key], 0.0))
        x = list(range(612))
        for r, c in LINKS:
            dpg.set_value(self.t[f"link_{r}{c}"], [x, (tm[r, c] / scale).tolist()])
        lo = 0.5
        for k in KEYS:
            v = rv.beam_cos.get(k)
            if v is not None and v.size:
                v = np.sort(v)
                lo = min(lo, float(np.floor(v[int(0.02 * (v.size - 1))] * 10) / 10))  # keep 98% of every curve
                idx = np.linspace(0, v.size - 1, min(v.size, 400)).astype(int)
                dpg.set_value(self.t[f"cos_{k}"], [v[idx].tolist(), ((idx + 1) / v.size).tolist()])
            else:
                dpg.set_value(self.t[f"cos_{k}"], [[], []])  # not on air this session (yet)
        dpg.set_axis_limits(self.t["cos_x"], lo, 1.0)

    # ------------------------------------------------------------------ main loop
    def on_click(self):
        for key, card in self.cards.items():
            if dpg.does_item_exist(card) and dpg.is_item_hovered(card):
                self.state["scenario"] = key

    def run(self):
        log_dir = REPO / "data/runs/gui_logs"  # the GUI's own events, kept next to the softmodem logs
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / time.strftime("gui_%Y%m%d_%H%M%S.log")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                            handlers=[logging.StreamHandler(), logging.FileHandler(log_file)])
        log.info("profile %s, GUI log %s", self.args.profile, log_file)
        dpg.create_context()
        dpg.create_viewport(title="Interoperable AI-Native CSI Feedback · Live OAI prototype", width=1920, height=1080)
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.toggle_viewport_fullscreen() if self.args.fullscreen else dpg.maximize_viewport()
        self.resources()
        with dpg.handler_registry():
            dpg.add_key_press_handler(dpg.mvKey_F11, callback=lambda: self.state.update(fullscreen=True))
            dpg.add_mouse_click_handler(button=dpg.mvMouseButton_Left, callback=self.on_click)
            for k, key in ((dpg.mvKey_1, "legacy"), (dpg.mvKey_2, "same"), (dpg.mvKey_3, "cross")):
                dpg.add_key_press_handler(k, callback=lambda s_, a_, u: self.state.update(scenario=u), user_data=key)
        self.demo.start()
        self.replay.start()
        threading.Thread(target=self._core_watch, name="core-watch", daemon=True).start()
        script = sorted(self._parse_script(self.args.script))
        t0 = time.monotonic()
        frame, last_refresh = 0, 0.0
        try:
            while dpg.is_dearpygui_running():
                frame_t = time.monotonic()
                frame += 1
                now = frame_t - t0
                while script and script[0][0] <= now:
                    self._script_action(script.pop(0)[1])
                if self.state.pop("fullscreen", False):
                    dpg.toggle_viewport_fullscreen()
                if "scenario" in self.state:
                    self.demo.set_scenario(self.state.pop("scenario"))
                if self.state.pop("start", False):
                    self.replay.clear()  # a new session: new channel history and cosine CDFs
                    self.demo.start_system()
                if self.state.pop("stop", False):
                    threading.Thread(target=self.demo.stop_system, name="demo-stop", daemon=True).start()
                if "tab" in self.state:
                    tab = self.state.pop("tab")
                    self.state["tab_now"] = "channel" if dpg.get_item_alias(tab) == "tab_channel" else "demo"
                cur = (dpg.get_viewport_client_width(), dpg.get_viewport_client_height())
                build_key = (cur, *self.demo.state_key())
                if frame >= 6 and build_key != self._built_key:
                    if dpg.does_item_exist("main"):
                        dpg.delete_item("main")
                    self._steps_key = None
                    self.build(*cur, self.demo.view())
                    self._built_key = build_key
                    last_refresh = 0.0
                if self._built_key and frame_t - last_refresh >= 0.5:
                    last_refresh = frame_t
                    view = self.demo.view()
                    self.refresh_system(view)
                    self.refresh_plots(view)
                    if self.state.get("tab_now") == "channel":
                        self.refresh_channel(view, self.replay.view())
                dpg.render_dearpygui_frame()
                fps = 20 if self.state.get("tab_now") == "channel" else 30  # leave the CPU to the radios
                time.sleep(max(0.0, 1 / fps - (time.monotonic() - frame_t)))
        finally:
            log.info("window closed: stopping the system")
            self.replay.stop()
            self.demo.shutdown()
            dpg.destroy_context()

    def _core_watch(self):
        while True:
            self.core_ok = core_network_up()
            time.sleep(5.0)

    # ------------------------------------------------------------------ scripted runs (testing / screenshots)
    @staticmethod
    def _parse_script(text: str):
        out = []
        for item in filter(None, (text or "").split(",")):
            at, action = item.split(":", 1)
            out.append((float(at), action))
        return out

    def _script_action(self, action: str):
        log.info("script: %s", action)
        if action in KEYS:
            self.state["scenario"] = action
        elif action == "start":
            self.state["start"] = True
        elif action == "stop":
            self.state["stop"] = True
        elif action in ("demo", "channel"):
            dpg.set_value("tabbar", "tab_channel" if action == "channel" else "tab_demo")
            self.state["tab_now"] = action
            self._built_key = None
        elif action.startswith("shot="):
            dpg.output_frame_buffer(file=action[5:])
        elif action == "quit":
            dpg.stop_dearpygui()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--profile", default=str(REPO / "configs/demo_usrp.yaml"))
    ap.add_argument("--fullscreen", action="store_true")
    ap.add_argument("--script", default="", help="testing: comma-separated <seconds>:<action>, actions "
                    "start | stop | legacy | same | cross | demo | channel | shot=<png> | quit")
    args = ap.parse_args()
    # Checked before anything starts: the controller clears stray softmodems and creates run folders.
    if os.geteuid() == 0:
        sys.exit("Run the GUI as your normal user, not as root (no sudo / su): it starts the gNB and UE through\n"
                 "the passwordless sudo rule (/etc/sudoers.d/pyoaicsinet), and root has no access to your display.")
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        sys.exit("No display: start the GUI from a terminal inside the desktop session (DISPLAY is not set).")
    App(args).run()


if __name__ == "__main__":
    main()
