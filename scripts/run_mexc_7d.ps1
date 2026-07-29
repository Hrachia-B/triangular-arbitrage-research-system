[CmdletBinding()]
param(
    [ValidateRange(500, 100000)]
    [int]$MinimumFreeGiB = 500
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$FeeConfig = Join-Path $RepoRoot "configs\generated\mexc_account_fee.yaml"
$LatestReport = Join-Path $RepoRoot "data\reports\latest.md"
$LogsDirectory = Join-Path $RepoRoot "data\logs"
$ConsoleLog = Join-Path $LogsDirectory "mexc_7d_console.txt"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Windows virtual environment not found. Run .\scripts\setup_windows.ps1 first."
}
if (-not (Test-Path -LiteralPath $FeeConfig -PathType Leaf)) {
    throw "Run python -m tri_arb.tools.check_mexc_fees first."
}
if (-not (Test-Path -LiteralPath $LatestReport -PathType Leaf)) {
    throw "No data\reports\latest.md exists. Complete the 48-hour run before considering a 7-day run."
}

$latestReportText = Get-Content -LiteralPath $LatestReport -Raw
$continueDecisionPattern = "(?im)^\s*(?:\**48H_DECISION\s*:\s*CONTINUE_TO_7D\**(?:\s|—|$)|\|\s*48H_DECISION\s*\|\s*CONTINUE_TO_7D\s*\|)"
if ($latestReportText -notmatch $continueDecisionPattern) {
    throw "The latest report does not say 48H_DECISION: CONTINUE_TO_7D. Do not start the 7-day run."
}

$driveRoot = [IO.Path]::GetPathRoot($RepoRoot)
$drive = [IO.DriveInfo]::new($driveRoot)
$freeGiB = [math]::Floor($drive.AvailableFreeSpace / 1GB)
if ($freeGiB -lt $MinimumFreeGiB) {
    Write-Warning "Only $freeGiB GiB is free on $driveRoot. Long-run JSONL output is large."
    throw "The 7-day run requires at least $MinimumFreeGiB GiB free. Free disk space before continuing."
}

New-Item -ItemType Directory -Path $LogsDirectory -Force | Out-Null
$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"
$exitCode = 1

Write-Warning "This run lasts about 7 days and can consume hundreds of GiB. Keep the laptop plugged in and sleep disabled."
Write-Host "Console output will also be written to $ConsoleLog"

Push-Location $RepoRoot
try {
    & $Python -u -m tri_arb.main --exchange mexc --duration-minutes 10080 --max-cycles 20 --use-account-fees 2>&1 |
        Tee-Object -FilePath $ConsoleLog
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode
