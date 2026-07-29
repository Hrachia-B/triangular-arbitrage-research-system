import asyncio
import csv
import json
import math
import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tri_arb.latency import LatencyTracker
from tri_arb.recorder import JSONLRecorder
from tri_arb.report import ReportGenerator, calculate_metrics


def synthetic_records(count=20, profitable=True):
    edge = "0.002" if profitable else "-0.002"
    pnl = "0.20" if profitable else "-0.20"
    records = []
    for index in range(count):
        records.append(
            {
                "category": "signal",
                "recorded_at": f"2026-01-01T00:00:{index:02d}+00:00",
                "data": {
                    "signal_id": f"signal-{index}",
                    "cycle_id": "USDT-A-B-USDT" if index % 2 == 0 else "USDT-C-D-USDT",
                    "gross_return": "0.005",
                    "net_return": edge,
                    "return_after_fees": edge,
                    "return_after_depth": edge,
                    "pnl": pnl,
                    "fully_executable": True,
                    "pessimistic_return": edge,
                    "profitable_pessimistic": profitable,
                    "book_staleness_ms": 12 + index,
                },
            }
        )
        checks = [
            {
                "delay_ms": delay,
                "elapsed_ms": str(Decimal(delay) + Decimal("2")),
                "edge": edge,
                "pnl": pnl,
                "profitable": profitable,
                "executable": True,
            }
            for delay in (50, 100, 250, 500, 1000)
        ]
        records.append(
            {
                "category": "latency",
                "recorded_at": f"2026-01-01T00:01:{index:02d}+00:00",
                "data": {
                    "signal_id": f"signal-{index}",
                    "cycle_id": "USDT-A-B-USDT" if index % 2 == 0 else "USDT-C-D-USDT",
                    "checks": checks,
                    "lifetime_ms": 1000 if profitable else 50,
                    "ghost_arbitrage": not profitable,
                },
            }
        )
    records.extend(
        [
            {"category": "health", "data": {"event": "sequence_gap", "symbol": "AAAUSDT"}},
            {"category": "health", "data": {"event": "resync_complete", "symbol": "AAAUSDT"}},
        ]
    )
    return records


def test_report_generation_from_synthetic_records(tmp_path):
    output = tmp_path / "reports"
    artifacts = ReportGenerator(output).generate(
        synthetic_records(),
        {
            "monitored_symbols": ["AAAUSDT", "AAABBB", "BBBUSDT"],
            "monitored_cycles": ["USDT-A-B-USDT", "USDT-C-D-USDT"],
            "duration_seconds": 3600,
            "fee_rate": "0.001",
            "start_sizes": [10, 25, 50, 100],
            "latency_buckets_ms": [50, 100, 250, 500, 1000],
            "depth_levels": 100,
            "quantity_haircut": "0.25",
            "extra_slippage_bps": 2,
        },
        timestamp="fixture",
    )

    assert artifacts.conclusion == "PROCEED"
    assert artifacts.markdown_path.name == "report_fixture.md"
    assert artifacts.csv_path.name == "summary_fixture.csv"
    assert artifacts.top_opportunities_path.name == "top_opportunities_fixture.csv"
    markdown = artifacts.markdown_path.read_text()
    assert "**PROCEED**" in markdown
    assert "50 ms | 52.0 ms | 2.0 ms | 20 | 20" in markdown
    assert "This report does not prove live profitability" in markdown
    assert artifacts.metrics["raw_opportunities"] == 20
    assert artifacts.metrics["profitable_after_depth"] == 20
    assert artifacts.metrics["profitable_at_slowest_latency"] == 20
    assert artifacts.metrics["latency_coverage_percentage"] == 100
    assert artifacts.metrics["meaningful_latency_coverage"] is True
    assert artifacts.metrics["maximum_latency_lateness_ms"] == 2
    assert artifacts.metrics["latency_scheduling_accurate"] is True
    assert artifacts.metrics["sequence_gaps"] == 1
    assert artifacts.metrics["order_book_resyncs"] == 1
    with artifacts.csv_path.open(newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["conclusion"] == "PROCEED"
    assert row["profitable_after_1000ms"] == "20"
    assert artifacts.top_opportunities_path.exists()


def test_edge_percentiles_break_even_fees_and_fee_sensitivity():
    raw_edges = [0, 0.01, 0.02, 0.03, 0.04]
    fee_edges = [-0.04, -0.03, -0.02, -0.01, 0]
    records = [
        {
            "category": "signal",
            "data": {
                "signal_id": f"distribution-{index}",
                "cycle_id": "distribution",
                "raw_return": raw_edge,
                "return_after_fees": fee_edge,
                "return_after_depth": fee_edge,
                "fully_executable": True,
            },
        }
        for index, (raw_edge, fee_edge) in enumerate(zip(raw_edges, fee_edges, strict=True))
    ]

    metrics = calculate_metrics(records, {"minimum_decision_sample": 100})

    assert metrics["raw_edge_percentiles"] == pytest.approx(
        {
            "sample_count": 5,
            "min": 0,
            "p50": 0.02,
            "p90": 0.036,
            "p95": 0.038,
            "p99": 0.0396,
            "p99_9": 0.03996,
            "max": 0.04,
        }
    )
    assert metrics["net_edge_percentiles_after_fees"] == pytest.approx(
        {
            "sample_count": 5,
            "min": -0.04,
            "p50": -0.02,
            "p90": -0.004,
            "p95": -0.002,
            "p99": -0.0004,
            "p99_9": -0.00004,
            "max": 0,
        }
    )
    expected_break_even = [1 - (1 + raw_edge) ** (-1 / 3) for raw_edge in raw_edges]
    break_even = metrics["break_even_fee_per_leg"]
    assert break_even["average"] == pytest.approx(sum(expected_break_even) / 5)
    assert break_even["median"] == pytest.approx(expected_break_even[2])
    assert break_even["max"] == pytest.approx(expected_break_even[-1])

    thresholds = [0.00005, 0.00015, 0.0003, 0.0006, 0.0008, 0.0011]
    sensitivity_records = [
        {
            "category": "signal",
            "data": {
                "signal_id": f"sensitivity-{index}",
                "cycle_id": "sensitivity",
                "raw_return": math.expm1(-3 * math.log1p(-threshold)),
                "return_after_fees": -0.001,
                "return_after_depth": -0.001,
                "fully_executable": True,
            },
        }
        for index, threshold in enumerate(thresholds)
    ]
    sensitivity = calculate_metrics(sensitivity_records)["fee_sensitivity"]

    assert [item["fee_rate"] for item in sensitivity] == [
        0.001,
        0.00075,
        0.0005,
        0.0002,
        0.0001,
        0.0,
    ]
    assert [item["profitable_count"] for item in sensitivity] == [1, 2, 3, 4, 5, 6]


def test_mexc_report_uses_persisted_fee_presets_and_account_warning(tmp_path):
    records = [
        {
            "category": "signal",
            "data": {
                "signal_id": "mexc-fee-fixture",
                "cycle_id": "USDT-A-B-USDT",
                "raw_return": "0.002",
                "return_after_fees": "-0.001",
                "return_after_depth": "-0.001",
                "estimated_pnl": "-0.01",
                "fully_executable": True,
            },
        }
    ]
    metadata = {
        "exchange": "MEXC Spot",
        "duration_seconds": 60,
        "assumptions": {
            "exchange": "MEXC Spot",
            "fee_rate_per_taker_leg": "0.001",
            "fee_sensitivity_rates": ["0.001", "0.0005", "0.0002", "0.0001", "0"],
        },
    }

    artifacts = ReportGenerator(tmp_path).generate(records, metadata, timestamp="mexc")

    assert [item["fee_rate"] for item in artifacts.metrics["fee_sensitivity"]] == [
        0.001,
        0.0005,
        0.0002,
        0.0001,
        0.0,
    ]
    markdown = artifacts.markdown_path.read_text(encoding="utf-8")
    assert markdown.startswith("# MEXC Spot Triangular Arbitrage Simulation Report")
    assert "Simulated taker fee per leg | 0.10000%" in markdown
    assert "actual fee shown on the MEXC account fee page and in trade history" in markdown


def test_per_cycle_and_data_quality_diagnostics_distinguish_missing_data():
    records = []
    raw_edges = (0.001, 0.003, 0.005)
    net_edges = (-0.002, 0.0, 0.002)
    for index, (raw_edge, net_edge) in enumerate(zip(raw_edges, net_edges, strict=True)):
        records.append(
            {
                "category": "signal",
                "recorded_at": f"2026-01-01T00:00:0{index}+00:00",
                "data": {
                    "signal_id": f"cycle-a-{index}",
                    "cycle_id": "cycle-a",
                    "start_size": 10,
                    "raw_return": raw_edge,
                    "return_after_fees": net_edge,
                    "return_after_depth": net_edge,
                    "estimated_pnl": net_edge * 10,
                    "limiting_leg": "AAABBB",
                    "book_staleness_ms": 30 + index,
                    "average_book_staleness_ms": 10 + index * 10,
                    "fully_executable": True,
                },
            }
        )
        records.append(
            {
                "category": "latency",
                "recorded_at": f"2026-01-01T00:01:0{index}+00:00",
                "data": {
                    "signal_id": f"cycle-a-{index}",
                    "cycle_id": "cycle-a",
                    "ghost_arbitrage": index < 2,
                    "checks": [
                        {
                            "delay_ms": 50,
                            "elapsed_ms": 55 if index < 2 else 90,
                            "edge": net_edge,
                            "profitable": net_edge > 0,
                        }
                    ],
                },
            }
        )
    records.append(
        {
            "category": "health",
            "recorded_at": "2026-01-01T00:00:00+00:00",
            "data": {
                "health": {
                    "AAAUSDT": {"healthy": False, "age_ms": 999},
                    "AAABBB": {"healthy": False, "age_ms": 999},
                    "BBBUSDT": {"healthy": True, "age_ms": 999},
                }
            },
        }
    )

    metrics = calculate_metrics(
        records,
        {
            "monitored_cycle_ids": ["cycle-a", "cycle-zero"],
            "latency_buckets_ms": [50],
            "assumptions": {"book_stale_after_ms": 2000},
            "scanner_stats": {
                "scans": 100,
                "scan_deadline_misses": 7,
                "total_scan_time_ms": 2500,
                "maximum_scan_time_ms": 80,
            },
        },
    )

    by_cycle = {item["cycle_id"]: item for item in metrics["per_cycle_diagnostics"]}
    assert by_cycle["cycle-a"]["signals"] == 3
    assert by_cycle["cycle-a"]["average_raw_edge"] == pytest.approx(0.003)
    assert by_cycle["cycle-a"]["max_raw_edge"] == pytest.approx(0.005)
    assert by_cycle["cycle-a"]["average_net_edge"] == pytest.approx(0)
    assert by_cycle["cycle-a"]["best_net_edge"] == pytest.approx(0.002)
    assert by_cycle["cycle-a"]["ghost_percentage"] == pytest.approx(200 / 3)
    assert by_cycle["cycle-zero"]["signals"] == 0
    assert by_cycle["cycle-zero"]["average_raw_edge"] is None
    assert by_cycle["cycle-zero"]["ghost_percentage"] is None
    assert "cycle-zero" not in {item["cycle_id"] for item in metrics["best_cycles"]}
    assert metrics["average_book_staleness_ms"] == 20
    assert metrics["book_staleness_sample_count"] == 3
    assert metrics["scan_deadline_miss_percentage"] == pytest.approx(7)
    assert metrics["average_scan_time_ms"] == 25
    assert metrics["maximum_latency_lateness_ms"] == 40
    assert metrics["startup_unhealthy_books"] == 2
    assert metrics["startup_unhealthy_symbols"] == ["AAABBB", "AAAUSDT"]


def test_top_20_raw_opportunities_are_rendered_and_saved(tmp_path):
    records = [
        {
            "category": "signal",
            "data": {
                "signal_id": f"top-{index}",
                "cycle_id": f"cycle-{index:02d}",
                "start_size": index,
                "raw_return": index / 1000,
                "return_after_fees": index / 1000 - 0.003,
                "return_after_depth": index / 1000 - 0.003,
                "estimated_pnl": 0 if index == 21 else index / 100,
                "limiting_leg": f"LEG{index}",
                "book_staleness_ms": index,
                "fully_executable": True,
            },
        }
        for index in range(1, 22)
    ]

    artifacts = ReportGenerator(tmp_path / "reports").generate(
        records,
        {"minimum_decision_sample": 100},
        timestamp="diagnostics",
    )

    top = artifacts.metrics["best_raw_opportunities"]
    assert len(top) == 20
    assert top[0]["raw_edge"] == pytest.approx(0.021)
    assert top[-1]["raw_edge"] == pytest.approx(0.002)
    assert all(item["raw_edge"] != pytest.approx(0.001) for item in top)
    with artifacts.top_opportunities_path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 20
    assert rows[0]["cycle_id"] == "cycle-21"
    assert rows[0]["estimated_pnl"] == "0.0"
    markdown = artifacts.markdown_path.read_text()
    assert "## Diagnostics" in markdown
    assert "### Fee sensitivity" in markdown
    assert "### Best 20 raw opportunities" in markdown
    assert "### Per-cycle diagnostics" in markdown
    assert "## Data-quality warnings" in markdown
    assert "A miss means local cycle evaluation exceeded" in markdown


def test_report_stops_after_meaningful_sample_has_no_depth_edge():
    metrics = calculate_metrics(synthetic_records(profitable=False), {"duration_seconds": 600})

    assert metrics["raw_opportunities"] == 20
    assert metrics["profitable_after_depth"] == 0
    assert metrics["ghost_arbitrage_percentage"] == 100
    assert metrics["conclusion"] == "STOP"


def test_report_does_not_proceed_with_sparse_latency_bad_timing_or_run_error():
    records = synthetic_records()
    signals = [record for record in records if record.get("category") == "signal"]
    latency = [record for record in records if record.get("category") == "latency"]

    sparse = calculate_metrics([*signals, latency[0]], {"duration_seconds": 600})
    errored = calculate_metrics(records, {"duration_seconds": 600, "error": "stream failed"})
    late_records = synthetic_records()
    for envelope in late_records:
        if envelope.get("category") != "latency":
            continue
        for check in envelope["data"]["checks"]:
            check["elapsed_ms"] = str(Decimal(check["delay_ms"]) + Decimal("30"))
    late = calculate_metrics(late_records, {"duration_seconds": 600})

    assert sparse["latency_covered_signals"] == 1
    assert sparse["latency_coverage_percentage"] == 5
    assert sparse["meaningful_latency_coverage"] is False
    assert sparse["conclusion"] == "UNCLEAR"
    assert errored["meaningful_latency_coverage"] is True
    assert errored["conclusion"] == "UNCLEAR"
    assert late["latency_scheduling_accurate"] is False
    assert late["conclusion"] == "UNCLEAR"


def test_report_deduplicates_signal_stages_and_uses_max_health_counters():
    complete = {
        "signal_id": "same-signal",
        "cycle_id": "fixture",
        "raw_return": "0.01",
        "return_after_fees": "0.005",
        "return_after_depth": "0.004",
        "pessimistic_return": "0.001",
        "profitable_pessimistic": True,
        "estimated_pnl": "0.04",
        "fully_executable": True,
    }
    records = [
        {"category": "raw_opportunity", "data": {"signal_id": "same-signal", "raw_return": "0.01"}},
        {"category": "signal", "data": complete},
        {"category": "after_fees", "data": complete},
        {"category": "depth", "data": complete},
        {"category": "pessimistic", "data": complete},
        {"category": "health", "data": {"metrics": {"resyncs": 2, "sequence_gaps": 3}}},
        {
            "category": "health",
            "data": {
                "health": {"AAAUSDT": {"age_ms": 10}, "AAABBB": {"staleness_ms": 30}},
                "metrics": {"resyncs": 5, "book_resyncs": 5, "sequence_gaps": 7},
            },
        },
    ]

    metrics = calculate_metrics(
        records,
        {
            "minimum_decision_sample": 1,
            "scanner_stats": {
                "raw_opportunity_observations": 9,
                "unique_signals": 1,
                "profitable_after_depth": 1,
            },
        },
    )

    assert metrics["raw_opportunities"] == 1
    assert metrics["unique_signals"] == 1
    assert metrics["raw_opportunity_observations"] == 9
    assert metrics["profitable_after_fees"] == 1
    assert metrics["profitable_after_depth"] == 1
    assert metrics["profitable_pessimistic"] == 1
    assert metrics["order_book_resyncs"] == 5
    assert metrics["sequence_gaps"] == 7
    assert metrics["average_book_staleness_ms"] == 20


def test_non_executable_records_do_not_pollute_edge_or_pnl_distributions():
    records = [
        {
            "category": "signal",
            "data": {
                "signal_id": "filled",
                "cycle_id": "fixture",
                "raw_return": "0.02",
                "return_after_depth": "0.01",
                "estimated_pnl": "1",
                "fully_executable": True,
            },
        },
        {
            "category": "signal",
            "data": {
                "signal_id": "rejected",
                "cycle_id": "fixture",
                "raw_return": "0.02",
                "return_after_depth": "-1",
                "estimated_pnl": "-100",
                "fully_executable": False,
                "filter_rejected": True,
            },
        },
    ]

    metrics = calculate_metrics(records, {"minimum_decision_sample": 10})

    assert metrics["average_edge"] == 0.01
    assert metrics["average_estimated_pnl"] == 1
    assert metrics["non_executable_signals"] == 1
    assert metrics["filter_rejected_signals"] == 1


def test_async_jsonl_recorder_writes_decimal_and_report_can_read_it(tmp_path):
    async def write_records():
        recorder = JSONLRecorder(tmp_path / "data", run_id="test", max_bytes=10_000)
        await recorder.record_signal(
            {
                "cycle_id": "fixture",
                "gross_return": Decimal("0.01"),
                "net_return": Decimal("0.002"),
                "pnl": Decimal("0.02"),
                "fully_executable": True,
            }
        )
        selection = recorder.write_selection("symbols", [{"symbol": "AAAUSDT"}])
        path = recorder.path_for("signal")
        await recorder.aclose()
        return path, selection

    signal_path, selection_path = asyncio.run(write_records())

    line = json.loads(signal_path.read_text().strip())
    assert line["data"]["net_return"] == "0.002"
    assert selection_path.exists()
    artifacts = ReportGenerator(tmp_path / "reports").generate(
        signal_path, {"minimum_decision_sample": 1}, timestamp="jsonl"
    )
    assert artifacts.metrics["raw_opportunities"] == 1
    assert artifacts.markdown_path.exists()


def test_latency_result_serializes_without_recursing():
    class Evaluator:
        def __init__(self):
            self.calls = 0

        def simulate(self, cycle, books, start_amount, mode="depth"):
            self.calls += 1
            edge = Decimal("0.01") if self.calls == 1 else Decimal("-0.01")
            return {
                "net_return": edge,
                "pnl": edge * Decimal(str(start_amount)),
                "fully_executable": True,
            }

    async def track():
        tracker = LatencyTracker(Evaluator(), (0,))
        cycle = type("Cycle", (), {"cycle_id": "fixture"})()
        return await tracker.track("signal", cycle, {}, Decimal("10"))

    result = asyncio.run(track())
    payload = result.to_dict()

    assert payload["signal_id"] == "signal"
    assert payload["return_after_0ms"] == "-0.01"
    assert payload["ghost_arbitrage"] is True


def test_latency_deadline_can_include_pre_schedule_work():
    sleep_calls = []
    detected = datetime(2026, 1, 1, tzinfo=UTC)

    class Evaluator:
        def simulate(self, cycle, books, start_amount, mode="depth"):
            return {"net_return": "0.01", "pnl": "0.1", "fully_executable": True}

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    async def track():
        tracker = LatencyTracker(Evaluator(), (50,), sleep=fake_sleep)
        cycle = type("Cycle", (), {"cycle_id": "fixture"})()
        return await tracker.track(
            "signal",
            cycle,
            {},
            Decimal("10"),
            initial_result={"net_return": "0.01", "pnl": "0.1", "fully_executable": True},
            detected_at=detected,
            started_monotonic=time.monotonic() - 0.1,
        )

    result = asyncio.run(track())

    assert result.detected_at == detected
    assert not sleep_calls
    assert result.checks[0].elapsed_ms >= Decimal("100")


def test_recorder_file_work_does_not_block_event_loop(tmp_path, monkeypatch):
    async def scenario():
        recorder = JSONLRecorder(tmp_path / "data", run_id="nonblocking")
        original = recorder._write_envelope

        def slow_write(*args):
            time.sleep(0.05)
            return original(*args)

        monkeypatch.setattr(recorder, "_write_envelope", slow_write)
        write_task = asyncio.create_task(recorder.record("signal", {"value": "fixture"}))
        await asyncio.sleep(0.005)
        assert not write_task.done()
        await write_task
        await recorder.aclose()

    asyncio.run(scenario())
