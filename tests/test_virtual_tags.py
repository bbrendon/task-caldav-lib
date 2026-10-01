from datetime import datetime
from zoneinfo import ZoneInfo

from task_caldav_lib.virtual_tags import add_virtual_tags, compute_virtual_tags

LA = ZoneInfo('America/Los_Angeles')
NOW = datetime(2026, 9, 30, 21, 0, tzinfo=LA)   # 9pm local = already Oct 1 in UTC


def tags(**task):
    task.setdefault('status', 'NEEDS-ACTION')
    return compute_virtual_tags(task, set(), LA, NOW)


def test_all_day_due_today_is_not_overdue_late_in_the_day():
    t = tags(due='2026-09-30')
    assert 'DUE_TODAY' in t and 'OVERDUE' not in t and 'DUE_WEEK' in t


def test_timed_due_earlier_today_is_overdue():
    assert 'OVERDUE' in tags(due='2026-09-30T09:00:00-07:00')


def test_yesterday_overdue_and_week_window():
    assert 'OVERDUE' in tags(due='2026-09-29')
    assert 'DUE_WEEK' in tags(due='2026-10-06') and 'DUE_WEEK' not in tags(due='2026-10-07')


def test_completed_is_not_pending_or_overdue():
    t = tags(due='2026-09-01', status='COMPLETED')
    assert not {'PENDING', 'OVERDUE'} & set(t)


def test_deferred_recurring_priority_star():
    t = tags(start='2026-10-05', recur_after='P30D', priority=1, starred=True, tags=['Home'])
    assert {'DEFERRED', 'RECURRING', 'HIGH', 'STARRED', 'TAGGED'} <= set(t)


def test_has_subtasks():
    ts = add_virtual_tags([{'uid': 'p'}, {'uid': 'c', 'parent_uid': 'p'}], LA)
    assert 'HAS_SUBTASKS' in ts[0]['virtual_tags'] and 'CHILD' in ts[1]['virtual_tags']
