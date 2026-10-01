from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from task_caldav_lib.recurrence import (
    is_valid_interval, legacy_rrule_to_interval, next_after_done, next_fixed, parse_interval,
)

LA = ZoneInfo('America/Los_Angeles')


@pytest.mark.parametrize('s', ['P30D', 'P2W', 'P3M', 'P1Y', 'p7d'])
def test_valid_intervals(s):
    assert is_valid_interval(s)


@pytest.mark.parametrize('s', ['', '30', 'P0D', 'PT5H', 'P1M2D', 'every 30 days'])
def test_invalid_intervals(s):
    assert not is_valid_interval(s)
    with pytest.raises(ValueError):
        parse_interval(s)


def test_after_done_date_counts_from_completion_not_due():
    # due long ago, done today -> next due is 30 days from today
    assert next_after_done(date(2026, 1, 1), date(2026, 9, 30), 'P30D', LA) == date(2026, 10, 30)


def test_after_done_without_due():
    assert next_after_done(None, date(2026, 9, 30), 'P1W', LA) == date(2026, 10, 7)


def test_after_done_month_end_clamps():
    assert next_after_done(None, date(2026, 1, 31), 'P1M', LA) == date(2026, 2, 28)


def test_after_done_keeps_local_time_across_dst():
    due = datetime(2026, 10, 20, 9, 0, tzinfo=LA)       # PDT
    nxt = next_after_done(due, date(2026, 10, 25), 'P30D', LA)
    assert nxt == datetime(2026, 11, 24, 9, 0, tzinfo=LA)  # PST, still 9am local
    assert nxt.utcoffset().total_seconds() == -8 * 3600


def test_fixed_first_of_every_other_month():
    rule = 'FREQ=MONTHLY;INTERVAL=2;BYMONTHDAY=1'
    assert next_fixed(date(2026, 9, 1), rule, date(2026, 9, 2), LA) == date(2026, 11, 1)


def test_fixed_skips_past_occurrences():
    # due in January, completed in late September -> first occurrence on/after today
    assert next_fixed(date(2026, 1, 5), 'FREQ=WEEKLY', date(2026, 9, 30), LA) == date(2026, 10, 5)


def test_fixed_timed_keeps_time():
    due = datetime(2026, 9, 30, 8, 30, tzinfo=LA)
    assert next_fixed(due, 'FREQ=DAILY', date(2026, 9, 30), LA) == datetime(2026, 10, 1, 8, 30, tzinfo=LA)


def test_fixed_ended_returns_none():
    assert next_fixed(date(2026, 9, 1), 'FREQ=DAILY;COUNT=1', date(2026, 9, 1), LA) is None


def test_fixed_until_in_utc_with_date_due():
    assert next_fixed(date(2026, 9, 1), 'FREQ=DAILY;UNTIL=20261231T000000Z', date(2026, 9, 1), LA) == date(2026, 9, 2)


@pytest.mark.parametrize('rrule,expected', [
    ('FREQ=DAILY;INTERVAL=30', 'P30D'),
    ('FREQ=WEEKLY', 'P1W'),
    ('FREQ=MONTHLY;INTERVAL=3;WKST=MO', 'P3M'),
    ('FREQ=YEARLY', 'P1Y'),
    ('FREQ=MONTHLY;BYMONTHDAY=1', None),
    ('FREQ=WEEKLY;BYDAY=MO,WE', None),
    ('FREQ=DAILY;COUNT=5', None),
])
def test_legacy_rrule_conversion(rrule, expected):
    assert legacy_rrule_to_interval(rrule) == expected
