[CmdletBinding()]
param(
    [switch]$Dev,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-BootstrapPython {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $launcher) {
        $candidate = (& $launcher.Source -3 -c "import sys; print(sys.executable)").Trim()
        if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }

    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -eq $python) {
        throw "Python 3.11+ was not found. Install Python, then run Setup OpenBench.cmd again."
    }
    return $python.Source
}

function Assert-SupportedPython([string]$PythonPath) {
    $versionJson = & $PythonPath -c `
        "import json,sys; print(json.dumps({'major':sys.version_info.major,'minor':sys.version_info.minor,'text':sys.version.split()[0]}))"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not run Python at $PythonPath"
    }
    $version = $versionJson | ConvertFrom-Json
    if ($version.major -ne 3 -or $version.minor -lt 11) {
        throw "OpenBench requires Python 3.11 or newer; found $($version.text)."
    }
    return $version.text
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimeDirectory = Join-Path $projectRoot ".openbench"
$venvDirectory = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venvDirectory "Scripts\python.exe"
$lockName = if ($Dev) { "windows-dev.lock" } else { "windows-runtime.lock" }
$lockPath = Join-Path $projectRoot "requirements\$lockName"
$statePath = Join-Path $runtimeDirectory "setup-state.json"

if (-not (Test-Path -LiteralPath $lockPath)) {
    throw "Dependency lock file is missing: $lockPath"
}

New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null

if (-not (Test-Path -LiteralPath $venvPython)) {
    $bootstrapPython = Get-BootstrapPython
    $bootstrapVersion = Assert-SupportedPython $bootstrapPython
    Write-Host "Creating .venv with Python $bootstrapVersion..."
    & $bootstrapPython -m venv $venvDirectory
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) {
        throw "Python could not create $venvDirectory"
    }
}

$venvVersion = Assert-SupportedPython $venvPython
$lockHash = (Get-FileHash -LiteralPath $lockPath -Algorithm SHA256).Hash
$stateCurrent = $false
if (-not $Force -and (Test-Path -LiteralPath $statePath)) {
    try {
        $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        $stateCurrent = (
            $state.lock_name -eq $lockName -and
            $state.lock_sha256 -eq $lockHash -and
            $state.python_version -eq $venvVersion
        )
        if ($stateCurrent) {
            & $venvPython -c "import openbench, bleak, hid, serial"
            $stateCurrent = $LASTEXITCODE -eq 0
            if ($stateCurrent) {
                & $venvPython -m pip check --disable-pip-version-check | Out-Null
                $stateCurrent = $LASTEXITCODE -eq 0
            }
        }
    }
    catch {
        $stateCurrent = $false
    }
}

if ($stateCurrent) {
    Write-Host "OpenBench environment is already current ($lockName)."
    return
}

Write-Host "Installing the tested dependency set from $lockName..."
& $venvPython -m pip install --disable-pip-version-check --upgrade "pip==26.1.2"
if ($LASTEXITCODE -ne 0) {
    throw "pip bootstrap failed"
}
& $venvPython -m pip install --disable-pip-version-check --requirement $lockPath
if ($LASTEXITCODE -ne 0) {
    throw "OpenBench dependency installation failed"
}
& $venvPython -m pip install --disable-pip-version-check --no-deps --editable $projectRoot
if ($LASTEXITCODE -ne 0) {
    throw "OpenBench editable installation failed"
}
& $venvPython -c "import openbench, bleak, hid, serial"
if ($LASTEXITCODE -ne 0) {
    throw "OpenBench import smoke test failed"
}
& $venvPython -m pip check --disable-pip-version-check
if ($LASTEXITCODE -ne 0) {
    throw "OpenBench dependency consistency check failed"
}

$setupState = [ordered]@{
    schema_version = 1
    created_at = (Get-Date).ToUniversalTime().ToString("o")
    python_version = $venvVersion
    lock_name = $lockName
    lock_sha256 = $lockHash
    project_root = $projectRoot
}
$setupState | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
Write-Host "OpenBench environment is ready: $venvPython"
