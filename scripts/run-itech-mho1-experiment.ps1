param(
    [string]$Title = "",
    [string]$Comment = "",
    [double]$SettleSeconds = 1.0
)

$ErrorActionPreference = "Stop"
$utf8Encoding = New-Object System.Text.UTF8Encoding $false
[Console]::InputEncoding = $utf8Encoding
[Console]::OutputEncoding = $utf8Encoding
$OutputEncoding = $utf8Encoding
$env:PYTHONUTF8 = "1"

try {
    $projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
    $experimentPath = Join-Path $PSScriptRoot "run_itech_scope_sink_steps.py"

    $confirmationWord = "RUN"
    $pointsText = "0, 2, 4, 7, 9, 11 A sink"
    $safetyText = "Verify the 12 V source, current limit, load wiring, and oscilloscope probes."

    $safetyText = "Oscilloscope: visually verify normal YT mode. $safetyText"

    Write-Host "ITECH + MHO1 stepped sink experiment" -ForegroundColor Cyan
    Write-Host "Points: $pointsText"
    Write-Host "Sequence: set current -> wait $SettleSeconds s -> parallel ITECH + MHO1 capture"
    Write-Host "The ITECH output will be enabled and will be forced OFF when the run finishes."
    Write-Host "Safety: missing ITECH U/I aborts immediately; OFF is read back and COM rediscovery retries the same device if needed."
    Write-Host $safetyText
    Write-Host ""

    $titlePrompt = if ([string]::IsNullOrWhiteSpace($Title)) {
        "Title [press Enter for saved default]"
    }
    else {
        "Title [$Title]"
    }
    $enteredTitle = Read-Host $titlePrompt
    if (-not [string]::IsNullOrWhiteSpace($enteredTitle)) {
        $Title = $enteredTitle
    }
    $commentPrompt = "Comment [press Enter for saved default]"
    $enteredComment = Read-Host $commentPrompt
    if (-not [string]::IsNullOrWhiteSpace($enteredComment)) {
        $Comment = $enteredComment
    }
    & (Join-Path $PSScriptRoot "start-openbench.ps1") -NoBrowser
    try {
        $health = Invoke-RestMethod `
            -Uri "http://127.0.0.1:8000/api/v1/health" `
            -TimeoutSec 2
    }
    catch {
        throw "OpenBench did not start."
    }
    if ($health.status -ne "ok") {
        throw "OpenBench health check did not return OK."
    }
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        throw "OpenBench Python environment is unavailable: $pythonPath"
    }

    $settleText = $SettleSeconds.ToString([System.Globalization.CultureInfo]::InvariantCulture)
    $experimentArguments = @(
        $experimentPath,
        "--settle-s", $settleText
    )
    if (-not [string]::IsNullOrWhiteSpace($Title)) {
        $experimentArguments += @("--title", $Title)
    }
    if (-not [string]::IsNullOrWhiteSpace($Comment)) {
        $experimentArguments += @("--comment", $Comment)
    }

    $executionArguments = @($experimentArguments)
    $executionArguments += @(
        "--execute",
        "--wiring-confirmed",
        "--scope-yt-confirmed",
        "--operator-confirmation-phrase", $confirmationWord,
        "--open-result-folder"
    )
    Write-Host ""
    Write-Host "Checking instruments and experiment plan; no output will be enabled before the exact confirmation..." -ForegroundColor Cyan
    & $pythonPath @executionArguments
    if ($LASTEXITCODE -eq 3) {
        Write-Host "Experiment cancelled. No output was enabled." -ForegroundColor Yellow
        exit 0
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Experiment failed with exit code $LASTEXITCODE."
    }

    Write-Host "Experiment completed. The result folder has been opened." -ForegroundColor Green
}
catch {
    Write-Host "Experiment could not be completed." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
