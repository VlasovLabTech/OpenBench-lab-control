# ITECH IT6054C-800-225 live test — 2026-08-13

## Hardware and transport

- Instrument: ITECH `IT6054C-800-225`
- Instrument serial: `ITECH-DEMO-0001`
- Identification: `ITECH Electronics,IT6054C-800-225,ITECH-DEMO-0001,000.006.101,640.R,132.R`
- SCPI version reply: `IT60xxSrc-v1.3.7.xx`
- USB virtual COM identity: `VID 2EC7`, `PID A4A7`
- Live port and framing: temporary Windows `COMn`, 115200 baud, 8N1, LF termination
- Wiring: nothing connected to the power terminals; no load or external source
- Operator authorization: low-voltage Output ON/OFF testing was explicitly allowed

Discovery first tries 115200 and then 9600 because this instrument family may
return to 9600 after a local configuration change. The live unit answered at
115200. The 9600 fallback has deterministic transport coverage but was not
forced on the physical instrument.

## Source material

- ITECH IT6000C product page:
  <https://www.itechate.com/en/product/dc-power-supply/IT6000C.html>
- ITECH IT6000C User Manual (official):
  <https://www.itechate.com/uploadfiles/%E7%94%A8%E6%88%B7%E6%89%8B%E5%86%8C/user%20manual/it6000c/en-us/IT6000C%20User%20Manual-EN.pdf>
- ITECH IT6000C Programming Guide (official CDN):
  <https://cdn.itechate.com/uploadfiles/%E7%94%A8%E6%88%B7%E6%89%8B%E5%86%8C/user%20manual/it6000c/en-us/IT6000C%20Programming%20Guide-EN.pdf>

No vendor executable, firmware image, patched library, or generated binary is
required at runtime. The implementation is the tracked bounded SCPI driver plus
the locked `pyserial` dependency.

## Initial read-back

The full read-only inventory succeeded through the OpenBench API. Representative
initial state:

```text
Output OFF; FIXED; CV
VOLT 4.00 V
CURR 2.31 A
CURR:LIM +2.31 A / -2.27251 A
VOLT:LIM +0.20 V / 0.00 V
POW:LIM +55080 W / -55080 W
OVP ON 12.8 V; OCP ON 227.25 A; OPP ON 3200 W
UVP ON 10 V; UCP OFF
slew V+/V-/I+/I- 0.1; output delays 0 s; watchdog OFF, 30 s
questionable status 0; operation status 0
```

The Dashboard correctly flags the 55.08 kW power limits, 227.25 A OCP, and
3.2 kW OPP as dangerous operator settings. These warnings do not silently
rewrite the instrument.

## Mutating API tests

All mutations used `/api/v1/bidirectional-power-supplies/...`; no raw SCPI was
issued by the test workflow.

1. UVP was temporarily disabled because its original 10 V threshold would
   intentionally trip during the 1 V test.
2. Fixed CV was set to 1.00 V with Output OFF.
3. Output enable without `wiring_confirmed` is covered as a rejected API test.
4. With explicit wiring confirmation, Output ON read back as enabled, active
   regulation was `CV`, and the open-circuit measurement was 0.987793 V,
   0.004135 A, 0.003895 W. No fault bits were set.
5. Output OFF was written and verified.
6. Fixed CC was set to 0.00 A. Output ON read back as enabled with active
   regulation `CC+` and approximately zero terminal power. Output was then
   turned OFF and verified.
7. A signed `-0.10 A` CC setpoint was accepted and read back with Output OFF,
   validating the sink-side programming path without applying an external
   source.
8. The initial CV operating point and UVP state were restored. The negative
   current limit was restored to `-2.27 A`, the instrument's documented 0.01 A
   programming resolution, instead of its initial higher-precision read-back
   `-2.27251 A`. All other tested settings match the initial state and Output
   is OFF.

An observed hardware rule is now part of the driver contract: `VOLT:LIM` is
writable in CC priority and `CURR:LIM` in CV priority. Changing priority also
copies active values into related limit registers. OpenBench therefore rebuilds
the inactive priority first and the requested final priority second, then reads
the complete state back. A regression test simulates this coupling.

## Capture and interface validation

- Dashboard rendering includes the ITECH identity, prominent actual voltage and
  current, actual power, Output state, SOURCE/SINK/IDLE, CV/CC/CP regulation,
  signed I/V/P limits, warnings, and complete Settings forms.
- The ordinary snapshot API returned 15 measurements: one simulated-meter
  channel plus all fourteen ITECH channels.
- Test artifact: `20260813_1456_snap_itech_api.csv`
- Its wide CSV header contains `ITECH IT6054C-800-225`, the stable serial-based
  device ID, polling interval, actual V/A/W, setpoints, every signed limit,
  output, priority, and direction.

## Boundaries and untested cases

- No terminal load or external source was connected, so real positive-current
  delivery and regenerative negative-current absorption were not exercised.
- No high-voltage, high-current, or high-power test was attempted.
- List, Battery, Solar, calibration, system, and arbitrary SCPI functions are
  intentionally outside OpenBench's bounded API.
- Only `IT6054C-800-225` is accepted. Other IT6000C variants require separate
  rated/SCPI limits and a dated physical validation record.

Final live state: connected at 115200 baud, FIXED CV, 4.00 V setpoint, Output
OFF, UVP restored ON, no reported faults.

## Fast sink-step validation with ETO5004

A later bounded test used an external 12 V / 10 A source and exercised only
0, -2, and -4 A in continuous CC sink operation. Output remained ON between
points and was forced OFF at the end. Each live current PATCH sent only the
setpoint command; it performed no full state read-back. After a 0.5 s minimum
settle, the API issued exactly the three measured V/I/P queries while the ETO
screenshot and six scalar Measurements were captured concurrently.

Artifact: `.openbench/data/captures/sessions/20260813_1837_rec_itech_fast.csv` with three
`eto_frame_*` directories. All three screenshots and measurement files are
present. Recorded ITECH samples were:

```text
set  0 A: 11.9827 V,  0.00123215 A,  -0.0241145 W
set -2 A: 11.9241 V, -1.99969 A,    -23.8495 W
set -4 A: 11.8643 V, -3.99986 A,    -47.4584 W
```

The three ETO frame transactions took 6.129 s, 5.191 s, and 4.114 s; the
complete three-point recording span was about 15.65 s. Therefore the remaining
4–6 s point cadence is dominated by ETO stored-screenshot download plus six
scope scalar reads, not ITECH setpoint or full-state read-back. Final ITECH
Output was verified OFF.
