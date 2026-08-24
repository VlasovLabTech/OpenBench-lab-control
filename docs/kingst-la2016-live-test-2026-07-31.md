# Kingst LA2016 integration record — 2026-07-31

## Identified hardware

- Model: Kingst LA2016
- USB VID/PID: `77A1:01A2`
- Stable USB location: `usb/2-9` (bus/address is transient)
- Manufacture date reported from EEPROM: 2019-11
- Logic inputs: CH0–CH15
- Maximum sample rate: 200 MHz
- sigrok driver: `kingst-la2016`
- FPGA selected by the driver: `kingst-la2016a1-fpga.bitstream`

The driver also reports PWM1 and PWM2. OpenBench intentionally excludes them
and exposes only read-only logic acquisition.

## Installed software

- sigrok-cli `0.8.0-git-f44dd91`
- libsigrok `0.6.0-git-0bc2487`
- libusb `1.0.30`
- PulseView nightly Windows package
- Kingst firmware extracted from official KingstVIS 3.6.5

OpenBench executable:

```text
.openbench\tools\sigrok-modern\sigrok-cli.exe
```

Firmware directory:

```text
%LOCALAPPDATA%\sigrok-firmware
```

Files used by this hardware revision:

| File | SHA-256 |
|---|---|
| `kingst-la-01a2.fw` | `87A8BBC96BED5E179069717F971E05B3A3D1703D4EF096F2B276D454F129BD22` |
| `kingst-la2016a1-fpga.bitstream` | `9110D239F8F117D3333574F5493D477873FEEF73DB82EE8025D68C73A381A159` |

Firmware loaded by sigrok is volatile acquisition firmware; OpenBench does not
flash persistent device firmware.

## Confirmed capabilities

Source inspection and driver configuration queries confirmed:

- sample rates: 20 kHz, 50 kHz, 100 kHz, 200 kHz, 500 kHz, 1 MHz, 2 MHz,
  5 MHz, 10 MHz, 20 MHz, 50 MHz, 100 MHz, and 200 MHz;
- thresholds: 0.4, 0.6, 0.9, 1.2, 1.4, 2.0, 2.5, and 4.0 V;
- capture sizes from 1 through 10,000,000,000 samples;
- multiple low/high level trigger conditions;
- at most one rising/falling edge trigger;
- configurable capture ratio for pre-trigger samples.

## Live result

The native Kingst WinUSB driver was retained. A current libusb probe opened and
claimed the physical interface successfully; no Zadig or libusbK replacement
was performed. OpenBench uses a local sigrok runtime with modern libusb and a
timer-backed Windows event source.

Three consecutive REST API captures completed without reconnecting the device:

- CH0 enabled;
- 20 kHz sample rate;
- 20 KSa requested;
- 1.4 V threshold;
- no trigger;
- stable device ID `kingst_la2016_usb-2-9`;
- each result contained a valid `capture.sr` with metadata and logic data.

A later Dashboard capture was also started while the global CSV recording was
active. The operator confirmed that the dedicated logic-capture directory and
files were created and that the common CSV timeline contained the matching
capture/folder reference, so the artifact can be unambiguously associated with
the main run.

The current Windows runtime can terminate with `0xC0000005` during process
cleanup after it has written and closed a complete session. OpenBench accepts
that one exact exit only after validating the ZIP structure, CRCs, metadata,
and a non-empty logic entry; every other non-zero exit remains an error.

KingstVIS has no documented hardware-acquisition API, so OpenBench does not
automate it. The two applications are used sequentially: close KingstVIS before
an OpenBench capture, then reopen it after OpenBench finishes.

## Remaining hardware checklist

1. Configure a CH0 rising trigger and 50% capture ratio.
2. Arm, apply a known safe digital edge to CH0, and confirm `triggered_at`,
   countdown, completion, metadata, and artifact download.
3. Hardware-triggered capture during a global CSV run remains to be checked;
   the immediate-capture CSV/artifact association is confirmed above.
