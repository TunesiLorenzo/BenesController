"""Run with: python benes_gui.py"""

import random
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, simpledialog, ttk

from network import BenesNetwork
import osa
from power_meter import ArduinoPowerMeter
from spectrum_export import write_spectra
from voltage_controller import BenesVoltageController


BG = "#101827"
PANEL = "#192437"
WIRE = "#52647c"
TEXT = "#e6edf7"
ACCENT = "#53dfbb"
COLORS = ("#53dfbb", "#ffbe69", "#78b9ff", "#ed91cf", "#b6a0ff", "#fff080")


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
        self.source = 0
        self.destination = None
        self.routes = []
        self.selected_route = 0
        self.all_paths = tk.BooleanVar(value=True)
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
        self.notebook.add(self.route_tab, text="Route explorer")
        self.notebook.add(self.hardware_tab, text="Boards & measurement")

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

        self.status = ttk.Label(self.route_tab, font=("Segoe UI", 12, "bold"), padding=(24, 8))
        self.status.pack(fill="x")
        self.canvas = tk.Canvas(self.route_tab, bg=BG, highlightthickness=0, height=355)
        self.canvas.pack(fill="both", expand=True, padx=12)
        self.canvas.bind("<Configure>", lambda event: self.draw())

        ttk.Label(self.route_tab, text="0 V is the default cross state. Click a switch to toggle it. Type a voltage in a switch field and press Enter to send it.", padding=(24, 6)).pack(anchor="w")
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

    def click_input(self, row):
        self.input_box.current(row)
        self.choose_input()

    def click_output(self, row):
        self.output_box.current(row + 1)
        self.choose_output()

    def toggle(self, name):
        self.network.states[name] = not self.network.states[name]
        self.apply_calibration()
        self.refresh(keep_selection=True)

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

    def apply_calibration(self):
        try:
            self.controller.apply_calibrated_states(self.network.states)
        except (OSError, RuntimeError, ValueError) as error:
            messagebox.showerror("Calibration command failed", str(error), parent=self)

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
        ttk.Label(body, text="Straight and cross values are optional calibration points. When set, routing or clicking a switch sends the voltage for its new state.", wraplength=590).grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 14))
        for column, title in enumerate(("MZI", "Pin", "Straight [V]", "Cross [V]")):
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
            self.update_hardware_status("Mapping saved")
            self.refresh(keep_selection=True)

        buttons = ttk.Frame(body)
        buttons.grid(row=3 + len(self.network.switches), column=0, columnspan=4, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="Save", command=save_mapping).pack(side="right", padx=6)

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
        if len(power) >= 2:
            power[-1] = power[-2]
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
                tag = f"switch:{name}"
                c.create_rectangle(x - half, self.ys[a] - 12, x + half, self.ys[b] + 12, fill=PANEL, outline="#7790ad", width=1.5, tags=tag)
                for start, end in ((a, b), (b, a)) if crossed else ((a, a), (b, b)):
                    c.create_line(x - half, self.ys[start], x + half, self.ys[end], fill="#a4bad4", width=2, tags=tag)
                c.create_text(x, self.ys[a] - 23, text=f"{name} {'X' if crossed else '='}", fill=TEXT, font=("Segoe UI", 9), tags=tag)
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
