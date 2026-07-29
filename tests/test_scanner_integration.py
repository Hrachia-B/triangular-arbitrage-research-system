import asyncio
import time
from decimal import Decimal

from tri_arb.models import BookSide, CycleLeg, LegSide, TriangularCycle
from tri_arb.scanner import OpportunityScanner


class StaticHealthyBook:
    def __init__(self, bids, asks):
        self._bids = tuple((Decimal(str(price)), Decimal(str(qty))) for price, qty in bids)
        self._asks = tuple((Decimal(str(price)), Decimal(str(qty))) for price, qty in asks)
        now_ms = time.time_ns() // 1_000_000
        self.exchange_event_time_ms = now_ms - 2
        self.local_receive_time_ms = now_ms - 1
        self.book_update_time_ms = now_ms

    def bid_levels(self, limit=None):
        return self._bids if limit is None else self._bids[:limit]

    def ask_levels(self, limit=None):
        return self._asks if limit is None else self._asks[:limit]


class StaticManager:
    def __init__(self, books):
        self.books = books
        self.disabled = set()

    def get_book(self, symbol):
        return self.books.get(symbol)

    def healthy_book(self, symbol):
        return None if symbol in self.disabled else self.books.get(symbol)

    def health_snapshot(self):
        return {symbol: {"healthy": True} for symbol in self.books}

    def metrics_snapshot(self):
        return {"managed_books": len(self.books), "healthy_books": len(self.books)}


class MemoryRecorder:
    def __init__(self):
        self.records = []

    async def record(self, category, record):
        self.records.append((category, record))


def profitable_cycle():
    legs = (
        CycleLeg("USDT", "AAA", "AAAUSDT", "AAA", "USDT", LegSide.BUY_BASE, BookSide.ASKS),
        CycleLeg("AAA", "BBB", "AAABBB", "AAA", "BBB", LegSide.SELL_BASE, BookSide.BIDS),
        CycleLeg("BBB", "USDT", "BBBUSDT", "BBB", "USDT", LegSide.SELL_BASE, BookSide.BIDS),
    )
    return TriangularCycle(
        cycle_id="USDT:AAA:BBB:USDT",
        root_asset="USDT",
        assets=("USDT", "AAA", "BBB", "USDT"),
        symbols=("AAAUSDT", "AAABBB", "BBBUSDT"),
        legs=legs,
        liquidity_score=Decimal("1"),
        spread_score=Decimal("1"),
        feasibility_score=Decimal("1"),
    )


def test_scanner_records_all_realism_stages_and_latency_result():
    async def scenario():
        books = {
            "AAAUSDT": StaticHealthyBook(bids=[("0.99", 1000)], asks=[("1", 1000)]),
            "AAABBB": StaticHealthyBook(bids=[("2", 1000)], asks=[("2.01", 1000)]),
            "BBBUSDT": StaticHealthyBook(bids=[("0.6", 1000)], asks=[("0.61", 1000)]),
        }
        recorder = MemoryRecorder()
        scanner = OpportunityScanner(
            [profitable_cycle()],
            StaticManager(books),
            recorder,
            fee_rate=Decimal("0.001"),
            start_sizes=[Decimal("10")],
            latency_buckets_ms=[0],
            quantity_haircut=Decimal("0.25"),
            extra_slippage_bps=Decimal("1"),
        )

        await scanner.scan_once()
        await scanner.drain_latency_checks()

        categories = [category for category, _ in recorder.records]
        assert categories == [
            "raw_opportunity",
            "signal",
            "after_fees",
            "depth",
            "pessimistic",
            "latency",
        ]
        signal = next(record for category, record in recorder.records if category == "signal")
        latency = next(record for category, record in recorder.records if category == "latency")
        assert Decimal(signal["raw_return"]) > 0
        assert Decimal(signal["return_after_fees"]) > 0
        assert Decimal(signal["return_after_depth"]) > 0
        assert Decimal(signal["pessimistic_return"]) > 0
        assert signal["fully_executable"] is True
        assert latency["signal_id"] == signal["signal_id"]
        assert latency["profitable_after_0ms"] is True
        assert scanner.stats.latency_checks_completed == 1

    asyncio.run(scenario())


def test_raw_top_signal_without_displayed_depth_is_immediate_ghost():
    async def scenario():
        books = {
            "AAAUSDT": StaticHealthyBook(bids=[("0.99", 1)], asks=[("1", 1)]),
            "AAABBB": StaticHealthyBook(bids=[("2", 1000)], asks=[("2.01", 1000)]),
            "BBBUSDT": StaticHealthyBook(bids=[("0.6", 1000)], asks=[("0.61", 1000)]),
        }
        recorder = MemoryRecorder()
        scanner = OpportunityScanner(
            [profitable_cycle()],
            StaticManager(books),
            recorder,
            fee_rate=Decimal("0.001"),
            start_sizes=[Decimal("10")],
            latency_buckets_ms=[0],
            quantity_haircut=Decimal("0.25"),
            extra_slippage_bps=Decimal("1"),
        )

        await scanner.scan_once()
        await scanner.drain_latency_checks()

        signal = next(record for category, record in recorder.records if category == "signal")
        latency = next(record for category, record in recorder.records if category == "latency")
        assert signal["fully_executable"] is False
        assert latency["ghost_arbitrage"] is True
        assert latency["disappeared_after_ms"] == 0
        assert latency["signal_lifetime_ms"] == 0

    asyncio.run(scenario())


def test_latency_recheck_rejects_a_book_that_became_unhealthy():
    async def scenario():
        books = {
            "AAAUSDT": StaticHealthyBook(bids=[("0.99", 1000)], asks=[("1", 1000)]),
            "AAABBB": StaticHealthyBook(bids=[("2", 1000)], asks=[("2.01", 1000)]),
            "BBBUSDT": StaticHealthyBook(bids=[("0.6", 1000)], asks=[("0.61", 1000)]),
        }
        manager = StaticManager(books)
        recorder = MemoryRecorder()
        scanner = OpportunityScanner(
            [profitable_cycle()],
            manager,
            recorder,
            fee_rate=Decimal("0.001"),
            start_sizes=[Decimal("10")],
            latency_buckets_ms=[0],
            quantity_haircut=Decimal("0.25"),
            extra_slippage_bps=Decimal("1"),
        )

        await scanner.scan_once()
        manager.disabled.add("AAABBB")
        await scanner.drain_latency_checks()

        latency = next(record for category, record in recorder.records if category == "latency")
        assert latency["profitable_after_0ms"] is False
        assert latency["ghost_arbitrage"] is True

    asyncio.run(scenario())


def test_scanner_revalidates_health_after_its_positive_cycle_yield():
    async def scenario():
        books = {
            "AAAUSDT": StaticHealthyBook(bids=[("0.99", 1000)], asks=[("1", 1000)]),
            "AAABBB": StaticHealthyBook(bids=[("2", 1000)], asks=[("2.01", 1000)]),
            "BBBUSDT": StaticHealthyBook(bids=[("0.6", 1000)], asks=[("0.61", 1000)]),
        }
        manager = StaticManager(books)
        recorder = MemoryRecorder()
        scanner = OpportunityScanner(
            [profitable_cycle()],
            manager,
            recorder,
            fee_rate=Decimal("0.001"),
            start_sizes=[Decimal("10")],
            latency_buckets_ms=[0],
            quantity_haircut=Decimal("0.25"),
            extra_slippage_bps=Decimal("1"),
        )

        async def disconnect_before_size_simulation():
            manager.disabled.add("AAABBB")

        disconnect = asyncio.create_task(disconnect_before_size_simulation())
        await scanner.scan_once()
        await disconnect

        assert recorder.records == []
        assert scanner.stats.skipped_unhealthy_cycle_sizes == 1

    asyncio.run(scenario())


def test_raw_theoretical_edge_is_retained_when_exchange_filters_reject_it():
    async def scenario():
        books = {
            "AAAUSDT": StaticHealthyBook(bids=[("0.99", 1000)], asks=[("1", 1000)]),
            "AAABBB": StaticHealthyBook(bids=[("2", 1000)], asks=[("2.01", 1000)]),
            "BBBUSDT": StaticHealthyBook(bids=[("0.6", 1000)], asks=[("0.61", 1000)]),
        }
        recorder = MemoryRecorder()
        scanner = OpportunityScanner(
            [profitable_cycle()],
            StaticManager(books),
            recorder,
            fee_rate=Decimal("0.001"),
            start_sizes=[Decimal("10")],
            latency_buckets_ms=[0],
            quantity_haircut=Decimal("0.25"),
            extra_slippage_bps=Decimal("1"),
            symbol_filters={
                "AAAUSDT": [
                    {
                        "filterType": "MIN_NOTIONAL",
                        "minNotional": "20",
                    }
                ]
            },
        )

        await scanner.scan_once()
        await scanner.drain_latency_checks()

        raw = next(record for category, record in recorder.records if category == "raw_opportunity")
        signal = next(record for category, record in recorder.records if category == "signal")
        assert Decimal(raw["raw_return"]) > 0
        assert signal["filter_rejected"] is True
        assert signal["fully_executable"] is False
        assert signal["estimated_pnl"] is None

    asyncio.run(scenario())
