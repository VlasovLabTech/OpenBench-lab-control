# OWON SPM6103 live test — 2026-07-31

## Hardware

- Instrument: OWON SPM6103, serial `SPM-DEMO-0001`
- Instrument firmware: `FV:V2.1.0`
- SCPI version response: `V1.0.0`
- USB bridge: CH340, `1A86:7523`, temporary Windows `COMn` assignment and USB path
- Transport: 115200 baud, 8 data bits, no parity, 1 stop bit, newline termination
- Output wiring: nothing connected; operator explicitly authorized output tests
- Host: Windows, Python 3.13, Europe/Moscow

The COM number is recorded only as test evidence. OpenBench identity is
`owon_spm6103_SPM-DEMO-0001`, derived from the instrument serial number.

## Primary references

- [OWON SPM product page](https://www.owon.com.hk/products_owon_spm_series_1_ch_dc_power_multimeter)
- [OWON SPM Series programming manual](https://files.owon.com.cn/software/Application/SPM_Series_programming_manual.pdf)

## Read-only protocol confirmation

| Query | Response |
|---|---|
| `*IDN?` | `OWON,SPM6103,SPM-DEMO-0001,FV:V2.1.0` |
| `SYST:VERS?` | `V1.0.0` |
| `OUTP?` | `OFF` |
| `VOLT?` | `5.000` |
| `CURR?` | `9.600` |
| `VOLT:LIM?` | `62.000` |
| `CURR:LIM?` | `10.000` |
| `MEAS:ALL?` | `0.000,0.000` |
| `MEAS:ALL:INFO?` | `0.000,0.000,0.000,OFF,OFF,OFF,0` |
| `CONF:ALL?` | `VOLT:DC,+0.0001V,AUTO,2V` |
| `CONF?` | `VOLT:DC +1.0000E-04` |

`MEAS:ALL:INFO?` was confirmed as voltage, current, power, OVP fault, OCP
fault, OTP fault, and mode (`0` standby, `1` CV, `2` CC, `3` fault).

## Safe output test

Initial settings were saved, then the documented remote sequence was used:

1. `SYST:REM`
2. `VOLT 1.000`
3. `CURR 0.100`
4. `OUTP ON`
5. Read `MEAS:ALL:INFO?` repeatedly for approximately one second.

Observed voltage rose from approximately `0.060 V` to `0.990 V` and then
`1.000 V`; current and power remained zero with no load. The mode was CV and
all fault flags remained OFF. The roughly 0.5-second settling interval is why
the driver waits before verifying an ON transition.

The test finished with `OUTP OFF`, restored `5.000 V` / `9.600 A`, preserved
the original `62.000 V` / `10.000 A` protection thresholds, sent `SYST:LOC`,
and verified zero live output.

## OpenBench validation

- Unit/parser/driver/service/API/Dashboard tests: passed.
- Final repository suite after integration and the live API pass: `149 passed`;
  Ruff and mypy also passed without findings.
- Public API discovery/control and all eight DMM function transitions are
  validated in the final live pass before commit.
- DMM range, relative/null, and Hold queries were confirmed against the live
  firmware. Voltage and resistance expose documented auto/manual control;
  current exposes fixed range control; capacitance exposes range query only.
- One combined API update successfully selected the 20 V manual range, enabled
  Relative and Hold, and read all three settings back. The driver deliberately
  confirms each asynchronous setting before sending the next command.
- The official OWON model table was rechecked before commit: SPM6103 is limited
  to 300 W. The driver enforces 60 V, 10 A, and the coupled 300 W envelope and
  rejects other SPM models until they receive their own tested limit profiles.
- Disconnect, application shutdown, and emergency stop paths call a bounded
  Output OFF operation and return the panel to local mode.
- The published programming manual contains no remote commands for the
  front-panel List waveform editor or startup-auto-output setting. They remain
  local rather than being implemented with guessed output commands.

No vendor executable, firmware, or generated binary is required by this
driver. The implementation uses documented SCPI through the locked `pyserial`
dependency and is fully represented in the repository.
