"""Decimal local order books synchronized with Binance Spot diff events.

This module deliberately contains no networking.  :class:`OrderBook` implements
the sequence rules documented for Binance Spot's diff-depth stream and can be
tested by replaying ordinary dictionaries.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

Level = tuple[Decimal, Decimal]


def epoch_ms() -> int:
    """Return wall-clock milliseconds for persisted/local timestamps."""

    return time.time_ns() // 1_000_000


class UpdateStatus(StrEnum):
    """Outcome of a diff-depth update."""

    APPLIED = "applied"
    STALE = "stale"
    GAP = "gap"
    CROSSED = "crossed"


@dataclass(frozen=True, slots=True)
class DepthUpdate:
    """Normalized Binance Spot diff-depth message."""

    first_update_id: int
    final_update_id: int
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    event_time_ms: int | None = None
    previous_final_update_id: int | None = None
    symbol: str | None = None

    @classmethod
    def from_message(cls, message: Mapping[str, Any]) -> DepthUpdate:
        """Parse a raw stream event, including an optional combined wrapper."""

        payload: Any = message.get("data", message)
        if not isinstance(payload, Mapping):
            raise ValueError("depth update payload must be a mapping")
        try:
            first_update_id = int(payload["U"])
            final_update_id = int(payload["u"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("depth update requires integer U and u fields") from exc
        if first_update_id < 0 or final_update_id < first_update_id:
            raise ValueError("invalid depth update id range")

        event_time = _optional_int(payload.get("E"), "E")
        previous_id = _optional_int(payload.get("pu"), "pu")
        symbol_value = payload.get("s")
        symbol = str(symbol_value).upper() if symbol_value is not None else None
        return cls(
            first_update_id=first_update_id,
            final_update_id=final_update_id,
            bids=_parse_levels(payload.get("b", ()), "bids"),
            asks=_parse_levels(payload.get("a", ()), "asks"),
            event_time_ms=event_time,
            previous_final_update_id=previous_id,
            symbol=symbol,
        )


@dataclass(frozen=True, slots=True)
class BookUpdateResult:
    """Detailed result returned by :meth:`OrderBook.apply_update`."""

    status: UpdateStatus
    previous_update_id: int | None
    final_update_id: int
    applied_levels: int = 0
    reason: str | None = None

    @property
    def applied(self) -> bool:
        return self.status in (UpdateStatus.APPLIED, UpdateStatus.CROSSED)


@dataclass(slots=True)
class BookMetrics:
    """Cumulative counters retained across resets and resynchronizations."""

    snapshots: int = 0
    updates_applied: int = 0
    levels_updated: int = 0
    stale_updates: int = 0
    sequence_gaps: int = 0
    crossed_events: int = 0
    resyncs: int = 0
    disconnects: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BookHealth:
    """Point-in-time health information suitable for logs and reports."""

    symbol: str
    healthy: bool
    synchronized: bool
    connected: bool
    stale: bool
    crossed: bool
    has_bids: bool
    has_asks: bool
    last_update_id: int | None
    exchange_event_time_ms: int | None
    local_receive_time_ms: int | None
    book_update_time_ms: int | None
    age_ms: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OrderBook:
    """A bounded, price-sorted Binance Spot local order book.

    Prices and quantities are stored as :class:`~decimal.Decimal`.  The first
    update after a snapshot must span ``lastUpdateId + 1``.  Later Spot updates
    may overlap an already-applied batch; they are valid unless they begin after
    ``local_id + 1``.  Some Binance-compatible feeds include ``pu`` and, when
    present, it must equal the prior final update id.
    """

    def __init__(self, symbol: str, max_depth: int = 1000) -> None:
        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            raise ValueError("symbol must not be empty")
        if max_depth <= 0:
            raise ValueError("max_depth must be positive")

        self.symbol = normalized_symbol
        self.max_depth = int(max_depth)
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self.last_update_id: int | None = None
        self.exchange_event_time_ms: int | None = None
        self.local_receive_time_ms: int | None = None
        self.book_update_time_ms: int | None = None
        self.synchronized = False
        self.connected = True
        self.crossed = False
        self.metrics = BookMetrics()
        self._has_snapshot = False

    @property
    def has_snapshot(self) -> bool:
        return self._has_snapshot

    @property
    def healthy(self) -> bool:
        """Structural health, excluding caller-specific staleness limits."""

        return (
            self.connected
            and self.synchronized
            and not self.crossed
            and bool(self._bids)
            and bool(self._asks)
        )

    @property
    def bids(self) -> tuple[Level, ...]:
        return self.bid_levels()

    @property
    def asks(self) -> tuple[Level, ...]:
        return self.ask_levels()

    @property
    def best_bid(self) -> Level | None:
        levels = self.bid_levels(1)
        return levels[0] if levels else None

    @property
    def best_ask(self) -> Level | None:
        levels = self.ask_levels(1)
        return levels[0] if levels else None

    def top_of_book(self) -> tuple[Level | None, Level | None]:
        """Return ``(best_bid, best_ask)``."""

        return self.best_bid, self.best_ask

    def bid_levels(self, limit: int | None = None) -> tuple[Level, ...]:
        levels = tuple(sorted(self._bids.items(), reverse=True))
        return _limit_levels(levels, limit)

    def ask_levels(self, limit: int | None = None) -> tuple[Level, ...]:
        levels = tuple(sorted(self._asks.items()))
        return _limit_levels(levels, limit)

    def depth_levels(self, side: str, limit: int | None = None) -> tuple[Level, ...]:
        normalized_side = side.strip().lower()
        if normalized_side in {"bid", "bids", "buy"}:
            return self.bid_levels(limit)
        if normalized_side in {"ask", "asks", "sell"}:
            return self.ask_levels(limit)
        raise ValueError("side must be bids or asks")

    def reset(self, *, connected: bool | None = None) -> None:
        """Discard levels/sequence state while retaining cumulative metrics."""

        self._bids.clear()
        self._asks.clear()
        self.last_update_id = None
        self.exchange_event_time_ms = None
        self.book_update_time_ms = None
        self.synchronized = False
        self.crossed = False
        self._has_snapshot = False
        if connected is not None:
            self.connected = connected

    def mark_resync(self) -> None:
        self.metrics.resyncs += 1
        self.reset()

    def mark_disconnected(self) -> None:
        if self.connected:
            self.metrics.disconnects += 1
        self.connected = False
        self.synchronized = False

    def load_snapshot(
        self,
        snapshot: Mapping[str, Any],
        *,
        local_receive_time_ms: int | None = None,
    ) -> None:
        """Replace levels with a REST snapshot and await its bridge event."""

        try:
            update_id = int(snapshot["lastUpdateId"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("snapshot requires integer lastUpdateId") from exc
        if update_id < 0:
            raise ValueError("snapshot lastUpdateId must be non-negative")

        bids = _parse_levels(snapshot.get("bids", ()), "bids")
        asks = _parse_levels(snapshot.get("asks", ()), "asks")
        received_ms = epoch_ms() if local_receive_time_ms is None else int(local_receive_time_ms)

        self._bids = {price: quantity for price, quantity in bids if quantity != 0}
        self._asks = {price: quantity for price, quantity in asks if quantity != 0}
        self._trim()
        self.last_update_id = update_id
        self.local_receive_time_ms = received_ms
        self.book_update_time_ms = received_ms
        self.exchange_event_time_ms = None
        self.synchronized = False
        self.connected = True
        self._has_snapshot = True
        self.crossed = self._is_crossed()
        if self.crossed:
            self.metrics.crossed_events += 1
        self.metrics.snapshots += 1

    def apply_update(
        self,
        event: DepthUpdate | Mapping[str, Any],
        *,
        local_receive_time_ms: int | None = None,
    ) -> BookUpdateResult:
        """Validate and apply one Binance diff-depth update atomically."""

        update = event if isinstance(event, DepthUpdate) else DepthUpdate.from_message(event)
        if update.symbol is not None and update.symbol != self.symbol:
            raise ValueError(f"event symbol {update.symbol!r} does not match {self.symbol!r}")
        if not self._has_snapshot or self.last_update_id is None:
            raise RuntimeError("a REST snapshot must be loaded before applying updates")

        received_ms = epoch_ms() if local_receive_time_ms is None else int(local_receive_time_ms)
        self.local_receive_time_ms = received_ms
        self.exchange_event_time_ms = update.event_time_ms
        previous_id = self.last_update_id

        if update.final_update_id <= previous_id:
            self.metrics.stale_updates += 1
            return BookUpdateResult(
                status=UpdateStatus.STALE,
                previous_update_id=previous_id,
                final_update_id=update.final_update_id,
                reason="event final id is not newer than the local id",
            )

        expected_id = previous_id + 1
        if update.first_update_id > expected_id:
            return self._gap_result(
                previous_id,
                update.final_update_id,
                f"event starts at {update.first_update_id}, expected coverage of {expected_id}",
            )
        if (
            self.synchronized
            and update.previous_final_update_id is not None
            and update.previous_final_update_id != previous_id
        ):
            return self._gap_result(
                previous_id,
                update.final_update_id,
                f"event pu={update.previous_final_update_id}, expected {previous_id}",
            )

        was_crossed = self.crossed
        for price, quantity in update.bids:
            self._update_level(self._bids, price, quantity)
        for price, quantity in update.asks:
            self._update_level(self._asks, price, quantity)
        self._trim()

        self.last_update_id = update.final_update_id
        self.book_update_time_ms = received_ms
        self.synchronized = True
        self.crossed = self._is_crossed()
        self.metrics.updates_applied += 1
        applied_levels = len(update.bids) + len(update.asks)
        self.metrics.levels_updated += applied_levels
        if self.crossed and not was_crossed:
            self.metrics.crossed_events += 1

        status = UpdateStatus.CROSSED if self.crossed else UpdateStatus.APPLIED
        return BookUpdateResult(
            status=status,
            previous_update_id=previous_id,
            final_update_id=update.final_update_id,
            applied_levels=applied_levels,
            reason="best bid is at or above best ask" if self.crossed else None,
        )

    def age_ms(self, *, now_ms: int | None = None) -> int | None:
        if self.book_update_time_ms is None:
            return None
        current_ms = epoch_ms() if now_ms is None else int(now_ms)
        return max(0, current_ms - self.book_update_time_ms)

    def is_stale(self, stale_after_ms: int, *, now_ms: int | None = None) -> bool:
        if stale_after_ms < 0:
            raise ValueError("stale_after_ms must be non-negative")
        age = self.age_ms(now_ms=now_ms)
        return age is None or age > stale_after_ms

    def health(self, stale_after_ms: int = 2000, *, now_ms: int | None = None) -> BookHealth:
        age = self.age_ms(now_ms=now_ms)
        stale = age is None or age > stale_after_ms
        return BookHealth(
            symbol=self.symbol,
            healthy=self.healthy and not stale,
            synchronized=self.synchronized,
            connected=self.connected,
            stale=stale,
            crossed=self.crossed,
            has_bids=bool(self._bids),
            has_asks=bool(self._asks),
            last_update_id=self.last_update_id,
            exchange_event_time_ms=self.exchange_event_time_ms,
            local_receive_time_ms=self.local_receive_time_ms,
            book_update_time_ms=self.book_update_time_ms,
            age_ms=age,
        )

    def _gap_result(self, previous_id: int, final_id: int, reason: str) -> BookUpdateResult:
        self.metrics.sequence_gaps += 1
        self.synchronized = False
        # A missing diff cannot be repaired by later diffs alone.  Requiring a
        # new snapshot prevents callers from accidentally reviving a gapped book.
        self._has_snapshot = False
        return BookUpdateResult(
            status=UpdateStatus.GAP,
            previous_update_id=previous_id,
            final_update_id=final_id,
            reason=reason,
        )

    @staticmethod
    def _update_level(levels: dict[Decimal, Decimal], price: Decimal, quantity: Decimal) -> None:
        if quantity == 0:
            levels.pop(price, None)
        else:
            levels[price] = quantity

    def _trim(self) -> None:
        if len(self._bids) > self.max_depth:
            keep = set(sorted(self._bids, reverse=True)[: self.max_depth])
            self._bids = {
                price: quantity for price, quantity in self._bids.items() if price in keep
            }
        if len(self._asks) > self.max_depth:
            keep = set(sorted(self._asks)[: self.max_depth])
            self._asks = {
                price: quantity for price, quantity in self._asks.items() if price in keep
            }

    def _is_crossed(self) -> bool:
        if not self._bids or not self._asks:
            return False
        return max(self._bids) >= min(self._asks)


def _parse_levels(raw_levels: Any, name: str) -> tuple[Level, ...]:
    if raw_levels is None:
        return ()
    if isinstance(raw_levels, (str, bytes)) or not isinstance(raw_levels, Iterable):
        raise ValueError(f"{name} must be an iterable of price/quantity pairs")

    parsed: list[Level] = []
    for raw_level in raw_levels:
        if isinstance(raw_level, (str, bytes)) or not isinstance(raw_level, Sequence):
            raise ValueError(f"invalid {name} level: {raw_level!r}")
        if len(raw_level) < 2:
            raise ValueError(f"invalid {name} level: {raw_level!r}")
        try:
            price = Decimal(str(raw_level[0]))
            quantity = Decimal(str(raw_level[1]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"invalid Decimal in {name} level: {raw_level!r}") from exc
        if not price.is_finite() or price <= 0:
            raise ValueError(f"{name} price must be finite and positive")
        if not quantity.is_finite() or quantity < 0:
            raise ValueError(f"{name} quantity must be finite and non-negative")
        parsed.append((price, quantity))
    return tuple(parsed)


def _optional_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"depth update {field} must be an integer") from exc


def _limit_levels(levels: tuple[Level, ...], limit: int | None) -> tuple[Level, ...]:
    if limit is None:
        return levels
    if limit < 0:
        raise ValueError("limit must be non-negative")
    return levels[:limit]


__all__ = [
    "BookHealth",
    "BookMetrics",
    "BookUpdateResult",
    "DepthUpdate",
    "Level",
    "OrderBook",
    "UpdateStatus",
    "epoch_ms",
]
