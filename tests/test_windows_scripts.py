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
    }

    assert required <= {path.name for path in SCRIPTS.glob("*.ps1")}
    for name in required - {"collect_reports.ps1"}:
        assert "Scripts\\python.exe" in _script(name)


def test_windows_run_commands_and_long_run_guards_are_exact() -> None:
    assert "& $Python -u -m tri_arb.tools.check_mexc_fees @FeeCheckerArguments" in _script(
        "check_mexc_fees.ps1"
    )
    assert "& $Python -u -m tri_arb.main --exchange mexc --discover-only" in _script(
        "run_mexc_discovery.ps1"
    )
    assert (
        "& $Python -u -m tri_arb.main --exchange mexc --duration-minutes 10 "
        "--max-cycles 20 --use-account-fees" in _script("run_mexc_10min.ps1")
    )

    run_48h = _script("run_mexc_48h.ps1")
    assert (
        "& $Python -u -m tri_arb.main --exchange mexc --duration-minutes 2880 "
        "--max-cycles 20 --use-account-fees" in run_48h
    )
    assert "mexc_48h_console.txt" in run_48h
    assert "Tee-Object -FilePath $ConsoleLog" in run_48h
    assert "[int]$MinimumFreeGiB = 100" in run_48h

    run_7d = _script("run_mexc_7d.ps1")
    assert (
        "& $Python -u -m tri_arb.main --exchange mexc --duration-minutes 10080 "
        "--max-cycles 20 --use-account-fees" in run_7d
    )
    assert "mexc_7d_console.txt" in run_7d
    assert "Tee-Object -FilePath $ConsoleLog" in run_7d
    assert "[int]$MinimumFreeGiB = 500" in run_7d
    assert "48H_DECISION\\s*:\\s*CONTINUE_TO_7D" in run_7d


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
    ):
        assert pattern in ignore
