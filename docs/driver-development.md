# Adding an OpenBench instrument driver

OpenBench drivers are deliberately small adapters behind stable application
services and REST endpoints. The Dashboard is not allowed to call hardware
transports directly.

## Driver structure

Create `src/openbench/drivers/<driver_name>/` and normally separate:

- `protocol.py`: pure encoding, decoding, validation, units, and model limits;
- `transport.py`: USB/HID/serial/BLE/LAN I/O, discovery, timeouts, and reconnect;
- an instrument class such as `meter.py`, `generator.py`, or `supply.py` that
  exposes the bounded capability used by application services.

Keep protocol parsing testable without physical hardware. Do not expose raw
SCPI, arbitrary serial writes, shell commands, or unrestricted GPIO through the
REST API.

## Identity and discovery

Discovery must validate the actual protocol/model, not only a friendly COM-port
name. Device IDs must remain stable across reconnects and must distinguish
multiple units of the same model. Prefer, in order:

1. instrument serial number;
2. stable USB physical port path;
3. documented BLE identity;
4. an explicitly documented fallback when the hardware exposes no identity.

Do not persist transient COM numbers, IP addresses, or USB bus addresses as the
only identity when a stable alternative exists.

Register discovery in `bootstrap.py`, expose it through the device service/API,
add it to the Dashboard's explicit Find list, and update the automation helper
and OpenBench Codex skill. `Find All` must remain bounded to enabled driver
types and must not connect the simulator automatically.

## Read-only and output instruments

Read-only instruments publish normalized values, units, quality, status, and
freshness. Silence beyond the allowed update interval must become disconnected
or unavailable rather than leaving an old value marked live.

Output instruments require stronger guarantees:

- read current state before changing it;
- validate the complete proposed state before any write;
- preserve omitted parameters;
- verify hardware read-back when the protocol permits it;
- treat output enable, presets, sweeps, and programs as explicit operator intent;
- provide a safe OFF path and document behavior on failure/disconnect.

## Tests and hardware records

Every driver needs:

- protocol vectors, including malformed and boundary inputs;
- mocked transport/discovery tests;
- service/API tests and Dashboard rendering checks;
- stable-identity and multiple-device tests where applicable;
- a dated `docs/<device>-live-test-YYYY-MM-DD.md` after physical validation.

The live record should include exact model, hardware/firmware version, adapter,
transport settings, safe wiring/load state, commands or API calls used, observed
limits, known gaps, and representative artifact hashes when useful.

## Native tools, firmware, and generated artifacts

If a driver requires a native executable, firmware extractor, patched library,
or vendor file:

1. pin upstream repository revisions or a versioned official download;
2. record SHA-256 hashes for downloads;
3. track every local patch;
4. provide a non-interactive build/setup script;
5. generate a manifest containing the resulting files and hashes;
6. include all applicable third-party notices and licenses;
7. document what cannot legally be redistributed and reproduce it locally;
8. add a diagnostic check that reports a missing or incompatible component.

Never make an ignored directory on one workstation the only copy of a required
runtime.

## Completion checklist

Before merging a driver:

```text
[ ] Protocol and limits are documented
[ ] Stable ID and multiple identical instruments are handled
[ ] API is bounded and automation-friendly
[ ] Output safety rules are explicit (if applicable)
[ ] Unit/API/UI tests pass
[ ] Physical live-test record exists or support is clearly marked unvalidated
[ ] Build/setup is reproducible on a clean machine
[ ] Checksums, manifests, licenses, and diagnostics are present
[ ] README, architecture, automation API, AGENTS.md, and Codex skill are updated
```

The future per-installation driver selector should consume a central driver
catalog. Until that catalog exists, do not silently scan a newly added driver
at server startup; add it to the explicit Find flow and keep automatic scanning
configurable.
