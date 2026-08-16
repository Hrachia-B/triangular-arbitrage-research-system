[CmdletBinding()]
param(
    [string]$DataDir = $env:TRI_ARB_DATA_DIR,
    [int]$MinFreeGiB = 5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($DataDir)) {
    $DataDir = Join-Path $RepoRoot "data"
}
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Windows virtual environment not found. Run .\scripts\setup_windows.ps1 first."
}

$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"
$exitCode = 1

Push-Location $RepoRoot
try {
    & $Python -u -m tri_arb.main --exchange mexc --discover-only `
        --data-dir $DataDir --storage-mode compact --min-free-gib $MinFreeGiB
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode
