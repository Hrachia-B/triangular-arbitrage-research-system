[CmdletBinding()]
param(
    [string]$DataDir = $env:TRI_ARB_DATA_DIR
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($DataDir)) {
    throw "Pass -DataDir with an absolute path or set TRI_ARB_DATA_DIR."
}

$dataRoot = [IO.Path]::GetPathRoot($DataDir)
if ([string]::IsNullOrWhiteSpace($dataRoot)) {
    throw "Data directory must be an absolute path: $DataDir"
}
if (-not (Test-Path -LiteralPath $dataRoot -PathType Container)) {
    throw "The data drive is not available: $dataRoot. Connect the external SSD and retry."
}

New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
foreach ($name in @("raw", "signals", "snapshots", "logs", "reports", "account", "exports")) {
    New-Item -ItemType Directory -Path (Join-Path $DataDir $name) -Force | Out-Null
}

$probe = Join-Path $DataDir ".tri_arb_write_test_$PID"
try {
    [IO.File]::WriteAllText($probe, "write-test")
}
catch {
    throw "Data directory is not writable: $DataDir. $($_.Exception.Message)"
}
finally {
    Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
}

$drive = [IO.DriveInfo]::new($dataRoot)
$freeGiB = [math]::Floor($drive.AvailableFreeSpace / 1GB)
Write-Host "External data directory is ready: $DataDir"
Write-Host "Available free space on $dataRoot`: $freeGiB GiB"
Write-Warning "Do not disconnect the SSD during monitoring."
Write-Warning "Keep the laptop plugged in and disable sleep mode."
Write-Warning "Avoid USB hubs if possible; connect the SSD directly."
