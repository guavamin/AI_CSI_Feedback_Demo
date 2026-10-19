"""Robot tab of the UE station: parameters, expected trajectory, status, Start / Stop of the AgileX Scout Mini.

Mixed into StationUI (uses its helpers: text, box, heading, child_theme, series_theme, axis_font, res, state).
The source provides .robot (separate_laptops.ue_station.robot.RobotControl) and, for the CAN interface,
.can_status() / .bring_up_can().
"""

from __future__ import annotations

import dearpygui.dearpygui as dpg
import numpy as np

from common.style import BLUE, DIM, GREEN, MUTED, ORANGE, TEXT, rgb
from same_laptop.app import RED
from separate_laptops.ue_station.robot import MAX_SPEED, MAX_TURN, RobotParams, expected_path

PHASE_TEXT = {"idle": "Stopped", "connecting": "Connecting to the robot (CAN)...", "connected": "Connected",
              "forward": "Driving forward", "pause_end": "Paused at the far end", "backward": "Driving back",
              "pause_start": "Paused at the start", "done": "Finished all cycles", "stopped": "Stopped",
              "error": "Error"}


class RobotTabMixin:
    def build_robot_tab(self, inner_w, body_h, GAP):
        robot = self.src.robot
        p = robot.params
        left_w = max(400, int(inner_w * 0.27))
        right_w = inner_w - left_w - GAP
        with dpg.group(horizontal=True, horizontal_spacing=GAP):
            with dpg.child_window(width=left_w, height=body_h, border=False, no_scrollbar=True) as left:
                dpg.bind_item_theme(left, self.child_theme(rgb("#1B1B1A"), rgb("#1B1B1A"), pad=(0, 0), spacing=(8, GAP)))
                with self.box(left_w, 218):
                    self.heading("ROBOT", "  ·  AgileX Scout Mini carrying the UE")
                    with dpg.group(horizontal=True):
                        dpg.add_drawlist(width=14, height=18, tag="rb_can_dot")
                        self.text("", "small", MUTED, tag="rb_can")
                    dpg.add_button(label="Bring up CAN", tag="rb_can_btn", height=30, show=False,
                                   callback=lambda: self.state.update(robot_can=True))
                    self.text("", "body", TEXT, tag="rb_state", wrap=left_w - 40)
                    self.text("", "small", MUTED, tag="rb_detail", wrap=left_w - 40)
                    bw = (left_w - 60) // 2
                    with dpg.group(horizontal=True):
                        b1 = dpg.add_button(label="Start robot", width=bw, height=44, tag="rb_start",
                                            callback=lambda: self.state.update(robot_start=True))
                        b2 = dpg.add_button(label="Stop robot", width=bw, height=44, tag="rb_stop",
                                            callback=lambda: self.state.update(robot_stop=True))
                    for b in (b1, b2):
                        dpg.bind_item_font(b, self.res["med"])
                    dpg.bind_item_theme(b2, self.res["t_rb_stop"])
                with self.box(left_w, body_h - 218 - GAP):
                    self.heading("PARAMETERS", "  ·  applied at Start")
                    dpg.add_radio_button(("Straight line", "Arc"), tag="rb_pattern", horizontal=True,
                                         default_value="Arc" if p.pattern == "arc" else "Straight line",
                                         callback=self._robot_param_changed)
                    sw = left_w - 40
                    for tag, label, kind, lo, hi, val, fmt in (
                            ("rb_speed", "Speed (m/s)", "f", 0.05, MAX_SPEED, p.speed, "%.2f m/s"),
                            ("rb_leg", "Time per direction (s)", "f", 2.0, 60.0, p.leg_s, "%.1f s"),
                            ("rb_turn", "Turn rate while going forward (rad/s)", "f", -MAX_TURN, MAX_TURN, p.turn,
                             "%.2f rad/s"),
                            ("rb_pause", "Pause at each end (s)", "f", 0.0, 5.0, p.pause_s, "%.1f s"),
                            ("rb_cycles", "Cycles (0 = until Stop)", "i", 0, 300, p.cycles, "%d")):
                        self.text(label, "small", DIM, tag=tag + "_lbl")
                        if kind == "f":
                            dpg.add_slider_float(tag=tag, width=sw, min_value=lo, max_value=hi, default_value=val,
                                                 format=fmt, callback=self._robot_param_changed)
                        else:
                            dpg.add_slider_int(tag=tag, width=sw, min_value=lo, max_value=hi, default_value=val,
                                               format=fmt, callback=self._robot_param_changed)
            with dpg.child_window(width=right_w, height=body_h, border=False, no_scrollbar=True) as right:
                dpg.bind_item_theme(right, self.child_theme(rgb("#1B1B1A"), rgb("#1B1B1A"), pad=(0, 0), spacing=(8, GAP)))
                top = int(body_h * 0.68)
                with self.box(right_w, top):
                    self.heading("TRAJECTORY  ·  SEEN FROM ABOVE",
                                 "  ·  starts at (0, 0) facing +x (the robot's front); the way back retraces the way out")
                    with dpg.plot(width=-1, height=top - 40, no_menus=True, no_title=True, no_mouse_pos=True,
                                  equal_aspects=True) as pl:
                        dpg.add_plot_legend(location=dpg.mvPlot_Location_NorthEast)
                        self.t["rb_x"] = dpg.add_plot_axis(dpg.mvXAxis, label="x (m)")
                        with dpg.plot_axis(dpg.mvYAxis, label="y (m)") as ya:
                            self.t["rb_y"] = ya
                            for tag, label, col, w in (("rb_plan", "  Planned path", BLUE, 3.0),
                                                       ("rb_actual", "  Driven (odometry)", GREEN, 2.0)):
                                s = dpg.add_line_series([], [], label=label, tag=tag)
                                dpg.bind_item_theme(s, self.series_theme(dpg.mvLineSeries, col, 255, w))
                            for tag, label, col in (("rb_start_pt", "  Start", MUTED), ("rb_end_pt", "  Turn-around",
                                                                                         ORANGE),
                                                    ("rb_now", "  Planned position now", TEXT)):
                                s = dpg.add_scatter_series([], [], label=label, tag=tag)
                                dpg.bind_item_theme(s, self.res[f"t_rb_pt_{tag}"])
                    self.axis_font(pl)
                with self.box(right_w, body_h - top - GAP):
                    self.heading("SPEED", "  ·  commanded and measured by the robot, last 60 s")
                    with dpg.plot(width=-1, height=body_h - top - GAP - 44, no_menus=True, no_title=True,
                                  no_mouse_pos=True) as pl:
                        dpg.add_plot_legend(location=dpg.mvPlot_Location_NorthEast)
                        self.t["rb_sx"] = dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)")
                        with dpg.plot_axis(dpg.mvYAxis, label="m/s") as ya:
                            self.t["rb_sy"] = ya
                            dpg.set_axis_limits(ya, -MAX_SPEED - 0.05, MAX_SPEED + 0.05)
                            for tag, label, col in (("rb_vcmd", "  Commanded", BLUE), ("rb_vmeas", "  Measured", GREEN)):
                                s = dpg.add_line_series([], [], label=label, tag=tag)
                                dpg.bind_item_theme(s, self.series_theme(dpg.mvLineSeries, col, 255, 2.0))
                    self.axis_font(pl)
        self._robot_plan_key = None
        self._robot_param_changed()

    def robot_resources(self):
        r = self.res
        with dpg.theme() as th:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, rgb("#5A2420"))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, rgb("#7A2E28"))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, rgb(RED))
        r["t_rb_stop"] = th
        for tag, col, size in (("rb_start_pt", MUTED, 6), ("rb_end_pt", ORANGE, 6), ("rb_now", "#FFFFFF", 9)):
            with dpg.theme() as th:
                with dpg.theme_component(dpg.mvScatterSeries):
                    dpg.add_theme_color(dpg.mvPlotCol_MarkerFill, rgb(col), category=dpg.mvThemeCat_Plots)
                    dpg.add_theme_color(dpg.mvPlotCol_MarkerOutline, rgb(col), category=dpg.mvThemeCat_Plots)
                    dpg.add_theme_style(dpg.mvPlotStyleVar_Marker, dpg.mvPlotMarker_Circle, category=dpg.mvThemeCat_Plots)
                    dpg.add_theme_style(dpg.mvPlotStyleVar_MarkerSize, size, category=dpg.mvThemeCat_Plots)
            r[f"t_rb_pt_{tag}"] = th

    # ------------------------------------------------------------------ parameters -> plan
    def _robot_params(self) -> RobotParams:
        return RobotParams(pattern="arc" if dpg.get_value("rb_pattern") == "Arc" else "line",
                           speed=dpg.get_value("rb_speed"), leg_s=dpg.get_value("rb_leg"),
                           pause_s=dpg.get_value("rb_pause"), cycles=dpg.get_value("rb_cycles"),
                           turn=dpg.get_value("rb_turn")).clamp()

    def _robot_param_changed(self, *_):
        if not dpg.does_item_exist("rb_speed"):
            return
        p = self._robot_params()
        if not self.src.robot.running():
            self.src.robot.params = p
        for tag in ("rb_turn", "rb_turn_lbl"):  # only the arc turns
            dpg.configure_item(tag, show=p.pattern == "arc")
        self._draw_plan(self.src.robot.params if self.src.robot.running() else p)

    def _draw_plan(self, p: RobotParams):
        path = expected_path(p)
        dpg.set_value("rb_plan", [path[:, 0].tolist(), path[:, 1].tolist()])
        dpg.set_value("rb_start_pt", [[0.0], [0.0]])
        dpg.set_value("rb_end_pt", [[float(path[-1, 0])], [float(path[-1, 1])]])
        lo, hi = path.min(axis=0), path.max(axis=0)
        span = max(float(np.max(hi - lo)), 0.5)
        mid = (lo + hi) / 2
        pad = 0.15 * span + 0.1
        dpg.set_axis_limits(self.t["rb_x"], mid[0] - span / 2 - pad, mid[0] + span / 2 + pad)
        dpg.set_axis_limits(self.t["rb_y"], mid[1] - span / 2 - pad, mid[1] + span / 2 + pad)

    # ------------------------------------------------------------------ live
    def refresh_robot(self):
        if not dpg.does_item_exist("rb_state"):
            return
        robot = self.src.robot
        v = robot.view()
        up, can_text = (True, "dry run: no robot, no CAN") if robot.dry_run else self.src.can_status()
        dpg.delete_item("rb_can_dot", children_only=True)
        c = rgb(GREEN if up else RED)
        dpg.draw_circle((6, 10), 5, color=c, fill=c, parent="rb_can_dot")
        dpg.set_value("rb_can", can_text)
        dpg.configure_item("rb_can_btn", show=not up and not robot.dry_run)
        state = PHASE_TEXT.get(v.phase, v.phase)
        if v.running and v.phase in ("forward", "backward", "pause_end", "pause_start"):
            p = robot.params
            of = f" of {p.cycles}" if p.cycles else ""
            state += f"  ·  cycle {v.cycle}{of}  ·  {v.leg_t:.1f} s"
        dpg.set_value("rb_state", state)
        dpg.configure_item("rb_state", color=rgb(RED if v.phase == "error" else (GREEN if v.running else TEXT)))
        detail = []
        if v.battery_v is not None:
            detail.append(f"battery {v.battery_v:.1f} V")
        if v.v_meas is not None:
            detail.append(f"measured {v.v_meas:+.2f} m/s, {v.w_meas or 0:+.2f} rad/s")
        if v.error:
            detail.append(v.error)
        dpg.set_value("rb_detail", "  ·  ".join(detail))
        dpg.configure_item("rb_start", enabled=not v.running and (up or robot.dry_run),
                           label="•  Running" if v.running else "Start robot")
        dpg.bind_item_theme("rb_start", self.res["t_running"] if v.running else self.res["t_off"])
        dpg.configure_item("rb_stop", enabled=v.running)
        for tag in ("rb_pattern", "rb_speed", "rb_leg", "rb_turn", "rb_pause", "rb_cycles"):
            dpg.configure_item(tag, enabled=not v.running)  # the plan in use stays fixed while driving
        if v.running and self._robot_plan_key != id(robot.params):
            self._robot_plan_key = id(robot.params)
            self._draw_plan(robot.params)
        now = v.expected_xy if v.running else None
        dpg.set_value("rb_now", [[now[0]], [now[1]]] if now else [[], []])
        a = v.actual if v.actual is not None and len(v.actual) > 1 else np.zeros((0, 2))
        dpg.set_value("rb_actual", [a[:, 0].tolist(), a[:, 1].tolist()])
        if v.speeds:
            t_end = v.speeds[-1][0]
            pts = [s for s in v.speeds if s[0] >= t_end - 60]
            t0 = pts[0][0]
            ts = [s[0] - t0 for s in pts]
            dpg.set_value("rb_vcmd", [ts, [s[1] for s in pts]])
            dpg.set_value("rb_vmeas", [ts, [s[2] for s in pts]])
            dpg.set_axis_limits(self.t["rb_sx"], 0, max(60.0, ts[-1]))

    def handle_robot_actions(self):
        if self.state.pop("robot_can", False):
            err = self.src.bring_up_can()
            self.src.event("fail" if err else "ok", err or "CAN interface up (500 kbit/s)")
        if self.state.pop("robot_start", False):
            err = self.src.robot.start(self._robot_params())
            p = self.src.robot.params
            self.src.event("fail" if err else "busy", err or (
                f"Robot started: {'arc' if p.pattern == 'arc' else 'straight line'}, {p.speed:.2f} m/s, "
                f"{p.leg_s:.0f} s each way, {p.cycles or 'until Stop'} cycles"))
            self._robot_plan_key = None
        if self.state.pop("robot_stop", False):
            self.src.robot.stop()
            self.src.event("ok", "Robot stopped")

