"""Command-line entry point for the research-only live observer."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from tri_arb.account_fees import (
    DEFAULT_NORMALIZED_FEE_PATH,
    AccountFeeError,
    FeeSchedule,
    load_fee_schedule,
)
from tri_arb.binance_public import BinancePublicClient
from tri_arb.book_manager import BookManager
from tri_arb.config import AppConfig, ConfigError, config_to_dict, load_config
from tri_arb.discovery import (
    DiscoveryResult,
    discover_market,
    discover_normalized_market,
)
from tri_arb.mexc_book_manager import MexcBookManager
from tri_arb.mexc_public import MexcPublicClient
from tri_arb.recorder import JSONLRecorder
from tri_arb.report import (
    ReportArtifacts,
    ReportGenerator,
    StreamingReportAccumulator,
    load_jsonl,
)
from tri_arb.scanner import OpportunityScanner, ScanStats
from tri_arb.utils import iso_utc, jsonable

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunOutcome:
    artifacts: ReportArtifacts
    discovery: DiscoveryResult | None
    scanner_stats: ScanStats
    run_id: str
    error: str | None = None


def _csv_values(value: str) -> tuple[str, ...]:
    values = tuple(item.strip() for item in value.split(",") if item.strip())
    if not values:
        raise argparse.ArgumentTypeError("provide at least one comma-separated value")
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tri_arb.main",
        description=(
            "Observe public Spot books and paper-simulate triangular arbitrage. "
            "This program cannot place orders."
        ),
    )
    parser.add_argument(
        "--exchange",
        choices=("binance", "mexc"),
        help="public market-data exchange (default: binance)",
    )
    parser.add_argument("--config", type=Path, help="YAML configuration file")
    parser.add_argument("--duration-minutes", type=float, help="live observation duration")
    parser.add_argument(
        "--checkpoint-every-minutes",
        type=float,
        help="publish latest/checkpoint reports at this interval; 0 disables checkpoints",
    )
    parser.add_argument("--max-symbols", type=int, help="maximum required depth streams")
    parser.add_argument("--max-cycles", type=int, help="maximum monitored cycle directions")
    parser.add_argument("--root-asset", help="cycle start/end asset, normally USDT")
    fee_source = parser.add_mutually_exclusive_group()
    fee_source.add_argument("--fee-rate", help="manual taker-fee override per leg")
    fee_source.add_argument(
        "--use-account-fees",
        action="store_true",
        help=("use read-only MEXC account fees from configs/generated/mexc_account_fee.yaml"),
    )
    parser.add_argument(
        "--start-sizes",
        type=_csv_values,
        help="comma-separated root-asset sizes, e.g. 10,25,50,100",
    )
    parser.add_argument(
        "--latency-buckets",
        type=_csv_values,
        help="comma-separated latency rechecks in milliseconds",
    )
    parser.add_argument("--output-dir", type=Path, help="artifact directory")
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        help="console and file log level",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--discover-only",
        action="store_true",
        help="rank symbols/cycles and exit without opening depth streams",
    )
    mode.add_argument(
        "--report-only",
        type=Path,
        metavar="JSONL_OR_DIR",
        help="generate a report from existing records without network access",
    )
    return parser


def _config_overrides(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "exchange": args.exchange,
        "duration_minutes": args.duration_minutes,
        "checkpoint_every_minutes": args.checkpoint_every_minutes,
        "max_symbols": args.max_symbols,
        "max_cycles": args.max_cycles,
        "root_asset": args.root_asset,
        "fee_rate": args.fee_rate,
        "start_sizes": args.start_sizes,
        "latency_buckets_ms": args.latency_buckets,
        "output_dir": args.output_dir,
        "log_level": args.log_level,
    }


def _configure_logging(config: AppConfig) -> Path:
    logs_dir = config.output.output_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = logs_dir / f"observer_{stamp}.log"
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        path,
        maxBytes=min(config.output.max_jsonl_bytes, 10 * 1024 * 1024),
        backupCount=config.output.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logging.basicConfig(
        level=getattr(logging, config.output.log_level),
        handlers=(stream_handler, file_handler),
        force=True,
    )
    return path


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
            continue
        except (NotImplementedError, RuntimeError):
            pass

        # Windows' ProactorEventLoop does not implement add_signal_handler.
        # Installing a regular handler prevents asyncio.run() from cancelling
        # the observer before it can close the recorder and generate a report.
        def request_stop(_signum: int, _frame: Any, *, event: asyncio.Event = stop_event) -> None:
            loop.call_soon_threadsafe(event.set)

        with suppress(OSError, RuntimeError, ValueError):
            signal.signal(signum, request_stop)


def _config_fee_schedule(config: AppConfig) -> FeeSchedule:
    return FeeSchedule(
        source="config_fee",
        fallback_taker_fee=config.simulation.fee_rate,
        symbol_taker_fees={},
    )


def _resolve_fee_schedule(args: argparse.Namespace, config: AppConfig) -> FeeSchedule:
    """Resolve the simulation-only fee source without reading credentials."""

    if args.use_account_fees:
        if config.exchange != "mexc":
            raise AccountFeeError("--use-account-fees is supported only with --exchange mexc")
        try:
            loaded = load_fee_schedule(DEFAULT_NORMALIZED_FEE_PATH)
        except AccountFeeError as exc:
            detail = str(exc)
            instruction = "Run python -m tri_arb.tools.check_mexc_fees first."
            raise AccountFeeError(
                detail if instruction in detail else f"{detail}. {instruction}"
            ) from exc
        return FeeSchedule(
            source="mexc_account_tradeFee_read_only",
            fallback_taker_fee=loaded.fallback_taker_fee,
            symbol_taker_fees=loaded.symbol_taker_fees,
            generated_at=loaded.generated_at,
            symbol_maker_fees=loaded.symbol_maker_fees,
        )
    if args.fee_rate is not None:
        return FeeSchedule(
            source="fixed_cli_fee",
            fallback_taker_fee=config.simulation.fee_rate,
            symbol_taker_fees={},
        )
    return _config_fee_schedule(config)


def _assumptions(
    config: AppConfig,
    fee_schedule: FeeSchedule,
    monitored_symbols: Iterable[str] | None = None,
) -> dict[str, Any]:
    exchange_name = "MEXC Spot" if config.exchange == "mexc" else "Binance Spot"
    reported_symbol_fees = dict(fee_schedule.symbol_taker_fees)
    if monitored_symbols is not None:
        selected = {
            str(symbol).strip().upper() for symbol in monitored_symbols if str(symbol).strip()
        }
        reported_symbol_fees = {
            symbol: fee for symbol, fee in reported_symbol_fees.items() if symbol in selected
        }
    observed_fees = tuple(fee_schedule.symbol_taker_fees.values())
    return {
        "exchange": exchange_name,
        "market_data_only": True,
        "research_only": True,
        "live_trading_available": False,
        "root_asset": config.discovery.root_asset,
        "fee_source": fee_schedule.source,
        "fee_rate_per_taker_leg": str(fee_schedule.fallback_taker_fee),
        "fee_fallback_is_maximum_observed": (
            fee_schedule.source == "mexc_account_tradeFee_read_only"
        ),
        "account_fee_generated_at": fee_schedule.generated_at,
        "account_fee_symbol_count": len(fee_schedule.symbol_taker_fees),
        "reported_symbol_fee_count": len(reported_symbol_fees),
        "account_fee_minimum_taker_fee": (str(min(observed_fees)) if observed_fees else None),
        "account_fee_maximum_taker_fee": (str(max(observed_fees)) if observed_fees else None),
        "symbol_taker_fees": {
            symbol: str(value) for symbol, value in sorted(reported_symbol_fees.items())
        },
        "fee_sensitivity_rates": [str(value) for value in config.simulation.fee_sensitivity_rates],
        "fee_assumptions_are_simulated": True,
        "mexc_fee_verification_required": config.exchange == "mexc",
        "start_sizes": [str(value) for value in config.simulation.start_sizes],
        "latency_buckets_ms": list(config.simulation.latency_buckets_ms),
        "scan_interval_ms": config.simulation.scan_interval_ms,
        "signal_cooldown_ms": config.simulation.signal_cooldown_ms,
        "stored_depth_levels": config.order_book.depth_levels,
        "snapshot_limit": config.order_book.snapshot_limit,
        "depth_stream_interval_ms": config.order_book.stream_interval_ms,
        "displayed_quantity_haircut": str(config.simulation.quantity_haircut),
        "extra_slippage_bps": str(config.simulation.extra_slippage_bps),
        "book_stale_after_ms": config.order_book.stale_after_ms,
        "min_quote_volume_usdt": str(config.discovery.min_quote_volume_usdt),
        "max_spread_bps": (
            str(config.discovery.max_spread_bps)
            if config.discovery.max_spread_bps is not None
            else None
        ),
        "min_top_of_book_notional": str(config.discovery.min_top_of_book_notional),
        "exclude_assets": list(config.discovery.exclude_assets),
        "exclude_symbol_patterns": list(config.discovery.exclude_symbol_patterns),
        "rest_base_url": config.network.rest_base_url,
        "websocket_base_url": config.network.websocket_base_url,
    }


def _report_metadata(
    config: AppConfig,
    fee_schedule: FeeSchedule,
    *,
    started_at: str,
    ended_at: str,
    duration_seconds: float,
    discovery: DiscoveryResult | None,
    run_id: str,
    scanner_stats: ScanStats,
    manager_metrics: Mapping[str, Any],
    startup_book_health: Mapping[str, Any],
    error: str | None,
) -> dict[str, Any]:
    return {
        "exchange": "MEXC Spot" if config.exchange == "mexc" else "Binance Spot",
        "run_id": run_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": duration_seconds,
        "monitored_symbols": len(discovery.symbols) if discovery else 0,
        "monitored_cycles": len(discovery.cycles) if discovery else 0,
        "monitored_cycle_ids": [cycle.cycle_id for cycle in discovery.cycles] if discovery else [],
        "eligible_symbols": discovery.eligible_symbol_count if discovery else 0,
        "candidate_cycles": discovery.candidate_cycle_count if discovery else 0,
        "latency_buckets_ms": list(config.simulation.latency_buckets_ms),
        "minimum_decision_sample": config.decision.minimum_sample_size,
        "decision": config_to_dict(config)["decision"],
        "fee_source": fee_schedule.source,
        "assumptions": _assumptions(
            config,
            fee_schedule,
            discovery.symbol_names if discovery else None,
        ),
        "scanner_stats": scanner_stats.to_dict(),
        "order_book_metrics": jsonable(manager_metrics),
        "startup_book_health": jsonable(startup_book_health),
        "error": error,
    }


def _load_artifacts(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Read current-run JSONL bases plus any rotated siblings."""

    records: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for base in paths:
        for candidate in _jsonl_candidates(base):
            if candidate in seen or not candidate.is_file():
                continue
            seen.add(candidate)
            records.extend(_load_slim_report_jsonl(candidate))
    return records


_SIGNAL_REPORT_FIELDS = frozenset(
    {
        "_category",
        "_recorded_at",
        "_run_id",
        "record_type",
        "signal_id",
        "opportunity_id",
        "cycle_id",
        "route",
        "start_asset",
        "start_size",
        "start_amount",
        "raw_return",
        "gross_return",
        "return_after_fees",
        "after_fee_return",
        "return_after_depth",
        "net_return",
        "pessimistic_return",
        "profitable_pessimistic",
        "estimated_pnl",
        "pnl",
        "limiting_leg",
        "fully_executable",
        "fully_executable_at_displayed_depth",
        "filter_rejected",
        "book_staleness_ms",
        "average_book_staleness_ms",
        "max_book_staleness_ms",
        "timestamp_local",
        "timestamp_exchange",
        "timestamp_exchange_ms",
        "evaluated_at",
    }
)

_LATENCY_REPORT_FIELDS = frozenset(
    {
        "_category",
        "_recorded_at",
        "_run_id",
        "record_type",
        "signal_id",
        "opportunity_id",
        "cycle_id",
        "start_size",
        "start_amount",
        "detected_at",
        "initial_edge",
        "initial_pnl",
        "initial_profitable",
        "ghost_arbitrage",
        "ghost_arbitrage_flag",
        "ghost",
        "lifetime_ms",
        "signal_lifetime_ms",
        "book_staleness_ms",
        "average_book_staleness_ms",
        "max_book_staleness_ms",
    }
)

_LATENCY_CHECK_REPORT_FIELDS = frozenset(
    {
        "delay_ms",
        "checked_at",
        "elapsed_ms",
        "edge",
        "net_return",
        "pnl",
        "profitable",
        "executable",
    }
)


def _slim_report_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Drop execution-leg payloads that no report calculation reads."""

    category = str(record.get("_category", record.get("category", ""))).lower()
    if category in {"signal", "opportunity"}:
        return {key: value for key, value in record.items() if key in _SIGNAL_REPORT_FIELDS}
    if category == "latency":
        dynamic_prefixes = (
            "return_after_",
            "profit_after_",
            "profitable_after_",
            "elapsed_after_",
            "lateness_after_",
        )
        slim = {
            key: value
            for key, value in record.items()
            if key in _LATENCY_REPORT_FIELDS or key.startswith(dynamic_prefixes)
        }
        checks = record.get("checks")
        if isinstance(checks, Sequence) and not isinstance(checks, (str, bytes)):
            slim["checks"] = [
                {key: value for key, value in check.items() if key in _LATENCY_CHECK_REPORT_FIELDS}
                for check in checks
                if isinstance(check, Mapping)
            ]
        return slim
    return dict(record)


def _normalise_report_envelope(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {"value": payload}
    if not isinstance(payload.get("data"), Mapping):
        return dict(payload)
    record = dict(payload["data"])
    record.setdefault("_category", payload.get("category"))
    record.setdefault("_recorded_at", payload.get("recorded_at"))
    record.setdefault("_run_id", payload.get("run_id"))
    return record


def _load_slim_report_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse recorder signal/latency lines without materializing nested leg payloads."""

    family = _jsonl_family(path)
    base_name = family[0].name if family is not None else ""
    is_signal_file = path.parent.name == "signals" and base_name.startswith("signals_")
    is_latency_file = path.parent.name == "signals" and base_name.startswith("latency_rechecks_")
    if not is_signal_file and not is_latency_file:
        loaded, _ = load_jsonl(path)
        return [_slim_report_record(record) for record in loaded]

    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                if is_signal_file:
                    marker_at = stripped.find(',"raw_simulation":')
                    payload = (
                        json.loads(f"{stripped[:marker_at]}}}}}")
                        if marker_at >= 0
                        else json.loads(stripped)
                    )
                else:
                    initial_result_at = stripped.find(',"initial_result":')
                    flat_tail_at = stripped.find(',"disappeared":')
                    if initial_result_at >= 0 and flat_tail_at >= 0:
                        payload = json.loads(f"{stripped[:initial_result_at]}}}}}")
                        tail = json.loads("{" + stripped[flat_tail_at + 1 : -1])
                        payload["data"].update(tail)
                    else:
                        payload = json.loads(stripped)
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
            records.append(_slim_report_record(_normalise_report_envelope(payload)))
    return records


def _records_for_report(recorder: JSONLRecorder) -> list[dict[str, Any]]:
    # These contain one complete signal per cooldown window, its latency outcome,
    # and cumulative book health. Stage-specific files remain available for raw
    # analysis but are intentionally excluded here to avoid double counting.
    paths = recorder.paths
    selected = [paths[key] for key in ("signal", "latency", "health") if key in paths]
    return _load_artifacts(selected)


def _jsonl_family(path: Path) -> tuple[Path, int | None] | None:
    """Return a recorder base path and optional numeric rotation number."""

    marker = ".jsonl"
    marker_at = path.name.rfind(marker)
    if marker_at < 0:
        return None
    tail = path.name[marker_at + len(marker) :]
    if tail and (not tail.startswith(".") or not tail[1:].isdigit()):
        return None
    base = path.with_name(path.name[: marker_at + len(marker)])
    return base, int(tail[1:]) if tail else None


def _jsonl_candidates(source: Path) -> list[Path]:
    """Resolve strict JSONL families in oldest-to-newest rotation order.

    Selecting either ``records.jsonl`` or ``records.jsonl.N`` loads every
    numeric rotation plus the active base. Unrelated prefix matches and stale
    temporary files are intentionally ignored.
    """

    if source.is_dir():
        expected_base: Path | None = None
        possible = (path for path in source.rglob("*") if path.is_file())
    else:
        family = _jsonl_family(source)
        if family is None:
            raise ValueError("report source must be a .jsonl file or numeric rotation")
        expected_base, _ = family
        possible = (
            path for path in expected_base.parent.glob(f"{expected_base.name}*") if path.is_file()
        )

    candidates: list[tuple[Path, int | None, Path]] = []
    for path in possible:
        family = _jsonl_family(path)
        if family is None:
            continue
        base, rotation = family
        if expected_base is not None and base != expected_base:
            continue
        candidates.append((base, rotation, path))
    candidates.sort(
        key=lambda item: (
            str(item[0]),
            item[1] is None,
            item[1] if item[1] is not None else 0,
        )
    )
    return [path for _, _, path in candidates]


def _is_canonical_recorder_report_path(path: Path) -> bool:
    """Exclude duplicated stage/raw files before parsing a recorder run."""

    family = _jsonl_family(path)
    if family is None:
        return False
    base_name = family[0].name
    return (path.parent.name, base_name.split("_", 1)[0]) in {
        ("signals", "signals"),
        ("signals", "latency"),
        ("raw", "order"),
        ("reports", "summary"),
    }


def _canonical_report_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Use the same non-duplicating record stages for live and offline reports."""

    canonical: list[dict[str, Any]] = []
    for value in records:
        record = dict(value)
        category = str(record.get("_category", record.get("category", ""))).lower()
        record_type = str(record.get("record_type", "")).lower()
        if category:
            # Recorder envelopes identify their persistence stage explicitly.
            # Never let a nested record_type make after-fee/depth/pessimistic
            # copies look like additional unique signals.
            if category in {
                "signal",
                "opportunity",
                "latency",
                "health",
                "book_health",
                "order_book_health",
            }:
                canonical.append(record)
            continue
        if record_type in {"signal", "latency", "order_book_health"} or any(
            key in record for key in ("raw_return", "return_after_depth", "net_return", "checks")
        ):
            canonical.append(record)
    return canonical


def _load_report_bundle(source: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Resolve one run's canonical records and persisted summary metadata."""

    if not source.exists():
        raise FileNotFoundError(source)
    initial: list[dict[str, Any]] = []
    for path in _jsonl_candidates(source):
        initial.extend(_load_slim_report_jsonl(path))
    run_ids = {str(record["_run_id"]) for record in initial if record.get("_run_id")}
    selected_run_id: str | None = None
    artifact_root: Path | None = None

    if source.is_dir():
        category_names = {"raw", "signals", "reports", "snapshots", "logs"}
        artifact_root = source.parent if source.name in category_names else source
        if len(run_ids) > 1:
            raise ValueError(
                "report directory contains multiple run IDs; pass a JSONL file from one run"
            )
        if run_ids:
            selected_run_id = next(iter(run_ids))
            anonymous = [record for record in initial if not record.get("_run_id")]
            if anonymous:
                raise ValueError(
                    "report directory mixes run-scoped and unscoped JSONL records; "
                    "pass a JSONL file from the intended run"
                )
            records = [
                record for record in initial if str(record.get("_run_id", "")) == selected_run_id
            ]
        else:
            # A directory of plain user-authored fixtures remains supported.
            records = initial
    elif len(run_ids) == 1:
        selected_run_id = next(iter(run_ids))
        category_names = {"raw", "signals", "reports", "snapshots", "logs"}
        recorder_layout = source.parent.name in category_names
        artifact_root = (
            source.parent.parent if source.parent.name in category_names else source.parent
        )
        records = []
        for path in _jsonl_candidates(artifact_root):
            if recorder_layout and not _is_canonical_recorder_report_path(path):
                continue
            loaded = _load_slim_report_jsonl(path)
            records.extend(
                record for record in loaded if str(record.get("_run_id", "")) == selected_run_id
            )
    elif len(run_ids) > 1:
        raise ValueError("report source rotations contain multiple run IDs")
    else:
        # Plain user-authored JSONL has no recorder envelope or sibling run ID.
        records = initial

    summaries = [
        record
        for record in records
        if str(record.get("_category", record.get("category", ""))).lower() == "summary"
    ]
    summaries.sort(
        key=lambda record: (
            str(record.get("ended_at", "")),
            str(record.get("_recorded_at", record.get("recorded_at", ""))),
        )
    )
    metadata = dict(summaries[-1]) if summaries else {}
    for private_key in tuple(key for key in metadata if key.startswith("_")):
        metadata.pop(private_key, None)
    if str(metadata.get("category", "")).lower() == "summary":
        metadata.pop("category", None)
    if selected_run_id is not None:
        metadata.setdefault("run_id", selected_run_id)
    if selected_run_id is not None and artifact_root is not None:
        selection_path = artifact_root / "raw" / f"selected_cycles_{selected_run_id}.json"
        if selection_path.is_file() and "monitored_cycle_ids" not in metadata:
            try:
                selection = json.loads(selection_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                selection = None
            if (
                isinstance(selection, Mapping)
                and str(selection.get("run_id", "")) == selected_run_id
                and isinstance(selection.get("records"), list)
            ):
                metadata["monitored_cycle_ids"] = [
                    str(record["cycle_id"])
                    for record in selection["records"]
                    if isinstance(record, Mapping) and record.get("cycle_id")
                ]
    return _canonical_report_records(records), metadata


def _build_public_adapter(config: AppConfig) -> Any:
    """Construct the selected public-only adapter; there is no trading client."""

    common = {
        "request_timeout_s": config.network.request_timeout_seconds,
        "max_retries": config.network.max_retries,
        "backoff_base_s": config.network.retry_base_delay_seconds,
    }
    if config.exchange == "mexc":
        return MexcPublicClient(
            config.network.rest_base_url,
            websocket_base_url=config.network.websocket_base_url,
            stream_interval_ms=config.order_book.stream_interval_ms,
            max_streams_per_connection=config.order_book.max_streams_per_connection,
            **common,
        )
    return BinancePublicClient(config.network.rest_base_url, **common)


async def _fetch_public_discovery_payloads(adapter: Any, exchange: str) -> tuple[Any, Any, Any]:
    info_getter = getattr(adapter, "get_exchange_info", None)
    if not callable(info_getter):
        info_getter = adapter.exchange_info
    ticker_getter = getattr(adapter, "get_24h_tickers", None)
    if not callable(ticker_getter):
        ticker_getter = adapter.ticker_24hr

    if exchange == "mexc" and callable(getattr(adapter, "get_book_tickers", None)):
        exchange_info, tickers, book_tickers = await asyncio.gather(
            info_getter(),
            ticker_getter(),
            adapter.get_book_tickers(),
        )
        return exchange_info, tickers, book_tickers
    exchange_info, tickers = await asyncio.gather(info_getter(), ticker_getter())
    return exchange_info, tickers, None


def _discover_with_adapter(
    adapter: Any,
    exchange_info: Any,
    tickers: Any,
    book_tickers: Any,
    config: AppConfig,
) -> DiscoveryResult:
    symbol_normalizer = getattr(adapter, "normalize_symbol_metadata", None)
    ticker_normalizer = getattr(adapter, "normalize_ticker", None)
    if not callable(symbol_normalizer) or not callable(ticker_normalizer):
        # Compatibility path for existing injected Binance test clients.
        return discover_market(exchange_info, tickers, config)
    symbols = symbol_normalizer(exchange_info)
    if config.exchange == "mexc":
        stats = ticker_normalizer(tickers, book_tickers=book_tickers)
    else:
        stats = ticker_normalizer(tickers)
    return discover_normalized_market(symbols, stats, config)


def _build_book_manager(config: AppConfig, discovery: DiscoveryResult, adapter: Any) -> Any:
    common = {
        "max_depth": config.order_book.depth_levels,
        "snapshot_limit": config.order_book.snapshot_limit,
        "max_streams_per_connection": config.order_book.max_streams_per_connection,
        "stream_interval_ms": config.order_book.stream_interval_ms,
        "ws_base_url": config.network.websocket_base_url,
        "stale_after_ms": config.order_book.stale_after_ms,
    }
    if config.exchange == "mexc":
        return MexcBookManager(discovery.symbol_names, adapter, **common)
    return BookManager(discovery.symbol_names, adapter, **common)


async def _wait_for_books(
    manager: BookManager,
    timeout_seconds: float,
    stop_event: asyncio.Event,
) -> bool:
    ready_task = asyncio.create_task(manager.wait_until_ready(timeout_seconds))
    stop_task = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait(
        (ready_task, stop_task),
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return bool(ready_task in done and ready_task.result())


async def _publish_periodic_checkpoints(
    config: AppConfig,
    fee_schedule: FeeSchedule,
    recorder: JSONLRecorder,
    discovery: DiscoveryResult,
    scanner: OpportunityScanner,
    manager: Any,
    startup_book_health: Mapping[str, Any],
    overall_started_at: str,
    simulation_started: float,
    stop_event: asyncio.Event,
    report_accumulator: StreamingReportAccumulator,
) -> None:
    """Publish bounded-memory cumulative checkpoints without rereading JSONL."""

    interval_seconds = config.run.checkpoint_every_minutes * 60
    if interval_seconds <= 0:
        return
    generator = ReportGenerator(config.output.output_dir / "reports")
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            return
        except TimeoutError:
            pass

        try:
            elapsed = time.monotonic() - simulation_started
            metadata = _report_metadata(
                config,
                fee_schedule,
                started_at=overall_started_at,
                ended_at=iso_utc(),
                duration_seconds=elapsed,
                discovery=discovery,
                run_id=recorder.run_id,
                scanner_stats=scanner.stats,
                manager_metrics=manager.metrics_snapshot(),
                startup_book_health=startup_book_health,
                error=None,
            )
            metadata.update(report_accumulator.aggregation_metadata())
            snapshot = report_accumulator.snapshot()
            metrics = await asyncio.to_thread(snapshot.calculate_metrics, metadata)
            checkpoint_history = report_accumulator.prepare_checkpoint(metrics, metadata)
            metadata.update(checkpoint_history)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            publish_task = asyncio.create_task(
                asyncio.to_thread(
                    generator.generate_checkpoint_metrics,
                    metrics,
                    metadata,
                    timestamp=stamp,
                )
            )
            try:
                checkpoint = await asyncio.shield(publish_task)
            except asyncio.CancelledError:
                # asyncio cancellation cannot stop an active worker thread.
                # Wait for its atomic file writes before final-report shutdown,
                # then commit exactly the history that reached disk.
                try:
                    await publish_task
                except Exception:
                    LOGGER.exception("checkpoint publication failed during shutdown")
                else:
                    report_accumulator.commit_checkpoint(checkpoint_history)
                raise
            report_accumulator.commit_checkpoint(checkpoint_history)
            LOGGER.info(
                "checkpoint report written after %.2f minutes: %s",
                elapsed / 60,
                checkpoint.checkpoint_path,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A checkpoint failure must never terminate the observer; the final
            # report path remains available during graceful shutdown.
            LOGGER.exception("could not publish periodic checkpoint report")


async def run_observer(
    config: AppConfig,
    *,
    discover_only: bool = False,
    stop_event: asyncio.Event | None = None,
    fee_schedule: FeeSchedule | None = None,
) -> RunOutcome:
    """Run discovery and, unless requested otherwise, the live paper observer."""

    effective_fees = fee_schedule or _config_fee_schedule(config)
    stop = stop_event or asyncio.Event()
    _install_signal_handlers(stop)
    report_accumulator = StreamingReportAccumulator(
        fee_sensitivity_rates=config.simulation.fee_sensitivity_rates,
        latency_buckets_ms=config.simulation.latency_buckets_ms,
    )
    recorder = JSONLRecorder(
        config.output.output_dir,
        max_bytes=config.output.max_jsonl_bytes,
        record_observer=report_accumulator.observe,
    )
    await recorder.start()
    overall_started_at = iso_utc()
    discovery: DiscoveryResult | None = None
    scanner_stats = ScanStats()
    manager: Any | None = None
    manager_metrics: Mapping[str, Any] = {}
    startup_book_health: Mapping[str, Any] = {}
    simulation_duration_seconds = 0.0
    simulation_started: float | None = None
    checkpoint_task: asyncio.Task[None] | None = None
    error: str | None = None

    try:
        client = _build_public_adapter(config)
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(client)
            LOGGER.info(
                "fetching %s public exchange metadata and 24-hour statistics",
                config.exchange,
            )
            exchange_info, tickers, book_tickers = await _fetch_public_discovery_payloads(
                client,
                config.exchange,
            )
            discovery = _discover_with_adapter(
                client,
                exchange_info,
                tickers,
                book_tickers,
                config,
            )
            recorder.write_selection(
                "symbols",
                discovery.symbols,
                eligible_symbol_count=discovery.eligible_symbol_count,
            )
            recorder.write_selection(
                "cycles",
                (cycle.to_record() for cycle in discovery.cycles),
                candidate_cycle_count=discovery.candidate_cycle_count,
            )
            LOGGER.info(
                "selected %d symbols and %d cycle directions",
                len(discovery.symbols),
                len(discovery.cycles),
            )

            if not discover_only and discovery.cycles and not stop.is_set():
                manager = _build_book_manager(config, discovery, client)
                await manager.start()
                # Registered after the REST client, so LIFO cleanup stops all
                # snapshot tasks before closing the client they depend on.
                stack.push_async_callback(manager.stop)
                ready = await _wait_for_books(
                    manager,
                    config.order_book.startup_timeout_seconds,
                    stop,
                )
                if not ready and not stop.is_set():
                    LOGGER.warning(
                        "not every selected book became healthy before the startup timeout; "
                        "unhealthy cycles will be skipped"
                    )
                startup_health_snapshot = manager.health_snapshot()
                startup_unhealthy_symbols = sorted(
                    symbol
                    for symbol, health in startup_health_snapshot.items()
                    if not bool(health.get("healthy"))
                )
                startup_book_health = {
                    "captured_at": iso_utc(),
                    "managed_books": len(startup_health_snapshot),
                    "healthy_books": len(startup_health_snapshot) - len(startup_unhealthy_symbols),
                    "unhealthy_books": len(startup_unhealthy_symbols),
                    "unhealthy_symbols": startup_unhealthy_symbols,
                }

                symbol_filters = {symbol.symbol: symbol for symbol in discovery.symbols}
                scanner = OpportunityScanner(
                    discovery.cycles,
                    manager,
                    recorder,
                    fee_rate=effective_fees.fallback_taker_fee,
                    start_sizes=config.simulation.start_sizes,
                    latency_buckets_ms=config.simulation.latency_buckets_ms,
                    quantity_haircut=config.simulation.quantity_haircut,
                    extra_slippage_bps=config.simulation.extra_slippage_bps,
                    scan_interval_ms=config.simulation.scan_interval_ms,
                    signal_cooldown_ms=config.simulation.signal_cooldown_ms,
                    profit_threshold_bps=config.simulation.profit_threshold_bps,
                    symbol_filters=symbol_filters,
                    symbol_fee_rates=effective_fees.symbol_taker_fees,
                )
                simulation_started = time.monotonic()
                checkpoint_task = asyncio.create_task(
                    _publish_periodic_checkpoints(
                        config,
                        effective_fees,
                        recorder,
                        discovery,
                        scanner,
                        manager,
                        startup_book_health,
                        overall_started_at,
                        simulation_started,
                        stop,
                        report_accumulator,
                    ),
                    name="periodic-report-checkpoints",
                )
                try:
                    scanner_stats = await scanner.run(config.duration_minutes * 60, stop)
                finally:
                    checkpoint_task.cancel()
                    await asyncio.gather(checkpoint_task, return_exceptions=True)
                    checkpoint_task = None
                    simulation_duration_seconds = time.monotonic() - simulation_started
                manager_metrics = manager.metrics_snapshot()
            elif not discovery.cycles:
                LOGGER.warning("discovery found no monitorable triangular cycles")
    except asyncio.CancelledError:
        error = "observer task was cancelled"
        LOGGER.warning(error)
        current_task = asyncio.current_task()
        if current_task is not None:
            while current_task.cancelling():
                current_task.uncancel()
    except Exception as exc:  # A diagnostic report is still required on network/runtime failure.
        error = f"{type(exc).__name__}: {exc}"
        LOGGER.exception("observer run failed")
        try:
            await recorder.record(
                "error",
                {
                    "timestamp_local": iso_utc(),
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
        except Exception:
            LOGGER.exception("could not persist the observer error record")
    finally:
        if checkpoint_task is not None:
            checkpoint_task.cancel()
            await asyncio.gather(checkpoint_task, return_exceptions=True)
        if manager is not None:
            try:
                if not manager_metrics:
                    manager_metrics = manager.metrics_snapshot()
                await manager.stop()
            except Exception as exc:
                cleanup_error = f"{type(exc).__name__}: {exc}"
                error = f"{error}; cleanup failed: {cleanup_error}" if error else cleanup_error
                LOGGER.exception("order-book manager cleanup failed")

    ended_at = iso_utc()
    metadata = _report_metadata(
        config,
        effective_fees,
        started_at=overall_started_at,
        ended_at=ended_at,
        duration_seconds=simulation_duration_seconds,
        discovery=discovery,
        run_id=recorder.run_id,
        scanner_stats=scanner_stats,
        manager_metrics=manager_metrics,
        startup_book_health=startup_book_health,
        error=error,
    )
    try:
        metadata.update(report_accumulator.aggregation_metadata())
        metadata.update(report_accumulator.checkpoint_metadata())
        await recorder.record_summary(metadata)
        snapshot = report_accumulator.snapshot()
        metrics = await asyncio.to_thread(snapshot.calculate_metrics, metadata)
        generator = ReportGenerator(config.output.output_dir / "reports")
        artifacts = await asyncio.to_thread(
            generator.generate_metrics,
            metrics,
            metadata,
            timestamp=recorder.run_id,
        )
        generator.publish_latest(artifacts)
    finally:
        recorder.close()
    return RunOutcome(artifacts, discovery, scanner_stats, recorder.run_id, error)


def _print_report(artifacts: ReportArtifacts) -> None:
    print(f"Report: {artifacts.markdown_path}")
    print(f"CSV summary: {artifacts.csv_path}")
    print(f"Top opportunities: {artifacts.top_opportunities_path}")
    print(f"Conclusion: {artifacts.conclusion}")


async def _async_main(
    args: argparse.Namespace,
    config: AppConfig,
    fee_schedule: FeeSchedule,
) -> int:
    if args.report_only is not None:
        records, metadata = _load_report_bundle(args.report_only)
        metadata.setdefault("latency_buckets_ms", list(config.simulation.latency_buckets_ms))
        metadata.setdefault("fee_source", fee_schedule.source)
        metadata.setdefault("assumptions", _assumptions(config, fee_schedule))
        artifacts = ReportGenerator(config.output.output_dir / "reports").generate(
            records,
            metadata,
        )
        _print_report(artifacts)
        return 0

    outcome = await run_observer(
        config,
        discover_only=args.discover_only,
        fee_schedule=fee_schedule,
    )
    if outcome.discovery is not None:
        print(
            f"Selected {len(outcome.discovery.symbols)} symbols and "
            f"{len(outcome.discovery.cycles)} cycle directions."
        )
    _print_report(outcome.artifacts)
    if outcome.error:
        print(f"Run error: {outcome.error}", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, _config_overrides(args))
    except ConfigError as exc:
        parser.error(str(exc))
    try:
        fee_schedule = _resolve_fee_schedule(args, config)
    except AccountFeeError as exc:
        parser.error(str(exc))
    _configure_logging(config)
    LOGGER.warning(
        "simulation-only mode: market observation uses public data and no order endpoints exist"
    )
    try:
        return asyncio.run(_async_main(args, config, fee_schedule))
    except KeyboardInterrupt:  # Fallback for platforms without loop signal handlers.
        LOGGER.warning("interrupted before graceful shutdown completed")
        return 130
    except (FileNotFoundError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
