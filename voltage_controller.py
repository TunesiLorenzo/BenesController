"""Persistent bridge between the Beneš GUI and VoltageTuner's board API."""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
from typing import Callable


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
    ):
        self.switch_names = tuple(switch_names)
        self.config_path = config_path or Path(__file__).with_name("benes_voltage_config.json")
        self._board_factory = board_factory
        self.board = None
        self.applied_voltages: dict[str, float] = {}
        self.load_error = ""
        self.settings = self._load()

    def _defaults(self) -> dict:
        # The first 12 tuner channels mirror the 12 switches in the network.
        return {
            "port": DEFAULT_PORT,
            "baud": DEFAULT_BAUD,
            "dry_run": False,
            "mapping": {name: index for index, name in enumerate(self.switch_names)},
            "voltage": {name: 0.0 for name in self.switch_names},
            "calibration": {name: {"straight": None, "cross": None} for name in self.switch_names},
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
            return settings
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            self.load_error = f"Could not read {self.config_path.name}: {error}. Defaults are in use."
            return self._defaults()

    @staticmethod
    def _optional_voltage(value: object) -> float | None:
        return None if value in (None, "") else validate_voltage(value)

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

    def set_calibration(self, name: str, straight: object, cross: object) -> None:
        self._require_switch(name)
        self.settings["calibration"][name] = {
            "straight": self._optional_voltage(straight),
            "cross": self._optional_voltage(cross),
        }
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
            self.board.send(pin, voltage)
            self.applied_voltages[name] = voltage

    def _require_switch(self, name: str) -> None:
        if name not in self.switch_names:
            raise ValueError(f"Unknown MZI: {name}")

    @staticmethod
    def _load_board_factory() -> Callable[..., object]:
        board_path = Path(__file__).resolve().parent.parent / "VoltageTuner" / "board.py"
        if not board_path.exists():
            raise RuntimeError(f"VoltageTuner board module was not found at {board_path}")
        spec = importlib.util.spec_from_file_location("benes_voltage_tuner_board", board_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("Could not load VoltageTuner's board module")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.Board
