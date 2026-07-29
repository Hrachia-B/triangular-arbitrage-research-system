from __future__ import annotations

from decimal import Decimal

import pytest

from tri_arb.config import ConfigError, DiscoveryConfig, config_from_mapping, config_to_dict
from tri_arb.discovery import (
    build_triangular_cycles,
    combine_market_stats,
    discover_market,
    eligible_symbols,
    is_obvious_special_asset,
    parse_exchange_info,
    parse_ticker_stats,
)
from tri_arb.models import BookSide, LegSide


def _filters() -> list[dict[str, object]]:
    return [
        {
            "filterType": "PRICE_FILTER",
            "minPrice": "0.00000100",
            "maxPrice": "1000000.00000000",
            "tickSize": "0.00000100",
        },
        {
            "filterType": "LOT_SIZE",
            "minQty": "0.00100000",
            "maxQty": "9000.00000000",
            "stepSize": "0.00100000",
        },
        {
            "filterType": "NOTIONAL",
            "minNotional": "5.00000000",
            "maxNotional": "1000000.00000000",
            "applyMinToMarket": True,
            "applyMaxToMarket": False,
            "avgPriceMins": 5,
        },
        {"filterType": "MAX_NUM_ORDERS", "maxNumOrders": 200},
    ]


def _raw_symbol(
    symbol: str,
    base: str,
    quote: str,
    *,
    status: str = "TRADING",
    spot: bool | None = True,
    permissions: list[str] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "symbol": symbol,
        "baseAsset": base,
        "quoteAsset": quote,
        "status": status,
        "orderTypes": ["LIMIT", "MARKET"],
        "quoteOrderQtyMarketAllowed": True,
        "baseAssetPrecision": 8,
        "quoteAssetPrecision": 8,
        "filters": _filters(),
    }
    if spot is not None:
        result["isSpotTradingAllowed"] = spot
    if permissions is not None:
        result["permissions"] = permissions
    return result


def _ticker(
    symbol: str,
    quote_volume: str,
    bid: str = "99",
    ask: str = "101",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "quoteVolume": quote_volume,
        "volume": "1000.125",
        "bidPrice": bid,
        "bidQty": "12.5",
        "askPrice": ask,
        "askQty": "10.25",
        "lastPrice": "100",
        "weightedAvgPrice": "99.75",
        "count": 1234,
    }


def _triangle_exchange_info() -> dict[str, object]:
    eth_btc = _raw_symbol("ETHBTC", "ETH", "BTC", spot=None)
    eth_btc["permissionSets"] = [["SPOT", "TRD_GRP_004"]]
    return {
        "timezone": "UTC",
        "symbols": [
            _raw_symbol("BTCUSDT", "BTC", "USDT"),
            eth_btc,
            _raw_symbol("ETHUSDT", "ETH", "USDT"),
        ],
    }


def _triangle_tickers() -> list[dict[str, object]]:
    return [
        _ticker("BTCUSDT", "1000000000", "59999", "60001"),
        _ticker("ETHBTC", "800000000", "0.0499", "0.0501"),
        _ticker("ETHUSDT", "900000000", "2999", "3001"),
    ]


def test_exchange_info_parses_permissions_and_decimal_filters() -> None:
    symbols = parse_exchange_info(_triangle_exchange_info())
    by_name = {symbol.symbol: symbol for symbol in symbols}

    eth_btc = by_name["ETHBTC"]
    assert eth_btc.is_spot_trading_allowed
    assert {"SPOT", "TRD_GRP_004"} <= eth_btc.permissions

    btc_usdt = by_name["BTCUSDT"]
    assert btc_usdt.tick_size == Decimal("0.00000100")
    assert btc_usdt.min_qty == Decimal("0.00100000")
    assert btc_usdt.step_size == Decimal("0.00100000")
    assert btc_usdt.min_notional == Decimal("5.00000000")
    assert btc_usdt.max_notional == Decimal("1000000.00000000")
    assert "MAX_NUM_ORDERS" in btc_usdt.filters.filter_types
    assert isinstance(btc_usdt.min_notional, Decimal)


def test_discovery_filters_halted_non_spot_and_leveraged_tokens() -> None:
    raw = _triangle_exchange_info()
    raw["symbols"].extend(  # type: ignore[union-attr]
        [
            _raw_symbol("OLDUSDT", "OLD", "USDT", status="BREAK"),
            _raw_symbol("MARGINUSDT", "MARGIN", "USDT", spot=False),
            _raw_symbol("ETHUPUSDT", "ETHUP", "USDT"),
            _raw_symbol("JUPUSDT", "JUP", "USDT"),
        ]
    )
    tickers = [
        *_triangle_tickers(),
        _ticker("OLDUSDT", "100"),
        _ticker("MARGINUSDT", "100"),
        _ticker("ETHUPUSDT", "100"),
        _ticker("JUPUSDT", "100"),
    ]
    symbols = combine_market_stats(parse_exchange_info(raw), parse_ticker_stats(tickers))
    names = {symbol.symbol for symbol in eligible_symbols(symbols)}

    assert "OLDUSDT" not in names
    assert "MARGINUSDT" not in names
    assert "ETHUPUSDT" not in names
    assert "JUPUSDT" in names
    assert is_obvious_special_asset("BTC3L")
    assert not is_obvious_special_asset("JUP")


def test_cycle_route_builds_both_directions_with_correct_book_sides() -> None:
    symbols = combine_market_stats(
        parse_exchange_info(_triangle_exchange_info()),
        parse_ticker_stats(_triangle_tickers()),
    )
    cycles = build_triangular_cycles(symbols, root_asset="USDT")

    assert len(cycles) == 2
    by_assets = {cycle.assets: cycle for cycle in cycles}

    forward = by_assets[("USDT", "BTC", "ETH", "USDT")]
    assert forward.symbols == ("BTCUSDT", "ETHBTC", "ETHUSDT")
    assert [leg.side for leg in forward.legs] == [
        LegSide.BUY_BASE,
        LegSide.BUY_BASE,
        LegSide.SELL_BASE,
    ]
    assert [leg.book_side for leg in forward.legs] == [
        BookSide.ASKS,
        BookSide.ASKS,
        BookSide.BIDS,
    ]
    assert forward.route == "USDT -> BTC -> ETH -> USDT"

    reverse = by_assets[("USDT", "ETH", "BTC", "USDT")]
    assert reverse.symbols == ("ETHUSDT", "ETHBTC", "BTCUSDT")
    assert [leg.side for leg in reverse.legs] == [
        LegSide.BUY_BASE,
        LegSide.SELL_BASE,
        LegSide.SELL_BASE,
    ]
    assert [leg.book_side for leg in reverse.legs] == [
        BookSide.ASKS,
        BookSide.BIDS,
        BookSide.BIDS,
    ]


def test_discovery_combines_volume_and_enforces_caps_deterministically() -> None:
    raw = _triangle_exchange_info()
    raw["symbols"].extend(  # type: ignore[union-attr]
        [
            _raw_symbol("BNBUSDT", "BNB", "USDT"),
            _raw_symbol("ETHBNB", "ETH", "BNB"),
        ]
    )
    tickers = [
        *_triangle_tickers(),
        _ticker("BNBUSDT", "700000000", "499", "501"),
        _ticker("ETHBNB", "600000000", "5.99", "6.01"),
    ]
    config = DiscoveryConfig(
        max_symbols=3,
        max_cycles=2,
        important_bridge_assets=("BTC", "ETH", "BNB"),
    )

    first = discover_market(raw, tickers, config)
    second = discover_market(raw, list(reversed(tickers)), config)

    assert first.symbol_names == second.symbol_names
    assert tuple(cycle.cycle_id for cycle in first.cycles) == tuple(
        cycle.cycle_id for cycle in second.cycles
    )
    assert len(first.symbols) == 3
    assert len(first.cycles) == 2
    assert first.candidate_cycle_count == 4
    assert {cycle.direction for cycle in first.cycles} == {"FORWARD", "REVERSE"}
    assert all(isinstance(symbol.quote_volume, Decimal) for symbol in first.symbols)
    assert all(cycle.liquidity_score > 0 for cycle in first.cycles)
    assert all(Decimal("0") <= cycle.feasibility_score <= Decimal("1") for cycle in first.cycles)


def test_cycle_ranking_compares_quote_volume_in_root_denomination() -> None:
    exchange_info = {
        "symbols": [
            _raw_symbol("BTCUSDT", "BTC", "USDT"),
            _raw_symbol("AAABTC", "AAA", "BTC"),
            _raw_symbol("AAAUSDT", "AAA", "USDT"),
            _raw_symbol("DOGEUSDT", "DOGE", "USDT"),
            _raw_symbol("BBBDOGE", "BBB", "DOGE"),
            _raw_symbol("BBBUSDT", "BBB", "USDT"),
        ]
    }
    tickers = [
        _ticker("BTCUSDT", "500", "990", "1010"),
        _ticker("AAABTC", "1", "0.99", "1.01"),
        _ticker("AAAUSDT", "500", "0.99", "1.01"),
        _ticker("DOGEUSDT", "500", "0.099", "0.101"),
        _ticker("BBBDOGE", "100", "0.99", "1.01"),
        _ticker("BBBUSDT", "500", "0.99", "1.01"),
    ]

    result = discover_market(
        exchange_info,
        tickers,
        DiscoveryConfig(
            max_symbols=3,
            max_cycles=2,
            important_bridge_assets=(),
        ),
    )

    # Raw quote units would prefer 100 DOGE to 1 BTC.  Root valuation correctly
    # prefers the BTC triangle: 1 BTC is worth 990 USDT at the direct-market bid,
    # while 100 DOGE is worth only 9.9 USDT.
    assert set(result.symbol_names) == {"AAAUSDT", "AAABTC", "BTCUSDT"}
    assert {cycle.liquidity_score for cycle in result.cycles} == {Decimal("500")}


def test_odd_cycle_cap_keeps_only_complete_direction_pairs() -> None:
    raw = _triangle_exchange_info()
    raw["symbols"].extend(  # type: ignore[union-attr]
        [
            _raw_symbol("BNBUSDT", "BNB", "USDT"),
            _raw_symbol("ETHBNB", "ETH", "BNB"),
        ]
    )
    tickers = [
        *_triangle_tickers(),
        _ticker("BNBUSDT", "700000000", "499", "501"),
        _ticker("ETHBNB", "600000000", "5.99", "6.01"),
    ]

    result = discover_market(
        raw,
        tickers,
        DiscoveryConfig(max_symbols=5, max_cycles=3),
    )

    assert len(result.cycles) == 2
    assert {cycle.direction for cycle in result.cycles} == {"FORWARD", "REVERSE"}
    assert len({frozenset(cycle.assets[1:3]) for cycle in result.cycles}) == 1


def test_missing_or_crossed_ticker_book_is_not_selected() -> None:
    tickers = _triangle_tickers()
    tickers[1] = _ticker("ETHBTC", "800000000", "0.051", "0.050")
    result = discover_market(_triangle_exchange_info(), tickers)

    assert result.symbols == ()
    assert result.cycles == ()
    assert result.eligible_symbol_count == 2


def test_config_accepts_nested_yaml_shape_and_flat_cli_overrides() -> None:
    config = config_from_mapping(
        {
            "discovery": {"root_asset": "usdc", "max_cycles": 25},
            "simulation": {
                "fee_rate": "0.00075",
                "start_sizes": ["10.5", 25],
                "latency_buckets": "100,50,100",
            },
            "max_symbols": 20,
            "output_dir": "research-data",
        }
    )

    assert config.discovery.root_asset == "USDC"
    assert config.max_symbols == 20
    assert config.max_cycles == 25
    assert config.simulation.fee_rate == Decimal("0.00075")
    assert config.simulation.start_sizes == (Decimal("10.5"), Decimal("25"))
    assert config.simulation.latency_buckets_ms == (50, 100)
    assert str(config.output.output_dir) == "research-data"
    assert config.network.rest_base_url == "https://data-api.binance.vision"
    assert config.network.websocket_base_url == "wss://data-stream.binance.vision:443"
    assert config_to_dict(config)["simulation"]["fee_rate"] == "0.00075"


def test_config_rejects_unknown_keys_and_invalid_limits() -> None:
    with pytest.raises(ConfigError, match="unknown configuration key"):
        config_from_mapping({"live_trading": True})
    with pytest.raises(ConfigError, match="at least 3"):
        config_from_mapping({"max_symbols": 2})
    with pytest.raises(ConfigError, match="latency_buckets_ms cannot be empty"):
        config_from_mapping({"simulation": {"latency_buckets_ms": []}})
    with pytest.raises(ConfigError, match=r"extra_slippage_bps must be in \[0, 10000\)"):
        config_from_mapping({"simulation": {"extra_slippage_bps": 10000}})
    with pytest.raises(ConfigError, match="must be between 1 and 1024"):
        config_from_mapping({"order_book": {"max_streams_per_connection": 1025}})
