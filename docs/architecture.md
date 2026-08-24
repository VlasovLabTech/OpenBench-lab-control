# OpenBench prototype architecture

## Scope

This document describes the stage-one executable prototype and its first
physical instrument driver. It does not describe future recipe execution, MCP
integration, or ESP32 firmware.

## Runtime topology

```text
Browser
  ├── GET / and /matrix ───────────────┐
  ├── HTMX matrix/safety actions ──────┤
  └── WS /ws/measurements ─────────────┤
                                       ▼
CLI ───────────────────────────► Application services
REST /api/v1 ──────────────────► DeviceService
                                 MeasurementService
                                 MatrixService
                                       │
                     ┌─────────────────┼─────────────────┐
                     ▼                 ▼                 ▼
                Registry /        Capability        SQLAlchemy
                Scheduler         protocols         repositories
                     │                 │                 │
                     └────────► instrument drivers ──────┘
                                                           │
                                                           ▼
                                                         SQLite
```

## Layer boundaries

### Domain

`openbench.domain` contains immutable dataclasses for devices, channels,
measurements, matrix objects, apply results, and safety state. These classes have
no FastAPI or SQLAlchemy dependency.

### Core

`openbench.core` contains:

- narrow capability protocols (`Instrument`, `MeterCapability`,
  `MatrixCapability`);
- the device registry;
- the bounded asyncio measurement event bus;
- the per-channel polling scheduler;
- safety state constants.

The scheduler owns one task per channel. A task awaits a read before calculating
the remaining interval, so the same channel is never polled concurrently with
itself. Monotonic loop time controls scheduling and is included in each sample.
Transport failures produce a `disconnected` measurement; malformed instrument
data remains `invalid`. A watchdog also marks a target disconnected when a read
does not complete within its polling interval plus a bounded grace period. A
later valid sample restores the connected state automatically. Stop signals
interrupt the bounded wait and application shutdown awaits all poll tasks.

Targets can also be added after startup by a selected discovery action. Their
polling intervals can be changed while the scheduler is running; the affected
task is restarted immediately with the new interval.

### Drivers

`openbench.drivers.simulated` contains the deterministic 12 V meter and logical
matrix used for development and tests. `openbench.drivers.ut61eplus` contains a
read-only driver for a UNI-T UT61E+ connected through either a CH9329 custom-HID
adapter (`1A86:E429`) or a CP2110 HID-to-UART adapter (`10C4:EA80`). It sends the
documented reading request, validates the frame length and checksum, and
normalizes values to SI units. Multiple adapters are discovered independently.
`openbench.drivers.ut61e` contains the shared read-only protocol driver for
original UNI-T UT61D and UT61E meters. It discovers the `1A86:E008`
UT-D04/CH9325 interface, configures its model-specific unidirectional UART bridge,
validates FS9922 frames for UT61D and ES51922 frames for UT61E, and normalizes
values to SI units. The UT61E path strips the captured parity bit; UT61D retains
all eight data bits because its flags and units use the high bit. Because the
stream has no model identifier, the discovery action supplies UT61D or UT61E
explicitly and selects 2400 or 19200 baud respectively.
`openbench.drivers.ut197` discovers the meter over Bluetooth LE,
maintains a persistent GATT connection, polls through the vendor UART service,
reassembles notifications, validates checksums, decodes status flags, and
normalizes readings to SI units. `openbench.drivers.micsig_mho1` discovers MHO1
oscilloscopes with VXI-11 broadcast plus `*IDN?`, keeps the validated SCPI
connection, and exposes bounded channel, acquisition, timebase, EDGE-trigger,
start/stop/single, and capture operations with setting read-back. A snapshot
stops the scope, obtains one PNG
containing all four traces, reads scale/position/timebase calibration and
scalar measurements, and emits a calibrated screen-column CSV without
interpolation.
The separate numeric waveform path reads bounded fast-ASCII sample blocks from
the MHO1, closes the SCPI connection at each firmware-specific block boundary,
and can write a local multi-channel CSV for explicit capture and diagnostics.
`openbench.drivers.micsig_eto` reuses the bounded Micsig transport and scope
service surface but validates only the exact `ETO5004` identity. It uses the
instrument serial number for the stable device ID, while host/IP remains a
transient descriptor. Production capture selects the documented NORMAL
`DATA?` transaction through the model-specific WORD-hex path; the live-tested firmware
returns 1,100 samples for one selected channel and requires a 0.5 correction to
its reported WORD vertical scaling. Atomic multi-channel Data is not claimed:
the same firmware returns an empty block after switching to the second source in
one stopped frame. ETO discovery is explicit by default, so adding the driver
does not introduce a silent network scan. Direct screenshots remain broken on
firmware `3.392.132`, but the registered `screenshot_capture` capability uses the
documented stored screenshot command and downloads the new PNG/JPEG through the
scope HTTP `/pictures/Screenshots` directory.
Both Micsig drivers expose a distinct STOP-only MAXIMUM ASCII stream. It reads
the current acquisition depth without changing it, holds the driver's operation
lock for the complete transaction, and emits validated chunks of at most 15,625
points instead of materializing full memory in RAM. The MHO1 implementation
uses fresh SCPI sessions between commands to match its known firmware behavior.
`ScopeMaximumCaptureService` owns the asynchronous one-shot job, performs
conservative disk-space preflight, writes one raw ASCII artifact per channel
plus a manifest, publishes point-based progress, suspends live scalar polling,
and reserves the device from common capture and all mutating API/UI operations
until terminal completion.
`openbench.drivers.feeltech_fy` discovers CH340 (`1A86:7523`) serial interfaces,
identifies FY-series generators with the read-only `UMO` query, and polls the
documented read commands for waveform, frequency, amplitude, offset, duty
cycle, phase, and output state on CH1 and CH2. A short coherent-state cache lets
the scheduler expose those fourteen outputs plus six counter measurement slots
without issuing a complete state read per channel. The counter starts paused
and queries only the selected frequency/timing, count, or combined group.
Combined mode may alternate the hardware display. The transport accepts only
an explicit protocol allowlist.
The generator driver validates complete proposed channel state before writing,
temporarily disables an active output during signal changes, and verifies each
basic write by reading it back. The signal-generator service adds the matrix
safety interlock and exposes synchronization, presets, burst/trigger,
ASK/FSK/PSK, timed sweep/VCO, and counter operations. Unsupported FY6200
advanced reads are reported as unavailable; sweep/VCO remains explicitly
write-only/unverified. Undocumented AM/FM/PM and round-trip controls are not
fabricated by the driver. The FY6200 hardware dialect encodes an FSK secondary
frequency write as a fixed-width integer count of microhertz even though newer
published FY-series protocols show a decimal-hertz example; OpenBench uses the
hardware-confirmed form and verifies it through `RFK`.
`openbench.drivers.fnirsi_dps150` discovers the FNIRSI/AT32 USB virtual COM
identity (`2E3C:5740`), establishes the documented 115200 8N1 hardware-flow-
controlled binary session, validates packet checksums and model/HW/FW identity,
and parses one coherent 139-byte state dump. A short cache lets fifteen
scheduler channels share that dump. Output, protection, display, metering, and
preset writes are followed by a complete state read-back. The driver validates
the live input-limited voltage/current ceilings, resolution, and power before
writing. An OFF transition precedes setpoint writes when an OFF state is
requested, an ON transition is last when enabling, and live setpoint changes
reduce a limit before increasing the other dimension.

`openbench.drivers.owon_spm` separates a documented SCPI parser, bounded CH340
serial transport, and combined source/multimeter instrument. Discovery sends
only `*IDN?`, accepts the tested OWON SPM6103 reply, and derives device identity from the
instrument serial rather than COM number. Because FeelElec uses the same CH340
USB identity, Find All probes SPM first and excludes claimed ports from the
FeelElec scan. A coherent short cache backs nine scheduler channels. The source
driver validates resolution, model limits, and the 300 W envelope, pauses an
active output before setpoint changes, writes Output ON last, reads every state
back, and returns the panel to local control. Multimeter readings are normalized
to SI units; function, documented auto/manual ranges, relative mode, and Hold
are allowlisted and read back. Capacitance range remains read-only because the
SPM programming manual exposes only its query.
The front-panel List waveform editor and startup-auto-output setting are also
left local: no corresponding commands are present in the published SPM
programming manual, and OpenBench does not guess undocumented output commands.

`openbench.drivers.itech_it6000c` contains an exact-model SCPI profile, bounded
USB-VCP transport, and bidirectional source/load instrument. Discovery filters
USB `2EC7:A4A7`, tries 115200 before the instrument's possible 9600 fallback,
validates `*IDN?`, and derives the device ID from the instrument serial number.
Only the physically validated `IT6054C-800-225` profile is accepted. A short
coherent-state cache backs fourteen scheduler channels. Fixed CV/CC operating
points, signed source/sink current and voltage limits, positive/negative power
limits, protections, slew, delays, and watchdog are allowlisted; no arbitrary
SCPI or advanced List/Battery/Solar function crosses the service boundary.
Every mutation is followed by full read-back. Active setpoint changes pause
Output, and disconnect/shutdown force Output OFF before returning local control.

`openbench.drivers.kingst_la2016` is a bounded wrapper around the documented
sigrok CLI rather than a second USB protocol implementation. Discovery accepts
only `kingst-la2016` descriptors and removes the two PWM outputs from the
read-only acquisition surface. The transport constructs a fixed command from
validated channels, sample count/rate, threshold, capture ratio, and hardware
trigger conditions; callers cannot inject arbitrary CLI options. The tested
LA2016 exposes 16 logic inputs, rates from 20 kHz to 200 MHz, multiple level
triggers, and at most one edge trigger. Native `.sr` output preserves the full
capture for later PulseView or external sigrok decoding.
On Windows the transport prefers `.openbench/tools/sigrok-modern`, built with
libusb 1.0.30, and keeps the native Kingst WinUSB driver for sequential use
with KingstVIS. Device identity uses the stable physical USB port path. The
runtime's known post-capture cleanup access violation is accepted only when the
completed `.sr` archive passes ZIP, CRC, metadata, and non-empty logic checks.

Drivers do not know about OpenBench HTTP routes, templates, or SQLAlchemy. The
Micsig transport uses the instrument's own read-only HTTP file service to fetch
PNG artifacts created by documented SCPI commands.
The service is read-only in the tested firmware even when acquisition is STOP:
HTTP `DELETE` returns success without removing screenshot, CSV, or BIN files,
and no documented SCPI deletion command exists. Automated screenshot cleanup
therefore remains blocked on a verified instrument-side deletion mechanism.

### Services

Services implement use cases shared by API, UI, and CLI:

- `DeviceService` registers and lists instruments and channels.
- `MeasurementService` persists history and maintains a thread-safe latest-value
  cache.
- `ScopeMeasurementService` owns the explicit MHO1 live profile (up to ten
  channel/measurement pairs), polls it no faster than once every two seconds,
  and publishes transient readings on the common event bus. The idle Dashboard
  path performs only scalar queries while acquisition remains in RUN; it does
  not call the frame path and does not persist history. An active common capture
  is the only consumer that writes those events or an explicit frame package.
  Applying a profile first
  clears every front-panel measurement with `:MEASure:CLEar all`, then opens
  every selected channel/item pair sequentially with `:MEASure:OPEN` and a
  100 ms inter-command pause. Ordinary reads do not repeat those setup commands
  and use no artificial inter-query delay. Firmware `2.154.75` exposes ten
  global measurement slots; a 20-item live probe returned the first ten and
  marked the remaining ten unavailable.
- `InstrumentPreferenceStore` persists safe UI and acquisition preferences by
  stable device ID. Reconnecting the same instrument restores context and
  polling; MHO1 also restores its artifact/profile selection and LA2016 restores
  acquisition setup. Hardware output state and commands that start acquisition
  are deliberately not replayed.
- `CaptureService` takes a simultaneous current sample from every meter target
  or subscribes to the live event bus for an operator-controlled CSV session.
  Operator title/comment metadata is written once above a wide table. Each
  instrument owns a horizontal column block; an event row fills only the block
  of the reporting instrument. UTF-8 BOM preserves Cyrillic in spreadsheet
  applications. Local-time filenames use `snap`/`rec`, ten safe title
  characters, and a numeric collision suffix. Every MHO1 snapshot has a sibling
  folder containing capture JSON, measurements CSV, the original ASCII payload
  for each enabled channel, and the screenshot when enabled. The JSON preserves
  the UTC timestamp, frame duration, selected channels, common nine-field
  preamble, and derived sample timing. Relative paths and selected scalar
  measurements also appear in the main table and CSV recordings. Transient
  Dashboard scope polling is scalar-only, shares one transport session per
  profile, and never sends STOP/RUN. Active recording suspends that poll and
  owns periodic frame capture. Optional trigger waiting implements
  SINGLE -> 50 ms status polling -> STOP -> frozen-frame save -> re-arm; trigger
  timestamps and artifact paths are emitted into the common CSV timeline.
- `DCPowerSupplyService` applies the matrix safety interlock to DPS-150 output
  enable, preset application, and programs. It owns one asynchronous sequence
  or voltage/current sweep per supply, including pause/resume, bounded dwell,
  read-back on every step, cancellation, and forced Output OFF on completion
  or failure. Disconnect confirms Output OFF before closing the transport;
  graceful application shutdown performs a best-effort all-supply stop.
- `SourceMeasureUnitService` applies the same safety interlock to OWON SPM
  source enable and owns verified source/protection/DMM operations. Disconnect,
  shutdown, and emergency stop force Output OFF before the serial port closes.
- `BidirectionalPowerSupplyService` applies matrix safety plus explicit wiring
  confirmation to ITECH Output enable, requires enabled OVP/OCP/OPP, owns
  verified CV/CC/protection/advanced operations, and forces Output OFF during
  disconnect, shutdown, and emergency stop. Its external-experiment reservation
  waits out an in-flight scheduled read and suspends ordinary ITECH poll targets
  without changing the saved interval. Compact V/I samples remain explicit,
  common CSV start uses cached invariant state, and release defers ordinary
  polling for one full configured interval.
- `LogicAnalyzerService` owns per-analyzer acquisition settings, schedules an
  optional delayed start from the global recording lifecycle, maps sigrok
  driver states to `pretrigger`/`armed`/`capturing`/`downloading`, estimates
  remaining time, writes `.sr` plus JSON metadata, and publishes timestamped
  timeline events into an active common CSV recording. Immediate triggerless
  start, hardware-trigger arm, stop, status, and download remain separate REST
  operations so Codex or a script can implement complex cross-instrument
  experiments without a hidden in-application rule engine.
- `MatrixService` owns profile CRUD, validation, persisted active state, the
  safety latch, audit events, and driver switching.

The service boundary is where business invariants live. Templates and API
handlers only translate input/output.

### Storage

SQLAlchemy 2.x entities are defined separately from both domain models and
Pydantic schemas. SQLite stores:

- registered devices and channels;
- measurement history;
- safe per-instrument UI and acquisition preferences;
- matrix ports, profiles, and connections;
- the singleton active matrix state;
- the singleton global safety state;
- an append-only audit log.

Repositories convert ORM rows into domain objects. Foreign-key enforcement is
enabled on every SQLite connection.

### API and web

Pydantic v2 schemas define REST input/output. API routers are split by device,
instrument-control, logic-capture, matrix, and WebSocket concerns. Jinja2
templates render two working pages, while HTMX posts forms to dedicated UI
routes. The dashboard receives voltage samples over WebSocket; active logic and
MHO1/ETO5004 full-memory capture panels refresh their bounded status once per second.

## Matrix atomicity

`MatrixService.apply_profile` uses a process-local reentrant lock plus a database
transaction:

1. Load the complete target profile.
2. Validate all routes against the current port inventory.
3. Check that the global safety state is `safe`.
4. Capture the previous persisted route set.
5. Open every current route.
6. Apply the entire validated target set.
7. Update active profile, active connections, and audit log in one transaction.
8. Commit.

Validation failure occurs before driver switching. A driver or database failure
rolls back the database transaction and triggers a best-effort restoration of
the previous driver routes.

## Emergency stop

Emergency stop is intentionally independent of profile validity:

1. Acquire the same matrix operation lock.
2. Open all simulated driver routes.
3. Clear persisted active routes.
4. Latch global state to `emergency_stop`.
5. Append an audit event.
6. Commit the state changes together.
7. Cancel power programs and best-effort disable every registered FeelElec and
   DPS-150 output.

While latched, profile application is rejected. Only the explicitly named
simulation reset endpoint clears the latch in this prototype.

## Event delivery

Each WebSocket subscriber receives a bounded asyncio queue. When a slow
subscriber fills its queue, the oldest pending measurement is discarded before
the newest value is inserted. Subscriber queues are removed in a `finally`
block, so disconnected clients cannot leak memory.

## Web localization

English is the source language of the Jinja templates and API schema. The
vendored `web/static/i18n.js` catalog provides the Russian interface, including
content inserted by HTMX and live status updates. The selected `en` or `ru`
locale is stored in a same-site `openbench_language` cookie and is available on
Dashboard, Matrix, and the interactive API page. No translation CDN
or external service is used.

## Startup and shutdown

FastAPI lifespan creates the database schema, seeds stable simulated inventory
and the initial profile, restores the persisted matrix state into the simulated
driver, and starts the scheduler. Physical transports are discovered
independently through the selected driver button; optional automatic startup
discovery remains configurable. Shutdown cancels scheduled logic captures,
stops active acquisition and CSV capture, cancels an active Micsig maximum
export while preserving partial files, closes polling and persistent BLE/SCPI
connections, and disposes the database engine.

The default SQLite database and every generated capture live below
`.openbench/data/`. Build tools, caches, audit reports, and archived local
research use sibling directories under `.openbench/`; the whole mutable tree is
ignored by Git.
