"""Same-laptop booth GUI, mockup (static replay of the 2026-09-24 OTA runs). Not wired to OAI yet.

    python -m same_laptop.mockup                                    # open the window (F11 fullscreen)
    python -m same_laptop.mockup --active same --screenshot a.png   # legacy | same | cross
    python -m same_laptop.mockup --tab channel --screenshot b.png
Run from the repository root. Keys 1 / 2 / 3 or a click on a scenario card switch the scenario.

Design for the "UE local" setup (gNB, UE, core and GUI on this laptop). The separate-laptops design lives in
separate_laptops/.

Data (data/mockups/, regenerate with tools/export_mockup_*.py): per-second metrics of OTA runs 01 (legacy),
02 (same vendor) and 06 (cross vendor, translation at the gNB), and, all on the channels of run 02 so the
scenarios are compared on identical channels:
  mockup_channels.npz: 120 consecutive UE channel reports with the model-set-B reconstructions;
  mockup_cosine.npz: per-sub-band cosine similarity |v^H w| between the true channel's best beam v and the
  beam w the gNB gets (AI: from the reconstructed channel; legacy: the codeword of the reported PMI),
  all 5075 reports x 13 sub-bands.
"""

from __future__ import annotations

import argparse
import csv
from contextlib import contextmanager

import dearpygui.dearpygui as dpg
import numpy as np

from common.style import (viewport_icons, BG, BLUE, BLUE_2, BOX_EDGE, DIM, FONTS, GRAY, GREEN, INK, LINE, LOGO_FILES, LOGOS,
                          MOCKUP_DATA, MUTED, ORANGE, PALE, SCENARIOS, SURFACE, TEXT, TITLE, V1, V1_LIGHT, V2,
                          V2_LIGHT, mix, rgb, roll)

CHANNELS = MOCKUP_DATA / "mockup_channels.npz"
COSINE = MOCKUP_DATA / "mockup_cosine.npz"
SEG_S = 60
RES: dict = {}       # fonts / textures / global theme, created once
STATE: dict = {}     # pending interactive change (scenario / tab / fullscreen), applied between frames
CARDS: dict = {}     # scenario key -> card item, for click detection


def load():
    runs = {}
    for s in SCENARIOS:
        runs[s["key"]] = [r for r in csv.DictReader(open(MOCKUP_DATA / "runs" / s["run"] / "metrics.csv"))
                          if float(r["dl_mbps"]) > 0.5]
    ch = dict(np.load(CHANNELS))
    cos = dict(np.load(COSINE))
    return runs, ch, cos


def build(args, W: int, H: int) -> None:
    runs, ch, cos = load()
    act_i = [s["key"] for s in SCENARIOS].index(args.active)
    act = SCENARIOS[act_i]
    # timeline: 60 s of each scenario back to back (legacy -> same -> cross), as if switched live
    t, tput, mcs, bler, seg = [], [], [], [], []
    for i, s in enumerate(SCENARIOS):
        for j, r in enumerate(runs[s["key"]][20: 20 + SEG_S]):
            t.append(i * SEG_S + j)
            tput.append(float(r["dl_mbps"]))
            mcs.append(float(r["mcs"]))
            bler.append(float(r["bler"]))
            seg.append(i)

    # ---------------------------------------------------------------- fonts, textures, themes (created once)
    if not RES:
        def font(name, size):
            return dpg.add_font(str(FONTS / name), size)  # Ubuntu has no arrows / U+25CF: avoid them in text

        with dpg.font_registry():
            f_body = font("Ubuntu-R.ttf", 18)
            f_small = font("Ubuntu-R.ttf", 15)
            f_med = font("Ubuntu-M.ttf", 18)
            f_h2 = font("Ubuntu-M.ttf", 22)
            f_title = font("Ubuntu-B.ttf", 23)
            f_kicker = font("Ubuntu-M.ttf", 14)
            f_draw = font("Ubuntu-M.ttf", 20)
        dpg.bind_font(f_body)

        logos = []
        with dpg.texture_registry():
            for name, h in LOGO_FILES:
                w_, h_, _, d = dpg.load_image(str(LOGOS / name))
                logos.append((dpg.add_static_texture(w_, h_, d), int(w_ * h / h_), h))

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
        RES.update(f_body=f_body, f_small=f_small, f_med=f_med, f_h2=f_h2, f_title=f_title, f_kicker=f_kicker,
                   f_draw=f_draw, logos=logos)
    f_body, f_small, f_med, f_h2 = RES["f_body"], RES["f_small"], RES["f_med"], RES["f_h2"]
    f_title, f_kicker, f_draw, logos = RES["f_title"], RES["f_kicker"], RES["f_draw"], RES["logos"]

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

    def text(s, font=None, color=None, wrap=0):
        kw = dict(wrap=wrap)
        if color:
            kw["color"] = rgb(color) if isinstance(color, str) else color
        item = dpg.add_text(s, **kw)
        if font:
            dpg.bind_item_font(item, font)
        return item

    def tcol(sc):
        return sc.get("text", sc["color"])

    def panel(width, height, title, pad=(16, 12), spacing=(8, 6)):
        cw = dpg.child_window(width=width, height=height, border=True, no_scrollbar=True)
        return cw, title, pad, spacing

    def axis_font(plot):
        for ax in dpg.get_item_children(plot, 1) or []:
            dpg.bind_item_font(ax, f_small)

    # ---------------------------------------------------------------- layout from the real client size
    PAD, GAP = 18, 12
    inner_w = W - 2 * PAD
    HEADER_H, TAB_H = 78, 46
    body_h = H - HEADER_H - TAB_H - 2 * PAD - 26
    LEFT_W = max(400, int(inner_w * 0.225))
    RIGHT_W = inner_w - LEFT_W - GAP - 4
    DIA_H = int(body_h * 0.43)
    DIA_H = min(DIA_H, int(44 + DIA_DESIGN_H * 0.88 * diagram_scale(RIGHT_W - 34, DIA_H - 44) + 18))  # slightly smaller
    PLOT_H = body_h - DIA_H - GAP - 4
    CARD_H = 104

    with dpg.window(tag="main", no_scrollbar=True):
        # ------------------------------------------------------------ header band (light, logos in original colours)
        # Items are placed at absolute positions so the title and every logo share the band's midline and the
        # logos keep the same right margin as the title's left margin.
        HEADER_W = W - 2 * PAD
        MARGIN, SEP_W = 24, 34
        with dpg.child_window(height=HEADER_H, border=False, no_scrollbar=True) as header:
            dpg.bind_item_theme(header, child_theme(rgb(PALE), rgb(PALE), 0, pad=(0, 0), spacing=(0, 0)))
            mid = HEADER_H / 2
            logos_w = sum(w_ for _, w_, _ in logos) + SEP_W * (len(logos) - 1)
            x0 = HEADER_W - MARGIN - logos_w
            # one drawing layer behind everything (DPG keeps drawlists at the child origin): separators only
            with dpg.drawlist(width=HEADER_W, height=HEADER_H):
                x = x0
                for i, (_, w_, _) in enumerate(logos[:-1]):
                    x += w_
                    dpg.draw_line((x + SEP_W / 2, mid - 21), (x + SEP_W / 2, mid + 21), color=rgb("#D8D4C8"), thickness=1.5)
                    x += SEP_W
            title = text(TITLE, f_title, INK)
            dpg.set_item_pos(title, (MARGIN, int(mid - 23 * 0.86)))  # Ubuntu Bold 23 px: cap height centred
            x = x0
            for i, (tag, w_, h_) in enumerate(logos):
                img = dpg.add_image(tag, width=w_, height=h_)
                dpg.set_item_pos(img, (x, int(mid - h_ / 2)))
                x += w_ + SEP_W

        with dpg.tab_bar(tag="tabbar"):
            # ======================================================== LIVE DEMO
            with dpg.tab(label="  Live demo  ", tag="tab_demo"):
                with dpg.group(horizontal=True):
                    # ---------------- left column: scenarios + system
                    with dpg.child_window(width=LEFT_W, height=body_h, border=False, no_scrollbar=True) as left:
                        dpg.bind_item_theme(left, child_theme(rgb(BG), rgb(BG)))
                        text("CSI FEEDBACK SCENARIOS", f_kicker, DIM)
                        for i, sc in enumerate(SCENARIOS):
                            on = i == act_i
                            with dpg.child_window(height=CARD_H, border=True, no_scrollbar=True) as card:
                                CARDS[sc["key"]] = card  # clicked = hovered when the mouse button goes down
                                dpg.bind_item_theme(card, child_theme(mix(sc["color"], SURFACE, 0.22) if on else rgb(SURFACE),
                                                                      rgb(sc["color"]) if on else rgb(LINE),
                                                                      pad=(16, 12), spacing=(10, 6)))
                                with dpg.group(horizontal=True):
                                    with dpg.drawlist(width=34, height=34):
                                        dpg.draw_circle((16, 16), 15, color=rgb(sc["color"]),
                                                        fill=rgb(sc["color"]) if on else rgb(SURFACE), thickness=2)
                                        dpg.draw_text((11 if sc["n"] != 1 else 12, 6), str(sc["n"]),
                                                      color=rgb(PALE if on else tcol(sc)), size=19)
                                    text(sc["title"], f_h2, TEXT if on else MUTED)
                                text(sc["sub"], f_small, MUTED if on else DIM, wrap=LEFT_W - 50)
                            dpg.add_spacer(height=2)
                        text("Press 1 · 2 · 3 or click a scenario to switch live.", f_small, DIM, wrap=LEFT_W - 10)
                        dpg.add_spacer(height=4)
                        with dpg.child_window(height=-1, border=True, no_scrollbar=True) as sysc:
                            dpg.bind_item_theme(sysc, child_theme(rgb(SURFACE), rgb(LINE), pad=(16, 12), spacing=(10, 8)))
                            text("SYSTEM", f_kicker, DIM)
                            bw = (LEFT_W - 60) // 2
                            with dpg.group(horizontal=True):
                                b1 = dpg.add_button(label="•  Running", width=bw, height=44, enabled=False)
                                b2 = dpg.add_button(label="Stop system", width=bw, height=44)
                            with dpg.theme() as t_start:
                                with dpg.theme_component(dpg.mvButton, enabled_state=False):
                                    dpg.add_theme_color(dpg.mvThemeCol_Button, rgb("#26332B"))
                                    dpg.add_theme_color(dpg.mvThemeCol_Text, rgb(GREEN))
                            dpg.bind_item_theme(b1, t_start)
                            dpg.bind_item_font(b1, f_med)
                            dpg.bind_item_font(b2, f_med)
                            steps = ["gNB", "USRPs", "UE sync", "Attach", "IP", "Traffic"]
                            sw = LEFT_W - 50
                            step = (sw - 50) / (len(steps) - 1)
                            with dpg.drawlist(width=sw, height=60):
                                for k, name in enumerate(steps):
                                    x = 25 + k * step
                                    if k:
                                        dpg.draw_line((x - step + 13, 16), (x - 13, 16), color=rgb(GREEN), thickness=2)
                                    dpg.draw_circle((x, 16), 11, color=rgb(GREEN), fill=rgb(GREEN))
                                    dpg.draw_text((x - len(name) * 4, 34), name, color=rgb(MUTED), size=15)
                            text("Traffic running  ·  UE 10.0.0.3  ·  UDP downlink", f_small, MUTED)
                            with dpg.group(horizontal=True):
                                for name in ("Core", "gNB", "UE", "USRP ×2", "Telnet"):
                                    with dpg.drawlist(width=14, height=20):
                                        dpg.draw_circle((7, 11), 5, color=rgb(GREEN), fill=rgb(GREEN))
                                    text(name, f_small, MUTED)

                    # ---------------- right area
                    with dpg.child_window(width=RIGHT_W, height=body_h, border=False, no_scrollbar=True) as right:
                        dpg.bind_item_theme(right, child_theme(rgb(BG), rgb(BG)))
                        with dpg.child_window(height=DIA_H, border=True, no_scrollbar=True) as dia:
                            dpg.bind_item_theme(dia, child_theme(rgb(SURFACE), rgb(LINE), pad=(16, 12), spacing=(8, 4)))
                            text("SYSTEM DIAGRAM  ·  " + act["title"].upper(), f_kicker, tcol(act))
                            draw_diagram(act, RIGHT_W - 34, DIA_H - 58, ch, f_draw)  # 14 px bottom margin

                        # plots: one row per metric, time series (left) and its CDF (right)
                        col_w = (RIGHT_W - GAP) // 2
                        ph = (PLOT_H - 2 * 10 + 10) // 3  # rows are separated by the 10 px item spacing
                        plot_h = ph - 2 * 8 - 24  # child padding + heading
                        cdf = {col: {s["key"]: sorted(float(r[col]) for r in runs[s["key"]]) for s in SCENARIOS}
                               for col in ("dl_mbps", "mcs", "bler")}
                        # (title, values, csv column, y label of the time plot, x label of the CDF, y range, CDF x range)
                        rows = (("THROUGHPUT", tput, "dl_mbps", "Mbps", "Throughput (Mbps)", (0.0, 9.0), (3.0, 9.0)),
                                ("MCS", mcs, "mcs", "MCS", "MCS index", (0.0, 16.0), (4.0, 14.0)),
                                ("BLER", bler, "bler", "BLER", "BLER", (0.0, 0.25), (0.0, 0.25)))

                        for k, (name, ys, col, lab, lab_cdf, ylim, xlim_cdf) in enumerate(rows):
                            with dpg.group(horizontal=True, horizontal_spacing=GAP):
                                with dpg.child_window(width=col_w, height=ph, border=True, no_scrollbar=True) as pc:
                                    dpg.bind_item_theme(pc, child_theme(rgb(SURFACE), rgb(LINE), pad=(14, 8), spacing=(6, 2)))
                                    text("DOWNLINK " + name, f_kicker, DIM)
                                    with dpg.plot(width=-1, height=plot_h, no_menus=True, no_title=True, no_mouse_pos=True) as pl:
                                        xa = dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)")
                                        dpg.set_axis_limits(xa, 0, len(t))
                                        with dpg.plot_axis(dpg.mvYAxis, label=lab) as ya:
                                            for i, sc in enumerate(SCENARIOS):
                                                xs = [x for x, g in zip(t, seg) if g == i]
                                                sh = dpg.add_shade_series(xs, [ylim[1]] * len(xs), y2=[ylim[0]] * len(xs))
                                                dpg.bind_item_theme(sh, series_theme(dpg.mvShadeSeries, sc["color"], 40))
                                            ln = dpg.add_line_series(t, ys)
                                            dpg.bind_item_theme(ln, series_theme(dpg.mvLineSeries, MUTED, 100, 1.2))
                                            for i, sc in enumerate(SCENARIOS):
                                                xs = [x for x, g in zip(t, seg) if g == i]
                                                av = dpg.add_line_series(xs, roll([y for y, g in zip(ys, seg) if g == i]))
                                                dpg.bind_item_theme(av, series_theme(dpg.mvLineSeries, tcol(sc), 255, 3.0))
                                        dpg.set_axis_limits(ya, *ylim)
                                    axis_font(pl)
                                with dpg.child_window(width=col_w, height=ph, border=True, no_scrollbar=True) as pc:
                                    dpg.bind_item_theme(pc, child_theme(rgb(SURFACE), rgb(LINE), pad=(14, 8), spacing=(6, 2)))
                                    with dpg.group(horizontal=True):
                                        text(name + " CDF", f_kicker, DIM)
                                        if k == 0:  # one colour key for all six plots, in the heading line
                                            for sc, short in zip(SCENARIOS, ("Legacy", "Same vendor", "Cross vendor")):
                                                dpg.add_spacer(width=10)
                                                with dpg.drawlist(width=16, height=14):
                                                    dpg.draw_line((0, 8), (16, 8), color=rgb(tcol(sc)), thickness=3)
                                                text(short, f_kicker, tcol(sc))
                                    with dpg.plot(width=-1, height=plot_h, no_menus=True, no_title=True, no_mouse_pos=True) as pl:
                                        xa = dpg.add_plot_axis(dpg.mvXAxis, label=lab_cdf)
                                        dpg.set_axis_limits(xa, *xlim_cdf)
                                        with dpg.plot_axis(dpg.mvYAxis, label="CDF") as ya:
                                            for sc in SCENARIOS:
                                                v = cdf[col][sc["key"]]
                                                ser = dpg.add_line_series(v, [(j + 1) / len(v) for j in range(len(v))],
                                                                          label=f"  {sc['title']}")
                                                dpg.bind_item_theme(ser, series_theme(dpg.mvLineSeries, tcol(sc), 255, 3.0))
                                        dpg.set_axis_limits(ya, 0, 1.02)
                                    axis_font(pl)

            # ======================================================== CHANNEL VISUALIZATION
            with dpg.tab(label="  Channel visualization  ", tag="tab_channel"):
                build_channel_tab(act, ch, cos, inner_w, body_h, GAP, f_kicker, f_small, f_body, text, child_theme,
                                  series_theme, axis_font, tcol)

    if args.tab == "channel":
        dpg.set_value("tabbar", "tab_channel")
    dpg.set_primary_window("main", True)


# ---------------------------------------------------------------------------- trailer-style system diagram
# Design units. Each node panel is fitted to its blocks; one scale for all scenarios (set by the widest, cross
# vendor) so blocks keep the same size when switching, and every diagram is centred in the panel.
DIA_DESIGN_H = 300
UE_W, OTA_GAP = 495, 260
GNB_W = {"legacy": 560, "same": 790, "cross": 836}
DIA_DESIGN_W = UE_W + OTA_GAP + max(GNB_W.values())


def diagram_scale(DW, DH):
    return min(DW / DIA_DESIGN_W, DH / DIA_DESIGN_H)


def draw_diagram(act, DW, DH, ch, f_draw, focus=None, bits=False):
    """One diagram per scenario, drawn in design units, uniformly scaled and centred in DW x DH.

    focus "ue" / "gnb" (separate-laptop stations): the other node panel is dimmed and this one is tagged
    THIS LAPTOP. bits: the over-the-air label gives the feedback size per report."""
    key = act["key"]
    s_ = diagram_scale(DW, DH)
    width = UE_W + OTA_GAP + GNB_W[key]
    ox, oy = (DW - width * s_) / 2, (DH - DIA_DESIGN_H * s_) / 2
    P = lambda x, y: (ox + x * s_, oy + y * s_)  # noqa: E731
    F = lambda z: max(12.0, z * s_ * 1.1)  # noqa: E731
    G = UE_W + OTA_GAP  # gNB panel left edge

    def label(x, y, txt, color, size, center=True):
        lines = txt.split("\n")
        for k, ln in enumerate(lines):
            w = len(ln) * F(size) * 0.56
            px, py = P(x, y)
            dpg.draw_text((px - (w / 2 if center else 0), py + k * F(size) * 1.15 - (len(lines) - 1) * F(size) * 0.58),
                          ln, color=rgb(color), size=F(size))

    def white_box(x0, x1, txt, edge=BOX_EDGE, ink=INK, y0=118, y1=228):
        dpg.draw_rectangle(P(x0, y0), P(x1, y1), color=rgb(edge), fill=rgb(PALE), rounding=12 * s_, thickness=3)
        label((x0 + x1) / 2, (y0 + y1) / 2 - 6, txt, ink, 20)

    def grey_box(x0, x1, txt):
        dpg.draw_rectangle(P(x0, 112), P(x1, 234), color=rgb(GRAY), fill=rgb("#3B4552"), rounding=12 * s_, thickness=3)
        label((x0 + x1) / 2, 166, txt, PALE, 20)

    def trapezoid(x0, x1, kind, fill, light, txt):
        y0, y1, inset = 100, 246, 34
        if kind == "enc":  # wide input, narrow output
            pts = (P(x0, y0), P(x1, y0 + inset), P(x1, y1 - inset), P(x0, y1))
        else:
            pts = (P(x0, y0 + inset), P(x1, y0), P(x1, y1), P(x0, y1 - inset))
        dpg.draw_quad(*pts, color=rgb(light), fill=rgb(fill), thickness=3)
        label((x0 + x1) / 2, (y0 + y1) / 2 - 6, txt, PALE, 20)

    def arrow(x0, x1, y=173, color=BLUE):
        dpg.draw_line(P(x0, y), P(x1 - 10, y), color=rgb(color), thickness=4 * s_)
        dpg.draw_triangle(P(x1, y), P(x1 - 14, y - 9), P(x1 - 14, y + 9), color=rgb(color), fill=rgb(color))

    def ota_arc(x0, x1, color, top, bottom):
        """One colour for curve and head; the head follows the curve's end tangent."""
        c0, c1, c2 = np.array((x0, 168.0)), np.array((x0 + 90, 70.0)), np.array((x1 - 90, 70.0))
        tip = np.array((x1, 172.0))
        d = (tip - c2) / np.linalg.norm(tip - c2)
        n = np.array((-d[1], d[0]))
        base = tip - 22 * d  # the curve ends where the head starts
        dpg.draw_bezier_cubic(P(*c0), P(*c1), P(*c2), P(*base), color=rgb(color), thickness=4 * s_, segments=48)
        dpg.draw_triangle(P(*tip), P(*(base + 10 * n)), P(*(base - 10 * n)), color=rgb(color), fill=rgb(color))
        label((x0 + x1) / 2, 52, top, PALE, 22)
        label((x0 + x1) / 2, 196, bottom, MUTED, 16)

    def node_panel(x0, x1, title, vendor, vendor_col):
        dpg.draw_rectangle(P(x0, 8), P(x1, 292), color=rgb("#46566A"), fill=rgb("#1C2733"), rounding=22 * s_, thickness=2)
        label(x0 + 28, 34, title, PALE, 30, center=False)
        if vendor:
            label(x0 + 28 + len(title) * 30 * 0.6 + 14, 40, vendor, vendor_col, 16, center=False)

    with dpg.drawlist(width=DW, height=DH) as dl:
        dpg.bind_item_font(dl, f_draw)
        if key == "legacy":
            node_panel(0, UE_W, "OAI UE", "", MUTED)
            node_panel(G, G + GNB_W[key], "OAI gNB", "", MUTED)
            white_box(25, 205, "MEASURED\nCHANNEL")
            arrow(207, 240)
            grey_box(245, 470, "CODEBOOK\nSEARCH")
            ota_arc(473, G + 32, GRAY, "OTA", "2-BIT BEAM INDEX + CQI" if bits else "PMI · BEAM INDEX")
            grey_box(G + 35, G + 265, "CODEBOOK\nLOOKUP")
            arrow(G + 267, G + 310)
            white_box(G + 315, G + 530, "PRECODING", edge=BLUE_2)
            label(G + GNB_W[key] / 2, 262, "No channel reconstruction  ·  beam index only", DIM, 17)
        else:
            gnb_vendor, gnb_col = ("VENDOR 1", V1_LIGHT) if key == "same" else ("VENDOR 2", V2_LIGHT)
            node_panel(0, UE_W, "OAI UE", "VENDOR 1", V1_LIGHT)
            node_panel(G, G + GNB_W[key], "OAI gNB", gnb_vendor, gnb_col)
            white_box(25, 205, "MEASURED\nCHANNEL")
            arrow(207, 240)
            trapezoid(245, 470, "enc", V1, V1_LIGHT, "AI\nENCODER")
            if key == "same":
                ota_arc(473, G + 32, BLUE, "OTA", "48-BIT LATENT + CQI" if bits else "CHANNEL FEEDBACK")
                trapezoid(G + 35, G + 270, "dec", V1, V1_LIGHT, "AI\nDECODER")
                arrow(G + 272, G + 315)
                rx0, rx1 = G + 320, G + 530
            else:
                ota_arc(473, G + 27, BLUE, "OTA", "48-BIT LATENT + CQI" if bits else "CHANNEL FEEDBACK")
                dpg.draw_rectangle(P(G + 30, 112), P(G + 182, 234), color=rgb(ORANGE), fill=rgb(ORANGE), rounding=14 * s_,
                                   thickness=3)
                dpg.draw_rectangle(P(G + 38, 120), P(G + 174, 226), color=rgb(PALE), fill=rgb(ORANGE), rounding=10 * s_,
                                   thickness=2)
                label(G + 106, 166, "LINEAR\nTRANSFORM", PALE, 20)
                arrow(G + 184, G + 212)
                trapezoid(G + 214, G + 378, "dec", V2, V2_LIGHT, "AI\nDECODER")
                arrow(G + 380, G + 408)
                rx0, rx1 = G + 412, G + 606
            white_box(rx0, rx1, "RECONSTRUCTED\nCHANNEL", edge="#7E8794")
            arrow(rx1 + 2, rx1 + 34)
            white_box(rx1 + 38, G + GNB_W[key] - 30, "PRECODING", edge=BLUE_2)
        if focus in ("ue", "gnb"):
            other = (G, G + GNB_W[key]) if focus == "ue" else (0, UE_W)
            mine = (0, UE_W) if focus == "ue" else (G, G + GNB_W[key])
            dpg.draw_rectangle(P(other[0] - 4, 4), P(other[1] + 4, 296), color=rgb(BG, 0), fill=rgb(BG, 175),
                               rounding=22 * s_)
            label(mine[1] - 28 - 11 * 15 * 1.1 * 0.56, 34, "THIS LAPTOP", ORANGE, 15, center=False)  # right-aligned


# ---------------------------------------------------------------------------- channel visualization tab
LINKS = ((0, 0), (0, 1), (1, 0), (1, 1))
SUBC = "Subcarrier (occupied, 612)"


def build_channel_tab(act, ch, cos, inner_w, body_h, GAP, f_kicker, f_small, f_body, text, child_theme, series_theme,
                      axis_font, tcol):
    key = act["key"]
    T = ch["true_cfr"]  # [N, rx, tx, 612]
    P = ch[f"{key}_cfr"] if key != "legacy" else None
    r1 = int(body_h * 0.27)
    r3 = int(body_h * 0.32)
    r2 = body_h - r1 - r3 - 2 * GAP - 6
    half = (inner_w - GAP) // 2
    sub_act = f"  ·  {act['title']}"

    @contextmanager
    def box(height):
        with dpg.child_window(width=half, height=height, border=True, no_scrollbar=True) as c:
            dpg.bind_item_theme(c, child_theme(rgb(SURFACE), rgb(LINE), pad=(14, 10), spacing=(6, 4)))
            yield c

    def heading(title, sub, sub_col=DIM):
        with dpg.group(horizontal=True):
            text(title, f_kicker, DIM)
            text(sub, f_kicker, sub_col)

    def heat(values, height, ylab="", yticks=None, cmap=dpg.mvPlotColormap_Viridis, lo=0.0, hi=1.0):
        rows, cols = values.shape
        with dpg.group(horizontal=True):
            with dpg.plot(width=half - 104, height=height, no_menus=True, no_title=True, no_mouse_pos=True) as pl:
                dpg.bind_colormap(pl, cmap)
                xa = dpg.add_plot_axis(dpg.mvXAxis, label=SUBC)
                with dpg.plot_axis(dpg.mvYAxis, label=ylab) as ya:
                    dpg.add_heat_series(np.clip(values, lo, hi).flatten().tolist(), rows, cols, scale_min=lo, scale_max=hi,
                                        bounds_min=(0, 0), bounds_max=(cols, rows), format="")
                dpg.set_axis_limits(xa, 0, cols)
                dpg.set_axis_limits(ya, 0, rows)
                if yticks:
                    dpg.set_axis_ticks(ya, yticks)
            axis_font(pl)
            dpg.add_colormap_scale(min_scale=lo, max_scale=hi, colormap=cmap, height=height, width=72, label="")

    def legacy_note(height):
        dpg.add_spacer(height=max(0, height // 2 - 50))
        text("Legacy CSI feedback carries only a beam index (PMI).", f_body, MUTED)
        text("The gNB never reconstructs the channel, so there is nothing to show here.", f_body, DIM)

    def plot(height, series, xlab, ylab, xlim, ylim):
        """series: (label, xs, ys, colour, weight)."""
        with dpg.plot(width=-1, height=height, no_menus=True, no_title=True, no_mouse_pos=True) as pl:
            dpg.add_plot_legend(location=dpg.mvPlot_Location_SouthEast if ylab != "CDF" else dpg.mvPlot_Location_NorthWest)
            xa = dpg.add_plot_axis(dpg.mvXAxis, label=xlab)
            with dpg.plot_axis(dpg.mvYAxis, label=ylab) as ya:
                for lab, xs, ys, col, w in series:
                    ser = dpg.add_line_series(np.asarray(xs).tolist(), np.asarray(ys).tolist(), label=lab)
                    dpg.bind_item_theme(ser, series_theme(dpg.mvLineSeries, col, 255, w))
            dpg.set_axis_limits(xa, *xlim)
            dpg.set_axis_limits(ya, *ylim)
        axis_font(pl)

    # rows 1-2: |H| per subcarrier, ground truth vs reconstruction, one shared scale (level errors stay visible)
    tm = np.abs(T)
    pm = np.abs(P) if P is not None else None
    link_ticks = tuple((f"RX{r}-TX{c}", 3 - k + 0.5) for k, (r, c) in enumerate(LINKS))
    scale_now = float(tm[-1].max())

    def image(a):  # DPG draws heat-series row 0 at the TOP: RX0-TX0 first so the rows match link_ticks
        return np.stack([a[r, c] for r, c in LINKS]) / scale_now

    row_scale = tm[:, 0, 0].max(axis=1, keepdims=True)  # per report, from the ground truth, applied to both
    with dpg.group(horizontal=True, horizontal_spacing=GAP):
        with box(r1):
            heading("GROUND TRUTH  ·  CHANNEL RIGHT NOW", "  ·  |H| measured by the UE, all RX-TX links")
            heat(image(tm[-1]), r1 - 46, yticks=link_ticks)
        with box(r1):
            heading("RECONSTRUCTED AT THE gNB  ·  CHANNEL RIGHT NOW", sub_act, tcol(act))
            legacy_note(r1 - 46) if pm is None else heat(image(pm[-1]), r1 - 46, yticks=link_ticks)
    dpg.add_spacer(height=GAP - 10)
    with dpg.group(horizontal=True, horizontal_spacing=GAP):
        with box(r2):
            heading("GROUND TRUTH  ·  LAST 10 s", "  ·  RX0-TX0, scaled per report by the ground truth peak")
            heat((tm[:, 0, 0] / row_scale)[::-1], r2 - 46, "CSI report (time)")  # newest report on top
        with box(r2):
            heading("RECONSTRUCTED AT THE gNB  ·  LAST 10 s", sub_act, tcol(act))
            legacy_note(r2 - 46) if pm is None else heat((pm[:, 0, 0] / row_scale)[::-1], r2 - 46, "CSI report (time)")
    dpg.add_spacer(height=GAP - 10)
    # row 3: |H| per link and the cosine-similarity CDF of all three scenarios on the same channels
    with dpg.group(horizontal=True, horizontal_spacing=GAP):
        with box(r3):
            text("CSI-RS CHANNEL MAGNITUDE |H|  ·  LATEST REPORT, PER RX-TX LINK", f_kicker, DIM)
            last = np.abs(T[-1])
            plot(r3 - 40, [(f"RX{r}-TX{c}", range(last.shape[-1]), last[r, c] / last.max(), col, 2.0)
                           for (r, c), col in zip(LINKS, (BLUE, ORANGE, V1_LIGHT, GREEN))],
                 SUBC, "|H| (normalized)", (0, last.shape[-1]), (0.0, 1.05))
        with box(r3):
            text("CHANNEL COSINE SIMILARITY", f_kicker, DIM)
            series = []
            for sc in SCENARIOS:
                v = np.sort(cos[f"{sc['key']}_beamcos"].ravel())
                idx = np.linspace(0, len(v) - 1, 400).astype(int)  # 400 points are plenty for a smooth CDF
                series.append((f"  {sc['title']}", v[idx], (idx + 1) / len(v), tcol(sc), 3.0 if sc["key"] == key else 1.8))
            plot(r3 - 40, series, "Cosine similarity", "CDF", (0.5, 1.0), (0.0, 1.02))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--screenshot", default="")
    ap.add_argument("--tab", default="demo", choices=["demo", "channel"])
    ap.add_argument("--active", default="cross", choices=["legacy", "same", "cross"])
    ap.add_argument("--fullscreen", action="store_true")
    args = ap.parse_args()
    dpg.create_context()
    dpg.create_viewport(title="Interoperable AI-Native CSI Feedback · Live OAI prototype", width=1920, height=1080,
                        **viewport_icons())
    dpg.setup_dearpygui()
    dpg.show_viewport()
    if args.fullscreen:
        dpg.toggle_viewport_fullscreen()
    else:
        dpg.maximize_viewport()
    def on_click():
        for key, card in CARDS.items():
            if dpg.does_item_exist(card) and dpg.is_item_hovered(card):
                STATE["active"] = key

    with dpg.handler_registry():
        dpg.add_key_press_handler(dpg.mvKey_F11, callback=lambda: STATE.update(fullscreen=True))
        dpg.add_mouse_click_handler(button=dpg.mvMouseButton_Left, callback=on_click)
        for k, key in ((dpg.mvKey_1, "legacy"), (dpg.mvKey_2, "same"), (dpg.mvKey_3, "cross")):
            dpg.add_key_press_handler(k, callback=lambda s_, a_, u: STATE.update(active=u), user_data=key)

    def current_tab():
        try:
            return "channel" if dpg.get_item_alias(dpg.get_value("tabbar")) == "tab_channel" else "demo"
        except Exception:  # noqa: BLE001
            return args.tab

    def rebuild():
        if dpg.does_item_exist("main"):
            dpg.delete_item("main")
        build(args, dpg.get_viewport_client_width(), dpg.get_viewport_client_height())

    frame, built, size = 0, False, None
    while dpg.is_dearpygui_running():
        frame += 1
        if not built and frame >= 6:
            rebuild()
            built = True
            size = (dpg.get_viewport_client_width(), dpg.get_viewport_client_height())
        elif built and (STATE or size != (dpg.get_viewport_client_width(), dpg.get_viewport_client_height())):
            if STATE.pop("fullscreen", False):
                dpg.toggle_viewport_fullscreen()
            args.tab = STATE.pop("tab", None) or current_tab()
            args.active = STATE.pop("active", args.active)
            rebuild()
            size = (dpg.get_viewport_client_width(), dpg.get_viewport_client_height())
        if args.screenshot and frame == 70:
            dpg.output_frame_buffer(file=args.screenshot)
        if args.screenshot and frame == 100:
            break
        dpg.render_dearpygui_frame()
    dpg.destroy_context()


if __name__ == "__main__":
    main()
