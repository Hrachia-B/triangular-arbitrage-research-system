[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$FeeConfig = Join-Path $RepoRoot "configs\generated\mexc_account_fee.yaml"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Windows virtual environment not found. Run .\scripts\setup_windows.ps1 first."
}
if (-not (Test-Path -LiteralPath $FeeConfig -PathType Leaf)) {
    throw "Run python -m tri_arb.tools.check_mexc_fees first."
}

$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"
$exitCode = 1

Push-Location $RepoRoot
try {
    & $Python -u -m tri_arb.main --exchange mexc --duration-minutes 10 --max-cycles 20 --use-account-fees
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode
