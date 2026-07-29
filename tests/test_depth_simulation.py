from dataclasses import dataclass
from decimal import Decimal

from tri_arb.models import BookSide, CycleLeg, LegSide
from tri_arb.simulator import CycleSimulator


class StaticBook:
    def __init__(self, bids=(), asks=()):
        self.bids = {Decimal(str(price)): Decimal(str(qty)) for price, qty in bids}
        self.asks = {Decimal(str(price)): Decimal(str(qty)) for price, qty in asks}


@dataclass(frozen=True)
class LooseCycle:
    cycle_id: str
    assets: tuple[str, ...]
    legs: tuple[CycleLeg, ...]


def sell_leg():
    return CycleLeg("AAA", "USDT", "AAAUSDT", "AAA", "USDT", LegSide.SELL_BASE, BookSide.BIDS)


def buy_leg():
    return CycleLeg("USDT", "AAA", "AAAUSDT", "AAA", "USDT", LegSide.BUY_BASE, BookSide.ASKS)


def test_sell_walks_multiple_bid_levels_with_weighted_price():
    book = StaticBook(bids=[(10, 1), (9, 5)], asks=[(11, 10)])
    result = CycleSimulator(fee_rate=0).simulate_leg(sell_leg(), book, Decimal("3"))

    assert result.executable
    assert result.output_after_fee == Decimal("28")
    assert result.average_price == Decimal("28") / Decimal("3")
    assert [level.consumed_quantity for level in result.consumed_levels] == [
        Decimal("1"),
        Decimal("2"),
    ]
    assert result.slippage == Decimal("1") - (Decimal("28") / Decimal("3")) / Decimal("10")


def test_buy_walks_asks_and_reports_unspent_rounding_dust():
    filters = {"AAAUSDT": {"min_qty": "0", "step_size": "0.1", "min_notional": "0"}}
    book = StaticBook(bids=[(9, 10)], asks=[(10, 5), (11, 10)])
    result = CycleSimulator(fee_rate=0, symbol_filters=filters).simulate_leg(
        buy_leg(), book, Decimal("100")
    )

    # Affordable raw base is 5 + 50/11; a 0.1 step rounds the order to 9.5.
    assert result.executable
    assert result.order_quantity == Decimal("9.5")
    assert result.output_after_fee == Decimal("9.5")
    assert result.input_consumed == Decimal("99.5")
    assert result.unspent_input == Decimal("0.5")
    assert len(result.consumed_levels) == 2


def test_insufficient_depth_makes_cycle_all_or_nothing():
    legs = (
        CycleLeg("USDT", "AAA", "AAAUSDT", "AAA", "USDT", LegSide.BUY_BASE, BookSide.ASKS),
        CycleLeg("AAA", "BBB", "AAABBB", "AAA", "BBB", LegSide.SELL_BASE, BookSide.BIDS),
        CycleLeg("BBB", "USDT", "BBBUSDT", "BBB", "USDT", LegSide.SELL_BASE, BookSide.BIDS),
    )
    cycle = LooseCycle("shallow", ("USDT", "AAA", "BBB", "USDT"), legs)
    books = {
        "AAAUSDT": StaticBook(bids=[(9, 5)], asks=[(10, 1)]),
        "AAABBB": StaticBook(bids=[(2, 100)], asks=[(3, 100)]),
        "BBBUSDT": StaticBook(bids=[(6, 100)], asks=[(7, 100)]),
    }

    result = CycleSimulator(fee_rate=0).simulate(cycle, books, Decimal("20"), mode="depth")

    assert not result.fully_executable
    assert result.final_amount == 0
    assert result.pnl is None
    assert result.to_dict()["estimated_pnl"] is None
    assert result.limiting_leg == "AAAUSDT"
    assert len(result.legs) == 1
    assert "insufficient displayed liquidity" in result.legs[0].rejection_reasons


def test_pessimistic_haircut_and_slippage_are_applied_after_leg_one():
    legs = (
        CycleLeg("USDT", "AAA", "AAAUSDT", "AAA", "USDT", LegSide.BUY_BASE, BookSide.ASKS),
        CycleLeg("AAA", "BBB", "AAABBB", "AAA", "BBB", LegSide.SELL_BASE, BookSide.BIDS),
        CycleLeg("BBB", "USDT", "BBBUSDT", "BBB", "USDT", LegSide.SELL_BASE, BookSide.BIDS),
    )
    cycle = LooseCycle("pessimistic", ("USDT", "AAA", "BBB", "USDT"), legs)
    books = {
        "AAAUSDT": StaticBook(bids=[("0.9", 100)], asks=[(1, 100)]),
        "AAABBB": StaticBook(bids=[(1, 100)], asks=[("1.1", 100)]),
        "BBBUSDT": StaticBook(bids=[("1.02", 100)], asks=[("1.03", 100)]),
    }
    simulator = CycleSimulator(fee_rate=0, quantity_haircut="0.5", extra_slippage_bps=100)

    depth = simulator.simulate(cycle, books, Decimal("10"), mode="depth")
    pessimistic = simulator.simulate(cycle, books, Decimal("10"), mode="pessimistic")

    assert depth.final_amount == Decimal("10.20")
    assert pessimistic.legs[0].average_price == depth.legs[0].average_price
    assert pessimistic.legs[1].average_price == Decimal("0.99")
    assert pessimistic.final_amount < depth.final_amount
    assert pessimistic.max_executable_size < depth.max_executable_size


def test_max_executable_size_identifies_the_downstream_limit_in_start_asset():
    legs = (
        CycleLeg("USDT", "AAA", "AAAUSDT", "AAA", "USDT", LegSide.BUY_BASE, BookSide.ASKS),
        CycleLeg("AAA", "BBB", "AAABBB", "AAA", "BBB", LegSide.SELL_BASE, BookSide.BIDS),
        CycleLeg("BBB", "USDT", "BBBUSDT", "BBB", "USDT", LegSide.SELL_BASE, BookSide.BIDS),
    )
    cycle = LooseCycle("limited-second", ("USDT", "AAA", "BBB", "USDT"), legs)
    books = {
        "AAAUSDT": StaticBook(bids=[(9, 100)], asks=[(10, 100)]),  # 1000 USDT capacity
        "AAABBB": StaticBook(bids=[(2, 3)], asks=[(3, 100)]),  # only 3 AAA = 30 starting USDT
        "BBBUSDT": StaticBook(bids=[(6, 100)], asks=[(7, 100)]),
    }

    maximum = CycleSimulator(fee_rate=0).estimate_max_executable(cycle, books)

    assert maximum == Decimal("30")


def test_max_executable_size_includes_quantity_notional_and_step_caps():
    legs = (
        CycleLeg("USDT", "AAA", "AAAUSDT", "AAA", "USDT", LegSide.BUY_BASE, BookSide.ASKS),
        CycleLeg("AAA", "BBB", "AAABBB", "AAA", "BBB", LegSide.SELL_BASE, BookSide.BIDS),
        CycleLeg("BBB", "USDT", "BBBUSDT", "BBB", "USDT", LegSide.SELL_BASE, BookSide.BIDS),
    )
    cycle = LooseCycle("filtered-second", ("USDT", "AAA", "BBB", "USDT"), legs)
    books = {
        "AAAUSDT": StaticBook(bids=[(9, 100)], asks=[(10, 100)]),
        "AAABBB": StaticBook(bids=[(2, 100)], asks=[(3, 100)]),
        "BBBUSDT": StaticBook(bids=[(6, 100)], asks=[(7, 100)]),
    }
    filters = {
        "AAABBB": [
            {"filterType": "LOT_SIZE", "minQty": "0.5", "maxQty": "2", "stepSize": "0.5"},
            {
                "filterType": "NOTIONAL",
                "minNotional": "1",
                "maxNotional": "3",
                "applyMinToMarket": True,
                "applyMaxToMarket": True,
            },
        ]
    }

    maximum = CycleSimulator(fee_rate=0, symbol_filters=filters).estimate_max_executable(
        cycle, books
    )

    # Leg two may sell 1.5 AAA (3 BBB notional), requiring 15 starting USDT.
    assert maximum == Decimal("15")


def test_max_executable_is_zero_when_upstream_depth_cannot_reach_downstream_minimum():
    legs = (
        CycleLeg("USDT", "AAA", "AAAUSDT", "AAA", "USDT", LegSide.BUY_BASE, BookSide.ASKS),
        CycleLeg("AAA", "BBB", "AAABBB", "AAA", "BBB", LegSide.SELL_BASE, BookSide.BIDS),
        CycleLeg("BBB", "USDT", "BBBUSDT", "BBB", "USDT", LegSide.SELL_BASE, BookSide.BIDS),
    )
    cycle = LooseCycle("below-minimum", ("USDT", "AAA", "BBB", "USDT"), legs)
    books = {
        "AAAUSDT": StaticBook(bids=[(9, "0.1")], asks=[(10, "0.1")]),
        "AAABBB": StaticBook(bids=[(2, 100)], asks=[(3, 100)]),
        "BBBUSDT": StaticBook(bids=[(6, 100)], asks=[(7, 100)]),
    }
    filters = {
        "AAABBB": [{"filterType": "LOT_SIZE", "minQty": "1", "maxQty": "100", "stepSize": "0.1"}]
    }

    maximum = CycleSimulator(fee_rate=0, symbol_filters=filters).estimate_max_executable(
        cycle, books
    )

    assert maximum == 0
