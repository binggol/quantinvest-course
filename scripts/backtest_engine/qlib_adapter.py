"""Qlib execution adapters for point-in-time A-share portfolio backtests.

The stock-selection model stays outside this module.  Callers supply dated
target weights; this module is responsible for executable orders, market
constraints, settlement, transaction costs, and an order-level audit trail.
"""

from __future__ import annotations

import copy
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from qlib.backtest.decision import Order, TradeDecisionWO
from qlib.backtest.exchange import Exchange
from qlib.strategy.base import BaseStrategy


def _as_date(value: date | datetime | str | pd.Timestamp) -> date:
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = pd.Timestamp(value)
    if pd.isna(parsed):
        raise ValueError(f"invalid date: {value!r}")
    return parsed.date()


def _qlib_code(value: object) -> str:
    text = str(value).strip().upper()
    if text.startswith(("SH", "SZ", "BJ")) and len(text) >= 8:
        return text[:8]
    if "." in text:
        digits, market = text.split(".", 1)
        prefix = {"SH": "SH", "SZ": "SZ", "BJ": "BJ"}.get(market)
        if prefix and digits.isdigit():
            return prefix + digits.zfill(6)
    digits = "".join(ch for ch in text if ch.isdigit())[-6:]
    if len(digits) != 6:
        raise ValueError(f"invalid instrument code: {value!r}")
    if digits.startswith(("4", "8", "920")):
        return "BJ" + digits
    return ("SH" if digits.startswith(("5", "6", "9")) else "SZ") + digits


@dataclass(frozen=True)
class CorporateAction:
    from_code: str
    to_code: str
    effective_date: date | str
    raw_share_ratio: float
    from_factor: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "from_code", _qlib_code(self.from_code))
        object.__setattr__(self, "to_code", _qlib_code(self.to_code))
        object.__setattr__(self, "effective_date", _as_date(self.effective_date))
        object.__setattr__(self, "raw_share_ratio", float(self.raw_share_ratio))
        object.__setattr__(self, "from_factor", float(self.from_factor))
        if self.from_code == self.to_code:
            raise ValueError("corporate action codes must differ")
        if not np.isfinite(self.raw_share_ratio) or self.raw_share_ratio <= 0:
            raise ValueError("corporate action raw_share_ratio must be positive")
        if not np.isfinite(self.from_factor) or self.from_factor <= 0:
            raise ValueError("corporate action from_factor must be positive")


def apply_adjustment_factors(
    quote_df: pd.DataFrame,
    adjustment_max_factors: Mapping[str, float],
    *,
    adjustment_field: str = "$adj",
) -> pd.DataFrame:
    """Restore Qlib's normalized share factor from this project's price bins.

    Stored prices equal ``raw_price * adj / max_adj`` while daily volume stays
    in raw lots.  Qlib therefore needs ``factor = adj / max_adj`` so adjusted
    order amounts, raw shares, lot rounding, and traded value remain coherent.
    """

    if adjustment_field not in quote_df.columns:
        raise ValueError(f"quote data missing adjustment field: {adjustment_field}")
    if not isinstance(quote_df.index, pd.MultiIndex) or quote_df.index.nlevels != 2:
        raise ValueError("quote data index must be (instrument, datetime)")

    normalized_max = {_qlib_code(code): float(value) for code, value in adjustment_max_factors.items()}
    if any(not np.isfinite(value) or value <= 0 for value in normalized_max.values()):
        raise ValueError("adjustment max factors must be finite and positive")

    result = quote_df.copy()
    instruments = [_qlib_code(value) for value in result.index.get_level_values(0)]
    result.index = pd.MultiIndex.from_arrays(
        [instruments, pd.to_datetime(result.index.get_level_values(1)).normalize()],
        names=["instrument", "datetime"],
    )
    maxima = pd.Series(instruments, index=result.index).map(normalized_max)
    adjustment = pd.to_numeric(result[adjustment_field], errors="coerce")
    factor = adjustment / pd.to_numeric(maxima, errors="coerce")
    invalid = factor.isna() | ~np.isfinite(factor) | factor.le(0)
    if invalid.any():
        missing_codes = sorted(set(pd.Index(instruments)[invalid.to_numpy()]))
        raise ValueError(f"missing valid adjustment factors for: {missing_codes[:10]}")
    result["$factor"] = factor.astype(float)
    return result


@dataclass(frozen=True)
class FeeRate:
    """Aggregate Qlib buy/sell rates effective from one trading date."""

    effective_date: date | str
    open_cost: float
    close_cost: float
    min_cost: float = 5.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "effective_date", _as_date(self.effective_date))
        for field in ("open_cost", "close_cost", "min_cost"):
            value = float(getattr(self, field))
            if not np.isfinite(value) or value < 0:
                raise ValueError(f"{field} must be a finite non-negative number")
            object.__setattr__(self, field, value)


class HistoricalFeeSchedule:
    """Lookup transaction-cost rates without using a future fee regime."""

    def __init__(self, rates: Iterable[FeeRate]) -> None:
        ordered = sorted(rates, key=lambda item: item.effective_date)
        if not ordered:
            raise ValueError("at least one fee rate is required")
        dates = [item.effective_date for item in ordered]
        if len(dates) != len(set(dates)):
            raise ValueError("fee effective dates must be unique")
        self.rates = tuple(ordered)

    def at(self, value: date | datetime | str | pd.Timestamp) -> FeeRate:
        trade_date = _as_date(value)
        available = [item for item in self.rates if item.effective_date <= trade_date]
        if not available:
            raise ValueError(f"no fee rate is effective on {trade_date}")
        return available[-1]

    @classmethod
    def mainland_a_default(cls, *, commission: float = 0.0003) -> "HistoricalFeeSchedule":
        """Common A-share fee regimes for the current 2017+ research window.

        Commission remains configurable.  The schedule reflects the transfer
        fee reduction effective 2022-04-29 and the stamp-duty reduction
        effective 2023-08-28.  Qlib applies ``min_cost`` to aggregate costs, so
        reports retain that approximation explicitly in their metadata.
        """

        commission = float(commission)
        return cls(
            [
                FeeRate("2015-08-01", commission + 0.00002, commission + 0.00002 + 0.001),
                FeeRate("2022-04-29", commission + 0.00001, commission + 0.00001 + 0.001),
                FeeRate("2023-08-28", commission + 0.00001, commission + 0.00001 + 0.0005),
            ]
        )


def _round_limit(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    return np.floor(numeric * 100.0 + 0.5 + 1e-9) / 100.0


def _fallback_limit_pct(index: pd.MultiIndex) -> pd.Series:
    """Present-board fallback plus the known ChiNext 2020 regime boundary.

    Historical ST status, IPO no-limit windows, and exceptional securities must
    be provided through an override frame by the caller.
    """

    values: list[float] = []
    for instrument, timestamp in index:
        code = _qlib_code(instrument)[2:]
        trade_date = pd.Timestamp(timestamp).date()
        if code.startswith(("4", "8", "920")):
            values.append(0.30)
        elif code.startswith(("688", "689")):
            values.append(0.20)
        elif code.startswith(("300", "301")):
            values.append(0.20 if trade_date >= date(2020, 8, 24) else 0.10)
        else:
            values.append(0.10)
    return pd.Series(values, index=index, dtype=float)


def derive_market_states(
    quote_df: pd.DataFrame,
    *,
    buy_price_field: str,
    sell_price_field: str,
    overrides: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build direction-specific daily tradability flags from Qlib quotes.

    ``overrides`` may contain nullable ``suspended``, ``limit_buy``,
    ``limit_sell`` and ``limit_pct`` columns.  Non-null values take priority
    over fallbacks, which is how historical ST/IPO/rule data should enter.
    """

    required = {"$open", "$high", "$low", "$close", "$change", "$volume", buy_price_field, sell_price_field}
    missing = required - set(quote_df.columns)
    if missing:
        raise ValueError(f"quote data missing fields: {sorted(missing)}")
    if not isinstance(quote_df.index, pd.MultiIndex) or quote_df.index.nlevels != 2:
        raise ValueError("quote data index must be (instrument, datetime)")

    frame = quote_df.copy()
    normalized_index = pd.MultiIndex.from_arrays(
        [
            [_qlib_code(value) for value in frame.index.get_level_values(0)],
            pd.to_datetime(frame.index.get_level_values(1)).normalize(),
        ],
        names=["instrument", "datetime"],
    )
    frame.index = normalized_index
    if frame.index.has_duplicates:
        raise ValueError("quote data contains duplicate instrument/date rows")

    price_fields = ["$open", "$high", "$low", "$close", buy_price_field, sell_price_field]
    prices = frame[price_fields].apply(pd.to_numeric, errors="coerce")
    volume = pd.to_numeric(frame["$volume"], errors="coerce")
    change = pd.to_numeric(frame["$change"], errors="coerce")
    suspended = (
        volume.isna()
        | volume.le(0)
        | change.isna()
        | prices.isna().any(axis=1)
        | prices.le(0).any(axis=1)
    )

    limit_pct = _fallback_limit_pct(frame.index)
    normalized_overrides = None
    if overrides is not None:
        normalized_overrides = overrides.copy()
        if not isinstance(normalized_overrides.index, pd.MultiIndex) or normalized_overrides.index.nlevels != 2:
            raise ValueError("market-state overrides must use (instrument, datetime) index")
        normalized_overrides.index = pd.MultiIndex.from_arrays(
            [
                [_qlib_code(value) for value in normalized_overrides.index.get_level_values(0)],
                pd.to_datetime(normalized_overrides.index.get_level_values(1)).normalize(),
            ],
            names=frame.index.names,
        )
        if normalized_overrides.index.has_duplicates:
            raise ValueError("market-state overrides contain duplicate rows")
        normalized_overrides = normalized_overrides.reindex(frame.index)
        if "limit_pct" in normalized_overrides:
            explicit_pct = pd.to_numeric(normalized_overrides["limit_pct"], errors="coerce")
            limit_pct = explicit_pct.where(explicit_pct.notna(), limit_pct)

    close = pd.to_numeric(frame["$close"], errors="coerce")
    pre_close = close / (1.0 + change)
    factor = (
        pd.to_numeric(frame["$factor"], errors="coerce")
        if "$factor" in frame
        else pd.Series(1.0, index=frame.index)
    )
    factor = factor.where(factor.gt(0), np.nan)
    raw_pre_close = pre_close / factor
    limit_up_price = _round_limit(raw_pre_close * (1.0 + limit_pct)) * factor
    limit_down_price = _round_limit(raw_pre_close * (1.0 - limit_pct)) * factor
    eps = 0.0051
    buy_price = pd.to_numeric(frame[buy_price_field], errors="coerce")
    sell_price = pd.to_numeric(frame[sell_price_field], errors="coerce")
    derived_buy = buy_price.ge(limit_up_price - eps)
    derived_sell = sell_price.le(limit_down_price + eps)

    result = pd.DataFrame(
        {
            "suspended": suspended.astype(bool),
            "limit_buy": (suspended | derived_buy).astype(bool),
            "limit_sell": (suspended | derived_sell).astype(bool),
            "limit_pct": limit_pct,
            "limit_up_price": limit_up_price,
            "limit_down_price": limit_down_price,
            "rule_source": "board_fallback",
        },
        index=frame.index,
    )
    if normalized_overrides is not None:
        for column in ("suspended", "limit_buy", "limit_sell"):
            if column in normalized_overrides:
                explicit = normalized_overrides[column]
                mask = explicit.notna()
                result.loc[mask, column] = explicit.loc[mask].astype(bool)
                result.loc[mask, "rule_source"] = "explicit"
        result["limit_buy"] = result["limit_buy"] | result["suspended"]
        result["limit_sell"] = result["limit_sell"] | result["suspended"]
    return result


class ChinaAExchange(Exchange):
    """Qlib Exchange with A-share status, fee history, T+1, and audit logs."""

    def __init__(
        self,
        *args,
        market_state_overrides: pd.DataFrame | None = None,
        fee_schedule: HistoricalFeeSchedule | None = None,
        volume_unit_multiplier: float = 100.0,
        adjustment_max_factors: Mapping[str, float] | None = None,
        adjustment_field: str = "$adj",
        **kwargs,
    ) -> None:
        subscribe_fields = set(kwargs.pop("subscribe_fields", []) or [])
        subscribe_fields.update({"$open", "$high", "$low"})
        if adjustment_max_factors is not None:
            subscribe_fields.add(adjustment_field)
        kwargs["subscribe_fields"] = sorted(subscribe_fields)
        super().__init__(*args, **kwargs)
        self.fee_schedule = fee_schedule
        self.volume_unit_multiplier = float(volume_unit_multiplier)
        if not np.isfinite(self.volume_unit_multiplier) or self.volume_unit_multiplier <= 0:
            raise ValueError("volume_unit_multiplier must be positive")
        self.execution_audit: list[dict] = []
        self._frozen_buys: defaultdict[str, float] = defaultdict(float)
        self._frozen_trade_date: date | None = None
        self.adjustment_field = adjustment_field
        self.uses_adjusted_share_units = adjustment_max_factors is not None

        if adjustment_max_factors is not None:
            self.quote_df = apply_adjustment_factors(
                self.quote_df,
                adjustment_max_factors,
                adjustment_field=adjustment_field,
            )
            factor = pd.to_numeric(self.quote_df["$factor"], errors="coerce")
            volume_limit_fields = {
                item[1]
                for limits in (self.buy_vol_limit, self.sell_vol_limit)
                if limits is not None
                for item in limits
            }
            for field in volume_limit_fields:
                # Replay volume-limit expressions are specified in raw shares.
                self.quote_df[field] = pd.to_numeric(self.quote_df[field], errors="coerce") / factor
            self.trade_w_adj_price = False

        self.market_states = derive_market_states(
            self.quote_df,
            buy_price_field=self.buy_price,
            sell_price_field=self.sell_price,
            overrides=market_state_overrides,
        )
        aligned = self.market_states.reindex(self.quote_df.index)
        if aligned[["limit_buy", "limit_sell"]].isna().any().any():
            raise ValueError("market-state coverage is incomplete")
        self.quote_df["limit_buy"] = aligned["limit_buy"].astype(bool)
        self.quote_df["limit_sell"] = aligned["limit_sell"].astype(bool)
        self.quote = self.quote_cls(self.quote_df, self.freq)

    def get_volume(self, stock_id, start_time, end_time):
        # Tushare daily ``vol`` and this project's Qlib $volume are in lots;
        # Qlib orders use normalized share units when prices are adjusted.
        raw_shares = super().get_volume(stock_id, start_time, end_time) * self.volume_unit_multiplier
        if not self.uses_adjusted_share_units:
            return raw_shares
        factor = self.get_factor(stock_id, start_time, end_time)
        if factor is None or not np.isfinite(factor) or factor <= 0:
            return np.nan
        return raw_shares / factor

    def _calc_trade_info_by_order(self, order, position, dealt_order_amount):
        if self.fee_schedule is None:
            return super()._calc_trade_info_by_order(order, position, dealt_order_amount)
        rate = self.fee_schedule.at(order.start_time)
        old = self.open_cost, self.close_cost, self.min_cost
        self.open_cost, self.close_cost, self.min_cost = rate.open_cost, rate.close_cost, rate.min_cost
        try:
            return super()._calc_trade_info_by_order(order, position, dealt_order_amount)
        finally:
            self.open_cost, self.close_cost, self.min_cost = old

    def deal_order(self, order, trade_account=None, position=None, dealt_order_amount=None):
        if dealt_order_amount is None:
            dealt_order_amount = defaultdict(float)
        trade_date = _as_date(order.start_time)
        if self._frozen_trade_date != trade_date:
            self._frozen_buys.clear()
            self._frozen_trade_date = trade_date

        account_position = trade_account.current_position if trade_account is not None else position
        requested_amount = float(order.amount)
        t1_clipped = False
        if order.direction == Order.SELL and account_position is not None:
            held = (
                float(account_position.get_stock_amount(order.stock_id))
                if account_position.check_stock(order.stock_id)
                else 0.0
            )
            sellable = max(0.0, held - self._frozen_buys[order.stock_id])
            if order.amount > sellable:
                order.amount = sellable
                t1_clipped = True

        was_tradable = self.is_stock_tradable(
            order.stock_id, order.start_time, order.end_time, direction=order.direction
        )
        state_key = (_qlib_code(order.stock_id), pd.Timestamp(order.start_time).normalize())
        if state_key in self.market_states.index and state_key in self.quote_df.index:
            state_row = self.market_states.loc[state_key]
            quote_row = self.quote_df.loc[state_key]
            factor = float(quote_row["$factor"]) if pd.notna(quote_row["$factor"]) else np.nan
            bar_prices = [float(quote_row[field]) for field in ("$open", "$high", "$low", "$close")]
            change = float(quote_row["$change"]) if pd.notna(quote_row["$change"]) else np.nan
        else:
            fallback_index = pd.MultiIndex.from_tuples(
                [state_key], names=["instrument", "datetime"]
            )
            limit_pct = float(_fallback_limit_pct(fallback_index).iloc[0])
            state_row = {
                "suspended": True,
                "limit_buy": True,
                "limit_sell": True,
                "rule_source": "missing_quote",
                "limit_pct": limit_pct,
                "limit_up_price": np.nan,
                "limit_down_price": np.nan,
            }
            factor = np.nan
            bar_prices = [np.nan] * 4
            change = np.nan
        one_price_bar = all(np.isfinite(value) for value in bar_prices) and max(bar_prices) - min(bar_prices) < 0.0051
        potential_st_locked = one_price_bar and np.isfinite(change) and 0.045 <= abs(change) <= 0.055
        trade_val, trade_cost, trade_price = super().deal_order(
            order,
            trade_account=trade_account,
            position=position,
            dealt_order_amount=dealt_order_amount,
        )
        if order.direction == Order.BUY and order.deal_amount > 0:
            self._frozen_buys[order.stock_id] += float(order.deal_amount)

        if t1_clipped and order.deal_amount <= 0:
            reason = "t1_frozen"
        elif not was_tradable:
            if bool(state_row["suspended"]):
                reason = "suspended"
            elif order.direction == Order.BUY and bool(state_row["limit_buy"]):
                reason = "limit_buy"
            elif order.direction == Order.SELL and bool(state_row["limit_sell"]):
                reason = "limit_sell"
            else:
                reason = "untradable"
        elif order.deal_amount <= 0:
            reason = "cash_or_volume"
        elif float(order.deal_amount) + 1e-8 < requested_amount:
            reason = "partial"
        else:
            reason = "filled"
        self.execution_audit.append(
            {
                "trade_date": trade_date.isoformat(),
                "instrument": _qlib_code(order.stock_id),
                "side": "buy" if order.direction == Order.BUY else "sell",
                "requested_amount": requested_amount,
                "deal_amount": float(order.deal_amount),
                "factor": None if not np.isfinite(factor) else factor,
                "requested_raw_shares": None
                if not np.isfinite(factor)
                else requested_amount * factor,
                "deal_raw_shares": None
                if not np.isfinite(factor)
                else float(order.deal_amount) * factor,
                "trade_price": None if not np.isfinite(trade_price) else float(trade_price),
                "trade_value": float(trade_val),
                "trade_cost": float(trade_cost),
                "reason": reason,
                "market_rule_source": str(state_row["rule_source"]),
                "limit_pct": float(state_row["limit_pct"]),
                "limit_up_price": None
                if pd.isna(state_row["limit_up_price"])
                else float(state_row["limit_up_price"]),
                "limit_down_price": None
                if pd.isna(state_row["limit_down_price"])
                else float(state_row["limit_down_price"]),
                "potential_st_locked": bool(potential_st_locked),
            }
        )
        return trade_val, trade_cost, trade_price


class EventTargetWeightStrategy(BaseStrategy):
    """Execute dated target weights on the current bar without Qlib's signal shift."""

    def __init__(
        self,
        *,
        target_weights: Mapping[date | str, Mapping[str, float]],
        risk_degree: float = 0.95,
        retry_days: int = 5,
        retry_days_by_target: Mapping[date | str, int] | None = None,
        corporate_actions: Iterable[CorporateAction | Mapping[str, Any]] | None = None,
        rebalance_mode: str = "target_weight",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        if not 0 < float(risk_degree) <= 1:
            raise ValueError("risk_degree must be in (0, 1]")
        if int(retry_days) < 1:
            raise ValueError("retry_days must be positive")
        if rebalance_mode not in ("target_weight", "replace_only"):
            raise ValueError("rebalance_mode must be 'target_weight' or 'replace_only'")
        self.risk_degree = float(risk_degree)
        self.retry_days = int(retry_days)
        self.rebalance_mode = rebalance_mode
        self.target_weights = self._normalize_targets(target_weights)
        self.retry_days_by_target = self._normalize_retry_overrides(retry_days_by_target)
        self.corporate_actions = self._normalize_corporate_actions(corporate_actions)
        self.strategy_audit: list[dict] = []
        self._applied_corporate_actions: set[CorporateAction] = set()
        self._desired_amounts: dict[str, float] | None = None
        self._active_weights: dict[str, float] = {}
        self._pending_codes: set[str] = set()
        self._attempts = 0
        self._active_date: date | None = None
        self._active_retry_days = self.retry_days
        self._replace_preserved_codes: set[str] = set()
        self._replace_exit_codes: set[str] = set()
        self._replace_entrant_codes: set[str] = set()
        self._replace_entrant_budgets: dict[str, float] = {}
        self._replace_entrant_spent: dict[str, float] = {}

    @staticmethod
    def _normalize_targets(
        targets: Mapping[date | str, Mapping[str, float]],
    ) -> dict[date, dict[str, float]]:
        result: dict[date, dict[str, float]] = {}
        for raw_date, raw_weights in targets.items():
            trade_date = _as_date(raw_date)
            if trade_date in result:
                raise ValueError(f"duplicate target date: {trade_date}")
            weights = {_qlib_code(code): float(weight) for code, weight in raw_weights.items()}
            if any(not np.isfinite(weight) or weight < 0 for weight in weights.values()):
                raise ValueError(f"invalid target weight on {trade_date}")
            if sum(weights.values()) > 1.0 + 1e-8:
                raise ValueError(f"target weights exceed 100% on {trade_date}")
            result[trade_date] = weights
        return dict(sorted(result.items()))

    def _normalize_retry_overrides(
        self, overrides: Mapping[date | str, int] | None
    ) -> dict[date, int]:
        result: dict[date, int] = {}
        for raw_date, raw_days in (overrides or {}).items():
            trade_date = _as_date(raw_date)
            days = int(raw_days)
            if days < 1:
                raise ValueError("target retry days must be positive")
            if trade_date not in self.target_weights:
                raise ValueError(f"retry override has no target: {trade_date}")
            result[trade_date] = days
        return dict(sorted(result.items()))

    @staticmethod
    def _normalize_corporate_actions(
        actions: Iterable[CorporateAction | Mapping[str, Any]] | None,
    ) -> tuple[CorporateAction, ...]:
        result: list[CorporateAction] = []
        seen: set[tuple[str, date]] = set()
        for raw_action in actions or ():
            action = (
                raw_action
                if isinstance(raw_action, CorporateAction)
                else CorporateAction(**dict(raw_action))
            )
            key = action.from_code, action.effective_date
            if key in seen:
                raise ValueError(f"duplicate corporate action: {key}")
            seen.add(key)
            result.append(action)
        return tuple(sorted(result, key=lambda item: (item.effective_date, item.from_code)))

    def _is_retired(self, code: str, trade_date: date) -> bool:
        normalized = _qlib_code(code)
        return any(
            action.from_code == normalized and trade_date >= action.effective_date
            for action in self.corporate_actions
        )

    def _map_target_code(self, code: str, trade_date: date) -> tuple[str, list[CorporateAction]]:
        current = _qlib_code(code)
        applied: list[CorporateAction] = []
        visited = {current}
        while True:
            matches = [
                action
                for action in self.corporate_actions
                if action.from_code == current and trade_date >= action.effective_date
            ]
            if not matches:
                return current, applied
            action = max(matches, key=lambda item: item.effective_date)
            if action.to_code in visited:
                raise ValueError(f"corporate action target mapping contains a cycle: {code}")
            applied.append(action)
            current = action.to_code
            visited.add(current)

    def _replace_role(self, code: str) -> str | None:
        normalized = _qlib_code(code)
        if normalized in self._replace_exit_codes:
            return "exit"
        if normalized in self._replace_entrant_codes:
            return "entrant"
        if normalized in self._replace_preserved_codes:
            return "preserved"
        return None

    def _finish_replace_code(self, code: str) -> None:
        normalized = _qlib_code(code)
        self._pending_codes.discard(normalized)
        self._replace_exit_codes.discard(normalized)
        self._replace_entrant_codes.discard(normalized)
        self._replace_entrant_budgets.pop(normalized, None)
        self._replace_entrant_spent.pop(normalized, None)
        if normalized in self._active_weights and self.trade_position.check_stock(normalized):
            self._replace_preserved_codes.add(normalized)

    def _latest_execution_record(self, order: Order) -> Mapping[str, Any] | None:
        side = "buy" if order.direction == Order.BUY else "sell"
        trade_date = _as_date(order.start_time).isoformat()
        code = _qlib_code(order.stock_id)
        for item in reversed(getattr(self.trade_exchange, "execution_audit", [])):
            if (
                item.get("trade_date") == trade_date
                and item.get("instrument") == code
                and item.get("side") == side
            ):
                return item
        return None

    def _latest_execution_reason(self, order: Order) -> str | None:
        record = self._latest_execution_record(order)
        return str(record.get("reason")) if record is not None else None

    def _capture_replace_action(self, action: CorporateAction) -> dict[str, Any]:
        return {
            "was_preserved": action.from_code in self._replace_preserved_codes,
            "was_exit": action.from_code in self._replace_exit_codes,
            "was_entrant": action.from_code in self._replace_entrant_codes,
            "old_weight": self._active_weights.get(action.from_code),
            "old_desired": (
                self._desired_amounts.get(action.from_code)
                if self._desired_amounts is not None
                else None
            ),
            "old_budget": self._replace_entrant_budgets.get(action.from_code),
            "old_spent": self._replace_entrant_spent.get(action.from_code, 0.0),
            "destination_preserved": action.to_code in self._replace_preserved_codes,
            "destination_exit": action.to_code in self._replace_exit_codes,
            "destination_entrant": action.to_code in self._replace_entrant_codes,
            "destination_desired": (
                self._desired_amounts.get(action.to_code)
                if self._desired_amounts is not None
                else None
            ),
        }

    def _remap_replace_action(
        self,
        action: CorporateAction,
        state: Mapping[str, Any],
        *,
        converted: bool,
        existing_to_amount: float = 0.0,
        converted_amount: float = 0.0,
        to_factor: float | None = None,
        start_time=None,
        end_time=None,
    ) -> None:
        self._replace_preserved_codes.discard(action.from_code)
        self._replace_exit_codes.discard(action.from_code)
        self._replace_entrant_codes.discard(action.from_code)
        self._replace_entrant_budgets.pop(action.from_code, None)
        self._replace_entrant_spent.pop(action.from_code, None)
        old_weight = state.get("old_weight")
        if old_weight is not None:
            self._active_weights[action.to_code] = (
                self._active_weights.get(action.to_code, 0.0) + float(old_weight)
            )

        if not any(state.get(key) for key in ("was_preserved", "was_exit", "was_entrant")):
            return
        if self._desired_amounts is None:
            self._desired_amounts = {}

        if not converted:
            if state.get("was_exit"):
                return
            budget = state.get("old_budget")
            if budget is None and old_weight is not None:
                nav_budget = float(self.trade_position.calculate_value()) * self.risk_degree
                preserved_value = sum(
                    float(self.trade_position.get_stock_amount(code))
                    * float(self.trade_position.get_stock_price(code))
                    for code in self._replace_preserved_codes
                    if self.trade_position.check_stock(code)
                )
                budget = min(nav_budget * float(old_weight), max(0.0, nav_budget - preserved_value))
            if budget is not None and float(budget) > 1e-8:
                budget = float(budget)
                self._replace_entrant_codes.add(action.to_code)
                self._replace_entrant_budgets[action.to_code] = (
                    self._replace_entrant_budgets.get(action.to_code, 0.0) + budget
                )
                self._replace_entrant_spent[action.to_code] = (
                    self._replace_entrant_spent.get(action.to_code, 0.0)
                    + float(state.get("old_spent", 0.0))
                )
                self._pending_codes.add(action.to_code)
            return

        destination_target = state.get("destination_desired")
        if destination_target is None and state.get("destination_preserved"):
            destination_target = float(existing_to_amount)
        destination_target = float(destination_target or 0.0)

        if state.get("was_exit"):
            if state.get("destination_entrant"):
                budget = float(self._replace_entrant_budgets.get(action.to_code, 0.0))
                spent = float(self._replace_entrant_spent.get(action.to_code, 0.0))
                remaining_budget = max(0.0, budget - spent)
                try:
                    price = float(
                        self.trade_exchange.get_deal_price(
                            action.to_code, start_time, end_time, direction=Order.BUY
                        )
                    )
                except Exception:
                    price = np.nan
                if not np.isfinite(price) or price <= 0:
                    raise RuntimeError(
                        f"missing valid destination price for corporate action target: {action.to_code}"
                    )
                required_new_amount = remaining_budget / price
                target_amount = float(existing_to_amount) + required_new_amount
                retained_converted = min(float(converted_amount), required_new_amount)
                self._replace_entrant_spent[action.to_code] = min(
                    budget, spent + retained_converted * price
                )
                merged_amount = float(existing_to_amount) + float(converted_amount)
                self._replace_exit_codes.discard(action.to_code)
                self._replace_entrant_codes.discard(action.to_code)
                self._pending_codes.discard(action.to_code)
                if merged_amount > target_amount + 1e-8:
                    self._desired_amounts[action.to_code] = target_amount
                    self._replace_exit_codes.add(action.to_code)
                    self._pending_codes.add(action.to_code)
                elif merged_amount < target_amount - 1e-8:
                    self._desired_amounts.pop(action.to_code, None)
                    self._replace_entrant_codes.add(action.to_code)
                    self._pending_codes.add(action.to_code)
                else:
                    self._desired_amounts.pop(action.to_code, None)
                    self._finish_replace_code(action.to_code)
            else:
                self._desired_amounts[action.to_code] = destination_target
                self._replace_exit_codes.add(action.to_code)
                self._pending_codes.add(action.to_code)
        elif state.get("was_entrant"):
            self._replace_entrant_codes.add(action.to_code)
            if state.get("old_budget") is not None:
                self._replace_entrant_budgets[action.to_code] = (
                    self._replace_entrant_budgets.get(action.to_code, 0.0)
                    + float(state["old_budget"])
                )
                self._replace_entrant_spent[action.to_code] = (
                    self._replace_entrant_spent.get(action.to_code, 0.0)
                    + float(state.get("old_spent", 0.0))
                )
            self._desired_amounts.pop(action.to_code, None)
            self._pending_codes.add(action.to_code)
        elif state.get("was_preserved"):
            if state.get("destination_exit"):
                self._desired_amounts[action.to_code] = float(converted_amount)
                self._replace_exit_codes.add(action.to_code)
                self._pending_codes.add(action.to_code)
            else:
                self._replace_preserved_codes.add(action.to_code)

        if action.to_code in self._pending_codes:
            self._replace_preserved_codes.discard(action.to_code)

    def _apply_corporate_actions(self, trade_date: date, start_time, end_time) -> None:
        position = self.trade_position
        for action in self.corporate_actions:
            if action in self._applied_corporate_actions or trade_date < action.effective_date:
                continue

            replace_state = (
                self._capture_replace_action(action)
                if self.rebalance_mode == "replace_only"
                else None
            )
            self._active_weights.pop(action.from_code, None)
            self._pending_codes.discard(action.from_code)
            if self._desired_amounts is not None:
                self._desired_amounts.pop(action.from_code, None)
                if not self._pending_codes and self.rebalance_mode != "replace_only":
                    self._desired_amounts = None

            if not position.check_stock(action.from_code):
                if replace_state is not None:
                    self._remap_replace_action(
                        action,
                        replace_state,
                        converted=False,
                        start_time=start_time,
                        end_time=end_time,
                    )
                    if not self._pending_codes:
                        self._desired_amounts = None
                self.strategy_audit.append(
                    {
                        "status": "corporate_action_no_position",
                        "effective_date": action.effective_date.isoformat(),
                        "applied_date": trade_date.isoformat(),
                        "from_code": action.from_code,
                        "to_code": action.to_code,
                        "replace_role": (
                            self._replace_role(action.to_code) if replace_state is not None else None
                        ),
                    }
                )
                self._applied_corporate_actions.add(action)
                continue

            to_factor = float(self.trade_exchange.get_factor(action.to_code, start_time, end_time))
            if not np.isfinite(to_factor) or to_factor <= 0:
                raise RuntimeError(
                    f"missing valid destination factor for corporate action: {action.to_code}"
                )

            cash_before = float(position.get_cash(include_settle=True))
            value_before = float(position.calculate_value())
            old_adjusted_amount = float(position.get_stock_amount(action.from_code))
            old_price = float(position.get_stock_price(action.from_code))
            old_value = old_adjusted_amount * old_price
            old_raw_shares = old_adjusted_amount * action.from_factor
            new_raw_shares = old_raw_shares * action.raw_share_ratio
            converted_adjusted_amount = new_raw_shares / to_factor

            existing_adjusted_amount = float(position.get_stock_amount(action.to_code))
            existing_value = (
                existing_adjusted_amount * float(position.get_stock_price(action.to_code))
                if existing_adjusted_amount > 0
                else 0.0
            )
            merged_amount = existing_adjusted_amount + converted_adjusted_amount
            if not np.isfinite(merged_amount) or merged_amount <= 0:
                raise RuntimeError(f"invalid converted amount for corporate action: {action}")
            carry_price = (existing_value + old_value) / merged_amount

            if position.check_stock(action.to_code):
                position.position[action.to_code]["amount"] = merged_amount
                position.update_stock_price(action.to_code, carry_price)
            else:
                position._init_stock(action.to_code, merged_amount, carry_price)
            position._del_stock(action.from_code)
            position.position["now_account_value"] = position.calculate_value()

            if replace_state is not None:
                self._remap_replace_action(
                    action,
                    replace_state,
                    converted=True,
                    existing_to_amount=existing_adjusted_amount,
                    converted_amount=converted_adjusted_amount,
                    to_factor=to_factor,
                    start_time=start_time,
                    end_time=end_time,
                )
                if not self._pending_codes:
                    self._desired_amounts = None

            cash_after = float(position.get_cash(include_settle=True))
            value_after = float(position.calculate_value())
            if not np.isclose(cash_before, cash_after, rtol=0.0, atol=1e-8):
                raise RuntimeError("corporate action unexpectedly changed cash")
            if not np.isclose(value_before, value_after, rtol=1e-10, atol=1e-6):
                raise RuntimeError("corporate action unexpectedly changed portfolio value")

            if self._desired_amounts is not None and action.to_code in self._desired_amounts:
                self._pending_codes.add(action.to_code)
            self.strategy_audit.append(
                {
                    "status": "corporate_action_converted",
                    "effective_date": action.effective_date.isoformat(),
                    "applied_date": trade_date.isoformat(),
                    "from_code": action.from_code,
                    "to_code": action.to_code,
                    "from_adjusted_amount": old_adjusted_amount,
                    "from_factor": action.from_factor,
                    "from_raw_shares": old_raw_shares,
                    "raw_share_ratio": action.raw_share_ratio,
                    "to_factor": to_factor,
                    "converted_raw_shares": new_raw_shares,
                    "converted_adjusted_amount": converted_adjusted_amount,
                    "merged_adjusted_amount": merged_amount,
                    "cash_before": cash_before,
                    "cash_after": cash_after,
                    "value_before": value_before,
                    "value_after": value_after,
                    "trade_cost": 0.0,
                    "replace_role": (
                        self._replace_role(action.to_code) if replace_state is not None else None
                    ),
                }
            )
            self._applied_corporate_actions.add(action)

    def _consume_previous_result(self, execute_result) -> None:
        if self._desired_amounts is None or execute_result is None:
            return
        if self.rebalance_mode == "replace_only":
            self._consume_replace_result(execute_result)
            return
        for item in execute_result:
            if not item:
                continue
            order = item[0]
            requested = float(getattr(order, "_qi_requested_amount", order.amount))
            if float(order.deal_amount) + 1e-8 >= requested:
                self._pending_codes.discard(_qlib_code(order.stock_id))
        if not self._pending_codes:
            self._desired_amounts = None

    def _consume_replace_result(self, execute_result) -> None:
        for item in execute_result:
            if not item:
                continue
            order = item[0]
            code = _qlib_code(order.stock_id)
            requested = float(getattr(order, "_qi_requested_amount", order.amount))
            dealt = float(order.deal_amount)
            completed = dealt + 1e-8 >= requested
            outcome = "filled" if completed else ("partial" if dealt > 0 else "unfilled")
            role = str(getattr(order, "_qi_replace_role", self._replace_role(code)))
            execution = self._latest_execution_record(order)
            reason = str(execution.get("reason")) if execution is not None else None
            if role == "entrant" and dealt > 0:
                if execution is not None:
                    trade_value = float(execution.get("trade_value", 0.0))
                else:
                    try:
                        price = float(
                            self.trade_exchange.get_deal_price(
                                code, order.start_time, order.end_time, direction=Order.BUY
                            )
                        )
                    except Exception:
                        price = np.nan
                    trade_value = dealt * price if np.isfinite(price) else 0.0
                self._replace_entrant_spent[code] = (
                    self._replace_entrant_spent.get(code, 0.0) + max(0.0, trade_value)
                )
            frozen_budget = self._replace_entrant_budgets.get(code)
            spent_budget = self._replace_entrant_spent.get(code)
            if completed:
                self._finish_replace_code(code)
            self.strategy_audit.append(
                {
                    "status": "replace_order_result",
                    "target_date": self._active_date.isoformat() if self._active_date else None,
                    "attempt_date": _as_date(order.start_time).isoformat(),
                    "instrument": code,
                    "role": role,
                    "side": "buy" if order.direction == Order.BUY else "sell",
                    "requested_amount": requested,
                    "deal_amount": dealt,
                    "outcome": outcome,
                    "execution_reason": reason,
                    "frozen_budget": frozen_budget,
                    "spent_budget": spent_budget,
                    "will_retry": not completed,
                }
            )
        if not self._pending_codes:
            self._desired_amounts = None

    def _start_target(self, trade_date: date, start_time, end_time) -> None:
        raw_weights = self.target_weights[trade_date]
        weights: dict[str, float] = {}
        mapped: list[dict[str, Any]] = []
        for code, weight in raw_weights.items():
            mapped_code, actions = self._map_target_code(code, trade_date)
            weights[mapped_code] = weights.get(mapped_code, 0.0) + float(weight)
            if actions:
                mapped.append(
                    {
                        "from_code": _qlib_code(code),
                        "to_code": mapped_code,
                        "weight": float(weight),
                        "effective_dates": [item.effective_date.isoformat() for item in actions],
                    }
                )
        if mapped:
            self.strategy_audit.append(
                {
                    "status": "retired_target_mapped",
                    "target_date": trade_date.isoformat(),
                    "mappings": mapped,
                }
            )
        self._active_weights = dict(weights)
        if self.rebalance_mode == "replace_only":
            self._start_replace_target(trade_date, start_time, end_time)
            return

        current = self.trade_position
        total_value = float(current.calculate_value()) * self.risk_degree
        desired: dict[str, float] = {}
        for code, weight in self._active_weights.items():
            try:
                price = float(
                    self.trade_exchange.get_deal_price(
                        code, start_time, end_time, direction=Order.BUY
                    )
                )
            except Exception:
                price = np.nan
            if np.isfinite(price) and price > 0:
                desired[code] = total_value * weight / price
        for code in current.get_stock_list():
            desired.setdefault(_qlib_code(code), 0.0)
        self._desired_amounts = desired
        self._pending_codes = set(self._active_weights) | set(desired)
        self._attempts = 0
        self._active_date = trade_date
        self._active_retry_days = self.retry_days_by_target.get(trade_date, self.retry_days)

    def _start_replace_target(self, trade_date: date, start_time, end_time) -> None:
        current = self.trade_position
        current_codes = {
            _qlib_code(code)
            for code, amount in current.get_stock_amount_dict().items()
            if float(amount) > 1e-8
        }
        target_codes = set(self._active_weights)
        if target_codes:
            preserved = current_codes & target_codes
            exits = current_codes - target_codes
            entrants = target_codes - current_codes
        else:
            preserved, entrants = set(), set()
            exits = set(current_codes)

        nav = float(current.calculate_value())
        risk_budget = max(0.0, nav * self.risk_degree)
        preserved_value = 0.0
        for code in preserved:
            amount = float(current.get_stock_amount(code))
            price = float(current.get_stock_price(code))
            value = amount * price
            if not np.isfinite(value) or value < 0:
                raise RuntimeError(f"invalid preserved holding value for: {code}")
            preserved_value += value
        available_budget = max(0.0, risk_budget - preserved_value)
        nominal_budgets = {
            code: risk_budget * float(self._active_weights[code]) for code in entrants
        }
        nominal_total = sum(nominal_budgets.values())
        scale = min(1.0, available_budget / nominal_total) if nominal_total > 0 else 0.0
        entrant_budgets = {
            code: value * scale for code, value in nominal_budgets.items() if value * scale > 1e-8
        }

        desired: dict[str, float] = {code: 0.0 for code in exits}

        self._replace_preserved_codes = set(preserved)
        self._replace_exit_codes = set(exits)
        self._replace_entrant_codes = set(entrant_budgets)
        self._replace_entrant_budgets = entrant_budgets
        self._replace_entrant_spent = {code: 0.0 for code in entrant_budgets}
        self._pending_codes = set(exits) | set(entrant_budgets)
        self._desired_amounts = desired if self._pending_codes else None
        self._attempts = 0
        self._active_date = trade_date
        self._active_retry_days = self.retry_days_by_target.get(trade_date, self.retry_days)
        self.strategy_audit.append(
            {
                "status": "replace_target_started",
                "target_date": trade_date.isoformat(),
                "preserved_codes": sorted(preserved),
                "exit_codes": sorted(exits),
                "entrant_codes": sorted(entrant_budgets),
                "unfunded_entrant_codes": sorted(entrants - set(entrant_budgets)),
                "nav": nav,
                "risk_budget": risk_budget,
                "preserved_value": preserved_value,
                "available_entrant_budget": available_budget,
                "frozen_entrant_budget": sum(entrant_budgets.values()),
            }
        )

    def _orders_for_pending(self, start_time, end_time) -> list[Order]:
        if self.rebalance_mode == "replace_only":
            return self._replace_orders_for_pending(start_time, end_time)
        if self._desired_amounts is None:
            return []
        current_amounts = {
            _qlib_code(code): float(amount)
            for code, amount in self.trade_position.get_stock_amount_dict().items()
        }
        sells: list[Order] = []
        buys: list[Order] = []
        completed: set[str] = set()
        for code in sorted(self._pending_codes):
            if self._is_retired(code, _as_date(start_time)):
                completed.add(code)
                continue
            if code not in self._desired_amounts and code in self._active_weights:
                try:
                    price = float(
                        self.trade_exchange.get_deal_price(
                            code, start_time, end_time, direction=Order.BUY
                        )
                    )
                except Exception:
                    price = np.nan
                if np.isfinite(price) and price > 0:
                    total_value = float(self.trade_position.calculate_value()) * self.risk_degree
                    self._desired_amounts[code] = total_value * self._active_weights[code] / price
                else:
                    continue
            target = self._desired_amounts.get(code, 0.0)
            current = current_amounts.get(code, 0.0)
            try:
                factor = self.trade_exchange.get_factor(code, start_time, end_time)
                amount = self.trade_exchange.get_real_deal_amount(current, target, factor)
            except Exception:
                continue
            if abs(float(amount)) <= 1e-8:
                completed.add(code)
                continue
            order = Order(
                stock_id=code,
                amount=abs(float(amount)),
                direction=Order.BUY if amount > 0 else Order.SELL,
                start_time=start_time,
                end_time=end_time,
                factor=factor,
            )
            setattr(order, "_qi_requested_amount", abs(float(amount)))
            (buys if amount > 0 else sells).append(order)
        self._pending_codes.difference_update(completed)
        return sells + buys

    def _replace_orders_for_pending(self, start_time, end_time) -> list[Order]:
        if self._desired_amounts is None:
            return []
        current_amounts = {
            _qlib_code(code): float(amount)
            for code, amount in self.trade_position.get_stock_amount_dict().items()
        }
        exit_orders: list[Order] = []
        completed: set[str] = set()
        for code in sorted(self._pending_codes & self._replace_exit_codes):
            order, is_complete = self._replace_order_for_code(
                code, current_amounts, start_time, end_time
            )
            if is_complete:
                completed.add(code)
            elif order is not None:
                exit_orders.append(order)
        for code in completed:
            self._finish_replace_code(code)

        pending_exits = self._pending_codes & self._replace_exit_codes
        if pending_exits:
            order_codes = {_qlib_code(order.stock_id) for order in exit_orders}
            preview_ok = order_codes == pending_exits and self._preview_replace_exits(
                exit_orders
            )
            self.strategy_audit.append(
                {
                    "status": "replace_exit_preview",
                    "target_date": self._active_date.isoformat() if self._active_date else None,
                    "attempt_date": _as_date(start_time).isoformat(),
                    "exit_codes": sorted(pending_exits),
                    "full_fill": bool(preview_ok),
                    "same_day_entrants": bool(preview_ok),
                }
            )
            if not preview_ok:
                return exit_orders

        entrant_orders: list[Order] = []
        completed = set()
        for code in sorted(self._pending_codes & self._replace_entrant_codes):
            order, is_complete = self._replace_order_for_code(
                code, current_amounts, start_time, end_time
            )
            if is_complete:
                completed.add(code)
            elif order is not None:
                entrant_orders.append(order)
        for code in completed:
            self._finish_replace_code(code)
        return exit_orders + entrant_orders

    def _replace_order_for_code(
        self,
        code: str,
        current_amounts: Mapping[str, float],
        start_time,
        end_time,
    ) -> tuple[Order | None, bool]:
        role = self._replace_role(code)
        current = float(current_amounts.get(code, 0.0))
        if self._is_retired(code, _as_date(start_time)):
            if current <= 1e-8:
                return None, True
            self.strategy_audit.append(
                {
                    "status": "replace_order_deferred",
                    "target_date": self._active_date.isoformat() if self._active_date else None,
                    "attempt_date": _as_date(start_time).isoformat(),
                    "instrument": code,
                    "role": role,
                    "reason": "retired_position_requires_corporate_action",
                }
            )
            return None, False
        if role == "entrant":
            remaining_budget = max(
                0.0,
                self._replace_entrant_budgets.get(code, 0.0)
                - self._replace_entrant_spent.get(code, 0.0),
            )
            if remaining_budget <= 1e-8:
                return None, True
            try:
                price = float(
                    self.trade_exchange.get_deal_price(
                        code, start_time, end_time, direction=Order.BUY
                    )
                )
            except Exception:
                price = np.nan
            if np.isfinite(price) and price > 0:
                self._desired_amounts[code] = current + remaining_budget / price
            else:
                self.strategy_audit.append(
                    {
                        "status": "replace_order_deferred",
                        "target_date": self._active_date.isoformat() if self._active_date else None,
                        "attempt_date": _as_date(start_time).isoformat(),
                        "instrument": code,
                        "role": role,
                        "reason": "invalid_deal_price",
                    }
                )
                return None, False
        target = self._desired_amounts.get(code, 0.0)
        try:
            factor = self.trade_exchange.get_factor(code, start_time, end_time)
            amount = self.trade_exchange.get_real_deal_amount(current, target, factor)
        except Exception as exc:
            self.strategy_audit.append(
                {
                    "status": "replace_order_deferred",
                    "target_date": self._active_date.isoformat() if self._active_date else None,
                    "attempt_date": _as_date(start_time).isoformat(),
                    "instrument": code,
                    "role": role,
                    "reason": "factor_or_amount_unavailable",
                    "error": str(exc),
                }
            )
            return None, False
        if abs(float(amount)) <= 1e-8:
            return None, True
        order = Order(
            stock_id=code,
            amount=abs(float(amount)),
            direction=Order.BUY if amount > 0 else Order.SELL,
            start_time=start_time,
            end_time=end_time,
            factor=factor,
        )
        setattr(order, "_qi_requested_amount", abs(float(amount)))
        setattr(order, "_qi_replace_role", role)
        return order, False

    def _preview_replace_exits(self, orders: Iterable[Order]) -> bool:
        preview_position = copy.deepcopy(self.trade_position)
        exchange = self.trade_exchange
        audit = getattr(exchange, "execution_audit", None)
        audit_snapshot = list(audit) if isinstance(audit, list) else None
        frozen_snapshot = (
            copy.deepcopy(exchange._frozen_buys)
            if hasattr(exchange, "_frozen_buys")
            else None
        )
        frozen_date_snapshot = getattr(exchange, "_frozen_trade_date", None)
        try:
            for source_order in orders:
                order = copy.deepcopy(source_order)
                requested = float(getattr(order, "_qi_requested_amount", order.amount))
                exchange.deal_order(order, position=preview_position)
                if float(order.deal_amount) + 1e-8 < requested:
                    return False
            return True
        except Exception:
            return False
        finally:
            if audit_snapshot is not None:
                audit[:] = audit_snapshot
            if frozen_snapshot is not None:
                exchange._frozen_buys = frozen_snapshot
            if hasattr(exchange, "_frozen_trade_date"):
                exchange._frozen_trade_date = frozen_date_snapshot

    def generate_trade_decision(self, execute_result=None):
        self._consume_previous_result(execute_result)
        step = self.trade_calendar.get_trade_step()
        start_time, end_time = self.trade_calendar.get_step_time(step)
        trade_date = _as_date(start_time)
        self._apply_corporate_actions(trade_date, start_time, end_time)
        if trade_date in self.target_weights:
            self._start_target(trade_date, start_time, end_time)

        if self._desired_amounts is None:
            return TradeDecisionWO([], self)
        if self._attempts >= self._active_retry_days:
            exhausted = {
                "target_date": self._active_date.isoformat() if self._active_date else None,
                "status": "retry_exhausted",
                "retry_limit": self._active_retry_days,
                "pending_codes": sorted(self._pending_codes),
            }
            if self.rebalance_mode == "replace_only":
                exhausted["pending_roles"] = {
                    code: self._replace_role(code) for code in sorted(self._pending_codes)
                }
            self.strategy_audit.append(exhausted)
            self._desired_amounts = None
            self._pending_codes.clear()
            return TradeDecisionWO([], self)

        orders = self._orders_for_pending(start_time, end_time)
        self._attempts += 1
        attempt = {
            "target_date": self._active_date.isoformat() if self._active_date else None,
            "attempt_date": trade_date.isoformat(),
            "attempt": self._attempts,
            "retry_limit": self._active_retry_days,
            "orders": len(orders),
            "pending_codes": sorted(self._pending_codes),
        }
        if self.rebalance_mode == "replace_only":
            attempt["pending_roles"] = {
                code: self._replace_role(code) for code in sorted(self._pending_codes)
            }
        self.strategy_audit.append(attempt)
        return TradeDecisionWO(orders, self)
