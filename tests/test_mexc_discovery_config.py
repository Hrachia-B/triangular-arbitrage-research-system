from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tri_arb.config import ConfigError, DiscoveryConfig, config_from_mapping, load_config
from tri_arb.discovery import (
    combine_market_stats,
    discover_market,
    eligible_symbols,
    parse_exchange_info,
    parse_ticker_stats,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _symbol(symbol: str, base: str, quote: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "baseAsset": base,
        "quoteAsset": quote,
        "status": "TRADING",
        "isSpotTradingAllowed": True,
        "permissions": ["SPOT"],
        "orderTypes": ["MARKET"],
        "filters": [],
    }


def _ticker(
    symbol: str,
    quote_volume: str,
    bid: str,
    ask: str,
    *,
    bid_qty: str = "200000",
    ask_qty: str = "200000",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "quoteVolume": quote_volume,
        "volume": "1000000",
        "bidPrice": bid,
        "bidQty": bid_qty,
        "askPrice": ask,
        "askQty": ask_qty,
        "lastPrice": bid,
        "weightedAvgPrice": bid,
        "count": 1000,
    }


def _cross_quoted_triangle() -> tuple[dict[str, object], list[dict[str, object]]]:
    exchange_info = {
        "symbols": [
            _symbol("AAAUSDT", "AAA", "USDT"),
            _symbol("AAABTC", "AAA", "BTC"),
            _symbol("BTCUSDT", "BTC", "USDT"),
        ]
    }
    tickers = [
        _ticker("AAAUSDT", "150000", "0.999", "1.001"),
        # Three BTC of quote turnover is about 150,000 USDT. The native value
        # deliberately looks tiny so this catches unit-confused filtering.
        _ticker("AAABTC", "3", "0.00001999", "0.00002001", bid_qty="5000000", ask_qty="5000000"),
        _ticker("BTCUSDT", "100000000", "49999", "50001", bid_qty="10", ask_qty="10"),
    ]
    return exchange_info, tickers


def test_exchange_selection_sets_public_endpoint_and_fee_defaults() -> None:
    binance = config_from_mapping()
    mexc = config_from_mapping({"exchange": "MEXC"})

    assert binance.exchange == "binance"
    assert binance.network.rest_base_url == "https://data-api.binance.vision"
    assert mexc.exchange == "mexc"
    assert mexc.network.rest_base_url == "https://api.mexc.com"
    assert mexc.network.websocket_base_url == "wss://wbs-api.mexc.com/ws"
    assert mexc.discovery.min_quote_volume_usdt == Decimal("100000")
    assert mexc.discovery.max_spread_bps == Decimal("50")
    assert mexc.discovery.min_top_of_book_notional == Decimal("50")
    assert "*3L*" in mexc.discovery.exclude_symbol_patterns
    assert mexc.order_book.max_streams_per_connection == 30
    assert mexc.simulation.fee_sensitivity_rates == tuple(
        Decimal(value) for value in ("0.001", "0.0005", "0.0002", "0.0001", "0")
    )

    with pytest.raises(ConfigError, match="exchange must be one of"):
        config_from_mapping({"exchange": "private-desk"})

    ten_ms = config_from_mapping({"exchange": "mexc", "order_book": {"stream_interval_ms": 10}})
    assert ten_ms.order_book.stream_interval_ms == 10


def test_mexc_checked_in_profile_is_strict_bounded_and_research_sized() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "mexc_default.yaml")

    assert config.exchange == "mexc"
    assert config.duration_minutes == 60
    assert config.discovery.root_asset == "USDT"
    assert config.discovery.min_quote_volume_usdt == Decimal("100000")
    assert config.discovery.max_spread_bps == Decimal("50")
    assert config.discovery.min_top_of_book_notional == Decimal("50")
    assert config.discovery.max_symbols == 50
    assert config.discovery.max_cycles == 50
    assert config.order_book.max_streams_per_connection == 30
    assert config.simulation.start_sizes == tuple(
        Decimal(value) for value in ("10", "25", "50", "100")
    )
    assert config.simulation.fee_sensitivity_rates == tuple(
        Decimal(value) for value in ("0.001", "0.0005", "0.0002", "0.0001", "0")
    )
    assert config.decision.minimum_positive_checkpoints == 2


def test_discovery_filters_cross_quotes_by_usdt_equivalent_volume() -> None:
    exchange_info, tickers = _cross_quoted_triangle()

    accepted = discover_market(
        exchange_info,
        tickers,
        DiscoveryConfig(
            max_symbols=3,
            max_cycles=2,
            min_quote_volume_usdt=Decimal("100000"),
            max_spread_bps=Decimal("25"),
            min_top_of_book_notional=Decimal("1000"),
        ),
    )
    rejected = discover_market(
        exchange_info,
        tickers,
        DiscoveryConfig(
            max_symbols=3,
            max_cycles=2,
            min_quote_volume_usdt=Decimal("200000"),
        ),
    )

    assert set(accepted.symbol_names) == {"AAABTC", "AAAUSDT", "BTCUSDT"}
    assert len(accepted.cycles) == 2
    assert rejected.cycles == ()


def test_spread_top_notional_asset_and_symbol_glob_filters() -> None:
    exchange_info, tickers = _cross_quoted_triangle()
    exchange_info["symbols"].append(_symbol("SCAMUSDT", "SCAM", "USDT"))  # type: ignore[union-attr]
    tickers.append(_ticker("SCAMUSDT", "5000000", "0.90", "1.10"))
    combined = combine_market_stats(
        parse_exchange_info(exchange_info),
        parse_ticker_stats(tickers),
    )

    filtered = eligible_symbols(
        combined,
        DiscoveryConfig(
            max_spread_bps=Decimal("25"),
            min_top_of_book_notional=Decimal("1000"),
            exclude_symbol_patterns=("scam*",),
            exclude_assets=("unused",),
        ),
    )
    names = {symbol.symbol for symbol in filtered}

    assert names == {"AAABTC", "AAAUSDT", "BTCUSDT"}

    thin_tickers = [
        ticker | ({"askQty": "0.5"} if ticker["symbol"] == "AAABTC" else {}) for ticker in tickers
    ]
    thin = combine_market_stats(
        parse_exchange_info(exchange_info),
        parse_ticker_stats(thin_tickers),
    )
    thin_names = {
        symbol.symbol
        for symbol in eligible_symbols(
            thin,
            DiscoveryConfig(
                max_spread_bps=Decimal("25"),
                min_top_of_book_notional=Decimal("1000"),
                exclude_symbol_patterns=("SCAM*",),
            ),
        )
    }
    assert "AAABTC" not in thin_names


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"min_quote_volume_usdt": -1}, "min_quote_volume_usdt cannot be negative"),
        ({"max_spread_bps": -1}, "max_spread_bps cannot be negative"),
        ({"min_top_of_book_notional": -1}, "min_top_of_book_notional cannot be negative"),
        (
            {"simulation": {"fee_sensitivity_rates": []}},
            "fee_sensitivity_rates cannot be empty",
        ),
        (
            {"decision": {"minimum_positive_checkpoints": 1}},
            "minimum_positive_checkpoints must be at least 2",
        ),
    ],
)
def test_strict_discovery_and_fee_settings_are_validated(
    values: dict[str, object], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        config_from_mapping(values)
