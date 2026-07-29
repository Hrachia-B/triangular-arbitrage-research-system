"""Live Binance public diff-stream orchestration and snapshot synchronization."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from types import MappingProxyType
from typing import Any

try:  # Allow pure replay tests to run in environments without optional deps.
    import aiohttp
except ImportError:  # pragma: no cover - only relevant before dependencies install.
    aiohttp = None  # type: ignore[assignment]

from .binance_public import BinancePublicClient
from .orderbook import (
    BookUpdateResult,
    DepthUpdate,
    OrderBook,
    UpdateStatus,
    epoch_ms,
)

DEFAULT_WS_BASE_URL = "wss://data-stream.binance.vision:443"

UpdateListener = Callable[[str, OrderBook, BookUpdateResult], Awaitable[None] | None]


class SnapshotSyncError(RuntimeError):
    """Raised when a symbol cannot be bridged to a REST snapshot."""


@dataclass(slots=True)
class BookManagerMetrics:
    websocket_connections: int = 0
    websocket_reconnects: int = 0
    websocket_messages: int = 0
    malformed_messages: int = 0
    snapshot_requests: int = 0
    snapshot_failures: int = 0
    sequence_gaps: int = 0
    resyncs: int = 0
    buffer_overflows: int = 0
    crossed_updates: int = 0
    listener_errors: int = 0
    update_notifications_dropped: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _BufferedUpdate:
    update: DepthUpdate
    local_receive_time_ms: int


@dataclass(slots=True)
class _SymbolState:
    book: OrderBook
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    buffer: deque[_BufferedUpdate] = field(default_factory=deque)
    syncing: bool = False
    sync_token: int = 0
    connected: bool = False


class BookManager:
    """Maintain synchronized local books over sharded combined depth streams.

    A socket is connected before any snapshot request is started.  Updates are
    buffered during the REST request, stale events are discarded, and the first
    retained event must bridge ``lastUpdateId + 1``.  A sequence gap invalidates
    only the affected symbol and triggers a fresh snapshot while its stream keeps
    buffering; unrelated books on the same socket remain usable.
    """

    def __init__(
        self,
        symbols: Iterable[str],
        rest_client: BinancePublicClient,
        *,
        max_depth: int = 1000,
        snapshot_limit: int = 1000,
        max_streams_per_connection: int = 200,
        stream_interval_ms: int = 100,
        ws_base_url: str = DEFAULT_WS_BASE_URL,
        stale_after_ms: int = 2000,
        max_buffer_events: int = 50_000,
        reconnect_backoff_base_s: float = 0.5,
        reconnect_backoff_cap_s: float = 30.0,
        sync_max_attempts: int = 5,
        ws_session: Any | None = None,
        on_book_update: UpdateListener | None = None,
        update_queue_size: int = 10_000,
    ) -> None:
        normalized_symbols = tuple(dict.fromkeys(_normalize_symbol(value) for value in symbols))
        if not normalized_symbols:
            raise ValueError("at least one symbol is required")
        if max_depth <= 0:
            raise ValueError("max_depth must be positive")
        if snapshot_limit not in {5, 10, 20, 50, 100, 500, 1000, 5000}:
            raise ValueError("snapshot_limit is not supported by Binance Spot")
        if max_depth > snapshot_limit:
            raise ValueError("max_depth cannot exceed snapshot_limit")
        if not 1 <= max_streams_per_connection <= 1024:
            raise ValueError("max_streams_per_connection must be between 1 and 1024")
        if stream_interval_ms not in {100, 1000}:
            raise ValueError("stream_interval_ms must be 100 or 1000")
        if not ws_base_url.lower().startswith("wss://"):
            raise ValueError("Binance WebSocket base URL must use wss")
        if stale_after_ms < 0 or max_buffer_events <= 0:
            raise ValueError("invalid staleness or buffer configuration")
        if reconnect_backoff_base_s < 0 or reconnect_backoff_cap_s <= 0:
            raise ValueError("invalid reconnect backoff configuration")
        if sync_max_attempts <= 0:
            raise ValueError("sync_max_attempts must be positive")
        if update_queue_size <= 0:
            raise ValueError("update_queue_size must be positive")

        self.symbols = normalized_symbols
        self.rest_client = rest_client
        self.max_depth = int(max_depth)
        self.snapshot_limit = int(snapshot_limit)
        self.max_streams_per_connection = int(max_streams_per_connection)
        self.stream_interval_ms = int(stream_interval_ms)
        self.ws_base_url = ws_base_url.rstrip("/")
        self.stale_after_ms = int(stale_after_ms)
        self.max_buffer_events = int(max_buffer_events)
        self.reconnect_backoff_base_s = float(reconnect_backoff_base_s)
        self.reconnect_backoff_cap_s = float(reconnect_backoff_cap_s)
        self.sync_max_attempts = int(sync_max_attempts)

        self._states = {
            symbol: _SymbolState(book=OrderBook(symbol, max_depth=self.max_depth))
            for symbol in normalized_symbols
        }
        for state in self._states.values():
            # A managed book is disconnected until its shard has actually opened.
            state.book.connected = False
        self._books = {symbol: state.book for symbol, state in self._states.items()}
        self._books_view: Mapping[str, OrderBook] = MappingProxyType(self._books)
        self._ws_session = ws_session
        self._owns_ws_session = ws_session is None
        self._running = False
        self._shard_tasks: set[asyncio.Task[Any]] = set()
        self._sync_tasks: set[asyncio.Task[Any]] = set()
        self._listener_tasks: set[asyncio.Future[Any]] = set()
        self._listeners: list[UpdateListener] = []
        if on_book_update is not None:
            self._listeners.append(on_book_update)
        self._health_changed = asyncio.Event()
        self.updated_symbols: asyncio.Queue[str] = asyncio.Queue(maxsize=update_queue_size)
        self.metrics = BookManagerMetrics()
        self._logger = logging.getLogger(__name__)

    @property
    def books(self) -> Mapping[str, OrderBook]:
        return self._books_view

    @property
    def running(self) -> bool:
        return self._running

    @property
    def stream_urls(self) -> tuple[str, ...]:
        """Combined stream URLs, exposed for diagnostics and dry-run tests."""

        return tuple(self._stream_url(shard) for shard in self._symbol_shards())

    async def __aenter__(self) -> BookManager:
        await self.start()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.stop()

    def get_book(self, symbol: str) -> OrderBook | None:
        return self._books.get(symbol.strip().upper())

    def healthy_book(self, symbol: str, *, now_ms: int | None = None) -> OrderBook | None:
        book = self.get_book(symbol)
        if book is None:
            return None
        return book if book.health(self.stale_after_ms, now_ms=now_ms).healthy else None

    def add_update_listener(self, listener: UpdateListener) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_update_listener(self, listener: UpdateListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    async def next_update(self) -> str:
        """Wait for and return the symbol of the next applied book update."""

        return await self.updated_symbols.get()

    async def start(self) -> None:
        if self._running:
            return
        if self._ws_session is None:
            if aiohttp is None:
                raise RuntimeError("aiohttp is required for Binance WebSocket access")
            self._ws_session = aiohttp.ClientSession()
        self._running = True
        for shard_number, shard in enumerate(self._symbol_shards()):
            task = asyncio.create_task(
                self._run_shard(shard, shard_number),
                name=f"binance-depth-shard-{shard_number}",
            )
            self._shard_tasks.add(task)
            task.add_done_callback(self._shard_tasks.discard)
        await asyncio.sleep(0)

    async def stop(self) -> None:
        if (
            not self._running
            and not self._shard_tasks
            and not self._sync_tasks
            and not self._listener_tasks
        ):
            if self._owns_ws_session and self._ws_session is not None:
                await self._ws_session.close()
                self._ws_session = None
            return

        self._running = False
        tasks = tuple(self._shard_tasks | self._sync_tasks | self._listener_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._shard_tasks.clear()
        self._sync_tasks.clear()
        self._listener_tasks.clear()

        for state in self._states.values():
            state.sync_token += 1
            state.syncing = False
            state.connected = False
            state.buffer.clear()
            state.book.mark_disconnected()
        self._health_changed.set()

        if self._owns_ws_session and self._ws_session is not None:
            await self._ws_session.close()
            self._ws_session = None

    async def wait_until_ready(self, timeout_s: float = 30.0) -> bool:
        """Wait until every requested book is synchronized, healthy, and fresh."""

        if timeout_s < 0:
            raise ValueError("timeout_s must be non-negative")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            self._health_changed.clear()
            if all(
                state.book.health(self.stale_after_ms).healthy for state in self._states.values()
            ):
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self._health_changed.wait(), timeout=remaining)
            except TimeoutError:
                return False

    async def ingest_depth_event(
        self,
        symbol: str,
        event: DepthUpdate | Mapping[str, Any],
        *,
        local_receive_time_ms: int | None = None,
    ) -> BookUpdateResult | None:
        """Ingest one event; useful both for live sockets and deterministic replay."""

        normalized_symbol = _normalize_symbol(symbol)
        try:
            state = self._states[normalized_symbol]
        except KeyError as exc:
            raise KeyError(f"symbol is not managed: {normalized_symbol}") from exc
        update = event if isinstance(event, DepthUpdate) else DepthUpdate.from_message(event)
        if update.symbol is not None and update.symbol != normalized_symbol:
            raise ValueError("event symbol does not match the requested managed symbol")
        received_ms = epoch_ms() if local_receive_time_ms is None else int(local_receive_time_ms)
        schedule_token: int | None = None

        async with state.lock:
            if state.syncing or not state.book.has_snapshot:
                self._buffer_locked(state, update, received_ms)
                return None

            result = state.book.apply_update(update, local_receive_time_ms=received_ms)
            if result.status is UpdateStatus.GAP:
                self.metrics.sequence_gaps += 1
                self._buffer_locked(state, update, received_ms)
                schedule_token = self._begin_resync_locked(state)
            elif result.status is UpdateStatus.CROSSED:
                self.metrics.crossed_updates += 1

        self._health_changed.set()
        if schedule_token is not None:
            self._spawn_sync_task(normalized_symbol, schedule_token, retry_forever=self._running)
        elif result.applied:
            self._publish_update(normalized_symbol, result)
        return result

    async def resync_symbol(self, symbol: str) -> bool:
        """Force and await an isolated symbol snapshot/resynchronization."""

        normalized_symbol = _normalize_symbol(symbol)
        try:
            state = self._states[normalized_symbol]
        except KeyError as exc:
            raise KeyError(f"symbol is not managed: {normalized_symbol}") from exc
        async with state.lock:
            token = self._begin_resync_locked(state)
        return await self._synchronize_symbol(normalized_symbol, token, retry_forever=False)

    def health_snapshot(self, *, now_ms: int | None = None) -> dict[str, dict[str, Any]]:
        return {
            symbol: state.book.health(self.stale_after_ms, now_ms=now_ms).to_dict()
            for symbol, state in self._states.items()
        }

    def metrics_snapshot(self, *, now_ms: int | None = None) -> dict[str, Any]:
        health = {
            symbol: state.book.health(self.stale_after_ms, now_ms=now_ms)
            for symbol, state in self._states.items()
        }
        totals = self.metrics.to_dict()
        totals.update(
            {
                "managed_books": len(self._states),
                "healthy_books": sum(item.healthy for item in health.values()),
                "stale_books": sum(item.stale for item in health.values()),
                "crossed_books": sum(item.crossed for item in health.values()),
                "synchronized_books": sum(item.synchronized for item in health.values()),
                "book_updates_applied": sum(
                    state.book.metrics.updates_applied for state in self._states.values()
                ),
                "book_stale_updates": sum(
                    state.book.metrics.stale_updates for state in self._states.values()
                ),
                "book_sequence_gaps": sum(
                    state.book.metrics.sequence_gaps for state in self._states.values()
                ),
                "book_resyncs": sum(state.book.metrics.resyncs for state in self._states.values()),
            }
        )
        return totals

    async def _run_shard(self, symbols: tuple[str, ...], shard_number: int) -> None:
        url = self._stream_url(symbols)
        reconnect_attempt = 0
        connected_once = False
        while self._running:
            try:
                assert self._ws_session is not None
                async with self._ws_session.ws_connect(
                    url,
                    heartbeat=20.0,
                    autoclose=True,
                    max_msg_size=4 * 1024 * 1024,
                ) as socket:
                    self.metrics.websocket_connections += 1
                    if connected_once:
                        self.metrics.websocket_reconnects += 1
                    connected_once = True
                    reconnect_attempt = 0
                    await self._prepare_connected_shard(symbols)

                    async for message in socket:
                        if not self._running:
                            break
                        if message.type == aiohttp.WSMsgType.TEXT:
                            received_ms = epoch_ms()
                            try:
                                payload = json.loads(message.data)
                                await self._ingest_ws_payload(payload, received_ms)
                            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                                self.metrics.malformed_messages += 1
                                self._logger.debug(
                                    "discarded malformed depth message", exc_info=True
                                )
                        elif message.type in {
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.ERROR,
                        }:
                            break
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.warning(
                    "Binance public depth shard %s disconnected", shard_number, exc_info=True
                )
            finally:
                await self._mark_shard_disconnected(symbols)

            if self._running:
                delay = min(
                    self.reconnect_backoff_cap_s,
                    self.reconnect_backoff_base_s * (2**reconnect_attempt),
                )
                reconnect_attempt += 1
                await asyncio.sleep(delay * random.uniform(0.8, 1.2))

    async def _prepare_connected_shard(self, symbols: tuple[str, ...]) -> None:
        for symbol in symbols:
            state = self._states[symbol]
            async with state.lock:
                state.sync_token += 1
                token = state.sync_token
                state.syncing = True
                state.connected = True
                state.buffer.clear()
                state.book.reset(connected=True)
            self._spawn_sync_task(symbol, token, retry_forever=True)
        self._health_changed.set()

    async def _mark_shard_disconnected(self, symbols: tuple[str, ...]) -> None:
        for symbol in symbols:
            state = self._states[symbol]
            async with state.lock:
                state.sync_token += 1
                state.syncing = False
                state.connected = False
                state.buffer.clear()
                state.book.mark_disconnected()
        self._health_changed.set()

    async def _ingest_ws_payload(self, payload: Any, received_ms: int) -> None:
        self.metrics.websocket_messages += 1
        if not isinstance(payload, Mapping):
            raise ValueError("combined stream payload must be a mapping")
        update = DepthUpdate.from_message(payload)
        symbol = update.symbol
        if symbol is None:
            stream = payload.get("stream")
            if not isinstance(stream, str) or "@" not in stream:
                raise ValueError("depth payload has neither symbol nor stream name")
            symbol = stream.split("@", 1)[0].upper()
        if symbol not in self._states:
            raise KeyError(symbol)
        await self.ingest_depth_event(symbol, update, local_receive_time_ms=received_ms)

    async def _synchronize_symbol(
        self,
        symbol: str,
        token: int,
        *,
        retry_forever: bool,
    ) -> bool:
        state = self._states[symbol]
        attempt = 0
        last_error: BaseException | None = None

        while retry_forever or attempt < self.sync_max_attempts:
            if not await self._token_is_current(state, token):
                return False
            self.metrics.snapshot_requests += 1
            try:
                snapshot = await self.rest_client.depth_snapshot(symbol, limit=self.snapshot_limit)
                snapshot_received_ms = epoch_ms()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.metrics.snapshot_failures += 1
                last_error = exc
                attempt += 1
                if not retry_forever and attempt >= self.sync_max_attempts:
                    break
                await asyncio.sleep(self._sync_backoff(attempt))
                continue

            buffered_results: list[BookUpdateResult] = []
            gap_during_replay = False
            snapshot_error: Exception | None = None
            async with state.lock:
                if token != state.sync_token:
                    return False
                buffered = tuple(state.buffer)
                try:
                    state.book.load_snapshot(
                        snapshot,
                        local_receive_time_ms=snapshot_received_ms,
                    )
                    state.buffer.clear()
                    for index, buffered_event in enumerate(buffered):
                        result = state.book.apply_update(
                            buffered_event.update,
                            local_receive_time_ms=buffered_event.local_receive_time_ms,
                        )
                        if result.status is UpdateStatus.GAP:
                            self.metrics.sequence_gaps += 1
                            for remaining in buffered[index:]:
                                self._buffer_locked(
                                    state,
                                    remaining.update,
                                    remaining.local_receive_time_ms,
                                )
                            state.book.reset(connected=state.connected or not self._running)
                            gap_during_replay = True
                            break
                        if result.status is UpdateStatus.CROSSED:
                            self.metrics.crossed_updates += 1
                        if result.applied:
                            buffered_results.append(result)
                except Exception as exc:
                    # A malformed snapshot or unexpected replay error must not
                    # wedge this symbol in the syncing state. Keep every event
                    # so a subsequent snapshot can bridge the same buffer.
                    snapshot_error = exc
                    state.book.reset(connected=state.connected or not self._running)
                    state.buffer.clear()
                    for buffered_event in buffered:
                        self._buffer_locked(
                            state,
                            buffered_event.update,
                            buffered_event.local_receive_time_ms,
                        )

                if not gap_during_replay and snapshot_error is None:
                    state.syncing = False

            if snapshot_error is not None:
                self.metrics.snapshot_failures += 1
                last_error = snapshot_error
                attempt += 1
                if not retry_forever and attempt >= self.sync_max_attempts:
                    break
                await asyncio.sleep(self._sync_backoff(attempt))
                continue

            if gap_during_replay:
                attempt += 1
                if not retry_forever and attempt >= self.sync_max_attempts:
                    last_error = SnapshotSyncError(
                        f"buffer for {symbol} could not bridge the REST snapshot"
                    )
                    break
                await asyncio.sleep(self._sync_backoff(attempt))
                continue

            self._health_changed.set()
            for result in buffered_results:
                self._publish_update(symbol, result)
            return True

        async with state.lock:
            if token == state.sync_token:
                state.syncing = False
                state.book.reset(connected=state.connected or not self._running)
        self._health_changed.set()
        raise SnapshotSyncError(f"failed to synchronize {symbol}") from last_error

    async def _token_is_current(self, state: _SymbolState, token: int) -> bool:
        async with state.lock:
            return token == state.sync_token and (state.connected or not self._running)

    def _begin_resync_locked(self, state: _SymbolState) -> int:
        state.sync_token += 1
        state.syncing = True
        state.book.mark_resync()
        self.metrics.resyncs += 1
        return state.sync_token

    def _buffer_locked(self, state: _SymbolState, update: DepthUpdate, received_ms: int) -> None:
        if len(state.buffer) >= self.max_buffer_events:
            state.buffer.popleft()
            self.metrics.buffer_overflows += 1
        state.buffer.append(_BufferedUpdate(update, received_ms))

    def _spawn_sync_task(self, symbol: str, token: int, *, retry_forever: bool) -> None:
        task = asyncio.create_task(
            self._synchronize_symbol(symbol, token, retry_forever=retry_forever),
            name=f"binance-book-sync-{symbol}",
        )
        self._sync_tasks.add(task)

        def done(completed: asyncio.Task[Any]) -> None:
            self._sync_tasks.discard(completed)
            if completed.cancelled():
                return
            error = completed.exception()
            if error is not None:
                self._logger.warning(
                    "book synchronization failed",
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(done)

    def _publish_update(self, symbol: str, result: BookUpdateResult) -> None:
        try:
            self.updated_symbols.put_nowait(symbol)
        except asyncio.QueueFull:
            self.metrics.update_notifications_dropped += 1

        book = self._books[symbol]
        for listener in tuple(self._listeners):
            try:
                outcome = listener(symbol, book, result)
                if inspect.isawaitable(outcome):
                    task = asyncio.ensure_future(outcome)
                    self._listener_tasks.add(task)
                    task.add_done_callback(self._listener_done)
            except Exception:
                self.metrics.listener_errors += 1
                self._logger.exception("book update listener failed")

    def _listener_done(self, task: asyncio.Future[Any]) -> None:
        self._listener_tasks.discard(task)
        if task.cancelled():
            return
        if task.exception() is not None:
            self.metrics.listener_errors += 1
            error = task.exception()
            assert error is not None
            self._logger.error(
                "async book update listener failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    def _symbol_shards(self) -> tuple[tuple[str, ...], ...]:
        size = self.max_streams_per_connection
        return tuple(
            self.symbols[index : index + size] for index in range(0, len(self.symbols), size)
        )

    def _stream_url(self, symbols: tuple[str, ...]) -> str:
        streams = "/".join(
            f"{symbol.lower()}@depth@{self.stream_interval_ms}ms" for symbol in symbols
        )
        return f"{self.ws_base_url}/stream?streams={streams}"

    def _sync_backoff(self, attempt: int) -> float:
        return min(2.0, 0.05 * (2 ** min(attempt, 6)))


def _normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if not normalized or not normalized.isalnum():
        raise ValueError("symbol must be a non-empty alphanumeric Binance symbol")
    return normalized


__all__ = [
    "DEFAULT_WS_BASE_URL",
    "BookManager",
    "BookManagerMetrics",
    "SnapshotSyncError",
    "UpdateListener",
]
