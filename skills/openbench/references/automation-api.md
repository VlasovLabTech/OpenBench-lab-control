# OpenBench automation API

This is a compact snapshot of the supported automation surface. The live schema
at `http://127.0.0.1:8000/openapi.json` is authoritative after application
changes.

Base URL: `http://127.0.0.1:8000/api/v1`

## Contents

- [System and discovery](#system-and-discovery)
- [Signal generator](#signal-generator)
- [Micsig oscilloscopes](#micsig-oscilloscopes)
- [FNIRSI DPS-150 power supply](#fnirsi-dps-150-power-supply)
- [ITECH IT6000C bidirectional source/load](#itech-it6000c-bidirectional-sourceload)
- [OWON SPM source-measure unit](#owon-spm-source-measure-unit)
- [Instrument settings](#instrument-settings)
- [Snapshot](#snapshot)
- [CSV recording](#csv-recording)

## System and discovery

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Check server readiness |
| `GET` | `/devices` | List registered devices and connection state |
| `POST` | `/devices/discover/{driver}` | Discover `simulated`, `ut197`, `ut61d`, `ut61e`, `ut61eplus`, `micsig`, `micsig_eto`, `feeltech`, `dps150`, `owon_spm`, `kingst`, `itech_it6000c`, or `all` |
| `DELETE` | `/devices/{device_id}` | Disconnect one registered device |
| `GET` | `/channels` | List measurement channels |
| `GET` | `/channels/{channel_id}/latest` | Read the latest measurement |

Discovery returns `404` when no matching instrument is found and `503` on a
transport or driver failure. A device contains `id`, `name`, `kind`,
`connected`, and `capabilities`. A channel contains `id`, `device_id`, `name`,
`capability`, `unit`, `poll_interval_s`, and `state`.

UT61D, UT61E, and UT61E+ discovery can return multiple devices of the same kind.
Original UT61D/UT61E packets do not identify the model; select the matching
driver explicitly.
The `all` target scans physical instrument drivers only and never connects the
simulated meter; use the `simulated` target explicitly when it is wanted.
UT61E+ supports CH9329 (`1A86:E429`) and CP2110 (`10C4:EA80`) USB-HID
adapters. Always use the returned device and channel IDs rather than deriving
or reusing one ID for all meters.
FeelElec FY-series generators are discovered as `feeltech` through CH340 USB
serial adapters. They expose read-only waveform, frequency, amplitude, offset,
duty cycle, phase, and output-state channels for CH1 and CH2, plus live
`counter.frequency`, `counter.count`, `counter.period`,
`counter.positive_width`, `counter.negative_width`, and `counter.duty` channels
from the separate COUNTER IN connector. These twenty channels are all captured.
Use the returned exact model name and IDs.
FNIRSI DPS-150 discovery uses the AT32 virtual COM identity (`2E3C:5740`),
validates model/HW/FW, and registers fifteen channels. The live-tested unit
reports `DPS-150`, HW `V1.0`, FW `V1.2`; minimum polling is 0.5 seconds.
OWON SPM discovery probes CH340 ports with read-only `*IDN?`, accepts only the
live-tested SPM6103 reply, and uses the instrument serial number for stable multi-unit IDs.
SPM and FeelElec scans are serialized because both use `1A86:7523`. The tested
SPM6103 is serial `SPM-DEMO-0001`, FW `FV:V2.1.0`; minimum polling is 0.5 seconds.
Kingst discovery uses sigrok and registers every `kingst-la2016` descriptor by
its exact USB connection ID. The tested LA2016 is `77A1:01A2`, with 16 logic
inputs and a 200 MHz maximum. PWM outputs are intentionally excluded.
ITECH discovery probes only USB VCP `2EC7:A4A7`, tries 115200 then 9600 baud,
and currently accepts only the live-tested `IT6054C-800-225`. Its stable ID
uses the instrument serial number. Automatic startup discovery is off by
default; use the explicit target, Find All, or enable
`OPENBENCH_ITECH_IT6000C_AUTO_DISCOVER`.
Rediscovery replaces a disconnected device's stale COM transport by stable
serial identity. On Windows, an access-denied serial open gets one bounded
known-NI-claim release and same-port retry. Discovery failure cleanup only
closes the transport and never writes an Output command.

`micsig_eto` accepts only the exact `ETO5004` model and derives its stable ID
from the returned serial number. It is not silently scanned at startup; call
the explicit discovery target or enable `OPENBENCH_MICSIG_ETO_AUTO_DISCOVER`.
Fixed hosts may be supplied through `OPENBENCH_MICSIG_ETO_HOSTS`.

The latest measurement contains:

```json
{
  "timestamp_utc": "2026-07-30T10:00:00Z",
  "monotonic_s": 123.45,
  "device_id": "device-id",
  "channel_id": "channel-id",
  "value": 12.34,
  "unit": "V",
  "quality": "valid",
  "status": "ok"
}
```

## Signal generator

```http
GET /generators/{device_id}
PATCH /generators/{device_id}/channels/{1|2}
PUT /generators/{device_id}/outputs
PATCH /generators/{device_id}/synchronization
PATCH /generators/{device_id}/burst
POST /generators/{device_id}/burst/trigger
PATCH /generators/{device_id}/keying
PATCH /generators/{device_id}/counter
POST /generators/{device_id}/counter/pause
POST /generators/{device_id}/counter/reset
PATCH /generators/{device_id}/sweep
POST /generators/{device_id}/presets/{1..20}/save
POST /generators/{device_id}/presets/{1..20}/load
```

Channel PATCH accepts any nonempty subset of:

```json
{
  "waveform_code": 0,
  "frequency_hz": 1000,
  "amplitude_vpp": 2,
  "offset_v": 0,
  "duty_percent": 50,
  "phase_deg": 0,
  "pulse_width_ns": 50000,
  "output_enabled": false
}
```

Basic channel writes and output states are read back and verified. An enabled
channel is paused while signal parameters change. Output enable, manual trigger,
enabled sweep, and preset load require explicit user intent and safe wiring.
`pulse_width_ns` is CH1-only (20 ns to 1 s) and must be shorter than the
configured period.

Payloads for the other controls:

```json
{"channel_1": false, "channel_2": false}
{"parameter": "frequency", "enabled": true}
{"source": "off", "cycles": 10}
{"cycles": 10}
{"kind": "fsk", "source": "manual", "secondary_frequency_hz": 1000}
{"mode": "frequency", "gate_time_s": 1, "coupling": "dc"}
{"target": "frequency", "start": 100, "end": 10000, "duration_s": 1,
 "mode": "linear", "source": "time", "enabled": false}
```

The counter starts paused. Use mode `frequency` for frequency/timing, `count`
for pulse count, or `both` for both groups. Disabled groups are not queried;
`both` can alternate the physical counter display.

Some FY6200 firmware does not implement ASK/FSK/PSK read commands. Inspect
`advanced.unavailable_reads`; unavailable fields are `null`. Sweep has no
hardware read-back and the response marks it `verified: false`. `source: "vco"`
maps an external 0–5 V signal at COUNTER IN to the selected Start/End range.
The FSK secondary frequency does have independent read-back and is verified.
AM/FM/PM depth/source and round-trip sweep are not exposed by the published
serial protocol and remain front-panel-only. Preset save overwrites nonvolatile
memory; preset load can enable stored outputs.

## Micsig oscilloscopes

```http
GET   /oscilloscopes/{device_id}
PATCH /oscilloscopes/{device_id}/settings
POST  /oscilloscopes/{device_id}/run
POST  /oscilloscopes/{device_id}/stop
POST  /oscilloscopes/{device_id}/single
POST  /oscilloscopes/{device_id}/screenshot-probe
POST  /oscilloscopes/{device_id}/fast-binary-probe
POST  /oscilloscopes/{device_id}/waveforms
GET   /oscilloscopes/{device_id}/measurements
PUT   /oscilloscopes/{device_id}/measurements
POST  /oscilloscopes/{device_id}/measurements/read
POST  /oscilloscopes/{device_id}/numeric-waveforms/csv
GET   /oscilloscopes/{device_id}/maximum-capture
POST  /oscilloscopes/{device_id}/maximum-capture
GET   /oscilloscopes/{device_id}/maximum-capture/files/{filename}
GET   /oscilloscopes/{device_id}/storage-index
POST  /oscilloscopes/{device_id}/storage-waveforms
POST  /oscilloscopes/{device_id}/storage-waveforms/import
GET   /oscilloscopes/{device_id}/storage-waveforms/{filename}

```

Scope settings accept bounded channel, acquisition, timebase, and EDGE trigger
updates; read state before changing them. Every idle Dashboard poll reads only
the configured scalar measurements while acquisition remains in RUN. It takes
no Screenshot/ASCII artifacts and writes nothing to SQLite or disk; values stay
in memory/on the live event bus until common RUN recording subscribes. Screenshot
and ASCII data are explicit-frame options, and `scope_channels` selects any
subset of CH1-CH4. If ASCII data is enabled, the tested MHO14-200 firmware
`2.154.75` uses one atomic NORMAL-mode
frame: STOP; SOURCE, MODE NORMAL, then ASCII for only the selected channels,
with one common preamble; optional direct `:SYS:SCR?`; scalar queries; and RUN
in `finally`. With ASCII data disabled, OpenBench sends no STOP/RUN and requests
only the optional screenshot and scalars. No path sends acquisition-state or
read-back queries or a fixed delay after STOP. Each tested channel returned
1,375 points.

`POST /waveforms` returns selected channels from this frame. The normal global
snapshot path writes a sibling folder containing measurements CSV, capture JSON,
the original ASCII payload for every enabled channel, one combined waveform CSV,
and the screenshot when
enabled. The JSON preserves the UTC timestamp, frame duration, selected
channels, common nine-field preamble, and sample timing. The combined CSV is
generated from the instrument's numeric ASCII payload; screenshot pixels are
never converted to traces. A normal direct screenshot may retry once, paced by one
second, when the first response is empty or invalid; it never falls back to a
scope-side saved file. `POST /screenshot-probe` remains a bounded TCP/VXI-11
transport diagnostic. `POST /fast-binary-probe` and stored BIN endpoints remain
available only for explicit diagnostics; binary data is not part of production
MHO1 acquisition.

ETO5004 uses the same bounded state, setting, acquisition, scalar, frame, and
numeric-CSV endpoints. Firmware `3.392.132` was validated with standard
`DATA?` after `MODE NORMAL`, `FORMAT WORD`, `START 1`, and `STOP 1100`.
It returns four ASCII hexadecimal characters per point; OpenBench parses 1,100
codes and applies the firmware-specific 0.5 vertical correction. One selected
channel is physically validated. A second source in the same stopped frame
returns an empty block, so atomic multi-channel ETO Data is not claimed.

The tested ETO firmware returned no direct screenshot over raw TCP or VXI-11.
Production Screen uses documented `:STORage:CAPTure:STARt`, discovers the new
PNG/JPEG in `/pictures/Screenshots`, and downloads it over HTTP. ETO therefore
advertises `screenshot_capture` and enables Saved frame Screen. Each capture
leaves a scope-side image; `screenshot-probe` remains the direct, file-free
firmware diagnostic and does not test this fallback.

The MHO1/ETO5004 `/maximum-capture` resource is a separate asynchronous
one-shot full-memory ASCII export. POST accepts `{"channels":["CH1","CH2"]}` and is
rejected unless the scope already reports STOP or while common CSV recording is
active. It never sends STOP/RUN and never writes memory depth: the driver reads
the current `ACQuire:DEPTh?`, selects MODE MAXIMUM plus ASCII, and streams each
selected channel in validated blocks of at most 15,625 points. It restores only
the waveform reader source/mode/format/start/stop settings, leaving acquisition
stopped.

Poll GET until `state` becomes `completed` or `error`; status includes point
counts and `progress_percent`. While `starting`, `capturing`, or `finalizing`,
OpenBench suspends live scope polling and rejects mutations, Disconnect, common
Snapshot/recording access, and another maximum capture. There is intentionally
no ordinary stop endpoint. Completion returns per-channel raw ASCII text and
`capture.json` download URLs; files are streamed to disk and partial data is
preserved after an error or shutdown.

ETO5004 has live STOP-gating coverage. The MHO1 implementation uses fresh SCPI
sessions between commands to match firmware `2.154.75`, but its full-memory
path currently has mocked contract coverage only and awaits completion of the
dated physical test plan.

`GET /measurements` returns the current Dashboard/front-panel profile without a
hardware write. `PUT /measurements` applies the complete requested profile: it
sends CLEAR, waits 100 ms, opens every requested slot with 100 ms after
each command, then waits a final 100 ms. An empty list clears all slots. Firmware
`2.154.75` exposes ten global slots across CH1-CH4. The UI exposes ten rows and
performs this replacement only when the operator presses **Apply settings**;
profile configuration never occurs inside a frame. `POST /measurements/read`
reads an already configured scalar list without CLEAR or OPEN.

Ordinary slots use `{"channel":"CH1","item":"amplitude"}`. `PHASE` uses
`{"channel":"CH1","secondary_channel":"CH2","item":"phase"}`. `DELAY`
uses the same two channels plus optional `source_edge` and `target_edge`; valid
edges are `FRISe`, `FFALL`, `LRISe`, and `LFALL`, and both default to `FRISe`.
The two channels must differ. Each PHASE or DELAY selection consumes one of the
same ten global slots. Idle reads query only the selected values without OPEN,
CLEAR, STOP, artifact capture, or persistence.

Dashboard MHO1 polling defaults to two seconds, rejects any smaller value, and
always uses scalar-only queries without STOP/RUN. OpenBench leaves at least
250 ms in RUN after an explicit waveform frame before
another may start. In the validated ten-frame series, all four channels, the screenshot,
and all ten scalars succeeded 10/10; frame duration was 0.834016-1.079901 s
(0.956884 s mean). One earlier series observed a transient 2.072785 s transport
stall, so the two-second setting is a scheduling lower bound rather than a hard
frame-completion deadline.

## FNIRSI DPS-150 power supply

```http
GET   /power-supplies/{device_id}
PATCH /power-supplies/{device_id}/output
PATCH /power-supplies/{device_id}/protections
PATCH /power-supplies/{device_id}/display
PATCH /power-supplies/{device_id}/metering
PUT   /power-supplies/{device_id}/presets/{1..6}
POST  /power-supplies/{device_id}/presets/{1..6}/apply
```

Output update example:

```json
{"voltage_v": 1.4, "current_a": 0.05, "enabled": false}
```

Voltage is 0 to the smaller of 30 V and live `upper_voltage_v`, in 0.01 V
steps. Current is 0 to the smaller of 5 A and `upper_current_a`, in 0.001 A
steps. Power may not exceed 150 W. Output OFF is written before setpoint
changes; Output ON is written last. All writes are read back. Output enable is
blocked unless safety is `safe`.

Protection and auxiliary examples:

```json
{"over_voltage_v": 30, "over_current_a": 5.1, "over_power_w": 150,
 "over_temperature_c": 80, "low_input_voltage_v": 5}
{"brightness": 8, "volume": 4}
{"enabled": true}
```

Brightness/volume are 0 through 10. Metering supports verified start/stop;
there is no verified hardware reset command. Preset save overwrites instrument
nonvolatile memory. Preset apply preserves output when `enabled` is omitted.

Application-side output programs:

```http
POST /power-supplies/{device_id}/programs/sequence
{"steps": [{"voltage_v": 0.5, "current_a": 0.01, "dwell_s": 0.5}],
 "loops": 1}

POST /power-supplies/{device_id}/programs/sweep
{"parameter": "voltage", "start": 0.5, "end": 1, "step": 0.1,
 "fixed_value": 0.01, "dwell_s": 1, "loops": 1}

GET  /power-supplies/{device_id}/programs/status
POST /power-supplies/{device_id}/programs/pause
POST /power-supplies/{device_id}/programs/resume
POST /power-supplies/{device_id}/programs/stop
{"output_off": true}
```

One program may run per supply. Sequence dwell starts at 0.1 seconds; sweep
dwell starts at 1 second. Natural completion and failures force Output OFF.
Stop defaults to Output OFF. Device disconnect verifies Output OFF before
closing the COM port.

## ITECH IT6000C bidirectional source/load

```http
GET   /bidirectional-power-supplies
GET   /bidirectional-power-supplies/{device_id}
GET   /bidirectional-power-supplies/{device_id}/measurements
POST  /bidirectional-power-supplies/{device_id}/experiment-reservation
DELETE /bidirectional-power-supplies/{device_id}/experiment-reservation
PATCH /bidirectional-power-supplies/{device_id}/operating-point
PATCH /bidirectional-power-supplies/{device_id}/protections
POST /bidirectional-power-supplies/{device_id}/protections/clear
PATCH /bidirectional-power-supplies/{device_id}/advanced
```

The complete response includes identity/firmware, actual V/A/W,
SOURCE/SINK/IDLE direction, CV/CC/CP regulation, all signed operating limits,
protections, slew/delay/watchdog settings, status registers, baud rate, and
operator safety warnings. The only accepted profile is the physically tested
`IT6054C-800-225` (800 V, +/-225 A, 54 kW).

CV priority uses `voltage_setpoint_v` plus signed positive/negative current
limits. CC priority uses signed `current_setpoint_a` plus positive/negative
voltage limits. Positive current sources power and negative current sinks it.
Both priorities use signed positive/negative power limits. Omitted fields
remain unchanged. Configuration and multi-field writes are verified by
complete read-back; the live single-setpoint exception is described below.

`GET .../measurements` is the fast experiment sample. It performs exactly the
two measured V/I queries, calculates `P = V * I`, returns V/I/P, and publishes
them to an active common CSV. Calculated power has quality
`calculated_u_times_i`. It reads no `MEAS:POW?`, settings, limits, modes, or
protections.

For an external stepped experiment, reserve the ITECH after read-only preflight
and final operator confirmation but before the first write. Reservation waits
for any in-flight Dashboard read, suspends ordinary ITECH polling, keeps the
compact `/measurements` path available, and makes common CSV start reuse cached
invariant settings. Keep it through verified Output OFF and restoration, then
DELETE it. The next ordinary poll waits one complete configured interval. The
external script never opens COM; OpenBench remains the sole transport owner.

The hardware couples priority and limit registers. OpenBench internally writes
the inactive priority first and the requested final priority second; callers
submit only the desired final state and must not sequence direct SCPI.

Output enable requires explicit operator intent, known wiring/load,
`"wiring_confirmed": true`, matrix safety `safe`, and enabled OVP/OCP/OPP.
With Output already active, a single `current_setpoint_a` change in CC priority
or a single `voltage_setpoint_v` change in CV priority is a command-only live
step without Output toggling or automatic read-back and still requires
`wiring_confirmed`. Read the compact measurements endpoint once after settling;
its two queries can run in parallel with an oscilloscope frame. Priority, limits, power
limits, and multi-field changes pause an active output. Protection and advanced
writes are accepted only while Output is OFF. Output OFF is always safe;
disconnect, shutdown, and emergency stop force it and return local front-panel
control.
`POST .../protections/clear` clears only the instrument's latched protection
state and is rejected while Output is ON; it does not change thresholds.
For the supported serial transport, select front-panel `SYSTEM I/O -> USB-VCP`
at matching `115200, 8-N-1`; `USB-TMC` is not the VCP/COM interface.
OpenBench exposes no arbitrary SCPI or List/Battery/Solar function.

## OWON SPM source-measure unit

```http
GET   /source-measure-units
GET   /source-measure-units/{device_id}
PATCH /source-measure-units/{device_id}/output
PATCH /source-measure-units/{device_id}/protections
PATCH /source-measure-units/{device_id}/multimeter
```

Output payload fields are `voltage_v`, `current_a`, and `enabled`; omitted
fields are preserved. SPM6103 validates 0–60 V (0.01 V), 0–10 A (0.001 A),
and 300 W. Protection fields are `over_voltage_v` (up to 62 V) and
`over_current_a` (up to 10 A). Active outputs are paused for setpoint changes,
all writes are read back, enabling requires safety `safe`, and disconnect,
shutdown, or emergency stop forces Output OFF and returns local control.

The multimeter payload may include `function`, `range_mode`, `range_value`,
`relative_enabled`, and `hold_enabled`. Functions are `dc_voltage`,
`ac_voltage`, `dc_current`, `ac_current`, `resistance`, `capacitance`, `diode`,
and `continuity`. Auto range is supported for voltage and resistance; documented
manual ranges are supported for voltage, current, and resistance. Capacitance
range is read-only through SCPI. Responses include the SI-normalized value,
status, live range, relative state, and Hold state. Unsupported combinations
are rejected before a write and all accepted settings are read back.
Front-panel List waveform and startup-auto-output settings are not remotely
exposed in the published SPM programming manual and are intentionally absent.

## Instrument settings

```http
GET /devices/{device_id}/settings
PATCH /devices/{device_id}/settings
Content-Type: application/json
```

All PATCH fields are optional; omitted fields remain unchanged:

```json
{
  "context": "Output stage current",
  "poll_interval_s": 0.5,
  "scope_screen": true,
  "scope_data": true,
  "scope_channels": ["CH1", "CH3"],
  "scope_wait_for_trigger": false
}
```

- `context`: up to 10,000 characters; `""` clears it.
- `poll_interval_s`: greater than zero and no more than 600 seconds. Respect the
  device-specific `minimum_poll_interval_s` returned by GET. MHO1 returns a
  two-second minimum for Dashboard-card polling.
- `scope_screen`: request and save a direct MHO1 screenshot in an explicit frame.
- `scope_data`: enable or disable raw ASCII waveform capture in an explicit frame.
- `scope_channels`: selected ASCII channels, any unique subset of CH1-CH4. At
  least one is required when `scope_data` is true. The selection is preserved
  while data capture is disabled.
- `scope_wait_for_trigger`: for common RUN, use SINGLE and poll trigger status
  every 50 ms until STOP, then save the frozen frame and re-arm. Trigger events
  and relative artifact paths are written into the common recording CSV.

Configured scalar measurements are always queried on every MHO1 poll regardless
of these artifact options. Idle values are transient and the Dashboard card
shows them from memory; common RUN recording is what writes them to CSV.
Artifact toggles live in Settings. A waveform CSV is created by normal
acquisition when Data is enabled and is never derived from screenshot pixels.

Accepted safe preferences persist in SQLite under the stable device ID and are
restored after restart or rediscovery. This includes context/polling, MHO1
artifact/channel/measurement selection, and LA2016 acquisition setup. Output
enable is never replayed, and restoring preferences alone never starts capture.

## Kingst LA2016 logic acquisition

```http
GET   /logic-analyzers
GET   /logic-analyzers/{device_id}/settings
PATCH /logic-analyzers/{device_id}/settings
GET   /logic-analyzers/{device_id}/captures/status
GET   /logic-analyzers/{device_id}/captures/{capture_id}
POST  /logic-analyzers/{device_id}/captures/start
POST  /logic-analyzers/{device_id}/captures/arm
POST  /logic-analyzers/{device_id}/captures/stop
GET   /logic-analyzers/{device_id}/captures/{capture_id}/files/{filename}
```

Settings PATCH accepts any subset:

```json
{
  "channels": [0, 1, 2, 3],
  "sample_rate_hz": 20000000,
  "sample_count": 2000000,
  "threshold_v": 1.4,
  "capture_ratio_percent": 50,
  "triggers": [{"channel": 0, "condition": "rising"}],
  "auto_start_enabled": false,
  "auto_start_delay_s": 0
}
```

GET settings returns the exact supported rates and thresholds. At least one of
CH0–CH15 must be enabled. Conditions are `low`, `high`, `rising`, and
`falling`; multiple level triggers but only one edge trigger are allowed.

`start` is immediate and triggerless. `arm` uses configured hardware triggers.
Both accept `title` and `comment`. Poll status through `starting`,
`pretrigger`, `armed`, `capturing`, and `downloading` until a terminal state.
`remaining_s` includes approximate setup and memory-download overhead for
bounded acquisition phases. It is absent only while waiting in `armed`, then
restarts after the hardware trigger. Download `capture.sr` and
`metadata.json` using the returned capture ID.

If global CSV recording is active, logic start/arm, trigger, completion, stop,
and error are timestamped in its fixed analyzer column block. Enabling
`auto_start_enabled` schedules start/arm after `auto_start_delay_s` from global
RUN. Complex conditions across other instrument values belong in the calling
Codex workflow or script, not an internal OpenBench rule.

## Snapshot

```http
POST /captures/snapshot
Content-Type: application/json

{
  "title": "Power stage idle",
  "comment": "No load, 24 V supply"
}
```

The response contains `file_name`, `download_url`, and `measurement_count`.
Title is limited to 120 characters; comment to 10,000.

## CSV recording

Start until explicitly stopped:

```http
POST /captures/recording/start
Content-Type: application/json

{
  "title": "Thermal drift",
  "comment": "Ten-minute warm-up"
}
```

Start with automatic stop:

```json
{
  "title": "Load step",
  "comment": "2 A to 8 A",
  "duration_s": 30,
  "scope_capture_mode": "periodic"
}
```

Duration is optional and must be from 1 to 86,400 seconds.

```http
GET /captures/status
POST /captures/recording/stop
GET /captures/files/{filename}
```

Status includes `active`, `started_at`, `current_file`,
`last_recording_file`, `last_snapshot_file`, `samples_written`, `title`,
`comment`, `duration_s`, `elapsed_s`, and `remaining_s`.

Only one CSV recording can be active. Starting another or stopping when none is
active returns HTTP `409`.
Invariant driver settings are written once in the instrument group's
`initial_settings` header instead of receiving repeating stream columns.
ITECH streams only measured voltage/current/power and commanded voltage/current;
its signed limits and initial Output/priority/direction are written once.
Snapshots are unchanged because their complete state already occupies one row.
During recording, each MHO1 owns a periodic frame loop and the transient idle
poll is suspended. With trigger waiting enabled, that loop becomes
SINGLE -> status WAIT -> status STOP -> save -> re-arm. All exit paths restore
RUN.

For an externally orchestrated stepped experiment, start with
`"scope_capture_mode": "manual"` and request each exact frame with:

```http
POST /captures/recording/scopes/{scope_device_id}/frame
{"label": "sink_set_4A"}
```

The request uses the stored Screen/Data/channel/trigger options and always
reads the configured scalar Measurements. It saves one frame inside the active
recording directory and writes its sequence, status, label, and relative
manifest path into the recording CSV. Manual mode creates no periodic frames;
recording STOP restores scope RUN and ordinary polling. Non-triggered periodic
frames also write their sequence and manifest path into the CSV.
