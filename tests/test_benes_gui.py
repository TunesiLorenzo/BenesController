import unittest
import math
from types import SimpleNamespace
from unittest.mock import patch

from benes.gui.app import BenesApp
from benes.gui.calibration import CalibrationMixin
from benes.core.network import BenesNetwork
from benes.measurements.impulse_test import validate_impulse_settings


class ActivityProgressTests(unittest.TestCase):
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

    def test_impulse_time_fields_accept_scientific_notation_in_seconds(self):
        values = {
            "Low voltage [V]": "0", "High voltage [V]": "2e0",
            "Lead-in [s]": "1e-3", "Pulse duration [s]": "5e-2",
            "Measure duration [s]": "2e-1",
        }
        app = SimpleNamespace(
            impulse_running=False, crosstalk_running=False, mzi_calibration_running=False,
            read_power_button=SimpleNamespace(instate=lambda states: False),
            impulse_input_ready=SimpleNamespace(get=lambda: True),
            impulse_fields={key: SimpleNamespace(get=lambda value=value: value)
                            for key, value in values.items()},
            impulse_average_var=SimpleNamespace(get=lambda: "0e0"),
            impulse_switch_var=SimpleNamespace(get=lambda: "S1.1"),
            controller=SimpleNamespace(connected=False),
        )
        with patch("benes.gui.impulse.validate_impulse_settings", wraps=validate_impulse_settings) as validate, \
                patch("tkinter.messagebox.showerror") as error:
            BenesApp.start_impulse(app, "single")
        validate.assert_called_once_with("single", 0, 2, lead_in_s=0.001,
                                         pulse_s=0.05, measure_s=0.2)
        self.assertEqual(error.call_args.args[1], "Connect the physical voltage board first")

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


if __name__ == "__main__":
    unittest.main()
