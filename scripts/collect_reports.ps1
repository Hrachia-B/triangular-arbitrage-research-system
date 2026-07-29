[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ReportsDirectory = Join-Path $RepoRoot "data\reports"
$ExportDirectory = Join-Path $ReportsDirectory "export"

if (-not (Test-Path -LiteralPath $ReportsDirectory -PathType Container)) {
    throw "No data\reports directory exists. Run a simulation first."
}

New-Item -ItemType Directory -Path $ExportDirectory -Force | Out-Null
$selectedFiles = New-Object "System.Collections.Generic.List[System.IO.FileInfo]"

foreach ($fixedName in @("latest.md", "latest_summary.csv")) {
    $fixedPath = Join-Path $ReportsDirectory $fixedName
    if (Test-Path -LiteralPath $fixedPath -PathType Leaf) {
        $selectedFiles.Add((Get-Item -LiteralPath $fixedPath))
    }
}

foreach ($pattern in @("report_*.md", "checkpoint_*.md", "summary_*.csv", "top_opportunities_*.csv")) {
    $candidate = Get-ChildItem -Path (Join-Path $ReportsDirectory $pattern) -File |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1
    if ($null -ne $candidate) {
        $alreadySelected = $selectedFiles | Where-Object { $_.FullName -eq $candidate.FullName }
        if ($null -eq $alreadySelected) {
            $selectedFiles.Add($candidate)
        }
    }
}

if ($selectedFiles.Count -eq 0) {
    throw "No Markdown or CSV report artifacts were found."
}

foreach ($source in $selectedFiles) {
    Copy-Item -LiteralPath $source.FullName -Destination $ExportDirectory -Force
    Write-Host "Copied $($source.Name)"
}

Write-Host "Report export is ready at $ExportDirectory"
