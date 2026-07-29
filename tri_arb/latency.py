"""Asynchronous latency-shock rechecks for paper opportunities."""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from .simulator import CycleSimulator, decimal

ZERO = Decimal("0")


def _serialise(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: _serialise(item) for key, item in asdict(value).items()}
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(key): _serialise(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_serialise(item) for item in value]
    return value


def _result_edge(result: Any) -> Decimal:
    value = getattr(result, "net_return", None)
    if value is None and isinstance(result, Mapping):
        value = result.get("net_return", result.get("return_after_depth", result.get("edge", ZERO)))
    return decimal(value, ZERO)


def _result_pnl(result: Any) -> Decimal:
    value = getattr(result, "pnl", None)
    if value is None and isinstance(result, Mapping):
        value = result.get("pnl", result.get("estimated_pnl", ZERO))
    return decimal(value, ZERO)


def _result_executable(result: Any) -> bool:
    value = getattr(result, "fully_executable", None)
    if value is None and isinstance(result, Mapping):
        value = result.get("fully_executable", result.get("executable", True))
    return bool(True if value is None else value)


@dataclass(frozen=True, slots=True)
class LatencyCheck:
    delay_ms: int
    checked_at: datetime
    elapsed_ms: Decimal
    edge: Decimal
    pnl: Decimal
    profitable: bool
    executable: bool
    result: Any

    def to_dict(self) -> dict[str, Any]:
        return _serialise(self)


@dataclass(frozen=True, slots=True)
class LatencyResult:
    signal_id: str
    cycle_id: str
    start_amount: Decimal
    detected_at: datetime
    initial_edge: Decimal
    initial_pnl: Decimal
    initial_profitable: bool
    initial_result: Any
    checks: tuple[LatencyCheck, ...]
    edge_decay: Decimal
    edge_decay_by_bucket: Mapping[int, Decimal]
    disappeared: bool
    disappeared_after_ms: int | None
    lifetime_ms: int
    ghost_arbitrage: bool

    @property
    def ghost(self) -> bool:
        return self.ghost_arbitrage

    def to_dict(self) -> dict[str, Any]:
        record = _serialise(self)
        returns: dict[str, Any] = {}
        pnls: dict[str, Any] = {}
        survivals: dict[str, bool] = {}
        for check in self.checks:
            key = f"{check.delay_ms}ms"
            returns[key] = str(check.edge)
            pnls[key] = str(check.pnl)
            survivals[key] = check.profitable
            # Flat names are convenient in CSV/dataframe workflows.
            record[f"return_after_{key}"] = str(check.edge)
            record[f"profit_after_{key}"] = str(check.pnl)
            record[f"profitable_after_{key}"] = check.profitable
            record[f"elapsed_after_{key}"] = str(check.elapsed_ms)
            record[f"lateness_after_{key}"] = str(check.elapsed_ms - Decimal(check.delay_ms))
        record["return_after_latency"] = returns
        record["pnl_after_latency"] = pnls
        record["profitable_after_latency"] = survivals
        record["ghost_arbitrage_flag"] = self.ghost_arbitrage
        record["signal_lifetime_ms"] = self.lifetime_ms
        return record


BooksProvider = Callable[..., Any | Awaitable[Any]]
ResultCallback = Callable[[LatencyResult], Any | Awaitable[Any]]


class LatencyTracker:
    """Snapshot a signal and revisit it at absolute, configurable deadlines.

    ``track`` waits until the largest bucket and is normally launched via
    :meth:`schedule`, so the hot scanner never blocks.  Rechecks use the current
    live books supplied by ``snapshot_factory`` (or the original manager/mapping).
    """

    def __init__(
        self,
        simulator: CycleSimulator | Callable[..., Any],
        latency_buckets_ms: Sequence[int] = (50, 100, 250, 500, 1000),
        *,
        profitability_threshold: Decimal | str | float = ZERO,
        simulation_mode: str = "depth",
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        buckets = tuple(sorted(set(int(value) for value in latency_buckets_ms)))
        if not buckets or buckets[0] < 0:
            raise ValueError("latency_buckets_ms must contain non-negative delays")
        self.simulator = simulator
        self.latency_buckets_ms = buckets
        self.profitability_threshold = decimal(profitability_threshold)
        self.simulation_mode = simulation_mode
        self._sleep = sleep
        self._tasks: set[asyncio.Task[LatencyResult]] = set()

    async def _books_at(self, provider: BooksProvider | None, fallback: Any, delay_ms: int) -> Any:
        if provider is None:
            return fallback
        try:
            value = provider(delay_ms)
        except TypeError:
            value = provider()
        if inspect.isawaitable(value):
            value = await value
        return value

    async def _evaluate(self, cycle: Any, books: Any, start_amount: Decimal) -> Any:
        evaluator = getattr(self.simulator, "simulate", None)
        if callable(evaluator):
            value = evaluator(cycle, books, start_amount, mode=self.simulation_mode)
        elif callable(self.simulator):
            try:
                value = self.simulator(cycle, books, start_amount, mode=self.simulation_mode)
            except TypeError:
                value = self.simulator(cycle, books, start_amount)
        else:
            raise TypeError("simulator must expose simulate() or be callable")
        if inspect.isawaitable(value):
            value = await value
        return value

    def _profitable(self, result: Any) -> bool:
        return _result_executable(result) and _result_edge(result) > self.profitability_threshold

    async def track(
        self,
        signal_id: str | None,
        cycle: Any,
        books: Any,
        start_amount: Decimal | str | float,
        *,
        initial_result: Any | None = None,
        snapshot_factory: BooksProvider | None = None,
        result_callback: ResultCallback | None = None,
        detected_at: datetime | None = None,
        started_monotonic: float | None = None,
    ) -> LatencyResult:
        """Re-evaluate one signal and derive decay, disappearance, and ghost flags.

        A scanner may pass timestamps captured before its synchronous simulation
        and recording work.  Bucket deadlines then include that scheduling cost,
        instead of restarting the latency clock when this coroutine begins.
        """

        amount = decimal(start_amount)
        effective_detected_at = detected_at or datetime.now(UTC)
        if effective_detected_at.tzinfo is None:
            effective_detected_at = effective_detected_at.replace(tzinfo=UTC)
        else:
            effective_detected_at = effective_detected_at.astimezone(UTC)
        started = time.monotonic() if started_monotonic is None else float(started_monotonic)
        if initial_result is None:
            initial_books = await self._books_at(snapshot_factory, books, 0)
            initial_result = await self._evaluate(cycle, initial_books, amount)
        initial_edge = _result_edge(initial_result)
        initial_pnl = _result_pnl(initial_result)
        initial_profitable = self._profitable(initial_result)
        checks: list[LatencyCheck] = []

        for delay_ms in self.latency_buckets_ms:
            remaining_seconds = max(0.0, delay_ms / 1000 - (time.monotonic() - started))
            if remaining_seconds:
                await self._sleep(remaining_seconds)
            current_books = await self._books_at(snapshot_factory, books, delay_ms)
            result = await self._evaluate(cycle, current_books, amount)
            elapsed_ms = decimal((time.monotonic() - started) * 1000)
            checks.append(
                LatencyCheck(
                    delay_ms=delay_ms,
                    checked_at=datetime.now(UTC),
                    elapsed_ms=elapsed_ms,
                    edge=_result_edge(result),
                    pnl=_result_pnl(result),
                    profitable=self._profitable(result),
                    executable=_result_executable(result),
                    result=result,
                )
            )

        first_dead = next((check for check in checks if not check.profitable), None)
        disappeared = initial_profitable and first_dead is not None
        disappeared_after_ms = first_dead.delay_ms if disappeared else None
        lifetime_ms = (
            disappeared_after_ms if disappeared_after_ms is not None else checks[-1].delay_ms
        )
        edge_decay_by_bucket = {check.delay_ms: initial_edge - check.edge for check in checks}
        final_decay = initial_edge - checks[-1].edge
        # A signal gone by the fastest scheduled observation is indistinguishable
        # from a top-of-book ghost at this infrastructure's resolution.
        ghost = initial_profitable and not checks[0].profitable
        cycle_id = getattr(cycle, "cycle_id", getattr(cycle, "id", "unknown"))
        latency_result = LatencyResult(
            signal_id=signal_id or uuid.uuid4().hex,
            cycle_id=str(cycle_id),
            start_amount=amount,
            detected_at=effective_detected_at,
            initial_edge=initial_edge,
            initial_pnl=initial_pnl,
            initial_profitable=initial_profitable,
            initial_result=initial_result,
            checks=tuple(checks),
            edge_decay=final_decay,
            edge_decay_by_bucket=edge_decay_by_bucket,
            disappeared=disappeared,
            disappeared_after_ms=disappeared_after_ms,
            lifetime_ms=lifetime_ms,
            ghost_arbitrage=ghost,
        )
        if result_callback is not None:
            callback_result = result_callback(latency_result)
            if inspect.isawaitable(callback_result):
                await callback_result
        return latency_result

    def schedule(self, *args: Any, **kwargs: Any) -> asyncio.Task[LatencyResult]:
        """Launch :meth:`track` in the current loop and retain it until done."""

        task = asyncio.create_task(self.track(*args, **kwargs))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self) -> list[LatencyResult]:
        """Wait for all outstanding checks, returning successful results."""

        if not self._tasks:
            return []
        outcomes = await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        return [outcome for outcome in outcomes if isinstance(outcome, LatencyResult)]

    async def close(self, *, cancel: bool = False) -> list[LatencyResult]:
        if cancel:
            for task in tuple(self._tasks):
                task.cancel()
        return await self.drain()


LatencyScheduler = LatencyTracker


__all__ = ["LatencyCheck", "LatencyResult", "LatencyScheduler", "LatencyTracker"]
