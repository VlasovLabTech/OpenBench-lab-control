[CmdletBinding()]
param(
    [string]$DestinationRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$sourceRoot = Join-Path $projectRoot "skills\openbench"

if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot "SKILL.md"))) {
    throw "The tracked OpenBench skill is missing: $sourceRoot"
}

if ([string]::IsNullOrWhiteSpace($DestinationRoot)) {
    $codexProfileRoot = $env:CODEX_HOME
    if ([string]::IsNullOrWhiteSpace($codexProfileRoot)) {
        if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
            throw "Neither CODEX_HOME nor USERPROFILE is available. Pass -DestinationRoot explicitly."
        }
        $codexProfileRoot = Join-Path $env:USERPROFILE ".codex"
    }
    $DestinationRoot = Join-Path $codexProfileRoot "skills\openbench"
}

$destinationSkillRoot = [System.IO.Path]::GetFullPath($DestinationRoot)
New-Item -ItemType Directory -Path $destinationSkillRoot -Force | Out-Null

foreach ($sourceFile in Get-ChildItem -LiteralPath $sourceRoot -Recurse -File) {
    $relativePath = $sourceFile.FullName.Substring($sourceRoot.Length).TrimStart("\", "/")
    $destinationFile = Join-Path $destinationSkillRoot $relativePath
    $destinationDirectory = Split-Path -Parent $destinationFile
    New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
    Copy-Item -LiteralPath $sourceFile.FullName -Destination $destinationFile -Force
}

Write-Host "OpenBench Codex skill installed: $destinationSkillRoot"
Write-Host "Restart Codex if the skill was not already loaded in this session."
