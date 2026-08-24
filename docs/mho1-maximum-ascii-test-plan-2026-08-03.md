# Micsig MHO1 MAXIMUM ASCII deferred test plan — 2026-08-03

## Status

Implementation complete with mocked driver, service, API, and Dashboard
coverage. Physical validation is deferred because the MHO1 is not available on
the bench on 2026-08-03. This document is not a live-test success record and
the full-memory path must remain marked unvalidated until the procedure below
is completed and observed results are appended.

Target previously used by OpenBench: Micsig MHO14-200, serial `MHO1-DEMO-0001`,
firmware `2.154.75`, LAN SCPI. The official reference is
`Micsig-Oscilloscope-SCPI-Command-Manual-EN-202601`.

## Implemented bounded contract

- The dedicated action accepts CH1–CH4 and starts only when trigger status is
  already `STOP`.
- It reads current `ACQuire:DEPTh?` and never writes `ACQuire:DEPSelect`.
- It uses documented `MODE MAXIMUM`, `FORMAT ASCII`, `START`, `STOP`, and
  standard `DATA?`; ASCII blocks are limited to 15,625 points.
- It never sends acquisition STOP, RUN, or SINGLE and leaves acquisition
  stopped.
- It holds the driver operation lock, suspends live polling, excludes common
  capture, blocks scope mutations, streams to disk, and publishes point-based
  progress.
- MHO1 commands and reads use fresh SCPI sessions to match the known behavior
  of firmware `2.154.75`.

## Physical procedure to run at home

Use only the tracked REST helper, Dashboard controls, or physical front panel;
do not send raw SCPI.

1. Confirm safe read-only bench state, start OpenBench, run `health`, discover
   the MHO1, and record the exact identity and firmware.
2. Run `scope-get DEVICE_ID`; record trigger state, current effective memory
   depth, waveform source/mode/format/start/stop, and free disk space.
3. While the scope is in RUN, call
   `scope-maximum-start DEVICE_ID --channel CH1`. It must return HTTP 409, send
   no acquisition action, and leave all state unchanged.
4. Stop the scope explicitly. Do not change memory depth as part of the export.
   For the first validation, a modest depth selected beforehand is preferable.
5. Start one CH1 export, poll `scope-maximum-status DEVICE_ID`, and verify
   monotonic point progress with no concurrent scope controls enabled.
6. After completion, verify the text file contains exactly the recorded depth
   in numeric tokens, `capture.json` reports `memory_depth_changed: false`, and
   the acquisition state is still STOP. Verify the previous waveform-reader
   source/mode/format/start/stop values were restored.
7. Record elapsed time, artifact byte sizes and SHA-256 hashes, then repeat with
   two displayed channels if the first transfer is stable.
8. Start a common CSV recording and verify a new maximum job is rejected; then
   stop that recording normally.
9. Append exact observations, failures, representative hashes, and final
   validated/unsupported status to this file. Do not claim support from mocked
   tests alone.

## Automated evidence currently available

Tests enforce STOP-only start, absence of depth and acquisition writes,
15,625-point chunk boundaries, exact point counts, reader-context restoration,
service ownership, MHO1 artifact naming, progress completion, API status, and
Dashboard rendering. The full repository suite must remain green before the
physical procedure is attempted.
