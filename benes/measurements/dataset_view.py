"""Load the six saved OSA traces for a dataset scan preview."""

import csv
import json
import math
from datetime import datetime
from functools import lru_cache
from itertools import permutations, product
from pathlib import Path

from benes.core.network import BenesNetwork


PERMUTATION_NUMBERS = {permutation: number for number, permutation in
                       enumerate(permutations(range(1, 7)), start=1)}


def format_transmission_share(fraction):
    """Display routed power divided by this wavelength's six-output power."""
    if fraction is None or fraction <= 0:
        return "—"
    return f"{10 * math.log10(fraction):.2f} dB ({100 * fraction:.1f}%)"


@lru_cache(maxsize=1)
def _path_alternatives():
    """Number full MZI state maps in the network's stable bit order."""
    network = BenesNetwork()
    names = tuple(network.switches)
    grouped = {}
    for bits in product((False, True), repeat=len(names)):
        permutation = tuple(output + 1 for output in network.mapping(dict(zip(names, bits))))
        grouped.setdefault(permutation, []).append(bits)
    return names, {(permutation, bits): (index, len(choices))
                   for permutation, choices in grouped.items()
                   for index, bits in enumerate(choices, start=1)}


def saved_scan_entry(folder):
    """Describe one six-output scan with stable permutation and path numbers."""
    folder = Path(folder)
    if not all((folder / filename).is_file() for filename in
               ("wdm_scan.json", "wdm_summary.csv", "wdm_spectra.csv")):
        return None
    context = json.loads((folder / "wdm_scan.json").read_text(encoding="utf-8"))
    permutation = tuple(int(value) for value in context["permutation"])
    number = PERMUTATION_NUMBERS.get(permutation)
    if number is None or len(context.get("outputs", ())) != 6:
        return None
    alternative = None
    states = context.get("switch_states")
    if isinstance(states, dict):
        names, alternatives = _path_alternatives()
        if all(states.get(name) in ("cross", "bar") for name in names):
            bits = tuple(states[name] == "cross" for name in names)
            alternative = alternatives.get((permutation, bits))
    path_text = "" if alternative is None else f" · Path {alternative[0]}/{alternative[1]}"
    label = (f"P{number:03d} [{' '.join(map(str, permutation))}]{path_text}"
             f" · {folder.name[:22]}")
    return {"label": label, "folder": folder, "permutation": permutation,
            "number": number, "alternative": alternative,
            "stamp": folder.name[:22]}


def saved_dataset_scans(root):
    """Index earlier six-output scans in the linked dataset without loading traces."""
    entries = []
    for context_path in (Path(root) / "Measurements").glob("*/wdm_scan.json"):
        try:
            entry = saved_scan_entry(context_path.parent)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
        if entry is not None:
            entries.append(entry)
    return sorted(entries, key=lambda item: (item["number"],
                                             item["alternative"][0] if item["alternative"] else 0,
                                             item["stamp"]))


def load_output_spectra(folder):
    """Return (output, routed input, target wavelength, wave, power) for a scan."""
    folder = Path(folder)
    context = json.loads((folder / "wdm_scan.json").read_text(encoding="utf-8"))
    outputs = context["outputs"]
    if len(outputs) != 6:
        raise ValueError("The saved scan does not contain six outputs")
    traces = {int(item["output"]): (int(item["routed_input"]), float(item["wavelength_nm"]), [], [])
              for item in outputs}
    if set(traces) != set(range(1, 7)):
        raise ValueError("The saved scan has missing or duplicate outputs")
    with (folder / "wdm_spectra.csv").open(encoding="utf-8", newline="") as file:
        rows = csv.reader(line for line in file if not line.startswith("#"))
        header = next(rows)
        columns = {}
        for output in traces:
            prefix = f"Out {output} ("
            matches = [index for index, name in enumerate(header)
                       if name.startswith(prefix) and name.endswith(" [dBm]")]
            if len(matches) != 1:
                raise ValueError(f"The saved spectrum for output {output} is missing or ambiguous")
            columns[output] = matches[0]
        for row in rows:
            wavelength = float(row[0])
            for output, column in columns.items():
                if column < len(row) and row[column]:
                    traces[output][2].append(wavelength)
                    traces[output][3].append(float(row[column]))
    if any(not trace[2] for trace in traces.values()):
        raise ValueError("A saved output spectrum is empty")
    return [(output, *traces[output]) for output in range(1, 7)]


def load_target_summary(folder):
    """Return the permutation and measured target transmission at every output."""
    folder = Path(folder)
    context = json.loads((folder / "wdm_scan.json").read_text(encoding="utf-8"))
    permutation = tuple(int(output) for output in context["permutation"])
    if len(permutation) != 6 or set(permutation) != set(range(1, 7)):
        raise ValueError("The saved scan has an invalid six-port permutation")
    target_input = {output: input_port for input_port, output in enumerate(permutation, start=1)}
    targets = {int(item["output"]): item for item in context["outputs"]}
    if set(targets) != set(range(1, 7)):
        raise ValueError("The saved scan does not contain six distinct output targets")
    with (folder / "wdm_summary.csv").open(encoding="utf-8", newline="") as file:
        reader = csv.DictReader(line for line in file if not line.startswith("#"))
        powers = {int(row["output"]): row for row in reader}
    if set(powers) != set(range(1, 7)):
        raise ValueError("The saved scan summary does not contain six outputs")
    rows = []
    for output in range(1, 7):
        input_port = target_input[output]
        target = targets[output]
        measured = powers[output]
        if int(target["routed_input"]) != input_port or int(measured["routed_input"]) != input_port:
            raise ValueError(f"The target input for output {output} disagrees with the permutation")
        line_powers = []
        target_power = None
        for measured_output, item in targets.items():
            line = item.get("lines", {}).get(str(input_port))
            if line is not None:
                power_mw = 10 ** (float(line["peak_dbm"]) / 10)
                line_powers.append(power_mw)
                if measured_output == output:
                    target_power = power_mw
        total_power = math.fsum(line_powers)
        share = target_power / total_power if target_power is not None and total_power else None
        rows.append((output, input_port, float(target["wavelength_nm"]),
                     float(measured["peak_power_dbm"]) if measured["peak_power_dbm"] else None,
                     float(measured["window_mean_dbm"]) if measured["window_mean_dbm"] else None,
                     share))
    return permutation, rows


def load_total_power_history(root):
    """Time series of each input line summed over all six output ports.

    Peak powers are stored in dBm, so sum them as mW before converting back.
    Incomplete or simulated scans are excluded from the physical time series.
    """
    history = {input_port: [] for input_port in range(1, 7)}
    for path in (Path(root) / "Measurements").glob("*/wdm_scan.json"):
        try:
            scan = json.loads(path.read_text(encoding="utf-8"))
            if scan.get("simulated_osa"):
                continue
            stamp = datetime.fromisoformat(scan["started_at"])
            if stamp.tzinfo is None:
                continue
            assignment = scan["assignment"]
            outputs = scan["outputs"]
            if len(outputs) != 6 or {int(item["output"]) for item in outputs} != set(range(1, 7)):
                continue
            for input_port in history:
                wavelength = assignment.get(str(input_port))
                if wavelength is None:
                    continue
                lines = [item.get("lines", {}).get(str(input_port)) for item in outputs]
                if any(line is None or line.get("peak_dbm") is None for line in lines):
                    continue
                powers = [float(line["peak_dbm"]) for line in lines]
                total_mw = math.fsum(10 ** (power / 10) for power in powers)
                if total_mw > 0 and math.isfinite(total_mw):
                    history[input_port].append((stamp, float(wavelength), 10 * math.log10(total_mw)))
        except (OSError, ValueError, KeyError, TypeError, OverflowError, json.JSONDecodeError):
            continue
    for points in history.values():
        points.sort(key=lambda item: item[0])
    return history
