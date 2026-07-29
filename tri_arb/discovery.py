"""Deterministic normalized Spot symbol and triangular-cycle discovery.

Only public ``exchangeInfo`` and 24-hour ticker payloads are consumed.  This
module deliberately has no HTTP client and no knowledge of account or order
endpoints, which keeps discovery safe to reuse in offline tests.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from fnmatch import fnmatchcase
from itertools import combinations
from typing import Any

from .config import AppConfig, DiscoveryConfig
from .models import (
    ZERO,
    BookSide,
    CycleLeg,
    DiscoveryResult,
    LegSide,
    LotSizeFilter,
    MarketStats,
    NotionalFilter,
    PriceFilter,
    SymbolFilters,
    SymbolInfo,
    TriangularCycle,
)

_LEVERAGED_SUFFIXES = ("DOWN", "BULL", "BEAR", "HALF", "HEDGE", "UP")
_MULTIPLIER_SUFFIX = re.compile(r"^.+(?:2L|2S|3L|3S|5L|5S)$")


class DiscoveryError(ValueError):
    """Raised for a malformed top-level Binance discovery payload."""


def _decimal(value: Any, default: Decimal = ZERO) -> Decimal:
    if value is None or value == "":
        return default
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return result if result.is_finite() else default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _boolean(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    if value in (0, 1):
        return bool(value)
    return default


def _permissions(raw: Mapping[str, Any]) -> frozenset[str]:
    values: set[str] = set()
    direct = raw.get("permissions", ())
    if isinstance(direct, Sequence) and not isinstance(direct, (str, bytes)):
        values.update(str(item).upper() for item in direct if item)
    permission_sets = raw.get("permissionSets", ())
    if isinstance(permission_sets, Sequence) and not isinstance(permission_sets, (str, bytes)):
        for permission_set in permission_sets:
            if isinstance(permission_set, Sequence) and not isinstance(
                permission_set, (str, bytes)
            ):
                values.update(str(item).upper() for item in permission_set if item)
    return frozenset(values)


def parse_symbol_filters(raw_filters: Any) -> SymbolFilters:
    """Parse the execution-relevant filters from one exchangeInfo symbol."""

    price: PriceFilter | None = None
    lot_size: LotSizeFilter | None = None
    market_lot_size: LotSizeFilter | None = None
    min_notional: NotionalFilter | None = None
    full_notional: NotionalFilter | None = None
    filter_types: set[str] = set()

    if not isinstance(raw_filters, Sequence) or isinstance(raw_filters, (str, bytes)):
        return SymbolFilters()

    for item in raw_filters:
        if not isinstance(item, Mapping):
            continue
        filter_type = str(item.get("filterType", "")).upper()
        if not filter_type:
            continue
        filter_types.add(filter_type)
        if filter_type == "PRICE_FILTER":
            price = PriceFilter(
                min_price=_decimal(item.get("minPrice")),
                max_price=_decimal(item.get("maxPrice")),
                tick_size=_decimal(item.get("tickSize")),
            )
        elif filter_type in {"LOT_SIZE", "MARKET_LOT_SIZE"}:
            parsed_lot = LotSizeFilter(
                min_qty=_decimal(item.get("minQty")),
                max_qty=_decimal(item.get("maxQty")),
                step_size=_decimal(item.get("stepSize")),
            )
            if filter_type == "LOT_SIZE":
                lot_size = parsed_lot
            else:
                market_lot_size = parsed_lot
        elif filter_type == "MIN_NOTIONAL":
            min_notional = NotionalFilter(
                min_notional=_decimal(item.get("minNotional")),
                apply_min_to_market=_boolean(item.get("applyToMarket")),
                average_price_minutes=_integer(item.get("avgPriceMins")),
            )
        elif filter_type == "NOTIONAL":
            full_notional = NotionalFilter(
                min_notional=_decimal(item.get("minNotional")),
                max_notional=_decimal(item.get("maxNotional")),
                apply_min_to_market=_boolean(item.get("applyMinToMarket")),
                apply_max_to_market=_boolean(item.get("applyMaxToMarket")),
                average_price_minutes=_integer(item.get("avgPriceMins")),
            )

    return SymbolFilters(
        price=price,
        lot_size=lot_size,
        market_lot_size=market_lot_size,
        # NOTIONAL supersedes the older MIN_NOTIONAL rule if both are present.
        notional=full_notional or min_notional,
        filter_types=frozenset(filter_types),
    )


def parse_exchange_info(exchange_info: Mapping[str, Any]) -> tuple[SymbolInfo, ...]:
    """Parse all valid symbol records, retaining status and permission metadata.

    Eligibility is intentionally a separate step: callers can inspect suspended
    records in diagnostics while :func:`eligible_symbols` guarantees that only
    tradable Spot markets enter the live universe.
    """

    if not isinstance(exchange_info, Mapping):
        raise DiscoveryError("exchangeInfo must be a mapping")
    raw_symbols = exchange_info.get("symbols")
    if not isinstance(raw_symbols, Sequence) or isinstance(raw_symbols, (str, bytes)):
        raise DiscoveryError("exchangeInfo.symbols must be a list")

    parsed: dict[str, SymbolInfo] = {}
    for raw in raw_symbols:
        if not isinstance(raw, Mapping):
            continue
        symbol = str(raw.get("symbol", "")).strip().upper()
        base_asset = str(raw.get("baseAsset", "")).strip().upper()
        quote_asset = str(raw.get("quoteAsset", "")).strip().upper()
        if not symbol or not base_asset or not quote_asset or base_asset == quote_asset:
            continue
        permissions = _permissions(raw)
        if "isSpotTradingAllowed" in raw:
            is_spot = _boolean(raw.get("isSpotTradingAllowed"))
        else:
            is_spot = "SPOT" in permissions
        order_types_raw = raw.get("orderTypes", ())
        order_types = (
            tuple(str(item).upper() for item in order_types_raw if item)
            if isinstance(order_types_raw, Sequence)
            and not isinstance(order_types_raw, (str, bytes))
            else ()
        )
        parsed[symbol] = SymbolInfo(
            symbol=symbol,
            base_asset=base_asset,
            quote_asset=quote_asset,
            status=str(raw.get("status", "")).upper(),
            is_spot_trading_allowed=is_spot,
            permissions=permissions,
            order_types=order_types,
            filters=parse_symbol_filters(raw.get("filters", ())),
            quote_order_qty_market_allowed=_boolean(raw.get("quoteOrderQtyMarketAllowed")),
            base_asset_precision=(
                _integer(raw.get("baseAssetPrecision"))
                if raw.get("baseAssetPrecision") is not None
                else None
            ),
            quote_asset_precision=(
                _integer(raw.get("quoteAssetPrecision"))
                if raw.get("quoteAssetPrecision") is not None
                else None
            ),
        )
    return tuple(parsed[name] for name in sorted(parsed))


def parse_ticker_stats(payload: Any) -> dict[str, MarketStats]:
    """Parse either the all-symbol or single-symbol 24-hour ticker response."""

    if isinstance(payload, Mapping):
        raw_tickers: Sequence[Any] = (payload,)
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        raw_tickers = payload
    else:
        raise DiscoveryError("24-hour ticker payload must be a mapping or list")

    parsed: dict[str, MarketStats] = {}
    for raw in raw_tickers:
        if not isinstance(raw, Mapping):
            continue
        symbol = str(raw.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        ticker = MarketStats(
            symbol=symbol,
            quote_volume=_decimal(raw.get("quoteVolume")),
            base_volume=_decimal(raw.get("volume")),
            bid_price=_decimal(raw.get("bidPrice")),
            bid_qty=_decimal(raw.get("bidQty")),
            ask_price=_decimal(raw.get("askPrice")),
            ask_qty=_decimal(raw.get("askQty")),
            last_price=_decimal(raw.get("lastPrice")),
            weighted_average_price=_decimal(raw.get("weightedAvgPrice")),
            trade_count=_integer(raw.get("count")),
        )
        previous = parsed.get(symbol)
        if previous is None or ticker.quote_volume > previous.quote_volume:
            parsed[symbol] = ticker
    return parsed


def combine_market_stats(
    symbols: Iterable[SymbolInfo],
    ticker_stats: Mapping[str, MarketStats],
) -> tuple[SymbolInfo, ...]:
    """Attach exact 24-hour volume and top-of-book metadata to symbols."""

    combined: list[SymbolInfo] = []
    for symbol in symbols:
        stats = ticker_stats.get(symbol.symbol)
        if stats is None:
            combined.append(symbol)
            continue
        combined.append(
            replace(
                symbol,
                quote_volume=stats.quote_volume,
                base_volume=stats.base_volume,
                bid_price=stats.bid_price,
                bid_qty=stats.bid_qty,
                ask_price=stats.ask_price,
                ask_qty=stats.ask_qty,
                last_price=stats.last_price,
                weighted_average_price=stats.weighted_average_price,
                trade_count=stats.trade_count,
            )
        )
    return tuple(combined)


def is_obvious_special_asset(asset: str) -> bool:
    """Conservatively identify leveraged-token and non-spot wrapper names.

    Requiring a stem of at least two characters avoids classifying ordinary assets
    such as JUP as the historical ``UP`` leveraged-token suffix.
    """

    normalized = asset.strip().upper()
    if not normalized:
        return True
    if normalized.startswith("LD") and len(normalized) > 4:
        return True
    if _MULTIPLIER_SUFFIX.fullmatch(normalized):
        return True
    for suffix in _LEVERAGED_SUFFIXES:
        if normalized.endswith(suffix) and len(normalized) - len(suffix) >= 2:
            return True
    return False


def eligible_symbols(
    symbols: Iterable[SymbolInfo],
    config: DiscoveryConfig | None = None,
) -> tuple[SymbolInfo, ...]:
    """Filter to currently trading, ordinary Spot symbols with usable data."""

    config = config or DiscoveryConfig()
    symbols = tuple(symbols)
    excluded = set(config.exclude_assets)
    needs_usdt_valuation = bool(
        config.min_quote_volume_usdt > ZERO or config.min_top_of_book_notional > ZERO
    )
    market_by_pair: dict[frozenset[str], SymbolInfo] = {}
    if needs_usdt_valuation:
        grouped_markets: dict[frozenset[str], list[SymbolInfo]] = defaultdict(list)
        for candidate in symbols:
            if candidate.is_tradable_spot and candidate.has_usable_book:
                grouped_markets[_pair_key(candidate.base_asset, candidate.quote_asset)].append(
                    candidate
                )
        market_by_pair = {pair: _best_market(markets) for pair, markets in grouped_markets.items()}

    eligible: list[SymbolInfo] = []
    for symbol in symbols:
        if not symbol.is_tradable_spot:
            continue
        if symbol.base_asset in excluded or symbol.quote_asset in excluded:
            continue
        if any(fnmatchcase(symbol.symbol, pattern) for pattern in config.exclude_symbol_patterns):
            continue
        if is_obvious_special_asset(symbol.base_asset) or is_obvious_special_asset(
            symbol.quote_asset
        ):
            continue
        if symbol.quote_volume < config.min_quote_volume:
            continue
        if config.require_usable_ticker_book and not symbol.has_usable_book:
            continue
        spread = symbol.spread_bps
        if config.max_spread_bps is not None and (spread is None or spread > config.max_spread_bps):
            continue
        if (
            config.min_quote_volume_usdt > ZERO
            and _root_denominated_quote_volume(
                symbol,
                "USDT",
                market_by_pair,
            )
            < config.min_quote_volume_usdt
        ):
            continue
        if (
            config.min_top_of_book_notional > ZERO
            and _root_denominated_top_notional(
                symbol,
                "USDT",
                market_by_pair,
            )
            < config.min_top_of_book_notional
        ):
            continue
        eligible.append(symbol)
    return tuple(sorted(eligible, key=lambda item: item.symbol))


def rank_symbols(
    symbols: Iterable[SymbolInfo],
    *,
    root_asset: str = "USDT",
    important_bridge_assets: Iterable[str] = (),
) -> tuple[SymbolInfo, ...]:
    """Rank useful markets deterministically, with liquidity as the main signal."""

    root = root_asset.upper()
    bridges = {asset.upper() for asset in important_bridge_assets}

    def key(symbol: SymbolInfo) -> tuple[Any, ...]:
        assets = {symbol.base_asset, symbol.quote_asset}
        root_connected = root in assets
        bridge_connected = bool(assets & bridges)
        spread = symbol.spread_bps
        return (
            -int(root_connected),
            -int(bridge_connected),
            -symbol.quote_volume,
            spread if spread is not None else Decimal("Infinity"),
            symbol.symbol,
        )

    return tuple(sorted(symbols, key=key))


def select_symbols(
    symbols: Iterable[SymbolInfo],
    *,
    max_symbols: int,
    root_asset: str = "USDT",
    important_bridge_assets: Iterable[str] = (),
) -> tuple[SymbolInfo, ...]:
    if max_symbols < 0:
        raise ValueError("max_symbols cannot be negative")
    return rank_symbols(
        symbols,
        root_asset=root_asset,
        important_bridge_assets=important_bridge_assets,
    )[:max_symbols]


def build_cycle_leg(
    from_asset: str,
    to_asset: str,
    symbol: SymbolInfo,
) -> CycleLeg:
    """Create one conversion with the correct Binance bid/ask direction."""

    source = from_asset.upper()
    target = to_asset.upper()
    if source == symbol.base_asset and target == symbol.quote_asset:
        side = LegSide.SELL_BASE
        book_side = BookSide.BIDS
    elif source == symbol.quote_asset and target == symbol.base_asset:
        side = LegSide.BUY_BASE
        book_side = BookSide.ASKS
    else:
        raise ValueError(f"symbol {symbol.symbol} does not convert {from_asset} to {to_asset}")
    return CycleLeg(
        from_asset=source,
        to_asset=target,
        symbol=symbol.symbol,
        base_asset=symbol.base_asset,
        quote_asset=symbol.quote_asset,
        side=side,
        book_side=book_side,
    )


def _pair_key(first: str, second: str) -> frozenset[str]:
    return frozenset((first, second))


def _best_market(markets: Iterable[SymbolInfo]) -> SymbolInfo:
    def key(symbol: SymbolInfo) -> tuple[Any, ...]:
        spread = symbol.spread_bps
        return (
            -int(symbol.has_usable_book),
            -symbol.quote_volume,
            spread if spread is not None else Decimal("Infinity"),
            symbol.symbol,
        )

    return min(markets, key=key)


def _cycle_scores(
    markets: Sequence[SymbolInfo],
    route_assets: Sequence[str],
    important_bridge_assets: set[str],
    market_by_pair: Mapping[frozenset[str], SymbolInfo],
) -> tuple[Decimal, Decimal, Decimal]:
    root_asset = route_assets[0]
    root_quote_volumes = (
        _root_denominated_quote_volume(market, root_asset, market_by_pair) for market in markets
    )
    liquidity = min(root_quote_volumes, default=ZERO)
    spreads = [market.spread_bps for market in markets]
    if any(spread is None for spread in spreads):
        spread_score = ZERO
    else:
        average_spread = sum((spread for spread in spreads if spread is not None), ZERO) / Decimal(
            len(spreads)
        )
        spread_score = Decimal("1") / (Decimal("1") + max(average_spread, ZERO))

    # Saturation keeps the score comparable across days while ``liquidity`` itself
    # remains the exact, inspectable minimum quote volume.
    liquidity_scale = Decimal("1000000")
    liquidity_component = liquidity / (liquidity + liquidity_scale) if liquidity > ZERO else ZERO
    feasibility = liquidity_component * Decimal("0.70") + spread_score * Decimal("0.30")
    if set(route_assets[1:-1]) & important_bridge_assets:
        feasibility += Decimal("0.03")
    feasibility = min(Decimal("1"), max(ZERO, feasibility))
    return liquidity, spread_score, feasibility


def _root_conversion_rate(
    asset: str,
    root_asset: str,
    market: SymbolInfo,
) -> Decimal:
    """Return a conservative direct-market conversion rate into ``root_asset``.

    A bid values base-asset proceeds when the asset is sold for the root.  The
    reciprocal ask values quote-asset proceeds when the root must be bought.
    Weighted-average and last prices are deterministic fallbacks for incomplete
    ticker books; choosing the least favorable positive fallback avoids
    overstating liquidity.
    """

    if asset == root_asset:
        return Decimal("1")

    fallback_prices = tuple(
        price for price in (market.weighted_average_price, market.last_price) if price > ZERO
    )
    if market.base_asset == asset and market.quote_asset == root_asset:
        if market.bid_price > ZERO:
            return market.bid_price
        return min(fallback_prices, default=ZERO)
    if market.quote_asset == asset and market.base_asset == root_asset:
        if market.ask_price > ZERO:
            return Decimal("1") / market.ask_price
        fallback = max(fallback_prices, default=ZERO)
        return Decimal("1") / fallback if fallback > ZERO else ZERO
    return ZERO


def _root_denominated_quote_volume(
    market: SymbolInfo,
    root_asset: str,
    market_by_pair: Mapping[frozenset[str], SymbolInfo],
) -> Decimal:
    """Value a market's 24-hour quote volume in the cycle's root asset."""

    if market.quote_volume <= ZERO:
        return ZERO
    if market.quote_asset == root_asset:
        return market.quote_volume
    conversion_market = market_by_pair.get(_pair_key(market.quote_asset, root_asset))
    if conversion_market is None:
        return ZERO
    return market.quote_volume * _root_conversion_rate(
        market.quote_asset,
        root_asset,
        conversion_market,
    )


def _root_denominated_top_notional(
    market: SymbolInfo,
    root_asset: str,
    market_by_pair: Mapping[frozenset[str], SymbolInfo],
) -> Decimal:
    """Conservatively value the smaller displayed top level in root units."""

    bid_notional = market.bid_price * market.bid_qty
    ask_notional = market.ask_price * market.ask_qty
    quote_notional = min(bid_notional, ask_notional)
    if quote_notional <= ZERO:
        return ZERO
    if market.quote_asset == root_asset:
        return quote_notional
    conversion_market = market_by_pair.get(_pair_key(market.quote_asset, root_asset))
    if conversion_market is None:
        return ZERO
    return quote_notional * _root_conversion_rate(
        market.quote_asset,
        root_asset,
        conversion_market,
    )


def _make_cycle(
    root_asset: str,
    first_asset: str,
    second_asset: str,
    market_by_pair: Mapping[frozenset[str], SymbolInfo],
    important_bridge_assets: set[str],
    direction: str,
) -> TriangularCycle:
    assets = (root_asset, first_asset, second_asset, root_asset)
    markets = tuple(
        market_by_pair[_pair_key(assets[index], assets[index + 1])] for index in range(3)
    )
    legs = tuple(
        build_cycle_leg(assets[index], assets[index + 1], markets[index]) for index in range(3)
    )
    liquidity, spread, feasibility = _cycle_scores(
        markets, assets, important_bridge_assets, market_by_pair
    )
    return TriangularCycle(
        cycle_id=":".join(assets),
        root_asset=root_asset,
        assets=assets,
        symbols=tuple(leg.symbol for leg in legs),
        legs=legs,  # type: ignore[arg-type]
        liquidity_score=liquidity,
        spread_score=spread,
        feasibility_score=feasibility,
        direction=direction,
    )


def rank_cycles(
    cycles: Iterable[TriangularCycle],
    *,
    max_cycles: int | None = None,
) -> tuple[TriangularCycle, ...]:
    """Sort by feasibility, liquidity, tight spread, then stable route id."""

    if max_cycles is not None and max_cycles < 0:
        raise ValueError("max_cycles cannot be negative")
    ranked = sorted(
        cycles,
        key=lambda cycle: (
            -cycle.feasibility_score,
            -cycle.liquidity_score,
            -cycle.spread_score,
            cycle.cycle_id,
        ),
    )
    if max_cycles is not None:
        ranked = ranked[:max_cycles]
    return tuple(replace(cycle, rank=index) for index, cycle in enumerate(ranked, start=1))


def build_triangular_cycles(
    symbols: Iterable[SymbolInfo],
    *,
    root_asset: str = "USDT",
    important_bridge_assets: Iterable[str] = (),
    max_cycles: int | None = None,
) -> tuple[TriangularCycle, ...]:
    """Construct both directions of every three-market root-anchored triangle."""

    root = root_asset.strip().upper()
    if not root:
        raise ValueError("root_asset cannot be empty")
    bridges = {asset.strip().upper() for asset in important_bridge_assets}

    grouped_markets: dict[frozenset[str], list[SymbolInfo]] = defaultdict(list)
    for symbol in symbols:
        if symbol.base_asset == symbol.quote_asset:
            continue
        grouped_markets[_pair_key(symbol.base_asset, symbol.quote_asset)].append(symbol)
    market_by_pair = {pair: _best_market(markets) for pair, markets in grouped_markets.items()}

    root_neighbors: set[str] = set()
    for pair in market_by_pair:
        if root in pair and len(pair) == 2:
            root_neighbors.update(pair - {root})

    cycles: list[TriangularCycle] = []
    for first, second in combinations(sorted(root_neighbors), 2):
        if _pair_key(first, second) not in market_by_pair:
            continue
        cycles.append(_make_cycle(root, first, second, market_by_pair, bridges, "FORWARD"))
        cycles.append(_make_cycle(root, second, first, market_by_pair, bridges, "REVERSE"))
    return rank_cycles(cycles, max_cycles=max_cycles)


construct_triangular_cycles = build_triangular_cycles
discover_cycles = build_triangular_cycles


def _bounded_cycle_universe(
    cycles: Sequence[TriangularCycle],
    *,
    max_symbols: int,
    max_cycles: int,
) -> tuple[TriangularCycle, ...]:
    """Greedily retain whole direction-pairs while respecting both hard caps."""

    ranked = rank_cycles(cycles)
    groups: dict[frozenset[str], list[TriangularCycle]] = {}
    for cycle in ranked:
        pair = frozenset(cycle.assets[1:3])
        groups.setdefault(pair, []).append(cycle)

    selected: list[TriangularCycle] = []
    selected_symbols: set[str] = set()
    for group in groups.values():
        group = sorted(group, key=lambda cycle: (cycle.direction != "FORWARD", cycle.cycle_id))
        if len(group) != 2 or {cycle.direction for cycle in group} != {
            "FORWARD",
            "REVERSE",
        }:
            continue
        required = set().union(*(cycle.required_symbols for cycle in group))
        if len(selected_symbols | required) > max_symbols:
            continue
        if len(selected) + len(group) > max_cycles:
            continue
        selected.extend(group)
        for cycle in group:
            selected_symbols.update(cycle.required_symbols)
    return rank_cycles(selected)


def _discovery_config(config: DiscoveryConfig | AppConfig | None) -> DiscoveryConfig:
    if config is None:
        return DiscoveryConfig()
    if isinstance(config, AppConfig):
        return config.discovery
    return config


def discover_market(
    exchange_info: Mapping[str, Any],
    ticker_24h: Any,
    config: DiscoveryConfig | AppConfig | None = None,
) -> DiscoveryResult:
    """Parse, filter, rank, and hard-bound a monitorable triangular universe."""

    return discover_normalized_market(
        parse_exchange_info(exchange_info),
        parse_ticker_stats(ticker_24h),
        config,
    )


def discover_normalized_market(
    symbols: Iterable[SymbolInfo],
    ticker_stats: Mapping[str, MarketStats],
    config: DiscoveryConfig | AppConfig | None = None,
) -> DiscoveryResult:
    """Filter and rank exchange-adapter models without reparsing raw payloads."""

    discovery_config = _discovery_config(config)
    combined = combine_market_stats(symbols, ticker_stats)
    eligible = eligible_symbols(combined, discovery_config)

    candidates = build_triangular_cycles(
        eligible,
        root_asset=discovery_config.root_asset,
        important_bridge_assets=discovery_config.important_bridge_assets,
    )
    cycles = _bounded_cycle_universe(
        candidates,
        max_symbols=discovery_config.max_symbols,
        max_cycles=discovery_config.max_cycles,
    )
    required_symbols = (
        set().union(*(cycle.required_symbols for cycle in cycles)) if cycles else set()
    )
    selected_symbols = tuple(
        symbol
        for symbol in rank_symbols(
            eligible,
            root_asset=discovery_config.root_asset,
            important_bridge_assets=discovery_config.important_bridge_assets,
        )
        if symbol.symbol in required_symbols
    )
    return DiscoveryResult(
        symbols=selected_symbols,
        cycles=cycles,
        eligible_symbol_count=len(eligible),
        candidate_cycle_count=len(candidates),
    )


discover = discover_market
