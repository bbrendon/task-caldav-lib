"""
Recurrence math.

Two kinds of repeat:
  - "after done" (X-TASKWEB-RECUR-AFTER, e.g. 'P30D'): next due = completion date + interval
  - fixed schedule (RRULE): next due = next occurrence after the current due date,
    skipping occurrences already in the past

Due values are python `date` (all-day) or tz-aware `datetime`. Time-of-day is
preserved in the local time zone, so DST changes don't shift a 9am task to 8am.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, tzinfo
from typing import Optional, Union

from dateutil.relativedelta import relativedelta
from dateutil.rrule import rrulestr

log = logging.getLogger(__name__)

DueValue = Union[date, datetime]

_INTERVAL_RE = re.compile(r'^P(\d+)([DWMY])$')
_UNITS = {'D': 'days', 'W': 'weeks', 'M': 'months', 'Y': 'years'}
_FREQ_TO_UNIT = {'DAILY': 'D', 'WEEKLY': 'W', 'MONTHLY': 'M', 'YEARLY': 'Y'}


def parse_interval(s: str) -> relativedelta:
    """'P30D' / 'P2W' / 'P3M' / 'P1Y' -> relativedelta."""
    m = _INTERVAL_RE.match((s or '').strip().upper())
    if not m or int(m.group(1)) < 1:
        raise ValueError(f"Invalid repeat interval {s!r}; expected e.g. 'P30D', 'P2W', 'P1M'")
    return relativedelta(**{_UNITS[m.group(2)]: int(m.group(1))})


def is_valid_interval(s: str) -> bool:
    try:
        parse_interval(s)
        return True
    except ValueError:
        return False


def next_after_done(due: Optional[DueValue], done_on: date, interval: str, tz: tzinfo) -> DueValue:
    """Next due for an after-done task completed on local date `done_on`."""
    new_date = done_on + parse_interval(interval)
    if isinstance(due, datetime):
        local = due.astimezone(tz)
        return datetime.combine(new_date, local.time(), tzinfo=tz)
    return new_date


def next_fixed(due: Optional[DueValue], rrule: str, today: date, tz: tzinfo) -> Optional[DueValue]:
    """Next occurrence after `due`, advanced until it is on/after `today`. None if the rule ended."""
    timed = isinstance(due, datetime)
    if due is None:
        anchor = datetime.combine(today, time())
    elif timed:
        anchor = due.astimezone(tz).replace(tzinfo=None)
    else:
        anchor = datetime.combine(due, time())

    try:
        rule = rrulestr(rrule, dtstart=anchor)
    except ValueError:
        # e.g. UNTIL given in UTC while our anchor is floating local time
        rule = rrulestr(_strip_until_tz(rrule), dtstart=anchor)

    nxt = rule.after(anchor, inc=due is None)
    while nxt is not None and nxt.date() < today:
        nxt = rule.after(nxt)
    if nxt is None:
        return None
    return nxt.replace(tzinfo=tz) if timed else nxt.date()


def legacy_rrule_to_interval(rrule: str) -> Optional[str]:
    """Convert a simple 'every N units' RRULE to an after-done interval, else None.

    Used by the one-time migration: the old library treated every RRULE as
    "after completion", but only plain FREQ/INTERVAL rules map cleanly.
    """
    parts = dict(p.split('=', 1) for p in rrule.upper().split(';') if '=' in p)
    parts.pop('WKST', None)
    freq = parts.pop('FREQ', None)
    interval = parts.pop('INTERVAL', '1')
    if parts or freq not in _FREQ_TO_UNIT or not interval.isdigit():
        return None
    return f'P{int(interval)}{_FREQ_TO_UNIT[freq]}'


def _strip_until_tz(rrule: str) -> str:
    return re.sub(r'(UNTIL=\d{8}T\d{6})Z', r'\1', rrule, flags=re.IGNORECASE)
