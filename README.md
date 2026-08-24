# VlasovLab OpenBench

[![CI](https://github.com/VlasovLabTech/OpenBench-lab-control/actions/workflows/ci.yml/badge.svg)](https://github.com/VlasovLabTech/OpenBench-lab-control/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](https://www.python.org/)

OpenBench is a local, safety-oriented control and data-acquisition platform for an
electronics laboratory bench. It combines instrument discovery, live measurements,
CSV and waveform capture, a commutation matrix, automated experiments, and a REST API
in one offline-capable application.

The web interface is available in **English and Russian**. Use the `EN` / `RU` switch
in the header; the choice is stored locally in the browser.

> [!WARNING]
> OpenBench can control real power supplies, signal generators, and electronic loads.
> Check the wiring, load limits, protection settings, and instrument state before
> enabling an output. The software interlock is an additional safeguard, not a
> substitute for proper laboratory safety.

## Highlights

- Responsive bilingual Dashboard, Matrix, and interactive API pages.
- Stable instrument identities, configurable polling, live WebSocket updates, and
  disconnected/reconnected state handling.
- Instant snapshots and timed or continuous CSV recordings with titles and comments.
- Oscilloscope screenshots, scalar measurements, numeric waveforms, stored captures,
  and bounded full-memory exports where the instrument supports them.
- Persistent commutation profiles with validation and break-before-make application.
- Global emergency stop that disables supported sources, opens all matrix routes, and
  latches the safety state.
- FastAPI REST and WebSocket interfaces plus a Typer command-line client.
- SQLite persistence and project-local runtime storage with no required cloud service.
- Simulated instruments for evaluation and development without laboratory hardware.

## Supported instruments

| Instrument | Transport | Implemented support |
| --- | --- | --- |
| Simulated meter and matrix | In-process | Deterministic measurements and safe UI/API evaluation |
| UNI-T UT61D / original UT61E | Optical USB adapter | Read-only ES51922 measurements |
| UNI-T UT61E+ | CH9329 or CP2110 USB HID | Read-only measurements, checksum validation, multiple meters |
| UNI-T UT197 | Bluetooth LE | Discovery, persistent GATT connection, normalized measurements |
| Micsig MHO1 | LAN, SCPI + HTTP | Discovery, control, measurements, screenshots, waveforms |
| Micsig ETO5004 | LAN/Wi-Fi, SCPI + HTTP | Discovery, bounded waveforms, stored screenshots, full-memory export |
| FeelElec FY series | CH340 USB serial | Two-channel control, presets, sync, burst, modulation, sweep, counter |
| FNIRSI DPS-150 | AT32 USB virtual COM | Output, protections, display, metering, presets, sequences, sweeps |
| OWON SPM6103 | CH340 USB serial, SCPI | 60 V / 10 A / 300 W source control and built-in multimeter |
| ITECH IT6054C-800-225 | USB virtual COM, SCPI | Bounded CV/CC source and regenerative-load control with protections |
| Kingst LA2016 | USB through sigrok | Configuration, immediate/triggered capture, `.sr` and metadata artifacts |

The physical validation scope and known limitations are recorded in the dated files
under [`docs/`](docs/). Other models in the same product families are not assumed to
be compatible until they have a validated profile.

## Requirements

- Windows 10 or 11 and Python 3.11 or newer for the tested setup path.
- Git is recommended for installation and updates.
- Supported hardware drivers supplied by Windows or Windows Update.
- MSYS2 UCRT64 only when building the optional Kingst/sigrok runtime.

Linux application support is experimental. Hardware permissions, udev rules, and
transport behavior have not yet received the same physical test coverage as Windows.

## Quick start on Windows

1. Clone or download this repository.
2. Double-click `Setup OpenBench.cmd`.
3. Double-click `Start OpenBench.cmd`.
4. Open <http://127.0.0.1:8000/> if the browser does not open automatically.

The setup creates `.venv`, installs the pinned hardware-enabled Windows dependency
set, installs OpenBench in editable mode, and runs an import check. `Start
OpenBench.cmd` automatically refreshes an absent or outdated environment.

Use `Stop OpenBench.cmd` to stop the background server. Diagnose a setup with:

```powershell
scripts\diagnose-openbench.ps1
```

The local entry points are:

- Dashboard: <http://127.0.0.1:8000/>
- Commutation matrix: <http://127.0.0.1:8000/matrix>
- Interactive API: <http://127.0.0.1:8000/docs>
- Health check: <http://127.0.0.1:8000/api/v1/health>

OpenBench binds to `127.0.0.1` by default and provides neither authentication nor
TLS. Do not expose it directly to an untrusted network.

## Development setup

On Windows:

```powershell
scripts\setup-openbench.ps1 -Dev
```

On Linux (experimental):

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[hardware,dev]"
openbench serve
```

Run the quality checks before submitting a change:

```powershell
.venv\Scripts\ruff.exe check .
.venv\Scripts\mypy.exe src
.venv\Scripts\pytest.exe -q
```

Before publishing, scan the reachable Git history and every tracked or non-ignored
working-tree file with the pinned, checksum-verified Gitleaks release:

```powershell
scripts\audit-publication.ps1 -Full
```

To build the wheel, install it with only its declared base dependencies in a clean
virtual environment, and verify an isolated server health check:

```powershell
scripts\test-package-install.ps1
```

## Runtime data and privacy

All mutable project-local state is stored below the ignored `.openbench/` directory:

```text
.openbench/
├── data/
│   ├── openbench.db
│   └── captures/
├── cache/
├── tools/
└── archive/
```

This keeps databases, captures, waveform files, screenshots, logs, comments, hardware
identifiers, and local research out of normal Git operations. The directory can still
contain sensitive bench information, so inspect or remove it before sharing a complete
workspace archive. See [`SECURITY.md`](SECURITY.md) for reporting and deployment
guidance.

## Configuration

Configuration uses `OPENBENCH_...` environment variables. The most common settings
are:

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENBENCH_HOST` | `127.0.0.1` | HTTP bind address |
| `OPENBENCH_PORT` | `8000` | HTTP port |
| `OPENBENCH_DATABASE_URL` | `sqlite:///./.openbench/data/openbench.db` | Database URL |
| `OPENBENCH_CAPTURE_DIRECTORY` | `.openbench/data/captures/sessions` | Capture output directory |
| `OPENBENCH_AUTO_DISCOVER` | `false` | Discover ordinary instruments at startup |
| `OPENBENCH_MICSIG_HOSTS` | empty | Comma-separated known MHO1 addresses |
| `OPENBENCH_MICSIG_ETO_HOSTS` | empty | Comma-separated known ETO5004 addresses |
| `OPENBENCH_ITECH_IT6000C_AUTO_DISCOVER` | `false` | Allow IT6000C discovery at startup |
| `OPENBENCH_SIGROK_CLI` | auto-detected | Explicit `sigrok-cli` path |

Each driver also has an enable flag and a safe polling-interval setting. See
[`src/openbench/config.py`](src/openbench/config.py) for the complete list.

## Safety model

OpenBench treats output enable as a deliberate operation:

- Generator and power outputs start or return OFF wherever the instrument contract
  permits.
- High-energy ITECH output enable requires confirmed wiring, a safe matrix state, and
  enabled OVP/OCP/OPP protections.
- Energized presets, sweeps, sequences, bursts, and experiments require explicit
  operator action.
- The emergency stop attempts to disable every supported source, opens the matrix,
  and prevents further output enable until the interlock is reset.
- Instrument commands are bounded by driver allowlists; the API does not expose an
  arbitrary SCPI or shell-command tunnel.

Review the exact limitations of a driver before connecting unfamiliar hardware.

## Architecture

```text
Browser UI / REST / WebSocket / CLI
                  │
                  ▼
        Application services
 Device · Measurement · Capture · Matrix · Safety
                  │
          ┌───────┴────────┐
          ▼                ▼
 Drivers and registry   SQLite repositories
```

The web and API layers call application services rather than hardware drivers
directly. Drivers implement bounded capabilities; the registry, scheduler, event bus,
and safety service coordinate their behavior. See
[`docs/architecture.md`](docs/architecture.md) for component boundaries and
[`docs/automation-api.md`](docs/automation-api.md) for the compact automation API.

## Optional Kingst support

Kingst LA2016 requires two reproducible local steps:

1. Run `Install Kingst Firmware.cmd` to download, verify, extract, and install the
   pinned vendor firmware locally.
2. Install MSYS2 UCRT64 and run `Build Kingst Runtime.cmd` to build the pinned,
   patched sigrok runtime and its artifact manifest.

Proprietary firmware and generated binaries are not committed. The source pins,
checksums, patch, build procedure, and third-party licenses are tracked. See
[`docs/kingst-sigrok-runtime-build.md`](docs/kingst-sigrok-runtime-build.md).

## Codex automation

The canonical OpenBench Codex skill is tracked in [`skills/openbench`](skills/openbench).
Install or refresh the workstation copy with `Install Codex Skill.cmd`. The repository
copy remains the source of truth and must be updated when API behavior or safety
constraints change.

## Documentation

- [`docs/system-concept-ru.md`](docs/system-concept-ru.md) — concise current project
  context in Russian.
- [`docs/architecture.md`](docs/architecture.md) — architecture and runtime behavior.
- [`docs/automation-api.md`](docs/automation-api.md) — compact REST automation guide.
- [`docs/driver-development.md`](docs/driver-development.md) — physical-driver rules.
- [`docs/portability.md`](docs/portability.md) — clean-machine and reproducibility
  requirements.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — contribution and bilingual UI rules.
- [`SECURITY.md`](SECURITY.md) — vulnerability reporting and deployment boundary.
- [`SUPPORT.md`](SUPPORT.md) — questions, bug reports, and safe support requests.
- [`CHANGELOG.md`](CHANGELOG.md) — user-facing release history.
- [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) — community participation rules.

## Project status

OpenBench is an engineering project at version `0.1.1`. Its tested Windows workflows
are intended for controlled laboratory use; it is not a certified safety system or a
general-purpose remote instrument gateway. Compatibility claims are limited to the
models, firmware, and dated hardware records in this repository.

## License

OpenBench is released under the [MIT License](LICENSE). Third-party components retain
their own licenses as documented in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
