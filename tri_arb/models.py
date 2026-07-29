"""Core immutable models shared by discovery and the paper simulator.

The exchange sends every numeric field as a JSON string.  Keeping those values as
``Decimal`` here prevents an accidental conversion to binary floating point before
the execution simulator sees them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

ZERO = Decimal("0")
TEN_THOUSAND = Decimal("10000")


class LegSide(StrEnum):
    """How a conversion is executed in Binance base/quote notation."""

    SELL_BASE = "SELL_BASE"
    BUY_BASE = "BUY_BASE"


class BookSide(StrEnum):
    """The side of the order book consumed by a conversion."""

    BIDS = "bids"
    ASKS = "asks"


# Compatibility-friendly names used by some callers.
ConversionSide = LegSide
TradeDirection = LegSide


@dataclass(frozen=True, slots=True)
class PriceFilter:
    min_price: Decimal = ZERO
    max_price: Decimal = ZERO
    tick_size: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class LotSizeFilter:
    min_qty: Decimal = ZERO
    max_qty: Decimal = ZERO
    step_size: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class NotionalFilter:
    min_notional: Decimal = ZERO
    max_notional: Decimal = ZERO
    apply_min_to_market: bool = False
    apply_max_to_market: bool = False
    average_price_minutes: int = 0


@dataclass(frozen=True, slots=True)
class SymbolFilters:
    """The Binance filters relevant to paper execution feasibility.

    ``filter_types`` preserves the complete set returned by exchangeInfo, even
    though only price, quantity, and notional rules need typed numeric fields.
    """

    price: PriceFilter | None = None
    lot_size: LotSizeFilter | None = None
    market_lot_size: LotSizeFilter | None = None
    notional: NotionalFilter | None = None
    filter_types: frozenset[str] = field(default_factory=frozenset)

    @property
    def tick_size(self) -> Decimal:
        return self.price.tick_size if self.price else ZERO

    @property
    def min_price(self) -> Decimal:
        return self.price.min_price if self.price else ZERO

    @property
    def max_price(self) -> Decimal:
        return self.price.max_price if self.price else ZERO

    @property
    def min_qty(self) -> Decimal:
        return self.lot_size.min_qty if self.lot_size else ZERO

    @property
    def max_qty(self) -> Decimal:
        return self.lot_size.max_qty if self.lot_size else ZERO

    @property
    def step_size(self) -> Decimal:
        return self.lot_size.step_size if self.lot_size else ZERO

    @property
    def min_notional(self) -> Decimal:
        return self.notional.min_notional if self.notional else ZERO

    @property
    def max_notional(self) -> Decimal:
        return self.notional.max_notional if self.notional else ZERO


@dataclass(frozen=True, slots=True)
class MarketStats:
    """Relevant fields from Binance's 24-hour ticker statistics."""

    symbol: str
    quote_volume: Decimal = ZERO
    base_volume: Decimal = ZERO
    bid_price: Decimal = ZERO
    bid_qty: Decimal = ZERO
    ask_price: Decimal = ZERO
    ask_qty: Decimal = ZERO
    last_price: Decimal = ZERO
    weighted_average_price: Decimal = ZERO
    trade_count: int = 0

    @property
    def has_usable_book(self) -> bool:
        return self.bid_price > ZERO and self.ask_price >= self.bid_price

    @property
    def mid_price(self) -> Decimal:
        if not self.has_usable_book:
            return ZERO
        return (self.bid_price + self.ask_price) / Decimal("2")

    @property
    def spread_bps(self) -> Decimal | None:
        mid = self.mid_price
        if mid <= ZERO:
            return None
        return (self.ask_price - self.bid_price) / mid * TEN_THOUSAND


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    """A spot symbol plus the public market metadata used during discovery."""

    symbol: str
    base_asset: str
    quote_asset: str
    status: str
    is_spot_trading_allowed: bool
    permissions: frozenset[str] = field(default_factory=frozenset)
    order_types: tuple[str, ...] = ()
    filters: SymbolFilters = field(default_factory=SymbolFilters)
    quote_order_qty_market_allowed: bool = False
    base_asset_precision: int | None = None
    quote_asset_precision: int | None = None
    quote_volume: Decimal = ZERO
    base_volume: Decimal = ZERO
    bid_price: Decimal = ZERO
    bid_qty: Decimal = ZERO
    ask_price: Decimal = ZERO
    ask_qty: Decimal = ZERO
    last_price: Decimal = ZERO
    weighted_average_price: Decimal = ZERO
    trade_count: int = 0

    @property
    def trading_rules(self) -> SymbolFilters:
        return self.filters

    @property
    def rules(self) -> SymbolFilters:
        return self.filters

    @property
    def tick_size(self) -> Decimal:
        return self.filters.tick_size

    @property
    def min_qty(self) -> Decimal:
        return self.filters.min_qty

    @property
    def max_qty(self) -> Decimal:
        return self.filters.max_qty

    @property
    def step_size(self) -> Decimal:
        return self.filters.step_size

    @property
    def min_notional(self) -> Decimal:
        return self.filters.min_notional

    @property
    def max_notional(self) -> Decimal:
        return self.filters.max_notional

    @property
    def has_usable_book(self) -> bool:
        return self.bid_price > ZERO and self.ask_price >= self.bid_price

    @property
    def spread_bps(self) -> Decimal | None:
        if not self.has_usable_book:
            return None
        mid = (self.bid_price + self.ask_price) / Decimal("2")
        return (self.ask_price - self.bid_price) / mid * TEN_THOUSAND

    @property
    def is_tradable_spot(self) -> bool:
        return (
            self.status == "TRADING"
            and self.is_spot_trading_allowed
            and bool(self.base_asset)
            and bool(self.quote_asset)
            and self.base_asset != self.quote_asset
        )


@dataclass(frozen=True, slots=True)
class CycleLeg:
    """One directed asset conversion and the book side it consumes."""

    from_asset: str
    to_asset: str
    symbol: str
    base_asset: str
    quote_asset: str
    side: LegSide
    book_side: BookSide

    def __post_init__(self) -> None:
        if self.side is LegSide.SELL_BASE:
            expected_assets = (self.base_asset, self.quote_asset)
            expected_book_side = BookSide.BIDS
        else:
            expected_assets = (self.quote_asset, self.base_asset)
            expected_book_side = BookSide.ASKS
        if (self.from_asset, self.to_asset) != expected_assets:
            raise ValueError(
                f"{self.side.value} on {self.symbol} cannot convert "
                f"{self.from_asset} to {self.to_asset}"
            )
        if self.book_side is not expected_book_side:
            raise ValueError(
                f"{self.side.value} on {self.symbol} must consume {expected_book_side.value}"
            )

    @property
    def direction(self) -> LegSide:
        return self.side

    @property
    def input_asset(self) -> str:
        return self.from_asset

    @property
    def output_asset(self) -> str:
        return self.to_asset

    @property
    def uses_bid(self) -> bool:
        return self.book_side is BookSide.BIDS

    @property
    def uses_ask(self) -> bool:
        return self.book_side is BookSide.ASKS

    def to_record(self) -> dict[str, str]:
        return {
            "from_asset": self.from_asset,
            "to_asset": self.to_asset,
            "symbol": self.symbol,
            "base_asset": self.base_asset,
            "quote_asset": self.quote_asset,
            "side": self.side.value,
            "book_side": self.book_side.value,
        }


@dataclass(frozen=True, slots=True)
class TriangularCycle:
    """A closed, root-anchored route containing exactly three conversions."""

    cycle_id: str
    root_asset: str
    assets: tuple[str, str, str, str]
    symbols: tuple[str, str, str]
    legs: tuple[CycleLeg, CycleLeg, CycleLeg]
    liquidity_score: Decimal
    spread_score: Decimal
    feasibility_score: Decimal
    direction: str = "FORWARD"
    rank: int = 0

    def __post_init__(self) -> None:
        if self.assets[0] != self.root_asset or self.assets[-1] != self.root_asset:
            raise ValueError("a triangular cycle must start and end at root_asset")
        if len(set(self.assets[:-1])) != 3:
            raise ValueError("a triangular cycle must contain three distinct assets")
        for index, leg in enumerate(self.legs):
            if (leg.from_asset, leg.to_asset) != (
                self.assets[index],
                self.assets[index + 1],
            ):
                raise ValueError("cycle leg order does not match the asset route")
            if leg.symbol != self.symbols[index]:
                raise ValueError("cycle symbols do not match the ordered legs")

    @property
    def id(self) -> str:
        return self.cycle_id

    @property
    def route(self) -> str:
        return " -> ".join(self.assets)

    @property
    def expected_route(self) -> str:
        return self.route

    @property
    def involved_order_books(self) -> tuple[str, str, str]:
        return self.symbols

    @property
    def required_symbols(self) -> frozenset[str]:
        return frozenset(self.symbols)

    def to_record(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "root_asset": self.root_asset,
            "assets": list(self.assets),
            "symbols": list(self.symbols),
            "direction": self.direction,
            "route": self.route,
            "legs": [leg.to_record() for leg in self.legs],
            "liquidity_score": str(self.liquidity_score),
            "spread_score": str(self.spread_score),
            "feasibility_score": str(self.feasibility_score),
            "rank": self.rank,
        }


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """The bounded market universe handed to the book manager."""

    symbols: tuple[SymbolInfo, ...]
    cycles: tuple[TriangularCycle, ...]
    eligible_symbol_count: int
    candidate_cycle_count: int

    @property
    def symbol_names(self) -> tuple[str, ...]:
        return tuple(symbol.symbol for symbol in self.symbols)

    @property
    def symbols_by_name(self) -> Mapping[str, SymbolInfo]:
        return {symbol.symbol: symbol for symbol in self.symbols}
