# Environment versions

The project targets Python 3.11 and 3.12. The checked-in `.python-version`
selects Python 3.12 for tools that support it.

## Verified Windows environment

These versions were used for the public-release verification on 2026-08-16:

- Python: 3.12.7
- pip (project virtual environment): 26.1.2
- pytest: 9.1.1
- Ruff: 0.16.0
- Git for Windows: 2.50.1
- Windows PowerShell: 5.1.26100.9168

## Previously tested macOS environment

- macOS 26.5.2 on Apple Silicon
- Python 3.14.3
- pip 26.0
- Git 2.53.0

The macOS environment demonstrated forward compatibility but should not be
copied to Windows. Create a fresh platform-specific virtual environment.

## Dependency policy

Direct dependencies use reviewed compatible ranges in `requirements.txt`
rather than platform-specific pins. This allows pip to select supported wheels
while preventing unreviewed major-version upgrades. For exact reproduction,
record `python -m pip freeze` alongside an experiment without committing the
machine-specific environment.

Check the active Windows environment with:

```powershell
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pip --version
.\.venv\Scripts\python.exe -m pytest --version
.\.venv\Scripts\python.exe -m ruff --version
git --version
$PSVersionTable.PSVersion
```

Using `.venv\Scripts\python.exe -m ...` prevents accidental use of global
Python tools.
