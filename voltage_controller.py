"""Persistent bridge between the Beneš GUI and VoltageTuner's board API."""

from __future__ import annotations

import importlib.util
import json
import math
import sys
import time
from pathlib import Path
from typing import Callable


def app_dir() -> Path:
    """Folder for settings and results: beside the exe when frozen, else beside the sources."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def sibling_module_path(project: str, filename: str) -> Path:
    """Locate a helper from a sibling project, or the copy bundled into the exe."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / project / filename
    return Path(__file__).resolve().parent.parent / project / filename


DEFAULT_PORT = "COM4"
DEFAULT_BAUD = 115200


def validate_voltage(value: object) -> float:
    """Return a board-safe voltage, using VoltageTuner's -10 to +10 V limits."""
    voltage = float(value)
    if not math.isfinite(voltage):
        raise ValueError("Voltage must be finite")
    return min(10.0, max(-10.0, voltage))


class BenesVoltageController:
    """Own the persisted MZI configuration and the optional serial-board link."""

    def __init__(
        self,
        switch_names: tuple[str, ...] | list[str],
        config_path: Path | None = None,
        board_factory: Callable[..., object] | None = None,
        unsafe_voltage_authorizer: Callable[[str, float], bool] | None = None,
    ):
        self.switch_names = tuple(switch_names)
        self.config_path = config_path or app_dir() / "benes_voltage_config.json"
        self._board_factory = board_factory
        self.unsafe_voltage_authorizer = unsafe_voltage_authorizer
        self.board = None
        self.applied_voltages: dict[str, float] = {}
        self.load_error = ""
        self.settings = self._load()

    def _defaults(self) -> dict:
        # Reserve pin 8 for the electrical monitor on new configurations.
        pins = [pin for pin in range(16) if pin != 8]
        return {
            "port": DEFAULT_PORT,
            "baud": DEFAULT_BAUD,
            "dry_run": False,
            "mapping": {name: pins[index] if index < len(pins) else None
                        for index, name in enumerate(self.switch_names)},
            "voltage": {name: 0.0 for name in self.switch_names},
            "calibration": {name: {"straight": None, "cross": None} for name in self.switch_names},
            # Heater resistances for plotting power = V^2 / R; pins without one use the default.
            "heater_resistance_ohm": {},
            "default_heater_resistance_ohm": None,
        }

    def _load(self) -> dict:
        settings = self._defaults()
        if not self.config_path.exists():
            return settings
        try:
            stored = json.loads(self.config_path.read_text(encoding="utf-8"))
            settings["port"] = str(stored.get("port", settings["port"]))
            settings["baud"] = int(stored.get("baud", settings["baud"]))
            settings["dry_run"] = bool(stored.get("dry_run", settings["dry_run"]))
            for name in self.switch_names:
                pin = stored.get("mapping", {}).get(name, settings["mapping"][name])
                settings["mapping"][name] = self._validate_pin(pin, allow_none=True)
                settings["voltage"][name] = validate_voltage(stored.get("voltage", {}).get(name, 0.0))
                calibration = stored.get("calibration", {}).get(name, {})
                settings["calibration"][name] = {
                    state: self._optional_voltage(calibration.get(state))
                    for state in ("straight", "cross")
                }
            self._ensure_unique_pins(settings["mapping"])
            settings["heater_resistance_ohm"] = {
                str(self._validate_pin(int(pin))): self._optional_resistance(value)
                for pin, value in stored.get("heater_resistance_ohm", {}).items()
                if value is not None
            }
            settings["default_heater_resistance_ohm"] = self._optional_resistance(
                stored.get("default_heater_resistance_ohm"))
            return settings
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            self.load_error = f"Could not read {self.config_path.name}: {error}. Defaults are in use."
            return self._defaults()

    @staticmethod
    def _optional_voltage(value: object) -> float | None:
        return None if value in (None, "") else validate_voltage(value)

    @staticmethod
    def _optional_resistance(value: object) -> float | None:
        if value is None or str(value).strip() == "":
            return None
        resistance = float(value)
        if not math.isfinite(resistance) or resistance <= 0:
            raise ValueError("Heater resistance must be a positive number of ohms")
        return resistance

    def set_heater_resistances(self, per_pin: dict[int, object], default: object) -> None:
        """Store per-pin heater resistances; blank values fall back to the default."""
        checked = {pin: self._optional_resistance(value)
                   for pin, value in per_pin.items() if self._validate_pin(pin) is not None}
        default = self._optional_resistance(default)
        stored = dict(self.settings["heater_resistance_ohm"])
        for pin, resistance in checked.items():
            if resistance is None:
                stored.pop(str(pin), None)
            else:
                stored[str(pin)] = resistance
        self.settings["heater_resistance_ohm"] = stored
        self.settings["default_heater_resistance_ohm"] = default
        self.save()

    def heater_resistance(self, name: str) -> float | None:
        """Resistance of the heater on an MZI's mapped pin, or the global default."""
        self._require_switch(name)
        pin = self.settings["mapping"][name]
        resistance = None if pin is None else self.settings["heater_resistance_ohm"].get(str(pin))
        return resistance if resistance is not None else self.settings["default_heater_resistance_ohm"]

    def heater_power_mw(self, name: str, voltage: float) -> float | None:
        """Electrical heater power for a commanded voltage, if a resistance is known."""
        resistance = self.heater_resistance(name)
        return None if resistance is None else voltage * voltage / resistance * 1000

    @staticmethod
    def _validate_pin(pin: object, allow_none: bool = False) -> int | None:
        if pin is None and allow_none:
            return None
        if isinstance(pin, bool) or not isinstance(pin, int) or not 0 <= pin <= 15:
            raise ValueError("Pin must be an integer from 0 to 15, or unassigned")
        return pin

    @staticmethod
    def _ensure_unique_pins(mapping: dict[str, int | None]) -> None:
        pins = [pin for pin in mapping.values() if pin is not None]
        if len(pins) != len(set(pins)):
            raise ValueError("A physical pin can only be assigned to one MZI")

    def save(self) -> None:
        temp_path = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        temp_path.write_text(json.dumps(self.settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp_path.replace(self.config_path)

    @property
    def connected(self) -> bool:
        return self.board is not None

    def configure_connection(self, port: str, baud: object, dry_run: bool) -> None:
        baud = int(baud)
        if not port.strip():
            raise ValueError("Enter a serial port, for example COM4")
        if baud <= 0:
            raise ValueError("Baud rate must be positive")
        self.settings.update(port=port.strip(), baud=baud, dry_run=bool(dry_run))
        self.save()

    def set_mapping(self, mapping: dict[str, int | None]) -> None:
        if set(mapping) != set(self.switch_names):
            raise ValueError("The mapping must include every MZI")
        checked = {name: self._validate_pin(pin, allow_none=True) for name, pin in mapping.items()}
        self._ensure_unique_pins(checked)
        self.settings["mapping"] = checked
        self.save()
        self.send_configured_voltages()

    def set_voltage(self, name: str, value: object) -> float:
        self._require_switch(name)
        voltage = validate_voltage(value)
        self._check_voltage(name, voltage)
        self.settings["voltage"][name] = voltage
        self.save()
        self._send(name, voltage)
        return voltage

    def drive_voltage(self, name: str, value: object) -> float:
        """Send a temporary voltage without changing the saved manual setting."""
        self._require_switch(name)
        voltage = validate_voltage(value)
        self._send(name, voltage)
        return voltage

    def drive_with_mirror(self, name: str, value: object, mirror_pin: int) -> tuple[float, float]:
        """Send one edge to an MZI and a mirror pin in a single unpaced serial write.

        VoltageTuner's Board.send sleeps 10 ms per command and pyserial's Windows
        flush() polls in 50 ms steps, so both are bypassed here. Returns the
        estimated perf_counter times at which each command finished arriving.
        """
        self._require_switch(name)
        voltage = validate_voltage(value)
        pin = self.settings["mapping"][name]
        if self.board is None or pin is None:
            raise RuntimeError(f"{name} is not connected to a board pin")
        self._check_voltage(name, voltage)
        serial = getattr(self.board, "serial", None)
        if serial is None:
            # Dry-run or test boards: keep their own pacing and logging.
            self.board.send(pin, voltage)
            target_time = time.perf_counter()
            self.board.send(mirror_pin, voltage)
            self.applied_voltages[name] = voltage
            return target_time, time.perf_counter()
        # Same text protocol as VoltageTuner's Board.send.
        target = f"DC {pin} {voltage:.2f}\n".encode("ascii")
        payload = target + f"DC {mirror_pin} {voltage:.2f}\n".encode("ascii")
        start = time.perf_counter()
        if serial.write(payload) != len(payload):
            raise OSError("Incomplete serial write")
        self.applied_voltages[name] = voltage
        seconds_per_byte = 10 / self.settings["baud"]
        return (start + len(target) * seconds_per_byte,
                start + len(payload) * seconds_per_byte)

    def set_calibration(self, name: str, straight: object, cross: object) -> None:
        self._require_switch(name)
        self.settings["calibration"][name] = {
            "straight": self._optional_voltage(straight),
            "cross": self._optional_voltage(cross),
        }
        self.save()

    def clear_calibration(self, names, state=None) -> None:
        """Forget selected saved voltages without changing board outputs."""
        names = tuple(names)
        for name in names:
            self._require_switch(name)
        if state not in (None, "straight", "cross"):
            raise ValueError(f"Unknown calibration state: {state}")
        for name in names:
            if state is None:
                self.settings["calibration"][name] = {"straight": None, "cross": None}
            else:
                self.settings["calibration"][name][state] = None
        self.save()

    def connect(self) -> None:
        if self.connected:
            return
        factory = self._board_factory or self._load_board_factory()
        self.board = factory(
            port=self.settings["port"],
            baud=self.settings["baud"],
            dry_run=self.settings["dry_run"],
        )
        try:
            self.send_configured_voltages()
        except Exception:
            self.disconnect()
            raise

    def disconnect(self) -> None:
        if self.board is not None:
            self.board.close()
            self.board = None
            self.applied_voltages.clear()

    def apply_calibrated_states(self, states: dict[str, bool]) -> None:
        """Send calibrated cross/straight voltages where they have been defined."""
        for name in self.switch_names:
            self.apply_calibrated_state(name, states[name])

    def apply_calibrated_state(self, name: str, crossed: bool) -> float | None:
        """Send one switch's calibrated voltage, if that state has one."""
        self._require_switch(name)
        state = "cross" if crossed else "straight"
        voltage = self.settings["calibration"][name][state]
        if voltage is not None:
            self._send(name, voltage)
        return voltage

    def send_configured_voltages(self) -> None:
        """Synchronize every currently mapped MZI to its saved manual voltage."""
        for name in self.switch_names:
            self._send(name, self.settings["voltage"][name])

    def _send(self, name: str, voltage: float) -> None:
        if self.board is None:
            return
        pin = self.settings["mapping"][name]
        if pin is not None:
            self._check_voltage(name, voltage)
            self.board.send(pin, voltage)
            self.applied_voltages[name] = voltage

    def _check_voltage(self, name: str, voltage: float) -> None:
        if 0.0 <= voltage <= 5.0:
            return
        if self.unsafe_voltage_authorizer is None or not self.unsafe_voltage_authorizer(name, voltage):
            raise ValueError(
                f"{name}: {voltage:g} V is outside the default 0–5 V range and was not confirmed"
            )

    def _require_switch(self, name: str) -> None:
        if name not in self.switch_names:
            raise ValueError(f"Unknown MZI: {name}")

    @staticmethod
    def _load_board_factory() -> Callable[..., object]:
        board_path = sibling_module_path("VoltageTuner", "board.py")
        if not board_path.exists():
            raise RuntimeError(f"VoltageTuner board module was not found at {board_path}")
        spec = importlib.util.spec_from_file_location("benes_voltage_tuner_board", board_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("Could not load VoltageTuner's board module")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.Board
