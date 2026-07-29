from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from decimal import Decimal
from typing import Any

from tri_arb.exchange import NormalizedDepthUpdate, NormalizedOrderBookSnapshot
from tri_arb.mexc_book_manager import (
    DEFAULT_MEXC_WS_BASE_URL,
    MEXC_MAX_SUBSCRIPTIONS_PER_CONNECTION,
    MexcBookManager,
)
from tri_arb.mexc_orderbook import MexcOrderBook
from tri_arb.orderbook import UpdateStatus


def _snapshot(version: int = 100) -> dict[str, Any]:
    return {
        "version": str(version),
        "bids": [["100", "2"], ["99", "3"]],
        "asks": [["101", "2"], ["102", "3"]],
    }


def _update(
    from_version: int,
    to_version: int,
    *,
    bids: list[Any] | None = None,
    asks: list[Any] | None = None,
    symbol: str = "BTCUSDT",
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "fromVersion": str(from_version),
        "toVersion": str(to_version),
        "bids": bids or [],
        "asks": asks or [],
        "sendTime": 1_700_000_000_000,
    }


def test_mexc_strict_bridge_stale_updates_and_absolute_quantities() -> None:
    book = MexcOrderBook("btcusdt", max_depth=5)
    book.load_snapshot(_snapshot(), local_receive_time_ms=900)

    stale = book.apply_update(
        _update(90, 100, bids=[{"price": "100", "quantity": "999"}]),
        local_receive_time_ms=950,
    )
    assert stale.status is UpdateStatus.STALE
    assert book.best_bid == (Decimal("100"), Decimal("2"))

    bridge = book.apply_update(
        _update(
            101,
            102,
            bids=[{"price": "100", "quantity": "7"}],
            asks=[{"price": "101", "quantity": "0"}],
        ),
        local_receive_time_ms=1_000,
    )
    assert bridge.status is UpdateStatus.APPLIED
    assert book.synchronized and book.last_update_id == 102
    # MEXC quantities replace the displayed size; they are not added as deltas.
    assert book.best_bid == (Decimal("100"), Decimal("7"))
    assert book.best_ask == (Decimal("102"), Decimal("3"))

    deleted = book.apply_update(
        _update(103, 103, bids=[["100", "0"]]),
        local_receive_time_ms=1_010,
    )
    assert deleted.status is UpdateStatus.APPLIED
    assert book.best_bid == (Decimal("99"), Decimal("3"))
    assert book.metrics.stale_updates == 1


def test_mexc_snapshot_bridge_can_span_next_version_then_requires_exact_contiguity() -> None:
    book = MexcOrderBook("BTCUSDT")
    book.load_snapshot(_snapshot(100))

    bridge = book.apply_update(_update(100, 101))
    assert bridge.status is UpdateStatus.APPLIED
    assert book.synchronized and book.last_update_id == 101

    overlap = book.apply_update(_update(101, 102))
    assert overlap.status is UpdateStatus.GAP
    assert not book.synchronized
    assert not book.has_snapshot
    assert book.metrics.sequence_gaps == 1


def test_mexc_forward_jump_from_snapshot_is_a_gap() -> None:
    book = MexcOrderBook("BTCUSDT")
    book.load_snapshot(_snapshot(100))

    result = book.apply_update(_update(102, 103))

    assert result.status is UpdateStatus.GAP
    assert not book.synchronized
    assert not book.has_snapshot


class _FakeMexcAdapter:
    def __init__(self, snapshots: dict[str, list[NormalizedOrderBookSnapshot]]) -> None:
        self.snapshots = {symbol: deque(values) for symbol, values in snapshots.items()}
        self.calls: defaultdict[str, int] = defaultdict(int)

    async def get_depth_snapshot(self, symbol: str, limit: int) -> NormalizedOrderBookSnapshot:
        del limit
        self.calls[symbol] += 1
        values = self.snapshots[symbol]
        if len(values) > 1:
            return values.popleft()
        return values[0]

    async def subscribe_depth(self, symbols: tuple[str, ...]):
        del symbols
        if False:  # pragma: no cover - makes this a deterministic async generator.
            yield None


class _StreamingMexcAdapter(_FakeMexcAdapter):
    def __init__(self, snapshots: dict[str, list[NormalizedOrderBookSnapshot]]) -> None:
        super().__init__(snapshots)
        self.release = asyncio.Event()

    async def subscribe_depth(self, symbols: tuple[str, ...]):
        assert symbols == ("BTCUSDT",)
        yield _normalized_update(101, 101)
        await self.release.wait()


def _normalized_snapshot(version: int) -> NormalizedOrderBookSnapshot:
    return NormalizedOrderBookSnapshot(
        exchange="MEXC Spot",
        symbol="BTCUSDT",
        last_update_id=version,
        bids=((Decimal("100"), Decimal("2")),),
        asks=((Decimal("101"), Decimal("2")),),
    )


def _normalized_update(
    from_version: int,
    to_version: int,
    *,
    bids: tuple[tuple[Decimal, Decimal], ...] = (),
    asks: tuple[tuple[Decimal, Decimal], ...] = (),
) -> NormalizedDepthUpdate:
    return NormalizedDepthUpdate(
        exchange="MEXC Spot",
        symbol="BTCUSDT",
        first_update_id=from_version,
        final_update_id=to_version,
        bids=bids,
        asks=asks,
    )


def test_mexc_manager_gap_triggers_rest_resync_and_replay() -> None:
    async def scenario() -> None:
        adapter = _FakeMexcAdapter(
            {"BTCUSDT": [_normalized_snapshot(100), _normalized_snapshot(102)]}
        )
        manager = MexcBookManager(
            ["BTCUSDT"],
            adapter,
            max_depth=5,
            snapshot_limit=5,
            reconnect_backoff_base_s=0,
        )

        first = _normalized_update(
            101,
            101,
            bids=((Decimal("100"), Decimal("5")),),
        )
        assert await manager.ingest_depth_event("BTCUSDT", first) is None
        assert await manager.resync_symbol("BTCUSDT") is True
        book = manager.get_book("BTCUSDT")
        assert isinstance(book, MexcOrderBook)
        assert book.synchronized and book.last_update_id == 101
        assert book.best_bid == (Decimal("100"), Decimal("5"))

        stale = await manager.ingest_depth_event("BTCUSDT", _normalized_update(100, 101))
        assert stale is not None and stale.status is UpdateStatus.STALE
        assert adapter.calls["BTCUSDT"] == 1

        gap = await manager.ingest_depth_event(
            "BTCUSDT",
            _normalized_update(
                103,
                103,
                asks=((Decimal("101"), Decimal("4")),),
            ),
        )
        assert gap is not None and gap.status is UpdateStatus.GAP
        for _ in range(100):
            await asyncio.sleep(0)
            if adapter.calls["BTCUSDT"] >= 2 and book.last_update_id == 103:
                break

        assert adapter.calls["BTCUSDT"] == 2
        assert book.synchronized and book.last_update_id == 103
        assert book.best_ask == (Decimal("101"), Decimal("4"))
        assert manager.metrics.sequence_gaps == 1
        assert manager.metrics.resyncs == 2
        await manager.stop()

    asyncio.run(scenario())


def test_mexc_manager_consumes_adapter_subscription() -> None:
    async def scenario() -> None:
        adapter = _StreamingMexcAdapter({"BTCUSDT": [_normalized_snapshot(100)]})
        manager = MexcBookManager(
            ["BTCUSDT"],
            adapter,
            max_depth=5,
            snapshot_limit=5,
        )

        await manager.start()
        try:
            assert await manager.wait_until_ready(timeout_s=1)
            book = manager.get_book("BTCUSDT")
            assert book is not None and book.last_update_id == 101
            assert manager.metrics.websocket_messages == 1
            assert adapter.calls["BTCUSDT"] == 1
        finally:
            adapter.release.set()
            await manager.stop()

    asyncio.run(scenario())


def test_mexc_manager_caps_shards_and_exposes_protobuf_channels() -> None:
    symbols = [f"A{index}USDT" for index in range(31)]
    adapter = _FakeMexcAdapter({symbol: [_normalized_snapshot(1)] for symbol in symbols})
    manager = MexcBookManager(
        symbols,
        adapter,
        snapshot_limit=5,
        max_depth=5,
        max_streams_per_connection=50,
        stream_interval_ms=10,
    )

    assert DEFAULT_MEXC_WS_BASE_URL == "wss://wbs-api.mexc.com/ws"
    assert manager.max_streams_per_connection == MEXC_MAX_SUBSCRIPTIONS_PER_CONNECTION
    assert len(manager.stream_urls) == 2
    assert len(manager.subscription_channels[0]) == 30
    assert manager.subscription_channels[0][0] == "spot@public.aggre.depth.v3.api.pb@10ms@A0USDT"
