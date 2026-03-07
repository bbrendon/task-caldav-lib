"""
Virtual tags — computed at runtime from task fields.
No database, no storage — pure derived state.
"""
from datetime import datetime, timezone, date, timedelta
from dateutil.parser import parse as parse_dt
from typing import Optional


def _parse_dt_field(val: Optional[str]) -> Optional[datetime]:
    if not val:
        return None
    try:
        dt = parse_dt(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _due_date(task: dict) -> Optional[date]:
    dt = _parse_dt_field(task.get('due'))
    return dt.date() if dt else None


def compute_virtual_tags(task: dict, parent_uids: set[str]) -> list[str]:
    """
    Compute virtual tag strings for a single task.

    Args:
        task: Task dict from CalDAVService
        parent_uids: Set of UIDs that are referenced as parents by other tasks
    """
    tags = []
    now = datetime.now(tz=timezone.utc)
    today = now.date()

    status = task.get('status', 'NEEDS-ACTION').upper()
    due = _parse_dt_field(task.get('due'))
    priority = task.get('priority', 0) or 0
    recurrence = task.get('recurrence')
    uid = task.get('uid', '')
    starred = task.get('starred', False)
    categories = task.get('tags', []) or []
    parent_uid = task.get('parent_uid')

    # OVERDUE
    if due and status not in ('COMPLETED', 'CANCELLED') and due < now:
        tags.append('OVERDUE')

    # DUE_TODAY
    if due and due.date() == today and status not in ('COMPLETED', 'CANCELLED'):
        tags.append('DUE_TODAY')

    # DUE_WEEK
    if due and today < due.date() <= today + timedelta(days=7) and status not in ('COMPLETED', 'CANCELLED'):
        tags.append('DUE_WEEK')

    # RECURRING / SINGLE
    if recurrence:
        tags.append('RECURRING')
    else:
        tags.append('SINGLE')

    # PARENT / CHILD
    if uid in parent_uids:
        tags.append('PARENT')
    if parent_uid:
        tags.append('CHILD')

    # STARRED
    if starred:
        tags.append('STARRED')

    # PENDING
    if status in ('NEEDS-ACTION', 'IN-PROCESS'):
        tags.append('PENDING')

    # Priority bands
    if 1 <= priority <= 4:
        tags.append('HIGH')
    elif priority == 5:
        tags.append('MEDIUM')
    elif 6 <= priority <= 9:
        tags.append('LOW')

    # TAGGED / UNTAGGED
    if categories:
        tags.append('TAGGED')
    else:
        tags.append('UNTAGGED')

    return tags


def compute_parent_uids(all_tasks: list[dict]) -> set[str]:
    """Return set of UIDs that have at least one child pointing to them."""
    return {t['parent_uid'] for t in all_tasks if t.get('parent_uid')}


def add_virtual_tags(all_tasks: list[dict]) -> list[dict]:
    """Mutate each task dict to add a 'virtual_tags' key."""
    parent_uids = compute_parent_uids(all_tasks)
    for task in all_tasks:
        task['virtual_tags'] = compute_virtual_tags(task, parent_uids)
    return all_tasks


def get_virtual_tag_counts(all_tasks: list[dict]) -> dict[str, int]:
    """Return counts of each virtual tag across all tasks."""
    counts: dict[str, int] = {}
    parent_uids = compute_parent_uids(all_tasks)
    for task in all_tasks:
        for vt in compute_virtual_tags(task, parent_uids):
            counts[vt] = counts.get(vt, 0) + 1
    return counts
