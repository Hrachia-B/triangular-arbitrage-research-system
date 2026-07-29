[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$FeeCheckerArguments = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Windows virtual environment not found. Run .\scripts\setup_windows.ps1 first."
}

$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"
$exitCode = 1

Push-Location $RepoRoot
try {
    & $Python -u -m tri_arb.tools.check_mexc_fees @FeeCheckerArguments
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode
