"""Turn the bench off after a measurement and bring the previous routing back.

A measurement left alone must not keep heaters hot or lasers lit, so every
scan ends with each MZI at 0 V and every Newport laser current off (TECs
stay on). ``BenchState`` remembers what was on before, so one click restores
the routing and the lasers.

An MZI that sat at its calibrated cross or bar voltage is restored to that
state's calibration as it is now, so a calibration run in between is picked
up; any other voltage is sent again as it was.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BenchState:
    # Routing shown in the GUI, cross = True.
    states: dict[str, bool]
    zero: frozenset[str]
    # Voltage the board was driving on each mapped MZI; empty without a board.
    voltages: dict[str, float]
    # MZIs whose voltage was their state's calibrated voltage.
    calibrated: frozenset[str]
    # Newport slots whose laser current was on; empty without the controller.
    lasers: tuple[int, ...]

    @property
    def is_off(self) -> bool:
        return not self.lasers and all(voltage == 0 for voltage in self.voltages.values())


def snapshot(controller, states, zero, newport=None) -> BenchState:
    """What the board and the lasers are doing now, from their last confirmed commands."""
    voltages, calibrated = {}, set()
    if controller.connected:
        calibration = controller.settings["calibration"]
        for name, voltage in controller.applied_voltages.items():
            voltages[name] = voltage
            saved = calibration[name]["cross" if states[name] else "straight"]
            if name not in zero and saved is not None and abs(saved - voltage) < 0.005:
                calibrated.add(name)
    lasers = ()
    if newport is not None and newport.connected:
        lasers = tuple(slot for slot, module in enumerate(newport.modules, 1) if module.laser)
    return BenchState(dict(states), frozenset(zero), voltages, frozenset(calibrated), lasers)


def power_down(controller, names, newport=None) -> list[str]:
    """Every laser current off, then every MZI to 0 V; each step is tried even when another fails."""
    errors = []
    if newport is not None and newport.connected:
        try:
            errors += [f"Newport {error}: not confirmed off" for error in newport.all_off()]
        except Exception as error:
            errors.append(f"Newport lasers could not be turned off: {error}")
    if controller.connected:
        for name in names:
            try:
                controller.drive_voltage(name, 0.0)
            except Exception as error:
                errors.append(f"{name} not set to 0 V: {error}")
    return errors


def restore(controller, state: BenchState, newport=None) -> tuple[dict[str, float], list[str]]:
    """Send ``state`` again; return the voltage sent to each MZI and what failed."""
    sent, errors = {}, []
    if state.voltages and not controller.connected:
        errors.append("Connect the voltage board to restore the routing")
    elif state.voltages:
        calibration = controller.settings["calibration"]
        for name, voltage in state.voltages.items():
            try:
                crossed = state.states[name]
                if name in state.calibrated and calibration[name]["cross" if crossed else "straight"] is not None:
                    sent[name] = controller.apply_calibrated_state(name, crossed)
                else:
                    sent[name] = controller.drive_voltage(name, voltage)
            except Exception as error:
                errors.append(f"{name}: {error}")
    if state.lasers and (newport is None or not newport.connected):
        errors.append("Connect the Newport controller to turn the lasers back on")
    elif state.lasers:
        for slot in state.lasers:
            if newport.modules[slot - 1].tec is False:
                errors.append(f"Newport slot {slot}: its TEC is off, so the laser stays off")
                continue
            try:
                newport.set_output(slot, "laser", True)
            except Exception as error:
                errors.append(f"Newport slot {slot}: {error}")
    return sent, errors
