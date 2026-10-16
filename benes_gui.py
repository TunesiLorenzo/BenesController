"""Run with: python benes_gui.py"""

import random
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from network import BenesNetwork
from calibration_scan import scan_switch, scan_switches, scan_switches_with_reader, scan_voltages, spectrum_scan_point
from crosstalk_results import create_result_folder, save_scan_artifacts, scan_context
import osa
from power_meter import ArduinoPowerMeter
from spectrum_export import write_crosstalk_csv, write_spectra
from voltage_controller import BenesVoltageController


BG = "#101827"
PANEL = "#192437"
WIRE = "#52647c"
TEXT = "#e6edf7"
ACCENT = "#53dfbb"
COLORS = ("#53dfbb", "#ffbe69", "#78b9ff", "#ed91cf", "#b6a0ff", "#fff080")
POWER_METER_COLOR = "#ffbe69"
OSA_COLOR = "#78b9ff"


class BenesApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Beneš • 6 × 6 route explorer")
        self.geometry("1320x960")
        self.minsize(1120, 840)
        self.configure(bg=BG)
        self.network = BenesNetwork()
        self.controller = BenesVoltageController(tuple(self.network.switches))
        self.power_meter = ArduinoPowerMeter()
        self.scan_settings = {}
        self.crosstalk_running = False
        self.crosstalk_data = {}
        self.crosstalk_data_source = None
        self.crosstalk_data_unit = None
        self.crosstalk_result_folder = None
        self.source = 0
        self.destination = None
        self.routes = []
        self.selected_route = 0
        self.all_paths = tk.BooleanVar(value=True)
        self.power_meter_output_var = tk.StringVar(value="None")
        self.osa_output_var = tk.StringVar(value="None")
        self.port_var = tk.StringVar(value=self.controller.settings["port"])
        self.baud_var = tk.StringVar(value=str(self.controller.settings["baud"]))
        self.dry_run_var = tk.BooleanVar(value=self.controller.settings["dry_run"])
        self.voltage_vars = {
            name: tk.StringVar(value=f"{value:.2f}")
            for name, value in self.controller.settings["voltage"].items()
        }
        self.switch_voltage_entries = {}
        self.osa_host_var = tk.StringVar(value="192.168.0.200")
        self.osa_port_var = tk.StringVar(value="8002")
        self.osa_dry_run_var = tk.BooleanVar(value=False)
        self.osa_plot_mode = tk.StringVar(value="Overlay")
        self.osa_axes = None
        self.trace_count = 0
        self.arduino_port_var = tk.StringVar(value=self.power_meter.settings["port"])
        self.arduino_baud_var = tk.StringVar(value=str(self.power_meter.settings["baud"]))
        self.arduino_duration_var = tk.StringVar(value=str(self.power_meter.settings["duration"]))
        self.arduino_scale_var = tk.StringVar(value=str(self.power_meter.settings["scale"]))
        self.arduino_offset_var = tk.StringVar(value=str(self.power_meter.settings["offset"]))
        self.arduino_unit_var = tk.StringVar(value=self.power_meter.settings["unit"])
        self.power_reading_var = tk.StringVar(value="No Arduino reading yet")
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10), padding=7)
        style.configure("TCheckbutton", background=BG, foreground=TEXT)
        style.configure("TEntry", fieldbackground=PANEL, foreground=TEXT, insertcolor=TEXT, bordercolor="#7790ad")
        style.configure("TCombobox", fieldbackground=PANEL, foreground=TEXT, arrowcolor=TEXT, bordercolor="#7790ad")
        style.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", TEXT)])
        style.configure("TLabelframe", background=BG, foreground=TEXT)
        style.configure("TLabelframe.Label", background=BG, foreground=TEXT, font=("Segoe UI", 10, "bold"))

        header = ttk.Frame(self, padding=(24, 18, 24, 8))
        header.pack(fill="x")
        ttk.Label(header, text="Beneš route explorer", font=("Segoe UI", 23, "bold")).pack(anchor="w")
        ttk.Label(header, text="6 inputs · 6 outputs · 5 stages · 12 switches · generalized topology with bypass wires").pack(anchor="w", pady=(5, 0))

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.route_tab = ttk.Frame(self.notebook)
        self.hardware_tab = ttk.Frame(self.notebook)
        self.crosstalk_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.route_tab, text="Route explorer")
        self.notebook.add(self.hardware_tab, text="Boards & measurement")
        self.notebook.add(self.crosstalk_tab, text="Crosstalk")

        controls = ttk.Frame(self.route_tab, padding=(24, 8))
        controls.pack(fill="x")
        ttk.Label(controls, text="Input").pack(side="left")
        self.input_box = ttk.Combobox(controls, values=list(range(1, 7)), width=5, state="readonly")
        self.input_box.current(0)
        self.input_box.pack(side="left", padx=(8, 20))
        self.input_box.bind("<<ComboboxSelected>>", self.choose_input)
        ttk.Label(controls, text="Destination").pack(side="left")
        self.output_box = ttk.Combobox(controls, values=["Trace current"] + list(range(1, 7)), width=14, state="readonly")
        self.output_box.current(0)
        self.output_box.pack(side="left", padx=8)
        self.output_box.bind("<<ComboboxSelected>>", self.choose_output)
        ttk.Checkbutton(controls, text="Overlay all possible paths", variable=self.all_paths, command=self.draw).pack(side="left", padx=15)
        ttk.Button(controls, text="Reset switches", command=self.reset).pack(side="right")
        ttk.Button(controls, text="Random switches", command=self.randomize).pack(side="right", padx=8)

        detector_controls = ttk.Frame(self.route_tab, padding=(24, 0, 24, 8))
        detector_controls.pack(fill="x")
        ttk.Label(detector_controls, text="Power Meter output", foreground=POWER_METER_COLOR).pack(side="left")
        ttk.Combobox(detector_controls, textvariable=self.power_meter_output_var,
                     values=("None", "1", "2", "3", "4", "5", "6"), state="readonly", width=6).pack(side="left", padx=(7, 24))
        ttk.Label(detector_controls, text="OSA output", foreground=OSA_COLOR).pack(side="left")
        ttk.Combobox(detector_controls, textvariable=self.osa_output_var,
                     values=("None", "1", "2", "3", "4", "5", "6"), state="readonly", width=6).pack(side="left", padx=(7, 18))
        ttk.Label(detector_controls, text="Colored rings mark detector ports; the selected route output remains green.").pack(side="left")
        self.power_meter_output_var.trace_add("write", lambda *_: self.draw())
        self.osa_output_var.trace_add("write", lambda *_: self.draw())

        hardware = ttk.LabelFrame(self.hardware_tab, text="Voltage controller", padding=(18, 12))
        hardware.pack(fill="x", padx=24, pady=(16, 8))
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

        osa_controls = ttk.LabelFrame(self.hardware_tab, text="OSA spectrum analyser", padding=(18, 12))
        osa_controls.pack(fill="x", padx=24, pady=(0, 8))
        ttk.Label(osa_controls, text="OSA", font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Label(osa_controls, text="Host").pack(side="left", padx=(28, 4))
        ttk.Entry(osa_controls, textvariable=self.osa_host_var, width=16).pack(side="left")
        ttk.Label(osa_controls, text="Port").pack(side="left", padx=(10, 4))
        ttk.Entry(osa_controls, textvariable=self.osa_port_var, width=6).pack(side="left")
        ttk.Combobox(osa_controls, textvariable=self.osa_plot_mode, values=("Overlay", "New window"), state="readonly", width=11).pack(side="left", padx=10)
        ttk.Checkbutton(osa_controls, text="Simulate", variable=self.osa_dry_run_var).pack(side="left")
        self.osa_button = ttk.Button(osa_controls, text="Read trace", command=self.read_spectrum)
        self.osa_button.pack(side="left", padx=8)

        arduino = ttk.LabelFrame(self.hardware_tab, text="Arduino power meter", padding=(18, 12))
        arduino.pack(fill="x", padx=24, pady=(0, 8))
        ttk.Label(arduino, text="Port").grid(row=0, column=0, sticky="w")
        ttk.Entry(arduino, textvariable=self.arduino_port_var, width=10).grid(row=0, column=1, sticky="w", padx=(4, 12))
        ttk.Label(arduino, text="Baud").grid(row=0, column=2, sticky="w")
        ttk.Entry(arduino, textvariable=self.arduino_baud_var, width=9).grid(row=0, column=3, sticky="w", padx=(4, 12))
        ttk.Label(arduino, text="Burst [s]").grid(row=0, column=4, sticky="w")
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
        ttk.Label(arduino, textvariable=self.power_reading_var, font=("Segoe UI", 10, "bold")).grid(row=2, column=0, columnspan=8, sticky="w", pady=(12, 0))

        scan_controls = ttk.LabelFrame(self.crosstalk_tab, text="Sequential switch scan", padding=18)
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
        for row, (label, default) in enumerate((("Lower limit [V]", "-10"), ("Upper limit [V]", "10"),
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
        self.crosstalk_plot_frame = ttk.Frame(self.crosstalk_tab, padding=(0, 18, 18, 18))
        self.crosstalk_plot_frame.pack(side="left", fill="both", expand=True)
        ttk.Label(self.crosstalk_plot_frame, text="Power versus voltage will appear here after a scan.").pack(anchor="center", expand=True)

        self.status = ttk.Label(self.route_tab, font=("Segoe UI", 12, "bold"), padding=(24, 8))
        self.status.pack(fill="x")
        self.canvas = tk.Canvas(self.route_tab, bg=BG, highlightthickness=0, height=355)
        self.canvas.pack(fill="both", expand=True, padx=12)
        self.canvas.bind("<Configure>", lambda event: self.draw())

        ttk.Label(self.route_tab, text="Green switches have both bar and cross voltages saved. Click a switch to toggle it. Type a voltage and press Enter to send it.", padding=(24, 6)).pack(anchor="w")
        bottom = ttk.Frame(self.route_tab, padding=(24, 4, 24, 12))
        bottom.pack(fill="x")
        left = ttk.Frame(bottom)
        left.pack(side="left", fill="both", expand=True)
        self.route_label = ttk.Label(left, text="")
        self.route_label.pack(anchor="w")
        self.route_list = tk.Listbox(left, height=4, bg=PANEL, fg=TEXT, selectbackground="#315b78", selectforeground="white", font=("Consolas", 10), exportselection=False, borderwidth=0, highlightthickness=0)
        self.route_list.pack(fill="x", pady=6)
        self.route_list.bind("<<ListboxSelect>>", self.select_route)
        ttk.Button(left, text="Apply selected path to switches", command=self.apply_route).pack(anchor="w")
        right = ttk.Frame(bottom, padding=(24, 0, 0, 0))
        right.pack(side="right", fill="y")
        ttk.Label(right, text="Route a complete permutation", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(right, text="Desired outputs for inputs 1, 2, 3, 4, 5, 6").pack(anchor="w", pady=4)
        self.permutation = ttk.Entry(right, width=32)
        self.permutation.insert(0, "1 2 3 4 5 6")
        self.permutation.pack(anchor="w", pady=4)
        buttons = ttk.Frame(right)
        buttons.pack(anchor="w")
        ttk.Button(buttons, text="Route permutation", command=self.route_permutation).pack(side="left")
        ttk.Button(buttons, text="Shuffle", command=self.shuffle).pack(side="left", padx=6)
        self.mapping_label = ttk.Label(self.route_tab, padding=(24, 6, 24, 14))
        self.mapping_label.pack(fill="x")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    def choose_input(self, event=None):
        self.source = self.input_box.current()
        self.refresh()

    def choose_output(self, event=None):
        self.destination = self.output_box.current() - 1
        if self.destination < 0:
            self.destination = None
        self.refresh()

    @staticmethod
    def detector_port(variable):
        value = variable.get()
        return None if value == "None" else int(value) - 1

    def click_input(self, row):
        self.input_box.current(row)
        self.choose_input()

    def click_output(self, row):
        self.output_box.current(row + 1)
        self.choose_output()

    def toggle(self, name):
        crossed = not self.network.states[name]
        try:
            voltage = self.controller.apply_calibrated_state(name, crossed)
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Calibration command failed", str(error), parent=self)
            return
        self.network.states[name] = crossed
        if voltage is not None:
            self.voltage_vars[name].set(f"{voltage:.2f}")
        self.refresh(keep_selection=True)
        if voltage is not None:
            self.update_hardware_status(f"{name} {'cross' if crossed else 'bar'}: {voltage:.2f} V")

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
        if self.routes:
            self.network.states.update(dict(self.routes[self.selected_route].states))
            self.apply_calibration()
            self.refresh(keep_selection=True)

    def route_permutation(self):
        try:
            values = [int(x) - 1 for x in self.permutation.get().replace(",", " ").split()]
            if not self.network.solve(values):
                raise ValueError("No switch configuration found.")
        except ValueError as error:
            messagebox.showerror("Invalid permutation", str(error), parent=self)
            return
        self.apply_calibration()
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
        self.voltage_vars[name].set(f"{voltage:.2f}")
        self.update_hardware_status(f"{name} set to {voltage:.2f} V")

    def connect_board(self):
        try:
            self.controller.configure_connection(self.port_var.get(), self.baud_var.get(), self.dry_run_var.get())
            self.controller.connect()
            self.controller.apply_calibrated_states(self.network.states)
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Board connection failed", str(error), parent=self)
            return
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
            )
            self.power_meter.connect()
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Arduino connection failed", str(error), parent=self)
            return
        self.power_reading_var.set(f"Arduino connected on {self.power_meter.settings['port']}")

    def disconnect_power_meter(self):
        self.power_meter.disconnect()
        self.power_reading_var.set("Arduino disconnected")

    def read_current_power(self):
        self.read_power_button.configure(state="disabled")

        def acquire():
            try:
                reading = self.power_meter.read_current_power()
            except Exception as error:
                self.after(0, lambda: self.power_read_failed(str(error)))
            else:
                self.after(0, lambda: self.show_current_power(reading))

        threading.Thread(target=acquire, daemon=True).start()

    def power_read_failed(self, error):
        self.read_power_button.configure(state="normal")
        messagebox.showerror("Arduino power read failed", error, parent=self)

    def show_current_power(self, reading):
        self.read_power_button.configure(state="normal")
        self.power_reading_var.set(
            f"Current power: {reading.value:.4g} {reading.unit} "
            f"(ADC mean {reading.raw_mean:.2f}, {reading.sample_count} samples)"
        )

    def start_crosstalk_scan(self):
        names = [self.crosstalk_switches.get(index) for index in self.crosstalk_switches.curselection()]
        source = self.crosstalk_source.get()
        try:
            if not names:
                raise ValueError("Select at least one switch")
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
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk  # noqa: F401
            from matplotlib.figure import Figure  # noqa: F401
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Crosstalk scan unavailable", str(error), parent=self)
            return

        response_unit = "dBm" if source == "OSA" else self.power_meter.settings["unit"]
        output_port = self.destination if self.destination is not None else self.network.trace(self.source).rows[-1]
        context = scan_context(
            self.network, self.source, output_port,
            self.detector_port(self.power_meter_output_var), self.detector_port(self.osa_output_var),
            self.controller, names, voltages, settle_ms, source, self.crosstalk_comment_var.get().strip(),
        )
        try:
            result_folder = create_result_folder(Path(__file__).resolve().parent, context)
            save_scan_artifacts(result_folder, context, {}, source, response_unit, self.network,
                                status="in_progress")
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Crosstalk results unavailable", str(error), parent=self)
            return

        self.crosstalk_running = True
        self.crosstalk_data = {}
        self.crosstalk_data_source = None
        self.crosstalk_data_unit = None
        self.crosstalk_result_folder = result_folder
        self.crosstalk_button.configure(state="disabled")
        self.crosstalk_export_button.configure(state="disabled")
        self.crosstalk_switches.configure(state="disabled")
        self.crosstalk_source_box.configure(state="disabled")
        self.notebook.tab(self.route_tab, state="disabled")
        self.notebook.tab(self.hardware_tab, state="disabled")
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

        def save_results():
            try:
                final_context = {**context, "scan_error": error} if error else context
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
        self.notebook.tab(self.route_tab, state="normal")
        self.notebook.tab(self.hardware_tab, state="normal")
        if data:
            self.crosstalk_export_button.configure(state="normal")
        if scan_error:
            self.crosstalk_status.set(f"Scan stopped: {scan_error}. Results: {folder}")
            messagebox.showerror("Crosstalk scan failed", scan_error, parent=self)
        elif save_error:
            self.crosstalk_status.set(f"Scan finished, but automatic export failed: {save_error}")
        else:
            self.crosstalk_status.set(f"Scan complete. Results: {folder}")
        if save_error:
            messagebox.showerror("Crosstalk export failed", f"{save_error}\nFolder: {folder}", parent=self)

    def prepare_crosstalk_plots(self, voltages):
        for child in self.crosstalk_plot_frame.winfo_children():
            child.destroy()
        self.crosstalk_plots = ttk.Notebook(self.crosstalk_plot_frame)
        self.crosstalk_plots.pack(fill="both", expand=True)
        self.crosstalk_plot_items = {}
        self.crosstalk_voltage_limits = (min(voltages), max(voltages))

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
            response.set(xlabel="Voltage [V]",
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
            canvas.get_tk_widget().pack(fill="both", expand=True)
            plot = {"canvas": canvas, "toolbar": toolbar, "response": response, "line": line,
                    "mean_line": mean_line, "means": [],
                    "spectra": spectra, "norm": norm, "cmap": cmap,
                    "voltages": [], "powers": []}
            self.crosstalk_plot_items[name] = plot
        plot["voltages"].append(point.voltage)
        plot["powers"].append(point.power)
        plot["line"].set_data(plot["voltages"], plot["powers"])
        if source == "OSA":
            plot["means"].append(point.mean_power_dbm)
            plot["mean_line"].set_data(plot["voltages"], plot["means"])
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

    def apply_calibration(self):
        try:
            self.controller.apply_calibrated_states(self.network.states)
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Calibration command failed", str(error), parent=self)
            return
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

    def show_mapping(self):
        dialog = tk.Toplevel(self)
        dialog.title("MZI pin mapping and calibration")
        dialog.configure(bg=BG)
        dialog.transient(self)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Assign each MZI to a unique VoltageTuner pin (0–15).", font=("Segoe UI", 11, "bold")).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(body, text="Save a pin mapping before scanning. Each scan restores the switch voltage; choose which power extreme means straight and cross.", wraplength=650).grid(row=1, column=0, columnspan=5, sticky="w", pady=(4, 14))
        for column, title in enumerate(("MZI", "Pin", "Straight [V]", "Cross [V]", "Power scan")):
            ttk.Label(body, text=title).grid(row=2, column=column, sticky="w", padx=(0, 14), pady=(0, 4))
        pin_vars, straight_vars, cross_vars = {}, {}, {}
        choices = ("Unassigned",) + tuple(str(pin) for pin in range(16))
        for row, name in enumerate(self.network.switches, start=3):
            pin = self.controller.settings["mapping"][name]
            calibration = self.controller.settings["calibration"][name]
            pin_vars[name] = tk.StringVar(value="Unassigned" if pin is None else str(pin))
            straight_vars[name] = tk.StringVar(value="" if calibration["straight"] is None else f"{calibration['straight']:.2f}")
            cross_vars[name] = tk.StringVar(value="" if calibration["cross"] is None else f"{calibration['cross']:.2f}")
            ttk.Label(body, text=name).grid(row=row, column=0, sticky="w", padx=(0, 14), pady=2)
            ttk.Combobox(body, textvariable=pin_vars[name], values=choices, state="readonly", width=12).grid(row=row, column=1, sticky="w", padx=(0, 14), pady=2)
            ttk.Entry(body, textvariable=straight_vars[name], width=12).grid(row=row, column=2, sticky="w", padx=(0, 14), pady=2)
            ttk.Entry(body, textvariable=cross_vars[name], width=12).grid(row=row, column=3, sticky="w", pady=2)
            ttk.Button(body, text="Scan...", command=lambda n=name: self.show_power_scan(dialog, n, pin_vars[n], straight_vars[n], cross_vars[n])).grid(row=row, column=4, sticky="w", padx=(14, 0), pady=2)

        def save_mapping():
            try:
                mapping = {name: None if pin_vars[name].get() == "Unassigned" else int(pin_vars[name].get()) for name in self.network.switches}
                self.controller.set_mapping(mapping)
                for name in self.network.switches:
                    self.controller.set_calibration(name, straight_vars[name].get(), cross_vars[name].get())
            except ValueError as error:
                messagebox.showerror("Invalid mapping or calibration", str(error), parent=dialog)
                return
            self.apply_calibration()
            dialog.destroy()
            self.draw()
            self.update_hardware_status("Mapping saved")
            self.refresh(keep_selection=True)

        buttons = ttk.Frame(body)
        buttons.grid(row=3 + len(self.network.switches), column=0, columnspan=5, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="Save", command=save_mapping).pack(side="right", padx=6)

    def show_power_scan(self, parent, name, pin_var, straight_var, cross_var):
        window = tk.Toplevel(parent)
        window.title(f"Power scan: {name}")
        window.transient(parent)
        window.grab_set()
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        fields = {}
        defaults = {"Lower limit [V]": "-10", "Upper limit [V]": "10",
                    "Step [V]": "0.5", "Settle [ms]": "50"}
        saved = self.scan_settings.get(name, defaults)
        for row, label in enumerate(defaults):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=3)
            value = tk.StringVar(value=saved[label])
            ttk.Entry(frame, textvariable=value, width=12).grid(row=row, column=1, sticky="w", padx=8, pady=3)
            fields[label] = value
        ttk.Label(frame, text=f"Each point uses the Arduino burst ({self.power_meter.settings['duration']:g} s).", wraplength=360).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 4))
        progress = tk.StringVar(value="Choose a range and start the scan.")
        ttk.Label(frame, textvariable=progress, wraplength=390).grid(row=5, column=0, columnspan=3, sticky="w", pady=6)
        extrema = tk.StringVar(value="")
        ttk.Label(frame, textvariable=extrema, wraplength=390).grid(row=6, column=0, columnspan=3, sticky="w", pady=6)
        straight_choice = tk.StringVar()
        cross_choice = tk.StringVar()
        for row, (label, variable) in enumerate((("Straight", straight_choice), ("Cross", cross_choice)), start=7):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Combobox(frame, textvariable=variable, values=("Maximum", "Minimum"), state="readonly", width=12).grid(row=row, column=1, sticky="w", padx=8, pady=3)
        result = {"maximum": None, "minimum": None}
        running = {"value": False}

        def close_scan():
            if running["value"]:
                return
            window.grab_release()
            window.destroy()
            parent.grab_set()

        window.protocol("WM_DELETE_WINDOW", close_scan)

        def save_scan():
            if result["maximum"] is None:
                messagebox.showerror("No scan", "Run a scan before saving calibration.", parent=window)
                return
            if {straight_choice.get(), cross_choice.get()} != {"Maximum", "Minimum"}:
                messagebox.showerror("Choose states", "Assign one extreme to straight and the other to cross.", parent=window)
                return
            options = {"Maximum": result["maximum"].voltage, "Minimum": result["minimum"].voltage}
            straight = options[straight_choice.get()]
            cross = options[cross_choice.get()]
            try:
                self.controller.set_calibration(name, straight, cross)
                self.controller.apply_calibrated_states(self.network.states)
            except (OSError, RuntimeError, ValueError) as error:
                messagebox.showerror("Calibration failed", str(error), parent=window)
                return
            straight_var.set(f"{straight:.2f}")
            cross_var.set(f"{cross:.2f}")
            self.show_calibrated_voltages()
            self.draw()
            self.update_hardware_status(f"{name} calibrated")
            close_scan()

        def start_scan():
            try:
                voltages = scan_voltages(*(float(fields[key].get()) for key in ("Lower limit [V]", "Upper limit [V]", "Step [V]")))
                settle_ms = float(fields["Settle [ms]"].get())
                if not 0 <= settle_ms <= 10000:
                    raise ValueError("Settling time must be between 0 and 10000 ms")
            except ValueError as error:
                messagebox.showerror("Invalid scan", str(error), parent=window)
                return
            if str(self.controller.settings["mapping"][name]) != pin_var.get():
                messagebox.showerror("Save mapping first", "Save this pin mapping before scanning its power response.", parent=window)
                return
            if not self.controller.connected or self.controller.settings["dry_run"] or not self.power_meter.connected:
                messagebox.showerror("Hardware not connected", "Connect the physical voltage board and Arduino power meter before scanning.", parent=window)
                return
            self.scan_settings[name] = {key: value.get() for key, value in fields.items()}
            running["value"] = True
            result.update(maximum=None, minimum=None)
            straight_choice.set("")
            cross_choice.set("")
            extrema.set("")
            scan_button.configure(state="disabled")
            save_button.configure(state="disabled")
            progress.set(f"Scanning {len(voltages)} voltages...")

            def on_point(index, total, point):
                self.after(0, lambda: progress.set(f"{index}/{total}: {point.voltage:.2f} V, {point.power:.4g} {self.power_meter.settings['unit']}"))

            def worker():
                try:
                    points = scan_switch(self.controller, self.power_meter, name, voltages, settle_ms / 1000, on_point)
                except Exception as error:
                    self.after(0, lambda message=str(error): finish_error(message))
                else:
                    self.after(0, lambda: finish_scan(points))

            threading.Thread(target=worker, daemon=True).start()

        def finish_error(message):
            running["value"] = False
            scan_button.configure(state="normal")
            progress.set(f"Scan failed: {message}")

        def finish_scan(points):
            running["value"] = False
            scan_button.configure(state="normal")
            save_button.configure(state="normal")
            result["maximum"] = max(points, key=lambda point: point.power)
            result["minimum"] = min(points, key=lambda point: point.power)
            unit = self.power_meter.settings["unit"]
            extrema.set(f"Maximum: {result['maximum'].power:.4g} {unit} at {result['maximum'].voltage:.2f} V\nMinimum: {result['minimum'].power:.4g} {unit} at {result['minimum'].voltage:.2f} V")
            progress.set(f"Scan complete: {len(points)} points. Choose the state for each extreme.")

        scan_button = ttk.Button(frame, text="Start scan", command=start_scan)
        scan_button.grid(row=9, column=0, sticky="w", pady=(12, 0))
        save_button = ttk.Button(frame, text="Save calibration", command=save_scan, state="disabled")
        save_button.grid(row=9, column=1, sticky="w", pady=(12, 0))
        ttk.Button(frame, text="Close", command=close_scan).grid(row=9, column=2, sticky="e", pady=(12, 0))

    def read_spectrum(self):
        try:
            port = int(self.osa_port_var.get())
            if not self.osa_host_var.get().strip() or not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid OSA connection", "Enter a host and a port from 1 to 65535.", parent=self)
            return
        self.osa_button.configure(state="disabled")

        def acquire():
            try:
                data = osa.simulated_spectrum() if self.osa_dry_run_var.get() else osa.get_spectrum(self.osa_host_var.get().strip(), port)
            except Exception as error:
                self.after(0, lambda: self.osa_failed(str(error)))
            else:
                self.after(0, lambda: self.show_spectrum(*data))

        threading.Thread(target=acquire, daemon=True).start()

    def osa_failed(self, error):
        self.osa_button.configure(state="normal")
        messagebox.showerror("OSA read failed", error, parent=self)

    def show_spectrum(self, wave, power):
        self.osa_button.configure(state="normal")
        try:
            wave, power = osa.trim_spurious_tail(wave, power)
        except ValueError as error:
            messagebox.showerror("OSA trace unavailable", str(error), parent=self)
            return
        self.trace_count += 1
        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
            from matplotlib.figure import Figure
        except ImportError:
            messagebox.showerror("Plot unavailable", "Install matplotlib to plot OSA traces:\npython -m pip install matplotlib", parent=self)
            return
        name = simpledialog.askstring("Trace name", "Name for this OSA trace:", initialvalue=f"Trace {self.trace_count}", parent=self)
        if name is None:
            return
        name = name.strip() or f"Trace {self.trace_count}"
        new_window = self.osa_plot_mode.get() == "New window" or self.osa_axes is None
        if not new_window:
            try:
                new_window = not self.osa_axes.figure.canvas.get_tk_widget().winfo_exists()
            except tk.TclError:
                new_window = True
        if new_window:
            window = tk.Toplevel(self)
            window.title("OSA Spectrum")
            figure = Figure(figsize=(8, 5))
            self.osa_axes = figure.add_subplot(xlabel="Wavelength [nm]", ylabel="Power [dBm]", title="OSA Spectrum")
            self.osa_axes.grid(True)
            canvas = FigureCanvasTkAgg(figure, master=window)
            NavigationToolbar2Tk(canvas, window).update()
            ttk.Button(window, text="Export displayed datasets", command=lambda axes=self.osa_axes: self.export_spectra(axes)).pack(anchor="e", padx=8, pady=4)
            canvas.get_tk_widget().pack(fill="both", expand=True)
        self.osa_axes.plot(wave, power, linewidth=1.2, label=name)
        self.osa_axes.legend(loc="best")
        self.osa_axes.figure.canvas.draw_idle()

    def export_spectra(self, axes):
        parent = axes.figure.canvas.get_tk_widget().winfo_toplevel()
        traces = [(line.get_label(), list(line.get_xdata()), list(line.get_ydata())) for line in axes.lines if line.get_visible()]
        if not traces:
            messagebox.showinfo("Export datasets", "There are no displayed traces to export.", parent=parent)
            return
        path = filedialog.asksaveasfilename(parent=parent, title="Export displayed datasets", defaultextension=".csv", filetypes=[("CSV data", "*.csv")], initialfile=f"osa_spectra_{datetime.now():%Y%m%d_%H%M%S}.csv")
        if not path:
            return
        description = simpledialog.askstring("Dataset description", "Short description (optional):", parent=parent)
        if description is None:
            return
        try:
            write_spectra(path, traces, description)
        except (OSError, ValueError) as error:
            messagebox.showerror("Export failed", str(error), parent=parent)
            return
        messagebox.showinfo("Export complete", f"Saved {len(traces)} trace(s) to:\n{path}", parent=parent)

    def close(self):
        if self.crosstalk_running:
            messagebox.showinfo("Scan in progress", "Wait for the crosstalk scan to finish and reset the switches to 0 V.", parent=self)
            return
        self.controller.disconnect()
        self.power_meter.disconnect()
        self.destroy()

    def refresh(self, keep_selection=False):
        if not keep_selection:
            self.selected_route = 0
        self.routes = [] if self.destination is None else self.network.routes(self.source, self.destination)
        self.route_list.delete(0, "end")
        for index, route in enumerate(self.routes):
            states = "  ".join(f"{name}={'X' if crossed else '='}" for name, crossed in route.states)
            self.route_list.insert("end", f"Path {index + 1}   {states}")
        if self.routes:
            self.route_list.selection_set(self.selected_route)
            self.route_list.see(self.selected_route)
        self.route_label.configure(text=f"{len(self.routes)} possible paths · = straight, X cross · switches omitted from a path are unconstrained" if self.routes else "Current route · select a destination to list every possible path and its required switch states")
        mapping = self.network.mapping()
        self.mapping_label.configure(text="CURRENT CONNECTIONS    " + "     ".join(f"{i + 1} → {out + 1}" for i, out in enumerate(mapping)))
        current = mapping[self.source] + 1
        self.status.configure(text=f"Input {self.source + 1} currently reaches output {current}" + (f"   •   Exploring output {self.destination + 1}" if self.destination is not None else ""))
        self.update_hardware_status()
        self.draw()

    def draw(self):
        c = self.canvas
        for entry in self.switch_voltage_entries.values():
            entry.destroy()
        self.switch_voltage_entries = {}
        c.delete("all")
        width, height = max(c.winfo_width(), 1080), max(c.winfo_height(), 300)
        self.ys = [58 + i * (height - 100) / 5 for i in range(6)]
        self.xs = [140 + i * (width - 330) / 4 for i in range(5)]
        self.left_x, self.right_x = 65, width - 65
        half = 24
        # Group the three middle stages into two independent 3x3 networks.
        for first, last, label in ((0, 2, "Upper 3×3"), (3, 5, "Lower 3×3")):
            c.create_rectangle(self.xs[1] - 65, self.ys[first] - 38,
                               self.xs[3] + 65, self.ys[last] + 26,
                               fill="#131e30", outline="#33465f", dash=(5, 4))
            c.create_text(self.xs[2], self.ys[last] + 17, text=label,
                          fill="#9baec6", font=("Segoe UI", 9))
        for stage, pairs in enumerate(self.network.pairs):
            x = self.xs[stage]
            c.create_text(x, 16, text=f"STAGE {stage + 1}", fill="#9baec6", font=("Segoe UI", 10, "bold"))
            for row, y in enumerate(self.ys):
                if stage == 0:
                    c.create_line(self.left_x + 18, y, x - half, y, fill=WIRE, width=2)
                next_x = self.xs[stage + 1] - half if stage < 4 else self.right_x - 18
                c.create_line(x + half, y, next_x, self.ys[self.network.links[stage][row]], fill=WIRE, width=2)
                if (stage, row) not in self.network.at:
                    c.create_line(x - half, y, x + half, y, fill=WIRE, width=2)
                    c.create_text(x, y + 13, text="bypass", fill="#8393a9", font=("Segoe UI", 8))
            for index, (a, b) in enumerate(pairs):
                name = f"S{stage + 1}.{index + 1}"
                crossed = self.network.states[name]
                calibration = self.controller.settings["calibration"][name]
                calibrated = calibration["straight"] is not None and calibration["cross"] is not None
                tag = f"switch:{name}"
                c.create_rectangle(x - half, self.ys[a] - 12, x + half, self.ys[b] + 12, fill=PANEL, outline=ACCENT if calibrated else "#7790ad", width=2.5 if calibrated else 1.5, tags=tag)
                for start, end in ((a, b), (b, a)) if crossed else ((a, a), (b, b)):
                    c.create_line(x - half, self.ys[start], x + half, self.ys[end], fill="#a4bad4", width=2, tags=tag)
                c.create_text(x, self.ys[a] - 23, text=f"{name} {'X' if crossed else '='}", fill=ACCENT if calibrated else TEXT, font=("Segoe UI", 9), tags=tag)
                pin = self.controller.settings["mapping"][name]
                c.create_text(x, self.ys[b] + 17, text=f"Pin {pin}" if pin is not None else "Unassigned", fill="#9baec6", font=("Segoe UI", 8))
                entry = ttk.Entry(c, textvariable=self.voltage_vars[name], width=6, justify="center")
                entry.bind("<Return>", lambda event, n=name: self.apply_voltage(n))
                c.create_window(x, self.ys[b] + 33, window=entry)
                self.switch_voltage_entries[name] = entry
                c.tag_bind(tag, "<Button-1>", lambda event, n=name: self.toggle(n))
                c.tag_bind(tag, "<Enter>", lambda event: c.configure(cursor="hand2"))
                c.tag_bind(tag, "<Leave>", lambda event: c.configure(cursor=""))

        current = self.network.trace(self.source)
        if self.routes:
            if self.all_paths.get():
                for index, route in enumerate(self.routes):
                    self.draw_route(route, COLORS[index % len(COLORS)], 2, dashed=True)
            self.draw_route(self.routes[self.selected_route], ACCENT, 4)
            c.create_text(width / 2, height - 12, text=f"Preview: path {self.selected_route + 1} in green · dashed lines show alternatives · Apply sets the required switches", fill=ACCENT, font=("Segoe UI", 10))
        else:
            self.draw_route(current, ACCENT, 4)
        for row, y in enumerate(self.ys):
            for side, x, selected, callback in (
                ("in", self.left_x, row == self.source, self.click_input),
                ("out", self.right_x, row == (self.destination if self.destination is not None else current.rows[-1]), self.click_output),
            ):
                tag = f"{side}:{row}"
                c.create_oval(x - 18, y - 18, x + 18, y + 18, fill=ACCENT if selected else PANEL, outline=ACCENT if selected else WIRE, width=2, tags=tag)
                c.create_text(x, y, text=str(row + 1), fill=BG if selected else TEXT, font=("Segoe UI", 12, "bold"), tags=tag)
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
        c.create_text(self.left_x, 16, text="INPUT", fill=TEXT, font=("Segoe UI", 10, "bold"))
        c.create_text(self.right_x, 16, text="OUTPUT", fill=TEXT, font=("Segoe UI", 10, "bold"))

    def draw_route(self, route, color, line_width, dashed=False):
        points = [self.left_x + 18, self.ys[route.rows[0]]]
        states = dict(route.states)
        for stage, row in enumerate(route.rows[:-1]):
            x = self.xs[stage]
            points.extend((x - 24, self.ys[row]))
            name = self.network.at.get((stage, row))
            exit_row = self.network.switch_output(stage, row, states[name] if name else False)
            points.extend((x + 24, self.ys[exit_row]))
        points.extend((self.right_x - 18, self.ys[route.rows[-1]]))
        self.canvas.create_line(*points, fill=color, width=line_width, dash=(5, 5) if dashed else (), joinstyle="round", capstyle="round", state="disabled")


if __name__ == "__main__":
    BenesApp().mainloop()
