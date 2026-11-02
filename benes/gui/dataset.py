"""Dataset coverage tab: linked dataset, batch acquisition and coverage reports."""

import csv
import json
import threading
import tkinter as tk
from datetime import datetime
from itertools import product
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from benes.gui.theme import ACCENT, BG, PANEL, POST_SWITCH_SWEEPS, TEXT, WDM_COLORS, WIRE
from benes.gui.widgets import VerticalScrolledFrame, cap_width
from benes.hardware import osa
from benes.hardware.osa_output_switch import OUTPUTS
from benes.measurements.dataset_registry import (analyze_dataset, create_dataset, save_measurement,
                                                 write_coverage_report)
from benes.measurements.dataset_view import (format_transmission_share, load_output_spectra, load_target_summary,
                                             saved_dataset_scans, saved_scan_entry)
from benes.measurements.spectrum_export import write_spectra
from benes.measurements.wdm_comb import (INPUTS, create_wdm_folder, evaluate_configuration,
                                         power_ratio_db, routed_wavelengths, save_wdm_scan, scan_outputs,
                                         simulated_comb_spectrum)
from benes.paths import app_dir


# Remembers the last dataset folder, so the next session links it again.
DATASET_CONFIG = "benes_dataset_config.json"


class DatasetMixin:
    """Mixed into ``BenesApp``; relies on the state it sets up."""

    def build_dataset_tab(self):
        """Build the durable measurement index and coverage view.

        The raw OSA/power-meter CSV is deliberately stored next to a small
        metadata file.  File names alone cannot tell whether a trace represents
        one route, a fan-in measurement, or a full parallel acquisition.
        """
        self.dataset_scan_views = {}
        self.dataset_scan_entries = {}
        self.dataset_scan_latest = None
        self.dataset_scan_viewer = None
        controls_column = ttk.Frame(self.dataset_tab, padding=(18, 18, 8, 18))
        controls_column.pack(side="left", fill="y")
        control_scroll = VerticalScrolledFrame(controls_column)
        control_scroll.pack(fill="both", expand=True)
        controls = ttk.Frame(control_scroll.content, padding=(0, 0, 12, 0))
        controls.pack(anchor="nw", fill="x")
        # Both columns scroll on their own, so the tab itself never needs a second scrollbar.
        summary_scroll = VerticalScrolledFrame(self.dataset_tab)
        summary_scroll.pack(side="left", fill="both", expand=True)
        summary = ttk.Frame(summary_scroll.content, padding=(8, 18, 18, 18))
        summary.pack(fill="both", expand=True)

        self.dataset_folder_var = tk.StringVar(value=self.last_dataset_folder())
        # Any existing folder put in the field, typed or chosen, becomes next start's default.
        self.dataset_folder_var.trace_add("write", lambda *_: self.dataset_folder_changed())
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
        ttk.Button(batch, text="Live optical viewer…", command=self.show_dataset_scan_viewer).pack(
            fill="x", pady=(5, 0))

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
                  wraplength=600).pack(anchor="w", pady=(2, 8))
        ttk.Label(summary, text="Permutation tracker · live optical power", foreground=ACCENT,
                  font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(4, 2))
        ttk.Label(summary, text=("Each row is one input wavelength at one switch state. Transmission shows dB and "
                                 "percent of that wavelength's power summed across all six outputs; other output "
                                 "columns show crosstalk in dB."),
                  foreground="#9baec6", wraplength=600).pack(anchor="w", pady=(0, 4))
        metrics_frame = ttk.Frame(summary)
        metrics_frame.pack(fill="x", pady=(0, 10))
        metric_columns = ("state", "permutation", "input", "wavelength", "total", "expected", "transmission") + \
                         tuple(f"out{port}" for port in OUTPUTS)
        self.dataset_metrics_table = ttk.Treeview(metrics_frame, columns=metric_columns, show="headings", height=8)
        headings = {"state": "Switch state", "permutation": "Outputs", "input": "Input", "wavelength": "λ [nm]",
                    "total": "Total [dBm]", "expected": "Expected", "transmission": "Tx / total [dB (%)]"}
        headings.update({f"out{port}": f"Out {port} XT / total [dB]" for port in OUTPUTS})
        widths = {"state": 125, "permutation": 110, "input": 60, "wavelength": 85, "total": 85,
                  "expected": 75, "transmission": 170, **{f"out{port}": 125 for port in OUTPUTS}}
        for column in metric_columns:
            self.dataset_metrics_table.heading(column, text=headings[column])
            self.dataset_metrics_table.column(column, width=widths[column], anchor="center", stretch=False)
        metrics_scroll = ttk.Scrollbar(metrics_frame, orient="horizontal", command=self.dataset_metrics_table.xview,
                                       style="Accent.Horizontal.TScrollbar")
        self.dataset_metrics_table.configure(xscrollcommand=metrics_scroll.set)
        self.dataset_metrics_table.pack(fill="x")
        metrics_scroll.pack(fill="x")
        # The table scrolls sideways rather than widening the tab.
        cap_width(metrics_frame, 600)
        report_frame = ttk.Frame(summary)
        report_frame.pack(fill="both", expand=True)
        report_scroll = ttk.Scrollbar(report_frame, orient="vertical", style="Accent.Vertical.TScrollbar")
        self.dataset_report = tk.Text(report_frame, wrap="word", bg=PANEL, fg=TEXT, insertbackground=TEXT,
                                      font=("Consolas", 10), height=10, borderwidth=0, highlightthickness=1,
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
        self.wdm_evaluation_sweeps_var = tk.StringVar(value=str(settings["evaluation_sweeps"]))
        self.wdm_output_vars = {output: tk.BooleanVar(value=True) for output in OUTPUTS}
        self.wdm_comment_var = self.dataset_comment_var
        self.wdm_dataset_var = tk.BooleanVar(value=True)
        self.wdm_plot_var = tk.BooleanVar(value=False)
        self.wdm_status_var = tk.StringVar(value="Configure the six laser wavelengths, then evaluate from Routing.")

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
        ttk.Label(scan, text="Extra JDSU settle [s]").grid(row=8, column=0, sticky="w", pady=2)
        ttk.Combobox(scan, textvariable=self.osa_switch_settle_var,
                     values=("0", "0.25", "0.5", "1", "3"), width=8).grid(
            row=8, column=1, sticky="w", padx=(8, 0), pady=2)
        ttk.Label(scan, text="Extra OSA settle [s]").grid(row=9, column=0, sticky="w", pady=2)
        ttk.Combobox(scan, textvariable=self.wdm_settle_var,
                     values=("0", "0.5", "1", "2"), width=8).grid(
            row=9, column=1, sticky="w", padx=(8, 0), pady=2)
        ttk.Label(scan, text="Evaluate sweeps per output").grid(row=10, column=0, sticky="w", pady=2)
        ttk.Entry(scan, textvariable=self.wdm_evaluation_sweeps_var, width=8).grid(
            row=10, column=1, sticky="w", padx=(8, 0), pady=2)
        ttk.Button(scan, text="Save six-laser configuration", command=self.save_dataset_scan_configuration).grid(
            row=11, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(scan, text=("JDSU settled status and channel readback are checked before these extra delays. "
                              "OSA reads still wait for two fresh sweeps."), foreground="#9baec6",
                  wraplength=280).grid(row=12, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(scan, textvariable=self.wdm_status_var, foreground="#9baec6", wraplength=280).grid(
            row=13, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.update_wdm_routing()

    def save_dataset_scan_configuration(self):
        """Persist the WDM plan and JDSU delay shown together in the scan panel."""
        try:
            settle_s = float(self.osa_switch_settle_var.get())
            if not 0 <= settle_s <= 60:
                raise ValueError("Extra JDSU settling time must be between 0 and 60 seconds")
        except ValueError as error:
            messagebox.showerror("OSA output switch", str(error), parent=self)
            return False
        try:
            self.save_wdm_plan(quiet=True)
        except (OSError, ValueError) as error:
            messagebox.showerror("WDM plan", str(error), parent=self)
            return False
        if not self.save_osa_output_switch_settings():
            return False
        self.wdm_status_var.set("Six-laser plan and both settling delays saved.")
        return True

    @staticmethod
    def last_dataset_folder():
        try:
            return json.loads((app_dir() / DATASET_CONFIG).read_text(encoding="utf-8")).get("folder") or ""
        except (OSError, ValueError, AttributeError):
            return ""

    @staticmethod
    def remember_dataset_folder(folder):
        try:
            (app_dir() / DATASET_CONFIG).write_text(json.dumps({"folder": str(folder)}, indent=2) + "\n",
                                                    encoding="utf-8")
        except OSError:
            pass  # Only a convenience: the dataset itself is unaffected.

    def dataset_folder_changed(self):
        value = self.dataset_folder_var.get().strip()
        if value and Path(value).is_dir():
            self.remember_dataset_folder(value)

    def restore_dataset_folder(self):
        """At startup, link the dataset used last time unless it is gone."""
        value = self.dataset_folder_var.get().strip()
        if not value:
            return
        if not Path(value).is_dir():
            self.dataset_status_var.set(f"Last dataset folder {value} was not found; choose a folder.")
        elif (Path(value) / "benes_dataset.json").exists():
            self.dataset_status_var.set(f"Linked the dataset from last time: {value}")
            self.analyze_linked_dataset()
        else:
            self.dataset_status_var.set("Folder from last time selected. Initialize the full set or the active permutation.")

    def choose_dataset_folder(self):
        folder = filedialog.askdirectory(parent=self, title="Choose Benes measurement dataset",
                                         initialdir=self.dataset_folder_var.get().strip() or None)
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
            if not simulate:
                if not self.newport_controller.connected:
                    raise RuntimeError("Connect the Newport laser controller in Hardware → Newport lasers "
                                       "so the batch can turn on the six input lasers")
                if self.newport_panel.busy:
                    raise RuntimeError("Wait for the Newport command to finish")
            missing_calibration = [name for name in self.network.switches
                                   if self.controller.settings["calibration"][name]["cross"] is None or
                                   self.controller.settings["calibration"][name]["straight"] is None]
            if missing_calibration:
                raise ValueError("Calibrate every MZI before automatically applying control states")
            if scope == "active":
                configurations = [(self.active_dataset_permutation(), dict(self.network.states))]
            elif scope == "all":
                # Resume: permutations the dataset already covers on all six paths are not measured again.
                complete = {item["permutation"] for item in analyze_dataset(root)["configurations"]
                            if item["complete"]}
                configurations = [item for item in self.all_permutation_control_states()
                                  if item[0] not in complete]
                if not configurations:
                    messagebox.showinfo("Dataset complete", "Every one of the 720 permutations is already "
                                        "complete in this dataset.", parent=self)
                    return
            else:
                configurations = self.all_switch_control_states()
            batch_sizes = {"all": f"{len(configurations)} missing output permutation(s)",
                           "states": "4096 switch states"}
            if scope in batch_sizes and not messagebox.askyesno(
                    "Run dataset batch",
                    f"This will drive and measure {batch_sizes[scope]}, with six OSA output scans each. "
                    "It can take a long time. Continue?", parent=self):
                return
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Dataset batch unavailable", str(error), parent=self)
            return

        original_states = dict(self.network.states)
        original_zero = set(self.zero_volt_switches)
        settings = self.wdm_plan.settings
        comment = self.dataset_comment_var.get()
        self.dataset_batch_running = True
        self.begin_activity("dataset", "Preparing dataset acquisition...", len(configurations) * len(OUTPUTS) + 1)
        self.dataset_batch_stop_event = threading.Event()
        self.dataset_active_scan_button.configure(state="disabled")
        self.dataset_all_scan_button.configure(state="disabled")
        self.dataset_states_scan_button.configure(state="disabled")
        self.dataset_batch_stop_button.configure(state="normal")
        self.evaluate_button.configure(state="disabled")
        self.set_osa_reading(True)
        for tab in (self.route_tab, self.hardware_tab, self.calibration_tab, self.crosstalk_tab):
            self.set_tab_state(tab, "disabled")
        self.dataset_metrics_table.delete(*self.dataset_metrics_table.get_children())
        self.refresh_dataset_scan_views(root)
        self.dataset_status_var.set(f"Preparing {len(configurations)} configuration(s) for acquisition...")
        stop_event = self.dataset_batch_stop_event

        def worker():
            saved, error = 0, None
            try:
                if not simulate:
                    self.after(0, lambda: self.dataset_status_var.set("Checking the six input lasers..."))
                    switched = self.laser_bank.light_all()
                    if switched:
                        self.after(0, lambda ports=", ".join(map(str, switched)): self.dataset_status_var.set(
                            f"Turned on the laser(s) for input {ports}."))
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
                    self.after(0, lambda i=index, total=len(configurations): self.update_activity(
                        "dataset", detail=f"Dataset configuration {i}/{total}: applying controls..."))

                    def read(output):
                        if simulate:
                            return simulated_comb_spectrum(assignment, routed[output][0])
                        return osa.get_spectrum(host, port, fresh_sweeps=POST_SWITCH_SWEEPS)

                    def on_result(result, output_index, output_total):
                        self.after(0, lambda i=index, o=result.output, oi=output_index, ot=output_total:
                                   self.update_activity(
                                       "dataset", (i - 1) * len(OUTPUTS) + oi,
                                       detail=f"Dataset configuration {i}/{len(configurations)}: "
                                              f"output {o} ({oi}/{ot})"))

                    results = scan_outputs(switch, read, OUTPUTS, mapping, assignment, settings["half_window_nm"],
                                           settings["settle_s"], on_result=on_result, stop_event=stop_event)
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
                    self.after(0, lambda i=index, p=permutation, path=record:
                               self.register_dataset_scan_view(i, p, path))
                    self.after(0, lambda i=index: self.update_activity(
                        "dataset", detail=f"Saved dataset configuration {i}/{len(configurations)}"))
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
        """Append per-wavelength power ratios for one measured switch state."""
        fmt = lambda value: "—" if value is None else f"{value:.2f}"
        state = " ".join(f"{name}={'X' if states[name] else '='}" for name in self.network.switches)
        mapping = tuple(output - 1 for output in permutation)
        for item in evaluate_configuration(results, mapping, assignment):
            crosstalk = ["—" if port == item.expected_output else fmt(
                power_ratio_db(item.normalized_crosstalk.get(port)))
                         for port in OUTPUTS]
            self.dataset_metrics_table.insert("", "end", values=(
                state, " ".join(map(str, permutation)), f"In {item.input_port}", f"{item.wavelength_nm:.3f}",
                fmt(item.total_power_dbm), f"Out {item.expected_output}",
                format_transmission_share(item.normalized_transmission),
                *crosstalk))

    def register_dataset_scan_view(self, index, permutation, folder):
        """Make a completed configuration selectable without retaining its traces in RAM."""
        entry = saved_scan_entry(folder)
        if entry is None:
            return
        label = entry["label"]
        previous_latest = self.dataset_scan_latest
        self.dataset_scan_entries[label] = entry
        self.dataset_scan_views = {item["label"]: item["folder"] for item in sorted(
            self.dataset_scan_entries.values(), key=lambda item: (
                item["number"], item["alternative"][0] if item["alternative"] else 0,
                item["stamp"]))}
        self.dataset_scan_latest = label
        if self.dataset_scan_viewer is not None and self.dataset_scan_viewer.winfo_exists():
            self.dataset_scan_picker.configure(values=tuple(self.dataset_scan_views))
            if not self.dataset_scan_choice.get() or self.dataset_scan_choice.get() == previous_latest:
                self.dataset_scan_choice.set(label)
                self.draw_dataset_scan_view()

    def refresh_dataset_scan_views(self, root=None):
        """Include scans from previous sessions of the linked dataset."""
        if root is None:
            try:
                root = self.dataset_root()
            except ValueError as error:
                messagebox.showerror("Live optical viewer", str(error), parent=self.dataset_scan_viewer or self)
                return
        root = Path(root)
        entries = saved_dataset_scans(root)
        self.dataset_scan_entries = {item["label"]: item for item in entries}
        self.dataset_scan_views = {item["label"]: item["folder"] for item in entries}
        self.dataset_scan_latest = max(entries, key=lambda item: item["stamp"])["label"] if entries else None
        if self.dataset_scan_viewer is not None and self.dataset_scan_viewer.winfo_exists():
            choice = self.dataset_scan_choice.get()
            self.dataset_scan_picker.configure(values=tuple(self.dataset_scan_views))
            if choice not in self.dataset_scan_views:
                self.dataset_scan_choice.set(self.dataset_scan_latest or "")
                if self.dataset_scan_latest:
                    self.draw_dataset_scan_view()
                else:
                    self.dataset_scan_view_status.set("No completed six-output scans in this dataset yet.")
                    self.dataset_scan_permutation_var.set("Permutation: no scan selected")
                    self.dataset_scan_summary.delete(*self.dataset_scan_summary.get_children())
                    for axis in self.dataset_scan_axes:
                        axis.clear()
                    self.dataset_scan_canvas.draw_idle()
            else:
                self.draw_dataset_scan_view()

    def show_dataset_scan_viewer(self):
        """Display six spectra from the current or an earlier dataset run."""
        if self.dataset_scan_viewer is not None and self.dataset_scan_viewer.winfo_exists():
            self.refresh_dataset_scan_views()
            self.dataset_scan_viewer.lift()
            return
        try:
            root = self.dataset_root()
        except ValueError as error:
            messagebox.showerror("Live optical viewer", str(error), parent=self)
            return
        self.refresh_dataset_scan_views(root)
        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure
        except ImportError:
            messagebox.showerror("Batch spectra unavailable", "Install matplotlib to view OSA spectra.", parent=self)
            return
        window = tk.Toplevel(self)
        window.title("Live optical viewer")
        self.fit_dialog_to_screen(window, preferred_width=1150, preferred_height=800)
        self.dataset_scan_viewer = window
        controls = ttk.Frame(window, padding=10)
        controls.pack(fill="x")
        ttk.Label(controls, text="Saved configuration").pack(side="left")
        self.dataset_scan_choice = tk.StringVar(value=self.dataset_scan_latest or "")
        self.dataset_scan_picker = ttk.Combobox(
            controls, textvariable=self.dataset_scan_choice,
            values=tuple(self.dataset_scan_views), state="readonly", width=66)
        self.dataset_scan_picker.pack(side="left", padx=(8, 0))
        self.dataset_scan_picker.bind("<<ComboboxSelected>>", lambda _event: self.draw_dataset_scan_view())
        ttk.Button(controls, text="Refresh", command=self.refresh_dataset_scan_views).pack(side="left", padx=(8, 0))
        ttk.Label(controls, text="Latest scan follows automatically until you select an earlier one.").pack(
            side="left", padx=(12, 0))
        self.dataset_scan_view_status = tk.StringVar(value="Waiting for a completed configuration…")
        ttk.Label(window, textvariable=self.dataset_scan_view_status).pack(anchor="w", padx=10)
        pages = ttk.Notebook(window)
        pages.pack(fill="both", expand=True)
        spectra_page = ttk.Frame(pages)
        summary_page = ttk.Frame(pages, padding=16)
        pages.add(spectra_page, text="Output spectra")
        pages.add(summary_page, text="Permutation summary")
        self.dataset_scan_permutation_var = tk.StringVar(value="Permutation: waiting for a completed scan")
        ttk.Label(summary_page, textvariable=self.dataset_scan_permutation_var,
                  font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(0, 12))
        ttk.Label(summary_page, text="Tx / total is the routed line's share of its wavelength's measured power over all six outputs.",
                  foreground="#9baec6").pack(anchor="w", pady=(0, 10))
        columns = ("output", "input", "wavelength", "share", "peak", "mean")
        self.dataset_scan_summary = ttk.Treeview(summary_page, columns=columns, show="headings", height=6)
        for column, heading, width in (
                ("output", "Output", 90), ("input", "Target input", 110),
                ("wavelength", "Target λ [nm]", 140),
                ("share", "Tx / total [dB (%)]", 190),
                ("peak", "Target peak [dBm]", 165), ("mean", "Window mean [dBm]", 175)):
            self.dataset_scan_summary.heading(column, text=heading)
            self.dataset_scan_summary.column(column, width=width, anchor="center", stretch=False)
        self.dataset_scan_summary.pack(fill="x")
        summary_scroll = ttk.Scrollbar(summary_page, orient="horizontal",
                                       command=self.dataset_scan_summary.xview,
                                       style="Accent.Horizontal.TScrollbar")
        self.dataset_scan_summary.configure(xscrollcommand=summary_scroll.set)
        summary_scroll.pack(fill="x")
        figure = Figure(figsize=(11, 7), facecolor=BG)
        self.dataset_scan_axes = [figure.add_subplot(3, 2, output) for output in range(1, 7)]
        figure.subplots_adjust(left=0.07, right=0.98, top=0.94, bottom=0.08, hspace=0.58, wspace=0.22)
        self.dataset_scan_canvas = FigureCanvasTkAgg(figure, master=spectra_page)
        self.dataset_scan_canvas.get_tk_widget().pack(fill="both", expand=True)
        if self.dataset_scan_latest:
            self.draw_dataset_scan_view()
        else:
            self.dataset_scan_canvas.draw_idle()

    def draw_dataset_scan_view(self):
        folder = self.dataset_scan_views.get(self.dataset_scan_choice.get())
        if folder is None:
            return
        try:
            spectra = load_output_spectra(folder)
            permutation, summary = load_target_summary(folder)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            messagebox.showerror("Batch spectra unavailable", str(error), parent=self.dataset_scan_viewer)
            return
        for axis, (output, input_port, wavelength, wave, power) in zip(self.dataset_scan_axes, spectra):
            axis.clear()
            axis.set_facecolor(PANEL)
            axis.plot(wave, power, color="#78b9ff", linewidth=0.9)
            axis.axvline(wavelength, color="#ffbe69", linewidth=1.5, linestyle="--")
            axis.set_title(f"Out {output}  ←  In {input_port} target: {wavelength:g} nm",
                           color=TEXT, fontsize=10)
            axis.tick_params(colors=TEXT, labelsize=8)
            axis.grid(True, alpha=0.2)
            axis.set_xlabel("Wavelength [nm]", color=TEXT, fontsize=8)
            axis.set_ylabel("Power [dBm]", color=TEXT, fontsize=8)
        entry = self.dataset_scan_entries.get(self.dataset_scan_choice.get())
        identity = (f"Permutation P{entry['number']:03d}"
                    + (f" · Path {entry['alternative'][0]}/{entry['alternative'][1]}"
                       if entry['alternative'] else "")) if entry else "Permutation"
        self.dataset_scan_permutation_var.set(
            identity + "\n" + "   ".join(f"In {input_port} → Out {output}"
                                          for input_port, output in enumerate(permutation, start=1)))
        self.dataset_scan_summary.delete(*self.dataset_scan_summary.get_children())
        for output, input_port, wavelength, peak, mean, share in summary:
            self.dataset_scan_summary.insert("", "end", values=(
                f"Out {output}", f"In {input_port}", f"{wavelength:.3f}",
                format_transmission_share(share),
                "—" if peak is None else f"{peak:.2f}",
                "—" if mean is None else f"{mean:.2f}"))
        self.dataset_scan_view_status.set(f"Showing configuration {self.dataset_scan_choice.get()}")
        self.dataset_scan_canvas.draw_idle()

    def stop_dataset_batch(self):
        if self.dataset_batch_stop_event is not None:
            self.dataset_batch_stop_event.set()
            self.dataset_status_var.set("Stopping after the current OSA output...")

    def finish_dataset_batch(self, saved, total, error, stopped):
        self.dataset_batch_running = False
        self.finish_activity("dataset", f"Dataset acquisition {'failed' if error else 'stopped' if stopped else 'complete'}: "
                             f"{saved}/{total} configurations saved")
        self.dataset_batch_stop_event = None
        self.dataset_active_scan_button.configure(state="normal")
        self.dataset_all_scan_button.configure(state="normal")
        self.dataset_states_scan_button.configure(state="normal")
        self.dataset_batch_stop_button.configure(state="disabled")
        self.evaluate_button.configure(state="normal")
        self.set_osa_reading(False)
        for tab in (self.route_tab, self.hardware_tab, self.calibration_tab, self.crosstalk_tab):
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
        except (OSError, ValueError) as error:
            messagebox.showerror("Dataset analysis failed", str(error), parent=self)
            return
        self.dataset_analysis_request += 1
        request = self.dataset_analysis_request
        self.begin_activity("dataset_analysis", "Evaluating dataset coverage...")

        def worker():
            try:
                with self.dataset_analysis_lock:
                    report = analyze_dataset(root)
                    csv_path = write_coverage_report(root, report)
            except Exception as error:
                self.after(0, lambda message=str(error): self.finish_dataset_analysis(request, root=root, error=message))
            else:
                self.after(0, lambda: self.finish_dataset_analysis(request, root, report, csv_path))

        threading.Thread(target=worker, daemon=True).start()

    def finish_dataset_analysis(self, request, root=None, report=None, csv_path=None, error=None):
        if request != self.dataset_analysis_request:
            return
        if root is not None and root != Path(self.dataset_folder_var.get().strip()):
            self.finish_activity("dataset_analysis", "Dataset selection changed")
            return
        if error:
            self.finish_activity("dataset_analysis", "Dataset analysis failed")
            messagebox.showerror("Dataset analysis failed", error, parent=self)
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
        self.finish_activity("dataset_analysis", "Dataset coverage updated")

    def set_dataset_report(self, text):
        self.dataset_report.configure(state="normal")
        self.dataset_report.delete("1.0", "end")
        self.dataset_report.insert("1.0", text)
        self.dataset_report.configure(state="disabled")
