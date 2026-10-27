"""Dataset coverage tab: linked dataset, batch acquisition and coverage reports."""

import csv
import threading
import tkinter as tk
from datetime import datetime
from itertools import product
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from benes.gui.theme import ACCENT, PANEL, POST_SWITCH_SWEEPS, TEXT, WDM_COLORS, WIRE
from benes.gui.widgets import VerticalScrolledFrame
from benes.hardware import osa
from benes.hardware.osa_output_switch import OUTPUTS
from benes.measurements.dataset_registry import (analyze_dataset, create_dataset, save_measurement,
                                                 write_coverage_report)
from benes.measurements.spectrum_export import write_spectra
from benes.measurements.wdm_comb import (INPUTS, create_wdm_folder, evaluate_configuration,
                                         routed_wavelengths, save_wdm_scan, scan_outputs,
                                         simulated_comb_spectrum)
from benes.paths import app_dir


class DatasetMixin:
    """Mixed into ``BenesApp``; relies on the state it sets up."""

    def build_dataset_tab(self):
        """Build the durable measurement index and coverage view.

        The raw OSA/power-meter CSV is deliberately stored next to a small
        metadata file.  File names alone cannot tell whether a trace represents
        one route, a fan-in measurement, or a full parallel acquisition.
        """
        controls_column = ttk.Frame(self.dataset_tab, padding=(18, 18, 8, 18), width=335)
        controls_column.pack(side="left", fill="y")
        controls_column.pack_propagate(False)
        control_scroll = VerticalScrolledFrame(controls_column)
        control_scroll.pack(fill="both", expand=True)
        controls = ttk.Frame(control_scroll.content, padding=(0, 0, 12, 0))
        controls.pack(anchor="nw", fill="x")
        summary = ttk.Frame(self.dataset_tab, padding=(8, 18, 18, 18))
        summary.pack(side="left", fill="both", expand=True)

        self.dataset_folder_var = tk.StringVar()
        self.dataset_mode_var = tk.StringVar(value="Routed pairs")
        self.dataset_comment_var = tk.StringVar()
        self.dataset_status_var = tk.StringVar(value="Choose or initialize a dataset folder.")

        ttk.Label(controls, text="MEASUREMENT DATASET", foreground=ACCENT,
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(controls, text="Each saved record includes the active permutation and the input/output ports it represents.",
                  wraplength=285).pack(anchor="w", pady=(4, 12))
        self.build_dataset_scan_configuration(controls)

        folder_box = ttk.LabelFrame(controls, text="Dataset folder", padding=10)
        folder_box.pack(fill="x")
        ttk.Entry(folder_box, textvariable=self.dataset_folder_var, width=34).pack(fill="x")
        folder_buttons = ttk.Frame(folder_box)
        folder_buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(folder_buttons, text="Choose folder…", command=self.choose_dataset_folder).pack(
            side="left", fill="x", expand=True)
        ttk.Button(folder_buttons, text="Analyse", style="Accent.TButton",
                   command=self.analyze_linked_dataset).pack(side="left", padx=(6, 0))
        ttk.Button(folder_box, text="Initialize full 720-permutation set",
                   command=lambda: self.initialize_dataset("full_permutations")).pack(
            fill="x", pady=(8, 0))
        ttk.Button(folder_box, text="Initialize active permutation only",
                   command=lambda: self.initialize_dataset("active_permutation")).pack(
            fill="x", pady=(4, 0))

        selection = ttk.LabelFrame(controls, text="What this measurement represents", padding=10)
        selection.pack(fill="x", pady=(12, 0))
        ttk.Label(selection, text="Select one or many ports. Ctrl-click selects several.").grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(selection, text="Inputs").grid(row=1, column=0, sticky="w", pady=(8, 3))
        ttk.Label(selection, text="Outputs").grid(row=1, column=1, sticky="w", padx=(12, 0), pady=(8, 3))
        self.dataset_input_list = tk.Listbox(selection, selectmode="extended", exportselection=False,
                                             height=6, width=12, bg=PANEL, fg=TEXT,
                                             selectbackground="#2c796b", selectforeground=TEXT,
                                             highlightthickness=1, highlightbackground=WIRE,
                                             activestyle="none", borderwidth=0)
        self.dataset_output_list = tk.Listbox(selection, selectmode="extended", exportselection=False,
                                              height=6, width=12, bg=PANEL, fg=TEXT,
                                              selectbackground="#2c796b", selectforeground=TEXT,
                                              highlightthickness=1, highlightbackground=WIRE,
                                              activestyle="none", borderwidth=0)
        for port in range(1, 7):
            self.dataset_input_list.insert("end", f"In {port}")
            self.dataset_output_list.insert("end", f"Out {port}")
        self.dataset_input_list.selection_set(0)
        self.dataset_output_list.selection_set(0)
        self.dataset_input_list.grid(row=2, column=0, sticky="w")
        self.dataset_output_list.grid(row=2, column=1, sticky="w", padx=(12, 0))
        ttk.Label(selection, text="Connection rule").grid(row=3, column=0, sticky="w", pady=(10, 3))
        ttk.Combobox(selection, textvariable=self.dataset_mode_var,
                     values=("Routed pairs", "All selected pairs"), state="readonly", width=22).grid(
            row=4, column=0, columnspan=2, sticky="ew")
        ttk.Label(selection, text="Routed pairs records only active Benes routes. All selected pairs is for a future fan-in/fan-out fixture.",
                  wraplength=280).grid(row=5, column=0, columnspan=2, sticky="w", pady=(5, 0))

        batch = ttk.LabelFrame(controls, text="Fill dataset from hardware", padding=10)
        batch.pack(fill="x", pady=(12, 0))
        ttk.Label(batch, text=("Uses the saved six-laser OSA configuration and records all six routed pairs at "
                               "each control state."), wraplength=280).pack(anchor="w")
        self.dataset_active_scan_button = ttk.Button(
            batch, text="Scan active control state", style="Accent.TButton",
            command=lambda: self.start_dataset_batch("active"))
        self.dataset_active_scan_button.pack(fill="x", pady=(8, 0))
        self.dataset_all_scan_button = ttk.Button(
            batch, text="Run all 720 output permutations", command=lambda: self.start_dataset_batch("all"))
        self.dataset_all_scan_button.pack(fill="x", pady=(5, 0))
        self.dataset_states_scan_button = ttk.Button(
            batch, text="Run all 4096 switch states", command=lambda: self.start_dataset_batch("states"))
        self.dataset_states_scan_button.pack(fill="x", pady=(5, 0))
        self.dataset_batch_stop_button = ttk.Button(batch, text="Stop after current output", command=self.stop_dataset_batch,
                                                    state="disabled")
        self.dataset_batch_stop_button.pack(fill="x", pady=(5, 0))

        save_box = ttk.LabelFrame(controls, text="Save latest acquisition", padding=10)
        save_box.pack(fill="x", pady=(12, 0))
        ttk.Label(save_box, text="Comment (saved inside the record and CSV)").pack(anchor="w")
        ttk.Entry(save_box, textvariable=self.dataset_comment_var).pack(fill="x", pady=(3, 8))
        ttk.Button(save_box, text="Save latest OSA trace(s)", style="Accent.TButton",
                   command=self.save_dataset_osa).pack(fill="x")
        ttk.Button(save_box, text="Save latest power reading", command=self.save_dataset_power).pack(
            fill="x", pady=(5, 0))
        ttk.Label(save_box, text="Use Route + read / Read all outputs before saving OSA data. The selected ports define its coverage.",
                  wraplength=280).pack(anchor="w", pady=(8, 0))
        ttk.Label(controls, textvariable=self.dataset_status_var, wraplength=285,
                  foreground="#9baec6").pack(anchor="w", pady=(12, 4))

        ttk.Label(summary, text="Coverage", foreground=ACCENT,
                  font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(summary, text="The report counts the six routed input→output paths required for each expected permutation.",
                  wraplength=700).pack(anchor="w", pady=(2, 8))
        ttk.Label(summary, text="Live normalized optical metrics", foreground=ACCENT,
                  font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(4, 2))
        ttk.Label(summary, text=("Each row is one input wavelength at one switch state. Transmission and crosstalk "
                                 "are normalized by that wavelength's linear power summed across all six outputs."),
                  foreground="#9baec6", wraplength=700).pack(anchor="w", pady=(0, 4))
        metrics_frame = ttk.Frame(summary)
        metrics_frame.pack(fill="x", pady=(0, 10))
        metric_columns = ("state", "permutation", "input", "wavelength", "total", "expected", "transmission") + \
                         tuple(f"out{port}" for port in OUTPUTS)
        self.dataset_metrics_table = ttk.Treeview(metrics_frame, columns=metric_columns, show="headings", height=8)
        headings = {"state": "Switch state", "permutation": "Outputs", "input": "Input", "wavelength": "λ [nm]",
                    "total": "Total [dBm]", "expected": "Expected", "transmission": "Tx / total [%]"}
        headings.update({f"out{port}": f"Out {port} XT [%]" for port in OUTPUTS})
        widths = {"state": 125, "permutation": 110, "input": 60, "wavelength": 85, "total": 85,
                  "expected": 75, "transmission": 95, **{f"out{port}": 80 for port in OUTPUTS}}
        for column in metric_columns:
            self.dataset_metrics_table.heading(column, text=headings[column])
            self.dataset_metrics_table.column(column, width=widths[column], anchor="center")
        metrics_scroll = ttk.Scrollbar(metrics_frame, orient="horizontal", command=self.dataset_metrics_table.xview,
                                       style="Accent.Horizontal.TScrollbar")
        self.dataset_metrics_table.configure(xscrollcommand=metrics_scroll.set)
        self.dataset_metrics_table.pack(fill="x")
        metrics_scroll.pack(fill="x")
        report_frame = ttk.Frame(summary)
        report_frame.pack(fill="both", expand=True)
        report_scroll = ttk.Scrollbar(report_frame, orient="vertical", style="Accent.Vertical.TScrollbar")
        self.dataset_report = tk.Text(report_frame, wrap="word", bg=PANEL, fg=TEXT, insertbackground=TEXT,
                                      font=("Consolas", 10), borderwidth=0, highlightthickness=1,
                                      highlightbackground=WIRE, padx=12, pady=10, state="disabled")
        report_scroll.configure(command=self.dataset_report.yview)
        self.dataset_report.configure(yscrollcommand=report_scroll.set)
        self.dataset_report.pack(side="left", fill="both", expand=True)
        report_scroll.pack(side="right", fill="y")
        self.set_dataset_report("No dataset linked. Choose a folder, initialize its expected permutation set, then save OSA or power-meter acquisitions.")

    def build_dataset_scan_configuration(self, controls):
        """Keep the six-laser scan settings with the dataset they describe."""
        settings = self.wdm_plan.settings
        self.wdm_wavelength_vars = {
            port: tk.StringVar(value="" if settings["assignment"][str(port)] is None
                               else f"{settings['assignment'][str(port)]:g}")
            for port in INPUTS
        }
        self.wdm_destination_vars = {port: tk.StringVar() for port in INPUTS}
        self.wdm_comb_var = tk.StringVar(value=" ".join(f"{line:g}" for line in settings["comb_lines"]))
        self.wdm_window_var = tk.StringVar(value=f"{settings['half_window_nm']:g}")
        self.wdm_settle_var = tk.StringVar(value=f"{settings['settle_s']:g}")
        self.wdm_output_vars = {output: tk.BooleanVar(value=True) for output in OUTPUTS}
        self.wdm_comment_var = self.dataset_comment_var
        self.wdm_dataset_var = tk.BooleanVar(value=True)
        self.wdm_plot_var = tk.BooleanVar(value=False)
        self.wdm_status_var = tk.StringVar(value="Configure the six laser wavelengths, then evaluate from Routing.")
        self.wdm_wavelength_boxes = []

        scan = ttk.LabelFrame(controls, text="Six-laser OSA scan configuration", padding=10)
        scan.pack(fill="x", pady=(12, 0))
        ttk.Label(scan, text="Comb lines [nm]").grid(row=0, column=0, sticky="w")
        comb = ttk.Entry(scan, textvariable=self.wdm_comb_var, width=20)
        comb.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        for row, port in enumerate(INPUTS, start=1):
            ttk.Label(scan, text=f"In {port}", foreground=WDM_COLORS[port - 1]).grid(
                row=row, column=0, sticky="w", pady=2)
            entry = ttk.Entry(scan, textvariable=self.wdm_wavelength_vars[port], width=12)
            entry.grid(row=row, column=1, sticky="w", padx=(8, 0), pady=2)
            self.wdm_wavelength_vars[port].trace_add(
                "write", lambda *_: (self.update_wdm_routing(), self.draw()))
        ttk.Label(scan, text="Line window ± [nm]").grid(row=7, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(scan, textvariable=self.wdm_window_var, width=8).grid(
            row=7, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Label(scan, text="OSA/JDSU settling [s]").grid(row=8, column=0, sticky="w", pady=2)
        ttk.Entry(scan, textvariable=self.wdm_settle_var, width=8).grid(
            row=8, column=1, sticky="w", padx=(8, 0), pady=2)
        ttk.Button(scan, text="Save six-laser configuration", command=self.save_wdm_plan).grid(
            row=9, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(scan, text=("Evaluate Configuration always scans all six outputs using this saved plan. "
                              "Dataset Coverage has separate batch acquisition actions."), foreground="#9baec6",
                  wraplength=280).grid(row=10, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(scan, textvariable=self.wdm_status_var, foreground="#9baec6", wraplength=280).grid(
            row=11, column=0, columnspan=2, sticky="w", pady=(6, 0))
        comb.bind("<FocusOut>", lambda _event: self.update_wdm_comb_choices())
        self.update_wdm_routing()

    def choose_dataset_folder(self):
        folder = filedialog.askdirectory(parent=self, title="Choose Benes measurement dataset")
        if not folder:
            return
        self.dataset_folder_var.set(folder)
        path = Path(folder) / "benes_dataset.json"
        if path.exists():
            self.analyze_linked_dataset()
        else:
            self.dataset_status_var.set("Folder selected. Initialize the full set or the active permutation.")

    def active_dataset_permutation(self):
        return tuple(output + 1 for output in self.network.mapping())

    def dataset_selection(self):
        inputs = [index + 1 for index in self.dataset_input_list.curselection()]
        outputs = [index + 1 for index in self.dataset_output_list.curselection()]
        if not inputs or not outputs:
            raise ValueError("Select at least one input and one output port")
        mode = "routed_pairs" if self.dataset_mode_var.get() == "Routed pairs" else "cartesian"
        return inputs, outputs, mode

    def dataset_root(self):
        value = self.dataset_folder_var.get().strip()
        if not value:
            raise ValueError("Choose a dataset folder first")
        return Path(value)

    def start_dataset_batch(self, scope):
        """Scan and index either the present controls or every six-port permutation."""
        if self.dataset_batch_running or self.wdm_running:
            return
        try:
            root = self.dataset_root()
            if not (root / "benes_dataset.json").exists():
                raise ValueError("Initialize the dataset before filling it from hardware")
            assignment = dict(self.wdm_plan.assignment)
            missing_wavelengths = [str(port) for port in INPUTS if assignment[str(port)] is None]
            if missing_wavelengths:
                raise ValueError("Save a wavelength for every input before filling the dataset")
            if self.osa_button.instate(["disabled"]):
                raise RuntimeError("Wait for the current OSA trace read to finish")
            host, port, simulate = self.osa_connection()
            if not self.controller.connected:
                raise RuntimeError("Connect the voltage board before filling the dataset")
            if self.controller.settings["dry_run"] != simulate:
                raise RuntimeError("Use the physical voltage board with the real OSA, or dry-run board with OSA Simulate")
            switch = self.osa_output_switch
            if not switch.connected:
                raise RuntimeError("Connect the OSA output switch before filling the dataset")
            for output in OUTPUTS:
                switch.channel_for(output)
            missing_calibration = [name for name in self.network.switches
                                   if self.controller.settings["calibration"][name]["cross"] is None or
                                   self.controller.settings["calibration"][name]["straight"] is None]
            if missing_calibration:
                raise ValueError("Calibrate every MZI before automatically applying control states")
            batch_sizes = {"all": "720 output permutations", "states": "4096 switch states"}
            if scope in batch_sizes and not messagebox.askyesno(
                    f"Run all {batch_sizes[scope]}",
                    f"This will drive and measure all {batch_sizes[scope]}, with six OSA output scans each. "
                    "It can take a long time. Continue?", parent=self):
                return
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Dataset batch unavailable", str(error), parent=self)
            return

        original_states = dict(self.network.states)
        original_zero = set(self.zero_volt_switches)
        if scope == "active":
            configurations = [(self.active_dataset_permutation(), dict(self.network.states))]
        elif scope == "all":
            configurations = self.all_permutation_control_states()
        else:
            configurations = self.all_switch_control_states()
        settings = self.wdm_plan.settings
        comment = self.dataset_comment_var.get()
        self.dataset_batch_running = True
        self.dataset_batch_stop_event = threading.Event()
        self.dataset_active_scan_button.configure(state="disabled")
        self.dataset_all_scan_button.configure(state="disabled")
        self.dataset_states_scan_button.configure(state="disabled")
        self.dataset_batch_stop_button.configure(state="normal")
        self.evaluate_button.configure(state="disabled")
        self.set_osa_reading(True)
        for tab in (self.route_tab, self.hardware_tab, self.calibration_tab, self.crosstalk_tab, self.impulse_tab):
            self.set_tab_state(tab, "disabled")
        self.dataset_metrics_table.delete(*self.dataset_metrics_table.get_children())
        self.dataset_status_var.set(f"Preparing {len(configurations)} configuration(s) for acquisition...")
        stop_event = self.dataset_batch_stop_event

        def worker():
            saved, error = 0, None
            try:
                for index, (permutation, states) in enumerate(configurations, start=1):
                    if stop_event.is_set():
                        break
                    self.controller.apply_calibrated_states(states)
                    self.network.states.update(states)
                    self.zero_volt_switches.clear()
                    mapping = self.network.mapping()
                    routed = routed_wavelengths(mapping, assignment)
                    self.after(0, lambda i=index, total=len(configurations): self.dataset_status_var.set(
                        f"Configuration {i}/{total}: scanning six outputs..."))

                    def read(output):
                        if simulate:
                            return simulated_comb_spectrum(assignment, routed[output][0])
                        return osa.get_spectrum(host, port, fresh_sweeps=POST_SWITCH_SWEEPS)

                    results = scan_outputs(switch, read, OUTPUTS, mapping, assignment, settings["half_window_nm"],
                                           settings["settle_s"], stop_event=stop_event)
                    if len(results) != len(OUTPUTS):
                        break
                    self.after(0, lambda p=permutation, s=dict(states), r=results:
                               self.add_dataset_batch_metrics(p, s, r, assignment))
                    context = {
                        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "permutation": list(permutation), "assignment": assignment,
                        "half_window_nm": settings["half_window_nm"], "settle_s": settings["settle_s"],
                        "switch_states": {name: "cross" if crossed else "bar" for name, crossed in states.items()},
                        "simulated_osa": simulate, "batch_scope": scope,
                    }
                    folder = create_wdm_folder(app_dir())
                    files = save_wdm_scan(folder, results, context, comment)
                    record = save_measurement(
                        root, source="OSA WDM", permutation=permutation, input_ports=list(INPUTS),
                        output_ports=list(OUTPUTS), mode="routed_pairs", files=files, comment=comment,
                        extra={"results_folder": str(folder), "batch_scope": scope})
                    save_wdm_scan(record, results, context, comment)
                    saved += 1
            except Exception as error_value:
                error = str(error_value)
            finally:
                try:
                    self.controller.apply_calibrated_states(original_states)
                except Exception as restore_error:
                    error = error or f"Could not restore the original control state: {restore_error}"
                self.network.states.update(original_states)
                self.zero_volt_switches = original_zero
            self.after(0, lambda: self.finish_dataset_batch(saved, len(configurations), error, stop_event.is_set()))

        threading.Thread(target=worker, daemon=True).start()

    def all_permutation_control_states(self):
        """One valid MZI state map for each of the 720 output permutations."""
        configurations = {}
        names = tuple(self.network.switches)
        for bits in product((False, True), repeat=len(names)):
            states = dict(zip(names, bits))
            configurations.setdefault(tuple(output + 1 for output in self.network.mapping(states)), states)
        return sorted(configurations.items())

    def all_switch_control_states(self):
        """Every one of the 2**12 physical MZI bar/cross configurations."""
        names = tuple(self.network.switches)
        return [(tuple(output + 1 for output in self.network.mapping(states)), states)
                for bits in product((False, True), repeat=len(names))
                for states in (dict(zip(names, bits)),)]

    def add_dataset_batch_metrics(self, permutation, states, results, assignment):
        """Append normalized per-wavelength metrics for one measured switch state."""
        fmt = lambda value: "—" if value is None else f"{value:.2f}"
        state = " ".join(f"{name}={'X' if states[name] else '='}" for name in self.network.switches)
        mapping = tuple(output - 1 for output in permutation)
        for item in evaluate_configuration(results, mapping, assignment):
            crosstalk = ["—" if port == item.expected_output else fmt(
                None if item.normalized_crosstalk.get(port) is None else item.normalized_crosstalk[port] * 100)
                         for port in OUTPUTS]
            self.dataset_metrics_table.insert("", "end", values=(
                state, " ".join(map(str, permutation)), f"In {item.input_port}", f"{item.wavelength_nm:.3f}",
                fmt(item.total_power_dbm), f"Out {item.expected_output}",
                fmt(None if item.normalized_transmission is None else item.normalized_transmission * 100),
                *crosstalk))

    def stop_dataset_batch(self):
        if self.dataset_batch_stop_event is not None:
            self.dataset_batch_stop_event.set()
            self.dataset_status_var.set("Stopping after the current OSA output...")

    def finish_dataset_batch(self, saved, total, error, stopped):
        self.dataset_batch_running = False
        self.dataset_batch_stop_event = None
        self.dataset_active_scan_button.configure(state="normal")
        self.dataset_all_scan_button.configure(state="normal")
        self.dataset_states_scan_button.configure(state="normal")
        self.dataset_batch_stop_button.configure(state="disabled")
        self.evaluate_button.configure(state="normal")
        self.set_osa_reading(False)
        for tab in (self.route_tab, self.hardware_tab, self.calibration_tab, self.crosstalk_tab, self.impulse_tab):
            self.set_tab_state(tab, "normal")
        self.refresh(keep_selection=True)
        if error:
            self.dataset_status_var.set(f"Dataset batch failed after {saved}/{total} configuration(s): {error}")
            messagebox.showerror("Dataset batch failed", error, parent=self)
        else:
            state = "stopped" if stopped else "complete"
            self.dataset_status_var.set(f"Dataset batch {state}: recorded {saved}/{total} configuration(s).")
        self.analyze_linked_dataset()

    def initialize_dataset(self, scope):
        try:
            root = self.dataset_root()
            expected = None if scope == "full_permutations" else [self.active_dataset_permutation()]
            dataset = create_dataset(root, scope=scope, expected_permutations=expected)
        except (OSError, ValueError) as error:
            messagebox.showerror("Dataset setup failed", str(error), parent=self)
            return
        self.dataset_status_var.set(
            f"Dataset ready: {len(dataset['expected_permutations'])} expected permutation(s).")
        self.analyze_linked_dataset()

    def save_dataset_osa(self):
        try:
            root = self.dataset_root()
            inputs, outputs, mode = self.dataset_selection()
            traces = [item for item in self.osa_trace_records if item["output_port"] in outputs]
            recorded_outputs = {item["output_port"] for item in traces}
            missing_outputs = sorted(set(outputs) - recorded_outputs)
            if missing_outputs:
                ports = ", ".join(f"Out {port}" for port in missing_outputs)
                raise ValueError(f"No current OSA trace is available for {ports}. Read each selected output first.")
            files = ["osa_spectra.csv"]
            folder = save_measurement(
                root, source="OSA", permutation=self.active_dataset_permutation(), input_ports=inputs,
                output_ports=outputs, mode=mode, files=files, comment=self.dataset_comment_var.get(),
                extra={"trace_count": len(traces),
                       "trace_outputs": [item["output_port"] for item in traces]},
            )
            write_spectra(folder / files[0], [(item["name"], item["wave"], item["power"]) for item in traces],
                          self.dataset_comment_var.get())
        except (OSError, ValueError) as error:
            messagebox.showerror("OSA dataset save failed", str(error), parent=self)
            return
        self.dataset_status_var.set(f"Saved {len(traces)} OSA trace(s) in {folder.name}.")
        self.analyze_linked_dataset()

    def save_dataset_power(self):
        try:
            root = self.dataset_root()
            inputs, outputs, mode = self.dataset_selection()
            reading = self.power_meter.last_reading
            if reading is None:
                raise ValueError("Read the current Arduino power first")
            files = ["power_reading.csv"]
            folder = save_measurement(
                root, source="Power meter", permutation=self.active_dataset_permutation(), input_ports=inputs,
                output_ports=outputs, mode=mode, files=files, comment=self.dataset_comment_var.get(),
                extra={"value": reading.value, "unit": reading.unit, "raw_mean": reading.raw_mean,
                       "sample_count": reading.sample_count},
            )
            with (folder / files[0]).open("w", encoding="utf-8", newline="") as file:
                for line in self.dataset_comment_var.get().splitlines() or [""]:
                    file.write(f"# Comment: {line}\n")
                writer = csv.writer(file)
                writer.writerow(["value", "unit", "raw_mean", "sample_count"])
                writer.writerow([reading.value, reading.unit, reading.raw_mean, reading.sample_count])
        except (OSError, ValueError) as error:
            messagebox.showerror("Power dataset save failed", str(error), parent=self)
            return
        self.dataset_status_var.set(f"Saved power reading in {folder.name}.")
        self.analyze_linked_dataset()

    def analyze_linked_dataset(self):
        try:
            root = self.dataset_root()
            report = analyze_dataset(root)
            csv_path = write_coverage_report(root, report)
        except (OSError, ValueError) as error:
            messagebox.showerror("Dataset analysis failed", str(error), parent=self)
            return
        active = self.active_dataset_permutation()
        active_item = next((item for item in report["configurations"] if item["permutation"] == active), None)
        format_pair = lambda pair: f"In{pair[0]}→Out{pair[1]}"
        lines = [
            f"Dataset: {root}",
            f"Measurements saved: {report['measurement_count']}",
            f"Configurations complete: {report['complete_configurations']} / {report['expected_configurations']}",
            f"Routed paths covered: {report['covered_paths']} / {report['expected_paths']}  •  missing {report['missing_paths']}",
            f"Detailed CSV: {csv_path.name}",
            "",
        ]
        if active_item is None:
            lines.extend((f"Active permutation: {' '.join(map(str, active))}",
                          "It is not in this dataset's expected scope."))
        elif active_item["complete"]:
            lines.extend((f"Active permutation: {' '.join(map(str, active))}", "All six routed paths are covered."))
        else:
            lines.extend((f"Active permutation: {' '.join(map(str, active))}",
                          "Still required: " + ", ".join(map(format_pair, active_item["missing"]))))
        incomplete = [item for item in report["configurations"] if not item["complete"]]
        if incomplete:
            lines.extend(("", "Other incomplete configurations (first 30):"))
            for item in incomplete[:30]:
                permutation = " ".join(map(str, item["permutation"]))
                missing = ", ".join(map(format_pair, item["missing"]))
                lines.append(f"  {permutation}: {missing}")
            if len(incomplete) > 30:
                lines.append(f"  … plus {len(incomplete) - 30} more in {csv_path.name}")
        self.set_dataset_report("\n".join(lines))
        self.dataset_status_var.set(f"Coverage analysed: {report['missing_paths']} routed path(s) still missing.")

    def set_dataset_report(self, text):
        self.dataset_report.configure(state="normal")
        self.dataset_report.delete("1.0", "end")
        self.dataset_report.insert("1.0", text)
        self.dataset_report.configure(state="disabled")
