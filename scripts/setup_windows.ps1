[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvDirectory = Join-Path $RepoRoot ".venv"
$VenvPython = Join-Path $VenvDirectory "Scripts\python.exe"
$EnvFile = Join-Path $RepoRoot ".env"

function Get-PreferredPython {
    $candidates = @(
        [pscustomobject]@{ Executable = "py.exe"; PrefixArguments = @("-3.12") },
        [pscustomobject]@{ Executable = "py.exe"; PrefixArguments = @("-3.11") },
        [pscustomobject]@{ Executable = "python.exe"; PrefixArguments = @() }
    )

    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.Executable -ErrorAction SilentlyContinue)) {
            continue
        }

        $candidateExecutable = [string]$candidate.Executable
        $prefixArguments = @($candidate.PrefixArguments)
        $version = & $candidateExecutable @prefixArguments -c "import platform; print(platform.python_version())" 2>$null
        if ($LASTEXITCODE -eq 0 -and $version -match "^3\.(11|12)\.") {
            return $candidate
        }
    }

    throw "Python 3.11 or 3.12 was not found. Install 64-bit Python 3.12, reopen PowerShell, and rerun this script."
}

function Read-RequiredSecret {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Prompt
    )

    $secureValue = Read-Host -Prompt $Prompt -AsSecureString
    $pointer = [IntPtr]::Zero
    try {
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
        $plainValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        if ([string]::IsNullOrWhiteSpace($plainValue)) {
            throw "$Prompt cannot be empty."
        }
        if ($plainValue.Contains("`r") -or $plainValue.Contains("`n")) {
            throw "$Prompt cannot contain a line break."
        }
        return $plainValue
    }
    finally {
        if ($pointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
    }
}

Push-Location $RepoRoot
try {
    $selectedPython = Get-PreferredPython
    $basePython = $selectedPython.Executable
    $baseArguments = @($selectedPython.PrefixArguments)
    $baseVersion = & $basePython @baseArguments -c "import platform; print(platform.python_version())"
    if ($LASTEXITCODE -ne 0) {
        throw "Python version verification failed."
    }
    Write-Host "Using Python $baseVersion."

    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        if (Test-Path -LiteralPath $VenvDirectory) {
            throw "An incomplete or non-Windows .venv already exists. Move it out of the repository, then rerun setup_windows.ps1."
        }
        Write-Host "Creating .venv..."
        & $basePython @baseArguments -m venv $VenvDirectory
        if ($LASTEXITCODE -ne 0) {
            throw "Could not create .venv."
        }
    }

    # Activation is local to this setup process. Every other script also calls
    # .venv\Scripts\python.exe explicitly, so it cannot use the wrong Python.
    . (Join-Path $VenvDirectory "Scripts\Activate.ps1")

    & $VenvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "pip upgrade failed."
    }

    & $VenvPython -m pip install -r (Join-Path $RepoRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed."
    }

    $requiredDirectories = @(
        "data\raw",
        "data\signals",
        "data\snapshots",
        "data\logs",
        "data\account",
        "data\reports",
        "data\reports\export",
        "configs\generated"
    )
    foreach ($relativeDirectory in $requiredDirectories) {
        New-Item -ItemType Directory -Path (Join-Path $RepoRoot $relativeDirectory) -Force | Out-Null
    }

    Write-Host "Running the offline test suite..."
    & $VenvPython -m pytest
    if ($LASTEXITCODE -ne 0) {
        throw "pytest failed. Fix the test failure before running a monitoring experiment."
    }

    if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
        Write-Host ""
        Write-Host "A local .env file is required for the read-only MEXC fee check."
        Write-Host "Input is hidden. Use a key with trading, transfers, and withdrawals disabled."

        $apiKey = $null
        $apiSecret = $null
        try {
            $apiKey = Read-RequiredSecret "MEXC API key"
            $apiSecret = Read-RequiredSecret "MEXC API secret"
            $lineEnding = [Environment]::NewLine
            $contents = "MEXC_API_KEY=$apiKey${lineEnding}MEXC_API_SECRET=$apiSecret${lineEnding}"
            $utf8WithoutBom = New-Object System.Text.UTF8Encoding -ArgumentList $false
            [IO.File]::WriteAllText($EnvFile, $contents, $utf8WithoutBom)
        }
        finally {
            $apiKey = $null
            $apiSecret = $null
        }

        if (Get-Command "icacls.exe" -ErrorAction SilentlyContinue) {
            $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
            $null = & icacls.exe $EnvFile /inheritance:r /grant:r "${identity}:(F)" "SYSTEM:(F)"
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "Could not restrict the .env ACL automatically. Restrict this file to your Windows account before continuing."
            }
        }

        Write-Host "Created local .env without displaying its values."
    }
    else {
        Write-Host "Existing local .env found; it was not read or displayed by setup."
    }

    if ((Test-Path -LiteralPath (Join-Path $RepoRoot ".git")) -and (Get-Command "git.exe" -ErrorAction SilentlyContinue)) {
        $null = & git.exe check-ignore -q -- ".env"
        if ($LASTEXITCODE -ne 0) {
            throw ".env is not ignored by Git. Do not continue until .gitignore is fixed."
        }
    }

    Write-Host ""
    Write-Host "Windows setup completed successfully."
    Write-Host "Next: .\scripts\check_mexc_fees.ps1"
}
finally {
    Pop-Location
}
