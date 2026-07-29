"""Deterministic spot-cycle execution simulation.

The simulator deliberately has no exchange client and no order-placement code.  It
only consumes immutable-looking cycle/book protocols, which makes the dangerous
money and direction logic straightforward to unit test.

Amounts and prices are converted to :class:`~decimal.Decimal` at the boundary.
For a ``SELL_BASE`` leg the input is base quantity and bids are consumed.  For a
``BUY_BASE`` leg the input is quote quantity and asks are consumed.  Taker fees
are charged from the output of every leg.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal, InvalidOperation
from enum import Enum
from math import lcm
from typing import Any, Protocol

ZERO = Decimal("0")
ONE = Decimal("1")
TEN_THOUSAND = Decimal("10000")


class BookProtocol(Protocol):
    """The small portion of a local order book used by the simulator."""

    def bid_levels(self, limit: int | None = None) -> Sequence[tuple[Any, Any]]: ...

    def ask_levels(self, limit: int | None = None) -> Sequence[tuple[Any, Any]]: ...


def decimal(value: Any, default: Decimal | None = None) -> Decimal:
    """Convert common numeric inputs without introducing binary-float noise."""

    if isinstance(value, Decimal):
        return value
    if value is None:
        if default is not None:
            return default
        raise ValueError("a decimal value is required")
    if isinstance(value, float):
        value = repr(value)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        if default is not None:
            return default
        raise ValueError(f"invalid decimal value: {value!r}") from exc


def _enum_text(value: Any) -> str:
    if isinstance(value, Enum):
        value = value.value
    return str(value).strip().upper()


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ConsumedLevel:
    """A displayed level used by one simulated market order."""

    price: Decimal
    available_quantity: Decimal
    consumed_quantity: Decimal
    input_consumed: Decimal
    output_amount: Decimal

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


@dataclass(frozen=True, slots=True)
class LegExecution:
    """Execution detail for one conversion in the route."""

    index: int
    symbol: str
    from_asset: str
    to_asset: str
    operation: str
    book_side: str
    input_amount: Decimal
    order_quantity: Decimal
    input_consumed: Decimal
    unspent_input: Decimal
    output_before_fee: Decimal
    fee_paid: Decimal
    fee_rate: Decimal
    output_after_fee: Decimal
    top_price: Decimal | None
    average_price: Decimal | None
    worst_price: Decimal | None
    consumed_levels: tuple[ConsumedLevel, ...] = ()
    liquidity_capacity: Decimal = ZERO
    utilization: Decimal = ZERO
    slippage: Decimal = ZERO
    executable: bool = True
    filter_rejected: bool = False
    rejection_reasons: tuple[str, ...] = ()

    @property
    def gross_output(self) -> Decimal:
        return self.output_before_fee

    @property
    def net_output(self) -> Decimal:
        return self.output_after_fee

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """Result of attempting an all-or-nothing paper cycle."""

    cycle_id: str
    route: tuple[str, ...]
    start_asset: str
    start_amount: Decimal
    final_amount: Decimal
    converted_final_amount: Decimal
    residual_balances: Mapping[str, Decimal]
    residual_value: Decimal
    gross_final_amount: Decimal
    gross_return: Decimal
    net_return: Decimal
    pnl: Decimal | None
    fully_executable: bool
    filter_rejected: bool
    rejection_reasons: tuple[str, ...]
    legs: tuple[LegExecution, ...]
    limiting_leg: str | None
    slippage: Decimal
    max_executable_size: Decimal
    mode: str
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def estimated_pnl(self) -> Decimal | None:
        return self.pnl

    @property
    def raw_return(self) -> Decimal:
        return self.gross_return

    @property
    def return_after_fees(self) -> Decimal:
        return self.net_return

    @property
    def return_after_depth(self) -> Decimal:
        return self.net_return

    @property
    def fully_executable_at_displayed_depth(self) -> bool:
        return self.fully_executable

    @property
    def profitable(self) -> bool:
        return (
            self.fully_executable
            and not self.filter_rejected
            and self.pnl is not None
            and self.pnl > ZERO
        )

    def to_dict(self) -> dict[str, Any]:
        result = _json_value(self)
        # Stable aliases make JSONL records useful to report tools and notebooks.
        result.update(
            {
                "raw_return": result["gross_return"],
                "return_after_fees": result["net_return"],
                "return_after_depth": result["net_return"],
                "estimated_pnl": result["pnl"],
                "profitable": self.profitable,
            }
        )
        return result


@dataclass(frozen=True, slots=True)
class _SymbolFilters:
    min_qty: Decimal = ZERO
    max_qty: Decimal | None = None
    step_size: Decimal = ZERO
    min_notional: Decimal = ZERO
    max_notional: Decimal | None = None
    tick_size: Decimal = ZERO
    min_price: Decimal = ZERO
    max_price: Decimal | None = None


def _object_value(source: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(source, Mapping) and name in source:
            return source[name]
        if hasattr(source, name):
            return getattr(source, name)
    return default


def _filter_flag(source: Any, *names: str, default: bool) -> bool:
    value = _object_value(source, *names, default=None)
    if value is None:
        return default
    if isinstance(value, str):
        normalised = value.strip().lower()
        if normalised in {"true", "1", "yes", "on"}:
            return True
        if normalised in {"false", "0", "no", "off", ""}:
            return False
    return bool(value)


def _positive(values: Iterable[Any]) -> list[Decimal]:
    result: list[Decimal] = []
    for value in values:
        if value in (None, ""):
            continue
        parsed = decimal(value, ZERO)
        if parsed > ZERO:
            result.append(parsed)
    return result


def _combined_quantum(values: Iterable[Any]) -> Decimal:
    """Return a Decimal LCM so rounding satisfies every positive step/tick."""

    quanta = _positive(values)
    if not quanta:
        return ZERO
    decimal_places = max(0, max(-value.normalize().as_tuple().exponent for value in quanta))
    scale = 10**decimal_places
    integer_lcm = 1
    for value in quanta:
        integer_lcm = lcm(integer_lcm, int(value * scale))
    return Decimal(integer_lcm) / Decimal(scale)


def _combined_rules(entries: Sequence[Any]) -> tuple[Decimal, Decimal | None, Decimal]:
    minimums = _positive(_object_value(item, "min_qty", "minQty", default=None) for item in entries)
    maximums = _positive(_object_value(item, "max_qty", "maxQty", default=None) for item in entries)
    steps = (_object_value(item, "step_size", "stepSize", default=None) for item in entries)
    return (
        max(minimums, default=ZERO),
        min(maximums) if maximums else None,
        _combined_quantum(steps),
    )


def _normalise_filters(source: Any) -> _SymbolFilters:
    """Accept raw Binance filters or typed metadata deterministically.

    A market quantity must satisfy both ``LOT_SIZE`` and ``MARKET_LOT_SIZE``.
    Their minimums/maxima are intersected and their step sizes are combined via
    a Decimal least-common multiple.  Notional rules are enabled only when their
    market-order flags say so; legacy ``MIN_NOTIONAL`` defaults to applying when
    ``applyToMarket`` is absent, preserving Binance's conservative legacy usage.
    """

    if source is None:
        return _SymbolFilters()
    nested = _object_value(source, "filters", default=None)
    if nested is not None and nested is not source:
        source = nested

    lot_entries: list[Any] = []
    price_entries: list[Any] = []
    min_notional_entries: list[Any] = []
    full_notional_entries: list[Any] = []
    if isinstance(source, Sequence) and not isinstance(source, (str, bytes, bytearray)):
        for item in source:
            filter_type = _enum_text(_object_value(item, "filterType", "filter_type", default=""))
            if filter_type in {"LOT_SIZE", "MARKET_LOT_SIZE"}:
                lot_entries.append(item)
            elif filter_type == "MIN_NOTIONAL":
                min_notional_entries.append(item)
            elif filter_type == "NOTIONAL":
                full_notional_entries.append(item)
            elif filter_type == "PRICE_FILTER":
                price_entries.append(item)
    else:
        market_lot = _object_value(source, "market_lot_size", default=None)
        regular_lot = _object_value(source, "lot_size", default=None)
        lot_entries.extend(item for item in (regular_lot, market_lot) if item is not None)
        if not lot_entries and any(
            _object_value(source, name, default=None) is not None
            for name in ("min_qty", "minQty", "max_qty", "maxQty", "step_size", "stepSize")
        ):
            lot_entries.append(source)
        price = _object_value(source, "price", default=None)
        price_entries.append(price if price is not None else source)

        notional = _object_value(source, "notional", default=None)
        filter_types = {
            _enum_text(item) for item in (_object_value(source, "filter_types", default=()) or ())
        }
        if notional is not None:
            if "NOTIONAL" in filter_types:
                full_notional_entries.append(notional)
            elif "MIN_NOTIONAL" in filter_types:
                min_notional_entries.append(notional)
            else:
                # A protocol object without filter type still carries explicit
                # apply flags when available; otherwise use safe legacy defaults.
                full_notional_entries.append(notional)
        elif any(
            _object_value(source, name, default=None) is not None
            for name in ("min_notional", "minNotional", "max_notional", "maxNotional")
        ):
            has_full_notional_shape = any(
                _object_value(source, name, default=None) is not None
                for name in (
                    "max_notional",
                    "maxNotional",
                    "apply_min_to_market",
                    "applyMinToMarket",
                    "apply_max_to_market",
                    "applyMaxToMarket",
                )
            )
            (full_notional_entries if has_full_notional_shape else min_notional_entries).append(
                source
            )

    min_qty, max_qty, step_size = _combined_rules(lot_entries)
    price_minimums = _positive(
        _object_value(item, "min_price", "minPrice", default=None) for item in price_entries
    )
    price_maximums = _positive(
        _object_value(item, "max_price", "maxPrice", default=None) for item in price_entries
    )
    tick_size = _combined_quantum(
        _object_value(item, "tick_size", "tickSize", default=None) for item in price_entries
    )

    min_notionals: list[Decimal] = []
    max_notionals: list[Decimal] = []
    # NOTIONAL supersedes legacy MIN_NOTIONAL when both appear.  Within either
    # type, duplicate constraints are intersected independently of list order.
    if full_notional_entries:
        for item in full_notional_entries:
            if _filter_flag(item, "apply_min_to_market", "applyMinToMarket", default=False):
                min_notionals.extend(
                    _positive([_object_value(item, "min_notional", "minNotional", default=None)])
                )
            if _filter_flag(item, "apply_max_to_market", "applyMaxToMarket", default=False):
                max_notionals.extend(
                    _positive([_object_value(item, "max_notional", "maxNotional", default=None)])
                )
    else:
        for item in min_notional_entries:
            if _filter_flag(item, "apply_min_to_market", "applyToMarket", default=True):
                min_notionals.extend(
                    _positive([_object_value(item, "min_notional", "minNotional", default=None)])
                )

    return _SymbolFilters(
        min_qty=min_qty,
        max_qty=max_qty,
        step_size=step_size,
        min_notional=max(min_notionals, default=ZERO),
        max_notional=min(max_notionals) if max_notionals else None,
        tick_size=tick_size,
        min_price=max(price_minimums, default=ZERO),
        max_price=min(price_maximums) if price_maximums else None,
    )


def _quantize_down(value: Decimal, quantum: Decimal) -> Decimal:
    if quantum <= ZERO:
        return value
    return (value / quantum).to_integral_value(rounding=ROUND_DOWN) * quantum


def _quantize_up(value: Decimal, quantum: Decimal) -> Decimal:
    if quantum <= ZERO:
        return value
    return (value / quantum).to_integral_value(rounding=ROUND_CEILING) * quantum


def _level_pair(level: Any) -> tuple[Decimal, Decimal] | None:
    if isinstance(level, Mapping):
        price = _object_value(level, "price", "p", default=None)
        quantity = _object_value(level, "quantity", "qty", "q", default=None)
    elif hasattr(level, "price"):
        price = level.price
        quantity = _object_value(level, "quantity", "qty", default=None)
    else:
        try:
            price, quantity = level[0], level[1]
        except (IndexError, KeyError, TypeError):
            return None
    try:
        price_decimal, quantity_decimal = decimal(price), decimal(quantity)
    except ValueError:
        return None
    if price_decimal <= ZERO or quantity_decimal <= ZERO:
        return None
    return price_decimal, quantity_decimal


def _book_levels(book: Any, side: str) -> list[tuple[Decimal, Decimal]]:
    """Read levels from the project book class or a simple test/data protocol."""

    bids = side == "bids"
    raw: Any = None
    for method_name in ("bid_levels", "get_bids") if bids else ("ask_levels", "get_asks"):
        method = getattr(book, method_name, None)
        if callable(method):
            try:
                raw = method()
            except TypeError:
                raw = method(None)
            break
    if raw is None:
        method = getattr(book, "depth_levels", None)
        if callable(method):
            attempts: list[Any] = [side, side.upper(), "BIDS" if bids else "ASKS"]
            try:
                from .models import BookSide

                attempts.insert(0, BookSide.BIDS if bids else BookSide.ASKS)
            except ImportError:  # pragma: no cover - supports copying this pure module standalone
                pass
            for candidate in attempts:
                try:
                    raw = method(candidate)
                    break
                except (AttributeError, TypeError, ValueError, KeyError):
                    continue
    if raw is None:
        raw = _object_value(book, side, default=())
    if callable(raw):
        raw = raw()
    if isinstance(raw, Mapping):
        raw = raw.items()
    parsed = [pair for item in raw if (pair := _level_pair(item)) is not None]
    parsed.sort(key=lambda item: item[0], reverse=bids)
    return parsed


def _leg_operation(leg: Any) -> str:
    value = _object_value(leg, "side", "operation", default="")
    operation = _enum_text(value)
    if operation in {"SELL_BASE", "SELL", "BIDS", "BID"}:
        return "SELL_BASE"
    if operation in {"BUY_BASE", "BUY", "ASKS", "ASK"}:
        return "BUY_BASE"
    book_side = _enum_text(_object_value(leg, "book_side", default=""))
    if book_side in {"BIDS", "BID"}:
        return "SELL_BASE"
    if book_side in {"ASKS", "ASK"}:
        return "BUY_BASE"
    raise ValueError(f"unsupported cycle leg operation: {value!r}")


def _effective_levels(
    levels: Iterable[tuple[Decimal, Decimal]],
    operation: str,
    haircut: Decimal,
    slippage_bps: Decimal,
) -> list[tuple[Decimal, Decimal]]:
    quantity_factor = ONE - haircut
    price_factor = ONE - slippage_bps / TEN_THOUSAND
    if operation == "BUY_BASE":
        price_factor = ONE + slippage_bps / TEN_THOUSAND
    result: list[tuple[Decimal, Decimal]] = []
    for price, quantity in levels:
        effective_price = price * price_factor
        effective_quantity = quantity * quantity_factor
        if effective_price > ZERO and effective_quantity > ZERO:
            result.append((effective_price, effective_quantity))
    return result


class CycleSimulator:
    """Top-of-book, depth-aware, and pessimistic cycle simulator."""

    def __init__(
        self,
        fee_rate: Decimal | str | float = Decimal("0.001"),
        *,
        quantity_haircut: Decimal | str | float = ZERO,
        extra_slippage_bps: Decimal | str | float = ZERO,
        symbol_filters: Mapping[str, Any] | None = None,
        symbol_fee_rates: Mapping[str, Decimal | str | float] | None = None,
    ) -> None:
        self.fee_rate = decimal(fee_rate)
        self.quantity_haircut = decimal(quantity_haircut)
        self.extra_slippage_bps = decimal(extra_slippage_bps)
        if not ZERO <= self.fee_rate < ONE:
            raise ValueError("fee_rate must be in [0, 1)")
        if not ZERO <= self.quantity_haircut < ONE:
            raise ValueError("quantity_haircut must be in [0, 1)")
        if self.extra_slippage_bps < ZERO or self.extra_slippage_bps >= TEN_THOUSAND:
            raise ValueError("extra_slippage_bps must be in [0, 10000)")
        self.symbol_filters = {
            str(key).upper(): value for key, value in (symbol_filters or {}).items()
        }
        self.symbol_fee_rates = {
            str(key).strip().upper(): decimal(value)
            for key, value in (symbol_fee_rates or {}).items()
        }
        if any(not ZERO <= value < ONE for value in self.symbol_fee_rates.values()):
            raise ValueError("symbol fee rates must be in [0, 1)")

    def fee_rate_for_symbol(self, symbol: str) -> Decimal:
        """Return the account-specific fee or the conservative fallback."""

        return self.symbol_fee_rates.get(symbol.strip().upper(), self.fee_rate)

    def _book(self, books: Any, symbol: str) -> Any:
        if isinstance(books, Mapping):
            book = books.get(symbol)
            if book is None:
                book = books.get(symbol.upper())
            if book is None:
                book = books.get(symbol.lower())
        else:
            getter = getattr(books, "get_book", None)
            book = getter(symbol) if callable(getter) else None
        if book is None:
            raise KeyError(f"no order book for {symbol}")
        return book

    def _filters(self, leg: Any, book: Any) -> _SymbolFilters:
        symbol = str(_object_value(leg, "symbol", default="")).upper()
        source = self.symbol_filters.get(symbol)
        if source is None:
            source = _object_value(leg, "filters", "symbol_info", default=None)
        if source is None:
            source = _object_value(book, "filters", "symbol_info", default=None)
        return _normalise_filters(source)

    def simulate_leg(
        self,
        leg: Any,
        book: Any,
        input_amount: Decimal | str | float,
        *,
        index: int = 0,
        mode: str = "depth",
        fee_rate: Decimal | str | float | None = None,
        quantity_haircut: Decimal | str | float = ZERO,
        extra_slippage_bps: Decimal | str | float = ZERO,
        check_filters: bool = True,
    ) -> LegExecution:
        """Convert one asset through one market, walking the appropriate side."""

        amount = decimal(input_amount)
        haircut = decimal(quantity_haircut)
        slippage_buffer = decimal(extra_slippage_bps)
        if amount <= ZERO:
            raise ValueError("input_amount must be positive")
        if not ZERO <= haircut < ONE:
            raise ValueError("quantity_haircut must be in [0, 1)")
        operation = _leg_operation(leg)
        side = "bids" if operation == "SELL_BASE" else "asks"
        symbol = str(_object_value(leg, "symbol", default=""))
        fee = self.fee_rate_for_symbol(symbol) if fee_rate is None else decimal(fee_rate)
        from_asset = str(_object_value(leg, "from_asset", default=""))
        to_asset = str(_object_value(leg, "to_asset", default=""))
        # ``check_filters=False`` is the mathematical/reference path.  It must
        # not quietly quantize with exchange lot sizes, otherwise gross/raw
        # return would include execution rounding despite its documented role.
        filters = self._filters(leg, book) if check_filters else _SymbolFilters()
        visible_levels = _book_levels(book, side)
        levels = _effective_levels(visible_levels, operation, haircut, slippage_buffer)
        top_price = visible_levels[0][0] if visible_levels else None

        if not levels:
            return LegExecution(
                index=index,
                symbol=symbol,
                from_asset=from_asset,
                to_asset=to_asset,
                operation=operation,
                book_side=side,
                input_amount=amount,
                order_quantity=ZERO,
                input_consumed=ZERO,
                unspent_input=amount,
                output_before_fee=ZERO,
                fee_paid=ZERO,
                fee_rate=fee,
                output_after_fee=ZERO,
                top_price=top_price,
                average_price=None,
                worst_price=None,
                executable=False,
                rejection_reasons=("empty order book side",),
            )

        # Level 1 intentionally estimates at the best displayed price without
        # imposing the top level's quantity.  Level 2/3 consume finite depth.
        walk_levels = levels if mode.lower() != "top" else [(levels[0][0], Decimal("Infinity"))]
        capacity = (
            sum((quantity for _, quantity in levels), ZERO)
            if operation == "SELL_BASE"
            else sum((price * quantity for price, quantity in levels), ZERO)
        )
        consumed: list[ConsumedLevel] = []
        reasons: list[str] = []

        if operation == "SELL_BASE":
            order_quantity = _quantize_down(amount, filters.step_size)
            remaining_quantity = order_quantity
            gross_output = ZERO
            input_consumed = ZERO
            for price, available in walk_levels:
                if remaining_quantity <= ZERO:
                    break
                take = min(remaining_quantity, available)
                output = take * price
                consumed.append(ConsumedLevel(price, available, take, take, output))
                input_consumed += take
                gross_output += output
                remaining_quantity -= take
            liquidity_ok = remaining_quantity <= ZERO and order_quantity > ZERO
            unspent_input = amount - input_consumed
            notional = gross_output
        else:
            # First determine the affordable base amount, then round that order
            # quantity down to the exchange step and replay the levels exactly.
            quote_remaining = amount
            affordable_base = ZERO
            for price, available in walk_levels:
                if quote_remaining <= ZERO:
                    break
                base_take = min(available, quote_remaining / price)
                affordable_base += base_take
                quote_remaining -= base_take * price
            enough_quote_capacity = mode.lower() == "top" or quote_remaining <= ZERO
            order_quantity = _quantize_down(affordable_base, filters.step_size)
            remaining_quantity = order_quantity
            input_consumed = ZERO
            gross_output = ZERO
            for price, available in walk_levels:
                if remaining_quantity <= ZERO:
                    break
                take = min(remaining_quantity, available)
                quote = take * price
                consumed.append(ConsumedLevel(price, available, take, quote, take))
                input_consumed += quote
                gross_output += take
                remaining_quantity -= take
            liquidity_ok = (
                enough_quote_capacity and remaining_quantity <= ZERO and order_quantity > ZERO
            )
            unspent_input = amount - input_consumed
            notional = input_consumed

        if not liquidity_ok:
            reasons.append("insufficient displayed liquidity")
        if order_quantity <= ZERO:
            reasons.append("quantity rounds to zero at step size")
        if check_filters:
            if filters.min_qty > ZERO and order_quantity < filters.min_qty:
                reasons.append(f"quantity below minQty {filters.min_qty}")
            if filters.max_qty is not None and order_quantity > filters.max_qty:
                reasons.append(f"quantity above maxQty {filters.max_qty}")
            if filters.min_notional > ZERO and notional < filters.min_notional:
                reasons.append(f"notional below minNotional {filters.min_notional}")
            if filters.max_notional is not None and notional > filters.max_notional:
                reasons.append(f"notional above maxNotional {filters.max_notional}")
            # PRICE_FILTER constrains submitted limit prices.  These are market
            # fills against exchange-published levels, so no order price exists
            # to quantize or reject (including the synthetic slippage buffer).

        filter_rejected = any(
            marker in reason
            for reason in reasons
            for marker in ("minQty", "maxQty", "minNotional", "maxNotional", "step size")
        )
        fee_paid = gross_output * fee
        net_output = gross_output - fee_paid
        average_price: Decimal | None
        if operation == "SELL_BASE":
            average_price = gross_output / input_consumed if input_consumed > ZERO else None
        else:
            average_price = input_consumed / gross_output if gross_output > ZERO else None
        worst_price = consumed[-1].price if consumed else None
        if top_price is None or average_price is None:
            slippage = ZERO
        elif operation == "SELL_BASE":
            slippage = max(ZERO, ONE - average_price / top_price)
        else:
            slippage = max(ZERO, average_price / top_price - ONE)
        utilization = min(ONE, input_consumed / capacity) if capacity > ZERO else ZERO
        executable = liquidity_ok and not filter_rejected
        return LegExecution(
            index=index,
            symbol=symbol,
            from_asset=from_asset,
            to_asset=to_asset,
            operation=operation,
            book_side=side,
            input_amount=amount,
            order_quantity=order_quantity,
            input_consumed=input_consumed,
            unspent_input=max(ZERO, unspent_input),
            output_before_fee=gross_output,
            fee_paid=fee_paid,
            fee_rate=fee,
            output_after_fee=net_output,
            top_price=top_price,
            average_price=average_price,
            worst_price=worst_price,
            consumed_levels=tuple(consumed),
            liquidity_capacity=capacity,
            utilization=utilization,
            slippage=slippage,
            executable=executable,
            filter_rejected=filter_rejected,
            rejection_reasons=tuple(dict.fromkeys(reasons)),
        )

    def _run_route(
        self,
        cycle: Any,
        books: Any,
        amount: Decimal,
        *,
        mode: str,
        fee_rate: Decimal | None,
        quantity_haircut: Decimal,
        extra_slippage_bps: Decimal,
        pessimistic_from_leg: int,
        check_filters: bool,
    ) -> tuple[Decimal, tuple[LegExecution, ...], bool, tuple[str, ...]]:
        current = amount
        executions: list[LegExecution] = []
        reasons: list[str] = []
        for index, leg in enumerate(tuple(_object_value(cycle, "legs", default=()))):
            symbol = str(_object_value(leg, "symbol", default=""))
            try:
                book = self._book(books, symbol)
            except KeyError as exc:
                reasons.append(str(exc))
                return ZERO, tuple(executions), False, tuple(reasons)
            apply_shock = index >= pessimistic_from_leg
            execution = self.simulate_leg(
                leg,
                book,
                current,
                index=index,
                mode=mode,
                fee_rate=fee_rate,
                quantity_haircut=quantity_haircut if apply_shock else ZERO,
                extra_slippage_bps=extra_slippage_bps if apply_shock else ZERO,
                check_filters=check_filters,
            )
            executions.append(execution)
            reasons.extend(
                f"leg {index + 1} {symbol}: {reason}" for reason in execution.rejection_reasons
            )
            if not execution.executable:
                return ZERO, tuple(executions), False, tuple(reasons)
            current = execution.output_after_fee
        if not executions:
            reasons.append("cycle has no legs")
            return ZERO, (), False, tuple(reasons)
        return current, tuple(executions), True, tuple(reasons)

    def _value_residuals(
        self,
        cycle: Any,
        books: Any,
        executions: Sequence[LegExecution],
        *,
        fee_rate: Decimal | None,
        extra_slippage_bps: Decimal,
        pessimistic_from_leg: int,
    ) -> tuple[dict[str, Decimal], Decimal]:
        """Conservatively mark lot-rounding residuals into the root asset.

        Rounded market quantities leave real balances behind; they are not a
        realized loss.  Root-asset cash is retained at par.  Other residuals
        are marked through the remaining route at visible top prices, including
        the configured fees and any adverse price buffer.  This valuation does
        not claim the dust is immediately tradable: each leg record still
        exposes the exact unspent balance.
        """

        legs = tuple(_object_value(cycle, "legs", default=()))
        route = tuple(_object_value(cycle, "assets", "route", default=()))
        root_asset = str(route[0]) if route else executions[0].from_asset
        balances: dict[str, Decimal] = {}
        root_value = ZERO
        for execution in executions:
            residual = execution.unspent_input
            if residual <= ZERO:
                continue
            balances[execution.from_asset] = balances.get(execution.from_asset, ZERO) + residual
            if execution.from_asset == root_asset:
                root_value += residual
                continue

            marked = residual
            for leg_index in range(execution.index, len(legs)):
                leg = legs[leg_index]
                operation = _leg_operation(leg)
                side = "bids" if operation == "SELL_BASE" else "asks"
                symbol = str(_object_value(leg, "symbol", default=""))
                book = self._book(books, symbol)
                slippage = extra_slippage_bps if leg_index >= pessimistic_from_leg else ZERO
                levels = _effective_levels(
                    _book_levels(book, side),
                    operation,
                    ZERO,
                    slippage,
                )
                if not levels:
                    marked = ZERO
                    break
                price = levels[0][0]
                marked = marked * price if operation == "SELL_BASE" else marked / price
                symbol = str(_object_value(leg, "symbol", default=""))
                applied_fee = self.fee_rate_for_symbol(symbol) if fee_rate is None else fee_rate
                fee_factor = ONE - applied_fee
                marked *= fee_factor
            root_value += marked

        return balances, root_value

    def simulate(
        self,
        cycle: Any,
        books: Any,
        start_amount: Decimal | str | float,
        *,
        mode: str = "depth",
        quantity_haircut: Decimal | str | float | None = None,
        extra_slippage_bps: Decimal | str | float | None = None,
    ) -> SimulationResult:
        """Attempt a complete cycle, using zero final amount as a no-trade sentinel.

        ``mode='top'`` is the fast theoretical estimate. ``mode='depth'`` walks
        displayed depth. ``mode='pessimistic'`` walks depth normally on leg one,
        then applies configured quantity haircut and adverse price buffer to legs
        two and three.

        Failed/filter-rejected routes have ``pnl=None`` because no paper order is
        considered placed; the zero final amount must not be read as capital loss.
        Successful routes expose actual root cash as ``converted_final_amount``;
        ``final_amount`` additionally includes a conservative root-value mark for
        lot-rounding residual balances so retained inventory is not called a loss.
        """

        amount = decimal(start_amount)
        if amount <= ZERO:
            raise ValueError("start_amount must be positive")
        selected_mode = mode.strip().lower().replace("-", "_")
        if selected_mode in {"top_of_book", "tob", "level1", "level_1"}:
            selected_mode = "top"
        elif selected_mode in {"depth_aware", "level2", "level_2"}:
            selected_mode = "depth"
        elif selected_mode in {"level3", "level_3", "conservative"}:
            selected_mode = "pessimistic"
        if selected_mode not in {"top", "depth", "pessimistic"}:
            raise ValueError("mode must be 'top', 'depth', or 'pessimistic'")

        if quantity_haircut is None:
            haircut = self.quantity_haircut if selected_mode == "pessimistic" else ZERO
        else:
            haircut = decimal(quantity_haircut)
        if extra_slippage_bps is None:
            slippage_buffer = self.extra_slippage_bps if selected_mode == "pessimistic" else ZERO
        else:
            slippage_buffer = decimal(extra_slippage_bps)
        if not ZERO <= haircut < ONE:
            raise ValueError("quantity_haircut must be in [0, 1)")

        cycle_id = str(_object_value(cycle, "id", "cycle_id", default="unknown"))
        route_value = _object_value(cycle, "assets", "route", default=())
        route = tuple(str(asset) for asset in route_value)
        legs_value = tuple(_object_value(cycle, "legs", default=()))
        if not route and legs_value:
            route = tuple(
                [str(_object_value(legs_value[0], "from_asset", default=""))]
                + [str(_object_value(leg, "to_asset", default="")) for leg in legs_value]
            )
        start_asset = (
            route[0]
            if route
            else str(_object_value(legs_value[0], "from_asset", default=""))
            if legs_value
            else ""
        )

        route_mode = "top" if selected_mode == "top" else "depth"
        shock_start = 1 if selected_mode == "pessimistic" else 0
        converted_final_amount, leg_results, executable, reasons = self._run_route(
            cycle,
            books,
            amount,
            mode=route_mode,
            fee_rate=None,
            quantity_haircut=haircut,
            extra_slippage_bps=slippage_buffer,
            pessimistic_from_leg=shock_start,
            check_filters=True,
        )
        gross_final, _, gross_executable, _ = self._run_route(
            cycle,
            books,
            amount,
            mode="top",
            fee_rate=ZERO,
            quantity_haircut=ZERO,
            extra_slippage_bps=ZERO,
            pessimistic_from_leg=0,
            check_filters=False,
        )
        if not gross_executable:
            gross_final = ZERO
        gross_return = gross_final / amount - ONE if gross_final > ZERO else -ONE
        if executable:
            residual_balances, residual_value = self._value_residuals(
                cycle,
                books,
                leg_results,
                fee_rate=None,
                extra_slippage_bps=slippage_buffer,
                pessimistic_from_leg=shock_start,
            )
            final_amount = converted_final_amount + residual_value
        else:
            residual_balances = {}
            residual_value = ZERO
            final_amount = ZERO
        net_return = final_amount / amount - ONE if executable else -ONE
        # A rejected or liquidity-incomplete paper route never sent an order, so
        # it has no realized/estimated PnL.  Zero final_amount remains an explicit
        # all-or-nothing result sentinel, not a claim that capital was lost.
        pnl = final_amount - amount if executable else None
        filter_rejected = any(item.filter_rejected for item in leg_results)

        max_size, estimated_limiting = self._estimate_max_with_leg(
            cycle,
            books,
            quantity_haircut=haircut,
            extra_slippage_bps=slippage_buffer,
            pessimistic_from_leg=shock_start,
        )
        failed_leg = next((item.symbol for item in leg_results if not item.executable), None)
        if not executable and failed_leg is None and len(leg_results) < len(legs_value):
            failed_leg = str(
                _object_value(legs_value[len(leg_results)], "symbol", default="unknown")
            )
        limiting_leg = failed_leg or estimated_limiting
        # This compounded measure remains useful even when a separate no-fee depth
        # route cannot complete because its downstream quantity is larger.
        retained = ONE
        for item in leg_results:
            retained *= ONE - min(ONE, max(ZERO, item.slippage))
        aggregate_slippage = ONE - retained
        return SimulationResult(
            cycle_id=cycle_id,
            route=route,
            start_asset=start_asset,
            start_amount=amount,
            final_amount=final_amount,
            converted_final_amount=converted_final_amount,
            residual_balances=residual_balances,
            residual_value=residual_value,
            gross_final_amount=gross_final,
            gross_return=gross_return,
            net_return=net_return,
            pnl=pnl,
            fully_executable=executable,
            filter_rejected=filter_rejected,
            rejection_reasons=tuple(dict.fromkeys(reasons)),
            legs=leg_results,
            limiting_leg=limiting_leg,
            slippage=aggregate_slippage,
            max_executable_size=max_size,
            mode=selected_mode,
        )

    def top_of_book(self, cycle: Any, books: Any, start_amount: Any) -> SimulationResult:
        return self.simulate(cycle, books, start_amount, mode="top")

    def simulate_pessimistic(self, cycle: Any, books: Any, start_amount: Any) -> SimulationResult:
        return self.simulate(cycle, books, start_amount, mode="pessimistic")

    def _leg_capacity(
        self,
        leg: Any,
        book: Any,
        *,
        haircut: Decimal,
        slippage_bps: Decimal,
    ) -> tuple[Decimal, list[tuple[Decimal, Decimal]], str]:
        operation = _leg_operation(leg)
        side = "bids" if operation == "SELL_BASE" else "asks"
        levels = _effective_levels(_book_levels(book, side), operation, haircut, slippage_bps)
        filters = self._filters(leg, book)

        def quote_for_base(base_target: Decimal) -> Decimal:
            remaining = base_target
            quote = ZERO
            for price, available_base in levels:
                take = min(remaining, available_base)
                quote += take * price
                remaining -= take
                if remaining <= ZERO:
                    break
            return quote

        def base_for_quote(quote_target: Decimal) -> Decimal:
            remaining = quote_target
            base = ZERO
            for price, available_base in levels:
                take = min(available_base, remaining / price)
                base += take
                remaining -= take * price
                if remaining <= ZERO:
                    break
            return base

        total_base = sum((quantity for _, quantity in levels), ZERO)
        if operation == "SELL_BASE":
            maximum_base = total_base
            if filters.max_qty is not None:
                maximum_base = min(maximum_base, filters.max_qty)
            if filters.max_notional is not None:
                maximum_base = min(maximum_base, base_for_quote(filters.max_notional))
            maximum_base = _quantize_down(maximum_base, filters.step_size)
            capacity = maximum_base
            if filters.min_qty > ZERO and maximum_base < filters.min_qty:
                capacity = ZERO
            if filters.min_notional > ZERO and quote_for_base(maximum_base) < filters.min_notional:
                capacity = ZERO
        else:
            maximum_base = total_base
            if filters.max_qty is not None:
                maximum_base = min(maximum_base, filters.max_qty)
            maximum_base = _quantize_down(maximum_base, filters.step_size)
            capacity = quote_for_base(maximum_base)
            if filters.max_notional is not None:
                capacity = min(capacity, filters.max_notional)
                # A quote cap can stop between base steps. Recompute the largest
                # step-aligned market quantity and its actual quote consumption.
                maximum_base = _quantize_down(base_for_quote(capacity), filters.step_size)
                capacity = quote_for_base(maximum_base)
            if filters.min_qty > ZERO and maximum_base < filters.min_qty:
                capacity = ZERO
            if filters.min_notional > ZERO and capacity < filters.min_notional:
                capacity = ZERO
        return capacity, levels, operation

    def _required_input(
        self,
        operation: str,
        levels: Sequence[tuple[Decimal, Decimal]],
        desired_net_output: Decimal,
        fee_rate: Decimal,
    ) -> Decimal | None:
        desired_gross = desired_net_output / (ONE - fee_rate)
        remaining = desired_gross
        required = ZERO
        if operation == "SELL_BASE":
            # Desired output is quote; consume base until quote target is reached.
            for price, available_base in levels:
                quote_available = price * available_base
                quote_take = min(remaining, quote_available)
                required += quote_take / price
                remaining -= quote_take
                if remaining <= ZERO:
                    return required
        else:
            # Desired output is base; calculate the quote required for that base.
            for price, available_base in levels:
                base_take = min(remaining, available_base)
                required += base_take * price
                remaining -= base_take
                if remaining <= ZERO:
                    return required
        return None

    def _estimate_max_with_leg(
        self,
        cycle: Any,
        books: Any,
        *,
        quantity_haircut: Decimal,
        extra_slippage_bps: Decimal,
        pessimistic_from_leg: int,
    ) -> tuple[Decimal, str | None]:
        legs = tuple(_object_value(cycle, "legs", default=()))
        if not legs:
            return ZERO, None
        infos: list[tuple[Decimal, list[tuple[Decimal, Decimal]], str, Decimal]] = []
        for index, leg in enumerate(legs):
            symbol = str(_object_value(leg, "symbol", default=""))
            try:
                book = self._book(books, symbol)
            except KeyError:
                return ZERO, symbol or None
            shocked = index >= pessimistic_from_leg
            info = self._leg_capacity(
                leg,
                book,
                haircut=quantity_haircut if shocked else ZERO,
                slippage_bps=extra_slippage_bps if shocked else ZERO,
            )
            infos.append((*info, self.fee_rate_for_symbol(symbol)))
        candidates: list[tuple[Decimal, int]] = [(infos[0][0], 0)]
        for target_index in range(1, len(legs)):
            target = infos[target_index][0]
            possible = True
            for prior_index in range(target_index - 1, -1, -1):
                _, levels, operation, fee_rate = infos[prior_index]
                required = self._required_input(operation, levels, target, fee_rate)
                if required is None:
                    possible = False
                    break
                target = required
            if possible:
                candidates.append((target, target_index))
        maximum, limiting_index = min(candidates, key=lambda item: item[0])
        symbol = str(_object_value(legs[limiting_index], "symbol", default="")) or None
        if maximum > ZERO:
            _, trial_legs, executable, _ = self._run_route(
                cycle,
                books,
                maximum,
                mode="depth",
                fee_rate=None,
                quantity_haircut=quantity_haircut,
                extra_slippage_bps=extra_slippage_bps,
                pessimistic_from_leg=pessimistic_from_leg,
                check_filters=True,
            )
            if not executable:
                failed = next((item.symbol for item in trial_legs if not item.executable), symbol)
                return ZERO, failed
        return max(ZERO, maximum), symbol

    def estimate_max_executable(
        self,
        cycle: Any,
        books: Any,
        *,
        quantity_haircut: Decimal | str | float = ZERO,
        extra_slippage_bps: Decimal | str | float = ZERO,
        pessimistic_from_leg: int = 0,
    ) -> Decimal:
        """Estimate the largest start amount supported by every displayed leg."""

        maximum, _ = self._estimate_max_with_leg(
            cycle,
            books,
            quantity_haircut=decimal(quantity_haircut),
            extra_slippage_bps=decimal(extra_slippage_bps),
            pessimistic_from_leg=pessimistic_from_leg,
        )
        return maximum


# Friendly functional API and compatibility aliases.
ExecutionSimulator = CycleSimulator


def simulate_cycle(
    cycle: Any,
    books: Any,
    start_amount: Decimal | str | float,
    *,
    fee_rate: Decimal | str | float = Decimal("0.001"),
    mode: str = "depth",
    quantity_haircut: Decimal | str | float = ZERO,
    extra_slippage_bps: Decimal | str | float = ZERO,
    symbol_filters: Mapping[str, Any] | None = None,
) -> SimulationResult:
    simulator = CycleSimulator(
        fee_rate,
        quantity_haircut=quantity_haircut,
        extra_slippage_bps=extra_slippage_bps,
        symbol_filters=symbol_filters,
    )
    return simulator.simulate(
        cycle,
        books,
        start_amount,
        mode=mode,
        quantity_haircut=quantity_haircut if mode != "pessimistic" else None,
        extra_slippage_bps=extra_slippage_bps if mode != "pessimistic" else None,
    )


def estimate_max_executable(
    cycle: Any,
    books: Any,
    *,
    fee_rate: Decimal | str | float = Decimal("0.001"),
    quantity_haircut: Decimal | str | float = ZERO,
    extra_slippage_bps: Decimal | str | float = ZERO,
) -> Decimal:
    return CycleSimulator(fee_rate).estimate_max_executable(
        cycle,
        books,
        quantity_haircut=quantity_haircut,
        extra_slippage_bps=extra_slippage_bps,
    )


__all__ = [
    "BookProtocol",
    "ConsumedLevel",
    "CycleSimulator",
    "ExecutionSimulator",
    "LegExecution",
    "SimulationResult",
    "decimal",
    "estimate_max_executable",
    "simulate_cycle",
]
