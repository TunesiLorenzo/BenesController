"""Six-laser WDM plan, per-output OSA scan and the Evaluate Configuration view."""

import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, simpledialog, ttk

from benes.gui.theme import (ACCENT, BG, OSA_COLOR, PANEL, PLOT_MIN_SIZE, POST_SWITCH_SWEEPS, TEXT,
                             WDM_COLORS, WIRE)
from benes.gui.widgets import VerticalScrolledFrame
from benes.hardware import osa
from benes.hardware.osa_output_switch import OUTPUTS, fiber_number
from benes.measurements.dataset_registry import save_measurement
from benes.measurements.wdm_comb import (INPUTS, acquire_averaged_spectrum, create_wdm_folder,
                                         evaluate_configuration,
                                         input_fiber_number, power_ratio_db, routed_wavelengths,
                                         save_configuration_evaluation, save_wdm_scan, scan_outputs,
                                         simulated_comb_spectrum)
from benes.paths import app_dir


class WDMMixin:
    """Mixed into ``BenesApp``; relies on the state it sets up."""

    def build_wdm_tab(self):
        """Wavelength plan for the WDM comb at the inputs and the per-output OSA scan."""
        controls_column = ttk.Frame(self.wdm_tab, padding=(18, 18, 8, 18), width=390)
        controls_column.pack(side="left", fill="y")
        controls_column.pack_propagate(False)
        control_scroll = VerticalScrolledFrame(controls_column)
        control_scroll.pack(fill="both", expand=True)
        controls = ttk.Frame(control_scroll.content, padding=(0, 0, 12, 0))
        controls.pack(anchor="nw", fill="x")
        results = ttk.Frame(self.wdm_tab, padding=(8, 18, 18, 18))
        results.pack(side="left", fill="both", expand=True)

        settings = self.wdm_plan.settings
        self.wdm_wavelength_vars = {
            port: tk.StringVar(value="" if settings["assignment"][str(port)] is None
                               else f"{settings['assignment'][str(port)]:g}")
            for port in INPUTS
        }
        self.wdm_destination_vars = {port: tk.StringVar() for port in INPUTS}
        self.wdm_comb_var = tk.StringVar(value=" ".join(f"{line:g}" for line in settings["comb_lines"]))
        self.wdm_preset_var = tk.StringVar(value=settings["active_preset"] or "")
        self.wdm_window_var = tk.StringVar(value=f"{settings['half_window_nm']:g}")
        self.wdm_settle_var = tk.StringVar(value=f"{settings['settle_s']:g}")
        self.wdm_output_vars = {output: tk.BooleanVar(value=True) for output in OUTPUTS}
        self.wdm_comment_var = tk.StringVar()
        self.wdm_dataset_var = tk.BooleanVar(value=False)
        self.wdm_plot_var = tk.BooleanVar(value=True)
        self.wdm_status_var = tk.StringVar(value="Assign comb wavelengths to the inputs, then scan the outputs.")

        ttk.Label(controls, text="WDM COMB", foreground=ACCENT, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(controls, text="Assign one comb line to each input. The current permutation decides the "
                                 "output where each line should appear.", wraplength=340).pack(anchor="w", pady=(4, 12))

        plan = ttk.LabelFrame(controls, text="Wavelength plan", padding=10)
        plan.pack(fill="x")
        ttk.Label(plan, text="Comb lines [nm]").grid(row=0, column=0, sticky="w")
        comb_entry = ttk.Entry(plan, textvariable=self.wdm_comb_var, width=30)
        comb_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(8, 0))
        ttk.Label(plan, text="Optional grid offered in each input's list, e.g. 1549.32 1550.12 1550.92",
                  foreground="#9baec6", wraplength=320).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 8))
        ttk.Label(plan, text="Input").grid(row=2, column=0, sticky="w")
        ttk.Label(plan, text="λ [nm]").grid(row=2, column=1, sticky="w", padx=(8, 0))
        ttk.Label(plan, text="Reaches").grid(row=2, column=2, sticky="w", padx=(8, 0))
        self.wdm_wavelength_boxes = []
        for row, port in enumerate(INPUTS, start=3):
            ttk.Label(plan, text=f"In {port} · Fiber {input_fiber_number(port)}", foreground=WDM_COLORS[port - 1]).grid(
                row=row, column=0, sticky="w", pady=2)
            box = ttk.Combobox(plan, textvariable=self.wdm_wavelength_vars[port], width=10)
            box.grid(row=row, column=1, sticky="w", padx=(8, 0), pady=2)
            self.wdm_wavelength_boxes.append(box)
            ttk.Label(plan, textvariable=self.wdm_destination_vars[port], foreground=OSA_COLOR).grid(
                row=row, column=2, sticky="w", padx=(8, 0), pady=2)
            self.wdm_wavelength_vars[port].trace_add("write", lambda *_: (self.update_wdm_routing(), self.draw()))
        comb_entry.bind("<FocusOut>", lambda _event: self.update_wdm_comb_choices())
        comb_entry.bind("<Return>", lambda _event: self.update_wdm_comb_choices())
        ttk.Button(plan, text="Save plan", style="Accent.TButton", command=self.save_wdm_plan).grid(
            row=9, column=0, columnspan=3, sticky="ew", pady=(10, 0))

        presets = ttk.LabelFrame(controls, text="Saved wavelength plans", padding=10)
        presets.pack(fill="x", pady=(12, 0))
        self.wdm_preset_box = ttk.Combobox(presets, textvariable=self.wdm_preset_var, state="readonly", width=24)
        self.wdm_preset_box.pack(fill="x")
        preset_buttons = ttk.Frame(presets)
        preset_buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(preset_buttons, text="Load", command=self.load_wdm_preset).pack(side="left", fill="x", expand=True)
        ttk.Button(preset_buttons, text="Save as…", command=self.save_wdm_preset).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(preset_buttons, text="Delete", command=self.delete_wdm_preset).pack(side="left", fill="x", expand=True)

        scan = ttk.LabelFrame(controls, text="Output scan", padding=10)
        scan.pack(fill="x", pady=(12, 0))
        ttk.Label(scan, text="Settling delay [s]").grid(row=0, column=0, sticky="w")
        ttk.Entry(scan, textvariable=self.wdm_settle_var, width=8).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(scan, text="Waited after each switch move, before reading. Allow at least one full OSA sweep "
                             "so the trace holds no light from the previous output.",
                  foreground="#9baec6", wraplength=320).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 8))
        ttk.Label(scan, text="Line window ± [nm]").grid(row=2, column=0, sticky="w")
        ttk.Entry(scan, textvariable=self.wdm_window_var, width=8).grid(row=2, column=1, sticky="w", padx=(8, 0))
        ttk.Label(scan, text="Outputs").grid(row=3, column=0, sticky="w", pady=(8, 0))
        output_checks = ttk.Frame(scan)
        output_checks.grid(row=4, column=0, columnspan=2, sticky="w")
        for output in OUTPUTS:
            ttk.Checkbutton(output_checks, text=str(output), variable=self.wdm_output_vars[output]).pack(side="left")
        ttk.Label(scan, text="Comment").grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(scan, textvariable=self.wdm_comment_var).grid(row=6, column=0, columnspan=2, sticky="ew")
        ttk.Checkbutton(scan, text="Plot the spectra", variable=self.wdm_plot_var).grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(scan, text="Also record in the linked dataset", variable=self.wdm_dataset_var).grid(
            row=8, column=0, columnspan=2, sticky="w")
        scan_buttons = ttk.Frame(scan)
        scan_buttons.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.wdm_start_button = ttk.Button(scan_buttons, text="Start scan", style="Accent.TButton",
                                           command=self.start_wdm_scan)
        self.wdm_start_button.pack(side="left", fill="x", expand=True)
        self.wdm_stop_button = ttk.Button(scan_buttons, text="Stop", command=self.stop_wdm_scan, state="disabled")
        self.wdm_stop_button.pack(side="left", padx=(6, 0))
        ttk.Label(controls, text="Uses the OSA and JDSU settings from Hardware & OSA.",
                  foreground="#9baec6", wraplength=340).pack(anchor="w", pady=(10, 0))
        ttk.Label(controls, textvariable=self.wdm_status_var, wraplength=340).pack(anchor="w", pady=(8, 4))

        ttk.Label(results, text="Output measurements", foreground=ACCENT,
                  font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(results, text="Peak and window-mean power of the routed comb line at each output. Isolation is "
                                "the routed peak minus the strongest other comb line.", wraplength=700).pack(anchor="w", pady=(2, 8))
        # Read-only view of the current switch states with each assigned comb line in its input's colour.
        self.wdm_canvas = tk.Canvas(results, bg=BG, highlightthickness=0, height=240)
        self.wdm_canvas.pack(fill="x")
        self.wdm_canvas.bind("<Configure>", lambda event: self.draw_wdm_diagram())
        ttk.Label(results, text="Solid: routed light · thin dashes: possible crosstalk from the other switch port",
                  foreground="#9baec6").pack(anchor="w", pady=(0, 10))
        style = ttk.Style(self)
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=24)
        style.configure("Treeview.Heading", background="#263650", foreground=TEXT, font=("Segoe UI", 9, "bold"))
        columns = (("output", "Output", 70), ("fiber", "Fiber", 50), ("channel", "JDSU", 50),
                   ("input", "From input", 80), ("wavelength", "λ [nm]", 90), ("peak", "Peak [dBm]", 85),
                   ("peak_nm", "Peak λ [nm]", 95), ("mean", "Window mean [dBm]", 120), ("isolation", "Isolation [dB]", 100))
        table_frame = ttk.Frame(results)
        table_frame.pack(fill="both", expand=True)
        self.wdm_table = ttk.Treeview(table_frame, columns=[name for name, _, _ in columns], show="headings", height=8)
        for name, heading, width in columns:
            self.wdm_table.heading(name, text=heading)
            self.wdm_table.column(name, width=width, anchor="center")
        for port in INPUTS:
            self.wdm_table.tag_configure(f"input{port}", foreground=WDM_COLORS[port - 1])
        table_scroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.wdm_table.xview,
                                     style="Accent.Horizontal.TScrollbar")
        self.wdm_table.configure(xscrollcommand=table_scroll.set)
        self.wdm_table.pack(fill="both", expand=True)
        table_scroll.pack(fill="x")
        self.update_wdm_comb_choices()
        self.update_wdm_presets()

    def update_wdm_comb_choices(self):
        values = self.wdm_comb_var.get().replace(",", " ").replace(";", " ").split()
        for box in self.wdm_wavelength_boxes:
            box.configure(values=values)

    def update_wdm_presets(self):
        self.wdm_preset_box.configure(values=sorted(self.wdm_plan.settings["presets"]))

    def wdm_assignment(self):
        return {str(port): variable.get() for port, variable in self.wdm_wavelength_vars.items()}

    def update_wdm_routing(self):
        """Show where each assigned comb line leaves the network for the current switch states."""
        if not hasattr(self, "wdm_destination_vars"):
            return
        mapping = self.network.mapping()
        lines = []
        for port in INPUTS:
            output = mapping[port - 1] + 1
            self.wdm_destination_vars[port].set(f"→ Out {output} · Fiber {fiber_number(output)}")
            wavelength = self.wdm_wavelength_vars[port].get().strip()
            if wavelength:
                lines.append(f"{wavelength} nm: In {port} → Out {output}")
        self.wdm_route_label.configure(text="WDM COMB    " + "     ".join(lines) if lines else "")
        self.draw_wdm_diagram()
        if hasattr(self, "cal_canvas"):
            self.update_calibration_lasers()

    def wdm_routes(self):
        """(input row, wavelength nm, colour) for every input with an assigned comb line."""
        if not hasattr(self, "wdm_wavelength_vars"):
            return []
        routes = []
        for port in INPUTS:
            try:
                wavelength = float(self.wdm_wavelength_vars[port].get())
            except ValueError:
                continue
            routes.append((port - 1, wavelength, WDM_COLORS[port - 1]))
        return routes

    def draw_wdm_diagram(self):
        c = getattr(self, "wdm_canvas", None)
        if c is None:
            return
        c.delete("all")
        width, height = c.winfo_width(), c.winfo_height()
        if width < 360 or height < 150:
            return
        font = ("Segoe UI", 8)
        left_x, right_x = 92, width - 92
        first_stage, last_stage = left_x + 45, right_x - 60
        xs = [first_stage + i * (last_stage - first_stage) / 4 for i in range(5)]
        ys = [20 + i * (height - 40) / 5 for i in range(6)]
        half = 11
        for stage, pairs in enumerate(self.network.pairs):
            x = xs[stage]
            for row, y in enumerate(ys):
                if stage == 0:
                    c.create_line(left_x + 11, y, x - half, y, fill=WIRE, width=1.5)
                next_x = xs[stage + 1] - half if stage < 4 else right_x - 11
                c.create_line(x + half, y, next_x, ys[self.network.links[stage][row]], fill=WIRE, width=1.5)
                if (stage, row) not in self.network.at:
                    c.create_line(x - half, y, x + half, y, fill=WIRE, width=1.5)
            for a, b in pairs:
                crossed = self.network.states[self.network.at[stage, a]]
                c.create_rectangle(x - half, ys[a] - 5, x + half, ys[b] + 5, fill=PANEL, outline="#7790ad")
                for start, end in ((a, b), (b, a)) if crossed else ((a, a), (b, b)):
                    c.create_line(x - half, ys[start], x + half, ys[end], fill="#a4bad4", width=1.5)
        routed = {}
        routes = [(self.network.trace(row), wavelength, color)
                  for row, wavelength, color in self.wdm_routes()]
        for route, _, color in routes:
            for points in self.crosstalk_points(route, right_x - 11, xs, ys, half):
                c.create_line(*points, fill=color, width=1.5, dash=(3, 4),
                              joinstyle="round", capstyle="round")
        for route, wavelength, color in routes:
            row = route.rows[0]
            routed[row, "in"] = routed[route.rows[-1], "out"] = (wavelength, color)
            c.create_line(*self.route_points(route, left_x + 11, right_x - 11, xs, ys, half),
                          fill=color, width=3, joinstyle="round", capstyle="round")
        for row, y in enumerate(ys):
            for side, x, anchor, offset in (("in", left_x, "e", -16), ("out", right_x, "w", 16)):
                wavelength, color = routed.get((row, side), (None, None))
                c.create_oval(x - 11, y - 11, x + 11, y + 11, fill=PANEL, outline=color or WIRE,
                              width=2.5 if color else 1.5)
                c.create_text(x, y, text=str(row + 1), fill=TEXT, font=("Segoe UI", 9, "bold"))
                label = f"{'In' if side == 'in' else 'Out'} {row + 1}"
                if wavelength is not None:
                    label += f"\n{wavelength:g} nm"
                c.create_text(x + offset, y, text=label, anchor=anchor, fill=color or "#9baec6", font=font)

    def save_wdm_plan(self, quiet=False):
        try:
            self.wdm_plan.configure(self.wdm_assignment(), self.wdm_window_var.get(), self.wdm_settle_var.get(),
                                    self.wdm_comb_var.get(), self.wdm_evaluation_sweeps_var.get())
        except (OSError, ValueError) as error:
            if not quiet:
                messagebox.showerror("WDM plan", str(error), parent=self)
            raise
        self.update_wdm_comb_choices()
        self.wdm_status_var.set("Wavelength plan saved.")
        return True

    def set_wdm_assignment(self, assignment):
        for port in INPUTS:
            wavelength = assignment[str(port)]
            self.wdm_wavelength_vars[port].set("" if wavelength is None else f"{wavelength:g}")

    def save_wdm_preset(self):
        name = simpledialog.askstring("Save wavelength plan", "Name for this wavelength plan:",
                                      initialvalue=self.wdm_preset_var.get(), parent=self)
        if name is None:
            return
        try:
            self.save_wdm_plan()
            self.wdm_plan.save_preset(name)
        except (OSError, ValueError) as error:
            messagebox.showerror("WDM plan", str(error), parent=self)
            return
        self.update_wdm_presets()
        self.wdm_preset_var.set(name.strip())
        self.wdm_status_var.set(f"Saved wavelength plan {name.strip()!r}.")

    def load_wdm_preset(self):
        name = self.wdm_preset_var.get()
        if not name:
            return
        try:
            assignment = self.wdm_plan.load_preset(name)
        except (OSError, ValueError) as error:
            messagebox.showerror("WDM plan", str(error), parent=self)
            return
        self.set_wdm_assignment(assignment)
        self.wdm_status_var.set(f"Loaded wavelength plan {name!r}.")

    def delete_wdm_preset(self):
        name = self.wdm_preset_var.get()
        if not name or not messagebox.askyesno("Delete wavelength plan", f"Delete the plan {name!r}?", parent=self):
            return
        try:
            self.wdm_plan.delete_preset(name)
        except OSError as error:
            messagebox.showerror("WDM plan", str(error), parent=self)
            return
        self.wdm_preset_var.set("")
        self.update_wdm_presets()

    def start_configuration_evaluation(self):
        """Evaluate the persisted six-laser plan at every chip output."""
        self.start_wdm_scan(outputs=OUTPUTS, evaluation=True, record_dataset=False)

    def start_wdm_scan(self, outputs=None, evaluation=False, record_dataset=None):
        if self.wdm_running or self.dataset_batch_running:
            return
        switch = self.osa_output_switch
        try:
            if self.mzi_calibration_running:
                raise RuntimeError("Wait for the MZI calibration to finish")
            if self.osa_button.instate(["disabled"]):
                raise RuntimeError("Wait for the current OSA trace read to finish")
            if not evaluation:
                try:
                    self.save_wdm_plan(quiet=True)
                except OSError as error:
                    raise ValueError(f"Could not save the wavelength plan: {error}") from error
            assignment = dict(self.wdm_plan.assignment)
            missing = [str(port) for port in INPUTS if assignment[str(port)] is None]
            if evaluation and missing:
                raise ValueError("Save a wavelength for every input before evaluating the configuration")
            if not any(wavelength is not None for wavelength in assignment.values()):
                raise ValueError("Assign a comb wavelength to at least one input")
            host, port, simulate = self.osa_connection()
            if not switch.connected:
                raise RuntimeError("Connect the OSA output switch first (dry run is available)")
            if outputs is None:
                outputs = [output for output, variable in self.wdm_output_vars.items() if variable.get()]
            else:
                outputs = list(outputs)
            if not outputs:
                raise ValueError("Select at least one output to scan")
            for output in outputs:
                switch.channel_for(output)
            record_dataset = self.wdm_dataset_var.get() if record_dataset is None else record_dataset
            dataset_root = self.dataset_root() if record_dataset else None
            if dataset_root is not None and not (dataset_root / "benes_dataset.json").exists():
                raise ValueError("Initialize the linked dataset in Dataset coverage first")
        except (RuntimeError, ValueError) as error:
            messagebox.showerror("WDM scan unavailable", str(error), parent=self)
            return

        mapping = self.network.mapping()
        permutation = self.active_dataset_permutation()
        routed = routed_wavelengths(mapping, assignment)
        settings = self.wdm_plan.settings
        evaluation_sweeps = settings["evaluation_sweeps"] if evaluation else 1
        comment = self.dataset_comment_var.get()
        context = {
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "permutation": list(permutation),
            "assignment": assignment,
            "wavelength_plan": settings["active_preset"],
            "half_window_nm": settings["half_window_nm"],
            "settle_s": settings["settle_s"],
            "averaged_sweeps_per_output": evaluation_sweeps,
            "jdsu_switch_settle_s": switch.settings["settle_s"],
            "jdsu_channel_map": dict(switch.settings["channel_map"]),
            "switch_states": {name: "cross" if crossed else "bar" for name, crossed in self.network.states.items()},
            "switch_voltages_v": {
                name: self.controller.applied_voltages.get(name, self.controller.settings["voltage"].get(name))
                for name in self.network.states
            },
            "simulated_osa": simulate,
            "dry_run_switch": switch.settings["dry_run"],
        }

        def read(output):
            output_index = outputs.index(output) + 1
            def on_sweep(index, count):
                self.after(0, lambda o=output, oi=output_index, i=index, c=count:
                           self.update_activity("wdm", (oi - 1) * c + i,
                                                detail=f"{'Evaluating' if evaluation else 'Scanning'} output "
                                                       f"{o} ({oi}/{len(outputs)}), sweep {i}/{c}"))
            if simulate:
                return acquire_averaged_spectrum(
                    lambda _: simulated_comb_spectrum(assignment, routed[output][0]),
                    evaluation_sweeps, POST_SWITCH_SWEEPS, on_sweep)
            return acquire_averaged_spectrum(
                lambda fresh: osa.get_spectrum(host, port, fresh_sweeps=fresh),
                evaluation_sweeps, POST_SWITCH_SWEEPS, on_sweep)

        self.wdm_running = True
        self.begin_activity("wdm", "Evaluating configuration" if evaluation else "WDM output scan",
                            len(outputs) * evaluation_sweeps + 1)
        self.wdm_stop_event = threading.Event()
        if hasattr(self, "wdm_start_button"):
            self.wdm_start_button.configure(state="disabled")
        if hasattr(self, "wdm_stop_button"):
            self.wdm_stop_button.configure(state="normal")
        if hasattr(self, "evaluate_button"):
            self.evaluate_button.configure(state="disabled")
        self.set_osa_reading(True)
        if hasattr(self, "wdm_table"):
            self.wdm_table.delete(*self.wdm_table.get_children())
        self.wdm_status_var.set(("Evaluating" if evaluation else "Scanning") +
                                f" outputs {', '.join(map(str, outputs))}"
                                + (f" ({evaluation_sweeps} sweeps/output)" if evaluation else "") + "...")
        stop_event = self.wdm_stop_event

        markers = [(wavelength, WDM_COLORS[int(port) - 1], f"In {port} · {wavelength:g} nm")
                   for port, wavelength in assignment.items() if wavelength is not None]

        def on_result(result, index, total):
            self.after(0, lambda: self.add_wdm_result(result, index, total))

        def worker():
            results, folder, error = [], None, None
            try:
                results = scan_outputs(switch, read, outputs, mapping, assignment, settings["half_window_nm"],
                                       settings["settle_s"], on_result=on_result, stop_event=stop_event)
                if results:
                    self.after(0, lambda: self.update_activity("wdm", detail="Saving WDM results..."))
                    complete = {**context, "complete": len(results) == len(outputs)}
                    folder = create_wdm_folder(app_dir())
                    files = save_wdm_scan(folder, results, complete, comment)
                    if dataset_root is not None:
                        record = save_measurement(
                            dataset_root, source="OSA WDM", permutation=permutation,
                            input_ports=[result.routed_input for result in results],
                            output_ports=[result.output for result in results], mode="routed_pairs",
                            files=files, comment=comment, extra={"results_folder": str(folder)})
                        save_wdm_scan(record, results, complete, comment)
            except Exception as error_value:
                error = str(error_value)
            self.after(0, lambda: self.finish_wdm_scan(results, folder, error, stop_event.is_set(),
                                                       dataset_root is not None, markers, evaluation, mapping,
                                                       assignment, context))


        threading.Thread(target=worker, daemon=True).start()

    def add_wdm_result(self, result, index, total):
        signal = result.signal
        fmt = lambda value, digits=2: "—" if value is None else f"{value:.{digits}f}"
        if hasattr(self, "wdm_table"):
            self.wdm_table.insert("", "end", values=(
                result.output, result.fiber, result.jdsu_channel, result.routed_input,
                fmt(result.wavelength_nm, 3), fmt(signal and signal.peak_dbm), fmt(signal and signal.peak_nm, 3),
                fmt(signal and signal.mean_dbm), fmt(result.isolation_db)), tags=(f"input{result.routed_input}",))
        self.wdm_status_var.set(f"Measured output {result.output} ({index}/{total}).")

    def stop_wdm_scan(self):
        if self.wdm_stop_event is not None:
            self.wdm_stop_event.set()
            self.wdm_status_var.set("Stopping after the current output...")

    def finish_wdm_scan(self, results, folder, error, stopped, recorded, markers=(), evaluation=False,
                        mapping=(), assignment=None, context=None):
        self.wdm_running = False
        job_name = "Evaluation" if evaluation else "WDM scan"
        self.finish_activity("wdm", f"{job_name} {'failed' if error else 'stopped' if stopped else 'complete'}")
        if hasattr(self, "wdm_start_button"):
            self.wdm_start_button.configure(state="normal")
        if hasattr(self, "wdm_stop_button"):
            self.wdm_stop_button.configure(state="disabled")
        if hasattr(self, "evaluate_button"):
            self.evaluate_button.configure(state="normal")
        self.set_osa_reading(False)
        if results:
            self.osa_switch_status_var.set(f"OSA sampling output {results[-1].output} after the WDM scan")
            self.osa_output_var.set(str(results[-1].output))
        if evaluation and results:
            self.show_configuration_evaluation(results, mapping, assignment, folder, context)
        elif self.wdm_plot_var.get():
            for result in results:
                self.show_spectrum(result.wave, result.power, name=f"WDM Out {result.output} (In {result.routed_input})",
                                   restore_buttons=False, output_port=result.output, trimmed=True, markers=markers)
        if error:
            self.wdm_status_var.set(f"WDM scan failed: {error}" + (f" Partial results saved in {folder}." if folder else ""))
            messagebox.showerror("WDM scan failed", error, parent=self)
            return
        prefix = "WDM scan stopped" if stopped else "WDM scan complete"
        if folder is None:
            self.wdm_status_var.set(f"{prefix}; nothing was measured.")
            return
        self.wdm_status_var.set(f"{prefix}: {len(results)} output(s) saved in {folder}"
                                + (" and recorded in the dataset." if recorded else "."))
        if recorded:
            self.analyze_linked_dataset()

    def show_configuration_evaluation(self, results, mapping, assignment, folder=None, context=None):
        """Show the six output spectra next to the configuration power table."""
        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
            from matplotlib.figure import Figure
        except ImportError:
            messagebox.showerror("Evaluation plot unavailable",
                                 "Install matplotlib to display the configuration evaluation.", parent=self)
            return
        summary = evaluate_configuration(results, mapping, assignment)
        window = tk.Toplevel(self)
        window.title("Evaluate Configuration")
        self.fit_dialog_to_screen(window, preferred_width=1280, preferred_height=720)
        figure = Figure(figsize=(12, 6), facecolor=BG)
        spectra = figure.add_subplot(121, facecolor=PANEL)
        table_axis = figure.add_subplot(122, facecolor=PANEL)
        for result in results:
            spectra.plot(result.wave, result.power, linewidth=1.0, label=f"Out {result.output}")
        spectra.set(xlabel="Wavelength [nm]", ylabel="Power [dBm]", title="Six-output OSA scan")
        spectra.title.set_color("white")
        spectra.xaxis.label.set_color("white")
        spectra.yaxis.label.set_color("white")
        spectra.tick_params(colors="white")
        spectra.grid(True, alpha=0.3)
        spectra.legend(loc="best", fontsize=8)
        table_axis.axis("off")
        headers = ["Input", "λ [nm]", "Total\n[dBm]", "Expected", "Transmission\n[dBm]", "Tx / total\n[dB]"] + \
                  [f"Out {port}\nXT / total [dB]" for port in OUTPUTS]
        fmt = lambda value: "—" if value is None else f"{value:.2f}"
        rows = [[f"In {item.input_port}", f"{item.wavelength_nm:.3f}", fmt(item.total_power_dbm),
                 f"Out {item.expected_output}", fmt(item.transmission_dbm),
                 fmt(power_ratio_db(item.normalized_transmission))] +
                ["—" if port == item.expected_output else
                 fmt(power_ratio_db(item.normalized_crosstalk.get(port)))
                for port in OUTPUTS]
                for item in summary]
        table_axis.set_title("Per-wavelength routing evaluation", color=TEXT, fontsize=11, pad=12)
        table = table_axis.table(cellText=rows, colLabels=headers, cellLoc="center", loc="center")
        table.auto_set_font_size(False)
        table.set_fontsize(6.5)
        table.scale(1.05, 1.75)
        for (row, _column), cell in table.get_celld().items():
            cell.set_edgecolor(WIRE)
            cell.set_facecolor("#263650" if row == 0 else PANEL)
            cell.get_text().set_color(TEXT)
        figure.subplots_adjust(left=0.06, right=0.98, bottom=0.12, top=0.9, wspace=0.24)
        canvas = FigureCanvasTkAgg(figure, master=window)
        NavigationToolbar2Tk(canvas, window).update()
        ttk.Button(window, text="Export image, controls and data to results",
                   command=lambda: self.export_configuration_evaluation(
                       folder, figure, summary, context, window),
                   state="normal" if folder is not None and context is not None else "disabled").pack(
                       anchor="e", padx=8, pady=4)
        canvas.get_tk_widget().configure(**PLOT_MIN_SIZE)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        canvas.draw_idle()

    def export_configuration_evaluation(self, folder, figure, summary, context, parent):
        try:
            save_configuration_evaluation(folder, summary, context)
            figure.savefig(folder / "configuration_evaluation.png", dpi=200, facecolor=figure.get_facecolor())
        except (OSError, ValueError) as error:
            messagebox.showerror("Evaluation export failed", str(error), parent=parent)
            return
        messagebox.showinfo("Evaluation export complete",
                            f"Saved the image, control values and evaluation data in:\n{folder}", parent=parent)
