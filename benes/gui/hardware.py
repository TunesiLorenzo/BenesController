"""Hardware tab: voltage board, optical meters, JDSU switch and voltage prompts."""

from collections import deque
import math
from queue import Empty, Full, Queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from benes.core.heater_setup import BREAK_CURRENT_DENSITY, SAFE_CURRENT_DENSITY
from benes.gui.theme import ACCENT, BG
from benes.hardware.hp8152a import RANGES_DBM, validate_stream_rate
from benes.hardware.osa_output_switch import JDSU_CHANNELS, OUTPUTS, fiber_number


class HardwareMixin:
    """Mixed into ``BenesApp``; relies on the state it sets up."""

    def build_hardware_tab(self):
        """Voltage board, OSA/output switch, and both power meter pages."""
        hardware_tabs = ttk.Notebook(self.hardware_tab)
        hardware_tabs.pack(fill="both", expand=True, padx=18, pady=16)
        voltage_page = ttk.Frame(hardware_tabs)
        osa_page = ttk.Frame(hardware_tabs)
        meter_page = ttk.Frame(hardware_tabs)
        hp_page = ttk.Frame(hardware_tabs)
        hardware_tabs.add(voltage_page, text="Voltage board")
        hardware_tabs.add(osa_page, text="OSA & output switch")
        hardware_tabs.add(meter_page, text="Arduino meter")
        hardware_tabs.add(hp_page, text="HP 8152A meter")

        hardware = ttk.LabelFrame(voltage_page, text="Voltage controller", padding=(18, 12))
        hardware.pack(fill="x")
        ttk.Label(hardware, text="Voltage board", font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Label(hardware, text="Port").pack(side="left", padx=(14, 4))
        ttk.Entry(hardware, textvariable=self.port_var, width=9).pack(side="left")
        ttk.Label(hardware, text="Baud").pack(side="left", padx=(10, 4))
        ttk.Entry(hardware, textvariable=self.baud_var, width=8).pack(side="left")
        ttk.Checkbutton(hardware, text="Dry run", variable=self.dry_run_var).pack(side="left", padx=10)
        ttk.Button(hardware, text="Connect", command=self.connect_board).pack(side="left")
        ttk.Button(hardware, text="Disconnect", command=self.disconnect_board).pack(side="left", padx=6)
        ttk.Button(hardware, text="MZI ↔ pin mapping / calibration…", command=self.show_mapping).pack(side="left", padx=10)
        self.hardware_status = ttk.Label(hardware)
        self.hardware_status.pack(side="right")

        osa_controls = ttk.LabelFrame(osa_page, text="OSA spectrum analyser", padding=(18, 12))
        osa_controls.pack(fill="x")
        ttk.Label(osa_controls, text="OSA", font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Label(osa_controls, text="Host").pack(side="left", padx=(28, 4))
        ttk.Entry(osa_controls, textvariable=self.osa_host_var, width=16).pack(side="left")
        ttk.Label(osa_controls, text="Port").pack(side="left", padx=(10, 4))
        ttk.Entry(osa_controls, textvariable=self.osa_port_var, width=6).pack(side="left")
        ttk.Combobox(osa_controls, textvariable=self.osa_plot_mode, values=("Overlay", "New window"), state="readonly", width=11).pack(side="left", padx=10)
        ttk.Checkbutton(osa_controls, text="Simulate", variable=self.osa_dry_run_var).pack(side="left")
        self.osa_button = ttk.Button(osa_controls, text="Read trace", style="Accent.TButton", command=self.read_spectrum)
        self.osa_button.pack(side="left", padx=8)

        osa_router = ttk.LabelFrame(osa_page, text="OSA output switch", padding=(18, 12))
        osa_router.pack(fill="x", pady=(12, 0))
        ttk.Label(osa_router, text="Selecting an output routes the JDSU switch so the OSA samples it.",
                  foreground="#9baec6").grid(
            row=0, column=0, columnspan=5, sticky="w")
        ttk.Label(osa_router, text="Output").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Combobox(osa_router, textvariable=self.osa_switch_output_var, values=tuple(map(str, range(1, 7))),
                     state="readonly", width=6).grid(row=1, column=1, sticky="w", padx=(6, 12), pady=(10, 0))
        self.osa_port_read_button = ttk.Button(osa_router, text="Route + read", style="Accent.TButton",
                                               command=self.read_selected_osa_output)
        self.osa_port_read_button.grid(row=1, column=2, sticky="w", pady=(10, 0))
        self.osa_all_outputs_button = ttk.Button(osa_router, text="Read all outputs", command=self.read_all_osa_outputs)
        self.osa_all_outputs_button.grid(row=1, column=3, sticky="w", padx=(6, 0), pady=(10, 0))
        ttk.Button(osa_router, text="Selector setup…", command=self.show_osa_output_switch_setup).grid(
            row=1, column=4, sticky="w", padx=(12, 0), pady=(10, 0))
        ttk.Label(osa_router, textvariable=self.osa_switch_status_var, wraplength=650).grid(
            row=2, column=0, columnspan=5, sticky="w", pady=(10, 0))
        self.osa_switch_output_var.trace_add("write", lambda *_: self.osa_output_chosen(self.osa_switch_output_var))

        arduino = ttk.LabelFrame(meter_page, text="Arduino power meter", padding=(18, 12))
        arduino.pack(fill="x")
        ttk.Label(arduino, text="Port").grid(row=0, column=0, sticky="w")
        ttk.Entry(arduino, textvariable=self.arduino_port_var, width=10).grid(row=0, column=1, sticky="w", padx=(4, 12))
        ttk.Label(arduino, text="Baud").grid(row=0, column=2, sticky="w")
        ttk.Entry(arduino, textvariable=self.arduino_baud_var, width=9).grid(row=0, column=3, sticky="w", padx=(4, 12))
        ttk.Label(arduino, text="Average [s]").grid(row=0, column=4, sticky="w")
        ttk.Entry(arduino, textvariable=self.arduino_duration_var, width=7).grid(row=0, column=5, sticky="w", padx=(4, 12))
        ttk.Button(arduino, text="Connect", command=self.connect_power_meter).grid(row=0, column=6, sticky="w")
        ttk.Button(arduino, text="Disconnect", command=self.disconnect_power_meter).grid(row=0, column=7, sticky="w", padx=6)
        ttk.Label(arduino, text="Power = ADC ×").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(arduino, textvariable=self.arduino_scale_var, width=8).grid(row=1, column=1, sticky="w", padx=(4, 4), pady=(10, 0))
        ttk.Label(arduino, text="+").grid(row=1, column=2, sticky="w", pady=(10, 0))
        ttk.Entry(arduino, textvariable=self.arduino_offset_var, width=8).grid(row=1, column=3, sticky="w", padx=(4, 12), pady=(10, 0))
        ttk.Label(arduino, text="Unit").grid(row=1, column=4, sticky="w", pady=(10, 0))
        ttk.Entry(arduino, textvariable=self.arduino_unit_var, width=14).grid(row=1, column=5, sticky="w", padx=(4, 12), pady=(10, 0))
        self.read_power_button = ttk.Button(arduino, text="Read current power", command=self.read_current_power)
        self.read_power_button.grid(row=1, column=6, sticky="w", pady=(10, 0))
        ttk.Label(arduino, text="Stream").grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Combobox(arduino, textvariable=self.arduino_stream_var, values=("raw", "legacy", "dual"),
                     state="readonly", width=10).grid(row=2, column=1, sticky="w", pady=(10, 0))
        ttk.Label(arduino, text="Ceiling [ADC]").grid(row=2, column=2, sticky="w", pady=(10, 0))
        ttk.Entry(arduino, textvariable=self.arduino_ceiling_var, width=9).grid(
            row=2, column=3, sticky="w", padx=(4, 12), pady=(10, 0))
        ttk.Label(arduino, text="Raw: optical A0 + jump flag; dual: older framed A0/A1; legacy: A0 only").grid(
            row=2, column=4, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Label(arduino, text="Jump guard [ms]").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(arduino, textvariable=self.arduino_jump_guard_var, width=10).grid(
            row=3, column=1, sticky="w", pady=(8, 0))
        ttk.Label(arduino, text="Raw optical stream discards flagged jumps, the guard window, and saturated samples.",
                  wraplength=520).grid(row=3, column=2, columnspan=6, sticky="w", pady=(8, 0))
        ttk.Label(arduino, textvariable=self.power_reading_var, font=("Segoe UI", 10, "bold")).grid(
            row=4, column=0, columnspan=8, sticky="w", pady=(12, 0))

        self.build_hp_meter_page(hp_page)

    def build_hp_meter_page(self, page):
        controls = ttk.LabelFrame(page, text="HP 8152A optical power meter", padding=(18, 12))
        controls.pack(fill="x")
        fields = (("GPIB resource", self.hp_meter_resource_var),
                  ("Channel", self.hp_meter_channel_var),
                  ("Wavelength [nm]", self.hp_meter_wavelength_var),
                  ("Range [dBm]", self.hp_meter_range_var),
                  ("Requested GPIB reads [Hz]", self.hp_stream_hz_var))
        for index, (label, variable) in enumerate(fields):
            ttk.Label(controls, text=label).grid(row=index // 3, column=2 * (index % 3),
                                                  sticky="w", padx=(0, 4), pady=4)
            if label == "Channel":
                field = ttk.Combobox(controls, textvariable=variable, values=("A", "B"),
                                     state="readonly", width=9)
            elif label == "Range [dBm]":
                field = ttk.Combobox(controls, textvariable=variable,
                                     values=("Auto",) + tuple(map(str, RANGES_DBM)),
                                     state="readonly", width=9)
            else:
                field = ttk.Entry(controls, textvariable=variable, width=19 if index == 0 else 10)
            field.grid(row=index // 3, column=2 * (index % 3) + 1, sticky="w", padx=(0, 12), pady=4)
        self.hp_stream_start_button = ttk.Button(controls, text="Start stream", command=self.start_hp_stream)
        self.hp_stream_start_button.grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.hp_stream_stop_button = ttk.Button(controls, text="Stop", command=self.stop_hp_stream,
                                                state="disabled")
        self.hp_stream_stop_button.grid(row=2, column=1, sticky="w", pady=(10, 0))
        ttk.Button(controls, text="Apply read rate", command=self.apply_hp_stream_rate).grid(
            row=2, column=2, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Label(controls, text="Rate sets the requested GPIB read cadence. The meter's 2/8 Hz filter limits "
                  "optical response; the achieved read rate appears below.", wraplength=660).grid(
            row=3, column=0, columnspan=6, sticky="w", pady=(10, 0))

        self.hp_stream_status_var = tk.StringVar(value="Stopped. Set the meter input and start the stream.")
        ttk.Label(page, textvariable=self.hp_stream_status_var, wraplength=800).pack(anchor="w", pady=(10, 4))
        self.hp_stream_power_var = tk.StringVar(value="— dBm")
        ttk.Label(page, textvariable=self.hp_stream_power_var,
                  font=("Segoe UI", 16, "bold")).pack(anchor="w", pady=(0, 8))

        self.hp_stream_canvas = None
        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure
        except ImportError:
            ttk.Label(page, text="Install matplotlib to show the live plot; readings remain in the table.").pack(
                anchor="w", pady=8)
        else:
            figure = Figure(figsize=(7, 3), dpi=100)
            self.hp_stream_axes = figure.add_subplot(111)
            self.hp_stream_axes.set(xlabel="Time since start [s]", ylabel="Optical power [dBm]")
            self.hp_stream_axes.grid(True)
            self.hp_stream_line, = self.hp_stream_axes.plot([], [], color=ACCENT, linewidth=1.2)
            self.hp_stream_canvas = FigureCanvasTkAgg(figure, master=page)
            self.hp_stream_canvas.get_tk_widget().pack(fill="both", expand=True)
        self.hp_stream_rows = ttk.Treeview(page, columns=("time", "power"), show="headings", height=6)
        self.hp_stream_rows.heading("time", text="Time [s]")
        self.hp_stream_rows.heading("power", text="Power [dBm]")
        self.hp_stream_rows.column("time", width=100, anchor="e", stretch=False)
        self.hp_stream_rows.column("power", width=140, anchor="e")
        self.hp_stream_rows.pack(fill="x", pady=(8, 0))
        self.hp_stream_points = deque(maxlen=2000)
        self.hp_stream_times = deque(maxlen=100)
        self.hp_stream_queue = Queue(maxsize=3000)
        self.hp_stream_count = 0
        self.hp_stream_rate_hz = 5.0
        self.hp_stream_poll_id = None

    def apply_hp_stream_rate(self):
        try:
            rate = validate_stream_rate(self.hp_stream_hz_var.get())
        except ValueError as error:
            messagebox.showerror("HP meter read rate", str(error), parent=self)
            return False
        self.hp_stream_rate_hz = rate
        if self.hp_meter_busy == "stream":
            self.hp_stream_status_var.set(f"Requested {rate:g} GPIB reads/s; measuring achieved rate...")
        return True

    def start_hp_stream(self):
        if self.hp_meter_busy or self.crosstalk_running:
            messagebox.showerror("HP meter busy", "Wait for the HP meter test or scan to finish.", parent=self)
            return
        if not self.apply_hp_stream_rate():
            return
        try:
            options = self.hp_meter_options()
        except (OSError, ValueError) as error:
            messagebox.showerror("HP meter settings", str(error), parent=self)
            return
        self.hp_stream_points.clear()
        self.hp_stream_times.clear()
        self.hp_stream_count = 0
        self.hp_stream_queue = Queue(maxsize=3000)
        for row in self.hp_stream_rows.get_children():
            self.hp_stream_rows.delete(row)
        if self.hp_stream_canvas is not None:
            self.hp_stream_line.set_data([], [])
            self.hp_stream_canvas.draw_idle()
        self.hp_stream_power_var.set("— dBm")
        self.hp_stream_stop = threading.Event()
        self.hp_meter_busy = "stream"
        self.hp_stream_start_button.configure(state="disabled")
        self.hp_stream_stop_button.configure(state="normal")
        self.hp_stream_status_var.set(f"Connecting to {options['resource']}...")
        self.begin_activity("hp_stream", "HP meter: connecting...")
        threading.Thread(target=self._hp_stream_worker, args=(options, self.hp_stream_stop), daemon=True).start()
        self.hp_stream_poll_id = self.after(100, self._poll_hp_stream)

    def _hp_stream_worker(self, options, stop):
        meter = None
        error = None
        try:
            meter = self.open_hp_meter(options)
            started = time.monotonic()
            next_due = started
            while not stop.is_set():
                if stop.wait(max(0, next_due - time.monotonic())):
                    break
                power = meter.read_dbm_or_none()
                measured_at = time.monotonic()
                try:
                    self.hp_stream_queue.put_nowait((measured_at - started, power))
                except Full:
                    pass
                next_due = max(next_due + 1 / self.hp_stream_rate_hz, measured_at)
        except Exception as failure:
            error = str(failure)
        finally:
            if meter is not None:
                try:
                    meter.disconnect()
                except Exception as restore_error:
                    error = error or f"Could not restore HP meter settings: {restore_error}"
        self.after(0, lambda message=error: self.finish_hp_stream(message))

    def stop_hp_stream(self):
        if self.hp_meter_busy == "stream" and self.hp_stream_stop is not None:
            self.hp_stream_stop.set()
            self.hp_stream_stop_button.configure(state="disabled")
            self.hp_stream_status_var.set("Stopping HP meter stream and restoring settings...")

    def _poll_hp_stream(self, schedule=True):
        self.hp_stream_poll_id = None
        changed = False
        while True:
            try:
                elapsed, power = self.hp_stream_queue.get_nowait()
            except Empty:
                break
            self.hp_stream_count += 1
            self.hp_stream_times.append(elapsed)
            self.hp_stream_points.append((elapsed, float("nan") if power is None else power))
            self.hp_stream_power_var.set("Out of range" if power is None else f"{power:.3f} dBm")
            self.hp_stream_rows.insert("", 0, values=(f"{elapsed:.3f}",
                                                      "Out of range" if power is None else f"{power:.4f}"))
            changed = True
        if changed:
            children = self.hp_stream_rows.get_children()
            for row in children[200:]:
                self.hp_stream_rows.delete(row)
            xs, ys = zip(*self.hp_stream_points)
            if self.hp_stream_canvas is not None:
                self.hp_stream_line.set_data(xs, ys)
                if any(math.isfinite(value) for value in ys):
                    self.hp_stream_axes.relim()
                    self.hp_stream_axes.autoscale_view()
                self.hp_stream_axes.set_xlim(max(0, xs[-1] - 30), max(30, xs[-1]))
                self.hp_stream_canvas.draw_idle()
            actual = ((len(self.hp_stream_times) - 1) /
                      (self.hp_stream_times[-1] - self.hp_stream_times[0])
                      if len(self.hp_stream_times) > 1 and
                      self.hp_stream_times[-1] > self.hp_stream_times[0] else 0)
            detail = (f"{self.hp_stream_count} reads; requested {self.hp_stream_rate_hz:g} Hz; "
                      f"achieved {actual:.1f} GPIB reads/s")
            self.hp_stream_status_var.set(detail)
            self.update_activity("hp_stream", detail=f"HP meter stream: {detail}")
        if schedule and self.hp_meter_busy == "stream":
            self.hp_stream_poll_id = self.after(100, self._poll_hp_stream)

    def finish_hp_stream(self, error=None):
        if self.hp_stream_poll_id is not None:
            self.after_cancel(self.hp_stream_poll_id)
            self.hp_stream_poll_id = None
        self._poll_hp_stream(schedule=False)
        self.hp_meter_busy = None
        self.hp_stream_stop = None
        self.hp_stream_start_button.configure(state="normal")
        self.hp_stream_stop_button.configure(state="disabled")
        detail = f"HP meter stream stopped after {self.hp_stream_count} readings"
        if error:
            detail += f": {error}"
        self.hp_stream_status_var.set(detail)
        self.finish_activity("hp_stream", detail)
        if error:
            messagebox.showerror("HP meter stream failed", error, parent=self)


    def confirm_unsafe_voltage(self, name, voltage):
        if voltage in self._approved_unsafe_voltages:
            return True
        accepted = self._ask_on_main_thread(
            "Voltage outside 0–5 V",
            f"{name} is about to receive {voltage:g} V.\n\n"
            "Disconnect the Arduino sampler from the voltage-board signal "
            "and check the heater overcurrent limit before continuing.\n\n"
            "Apply this voltage?",
        )
        if accepted:
            self._approved_unsafe_voltages.add(voltage)
        return accepted

    def confirm_over_safe_current(self, name, voltage, safe):
        """Warn before a heater exceeds the safe current density."""
        if (name, voltage) in self._approved_over_safe:
            return True
        broken = self.controller.voltage_limits(name)[1]
        accepted = self._ask_on_main_thread(
            "Heater current above the safe limit",
            f"{name} is about to receive {voltage:g} V, above its {SAFE_CURRENT_DENSITY:g} mA/µm² "
            f"safe current density ({safe:.2f} V).\n\n"
            f"The hard limit is {broken:.2f} V ({BREAK_CURRENT_DENSITY:g} mA/µm²).\n\n"
            "Apply this voltage?",
            icon=messagebox.WARNING,
        )
        if accepted:
            self._approved_over_safe.add((name, voltage))
        return accepted

    def confirm_voltage_sweep(self, voltages, parent=None, names=()):
        """Check a planned sweep against heater limits and the 0–5 V range up front."""
        # The break limit is never overridable, so refuse the whole sweep before it starts.
        for name in names:
            for voltage in voltages:
                self.controller.check_current_density(name, voltage)
        over_safe = {(name, voltage) for name in names for voltage in voltages
                     if self.controller.check_current_density(name, voltage) is not None
                     and (name, voltage) not in self._approved_over_safe}
        if over_safe:
            lines = "\n".join(
                f"{name}: safe up to {self.controller.voltage_limits(name)[0]:.2f} V, "
                f"scan reaches {max(abs(v) for n, v in over_safe if n == name):g} V"
                for name in sorted({name for name, _ in over_safe}))
            if not messagebox.askyesno(
                "Heater current above the safe limit",
                f"This sweep exceeds the {SAFE_CURRENT_DENSITY:g} mA/µm² safe current density:\n\n"
                f"{lines}\n\nApprove these voltages?",
                icon=messagebox.WARNING, parent=parent or self,
            ):
                raise ValueError("Voltages above the safe current density were not confirmed")
            self._approved_over_safe.update(over_safe)
        unsafe = {voltage for voltage in voltages
                  if not 0 <= voltage <= 5 and voltage not in self._approved_unsafe_voltages}
        if not unsafe:
            return
        if not messagebox.askyesno(
            "Voltage sweep outside 0–5 V",
            f"This scan will apply voltages from {min(voltages):g} to {max(voltages):g} V.\n\n"
            "Disconnect the Arduino sampler from the voltage-board signal "
            "and check the heater overcurrent limit before continuing.\n\n"
            "Approve these scan voltages?",
            parent=parent or self,
        ):
            raise ValueError("Voltage sweep was not confirmed")
        self._approved_unsafe_voltages.update(unsafe)

    def connect_board(self):
        try:
            self.controller.configure_connection(self.port_var.get(), self.baud_var.get(), self.dry_run_var.get())
            self.controller.connect()
            self.controller.apply_calibrated_states(self.network.states)
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Board connection failed", str(error), parent=self)
            return
        self.zero_volt_switches.clear()
        mode = "dry run" if self.controller.settings["dry_run"] else self.controller.settings["port"]
        self.show_calibrated_voltages()
        self.update_hardware_status(f"Connected: {mode}")

    def disconnect_board(self):
        self.controller.disconnect()
        self.update_hardware_status("Disconnected")

    def connect_power_meter(self):
        try:
            if self.power_meter.connected:
                raise RuntimeError("Disconnect the Arduino power meter before changing its connection settings")
            self.power_meter.configure(
                self.arduino_port_var.get(), self.arduino_baud_var.get(), self.arduino_duration_var.get(),
                self.arduino_scale_var.get(), self.arduino_offset_var.get(), self.arduino_unit_var.get(),
                self.arduino_stream_var.get(), self.arduino_ceiling_var.get(),
                self.arduino_jump_guard_var.get(),
            )
            self.power_meter.connect()
            self._approved_unsafe_voltages.clear()
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Arduino connection failed", str(error), parent=self)
            return
        self.power_reading_var.set(f"Arduino connected on {self.power_meter.settings['port']}")

    def disconnect_power_meter(self):
        self.power_meter.disconnect()
        self._approved_unsafe_voltages.clear()
        self.power_reading_var.set("Arduino disconnected")

    def osa_switch_channel_map(self):
        return {str(output): variable.get() for output, variable in self.osa_switch_channel_vars.items()}

    def save_osa_output_switch_settings(self):
        try:
            self.osa_output_switch.configure(
                self.osa_switch_resource_var.get(), self.osa_switch_dry_run_var.get(),
                self.osa_switch_settle_var.get(), self.osa_switch_channel_map(),
            )
        except Exception as error:
            # Changing the resource or dry-run mode reconnects, which can raise PyVISA errors.
            messagebox.showerror("OSA output switch", str(error), parent=self)
            return False
        return True

    def auto_connect_osa_output_switch(self):
        """Connect the JDSU in the background at startup; a missing switch only updates the status."""
        switch = self.osa_output_switch
        self.osa_switch_status_var.set(f"Connecting OSA output switch on {switch.settings['resource']}...")

        def connect():
            try:
                switch.connect()
            except Exception as error:
                message = f"OSA output switch not connected: {error}"
            else:
                message = f"OSA output switch connected: {switch.settings['resource']}"
            self.after(0, lambda: self.osa_switch_status_var.set(message))

        threading.Thread(target=connect, daemon=True).start()

    def connect_osa_output_switch(self):
        if not self.save_osa_output_switch_settings():
            return
        try:
            self.osa_output_switch.connect()
        except Exception as error:
            # PyVISA raises its own error types for GPIB faults.
            messagebox.showerror("OSA output switch", str(error), parent=self)
            return
        mode = "dry run" if self.osa_output_switch.settings["dry_run"] else self.osa_output_switch.settings["resource"]
        self.osa_switch_status_var.set(f"OSA output switch connected: {mode}")
        self.osa_output_chosen(self.osa_switch_output_var)

    def disconnect_osa_output_switch(self):
        self.osa_output_switch.disconnect()
        self.osa_switch_status_var.set("OSA output switch disconnected")

    def osa_output_chosen(self, variable):
        """Mirror an OSA output choice to the other picker and route the JDSU to it."""
        if self._syncing_osa_output:
            return
        value = variable.get()
        if value == "None":
            return
        other = self.osa_switch_output_var if variable is self.osa_output_var else self.osa_output_var
        self._syncing_osa_output = True
        try:
            other.set(value)
        finally:
            self._syncing_osa_output = False
        self.route_osa_output(int(value))

    def route_osa_output(self, output):
        switch = self.osa_output_switch
        if not switch.connected or switch.routed_output == output:
            return
        try:
            channel = switch.channel_for(output)
        except ValueError as error:
            self.osa_switch_status_var.set(str(error))
            return
        self.osa_switch_status_var.set(f"Routing output {output} (fiber {fiber_number(output)}) "
                                       f"to the OSA via JDSU channel {channel}...")

        def route():
            try:
                switch.select_output(output)
            except Exception as error:
                self.after(0, lambda error=error: self.osa_switch_status_var.set(f"OSA output switch failed: {error}"))
            else:
                self.after(0, lambda: self.osa_switch_status_var.set(
                    f"OSA sampling output {output} (fiber {fiber_number(output)}) via JDSU channel {channel}"))

        threading.Thread(target=route, daemon=True).start()

    def show_osa_output_switch_setup(self):
        """Keep GPIB details and the fiber patching out of the normal measurement workflow."""
        dialog = tk.Toplevel(self)
        dialog.title("OSA output switch setup")
        dialog.configure(bg=BG)
        dialog.transient(self)
        dialog.grab_set()
        self.fit_dialog_to_screen(dialog, preferred_width=560, preferred_height=620)
        body = ttk.Frame(dialog, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="JDSU SC optical switch", foreground=ACCENT,
                  font=("Segoe UI", 12, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(body, text="Each Benes output fiber enters one JDSU channel; the switch common port "
                             "feeds the OSA. The switch must use its SC command set with CR+LF termination.",
                  foreground="#9baec6", wraplength=480).grid(row=1, column=0, columnspan=3,
                                                               sticky="w", pady=(3, 14))
        ttk.Label(body, text="VISA resource").grid(row=2, column=0, sticky="w", pady=4)
        resource_box = ttk.Combobox(body, textvariable=self.osa_switch_resource_var, width=22)
        resource_box.grid(row=2, column=1, sticky="w", padx=(12, 0), pady=4)

        def list_resources():
            try:
                resources = self.osa_output_switch.list_resources()
            except Exception as error:
                messagebox.showerror("VISA resources", str(error), parent=dialog)
                return
            resource_box.configure(values=resources)
            if not resources:
                messagebox.showinfo("VISA resources", "No VISA resources found", parent=dialog)

        ttk.Button(body, text="List", command=list_resources).grid(row=2, column=2, sticky="w", padx=(6, 0))
        ttk.Label(body, text="Extra settle [s]").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.osa_switch_settle_var, width=10).grid(
            row=3, column=1, sticky="w", padx=(12, 0), pady=4)
        ttk.Checkbutton(body, text="Dry run", variable=self.osa_switch_dry_run_var).grid(
            row=4, column=1, sticky="w", padx=(12, 0), pady=(4, 0))

        mapping = ttk.LabelFrame(body, text="Output fiber → JDSU channel", padding=(14, 8))
        mapping.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(16, 0))
        channels = ("—",) + tuple(map(str, JDSU_CHANNELS))
        for row, output in enumerate(OUTPUTS):
            ttk.Label(mapping, text=f"Output {output} · Fiber {fiber_number(output)}").grid(
                row=row, column=0, sticky="w", pady=3)
            ttk.Combobox(mapping, textvariable=self.osa_switch_channel_vars[output], values=channels,
                         state="readonly", width=6).grid(row=row, column=1, sticky="w", padx=(14, 0), pady=3)
        ttk.Label(mapping, text="Each channel can be used once; an output set to — is skipped by Read all outputs.",
                  foreground="#9baec6", wraplength=440).grid(row=len(OUTPUTS), column=0, columnspan=2,
                                                              sticky="w", pady=(8, 0))

        def save_and_close():
            if self.save_osa_output_switch_settings():
                dialog.destroy()

        def connect_and_close():
            self.connect_osa_output_switch()
            if self.osa_output_switch.connected:
                dialog.destroy()

        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=3, sticky="e", pady=(18, 0))
        ttk.Button(buttons, text="Disconnect", command=self.disconnect_osa_output_switch).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Save", command=save_and_close).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Connect", style="Accent.TButton", command=connect_and_close).pack(side="left")

    def read_current_power(self):
        self.begin_activity("power_read", "Reading Arduino power...")
        self.read_power_button.configure(state="disabled")

        def acquire():
            try:
                reading = self.power_meter.read_current_power()
            except Exception as error:
                self.after(0, lambda error=error: self.power_read_failed(str(error)))
            else:
                self.after(0, lambda: self.show_current_power(reading))

        threading.Thread(target=acquire, daemon=True).start()

    def power_read_failed(self, error):
        self.finish_activity("power_read", "Arduino power read failed")
        self.read_power_button.configure(state="normal")
        messagebox.showerror("Arduino power read failed", error, parent=self)

    def show_current_power(self, reading):
        self.finish_activity("power_read", "Arduino power reading complete")
        self.read_power_button.configure(state="normal")
        self.power_reading_var.set(
            f"Current power: {reading.value:.4g} {reading.unit} "
            f"(ADC mean {reading.raw_mean:.2f}, {reading.sample_count} clean samples"
            + (f", {reading.rejected_count} rejected, {reading.jump_count} jumps" if reading.rejected_count else "")
            + (f", ~{reading.sample_rate_hz:,.0f} raw samples/s" if reading.sample_rate_hz else "")
            + ")"
        )

    def apply_calibration(self):
        try:
            self.controller.apply_calibrated_states(self.network.states)
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Calibration command failed", str(error), parent=self)
            return
        self.zero_volt_switches.clear()
        self.show_calibrated_voltages()

    def show_calibrated_voltages(self):
        for name in self.network.switches:
            state = "cross" if self.network.states[name] else "straight"
            voltage = self.controller.settings["calibration"][name][state]
            if voltage is not None:
                self.voltage_vars[name].set(f"{voltage:.2f}")

    def update_hardware_status(self, detail=""):
        prefix = "Board connected" if self.controller.connected else "Board disconnected"
        if self.controller.load_error:
            prefix = self.controller.load_error
        self.hardware_status.configure(text=f"{prefix}{' · ' + detail if detail else ''}")
