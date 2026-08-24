param(
    [Parameter(Mandatory = $true)]
    [string]$DeviceId,
    [string]$BaseUrl = "http://127.0.0.1:8000/api/v1"
)

$ErrorActionPreference = "Stop"
$scopeUrl = "$BaseUrl/oscilloscopes/$DeviceId"
$timer = [System.Diagnostics.Stopwatch]::StartNew()

$listing = (Invoke-WebRequest `
    -UseBasicParsing `
    -Uri "$scopeUrl/storage-index?path=%2Ffiles%2Fbinwave").Content | ConvertFrom-Json
$scopePaths = [string[]]@($listing | Where-Object { $_ -match '\.bin$' })

if ($scopePaths.Count -eq 0) {
    throw "No BIN files found on the oscilloscope."
}

$body = @{ scope_paths = $scopePaths } | ConvertTo-Json -Compress
$result = Invoke-RestMethod `
    -Method Post `
    -Uri "$scopeUrl/storage-waveforms/import" `
    -ContentType "application/json" `
    -Body $body

$timer.Stop()
$destination = Join-Path (Split-Path $PSScriptRoot) `
    ".openbench\data\captures\sessions\scope-waveforms\$DeviceId"

$result.files | ForEach-Object {
    Write-Output ("{0}  {1} bytes" -f (Join-Path $destination $_.filename), $_.bytes)
}
Write-Output ("Copied {0} file(s), {1} bytes total, in {2:N3} s" -f `
    $result.files.Count, (($result.files | Measure-Object bytes -Sum).Sum), $timer.Elapsed.TotalSeconds)
