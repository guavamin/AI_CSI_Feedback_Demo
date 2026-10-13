"""gNB station GUI, mockup (separate-laptops setup; static replay of the 2026-09-24 OTA runs, not wired to OAI).

    python -m separate_laptops.gnb_station.mockup                      # F11 fullscreen, keys 1 / 2 / 3
    python -m separate_laptops.gnb_station.mockup --popup --screenshot g.png

Runs on the gNB laptop: core network, gNB, downlink stream, scenario control. Shows everything the gNB
knows (throughput, MCS, BLER, CQI, uplink SNR, the feedback it receives); the ground-truth channel exists
only at the UE, so that panel points to the UE station. A scenario change is sent to the UE station through
the 5G link; both stations show a pop-up.
"""

from __future__ import annotations

import dearpygui.dearpygui as dpg

from common.style import SCENARIO, SCENARIOS
from separate_laptops import mockup_common
from separate_laptops.station_ui import DIM, MUTED, SC_COL, StationUI

GNB_DECODER = {"legacy": "codebook lookup (no AI decoder)", "same": "Vendor 1 AI decoder",
               "cross": "Vendor 2 AI decoder + linear transform"}


def build(args, W: int, H: int, d: dict) -> None:
    ui = StationUI("gnb")
    act = SCENARIO[args.active]
    k_act = [s["key"] for s in SCENARIOS].index(args.active)
    PAD, GAP = 16, 10
    inner = W - 2 * PAD
    HEAD = 70
    body = H - HEAD - 2 * PAD - 14
    LEFT = int(inner * 0.33)
    RIGHT = inner - LEFT - GAP - 2
    seg_rows = [i for i, g in enumerate(d["seg"]) if g == k_act]
    last = seg_rows[-1]
    with dpg.window(tag="main", no_scrollbar=True):
        ui.header(inner)
        dpg.add_spacer(height=2)
        with dpg.group(horizontal=True, horizontal_spacing=GAP):
            # ------------------------------------------------------------ left: steps + event log
            with dpg.child_window(width=LEFT, height=body, border=False, no_scrollbar=True) as left:
                dpg.bind_item_theme(left, ui.child_theme(pad=(0, 0), spacing=(0, 6), bg="#07090A", border="#07090A"))
                sw = LEFT - 4
                used = 0
                used += 6 + ui.step(sw, None, 1, "CORE NETWORK", "ok", "OAI CN5G up: AMF SMF UPF NRF AUSF UDM UDR (docker)",
                        (140, lambda: ui.on_off(True)))
                used += 6 + ui.step(sw, None, 2, "START gNB", "ok", ["nr-softmodem running  ·  telnet :9090",
                                                       "USRP B210 31F3CC3  ·  ext. reference locked"],
                        (140, lambda: ui.on_off(True)))
                used += 6 + ui.step(sw, None, 3, "UE ATTACHED", "ok", "RNTI 0x80e0  ·  in-sync  ·  RSRP -77 dBm")
                used += 6 + ui.step(sw, None, 4, "FIND UE IP", "ok", "10.0.0.52  (from the core network, SMF)",
                        (100, lambda: ui.button("REFRESH", 100)))
                used += 6 + ui.step(sw, None, 5, "UE RECEIVER", "ok", "10.0.0.52:5201 reachable  ·  ping 18 ms",
                        (100, lambda: ui.button("CHECK", 100)))
                used += 6 + ui.step(sw, None, 6, "DOWNLINK STREAM", "ok", "iperf3 UDP 8 Mbit/s  ->  10.0.0.52",
                        (140, lambda: ui.on_off(True)))

                def scenario_buttons():
                    bw = (sw - 40) // 3
                    dpg.add_spacer(height=2)
                    with dpg.group(horizontal=True, horizontal_spacing=6):
                        for sc in SCENARIOS:
                            ui.button(f"{sc['n']} {sc['short'].upper()}", bw, active=sc["key"] == act["key"],
                                      color=SC_COL[sc["key"]])

                used += 6 + ui.step(sw, None, 7, "FEEDBACK SCENARIO", "ok",
                        [f"gNB: {GNB_DECODER[act['key']]}", "UE control link: OK (through the 5G link)"],
                        extra=scenario_buttons, extra_h=50)
                prev = SCENARIO[getattr(args, "prev", "same" if act["key"] == "cross" else "cross")]
                log = [("12:04:02", "ok", "Core network up (8 containers)"),
                       ("12:04:10", "busy", "Starting gNB (nr-softmodem)"),
                       ("12:04:21", "ok", "USRP B210 locked to external reference"),
                       ("12:04:22", "ok", "gNB running, waiting for UE"),
                       ("12:04:58", "ok", "UE attached, RNTI 0x80e0"),
                       ("12:05:02", "ok", "UE IP 10.0.0.52 (SMF)"),
                       ("12:05:06", "ok", "UE receiver reachable, ping 18 ms"),
                       ("12:05:07", "ok", "Downlink stream started, UDP 8 Mbit/s"),
                       ("12:07:12", "busy", f"Switch {prev['short']} -> {act['short']} armed, frame 787"),
                       ("12:07:12", "ok", "UE station confirmed" if "legacy" in (act["key"], prev["key"])
                        else "UE station informed (encoder unchanged)"),
                       ("12:07:12", "ok", f"Switched at frame 787: {GNB_DECODER[act['key']]}")]
                ui.event_log(sw, body - used, log)

            # ------------------------------------------------------------ right: readouts, plots, feedback
            with dpg.child_window(width=RIGHT, height=body, border=False, no_scrollbar=True) as right:
                dpg.bind_item_theme(right, ui.child_theme(pad=(0, 0), spacing=(GAP, GAP), bg="#07090A", border="#07090A"))
                tiles = (("DL THROUGHPUT", f"{d['tput'][last]:.2f}", "Mbps"), ("MCS", f"{d['mcs'][last]:.0f}", ""),
                         ("BLER", f"{d['bler'][last]:.3f}", ""), ("CQI", f"{d['cqi'][last]:.0f}", ""),
                         ("UL SNR", "35.0", "dB"), ("RSRP (UE)", "-77", "dBm"),
                         ("SCENARIO", act["short"].upper(), ""))
                tw = (RIGHT - GAP * (len(tiles) - 1) - 2) // len(tiles)
                with dpg.group(horizontal=True, horizontal_spacing=GAP):
                    for lab, val, unit in tiles:
                        ui.readout(tw, lab, val, unit, color=SC_COL[act["key"]] if lab == "SCENARIO" else None)
                bottom_h = 190
                plots_h = body - 86 - bottom_h - 2 * GAP - 4
                ph = (plots_h - 2 * GAP) // 3
                col_w = (RIGHT - GAP) // 2
                for k, (name, ys, col, lab, lab_cdf, ylim, xlim) in enumerate((
                        ("DL THROUGHPUT", d["tput"], "dl_mbps", "Mbps", "Throughput (Mbps)", (0, 9), (3, 9)),
                        ("MCS", d["mcs"], "mcs", "MCS", "MCS index", (0, 16), (4, 14)),
                        ("BLER", d["bler"], "bler", "BLER", "BLER", (0, 0.25), (0, 0.25)))):
                    with dpg.group(horizontal=True, horizontal_spacing=GAP):
                        with ui.panel(col_w, ph, name, spacing=(6, 2)):
                            ui.time_plot(ph - 40, d["t"], ys, d["seg"], lab, ylim)
                        with ui.panel(col_w, ph, spacing=(6, 2)):
                            with dpg.group(horizontal=True, horizontal_spacing=20):
                                ui.text(f"> {name} CDF", "kicker", ui.accent)
                                if k == 0:
                                    ui.scenario_key_line()
                            ui.cdf_plot(ph - 40, d["cdf"][col], lab_cdf, xlim, act["key"])
                with dpg.group(horizontal=True, horizontal_spacing=GAP):
                    with ui.panel(col_w, bottom_h, "FEEDBACK RECEIVED ON PUCCH", spacing=(6, 4)):
                        if act["key"] == "legacy":
                            i2 = [int(r["i2"]) for r in d["ue"]["legacy"][1000:1060]]
                            ui.pmi_strip(col_w - 30, bottom_h - 44, i2, SC_COL["legacy"])
                        else:
                            cqi = [int(float(c)) for c in d["cqi"][last - 9: last + 1]][::-1]
                            ui.bit_waterfall(col_w - 30, bottom_h - 44, d["latents"][-10:][::-1], cqi, SC_COL[act["key"]])
                    with ui.panel(col_w, bottom_h, "CHANNEL", spacing=(6, 6)):
                        dpg.add_spacer(height=24)
                        ui.text("The ground-truth channel is measured only at the UE.", "body", MUTED)
                        ui.text("See the UE station for the channel and cosine similarity.", "body", DIM)
    dpg.set_primary_window("main", True)
    if args.popup:
        same_ue = act["key"] != "legacy" and getattr(args, "prev", "same") != "legacy"
        lines = [("frame 787 armed on gNB", "ok"),
                 ("UE informed (encoder unchanged)" if same_ue else "UE station confirmed", "ok"),
                 ("switched at frame 787", "ok"),
                 (f"gNB: {GNB_DECODER[act['key']]}", None)]
        ui.popup(W - 640 - PAD - 10, HEAD + 120, 640, f"SCENARIO -> {act['title'].upper()}", lines, SC_COL[act["key"]])


if __name__ == "__main__":
    mockup_common.run(build, "gNB station · AI-native CSI feedback demo")
