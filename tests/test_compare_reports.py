from __future__ import annotations

import csv

import pytest

from tri_arb.tools.compare_reports import (
    ReportComparisonError,
    load_report,
    main,
    render_comparison,
)


def _write_markdown(
    path,
    *,
    exchange="MEXC Spot",
    duration="60.00 minutes",
    conclusion="STOP",
    decision="UNCLEAR",
    fee_source="config_fee",
):
    path.write_text(
        f"""# Fixture report

## Executive summary

| Metric | Value |
|---|---:|
| Exchange | {exchange} |
| Simulated taker fee per leg | 0.02000% |
| Fee source | {fee_source} |
| Run duration | {duration} |
| Symbols monitored | 12 |
| Cycles monitored | 8 |
| Profitable after fees | 7 |
| Profitable after displayed depth | 5 |
| Profitable under pessimistic model | 3 / 10 observed |
| Average / median realistic edge | 0.0123% / 0.0045% |
| Total estimated PnL | 1.25 |
| Ghost arbitrage | 75.00% (9) |
| 48H_DECISION | {decision} |

## Best cycles by realistic PnL

| Cycle | Signals | Total PnL | Average PnL | Ghosts |
|---|---:|---:|---:|---:|
| USDT:AAA:BBB:USDT | 4 | 0.80 | 0.20 | 25% |
| USDT:CCC:DDD:USDT | 2 | 0.40 | 0.20 | 50% |

## Conclusion

**{conclusion}** — fixture rationale.
""",
        encoding="utf-8",
    )


def _write_summary(path, **overrides):
    row = {
        "exchange": "Binance Spot",
        "fee_assumption_per_leg": "0.001",
        "fee_source": "fixed_cli_fee",
        "run_duration_minutes": "90",
        "monitored_symbols": "20",
        "monitored_cycles": "18",
        "profitable_after_fees": "11",
        "profitable_after_depth": "9",
        "profitable_pessimistic": "4",
        "average_edge": "0.00125",
        "median_edge": "0.0005",
        "total_estimated_pnl": "12.3456",
        "ghost_arbitrage_percentage": "25",
        "conclusion": "PROCEED",
        "decision_48h": "CONTINUE_TO_7D",
    }
    row.update(overrides)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)


def test_sidecar_scalars_win_and_markdown_supplies_best_cycles(tmp_path):
    report_path = tmp_path / "report_run-1.md"
    _write_markdown(report_path)
    _write_summary(tmp_path / "summary_run-1.csv")

    report = load_report(report_path)

    assert report.values["exchange"] == "Binance Spot"
    assert report.values["fee_assumption"] == "0.10000%"
    assert report.values["fee_source"] == "fixed_cli_fee"
    assert report.values["duration"] == "90.00 min"
    assert report.values["average_realistic_edge"] == "0.125000%"
    assert report.values["median_realistic_edge"] == "0.050000%"
    assert report.values["total_estimated_pnl"] == "12.3456"
    assert report.values["conclusion"] == "PROCEED"
    assert report.values["decision_48h"] == "CONTINUE_TO_7D"
    assert report.best_cycles == (
        "USDT:AAA:BBB:USDT (PnL 0.80)",
        "USDT:CCC:DDD:USDT (PnL 0.40)",
    )


def test_latest_markdown_fallback_covers_every_requested_metric(tmp_path):
    report_path = tmp_path / "latest.md"
    _write_markdown(report_path)

    report = load_report(report_path)
    rendered = render_comparison([report])

    assert report.values == {
        "exchange": "MEXC Spot",
        "fee_assumption": "0.02000%",
        "fee_source": "config_fee",
        "duration": "60.00 minutes",
        "monitored_symbols": "12",
        "monitored_cycles": "8",
        "profitable_after_fees": "7",
        "profitable_after_depth": "5",
        "profitable_pessimistic": "3",
        "average_realistic_edge": "0.0123%",
        "median_realistic_edge": "0.0045%",
        "total_estimated_pnl": "1.25",
        "ghost_percentage": "75.00%",
        "decision_48h": "UNCLEAR",
        "conclusion": "STOP",
    }
    for label in (
        "Exchange",
        "Fee assumption per leg",
        "Fee source",
        "Duration",
        "Monitored symbols",
        "Monitored cycles",
        "Profitable after fees",
        "Profitable after depth",
        "Profitable pessimistic",
        "Average realistic edge",
        "Median realistic edge",
        "Total estimated PnL",
        "Ghost arbitrage",
        "Best cycles",
        "Conclusion",
        "48H_DECISION",
    ):
        assert f"| {label} |" in rendered


def test_account_fee_fallback_label_is_comparable(tmp_path):
    report_path = tmp_path / "account.md"
    _write_markdown(report_path, fee_source="mexc_account_tradeFee_read_only")
    text = report_path.read_text(encoding="utf-8").replace(
        "Simulated taker fee per leg",
        "Maximum/fallback taker fee per leg",
    )
    report_path.write_text(text, encoding="utf-8")

    assert load_report(report_path).values["fee_assumption"] == "0.02000%"


def test_timestamped_checkpoint_does_not_borrow_unrelated_latest_sidecar(tmp_path):
    checkpoint = tmp_path / "checkpoint_20260729T120000Z.md"
    _write_markdown(
        checkpoint,
        exchange="MEXC Spot",
        duration="60.00 minutes",
        conclusion="STOP",
    )
    _write_summary(
        tmp_path / "latest_summary.csv",
        exchange="Binance Spot",
        run_duration_minutes="999",
        conclusion="PROCEED",
    )

    report = load_report(checkpoint)

    assert report.values["exchange"] == "MEXC Spot"
    assert report.values["duration"] == "60.00 minutes"
    assert report.values["conclusion"] == "STOP"


def test_cli_sorts_reports_deterministically(tmp_path, capsys):
    later = tmp_path / "report_z.md"
    earlier = tmp_path / "report_a.md"
    _write_markdown(later, exchange="MEXC Spot")
    _write_markdown(earlier, exchange="Binance Spot")

    result = main([str(later), str(earlier)])

    captured = capsys.readouterr()
    assert result == 0
    header = next(line for line in captured.out.splitlines() if line.startswith("| Metric |"))
    assert header.index("report_a.md") < header.index("report_z.md")
    assert "Binance Spot" in captured.out
    assert "MEXC Spot" in captured.out
    assert captured.err == ""


def test_missing_or_unparseable_report_fails_without_echoing_contents(tmp_path, capsys):
    missing = tmp_path / "missing.md"
    assert main([str(missing)]) == 2
    missing_output = capsys.readouterr()
    assert "report file does not exist" in missing_output.err

    secret = "never-print-this-secret"
    malformed = tmp_path / "malformed.md"
    malformed.write_text(f"MEXC_API_SECRET={secret}\nnot a report\n", encoding="utf-8")

    assert main([str(malformed)]) == 2
    malformed_output = capsys.readouterr()
    assert "could not parse required report fields" in malformed_output.err
    assert secret not in malformed_output.out
    assert secret not in malformed_output.err


def test_load_report_rejects_non_markdown_input(tmp_path):
    source = tmp_path / "summary.csv"
    source.write_text("exchange\nMEXC Spot\n", encoding="utf-8")

    with pytest.raises(ReportComparisonError, match="must be a Markdown file"):
        load_report(source)
