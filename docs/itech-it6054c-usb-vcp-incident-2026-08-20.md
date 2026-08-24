# ITECH IT6054C USB-VCP experiment incident — 2026-08-20

## Observed failure

- Instrument: ITECH `IT6054C-800-225`, stable OpenBench ID
  `itech_it6000c_ITECH-DEMO-0001`, USB VID/PID `2EC7:A4A7`.
- Repeated experiment preflight/discovery attempts eventually left the virtual
  COM device absent from Windows, not merely busy inside OpenBench.
- Replugging did not reliably restore it. A Windows reboot restored the device
  as a new temporary `COMn` port with Device Manager status OK.
- The planned powered experiment was not completed. No result from this date
  is a valid hardware-test record.

## Software audit

The experiment launcher is a separate REST client, but it never opens the COM
port. The OpenBench server is the only serial owner. Two independent read paths
could nevertheless add transactions to the same server-owned transport:

1. the ordinary per-channel Dashboard scheduler remained active while the
   experiment issued its explicit per-point reads;
2. common CSV startup forced another complete ITECH state read for the invariant
   header.

The previous workaround changed the Dashboard interval to 600 seconds. Updating
the interval cancels and recreates each polling task, so the workaround could
itself cause immediate scheduler activity and was not exclusive ownership.
Driver locks prevented byte-level overlap, but serialization alone did not
remove these unnecessary USB-VCP transactions.

## Corrective change

OpenBench now has an explicit ITECH experiment reservation. It waits for an
in-flight scheduler transaction, suspends all ordinary ITECH poll targets, and
leaves the persisted polling interval unchanged. Reserved compact measurements
still perform exactly `MEAS:VOLT?` and `MEAS:CURR?`; power is calculated. Common
CSV startup uses cached invariant state while reserved. The launcher acquires
the reservation only after exact operator confirmation, holds it through
verified Output OFF and settings restoration, and then releases it. Ordinary
polling waits one complete configured interval after release instead of doing a
catch-up read.

Automated tests cover reservation, duplicate rejection, compact reads, the
absence of an extra CSV-start read, cleanup release, and deferred polling
resume. No hardware commands were sent while implementing this change.

## Required controlled validation

Before another powered experiment:

1. reboot/start from a visible, healthy USB-VCP device and keep Output OFF;
2. run one discovery and one read-only preflight;
3. acquire the reservation and observe a short Output-OFF compact-read sequence;
4. confirm only one V/I pair per requested sample and no scheduler/full-state
   reads between points;
5. release the reservation and confirm the first normal poll occurs only after
   the configured interval;
6. recheck that Windows still enumerates the USB-VCP device;
7. only then run a low-current experiment before returning to normal use.

The reservation removes confirmed software-side excess traffic. A remaining
Windows USB driver, host-controller, cable/EMI, or instrument VCP-firmware issue
cannot be ruled out until that controlled physical validation passes.
