"""Check calibrated cross/bar routes with one laser and a power meter."""

from __future__ import annotations

import csv
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

from benes.measurements.laser_calibration import _check_stop, _wait, reset_all
from benes.measurements.mzi_characterization import mw_to_dbm


@dataclass(frozen=True)
class RouteCheck:
    source: int
    target: int
    route: object
    powers_mw: tuple[float | None, ...]
    strongest: int | None
    margin_db: float | None
    verdict: str
    suspects: tuple[str, ...]

    @property
    def states(self):
        return dict(self.route.states)


def plan_route_checks(network):
    """Choose one route per input/output pair while covering both switch states."""
    seen = set()
    plan = []
    for source in range(network.size):
        for target in range(network.size):
            routes = network.routes(source, target)
            if not routes:
                raise ValueError(f"No route from input {source + 1} to output {target + 1}")
            route = max(routes, key=lambda item: (
                sum(state not in seen for state in item.states),
                -sum(not crossed for _, crossed in item.states),
                -len(item.states)))
            plan.append((source, target, route))
            seen.update(route.states)
    return tuple(plan)


def evaluate_route(network, source, target, route, powers_mw, pass_margin_db):
    """Grade one six-output profile and find single-switch explanations."""
    if len(powers_mw) != network.size:
        raise ValueError("Read one power value at every output")
    if not math.isfinite(pass_margin_db) or pass_margin_db <= 0:
        raise ValueError("Pass margin must be positive")
    dbm = [mw_to_dbm(value) if value is not None and math.isfinite(value) else None
           for value in powers_mw]
    measurable = [(index, value) for index, value in enumerate(dbm) if value is not None]
    if not measurable:
        return RouteCheck(source, target, route, tuple(powers_mw), None, None, "no signal", ())
    strongest = max(measurable, key=lambda item: item[1])[0]
    wanted = dbm[target]
    competitors = [value for index, value in measurable if index != target]
    competitor = max(competitors) if competitors else None
    margin = None if wanted is None else (math.inf if competitor is None else wanted - competitor)
    if margin is not None and margin >= pass_margin_db:
        verdict = "pass"
    elif strongest != target and (wanted is None or dbm[strongest] - wanted >= pass_margin_db):
        verdict = "misroute"
    else:
        verdict = "ambiguous"
    suspects = ()
    if verdict == "misroute":
        states = dict.fromkeys(network.switches, True)
        states.update(route.states)
        suspects = tuple(name for name, crossed in route.states
                         if network.trace(source, {**states, name: not crossed}).rows[-1] == strongest)
    return RouteCheck(source, target, route, tuple(powers_mw), strongest, margin, verdict, suspects)


def run_power_route_check(controller, lasers, selector, read_power, network, settle_s, pass_margin_db,
                          on_route=None, on_output=None, on_timing=None, stop_event=None, sleep=time.sleep,
                          plan=None, results=None):
    """Run each planned route once, reading all six outputs before advancing."""
    if not math.isfinite(settle_s) or settle_s < 0:
        raise ValueError("Settling time must be nonnegative")
    plan = plan_route_checks(network) if plan is None else tuple(plan)
    results = [] if results is None else results
    calibration = controller.settings["calibration"]
    previous_output = selector.routed_output
    voltages = dict.fromkeys(network.switches, None)
    primary_error = None
    try:
        if hasattr(selector, "sweep_timing") and not selector.settings["dry_run"]:
            timing = selector.sweep_timing(repeats=2)
            if on_timing is not None:
                on_timing(timing)
        reset_all(controller, network)
        voltages = dict.fromkeys(network.switches, 0.0)
        lasers.begin()
        current_source = None
        for index, (source, target, route) in enumerate(plan, start=1):
            _check_stop(stop_event)
            if source != current_source:
                lasers.light_only(source, minimum_settle_s=5.0)
                current_source = source
            route_states = dict(route.states)
            for name in network.switches:
                crossed = route_states.get(name)
                required = (0.0 if crossed is None else
                            calibration[name]["cross" if crossed else "straight"])
                if required is None:
                    raise ValueError(f"{name}: missing saved {'cross' if crossed else 'bar'} voltage")
                if voltages[name] != required:
                    controller.drive_voltage(name, required)
                    voltages[name] = required
            _wait(settle_s, stop_event, sleep)
            powers = []
            for output in range(network.size):
                _check_stop(stop_event)
                selector.select_output(output + 1, extra_settle_s=0)
                power = read_power()
                if power is not None and (not math.isfinite(power) or power < 0):
                    raise ValueError(f"Invalid optical power at output {output + 1}")
                powers.append(power)
                if on_output is not None:
                    on_output(source, target, output, power, index, len(plan))
            result = evaluate_route(network, source, target, route, powers, pass_margin_db)
            results.append(result)
            if on_route is not None:
                on_route(result, index, len(plan))
        return results
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_errors = []
        try:
            reset_all(controller, network)
        except Exception as error:
            cleanup_errors.append(f"switch reset: {error}")
        try:
            lasers.finish()
        except Exception as error:
            cleanup_errors.append(f"laser restore: {error}")
        if previous_output is not None:
            try:
                selector.select_output(previous_output, extra_settle_s=0)
            except Exception as error:
                cleanup_errors.append(f"JDSU restore: {error}")
        if cleanup_errors:
            message = "Route check cleanup failed: " + "; ".join(cleanup_errors)
            if primary_error is not None:
                primary_error.add_note(message)
            else:
                raise RuntimeError(message)


def save_power_route_check(folder, context, results):
    """Save all six dBm readings and the inferred path suspects."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for result in results:
        rows.append({
            "input": result.source + 1,
            "target_output": result.target + 1,
            "strongest_output": None if result.strongest is None else result.strongest + 1,
            "margin_db": (result.margin_db if result.margin_db is not None
                          and math.isfinite(result.margin_db) else None),
            "verdict": result.verdict,
            "suspects": list(result.suspects),
            "path_switches": [{"name": name, "state": "cross" if crossed else "bar"}
                              for name, crossed in result.route.states],
            "outputs_dbm": [mw_to_dbm(power) for power in result.powers_mw],
        })
    (folder / "power_route_check.json").write_text(
        json.dumps({**context, "routes": rows}, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    with (folder / "power_route_check.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["input", "target_output", "strongest_output", "margin_db", "verdict",
                         "suspects", "path_switches"] + [f"output_{port}_dbm" for port in range(1, 7)])
        for row in rows:
            writer.writerow([row["input"], row["target_output"], row["strongest_output"],
                             row["margin_db"], row["verdict"], " ".join(row["suspects"]),
                             " ".join(f"{item['name']}:{item['state']}" for item in row["path_switches"]),
                             *row["outputs_dbm"]])
    return ("power_route_check.json", "power_route_check.csv")
