# Beneš 6×6 route explorer

A standalone Python/Tkinter desktop GUI. Run with Python 3.10 or later:

```powershell
python benes_gui.py
```

No pip packages are required. Tkinter is included in the standard Windows Python installer.
Physical board control additionally needs the `pyserial` dependency used by the
sibling VoltageTuner project.

## Project layout

```text
benes_gui.py          launcher (also: python -m benes); the PyInstaller entry point
benes/
  paths.py            app_dir() for configs and Results/, sibling-project lookup
  core/               network topology/solver, heater current limits
  hardware/           voltage board, Arduino meter, OSA, JDSU output switch, Newport lasers
  measurements/       scans, calibration, WDM comb, impulse test, datasets, CSV export
  gui/
    app.py            main window: shared state, notebook, lifecycle
    theme.py          colours and ttk style
    widgets.py        scrollable containers
    routing.py, hardware.py, spectra.py, mapping.py, crosstalk.py,
    calibration.py, wdm.py, dataset.py, impulse.py
                      one mixin per tab or dialog, combined into BenesApp
tests/                unittest suite
```

Settings (`benes_*_config.json`) and `Results/` stay in the project folder, or beside
the exe when frozen.

The status strip below the window title stays visible across tabs during OSA
reads, configuration evaluation, calibration, dataset acquisition, crosstalk
and voltage scans, and impulse capture. It names the current step and shows a
counted progress bar when the work has a known number of measurements; other
operations show an animated bar.

## Board control and calibration

The GUI uses the existing sibling `../VoltageTuner/board.py` implementation, so
it speaks the same `DC <pin> <voltage>` serial protocol. In the **Voltage board**
row, choose the serial port and baud rate (or **Dry run** to log commands without
hardware), then click **Connect**. The saved voltage for every mapped MZI is sent
on connection.

Each switch in the diagram shows its assigned physical pin and a voltage field.
Type a voltage and press Enter to store it and, when connected, send it to that
pin. **MZI ↔ pin mapping / calibration…** maps the 12 MZIs to unique pins 0–15
and optionally records straight/cross calibration voltages. Once those values are
recorded, toggling a switch or applying a route sends the voltage for its logical
state. Mapping, connection settings, voltages, and calibration points are kept in
`benes_voltage_config.json` beside the application. It is created on first change.
The route diagram starts with every MZI crossed, matching the 0 V physical state.
New configurations leave board pin 8 free for the optional legacy dual-channel
impulse monitor. Existing pin mappings are preserved.
The default scan limits are 0–5 V. A voltage outside this range requires a
confirmation before the board sends it; the prompt reminds you to disconnect
the Arduino sampler from the voltage-board signal and check the heater current
limit.

The UI is organized by workflow: **Routing**, **Hardware & OSA**, **MZI
calibration**, **Thermal crosstalk**, and **Impulse test**. Hardware & OSA separates the voltage board, OSA
and its output selector, Arduino meter, and HP 8152A meter into sub-tabs so every control remains
visible on a laptop display.

The **HP 8152A meter** hardware page streams triggered GPIB readings into a
rolling plot and recent-samples table. Set the resource, channel, wavelength,
range, and requested GPIB reads per second, then press **Start stream**. The
default request is 5 Hz; **Apply read rate** changes the cadence while streaming
(0.1–100 Hz). The status reports the achieved transfer rate, which may be lower
than requested. **Stop** closes the connection and restores the meter's previous
settings. Invalid or out-of-range readings appear as such in the table and as
gaps in the plot. The meter settings are shared with Thermal crosstalk, and the
stream must stop before a crosstalk meter test or scan can start. The GPIB read
cadence does not change the meter's 2/8 Hz optical low-pass filter.

Every tab and dialog stretches to fill its window and scrolls when the window
is smaller than its content. Vertical and horizontal scrollbars appear only
when needed. The mouse wheel scrolls the area under the pointer
(Shift+wheel scrolls horizontally). Dialogs open at their natural size,
capped to the screen. The mapping dialog keeps **Save**/**Cancel** visible
below its scrolling table. Plots rescale with the window down to about
480 × 320 px, then the tab scrolls instead.

## Newport 9016 laser modules

The **Newport lasers** tab connects to the 9016 at `GPIB0::19::INSTR` by
default. It reads the installed modules and shows separate **Laser current**
and **TEC temperature** output states and setpoints for each of the 16 slots.
Enter a laser current in mA or TEC temperature in °C and press its **Set**
button. When the corresponding output is off, Set selects constant current or
constant temperature mode if needed, writes the value, and verifies readback.
If the output is on in a different mode, turn it off before changing modes.
**Turn ON** and **Turn OFF** act on one output at a time. The laser and TEC
sections are highlighted green independently when their outputs are on.
**Refresh** rereads output states and setpoints.
**ALL CURRENT OFF** turns off every laser output while leaving TEC outputs
unchanged and reports any channel that could not be confirmed off.

**Detach** opens the controls in their own scrollable window; **Attach** or the
window close button returns them to the main tab. The connection remains open
while moving between windows. NI-VISA and `python -m pip install pyvisa` are
needed for the physical controller.

## OSA traces

The **OSA** row reads trace 1 from a VIAVI OSA 500RS. Choose **Overlay** or **New
window**, name each trace, and use **Export displayed datasets** in a plot window
to write every visible trace to CSV. The export dialog asks for an optional
description, which is included as `# Description:` comment line(s) at the top of
the file. Plotting needs matplotlib (`python -m pip install matplotlib`).

### OSA output selector

All six Benes output fibers are patched into a JDSU SC optical switch (GPIB),
whose common port feeds the OSA. The **OSA & output switch** hardware page
selects which output the OSA samples. Choosing an output there, or in the **OSA
output** picker on the routing tab, immediately closes the matching JDSU channel
(the two pickers stay in sync). **Route + read** routes and acquires one output;
**Read all outputs** acquires every mapped output sequentially—useful when
different signals are present at all inputs in parallel. Every automatic trace is
labelled with the current `In → Out` connection and the output/fiber number; all
traces can be exported together from the spectrum plot window.

**Selector setup…** holds the VISA resource (default `GPIB0::25::INSTR`; **List**
shows detected resources), an extra settling delay, **Dry run**, and the
**Output fiber → JDSU channel** table (default: output *n* → channel *n*; each
channel may be used once, `—` leaves an output unmapped). The mapping can be
edited and saved while connected. The switch must use its SC command set with
CR+LF termination: the application sends `CLOSE n`, waits for bit 2 of `CNB?`,
and verifies the channel with `CLOSE?`, as in the sister `Jdsu_switch` project.
After each switch move, reads wait for the OSA to finish two new sweeps (the first may have begun before the move); if the OSA was stopped, the read starts it and waits for that one sweep. A trace therefore never contains light from the previous output, whatever the sweep time. Settings and the mapping persist in `benes_osa_output_switch_config.json`.
Physical mode needs NI-VISA and `python -m pip install pyvisa`.

### Six-laser configuration evaluation

**Dataset coverage** holds the saved six-laser wavelengths, line window and
OSA/JDSU settling delay. **Evaluate Configuration** on the **Routing** tab
always scans all six output ports using that persisted configuration. Its result
window overlays the six spectra and includes a table for every wavelength:
linear-sum power across all outputs in dBm, expected routed output, transmission
there in dBm, and transmission and crosstalk ratios relative to the total in dB.
Set **Evaluate sweeps per output** under **Dataset coverage** and save the
six-laser configuration to average 1–16 fresh OSA sweeps at each output (default
10). Each trace is averaged in linear power before the evaluation and spectra
CSV are calculated; `wdm_scan.json` records the sweep count. More sweeps take
longer and reduce sample variation, but cannot correct a shifted coupling mean.

The saved plan records which comb wavelength enters each input. Configure the
wavelengths under **Dataset coverage**; an input left empty carries no line. The
routing tab lists
every `λ: In → Out` pair, both following the applied permutation. **Save plan**
keeps the plan in `benes_wdm_plan_config.json`; **Saved wavelength plans** stores
named plans to load later.

**Evaluate Configuration** routes the JDSU to all six outputs, waits the saved
**Settling delay**, reads the OSA (Hardware & OSA settings; **Simulate** gives a synthetic
comb) and measures every assigned comb line within **Line window ±** of its
wavelength. The delay should cover the switch settling and at least one complete
OSA sweep, so a trace holds no light from the previous output; the JDSU's own
settle check and its extra delay from **Selector setup…** still run first.
The separate Dataset Coverage batch controls can be stopped after the current
output and still save completed configurations.

The table lists, per output, the routed input and wavelength, the routed line's
peak power and wavelength, the linear mean over the window, and the isolation:
the routed peak minus the strongest other comb line at that output. Each scan is
saved in `Results/WDM_scan/<timestamp>/`:

- `wdm_summary.csv`: one row per output, as in the table.
- `wdm_line_matrix.csv`: peak power of every input's line (columns) at every output (rows).
- `wdm_spectra.csv`: all spectra.
- `wdm_scan.json`: permutation, wavelength plan, delays, JDSU map, switch states and voltages.

The **Export image, controls and data to results** button in the evaluation
window adds `configuration_evaluation.png`, `configuration_evaluation.csv`, and
`configuration_controls.csv` to that scan folder. The control CSV records each
switch's state and voltage captured when the scan started; the existing
`wdm_spectra.csv` holds the measured spectrum data (averaged for evaluation
when the sweep count is greater than one).

**Dataset Coverage** has separate actions to scan the active control state or
to run all 720 output permutations. Each action records the six routed pairs in
the linked dataset and restores the original control state when it finishes.
**Run all 4096 switch states** additionally measures every one of the 12-MZI
bar/cross configurations, including configurations that realize the same output
permutation. The live metrics table has one row per state and input wavelength:
total line power over the six outputs in dBm, and expected-port transmission
and crosstalk at the other outputs in dB relative to that total.

## Permutation measurement datasets

The **Dataset coverage** tab links a folder to a structured Benes measurement
dataset. Initialize it for the full 720 six-port permutations, or for just the
currently applied permutation. Each saved acquisition receives its own folder
under `Measurements/`, containing the raw CSV and a `measurement.json` sidecar
with the active permutation, selected inputs and outputs, source, comment, and
the exact atomic inputâ†’output connections it represents.

Choose one or more input and output ports before saving. This supports one-input/
one-output, many-input/one-output, and many-input/many-output acquisitions. For
an OSA save, acquire each selected output using **Route + read** or **Read all
outputs** first; the app stores one current spectrum per selected output in the
same CSV. For the Arduino source, read the current power first. The selection is
deliberate metadata: it states which parallel signals or routes the reading
represents rather than guessing from filenames.

Press **Analyse** at any time to calculate coverage. The panel reports the active
permutation's missing routed pairs and a compact list of other incomplete
configurations. `coverage_report.csv` in the dataset root holds the full
configuration-by-configuration list, so it remains practical for all 720
permutations.

## Arduino power meter

The same tab can use the Arduino detector stream from the sibling
`../ThorlabsControl/ArduinoSampler.py`. Upload
[ArduinoOptical.ino](ArduinoOptical/ArduinoOptical.ino), connect the detector
output to A0, and select **Stream: raw**. This sketch samples A0 only. Every
optical value has a second status byte whose low bit marks a sudden jump of at
least 24 of the 7-bit ADC counts. The bit is a transition heuristic, not a
readout of the power meter's actual range. A real optical step can also set it.
The reader discards marked samples, the following **Jump guard [ms]** (default
5 ms), and values at or above **Ceiling [ADC]** (default 80) before averaging.
If no clean samples remain, the read fails instead of returning a biased value.
The GUI reports clean/rejected counts and the observed raw frame rate. Set the
burst duration, scale, offset, and unit for the converted power value. Settings
are stored in `benes_power_meter_config.json`.

With the Uno's 16 MHz clock and ADC prescaler 32, the ADC's 13-clock normal
conversion is about 26 µs, a theoretical ceiling of 38,500 optical readings/s
before `analogRead` and serial overhead. Two bytes per reading need about
770 kbit/s including UART framing, so 1,000,000 baud can carry the default
stream. For more speed, change the sketch's ADC prescaler from 32 to 16 and
use 2,000,000 baud; the theoretical ADC ceiling becomes about 77,000/s, but
ADC accuracy falls further and the achieved rate must be checked in the GUI.
No baud change alone can make the ADC convert faster. The old
[ArduinoDualChannel.ino](ArduinoDualChannel/ArduinoDualChannel.ino) is archived;
its two-byte A0/A1 format is not accepted by the new raw optical decoder.
**Stream: dual** remains available for an older *six-byte framed* A0/A1 sketch,
and **Stream: legacy** for one-byte A0 firmware.

## Impulse test

The **Impulse test** tab can capture the same optical-only raw stream. The
second plot line shows the voltage commanded by the PC, not a measured
electrical waveform. The jump flags are retained in CSV and are not removed
from impulse traces, since real optical pulse edges can set the bit. The older
dual A0/A1 sketch can still capture a measured electrical trace when selected
as **Stream: dual**; only that mode needs the A1 voltage confirmation and free
voltage-board pin 8.

Set low and high voltages, then either play a square wave with **Period** and
**High time**, or take a **Single pulse** with pulse and total measurement
durations. **Lead-in** records the baseline before the first high level. Click
**Stop** to end a square wave; the selected MZI returns to the low voltage.
Impulse voltages remain limited to 0–5 V.
Time fields accept scientific notation in seconds: `1e-3` is 1 ms, `5e-2` is
50 ms, and `2e0` is 2 s. The display-average field uses milliseconds.
Fields start blank. Timing has no limits beyond being positive (and high time
shorter than the period). Edges are scheduled on absolute PC deadlines with a
spin-wait for the last 20 ms, so millisecond pulses and square waves work.
Pulses shorter than the board's command latency can stretch, and Windows/Python
scheduling can add jitter. Without A1, commanded times are host estimates.
The live plot shows the latest 10 seconds while
recording. It retains brief peaks when reducing points for display and shows
the full recording after Stop. Time ticks use seconds, ms, or µs as appropriate,
with contrasting labels. Use the plot toolbar to zoom into an edge or pan;
incoming data preserves the selected view. Optical display averaging is blank
or 0 for raw; enter a larger value to reduce displayed noise. CSV exports always
retain raw A0 samples and jump flags, independent of display averaging.
Both **Single pulse** and **Play square wave** use the same continuous stream.
**Export trace CSV** saves optical samples and commanded voltages. The sketch
has no sample clock, so the frame period is measured as host recording time
divided by frames received. After a run, every sample is placed at frame index
× that final period; USB delivery times do not determine the
spacing. Alignment to PC command times is approximate because the stream
contains no timestamps, and samples lost at a resync shift later times
slightly. A stalled stream raises an error in the reader. The trace limit is
2,000,000 optical samples.

To calibrate a switch from measured power, connect both the voltage board and
Arduino power meter, then open **MZI / pin mapping / calibration** and click
**Scan...** beside the switch. Save any new pin mapping before scanning. Choose
the lower voltage limit, upper voltage limit, step, and settling time; the scan measures one Arduino burst
at each voltage and restores the previous voltage when it finishes. The results
show the measured maximum and minimum. Assign each extreme to **Straight** or
**Cross**, then click **Save calibration**. The saved voltages are used when the
switch state changes. Keep the optical input and other switch states fixed
during a scan.

The mapping dialog also records each pin's heater: **R heater [Ω]**, **R total
[Ω]** (heater plus routing, pad to pad) and **J peak/V** (peak current density
per applied volt, in mA/µm²/V). **Import heater CSV...** reads the
`mzi_results.csv` written by the sibling GDS_heater project (for example
`../GDS_heater/Setups/`). Pick a row's device in the **Device** column to fill
its values. On import, rows without a device in the file are filled in file
order, so check the assignment before saving. J peak/V is the heater's
`J peak` divided by the file's `V [V]`; the routing density is not used, as the
limits apply to the heater.
**Default R [Ω]** is used as both resistances for pins with a blank R heater, and
a blank R total equals R heater. The current is I = V / R total and the heater
power is I²·R heater.

**Max V safe / break** gives the voltage at which the heater's peak current
density reaches 50 mA/µm² (safe) and 70 mA/µm² (break), with the currents at those
voltages. It turns amber when a saved straight/cross voltage exceeds the safe
limit and red when one reaches the break limit. The power-scan window also
shows the limits.

The limits are enforced on every voltage sent to the board: manual entry,
switch toggles and routes, scans, path calibration, impulse tests, and the
saved voltages sent on connect. Sign is ignored, since current flows either
way. A voltage at or above the break limit is always refused, with no
override. A voltage above the safe limit asks for confirmation first; each
approval covers that switch and voltage until the mapping is saved or the
application restarts. Scans, path calibration and impulse tests check their
whole voltage list before starting: one break-limit voltage stops the run, and
all over-safe voltages are approved in a single prompt. Switches without a
J peak/V value only have the 0–5 V confirmation.

The **X axis** selector above the Crosstalk-tab plots switches every response
plot, including rescans and the saved PNGs, between commanded voltage and
heater power in mW. Switches with no resistance stay in volts. All controls and
scan limits remain in volts, and `base_setting.json` records both resistances
used for each scanned switch. Runs saved before R total was recorded plot as
V²/R. The voltage board has no readback, so power is computed from the
commanded voltage, not measured.

The **Thermal crosstalk** tab can scan several switches in sequence. Select switches
with Ctrl or Shift, choose **HP 8152A** or **OSA**, enter the lower and upper
voltage limits, step, and settling time, then start the scan. The HP power meter
defaults to `GPIB0::23::INSTR`, channel A, 1550 nm and autoranging. Select a
fixed dBm range if the meter's automatic range changes are unsuitable. **Test
reading and GPIB read rate** measures 12 triggered readings and reports the
observed GPIB read rate and count of numeric values; no-voltage scan is started.
This transfer rate is not the optical sampling bandwidth: the meter's low-pass
filter is 8 Hz with filtering off or 2 Hz with filtering on. Its GPIB readings
cannot resolve a 50–100 Hz coupling oscillation. The meter
settings are saved in `benes_hp8152a_config.json` and restored on the instrument
after each run. For OSA scans, each voltage produces a spectrum after the
last two bins are discarded. The voltage response shows both peak power and
the mean of the retained bins, calculated in linear mW and converted to dBm.
Each switch gets its own
plot tab as measurements arrive, with interactive zoom, pan, and save controls;
OSA plots also overlay its spectra, colored by voltage. The **Export CSV
with comment** button saves the voltage response and every acquired OSA
spectrum in one dataset, with a comment at the top of the file. After each
switch is measured, its voltage-board output is set to 0 V, including when a
measurement fails. Keep the optical input and unselected switches fixed during
the scan.

Before starting a crosstalk scan, choose the input and destination in **Route
explorer**. You can also mark the output ports connected to the **Power Meter**
and **OSA** with separate colored rings. These port selections and the current
switch states are saved with each scan. Enter an optional scan comment in the
**Thermal crosstalk** tab. The application automatically creates
`Results/Crosstalk_scan/Port{input}-Port{output}_{dateTime}/` and saves
`base_setting.json`, a compact `base_path.png` diagram, one PNG plot per
measured switch, and `crosstalk_dataset.csv` with the comment. The folder uses
the selected output; the summary records the actual reached output separately.
Automatic path calibrations use the same layout under
`Results/Path_calibration/` instead. Calibrations saved before this change
remain in `Results/Crosstalk_scan/`.

**Open results…** above the plots replots any saved scan or calibration folder.
It shows a **Setup** tab with the run summary, comment, any error, and the base
path diagram, then one tab per switch with the response, OSA spectra, and any
range-change rescans. The **X axis** toggle works on loaded results. It uses
the heater resistances stored with the run, or the current ones when the run
predates them. **Export CSV with comment** re-exports the loaded dataset.

### MZI calibration

Every MZI is calibrated with a *probe*: one lit input, one read output, and
fixed states for the other MZIs, chosen so that the probed input's light
reaches that output in only one of the MZI's states (bright) and leaves
elsewhere in the other (dark). The other MZIs are cross at 0 V while
uncalibrated. Once an MZI is calibrated, the plan may hold it at its bar
voltage so that the detector can see more MZIs.

Choose the detection method under **Detection** in the MZI calibration tab:

- **OSA · all lasers on**: every input carries its own line from the saved
  six-laser plan. The JDSU selects the output and the OSA measures the probed
  input's line there, with the line that arrives in the other state as a
  reference. Every output is reachable through the JDSU.
- **Power meter · one laser at a time**: the Newport 9016 lights one input at a
  time, so the meter reading comes only from the probed input. This is much
  faster than the OSA. Choose **HP 8152A** (settings in Thermal crosstalk) or
  **Arduino** (settings in Hardware & OSA). Choose where the meter is connected:
  **JDSU common (any output)**, or a fixed **Output n**. Each input has its own
  Newport slot. The bays are reversed by default (slot 1 feeds input 6, slot 6
  feeds input 1). **Laser settle** is the wait after a laser turns on. The
  previous laser is always switched off before the next one turns on. All
  mapped TECs must be on. When the run ends, the lasers return to the on/off
  states they had before it started. The slot map and the settle time are
  stored in `benes_laser_bank_config.json`.

The **Calibrate** box and the **Probe** column of the MZIs table update whenever
these settings change. They show which MZIs are visible and how each one is
probed, for example `In 1 → Out 5, bright when cross; S2.1 at bar`. For an MZI
that cannot be seen, they show where to move the detector. A fixed output sees
8–10 MZIs: a last-stage MZI only reaches its own two outputs. Through the JDSU,
all 12 MZIs are visible at 0 V. The diagram previews the selected probe: the lit
path, dashes for the other state, `=` on MZIs held at bar, and a yellow ring on
the read output.

**Sweep selected MZI** sweeps one MZI; **Save the voltages found** stores the
result. **Calibrate all visible MZIs** works in rounds. Each round sweeps every
MZI that is visible with the MZIs calibrated so far. Probes are grouped by
input and output, so lasers and the JDSU change as little as possible.
With the OSA method, **Then check all-cross and all-bar** finishes with both
calibrated state checks.

Each sweep is fitted with T = offset + A·(1 + cos(π·x/x_π + φ₀))/2. Here x is
the heater power (V² when the heater resistance is unknown). Power changes
little near a maximum or a null, so a good fit sets cross and bar: the measured
points nearest the first maximum and the first null. A sweep that runs past 2π
therefore never picks the second extreme. When the fit is poor, the extremes
come from the data: the two-line contrast for the OSA, or the power for a
meter. Near-equal points then resolve to the lowest voltage. An MZI whose
reading changes by less than 3 dB is rejected. This usually means the wrong
input is lit or the detector is on the wrong output. Every MZI returns to 0 V at
the end, also after **Stop** or an error. Runs are saved in the dataset's
`calibration/` folder (an initialized dataset is required) as
`mzi_calibration.json` (probe, voltages, extinction and fit per MZI),
`mzi_sweeps.csv` and one plot per MZI.

**Detect six lasers (1550–1560 nm)** scans the mapped OSA outputs, finds six
distinct prominent peaks and saves them into the WDM plan by wavelength: the
highest goes to Input 1 and the lowest to Input 6.

**Alignment check (all 0 V)** and **Check routed states** use the OSA method
with all lasers on. They read all outputs and grade every MZI: its margin is
the expected line minus the other line at its outputs. *State OK* means at
least the **Pass margin**; *wrong state* means the lines are swapped by at
least that margin. Routed states leaves the Routing tab's states applied.
These checks save `state_checks.json`, `output_readouts.csv` and
`readout_spectra.csv`.

For a rehearsal without hardware, use a dry-run voltage board. The OSA method
also needs OSA **Simulate** and a dry-run JDSU. A simulated chip answers. It
has cos² MZIs, a laser per input that can be switched on and off, and thermal
crosstalk between neighbouring heaters.

### Thermal crosstalk matrix

**Crosstalk matrix (automatic)** in the Thermal crosstalk tab uses the detection
method chosen in the MZI calibration tab. It also uses the voltage limits, step
and settle time of the manual scan above it. Select aggressors and victims, then
press **Plan** to see which pairs can be measured. A pair cannot be measured when
every probe of the victim passes through the aggressor, because the aggressor
would then switch the light itself. For each victim the app:

1. applies a probe whose lit path avoids the aggressors, holding at most two
   calibrated MZIs at bar;
2. sweeps the victim, fits its cos² response, and biases it at the swept point
   nearest quadrature;
3. sweeps each aggressor. **Path reference sweep** (on by default) first repeats
   that aggressor sweep with the victim at its bright extreme, where the victim
   is insensitive to phase. Dividing by this reference removes the aggressor's
   heating of the other MZIs on the path, and doubles the aggressor sweeps.

The fit turns the victim's power into a phase shift. κ is the slope of that
phase shift against aggressor heater power, divided by the victim's own phase
per heater power. It is the share of the aggressor's heater power that the
victim effectively feels. The tab shows a live κ heatmap and one tab per victim
with its fit, bias and phase-shift curves. Results go to
`Results/Crosstalk_matrix/<time>_matrix/`: `crosstalk_matrix.json`,
`crosstalk_matrix.csv` (κ, aggressor rows × victim columns),
`crosstalk_sweeps.csv`, the heatmap, and one plot per victim. κ is reported
only when both heaters have resistances, or both have none, so that the two
drives are in the same unit.

### Route explorer

- Click an **input** to highlight its actual path and current output.
- Thin dashed lines in the input's colour show where possible crosstalk leaves the unused port of each switch and travels through downstream switches. These paths are illustrative; their optical power is not measured by the diagram.
- Click an **output**, or choose an output above the diagram, to enumerate every physical path between the selected ports.
- The route list gives the required switch states: **=** means straight; **X** means cross. Switches absent from a route may have either state.
- Select a path to highlight it in green. The overlay checkbox shows all alternatives with dashed lines. Click **Apply path (drive bar/cross)** to send that path's required MZIs to their calibrated bar or cross voltages.
- Use **Keep complete output permutation** to choose how an alternative path is applied: off changes only MZIs on that path; on finds the compatible full configuration with the fewest state changes while preserving every current input→output connection. The latter reports an error when no such configuration exists.
- After routing a complete permutation, **Equivalent full-network routings** lists every switch configuration that realizes it. Select one to preview all six color-coded routes without changing the hardware.
- Use the **X**, **=**, and **0** controls beside any switch to set cross, bar, or 0 V directly. Clicking a switch still cycles it: cross (`X`) → straight (`=`) → 0 V (`0V`) → cross. Cross and bar send the calibrated voltages; 0 V sends 0 V and is drawn with an amber outline and dashed lines. It routes as cross, the chip's unbiased state. Typing a voltage, applying a path or calibration, reset, and randomize clear the 0 V mark; path calibration ends with every switch marked 0 V. Reset and randomize controls update the complete circuit.
- Enter six desired output numbers (one for each input, using every output once) and click **Route permutation** to configure simultaneous connections. **Shuffle** generates and routes a random permutation.

The diagram is a generalized six-port Beneš network with five stages and twelve 2×2 switches. Each of the two three-port subnetworks uses three switches on local row pairs (2,3), (1,2), (2,3). The straight bypass is at the top in the first and last subnetwork stages, and at the bottom in the middle stage. Wires between these three stages are straight. The outer stages split into and recombine the two subnetworks. A conventional binary Beneš network has a power-of-two port count; this generalized layout supports all 720 six-port permutations without adding unused external ports.

Verify the topology, route enumeration, and solver with:

```powershell
python -m unittest -v
```
