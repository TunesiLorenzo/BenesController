# VIAVI OSA Ethernet interface guide

This guide is a handoff for an agent adding OSA controls to BenesController. The project identifies the optical module as a **VIAVI OSA-500RS**. The reachable Ethernet endpoint is `192.168.0.200:8002`; its `*IDN?` response was `VIAVI Solutions,MTS 8000E,2770,FO,21.78` on 2026-11-04. That response identifies the MTS mainframe and optical-function port, not the module model by itself.

## Manuals

- [VIAVI Programming the MTS/T-BERD, revision 007](https://www.viavisolutions.com/sites/default/files/support/8010M00rev007rev007remote.pdf), especially Chapter 5, “Configuration of the WDM and OSA modules” (printed pages 78–95). This is the command reference for the settings below.
- [VIAVI 8100 Series Modules User Manual](https://www.viavisolutions.com/en-us/literature/8100-series-modules-otdr-fibercomplete-osa-dispersion-fiber-characterization-manuals-user-guides.pdf), Chapter 8, “Optical Spectrum Measurement,” for the instrument's acquisition controls and their measurement meaning.

## Connection and command protocol

Use a TCP socket with a timeout. Send ASCII commands terminated by LF (`\n`); read one LF-terminated line for each query. On a new connection, send `*REM` before querying. Send `*LOC` when finished if returning the instrument to front-panel control is appropriate. `*IDN?` confirms the endpoint. The manual says function ports can be assigned at connection time; the app currently uses port 8002, but a more general client should discover the optical-function port through the mainframe's port 8000 rather than assume it is permanent.

```python
import socket

with socket.create_connection(("192.168.0.200", 8002), timeout=5) as sock:
    sock.settimeout(5)
    stream = sock.makefile("rwb")

    def send(command):
        stream.write(command.encode("ascii") + b"\n")
        stream.flush()

    def query(command):
        send(command)
        reply = stream.readline()
        if not reply.endswith(b"\n"):
            raise OSError(f"No complete reply to {command}")
        return reply.decode("ascii").strip()

    try:
        send("*REM")
        print(query("*IDN?"))
        print("resolution:", query("OSASetup:RESO?"))
        print("acquisition mode:", query("OSASetup:MODe?"))
    finally:
        send("*LOC")
```

Queries below were answered by the connected instrument. Settings are shown as read on 2026-11-04; they may have changed since.

| Setting | Query | Set command | Observed value / meaning |
| --- | --- | --- | --- |
| Optical resolution | `OSASetup:RESO?` | `OSASetup:RESO FULL` or a supported resolution keyword | `FULL`: maximum optical filter resolution. The manual lists `R01nm` through `R05nm` for 0.1–0.5 nm; other options depend on the module. |
| Acquisition mode | `OSASetup:MODe?` | `OSASetup:MODe 0` | `0`: continuous. `1`: single acquisition. `2`–`1000`: that many acquisitions for instrument statistics. The manual also lists `-1` for filter/drop mode. |
| Acquisition range preset | `OSASetup:SEAcq?` | `OSASetup:SEAcq <keyword>` | `SEM`, the instrument's abbreviation for a manual range selection. The manual lists full band and several preset bands, plus manual variants. |
| Manual range start/end | `OSASetup:MSAcq?`, `OSASetup:MEAcq?` | `OSASetup:MSAcq <THz>`, `OSASetup:MEAcq <THz>` | `193.290`, `192.298` THz. These controls use **frequency in THz**, not wavelength in nm; frequency decreases as wavelength increases. |
| Manual range center/span | `OSASetup:MCAcq?`, `OSASetup:MSPAcq?` | Corresponding commands with THz values | `194.355`, `3.000` THz when queried. These may be stored setup values for a different manual range variant; do not assume all four manual values describe the active trace simultaneously. |
| Instrument averaging | `OSASetup:AVG?` | `OSASetup:AVG NO`, `LOW`, `MEDium`, or `HIGH` | `NO`. This is the OSA's own averaging control. |
| Sensitivity | `OSASetup:HSEns?` | `OSASetup:HSEns AUTO`, `NO`, or `YES` | `AUT` (abbreviated `AUTO`). |
| Acquisition status | `STAT:ACQ?` | `KEY START` starts or stops via the front-panel key command | `IN_PROGRESS` when queried. Check state before using `KEY START`, since it behaves like a key press. |
| Selected trace size | `:CUR:SIZE?` | — | `801` points when queried. |

Changing resolution or acquisition range can alter trace length, wavelength spacing, and sweep time. Query the setting after writing it and take a new sweep before using the resulting trace. The `FULL` resolution setting is an instrument filter setting; it is separate from the number of downloaded trace points and the graph's display resolution.

## Existing BenesController behavior

- [`benes/hardware/osa.py`](../benes/hardware/osa.py) implements the TCP trace reader. It sends `*REM`, selects trace 1, checks `STAT:ACQ?`, starts a stopped acquisition with `KEY START`, reads `:CUR:BUFF?`, and queries `:CUR:XOFF?`, `:CUR:XSCale?`, `:CUR:YOFF?`, and `:CUR:YSCale?` to scale the data. It does **not** currently set resolution, range, mode, sensitivity, or instrument averaging.
- The curve response is an ASCII definite-length block containing four hexadecimal characters per signed 16-bit sample. The current reader converts the x-axis from frequency to wavelength with `c / frequency / 1000`; follow the existing `parse_curve` and `scale_curve` functions rather than making a new parser.
- [`benes/measurements/osa_reading.py`](../benes/measurements/osa_reading.py) waits for two completed sweeps after a JDSU or heater change, then optionally averages 1–16 downloaded traces in **linear power**. The GUI's “Sweeps per reading” controls this application-level averaging. It is distinct from `OSASetup:MODe` and `OSASetup:AVG`.
- The app assumes continuous acquisition for its fresh-trace polling. If adding a single-acquisition mode, adapt that flow and verify the state transitions; merely setting `OSASetup:MODe 1` may leave later app reads waiting for a sweep that never starts.

## Suggested integration sequence

1. Query `*IDN?` and the current OSA settings on connect. Preserve the returned strings, since this firmware abbreviates values such as `AUTO` to `AUT`.
2. Add explicit resolution and acquisition controls with only manual-documented values supported by the installed module. Separate instrument settings from “Sweeps per reading.”
3. Apply changed settings while no app read is in progress, read them back, then discard any pre-change buffer and wait for a fresh acquisition. Re-query `:CUR:SIZE?` and the scale fields after the change.
4. Exercise the actual 500RS for each value exposed in the GUI. A command appearing in the mainframe manual does not guarantee every listed option is supported by this optical module or firmware.
