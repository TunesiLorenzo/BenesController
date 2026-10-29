import csv
import json
import math
import tempfile
import unittest
from pathlib import Path

from benes.core.network import BenesNetwork
from benes.measurements.power_route_check import (evaluate_route, plan_route_checks,
                                                  run_power_route_check, save_power_route_check)


class FakeController:
    def __init__(self, network):
        self.settings = {"calibration": {name: {"cross": 1.1, "straight": 2.6}
                                         for name in network.switches}}
        self.voltages = dict.fromkeys(network.switches, 0.0)

    def drive_voltage(self, name, voltage):
        self.voltages[name] = voltage


class FakeLasers:
    def __init__(self):
        self.lit = None
        self.sources = []
        self.waits = []
        self.finished = False

    def begin(self):
        self.lit = None

    def light_only(self, source, *, minimum_settle_s=0.0):
        self.lit = source
        self.sources.append(source)
        self.waits.append(minimum_settle_s)

    def finish(self):
        self.lit = None
        self.finished = True


class FakeSelector:
    def __init__(self):
        self.routed_output = 3
        self.reads = []
        self.settles = []

    def select_output(self, output, *, extra_settle_s=None):
        self.routed_output = output
        self.reads.append(output)
        self.settles.append(extra_settle_s)


class PowerRouteCheckTests(unittest.TestCase):
    def setUp(self):
        self.network = BenesNetwork()

    def test_plan_covers_every_input_output_and_both_states_of_every_switch(self):
        plan = plan_route_checks(self.network)
        self.assertEqual(len(plan), 36)
        self.assertEqual({(source, target) for source, target, _ in plan},
                         {(source, target) for source in range(6) for target in range(6)})
        self.assertEqual({state for _, _, route in plan for state in route.states},
                         {(name, crossed) for name in self.network.switches
                          for crossed in (False, True)})

    def test_single_reversed_switch_is_named_as_suspect(self):
        source, target, route = next(case for case in plan_route_checks(self.network)
                                     if any(name == "S5.1" for name, _ in case[2].states))
        states = dict.fromkeys(self.network.switches, True)
        states.update(route.states)
        states["S5.1"] = not states["S5.1"]
        actual = self.network.trace(source, states).rows[-1]
        self.assertNotEqual(actual, target)
        powers = [1e-5] * 6
        powers[actual] = 1.0
        result = evaluate_route(self.network, source, target, route, powers, 3.0)
        self.assertEqual(result.verdict, "misroute")
        self.assertIn("S5.1", result.suspects)

    def test_one_laser_and_six_outputs_per_route_then_cleanup(self):
        controller = FakeController(self.network)
        lasers = FakeLasers()
        selector = FakeSelector()
        readings = []

        def read_power():
            states = {name: voltage != 2.6 for name, voltage in controller.voltages.items()}
            actual = self.network.trace(lasers.lit, states).rows[-1]
            readings.append((lasers.lit, selector.routed_output))
            return 1.0 if selector.routed_output == actual + 1 else 1e-5

        results = run_power_route_check(controller, lasers, selector, read_power, self.network,
                                        0, 3, sleep=lambda _: None)
        self.assertEqual(len(results), 36)
        self.assertTrue(all(result.verdict == "pass" for result in results))
        self.assertEqual(len(readings), 216)
        self.assertEqual(lasers.sources, list(range(6)))
        self.assertEqual(lasers.waits, [5.0] * 6)
        self.assertTrue(lasers.finished)
        self.assertIsNone(lasers.lit)
        self.assertEqual(selector.routed_output, 3)
        self.assertEqual(selector.settles, [0] * 217)
        self.assertTrue(all(voltage == 0 for voltage in controller.voltages.values()))

    def test_physical_selector_timing_is_reported_before_route_readings(self):
        class TimingSelector(FakeSelector):
            settings = {"dry_run": False}

            def sweep_timing(self, repeats):
                self.timing_sweeps = repeats
                return {"count": 12, "median_s": 0.2, "per_output_median_s": {str(n): 0.2 for n in range(1, 7)}}

        controller = FakeController(self.network)
        selector = TimingSelector()
        events = []
        run_power_route_check(controller, FakeLasers(), selector, lambda: 1.0,
                              self.network, 0.05, 3, plan=plan_route_checks(self.network)[:1],
                              on_timing=lambda timing: events.append(("timing", timing["median_s"])),
                              on_output=lambda *args: events.append(("reading", args[2])),
                              sleep=lambda _: None)
        self.assertEqual(selector.timing_sweeps, 2)
        self.assertEqual(events[0], ("timing", 0.2))
        self.assertEqual(selector.settles, [0] * 7)

    def test_failure_still_restores_lasers_switches_and_jdsu(self):
        controller = FakeController(self.network)
        lasers = FakeLasers()
        selector = FakeSelector()
        def broken_read():
            raise TimeoutError("power meter did not respond")
        with self.assertRaisesRegex(TimeoutError, "did not respond"):
            run_power_route_check(controller, lasers, selector, broken_read, self.network,
                                  0, 3, sleep=lambda _: None,
                                  plan=plan_route_checks(self.network)[:1])
        self.assertTrue(lasers.finished)
        self.assertEqual(selector.routed_output, 3)
        self.assertTrue(all(voltage == 0 for voltage in controller.voltages.values()))

    def test_no_signal_and_ambiguous_are_not_called_misroutes(self):
        source, target, route = plan_route_checks(self.network)[0]
        self.assertEqual(evaluate_route(self.network, source, target, route,
                                        [None] * 6, 3).verdict, "no signal")
        self.assertEqual(evaluate_route(self.network, source, target, route,
                                        [0.01] * 6, 3).verdict, "ambiguous")

    def test_saved_csv_and_json_include_all_outputs(self):
        source, target, route = plan_route_checks(self.network)[0]
        result = evaluate_route(self.network, source, target, route,
                                [1.0 if output == target else 0.0 for output in range(6)], 3)
        self.assertTrue(math.isinf(result.margin_db))
        with tempfile.TemporaryDirectory() as temporary:
            files = save_power_route_check(temporary, {"kind": "test"}, [result])
            self.assertEqual(len(files), 2)
            record = json.loads((Path(temporary) / files[0]).read_text(encoding="utf-8"))
            self.assertEqual(len(record["routes"][0]["outputs_dbm"]), 6)
            with (Path(temporary) / files[1]).open(newline="", encoding="utf-8") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(rows[0]["verdict"], "pass")


if __name__ == "__main__":
    unittest.main()
