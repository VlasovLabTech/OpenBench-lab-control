# Reproducibility and portability

OpenBench must be recoverable from the repository and documented upstream
downloads. A working developer machine is not a source of truth.

## Fresh Windows installation

1. Install Git and Python 3.11 or newer.
2. Clone the repository.
3. Double-click `Setup OpenBench.cmd`, or simply double-click
   `Start OpenBench.cmd`: the launcher performs the locked setup on first use.
4. Run `scripts\diagnose-openbench.ps1` to inspect the environment.
5. For Kingst LA2016 support, follow the two optional steps below.

The setup script creates `.venv`, installs the tested versions from
`requirements/windows-runtime.lock`, installs OpenBench itself in editable mode,
runs an import smoke test, and records the lock hash in
`.openbench/setup-state.json`. When the lock changes, the normal launcher
refreshes the environment before starting the server.

`scripts/test-package-install.ps1` independently builds the wheel, installs only
its declared base dependencies into a fresh temporary environment, verifies the
packaged bilingual web assets, and starts an isolated server health check. Its
temporary files stay below ignored `.openbench/audit/` and are removed after a
successful or failed run unless explicitly retained for diagnosis.

All mutable project-local state is consolidated below `.openbench/`. The default
database is `.openbench/data/openbench.db`; recordings, screenshots, waveform
exports, logic captures, and analysis reports are below
`.openbench/data/captures/`. This directory is ignored as a unit so a normal Git
operation cannot publish bench comments, hardware identifiers, or captured data.

MHO1 direct screenshot diagnostics use the pinned `python-vxi11` runtime for a
VXI-11 comparison alongside the instrument's raw TCP service; no vendor binary
is required.

Production MHO1 waveform acquisition also requires no vendor executable or
firmware artifact. It uses the repository's bounded raw-TCP SCPI transport and
standard-library ASCII parsing. The exact four-channel frame contract and the
2026-08-02 physical results are tracked in
`docs/mho1-ascii-minimal-live-test-2026-08-02.md`; the reproducible isolated
probes are `scripts/mho1_ascii_four_channel_probe.py` and
`scripts/mho1_ascii_ten_frame_probe.py`. Runtime captures remain ignored local
state, while the scripts, tests, command contract, and dated record belong in
Git.

The Micsig ETO5004 driver likewise requires no vendor executable, firmware
image, generated binding, or proprietary runtime. Discovery, TCP SCPI framing,
ASCII/WORD-hex parsing, scope-side screenshot discovery, HTTP download, and CSV
generation use the OpenBench source tree and Python standard library. The
protocol reference is Micsig's official
`Micsig-Oscilloscope-SCPI-Command-Manual-EN-202601`, published at
`https://www.micsig.com/uploads/Micsig-Oscilloscope-SCPI-Command-Manual-EN-202601_1770002260.pdf`;
the repository does not redistribute that vendor PDF. Physical validation and
the representative artifact hash are recorded in
`docs/eto5004-live-test-2026-08-03.md`.
The shared MHO1/ETO5004 MAXIMUM ASCII exporter adds no dependency or generated
binary. Its 15,625-point chunk bound is taken from that official manual, and
its tracked driver/service tests cover STOP enforcement, current-depth
preservation, streamed assembly, progress, and reader-context restoration.
MHO1 physical validation is explicitly deferred in
`docs/mho1-maximum-ascii-test-plan-2026-08-03.md`. Runtime `.txt` and
`capture.json` artifacts remain ignored local capture data.

Development dependencies use:

```powershell
scripts\setup-openbench.ps1 -Dev
```

## Codex skill

The canonical OpenBench skill, compact API reference, and command-line helper
are tracked under `skills/openbench`. Install or update the workstation copy
with:

```text
Install Codex Skill.cmd
```

The installer copies the tracked package to `CODEX_HOME\skills\openbench`, or
to `%USERPROFILE%\.codex\skills\openbench` when `CODEX_HOME` is unset. The
repository copy is authoritative. When API behavior changes, update the
application documentation, the skill reference/helper, and their tests in the
same commit.

## Kingst LA2016 on Windows

The Kingst integration has two independent local components. Neither is stored
as an unexplained binary in Git.

### Vendor firmware

Run:

```text
Install Kingst Firmware.cmd
```

The script:

1. downloads the pinned official KingstVIS Linux archive;
2. verifies its SHA-256;
3. downloads two files from the pinned `sigrok-util` revision and verifies them;
4. extracts the vendor firmware and FPGA images locally;
5. verifies the LA2016 hashes recorded in `scripts/kingst-runtime.lock.json`;
6. installs the extracted files under `%LOCALAPPDATA%\sigrok-firmware`.

The proprietary firmware is not redistributed by the OpenBench repository.

### sigrok runtime

Install MSYS2 UCRT64, then run:

```text
Build Kingst Runtime.cmd
```

or:

```powershell
scripts\build-kingst-sigrok.ps1 -MsysRoot C:\msys64
```

The build uses pinned libsigrok and sigrok-cli commits, applies the tracked
Windows event-loop patch, compiles only the Kingst driver, collects the actual
DLL dependency closure, includes upstream license texts, performs a version
smoke test, and atomically installs the package under
`.openbench/tools/sigrok-modern`. Every build creates a
`runtime-manifest.json` containing source revisions and hashes of all packaged
files.

The build tree and runtime are ignored because they are generated artifacts.
The source pins, patch, build script, firmware extraction procedure, checksums,
and licenses are tracked.

## What belongs in Git

- application source, tests, API documentation, and migration notes;
- the canonical Codex skill, API helper, and installation script;
- dependency lock files;
- driver protocols and bounded transport implementations;
- source revisions, patches, build scripts, artifact manifests, and checksums;
- third-party license and attribution files;
- hardware test records that identify the exact model/firmware and limits.

The following are local state and must not be committed:

- `.venv/`, `.openbench/`, caches, and build trees;
- SQLite databases and sidecar files;
- captures and user comments/context under `.openbench/data/`;
- extracted proprietary firmware;
- machine-specific COM ports, BLE addresses, IP addresses, and USB bus addresses.

Stable physical identity may be persisted only when the driver derives it from
a serial number, USB port path, or another documented stable identifier.

## Offline operation

After the initial setup, ordinary Dashboard, API, database, and device-driver
operation have no CDN dependency. HTMX 2.0.4 is stored locally with its license.
The English UI source and the Russian translation catalog are also local static
assets; the language choice is stored in a same-site browser cookie and needs no
network service.
Network instruments naturally still require their local LAN connection.
OWON SPM support needs no vendor binary: it uses the locked `pyserial` runtime,
documented SCPI, and the standard CH340 driver supplied by Windows/Windows
Update. No machine-specific COM number is persisted; the SPM serial number is
the stable OpenBench identity.

ITECH IT6000C support likewise needs no vendor executable or proprietary
firmware. It uses locked `pyserial`, the Windows USB virtual-COM driver, and the
bounded SCPI implementation tracked in the repository. Discovery identifies
USB `2EC7:A4A7`, verifies the exact model/serial reply at 115200 or 9600 baud,
and never persists the machine-specific COM number. The command allowlists,
validated model profile, official-document references, and dated hardware test
record are sufficient to reproduce this integration on another Windows host.

## Linux status

The Python application and most transports are designed to be portable. A
basic Linux developer installation is:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[hardware,dev]"
ruff check .
mypy src
pytest -q
```

Linux hardware access may require distribution packages and udev rules for USB
and serial devices. The custom libsigrok timer patch is Windows-only; Linux
should use a current distribution or locally built sigrok. Linux hardware is
not yet part of the validated hardware matrix, so support must be described as
experimental until physical tests are recorded.

## Release rule

A release artifact must be produced by automation from a clean checkout. It
must include its dependency/source manifest and third-party notices. A binary
copied from a developer workstation without a build record is not a release.
