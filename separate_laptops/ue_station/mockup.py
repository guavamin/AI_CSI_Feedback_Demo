"""UE station GUI, mockup (separate-laptops setup; static replay of the 2026-09-24 OTA runs, not wired to OAI).

    python -m separate_laptops.ue_station.mockup                       # F11 fullscreen, keys 1 / 2 / 3
    python -m separate_laptops.ue_station.mockup --popup --screenshot u.png

Runs on the UE laptop: UE, traffic receiver. Shows what only the UE knows: the ground-truth channel, the
feedback it sends, signal quality and frequency offset, and (by replaying the gNB's decoder, bit-exact with
OAI) the reconstruction and cosine similarity. The feedback scenario follows requests from the gNB station;
on the UE station keys 1 / 2 / 3 simulate such a request.
"""

from __future__ import annotations

import dearpygui.dearpygui as dpg
import numpy as np

from common.style import SCENARIO, SCENARIOS
from separate_laptops import mockup_common
from separate_laptops.station_ui import DIM, MUTED, SC_COL, StationUI

UE_FEEDBACK = {"legacy": "Type-I codebook search (PMI)", "same": "AI encoder, model 1 (Vendor 1)",
               "cross": "AI encoder, model 1 (Vendor 1)"}
GNB_DECODER = {"legacy": "codebook lookup", "same": "Vendor 1 AI decoder",
               "cross": "Vendor 2 AI decoder + linear transform"}
LINKS = ((0, 0), (0, 1), (1, 0), (1, 1))


def build(args, W: int, H: int, d: dict) -> None:
    ui = StationUI("ue")
    act = SCENARIO[args.active]
    key = act["key"]
    k_act = [s["key"] for s in SCENARIOS].index(key)
    prev = getattr(args, "prev", "same" if key == "cross" else "cross")
    PAD, GAP = 16, 10
    inner = W - 2 * PAD
    body = H - 70 - 2 * PAD - 14
    LEFT = int(inner * 0.33)
    RIGHT = inner - LEFT - GAP - 2
    rep = d["ue"][key][1100]
    last = [i for i, g in enumerate(d["seg"]) if g == k_act][-1]
    with dpg.window(tag="main", no_scrollbar=True):
        ui.header(inner)
        dpg.add_spacer(height=2)
        with dpg.group(horizontal=True, horizontal_spacing=GAP):
            # ------------------------------------------------------------ left: steps + event log
            with dpg.child_window(width=LEFT, height=body, border=False, no_scrollbar=True) as left:
                dpg.bind_item_theme(left, ui.child_theme(pad=(0, 0), spacing=(0, 6), bg="#07090A", border="#07090A"))
                sw = LEFT - 4
                used = 0
                used += 6 + ui.step(sw, None, 1, "START UE", "ok", [], (140, lambda: ui.on_off(True)),
                                    sub=[("ok", "USRP B210 found  ·  ext. reference locked"),
                                         ("ok", "Synced to gNB  ·  SSB 234  ·  CFO 4 Hz"),
                                         ("ok", "Attached  ·  RRC connected  ·  registered")])
                used += 6 + ui.step(sw, None, 2, "UE IP", "ok", "oaitun_ue1  10.0.0.52")
                used += 6 + ui.step(sw, None, 3, "TRAFFIC RECEIVER", "ok",
                                    f"iperf3 server :5201  ·  receiving {d['tput'][last]:.1f} Mbit/s",
                                    (140, lambda: ui.on_off(True)))

                def mode_chips():  # read-only: the scenario is chosen on the gNB station
                    bw = (sw - 40) // 3
                    dpg.add_spacer(height=2)
                    with dpg.group(horizontal=True, horizontal_spacing=6):
                        for sc in SCENARIOS:
                            ui.button(f"{sc['n']} {sc['short'].upper()}", bw, active=sc["key"] == key,
                                      color=SC_COL[sc["key"]], enabled=False)

                used += 6 + ui.step(sw, None, 4, "FEEDBACK MODE (SET BY gNB)", "ok",
                                    [f"UE:  {UE_FEEDBACK[key]}", f"gNB: {GNB_DECODER[key]}",
                                     "Link to gNB station: OK  ·  last message 12:07:12"],
                                    extra=mode_chips, extra_h=50)
                same_ue = UE_FEEDBACK[key] == UE_FEEDBACK[prev]
                log = [("12:04:40", "busy", "Starting UE (nr-uesoftmodem)"),
                       ("12:04:44", "ok", "USRP B210 found, ext. reference locked"),
                       ("12:04:47", "ok", "Synced to gNB, CFO 4 Hz"),
                       ("12:04:58", "ok", "RRC connected, registered"),
                       ("12:05:00", "ok", "IP 10.0.0.52 on oaitun_ue1"),
                       ("12:05:04", "ok", "Receiver listening on :5201"),
                       ("12:05:07", "ok", "Receiving downlink, 6.9 Mbit/s"),
                       ("12:07:12", "info", f"gNB request: {act['short']} at frame 787"),
                       ("12:07:12", "ok", "Encoder unchanged, acknowledged" if same_ue
                        else "Armed for frame 787, acknowledged"),
                       ("12:07:12", "ok", f"Now replaying: {GNB_DECODER[key]}" if key != "legacy"
                        else "Switched at frame 787: legacy PMI")]
                ui.event_log(sw, body - used, log)

            # ------------------------------------------------------------ right: readouts + UE-only views
            with dpg.child_window(width=RIGHT, height=body, border=False, no_scrollbar=True) as right:
                dpg.bind_item_theme(right, ui.child_theme(pad=(0, 0), spacing=(GAP, GAP), bg="#07090A", border="#07090A"))
                fb = f"i2={rep['i2']}" if key == "legacy" else "52 bit"
                tiles = (("RX THROUGHPUT", f"{d['tput'][last]:.2f}", "Mbps"), ("RSRP", rep["rsrp_dBm"], "dBm"),
                         ("SINR", rep["sinr_dB"], "dB"), ("CQI", rep["cqi"], ""), ("CFO", "4", "Hz"),
                         ("FEEDBACK", fb, ""), ("SCENARIO", act["short"].upper(), ""))
                tw = (RIGHT - GAP * (len(tiles) - 1) - 2) // len(tiles)
                with dpg.group(horizontal=True, horizontal_spacing=GAP):
                    for lab, val, unit in tiles:
                        ui.readout(tw, lab, str(val), unit, color=SC_COL[key] if lab == "SCENARIO" else None)
                rh = (body - 86 - 3 * GAP - 4) // 3
                col_w = (RIGHT - GAP) // 2
                T = d["ch"]["true_cfr"]
                P = d["ch"][f"{key}_cfr"] if key != "legacy" else None
                tm = np.abs(T)
                scale_now = float(tm[-1].max())
                ticks = tuple((f"RX{r}-TX{c}", 3 - k + 0.5) for k, (r, c) in enumerate(LINKS))
                row_scale = tm[:, 0, 0].max(axis=1, keepdims=True)

                def links_now(a):  # heat-series row 0 is drawn at the top
                    return np.stack([a[r, c] for r, c in LINKS]) / scale_now

                def legacy_note():
                    dpg.add_spacer(height=rh // 2 - 60)
                    ui.text("Legacy feedback is a beam index (PMI).", "body", MUTED)
                    ui.text("The gNB never reconstructs the channel.", "body", DIM)

                hh = rh - 44
                with dpg.group(horizontal=True, horizontal_spacing=GAP):
                    with ui.panel(col_w, rh, "GROUND TRUTH · CHANNEL RIGHT NOW · |H| ALL RX-TX LINKS", spacing=(6, 4)):
                        ui.heat(links_now(tm[-1]), col_w - 28, hh, yticks=ticks)
                    with ui.panel(col_w, rh, f"RECONSTRUCTED BY THE gNB (REPLAY) · {act['short'].upper()}", spacing=(6, 4)):
                        legacy_note() if P is None else ui.heat(links_now(np.abs(P[-1])), col_w - 28, hh, yticks=ticks)
                with dpg.group(horizontal=True, horizontal_spacing=GAP):
                    with ui.panel(col_w, rh, "GROUND TRUTH · LAST 10 s · RX0-TX0", spacing=(6, 4)):
                        ui.heat((tm[:, 0, 0] / row_scale)[::-1], col_w - 28, hh, ylab="Report")
                    with ui.panel(col_w, rh, spacing=(6, 4)):
                        with dpg.group(horizontal=True, horizontal_spacing=20):
                            ui.text("> CHANNEL COSINE SIMILARITY", "kicker", ui.accent)
                            ui.scenario_key_line()
                        ui.cdf_plot(hh, {s["key"]: d["cos"][f"{s['key']}_beamcos"] for s in SCENARIOS},
                                    "Cosine similarity", (0.5, 1.0), key)
                with dpg.group(horizontal=True, horizontal_spacing=GAP):
                    with ui.panel(col_w, rh, "FEEDBACK SENT ON PUCCH", spacing=(6, 4)):
                        if key == "legacy":
                            i2 = [int(r["i2"]) for r in d["ue"]["legacy"][1000:1060]]
                            ui.pmi_strip(col_w - 30, hh, i2, SC_COL["legacy"])
                        else:
                            cqi = [int(float(c)) for c in d["cqi"][last - 9: last + 1]][::-1]
                            ui.bit_waterfall(col_w - 30, hh, d["latents"][-10:][::-1], cqi, SC_COL[key])
                    with ui.panel(col_w, rh, "RX THROUGHPUT", spacing=(6, 2)):
                        ui.time_plot(hh, d["t"], d["tput"], d["seg"], "Mbps", (0, 9))
    dpg.set_primary_window("main", True)
    if args.popup:
        lines = [("request received from gNB station", "ok")]
        if UE_FEEDBACK[key] == UE_FEEDBACK[prev]:
            lines.append(("UE encoder unchanged (model 1)", "ok"))
        else:
            lines += [("UE armed for frame 787", "ok"), ("switched at frame 787", "ok")]
        lines += [("acknowledged to gNB station", "ok"), (f"gNB decoder: {GNB_DECODER[key]}", None)]
        ui.popup(W - 640 - PAD - 10, 70 + 120, 640, f"gNB REQUEST -> {act['title'].upper()}", lines, SC_COL[key])


if __name__ == "__main__":
    mockup_common.run(build, "UE station · AI-native CSI feedback demo")
