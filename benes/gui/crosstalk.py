"""Thermal crosstalk tab: voltage scans, live plots and saved result folders."""

import json
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from benes.gui.theme import ACCENT, PANEL, PLOT_MIN_SIZE, POWER_METER_COLOR, TEXT
from benes.hardware import osa
from benes.measurements.calibration_scan import (ScanPoint, scan_switches, scan_switches_with_reader,
                                                 scan_voltages, spectrum_scan_point)
from benes.measurements.crosstalk_results import (PLOT_X_POWER, PLOT_X_VOLTAGE, RESCAN_STYLES,
                                                  create_result_folder, load_scan_folder, plot_x_values,
                                                  save_scan_artifacts, scan_context)
from benes.measurements.spectrum_export import write_crosstalk_csv
from benes.paths import app_dir


class CrosstalkMixin:
    """Mixed into ``BenesApp``; relies on the state it sets up."""

    def build_crosstalk_tab(self):
        """Switch selection and scan settings beside the live plot area."""
        scan_controls = ttk.Frame(self.crosstalk_tab, padding=18)
        scan_controls.pack(side="left", fill="y", padx=(24, 12), pady=18)
        ttk.Label(scan_controls, text="Select switches (Ctrl or Shift for multiple)").grid(row=0, column=0, columnspan=2, sticky="w")
        self.crosstalk_switches = tk.Listbox(scan_controls, selectmode="extended", exportselection=False,
                                             height=len(self.network.switches), width=18, bg=PANEL, fg=TEXT,
                                             selectbackground="#315b78", selectforeground="white")
        for name in self.network.switches:
            self.crosstalk_switches.insert("end", name)
        self.crosstalk_switches.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 14))
        self.crosstalk_source = tk.StringVar(value="Arduino")
        ttk.Label(scan_controls, text="Measurement").grid(row=2, column=0, sticky="w", pady=3)
        self.crosstalk_source_box = ttk.Combobox(scan_controls, textvariable=self.crosstalk_source,
                                                 values=("Arduino", "OSA"), state="readonly", width=12)
        self.crosstalk_source_box.grid(row=2, column=1, sticky="w", padx=(10, 0), pady=3)
        self.crosstalk_fields = {}
        for row, (label, default) in enumerate((("Lower limit [V]", "0"), ("Upper limit [V]", "5"),
                                                 ("Step [V]", "0.5"), ("Settle [ms]", "50")), start=3):
            ttk.Label(scan_controls, text=label).grid(row=row, column=0, sticky="w", pady=3)
            value = tk.StringVar(value=default)
            ttk.Entry(scan_controls, textvariable=value, width=12).grid(row=row, column=1, sticky="w", padx=(10, 0), pady=3)
            self.crosstalk_fields[label] = value
        self.crosstalk_comment_var = tk.StringVar()
        ttk.Label(scan_controls, text="Scan comment").grid(row=7, column=0, sticky="w", pady=3)
        ttk.Entry(scan_controls, textvariable=self.crosstalk_comment_var, width=19).grid(row=7, column=1, sticky="w", padx=(10, 0), pady=3)
        ttk.Label(scan_controls, text="Arduino: burst average. OSA: trimmed spectrum, peak and mean dBm. Each point is taken after settling; switches return to 0 V.", wraplength=245).grid(row=8, column=0, columnspan=2, sticky="w", pady=(12, 8))
        self.crosstalk_button = ttk.Button(scan_controls, text="Scan selected switches", command=self.start_crosstalk_scan)
        self.crosstalk_button.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(4, 6))
        self.crosstalk_export_button = ttk.Button(scan_controls, text="Export CSV with comment", command=self.export_crosstalk_csv, state="disabled")
        self.crosstalk_export_button.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        self.crosstalk_status = tk.StringVar(value="Connect the voltage board and select a measurement source.")
        ttk.Label(scan_controls, textvariable=self.crosstalk_status, wraplength=245).grid(row=11, column=0, columnspan=2, sticky="w")

        plot_area = ttk.Frame(self.crosstalk_tab, padding=(0, 18, 18, 18))
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


    def start_crosstalk_scan(self):
        names = [self.crosstalk_switches.get(index) for index in self.crosstalk_switches.curselection()]
        source = self.crosstalk_source.get()
        try:
            if self.mzi_calibration_running or self.dataset_batch_running:
                raise RuntimeError("Wait for the MZI calibration to finish")
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
            if source == "Arduino":
                if not self.power_meter.connected:
                    raise RuntimeError("Connect the Arduino power meter first")
                if self.read_power_button.instate(["disabled"]):
                    raise RuntimeError("Wait for the current Arduino power reading to finish")
            elif source == "OSA":
                if self.osa_button.instate(["disabled"]):
                    raise RuntimeError("Wait for the current OSA trace read to finish")
                simulate_osa = self.osa_dry_run_var.get()
                osa_host = self.osa_host_var.get().strip()
                osa_port = int(self.osa_port_var.get()) if not simulate_osa else 0
                if not simulate_osa and (not osa_host or not 1 <= osa_port <= 65535):
                    raise ValueError("Enter a valid OSA host and port in Boards & measurement")
            else:
                raise ValueError("Choose Arduino or OSA as the measurement source")
            for name in names:
                if self.controller.settings["mapping"][name] is None:
                    raise ValueError(f"Assign a voltage-board pin to {name} first")
            self.confirm_voltage_sweep(voltages, names=names)
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk  # noqa: F401
            from matplotlib.figure import Figure  # noqa: F401
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Crosstalk scan unavailable", str(error), parent=self)
            return

        response_unit = "dBm" if source == "OSA" else self.power_meter.settings["unit"]
        output_port = self.destination
        context = scan_context(
            self.network, self.source, output_port,
            self.detector_port(self.power_meter_output_var), self.detector_port(self.osa_output_var),
            self.controller, names, voltages, settle_ms, source, self.crosstalk_comment_var.get().strip(),
        )
        try:
            result_folder = create_result_folder(app_dir(), context)
            save_scan_artifacts(result_folder, context, {}, source, response_unit, self.network,
                                status="in_progress")
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Crosstalk results unavailable", str(error), parent=self)
            return

        self.active_scan_kind = "Thermal crosstalk scan"
        self.crosstalk_running = True
        self.crosstalk_data = {}
        self.crosstalk_data_source = None
        self.crosstalk_data_unit = None
        self.crosstalk_result_folder = result_folder
        self.crosstalk_button.configure(state="disabled")
        self.crosstalk_export_button.configure(state="disabled")
        self.crosstalk_switches.configure(state="disabled")
        self.crosstalk_source_box.configure(state="disabled")
        self.set_tab_state(self.route_tab, "disabled")
        self.set_tab_state(self.hardware_tab, "disabled")
        self.crosstalk_status.set(f"Scanning {len(names)} switch(es), {len(voltages)} points each...")
        self.prepare_crosstalk_plots(voltages)
        data = {}

        def on_point(name, index, total, point):
            data.setdefault(name, []).append(point)
            self.after(0, lambda: self.add_crosstalk_point(name, point, index, total, source, response_unit))

        def worker():
            try:
                if source == "Arduino":
                    scan_switches(self.controller, self.power_meter, names, voltages, settle_ms / 1000, on_point)
                else:
                    def read_osa_point(voltage):
                        spectrum = osa.simulated_spectrum() if simulate_osa else osa.get_spectrum(osa_host, osa_port)
                        return spectrum_scan_point(voltage, spectrum)

                    scan_switches_with_reader(self.controller, names, voltages, settle_ms / 1000,
                                               read_osa_point, on_point)
            except Exception as error:
                self.after(0, lambda message=str(error): self.finish_crosstalk_scan(names, data, source, response_unit, result_folder, context, message))
            else:
                self.after(0, lambda: self.finish_crosstalk_scan(names, data, source, response_unit, result_folder, context))

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
        self.crosstalk_button.configure(state="normal")
        self.crosstalk_switches.configure(state="normal")
        self.crosstalk_source_box.configure(state="readonly")
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
                                  label="Peak" if source == "OSA" else "Power")
            mean_line = None
            if source == "OSA":
                mean_line, = response.plot([], [], marker=".", linewidth=1.4,
                                           color=POWER_METER_COLOR, label="Mean")
                response.legend(loc="best")
            response.set(xlabel=self.plot_x(name, [])[1],
                         ylabel="Power [dBm]" if source == "OSA" else f"Power [{response_unit}]",
                         title=f"{name}: power response")
            response.grid(True)
            spectra = None
            norm = None
            cmap = None
            if source == "OSA":
                spectra = figure.add_subplot(122)
                spectra.set(xlabel="Wavelength [nm]", ylabel="Power [dBm]", title=f"{name}: spectra")
                spectra.grid(True)
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
        detail = (f"peak {point.power:.4g}, mean {point.mean_power_dbm:.4g} dBm" if source == "OSA"
                  else f"{point.power:.4g} {response_unit}")
        self.crosstalk_status.set(f"{name}: {index}/{total} at {point.voltage:.2f} V, {detail}")

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
        """Redraw every open response plot against volts or heater power."""
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
