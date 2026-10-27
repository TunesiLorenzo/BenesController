"""MZI ↔ pin mapping dialog and the per-MZI power scan used to set calibration voltages."""

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from benes.core.heater_setup import (BREAK_CURRENT_DENSITY, SAFE_CURRENT_DENSITY, load_heater_csv,
                                     voltage_limit)
from benes.gui.theme import BG, TEXT, ZERO_VOLT_COLOR
from benes.gui.widgets import ScrollableFrame, fit_to_screen
from benes.measurements.calibration_scan import scan_switch, scan_voltages
from benes.paths import app_dir


class MappingMixin:
    """Mixed into ``BenesApp``; relies on the state it sets up."""

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
        progress_bar = ttk.Progressbar(frame, maximum=1, style="Activity.Horizontal.TProgressbar")
        progress_bar.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        extrema = tk.StringVar(value="")
        ttk.Label(frame, textvariable=extrema, wraplength=390).grid(row=7, column=0, columnspan=3, sticky="w", pady=6)
        straight_choice = tk.StringVar()
        cross_choice = tk.StringVar()
        for row, (label, variable) in enumerate((("Straight", straight_choice), ("Cross", cross_choice)), start=8):
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
            self.begin_activity("mapping_scan", f"Scanning {name} voltage response...", len(voltages) + 1)
            progress_bar.configure(maximum=len(voltages) + 1, value=0)
            result.update(maximum=None, minimum=None)
            straight_choice.set("")
            cross_choice.set("")
            extrema.set("")
            scan_button.configure(state="disabled")
            save_button.configure(state="disabled")
            progress.set(f"Scanning {len(voltages)} voltages...")

            def on_point(index, total, point):
                self.after(0, lambda: progress.set(f"{index}/{total}: {point.voltage:.2f} V, {point.power:.4g} {self.power_meter.settings['unit']}"))
                self.after(0, lambda i=index: progress_bar.configure(value=i))
                self.after(0, lambda i=index, t=total, v=point.voltage: self.update_activity(
                    "mapping_scan", i, detail=f"{name}: voltage {i}/{t} ({v:.2f} V)"))

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
            progress_bar.configure(value=0)
            self.finish_activity("mapping_scan", f"{name} voltage scan failed")
            scan_button.configure(state="normal")
            progress.set(f"Scan failed: {message}")

        def finish_scan(points):
            running["value"] = False
            progress_bar.configure(value=len(points) + 1)
            self.finish_activity("mapping_scan", f"{name} voltage scan complete")
            scan_button.configure(state="normal")
            save_button.configure(state="normal")
            result["maximum"] = max(points, key=lambda point: point.power)
            result["minimum"] = min(points, key=lambda point: point.power)
            unit = self.power_meter.settings["unit"]
            extrema.set(f"Maximum: {result['maximum'].power:.4g} {unit} at {result['maximum'].voltage:.2f} V\nMinimum: {result['minimum'].power:.4g} {unit} at {result['minimum'].voltage:.2f} V")
            progress.set(f"Scan complete: {len(points)} points. Choose the state for each extreme.")

        scan_button = ttk.Button(frame, text="Start scan", command=start_scan)
        scan_button.grid(row=10, column=0, sticky="w", pady=(12, 0))
        save_button = ttk.Button(frame, text="Save calibration", command=save_scan, state="disabled")
        save_button.grid(row=10, column=1, sticky="w", pady=(12, 0))
        ttk.Button(frame, text="Close", command=close_scan).grid(row=10, column=2, sticky="e", pady=(12, 0))
        fit_to_screen(window)
