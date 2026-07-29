"""Compare generated research reports without loading raw market-data artifacts.

The generated ``summary_<report-id>.csv`` sidecar is the most precise source for
scalar metrics.  Markdown parsing remains intentionally supported for legacy
reports, ``latest.md``, and long-run checkpoint reports that may not have a
dedicated sidecar.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


class ReportComparisonError(ValueError):
    """Raised when a report cannot be read or recognized safely."""


@dataclass(frozen=True, slots=True)
class ComparableReport:
    """The allow-listed values displayed by the comparison command."""

    path: Path
    values: Mapping[str, str]
    best_cycles: tuple[str, ...]


_COMPARISON_FIELDS = (
    ("exchange", "Exchange"),
    ("fee_assumption", "Fee assumption per leg"),
    ("fee_source", "Fee source"),
    ("duration", "Duration"),
    ("monitored_symbols", "Monitored symbols"),
    ("monitored_cycles", "Monitored cycles"),
    ("profitable_after_fees", "Profitable after fees"),
    ("profitable_after_depth", "Profitable after depth"),
    ("profitable_pessimistic", "Profitable pessimistic"),
    ("average_realistic_edge", "Average realistic edge"),
    ("median_realistic_edge", "Median realistic edge"),
    ("total_estimated_pnl", "Total estimated PnL"),
    ("ghost_percentage", "Ghost arbitrage"),
    ("best_cycles", "Best cycles"),
    ("conclusion", "Conclusion"),
    ("decision_48h", "48H_DECISION"),
)

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(MEXC_API_(?:KEY|SECRET)|api[_ -]?key|api[_ -]?secret)\b\s*[:=]\s*\S+"
)
_SIGNATURE_VALUE = re.compile(r"(?i)\b(signature|x-mexc-apikey)\b\s*[:=]\s*\S+")
_HEADING = re.compile(r"(?m)^##\s+(.+?)\s*$")
_ALIGNMENT_CELL = re.compile(r"^:?-{3,}:?$")


def _redact(value: str) -> str:
    """Defensively redact credential-shaped text from allow-listed report fields."""

    redacted = _SECRET_ASSIGNMENT.sub(r"\1=[REDACTED]", value)
    return _SIGNATURE_VALUE.sub(r"\1=[REDACTED]", redacted)


def _normalise_label(value: str) -> str:
    value = value.replace("`", "").replace("*", "")
    value = value.replace("_", " ").replace("-", " ")
    return " ".join(value.lower().split())


def _split_markdown_row(line: str) -> tuple[str, ...]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return ()
    cells = re.split(r"(?<!\\)\|", stripped[1:-1])
    return tuple(cell.strip().replace(r"\|", "|") for cell in cells)


def _section(text: str, heading: str) -> str:
    target = _normalise_label(heading)
    headings = list(_HEADING.finditer(text))
    for index, match in enumerate(headings):
        if _normalise_label(match.group(1)) != target:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        return text[match.end() : end]
    return ""


def _executive_summary(text: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in _section(text, "Executive summary").splitlines():
        cells = _split_markdown_row(line)
        if len(cells) != 2 or all(_ALIGNMENT_CELL.fullmatch(cell) for cell in cells):
            continue
        label = _normalise_label(cells[0])
        if label == "metric":
            continue
        rows[label] = cells[1].strip()
    return rows


def _first(mapping: Mapping[str, str], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value.strip():
            return value.strip()
    return None


def _decimal(value: str | None, *, percentage: bool = False) -> Decimal | None:
    if value is None:
        return None
    cleaned = value.strip().replace(",", "")
    if percentage:
        cleaned = cleaned.removesuffix("%")
    try:
        result = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _format_decimal(value: Decimal, digits: int = 8) -> str:
    rendered = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def _format_count(value: str | None) -> str | None:
    number = _decimal(value)
    if number is None:
        return None
    if number == number.to_integral_value():
        return f"{int(number):,}"
    return _format_decimal(number)


def _format_fraction_as_percent(value: str | None, *, digits: int) -> str | None:
    number = _decimal(value)
    if number is None:
        return None
    return f"{number * 100:.{digits}f}%"


def _format_percentage_points(value: str | None, *, digits: int = 2) -> str | None:
    number = _decimal(value)
    if number is None:
        return None
    return f"{number:.{digits}f}%"


def _summary_candidates(report_path: Path) -> tuple[Path, ...]:
    stem = report_path.stem
    directory = report_path.parent
    if stem == "latest":
        return (directory / "latest_summary.csv",)
    if stem.startswith("report_"):
        report_id = stem.removeprefix("report_")
        return (directory / f"summary_{report_id}.csv",)
    if stem.startswith("checkpoint_"):
        report_id = stem.removeprefix("checkpoint_")
        return (
            directory / f"summary_{report_id}.csv",
            directory / f"checkpoint_summary_{report_id}.csv",
        )
    return (directory / f"summary_{stem}.csv",)


def _read_summary_sidecar(report_path: Path) -> dict[str, str]:
    for candidate in _summary_candidates(report_path):
        if not candidate.is_file():
            continue
        try:
            with candidate.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle), None)
        except (OSError, UnicodeError, csv.Error):
            continue
        if row:
            return {
                _normalise_label(str(key)): str(value)
                for key, value in row.items()
                if key is not None and value is not None
            }
    return {}


def _csv_values(row: Mapping[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}

    def set_value(name: str, value: str | None) -> None:
        if value is not None and value != "":
            values[name] = value

    set_value("exchange", _first(row, "exchange"))
    set_value(
        "fee_assumption",
        _format_fraction_as_percent(
            _first(
                row,
                "fee assumption per leg",
                "fee rate per taker leg",
                "maximum taker fee",
                "recommended conservative simulation fee",
            ),
            digits=5,
        ),
    )
    set_value("fee_source", _first(row, "fee source", "fee assumption source"))

    duration = _decimal(_first(row, "run duration minutes", "duration minutes"))
    if duration is None:
        duration_seconds = _decimal(_first(row, "run duration seconds", "duration seconds"))
        duration = duration_seconds / Decimal(60) if duration_seconds is not None else None
    if duration is not None:
        set_value("duration", f"{duration:.2f} min")
    set_value(
        "monitored_symbols",
        _format_count(_first(row, "monitored symbols", "symbol count")),
    )
    set_value(
        "monitored_cycles",
        _format_count(_first(row, "monitored cycles", "cycle count")),
    )
    set_value(
        "profitable_after_fees",
        _format_count(_first(row, "profitable after fees")),
    )
    set_value(
        "profitable_after_depth",
        _format_count(_first(row, "profitable after depth")),
    )
    set_value(
        "profitable_pessimistic",
        _format_count(
            _first(
                row,
                "profitable pessimistic",
                "profitable under pessimistic model",
            )
        ),
    )
    set_value(
        "average_realistic_edge",
        _format_fraction_as_percent(_first(row, "average edge"), digits=6),
    )
    set_value(
        "median_realistic_edge",
        _format_fraction_as_percent(_first(row, "median edge"), digits=6),
    )
    total_pnl = _decimal(_first(row, "total estimated pnl"))
    if total_pnl is not None:
        set_value("total_estimated_pnl", _format_decimal(total_pnl))
    set_value(
        "ghost_percentage",
        _format_percentage_points(_first(row, "ghost arbitrage percentage")),
    )
    set_value("conclusion", _first(row, "conclusion"))
    set_value(
        "decision_48h",
        _first(row, "48h decision", "decision 48h"),
    )
    return values


def _markdown_values(text: str) -> dict[str, str]:
    summary = _executive_summary(text)
    values: dict[str, str] = {}

    def set_value(name: str, value: str | None) -> None:
        if value is not None and value.strip():
            values[name] = value.strip()

    set_value("exchange", _first(summary, "exchange"))
    set_value(
        "fee_assumption",
        _first(
            summary,
            "simulated taker fee per leg",
            "fee assumption per leg",
            "conservative taker fee per leg",
            "maximum taker fee per leg",
            "maximum/fallback taker fee per leg",
        ),
    )
    set_value("fee_source", _first(summary, "fee source"))
    set_value("duration", _first(summary, "run duration", "duration"))
    set_value("monitored_symbols", _first(summary, "symbols monitored", "monitored symbols"))
    set_value("monitored_cycles", _first(summary, "cycles monitored", "monitored cycles"))
    set_value("profitable_after_fees", _first(summary, "profitable after fees"))
    set_value(
        "profitable_after_depth",
        _first(summary, "profitable after displayed depth", "profitable after depth"),
    )
    pessimistic = _first(
        summary,
        "profitable under pessimistic model",
        "profitable pessimistic",
    )
    if pessimistic is not None:
        set_value("profitable_pessimistic", pessimistic.split("/", 1)[0].strip())
    edge_pair = _first(summary, "average / median realistic edge")
    if edge_pair is not None:
        average, separator, median = edge_pair.partition("/")
        if separator:
            set_value("average_realistic_edge", average)
            set_value("median_realistic_edge", median)
    set_value(
        "average_realistic_edge",
        values.get("average_realistic_edge") or _first(summary, "average realistic edge"),
    )
    set_value(
        "median_realistic_edge",
        values.get("median_realistic_edge") or _first(summary, "median realistic edge"),
    )
    set_value("total_estimated_pnl", _first(summary, "total estimated pnl"))
    ghost = _first(summary, "ghost arbitrage", "ghost arbitrage percentage")
    if ghost is not None:
        set_value("ghost_percentage", ghost.split("(", 1)[0].strip())
    set_value("conclusion", _first(summary, "conclusion"))
    set_value("decision_48h", _first(summary, "48h decision", "decision 48h"))

    if "fee_source" not in values:
        fee_source = re.search(r"(?im)^-\s+\**Fee Source\**:\s*(.+?)\s*$", text)
        if fee_source:
            set_value("fee_source", fee_source.group(1))

    conclusion_section = _section(text, "Conclusion")
    if "conclusion" not in values and conclusion_section:
        match = re.search(r"(?im)^\s*\**(PROCEED|UNCLEAR|STOP)\**\b", conclusion_section)
        if match:
            set_value("conclusion", match.group(1))

    if "decision_48h" not in values:
        decision_section = _section(text, "48H_DECISION") or _section(text, "48H decision")
        match = re.search(
            r"(?im)^\s*\**(STOP|CONTINUE_TO_7D|UNCLEAR)\**\b",
            decision_section,
        )
        if match is None:
            match = re.search(
                r"(?im)^\s*\**48H_DECISION\**\s*:\s*\**"
                r"(STOP|CONTINUE_TO_7D|UNCLEAR)\**\b",
                text,
            )
        if match:
            set_value("decision_48h", match.group(1))
    return values


def _best_cycles(text: str) -> tuple[str, ...]:
    section = _section(text, "Best cycles by realistic PnL") or _section(text, "Best cycles")
    header: tuple[str, ...] = ()
    cycles: list[str] = []
    for line in section.splitlines():
        cells = _split_markdown_row(line)
        if not cells:
            continue
        if all(_ALIGNMENT_CELL.fullmatch(cell) for cell in cells):
            continue
        labels = tuple(_normalise_label(cell) for cell in cells)
        if "cycle" in labels:
            header = labels
            continue
        if not header or len(cells) != len(header):
            continue
        cycle = cells[header.index("cycle")].strip()
        if not cycle or cycle.startswith("_No cycle data"):
            continue
        total_pnl = cells[header.index("total pnl")].strip() if "total pnl" in header else ""
        description = f"{cycle} (PnL {total_pnl})" if total_pnl and total_pnl != "n/a" else cycle
        cycles.append(description)
    return tuple(cycles[:5])


def load_report(path: str | Path) -> ComparableReport:
    """Load one Markdown report and its matching scalar sidecar, when available."""

    source = Path(path)
    if not source.exists():
        raise ReportComparisonError(f"report file does not exist: {source}")
    if not source.is_file():
        raise ReportComparisonError(f"report path is not a file: {source}")
    if source.suffix.lower() != ".md":
        raise ReportComparisonError(f"report must be a Markdown file: {source}")
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReportComparisonError(f"could not read report file: {source}") from exc

    values = _markdown_values(text)
    # Exact CSV scalars win; Markdown supplies absent/new fields and cycle tables.
    values.update(_csv_values(_read_summary_sidecar(source)))
    missing = [
        label
        for key, label in (
            ("exchange", "exchange"),
            ("duration", "duration"),
            ("conclusion", "conclusion"),
        )
        if not values.get(key)
    ]
    if missing:
        fields = ", ".join(missing)
        raise ReportComparisonError(f"could not parse required report fields ({fields}): {source}")
    safe_values = {key: _redact(value) for key, value in values.items()}
    return ComparableReport(
        source,
        safe_values,
        tuple(_redact(cycle) for cycle in _best_cycles(text)),
    )


def _escape_cell(value: str) -> str:
    return value.replace("|", r"\|").replace("\r", " ").replace("\n", "<br>")


def render_comparison(reports: Sequence[ComparableReport]) -> str:
    """Render a deterministic, transposed Markdown comparison table."""

    if not reports:
        raise ReportComparisonError("provide at least one report file")
    names = [report.path.name for report in reports]
    duplicate_names = {name for name in names if names.count(name) > 1}
    labels = [
        str(report.path) if report.path.name in duplicate_names else report.path.name
        for report in reports
    ]
    lines = [
        "# Triangular Arbitrage Report Comparison",
        "",
        "| Metric | " + " | ".join(_escape_cell(label) for label in labels) + " |",
        "|---|" + "|".join("---:" for _ in reports) + "|",
    ]
    for key, label in _COMPARISON_FIELDS:
        cells: list[str] = []
        for report in reports:
            if key == "best_cycles":
                value = "<br>".join(report.best_cycles) if report.best_cycles else "n/a"
            else:
                value = report.values.get(key, "n/a")
            cells.append(_escape_cell(value))
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tri_arb.tools.compare_reports",
        description="Compare safe, allow-listed metrics from triangular-arbitrage Markdown reports.",
    )
    parser.add_argument("reports", nargs="+", type=Path, help="Markdown report paths")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    unique_paths = sorted(dict.fromkeys(args.reports), key=lambda path: str(path))
    try:
        reports = [load_report(path) for path in unique_paths]
        output = render_comparison(reports)
    except ReportComparisonError as exc:
        print(f"compare_reports: {exc}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
