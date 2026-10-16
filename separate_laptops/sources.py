"""What the station window draws, from recorded data (MockSource) or from the live stations (GnbSource, UeSource).

Interface used by StationUI (duck-typed):
  station ("gnb" | "ue"), can_choose, ota
  scenario() -> (key on the cards / diagram, switching); running(); steps(); status_line(); last_event();
  dots(); plot_view() (samples + session_t0, as DemoView); channel() -> ChannelView; popup() -> None or
  (title, [(line, ok | busy | fail | None)], key); start(); stop(); request(key); hide_popup(); tick(); shutdown()
"""

from __future__ import annotations

import csv
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from common.oai.config import AppConfig
from common.replay.engine import codebook_beam, dominant_beams
from common.station_data import HISTORY, ChannelView, GnbChannelWorker, beam_phase_deg
from common.style import MOCKUP_DATA, SCENARIO, SCENARIOS
from same_laptop.app import KEYS, core_network_up

POPUP_S = 5.0
GNB_DECODER = {"legacy": "codebook lookup (beam index only)", "same": "Vendor 1 AI decoder",
               "cross": "Vendor 2 AI decoder + translation"}
UE_FEEDBACK = {"legacy": "codebook search (PMI)", "same": "AI encoder (Vendor 1)", "cross": "AI encoder (Vendor 1)"}


def _short(key: str) -> str:
    return SCENARIO[key]["short"]


def _pmi(cfr: np.ndarray) -> np.ndarray:
    """The UE's wideband PMI (2-port Type I, w = [1, j^i2]): the codeword with the most power. cfr [..., rx, tx, 612]."""
    g = [np.sum(np.abs(cfr[..., :, 0, :] + (1j ** i) * cfr[..., :, 1, :]) ** 2, axis=(-2, -1)) for i in range(4)]
    return np.argmax(np.stack(g, axis=-1), axis=-1)


# =============================================================================================== mockup
class MockSource:
    """Recorded OTA data (data/mockups): runs 01 / 02 / 06 per second, 120 recorded CSI reports. The bring-up
    is simulated (one step per 1.2 s). Nothing talks to OAI."""

    ota = True

    def __init__(self, station: str, active: str = "legacy", history: bool = True):
        self.station, self.can_choose = station, station == "gnb"
        self.history = history
        self.clock = "internal"
        self._key = active
        self.runs = {s["key"]: [r for r in csv.DictReader(open(MOCKUP_DATA / "runs" / s["run"] / "metrics.csv"))
                                if float(r["dl_mbps"]) > 0.5] for s in SCENARIOS}
        self.ue = {s["key"]: list(csv.DictReader(open(MOCKUP_DATA / "runs" / s["run"] / "ue_reports.csv")))
                   for s in SCENARIOS}
        ch = dict(np.load(MOCKUP_DATA / "mockup_channels.npz"))
        self.cfr = {"truth": ch["true_cfr"], "same": ch["same_cfr"], "cross": ch["cross_cfr"]}
        self.beams = {k: beam_phase_deg(np.stack([dominant_beams(c) for c in v])) for k, v in self.cfr.items()}
        self.pmi_deg = beam_phase_deg(np.stack([codebook_beam(i) for i in _pmi(self.cfr["truth"])]))
        self.running_ = False
        self.boot_t = 0.0
        self.samples: list = []
        self.cursor = {k: 0 for k in KEYS}
        self.events = [(time.strftime("%H:%M:%S"), "info", "Station ready (mockup: recorded OTA data)")]
        self._popup = None
        self._popup_until = 0.0
        self._announced = False
        self._report0 = time.monotonic()

    STEPS = {"gnb": ("Core", "gNB", "USRP", "UE", "Wi-Fi", "Traffic"),
             "ue": ("USRP", "Sync", "Attach", "IP", "Receiver", "Wi-Fi")}

    def event(self, level, text):
        self.events.append((time.strftime("%H:%M:%S"), level, text))

    def scenario(self):
        return self._key, False

    def running(self):
        return self.running_

    def steps(self):
        names = self.STEPS[self.station]
        if not self.running_:
            return [(n, "idle") for n in names]
        done = int((time.monotonic() - self.boot_t) / 1.2)
        return [(n, "ok" if i < done else ("busy" if i == done else "idle")) for i, n in enumerate(names)]

    def traffic(self):
        return self.running_ and all(s == "ok" for _, s in self.steps())

    def status_line(self):
        if not self.running_:
            return "Idle  ·  press " + ("Start system" if self.station == "gnb" else "Start UE")
        if not self.traffic():
            return "Bringing up: " + next((n for n, s in self.steps() if s == "busy"), "") + "..."
        return ("Traffic running  ·  UE 10.0.0.3  ·  UDP downlink" if self.station == "gnb"
                else "Receiving downlink  ·  IP 10.0.0.3  ·  linked to the gNB station")

    def last_event(self):
        return self.events[-1]

    def dots(self):
        names = (("Core", "gNB", "USRP", "UE station (Wi-Fi)", "Telnet") if self.station == "gnb"
                 else ("UE", "USRP", "Receiver", "gNB station (Wi-Fi)", "Telnet"))
        return [(n, self.traffic() or (self.running_ and i < 2)) for i, n in enumerate(names)]

    def _sample(self, t, key):
        rows, ue = self.runs[key], self.ue[key]
        j = self.cursor[key] = self.cursor[key] + 1
        r = rows[j % len(rows)]
        return SimpleNamespace(t=t, scenario=key, tput_mbps=float(r["dl_mbps"]), mcs=float(r["mcs"]),
                               bler=float(r["bler"]), sinr=float(ue[(j * 12) % len(ue)]["sinr_dB"]))

    def tick(self):
        now = time.monotonic()
        if self.running_ and self.traffic() and not self._announced:
            self._announced = True
            self.event("ok", "Traffic running" if self.station == "gnb" else "Receiving downlink traffic")
        if not self.traffic():
            return
        if not self.samples:
            if self.history:
                self.samples = [self._sample(now - 180 + i, KEYS[i // 60]) for i in range(180)]
            self.samples.append(self._sample(now, self._key))
        elif now - self.samples[-1].t >= 1.0:
            self.samples.append(self._sample(self.samples[-1].t + 1.0, self._key))

    def plot_view(self):
        return SimpleNamespace(samples=self.samples, session_t0=self.samples[0].t if self.samples else None)

    def channel(self):
        if not self.traffic():
            return ChannelView()
        n = len(self.cfr["truth"])
        idx = int((time.monotonic() - self._report0) * 12)  # ~12 reports / s, looping over the recorded 120
        rows = [(idx - k) % n for k in range(min(HISTORY, idx + 1))][::-1]
        if self.station == "ue":
            c = self.cfr["truth"][rows[-1]]
            return ChannelView(n_reports=idx + 1, cfr=c, beam_deg=self.beams["truth"][rows[-1]],
                               pmi_deg=float(self.pmi_deg[rows[-1]]) if self._key == "legacy" else None,
                               hist_mag=np.abs(self.cfr["truth"][rows, 0, 0]), hist_beam=self.beams["truth"][rows])
        if self._key == "legacy":
            p = self.pmi_deg[rows]
            return ChannelView(n_reports=idx + 1, beam_deg=np.full(13, p[-1]), pmi_deg=float(p[-1]), kind="legacy",
                               hist_mag=np.full((len(rows), 612), np.nan), hist_beam=np.repeat(p[:, None], 13, axis=1))
        rec = self.cfr[self._key]
        return ChannelView(n_reports=idx + 1, cfr=rec[rows[-1]], beam_deg=self.beams[self._key][rows[-1]],
                           kind=self._key, hist_mag=np.abs(rec[rows, 0, 0]), hist_beam=self.beams[self._key][rows])

    def popup(self):
        return self._popup if time.monotonic() < self._popup_until else None

    def hide_popup(self):
        self._popup_until = 0.0

    def start(self):
        if not self.running_:
            self.running_, self.boot_t, self._announced = True, time.monotonic(), False
            self.samples = []
            self.event("busy", "Starting " + ("core network, gNB and traffic" if self.station == "gnb" else "UE"))

    def stop(self):
        if self.running_:
            self.running_, self.samples = False, []
            self.event("ok", "Stopped")

    def request(self, key):
        """gNB: the scenario cards / keys. UE (mockup only): keys simulate a request from the gNB station."""
        if key == self._key:
            return
        prev, self._key = self._key, key
        title = SCENARIO[key]["title"]
        if self.station == "gnb":
            lines = [("Request sent to the UE station (Wi-Fi)", "ok"), ("UE station armed for frame 787", "ok"),
                     ("Switched at frame 787", "ok"), (f"gNB: {GNB_DECODER[key]}", None)]
            if "legacy" not in (key, prev):
                lines[:2] = [("gNB decoder swap at frame 787 (UE unchanged)", "ok")]
                lines.pop(1)
            self._popup = (f"SWITCHING TO {title.upper()}", lines, key)
        else:
            lines = [("Request from the gNB station (Wi-Fi)", "ok"), ("Armed for frame 787", "ok"),
                     ("Switched at frame 787", "ok"), (f"UE feedback: {UE_FEEDBACK[key]}", None)]
            if "legacy" not in (key, prev):
                lines[1:3] = [("Encoder unchanged: nothing to switch on the UE", "ok")]
            self._popup = (f"gNB STATION: {title.upper()}", lines, key)
        self._popup_until = time.monotonic() + POPUP_S
        self.event("ok", f"{_short(prev)} -> {_short(key)}")

    def set_clock(self, value):
        self.clock = value
        self.event("info", f"USRP clock reference: {value} (clock / time source {1 if value == 'external' else 0} "
                           f"at the next start)")

    def shutdown(self):
        pass


# =============================================================================================== gNB station
class GnbSource:
    """The live gNB station (separate_laptops.gnb_station.controller.GnbDemo)."""

    station, can_choose = "gnb", True

    def __init__(self, cfg: AppConfig):
        from separate_laptops.gnb_station.controller import GnbDemo

        self.cfg = cfg
        self.ota = not cfg.oai.rfsim
        self.clock = "internal"  # USRP clock reference until the switch is used
        self.demo = GnbDemo(cfg)
        self.st = self.demo.station
        if self.ota:
            self.st.launcher.clock_source = self.clock
        a, b = cfg.oai.gnb.base_args, Path(cfg.oai.build_dir)
        self.channel_worker = GnbChannelWorker(
            lambda: str(self.st.csirs_record_root), b.parents[2], b,
            {0: Path(a[a.index("--ai-fb-model-path") + 1]), 1: Path(a[a.index("--ai-fb-live-alt-model-path") + 1])})
        self._popup = None
        self._popup_until = 0.0
        self._pending_key = None
        self._prev = "legacy"
        self._stop = threading.Event()
        self.demo.start()
        self.channel_worker.start()
        threading.Thread(target=self._core_watch, name="core-watch", daemon=True).start()

    def _core_watch(self):
        while not self._stop.wait(5.0 if self.st.core_ok else 2.0):
            self.st.core_ok = core_network_up()

    def scenario(self):
        scen, target, switching = self.demo.state_key()
        return (target if switching else scen), switching

    def running(self):
        s = self.st.get_snapshot()
        return s.gnb_running or s.traffic_desired

    def steps(self):
        return self.st.steps()

    def status_line(self):
        s, ue = self.st.get_snapshot(), self.st.ue
        if not self.st.core_ok and not self.running():
            return "Core network (OAI CN5G) is not running: start it with docker compose first."
        if not ue:
            link = f"UE station {self.st.link.host} not reachable"
            return link if not self.running() else f"gNB: {s.gnb_boot_label or 'starting'}  ·  {link}"
        if s.traffic_stage == "running":
            return f"Traffic running  ·  UE {s.ue_ip}  ·  UDP downlink  ·  link {self.st.link.rtt_s * 1000:.0f} ms"
        if self.running():
            return f"gNB: {s.gnb_boot_label or 'starting'}  ·  UE: {ue.get('stage_label') or 'not started'}"
        return ("Idle  ·  UE station connected  ·  " + ("over the air (B210)" if self.ota else "RF simulator"))

    def last_event(self):
        ev = self.demo.view().events
        return (ev[-1].clock, ev[-1].level, ev[-1].text) if ev else None

    def dots(self):
        s = self.st.get_snapshot()
        return [("Core", self.st.core_ok), ("gNB", s.gnb_running), ("USRP" if self.ota else "RF sim", s.gnb_boot_stage == "rf_ssb"),
                ("UE station (Wi-Fi)", bool(self.st.ue)), ("Telnet", bool(self.demo.view().aifb))]

    def plot_view(self):
        return self.demo.view()

    def channel(self):
        return self.channel_worker.view()

    def tick(self):
        scen, target, switching = self.demo.state_key()
        key = self._pending_key
        if key is None:
            return
        prev = self._prev
        frame = self.st.target_frame
        ai_change = "legacy" in (key, prev)
        events = [e.text for e in self.demo.view().events[-6:]]
        done = not switching and scen == key
        failed = not switching and scen != key
        lines = [("Request sent to the UE station (Wi-Fi)" if ai_change else "gNB only: the UE keeps its encoder",
                  "ok")]
        if ai_change:
            lines.append((f"Armed on both sides for frame {frame}", "ok") if frame is not None
                         else ("Arming gNB and UE...", "busy"))
        else:
            dec = next((t for t in reversed(events) if "decoder" in t and "frame" in t), "Arming the decoder swap...")
            lines.append((dec, "ok" if "active" in dec or done else "busy"))
        lines.append(("Switched" + (f" at frame {frame}" if ai_change and frame is not None else ""), "ok") if done else
                     (("Switch failed: " + next((t for t in reversed(events) if "failed" in t.lower()), "see log")[:60], "fail")
                      if failed else ("Waiting for the frame...", "busy")))
        lines.append((f"gNB: {GNB_DECODER[key]}", None))
        self._popup = (f"SWITCHING TO {SCENARIO[key]['title'].upper()}", lines, key)
        if done or failed:
            self._popup_until = time.monotonic() + POPUP_S
            self._pending_key = None

    def popup(self):
        return self._popup if (self._pending_key is not None or time.monotonic() < self._popup_until) else None

    def hide_popup(self):
        self._popup_until = 0.0
        self._pending_key = None

    def start(self):
        self.channel_worker.clear()
        self.demo.start_system()

    def stop(self):
        threading.Thread(target=self.demo.stop_system, name="stop", daemon=True).start()

    def request(self, key):
        scen, _, _ = self.demo.state_key()
        if key == scen:
            return
        self.st.target_frame = None
        if self.demo.set_scenario(key) and self.st.get_snapshot().gnb_running:
            self._prev, self._pending_key = scen, key

    def set_clock(self, value):
        """the reference of this laptop's USRP for the next start (gNB: --RUs.[0].clock_src / time_src)."""
        self.clock = self.st.launcher.clock_source = value
        self.demo.event("info", f"USRP clock reference: {value} (applied at the next start)")

    def shutdown(self):
        self._stop.set()
        self.channel_worker.stop()
        self.demo.shutdown()


# =============================================================================================== UE station
class UeSource:
    """The live UE station (separate_laptops.ue_station.controller.UeStation)."""

    station, can_choose = "ue", False

    def __init__(self, cfg: AppConfig):
        from separate_laptops.ue_station.controller import UeStation

        self.ota = not cfg.oai.rfsim
        self.clock = "internal"  # USRP clock reference until the switch is used
        self.ue = UeStation(cfg)
        if self.ota:
            self.ue.launcher.clock_source = self.clock
        self.ue.start()
        self._hidden_t = None
        self._v = self.ue.view()
        self._v_t = 0.0

    def _view(self):
        if time.monotonic() - self._v_t > 0.2:
            self._v, self._v_t = self.ue.view(), time.monotonic()
        return self._v

    def scenario(self):
        v = self._view()
        p = v.popup
        switching = p.get("state") == "switching" and time.monotonic() - p.get("t", 0) < 10
        return (p["key"] if switching else v.scenario), switching

    def running(self):
        return self._view().desired

    def steps(self):
        return self._view().steps

    def status_line(self):
        v = self._view()
        link = f"linked to the gNB station ({v.peer})" if v.link_up else "gNB station not connected"
        if v.server_running and v.rx_silent_s:
            return (f"Receiver listening on {v.ue_ip}, but no downlink data for {v.rx_silent_s:.0f} s: is traffic "
                    f"running on the gNB station? A firewall on this laptop (ufw) blocks it unless "
                    f"'ufw allow in on oaitun_ue1'")
        if v.server_running:
            return f"Receiving downlink  ·  IP {v.ue_ip}  ·  {link}"
        if v.desired:
            return f"UE: {v.stage_label}  ·  {link}"
        return f"Idle  ·  {link}"

    def last_event(self):
        ev = self._view().events
        return (ev[-1].clock, ev[-1].level, ev[-1].text) if ev else None

    def dots(self):
        v = self._view()
        return [("UE", v.running), ("USRP" if self.ota else "RF sim", v.running and v.stage not in ("starting", "init")),
                ("Receiver", v.server_running), ("gNB station (Wi-Fi)", v.link_up), ("Telnet", v.running)]

    def plot_view(self):
        v = self._view()
        return SimpleNamespace(samples=v.samples, session_t0=v.session_t0)

    def channel(self):
        return self.ue.channel.view()

    def tick(self):
        pass

    def popup(self):
        p = self._view().popup
        if not p or p.get("t") == self._hidden_t:
            return None
        live = p.get("state") == "live"
        if live and time.monotonic() - p["t"] > POPUP_S:
            return None
        key, prev = p["key"], p.get("prev", "legacy")
        frame = p.get("frame")
        lines = [("Request from the gNB station (Wi-Fi)", "ok")]
        if "legacy" in (key, prev):
            lines.append((f"Armed for frame {frame}" if frame is not None else "Armed for the agreed frame", "ok"))
            lines.append(("Switched" + (f" at frame {frame}" if frame is not None else ""), "ok") if live
                         else ("Waiting for the frame...", "busy"))
        else:
            lines.append(("Encoder unchanged: nothing to switch on the UE", "ok"))
        lines.append((f"UE feedback: {UE_FEEDBACK[key]}", None))
        return (f"gNB STATION: {SCENARIO[key]['title'].upper()}", lines, key)

    def hide_popup(self):
        p = self._view().popup
        self._hidden_t = p.get("t") if p else None

    def start(self):
        self.ue.start_ue()

    def stop(self):
        threading.Thread(target=self.ue.stop_ue, name="stop", daemon=True).start()

    def request(self, key):
        pass  # chosen on the gNB station

    def set_clock(self, value):
        """the reference of this laptop's USRP for the next start (UE: --clock-source / --time-source 0 | 1)."""
        self.clock = self.ue.launcher.clock_source = value
        self.ue.event("info", f"USRP clock reference: {value} (clock / time source {1 if value == 'external' else 0} "
                              f"at the next UE start)")

    def shutdown(self):
        self.ue.shutdown()

