"""CSV export for all displayed OSA traces."""

import csv
from datetime import datetime


def write_spectra(path, traces, description=""):
    if not traces:
        raise ValueError("There are no traces to export")
    columns, wavelengths = [], set()
    for name, wave, power in traces:
        if len(wave) != len(power):
            raise ValueError(f"Trace {name!r} has different wavelength and power lengths")
        column = dict(zip(wave, power))
        if len(column) != len(wave):
            raise ValueError(f"Trace {name!r} has repeated wavelengths")
        columns.append(column)
        wavelengths.update(wave)
    first_wave = traces[0][1]
    if all(wave == first_wave for _, wave, _ in traces):
        axis = first_wave
    else:
        axis = sorted(wavelengths, reverse=len(first_wave) > 1 and first_wave[0] > first_wave[-1])
    with open(path, "w", encoding="utf-8", newline="") as file:
        file.write(f"# OSA spectra exported {datetime.now().isoformat()}\n")
        for line in description.splitlines() or [""]:
            file.write(f"# Description: {line}\n")
        writer = csv.writer(file)
        writer.writerow(["wavelength_nm"] + [f"{name} [dBm]" for name, _, _ in traces])
        writer.writerows([wave] + [column.get(wave, "") for column in columns] for wave in axis)
