# Beneš 6×6 route explorer

A standalone Python/Tkinter desktop GUI. Run with Python 3.10 or later:

```powershell
python benes_gui.py
```

No pip packages are required. Tkinter is included in the standard Windows Python installer.
Physical board control additionally needs the `pyserial` dependency used by the
sibling VoltageTuner project.

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
New configurations leave board pin 8 free for the impulse monitor. Existing pin
mappings are preserved; unassign pin 8 from any MZI before an impulse test.
The default scan limits are 0–5 V. A voltage outside this range requires a
confirmation before the board sends it; the prompt reminds you to disconnect
the Arduino sampler from the voltage-board signal and check the heater current
limit.

The UI is organized by workflow: **Routing**, **Hardware & OSA**, **MZI
calibration**, **Thermal crosstalk**, and **Impulse test**. Hardware & OSA separates the voltage board, OSA
and its output selector, and Arduino meter into sub-tabs so every control remains
visible on a laptop display.

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
and **TEC temperature** output states for each of the 16 slots. **Turn ON** and
**Turn OFF** act on one output at a time; they use the current and temperature
setpoints already configured on the Newport controller. **Refresh** rereads
the output states. **ALL OFF** turns off every laser output first, then every
TEC output, and reports any channel that could not be confirmed off.

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
linear-sum power across all outputs, expected routed output, transmission at
that output, and crosstalk at each other output in dBc.

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

**Dataset Coverage** has separate actions to scan the active control state or
to run all 720 output permutations. Each action records the six routed pairs in
the linked dataset and restores the original control state when it finishes.
**Run all 4096 switch states** additionally measures every one of the 12-MZI
bar/cross configurations, including configurations that realize the same output
permutation. The live metrics table has one row per state and input wavelength:
total line power over the six outputs, normalized expected-port transmission,
and normalized crosstalk at the other outputs.

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

The same tab can instead use the Arduino detector stream from the sibling
`../ThorlabsControl/ArduinoSampler.py`. Enter its port, baud rate, and sampling
burst duration, then connect and choose **Read current power**. The reader
averages the raw ADC samples and displays `ADC × scale + offset`; set the scale,
offset, and unit to match the power-meter calibration. The latest scalar result
is retained by the application for calibration. Arduino settings are stored in
`benes_power_meter_config.json`.

## Impulse test

The **Impulse test** tab drives the selected MZI through the voltage board and
sends the same low/high pattern to unused board pin 8. Connect the optical
detector to Arduino A0 and board pin 8 to A1 after verifying that its signal
stays within 0–5 V. Each edge sends the MZI and pin 8 commands in one serial
write, without VoltageTuner's 10 ms per-command pause, so pin 8 lags the MZI by
one command (~0.9 ms at 115200 baud).
Upload [ArduinoDualChannel.ino](ArduinoDualChannel/ArduinoDualChannel.ino) to the
Uno R3, then select **Stream: raw** and set **Baud** to the sketch's
`Serial.begin` rate (1000000 or 2000000) before connecting. 2000000 is needed
only if a faster ADC prescaler (8) pushes the stream past ~100 kB/s.
The sketch streams continuously from power-up, as fast as its `analogRead` loop
runs, and needs no commands. Each pair is two bytes holding 7-bit values
(0–127), tagged by the top bit: `0xxxxxxx` is A0 optical and `1xxxxxxx` is A1
electrical, read right after A0. The PC pairs each A0 byte with the A1 byte
after it, so it can join the stream at any byte and resynchronizes by itself
after a dropped byte; the number of skipped bytes is shown and exported.
Each reading discards bytes that arrived before it starts. Power readings for
calibration and crosstalk scans use the A0 values. The detector saturates
near 80 counts, well below the 127 full scale, so **Ceiling [ADC]** (default
80) sets the raw level treated as saturated; it is capped at the full scale.
Older framed A0/A1 sketches remain available through **Stream: dual**, and
one-byte A0 firmware through **Stream: legacy**, at their original baud rates.

Set low and high voltages, then either play a square wave with **Period** and
**High time**, or take a **Single pulse** with pulse and total measurement
durations. **Lead-in** records the baseline before the first high level. Click
**Stop** to end a square wave; both voltage-board outputs return to the low
voltage. While A1 is connected, impulse voltages must stay within 0–5 V.
Time fields accept scientific notation in seconds: `1e-3` is 1 ms, `5e-2` is
50 ms, and `2e0` is 2 s. The display-average field uses milliseconds.
Fields start blank. Timing has no limits beyond being positive (and high time
shorter than the period). Edges are scheduled on absolute PC deadlines with a
spin-wait for the last 20 ms, so millisecond pulses and square waves work.
Pulses shorter than one edge (~1.9 ms on the wire at 115200 baud) are
stretched, and Windows/Python scheduling can add jitter. A1 records the actual
pin 8 waveform on every pair, so check it for the delivered timing.
The live plot labels both channels and shows the latest 10 seconds while
recording. It retains brief peaks when reducing points for display and shows
the full recording after Stop. Time ticks use seconds, ms, or µs as appropriate,
with contrasting labels. Use the plot toolbar to zoom into an edge or pan;
incoming data preserves the selected view. Optical display averaging is blank
or 0 for raw; enter a larger value to reduce displayed noise. CSV exports always
retain raw A0/A1 samples, independent of display averaging.
Both **Single pulse** and **Play square wave** use the same continuous stream.
**Export trace CSV** saves both measured channels and the commanded voltages.
The sketch has no sample clock, so the pair period is measured as host
recording time divided by pairs received. After a run, every sample is placed
at pair index × that final period; USB delivery times do not determine the
spacing. Alignment to PC command times is approximate because the stream
contains no timestamps, and samples lost at a resync shift later times
slightly. A stalled stream raises an error in the reader. The trace limit is
2,000,000 paired samples.

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
with Ctrl or Shift, choose **Arduino** or **OSA**, enter the lower and upper
voltage limits, step, and settling time, then start the scan. Arduino readings
are burst averages. For OSA scans, each voltage produces a spectrum after the
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

### Six-laser MZI calibration

The **MZI calibration** tab assumes a laser on every input, each at its own
wavelength from the saved six-laser plan. Whatever the switch states, every MZI then
carries two distinguishable lines, so no fibers need moving between steps. The
OSA reads one output at a time through the JDSU switch. For any MZI, two outputs
receive a different input's line depending on its state, and the power of those
two lines there gives the MZI's state. MZIs not being swept stay at 0 V (cross).

Use **Read OSA + detect six channels** when all six laser lines are visible in
the OSA trace. It finds the six strongest distinct peaks, saves them into the
WDM comb plan, and assigns them by wavelength: highest wavelength to Input 1
and lowest to Input 6. It reports an error instead of changing the plan unless
all six peaks are distinct and prominent.

- **Sweep selected MZI** (single MZI): sweeps one MZI while watching one of its two
  outputs (**Auto** picks one that sees both lines). Cross is the voltage where the
  cross-state line most exceeds the other line, and bar the opposite. Points
  within 0.5 dB of an extreme count as equal, and the lowest voltage wins.
  **Save the voltages found** stores them.
- **Calibrate all MZIs**: checks alignment (reads all six outputs at 0 V and asks
  before continuing if a line leaves at an unexpected output), then sweeps every
  MZI, grouped by watched output so the JDSU moves as little as possible. It then
  optionally checks the calibrated all-cross and all-bar states.
- **Alignment check (all 0 V)** and **Check routed states** read all outputs and
  grade every MZI: its margin is the expected line minus the other line at its
  outputs. *State OK* means at least the **Pass margin**; *wrong state* means the
  lines are swapped by at least that margin. Routed states applies the Routing
  tab's states with their saved voltages and leaves them applied. Every other
  action returns all MZIs to 0 V, also after **Stop** or an error.

The diagram shows each laser's expected path in its input colour, with dashes
where the swept MZI would send its lines in the other state. It also shows each
MZI's status colour and margin, a blue ring on the output being read, a yellow
ring on the watched output, and each output's strongest measured line. Tables
list per-MZI voltages and margins and the latest output × input line matrix.
Sweeps are plotted per MZI. Each run is saved in `Results/MZI_calibration/<time>_<kind>/`
(`mzi_calibration.json`, `mzi_sweeps.csv`, `output_readouts.csv`,
`readout_spectra.csv`). For a rehearsal without hardware, use a dry-run voltage
board, a dry-run JDSU and OSA **Simulate**: a simulated chip with cos² MZIs
answers.

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
