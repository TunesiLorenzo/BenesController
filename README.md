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

Board connection and measurement controls live under the **Boards & measurement**
tab at the top of the window.

## OSA traces

The **OSA** row reads trace 1 from a VIAVI OSA 500RS. Choose **Overlay** or **New
window**, name each trace, and use **Export displayed datasets** in a plot window
to write every visible trace to CSV. The export dialog asks for an optional
description, which is included as `# Description:` comment line(s) at the top of
the file. Plotting needs matplotlib (`python -m pip install matplotlib`).

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

The **Crosstalk** tab can scan several switches in sequence. Select switches
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
**Crosstalk** tab. The application automatically creates
`Results/Crosstalk_scan/Port{input}-Port{output}_{dateTime}/` and saves
`base_setting.json`, a compact `base_path.png` diagram, one PNG plot per
measured switch, and `crosstalk_dataset.csv` with the comment. The folder uses
the selected destination, or the currently reached output when **Trace current**
is selected. The summary records the actual reached output separately.

### Automatic 0 V path calibration

The **Crosstalk** tab also has **Calibrate selected 0 V path**. It is an Arduino
power-meter workflow designed to minimize thermal crosstalk: choose an input and
its matching 0 V output in **Route explorer**, mark that output as **Power Meter
output**, and include 0 V in the scan range. The routine first sends every MZI to
0 V, then sweeps the selected path in reverse order (output-side switch first).
For each MZI it stores the measured maximum as **cross** and minimum as
**straight**, returns that MZI to 0 V, and then advances to the next one. Every
MZI is forced back to 0 V even if a measurement fails.

If two or more readings for one MZI reach the meter floor (0) or the
**Ceiling [ADC]** level or above,
calibration pauses with that MZI at 0 V. A floor prompts you to select a more
sensitive range and rescan for the straight minimum. A ceiling prompts you to
select a less sensitive range and rescan for the cross maximum. After each
rescan, restore the range used at the start of the switch before continuing.
If both limits are reached, both rescans are requested. If the sensitive rescan
still reaches the floor, the usual restore-range prompt is shown, the lowest
reading found is kept as a best-estimate straight voltage, and calibration
continues with the next MZI; a warning at the end lists those MZIs and
`base_setting.json` marks them with `straight_at_noise_floor`. Rescans are
plotted live in the MZI's tab, and in its saved figure, on a separate right-hand
axis because they use another meter range. Cancel stops calibration
without saving the current MZI. Each rescan is recorded separately in
`base_setting.json`, with the measurement range used for each chosen extremum.

For thermally quiet calibration, the routine does not use heated steering MZIs.
For this six-port topology, the minimum complete default-cross plan is four
input/output pairs: **1 → 5**, **2 → 6**, **3 → 3**, and **6 → 2**. Together their
paths cover all twelve MZIs. The tab prompts the required next connection after
each completed path. Plan completion is based on MZIs calibrated by successful
path scans in the current app session, so starting with a later listed path or
repeating a path cannot mark the plan complete early. Choose the settling time
and the number of Arduino burst averages per voltage point before starting a path.

- Click an **input** to highlight its actual path and current output.
- Click an **output**, or choose a destination above the diagram, to enumerate every physical path between the selected ports. Select **Trace current** to return to the actual route.
- The route list gives the required switch states: **=** means straight; **X** means cross. Switches absent from a route may have either state.
- Select a path to highlight it in green. The overlay checkbox shows all alternatives with dashed lines. This is a preview until you click **Apply selected path to switches**.
- Click any switch to cycle it: cross (`X`) → straight (`=`) → 0 V (`0V`) → cross. Cross and straight send the calibrated voltages; 0 V sends 0 V and is drawn with an amber outline and dashed lines. It routes as cross, the chip's unbiased state. Typing a voltage, applying a path or calibration, reset, and randomize clear the 0 V mark; path calibration ends with every switch marked 0 V. Reset and randomize controls update the complete circuit.
- Enter six desired output numbers (one for each input, using every output once) and click **Route permutation** to configure simultaneous connections. **Shuffle** generates and routes a random permutation.

The diagram is a generalized six-port Beneš network with five stages and twelve 2×2 switches. Each of the two three-port subnetworks uses three switches on local row pairs (2,3), (1,2), (2,3). The straight bypass is at the top in the first and last subnetwork stages, and at the bottom in the middle stage. Wires between these three stages are straight. The outer stages split into and recombine the two subnetworks. A conventional binary Beneš network has a power-of-two port count; this generalized layout supports all 720 six-port permutations without adding unused external ports.

Verify the topology, route enumeration, and solver with:

```powershell
python -m unittest -v
```
