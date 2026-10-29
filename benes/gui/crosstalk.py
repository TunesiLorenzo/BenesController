"""Thermal crosstalk tab: voltage scans, live plots and saved result folders."""

import json
import math
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from benes.gui.theme import ACCENT, PANEL, PLOT_MIN_SIZE, POWER_METER_COLOR, TEXT
from benes.gui.widgets import VerticalScrolledFrame
from benes.hardware import osa
from benes.hardware.hp8152a import HP8152A, RANGES_DBM
from benes.measurements.calibration_scan import (ScanPoint, scan_switches_with_reader,
                                                 scan_voltages, spectrum_scan_point)
from benes.measurements.crosstalk_results import (PLOT_X_POWER, PLOT_X_VOLTAGE, RESCAN_STYLES,
                                                  create_result_folder, load_scan_folder, plot_x_values,
                                                  save_scan_artifacts, scan_context)
from benes.measurements.laser_calibration import CalibrationStopped
from benes.measurements.mzi_characterization import (DEFAULT_SCREEN_MIN_PHASE_RAD, SCREEN_PATTERN, CrosstalkRun,
                                                     create_crosstalk_folder, crosstalk_marks, draw_kappa_heatmap,
                                                     draw_phase_shifts, heater_drive, kappa_matrix, mw_to_dbm,
                                                     run_crosstalk, save_crosstalk, scan_with_detector)
from benes.measurements.mzi_probing import DEFAULT_MIN_LEAK_ORDER, ENOUGH_LEAK_ORDER, plan_crosstalk
from benes.measurements.spectrum_export import write_crosstalk_csv
from benes.paths import app_dir


class CrosstalkMixin:
    """Mixed into ``BenesApp``; relies on the state it sets up."""

    def build_crosstalk_tab(self):
        """Sweep settings, the automatic matrix and single-switch scans beside the live plot area."""
        controls_column = ttk.Frame(self.crosstalk_tab, padding=(18, 18, 8, 18))
        controls_column.pack(side="left", fill="y")
        # The controls scroll on their own, so the plots keep the full height.
        control_scroll = VerticalScrolledFrame(controls_column)
        control_scroll.pack(fill="both", expand=True)
        controls = ttk.Frame(control_scroll.content, padding=(0, 0, 12, 0))
        controls.pack(anchor="nw", fill="x")
        ttk.Label(controls, text="THERMAL CROSSTALK", foreground=ACCENT,
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.crosstalk_status = tk.StringVar(value="Connect the voltage board and select a measurement source.")
        ttk.Label(controls, textvariable=self.crosstalk_status, wraplength=270).pack(anchor="w", pady=(4, 10))

        sweep = ttk.LabelFrame(controls, text="Sweep (both scans)", padding=8)
        sweep.pack(fill="x")
        self.crosstalk_fields = {}
        for row, (label, default) in enumerate((("Lower limit [V]", "0"), ("Upper limit [V]", "5"),
                                                 ("Step [V]", "0.5"), ("Settle [ms]", "50"))):
            ttk.Label(sweep, text=label).grid(row=row, column=0, sticky="w", pady=3)
            value = tk.StringVar(value=default)
            ttk.Entry(sweep, textvariable=value, width=12).grid(row=row, column=1, sticky="w", padx=(10, 0), pady=3)
            self.crosstalk_fields[label] = value
        self.crosstalk_comment_var = tk.StringVar()
        ttk.Label(sweep, text="Comment").grid(row=4, column=0, sticky="w", pady=3)
        ttk.Entry(sweep, textvariable=self.crosstalk_comment_var, width=19).grid(row=4, column=1, sticky="w",
                                                                                 padx=(10, 0), pady=3)
        self.build_crosstalk_matrix_frame(controls)

        scan_controls = ttk.LabelFrame(controls, text="Single-switch scan (manual route)", padding=8)
        scan_controls.pack(fill="x", pady=(12, 0))
        ttk.Label(scan_controls, text="Switches (Ctrl or Shift for multiple)").grid(row=0, column=0, columnspan=2, sticky="w")
        self.crosstalk_switches = tk.Listbox(scan_controls, selectmode="extended", exportselection=False,
                                             height=len(self.network.switches), width=18, bg=PANEL, fg=TEXT,
                                             selectbackground="#315b78", selectforeground="white")
        for name in self.network.switches:
            self.crosstalk_switches.insert("end", name)
        self.crosstalk_switches.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 14))
        # Both scans share the detection chosen in MZI calibration; the matrix frame sets this text.
        ttk.Label(scan_controls, textvariable=self.crosstalk_detection_var, foreground="#9baec6",
                  wraplength=250).grid(row=2, column=0, columnspan=2, sticky="w", pady=3)
        self.hp_meter_frame = ttk.LabelFrame(scan_controls, text="HP 8152A GPIB settings", padding=8)
        self.hp_meter_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(5, 8))
        for row, (label, variable) in enumerate((
                ("VISA resource", self.hp_meter_resource_var),
                ("Channel", self.hp_meter_channel_var),
                ("Wavelength [nm]", self.hp_meter_wavelength_var),
                ("Range [dBm]", self.hp_meter_range_var))):
            ttk.Label(self.hp_meter_frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
            if label == "Channel":
                field = ttk.Combobox(self.hp_meter_frame, textvariable=variable, values=("A", "B"),
                                     state="readonly", width=14)
            elif label == "Range [dBm]":
                field = ttk.Combobox(self.hp_meter_frame, textvariable=variable,
                                     values=("Auto",) + tuple(map(str, RANGES_DBM)),
                                     state="readonly", width=14)
            else:
                field = ttk.Entry(self.hp_meter_frame, textvariable=variable, width=19)
            field.grid(row=row, column=1, sticky="w", padx=(8, 0), pady=2)
        self.hp_meter_test_button = ttk.Button(self.hp_meter_frame, text="Test reading and GPIB read rate",
                                                command=self.test_hp_meter)
        self.hp_meter_test_button.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Label(self.hp_meter_frame, text="Meter filter: 8 Hz off, 2 Hz on; GPIB read rate is not optical bandwidth.",
                  wraplength=225).grid(row=5, column=0, columnspan=2, sticky="w", pady=(5, 0))
        ttk.Label(scan_controls, text="Uses the input and output selected in Routing. Power meter (HP, Arduino or Arduino + HP BNC): only that input's laser is lit and the JDSU follows the output. OSA: peak and mean in the input's wavelength window. Each point is taken after settling; switches return to 0 V.", wraplength=250, foreground="#9baec6").grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 8))
        self.crosstalk_button = ttk.Button(scan_controls, text="Scan selected switches", command=self.start_crosstalk_scan)
        self.crosstalk_button.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(4, 6))
        self.crosstalk_export_button = ttk.Button(scan_controls, text="Export CSV with comment", command=self.export_crosstalk_csv, state="disabled")
        self.crosstalk_export_button.grid(row=6, column=0, columnspan=2, sticky="ew")

        plot_area = ttk.Frame(self.crosstalk_tab, padding=(8, 18, 18, 18))
        plot_area.pack(side="left", fill="both", expand=True)
        plot_bar = ttk.Frame(plot_area)
        plot_bar.pack(side="top", fill="x", pady=(0, 6))
        self.open_results_button = ttk.Button(plot_bar, text="Open results…", command=self.open_results_folder)
        self.open_results_button.pack(side="left", padx=(0, 16))
        ttk.Label(plot_bar, text="X axis").pack(side="left")
        self.plot_x_axis_var = tk.StringVar(value="Voltage [V]")
        plot_x_box = ttk.Combobox(plot_bar, textvariable=self.plot_x_axis_var,
                                  values=("Voltage [V]", "Heater power [mW]"), state="readonly", width=18)
        plot_x_box.pack(side="left", padx=(8, 10))
        plot_x_box.bind("<<ComboboxSelected>>", lambda event: self.replot_crosstalk_x_axis())
        ttk.Label(plot_bar, text="Heater power = I²·R heater with I = V / R total, set per pin in MZI / pin mapping. "
                  "Control stays in volts.", foreground="#9baec6", wraplength=420).pack(side="left")
        self.crosstalk_plot_frame = ttk.Frame(plot_area)
        self.crosstalk_plot_frame.pack(side="top", fill="both", expand=True)
        ttk.Label(self.crosstalk_plot_frame, text="Power versus voltage will appear here after a scan.").pack(anchor="center", expand=True)

    def build_crosstalk_matrix_frame(self, parent):
        """Automatic aggressor × victim scan with the MZI calibration tab's detection method."""
        frame = ttk.LabelFrame(parent, text="Crosstalk matrix (automatic)", padding=8)
        frame.pack(fill="x", pady=(12, 0))
        ttk.Label(frame, wraplength=250, text=(
            "Each victim is biased at quadrature with light through it, then every aggressor whose light "
            "cannot reach the detector is swept with the limits above. κ is the share of the aggressor's heater power the victim feels. "
            "Each probe sets its own lit input and, through the JDSU, its output.")
                  ).grid(row=0, column=0, columnspan=2, sticky="w")
        self.crosstalk_detection_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.crosstalk_detection_var, foreground="#9baec6", wraplength=250).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.crosstalk_matrix_lists = {}
        for column, label in enumerate(("Aggressors", "Victims")):
            ttk.Label(frame, text=label).grid(row=2, column=column, sticky="w", pady=(8, 2))
            listbox = tk.Listbox(frame, selectmode="extended", exportselection=False, width=9,
                                 height=len(self.network.switches), bg=PANEL, fg=TEXT,
                                 selectbackground="#315b78", selectforeground="white")
            for name in self.network.switches:
                listbox.insert("end", name)
            listbox.selection_set(0, "end")
            listbox.grid(row=3, column=column, sticky="w", padx=(0, 8))
            self.crosstalk_matrix_lists[label] = listbox
        self.crosstalk_reference_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="Path reference sweep (victim bright)",
                        variable=self.crosstalk_reference_var).grid(row=4, column=0, columnspan=2, sticky="w",
                                                                    pady=(6, 0))
        ttk.Label(frame, foreground="#9baec6", wraplength=250, text=(
            "Sweeps each aggressor again with the victim at its bright extreme and divides it out, removing "
            "heating of the other MZIs on the path. Doubles the aggressor sweeps.")).grid(
            row=5, column=0, columnspan=2, sticky="w")
        self.crosstalk_adjacent_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="Adjacent MZIs only (same or next stage)",
                        variable=self.crosstalk_adjacent_var).grid(row=6, column=0, columnspan=2, sticky="w",
                                                                   pady=(6, 0))
        ttk.Label(frame, text="Min. leak order").grid(row=7, column=0, sticky="w", pady=(6, 0))
        self.crosstalk_leak_order_var = tk.StringVar(value=str(DEFAULT_MIN_LEAK_ORDER))
        ttk.Combobox(frame, textvariable=self.crosstalk_leak_order_var, state="readonly", width=5,
                     values=tuple(str(order) for order in range(1, ENOUGH_LEAK_ORDER + 1))).grid(
            row=7, column=1, sticky="w", pady=(6, 0))
        ttk.Label(frame, foreground="#9baec6", wraplength=250, text=(
            "Held MZIs that must leak before light through an aggressor reaches the detector. Each leak "
            "weakens that light by the MZI's extinction; one leak still moves the victim reading by tenths "
            "of a radian. Each aggressor gets the best-isolated probe; pairs below the minimum are marked ×. "
            "Curves that follow the aggressor's own switching are marked path and get no κ.")).grid(
            row=8, column=0, columnspan=2, sticky="w")
        self.crosstalk_pretest_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="Pretest: skip aggressors that do not move the victim",
                        variable=self.crosstalk_pretest_var).grid(row=9, column=0, columnspan=2, sticky="w",
                                                                  pady=(6, 0))
        ttk.Label(frame, text="Min. victim shift [mrad]").grid(row=10, column=0, sticky="w", pady=(2, 0))
        self.crosstalk_pretest_mrad_var = tk.StringVar(value=f"{DEFAULT_SCREEN_MIN_PHASE_RAD * 1000:g}")
        ttk.Entry(frame, textvariable=self.crosstalk_pretest_mrad_var, width=7).grid(
            row=10, column=1, sticky="w", pady=(2, 0))
        ttk.Label(frame, foreground="#9baec6", wraplength=250, text=(
            f"With the victim at quadrature, each aggressor is swung between the limits above "
            f"{len(SCREEN_PATTERN)} times (and again with the victim bright when the path reference is on). "
            "Pairs whose step stays below the minimum or the noise are marked n.s. and not swept.")).grid(
            row=11, column=0, columnspan=2, sticky="w")
        ttk.Button(frame, text="Plan", command=self.show_crosstalk_plan).grid(
            row=12, column=0, sticky="ew", pady=(8, 0), padx=(0, 4))
        self.crosstalk_matrix_button = ttk.Button(frame, text="Run matrix", style="Accent.TButton",
                                                  command=self.start_crosstalk_matrix)
        self.crosstalk_matrix_button.grid(row=12, column=1, sticky="ew", pady=(8, 0))
        self.crosstalk_stop_button = ttk.Button(frame, text="Stop", state="disabled",
                                                command=self.stop_crosstalk_matrix)
        self.crosstalk_stop_button.grid(row=13, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.crosstalk_plan_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.crosstalk_plan_var, wraplength=250).grid(
            row=14, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.crosstalk_stop_event = None
        self.crosstalk_scan_setup = None
        for variable in (self.detection_method_var, self.detection_meter_var, self.detection_position_var):
            variable.trace_add("write", lambda *_: self.update_crosstalk_detection())
        # The summary names the WDM lines, whose fields the dataset tab builds later.
        self.after_idle(self.update_crosstalk_detection)

    def update_crosstalk_detection(self):
        self.crosstalk_detection_var.set(f"Detection: {self.detection_summary()} (set in MZI calibration).")

    def crosstalk_matrix_selection(self):
        return tuple([listbox.get(index) for index in listbox.curselection()]
                     for listbox in (self.crosstalk_matrix_lists["Aggressors"], self.crosstalk_matrix_lists["Victims"]))

    def crosstalk_plan(self, inputs=None, outputs=None):
        aggressors, victims = self.crosstalk_matrix_selection()
        if not aggressors or not victims:
            raise ValueError("Select at least one aggressor and one victim")
        if inputs is None:
            inputs, outputs = self.detection_ports()
        return plan_crosstalk(self.network, victims, aggressors, inputs, outputs, known=self.calibrated_switches(),
                              max_stage_distance=1 if self.crosstalk_adjacent_var.get() else None,
                              min_leak_order=int(self.crosstalk_leak_order_var.get()))

    def describe_crosstalk_plan(self, plan):
        text = (f"{plan.pair_count} pair(s) in {len(plan.groups)} probe(s), "
                f"{len({group.probe.name for group in plan.groups})} victim(s).")
        orders = [order for group in plan.groups for order in group.leak_orders]
        if orders:
            isolated = sum(order is None for order in orders)
            leaky = [order for order in orders if order is not None]
            text += f" {isolated} pair(s) optically isolated"
            text += f", the rest ≥ {min(leaky)} leak(s)." if leaky else "."
        if plan.distant:
            text += f" {len(plan.distant)} pair(s) more than one stage apart left out."
        if plan.uncovered:
            reasons = sorted(set(plan.uncovered.values()))
            text += f" {len(plan.uncovered)} pair(s) not measurable: " + "; ".join(reasons[:4])
            if len(reasons) > 4:
                text += "; …"
        return text

    def show_crosstalk_plan(self):
        try:
            plan = self.crosstalk_plan()
        except ValueError as error:
            messagebox.showerror("Crosstalk plan", str(error), parent=self)
            return
        self.crosstalk_plan_var.set(self.describe_crosstalk_plan(plan))

    def stop_crosstalk_matrix(self):
        if self.crosstalk_stop_event is not None:
            self.crosstalk_stop_event.set()
            self.crosstalk_status.set("Stopping after the current point; every MZI will return to 0 V...")

    def start_crosstalk_matrix(self):
        try:
            if self.crosstalk_running or self.mzi_calibration_running:
                raise RuntimeError("Wait for the current scan or calibration to finish")
            setup = self.detection_setup()
            plan = self.crosstalk_plan(setup["inputs"], setup["outputs"])
            if not plan.groups:
                raise ValueError("No selected pair is measurable with this detection. "
                                 + self.describe_crosstalk_plan(plan))
            fields = self.crosstalk_fields
            voltages = scan_voltages(*(float(fields[key].get()) for key in
                                       ("Lower limit [V]", "Upper limit [V]", "Step [V]")))
            settle_ms = float(fields["Settle [ms]"].get())
            if not 0 <= settle_ms <= 10000:
                raise ValueError("Settling time must be between 0 and 10000 ms")
            screen_min_phase = None
            if self.crosstalk_pretest_var.get():
                screen_min_phase = float(self.crosstalk_pretest_mrad_var.get()) / 1000
                if not 0 <= screen_min_phase <= math.pi:
                    raise ValueError("The pretest minimum shift must be between 0 and 3142 mrad")
            swept = sorted({group.probe.name for group in plan.groups} |
                           {name for group in plan.groups for name in group.aggressors})
            self.confirm_voltage_sweep(voltages, names=swept)
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  # noqa: F401
        except (ImportError, OSError, RuntimeError, ValueError, tk.TclError) as error:
            messagebox.showerror("Crosstalk matrix unavailable", str(error), parent=self)
            return

        reference = self.crosstalk_reference_var.get()
        settle_s = settle_ms / 1000
        context = {
            "kind": "crosstalk_matrix", **setup["context"],
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "comment": self.crosstalk_comment_var.get().strip(), "unit": setup["unit"],
            "voltages_v": list(voltages), "settle_s": settle_s, "path_reference": reference,
            "victim_bias": "swept point nearest quadrature of the victim's fitted cos² response",
            "max_stage_distance": 1 if self.crosstalk_adjacent_var.get() else None,
            "min_leak_order": int(self.crosstalk_leak_order_var.get()),
            "distant_pairs_left_out": [f"{aggressor}->{victim}" for aggressor, victim in plan.distant],
            "pretest_min_phase_rad": screen_min_phase,
            "background": "MZIs off the probe at their calibrated cross voltage (0 V if uncalibrated), "
                          "or calibrated bar where the probe needs it",
            "not_measurable": {f"{aggressor}->{victim}": reason
                               for (aggressor, victim), reason in plan.uncovered.items()},
            "switch_pins": dict(self.controller.settings["mapping"]),
            "calibration": {name: dict(value) for name, value in self.controller.settings["calibration"].items()},
            "heater_resistance_ohm": {name: self.controller.heater_resistance(name) for name in self.network.switches},
            "total_resistance_ohm": {name: self.controller.total_resistance(name) for name in self.network.switches},
        }
        per_aggressor = len(voltages) * (2 if reference else 1)
        per_screen = 0 if screen_min_phase is None else len(SCREEN_PATTERN) * (2 if reference else 1)
        total = sum(len(voltages) + len(group.aggressors) * (per_screen + per_aggressor) for group in plan.groups)
        self.crosstalk_running = True
        self.active_scan_kind = "Crosstalk matrix"
        self.crosstalk_stop_event = stop_event = threading.Event()
        self.begin_activity("crosstalk", f"Crosstalk matrix: {plan.pair_count} pairs", total)
        self.set_crosstalk_matrix_controls(True)
        self.claim_detection(setup, True)
        self.crosstalk_plan_var.set(self.describe_crosstalk_plan(plan))
        self.crosstalk_status.set(f"Crosstalk matrix: {plan.pair_count} pairs with {self.detection_summary()}...")
        run = CrosstalkRun()
        self.prepare_crosstalk_matrix_plots(setup["unit"], plan, run)
        done = [0]

        def on_point(name, index, count, point):
            done[0] += 1
            self.after(0, lambda value=done[0]: self.update_activity(
                "crosstalk", value, detail=f"Crosstalk matrix: sweeping {name} {index}/{count} "
                                           f"({point.voltage:.2f} V)"))

        def on_screen(screen):
            if not screen.moved:
                # The skipped sweeps count as done, so the bar keeps its pace.
                done[0] += per_aggressor
            self.after(0, lambda: self.crosstalk_screen_done(screen, run))

        def on_step(group, index, count):
            self.after(0, lambda: self.crosstalk_status.set(
                f"Victim {group.probe.name} ({index}/{count}): {group.probe.describe()}; "
                f"aggressors {', '.join(group.aggressors)}"))

        def worker():
            error, stopped, folder, save_error = None, False, None, None
            try:
                detector, close = setup["open"]()
                try:
                    run_crosstalk(self.controller, detector, self.network, plan.groups, voltages, settle_s,
                                  reference, run, on_step, on_point,
                                  lambda victim, bias: self.after(0, lambda: self.crosstalk_victim_done(victim, bias)),
                                  lambda pair: self.after(0, lambda: self.crosstalk_pair_done(pair, run)),
                                  stop_event, screen_min_phase_rad=screen_min_phase, on_screen=on_screen,
                                  on_failed=lambda group, reason: self.after(
                                      0, lambda: self.crosstalk_victim_failed(group, reason, run)))
                finally:
                    close()
            except CalibrationStopped:
                stopped = True
            except Exception as failure:
                error = str(failure)
            if run.victims:
                try:
                    folder = create_crosstalk_folder(app_dir(), "matrix")
                    save_crosstalk(folder, {**context, "status": "stopped" if stopped else
                                            "failed" if error else "complete", "error": error},
                                   run, self.network, plan)
                except Exception as failure:
                    save_error = str(failure)
            self.after(0, lambda: self.finish_crosstalk_matrix(setup, run, error, stopped, folder, save_error))

        threading.Thread(target=worker, daemon=True).start()

    def set_crosstalk_matrix_controls(self, running):
        state = "disabled" if running else "normal"
        for widget in (self.crosstalk_button, self.crosstalk_matrix_button, self.open_results_button):
            widget.configure(state=state)
        self.crosstalk_stop_button.configure(state="normal" if running else "disabled")
        for tab in (self.route_tab, self.hardware_tab, self.calibration_tab, self.dataset_tab):
            self.set_tab_state(tab, state)
        self.set_osa_reading(running)

    def finish_crosstalk_matrix(self, setup, run, error, stopped, folder, save_error):
        self.crosstalk_running = False
        self.crosstalk_stop_event = None
        self.claim_detection(setup, False)
        self.set_crosstalk_matrix_controls(False)
        self.finish_activity("crosstalk", "Crosstalk matrix failed" if error or save_error else
                             "Crosstalk matrix stopped" if stopped else "Crosstalk matrix complete")
        # Every MZI is back at 0 V, which is cross.
        self.network.states = dict.fromkeys(self.network.switches, True)
        self.zero_volt_switches = set(self.network.switches)
        for name in self.network.switches:
            self.voltage_vars[name].set("0.00")
        self.refresh(keep_selection=True)
        detail = f"{len(run.pairs)} pair(s) measured"
        if run.screens:
            detail += f", {len(run.static_pairs)} of {len(run.screens)} pretested pair(s) did not move"
        if run.failed:
            victims = sorted({victim for _, victim in run.failed})
            detail += f", victim probe failed for {', '.join(victims)} ({len(run.failed)} pair(s) marked !)"
        detail += "" if folder is None else f". Results: {folder}"
        if error:
            self.crosstalk_status.set(f"Crosstalk matrix failed: {error}. {detail}")
            messagebox.showerror("Crosstalk matrix failed", error, parent=self)
        elif stopped:
            self.crosstalk_status.set(f"Crosstalk matrix stopped; every MZI is back at 0 V. {detail}")
        else:
            self.crosstalk_status.set(f"Crosstalk matrix complete. {detail}")
        if save_error:
            messagebox.showerror("Crosstalk matrix not saved", save_error, parent=self)

    def prepare_crosstalk_matrix_plots(self, unit, plan, run):
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        from matplotlib.figure import Figure

        for child in self.crosstalk_plot_frame.winfo_children():
            child.destroy()
        self.crosstalk_plots = ttk.Notebook(self.crosstalk_plot_frame)
        self.crosstalk_plots.pack(fill="both", expand=True)
        self.crosstalk_plot_items = {}
        self.crosstalk_data = {}
        self.crosstalk_export_button.configure(state="disabled")
        self.crosstalk_matrix_unit = unit
        self.crosstalk_matrix_plan = plan
        self.plot_resistance_override = {}
        tab = ttk.Frame(self.crosstalk_plots)
        self.crosstalk_plots.add(tab, text="κ matrix")
        figure = Figure(figsize=(7, 6))
        canvas = FigureCanvasTkAgg(figure, master=tab)
        NavigationToolbar2Tk(canvas, tab).update()
        canvas.get_tk_widget().configure(**PLOT_MIN_SIZE)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        self.crosstalk_matrix_canvas = canvas

        # One tab for every victim, so twelve victims do not widen the tab strip past the window.
        tab = ttk.Frame(self.crosstalk_plots)
        self.crosstalk_plots.add(tab, text="Victims")
        bar = ttk.Frame(tab)
        bar.pack(side="top", fill="x", pady=(4, 4))
        ttk.Label(bar, text="Victim").pack(side="left")
        self.crosstalk_victim_var = tk.StringVar()
        self.crosstalk_victim_box = ttk.Combobox(bar, textvariable=self.crosstalk_victim_var, state="readonly",
                                                 width=8, values=())
        self.crosstalk_victim_box.pack(side="left", padx=(8, 12))
        self.crosstalk_victim_box.bind("<<ComboboxSelected>>",
                                       lambda event: self.draw_crosstalk_victim(self.crosstalk_victim_var.get()))
        self.crosstalk_follow_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Follow the running victim", variable=self.crosstalk_follow_var).pack(side="left")
        figure = Figure(figsize=(9, 4.5))
        canvas = FigureCanvasTkAgg(figure, master=tab)
        NavigationToolbar2Tk(canvas, tab).update()
        canvas.get_tk_widget().configure(**PLOT_MIN_SIZE)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        self.crosstalk_victim_canvas = canvas
        self.crosstalk_victim_plots = {}
        self.draw_crosstalk_matrix(run)

    def draw_crosstalk_matrix(self, run):
        figure = self.crosstalk_matrix_canvas.figure
        figure.clear()
        draw_kappa_heatmap(figure.add_subplot(111), self.network, kappa_matrix(self.network, run.pairs),
                           crosstalk_marks(run, self.crosstalk_matrix_plan))
        figure.tight_layout(rect=(0, 0.04, 1, 1))
        self.crosstalk_matrix_canvas.draw_idle()

    def draw_crosstalk_victim(self, name):
        """Left: each probe's victim sweep, fit and quadrature bias. Right: phase shift per aggressor."""
        from matplotlib import colormaps

        plot = self.crosstalk_victim_plots.get(name)
        if plot is None:
            return
        self.crosstalk_victim_var.set(name)
        figure = self.crosstalk_victim_canvas.figure
        figure.clear()
        response, shifts = figure.subplots(1, 2, gridspec_kw={"width_ratios": (1, 1.25)})
        decibels = self.crosstalk_matrix_unit == "mW"

        def convert(power):
            value = mw_to_dbm(power) if decibels else power
            return float("nan") if value is None else value

        colors = colormaps["tab10"].colors
        for index, (victim, bias) in enumerate(plot["probes"]):
            color = colors[index % len(colors)]
            probe = victim.probe
            voltages = [point.voltage for point in victim.points]
            response.plot(self.plot_x(name, voltages)[0], [convert(point.power) for point in victim.points], ".",
                          color=color, markersize=4, label=f"In {probe.input + 1} → Out {probe.output + 1}")
            if victim.fit is not None:
                dense = [voltages[0] + step * (voltages[-1] - voltages[0]) / 200 for step in range(201)]
                drive = [heater_drive(self.controller, name, voltage)[0] for voltage in dense]
                response.plot(self.plot_x(name, dense)[0], [convert(max(victim.fit.model(x), 1e-12)) for x in drive],
                              "-", color=color, linewidth=1, alpha=0.7)
            bright = victim.cross if probe.bright_crossed else victim.bar
            response.plot(self.plot_x(name, [bias.voltage])[0], [convert(bias.power)], "o", color=color,
                          markersize=8, markerfacecolor="none", markeredgewidth=1.6)
            response.plot(self.plot_x(name, [bright.voltage])[0], [convert(bright.power)], "^", color=color,
                          markersize=7, markerfacecolor="none", markeredgewidth=1.4)
        response.plot([], [], "o", color="gray", markerfacecolor="none", label="Quadrature bias")
        response.plot([], [], "^", color="gray", markerfacecolor="none", label="Bright (path reference)")
        label = self.plot_x(name, [])[1]
        response.set(xlabel=f"Victim {label[0].lower()}{label[1:]}",
                     ylabel=f"Power [{'dBm' if decibels else self.crosstalk_matrix_unit}]",
                     title=f"{name}: victim sweep, line = cos² fit")
        response.grid(True, alpha=0.4)
        response.legend(fontsize=7)
        draw_phase_shifts(shifts, name, plot["pairs"])
        figure.tight_layout()
        self.crosstalk_victim_canvas.draw_idle()

    def show_crosstalk_victim(self, name):
        """Redraw ``name`` if it is on screen, or switch to it while following the run."""
        if self.crosstalk_follow_var.get() or self.crosstalk_victim_var.get() in ("", name):
            self.draw_crosstalk_victim(name)

    def crosstalk_victim_done(self, victim, bias):
        plot = self.crosstalk_victim_plots.setdefault(victim.name, {"probes": [], "pairs": []})
        plot["probes"].append((victim, bias))
        self.crosstalk_victim_box.configure(values=[name for name in self.network.switches
                                                    if name in self.crosstalk_victim_plots])
        self.show_crosstalk_victim(victim.name)

    def crosstalk_victim_failed(self, group, reason, run):
        self.crosstalk_status.set(f"Victim {group.probe.name} skipped ({group.probe.describe()}): {reason}")
        self.draw_crosstalk_matrix(run)

    def crosstalk_screen_done(self, screen, run):
        shift = "—" if screen.phase_shift_rad is None else f"{screen.phase_shift_rad * 1000:.1f} mrad"
        noise = "" if screen.resolution_rad is None else f", noise limit {screen.resolution_rad * 1000:.1f} mrad"
        verdict = "moves it; sweeping" if screen.moved else "no movement; not swept (n.s.)"
        self.crosstalk_status.set(f"Pretest {screen.aggressor} → {screen.victim}: {shift}{noise}, {verdict}")
        if not screen.moved:
            self.draw_crosstalk_matrix(run)

    def crosstalk_pair_done(self, pair, run):
        self.draw_crosstalk_matrix(run)
        if pair.contamination:
            self.crosstalk_status.set(f"{pair.aggressor} → {pair.victim}: no κ, {pair.contamination}")
        else:
            kappa = "—" if pair.kappa is None else f"{pair.kappa * 100:.2f} %"
            self.crosstalk_status.set(f"{pair.aggressor} → {pair.victim}: κ = {kappa} "
                                      f"(bias {pair.bias_voltage:.2f} V)")
        plot = self.crosstalk_victim_plots.get(pair.victim)
        if plot is not None:
            plot["pairs"].append(pair)
            self.show_crosstalk_victim(pair.victim)

    def hp_meter_options(self):
        resource = self.hp_meter_resource_var.get().strip()
        channel = self.hp_meter_channel_var.get()
        range_text = self.hp_meter_range_var.get()
        try:
            wavelength = float(self.hp_meter_wavelength_var.get())
        except ValueError as error:
            raise ValueError("Enter the HP meter wavelength in nm") from error
        if not resource:
            raise ValueError("Enter the HP meter VISA resource, for example GPIB0::23::INSTR")
        if channel not in ("A", "B"):
            raise ValueError("Select HP meter channel A or B")
        if not math.isfinite(wavelength) or not 400 <= wavelength <= 1700:
            raise ValueError("HP meter wavelength must be between 400 and 1700 nm")
        if range_text != "Auto" and range_text not in tuple(map(str, RANGES_DBM)):
            raise ValueError("Select Auto or a fixed HP meter range from the list")
        options = {"resource": resource, "channel": channel,
                   "wavelength_nm": wavelength,
                   "range": range_text}
        (app_dir() / "benes_hp8152a_config.json").write_text(
            json.dumps(options, indent=2) + "\n", encoding="utf-8")
        return options

    @staticmethod
    def open_hp_meter(options):
        meter = HP8152A(options["resource"])
        try:
            meter.connect()
            CrosstalkMixin.configure_hp_meter(meter, options)
        except Exception:
            meter.disconnect()
            raise
        return meter

    @staticmethod
    def configure_hp_meter(meter, options):
        meter.configure(1 if options["channel"] == "A" else 2,
                        options["wavelength_nm"],
                        None if options["range"] == "Auto" else int(options["range"]))

    def acquire_hp_meter(self, options, *, configure=True):
        """Borrow the connected HP session, or open one for a single job."""
        session = self.hp_meter_session
        if session is not None:
            saved = self.hp_meter_session_options
            if options["resource"] != saved["resource"]:
                raise ValueError("Disconnect the HP 8152A before changing its GPIB resource")
            if not configure and options != saved:
                raise ValueError("Disconnect and reconnect the HP 8152A after changing its settings")
            if configure:
                self.configure_hp_meter(session, options)
            return session, lambda: self.configure_hp_meter(session, saved)
        if configure:
            meter = self.open_hp_meter(options)
        else:
            meter = HP8152A(options["resource"])
            try:
                meter.connect()
            except Exception:
                meter.disconnect()
                raise
        return meter, meter.disconnect

    def test_hp_meter(self):
        if self.crosstalk_running or self.hp_meter_busy:
            self.crosstalk_status.set("HP meter is in use; stop the stream or current check first.")
            return
        try:
            options = self.hp_meter_options()
        except (OSError, ValueError) as error:
            messagebox.showerror("HP meter settings", str(error), parent=self)
            return
        self.hp_meter_test_button.configure(state="disabled")
        self.hp_meter_busy = "test"
        self.refresh_hp_connection_buttons()
        self.begin_activity("hp_meter_test", "Checking HP meter GPIB read rate...")
        self.crosstalk_status.set("Checking 12 HP meter readings over GPIB...")

        def worker():
            meter = None
            release = None
            result, error = None, None
            try:
                meter, release = self.acquire_hp_meter(options)
                result = meter.sample_rate(12)
            except Exception as failure:
                error = str(failure)
            finally:
                if release is not None:
                    try:
                        release()
                    except Exception as restore_error:
                        error = error or f"Could not restore HP meter settings: {restore_error}"
            self.after(0, lambda result=result, error=error:
                       self.finish_hp_meter_test(result=result, error=error))

        threading.Thread(target=worker, daemon=True).start()

    def finish_hp_meter_test(self, result=None, error=None):
        self.hp_meter_busy = None
        self.refresh_hp_connection_buttons()
        self.hp_meter_test_button.configure(state="normal")
        if error:
            self.finish_activity("hp_meter_test", "HP meter check failed")
            self.crosstalk_status.set(f"HP meter check failed: {error}")
            messagebox.showerror("HP meter check failed", error, parent=self)
            return
        valid = result["valid_values_dbm"]
        detail = (f"HP meter: {result['rate_hz']:.1f} GPIB reads/s; "
                  f"{len(valid)}/{result['samples']} numeric readings")
        if valid:
            detail += f"; latest {valid[-1]:.2f} dBm"
        else:
            detail += ". Check optical input or range before scanning."
        self.crosstalk_status.set(detail)
        self.finish_activity("hp_meter_test", detail)


    def start_crosstalk_scan(self):
        names = [self.crosstalk_switches.get(index) for index in self.crosstalk_switches.curselection()]
        setup = None
        try:
            if self.measurement_busy():
                raise RuntimeError("Wait for the current measurement to finish")
            if not names:
                raise ValueError("Select at least one switch")
            if self.source is None or self.destination is None:
                raise ValueError("Select an input and output in Routing before starting a thermal crosstalk scan")
            fields = self.crosstalk_fields
            voltages = scan_voltages(*(float(fields[key].get()) for key in
                                       ("Lower limit [V]", "Upper limit [V]", "Step [V]")))
            settle_ms = float(fields["Settle [ms]"].get())
            if not 0 <= settle_ms <= 10000:
                raise ValueError("Settling time must be between 0 and 10000 ms")
            if not self.controller.connected or self.controller.settings["dry_run"]:
                raise RuntimeError("Connect the physical voltage board first")
            if self.power_meter_method():
                setup = self.detection_setup()
                if self.destination not in setup["outputs"]:
                    raise ValueError(f"The power meter sits on output {setup['outputs'][0] + 1}; select that output "
                                     "in Routing, or read the meter through the JDSU (MZI calibration)")
                source = setup["context"]["meter"]
                response_unit = "dBm" if setup["unit"] == "mW" else setup["unit"]
            else:
                source = "OSA"
                wavelength_text = self.wdm_wavelength_vars[self.source + 1].get().strip()
                if not wavelength_text:
                    raise ValueError(f"Assign a WDM wavelength to input {self.source + 1} before the OSA scan")
                wavelength = float(wavelength_text)
                half_window_nm = float(self.wdm_window_var.get())
                if not math.isfinite(wavelength):
                    raise ValueError("The selected input's WDM wavelength must be finite")
                if not math.isfinite(half_window_nm) or not 0 < half_window_nm <= 10:
                    raise ValueError("Set a line window between 0 and 10 nm in the WDM comb tab")
                if self.osa_button.instate(["disabled"]):
                    raise RuntimeError("Wait for the current OSA trace read to finish")
                simulate_osa = self.osa_dry_run_var.get()
                osa_host = self.osa_host_var.get().strip()
                osa_port = int(self.osa_port_var.get()) if not simulate_osa else 0
                if not simulate_osa and (not osa_host or not 1 <= osa_port <= 65535):
                    raise ValueError("Enter a valid OSA host and port in Hardware")
                response_unit = "dBm"
            for name in names:
                if self.controller.settings["mapping"][name] is None:
                    raise ValueError(f"Assign a voltage-board pin to {name} first")
            self.confirm_voltage_sweep(voltages, names=names)
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk  # noqa: F401
            from matplotlib.figure import Figure  # noqa: F401
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Crosstalk scan unavailable", str(error), parent=self)
            return

        output_port = self.destination
        context = scan_context(
            self.network, self.source, output_port,
            output_port if setup else self.detector_port(self.power_meter_output_var),
            None if setup else self.detector_port(self.osa_output_var),
            self.controller, names, voltages, settle_ms, source, self.crosstalk_comment_var.get().strip(),
        )
        if source == "OSA":
            context.update(reference_input_port=self.source + 1, reference_wavelength_nm=wavelength,
                           reference_half_window_nm=half_window_nm)
        else:
            context.update(detection=setup["context"], response_unit=response_unit)
        try:
            result_folder = create_result_folder(app_dir(), context)
            save_scan_artifacts(result_folder, context, {}, source, response_unit, self.network,
                                status="in_progress")
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Crosstalk results unavailable", str(error), parent=self)
            return

        self.active_scan_kind = "Thermal crosstalk scan"
        self.crosstalk_running = True
        self.crosstalk_scan_setup = setup
        if setup is not None:
            self.claim_detection(setup, True)
        self.begin_activity("crosstalk", f"Thermal crosstalk: scanning {len(names)} switches",
                            len(names) * len(voltages) + 1)
        self.crosstalk_data = {}
        self.crosstalk_data_source = None
        self.crosstalk_data_unit = None
        self.crosstalk_result_folder = result_folder
        self.crosstalk_button.configure(state="disabled")
        self.crosstalk_export_button.configure(state="disabled")
        self.crosstalk_switches.configure(state="disabled")
        self.crosstalk_matrix_button.configure(state="disabled")
        self.set_tab_state(self.route_tab, "disabled")
        self.set_tab_state(self.hardware_tab, "disabled")
        self.crosstalk_status.set(f"Scanning {len(names)} switch(es), {len(voltages)} points each"
                                  + (f" at {wavelength:g} nm..." if source == "OSA" else "..."))
        self.crosstalk_reference_wavelength_nm = wavelength if source == "OSA" else None
        self.prepare_crosstalk_plots(voltages)
        data = {}

        def on_point(name, index, total, point):
            data.setdefault(name, []).append(point)
            self.after(0, lambda: self.add_crosstalk_point(name, point, index, total, source, response_unit))
            self.after(0, lambda n=name, i=index, t=total: self.update_activity(
                "crosstalk", names.index(n) * t + i,
                detail=f"Crosstalk {n}: voltage {i}/{t} ({point.voltage:.2f} V)"))

        def to_response(point):
            """A power-meter reading in the scan's unit: dBm for mW meters, else the meter's own unit."""
            power = point.power
            if response_unit == "dBm":
                power = mw_to_dbm(power)
            return ScanPoint(point.voltage, float("nan") if power is None else power)

        def replace_points(name, points):
            data[name] = points
            self.after(0, lambda: self.replace_crosstalk_points(name, points))

        def worker():
            error = None
            try:
                if setup is not None:
                    scan_with_detector(self.controller, setup, names, self.source, self.destination, voltages,
                                       settle_ms / 1000,
                                       lambda name, index, total, point: on_point(name, index, total,
                                                                                  to_response(point)),
                                       lambda name, points: replace_points(name, [to_response(point)
                                                                                  for point in points]))
                else:
                    if self.osa_output_switch.connected:
                        self.osa_output_switch.select_output(self.destination + 1)

                    def read_osa_point(voltage):
                        spectrum = osa.simulated_spectrum() if simulate_osa else osa.get_spectrum(osa_host, osa_port)
                        return spectrum_scan_point(voltage, spectrum, wavelength, half_window_nm)

                    scan_switches_with_reader(self.controller, names, voltages, settle_ms / 1000,
                                               read_osa_point, on_point)
            except Exception as failure:
                error = str(failure)
            self.after(0, lambda message=error: self.finish_crosstalk_scan(
                names, data, source, response_unit, result_folder, context, message))

        threading.Thread(target=worker, daemon=True).start()

    def finish_crosstalk_scan(self, names, data, source, response_unit, folder, context, error=None):
        for name in names:
            if self.controller.applied_voltages.get(name) == 0.0:
                self.voltage_vars[name].set("0.00")
        if data:
            self.crosstalk_data = data
            self.crosstalk_data_source = source
            self.crosstalk_data_unit = response_unit
        else:
            for child in self.crosstalk_plot_frame.winfo_children():
                child.destroy()
            ttk.Label(self.crosstalk_plot_frame, text="No measurements were collected.").pack(anchor="center", expand=True)
        self.crosstalk_status.set("Saving figures, CSV, and base path...")
        self.update_activity("crosstalk", detail="Saving crosstalk figures and data...")

        plot_x_mode = self.plot_x_mode()

        def save_results():
            try:
                final_context = {**context, "plot_x_axis": plot_x_mode}
                if error:
                    final_context["scan_error"] = error
                save_scan_artifacts(folder, final_context, data, source, response_unit, self.network,
                                    status="failed" if error else "complete")
            except Exception as save_error:
                self.after(0, lambda message=str(save_error): self.complete_crosstalk_scan(data, folder, error, message))
            else:
                self.after(0, lambda: self.complete_crosstalk_scan(data, folder, error))

        threading.Thread(target=save_results, daemon=True).start()

    def complete_crosstalk_scan(self, data, folder, scan_error=None, save_error=None):
        self.crosstalk_running = False
        if self.crosstalk_scan_setup is not None:
            self.claim_detection(self.crosstalk_scan_setup, False)
            self.crosstalk_scan_setup = None
        self.finish_activity("crosstalk", "Crosstalk scan failed" if scan_error or save_error else
                             "Crosstalk scan complete")
        self.crosstalk_button.configure(state="normal")
        self.crosstalk_switches.configure(state="normal")
        self.crosstalk_matrix_button.configure(state="normal")
        self.set_tab_state(self.route_tab, "normal")
        self.set_tab_state(self.hardware_tab, "normal")
        if data:
            self.crosstalk_export_button.configure(state="normal")
        if scan_error:
            self.crosstalk_status.set(f"{self.active_scan_kind} stopped: {scan_error}. Results: {folder}")
            messagebox.showerror(f"{self.active_scan_kind} failed", scan_error, parent=self)
        elif save_error:
            self.crosstalk_status.set(f"{self.active_scan_kind} finished, but automatic export failed: {save_error}")
        else:
            self.crosstalk_status.set(f"{self.active_scan_kind} complete. Results: {folder}")
        if save_error:
            messagebox.showerror("Crosstalk export failed", f"{save_error}\nFolder: {folder}", parent=self)

    def prepare_crosstalk_plots(self, voltages):
        for child in self.crosstalk_plot_frame.winfo_children():
            child.destroy()
        self.crosstalk_plots = ttk.Notebook(self.crosstalk_plot_frame)
        self.crosstalk_plots.pack(fill="both", expand=True)
        self.crosstalk_plot_items = {}
        self.crosstalk_victim_plots = {}
        self.crosstalk_voltage_limits = (min(voltages), max(voltages))
        # A loaded results folder plots with the resistances it was saved with.
        self.plot_resistance_override = {}

    def add_crosstalk_point(self, name, point, index, total, source, response_unit):
        from matplotlib import colormaps
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        from matplotlib.cm import ScalarMappable
        from matplotlib.colors import Normalize
        from matplotlib.figure import Figure

        plot = self.crosstalk_plot_items.get(name)
        if plot is None:
            tab = ttk.Frame(self.crosstalk_plots)
            self.crosstalk_plots.add(tab, text=name)
            self.crosstalk_plots.select(tab)
            figure = Figure(figsize=(8, 5))
            response = figure.add_subplot(121 if source == "OSA" else 111)
            line, = response.plot([], [], marker=".", linewidth=1.4, color=ACCENT,
                                  label=("Reference line peak" if self.crosstalk_reference_wavelength_nm is not None
                                         else "Peak") if source == "OSA" else "Power")
            mean_line = None
            if source == "OSA":
                mean_line, = response.plot([], [], marker=".", linewidth=1.4,
                                           color=POWER_METER_COLOR,
                                           label="Reference window mean" if self.crosstalk_reference_wavelength_nm is not None
                                           else "Mean")
                response.legend(loc="best")
            response.set(xlabel=self.plot_x(name, [])[1],
                         ylabel="Power [dBm]" if source == "OSA" else f"Power [{response_unit}]",
                         title=(f"{name}: {self.crosstalk_reference_wavelength_nm:g} nm power"
                                if source == "OSA" and self.crosstalk_reference_wavelength_nm is not None
                                else f"{name}: power response"))
            response.grid(True)
            spectra = None
            norm = None
            cmap = None
            if source == "OSA":
                spectra = figure.add_subplot(122)
                spectra.set(xlabel="Wavelength [nm]", ylabel="Power [dBm]", title=f"{name}: spectra")
                spectra.grid(True)
                if self.crosstalk_reference_wavelength_nm is not None:
                    spectra.axvline(self.crosstalk_reference_wavelength_nm, color=ACCENT, linestyle="--")
                norm = Normalize(*self.crosstalk_voltage_limits)
                cmap = colormaps["viridis"]
                figure.colorbar(ScalarMappable(norm=norm, cmap=cmap), ax=spectra, label="Voltage [V]")
            figure.tight_layout()
            canvas = FigureCanvasTkAgg(figure, master=tab)
            toolbar = NavigationToolbar2Tk(canvas, tab)
            toolbar.update()
            canvas.get_tk_widget().configure(**PLOT_MIN_SIZE)
            canvas.get_tk_widget().pack(fill="both", expand=True)
            plot = {"canvas": canvas, "toolbar": toolbar, "response": response, "line": line,
                    "mean_line": mean_line, "means": [],
                    "spectra": spectra, "norm": norm, "cmap": cmap,
                    "voltages": [], "powers": [], "source": source}
            self.crosstalk_plot_items[name] = plot
        plot["voltages"].append(point.voltage)
        plot["powers"].append(point.power)
        if source == "OSA":
            plot["means"].append(point.mean_power_dbm)
        self.set_crosstalk_plot_data(name, plot)
        plot["response"].relim()
        plot["response"].autoscale(enable=True, axis="y")
        plot["response"].autoscale_view()
        if source == "OSA":
            spectra = plot["spectra"]
            spectra.plot(point.wavelength_nm, point.spectrum_dbm, linewidth=0.8,
                         alpha=0.65, color=plot["cmap"](plot["norm"](point.voltage)))
            spectra.relim()
            spectra.autoscale(enable=True, axis="y")
            spectra.autoscale_view()
            spectra.set_title(f"{name}: {index} spectra")
        plot["canvas"].draw_idle()
        detail = (f"{point.reference_wavelength_nm:g} nm peak {point.power:.4g}, "
                  f"window mean {point.mean_power_dbm:.4g} dBm" if source == "OSA" and
                  point.reference_wavelength_nm is not None else
                  f"peak {point.power:.4g}, mean {point.mean_power_dbm:.4g} dBm" if source == "OSA"
                  else f"{point.power:.4g} {response_unit}")
        self.crosstalk_status.set(f"{name}: {index}/{total} at {point.voltage:.2f} V, {detail}")

    def replace_crosstalk_points(self, name, points):
        """Redraw a switch's curve once out-of-window points were re-read at another HP range."""
        plot = self.crosstalk_plot_items.get(name)
        if plot is None:
            return
        plot["voltages"] = [point.voltage for point in points]
        plot["powers"] = [point.power for point in points]
        self.set_crosstalk_plot_data(name, plot)
        plot["response"].relim()
        plot["response"].autoscale_view()
        plot["canvas"].draw_idle()
        self.crosstalk_status.set(f"{name}: out-of-window points re-read at another HP range")

    def add_rescan_point(self, name, phase, point, index, total, unit=None):
        """Plot a range-change rescan on its own axis, since its scale differs."""
        _, label, color = RESCAN_STYLES[phase]
        unit = unit or self.power_meter.settings["unit"]
        self.crosstalk_status.set(f"{name}: {label.lower()} {index}/{total} at {point.voltage:.2f} V, "
                                  f"{point.power:.4g} {unit}")
        plot = self.crosstalk_plot_items.get(name)
        if plot is None:
            return
        axis = plot.get("rescan_axis")
        if axis is None:
            axis = plot["response"].twinx()
            axis.set_ylabel(f"Rescan power [{unit}] (changed range)")
            plot["rescan_axis"] = axis
            plot["rescans"] = {}
        series = plot["rescans"].get(phase)
        if series is None:
            line, = axis.plot([], [], marker=".", linestyle="--", linewidth=1.2, color=color, label=label)
            series = plot["rescans"][phase] = {"line": line, "voltages": [], "powers": []}
            lines = [plot["line"]] + [item["line"] for item in plot["rescans"].values()]
            plot["response"].legend(lines, [line.get_label() for line in lines], loc="best")
            plot["canvas"].figure.tight_layout()
        series["voltages"].append(point.voltage)
        series["powers"].append(point.power)
        series["line"].set_data(self.plot_x(name, series["voltages"])[0], series["powers"])
        axis.relim()
        axis.autoscale_view()
        plot["canvas"].draw_idle()

    def plot_x_mode(self):
        return PLOT_X_POWER if self.plot_x_axis_var.get().startswith("Heater power") else PLOT_X_VOLTAGE

    def plot_x(self, name, voltages):
        """X values and label for a switch's voltages under the selected axis mode."""
        resistance, total = (getattr(self, "plot_resistance_override", {}).get(name)
                             or (self.controller.heater_resistance(name), self.controller.total_resistance(name)))
        return plot_x_values(voltages, resistance, self.plot_x_mode(), total)

    def open_results_folder(self):
        """Replot a saved crosstalk scan or path calibration."""
        if self.crosstalk_running:
            messagebox.showinfo("Scan in progress", "Wait for the current scan to finish.", parent=self)
            return
        results = app_dir() / "Results"
        folder = filedialog.askdirectory(parent=self, title="Open a scan or calibration results folder",
                                         initialdir=str(results if results.exists() else app_dir()))
        if not folder:
            return
        try:
            loaded = load_scan_folder(folder)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            messagebox.showerror("Could not open results", str(error), parent=self)
            return
        self.show_loaded_results(Path(folder), loaded)

    def show_loaded_results(self, folder, loaded):
        context, data, source, unit = loaded["context"], loaded["data"], loaded["source"], loaded["unit"]
        voltages = context.get("voltages_v") or [point.voltage for points in data.values() for point in points]
        if not data or not voltages:
            messagebox.showinfo("No measurements", f"{folder.name} contains no measured points.", parent=self)
            return
        self.prepare_crosstalk_plots(voltages)
        self.crosstalk_reference_wavelength_nm = context.get("reference_wavelength_nm")
        # Runs saved before R total was recorded used P = V^2 / R heater.
        totals = context.get("total_resistance_ohm", {})
        self.plot_resistance_override = {name: (value, totals.get(name)) for name, value in
                                         context.get("heater_resistance_ohm", {}).items() if value}
        self.add_results_setup_tab(folder, context, loaded["comment"])
        for name, points in data.items():
            for index, point in enumerate(points, start=1):
                self.add_crosstalk_point(name, point, index, len(points), source, unit)
            for phase, (key, _, _) in RESCAN_STYLES.items():
                rows = context.get(key, {}).get(name, [])
                for index, row in enumerate(rows, start=1):
                    self.add_rescan_point(name, phase, ScanPoint(row["voltage_v"], row["power"]),
                                          index, len(rows), unit)
        self.crosstalk_plots.select(0)
        self.crosstalk_data = data
        self.crosstalk_data_source = source
        self.crosstalk_data_unit = unit
        self.crosstalk_export_button.configure(state="normal")
        kind = "Path calibration" if context.get("calibration_mode") else f"{source} crosstalk scan"
        self.crosstalk_status.set(f"Viewing {folder.name}: {kind}, {context.get('status', 'unknown')}, "
                                  f"{len(data)} switch(es).")

    def add_results_setup_tab(self, folder, context, comment):
        tab = ttk.Frame(self.crosstalk_plots, padding=12)
        self.crosstalk_plots.add(tab, text="Setup")
        kind = "Path calibration" if context.get("calibration_mode") else "Crosstalk scan"
        lines = [
            f"{kind} · {context.get('measurement_source', '')} · {context.get('status', 'unknown')}",
            f"Folder: {folder}",
            f"Started: {context.get('started_at', '')}",
            f"Input {context.get('input_port')} → selected output {context.get('selected_output_port')} "
            f"(actual {context.get('actual_output_port')})",
            f"Power meter port: {context.get('power_meter_output_port')} · OSA port: {context.get('osa_output_port')}",
            f"Switches: {', '.join(context.get('scanned_switches', []))}",
            f"Settle: {context.get('settle_ms')} ms",
        ]
        if context.get("reference_wavelength_nm") is not None:
            lines.append(f"OSA reference: input {context.get('reference_input_port', '?')}, "
                         f"{context['reference_wavelength_nm']:g} ± "
                         f"{context.get('reference_half_window_nm', 0):g} nm")
        if comment:
            lines.append(f"Comment: {comment}")
        if context.get("scan_error"):
            lines.append(f"Error: {context['scan_error']}")
        ttk.Label(tab, text="\n".join(lines), justify="left", wraplength=900).pack(anchor="w")
        image_path = folder / "base_path.png"
        if image_path.exists():
            try:
                image = tk.PhotoImage(file=str(image_path))
                image = image.subsample(max(1, -(-image.width() // 900)))
            except tk.TclError:
                return
            label = ttk.Label(tab, image=image)
            label.image = image  # Keep a reference so Tk does not discard it.
            label.pack(anchor="w", pady=(12, 0))

    def set_crosstalk_plot_data(self, name, plot):
        xs, label = self.plot_x(name, plot["voltages"])
        plot["line"].set_data(xs, plot["powers"])
        if plot["mean_line"] is not None:
            plot["mean_line"].set_data(xs, plot["means"])
        plot["response"].set_xlabel(label)

    def replot_crosstalk_x_axis(self):
        """Redraw every open response and victim plot against volts or heater power."""
        for name, plot in getattr(self, "crosstalk_plot_items", {}).items():
            self.set_crosstalk_plot_data(name, plot)
            axes = [plot["response"]]
            for series in plot.get("rescans", {}).values():
                series["line"].set_data(self.plot_x(name, series["voltages"])[0], series["powers"])
            if plot.get("rescan_axis") is not None:
                axes.append(plot["rescan_axis"])
            for axis in axes:
                axis.relim()
                axis.autoscale_view()
            plot["canvas"].draw_idle()
        if getattr(self, "crosstalk_victim_plots", None):
            self.draw_crosstalk_victim(self.crosstalk_victim_var.get())

    def export_crosstalk_csv(self):
        if not self.crosstalk_data:
            return
        path = filedialog.asksaveasfilename(parent=self, title="Export crosstalk dataset",
                                            defaultextension=".csv", filetypes=[("CSV data", "*.csv")],
                                            initialfile=f"crosstalk_{self.crosstalk_data_source.lower()}_{datetime.now():%Y%m%d_%H%M%S}.csv")
        if not path:
            return
        comment = simpledialog.askstring("Dataset comment", "Comment for this CSV:", parent=self)
        if comment is None:
            return
        try:
            write_crosstalk_csv(path, self.crosstalk_data, self.crosstalk_data_source, comment,
                                self.crosstalk_data_unit)
        except (OSError, ValueError) as error:
            messagebox.showerror("Crosstalk export failed", str(error), parent=self)
            return
        messagebox.showinfo("Crosstalk export complete", f"Saved dataset to:\n{path}", parent=self)
