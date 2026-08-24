---
name: openbench
description: Operate and automate the local VlasovLab OpenBench laboratory-control application through its supported REST API. Use for server health and startup, instrument discovery and status, UT197/UT61D/UT61E/UT61E+/Micsig MHO1/Micsig ETO5004/FeelElec FY-series/FNIRSI DPS-150/OWON SPM/ITECH IT6054C/Kingst LA2016/simulated-meter access, Micsig scope settings and acquisition control, FeelElec signal control, DPS-150 control and programs, OWON SPM source and built-in multimeter control, ITECH bidirectional source/sink control, Kingst logic acquisition configuration/start/arm/stop/status/download, live measurements, instrument context and polling, oscilloscope Screen/Data capture, snapshots, CSV recordings, and external multi-instrument experiment orchestration. Also use when modifying OpenBench code or diagnosing Codex integration.
---

# OpenBench

Use the Dashboard as the human interface and the local JSON API as the Codex
interface. Avoid browser clicking and direct serial, Bluetooth, or oscilloscope
protocol work when the supported API can perform the task.

## Connect

1. Run `python scripts/openbench_api.py health` from this skill directory.
2. If the server is unavailable, locate the OpenBench project. Prefer the current
   repository or ask the user for its path; never assume a machine-specific path.
3. Start it with `scripts\start-openbench.ps1 -NoBrowser` from the project root.
   Do not start a second server when health already succeeds.
4. Run `devices` and, when needed, `channels` before using identifiers. Never
   invent a device or channel ID.
5. Expect original-series and `discover ut61eplus` calls to return multiple meters.
   Keep every returned ID distinct and target settings or disconnect actions by
   the exact ID; never assume there is only one meter of a given model.
6. For an original meter, use `discover ut61d` or `discover ut61e` exactly as
   labeled on the instrument. Their one-way stream has no model identifier.
7. Use `discover micsig_eto` for ETO5004. This exact-model driver is not
   silently scanned at startup, and its returned serial-number ID must be used.
8. Use `discover itech_it6000c` for the ITECH source/load. It is not silently
   scanned at startup. Keep the exact serial-number device ID and read its full
   state before changing any operating point.

Read [references/automation-api.md](references/automation-api.md) when exact
endpoints, request fields, response fields, limits, or failure cases are needed.
If the repository has changed or the API rejects a documented call, inspect the
running `/openapi.json` and treat it as authoritative.

## Use the helper

Run `python scripts/openbench_api.py --help` for all commands. Common calls:

```powershell
python scripts/openbench_api.py devices
python scripts/openbench_api.py discover ut197
python scripts/openbench_api.py discover ut61d
python scripts/openbench_api.py discover ut61e
python scripts/openbench_api.py discover micsig_eto
python scripts/openbench_api.py discover feeltech
python scripts/openbench_api.py discover dps150
python scripts/openbench_api.py discover owon_spm
python scripts/openbench_api.py discover kingst
python scripts/openbench_api.py discover itech_it6000c
python scripts/openbench_api.py latest
python scripts/openbench_api.py generator-get DEVICE_ID
python scripts/openbench_api.py generator-set DEVICE_ID 2 --frequency-hz 1000 --amplitude-vpp 2 --output off
python scripts/openbench_api.py generator-outputs DEVICE_ID --ch1 off --ch2 off
python scripts/openbench_api.py generator-sync DEVICE_ID frequency on
python scripts/openbench_api.py generator-burst DEVICE_ID off 10
python scripts/openbench_api.py generator-counter DEVICE_ID --mode frequency --gate-time-s 1 --coupling dc
python scripts/openbench_api.py generator-counter-pause DEVICE_ID
python scripts/openbench_api.py generator-sweep DEVICE_ID --target frequency --start 100 --end 10000 --duration-s 1 --mode linear --source time --enabled off
python scripts/openbench_api.py power-get DEVICE_ID
python scripts/openbench_api.py power-set DEVICE_ID --voltage-v 1.4 --current-a 0.05 --output off
python scripts/openbench_api.py power-protections DEVICE_ID --ovp-v 2 --ocp-a 0.1
python scripts/openbench_api.py power-metering DEVICE_ID on
python scripts/openbench_api.py power-sequence DEVICE_ID --step 0.5,0.01,0.5 --step 1,0.01,0.5
python scripts/openbench_api.py power-sweep DEVICE_ID voltage --start 0.5 --end 1 --step 0.1 --fixed-value 0.01 --dwell-s 1
python scripts/openbench_api.py power-program-status DEVICE_ID
python scripts/openbench_api.py power-program-stop DEVICE_ID
python scripts/openbench_api.py itech-list
python scripts/openbench_api.py itech-get DEVICE_ID
python scripts/openbench_api.py itech-set DEVICE_ID --priority CV --voltage-v 1 --output off
python scripts/openbench_api.py itech-set DEVICE_ID --output on --wiring-confirmed
python scripts/openbench_api.py itech-set DEVICE_ID --output off
python scripts/openbench_api.py itech-protections DEVICE_ID --ovp on --ovp-level 12 --ocp-level 1
python scripts/openbench_api.py itech-clear-protection DEVICE_ID
python scripts/openbench_api.py itech-advanced DEVICE_ID --watchdog off --watchdog-delay-s 30
python scripts/openbench_api.py smu-list
python scripts/openbench_api.py smu-get DEVICE_ID
python scripts/openbench_api.py smu-set DEVICE_ID --voltage-v 1 --current-a 0.1 --output off
python scripts/openbench_api.py smu-protections DEVICE_ID --ovp-v 12 --ocp-a 1
python scripts/openbench_api.py smu-dmm DEVICE_ID dc_voltage
python scripts/openbench_api.py smu-dmm DEVICE_ID --range-mode manual --range-value 20 --relative off --hold off
python scripts/openbench_api.py logic-list
python scripts/openbench_api.py logic-settings-get DEVICE_ID
python scripts/openbench_api.py logic-settings-set DEVICE_ID --channels 0,1,2,3 --sample-rate-hz 20000000 --sample-count 2000000 --threshold-v 1.4 --capture-ratio-percent 50 --trigger CH0=rising
python scripts/openbench_api.py logic-status DEVICE_ID
python scripts/openbench_api.py logic-start DEVICE_ID --title "Immediate logic" --comment "No trigger"
python scripts/openbench_api.py logic-arm DEVICE_ID --title "Triggered logic" --comment "Wait for CH0"
python scripts/openbench_api.py logic-stop DEVICE_ID
python scripts/openbench_api.py logic-download DEVICE_ID CAPTURE_ID capture.sr
python scripts/openbench_api.py settings-get DEVICE_ID
python scripts/openbench_api.py settings-set DEVICE_ID --context "Power stage output" --poll-interval-s 0.5
python scripts/openbench_api.py settings-set DEVICE_ID --screen on --data on --scope-channel CH1 --scope-channel CH3
python scripts/openbench_api.py scope-measurements-get DEVICE_ID
python scripts/openbench_api.py scope-measurements-set DEVICE_ID --measurement CH1:amplitude --measurement CH1:frequency
python scripts/openbench_api.py scope-measurements-set DEVICE_ID --measurement CH1:phase:CH2 --measurement CH1:delay:CH2:FRISe:FRISe
python scripts/openbench_api.py scope-measurements-read DEVICE_ID
python scripts/openbench_api.py scope-maximum-start DEVICE_ID --channel CH1
python scripts/openbench_api.py scope-maximum-status DEVICE_ID
python scripts/openbench_api.py scope-maximum-download DEVICE_ID mho1_ch1_maximum_ascii.txt
python scripts/openbench_api.py snapshot --title "Idle" --comment "24 V, no load"
python scripts/openbench_api.py record-start --title "Warm-up" --duration-s 600
python scripts/openbench_api.py record-start --title "Load steps" --scope-capture-mode manual
python scripts/openbench_api.py record-scope-frame DEVICE_ID --label "sink_set_4A"
python scripts/openbench_api.py record-status
python scripts/openbench_api.py record-stop
python scripts/openbench_api.py itech-measurements DEVICE_ID
```

Preserve user-supplied title, comment, and instrument context exactly unless the
user asks for editing. Report returned filenames and useful status fields.

## Operate safely

- Discover only the requested driver; use `discover all` only when the user asks
  to search all physical instruments. `discover all` intentionally excludes the
  simulated meter; connect it explicitly with `discover simulated`.
- Treat `connected: false` and stale/unavailable measurements as offline state.
- Do not disconnect an instrument or stop an active recording unless requested
  or required to complete the user's explicit workflow.
- Before any generator change, run `generator-get` and preserve every omitted
  setting. Channel updates pause an enabled channel, apply and read back each
  basic parameter, then restore only the explicitly requested output state.
- Enabling an output, starting a sweep, issuing a manual burst trigger, or
  loading a preset requires explicit user intent and known load/wiring context.
  Never infer permission to energize an output. Turning outputs off is safe.
- Prefer `generator-outputs DEVICE_ID --ch1 off --ch2 off` for an ordinary
  generator stop. Use `all-outputs-off` only for an explicit emergency or
  all-bench stop because it also latches safety and opens the matrix.
- Before any DPS-150 change, run `power-get` and preserve omitted settings.
  Enabling Output, applying a preset with `--output on`, starting a sequence or
  sweep, or stopping with `--keep-output` requires explicit user intent and
  known load/wiring context. Turning Output off is safe.
- DPS-150 writes are verified through a full hardware read-back. Voltage uses
  0.01 V steps, current 0.001 A steps, and live `upper_voltage_v` /
  `upper_current_a` may be lower than nameplate limits. Preset save changes
  nonvolatile memory. Metering has verified start/stop but no reset command.
- One DPS-150 program may run per device. Natural completion, failure, and the
  default program stop force Output OFF. Pause freezes dwell time; resume
  rechecks the safety state. Disconnecting a DPS-150 also verifies Output OFF
  before releasing its COM port.
- Before any ITECH change, run `itech-get` and preserve omitted settings. The
  only live-tested profile is `IT6054C-800-225` (800 V, +/-225 A, 54 kW), so
  treat it as high-energy equipment even when commanded to a low setpoint.
  Positive current sources and negative current sinks. Use CV priority with
  signed current limits, or CC priority with signed current setpoint and
  voltage limits. Never infer permission to enable Output: it requires explicit
  operator intent, known wiring/load, `--wiring-confirmed`, matrix safety, and
  enabled OVP/OCP/OPP. Protection and advanced changes require Output OFF.
  Clear a latched protection only with `itech-clear-protection` while Output is
  OFF; this does not alter protection thresholds.
  A single active `current_setpoint_a` update in CC or `voltage_setpoint_v`
  update in CV may be applied live without toggling Output, but still requires
  wiring confirmation. Priority, limits, power limits, and multi-field changes
  pause an active output. All writes are fully read back. Output OFF is safe;
  disconnect, shutdown, and emergency stop force it. Do not use direct SCPI or
  non-fixed functions.
- ITECH rediscovery is safe to use for recovery: a disconnected stable serial
  identity replaces its stale COM transport. On Windows, Access Denied causes
  one bounded release attempt for known NI-VISA COM claimants and a retry of
  the same port. Discovery failure cleanup closes the transport without an
  instrument write; it never enables or reconfigures Output.
  The front panel must be set to `SYSTEM I/O -> USB-VCP`, with matching
  `115200, 8-N-1`; `USB-TMC` is a different interface and will not appear as
  the supported virtual COM transport.
- Before an OWON SPM change, run `smu-get` and preserve omitted source or DMM
  settings. Enabling its source requires explicit operator intent and known
  wiring/load context. The live-tested SPM6103 profile enforces 60 V, 10 A,
  and the coupled 300 W limit; other SPM models are not accepted. Source writes use read-back; active setpoint changes
  pause the output, Output ON is last, and disconnect/shutdown/emergency stop
  force Output OFF. Use only `smu-dmm` for the eight documented functions,
  validated auto/manual ranges, relative mode, and Hold. Capacitance range is
  read-only because this SPM manual documents only its query. Do not attempt
  front-panel List waveform or startup-auto-output control: the published SCPI
  manual exposes no commands for those functions.
- FY6200 reports basic channel settings, outputs, synchronization, burst, and
  selected counter values with read-back. The counter starts paused. Use
  `frequency` for frequency/timing, `count` for pulse count, or `both` for both
  groups; OpenBench does not read the disabled group, while `both` can alternate
  the physical display. Selected `counter.*` channels are included in captures;
  dependent timing values are blank when no counter input is present. On the live-tested
  FY6200-20M, CH1 pulse width is 100 ns to 1 s in 10 ns steps. Built-in shaped
  waveforms 2-27 are limited to 10 Vpp; sine, square, modulation, and ARB slots
  support up to 19.999 Vpp at or below 10 MHz. Some firmware does not implement
  `RTA`, `RTF`, or `RTP`; inspect `advanced.unavailable_reads`. Sweep/VCO is
  documented write-only and is returned with `verified: false`. The published
  protocol does not expose AM/FM/PM depth/source or round-trip sweep; do not
  invent commands for those front-panel-only controls.
- Preset save overwrites nonvolatile instrument memory. Preset load may restore
  enabled outputs. This applies to both FeelElec and DPS-150 presets. Do neither
  without an explicit slot and user request.
- LA2016 uses atomic configure/start/arm/stop/status/download calls. `start`
  intentionally ignores configured triggers; `arm` requires them. Read status
  until `completed`, `stopped`, or `error`, then use the returned capture ID and
  download URL. `remaining_s` is a bounded acquisition/download estimate; it is
  intentionally absent while `armed` and waiting for an external signal.
  Native `.sr` files are decoded outside OpenBench.
- Use `auto_start_enabled` plus `auto_start_delay_s` only for a simple delay
  from global CSV RUN. Implement complex cross-instrument conditions in the
  calling Codex workflow or a small script by reading measurements and calling
  these atomic endpoints; do not invent or persist hidden Dashboard rules.
- The LA2016 PWM outputs are intentionally unavailable. Never bypass the API to
  send arbitrary sigrok CLI options. Keep the native Kingst WinUSB driver so
  KingstVIS remains usable. KingstVIS and OpenBench are mutually exclusive:
  close KingstVIS before OpenBench discovery/capture and finish OpenBench
  capture before reopening it. If `LIBUSB_ERROR_NOT_SUPPORTED` appears, verify
  that OpenBench selected `.openbench/tools/sigrok-modern/sigrok-cli.exe`; do
  not replace the USB driver with Zadig/libusbK.
- Do not send arbitrary SCPI, shell, GPIO, matrix, or safety-bypass commands
  through this skill. Use only the documented OpenBench endpoints.
- Use `/api/v1/oscilloscopes/...` for bounded MHO1/ETO5004 settings and acquisition
  control. `GET /measurements` reads the current profile. Apply the complete
  desired profile with one `PUT /measurements`; OpenBench sends CLEAR, waits
  100 ms, opens each selected slot with 100 ms pacing, and waits a final 100 ms.
  An empty list clears the profile. Firmware `2.154.75` has ten global slots
  across CH1-CH4. This configuration is a separate one-time operation and is
  never repeated inside a frame. `POST /measurements/read` reads the configured
  scalars without changing the profile. `PHASE` and `DELAY` are two-channel
  slots: use `secondary_channel`; `DELAY` also accepts `source_edge` and
  `target_edge` from `FRISe`, `FFALL`, `LRISe`, or `LFALL` (both default to
  `FRISe`). The two channels must be different.
- Every idle Dashboard MHO1 poll reads only the configured scalar measurements,
  keeps acquisition in RUN, captures no artifacts, and persists nothing. These
  values exist only in memory and on the live event bus until the common RUN
  workflow subscribes and saves them. Screen and ASCII data are independent
  explicit-frame options, and ASCII has a CH1-CH4 selection. With data enabled,
  the explicit frame transaction is STOP; only the selected
  SOURCE -> MODE NORMAL -> ASCII reads with one common preamble; optional direct
  screenshot; scalar queries; and RUN in `finally`. With no selected waveform
  data, an explicit frame does not send STOP/RUN: it requests only the optional
  direct screenshot and the scalars. It performs no state/read-back query and
  has no fixed post-STOP delay. A direct screenshot may make one paced retry for an
  empty or invalid response and never falls back to an oscilloscope-side file.
  Screenshot pixels are never waveform data.
- `scope_wait_for_trigger` is a persistent common-RUN option. When enabled,
  OpenBench sends SINGLE, polls `TRIGger:STATus?` every 50 ms, treats STOP as the
  completed frozen acquisition, reads it without another STOP/RUN pair, saves a
  trigger row plus the relative artifact path in the common CSV, and re-arms.
  Cancellation, error, recording STOP, and shutdown restore continuous RUN.
- A global snapshot always writes an MHO1 capture folder with measurements CSV
  and capture JSON. The JSON preserves the UTC timestamp, frame duration,
  selected channels, common nine-field preamble, and sample timing. Enabled
  channels add their unmodified ASCII payloads and one combined numeric waveform
  CSV; Screenshot adds the image. The manifest exposes the combined file as
  `files.waveforms_csv`.
- Dashboard MHO1 polling defaults to two seconds and the API and UI reject a
  faster interval. OpenBench also preserves at least 250 ms of RUN acquisition
  time between waveform frames. The binary endpoints and stored BIN workflows
  are retained only for explicit diagnostics and are not part of production
  MHO1 acquisition.
- ETO5004 firmware `3.392.132` uses standard `DATA?` with MODE NORMAL,
  FORMAT WORD, START 1, and STOP 1100. It returns four ASCII hexadecimal
  characters per sample; the driver applies the firmware-specific 0.5 vertical
  scale correction. One selected channel is physically validated, but the
  second source in the same stopped frame returns an empty block, so do not
  promise atomic multi-channel ETO Data. Direct screenshot probes still return
  no image, but the documented stored screenshot plus HTTP-download fallback is
  physically validated and Screen may be enabled. It creates a scope-side file
  in `/pictures/Screenshots` for every capture.
- For a deliberate full-memory MHO1 or ETO5004 export, use the dedicated
  `/api/v1/oscilloscopes/{device_id}/maximum-capture` job or the
  `scope-maximum-*` helper commands. Start it only after the instrument already
  reports STOP and only when common CSV recording is inactive. It uses MODE
  MAXIMUM and the current `ACQuire:DEPTh?`, streams 15,625-point ASCII chunks,
  never writes the memory-depth selection, and never changes STOP/RUN state.
  Poll status until `completed` or `error`; do not issue device/scope mutations,
  Disconnect, Snapshot, or another capture while it is active. There is no
  ordinary cancellation endpoint because partial operator actions during this
  long one-shot read are intentionally excluded.
  ETO5004 has live STOP-gating coverage. The MHO1 path uses fresh SCPI sessions
  between commands for firmware `2.154.75` but currently has mocked coverage
  only; do not call it physically validated until
  `docs/mho1-maximum-ascii-test-plan-2026-08-03.md` is completed at the bench.
- Safe instrument preferences persist by stable device ID: context and polling,
  MHO1 artifact/channel/measurement selection, and LA2016 acquisition setup.
  Reconnection never replays output enable or starts an acquisition.
- Expect only one active CSV recording. On HTTP 409, inspect `record-status`
  rather than silently replacing the recording.
- Common CSV recording defaults to periodic scope frames. For exact
  cross-instrument steps, start it with `--scope-capture-mode manual` and call
  `record-scope-frame` at each point. Each call uses the stored Screen/Data and
  CH1-CH4 selection, always reads configured Measurements, and records the
  label plus relative artifact manifest in the common CSV. Stop the recording
  to restore ordinary scope polling.
- Common recording separates invariant settings from streamed measurements.
  For ITECH, only measured V/I, calculated `P = V * I`, and commanded V/I
  receive repeating columns;
  signed limits and initial Output/priority/direction appear once in the
  instrument's `initial_settings` header. A Snapshot remains a complete
  single-row state capture.
- For stepped ITECH CC/CV experiments, read complete state once before the
  series. A single live current/voltage setpoint PATCH is command-only and uses
  cached invariant settings. After the required settle, call the compact
  `/bidirectional-power-supplies/{device_id}/measurements` resource exactly
  once; it reads only V/I, calculates `P = V * I`, and publishes V/I/P. Run that
  compact read concurrently with the oscilloscope frame when the experiment
  permits it. The validated local six-point launcher is
  `Run ITECH MHO1 Experiment.cmd`: each point writes the current, waits one
  second, captures both instruments in parallel, and immediately advances after
  both operations complete. Its default MHO1 profile is CH1 Maximum/High, CH2
  Maximum/High, CH3 Frequency/Amplitude, and CH4 Frequency/Amplitude.
- On tested MHO14-200 firmware `2.154.75`, the timebase-mode query can report
  `XY` while the front panel visibly remains in normal YT mode. Never write a
  scope mode to resolve that discrepancy. The launcher requires the operator to
  confirm visible YT mode as part of its exact-confirmation flow.
- Keep stepped-experiment discovery, read-only preflight, plan display, exact
  confirmation, and execution in one Python process. After confirmation, POST
  the ITECH `experiment-reservation` before the first write. Keep it through
  verified Output OFF and settings restoration, then DELETE it. The helper
  commands are `itech-reserve DEVICE_ID` and `itech-release DEVICE_ID`.
  OpenBench remains the only COM owner.
- Every stepped ITECH launcher bounds both its live current-step command and
  compact U/I read-back to three seconds, treating the read-back as a safety
  heartbeat. A request failure or missing U/I
  aborts the remaining points and immediately forces Output OFF, followed by a
  fresh full-state read-back. If the command or verification loses the COM
  transport, it makes up to four bounded attempts, rediscovers only
  `itech_it6000c`, requires the same stable serial-ID device to reconnect, and
  retries OFF. The same verified shutdown is mandatory after the final point
  or before CSV stop on error. Do not add state reads to successful intermediate
  points. If OFF still cannot be verified, treat the experiment as critical and
  instruct the operator to
  use the front panel or mains disconnect immediately.
- Use the API's polling limits. Do not force a faster rate than the returned
  `minimum_poll_interval_s`.

## Change OpenBench

When editing the application itself, read the repository `AGENTS.md` and relevant
tests first. Keep the REST API backward-compatible with this helper and update
both `docs/automation-api.md` and this skill reference when endpoints change.
Treat reproducibility as part of every physical driver: never leave a required
binary, patched library, firmware workflow, or generated runtime only in an
ignored local directory. Pin upstream sources/downloads, record SHA-256 values,
track patches, provide setup/build and diagnostic scripts, generate artifact
manifests, preserve third-party licenses, and add a dated physical live-test
record. Follow `docs/portability.md` and `docs/driver-development.md`. Do not
commit proprietary firmware when a verified local extraction workflow can
recreate it.
