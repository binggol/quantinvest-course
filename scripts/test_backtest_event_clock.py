from __future__ import annotations

from datetime import date, datetime

import pytest

from scripts.backtest_engine.event_clock import EventClockAdapter


TRADING_DATES = ["2026-07-03", "2026-07-06", "2026-07-07", "2026-07-09"]


def make_clock() -> EventClockAdapter:
    return EventClockAdapter(reversed(TRADING_DATES))


@pytest.mark.parametrize(
    "announced_at",
    ["2026-07-03", "20260703", date(2026, 7, 3), "2026-07-03 00:00:00"],
)
def test_date_precision_and_midnight_placeholder_execute_at_same_day_open(announced_at):
    result = make_clock().resolve(announced_at)

    assert result is not None
    assert result.source_precision == "date"
    assert result.execution_date == date(2026, 7, 3)
    assert result.execution_session == "open"
    assert result.price_field == "open"
    assert result.timing_rule == "same_day_open_date_precision"
    assert result.known_at == result.effective_execution_at
    assert result.effective_execution_at.isoformat() == "2026-07-03T09:30:00+08:00"


@pytest.mark.parametrize("announced_at", ["2026-07-03 08:15:00", "2026-07-03 09:29:59"])
def test_explicit_pre_market_timestamp_executes_at_same_day_open(announced_at):
    result = make_clock().resolve(announced_at)

    assert result is not None
    assert result.source_precision == "datetime"
    assert result.execution_date == date(2026, 7, 3)
    assert result.execution_session == "open"
    assert result.timing_rule == "same_day_open_pre_market"
    assert result.known_at <= result.effective_execution_at


def test_exact_market_open_cannot_reuse_the_open_price():
    result = make_clock().resolve("2026-07-03 09:30:00")

    assert result is not None
    assert result.execution_date == date(2026, 7, 3)
    assert result.execution_session == "close"
    assert result.timing_rule == "same_day_close_intraday"


def test_exact_market_close_executes_at_next_open():
    result = make_clock().resolve("2026-07-03 15:00:00")

    assert result is not None
    assert result.execution_date == date(2026, 7, 6)
    assert result.execution_session == "open"
    assert result.timing_rule == "next_open_after_close"


def test_intraday_defaults_to_same_day_close_like_earnings_backtest():
    result = make_clock().resolve("2026-07-03 13:20:00")

    assert result is not None
    assert result.execution_date == date(2026, 7, 3)
    assert result.execution_session == "close"
    assert result.price_field == "close"
    assert result.timing_rule == "same_day_close_intraday"
    assert result.known_at.isoformat() == "2026-07-03T13:20:00+08:00"
    assert result.effective_execution_at.isoformat() == "2026-07-03T15:00:00+08:00"


def test_intraday_can_use_strict_next_trading_day_open_policy():
    result = make_clock().resolve("2026-07-03 13:20:00", intraday_policy="next_open")

    assert result is not None
    assert result.execution_date == date(2026, 7, 6)
    assert result.execution_session == "open"
    assert result.timing_rule == "next_open_after_intraday"
    assert result.known_at < result.effective_execution_at


def test_after_close_on_friday_executes_at_monday_open():
    result = make_clock().resolve("2026-07-03 18:20:00")

    assert result is not None
    assert result.execution_date == date(2026, 7, 6)
    assert result.execution_session == "open"
    assert result.timing_rule == "next_open_after_close"


@pytest.mark.parametrize(
    ("announced_at", "expected_rule"),
    [
        ("2026-07-04 09:00:00", "next_open_non_trading_announcement_day"),
        ("2026-07-08 10:00:00", "next_open_non_trading_announcement_day"),
        ("2026-07-08", "next_open_non_trading_date_precision"),
    ],
)
def test_weekend_and_closed_dates_execute_at_next_market_open(announced_at, expected_rule):
    result = make_clock().resolve(announced_at)

    assert result is not None
    expected_date = date(2026, 7, 6) if str(announced_at).startswith("2026-07-04") else date(2026, 7, 9)
    assert result.execution_date == expected_date
    assert result.execution_session == "open"
    assert result.timing_rule == expected_rule
    assert result.known_at <= result.effective_execution_at


def test_aware_utc_timestamp_is_converted_before_market_classification():
    result = make_clock().resolve("2026-07-03T01:00:00Z")

    assert result is not None
    assert result.reported_at.isoformat() == "2026-07-03T09:00:00+08:00"
    assert result.execution_date == date(2026, 7, 3)
    assert result.execution_session == "open"


def test_effective_fields_are_serialized_for_point_in_time_audit():
    result = make_clock().resolve(datetime(2026, 7, 3, 13, 20))

    assert result is not None
    payload = result.as_dict()
    assert payload["known_at"] == "2026-07-03T13:20:00+08:00"
    assert payload["effective_execution_at"] == "2026-07-03T15:00:00+08:00"
    assert payload["execution_date"] == "2026-07-03"
    assert payload["source_precision"] == "datetime"


def test_no_execution_is_returned_when_calendar_has_no_later_session():
    assert make_clock().resolve("2026-07-09 18:00:00") is None
    assert make_clock().resolve("2026-07-10") is None


def test_invalid_inputs_fail_explicitly():
    with pytest.raises(ValueError, match="trading_dates must not be empty"):
        EventClockAdapter([])
    with pytest.raises(ValueError, match="unsupported intraday_policy"):
        make_clock().resolve("2026-07-03 10:00:00", intraday_policy="immediate")
    with pytest.raises(ValueError, match="invalid announcement datetime"):
        make_clock().resolve("not-a-date")
