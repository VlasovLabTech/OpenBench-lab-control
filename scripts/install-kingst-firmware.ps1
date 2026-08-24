[CmdletBinding()]
param(
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-Hash(
    [string]$Path,
    [string]$Expected,
    [string]$Label
) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "$Label is missing: $Path"
    }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    if ($actual -ne $Expected) {
        throw "$Label checksum mismatch. Expected $Expected, got $actual."
    }
}

function Invoke-CheckedDownload(
    [string]$Uri,
    [string]$Destination,
    [string]$ExpectedHash
) {
    if ((Test-Path -LiteralPath $Destination) -and -not $Force) {
        try {
            Assert-Hash $Destination $ExpectedHash "Cached download"
            return
        }
        catch {
            Write-Host "Cached file is invalid; downloading it again."
        }
    }
    $partial = "$Destination.partial"
    if (Test-Path -LiteralPath $partial) {
        Remove-Item -LiteralPath $partial -Force
    }
    Write-Host "Downloading $Uri"
    Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $partial
    Assert-Hash $partial $ExpectedHash "Downloaded file"
    Move-Item -LiteralPath $partial -Destination $Destination -Force
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$lockPath = Join-Path $PSScriptRoot "kingst-runtime.lock.json"
$lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
$cacheRoot = Join-Path $projectRoot ".openbench\cache\kingst-firmware"
$extractRoot = Join-Path $cacheRoot "vendor-extracted"
$toolRoot = Join-Path $cacheRoot "sigrok-util"
$generatedRoot = Join-Path $cacheRoot "generated"
$firmwareRoot = Join-Path $env:LOCALAPPDATA "sigrok-firmware"
$archiveName = Split-Path -Leaf $lock.vendor_firmware_source.url
$archivePath = Join-Path $cacheRoot $archiveName

$allInstalled = $true
foreach ($item in $lock.validated_firmware) {
    $target = Join-Path $firmwareRoot $item.name
    if (-not (Test-Path -LiteralPath $target)) {
        $allInstalled = $false
        break
    }
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $item.sha256) {
        $allInstalled = $false
        break
    }
}
if ($allInstalled -and -not $Force) {
    Write-Host "Validated Kingst LA2016 firmware is already installed: $firmwareRoot"
    return
}

New-Item -ItemType Directory -Path $cacheRoot,$extractRoot,$toolRoot,$generatedRoot -Force | Out-Null
Invoke-CheckedDownload `
    $lock.vendor_firmware_source.url `
    $archivePath `
    $lock.vendor_firmware_source.sha256

$rawRoot = "https://raw.githubusercontent.com/sigrokproject/sigrok-util/$($lock.sources.sigrok_util.commit)"
foreach ($file in $lock.extractor_files) {
    $relativePath = $file.path -replace "/", "\"
    $destination = Join-Path $toolRoot $relativePath
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Invoke-CheckedDownload "$rawRoot/$($file.path)" $destination $file.sha256
}

if (Test-Path -LiteralPath $extractRoot) {
    Remove-Item -LiteralPath $extractRoot -Recurse -Force
}
if (Test-Path -LiteralPath $generatedRoot) {
    Remove-Item -LiteralPath $generatedRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $extractRoot,$generatedRoot -Force | Out-Null

$vendorBinary = $lock.vendor_firmware_source.binary_path
& tar.exe -xf $archivePath -C $extractRoot $vendorBinary
if ($LASTEXITCODE -ne 0) {
    throw "Could not extract $vendorBinary from $archivePath"
}

$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        throw "Python 3 was not found. Run Setup OpenBench.cmd first."
    }
    $pythonPath = $pythonCommand.Source
}
$extractor = Join-Path $toolRoot "firmware\kingst-la\sigrok-fwextract-kingst-la2016"
$binaryPath = Join-Path $extractRoot ($vendorBinary -replace "/", "\")
Push-Location $generatedRoot
try {
    & $pythonPath $extractor $binaryPath
    if ($LASTEXITCODE -ne 0) {
        throw "sigrok Kingst firmware extraction failed"
    }
}
finally {
    Pop-Location
}

foreach ($item in $lock.validated_firmware) {
    $generated = Join-Path $generatedRoot $item.name
    Assert-Hash $generated $item.sha256 $item.name
    if ((Get-Item -LiteralPath $generated).Length -ne $item.size) {
        throw "$($item.name) size does not match the lock manifest."
    }
}

New-Item -ItemType Directory -Path $firmwareRoot -Force | Out-Null
Get-ChildItem -LiteralPath $generatedRoot -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $firmwareRoot $_.Name) -Force
}
Write-Host "Validated Kingst firmware installed: $firmwareRoot"
