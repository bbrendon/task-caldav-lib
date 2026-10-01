"""
Virtual tags — computed at runtime from task fields, in the user's time zone.
No storage — pure derived state.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, tzinfo
from typing import Iterable, Optional

from dateutil.parser import isoparse

from .model import STATUS_DONE, priority_band

VIRTUAL_TAGS = (
    'PENDING', 'OVERDUE', 'DUE_TODAY', 'DUE_WEEK', 'DEFERRED', 'RECURRING', 'SINGLE',
    'HAS_SUBTASKS', 'CHILD', 'STARRED', 'HIGH', 'MEDIUM', 'LOW', 'TAGGED', 'UNTAGGED',
)


def _value(s: Optional[str], tz: tzinfo):
    if not s:
        return None
    if len(s) == 10:
        return date.fromisoformat(s)
    dt = isoparse(s)
    return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).astimezone(tz)


def _day(v) -> Optional[date]:
    if v is None:
        return None
    return v.date() if isinstance(v, datetime) else v


def compute_virtual_tags(
    task: dict, parent_uids: set[str], tz: tzinfo, now: Optional[datetime] = None, week_days: int = 7,
) -> list[str]:
    """Virtual tags for one task dict (Task.to_dict()).

    Args:
        parent_uids: UIDs that at least one task points to as its parent
        week_days: length of the "due this week" window, starting today
    """
    now = (now or datetime.now(tz)).astimezone(tz)
    today = now.date()
    tags: list[str] = []
    is_open = task.get('status', 'NEEDS-ACTION').upper() not in STATUS_DONE
    due = _value(task.get('due'), tz)
    due_day = _day(due)
    start_day = _day(_value(task.get('start'), tz))

    if is_open:
        tags.append('PENDING')
        if due is not None:
            overdue = due < now if isinstance(due, datetime) else due < today
            if overdue:
                tags.append('OVERDUE')
            if due_day == today:
                tags.append('DUE_TODAY')
            if today <= due_day < today + timedelta(days=week_days):
                tags.append('DUE_WEEK')
        if start_day and start_day > today:
            tags.append('DEFERRED')

    tags.append('RECURRING' if task.get('recur_after') or task.get('rrule') else 'SINGLE')
    if task.get('uid') in parent_uids:
        tags.append('HAS_SUBTASKS')
    if task.get('parent_uid'):
        tags.append('CHILD')
    if task.get('starred'):
        tags.append('STARRED')
    band = priority_band(task.get('priority') or 0)
    if band != 'none':
        tags.append(band.upper())
    tags.append('TAGGED' if task.get('tags') else 'UNTAGGED')
    return tags


def compute_parent_uids(tasks: Iterable[dict]) -> set[str]:
    return {t['parent_uid'] for t in tasks if t.get('parent_uid')}


def add_virtual_tags(tasks: list[dict], tz: tzinfo, week_days: int = 7) -> list[dict]:
    """Add a 'virtual_tags' key to each task dict (mutates and returns the list)."""
    parents = compute_parent_uids(tasks)
    now = datetime.now(tz)
    for t in tasks:
        t['virtual_tags'] = compute_virtual_tags(t, parents, tz, now, week_days)
    return tasks
