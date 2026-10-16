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

## OSA traces

The **OSA** row reads trace 1 from a VIAVI OSA 500RS. Choose **Overlay** or **New
window**, name each trace, and use **Export displayed datasets** in a plot window
to write every visible trace to CSV. The export dialog asks for an optional
description, which is included as `# Description:` comment line(s) at the top of
the file. Plotting needs matplotlib (`python -m pip install matplotlib`).

- Click an **input** to highlight its actual path and current output.
- Click an **output**, or choose a destination above the diagram, to enumerate every physical path between the selected ports. Select **Trace current** to return to the actual route.
- The route list gives the required switch states: **=** means straight; **X** means cross. Switches absent from a route may have either state.
- Select a path to highlight it in green. The overlay checkbox shows all alternatives with dashed lines. This is a preview until you click **Apply selected path to switches**.
- Click any switch to toggle its current state. Reset and randomize controls update the complete circuit.
- Enter six desired output numbers (one for each input, using every output once) and click **Route permutation** to configure simultaneous connections. **Shuffle** generates and routes a random permutation.

The diagram is a generalized six-port Beneš network with five stages and twelve 2×2 switches. Each of the two three-port subnetworks uses three switches on local row pairs (2,3), (1,2), (2,3). The straight bypass is at the top in the first and last subnetwork stages, and at the bottom in the middle stage. Wires between these three stages are straight. The outer stages split into and recombine the two subnetworks. A conventional binary Beneš network has a power-of-two port count; this generalized layout supports all 720 six-port permutations without adding unused external ports.

Verify the topology, route enumeration, and solver with:

```powershell
python -m unittest -v
```
