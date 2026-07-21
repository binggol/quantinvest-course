"""Deterministic China A-share quote and settlement rules for backtests.

Historical limit regimes and IPO no-limit windows must be supplied through
``limit_pct``, exact limit prices, or ``has_price_limit``.  Symbol-prefix board
inference is only a present-day fallback; it is not a historical rules engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from typing import Mapping


ZERO = Decimal("0")
DEFAULT_TICK = Decimal("0.01")


class Board(str, Enum):
    MAIN = "main"
    CHINEXT = "chinext"
    STAR = "star"
    BSE = "bse"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


def _decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def _trade_date(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("trade_date must use YYYY-MM-DD") from exc


def infer_current_board(code: str) -> Board:
    """Infer the current board from a six-digit symbol as a fallback only."""
    digits = "".join(ch for ch in str(code) if ch.isdigit())[:6]
    if digits.startswith(("4", "8", "920")):
        return Board.BSE
    if digits.startswith(("688", "689")):
        return Board.STAR
    if digits.startswith(("300", "301")):
        return Board.CHINEXT
    return Board.MAIN


@dataclass(frozen=True)
class DailyQuote:
    code: str
    trade_date: date | str
    pre_close: Decimal | float
    open: Decimal | float
    high: Decimal | float
    low: Decimal | float
    close: Decimal | float
    volume: Decimal | float
    board: Board | str | None = None
    is_st: bool = False
    suspended: bool | None = None
    has_price_limit: bool | None = None
    limit_pct: Decimal | float | None = None
    limit_up_price: Decimal | float | None = None
    limit_down_price: Decimal | float | None = None
    tick_size: Decimal | float = DEFAULT_TICK


@dataclass(frozen=True)
class PositionState:
    sellable_qty: int = 0
    frozen_today_buy: int = 0

    @property
    def total_qty(self) -> int:
        return self.sellable_qty + self.frozen_today_buy


@dataclass(frozen=True)
class DailyTradingState:
    code: str
    trade_date: date
    suspended: bool
    limit_up_price: Decimal | None
    limit_down_price: Decimal | None
    # ``limit_up/down`` mean the daily bar touched the corresponding band.
    limit_up: bool
    limit_down: bool
    one_price_limit_up: bool
    one_price_limit_down: bool
    # Bar-level liquidity flags: buying/selling is impossible for a locked bar.
    limit_buy: bool
    limit_sell: bool
    sellable_qty: int
    frozen_today_buy: int

    @property
    def total_qty(self) -> int:
        return self.sellable_qty + self.frozen_today_buy


@dataclass(frozen=True)
class TradeDecision:
    allowed: bool
    reason: str
    side: Side
    execution_price: Decimal
    quantity: int


def _round_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    units = (value / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return units * tick


def _default_limit_pct(quote: DailyQuote) -> Decimal:
    if quote.limit_pct is not None:
        pct = _decimal(quote.limit_pct, "limit_pct")
        if pct <= ZERO:
            raise ValueError("limit_pct must be positive")
        return pct
    if quote.is_st:
        return Decimal("0.05")
    board = Board(quote.board) if quote.board is not None else infer_current_board(quote.code)
    return {
        Board.MAIN: Decimal("0.10"),
        Board.CHINEXT: Decimal("0.20"),
        Board.STAR: Decimal("0.20"),
        Board.BSE: Decimal("0.30"),
    }[board]


def price_limits(quote: DailyQuote) -> tuple[Decimal | None, Decimal | None]:
    """Return exact upper/lower prices, honoring explicit historical inputs."""
    tick = _decimal(quote.tick_size, "tick_size")
    if tick <= ZERO:
        raise ValueError("tick_size must be positive")

    explicit_up = (
        _decimal(quote.limit_up_price, "limit_up_price")
        if quote.limit_up_price is not None
        else None
    )
    explicit_down = (
        _decimal(quote.limit_down_price, "limit_down_price")
        if quote.limit_down_price is not None
        else None
    )
    if (explicit_up is None) != (explicit_down is None):
        raise ValueError("limit_up_price and limit_down_price must be supplied together")
    if explicit_up is not None:
        if explicit_down <= ZERO or explicit_up <= explicit_down:
            raise ValueError("explicit price limits are invalid")
        return explicit_up, explicit_down

    if quote.has_price_limit is False:
        return None, None

    pre_close = _decimal(quote.pre_close, "pre_close")
    if pre_close <= ZERO:
        raise ValueError("pre_close must be positive when price limits are enabled")
    pct = _default_limit_pct(quote)
    return (
        _round_to_tick(pre_close * (Decimal("1") + pct), tick),
        _round_to_tick(pre_close * (Decimal("1") - pct), tick),
    )


def _position_state(position: PositionState | Mapping[str, int] | None) -> PositionState:
    if position is None:
        return PositionState()
    if isinstance(position, PositionState):
        result = position
    else:
        result = PositionState(
            sellable_qty=int(position.get("sellable_qty", 0)),
            frozen_today_buy=int(position.get("frozen_today_buy", 0)),
        )
    if result.sellable_qty < 0 or result.frozen_today_buy < 0:
        raise ValueError("position quantities cannot be negative")
    return result


def build_daily_state(
    quote: DailyQuote,
    position: PositionState | Mapping[str, int] | None = None,
) -> DailyTradingState:
    """Convert a raw daily quote and position snapshot into trading state."""
    tick = _decimal(quote.tick_size, "tick_size")
    prices = {
        name: _decimal(getattr(quote, name), name)
        for name in ("open", "high", "low", "close")
    }
    pre_close = _decimal(quote.pre_close, "pre_close")
    volume = _decimal(quote.volume, "volume")
    # A stale filled price with zero volume is still suspended/untradable.
    suspended = bool(quote.suspended) or volume <= ZERO or pre_close <= ZERO or any(
        value <= ZERO for value in prices.values()
    )
    has_exact_limits = quote.limit_up_price is not None or quote.limit_down_price is not None
    if pre_close <= ZERO and not has_exact_limits and quote.has_price_limit is not False:
        # A zero previous close is an unusable quote, not a reason to invent bands.
        up, down = None, None
    else:
        up, down = price_limits(quote)

    rounded = {name: _round_to_tick(value, tick) for name, value in prices.items()}
    touched_up = not suspended and up is not None and rounded["high"] >= up
    touched_down = not suspended and down is not None and rounded["low"] <= down
    one_up = not suspended and up is not None and all(value == up for value in rounded.values())
    one_down = not suspended and down is not None and all(value == down for value in rounded.values())
    pos = _position_state(position)
    return DailyTradingState(
        code=quote.code,
        trade_date=_trade_date(quote.trade_date),
        suspended=suspended,
        limit_up_price=up,
        limit_down_price=down,
        limit_up=bool(touched_up),
        limit_down=bool(touched_down),
        one_price_limit_up=bool(one_up),
        one_price_limit_down=bool(one_down),
        limit_buy=bool(one_up),
        limit_sell=bool(one_down),
        sellable_qty=pos.sellable_qty,
        frozen_today_buy=pos.frozen_today_buy,
    )


def check_trade(
    state: DailyTradingState,
    side: Side | str,
    execution_price: Decimal | float,
    quantity: int,
) -> TradeDecision:
    """Apply suspension, band, direction, board-liquidity, lot, and T+1 checks."""
    side = Side(side)
    price = _decimal(execution_price, "execution_price")
    try:
        qty = int(quantity)
    except (TypeError, ValueError) as exc:
        raise ValueError("quantity must be an integer") from exc

    def decision(allowed: bool, reason: str) -> TradeDecision:
        return TradeDecision(allowed, reason, side, price, qty)

    if qty <= 0 or qty != quantity:
        return decision(False, "invalid_quantity")
    if price <= ZERO:
        return decision(False, "invalid_execution_price")
    if state.suspended:
        return decision(False, "suspended")

    if state.limit_up_price is not None and price > state.limit_up_price:
        return decision(False, "price_above_limit_up")
    if state.limit_down_price is not None and price < state.limit_down_price:
        return decision(False, "price_below_limit_down")

    if side is Side.BUY:
        if qty % 100:
            return decision(False, "buy_not_board_lot")
        if state.limit_buy:
            return decision(False, "one_price_limit_up")
        if state.limit_up_price is not None and price == state.limit_up_price:
            return decision(False, "buy_at_limit_up")
    else:
        if qty > state.sellable_qty:
            return decision(False, "insufficient_sellable_qty")
        # Odd-lot liquidation is accepted only when the entire position is sold.
        if qty % 100 and not (qty == state.total_qty and state.frozen_today_buy == 0):
            return decision(False, "sell_not_board_lot")
        if state.limit_sell:
            return decision(False, "one_price_limit_down")
        if state.limit_down_price is not None and price == state.limit_down_price:
            return decision(False, "sell_at_limit_down")
    return decision(True, "ok")


class T1PositionLedger:
    """Minimal long-only A-share T+1 inventory ledger.

    ``advance`` is called with trading dates, not calendar dates.  Buys become
    sellable on the next advanced trading date.
    """

    def __init__(self, initial: Mapping[str, int] | None = None) -> None:
        self._trade_date: date | None = None
        self._positions: dict[str, list[int]] = {}
        for code, quantity in (initial or {}).items():
            qty = int(quantity)
            if qty < 0:
                raise ValueError("initial quantity cannot be negative")
            self._positions[str(code)] = [qty, 0]

    @property
    def trade_date(self) -> date | None:
        return self._trade_date

    def advance(self, value: date | str) -> None:
        next_date = _trade_date(value)
        if self._trade_date is not None and next_date < self._trade_date:
            raise ValueError("trade dates must be monotonic")
        if self._trade_date is not None and next_date > self._trade_date:
            for quantities in self._positions.values():
                quantities[0] += quantities[1]
                quantities[1] = 0
        self._trade_date = next_date

    def position(self, code: str) -> PositionState:
        sellable, frozen = self._positions.get(str(code), [0, 0])
        return PositionState(sellable, frozen)

    def state(self, quote: DailyQuote) -> DailyTradingState:
        quote_date = _trade_date(quote.trade_date)
        if self._trade_date != quote_date:
            raise ValueError("ledger must be advanced to the quote trade date")
        return build_daily_state(quote, self.position(quote.code))

    def apply(
        self,
        quote: DailyQuote,
        side: Side | str,
        execution_price: Decimal | float,
        quantity: int,
    ) -> TradeDecision:
        side = Side(side)
        state = self.state(quote)
        result = check_trade(state, side, execution_price, quantity)
        if not result.allowed:
            return result
        quantities = self._positions.setdefault(str(quote.code), [0, 0])
        if side is Side.BUY:
            quantities[1] += result.quantity
        else:
            quantities[0] -= result.quantity
        return result
