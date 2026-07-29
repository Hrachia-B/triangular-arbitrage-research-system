"""MEXC Spot local order-book sequencing.

MEXC aggregate depth messages carry an inclusive ``fromVersion`` / ``toVersion``
range.  Unlike Binance's overlapping diff batches, every non-stale MEXC batch
must begin exactly one version after the previously applied batch.  Prices and
quantities are absolute level values: a zero quantity removes the level.

The networking/protobuf boundary normalizes messages before they reach this
module.  The small coercion helpers also accept mapping-shaped fixtures and the
exchange abstraction's duck-typed ``PublicDepthUpdate`` / ``OrderBookSnapshot``
objects, keeping deterministic replay tests independent of network packages.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from .orderbook import (
    BookUpdateResult,
    DepthUpdate,
    Level,
    OrderBook,
    UpdateStatus,
    epoch_ms,
)


def _value(source: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(source, Mapping) and name in source:
            return source[name]
        if hasattr(source, name):
            return getattr(source, name)
    return default


def _optional_int(value: Any, name: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


def _levels(values: Any, name: str) -> tuple[Level, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Iterable):
        raise ValueError(f"{name} must be an iterable of levels")

    result: list[Level] = []
    for item in values:
        if isinstance(item, Mapping) or hasattr(item, "price"):
            raw_price = _value(item, "price", "p")
            raw_quantity = _value(item, "quantity", "qty", "q", "v")
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            if len(item) < 2:
                raise ValueError(f"invalid {name} level: {item!r}")
            raw_price, raw_quantity = item[0], item[1]
        else:
            raise ValueError(f"invalid {name} level: {item!r}")
        try:
            price = Decimal(str(raw_price))
            quantity = Decimal(str(raw_quantity))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"invalid {name} level: {item!r}") from exc
        if not price.is_finite() or not quantity.is_finite() or price <= 0 or quantity < 0:
            raise ValueError(f"invalid {name} level: {item!r}")
        result.append((price, quantity))
    return tuple(result)


def coerce_mexc_depth_update(event: Any) -> DepthUpdate:
    """Convert normalized or raw MEXC depth data to the shared update model."""

    if isinstance(event, DepthUpdate):
        return event

    envelope = _value(event, "data", default=event)
    payload = _value(
        envelope,
        "public_aggre_depths",
        "publicAggreDepths",
        default=envelope,
    )
    if payload is None:
        payload = event
    first = _value(
        payload,
        "first_update_id",
        "from_version",
        "fromVersion",
        default=None,
    )
    final = _value(
        payload,
        "final_update_id",
        "to_version",
        "toVersion",
        default=None,
    )
    try:
        first_id = int(first)
        final_id = int(final)
    except (TypeError, ValueError) as exc:
        raise ValueError("MEXC depth update requires integer fromVersion and toVersion") from exc
    if first_id < 0 or final_id < first_id:
        raise ValueError("invalid MEXC depth version range")

    wrapper_symbol = _value(envelope, "symbol", default=None)
    nested_symbol = _value(payload, "symbol", default=None)
    raw_symbol = nested_symbol if nested_symbol not in (None, "") else wrapper_symbol
    symbol = str(raw_symbol).strip().upper() if raw_symbol not in (None, "") else None
    event_time = _optional_int(
        _value(
            event,
            "event_time_ms",
            "send_time_ms",
            "send_time",
            "sendTime",
            "create_time_ms",
            "create_time",
            "createTime",
            default=_value(payload, "event_time_ms", "timestamp", default=None),
        ),
        "event time",
    )
    return DepthUpdate(
        first_update_id=first_id,
        final_update_id=final_id,
        bids=_levels(_value(payload, "bids", "b", default=()), "bids"),
        asks=_levels(_value(payload, "asks", "a", default=()), "asks"),
        event_time_ms=event_time,
        symbol=symbol,
    )


def coerce_mexc_snapshot(snapshot: Any) -> Mapping[str, Any]:
    """Return an OrderBook-compatible mapping from a MEXC snapshot object."""

    mapper = getattr(snapshot, "as_mapping", None)
    if callable(mapper):
        snapshot = mapper()
    payload = _value(snapshot, "data", default=snapshot)
    if not isinstance(payload, Mapping) and not hasattr(payload, "last_update_id"):
        raise ValueError("MEXC depth snapshot must be a mapping or normalized snapshot")
    version = _value(payload, "last_update_id", "lastUpdateId", "version", default=None)
    try:
        update_id = int(version)
    except (TypeError, ValueError) as exc:
        raise ValueError("MEXC snapshot requires integer version or lastUpdateId") from exc
    if update_id < 0:
        raise ValueError("MEXC snapshot version must be non-negative")
    return {
        "lastUpdateId": update_id,
        "bids": _levels(_value(payload, "bids", default=()), "bids"),
        "asks": _levels(_value(payload, "asks", default=()), "asks"),
    }


class MexcOrderBook(OrderBook):
    """Bounded MEXC book with strict contiguous version enforcement."""

    def load_snapshot(
        self,
        snapshot: Any,
        *,
        local_receive_time_ms: int | None = None,
    ) -> None:
        super().load_snapshot(
            coerce_mexc_snapshot(snapshot),
            local_receive_time_ms=local_receive_time_ms,
        )

    def apply_update(
        self,
        event: Any,
        *,
        local_receive_time_ms: int | None = None,
    ) -> BookUpdateResult:
        """Apply one absolute-size MEXC batch, rejecting any version gap."""

        update = coerce_mexc_depth_update(event)
        if update.symbol is not None and update.symbol != self.symbol:
            raise ValueError(f"event symbol {update.symbol!r} does not match {self.symbol!r}")
        if not self.has_snapshot or self.last_update_id is None:
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
                reason="event toVersion is not newer than the local version",
            )

        expected_id = previous_id + 1
        # The first buffered batch only has to bridge the snapshot's next
        # version. Once synchronized, MEXC requires exact batch-to-batch
        # contiguity: fromVersion == previous toVersion + 1.
        gap = (
            update.first_update_id != expected_id
            if self.synchronized
            else update.first_update_id > expected_id
        )
        if gap:
            return self._gap_result(
                previous_id,
                update.final_update_id,
                f"event fromVersion={update.first_update_id}, expected {expected_id}",
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


__all__ = [
    "MexcOrderBook",
    "coerce_mexc_depth_update",
    "coerce_mexc_snapshot",
]
