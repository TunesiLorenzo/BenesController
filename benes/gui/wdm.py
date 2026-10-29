"""Six-laser WDM plan, per-output OSA scan and the Evaluate Configuration view."""

import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from benes.gui.theme import BG, PANEL, PLOT_MIN_SIZE, POST_SWITCH_SWEEPS, TEXT, WDM_COLORS, WIRE
from benes.hardware import osa
from benes.hardware.osa_output_switch import OUTPUTS, fiber_number
from benes.measurements.dataset_registry import save_measurement
from benes.measurements.wdm_comb import (INPUTS, acquire_averaged_spectrum, create_wdm_folder,
                                         evaluate_configuration,
                                         power_ratio_db, routed_wavelengths,
                                         save_configuration_evaluation, save_wdm_scan, scan_outputs,
                                         simulated_comb_spectrum)
from benes.paths import app_dir


class WDMMixin:
    """Mixed into ``BenesApp``; relies on the state it sets up."""

    def wdm_assignment(self):
        return {str(port): variable.get() for port, variable in self.wdm_wavelength_vars.items()}

    def update_wdm_routing(self):
        """Show where each assigned comb line leaves the network for the current switch states."""
        if not hasattr(self, "wdm_destination_vars"):
            return
        mapping = self.network.mapping()
        lines = []
        for port in INPUTS:
            output = mapping[port - 1] + 1
            self.wdm_destination_vars[port].set(f"→ Out {output} · Fiber {fiber_number(output)}")
            wavelength = self.wdm_wavelength_vars[port].get().strip()
            if wavelength:
                lines.append(f"{wavelength} nm: In {port} → Out {output}")
        self.wdm_route_label.configure(text="WDM COMB    " + "     ".join(lines) if lines else "")
        if hasattr(self, "cal_canvas"):
            self.update_calibration_lasers()

    def wdm_routes(self):
        """(input row, wavelength nm, colour) for every input with an assigned comb line."""
        if not hasattr(self, "wdm_wavelength_vars"):
            return []
        routes = []
        for port in INPUTS:
            try:
                wavelength = float(self.wdm_wavelength_vars[port].get())
            except ValueError:
                continue
            routes.append((port - 1, wavelength, WDM_COLORS[port - 1]))
        return routes

    def save_wdm_plan(self, quiet=False):
        try:
            self.wdm_plan.configure(self.wdm_assignment(), self.wdm_window_var.get(), self.wdm_settle_var.get(),
                                    self.wdm_comb_var.get(), self.wdm_evaluation_sweeps_var.get())
        except (OSError, ValueError) as error:
            if not quiet:
                messagebox.showerror("WDM plan", str(error), parent=self)
            raise
        self.wdm_status_var.set("Wavelength plan saved.")
        return True

    def set_wdm_assignment(self, assignment):
        for port in INPUTS:
            wavelength = assignment[str(port)]
            self.wdm_wavelength_vars[port].set("" if wavelength is None else f"{wavelength:g}")

    def start_configuration_evaluation(self):
        """Evaluate the persisted six-laser plan at every chip output."""
        self.start_wdm_scan(outputs=OUTPUTS, evaluation=True, record_dataset=False)

    def start_wdm_scan(self, outputs=None, evaluation=False, record_dataset=None):
        if self.wdm_running or self.dataset_batch_running:
            return
        switch = self.osa_output_switch
        try:
            if self.mzi_calibration_running:
                raise RuntimeError("Wait for the MZI calibration to finish")
            if self.osa_button.instate(["disabled"]):
                raise RuntimeError("Wait for the current OSA trace read to finish")
            if not evaluation:
                try:
                    self.save_wdm_plan(quiet=True)
                except OSError as error:
                    raise ValueError(f"Could not save the wavelength plan: {error}") from error
            assignment = dict(self.wdm_plan.assignment)
            missing = [str(port) for port in INPUTS if assignment[str(port)] is None]
            if evaluation and missing:
                raise ValueError("Save a wavelength for every input before evaluating the configuration")
            if not any(wavelength is not None for wavelength in assignment.values()):
                raise ValueError("Assign a comb wavelength to at least one input")
            host, port, simulate = self.osa_connection()
            if not switch.connected:
                raise RuntimeError("Connect the OSA output switch first (dry run is available)")
            if outputs is None:
                outputs = [output for output, variable in self.wdm_output_vars.items() if variable.get()]
            else:
                outputs = list(outputs)
            if not outputs:
                raise ValueError("Select at least one output to scan")
            for output in outputs:
                switch.channel_for(output)
            record_dataset = self.wdm_dataset_var.get() if record_dataset is None else record_dataset
            dataset_root = self.dataset_root() if record_dataset else None
            if dataset_root is not None and not (dataset_root / "benes_dataset.json").exists():
                raise ValueError("Initialize the linked dataset in Dataset coverage first")
        except (RuntimeError, ValueError) as error:
            messagebox.showerror("WDM scan unavailable", str(error), parent=self)
            return

        mapping = self.network.mapping()
        permutation = self.active_dataset_permutation()
        routed = routed_wavelengths(mapping, assignment)
        settings = self.wdm_plan.settings
        evaluation_sweeps = settings["evaluation_sweeps"] if evaluation else 1
        comment = self.dataset_comment_var.get()
        context = {
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "permutation": list(permutation),
            "assignment": assignment,
            "wavelength_plan": settings["active_preset"],
            "half_window_nm": settings["half_window_nm"],
            "settle_s": settings["settle_s"],
            "averaged_sweeps_per_output": evaluation_sweeps,
            "jdsu_switch_settle_s": switch.settings["settle_s"],
            "jdsu_channel_map": dict(switch.settings["channel_map"]),
            "switch_states": {name: "cross" if crossed else "bar" for name, crossed in self.network.states.items()},
            "switch_voltages_v": {
                name: self.controller.applied_voltages.get(name, self.controller.settings["voltage"].get(name))
                for name in self.network.states
            },
            "simulated_osa": simulate,
            "dry_run_switch": switch.settings["dry_run"],
        }

        def read(output):
            output_index = outputs.index(output) + 1
            def on_sweep(index, count):
                self.after(0, lambda o=output, oi=output_index, i=index, c=count:
                           self.update_activity("wdm", (oi - 1) * c + i,
                                                detail=f"{'Evaluating' if evaluation else 'Scanning'} output "
                                                       f"{o} ({oi}/{len(outputs)}), sweep {i}/{c}"))
            if simulate:
                return acquire_averaged_spectrum(
                    lambda _: simulated_comb_spectrum(assignment, routed[output][0]),
                    evaluation_sweeps, POST_SWITCH_SWEEPS, on_sweep)
            return acquire_averaged_spectrum(
                lambda fresh: osa.get_spectrum(host, port, fresh_sweeps=fresh),
                evaluation_sweeps, POST_SWITCH_SWEEPS, on_sweep)

        self.wdm_running = True
        self.begin_activity("wdm", "Evaluating configuration" if evaluation else "WDM output scan",
                            len(outputs) * evaluation_sweeps + 1)
        self.wdm_stop_event = threading.Event()
        if hasattr(self, "wdm_start_button"):
            self.wdm_start_button.configure(state="disabled")
        if hasattr(self, "wdm_stop_button"):
            self.wdm_stop_button.configure(state="normal")
        if hasattr(self, "evaluate_button"):
            self.evaluate_button.configure(state="disabled")
        self.set_osa_reading(True)
        self.wdm_status_var.set(("Evaluating" if evaluation else "Scanning") +
                                f" outputs {', '.join(map(str, outputs))}"
                                + (f" ({evaluation_sweeps} sweeps/output)" if evaluation else "") + "...")
        stop_event = self.wdm_stop_event

        markers = [(wavelength, WDM_COLORS[int(port) - 1], f"In {port} · {wavelength:g} nm")
                   for port, wavelength in assignment.items() if wavelength is not None]

        def on_result(result, index, total):
            self.after(0, lambda: self.add_wdm_result(result, index, total))

        def worker():
            results, folder, error = [], None, None
            try:
                results = scan_outputs(switch, read, outputs, mapping, assignment, settings["half_window_nm"],
                                       settings["settle_s"], on_result=on_result, stop_event=stop_event)
                if results:
                    self.after(0, lambda: self.update_activity("wdm", detail="Saving WDM results..."))
                    complete = {**context, "complete": len(results) == len(outputs)}
                    folder = create_wdm_folder(app_dir())
                    files = save_wdm_scan(folder, results, complete, comment)
                    if dataset_root is not None:
                        record = save_measurement(
                            dataset_root, source="OSA WDM", permutation=permutation,
                            input_ports=[result.routed_input for result in results],
                            output_ports=[result.output for result in results], mode="routed_pairs",
                            files=files, comment=comment, extra={"results_folder": str(folder)})
                        save_wdm_scan(record, results, complete, comment)
            except Exception as error_value:
                error = str(error_value)
            self.after(0, lambda: self.finish_wdm_scan(results, folder, error, stop_event.is_set(),
                                                       dataset_root is not None, markers, evaluation, mapping,
                                                       assignment, context))


        threading.Thread(target=worker, daemon=True).start()

    def add_wdm_result(self, result, index, total):
        self.wdm_status_var.set(f"Measured output {result.output} ({index}/{total}).")

    def stop_wdm_scan(self):
        if self.wdm_stop_event is not None:
            self.wdm_stop_event.set()
            self.wdm_status_var.set("Stopping after the current output...")

    def finish_wdm_scan(self, results, folder, error, stopped, recorded, markers=(), evaluation=False,
                        mapping=(), assignment=None, context=None):
        self.wdm_running = False
        job_name = "Evaluation" if evaluation else "WDM scan"
        self.finish_activity("wdm", f"{job_name} {'failed' if error else 'stopped' if stopped else 'complete'}")
        if hasattr(self, "wdm_start_button"):
            self.wdm_start_button.configure(state="normal")
        if hasattr(self, "wdm_stop_button"):
            self.wdm_stop_button.configure(state="disabled")
        if hasattr(self, "evaluate_button"):
            self.evaluate_button.configure(state="normal")
        self.set_osa_reading(False)
        if results:
            self.osa_switch_status_var.set(f"OSA sampling output {results[-1].output} after the WDM scan")
            self.osa_output_var.set(str(results[-1].output))
        if evaluation and results:
            self.show_configuration_evaluation(results, mapping, assignment, folder, context)
        elif self.wdm_plot_var.get():
            for result in results:
                self.show_spectrum(result.wave, result.power, name=f"WDM Out {result.output} (In {result.routed_input})",
                                   restore_buttons=False, output_port=result.output, trimmed=True, markers=markers)
        if error:
            self.wdm_status_var.set(f"WDM scan failed: {error}" + (f" Partial results saved in {folder}." if folder else ""))
            messagebox.showerror("WDM scan failed", error, parent=self)
            return
        prefix = "WDM scan stopped" if stopped else "WDM scan complete"
        if folder is None:
            self.wdm_status_var.set(f"{prefix}; nothing was measured.")
            return
        self.wdm_status_var.set(f"{prefix}: {len(results)} output(s) saved in {folder}"
                                + (" and recorded in the dataset." if recorded else "."))
        if recorded:
            self.analyze_linked_dataset()

    def show_configuration_evaluation(self, results, mapping, assignment, folder=None, context=None):
        """Show the six output spectra next to the configuration power table."""
        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
            from matplotlib.figure import Figure
        except ImportError:
            messagebox.showerror("Evaluation plot unavailable",
                                 "Install matplotlib to display the configuration evaluation.", parent=self)
            return
        summary = evaluate_configuration(results, mapping, assignment)
        window = tk.Toplevel(self)
        window.title("Evaluate Configuration")
        self.fit_dialog_to_screen(window, preferred_width=1280, preferred_height=720)
        figure = Figure(figsize=(12, 6), facecolor=BG)
        spectra = figure.add_subplot(121, facecolor=PANEL)
        table_axis = figure.add_subplot(122, facecolor=PANEL)
        for result in results:
            spectra.plot(result.wave, result.power, linewidth=1.0, label=f"Out {result.output}")
        spectra.set(xlabel="Wavelength [nm]", ylabel="Power [dBm]", title="Six-output OSA scan")
        spectra.title.set_color("white")
        spectra.xaxis.label.set_color("white")
        spectra.yaxis.label.set_color("white")
        spectra.tick_params(colors="white")
        spectra.grid(True, alpha=0.3)
        spectra.legend(loc="best", fontsize=8)
        table_axis.axis("off")
        headers = ["Input", "λ [nm]", "Total\n[dBm]", "Expected", "Transmission\n[dBm]", "Tx / total\n[dB]"] + \
                  [f"Out {port}\nXT / total [dB]" for port in OUTPUTS]
        fmt = lambda value: "—" if value is None else f"{value:.2f}"
        rows = [[f"In {item.input_port}", f"{item.wavelength_nm:.3f}", fmt(item.total_power_dbm),
                 f"Out {item.expected_output}", fmt(item.transmission_dbm),
                 fmt(power_ratio_db(item.normalized_transmission))] +
                ["—" if port == item.expected_output else
                 fmt(power_ratio_db(item.normalized_crosstalk.get(port)))
                for port in OUTPUTS]
                for item in summary]
        table_axis.set_title("Per-wavelength routing evaluation", color=TEXT, fontsize=11, pad=12)
        table = table_axis.table(cellText=rows, colLabels=headers, cellLoc="center", loc="center")
        table.auto_set_font_size(False)
        table.set_fontsize(6.5)
        table.scale(1.05, 1.75)
        for (row, _column), cell in table.get_celld().items():
            cell.set_edgecolor(WIRE)
            cell.set_facecolor("#263650" if row == 0 else PANEL)
            cell.get_text().set_color(TEXT)
        figure.subplots_adjust(left=0.06, right=0.98, bottom=0.12, top=0.9, wspace=0.24)
        canvas = FigureCanvasTkAgg(figure, master=window)
        NavigationToolbar2Tk(canvas, window).update()
        ttk.Button(window, text="Export image, controls and data to results",
                   command=lambda: self.export_configuration_evaluation(
                       folder, figure, summary, context, window),
                   state="normal" if folder is not None and context is not None else "disabled").pack(
                       anchor="e", padx=8, pady=4)
        canvas.get_tk_widget().configure(**PLOT_MIN_SIZE)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        canvas.draw_idle()

    def export_configuration_evaluation(self, folder, figure, summary, context, parent):
        try:
            save_configuration_evaluation(folder, summary, context)
            figure.savefig(folder / "configuration_evaluation.png", dpi=200, facecolor=figure.get_facecolor())
        except (OSError, ValueError) as error:
            messagebox.showerror("Evaluation export failed", str(error), parent=parent)
            return
        messagebox.showinfo("Evaluation export complete",
                            f"Saved the image, control values and evaluation data in:\n{folder}", parent=parent)
