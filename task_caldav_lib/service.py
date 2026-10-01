"""
CalDAV service — VTODO CRUD against Radicale (or any CalDAV server) with a local index.

The service keeps every VTODO resource of one calendar in memory and refreshes it
incrementally with WebDAV sync tokens. All writes are conditional (If-Match /
If-None-Match), so an edit made meanwhile in another client (e.g. iPhone Reminders)
is never silently overwritten: on a 412 the resource is re-fetched, the edit is
re-applied once, and only then does ConflictError surface.

Not thread-safe; callers serialize access (the web backend holds a lock).
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import caldav
from icalendar import Calendar, Component, Todo

from . import model, recurrence
from .model import Task, append_completion, apply_patch, parse_vtodo, set_status, to_local

log = logging.getLogger(__name__)

FULL_RELOAD_SECONDS = 600


class TaskNotFound(LookupError):
    pass


class ConflictError(RuntimeError):
    """The resource kept changing on the server while we tried to write it."""


@dataclass
class _Resource:
    href: str
    etag: Optional[str]
    ical: Calendar
    task: Task

    @property
    def component(self) -> Component:
        return _master(self.ical)


def _master(cal: Calendar) -> Component:
    """The VTODO without RECURRENCE-ID (the one this library edits)."""
    todos = [c for c in cal.walk('VTODO')]
    for c in todos:
        if 'RECURRENCE-ID' not in c:
            return c
    return todos[0]


class CalDAVService:
    """Tasks of one CalDAV calendar."""

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        calendar_name: str,
        tz: str = 'UTC',
        timeout: int = 10,
        verify_tls: bool = True,
        create_calendar: bool = False,
    ):
        self.tz: tzinfo = ZoneInfo(tz)
        self._client = caldav.DAVClient(
            url=url,
            username=username,
            password=password,
            timeout=timeout,
            ssl_verify_cert=verify_tls,
            require_tls=url.lower().startswith('https://'),
        )
        self._calendar = self._find_calendar(calendar_name, create_calendar)
        self._by_uid: dict[str, _Resource] = {}
        self._sync_token: Optional[str] = None
        self._last_full = 0.0

    def _find_calendar(self, name: str, create: bool):
        principal = self._client.principal()
        for cal in principal.calendars():
            if cal.get_display_name() == name:
                return cal
        if create:
            return principal.make_calendar(name=name, supported_calendar_component_set=['VTODO'])
        # Never fall back to some other calendar: this library writes to real data.
        raise LookupError(f"Calendar '{name}' not found on server")

    # ------------------------------------------------------------------ reading

    def refresh(self, full: bool = False) -> bool:
        """Pull changes from the server. Returns True if anything changed."""
        full = full or self._sync_token is None or time.monotonic() - self._last_full > FULL_RELOAD_SECONDS
        col = self._calendar.objects_by_sync_token(
            sync_token=None if full else self._sync_token, load_objects=True,
        )
        if full:
            seen = set()
            fresh: dict[str, _Resource] = {}
            for obj in col:
                res = self._resource_from(obj)
                if res:
                    fresh[res.task.uid] = res
                    seen.add(res.href)
            changed = set(fresh) != set(self._by_uid) or any(
                fresh[u].etag != self._by_uid[u].etag for u in fresh if u in self._by_uid
            )
            self._by_uid = fresh
            self._last_full = time.monotonic()
        else:
            changed = False
            for obj in col:
                changed = True
                href = str(obj.url)
                if obj.data is None:
                    self._drop_href(href)
                    continue
                res = self._resource_from(obj)
                if res:
                    self._drop_href(href)
                    self._by_uid[res.task.uid] = res
        self._sync_token = col.sync_token
        return changed

    def _resource_from(self, obj) -> Optional[_Resource]:
        if not obj.data or 'VTODO' not in obj.data:
            return None
        try:
            cal = Calendar.from_ical(obj.data)
            res = _Resource(str(obj.url), obj.etag, cal, None)  # type: ignore[arg-type]
            res.task = self._parse(res)
            return res
        except Exception:
            log.warning('Skipping unparseable resource %s', obj.url, exc_info=True)
            return None

    def _parse(self, res: _Resource) -> Task:
        task = parse_vtodo(res.component, self.tz)
        task.href, task.etag = res.href, res.etag
        return task

    def _drop_href(self, href: str) -> None:
        for uid, res in list(self._by_uid.items()):
            if res.href == href:
                del self._by_uid[uid]

    def tasks(self) -> list[Task]:
        return [r.task for r in self._by_uid.values()]

    def get(self, uid: str) -> Task:
        return self._res(uid).task

    def _res(self, uid: str) -> _Resource:
        try:
            return self._by_uid[uid]
        except KeyError:
            raise TaskNotFound(uid) from None

    def raw(self, uid: str) -> str:
        return self._res(uid).ical.to_ical().decode()

    # ------------------------------------------------------------------ writing

    def create(self, fields: dict) -> Task:
        uid = str(uuid.uuid4()).upper()
        now = datetime.now(timezone.utc)
        cal = Calendar()
        cal.add('PRODID', '-//task_caldav_lib//EN')
        cal.add('VERSION', '2.0')
        todo = Todo()
        todo.add('UID', uid)
        todo.add('DTSTAMP', now)
        todo.add('CREATED', now)
        todo.add('LAST-MODIFIED', now)
        todo.add('STATUS', 'NEEDS-ACTION')
        todo.add('SUMMARY', 'Untitled')
        apply_patch(todo, fields, self.tz)
        cal.add_component(todo)

        href = f'{str(self._calendar.url).rstrip("/")}/{uid}.ics'
        etag = self._put(href, cal, {'If-None-Match': '*'})
        res = _Resource(href, etag, cal, None)  # type: ignore[arg-type]
        res.task = self._parse(res)
        self._by_uid[uid] = res
        return res.task

    def update(self, uid: str, patch: dict) -> Task:
        return self._mutate(uid, lambda c: apply_patch(c, patch, self.tz))

    def delete(self, uid: str, children: str = 'orphan') -> list[str]:
        """Delete a task. children='orphan' detaches subtasks, 'delete' removes them too.

        Returns the UIDs that were deleted.
        """
        if children not in ('orphan', 'delete'):
            raise ValueError("children must be 'orphan' or 'delete'")
        deleted: list[str] = []
        for child in self.children(uid):
            if children == 'delete':
                deleted += self.delete(child.uid, 'delete')
            else:
                self.update(child.uid, {'parent_uid': None})
        res = self._res(uid)
        headers = {'If-Match': res.etag} if res.etag else {}
        r = self._client.request(res.href, 'DELETE', '', headers)
        if r.status == 412:
            raise ConflictError(f'{uid} changed on the server; refresh and retry')
        if r.status not in (200, 204, 404):
            raise RuntimeError(f'DELETE {res.href} failed: HTTP {r.status}')
        del self._by_uid[uid]
        return deleted + [uid]

    def children(self, uid: str) -> list[Task]:
        return [t for t in self.tasks() if t.parent_uid == uid]

    def descendants(self, uid: str) -> list[Task]:
        out: list[Task] = []
        for child in self.children(uid):
            out.append(child)
            out.extend(self.descendants(child.uid))
        return out

    # ------------------------------------------------------- completion / repeat

    def complete(self, uid: str, when: Optional[datetime] = None) -> Task:
        """Complete a task. Repeating tasks roll forward instead of closing."""
        when = when or datetime.now(timezone.utc)
        task = self.get(uid)
        if not task.is_recurring:
            return self._mutate(uid, lambda c: set_status(c, 'COMPLETED', when))
        rolled = self._roll_forward(uid, when, log_completion=True)
        if task.recur_after:
            for child in self.descendants(uid):
                if not child.is_open:
                    self.reopen(child.uid)
        return rolled

    def skip(self, uid: str) -> Task:
        """Advance a repeating task to its next due date without logging a completion."""
        if not self.get(uid).is_recurring:
            raise ValueError('Only repeating tasks can be skipped')
        return self._roll_forward(uid, datetime.now(timezone.utc), log_completion=False)

    def reopen(self, uid: str) -> Task:
        return self._mutate(uid, lambda c: set_status(c, 'NEEDS-ACTION'))

    def reconcile_external_completions(self) -> list[str]:
        """Roll forward after-done tasks another client (iPhone Reminders) marked COMPLETED.

        Reminders doesn't know X-TASKWEB-RECUR-AFTER, so it simply closes the task.
        Fixed-schedule (RRULE) tasks are left alone: Reminders handles those itself.
        """
        rolled = []
        for task in self.tasks():
            if task.recur_after and task.status == 'COMPLETED':
                done = model.date_from_api(task.completed_at, self.tz) if task.completed_at else None
                if not isinstance(done, datetime):
                    done = datetime.now(timezone.utc)
                try:
                    self._roll_forward(task.uid, done, log_completion=True)
                    rolled.append(task.uid)
                except Exception:
                    log.exception('Could not roll forward %s', task.uid)
        return rolled

    def _roll_forward(self, uid: str, when: datetime, log_completion: bool) -> Task:
        def mutate(c: Component) -> None:
            task = parse_vtodo(c, self.tz)
            old_due = to_local(c.get('DUE'), self.tz)
            today = datetime.now(self.tz).date()
            if task.recur_after:
                new_due = recurrence.next_after_done(
                    old_due, when.astimezone(self.tz).date(), task.recur_after, self.tz)
            else:
                new_due = recurrence.next_fixed(old_due, task.rrule, today, self.tz)
                if new_due is None:  # the schedule ended: this was the last occurrence
                    set_status(c, 'COMPLETED', when)
                    if log_completion:
                        append_completion(c, when)
                    return

            delta = _difference(old_due, new_due)
            model._set(c, 'DUE', _to_ical(new_due))
            if delta and 'DTSTART' in c:
                model._set(c, 'DTSTART', _to_ical(_shift(to_local(c.get('DTSTART'), self.tz), delta)))
            if delta:
                for alarm in c.walk('VALARM'):
                    trig = alarm.get('TRIGGER')
                    if trig is not None and isinstance(trig.dt, datetime) and not model.is_location_alarm(alarm):
                        alarm['TRIGGER'].dt = trig.dt + delta
            set_status(c, 'NEEDS-ACTION')
            if log_completion:
                append_completion(c, when)

        return self._mutate(uid, mutate)

    # ---------------------------------------------------------------- internals

    def _mutate(self, uid: str, fn: Callable[[Component], None]) -> Task:
        """Apply fn to a copy of the task's VTODO and PUT it with If-Match (one retry on 412)."""
        for attempt in (1, 2):
            res = self._res(uid)
            cal = Calendar.from_ical(res.ical.to_ical())
            c = _master(cal)
            fn(c)
            now = datetime.now(timezone.utc)
            model._set(c, 'LAST-MODIFIED', now)
            model._set(c, 'DTSTAMP', now)
            model._set(c, 'SEQUENCE', int(str(c.get('SEQUENCE', 0))) + 1)
            try:
                etag = self._put(res.href, cal, {'If-Match': res.etag} if res.etag else {})
            except ConflictError:
                if attempt == 2:
                    raise
                log.info('412 on %s; re-fetching and retrying', uid)
                self.refresh()
                continue
            new = _Resource(res.href, etag, cal, None)  # type: ignore[arg-type]
            new.task = self._parse(new)
            self._by_uid[uid] = new
            return new.task
        raise AssertionError('unreachable')

    def _put(self, href: str, cal: Calendar, headers: dict) -> Optional[str]:
        body = cal.to_ical().decode()
        r = self._client.request(href, 'PUT', body, {'Content-Type': 'text/calendar; charset=utf-8', **headers})
        if r.status == 412:
            raise ConflictError(href)
        if r.status not in (200, 201, 204):
            raise RuntimeError(f'PUT {href} failed: HTTP {r.status} {r.raw[:200] if r.raw else ""}')
        etag = r.headers.get('etag') if r.headers else None
        if not etag:  # some servers omit it; fetch so the next If-Match is valid
            head = self._client.request(href, 'HEAD')
            etag = head.headers.get('etag') if head.headers else None
        return etag


def _to_ical(val):
    if isinstance(val, datetime):
        return val.astimezone(timezone.utc)
    return val


def _difference(old, new) -> Optional[timedelta]:
    if old is None or new is None:
        return None
    if isinstance(old, datetime) != isinstance(new, datetime):
        old = old.date() if isinstance(old, datetime) else old
        new = new.date() if isinstance(new, datetime) else new
    return new - old


def _shift(val, delta: timedelta):
    if val is None:
        return None
    if isinstance(val, datetime):
        return val + delta
    return val + timedelta(days=delta.days)
