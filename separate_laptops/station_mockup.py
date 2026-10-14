"""gNB station and UE station mockups (separate laptops), in the booth GUI's style and built on its code.

    .venv/bin/python -m separate_laptops.gnb_station.mockup
    .venv/bin/python -m separate_laptops.ue_station.mockup

Each station shows only what its own laptop can know:
  gNB station: scenario control, gNB bring-up, throughput / MCS / BLER, the channel the gNB reconstructs from the
               feedback it received, and the beam it transmits across the band.
  UE station:  UE bring-up, received throughput / decode failures / SINR, the channel the UE measures and the best
               beam of that measured channel. The scenario follows requests from the gNB station (over Wi-Fi).
Side by side, the UE's "best beam" and the gNB's "transmitted beam" plots show how well the feedback works.

Mockup: replays recorded OTA data (data/mockups), the bring-up is simulated, nothing talks to OAI. Start / Stop
and (gNB) the scenario cards are clickable; keys 1 / 2 / 3 switch the scenario (on the UE station they simulate
a request arriving from the gNB station); Esc hides a pop-up; F11 fullscreen.
"""

from __future__ import annotations

import argparse
import csv
import math
import time
from contextlib import contextmanager
from types import SimpleNamespace

import dearpygui.dearpygui as dpg
import numpy as np

from common.style import BG, DIM, GREEN, LINE, MOCKUP_DATA, MUTED, ORANGE, SCENARIO, SCENARIOS, SURFACE, TEXT, rgb
from same_laptop.app import HEAT_COLS, KEYS, LINKS, METRICS, RED, SUBC, App, tcol
from same_laptop.mockup import DIA_DESIGN_H, diagram_scale, draw_diagram

PRB_SC, N_PRB, PRG = 12, 51, 4
UE_METRICS = (
    ("THROUGHPUT", "tput_mbps", "Mbps", "Throughput (Mbps)", "rate"),
    ("DECODE FAILURES", "bler", "Fail rate", "Decode failure rate", "ratio"),
    ("SINR", "sinr", "dB", "SINR (dB)", "db"),
)
STATION = {
    "gnb": dict(title="gNB STATION", sub="Core network · gNB · traffic sender · scenario control",
                steps=("Core", "gNB", "USRP", "UE", "Wi-Fi", "Traffic"),
                dots=("Core", "gNB", "USRP", "UE station (Wi-Fi)", "Telnet"),
                start="Start gNB", tab="  Channel at the gNB  "),
    "ue": dict(title="UE STATION", sub="UE · traffic receiver · follows the gNB station",
               steps=("USRP", "Sync", "Attach", "IP", "Receiver", "Wi-Fi"),
               dots=("UE", "USRP", "Receiver", "gNB station (Wi-Fi)", "Telnet"),
               start="Start UE", tab="  Channel at the UE  "),
}
GNB_DECODER = {"legacy": "codebook lookup (beam index only)", "same": "Vendor 1 AI decoder",
               "cross": "Vendor 2 AI decoder + translation"}


def prg_beam_phase(cfr: np.ndarray) -> np.ndarray:
    """Best rank-1 beam per 4-PRB group, as the phase of TX1 relative to TX0 (deg): cfr [..., rx, tx, 612]."""
    per_sc = np.einsum("...rtk,...rsk->...kts", cfr.conj(), cfr)
    groups = [per_sc[..., g * PRG * PRB_SC:min((g + 1) * PRG, N_PRB) * PRB_SC, :, :].sum(axis=-3)
              for g in range(math.ceil(N_PRB / PRG))]
    v = np.linalg.eigh(np.stack(groups, axis=-3))[1][..., -1]  # [..., 13, tx]
    return np.degrees(np.angle(v[..., 1] * np.conj(v[..., 0])))


def codebook_pmi(cfr: np.ndarray) -> np.ndarray:
    """The UE's wideband PMI (2-port Type I codebook, w = [1, j^i2]): the one beam with the most power: [..., rx, tx, 612]."""
    gains = [np.abs(cfr[..., :, 0, :] + (1j ** i2) * cfr[..., :, 1, :]) ** 2 for i2 in range(4)]
    return np.argmax(np.stack([g.sum(axis=(-2, -1)) for g in gains], axis=-1), axis=-1)


def wrap_deg(x):
    return (np.asarray(x, dtype=float) + 180) % 360 - 180


def stairs(values: np.ndarray) -> tuple[list[float], list[float]]:
    """Per-PRG values as a staircase over resource blocks 0..51."""
    xs, ys = [], []
    for g, val in enumerate(values):
        x0, x1 = g * PRG, min((g + 1) * PRG, N_PRB)
        xs += [x0, x1]
        ys += [float(val), float(val)]
    return xs, ys


def load_data() -> dict:
    """Recorded OTA runs 01 / 02 / 06 (per-second metrics with traffic, UE reports) and 120 CSI reports."""
    runs = {s["key"]: [r for r in csv.DictReader(open(MOCKUP_DATA / "runs" / s["run"] / "metrics.csv"))
                       if float(r["dl_mbps"]) > 0.5] for s in SCENARIOS}
    ue = {s["key"]: list(csv.DictReader(open(MOCKUP_DATA / "runs" / s["run"] / "ue_reports.csv"))) for s in SCENARIOS}
    return dict(runs=runs, ue=ue, ch=dict(np.load(MOCKUP_DATA / "mockup_channels.npz")))


class StationMockup(App):
    def __init__(self, station: str, args):  # noqa: D107 - no OAI, no controller: simulated data only
        self.station, self.st, self.args = station, STATION[station], args
        self.res, self.t, self.state, self.cards = {}, {}, {}, {}
        self.ota, self.core_ok = True, True
        self.scenario = args.active
        self.data = load_data()
        self.running, self.boot_t = False, None
        self.popup_until, self.popup_lines = 0.0, None
        self.events: list[tuple[str, str]] = []
        self._built_key = None
        self.metrics = METRICS if station == "gnb" else UE_METRICS
        ch = self.data["ch"]
        self.true_cfr = ch["true_cfr"]
        self.recon = {"same": ch["same_cfr"], "cross": ch["cross_cfr"]}
        self.i2 = codebook_pmi(self.true_cfr)
        self.live: list = []  # (t, scenario, row) once traffic runs, one per second
        self.cursor = {k: 0 for k in KEYS}
        self.event("info", f"{self.st['title'].title()} ready (mockup: recorded OTA data)")

    # ------------------------------------------------------------------ simulated state
    def event(self, level: str, text: str) -> None:
        self.events.append((time.strftime("%H:%M:%S"), text))

    def steps(self) -> list[tuple[str, str]]:
        if not self.running:
            return [(s, "idle") for s in self.st["steps"]]
        done = int((time.monotonic() - self.boot_t) / 1.2)
        return [(s, "ok" if i < done else ("busy" if i == done else "idle")) for i, s in enumerate(self.st["steps"])]

    def traffic(self) -> bool:
        return self.running and all(st == "ok" for _, st in self.steps())

    def sample(self, t: float, key: str):
        """The next recorded second of the scenario on air (runs 01 / 02 / 06), as a DemoView-like sample."""
        rows, ue = self.data["runs"][key], self.data["ue"][key]
        j = self.cursor[key] = self.cursor[key] + 1
        r = rows[j % len(rows)]
        return SimpleNamespace(t=t, scenario=key, tput_mbps=float(r["dl_mbps"]), mcs=float(r["mcs"]),
                               bler=float(r["bler"]), sinr=float(ue[(j * 12) % len(ue)]["sinr_dB"]))

    def tick(self, now: float) -> None:
        """One sample per second while traffic runs; with history, the first one brings the recorded 3 minutes
        (Legacy, Same vendor, Cross vendor, 60 s each) so the plots are full right away."""
        if not self.traffic():
            return
        if not self.live:
            if self.args.history:
                self.live = [self.sample(now - 180 + i, KEYS[i // 60]) for i in range(180)]
            self.live.append(self.sample(now, self.scenario))
        elif now - self.live[-1].t >= 1.0:
            self.live.append(self.sample(self.live[-1].t + 1.0, self.scenario))

    def switch(self, key: str) -> None:
        if key == self.scenario:
            return
        prev, self.scenario = SCENARIO[self.scenario], SCENARIO[key]
        title = self.scenario["title"]
        if self.station == "gnb":
            lines = [("Request sent to the UE station (Wi-Fi)", "ok"), ("UE station armed for frame 787", "ok"),
                     ("Switched at frame 787", "ok"), (f"gNB: {GNB_DECODER[key]}", None)]
            if "legacy" not in (key, prev["key"]):
                lines[1] = ("UE encoder unchanged (Vendor 1)", "ok")
            self.popup_lines = (f"SWITCHING TO {title.upper()}", lines)
        else:
            enc = "AI encoder (Vendor 1)" if key != "legacy" else "codebook search (PMI)"
            lines = [("Request from the gNB station (Wi-Fi)", "ok"), ("Armed for frame 787", "ok"),
                     ("Switched at frame 787", "ok"), (f"UE feedback: {enc}", None)]
            if "legacy" not in (key, prev["key"]):
                lines[1:3] = [("Encoder unchanged: nothing to switch on the UE", "ok")]
            self.popup_lines = (f"gNB STATION: {title.upper()}", lines)
        self.popup_until = time.monotonic() + 5.0
        self.event("ok", f"{prev['short']} -> {self.scenario['short']}")
        self.scenario = key
        self._built_key = None

    # ------------------------------------------------------------------ layout
    def build(self, W: int, H: int) -> None:  # noqa: C901 - one screen
        self.t, self.cards = {}, {}
        act = SCENARIO[self.scenario]
        PAD, GAP = 18, 12
        inner_w = W - 2 * PAD
        HEADER_H = 78
        body_h = H - HEADER_H - 46 - 2 * PAD - 26
        LEFT_W = max(400, int(inner_w * 0.225))
        RIGHT_W = inner_w - LEFT_W - GAP - 4
        DIA_H = int(body_h * 0.43)
        DIA_H = min(DIA_H, int(44 + DIA_DESIGN_H * 0.88 * diagram_scale(RIGHT_W - 34, DIA_H - 44) + 18))
        PLOT_H = body_h - DIA_H - GAP - 4
        view = SimpleNamespace(switching=False, target_scenario=self.scenario)
        with dpg.window(tag="main", no_scrollbar=True):
            self.build_header(W - 2 * PAD, HEADER_H)
            with dpg.tab_bar(tag="tabbar", callback=lambda s_, a_: self.state.update(tab=a_)):
                with dpg.tab(label="  Live demo  ", tag="tab_demo"):
                    with dpg.group(horizontal=True):
                        with dpg.child_window(width=LEFT_W, height=body_h, border=False, no_scrollbar=True) as left:
                            dpg.bind_item_theme(left, self.child_theme(rgb(BG), rgb(BG)))
                            self.station_badge(LEFT_W)
                            dpg.add_spacer(height=4)
                            self.text("CSI FEEDBACK SCENARIOS", "kicker", DIM)
                            for sc in SCENARIOS:
                                self.build_card(sc, sc["key"] == act["key"], view, LEFT_W, 92)
                                dpg.add_spacer(height=1)
                            hint = ("Press 1 · 2 · 3 or click a scenario: the UE station follows." if self.station == "gnb"
                                    else "Chosen on the gNB station; this laptop follows.")
                            self.text(hint, "small", DIM, wrap=LEFT_W - 10)
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
                    (self.build_gnb_channel if self.station == "gnb" else self.build_ue_channel)(act, inner_w, body_h, GAP)
        if self.state.get("tab_now") == "channel":
            dpg.set_value("tabbar", "tab_channel")
        dpg.set_primary_window("main", True)
        self.build_popup(W)

    def station_badge(self, LEFT_W):
        with dpg.child_window(height=80, border=True, no_scrollbar=True) as badge:
            col = ORANGE if self.station == "gnb" else GREEN
            dpg.bind_item_theme(badge, self.child_theme(rgb(SURFACE), rgb(col), pad=(14, 8), spacing=(6, 2)))
            self.text(self.st["title"], "h2", col)
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

    def build_popup(self, W):
        if not self.popup_lines or time.monotonic() > self.popup_until:
            return
        title, lines = self.popup_lines
        act = SCENARIO[self.scenario]
        w, h = 600, 84 + 42 * len(lines)
        with dpg.window(tag="popup", pos=((W - w) // 2 + W // 9, 470), width=w, height=h, no_title_bar=True, no_resize=True,
                        no_move=True, no_collapse=True, no_scrollbar=True):
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
            for line, ok in lines:
                with dpg.group(horizontal=True):
                    if ok:
                        with dpg.drawlist(width=18, height=20):
                            dpg.draw_circle((8, 11), 6, color=rgb(GREEN), fill=rgb(GREEN))
                    self.text(line, "body", TEXT if ok else MUTED)

    # ------------------------------------------------------------------ channel tabs
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

    def heat(self, values, width, height, ylab="", yticks=None, cmap=dpg.mvPlotColormap_Viridis, lo=0.0, hi=1.0,
             xlab=SUBC, cols_span=612):
        rows, cols = values.shape
        with dpg.group(horizontal=True):
            with dpg.plot(width=width - 104, height=height, no_menus=True, no_title=True, no_mouse_pos=True) as pl:
                dpg.bind_colormap(pl, cmap)
                xa = dpg.add_plot_axis(dpg.mvXAxis, label=xlab)
                with dpg.plot_axis(dpg.mvYAxis, label=ylab) as ya:
                    dpg.add_heat_series(np.clip(values, lo, hi).ravel().tolist(), rows, cols, scale_min=lo, scale_max=hi,
                                        bounds_min=(0, 0), bounds_max=(cols_span, rows), format="")
                dpg.set_axis_limits(xa, 0, cols_span)
                dpg.set_axis_limits(ya, 0, rows)
                if yticks:
                    dpg.set_axis_ticks(ya, yticks)
            self.axis_font(pl)
            dpg.add_colormap_scale(min_scale=lo, max_scale=hi, colormap=cmap, height=height, width=72, label="")

    def note(self, height, first, second):
        dpg.add_spacer(height=max(0, height // 2 - 50))
        self.text(first, "body", MUTED)
        self.text(second, "body", DIM)

    def beam_plot(self, height, series, title_kind):
        """series: (label, xs, ys, colour, weight); phase of TX1 vs TX0 per resource block."""
        with dpg.plot(width=-1, height=height, no_menus=True, no_title=True, no_mouse_pos=True) as pl:
            dpg.add_plot_legend(location=dpg.mvPlot_Location_NorthEast)
            xa = dpg.add_plot_axis(dpg.mvXAxis, label="Resource block (4-RB precoding groups)")
            dpg.set_axis_limits(xa, 0, N_PRB)
            with dpg.plot_axis(dpg.mvYAxis, label="Beam phase (deg)") as ya:
                for lab, xs, ys, col, w in series:
                    s = dpg.add_line_series(xs, ys, label=lab)
                    dpg.bind_item_theme(s, self.series_theme(dpg.mvLineSeries, col, 255, w))
            dpg.set_axis_limits(ya, -185, 185)
            dpg.set_axis_ticks(ya, tuple((f"{v}", v) for v in (-180, -90, 0, 90, 180)))
        self.axis_font(pl)

    def links_image(self, cfr_now, scale):
        mag = np.abs(cfr_now)
        img = np.stack([mag[r, c] for r, c in LINKS]) / scale
        return img.reshape(4, HEAT_COLS, -1).mean(axis=-1)

    def recent(self, cfr, row_scale):
        mag = np.abs(cfr[:, 0, 0]) / row_scale
        return mag.reshape(len(mag), HEAT_COLS, -1).mean(axis=-1)[::-1]

    def build_gnb_channel(self, act, inner_w, body_h, GAP):
        key = act["key"]
        self.text("Everything on this tab is what the gNB knows: the feedback it received and the beam it transmits. "
                  "The true channel is only measured at the UE.", "small", DIM)
        body_h -= 34
        half = (inner_w - GAP) // 2
        r1, r2 = int(body_h * 0.45), body_h - int(body_h * 0.45) - GAP - 6
        ticks = tuple((f"RX{r}-TX{c}", 3 - k + 0.5) for k, (r, c) in enumerate(LINKS))
        sub = f"  ·  {act['title']}"
        with dpg.group(horizontal=True, horizontal_spacing=GAP):
            with self.box(half, r1):
                self.heading("CHANNEL RECONSTRUCTED BY THE gNB  ·  RIGHT NOW", sub, tcol(act))
                if key == "legacy":
                    self.note(r1 - 46, "Legacy feedback is a beam index (PMI): the gNB never sees the channel.",
                              "It looks the beam up in the 3GPP codebook (one beam for the whole band).")
                else:
                    P = self.recon[key]
                    self.heat(self.links_image(P[-1], float(np.abs(P[-1]).max())), half, r1 - 46, yticks=ticks)
            with self.box(half, r1):
                self.heading("BEAM THE gNB TRANSMITS  ·  ACROSS THE BAND", sub, tcol(act))
                if key == "legacy":
                    ph = float(wrap_deg(90.0 * self.i2[-1]))
                    series = [("  Codebook beam (one for the whole band)", [0, N_PRB], [ph, ph], tcol(act), 3.0)]
                else:
                    xs, ys = stairs(prg_beam_phase(self.recon[key][-1]))
                    series = [("  One beam per 4 resource blocks", xs, ys, tcol(act), 3.0)]
                self.beam_plot(r1 - 40, series, "gnb")
        dpg.add_spacer(height=GAP - 10)
        with dpg.group(horizontal=True, horizontal_spacing=GAP):
            with self.box(half, r2):
                self.heading("RECONSTRUCTED BY THE gNB  ·  RECENT REPORTS", "  ·  RX0-TX0, newest on top, scaled per report")
                if key == "legacy":
                    self.note(r2 - 46, "No channel reconstruction in Legacy.", "")
                else:
                    P = self.recon[key]
                    rs = np.abs(P[:, 0, 0]).max(axis=1, keepdims=True)
                    self.heat(self.recent(P, rs), half, r2 - 46, "CSI report")
            with self.box(half, r2):
                self.heading("TRANSMITTED BEAM  ·  RECENT REPORTS", "  ·  phase per 4-RB group, newest on top")
                if key == "legacy":
                    img = np.repeat(wrap_deg(90.0 * self.i2)[:, None], 13, axis=1)
                else:
                    img = prg_beam_phase(self.recon[key])
                self.heat(img[::-1], half, r2 - 46, "CSI report", cmap=dpg.mvPlotColormap_Twilight, lo=-180, hi=180,
                          xlab="Resource block", cols_span=N_PRB)

    def build_ue_channel(self, act, inner_w, body_h, GAP):
        key = act["key"]
        self.text("Everything on this tab is what the UE knows: the channel it measures and what it reports. "
                  "What the gNB reconstructs is shown on the gNB station.", "small", DIM)
        body_h -= 34
        half = (inner_w - GAP) // 2
        r1, r2 = int(body_h * 0.45), body_h - int(body_h * 0.45) - GAP - 6
        ticks = tuple((f"RX{r}-TX{c}", 3 - k + 0.5) for k, (r, c) in enumerate(LINKS))
        T = self.true_cfr
        with dpg.group(horizontal=True, horizontal_spacing=GAP):
            with self.box(half, r1):
                self.heading("CHANNEL MEASURED BY THE UE  ·  RIGHT NOW", "  ·  |H| of all RX-TX links (CSI-RS)")
                self.heat(self.links_image(T[-1], float(np.abs(T[-1]).max())), half, r1 - 46, yticks=ticks)
            with self.box(half, r1):
                self.heading("BEST BEAM OF THE MEASURED CHANNEL  ·  ACROSS THE BAND")
                xs, ys = stairs(prg_beam_phase(T[-1]))
                series = [("  Best beam (from the measured channel)", xs, ys, "#E8E6DF", 3.0)]
                if key == "legacy":
                    ph = float(wrap_deg(90.0 * self.i2[-1]))
                    series.append(("  Beam index the UE reports (PMI)", [0, N_PRB], [ph, ph], tcol(act), 3.0))
                self.beam_plot(r1 - 40, series, "ue")
        dpg.add_spacer(height=GAP - 10)
        with dpg.group(horizontal=True, horizontal_spacing=GAP):
            with self.box(half, r2):
                self.heading("MEASURED CHANNEL  ·  RECENT REPORTS", "  ·  RX0-TX0, newest on top, scaled per report")
                rs = np.abs(T[:, 0, 0]).max(axis=1, keepdims=True)
                self.heat(self.recent(T, rs), half, r2 - 46, "CSI report")
            with self.box(half, r2):
                self.heading("BEST BEAM  ·  RECENT REPORTS", "  ·  phase per 4-RB group, newest on top")
                img = prg_beam_phase(T)
                self.heat(img[::-1], half, r2 - 46, "CSI report", cmap=dpg.mvPlotColormap_Twilight, lo=-180, hi=180,
                          xlab="Resource block", cols_span=N_PRB)

    # ------------------------------------------------------------------ live (simulated) updates
    def refresh(self):
        steps = self.steps()
        key = tuple(steps)
        if key != getattr(self, "_steps_key", None):
            self._steps_key = key
            dpg.delete_item("stepper", children_only=True)
            sw = self.t["stepper_w"]
            step = (sw - 50) / (len(steps) - 1)
            col = {"ok": GREEN, "busy": ORANGE, "idle": "#55534E", "fail": RED}
            for k, (name, st) in enumerate(steps):
                x = 25 + k * step
                if k:
                    dpg.draw_line((x - step + 13, 16), (x - 13, 16),
                                  color=rgb(GREEN if steps[k - 1][1] == "ok" and st == "ok" else "#55534E"),
                                  thickness=2, parent="stepper")
                dpg.draw_circle((x, 16), 11, color=rgb(col[st]), fill=rgb(col[st]) if st == "ok" else rgb(SURFACE),
                                thickness=2, parent="stepper")
                dpg.draw_text((x - len(name) * 3.8, 34), name, color=rgb(MUTED if st != "idle" else DIM), size=14,
                              parent="stepper")
            if self.running and all(s == "ok" for _, s in steps) and not getattr(self, "_announced", False):
                self._announced = True
                self.event("ok", "Traffic running" if self.station == "gnb" else "Receiving downlink traffic")
        running = self.running
        dpg.configure_item("btn_start", label="•  Running" if running else self.st["start"], enabled=not running)
        dpg.bind_item_theme("btn_start", self.res["t_running"] if running else self.res["t_off"])
        dpg.configure_item("btn_stop", enabled=running)
        dpg.bind_item_theme("btn_stop", self.res["t_off"])
        if not running:
            status = "Idle  ·  press " + self.st["start"]
        elif not self.traffic():
            busy = next((n for n, s in steps if s == "busy"), "")
            status = f"Bringing up: {busy}..."
        else:
            status = ("Traffic running  ·  UE 10.0.0.3  ·  UDP downlink" if self.station == "gnb"
                      else "Receiving downlink  ·  IP 10.0.0.3  ·  linked to the gNB station")
        dpg.set_value("status_line", status)
        clock, text = self.events[-1]
        dpg.set_value("event_line", f"{clock}  {text}")
        dpg.delete_item("dots", children_only=True)
        x, y = 6, 12
        for i, name in enumerate(self.st["dots"]):
            ok = self.traffic() or (running and i < 2)
            c = rgb(GREEN if ok else "#55534E")
            width = 28 + len(name) * 8
            if x + width > self.t["stepper_w"] + 40:
                x, y = 6, y + 22
            dpg.draw_circle((x, y), 5, color=c, fill=c, parent="dots")
            dpg.draw_text((x + 10, y - 9), name, color=rgb(MUTED), size=15, parent="dots")
            x += width
        if self.live and dpg.does_item_exist("tab_demo"):
            self.refresh_plots(SimpleNamespace(samples=self.live, session_t0=self.live[0].t))

    # ------------------------------------------------------------------ main loop
    def on_click(self):
        if self.station != "gnb":
            return  # the UE station does not choose the scenario
        for key, card in self.cards.items():
            if dpg.does_item_exist(card) and dpg.is_item_hovered(card):
                self.state["scenario"] = key

    def run(self):
        dpg.create_context()
        dpg.create_viewport(title=f"{self.st['title'].title()} · Interoperable AI-Native CSI Feedback (mockup)",
                            width=1920, height=1080)
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
        popup_shown = False
        while dpg.is_dearpygui_running():
            now = time.monotonic()
            frame += 1
            while script and script[0][0] <= now - t0:
                self._script_action(script.pop(0)[1])
            if self.state.pop("fullscreen", False):
                dpg.toggle_viewport_fullscreen()
            if "scenario" in self.state:
                self.switch(self.state.pop("scenario"))
            if self.state.pop("start", False) and not self.running:
                self.running, self.boot_t, self._announced = True, now, False
                self.event("busy", "Starting " + ("core network, gNB and traffic" if self.station == "gnb" else "UE"))
            if self.state.pop("stop", False) and self.running:
                self.running, self.live = False, []
                self.event("ok", "Stopped")
                self._built_key = None
            if self.state.pop("hide_popup", False):
                self.popup_until = 0.0
            if "tab" in self.state:
                tab = self.state.pop("tab")
                self.state["tab_now"] = "channel" if dpg.get_item_alias(tab) == "tab_channel" else "demo"
            show_popup = now < self.popup_until
            cur = (dpg.get_viewport_client_width(), dpg.get_viewport_client_height())
            key = (cur, self.scenario, self.running and self.traffic())
            if frame >= 6 and (key != self._built_key or show_popup != popup_shown):
                for tag in ("main", "popup"):
                    if dpg.does_item_exist(tag):
                        dpg.delete_item(tag)
                self._steps_key = None
                self.build(*cur)
                self._built_key, popup_shown, last = key, show_popup, 0.0
            if self._built_key and now - last >= 0.5:
                last = now
                self.tick(now)
                self.refresh()
            dpg.render_dearpygui_frame()
            time.sleep(max(0.0, 1 / 30 - (time.monotonic() - now)))
        dpg.destroy_context()


def main(station: str) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--active", default="legacy", choices=KEYS)
    ap.add_argument("--fullscreen", action="store_true")
    ap.add_argument("--no-history", dest="history", action="store_false",
                    help="start the plots empty instead of with 3 recorded minutes")
    ap.add_argument("--script", default="", help="testing: <seconds>:<action>,... actions start | stop | legacy | "
                    "same | cross | demo | channel | shot=<png> | quit")
    StationMockup(station, ap.parse_args()).run()
