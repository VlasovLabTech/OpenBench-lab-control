param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

function Test-OpenBenchReady {
    try {
        $health = Invoke-RestMethod `
            -Uri "http://127.0.0.1:8000/api/v1/health" `
            -TimeoutSec 1
        return $health.status -eq "ok"
    }
    catch {
        return $false
    }
}

try {
    $projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $runtimeDirectory = Join-Path $projectRoot ".openbench"
    $pidPath = Join-Path $runtimeDirectory "server.pid"
    $stdoutPath = Join-Path $runtimeDirectory "server.log"
    $stderrPath = Join-Path $runtimeDirectory "server-error.log"
    $dashboardUrl = "http://127.0.0.1:8000/"

    if (Test-OpenBenchReady) {
        if (-not $NoBrowser) {
            Start-Process $dashboardUrl
        }
        Write-Host "OpenBench is already running: $dashboardUrl"
        return
    }

    New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null

    $venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
    $runtimeLock = Join-Path $projectRoot "requirements\windows-runtime.lock"
    $setupState = Join-Path $runtimeDirectory "setup-state.json"
    $environmentReady = Test-Path -LiteralPath $venvPython
    if ($environmentReady -and (Test-Path -LiteralPath $runtimeLock)) {
        try {
            $state = Get-Content -LiteralPath $setupState -Raw | ConvertFrom-Json
            $stateLock = Join-Path $projectRoot "requirements\$($state.lock_name)"
            $expectedHash = (Get-FileHash -LiteralPath $stateLock -Algorithm SHA256).Hash
            $environmentReady = (
                $state.lock_name -in @("windows-runtime.lock", "windows-dev.lock") -and
                $state.lock_sha256 -eq $expectedHash
            )
            if ($environmentReady) {
                & $venvPython -c "import openbench, bleak, hid, serial" 2>$null
                $environmentReady = $LASTEXITCODE -eq 0
            }
        }
        catch {
            $environmentReady = $false
        }
    }
    if (-not $environmentReady) {
        Write-Host "Preparing the OpenBench environment (first run or dependency update)..."
        & (Join-Path $PSScriptRoot "setup-openbench.ps1")
        if ($LASTEXITCODE -ne 0) {
            throw "OpenBench environment setup failed."
        }
    }
    $pythonPath = $venvPython

    $previousPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = Join-Path $projectRoot "src"
    try {
        $serverProcess = Start-Process `
            -FilePath $pythonPath `
            -ArgumentList @("-m", "openbench.cli", "serve") `
            -WorkingDirectory $projectRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -PassThru
    }
    finally {
        $env:PYTHONPATH = $previousPythonPath
    }

    Set-Content -LiteralPath $pidPath -Value $serverProcess.Id -Encoding Ascii

    $ready = $false
    for ($attempt = 0; $attempt -lt 80; $attempt++) {
        if (Test-OpenBenchReady) {
            $ready = $true
            break
        }
        if ($serverProcess.HasExited) {
            break
        }
        Start-Sleep -Milliseconds 250
    }

    if (-not $ready) {
        $details = ""
        if (Test-Path -LiteralPath $stderrPath) {
            $details = (Get-Content -LiteralPath $stderrPath -Raw).Trim()
        }
        throw "The server did not become ready. See $stderrPath`n$details"
    }

    if (-not $NoBrowser) {
        Start-Process $dashboardUrl
    }
    Write-Host "OpenBench started: $dashboardUrl"
}
catch {
    Write-Host "OpenBench could not start." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
