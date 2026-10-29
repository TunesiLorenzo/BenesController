"""Newport 9016 GPIB output controls for the 16-slot Beneš laser rack."""

from __future__ import annotations

import math
import threading
import time
import tkinter as tk
from contextlib import contextmanager
from dataclasses import dataclass
from tkinter import messagebox, ttk


DEFAULT_RESOURCE = "GPIB0::19::INSTR"
SLOT_COUNT = 16


@dataclass(frozen=True)
class ModuleState:
    model: str = ""
    laser: bool | None = None
    tec: bool | None = None
    laser_setpoint: float | None = None
    tec_setpoint: float | None = None


class NewportLaserController:
    """Serialize all commands because LAS:CHAN and TEC:CHAN are shared state."""

    def __init__(self, resource=DEFAULT_RESOURCE, resource_manager_factory=None):
        self.resource = resource
        self._factory = resource_manager_factory
        self.manager = None
        self.instrument = None
        self._lock = threading.RLock()
        self.modules = [ModuleState() for _ in range(SLOT_COUNT)]

    @property
    def connected(self):
        return self.instrument is not None

    def connect(self, resource=None):
        with self._lock:
            if self.connected:
                self.disconnect()
            if resource is not None:
                self.resource = str(resource).strip()
            if not self.resource:
                raise ValueError("Enter a VISA resource")
            if self._factory is None:
                try:
                    import pyvisa
                except ImportError as error:
                    raise RuntimeError("Install PyVISA and NI-VISA to connect the Newport controller") from error
                factory = pyvisa.ResourceManager
            else:
                factory = self._factory
            manager = factory()
            instrument = None
            try:
                instrument = manager.open_resource(self.resource)
                instrument.timeout = 2500
                instrument.write_termination = "\r\n"
                instrument.read_termination = "\r\n"
                identity = instrument.query("*IDN?").strip()
                if "9016" not in identity.upper():
                    raise RuntimeError(f"Expected Newport 9016, found {identity!r}")
                equipment = instrument.query("EQUIP?").strip().split(",")
                if not equipment or "9016" not in equipment[0].upper():
                    raise RuntimeError(f"Unexpected module list: {equipment!r}")
                if len(equipment) - 1 != SLOT_COUNT:
                    raise RuntimeError(f"Expected {SLOT_COUNT} slots, received {len(equipment) - 1}")
            except Exception:
                if instrument is not None:
                    instrument.close()
                manager.close()
                raise
            self.manager, self.instrument = manager, instrument
            self.modules = [ModuleState(model.strip()) for model in equipment[1:]]
            return identity

    def disconnect(self):
        with self._lock:
            try:
                if self.instrument is not None:
                    self.instrument.close()
            finally:
                if self.manager is not None:
                    self.manager.close()
                self.instrument = self.manager = None
                self.modules = [ModuleState() for _ in range(SLOT_COUNT)]

    def _module(self, slot):
        if not self.connected:
            raise RuntimeError("Connect the Newport controller first")
        if not isinstance(slot, int) or not 1 <= slot <= SLOT_COUNT:
            raise ValueError("Module slot must be 1–16")
        module = self.modules[slot - 1]
        if not module.model:
            raise ValueError(f"Module slot {slot} is empty")
        return module

    @staticmethod
    def _subchannel(model, kind):
        description = model.upper()
        if kind == "laser" and ("LDD" in description or "LAS" in description):
            return 1
        if kind == "tec" and "TEC" in description:
            return 2 if ("LDD" in description or "LAS" in description) else 1
        raise ValueError(f"{model} has no {kind} output")

    @contextmanager
    def _preserve_channels(self):
        instrument = self.instrument
        original = {kind: instrument.query(f"{kind}:CHAN?").strip()
                    for kind in ("LAS", "TEC")}
        try:
            yield
        finally:
            for kind, channel in original.items():
                if channel and channel.split(",", 1)[0] != "0":
                    instrument.write(f"{kind}:CHAN {channel}")

    def _select(self, slot, kind):
        module = self._module(slot)
        channel = "LAS" if kind == "laser" else "TEC"
        subchannel = self._subchannel(module.model, kind)
        target = f"{slot},{subchannel}"
        self.instrument.write(f"{channel}:CHAN {target}")
        actual = self.instrument.query(f"{channel}:CHAN?").strip()
        if actual != target:
            raise RuntimeError(f"{channel} channel {target} was not selected (got {actual})")
        return channel

    def _read(self, slot, kind):
        channel = self._select(slot, kind)
        reply = self.instrument.query(f"{channel}:OUTPUT?").strip()
        if reply not in ("0", "1"):
            raise RuntimeError(f"Unexpected {channel} output state: {reply!r}")
        return reply == "1"

    def _read_setpoint(self, slot, kind):
        channel = self._select(slot, kind)
        command = "SET:LDI?" if kind == "laser" else "SET:T?"
        return float(self.instrument.query(f"{channel}:{command}").strip())

    def read_states(self):
        with self._lock:
            if not self.connected:
                raise RuntimeError("Connect the Newport controller first")
            states = []
            with self._preserve_channels():
                for slot, module in enumerate(self.modules, 1):
                    values = {}
                    for kind in ("laser", "tec"):
                        try:
                            self._subchannel(module.model, kind)
                        except ValueError:
                            values[kind] = None
                            values[f"{kind}_setpoint"] = None
                        else:
                            values[kind] = self._read(slot, kind)
                            values[f"{kind}_setpoint"] = self._read_setpoint(slot, kind)
                    states.append(ModuleState(module.model, **values))
            self.modules = states
            return list(states)

    def set_output(self, slot, kind, enabled):
        if kind not in ("laser", "tec") or type(enabled) is not bool:
            raise ValueError("Choose laser or TEC and an on/off state")
        with self._lock:
            self._module(slot)
            with self._preserve_channels():
                channel = self._select(slot, kind)
                self.instrument.write(f"{channel}:OUTPUT {int(enabled)}")
                actual = self._read(slot, kind)
            old = self.modules[slot - 1]
            self.modules[slot - 1] = ModuleState(
                old.model, actual if kind == "laser" else old.laser,
                actual if kind == "tec" else old.tec,
                old.laser_setpoint, old.tec_setpoint)
            if actual != enabled:
                raise RuntimeError(f"Module {slot} {kind} did not turn {'on' if enabled else 'off'}")
            return actual

    def set_setpoint(self, slot, kind, value):
        if kind not in ("laser", "tec"):
            raise ValueError("Choose laser current or TEC temperature")
        try:
            value = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError("Enter a numeric setpoint") from error
        if not math.isfinite(value) or (kind == "laser" and value < 0):
            raise ValueError("Enter a finite setpoint; laser current cannot be negative")
        with self._lock:
            self._module(slot)
            with self._preserve_channels():
                channel = self._select(slot, kind)
                mode = self.instrument.query(f"{channel}:MODE?").strip().upper()
                desired_modes = ("I", "ICW", "ILBW", "IHBW", "LDI") if kind == "laser" else ("T",)
                if mode not in desired_modes:
                    if self._read(slot, kind):
                        unit = "current" if kind == "laser" else "temperature"
                        raise RuntimeError(
                            f"Slot {slot} {kind} is ON in {mode} mode. Turn it OFF before "
                            f"setting {unit}; changing mode would turn the output off.")
                    self._select(slot, kind)
                    self.instrument.write(f"{channel}:MODE:{'ILBW' if kind == 'laser' else 'T'}")
                    selected_mode = self.instrument.query(f"{channel}:MODE?").strip().upper()
                    if selected_mode not in desired_modes:
                        raise RuntimeError(f"Slot {slot} {kind} did not enter the required mode "
                                           f"(controller reports {selected_mode})")
                    mode = selected_mode
                # The 9016 reports invalid parameters through an error queue.
                # Drain old entries so the next response belongs to this write.
                for _ in range(10):
                    if self.instrument.query("ERRSTR?").strip() in ("", "0"):
                        break
                command = "LDI" if kind == "laser" else "T"
                write_command = f"{channel}:{command} {value:.6f}"
                attempts = []
                self.instrument.write(write_command)
                error = self.instrument.query("ERRSTR?").strip()
                attempts.append((write_command + " (CR+LF)", error))
                if error.lstrip().startswith("106"):
                    # The manual permits LF as a message terminator. Send the
                    # same command without PyVISA appending CR+LF.
                    self.instrument.write_raw((write_command + "\n").encode("ascii"))
                    error = self.instrument.query("ERRSTR?").strip()
                    attempts.append((write_command + " (LF only)", error))
                tolerance = 0.05 if kind == "laser" else 0.1
                for attempt in range(4):
                    query = "SET:LDI?" if kind == "laser" else "SET:T?"
                    actual = float(self.instrument.query(f"{channel}:{query}").strip())
                    if math.isclose(actual, value, rel_tol=0, abs_tol=tolerance):
                        break
                    if attempt < 3:
                        time.sleep(0.1)
                else:
                    unit = "mA" if kind == "laser" else "°C"
                    read_error = self.instrument.query("ERRSTR?").strip()
                    details = "".join(
                        f" Sent: {candidate}. Error after write: {error}."
                        for candidate, error in attempts)
                    if read_error not in ("", "0"):
                        details += f" Error after readback: {read_error}."
                    if kind == "laser":
                        limit = self.instrument.query("LAS:LIM:LDI?").strip()
                        details += f" Current limit: {limit} mA."
                    raise RuntimeError(
                        f"Slot {slot} {kind} setpoint was not accepted: requested {value:g} {unit}, "
                        f"controller reports {actual:g} {unit}. Mode: {mode}." + details)
            old = self.modules[slot - 1]
            self.modules[slot - 1] = ModuleState(
                old.model, old.laser, old.tec,
                actual if kind == "laser" else old.laser_setpoint,
                actual if kind == "tec" else old.tec_setpoint)
            return actual

    def all_off(self):
        """Attempt every laser output even if one channel fails; leave TEC alone."""
        with self._lock:
            if not self.connected:
                raise RuntimeError("Connect the Newport controller first")
            errors = []
            with self._preserve_channels():
                for slot, module in enumerate(self.modules, 1):
                    try:
                        self._subchannel(module.model, "laser")
                    except ValueError:
                        continue
                    try:
                        channel = self._select(slot, "laser")
                        self.instrument.write(f"{channel}:OUTPUT 0")
                        if self._read(slot, "laser"):
                            raise RuntimeError("output still on")
                        old = self.modules[slot - 1]
                        self.modules[slot - 1] = ModuleState(
                            old.model, False, old.tec, old.laser_setpoint, old.tec_setpoint)
                    except Exception as error:
                        errors.append(f"slot {slot} laser: {error}")
            return errors


class NewportPanel(ttk.Frame):
    """A view that can be rebuilt inside the notebook or a separate window."""

    def __init__(self, parent, controller, detach=None, attach=None, on_change=None):
        super().__init__(parent, padding=16)
        self.controller = controller
        # Called after every finished operation, so other views can show the new output states.
        self.on_change = on_change
        self.detach = detach
        self.attach = attach
        self.resource_var = tk.StringVar(value=controller.resource)
        self.status_var = tk.StringVar(value="Not connected")
        self.busy = False
        self.rows = []
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 10))
        ttk.Label(bar, text="Newport 9016 · VISA resource").pack(side="left")
        ttk.Entry(bar, textvariable=self.resource_var, width=22).pack(side="left", padx=8)
        self.connect_button = ttk.Button(bar, text="Connect", command=self.connect_controller)
        self.connect_button.pack(side="left")
        self.disconnect_button = ttk.Button(bar, text="Disconnect", command=self.disconnect_controller)
        self.disconnect_button.pack(side="left", padx=5)
        self.refresh_button = ttk.Button(bar, text="Refresh", command=self.refresh)
        self.refresh_button.pack(side="left", padx=5)
        if detach or attach:
            ttk.Button(bar, text="Detach" if detach else "Attach",
                       command=detach or attach).pack(side="right")

        ttk.Label(self, text="Set laser current (mA) and TEC temperature (°C) for each module, "
                  "then use the separate output switches. Setpoints apply in constant current "
                  "and constant temperature modes respectively.", wraplength=1000).pack(anchor="w")
        self.all_off_button = ttk.Button(self, text="ALL CURRENT OFF · leave TEC on", command=self.all_off)
        self.all_off_button.pack(anchor="w", pady=(10, 8))
        ttk.Label(self, textvariable=self.status_var, wraplength=850).pack(anchor="w", pady=(0, 8))

        grid = ttk.Frame(self)
        grid.pack(fill="x")
        self._active_color = "#1f6b4e"
        self._idle_color = ttk.Style(self).lookup("TLabel", "background") or "#101827"
        ttk.Style(self).configure("NewportActive.TLabel", background=self._active_color,
                                  foreground="#f0fff5", font=("Segoe UI", 10, "bold"))
        for col, label in enumerate(("Slot", "Module", "Laser", "", "Current (mA)", "",
                                     "TEC", "", "Temperature (°C)", "")):
            ttk.Label(grid, text=label, font=("Segoe UI", 10, "bold")).grid(
                row=0, column=col, sticky="w", padx=(4, 12), pady=4)
        for slot in range(1, SLOT_COUNT + 1):
            module_highlight = tk.Frame(grid, background=self._idle_color)
            laser_highlight = tk.Frame(grid, background=self._idle_color)
            tec_highlight = tk.Frame(grid, background=self._idle_color)
            module_highlight.grid(row=slot, column=0, columnspan=2, sticky="nsew")
            laser_highlight.grid(row=slot, column=2, columnspan=4, sticky="nsew")
            tec_highlight.grid(row=slot, column=6, columnspan=4, sticky="nsew")
            slot_label = ttk.Label(grid, text=f"{slot:02d}")
            model = ttk.Label(grid, width=20)
            laser_state = ttk.Label(grid, width=8)
            tec_state = ttk.Label(grid, width=8)
            laser_button = ttk.Button(grid, width=11, command=lambda s=slot: self.toggle(s, "laser"))
            tec_button = ttk.Button(grid, width=11, command=lambda s=slot: self.toggle(s, "tec"))
            laser_var = tk.StringVar()
            tec_var = tk.StringVar()
            laser_entry = ttk.Entry(grid, textvariable=laser_var, width=10)
            tec_entry = ttk.Entry(grid, textvariable=tec_var, width=10)
            laser_set = ttk.Button(grid, text="Set", width=5,
                                   command=lambda s=slot, v=laser_var: self.set_setpoint(s, "laser", v.get()))
            tec_set = ttk.Button(grid, text="Set", width=5,
                                 command=lambda s=slot, v=tec_var: self.set_setpoint(s, "tec", v.get()))
            slot_label.grid(row=slot, column=0, sticky="w", padx=4, pady=2)
            model.grid(row=slot, column=1, sticky="w", padx=(4, 12))
            laser_state.grid(row=slot, column=2, sticky="w")
            laser_button.grid(row=slot, column=3, padx=(4, 18), pady=2)
            laser_entry.grid(row=slot, column=4, padx=(0, 4))
            laser_set.grid(row=slot, column=5, padx=(0, 18))
            tec_state.grid(row=slot, column=6, sticky="w")
            tec_button.grid(row=slot, column=7, padx=(4, 18), pady=2)
            tec_entry.grid(row=slot, column=8, padx=(0, 4))
            tec_set.grid(row=slot, column=9, padx=4)
            self.rows.append((model, laser_state, laser_button, laser_var, laser_entry,
                              laser_set, tec_state, tec_button, tec_var, tec_entry, tec_set,
                              module_highlight, laser_highlight, tec_highlight, slot_label))
        self._show_setpoints()
        self.render()

    def render(self):
        connected = self.controller.connected
        self.connect_button.configure(state="disabled" if self.busy or connected else "normal")
        self.disconnect_button.configure(state="disabled" if self.busy or not connected else "normal")
        self.refresh_button.configure(state="normal" if connected and not self.busy else "disabled")
        self.all_off_button.configure(state="normal" if connected and not self.busy else "disabled")
        for module, widgets in zip(self.controller.modules, self.rows):
            (model, laser_state, laser_button, _, laser_entry, laser_set,
             tec_state, tec_button, _, tec_entry, tec_set,
             module_highlight, laser_highlight, tec_highlight, slot_label) = widgets
            laser_on = connected and module.laser is True
            tec_on = connected and module.tec is True
            for highlight, active in ((module_highlight, laser_on or tec_on),
                                      (laser_highlight, laser_on), (tec_highlight, tec_on)):
                highlight.configure(background=self._active_color if active else self._idle_color)
            for label, active in ((slot_label, laser_on or tec_on),
                                  (model, laser_on or tec_on),
                                  (laser_state, laser_on), (tec_state, tec_on)):
                label.configure(style="NewportActive.TLabel" if active else "TLabel")
            model.configure(text=module.model or ("Empty" if connected else "—"))
            for kind, state_label, button in (("laser", laser_state, laser_button),
                                              ("tec", tec_state, tec_button)):
                available = connected and not self.busy and module.model and (
                    "LDD" in module.model.upper() or "LAS" in module.model.upper()
                    if kind == "laser" else "TEC" in module.model.upper())
                value = getattr(module, kind)
                state_label.configure(text="ON" if value is True else "OFF" if value is False
                                      else "—" if not available and not module.model else "?")
                button.configure(text="Turn OFF" if value is True else "Turn ON",
                                 state="normal" if available and value is not None else "disabled")
                entry, set_button = ((laser_entry, laser_set) if kind == "laser"
                                     else (tec_entry, tec_set))
                entry.configure(state="normal" if available else "disabled")
                set_button.configure(state="normal" if available else "disabled")

    def _show_setpoints(self):
        for module, widgets in zip(self.controller.modules, self.rows):
            for kind, variable in (("laser", widgets[3]), ("tec", widgets[8])):
                value = getattr(module, f"{kind}_setpoint")
                variable.set(f"{value:g}" if value is not None else "")

    def _run(self, message, operation, success):
        if self.busy:
            return
        self.busy = True
        self.status_var.set(message)
        self.render()

        def worker():
            try:
                result, error = operation(), None
            except Exception as caught:
                result, error = None, caught

            def finish():
                self.busy = False
                self.render()
                if self.on_change is not None:
                    self.on_change()
                if error is not None:
                    self.status_var.set(str(error))
                    messagebox.showerror("Newport 9016", str(error), parent=self.winfo_toplevel())
                else:
                    success(result)

            try:
                self.after(0, finish)
            except RuntimeError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def refresh_view(self, status):
        """Show the controller's state after it was changed from outside this panel."""
        self._show_setpoints()
        self.status_var.set(status)
        self.render()

    def connect_controller(self):
        resource = self.resource_var.get().strip()
        def operation():
            identity = self.controller.connect(resource)
            return identity, self.controller.read_states()
        def finish(result):
            self._show_setpoints()
            self.status_var.set(f"Connected: {result[0]} · {self.controller.resource}")
        self._run("Connecting and reading outputs and setpoints…", operation, finish)

    def disconnect_controller(self):
        def finish(_):
            self._show_setpoints()
            self.status_var.set("Disconnected")
        self._run("Disconnecting…", self.controller.disconnect, finish)

    def refresh(self):
        def finish(_):
            self._show_setpoints()
            self.status_var.set("Output states and setpoints refreshed")
        self._run("Reading outputs and setpoints…", self.controller.read_states, finish)

    def set_setpoint(self, slot, kind, text):
        try:
            value = float(text)
            if not math.isfinite(value) or (kind == "laser" and value < 0):
                raise ValueError
        except ValueError:
            unit = "mA" if kind == "laser" else "°C"
            messagebox.showerror("Newport 9016", f"Enter a valid {kind} setpoint in {unit}.",
                                 parent=self.winfo_toplevel())
            return
        unit = "mA" if kind == "laser" else "°C"
        def finish(actual):
            self._show_setpoints()
            self.status_var.set(f"Slot {slot} {kind} setpoint: {actual:g} {unit}")
        self._run(f"Setting slot {slot} {kind} setpoint…",
                  lambda: self.controller.set_setpoint(slot, kind, value), finish)

    def toggle(self, slot, kind):
        current = getattr(self.controller.modules[slot - 1], kind)
        if current is None:
            return
        desired = not current
        self._run(f"Setting slot {slot} {kind} {'ON' if desired else 'OFF'}…",
                  lambda: self.controller.set_output(slot, kind, desired),
                  lambda _: self.status_var.set(f"Slot {slot} {kind}: {'ON' if desired else 'OFF'}"))

    def all_off(self):
        def finish(errors):
            if errors:
                self.status_var.set(f"ALL CURRENT OFF incomplete: {len(errors)} output(s) failed")
                messagebox.showerror("Newport 9016", "Some outputs could not be confirmed off:\n" +
                                     "\n".join(errors), parent=self.winfo_toplevel())
            else:
                self.status_var.set("All laser current outputs OFF; TEC outputs unchanged")
        self._run("Turning all laser current outputs off…",
                  self.controller.all_off, finish)
