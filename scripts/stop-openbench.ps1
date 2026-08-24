$ErrorActionPreference = "Stop"

function Get-OpenBenchProcessTree {
    param([int]$RootProcessId)

    $pending = [System.Collections.Generic.Queue[int]]::new()
    $pending.Enqueue($RootProcessId)
    $processes = @()
    while ($pending.Count -gt 0) {
        $processId = $pending.Dequeue()
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $processId"
        if ($null -eq $process) {
            continue
        }
        $processes += $process
        Get-CimInstance Win32_Process -Filter "ParentProcessId = $processId" |
            ForEach-Object { $pending.Enqueue([int]$_.ProcessId) }
    }
    return $processes
}

try {
    $projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $runtimeDirectory = Join-Path $projectRoot ".openbench"
    $pidPath = Join-Path $runtimeDirectory "server.pid"

    if (-not (Test-Path -LiteralPath $pidPath)) {
        Write-Host "OpenBench launcher has no running server to stop."
        return
    }

    $serverPid = [int](Get-Content -LiteralPath $pidPath -Raw).Trim()
    $processTree = @(Get-OpenBenchProcessTree -RootProcessId $serverPid)
    if ($processTree.Count -eq 0) {
        Remove-Item -LiteralPath $pidPath
        Write-Host "OpenBench was already stopped."
        return
    }

    $rootProcess = $processTree | Where-Object { $_.ProcessId -eq $serverPid }
    if ($rootProcess.CommandLine -notmatch "openbench\.cli.+serve") {
        throw "PID $serverPid does not belong to the OpenBench server; nothing was stopped."
    }

    # A hidden console host may also be a descendant. Stop only processes whose
    # command line is the recorded OpenBench serve command.
    $stopOrder = @(
        $processTree | Where-Object { $_.CommandLine -match "openbench\.cli.+serve" }
    )
    [array]::Reverse($stopOrder)
    foreach ($process in $stopOrder) {
        Stop-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
        Wait-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $pidPath
    Write-Host "OpenBench stopped."
}
catch {
    Write-Host "OpenBench could not be stopped." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
