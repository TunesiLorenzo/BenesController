"""Voltage-board pulses with a continuously read Arduino optical trace."""

from __future__ import annotations

import csv
import math
import sys
import threading
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from pathlib import Path


MAX_TRACE_SAMPLES = 2_000_000
READ_CHUNK_S = 0.05
LIVE_PLOT_WINDOW_S = 10.0
MONITOR_PIN = 8
# Windows timer waits can overshoot by ~16 ms, so spin for the final stretch.
SPIN_WINDOW_S = 0.02
# A short GIL switch interval keeps the edge thread responsive during capture.
EDGE_SWITCH_INTERVAL_S = 0.0002


@dataclass
class ImpulseTrace:
    mode: str
    switch: str
    low_v: float
    high_v: float
    unit: str
    period_s: float | None = None
    high_time_s: float | None = None
    pulse_s: float | None = None
    measure_s: float | None = None
    lead_in_s: float = 0.2
    samples: list[tuple[float, int, int, float]] = field(default_factory=list)
    voltage_events: list[tuple[float, float]] = field(default_factory=list)
    dummy_events: list[tuple[float, float]] = field(default_factory=list)
    stopped: bool = False
    time_reference: str = "host recording start"
    sample_period_s: float | None = None
    skipped_bytes: int = 0
    capture_elapsed_s: float = 0.0


def received_pair_rate(samples, window_s=None):
    """Estimate paired A0/A1 samples received per second from trace timestamps."""
    if len(samples) < 2:
        return 0.0
    start = 0 if window_s is None else bisect_left(
        samples, samples[-1][0] - window_s, key=lambda sample: sample[0])
    if len(samples) - start < 2:
        return 0.0
    span = samples[-1][0] - samples[start][0]
    return (len(samples) - start - 1) / span if span > 0 else 0.0


def plot_trace_samples(samples, window_s=None, max_bins=500, optical_average_s=0,
                       time_range=None):
    """Average optical values for display, then retain both channels' extrema."""
    import numpy as np

    if not samples:
        return []
    start = 0
    end = len(samples)
    if window_s is not None:
        start = bisect_left(samples, samples[-1][0] - window_s,
                            key=lambda sample: sample[0])
    if time_range is not None:
        start = max(start, bisect_left(samples, time_range[0], key=lambda sample: sample[0]))
        end = bisect_right(samples, time_range[1], key=lambda sample: sample[0])
    if start >= end:
        return []
    count = end - start
    electrical = np.fromiter((sample[2] for sample in samples[start:end]), float, count)
    if optical_average_s > 0:
        history_start = bisect_left(samples, samples[start][0] - optical_average_s,
                                    key=lambda sample: sample[0])
        history = samples[history_start:end]
        times = np.fromiter((sample[0] for sample in history), float, len(history))
        power = np.fromiter((sample[3] for sample in history), float, len(history))
        totals = np.concatenate(([0.0], np.cumsum(power)))
        right = np.arange(start - history_start, len(history))
        left = np.searchsorted(times, times[right] - optical_average_s, side="left")
        optical = (totals[right + 1] - totals[left]) / (right - left + 1)
    else:
        optical = np.fromiter((sample[3] for sample in samples[start:end]), float, count)
    bin_size = max(1, math.ceil(count / max_bins))
    indices = {0, count - 1}
    full = count // bin_size * bin_size
    for values in (electrical, optical):
        if full:
            bins = values[:full].reshape(-1, bin_size)
            offsets = np.arange(0, full, bin_size)
            indices.update((offsets + bins.argmin(axis=1)).tolist())
            indices.update((offsets + bins.argmax(axis=1)).tolist())
        if full < count:
            indices.add(full + int(values[full:].argmin()))
            indices.add(full + int(values[full:].argmax()))
    if optical_average_s <= 0:
        return [samples[start + index] for index in sorted(indices)]
    return [samples[start + index][:3] + (float(optical[index]),) for index in sorted(indices)]


def validate_impulse_settings(mode, low_v, high_v, period_s=None, high_time_s=None,
                              pulse_s=None, measure_s=None, lead_in_s=0.2):
    values = (low_v, high_v, lead_in_s)
    if not all(math.isfinite(value) for value in values) or not all(0 <= value <= 5 for value in values[:2]):
        raise ValueError("Impulse voltages must stay within 0–5 V while Arduino A1 is connected")
    if low_v == high_v:
        raise ValueError("Low and high voltages must differ")
    if lead_in_s < 0:
        raise ValueError("Lead-in cannot be negative")
    if mode == "square":
        if period_s is None or high_time_s is None or not all(math.isfinite(v) for v in (period_s, high_time_s)):
            raise ValueError("Enter a finite period and high time")
        if not 0 < high_time_s < period_s:
            raise ValueError("High time must be positive and shorter than the period")
    elif mode == "single":
        if pulse_s is None or measure_s is None or not all(math.isfinite(v) for v in (pulse_s, measure_s)):
            raise ValueError("Enter a finite pulse and measurement duration")
        if pulse_s <= 0 or measure_s <= 0:
            raise ValueError("Pulse and measurement durations must be positive")
    else:
        raise ValueError("Unknown impulse mode")


def run_impulse_test(controller, meter, switch, mode, low_v, high_v, stop_event,
                     period_s=None, high_time_s=None, pulse_s=None, measure_s=None,
                     lead_in_s=0.2, on_progress=None):
    """Record raw optical samples while a second thread sends voltage edges."""
    validate_impulse_settings(mode, low_v, high_v, period_s, high_time_s,
                              pulse_s, measure_s, lead_in_s)
    if not controller.connected or controller.settings["dry_run"]:
        raise RuntimeError("Connect the physical voltage board first")
    if controller.settings["mapping"].get(switch) is None:
        raise ValueError(f"Assign a voltage-board pin to {switch} first")
    if MONITOR_PIN in controller.settings["mapping"].values():
        raise ValueError("Voltage-board pin 8 is mapped to an MZI; leave it free for the electrical monitor")
    if not meter.connected:
        raise RuntimeError("Connect the Arduino power meter first")
    if meter.settings.get("stream_format") not in ("raw", "dual"):
        raise RuntimeError("Select the raw A0/A1 Arduino stream in Boards & measurement")
    trace = ImpulseTrace(mode, switch, low_v, high_v, meter.settings["unit"],
                         period_s, high_time_s, pulse_s, measure_s, lead_in_s)
    raw_stream = meter.settings["stream_format"] == "raw"
    if raw_stream:
        meter.start_stream()
    origin = meter.stream_start_time if raw_stream else time.perf_counter()
    if raw_stream:
        trace.time_reference = "pair index x measured pair period; host start is approximate"
    drive_error = []
    # Suppresses live redraws while a single pulse is high, so plotting cannot delay its end.
    pulse_active = threading.Event()

    def drive(voltage):
        target_time, mirror_time = controller.drive_with_mirror(switch, voltage, MONITOR_PIN)
        trace.voltage_events.append((target_time - origin, voltage))
        trace.dummy_events.append((mirror_time - origin, voltage))

    def wait_until(deadline):
        """Return True if stopped; edges are scheduled on absolute deadlines."""
        coarse = deadline - time.perf_counter() - SPIN_WINDOW_S
        if coarse > 0 and stop_event.wait(coarse):
            return True
        while time.perf_counter() < deadline:
            if stop_event.is_set():
                return True
            time.sleep(0)
        return stop_event.is_set()

    def drive_pattern():
        try:
            drive(low_v)
            first_high = time.perf_counter() + lead_in_s
            if mode == "single":
                if not wait_until(first_high - SPIN_WINDOW_S):
                    pulse_active.set()
                    if not wait_until(first_high):
                        drive(high_v)
                        wait_until(first_high + pulse_s)
            else:
                next_high = first_high
                while not wait_until(next_high):
                    drive(high_v)
                    if wait_until(next_high + high_time_s):
                        break
                    drive(low_v)
                    next_high += period_s
        except Exception as error:
            drive_error.append(error)
            stop_event.set()
        finally:
            try:
                drive(low_v)
            except Exception as error:
                drive_error.append(error)
                stop_event.set()
            pulse_active.clear()

    switch_interval = sys.getswitchinterval()
    sys.setswitchinterval(EDGE_SWITCH_INTERVAL_S)
    driver = threading.Thread(target=drive_pattern, daemon=True)
    driver.start()
    first_chunk = not raw_stream
    last_update = origin
    capture_error = None
    try:
        while not stop_event.is_set():
            remaining = None if measure_s is None else origin + measure_s - time.perf_counter()
            if remaining is not None and remaining <= 0:
                break
            duration = min(READ_CHUNK_S, remaining) if remaining is not None else READ_CHUNK_S
            raw, timing = meter.read_timed_samples(duration, reset_buffer=first_chunk)
            first_chunk = False
            if not raw and not trace.samples and time.perf_counter() - origin > 1.0:
                raise RuntimeError("No A0/A1 samples received; check the Arduino firmware and baud rate")
            if raw:
                start = timing["start_time"]
                span = timing["end_time"] - start
                scale = meter.settings["scale"]
                offset = meter.settings["offset"]
                period = timing.get("sample_period_s")
                if raw_stream and period is not None:
                    trace.sample_period_s = period
                    trace.skipped_bytes = timing.get("skipped_bytes", 0)
                    # Continue from the previous chunk so live times stay sorted while
                    # the measured period settles; the whole trace is retimed at the end.
                    last = trace.samples[-1][0] if trace.samples else -period
                    trace.samples.extend(
                        (last + (index + 1) * period,
                         optical, electrical, optical * scale + offset)
                        for index, (optical, electrical) in enumerate(raw)
                    )
                else:
                    trace.samples.extend(
                        (start + (index + 0.5) * span / len(raw) - origin,
                         optical, electrical, optical * scale + offset)
                        for index, (optical, electrical) in enumerate(raw)
                    )
                if len(trace.samples) > MAX_TRACE_SAMPLES:
                    raise RuntimeError("Trace reached the 2,000,000-sample limit; export the partial trace")
            now = time.perf_counter()
            trace.capture_elapsed_s = now - origin
            if on_progress is not None and now - last_update >= 0.2 and not pulse_active.is_set():
                on_progress(trace)
                last_update = now
        trace.stopped = stop_event.is_set()
    except Exception as error:
        capture_error = error
        trace.stopped = True
    finally:
        stop_event.set()
        driver.join()
        sys.setswitchinterval(switch_interval)
        if raw_stream:
            try:
                meter.stop_stream()
            except Exception as error:
                capture_error = capture_error or error
            if trace.sample_period_s is not None:
                period = trace.sample_period_s
                trace.samples = [(index * period,) + sample[1:]
                                 for index, sample in enumerate(trace.samples)]
        if on_progress is not None:
            on_progress(trace)
    if capture_error is not None:
        raise RuntimeError(f"Optical capture failed: {capture_error}") from capture_error
    if drive_error:
        raise RuntimeError(f"Voltage pulse failed: {drive_error[0]}") from drive_error[0]
    return trace




def write_impulse_csv(path, trace: ImpulseTrace):
    if not trace.samples:
        raise ValueError("There are no optical samples to export")
    with Path(path).open("w", encoding="utf-8", newline="") as file:
        if trace.sample_period_s is not None:
            file.write("# Sample times are pair index x the pair period measured over the whole run.\n")
            file.write("# Values are 7-bit (0-127); A1 is read right after A0 in each row.\n")
            file.write("# USB latency makes alignment to host command timestamps approximate.\n")
        else:
            file.write("# Sample times are estimated uniformly within each Arduino read chunk.\n")
        for key in ("mode", "switch", "low_v", "high_v", "period_s", "high_time_s",
                    "pulse_s", "measure_s", "lead_in_s", "unit", "stopped",
                    "time_reference", "sample_period_s", "skipped_bytes", "capture_elapsed_s"):
            file.write(f"# {key}: {getattr(trace, key)}\n")
        if trace.skipped_bytes:
            file.write(f"# {trace.skipped_bytes} byte(s) were skipped to resynchronize; "
                       "samples were lost there and later times may shift slightly.\n")
        file.write("# Command times are estimated from the serial write start and 115200-baud wire time.\n")
        writer = csv.writer(file)
        writer.writerow(("time_s", "optical_adc_a0", "electrical_adc_a1", "optical_power",
                         "target_command_v", "dummy_pin8_command_v"))
        target_events = sorted(trace.voltage_events)
        dummy_events = sorted(trace.dummy_events)
        target_index = dummy_index = 0
        target_v = dummy_v = trace.low_v
        for sample_time, optical, electrical, power in trace.samples:
            while target_index < len(target_events) and target_events[target_index][0] <= sample_time:
                target_v = target_events[target_index][1]
                target_index += 1
            while dummy_index < len(dummy_events) and dummy_events[dummy_index][0] <= sample_time:
                dummy_v = dummy_events[dummy_index][1]
                dummy_index += 1
            writer.writerow((sample_time, optical, electrical, power, target_v, dummy_v))
