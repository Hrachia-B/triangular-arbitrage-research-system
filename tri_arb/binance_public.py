"""Async client for Binance's public, market-data-only Spot REST API.

Only three explicitly allow-listed endpoints are implemented.  The default
host (``data-api.binance.vision``) is Binance's market-data-only service and
cannot be used for accounts or order entry.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .discovery import parse_exchange_info, parse_ticker_stats
from .exchange import NormalizedDepthUpdate, NormalizedOrderBookSnapshot
from .orderbook import DepthUpdate

try:  # Keep pure order-book tests importable before optional deps are installed.
    import aiohttp
except ImportError:  # pragma: no cover - exercised only in minimal environments.
    aiohttp = None  # type: ignore[assignment]


DEFAULT_REST_BASE_URL = "https://data-api.binance.vision"
DEFAULT_WS_BASE_URL = "wss://data-stream.binance.vision:443"
_PUBLIC_PATHS = frozenset(
    {
        "/api/v3/exchangeInfo",
        "/api/v3/ticker/24hr",
        "/api/v3/depth",
    }
)
_RETRYABLE_STATUSES = frozenset({418, 429, 500, 502, 503, 504})
_DEPTH_LIMITS = frozenset({5, 10, 20, 50, 100, 500, 1000, 5000})


@dataclass(frozen=True, slots=True)
class BinancePublicError(RuntimeError):
    """REST error with safe response context."""

    message: str
    status: int | None = None
    endpoint: str | None = None

    def __str__(self) -> str:
        details = []
        if self.status is not None:
            details.append(f"status={self.status}")
        if self.endpoint is not None:
            details.append(f"endpoint={self.endpoint}")
        suffix = f" ({', '.join(details)})" if details else ""
        return f"{self.message}{suffix}"


class BinancePublicClient:
    """Small ``aiohttp`` client restricted to public Spot market data."""

    exchange_name = "binance"

    def __init__(
        self,
        base_url: str = DEFAULT_REST_BASE_URL,
        *,
        session: Any | None = None,
        request_timeout_s: float = 10.0,
        max_retries: int = 4,
        backoff_base_s: float = 0.25,
        backoff_cap_s: float = 8.0,
    ) -> None:
        if not base_url.lower().startswith("https://"):
            raise ValueError("Binance REST base_url must use https")
        if request_timeout_s <= 0:
            raise ValueError("request_timeout_s must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if backoff_base_s < 0 or backoff_cap_s <= 0:
            raise ValueError("invalid backoff configuration")

        self.base_url = base_url.rstrip("/")
        self.request_timeout_s = float(request_timeout_s)
        self.max_retries = int(max_retries)
        self.backoff_base_s = float(backoff_base_s)
        self.backoff_cap_s = float(backoff_cap_s)
        self._session = session
        self._owns_session = session is None
        self._closed = False

    async def __aenter__(self) -> BinancePublicClient:
        await self._ensure_session()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_session and self._session is not None:
            await self._session.close()

    async def exchange_info(self) -> Mapping[str, Any]:
        payload = await self._request_json("/api/v3/exchangeInfo")
        if not isinstance(payload, Mapping):
            raise BinancePublicError("unexpected exchangeInfo response shape")
        return payload

    async def get_exchange_info(self) -> Mapping[str, Any]:
        """Exchange-adapter alias for the public metadata request."""

        return await self.exchange_info()

    async def ticker_24hr(self, symbol: str | None = None) -> Any:
        params = {"symbol": _normalize_symbol(symbol)} if symbol is not None else None
        payload = await self._request_json("/api/v3/ticker/24hr", params=params)
        if not isinstance(payload, (Mapping, list)):
            raise BinancePublicError("unexpected 24hr ticker response shape")
        return payload

    async def ticker_24h(self, symbol: str | None = None) -> Any:
        """Compatibility alias for :meth:`ticker_24hr`."""

        return await self.ticker_24hr(symbol)

    async def get_24h_tickers(self) -> Any:
        """Exchange-adapter alias returning every public 24-hour ticker."""

        return await self.ticker_24hr()

    async def depth_snapshot(self, symbol: str, limit: int = 1000) -> Mapping[str, Any]:
        if limit not in _DEPTH_LIMITS:
            allowed = ", ".join(str(value) for value in sorted(_DEPTH_LIMITS))
            raise ValueError(f"depth limit must be one of: {allowed}")
        payload = await self._request_json(
            "/api/v3/depth",
            params={"symbol": _normalize_symbol(symbol), "limit": int(limit)},
        )
        if not isinstance(payload, Mapping) or "lastUpdateId" not in payload:
            raise BinancePublicError("unexpected depth snapshot response shape")
        return payload

    async def get_depth_snapshot(self, symbol: str, limit: int) -> Mapping[str, Any]:
        """Exchange-adapter alias for an allow-listed public depth snapshot."""

        return await self.depth_snapshot(symbol, limit=limit)

    def normalize_symbol_metadata(self, payload: Mapping[str, Any]):
        """Normalize Binance exchangeInfo into exchange-neutral symbol models."""

        return parse_exchange_info(payload)

    def normalize_ticker(self, payload: Any):
        """Normalize Binance 24-hour statistics into exchange-neutral models."""

        return parse_ticker_stats(payload)

    def normalize_order_book_snapshot(
        self,
        symbol: str,
        payload: Mapping[str, Any],
    ) -> NormalizedOrderBookSnapshot:
        try:
            version = int(payload["lastUpdateId"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Binance depth snapshot requires integer lastUpdateId") from exc
        return NormalizedOrderBookSnapshot(
            exchange=self.exchange_name,
            symbol=_normalize_symbol(symbol),
            last_update_id=version,
            bids=_normalise_levels(payload.get("bids", ()), "bids"),
            asks=_normalise_levels(payload.get("asks", ()), "asks"),
        )

    def normalize_depth_update(self, payload: Any) -> NormalizedDepthUpdate:
        if not isinstance(payload, Mapping):
            raise ValueError("Binance depth update must be a mapping")
        update = DepthUpdate.from_message(payload)
        symbol = update.symbol
        if symbol is None:
            stream = payload.get("stream")
            if not isinstance(stream, str) or "@" not in stream:
                raise ValueError("Binance depth update has no symbol")
            symbol = stream.split("@", 1)[0].upper()
        return NormalizedDepthUpdate(
            exchange=self.exchange_name,
            symbol=symbol,
            first_update_id=update.first_update_id,
            final_update_id=update.final_update_id,
            bids=update.bids,
            asks=update.asks,
            event_time_ms=update.event_time_ms,
        )

    async def subscribe_depth(
        self,
        symbols: Sequence[str],
    ) -> AsyncIterator[NormalizedDepthUpdate]:
        """Yield normalized public Binance diff-depth messages.

        The production Binance manager still owns snapshot synchronization and
        sharding; this method is the exchange-neutral transport seam used by
        adapter-level callers and tests.
        """

        normalized = tuple(dict.fromkeys(_normalize_symbol(symbol) for symbol in symbols))
        if not normalized:
            raise ValueError("at least one symbol is required")
        if len(normalized) > 1024:
            raise ValueError("Binance permits at most 1024 streams per connection")
        if aiohttp is None:
            raise RuntimeError("aiohttp is required for Binance public WebSocket access")
        streams = "/".join(f"{symbol.lower()}@depth@100ms" for symbol in normalized)
        url = f"{DEFAULT_WS_BASE_URL}/stream?streams={streams}"
        async with (
            aiohttp.ClientSession() as session,
            session.ws_connect(
                url,
                heartbeat=20.0,
                max_msg_size=4 * 1024 * 1024,
            ) as ws,
        ):
            async for message in ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    try:
                        decoded = json.loads(message.data)
                        yield self.normalize_depth_update(decoded)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue
                elif message.type in {
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                }:
                    break

    async def _ensure_session(self) -> Any:
        if self._closed:
            raise RuntimeError("BinancePublicClient is closed")
        if self._session is None:
            if aiohttp is None:
                raise RuntimeError("aiohttp is required for Binance public REST access")
            timeout = aiohttp.ClientTimeout(total=self.request_timeout_s)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _request_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        if path not in _PUBLIC_PATHS:
            raise ValueError(f"endpoint is not in the public market-data allow-list: {path}")
        session = await self._ensure_session()
        endpoint = f"{self.base_url}{path}"
        last_error: BaseException | None = None

        for attempt in range(self.max_retries + 1):
            try:
                async with session.get(endpoint, params=params) as response:
                    if response.status == 200:
                        try:
                            return await response.json(content_type=None)
                        except (json.JSONDecodeError, ValueError, TypeError) as exc:
                            raise BinancePublicError(
                                "Binance returned invalid JSON",
                                status=response.status,
                                endpoint=path,
                            ) from exc

                    body = await response.text()
                    message = _safe_error_message(body)
                    error = BinancePublicError(message, status=response.status, endpoint=path)
                    if response.status not in _RETRYABLE_STATUSES or attempt >= self.max_retries:
                        raise error
                    last_error = error
                    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
            except BinancePublicError:
                raise
            except asyncio.CancelledError:
                raise
            except _network_errors() as exc:
                last_error = exc
                retry_after = None
                if attempt >= self.max_retries:
                    raise BinancePublicError(
                        f"public market-data request failed: {type(exc).__name__}",
                        endpoint=path,
                    ) from exc

            delay = retry_after if retry_after is not None else self._backoff_delay(attempt)
            await asyncio.sleep(delay)

        raise BinancePublicError(
            f"public market-data request failed after retries: {type(last_error).__name__}",
            endpoint=path,
        )

    def _backoff_delay(self, attempt: int) -> float:
        base = min(self.backoff_cap_s, self.backoff_base_s * (2**attempt))
        return min(self.backoff_cap_s, base * random.uniform(0.8, 1.2))


def _normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if not normalized or not normalized.isalnum():
        raise ValueError("symbol must be a non-empty alphanumeric Binance symbol")
    return normalized


def _normalise_levels(values: Any, name: str) -> tuple[tuple[Decimal, Decimal], ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError(f"{name} must be a sequence")
    levels: list[tuple[Decimal, Decimal]] = []
    for value in values:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) < 2:
            raise ValueError(f"invalid {name} level")
        try:
            price = Decimal(str(value[0]))
            quantity = Decimal(str(value[1]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"invalid {name} level") from exc
        if not price.is_finite() or not quantity.is_finite() or price <= 0 or quantity < 0:
            raise ValueError(f"invalid {name} level")
        levels.append((price, quantity))
    return tuple(levels)


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return min(60.0, max(0.0, float(value)))
    except ValueError:
        return None


def _safe_error_message(body: str) -> str:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if isinstance(payload, Mapping) and isinstance(payload.get("msg"), str):
        return f"Binance public API error: {payload['msg'][:300]}"
    return f"Binance public API error: {body[:300] or 'empty response'}"


def _network_errors() -> tuple[type[BaseException], ...]:
    errors: tuple[type[BaseException], ...] = (asyncio.TimeoutError, OSError)
    if aiohttp is not None:
        errors += (aiohttp.ClientError,)
    return errors


__all__ = [
    "DEFAULT_REST_BASE_URL",
    "DEFAULT_WS_BASE_URL",
    "BinancePublicClient",
    "BinancePublicError",
]
