param(
    [switch]$Full
)

$ErrorActionPreference = "Stop"

$gitleaksVersion = "8.30.1"
$gitleaksArchiveName = "gitleaks_${gitleaksVersion}_windows_x64.zip"
$gitleaksArchiveSha256 = "D29144DEFF3A68AA93CED33DDDF84B7FDC26070ADD4AA0F4513094C8332AFC4E"
$gitleaksUrl = "https://github.com/gitleaks/gitleaks/releases/download/v$gitleaksVersion/$gitleaksArchiveName"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$auditRoot = Join-Path $projectRoot ".openbench\audit\publication"
$toolDirectory = Join-Path $auditRoot "gitleaks-$gitleaksVersion"
$archivePath = Join-Path $toolDirectory $gitleaksArchiveName
$gitleaksPath = Join-Path $toolDirectory "gitleaks.exe"
$scanDirectory = Join-Path $auditRoot ("public-tree-" + [guid]::NewGuid().ToString("N"))

function Assert-ChildPath {
    param(
        [Parameter(Mandatory)]
        [string]$Parent,
        [Parameter(Mandatory)]
        [string]$Child
    )

    $parentPath = [IO.Path]::GetFullPath($Parent).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $childPath = [IO.Path]::GetFullPath($Child)
    $prefix = $parentPath + [IO.Path]::DirectorySeparatorChar
    if (-not $childPath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to use a path outside the audit directory: $childPath"
    }
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory)]
        [scriptblock]$Command,
        [Parameter(Mandatory)]
        [string]$FailureMessage
    )

    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw $FailureMessage
    }
}

New-Item -ItemType Directory -Path $toolDirectory -Force | Out-Null
New-Item -ItemType Directory -Path $auditRoot -Force | Out-Null

if (-not (Test-Path -LiteralPath $gitleaksPath)) {
    if (-not (Test-Path -LiteralPath $archivePath)) {
        Write-Host "Downloading Gitleaks $gitleaksVersion from the official release..."
        Invoke-WebRequest -Uri $gitleaksUrl -OutFile $archivePath
    }

    $actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash
    if ($actualHash -ne $gitleaksArchiveSha256) {
        throw "Gitleaks archive hash mismatch. Expected $gitleaksArchiveSha256, got $actualHash."
    }
    Expand-Archive -LiteralPath $archivePath -DestinationPath $toolDirectory -Force
}

Write-Host "Scanning the reachable HEAD history for secrets..."
$historyReport = Join-Path $auditRoot "gitleaks-history.json"
Invoke-Checked -FailureMessage "Gitleaks found a secret in HEAD history." -Command {
    & $gitleaksPath git $projectRoot --log-opts HEAD --redact `
        --report-format json --report-path $historyReport --no-banner
}

Assert-ChildPath -Parent $auditRoot -Child $scanDirectory
New-Item -ItemType Directory -Path $scanDirectory | Out-Null
try {
    Write-Host "Collecting tracked and non-ignored working-tree files..."
    $publicFiles = & git -C $projectRoot ls-files --cached --others --exclude-standard
    if ($LASTEXITCODE -ne 0) {
        throw "git ls-files failed."
    }
    foreach ($relativePath in $publicFiles) {
        $sourcePath = Join-Path $projectRoot $relativePath
        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
            continue
        }
        $destinationPath = Join-Path $scanDirectory $relativePath
        $destinationParent = Split-Path -Parent $destinationPath
        New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
        Copy-Item -LiteralPath $sourcePath -Destination $destinationPath
    }

    Write-Host "Scanning the public working tree for secrets..."
    $workingTreeReport = Join-Path $auditRoot "gitleaks-working-tree.json"
    Invoke-Checked -FailureMessage "Gitleaks found a secret in the public working tree." -Command {
        & $gitleaksPath dir $scanDirectory --redact --report-format json `
            --report-path $workingTreeReport --no-banner
    }
}
finally {
    Assert-ChildPath -Parent $auditRoot -Child $scanDirectory
    if (Test-Path -LiteralPath $scanDirectory) {
        Remove-Item -LiteralPath $scanDirectory -Recurse -Force
    }
}

$pytestPath = Join-Path $projectRoot ".venv\Scripts\pytest.exe"
if (-not (Test-Path -LiteralPath $pytestPath)) {
    throw "Development environment missing. Run scripts\setup-openbench.ps1 -Dev first."
}

Write-Host "Checking public paths and identity placeholders..."
Invoke-Checked -FailureMessage "Publication portability checks failed." -Command {
    & $pytestPath -q (Join-Path $projectRoot "tests\test_portability.py")
}

Write-Host "Checking patch whitespace..."
Invoke-Checked -FailureMessage "git diff --check failed." -Command {
    & git -C $projectRoot diff --check
}

if ($Full) {
    Write-Host "Running the full development verification..."
    Invoke-Checked -FailureMessage "Ruff failed." -Command {
        & (Join-Path $projectRoot ".venv\Scripts\ruff.exe") check $projectRoot
    }
    Invoke-Checked -FailureMessage "mypy failed." -Command {
        & (Join-Path $projectRoot ".venv\Scripts\mypy.exe") (Join-Path $projectRoot "src")
    }
    Invoke-Checked -FailureMessage "pytest failed." -Command {
        & $pytestPath -q
    }
}

Write-Host "Publication audit passed. Reports are in $auditRoot"
