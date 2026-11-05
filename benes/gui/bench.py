"""Bench safety: every scan ends with the MZIs at 0 V and the lasers off; one toggle restores them."""

from tkinter import messagebox, ttk

from benes.hardware.bench_power import power_down, restore, snapshot

ALL_OFF_TEXT = "All off (MZIs 0 V, lasers off)"
RESTORE_TEXT = "Restore previous routing + lasers"


class BenchMixin:
    def build_bench_toggle(self, parent):
        # What "Restore" brings back; None while the bench is not in the all-off state this tab made.
        self.bench_previous = None
        # Bench state when each scan started, by activity key.
        self.bench_before = {}
        self.bench_button = ttk.Button(parent, text=ALL_OFF_TEXT, command=self.toggle_bench)
        return self.bench_button

    def bench_snapshot(self):
        return snapshot(self.controller, self.network.states, self.zero_volt_switches, self.newport_controller)

    def remember_bench(self, key):
        """Call when a scan starts, so its end can offer this routing back."""
        self.bench_before[key] = self.bench_snapshot()

    def power_down_after(self, key, previous=None):
        """Call when a scan ends, however it ended: lasers off, every MZI at 0 V."""
        before = self.bench_before.pop(key, None)
        errors = self.power_down_bench(previous or before)
        if not self.activities:
            self.activity_text.set(self.activity_text.get() + " · MZIs at 0 V, lasers off")
        if errors:
            messagebox.showwarning("Bench not fully off", "After the scan, some outputs could not be turned off:\n\n"
                                   + "\n".join(f"• {error}" for error in errors)
                                   + "\n\nCheck them before leaving the bench.", parent=self)

    def power_down_bench(self, previous):
        # A scan started from the all-off state keeps the routing from before it.
        if previous is not None and not previous.is_off:
            self.bench_previous = previous
        errors = power_down(self.controller, self.network.switches, self.newport_controller)
        if self.controller.connected:
            # 0 V is cross, the chip's unbiased state.
            self.network.states = dict.fromkeys(self.network.switches, True)
            self.zero_volt_switches = set(self.network.switches)
            for name in self.network.switches:
                self.voltage_vars[name].set("0.00")
        self.bench_changed("All off: MZIs at 0 V, laser currents off (TECs on)")
        return errors

    def toggle_bench(self):
        if self.measurement_busy():
            messagebox.showinfo("Bench power", "Wait for the current measurement to finish.", parent=self)
            return
        current = self.bench_snapshot()
        if self.bench_previous is not None and not current.is_off:
            choice = messagebox.askyesnocancel(
                "Restore previous routing",
                "MZIs or lasers were changed since All off.\n\nYes: restore the routing and lasers from before.\n"
                "No: turn the present ones off instead.", parent=self)
            if choice is None:
                return
            if not choice:
                self.bench_previous = None
        if self.bench_previous is None:
            if current.is_off:
                messagebox.showinfo("Bench power", "The MZIs are already at 0 V and no laser is on.", parent=self)
                return
            errors = self.power_down_bench(current)
            if errors:
                messagebox.showwarning("Bench not fully off", "\n".join(errors), parent=self)
            return
        previous, self.bench_previous = self.bench_previous, None
        sent, errors = restore(self.controller, previous, self.newport_controller)
        self.network.states = dict(previous.states)
        self.zero_volt_switches = set(previous.zero)
        for name, voltage in sent.items():
            self.voltage_vars[name].set(f"{voltage:.2f}")
        self.bench_changed(f"Restored {len(sent)} MZI voltage(s) and {len(previous.lasers)} laser(s)")
        if errors:
            messagebox.showwarning("Routing not fully restored", "\n".join(errors), parent=self)

    def bench_changed(self, status):
        self.bench_button.configure(text=ALL_OFF_TEXT if self.bench_previous is None else RESTORE_TEXT)
        if self.newport_controller.connected:
            self.newport_panel.refresh_view(status)
        self.refresh(keep_selection=True)
        self.update_hardware_status(status)
