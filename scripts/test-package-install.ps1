param(
    [int]$Port = 18117,
    [switch]$KeepArtifacts
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$auditRoot = Join-Path $projectRoot ".openbench\audit\package-smoke"
$smokeRoot = Join-Path $auditRoot ([guid]::NewGuid().ToString("N"))
$serverProcess = $null

function Assert-AuditChild {
    param([Parameter(Mandatory)][string]$Path)

    $auditPath = [IO.Path]::GetFullPath($auditRoot).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $candidate = [IO.Path]::GetFullPath($Path)
    if (-not $candidate.StartsWith(
        $auditPath + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to use a path outside the package audit directory: $candidate"
    }
}

Assert-AuditChild -Path $smokeRoot
New-Item -ItemType Directory -Path $smokeRoot -Force | Out-Null

try {
    $sourceDirectory = Join-Path $smokeRoot "source"
    $wheelDirectory = Join-Path $smokeRoot "wheel"
    New-Item -ItemType Directory -Path $sourceDirectory | Out-Null
    New-Item -ItemType Directory -Path $wheelDirectory | Out-Null

    $publicFiles = & git -C $projectRoot ls-files --cached --others --exclude-standard
    if ($LASTEXITCODE -ne 0) {
        throw "git ls-files failed."
    }
    foreach ($relativePath in $publicFiles) {
        $sourcePath = Join-Path $projectRoot $relativePath
        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
            continue
        }
        $destinationPath = Join-Path $sourceDirectory $relativePath
        New-Item -ItemType Directory -Path (Split-Path -Parent $destinationPath) `
            -Force | Out-Null
        Copy-Item -LiteralPath $sourcePath -Destination $destinationPath
    }

    & (Join-Path $projectRoot ".venv\Scripts\python.exe") -m pip wheel `
        --no-deps --disable-pip-version-check --wheel-dir $wheelDirectory $sourceDirectory
    if ($LASTEXITCODE -ne 0) {
        throw "Wheel build failed."
    }
    $wheel = Get-ChildItem -LiteralPath $wheelDirectory -Filter "*.whl" |
        Select-Object -First 1
    if ($null -eq $wheel) {
        throw "The built wheel was not found."
    }

    $cleanVenv = Join-Path $smokeRoot "venv"
    & (Join-Path $projectRoot ".venv\Scripts\python.exe") -m venv $cleanVenv
    if ($LASTEXITCODE -ne 0) {
        throw "Clean virtual environment creation failed."
    }
    $cleanPython = Join-Path $cleanVenv "Scripts\python.exe"
    & $cleanPython -m pip install --disable-pip-version-check $wheel.FullName
    if ($LASTEXITCODE -ne 0) {
        throw "Wheel installation failed."
    }
    & $cleanPython -c (
        "from importlib.resources import files; " +
        "import openbench; " +
        "assert files('openbench').joinpath('web/static/i18n.js').is_file()"
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Installed package or bilingual web assets could not be imported."
    }

    $serverProcess = Start-Process `
        -FilePath $cleanPython `
        -ArgumentList @("-m", "openbench.cli", "serve") `
        -WorkingDirectory $smokeRoot `
        -WindowStyle Hidden `
        -Environment @{
            OPENBENCH_PORT = [string]$Port
            OPENBENCH_AUTO_DISCOVER = "false"
        } `
        -RedirectStandardOutput (Join-Path $smokeRoot "server.log") `
        -RedirectStandardError (Join-Path $smokeRoot "server-error.log") `
        -PassThru

    $healthy = $false
    for ($attempt = 0; $attempt -lt 80; $attempt++) {
        try {
            $health = Invoke-RestMethod `
                -Uri "http://127.0.0.1:$Port/api/v1/health" `
                -TimeoutSec 1
            if ($health.status -eq "ok") {
                $healthy = $true
                break
            }
        }
        catch {
        }
        if ($serverProcess.HasExited) {
            break
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $healthy) {
        $errorLog = Join-Path $smokeRoot "server-error.log"
        $details = if (Test-Path -LiteralPath $errorLog) {
            Get-Content -LiteralPath $errorLog -Raw
        }
        else {
            ""
        }
        throw "Clean-package server health check failed. $details"
    }

    Write-Host "Package smoke test passed: $($wheel.Name)"
}
finally {
    if ($null -ne $serverProcess -and -not $serverProcess.HasExited) {
        Stop-Process -Id $serverProcess.Id -Force
    }
    if (-not $KeepArtifacts -and (Test-Path -LiteralPath $smokeRoot)) {
        Assert-AuditChild -Path $smokeRoot
        Remove-Item -LiteralPath $smokeRoot -Recurse -Force
    }
    elseif ($KeepArtifacts) {
        Write-Host "Package smoke-test artifacts kept at $smokeRoot"
    }
}
