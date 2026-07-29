"""Hot-loop opportunity scanning and latency follow-up orchestration.

The scanner has no network or order capabilities.  It reads synchronized books
from a manager, invokes the pure paper simulator, and sends serializable records
to the recorder.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from tri_arb.latency import LatencyTracker
from tri_arb.simulator import CycleSimulator, SimulationResult
from tri_arb.utils import iso_utc, jsonable

LOGGER = logging.getLogger(__name__)
ZERO = Decimal("0")
TEN_THOUSAND = Decimal("10000")


@dataclass(slots=True)
class ScanStats:
    """Counters for one observer run."""

    scans: int = 0
    cycle_size_evaluations: int = 0
    skipped_unhealthy_cycles: int = 0
    skipped_unhealthy_cycle_sizes: int = 0
    raw_opportunity_observations: int = 0
    unique_signals: int = 0
    profitable_after_fees: int = 0
    profitable_after_depth: int = 0
    profitable_pessimistic: int = 0
    latency_checks_completed: int = 0
    latency_checks_skipped: int = 0
    scanner_errors: int = 0
    scan_deadline_misses: int = 0
    last_scan_time_ms: float = 0.0
    maximum_scan_time_ms: float = 0.0
    total_scan_time_ms: float = 0.0

    def to_dict(self) -> dict[str, int | float]:
        return asdict(self)


class OpportunityScanner:
    """Continuously evaluate selected cycles against synchronized local books."""

    def __init__(
        self,
        cycles: Iterable[Any],
        book_manager: Any,
        recorder: Any,
        *,
        fee_rate: Decimal,
        start_sizes: Iterable[Decimal],
        latency_buckets_ms: Iterable[int],
        quantity_haircut: Decimal,
        extra_slippage_bps: Decimal,
        scan_interval_ms: int = 25,
        signal_cooldown_ms: int = 250,
        profit_threshold_bps: Decimal = ZERO,
        symbol_filters: Mapping[str, Any] | None = None,
        symbol_fee_rates: Mapping[str, Decimal] | None = None,
        health_record_interval_seconds: float = 5.0,
        max_pending_latency_checks: int = 1_000,
    ) -> None:
        self.cycles = tuple(cycles)
        self.book_manager = book_manager
        self.recorder = recorder
        self.start_sizes = tuple(Decimal(str(size)) for size in start_sizes)
        self.scan_interval_seconds = scan_interval_ms / 1_000
        self.signal_cooldown_seconds = signal_cooldown_ms / 1_000
        self.profit_threshold = Decimal(str(profit_threshold_bps)) / TEN_THOUSAND
        self.health_record_interval_seconds = health_record_interval_seconds
        self.max_pending_latency_checks = max_pending_latency_checks

        filters = {str(key).upper(): value for key, value in (symbol_filters or {}).items()}
        # Level 1 is a deliberately theoretical, no-fee price discrepancy.
        # Exchange filters belong to the executable stages below; retaining the
        # raw signal lets the report quantify filter/min-notional ghosts.
        self.raw_simulator = CycleSimulator(ZERO)
        self.simulator = CycleSimulator(
            fee_rate,
            symbol_filters=filters,
            symbol_fee_rates=symbol_fee_rates,
        )
        self.pessimistic_simulator = CycleSimulator(
            fee_rate,
            quantity_haircut=quantity_haircut,
            extra_slippage_bps=extra_slippage_bps,
            symbol_filters=filters,
            symbol_fee_rates=symbol_fee_rates,
        )
        self.latency_tracker = LatencyTracker(
            self.simulator,
            latency_buckets_ms=tuple(latency_buckets_ms),
            profitability_threshold=self.profit_threshold,
        )
        self.stats = ScanStats()
        self._last_signal_at: dict[tuple[str, str], float] = {}
        self._latency_tasks: set[asyncio.Task[None]] = set()
        self._last_health_record_at = 0.0

    async def _record(self, category: str, record: Mapping[str, Any]) -> None:
        method = getattr(self.recorder, "record", None)
        if not callable(method):
            raise TypeError("recorder must provide record(category, record)")
        result = method(category, jsonable(dict(record)))
        if inspect.isawaitable(result):
            await result

    def _get_book(self, symbol: str) -> Any | None:
        getter = getattr(self.book_manager, "get_book", None)
        if callable(getter):
            return getter(symbol)
        books = getattr(self.book_manager, "books", None)
        if isinstance(books, Mapping):
            return books.get(symbol)
        if isinstance(self.book_manager, Mapping):
            return self.book_manager.get(symbol)
        return None

    def _is_healthy(self, symbol: str, book: Any) -> bool:
        checker = getattr(self.book_manager, "healthy_book", None)
        if callable(checker):
            try:
                return bool(checker(symbol))
            except (KeyError, TypeError):
                return False
        for name in ("healthy", "is_healthy", "synchronized", "is_synchronized"):
            value = getattr(book, name, None)
            if callable(value):
                try:
                    value = value()
                except TypeError:
                    continue
            if value is not None and not bool(value):
                return False
        stale = getattr(book, "is_stale", None)
        if callable(stale):
            try:
                if stale():
                    return False
            except TypeError:
                # A manager-level staleness policy is preferred when the book
                # method requires an explicit threshold.
                pass
        return True

    def _books_for_cycle(self, cycle: Any) -> dict[str, Any] | None:
        symbols = tuple(getattr(cycle, "symbols", ())) or tuple(
            getattr(leg, "symbol", "") for leg in getattr(cycle, "legs", ())
        )
        result: dict[str, Any] = {}
        for symbol in symbols:
            book = self._get_book(symbol)
            if book is None or not self._is_healthy(symbol, book):
                return None
            result[symbol] = book
        return result

    @staticmethod
    def _top_price(book: Any, side: str) -> Decimal | None:
        attribute = getattr(book, "best_bid" if side == "bids" else "best_ask", None)
        level = attribute() if callable(attribute) else attribute
        if level is None:
            method = getattr(book, "bid_levels" if side == "bids" else "ask_levels", None)
            if not callable(method):
                return None
            levels = method(1)
            level = levels[0] if levels else None
        if level is None:
            return None
        try:
            price = Decimal(str(level[0]))
        except (ArithmeticError, IndexError, TypeError, ValueError):
            return None
        return price if price.is_finite() and price > ZERO else None

    def _theoretical_multiplier(
        self,
        cycle: Any,
        books: Mapping[str, Any],
        top_cache: dict[tuple[str, str], Decimal | None],
    ) -> Decimal | None:
        """Fast Level-1 screen without depth walks or capacity estimation."""

        multiplier = Decimal("1")
        for leg in getattr(cycle, "legs", ()):
            side_value = getattr(leg, "side", "")
            operation = str(getattr(side_value, "value", side_value)).upper()
            side = "bids" if operation == "SELL_BASE" else "asks"
            symbol = str(getattr(leg, "symbol", ""))
            cache_key = (symbol, side)
            if cache_key not in top_cache:
                top_cache[cache_key] = self._top_price(books[symbol], side)
            price = top_cache[cache_key]
            if price is None:
                return None
            if operation == "SELL_BASE":
                multiplier *= price
            elif operation == "BUY_BASE":
                multiplier /= price
            else:
                return None
        return multiplier

    @staticmethod
    def _book_timing(books: Mapping[str, Any]) -> dict[str, Any]:
        now_ms = time.time_ns() // 1_000_000
        exchange_times: list[int] = []
        local_times: list[int] = []
        per_symbol: dict[str, dict[str, int | None]] = {}
        for symbol, book in books.items():
            exchange = getattr(book, "exchange_event_time_ms", None)
            local_receive = getattr(book, "local_receive_time_ms", None)
            local_update = getattr(book, "local_book_update_time_ms", None)
            if local_update is None:
                local_update = getattr(book, "local_update_time_ms", None)
            if local_update is None:
                local_update = getattr(book, "book_update_time_ms", None)
            if isinstance(exchange, int):
                exchange_times.append(exchange)
            newest_local = local_update if isinstance(local_update, int) else local_receive
            if isinstance(newest_local, int):
                local_times.append(newest_local)
            per_symbol[symbol] = {
                "exchange_event_time_ms": exchange if isinstance(exchange, int) else None,
                "local_receive_time_ms": local_receive if isinstance(local_receive, int) else None,
                "local_book_update_time_ms": local_update
                if isinstance(local_update, int)
                else None,
                "staleness_ms": max(0, now_ms - newest_local)
                if isinstance(newest_local, int)
                else None,
            }
        ages = [max(0, now_ms - value) for value in local_times]
        return {
            "timestamp_exchange_ms": max(exchange_times) if exchange_times else None,
            "book_staleness_ms": max(ages) if ages else None,
            "average_book_staleness_ms": sum(ages) / len(ages) if ages else None,
            "book_timing": per_symbol,
        }

    @staticmethod
    def _result_record(result: SimulationResult) -> dict[str, Any]:
        return result.to_dict()

    async def scan_once(self) -> None:
        """Evaluate every configured cycle/size once."""

        scan_started = time.perf_counter()
        self.stats.scans += 1
        try:
            for cycle_index, cycle in enumerate(self.cycles):
                if cycle_index and cycle_index % 10 == 0:
                    # Do not starve WebSocket readers or latency tasks during a
                    # full-universe negative scan.
                    await asyncio.sleep(0)
                books = self._books_for_cycle(cycle)
                self.stats.cycle_size_evaluations += len(self.start_sizes)
                if books is None:
                    self.stats.skipped_unhealthy_cycles += 1
                    continue
                try:
                    # Keep the cache local to this cycle. The scanner yields
                    # between cycles/sizes, so a scan-wide cache could outlive
                    # one or more WebSocket book updates.
                    multiplier = self._theoretical_multiplier(cycle, books, {})
                except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
                    self.stats.scanner_errors += 1
                    LOGGER.warning("cycle %s top screen failed: %s", getattr(cycle, "id", "?"), exc)
                    continue
                if multiplier is None or multiplier - Decimal("1") <= self.profit_threshold:
                    continue

                for start_size in self.start_sizes:
                    # Bound event-loop monopolization during a burst where many
                    # cycles and sizes pass the cheap Level-1 screen.
                    await asyncio.sleep(0)
                    refreshed_books = self._books_for_cycle(cycle)
                    if refreshed_books is None:
                        # A gap/disconnect can occur at any yield point. Never
                        # simulate against levels retained by a newly unhealthy
                        # mutable book.
                        self.stats.skipped_unhealthy_cycle_sizes += 1
                        continue
                    books = refreshed_books
                    timing = self._book_timing(books)
                    detected_monotonic = time.monotonic()
                    detected_at = datetime.now(UTC)
                    try:
                        raw = self.raw_simulator.simulate(cycle, books, start_size, mode="top")
                    except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
                        self.stats.scanner_errors += 1
                        LOGGER.warning(
                            "cycle %s simulation failed: %s", getattr(cycle, "id", "?"), exc
                        )
                        continue
                    if not raw.fully_executable or raw.net_return <= self.profit_threshold:
                        continue

                    self.stats.raw_opportunity_observations += 1
                    cycle_id = str(getattr(cycle, "id", getattr(cycle, "cycle_id", "unknown")))
                    key = (cycle_id, str(start_size))
                    signal_id = f"{cycle_id}:{start_size}:{uuid.uuid4().hex[:12]}"
                    raw_record = {
                        "record_type": "raw_opportunity",
                        "signal_id": signal_id,
                        "timestamp_local": iso_utc(detected_at),
                        "cycle_id": cycle_id,
                        "route": getattr(cycle, "route", raw.route),
                        "start_asset": raw.start_asset,
                        "start_size": str(start_size),
                        "raw_return": str(raw.net_return),
                        "raw_pnl": None if raw.pnl is None else str(raw.pnl),
                        **timing,
                    }

                    last_signal = self._last_signal_at.get(key, float("-inf"))
                    if detected_monotonic - last_signal < self.signal_cooldown_seconds:
                        await self._record("raw_opportunity", raw_record)
                        continue
                    self._last_signal_at[key] = detected_monotonic

                    fee_result = self.simulator.simulate(cycle, books, start_size, mode="top")
                    depth_result = self.simulator.simulate(cycle, books, start_size, mode="depth")
                    pessimistic_result = self.pessimistic_simulator.simulate(
                        cycle, books, start_size, mode="pessimistic"
                    )
                    self.stats.unique_signals += 1
                    self.stats.profitable_after_fees += int(fee_result.profitable)
                    self.stats.profitable_after_depth += int(depth_result.profitable)
                    self.stats.profitable_pessimistic += int(pessimistic_result.profitable)

                    signal_record = {
                        "record_type": "signal",
                        "signal_id": signal_id,
                        "timestamp_local": raw_record["timestamp_local"],
                        "timestamp_exchange": timing["timestamp_exchange_ms"],
                        "cycle_id": cycle_id,
                        "route": getattr(cycle, "route", " -> ".join(raw.route)),
                        "start_asset": raw.start_asset,
                        "start_size": str(start_size),
                        "raw_return": str(raw.net_return),
                        "return_after_fees": str(fee_result.net_return),
                        "return_after_depth": str(depth_result.net_return),
                        "pessimistic_return": str(pessimistic_result.net_return),
                        "estimated_pnl": (
                            None if depth_result.pnl is None else str(depth_result.pnl)
                        ),
                        "pessimistic_pnl": (
                            None if pessimistic_result.pnl is None else str(pessimistic_result.pnl)
                        ),
                        "max_executable_size": str(depth_result.max_executable_size),
                        "limiting_leg": depth_result.limiting_leg,
                        "estimated_slippage": str(depth_result.slippage),
                        "fully_executable": depth_result.fully_executable,
                        "filter_rejected": depth_result.filter_rejected,
                        "rejection_reasons": list(depth_result.rejection_reasons),
                        "profitable_after_fees": fee_result.profitable,
                        "profitable_after_depth": depth_result.profitable,
                        "profitable_pessimistic": pessimistic_result.profitable,
                        "ghost_arbitrage": None,
                        "signal_lifetime_ms": None,
                        "latency_checks": {},
                        **timing,
                        "raw_simulation": self._result_record(raw),
                        "fee_simulation": self._result_record(fee_result),
                        "depth_simulation": self._result_record(depth_result),
                        "pessimistic_simulation": self._result_record(pessimistic_result),
                    }
                    latency_scheduled = self._schedule_latency(
                        signal_id,
                        cycle,
                        books,
                        start_size,
                        depth_result,
                        signal_record,
                        detected_at=detected_at,
                        detected_monotonic=detected_monotonic,
                    )
                    signal_record["latency_scheduled"] = latency_scheduled
                    await self._record("raw_opportunity", raw_record)
                    await self._record("signal", signal_record)
                    if fee_result.profitable:
                        await self._record("after_fees", signal_record)
                    if depth_result.profitable:
                        await self._record("depth", signal_record)
                    await self._record("pessimistic", signal_record)
        finally:
            elapsed_ms = (time.perf_counter() - scan_started) * 1_000
            self.stats.last_scan_time_ms = elapsed_ms
            self.stats.total_scan_time_ms += elapsed_ms
            self.stats.maximum_scan_time_ms = max(self.stats.maximum_scan_time_ms, elapsed_ms)
            if elapsed_ms > self.scan_interval_seconds * 1_000:
                self.stats.scan_deadline_misses += 1

    def _schedule_latency(
        self,
        signal_id: str,
        cycle: Any,
        books: Mapping[str, Any],
        start_size: Decimal,
        initial: SimulationResult,
        signal_record: Mapping[str, Any],
        *,
        detected_at: datetime,
        detected_monotonic: float,
    ) -> bool:
        if len(self._latency_tasks) >= self.max_pending_latency_checks:
            self.stats.latency_checks_skipped += 1
            LOGGER.warning("latency backlog limit reached; skipping signal %s", signal_id)
            return False
        task = asyncio.create_task(
            self._track_latency(
                signal_id,
                cycle,
                books,
                start_size,
                initial,
                signal_record,
                detected_at=detected_at,
                detected_monotonic=detected_monotonic,
            ),
            name=f"latency:{signal_id}",
        )
        self._latency_tasks.add(task)
        task.add_done_callback(self._latency_done)
        return True

    def _latency_done(self, task: asyncio.Task[None]) -> None:
        self._latency_tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            self.stats.scanner_errors += 1
            LOGGER.error(
                "latency recheck failed",
                exc_info=(type(exception), exception, exception.__traceback__),
            )

    async def _track_latency(
        self,
        signal_id: str,
        cycle: Any,
        books: Mapping[str, Any],
        start_size: Decimal,
        initial: SimulationResult,
        signal_record: Mapping[str, Any],
        *,
        detected_at: datetime,
        detected_monotonic: float,
    ) -> None:
        result = await self.latency_tracker.track(
            signal_id,
            cycle,
            books,
            start_size,
            initial_result=initial,
            snapshot_factory=lambda _delay: self._books_for_cycle(cycle) or {},
            detected_at=detected_at,
            started_monotonic=detected_monotonic,
        )
        payload = result.to_dict()
        # The scanner is triggered by a positive no-fee top estimate.  If that
        # estimate is already non-executable after fees/depth, it is a ghost at
        # detection time rather than a durable signal with a full-bucket life.
        if not initial.profitable:
            payload.update(
                {
                    "disappeared": True,
                    "disappeared_after_ms": 0,
                    "lifetime_ms": 0,
                    "signal_lifetime_ms": 0,
                    "ghost_arbitrage": True,
                    "ghost_arbitrage_flag": True,
                }
            )
        payload.update(
            {
                "record_type": "latency",
                "cycle_id": signal_record["cycle_id"],
                "route": signal_record["route"],
                "start_asset": signal_record["start_asset"],
                "start_size": signal_record["start_size"],
                **self._book_timing(books),
            }
        )
        await self._record("latency", payload)
        self.stats.latency_checks_completed += 1

    async def _record_health(self) -> None:
        health_method = getattr(self.book_manager, "health_snapshot", None)
        metrics_method = getattr(self.book_manager, "metrics_snapshot", None)
        health = health_method() if callable(health_method) else {}
        metrics = metrics_method() if callable(metrics_method) else {}
        await self._record(
            "health",
            {
                "record_type": "order_book_health",
                "timestamp_local": iso_utc(),
                "health": health,
                "metrics": metrics,
            },
        )

    async def run(
        self, duration_seconds: float, stop_event: asyncio.Event | None = None
    ) -> ScanStats:
        """Scan until duration elapses or ``stop_event`` is set."""

        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        stop = stop_event or asyncio.Event()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + duration_seconds
        self._last_health_record_at = loop.time() - self.health_record_interval_seconds
        try:
            while not stop.is_set() and loop.time() < deadline:
                iteration_started = loop.time()
                await self.scan_once()
                if loop.time() - self._last_health_record_at >= self.health_record_interval_seconds:
                    await self._record_health()
                    self._last_health_record_at = loop.time()
                remaining = min(
                    max(0.0, self.scan_interval_seconds - (loop.time() - iteration_started)),
                    max(0.0, deadline - loop.time()),
                )
                if remaining <= 0:
                    await asyncio.sleep(0)
                    continue
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=remaining)
        except BaseException:
            await self.cancel_latency_checks()
            raise
        else:
            await self.drain_latency_checks()
            await self._record_health()
        return self.stats

    async def cancel_latency_checks(self) -> None:
        """Cancel and collect every outstanding follow-up task."""

        tasks = tuple(self._latency_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def drain_latency_checks(self) -> None:
        """Let scheduled rechecks finish, then cancel only genuine stragglers."""

        if not self._latency_tasks:
            return
        max_bucket = max(getattr(self.latency_tracker, "latency_buckets_ms", (0,)), default=0)
        timeout = max_bucket / 1_000 + 2.0
        tasks = tuple(self._latency_tasks)
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                exception = task.exception()
                assert exception is not None
                LOGGER.error(
                    "latency task ended with an error",
                    exc_info=(type(exception), exception, exception.__traceback__),
                )


LiveScanner = OpportunityScanner


__all__ = ["LiveScanner", "OpportunityScanner", "ScanStats"]
