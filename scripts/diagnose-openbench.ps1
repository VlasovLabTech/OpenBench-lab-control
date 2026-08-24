[CmdletBinding()]
param(
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function New-Check(
    [string]$Name,
    [string]$Status,
    [string]$Detail
) {
    return [ordered]@{
        name = $Name
        status = $Status
        detail = $Detail
    }
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$runtimeLockPath = Join-Path $projectRoot "requirements\windows-runtime.lock"
$setupStatePath = Join-Path $projectRoot ".openbench\setup-state.json"
$kingstLockPath = Join-Path $PSScriptRoot "kingst-runtime.lock.json"
$kingstLock = Get-Content -LiteralPath $kingstLockPath -Raw | ConvertFrom-Json
$checks = [System.Collections.Generic.List[object]]::new()

if ($env:OS -eq "Windows_NT") {
    $checks.Add((New-Check "operating_system" "ok" ([System.Environment]::OSVersion.VersionString)))
}
else {
    $checks.Add((New-Check "operating_system" "info" "Non-Windows host; use the CLI installation path."))
}

if (Test-Path -LiteralPath $venvPython) {
    $version = (& $venvPython --version 2>&1 | Out-String).Trim()
    & $venvPython -c "import openbench, bleak, hid, serial" 2>$null
    if ($LASTEXITCODE -eq 0) {
        & $venvPython -m pip check --disable-pip-version-check 2>$null | Out-Null
        $pipStatus = if ($LASTEXITCODE -eq 0) { "ok" } else { "error" }
        $checks.Add((New-Check "python_environment" $pipStatus $version))
    }
    else {
        $checks.Add((New-Check "python_environment" "error" "The .venv import smoke test failed."))
    }
}
else {
    $checks.Add((New-Check "python_environment" "error" "Run Setup OpenBench.cmd."))
}

if ((Test-Path -LiteralPath $runtimeLockPath) -and (Test-Path -LiteralPath $setupStatePath)) {
    try {
        $state = Get-Content -LiteralPath $setupStatePath -Raw | ConvertFrom-Json
        $stateLockPath = Join-Path $projectRoot "requirements\$($state.lock_name)"
        $expected = (Get-FileHash -LiteralPath $stateLockPath -Algorithm SHA256).Hash
        if (
            $state.lock_name -in @("windows-runtime.lock", "windows-dev.lock") -and
            $state.lock_sha256 -eq $expected
        ) {
            $checks.Add((New-Check "dependency_lock" "ok" $expected))
        }
        else {
            $checks.Add((New-Check "dependency_lock" "warning" "Environment differs from windows-runtime.lock."))
        }
    }
    catch {
        $checks.Add((New-Check "dependency_lock" "warning" "setup-state.json is unreadable."))
    }
}
else {
    $checks.Add((New-Check "dependency_lock" "warning" "No completed locked setup was recorded."))
}

try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/health" -TimeoutSec 2
    $checks.Add((New-Check "server" "ok" "status=$($health.status), safety=$($health.safety_state)"))
}
catch {
    $checks.Add((New-Check "server" "info" "Server is not running."))
}

$sigrokPath = Join-Path $projectRoot ".openbench\tools\sigrok-modern\sigrok-cli.exe"
if (Test-Path -LiteralPath $sigrokPath) {
    $versionText = (& $sigrokPath --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -eq 0 -and $versionText -match [regex]::Escape($kingstLock.runtime.libusb_version)) {
        $checks.Add((New-Check "kingst_runtime" "ok" ($versionText -replace "`r?`n", "; ")))
    }
    else {
        $checks.Add((New-Check "kingst_runtime" "warning" "Runtime exists but its version is unexpected."))
    }
    foreach ($required in $kingstLock.runtime.required_files) {
        if (-not (Test-Path -LiteralPath (Join-Path (Split-Path $sigrokPath) $required))) {
            $checks.Add((New-Check "kingst_runtime_file" "error" "Missing $required"))
        }
    }
    $manifestPath = Join-Path (Split-Path $sigrokPath) "runtime-manifest.json"
    if (Test-Path -LiteralPath $manifestPath) {
        try {
            $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
            $manifestValid = (
                $manifest.libsigrok_commit -eq $kingstLock.sources.libsigrok.commit -and
                $manifest.sigrok_cli_commit -eq $kingstLock.sources.sigrok_cli.commit -and
                $manifest.patch_sha256 -eq $kingstLock.patch.sha256
            )
            foreach ($file in $manifest.files) {
                $filePath = Join-Path (Split-Path $sigrokPath) $file.name
                if (
                    -not (Test-Path -LiteralPath $filePath) -or
                    (Get-FileHash -LiteralPath $filePath -Algorithm SHA256).Hash -ne $file.sha256
                ) {
                    $manifestValid = $false
                    break
                }
            }
            $manifestStatus = if ($manifestValid) { "ok" } else { "error" }
            $checks.Add((New-Check "kingst_runtime_manifest" $manifestStatus $manifestPath))
        }
        catch {
            $checks.Add((New-Check "kingst_runtime_manifest" "error" "Manifest is unreadable."))
        }
    }
    else {
        $checks.Add((New-Check "kingst_runtime_manifest" "warning" "Rebuild the runtime to create its manifest."))
    }
}
else {
    $checks.Add((New-Check "kingst_runtime" "info" "Optional runtime is not built."))
}

$firmwareRoot = Join-Path $env:LOCALAPPDATA "sigrok-firmware"
foreach ($item in $kingstLock.validated_firmware) {
    $path = Join-Path $firmwareRoot $item.name
    if (-not (Test-Path -LiteralPath $path)) {
        $checks.Add((New-Check "kingst_firmware" "info" "Missing $($item.name); run Install Kingst Firmware.cmd."))
        continue
    }
    $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
    $status = if ($actual -eq $item.sha256) { "ok" } else { "error" }
    $checks.Add((New-Check "kingst_firmware" $status "$($item.name): $actual"))
}

$overall = if ($checks.status -contains "error") { "error" } elseif ($checks.status -contains "warning") { "warning" } else { "ok" }
$report = [ordered]@{
    schema_version = 1
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    project_root = $projectRoot
    overall = $overall
    checks = $checks
}

if ($Json) {
    $report | ConvertTo-Json -Depth 6
}
else {
    Write-Host "OpenBench diagnostics: $overall"
    foreach ($check in $checks) {
        Write-Host ("[{0}] {1}: {2}" -f $check.status.ToUpperInvariant(), $check.name, $check.detail)
    }
}

if ($overall -eq "error") {
    exit 1
}
