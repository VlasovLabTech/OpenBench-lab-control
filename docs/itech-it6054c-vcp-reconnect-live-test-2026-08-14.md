# ITECH IT6054C USB-VCP reconnect and low-level mode live test — 2026-08-14

## Hardware and connection

- ITECH `IT6054C-800-225`, serial `ITECH-DEMO-0001`.
- USB VID/PID `2EC7:A4A7`, temporary Windows `COMn` assignment.
- Front panel `SYSTEM I/O -> USB-VCP`, `115200`, `8-N-1`.
- DUT and external source/load physically disconnected for the mode tests.

Selecting USB-VCP was required. The USB-TMC selection is a different USB
interface and did not provide the virtual COM link expected by this driver.
With USB-VCP selected, initial discovery completed in 2.139 s. A later software
disconnect and rediscovery completed in 2.088 s with the same serial-based ID.

## Results

- Three complete-state reads completed in 2.145 s, 1.976 s, and 2.028 s.
- A compact `MEAS:VOLT?` + `MEAS:CURR?` API read completed in 0.015 s from the
  existing live cache and returned calculated power.
- CV 1 V with +/-0.1 A limits enabled and disabled normally. Enabled readback:
  0.987793 V, 0.00939178 A, `SOURCE`, `CV`, no faults.
- CC +0.1 A with a 1 V compliance limit enabled and disabled normally. On an
  open circuit it reached the voltage compliance: 0.999756 V, 0.00718689 A,
  `SOURCE`, `CV`, no faults.
- CC -0.1 A also enabled and disabled normally. With no external energy source
  it could not enter regenerative sink operation and remained at the lower
  voltage boundary; this is not a communications failure.
- The hardware aliases the inactive setpoint to the active priority's positive
  limit (`CURR?` mirrors `CURR:LIM?` in CV and `VOLT?` mirrors `VOLT:LIM?` in
  CC). Driver verification now accounts for that live behavior.

A deliberately too-low temporary 2 V OVP threshold latched OVP during an early
CV check. The official `OUTPut:PROTection:CLEar` command was added as a bounded
API action that is permitted only while Output is OFF. It cleared the latch
without changing the restored 13 V / 12 A / 150 W protection settings.

Final state: connected at 115200 baud, FIXED CC, current setpoint -4 A, limits
13 V / +/-12 A / +/-150 W, Output OFF, and no active faults. A read-only
multi-instrument preflight then found the ITECH, MHO1, and required UT61E+ and
completed without energizing the output.
