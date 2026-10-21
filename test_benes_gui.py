import unittest
from types import SimpleNamespace
from unittest.mock import patch

from benes_gui import BenesApp
from calibration_scan import minimum_default_cross_pairs
from network import BenesNetwork
from impulse_test import validate_impulse_settings


class PathProgressTests(unittest.TestCase):
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
            impulse_running=False, crosstalk_running=False,
            read_power_button=SimpleNamespace(instate=lambda states: False),
            impulse_input_ready=SimpleNamespace(get=lambda: True),
            impulse_fields={key: SimpleNamespace(get=lambda value=value: value)
                            for key, value in values.items()},
            impulse_average_var=SimpleNamespace(get=lambda: "0e0"),
            impulse_switch_var=SimpleNamespace(get=lambda: "S1.1"),
            controller=SimpleNamespace(connected=False),
        )
        with patch("benes_gui.validate_impulse_settings", wraps=validate_impulse_settings) as validate, \
                patch("benes_gui.messagebox.showerror") as error:
            BenesApp.start_impulse(app, "single")
        validate.assert_called_once_with("single", 0, 2, lead_in_s=0.001,
                                         pulse_s=0.05, measure_s=0.2)
        self.assertEqual(error.call_args.args[1], "Connect the physical voltage board first")

    def test_unsafe_voltage_prompt_names_arduino_and_heater_limit(self):
        app = SimpleNamespace(_approved_unsafe_voltages=set())
        app._ask_on_main_thread = lambda *args, **kwargs: BenesApp._ask_on_main_thread(app, *args, **kwargs)
        with patch("benes_gui.messagebox.askyesno", return_value=True) as ask:
            self.assertTrue(BenesApp.confirm_unsafe_voltage(app, "S1.1", 6.0))
        self.assertIn("Arduino sampler", ask.call_args.args[1])
        self.assertIn("heater overcurrent limit", ask.call_args.args[1])
        self.assertEqual(app._approved_unsafe_voltages, {6.0})

    def test_sweep_refuses_break_density_and_warns_once_above_safe(self):
        import tempfile
        from pathlib import Path
        from voltage_controller import BenesVoltageController

        with tempfile.TemporaryDirectory() as directory:
            controller = BenesVoltageController(("S1.1",), Path(directory) / "config.json")
            controller.set_heater_parameters({0: {"r_heater": "150", "j_per_v": "10"}}, "")
            app = SimpleNamespace(controller=controller, _approved_over_safe=set(),
                                  _approved_unsafe_voltages=set())
            # Safe 5 V, break 7 V.
            with patch("benes_gui.messagebox.askyesno", return_value=True) as ask:
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

    def test_completion_requires_every_mzi_even_when_last_planned_pair_runs_early(self):
        network = BenesNetwork()
        plan = minimum_default_cross_pairs(network)
        app = SimpleNamespace(
            network=network,
            voltage_vars={name: SimpleNamespace(set=lambda value: None) for name in network.switches},
            refresh=lambda keep_selection: None,
            power_meter=SimpleNamespace(settings={"unit": "ADC counts"}),
            default_cross_plan=plan,
            path_calibrated_switches=set(),
            next_connection_prompt=None,
            finish_crosstalk_scan=lambda *args: None,
        )

        def finish(pair):
            names = pair[2]
            BenesApp.finish_path_calibration(
                app, names, {}, "results", {"calibration_results": dict.fromkeys(names)},
            )

        finish(plan[0])
        finish(plan[-1])
        self.assertEqual(app.next_connection_prompt, plan[1][:2])
        self.assertLess(len(app.path_calibrated_switches), len(network.switches))

        finish(plan[1])
        self.assertEqual(app.next_connection_prompt, plan[2][:2])
        finish(plan[2])
        self.assertEqual(app.next_connection_prompt, "complete")
        self.assertEqual(app.path_calibrated_switches, set(network.switches))
        # Path calibration leaves every output at 0 V.
        self.assertEqual(app.zero_volt_switches, set(network.switches))

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


if __name__ == "__main__":
    unittest.main()
