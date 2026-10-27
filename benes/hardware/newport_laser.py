"""Newport 9016 GPIB output controls for the 16-slot Beneš laser rack."""

from __future__ import annotations

import threading
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
                        else:
                            values[kind] = self._read(slot, kind)
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
                actual if kind == "tec" else old.tec)
            if actual != enabled:
                raise RuntimeError(f"Module {slot} {kind} did not turn {'on' if enabled else 'off'}")
            return actual

    def all_off(self):
        """Attempt every laser and TEC output even if one channel fails."""
        with self._lock:
            if not self.connected:
                raise RuntimeError("Connect the Newport controller first")
            errors = []
            with self._preserve_channels():
                for kind in ("laser", "tec"):
                    for slot, module in enumerate(self.modules, 1):
                        try:
                            self._subchannel(module.model, kind)
                        except ValueError:
                            continue
                        try:
                            channel = self._select(slot, kind)
                            self.instrument.write(f"{channel}:OUTPUT 0")
                            if self._read(slot, kind):
                                raise RuntimeError("output still on")
                            old = self.modules[slot - 1]
                            self.modules[slot - 1] = ModuleState(
                                old.model, False if kind == "laser" else old.laser,
                                False if kind == "tec" else old.tec)
                        except Exception as error:
                            errors.append(f"slot {slot} {kind}: {error}")
            return errors


class NewportPanel(ttk.Frame):
    """A view that can be rebuilt inside the notebook or a separate window."""

    def __init__(self, parent, controller, detach=None, attach=None):
        super().__init__(parent, padding=16)
        self.controller = controller
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

        ttk.Label(self, text="Laser current and TEC temperature outputs are independent. "
                  "ON uses the setpoints already stored in the controller.", wraplength=850).pack(anchor="w")
        self.all_off_button = ttk.Button(self, text="ALL OFF · laser and TEC", command=self.all_off)
        self.all_off_button.pack(anchor="w", pady=(10, 8))
        ttk.Label(self, textvariable=self.status_var, wraplength=850).pack(anchor="w", pady=(0, 8))

        grid = ttk.Frame(self)
        grid.pack(fill="x")
        for col, label in enumerate(("Slot", "Module", "Laser current", "", "TEC temperature", "")):
            ttk.Label(grid, text=label, font=("Segoe UI", 10, "bold")).grid(
                row=0, column=col, sticky="w", padx=(4, 12), pady=4)
        for slot in range(1, SLOT_COUNT + 1):
            model = ttk.Label(grid, width=20)
            laser_state = ttk.Label(grid, width=8)
            tec_state = ttk.Label(grid, width=8)
            laser_button = ttk.Button(grid, width=11, command=lambda s=slot: self.toggle(s, "laser"))
            tec_button = ttk.Button(grid, width=11, command=lambda s=slot: self.toggle(s, "tec"))
            ttk.Label(grid, text=f"{slot:02d}").grid(row=slot, column=0, sticky="w", padx=4, pady=2)
            model.grid(row=slot, column=1, sticky="w", padx=(4, 12))
            laser_state.grid(row=slot, column=2, sticky="w")
            laser_button.grid(row=slot, column=3, padx=(4, 18), pady=2)
            tec_state.grid(row=slot, column=4, sticky="w")
            tec_button.grid(row=slot, column=5, padx=4, pady=2)
            self.rows.append((model, laser_state, laser_button, tec_state, tec_button))
        self.render()

    def render(self):
        connected = self.controller.connected
        self.connect_button.configure(state="disabled" if self.busy or connected else "normal")
        self.disconnect_button.configure(state="disabled" if self.busy or not connected else "normal")
        self.refresh_button.configure(state="normal" if connected and not self.busy else "disabled")
        self.all_off_button.configure(state="normal" if connected and not self.busy else "disabled")
        for module, widgets in zip(self.controller.modules, self.rows):
            model, laser_state, laser_button, tec_state, tec_button = widgets
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

    def connect_controller(self):
        resource = self.resource_var.get().strip()
        def operation():
            identity = self.controller.connect(resource)
            return identity, self.controller.read_states()
        self._run("Connecting and reading outputs…", operation,
                  lambda result: self.status_var.set(f"Connected: {result[0]} · {self.controller.resource}"))

    def disconnect_controller(self):
        self._run("Disconnecting…", self.controller.disconnect,
                  lambda _: self.status_var.set("Disconnected"))

    def refresh(self):
        self._run("Reading outputs…", self.controller.read_states,
                  lambda _: self.status_var.set("Output states refreshed"))

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
                self.status_var.set(f"ALL OFF incomplete: {len(errors)} output(s) failed")
                messagebox.showerror("Newport 9016", "Some outputs could not be confirmed off:\n" +
                                     "\n".join(errors), parent=self.winfo_toplevel())
            else:
                self.status_var.set("All 16 modules: laser and TEC outputs OFF")
        self._run("Turning all laser outputs off, then all TEC outputs off…",
                  self.controller.all_off, finish)
