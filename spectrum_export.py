"""CSV export for all displayed OSA traces."""

import csv
from datetime import datetime

from calibration_scan import SpectrumScanPoint
from osa import SPURIOUS_TAIL_POINTS


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


def write_crosstalk_csv(path, data, source, description="", response_unit=""):
    """Export voltage responses and, for OSA scans, every acquired spectrum."""
    if not data or not any(data.values()):
        raise ValueError("There is no crosstalk dataset to export")
    with open(path, "w", encoding="utf-8", newline="") as file:
        file.write(f"# Crosstalk {source} scan exported {datetime.now().isoformat()}\n")
        file.write(f"# Response power: {'OSA spectrum peak and linear-bin mean [dBm]' if source == 'OSA' else 'Arduino calibrated power'}\n")
        file.write(f"# Response unit: {response_unit or ('dBm' if source == 'OSA' else 'unspecified')}\n")
        if source == "OSA":
            file.write(f"# Final OSA bins discarded per trace: {SPURIOUS_TAIL_POINTS}\n")
            file.write("# Mean: average of linear mW values across retained bins, converted to dBm\n")
        for line in description.splitlines() or [""]:
            file.write(f"# Description: {line}\n")
        writer = csv.writer(file)
        if source == "OSA":
            writer.writerow(["switch", "voltage_v", "peak_power_dbm", "mean_power_dbm",
                             "wavelength_nm", "spectrum_power_dbm"])
        else:
            writer.writerow(["switch", "voltage_v", "response_power", "wavelength_nm", "spectrum_power_dbm"])
        for name, points in data.items():
            for point in points:
                if isinstance(point, SpectrumScanPoint):
                    writer.writerows((name, point.voltage, point.power, point.mean_power_dbm, wavelength, power)
                                     for wavelength, power in zip(point.wavelength_nm, point.spectrum_dbm))
                else:
                    writer.writerow((name, point.voltage, point.power, "", "", "") if source == "OSA"
                                    else (name, point.voltage, point.power, "", ""))
