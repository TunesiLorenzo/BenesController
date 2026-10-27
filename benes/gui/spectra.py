"""OSA trace reads and the shared spectrum plot."""

import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, simpledialog, ttk

from benes.gui.theme import PLOT_MIN_SIZE, POST_SWITCH_SWEEPS, WDM_MARKER_GID
from benes.hardware import osa
from benes.hardware.osa_output_switch import fiber_number
from benes.measurements.spectrum_export import write_spectra


class SpectraMixin:
    """Mixed into ``BenesApp``; relies on the state it sets up."""

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
        return f"In {input_port} → Out {output} · Fiber {fiber_number(output)}"

    def start_osa_read(self, output=None):
        try:
            host, port, simulate = self.osa_connection()
            if output is not None:
                output = int(output)
                if not self.osa_output_switch.connected:
                    raise RuntimeError("Connect the OSA output switch first (dry run is available)")
                self.osa_output_switch.channel_for(output)
        except (RuntimeError, ValueError) as error:
            messagebox.showerror("OSA read unavailable", str(error), parent=self)
            return
        self.set_osa_reading(True)
        self.begin_activity("osa_read", f"Reading OSA {'output ' + str(output) if output is not None else 'trace'}...")

        def acquire():
            try:
                if output is not None:
                    self.osa_output_switch.select_output(output)
                data = osa.simulated_spectrum() if simulate else osa.get_spectrum(
                    host, port, fresh_sweeps=0 if output is None else POST_SWITCH_SWEEPS)
            except Exception as error:
                self.after(0, lambda error=error: self.osa_failed(str(error)))
            else:
                self.after(0, lambda: self.finish_osa_read(data, output))

        threading.Thread(target=acquire, daemon=True).start()

    def finish_osa_read(self, data, output):
        if output is not None:
            self.osa_output_var.set(str(output))
            self.osa_switch_status_var.set(
                f"OSA routed to output {output} (fiber {fiber_number(output)}) via JDSU channel "
                f"{self.osa_output_switch.channel_for(output)}; spectrum acquired."
            )
        self.show_spectrum(*data, name=None if output is None else self.osa_trace_name(output),
                           output_port=output)
        self.finish_activity("osa_read", "OSA trace read complete")

    def read_selected_osa_output(self):
        self.start_osa_read(self.osa_switch_output_var.get())

    def read_all_osa_outputs(self):
        try:
            host, port, simulate = self.osa_connection()
            if not self.osa_output_switch.connected:
                raise RuntimeError("Connect the OSA output switch first (dry run is available)")
            outputs = self.osa_output_switch.mapped_outputs()
            if not outputs:
                raise RuntimeError("Map at least one output fiber to a JDSU channel in Selector setup")
        except (RuntimeError, ValueError) as error:
            messagebox.showerror("OSA read unavailable", str(error), parent=self)
            return
        self.set_osa_reading(True)
        self.begin_activity("osa_read", "Reading all OSA outputs...", len(outputs) + 1)
        self.osa_switch_status_var.set(f"Reading OSA spectra from outputs {', '.join(map(str, outputs))}...")

        def acquire_all():
            spectra = []
            try:
                for output in outputs:
                    self.osa_output_switch.select_output(output)
                    spectrum = osa.simulated_spectrum() if simulate else osa.get_spectrum(
                        host, port, fresh_sweeps=POST_SWITCH_SWEEPS)
                    spectra.append((output, spectrum))
                    self.after(0, lambda o=output, i=len(spectra): self.update_activity(
                        "osa_read", i, detail=f"OSA output {o} read ({i}/{len(outputs)})"))
            except Exception as error:
                self.after(0, lambda error=error: self.osa_failed(str(error)))
            else:
                self.after(0, lambda: self.show_all_osa_spectra(spectra))

        threading.Thread(target=acquire_all, daemon=True).start()

    def show_all_osa_spectra(self, spectra):
        self.set_osa_reading(False)
        # Each "Read all" sweep gets its own figure, with every output overlaid in it.
        opened = False
        for output, (wave, power) in spectra:
            opened = self.show_spectrum(wave, power, name=self.osa_trace_name(output), restore_buttons=False,
                                        output_port=output, new_window=not opened) or opened
        if spectra:
            self.osa_output_var.set(str(spectra[-1][0]))
        self.osa_switch_status_var.set(f"Read {len(spectra)} OSA output spectra; export them from the plot window.")
        self.finish_activity("osa_read", f"Read {len(spectra)} OSA outputs")

    def read_spectrum(self):
        """Read the currently connected OSA without changing the output selector."""
        self.start_osa_read()

    def osa_failed(self, error):
        self.finish_activity("osa_read", "OSA read failed")
        self.set_osa_reading(False)
        self.osa_switch_status_var.set(f"OSA read failed: {error}")
        messagebox.showerror("OSA read failed", error, parent=self)

    def show_spectrum(self, wave, power, name=None, restore_buttons=True, output_port=None, trimmed=False,
                      markers=(), new_window=None):
        """Plot an OSA trace; ``markers`` are (wavelength nm, colour, label) drawn as vertical lines.

        ``new_window`` forces (True) or suppresses (False) a new plot window; None follows the plot mode.
        Returns True when the trace was plotted.
        """
        if restore_buttons:
            self.set_osa_reading(False)
        try:
            if not trimmed:
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
        if new_window is None:
            new_window = self.osa_plot_mode.get() == "New window"
        new_window = new_window or self.osa_axes is None
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
        # Overlaid traces share their comb lines, so draw each marker once per plot.
        drawn = {line.get_xdata()[0] for line in self.osa_axes.lines if line.get_gid() == WDM_MARKER_GID}
        for wavelength, color, label in markers:
            if wavelength in drawn:
                continue
            drawn.add(wavelength)
            self.osa_axes.axvline(wavelength, color=color, linestyle="--", linewidth=1, gid=WDM_MARKER_GID,
                                  label="_nolegend_")
            self.osa_axes.text(wavelength, 0.98, label, color=color, fontsize=8, rotation=90, ha="right", va="top",
                               transform=self.osa_axes.get_xaxis_transform())
        self.osa_axes.legend(loc="best")
        self.osa_axes.figure.canvas.draw_idle()
        return True

    def export_spectra(self, axes):
        parent = axes.figure.canvas.get_tk_widget().winfo_toplevel()
        traces = [(line.get_label(), list(line.get_xdata()), list(line.get_ydata())) for line in axes.lines
                  if line.get_visible() and line.get_gid() != WDM_MARKER_GID]
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
