"""MZI calibration tab: detection settings, reachability plan, sweeps and OSA state checks.

Two detection methods share every sweep: the OSA with a WDM line on every
input, or a power meter with the Newport lasers lit one at a time. The plan
of which input and output reveal each MZI updates as the settings change.
"""

import math
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

from benes.gui.theme import (ACCENT, BG, CAL_STATUS, CHECK_SHORT, OSA_COLOR, PANEL, PLOT_MIN_SIZE,
                             POST_SWITCH_SWEEPS, TEXT, VERDICT_ORDER, WDM_COLORS, WIRE, ZERO_VOLT_COLOR)
from benes.gui.widgets import VerticalScrolledFrame
from benes.hardware import osa
from benes.hardware.laser_bank import LaserBank, SimulatedLaserController
from benes.hardware.osa_output_switch import OUTPUTS, fiber_number
from benes.measurements.calibration_scan import scan_voltages
from benes.measurements.laser_calibration import (DEFAULT_PASS_DB, CalibrationStopped, SimulatedChip,
                                                  check_alignment, check_calibrated_states,
                                                  create_calibration_folder, reference_states, reset_all,
                                                  save_state_checks, strongest_input, wavelengths_from_assignment)
from benes.measurements.mzi_characterization import (OSALineDetector, PowerMeterDetector, dbm_to_mw,
                                                     mw_to_dbm, run_calibration, save_calibration)
from benes.measurements.mzi_probing import plan_calibration
from benes.measurements.wdm_comb import INPUTS
from benes.paths import app_dir

LASER_DETECTION_RANGE_NM = (1550.0, 1560.0)
DETECT_OSA = "OSA · all lasers on"
DETECT_POWER_METER = "Power meter · one laser at a time"
METER_HP = "HP 8152A"
METER_ARDUINO = "Arduino"
METER_VIA_JDSU = "JDSU common (any output)"


class CalibrationMixin:
    """Mixed into ``BenesApp``; relies on the state it sets up."""

    def build_calibration_tab(self):
        """Detection settings, calibration and state-check controls beside the live view."""
        controls_column = ttk.Frame(self.calibration_tab, padding=(18, 18, 8, 18))
        controls_column.pack(side="left", fill="y")
        control_scroll = VerticalScrolledFrame(controls_column)
        control_scroll.pack(fill="both", expand=True)
        controls = ttk.Frame(control_scroll.content, padding=(0, 0, 12, 0))
        controls.pack(anchor="nw", fill="x")
        # Both columns scroll on their own, so the tab itself never needs a second scrollbar.
        results_scroll = VerticalScrolledFrame(self.calibration_tab)
        results_scroll.pack(side="left", fill="both", expand=True)
        results = ttk.Frame(results_scroll.content, padding=(8, 18, 18, 18))
        results.pack(fill="both", expand=True)

        ttk.Label(controls, text="MZI CALIBRATION", foreground=ACCENT,
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(controls, wraplength=330, text=(
            "Each MZI is swept while one input's light reaches the detector only in one of its states. "
            "The plan below picks that input and output and holds the other MZIs cross at 0 V or, once "
            "calibrated, at bar when that lets the detector see further.")).pack(anchor="w", pady=(4, 10))

        self.build_detection_frame(controls)

        lasers = ttk.LabelFrame(controls, text="WDM lines (OSA method)", padding=10)
        lasers.pack(fill="x", pady=(12, 0))
        self.cal_laser_labels = []
        for port in INPUTS:
            label = ttk.Label(lasers)
            label.grid(row=(port - 1) % 3, column=(port - 1) // 3, sticky="w", padx=(0, 18), pady=1)
            self.cal_laser_labels.append(label)
        self.cal_detect_lasers_button = ttk.Button(
            lasers, text="Detect six lasers (1550–1560 nm)", command=self.start_laser_channel_detection)
        self.cal_detect_lasers_button.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(lasers, text="Edit wavelengths…", command=lambda: self.notebook.select(
            self.tab_page(self.dataset_tab))).grid(row=4, column=0, columnspan=2, sticky="w", pady=(5, 0))

        sweep = ttk.LabelFrame(controls, text="Sweep", padding=10)
        sweep.pack(fill="x", pady=(12, 0))
        self.cal_fields = {}
        for row, (label, default) in enumerate((("Lower limit [V]", "0"), ("Upper limit [V]", "5"),
                                                 ("Step [V]", "0.1"), ("Settle [ms]", "50"),
                                                 ("Pass margin [dB]", f"{DEFAULT_PASS_DB:g}"))):
            ttk.Label(sweep, text=label).grid(row=row, column=0, sticky="w", pady=2)
            variable = tk.StringVar(value=default)
            ttk.Entry(sweep, textvariable=variable, width=9).grid(row=row, column=1, sticky="w", padx=(10, 0), pady=2)
            self.cal_fields[label] = variable
        ttk.Label(sweep, foreground="#9baec6", wraplength=320, text=(
            "Each point waits the settle time; the OSA then waits for two fresh sweeps. The pass margin "
            "grades the OSA state checks.")).grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

        calibrate = ttk.LabelFrame(controls, text="Calibrate", padding=10)
        calibrate.pack(fill="x", pady=(12, 0))
        self.cal_plan_var = tk.StringVar()
        ttk.Label(calibrate, textvariable=self.cal_plan_var, wraplength=320).grid(
            row=0, column=0, columnspan=2, sticky="w")
        self.cal_switch_var = tk.StringVar(value=next(iter(self.network.switches)))
        ttk.Label(calibrate, text="MZI").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.cal_switch_box = ttk.Combobox(calibrate, textvariable=self.cal_switch_var,
                                           values=tuple(self.network.switches), state="readonly", width=8)
        self.cal_switch_box.grid(row=1, column=1, sticky="w", padx=(10, 0), pady=(8, 0))
        self.cal_switch_box.bind("<<ComboboxSelected>>", lambda event: self.show_selected_probe())
        self.cal_probe_var = tk.StringVar()
        ttk.Label(calibrate, textvariable=self.cal_probe_var, foreground="#9baec6", wraplength=320).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.cal_save_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(calibrate, text="Save the voltages found", variable=self.cal_save_var).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.cal_single_button = ttk.Button(calibrate, text="Sweep selected MZI", command=self.start_single_calibration)
        self.cal_single_button.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.cal_verify_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(calibrate, text="Then check all-cross and all-bar (OSA)", variable=self.cal_verify_var).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.cal_all_button = ttk.Button(calibrate, text="Calibrate all visible MZIs", style="Accent.TButton",
                                         command=self.start_complete_calibration)
        self.cal_all_button.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        checks = ttk.LabelFrame(controls, text="State checks (OSA, all lasers on)", padding=10)
        checks.pack(fill="x", pady=(12, 0))
        self.cal_alignment_button = ttk.Button(checks, text="Alignment check (all 0 V)",
                                               command=self.start_alignment_check)
        self.cal_alignment_button.pack(fill="x")
        self.cal_routed_button = ttk.Button(checks, text="Check routed states", command=self.start_routed_check)
        self.cal_routed_button.pack(fill="x", pady=(6, 0))
        ttk.Label(checks, foreground="#9baec6", wraplength=320, text=(
            "Routed states applies the Routing tab's switch states with their saved voltages and leaves them "
            "applied.")).pack(anchor="w", pady=(6, 0))

        self.cal_comment_var = tk.StringVar()
        ttk.Label(controls, text="Comment").pack(anchor="w", pady=(12, 0))
        ttk.Entry(controls, textvariable=self.cal_comment_var).pack(fill="x")
        self.cal_stop_button = ttk.Button(controls, text="Stop", command=self.stop_mzi_calibration, state="disabled")
        self.cal_stop_button.pack(fill="x", pady=(10, 0))
        ttk.Label(controls, text="For a rehearsal without hardware use a dry-run voltage board (and OSA "
                                 "Simulate for the OSA method): a simulated chip answers.",
                  foreground="#9baec6", wraplength=330).pack(anchor="w", pady=(10, 0))
        self.cal_status_var = tk.StringVar(value="Choose a detection method, then calibrate.")
        ttk.Label(controls, textvariable=self.cal_status_var, wraplength=330).pack(anchor="w", pady=(8, 4))

        # Live view: expected light in each input's colour, MZI status colours, detector ring on the read output.
        self.cal_canvas = tk.Canvas(results, bg=BG, highlightthickness=0, height=260)
        self.cal_canvas.pack(fill="x")
        self.cal_canvas.bind("<Configure>", lambda event: self.draw_calibration_diagram())
        legend = ttk.Frame(results)
        legend.pack(fill="x", pady=(2, 8))
        # Four entries per row so the legend does not widen the tab.
        for index, status in enumerate(("pending", "saved", "active", "calibrated", "partial", "inverted", "hidden")):
            color, text = CAL_STATUS[status]
            ttk.Label(legend, text=f"■ {text}", foreground=color).grid(
                row=index // 4, column=index % 4, sticky="w", padx=(0, 12))
        ttk.Label(legend, text="Solid: lit path · dashes: the other state",
                  foreground="#9baec6").grid(row=1, column=3, columnspan=2, sticky="w")

        views = ttk.Notebook(results)
        views.pack(fill="both", expand=True)
        table_page, matrix_page, overlay_page, sweep_page = (ttk.Frame(views) for _ in range(4))
        views.add(table_page, text="MZIs")
        views.add(sweep_page, text="Sweeps")
        views.add(matrix_page, text="Output matrix")
        views.add(overlay_page, text="OSA overlay")
        self.cal_views = views
        self.cal_overlay_page = overlay_page
        self.cal_sweep_page = sweep_page
        style = ttk.Style(self)
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=24)
        style.configure("Treeview.Heading", background="#263650", foreground=TEXT, font=("Segoe UI", 9, "bold"))
        # Modest starting widths; the columns stretch to fill a wider window.
        columns = (("mzi", "MZI", 50), ("status", "Status", 90), ("probe", "Probe", 170),
                   ("cross", "Cross [V]", 70), ("bar", "Bar [V]", 70), ("extinction", "Extinction [dB]", 100),
                   ("check", "State checks", 120))
        self.cal_table = ttk.Treeview(table_page, columns=[name for name, _, _ in columns], show="headings",
                                      height=8)
        for name, heading, width in columns:
            self.cal_table.heading(name, text=heading)
            self.cal_table.column(name, width=width, anchor="w" if name == "probe" else "center")
        for status, (color, _) in CAL_STATUS.items():
            self.cal_table.tag_configure(status, foreground=color)
        self.cal_table.pack(fill="both", expand=True)
        self.cal_table.bind("<<TreeviewSelect>>", self.select_calibration_row)

        ttk.Label(matrix_page, foreground="#9baec6", wraplength=600, text=(
            "Peak power [dBm] of each input's line at each output for the last OSA readout. ● marks the line "
            "expected at that output; the row colour is the strongest line.")).pack(anchor="w", pady=(6, 4))
        self.cal_matrix_title = tk.StringVar(value="No readout yet")
        ttk.Label(matrix_page, textvariable=self.cal_matrix_title, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        matrix_columns = ["output"] + [f"in{port}" for port in INPUTS] + ["strongest"]
        self.cal_matrix = ttk.Treeview(matrix_page, columns=matrix_columns, show="headings", height=6)
        for name in matrix_columns:
            heading = ("Output" if name == "output" else "Strongest" if name == "strongest"
                       else f"In {name[2:]}")
            self.cal_matrix.heading(name, text=heading)
            self.cal_matrix.column(name, width=80, anchor="center")
        for port in INPUTS:
            self.cal_matrix.tag_configure(f"input{port}", foreground=WDM_COLORS[port - 1])
        self.cal_matrix.pack(fill="both", expand=True)

        self.cal_overlay_plots = ttk.Notebook(overlay_page)
        self.cal_overlay_plots.pack(fill="both", expand=True)
        self.cal_overlay_items = {}
        self.cal_overlay_hint = ttk.Label(overlay_page, text="State-check OSA curves appear here, overlaid by output.")
        self.cal_overlay_hint.place(relx=0.5, rely=0.5, anchor="center")

        self.cal_plots = ttk.Notebook(sweep_page)
        self.cal_plots.pack(fill="both", expand=True)
        self.cal_plot_items = {}
        self.cal_plot_hint = ttk.Label(sweep_page, text="Sweep plots appear here, one tab per MZI.")
        self.cal_plot_hint.place(relx=0.5, rely=0.5, anchor="center")

        progress = ttk.Frame(results)
        progress.pack(fill="x", pady=(8, 0))
        self.cal_mzi_progress_var = tk.StringVar(value="MZIs completed: 0/0")
        self.cal_sweep_progress_var = tk.StringVar(value="Sweep points completed: 0/0")
        ttk.Label(progress, textvariable=self.cal_mzi_progress_var).grid(row=0, column=0, sticky="w")
        self.cal_mzi_progress = ttk.Progressbar(progress, maximum=1)
        self.cal_mzi_progress.grid(row=0, column=1, sticky="ew", padx=(12, 0))
        ttk.Label(progress, textvariable=self.cal_sweep_progress_var).grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.cal_sweep_progress = ttk.Progressbar(progress, maximum=1)
        self.cal_sweep_progress.grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=(4, 0))
        progress.columnconfigure(1, weight=1)

        self.cal_rows = {}
        self.cal_plan = None
        self.cal_view = {"states": reference_states(self.network), "probe": None, "reading": None,
                         "dominant": {}, "note": "All MZIs shown at 0 V (cross)."}
        self.reset_calibration_rows()
        for variable in (self.detection_method_var, self.detection_position_var):
            variable.trace_add("write", lambda *_: self.update_calibration_plan())
        self.update_calibration_lasers()

    def build_detection_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="Detection", padding=10)
        frame.pack(fill="x")
        for row, method in enumerate((DETECT_OSA, DETECT_POWER_METER)):
            ttk.Radiobutton(frame, text=method, value=method, variable=self.detection_method_var).grid(
                row=row, column=0, columnspan=4, sticky="w")
        meter = ttk.Frame(frame)
        meter.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        ttk.Label(meter, text="Meter").grid(row=0, column=0, sticky="w")
        ttk.Combobox(meter, textvariable=self.detection_meter_var, values=(METER_HP, METER_ARDUINO),
                     state="readonly", width=12).grid(row=0, column=1, sticky="w", padx=(10, 0))
        ttk.Label(meter, text="Meter on").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Combobox(meter, textvariable=self.detection_position_var,
                     values=(METER_VIA_JDSU,) + tuple(f"Output {output}" for output in OUTPUTS),
                     state="readonly", width=24).grid(row=1, column=1, sticky="w", padx=(10, 0), pady=(4, 0))
        ttk.Label(meter, text="Newport slot per input (bays are reversed: slot 1 feeds input 6)",
                  foreground="#9baec6", wraplength=320).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 2))
        slots = ttk.Frame(meter)
        slots.grid(row=3, column=0, columnspan=2, sticky="w")
        for index, port in enumerate(INPUTS):
            ttk.Label(slots, text=f"In {port}", foreground=WDM_COLORS[port - 1]).grid(
                row=index // 3, column=2 * (index % 3), sticky="w", padx=(0, 4), pady=1)
            ttk.Combobox(slots, textvariable=self.laser_slot_vars[port], values=tuple(map(str, range(1, 17))),
                         state="readonly", width=3).grid(row=index // 3, column=2 * (index % 3) + 1,
                                                         sticky="w", padx=(0, 12), pady=1)
        ttk.Label(meter, text="Laser settle [s]").grid(row=4, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(meter, textvariable=self.laser_settle_var, width=8).grid(row=4, column=1, sticky="w",
                                                                         padx=(10, 0), pady=(6, 0))
        ttk.Label(meter, foreground="#9baec6", wraplength=320, text=(
            "Connect the Newport controller in its tab; the TECs must be on. Each probe lights its input "
            "only, and the lasers return to their previous states afterwards. HP 8152A settings are in "
            "Thermal crosstalk, Arduino settings in Hardware & OSA.")).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

        def show_meter(*_):
            if self.detection_method_var.get() == DETECT_POWER_METER:
                meter.grid()
            else:
                meter.grid_remove()

        self.detection_method_var.trace_add("write", show_meter)
        show_meter()

    def power_meter_method(self):
        return self.detection_method_var.get() == DETECT_POWER_METER

    def detection_ports(self):
        """0-based (inputs, outputs) the chosen detection can light and read."""
        if not self.power_meter_method():
            return tuple(sorted(self.calibration_wavelengths())), tuple(range(self.network.size))
        position = self.detection_position_var.get()
        outputs = (tuple(range(self.network.size)) if position == METER_VIA_JDSU
                   else (int(position.split()[-1]) - 1,))
        return tuple(range(self.network.size)), outputs

    def detection_summary(self):
        if not self.power_meter_method():
            return f"{DETECT_OSA}, lines on inputs {', '.join(str(row + 1) for row in self.detection_ports()[0]) or '—'}"
        return (f"{self.detection_meter_var.get()} on {self.detection_position_var.get()}, "
                "one Newport laser at a time")

    def calibrated_switches(self):
        calibration = self.controller.settings["calibration"]
        return {name for name in self.network.switches if calibration[name]["straight"] is not None}

    def update_calibration_plan(self):
        """Work out which MZIs the current detection can see, and through which probe."""
        if not hasattr(self, "cal_rows") or self.mzi_calibration_running:
            return
        inputs, outputs = self.detection_ports()
        self.cal_plan = plan_calibration(self.network, inputs=inputs, outputs=outputs,
                                         known=self.calibrated_switches())
        for name, row in self.cal_rows.items():
            probe = self.cal_plan.probe_for(name)
            row["probe"] = (probe.describe() if probe is not None else
                            f"not visible: {self.cal_plan.unreachable.get(name, '')}")
            if row["status"] in ("pending", "hidden"):
                row["status"] = "pending" if probe is not None else "hidden"
            self.show_calibration_row(name)
        visible = len(self.cal_plan.steps)
        text = f"{visible}/{len(self.network.switches)} MZIs visible with {self.detection_summary()}."
        if self.cal_plan.unreachable:
            text += " Not visible: " + ", ".join(self.cal_plan.unreachable) + " (see the MZIs table)."
        if any(probe.biased for probe in self.cal_plan.steps):
            text += " Some probes hold calibrated MZIs at bar, so Calibrate all orders them."
        self.cal_plan_var.set(text)
        self.show_selected_probe()

    def show_selected_probe(self):
        name = self.cal_switch_var.get()
        probe = None if self.cal_plan is None else self.cal_plan.probe_for(name)
        if probe is not None:
            self.cal_probe_var.set(f"Probe: {probe.describe()}.")
        elif self.cal_plan is not None:
            self.cal_probe_var.set(f"{name} is not visible: {self.cal_plan.unreachable.get(name, '')}.")
        self.draw_calibration_diagram()

    def calibration_wavelengths(self):
        """{0-based input: nm} from the WDM tab fields, as typed."""
        return {row: wavelength for row, wavelength, _ in self.wdm_routes()}

    def update_calibration_lasers(self):
        wavelengths = self.calibration_wavelengths()
        for row, label in enumerate(self.cal_laser_labels):
            wavelength = wavelengths.get(row)
            label.configure(text=f"In {row + 1} · " + ("no line" if wavelength is None else f"{wavelength:g} nm"),
                            foreground="#71829a" if wavelength is None else WDM_COLORS[row])
        self.update_calibration_plan()

    def start_laser_channel_detection(self):
        """Read the OSA comb and fill the six input wavelengths by wavelength order."""
        try:
            if self.mzi_calibration_running:
                raise RuntimeError("Wait for the MZI calibration to finish")
            if self.osa_button.instate(["disabled"]):
                raise RuntimeError("Wait for the current OSA trace read to finish")
            host, port, simulate = self.osa_connection()
            self.calibration_plot_support()
        except (ImportError, RuntimeError, ValueError) as error:
            messagebox.showerror("Laser-channel detection unavailable", str(error), parent=self)
            return

        self.set_osa_reading(True)
        self.begin_activity("laser_detection", "Detecting laser channels from OSA outputs...")
        self.cal_detect_lasers_button.configure(state="disabled")
        self.cal_status_var.set("Reading OSA outputs for six laser channels in 1550–1560 nm...")

        def acquire():
            traces = []
            try:
                switch = self.osa_output_switch
                if not simulate and switch.connected and len(switch.mapped_outputs()) == len(INPUTS):
                    wavelengths = self.detect_laser_channels_from_outputs(host, port, traces)
                else:
                    wave, power = osa.simulated_spectrum() if simulate else osa.get_spectrum(host, port)
                    wave, power = osa.trim_spurious_tail(wave, power)
                    traces.append(("OSA trace", wave, power))
                    wavelengths = osa.find_laser_wavelengths(
                        wave, power, count=len(INPUTS), wavelength_range_nm=LASER_DETECTION_RANGE_NM)
            except Exception as error:
                self.after(0, lambda error=error: self.finish_laser_channel_detection(
                    traces=traces, error=str(error)))
            else:
                self.after(0, lambda: self.finish_laser_channel_detection(wavelengths=wavelengths, traces=traces))

        threading.Thread(target=acquire, daemon=True).start()

    def detect_laser_channels_from_outputs(self, host, port, traces):
        """Read all mapped outputs and detect one dominant wavelength per output."""
        switch = self.osa_output_switch
        outputs = switch.mapped_outputs()
        peaks = []
        for index, output in enumerate(outputs, start=1):
            self.after(0, lambda o=output, i=index: self.cal_status_var.set(
                f"Reading OSA output {o} ({i}/{len(outputs)})..."))
            if hasattr(self, "update_activity"):
                self.after(0, lambda o=output, i=index: self.update_activity(
                    "laser_detection", i - 1, total=len(outputs) + 1,
                    detail=f"Detecting lasers: reading output {o} ({i}/{len(outputs)})"))
            switch.select_output(output)
            wave, power = osa.get_spectrum(host, port, fresh_sweeps=POST_SWITCH_SWEEPS)
            wave, power = osa.trim_spurious_tail(wave, power)
            traces.append((f"Out {output} (fiber {fiber_number(output)})", wave, power))
            if hasattr(self, "update_activity"):
                self.after(0, lambda o=output, i=index: self.update_activity(
                    "laser_detection", i, detail=f"Detected spectrum at output {o} ({i}/{len(outputs)})"))
            peaks.extend(osa.find_laser_wavelengths(
                wave, power, count=1, wavelength_range_nm=LASER_DETECTION_RANGE_NM))
        try:
            return osa.distinct_laser_wavelengths(
                peaks, count=len(INPUTS), wavelength_range_nm=LASER_DETECTION_RANGE_NM)
        except ValueError:
            # A line may be strongest at more than one output. A full comb in
            # any readout can still provide the six wavelength positions.
            for _, wave, power in traces:
                try:
                    return osa.find_laser_wavelengths(
                        wave, power, count=len(INPUTS), wavelength_range_nm=LASER_DETECTION_RANGE_NM)
                except ValueError:
                    continue
            raise

    def finish_laser_channel_detection(self, wavelengths=None, traces=None, error=None):
        self.finish_activity("laser_detection", "Laser detection failed" if error else "Laser detection complete")
        self.set_osa_reading(False)
        self.cal_detect_lasers_button.configure(state="normal")
        if traces:
            self.show_laser_detection_overlay(traces, wavelengths or ())
        if error is not None:
            self.cal_status_var.set("Could not detect six laser channels from the OSA outputs.")
            messagebox.showerror("Laser-channel detection failed", error, parent=self)
            return
        assignment = {str(port): wavelength for port, wavelength in zip(INPUTS, wavelengths)}
        try:
            self.set_wdm_assignment(assignment)
            self.save_wdm_plan(quiet=True)
        except (OSError, ValueError) as error:
            messagebox.showerror("Could not save wavelength plan", str(error), parent=self)
            return
        self.cal_status_var.set("Detected six 1550–1560 nm channels; inspect their curves in the OSA overlay tab.")

    def select_calibration_row(self, _event=None):
        selection = self.cal_table.selection()
        if selection and not self.mzi_calibration_running:
            self.cal_switch_var.set(selection[0])
            self.show_selected_probe()

    def reset_calibration_rows(self):
        """Show each MZI's saved voltages; clears this session's sweep and check results."""
        for name in self.network.switches:
            calibration = self.controller.settings["calibration"][name]
            saved = calibration["straight"] is not None and calibration["cross"] is not None
            self.cal_rows[name] = {"status": "saved" if saved else "pending", "probe": "", "cross": calibration["cross"],
                                   "bar": calibration["straight"], "extinction": None, "checks": []}
            self.show_calibration_row(name)

    def show_calibration_row(self, name):
        row = self.cal_rows[name]
        number = lambda value, digits=2: "—" if value is None or math.isinf(value) else f"{value:.{digits}f}"
        checks = " · ".join(f"{label} {verdict}" + ("" if margin is None else f" {margin:.1f}")
                            for label, verdict, margin in row["checks"])
        values = (name, CAL_STATUS[row["status"]][1], row["probe"], number(row["cross"]), number(row["bar"]),
                  "> 60" if row["extinction"] is not None and math.isinf(row["extinction"])
                  else number(row["extinction"], 1), checks)
        if self.cal_table.exists(name):
            self.cal_table.item(name, values=values, tags=(row["status"],))
        else:
            self.cal_table.insert("", "end", iid=name, values=values, tags=(row["status"],))

    def calibration_settings(self):
        """Voltages, settle time [s] and pass margin [dB] from the Sweep fields."""
        fields = self.cal_fields
        voltages = scan_voltages(*(float(fields[key].get()) for key in
                                   ("Lower limit [V]", "Upper limit [V]", "Step [V]")))
        settle_ms = float(fields["Settle [ms]"].get())
        if not 0 <= settle_ms <= 10000:
            raise ValueError("Settling time must be between 0 and 10000 ms")
        pass_db = float(fields["Pass margin [dB]"].get())
        if not 0 < pass_db <= 60:
            raise ValueError("The pass margin must be between 0 and 60 dB")
        return voltages, settle_ms / 1000, pass_db

    def measurement_busy(self):
        return (self.mzi_calibration_running or self.crosstalk_running or self.wdm_running or
                self.dataset_batch_running or self.impulse_running)

    def calibration_hardware(self):
        """Validate the OSA instruments and wavelength plan; return (read_spectrum, wavelengths, half window, simulated)."""
        if self.measurement_busy():
            raise RuntimeError("Wait for the current measurement to finish")
        if self.osa_button.instate(["disabled"]):
            raise RuntimeError("Wait for the current OSA read to finish")
        host, port, simulate = self.osa_connection()
        if not self.controller.connected:
            raise RuntimeError("Connect the voltage board first (Hardware & OSA)")
        if self.controller.settings["dry_run"] != simulate:
            raise RuntimeError("Use the physical voltage board with the real OSA, or a dry-run board with "
                               "OSA Simulate for a rehearsal")
        self.require_jdsu()
        try:
            self.save_wdm_plan(quiet=True)
        except OSError as error:
            raise ValueError(f"Could not save the wavelength plan: {error}") from error
        wavelengths = wavelengths_from_assignment(self.wdm_plan.assignment)
        if not wavelengths:
            raise ValueError("Assign a comb wavelength to each laser input in the WDM comb tab")
        half_window = self.wdm_plan.settings["half_window_nm"]
        if simulate:
            chip = SimulatedChip(self.network, lambda name: self.controller.applied_voltages.get(name, 0.0),
                                 wavelengths)
            return chip.spectrum, wavelengths, half_window, True
        return (lambda output: osa.get_spectrum(host, port, fresh_sweeps=POST_SWITCH_SWEEPS),
                wavelengths, half_window, False)

    def require_jdsu(self):
        switch = self.osa_output_switch
        if not switch.connected:
            raise RuntimeError("Connect the OSA output switch first (dry run is available in Selector setup)")
        for output in OUTPUTS:
            switch.channel_for(output)

    def detection_setup(self):
        """Validate the chosen detection on the Tk thread.

        Returns a dict whose ``open()`` runs on the worker thread and gives
        (detector, close); also the ports, unit and a context record.
        """
        for name in self.network.switches:
            if self.controller.settings["mapping"].get(name) is None:
                raise ValueError(f"Assign a voltage-board pin to {name} first")
        if not self.power_meter_method():
            read, wavelengths, half_window, simulate = self.calibration_hardware()
            switch = self.osa_output_switch
            return {"open": lambda: (OSALineDetector(switch, read, wavelengths, half_window), lambda: None),
                    "simulate": simulate, "unit": "mW", "osa": (read, wavelengths, half_window),
                    "inputs": tuple(sorted(wavelengths)), "outputs": tuple(range(self.network.size)),
                    "context": {"detection": DETECT_OSA,
                                "laser_wavelengths_nm": {str(row + 1): value for row, value in wavelengths.items()},
                                "line_half_window_nm": half_window, "simulated": simulate,
                                "jdsu_channel_map": dict(switch.settings["channel_map"])}}

        if self.measurement_busy():
            raise RuntimeError("Wait for the current measurement to finish")
        if not self.controller.connected:
            raise RuntimeError("Connect the voltage board first (Hardware & OSA)")
        simulate = self.controller.settings["dry_run"]
        self.laser_bank.configure({port: variable.get() for port, variable in self.laser_slot_vars.items()},
                                  self.laser_settle_var.get())
        slots = dict(self.laser_bank.settings["slots"])
        inputs, outputs = self.detection_ports()
        via_jdsu = len(outputs) > 1
        switch = self.osa_output_switch if via_jdsu else None
        if via_jdsu:
            self.require_jdsu()
        meter_name = self.detection_meter_var.get()
        context = {"detection": DETECT_POWER_METER, "meter": meter_name,
                   "meter_position": self.detection_position_var.get(), "simulated": simulate,
                   "newport_slot_per_input": {str(port): slot for port, slot in slots.items()},
                   "laser_settle_s": self.laser_bank.settings["settle_s"]}
        if via_jdsu:
            context["jdsu_channel_map"] = dict(switch.settings["channel_map"])
        fixed = None if via_jdsu else outputs[0]

        def detector(read_power, lasers, unit, label):
            return PowerMeterDetector(read_power, lasers, unit, switch, fixed, label)

        if simulate:
            def open_simulated():
                chip = SimulatedChip(self.network, lambda name: self.controller.applied_voltages.get(name, 0.0), {})
                lasers = LaserBank(SimulatedLaserController(chip, slots), self.laser_bank.config_path,
                                   sleep=lambda seconds: None)

                def read_power():
                    return dbm_to_mw(chip.meter_dbm(switch.routed_output if via_jdsu else fixed + 1))

                return detector(read_power, lasers, "mW", "Simulated power meter"), lambda: None

            return {"open": open_simulated, "simulate": True, "unit": "mW", "osa": None,
                    "inputs": inputs, "outputs": outputs, "context": context}

        if not self.newport_controller.connected:
            raise RuntimeError("Connect the Newport laser controller in the Newport lasers tab first")
        if self.newport_panel.busy:
            raise RuntimeError("Wait for the Newport command to finish")
        if meter_name == METER_HP:
            if self.hp_meter_busy:
                raise RuntimeError("Stop the HP meter stream or test first")
            options = self.hp_meter_options()
            context["hp_meter"] = options

            def open_hp():
                meter = self.open_hp_meter(options)
                return (detector(lambda: dbm_to_mw(meter.read_dbm_or_none()), self.laser_bank, "mW", METER_HP),
                        meter.disconnect)

            return {"open": open_hp, "simulate": False, "unit": "mW", "osa": None, "hp": True,
                    "inputs": inputs, "outputs": outputs, "context": context}
        meter = self.power_meter
        if not meter.connected:
            raise RuntimeError("Connect the Arduino meter in Hardware & OSA first")
        if meter.settings["scale"] <= 0:
            raise ValueError("Set a positive Arduino power scale in Hardware & OSA")
        if self.read_power_button.instate(["disabled"]):
            raise RuntimeError("Wait for the current Arduino reading to finish")
        context["arduino_settings"] = dict(meter.settings)

        def read_arduino():
            reading = meter.read_current_power()
            if not math.isfinite(reading.raw_mean) or reading.raw_mean >= meter.ceiling:
                raise ValueError("The Arduino detector saturated or returned an invalid value")
            return reading.value

        unit = meter.settings["unit"]
        return {"open": lambda: (detector(read_arduino, self.laser_bank, unit, METER_ARDUINO), lambda: None),
                "simulate": False, "unit": unit, "osa": None, "inputs": inputs, "outputs": outputs,
                "context": context}

    def claim_detection(self, setup, claim):
        """Keep the Newport panel and HP meter away from a running measurement."""
        if setup["simulate"] or setup["osa"] is not None:
            return
        self.newport_panel.busy = claim
        self.newport_panel.render()
        if setup.get("hp"):
            self.hp_meter_busy = "measurement" if claim else None

    def start_alignment_check(self):
        try:
            read, wavelengths, half_window, simulate = self.calibration_hardware()
            _, settle_s, pass_db = self.calibration_settings()
            self.calibration_plot_support()
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Alignment check unavailable", str(error), parent=self)
            return

        def job(hooks):
            check = check_alignment(self.controller, self.osa_output_switch, read, self.network, wavelengths,
                                    half_window, settle_s, hooks["on_output"], hooks["stop_event"])
            hooks["on_check"](check)
            hooks["checks"].append(check)

        self.run_mzi_job("alignment", "Alignment check: reading all outputs with every MZI at 0 V...", job,
                         wavelengths, half_window, simulate, pass_db, settle_s)

    def start_routed_check(self):
        states = dict(self.network.states)
        try:
            read, wavelengths, half_window, simulate = self.calibration_hardware()
            _, settle_s, pass_db = self.calibration_settings()
            calibration = self.controller.settings["calibration"]
            missing = [name for name in self.network.switches
                       if calibration[name]["cross" if states[name] else "straight"] is None]
            if missing:
                raise ValueError(f"Calibrate {', '.join(missing)} first; their routed state has no saved voltage")
            self.calibration_plot_support()
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("State check unavailable", str(error), parent=self)
            return

        def job(hooks):
            check = check_calibrated_states(self.controller, self.osa_output_switch, read, self.network, states,
                                            wavelengths, half_window, "Routed states", settle_s,
                                            hooks["on_output"], hooks["stop_event"])
            hooks["on_check"](check)
            hooks["checks"].append(check)

        self.cal_view["states"] = states
        self.run_mzi_job("routed_check", "Checking the routed states on every output...", job,
                         wavelengths, half_window, simulate, pass_db, settle_s, leaves_zero=False)

    def start_single_calibration(self):
        name = self.cal_switch_var.get()
        self.start_calibration([name], f"single_{name.replace('.', '_')}", self.cal_save_var.get(), verify=False)

    def start_complete_calibration(self):
        self.start_calibration(list(self.network.switches), "complete", True, self.cal_verify_var.get())

    def start_calibration(self, names, kind, save, verify):
        try:
            if self.mzi_calibration_running:
                raise RuntimeError("A calibration is already running")
            setup = self.detection_setup()
            voltages, settle_s, pass_db = self.calibration_settings()
            # A single sweep keeps the other MZIs' saved voltages; a full run re-plans as it goes.
            plan = plan_calibration(self.network, names, setup["inputs"], setup["outputs"],
                                    known=self.calibrated_switches())
            if not plan.steps:
                hidden = "; ".join(f"{name}: {reason}" for name, reason in plan.unreachable.items())
                raise ValueError(f"No selected MZI is visible with this detection. {hidden}")
            dataset_root = self.calibration_dataset_root()
            self.confirm_voltage_sweep(voltages, names=[probe.name for probe in plan.steps])
            self.calibration_plot_support()
        except (ImportError, OSError, RuntimeError, ValueError, tk.TclError) as error:
            messagebox.showerror("MZI calibration unavailable", str(error), parent=self)
            return
        verify = verify and setup["osa"] is not None and not plan.unreachable
        context = {
            "kind": kind, **setup["context"],
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "comment": self.cal_comment_var.get().strip(), "unit": setup["unit"], "saved_voltages": save,
            "voltages_v": list(voltages), "settle_s": settle_s,
            "background": "MZIs off the probe cross at 0 V, or held at a calibrated bar where the plan says",
            "not_visible": plan.unreachable,
            "switch_pins": dict(self.controller.settings["mapping"]),
            "calibration_before": {name: dict(value) for name, value in self.controller.settings["calibration"].items()},
        }
        self.mzi_calibration_running = True
        self.calibration_stop_event = stop_event = threading.Event()
        self.cal_pass_db = pass_db
        self.cal_job_verdicts = {}
        self.cal_mzi_total = len(plan.steps)
        self.cal_mzi_finished = 0
        self.cal_sweep_total = len(voltages)
        self.cal_unit = setup["unit"]
        self.cal_reference_lines = setup["osa"] is not None
        self.begin_activity("calibration", f"Calibrating {len(plan.steps)} MZI(s)...",
                            len(plan.steps) * len(voltages) + 1)
        self.cal_mzi_progress.configure(maximum=len(plan.steps), value=0)
        self.cal_mzi_progress_var.set(f"MZIs completed: 0/{len(plan.steps)}")
        self.cal_sweep_progress.configure(maximum=len(voltages), value=0)
        self.cal_sweep_progress_var.set(f"Sweep points completed: 0/{len(voltages)}")
        self.set_calibration_controls(True)
        self.claim_detection(setup, True)
        for name in names:
            self.cal_rows[name]["checks"] = []
        self.cal_status_var.set(f"Calibrating with {self.detection_summary()}...")
        after = lambda function: lambda *args: self.after(0, lambda: function(*args))
        results, checks = {}, []

        def worker():
            error, stopped, folder, save_error = None, False, None, None
            try:
                detector, close = setup["open"]()
                try:
                    run_calibration(self.controller, detector, self.network, plan.steps, voltages, settle_s, save,
                                    results=results, on_step=after(self.calibration_probe_started),
                                    on_point=after(self.calibration_point),
                                    on_result=after(lambda result: self.calibration_switch_done(result, save)),
                                    stop_event=stop_event)
                finally:
                    close()
                if verify:
                    read, wavelengths, half_window = setup["osa"]
                    for label, crossed in (("All cross (calibrated)", True), ("All bar (calibrated)", False)):
                        check = check_calibrated_states(
                            self.controller, self.osa_output_switch, read, self.network,
                            dict.fromkeys(self.network.switches, crossed), wavelengths, half_window, label,
                            settle_s, after(self.calibration_output_read), stop_event)
                        checks.append(check)
                        self.after(0, lambda check=check: self.calibration_check_done(check))
            except CalibrationStopped:
                stopped = True
            except Exception as failure:
                error = str(failure)
            finally:
                if verify:
                    # The checks leave the calibrated states applied.
                    try:
                        reset_all(self.controller, self.network)
                    except Exception as failure:
                        error = error or str(failure)
            if results or checks:
                status = "stopped" if stopped else "failed" if error else "complete"
                try:
                    folder = create_calibration_folder(app_dir(), kind, dataset_root)
                    record = {**context, "status": status, "error": error}
                    save_calibration(folder, record, results)
                    if checks:
                        save_state_checks(folder, record, checks, pass_db)
                except Exception as failure:
                    save_error = str(failure)
            self.after(0, lambda: self.finish_calibration(setup, error, stopped, folder, save_error))

        threading.Thread(target=worker, daemon=True).start()

    def finish_calibration(self, setup, error, stopped, folder, save_error):
        self.claim_detection(setup, False)
        self.finish_mzi_job(error, stopped, folder, save_error, leaves_zero=True)
        self.update_calibration_plan()

    def calibration_plot_support(self):
        """Raise ImportError early when matplotlib is missing, like the other scans."""
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  # noqa: F401
        from matplotlib.figure import Figure  # noqa: F401

    def calibration_dataset_root(self):
        value = self.dataset_folder_var.get().strip()
        if not value or not (Path(value) / "benes_dataset.json").is_file():
            raise ValueError("Choose an initialized dataset folder in the Dataset tab before sweeping MZIs")
        return Path(value)

    def stop_mzi_calibration(self):
        if self.calibration_stop_event is not None:
            self.calibration_stop_event.set()
            self.cal_status_var.set("Stopping after the current measurement; every MZI will return to 0 V...")

    def set_calibration_controls(self, running):
        state = "disabled" if running else "normal"
        for button in (self.cal_single_button, self.cal_all_button, self.cal_alignment_button,
                       self.cal_routed_button):
            button.configure(state=state)
        self.cal_stop_button.configure(state="normal" if running else "disabled")
        for tab in (self.route_tab, self.hardware_tab, self.crosstalk_tab, self.dataset_tab, self.impulse_tab,
                    self.newport_tab):
            self.set_tab_state(tab, state)
        self.set_osa_reading(running)

    def run_mzi_job(self, kind, message, job, wavelengths, half_window, simulate, pass_db, settle_s,
                    leaves_zero=True):
        """Run an OSA state check on a worker thread, then save its results.

        ``job(hooks)`` fills ``hooks["checks"]`` and reports progress through
        the hook callbacks.
        """
        context = {
            "kind": kind,
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "comment": self.cal_comment_var.get().strip(),
            "laser_wavelengths_nm": {str(row + 1): wavelength for row, wavelength in wavelengths.items()},
            "line_half_window_nm": half_window,
            "settle_s": settle_s,
            "simulated": simulate,
            "switch_pins": dict(self.controller.settings["mapping"]),
            "calibration": {name: dict(value) for name, value in self.controller.settings["calibration"].items()},
            "jdsu_channel_map": dict(self.osa_output_switch.settings["channel_map"]),
        }
        self.mzi_calibration_running = True
        self.calibration_stop_event = stop_event = threading.Event()
        self.cal_pass_db = pass_db
        self.cal_job_verdicts = {}
        self.cal_mzi_total = self.cal_mzi_finished = self.cal_sweep_total = 0
        self.begin_activity("calibration", message)
        self.cal_mzi_progress.configure(maximum=1, value=0)
        self.cal_mzi_progress_var.set("MZIs completed: 0/0")
        self.cal_sweep_progress.configure(maximum=1, value=0)
        self.cal_sweep_progress_var.set("Sweep points completed: 0/0")
        dataset_root = (Path(self.dataset_folder_var.get().strip())
                        if self.dataset_folder_var.get().strip() else None)
        if dataset_root is not None and not (dataset_root / "benes_dataset.json").is_file():
            dataset_root = None
        self.set_calibration_controls(True)
        self.cal_status_var.set(message)
        hooks = {
            "stop_event": stop_event, "checks": [],
            "on_output": lambda readout, index, total: self.after(
                0, lambda: self.calibration_output_read(readout, index, total)),
            "on_check": lambda check: self.after(0, lambda: self.calibration_check_done(check)),
        }

        def worker():
            error, stopped, folder, save_error = None, False, None, None
            try:
                job(hooks)
            except CalibrationStopped:
                stopped = True
            except Exception as error_value:
                error = str(error_value)
            if hooks["checks"]:
                status = "stopped" if stopped else "failed" if error else "complete"
                try:
                    folder = create_calibration_folder(app_dir(), kind, dataset_root)
                    save_state_checks(folder, {**context, "status": status, "error": error}, hooks["checks"], pass_db)
                except Exception as error_value:
                    save_error = str(error_value)
            self.after(0, lambda: self.finish_mzi_job(error, stopped, folder, save_error, leaves_zero))

        threading.Thread(target=worker, daemon=True).start()

    def finish_mzi_job(self, error, stopped, folder, save_error, leaves_zero):
        self.mzi_calibration_running = False
        self.finish_activity("calibration", "Calibration failed" if error or save_error else
                             "Calibration stopped" if stopped else "Calibration complete")
        self.calibration_stop_event = None
        self.set_calibration_controls(False)
        probe = self.cal_view["probe"]
        if probe is not None and self.cal_rows[probe.name]["status"] == "active":
            row = self.cal_rows[probe.name]
            row["status"] = "failed" if error else "saved" if None not in (row["cross"], row["bar"]) else "pending"
            self.show_calibration_row(probe.name)
        self.cal_view.update(probe=None, reading=None, lit=None)
        if leaves_zero:
            # The routine has sent 0 V to every MZI, which is cross.
            self.network.states = reference_states(self.network)
            self.zero_volt_switches = set(self.network.switches)
            for name in self.network.switches:
                self.voltage_vars[name].set("0.00")
            self.cal_view["states"] = reference_states(self.network)
        else:
            self.zero_volt_switches.clear()
            self.show_calibrated_voltages()
        routed = self.osa_output_switch.routed_output
        if routed is not None:
            self.osa_output_var.set(str(routed))
        self.refresh(keep_selection=True)
        self.draw_calibration_diagram()
        saved = "" if folder is None else f" Results: {folder}"
        if save_error:
            saved = f" Saving the results failed: {save_error}"
        if error:
            self.cal_status_var.set(f"Calibration failed: {error}.{saved}")
            messagebox.showerror("Calibration failed", error, parent=self)
        elif stopped:
            self.cal_status_var.set(f"Calibration stopped; every MZI is back at 0 V.{saved}")
        else:
            self.cal_status_var.set(f"{self.cal_view['note'].rstrip('.')}.{saved}")
        if save_error:
            messagebox.showerror("Calibration results not saved", save_error, parent=self)

    def calibration_output_read(self, readout, index, total):
        self.update_activity("calibration", detail=f"Calibration: reading output {readout.output + 1} "
                             f"({index}/{total})")
        self.cal_view["reading"] = readout.output
        self.cal_view["dominant"][readout.output] = strongest_input(readout.lines)
        self.cal_status_var.set(f"Read output {readout.output + 1} ({index}/{total}).")
        self.draw_calibration_diagram()

    def calibration_check_done(self, check):
        self.update_activity("calibration", detail=f"Calibration: checking {check.label}...")
        pass_db = self.cal_pass_db
        short = CHECK_SHORT.get(check.label, check.label)
        for name, item in check.switches.items():
            verdict = item.verdict(pass_db)
            row = self.cal_rows[name]
            row["checks"] = [entry for entry in row["checks"] if entry[0] != short] + [(short, verdict, item.margin_db)]
            # Several checks in one run combine to their worst verdict.
            verdicts = self.cal_job_verdicts.setdefault(name, [])
            verdicts.append(verdict)
            row["status"] = min(verdicts, key=VERDICT_ORDER.index)
            self.show_calibration_row(name)
        self.cal_view.update(states=dict(check.states), probe=None, reading=None, lit=None,
                             dominant={output: strongest_input(lines) for output, lines in check.matrix.items()})
        passed = sum(item.verdict(pass_db) == "ok" for item in check.switches.values())
        errors = check.routing_errors()
        note = f"{check.label}: {passed}/{len(check.switches)} MZIs pass the {pass_db:g} dB margin"
        note += (". Misrouted lines: " + ", ".join(
            f"In {source + 1}→{'—' if measured is None else f'Out {measured + 1}'} (expected Out {expected + 1})"
            for source, expected, measured in errors) if errors else "; every line leaves where expected.")
        self.cal_view["note"] = note
        self.cal_status_var.set(note)
        self.show_calibration_matrix(check)
        self.show_calibration_overlay(check)
        self.cal_views.select(self.cal_overlay_page)
        self.draw_calibration_diagram()

    def show_calibration_overlay(self, check):
        """Overlay the measured spectrum from every OSA output in a state check."""
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        from matplotlib.figure import Figure

        self.cal_overlay_hint.place_forget()
        old = self.cal_overlay_items.pop(check.label, None)
        if old is not None:
            old.destroy()
        tab = ttk.Frame(self.cal_overlay_plots)
        self.cal_overlay_plots.add(tab, text=CHECK_SHORT.get(check.label, check.label))
        self.cal_overlay_plots.select(tab)
        figure = Figure(figsize=(8, 4.8))
        axis = figure.add_subplot(111)
        for readout in check.readouts:
            axis.plot(readout.wave, readout.power, linewidth=1.2,
                      color=WDM_COLORS[readout.output],
                      label=f"Out {readout.output + 1} (fiber {fiber_number(readout.output + 1)})")
        for source, wavelength in sorted(self.calibration_wavelengths().items()):
            expected = check.expected[source]
            axis.axvline(wavelength, linewidth=1, linestyle=":", alpha=0.85,
                         color=WDM_COLORS[source],
                         label=f"In {source + 1}: {wavelength:g} nm → Out {expected + 1}")
        axis.set(title=f"{check.label}: measured OSA spectra by output",
                 xlabel="Wavelength [nm]", ylabel="Power [dBm]")
        axis.grid(True, alpha=0.35)
        axis.legend(loc="upper left", bbox_to_anchor=(1.01, 1), fontsize=8)
        figure.subplots_adjust(left=0.09, right=0.69, bottom=0.13, top=0.90)
        canvas = FigureCanvasTkAgg(figure, master=tab)
        toolbar = NavigationToolbar2Tk(canvas, tab)
        toolbar.update()
        canvas.get_tk_widget().configure(**PLOT_MIN_SIZE)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        canvas.draw_idle()
        self.cal_overlay_items[check.label] = tab

    def show_laser_detection_overlay(self, traces, wavelengths):
        """Show the actual OSA curves used to identify the six laser channels."""
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        from matplotlib.figure import Figure

        self.cal_overlay_hint.place_forget()
        label = "Laser detection"
        old = self.cal_overlay_items.pop(label, None)
        if old is not None:
            old.destroy()
        tab = ttk.Frame(self.cal_overlay_plots)
        self.cal_overlay_plots.add(tab, text=label)
        self.cal_overlay_plots.select(tab)
        figure = Figure(figsize=(8, 4.8))
        axis = figure.add_subplot(111)
        for index, (name, wave, power) in enumerate(traces):
            axis.plot(wave, power, linewidth=1.2,
                      color=WDM_COLORS[index] if len(traces) > 1 else OSA_COLOR,
                      label=name)
        for index, wavelength in enumerate(wavelengths):
            axis.axvline(wavelength, linewidth=1, linestyle=":", color=WDM_COLORS[index],
                         label=f"In {index + 1}: {wavelength:g} nm")
        axis.set(title="Detected laser channels: measured OSA output spectra",
                 xlabel="Wavelength [nm]", ylabel="Power [dBm]")
        axis.grid(True, alpha=0.35)
        axis.legend(loc="upper left", bbox_to_anchor=(1.01, 1), fontsize=8)
        figure.subplots_adjust(left=0.09, right=0.69, bottom=0.13, top=0.90)
        canvas = FigureCanvasTkAgg(figure, master=tab)
        toolbar = NavigationToolbar2Tk(canvas, tab)
        toolbar.update()
        canvas.get_tk_widget().configure(**PLOT_MIN_SIZE)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        canvas.draw_idle()
        self.cal_overlay_items[label] = tab
        self.cal_views.select(self.cal_overlay_page)

    def show_calibration_matrix(self, check):
        self.cal_matrix.delete(*self.cal_matrix.get_children())
        self.cal_matrix_title.set(check.label)
        for output, lines in sorted(check.matrix.items()):
            expected = check.expected.index(output)
            strongest = strongest_input(lines)
            cells = []
            for source in range(self.network.size):
                power = lines.get(source)
                cell = "—" if power is None else f"{power:.1f}"
                cells.append(f"● {cell}" if source == expected else cell)
            self.cal_matrix.insert("", "end", values=[f"Out {output + 1}", *cells,
                                                      "—" if strongest is None else f"In {strongest + 1}"],
                                   tags=() if strongest is None else (f"input{strongest + 1}",))

    def calibration_probe_started(self, probe, index, total):
        self.update_activity("calibration", detail=f"Calibrating {probe.name} ({index}/{total})...")
        self.cal_sweep_progress.configure(value=0)
        self.cal_sweep_progress_var.set(f"{probe.name} sweep points completed: 0/{self.cal_sweep_total}")
        row = self.cal_rows[probe.name]
        row.update(status="active", probe=probe.describe())
        self.cal_job_verdicts.pop(probe.name, None)
        self.show_calibration_row(probe.name)
        self.cal_view.update(probe=probe, reading=probe.output, states=probe.states(probe.bright_crossed),
                             lit=None if self.cal_reference_lines else {probe.input})
        self.cal_view["note"] = f"{probe.name}: {probe.describe()}."
        self.cal_status_var.set(self.cal_view["note"])
        self.add_calibration_plot(probe)
        self.cal_views.select(self.cal_sweep_page)
        self.draw_calibration_diagram()

    def plotted_power(self, power):
        """dBm for powers in mW, otherwise the meter's own linear unit; NaN when missing."""
        if self.cal_unit == "mW":
            value = mw_to_dbm(power)
        else:
            value = power
        return float("nan") if value is None else value

    def calibration_point(self, name, index, total, point):
        self.update_activity("calibration", self.cal_mzi_finished * self.cal_sweep_total + index,
                             detail=f"Calibrating {name}: point {index}/{total} at {point.voltage:.2f} V")
        self.cal_sweep_progress.configure(maximum=total, value=index)
        self.cal_sweep_progress_var.set(f"{name} sweep points completed: {index}/{total}")
        unit = "dBm" if self.cal_unit == "mW" else self.cal_unit
        self.cal_status_var.set(f"{name}: {index}/{total} at {point.voltage:.2f} V · "
                                f"{self.plotted_power(point.power):.4g} {unit}")
        plot = self.cal_plot_items.get(name)
        if plot is None:
            return
        plot["points"].append(point)
        voltages = [item.voltage for item in plot["points"]]
        plot["power_line"].set_data(voltages, [self.plotted_power(item.power) for item in plot["points"]])
        if plot["reference_line"] is not None:
            plot["reference_line"].set_data(voltages, [self.plotted_power(item.reference) for item in plot["points"]])
            plot["contrast_line"].set_data(voltages, [float("nan") if item.contrast_db is None else item.contrast_db
                                                      for item in plot["points"]])
        for axis in plot["axes"]:
            axis.relim()
            axis.autoscale_view()
        plot["canvas"].draw_idle()

    def calibration_switch_done(self, result, saved):
        self.cal_mzi_finished += 1
        self.update_activity("calibration", self.cal_mzi_finished * self.cal_sweep_total,
                             detail=f"Calibration: {self.cal_mzi_finished}/{self.cal_mzi_total} MZIs complete")
        self.cal_mzi_progress.configure(value=self.cal_mzi_finished)
        self.cal_mzi_progress_var.set(f"MZIs completed: {self.cal_mzi_finished}/{self.cal_mzi_total}")
        name = result.name
        row = self.cal_rows[name]
        row.update(status="calibrated" if saved else "saved" if row["cross"] is not None else "pending",
                   extinction=result.extinction_db)
        if saved:
            row.update(cross=result.cross.voltage, bar=result.bar.voltage)
        self.show_calibration_row(name)
        self.cal_view["note"] = (f"{name}: cross {result.cross.voltage:.2f} V, bar {result.bar.voltage:.2f} V, "
                                 + ("extinction > 60 dB" if math.isinf(result.extinction_db)
                                    else f"extinction {result.extinction_db:.1f} dB")
                                 + ("" if saved else " (not saved)"))
        plot = self.cal_plot_items.get(name)
        if plot is not None:
            for axis in plot["axes"]:
                axis.axvline(result.cross.voltage, color=ACCENT, linestyle="--", linewidth=1.2,
                             label=f"Cross {result.cross.voltage:.2f} V")
                axis.axvline(result.bar.voltage, color="#e0a100", linestyle="--", linewidth=1.2,
                             label=f"Bar {result.bar.voltage:.2f} V")
            plot["axes"][-1].legend(loc="best", fontsize=8)
            plot["canvas"].draw_idle()
        self.draw_calibration_diagram()

    def add_calibration_plot(self, probe):
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        from matplotlib.figure import Figure

        self.cal_plot_hint.place_forget()
        old = self.cal_plot_items.pop(probe.name, None)
        if old is not None:
            old["tab"].destroy()
        tab = ttk.Frame(self.cal_plots)
        self.cal_plots.add(tab, text=probe.name)
        self.cal_plots.select(tab)
        figure = Figure(figsize=(7, 4.6))
        unit = "dBm" if self.cal_unit == "mW" else self.cal_unit
        if self.cal_reference_lines:
            lines_axis = figure.add_subplot(211)
            contrast_axis = figure.add_subplot(212, sharex=lines_axis)
            axes = (lines_axis, contrast_axis)
        else:
            lines_axis = figure.add_subplot(111)
            axes = (lines_axis,)
        state = "cross" if probe.bright_crossed else "bar"
        power_line, = lines_axis.plot([], [], marker=".", linewidth=1.4, color=WDM_COLORS[probe.input],
                                      label=f"In {probe.input + 1} (arrives if {state})")
        reference_line = contrast_line = None
        if self.cal_reference_lines:
            other = "bar" if probe.bright_crossed else "cross"
            reference_line, = lines_axis.plot([], [], marker=".", linewidth=1.4, color=WDM_COLORS[probe.other_input],
                                              label=f"In {probe.other_input + 1} (arrives if {other})")
            contrast_line, = contrast_axis.plot([], [], marker=".", linewidth=1.4, color="#315b78")
            contrast_axis.axhline(0, color="#9baec6", linewidth=0.8)
            contrast_axis.set(xlabel="Voltage [V]", ylabel=f"In {probe.input + 1} − In {probe.other_input + 1} [dB]")
            contrast_axis.grid(True)
        else:
            lines_axis.set_xlabel("Voltage [V]")
        lines_axis.set(ylabel=f"Power [{unit}]",
                       title=f"{probe.name}: output {probe.output + 1} (fiber {fiber_number(probe.output + 1)})")
        lines_axis.legend(loc="best", fontsize=8)
        lines_axis.grid(True)
        figure.tight_layout()
        canvas = FigureCanvasTkAgg(figure, master=tab)
        toolbar = NavigationToolbar2Tk(canvas, tab)
        toolbar.update()
        canvas.get_tk_widget().configure(**PLOT_MIN_SIZE)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        self.cal_plot_items[probe.name] = {"tab": tab, "canvas": canvas, "axes": axes, "power_line": power_line,
                                           "reference_line": reference_line, "contrast_line": contrast_line,
                                           "points": []}

    def draw_calibration_diagram(self):
        """Draw the network with each lit input's path in its colour and each MZI's status."""
        c = getattr(self, "cal_canvas", None)
        if c is None or not hasattr(self, "cal_rows"):
            return
        c.delete("all")
        width, height = c.winfo_width(), c.winfo_height()
        if width < 420 or height < 220:
            return
        view = self.cal_view
        states = view["states"]
        probe = view["probe"]
        if probe is None and not self.mzi_calibration_running and self.cal_plan is not None:
            # Preview the selected MZI's probe.
            probe = self.cal_plan.probe_for(self.cal_switch_var.get())
            if probe is not None:
                states = probe.states(probe.bright_crossed)
        font = ("Segoe UI", 8)
        left_x, right_x = 96, width - 104
        first_stage, last_stage = left_x + 60, right_x - 70
        xs = [first_stage + i * (last_stage - first_stage) / 4 for i in range(5)]
        ys = [42 + i * (height - 84) / 5 for i in range(6)]
        half = 16
        for stage, pairs in enumerate(self.network.pairs):
            x = xs[stage]
            c.create_text(x, 12, text=f"STAGE {stage + 1}", fill="#9baec6", font=("Segoe UI", 8, "bold"))
            for row, y in enumerate(ys):
                if stage == 0:
                    c.create_line(left_x + 14, y, x - half, y, fill=WIRE, width=2)
                next_x = xs[stage + 1] - half if stage < 4 else right_x - 14
                c.create_line(x + half, y, next_x, ys[self.network.links[stage][row]], fill=WIRE, width=2)
                if (stage, row) not in self.network.at:
                    c.create_line(x - half, y, x + half, y, fill=WIRE, width=2)

        osa_method = not self.power_meter_method()
        wavelengths = self.calibration_wavelengths()
        lit = view.get("lit")
        if lit is None:
            lit = set(wavelengths) if osa_method else ({probe.input} if probe is not None else set())
        routes = {row: self.network.trace(row, states) for row in lit}
        if probe is not None:
            # Where the probed MZI's lines would go in its other state.
            other = {**states, probe.name: not states[probe.name]}
            for row, route in routes.items():
                if any(name == probe.name for name, _ in route.states):
                    c.create_line(*self.route_points(self.network.trace(row, other), left_x + 14, right_x - 14,
                                                     xs, ys, half),
                                  fill=WDM_COLORS[row], width=2, dash=(6, 4), joinstyle="round", capstyle="round")
        for row, route in routes.items():
            c.create_line(*self.route_points(route, left_x + 14, right_x - 14, xs, ys, half),
                          fill=WDM_COLORS[row], width=3, joinstyle="round", capstyle="round")

        biased = set(probe.biased) if probe is not None else set()
        for stage, pairs in enumerate(self.network.pairs):
            x = xs[stage]
            for index, (a, b) in enumerate(pairs):
                name = f"S{stage + 1}.{index + 1}"
                status = self.cal_rows[name]["status"]
                color = CAL_STATUS[status][0]
                active = probe is not None and probe.name == name
                c.create_rectangle(x - half, ys[a] - 7, x + half, ys[b] + 7,
                                   fill="#2b2a1c" if active else PANEL,
                                   outline=ZERO_VOLT_COLOR if active else color,
                                   width=3 if active or status not in ("pending", "saved") else 2)
                for start, end in ((a, b), (b, a)) if states[name] else ((a, a), (b, b)):
                    c.create_line(x - half, ys[start], x + half, ys[end], fill="#a4bad4", width=1.5)
                c.create_text(x, ys[a] - 16, text=name + (" =" if name in biased else ""),
                              fill=ZERO_VOLT_COLOR if active else color, font=("Segoe UI", 8, "bold"))
                margins = [margin for _, _, margin in self.cal_rows[name]["checks"] if margin is not None]
                if margins:
                    c.create_text(x, ys[b] + 16, text=f"{min(margins):.0f} dB", fill=color, font=font)

        watched = None if probe is None else probe.output
        slots = self.laser_bank.settings["slots"]
        for row, y in enumerate(ys):
            if osa_method:
                wavelength = wavelengths.get(row)
                text = f"In {row + 1}\n" + ("no laser" if wavelength is None else f"{wavelength:g} nm")
            else:
                text = f"In {row + 1}\nslot {slots[row + 1]}"
            on = row in lit
            color = WDM_COLORS[row] if on else WIRE
            c.create_oval(left_x - 13, y - 13, left_x + 13, y + 13, fill=PANEL, outline=color, width=3 if on else 1.5)
            c.create_text(left_x, y, text=str(row + 1), fill=TEXT, font=("Segoe UI", 9, "bold"))
            c.create_text(left_x - 20, y, anchor="e", font=font, fill=color if on else "#71829a", text=text)
            dominant = view["dominant"].get(row)
            c.create_oval(right_x - 13, y - 13, right_x + 13, y + 13, outline=WIRE, width=1.5,
                          fill=PANEL if dominant is None else WDM_COLORS[dominant])
            c.create_text(right_x, y, text=str(row + 1), fill=TEXT if dominant is None else BG,
                          font=("Segoe UI", 9, "bold"))
            label = f"Out {row + 1}" + ("" if dominant is None else f"\nmeas. In {dominant + 1}")
            c.create_text(right_x + 20, y, anchor="w", text=label, fill="#9baec6", font=font)
            for radius, ring, ring_color in ((18, view["reading"], OSA_COLOR), (23, watched, ZERO_VOLT_COLOR)):
                if ring == row:
                    c.create_oval(right_x - radius, y - radius, right_x + radius, y + radius,
                                  outline=ring_color, width=2.5)
        c.create_text(width / 2, height - 10, text=view["note"], fill="#9baec6", font=font)
