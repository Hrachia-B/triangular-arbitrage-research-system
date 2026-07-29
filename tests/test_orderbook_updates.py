from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from decimal import Decimal
from typing import Any

from tri_arb.book_manager import DEFAULT_WS_BASE_URL, BookManager
from tri_arb.orderbook import OrderBook, UpdateStatus


def _snapshot(update_id: int = 100) -> dict[str, Any]:
    return {
        "lastUpdateId": update_id,
        "bids": [["100", "2"], ["99", "3"], ["98", "4"]],
        "asks": [["101", "2"], ["102", "3"], ["103", "4"]],
    }


def _event(
    first_id: int,
    final_id: int,
    *,
    bids: list[list[str]] | None = None,
    asks: list[list[str]] | None = None,
    event_time: int = 1_700_000_000_000,
    pu: int | None = None,
    symbol: str = "BTCUSDT",
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "e": "depthUpdate",
        "E": event_time,
        "s": symbol,
        "U": first_id,
        "u": final_id,
        "b": bids or [],
        "a": asks or [],
    }
    if pu is not None:
        event["pu"] = pu
    return event


def test_snapshot_bridge_stale_drop_decimal_sorting_and_timestamps() -> None:
    book = OrderBook("btcusdt", max_depth=3)
    book.load_snapshot(_snapshot(), local_receive_time_ms=900)

    stale = book.apply_update(
        _event(90, 100, bids=[["100", "999"]]),
        local_receive_time_ms=950,
    )
    assert stale.status is UpdateStatus.STALE
    assert book.best_bid == (Decimal("100"), Decimal("2"))

    bridge = book.apply_update(
        _event(
            99,
            102,
            bids=[["100", "0"], ["100.5", "1.25"]],
            asks=[["101", "0"], ["100.75", "0.5"]],
            event_time=975,
        ),
        local_receive_time_ms=1_000,
    )
    assert bridge.status is UpdateStatus.APPLIED
    assert book.synchronized and book.healthy
    assert book.last_update_id == 102
    assert book.bid_levels() == (
        (Decimal("100.5"), Decimal("1.25")),
        (Decimal("99"), Decimal("3")),
        (Decimal("98"), Decimal("4")),
    )
    assert book.ask_levels() == (
        (Decimal("100.75"), Decimal("0.5")),
        (Decimal("102"), Decimal("3")),
        (Decimal("103"), Decimal("4")),
    )
    assert book.exchange_event_time_ms == 975
    assert book.local_receive_time_ms == 1_000
    assert book.book_update_time_ms == 1_000
    assert book.metrics.stale_updates == 1


def test_first_event_must_bridge_snapshot_and_gap_requires_new_snapshot() -> None:
    book = OrderBook("BTCUSDT")
    book.load_snapshot(_snapshot())

    result = book.apply_update(_event(102, 104))

    assert result.status is UpdateStatus.GAP
    assert not book.synchronized
    assert not book.has_snapshot
    assert book.metrics.sequence_gaps == 1
    try:
        book.apply_update(_event(105, 105))
    except RuntimeError as exc:
        assert "snapshot" in str(exc)
    else:  # pragma: no cover - documents the safety invariant.
        raise AssertionError("a gapped book must reject updates until resnapshotted")


def test_overlapping_spot_batches_are_valid_but_forward_gap_is_not() -> None:
    book = OrderBook("BTCUSDT")
    book.load_snapshot(_snapshot())
    assert book.apply_update(_event(101, 105)).status is UpdateStatus.APPLIED

    overlap = book.apply_update(_event(104, 108, bids=[["100", "5"]]))
    assert overlap.status is UpdateStatus.APPLIED
    assert book.last_update_id == 108

    gap = book.apply_update(_event(110, 111))
    assert gap.status is UpdateStatus.GAP
    assert gap.previous_update_id == 108


def test_optional_pu_is_enforced_after_initial_bridge() -> None:
    book = OrderBook("BTCUSDT")
    book.load_snapshot(_snapshot())
    assert book.apply_update(_event(101, 103, pu=99)).status is UpdateStatus.APPLIED

    mismatch = book.apply_update(_event(104, 104, pu=102))
    assert mismatch.status is UpdateStatus.GAP
    assert "pu=102" in (mismatch.reason or "")


def test_zero_quantity_deletes_levels_and_depth_is_bounded() -> None:
    book = OrderBook("BTCUSDT", max_depth=2)
    book.load_snapshot(_snapshot())
    assert [price for price, _ in book.bid_levels()] == [Decimal("100"), Decimal("99")]
    assert [price for price, _ in book.ask_levels()] == [Decimal("101"), Decimal("102")]

    book.apply_update(
        _event(
            101,
            101,
            bids=[["100", "0"], ["100.25", "7"], ["97", "20"]],
            asks=[["101", "0"], ["100.75", "8"], ["104", "20"]],
        )
    )
    assert book.bid_levels() == (
        (Decimal("100.25"), Decimal("7")),
        (Decimal("99"), Decimal("3")),
    )
    assert book.ask_levels() == (
        (Decimal("100.75"), Decimal("8")),
        (Decimal("102"), Decimal("3")),
    )


def test_cross_and_staleness_health_metrics() -> None:
    book = OrderBook("BTCUSDT")
    book.load_snapshot(_snapshot(), local_receive_time_ms=1_000)
    result = book.apply_update(
        _event(101, 101, bids=[["101", "1"]]),
        local_receive_time_ms=1_100,
    )
    assert result.status is UpdateStatus.CROSSED
    assert book.crossed and not book.healthy
    assert book.health(stale_after_ms=100, now_ms=1_150).stale is False
    assert book.health(stale_after_ms=100, now_ms=1_201).stale is True
    assert book.metrics.crossed_events == 1

    uncross = book.apply_update(
        _event(102, 102, bids=[["101", "0"]]),
        local_receive_time_ms=1_210,
    )
    assert uncross.status is UpdateStatus.APPLIED
    assert book.healthy


class _FakeRestClient:
    def __init__(self, snapshots: dict[str, list[dict[str, Any]]]) -> None:
        self._snapshots = {symbol: deque(values) for symbol, values in snapshots.items()}
        self.calls: defaultdict[str, int] = defaultdict(int)

    async def depth_snapshot(self, symbol: str, limit: int = 1000) -> dict[str, Any]:
        self.calls[symbol] += 1
        values = self._snapshots[symbol]
        if len(values) > 1:
            return values.popleft()
        return values[0]


def test_manager_buffers_before_snapshot_and_replays_in_order() -> None:
    async def scenario() -> None:
        rest = _FakeRestClient({"BTCUSDT": [_snapshot(100)]})
        manager = BookManager(
            ["BTCUSDT"],
            rest,  # type: ignore[arg-type]
            max_depth=5,
            snapshot_limit=5,
        )

        assert await manager.ingest_depth_event("BTCUSDT", _event(95, 100)) is None
        assert (
            await manager.ingest_depth_event("BTCUSDT", _event(101, 101, bids=[["100", "6"]]))
            is None
        )
        assert await manager.resync_symbol("BTCUSDT") is True

        book = manager.get_book("btcusdt")
        assert book is not None
        assert book.synchronized and book.last_update_id == 101
        assert book.best_bid == (Decimal("100"), Decimal("6"))
        assert book.metrics.stale_updates == 1
        assert manager.metrics.resyncs == 1
        assert await manager.next_update() == "BTCUSDT"

    asyncio.run(scenario())


def test_manager_gap_resync_is_per_symbol() -> None:
    async def scenario() -> None:
        rest = _FakeRestClient(
            {
                "BTCUSDT": [_snapshot(100), _snapshot(103)],
                "ETHUSDT": [_snapshot(200)],
            }
        )
        manager = BookManager(
            ["BTCUSDT", "ETHUSDT"],
            rest,  # type: ignore[arg-type]
            max_depth=5,
            snapshot_limit=5,
        )
        await manager.resync_symbol("BTCUSDT")
        await manager.resync_symbol("ETHUSDT")
        await manager.ingest_depth_event("BTCUSDT", _event(101, 101))
        await manager.ingest_depth_event("ETHUSDT", _event(201, 201, symbol="ETHUSDT"))

        gap = await manager.ingest_depth_event("BTCUSDT", _event(103, 103))
        assert gap is not None and gap.status is UpdateStatus.GAP
        for _ in range(20):
            await asyncio.sleep(0)
            if rest.calls["BTCUSDT"] >= 2:
                break
        assert rest.calls["BTCUSDT"] == 2

        btc = manager.get_book("BTCUSDT")
        eth = manager.get_book("ETHUSDT")
        assert btc is not None and eth is not None
        assert eth.synchronized and eth.last_update_id == 201
        assert await manager.ingest_depth_event("BTCUSDT", _event(104, 104)) is not None
        assert btc.synchronized and btc.last_update_id == 104
        assert manager.metrics.sequence_gaps == 1
        assert manager.metrics.resyncs == 3  # two explicit initial syncs plus the gap

    asyncio.run(scenario())


def test_manager_retries_malformed_snapshot_without_losing_buffer() -> None:
    async def scenario() -> None:
        malformed = _snapshot(100)
        malformed["bids"] = [["not-a-price", "2"]]
        rest = _FakeRestClient({"BTCUSDT": [malformed, _snapshot(100)]})
        manager = BookManager(
            ["BTCUSDT"],
            rest,  # type: ignore[arg-type]
            max_depth=5,
            snapshot_limit=5,
            sync_max_attempts=2,
        )

        await manager.ingest_depth_event(
            "BTCUSDT",
            _event(101, 101, bids=[["100", "7"]]),
        )
        assert await manager.resync_symbol("BTCUSDT") is True

        book = manager.get_book("BTCUSDT")
        assert book is not None
        assert book.synchronized and book.last_update_id == 101
        assert book.best_bid == (Decimal("100"), Decimal("7"))
        assert rest.calls["BTCUSDT"] == 2
        assert manager.metrics.snapshot_failures == 1

    asyncio.run(scenario())


def test_manager_stop_cancels_in_flight_async_listeners() -> None:
    async def scenario() -> None:
        rest = _FakeRestClient({"BTCUSDT": [_snapshot(100)]})
        manager = BookManager(
            ["BTCUSDT"],
            rest,  # type: ignore[arg-type]
            max_depth=5,
            snapshot_limit=5,
        )
        started = asyncio.Event()
        cleaned_up = asyncio.Event()

        async def listener(_symbol: str, _book: OrderBook, _result: Any) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned_up.set()

        manager.add_update_listener(listener)
        await manager.resync_symbol("BTCUSDT")
        await manager.ingest_depth_event("BTCUSDT", _event(101, 101))
        await asyncio.wait_for(started.wait(), timeout=1)

        await manager.stop()

        assert cleaned_up.is_set()

    asyncio.run(scenario())


def test_combined_streams_are_split_and_use_market_data_only_default() -> None:
    rest = _FakeRestClient(
        {symbol: [_snapshot()] for symbol in ["AUSDT", "BUSDT", "CUSDT", "DUSDT", "EUSDT"]}
    )
    manager = BookManager(
        ["AUSDT", "BUSDT", "CUSDT", "DUSDT", "EUSDT"],
        rest,  # type: ignore[arg-type]
        max_depth=5,
        snapshot_limit=5,
        max_streams_per_connection=2,
    )

    assert DEFAULT_WS_BASE_URL == "wss://data-stream.binance.vision:443"
    assert len(manager.stream_urls) == 3
    assert all(
        url.startswith(f"{DEFAULT_WS_BASE_URL}/stream?streams=") for url in manager.stream_urls
    )
    assert "ausdt@depth@100ms/busdt@depth@100ms" in manager.stream_urls[0]
