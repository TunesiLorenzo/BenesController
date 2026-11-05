"""Live OSA capture saves complete traces while the user stops the run."""

import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from benes.gui.spectra import SpectraMixin


class ImmediateThread:
    def __init__(self, target, daemon):
        self.target = target

    def start(self):
        self.target()


class LiveOSATests(unittest.TestCase):
    def test_records_each_trace_and_stops_after_current_read(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "live.csv"
            seen = []
            states = []
            app = SimpleNamespace(
                measurement_busy=lambda: False,
                osa_button=SimpleNamespace(instate=lambda _: False),
                osa_connection=lambda: ("127.0.0.1", 8002),
                set_osa_reading=states.append,
                osa_live_stop_button=SimpleNamespace(configure=lambda **kwargs: None),
                begin_activity=lambda *args: None,
                after=lambda delay, callback: callback(),
                show_osa_live_trace=lambda wave, power, n, stamp: seen.append((n, wave, power)),
                finish_osa_live=lambda n, filename, error: states.append((n, filename, error)),
            )

            def spectrum(host, port, fresh_sweeps):
                self.assertEqual(fresh_sweeps, 2 if not seen else 1)
                if seen:
                    app.osa_live_stop.set()
                return [1550, 1551, 1552, 1553], [-10, -11, -12, -13]

            with patch("benes.gui.spectra.filedialog.asksaveasfilename", return_value=str(path)), \
                 patch("benes.gui.spectra.threading.Thread", ImmediateThread), \
                 patch("benes.gui.spectra.osa.get_spectrum", side_effect=spectrum):
                SpectraMixin.start_osa_live(app)

            with path.open(newline="", encoding="utf-8") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(len(rows), 4)  # Two bins per trace after the tail trim.
            self.assertEqual([row["trace"] for row in rows], ["1", "1", "2", "2"])
            self.assertEqual([item[0] for item in seen], [1, 2])
            self.assertEqual(states[-1], (2, str(path), None))


if __name__ == "__main__":
    unittest.main()
