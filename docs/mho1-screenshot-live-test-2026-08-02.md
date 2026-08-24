# MHO1 direct screenshot live test — 2026-08-02

## Bench

- Instrument: Micsig MHO14-200, serial `MHO1-DEMO-0001`
- Firmware: `2.154.75`
- Network address: `192.0.2.10`
- OpenBench API only; no direct driver or arbitrary SCPI invocation

## Result

The documented `:SYS:SCR?` query was tested through the scope's raw TCP SCPI
service and its discovered VXI-11 service in both RUN and STOP states.

| State | Transport | Payload | Result |
| --- | --- | ---: | --- |
| RUN | TCP | 192,289 bytes | Complete malformed-JFIF stream |
| STOP | TCP | 192,961 bytes | Complete malformed-JFIF stream |
| RUN | VXI-11 | 0 bytes | No image |
| STOP | VXI-11 | 0 bytes | No image |

RUN versus STOP did not affect the outcome. USBTMC was not tested because the
oscilloscope was not connected to the computer by USB.

The TCP payload begins with the same contradictory pattern illustrated in the
Micsig SCPI manual: it is described as PNG, but contains a JPEG/JFIF stream.
On this unit the normal `FF D8 FF E0 00 10 JFIF` prefix arrives as
`FF D8 ?? ?? 00 10 JFIF`. OpenBench now repairs only those two marker bytes
when the rest of this exact signature matches.

One normal OpenBench snapshot then completed in 0.339 seconds. Its repaired
JPEG was 193,644 bytes, decoded successfully as 1280x800, and the scope's
`/pictures/Screenshots` entry count remained 50 before and after the capture.
The stored-file method remains a fallback for empty or unrecognized responses.
Normal capture now enforces a one-second minimum interval between hardware
screenshot commands and returns the most recent completed image to callers
inside that interval.

Reference: [Micsig Oscilloscope SCPI Command Manual, January 2026](https://www.micsig.com/uploads/Micsig-Oscilloscope-SCPI-Command-Manual-EN-202601_1770002260.pdf).
