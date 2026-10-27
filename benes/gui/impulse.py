"""Impulse test tab: pulse one heater and record the Arduino detector."""

import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from benes.gui.theme import BG, OSA_COLOR, PANEL, PLOT_MIN_SIZE, POWER_METER_COLOR, TEXT, WIRE
from benes.gui.widgets import VerticalScrolledFrame
from benes.measurements.impulse_test import (LIVE_PLOT_WINDOW_S, plot_trace_samples, received_pair_rate,
                                             run_impulse_test, validate_impulse_settings, write_impulse_csv)


class ImpulseMixin:
    """Mixed into ``BenesApp``; relies on the state it sets up."""

    def build_impulse_tab(self):
        # The settings scroll on their own so the plot stays in view.
        control_column = ttk.Frame(self.impulse_tab, padding=18, width=310)
        control_column.pack(side="left", fill="y")
        control_column.pack_propagate(False)
        settings_scroll = VerticalScrolledFrame(control_column)
        settings_scroll.pack(fill="both", expand=True)
        controls = ttk.Frame(settings_scroll.content, padding=(0, 0, 10, 0))
        controls.pack(anchor="nw")
        plot_area = ttk.Frame(self.impulse_tab, padding=(0, 18, 18, 18))
        plot_area.pack(side="left", fill="both", expand=True)
        self.impulse_plot_area = plot_area
        ttk.Label(plot_area, text="Optical power and electrical monitor traces appear here.").pack(
            anchor="center", expand=True)

        self.impulse_switch_var = tk.StringVar(value=next(iter(self.network.switches)))
        self.impulse_fields = {}
        fields = ("Low voltage [V]", "High voltage [V]", "Period [s]", "High time [s]",
                  "Pulse duration [s]", "Measure duration [s]", "Lead-in [s]")
        ttk.Label(controls, text="Voltage-board contact", font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Combobox(controls, textvariable=self.impulse_switch_var, values=tuple(self.network.switches),
                     state="readonly", width=12).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))
        for row, label in enumerate(fields, start=2):
            variable = tk.StringVar()
            self.impulse_fields[label] = variable
            ttk.Label(controls, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(controls, textvariable=variable, width=10).grid(
                row=row, column=1, sticky="w", padx=(8, 0), pady=3)
        self.impulse_frequency_var = tk.StringVar()

        def update_frequency(*_):
            try:
                period = float(self.impulse_fields["Period [s]"].get())
                self.impulse_frequency_var.set(f"Frequency: {1 / period:.4g} Hz" if period > 0 else "")
            except ValueError:
                self.impulse_frequency_var.set("")

        self.impulse_fields["Period [s]"].trace_add("write", update_frequency)
        update_frequency()
        ttk.Label(controls, textvariable=self.impulse_frequency_var).grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self.impulse_input_ready = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="Pin 8 signal at A1 stays within 0–5 V",
                        variable=self.impulse_input_ready).grid(
            row=10, column=0, columnspan=2, sticky="w", pady=(12, 4))
        ttk.Label(controls, text="Board pin 8 mirrors the selected MZI. Arduino A0 reads optical power; A1 reads pin 8.",
                  wraplength=275).grid(row=11, column=0, columnspan=2, sticky="w", pady=(0, 12))
        self.impulse_play_button = ttk.Button(controls, text="Play square wave", style="Accent.TButton",
                                               command=lambda: self.start_impulse("square"))
        self.impulse_play_button.grid(row=12, column=0, columnspan=2, sticky="ew", pady=3)
        self.impulse_single_button = ttk.Button(controls, text="Single pulse", command=lambda: self.start_impulse("single"))
        self.impulse_single_button.grid(row=13, column=0, columnspan=2, sticky="ew", pady=3)
        self.impulse_stop_button = ttk.Button(controls, text="Stop", command=self.stop_impulse, state="disabled")
        self.impulse_stop_button.grid(row=14, column=0, columnspan=2, sticky="ew", pady=3)
        self.impulse_export_button = ttk.Button(controls, text="Export trace CSV", style="Accent.TButton",
                                                 command=self.export_impulse, state="disabled")
        self.impulse_export_button.grid(row=16, column=0, columnspan=2, sticky="ew", pady=(12, 3))
        self.impulse_status = tk.StringVar(value="Raw stream: tagged 7-bit A0/A1 pairs.")
        ttk.Label(controls, textvariable=self.impulse_status, wraplength=275).grid(
            row=17, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.impulse_average_var = tk.StringVar()
        ttk.Label(controls, text="Optical display average [ms]").grid(
            row=18, column=0, sticky="w", pady=(12, 0))
        self.impulse_average_entry = ttk.Entry(
            controls, textvariable=self.impulse_average_var, width=10)
        self.impulse_average_entry.grid(row=18, column=1, sticky="w", padx=(8, 0), pady=(12, 0))
        ttk.Label(controls, text="Blank or 0 shows raw samples; CSV always exports raw A0/A1 data.",
                  wraplength=275).grid(row=19, column=0, columnspan=2, sticky="w")
        ttk.Label(controls, text="Times are in seconds: 1e-3 = 1 ms, 5e-2 = 50 ms. "
                  "Each edge takes ~1.9 ms to send at 115200 baud, so shorter pulses stretch. "
                  "Display average is in ms.",
                  wraplength=275).grid(row=20, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.impulse_canvas = None

    def start_impulse(self, mode):
        if self.impulse_running:
            return
        try:
            if self.crosstalk_running or self.mzi_calibration_running:
                raise RuntimeError("Wait for the current calibration or scan to finish")
            if self.read_power_button.instate(["disabled"]):
                raise RuntimeError("Wait for the current Arduino reading to finish")
            if not self.impulse_input_ready.get():
                raise ValueError("Confirm that the pin 8 signal at Arduino A1 stays within 0–5 V")
            fields = self.impulse_fields
            low = float(fields["Low voltage [V]"].get())
            high = float(fields["High voltage [V]"].get())
            lead_in = float(fields["Lead-in [s]"].get())
            options = {"lead_in_s": lead_in}
            if mode == "square":
                options.update(period_s=float(fields["Period [s]"].get()),
                               high_time_s=float(fields["High time [s]"].get()))
            else:
                options.update(pulse_s=float(fields["Pulse duration [s]"].get()),
                               measure_s=float(fields["Measure duration [s]"].get()))
            validate_impulse_settings(mode, low, high, **options)
            average_ms = float(self.impulse_average_var.get().strip() or 0)
            if not average_ms >= 0:
                raise ValueError("Optical display average cannot be negative")
            switch = self.impulse_switch_var.get()
            if not self.controller.connected or self.controller.settings["dry_run"]:
                raise RuntimeError("Connect the physical voltage board first")
            if self.controller.settings["mapping"].get(switch) is None:
                raise ValueError(f"Assign a board pin to {switch} first")
            if 8 in self.controller.settings["mapping"].values():
                raise ValueError("Board pin 8 must be unassigned to mirror the pulse")
            if not self.power_meter.connected or self.power_meter.settings["stream_format"] not in ("raw", "dual"):
                raise RuntimeError("Connect the Arduino with the raw A0/A1 stream selected")
            # Confirm here: a prompt during the run would stall the pulse timing.
            self.confirm_voltage_sweep([low, high], names=[switch])
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
            from matplotlib.figure import Figure
            from matplotlib.ticker import EngFormatter, MaxNLocator
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Impulse test unavailable", str(error), parent=self)
            return

        for child in self.impulse_plot_area.winfo_children():
            child.destroy()
        figure = Figure(figsize=(7, 5), dpi=100, facecolor=BG)
        self.impulse_average_ms = average_ms
        self.impulse_optical_axis = figure.add_subplot(111, facecolor=PANEL)
        self.impulse_electrical_axis = self.impulse_optical_axis.twinx()
        optical_label = (f"Optical A0 · {average_ms:g} ms average"
                         if average_ms else "Optical A0 · raw")
        self.impulse_optical_line, = self.impulse_optical_axis.plot(
            [], [], color=POWER_METER_COLOR, linewidth=1.5, label=optical_label)
        self.impulse_electrical_line, = self.impulse_electrical_axis.plot(
            [], [], color=OSA_COLOR, linewidth=1.5, label="Electrical monitor · A1")
        self.impulse_optical_axis.set(xlabel="Time since first sample",
                                      ylabel=f"Optical power [{self.power_meter.settings['unit']}]")
        self.impulse_optical_axis.xaxis.set_major_locator(MaxNLocator(nbins=6, min_n_ticks=3))
        self.impulse_optical_axis.xaxis.set_major_formatter(EngFormatter(unit="s", sep=" "))
        self.impulse_optical_axis.xaxis.label.set_color(TEXT)
        self.impulse_optical_axis.tick_params(axis="x", labelsize=10, pad=6, colors=TEXT)
        for axis in (self.impulse_optical_axis, self.impulse_electrical_axis):
            for spine in axis.spines.values():
                spine.set_color(WIRE)
        self.impulse_electrical_axis.set_ylabel(
            f"Electrical signal [ADC counts, 0–{self.power_meter.full_scale}]")
        self.impulse_optical_axis.tick_params(axis="y", colors=POWER_METER_COLOR)
        self.impulse_electrical_axis.tick_params(axis="y", colors=OSA_COLOR)
        self.impulse_optical_axis.yaxis.label.set_color(POWER_METER_COLOR)
        self.impulse_electrical_axis.yaxis.label.set_color(OSA_COLOR)
        self.impulse_optical_axis.grid(True, color="#aab5c4", alpha=0.4)
        figure.suptitle(f"{switch} · {mode.capitalize()} pulse response", color=TEXT, y=0.99)
        figure.legend((self.impulse_optical_line, self.impulse_electrical_line),
                      (optical_label, "Electrical monitor · A1"),
                      loc="upper center", bbox_to_anchor=(0.5, 0.95), ncol=2,
                      facecolor=PANEL, edgecolor=WIRE, labelcolor=TEXT)
        figure.subplots_adjust(left=0.13, right=0.84, bottom=0.18, top=0.83)
        self.impulse_canvas = FigureCanvasTkAgg(figure, master=self.impulse_plot_area)
        self.impulse_toolbar = NavigationToolbar2Tk(self.impulse_canvas, self.impulse_plot_area, pack_toolbar=False)
        self.impulse_toolbar.pack(side="bottom", fill="x")
        self.impulse_canvas.get_tk_widget().configure(**PLOT_MIN_SIZE)
        self.impulse_canvas.get_tk_widget().pack(fill="both", expand=True)

        def refresh_zoom(_axis):
            if (self.impulse_trace is not None and
                    (self.impulse_toolbar.mode or self.impulse_toolbar._nav_stack() is not None)):
                self.render_impulse_trace(self.impulse_trace)

        self.impulse_optical_axis.callbacks.connect("xlim_changed", refresh_zoom)
        self.impulse_stop_event = threading.Event()
        self.impulse_trace = None
        self.impulse_running = True
        self.impulse_play_button.configure(state="disabled")
        self.impulse_single_button.configure(state="disabled")
        self.impulse_stop_button.configure(state="normal")
        self.impulse_export_button.configure(state="disabled")
        self.impulse_average_entry.configure(state="disabled")
        self.read_power_button.configure(state="disabled")
        for tab in (self.route_tab, self.hardware_tab, self.calibration_tab, self.crosstalk_tab):
            self.set_tab_state(tab, "disabled")
        self.impulse_status.set(f"Recording {mode} pulse on {switch} and board pin 8...")

        def progress(trace):
            self.after(0, lambda: self.render_impulse_trace(trace))

        def worker():
            try:
                trace = run_impulse_test(
                    self.controller, self.power_meter, switch, mode, low, high,
                    self.impulse_stop_event, on_progress=progress, **options)
            except Exception as error:
                self.after(0, lambda message=str(error): self.finish_impulse(switch, low, error=message))
            else:
                self.after(0, lambda: self.finish_impulse(switch, low, trace=trace))

        threading.Thread(target=worker, daemon=True).start()

    def stop_impulse(self):
        if self.impulse_stop_event is not None:
            self.impulse_stop_event.set()
            self.impulse_status.set("Stopping and returning both board pins to the low voltage...")

    def render_impulse_trace(self, trace):
        if self.impulse_canvas is None:
            return
        self.impulse_trace = trace
        if not trace.samples:
            return
        navigating = bool(self.impulse_toolbar.mode) or self.impulse_toolbar._nav_stack() is not None
        shown = plot_trace_samples(
            trace.samples, window_s=LIVE_PLOT_WINDOW_S if self.impulse_running and not navigating else None,
            optical_average_s=self.impulse_average_ms / 1000,
            time_range=self.impulse_optical_axis.get_xlim() if navigating else None)
        if not shown:
            return
        times = [sample[0] for sample in shown]
        optical = [sample[3] for sample in shown]
        electrical = [sample[2] for sample in shown]
        left = self.impulse_optical_axis
        right = self.impulse_electrical_axis
        self.impulse_optical_line.set_data(times, optical)
        self.impulse_electrical_line.set_data(times, electrical)
        if not navigating:
            left.set_xlim(min(0, times[0]) if not self.impulse_running else max(0, times[-1] - LIVE_PLOT_WINDOW_S),
                          max(0.0001, times[-1]))
        for axis, values in ((left, optical), (right, electrical)):
            if navigating:
                continue
            low, high = min(values), max(values)
            padding = max((high - low) * 0.1, 0.5 if axis is right else 0.01)
            axis.set_ylim(low - padding, high + padding)
        self.impulse_canvas.draw_idle()
        rate = (len(trace.samples) / trace.capture_elapsed_s if trace.sample_period_s and trace.capture_elapsed_s
                else received_pair_rate(trace.samples, window_s=1.0 if self.impulse_running else None))
        self.impulse_status.set(
            f"{trace.mode.capitalize()} trace: {len(trace.samples)} paired samples, "
            f"~{rate:,.0f} pairs/s · " +
            (f"{trace.sample_period_s * 1e6:.1f} µs measured pairs · " if trace.sample_period_s else "") +
            (f"{trace.skipped_bytes} bytes resynced · " if trace.skipped_bytes else "") +
            (f"showing latest {LIVE_PLOT_WINDOW_S:g} s" if self.impulse_running else "full recording; use zoom to inspect edges")
        )

    def finish_impulse(self, switch, low, trace=None, error=None):
        self.impulse_running = False
        self.impulse_stop_event = None
        self.impulse_play_button.configure(state="normal")
        self.impulse_single_button.configure(state="normal")
        self.impulse_stop_button.configure(state="disabled")
        self.impulse_average_entry.configure(state="normal")
        self.read_power_button.configure(state="normal")
        for tab in (self.route_tab, self.hardware_tab, self.calibration_tab, self.crosstalk_tab):
            self.set_tab_state(tab, "normal")
        self.voltage_vars[switch].set(f"{low:.2f}")
        if trace is not None:
            self.impulse_trace = trace
            self.render_impulse_trace(trace)
        if self.impulse_trace is not None and self.impulse_trace.samples:
            self.impulse_export_button.configure(state="normal")
        if error:
            self.impulse_status.set(f"Impulse test stopped: {error}")
            messagebox.showerror("Impulse test failed", error, parent=self)
        elif trace is not None:
            rate = (len(trace.samples) / trace.capture_elapsed_s if trace.sample_period_s and trace.capture_elapsed_s
                    else received_pair_rate(trace.samples))
            self.impulse_status.set(
                f"Trace ready: {len(trace.samples)} optical/electrical pairs "
                f"(~{rate:,.0f} pairs/s"
                + (f", {trace.sample_period_s * 1e6:.1f} µs measured spacing" if trace.sample_period_s else "")
                + (f", {trace.skipped_bytes} bytes resynced" if trace.skipped_bytes else "")
                + "). Export CSV to save it."
            )

    def export_impulse(self):
        trace = self.impulse_trace
        if trace is None or not trace.samples:
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="Export impulse trace", defaultextension=".csv",
            filetypes=[("CSV data", "*.csv")],
            initialfile=f"impulse_{trace.switch.replace('.', '_')}_{datetime.now():%Y%m%d_%H%M%S}.csv",
        )
        if not path:
            return
        try:
            write_impulse_csv(path, trace)
        except (OSError, ValueError) as error:
            messagebox.showerror("Export failed", str(error), parent=self)
        else:
            self.impulse_status.set(f"Exported trace: {path}")
