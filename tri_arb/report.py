"""Generate a conservative Markdown and CSV research report from JSONL data."""

from __future__ import annotations

import csv
import heapq
import json
import math
import random
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any

STANDARD_LATENCY_BUCKETS = (50, 100, 250, 500, 1000)
DIAGNOSTIC_FEE_RATES = (0.001, 0.00075, 0.0005, 0.0002, 0.0001, 0.0)
EDGE_PERCENTILES = (
    ("min", 0.0),
    ("p50", 0.5),
    ("p90", 0.9),
    ("p95", 0.95),
    ("p99", 0.99),
    ("p99_9", 0.999),
    ("max", 1.0),
)


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if hasattr(value, "to_record") and callable(value.to_record):
        return value.to_record()
    if is_dataclass(value):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    return value


def _normalise_record(value: Any) -> dict[str, Any]:
    plain = _plain(value)
    if not isinstance(plain, Mapping):
        return {"value": plain}
    record = dict(plain)
    if "data" in record and isinstance(record["data"], Mapping):
        data = dict(record["data"])
        data.setdefault("_category", record.get("category"))
        data.setdefault("_recorded_at", record.get("recorded_at"))
        data.setdefault("_run_id", record.get("run_id"))
        if isinstance(record.get("context"), Mapping):
            for key, item in record["context"].items():
                data.setdefault(key, item)
        return data
    return record


def load_jsonl(path: str | Path) -> tuple[list[dict[str, Any]], int]:
    """Load one JSONL file or every JSONL artifact below a directory."""

    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(source)
    if source.is_dir():
        paths = sorted(
            {
                *source.rglob("*.jsonl"),
                *source.rglob("*.jsonl.*"),
            }
        )
    else:
        paths = [source]
    records: list[dict[str, Any]] = []
    parse_errors = 0
    for item in paths:
        with item.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    records.append(_normalise_record(json.loads(line)))
                except (json.JSONDecodeError, TypeError):
                    parse_errors += 1
    return records, parse_errors


def _path_value(record: Mapping[str, Any], path: str) -> Any:
    current: Any = record
    for component in path.split("."):
        if not isinstance(current, Mapping) or component not in current:
            return None
        current = current[component]
    return current


def _first(record: Mapping[str, Any], *paths: str, default: Any = None) -> Any:
    for path in paths:
        value = _path_value(record, path)
        if value is not None:
            return value
    return default


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "profitable"}
    return bool(value)


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000
        try:
            parsed = datetime.fromtimestamp(seconds, tz=UTC)
        except (ValueError, OSError, OverflowError):
            return None
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            numeric = _float(value)
            return _datetime(numeric) if numeric is not None else None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _average(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _median(values: Sequence[float]) -> float:
    return statistics.median(values) if values else 0.0


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    return _percentile_sorted(sorted(values), quantile)


def _percentile_sorted(ordered: Sequence[float], quantile: float) -> float:
    """Return a linearly interpolated percentile from an already sorted sample."""

    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    location = (len(ordered) - 1) * quantile
    lower = math.floor(location)
    upper = math.ceil(location)
    if lower == upper:
        return ordered[lower]
    fraction = location - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    """Summarize a numeric sample without presenting missing data as zero."""

    ordered = sorted(values)
    result: dict[str, float | int | None] = {"sample_count": len(ordered)}
    for label, quantile in EDGE_PERCENTILES:
        result[label] = _percentile_sorted(ordered, quantile) if ordered else None
    return result


def _break_even_fee_per_leg(raw_edge: float) -> float | None:
    """Estimate the non-negative equal per-leg fee tolerated by a three-leg edge."""

    if not math.isfinite(raw_edge) or raw_edge < 0 or raw_edge <= -1:
        return None
    # (1 + raw_edge) * (1 - fee) ** 3 == 1.  log1p/expm1 retain
    # precision for the tiny edges that dominate this data set.
    return -math.expm1(-math.log1p(raw_edge) / 3)


def _theoretical_edge_after_fee(raw_edge: float, fee_rate: float) -> float | None:
    """Apply an equal output-asset fee to each of three theoretical legs."""

    if raw_edge <= -1 or not 0 <= fee_rate < 1:
        return None
    return math.expm1(math.log1p(raw_edge) + 3 * math.log1p(-fee_rate))


def _configured_fee_rates(metadata: Mapping[str, Any]) -> tuple[float, ...]:
    """Return report fee scenarios persisted with the run, with a legacy fallback."""

    configured = _first(
        metadata,
        "fee_sensitivity_rates",
        "assumptions.fee_sensitivity_rates",
        default=DIAGNOSTIC_FEE_RATES,
    )
    if isinstance(configured, (str, bytes)) or not isinstance(configured, Sequence):
        configured = DIAGNOSTIC_FEE_RATES
    rates: list[float] = []
    for value in configured:
        rate = _float(value)
        if rate is None or not 0 <= rate < 1 or rate in rates:
            continue
        rates.append(rate)
    return tuple(rates) or DIAGNOSTIC_FEE_RATES


def _exchange_display_name(metadata: Mapping[str, Any]) -> str:
    value = str(
        _first(metadata, "exchange", "assumptions.exchange", default="Binance Spot")
    ).strip()
    normalized = value.lower().replace(" spot", "")
    if normalized == "mexc":
        return "MEXC Spot"
    if normalized == "binance":
        return "Binance Spot"
    return value or "Unknown Spot exchange"


def _latency_value(record: Mapping[str, Any], delay_ms: int) -> tuple[float | None, bool | None]:
    key = f"{delay_ms}ms"
    edge = _float(
        _first(
            record,
            f"return_after_{key}",
            f"return_after_latency.{key}",
            f"latency_returns.{key}",
            f"latency.{key}.net_return",
        )
    )
    profitable_value = _first(
        record,
        f"profitable_after_{key}",
        f"profitable_after_latency.{key}",
        f"latency.{key}.profitable",
    )
    checks = record.get("checks")
    if isinstance(checks, Sequence) and not isinstance(checks, (str, bytes)):
        for check in checks:
            if not isinstance(check, Mapping):
                continue
            parsed_delay = _float(check.get("delay_ms"))
            if parsed_delay is not None and int(parsed_delay) == delay_ms:
                if edge is None:
                    edge = _float(_first(check, "edge", "net_return", "result.net_return"))
                if profitable_value is None:
                    profitable_value = check.get("profitable")
                break
    profitable = (
        None
        if profitable_value is None and edge is None
        else _bool(profitable_value, (edge or 0) > 0)
    )
    return edge, profitable


def _latency_elapsed(record: Mapping[str, Any], delay_ms: int) -> float | None:
    key = f"{delay_ms}ms"
    elapsed = _float(
        _first(
            record,
            f"elapsed_after_{key}",
            f"elapsed_after_latency.{key}",
            f"latency.{key}.elapsed_ms",
        )
    )
    checks = record.get("checks")
    if elapsed is None and isinstance(checks, Sequence) and not isinstance(checks, (str, bytes)):
        for check in checks:
            if not isinstance(check, Mapping):
                continue
            parsed_delay = _float(check.get("delay_ms"))
            if parsed_delay is not None and int(parsed_delay) == delay_ms:
                elapsed = _float(check.get("elapsed_ms"))
                break
    return elapsed


def _record_edge(record: Mapping[str, Any]) -> float | None:
    return _float(
        _first(
            record,
            "return_after_depth",
            "net_return",
            "initial_edge",
            "initial_result.net_return",
            "result.net_return",
            "edge",
        )
    )


def _record_raw_edge(record: Mapping[str, Any]) -> float | None:
    return _float(
        _first(
            record,
            "raw_return",
            "gross_return",
            "initial_result.gross_return",
            "top_of_book.gross_return",
            "top_result.gross_return",
        )
    )


def _record_fee_edge(record: Mapping[str, Any]) -> float | None:
    return _float(
        _first(
            record,
            "return_after_fees",
            "after_fee_return",
            "net_return",
            "initial_result.net_return",
        )
    )


def _record_pnl(record: Mapping[str, Any]) -> float | None:
    return _float(
        _first(record, "estimated_pnl", "pnl", "initial_pnl", "initial_result.pnl", "result.pnl")
    )


def _record_executable(record: Mapping[str, Any]) -> bool:
    return _bool(
        _first(
            record,
            "fully_executable",
            "fully_executable_at_displayed_depth",
            "initial_result.fully_executable",
            "executable",
            default=True,
        ),
        True,
    ) and not _bool(_first(record, "filter_rejected", "initial_result.filter_rejected"), False)


def _opportunity_records(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    excluded_categories = {
        "health",
        "book_health",
        "latency",
        "summary",
        "snapshot",
        "error",
        "log",
    }
    candidates = [
        record
        for record in records
        if str(record.get("_category", "")).lower() not in excluded_categories
        and not (
            "checks" in record
            and _first(record, "initial_edge", "initial_result", "ghost_arbitrage") is not None
        )
        and any(
            value is not None
            for value in (
                _record_raw_edge(record),
                _record_fee_edge(record),
                _record_edge(record),
                _record_pnl(record),
            )
        )
    ]
    if candidates:
        # When canonical cooldown-window records exist, raw scan observations
        # remain useful as a separate frequency counter but must not inflate the
        # opportunity sample used for edge, PnL, or the viability decision.
        canonical = [
            record
            for record in candidates
            if str(record.get("_category", "")).lower() != "raw_opportunity"
        ]
        if canonical:
            candidates = canonical

        # The recorder intentionally persists the same signal at several stages
        # (raw, after-fee, depth, pessimistic).  Keep the canonical, richest
        # signal line while leaving id-less ad-hoc/synthetic observations alone.
        def completeness(item: Any) -> int:
            if isinstance(item, Mapping):
                return sum(completeness(value) for value in item.values())
            if isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
                return sum(completeness(value) for value in item)
            return int(item is not None)

        identified: dict[str, Mapping[str, Any]] = {}
        idless: list[Mapping[str, Any]] = []
        for record in candidates:
            signal_id = _first(record, "signal_id", "opportunity_id")
            if signal_id in (None, ""):
                idless.append(record)
                continue
            key = str(signal_id)
            category = str(record.get("_category", "")).lower()
            score = (int(category == "signal"), completeness(record))
            incumbent = identified.get(key)
            if incumbent is None:
                identified[key] = record
                continue
            incumbent_category = str(incumbent.get("_category", "")).lower()
            incumbent_score = (int(incumbent_category == "signal"), completeness(incumbent))
            if score > incumbent_score:
                identified[key] = record
        return [*identified.values(), *idless]
    # A report-only run may be given latency lines without their original signal
    # file.  Their immutable initial snapshots still contain the needed metrics.
    return [
        record
        for record in records
        if _first(record, "initial_edge", "initial_result.net_return", "initial_pnl") is not None
    ]


def _collect_staleness(record: Mapping[str, Any]) -> list[float]:
    value = _first(
        record,
        "average_book_staleness_ms",
        "book_staleness_ms",
        "book_staleness",
        "staleness_ms",
        "max_book_staleness_ms",
    )
    if isinstance(value, Mapping):
        return [number for item in value.values() if (number := _float(item)) is not None]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [number for item in value if (number := _float(item)) is not None]
    number = _float(value)
    if number is not None:
        return [number]

    def nested_ages(value: Any) -> list[float]:
        if not isinstance(value, Mapping):
            return []
        ages: list[float] = []
        for key, item in value.items():
            normalised = str(key).lower().replace("-", "_").replace(" ", "_")
            if normalised in {"age_ms", "staleness_ms", "book_staleness_ms"}:
                parsed = _float(item)
                if parsed is not None:
                    ages.append(parsed)
            elif isinstance(item, Mapping):
                ages.extend(nested_ages(item))
        return ages

    return nested_ages(record.get("health"))


def _monitored_count(metadata: Mapping[str, Any], singular: str, plural: str) -> int:
    value = metadata.get(
        f"monitored_{plural}", metadata.get(f"{singular}_count", metadata.get(plural))
    )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return len(value)
    number = _float(value)
    return int(number or 0)


def _decision(metrics: Mapping[str, Any], min_sample_size: int) -> tuple[str, str]:
    raw = int(metrics["raw_opportunities"])
    depth = int(metrics["profitable_after_depth"])
    pessimistic = int(metrics["profitable_pessimistic"])
    pessimistic_observed = int(metrics["pessimistic_observations"])
    slowest = int(metrics["profitable_at_slowest_latency"])
    latency_observed = int(metrics["latency_observations"])
    meaningful_latency_coverage = bool(metrics["meaningful_latency_coverage"])
    scheduling_accurate = bool(metrics["latency_scheduling_accurate"])
    median_pnl = float(metrics["median_estimated_pnl"])
    ghost_pct = float(metrics["ghost_arbitrage_percentage"])
    if metrics.get("run_error"):
        return "UNCLEAR", "The observer ended with an error, so its evidence is incomplete."
    if raw == 0:
        return (
            "UNCLEAR",
            "No raw opportunities were captured; the run cannot establish absence of edge.",
        )
    if raw >= min_sample_size and depth == 0:
        return (
            "STOP",
            "A meaningful sample produced no opportunity executable after fees and displayed depth.",
        )
    if raw >= min_sample_size and pessimistic_observed >= min_sample_size and pessimistic == 0:
        return (
            "STOP",
            "A meaningful sample produced no opportunity profitable under the pessimistic execution model.",
        )
    if (
        raw >= min_sample_size
        and meaningful_latency_coverage
        and scheduling_accurate
        and latency_observed
        and slowest == 0
        and median_pnl <= 0
    ):
        return (
            "STOP",
            "No signal survived the slowest measured latency bucket with positive median PnL.",
        )
    if (
        raw >= min_sample_size
        and depth > 0
        and pessimistic > 0
        and pessimistic_observed >= min_sample_size
        and latency_observed > 0
        and meaningful_latency_coverage
        and scheduling_accurate
        and slowest > 0
        and median_pnl > 0
        and ghost_pct <= 50
    ):
        return (
            "PROCEED",
            "Some depth-valid signals survived conservative latency with positive median estimated PnL.",
        )
    return (
        "UNCLEAR",
        "The sample or conservative survival evidence is not yet strong enough for a go/no-go decision.",
    )


def _decision_48h(
    metrics: Mapping[str, Any],
    thresholds: Mapping[str, Any],
) -> tuple[str, str]:
    """Apply the explicit 48-hour research continuation gate."""

    minimum_duration = max(
        0.0,
        _float(thresholds.get("minimum_duration_minutes")) or 2_880.0,
    )
    minimum_sample = max(1, int(_float(thresholds.get("minimum_sample_size")) or 20))
    stop_ghost_percentage = _float(thresholds.get("stop_ghost_percentage"))
    if stop_ghost_percentage is None:
        stop_ghost_percentage = 99.0
    minimum_total_pnl = _float(thresholds.get("minimum_total_estimated_pnl"))
    if minimum_total_pnl is None:
        minimum_total_pnl = 0.0
    minimum_positive_checkpoints = max(
        2,
        int(_float(thresholds.get("minimum_positive_checkpoints")) or 2),
    )
    positive_checkpoint_count = max(
        0,
        int(_float(metrics.get("positive_checkpoint_count")) or 0),
    )

    if metrics.get("run_error"):
        return "UNCLEAR", "The run ended with an error."
    if float(metrics["run_duration_minutes"]) < minimum_duration:
        return (
            "UNCLEAR",
            f"Complete at least {minimum_duration:,.0f} minutes before making the 48-hour decision.",
        )
    if int(metrics["raw_opportunities"]) < minimum_sample:
        return "UNCLEAR", "The 48-hour sample is too small for the configured decision threshold."

    best_net_edge = _float(metrics.get("best_net_edge"))
    if (
        int(metrics["profitable_after_depth"]) == 0
        and int(metrics["profitable_pessimistic"]) == 0
        and float(metrics["ghost_arbitrage_percentage"]) > stop_ghost_percentage
        and best_net_edge is not None
        and best_net_edge <= 0
    ):
        return (
            "STOP",
            "No depth or pessimistic survivor remained, ghost arbitrage exceeded the "
            "configured threshold, and the best net edge was non-positive.",
        )

    if _positive_checkpoint_evidence(metrics, thresholds):
        if positive_checkpoint_count < minimum_positive_checkpoints:
            return (
                "UNCLEAR",
                "Positive evidence has not yet persisted across enough periodic checkpoints "
                f"({positive_checkpoint_count}/{minimum_positive_checkpoints}).",
            )
        return (
            "CONTINUE_TO_7D",
            "Depth and pessimistic survivors repeated by cycle and checkpoint, survived a "
            "configured fast latency bucket, and did not have negative aggregate diagnostic PnL.",
        )
    return (
        "UNCLEAR",
        "The completed evidence does not satisfy every STOP or CONTINUE_TO_7D condition.",
    )


def _positive_checkpoint_evidence(
    metrics: Mapping[str, Any],
    thresholds: Mapping[str, Any],
) -> bool:
    """Return whether one cumulative checkpoint contains continuation evidence."""

    minimum_total_pnl = _float(thresholds.get("minimum_total_estimated_pnl"))
    if minimum_total_pnl is None:
        minimum_total_pnl = 0.0
    survival_buckets = thresholds.get("survival_buckets_ms", (50, 100))
    if isinstance(survival_buckets, (str, bytes)) or not isinstance(survival_buckets, Sequence):
        survival_buckets = (50, 100)
    profitable_by_bucket = metrics.get("latency_profitable_counts", {})
    survived_fast_latency = any(
        int(profitable_by_bucket.get(int(bucket), 0)) > 0
        for bucket in survival_buckets
        if _float(bucket) is not None
    )
    return bool(
        int(metrics["profitable_after_depth"]) > 0
        and int(metrics["profitable_pessimistic"]) > 0
        and survived_fast_latency
        and int(metrics["repeated_positive_cycles"]) > 0
        and float(metrics["total_estimated_pnl"]) >= minimum_total_pnl
    )


def calculate_metrics(
    records: Iterable[Any], metadata: Mapping[str, Any] | None = None, *, parse_errors: int = 0
) -> dict[str, Any]:
    """Calculate report metrics without writing files (useful in tests/notebooks)."""

    normalised = [_normalise_record(record) for record in records]
    meta = dict(metadata or {})
    opportunities = _opportunity_records(normalised)
    raw_observation_records = [
        record
        for record in normalised
        if str(record.get("_category", "")).lower() == "raw_opportunity"
        and (_record_raw_edge(record) or 0) > 0
    ]
    latency_candidates = [
        record
        for record in normalised
        if str(record.get("_category", "")).lower() == "latency"
        or "checks" in record
        or "return_after_latency" in record
    ]
    latency_by_id: dict[str, Mapping[str, Any]] = {}
    anonymous_latency: list[Mapping[str, Any]] = []
    for record in latency_candidates:
        signal_id = _first(record, "signal_id", "opportunity_id")
        if signal_id in (None, ""):
            anonymous_latency.append(record)
            continue
        key = str(signal_id)
        incumbent = latency_by_id.get(key)
        if incumbent is None or len(record.get("checks", ())) > len(incumbent.get("checks", ())):
            latency_by_id[key] = record
    latency_records = [*latency_by_id.values(), *anonymous_latency]

    raw_edges = [edge for record in opportunities if (edge := _record_raw_edge(record)) is not None]
    fee_edges = [edge for record in opportunities if (edge := _record_fee_edge(record)) is not None]
    realistic_edges = [
        edge
        for record in opportunities
        if _record_executable(record) and (edge := _record_edge(record)) is not None
    ]
    raw_edge_percentiles = _distribution(raw_edges)
    net_edge_percentiles = _distribution(fee_edges)
    break_even_fees = [
        fee for raw_edge in raw_edges if (fee := _break_even_fee_per_leg(raw_edge)) is not None
    ]
    ordered_break_even_fees = sorted(break_even_fees)
    break_even_fee_stats: dict[str, float | int | None] = {
        "sample_count": len(ordered_break_even_fees),
        "average": _average(ordered_break_even_fees) if ordered_break_even_fees else None,
        "median": _percentile_sorted(ordered_break_even_fees, 0.5)
        if ordered_break_even_fees
        else None,
        "p95": _percentile_sorted(ordered_break_even_fees, 0.95)
        if ordered_break_even_fees
        else None,
        "p99": _percentile_sorted(ordered_break_even_fees, 0.99)
        if ordered_break_even_fees
        else None,
        "max": ordered_break_even_fees[-1] if ordered_break_even_fees else None,
    }
    fee_sensitivity_rates = _configured_fee_rates(meta)
    fee_sensitivity: list[dict[str, float | int]] = []
    for fee_rate in fee_sensitivity_rates:
        theoretical_edges = [
            edge
            for raw_edge in raw_edges
            if (edge := _theoretical_edge_after_fee(raw_edge, fee_rate)) is not None
        ]
        profitable_count = sum(edge > 0 for edge in theoretical_edges)
        sample_count = len(theoretical_edges)
        fee_sensitivity.append(
            {
                "fee_rate": fee_rate,
                "profitable_count": profitable_count,
                "sample_count": sample_count,
                "profitable_percentage": profitable_count / sample_count * 100
                if sample_count
                else 0.0,
            }
        )

    indexed_raw_opportunities = [
        (index, record)
        for index, record in enumerate(opportunities)
        if _record_raw_edge(record) is not None
    ]
    top_raw_records = heapq.nlargest(
        20,
        indexed_raw_opportunities,
        key=lambda item: (_record_raw_edge(item[1]) or 0.0, -item[0]),
    )
    best_raw_opportunities = [
        {
            "cycle_id": str(
                _first(record, "cycle_id", "initial_result.cycle_id", default="unknown")
            ),
            "start_size": _first(
                record,
                "start_size",
                "start_amount",
                "initial_result.start_amount",
            ),
            "raw_edge": _record_raw_edge(record),
            "edge_after_fees": _record_fee_edge(record),
            "estimated_pnl": _record_pnl(record),
            "limiting_leg": _first(
                record,
                "limiting_leg",
                "depth_simulation.limiting_leg",
                default=None,
            ),
            "book_staleness_ms": _float(
                _first(
                    record,
                    "book_staleness_ms",
                    "max_book_staleness_ms",
                    default=None,
                )
            ),
        }
        for _, record in top_raw_records
    ]
    fee_profitable = sum(1 for record in opportunities if (_record_fee_edge(record) or 0) > 0)
    depth_profitable = sum(
        1
        for record in opportunities
        if _record_executable(record) and (_record_edge(record) or 0) > 0
    )
    pessimistic_profitable = sum(
        1
        for record in opportunities
        if _bool(
            _first(record, "profitable_pessimistic"),
            (_float(_first(record, "pessimistic_return", "pessimistic_simulation.net_return")) or 0)
            > 0,
        )
    )
    pessimistic_observations = sum(
        1
        for record in opportunities
        if _first(
            record,
            "profitable_pessimistic",
            "pessimistic_return",
            "pessimistic_simulation.net_return",
        )
        is not None
    )
    pnls = [
        pnl
        for record in opportunities
        if _record_executable(record) and (pnl := _record_pnl(record)) is not None
    ]
    non_executable_count = sum(1 for record in opportunities if not _record_executable(record))
    filter_rejected_count = sum(
        1
        for record in opportunities
        if _bool(_first(record, "filter_rejected", "depth_simulation.filter_rejected"), False)
    )

    configured_buckets = meta.get("latency_buckets_ms", STANDARD_LATENCY_BUCKETS)
    buckets = {int(item) for item in configured_buckets}
    for record in latency_records:
        checks = record.get("checks")
        if isinstance(checks, Sequence) and not isinstance(checks, (str, bytes)):
            for check in checks:
                if isinstance(check, Mapping) and _float(check.get("delay_ms")) is not None:
                    buckets.add(int(float(check["delay_ms"])))
    ordered_buckets = tuple(sorted(buckets))
    latency_counts: dict[int, int] = {}
    latency_samples: dict[int, int] = {}
    latency_edges: dict[int, list[float]] = defaultdict(list)
    latency_actual_elapsed: dict[int, list[float]] = defaultdict(list)
    latency_lateness: dict[int, list[float]] = defaultdict(list)
    configured_tolerance = _float(
        _first(meta, "latency_lateness_tolerance_ms", "assumptions.latency_lateness_tolerance_ms")
    )
    lateness_tolerance_ms = 25.0 if configured_tolerance is None else max(0.0, configured_tolerance)
    for bucket in ordered_buckets:
        count = 0
        samples = 0
        for record in latency_records:
            edge, profitable = _latency_value(record, bucket)
            if profitable is None:
                continue
            samples += 1
            count += int(profitable)
            if edge is not None:
                latency_edges[bucket].append(edge)
            elapsed = _latency_elapsed(record, bucket)
            if elapsed is not None:
                latency_actual_elapsed[bucket].append(elapsed)
                latency_lateness[bucket].append(elapsed - bucket)
        latency_counts[bucket] = count
        latency_samples[bucket] = samples

    average_actual_by_bucket = {
        bucket: _average(latency_actual_elapsed[bucket]) for bucket in ordered_buckets
    }
    median_actual_by_bucket = {
        bucket: _median(latency_actual_elapsed[bucket]) for bucket in ordered_buckets
    }
    average_lateness_by_bucket = {
        bucket: _average(latency_lateness[bucket]) for bucket in ordered_buckets
    }
    median_lateness_by_bucket = {
        bucket: _median(latency_lateness[bucket]) for bucket in ordered_buckets
    }
    late_counts_by_bucket = {
        bucket: sum(1 for value in latency_lateness[bucket] if value > lateness_tolerance_ms)
        for bucket in ordered_buckets
    }
    all_lateness = [value for bucket in ordered_buckets for value in latency_lateness[bucket]]

    ghosts = sum(
        1
        for record in latency_records
        if _bool(_first(record, "ghost_arbitrage", "ghost_arbitrage_flag", "ghost"), False)
    )
    lifetimes = [
        lifetime
        for record in latency_records
        if (lifetime := _float(_first(record, "lifetime_ms", "signal_lifetime_ms"))) is not None
    ]
    # Signal-detection staleness is the population relevant to opportunity
    # quality. Mixing periodic health snapshots and latency rechecks here would
    # weight the same run state several different ways.
    staleness = [value for record in opportunities for value in _collect_staleness(record)]
    if not staleness:
        staleness = [value for record in normalised for value in _collect_staleness(record)]

    health_records = [
        record
        for record in normalised
        if str(record.get("_category", "")).lower() in {"health", "book_health"}
        or _first(record, "event", "event_type", "health_event") is not None
    ]
    startup_health = (
        meta.get("startup_book_health")
        if isinstance(meta.get("startup_book_health"), Mapping)
        else {}
    )
    startup_unhealthy_symbols: list[str] = []
    startup_managed_books: int | None = None
    startup_health_source = "unavailable"
    configured_startup_symbols = startup_health.get("unhealthy_symbols")
    if isinstance(configured_startup_symbols, Sequence) and not isinstance(
        configured_startup_symbols, (str, bytes)
    ):
        startup_unhealthy_symbols = sorted(str(symbol) for symbol in configured_startup_symbols)
        startup_managed_books = int(
            _float(startup_health.get("managed_books"))
            or _monitored_count(meta, "symbol", "symbols")
        )
        startup_health_source = "captured after the startup wait"
    else:
        health_snapshots = [
            record for record in health_records if isinstance(record.get("health"), Mapping)
        ]
        health_snapshots.sort(
            key=lambda record: (
                _datetime(_first(record, "timestamp_local", "_recorded_at", "recorded_at"))
                or datetime.max.replace(tzinfo=UTC)
            )
        )
        if health_snapshots:
            first_health = health_snapshots[0]["health"]
            startup_unhealthy_symbols = sorted(
                str(symbol)
                for symbol, value in first_health.items()
                if isinstance(value, Mapping) and value.get("healthy") is False
            )
            startup_managed_books = len(first_health)
            startup_health_source = "inferred from the first recorded health snapshot"
    explicit_resyncs = 0
    explicit_sequence_gaps = 0
    cumulative_resyncs = 0
    cumulative_sequence_gaps = 0

    def counter_values(value: Any, needles: set[str]) -> list[int]:
        found: list[int] = []
        if not isinstance(value, Mapping):
            return found
        for key, item in value.items():
            normalised_key = str(key).lower().replace("-", "_").replace(" ", "_")
            if normalised_key in needles:
                number = _float(item)
                if number is not None:
                    found.append(int(number))
            elif isinstance(item, Mapping):
                found.extend(counter_values(item, needles))
        return found

    resync_keys = {"resync", "resyncs", "resync_count", "book_resyncs", "order_book_resyncs"}
    gap_keys = {
        "gap_count",
        "gaps",
        "sequence_gap_count",
        "sequence_gaps",
        "book_sequence_gaps",
        "order_book_sequence_gaps",
    }
    for record in health_records:
        event = str(
            _first(record, "event", "event_type", "health_event", "type", default="")
        ).lower()
        explicit_resyncs += int("resync" in event)
        explicit_sequence_gaps += int(
            "sequence_gap" in event or "sequence gap" in event or event == "gap"
        )
        # Manager snapshots contain cumulative counters and are emitted
        # repeatedly.  Their maximum is the run total; summing would inflate it.
        record_resyncs = counter_values(record, resync_keys)
        record_gaps = counter_values(record, gap_keys)
        cumulative_resyncs = max(cumulative_resyncs, max(record_resyncs, default=0))
        cumulative_sequence_gaps = max(cumulative_sequence_gaps, max(record_gaps, default=0))
    resyncs = max(explicit_resyncs, cumulative_resyncs)
    sequence_gaps = max(explicit_sequence_gaps, cumulative_sequence_gaps)

    timestamps = [
        parsed
        for record in normalised
        if (
            parsed := _datetime(
                _first(
                    record,
                    "timestamp_local",
                    "evaluated_at",
                    "detected_at",
                    "checked_at",
                    "_recorded_at",
                    "recorded_at",
                )
            )
        )
        is not None
    ]
    duration_seconds = _float(meta.get("duration_seconds"))
    if duration_seconds is None:
        started = _datetime(meta.get("started_at"))
        ended = _datetime(meta.get("ended_at"))
        if started and ended:
            duration_seconds = max(0.0, (ended - started).total_seconds())
        elif len(timestamps) >= 2:
            duration_seconds = max(0.0, (max(timestamps) - min(timestamps)).total_seconds())
        else:
            duration_seconds = 0.0

    cycle_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "signals": 0,
            "total_pnl": 0.0,
            "pnls": [],
            "raw_edges": [],
            "net_edges": [],
            "ghosts": 0,
            "ghost_samples": 0,
        }
    )
    monitored_cycle_ids = meta.get("monitored_cycle_ids", ())
    if isinstance(monitored_cycle_ids, Sequence) and not isinstance(
        monitored_cycle_ids, (str, bytes)
    ):
        for cycle_id in monitored_cycle_ids:
            cycle_stats[str(cycle_id)]
    for record in opportunities:
        cycle_id = str(_first(record, "cycle_id", "initial_result.cycle_id", default="unknown"))
        pnl = _record_pnl(record) if _record_executable(record) else None
        cycle_stats[cycle_id]["signals"] += 1
        raw_edge = _record_raw_edge(record)
        if raw_edge is not None:
            cycle_stats[cycle_id]["raw_edges"].append(raw_edge)
        net_edge = _record_fee_edge(record)
        if net_edge is not None:
            cycle_stats[cycle_id]["net_edges"].append(net_edge)
        if pnl is not None:
            cycle_stats[cycle_id]["total_pnl"] += pnl
            cycle_stats[cycle_id]["pnls"].append(pnl)
        depth_positive = _bool(
            _first(record, "profitable_after_depth"),
            (_record_edge(record) or 0) > 0,
        )
        pessimistic_positive = _bool(
            _first(record, "profitable_pessimistic"),
            (_float(_first(record, "pessimistic_return")) or 0) > 0,
        )
        if depth_positive and pessimistic_positive:
            cycle_stats[cycle_id].setdefault("positive_realistic_signals", 0)
            cycle_stats[cycle_id]["positive_realistic_signals"] += 1
    for record in latency_records:
        cycle_id = str(_first(record, "cycle_id", "initial_result.cycle_id", default="unknown"))
        ghost = _first(record, "ghost_arbitrage", "ghost_arbitrage_flag", "ghost")
        if ghost is not None:
            cycle_stats[cycle_id]["ghost_samples"] += 1
            cycle_stats[cycle_id]["ghosts"] += int(_bool(ghost, False))
    ranked_cycles: list[dict[str, Any]] = []
    per_cycle_diagnostics: list[dict[str, Any]] = []
    for cycle_id, values in cycle_stats.items():
        signals = values["signals"]
        ghost_samples = values["ghost_samples"]
        if signals:
            ranked_cycles.append(
                {
                    "cycle_id": cycle_id,
                    "signals": signals,
                    "total_pnl": values["total_pnl"],
                    "average_pnl": _average(values["pnls"]),
                    "ghost_percentage": values["ghosts"] / signals * 100,
                }
            )
        per_cycle_diagnostics.append(
            {
                "cycle_id": cycle_id,
                "signals": signals,
                "average_raw_edge": _average(values["raw_edges"]) if values["raw_edges"] else None,
                "max_raw_edge": max(values["raw_edges"]) if values["raw_edges"] else None,
                "average_net_edge": _average(values["net_edges"]) if values["net_edges"] else None,
                "best_net_edge": max(values["net_edges"]) if values["net_edges"] else None,
                "ghost_percentage": values["ghosts"] / ghost_samples * 100
                if ghost_samples
                else None,
                "ghost_samples": ghost_samples,
            }
        )
    per_cycle_diagnostics.sort(key=lambda item: item["cycle_id"])
    best_cycles = sorted(ranked_cycles, key=lambda item: item["total_pnl"], reverse=True)[:5]
    worst_cycles = sorted(ranked_cycles, key=lambda item: item["total_pnl"])[:5]
    noisy_cycles = sorted(
        ranked_cycles, key=lambda item: (item["ghost_percentage"], item["signals"]), reverse=True
    )[:5]

    slowest_bucket = ordered_buckets[-1] if ordered_buckets else 0
    unique_signal_count = sum(
        1
        for record in opportunities
        if (_record_raw_edge(record) or 0) > 0
        or (_record_fee_edge(record) or 0) > 0
        or _record_edge(record) is not None
    )
    scanner_stats = (
        meta.get("scanner_stats") if isinstance(meta.get("scanner_stats"), Mapping) else {}
    )
    scanner_scan_count = int(_float(scanner_stats.get("scans")) or 0)
    scan_deadline_misses = int(_float(scanner_stats.get("scan_deadline_misses")) or 0)
    total_scan_time_ms = _float(scanner_stats.get("total_scan_time_ms"))
    average_scan_time_ms = (
        total_scan_time_ms / scanner_scan_count
        if total_scan_time_ms is not None and scanner_scan_count
        else None
    )
    maximum_scan_time_ms = _float(scanner_stats.get("maximum_scan_time_ms"))
    raw_observation_count = int(
        _float(scanner_stats.get("raw_opportunity_observations")) or len(raw_observation_records)
    )
    min_sample_size = max(1, int(_float(meta.get("minimum_decision_sample")) or 20))
    canonical_ids = {
        str(signal_id)
        for record in opportunities
        if (signal_id := _first(record, "signal_id", "opportunity_id")) not in (None, "")
    }
    slowest_latency_ids = {
        str(signal_id)
        for record in latency_records
        if (signal_id := _first(record, "signal_id", "opportunity_id")) not in (None, "")
        and _latency_value(record, slowest_bucket)[1] is not None
    }
    if canonical_ids:
        covered_signals = len(canonical_ids & slowest_latency_ids)
        anonymous_signals = max(0, unique_signal_count - len(canonical_ids))
        anonymous_slowest_checks = sum(
            1
            for record in anonymous_latency
            if _latency_value(record, slowest_bucket)[1] is not None
        )
        covered_signals += min(anonymous_signals, anonymous_slowest_checks)
    else:
        covered_signals = min(unique_signal_count, latency_samples.get(slowest_bucket, 0))
    latency_coverage = covered_signals / unique_signal_count if unique_signal_count else 0.0
    meaningful_latency_coverage = bool(
        unique_signal_count and (latency_coverage >= 0.9 or covered_signals >= min_sample_size)
    )
    total_latency_samples = sum(latency_samples.values())
    timing_samples = len(all_lateness)
    latency_scheduling_accurate = bool(
        total_latency_samples
        and timing_samples >= total_latency_samples
        and max(all_lateness, default=float("inf")) <= lateness_tolerance_ms
    )
    run_error_value = meta.get("error")
    run_error = str(run_error_value) if run_error_value not in (None, "") else ""
    late_check_count = sum(late_counts_by_bucket.values())
    configured_stale_after_ms = _float(
        _first(meta, "book_stale_after_ms", "assumptions.book_stale_after_ms")
    )
    exchange_name = _exchange_display_name(meta)
    fee_assumption = _float(
        _first(meta, "fee_rate_per_taker_leg", "assumptions.fee_rate_per_taker_leg")
    )
    decision_thresholds = meta.get("decision") if isinstance(meta.get("decision"), Mapping) else {}
    repeated_minimum = max(
        1,
        int(_float(decision_thresholds.get("repeated_positive_cycle_min_signals")) or 2),
    )
    repeated_positive_cycles = sum(
        1
        for values in cycle_stats.values()
        if int(values.get("positive_realistic_signals", 0)) >= repeated_minimum
    )
    checkpoint_count = max(0, int(_float(meta.get("checkpoint_count")) or 0))
    positive_checkpoint_count = max(
        0,
        int(_float(meta.get("positive_checkpoint_count")) or 0),
    )
    minimum_positive_checkpoints = max(
        2,
        int(_float(decision_thresholds.get("minimum_positive_checkpoints")) or 2),
    )
    metrics: dict[str, Any] = {
        "exchange": exchange_name,
        "fee_assumption_per_leg": fee_assumption,
        "fee_source": str(
            _first(meta, "fee_source", "assumptions.fee_source", default="config_fee")
        ),
        "run_duration_seconds": duration_seconds,
        "run_duration_minutes": duration_seconds / 60,
        "monitored_symbols": _monitored_count(meta, "symbol", "symbols"),
        "monitored_cycles": _monitored_count(meta, "cycle", "cycles"),
        "total_records": len(normalised),
        "parse_errors": parse_errors,
        "raw_opportunities": unique_signal_count,
        "unique_signals": int(_float(scanner_stats.get("unique_signals")) or unique_signal_count),
        "raw_opportunity_observations": raw_observation_count,
        "profitable_after_fees": fee_profitable,
        "profitable_after_depth": depth_profitable,
        "profitable_pessimistic": pessimistic_profitable,
        "pessimistic_observations": pessimistic_observations,
        "non_executable_signals": non_executable_count,
        "filter_rejected_signals": filter_rejected_count,
        "scanner_profitable_after_fees": int(
            _float(scanner_stats.get("profitable_after_fees")) or 0
        ),
        "scanner_profitable_after_depth": int(
            _float(scanner_stats.get("profitable_after_depth")) or 0
        ),
        "scanner_profitable_pessimistic": int(
            _float(scanner_stats.get("profitable_pessimistic")) or 0
        ),
        "scanner_scan_count": scanner_scan_count,
        "scan_deadline_misses": scan_deadline_misses,
        "scan_deadline_miss_percentage": scan_deadline_misses / scanner_scan_count * 100
        if scanner_scan_count
        else None,
        "average_scan_time_ms": average_scan_time_ms,
        "maximum_scan_time_ms": maximum_scan_time_ms,
        "latency_observations": len(latency_records),
        "latency_covered_signals": covered_signals,
        "latency_coverage": latency_coverage,
        "latency_coverage_percentage": latency_coverage * 100,
        "meaningful_latency_coverage": meaningful_latency_coverage,
        "latency_profitable_counts": latency_counts,
        "latency_sample_counts": latency_samples,
        "latency_average_actual_ms": average_actual_by_bucket,
        "latency_median_actual_ms": median_actual_by_bucket,
        "latency_average_lateness_ms": average_lateness_by_bucket,
        "latency_median_lateness_ms": median_lateness_by_bucket,
        "latency_late_counts": late_counts_by_bucket,
        "latency_total_check_count": total_latency_samples,
        "latency_late_check_count": late_check_count,
        "latency_late_check_percentage": late_check_count / total_latency_samples * 100
        if total_latency_samples
        else None,
        "latency_timing_sample_count": timing_samples,
        "latency_lateness_tolerance_ms": lateness_tolerance_ms,
        "average_latency_lateness_ms": _average(all_lateness),
        "median_latency_lateness_ms": _median(all_lateness),
        "maximum_latency_lateness_ms": max(0.0, max(all_lateness, default=0.0)),
        "latency_scheduling_accurate": latency_scheduling_accurate,
        "profitable_at_slowest_latency": latency_counts.get(slowest_bucket, 0),
        "slowest_latency_bucket_ms": slowest_bucket,
        "average_edge": _average(realistic_edges),
        "median_edge": _median(realistic_edges),
        "best_realistic_edge": max(realistic_edges) if realistic_edges else None,
        "average_raw_edge": _average(raw_edges),
        "median_raw_edge": _median(raw_edges),
        "best_net_edge": max(fee_edges) if fee_edges else None,
        "raw_edge_percentiles": raw_edge_percentiles,
        "net_edge_percentiles_after_fees": net_edge_percentiles,
        "break_even_fee_per_leg": break_even_fee_stats,
        "fee_sensitivity": fee_sensitivity,
        "fee_sensitivity_rates": fee_sensitivity_rates,
        "best_raw_opportunities": best_raw_opportunities,
        "average_estimated_pnl": _average(pnls),
        "median_estimated_pnl": _median(pnls),
        "total_estimated_pnl": sum(pnls),
        "best_cycles": best_cycles,
        "worst_cycles": worst_cycles,
        "noisy_cycles": noisy_cycles,
        "per_cycle_diagnostics": per_cycle_diagnostics,
        "repeated_positive_cycles": repeated_positive_cycles,
        "checkpoint_count": checkpoint_count,
        "positive_checkpoint_count": positive_checkpoint_count,
        "minimum_positive_checkpoints": minimum_positive_checkpoints,
        "lifetime_distribution_ms": {
            "minimum": min(lifetimes) if lifetimes else 0.0,
            "median": _median(lifetimes),
            "p90": _percentile(lifetimes, 0.9),
            "maximum": max(lifetimes) if lifetimes else 0.0,
        },
        "ghost_arbitrage_count": ghosts,
        "ghost_arbitrage_percentage": ghosts / len(latency_records) * 100
        if latency_records
        else 0.0,
        "average_book_staleness_ms": _average(staleness) if staleness else None,
        "book_staleness_sample_count": len(staleness),
        "book_stale_after_ms": configured_stale_after_ms,
        "startup_unhealthy_books": len(startup_unhealthy_symbols)
        if startup_managed_books is not None
        else None,
        "startup_managed_books": startup_managed_books,
        "startup_unhealthy_symbols": startup_unhealthy_symbols,
        "startup_health_source": startup_health_source,
        "order_book_resyncs": resyncs,
        "sequence_gaps": sequence_gaps,
        "run_error": run_error,
        "assumptions": meta.get("assumptions", meta),
    }
    for key in (
        "data_dir",
        "data_drive",
        "storage_mode",
        "min_free_gib",
        "free_gib_at_start",
        "raw_signal_sample_rate",
        "top_n_retention",
        "near_break_even_threshold",
        "checkpoint_interval_minutes",
        "compact_mode_active",
        "latest_report_path",
    ):
        metrics[key] = meta.get(key)
    conclusion, rationale = _decision(metrics, min_sample_size)
    metrics["conclusion"] = conclusion
    metrics["conclusion_rationale"] = rationale
    metrics["minimum_decision_sample"] = min_sample_size
    decision_48h, decision_48h_rationale = _decision_48h(metrics, decision_thresholds)
    metrics["decision_48h"] = decision_48h
    metrics["decision_48h_rationale"] = decision_48h_rationale
    return metrics


class _BoundedReservoir:
    """Deterministic Algorithm-R reservoir used only for distribution estimates."""

    def __init__(self, limit: int, seed: int) -> None:
        self.limit = limit
        self.seen = 0
        self._items: list[dict[str, Any]] = []
        self._random = random.Random(seed)

    def add(self, record: dict[str, Any]) -> None:
        self.seen += 1
        if len(self._items) < self.limit:
            self._items.append(record)
            return
        replacement = self._random.randrange(self.seen)
        if replacement < self.limit:
            self._items[replacement] = record

    def snapshot(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._items)


def _empty_running_stats() -> dict[str, float | int | None]:
    return {"count": 0, "sum": 0.0, "min": None, "max": None}


def _observe_running_stats(
    stats: dict[str, float | int | None],
    value: float | None,
) -> None:
    if value is None:
        return
    stats["count"] = int(stats["count"] or 0) + 1
    stats["sum"] = float(stats["sum"] or 0.0) + value
    current_min = _float(stats["min"])
    current_max = _float(stats["max"])
    stats["min"] = value if current_min is None else min(current_min, value)
    stats["max"] = value if current_max is None else max(current_max, value)


_STREAM_SIGNAL_FIELDS = frozenset(
    {
        "signal_id",
        "opportunity_id",
        "cycle_id",
        "start_size",
        "start_amount",
        "raw_return",
        "gross_return",
        "net_return",
        "return_after_fees",
        "after_fee_return",
        "return_after_depth",
        "pessimistic_return",
        "profitable_after_fees",
        "profitable_after_depth",
        "profitable_pessimistic",
        "estimated_pnl",
        "pnl",
        "fully_executable",
        "fully_executable_at_displayed_depth",
        "executable",
        "filter_rejected",
        "limiting_leg",
        "book_staleness_ms",
        "average_book_staleness_ms",
        "max_book_staleness_ms",
        "timestamp_local",
        "evaluated_at",
        "detected_at",
    }
)
_STREAM_LATENCY_FIELDS = frozenset(
    {
        "signal_id",
        "opportunity_id",
        "cycle_id",
        "ghost_arbitrage",
        "ghost_arbitrage_flag",
        "ghost",
        "lifetime_ms",
        "signal_lifetime_ms",
        "timestamp_local",
        "checked_at",
    }
)
_STREAM_CHECK_FIELDS = frozenset(
    {"delay_ms", "elapsed_ms", "edge", "net_return", "profitable", "executable", "pnl"}
)


def _compact_stream_record(category: str, value: Any) -> dict[str, Any]:
    """Copy only report-relevant fields from a just-persisted recorder value."""

    # Recorder mappings are already JSON-serializable enough for the durable
    # writer. Avoid recursively copying large nested simulation-leg payloads
    # that this streaming summary deliberately does not retain.
    plain = value if isinstance(value, Mapping) else _plain(value)
    if not isinstance(plain, Mapping):
        return {"_category": category, "value": plain}
    if category in {"signal", "opportunity"}:
        record = {key: plain[key] for key in _STREAM_SIGNAL_FIELDS if key in plain}
    elif category == "latency":
        record = {key: plain[key] for key in _STREAM_LATENCY_FIELDS if key in plain}
        checks = plain.get("checks")
        if isinstance(checks, Sequence) and not isinstance(checks, (str, bytes)):
            record["checks"] = [
                {key: check[key] for key in _STREAM_CHECK_FIELDS if key in check}
                for check in checks
                if isinstance(check, Mapping)
            ]
        # Older latency records may expose bucket values as flat keys.
        for key, item in plain.items():
            if key.startswith(("return_after_", "profitable_after_", "elapsed_after_")):
                record[key] = item
    else:
        record = dict(plain)
    record["_category"] = category
    return record


def _nested_counter_values(
    value: Any,
    needles: set[str] | frozenset[str],
) -> list[int]:
    found: list[int] = []
    if not isinstance(value, Mapping):
        return found
    for key, item in value.items():
        normalised_key = str(key).lower().replace("-", "_").replace(" ", "_")
        if normalised_key in needles:
            number = _float(item)
            if number is not None:
                found.append(int(number))
        elif isinstance(item, Mapping):
            found.extend(_nested_counter_values(item, needles))
    return found


def _best_raw_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "cycle_id": str(_first(record, "cycle_id", "initial_result.cycle_id", default="unknown")),
        "start_size": _first(
            record,
            "start_size",
            "start_amount",
            "initial_result.start_amount",
        ),
        "raw_edge": _record_raw_edge(record),
        "edge_after_fees": _record_fee_edge(record),
        "estimated_pnl": _record_pnl(record),
        "limiting_leg": _first(
            record,
            "limiting_leg",
            "depth_simulation.limiting_leg",
            default=None,
        ),
        "book_staleness_ms": _float(
            _first(
                record,
                "book_staleness_ms",
                "max_book_staleness_ms",
                default=None,
            )
        ),
    }


@dataclass(frozen=True, slots=True)
class StreamingReportSnapshot:
    """Immutable, bounded snapshot that can be evaluated in a worker thread."""

    records: tuple[dict[str, Any], ...]
    state: Mapping[str, Any]
    sample_limit: int

    def calculate_metrics(
        self,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        metrics = calculate_metrics(self.records, metadata)
        return _apply_streaming_metrics(
            metrics,
            self.state,
            metadata or {},
            sample_limit=self.sample_limit,
        )


class StreamingReportAccumulator:
    """Bounded-memory cumulative report state for live multi-day simulations.

    Counts, sums, extrema, fee-sensitivity counts, top opportunities, per-cycle
    aggregates, and latency/health counters are exact since process start.
    Percentiles and medians use deterministic fixed-size reservoir samples.
    """

    _RESYNC_KEYS = frozenset(
        {
            "resync",
            "resyncs",
            "resync_count",
            "book_resyncs",
            "order_book_resyncs",
        }
    )
    _GAP_KEYS = frozenset(
        {
            "gap_count",
            "gaps",
            "sequence_gap_count",
            "sequence_gaps",
            "book_sequence_gaps",
            "order_book_sequence_gaps",
        }
    )

    def __init__(
        self,
        *,
        fee_sensitivity_rates: Sequence[Any] = DIAGNOSTIC_FEE_RATES,
        latency_buckets_ms: Sequence[int] = STANDARD_LATENCY_BUCKETS,
        sample_limit: int = 4_096,
        latency_lateness_tolerance_ms: float = 25.0,
    ) -> None:
        if sample_limit <= 0:
            raise ValueError("sample_limit must be positive")
        self.sample_limit = int(sample_limit)
        self.fee_rates = tuple(
            rate
            for item in fee_sensitivity_rates
            if (rate := _float(item)) is not None and 0 <= rate < 1
        )
        if not self.fee_rates:
            self.fee_rates = DIAGNOSTIC_FEE_RATES
        self.latency_buckets = {int(bucket) for bucket in latency_buckets_ms}
        self.lateness_tolerance_ms = max(0.0, float(latency_lateness_tolerance_ms))
        self._signal_sample = _BoundedReservoir(self.sample_limit, 0x51A1)
        self._latency_sample = _BoundedReservoir(self.sample_limit, 0x1A7E)
        self._total_records = 0
        self._signal_count = 0
        self._latency_count = 0
        self._health_count = 0
        self._fee_profitable = 0
        self._depth_profitable = 0
        self._pessimistic_profitable = 0
        self._pessimistic_observations = 0
        self._non_executable = 0
        self._filter_rejected = 0
        self._ghosts = 0
        self._raw = _empty_running_stats()
        self._net = _empty_running_stats()
        self._realistic = _empty_running_stats()
        self._pnl = _empty_running_stats()
        self._break_even = _empty_running_stats()
        self._staleness = _empty_running_stats()
        self._lifetime = _empty_running_stats()
        self._fee_sensitivity = {
            rate: {"sample_count": 0, "profitable_count": 0} for rate in self.fee_rates
        }
        self._latency: dict[int, dict[str, float | int | None]] = {}
        self._cycles: dict[str, dict[str, float | int | None]] = {}
        self._top_raw: list[tuple[tuple[float, int], dict[str, Any]]] = []
        self._sequence = 0
        self._first_health: dict[str, Any] | None = None
        self._latest_health: dict[str, Any] | None = None
        self._explicit_resyncs = 0
        self._explicit_gaps = 0
        self._cumulative_resyncs = 0
        self._cumulative_gaps = 0
        self._checkpoint_count = 0
        self._positive_checkpoint_count = 0

    @staticmethod
    def _empty_cycle() -> dict[str, float | int | None]:
        return {
            "signals": 0,
            "pnl_sum": 0.0,
            "pnl_count": 0,
            "raw_sum": 0.0,
            "raw_count": 0,
            "raw_max": None,
            "net_sum": 0.0,
            "net_count": 0,
            "net_max": None,
            "ghosts": 0,
            "ghost_samples": 0,
            "positive_realistic_signals": 0,
        }

    @staticmethod
    def _empty_latency() -> dict[str, float | int | None]:
        return {
            "samples": 0,
            "profitable": 0,
            "elapsed_sum": 0.0,
            "elapsed_count": 0,
            "lateness_sum": 0.0,
            "lateness_count": 0,
            "late_count": 0,
            "max_lateness": None,
        }

    def observe(self, category: str, value: Any) -> None:
        """Observe a durable recorder append; non-canonical stages are ignored."""

        category = str(category).strip().lower()
        if category not in {
            "signal",
            "opportunity",
            "latency",
            "health",
            "book_health",
        }:
            return
        record = _compact_stream_record(category, value)
        self._total_records += 1
        if category in {"signal", "opportunity"}:
            self._observe_signal(record)
        elif category == "latency":
            self._observe_latency(record)
        else:
            self._observe_health(record)

    def _observe_signal(self, record: dict[str, Any]) -> None:
        self._signal_count += 1
        self._signal_sample.add(record)
        self._sequence += 1
        raw_edge = _record_raw_edge(record)
        net_edge = _record_fee_edge(record)
        executable = _record_executable(record)
        realistic_edge = _record_edge(record) if executable else None
        pnl = _record_pnl(record) if executable else None
        _observe_running_stats(self._raw, raw_edge)
        _observe_running_stats(self._net, net_edge)
        _observe_running_stats(self._realistic, realistic_edge)
        _observe_running_stats(self._pnl, pnl)
        break_even = _break_even_fee_per_leg(raw_edge) if raw_edge is not None else None
        _observe_running_stats(self._break_even, break_even)
        for value in _collect_staleness(record):
            _observe_running_stats(self._staleness, value)

        self._fee_profitable += int((net_edge or 0) > 0)
        self._depth_profitable += int(executable and (realistic_edge or 0) > 0)
        pessimistic_value = _first(
            record,
            "profitable_pessimistic",
            "pessimistic_return",
        )
        if pessimistic_value is not None:
            self._pessimistic_observations += 1
        self._pessimistic_profitable += int(
            _bool(
                _first(record, "profitable_pessimistic"),
                (_float(_first(record, "pessimistic_return")) or 0) > 0,
            )
        )
        self._non_executable += int(not executable)
        self._filter_rejected += int(_bool(record.get("filter_rejected"), False))

        if raw_edge is not None:
            for rate, values in self._fee_sensitivity.items():
                theoretical = _theoretical_edge_after_fee(raw_edge, rate)
                if theoretical is None:
                    continue
                values["sample_count"] += 1
                values["profitable_count"] += int(theoretical > 0)
            key = (raw_edge, -self._sequence)
            item = (key, record)
            if len(self._top_raw) < 20:
                heapq.heappush(self._top_raw, item)
            elif key > self._top_raw[0][0]:
                heapq.heapreplace(self._top_raw, item)

        cycle_id = str(_first(record, "cycle_id", default="unknown"))
        cycle = self._cycles.setdefault(cycle_id, self._empty_cycle())
        cycle["signals"] += 1
        if raw_edge is not None:
            cycle["raw_sum"] += raw_edge
            cycle["raw_count"] += 1
            cycle["raw_max"] = (
                raw_edge if cycle["raw_max"] is None else max(float(cycle["raw_max"]), raw_edge)
            )
        if net_edge is not None:
            cycle["net_sum"] += net_edge
            cycle["net_count"] += 1
            cycle["net_max"] = (
                net_edge if cycle["net_max"] is None else max(float(cycle["net_max"]), net_edge)
            )
        if pnl is not None:
            cycle["pnl_sum"] += pnl
            cycle["pnl_count"] += 1
        depth_positive = _bool(
            _first(record, "profitable_after_depth"),
            (realistic_edge or 0) > 0,
        )
        pessimistic_positive = _bool(
            _first(record, "profitable_pessimistic"),
            (_float(_first(record, "pessimistic_return")) or 0) > 0,
        )
        cycle["positive_realistic_signals"] += int(depth_positive and pessimistic_positive)

    def _observe_latency(self, record: dict[str, Any]) -> None:
        self._latency_count += 1
        self._latency_sample.add(record)
        ghost_value = _first(
            record,
            "ghost_arbitrage",
            "ghost_arbitrage_flag",
            "ghost",
        )
        self._ghosts += int(_bool(ghost_value, False))
        _observe_running_stats(
            self._lifetime,
            _float(_first(record, "lifetime_ms", "signal_lifetime_ms")),
        )
        checks = record.get("checks")
        if isinstance(checks, Sequence) and not isinstance(checks, (str, bytes)):
            for check in checks:
                if not isinstance(check, Mapping):
                    continue
                delay = _float(check.get("delay_ms"))
                if delay is not None:
                    self.latency_buckets.add(int(delay))
        for bucket in self.latency_buckets:
            _, profitable = _latency_value(record, bucket)
            if profitable is None:
                continue
            values = self._latency.setdefault(bucket, self._empty_latency())
            values["samples"] += 1
            values["profitable"] += int(profitable)
            elapsed = _latency_elapsed(record, bucket)
            if elapsed is None:
                continue
            lateness = elapsed - bucket
            values["elapsed_sum"] += elapsed
            values["elapsed_count"] += 1
            values["lateness_sum"] += lateness
            values["lateness_count"] += 1
            values["late_count"] += int(lateness > self.lateness_tolerance_ms)
            values["max_lateness"] = (
                lateness
                if values["max_lateness"] is None
                else max(float(values["max_lateness"]), lateness)
            )

        cycle_id = str(_first(record, "cycle_id", default="unknown"))
        cycle = self._cycles.setdefault(cycle_id, self._empty_cycle())
        if ghost_value is not None:
            cycle["ghost_samples"] += 1
            cycle["ghosts"] += int(_bool(ghost_value, False))

    def _observe_health(self, record: dict[str, Any]) -> None:
        self._health_count += 1
        if self._first_health is None:
            self._first_health = record
        self._latest_health = record
        event = str(
            _first(record, "event", "event_type", "health_event", "type", default="")
        ).lower()
        self._explicit_resyncs += int("resync" in event)
        self._explicit_gaps += int(
            "sequence_gap" in event or "sequence gap" in event or event == "gap"
        )
        self._cumulative_resyncs = max(
            self._cumulative_resyncs,
            max(_nested_counter_values(record, self._RESYNC_KEYS), default=0),
        )
        self._cumulative_gaps = max(
            self._cumulative_gaps,
            max(_nested_counter_values(record, self._GAP_KEYS), default=0),
        )

    def checkpoint_metadata(self) -> dict[str, int]:
        return {
            "checkpoint_count": self._checkpoint_count,
            "positive_checkpoint_count": self._positive_checkpoint_count,
        }

    def aggregation_metadata(self) -> dict[str, Any]:
        """Return the persisted contract for interpreting live report precision."""

        return {
            "report_aggregation": {
                "mode": "cumulative_streaming_bounded_sample",
                "sample_limit_per_record_type": self.sample_limit,
                "exact": (
                    "counts",
                    "sums",
                    "extrema",
                    "fee_sensitivity",
                    "top_opportunities",
                    "per_cycle_totals",
                ),
                "approximate": ("percentiles", "medians"),
            }
        }

    def record_checkpoint(
        self,
        metrics: dict[str, Any],
        metadata: Mapping[str, Any],
    ) -> dict[str, int]:
        """Record one checkpoint immediately (convenience API for callers/tests)."""

        checkpoint = self.prepare_checkpoint(metrics, metadata)
        self.commit_checkpoint(checkpoint)
        return checkpoint

    def prepare_checkpoint(
        self,
        metrics: dict[str, Any],
        metadata: Mapping[str, Any],
    ) -> dict[str, int]:
        """Preview history fields so a report can persist them before commit."""

        thresholds = (
            metadata.get("decision") if isinstance(metadata.get("decision"), Mapping) else {}
        )
        checkpoint_count = self._checkpoint_count + 1
        positive_checkpoint_count = self._positive_checkpoint_count + int(
            _positive_checkpoint_evidence(metrics, thresholds)
        )
        _apply_checkpoint_history(
            metrics,
            thresholds,
            checkpoint_count=checkpoint_count,
            positive_checkpoint_count=positive_checkpoint_count,
        )
        return {
            "checkpoint_count": checkpoint_count,
            "positive_checkpoint_count": positive_checkpoint_count,
        }

    def commit_checkpoint(self, checkpoint: Mapping[str, Any]) -> None:
        """Commit history only after its checkpoint artifacts were published."""

        checkpoint_count = int(checkpoint["checkpoint_count"])
        if checkpoint_count != self._checkpoint_count + 1:
            raise ValueError("checkpoint history commit is out of sequence")
        self._checkpoint_count = checkpoint_count
        self._positive_checkpoint_count = int(checkpoint["positive_checkpoint_count"])

    def snapshot(self) -> StreamingReportSnapshot:
        health_records: tuple[dict[str, Any], ...] = ()
        if self._first_health is not None:
            health_records = (self._first_health,)
            if self._latest_health is not None and self._latest_health is not self._first_health:
                health_records += (self._latest_health,)
        state = {
            "total_records": self._total_records,
            "signal_count": self._signal_count,
            "latency_count": self._latency_count,
            "health_count": self._health_count,
            "fee_profitable": self._fee_profitable,
            "depth_profitable": self._depth_profitable,
            "pessimistic_profitable": self._pessimistic_profitable,
            "pessimistic_observations": self._pessimistic_observations,
            "non_executable": self._non_executable,
            "filter_rejected": self._filter_rejected,
            "ghosts": self._ghosts,
            "raw": dict(self._raw),
            "net": dict(self._net),
            "realistic": dict(self._realistic),
            "pnl": dict(self._pnl),
            "break_even": dict(self._break_even),
            "staleness": dict(self._staleness),
            "lifetime": dict(self._lifetime),
            "fee_rates": self.fee_rates,
            "fee_sensitivity": {
                rate: dict(values) for rate, values in self._fee_sensitivity.items()
            },
            "latency": {bucket: dict(values) for bucket, values in self._latency.items()},
            "latency_buckets": tuple(sorted(self.latency_buckets)),
            "lateness_tolerance_ms": self.lateness_tolerance_ms,
            "cycles": {cycle_id: dict(values) for cycle_id, values in self._cycles.items()},
            "top_raw": tuple(
                record
                for _, record in sorted(self._top_raw, key=lambda item: item[0], reverse=True)
            ),
            "resyncs": max(self._explicit_resyncs, self._cumulative_resyncs),
            "gaps": max(self._explicit_gaps, self._cumulative_gaps),
            "checkpoint_count": self._checkpoint_count,
            "positive_checkpoint_count": self._positive_checkpoint_count,
            "signal_sample_size": len(self._signal_sample.snapshot()),
            "latency_sample_size": len(self._latency_sample.snapshot()),
        }
        return StreamingReportSnapshot(
            records=(
                *self._signal_sample.snapshot(),
                *self._latency_sample.snapshot(),
                *health_records,
            ),
            state=state,
            sample_limit=self.sample_limit,
        )


def _apply_checkpoint_history(
    metrics: dict[str, Any],
    thresholds: Mapping[str, Any],
    *,
    checkpoint_count: int,
    positive_checkpoint_count: int,
) -> None:
    metrics["checkpoint_count"] = checkpoint_count
    metrics["positive_checkpoint_count"] = positive_checkpoint_count
    metrics["minimum_positive_checkpoints"] = max(
        2,
        int(_float(thresholds.get("minimum_positive_checkpoints")) or 2),
    )
    decision, rationale = _decision_48h(metrics, thresholds)
    metrics["decision_48h"] = decision
    metrics["decision_48h_rationale"] = rationale


def _streaming_cycle_metrics(
    state: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], int
]:
    cycles = {str(cycle_id): dict(values) for cycle_id, values in state.get("cycles", {}).items()}
    monitored = metadata.get("monitored_cycle_ids", ())
    if isinstance(monitored, Sequence) and not isinstance(monitored, (str, bytes)):
        for cycle_id in monitored:
            cycles.setdefault(str(cycle_id), StreamingReportAccumulator._empty_cycle())
    ranked: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    thresholds = metadata.get("decision") if isinstance(metadata.get("decision"), Mapping) else {}
    repeated_minimum = max(
        1,
        int(_float(thresholds.get("repeated_positive_cycle_min_signals")) or 2),
    )
    repeated = 0
    for cycle_id, values in cycles.items():
        signals = int(values["signals"])
        pnl_count = int(values["pnl_count"])
        raw_count = int(values["raw_count"])
        net_count = int(values["net_count"])
        ghost_samples = int(values["ghost_samples"])
        if signals:
            ranked.append(
                {
                    "cycle_id": cycle_id,
                    "signals": signals,
                    "total_pnl": float(values["pnl_sum"]),
                    "average_pnl": float(values["pnl_sum"]) / pnl_count if pnl_count else 0.0,
                    "ghost_percentage": int(values["ghosts"]) / signals * 100,
                }
            )
        diagnostics.append(
            {
                "cycle_id": cycle_id,
                "signals": signals,
                "average_raw_edge": float(values["raw_sum"]) / raw_count if raw_count else None,
                "max_raw_edge": values["raw_max"],
                "average_net_edge": float(values["net_sum"]) / net_count if net_count else None,
                "best_net_edge": values["net_max"],
                "ghost_percentage": int(values["ghosts"]) / ghost_samples * 100
                if ghost_samples
                else None,
                "ghost_samples": ghost_samples,
            }
        )
        repeated += int(int(values["positive_realistic_signals"]) >= repeated_minimum)
    diagnostics.sort(key=lambda item: item["cycle_id"])
    best = sorted(ranked, key=lambda item: item["total_pnl"], reverse=True)[:5]
    worst = sorted(ranked, key=lambda item: item["total_pnl"])[:5]
    noisy = sorted(
        ranked,
        key=lambda item: (item["ghost_percentage"], item["signals"]),
        reverse=True,
    )[:5]
    return best, worst, noisy, diagnostics, repeated


def _apply_streaming_metrics(
    metrics: dict[str, Any],
    state: Mapping[str, Any],
    metadata: Mapping[str, Any],
    *,
    sample_limit: int,
) -> dict[str, Any]:
    """Overlay exact cumulative aggregates on bounded-sample distributions."""

    raw = state["raw"]
    net = state["net"]
    realistic = state["realistic"]
    pnl = state["pnl"]
    break_even = state["break_even"]
    staleness = state["staleness"]
    lifetime = state["lifetime"]
    signal_count = int(state["signal_count"])
    latency_count = int(state["latency_count"])
    raw_count = int(raw["count"])
    net_count = int(net["count"])
    raw_distribution = dict(metrics["raw_edge_percentiles"])
    raw_distribution.update({"sample_count": raw_count, "min": raw["min"], "max": raw["max"]})
    net_distribution = dict(metrics["net_edge_percentiles_after_fees"])
    net_distribution.update({"sample_count": net_count, "min": net["min"], "max": net["max"]})
    break_even_stats = dict(metrics["break_even_fee_per_leg"])
    break_even_count = int(break_even["count"])
    break_even_stats.update(
        {
            "sample_count": break_even_count,
            "average": float(break_even["sum"]) / break_even_count if break_even_count else None,
            "max": break_even["max"],
        }
    )
    fee_sensitivity = []
    for rate in state["fee_rates"]:
        values = state["fee_sensitivity"][rate]
        sample_count = int(values["sample_count"])
        profitable_count = int(values["profitable_count"])
        fee_sensitivity.append(
            {
                "fee_rate": rate,
                "profitable_count": profitable_count,
                "sample_count": sample_count,
                "profitable_percentage": profitable_count / sample_count * 100
                if sample_count
                else 0.0,
            }
        )

    latency_state = state["latency"]
    buckets = tuple(state["latency_buckets"])
    latency_counts = {
        bucket: int(latency_state.get(bucket, {}).get("profitable", 0)) for bucket in buckets
    }
    latency_samples = {
        bucket: int(latency_state.get(bucket, {}).get("samples", 0)) for bucket in buckets
    }
    average_actual: dict[int, float] = {}
    average_lateness: dict[int, float] = {}
    median_actual: dict[int, float] = {}
    median_lateness: dict[int, float] = {}
    late_counts: dict[int, int] = {}
    total_lateness = 0.0
    timing_count = 0
    maximum_lateness = 0.0
    for bucket in buckets:
        values = latency_state.get(bucket, {})
        elapsed_count = int(values.get("elapsed_count", 0))
        lateness_count = int(values.get("lateness_count", 0))
        average_actual[bucket] = (
            float(values.get("elapsed_sum", 0.0)) / elapsed_count if elapsed_count else 0.0
        )
        average_lateness[bucket] = (
            float(values.get("lateness_sum", 0.0)) / lateness_count if lateness_count else 0.0
        )
        median_actual[bucket] = float(metrics["latency_median_actual_ms"].get(bucket, 0.0))
        median_lateness[bucket] = float(metrics["latency_median_lateness_ms"].get(bucket, 0.0))
        late_counts[bucket] = int(values.get("late_count", 0))
        total_lateness += float(values.get("lateness_sum", 0.0))
        timing_count += lateness_count
        observed_max = _float(values.get("max_lateness"))
        if observed_max is not None:
            maximum_lateness = max(maximum_lateness, observed_max)

    total_latency_checks = sum(latency_samples.values())
    late_check_count = sum(late_counts.values())
    slowest = buckets[-1] if buckets else 0
    covered = min(signal_count, latency_samples.get(slowest, 0))
    coverage = covered / signal_count if signal_count else 0.0
    min_sample_size = max(
        1,
        int(_float(metadata.get("minimum_decision_sample")) or 20),
    )
    meaningful_coverage = bool(signal_count and (coverage >= 0.9 or covered >= min_sample_size))
    tolerance = float(state["lateness_tolerance_ms"])
    scheduling_accurate = bool(
        total_latency_checks
        and timing_count >= total_latency_checks
        and maximum_lateness <= tolerance
    )
    best, worst, noisy, diagnostics, repeated = _streaming_cycle_metrics(
        state,
        metadata,
    )
    scanner_stats = (
        metadata.get("scanner_stats") if isinstance(metadata.get("scanner_stats"), Mapping) else {}
    )
    unique_signals = int(_float(scanner_stats.get("unique_signals")) or signal_count)

    metrics.update(
        {
            "aggregation_mode": "cumulative_streaming_bounded_sample",
            "quantiles_approximate": bool(
                signal_count > int(state["signal_sample_size"])
                or latency_count > int(state["latency_sample_size"])
            ),
            "quantile_sample_limit": sample_limit,
            "signal_quantile_sample_size": int(state["signal_sample_size"]),
            "latency_quantile_sample_size": int(state["latency_sample_size"]),
            "total_records": int(state["total_records"]),
            "parse_errors": 0,
            "raw_opportunities": signal_count,
            "unique_signals": unique_signals,
            "profitable_after_fees": int(state["fee_profitable"]),
            "profitable_after_depth": int(state["depth_profitable"]),
            "profitable_pessimistic": int(state["pessimistic_profitable"]),
            "pessimistic_observations": int(state["pessimistic_observations"]),
            "non_executable_signals": int(state["non_executable"]),
            "filter_rejected_signals": int(state["filter_rejected"]),
            "latency_observations": latency_count,
            "latency_covered_signals": covered,
            "latency_coverage": coverage,
            "latency_coverage_percentage": coverage * 100,
            "meaningful_latency_coverage": meaningful_coverage,
            "latency_profitable_counts": latency_counts,
            "latency_sample_counts": latency_samples,
            "latency_average_actual_ms": average_actual,
            "latency_median_actual_ms": median_actual,
            "latency_average_lateness_ms": average_lateness,
            "latency_median_lateness_ms": median_lateness,
            "latency_late_counts": late_counts,
            "latency_total_check_count": total_latency_checks,
            "latency_late_check_count": late_check_count,
            "latency_late_check_percentage": late_check_count / total_latency_checks * 100
            if total_latency_checks
            else None,
            "latency_timing_sample_count": timing_count,
            "latency_lateness_tolerance_ms": tolerance,
            "average_latency_lateness_ms": total_lateness / timing_count if timing_count else 0.0,
            "maximum_latency_lateness_ms": maximum_lateness,
            "latency_scheduling_accurate": scheduling_accurate,
            "profitable_at_slowest_latency": latency_counts.get(slowest, 0),
            "slowest_latency_bucket_ms": slowest,
            "average_edge": float(realistic["sum"]) / int(realistic["count"])
            if int(realistic["count"])
            else 0.0,
            "best_realistic_edge": realistic["max"],
            "average_raw_edge": float(raw["sum"]) / raw_count if raw_count else 0.0,
            "best_net_edge": net["max"],
            "raw_edge_percentiles": raw_distribution,
            "net_edge_percentiles_after_fees": net_distribution,
            "break_even_fee_per_leg": break_even_stats,
            "fee_sensitivity": fee_sensitivity,
            "fee_sensitivity_rates": tuple(state["fee_rates"]),
            "best_raw_opportunities": [_best_raw_summary(record) for record in state["top_raw"]],
            "average_estimated_pnl": float(pnl["sum"]) / int(pnl["count"])
            if int(pnl["count"])
            else 0.0,
            "total_estimated_pnl": float(pnl["sum"]),
            "best_cycles": best,
            "worst_cycles": worst,
            "noisy_cycles": noisy,
            "per_cycle_diagnostics": diagnostics,
            "repeated_positive_cycles": repeated,
            "lifetime_distribution_ms": {
                "minimum": lifetime["min"] if lifetime["min"] is not None else 0.0,
                "median": metrics["lifetime_distribution_ms"]["median"],
                "p90": metrics["lifetime_distribution_ms"]["p90"],
                "maximum": lifetime["max"] if lifetime["max"] is not None else 0.0,
            },
            "ghost_arbitrage_count": int(state["ghosts"]),
            "ghost_arbitrage_percentage": int(state["ghosts"]) / latency_count * 100
            if latency_count
            else 0.0,
            "average_book_staleness_ms": float(staleness["sum"]) / int(staleness["count"])
            if int(staleness["count"])
            else None,
            "book_staleness_sample_count": int(staleness["count"]),
            "order_book_resyncs": int(state["resyncs"]),
            "sequence_gaps": int(state["gaps"]),
        }
    )
    for key in (
        "data_dir",
        "data_drive",
        "storage_mode",
        "min_free_gib",
        "free_gib_at_start",
        "raw_signal_sample_rate",
        "top_n_retention",
        "near_break_even_threshold",
        "checkpoint_interval_minutes",
        "compact_mode_active",
        "latest_report_path",
    ):
        metrics[key] = metadata.get(key)
    thresholds = metadata.get("decision") if isinstance(metadata.get("decision"), Mapping) else {}
    conclusion, rationale = _decision(metrics, min_sample_size)
    metrics["conclusion"] = conclusion
    metrics["conclusion_rationale"] = rationale
    metrics["minimum_decision_sample"] = min_sample_size
    _apply_checkpoint_history(
        metrics,
        thresholds,
        checkpoint_count=int(state["checkpoint_count"]),
        positive_checkpoint_count=int(state["positive_checkpoint_count"]),
    )
    return metrics


def _fmt_number(value: Any, digits: int = 4) -> str:
    number = _float(value)
    return f"{number:,.{digits}f}" if number is not None else "n/a"


def _fmt_percent(fraction: Any, digits: int = 4) -> str:
    number = _float(fraction)
    return f"{number * 100:.{digits}f}%" if number is not None else "n/a"


def _fmt_percentage_points(value: Any, digits: int = 2) -> str:
    number = _float(value)
    return f"{number:.{digits}f}%" if number is not None else "n/a"


def _fmt_text(value: Any) -> str:
    return "n/a" if value is None or value == "" else _escape(value)


def _escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _cycle_table(cycles: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = ["| Cycle | Signals | Total PnL | Average PnL | Ghosts |", "|---|---:|---:|---:|---:|"]
    if not cycles:
        lines.append("| _No cycle data_ | 0 | 0 | 0 | 0% |")
    for item in cycles:
        lines.append(
            f"| {_escape(item['cycle_id'])} | {item['signals']} | "
            f"{_fmt_number(item['total_pnl'], 8)} | {_fmt_number(item['average_pnl'], 8)} | "
            f"{_fmt_number(item['ghost_percentage'], 2)}% |"
        )
    return lines


def _best_raw_table(opportunities: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [
        "| Cycle | Start size | Raw edge | Edge after fees | Estimated PnL | Limiting leg | Book staleness |",
        "|---|---:|---:|---:|---:|---|---:|",
    ]
    if not opportunities:
        lines.append("| _No raw opportunities_ | n/a | n/a | n/a | n/a | n/a | n/a |")
    for item in opportunities:
        lines.append(
            f"| {_fmt_text(item.get('cycle_id'))} | {_fmt_text(item.get('start_size'))} | "
            f"{_fmt_percent(item.get('raw_edge'), 8)} | "
            f"{_fmt_percent(item.get('edge_after_fees'), 8)} | "
            f"{_fmt_number(item.get('estimated_pnl'), 8)} | "
            f"{_fmt_text(item.get('limiting_leg'))} | "
            f"{_fmt_number(item.get('book_staleness_ms'), 1)} ms |"
        )
    return lines


def _per_cycle_diagnostics_table(cycles: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [
        "| Cycle | Signals | Avg raw edge | Max raw edge | Avg net edge | Best net edge | Ghosts |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    if not cycles:
        lines.append("| _No cycle data_ | 0 | n/a | n/a | n/a | n/a | n/a |")
    for item in cycles:
        lines.append(
            f"| {_fmt_text(item.get('cycle_id'))} | {item.get('signals', 0)} | "
            f"{_fmt_percent(item.get('average_raw_edge'), 6)} | "
            f"{_fmt_percent(item.get('max_raw_edge'), 6)} | "
            f"{_fmt_percent(item.get('average_net_edge'), 6)} | "
            f"{_fmt_percent(item.get('best_net_edge'), 6)} | "
            f"{_fmt_percentage_points(item.get('ghost_percentage'))} |"
        )
    return lines


def render_markdown(metrics: Mapping[str, Any], metadata: Mapping[str, Any] | None = None) -> str:
    latency_counts = metrics["latency_profitable_counts"]
    latency_samples = metrics["latency_sample_counts"]
    exchange_name = str(metrics.get("exchange", "Unknown Spot exchange"))
    is_mexc = exchange_name.lower().startswith("mexc")
    assumptions = metrics.get("assumptions", {})
    account_fee_schedule = metrics.get(
        "fee_source"
    ) == "mexc_account_tradeFee_read_only" and isinstance(assumptions, Mapping)
    fee_label = (
        "Maximum/fallback taker fee per leg"
        if account_fee_schedule
        else "Simulated taker fee per leg"
    )
    account_fee_rows: list[str] = []
    if account_fee_schedule:
        minimum_fee = assumptions.get("account_fee_minimum_taker_fee")
        maximum_fee = assumptions.get("account_fee_maximum_taker_fee")
        fee_range = (
            f"{_fmt_percent(minimum_fee, 5)} to {_fmt_percent(maximum_fee, 5)}"
            if minimum_fee is not None and maximum_fee is not None
            else "n/a"
        )
        account_fee_rows = [
            f"| Account fee schedule symbols loaded | "
            f"{int(_float(assumptions.get('account_fee_symbol_count')) or 0)} |",
            f"| Monitored symbols with account fees | "
            f"{int(_float(assumptions.get('reported_symbol_fee_count')) or 0)} |",
            f"| Symbol-specific taker fee range | {fee_range} |",
            f"| Account fee schedule generated | "
            f"{_escape(assumptions.get('account_fee_generated_at') or 'n/a')} |",
        ]
    lines = [
        f"# {exchange_name} Triangular Arbitrage Simulation Report",
        "",
        f"Generated: {datetime.now(UTC).isoformat()}",
        "",
        "> Research-only paper simulation. This report does not prove live profitability.",
        "",
        *(
            [
                "> Cumulative streaming report: counts, sums, and extrema are exact since "
                "process start; percentiles and medians use a bounded deterministic sample.",
                "",
            ]
            if metrics.get("aggregation_mode") == "cumulative_streaming_bounded_sample"
            else []
        ),
        "## Executive summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Exchange | {_escape(exchange_name)} |",
        f"| {fee_label} | {_fmt_percent(metrics.get('fee_assumption_per_leg'), 5)} |",
        f"| Fee source | {_escape(metrics.get('fee_source', 'config_fee'))} |",
        *account_fee_rows,
        f"| Run duration | {_fmt_number(metrics['run_duration_minutes'], 2)} minutes |",
        f"| Symbols monitored | {metrics['monitored_symbols']} |",
        f"| Cycles monitored | {metrics['monitored_cycles']} |",
        f"| Unique raw opportunity signals | {metrics['raw_opportunities']} |",
        f"| Raw opportunity scan observations | {metrics['raw_opportunity_observations']} |",
        f"| Profitable after fees | {metrics['profitable_after_fees']} |",
        f"| Profitable after displayed depth | {metrics['profitable_after_depth']} |",
        f"| Profitable under pessimistic model | {metrics['profitable_pessimistic']} / {metrics['pessimistic_observations']} observed |",
        f"| Non-executable / filter-rejected signals | {metrics['non_executable_signals']} / {metrics['filter_rejected_signals']} |",
        f"| Slowest-bucket latency coverage | {metrics['latency_covered_signals']} / {metrics['raw_opportunities']} signals ({_fmt_number(metrics['latency_coverage_percentage'], 2)}%) |",
        f"| Latency scheduling tolerance / maximum lateness | {_fmt_number(metrics['latency_lateness_tolerance_ms'], 1)} / {_fmt_number(metrics['maximum_latency_lateness_ms'], 1)} ms |",
        f"| Scanner deadline misses | {metrics['scan_deadline_misses']} |",
        f"| Average / median realistic edge | {_fmt_percent(metrics['average_edge'])} / {_fmt_percent(metrics['median_edge'])} |",
        f"| Best net edge after fees | {_fmt_percent(metrics['best_net_edge'])} |",
        f"| Average / median estimated PnL | {_fmt_number(metrics['average_estimated_pnl'], 8)} / {_fmt_number(metrics['median_estimated_pnl'], 8)} |",
        f"| Total diagnostic estimated PnL | {_fmt_number(metrics['total_estimated_pnl'], 8)} |",
        f"| Ghost arbitrage | {_fmt_number(metrics['ghost_arbitrage_percentage'], 2)}% ({metrics['ghost_arbitrage_count']}) |",
        f"| Average book staleness at signal detection | {_fmt_number(metrics['average_book_staleness_ms'], 2)} ms |",
        f"| Resyncs / sequence gaps | {metrics['order_book_resyncs']} / {metrics['sequence_gaps']} |",
        f"| Positive periodic checkpoints | {metrics.get('positive_checkpoint_count', 0)} / "
        f"{metrics.get('checkpoint_count', 0)} |",
        "",
        "## Diagnostics",
        "",
        "### Raw and net edge percentiles",
        "",
        "Net edge is the recorded edge after the configured taker fee on all three legs.",
        "",
        *(
            [
                f"Online percentile and median values use a reservoir of "
                f"{metrics.get('signal_quantile_sample_size', 0):,} sampled signals out of "
                f"{metrics['raw_opportunities']:,} cumulative signals (limit "
                f"{metrics.get('quantile_sample_limit', 0):,}). The Samples column is the exact "
                "cumulative population; minima, maxima, counts, sums, and averages remain exact.",
                "",
            ]
            if metrics.get("aggregation_mode") == "cumulative_streaming_bounded_sample"
            else []
        ),
        "| Stage | Samples | Min | P50 | P90 | P95 | P99 | P99.9 | Max |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    raw_distribution = metrics["raw_edge_percentiles"]
    net_distribution = metrics["net_edge_percentiles_after_fees"]
    for label, distribution in (
        ("Raw top-of-book", raw_distribution),
        ("Net after fees", net_distribution),
    ):
        lines.append(
            f"| {label} | {distribution['sample_count']} | "
            f"{_fmt_percent(distribution['min'], 8)} | {_fmt_percent(distribution['p50'], 8)} | "
            f"{_fmt_percent(distribution['p90'], 8)} | {_fmt_percent(distribution['p95'], 8)} | "
            f"{_fmt_percent(distribution['p99'], 8)} | {_fmt_percent(distribution['p99_9'], 8)} | "
            f"{_fmt_percent(distribution['max'], 8)} |"
        )

    break_even = metrics["break_even_fee_per_leg"]
    lines.extend(
        [
            "",
            "### Break-even taker fee per leg",
            "",
            "For each non-negative raw edge, this estimates the largest equal taker fee on each "
            "of three legs that leaves the theoretical cycle at break-even: "
            "`(1 + raw_edge) * (1 - fee) ** 3 = 1`.",
            "",
            "| Samples | Average | Median | P95 | P99 | Maximum |",
            "|---:|---:|---:|---:|---:|---:|",
            f"| {break_even['sample_count']} | {_fmt_percent(break_even['average'], 8)} | "
            f"{_fmt_percent(break_even['median'], 8)} | {_fmt_percent(break_even['p95'], 8)} | "
            f"{_fmt_percent(break_even['p99'], 8)} | {_fmt_percent(break_even['max'], 8)} |",
            "",
            "### Fee sensitivity",
            "",
            "This is a theoretical fee-only recomputation from each raw three-leg multiplier. "
            "It does not replay lot-size rounding, filters, displayed depth, or latency.",
            "",
            "| Taker fee per leg | Fee percent | Profitable | Samples | Share |",
            "|---:|---:|---:|---:|---:|",
        ]
    )
    for item in metrics["fee_sensitivity"]:
        lines.append(
            f"| {item['fee_rate']} | {_fmt_percent(item['fee_rate'], 5)} | "
            f"{item['profitable_count']} | {item['sample_count']} | "
            f"{_fmt_percentage_points(item['profitable_percentage'], 4)} |"
        )

    lines.extend(
        [
            "",
            "### Best 20 raw opportunities",
            "",
            *_best_raw_table(metrics["best_raw_opportunities"]),
            "",
            "A machine-readable copy is saved beside this report as "
            "`top_opportunities_<report-id>.csv`.",
            "",
            "### Per-cycle diagnostics",
            "",
            *_per_cycle_diagnostics_table(metrics["per_cycle_diagnostics"]),
            "",
            "## Latency survival",
            "",
            "| Target | Avg actual | Median lateness | Profitable | Observed | Survival | Late checks |",
            "|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for bucket in sorted(latency_counts):
        surviving = latency_counts[bucket]
        observed = latency_samples[bucket]
        survival = surviving / observed if observed else 0
        lines.append(
            f"| {bucket} ms | {_fmt_number(metrics['latency_average_actual_ms'][bucket], 1)} ms | "
            f"{_fmt_number(metrics['latency_median_lateness_ms'][bucket], 1)} ms | {surviving} | "
            f"{observed} | {survival * 100:.2f}% | {metrics['latency_late_counts'][bucket]} |"
        )
    lifetime = metrics["lifetime_distribution_ms"]
    lines.extend(
        [
            "",
            "## Opportunity lifetime",
            "",
            "| Minimum | Median | P90 | Maximum |",
            "|---:|---:|---:|---:|",
            f"| {_fmt_number(lifetime['minimum'], 1)} ms | {_fmt_number(lifetime['median'], 1)} ms | "
            f"{_fmt_number(lifetime['p90'], 1)} ms | {_fmt_number(lifetime['maximum'], 1)} ms |",
            "",
            "## Best cycles by realistic PnL",
            "",
            *_cycle_table(metrics["best_cycles"]),
            "",
            "## Worst cycles by realistic PnL",
            "",
            *_cycle_table(metrics["worst_cycles"]),
            "",
            "## Noisy cycles",
            "",
            *_cycle_table(metrics["noisy_cycles"]),
            "",
            "## Storage",
            "",
            f"- data_dir: {_escape(metrics.get('data_dir') or 'n/a')}",
            f"- data_drive: {_escape(metrics.get('data_drive') or 'n/a')}",
            f"- storage_mode: {_escape(metrics.get('storage_mode') or 'n/a')}",
            f"- min_free_gib: {_escape(metrics.get('min_free_gib'))}",
            f"- free_gib_at_start: {_escape(metrics.get('free_gib_at_start'))}",
            f"- raw_signal_sample_rate: {_escape(metrics.get('raw_signal_sample_rate'))}",
            f"- top_n_retention: {_escape(metrics.get('top_n_retention'))}",
            f"- near_break_even_threshold: {_escape(metrics.get('near_break_even_threshold'))}",
            f"- checkpoint_interval_minutes: {_escape(metrics.get('checkpoint_interval_minutes'))}",
            f"- compact_mode_active: {_escape(metrics.get('compact_mode_active'))}",
            f"- latest_report_path: {_escape(metrics.get('latest_report_path') or 'n/a')}",
            "",
            "## Assumptions",
            "",
        ]
    )
    if isinstance(assumptions, Mapping) and assumptions:
        for key, value in assumptions.items():
            if key in {"started_at", "ended_at", "assumptions"}:
                continue
            rendered = (
                json.dumps(_plain(value), ensure_ascii=False)
                if isinstance(value, (Mapping, list, tuple))
                else value
            )
            lines.append(f"- {_escape(key).replace('_', ' ').title()}: {_escape(rendered)}")
    else:
        lines.append("- No run assumptions were supplied to the report generator.")
    unhealthy_symbols = metrics.get("startup_unhealthy_symbols", [])
    unhealthy_names = (
        ", ".join(_escape(symbol) for symbol in unhealthy_symbols) if unhealthy_symbols else "none"
    )
    startup_unhealthy = metrics.get("startup_unhealthy_books")
    startup_managed = metrics.get("startup_managed_books")
    startup_summary = (
        f"{startup_unhealthy} of {startup_managed} ({unhealthy_names}; "
        f"{metrics['startup_health_source']})"
        if startup_unhealthy is not None and startup_managed is not None
        else "n/a (no startup health snapshot was available)"
    )
    stale_limit = metrics.get("book_stale_after_ms")
    stale_limit_text = (
        f"; configured unhealthy cutoff {_fmt_number(stale_limit, 1)} ms"
        if stale_limit is not None
        else ""
    )
    lines.extend(
        [
            "",
            "## Data-quality warnings",
            "",
            f"- **Scanner deadline misses:** {metrics['scan_deadline_misses']} of "
            f"{metrics['scanner_scan_count']} scans "
            f"({_fmt_percentage_points(metrics['scan_deadline_miss_percentage'])}); mean scan "
            f"duration {_fmt_number(metrics['average_scan_time_ms'], 2)} ms and maximum "
            f"{_fmt_number(metrics['maximum_scan_time_ms'], 2)} ms. A miss means local cycle "
            "evaluation exceeded its configured cadence; it reduces sampling frequency but is "
            "not itself a dropped exchange message.",
            f"- **Average book staleness at signal detection:** "
            f"{_fmt_number(metrics['average_book_staleness_ms'], 2)} ms across "
            f"{metrics['book_staleness_sample_count']} samples{stale_limit_text}. Staleness is "
            "time since the local book last changed, not one-way network latency; unhealthy "
            "books are excluded from simulation.",
            f"- **Maximum latency lateness:** "
            f"{_fmt_number(metrics['maximum_latency_lateness_ms'], 2)} ms; "
            f"{metrics['latency_late_check_count']} of {metrics['latency_total_check_count']} "
            f"checks ({_fmt_percentage_points(metrics['latency_late_check_percentage'], 4)}) "
            f"exceeded the {_fmt_number(metrics['latency_lateness_tolerance_ms'], 1)} ms "
            "tolerance. Lateness is local scheduler overshoot beyond a target recheck time, not "
            "exchange response latency.",
            f"- **Unhealthy books at startup:** {startup_summary}. Cycles requiring an "
            "unhealthy book are skipped until every required book is synchronized and fresh.",
            "",
            f"- Records read: {metrics['total_records']}",
            f"- Malformed JSONL lines skipped: {metrics['parse_errors']}",
            f"- Decision sample threshold: {metrics['minimum_decision_sample']} raw opportunities",
            f"- Meaningful latency coverage: {'yes' if metrics['meaningful_latency_coverage'] else 'no'}",
            f"- Latency scheduling within tolerance: {'yes' if metrics['latency_scheduling_accurate'] else 'no'}",
            f"- Repeated positive cycles: {metrics['repeated_positive_cycles']}",
            f"- Positive checkpoints toward the 48-hour continuation gate: "
            f"{metrics.get('positive_checkpoint_count', 0)} / "
            f"{metrics.get('minimum_positive_checkpoints', 2)} required "
            f"({metrics.get('checkpoint_count', 0)} checkpoints published)",
            *(
                [
                    "- Streaming aggregation: cumulative counts, sums, extrema, fee-sensitivity "
                    "counts, top opportunities, and per-cycle totals are exact since process "
                    f"start. Percentiles and medians are estimates from at most "
                    f"{metrics.get('quantile_sample_limit', 0)} signal and latency records each "
                    f"(current samples: {metrics.get('signal_quantile_sample_size', 0)} / "
                    f"{metrics.get('latency_quantile_sample_size', 0)})."
                ]
                if metrics.get("aggregation_mode") == "cumulative_streaming_bounded_sample"
                else []
            ),
            *(
                [f"- Observer error: {_escape(metrics['run_error'])}"]
                if metrics["run_error"]
                else []
            ),
            "",
            "## Conclusion",
            "",
            f"**{metrics['conclusion']}** — {metrics['conclusion_rationale']}",
            "",
            "## 48-hour decision",
            "",
            f"**48H_DECISION: {metrics['decision_48h']}** — {metrics['decision_48h_rationale']}",
            "",
            "This conclusion concerns further research only. Displayed liquidity can vanish, queue position is unknown, "
            "and paper fills do not establish live execution performance.",
            *(
                [
                    "",
                    "**MEXC fee warning:** Every fee value in this report is a simulated assumption. "
                    "Verify the actual fee shown on the MEXC account fee page and in trade history "
                    "before considering any live trading.",
                ]
                if is_mexc
                else []
            ),
            "",
        ]
    )
    return "\n".join(str(line) for line in lines)


@dataclass(frozen=True, slots=True)
class ReportArtifacts:
    markdown_path: Path
    csv_path: Path
    metrics: Mapping[str, Any]
    conclusion: str
    top_opportunities_path: Path

    def __iter__(self):
        yield self.markdown_path
        yield self.csv_path


@dataclass(frozen=True, slots=True)
class CheckpointArtifacts:
    checkpoint_path: Path
    latest_path: Path
    latest_summary_path: Path
    metrics: Mapping[str, Any]


def _safe_stamp(value: str) -> str:
    return "".join(character for character in value if character.isalnum() or character in "_-")


def _atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _scalar_metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {
        key: value
        for key, value in metrics.items()
        if not isinstance(value, (Mapping, list, tuple, set))
    }
    for bucket, count in metrics["latency_profitable_counts"].items():
        values[f"profitable_after_{bucket}ms"] = count
        values[f"observed_after_{bucket}ms"] = metrics["latency_sample_counts"][bucket]
        values[f"average_actual_after_{bucket}ms"] = metrics["latency_average_actual_ms"][bucket]
        values[f"median_lateness_after_{bucket}ms"] = metrics["latency_median_lateness_ms"][bucket]
        values[f"late_checks_after_{bucket}ms"] = metrics["latency_late_counts"][bucket]
    for key, value in metrics["raw_edge_percentiles"].items():
        values[f"raw_edge_{key}"] = value
    for key, value in metrics["net_edge_percentiles_after_fees"].items():
        values[f"net_edge_after_fees_{key}"] = value
    for key, value in metrics["break_even_fee_per_leg"].items():
        values[f"break_even_fee_per_leg_{key}"] = value
    for item in metrics["fee_sensitivity"]:
        fee_key = str(item["fee_rate"]).replace(".", "_")
        values[f"profitable_at_fee_{fee_key}"] = item["profitable_count"]
    return values


def _write_scalar_csv(path: Path, metrics: Mapping[str, Any], *, atomic: bool) -> None:
    destination = path.with_name(f".{path.name}.tmp") if atomic else path
    scalar_metrics = _scalar_metrics(metrics)
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(scalar_metrics))
        writer.writeheader()
        writer.writerow(scalar_metrics)
    if atomic:
        destination.replace(path)


class ReportGenerator:
    def __init__(self, output_dir: str | Path = "data/reports") -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(
        self,
        records_or_path: Iterable[Any] | str | Path,
        metadata: Mapping[str, Any] | None = None,
        *,
        timestamp: str | None = None,
    ) -> ReportArtifacts:
        if isinstance(records_or_path, (str, Path)):
            records, parse_errors = load_jsonl(records_or_path)
        else:
            records = [_normalise_record(record) for record in records_or_path]
            parse_errors = 0
        metrics = calculate_metrics(records, metadata, parse_errors=parse_errors)
        return self.generate_metrics(metrics, metadata, timestamp=timestamp)

    def generate_metrics(
        self,
        metrics: Mapping[str, Any],
        metadata: Mapping[str, Any] | None = None,
        *,
        timestamp: str | None = None,
    ) -> ReportArtifacts:
        """Write a final report from already calculated live-stream metrics."""

        stamp = timestamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        safe_stamp = _safe_stamp(stamp)
        markdown_path = self.output_dir / f"report_{safe_stamp}.md"
        csv_path = self.output_dir / f"summary_{safe_stamp}.csv"
        top_opportunities_path = self.output_dir / f"top_opportunities_{safe_stamp}.csv"
        _atomic_write_text(markdown_path, render_markdown(metrics, metadata))
        _write_scalar_csv(csv_path, metrics, atomic=True)

        top_fieldnames = (
            "cycle_id",
            "start_size",
            "raw_edge",
            "edge_after_fees",
            "estimated_pnl",
            "limiting_leg",
            "book_staleness_ms",
        )
        with top_opportunities_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=top_fieldnames)
            writer.writeheader()
            writer.writerows(metrics["best_raw_opportunities"])
        return ReportArtifacts(
            markdown_path,
            csv_path,
            metrics,
            str(metrics["conclusion"]),
            top_opportunities_path,
        )

    def generate_checkpoint(
        self,
        records_or_path: Iterable[Any] | str | Path,
        metadata: Mapping[str, Any] | None = None,
        *,
        timestamp: str | None = None,
    ) -> CheckpointArtifacts:
        """Publish an atomic timestamped and latest checkpoint report."""

        if isinstance(records_or_path, (str, Path)):
            records, parse_errors = load_jsonl(records_or_path)
        else:
            records = [_normalise_record(record) for record in records_or_path]
            parse_errors = 0
        metrics = calculate_metrics(records, metadata, parse_errors=parse_errors)
        return self.generate_checkpoint_metrics(
            metrics,
            metadata,
            timestamp=timestamp,
        )

    def generate_checkpoint_metrics(
        self,
        metrics: Mapping[str, Any],
        metadata: Mapping[str, Any] | None = None,
        *,
        timestamp: str | None = None,
    ) -> CheckpointArtifacts:
        """Publish a checkpoint from already calculated live-stream metrics."""

        stamp = _safe_stamp(timestamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
        checkpoint_path = self.output_dir / f"checkpoint_{stamp}.md"
        latest_path = self.output_dir / "latest.md"
        latest_summary_path = self.output_dir / "latest_summary.csv"
        markdown = render_markdown(metrics, metadata)
        _atomic_write_text(checkpoint_path, markdown)
        _atomic_write_text(latest_path, markdown)
        _write_scalar_csv(latest_summary_path, metrics, atomic=True)
        return CheckpointArtifacts(
            checkpoint_path=checkpoint_path,
            latest_path=latest_path,
            latest_summary_path=latest_summary_path,
            metrics=metrics,
        )

    def publish_latest(self, artifacts: ReportArtifacts) -> tuple[Path, Path]:
        """Atomically point the portable latest files at a completed report."""

        latest_path = self.output_dir / "latest.md"
        latest_summary_path = self.output_dir / "latest_summary.csv"
        _atomic_write_text(
            latest_path,
            artifacts.markdown_path.read_text(encoding="utf-8"),
        )
        _atomic_write_text(
            latest_summary_path,
            artifacts.csv_path.read_text(encoding="utf-8"),
        )
        return latest_path, latest_summary_path

    def generate_report(self, *args: Any, **kwargs: Any) -> ReportArtifacts:
        return self.generate(*args, **kwargs)


def generate_report(
    records_or_path: Iterable[Any] | str | Path,
    *,
    output_dir: str | Path = "data/reports",
    metadata: Mapping[str, Any] | None = None,
    timestamp: str | None = None,
) -> ReportArtifacts:
    return ReportGenerator(output_dir).generate(records_or_path, metadata, timestamp=timestamp)


__all__ = [
    "CheckpointArtifacts",
    "ReportArtifacts",
    "ReportGenerator",
    "StreamingReportAccumulator",
    "StreamingReportSnapshot",
    "calculate_metrics",
    "generate_report",
    "load_jsonl",
    "render_markdown",
]
