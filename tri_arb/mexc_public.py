"""Research-only adapter for MEXC Spot V3 public market data.

Only explicitly allow-listed public REST resources and public protobuf depth
streams are implemented.  This module has no API-key, account, balance, order,
or private-stream surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

try:  # Preserve offline Binance/test imports before optional deps are installed.
    import aiohttp
except ImportError:  # pragma: no cover - only relevant in minimal environments.
    aiohttp = None  # type: ignore[assignment]

from .exchange import NormalizedDepthUpdate, NormalizedOrderBookSnapshot
from .mexc_proto import MexcProtoDepthUpdate, decode_mexc_depth_frame
from .models import (
    ZERO,
    LotSizeFilter,
    MarketStats,
    NotionalFilter,
    PriceFilter,
    SymbolFilters,
    SymbolInfo,
)

DEFAULT_MEXC_REST_BASE_URL = "https://api.mexc.com"
DEFAULT_MEXC_WS_BASE_URL = "wss://wbs-api.mexc.com/ws"
MAX_MEXC_STREAMS_PER_CONNECTION = 30

_PUBLIC_PATHS = frozenset(
    {
        "/api/v3/exchangeInfo",
        "/api/v3/ticker/24hr",
        "/api/v3/ticker/bookTicker",
        "/api/v3/depth",
    }
)
_RETRYABLE_STATUSES = frozenset({418, 429, 500, 502, 503, 504})
_DEPTH_LIMITS = frozenset({5, 10, 20, 50, 100, 500, 1000, 5000})
_DEPTH_CHANNEL = re.compile(r"^spot@public\.aggre\.depth\.v3\.api\.pb@(10ms|100ms)@([A-Z0-9]+)$")


@dataclass(frozen=True, slots=True)
class MexcPublicError(RuntimeError):
    """Public MEXC transport/response error with non-sensitive context."""

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


class MexcPublicClient:
    """MEXC Spot V3 public-data adapter and protobuf depth subscriber."""

    exchange_name = "MEXC"

    def __init__(
        self,
        base_url: str = DEFAULT_MEXC_REST_BASE_URL,
        *,
        websocket_base_url: str = DEFAULT_MEXC_WS_BASE_URL,
        session: Any | None = None,
        request_timeout_s: float = 10.0,
        max_retries: int = 4,
        backoff_base_s: float = 0.25,
        backoff_cap_s: float = 8.0,
        stream_interval_ms: int = 100,
        max_streams_per_connection: int = MAX_MEXC_STREAMS_PER_CONNECTION,
        websocket_ping_interval_s: float = 20.0,
        update_queue_size: int = 10_000,
    ) -> None:
        if not base_url.lower().startswith("https://"):
            raise ValueError("MEXC REST base_url must use https")
        if not websocket_base_url.lower().startswith("wss://"):
            raise ValueError("MEXC WebSocket base URL must use wss")
        if request_timeout_s <= 0:
            raise ValueError("request_timeout_s must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if backoff_base_s < 0 or backoff_cap_s <= 0:
            raise ValueError("invalid backoff configuration")
        if stream_interval_ms not in {10, 100}:
            raise ValueError("MEXC depth stream interval must be 10 or 100 ms")
        if not 1 <= max_streams_per_connection <= MAX_MEXC_STREAMS_PER_CONNECTION:
            raise ValueError("MEXC permits at most 30 streams per WebSocket connection")
        if websocket_ping_interval_s <= 0:
            raise ValueError("websocket_ping_interval_s must be positive")
        if update_queue_size <= 0:
            raise ValueError("update_queue_size must be positive")

        self.base_url = base_url.rstrip("/")
        self.websocket_base_url = websocket_base_url.rstrip("/")
        self.request_timeout_s = float(request_timeout_s)
        self.max_retries = int(max_retries)
        self.backoff_base_s = float(backoff_base_s)
        self.backoff_cap_s = float(backoff_cap_s)
        self.stream_interval_ms = int(stream_interval_ms)
        self.max_streams_per_connection = int(max_streams_per_connection)
        self.websocket_ping_interval_s = float(websocket_ping_interval_s)
        self.update_queue_size = int(update_queue_size)
        self._session = session
        self._owns_session = session is None
        self._closed = False
        self._logger = logging.getLogger(__name__)

    async def __aenter__(self) -> MexcPublicClient:
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

    async def get_exchange_info(self) -> Mapping[str, Any]:
        payload = await self._request_json("/api/v3/exchangeInfo")
        if not isinstance(payload, Mapping):
            raise MexcPublicError("unexpected exchangeInfo response shape")
        return payload

    async def get_24h_tickers(self, symbol: str | None = None) -> Any:
        params = {"symbol": normalize_mexc_symbol(symbol)} if symbol is not None else None
        payload = await self._request_json("/api/v3/ticker/24hr", params=params)
        if not isinstance(payload, (Mapping, list)):
            raise MexcPublicError("unexpected 24hr ticker response shape")
        return payload

    async def get_book_tickers(self, symbol: str | None = None) -> Any:
        """Fetch public top-of-book fields when 24-hour rows omit quantities."""

        params = {"symbol": normalize_mexc_symbol(symbol)} if symbol is not None else None
        payload = await self._request_json("/api/v3/ticker/bookTicker", params=params)
        if not isinstance(payload, (Mapping, list)):
            raise MexcPublicError("unexpected bookTicker response shape")
        return payload

    async def get_depth_snapshot(self, symbol: str, limit: int = 1000) -> Mapping[str, Any]:
        if limit not in _DEPTH_LIMITS:
            allowed = ", ".join(str(value) for value in sorted(_DEPTH_LIMITS))
            raise ValueError(f"MEXC depth limit must be one of: {allowed}")
        payload = await self._request_json(
            "/api/v3/depth",
            params={"symbol": normalize_mexc_symbol(symbol), "limit": int(limit)},
        )
        if not isinstance(payload, Mapping) or not ({"lastUpdateId", "version"} & payload.keys()):
            raise MexcPublicError("unexpected depth snapshot response shape")
        return payload

    # Compatibility aliases make the adapter drop-in friendly for the original
    # Binance-oriented orchestration while callers migrate to the public interface.
    exchange_info = get_exchange_info
    ticker_24hr = get_24h_tickers
    ticker_24h = get_24h_tickers
    depth_snapshot = get_depth_snapshot

    def normalize_symbol_metadata(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[SymbolInfo, ...]:
        return normalize_mexc_symbol_metadata(payload)

    def normalize_ticker(
        self,
        payload: Any,
        book_tickers: Any | None = None,
    ) -> dict[str, MarketStats]:
        return normalize_mexc_tickers(payload, book_tickers=book_tickers)

    def normalize_order_book_snapshot(
        self,
        symbol: str,
        payload: Mapping[str, Any],
    ) -> NormalizedOrderBookSnapshot:
        return normalize_mexc_order_book_snapshot(symbol, payload)

    def normalize_depth_update(self, payload: Any) -> NormalizedDepthUpdate:
        return normalize_mexc_depth_update(payload)

    def depth_channels(self, symbols: Sequence[str]) -> tuple[str, ...]:
        return tuple(
            mexc_depth_channel(symbol, interval_ms=self.stream_interval_ms)
            for symbol in _unique_symbols(symbols)
        )

    def depth_shards(self, symbols: Sequence[str]) -> tuple[tuple[str, ...], ...]:
        normalized = _unique_symbols(symbols)
        return tuple(
            normalized[index : index + self.max_streams_per_connection]
            for index in range(0, len(normalized), self.max_streams_per_connection)
        )

    async def subscribe_depth(
        self,
        symbols: Sequence[str],
    ) -> AsyncIterator[NormalizedDepthUpdate]:
        """Yield public protobuf depth updates across <=30-stream WS shards."""

        shards = self.depth_shards(symbols)
        if not shards:
            raise ValueError("at least one MEXC symbol is required")
        await self._ensure_session()
        queue: asyncio.Queue[NormalizedDepthUpdate] = asyncio.Queue(maxsize=self.update_queue_size)
        tasks = [asyncio.create_task(self._run_depth_shard(shard, queue)) for shard in shards]
        try:
            while True:
                yield await queue.get()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_depth_shard(
        self,
        symbols: tuple[str, ...],
        queue: asyncio.Queue[NormalizedDepthUpdate],
    ) -> None:
        attempt = 0
        channels = [
            mexc_depth_channel(symbol, interval_ms=self.stream_interval_ms) for symbol in symbols
        ]
        while not self._closed:
            try:
                session = await self._ensure_session()
                async with session.ws_connect(
                    self.websocket_base_url,
                    autoping=True,
                    heartbeat=None,
                ) as websocket:
                    await websocket.send_json({"method": "SUBSCRIPTION", "params": channels})
                    attempt = 0
                    while not self._closed:
                        try:
                            message = await asyncio.wait_for(
                                websocket.receive(),
                                timeout=self.websocket_ping_interval_s,
                            )
                        except TimeoutError:
                            await websocket.send_json({"method": "PING"})
                            continue
                        if aiohttp is None:  # pragma: no cover - guarded by _ensure_session.
                            raise RuntimeError("aiohttp is required for MEXC WebSocket access")
                        if message.type is aiohttp.WSMsgType.BINARY:
                            try:
                                update = self.normalize_depth_update(message.data)
                            except (TypeError, ValueError) as exc:
                                self._logger.warning(
                                    "discarding malformed MEXC depth frame: %s", exc
                                )
                                continue
                            if update.symbol not in symbols:
                                self._logger.warning(
                                    "discarding MEXC depth frame for unsubscribed symbol %s",
                                    update.symbol,
                                )
                                continue
                            await queue.put(update)
                        elif message.type is aiohttp.WSMsgType.TEXT:
                            self._validate_control_message(message.data)
                        elif message.type in {
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.ERROR,
                        }:
                            raise MexcPublicError("MEXC public WebSocket disconnected")
            except asyncio.CancelledError:
                raise
            except (*_network_errors(), MexcPublicError) as exc:
                if self._closed:
                    return
                delay = self._backoff_delay(attempt)
                attempt += 1
                self._logger.warning(
                    "MEXC public WebSocket shard reconnecting in %.2fs: %s",
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

    @staticmethod
    def _validate_control_message(raw: str) -> None:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise MexcPublicError("invalid MEXC WebSocket control response") from exc
        if not isinstance(payload, Mapping):
            raise MexcPublicError("invalid MEXC WebSocket control response")
        code = payload.get("code")
        if code not in (None, 0, "0"):
            raise MexcPublicError(
                f"MEXC WebSocket subscription rejected: {str(payload.get('msg', code))[:200]}"
            )

    async def _ensure_session(self) -> Any:
        if self._closed:
            raise RuntimeError("MexcPublicClient is closed")
        if self._session is None:
            if aiohttp is None:
                raise RuntimeError("aiohttp is required for MEXC public market data")
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
                            raise MexcPublicError(
                                "MEXC returned invalid JSON",
                                status=response.status,
                                endpoint=path,
                            ) from exc
                    body = await response.text()
                    error = MexcPublicError(
                        _safe_error_message(body),
                        status=response.status,
                        endpoint=path,
                    )
                    if response.status not in _RETRYABLE_STATUSES or attempt >= self.max_retries:
                        raise error
                    last_error = error
                    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
            except MexcPublicError:
                raise
            except asyncio.CancelledError:
                raise
            except _network_errors() as exc:
                last_error = exc
                retry_after = None
                if attempt >= self.max_retries:
                    raise MexcPublicError(
                        f"public market-data request failed: {type(exc).__name__}",
                        endpoint=path,
                    ) from exc
            delay = retry_after if retry_after is not None else self._backoff_delay(attempt)
            await asyncio.sleep(delay)

        raise MexcPublicError(
            f"public market-data request failed after retries: {type(last_error).__name__}",
            endpoint=path,
        )

    def _backoff_delay(self, attempt: int) -> float:
        base = min(self.backoff_cap_s, self.backoff_base_s * (2**attempt))
        return min(self.backoff_cap_s, base * random.uniform(0.8, 1.2))


def normalize_mexc_symbol(symbol: str) -> str:
    """Normalize a channel/REST symbol without guessing its base/quote split."""

    normalized = symbol.strip().upper()
    if not normalized or not normalized.isalnum():
        raise ValueError("MEXC symbol must be a non-empty alphanumeric value")
    return normalized


def mexc_depth_channel(symbol: str, *, interval_ms: int = 100) -> str:
    if interval_ms not in {10, 100}:
        raise ValueError("MEXC aggregate-depth interval must be 10 or 100 ms")
    return f"spot@public.aggre.depth.v3.api.pb@{interval_ms}ms@{normalize_mexc_symbol(symbol)}"


def symbol_from_mexc_depth_channel(channel: str) -> str:
    match = _DEPTH_CHANNEL.fullmatch(channel.strip())
    if match is None:
        raise ValueError("invalid MEXC aggregate-depth channel")
    return match.group(2)


def normalize_mexc_symbol_metadata(payload: Mapping[str, Any]) -> tuple[SymbolInfo, ...]:
    """Normalize MEXC exchangeInfo records into the existing discovery model."""

    if not isinstance(payload, Mapping):
        raise ValueError("MEXC exchangeInfo must be a mapping")
    raw_symbols = payload.get("symbols")
    if raw_symbols is None and all(key in payload for key in ("symbol", "baseAsset", "quoteAsset")):
        raw_symbols = (payload,)
    if not _is_sequence(raw_symbols):
        raise ValueError("MEXC exchangeInfo.symbols must be a list")

    parsed: dict[str, SymbolInfo] = {}
    for raw in raw_symbols:
        if not isinstance(raw, Mapping):
            continue
        symbol = str(raw.get("symbol", "")).strip().upper()
        base_asset = str(raw.get("baseAsset", "")).strip().upper()
        quote_asset = str(raw.get("quoteAsset", "")).strip().upper()
        if (
            not symbol
            or not symbol.isalnum()
            or not base_asset
            or not quote_asset
            or base_asset == quote_asset
        ):
            continue

        permissions = _string_set(raw.get("permissions", ()))
        raw_status = str(raw.get("status", "")).strip().upper()
        status = "TRADING" if raw_status in {"1", "ENABLED", "TRADING"} else raw_status
        if "isSpotTradingAllowed" in raw:
            spot_allowed = _boolean(raw.get("isSpotTradingAllowed"))
        else:
            spot_allowed = "SPOT" in permissions
        trade_side_type = str(raw.get("tradeSideType", "")).strip()
        if trade_side_type and trade_side_type != "1":
            spot_allowed = False

        order_types = (
            tuple(str(value).upper() for value in raw.get("orderTypes", ()) if value)
            if _is_sequence(raw.get("orderTypes", ()))
            else ()
        )
        parsed[symbol] = SymbolInfo(
            symbol=symbol,
            base_asset=base_asset,
            quote_asset=quote_asset,
            status=status,
            is_spot_trading_allowed=spot_allowed,
            permissions=permissions,
            order_types=order_types,
            filters=_normalize_mexc_filters(raw),
            quote_order_qty_market_allowed=_boolean(raw.get("quoteOrderQtyMarketAllowed")),
            base_asset_precision=_optional_integer(raw.get("baseAssetPrecision")),
            quote_asset_precision=_optional_integer(
                raw.get("quoteAssetPrecision", raw.get("quotePrecision"))
            ),
        )
    return tuple(parsed[symbol] for symbol in sorted(parsed))


def normalize_mexc_tickers(
    payload: Any,
    *,
    book_tickers: Any | None = None,
) -> dict[str, MarketStats]:
    """Normalize MEXC 24-hour rows, optionally merging public bookTicker rows."""

    book_rows = _rows(book_tickers) if book_tickers is not None else ()
    top_by_symbol = {
        str(row.get("symbol", "")).strip().upper(): row
        for row in book_rows
        if isinstance(row, Mapping) and row.get("symbol")
    }
    parsed: dict[str, MarketStats] = {}
    for raw in _rows(payload):
        if not isinstance(raw, Mapping):
            continue
        symbol = str(raw.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        top = top_by_symbol.get(symbol, {})
        base_volume = _decimal(raw.get("volume"))
        quote_volume = _decimal(raw.get("quoteVolume"))
        last_price = _decimal(raw.get("lastPrice"))
        weighted_average = _decimal(raw.get("weightedAvgPrice"))
        if weighted_average <= ZERO and base_volume > ZERO and quote_volume > ZERO:
            weighted_average = quote_volume / base_volume
        if weighted_average <= ZERO:
            weighted_average = last_price
        stats = MarketStats(
            symbol=symbol,
            quote_volume=quote_volume,
            base_volume=base_volume,
            bid_price=_decimal(top.get("bidPrice", raw.get("bidPrice"))),
            bid_qty=_decimal(top.get("bidQty", raw.get("bidQty"))),
            ask_price=_decimal(top.get("askPrice", raw.get("askPrice"))),
            ask_qty=_decimal(top.get("askQty", raw.get("askQty"))),
            last_price=last_price,
            weighted_average_price=weighted_average,
            trade_count=_integer(raw.get("count")),
        )
        previous = parsed.get(symbol)
        if previous is None or stats.quote_volume > previous.quote_volume:
            parsed[symbol] = stats
    return parsed


def normalize_mexc_order_book_snapshot(
    symbol: str,
    payload: Mapping[str, Any],
) -> NormalizedOrderBookSnapshot:
    if not isinstance(payload, Mapping):
        raise ValueError("MEXC depth snapshot must be a mapping")
    raw_version = payload.get("lastUpdateId", payload.get("version"))
    try:
        version = int(raw_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("MEXC depth snapshot requires lastUpdateId or version") from exc
    if version < 0:
        raise ValueError("MEXC depth snapshot version must be non-negative")
    return NormalizedOrderBookSnapshot(
        exchange="MEXC",
        symbol=normalize_mexc_symbol(symbol),
        last_update_id=version,
        bids=_levels(payload.get("bids", ()), "bids"),
        asks=_levels(payload.get("asks", ()), "asks"),
        event_time_ms=_optional_integer(payload.get("timestamp", payload.get("time"))),
    )


def normalize_mexc_depth_update(payload: Any) -> NormalizedDepthUpdate:
    """Decode and normalize an absolute MEXC aggregate-depth protobuf update."""

    if isinstance(payload, (bytes, bytearray, memoryview)):
        decoded = decode_mexc_depth_frame(payload)
    elif isinstance(payload, MexcProtoDepthUpdate):
        decoded = payload
    elif isinstance(payload, Mapping):
        decoded = _proto_depth_from_mapping(payload)
    else:
        raise TypeError("MEXC depth update must be a protobuf frame or decoded message")

    try:
        first_version = int(decoded.from_version)
        final_version = int(decoded.to_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("MEXC depth update requires integer versions") from exc
    if first_version < 0 or final_version < first_version:
        raise ValueError("invalid MEXC depth update version range")
    symbol = normalize_mexc_symbol(decoded.symbol)
    if decoded.channel:
        channel_symbol = symbol_from_mexc_depth_channel(decoded.channel)
        if channel_symbol != symbol:
            raise ValueError("MEXC depth channel symbol does not match wrapper symbol")

    event_time = decoded.send_time_ms or decoded.create_time_ms
    return NormalizedDepthUpdate(
        exchange="MEXC",
        symbol=symbol,
        first_update_id=first_version,
        final_update_id=final_version,
        bids=_levels(decoded.bids, "bids"),
        asks=_levels(decoded.asks, "asks"),
        event_time_ms=event_time,
    )


def _proto_depth_from_mapping(payload: Mapping[str, Any]) -> MexcProtoDepthUpdate:
    body = payload.get("publicAggreDepths", payload.get("publicincreasedepths", payload))
    if not isinstance(body, Mapping):
        raise ValueError("MEXC decoded depth body must be a mapping")
    return MexcProtoDepthUpdate(
        channel=str(payload.get("channel", "")),
        symbol=str(payload.get("symbol", "")),
        from_version=str(body.get("fromVersion", body.get("from_version", ""))),
        to_version=str(body.get("toVersion", body.get("to_version", ""))),
        bids=tuple(_string_levels(body.get("bids", body.get("bidsList", ())), "bids")),
        asks=tuple(_string_levels(body.get("asks", body.get("asksList", ())), "asks")),
        event_type=str(body.get("eventType", body.get("eventtype", ""))),
        create_time_ms=_optional_integer(payload.get("createTime", payload.get("createtime"))),
        send_time_ms=_optional_integer(payload.get("sendTime", payload.get("sendtime"))),
        last_order_create_time_ms=_optional_integer(body.get("lastOrderCreateTime")),
    )


def _normalize_mexc_filters(raw: Mapping[str, Any]) -> SymbolFilters:
    price: PriceFilter | None = None
    lot: LotSizeFilter | None = None
    notional: NotionalFilter | None = None
    filter_types: set[str] = set()
    raw_filters = raw.get("filters", ())
    if _is_sequence(raw_filters):
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
            elif filter_type == "LOT_SIZE":
                lot = LotSizeFilter(
                    min_qty=_decimal(item.get("minQty")),
                    max_qty=_decimal(item.get("maxQty")),
                    step_size=_decimal(item.get("stepSize")),
                )
            elif filter_type in {"MIN_NOTIONAL", "NOTIONAL"}:
                notional = NotionalFilter(
                    min_notional=_decimal(item.get("minNotional")),
                    max_notional=_decimal(item.get("maxNotional")),
                    apply_min_to_market=True,
                )

    base_size = _decimal(raw.get("baseSizePrecision"))
    quote_minimum = _decimal(raw.get("quoteAmountPrecision"))
    max_quote = _decimal(raw.get("maxQuoteAmount"))
    if lot is None and base_size > ZERO:
        lot = LotSizeFilter(min_qty=base_size, step_size=base_size)
        filter_types.add("MEXC_BASE_SIZE")
    if notional is None and (quote_minimum > ZERO or max_quote > ZERO):
        notional = NotionalFilter(
            min_notional=quote_minimum,
            max_notional=max_quote,
            apply_min_to_market=True,
        )
        filter_types.add("MEXC_QUOTE_AMOUNT")
    return SymbolFilters(
        price=price,
        lot_size=lot,
        market_lot_size=lot,
        notional=notional,
        filter_types=frozenset(filter_types),
    )


def _levels(raw_levels: Any, label: str) -> tuple[tuple[Decimal, Decimal], ...]:
    result: list[tuple[Decimal, Decimal]] = []
    for price_value, quantity_value in _string_levels(raw_levels, label):
        try:
            price = Decimal(price_value)
            quantity = Decimal(quantity_value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"invalid MEXC {label} decimal level") from exc
        if not price.is_finite() or not quantity.is_finite() or price <= ZERO or quantity < ZERO:
            raise ValueError(f"invalid MEXC {label} price or quantity")
        result.append((price, quantity))
    return tuple(result)


def _string_levels(raw_levels: Any, label: str) -> tuple[tuple[str, str], ...]:
    if not _is_sequence(raw_levels):
        raise ValueError(f"MEXC {label} must be a list")
    result: list[tuple[str, str]] = []
    for level in raw_levels:
        if isinstance(level, Mapping):
            price = level.get("price")
            quantity = level.get("quantity", level.get("qty"))
        elif _is_sequence(level) and len(level) >= 2:
            price, quantity = level[0], level[1]
        else:
            raise ValueError(f"invalid MEXC {label} level")
        if price is None or quantity is None:
            raise ValueError(f"invalid MEXC {label} level")
        result.append((str(price), str(quantity)))
    return tuple(result)


def _rows(payload: Any) -> tuple[Any, ...]:
    if isinstance(payload, Mapping):
        return (payload,)
    if _is_sequence(payload):
        return tuple(payload)
    raise ValueError("MEXC ticker payload must be a mapping or list")


def _unique_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    if isinstance(symbols, (str, bytes)):
        raise TypeError("MEXC symbols must be a sequence, not one string")
    return tuple(dict.fromkeys(normalize_mexc_symbol(symbol) for symbol in symbols))


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _string_set(value: Any) -> frozenset[str]:
    if not _is_sequence(value):
        return frozenset()
    return frozenset(str(item).upper() for item in value if item)


def _decimal(value: Any) -> Decimal:
    if value is None or value == "":
        return ZERO
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return ZERO
    return parsed if parsed.is_finite() else ZERO


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_integer(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
    if isinstance(payload, Mapping):
        message = payload.get("msg", payload.get("message"))
        if isinstance(message, str):
            return f"MEXC public API error: {message[:300]}"
    return f"MEXC public API error: {body[:300] or 'empty response'}"


def _network_errors() -> tuple[type[BaseException], ...]:
    errors: tuple[type[BaseException], ...] = (asyncio.TimeoutError, OSError)
    if aiohttp is not None:
        errors += (aiohttp.ClientError,)
    return errors


# Adapter-oriented aliases retained so orchestration can use descriptive naming.
MexcPublicAdapter = MexcPublicClient
MexcSpotAdapter = MexcPublicClient


__all__ = [
    "DEFAULT_MEXC_REST_BASE_URL",
    "DEFAULT_MEXC_WS_BASE_URL",
    "MAX_MEXC_STREAMS_PER_CONNECTION",
    "MexcPublicAdapter",
    "MexcPublicClient",
    "MexcPublicError",
    "MexcSpotAdapter",
    "mexc_depth_channel",
    "normalize_mexc_depth_update",
    "normalize_mexc_order_book_snapshot",
    "normalize_mexc_symbol",
    "normalize_mexc_symbol_metadata",
    "normalize_mexc_tickers",
    "symbol_from_mexc_depth_channel",
]
