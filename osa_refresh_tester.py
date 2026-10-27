"""Standalone OSA buffer refresh tester.

Polls ``:CUR:BUFF?`` back to back over one connection, timestamps every change
of the trace buffer (one finished sweep) and overlays each new trace on a live
plot, older traces fading out.  Close the window or press Ctrl+C to stop; a
summary of poll and refresh timing is printed on exit.

    python osa_refresh_tester.py                     # lab OSA, C-band view
    python osa_refresh_tester.py --xlim 1250 1650    # full range
    python osa_refresh_tester.py --simulate          # no hardware
"""

import argparse
import math
import random
import socket
import statistics
import threading
import time

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

from benes.hardware import osa


class OsaConnection:
    def __init__(self, host, port, timeout, curve):
        self.connection = socket.create_connection((host, port), timeout=timeout)
        self.stream = self.connection.makefile("rwb")
        self.write("*REM")
        self.identity = self.query("*IDN?")
        self.write(f":CUR:SELECT {curve}")
        self.query(":CUR:SIZE?")
        if self.query("STAT:ACQ?") == "STOPPED":
            self.write("KEY START")

    def write(self, command):
        self.stream.write(command.encode("ascii") + b"\n")
        self.stream.flush()

    def query(self, command):
        self.write(command)
        reply = self.stream.readline()
        if not reply.endswith(b"\n"):
            raise OSError(f"OSA closed the connection during {command}")
        return reply.decode("ascii").strip()

    def fetch(self):
        return self.query(":CUR:BUFF?")

    def scale(self):
        return tuple(float(self.query(f":CUR:{name}?")) for name in ("XOFF", "XSCale", "YOFF", "YSCale"))

    def close(self):
        self.connection.close()


class SimulatedOsa:
    """Republishes a noisy trace every ``sweep_s``, with a realistic query delay."""

    identity = "Simulated OSA"

    def __init__(self, sweep_s=2.0, query_s=0.05, points=4001):
        self.sweep_s, self.query_s, self.points = sweep_s, query_s, points
        self.started = time.monotonic()
        self.sweep = -1
        self.buffer = ""

    def fetch(self):
        time.sleep(self.query_s)
        sweep = int((time.monotonic() - self.started) / self.sweep_s)
        if sweep != self.sweep:
            self.sweep = sweep
            centre = self.points * 0.2 + random.uniform(-3, 3)
            raw = [round(-6000 + 4500 * math.exp(-((index - centre) / 40) ** 2) + random.gauss(0, 30))
                   for index in range(self.points)]
            data = "".join(f"{value & 0xFFFF:04X}" for value in raw)
            self.buffer = f"#{len(str(len(data)))}{len(data)}{data}"
        return self.buffer

    def scale(self):
        return 181.692, (239.834 - 181.692) / (self.points - 1), 0.0, 0.01

    def close(self):
        pass


class Poller(threading.Thread):
    def __init__(self, source):
        super().__init__(daemon=True)
        self.source = source
        self.scale = source.scale()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.started = time.monotonic()
        self.polls = 0
        self.poll_times = []
        self.refresh_times = []
        self.new_traces = []
        self.error = None

    def run(self):
        try:
            previous = self.source.fetch()
            while not self.stop_event.is_set():
                before = time.monotonic()
                buffer = self.source.fetch()
                after = time.monotonic()
                with self.lock:
                    self.polls += 1
                    self.poll_times.append(after - before)
                if buffer == previous:
                    continue
                previous = buffer
                # The change happened somewhere during this query, so the
                # midpoint is the best estimate, accurate to half a poll.
                stamp = (before + after) / 2
                trace = osa.trim_spurious_tail(*osa.scale_curve(osa.parse_curve(buffer), *self.scale))
                with self.lock:
                    self.refresh_times.append(stamp)
                    self.new_traces.append(trace)
                intervals = self.intervals()
                print(f"refresh {len(self.refresh_times):4d}  t={stamp - self.started:8.3f} s"
                      + (f"  interval={intervals[-1]:.3f} s" if intervals else "  (first, partial)"))
        except Exception as error:  # surfaced in the plot title and on exit
            self.error = error
        finally:
            self.source.close()

    def intervals(self):
        with self.lock:
            times = list(self.refresh_times)
        return [right - left for left, right in zip(times, times[1:])]

    def take_traces(self):
        with self.lock:
            traces, self.new_traces = self.new_traces, []
        return traces

    def summary(self):
        with self.lock:
            polls, poll_times = self.polls, list(self.poll_times)
        elapsed = time.monotonic() - self.started
        lines = [f"polls {polls}  ({polls / elapsed:.1f}/s)"]
        if poll_times:
            lines.append(f"query {statistics.mean(poll_times) * 1000:.1f} ms avg, "
                         f"{min(poll_times) * 1000:.1f}-{max(poll_times) * 1000:.1f} ms")
        intervals = self.intervals()
        if intervals:
            spread = statistics.stdev(intervals) if len(intervals) > 1 else 0.0
            lines.append(f"refresh {statistics.mean(intervals):.3f} +/- {spread:.3f} s "
                         f"(min {min(intervals):.3f}, max {max(intervals):.3f}, n={len(intervals)})")
        else:
            lines.append("refresh: waiting for two buffer changes")
        return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="192.168.0.200")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--curve", type=int, default=1)
    parser.add_argument("--keep", type=int, default=20, help="number of traces kept on the plot")
    parser.add_argument("--xlim", type=float, nargs=2, default=(1530, 1565), metavar=("MIN_NM", "MAX_NM"))
    parser.add_argument("--simulate", action="store_true", help="use a fake OSA sweeping every 2 s")
    args = parser.parse_args()

    source = SimulatedOsa() if args.simulate else OsaConnection(args.host, args.port, args.timeout, args.curve)
    print(f"Connected: {source.identity}")
    poller = Poller(source)
    poller.start()

    figure, (spectrum_axes, interval_axes) = plt.subplots(
        2, 1, figsize=(10, 7), gridspec_kw={"height_ratios": (3, 1)})
    spectrum_axes.set_xlim(*args.xlim)
    spectrum_axes.set_xlabel("Wavelength (nm)")
    spectrum_axes.set_ylabel("Power (dBm)")
    spectrum_axes.grid(True, alpha=0.3)
    interval_axes.set_xlabel("Refresh #")
    interval_axes.set_ylabel("Interval (s)")
    interval_axes.grid(True, alpha=0.3)
    interval_line, = interval_axes.plot([], [], "o-", markersize=3)
    stats_text = spectrum_axes.text(0.01, 0.98, "", transform=spectrum_axes.transAxes, va="top",
                                    family="monospace", fontsize=9,
                                    bbox={"facecolor": "white", "alpha": 0.8, "edgecolor": "none"})
    lines = []

    def update(_frame):
        for wavelength, power in poller.take_traces():
            line, = spectrum_axes.plot(wavelength, power, linewidth=1)
            lines.append(line)
            while len(lines) > args.keep:
                lines.pop(0).remove()
            # Only the visible window sets the y range, so the C band is not
            # flattened by the noise floor elsewhere in the sweep.
            visible = [value for item_wavelength, value in zip(wavelength, power)
                       if args.xlim[0] <= item_wavelength <= args.xlim[1]]
            if visible:
                low, high = spectrum_axes.get_ylim() if len(lines) > 1 else (math.inf, -math.inf)
                spectrum_axes.set_ylim(min(low, min(visible) - 2), max(high, max(visible) + 2))
        for age, line in enumerate(reversed(lines)):
            line.set_color("tab:red" if age == 0 else "tab:blue")
            line.set_alpha(1.0 if age == 0 else max(0.08, 0.6 * (1 - age / args.keep)))
            line.set_zorder(3 if age == 0 else 2)

        intervals = poller.intervals()
        interval_line.set_data(range(1, len(intervals) + 1), intervals)
        if intervals:
            interval_axes.set_xlim(0, len(intervals) + 1)
            interval_axes.set_ylim(0, max(intervals) * 1.2)
        summary = poller.summary()
        if poller.error:
            summary.append(f"ERROR: {poller.error}")
        stats_text.set_text("\n".join(summary))
        return []

    animation = FuncAnimation(figure, update, interval=50, cache_frame_data=False)
    figure.canvas.manager.set_window_title("OSA refresh tester")
    figure.tight_layout()
    try:
        plt.show()
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop_event.set()
        del animation
        print("\n".join(["", "Summary:"] + poller.summary()))
        if poller.error:
            print(f"Stopped by error: {poller.error!r}")


if __name__ == "__main__":
    main()
