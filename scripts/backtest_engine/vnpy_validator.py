"""Independent vn.py execution validator for frozen A-share replay bundles.

The validator deliberately does not import Qlib or QuantInvest's Qlib adapter.
It uses vn.py's engine and data objects, but replaces the native futures-style
matcher and PnL calculation with an A-share cash ledger.  The only accepted
input is a verified validation bundle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
import hashlib
import importlib.metadata
import json
import math
from typing import Any, Mapping

import pandas as pd

try:
    from vnpy.trader.constant import Direction, Exchange, Interval, Offset, Status
    from vnpy.trader.object import BarData, OrderData, TradeData
    from vnpy_portfoliostrategy.backtesting import BacktestingEngine

    VNPY_AVAILABLE = True
    VNPY_IMPORT_ERROR: Exception | None = None
except (ImportError, ModuleNotFoundError) as exc:  # pragma: no cover - depends on isolated env
    VNPY_AVAILABLE = False
    VNPY_IMPORT_ERROR = exc

    class BacktestingEngine:  # type: ignore[no-redef]
        pass


EPS = 1e-8


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _optional_float(value: Any) -> float | None:
    return float(value) if _finite(value) else None


def _as_bool(value: Any) -> bool | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes"}:
            return True
        if text in {"0", "false", "no"}:
            return False
        return None
    return bool(value)


def normalize_code(value: Any) -> str:
    """Normalize a mainland instrument without importing the Qlib adapter."""

    text = str(value).strip().upper()
    if text.startswith(("SH", "SZ", "BJ")) and len(text) >= 8:
        return text[:8]
    if "." in text:
        left, right = text.split(".", 1)
        market = {"SH": "SH", "SSE": "SH", "SZ": "SZ", "SZSE": "SZ", "BJ": "BJ", "BSE": "BJ"}.get(right)
        if market and left.isdigit():
            return market + left.zfill(6)
    digits = "".join(char for char in text if char.isdigit())[-6:]
    if len(digits) != 6:
        raise ValueError(f"invalid instrument code: {value!r}")
    if digits.startswith(("4", "8", "920")):
        return "BJ" + digits
    return ("SH" if digits.startswith(("5", "6", "9")) else "SZ") + digits


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _round_limit(value: float) -> float:
    return math.floor(value * 100.0 + 0.5 + 1e-9) / 100.0


def _default_limit_pct(code: str, trade_date: date) -> float:
    digits = normalize_code(code)[2:]
    if digits.startswith(("4", "8", "920")):
        return 0.30
    if digits.startswith(("688", "689")):
        return 0.20
    if digits.startswith(("300", "301")):
        return 0.20 if trade_date >= date(2020, 8, 24) else 0.10
    return 0.10


def _round_amount_by_lot(amount: float, factor: float, lot_size: int) -> float:
    if amount <= EPS:
        return 0.0
    return math.floor((amount * factor + 0.1) / lot_size) * lot_size / factor


def _performance_metrics(returns: pd.Series) -> dict[str, float | int]:
    values = pd.to_numeric(returns, errors="coerce").fillna(0.0)
    if values.empty:
        return {"n": 0}
    nav = pd.concat([pd.Series([1.0]), (1.0 + values).cumprod().reset_index(drop=True)])
    years = len(values) / 252.0
    std = float(values.std(ddof=1)) if len(values) > 1 else 0.0
    return {
        "n": int(len(values)),
        "total_return": round(float(nav.iloc[-1] - 1.0), 6),
        "annualized_return": round(float(nav.iloc[-1] ** (1.0 / years) - 1.0), 6),
        "sharpe": round(float(values.mean() / std * math.sqrt(252.0)) if std > 0 else 0.0, 4),
        "max_drawdown": round(float((nav / nav.cummax() - 1.0).min()), 6),
    }


@dataclass(frozen=True)
class MarketState:
    suspended: bool
    limit_buy: bool
    limit_sell: bool
    factor: float | None
    limit_pct: float
    limit_up_adj: float | None
    limit_down_adj: float | None
    rule_source: str


def derive_market_state(code: str, trade_date: date, row: Mapping[str, Any]) -> MarketState:
    """Derive direction-specific tradability from raw frozen quote fields."""

    prices = [row.get(name) for name in ("open", "high", "low", "close")]
    volume = row.get("volume_lots")
    change = row.get("change")
    adj = row.get("adj")
    max_adj = row.get("max_adj")
    valid_factor = _finite(adj) and _finite(max_adj) and float(adj) > 0 and float(max_adj) > 0
    factor = float(adj) / float(max_adj) if valid_factor else None
    invalid_quote = (
        not all(_finite(value) and float(value) > 0 for value in prices)
        or not _finite(volume)
        or float(volume) <= 0
        or not _finite(change)
        or factor is None
        or factor <= 0
    )
    explicit_suspended = _as_bool(row.get("suspended"))
    suspended = invalid_quote or explicit_suspended is True

    explicit_pct = _optional_float(row.get("limit_pct"))
    is_st = _as_bool(row.get("is_st")) is True
    limit_pct = (
        explicit_pct
        if explicit_pct is not None and explicit_pct >= 0
        else (0.05 if is_st else _default_limit_pct(code, trade_date))
    )
    has_price_limit = _as_bool(row.get("has_price_limit"))
    if has_price_limit is False:
        return MarketState(
            suspended=suspended,
            limit_buy=suspended,
            limit_sell=suspended,
            factor=factor,
            limit_pct=limit_pct,
            limit_up_adj=None,
            limit_down_adj=None,
            rule_source=str(row.get("rule_source") or "explicit_no_limit"),
        )

    limit_up_adj: float | None = None
    limit_down_adj: float | None = None
    limit_buy = suspended
    limit_sell = suspended
    if not suspended and factor is not None:
        close = float(row["close"])
        prior_close_adj = close / (1.0 + float(change)) if abs(1.0 + float(change)) > EPS else math.nan
        prior_close_raw = prior_close_adj / factor
        if _finite(prior_close_raw) and prior_close_raw > 0:
            limit_up_adj = _round_limit(prior_close_raw * (1.0 + limit_pct)) * factor
            limit_down_adj = _round_limit(prior_close_raw * (1.0 - limit_pct)) * factor
            open_price = float(row["open"])
            limit_buy = open_price >= limit_up_adj - 0.0051
            limit_sell = open_price <= limit_down_adj + 0.0051
        else:
            suspended = limit_buy = limit_sell = True
    return MarketState(
        suspended=suspended,
        limit_buy=limit_buy,
        limit_sell=limit_sell,
        factor=factor,
        limit_pct=limit_pct,
        limit_up_adj=limit_up_adj,
        limit_down_adj=limit_down_adj,
        rule_source=str(
            row.get("rule_source")
            or ("explicit" if explicit_pct is not None or is_st else "board_fallback")
        ),
    )


def _fee_rates(trade_date: date, commission: float, side: str) -> tuple[float, float, float]:
    transfer = 0.00001 if trade_date >= date(2022, 4, 29) else 0.00002
    stamp = 0.0005 if side == "sell" and trade_date >= date(2023, 8, 28) else 0.0
    if side == "sell" and trade_date < date(2023, 8, 28):
        stamp = 0.001
    return commission, transfer, stamp


def _vnpy_version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


class QuantInvestPortfolioEngine(BacktestingEngine):
    """Headless vn.py engine with an independent A-share matcher and ledger."""

    gateway_name = "QI_VALIDATOR"

    def __init__(self, bundle: Any) -> None:
        if not VNPY_AVAILABLE:  # pragma: no cover - exercised by CLI environment check
            raise RuntimeError(
                "vn.py validator dependencies are unavailable; install requirements-vnpy-validator.txt "
                "in a separate environment"
            ) from VNPY_IMPORT_ERROR
        super().__init__()
        self.bundle = bundle
        self.config = dict(bundle.config)
        self.provenance = dict(bundle.provenance)
        self.targets = {
            pd.Timestamp(raw_date).date(): {
                normalize_code(code): float(weight) for code, weight in weights.items()
            }
            for raw_date, weights in bundle.targets.items()
        }
        self.quotes = pd.DataFrame(bundle.quotes).copy()
        self.quotes["date"] = pd.to_datetime(self.quotes["date"]).dt.date
        self.quotes["instrument"] = self.quotes["instrument"].map(normalize_code)
        self.quotes = self.quotes.set_index(["date", "instrument"]).sort_index()
        if self.quotes.index.has_duplicates:
            raise ValueError("bundle quotes contain duplicate date/instrument rows")

        self.initial_cash = float(self.config.get("account", self.config.get("initial_cash", 100_000_000)))
        self.cash = self.initial_cash
        self.risk_degree = float(self.config.get("risk_degree", 0.95))
        self.retry_days = int(self.config.get("retry_days", 5))
        self.commission = float(self.config.get("commission", 0.0003))
        self.volume_participation = float(self.config.get("max_volume_participation", 0.10))
        self.impact_coefficient = float(self.config.get("impact_cost", 0.10))
        self.hedge_yearly_cost = float(self.config.get("hedge_yearly_cost", 0.01))
        self.lot_size = int(self.config.get("trade_unit", 100))
        self.volume_unit_multiplier = float(self.config.get("volume_unit_multiplier", 100))
        self.min_cost = float(self.config.get("min_cost", 5.0))
        self.benchmark = normalize_code(self.config.get("benchmark", "SH000300"))
        if not 0 < self.risk_degree <= 1:
            raise ValueError("risk_degree must be in (0, 1]")
        if self.retry_days < 1 or self.lot_size < 1:
            raise ValueError("retry_days and trade_unit must be positive")

        self.positions: dict[str, float] = {}
        self.last_closes: dict[str, float] = {}
        self.bought_today: dict[str, float] = {}
        self.execution_audit: list[dict[str, Any]] = []
        self.strategy_audit: list[dict[str, Any]] = []
        self.daily_path: list[dict[str, Any]] = []
        self._desired_amounts: dict[str, float] | None = None
        self._active_weights: dict[str, float] = {}
        self._pending_codes: set[str] = set()
        self._attempts = 0
        self._active_date: date | None = None
        self._active_target_id: str | None = None
        self._previous_account = self.initial_cash
        self._previous_exposure = 0.0
        self._previous_benchmark_close: float | None = None
        self._hedged_nav = 1.0

        quote_dates = sorted(set(self.quotes.index.get_level_values("date")))
        if not quote_dates:
            raise ValueError("bundle has no quote dates")
        self.calendar = quote_dates
        self._validate_coverage()

    def _validate_coverage(self) -> None:
        start = min(self.targets) if self.targets else self.calendar[0]
        configured_end = self.config.get("backtest_end_after_final_retry") or self.config.get("end_date")
        end = pd.Timestamp(configured_end).date() if configured_end else self.calendar[-1]
        self.calendar = [item for item in self.calendar if start <= item <= end]
        if not self.calendar:
            raise ValueError("bundle calendar does not cover target schedule")
        missing_targets = sorted(item.isoformat() for item in self.targets if item not in self.calendar)
        if missing_targets:
            raise ValueError(f"target dates missing from quote calendar: {missing_targets[:10]}")
        calendar_set = set(self.calendar)
        benchmark_dates = {
            item for item, code in self.quotes.index if code == self.benchmark and item in calendar_set
        }
        missing_benchmark = sorted(item.isoformat() for item in calendar_set - benchmark_dates)
        if missing_benchmark:
            raise ValueError(f"benchmark quote coverage is incomplete: {missing_benchmark[:10]}")

    @staticmethod
    def _exchange(code: str):
        return {"SH": Exchange.SSE, "SZ": Exchange.SZSE, "BJ": Exchange.BSE}[code[:2]]

    def _bars_for_date(self, trade_date: date, codes: set[str]) -> dict[str, Any]:
        """Create only real vn.py bars required by the current portfolio event."""

        bars: dict[str, Any] = {}
        for code in sorted(codes):
            row = self._row(trade_date, code)
            if row is None:
                continue
            state = derive_market_state(code, trade_date, row)
            if state.suspended or state.factor is None:
                continue
            dt = datetime.combine(trade_date, time())
            factor = state.factor
            bar = BarData(
                symbol=code[2:],
                exchange=self._exchange(code),
                datetime=dt,
                interval=Interval.DAILY,
                volume=float(row["volume_lots"]) * self.volume_unit_multiplier,
                open_price=float(row["open"]) / factor,
                high_price=float(row["high"]) / factor,
                low_price=float(row["low"]) / factor,
                close_price=float(row["close"]) / factor,
                gateway_name=self.gateway_name,
            )
            bars[bar.vt_symbol] = bar
        return bars

    def _row(self, trade_date: date, code: str) -> pd.Series | None:
        try:
            return self.quotes.loc[(trade_date, normalize_code(code))]
        except KeyError:
            return None

    def _current_account(self) -> float:
        value = self.cash
        for code, amount in self.positions.items():
            price = self.last_closes.get(code)
            if price is not None:
                value += amount * price
        return float(value)

    def _start_target(self, trade_date: date) -> None:
        weights = dict(self.targets[trade_date])
        total_value = self._current_account() * self.risk_degree
        desired: dict[str, float] = {}
        for code, weight in weights.items():
            row = self._row(trade_date, code)
            if row is not None and _finite(row.get("open")) and float(row["open"]) > 0:
                desired[code] = total_value * weight / float(row["open"])
        for code in self.positions:
            desired.setdefault(code, 0.0)
        self._active_weights = weights
        self._desired_amounts = desired
        self._pending_codes = set(weights) | set(desired)
        self._attempts = 0
        self._active_date = trade_date
        self._active_target_id = _canonical_hash(
            {"trade_date": trade_date.isoformat(), "weights": dict(sorted(weights.items()))}
        )

    def _requested_orders(self, trade_date: date) -> list[tuple[str, str, float, float]]:
        if self._desired_amounts is None:
            return []
        completed: set[str] = set()
        orders: list[tuple[str, str, float, float]] = []
        for code in sorted(self._pending_codes):
            row = self._row(trade_date, code)
            if row is None:
                continue
            state = derive_market_state(code, trade_date, row)
            factor = state.factor
            if code not in self._desired_amounts and code in self._active_weights:
                if _finite(row.get("open")) and float(row["open"]) > 0:
                    total_value = self._current_account() * self.risk_degree
                    self._desired_amounts[code] = (
                        total_value * self._active_weights[code] / float(row["open"])
                    )
                else:
                    continue
            if factor is None or factor <= 0:
                continue
            target = self._desired_amounts.get(code, 0.0)
            current = self.positions.get(code, 0.0)
            if current < target:
                amount = _round_amount_by_lot(target - current, factor, self.lot_size)
            elif current > target:
                amount = current if abs(target) <= EPS else _round_amount_by_lot(current - target, factor, self.lot_size)
                amount = -amount
            else:
                amount = 0.0
            if abs(amount) <= EPS:
                completed.add(code)
                continue
            orders.append(("buy" if amount > 0 else "sell", code, abs(amount), factor))
        self._pending_codes.difference_update(completed)
        return sorted(orders, key=lambda item: (0 if item[0] == "sell" else 1, item[1]))

    def _new_order(self, trade_date: date, side: str, code: str, requested: float, factor: float, row: pd.Series):
        self.limit_order_count += 1
        direction = Direction.LONG if side == "buy" else Direction.SHORT
        raw_price = float(row["open"]) / factor
        order = OrderData(
            symbol=code[2:],
            exchange=self._exchange(code),
            orderid=str(self.limit_order_count),
            direction=direction,
            offset=Offset.OPEN if side == "buy" else Offset.CLOSE,
            price=raw_price,
            volume=requested * factor,
            status=Status.NOTTRADED,
            datetime=datetime.combine(trade_date, time()),
            gateway_name=self.gateway_name,
            reference=f"target:{self._active_target_id}",
        )
        self.limit_orders[order.vt_orderid] = order
        return order

    def _match_order(self, trade_date: date, side: str, code: str, requested: float, factor: float) -> float:
        row = self._row(trade_date, code)
        if row is None:
            return 0.0
        state = derive_market_state(code, trade_date, row)
        order = self._new_order(trade_date, side, code, requested, factor, row)
        cash_before = self.cash
        position_before = self.positions.get(code, 0.0)
        requested_original = requested
        t1_clipped = False
        if side == "sell":
            sellable = max(0.0, position_before - self.bought_today.get(code, 0.0))
            if requested > sellable:
                requested = sellable
                t1_clipped = True

        blocked = state.suspended or (side == "buy" and state.limit_buy) or (side == "sell" and state.limit_sell)
        filled = 0.0
        trade_value = 0.0
        trade_cost = 0.0
        impact_rate = 0.0
        if not blocked and requested > EPS:
            adjusted_price = float(row["open"])
            normalized_daily_volume = float(row["volume_lots"]) * self.volume_unit_multiplier / factor
            volume_cap = max(0.0, self.volume_participation * normalized_daily_volume)
            filled = min(requested, volume_cap)
            total_trade_value = normalized_daily_volume * adjusted_price
            pre_round_value = filled * adjusted_price
            impact_rate = (
                self.impact_coefficient * (pre_round_value / total_trade_value) ** 2
                if total_trade_value > 0
                else self.impact_coefficient
            )
            commission_rate, transfer_rate, stamp_rate = _fee_rates(trade_date, self.commission, side)
            cost_ratio = commission_rate + transfer_rate + stamp_rate + impact_rate
            if side == "sell":
                if not math.isclose(filled, position_before, rel_tol=1e-9, abs_tol=1e-8):
                    filled = _round_amount_by_lot(min(position_before, filled), factor, self.lot_size)
                if self.cash + filled * adjusted_price < max(filled * adjusted_price * cost_ratio, self.min_cost):
                    filled = 0.0
            else:
                trade_value_before_cash = filled * adjusted_price
                if self.cash < max(trade_value_before_cash * cost_ratio, self.min_cost):
                    filled = 0.0
                elif self.cash < trade_value_before_cash + max(trade_value_before_cash * cost_ratio, self.min_cost):
                    if self.cash >= self.min_cost:
                        critical_cash = self.min_cost / cost_ratio + self.min_cost if cost_ratio > 0 else math.inf
                        if self.cash >= critical_cash:
                            max_buy = self.cash / (1.0 + cost_ratio) / adjusted_price
                        else:
                            max_buy = (self.cash - self.min_cost) / adjusted_price
                        filled = _round_amount_by_lot(min(max_buy, filled), factor, self.lot_size)
                    else:
                        filled = 0.0
                else:
                    filled = _round_amount_by_lot(filled, factor, self.lot_size)
            trade_value = filled * adjusted_price
            trade_cost = max(trade_value * cost_ratio, self.min_cost) if trade_value > 1e-5 else 0.0

        if filled > EPS:
            if side == "buy":
                self.cash -= trade_value + trade_cost
                self.positions[code] = position_before + filled
                self.bought_today[code] = self.bought_today.get(code, 0.0) + filled
            else:
                self.cash += trade_value - trade_cost
                remaining = position_before - filled
                if remaining <= EPS:
                    self.positions.pop(code, None)
                else:
                    self.positions[code] = remaining
            self.trade_count += 1
            trade = TradeData(
                symbol=code[2:],
                exchange=self._exchange(code),
                orderid=order.orderid,
                tradeid=str(self.trade_count),
                direction=order.direction,
                offset=order.offset,
                price=float(row["open"]) / factor,
                volume=filled * factor,
                datetime=datetime.combine(trade_date, time()),
                gateway_name=self.gateway_name,
            )
            self.trades[trade.vt_tradeid] = trade
            order.traded = filled * factor
            order.status = Status.ALLTRADED if filled + EPS >= requested_original else Status.PARTTRADED
        else:
            order.status = Status.REJECTED

        if t1_clipped and filled <= EPS:
            reason = "t1_frozen"
        elif blocked:
            if state.suspended:
                reason = "suspended"
            elif side == "buy" and state.limit_buy:
                reason = "limit_buy"
            elif side == "sell" and state.limit_sell:
                reason = "limit_sell"
            else:  # pragma: no cover - defensive
                reason = "untradable"
        elif filled <= EPS:
            reason = "cash_or_volume"
        elif filled + EPS < requested_original:
            reason = "partial"
        else:
            reason = "filled"
        self.execution_audit.append(
            {
                "target_id": self._active_target_id,
                "target_date": self._active_date.isoformat() if self._active_date else None,
                "attempt": self._attempts + 1,
                "trade_date": trade_date.isoformat(),
                "instrument": code,
                "side": side,
                "requested_amount": requested_original,
                "deal_amount": filled,
                "factor": factor,
                "requested_raw_shares": requested_original * factor,
                "deal_raw_shares": filled * factor,
                "trade_price": float(row["open"]) if not blocked else None,
                "raw_trade_price": float(row["open"]) / factor if not blocked else None,
                "trade_value": trade_value,
                "trade_cost": trade_cost,
                "impact_rate": impact_rate,
                "reason": reason,
                "market_rule_source": state.rule_source,
                "limit_pct": state.limit_pct,
                "limit_up_price": state.limit_up_adj,
                "limit_down_price": state.limit_down_adj,
                "cash_before": cash_before,
                "cash_after": self.cash,
                "position_before": position_before,
                "position_after": self.positions.get(code, 0.0),
            }
        )
        return filled

    def _execute_pending(self, trade_date: date) -> None:
        if self._desired_amounts is None:
            return
        if self._attempts >= self.retry_days:
            self.strategy_audit.append(
                {
                    "target_date": self._active_date.isoformat() if self._active_date else None,
                    "status": "retry_exhausted",
                    "pending_codes": sorted(self._pending_codes),
                }
            )
            self._desired_amounts = None
            self._pending_codes.clear()
            return
        orders = self._requested_orders(trade_date)
        pending_before = sorted(self._pending_codes)
        for side, code, requested, factor in orders:
            filled = self._match_order(trade_date, side, code, requested, factor)
            if filled + EPS >= requested:
                self._pending_codes.discard(code)
        self._attempts += 1
        self.strategy_audit.append(
            {
                "target_date": self._active_date.isoformat() if self._active_date else None,
                "attempt_date": trade_date.isoformat(),
                "attempt": self._attempts,
                "orders": len(orders),
                "pending_codes_before": pending_before,
                "pending_codes": sorted(self._pending_codes),
            }
        )
        if not self._pending_codes:
            self._desired_amounts = None

    def _mark_day(self, trade_date: date, day_cost: float, day_turnover: float) -> None:
        for code in set(self.positions) | {self.benchmark}:
            row = self._row(trade_date, code)
            if row is not None and _finite(row.get("close")) and float(row["close"]) > 0:
                self.last_closes[code] = float(row["close"])
        stock_value = sum(
            amount * self.last_closes.get(code, 0.0) for code, amount in self.positions.items()
        )
        account = self.cash + stock_value
        net_return = account / self._previous_account - 1.0 if self._previous_account else 0.0
        exposure = stock_value / account if account > 0 else 0.0
        benchmark_row = self._row(trade_date, self.benchmark)
        benchmark_open = float(benchmark_row["open"]) if benchmark_row is not None and _finite(benchmark_row.get("open")) else math.nan
        benchmark_close = float(benchmark_row["close"]) if benchmark_row is not None and _finite(benchmark_row.get("close")) else math.nan
        benchmark_overnight = (
            benchmark_open / self._previous_benchmark_close - 1.0
            if self._previous_benchmark_close and _finite(benchmark_open)
            else 0.0
        )
        benchmark_intraday = benchmark_close / benchmark_open - 1.0 if _finite(benchmark_open) and _finite(benchmark_close) else 0.0
        hedge_proxy_return = self._previous_exposure * benchmark_overnight + exposure * benchmark_intraday
        hedge_notional = (self._previous_exposure + exposure) / 2.0
        hedge_cost = self.hedge_yearly_cost / 252.0 * hedge_notional
        hedged_return = net_return - hedge_proxy_return - hedge_cost
        self._hedged_nav *= 1.0 + hedged_return
        positions_hash = _canonical_hash(
            {code: round(amount, 10) for code, amount in sorted(self.positions.items()) if amount > EPS}
        )
        self.daily_path.append(
            {
                "date": trade_date.isoformat(),
                "cash": round(self.cash, 2),
                "stock_value": round(stock_value, 2),
                "account": round(account, 2),
                "turnover": round(day_turnover, 2),
                "cost": round(day_cost, 2),
                "net_return": round(net_return, 12),
                "stock_exposure": round(exposure, 10),
                "benchmark_overnight": round(benchmark_overnight, 12),
                "benchmark_intraday": round(benchmark_intraday, 12),
                "hedge_proxy_return": round(hedge_proxy_return, 12),
                "hedge_cost": round(hedge_cost, 12),
                "hedged_return": round(hedged_return, 12),
                "hedged_nav": round(self._hedged_nav, 10),
                "positions_hash": positions_hash,
            }
        )
        self._previous_account = account
        self._previous_exposure = exposure
        if _finite(benchmark_close):
            self._previous_benchmark_close = benchmark_close

    def run_validation(self) -> dict[str, Any]:
        for trade_date in self.calendar:
            self.datetime = datetime.combine(trade_date, time())
            self.bought_today = {}
            event_codes = set(self.positions) | set(self._pending_codes) | {self.benchmark}
            event_codes.update(self.targets.get(trade_date, {}))
            self.bars = self._bars_for_date(trade_date, event_codes)
            before = len(self.execution_audit)
            if trade_date in self.targets:
                self._start_target(trade_date)
            self._execute_pending(trade_date)
            day_rows = self.execution_audit[before:]
            self._mark_day(
                trade_date,
                sum(float(item["trade_cost"]) for item in day_rows),
                sum(float(item["trade_value"]) for item in day_rows),
            )

        daily = pd.DataFrame(self.daily_path)
        daily.index = pd.to_datetime(daily["date"])
        full_returns = daily["net_return"].astype(float)
        hedged_returns = daily["hedged_return"].astype(float)
        oos = daily.index >= pd.Timestamp("2022-01-01")
        final_positions = {
            code: amount for code, amount in sorted(self.positions.items()) if amount > EPS
        }
        result = {
            "schema_version": 1,
            "status": "independent_research_validation",
            "engine": {
                "name": "quantinvest_vnpy_a_share_validator",
                "vnpy": _vnpy_version("vnpy"),
                "vnpy_portfoliostrategy": _vnpy_version("vnpy_portfoliostrategy"),
                "native_matcher_replaced": True,
                "qlib_imported": False,
            },
            "bundle": {
                "manifest_sha256": _canonical_hash(self.bundle.manifest),
                "payloads": self.bundle.manifest.get("files", self.bundle.manifest.get("payloads", {})),
            },
            "config": self.config,
            "execution_metrics": {
                "long_only_full": _performance_metrics(full_returns),
                "long_only_2022_plus": _performance_metrics(full_returns.loc[oos]),
                "exposure_matched_hedged_full": _performance_metrics(hedged_returns),
                "exposure_matched_hedged_2022_plus": _performance_metrics(hedged_returns.loc[oos]),
                "final_account": round(float(daily["account"].iloc[-1]), 2),
                "total_cost": round(sum(float(item["trade_cost"]) for item in self.execution_audit), 2),
            },
            "orders": {
                "attempts": len(self.execution_audit),
                "trades": len(self.trades),
                "unfilled": sum(float(item["deal_amount"]) <= EPS for item in self.execution_audit),
            },
            "final_position": {
                "date": self.calendar[-1].isoformat(),
                "holding_count": len(final_positions),
                "adjusted_amounts": final_positions,
                "cash": round(self.cash, 2),
            },
            "daily_path": self.daily_path,
            "execution_audit": self.execution_audit,
            "strategy_audit": self.strategy_audit,
            "caveats": [
                "The validator checks execution and accounting for frozen targets; it does not reselect factors or regimes.",
                "Historical ST and IPO rule overrides remain required before publication if the bundle uses board_fallback on attempted orders.",
                "Aggregate minimum-cost behavior intentionally matches the current Qlib audit and remains a documented approximation.",
            ],
        }
        return result


__all__ = [
    "QuantInvestPortfolioEngine",
    "VNPY_AVAILABLE",
    "derive_market_state",
    "normalize_code",
]
