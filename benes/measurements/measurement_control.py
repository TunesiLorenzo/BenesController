"""Small, hardware-agnostic controls shared by interruptible measurements.

The helpers here deliberately know nothing about a particular measurement
workflow.  Keeping cancellation, settling and output selection together
prevents calibration, route checks and optimisation from developing subtly
different behaviour.
"""

from __future__ import annotations


class CalibrationStopped(RuntimeError):
    """Raised when the operator stops an interruptible measurement."""


def check_stop(stop_event) -> None:
    """Raise promptly when an optional stop event has been set."""
    if stop_event is not None and stop_event.is_set():
        raise CalibrationStopped("Measurement stopped")


def wait(seconds: float, stop_event, sleep) -> None:
    """Wait in short intervals so a long instrument settle remains stoppable."""
    while seconds > 0:
        check_stop(stop_event)
        step = min(0.1, seconds)
        sleep(step)
        seconds -= step


def select_output(switch, output: int) -> None:
    """Select a zero-based chip output and let the selector verify its readback."""
    # A cached output number cannot establish where the physical JDSU is now.
    switch.select_output(output + 1)
