"""How every measurement and trace read takes an OSA reading.

A reading first waits for ``FRESH_SWEEPS`` complete sweeps, so the trace lies
wholly after the last change (a JDSU move or a heater step), then averages
``sweeps`` traces as linear power. ``sweeps`` is one setting for the whole
suite, ``osa_sweeps`` in the wavelength plan.
"""

from __future__ import annotations

import math
from typing import Callable

from benes.hardware import osa

# The first new sweep may have started before the change; the second lies wholly after it.
FRESH_SWEEPS = 2
MAX_SWEEPS = 16


def trim_spectrum(spectrum) -> tuple[list[float], list[float]]:
    """Validate and remove the OSA's spurious tail from one raw spectrum."""
    wave, power = osa.trim_spurious_tail(*spectrum)
    if not wave or len(wave) != len(power):
        raise ValueError("The OSA returned an empty or mismatched spectrum")
    if not all(math.isfinite(value) for value in (*wave, *power)):
        raise ValueError("The OSA returned non-finite spectrum values")
    return wave, power


def read_output_spectrum(read_spectrum: Callable[[int], tuple], output: int) -> tuple[list[float], list[float]]:
    """Read and validate the spectrum for a zero-based chip ``output``."""
    return trim_spectrum(read_spectrum(output + 1))


def validate_sweeps(value) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"OSA sweeps per reading must be a whole number from 1 to {MAX_SWEEPS}") from error
    if str(value).strip() != str(count) or not 1 <= count <= MAX_SWEEPS:
        raise ValueError(f"OSA sweeps per reading must be a whole number from 1 to {MAX_SWEEPS}")
    return count


def average_spectra(spectra) -> tuple[list[float], list[float]]:
    """Average matching OSA traces as linear power, then return dBm samples."""
    traces = list(spectra)
    if not traces:
        raise ValueError("No OSA spectra were acquired for averaging")
    wavelengths = list(traces[0][0])
    if not wavelengths or any(len(wave) != len(wavelengths) or len(power) != len(wavelengths)
                              for wave, power in traces):
        raise ValueError("OSA sweeps have different trace lengths")
    for wave, _ in traces[1:]:
        if any(not math.isclose(first, later, rel_tol=0, abs_tol=1e-6)
               for first, later in zip(wavelengths, wave)):
            raise ValueError("OSA wavelength grid changed between sweeps")
    averaged = [10 * math.log10(math.fsum(10 ** (power[index] / 10) for _, power in traces) / len(traces))
                for index in range(len(wavelengths))]
    return wavelengths, averaged


def acquire_averaged_spectrum(fetch: Callable[[int], tuple], count: int,
                              first_fresh_sweeps: int = FRESH_SWEEPS,
                              on_sweep: Callable[[int, int], None] | None = None) -> tuple[list[float], list[float]]:
    """``fetch(fresh_sweeps)`` ``count`` times, the first after a change, then average their power."""
    count = validate_sweeps(count)
    spectra = []
    for index in range(1, count + 1):
        spectra.append(fetch(first_fresh_sweeps if index == 1 else 1))
        if on_sweep is not None:
            on_sweep(index, count)
    return average_spectra(spectra) if count > 1 else spectra[0]


def osa_reader(host, port, sweeps, simulated=None):
    """``read(output=None, on_sweep=None)`` returning one averaged (wave, power) trace.

    ``simulated(output)`` stands in for the OSA in Simulate mode (``host`` None); without it
    the generic simulated trace is used. ``output`` is only passed on to ``simulated``.
    """
    sweeps = validate_sweeps(sweeps)

    def read(output=None, on_sweep=None):
        if host is None:
            fetch = (lambda fresh: simulated(output)) if simulated else (lambda fresh: osa.simulated_spectrum())
        else:
            fetch = lambda fresh: osa.get_spectrum(host, port, fresh_sweeps=fresh)
        return acquire_averaged_spectrum(fetch, sweeps, FRESH_SWEEPS, on_sweep)

    return read
