"""The Newport laser modules that feed the six Beneš inputs, lit one at a time.

The bays are patched in reverse: module slot 1 feeds input 6 and slot 6 feeds
input 1. The mapping and the laser settling time are kept in
``benes_laser_bank_config.json``.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Callable

from benes.paths import app_dir

INPUT_COUNT = 6


def default_slots() -> dict[int, int]:
    """{1-based input: Newport slot}; slot 1 feeds input 6."""
    return {port: INPUT_COUNT + 1 - port for port in range(1, INPUT_COUNT + 1)}


class LaserBank:
    """Light one Beneš input at a time through a Newport 9016 controller.

    ``controller`` needs ``connected``, ``modules``, ``read_states()`` and
    ``set_output(slot, "laser", on)``, as ``NewportLaserController`` has.
    """

    def __init__(self, controller, config_path: Path | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.controller = controller
        self.config_path = config_path or app_dir() / "benes_laser_bank_config.json"
        self._sleep = sleep
        self.settings = self._load()
        # 0-based input currently lit by this bank, None when all are off.
        self.lit = None
        self._initial = None

    @staticmethod
    def _defaults() -> dict:
        return {"slots": default_slots(), "settle_s": 1.0}

    def _load(self) -> dict:
        settings = self._defaults()
        try:
            stored = json.loads(self.config_path.read_text(encoding="utf-8"))
            slots = {int(port): int(slot) for port, slot in stored.get("slots", {}).items()}
            settle_s = float(stored.get("settle_s", settings["settle_s"]))
            self._validate(slots, settle_s)
            settings = {"slots": slots, "settle_s": settle_s}
        except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
            pass
        return settings

    @staticmethod
    def _validate(slots: dict[int, int], settle_s: float) -> None:
        if sorted(slots) != list(range(1, INPUT_COUNT + 1)):
            raise ValueError("Assign a Newport slot to each of inputs 1–6")
        if len(set(slots.values())) != len(slots) or not all(1 <= slot <= 16 for slot in slots.values()):
            raise ValueError("Use six different Newport slots from 1 to 16")
        if not math.isfinite(settle_s) or not 0 <= settle_s <= 60:
            raise ValueError("Laser settling time must be between 0 and 60 s")

    def configure(self, slots: dict, settle_s: object) -> None:
        slots = {int(port): int(slot) for port, slot in slots.items()}
        settle_s = float(settle_s)
        self._validate(slots, settle_s)
        self.settings = {"slots": slots, "settle_s": settle_s}
        self.config_path.write_text(json.dumps(
            {"slots": {str(port): slot for port, slot in slots.items()}, "settle_s": settle_s},
            indent=2) + "\n", encoding="utf-8")

    def slot_for(self, source: int) -> int:
        """Newport slot of a 0-based input."""
        return self.settings["slots"][source + 1]

    def begin(self) -> None:
        """Check every mapped module, remember its laser state and turn them all off."""
        if not self.controller.connected:
            raise RuntimeError("Connect the Newport laser controller first")
        modules = self.controller.read_states()
        for port, slot in self.settings["slots"].items():
            module = modules[slot - 1]
            if module.laser is None:
                raise ValueError(f"Newport slot {slot} (input {port}) has no laser output")
            if module.tec is False:
                raise RuntimeError(f"Turn on the TEC of Newport slot {slot} (input {port}) first")
        self._initial = {slot: modules[slot - 1].laser for slot in self.settings["slots"].values()}
        for slot in self.settings["slots"].values():
            if modules[slot - 1].laser:
                self.controller.set_output(slot, "laser", False)
        self.lit = None

    def light_only(self, source: int) -> None:
        """Light one 0-based input; the previous one goes off first so they never mix."""
        if self.lit == source:
            return
        if self.lit is not None:
            self.controller.set_output(self.slot_for(self.lit), "laser", False)
            self.lit = None
        self.controller.set_output(self.slot_for(source), "laser", True)
        self.lit = source
        self._sleep(self.settings["settle_s"])

    def finish(self) -> None:
        """Return every mapped laser to the state it had before ``begin``."""
        if self._initial is None:
            return
        errors = []
        for slot, was_on in self._initial.items():
            try:
                self.controller.set_output(slot, "laser", bool(was_on))
            except Exception as error:
                errors.append(f"slot {slot}: {error}")
        self._initial = None
        self.lit = None
        if errors:
            raise RuntimeError("Could not restore the Newport lasers: " + "; ".join(errors))


class SimulatedLaserController:
    """Dry-run Newport 9016: every slot holds a laser with its TEC on, all lasers start lit.

    Switching a laser lights or darkens the chip input its slot feeds.
    """

    connected = True

    def __init__(self, chip, slots: dict[int, int]):
        from benes.hardware.newport_laser import SLOT_COUNT, ModuleState

        self._state = ModuleState
        self.chip = chip
        self.input_for_slot = {slot: port - 1 for port, slot in slots.items()}
        self.modules = [ModuleState("SIM LDD/TEC", slot in self.input_for_slot, True)
                        for slot in range(1, SLOT_COUNT + 1)]
        chip.lit = set(self.input_for_slot.values())

    def read_states(self):
        return list(self.modules)

    def set_output(self, slot, kind, enabled):
        old = self.modules[slot - 1]
        self.modules[slot - 1] = self._state(old.model, enabled, old.tec)
        source = self.input_for_slot.get(slot)
        if source is not None:
            (self.chip.lit.add if enabled else self.chip.lit.discard)(source)
        return enabled
