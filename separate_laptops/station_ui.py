"""Shared "retro terminal / mission control" look for the two station GUIs (gNB station, UE station).

Monospace everywhere, square panels, boot-style status tags. One accent colour per station so the two laptops
are told apart at a glance: amber = gNB, phosphor green = UE. Scenario colours are the booth GUI's.
"""

from __future__ import annotations

from contextlib import contextmanager

import dearpygui.dearpygui as dpg
import numpy as np

from common.style import FONTS, LOGO_FILES, LOGOS, PALE, SCENARIOS, TITLE, rgb

BLACK, PANEL, PANEL_2, LINE = "#07090A", "#0C1011", "#121819", "#263033"
TEXT, MUTED, DIM = "#D8E0DC", "#8C9894", "#5C6764"
OK, BUSY, FAIL, IDLE = "#3CFF7A", "#FFD23C", "#FF5A4E", "#4A5452"
STATIONS = {
    "gnb": dict(accent="#FFB000", dim="#7A5A12", name="gNB STATION"),
    "ue": dict(accent="#3CFF7A", dim="#1E6B3B", name="UE STATION"),
}
TAGS = {"ok": ("[ OK ]", OK), "busy": ("[ .. ]", BUSY), "fail": ("[FAIL]", FAIL), "idle": ("[    ]", IDLE),
        "info": ("[INFO]", MUTED)}
SC_COL = {s["key"]: s["text"] for s in SCENARIOS}  # scenario line colours (light variants read well on black)
RES: dict = {}


def char_w(size: int) -> float:
    return size * 0.5  # Ubuntu Mono: every glyph is half an em wide


class StationUI:
    """Widget helpers bound to one station's accent colour."""

    def __init__(self, station: str):
        self.st = STATIONS[station]
        self.accent, self.dim = self.st["accent"], self.st["dim"]
        if not RES:
            with dpg.font_registry():
                for key, name, size in (("body", "UbuntuMono-R.ttf", 18), ("small", "UbuntuMono-R.ttf", 15),
                                        ("bold", "UbuntuMono-B.ttf", 18), ("h2", "UbuntuMono-B.ttf", 22),
                                        ("big", "UbuntuMono-B.ttf", 34), ("station", "UbuntuMono-B.ttf", 34),
                                        ("kicker", "UbuntuMono-B.ttf", 15)):
                    RES[key] = dpg.add_font(str(FONTS / name), size)
            with dpg.texture_registry():
                RES["logos"] = []
                for name, h in LOGO_FILES:
                    w_, h_, _, d = dpg.load_image(str(LOGOS / name))
                    h = int(h * 0.8)
                    RES["logos"].append((dpg.add_static_texture(w_, h_, d), int(w_ * h / h_), h))
        dpg.bind_font(RES["body"])
        with dpg.theme() as t:
            with dpg.theme_component(dpg.mvAll):
                for col, val in ((dpg.mvThemeCol_WindowBg, rgb(BLACK)), (dpg.mvThemeCol_ChildBg, rgb(PANEL)),
                                 (dpg.mvThemeCol_Border, rgb(LINE)), (dpg.mvThemeCol_Text, rgb(TEXT)),
                                 (dpg.mvThemeCol_FrameBg, rgb(PANEL_2)), (dpg.mvThemeCol_Button, rgb(PANEL_2)),
                                 (dpg.mvThemeCol_ButtonHovered, rgb(self.dim)), (dpg.mvThemeCol_ButtonActive, rgb(self.accent)),
                                 (dpg.mvThemeCol_PopupBg, rgb(PANEL))):
                    dpg.add_theme_color(col, val)
                for var, val in ((dpg.mvStyleVar_WindowPadding, (16, 12)), (dpg.mvStyleVar_ChildRounding, 0),
                                 (dpg.mvStyleVar_FrameRounding, 0), (dpg.mvStyleVar_ItemSpacing, (10, 8)),
                                 (dpg.mvStyleVar_FramePadding, (12, 6)), (dpg.mvStyleVar_ChildBorderSize, 1),
                                 (dpg.mvStyleVar_FrameBorderSize, 1), (dpg.mvStyleVar_WindowBorderSize, 0)):
                    dpg.add_theme_style(var, *val) if isinstance(val, tuple) else dpg.add_theme_style(var, val)
                for col, val in ((dpg.mvPlotCol_PlotBg, rgb(BLACK)), (dpg.mvPlotCol_FrameBg, rgb(PANEL)),
                                 (dpg.mvPlotCol_PlotBorder, rgb(LINE)), (dpg.mvPlotCol_AxisGrid, rgb(LINE, 200)),
                                 (dpg.mvPlotCol_AxisText, rgb(MUTED))):
                    dpg.add_theme_color(col, val, category=dpg.mvThemeCat_Plots)
                dpg.add_theme_style(dpg.mvPlotStyleVar_PlotPadding, 6, 6, category=dpg.mvThemeCat_Plots)
                dpg.add_theme_style(dpg.mvPlotStyleVar_LabelPadding, 3, 1, category=dpg.mvThemeCat_Plots)
        dpg.bind_theme(t)

    # ------------------------------------------------------------------ primitives
    def text(self, s, font="body", color=TEXT, wrap=0):
        item = dpg.add_text(s, color=rgb(color), wrap=wrap)
        dpg.bind_item_font(item, RES[font])
        return item

    def child_theme(self, bg=PANEL, border=LINE, pad=(14, 10), spacing=(8, 6)):
        with dpg.theme() as th:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, rgb(bg))
                dpg.add_theme_color(dpg.mvThemeCol_Border, rgb(border))
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, *pad)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, *spacing)
        return th

    @contextmanager
    def panel(self, width, height, title="", border=LINE, bg=PANEL, pad=(14, 10), spacing=(8, 6)):
        with dpg.child_window(width=width, height=height, border=True, no_scrollbar=True) as c:
            dpg.bind_item_theme(c, self.child_theme(bg, border, pad, spacing))
            if title:
                self.text("> " + title, "kicker", self.accent)
            yield c

    def tag(self, state, font="bold"):
        s, col = TAGS[state]
        return self.text(s, font, col)

    def button(self, label, width, active=False, color=None, enabled=True):
        b = dpg.add_button(label=label, width=width, height=34, enabled=enabled)
        col = color or self.accent
        with dpg.theme() as th:
            for state in (True, False):
                with dpg.theme_component(dpg.mvButton, enabled_state=state):
                    dpg.add_theme_color(dpg.mvThemeCol_Button, rgb(col) if active else rgb(PANEL_2))
                    dpg.add_theme_color(dpg.mvThemeCol_Text, rgb(BLACK) if active else rgb(col if enabled else DIM))
                    dpg.add_theme_color(dpg.mvThemeCol_Border, rgb(col) if enabled else rgb(LINE))
        dpg.bind_item_theme(b, th)
        dpg.bind_item_font(b, RES["bold"])
        return b

    def on_off(self, on: bool):
        with dpg.group(horizontal=True, horizontal_spacing=6):
            self.button("ON", 64, active=on)
            self.button("OFF", 64, active=not on)

    # ------------------------------------------------------------------ composite widgets
    def header(self, width, height=70):
        logos_w = sum(w for _, w, _ in RES["logos"]) + 26 * (len(RES["logos"]) - 1) + 2 * 22
        with dpg.group(horizontal=True, horizontal_spacing=0):
            with dpg.child_window(width=width - logos_w, height=height, border=False, no_scrollbar=True) as c:
                dpg.bind_item_theme(c, self.child_theme(BLACK, BLACK, pad=(4, 4), spacing=(0, 2)))
                self.text(f"[ {self.st['name']} ]", "station", self.accent)
                self.text(TITLE, "small", MUTED)
            with dpg.child_window(width=logos_w, height=height, border=False, no_scrollbar=True) as c:
                dpg.bind_item_theme(c, self.child_theme(PALE, PALE, pad=(0, 0), spacing=(0, 0)))
                x = 22
                for tex, w, h in RES["logos"]:
                    img = dpg.add_image(tex, width=w, height=h)
                    dpg.set_item_pos(img, (x, int(height / 2 - h / 2)))
                    x += w + 26

    @staticmethod
    def step_height(n_lines, controls=False, n_sub=0, extra_h=0):
        return 16 + (34 if controls else 22) + 30 * (n_lines + n_sub) + extra_h + 2  # ~30 px per text line

    def step(self, width, height, n, name, state, detail, controls=None, sub=None, extra=None, extra_h=0):
        """One numbered step: status tag, name, optional controls on the right, detail line(s).
        height None = fit the content."""
        detail = [detail] if isinstance(detail, str) else detail
        if height is None:
            height = self.step_height(len(detail), bool(controls), len(sub or []), extra_h)
        border = self.dim if state in ("ok", "busy") else LINE
        with self.panel(width, height, border=border, bg=PANEL, pad=(12, 8), spacing=(8, 4)):
            with dpg.group(horizontal=True):
                self.tag(state)
                self.text(f"{n}  {name}", "bold", TEXT if state != "idle" else MUTED)
                if controls:
                    used = (6 + 3 + len(name)) * char_w(18) + 28
                    dpg.add_spacer(width=max(8, int(width - used - controls[0] - 40)))
                    controls[1]()
            for line in detail:
                self.text("       " + line, "small", MUTED)
            for st_, line in sub or []:
                with dpg.group(horizontal=True, horizontal_spacing=4):
                    self.text("       ", "small")
                    self.tag(st_, "small")
                    self.text(line, "small", TEXT)
            if extra:
                extra()
        return height

    def readout(self, width, label, value, unit="", color=None):
        with self.panel(width, 86, pad=(12, 8), spacing=(4, 0)):
            self.text(label, "kicker", MUTED)
            with dpg.group(horizontal=True, horizontal_spacing=6):
                big = len(value) * char_w(34) <= width - 30 - len(unit) * char_w(15)
                if not big:
                    dpg.add_spacer(height=8)
                self.text(value, "big" if big else "h2", color or self.accent)
                if unit:
                    with dpg.group():
                        dpg.add_spacer(height=12)
                        self.text(unit, "small", MUTED)

    def event_log(self, width, height, lines):
        """lines: (hh:mm:ss, tag state, message). Newest at the bottom, like a terminal: only the tail that fits."""
        fit = max(3, int((height - 66) / 30))
        with self.panel(width, height, "EVENT LOG", spacing=(6, 3)):
            for t, st_, msg in lines[-fit:]:
                with dpg.group(horizontal=True, horizontal_spacing=8):
                    self.text(t, "small", DIM)
                    self.tag(st_, "small")
                    self.text(msg, "small", TEXT)
            self.text("_", "small", self.accent)  # cursor

    def popup(self, x, y, width, title, lines, color):
        """Scenario-change notification, drawn over the plots. lines: (text, tag state or None)."""
        h = 66 + 31 * len(lines)
        with dpg.window(tag="popup", pos=(x, y), width=width, height=h, no_title_bar=True, no_resize=True,
                        no_move=True, no_collapse=True, no_scrollbar=True):
            with dpg.theme() as th:
                with dpg.theme_component(dpg.mvAll):
                    dpg.add_theme_color(dpg.mvThemeCol_WindowBg, rgb(PANEL_2))
                    dpg.add_theme_color(dpg.mvThemeCol_Border, rgb(color))
                    dpg.add_theme_style(dpg.mvStyleVar_WindowBorderSize, 2)
                    dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 18, 12)
                    dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 8, 6)
            dpg.bind_item_theme("popup", th)
            self.text(">> " + title, "h2", color)
            for line, st_ in lines:
                with dpg.group(horizontal=True, horizontal_spacing=0):
                    if st_:
                        self.text("   " + line.ljust(44, "."), "body", TEXT)
                        self.tag(st_, "body")
                    else:
                        self.text("   " + line, "body", MUTED)

    # ------------------------------------------------------------------ plots
    def series_theme(self, kind, color, alpha=255, weight=2.0):
        with dpg.theme() as th:
            with dpg.theme_component(kind):
                if kind == dpg.mvShadeSeries:
                    dpg.add_theme_color(dpg.mvPlotCol_Fill, rgb(color, alpha), category=dpg.mvThemeCat_Plots)
                    dpg.add_theme_color(dpg.mvPlotCol_Line, rgb(color, 0), category=dpg.mvThemeCat_Plots)
                else:
                    dpg.add_theme_color(dpg.mvPlotCol_Line, rgb(color, alpha), category=dpg.mvThemeCat_Plots)
                    dpg.add_theme_style(dpg.mvPlotStyleVar_LineWeight, weight, category=dpg.mvThemeCat_Plots)
        return th

    def _axis_font(self, plot):
        for ax in dpg.get_item_children(plot, 1) or []:
            dpg.bind_item_font(ax, RES["small"])

    def time_plot(self, height, t, ys, seg, ylab, ylim):
        """Time series with one shaded band per scenario segment and a rolling mean in the scenario colour."""
        from common.style import roll
        with dpg.plot(width=-1, height=height, no_menus=True, no_title=True, no_mouse_pos=True) as pl:
            xa = dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)")
            dpg.set_axis_limits(xa, 0, len(t))
            with dpg.plot_axis(dpg.mvYAxis, label=ylab) as ya:
                for i, sc in enumerate(SCENARIOS):
                    xs = [x for x, g in zip(t, seg) if g == i]
                    sh = dpg.add_shade_series(xs, [ylim[1]] * len(xs), y2=[ylim[0]] * len(xs))
                    dpg.bind_item_theme(sh, self.series_theme(dpg.mvShadeSeries, sc["color"], 34))
                ln = dpg.add_line_series(t, ys)
                dpg.bind_item_theme(ln, self.series_theme(dpg.mvLineSeries, MUTED, 90, 1.2))
                for i, sc in enumerate(SCENARIOS):
                    xs = [x for x, g in zip(t, seg) if g == i]
                    av = dpg.add_line_series(xs, roll([y for y, g in zip(ys, seg) if g == i]))
                    dpg.bind_item_theme(av, self.series_theme(dpg.mvLineSeries, SC_COL[sc["key"]], 255, 2.6))
            dpg.set_axis_limits(ya, *ylim)
        self._axis_font(pl)

    def cdf_plot(self, height, data, xlab, xlim, active=None):
        """data: scenario key -> values."""
        with dpg.plot(width=-1, height=height, no_menus=True, no_title=True, no_mouse_pos=True) as pl:
            xa = dpg.add_plot_axis(dpg.mvXAxis, label=xlab)
            dpg.set_axis_limits(xa, *xlim)
            with dpg.plot_axis(dpg.mvYAxis, label="CDF") as ya:
                for sc in SCENARIOS:
                    v = np.sort(np.asarray(data[sc["key"]], dtype=float).ravel())
                    idx = np.linspace(0, len(v) - 1, min(len(v), 400)).astype(int)
                    ser = dpg.add_line_series(v[idx].tolist(), ((idx + 1) / len(v)).tolist())
                    w = 3.0 if active in (None, sc["key"]) else 1.8
                    dpg.bind_item_theme(ser, self.series_theme(dpg.mvLineSeries, SC_COL[sc["key"]], 255, w))
            dpg.set_axis_limits(ya, 0, 1.02)
        self._axis_font(pl)

    def scenario_key_line(self):
        with dpg.group(horizontal=True, horizontal_spacing=6):
            for sc in SCENARIOS:
                with dpg.drawlist(width=18, height=14):
                    dpg.draw_line((0, 8), (18, 8), color=rgb(SC_COL[sc["key"]]), thickness=3)
                self.text(sc["short"] + "  ", "kicker", SC_COL[sc["key"]])

    def heat(self, values, width, height, ylab="", yticks=None, xlab="Subcarrier (occupied, 612)"):
        rows, cols = values.shape
        cmap = dpg.mvPlotColormap_Viridis
        with dpg.group(horizontal=True):
            with dpg.plot(width=width - 88, height=height, no_menus=True, no_title=True, no_mouse_pos=True) as pl:
                dpg.bind_colormap(pl, cmap)
                xa = dpg.add_plot_axis(dpg.mvXAxis, label=xlab)
                with dpg.plot_axis(dpg.mvYAxis, label=ylab) as ya:
                    dpg.add_heat_series(np.clip(values, 0, 1).flatten().tolist(), rows, cols, scale_min=0, scale_max=1,
                                        bounds_min=(0, 0), bounds_max=(cols, rows), format="")
                dpg.set_axis_limits(xa, 0, cols)
                dpg.set_axis_limits(ya, 0, rows)
                if yticks:
                    dpg.set_axis_ticks(ya, yticks)
            self._axis_font(pl)
            dpg.add_colormap_scale(min_scale=0, max_scale=1, colormap=cmap, height=height, width=74, label="")

    def bit_waterfall(self, width, height, codes, cqi, title_col):
        """Latent feedback as it travels on PUCCH: one row per report (newest on top), 48 latent bits + 4 CQI bits."""
        rows = len(codes)
        bits = [[b for c in r for b in ((int(c) >> 1) & 1, int(c) & 1)] for r in codes]  # 2 bits per latent value
        cq = [[(q >> k) & 1 for k in (3, 2, 1, 0)] for q in cqi]
        n = 48 + 4
        cell = min((width - 30) / (n + 2), (height - 26) / rows)
        x0 = (width - (n + 2) * cell) / 2
        with dpg.drawlist(width=width, height=height):
            for i in range(rows):
                y = 4 + i * cell
                for j in range(n):
                    b = bits[i][j] if j < 48 else cq[i][j - 48]
                    x = x0 + (j if j < 48 else j + 2) * cell
                    on = rgb(title_col if j < 48 else MUTED)
                    dpg.draw_rectangle((x + 1, y + 1), (x + cell - 1, y + cell - 1), color=on if b else rgb(LINE),
                                       fill=on if b else rgb(PANEL_2))
            ty = 4 + rows * cell + 4
            dpg.draw_text((x0, ty), "48 LATENT BITS (24 VALUES x 2 BITS)", color=rgb(MUTED), size=14)
            dpg.draw_text((x0 + 50 * cell, ty), "CQI", color=rgb(MUTED), size=14)

    def pmi_strip(self, width, height, i2, title_col):
        """Legacy feedback: the reported beam index (PMI i2, 0..3) per report, newest on the right."""
        n = len(i2)
        cw = (width - 90) / n
        with dpg.drawlist(width=width, height=height):
            for k in range(4):
                y = 6 + (3 - k) * (height - 34) / 4
                dpg.draw_text((4, y), f"i2={k}", color=rgb(MUTED), size=14)
                for j, v in enumerate(i2):
                    if v == k:
                        x = 70 + j * cw
                        dpg.draw_rectangle((x, y + 2), (x + cw - 1, y + (height - 34) / 4 - 4), color=rgb(title_col),
                                           fill=rgb(title_col))
            dpg.draw_text((70, height - 22), "BEAM INDEX (PMI i2) PER REPORT, NEWEST ON THE RIGHT", color=rgb(MUTED), size=14)
