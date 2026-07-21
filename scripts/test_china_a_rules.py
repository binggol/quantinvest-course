from decimal import Decimal

import pytest

from scripts.backtest_engine.china_a_rules import (
    Board,
    DailyQuote,
    PositionState,
    Side,
    T1PositionLedger,
    build_daily_state,
    check_trade,
    price_limits,
)


def quote(code="600000", **overrides):
    values = {
        "code": code,
        "trade_date": "2026-07-10",
        "pre_close": 10,
        "open": 10.20,
        "high": 10.80,
        "low": 10.10,
        "close": 10.50,
        "volume": 1_000_000,
    }
    values.update(overrides)
    return DailyQuote(**values)


@pytest.mark.parametrize(
    ("item", "expected_up", "expected_down"),
    [
        (quote("600000", board=Board.MAIN), "11.00", "9.00"),
        (quote("600000", board=Board.MAIN, is_st=True), "10.50", "9.50"),
        (quote("300001", board=Board.CHINEXT), "12.00", "8.00"),
        (quote("688001", board=Board.STAR), "12.00", "8.00"),
        (quote("920001", board=Board.BSE), "13.00", "7.00"),
    ],
)
def test_board_and_st_price_limits(item, expected_up, expected_down):
    assert price_limits(item) == (Decimal(expected_up), Decimal(expected_down))


def test_explicit_historical_regime_and_ipo_window_take_priority():
    # A ChiNext symbol can still use a historical 10% regime when the data says so.
    historical = quote("300001", board=Board.CHINEXT, limit_pct=0.10)
    assert price_limits(historical) == (Decimal("11.00"), Decimal("9.00"))

    exact = quote(
        "300001",
        is_st=True,
        limit_pct=0.20,
        limit_up_price=10.73,
        limit_down_price=8.77,
    )
    assert price_limits(exact) == (Decimal("10.73"), Decimal("8.77"))

    no_limit = quote("688001", has_price_limit=False)
    assert price_limits(no_limit) == (None, None)


@pytest.mark.parametrize(
    "overrides",
    [
        {"volume": 0},
        {"pre_close": 0},
        {"open": 0},
        {"close": 0},
        {"suspended": True},
    ],
)
def test_zero_price_or_volume_and_explicit_flag_are_suspended(overrides):
    state = build_daily_state(quote(**overrides))
    assert state.suspended
    assert not check_trade(state, Side.BUY, 10, 100).allowed
    assert check_trade(state, Side.BUY, 10, 100).reason == "suspended"


def test_direction_and_execution_price_at_limit_are_checked_separately():
    state = build_daily_state(
        quote(open=10.20, high=11, low=10.10, close=11),
        PositionState(sellable_qty=500),
    )
    assert state.limit_up
    assert not state.one_price_limit_up
    assert check_trade(state, Side.BUY, 11, 100).reason == "buy_at_limit_up"
    assert check_trade(state, Side.BUY, 10.99, 100).allowed
    assert check_trade(state, Side.SELL, 11, 100).allowed
    assert check_trade(state, Side.BUY, 11.01, 100).reason == "price_above_limit_up"


def test_one_price_limit_up_blocks_buy_and_one_price_limit_down_blocks_sell():
    locked_up = build_daily_state(
        quote(open=11, high=11, low=11, close=11),
        PositionState(sellable_qty=500),
    )
    assert locked_up.one_price_limit_up and locked_up.limit_buy
    assert check_trade(locked_up, Side.BUY, 10.99, 100).reason == "one_price_limit_up"
    assert check_trade(locked_up, Side.SELL, 11, 100).allowed

    locked_down = build_daily_state(
        quote(open=9, high=9, low=9, close=9),
        PositionState(sellable_qty=500),
    )
    assert locked_down.one_price_limit_down and locked_down.limit_sell
    assert check_trade(locked_down, Side.SELL, 9.01, 100).reason == "one_price_limit_down"
    assert check_trade(locked_down, Side.BUY, 9, 100).allowed


def test_lot_size_and_t1_ledger_golden_path():
    ledger = T1PositionLedger({"600000": 500})
    day_one = quote(trade_date="2026-07-10")
    ledger.advance("2026-07-10")

    assert ledger.apply(day_one, Side.BUY, 10.30, 150).reason == "buy_not_board_lot"
    assert ledger.apply(day_one, Side.BUY, 10.30, 200).allowed
    assert ledger.position("600000") == PositionState(500, 200)
    assert ledger.state(day_one).sellable_qty == 500
    assert ledger.state(day_one).frozen_today_buy == 200
    assert ledger.apply(day_one, Side.SELL, 10.30, 600).reason == "insufficient_sellable_qty"
    assert ledger.apply(day_one, Side.SELL, 10.30, 500).allowed
    assert ledger.position("600000") == PositionState(0, 200)

    day_two = quote(trade_date="2026-07-13")
    ledger.advance("2026-07-13")
    assert ledger.position("600000") == PositionState(200, 0)
    assert ledger.apply(day_two, Side.SELL, 10.30, 200).allowed
    assert ledger.position("600000") == PositionState(0, 0)


def test_sell_allows_full_odd_lot_liquidation_but_not_partial_odd_lot():
    state = build_daily_state(quote(), PositionState(sellable_qty=150))
    assert check_trade(state, Side.SELL, 10.30, 50).reason == "sell_not_board_lot"
    assert check_trade(state, Side.SELL, 10.30, 150).allowed


def test_ledger_rejects_stale_quote_and_backwards_date():
    ledger = T1PositionLedger()
    ledger.advance("2026-07-13")
    with pytest.raises(ValueError, match="advanced"):
        ledger.state(quote(trade_date="2026-07-10"))
    with pytest.raises(ValueError, match="monotonic"):
        ledger.advance("2026-07-10")
