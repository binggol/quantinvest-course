from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from typing import Iterable, Literal
from zoneinfo import ZoneInfo


IntradayExecutionPolicy = Literal["same_day_close", "next_open"]
ExecutionSession = Literal["open", "close"]
SourcePrecision = Literal["date", "datetime"]


@dataclass(frozen=True)
class EventExecution:
    """Point-in-time mapping from an event timestamp to an executable session."""

    reported_at: datetime
    known_at: datetime
    effective_execution_at: datetime
    execution_date: date
    execution_session: ExecutionSession
    price_field: Literal["open", "close"]
    timing_rule: str
    source_precision: SourcePrecision

    def as_dict(self) -> dict[str, str]:
        payload = asdict(self)
        for key in ("reported_at", "known_at", "effective_execution_at"):
            payload[key] = payload[key].isoformat(timespec="seconds")
        payload["execution_date"] = self.execution_date.isoformat()
        return payload


class EventClockAdapter:
    """Map disclosure timestamps to daily backtest execution points.

    A midnight timestamp is treated as date precision, not as proof that the
    event was known at midnight. For a date-precision event, ``known_at`` is
    conservatively normalized to the first executable market open.
    """

    def __init__(
        self,
        trading_dates: Iterable[date | datetime | str],
        *,
        timezone: str = "Asia/Shanghai",
        market_open: time = time(9, 30),
        market_close: time = time(15, 0),
    ) -> None:
        self.timezone = ZoneInfo(timezone)
        self.market_open = market_open
        self.market_close = market_close
        self.trading_dates = tuple(sorted({_parse_date(value) for value in trading_dates}))
        if not self.trading_dates:
            raise ValueError("trading_dates must not be empty")
        if market_open >= market_close:
            raise ValueError("market_open must be earlier than market_close")

    def resolve(
        self,
        announced_at: date | datetime | str,
        *,
        intraday_policy: IntradayExecutionPolicy = "same_day_close",
    ) -> EventExecution | None:
        """Return the first permitted daily execution, or ``None`` past the calendar.

        ``same_day_close`` reuses the earnings-event convention: a trading-day
        intraday disclosure is known before and executed at that day's close.
        ``next_open`` applies a stricter daily-bar convention and waits for the
        next trading session's open.
        """

        if intraday_policy not in ("same_day_close", "next_open"):
            raise ValueError(f"unsupported intraday_policy: {intraday_policy}")

        reported_at, source_precision = self._parse_announcement(announced_at)
        announced_day = reported_at.date()
        is_trading_day = self._is_trading_day(announced_day)

        if source_precision == "date":
            execution_day = announced_day if is_trading_day else self._next_trading_day(announced_day)
            if execution_day is None:
                return None
            execution_at = self._at(execution_day, self.market_open)
            rule = (
                "same_day_open_date_precision"
                if is_trading_day
                else "next_open_non_trading_date_precision"
            )
            return EventExecution(
                reported_at=reported_at,
                known_at=execution_at,
                effective_execution_at=execution_at,
                execution_date=execution_day,
                execution_session="open",
                price_field="open",
                timing_rule=rule,
                source_precision=source_precision,
            )

        if not is_trading_day:
            execution_day = self._next_trading_day(announced_day)
            return self._open_execution(
                reported_at,
                execution_day,
                known_at=reported_at,
                rule="next_open_non_trading_announcement_day",
                source_precision=source_precision,
            )

        announced_time = reported_at.timetz().replace(tzinfo=None)
        if announced_time < self.market_open:
            execution_at = self._at(announced_day, self.market_open)
            return EventExecution(
                reported_at=reported_at,
                known_at=reported_at,
                effective_execution_at=execution_at,
                execution_date=announced_day,
                execution_session="open",
                price_field="open",
                timing_rule="same_day_open_pre_market",
                source_precision=source_precision,
            )

        if announced_time < self.market_close:
            if intraday_policy == "same_day_close":
                execution_at = self._at(announced_day, self.market_close)
                return EventExecution(
                    reported_at=reported_at,
                    known_at=reported_at,
                    effective_execution_at=execution_at,
                    execution_date=announced_day,
                    execution_session="close",
                    price_field="close",
                    timing_rule="same_day_close_intraday",
                    source_precision=source_precision,
                )
            execution_day = self._next_trading_day(announced_day)
            return self._open_execution(
                reported_at,
                execution_day,
                known_at=reported_at,
                rule="next_open_after_intraday",
                source_precision=source_precision,
            )

        execution_day = self._next_trading_day(announced_day)
        return self._open_execution(
            reported_at,
            execution_day,
            known_at=reported_at,
            rule="next_open_after_close",
            source_precision=source_precision,
        )

    def _parse_announcement(
        self, announced_at: date | datetime | str
    ) -> tuple[datetime, SourcePrecision]:
        parsed, date_only_input = _parse_datetime(announced_at)
        if parsed.tzinfo is None:
            local = parsed.replace(tzinfo=self.timezone)
        else:
            local = parsed.astimezone(self.timezone)

        # CNInfo 00:00 is a date placeholder even when serialized as a datetime.
        is_midnight_placeholder = local.time().replace(tzinfo=None) == time.min
        precision: SourcePrecision = "date" if date_only_input or is_midnight_placeholder else "datetime"
        return local, precision

    def _is_trading_day(self, value: date) -> bool:
        index = bisect_left(self.trading_dates, value)
        return index < len(self.trading_dates) and self.trading_dates[index] == value

    def _next_trading_day(self, value: date) -> date | None:
        index = bisect_right(self.trading_dates, value)
        return self.trading_dates[index] if index < len(self.trading_dates) else None

    def _at(self, value: date, value_time: time) -> datetime:
        return datetime.combine(value, value_time, tzinfo=self.timezone)

    def _open_execution(
        self,
        reported_at: datetime,
        execution_day: date | None,
        *,
        known_at: datetime,
        rule: str,
        source_precision: SourcePrecision,
    ) -> EventExecution | None:
        if execution_day is None:
            return None
        execution_at = self._at(execution_day, self.market_open)
        return EventExecution(
            reported_at=reported_at,
            known_at=known_at,
            effective_execution_at=execution_at,
            execution_date=execution_day,
            execution_session="open",
            price_field="open",
            timing_rule=rule,
            source_precision=source_precision,
        )


def _parse_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"invalid trading date: {value!r}")


def _parse_datetime(value: date | datetime | str) -> tuple[datetime, bool]:
    if isinstance(value, datetime):
        return value, False
    if isinstance(value, date):
        return datetime.combine(value, time.min), True

    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.combine(datetime.strptime(text, fmt).date(), time.min), True
        except ValueError:
            pass

    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        return datetime.fromisoformat(normalized), False
    except ValueError as exc:
        raise ValueError(f"invalid announcement datetime: {value!r}") from exc
