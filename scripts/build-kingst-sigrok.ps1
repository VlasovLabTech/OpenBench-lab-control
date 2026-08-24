[CmdletBinding()]
param(
    [string]$MsysRoot = $(if ($env:OPENBENCH_MSYS2_ROOT) { $env:OPENBENCH_MSYS2_ROOT } else { "C:\msys64" }),
    [string]$OutputDirectory,
    [switch]$SkipPackageInstall,
    [switch]$KeepBuildTree
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Convert-ToMsysPath([string]$Path, [string]$BashPath) {
    $previous = $env:OPENBENCH_CYGPATH_INPUT
    $env:OPENBENCH_CYGPATH_INPUT = [System.IO.Path]::GetFullPath($Path)
    try {
        $converted = (& $BashPath -lc 'cygpath -u "$OPENBENCH_CYGPATH_INPUT"').Trim()
        if ($LASTEXITCODE -ne 0 -or -not $converted) {
            throw "Could not convert path for MSYS2: $Path"
        }
        return $converted
    }
    finally {
        $env:OPENBENCH_CYGPATH_INPUT = $previous
    }
}

function Convert-FromMsysPath([string]$Path, [string]$BashPath) {
    $previous = $env:OPENBENCH_CYGPATH_INPUT
    $env:OPENBENCH_CYGPATH_INPUT = $Path
    try {
        $converted = (& $BashPath -lc 'cygpath -w "$OPENBENCH_CYGPATH_INPUT"').Trim()
        if ($LASTEXITCODE -ne 0 -or -not $converted) {
            throw "Could not convert MSYS2 path: $Path"
        }
        return $converted
    }
    finally {
        $env:OPENBENCH_CYGPATH_INPUT = $previous
    }
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$lockPath = Join-Path $PSScriptRoot "kingst-runtime.lock.json"
$lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
$patchPath = Join-Path $projectRoot $lock.patch.path
$actualPatchHash = (Get-FileHash -LiteralPath $patchPath -Algorithm SHA256).Hash
if ($actualPatchHash -ne $lock.patch.sha256) {
    throw "Kingst libsigrok patch differs from kingst-runtime.lock.json."
}

$bashPath = Join-Path $MsysRoot "usr\bin\bash.exe"
if (-not (Test-Path -LiteralPath $bashPath)) {
    throw "MSYS2 was not found at $MsysRoot. Install MSYS2 or pass -MsysRoot."
}
$env:MSYSTEM = "UCRT64"
$env:CHERE_INVOKING = "1"
$env:MSYS2_PATH_TYPE = "inherit"

if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $projectRoot ".openbench\tools\sigrok-modern"
}
$outputFull = [System.IO.Path]::GetFullPath($OutputDirectory)
$projectPrefix = [System.IO.Path]::GetFullPath($projectRoot).TrimEnd("\") + "\"
if (-not $outputFull.StartsWith($projectPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "OutputDirectory must stay inside the OpenBench project."
}

if (-not $SkipPackageInstall) {
    Write-Host "Installing MSYS2 UCRT64 build dependencies..."
    $packages = @(
        "base-devel",
        "git",
        "mingw-w64-ucrt-x86_64-toolchain",
        "mingw-w64-ucrt-x86_64-check",
        "mingw-w64-ucrt-x86_64-glib2",
        "mingw-w64-ucrt-x86_64-hidapi",
        "mingw-w64-ucrt-x86_64-libftdi",
        "mingw-w64-ucrt-x86_64-libserialport",
        "mingw-w64-ucrt-x86_64-libusb",
        "mingw-w64-ucrt-x86_64-libzip"
    ) -join " "
    & $bashPath -lc "pacman -S --needed --noconfirm $packages"
    if ($LASTEXITCODE -ne 0) {
        throw "MSYS2 dependency installation failed. Update MSYS2, then retry."
    }
}

$buildRootParent = Join-Path $projectRoot ".openbench\build\kingst-sigrok"
New-Item -ItemType Directory -Path $buildRootParent -Force | Out-Null
$buildRoot = Join-Path $buildRootParent ((Get-Date -Format "yyyyMMdd-HHmmss") + "-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
$buildMsys = Convert-ToMsysPath $buildRoot $bashPath
$patchMsys = Convert-ToMsysPath $patchPath $bashPath

$scriptPath = Join-Path $buildRoot "build.sh"
$scriptMsys = Convert-ToMsysPath $scriptPath $bashPath
$buildScript = @"
set -euo pipefail
export PATH=/ucrt64/bin:/usr/bin
ROOT='$buildMsys'
PREFIX="`$ROOT/prefix"

git clone --quiet '$($lock.sources.libsigrok.repository)' "`$ROOT/libsigrok"
git -C "`$ROOT/libsigrok" checkout --quiet --detach '$($lock.sources.libsigrok.commit)'
git -C "`$ROOT/libsigrok" apply '$patchMsys'
cd "`$ROOT/libsigrok"
./autogen.sh
mkdir "`$ROOT/build-libsigrok"
cd "`$ROOT/build-libsigrok"
"`$ROOT/libsigrok/configure" --prefix="`$PREFIX" \
  --enable-all-drivers=no --enable-kingst-la2016=yes \
  --disable-bindings --disable-static --enable-shared
make -j"`$(nproc)"
make install

git clone --quiet '$($lock.sources.sigrok_cli.repository)' "`$ROOT/sigrok-cli"
git -C "`$ROOT/sigrok-cli" checkout --quiet --detach '$($lock.sources.sigrok_cli.commit)'
cd "`$ROOT/sigrok-cli"
./autogen.sh
mkdir "`$ROOT/build-sigrok-cli"
cd "`$ROOT/build-sigrok-cli"
PKG_CONFIG_PATH="`$PREFIX/lib/pkgconfig" \
  "`$ROOT/sigrok-cli/configure" --prefix="`$PREFIX" --without-libsigrokdecode
make -j"`$(nproc)"
make install
"@
[System.IO.File]::WriteAllText(
    $scriptPath,
    $buildScript,
    [System.Text.UTF8Encoding]::new($false)
)

Write-Host "Building pinned libsigrok and sigrok-cli sources..."
& $bashPath -lc "bash '$scriptMsys'"
if ($LASTEXITCODE -ne 0) {
    throw "Kingst sigrok runtime build failed. Build tree retained at $buildRoot"
}

$prefixBin = Join-Path $buildRoot "prefix\bin"
$builtExecutable = Join-Path $prefixBin "sigrok-cli.exe"
if (-not (Test-Path -LiteralPath $builtExecutable)) {
    throw "Build completed without sigrok-cli.exe"
}
$builtExecutableMsys = Convert-ToMsysPath $builtExecutable $bashPath
$lddOutput = & $bashPath -lc "export PATH='$(Convert-ToMsysPath $prefixBin $bashPath)':/ucrt64/bin:/usr/bin; ldd '$builtExecutableMsys'"
if ($LASTEXITCODE -ne 0) {
    throw "Could not inspect sigrok-cli runtime dependencies."
}

$packageDirectory = Join-Path $buildRoot "package"
New-Item -ItemType Directory -Path $packageDirectory -Force | Out-Null
Copy-Item -LiteralPath $builtExecutable -Destination $packageDirectory

$dependencyPaths = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
foreach ($line in $lddOutput) {
    if ($line -match "=>\s+(?<path>/[^\s]+\.dll)\s+") {
        $msysPath = $Matches.path
        if (-not $msysPath.StartsWith("/c/WINDOWS", [System.StringComparison]::OrdinalIgnoreCase)) {
            [void]$dependencyPaths.Add((Convert-FromMsysPath $msysPath $bashPath))
        }
    }
}
foreach ($dependency in $dependencyPaths) {
    Copy-Item -LiteralPath $dependency -Destination $packageDirectory -Force
}

Copy-Item -LiteralPath (Join-Path $buildRoot "libsigrok\COPYING") `
    -Destination (Join-Path $packageDirectory "libsigrok-COPYING.txt")
Copy-Item -LiteralPath (Join-Path $buildRoot "sigrok-cli\COPYING") `
    -Destination (Join-Path $packageDirectory "sigrok-cli-COPYING.txt")

$versionOutput = (& (Join-Path $packageDirectory "sigrok-cli.exe") --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $versionOutput -notmatch [regex]::Escape($lock.runtime.libusb_version)) {
    throw "Packaged runtime failed validation or does not use libusb $($lock.runtime.libusb_version)."
}
foreach ($required in $lock.runtime.required_files) {
    if (-not (Test-Path -LiteralPath (Join-Path $packageDirectory $required))) {
        throw "Packaged runtime is missing $required"
    }
}

$manifest = [ordered]@{
    schema_version = 1
    built_at = (Get-Date).ToUniversalTime().ToString("o")
    platform = $lock.platform
    libsigrok_commit = $lock.sources.libsigrok.commit
    sigrok_cli_commit = $lock.sources.sigrok_cli.commit
    patch_sha256 = $lock.patch.sha256
    version_output = $versionOutput -split "`r?`n"
    files = @(
        Get-ChildItem -LiteralPath $packageDirectory -File | Sort-Object Name | ForEach-Object {
            [ordered]@{
                name = $_.Name
                size = $_.Length
                sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
            }
        }
    )
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content `
    -LiteralPath (Join-Path $packageDirectory "runtime-manifest.json") -Encoding UTF8

$replacement = "$outputFull.new"
$backup = "$outputFull.previous"
foreach ($path in @($replacement, $backup)) {
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Recurse -Force
    }
}
Move-Item -LiteralPath $packageDirectory -Destination $replacement
if (Test-Path -LiteralPath $outputFull) {
    Move-Item -LiteralPath $outputFull -Destination $backup
}
try {
    Move-Item -LiteralPath $replacement -Destination $outputFull
    & (Join-Path $outputFull "sigrok-cli.exe") --version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Installed runtime smoke test failed."
    }
    if (Test-Path -LiteralPath $backup) {
        Remove-Item -LiteralPath $backup -Recurse -Force
    }
}
catch {
    if (Test-Path -LiteralPath $outputFull) {
        Remove-Item -LiteralPath $outputFull -Recurse -Force
    }
    if (Test-Path -LiteralPath $backup) {
        Move-Item -LiteralPath $backup -Destination $outputFull
    }
    throw
}

Write-Host "Validated Kingst sigrok runtime installed: $outputFull"
Write-Host "Build record: $(Join-Path $outputFull 'runtime-manifest.json')"
if (-not $KeepBuildTree) {
    $buildParentFull = [System.IO.Path]::GetFullPath($buildRootParent).TrimEnd("\") + "\"
    $buildFull = [System.IO.Path]::GetFullPath($buildRoot)
    if (-not $buildFull.StartsWith($buildParentFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean an unexpected build directory: $buildFull"
    }
    Remove-Item -LiteralPath $buildFull -Recurse -Force
}
