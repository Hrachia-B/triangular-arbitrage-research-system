import asyncio
import json
from decimal import Decimal

import pytest

import tri_arb.main as main_module
from tri_arb.config import config_from_mapping, load_config


def write_jsonl(path, *records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def envelope(run_id, category, data, *, recorded_at="2026-01-01T00:00:00+00:00"):
    return {
        "recorded_at": recorded_at,
        "run_id": run_id,
        "category": category,
        "data": data,
    }


def exchange_payload():
    symbols = []
    for symbol, base, quote in (
        ("AAAUSDT", "AAA", "USDT"),
        ("AAABBB", "AAA", "BBB"),
        ("BBBUSDT", "BBB", "USDT"),
    ):
        symbols.append(
            {
                "symbol": symbol,
                "baseAsset": base,
                "quoteAsset": quote,
                "status": "TRADING",
                "isSpotTradingAllowed": True,
                "permissions": ["SPOT"],
                "orderTypes": ["MARKET"],
                "filters": [],
            }
        )
    return {"symbols": symbols}


def ticker_payload():
    return [
        {
            "symbol": symbol,
            "quoteVolume": "1000000",
            "volume": "100000",
            "bidPrice": bid,
            "bidQty": "1000",
            "askPrice": ask,
            "askQty": "1000",
            "lastPrice": bid,
            "count": 1000,
        }
        for symbol, bid, ask in (
            ("AAAUSDT", "0.99", "1"),
            ("AAABBB", "2", "2.01"),
            ("BBBUSDT", "0.6", "0.61"),
        )
    ]


class FakePublicClient:
    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def exchange_info(self):
        return exchange_payload()

    async def ticker_24hr(self):
        return ticker_payload()


def test_discover_only_runtime_writes_selection_and_report(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "BinancePublicClient", FakePublicClient)
    config = config_from_mapping(
        {
            "max_symbols": 3,
            "max_cycles": 2,
            "output_dir": tmp_path / "data",
        }
    )

    outcome = asyncio.run(main_module.run_observer(config, discover_only=True))

    assert outcome.error is None
    assert outcome.discovery is not None
    assert len(outcome.discovery.symbols) == 3
    assert len(outcome.discovery.cycles) == 2
    assert outcome.artifacts.markdown_path.exists()
    assert outcome.artifacts.csv_path.exists()
    assert len(list((tmp_path / "data" / "raw").glob("selected_symbols_*.json"))) == 1
    assert len(list((tmp_path / "data" / "raw").glob("selected_cycles_*.json"))) == 1


def test_mexc_discover_only_selects_public_adapter_and_dynamic_report(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "MexcPublicClient", FakePublicClient)
    config = config_from_mapping(
        {
            "exchange": "mexc",
            "max_symbols": 3,
            "max_cycles": 2,
            "min_quote_volume_usdt": 0,
            "max_spread_bps": "",
            "min_top_of_book_notional": 0,
            "exclude_symbol_patterns": [],
            "output_dir": tmp_path / "mexc-data",
        }
    )

    outcome = asyncio.run(main_module.run_observer(config, discover_only=True))

    assert outcome.error is None
    assert outcome.discovery is not None
    assert len(outcome.discovery.symbols) == 3
    markdown = outcome.artifacts.markdown_path.read_text(encoding="utf-8")
    assert markdown.startswith("# MEXC Spot Triangular Arbitrage Simulation Report")
    assert "MEXC fee warning" in markdown


def test_report_only_cli_requires_no_network(tmp_path, monkeypatch):
    def should_not_construct(*_args, **_kwargs):
        raise AssertionError("report-only mode attempted to create a network client")

    monkeypatch.setattr(main_module, "BinancePublicClient", should_not_construct)
    source = tmp_path / "signals.jsonl"
    source.write_text(
        json.dumps(
            {
                "category": "signal",
                "data": {
                    "signal_id": "offline",
                    "cycle_id": "fixture",
                    "raw_return": "0.01",
                    "return_after_fees": "0.001",
                    "return_after_depth": "0.001",
                    "estimated_pnl": "0.01",
                    "fully_executable": True,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = main_module.main(
        ["--report-only", str(source), "--output-dir", str(tmp_path / "output")]
    )

    assert result == 0
    assert len(list((tmp_path / "output" / "reports").glob("report_*.md"))) == 1


def test_report_bundle_from_rotation_collects_run_siblings_and_summary(tmp_path):
    root = tmp_path / "data"
    signal_base = root / "signals" / "signals_run-a.jsonl"
    write_jsonl(
        signal_base.with_name(f"{signal_base.name}.1"),
        envelope("run-a", "signal", {"signal_id": "old", "raw_return": "0.01"}),
    )
    write_jsonl(
        signal_base,
        envelope("run-a", "signal", {"signal_id": "new", "raw_return": "0.02"}),
    )
    write_jsonl(
        root / "signals" / "latency_rechecks_run-a.jsonl",
        envelope("run-a", "latency", {"signal_id": "old", "checks": []}),
    )
    write_jsonl(
        root / "raw" / "order_book_health_run-a.jsonl",
        envelope("run-a", "health", {"healthy": True}),
    )
    write_jsonl(
        root / "signals" / "raw_opportunities_run-a.jsonl",
        envelope("run-a", "raw_opportunity", {"signal_id": "duplicate-stage"}),
    )
    for category in ("after_fees", "depth", "pessimistic"):
        write_jsonl(
            root / "signals" / f"{category}_run-a.jsonl",
            envelope(
                "run-a",
                category,
                {"record_type": "signal", "signal_id": f"duplicate-{category}"},
            ),
        )
    write_jsonl(
        root / "reports" / "summary_metrics_run-a.jsonl",
        envelope(
            "run-a",
            "summary",
            {
                "run_id": "run-a",
                "started_at": "2026-01-01T00:00:00+00:00",
                "ended_at": "2026-01-01T00:10:00+00:00",
                "duration_seconds": 600,
                "monitored_symbols": 3,
                "monitored_cycles": 2,
                "error": None,
            },
        ),
    )
    (root / "raw" / "selected_cycles_run-a.json").write_text(
        json.dumps(
            {
                "run_id": "run-a",
                "kind": "selected_cycles",
                "records": [
                    {"cycle_id": "fixture-a"},
                    {"cycle_id": "fixture-zero-signals"},
                ],
            }
        ),
        encoding="utf-8",
    )
    # Another run under the same artifact root must not contaminate a
    # file-selected report.
    write_jsonl(
        root / "signals" / "signals_run-b.jsonl",
        envelope("run-b", "signal", {"signal_id": "foreign", "raw_return": "9"}),
    )

    records, metadata = main_module._load_report_bundle(
        signal_base.with_name(f"{signal_base.name}.1")
    )

    assert {record.get("signal_id") for record in records} == {"old", "new", None}
    assert len(records) == 4
    assert metadata["run_id"] == "run-a"
    assert metadata["duration_seconds"] == 600
    assert metadata["monitored_symbols"] == 3
    assert metadata["monitored_cycle_ids"] == ["fixture-a", "fixture-zero-signals"]


def test_report_only_rejects_a_mixed_run_directory(tmp_path, monkeypatch):
    def should_not_construct(*_args, **_kwargs):
        raise AssertionError("report-only mode attempted to create a network client")

    monkeypatch.setattr(main_module, "BinancePublicClient", should_not_construct)
    source = tmp_path / "mixed"
    write_jsonl(
        source / "a.jsonl",
        envelope("run-a", "signal", {"signal_id": "a", "raw_return": "0.01"}),
    )
    write_jsonl(
        source / "b.jsonl",
        envelope("run-b", "signal", {"signal_id": "b", "raw_return": "0.01"}),
    )

    result = main_module.main(
        ["--report-only", str(source), "--output-dir", str(tmp_path / "output")]
    )

    assert result == 2
    assert not list((tmp_path / "output" / "reports").glob("report_*.md"))


def test_report_directory_rejects_run_scoped_and_anonymous_mixture(tmp_path):
    source = tmp_path / "mixed"
    write_jsonl(
        source / "scoped.jsonl",
        envelope("run-a", "signal", {"signal_id": "a", "raw_return": "0.01"}),
    )
    write_jsonl(
        source / "anonymous.jsonl",
        {"category": "signal", "signal_id": "anonymous", "raw_return": "0.01"},
    )

    with pytest.raises(ValueError, match="run-scoped and unscoped"):
        main_module._load_report_bundle(source)


def test_report_loader_strips_nested_execution_payloads():
    signal = main_module._slim_report_record(
        {
            "_category": "signal",
            "signal_id": "fixture",
            "raw_return": "0.01",
            "return_after_fees": "0.001",
            "limiting_leg": "AAABBB",
            "book_staleness_ms": 12,
            "depth_simulation": {"legs": [{"large": "payload"}]},
        }
    )
    latency = main_module._slim_report_record(
        {
            "_category": "latency",
            "signal_id": "fixture",
            "ghost_arbitrage": True,
            "checks": [
                {
                    "delay_ms": 50,
                    "elapsed_ms": 52,
                    "edge": "-0.01",
                    "profitable": False,
                    "result": {"legs": [{"large": "payload"}]},
                }
            ],
            "initial_result": {"legs": [{"large": "payload"}]},
        }
    )

    assert signal == {
        "_category": "signal",
        "signal_id": "fixture",
        "raw_return": "0.01",
        "return_after_fees": "0.001",
        "limiting_leg": "AAABBB",
        "book_staleness_ms": 12,
    }
    assert latency == {
        "_category": "latency",
        "signal_id": "fixture",
        "ghost_arbitrage": True,
        "checks": [
            {
                "delay_ms": 50,
                "elapsed_ms": 52,
                "edge": "-0.01",
                "profitable": False,
            }
        ],
    }


def test_recorder_report_loader_parses_only_flat_signal_and_latency_fields(tmp_path):
    signals_dir = tmp_path / "signals"
    signal_path = signals_dir / "signals_run-a.jsonl"
    latency_path = signals_dir / "latency_rechecks_run-a.jsonl"
    signal_path.parent.mkdir(parents=True)
    signal_path.write_text(
        json.dumps(
            envelope(
                "run-a",
                "signal",
                {
                    "signal_id": "fixture",
                    "cycle_id": "cycle-a",
                    "raw_return": "0.01",
                    "return_after_fees": "-0.001",
                    "raw_simulation": {"legs": [{"large": "payload"}]},
                },
            ),
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    latency_path.write_text(
        json.dumps(
            envelope(
                "run-a",
                "latency",
                {
                    "signal_id": "fixture",
                    "cycle_id": "cycle-a",
                    "initial_edge": "-0.001",
                    "initial_result": {"legs": [{"large": "payload"}]},
                    "checks": [
                        {
                            "delay_ms": 50,
                            "elapsed_ms": "52",
                            "edge": "-0.001",
                            "profitable": False,
                            "result": {"legs": [{"large": "payload"}]},
                        }
                    ],
                    "disappeared": True,
                    "ghost_arbitrage": True,
                    "return_after_50ms": "-0.001",
                    "profitable_after_50ms": False,
                    "elapsed_after_50ms": "52",
                },
            ),
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    signal = main_module._load_slim_report_jsonl(signal_path)
    latency = main_module._load_slim_report_jsonl(latency_path)

    assert signal == [
        {
            "signal_id": "fixture",
            "cycle_id": "cycle-a",
            "raw_return": "0.01",
            "return_after_fees": "-0.001",
            "_category": "signal",
            "_recorded_at": "2026-01-01T00:00:00+00:00",
            "_run_id": "run-a",
        }
    ]
    assert latency == [
        {
            "signal_id": "fixture",
            "cycle_id": "cycle-a",
            "initial_edge": "-0.001",
            "ghost_arbitrage": True,
            "return_after_50ms": "-0.001",
            "profitable_after_50ms": False,
            "elapsed_after_50ms": "52",
            "_category": "latency",
            "_recorded_at": "2026-01-01T00:00:00+00:00",
            "_run_id": "run-a",
        }
    ]


def test_requested_cli_flags_override_configuration(tmp_path):
    args = main_module.build_parser().parse_args(
        [
            "--exchange",
            "mexc",
            "--duration-minutes",
            "10",
            "--max-symbols",
            "12",
            "--max-cycles",
            "18",
            "--root-asset",
            "usdc",
            "--fee-rate",
            "0.00075",
            "--start-sizes",
            "5,10.5",
            "--latency-buckets",
            "250,50,250",
            "--output-dir",
            str(tmp_path / "artifacts"),
            "--log-level",
            "DEBUG",
            "--discover-only",
        ]
    )

    config = load_config(args.config, main_module._config_overrides(args))

    assert config.exchange == "mexc"
    assert config.network.rest_base_url == "https://api.mexc.com"
    assert config.order_book.max_streams_per_connection == 30
    assert config.duration_minutes == 10
    assert config.max_symbols == 12
    assert config.max_cycles == 18
    assert config.root_asset == "USDC"
    assert config.simulation.fee_rate == Decimal("0.00075")
    assert config.simulation.start_sizes == (Decimal("5"), Decimal("10.5"))
    assert config.simulation.latency_buckets_ms == (50, 250)
    assert config.output.output_dir == tmp_path / "artifacts"
    assert config.output.log_level == "DEBUG"
    assert args.discover_only is True


def test_data_dir_and_compact_storage_cli_overrides(tmp_path):
    data_dir = tmp_path / "external-data"
    args = main_module.build_parser().parse_args(
        [
            "--data-dir",
            str(data_dir),
            "--storage-mode",
            "compact",
            "--min-free-gib",
            "5",
            "--raw-sample-rate",
            "0.01",
            "--top-n",
            "25",
            "--near-break-even-threshold",
            "-0.0002",
            "--discover-only",
        ]
    )

    config = load_config(args.config, main_module._config_overrides(args))

    assert config.output.output_dir == data_dir
    assert config.output.storage_mode == "compact"
    assert config.output.min_free_gib == 5
    assert config.output.raw_sample_rate == 0.01
    assert config.output.top_n == 25
    assert config.output.near_break_even_threshold == Decimal("-0.0002")


class FailingPublicClient(FakePublicClient):
    async def exchange_info(self):
        raise RuntimeError("synthetic discovery failure")


def test_runtime_failure_still_writes_summary_and_report(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "BinancePublicClient", FailingPublicClient)
    config = config_from_mapping({"output_dir": tmp_path / "data"})

    outcome = asyncio.run(main_module.run_observer(config, discover_only=True))

    assert outcome.error == "RuntimeError: synthetic discovery failure"
    assert outcome.artifacts.markdown_path.exists()
    assert outcome.artifacts.metrics["run_error"] == outcome.error
    summary = next((tmp_path / "data" / "reports").glob("summary_metrics_*.jsonl"))
    records, metadata = main_module._load_report_bundle(summary)
    assert records == []
    assert metadata["run_id"] == outcome.run_id
    assert metadata["error"] == outcome.error


class BlockingPublicClient(FakePublicClient):
    def __init__(self):
        self.started = asyncio.Event()

    async def exchange_info(self):
        self.started.set()
        await asyncio.Future()

    async def ticker_24hr(self):
        await asyncio.Future()


def test_task_cancellation_finishes_diagnostic_report(tmp_path, monkeypatch):
    client = BlockingPublicClient()
    monkeypatch.setattr(main_module, "BinancePublicClient", lambda *_args, **_kwargs: client)
    config = config_from_mapping({"output_dir": tmp_path / "data"})

    async def exercise():
        task = asyncio.create_task(main_module.run_observer(config, discover_only=True))
        await asyncio.wait_for(client.started.wait(), timeout=1)
        task.cancel()
        outcome = await asyncio.wait_for(task, timeout=2)
        return outcome, task.cancelled(), task.cancelling()

    outcome, cancelled, cancelling = asyncio.run(exercise())

    assert outcome.error == "observer task was cancelled"
    assert outcome.artifacts.markdown_path.exists()
    assert outcome.artifacts.metrics["run_error"] == outcome.error
    assert cancelled is False
    assert cancelling == 0
