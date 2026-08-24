# Building the OpenBench Kingst runtime on Windows

OpenBench keeps the native Kingst WinUSB driver so KingstVIS and OpenBench can
use the LA2016 sequentially. The stock Windows sigrok nightly bundles an old
libusb that cannot reopen this interface, so OpenBench prefers a small local
runtime built with current libusb.

## Pinned sources

- libsigrok: `0bc2487778e660f4d3116729b6f4aee2b1996bb0`
- sigrok-cli: `f44dd91347e7ac797cefc23162b9fcf0b7329f1f`
- libusb: MSYS2 UCRT64 package, validated with 1.0.30
- local patch: `scripts/patches/libsigrok-windows-timer-events.patch`

The patch supplies a 5 ms GLib timer source when libusb has no pollable
Windows handles. It does not change the Kingst protocol or system driver.

## Automated build

The supported path is:

```powershell
scripts\build-kingst-sigrok.ps1 -MsysRoot C:\msys64
```

It validates `scripts/kingst-runtime.lock.json` and the tracked patch, installs
the build dependencies unless `-SkipPackageInstall` is supplied, checks out the
pinned revisions, builds only `kingst-la2016`, collects the DLL closure, adds
license files, validates the packaged executable, and writes a complete
`runtime-manifest.json`. Installation into `.openbench/tools/sigrok-modern` is
atomic and retains the previous working runtime if validation fails.

The commands below describe the same process for maintainers.

## Manual build outline

Use an MSYS2 UCRT64 shell and install:

```sh
pacman -S --needed base-devel git mingw-w64-ucrt-x86_64-toolchain \
  mingw-w64-ucrt-x86_64-glib2 mingw-w64-ucrt-x86_64-libusb \
  mingw-w64-ucrt-x86_64-libzip mingw-w64-ucrt-x86_64-libserialport \
  mingw-w64-ucrt-x86_64-hidapi mingw-w64-ucrt-x86_64-libftdi \
  mingw-w64-ucrt-x86_64-check
```

Clone and check out the pinned revisions, then apply the patch in the
libsigrok working tree:

```sh
git apply /path/to/openbench/scripts/patches/libsigrok-windows-timer-events.patch
```

Build only the required hardware driver:

```sh
mkdir -p /tmp/build-libsigrok /tmp/build-sigrok-cli
cd /tmp/build-libsigrok
/path/to/libsigrok/configure --prefix=/opt/openbench-sigrok \
  --enable-all-drivers=no --enable-kingst-la2016=yes \
  --disable-bindings --disable-static --enable-shared
make -j && make install

cd /tmp/build-sigrok-cli
PKG_CONFIG_PATH=/opt/openbench-sigrok/lib/pkgconfig \
  /path/to/sigrok-cli/configure --prefix=/opt/openbench-sigrok \
  --without-libsigrokdecode
make -j && make install
```

Copy `sigrok-cli.exe`, `libsigrok-4.dll`, and all DLLs reported by `ldd` into:

```text
.openbench\tools\sigrok-modern
```

This directory is intentionally ignored by Git. Verify the packaged runtime:

```powershell
.openbench\tools\sigrok-modern\sigrok-cli.exe --version
.openbench\tools\sigrok-modern\sigrok-cli.exe --driver kingst-la2016 --scan --loglevel 4
```

Do not install Zadig/libusbK for this workflow. Close KingstVIS before scanning
or capturing in OpenBench, and finish the OpenBench capture before reopening
KingstVIS.

## Firmware extraction

Run `scripts/install-kingst-firmware.ps1`. The script downloads the official
pinned KingstVIS Linux package and the pinned `sigrok-util` extractor, verifies
all downloads, extracts the firmware locally, and verifies the resulting
LA2016 hashes. No proprietary firmware is stored in this repository.
