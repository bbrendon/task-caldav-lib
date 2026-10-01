"""
Task model — parse VTODO components into Task objects and apply edits back.

Edits are always applied to the original component, so properties this library
doesn't know about (2Do metadata, Apple extensions, ...) survive round trips.

Date values crossing the API boundary are strings:
  - 'YYYY-MM-DD' for all-day values (written as VALUE=DATE)
  - ISO 8601 datetimes with offset, expressed in the service's time zone
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Optional
from urllib.parse import quote, unquote

from dateutil.parser import isoparse
from icalendar import Alarm, Component, vRecur, vText, vUri

from .alarms import is_location_alarm, location_alarm_from_dict, location_alarm_to_dict

log = logging.getLogger(__name__)

STATUS_OPEN = ('NEEDS-ACTION', 'IN-PROCESS')
STATUS_DONE = ('COMPLETED', 'CANCELLED')

PRIORITY_BY_BAND = {'none': 0, 'high': 1, 'medium': 5, 'low': 9}

X_RECUR_AFTER = 'X-TASKWEB-RECUR-AFTER'
X_COMPLETIONS = 'X-TASKWEB-COMPLETIONS'
X_STARRED = 'X-STARRED'
X_2DO_META = 'X-2DOAPP-METADATA'


def priority_band(priority: int) -> str:
    """Map a 0-9 CalDAV priority to none/high/medium/low (Reminders' bands)."""
    if not priority:
        return 'none'
    if priority <= 4:
        return 'high'
    if priority == 5:
        return 'medium'
    return 'low'


# ---------------------------------------------------------------------------
# Date conversion
# ---------------------------------------------------------------------------

def to_local(val, tz: tzinfo):
    """Normalize an icalendar date/datetime: dates stay dates, datetimes become tz-aware in `tz`.

    Floating (naive) datetimes are interpreted as local time.
    """
    if val is None:
        return None
    if hasattr(val, 'dt'):
        val = val.dt
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=tz)
        return val.astimezone(tz)
    if isinstance(val, date):
        return val
    return None


def date_to_api(val, tz: tzinfo) -> Optional[str]:
    val = to_local(val, tz)
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat(timespec='seconds')
    return val.isoformat()


def date_from_api(s: Optional[str], tz: tzinfo):
    """'YYYY-MM-DD' -> date; ISO datetime -> UTC datetime (naive input is local time)."""
    if not s:
        return None
    if len(s) == 10:
        return date.fromisoformat(s)
    dt = isoparse(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec='seconds')


# ---------------------------------------------------------------------------
# Small property helpers
# ---------------------------------------------------------------------------

def _as_list(val) -> list:
    if val is None:
        return []
    return val if isinstance(val, list) else [val]


def _text(c: Component, key: str) -> str:
    val = c.get(key)
    if val is None:
        return ''
    if isinstance(val, list):
        val = val[0]
    return str(val)


def _parse_categories(c: Component) -> list[str]:
    tags: list[str] = []
    for prop in _as_list(c.get('CATEGORIES')):
        if hasattr(prop, 'cats'):
            tags.extend(str(x).strip() for x in prop.cats)
        else:
            tags.extend(x.strip() for x in str(prop).split(','))
    return [t for t in tags if t]  # iOS sometimes writes an empty 'CATEGORIES:'


def _parse_parent(c: Component) -> Optional[str]:
    for rel in _as_list(c.get('RELATED-TO')):
        reltype = str(getattr(rel, 'params', {}).get('RELTYPE', 'PARENT')).upper()
        if reltype == 'PARENT':
            return str(rel)
    return None


def _parse_2do_metadata(raw) -> dict:
    """Parse 2Do's URL-encoded JSON blob, e.g. '<2Do Meta>%7B...%7D</2Do Meta>'."""
    if raw is None:
        return {}
    try:
        s = str(raw)
        start = s.find('>') + 1
        end = s.rfind('<')
        if start > 0 and end > start:
            s = s[start:end]
        return json.loads(unquote(s.strip()))
    except Exception:
        return {}


def _set(c: Component, key: str, value) -> None:
    """Replace every instance of `key` with `value` (None/''/[] removes it)."""
    while key in c:
        del c[key]
    if value is not None and value != '' and value != []:
        c.add(key, value)


def _remove_alarms(c: Component, location: bool) -> None:
    c.subcomponents = [
        s for s in c.subcomponents
        if not (s.name == 'VALARM' and is_location_alarm(s) == location)
    ]


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------

@dataclass
class Task:
    uid: str
    title: str = ''
    notes: str = ''
    status: str = 'NEEDS-ACTION'
    priority: int = 0
    due: Optional[str] = None
    start: Optional[str] = None
    completed_at: Optional[str] = None
    percent: int = 0
    tags: list[str] = field(default_factory=list)
    location: str = ''
    url: str = ''
    parent_uid: Optional[str] = None
    starred: bool = False
    recur_after: Optional[str] = None
    rrule: Optional[str] = None
    completions: list[str] = field(default_factory=list)
    alarms: list[dict] = field(default_factory=list)
    location_alarm: Optional[dict] = None
    created: Optional[str] = None
    last_modified: Optional[str] = None
    href: Optional[str] = None
    etag: Optional[str] = None

    @property
    def is_open(self) -> bool:
        return self.status not in STATUS_DONE

    @property
    def is_recurring(self) -> bool:
        return bool(self.recur_after or self.rrule)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d['priority_band'] = priority_band(self.priority)
        return d


# Fields a client may change through apply_patch().
EDITABLE_FIELDS = frozenset({
    'title', 'notes', 'status', 'priority', 'due', 'start', 'tags', 'location', 'url',
    'parent_uid', 'starred', 'recur_after', 'rrule', 'alarms', 'location_alarm', 'percent',
})


def parse_vtodo(c: Component, tz: tzinfo) -> Task:
    """Parse a VTODO component into a Task."""
    starred = str(c.get(X_STARRED, 'FALSE')).upper() == 'TRUE'
    if not starred:
        starred = bool(_parse_2do_metadata(c.get(X_2DO_META)).get('IsStarred', 0))

    rrule = c.get('RRULE')
    if isinstance(rrule, list):
        rrule = rrule[0]

    completions_raw = _text(c, X_COMPLETIONS)
    completions = [x.strip() for x in completions_raw.split(',') if x.strip()]

    alarms: list[dict] = []
    location_alarm = None
    for sub in c.subcomponents:
        if sub.name != 'VALARM':
            continue
        if is_location_alarm(sub):
            location_alarm = location_alarm_to_dict(sub)
            continue
        trigger = sub.get('TRIGGER')
        if trigger is None:
            continue
        if isinstance(trigger.dt, timedelta):
            related = str(trigger.params.get('RELATED', 'START')).upper()
            alarms.append({'offset': trigger.to_ical().decode(), 'related': related})
        else:
            alarms.append({'at': date_to_api(trigger.dt, tz)})

    try:
        priority = int(str(c.get('PRIORITY', 0)))
    except ValueError:
        priority = 0
    try:
        percent = int(str(c.get('PERCENT-COMPLETE', 0)))
    except ValueError:
        percent = 0

    return Task(
        uid=_text(c, 'UID'),
        title=_text(c, 'SUMMARY'),
        notes=_text(c, 'DESCRIPTION'),
        status=_text(c, 'STATUS').upper() or 'NEEDS-ACTION',
        priority=priority,
        due=date_to_api(c.get('DUE'), tz),
        start=date_to_api(c.get('DTSTART'), tz),
        completed_at=date_to_api(c.get('COMPLETED'), tz),
        percent=percent,
        tags=_parse_categories(c),
        location=_text(c, 'LOCATION'),
        url=_text(c, 'URL'),
        parent_uid=_parse_parent(c),
        starred=starred,
        recur_after=_text(c, X_RECUR_AFTER) or None,
        rrule=rrule.to_ical().decode() if rrule is not None else None,
        completions=completions,
        alarms=alarms,
        location_alarm=location_alarm,
        created=date_to_api(c.get('CREATED'), tz),
        last_modified=date_to_api(c.get('LAST-MODIFIED'), tz),
    )


def apply_patch(c: Component, patch: dict, tz: tzinfo) -> None:
    """Apply a partial edit (Task field names -> new values) to a VTODO in place."""
    unknown = set(patch) - EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"Not editable: {', '.join(sorted(unknown))}")

    if 'title' in patch:
        _set(c, 'SUMMARY', patch['title'] or 'Untitled')
    if 'notes' in patch:
        _set(c, 'DESCRIPTION', patch['notes'])
    if 'location' in patch:
        _set(c, 'LOCATION', patch['location'])
    if 'url' in patch:
        _set(c, 'URL', vUri(patch['url']) if patch['url'] else None)
    if 'priority' in patch:
        _set(c, 'PRIORITY', int(patch['priority'] or 0) or None)
    if 'percent' in patch:
        _set(c, 'PERCENT-COMPLETE', int(patch['percent'] or 0) or None)
    if 'due' in patch:
        _set(c, 'DUE', date_from_api(patch['due'], tz))
    if 'start' in patch:
        _set(c, 'DTSTART', date_from_api(patch['start'], tz))
    if 'tags' in patch:
        _set(c, 'CATEGORIES', list(patch['tags'] or []))
    if 'recur_after' in patch:
        _set(c, X_RECUR_AFTER, patch['recur_after'])
    if 'rrule' in patch:
        _set(c, 'RRULE', vRecur.from_ical(patch['rrule']) if patch['rrule'] else None)
    if 'parent_uid' in patch:
        _set(c, 'RELATED-TO', None)
        if patch['parent_uid']:
            rel = vText(patch['parent_uid'])
            rel.params['RELTYPE'] = 'PARENT'
            c.add('RELATED-TO', rel)
    if 'starred' in patch:
        set_starred(c, bool(patch['starred']))
    if 'status' in patch:
        set_status(c, patch['status'])
    if 'alarms' in patch:
        _remove_alarms(c, location=False)
        for spec in patch['alarms'] or []:
            c.add_component(_time_alarm(spec, c, tz))
    if 'location_alarm' in patch:
        _remove_alarms(c, location=True)
        if patch['location_alarm']:
            c.add_component(location_alarm_from_dict(patch['location_alarm']))


def set_starred(c: Component, starred: bool) -> None:
    _set(c, X_STARRED, 'TRUE' if starred else 'FALSE')
    if X_2DO_META in c:
        meta = _parse_2do_metadata(c.get(X_2DO_META))
        meta['IsStarred'] = 1 if starred else 0
        encoded = quote(json.dumps(meta, separators=(',', ':')))
        _set(c, X_2DO_META, f'<2Do Meta>{encoded}</2Do Meta>')


def set_status(c: Component, status: str, when: Optional[datetime] = None) -> None:
    status = status.upper()
    _set(c, 'STATUS', status)
    if status == 'COMPLETED':
        _set(c, 'COMPLETED', (when or datetime.now(timezone.utc)).astimezone(timezone.utc))
        _set(c, 'PERCENT-COMPLETE', 100)
    else:
        _set(c, 'COMPLETED', None)
        if status != 'IN-PROCESS':
            _set(c, 'PERCENT-COMPLETE', None)


def append_completion(c: Component, when: datetime) -> None:
    history = [x.strip() for x in _text(c, X_COMPLETIONS).split(',') if x.strip()]
    history.append(_utc_iso(when))
    _set(c, X_COMPLETIONS, ','.join(history))


def _time_alarm(spec: dict, c: Component, tz: tzinfo) -> Alarm:
    """Build a display alarm from {'at': iso} or {'offset': '-PT15M', 'related': 'END'}."""
    alarm_uid = str(uuid.uuid4()).upper()
    a = Alarm()
    a.add('ACTION', 'DISPLAY')
    a.add('DESCRIPTION', _text(c, 'SUMMARY') or 'Reminder')
    a.add('UID', alarm_uid)
    a.add('X-WR-ALARMUID', alarm_uid)
    if spec.get('at'):
        at = date_from_api(spec['at'], tz)
        if not isinstance(at, datetime):
            at = datetime.combine(at, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
        a.add('TRIGGER', at)
    elif spec.get('offset'):
        from icalendar import vDuration
        trigger = vDuration(vDuration.from_ical(spec['offset']))
        trigger.params['RELATED'] = (spec.get('related') or 'END').upper()
        a.add('TRIGGER', trigger)
    else:
        raise ValueError("alarm needs 'at' or 'offset'")
    return a
