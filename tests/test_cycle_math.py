from decimal import Decimal

from tri_arb.models import BookSide, CycleLeg, LegSide, TriangularCycle
from tri_arb.simulator import CycleSimulator


class StaticBook:
    def __init__(self, bids, asks):
        self._bids = [(Decimal(str(price)), Decimal(str(qty))) for price, qty in bids]
        self._asks = [(Decimal(str(price)), Decimal(str(qty))) for price, qty in asks]

    def bid_levels(self, limit=None):
        return self._bids if limit is None else self._bids[:limit]

    def ask_levels(self, limit=None):
        return self._asks if limit is None else self._asks[:limit]


def usdt_btc_eth_cycle() -> TriangularCycle:
    legs = (
        CycleLeg("USDT", "BTC", "BTCUSDT", "BTC", "USDT", LegSide.BUY_BASE, BookSide.ASKS),
        CycleLeg("BTC", "ETH", "ETHBTC", "ETH", "BTC", LegSide.BUY_BASE, BookSide.ASKS),
        CycleLeg("ETH", "USDT", "ETHUSDT", "ETH", "USDT", LegSide.SELL_BASE, BookSide.BIDS),
    )
    return TriangularCycle(
        cycle_id="USDT-BTC-ETH-USDT",
        root_asset="USDT",
        assets=("USDT", "BTC", "ETH", "USDT"),
        symbols=("BTCUSDT", "ETHBTC", "ETHUSDT"),
        legs=legs,
        liquidity_score=Decimal("1"),
        spread_score=Decimal("1"),
        feasibility_score=Decimal("1"),
    )


def books():
    return {
        "BTCUSDT": StaticBook(bids=[(9, 100)], asks=[(10, 100)]),
        "ETHBTC": StaticBook(bids=[("1.8", 100)], asks=[(2, 100)]),
        "ETHUSDT": StaticBook(bids=[(21, 100)], asks=[(22, 100)]),
    }


def test_bid_ask_direction_and_three_leg_fee_application():
    result = CycleSimulator(fee_rate=Decimal("0.01")).simulate(
        usdt_btc_eth_cycle(), books(), Decimal("100"), mode="top"
    )

    # No-fee route: 100 USDT / 10 / 2 * 21 = 105 USDT.
    assert result.gross_final_amount == Decimal("105")
    assert result.gross_return == Decimal("0.05")
    # Fee is deducted from every leg's output: 105 * 0.99 ** 3.
    assert result.final_amount == Decimal("101.88139500")
    assert result.net_return == Decimal("0.0188139500")
    assert result.pnl == Decimal("1.88139500")
    assert [leg.book_side for leg in result.legs] == ["asks", "asks", "bids"]
    assert [leg.top_price for leg in result.legs] == [Decimal("10"), Decimal("2"), Decimal("21")]
    assert all(leg.fee_paid > 0 for leg in result.legs)
    assert result.fully_executable


def test_filters_flag_a_trade_that_binance_would_reject():
    filters = {
        "BTCUSDT": [
            {"filterType": "LOT_SIZE", "minQty": "0.0001", "maxQty": "100", "stepSize": "0.0001"},
            {"filterType": "MIN_NOTIONAL", "minNotional": "20"},
        ]
    }
    result = CycleSimulator(fee_rate=0, symbol_filters=filters).simulate(
        usdt_btc_eth_cycle(), books(), Decimal("10"), mode="depth"
    )

    assert not result.fully_executable
    assert result.filter_rejected
    assert result.final_amount == 0
    assert result.limiting_leg == "BTCUSDT"
    assert any("minNotional" in reason for reason in result.rejection_reasons)


def test_top_of_book_is_a_price_estimate_not_a_top_quantity_fill_test():
    shallow = books()
    shallow["BTCUSDT"] = StaticBook(bids=[(9, "0.01")], asks=[(10, "0.01")])
    simulator = CycleSimulator(fee_rate=0)

    top = simulator.simulate(usdt_btc_eth_cycle(), shallow, Decimal("100"), mode="top")
    depth = simulator.simulate(usdt_btc_eth_cycle(), shallow, Decimal("100"), mode="depth")

    assert top.fully_executable
    assert not depth.fully_executable


def test_lot_rounding_residuals_are_valued_instead_of_counted_as_lost_capital():
    filters = {
        "BTCUSDT": [
            {
                "filterType": "LOT_SIZE",
                "minQty": "0",
                "maxQty": "1000",
                "stepSize": "3",
            }
        ],
        "ETHBTC": [
            {
                "filterType": "LOT_SIZE",
                "minQty": "0",
                "maxQty": "1000",
                "stepSize": "1",
            }
        ],
        "ETHUSDT": [
            {
                "filterType": "LOT_SIZE",
                "minQty": "0",
                "maxQty": "1000",
                "stepSize": "3",
            }
        ],
    }

    result = CycleSimulator(fee_rate=0, symbol_filters=filters).simulate(
        usdt_btc_eth_cycle(), books(), Decimal("100"), mode="top"
    )

    assert result.converted_final_amount == Decimal("63")
    assert result.residual_balances == {
        "USDT": Decimal("10"),
        "BTC": Decimal("1"),
        "ETH": Decimal("1"),
    }
    assert result.residual_value == Decimal("41.5")
    assert result.final_amount == Decimal("104.5")
    assert result.gross_final_amount == Decimal("105")
    assert result.pnl == Decimal("4.5")
    assert result.fully_executable


def test_lot_and_market_lot_filters_combine_independent_of_wire_order():
    lot = {
        "filterType": "LOT_SIZE",
        "minQty": "1",
        "maxQty": "100",
        "stepSize": "0.01",
    }
    market_lot = {
        "filterType": "MARKET_LOT_SIZE",
        "minQty": "2",
        "maxQty": "50",
        "stepSize": "0.1",
    }
    leg = CycleLeg("BTC", "USDT", "BTCUSDT", "BTC", "USDT", LegSide.SELL_BASE, BookSide.BIDS)
    book = StaticBook(bids=[(10, 100)], asks=[(11, 100)])

    forward = CycleSimulator(
        fee_rate=0, symbol_filters={"BTCUSDT": [lot, market_lot]}
    ).simulate_leg(leg, book, Decimal("2.17"))
    reversed_order = CycleSimulator(
        fee_rate=0, symbol_filters={"BTCUSDT": [market_lot, lot]}
    ).simulate_leg(leg, book, Decimal("2.17"))

    assert forward == reversed_order
    assert forward.order_quantity == Decimal("2.1")
    assert forward.executable


def test_notional_market_flags_and_legacy_default_are_honored():
    leg = CycleLeg("BTC", "USDT", "BTCUSDT", "BTC", "USDT", LegSide.SELL_BASE, BookSide.BIDS)
    book = StaticBook(bids=[(10, 100)], asks=[(11, 100)])

    def execute(rule):
        return CycleSimulator(fee_rate=0, symbol_filters={"BTCUSDT": [rule]}).simulate_leg(
            leg, book, Decimal("1")
        )

    # Legacy MIN_NOTIONAL is conservatively active when applyToMarket is absent,
    # but an explicit false must be respected.
    assert execute({"filterType": "MIN_NOTIONAL", "minNotional": "20"}).filter_rejected
    assert not execute(
        {"filterType": "MIN_NOTIONAL", "minNotional": "20", "applyToMarket": False}
    ).filter_rejected

    assert not execute(
        {
            "filterType": "NOTIONAL",
            "minNotional": "20",
            "maxNotional": "5",
            "applyMinToMarket": False,
            "applyMaxToMarket": False,
        }
    ).filter_rejected
    assert execute(
        {
            "filterType": "NOTIONAL",
            "minNotional": "20",
            "maxNotional": "100",
            "applyMinToMarket": True,
            "applyMaxToMarket": False,
        }
    ).filter_rejected
    assert execute(
        {
            "filterType": "NOTIONAL",
            "minNotional": "1",
            "maxNotional": "5",
            "applyMinToMarket": False,
            "applyMaxToMarket": True,
        }
    ).filter_rejected


def test_price_filter_does_not_reject_market_fill_prices():
    leg = CycleLeg("BTC", "USDT", "BTCUSDT", "BTC", "USDT", LegSide.SELL_BASE, BookSide.BIDS)
    book = StaticBook(bids=[("10.037", 100)], asks=[("10.1", 100)])
    filters = {
        "BTCUSDT": [
            {
                "filterType": "PRICE_FILTER",
                "minPrice": "20",
                "maxPrice": "30",
                "tickSize": "0.1",
            }
        ]
    }

    result = CycleSimulator(fee_rate=0, symbol_filters=filters).simulate_leg(
        leg, book, Decimal("1")
    )

    assert result.executable
    assert result.average_price == Decimal("10.037")
