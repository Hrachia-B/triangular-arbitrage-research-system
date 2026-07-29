"""MEXC public-depth orchestration and REST snapshot synchronization.

The mature buffering/resynchronization machinery lives in the existing Binance
``BookManager``.  This adapter-backed subclass keeps those safety properties but
installs :class:`~tri_arb.mexc_orderbook.MexcOrderBook` instances and consumes
normalized events from ``subscribe_depth`` instead of Binance combined JSON
streams.  No authenticated or order endpoint is present here.
"""

from __future__ import annotations

import asyncio
import inspect
import random
from collections.abc import Iterable
from types import MappingProxyType
from typing import Any

from .book_manager import BookManager
from .mexc_orderbook import MexcOrderBook, coerce_mexc_depth_update
from .orderbook import BookUpdateResult, DepthUpdate, epoch_ms

DEFAULT_MEXC_WS_BASE_URL = "wss://wbs-api.mexc.com/ws"
MEXC_MAX_SUBSCRIPTIONS_PER_CONNECTION = 30


class _SnapshotClient:
    """Present the legacy manager snapshot method over the exchange interface."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter

    async def depth_snapshot(self, symbol: str, limit: int = 1000) -> Any:
        getter = getattr(self.adapter, "get_depth_snapshot", None)
        if not callable(getter):
            getter = getattr(self.adapter, "depth_snapshot", None)
        if not callable(getter):
            raise TypeError("MEXC adapter must provide get_depth_snapshot(symbol, limit)")
        result = getter(symbol, limit)
        return await result if inspect.isawaitable(result) else result


class MexcBookManager(BookManager):
    """Maintain scanner-compatible local MEXC books from public data only."""

    def __init__(
        self,
        symbols: Iterable[str],
        adapter: Any,
        *,
        max_depth: int = 1000,
        snapshot_limit: int = 1000,
        max_streams_per_connection: int = MEXC_MAX_SUBSCRIPTIONS_PER_CONNECTION,
        stream_interval_ms: int = 100,
        ws_base_url: str = DEFAULT_MEXC_WS_BASE_URL,
        stale_after_ms: int = 2000,
        max_buffer_events: int = 50_000,
        reconnect_backoff_base_s: float = 0.5,
        reconnect_backoff_cap_s: float = 30.0,
        sync_max_attempts: int = 5,
        on_book_update: Any | None = None,
        update_queue_size: int = 10_000,
    ) -> None:
        if stream_interval_ms not in {10, 100}:
            raise ValueError("MEXC depth stream_interval_ms must be 10 or 100")
        if not ws_base_url.lower().startswith("wss://"):
            raise ValueError("MEXC WebSocket base URL must use wss")
        if max_streams_per_connection <= 0:
            raise ValueError("max_streams_per_connection must be positive")

        self.adapter = adapter
        self.mexc_ws_base_url = ws_base_url.rstrip("/")
        self._snapshot_client = _SnapshotClient(adapter)
        requested_interval = int(stream_interval_ms)
        # The parent validates Binance's interval set.  Its networking method is
        # replaced below, so use 100 only during construction when MEXC 10 ms was
        # requested, then restore the actual MEXC interval.
        parent_interval = 100 if requested_interval == 10 else requested_interval
        super().__init__(
            symbols,
            self._snapshot_client,  # type: ignore[arg-type]
            max_depth=max_depth,
            snapshot_limit=snapshot_limit,
            max_streams_per_connection=min(
                int(max_streams_per_connection), MEXC_MAX_SUBSCRIPTIONS_PER_CONNECTION
            ),
            stream_interval_ms=parent_interval,
            ws_base_url=self.mexc_ws_base_url,
            stale_after_ms=stale_after_ms,
            max_buffer_events=max_buffer_events,
            reconnect_backoff_base_s=reconnect_backoff_base_s,
            reconnect_backoff_cap_s=reconnect_backoff_cap_s,
            sync_max_attempts=sync_max_attempts,
            ws_session=None,
            on_book_update=on_book_update,
            update_queue_size=update_queue_size,
        )
        self.stream_interval_ms = requested_interval

        # Retain the parent's private state container and synchronization logic,
        # changing only the sequence-policy implementation used by each book.
        state_type = type(next(iter(self._states.values())))
        self._states = {
            symbol: state_type(book=MexcOrderBook(symbol, max_depth=self.max_depth))
            for symbol in self.symbols
        }
        for state in self._states.values():
            state.book.connected = False
        self._books = {symbol: state.book for symbol, state in self._states.items()}
        self._books_view = MappingProxyType(self._books)

    @property
    def stream_urls(self) -> tuple[str, ...]:
        """One public MEXC endpoint per bounded subscription shard."""

        return tuple(self.mexc_ws_base_url for _ in self._symbol_shards())

    @property
    def subscription_channels(self) -> tuple[tuple[str, ...], ...]:
        return tuple(
            tuple(
                f"spot@public.aggre.depth.v3.api.pb@{self.stream_interval_ms}ms@{symbol}"
                for symbol in shard
            )
            for shard in self._symbol_shards()
        )

    async def start(self) -> None:
        """Start adapter subscriptions without creating a second WS session."""

        if self._running:
            return
        subscriber = getattr(self.adapter, "subscribe_depth", None)
        if not callable(subscriber):
            raise TypeError("MEXC adapter must provide subscribe_depth(symbols)")
        self._running = True
        for shard_number, shard in enumerate(self._symbol_shards()):
            task = asyncio.create_task(
                self._run_shard(shard, shard_number),
                name=f"mexc-depth-shard-{shard_number}",
            )
            self._shard_tasks.add(task)
            task.add_done_callback(self._shard_tasks.discard)
        await asyncio.sleep(0)

    async def ingest_depth_event(
        self,
        symbol: str,
        event: Any,
        *,
        local_receive_time_ms: int | None = None,
    ) -> BookUpdateResult | None:
        update = coerce_mexc_depth_update(event)
        return await super().ingest_depth_event(
            symbol,
            update,
            local_receive_time_ms=local_receive_time_ms,
        )

    async def _run_shard(self, symbols: tuple[str, ...], shard_number: int) -> None:
        reconnect_attempt = 0
        connected_once = False
        while self._running:
            prepared = False
            try:
                subscription = self.adapter.subscribe_depth(symbols)
                if inspect.isawaitable(subscription):
                    subscription = await subscription
                if not hasattr(subscription, "__aiter__"):
                    raise TypeError("subscribe_depth(symbols) must return an async iterator")

                async for item in subscription:  # type: ignore[union-attr]
                    if not self._running:
                        break
                    received_ms = epoch_ms()
                    try:
                        update = self._normalise_subscription_item(item)
                        symbol = update.symbol
                        if symbol is None or symbol not in self._states:
                            raise ValueError("MEXC depth update has an unmanaged or missing symbol")
                    except (KeyError, TypeError, ValueError):
                        self.metrics.malformed_messages += 1
                        self._logger.debug("discarded malformed MEXC depth message", exc_info=True)
                        continue

                    if not prepared:
                        await self._prepare_connected_shard(symbols)
                        self.metrics.websocket_connections += 1
                        if connected_once:
                            self.metrics.websocket_reconnects += 1
                        connected_once = True
                        reconnect_attempt = 0
                        prepared = True
                    self.metrics.websocket_messages += 1
                    await self.ingest_depth_event(
                        symbol,
                        update,
                        local_receive_time_ms=received_ms,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.warning(
                    "MEXC public depth shard %s disconnected", shard_number, exc_info=True
                )
            finally:
                if prepared:
                    await self._mark_shard_disconnected(symbols)

            if self._running:
                delay = min(
                    self.reconnect_backoff_cap_s,
                    self.reconnect_backoff_base_s * (2**reconnect_attempt),
                )
                reconnect_attempt += 1
                await asyncio.sleep(delay * random.uniform(0.8, 1.2))

    def _normalise_subscription_item(self, item: Any) -> DepthUpdate:
        candidate = getattr(item, "update", item)
        if isinstance(candidate, tuple) and len(candidate) == 2 and isinstance(candidate[0], str):
            candidate = candidate[1]
        try:
            return coerce_mexc_depth_update(candidate)
        except ValueError:
            normalizer = getattr(self.adapter, "normalize_depth_update", None)
            if not callable(normalizer):
                raise
            return coerce_mexc_depth_update(normalizer(candidate))


# Both spellings are exported because the exchange itself styles its name MEXC.
MEXCBookManager = MexcBookManager


__all__ = [
    "DEFAULT_MEXC_WS_BASE_URL",
    "MEXC_MAX_SUBSCRIPTIONS_PER_CONNECTION",
    "MEXCBookManager",
    "MexcBookManager",
]
