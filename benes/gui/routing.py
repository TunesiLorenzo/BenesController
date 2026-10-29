"""Routing tab: input/output selection, switch states, permutations and the network diagram."""

import json
import random
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from benes.gui.theme import (ACCENT, BG, COLORS, OSA_COLOR, PANEL, POWER_METER_COLOR, TEXT, WDM_COLORS, WIRE,
                             ZERO_VOLT_COLOR, blend)
from benes.gui.calibration import METER_VIA_JDSU
from benes.hardware.power_meter import RANGE_TRACKING_OFF
from benes.measurements.arduino_hp_range import (DEFAULT_HIGH_ADC, DEFAULT_LOW_ADC, ArduinoHPRangeSweep,
                                                 measure_bnc_reference)
from benes.measurements.laser_calibration import CalibrationStopped
from benes.measurements.mzi_characterization import mw_to_dbm
from benes.measurements.voltage_optimizer import (OBJECTIVE_LEAK, OBJECTIVE_THROUGH, optimization_plan,
                                                  optimize_paths)
from benes.paths import app_dir

# Ring around an input whose Newport laser is on.
LASER_ON_COLOR = "#ff4d4d"
# Input lit and output read for the BNC dBm reference, kept between sessions.
BNC_REFERENCE_CONFIG = "benes_bnc_reference_config.json"
BNC_REFERENCE_DEFAULT = {"input": 1, "output": 5}


class RoutingMixin:
    """Mixed into ``BenesApp``; relies on the state it sets up."""

    def build_routing_tab(self):
        """Route selection, detector rings, routing sidebar and the network diagram."""
        controls = ttk.Frame(self.route_tab, padding=(24, 8))
        controls.pack(fill="x")
        ttk.Label(controls, text="Input").pack(side="left")
        self.input_var = tk.StringVar(value="None")
        self.input_box = ttk.Combobox(controls, textvariable=self.input_var,
                                      values=("None", "1", "2", "3", "4", "5", "6"), width=5,
                                      state="readonly")
        self.input_box.pack(side="left", padx=(8, 20))
        self.input_box.bind("<<ComboboxSelected>>", self.choose_input)
        ttk.Label(controls, text="Output").pack(side="left")
        self.output_var = tk.StringVar(value="None")
        self.output_box = ttk.Combobox(controls, textvariable=self.output_var,
                                       values=("None", "1", "2", "3", "4", "5", "6"), width=5,
                                       state="readonly")
        self.output_box.pack(side="left", padx=8)
        self.output_box.bind("<<ComboboxSelected>>", self.choose_output)
        ttk.Checkbutton(controls, text="Overlay all possible paths", variable=self.all_paths, command=self.draw).pack(side="left", padx=15)
        self.evaluate_button = ttk.Button(controls, text="Evaluate Configuration", style="Accent.TButton",
                                          command=self.start_configuration_evaluation)
        self.evaluate_button.pack(side="left", padx=(0, 12))
        self.optimize_button = ttk.Button(controls, text="Optimize voltages",
                                          command=self.toggle_voltage_optimizer)
        self.optimize_button.pack(side="left", padx=(0, 12))
        self.route_optimizer_running = False
        self.route_optimizer_stop = None
        ttk.Button(controls, text="Reset switches", command=self.reset).pack(side="right")
        ttk.Button(controls, text="Random switches", command=self.randomize).pack(side="right", padx=8)

        self.route_arduino_var = tk.StringVar(value="Arduino: — dBm (connect Arduino and HP)")
        arduino_readout = ttk.Frame(self.route_tab, padding=(24, 0, 24, 6))
        arduino_readout.pack(fill="x")
        ttk.Label(arduino_readout, textvariable=self.route_arduino_var, foreground=POWER_METER_COLOR,
                  font=("Segoe UI", 11, "bold")).pack(side="left")
        self.bnc_reference_button = ttk.Button(arduino_readout, text="Set BNC dBm reference",
                                                command=self.start_bnc_reference)
        self.bnc_reference_button.pack(side="left", padx=(14, 0))
        pair = self.saved_bnc_reference_ports()
        self.bnc_input_var = tk.StringVar(value=str(pair["input"]))
        self.bnc_output_var = tk.StringVar(value=str(pair["output"]))
        ttk.Label(arduino_readout, text="from input").pack(side="left", padx=(10, 4))
        ttk.Combobox(arduino_readout, textvariable=self.bnc_input_var, values=tuple("123456"),
                     state="readonly", width=3).pack(side="left")
        ttk.Label(arduino_readout, text="at output").pack(side="left", padx=(8, 4))
        ttk.Combobox(arduino_readout, textvariable=self.bnc_output_var, values=tuple("123456"),
                     state="readonly", width=3).pack(side="left")
        for variable in (self.bnc_input_var, self.bnc_output_var):
            variable.trace_add("write", lambda *_: self.remember_bnc_reference_ports())

        detector_controls = ttk.Frame(self.route_tab, padding=(24, 0, 24, 8))
        detector_controls.pack(fill="x")
        ttk.Label(detector_controls, text="Power Meter output", foreground=POWER_METER_COLOR).pack(side="left")
        ttk.Combobox(detector_controls, textvariable=self.power_meter_output_var,
                     values=("None", "1", "2", "3", "4", "5", "6"), state="readonly", width=6).pack(side="left", padx=(7, 24))
        ttk.Label(detector_controls, text="OSA output", foreground=OSA_COLOR).pack(side="left")
        ttk.Combobox(detector_controls, textvariable=self.osa_output_var,
                     values=("None", "1", "2", "3", "4", "5", "6"), state="readonly", width=6).pack(side="left", padx=(7, 18))
        ttk.Label(detector_controls, text="Colored rings mark detector ports. Once connected, clicking an input "
                  "also toggles its Newport laser (red ring when on), and clicking an output routes the JDSU to it.",
                  wraplength=620).pack(side="left")
        self.power_meter_output_var.trace_add("write", lambda *_: self.draw())
        self.osa_output_var.trace_add("write", lambda *_: self.draw())
        # Choosing an OSA output on either page routes the JDSU switch to it.
        self.osa_output_var.trace_add("write", lambda *_: self.osa_output_chosen(self.osa_output_var))
        self._route_arduino_busy = False
        self._route_arduino_last_time = None
        self.after(300, self.poll_route_arduino)


        # Keep every routing action beside the diagram.  The old stacked layout
        # let the canvas consume the window height and clipped these controls.
        self.status = ttk.Label(self.route_tab, font=("Segoe UI", 11, "bold"), padding=(24, 4))
        self.status.pack(fill="x")
        self.mapping_label = ttk.Label(self.route_tab, padding=(24, 2, 24, 6))
        self.mapping_label.pack(fill="x")
        self.wdm_route_label = ttk.Label(self.route_tab, padding=(24, 0, 24, 6), foreground=OSA_COLOR)
        self.wdm_route_label.pack(fill="x")

        route_body = ttk.Frame(self.route_tab, padding=(12, 0, 12, 10))
        route_body.pack(fill="both", expand=True)
        route_sidebar = ttk.LabelFrame(route_body, text="Routing controls", padding=(12, 8), width=285)
        # Natural size (no pack_propagate(False)) so the tab knows when to scroll.
        route_sidebar.pack(side="right", fill="y", padx=(12, 0))

        self.route_label = ttk.Label(route_sidebar, text="", wraplength=255, justify="left")
        self.route_label.pack(anchor="w", fill="x")
        route_list_frame = ttk.Frame(route_sidebar)
        route_list_frame.pack(fill="both", expand=True, pady=(6, 8))
        self.route_list = tk.Listbox(route_list_frame, height=7, bg=PANEL, fg=TEXT,
                                     selectbackground="#315b78", selectforeground="white",
                                     font=("Consolas", 9), exportselection=False, borderwidth=0,
                                     highlightthickness=0)
        route_scrollbar = ttk.Scrollbar(route_list_frame, orient="vertical", command=self.route_list.yview)
        self.route_list.configure(yscrollcommand=route_scrollbar.set)
        self.route_list.pack(side="left", fill="both", expand=True)
        route_scrollbar.pack(side="right", fill="y")
        self.route_list.bind("<<ListboxSelect>>", self.select_route)
        ttk.Button(route_sidebar, text="Apply path (drive bar/cross)", command=self.apply_route).pack(anchor="w", fill="x")
        ttk.Checkbutton(route_sidebar, text="Keep complete output permutation",
                        variable=self.keep_permutation_var).pack(anchor="w", pady=(6, 0))
        ttk.Label(route_sidebar, text="Off: drive the selected path and set every other MZI to 0 V. On: adjust other MZIs as needed while retaining every current input → output connection.",
                  wraplength=255, foreground="#9baec6").pack(anchor="w", fill="x", pady=(2, 0))

        ttk.Separator(route_sidebar, orient="horizontal").pack(fill="x", pady=10)
        ttk.Label(route_sidebar, text="Route complete permutation", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(route_sidebar, text="Outputs for inputs 1, 2, 3, 4, 5, 6", wraplength=255).pack(anchor="w", pady=(3, 2))
        self.permutation = ttk.Entry(route_sidebar)
        self.permutation.insert(0, "1 2 3 4 5 6")
        self.permutation.pack(anchor="w", fill="x", pady=(0, 5))
        buttons = ttk.Frame(route_sidebar)
        buttons.pack(anchor="w", fill="x")
        ttk.Button(buttons, text="Route permutation", command=self.route_permutation).pack(side="left", fill="x", expand=True)
        ttk.Button(buttons, text="Shuffle", command=self.shuffle).pack(side="left", padx=(6, 0))
        ttk.Label(route_sidebar, text="Equivalent full-network routings").pack(anchor="w", pady=(10, 2))
        self.permutation_route_list = tk.Listbox(route_sidebar, height=5, bg=PANEL, fg=TEXT,
                                                 selectbackground="#315b78", selectforeground="white",
                                                 exportselection=False, state="disabled", borderwidth=0,
                                                 highlightthickness=0)
        self.permutation_route_list.pack(anchor="w", fill="x")
        self.permutation_route_list.bind("<<ListboxSelect>>", self.show_permutation_route)

        diagram = ttk.Frame(route_body)
        diagram.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(diagram, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda event: self.draw())
        ttk.Label(diagram, text="Green switches have saved bar and cross voltages. Use the X, =, and 0 controls beside each MZI to set cross, bar, or 0 V; clicking an MZI still cycles the states. Type a voltage and press Enter to send it.", padding=(4, 5), wraplength=680).pack(anchor="w", fill="x")

    def route_arduino_available(self):
        """Read only while routing is visible and no job owns the Arduino."""
        page = self.tab_page(self.route_tab)
        blocked = {"calibration", "crosstalk", "dataset", "mapping_scan", "power_read", "route_optimize"}
        return (self.power_meter.connected and self.hp_meter_session is not None
                and self.bnc_reference is not None and not self.hp_meter_busy
                and not self._bnc_reference_busy and self.notebook.select() == str(page)
                and not blocked.intersection(self.activities)
                and not self.power_meter._calibration_capture)

    def poll_route_arduino(self):
        self.after(300, self.poll_route_arduino)
        if self._bnc_reference_busy:
            self.route_arduino_var.set("Arduino: measuring dBm reference...")
            return
        if not self.power_meter.connected:
            self._route_arduino_last_time = None
            self.route_arduino_var.set("Arduino: — dBm (connect Arduino)")
            return
        if self.hp_meter_session is None:
            self._route_arduino_last_time = None
            self.route_arduino_var.set("Arduino: — dBm (connect HP 8152A)")
            return
        if self.bnc_reference is None:
            self._route_arduino_last_time = None
            self.route_arduino_var.set("Arduino: — dBm (set BNC reference)")
            return
        if not self.route_arduino_available():
            self._route_arduino_last_time = None
            if self.notebook.select() == str(self.tab_page(self.route_tab)):
                self.route_arduino_var.set("Arduino: paused during measurement")
            return
        if self._route_arduino_busy:
            return
        self._route_arduino_busy = True

        def acquire():
            try:
                meter = self.hp_meter_session
                reference = self.bnc_reference
                range_before = meter.read_range_dbm(reference.channel)
                reading = self.power_meter.read_current_power(
                    ceiling_override=self.power_meter.full_scale + 1,
                    range_tracking_mode=RANGE_TRACKING_OFF, ignore_jumps=True)
                range_after = meter.read_range_dbm(reference.channel)
                if range_before != range_after:
                    raise RuntimeError("HP range changed during sample; retrying")
                error = None
            except Exception as failure:
                reading, range_before, error = None, None, str(failure)
            try:
                self.after(0, self.finish_route_arduino, reading, range_before, error)
            except (RuntimeError, tk.TclError):
                pass  # The window closed while the read was finishing.

        threading.Thread(target=acquire, daemon=True).start()

    def finish_route_arduino(self, reading, range_dbm, error):
        self._route_arduino_busy = False
        if not self.route_arduino_available():
            return
        if error:
            self.route_arduino_var.set(f"Arduino: {error}")
        else:
            dbm = self.bnc_reference.dbm_at(reading.raw_mean, range_dbm)
            saturation = " · above ceiling" if reading.raw_mean >= self.power_meter.ceiling else ""
            power = "below ADC floor" if dbm is None else f"{dbm:.2f} dBm"
            now = time.monotonic()
            last = self._route_arduino_last_time
            self._route_arduino_last_time = now
            cadence = f" · {1 / (now - last):.1f} updates/s" if last is not None and now > last else ""
            self.route_arduino_var.set(
                f"Arduino: {power} · A0 {reading.raw_mean:.2f} ADC · "
                f"HP range {range_dbm:+d} dBm{cadence}{saturation}")

    @staticmethod
    def saved_bnc_reference_ports():
        try:
            stored = json.loads((app_dir() / BNC_REFERENCE_CONFIG).read_text(encoding="utf-8"))
            pair = {key: int(stored[key]) for key in BNC_REFERENCE_DEFAULT}
            if all(1 <= value <= 6 for value in pair.values()):
                return pair
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return dict(BNC_REFERENCE_DEFAULT)

    def remember_bnc_reference_ports(self):
        try:
            pair = {"input": int(self.bnc_input_var.get()), "output": int(self.bnc_output_var.get())}
            (app_dir() / BNC_REFERENCE_CONFIG).write_text(json.dumps(pair, indent=2) + "\n", encoding="utf-8")
        except (OSError, ValueError):
            pass

    def auto_bnc_reference(self):
        """After connecting at launch, take the reference if every instrument it needs is there."""
        if (self.bnc_reference is None and self.power_meter.connected and self.hp_meter_session is not None
                and not self.hp_meter_busy and not self.measurement_busy()):
            self.start_bnc_reference(automatic=True)

    def start_bnc_reference(self, automatic=False):
        """Pair one steady HP GPIB power reading with Arduino BNC counts.

        With the Newport controller connected, only the chosen input is lit;
        with the JDSU connected, it is routed to the chosen output. Both return
        to their earlier state afterwards (the JDSU stays on that output).
        """
        if self._bnc_reference_busy:
            return
        if not self.power_meter.connected or self.hp_meter_session is None or self.hp_meter_busy:
            if not automatic:
                messagebox.showerror("BNC reference unavailable", "Connect both Arduino and HP 8152A first.",
                                     parent=self)
            return
        source, output = int(self.bnc_input_var.get()), int(self.bnc_output_var.get())
        use_lasers = self.newport_controller.connected and not self.newport_panel.busy
        if automatic and not use_lasers:
            # Without the Newport nothing is lit at launch, so the light check would only time out.
            self.route_arduino_var.set("Arduino: — dBm (Newport not connected; press Set BNC dBm reference "
                                       "once the reference light is on)")
            return
        if use_lasers:
            try:
                self.laser_bank.configure({port: variable.get() for port, variable in self.laser_slot_vars.items()},
                                          self.laser_settle_var.get())
            except ValueError as error:
                self.route_arduino_var.set(f"Arduino: — dBm (laser settings: {error})")
                return
            self.newport_panel.busy = True
            self.newport_panel.render()
        switch = self.osa_output_switch if self.osa_output_switch.connected else None
        options = self.hp_meter_session_options
        channel = 1 if options["channel"] == "A" else 2
        self.bnc_reference = None
        self._bnc_reference_busy = True
        self.hp_meter_busy = "bnc_reference"
        self.refresh_hp_connection_buttons()
        self.bnc_reference_button.configure(state="disabled")
        where = (f"input {source}" if use_lasers else "the present light (Newport not connected)") + (
            f" → output {output}" if switch else "")
        self.route_arduino_var.set(f"Arduino: measuring dBm reference from {where}...")

        def acquire():
            hp = self.hp_meter_session
            result = error = None
            try:
                if switch is not None:
                    switch.select_output(output)
                if use_lasers:
                    self.laser_bank.begin()
                    self.laser_bank.light_only(source - 1)
                    self.after(0, self.show_reference_laser)
                result = measure_bnc_reference(self.power_meter, hp, channel, options["resource"])
            except Exception as failure:
                error = str(failure) or type(failure).__name__
            finally:
                cleanup = []
                if use_lasers:
                    try:
                        self.laser_bank.finish()
                    except Exception as failure:
                        cleanup.append(str(failure))
                try:
                    # The reference fixed the HP range; return to the connection settings.
                    self.configure_hp_meter(hp, options)
                except Exception as failure:
                    cleanup.append(f"HP settings: {failure}")
                if cleanup and error is None:
                    error = "; ".join(cleanup)
            try:
                self.after(0, self.finish_bnc_reference, result, error, use_lasers, switch is not None)
            except (RuntimeError, tk.TclError):
                pass

        threading.Thread(target=acquire, daemon=True).start()

    def show_reference_laser(self):
        """Draw the ring of the laser lit for the reference while it is measured."""
        try:
            self.newport_panel.render()
            self.draw()
        except tk.TclError:
            pass  # The window closed while the reference was running.

    def finish_bnc_reference(self, reference, error, used_lasers=False, used_switch=False):
        self._bnc_reference_busy = False
        self.hp_meter_busy = None
        if used_lasers:
            self.newport_panel.busy = False
            try:
                self.newport_controller.read_states()
            except Exception:
                pass  # The panel shows the last known states; Refresh reads them again.
            self.newport_panel.render()
            self.draw()
        if used_switch:
            self.osa_output_var.set(str(self.osa_output_switch.routed_output))
        self.refresh_hp_connection_buttons()
        self.bnc_reference_button.configure(state="normal")
        if error:
            self.route_arduino_var.set(f"Arduino: — dBm ({error})")
        else:
            self.bnc_reference = reference
            self.route_arduino_var.set(
                f"Arduino: reference {reference.dbm:.2f} dBm at {reference.adc:.2f} ADC "
                f"(HP range {reference.range_dbm:+d} dBm)")

    def toggle_voltage_optimizer(self):
        if self.route_optimizer_running:
            self.route_optimizer_stop.set()
            self.optimize_button.configure(state="disabled")
        else:
            self.start_voltage_optimizer()

    def start_voltage_optimizer(self):
        """Nudge the MZIs on the lit paths for the most Arduino power at their routed outputs.

        With an input selected only its path is tuned; otherwise every input is lit in
        turn through the Newport. Through the JDSU each MZI is tuned for the least light
        at its leak output; a meter fixed on one output can only maximize the routed power.
        """
        try:
            if self.measurement_busy() or self._bnc_reference_busy:
                raise RuntimeError("Wait for the current measurement to finish")
            if not self.controller.connected:
                raise RuntimeError("Connect the voltage board first (Hardware)")
            if not self.power_meter.connected or self.hp_meter_session is None:
                raise RuntimeError("Connect both the Arduino and the HP 8152A first")
            if self.hp_meter_busy:
                raise RuntimeError("Stop the HP meter stream or test first")
            if self.bnc_reference is None:
                raise ValueError("Set the BNC dBm reference first")
            options = self.hp_meter_session_options
            channel = 1 if options["channel"] == "A" else 2
            reference = self.bnc_reference
            if (reference.resource, reference.channel) != (options["resource"], channel):
                raise ValueError("Set a new BNC dBm reference for this HP channel")
            try:
                low_adc, high_adc = float(self.hp_bnc_low_adc_var.get()), float(self.hp_bnc_high_adc_var.get())
            except ValueError:
                low_adc, high_adc = DEFAULT_LOW_ADC, DEFAULT_HIGH_ADC
            use_lasers = self.newport_controller.connected and not self.newport_panel.busy
            sources = [self.source] if self.source is not None else list(range(self.network.size))
            if len(sources) > 1 and not use_lasers:
                raise ValueError("Select one input to tune with the light that is on now, or connect the "
                                 "Newport controller to light the inputs one at a time")
            if use_lasers:
                self.laser_bank.configure({port: variable.get() for port, variable in self.laser_slot_vars.items()},
                                          self.laser_settle_var.get())
            switch = None
            plan = optimization_plan(self.network, sources)
            if self.detection_position_var.get() == METER_VIA_JDSU:
                if not self.osa_output_switch.connected:
                    raise RuntimeError("Connect the JDSU switch, or set where the meter sits in "
                                       "MZI calibration → Detection")
                switch = self.osa_output_switch
            else:
                fixed = int(self.detection_position_var.get().split()[-1]) - 1
                plan = tuple(item for item in plan if item[1] == fixed)
                if not plan:
                    raise ValueError(f"The meter sits on output {fixed + 1}, which "
                                     + (f"input {sources[0] + 1} does not reach now" if len(sources) == 1
                                        else "no input reaches now"))
            objective = OBJECTIVE_LEAK if switch is not None else OBJECTIVE_THROUGH
            mapped = self.controller.settings["mapping"]
            plan = tuple((source, output, tuple((name, leak) for name, leak in names
                                                if mapped.get(name) is not None
                                                and name in self.controller.applied_voltages))
                         for source, output, names in plan)
            total = sum(len(names) for _, _, names in plan)
            if not total:
                raise ValueError("No MZI on the chosen path has a board pin and a known voltage")
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Voltage optimizer unavailable", str(error), parent=self)
            return

        self.route_optimizer_running = True
        self.route_optimizer_stop = stop_event = threading.Event()
        self.optimize_button.configure(text="Stop optimizer")
        self.evaluate_button.configure(state="disabled")
        self.hp_meter_busy = "optimizer"
        self.refresh_hp_connection_buttons()
        if use_lasers:
            self.newport_panel.busy = True
            self.newport_panel.render()
        zero_before = set(self.zero_volt_switches)
        states_before = dict(self.network.states)
        which = f"input {sources[0] + 1}" if len(sources) == 1 else f"{len(plan)} inputs, one at a time"
        goal = ("least light at each MZI's leak output" if objective == OBJECTIVE_LEAK
                else "most light at the routed output")
        self.begin_activity("route_optimize", f"Optimizing MZI voltages for {which}...", total)
        self.status.configure(text=f"Optimizing {total} MZI voltage(s) for {which}: {goal}...")
        previous_output = switch.routed_output if switch is not None else None
        done = []
        paths = []

        def on_read(source, output, name, voltage, power):
            dbm = mw_to_dbm(power) if power > 0 else None
            text = "below floor" if dbm is None else f"{dbm:.2f} dBm"
            self.after(0, lambda: self.route_arduino_var.set(
                f"Optimizer: input {source + 1}, {name} at {voltage:.2f} V → output {output + 1}: {text}"))

        def on_switch(item):
            self.after(0, self.voltage_optimizer_switch_done, item, len(done), total)

        def light(source):
            self.laser_bank.light_only(source)
            self.after(0, self.show_reference_laser)

        def worker():
            hp = self.hp_meter_session
            error = None
            stopped = False
            try:
                if use_lasers:
                    self.laser_bank.begin()
                reader = ArduinoHPRangeSweep(self.power_meter, hp, channel, low_adc=low_adc, high_adc=high_adc,
                                             bnc_reference=reference)
                paths.extend(optimize_paths(
                    self.controller, plan, reader.read, objective=objective,
                    light=light if use_lasers else None,
                    select_output=None if switch is None else (
                        lambda output: switch.select_output(output + 1)),
                    allowed=self.optimizer_voltage_allowed, on_switch=on_switch, on_read=on_read,
                    stop_event=stop_event, results=done))
            except CalibrationStopped:
                stopped = True
            except Exception as failure:
                error = str(failure) or type(failure).__name__
            finally:
                cleanup = []
                if use_lasers:
                    try:
                        self.laser_bank.finish()
                    except Exception as failure:
                        cleanup.append(str(failure))
                try:
                    self.configure_hp_meter(hp, options)
                except Exception as failure:
                    cleanup.append(f"HP settings: {failure}")
                if previous_output is not None:
                    try:
                        switch.select_output(previous_output)
                    except Exception as failure:
                        cleanup.append(f"JDSU restore: {failure}")
                if cleanup and error is None:
                    error = "; ".join(cleanup)
            try:
                self.after(0, self.finish_voltage_optimizer, list(done), paths, error, stopped, use_lasers,
                           switch is not None, zero_before, states_before)
            except (RuntimeError, tk.TclError):
                pass

        threading.Thread(target=worker, daemon=True).start()

    def optimizer_voltage_allowed(self, name, voltage):
        """Only voltages the board sends without asking; a search must not stop for a dialog."""
        try:
            return self.controller.check_current_density(name, voltage) is None
        except ValueError:
            return False

    def voltage_optimizer_switch_done(self, item, done, total):
        self.voltage_vars[item.name].set(f"{item.best_v:.2f}")
        if item.moved:
            self.zero_volt_switches.discard(item.name)
        gain = item.gain_db
        self.update_activity("route_optimize", done, detail=(
            f"Input {item.source + 1}: {item.name} {item.start_v:.2f} → {item.best_v:.2f} V"
            + ("" if gain is None else f" ({gain:+.2f} dB)")))
        self.draw()

    def finish_voltage_optimizer(self, switches, paths, error, stopped, used_lasers, used_switch,
                                 zero_before, states_before):
        self.route_optimizer_running = False
        self.route_optimizer_stop = None
        self.optimize_button.configure(text="Optimize voltages", state="normal")
        self.evaluate_button.configure(state="normal")
        self.hp_meter_busy = None
        self.refresh_hp_connection_buttons()
        if used_lasers:
            self.newport_panel.busy = False
            try:
                self.newport_controller.read_states()
            except Exception:
                pass  # The panel shows the last known states; Refresh reads them again.
            self.newport_panel.render()
        if used_switch and self.osa_output_switch.routed_output is not None:
            self.osa_output_var.set(str(self.osa_output_switch.routed_output))
        for item in switches:
            self.voltage_vars[item.name].set(f"{item.best_v:.2f}")
            if item.moved:
                self.zero_volt_switches.discard(item.name)
        summary = "; ".join(self.describe_optimized_path(path) for path in paths) or (
            f"{len(switches)} MZI(s) finished" if switches else "no MZI finished")
        outcome = "stopped" if stopped else "failed" if error else "done"
        self.finish_activity("route_optimize", f"Voltage optimizer {outcome}: {summary}")
        self.status.configure(text=f"Voltage optimizer {outcome}: {summary}"
                              + (f" · {error}" if error else ""))
        self.update_hardware_status(f"Optimizer moved {sum(item.moved for item in switches)} MZI(s)")
        self.draw()
        if error:
            messagebox.showerror("Voltage optimizer", error, parent=self)
        self.offer_optimized_calibration(switches, zero_before, states_before)

    @staticmethod
    def describe_optimized_path(path):
        """'In 1 → Out 3 +0.12 dB (leaks −4.1 dB)': routed gain, then the mean leak drop."""
        gain = path.gain_db
        text = f"In {path.source + 1} → Out {path.output + 1} " + (
            "no reading" if gain is None else f"{gain:+.2f} dB")
        leaks = [item.gain_db for item in path.switches
                 if item.objective == OBJECTIVE_LEAK and item.gain_db is not None]
        if leaks:
            text += f" (leaks {-sum(leaks) / len(leaks):+.1f} dB)"
        return text

    def offer_optimized_calibration(self, switches, zero_before, states_before):
        """Ask whether the optimized voltages replace the saved cross/bar voltages they started from."""
        calibration = self.controller.settings["calibration"]
        changes = []
        for item in switches:
            if not item.moved or item.name in zero_before:
                continue
            state = "cross" if states_before[item.name] else "straight"
            if calibration[item.name][state] is None:
                continue
            changes.append((item, state))
        if not changes:
            return
        lines = "\n".join(f"{item.name} {'cross' if state == 'cross' else 'bar'}: "
                          f"{calibration[item.name][state]:.2f} → {item.best_v:.2f} V" for item, state in changes)
        if not messagebox.askyesno("Save optimized voltages",
                                   "The optimized voltages are applied now. Also save them as the calibrated "
                                   f"voltages?\n\n{lines}", parent=self):
            return
        try:
            for item, state in changes:
                saved = dict(calibration[item.name])
                saved[state] = item.best_v
                self.controller.set_calibration(item.name, saved["straight"], saved["cross"])
        except (OSError, ValueError) as error:
            messagebox.showerror("Calibration not saved", str(error), parent=self)
            return
        self.update_hardware_status(f"Saved {len(changes)} optimized calibration voltage(s)")
        self.draw()


    def choose_input(self, event=None):
        self.clear_permutation_preview()
        self.source = self.route_port(self.input_var)
        self.refresh()

    def choose_output(self, event=None):
        self.clear_permutation_preview()
        self.destination = self.route_port(self.output_var)
        self.refresh()

    @staticmethod
    def route_port(variable):
        value = variable.get()
        return None if value == "None" else int(value) - 1

    @staticmethod
    def detector_port(variable):
        value = variable.get()
        return None if value == "None" else int(value) - 1

    def click_input(self, row):
        """Select the input; with the Newport connected, also toggle its laser and follow it."""
        lit = self.toggle_input_laser(row)
        if lit is None:
            self.input_var.set("None" if self.source == row else str(row + 1))
        elif lit:
            self.input_var.set(str(row + 1))
        elif self.source == row:
            self.input_var.set("None")
        self.choose_input()

    def toggle_input_laser(self, row):
        """Switch the laser feeding a 0-based input; returns its new state, or None when not possible."""
        controller = self.newport_controller
        if not controller.connected or self.newport_panel.busy or self.measurement_busy():
            return None
        slot = self.laser_bank.slot_for(row)
        module = controller.modules[slot - 1]
        if module.laser is None:
            self.status.configure(text=f"Input {row + 1}: Newport slot {slot} has no laser output")
            return None
        desired = not module.laser
        if desired and module.tec is False:
            messagebox.showerror("Newport laser", f"Turn on the TEC of Newport slot {slot} (input {row + 1}) "
                                 "before its laser.", parent=self)
            return None
        self.newport_panel.toggle(slot, "laser")
        return desired

    def input_laser_on(self, row):
        controller = self.newport_controller
        if not controller.connected:
            return False
        slot = self.laser_bank.slot_for(row)
        return controller.modules[slot - 1].laser is True

    def click_output(self, row):
        """Select the output; with the JDSU connected, also route it to that output."""
        self.output_var.set("None" if self.destination == row else str(row + 1))
        self.choose_output()
        if self.osa_output_switch.connected and not self.measurement_busy():
            # The OSA output picker routes the JDSU when it changes.
            self.osa_output_var.set(str(row + 1))

    def toggle(self, name):
        """Cycle a switch: cross -> straight -> 0 V -> cross."""
        if hasattr(self, "clear_permutation_preview"):
            self.clear_permutation_preview()
        to_zero = name not in self.zero_volt_switches and not self.network.states[name]
        crossed = to_zero or name in self.zero_volt_switches
        try:
            if to_zero:
                voltage = self.controller.drive_voltage(name, 0.0)
            else:
                voltage = self.controller.apply_calibrated_state(name, crossed)
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Calibration command failed", str(error), parent=self)
            return
        self.network.states[name] = crossed
        if to_zero:
            self.zero_volt_switches.add(name)
        else:
            self.zero_volt_switches.discard(name)
        if voltage is not None:
            self.voltage_vars[name].set(f"{voltage:.2f}")
        self.refresh(keep_selection=True)
        if voltage is not None:
            state = "0 V (unbiased)" if to_zero else "cross" if crossed else "bar"
            self.update_hardware_status(f"{name} {state}: {voltage:.2f} V")

    def set_switch_state(self, name, state):
        """Set one MZI directly to cross, bar, or its unbiased 0 V state."""
        if hasattr(self, "clear_permutation_preview"):
            self.clear_permutation_preview()
        try:
            if state == "zero":
                crossed, voltage = True, self.controller.drive_voltage(name, 0.0)
            else:
                crossed = state == "cross"
                voltage = self.controller.apply_calibrated_state(name, crossed)
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Calibration command failed", str(error), parent=self)
            return
        self.network.states[name] = crossed
        if state == "zero":
            self.zero_volt_switches.add(name)
        else:
            self.zero_volt_switches.discard(name)
        if voltage is not None:
            self.voltage_vars[name].set(f"{voltage:.2f}")
        self.refresh(keep_selection=True)
        if voltage is not None:
            self.update_hardware_status(f"{name} {'0 V (unbiased)' if state == 'zero' else state}: {voltage:.2f} V")

    def reset(self):
        self.network.states = dict.fromkeys(self.network.switches, True)
        self.apply_calibration()
        self.refresh(keep_selection=True)

    def randomize(self):
        for name in self.network.states:
            self.network.states[name] = bool(random.getrandbits(1))
        self.apply_calibration()
        self.refresh(keep_selection=True)

    def select_route(self, event=None):
        selection = self.route_list.curselection()
        if selection:
            self.selected_route = selection[0]
            self.draw()

    def apply_route(self):
        if not self.routes:
            return
        self.clear_permutation_preview()
        route_states = dict(self.routes[self.selected_route].states)
        if self.keep_permutation_var.get():
            target_states = self.network.states_for_permutation(
                self.network.mapping(), route_states, prefer_states=self.network.states,
            )
            if target_states is None:
                messagebox.showerror(
                    "Equivalent path unavailable",
                    "This alternative path cannot be used while retaining the current complete output permutation.",
                    parent=self,
                )
                return
            names = [name for name in self.network.switches
                     if target_states[name] != self.network.states[name] or name in self.zero_volt_switches]
            zero_others = ()
        else:
            target_states = {**dict.fromkeys(self.network.switches, True), **route_states}
            names = list(route_states)
            zero_others = [name for name in self.network.switches if name not in route_states]
        if self.drive_route_states(target_states, names, zero_others):
            self.refresh(keep_selection=True)

    def drive_route_states(self, states, names, zero_others=()):
        """Drive the selected route and clear unused heaters to 0 V."""
        try:
            for name in zero_others:
                self.controller.drive_voltage(name, 0.0)
                self.network.states[name] = True
                self.zero_volt_switches.add(name)
                self.voltage_vars[name].set("0.00")
            for name in names:
                voltage = self.controller.apply_calibrated_state(name, states[name])
                if voltage is None:
                    if not states[name]:
                        raise ValueError(f"{name}: calibrate its bar voltage before applying this path")
                    voltage = self.controller.drive_voltage(name, 0.0)
                    self.zero_volt_switches.add(name)
                else:
                    self.zero_volt_switches.discard(name)
                self.network.states[name] = states[name]
                if voltage is not None:
                    self.voltage_vars[name].set(f"{voltage:.2f}")
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Path command failed", str(error), parent=self)
            return False
        mode = ("while retaining the complete permutation" if self.keep_permutation_var.get()
                else "on the selected path, with other MZIs at 0 V")
        self.update_hardware_status(f"Applied {len(names)} MZI state(s) {mode}")
        return True

    def clear_permutation_preview(self):
        self.permutation_preview_states = None
        if hasattr(self, "permutation_route_list"):
            self.permutation_route_list.selection_clear(0, "end")

    def set_permutation_route_options(self, permutation):
        """List every whole-network routing that produces one output permutation."""
        states = self.network.equivalent_permutation_states(permutation)
        current = self.network.states
        self.permutation_route_states = tuple(sorted(
            states,
            key=lambda candidate: sum(candidate[name] != current[name] for name in self.network.switches),
        ))
        self.permutation_route_list.configure(state="normal")
        self.permutation_route_list.delete(0, "end")
        for index, candidate in enumerate(self.permutation_route_states):
            changes = sum(candidate[name] != current[name] for name in self.network.switches)
            suffix = "current states" if changes == 0 else f"{changes} MZI state changes"
            self.permutation_route_list.insert("end", f"Routing {index + 1} — {suffix}")
        self.selected_permutation_route = 0
        self.permutation_route_list.selection_set(0)
        self.permutation_route_list.see(0)
        self.permutation_preview_states = self.permutation_route_states[0]

    def show_permutation_route(self, event=None):
        selection = self.permutation_route_list.curselection()
        if not selection or selection[0] >= len(self.permutation_route_states):
            return
        self.selected_permutation_route = selection[0]
        self.permutation_preview_states = self.permutation_route_states[self.selected_permutation_route]
        self.draw()

    def route_permutation(self):
        try:
            values = [int(x) - 1 for x in self.permutation.get().replace(",", " ").split()]
            if not self.network.solve(values):
                raise ValueError("No switch configuration found.")
        except ValueError as error:
            messagebox.showerror("Invalid permutation", str(error), parent=self)
            return
        self.apply_calibration()
        self.set_permutation_route_options(values)
        self.refresh(keep_selection=True)

    def shuffle(self):
        values = random.sample(range(1, 7), 6)
        self.permutation.delete(0, "end")
        self.permutation.insert(0, " ".join(map(str, values)))
        self.route_permutation()

    def apply_voltage(self, name):
        try:
            voltage = self.controller.set_voltage(name, self.voltage_vars[name].get())
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Voltage not applied", str(error), parent=self)
            return
        self.zero_volt_switches.discard(name)
        self.draw()
        self.voltage_vars[name].set(f"{voltage:.2f}")
        self.update_hardware_status(f"{name} set to {voltage:.2f} V")

    def refresh(self, keep_selection=False):
        if not keep_selection:
            self.selected_route = 0
        self.routes = ([] if self.source is None or self.destination is None
                       else self.network.routes(self.source, self.destination))
        self.route_list.delete(0, "end")
        for index, route in enumerate(self.routes):
            states = "  ".join(f"{name}={'X' if crossed else '='}" for name, crossed in route.states)
            self.route_list.insert("end", f"Path {index + 1}   {states}")
        if self.routes:
            self.route_list.selection_set(self.selected_route)
            self.route_list.see(self.selected_route)
        if self.source is None or self.destination is None:
            route_text = "No path selected. Choose an input and an output to explore paths."
        else:
            route_text = (f"{len(self.routes)} possible paths · = straight, X cross, 0V unbiased "
                          "(routes as cross; click a switch to cycle) · switches omitted from a path are unconstrained")
        self.route_label.configure(text=route_text)
        mapping = self.network.mapping()
        self.mapping_label.configure(text="CURRENT CONNECTIONS    " + "     ".join(f"{i + 1} → {out + 1}" for i, out in enumerate(mapping)))
        self.update_wdm_routing()
        if self.source is None or self.destination is None:
            self.status.configure(text="No path selected. Click an input and output, or choose None to clear either selection.")
        else:
            current = mapping[self.source] + 1
            self.status.configure(text=f"Input {self.source + 1} currently reaches output {current}   •   Exploring output {self.destination + 1}")
        self.update_hardware_status()
        self.draw()

    def draw(self):
        c = self.canvas
        for entry in self.switch_voltage_entries.values():
            entry.destroy()
        self.switch_voltage_entries = {}
        c.delete("all")
        width, height = c.winfo_width(), c.winfo_height()
        # Tk initially reports a 1x1 canvas before its first layout pass.
        if width < 420 or height < 220:
            return
        display_states = self.permutation_preview_states or self.network.states
        previewing_permutation = self.permutation_preview_states is not None
        scale = min(1.0, width / 1180, height / 355)
        font_small = ("Segoe UI", max(7, round(8 * scale)))
        font_normal = ("Segoe UI", max(8, round(9 * scale)))
        font_heading = ("Segoe UI", max(9, round(10 * scale)), "bold")
        self.left_x = max(58, round(78 * scale))
        self.right_x = width - self.left_x
        first_stage = self.left_x + max(65, round(75 * scale))
        last_stage = self.right_x - max(88, round(125 * scale))
        self.xs = [first_stage + i * (last_stage - first_stage) / 4 for i in range(5)]
        top, bottom = max(48, round(58 * scale)), max(47, round(55 * scale))
        self.ys = [top + i * (height - top - bottom) / 5 for i in range(6)]
        # Keep each MZI visually quiet: one compact label and one voltage field.
        half = max(15, round(18 * scale))
        self.switch_half = half
        row_gap = min(self.ys[index + 1] - self.ys[index] for index in range(5))
        show_voltage_entry = row_gap >= 42
        card_pad = max(6, round(7 * scale))
        # Group the three middle stages into two independent 3x3 networks.
        for first, last, label in ((0, 2, "Upper 3×3"), (3, 5, "Lower 3×3")):
            c.create_rectangle(self.xs[1] - 65, self.ys[first] - 38,
                               self.xs[3] + 65, self.ys[last] + 26,
                               fill="#131e30", outline="#33465f", dash=(5, 4))
            c.create_text(self.xs[2], self.ys[last] + 17, text=label,
                          fill="#9baec6", font=font_normal)
        for stage, pairs in enumerate(self.network.pairs):
            x = self.xs[stage]
            c.create_text(x, max(14, round(16 * scale)), text=f"STAGE {stage + 1}", fill="#9baec6", font=font_heading)
            for row, y in enumerate(self.ys):
                if stage == 0:
                    c.create_line(self.left_x + 18, y, x - half, y, fill=WIRE, width=2)
                next_x = self.xs[stage + 1] - half if stage < 4 else self.right_x - 18
                c.create_line(x + half, y, next_x, self.ys[self.network.links[stage][row]], fill=WIRE, width=2)
                if (stage, row) not in self.network.at:
                    c.create_line(x - half, y, x + half, y, fill=WIRE, width=2)
                    c.create_text(x, y + round(13 * scale), text="bypass", fill="#8393a9", font=font_small)
            for index, (a, b) in enumerate(pairs):
                name = f"S{stage + 1}.{index + 1}"
                crossed = display_states[name]
                zero = name in self.zero_volt_switches and not previewing_permutation
                calibration = self.controller.settings["calibration"][name]
                calibrated = calibration["straight"] is not None and calibration["cross"] is not None
                tag = f"switch:{name}"
                outline = ZERO_VOLT_COLOR if zero else ACCENT if calibrated else "#7790ad"
                c.create_rectangle(x - half, self.ys[a] - card_pad, x + half, self.ys[b] + card_pad,
                                   fill="#2b2a1c" if zero else PANEL, outline=outline,
                                   width=2.5 if zero or calibrated else 1.5, tags=tag)
                for start, end in ((a, b), (b, a)) if crossed else ((a, a), (b, b)):
                    c.create_line(x - half, self.ys[start], x + half, self.ys[end],
                                  fill=ZERO_VOLT_COLOR if zero else "#a4bad4", width=2,
                                  dash=(4, 3) if zero else (), tags=tag)
                pin = self.controller.settings["mapping"][name]
                c.create_text(x, self.ys[a] - max(13, round(15 * scale)),
                              text=f"{name}   PIN {pin if pin is not None else '-'}",
                              fill=ZERO_VOLT_COLOR if zero else ACCENT if calibrated else TEXT,
                              font=font_normal, tags=tag)
                button_size = max(10, round(11 * scale))
                button_gap = max(2, round(3 * scale))
                button_x = x + half + max(4, round(5 * scale))
                button_y = (self.ys[a] + self.ys[b]) / 2
                for button_index, (label, state, color) in enumerate(
                        (("X", "cross", ACCENT), ("=", "bar", "#ffbe69"), ("0", "zero", ZERO_VOLT_COLOR))):
                    left = button_x + button_index * (button_size + button_gap)
                    button_tag = f"set:{name}:{state}"
                    c.create_rectangle(left, button_y - button_size / 2,
                                       left + button_size, button_y + button_size / 2,
                                       fill="#263650", outline=color, width=1.25, tags=button_tag)
                    c.create_text(left + button_size / 2, button_y, text=label, fill=color,
                                  font=("Segoe UI", max(7, round(8 * scale)), "bold"), tags=button_tag)
                    c.tag_bind(button_tag, "<Button-1>",
                               lambda event, n=name, s=state: self.set_switch_state(n, s))
                    c.tag_bind(button_tag, "<Enter>", lambda event: c.configure(cursor="hand2"))
                    c.tag_bind(button_tag, "<Leave>", lambda event: c.configure(cursor=""))
                if show_voltage_entry:
                    entry = ttk.Entry(c, textvariable=self.voltage_vars[name], width=4, justify="center")
                    entry.bind("<Return>", lambda event, n=name: self.apply_voltage(n))
                    c.create_window(x, self.ys[b] + max(19, round(20 * scale)), window=entry)
                    self.switch_voltage_entries[name] = entry
                c.tag_bind(tag, "<Button-1>", lambda event, n=name: self.toggle(n))
                c.tag_bind(tag, "<Enter>", lambda event: c.configure(cursor="hand2"))
                c.tag_bind(tag, "<Leave>", lambda event: c.configure(cursor=""))

        current = None if self.source is None else self.network.trace(self.source, display_states)
        wdm = self.wdm_routes()
        wdm_ends = {}
        for row, wavelength, color in wdm:
            wdm_ends[row, "in"] = wdm_ends[self.network.trace(row, display_states).rows[-1], "out"] = (wavelength, color)
        # With an input selected, its channel is the main one: the other comb lines fade
        # back and only the main channel's crosstalk is drawn.
        main = self.source if not previewing_permutation else None
        main_color = next((color for row, _, color in wdm if row == main), ACCENT)
        if main is not None:
            wdm = [(row, wavelength, color if row == main else blend(color, BG, 0.6)) for row, wavelength, color in wdm]
        if not previewing_permutation:
            for row, _, color in wdm:
                if main is not None and row != main:
                    continue
                for points in self.crosstalk_points(self.network.trace(row), self.right_x - 18,
                                                    self.xs, self.ys, self.switch_half):
                    c.create_line(*points, fill=color, width=1.5, dash=(3, 4),
                                  joinstyle="round", capstyle="round", state="disabled")
        if previewing_permutation:
            for row in range(self.network.size):
                self.draw_route(self.network.trace(row, display_states), WDM_COLORS[row], 3)
            c.create_text(width / 2, height - 12,
                          text=(f"Equivalent permutation routing {self.selected_permutation_route + 1} / "
                                f"{len(self.permutation_route_states)} · preview only"),
                          fill=ACCENT, font=font_normal)
        elif current is not None and not self.routes and (self.source, "in") not in wdm_ends:
            for points in self.crosstalk_points(current, self.right_x - 18,
                                                self.xs, self.ys, self.switch_half):
                c.create_line(*points, fill=ACCENT, width=1.5, dash=(3, 4),
                              joinstyle="round", capstyle="round", state="disabled")
        if self.routes and not previewing_permutation:
            for row, _, color in wdm:
                if row != main:
                    self.draw_route(self.network.trace(row), color, 2)
            if self.all_paths.get():
                for index, route in enumerate(self.routes):
                    self.draw_route(route, COLORS[index % len(COLORS)], 2, dashed=True)
            self.draw_main_route(current, main_color)
            self.draw_route(self.routes[self.selected_route], ACCENT, 4)
            c.create_text(width / 2, height - 12, text=f"Bold: input {main + 1} as routed now · path {self.selected_route + 1} preview in green · broad dashes show alternatives · thin dashes show crosstalk", fill=ACCENT, font=("Segoe UI", 10))
        elif not previewing_permutation and current is not None:
            for row, _, color in wdm:
                if row != main:
                    self.draw_route(self.network.trace(row), color, 2)
            self.draw_main_route(current, main_color)
            c.create_text(width / 2, height - 12, text=f"Bold: input {main + 1} as routed now · faded: other comb lines · thin dashes: its possible crosstalk",
                          fill="#9baec6", font=font_normal)
        elif not previewing_permutation:
            for row, _, color in wdm:
                self.draw_route(self.network.trace(row), color, 3)
        for row, y in enumerate(self.ys):
            for side, x, selected, callback in (
                ("in", self.left_x, row == self.source, self.click_input),
                ("out", self.right_x, row == self.destination, self.click_output),
            ):
                tag = f"{side}:{row}"
                wavelength, wdm_color = wdm_ends.get((row, side), (None, None))
                outline = ACCENT if selected else wdm_color or WIRE
                if side == "in" and self.input_laser_on(row):
                    c.create_oval(x - 24, y - 24, x + 24, y + 24, outline=LASER_ON_COLOR, width=3, tags=tag)
                    c.create_text(x, y - 31, text="LASER ON", fill=LASER_ON_COLOR,
                                  font=("Segoe UI", max(7, round(7 * scale)), "bold"), tags=tag)
                c.create_oval(x - 18, y - 18, x + 18, y + 18, fill=ACCENT if selected else PANEL, outline=outline,
                              width=3 if wdm_color and not selected else 2, tags=tag)
                c.create_text(x, y, text=str(row + 1), fill=BG if selected else TEXT, font=("Segoe UI", max(10, round(12 * scale)), "bold"), tags=tag)
                label_x, anchor = (x - 25, "e") if side == "in" else (x + 25, "w")
                label = f"In {row + 1}\nFiber {15 - row}" if side == "in" else f"Out {row + 1}\nFiber {4 + row}"
                label_y = y if wavelength is None else y - round(6 * scale)
                c.create_text(label_x, label_y, text=label, anchor=anchor, fill="#9baec6", font=font_small)
                if wavelength is not None:
                    c.create_text(label_x, y + round(18 * scale), text=f"{wavelength:g} nm", anchor=anchor,
                                  fill=wdm_color, font=font_small)
                if side == "out":
                    detector_colors = []
                    if row == self.detector_port(self.power_meter_output_var):
                        detector_colors.append(POWER_METER_COLOR)
                    if row == self.detector_port(self.osa_output_var):
                        detector_colors.append(OSA_COLOR)
                    for index, color in enumerate(detector_colors):
                        radius = 23 + index * 5
                        c.create_oval(x - radius, y - radius, x + radius, y + radius,
                                      outline=color, width=3, tags=tag)
                c.tag_bind(tag, "<Button-1>", lambda event, r=row, fn=callback: fn(r))
        c.create_text(self.left_x, max(14, round(16 * scale)), text="INPUT", fill=TEXT, font=font_heading)
        c.create_text(self.right_x, max(14, round(16 * scale)), text="OUTPUT", fill=TEXT, font=font_heading)

    def route_points(self, route, start_x, end_x, xs, ys, half):
        points = [start_x, ys[route.rows[0]]]
        states = dict(route.states)
        for stage, row in enumerate(route.rows[:-1]):
            x = xs[stage]
            points.extend((x - half, ys[row]))
            name = self.network.at.get((stage, row))
            exit_row = self.network.switch_output(stage, row, states[name] if name else False)
            points.extend((x + half, ys[exit_row]))
        points.extend((end_x, ys[route.rows[-1]]))
        return points

    def crosstalk_points(self, route, end_x, xs, ys, half):
        """Possible leakage from each used switch's other port, then along current states."""
        states = self.network.states
        for stage, row in enumerate(route.rows[:-1]):
            name = self.network.at.get((stage, row))
            if name is None:
                continue
            a, b = self.network.switches[name][1]
            main_exit = self.network.switch_output(stage, row, states[name])
            other_exit = b if main_exit == a else a
            points = [xs[stage] - half, ys[row], xs[stage] + half, ys[other_exit]]
            next_row = self.network.links[stage][other_exit]
            for next_stage in range(stage + 1, len(self.network.pairs)):
                x = xs[next_stage]
                points.extend((x - half, ys[next_row]))
                next_name = self.network.at.get((next_stage, next_row))
                exit_row = self.network.switch_output(
                    next_stage, next_row, states[next_name] if next_name else False)
                points.extend((x + half, ys[exit_row]))
                next_row = self.network.links[next_stage][exit_row]
            points.extend((end_x, ys[next_row]))
            yield points

    def draw_main_route(self, route, color):
        """The selected input's light: thick, in its comb-line colour, over a dark gap that sets it apart."""
        self.draw_route(route, BG, 12)
        self.draw_route(route, color, 6)

    def draw_route(self, route, color, line_width, dashed=False):
        points = self.route_points(route, self.left_x + 18, self.right_x - 18, self.xs, self.ys, self.switch_half)
        self.canvas.create_line(*points, fill=color, width=line_width, dash=(5, 5) if dashed else (), joinstyle="round", capstyle="round", state="disabled")
