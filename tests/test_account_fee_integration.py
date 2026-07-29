from decimal import Decimal

import pytest

import tri_arb.main as main_module
from tri_arb.account_fees import AccountFeeError, FeeSchedule
from tri_arb.config import config_from_mapping, load_config
from tri_arb.models import BookSide, CycleLeg, LegSide, TriangularCycle
from tri_arb.report import calculate_metrics, render_markdown
from tri_arb.scanner import OpportunityScanner
from tri_arb.simulator import CycleSimulator


class StaticBook:
    def __init__(self, bids, asks):
        self._bids = [(Decimal(str(price)), Decimal(str(quantity))) for price, quantity in bids]
        self._asks = [(Decimal(str(price)), Decimal(str(quantity))) for price, quantity in asks]

    def bid_levels(self, limit=None):
        return self._bids if limit is None else self._bids[:limit]

    def ask_levels(self, limit=None):
        return self._asks if limit is None else self._asks[:limit]


def cycle() -> TriangularCycle:
    return TriangularCycle(
        cycle_id="USDT-BTC-ETH-USDT",
        root_asset="USDT",
        assets=("USDT", "BTC", "ETH", "USDT"),
        symbols=("BTCUSDT", "ETHBTC", "ETHUSDT"),
        legs=(
            CycleLeg(
                "USDT",
                "BTC",
                "BTCUSDT",
                "BTC",
                "USDT",
                LegSide.BUY_BASE,
                BookSide.ASKS,
            ),
            CycleLeg(
                "BTC",
                "ETH",
                "ETHBTC",
                "ETH",
                "BTC",
                LegSide.BUY_BASE,
                BookSide.ASKS,
            ),
            CycleLeg(
                "ETH",
                "USDT",
                "ETHUSDT",
                "ETH",
                "USDT",
                LegSide.SELL_BASE,
                BookSide.BIDS,
            ),
        ),
        liquidity_score=Decimal("1"),
        spread_score=Decimal("1"),
        feasibility_score=Decimal("1"),
    )


def books():
    return {
        "BTCUSDT": StaticBook(bids=[(10, 100)], asks=[(10, 100)]),
        "ETHBTC": StaticBook(bids=[(2, 100)], asks=[(2, 100)]),
        "ETHUSDT": StaticBook(bids=[(20, 100)], asks=[(20, 100)]),
    }


def test_symbol_specific_fees_apply_per_leg_with_maximum_fallback():
    simulator = CycleSimulator(
        fee_rate=Decimal("0.003"),
        symbol_fee_rates={
            "BTCUSDT": Decimal("0.001"),
            "ETHBTC": Decimal("0.002"),
        },
    )

    result = simulator.simulate(cycle(), books(), Decimal("100"), mode="top")

    assert [leg.fee_rate for leg in result.legs] == [
        Decimal("0.001"),
        Decimal("0.002"),
        Decimal("0.003"),
    ]
    assert result.final_amount == Decimal("100") * Decimal("0.999") * Decimal("0.998") * Decimal(
        "0.997"
    )


def test_scanner_propagates_symbol_fees_to_realistic_and_latency_models():
    schedule = {"BTCUSDT": Decimal("0.0001")}
    scanner = OpportunityScanner(
        [cycle()],
        {},
        object(),
        fee_rate=Decimal("0.001"),
        symbol_fee_rates=schedule,
        start_sizes=(Decimal("10"),),
        latency_buckets_ms=(50,),
        quantity_haircut=Decimal("0.25"),
        extra_slippage_bps=Decimal("1"),
    )

    assert scanner.simulator.fee_rate_for_symbol("BTCUSDT") == Decimal("0.0001")
    assert scanner.pessimistic_simulator.fee_rate_for_symbol("BTCUSDT") == Decimal("0.0001")
    assert scanner.latency_tracker.simulator is scanner.simulator
    assert scanner.simulator.fee_rate_for_symbol("MISSING") == Decimal("0.001")


def test_manual_cli_fee_override_is_labeled_and_reaches_effective_config():
    args = main_module.build_parser().parse_args(["--exchange", "mexc", "--fee-rate", "0"])
    config = load_config(args.config, main_module._config_overrides(args))

    schedule = main_module._resolve_fee_schedule(args, config)

    assert config.simulation.fee_rate == Decimal("0")
    assert schedule.source == "fixed_cli_fee"
    assert schedule.fallback_taker_fee == Decimal("0")


def test_use_account_fees_loads_schedule_and_conservative_maximum(tmp_path, monkeypatch):
    fee_path = tmp_path / "mexc_account_fee.yaml"
    fee_path.write_text(
        "\n".join(
            (
                "fee_source: mexc_account_tradeFee_read_only",
                "generated_at: '2026-07-29T12:00:00+00:00'",
                "fallback_taker_fee: '0.0005'",
                "symbol_taker_fees:",
                "  BTCUSDT: '0.0001'",
                "  ETHBTC: '0.0007'",
                "symbol_maker_fees:",
                "  BTCUSDT: '0'",
                "  ETHBTC: '0'",
                "",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(main_module, "DEFAULT_NORMALIZED_FEE_PATH", fee_path)
    args = main_module.build_parser().parse_args(["--exchange", "mexc", "--use-account-fees"])
    config = load_config(args.config, main_module._config_overrides(args))

    schedule = main_module._resolve_fee_schedule(args, config)

    assert schedule.source == "mexc_account_tradeFee_read_only"
    assert schedule.fee_for_symbol("BTCUSDT") == Decimal("0.0001")
    assert schedule.fee_for_symbol("UNKNOWN") == Decimal("0.0007")
    assumptions = main_module._assumptions(config, schedule)
    assert assumptions["fee_source"] == "mexc_account_tradeFee_read_only"
    assert assumptions["fee_rate_per_taker_leg"] == "0.0007"


def test_missing_account_fee_config_has_actionable_command(tmp_path, monkeypatch):
    missing = tmp_path / "missing.yaml"
    monkeypatch.setattr(main_module, "DEFAULT_NORMALIZED_FEE_PATH", missing)
    args = main_module.build_parser().parse_args(["--exchange", "mexc", "--use-account-fees"])
    config = config_from_mapping({"exchange": "mexc"})

    with pytest.raises(
        AccountFeeError,
        match=r"Run python -m tri_arb\.tools\.check_mexc_fees first",
    ):
        main_module._resolve_fee_schedule(args, config)


def test_account_fees_are_rejected_for_binance():
    args = main_module.build_parser().parse_args(["--use-account-fees"])
    config = config_from_mapping()

    with pytest.raises(AccountFeeError, match="only with --exchange mexc"):
        main_module._resolve_fee_schedule(args, config)


def test_fee_flags_are_mutually_exclusive():
    parser = main_module.build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--fee-rate", "0", "--use-account-fees"])

    assert exc_info.value.code == 2


def test_config_fee_schedule_is_the_default_report_source():
    config = config_from_mapping({"fee_rate": "0.00075"})
    args = main_module.build_parser().parse_args([])

    schedule = main_module._resolve_fee_schedule(args, config)

    assert schedule == FeeSchedule(
        source="config_fee",
        fallback_taker_fee=Decimal("0.00075"),
        symbol_taker_fees={},
    )
    assert main_module._assumptions(config, schedule)["fee_source"] == "config_fee"


def test_report_uses_effective_account_fee_source_and_maximum_fallback():
    config = config_from_mapping({"exchange": "mexc"})
    schedule = FeeSchedule(
        source="mexc_account_tradeFee_read_only",
        fallback_taker_fee=Decimal("0.0005"),
        symbol_taker_fees={"BTCUSDT": Decimal("0.0002")},
    )

    metrics = calculate_metrics(
        [
            {
                "signal_id": "one",
                "cycle_id": "USDT-BTC-ETH-USDT",
                "raw_return": "0.001",
                "return_after_fees": "-0.0005",
                "return_after_depth": "-0.0006",
                "pessimistic_return": "-0.0007",
                "estimated_pnl": "-0.006",
                "fully_executable": True,
            }
        ],
        {
            "duration_seconds": 60,
            "assumptions": main_module._assumptions(config, schedule),
        },
    )

    assert metrics["fee_source"] == "mexc_account_tradeFee_read_only"
    assert metrics["fee_assumption_per_leg"] == 0.0005
    assert metrics["assumptions"]["symbol_taker_fees"] == {"BTCUSDT": "0.0002"}
    markdown = render_markdown(metrics)
    assert "Maximum/fallback taker fee per leg | 0.05000%" in markdown
    assert "Symbol-specific taker fee range | 0.02000% to 0.02000%" in markdown
    assert "Simulated taker fee per leg" not in markdown


def test_report_metadata_limits_fee_detail_to_monitored_symbols():
    config = config_from_mapping({"exchange": "mexc"})
    schedule = FeeSchedule(
        source="mexc_account_tradeFee_read_only",
        fallback_taker_fee=Decimal("0.0007"),
        symbol_taker_fees={
            "BTCUSDT": Decimal("0.0002"),
            "ETHUSDT": Decimal("0.0003"),
        },
    )

    assumptions = main_module._assumptions(config, schedule, ("BTCUSDT", "MISSING"))

    assert assumptions["account_fee_symbol_count"] == 2
    assert assumptions["reported_symbol_fee_count"] == 1
    assert assumptions["symbol_taker_fees"] == {"BTCUSDT": "0.0002"}
