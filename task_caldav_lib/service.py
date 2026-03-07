"""
CalDAV service layer — all CRUD operations against a Radicale (or any CalDAV) server.
Uses python-caldav + icalendar to read/write VTODOs.

No framework dependencies — credentials are passed to the constructor.
"""
import json
import uuid
from datetime import datetime, timezone, date
from typing import Optional
from urllib.parse import unquote, quote

import caldav
from caldav.elements import dav
from icalendar import Calendar, Todo, vDatetime
from dateutil.parser import parse as parse_dt


def _parse_dt(val) -> Optional[str]:
    """Convert icalendar date/datetime to ISO string or None."""
    if val is None:
        return None
    if hasattr(val, 'dt'):
        val = val.dt
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return val.isoformat()
    if isinstance(val, date):
        return val.isoformat()
    return str(val)


def _to_vtodo_dt(iso_str: str):
    """Convert ISO string to icalendar datetime."""
    dt = parse_dt(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return vDatetime(dt)


def _parse_categories(cat_prop) -> list[str]:
    """Parse CATEGORIES property into list of strings."""
    if cat_prop is None:
        return []
    if hasattr(cat_prop, 'cats'):
        return [str(c) for c in cat_prop.cats]
    raw = str(cat_prop)
    if not raw:
        return []
    return [c.strip() for c in raw.split(',') if c.strip()]


def _parse_2do_metadata(raw) -> dict:
    """Parse 2Do's URL-encoded JSON metadata blob, e.g. X-2DOAPP-METADATA."""
    if raw is None:
        return {}
    try:
        s = str(raw)
        start = s.find('>') + 1
        end = s.rfind('<')
        if start > 0 and end > start:
            s = s[start:end]
        s = unquote(s.strip())
        return json.loads(s)
    except Exception:
        return {}


def _vtodo_to_dict(todo_component) -> dict:
    """Parse a VTODO icalendar component into a Python dict."""
    c = todo_component

    def get(key, default=None):
        val = c.get(key)
        if val is None:
            return default
        return val

    uid = str(get('UID', ''))
    summary = str(get('SUMMARY', ''))
    description = str(get('DESCRIPTION', '')) if get('DESCRIPTION') else ''
    status = str(get('STATUS', 'NEEDS-ACTION')).upper()

    priority_raw = get('PRIORITY')
    priority_int = int(str(priority_raw)) if priority_raw is not None else 0

    due = _parse_dt(get('DUE'))
    start = _parse_dt(get('DTSTART'))
    completed_at = _parse_dt(get('COMPLETED'))

    percent_raw = get('PERCENT-COMPLETE')
    percent = int(str(percent_raw)) if percent_raw is not None else 0

    categories = _parse_categories(get('CATEGORIES'))
    location = str(get('LOCATION', '')) if get('LOCATION') else ''

    parent_uid = None
    related = c.get('RELATED-TO')
    if related is not None:
        params = related.params if hasattr(related, 'params') else {}
        reltype = params.get('RELTYPE', 'PARENT').upper() if params else 'PARENT'
        if reltype == 'PARENT':
            parent_uid = str(related)

    rrule = None
    rrule_raw = get('RRULE')
    if rrule_raw is not None:
        rrule = rrule_raw.to_ical().decode('utf-8') if hasattr(rrule_raw, 'to_ical') else str(rrule_raw)

    starred_raw = get('X-STARRED', 'FALSE')
    starred = str(starred_raw).upper() == 'TRUE'
    if not starred:
        meta = _parse_2do_metadata(get('X-2DOAPP-METADATA'))
        starred = bool(meta.get('IsStarred', 0))

    created = _parse_dt(get('CREATED'))
    last_modified = _parse_dt(get('LAST-MODIFIED'))

    return {
        'uid': uid,
        'title': summary,
        'description': description,
        'status': status,
        'priority': priority_int,
        'due': due,
        'start': start,
        'completed_at': completed_at,
        'percent': percent,
        'tags': categories,
        'location': location,
        'parent_uid': parent_uid,
        'recurrence': rrule,
        'starred': starred,
        'created': created,
        'last_modified': last_modified,
    }


class CalDAVService:
    """CalDAV CRUD operations for VTODO tasks."""

    def __init__(self, url: str, username: str, password: str, calendar_name: str, timeout: int = 5):
        self._calendar_name = calendar_name
        self._client = caldav.DAVClient(
            url=url,
            username=username,
            password=password,
            timeout=timeout,
        )
        self._principal = self._client.principal()
        self._calendar = self._get_calendar()

    def _get_calendar(self):
        calendars = self._principal.calendars()
        for cal in calendars:
            props = cal.get_properties([dav.DisplayName()])
            name = props.get('{DAV:}displayname', '')
            if name == self._calendar_name:
                return cal
        if calendars:
            return calendars[0]
        raise RuntimeError(f"Calendar '{self._calendar_name}' not found on server")

    def _parse_todo(self, todo) -> list[dict]:
        cal = Calendar.from_ical(todo.data)
        return [_vtodo_to_dict(c) for c in cal.walk('VTODO')]

    def get_all_tasks(self, include_completed: bool = True) -> list[dict]:
        todos = self._calendar.todos(include_completed=include_completed)
        results = []
        for todo in todos:
            try:
                results.extend(self._parse_todo(todo))
            except Exception as e:
                print(f"Warning: skipping malformed VTODO: {e}")
        return results

    def get_task(self, uid: str) -> Optional[dict]:
        todos = self._calendar.todos(include_completed=True)
        for todo in todos:
            try:
                for task in self._parse_todo(todo):
                    if task['uid'] == uid:
                        return task
            except Exception:
                continue
        return None

    def create_task(self, data: dict) -> dict:
        uid = str(uuid.uuid4())
        now = datetime.now(tz=timezone.utc)

        cal = Calendar()
        cal.add('prodid', '-//task_caldav_lib//EN')
        cal.add('version', '2.0')

        todo = Todo()
        todo.add('uid', uid)
        todo.add('summary', data.get('title', 'Untitled'))
        todo.add('dtstamp', now)
        todo.add('created', now)
        todo.add('last-modified', now)
        todo.add('status', data.get('status', 'NEEDS-ACTION'))

        if data.get('description'):
            todo.add('description', data['description'])

        priority = data.get('priority', 0)
        if priority is not None:
            todo.add('priority', int(priority))

        if data.get('due'):
            todo.add('due', _to_vtodo_dt(data['due']))

        if data.get('start'):
            todo.add('dtstart', _to_vtodo_dt(data['start']))

        if data.get('tags'):
            todo.add('categories', data['tags'])

        if data.get('location'):
            todo.add('location', data['location'])

        if data.get('parent_uid'):
            from icalendar import vText as vT
            rel = vT(data['parent_uid'])
            rel.params['RELTYPE'] = 'PARENT'
            todo.add('related-to', rel)

        if data.get('recurrence'):
            from icalendar import vRecur
            todo.add('rrule', vRecur.from_ical(data['recurrence']))

        todo.add('x-starred', 'TRUE' if data.get('starred') else 'FALSE')

        if data.get('percent'):
            todo.add('percent-complete', int(data['percent']))

        cal.add_component(todo)
        self._calendar.add_todo(cal.to_ical().decode('utf-8'))

        return self.get_task(uid) or {'uid': uid, **data}

    def update_task(self, uid: str, data: dict) -> Optional[dict]:
        todos = self._calendar.todos(include_completed=True)
        for todo in todos:
            try:
                cal = Calendar.from_ical(todo.data)
                for component in cal.walk('VTODO'):
                    if str(component.get('UID', '')) == uid:
                        self._update_component(component, data)
                        component['last-modified'] = vDatetime(datetime.now(tz=timezone.utc))
                        todo.data = cal.to_ical().decode('utf-8')
                        todo.save()
                        return _vtodo_to_dict(component)
            except Exception as e:
                print(f"Warning: error updating task {uid}: {e}")
                continue
        return None

    def _update_component(self, component, data: dict):
        def set_field(key, value):
            if key in component:
                del component[key]
            if value is not None:
                component.add(key, value)

        if 'title' in data:
            set_field('summary', data['title'])
        if 'description' in data:
            set_field('description', data['description'])
        if 'status' in data:
            set_field('status', data['status'])
        if 'priority' in data and data['priority'] is not None:
            set_field('priority', int(data['priority']))
        if 'due' in data:
            set_field('due', _to_vtodo_dt(data['due']) if data['due'] else None)
        if 'start' in data:
            set_field('dtstart', _to_vtodo_dt(data['start']) if data['start'] else None)
        if 'tags' in data:
            set_field('categories', data['tags'])
        if 'location' in data:
            set_field('location', data['location'])
        if 'starred' in data:
            set_field('x-starred', 'TRUE' if data['starred'] else 'FALSE')
            meta_raw = component.get('X-2DOAPP-METADATA')
            if meta_raw is not None:
                meta = _parse_2do_metadata(meta_raw)
                meta['IsStarred'] = 1 if data['starred'] else 0
                encoded = quote(json.dumps(meta, separators=(',', ':')))
                new_meta = f'<2Do Meta>{encoded}</2Do Meta>'
                if 'X-2DOAPP-METADATA' in component:
                    del component['X-2DOAPP-METADATA']
                component.add('X-2DOAPP-METADATA', new_meta)
        if 'percent' in data:
            set_field('percent-complete', int(data['percent']) if data['percent'] is not None else None)
        if 'recurrence' in data:
            if 'rrule' in component:
                del component['rrule']
            if data['recurrence']:
                from icalendar import vRecur
                component.add('rrule', vRecur.from_ical(data['recurrence']))
        if 'parent_uid' in data:
            if 'related-to' in component:
                del component['related-to']
            if data['parent_uid']:
                from icalendar import vText as vT
                rel = vT(data['parent_uid'])
                rel.params['RELTYPE'] = 'PARENT'
                component.add('related-to', rel)
        if 'completed_at' in data and data['completed_at']:
            set_field('completed', _to_vtodo_dt(data['completed_at']))

    def delete_task(self, uid: str) -> bool:
        todos = self._calendar.todos(include_completed=True)
        for todo in todos:
            try:
                cal = Calendar.from_ical(todo.data)
                for component in cal.walk('VTODO'):
                    if str(component.get('UID', '')) == uid:
                        todo.delete()
                        return True
            except Exception:
                continue
        return False

    def complete_task(self, uid: str) -> Optional[dict]:
        """Mark a task as COMPLETED. If it has a recurrence, spawn the next occurrence."""
        task = self.get_task(uid)
        if task is None:
            return None

        now = datetime.now(tz=timezone.utc)
        completed = self.update_task(uid, {
            'status': 'COMPLETED',
            'completed_at': now.isoformat(),
            'percent': 100,
        })

        if task.get('recurrence'):
            self._spawn_next_recurrence(task, now)

        return completed

    def _spawn_next_recurrence(self, task: dict, completed_at: datetime):
        """Create the next occurrence of a recurring task after completion."""
        from dateutil.rrule import rrulestr

        rrule_str = task['recurrence']
        due_str = task.get('due')

        if due_str:
            dtstart = parse_dt(due_str)
            if dtstart.tzinfo is None:
                dtstart = dtstart.replace(tzinfo=timezone.utc)
        else:
            dtstart = completed_at

        try:
            rule = rrulestr(rrule_str, dtstart=dtstart, ignoretz=False)
            after = max(dtstart, completed_at)
            next_dt = rule.after(after, inc=False)
        except Exception as e:
            print(f"Warning: could not compute next recurrence from '{rrule_str}': {e}")
            next_dt = None

        new_data = {
            'title': task['title'],
            'description': task.get('description') or '',
            'status': 'NEEDS-ACTION',
            'priority': task.get('priority', 0),
            'tags': task.get('tags') or [],
            'location': task.get('location') or '',
            'parent_uid': task.get('parent_uid'),
            'recurrence': task['recurrence'],
            'starred': task.get('starred', False),
        }

        if next_dt:
            new_data['due'] = next_dt.isoformat()
            if due_str and task.get('start'):
                try:
                    old_due = parse_dt(due_str)
                    old_start = parse_dt(task['start'])
                    if old_due.tzinfo is None:
                        old_due = old_due.replace(tzinfo=timezone.utc)
                    if old_start.tzinfo is None:
                        old_start = old_start.replace(tzinfo=timezone.utc)
                    offset = old_due - old_start
                    new_data['start'] = (next_dt - offset).isoformat()
                except Exception:
                    pass
        elif due_str:
            new_data['due'] = due_str
            if task.get('start'):
                new_data['start'] = task['start']

        self.create_task(new_data)
