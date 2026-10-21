"""Run with: python benes_gui.py"""

import csv
import json
import random
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from network import BenesNetwork
from calibration_scan import (ScanPoint, calibrate_default_cross_path, minimum_default_cross_pairs,
                              next_default_cross_pair,
                              scan_switch, scan_switches, scan_switches_with_reader,
                              scan_voltages, spectrum_scan_point, zero_voltage_paths)
from crosstalk_results import (PLOT_X_POWER, PLOT_X_VOLTAGE, RESCAN_STYLES, create_result_folder,
                               load_scan_folder, plot_x_values, save_scan_artifacts, scan_context)
from dataset_registry import (analyze_dataset, create_dataset, save_measurement,
                              write_coverage_report)
from impulse_test import (LIVE_PLOT_WINDOW_S, plot_trace_samples, received_pair_rate,
                          run_impulse_test,
                          validate_impulse_settings, write_impulse_csv)
import osa
from osa_output_switch import OSAOutputSwitch
from power_meter import ArduinoPowerMeter
from spectrum_export import write_crosstalk_csv, write_spectra
from voltage_controller import BenesVoltageController, app_dir
from heater_setup import BREAK_CURRENT_DENSITY, SAFE_CURRENT_DENSITY, load_heater_csv, voltage_limit
from scrollable import ScrollableFrame, fit_to_screen

# Smallest plot area before the tab scrolls instead of squeezing the figure further.
PLOT_MIN_SIZE = {"width": 480, "height": 320}


BG = "#101827"
PANEL = "#192437"
WIRE = "#52647c"
TEXT = "#e6edf7"
ACCENT = "#53dfbb"
COLORS = ("#53dfbb", "#ffbe69", "#78b9ff", "#ed91cf", "#b6a0ff", "#fff080")
POWER_METER_COLOR = "#ffbe69"
OSA_COLOR = "#78b9ff"
ZERO_VOLT_COLOR = "#ffd84d"


class VerticalScrolledFrame(ttk.Frame):
    """A compact scroll container for dialogs whose content exceeds the display."""

    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, borderwidth=0)
        vertical = ttk.Scrollbar(self, orient="vertical", style="Accent.Vertical.TScrollbar",
                                 command=self.canvas.yview)
        horizontal = ttk.Scrollbar(self, orient="horizontal", style="Accent.Horizontal.TScrollbar",
                                   command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        horizontal.pack(side="bottom", fill="x")
        vertical.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.content = ttk.Frame(self.canvas)
        self.content_id = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", self._update_region)
        self.canvas.bind("<MouseWheel>", self._scroll)

    def _update_region(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _scroll(self, event):
        self.canvas.yview_scroll(-1 * (event.delta // 120), "units")


class BenesApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Beneš • 6 × 6 route explorer")
        # Fit the initial window to the actual usable display.  Fixed 1180x880
        # geometry was taller than many laptop displays and hid lower controls.
        usable_width = max(760, self.winfo_screenwidth() - 80)
        usable_height = max(560, self.winfo_screenheight() - 120)
        self.geometry(f"{min(1180, usable_width)}x{min(880, usable_height)}")
        # Tabs scroll when the window is smaller than their content, so it may shrink further.
        self.minsize(480, 360)
        self.configure(bg=BG)
        self.network = BenesNetwork()
        self.controller = BenesVoltageController(tuple(self.network.switches))
        self._approved_unsafe_voltages = set()
        self._approved_over_safe = set()
        self.controller.unsafe_voltage_authorizer = self.confirm_unsafe_voltage
        self.controller.current_density_authorizer = self.confirm_over_safe_current
        self.power_meter = ArduinoPowerMeter()
        self.osa_output_switch = OSAOutputSwitch()
        self.scan_settings = {}
        self.crosstalk_running = False
        self.crosstalk_data = {}
        self.crosstalk_data_source = None
        self.crosstalk_data_unit = None
        self.crosstalk_result_folder = None
        self.active_scan_kind = "Crosstalk scan"
        self.next_connection_prompt = None
        self.path_calibrated_switches = set()
        # Switches clicked to 0 V; they route as cross, the chip's unbiased state.
        self.zero_volt_switches = set()
        self.impulse_running = False
        self.impulse_stop_event = None
        self.impulse_trace = None
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
        self.osa_trace_records = []
        self.osa_switch_port_var = tk.StringVar(value=self.osa_output_switch.settings["port"])
        self.osa_switch_baud_var = tk.StringVar(value=str(self.osa_output_switch.settings["baud"]))
        self.osa_switch_dry_run_var = tk.BooleanVar(value=self.osa_output_switch.settings["dry_run"])
        self.osa_switch_command_var = tk.StringVar(value=self.osa_output_switch.settings["command_template"])
        self.osa_switch_settle_var = tk.StringVar(value=str(self.osa_output_switch.settings["settle_s"]))
        selected_osa_output = self.osa_output_switch.settings["selected_output"]
        self.osa_switch_output_var = tk.StringVar(value="1" if selected_osa_output is None else str(selected_osa_output))
        self.osa_switch_status_var = tk.StringVar(value="OSA output switch not connected")
        self.arduino_port_var = tk.StringVar(value=self.power_meter.settings["port"])
        self.arduino_baud_var = tk.StringVar(value=str(self.power_meter.settings["baud"]))
        self.arduino_duration_var = tk.StringVar(value=str(self.power_meter.settings["duration"]))
        self.arduino_scale_var = tk.StringVar(value=str(self.power_meter.settings["scale"]))
        self.arduino_offset_var = tk.StringVar(value=str(self.power_meter.settings["offset"]))
        self.arduino_unit_var = tk.StringVar(value=self.power_meter.settings["unit"])
        self.arduino_stream_var = tk.StringVar(value=self.power_meter.settings["stream_format"])
        self.arduino_ceiling_var = tk.StringVar(value=f"{self.power_meter.settings['ceiling']:g}")
        self.power_reading_var = tk.StringVar(value="No Arduino reading yet")
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("TButton", background="#263650", foreground=TEXT, font=("Segoe UI", 10), padding=(10, 7))
        style.map("TButton", background=[("active", "#355171"), ("disabled", "#1b2638")],
                  foreground=[("disabled", "#71829a")])
        style.configure("Accent.TButton", background=ACCENT, foreground=BG,
                        font=("Segoe UI", 10, "bold"), padding=(10, 7))
        style.map("Accent.TButton", background=[("active", "#7bf0cf"), ("disabled", "#285749")],
                  foreground=[("disabled", "#9ab9af")])
        style.configure("TCheckbutton", background=BG, foreground=TEXT)
        style.configure("TEntry", fieldbackground=PANEL, foreground=TEXT, insertcolor=TEXT, bordercolor="#7790ad")
        style.configure("TCombobox", fieldbackground=PANEL, foreground=TEXT, arrowcolor=TEXT, bordercolor="#7790ad")
        style.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", TEXT)])
        style.configure("TLabelframe", background=BG, foreground=TEXT)
        style.configure("TLabelframe.Label", background=BG, foreground=TEXT, font=("Segoe UI", 10, "bold"))
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background="#172338", foreground="#9baec6", padding=(15, 8),
                        font=("Segoe UI", 10, "bold"))
        style.map("TNotebook.Tab", background=[("selected", PANEL), ("active", "#21324b")],
                  foreground=[("selected", ACCENT), ("active", TEXT)])
        # High-contrast scrollbars everywhere so it is obvious when a window has more content.
        for scrollbar_style in ("Accent.Vertical.TScrollbar", "Accent.Horizontal.TScrollbar",
                                "Vertical.TScrollbar", "Horizontal.TScrollbar"):
            style.configure(scrollbar_style, background=ACCENT, troughcolor="#172338",
                            bordercolor=BG, arrowcolor=BG, gripcount=0)
            style.map(scrollbar_style, background=[("active", "#7bf0cf"), ("pressed", "#3ac69f")])

        header = ttk.Frame(self, padding=(24, 14, 24, 6))
        header.pack(fill="x")
        ttk.Label(header, text="BENES LAB", foreground=ACCENT, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(header, text="6 × 6 photonic routing", font=("Segoe UI", 22, "bold")).pack(anchor="w")
        ttk.Label(header, text="Route · calibrate · measure", foreground="#9baec6").pack(anchor="w", pady=(2, 0))

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        # Each tab fills the window when it fits and scrolls when the window is smaller.
        tabs = []
        for text in ("Routing", "Hardware & OSA", "Calibration scans", "Dataset coverage", "Impulse test"):
            scroller = ScrollableFrame(self.notebook, background=BG)
            self.notebook.add(scroller, text=text)
            tabs.append(scroller.inner)
        self.route_tab, self.hardware_tab, self.crosstalk_tab, self.dataset_tab, self.impulse_tab = tabs

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

        hardware_tabs = ttk.Notebook(self.hardware_tab)
        hardware_tabs.pack(fill="both", expand=True, padx=18, pady=16)
        voltage_page = ttk.Frame(hardware_tabs)
        osa_page = ttk.Frame(hardware_tabs)
        meter_page = ttk.Frame(hardware_tabs)
        hardware_tabs.add(voltage_page, text="Voltage board")
        hardware_tabs.add(osa_page, text="OSA & output switch")
        hardware_tabs.add(meter_page, text="Arduino meter")

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
        ttk.Label(osa_router, text="Select an output to sample on the OSA.", foreground="#9baec6").grid(
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
        ttk.Label(arduino, text="Raw: A0 + A1 at 1000000 or 2000000 baud; dual: older framed sketch; legacy: A0 only").grid(
            row=2, column=4, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Label(arduino, textvariable=self.power_reading_var, font=("Segoe UI", 10, "bold")).grid(
            row=3, column=0, columnspan=8, sticky="w", pady=(12, 0))

        # Keep the two workflows in separate panels.  Appending automatic
        # calibration below the sequential form made its start button fall off
        # the bottom of a normal laptop window.
        scan_tabs = ttk.Notebook(self.crosstalk_tab)
        scan_tabs.pack(side="left", fill="y", padx=(24, 12), pady=18)
        scan_controls = ttk.Frame(scan_tabs, padding=18)
        path_controls = ttk.Frame(scan_tabs, padding=18)
        scan_tabs.add(scan_controls, text="Sequential scan")
        scan_tabs.add(path_controls, text="Automatic path")
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

        self.default_cross_plan = minimum_default_cross_pairs(self.network)
        plan = self.default_cross_plan
        plan_text = ", ".join(f"{source + 1}→{output + 1}" for source, output, _ in plan)
        ttk.Label(path_controls, text="Automatic default-cross calibration", font=("Segoe UI", 11, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(path_controls, text=("Choose an input and its 0 V output in Route explorer. The sweep starts at the end switch; every non-target MZI remains at 0 V."), wraplength=270).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 10))
        for row, label in enumerate(("Lower limit [V]", "Upper limit [V]", "Step [V]", "Settle [ms]"), start=2):
            ttk.Label(path_controls, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(path_controls, textvariable=self.crosstalk_fields[label], width=12).grid(row=row, column=1, sticky="w", padx=(10, 0), pady=3)
        self.path_averages_var = tk.StringVar(value="3")
        ttk.Label(path_controls, text="Burst averages / point").grid(row=6, column=0, sticky="w", pady=(8, 5))
        ttk.Entry(path_controls, textvariable=self.path_averages_var, width=7).grid(row=6, column=1, sticky="w", padx=(10, 0), pady=(8, 5))
        ttk.Label(path_controls, text="Scan comment").grid(row=7, column=0, sticky="w", pady=3)
        ttk.Entry(path_controls, textvariable=self.crosstalk_comment_var, width=19).grid(row=7, column=1, sticky="w", padx=(10, 0), pady=3)
        self.path_calibration_button = ttk.Button(path_controls, text="Calibrate selected default-cross path", command=self.start_path_calibration)
        self.path_calibration_button.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(8, 6))
        ttk.Label(path_controls, text=f"Minimum all-switch plan: {len(plan)} pairs — {plan_text}", wraplength=270).grid(row=9, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(path_controls, textvariable=self.crosstalk_status, wraplength=270).grid(row=10, column=0, columnspan=2, sticky="w")
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

        # Keep every routing action beside the diagram.  The old stacked layout
        # let the canvas consume the window height and clipped these controls.
        self.status = ttk.Label(self.route_tab, font=("Segoe UI", 11, "bold"), padding=(24, 4))
        self.status.pack(fill="x")
        self.mapping_label = ttk.Label(self.route_tab, padding=(24, 2, 24, 6))
        self.mapping_label.pack(fill="x")

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
        ttk.Button(route_sidebar, text="Apply selected path", command=self.apply_route).pack(anchor="w", fill="x")

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

        diagram = ttk.Frame(route_body)
        diagram.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(diagram, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda event: self.draw())
        ttk.Label(diagram, text="Green switches have saved bar and cross voltages. Click a switch to toggle it; type a voltage and press Enter to send it.", padding=(4, 5), wraplength=680).pack(anchor="w", fill="x")
        self.build_dataset_tab()
        self.build_impulse_tab()
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

    def fit_dialog_to_screen(self, dialog, preferred_width=900, preferred_height=650):
        """Size a dialog within the current display, leaving OS chrome visible."""
        width = min(preferred_width, max(480, self.winfo_screenwidth() - 100))
        height = min(preferred_height, max(360, self.winfo_screenheight() - 140))
        dialog.geometry(f"{width}x{height}")
        dialog.minsize(min(480, width), min(360, height))

    def _ask_on_main_thread(self, title, message, icon=messagebox.QUESTION):
        """Ask a yes/no question, also from a scan worker thread."""
        accepted = [False]
        ready = threading.Event()

        def ask():
            try:
                accepted[0] = messagebox.askyesno(title, message, icon=icon, parent=self)
            finally:
                ready.set()

        if threading.current_thread() is threading.main_thread():
            ask()
        else:
            self.after(0, ask)
            ready.wait()
        return accepted[0]

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

    def click_input(self, row):
        self.input_box.current(row)
        self.choose_input()

    def click_output(self, row):
        self.output_box.current(row + 1)
        self.choose_output()

    def toggle(self, name):
        """Cycle a switch: cross -> straight -> 0 V -> cross."""
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
        self.zero_volt_switches.discard(name)
        self.draw()
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

    def connect_osa_output_switch(self):
        try:
            if self.osa_output_switch.connected:
                raise RuntimeError("Disconnect the OSA output switch before changing its settings")
            self.osa_output_switch.configure(
                self.osa_switch_port_var.get(), self.osa_switch_baud_var.get(),
                self.osa_switch_dry_run_var.get(), self.osa_switch_command_var.get(),
                self.osa_switch_settle_var.get(),
            )
            self.osa_output_switch.connect()
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("OSA output switch", str(error), parent=self)
            return
        mode = "dry run" if self.osa_output_switch.settings["dry_run"] else self.osa_output_switch.settings["port"]
        self.osa_switch_status_var.set(f"OSA output switch connected: {mode}")

    def disconnect_osa_output_switch(self):
        self.osa_output_switch.disconnect()
        self.osa_switch_status_var.set("OSA output switch disconnected")

    def show_osa_output_switch_setup(self):
        """Keep optional protocol details out of the normal measurement workflow."""
        dialog = tk.Toplevel(self)
        dialog.title("OSA output switch setup")
        dialog.configure(bg=BG)
        dialog.transient(self)
        dialog.grab_set()
        self.fit_dialog_to_screen(dialog, preferred_width=520, preferred_height=420)
        body = ttk.Frame(dialog, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="OSA output switch", foreground=ACCENT,
                  font=("Segoe UI", 12, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(body, text="Use Dry run until the selector command protocol is known.",
                  foreground="#9baec6", wraplength=420).grid(row=1, column=0, columnspan=2,
                                                               sticky="w", pady=(3, 14))
        fields = (
            ("Serial port", self.osa_switch_port_var, 14),
            ("Baud", self.osa_switch_baud_var, 14),
            ("Command template", self.osa_switch_command_var, 28),
            ("Settle [s]", self.osa_switch_settle_var, 14),
        )
        for row, (label, variable, width) in enumerate(fields, start=2):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(body, textvariable=variable, width=width).grid(row=row, column=1, sticky="w", padx=(12, 0), pady=4)
        ttk.Checkbutton(body, text="Dry run", variable=self.osa_switch_dry_run_var).grid(
            row=6, column=1, sticky="w", padx=(12, 0), pady=(4, 0))
        ttk.Label(body, text="Physical serial commands must contain {port}; for example OUT {port}.",
                  foreground="#9baec6", wraplength=420).grid(row=7, column=0, columnspan=2,
                                                               sticky="w", pady=(12, 0))

        def connect_and_close():
            self.connect_osa_output_switch()
            if self.osa_output_switch.connected:
                dialog.destroy()

        buttons = ttk.Frame(body)
        buttons.grid(row=8, column=0, columnspan=2, sticky="e", pady=(18, 0))
        ttk.Button(buttons, text="Disconnect", command=self.disconnect_osa_output_switch).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Connect", style="Accent.TButton", command=connect_and_close).pack(side="left")

    def osa_connection(self):
        """Return OSA connection settings after validating the visible fields."""
        simulate = self.osa_dry_run_var.get()
        host = self.osa_host_var.get().strip()
        try:
            port = int(self.osa_port_var.get()) if not simulate else 0
        except ValueError as error:
            raise ValueError("Enter an OSA port from 1 to 65535") from error
        if not simulate and (not host or not 1 <= port <= 65535):
            raise ValueError("Enter a valid OSA host and port in Hardware & OSA")
        return host, port, simulate

    def set_osa_reading(self, reading: bool):
        state = "disabled" if reading else "normal"
        self.osa_button.configure(state=state)
        self.osa_port_read_button.configure(state=state)
        self.osa_all_outputs_button.configure(state=state)

    def osa_trace_name(self, output):
        """Identify the current routed input as well as the sampled output/fiber."""
        mapping = self.network.mapping()
        input_port = mapping.index(output - 1) + 1
        return f"In {input_port} → Out {output} · Fiber {output + 3}"

    def start_osa_read(self, output=None):
        try:
            host, port, simulate = self.osa_connection()
            if output is not None:
                output = int(output)
                if not self.osa_output_switch.connected:
                    raise RuntimeError("Connect the OSA output switch first (dry run is available)")
        except (RuntimeError, ValueError) as error:
            messagebox.showerror("OSA read unavailable", str(error), parent=self)
            return
        self.set_osa_reading(True)

        def acquire():
            try:
                if output is not None:
                    self.osa_output_switch.select_output(output)
                data = osa.simulated_spectrum() if simulate else osa.get_spectrum(host, port)
            except Exception as error:
                self.after(0, lambda: self.osa_failed(str(error)))
            else:
                self.after(0, lambda: self.finish_osa_read(data, output))

        threading.Thread(target=acquire, daemon=True).start()

    def finish_osa_read(self, data, output):
        if output is not None:
            self.osa_output_var.set(str(output))
            self.osa_switch_status_var.set(
                f"OSA routed to output {output} (fiber {output + 3}); spectrum acquired."
            )
        self.show_spectrum(*data, name=None if output is None else self.osa_trace_name(output),
                           output_port=output)

    def read_selected_osa_output(self):
        self.start_osa_read(self.osa_switch_output_var.get())

    def read_all_osa_outputs(self):
        try:
            host, port, simulate = self.osa_connection()
            if not self.osa_output_switch.connected:
                raise RuntimeError("Connect the OSA output switch first (dry run is available)")
        except (RuntimeError, ValueError) as error:
            messagebox.showerror("OSA read unavailable", str(error), parent=self)
            return
        self.set_osa_reading(True)
        self.osa_switch_status_var.set("Reading OSA spectra from outputs 1–6...")

        def acquire_all():
            spectra = []
            try:
                for output in range(1, 7):
                    self.osa_output_switch.select_output(output)
                    spectrum = osa.simulated_spectrum() if simulate else osa.get_spectrum(host, port)
                    spectra.append((output, spectrum))
            except Exception as error:
                self.after(0, lambda: self.osa_failed(str(error)))
            else:
                self.after(0, lambda: self.show_all_osa_spectra(spectra))

        threading.Thread(target=acquire_all, daemon=True).start()

    def show_all_osa_spectra(self, spectra):
        self.set_osa_reading(False)
        for output, (wave, power) in spectra:
            self.show_spectrum(wave, power, name=self.osa_trace_name(output), restore_buttons=False,
                               output_port=output)
        if spectra:
            self.osa_output_var.set(str(spectra[-1][0]))
        self.osa_switch_status_var.set(f"Read {len(spectra)} OSA output spectra; export them from the plot window.")

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

    def start_path_calibration(self):
        """Sweep the selected zero-volt optical path, starting at its final MZI."""
        try:
            if self.destination is None:
                raise ValueError("Choose both an input and a destination output in Route explorer")
            if not self.controller.connected or self.controller.settings["dry_run"]:
                raise RuntimeError("Connect the physical voltage board first")
            if not self.power_meter.connected:
                raise RuntimeError("Connect the Arduino power meter first")
            if self.read_power_button.instate(["disabled"]):
                raise RuntimeError("Wait for the current Arduino power reading to finish")
            meter_output = self.detector_port(self.power_meter_output_var)
            if meter_output != self.destination:
                raise ValueError("Set Power Meter output to the selected destination output before calibrating")
            fields = self.crosstalk_fields
            voltages = scan_voltages(*(float(fields[key].get()) for key in
                                       ("Lower limit [V]", "Upper limit [V]", "Step [V]")))
            if 0.0 not in voltages:
                raise ValueError("Include 0 V in the calibration range to verify the default cross state")
            settle_ms = float(fields["Settle [ms]"].get())
            if not 0 <= settle_ms <= 10000:
                raise ValueError("Settling time must be between 0 and 10000 ms")
            averages = int(self.path_averages_var.get())
            if not 1 <= averages <= 100:
                raise ValueError("Burst averages must be an integer from 1 to 100")
            zero_route = zero_voltage_paths(self.network)[self.source]
            if zero_route.rows[-1] != self.destination:
                raise ValueError(
                    f"At 0 V, input {self.source + 1} reaches output {zero_route.rows[-1] + 1}; "
                    "select that output so all non-target MZIs can remain at 0 V"
                )
            names = [name for name, _ in reversed(zero_route.states)]
            for name in self.network.switches:
                if self.controller.settings["mapping"].get(name) is None:
                    raise ValueError(f"Assign a voltage-board pin to every MZI before automatic path calibration ({name} is unassigned)")
            self.confirm_voltage_sweep(voltages, names=names)
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk  # noqa: F401
            from matplotlib.figure import Figure  # noqa: F401
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Path calibration unavailable", str(error), parent=self)
            return

        zero_states = dict.fromkeys(self.network.switches, True)
        context = scan_context(
            self.network, self.source, self.destination,
            meter_output, self.detector_port(self.osa_output_var), self.controller,
            names, voltages, settle_ms, "Arduino", self.crosstalk_comment_var.get().strip(),
            states=zero_states,
        )
        context["calibration_mode"] = "default_cross_path"
        context["burst_averages_per_point"] = averages
        context["sensitive_range_rescans"] = {}
        context["wide_range_rescans"] = {}
        try:
            result_folder = create_result_folder(app_dir(), context)
            save_scan_artifacts(result_folder, context, {}, "Arduino", self.power_meter.settings["unit"],
                                self.network, status="in_progress")
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Calibration results unavailable", str(error), parent=self)
            return

        self.active_scan_kind = "Path calibration"
        self.crosstalk_running = True
        self.crosstalk_data = {}
        self.crosstalk_data_source = None
        self.crosstalk_data_unit = None
        self.crosstalk_result_folder = result_folder
        self.crosstalk_button.configure(state="disabled")
        self.path_calibration_button.configure(state="disabled")
        self.crosstalk_export_button.configure(state="disabled")
        self.crosstalk_switches.configure(state="disabled")
        self.crosstalk_source_box.configure(state="disabled")
        self.notebook.tab(self.route_tab, state="disabled")
        self.notebook.tab(self.hardware_tab, state="disabled")
        self.crosstalk_status.set(
            f"Calibrating {len(names)} MZI(s), from output back to input; "
            f"{averages} burst average(s) per point..."
        )
        self.prepare_crosstalk_plots(voltages)
        data = {}

        def on_point(name, index, total, point):
            data.setdefault(name, []).append(point)
            self.after(0, lambda: self.add_crosstalk_point(
                name, point, index, total, "Arduino", self.power_meter.settings["unit"]
            ))

        def on_sensitive_point(name, index, total, point):
            context["sensitive_range_rescans"].setdefault(name, []).append({
                "voltage_v": point.voltage, "power": point.power,
            })
            self.after(0, lambda: self.add_rescan_point(name, "sensitive", point, index, total))

        def on_wide_point(name, index, total, point):
            context["wide_range_rescans"].setdefault(name, []).append({
                "voltage_v": point.voltage, "power": point.power,
            })
            self.after(0, lambda: self.add_rescan_point(name, "wide", point, index, total))

        def on_range_change(name, phase, limit_count):
            ready = threading.Event()
            confirmed = [False]

            def ask_user():
                try:
                    if phase == "sensitive":
                        self.crosstalk_status.set(f"{name}: waiting for a more sensitive meter range...")
                        message = (
                            f"{name} had {limit_count} readings at the meter floor. The switch is now at 0 V.\n\n"
                            "Change the power meter to its more sensitive (low) range, "
                            "then click OK to rescan for the minimum (straight state).\n\n"
                            "Cancel stops calibration without saving this switch."
                        )
                    elif phase == "wide":
                        self.crosstalk_status.set(f"{name}: waiting for a wider meter range...")
                        message = (
                            f"{name} had {limit_count} readings at or above the meter ceiling "
                            f"({self.power_meter.ceiling:g} ADC counts). The switch is now at 0 V.\n\n"
                            "Change the power meter to its less sensitive (high) range, "
                            "then click OK to rescan for the maximum (cross state).\n\n"
                            "Cancel stops calibration without saving this switch."
                        )
                    else:
                        self.crosstalk_status.set(f"{name}: waiting for the original meter range...")
                        finished = "minimum" if phase == "restore_sensitive" else "maximum"
                        message = (
                            f"The {finished} rescan of {name} is finished. The switch is at 0 V.\n\n"
                            "Return the power meter to the range used at the start of this switch, "
                            "then click OK before calibration continues.\n\n"
                            "Cancel stops calibration."
                        )
                    confirmed[0] = messagebox.askokcancel(
                        "Change power meter range", message, parent=self,
                    )
                finally:
                    ready.set()

            self.after(0, ask_user)
            ready.wait()
            return confirmed[0]

        def worker():
            try:
                results = calibrate_default_cross_path(
                    self.controller, self.power_meter, self.network, self.source, self.destination,
                    voltages, settle_ms / 1000, averages, on_point,
                    on_range_change=on_range_change, on_sensitive_point=on_sensitive_point,
                    on_wide_point=on_wide_point,
                )
                context["calibration_results"] = {
                    name: {
                        "cross_voltage_v": result.cross.voltage,
                        "cross_power": result.cross.power,
                        "cross_measurement_range": "wide" if result.wide_points else "original",
                        "straight_voltage_v": result.straight.voltage,
                        "straight_power": result.straight.power,
                        "straight_measurement_range": "sensitive" if result.sensitive_points else "original",
                        "straight_at_noise_floor": result.straight_at_floor,
                    }
                    for name, result in results.items()
                }
            except Exception as error:
                self.after(0, lambda message=str(error): self.finish_path_calibration(
                    names, data, result_folder, context, message
                ))
            else:
                self.after(0, lambda: self.finish_path_calibration(names, data, result_folder, context))

        threading.Thread(target=worker, daemon=True).start()

    def finish_path_calibration(self, names, data, folder, context, error=None):
        # The worker has forced every hardware output to 0 V, which is cross.
        self.network.states = dict.fromkeys(self.network.switches, True)
        self.zero_volt_switches = set(self.network.switches)
        for name in self.network.switches:
            self.voltage_vars[name].set("0.00")
        self.refresh(keep_selection=True)
        self.next_connection_prompt = None
        if error is None:
            self.path_calibrated_switches.update(context["calibration_results"])
            next_pair = next_default_cross_pair(self.default_cross_plan, self.path_calibrated_switches)
            self.next_connection_prompt = next_pair if next_pair is not None else "complete"
        self.finish_crosstalk_scan(names, data, "Arduino", self.power_meter.settings["unit"],
                                   folder, context, error)
        at_floor = [name for name, result in context.get("calibration_results", {}).items()
                    if (result or {}).get("straight_at_noise_floor")]
        if error is None and at_floor:
            messagebox.showwarning(
                "Straight state at noise floor",
                f"{', '.join(at_floor)} still reached the noise floor on the sensitive range. "
                "Their straight voltages are the lowest reading found and may be imprecise; "
                "check them or recalibrate with more light.", parent=self)

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
            self.confirm_voltage_sweep(voltages, names=names)
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
            result_folder = create_result_folder(app_dir(), context)
            save_scan_artifacts(result_folder, context, {}, source, response_unit, self.network,
                                status="in_progress")
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Crosstalk results unavailable", str(error), parent=self)
            return

        self.active_scan_kind = "Crosstalk scan"
        self.crosstalk_running = True
        self.crosstalk_data = {}
        self.crosstalk_data_source = None
        self.crosstalk_data_unit = None
        self.crosstalk_result_folder = result_folder
        self.crosstalk_button.configure(state="disabled")
        self.path_calibration_button.configure(state="disabled")
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
        self.path_calibration_button.configure(state="normal")
        self.crosstalk_switches.configure(state="normal")
        self.crosstalk_source_box.configure(state="readonly")
        self.notebook.tab(self.route_tab, state="normal")
        self.notebook.tab(self.hardware_tab, state="normal")
        if data:
            self.crosstalk_export_button.configure(state="normal")
        if scan_error:
            self.crosstalk_status.set(f"{self.active_scan_kind} stopped: {scan_error}. Results: {folder}")
            messagebox.showerror(f"{self.active_scan_kind} failed", scan_error, parent=self)
        elif save_error:
            self.crosstalk_status.set(f"{self.active_scan_kind} finished, but automatic export failed: {save_error}")
        elif self.active_scan_kind == "Path calibration":
            covered = len(self.path_calibrated_switches)
            total = len(self.network.switches)
            progress = ("Default-cross calibration complete" if covered == total else "Path scan finished")
            self.crosstalk_status.set(
                f"{progress}; {covered}/{total} MZIs calibrated this session. Results: {folder}"
            )
        else:
            self.crosstalk_status.set(f"{self.active_scan_kind} complete. Results: {folder}")
        if save_error:
            messagebox.showerror("Crosstalk export failed", f"{save_error}\nFolder: {folder}", parent=self)
        if self.next_connection_prompt is not None:
            next_connection = self.next_connection_prompt
            self.next_connection_prompt = None
            if next_connection == "complete":
                messagebox.showinfo(
                    "Default-cross plan complete",
                    "All MZIs have been calibrated by path scans this session. "
                    "All MZIs are currently at 0 V.",
                    parent=self,
                )
            else:
                input_port, output_port = next_connection
                messagebox.showinfo(
                    "Select the next fiber connection",
                    f"{len(self.path_calibrated_switches)}/{len(self.network.switches)} MZIs calibrated "
                    f"this session. Next default-cross path:\n\n"
                    f"Input {input_port + 1} — Fiber {15 - input_port}\n"
                    f"Output {output_port + 1} — Fiber {4 + output_port}\n\n"
                    f"Select In {input_port + 1} and Out {output_port + 1} in Route explorer, "
                    f"then set Power Meter output to Out {output_port + 1}.",
                    parent=self,
                )

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

    def show_mapping(self):
        dialog = tk.Toplevel(self)
        dialog.title("MZI pin mapping and calibration")
        dialog.configure(bg=BG)
        dialog.transient(self)
        dialog.grab_set()
        # Save/Cancel stay visible at the bottom; the table scrolls above them.
        buttons = ttk.Frame(dialog, padding=(18, 10, 18, 14))
        buttons.pack(side="bottom", fill="x")
        scroller = ScrollableFrame(dialog, background=BG, padding=18)
        scroller.pack(side="top", fill="both", expand=True)
        body = scroller.inner
        ttk.Label(body, text="Assign each MZI to a unique VoltageTuner pin (0–15).", font=("Segoe UI", 11, "bold")).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(body, text="Save a pin mapping before scanning. Each scan restores the switch voltage; choose which power extreme means straight and cross.", wraplength=650).grid(row=1, column=0, columnspan=6, sticky="w", pady=(4, 14))
        for column, title in enumerate(("MZI", "Pin", "Device", "R heater [Ω]", "R total [Ω]",
                                        "J peak/V [mA/µm²/V]", "Max V safe / break", "Straight [V]",
                                        "Cross [V]", "Power scan", "Saved values")):
            ttk.Label(body, text=title).grid(row=2, column=column, sticky="w", padx=(0, 10), pady=(0, 4))
        pin_vars, straight_vars, cross_vars, resistance_vars = {}, {}, {}, {}
        device_vars, total_vars, density_vars, limit_labels, device_boxes = {}, {}, {}, {}, []
        settings = self.controller.settings
        choices = ("Unassigned",) + tuple(str(pin) for pin in range(16))

        def parse_float(text):
            try:
                return float(text)
            except ValueError:
                return None

        def optional_float(text):
            value = parse_float(text)
            return value if value is not None and value > 0 else None

        def update_limits(name):
            """Show the voltages (and currents) at the safe and break current densities."""
            density = optional_float(density_vars[name].get())
            safe = voltage_limit(density, SAFE_CURRENT_DENSITY)
            if safe is None:
                limit_labels[name].configure(text="—", foreground=TEXT)
                return
            broken = voltage_limit(density, BREAK_CURRENT_DENSITY)
            text = f"{safe:.2f} / {broken:.2f} V"
            total = (optional_float(total_vars[name].get()) or optional_float(resistance_vars[name].get())
                     or optional_float(default_resistance_var.get()))
            if total:
                text += f"  ({safe / total * 1000:.1f} / {broken / total * 1000:.1f} mA)"
            # Flag saved calibration voltages that exceed a limit.
            saved = [abs(value) for value in map(parse_float, (straight_vars[name].get(), cross_vars[name].get()))
                     if value is not None]
            highest = max(saved, default=0.0)
            color = "#ff6b6b" if highest >= broken else ZERO_VOLT_COLOR if highest > safe else TEXT
            limit_labels[name].configure(text=text, foreground=color)

        def apply_device(name):
            device = self.controller.imported_heaters().get(device_vars[name].get())
            if device is not None:
                resistance_vars[name].set(f"{device.r_heater_ohm:g}")
                total_vars[name].set(f"{device.r_total_ohm:g}")
                density_vars[name].set(f"{device.j_peak_per_v:.4g}")

        def import_heaters():
            setups = app_dir().parent / "GDS_heater" / "Setups"
            path = filedialog.askopenfilename(
                parent=dialog, title="Import heater resistances (GDS_heater CSV)",
                initialdir=str(setups if setups.exists() else app_dir()),
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
            if not path:
                return
            try:
                devices = load_heater_csv(path)
                self.controller.set_imported_heaters(devices)
            except (OSError, ValueError) as error:
                messagebox.showerror("Could not import heater CSV", str(error), parent=dialog)
                return
            names = list(devices)
            for box in device_boxes:
                box.configure(values=("",) + tuple(names))
            # Keep existing assignments that are in the new table; fill the rest in file order.
            for index, name in enumerate(self.network.switches):
                if device_vars[name].get() not in devices:
                    device_vars[name].set(names[index] if index < len(names) else "")
                apply_device(name)
            import_status.set(f"Imported {len(devices)} devices from {Path(path).name}. Unassigned rows were "
                              "filled in file order; check the Device column, then Save.")

        def forget_calibration(names, state=None):
            try:
                self.controller.clear_calibration(names, state)
            except (OSError, ValueError) as error:
                messagebox.showerror("Could not forget calibration", str(error), parent=dialog)
                return
            for name in names:
                if state in (None, "straight"):
                    straight_vars[name].set("")
                if state in (None, "cross"):
                    cross_vars[name].set("")
            self.path_calibrated_switches.difference_update(names)
            self.draw()
            detail = (f"{names[0]} {state or 'straight/cross'} calibration forgotten"
                      if len(names) == 1 else "All calibrations forgotten")
            self.update_hardware_status(detail)

        def forget_all_calibrations():
            if messagebox.askyesno(
                "Forget all calibrations",
                "Forget the saved straight and cross voltages for every MZI?",
                parent=dialog,
            ):
                forget_calibration(tuple(self.network.switches))

        for row, name in enumerate(self.network.switches, start=3):
            pin = self.controller.settings["mapping"][name]
            calibration = self.controller.settings["calibration"][name]
            pin_vars[name] = tk.StringVar(value="Unassigned" if pin is None else str(pin))
            straight_vars[name] = tk.StringVar(value="" if calibration["straight"] is None else f"{calibration['straight']:.2f}")
            cross_vars[name] = tk.StringVar(value="" if calibration["cross"] is None else f"{calibration['cross']:.2f}")
            ttk.Label(body, text=name).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=2)
            ttk.Combobox(body, textvariable=pin_vars[name], values=choices, state="readonly", width=11).grid(row=row, column=1, sticky="w", padx=(0, 10), pady=2)

            def pin_value(key, fmt="g", pin=pin):
                value = None if pin is None else settings[key].get(str(pin))
                return "" if value is None else format(value, fmt)

            device_vars[name] = tk.StringVar(value=pin_value("heater_device", ""))
            box = ttk.Combobox(body, textvariable=device_vars[name], state="readonly", width=8,
                               values=("",) + tuple(settings["imported_heaters"]))
            box.grid(row=row, column=2, sticky="w", padx=(0, 10), pady=2)
            box.bind("<<ComboboxSelected>>", lambda event, n=name: apply_device(n))
            device_boxes.append(box)
            # Blank R heater uses the default resistance below; blank R total equals R heater.
            resistance_vars[name] = tk.StringVar(value=pin_value("heater_resistance_ohm"))
            total_vars[name] = tk.StringVar(value=pin_value("total_resistance_ohm"))
            density_vars[name] = tk.StringVar(value=pin_value("current_density_per_v", ".4g"))
            for column, variable in enumerate((resistance_vars, total_vars, density_vars), start=3):
                ttk.Entry(body, textvariable=variable[name], width=8).grid(row=row, column=column, sticky="w", padx=(0, 10), pady=2)
            limit_labels[name] = ttk.Label(body, text="—")
            limit_labels[name].grid(row=row, column=6, sticky="w", padx=(0, 10), pady=2)
            straight_cell = ttk.Frame(body)
            straight_cell.grid(row=row, column=7, sticky="w", padx=(0, 10), pady=2)
            ttk.Entry(straight_cell, textvariable=straight_vars[name], width=8).pack(side="left")
            ttk.Button(straight_cell, text="×", width=2,
                       command=lambda n=name: forget_calibration((n,), "straight")).pack(side="left", padx=(3, 0))
            cross_cell = ttk.Frame(body)
            cross_cell.grid(row=row, column=8, sticky="w", pady=2)
            ttk.Entry(cross_cell, textvariable=cross_vars[name], width=8).pack(side="left")
            ttk.Button(cross_cell, text="×", width=2,
                       command=lambda n=name: forget_calibration((n,), "cross")).pack(side="left", padx=(3, 0))
            ttk.Button(body, text="Scan...", command=lambda n=name: self.show_power_scan(dialog, n, pin_vars[n], straight_vars[n], cross_vars[n])).grid(row=row, column=9, sticky="w", padx=(10, 0), pady=2)
            ttk.Button(body, text="Forget", command=lambda n=name: forget_calibration((n,))).grid(row=row, column=10, sticky="w", padx=(10, 0), pady=2)
            for variable in (resistance_vars, total_vars, density_vars, straight_vars, cross_vars):
                variable[name].trace_add("write", lambda *args, n=name: update_limits(n))

        def save_mapping():
            try:
                mapping = {name: None if pin_vars[name].get() == "Unassigned" else int(pin_vars[name].get()) for name in self.network.switches}
                self.controller.set_mapping(mapping)
                for name in self.network.switches:
                    self.controller.set_calibration(name, straight_vars[name].get(), cross_vars[name].get())
                self.controller.set_heater_parameters(
                    {mapping[name]: {"r_heater": resistance_vars[name].get(), "r_total": total_vars[name].get(),
                                     "j_per_v": density_vars[name].get(), "device": device_vars[name].get()}
                     for name in self.network.switches if mapping[name] is not None},
                    default_resistance_var.get())
                # The limits may have changed, so earlier approvals no longer apply.
                self._approved_over_safe.clear()
            except ValueError as error:
                messagebox.showerror("Invalid mapping or calibration", str(error), parent=dialog)
                return
            self.apply_calibration()
            dialog.destroy()
            self.draw()
            self.replot_crosstalk_x_axis()
            self.update_hardware_status("Mapping saved")
            self.refresh(keep_selection=True)

        default_resistance = self.controller.settings["default_heater_resistance_ohm"]
        default_resistance_var = tk.StringVar(value="" if default_resistance is None else f"{default_resistance:g}")
        default_row = ttk.Frame(body)
        default_row.grid(row=3 + len(self.network.switches), column=0, columnspan=11, sticky="w", pady=(12, 0))
        ttk.Button(default_row, text="Import heater CSV...", command=import_heaters).pack(side="left", padx=(0, 16))
        ttk.Label(default_row, text="Default R [Ω]").pack(side="left")
        ttk.Entry(default_row, textvariable=default_resistance_var, width=8).pack(side="left", padx=(8, 12))
        ttk.Label(default_row, text="Used as R heater and R total for pins with a blank R heater.",
                  foreground="#9baec6").pack(side="left")
        import_status = tk.StringVar(value="")
        ttk.Label(body, textvariable=import_status, foreground=ZERO_VOLT_COLOR, wraplength=900).grid(
            row=4 + len(self.network.switches), column=0, columnspan=11, sticky="w", pady=(6, 0))
        ttk.Label(body, foreground="#9baec6", wraplength=900, text=(
            "Current I = V / R total; the power axis is the heater power I²·R heater. Max V is where the peak "
            f"current density in the heater reaches the safe {SAFE_CURRENT_DENSITY:g} mA/µm² "
            f"or break {BREAK_CURRENT_DENSITY:g} mA/µm² limit; the currents at those voltages are in brackets. "
            "Voltages above the safe limit need confirmation; the break limit is never sent. "
            "Amber: a saved voltage exceeds the safe limit. Red: it reaches the break limit."
        )).grid(row=5 + len(self.network.switches), column=0, columnspan=11, sticky="w", pady=(6, 0))
        for name in self.network.switches:
            update_limits(name)
        default_resistance_var.trace_add("write", lambda *args: [update_limits(n) for n in self.network.switches])
        ttk.Button(buttons, text="Forget all calibrations", command=forget_all_calibrations).pack(side="left", padx=(0, 18))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="Save", command=save_mapping).pack(side="right", padx=6)
        fit_to_screen(dialog)

    def show_power_scan(self, parent, name, pin_var, straight_var, cross_var):
        window = tk.Toplevel(parent)
        window.title(f"Power scan: {name}")
        window.transient(parent)
        window.grab_set()
        window.configure(bg=BG)
        scroller = ScrollableFrame(window, background=BG, padding=18)
        scroller.pack(fill="both", expand=True)
        frame = scroller.inner
        fields = {}
        defaults = {"Lower limit [V]": "0", "Upper limit [V]": "5",
                    "Step [V]": "0.5", "Settle [ms]": "50"}
        saved = self.scan_settings.get(name, defaults)
        for row, label in enumerate(defaults):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=3)
            value = tk.StringVar(value=saved[label])
            ttk.Entry(frame, textvariable=value, width=12).grid(row=row, column=1, sticky="w", padx=8, pady=3)
            fields[label] = value
        note = f"Each point uses the Arduino burst ({self.power_meter.settings['duration']:g} s)."
        safe, broken = self.controller.voltage_limits(name)
        if safe is not None:
            note += (f"\nCurrent-density limits for this heater: {safe:.2f} V safe ({SAFE_CURRENT_DENSITY:g} mA/µm²), "
                     f"{broken:.2f} V break ({BREAK_CURRENT_DENSITY:g} mA/µm²).")
        ttk.Label(frame, text=note, wraplength=360).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 4))
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
            try:
                self.confirm_voltage_sweep(voltages, parent=window, names=[name])
            except ValueError as error:
                messagebox.showerror("Scan cancelled", str(error), parent=window)
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
        fit_to_screen(window)

    def read_spectrum(self):
        """Read the currently connected OSA without changing the output selector."""
        self.start_osa_read()

    def osa_failed(self, error):
        self.set_osa_reading(False)
        self.osa_switch_status_var.set(f"OSA read failed: {error}")
        messagebox.showerror("OSA read failed", error, parent=self)

    def show_spectrum(self, wave, power, name=None, restore_buttons=True, output_port=None):
        if restore_buttons:
            self.set_osa_reading(False)
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
        if name is None:
            name = simpledialog.askstring("Trace name", "Name for this OSA trace:", initialvalue=f"Trace {self.trace_count}", parent=self)
            if name is None:
                return
        name = name.strip() or f"Trace {self.trace_count}"
        if output_port is None:
            selected_output = self.detector_port(self.osa_output_var)
            output_port = None if selected_output is None else selected_output + 1
        # Dataset saves use the current acquisition for each output, not every
        # historical overlay trace that happens to share the same port.
        if output_port is not None:
            self.osa_trace_records = [item for item in self.osa_trace_records
                                      if item["output_port"] != output_port]
        self.osa_trace_records.append({
            "name": name, "wave": list(wave), "power": list(power), "output_port": output_port,
        })
        new_window = self.osa_plot_mode.get() == "New window" or self.osa_axes is None
        if not new_window:
            try:
                new_window = not self.osa_axes.figure.canvas.get_tk_widget().winfo_exists()
            except tk.TclError:
                new_window = True
        if new_window:
            window = tk.Toplevel(self)
            window.title("OSA Spectrum")
            self.fit_dialog_to_screen(window, preferred_width=900, preferred_height=650)
            figure = Figure(figsize=(8, 5))
            self.osa_axes = figure.add_subplot(xlabel="Wavelength [nm]", ylabel="Power [dBm]", title="OSA Spectrum")
            self.osa_axes.grid(True)
            canvas = FigureCanvasTkAgg(figure, master=window)
            NavigationToolbar2Tk(canvas, window).update()
            ttk.Button(window, text="Export displayed datasets", command=lambda axes=self.osa_axes: self.export_spectra(axes)).pack(anchor="e", padx=8, pady=4)
            canvas.get_tk_widget().configure(**PLOT_MIN_SIZE)
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

        folder_box = ttk.LabelFrame(controls, text="Dataset folder", padding=10)
        folder_box.pack(fill="x")
        ttk.Entry(folder_box, textvariable=self.dataset_folder_var, width=34).pack(fill="x")
        folder_buttons = ttk.Frame(folder_box)
        folder_buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(folder_buttons, text="Choose folderâ€¦", command=self.choose_dataset_folder).pack(
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
        ttk.Label(summary, text="The report counts the six routed inputâ†’output paths required for each expected permutation.",
                  wraplength=700).pack(anchor="w", pady=(2, 8))
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
        format_pair = lambda pair: f"In{pair[0]}â†’Out{pair[1]}"
        lines = [
            f"Dataset: {root}",
            f"Measurements saved: {report['measurement_count']}",
            f"Configurations complete: {report['complete_configurations']} / {report['expected_configurations']}",
            f"Routed paths covered: {report['covered_paths']} / {report['expected_paths']}  â€¢  missing {report['missing_paths']}",
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
                lines.append(f"  â€¦ plus {len(incomplete) - 30} more in {csv_path.name}")
        self.set_dataset_report("\n".join(lines))
        self.dataset_status_var.set(f"Coverage analysed: {report['missing_paths']} routed path(s) still missing.")

    def set_dataset_report(self, text):
        self.dataset_report.configure(state="normal")
        self.dataset_report.delete("1.0", "end")
        self.dataset_report.insert("1.0", text)
        self.dataset_report.configure(state="disabled")

    def close(self):
        if self.impulse_running:
            self.stop_impulse()
            return
        if self.crosstalk_running:
            messagebox.showinfo("Scan in progress", "Wait for the crosstalk scan to finish and reset the switches to 0 V.", parent=self)
            return
        self.controller.disconnect()
        self.power_meter.disconnect()
        self.osa_output_switch.disconnect()
        self.destroy()

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
            if self.crosstalk_running:
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
        for tab in (self.route_tab, self.hardware_tab, self.crosstalk_tab):
            self.notebook.tab(tab, state="disabled")
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
        for tab in (self.route_tab, self.hardware_tab, self.crosstalk_tab):
            self.notebook.tab(tab, state="normal")
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
        self.route_label.configure(text=f"{len(self.routes)} possible paths · = straight, X cross, 0V unbiased (routes as cross; click a switch to cycle) · switches omitted from a path are unconstrained" if self.routes else "Current route · select a destination to list every possible path and its required switch states")
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
        width, height = c.winfo_width(), c.winfo_height()
        # Tk initially reports a 1x1 canvas before its first layout pass.
        if width < 420 or height < 220:
            return
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
                crossed = self.network.states[name]
                zero = name in self.zero_volt_switches
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
                if show_voltage_entry:
                    entry = ttk.Entry(c, textvariable=self.voltage_vars[name], width=4, justify="center")
                    entry.bind("<Return>", lambda event, n=name: self.apply_voltage(n))
                    c.create_window(x, self.ys[b] + max(19, round(20 * scale)), window=entry)
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
                c.create_text(x, y, text=str(row + 1), fill=BG if selected else TEXT, font=("Segoe UI", max(10, round(12 * scale)), "bold"), tags=tag)
                if side == "in":
                    c.create_text(x - 25, y, text=f"In {row + 1}\nFiber {15 - row}", anchor="e",
                                  fill="#9baec6", font=font_small)
                else:
                    c.create_text(x + 25, y, text=f"Out {row + 1}\nFiber {4 + row}", anchor="w",
                                  fill="#9baec6", font=font_small)
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

    def draw_route(self, route, color, line_width, dashed=False):
        points = [self.left_x + 18, self.ys[route.rows[0]]]
        states = dict(route.states)
        for stage, row in enumerate(route.rows[:-1]):
            x = self.xs[stage]
            points.extend((x - self.switch_half, self.ys[row]))
            name = self.network.at.get((stage, row))
            exit_row = self.network.switch_output(stage, row, states[name] if name else False)
            points.extend((x + self.switch_half, self.ys[exit_row]))
        points.extend((self.right_x - 18, self.ys[route.rows[-1]]))
        self.canvas.create_line(*points, fill=color, width=line_width, dash=(5, 5) if dashed else (), joinstyle="round", capstyle="round", state="disabled")


if __name__ == "__main__":
    BenesApp().mainloop()
