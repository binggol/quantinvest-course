from __future__ import annotations

from collections import defaultdict
from datetime import date

import pandas as pd
import pytest
from qlib.backtest.decision import Order
from qlib.backtest.exchange import Exchange
from qlib.backtest.position import Position
from qlib.config import C

from scripts.backtest_engine.qlib_adapter import (
    ChinaAExchange,
    CorporateAction,
    EventTargetWeightStrategy,
    FeeRate,
    HistoricalFeeSchedule,
    apply_adjustment_factors,
    derive_market_states,
)


class PositionBackedStrategy(EventTargetWeightStrategy):
    def __init__(self, position, *args, **kwargs):
        self._test_position = position
        super().__init__(*args, **kwargs)

    @property
    def trade_position(self):
        return self._test_position


class FactorOnlyExchange:
    def __init__(self, factors):
        self.factors = factors
        self.execution_audit = []

    def get_factor(self, code, start_time, end_time):
        return self.factors[code]


class StrategyExchange(FactorOnlyExchange):
    def __init__(self, prices, factors=None):
        super().__init__(factors or {code: 1.0 for code in prices})
        self.prices = prices

    def get_deal_price(self, code, start_time, end_time, direction):
        value = self.prices[code]
        if isinstance(value, dict):
            return value[pd.Timestamp(start_time).strftime("%Y-%m-%d")]
        return value

    def get_real_deal_amount(self, current, target, factor):
        return float(target) - float(current)


class PreviewStrategyExchange(StrategyExchange):
    def __init__(self, prices, *, sell_fill_ratio=1.0):
        super().__init__(prices)
        self.sell_fill_ratio = float(sell_fill_ratio)
        self._frozen_buys = defaultdict(float, {"SH600099": 7.0})
        self._frozen_trade_date = date(2026, 7, 9)

    def deal_order(self, order, trade_account=None, position=None, dealt_order_amount=None):
        trade_date = pd.Timestamp(order.start_time).date()
        if self._frozen_trade_date != trade_date:
            self._frozen_buys.clear()
            self._frozen_trade_date = trade_date
        price = float(self.get_deal_price(order.stock_id, order.start_time, order.end_time, order.direction))
        ratio = self.sell_fill_ratio if order.direction == Order.SELL else 1.0
        order.deal_amount = float(order.amount) * ratio
        trade_value = float(order.deal_amount) * price
        if order.deal_amount > 0:
            position.update_order(order, trade_value, 0.0, price)
            if order.direction == Order.BUY:
                self._frozen_buys[order.stock_id] += float(order.deal_amount)
        self.execution_audit.append(
            {
                "trade_date": trade_date.isoformat(),
                "instrument": order.stock_id,
                "side": "buy" if order.direction == Order.BUY else "sell",
                "reason": "filled" if ratio == 1.0 else "partial",
                "trade_value": trade_value,
                "trade_cost": 0.0,
            }
        )
        return trade_value, 0.0, price


def quote_frame(rows):
    frame = pd.DataFrame(rows)
    frame["datetime"] = pd.to_datetime(frame["datetime"])
    return frame.set_index(["instrument", "datetime"]).sort_index()


def test_market_states_use_zero_volume_even_when_prices_are_filled():
    quotes = quote_frame(
        [
            {
                "instrument": "SH600000",
                "datetime": "2026-07-10",
                "$open": 10,
                "$high": 10,
                "$low": 10,
                "$close": 10,
                "$change": 0,
                "$factor": 1,
                "$volume": 0,
            }
        ]
    )
    states = derive_market_states(quotes, buy_price_field="$open", sell_price_field="$open")
    row = states.iloc[0]
    assert row["suspended"]
    assert row["limit_buy"] and row["limit_sell"]


def test_market_states_block_missing_change_instead_of_guessing_limits():
    quotes = quote_frame(
        [
            {
                "instrument": "SH600000",
                "datetime": "2026-07-10",
                "$open": 10,
                "$high": 10,
                "$low": 10,
                "$close": 10,
                "$change": None,
                "$factor": 1,
                "$volume": 1000,
            }
        ]
    )
    states = derive_market_states(quotes, buy_price_field="$open", sell_price_field="$open")

    assert states.iloc[0]["suspended"]


@pytest.mark.parametrize(
    ("code", "trade_date", "open_price", "expected_pct", "buy_blocked"),
    [
        ("SZ300001", "2020-08-21", 11.0, 0.10, True),
        ("SZ300001", "2020-08-24", 11.0, 0.20, False),
        ("SH688001", "2026-07-10", 12.0, 0.20, True),
        ("BJ920001", "2026-07-10", 13.0, 0.30, True),
    ],
)
def test_board_fallback_and_chinext_regime_boundary(
    code, trade_date, open_price, expected_pct, buy_blocked
):
    quotes = quote_frame(
        [
            {
                "instrument": code,
                "datetime": trade_date,
                "$open": open_price,
                "$high": open_price,
                "$low": min(10.0, open_price),
                "$close": open_price,
                "$change": open_price / 10.0 - 1.0,
                "$factor": 1,
                "$volume": 1000,
            }
        ]
    )
    states = derive_market_states(quotes, buy_price_field="$open", sell_price_field="$open")
    row = states.iloc[0]
    assert row["limit_pct"] == pytest.approx(expected_pct)
    assert bool(row["limit_buy"]) is buy_blocked


def test_explicit_st_and_ipo_overrides_take_priority():
    quotes = quote_frame(
        [
            {
                "instrument": "SH600001",
                "datetime": "2026-07-10",
                "$open": 10.5,
                "$high": 10.5,
                "$low": 10.5,
                "$close": 10.5,
                "$change": 0.05,
                "$factor": 1,
                "$volume": 1000,
            },
            {
                "instrument": "SH688999",
                "datetime": "2026-07-10",
                "$open": 15,
                "$high": 16,
                "$low": 14,
                "$close": 15,
                "$change": 0.5,
                "$factor": 1,
                "$volume": 1000,
            },
        ]
    )
    overrides = pd.DataFrame(
        {
            "limit_pct": [0.05, None],
            "limit_buy": [None, False],
            "limit_sell": [None, False],
        },
        index=quotes.index,
    )
    states = derive_market_states(
        quotes,
        buy_price_field="$open",
        sell_price_field="$open",
        overrides=overrides,
    )
    assert states.loc[("SH600001", pd.Timestamp("2026-07-10")), "limit_buy"]
    assert not states.loc[("SH688999", pd.Timestamp("2026-07-10")), "limit_buy"]
    assert states.loc[("SH688999", pd.Timestamp("2026-07-10")), "rule_source"] == "explicit"


def test_historical_fee_schedule_never_uses_a_future_rate():
    schedule = HistoricalFeeSchedule(
        [FeeRate("2022-01-01", 0.0003, 0.0013), FeeRate("2023-08-28", 0.0003, 0.0008)]
    )
    assert schedule.at("2023-08-27").close_cost == pytest.approx(0.0013)
    assert schedule.at("2023-08-28").close_cost == pytest.approx(0.0008)
    with pytest.raises(ValueError, match="no fee rate"):
        schedule.at("2021-12-31")


def test_strategy_validates_and_normalizes_target_weights():
    strategy = EventTargetWeightStrategy(
        target_weights={"2026-07-10": {"600000.SH": 0.5, "000001.SZ": 0.5}},
        risk_degree=1,
    )
    assert strategy.target_weights == {
        date(2026, 7, 10): {"SH600000": 0.5, "SZ000001": 0.5}
    }
    with pytest.raises(ValueError, match="exceed"):
        EventTargetWeightStrategy(target_weights={"2026-07-10": {"600000.SH": 1.1}})
    with pytest.raises(ValueError, match="rebalance_mode"):
        EventTargetWeightStrategy(target_weights={}, rebalance_mode="unknown")


def test_replace_only_preserves_intersection_and_caps_frozen_entrant_budget():
    position = Position(
        cash=0,
        position_dict={
            "SH600000": {"amount": 80.0, "price": 10.0},
            "SH600001": {"amount": 20.0, "price": 10.0},
        },
    )
    exchange = StrategyExchange(
        {
            "SH600000": 10.0,
            "SH600001": 10.0,
            "SH600002": {"2026-07-10": 10.0, "2026-07-13": 20.0},
        }
    )
    strategy = PositionBackedStrategy(
        position,
        target_weights={"2026-07-10": {"SH600000": 0.5, "SH600002": 0.5}},
        trade_exchange=exchange,
        risk_degree=0.95,
        rebalance_mode="replace_only",
    )
    day = pd.Timestamp("2026-07-10")
    strategy._start_target(day.date(), day, day)

    assert strategy._replace_preserved_codes == {"SH600000"}
    assert strategy._replace_exit_codes == {"SH600001"}
    assert strategy._replace_entrant_codes == {"SH600002"}
    assert strategy._replace_entrant_budgets == {"SH600002": pytest.approx(150.0)}
    assert "SH600000" not in strategy._pending_codes
    assert "SH600002" not in strategy._desired_amounts
    first_orders = strategy._orders_for_pending(day, day)
    assert [(order.stock_id, order.direction) for order in first_orders] == [
        ("SH600001", Order.SELL)
    ]

    failed_sell = first_orders[0]
    failed_sell.deal_amount = 0.0
    exchange.execution_audit.append(
        {
            "trade_date": "2026-07-10",
            "instrument": "SH600001",
            "side": "sell",
            "reason": "suspended",
            "trade_value": 0.0,
        }
    )
    strategy._consume_previous_result([(failed_sell,)])
    assert strategy.strategy_audit[-1]["will_retry"]
    assert strategy.strategy_audit[-1]["execution_reason"] == "suspended"
    assert all(
        order.stock_id == "SH600001"
        for order in strategy._orders_for_pending(pd.Timestamp("2026-07-11"), pd.Timestamp("2026-07-11"))
    )

    successful_sell = strategy._orders_for_pending(
        pd.Timestamp("2026-07-11"), pd.Timestamp("2026-07-11")
    )[0]
    successful_sell.deal_amount = successful_sell.amount
    exchange.execution_audit.append(
        {
            "trade_date": "2026-07-11",
            "instrument": "SH600001",
            "side": "sell",
            "reason": "filled",
            "trade_value": 200.0,
        }
    )
    position._del_stock("SH600001")
    strategy._consume_previous_result([(successful_sell,)])
    buy_day = pd.Timestamp("2026-07-13")
    buy_orders = strategy._orders_for_pending(buy_day, buy_day)
    assert len(buy_orders) == 1
    assert buy_orders[0].stock_id == "SH600002"
    assert buy_orders[0].direction == Order.BUY
    assert buy_orders[0].amount == pytest.approx(7.5)
    assert buy_orders[0].amount * 20.0 == pytest.approx(150.0)


def test_replace_only_returns_same_day_sells_and_buys_after_clean_exit_preview():
    position = Position(
        cash=0,
        position_dict={
            "SH600000": {"amount": 80.0, "price": 10.0},
            "SH600001": {"amount": 20.0, "price": 10.0},
        },
    )
    exchange = PreviewStrategyExchange(
        {"SH600000": 10.0, "SH600001": 10.0, "SH600002": 10.0}
    )
    exchange.execution_audit.append({"sentinel": True})
    frozen_before = dict(exchange._frozen_buys)
    frozen_date_before = exchange._frozen_trade_date
    strategy = PositionBackedStrategy(
        position,
        target_weights={"2026-07-10": {"SH600000": 0.5, "SH600002": 0.5}},
        trade_exchange=exchange,
        risk_degree=0.95,
        rebalance_mode="replace_only",
    )
    day = pd.Timestamp("2026-07-10")
    strategy._start_target(day.date(), day, day)
    value_before = position.calculate_value()
    orders = strategy._orders_for_pending(day, day)

    assert [(order.stock_id, order.direction) for order in orders] == [
        ("SH600001", Order.SELL),
        ("SH600002", Order.BUY),
    ]
    assert orders[1].amount == pytest.approx(15.0)
    assert position.calculate_value() == pytest.approx(value_before)
    assert position.check_stock("SH600001")
    assert not position.check_stock("SH600002")
    assert exchange.execution_audit == [{"sentinel": True}]
    assert dict(exchange._frozen_buys) == frozen_before
    assert exchange._frozen_trade_date == frozen_date_before
    assert strategy.strategy_audit[-1]["full_fill"]
    assert strategy.strategy_audit[-1]["same_day_entrants"]

    results = []
    for order in orders:
        exchange.deal_order(order, position=position)
        results.append((order,))
    strategy._consume_previous_result(results)

    assert position.get_stock_amount("SH600000") == pytest.approx(80.0)
    assert not position.check_stock("SH600001")
    assert position.get_stock_amount("SH600002") == pytest.approx(15.0)
    assert position.get_cash() == pytest.approx(50.0)
    assert strategy._pending_codes == set()
    entrant_audit = next(
        item
        for item in reversed(strategy.strategy_audit)
        if item.get("status") == "replace_order_result" and item.get("role") == "entrant"
    )
    assert entrant_audit["frozen_budget"] == pytest.approx(150.0)
    assert entrant_audit["spent_budget"] == pytest.approx(150.0)


def test_replace_only_partial_exit_preview_returns_no_same_day_buy_and_is_side_effect_free():
    position = Position(
        cash=0,
        position_dict={
            "SH600000": {"amount": 80.0, "price": 10.0},
            "SH600001": {"amount": 20.0, "price": 10.0},
        },
    )
    exchange = PreviewStrategyExchange(
        {"SH600000": 10.0, "SH600001": 10.0, "SH600002": 10.0},
        sell_fill_ratio=0.5,
    )
    exchange.execution_audit.append({"sentinel": True})
    frozen_before = dict(exchange._frozen_buys)
    frozen_date_before = exchange._frozen_trade_date
    strategy = PositionBackedStrategy(
        position,
        target_weights={"2026-07-10": {"SH600000": 0.5, "SH600002": 0.5}},
        trade_exchange=exchange,
        risk_degree=0.95,
        rebalance_mode="replace_only",
    )
    day = pd.Timestamp("2026-07-10")
    strategy._start_target(day.date(), day, day)
    orders = strategy._orders_for_pending(day, day)

    assert [(order.stock_id, order.direction) for order in orders] == [
        ("SH600001", Order.SELL)
    ]
    assert position.get_stock_amount("SH600001") == pytest.approx(20.0)
    assert not position.check_stock("SH600002")
    assert exchange.execution_audit == [{"sentinel": True}]
    assert dict(exchange._frozen_buys) == frozen_before
    assert exchange._frozen_trade_date == frozen_date_before
    assert not strategy.strategy_audit[-1]["full_fill"]
    assert not strategy.strategy_audit[-1]["same_day_entrants"]


def test_replace_only_retries_partial_buy_against_remaining_currency_budget():
    position = Position(cash=1_000)
    exchange = StrategyExchange(
        {"SH600002": {"2026-07-10": 10.0, "2026-07-11": 20.0}}
    )
    strategy = PositionBackedStrategy(
        position,
        target_weights={"2026-07-10": {"SH600002": 0.5}},
        trade_exchange=exchange,
        risk_degree=1.0,
        rebalance_mode="replace_only",
    )
    first_day = pd.Timestamp("2026-07-10")
    strategy._start_target(first_day.date(), first_day, first_day)
    first_buy = strategy._orders_for_pending(first_day, first_day)[0]
    assert first_buy.amount == pytest.approx(50.0)
    first_buy.deal_amount = 20.0
    position._init_stock("SH600002", 20.0, 10.0)
    exchange.execution_audit.append(
        {
            "trade_date": "2026-07-10",
            "instrument": "SH600002",
            "side": "buy",
            "reason": "partial",
            "trade_value": 200.0,
        }
    )
    strategy._consume_previous_result([(first_buy,)])
    audit = strategy.strategy_audit[-1]
    assert audit["outcome"] == "partial"
    assert audit["frozen_budget"] == pytest.approx(500.0)
    assert audit["spent_budget"] == pytest.approx(200.0)
    assert audit["will_retry"]

    second_day = pd.Timestamp("2026-07-11")
    second_buy = strategy._orders_for_pending(second_day, second_day)[0]
    assert second_buy.amount == pytest.approx(15.0)
    assert second_buy.amount * 20.0 == pytest.approx(300.0)


def test_replace_only_audits_cash_constrained_buy_and_keeps_it_pending():
    position = Position(cash=1_000)
    exchange = StrategyExchange({"SH600002": 10.0})
    strategy = PositionBackedStrategy(
        position,
        target_weights={"2026-07-10": {"SH600002": 1.0}},
        trade_exchange=exchange,
        risk_degree=1.0,
        rebalance_mode="replace_only",
    )
    day = pd.Timestamp("2026-07-10")
    strategy._start_target(day.date(), day, day)
    buy = strategy._orders_for_pending(day, day)[0]
    buy.deal_amount = 0.0
    exchange.execution_audit.append(
        {
            "trade_date": "2026-07-10",
            "instrument": "SH600002",
            "side": "buy",
            "reason": "cash_or_volume",
            "trade_value": 0.0,
        }
    )
    strategy._consume_previous_result([(buy,)])

    audit = strategy.strategy_audit[-1]
    assert audit["role"] == "entrant"
    assert audit["side"] == "buy"
    assert audit["outcome"] == "unfilled"
    assert audit["execution_reason"] == "cash_or_volume"
    assert audit["will_retry"]
    assert strategy._pending_codes == {"SH600002"}


def test_replace_only_empty_target_strictly_clears_every_holding():
    position = Position(
        cash=100,
        position_dict={
            "SH600000": {"amount": 10.0, "price": 10.0},
            "SH600001": {"amount": 20.0, "price": 5.0},
        },
    )
    exchange = StrategyExchange({"SH600000": 10.0, "SH600001": 5.0})
    strategy = PositionBackedStrategy(
        position,
        target_weights={"2026-07-10": {}},
        trade_exchange=exchange,
        rebalance_mode="replace_only",
    )
    day = pd.Timestamp("2026-07-10")
    strategy._start_target(day.date(), day, day)
    orders = strategy._orders_for_pending(day, day)

    assert strategy._replace_preserved_codes == set()
    assert strategy._replace_entrant_codes == set()
    assert {order.stock_id for order in orders} == {"SH600000", "SH600001"}
    assert all(order.direction == Order.SELL for order in orders)


def test_strategy_can_extend_retries_for_only_the_liquidation_target():
    strategy = EventTargetWeightStrategy(
        target_weights={"2026-07-10": {"600000.SH": 1.0}, "2026-07-17": {}},
        retry_days=5,
        retry_days_by_target={"2026-07-17": 30},
    )

    assert strategy.retry_days == 5
    assert strategy.retry_days_by_target == {date(2026, 7, 17): 30}
    with pytest.raises(ValueError, match="no target"):
        EventTargetWeightStrategy(
            target_weights={"2026-07-10": {}},
            retry_days_by_target={"2026-07-11": 30},
        )


def test_corporate_action_converts_raw_shares_value_neutrally_without_cost():
    position = Position(
        cash=1_000,
        position_dict={
            "SH601989": {"amount": 1_000.0, "price": 10.0},
            "SH600150": {"amount": 200.0, "price": 20.0},
        },
    )
    exchange = FactorOnlyExchange({"SH600150": 0.5})
    strategy = PositionBackedStrategy(
        position,
        target_weights={"2025-09-16": {}},
        trade_exchange=exchange,
        corporate_actions=[
            CorporateAction(
                from_code="SH601989",
                to_code="SH600150",
                effective_date="2025-09-16",
                raw_share_ratio=0.5,
                from_factor=0.8,
            )
        ],
    )
    cash_before = position.get_cash(include_settle=True)
    value_before = position.calculate_value()

    strategy._apply_corporate_actions(
        date(2025, 9, 16), pd.Timestamp("2025-09-16"), pd.Timestamp("2025-09-16")
    )

    assert not position.check_stock("SH601989")
    assert position.get_stock_amount("SH600150") == pytest.approx(1_000.0)
    assert position.get_stock_price("SH600150") == pytest.approx(14.0)
    assert position.get_cash(include_settle=True) == cash_before
    assert position.calculate_value() == pytest.approx(value_before)
    assert exchange.execution_audit == []
    audit = strategy.strategy_audit[-1]
    assert audit["status"] == "corporate_action_converted"
    assert audit["from_raw_shares"] == pytest.approx(800.0)
    assert audit["converted_raw_shares"] == pytest.approx(400.0)
    assert audit["trade_cost"] == 0.0


def test_corporate_action_maps_a_retired_target_to_the_successor_code():
    position = Position(cash=1_000)
    strategy = PositionBackedStrategy(
        position,
        target_weights={"2025-09-16": {"SH601989": 1.0}},
        trade_exchange=FactorOnlyExchange({"SH600150": 1.0}),
        corporate_actions=[
            {
                "from_code": "SH601989",
                "to_code": "SH600150",
                "effective_date": "2025-09-16",
                "raw_share_ratio": 0.1339,
                "from_factor": 1.0,
            }
        ],
    )

    strategy._start_target(
        date(2025, 9, 16), pd.Timestamp("2025-09-16"), pd.Timestamp("2025-09-16")
    )

    assert strategy._active_weights == {"SH600150": 1.0}
    assert strategy._pending_codes == {"SH600150"}
    assert strategy.strategy_audit[-1]["status"] == "retired_target_mapped"
    assert strategy.strategy_audit[-1]["mappings"] == [
        {
            "from_code": "SH601989",
            "to_code": "SH600150",
            "weight": 1.0,
            "effective_dates": ["2025-09-16"],
        }
    ]


def test_replace_only_corporate_action_preserves_a_retained_position_role():
    position = Position(
        cash=100,
        position_dict={"SH601989": {"amount": 100.0, "price": 10.0}},
    )
    exchange = StrategyExchange({"SH601989": 10.0, "SH600150": 10.0})
    strategy = PositionBackedStrategy(
        position,
        target_weights={"2025-09-15": {"SH601989": 1.0}},
        trade_exchange=exchange,
        rebalance_mode="replace_only",
        corporate_actions=[
            CorporateAction("SH601989", "SH600150", "2025-09-16", 1.0, 1.0)
        ],
    )
    start = pd.Timestamp("2025-09-15")
    strategy._start_target(start.date(), start, start)
    effective = pd.Timestamp("2025-09-16")
    strategy._apply_corporate_actions(effective.date(), effective, effective)

    assert strategy._replace_preserved_codes == {"SH600150"}
    assert strategy._replace_exit_codes == set()
    assert strategy._replace_entrant_codes == set()
    assert strategy._pending_codes == set()
    assert strategy._active_weights == {"SH600150": 1.0}


def test_replace_only_corporate_action_keeps_converted_exit_pending():
    position = Position(
        cash=100,
        position_dict={
            "SH601989": {"amount": 100.0, "price": 10.0},
            "SH600150": {"amount": 50.0, "price": 20.0},
        },
    )
    exchange = StrategyExchange({"SH601989": 10.0, "SH600150": 10.0})
    strategy = PositionBackedStrategy(
        position,
        target_weights={"2025-09-15": {"SH600150": 1.0}},
        trade_exchange=exchange,
        rebalance_mode="replace_only",
        corporate_actions=[
            CorporateAction("SH601989", "SH600150", "2025-09-16", 1.0, 1.0)
        ],
    )
    start = pd.Timestamp("2025-09-15")
    strategy._start_target(start.date(), start, start)
    effective = pd.Timestamp("2025-09-16")
    strategy._apply_corporate_actions(effective.date(), effective, effective)
    orders = strategy._orders_for_pending(effective, effective)

    assert strategy._replace_exit_codes == {"SH600150"}
    assert strategy._pending_codes == {"SH600150"}
    assert len(orders) == 1
    assert orders[0].stock_id == "SH600150"
    assert orders[0].direction == Order.SELL
    assert orders[0].amount == pytest.approx(100.0)


def test_replace_only_corporate_action_moves_a_pending_entrant_budget():
    position = Position(cash=1_000)
    exchange = StrategyExchange({"SH601989": 10.0, "SH600150": 20.0})
    strategy = PositionBackedStrategy(
        position,
        target_weights={"2025-09-15": {"SH601989": 0.5}},
        trade_exchange=exchange,
        risk_degree=1.0,
        rebalance_mode="replace_only",
        corporate_actions=[
            CorporateAction("SH601989", "SH600150", "2025-09-16", 1.0, 1.0)
        ],
    )
    start = pd.Timestamp("2025-09-15")
    strategy._start_target(start.date(), start, start)
    effective = pd.Timestamp("2025-09-16")
    strategy._apply_corporate_actions(effective.date(), effective, effective)
    orders = strategy._orders_for_pending(effective, effective)

    assert strategy._active_weights == {"SH600150": 0.5}
    assert strategy._replace_entrant_codes == {"SH600150"}
    assert strategy._replace_entrant_budgets == {"SH600150": pytest.approx(500.0)}
    assert strategy._pending_codes == {"SH600150"}
    assert len(orders) == 1
    assert orders[0].stock_id == "SH600150"
    assert orders[0].direction == Order.BUY
    assert orders[0].amount == pytest.approx(25.0)


def test_replace_only_exit_merged_into_entrant_sells_only_excess_target_shares():
    position = Position(
        cash=200,
        position_dict={"SH601989": {"amount": 100.0, "price": 10.0}},
    )
    exchange = StrategyExchange({"SH601989": 10.0, "SH600150": 20.0})
    strategy = PositionBackedStrategy(
        position,
        target_weights={"2025-09-15": {"SH600150": 0.5}},
        trade_exchange=exchange,
        risk_degree=1.0,
        rebalance_mode="replace_only",
        corporate_actions=[
            CorporateAction("SH601989", "SH600150", "2025-09-16", 1.0, 1.0)
        ],
    )
    start = pd.Timestamp("2025-09-15")
    strategy._start_target(start.date(), start, start)
    partial_buy = Order(
        stock_id="SH600150",
        amount=10.0,
        direction=Order.BUY,
        start_time=start,
        end_time=start,
    )
    partial_buy.deal_amount = 10.0
    position.update_order(partial_buy, 200.0, 0.0, 20.0)
    strategy._replace_entrant_spent["SH600150"] = 200.0

    effective = pd.Timestamp("2025-09-16")
    strategy._apply_corporate_actions(effective.date(), effective, effective)
    orders = strategy._orders_for_pending(effective, effective)

    assert position.get_stock_amount("SH600150") == pytest.approx(110.0)
    assert strategy._active_weights == {"SH600150": 0.5}
    assert strategy._replace_exit_codes == {"SH600150"}
    assert strategy._replace_entrant_codes == set()
    assert strategy._replace_entrant_spent["SH600150"] == pytest.approx(600.0)
    assert strategy._desired_amounts["SH600150"] == pytest.approx(30.0)
    assert len(orders) == 1
    assert orders[0].stock_id == "SH600150"
    assert orders[0].direction == Order.SELL
    assert orders[0].amount == pytest.approx(80.0)


def test_replace_only_effective_day_target_maps_converted_holding_to_preserved():
    position = Position(
        cash=0,
        position_dict={"SH601989": {"amount": 100.0, "price": 10.0}},
    )
    exchange = StrategyExchange({"SH601989": 10.0, "SH600150": 10.0})
    strategy = PositionBackedStrategy(
        position,
        target_weights={"2025-09-16": {"SH601989": 1.0}},
        trade_exchange=exchange,
        rebalance_mode="replace_only",
        corporate_actions=[
            CorporateAction("SH601989", "SH600150", "2025-09-16", 1.0, 1.0)
        ],
    )
    effective = pd.Timestamp("2025-09-16")
    strategy._apply_corporate_actions(effective.date(), effective, effective)
    strategy._start_target(effective.date(), effective, effective)

    assert not position.check_stock("SH601989")
    assert position.get_stock_amount("SH600150") == pytest.approx(100.0)
    assert strategy._active_weights == {"SH600150": 1.0}
    assert strategy._replace_preserved_codes == {"SH600150"}
    assert strategy._replace_exit_codes == set()
    assert strategy._replace_entrant_codes == set()
    assert strategy._pending_codes == set()
    assert strategy._desired_amounts is None
    mapped = [item for item in strategy.strategy_audit if item["status"] == "retired_target_mapped"]
    assert len(mapped) == 1


def test_exchange_normalizes_lot_volume_and_freezes_same_day_buys(monkeypatch):
    monkeypatch.setitem(C._config, "trade_unit", 100)
    monkeypatch.setitem(C._config, "region", "cn")
    quotes = quote_frame(
        [
            {
                "instrument": "SH600000",
                "datetime": "2026-07-10",
                "$open": 10.0,
                "$high": 10.2,
                "$low": 9.8,
                "$close": 10.0,
                "$change": 0.0,
                "$factor": 1.0,
                "$volume": 1_000.0,
            },
            {
                "instrument": "SH600000",
                "datetime": "2026-07-13",
                "$open": 10.0,
                "$high": 10.2,
                "$low": 9.8,
                "$close": 10.0,
                "$change": 0.0,
                "$factor": 1.0,
                "$volume": 1_000.0,
            },
        ]
    )

    def fake_quotes(self):
        self.quote_df = quotes.copy()
        self.trade_w_adj_price = False
        self._update_limit(self.limit_threshold)

    monkeypatch.setattr(Exchange, "get_quote_from_qlib", fake_quotes)
    exchange = ChinaAExchange(
        start_time="2026-07-10",
        end_time="2026-07-13",
        codes=["SH600000"],
        deal_price="open",
        limit_threshold=0.095,
        open_cost=0,
        close_cost=0,
        min_cost=0,
        trade_unit=100,
        volume_unit_multiplier=100,
    )
    assert exchange.get_volume("SH600000", pd.Timestamp("2026-07-10"), pd.Timestamp("2026-07-10")) == 100_000

    position = Position(cash=1_000_000)
    buy = Order(
        stock_id="SH600000",
        amount=100,
        direction=Order.BUY,
        start_time=pd.Timestamp("2026-07-10"),
        end_time=pd.Timestamp("2026-07-10"),
    )
    exchange.deal_order(buy, position=position)
    assert buy.deal_amount == 100

    same_day_sell = Order(
        stock_id="SH600000",
        amount=100,
        direction=Order.SELL,
        start_time=pd.Timestamp("2026-07-10"),
        end_time=pd.Timestamp("2026-07-10"),
    )
    exchange.deal_order(same_day_sell, position=position)
    assert same_day_sell.deal_amount == 0
    assert exchange.execution_audit[-1]["reason"] == "t1_frozen"

    next_day_sell = Order(
        stock_id="SH600000",
        amount=100,
        direction=Order.SELL,
        start_time=pd.Timestamp("2026-07-13"),
        end_time=pd.Timestamp("2026-07-13"),
    )
    exchange.deal_order(next_day_sell, position=position)
    assert next_day_sell.deal_amount == 100


def test_volume_cap_and_impact_cost_use_share_units(monkeypatch):
    monkeypatch.setitem(C._config, "trade_unit", 100)
    monkeypatch.setitem(C._config, "region", "cn")
    volume_limit_field = "0.1 * $volume * 100"
    quotes = quote_frame(
        [
            {
                "instrument": "SH600000",
                "datetime": "2026-07-10",
                "$open": 10.0,
                "$high": 10.2,
                "$low": 9.8,
                "$close": 10.0,
                "$change": 0.0,
                "$factor": 1.0,
                "$volume": 1_000.0,
                volume_limit_field: 10_000.0,
            }
        ]
    )

    def fake_quotes(self):
        self.quote_df = quotes.copy()
        self.trade_w_adj_price = False
        self._update_limit(self.limit_threshold)

    monkeypatch.setattr(Exchange, "get_quote_from_qlib", fake_quotes)
    exchange = ChinaAExchange(
        start_time="2026-07-10",
        end_time="2026-07-10",
        codes=["SH600000"],
        deal_price="open",
        limit_threshold=0.095,
        volume_threshold=("current", volume_limit_field),
        open_cost=0,
        close_cost=0,
        min_cost=0,
        impact_cost=0.1,
        trade_unit=100,
        volume_unit_multiplier=100,
    )
    position = Position(cash=1_000_000)
    order = Order(
        stock_id="SH600000",
        amount=20_000,
        direction=Order.BUY,
        start_time=pd.Timestamp("2026-07-10"),
        end_time=pd.Timestamp("2026-07-10"),
    )

    trade_value, trade_cost, _ = exchange.deal_order(order, position=position)

    assert order.deal_amount == 10_000
    assert trade_value == pytest.approx(100_000)
    assert trade_cost == pytest.approx(100)
    assert exchange.execution_audit[-1]["reason"] == "partial"


def test_adjusted_prices_use_normalized_share_units(monkeypatch):
    monkeypatch.setitem(C._config, "trade_unit", 100)
    monkeypatch.setitem(C._config, "region", "cn")
    volume_limit_field = "0.1 * $volume * 100"
    quotes = quote_frame(
        [
            {
                "instrument": "SH600000",
                "datetime": "2026-07-10",
                "$open": 5.0,
                "$high": 5.1,
                "$low": 4.9,
                "$close": 5.0,
                "$change": 0.0,
                "$factor": 1.0,
                "$adj": 2.0,
                "$volume": 1_000.0,
                volume_limit_field: 10_000.0,
            }
        ]
    )

    def fake_quotes(self):
        self.quote_df = quotes.copy()
        self.trade_w_adj_price = False
        self._update_limit(self.limit_threshold)

    monkeypatch.setattr(Exchange, "get_quote_from_qlib", fake_quotes)
    exchange = ChinaAExchange(
        start_time="2026-07-10",
        end_time="2026-07-10",
        codes=["SH600000"],
        deal_price="open",
        limit_threshold=0.095,
        volume_threshold=("current", volume_limit_field),
        open_cost=0,
        close_cost=0,
        min_cost=0,
        impact_cost=0.1,
        trade_unit=100,
        volume_unit_multiplier=100,
        adjustment_max_factors={"SH600000": 4.0},
    )
    trade_date = pd.Timestamp("2026-07-10")
    assert exchange.get_factor("SH600000", trade_date, trade_date) == pytest.approx(0.5)
    assert exchange.get_volume("SH600000", trade_date, trade_date) == pytest.approx(200_000)

    position = Position(cash=1_000_000)
    order = Order(
        stock_id="SH600000",
        amount=40_000,
        direction=Order.BUY,
        start_time=trade_date,
        end_time=trade_date,
    )
    trade_value, trade_cost, _ = exchange.deal_order(order, position=position)

    assert order.deal_amount == 20_000
    assert trade_value == pytest.approx(100_000)
    assert trade_cost == pytest.approx(100)
    assert exchange.execution_audit[-1]["deal_raw_shares"] == pytest.approx(10_000)


def test_adjustment_factor_restores_raw_tick_rounding_basis():
    quotes = quote_frame(
        [
            {
                "instrument": "SH600000",
                "datetime": "2026-07-10",
                "$open": 5.5,
                "$high": 5.5,
                "$low": 5.5,
                "$close": 5.5,
                "$change": 0.1,
                "$factor": 1.0,
                "$adj": 2.0,
                "$volume": 1_000.0,
            }
        ]
    )
    adjusted = apply_adjustment_factors(quotes, {"600000.SH": 4.0})
    states = derive_market_states(adjusted, buy_price_field="$open", sell_price_field="$open")

    assert adjusted.iloc[0]["$factor"] == pytest.approx(0.5)
    assert states.iloc[0]["limit_up_price"] == pytest.approx(5.5)
    assert states.iloc[0]["limit_buy"]


def test_exchange_audits_a_completely_missing_quote_as_suspended(monkeypatch):
    monkeypatch.setitem(C._config, "trade_unit", 100)
    monkeypatch.setitem(C._config, "region", "cn")
    quotes = quote_frame(
        [
            {
                "instrument": "SH600000",
                "datetime": "2026-07-10",
                "$open": 10.0,
                "$high": 10.0,
                "$low": 10.0,
                "$close": 10.0,
                "$change": 0.0,
                "$factor": 1.0,
                "$volume": 1_000.0,
            }
        ]
    )

    def fake_quotes(self):
        self.quote_df = quotes.copy()
        self.trade_w_adj_price = False
        self._update_limit(self.limit_threshold)

    monkeypatch.setattr(Exchange, "get_quote_from_qlib", fake_quotes)
    exchange = ChinaAExchange(
        start_time="2026-07-10",
        end_time="2026-07-13",
        codes=["SH600000"],
        deal_price="open",
        limit_threshold=0.095,
        open_cost=0,
        close_cost=0,
        min_cost=0,
        trade_unit=100,
    )
    position = Position(cash=1_000_000)
    order = Order(
        stock_id="SH600000",
        amount=100,
        direction=Order.BUY,
        start_time=pd.Timestamp("2026-07-13"),
        end_time=pd.Timestamp("2026-07-13"),
    )

    trade_value, trade_cost, trade_price = exchange.deal_order(order, position=position)

    assert trade_value == 0
    assert trade_cost == 0
    assert pd.isna(trade_price)
    assert exchange.execution_audit[-1]["reason"] == "suspended"
    assert exchange.execution_audit[-1]["market_rule_source"] == "missing_quote"
