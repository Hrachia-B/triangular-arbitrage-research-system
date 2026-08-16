[CmdletBinding()]
param(
    [string]$DataDir = $env:TRI_ARB_DATA_DIR,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$FeeCheckerArguments = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($DataDir)) {
    $DataDir = Join-Path $RepoRoot "data"
}
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$FeeConfig = Join-Path $RepoRoot "configs\generated\mexc_account_fee.yaml"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Windows virtual environment not found. Run .\scripts\setup_windows.ps1 first."
}

$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"
$exitCode = 1

Push-Location $RepoRoot
try {
    $rawAccountDir = Join-Path $DataDir "account"
    New-Item -ItemType Directory -Path $rawAccountDir -Force | Out-Null
    if ($FeeCheckerArguments.Count -eq 0) {
        & $Python -u -m tri_arb.main --exchange mexc --discover-only `
            --data-dir $DataDir --storage-mode compact --min-free-gib 0
        if ($LASTEXITCODE -ne 0) {
            throw "MEXC public discovery failed before the bounded fee check."
        }
        $selection = Get-ChildItem -Path (Join-Path $DataDir "raw\selected_symbols_*.json") -File |
            Sort-Object LastWriteTimeUtc -Descending |
            Select-Object -First 1 -ExpandProperty FullName
        if ([string]::IsNullOrWhiteSpace($selection)) {
            throw "MEXC discovery did not produce a selected-symbols file in $DataDir\raw."
        }
        $FeeCheckerArguments = @("--discovery-selection", $selection)
    }
    & $Python -u -m tri_arb.tools.check_mexc_fees `
        --raw-output-dir $rawAccountDir `
        --config-output $FeeConfig `
        @FeeCheckerArguments
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode
