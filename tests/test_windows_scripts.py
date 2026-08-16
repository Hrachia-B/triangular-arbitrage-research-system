from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"


def _script(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


def test_required_windows_scripts_exist_and_use_the_windows_venv() -> None:
    required = {
        "setup_windows.ps1",
        "check_mexc_fees.ps1",
        "run_mexc_discovery.ps1",
        "run_mexc_10min.ps1",
        "run_mexc_48h.ps1",
        "run_mexc_7d.ps1",
        "collect_reports.ps1",
        "setup_external_data_dir.ps1",
    }

    assert required <= {path.name for path in SCRIPTS.glob("*.ps1")}
    for name in required - {"collect_reports.ps1", "setup_external_data_dir.ps1"}:
        assert "Scripts\\python.exe" in _script(name)


def test_windows_run_commands_and_long_run_guards_are_exact() -> None:
    fee_check = _script("check_mexc_fees.ps1")
    assert "tri_arb.tools.check_mexc_fees" in fee_check
    assert "--raw-output-dir $rawAccountDir" in fee_check
    assert 'Join-Path $DataDir "account"' in fee_check

    discovery = _script("run_mexc_discovery.ps1")
    assert "tri_arb.main --exchange mexc --discover-only" in discovery
    assert "--data-dir $DataDir" in discovery

    run_10min = _script("run_mexc_10min.ps1")
    assert "--duration-minutes 10" in run_10min
    assert "--data-dir $DataDir" in run_10min
    assert "--storage-mode compact" in run_10min
    assert "--min-free-gib $MinFreeGiB" in run_10min

    run_48h = _script("run_mexc_48h.ps1")
    assert "--duration-minutes 2880" in run_48h
    assert "--data-dir $DataDir" in run_48h
    assert "--storage-mode compact" in run_48h
    assert "--min-free-gib $MinFreeGiB" in run_48h
    assert "mexc_48h_console.txt" in run_48h
    assert "Tee-Object -FilePath $ConsoleLog" in run_48h
    assert '$ErrorActionPreference = "Continue"' in run_48h
    assert 'ForEach-Object { "$_" }' in run_48h
    assert "$ErrorActionPreference = $previousErrorActionPreference" in run_48h
    assert "[string]$DataDir = $env:TRI_ARB_DATA_DIR" in run_48h
    assert 'Join-Path $RepoRoot "data"' in run_48h
    assert "[int]$MinFreeGiB = 15" in run_48h
    assert "GetPathRoot($DataDir)" in run_48h
    assert "GetPathRoot($RepoRoot)" not in run_48h

    run_7d = _script("run_mexc_7d.ps1")
    assert "--duration-minutes 10080" in run_7d
    assert "--data-dir $DataDir" in run_7d
    assert "--storage-mode compact" in run_7d
    assert "mexc_7d_console.txt" in run_7d
    assert "Tee-Object -FilePath $ConsoleLog" in run_7d
    assert '$ErrorActionPreference = "Continue"' in run_7d
    assert 'ForEach-Object { "$_" }' in run_7d
    assert "[int]$MinFreeGiB = 50" in run_7d
    assert "48H_DECISION\\s*:\\s*CONTINUE_TO_7D" in run_7d


def test_external_data_setup_and_report_collection_are_portable() -> None:
    setup = _script("setup_external_data_dir.ps1")
    assert "[string]$DataDir = $env:TRI_ARB_DATA_DIR" in setup
    assert "Pass -DataDir with an absolute path or set TRI_ARB_DATA_DIR" in setup
    for name in ("raw", "signals", "snapshots", "logs", "reports", "account", "exports"):
        assert f'"{name}"' in setup
    assert "AvailableFreeSpace" in setup
    assert "not writable" in setup

    collect = _script("collect_reports.ps1")
    assert "[string]$DataDir = $env:TRI_ARB_DATA_DIR" in collect
    assert 'Join-Path $RepoRoot "data"' in collect
    assert 'Join-Path $DataDir "reports"' in collect
    assert 'Join-Path $DataDir "exports"' in collect


def test_windows_setup_hides_secrets_and_checks_env_is_ignored() -> None:
    setup = _script("setup_windows.ps1")

    assert "Read-Host -Prompt $Prompt -AsSecureString" in setup
    assert "MEXC_API_KEY=$apiKey" in setup
    assert "MEXC_API_SECRET=$apiSecret" in setup
    assert 'git.exe check-ignore -q -- ".env"' in setup
    assert not re.search(
        r"Write-(?:Host|Output|Verbose|Debug).*\$(?:apiKey|apiSecret)",
        setup,
        flags=re.IGNORECASE,
    )


def test_generated_credentials_and_observations_are_ignored() -> None:
    ignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")

    for pattern in (
        ".env",
        ".env.*",
        "!.env.example",
        "data/account/",
        "data/**/account/",
        "configs/generated/",
        "data/raw/",
        "data/signals/",
        "data/snapshots/",
        "data/logs/",
        "data/reports/",
        "D:/",
        "tri_arb_data/",
    ):
        assert pattern in ignore
