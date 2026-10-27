"""MZI calibration tab: six-laser sweeps, the complete routine and state checks."""

import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from benes.gui.theme import (ACCENT, BG, CAL_STATUS, CHECK_SHORT, OSA_COLOR, PANEL, PLOT_MIN_SIZE,
                             POST_SWITCH_SWEEPS, TEXT, VERDICT_ORDER, WDM_COLORS, WIRE, ZERO_VOLT_COLOR)
from benes.gui.widgets import VerticalScrolledFrame
from benes.hardware import osa
from benes.hardware.osa_output_switch import OUTPUTS, fiber_number
from benes.measurements.calibration_scan import scan_voltages
from benes.measurements.laser_calibration import (DEFAULT_PASS_DB, CalibrationStopped, CompleteCalibration,
                                                  SimulatedChip, calibrate_all, calibrate_switch,
                                                  check_alignment, check_calibrated_states,
                                                  create_calibration_folder, reference_states,
                                                  save_calibration_results, strongest_input, switch_probes,
                                                  wavelengths_from_assignment)
from benes.measurements.wdm_comb import INPUTS
from benes.paths import app_dir


class CalibrationMixin:
    """Mixed into ``BenesApp``; relies on the state it sets up."""

    def build_calibration_tab(self):
        """Six-laser MZI calibration: single-MZI sweeps, the complete routine and state checks."""
        controls_column = ttk.Frame(self.calibration_tab, padding=(18, 18, 8, 18), width=370)
        controls_column.pack(side="left", fill="y")
        controls_column.pack_propagate(False)
        control_scroll = VerticalScrolledFrame(controls_column)
        control_scroll.pack(fill="both", expand=True)
        controls = ttk.Frame(control_scroll.content, padding=(0, 0, 12, 0))
        controls.pack(anchor="nw", fill="x")
        results = ttk.Frame(self.calibration_tab, padding=(8, 18, 18, 18))
        results.pack(side="left", fill="both", expand=True)

        ttk.Label(controls, text="SIX-LASER CALIBRATION", foreground=ACCENT,
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(controls, wraplength=330, text=(
            "Every input carries a laser at its WDM comb wavelength, so every MZI carries two lines. The OSA "
            "reads the outputs through the JDSU switch; which line arrives where shows the state of each MZI. "
            "MZIs not being swept stay at 0 V (cross).")).pack(anchor="w", pady=(4, 10))

        lasers = ttk.LabelFrame(controls, text="Lasers (WDM comb plan)", padding=10)
        lasers.pack(fill="x")
        self.cal_laser_labels = []
        for port in INPUTS:
            label = ttk.Label(lasers)
            label.grid(row=(port - 1) % 3, column=(port - 1) // 3, sticky="w", padx=(0, 18), pady=1)
            self.cal_laser_labels.append(label)
        self.cal_detect_lasers_button = ttk.Button(
            lasers, text="Read OSA + detect six channels", command=self.start_laser_channel_detection)
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
            "Each point waits the settle time, then for two fresh OSA sweeps. Lines are measured within the "
            "WDM tab's line window. A state passes when the expected line beats the other by the margin.")).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

        single = ttk.LabelFrame(controls, text="Single MZI", padding=10)
        single.pack(fill="x", pady=(12, 0))
        self.cal_switch_var = tk.StringVar(value=next(iter(self.network.switches)))
        self.cal_watch_var = tk.StringVar(value="Auto")
        self.cal_save_var = tk.BooleanVar(value=True)
        ttk.Label(single, text="MZI").grid(row=0, column=0, sticky="w")
        switch_box = ttk.Combobox(single, textvariable=self.cal_switch_var, values=tuple(self.network.switches),
                                  state="readonly", width=8)
        switch_box.grid(row=0, column=1, sticky="w", padx=(10, 0))
        switch_box.bind("<<ComboboxSelected>>", lambda event: self.update_calibration_watch_choices())
        ttk.Label(single, text="Watch").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.cal_watch_box = ttk.Combobox(single, textvariable=self.cal_watch_var, state="readonly", width=30)
        self.cal_watch_box.grid(row=1, column=1, sticky="w", padx=(10, 0), pady=(6, 0))
        self.cal_watch_box.bind("<<ComboboxSelected>>", lambda event: self.draw_calibration_diagram())
        ttk.Checkbutton(single, text="Save the voltages found", variable=self.cal_save_var).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.cal_single_button = ttk.Button(single, text="Sweep selected MZI", command=self.start_single_calibration)
        self.cal_single_button.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        complete = ttk.LabelFrame(controls, text="Complete calibration", padding=10)
        complete.pack(fill="x", pady=(12, 0))
        ttk.Label(complete, wraplength=320, text=(
            "1. Alignment: read all outputs at 0 V.  2. Sweep every MZI, grouped by watched output so the "
            "JDSU moves as little as possible.  3. Check the calibrated all-cross and all-bar states.")).grid(
            row=0, column=0, sticky="w")
        self.cal_verify_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(complete, text="Verify all-cross and all-bar states", variable=self.cal_verify_var).grid(
            row=1, column=0, sticky="w", pady=(6, 0))
        self.cal_all_button = ttk.Button(complete, text="Calibrate all MZIs", style="Accent.TButton",
                                         command=self.start_complete_calibration)
        self.cal_all_button.grid(row=2, column=0, sticky="ew", pady=(8, 0))

        checks = ttk.LabelFrame(controls, text="State checks", padding=10)
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
        ttk.Label(controls, text="Uses the voltage board, OSA and JDSU from Hardware & OSA. For a rehearsal, use a "
                                 "dry-run board with OSA Simulate.", foreground="#9baec6",
                  wraplength=330).pack(anchor="w", pady=(10, 0))
        self.cal_status_var = tk.StringVar(value="Assign a comb wavelength to every input, then start a check.")
        ttk.Label(controls, textvariable=self.cal_status_var, wraplength=330).pack(anchor="w", pady=(8, 4))

        # Live view: expected light in each input's colour, MZI status colours, OSA ring on the read output.
        self.cal_canvas = tk.Canvas(results, bg=BG, highlightthickness=0, height=330)
        self.cal_canvas.pack(fill="x")
        self.cal_canvas.bind("<Configure>", lambda event: self.draw_calibration_diagram())
        legend = ttk.Frame(results)
        legend.pack(fill="x", pady=(2, 8))
        for status in ("pending", "saved", "active", "calibrated", "partial", "inverted"):
            color, text = CAL_STATUS[status]
            ttk.Label(legend, text=f"■ {text}", foreground=color).pack(side="left", padx=(0, 12))
        ttk.Label(legend, text="Solid: expected light · dashes: where it goes in the other state",
                  foreground="#9baec6").pack(side="left")

        views = ttk.Notebook(results)
        views.pack(fill="both", expand=True)
        table_page, matrix_page, sweep_page = ttk.Frame(views), ttk.Frame(views), ttk.Frame(views)
        views.add(table_page, text="MZIs")
        views.add(matrix_page, text="Output matrix")
        views.add(sweep_page, text="Sweeps")
        style = ttk.Style(self)
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=24)
        style.configure("Treeview.Heading", background="#263650", foreground=TEXT, font=("Segoe UI", 9, "bold"))
        columns = (("mzi", "MZI", 60), ("status", "Status", 110), ("watch", "Watched output", 170),
                   ("cross", "Cross [V]", 75), ("bar", "Bar [V]", 75), ("cross_margin", "Cross margin [dB]", 115),
                   ("bar_margin", "Bar margin [dB]", 110), ("check", "State checks", 230))
        self.cal_table = ttk.Treeview(table_page, columns=[name for name, _, _ in columns], show="headings",
                                      height=12)
        for name, heading, width in columns:
            self.cal_table.heading(name, text=heading)
            self.cal_table.column(name, width=width, anchor="center")
        for status, (color, _) in CAL_STATUS.items():
            self.cal_table.tag_configure(status, foreground=color)
        self.cal_table.pack(fill="both", expand=True)
        self.cal_table.bind("<<TreeviewSelect>>", self.select_calibration_row)

        ttk.Label(matrix_page, foreground="#9baec6", wraplength=760, text=(
            "Peak power [dBm] of each input's line at each output for the last readout. ● marks the line "
            "expected at that output; the row colour is the strongest line.")).pack(anchor="w", pady=(6, 4))
        self.cal_matrix_title = tk.StringVar(value="No readout yet")
        ttk.Label(matrix_page, textvariable=self.cal_matrix_title, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        matrix_columns = ["output"] + [f"in{port}" for port in INPUTS] + ["strongest"]
        self.cal_matrix = ttk.Treeview(matrix_page, columns=matrix_columns, show="headings", height=6)
        for name in matrix_columns:
            heading = ("Output" if name == "output" else "Strongest" if name == "strongest"
                       else f"In {name[2:]}")
            self.cal_matrix.heading(name, text=heading)
            self.cal_matrix.column(name, width=90, anchor="center")
        for port in INPUTS:
            self.cal_matrix.tag_configure(f"input{port}", foreground=WDM_COLORS[port - 1])
        self.cal_matrix.pack(fill="both", expand=True)

        self.cal_plots = ttk.Notebook(sweep_page)
        self.cal_plots.pack(fill="both", expand=True)
        self.cal_plot_items = {}
        self.cal_plot_hint = ttk.Label(sweep_page, text="Sweep plots appear here, one tab per MZI.")
        self.cal_plot_hint.place(relx=0.5, rely=0.5, anchor="center")

        self.cal_rows = {}
        self.cal_view = {"states": reference_states(self.network), "probe": None, "reading": None,
                         "dominant": {}, "note": "All MZIs shown at 0 V (cross)."}
        self.reset_calibration_rows()
        self.update_calibration_watch_choices()
        self.update_calibration_lasers()

    def calibration_wavelengths(self):
        """{0-based input: nm} from the WDM tab fields, as typed."""
        return {row: wavelength for row, wavelength, _ in self.wdm_routes()}

    def update_calibration_lasers(self):
        wavelengths = self.calibration_wavelengths()
        for row, label in enumerate(self.cal_laser_labels):
            wavelength = wavelengths.get(row)
            label.configure(text=f"In {row + 1} · " + ("no line" if wavelength is None else f"{wavelength:g} nm"),
                            foreground="#71829a" if wavelength is None else WDM_COLORS[row])
        self.update_calibration_watch_choices()

    def start_laser_channel_detection(self):
        """Read the OSA comb and fill the six input wavelengths by wavelength order."""
        try:
            if self.mzi_calibration_running:
                raise RuntimeError("Wait for the MZI calibration to finish")
            if self.osa_button.instate(["disabled"]):
                raise RuntimeError("Wait for the current OSA trace read to finish")
            host, port, simulate = self.osa_connection()
        except (RuntimeError, ValueError) as error:
            messagebox.showerror("Laser-channel detection unavailable", str(error), parent=self)
            return

        self.set_osa_reading(True)
        self.cal_detect_lasers_button.configure(state="disabled")
        self.cal_status_var.set("Reading the OSA trace and locating six laser channels...")

        def acquire():
            try:
                wave, power = osa.simulated_spectrum() if simulate else osa.get_spectrum(host, port)
                wave, power = osa.trim_spurious_tail(wave, power)
                try:
                    wavelengths = osa.find_laser_wavelengths(wave, power, count=len(INPUTS))
                except ValueError as direct_error:
                    wavelengths = self.detect_laser_channels_from_outputs(host, port, simulate, direct_error)
            except Exception as error:
                self.after(0, lambda error=error: self.finish_laser_channel_detection(error=str(error)))
            else:
                self.after(0, lambda: self.finish_laser_channel_detection(wavelengths=wavelengths))

        threading.Thread(target=acquire, daemon=True).start()

    def detect_laser_channels_from_outputs(self, host, port, simulate, direct_error):
        """Fall back to one strongest line per output when one trace is attenuated."""
        switch = self.osa_output_switch
        if not switch.connected:
            raise ValueError(f"{direct_error} Connect the OSA output switch to also search all outputs.")
        outputs = switch.mapped_outputs()
        if len(outputs) < len(INPUTS):
            raise ValueError(f"{direct_error} Map all six OSA outputs to use the multi-output fallback.")
        peaks = []
        for index, output in enumerate(outputs, start=1):
            self.after(0, lambda o=output, i=index: self.cal_status_var.set(
                f"Direct trace was attenuated; reading output {o} ({i}/{len(outputs)})..."))
            switch.select_output(output)
            wave, power = (osa.simulated_spectrum() if simulate else
                           osa.get_spectrum(host, port, fresh_sweeps=POST_SWITCH_SWEEPS))
            wave, power = osa.trim_spurious_tail(wave, power)
            peaks.extend(osa.find_laser_wavelengths(wave, power, count=1))
        return osa.distinct_laser_wavelengths(peaks, count=len(INPUTS))

    def finish_laser_channel_detection(self, wavelengths=None, error=None):
        self.set_osa_reading(False)
        self.cal_detect_lasers_button.configure(state="normal")
        if error is not None:
            self.cal_status_var.set("Could not detect six laser channels from the OSA trace.")
            messagebox.showerror("Laser-channel detection failed", error, parent=self)
            return
        assignment = {str(port): wavelength for port, wavelength in zip(INPUTS, wavelengths)}
        try:
            self.set_wdm_assignment(assignment)
            self.save_wdm_plan(quiet=True)
        except (OSError, ValueError) as error:
            messagebox.showerror("Could not save wavelength plan", str(error), parent=self)
            return
        self.cal_status_var.set("Detected six OSA channels: Input 1 is the highest wavelength; Input 6 the lowest.")

    def update_calibration_watch_choices(self):
        """Offer the two outputs whose arriving line the selected MZI decides."""
        self.cal_watch_probes = switch_probes(self.network, reference_states(self.network), self.cal_switch_var.get())
        wavelengths = self.calibration_wavelengths()
        choices = ["Auto"]
        for probe in self.cal_watch_probes:
            dark = [source + 1 for source in (probe.cross_input, probe.straight_input) if source not in wavelengths]
            choices.append(f"Out {probe.output + 1}: In {probe.cross_input + 1} if X, In {probe.straight_input + 1} if ="
                           + (f" (no line on In {', '.join(map(str, dark))})" if dark else ""))
        self.cal_watch_box.configure(values=choices)
        if self.cal_watch_var.get() not in choices:
            self.cal_watch_var.set("Auto")
        self.draw_calibration_diagram()

    def selected_watch_probe(self):
        choice = self.cal_watch_box.current()
        return None if choice <= 0 else self.cal_watch_probes[choice - 1]

    def select_calibration_row(self, _event=None):
        selection = self.cal_table.selection()
        if selection and not self.mzi_calibration_running:
            self.cal_switch_var.set(selection[0])
            self.update_calibration_watch_choices()

    def reset_calibration_rows(self):
        """Show each MZI's saved voltages; clears this session's sweep and check results."""
        for name in self.network.switches:
            calibration = self.controller.settings["calibration"][name]
            saved = calibration["straight"] is not None and calibration["cross"] is not None
            self.cal_rows[name] = {"status": "saved" if saved else "pending", "watch": "", "cross": calibration["cross"],
                                   "bar": calibration["straight"], "cross_margin": None, "bar_margin": None,
                                   "checks": []}
            self.show_calibration_row(name)

    def show_calibration_row(self, name):
        row = self.cal_rows[name]
        number = lambda value, digits=2: "—" if value is None else f"{value:.{digits}f}"
        checks = " · ".join(f"{label} {verdict}" + ("" if margin is None else f" {margin:.1f}")
                            for label, verdict, margin in row["checks"])
        values = (name, CAL_STATUS[row["status"]][1], row["watch"], number(row["cross"]), number(row["bar"]),
                  number(row["cross_margin"], 1), number(row["bar_margin"], 1), checks)
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

    def calibration_hardware(self):
        """Validate the instruments and wavelength plan; return (read_spectrum, wavelengths, half window, simulated)."""
        if self.mzi_calibration_running:
            raise RuntimeError("A calibration is already running")
        if self.crosstalk_running or self.wdm_running or self.dataset_batch_running or self.impulse_running:
            raise RuntimeError("Wait for the current scan or impulse test to finish")
        if self.osa_button.instate(["disabled"]):
            raise RuntimeError("Wait for the current OSA read to finish")
        host, port, simulate = self.osa_connection()
        if not self.controller.connected:
            raise RuntimeError("Connect the voltage board first (Hardware & OSA)")
        if self.controller.settings["dry_run"] != simulate:
            raise RuntimeError("Use the physical voltage board with the real OSA, or a dry-run board with "
                               "OSA Simulate for a rehearsal")
        switch = self.osa_output_switch
        if not switch.connected:
            raise RuntimeError("Connect the OSA output switch first (dry run is available in Selector setup)")
        for output in OUTPUTS:
            switch.channel_for(output)
        for name in self.network.switches:
            if self.controller.settings["mapping"].get(name) is None:
                raise ValueError(f"Assign a voltage-board pin to {name} first")
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

    def start_alignment_check(self):
        try:
            read, wavelengths, half_window, simulate = self.calibration_hardware()
            _, settle_s, pass_db = self.calibration_settings()
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Alignment check unavailable", str(error), parent=self)
            return

        def job(hooks):
            check = check_alignment(self.controller, self.osa_output_switch, read, self.network, wavelengths,
                                    half_window, settle_s, hooks["on_output"], hooks["stop_event"])
            hooks["on_check"](check)
            hooks["checks"].append(check)

        self.run_mzi_job("alignment", "Alignment check: reading all outputs with every MZI at 0 V...", job,
                         wavelengths, half_window, simulate, pass_db, [], settle_s)

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
        except (OSError, RuntimeError, ValueError) as error:
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
                         wavelengths, half_window, simulate, pass_db, [], settle_s, leaves_zero=False)

    def start_single_calibration(self):
        name = self.cal_switch_var.get()
        probe = self.selected_watch_probe()
        save = self.cal_save_var.get()
        try:
            read, wavelengths, half_window, simulate = self.calibration_hardware()
            voltages, settle_s, pass_db = self.calibration_settings()
            self.confirm_voltage_sweep(voltages, names=[name])
            self.calibration_plot_support()
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("MZI sweep unavailable", str(error), parent=self)
            return

        def job(hooks):
            result = calibrate_switch(self.controller, self.osa_output_switch, read, self.network, name, voltages,
                                      settle_s, wavelengths, half_window, None if probe is None else probe.output,
                                      save, hooks["on_probe"], hooks["on_point"], hooks["stop_event"])
            hooks["calibrations"][name] = result
            hooks["on_switch"](result, save)

        self.run_mzi_job(f"single_{name.replace('.', '_')}", f"Sweeping {name} with every other MZI at 0 V...",
                         job, wavelengths, half_window, simulate, pass_db, voltages, settle_s)

    def start_complete_calibration(self):
        verify = self.cal_verify_var.get()
        try:
            read, wavelengths, half_window, simulate = self.calibration_hardware()
            voltages, settle_s, pass_db = self.calibration_settings()
            self.confirm_voltage_sweep(voltages, names=list(self.network.switches))
            self.calibration_plot_support()
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Calibration unavailable", str(error), parent=self)
            return

        def confirm_alignment(check):
            errors = check.routing_errors()
            if not errors:
                return True
            lines = "\n".join(f"In {source + 1}: expected at Out {expected + 1}, strongest at "
                              f"{'—' if measured is None else f'Out {measured + 1}'}"
                              for source, expected, measured in errors)
            return self._ask_on_main_thread(
                "Alignment errors",
                f"With every MZI at 0 V, some lines do not leave at their all-cross output:\n\n{lines}\n\n"
                "Check the input fibers, lasers and JDSU mapping. Continue the calibration anyway?",
                icon=messagebox.WARNING)

        def job(hooks):
            result = CompleteCalibration()
            try:
                calibrate_all(self.controller, self.osa_output_switch, read, self.network, voltages, settle_s,
                              wavelengths, half_window, verify, confirm_alignment, hooks["on_check"],
                              hooks["on_output"], hooks["on_probe"], hooks["on_point"],
                              lambda calibration: hooks["on_switch"](calibration, True),
                              hooks["stop_event"], result)
            finally:
                hooks["calibrations"].update(result.switches)
                hooks["checks"].extend(check for check in (result.alignment, *result.checks) if check is not None)

        self.run_mzi_job("complete", "Complete calibration: alignment check at 0 V...", job,
                         wavelengths, half_window, simulate, pass_db, voltages, settle_s)

    def calibration_plot_support(self):
        """Raise ImportError early when matplotlib is missing, like the other scans."""
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  # noqa: F401
        from matplotlib.figure import Figure  # noqa: F401

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
        for tab in (self.route_tab, self.hardware_tab, self.crosstalk_tab, self.dataset_tab, self.impulse_tab):
            self.set_tab_state(tab, state)
        self.set_osa_reading(running)

    def run_mzi_job(self, kind, message, job, wavelengths, half_window, simulate, pass_db, voltages, settle_s,
                    leaves_zero=True):
        """Run a calibration job on a worker thread, then save its results.

        ``job(hooks)`` fills ``hooks["calibrations"]`` and ``hooks["checks"]``
        and reports progress through the hook callbacks.
        """
        context = {
            "kind": kind,
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "comment": self.cal_comment_var.get().strip(),
            "laser_wavelengths_nm": {str(row + 1): wavelength for row, wavelength in wavelengths.items()},
            "line_half_window_nm": half_window,
            "voltages_v": list(voltages),
            "settle_s": settle_s,
            "reference": "every MZI not being swept at 0 V (cross)",
            "simulated": simulate,
            "switch_pins": dict(self.controller.settings["mapping"]),
            "calibration_before": {name: dict(value) for name, value in self.controller.settings["calibration"].items()},
            "jdsu_channel_map": dict(self.osa_output_switch.settings["channel_map"]),
        }
        self.mzi_calibration_running = True
        self.calibration_stop_event = stop_event = threading.Event()
        self.cal_pass_db = pass_db
        self.cal_job_verdicts = {}
        self.set_calibration_controls(True)
        self.cal_status_var.set(message)
        if kind == "complete":
            for row in self.cal_rows.values():
                row["checks"] = []
        hooks = {
            "stop_event": stop_event, "calibrations": {}, "checks": [],
            "on_output": lambda readout, index, total: self.after(
                0, lambda: self.calibration_output_read(readout, index, total)),
            "on_check": lambda check: self.after(0, lambda: self.calibration_check_done(check)),
            "on_probe": lambda probe: self.after(0, lambda: self.calibration_probe_started(probe)),
            "on_point": lambda probe, index, total, point: self.after(
                0, lambda: self.calibration_point(probe, index, total, point)),
            "on_switch": lambda calibration, saved: self.after(
                0, lambda: self.calibration_switch_done(calibration, saved)),
        }

        def worker():
            error, stopped, folder, save_error = None, False, None, None
            try:
                job(hooks)
            except CalibrationStopped:
                stopped = True
            except Exception as error_value:
                error = str(error_value)
            if hooks["calibrations"] or hooks["checks"]:
                status = "stopped" if stopped else "failed" if error else "complete"
                try:
                    folder = create_calibration_folder(app_dir(), kind)
                    save_calibration_results(folder, {**context, "status": status, "error": error},
                                             hooks["calibrations"], hooks["checks"], pass_db)
                except Exception as error_value:
                    save_error = str(error_value)
            self.after(0, lambda: self.finish_mzi_job(error, stopped, folder, save_error, leaves_zero))

        threading.Thread(target=worker, daemon=True).start()

    def finish_mzi_job(self, error, stopped, folder, save_error, leaves_zero):
        self.mzi_calibration_running = False
        self.calibration_stop_event = None
        self.set_calibration_controls(False)
        probe = self.cal_view["probe"]
        if probe is not None and self.cal_rows[probe.name]["status"] == "active":
            row = self.cal_rows[probe.name]
            row["status"] = "failed" if error else "saved" if None not in (row["cross"], row["bar"]) else "pending"
            self.show_calibration_row(probe.name)
        self.cal_view.update(probe=None, reading=None)
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
        self.cal_view["reading"] = readout.output
        self.cal_view["dominant"][readout.output] = strongest_input(readout.lines)
        self.cal_status_var.set(f"Read output {readout.output + 1} ({index}/{total}).")
        self.draw_calibration_diagram()

    def calibration_check_done(self, check):
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
        self.cal_view.update(states=dict(check.states), probe=None, reading=None,
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
        self.draw_calibration_diagram()

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

    def calibration_probe_started(self, probe):
        row = self.cal_rows[probe.name]
        row["status"] = "active"
        row["watch"] = f"Out {probe.output + 1} (In {probe.cross_input + 1} X / In {probe.straight_input + 1} =)"
        self.cal_job_verdicts.pop(probe.name, None)
        self.show_calibration_row(probe.name)
        self.cal_view.update(probe=probe, reading=probe.output, states=reference_states(self.network))
        self.cal_view["note"] = (f"{probe.name}: watching Out {probe.output + 1}; In {probe.cross_input + 1} "
                                 f"arrives there when cross, In {probe.straight_input + 1} when bar.")
        self.cal_status_var.set(self.cal_view["note"])
        self.add_calibration_plot(probe)
        self.draw_calibration_diagram()

    def calibration_point(self, probe, index, total, point):
        if point.contrast_db is not None:
            # Draw the light where the measurement says it currently goes.
            self.cal_view["states"][probe.name] = point.contrast_db >= 0
        number = lambda value: "—" if value is None else f"{value:.1f}"
        self.cal_status_var.set(
            f"{probe.name}: {index}/{total} at {point.voltage:.2f} V · In {probe.cross_input + 1} "
            f"{number(point.cross_dbm)} dBm, In {probe.straight_input + 1} {number(point.straight_dbm)} dBm")
        plot = self.cal_plot_items.get(probe.name)
        if plot is not None:
            plot["points"].append(point)
            voltages = [item.voltage for item in plot["points"]]
            for key, attribute in (("cross_line", "cross_dbm"), ("straight_line", "straight_dbm"),
                                   ("contrast_line", "contrast_db")):
                values = [getattr(item, attribute) for item in plot["points"]]
                plot[key].set_data(voltages, [float("nan") if value is None else value for value in values])
            for axis in plot["axes"]:
                axis.relim()
                axis.autoscale_view()
            plot["canvas"].draw_idle()
        self.draw_calibration_diagram()

    def calibration_switch_done(self, calibration, saved):
        name = calibration.name
        row = self.cal_rows[name]
        row.update(status="calibrated" if saved else "saved" if row["cross"] is not None else "pending",
                   cross_margin=calibration.cross_margin_db, bar_margin=calibration.straight_margin_db)
        if saved:
            row.update(cross=calibration.cross.voltage, bar=calibration.straight.voltage)
        self.show_calibration_row(name)
        self.cal_view["states"][name] = True
        self.cal_view["note"] = (f"{name}: cross {calibration.cross.voltage:.2f} V, bar "
                                 f"{calibration.straight.voltage:.2f} V" + ("" if saved else " (not saved)"))
        plot = self.cal_plot_items.get(name)
        if plot is not None:
            for axis in plot["axes"]:
                axis.axvline(calibration.cross.voltage, color=ACCENT, linestyle="--", linewidth=1.2,
                             label=f"Cross {calibration.cross.voltage:.2f} V")
                axis.axvline(calibration.straight.voltage, color="#e0a100", linestyle="--", linewidth=1.2,
                             label=f"Bar {calibration.straight.voltage:.2f} V")
            plot["axes"][1].legend(loc="best", fontsize=8)
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
        lines_axis = figure.add_subplot(211)
        contrast_axis = figure.add_subplot(212, sharex=lines_axis)
        cross_line, = lines_axis.plot([], [], marker=".", linewidth=1.4, color=WDM_COLORS[probe.cross_input],
                                      label=f"In {probe.cross_input + 1} (arrives if cross)")
        straight_line, = lines_axis.plot([], [], marker=".", linewidth=1.4, color=WDM_COLORS[probe.straight_input],
                                         label=f"In {probe.straight_input + 1} (arrives if bar)")
        lines_axis.set(ylabel="Peak [dBm]", title=f"{probe.name}: lines at output {probe.output + 1} "
                                                    f"(fiber {fiber_number(probe.output + 1)})")
        lines_axis.legend(loc="best", fontsize=8)
        lines_axis.grid(True)
        contrast_line, = contrast_axis.plot([], [], marker=".", linewidth=1.4, color="#315b78")
        contrast_axis.axhline(0, color="#9baec6", linewidth=0.8)
        contrast_axis.set(xlabel="Voltage [V]", ylabel="Cross − bar line [dB]")
        contrast_axis.grid(True)
        figure.tight_layout()
        canvas = FigureCanvasTkAgg(figure, master=tab)
        toolbar = NavigationToolbar2Tk(canvas, tab)
        toolbar.update()
        canvas.get_tk_widget().configure(**PLOT_MIN_SIZE)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        self.cal_plot_items[probe.name] = {"tab": tab, "canvas": canvas, "axes": (lines_axis, contrast_axis),
                                           "cross_line": cross_line, "straight_line": straight_line,
                                           "contrast_line": contrast_line, "points": []}

    def draw_calibration_diagram(self):
        """Draw the network with each laser's expected path in its input colour and each MZI's status."""
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
        if probe is None and not self.mzi_calibration_running:
            # Preview the selected MZI and the output it would be watched on.
            probe = self.selected_watch_probe() if hasattr(self, "cal_watch_probes") else None
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

        wavelengths = self.calibration_wavelengths()
        routes = {row: self.network.trace(row, states) for row in wavelengths}
        if probe is not None:
            # Where the probed MZI's two lines would go in its other state.
            other = {**states, probe.name: not states[probe.name]}
            for row, route in routes.items():
                if any(name == probe.name for name, _ in route.states):
                    c.create_line(*self.route_points(self.network.trace(row, other), left_x + 14, right_x - 14,
                                                     xs, ys, half),
                                  fill=WDM_COLORS[row], width=2, dash=(6, 4), joinstyle="round", capstyle="round")
        for row, route in routes.items():
            c.create_line(*self.route_points(route, left_x + 14, right_x - 14, xs, ys, half),
                          fill=WDM_COLORS[row], width=3, joinstyle="round", capstyle="round")

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
                c.create_text(x, ys[a] - 16, text=name, fill=ZERO_VOLT_COLOR if active else color,
                              font=("Segoe UI", 8, "bold"))
                margins = [margin for _, _, margin in self.cal_rows[name]["checks"] if margin is not None]
                if margins:
                    c.create_text(x, ys[b] + 16, text=f"{min(margins):.0f} dB", fill=color, font=font)

        watched = None if probe is None else probe.output
        for row, y in enumerate(ys):
            wavelength = wavelengths.get(row)
            color = WDM_COLORS[row] if wavelength is not None else WIRE
            c.create_oval(left_x - 13, y - 13, left_x + 13, y + 13, fill=PANEL, outline=color,
                          width=3 if wavelength is not None else 1.5)
            c.create_text(left_x, y, text=str(row + 1), fill=TEXT, font=("Segoe UI", 9, "bold"))
            c.create_text(left_x - 20, y, anchor="e", font=font, fill=color if wavelength is not None else "#71829a",
                          text=f"In {row + 1}\n" + ("no laser" if wavelength is None else f"{wavelength:g} nm"))
            dominant = view["dominant"].get(row)
            c.create_oval(right_x - 13, y - 13, right_x + 13, y + 13, outline=WIRE, width=1.5,
                          fill=PANEL if dominant is None else WDM_COLORS[dominant])
            c.create_text(right_x, y, text=str(row + 1), fill=TEXT if dominant is None else BG,
                          font=("Segoe UI", 9, "bold"))
            label = f"Out {row + 1}" + ("" if dominant is None else f"\nmeas. In {dominant + 1}")
            c.create_text(right_x + 20, y, anchor="w", text=label, fill="#9baec6", font=font)
            for radius, ring, color in ((18, view["reading"], OSA_COLOR), (23, watched, ZERO_VOLT_COLOR)):
                if ring == row:
                    c.create_oval(right_x - radius, y - radius, right_x + radius, y + radius, outline=color, width=2.5)
        c.create_text(width / 2, height - 10, text=view["note"], fill="#9baec6", font=font)
