# Calibration when 0 V is not a known cross state

## Problem statement

The present path-based calibration assumes that every uncalibrated MZI is in
cross at 0 V. If instead each MZI has an unknown phase at 0 V, the voltage
sweep of the MZI under test can still look sinusoidal, but the surrounding
MZIs act as unknown splitters. The maximum and minimum seen at one selected
output therefore need not be the physical cross and bar states of the swept
MZI.

For an isolated MZI, an unknown but stable zero-voltage phase is not a problem:

\[
P(V) = A + B\cos(\phi_0 + \kappa V^2).
\]

The phase offset \(\phi_0\) can be fitted. The problem is identifying that
phase inside the complete coherent network. A reliable calibration should
therefore measure an input-to-output intensity matrix rather than one scalar
probe curve.

Both strategies below estimate

\[
P_{o,i}^{(j)}(V),
\]

the power at output \(o\) from input \(i\) while MZI \(j\) is swept. These
measurements are fitted jointly to a physical model of the Benes network, for
example

\[
\theta_j(V) = \theta_{j,0} + \kappa_j V^2,
\qquad
P_{o,i} = G_o L_i |U_{o,i}(\theta_1,\ldots,\theta_{12})|^2.
\]

The fitted parameters can include the zero-voltage phases, heater
coefficients, input powers, output gains, path losses and, if required,
coupler imbalance. After fitting, cross and bar are assigned from the physical
MZI phase, not from whichever point happens to maximize one output trace.

## Strategy 1: OSA matrix calibration

### Acquisition

Use six resolvable laser wavelengths, with all six lasers on. For each MZI:

1. Keep every other heater at a defined voltage, initially 0 V without calling
   it cross.
2. Sweep the selected heater through enough heater power to cover the required
   phase range. About \(2\pi\) of phase coverage guarantees that both binary
   states can be reached from an arbitrary initial phase.
3. At every voltage, read all six chip outputs through the JDSU.
4. Extract the powers of all six input wavelengths from each OSA trace.

One voltage point consequently produces the complete \(6\times6\) intensity
matrix. An output-major acquisition order is preferable: select one output,
sweep all voltages, and then change output. This avoids paying the JDSU delay
at every voltage.

### Timing

The 720-permutation dataset provides an empirical benchmark:

- about 34 seconds per complete six-output OSA matrix with zero extra settle;
- about 52 seconds per matrix in the slower acquisition section;
- the current saved setting averages four OSA sweeps per reading.

At four averaged sweeps, a complete matrix scan of all 12 MZIs takes
approximately:

| Voltage grid | Points per MZI | 34 s/matrix | 52 s/matrix |
|---|---:|---:|---:|
| 0-5 V, 0.10 V step | 51 | 5 h 51 min | 8 h 50 min |
| 0-5 V, 0.20 V step | 26 | 2 h 59 min | 4 h 30 min |
| 0-5 V, 0.25 V step | 21 | 2 h 25 min | 3 h 38 min |

For the identification pass, reducing the OSA average from four sweeps to one
should reduce the sweep-dominated portion substantially. In continuous mode,
the first reading still waits for two fresh physical sweeps, so the expected
reduction is approximately from five physical sweeps to two. A practical
0.20 V identification run is therefore estimated at 1 h 15 min to 1 h 30 min.

Recommended overall budget:

- coarse OSA matrix acquisition: 1 h 15 min to 1 h 30 min;
- global fit: 5 to 15 min;
- targeted refinement: 20 to 40 min;
- four-sweep validation: 15 to 25 min;
- total: approximately 2 to 2.5 hours.

### Advantages and limitations

Advantages:

- all six inputs are measured in parallel;
- wavelengths retain the input identity;
- complete spectra and a large dynamic range are available;
- the existing JDSU/OSA state-check path already acquires full output matrices.

Limitations:

- OSA sweeps dominate the acquisition time;
- the method requires resolvable and stable laser wavelengths;
- full high-resolution scans with several background configurations can take
  many hours.

## Strategy 2: power-meter matrix calibration

### Acquisition

Connect the power meter to the JDSU common port. Only one laser is enabled at
a time, because the power meter otherwise sums the six inputs and loses their
identities. For each MZI:

1. Keep every other heater at a defined voltage, initially 0 V.
2. Enable one input laser and wait for it to settle once.
3. Select one output through the JDSU.
4. Sweep all requested voltages and record the scalar power.
5. Repeat for all six outputs and all six inputs.

An efficient loop order is:

```text
for each MZI:
    for each input laser:
        enable only this laser and wait once
        for each output:
            select the JDSU output and confirm settled readback
            sweep all voltages and read the power meter
```

This recovers the same \(6\times6\) intensity matrices as the OSA, but
sequentially. It avoids paying the laser-settle delay at every voltage.

### Adaptive measurement plan

A PM study can be shortened by separating identification from refinement:

1. Measure 11 to 13 coarse points approximately uniform in \(V^2\), using all
   36 input/output pairs for every MZI.
2. Fit the complete network model.
3. Select the four to eight most phase-sensitive input/output pairs for each
   MZI.
4. Measure fine points only near the predicted cross and bar voltages.
5. Add a small number of points under a second background configuration only
   when the first fit is ambiguous.

For example, 13 coarse points for all pairs require
\(12\times36\times13=5616\) readings. Four selected pairs and ten local
refinement points per MZI add only 480 readings.

### Timing

The saved HP 8152A calibration acquired 612 scalar readings in approximately
5 min 12 s, or roughly 0.51 s per point including its existing overhead.
Using that benchmark:

| PM study | Estimated acquisition time |
|---|---:|
| Adaptive coarse plus targeted refinement | 55-75 min |
| Full 0-5 V scan, 0.20 V step, all 36 pairs | 1.6-2.0 h |
| Full 0-5 V scan, 0.10 V step, all 36 pairs | 3.2-3.6 h |

Allowing for fitting, retries and validation, the recommended adaptive PM
study should take approximately 1.5 to 2 hours.

### Drift and measurement controls

Because the matrix elements are acquired sequentially, the PM method needs
more explicit drift control:

- acquire an all-lasers-off dark reading for each output;
- repeat a reference voltage at the end of every trace;
- periodically return to one stable input/output reference;
- normalize each input by its measured power summed across the six outputs;
- run upward and downward sweeps on a subset of MZIs to test hysteresis;
- confirm the JDSU settled status, using no unnecessary fixed delay after its
  real settling time has been measured.

If the meter is attached to one fixed chip output rather than the JDSU common
port, the complete calibration is not identifiable without manual repatching.
That setup can still optimize a particular route, but it cannot provide the
same general cross/bar characterization.

## Validation common to both strategies

After estimating cross and bar voltages:

1. Apply all-cross and measure the full transfer matrix.
2. Apply all-bar and measure the full transfer matrix.
3. Test the identity and a representative set of permutations.
4. Check each MZI under at least two surrounding switch configurations.
5. Flag a switch when its inferred state voltage changes with the probe route.

A route-dependent optimum indicates surrounding coherent interference,
thermal crosstalk, drift or an incomplete network model. Simultaneous heater
operation may require a final closed-loop correction even when the individual
MZI fits are accurate.

## Recommended choice

- Use the **adaptive PM strategy** when the meter is on the JDSU common port
  and acquisition speed is the priority. It is expected to finish in about
  1.5 to 2 hours, but needs careful drift normalization.
- Use the **OSA strategy** when spectral identity, dynamic range and diagnostic
  visibility are more important. A coarse one-sweep identification followed
  by high-quality validation should take about 2 to 2.5 hours.
- Do not interpret 0 V as cross in either workflow. Store explicit calibrated
  voltages for both cross and bar, and use 0 V only to mean heater off.

The safest first experiment is an adaptive PM run followed by a smaller OSA
validation set. Since changing detector requires a fiber swap, these should be
performed as two separate sessions rather than mixed dynamically within one
calibration.
