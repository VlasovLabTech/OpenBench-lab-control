# OpenBench automation API

Base URL: `http://127.0.0.1:8000/api/v1`

The live OpenAPI schema is available at `/openapi.json`, with interactive
documentation at `/docs`. These endpoints are the preferred automation path for
Codex and scripts; browser clicking is not required.

## Check and discover

```http
GET /health
POST /devices/discover/ut197
POST /devices/discover/ut61d
POST /devices/discover/ut61e
POST /devices/discover/ut61eplus
POST /devices/discover/micsig
POST /devices/discover/micsig_eto
POST /devices/discover/feeltech
POST /devices/discover/dps150
POST /devices/discover/owon_spm
POST /devices/discover/kingst
POST /devices/discover/itech_it6000c
POST /devices/discover/all
GET /devices
GET /channels
GET /channels/{channel_id}/latest
```

`all` scans physical instrument drivers only. Connect the simulated meter
explicitly with `POST /devices/discover/simulated` when it is wanted.

`micsig_eto` validates only `Micsig,ETO5004,...` identities. It is enabled but
not automatically scanned at startup; use the explicit endpoint or set
`OPENBENCH_MICSIG_ETO_AUTO_DISCOVER=true`. Configure fixed hosts with
`OPENBENCH_MICSIG_ETO_HOSTS`. Device IDs use the returned serial number rather
than the transient IP address, so multiple units remain distinct.

`ut61d` and `ut61e` use the shared one-way original-series stream decoder. The
stream contains no model identifier, so call the endpoint matching the physical
meter. `ut61plus` is not an alias; UT61E+ remains the separate `ut61eplus`
driver.

`feeltech` scans CH340 USB serial interfaces and registers every candidate that
answers the read-only model query as a FeelElec FY-series generator. Each
generator exposes waveform, frequency, amplitude, offset, duty cycle, phase,
and output state channels for CH1 and CH2. Six additional live channels expose
frequency, count, period, positive/negative pulse width, and duty cycle from the
separate external counter input. All twenty channels participate in snapshot
and CSV capture. Use the signal-generator endpoints below for verified control.

`dps150` scans the FNIRSI AT32 USB virtual COM identity (`2E3C:5740`),
validates model/HW/FW through the binary protocol, and registers fifteen live
state, setpoint, and protection channels. The tested device reports `DPS-150`,
HW `V1.0`, FW `V1.2`, with a 0.5-second minimum polling interval.

`owon_spm` probes CH340 serial interfaces with the read-only `*IDN?` query and
accepts only the live-tested OWON SPM6103 reply. SPM and FeelElec discovery are serialized
so their shared `1A86:7523` bridge identity cannot cause a port race. Stable
device IDs use the instrument serial number, so multiple units are independent.
The live-tested SPM6103 reports serial `SPM-DEMO-0001`, FW `FV:V2.1.0`, and exposes
nine capture channels covering source setpoints/state/live V/A/W/protection plus
the built-in multimeter display. Minimum polling is 0.5 seconds.

`kingst` uses `sigrok-cli`, accepts only devices identified by the
`kingst-la2016` driver, and registers each analyzer under its stable physical
USB port path rather than its transient bus address. The tested hardware is
LA2016 (`77A1:01A2`), 16 inputs, up to 200 MHz. PWM outputs exposed by sigrok
are intentionally excluded: OpenBench uses this integration only for bounded
read-only logic acquisition. On Windows the validated local runtime keeps the
native Kingst WinUSB driver; KingstVIS must be closed during an OpenBench
capture.

`itech_it6000c` probes only the ITECH USB VCP identity `2EC7:A4A7`, tries
115200 first and 9600 second, and accepts only the live-tested
`IT6054C-800-225` identity. Its stable device ID is based on the instrument
serial number. Automatic startup discovery is disabled by default; use the
explicit endpoint, Find All, or `OPENBENCH_ITECH_IT6000C_AUTO_DISCOVER=true`.
Rediscovery replaces a disconnected device's stale COM transport by that stable
serial identity. On Windows, an access-denied serial open also makes one bounded
attempt to stop the known NI-VISA COM claimants and retry the same port. Probe
failure cleanup only closes the transport; discovery never changes Output.

## FeelElec FY-series generator

Read all channel, synchronization, burst/modulation, counter, and locally known
sweep state:

```http
GET /generators/{device_id}
```

The response includes `channels`, the model-specific `waveforms` list,
`synchronization`, `safety_state`, and `advanced`. Basic channel settings and
outputs always use hardware read-back. On FY6200 firmware that does not support
an advanced read command, its value is `null` and its command appears in
`advanced.unavailable_reads`.

Update any subset of one channel:

```http
PATCH /generators/{device_id}/channels/{1|2}
Content-Type: application/json

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

Omitted fields remain unchanged. When signal parameters of an enabled channel
change, OpenBench temporarily turns that channel off, writes and reads back each
value, then restores the requested output state. Model, waveform, frequency,
amplitude, offset, duty, and phase limits are validated before any write.
`pulse_width_ns` is CH1-only, from 100 ns to 1 s in 10 ns steps, and must be
shorter than the configured waveform period.

Set both outputs atomically from the API user's perspective:

```http
PUT /generators/{device_id}/outputs

{"channel_1": false, "channel_2": false}
```

Other supported controls:

```http
PATCH /generators/{device_id}/synchronization
{"parameter": "frequency", "enabled": true}

PATCH /generators/{device_id}/burst
{"source": "off", "cycles": 10}

POST /generators/{device_id}/burst/trigger
{"cycles": 10}

PATCH /generators/{device_id}/keying
{"kind": "fsk", "source": "manual", "secondary_frequency_hz": 1000}

PATCH /generators/{device_id}/counter
{"mode": "frequency", "gate_time_s": 1, "coupling": "dc"}

POST /generators/{device_id}/counter/pause

POST /generators/{device_id}/counter/reset

PATCH /generators/{device_id}/sweep
{
  "target": "frequency",
  "start": 100,
  "end": 10000,
  "duration_s": 1,
  "mode": "linear",
  "source": "time",
  "enabled": false
}

POST /generators/{device_id}/presets/{1..20}/save
POST /generators/{device_id}/presets/{1..20}/load
```

Burst sources are `off`, `ch2`, or `external`. Keying supports `ask`, `fsk`,
and `psk`, with source `off`, `external`, or `manual`. Sweep is a documented
write-only protocol block on FY6200; OpenBench returns the last commanded block
with `verified: false`. Use `source: "time"` for a timed forward or reverse
sweep and `source: "vco"` for external 0–5 V control through COUNTER IN.
Round-trip sweep and AM/FM/PM modulation depth/source are not exposed by the
published serial protocol and therefore remain front-panel-only functions.
Loading a preset can enable stored outputs. Saving a preset overwrites
instrument nonvolatile memory.

On the tested FY6200 firmware, ASK/FSK/PSK source read commands are unavailable,
so these source changes are returned as unavailable rather than falsely marked
verified. The FSK secondary frequency itself does have a working `RFK`
read-back and is verified.

The external counter is a separate input, not an internal readback of CH1/CH2.
It starts paused. Select `frequency` to read frequency and timing, `count` to
read only the pulse count, or `both` to read both groups. OpenBench does not
query the disabled group; `both` can alternate the physical counter display.
The selected channels are available from the regular channel/latest endpoints
and captures.

Any operation that can enable an output is rejected while matrix safety is not
`safe`. The global `POST /emergency-stop` opens the matrix, latches safety, and
attempts to turn outputs off on every registered generator, power supply, and
OWON SPM source.

## FNIRSI DPS-150 power supply

Read identity, live measurements, setpoints, protections, six presets,
brightness/volume, metering state, and hardware-available voltage/current:

```http
GET /power-supplies/{device_id}
```

Update only the specified output fields; omitted values remain unchanged:

```http
PATCH /power-supplies/{device_id}/output

{"voltage_v": 1.4, "current_a": 0.05, "enabled": false}
```

Voltage uses 0.01 V steps and may not exceed the smaller of 30 V and the live
`upper_voltage_v`. Current uses 0.001 A steps and may not exceed the smaller of
5 A and `upper_current_a`. The 150 W product limit is enforced. OpenBench
turns an active output off before setpoint writes when `enabled: false` is
requested, writes an ON transition last when enabling, orders live setpoint
changes to avoid a transient less-restrictive limit, and verifies the complete
state afterward. Enabling is blocked unless matrix safety is `safe`.

Other verified controls:

```http
PATCH /power-supplies/{device_id}/protections
{"over_voltage_v": 30, "over_current_a": 5.1, "over_power_w": 150,
 "over_temperature_c": 80, "low_input_voltage_v": 5}

PATCH /power-supplies/{device_id}/display
{"brightness": 8, "volume": 4}

PATCH /power-supplies/{device_id}/metering
{"enabled": true}

PUT  /power-supplies/{device_id}/presets/{1..6}
{"voltage_v": 5, "current_a": 1}

POST /power-supplies/{device_id}/presets/{1..6}/apply
{"enabled": false}
```

Brightness and volume are integers from 0 through 10. OCP accepts the
instrument's reported 5.1 A protection ceiling, although the normal current
setpoint remains limited to 5 A. The published protocol provides reliable
Ah/Wh metering start/stop but no verified reset command, so OpenBench does not
invent one. Preset save changes instrument nonvolatile memory; preset apply
preserves output state when `enabled` is omitted.

Start an explicit step sequence:

```http
POST /power-supplies/{device_id}/programs/sequence

{
  "steps": [
    {"voltage_v": 0.5, "current_a": 0.01, "dwell_s": 0.5},
    {"voltage_v": 1.0, "current_a": 0.01, "dwell_s": 0.5}
  ],
  "loops": 1
}
```

Start an application-side voltage or current sweep:

```http
POST /power-supplies/{device_id}/programs/sweep

{"parameter": "voltage", "start": 0.5, "end": 1.0, "step": 0.1,
 "fixed_value": 0.01, "dwell_s": 1, "loops": 1}
```

For a voltage sweep, `fixed_value` is current; for a current sweep it is
voltage. Sequence dwell is 0.1 to 86,400 seconds; sweep dwell is 1 to 86,400
seconds. One program may run per supply. Programs support:

```http
GET  /power-supplies/{device_id}/programs/status
POST /power-supplies/{device_id}/programs/pause
POST /power-supplies/{device_id}/programs/resume
POST /power-supplies/{device_id}/programs/stop
{"output_off": true}
```

Natural completion and failures force Output OFF. Stop defaults to Output OFF;
requesting `output_off: false` leaves the last state and therefore requires the
same explicit operator intent as any other energized output.
Disconnecting a DPS-150 from OpenBench also requires a verified Output OFF
before its COM port and registry entry are released.

## ITECH IT6000C bidirectional DC source/load

Read every registered unit or the complete state of one unit:

```http
GET /bidirectional-power-supplies
GET /bidirectional-power-supplies/{device_id}
GET /bidirectional-power-supplies/{device_id}/measurements
```

The response includes identity/firmware, actual voltage/current/power,
SOURCE/SINK/IDLE direction, active CV/CC/CP regulation, fixed-function
priority, signed source/sink setpoints and limits, protection thresholds,
slew rates, delays, status registers, the COM port/baud rate, and visual safety
warnings. The live-tested profile is `IT6054C-800-225`: 800 V, +/-225 A, and
54 kW nameplate ratings. OpenBench deliberately accepts no other IT6000C model
until that model has its own validated profile.

The `/measurements` resource is the compact experiment path: it sends exactly
`MEAS:VOLT?` and `MEAS:CURR?`, calculates power as `V * I`, returns V/I/P, and
publishes those three samples into an active common CSV recording. Calculated
power has quality `calculated_u_times_i`. This path does not read settings,
limits, protections, modes, status registers, or `MEAS:POW?`.

An external ITECH experiment must reserve the instrument after read-only
preflight and final operator confirmation, but before its first write:

```http
POST   /bidirectional-power-supplies/{device_id}/experiment-reservation
DELETE /bidirectional-power-supplies/{device_id}/experiment-reservation
```

Reservation waits for an in-flight Dashboard sample to finish and then
suspends every ordinary ITECH polling target. The compact `/measurements`
resource remains the explicit per-point sampling path. Starting common CSV
recording reuses cached invariant ITECH state instead of performing another
complete read. Release only after verified Output OFF and settings restoration;
ordinary polling then waits one complete configured interval before its next
read. A duplicate reservation is rejected. The script remains an API client;
only the OpenBench server opens the COM port.

For the validated six-point ITECH + MHO1 sink experiment on Windows, double-click
`Run ITECH MHO1 Experiment.cmd`. The launcher requires an explicit `RUN` wiring
confirmation. Each point writes the signed current setpoint, waits one second,
then starts compact ITECH U/I sampling and one complete MHO1 Screen/Data/
Measurements frame in parallel. The next setpoint is written immediately after
both operations finish. The launcher forces ITECH Output OFF on exit and opens
the new capture folder after a successful run. Its default scope profile is
CH1 Maximum/High, CH2 Maximum/High, CH3 Frequency/Amplitude, and CH4
Frequency/Amplitude. The confirmation dialog offers editable Title and Comment
defaults before any output is enabled. The launcher performs a read-only device
and plan preflight first, including up to four bounded ITECH discovery attempts;
only a successful preflight displays the final wiring confirmation.

On the tested MHO14-200 firmware `2.154.75`, `:TIMebase:MODE?` can report `XY`
while the front panel visibly remains in ordinary YT mode. The launcher never
writes the mode in response. A dry run reports the mismatch; execution requires
the operator's visual YT confirmation through the final exact-confirmation flow.
The launcher performs discovery, full read-only preflight, plan display, exact
confirmation, and execution in one Python process. It does not repeat discovery
or full state reads between the accepted phrase and the first controlled write.
After exact confirmation and before the first controlled write, it acquires the
ITECH experiment reservation described above. This leaves the saved polling
preference unchanged, prevents Dashboard/global-sampling reads from entering
the serial path, and avoids the extra complete state read that common CSV start
normally uses for its invariant header. Each point remains one explicit compact
U/I read-back. Cleanup keeps the reservation through verified Output OFF and
settings restoration, then releases it.

Every stepped ITECH launcher bounds both the live current-step command and the
compact voltage/current read-back to three seconds, treating the read-back as a
safety heartbeat. A request error or missing U/I
field aborts the remaining points and immediately sends Output OFF, then reads
the complete device state to prove `output_enabled: false`. If the OFF command
or verification loses the transport, the script performs up to four bounded
attempts, rediscovers only `itech_it6000c`, requires the same stable serial-ID
device to reconnect, and retries OFF. The identical verified shutdown runs
after the final point and before CSV stop, or on an error path before cleanup.
There is no extra state read during a successful measurement point. Exhaustion
prints an explicit front-panel/mains-disconnect warning and fails the experiment.

Update any nonempty subset of the fixed CV/CC operating point:

```http
PATCH /bidirectional-power-supplies/{device_id}/operating-point
{
  "priority": "CV",
  "voltage_setpoint_v": 1.0,
  "current_limit_positive_a": 0.1,
  "current_limit_negative_a": -0.1,
  "power_limit_positive_w": 10,
  "power_limit_negative_w": -10,
  "output_enabled": false
}
```

CV priority uses `voltage_setpoint_v` with positive/negative current limits.
CC priority uses the signed `current_setpoint_a` with positive/negative voltage
limits. Positive current sources power; negative current sinks it. Positive
and negative power limits apply in both directions. All writes are bounded by
the exact model profile. While an output is already active, a single
`current_setpoint_a` change in CC priority or a single `voltage_setpoint_v`
change in CV priority is a command-only live step: it neither toggles Output
nor performs an automatic read-back. The returned complete-state envelope uses
the cached invariant settings. It still requires `"wiring_confirmed": true`;
read `/measurements` once after the experiment's settle interval when actual
V/I and calculated power are needed. Priority,
limits, power limits, or multi-field changes pause an active output and restore
it only after successful writes. Enabling Output additionally requires matrix
safety `safe` and enabled OVP, OCP, and OPP. Output OFF never requires
confirmation.

The hardware makes `VOLT:LIM` writable in CC priority and `CURR:LIM` writable
in CV priority, and priority changes copy active values into related limit
registers. OpenBench handles this internally by rebuilding the inactive side
first and the requested final priority second. API clients should submit the
desired final state and must not reproduce this SCPI sequencing themselves.

Protection and advanced settings can be changed only while Output is OFF:

```http
PATCH /bidirectional-power-supplies/{device_id}/protections
{"ovp_enabled": true, "ovp_level_v": 12, "ocp_enabled": true,
 "ocp_level_a": 1, "opp_enabled": true, "opp_level_w": 10,
 "uvp_enabled": false, "ucp_enabled": false}

POST /bidirectional-power-supplies/{device_id}/protections/clear

PATCH /bidirectional-power-supplies/{device_id}/advanced
{"voltage_slew_positive_v_per_ms": 0.1,
 "voltage_slew_negative_v_per_ms": 0.1,
 "current_slew_positive_a_per_ms": 0.1,
 "current_slew_negative_a_per_ms": 0.1,
 "output_rise_delay_s": 0, "output_fall_delay_s": 0,
 "watchdog_enabled": false, "watchdog_delay_s": 30}
```

The clear endpoint is rejected while Output is ON. It clears only a latched
hardware protection condition and does not modify any threshold. The supported
serial connection requires front-panel `SYSTEM I/O -> USB-VCP` with matching
`115200, 8-N-1`; selecting `USB-TMC` exposes a different USB interface rather
than the virtual COM port used by this driver.

The API intentionally exposes no arbitrary SCPI, List/Battery/Solar function,
calibration, system, or bus-configuration command. Disconnect, server shutdown,
and global emergency stop force Output OFF and return the panel to local mode.
The instrument's fourteen normalized channels participate in ordinary snapshot
and CSV recording, including actual V/A/W, setpoints, signed limits, output,
priority, and direction.

## OWON SPM source-measure unit

Read every registered unit or one complete source + multimeter state:

```http
GET /source-measure-units
GET /source-measure-units/{device_id}
```

Verified source control accepts any nonempty subset:

```http
PATCH /source-measure-units/{device_id}/output
{"voltage_v": 1.0, "current_a": 0.1, "enabled": false}

PATCH /source-measure-units/{device_id}/protections
{"over_voltage_v": 12.0, "over_current_a": 1.0}
```

SPM6103 source limits are 0–60 V in 0.01 V steps, 0–10 A in 0.001 A
steps, and 300 W. OVP is limited to 62 V and OCP to 10 A. An active output
is turned OFF before setpoint changes and restored only after the new values
are written; every write is read back. Output enable requires matrix safety
`safe`. Disconnect, server shutdown, and global emergency stop force Output OFF
and return the front panel to local mode.

Select the built-in multimeter function:

```http
PATCH /source-measure-units/{device_id}/multimeter
{"function": "dc_voltage"}
```

Functions are `dc_voltage`, `ac_voltage`, `dc_current`, `ac_current`,
`resistance`, `capacitance`, `diode`, and `continuity`. The same PATCH can set
`range_mode` (`auto` or `manual`), a documented `range_value`,
`relative_enabled`, and global `hold_enabled`. Automatic range control is
available for AC/DC voltage and resistance; fixed ranges are available for
AC/DC voltage, AC/DC current, and resistance. Capacitance range is reported but
left front-panel controlled because the programming manual documents only its
query. Diode and continuity have fixed hardware ranges. Every setting is read
back; unsupported function/range combinations are rejected before a write.

The SPM6103 front-panel List waveform editor and startup-auto-output setting
are intentionally absent from the API. OWON's published SPM programming manual
does not expose commands for them, so OpenBench does not guess output-control
commands that cannot be verified.

## Instrument context and polling

```http
GET /devices/{device_id}/settings
Content-Type: application/json
```

```http
PATCH /devices/{device_id}/settings
Content-Type: application/json

{
  "context": "Output stage current",
  "poll_interval_s": 0.5
}
```

Respect `minimum_poll_interval_s` returned by GET. MHO1 returns a two-second
minimum for Dashboard-card polling.

Accepted settings are persisted in SQLite under the stable `device_id` and are
restored after an application restart or rediscovery. This applies to instrument
context and polling, the MHO1 artifact/channel/measurement profile, and LA2016
acquisition setup. OpenBench never restores an energized output, and loading
preferences by itself never starts an acquisition.

For MHO1, the same endpoint controls artifacts taken from one atomic frame:

```json
{
  "scope_screen": true,
  "scope_data": true,
  "scope_channels": ["CH1", "CH3"],
  "scope_wait_for_trigger": false
}
```

Every idle Dashboard poll reads only the configured scalar measurements. It
does not send STOP/RUN, request Screenshot/ASCII artifacts, or write readings to
SQLite or disk; values exist only in memory and on the live event bus. The
common `RUN` workflow is the persistence boundary: Once writes a snapshot and
an active recording normally captures periodic scope frames and writes their
scalar events to its CSV. An external experiment may instead request exact
manual frames aligned with commanded steps. `scope_screen`
independently enables the directly transferred image for an explicit frame,
`scope_data` enables raw ASCII waveform capture, and `scope_channels` selects
any non-empty subset of CH1-CH4. The selected channels are preserved while data
capture is disabled. Waveform data is never derived from screenshot pixels.

With `scope_wait_for_trigger` enabled, common RUN sends `SINGLE` and polls the
short `TRIGger:STATus?` query every 50 ms. `WAIT`, `RUN`, and `AUTO` continue the
wait; `STOP` identifies the completed frozen acquisition. OpenBench then reads
the selected screenshot, raw ASCII channels, and scalar measurements without an
extra STOP/RUN pair, saves the frame, timestamps a `trigger` row in the common
CSV during recording, and immediately arms the next SINGLE. Cancellation,
failure, recording STOP, and application shutdown restore continuous RUN.
On MHO14-200 firmware `2.154.75`, raw TCP `:SYS:SCR?` returns a complete JPEG
but corrupts the two-byte JFIF marker. OpenBench repairs only the narrowly
matched `FF D8 ?? ?? 00 10 JFIF` header. An empty or invalid direct reply permits
one direct retry after at least one second; normal frame capture never falls
back to a stored oscilloscope file. VXI-11 returned zero bytes on the tested
unit.
The scope HTTP server accepted
`DELETE` requests but retained screenshot, CSV, and BIN files in both RUN and
STOP states; the published SCPI guide has no file-delete command. Until a
verified deletion mechanism exists, these files must be removed in the scope's
front-panel file manager. `/storage-index` can list `/pictures/Screenshots` for
inspection but does not delete files.

Omit fields that should not change. Send `context: ""` to clear the context.

## Kingst LA2016 logic acquisition

The logic-analyzer surface is deliberately atomic so Codex or an external
script can orchestrate a complex experiment without embedding a rule engine in
OpenBench:

```http
GET   /logic-analyzers
GET   /logic-analyzers/{device_id}/settings
PATCH /logic-analyzers/{device_id}/settings
GET   /logic-analyzers/{device_id}/captures/status
GET   /logic-analyzers/{device_id}/captures/{capture_id}
POST  /logic-analyzers/{device_id}/captures/start
POST  /logic-analyzers/{device_id}/captures/arm
POST  /logic-analyzers/{device_id}/captures/stop
GET   /logic-analyzers/{device_id}/captures/{capture_id}/files/capture.sr
GET   /logic-analyzers/{device_id}/captures/{capture_id}/files/metadata.json
```

Configure any subset; omitted fields remain unchanged:

```json
{
  "channels": [0, 1, 2, 3],
  "sample_rate_hz": 20000000,
  "sample_count": 2000000,
  "threshold_v": 1.4,
  "capture_ratio_percent": 50,
  "triggers": [
    {"channel": 0, "condition": "high"},
    {"channel": 1, "condition": "rising"}
  ],
  "auto_start_enabled": true,
  "auto_start_delay_s": 2.5
}
```

Supported sample rates are returned by GET settings and range from 20 kHz to
200 MHz. Supported thresholds are 0.4, 0.6, 0.9, 1.2, 1.4, 2.0, 2.5, and
4.0 V. Capture size is 1 through 10,000,000,000 samples. At least one channel
must be enabled. Trigger conditions are `low`, `high`, `rising`, and `falling`;
the hardware accepts multiple level conditions but at most one edge condition.
Every triggered channel must also be enabled.

`start` ignores configured triggers and begins an immediate triggerless
capture. `arm` requires at least one configured trigger and waits for hardware.
Both accept `title` and `comment`. Status progresses through `starting`,
`pretrigger`, `armed`, `capturing`, and `downloading`, then `completed`;
`remaining_s` is available when time can be estimated. For a bounded capture,
the estimate includes sample acquisition plus approximate setup and memory
download time; the Dashboard keeps a determinate progress bar through the
`downloading` phase. Hardware-trigger wait time is inherently unknown, so the
timer pauses in `armed` and restarts with a bounded estimate after the trigger.
On a hardware trigger,
`triggered_at` is the host timestamp derived from the sigrok state transition
and `trigger_timestamp_quality` is `driver_log`.

Each capture creates a separate directory containing the native `capture.sr`
session and UTF-8 `metadata.json`. When a global CSV recording is active,
manual start/arm, trigger, completion, stop, and error are also written into
that CSV under the analyzer's fixed column block, including timestamp, capture
ID, state, and relative artifact path.

When `auto_start_enabled` is true, starting the global CSV recording schedules
the analyzer after `auto_start_delay_s`. If triggers are configured the
scheduled operation arms them; otherwise it starts immediately. Stopping the
CSV before the delay expires cancels the schedule. A logic capture that has
already begun remains an independent bounded capture.

Do not add internal cross-instrument conditions such as “start when a meter
exceeds a threshold.” Implement those experiments in Codex or a small script:
read the ordinary measurement endpoints, evaluate the required sequence, and
call the atomic logic start/arm/stop endpoints at the appropriate time.

## One snapshot

```http
POST /captures/snapshot
Content-Type: application/json

{
  "title": "Power stage idle",
  "comment": "No load, 24 V supply"
}
```

The response contains `file_name`, `download_url`, and `measurement_count`.

For every MHO1 in that snapshot, OpenBench creates a sibling folder named after
the snapshot CSV. It always contains `<scope>_measurements.csv` and
`<scope>_capture.json`; the JSON records the UTC timestamp, frame duration,
selected channels, common nine-field preamble, and per-channel timing. Enabled
channels add unmodified `<scope>_chN_ascii.txt` payloads, and Screenshot adds
`<scope>_screen.<format>`. When Data is enabled, the same folder also contains
`<scope>_waveforms.csv` with sample index, calibrated time, and one voltage
column per selected channel. The manifest exposes it as `files.waveforms_csv`.

## Micsig oscilloscope control

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

Scope settings accept optional acquisition type/count, memory depth,
timebase/mode/position, per-channel display/scale/position/coupling/probe/input
impedance, and bounded edge-trigger fields. Writes are read back. On tested
firmware `2.154.75`, production waveform capture uses one atomic NORMAL-mode
ASCII frame:

```text
STOP
selected CH1..CH4: SOURCE -> MODE NORMAL -> ASCII
one common PREAMBLE
optional direct screenshot
configured scalar queries
RUN (always, in finally)
```

The frame contains no acquisition state/read-back query and no fixed post-STOP
delay. When no waveform channel is enabled, OpenBench sends no STOP/RUN and
requests only the optional screenshot plus configured scalars. Every tested
channel returned 1,375 points. A normal screenshot can make one paced direct
retry but never stores or retrieves a scope-side screenshot.
The undocumented fast BIN query and stored BIN paths remain diagnostic only and
are not used for production frames.

ETO5004 uses the same bounded state, settings, Run/Stop/Single, scalar, frame,
and numeric-CSV endpoints. On live-tested firmware `3.392.132`, its production
frame transaction selects `MODE NORMAL`, `FORMAT WORD`, `START 1`, and
`STOP 1100`, then reads standard `DATA?`. This firmware encodes each WORD point
as four ASCII hexadecimal characters and reports double the correct vertical
increment/origin; the model-bounded driver parses the 1,100 codes and applies
the physically cross-checked 0.5 vertical correction. A single selected channel
is live-validated. A second selected channel in the same stopped frame still
returns an empty block even after a fresh TCP session, bounded retry, and source
settle; do not claim atomic multi-channel ETO Data on firmware `3.392.132`.
The 12-bit-only fast binary query returns a SCPI command error and is not used.

Direct ETO5004 screenshots returned no payload through both raw TCP and VXI-11
on firmware `3.392.132`. Production Screen capture therefore uses the documented
`:STORage:CAPTure:STARt` command, discovers the new PNG/JPEG under
`/pictures/Screenshots`, and downloads it through the scope's HTTP service.
The live test produced a valid 1920x1200 PNG in about 1.6 seconds, so ETO now
advertises `screenshot_capture` and enables its saved-frame Screen option. Each
capture leaves the scope-side image in storage because no safe delete command is
used. The bounded `screenshot-probe` endpoint remains a direct-transport
diagnostic and does not exercise this stored-file fallback.

MHO1/ETO5004 full-memory ASCII export is a separate asynchronous, one-shot action;
it is never part of Dashboard polling, common Snapshot, or common CSV recording:

```http
POST /oscilloscopes/{micsig_mho1_or_eto_device_id}/maximum-capture
Content-Type: application/json

{"channels":["CH1","CH2"]}
```

The request succeeds only if either supported Micsig oscilloscope already reports `STOP`. OpenBench
does not stop it, re-arm it, return it to RUN, or write `ACQuire:DEPSelect`.
Instead it reads the current `ACQuire:DEPTh?`, selects waveform `MODE MAXIMUM`
and `FORMAT ASCII`, and streams exactly that many points per selected channel in
blocks of at most 15,625 points. The previous waveform reader source, mode,
format, start, and stop are restored afterward; acquisition remains stopped.

The `202` response contains `capture_id`, `state`, `memory_depth_points`,
`points_total`, `points_completed`, and `progress_percent`. Poll the matching
`GET /maximum-capture` endpoint until `state` is `completed` or `error`; there
is intentionally no ordinary stop endpoint. While state is `starting`,
`capturing`, or `finalizing`, the scope is exclusively owned by this job: live
scope polling is suspended, mutating device/scope API and Dashboard actions are
rejected, Disconnect is rejected, and common Snapshot/CSV capture excludes the
device. Starting the job is also rejected while common CSV recording is active.

Completion exposes one comma-separated raw ASCII `.txt` file per channel and a
`capture.json` manifest through the returned file URLs. Files are written
incrementally and partial files are preserved on an error or application
shutdown. OpenBench checks conservative free-disk capacity before starting and
never holds the complete waveform in process memory.

ETO5004 firmware `3.392.132` has live STOP-gating coverage, while its full
36-Mpoint transfer was intentionally not started. The MHO1 implementation uses
the same documented standard `DATA?` path and fresh SCPI sessions between
commands to match the known behavior of firmware `2.154.75`; its full-memory
path currently has mocked contract coverage only and awaits a physical bench
test. Do not treat the MHO1 path as physically validated until the dated test
plan has been completed.

`POST /screenshot-probe` accepts `{"transport":"tcp"}` or
`{"transport":"vxi11"}` and performs one bounded direct `:SYS:SCR?` query.
It returns block length, signature, detected image format, elapsed time, and any
error. This diagnostic never falls back to a stored capture and therefore does
not create a file on the oscilloscope. Image detection includes the narrowly
matched malformed-JFIF repair used by normal screenshot capture.

`POST /fast-binary-probe` accepts `{"channel":"CH1"}` and runs exactly one
undocumented `:WAVeform:DATA:BIN?` query without ASCII or stored-file fallback.
It restores the selected waveform source and returns payload size, inferred
32-bit point count, prefix, elapsed time, any error, and a local `.bin` artifact
when data is returned. Use it only to diagnose
firmware support; the documented binary waveform command is `:WAVeform:DATA?`
after selecting `WORD` format.

`GET /measurements` returns the current Dashboard/front-panel profile without a
hardware write. `PUT /measurements` replaces the complete profile. Its body is
`{"measurements":[{"channel":"CH1","item":"amplitude"}]}`; an empty list
clears all pills. Apply sends CLEAR, waits 100 ms, sends every OPEN with a
100 ms pause, then waits a final 100 ms. This is a one-time settings operation;
frames never clear or reopen measurements. `POST /measurements/read` accepts the
same body and reads an already configured list. Responses include `elapsed_s`.

`PHASE` and `DELAY` are two-channel selections. For phase use
`{"channel":"CH1","secondary_channel":"CH2","item":"phase"}`. Delay also
accepts `source_edge` and `target_edge`, each one of `FRISe`, `FFALL`, `LRISe`,
or `LFALL`; omitted edges default to `FRISe`. Primary and secondary channels
must differ. Each pair selection consumes one of the same ten global slots.
The Dashboard shows the pair as `CH1→CH2`; phase is rendered in degrees and delay
uses the normal time-unit scaling. Measurement CSVs retain the second channel
and both edge fields.

Firmware `2.154.75` has ten global measurement slots across CH1-CH4. A live
20-item profile returned the first ten and marked the remaining ten unavailable,
so the API and Dashboard enforce ten. The fast read path uses no artificial
inter-query delay. Dashboard scope polling defaults to two seconds and values
below two seconds are rejected by the API and UI. It always uses scalar queries
without STOP/RUN, regardless of saved-frame options. Explicit waveform frames
additionally leave at least 250 ms in RUN before another frame starts. The
validated ten-frame series returned all four channels, a direct screenshot, and
all ten measurements 10/10 in 0.834016-1.079901 s (0.956884 s mean). One prior
series contained a transient 2.072785 s CH2 transport stall, so two seconds is a
scheduling lower bound, not a hard per-frame deadline.

`POST /numeric-waveforms/csv` remains a compatibility endpoint for an explicit
standalone conversion capture; it is not used by the Dashboard or the normal
snapshot pipeline. Normal acquisition preserves raw ASCII for later
post-processing. `POST /storage-waveforms/import` accepts `scope_paths` already listed by
`/storage-index`, copies CSV/BIN/WAV files from the scope HTTP store into the
local scope-waveforms directory, and does not delete or modify the originals.
This was live-tested with two operator-saved 11,256-byte BIN files. On firmware
`2.154.75`, quoted `STORage:SAVE:FILename` values are preserved literally and
rejected. OpenBench instead sends a safe unquoted numeric filename, knows the
exact `/files/binwave/<name>.bin` URL, and never lists a storage directory.
Each BIN is one channel: a 256-byte header plus 5,500 signed 16-bit samples,
11,256 bytes total. OpenBench retries that exact URL until the complete size
declared by the header is available; the scope first exposes a header-only
256-byte file while it is still writing. Live delay sweeps found that 5 ms
between storage commands and 5 ms before `SAVE:STARt` are the minimum working
values; either delay at 0 ms failed. At 5/5 ms, one channel completed in
151.6 ms and sequential CH1+CH2 completed in 395 ms. A ten-run CH1+CH2 series
then completed 10/10 with full 11,256-byte files: 307.9 ms minimum, 353.4 ms
mean, and 461.2 ms maximum. Manual front-panel saves remain visible and
importable as well.

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

`scope_capture_mode` defaults to `periodic`. For an externally orchestrated
stepped experiment, start with `"scope_capture_mode": "manual"` and request
each exact frame at its intended point:

```http
POST /captures/recording/scopes/{scope_device_id}/frame
Content-Type: application/json

{"label": "sink_set_4A"}
```

The request uses the stored Screen/Data/channel/trigger preferences and always
reads the configured scalar Measurements. It saves one frame in the active
recording directory and writes its sequence, status, label, and relative
capture-manifest path into that recording's fixed scope columns. Manual mode
creates no periodic frames; recording STOP restores scope RUN and ordinary
polling.

```http
GET /captures/status
POST /captures/recording/stop
GET /captures/files/{filename}
```

Only one CSV recording can be active. Starting a second returns HTTP `409`.
Recording drivers may classify invariant configuration separately from live
values. Such settings are written once as compact `initial_settings` text in
the instrument group header and do not receive repeating value/unit/status/
quality columns. Snapshots remain single-row complete state captures.

ITECH recording streams only measured voltage/current/power and commanded
voltage/current. Its signed current, voltage, and power limits plus initial
Output, priority, and direction are recorded once in `initial_settings`.
For each connected MHO1, recording pauses the transient Dashboard poll and
captures one complete frame per configured scope interval. If trigger waiting
is enabled, it instead repeats SINGLE/wait/save/re-arm and adds the trigger
timestamp, sequence, state, and relative capture-manifest path to the same CSV.
Non-triggered periodic frames also add their sequence and relative manifest
path to the same scope column block.
