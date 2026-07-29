# Environment versions

Versions below were recorded on 2026-07-29 before the Windows transfer.

## Tested Mac environment

- Operating system: macOS 26.5.2 (build 25F84)
- Architecture: Apple Silicon (`arm64`)
- Python: 3.14.3
- pip: 26.0
- Git: 2.53.0

Installed direct runtime dependencies:

| Package | Tested version | Repository constraint |
|---|---:|---|
| aiohttp | 3.14.2 | `>=3.10,<4` |
| PyYAML | 6.0.3 | `>=6,<7` |
| protobuf | 6.33.6 | `>=5,<7` |
| websockets | 16.1.1 | `>=14,<17` |

Installed verification dependencies:

| Package | Tested version | Repository constraint |
|---|---:|---|
| pytest | 9.1.1 | `>=8,<10` |
| pytest-asyncio | 1.4.0 | `>=0.24,<2` |
| ruff | 0.15.22 | `>=0.9,<1` |

The Mac environment demonstrates compatibility but is not copied to Windows.
The repository targets Python 3.11 language features, and the Windows setup
creates a fresh environment.

## Windows target environment

- Target operating system: 64-bit Windows 10 or Windows 11
- Recommended Python: latest available Python 3.12 patch release
- Supported setup alternative: latest available Python 3.11 patch release
- PowerShell: Windows PowerShell 5.1 or PowerShell 7
- Git: current Git for Windows with Git Credential Manager

The Windows target had not yet been executed when this file was recorded.
Successful completion of `scripts\setup_windows.ps1` and its pytest run is the
acceptance check for the laptop.

Direct dependencies use compatible version ranges rather than
platform-specific pins. This allows pip to select supported Windows wheels
while preventing unreviewed major-version upgrades.

## Version checks

After activating the Windows environment, or by using its interpreter
explicitly, run:

```powershell
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pip --version
.\.venv\Scripts\python.exe -m pytest --version
.\.venv\Scripts\python.exe -m ruff --version
git --version
$PSVersionTable.PSVersion
```

The generic commands requested for troubleshooting are:

```powershell
python --version
pip --version
pytest --version
git --version
```

Prefer the explicit `.venv\Scripts\python.exe -m ...` form for this project. It
prevents a global Python, pip, or pytest installation from being used
accidentally.
