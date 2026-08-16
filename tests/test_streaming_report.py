import pytest

from tri_arb.recorder import JSONLRecorder
from tri_arb.report import ReportGenerator, StreamingReportAccumulator


def _positive_signal(index: int) -> dict[str, object]:
    raw_edge = index / 10_000
    return {
        "signal_id": f"signal-{index}",
        "cycle_id": "USDT-A-B-USDT",
        "start_size": 10,
        "raw_return": raw_edge,
        "return_after_fees": raw_edge - 0.0001,
        "return_after_depth": raw_edge - 0.0001,
        "pessimistic_return": raw_edge - 0.0001,
        "profitable_after_depth": raw_edge > 0.0001,
        "profitable_pessimistic": raw_edge > 0.0001,
        "estimated_pnl": (raw_edge - 0.0001) * 10,
        "fully_executable": True,
        "average_book_staleness_ms": index,
    }


def _latency(index: int) -> dict[str, object]:
    profitable = index > 1
    return {
        "signal_id": f"signal-{index}",
        "cycle_id": "USDT-A-B-USDT",
        "ghost_arbitrage": not profitable,
        "lifetime_ms": 50 + index,
        "checks": [
            {
                "delay_ms": 50,
                "elapsed_ms": 52,
                "edge": 0.001 if profitable else -0.001,
                "profitable": profitable,
            }
        ],
    }


def test_streaming_accumulator_is_bounded_but_keeps_exact_cumulative_totals(tmp_path):
    accumulator = StreamingReportAccumulator(
        fee_sensitivity_rates=(0.001, 0.0),
        latency_buckets_ms=(50,),
        sample_limit=8,
    )
    for index in range(100):
        accumulator.observe("signal", _positive_signal(index))
        accumulator.observe("latency", _latency(index))

    snapshot = accumulator.snapshot()
    metrics = snapshot.calculate_metrics(
        {
            "duration_seconds": 60 * 60 * 48,
            "minimum_decision_sample": 20,
            "monitored_cycle_ids": ["USDT-A-B-USDT"],
            "latency_buckets_ms": [50],
        }
    )

    assert len(snapshot.records) == 16
    assert metrics["total_records"] == 200
    assert metrics["raw_opportunities"] == 100
    assert metrics["latency_observations"] == 100
    assert metrics["raw_edge_percentiles"]["sample_count"] == 100
    assert metrics["raw_edge_percentiles"]["min"] == 0
    assert metrics["raw_edge_percentiles"]["max"] == pytest.approx(0.0099)
    assert metrics["average_raw_edge"] == pytest.approx(0.00495)
    assert metrics["book_staleness_sample_count"] == 100
    assert metrics["average_book_staleness_ms"] == pytest.approx(49.5)
    assert metrics["fee_sensitivity"][1]["sample_count"] == 100
    assert metrics["fee_sensitivity"][1]["profitable_count"] == 99
    assert metrics["latency_sample_counts"][50] == 100
    assert metrics["latency_profitable_counts"][50] == 98
    assert metrics["maximum_latency_lateness_ms"] == 2
    assert metrics["best_raw_opportunities"][0]["raw_edge"] == pytest.approx(0.0099)
    assert metrics["aggregation_mode"] == "cumulative_streaming_bounded_sample"
    assert metrics["quantiles_approximate"] is True
    assert metrics["quantile_sample_limit"] == 8
    assert metrics["signal_quantile_sample_size"] == 8
    assert metrics["latency_quantile_sample_size"] == 8
    aggregation_metadata = accumulator.aggregation_metadata()["report_aggregation"]
    assert aggregation_metadata["sample_limit_per_record_type"] == 8
    assert aggregation_metadata["approximate"] == ("percentiles", "medians")
    assert "counts" in aggregation_metadata["exact"]

    artifacts = ReportGenerator(tmp_path).generate_metrics(
        metrics,
        {"duration_seconds": 60 * 60 * 48},
        timestamp="bounded",
    )
    markdown = artifacts.markdown_path.read_text(encoding="utf-8")
    assert "Cumulative streaming report" in markdown
    assert "percentiles and medians use a bounded deterministic sample" in markdown
    assert "8 sampled signals out of 100 cumulative signals" in markdown
    assert "Samples column is the exact cumulative population" in markdown
    assert "current samples: 8 / 8" in markdown


def test_48h_continue_requires_positive_evidence_at_multiple_checkpoints():
    accumulator = StreamingReportAccumulator(
        fee_sensitivity_rates=(0.0,),
        latency_buckets_ms=(50,),
        sample_limit=8,
    )
    accumulator.observe("signal", _positive_signal(10))
    accumulator.observe("latency", _latency(10))
    metadata = {
        "duration_seconds": 60 * 60 * 48,
        "minimum_decision_sample": 1,
        "monitored_cycle_ids": ["USDT-A-B-USDT"],
        "latency_buckets_ms": [50],
        "decision": {
            "minimum_duration_minutes": 2880,
            "minimum_sample_size": 1,
            "repeated_positive_cycle_min_signals": 1,
            "survival_buckets_ms": [50],
            "minimum_total_estimated_pnl": 0,
            "minimum_positive_checkpoints": 2,
        },
    }
    metrics = accumulator.snapshot().calculate_metrics(metadata)

    first = accumulator.prepare_checkpoint(metrics, metadata)
    assert first == {"checkpoint_count": 1, "positive_checkpoint_count": 1}
    assert accumulator.checkpoint_metadata() == {
        "checkpoint_count": 0,
        "positive_checkpoint_count": 0,
    }
    assert metrics["decision_48h"] == "UNCLEAR"
    assert "1/2" in metrics["decision_48h_rationale"]
    accumulator.commit_checkpoint(first)

    second = accumulator.record_checkpoint(metrics, metadata)
    assert second == {"checkpoint_count": 2, "positive_checkpoint_count": 2}
    assert metrics["decision_48h"] == "CONTINUE_TO_7D"

    persisted = accumulator.snapshot().calculate_metrics({**metadata, **second})
    assert persisted["checkpoint_count"] == 2
    assert persisted["positive_checkpoint_count"] == 2
    assert persisted["decision_48h"] == "CONTINUE_TO_7D"


@pytest.mark.asyncio
async def test_recorder_observer_runs_only_after_a_successful_append(tmp_path):
    observed: list[tuple[str, object]] = []
    recorder = JSONLRecorder(
        tmp_path,
        record_observer=lambda category, value: observed.append((category, value)),
    )

    path = await recorder.record("signal", {"signal_id": "durable"})

    assert path.read_text(encoding="utf-8")
    assert observed == [("signal", {"signal_id": "durable"})]


@pytest.mark.asyncio
async def test_compact_recorder_keeps_survivors_near_break_even_and_top_opportunities(tmp_path):
    observed: list[tuple[str, object]] = []
    recorder = JSONLRecorder(
        tmp_path,
        run_id="compact",
        storage_mode="compact",
        raw_sample_rate=0,
        top_n=2,
        near_break_even_threshold="-0.0005",
        record_observer=lambda category, value: observed.append((category, value)),
    )

    await recorder.record(
        "signal",
        {"signal_id": "discarded", "raw_return": "0.001", "return_after_fees": "-0.001"},
    )
    await recorder.record(
        "signal",
        {"signal_id": "near", "raw_return": "0.002", "return_after_fees": "-0.0004"},
    )
    await recorder.record(
        "signal",
        {
            "signal_id": "survivor",
            "raw_return": "0.003",
            "return_after_fees": "0.0001",
            "return_after_depth": "0.00005",
            "pessimistic_return": "0.00001",
            "profitable_after_fees": True,
            "profitable_after_depth": True,
            "profitable_pessimistic": True,
        },
    )
    recorder.close()

    text = recorder.path_for("signal").read_text(encoding="utf-8")
    assert '"signal_id":"near"' in text
    assert '"signal_id":"survivor"' in text
    assert '"signal_id":"discarded"' not in text
    assert len(observed) == 3

    retained = tmp_path / "signals" / "compact_top_opportunities_compact.json"
    payload = retained.read_text(encoding="utf-8")
    assert '"top_raw_opportunities"' in payload
    assert '"top_net_opportunities"' in payload
    assert '"top_realistic_opportunities"' in payload
    assert '"signal_id": "survivor"' in payload
