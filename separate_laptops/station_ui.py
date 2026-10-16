"""Window of the gNB station and of the UE station, in the booth GUI's style and built on its code.

Each station shows only what its own laptop knows:
  gNB station: scenario control, bring-up, downlink throughput / MCS / BLER, the channel the gNB reconstructs
               from the feedback it received, and the beam it transmits per 4-RB group.
  UE station:  bring-up, received throughput / DL decode failures / SINR, the channel the UE measures and the
               best beam of that channel (Legacy: also the codebook beam it reports).
Side by side, the UE's best beam and the gNB's transmitted beam use the same axes: how close the feedback gets
the gNB to the ideal beam.

The window draws whatever a source gives it: separate_laptops.sources (recorded data for the mockups, or the
live station controllers). Start / Stop and (gNB) the scenario cards are clickable; 1 / 2 / 3 switch the
scenario; Esc hides the pop-up; F11 fullscreen.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from types import SimpleNamespace

import dearpygui.dearpygui as dpg
import numpy as np

from common.station_data import ChannelView
from common.style import BG, DIM, GREEN, LINE, MUTED, ORANGE, SCENARIO, SCENARIOS, SURFACE, TEXT, rgb, viewport_icons
from same_laptop.app import HEAT_COLS, KEYS, LINKS, METRICS, RED, SUBC, App, tcol
from same_laptop.mockup import DIA_DESIGN_H, diagram_scale, draw_diagram

log = logging.getLogger(__name__)
N_PRB, PRG = 51, 4
UE_METRICS = (
    ("THROUGHPUT", "tput_mbps", "Mbps", "Throughput (Mbps)", "rate"),
    ("DECODE FAILURES", "bler", "Fail rate", "Decode failure rate", "ratio"),
    ("SINR", "sinr", "dB", "SINR (dB)", "db"),
)
STATION = {
    "gnb": dict(title="gNB STATION", sub="Core network · gNB · traffic sender · scenario control",
                start="Start system", tab="  Channel at the gNB  ", color=ORANGE,
                hint="Press 1 · 2 · 3 or click a scenario: the UE station follows.",
                note="Everything on this tab is what the gNB knows: the feedback it received and the beam it "
                     "transmits. The true channel is only measured at the UE."),
    "ue": dict(title="UE STATION", sub="UE · traffic receiver · follows the gNB station",
               start="Start UE", tab="  Channel at the UE  ", color=GREEN,
               hint="Chosen on the gNB station; this laptop follows.",
               note="Everything on this tab is what the UE knows: the channel it measures and what it reports. "
                    "What the gNB reconstructs is shown on the gNB station."),
}


def stairs(values) -> tuple[list[float], list[float]]:
    """Per-group values as a staircase over resource blocks 0..51."""
    xs, ys = [], []
    for g, val in enumerate(values):
        xs += [g * PRG, min((g + 1) * PRG, N_PRB)]
        ys += [float(val), float(val)]
    return xs, ys


def cells(a: np.ndarray) -> np.ndarray:
    """[..., 612] -> [..., 153]: mean of 4 subcarriers per heat-map cell (ImPlot draws every cell)."""
    return a.reshape(*a.shape[:-1], HEAT_COLS, -1).mean(axis=-1)


class StationUI(App):
    def __init__(self, source, args):  # noqa: D107 - no controller of its own: the source owns the station
        self.src, self.args = source, args
        self.station = source.station
        self.st = STATION[self.station]
        self.res, self.t, self.state, self.cards = {}, {}, {}, {}
        self.ota = getattr(source, "ota", True)
        self.core_ok = True
        self._built_key = None
        self._steps_key = None
        self._popup_key = None
        self.metrics = METRICS if self.station == "gnb" else UE_METRICS
        self.clock = getattr(source, "clock", "external")

    # ------------------------------------------------------------------ layout
    def build(self, W: int, H: int) -> None:
        self.t, self.cards = {}, {}
        key, switching = self.src.scenario()
        act = SCENARIO[key]
        PAD, GAP = 18, 12
        inner_w = W - 2 * PAD
        HEADER_H = 78
        body_h = H - HEADER_H - 46 - 2 * PAD - 26
        LEFT_W = max(400, int(inner_w * 0.225))
        RIGHT_W = inner_w - LEFT_W - GAP - 4
        DIA_H = int(body_h * 0.43)
        DIA_H = min(DIA_H, int(44 + DIA_DESIGN_H * 0.88 * diagram_scale(RIGHT_W - 34, DIA_H - 44) + 18))
        PLOT_H = body_h - DIA_H - GAP - 4
        view = SimpleNamespace(switching=switching, target_scenario=key)
        with dpg.window(tag="main", no_scrollbar=True):
            self.build_header(W - 2 * PAD, HEADER_H)
            with dpg.tab_bar(tag="tabbar", callback=lambda s_, a_: self.state.update(tab=a_)):
                with dpg.tab(label="  Live demo  ", tag="tab_demo"):
                    with dpg.group(horizontal=True):
                        with dpg.child_window(width=LEFT_W, height=body_h, border=False, no_scrollbar=True) as left:
                            dpg.bind_item_theme(left, self.child_theme(rgb(BG), rgb(BG)))
                            self.station_badge()
                            dpg.add_spacer(height=4)
                            self.text("CSI FEEDBACK SCENARIOS", "kicker", DIM)
                            for sc in SCENARIOS:
                                self.build_card(sc, sc["key"] == key, view, LEFT_W, 88)
                            self.text(self.st["hint"], "small", DIM, wrap=LEFT_W - 10)
                            dpg.add_spacer(height=2)
                            self.build_station_system(LEFT_W)
                        with dpg.child_window(width=RIGHT_W, height=body_h, border=False, no_scrollbar=True) as right:
                            dpg.bind_item_theme(right, self.child_theme(rgb(BG), rgb(BG)))
                            with dpg.child_window(height=DIA_H, border=True, no_scrollbar=True) as dia:
                                dpg.bind_item_theme(dia, self.child_theme(rgb(SURFACE), rgb(LINE), pad=(16, 12), spacing=(8, 4)))
                                self.text("SYSTEM DIAGRAM  ·  " + act["title"].upper(), "kicker", tcol(act))
                                draw_diagram(act, RIGHT_W - 34, DIA_H - 58, None, self.res["draw"], focus=self.station,
                                             bits=True)
                            self.build_plots(RIGHT_W, PLOT_H, GAP, self.metrics)
                with dpg.tab(label=self.st["tab"], tag="tab_channel"):
                    self.build_channel(act, inner_w, body_h, GAP)
        if self.state.get("tab_now") == "channel":
            dpg.set_value("tabbar", "tab_channel")
        dpg.set_primary_window("main", True)

    def station_badge(self):
        with dpg.child_window(height=80, border=True, no_scrollbar=True) as badge:
            dpg.bind_item_theme(badge, self.child_theme(rgb(SURFACE), rgb(self.st["color"]), pad=(14, 8), spacing=(6, 2)))
            self.text(self.st["title"], "h2", self.st["color"])
            self.text(self.st["sub"], "small", MUTED)

    def build_station_system(self, LEFT_W):
        with dpg.child_window(height=-1, border=True, no_scrollbar=True) as sysc:
            dpg.bind_item_theme(sysc, self.child_theme(rgb(SURFACE), rgb(LINE), pad=(16, 12), spacing=(10, 8)))
            self.text("SYSTEM", "kicker", DIM)
            bw = (LEFT_W - 60) // 2
            with dpg.group(horizontal=True):
                b1 = dpg.add_button(label=self.st["start"], width=bw, height=44, tag="btn_start",
                                    callback=lambda: self.state.update(start=True))
                b2 = dpg.add_button(label="Stop", width=bw, height=44, tag="btn_stop",
                                    callback=lambda: self.state.update(stop=True))
            for b in (b1, b2):
                dpg.bind_item_font(b, self.res["med"])
            self.build_clock_switch(LEFT_W)  # this laptop's USRP: internal or external 10 MHz / PPS
            with dpg.theme() as t_run:
                with dpg.theme_component(dpg.mvButton, enabled_state=False):
                    dpg.add_theme_color(dpg.mvThemeCol_Button, rgb("#26332B"))
                    dpg.add_theme_color(dpg.mvThemeCol_Text, rgb(GREEN))
            with dpg.theme() as t_off:
                with dpg.theme_component(dpg.mvButton, enabled_state=False):
                    dpg.add_theme_color(dpg.mvThemeCol_Button, rgb("#242322"))
                    dpg.add_theme_color(dpg.mvThemeCol_Text, rgb("#5E5C57"))
            self.res["t_running"], self.res["t_off"] = t_run, t_off
            self.t["stepper_w"] = LEFT_W - 50
            dpg.add_drawlist(width=LEFT_W - 50, height=60, tag="stepper")
            self.text("", "small", MUTED, wrap=LEFT_W - 40, tag="status_line")
            self.text("", "small", DIM, wrap=LEFT_W - 40, tag="event_line")
            dpg.add_drawlist(width=LEFT_W - 40, height=48, tag="dots")

    def build_popup(self, W, popup):
        if dpg.does_item_exist("popup"):
            dpg.delete_item("popup")
        if popup is None:
            return
        title, lines, key = popup
        act = SCENARIO[key]
        w, h = 600, 84 + 42 * len(lines)
        with dpg.window(tag="popup", pos=((W - w) // 2 + W // 9, 470), width=w, height=h, no_title_bar=True,
                        no_resize=True, no_move=True, no_collapse=True, no_scrollbar=True):
            with dpg.theme() as th:
                with dpg.theme_component(dpg.mvAll):
                    dpg.add_theme_color(dpg.mvThemeCol_WindowBg, rgb("#262523"))
                    dpg.add_theme_color(dpg.mvThemeCol_Border, rgb(act["color"]))
                    dpg.add_theme_style(dpg.mvStyleVar_WindowBorderSize, 2)
                    dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 14)
                    dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 20, 14)
                    dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 10, 8)
            dpg.bind_item_theme("popup", th)
            self.text(title, "h2", tcol(act))
            for line, state in lines:  # state: ok | busy | fail | None (plain note)
                with dpg.group(horizontal=True):
                    if state:
                        col = {"ok": GREEN, "busy": ORANGE, "fail": RED}[state]
                        with dpg.drawlist(width=18, height=20):
                            dpg.draw_circle((8, 11), 6, color=rgb(col), fill=rgb(col) if state != "busy" else rgb("#262523"),
                                            thickness=2)
                    self.text(line, "body", TEXT if state in ("ok", "fail") else MUTED)

    # ------------------------------------------------------------------ channel tab
    @contextmanager
    def box(self, width, height):
        with dpg.child_window(width=width, height=height, border=True, no_scrollbar=True) as c:
            dpg.bind_item_theme(c, self.child_theme(rgb(SURFACE), rgb(LINE), pad=(14, 10), spacing=(6, 4)))
            yield c

    def heading(self, title, sub="", sub_col=DIM):
        with dpg.group(horizontal=True):
            self.text(title, "kicker", DIM)
            if sub:
                self.text(sub, "kicker", sub_col)

    def heat(self, tag, rows, cols, width, height, ylab="", yticks=None, cmap=dpg.mvPlotColormap_Viridis,
             lo=0.0, hi=1.0, xlab=SUBC, span=612):
        with dpg.group(horizontal=True):
            with dpg.plot(width=width - 104, height=height, no_menus=True, no_title=True, no_mouse_pos=True) as pl:
                dpg.bind_colormap(pl, cmap)
                xa = dpg.add_plot_axis(dpg.mvXAxis, label=xlab)
                with dpg.plot_axis(dpg.mvYAxis, label=ylab) as ya:
                    dpg.add_heat_series([lo] * (rows * cols), rows, cols, scale_min=lo, scale_max=hi,
                                        bounds_min=(0, 0), bounds_max=(span, rows), format="", tag=tag)
                dpg.set_axis_limits(xa, 0, span)
                dpg.set_axis_limits(ya, 0, rows)
                self.t[tag + "_y"] = ya
                if yticks:
                    dpg.set_axis_ticks(ya, yticks)
            self.axis_font(pl)
            dpg.add_colormap_scale(min_scale=lo, max_scale=hi, colormap=cmap, height=height, width=72, label="")

    def note(self, height, first, second=""):
        dpg.add_spacer(height=max(0, height // 2 - 50))
        self.text(first, "body", MUTED)
        if second:
            self.text(second, "body", DIM)

    def build_channel(self, act, inner_w, body_h, GAP):
        key, gnb = act["key"], self.station == "gnb"
        with dpg.group(horizontal=True):
            self.text(self.st["note"], "small", DIM)
            self.text("", "small", MUTED, tag="ch_status")
        body_h -= 34
        half = (inner_w - GAP) // 2
        r1 = int(body_h * 0.45)
        r2 = body_h - r1 - GAP - 6
        ticks = tuple((f"RX{r}-TX{c}", 3 - k + 0.5) for k, (r, c) in enumerate(LINKS))
        sub_act = f"  ·  {act['title']}"
        no_channel = gnb and key == "legacy"
        with dpg.group(horizontal=True, horizontal_spacing=GAP):
            with self.box(half, r1):
                if gnb:
                    self.heading("CHANNEL RECONSTRUCTED BY THE gNB  ·  RIGHT NOW", sub_act, tcol(act))
                else:
                    self.heading("CHANNEL MEASURED BY THE UE  ·  RIGHT NOW", "  ·  |H| of all RX-TX links (CSI-RS)")
                if no_channel:
                    self.note(r1 - 46, "Legacy feedback is a beam index (PMI): the gNB never sees the channel.",
                              "It looks the beam up in the 3GPP codebook (one beam for the whole band).")
                else:
                    self.heat("heat_now", 4, HEAT_COLS, half, r1 - 46, yticks=ticks)
            with self.box(half, r1):
                if gnb:
                    self.heading("BEAM THE gNB TRANSMITS  ·  ACROSS THE BAND", sub_act, tcol(act))
                else:
                    self.heading("BEST BEAM OF THE MEASURED CHANNEL  ·  ACROSS THE BAND")
                with dpg.plot(width=-1, height=r1 - 40, no_menus=True, no_title=True, no_mouse_pos=True) as pl:
                    dpg.add_plot_legend(location=dpg.mvPlot_Location_NorthEast)
                    xa = dpg.add_plot_axis(dpg.mvXAxis, label="Resource block (4-RB precoding groups)")
                    dpg.set_axis_limits(xa, 0, N_PRB)
                    with dpg.plot_axis(dpg.mvYAxis, label="Beam phase (deg)") as ya:
                        self.t["beam_y"] = ya
                        if gnb:
                            lab = ("  Codebook beam (one for the whole band)" if key == "legacy"
                                   else "  One beam per 4 resource blocks")
                            s = dpg.add_line_series([], [], label=lab, tag="beam_main")
                            dpg.bind_item_theme(s, self.series_theme(dpg.mvLineSeries, tcol(act), 255, 3.0))
                        else:
                            s = dpg.add_line_series([], [], label="  Best beam (from the measured channel)", tag="beam_main")
                            dpg.bind_item_theme(s, self.series_theme(dpg.mvLineSeries, "#E8E6DF", 255, 3.0))
                            if key == "legacy":
                                s = dpg.add_line_series([], [], label="  Beam index the UE reports (PMI)", tag="beam_pmi")
                                dpg.bind_item_theme(s, self.series_theme(dpg.mvLineSeries, tcol(act), 255, 3.0))
                    dpg.set_axis_limits(ya, -185, 185)
                    dpg.set_axis_ticks(ya, tuple((f"{v}", v) for v in (-180, -90, 0, 90, 180)))
                self.axis_font(pl)
        dpg.add_spacer(height=GAP - 10)
        with dpg.group(horizontal=True, horizontal_spacing=GAP):
            with self.box(half, r2):
                if gnb:
                    self.heading("RECONSTRUCTED BY THE gNB  ·  RECENT REPORTS", "  ·  RX0-TX0, newest on top, scaled per report")
                else:
                    self.heading("MEASURED CHANNEL  ·  RECENT REPORTS", "  ·  RX0-TX0, newest on top, scaled per report")
                if no_channel:
                    self.note(r2 - 46, "No channel reconstruction in Legacy.")
                else:
                    self.heat("heat_hist", 1, HEAT_COLS, half, r2 - 46, "CSI report")
            with self.box(half, r2):
                if gnb:
                    self.heading("TRANSMITTED BEAM  ·  RECENT REPORTS", "  ·  phase per 4-RB group, newest on top")
                else:
                    self.heading("BEST BEAM  ·  RECENT REPORTS", "  ·  phase per 4-RB group, newest on top")
                self.heat("heat_beam", 1, 13, half, r2 - 46, "CSI report", cmap=dpg.mvPlotColormap_Twilight,
                          lo=-180.0, hi=180.0, xlab="Resource block", span=N_PRB)

    # ------------------------------------------------------------------ updates
    def refresh_system(self):
        steps = self.src.steps()
        if tuple(steps) != self._steps_key:
            self._steps_key = tuple(steps)
            dpg.delete_item("stepper", children_only=True)
            sw = self.t["stepper_w"]
            step = (sw - 50) / (len(steps) - 1)
            col = {"ok": GREEN, "busy": ORANGE, "idle": "#55534E", "fail": RED}
            for k, (name, st) in enumerate(steps):
                x = 25 + k * step
                if k:
                    on = steps[k - 1][1] == "ok" and st == "ok"
                    dpg.draw_line((x - step + 13, 16), (x - 13, 16), color=rgb(GREEN if on else "#55534E"), thickness=2,
                                  parent="stepper")
                dpg.draw_circle((x, 16), 11, color=rgb(col[st]), fill=rgb(col[st]) if st in ("ok", "fail") else rgb(SURFACE),
                                thickness=2, parent="stepper")
                dpg.draw_text((x - len(name) * 3.8, 34), name, color=rgb(MUTED if st != "idle" else DIM), size=14,
                              parent="stepper")
        running = self.src.running()
        self.refresh_clock_switch(running)
        dpg.configure_item("btn_start", label="•  Running" if running else self.st["start"], enabled=not running)
        dpg.bind_item_theme("btn_start", self.res["t_running"] if running else self.res["t_off"])
        dpg.configure_item("btn_stop", enabled=running)
        dpg.bind_item_theme("btn_stop", self.res["t_off"])
        dpg.set_value("status_line", self.src.status_line())
        ev = self.src.last_event()
        dpg.set_value("event_line", f"{ev[0]}  {ev[2]}" if ev else "")
        dpg.configure_item("event_line", color=rgb(RED if ev and ev[1] == "fail" else DIM))
        dpg.delete_item("dots", children_only=True)
        x, y = 6, 12
        for name, ok in self.src.dots():
            c = rgb(GREEN if ok else "#55534E")
            width = 20 + len(name) * 7
            if x + width > self.t["stepper_w"] + 40:
                x, y = 6, y + 22
            dpg.draw_circle((x, y), 5, color=c, fill=c, parent="dots")
            dpg.draw_text((x + 9, y - 8), name, color=rgb(MUTED), size=14, parent="dots")
            x += width

    def refresh_channel(self, v: ChannelView):
        if v.error:
            dpg.set_value("ch_status", "   " + v.error)
            return
        if v.n_reports == 0 or v.beam_deg is None:
            dpg.set_value("ch_status", "   Waiting for channel reports..." if self.src.running() else
                          "   Start to see the channel.")
            return
        dpg.set_value("ch_status", f"   {v.n_reports} reports")
        if v.cfr is not None and dpg.does_item_exist("heat_now"):
            mag = np.abs(v.cfr)
            scale = float(mag.max()) or 1.0
            dpg.set_value("heat_now", [np.clip(cells(np.stack([mag[r, c] for r, c in LINKS]) / scale), 0, 1).ravel().tolist()])
        # phase is circular: unwrap across the band so a beam near +-180 deg does not jump from edge to edge,
        # then centre the axis on it (the heat maps use a cyclic colour map instead)
        beam = np.degrees(np.unwrap(np.radians(v.beam_deg)))
        beam -= 360.0 * np.round(beam.mean() / 360.0)
        mid = float(beam.mean())
        dpg.set_value("beam_main", list(stairs(beam)))
        if dpg.does_item_exist("beam_pmi"):
            pmi = None if v.pmi_deg is None else v.pmi_deg + 360.0 * np.round((mid - v.pmi_deg) / 360.0)
            dpg.set_value("beam_pmi", [[0, N_PRB], [pmi, pmi]] if pmi is not None else [[], []])
        lo = 90 * np.floor((min(beam.min(), mid - 120) - 20) / 90)
        hi = 90 * np.ceil((max(beam.max(), mid + 120) + 20) / 90)
        dpg.set_axis_limits(self.t["beam_y"], lo - 5, hi + 5)
        dpg.set_axis_ticks(self.t["beam_y"], tuple((f"{int(t - 360 * round(t / 360))}", float(t))
                                                         for t in np.arange(lo, hi + 1, 90)))
        rows = v.hist_mag.shape[0]
        if not rows:
            return

        def hist(tag, values, cols, span):
            dpg.configure_item(tag, rows=rows, cols=cols, bounds_max=(span, rows))
            dpg.set_axis_limits(self.t[tag + "_y"], 0, rows)
            dpg.set_value(tag, [values[::-1].ravel().tolist()])

        if dpg.does_item_exist("heat_hist"):
            m = np.nan_to_num(v.hist_mag, nan=0.0)  # legacy rows at the gNB: no channel (dark)
            scale = m.max(axis=1, keepdims=True)
            scale[scale == 0] = 1.0
            hist("heat_hist", np.clip(cells(m / scale), 0, 1), HEAT_COLS, 612)
        hist("heat_beam", np.clip(v.hist_beam, -180, 180), 13, N_PRB)

    def apply_clock(self, value: str):
        self.clock = value
        self.src.set_clock(value)

    # ------------------------------------------------------------------ main loop
    def on_click(self):
        if not self.src.can_choose:
            return  # the UE station does not choose the scenario
        for key, card in self.cards.items():
            if dpg.does_item_exist(card) and dpg.is_item_hovered(card):
                self.state["scenario"] = key

    def run(self):
        dpg.create_context()
        dpg.create_viewport(title=f"{self.st['title'].title()} · Interoperable AI-Native CSI Feedback", width=1920, height=1080,
                        **viewport_icons())
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.toggle_viewport_fullscreen() if self.args.fullscreen else dpg.maximize_viewport()
        self.resources()
        with dpg.handler_registry():
            dpg.add_key_press_handler(dpg.mvKey_F11, callback=lambda: self.state.update(fullscreen=True))
            dpg.add_key_press_handler(dpg.mvKey_Escape, callback=lambda: self.state.update(hide_popup=True))
            dpg.add_mouse_click_handler(button=dpg.mvMouseButton_Left, callback=self.on_click)
            for k, key in ((dpg.mvKey_1, "legacy"), (dpg.mvKey_2, "same"), (dpg.mvKey_3, "cross")):
                dpg.add_key_press_handler(k, callback=lambda s_, a_, u: self.state.update(scenario=u), user_data=key)
        script = sorted(self._parse_script(self.args.script))
        t0, frame, last = time.monotonic(), 0, 0.0
        try:
            while dpg.is_dearpygui_running():
                now = time.monotonic()
                frame += 1
                while script and script[0][0] <= now - t0:
                    self._script_action(script.pop(0)[1])
                if self.state.pop("fullscreen", False):
                    dpg.toggle_viewport_fullscreen()
                if "scenario" in self.state:
                    self.src.request(self.state.pop("scenario"))
                if "clock" in self.state:
                    self.apply_clock(self.state.pop("clock"))
                if self.state.pop("start", False):
                    self.src.start()
                if self.state.pop("stop", False):
                    self.src.stop()
                if self.state.pop("hide_popup", False):
                    self.src.hide_popup()
                if "tab" in self.state:
                    tab = self.state.pop("tab")
                    self.state["tab_now"] = "channel" if dpg.get_item_alias(tab) == "tab_channel" else "demo"
                cur = (dpg.get_viewport_client_width(), dpg.get_viewport_client_height())
                key = (cur, *self.src.scenario())
                if frame >= 6 and key != self._built_key:
                    for tag in ("main", "popup"):
                        if dpg.does_item_exist(tag):
                            dpg.delete_item(tag)
                    self._steps_key = self._popup_key = None
                    self.build(*cur)
                    self._built_key, last = key, 0.0
                if self._built_key and now - last >= 0.5:
                    last = now
                    self.src.tick()
                    popup = self.src.popup()
                    if popup != self._popup_key:
                        self._popup_key = popup
                        self.build_popup(cur[0], popup)
                    self.refresh_system()
                    self.refresh_plots(self.src.plot_view())
                    if self.state.get("tab_now") == "channel":
                        self.refresh_channel(self.src.channel())
                dpg.render_dearpygui_frame()
                fps = 20 if self.state.get("tab_now") == "channel" else 30
                time.sleep(max(0.0, 1 / fps - (time.monotonic() - now)))
        finally:
            self.src.shutdown()
            dpg.destroy_context()


# ---------------------------------------------------------------------------------------------- entry points
def _args(desc, extra):
    import argparse

    ap = argparse.ArgumentParser(description=desc)
    ap.add_argument("--fullscreen", action="store_true")
    ap.add_argument("--script", default="", help="testing: <seconds>:<action>,... actions start | stop | legacy | "
                    "same | cross | demo | channel | shot=<png> | quit")
    extra(ap)
    return ap.parse_args()


def mockup_main(station: str) -> None:
    from separate_laptops.sources import MockSource

    def extra(ap):
        ap.add_argument("--active", default="legacy", choices=KEYS)
        ap.add_argument("--no-history", dest="history", action="store_false",
                        help="start the plots empty instead of with 3 recorded minutes")

    args = _args(f"{STATION[station]['title'].title()} mockup (recorded OTA data)", extra)
    StationUI(MockSource(station, args.active, args.history), args).run()


def live_main(station: str) -> None:
    """The real station: refuses root (the softmodems get root through sudo) and needs a desktop session."""
    import os
    import sys

    from common.oai.config import load_config
    from common.style import REPO

    def extra(ap):
        ap.add_argument("--profile", default=str(REPO / f"configs/station_{station}_usrp.yaml"))

    args = _args(f"{STATION[station]['title'].title()} (live)", extra)
    if os.geteuid() == 0:
        sys.exit("Run the station as your normal user, not root: it starts the softmodems through sudo.")
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        sys.exit("No desktop session (DISPLAY is not set): start the station from the laptop's desktop.")
    log_dir = REPO / "data/runs/gui_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / time.strftime(f"{station}_station_%Y%m%d_%H%M%S.log")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(log_file)])
    log.info("profile %s, log %s", args.profile, log_file)
    cfg = load_config(args.profile)
    if station == "gnb":
        from separate_laptops.sources import GnbSource as Source
    else:
        from separate_laptops.sources import UeSource as Source
    StationUI(Source(cfg), args).run()
