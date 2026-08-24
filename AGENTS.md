# OpenBench automation

When a task needs to operate the running OpenBench application, use its local
JSON API instead of clicking the Dashboard or calling hardware drivers directly.

1. Check `GET http://127.0.0.1:8000/api/v1/health`.
2. If the server is not running, tell the user to double-click
   `Start OpenBench.cmd`, or run `scripts/start-openbench.ps1`.
3. Read `docs/automation-api.md` for the compact endpoint list. Use
   `/openapi.json` only when exact live schemas are needed.
4. Use `/api/v1/devices/.../settings` for instrument context, polling, and
   MHO1/ETO5004 frame-data options. ETO5004 firmware `3.392.132` has no working
   direct screenshot query, but its documented stored-capture plus HTTP-download
   fallback is validated, so Screen may be enabled for that model. Each ETO
   screenshot creates a file in the scope's `/pictures/Screenshots` storage.
5. Use `/api/v1/oscilloscopes/{device_id}/maximum-capture` only for an explicit,
   one-shot MHO1 or ETO5004 full-memory ASCII export. It requires the scope to already
   be in STOP, never changes memory depth, and owns the instrument until the
   asynchronous job reaches a terminal state. Do not start it during common
   Snapshot/CSV capture or mutate the scope while it is active.
6. Use `/api/v1/captures/...` for snapshot and CSV recording actions, including
   `title` and `comment`.
7. Use `/api/v1/generators/...` for FeelElec state and control. Read current
   state first. Enabling outputs, starting sweep/manual burst, or loading a
   preset requires explicit operator intent and known wiring/load context.
8. The FeelElec counter starts paused. Select `frequency`, `count`, or `both`
   through its counter endpoint; only `both` intentionally reads both groups.
9. Use `/api/v1/power-supplies/...` for FNIRSI DPS-150 state, output,
   protections, presets, metering, and programs. Read state first. Output
   enable, energized preset application, sequence/sweep start, or stop while
   retaining output requires explicit operator intent and known wiring/load.
   Natural program completion and ordinary stop force Output OFF.
10. Use `/api/v1/logic-analyzers/...` to configure, start, arm, stop, monitor,
   and download Kingst LA2016 captures. Complex cross-instrument conditions
   belong in the calling Codex workflow or script; do not add them as hidden
   Dashboard rules.
11. Use `/api/v1/source-measure-units/...` for OWON SPM source and built-in
   multimeter state/control. Read state first. Output enable requires explicit
   operator intent and known wiring/load; Output OFF is always the safe action.
   The tested SPM6103 profile is bounded to 60 V, 10 A, and 300 W; other SPM
   models require their own validated profile.
12. Use `/api/v1/bidirectional-power-supplies/...` for ITECH IT6000C state,
   fixed CV/CC source/sink operating points, protections, slew, delays, and
   watchdog control. Read state first. Output enable requires explicit operator
   intent, known wiring/load, `wiring_confirmed`, safe matrix state, and enabled
   OVP/OCP/OPP. Positive current sources and negative current sinks. The only
   live-tested profile is IT6054C-800-225; other models require a validated
   profile. Output OFF is always the safe action.
13. Never send arbitrary SCPI, shell, GPIO, or safety-bypass commands through
   OpenBench. Use only the documented endpoints.

The Dashboard remains the operator interface; the REST API is the automation
interface.

## Reproducibility rule

Required native tools, firmware workflows, generated drivers, and patched
libraries must never exist only in an ignored local directory. Pin upstream
sources/downloads, record checksums, track patches, provide build/setup and
diagnostic scripts, generate artifact manifests, preserve third-party licenses,
and update `docs/portability.md`. Do not commit proprietary firmware when a
verified local extraction workflow is available. New physical drivers must
follow `docs/driver-development.md` and include a dated hardware test record.
The canonical Codex integration lives under `skills/openbench`; update it in
the same change whenever supported API behavior or safety constraints change.
