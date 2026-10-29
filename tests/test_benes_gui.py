import unittest
import math
from types import SimpleNamespace
from unittest.mock import patch

from benes.gui.app import BenesApp
from benes.gui.calibration import CalibrationMixin
from benes.gui.crosstalk import CrosstalkMixin
from benes.core.network import BenesNetwork
from benes.hardware.power_meter import PowerReading
from benes.measurements.arduino_hp_range import BNCReference
from benes.measurements.mzi_characterization import SweepPoint


class ActivityProgressTests(unittest.TestCase):
    def test_route_arduino_readout_ignores_stale_result_after_measurement_starts(self):
        shown = []
        available = [True]
        app = SimpleNamespace(_route_arduino_busy=True, _route_arduino_last_time=None,
                              route_arduino_available=lambda: available[0],
                              bnc_reference=BNCReference(40, -45, -40, "GPIB0::23::INSTR", 1),
                              power_meter=SimpleNamespace(ceiling=80.0),
                              route_arduino_var=SimpleNamespace(set=shown.append))
        reading = PowerReading(2.5, "mW", 42.0, 100)
        BenesApp.finish_route_arduino(app, reading, -40, None)
        self.assertEqual(shown, ["Arduino: -44.79 dBm · A0 42.00 ADC · HP range -40 dBm"])
        available[0] = False
        BenesApp.finish_route_arduino(app, PowerReading(9.0, "mW", 90.0, 100), -40, None)
        self.assertEqual(len(shown), 1)
        self.assertFalse(app._route_arduino_busy)

    def test_hp_connection_is_borrowed_without_closing_it(self):
        calls = []
        meter = SimpleNamespace(
            configure=lambda *args: calls.append(("configure", args)),
            disconnect=lambda: calls.append(("disconnect",)))
        saved = {"resource": "GPIB0::23::INSTR", "channel": "A",
                 "wavelength_nm": 1550.0, "range": "Auto"}
        changed = {**saved, "range": "-30"}
        app = SimpleNamespace(hp_meter_session=meter, hp_meter_session_options=saved,
                              configure_hp_meter=CrosstalkMixin.configure_hp_meter)
        borrowed, release = CrosstalkMixin.acquire_hp_meter(app, changed)
        self.assertIs(borrowed, meter)
        release()
        self.assertEqual(calls, [("configure", (1, 1550.0, -30)),
                                 ("configure", (1, 1550.0, None))])
        borrowed, release = CrosstalkMixin.acquire_hp_meter(app, saved, configure=False)
        self.assertIs(borrowed, meter)
        self.assertEqual(len(calls), 2)
        release()
        self.assertEqual(calls[-1], ("configure", (1, 1550.0, None)))
        with self.assertRaisesRegex(ValueError, "Disconnect the HP 8152A"):
            CrosstalkMixin.acquire_hp_meter(app, {**saved, "resource": "GPIB0::24::INSTR"})
        with self.assertRaisesRegex(ValueError, "Disconnect and reconnect"):
            CrosstalkMixin.acquire_hp_meter(app, changed, configure=False)

    def test_arduino_log_plot_keeps_linear_sample_values(self):
        arduino = SimpleNamespace(cal_unit="mW", cal_arduino_log=True)
        hp = SimpleNamespace(cal_unit="mW", cal_arduino_log=False)
        self.assertEqual(CalibrationMixin.plotted_power(arduino, 0.01), 0.01)
        self.assertAlmostEqual(CalibrationMixin.plotted_power(hp, 0.01), -20.0)

    def test_hp_bnc_rescan_keeps_initial_plot_points(self):
        class Line:
            def __init__(self):
                self.data = None

            def set_data(self, x, y):
                self.data = (list(x), list(y))

        original = Line()
        adjusted = Line()
        plot = {"points": [], "reconstructed_points": [], "power_line": original,
                "reconstructed_line": adjusted, "reference_line": None, "result": None, "markers": [],
                "axes": [SimpleNamespace(relim=lambda: None, autoscale_view=lambda: None,
                                         set_xlabel=lambda label: None)],
                "canvas": SimpleNamespace(draw_idle=lambda: None)}
        app = SimpleNamespace(cal_plot_items={"S2.1": plot}, cal_mzi_finished=0,
                              cal_sweep_total=2, cal_unit="ADC",
                              update_activity=lambda *args, **kwargs: None,
                              cal_sweep_progress=SimpleNamespace(configure=lambda **kwargs: None),
                              cal_sweep_progress_var=SimpleNamespace(set=lambda value: None),
                              cal_status_var=SimpleNamespace(set=lambda value: None),
                              plotted_power=lambda value: value,
                              calibration_x=lambda name, voltages: (list(voltages), "Voltage [V]"))
        app.draw_calibration_sweep = lambda name: CalibrationMixin.draw_calibration_sweep(app, name)
        for index, value in enumerate((88.0, 4.5), 1):
            CalibrationMixin.calibration_point(app, "S2.1", index, 2, SweepPoint(index, value))
        CalibrationMixin.calibration_point(app, "S2.1", 1, 2, SweepPoint(1, 96.0))
        self.assertEqual([point.power for point in plot["points"]], [88.0, 4.5])
        self.assertEqual(original.data[1], [88.0, 4.5])
        self.assertEqual(adjusted.data[1], [96.0, 4.5])

    def test_shows_counted_work_and_restores_another_running_job(self):
        class FakeBar:
            def __init__(self):
                self.options = {"mode": "determinate", "value": 0}

            def configure(self, **options):
                self.options.update(options)

            def cget(self, key):
                return self.options[key]

            def start(self, _interval):
                self.options["running"] = True

            def stop(self):
                self.options["running"] = False

        text = SimpleNamespace(value="", set=lambda value: setattr(text, "value", value))
        app = SimpleNamespace(activities={}, activity_text=text, activity_bar=FakeBar())
        app._render_activity = lambda: BenesApp._render_activity(app)
        BenesApp.begin_activity(app, "evaluation", "Evaluating", 61)
        BenesApp.update_activity(app, "evaluation", 20, detail="Output 2, sweep 10/10")
        self.assertEqual(app.activity_bar.options["value"], 20)
        self.assertEqual(text.value, "Output 2, sweep 10/10")
        BenesApp.begin_activity(app, "read", "Reading power")
        self.assertEqual(app.activity_bar.options["mode"], "indeterminate")
        BenesApp.finish_activity(app, "read", "Power read complete")
        self.assertEqual(app.activity_bar.options["mode"], "determinate")
        self.assertEqual(app.activity_bar.options["value"], 20)
        BenesApp.finish_activity(app, "evaluation", "Evaluation complete")
        self.assertEqual(text.value, "Evaluation complete")


class PathProgressTests(unittest.TestCase):
    def test_uncalibrated_cross_path_is_driven_to_zero(self):
        sent = []
        network = BenesNetwork()
        app = SimpleNamespace(
            controller=SimpleNamespace(
                apply_calibrated_state=lambda name, crossed: None,
                drive_voltage=lambda name, voltage: sent.append((name, voltage)) or voltage),
            network=network, zero_volt_switches=set(),
            voltage_vars={"S1.1": SimpleNamespace(set=lambda value: None)},
            keep_permutation_var=SimpleNamespace(get=lambda: False),
            update_hardware_status=lambda value: None)
        self.assertTrue(BenesApp.drive_route_states(app, {"S1.1": True}, ["S1.1"]))
        self.assertEqual(sent, [("S1.1", 0.0)])
        self.assertIn("S1.1", app.zero_volt_switches)

    def test_applying_path_zeros_every_mzi_outside_that_path(self):
        network = BenesNetwork()
        route = network.routes(0, 5)[0]
        used = {name for name, _ in route.states}
        sent = {}
        controller = SimpleNamespace(
            drive_voltage=lambda name, voltage: sent.__setitem__(name, voltage) or voltage,
            apply_calibrated_state=lambda name, crossed: sent.__setitem__(name, 2.0 if crossed else 3.0)
            or sent[name])
        shown = {name: SimpleNamespace(set=lambda value: None) for name in network.switches}
        app = SimpleNamespace(network=network, controller=controller, routes=[route], selected_route=0,
                              keep_permutation_var=SimpleNamespace(get=lambda: False),
                              zero_volt_switches=set(), voltage_vars=shown,
                              clear_permutation_preview=lambda: None,
                              refresh=lambda **kwargs: None,
                              update_hardware_status=lambda value: None)
        app.drive_route_states = lambda states, names, zero_others: BenesApp.drive_route_states(
            app, states, names, zero_others)
        BenesApp.apply_route(app)
        self.assertEqual({name for name, voltage in sent.items() if voltage == 0.0},
                         set(network.switches) - used)
        self.assertTrue(all(network.states[name] for name in set(network.switches) - used))
        self.assertEqual(network.trace(0).rows[-1], 5)

    def test_laser_detection_reads_and_retains_each_osa_output(self):
        selected = []
        selector = SimpleNamespace(select_output=selected.append, mapped_outputs=lambda: list(range(1, 7)))
        app = SimpleNamespace(osa_output_switch=selector,
                              cal_status_var=SimpleNamespace(set=lambda value: None),
                              after=lambda delay, callback: callback())
        wave = [1549 + index * 0.01 for index in range(1400)]

        def spectrum(host, port, fresh_sweeps):
            center = 1549 + selected[-1]
            return wave, [-60 + 40 * math.exp(-((value - center) / 0.03) ** 2)
                          + 50 * math.exp(-((value - 1561) / 0.03) ** 2) for value in wave]

        traces = []
        with patch("benes.gui.calibration.osa.get_spectrum", side_effect=spectrum):
            wavelengths = CalibrationMixin.detect_laser_channels_from_outputs(app, "osa", 1, traces)
        self.assertEqual(selected, list(range(1, 7)))
        self.assertEqual(wavelengths, [1555, 1554, 1553, 1552, 1551, 1550])
        self.assertEqual(len(traces), 6)
        self.assertTrue(all(len(item[1]) == len(item[2]) == len(wave) - 2 for item in traces))

    def test_all_switch_control_states_covers_every_12_bit_configuration(self):
        app = SimpleNamespace(network=BenesNetwork())
        configurations = BenesApp.all_switch_control_states(app)
        self.assertEqual(len(configurations), 2 ** 12)
        self.assertEqual(len({tuple(states.values()) for _, states in configurations}), 2 ** 12)
        self.assertEqual(len({permutation for permutation, _ in configurations}), 720)

    def test_crosstalk_branches_use_other_port_and_follow_downstream_switches(self):
        network = BenesNetwork()
        app = SimpleNamespace(network=network)
        xs = [100, 200, 300, 400, 500]
        ys = [10, 30, 50, 70, 90, 110]
        for crossed, other_exit in ((True, 0), (False, 1)):
            network.states["S1.1"] = crossed
            route = network.trace(0)
            branches = list(BenesApp.crosstalk_points(app, route, 600, xs, ys, 10))
            self.assertEqual(len(branches), len(route.states))
            self.assertEqual(branches[0][:4], [90, 10, 110, ys[other_exit]])
            row = network.links[0][other_exit]
            for stage in range(1, len(network.pairs)):
                row = network.step(stage, row, network.states.get(network.at.get((stage, row)), False))
            self.assertEqual(branches[0][-2:], [600, ys[row]])

    def test_unsafe_voltage_prompt_names_arduino_and_heater_limit(self):
        app = SimpleNamespace(_approved_unsafe_voltages=set())
        app._ask_on_main_thread = lambda *args, **kwargs: BenesApp._ask_on_main_thread(app, *args, **kwargs)
        with patch("tkinter.messagebox.askyesno", return_value=True) as ask:
            self.assertTrue(BenesApp.confirm_unsafe_voltage(app, "S1.1", 6.0))
        self.assertIn("Arduino sampler", ask.call_args.args[1])
        self.assertIn("heater overcurrent limit", ask.call_args.args[1])
        self.assertEqual(app._approved_unsafe_voltages, {6.0})

    def test_sweep_refuses_break_density_and_warns_once_above_safe(self):
        import tempfile
        from pathlib import Path
        from benes.hardware.voltage_controller import BenesVoltageController

        with tempfile.TemporaryDirectory() as directory:
            controller = BenesVoltageController(("S1.1",), Path(directory) / "config.json")
            controller.set_heater_parameters({0: {"r_heater": "150", "j_per_v": "10"}}, "")
            app = SimpleNamespace(controller=controller, _approved_over_safe=set(),
                                  _approved_unsafe_voltages=set())
            # Safe 5 V, break 7 V.
            with patch("tkinter.messagebox.askyesno", return_value=True) as ask:
                with self.assertRaisesRegex(ValueError, "break current"):
                    BenesApp.confirm_voltage_sweep(app, [0, 7.0], names=["S1.1"])
                ask.assert_not_called()
                BenesApp.confirm_voltage_sweep(app, [0, 4.0, 5.5], names=["S1.1"])
                self.assertIn("safe up to 5.00 V", ask.call_args_list[0].args[1])
                self.assertEqual(app._approved_over_safe, {("S1.1", 5.5)})
                # 5.5 V is outside 0–5 V too, so both prompts ran; a repeat asks nothing.
                self.assertEqual(ask.call_count, 2)
                BenesApp.confirm_voltage_sweep(app, [5.5], names=["S1.1"])
                self.assertEqual(ask.call_count, 2)

    def test_switch_click_cycles_cross_straight_zero_volt(self):
        network = BenesNetwork()
        sent = []
        controller = SimpleNamespace(
            apply_calibrated_state=lambda name, crossed: sent.append((name, crossed)) or (3.0 if crossed else 1.0),
            drive_voltage=lambda name, voltage: sent.append((name, voltage)) or voltage,
        )
        shown = {}
        app = SimpleNamespace(
            network=network, controller=controller, zero_volt_switches=set(),
            voltage_vars={"S1.1": SimpleNamespace(set=lambda value: shown.update(S11=value))},
            refresh=lambda keep_selection: None, update_hardware_status=lambda text: None,
        )
        network.states["S1.1"] = True
        states = []
        for _ in range(4):
            BenesApp.toggle(app, "S1.1")
            states.append((network.states["S1.1"], "S1.1" in app.zero_volt_switches, shown["S11"]))
        self.assertEqual(states, [(False, False, "1.00"), (True, True, "0.00"),
                                  (True, False, "3.00"), (False, False, "1.00")])
        self.assertEqual(sent[1], ("S1.1", 0.0))

    def test_explicit_switch_state_controls_set_cross_bar_and_zero(self):
        network = BenesNetwork()
        sent, shown = [], {}
        controller = SimpleNamespace(
            apply_calibrated_state=lambda name, crossed: sent.append((name, crossed)) or (3.0 if crossed else 1.0),
            drive_voltage=lambda name, voltage: sent.append((name, voltage)) or voltage,
        )
        app = SimpleNamespace(
            network=network, controller=controller, zero_volt_switches=set(),
            voltage_vars={"S1.1": SimpleNamespace(set=lambda value: shown.update(S11=value))},
            refresh=lambda keep_selection: None, update_hardware_status=lambda text: None,
        )
        BenesApp.set_switch_state(app, "S1.1", "bar")
        self.assertEqual((network.states["S1.1"], "S1.1" in app.zero_volt_switches, shown["S11"]),
                         (False, False, "1.00"))
        BenesApp.set_switch_state(app, "S1.1", "zero")
        self.assertEqual((network.states["S1.1"], "S1.1" in app.zero_volt_switches, shown["S11"]),
                         (True, True, "0.00"))
        BenesApp.set_switch_state(app, "S1.1", "cross")
        self.assertEqual((network.states["S1.1"], "S1.1" in app.zero_volt_switches, shown["S11"]),
                         (True, False, "3.00"))
        self.assertEqual(sent, [("S1.1", False), ("S1.1", 0.0), ("S1.1", True)])


class AutoBNCReferenceTests(unittest.TestCase):
    def reference_app(self, measure_error=None):
        from benes.gui.routing import RoutingMixin

        calls = []
        options = {"resource": "GPIB0::23::INSTR", "channel": "A", "wavelength_nm": 1550.0, "range": "Auto"}
        shown = []
        app = SimpleNamespace(
            _bnc_reference_busy=False, bnc_reference=None, hp_meter_busy=None,
            hp_meter_session=SimpleNamespace(name="hp"), hp_meter_session_options=options,
            power_meter=SimpleNamespace(connected=True),
            newport_controller=SimpleNamespace(connected=True, read_states=lambda: calls.append("read states")),
            newport_panel=SimpleNamespace(busy=False, render=lambda: None),
            laser_bank=SimpleNamespace(
                configure=lambda slots, settle: calls.append(("configure", settle)),
                begin=lambda: calls.append("begin"),
                light_only=lambda row: calls.append(("light", row)),
                finish=lambda: calls.append("restore lasers")),
            laser_slot_vars={}, laser_settle_var=SimpleNamespace(get=lambda: "5"),
            osa_output_switch=SimpleNamespace(connected=True, routed_output=5,
                                              select_output=lambda output: calls.append(("jdsu", output))),
            osa_output_var=SimpleNamespace(set=lambda value: calls.append(("osa var", value))),
            bnc_input_var=SimpleNamespace(get=lambda: "1"), bnc_output_var=SimpleNamespace(get=lambda: "5"),
            bnc_reference_button=SimpleNamespace(configure=lambda **kwargs: None),
            route_arduino_var=SimpleNamespace(set=shown.append),
            refresh_hp_connection_buttons=lambda: None, draw=lambda: None,
            measurement_busy=lambda: False,
            configure_hp_meter=lambda meter, opts: calls.append(("hp settings", opts["range"])),
            after=lambda delay, callback, *args: callback(*args))
        for name in ("start_bnc_reference", "finish_bnc_reference", "auto_bnc_reference", "show_reference_laser"):
            setattr(app, name, getattr(RoutingMixin, name).__get__(app))

        def measure(arduino, hp, channel, resource):
            calls.append(("measure", channel, resource))
            if measure_error:
                raise ValueError(measure_error)
            return BNCReference(30.0, -35.0, -40, resource, channel)

        class Inline:
            def __init__(self, target, daemon=None):
                self.target = target

            def start(self):
                self.target()

        patches = (patch("benes.gui.routing.measure_bnc_reference", measure),
                   patch("benes.gui.routing.threading.Thread", Inline))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return app, calls, shown

    def test_launch_reference_lights_input_1_reads_output_5_and_restores(self):
        app, calls, shown = self.reference_app()
        app.auto_bnc_reference()
        self.assertEqual(calls[:5], [("configure", "5"), ("jdsu", 5), "begin", ("light", 0),
                                     ("measure", 1, "GPIB0::23::INSTR")])
        self.assertIn("restore lasers", calls)
        self.assertIn(("hp settings", "Auto"), calls)  # the fixed reference range is undone
        self.assertEqual(app.bnc_reference.range_dbm, -40)
        self.assertFalse(app.newport_panel.busy)
        self.assertIsNone(app.hp_meter_busy)
        self.assertIn("reference -35.00 dBm", shown[-1])

    def test_launch_waits_for_the_newport_instead_of_reading_darkness(self):
        app, calls, shown = self.reference_app()
        app.newport_controller.connected = False
        app.auto_bnc_reference()
        self.assertEqual(calls, [])
        self.assertIsNone(app.bnc_reference)
        self.assertIn("Newport not connected", shown[-1])

    def test_a_failed_reference_still_restores_lasers_and_hp(self):
        app, calls, shown = self.reference_app(measure_error="No steady light on the HP")
        app.auto_bnc_reference()
        self.assertIsNone(app.bnc_reference)
        self.assertIn("restore lasers", calls)
        self.assertIn(("hp settings", "Auto"), calls)
        self.assertIn("No steady light", shown[-1])

    def test_launch_skips_the_reference_without_the_meters(self):
        app, calls, _ = self.reference_app()
        app.power_meter.connected = False
        app.auto_bnc_reference()
        self.assertEqual(calls, [])


class RoutingClickTests(unittest.TestCase):
    def routing_app(self, newport_connected=True, jdsu_connected=True, tec=True):
        from benes.gui.routing import RoutingMixin
        from benes.hardware.newport_laser import ModuleState

        toggled, routed = [], []
        modules = [ModuleState("SIM LDD/TEC", False, tec) for _ in range(16)]
        app = SimpleNamespace(
            source=None, destination=None,
            newport_controller=SimpleNamespace(connected=newport_connected, modules=modules),
            newport_panel=SimpleNamespace(busy=False, toggle=lambda slot, kind: toggled.append((slot, kind))),
            laser_bank=SimpleNamespace(slot_for=lambda row: 6 - row),
            osa_output_switch=SimpleNamespace(connected=jdsu_connected),
            measurement_busy=lambda: False,
            input_var=SimpleNamespace(set=lambda value: setattr(app, "input_value", value)),
            output_var=SimpleNamespace(set=lambda value: setattr(app, "output_value", value)),
            osa_output_var=SimpleNamespace(set=routed.append),
            choose_input=lambda: None, choose_output=lambda: None,
            status=SimpleNamespace(configure=lambda **kwargs: None))
        for name in ("click_input", "toggle_input_laser", "click_output"):
            setattr(app, name, getattr(RoutingMixin, name).__get__(app))
        return app, modules, toggled, routed

    def test_input_click_toggles_its_newport_laser_and_follows_it(self):
        app, modules, toggled, _ = self.routing_app()
        app.click_input(2)
        self.assertEqual((toggled, app.input_value), ([(4, "laser")], "3"))
        # Laser 3 is now on: the next click turns it off and clears the selection.
        modules[3] = type(modules[3])(modules[3].model, True, True)
        app.source = 2
        app.click_input(2)
        self.assertEqual((toggled[-1], app.input_value), ((4, "laser"), "None"))

    def test_input_click_without_newport_only_selects(self):
        app, _, toggled, _ = self.routing_app(newport_connected=False)
        app.click_input(0)
        self.assertEqual((toggled, app.input_value), ([], "1"))

    def test_laser_stays_off_while_its_tec_is_off(self):
        app, _, toggled, _ = self.routing_app(tec=False)
        with patch("benes.gui.routing.messagebox.showerror") as error:
            app.click_input(0)
        error.assert_called_once()
        self.assertEqual(toggled, [])

    def test_output_click_routes_the_jdsu_only_when_connected(self):
        app, _, _, routed = self.routing_app()
        app.click_output(4)
        self.assertEqual((app.output_value, routed), ("5", ["5"]))
        app, _, _, routed = self.routing_app(jdsu_connected=False)
        app.click_output(4)
        self.assertEqual(routed, [])


class ShutdownTests(unittest.TestCase):
    def test_close_turns_lasers_off_first_and_releases_every_instrument_despite_failures(self):
        calls = []

        def step(name, error=None):
            def run():
                calls.append(name)
                if error:
                    raise RuntimeError(error)
            return run

        hp = SimpleNamespace(disconnect=step("hp"))
        app = SimpleNamespace(
            newport_controller=SimpleNamespace(connected=True,
                                               all_off=lambda: calls.append("lasers off") or ["slot 3 laser: timeout"],
                                               disconnect=step("newport")),
            controller=SimpleNamespace(disconnect=step("board", "port vanished")),
            power_meter=SimpleNamespace(disconnect=step("arduino")),
            hp_meter_session=hp,
            osa_output_switch=SimpleNamespace(disconnect=step("jdsu")))
        errors = BenesApp.shutdown_instruments(app)
        self.assertEqual(calls, ["lasers off", "board", "arduino", "hp", "jdsu", "newport"])
        self.assertIsNone(app.hp_meter_session)
        self.assertEqual(errors, ["Newport slot 3 laser: timeout: not confirmed off",
                                  "Voltage board did not disconnect: port vanished"])


class DatasetFolderTests(unittest.TestCase):
    def test_last_dataset_folder_is_remembered_and_linked_at_startup(self):
        import tempfile
        from pathlib import Path
        from benes.gui.dataset import DatasetMixin

        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as dataset,                 patch("benes.gui.dataset.app_dir", lambda: Path(home)):
            self.assertEqual(DatasetMixin.last_dataset_folder(), "")
            DatasetMixin.remember_dataset_folder(dataset)
            self.assertEqual(DatasetMixin.last_dataset_folder(), dataset)
            (Path(dataset) / "benes_dataset.json").write_text("{}", encoding="utf-8")
            analysed, status = [], []
            app = SimpleNamespace(dataset_folder_var=SimpleNamespace(get=lambda: dataset),
                                  dataset_status_var=SimpleNamespace(set=status.append),
                                  analyze_linked_dataset=lambda: analysed.append(True))
            DatasetMixin.restore_dataset_folder(app)
            self.assertEqual(analysed, [True])
            missing = SimpleNamespace(dataset_folder_var=SimpleNamespace(get=lambda: str(Path(home) / "gone")),
                                      dataset_status_var=SimpleNamespace(set=status.append),
                                      analyze_linked_dataset=lambda: analysed.append(True))
            DatasetMixin.restore_dataset_folder(missing)
            self.assertEqual(analysed, [True])
            self.assertIn("was not found", status[-1])

    def test_a_typed_folder_is_remembered_without_using_the_dataset_tab(self):
        import tempfile
        import tkinter as tk
        from pathlib import Path
        from benes.gui.dataset import DatasetMixin

        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk unavailable: {error}")
        root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as dataset,                     patch("benes.gui.dataset.app_dir", lambda: Path(home)):
                holder = type("Holder", (DatasetMixin,), {})()
                holder.dataset_folder_var = tk.StringVar(root, value=DatasetMixin.last_dataset_folder())
                holder.dataset_folder_var.trace_add("write", lambda *_: holder.dataset_folder_changed())
                holder.dataset_folder_var.set(str(Path(home) / "not yet there"))
                self.assertEqual(DatasetMixin.last_dataset_folder(), "")
                holder.dataset_folder_var.set(dataset)
                self.assertEqual(DatasetMixin.last_dataset_folder(), dataset)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
