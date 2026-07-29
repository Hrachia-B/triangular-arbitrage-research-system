"""Exchange-neutral contracts for public Spot market data.

The observer intentionally exposes no account, authentication, or order-entry
methods.  Exchange adapters normalize their public payloads into these immutable
types before the discovery and local-order-book layers consume them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from .models import MarketStats, SymbolInfo

Level = tuple[Decimal, Decimal]


@dataclass(frozen=True, slots=True)
class NormalizedOrderBookSnapshot:
    """A public REST order-book snapshot with an exchange sequence version."""

    exchange: str
    symbol: str
    last_update_id: int
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    event_time_ms: int | None = None

    @property
    def version(self) -> int:
        """Exchange-neutral alias for ``last_update_id``."""

        return self.last_update_id

    def as_mapping(self) -> dict[str, Any]:
        """Return the shape accepted by the existing local-book snapshot loader."""

        return {
            "lastUpdateId": self.last_update_id,
            "bids": self.bids,
            "asks": self.asks,
        }


@dataclass(frozen=True, slots=True)
class NormalizedDepthUpdate:
    """An absolute public depth update with an inclusive version range."""

    exchange: str
    symbol: str
    first_update_id: int
    final_update_id: int
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    event_time_ms: int | None = None

    @property
    def from_version(self) -> int:
        return self.first_update_id

    @property
    def to_version(self) -> int:
        return self.final_update_id


@runtime_checkable
class PublicExchangeData(Protocol):
    """Narrow interface implemented by research-only public exchange adapters."""

    exchange_name: str

    async def get_exchange_info(self) -> Mapping[str, Any]: ...

    async def get_24h_tickers(self) -> Any: ...

    async def get_depth_snapshot(self, symbol: str, limit: int) -> Mapping[str, Any]: ...

    def subscribe_depth(self, symbols: Sequence[str]) -> AsyncIterator[NormalizedDepthUpdate]: ...

    def normalize_symbol_metadata(self, payload: Mapping[str, Any]) -> tuple[SymbolInfo, ...]: ...

    def normalize_ticker(self, payload: Any) -> dict[str, MarketStats]: ...

    def normalize_order_book_snapshot(
        self,
        symbol: str,
        payload: Mapping[str, Any],
    ) -> NormalizedOrderBookSnapshot: ...

    def normalize_depth_update(self, payload: Any) -> NormalizedDepthUpdate: ...


# Short compatibility names for callers that do not need the normalization detail.
OrderBookSnapshot = NormalizedOrderBookSnapshot
PublicDepthUpdate = NormalizedDepthUpdate
PublicExchangeAdapter = PublicExchangeData


__all__ = [
    "Level",
    "NormalizedDepthUpdate",
    "NormalizedOrderBookSnapshot",
    "OrderBookSnapshot",
    "PublicDepthUpdate",
    "PublicExchangeAdapter",
    "PublicExchangeData",
]
