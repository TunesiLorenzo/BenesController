"""Main window: shared state, notebook and lifecycle. Each tab lives in its own mixin module."""

import json
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from benes.core.network import BenesNetwork
from benes.gui.theme import ACCENT, BG, apply_theme
from benes.gui.widgets import ScrollableFrame
from benes.hardware.laser_bank import LaserBank
from benes.hardware.newport_laser import NewportLaserController, NewportPanel
from benes.hardware.hp8152a import DEFAULT_RESOURCE
from benes.hardware.osa_output_switch import OUTPUTS, OSAOutputSwitch
from benes.hardware.power_meter import ArduinoPowerMeter, RANGE_TRACKING_LABELS
from benes.hardware.voltage_controller import BenesVoltageController
from benes.measurements.wdm_comb import WDMPlan
from benes.gui.calibration import DETECT_OSA, METER_HP, METER_VIA_JDSU, CalibrationMixin
from benes.gui.crosstalk import CrosstalkMixin
from benes.gui.dataset import DatasetMixin
from benes.gui.hardware import HardwareMixin
from benes.gui.impulse import ImpulseMixin
from benes.gui.mapping import MappingMixin
from benes.gui.routing import RoutingMixin
from benes.gui.spectra import SpectraMixin
from benes.gui.wdm import WDMMixin
from benes.paths import app_dir


class BenesApp(RoutingMixin, HardwareMixin, SpectraMixin, MappingMixin, CrosstalkMixin,
               CalibrationMixin, WDMMixin, DatasetMixin, ImpulseMixin, tk.Tk):
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
        self.newport_controller = NewportLaserController()
        self.newport_window = None
        self.laser_bank = LaserBank(self.newport_controller)
        # The lab always drives the real JDSU; Dry run stays available in Selector setup.
        self.osa_output_switch.settings["dry_run"] = False
        self.wdm_plan = WDMPlan()
        self.wdm_running = False
        self.wdm_stop_event = None
        self.dataset_batch_running = False
        self.dataset_batch_stop_event = None
        self.dataset_analysis_lock = threading.Lock()
        self.dataset_analysis_request = 0
        self.scan_settings = {}
        self.crosstalk_running = False
        self.hp_meter_busy = None
        self.hp_stream_stop = None
        self.crosstalk_data = {}
        self.crosstalk_data_source = None
        self.crosstalk_data_unit = None
        self.crosstalk_result_folder = None
        self.active_scan_kind = "Thermal crosstalk scan"
        # Switches clicked to 0 V; they route as cross, the chip's unbiased state.
        self.zero_volt_switches = set()
        self.mzi_calibration_running = False
        self.calibration_stop_event = None
        self.impulse_running = False
        self.impulse_stop_event = None
        self.impulse_trace = None
        self.source = None
        self.destination = None
        self.routes = []
        self.selected_route = 0
        self.permutation_route_states = ()
        self.permutation_preview_states = None
        self.selected_permutation_route = 0
        self.keep_permutation_var = tk.BooleanVar(value=False)
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
        self.osa_switch_resource_var = tk.StringVar(value=self.osa_output_switch.settings["resource"])
        self.osa_switch_dry_run_var = tk.BooleanVar(value=self.osa_output_switch.settings["dry_run"])
        self.osa_switch_settle_var = tk.StringVar(value=str(self.osa_output_switch.settings["settle_s"]))
        channel_map = self.osa_output_switch.settings["channel_map"]
        self.osa_switch_channel_vars = {
            output: tk.StringVar(value="—" if channel_map[str(output)] is None else str(channel_map[str(output)]))
            for output in OUTPUTS
        }
        # Prevents the two linked OSA output pickers from re-triggering each other.
        self._syncing_osa_output = False
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
        self.arduino_jump_guard_var = tk.StringVar(value=f"{self.power_meter.settings['jump_guard_ms']:g}")
        self.arduino_range_tracking_var = tk.StringVar(
            value=RANGE_TRACKING_LABELS[self.power_meter.settings["range_tracking"]])
        self.power_reading_var = tk.StringVar(value="No Arduino reading yet")
        try:
            hp_settings = json.loads((app_dir() / "benes_hp8152a_config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            hp_settings = {}
        self.hp_meter_resource_var = tk.StringVar(value=hp_settings.get("resource", DEFAULT_RESOURCE))
        self.hp_meter_channel_var = tk.StringVar(value=hp_settings.get("channel", "A"))
        self.hp_meter_wavelength_var = tk.StringVar(value=str(hp_settings.get("wavelength_nm", 1550)))
        self.hp_meter_range_var = tk.StringVar(value=hp_settings.get("range", "Auto"))
        self.hp_stream_hz_var = tk.StringVar(value="5")
        # How MZI calibration and the crosstalk matrix read a probe (set in the MZI calibration tab).
        self.detection_method_var = tk.StringVar(value=DETECT_OSA)
        self.detection_meter_var = tk.StringVar(value=METER_HP)
        self.detection_position_var = tk.StringVar(value=METER_VIA_JDSU)
        self.laser_slot_vars = {port: tk.StringVar(value=str(slot))
                                for port, slot in self.laser_bank.settings["slots"].items()}
        self.laser_settle_var = tk.StringVar(value=f"{self.laser_bank.settings['settle_s']:g}")
        apply_theme(self)

        header = ttk.Frame(self, padding=(24, 14, 24, 6))
        header.pack(fill="x")
        ttk.Label(header, text="BENES LAB", foreground=ACCENT, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(header, text="6 × 6 photonic routing", font=("Segoe UI", 22, "bold")).pack(anchor="w")
        ttk.Label(header, text="Route · calibrate · measure", foreground="#9baec6").pack(anchor="w", pady=(2, 0))

        self.activities = {}
        self.activity_text = tk.StringVar(value="Ready")
        activity_strip = ttk.Frame(self, padding=(24, 4, 24, 10))
        activity_strip.pack(fill="x")
        ttk.Label(activity_strip, textvariable=self.activity_text).pack(anchor="w")
        self.activity_bar = ttk.Progressbar(activity_strip, mode="determinate", maximum=1,
                                            style="Activity.Horizontal.TProgressbar")
        self.activity_bar.pack(fill="x", pady=(4, 0))

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        # Each tab fills the window when it fits and scrolls when the window is smaller.
        tabs = []
        for text in ("Routing", "Hardware & OSA", "MZI calibration", "Thermal crosstalk",
                     "Dataset coverage", "Impulse test", "Newport lasers"):
            scroller = ScrollableFrame(self.notebook, background=BG)
            self.notebook.add(scroller, text=text)
            tabs.append(scroller.inner)
        (self.route_tab, self.hardware_tab, self.calibration_tab, self.crosstalk_tab,
         self.dataset_tab, self.impulse_tab, self.newport_tab) = tabs
        self.newport_panel = NewportPanel(self.newport_tab, self.newport_controller,
                                          detach=self.detach_newport)
        self.newport_panel.pack(fill="both", expand=True)

        self.build_routing_tab()
        self.build_hardware_tab()
        self.build_crosstalk_tab()
        self.build_calibration_tab()
        self.build_dataset_tab()
        self.build_impulse_tab()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()
        self.after(200, self.auto_connect_osa_output_switch)

    @staticmethod
    def tab_page(tab):
        """The notebook page that holds a scrollable tab's content frame."""
        return tab.master.master

    def set_tab_state(self, tab, state):
        self.notebook.tab(self.tab_page(tab), state=state)

    def begin_activity(self, key, label, total=None):
        """Show a running job in the fixed status strip; call from the Tk thread."""
        self.activities[key] = {"label": label, "detail": label, "done": 0, "total": total}
        self._render_activity()

    def update_activity(self, key, done=None, total=None, detail=None):
        if key not in self.activities:
            return
        activity = self.activities[key]
        if done is not None:
            activity["done"] = done
        if total is not None:
            activity["total"] = total
        if detail is not None:
            activity["detail"] = detail
        self._render_activity()

    def finish_activity(self, key, detail):
        self.activities.pop(key, None)
        if self.activities:
            self._render_activity()
        else:
            self.activity_bar.stop()
            self.activity_bar.configure(mode="determinate", maximum=1, value=1)
            self.activity_text.set(detail)

    def _render_activity(self):
        if not self.activities:
            return
        activity = next(reversed(self.activities.values()))
        total = activity["total"]
        if total is None:
            if str(self.activity_bar.cget("mode")) != "indeterminate":
                self.activity_bar.configure(mode="indeterminate")
                self.activity_bar.start(12)
        else:
            self.activity_bar.stop()
            self.activity_bar.configure(mode="determinate", maximum=max(1, total),
                                        value=min(activity["done"], total))
        self.activity_text.set(activity["detail"])

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

    def close(self):
        if self.hp_meter_busy == "stream":
            self.stop_hp_stream()
            messagebox.showinfo("HP meter stream", "Stopping the HP meter stream. Close again when it has stopped.", parent=self)
            return
        if self.hp_meter_busy == "test":
            messagebox.showinfo("HP meter test", "Wait for the HP meter test to finish.", parent=self)
            return
        if self.newport_panel.busy:
            messagebox.showinfo("Newport operation", "Wait for the Newport command to finish.", parent=self)
            return
        if self.impulse_running:
            self.stop_impulse()
            return
        if self.crosstalk_running:
            messagebox.showinfo("Scan in progress", "Wait for the crosstalk scan to finish and reset the switches to 0 V.", parent=self)
            return
        if self.wdm_running:
            messagebox.showinfo("WDM scan in progress", "Stop the WDM scan and wait for it to finish first.", parent=self)
            return
        if self.dataset_batch_running:
            messagebox.showinfo("Dataset batch in progress", "Stop the dataset batch and wait for it to finish first.", parent=self)
            return
        if self.mzi_calibration_running:
            messagebox.showinfo("Calibration in progress",
                                "Stop the MZI calibration and wait for every MZI to return to 0 V first.", parent=self)
            return
        self.controller.disconnect()
        self.power_meter.disconnect()
        self.osa_output_switch.disconnect()
        self.newport_controller.disconnect()
        self.destroy()

    def detach_newport(self):
        if self.newport_panel.busy or self.newport_window is not None:
            return
        self.newport_panel.destroy()
        window = tk.Toplevel(self)
        window.title("Newport 9016 laser controller")
        window.geometry("980x760")
        window.minsize(620, 400)
        scroller = ScrollableFrame(window, background=BG)
        scroller.pack(fill="both", expand=True)
        self.newport_window = window
        self.newport_panel = NewportPanel(scroller.inner, self.newport_controller,
                                          attach=self.attach_newport)
        self.newport_panel.pack(fill="both", expand=True)
        window.protocol("WM_DELETE_WINDOW", self.attach_newport)

    def attach_newport(self):
        if self.newport_panel.busy or self.newport_window is None:
            return
        self.newport_panel.destroy()
        self.newport_window.destroy()
        self.newport_window = None
        self.newport_panel = NewportPanel(self.newport_tab, self.newport_controller,
                                          detach=self.detach_newport)
        self.newport_panel.pack(fill="both", expand=True)
