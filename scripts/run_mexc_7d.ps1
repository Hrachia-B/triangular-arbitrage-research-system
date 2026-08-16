[CmdletBinding()]
param(
    [string]$DataDir = $env:TRI_ARB_DATA_DIR,
    [ValidateRange(0, 100000)]
    [int]$MinFreeGiB = 50
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($DataDir)) {
    $DataDir = Join-Path $RepoRoot "data"
}
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$FeeConfig = Join-Path $RepoRoot "configs\generated\mexc_account_fee.yaml"
$LatestReport = Join-Path $DataDir "reports\latest.md"
$LogsDirectory = Join-Path $DataDir "logs"
$ConsoleLog = Join-Path $LogsDirectory "mexc_7d_console.txt"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Windows virtual environment not found. Run .\scripts\setup_windows.ps1 first."
}
if (-not (Test-Path -LiteralPath $FeeConfig -PathType Leaf)) {
    throw "Run python -m tri_arb.tools.check_mexc_fees first."
}
if (-not (Test-Path -LiteralPath $LatestReport -PathType Leaf)) {
    throw "No $LatestReport exists. Complete the 48-hour run before considering a 7-day run."
}

$latestReportText = Get-Content -LiteralPath $LatestReport -Raw
$continueDecisionPattern = "(?im)^\s*(?:\**48H_DECISION\s*:\s*CONTINUE_TO_7D\**(?:\s|$)|\|\s*48H_DECISION\s*\|\s*CONTINUE_TO_7D\s*\|)"
if ($latestReportText -notmatch $continueDecisionPattern) {
    throw "The latest report does not say 48H_DECISION: CONTINUE_TO_7D. Do not start the 7-day run."
}

$driveRoot = [IO.Path]::GetPathRoot($DataDir)
$drive = [IO.DriveInfo]::new($driveRoot)
$freeGiB = [math]::Floor($drive.AvailableFreeSpace / 1GB)
if ($freeGiB -lt $MinFreeGiB) {
    throw "Data directory $DataDir uses drive $driveRoot, which has $freeGiB GiB free; the 7-day compact run requires $MinFreeGiB GiB."
}

New-Item -ItemType Directory -Path $LogsDirectory -Force | Out-Null
$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"
$exitCode = 1

Write-Warning "This run lasts about 7 days. Keep the external SSD connected, the laptop plugged in, and sleep disabled."
Write-Host "Console output will also be written to $ConsoleLog"

Push-Location $RepoRoot
$previousErrorActionPreference = $ErrorActionPreference
try {
    # Windows PowerShell 5.1 wraps native stderr as NativeCommandError. Python's
    # normal logging uses stderr, so allow it through the tee pipeline without
    # treating warnings as terminating PowerShell exceptions.
    $ErrorActionPreference = "Continue"
    & $Python -u -m tri_arb.main --exchange mexc --duration-minutes 10080 `
        --max-cycles 20 --use-account-fees --data-dir $DataDir `
        --storage-mode compact --min-free-gib $MinFreeGiB 2>&1 |
        ForEach-Object { "$_" } |
        Tee-Object -FilePath $ConsoleLog
    $exitCode = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $previousErrorActionPreference
    Pop-Location
}

exit $exitCode
