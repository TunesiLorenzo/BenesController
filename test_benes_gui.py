import unittest
from types import SimpleNamespace
from unittest.mock import patch

from benes_gui import BenesApp
from calibration_scan import minimum_default_cross_pairs
from network import BenesNetwork
from impulse_test import validate_impulse_settings


class PathProgressTests(unittest.TestCase):
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
        with patch("benes_gui.messagebox.askyesno", return_value=True) as ask:
            self.assertTrue(BenesApp.confirm_unsafe_voltage(app, "S1.1", 6.0))
        self.assertIn("Arduino sampler", ask.call_args.args[1])
        self.assertIn("heater overcurrent limit", ask.call_args.args[1])
        self.assertEqual(app._approved_unsafe_voltages, {6.0})

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
