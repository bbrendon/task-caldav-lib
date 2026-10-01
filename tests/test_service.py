from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from task_caldav_lib import ConflictError, TaskNotFound
from task_caldav_lib.service import CalDAVService

LA = ZoneInfo('America/Los_Angeles')


def today():
    return datetime.now(LA).date()


def test_create_get_update(svc):
    t = svc.create({'title': 'Buy milk', 'due': '2026-10-01', 'tags': ['Home'], 'priority': 5})
    assert t.etag and t.due == '2026-10-01'
    assert svc.get(t.uid).title == 'Buy milk'
    u = svc.update(t.uid, {'title': 'Buy oat milk', 'starred': True})
    assert u.title == 'Buy oat milk' and u.starred and u.etag != t.etag
    assert 'SEQUENCE:1' in svc.raw(t.uid)


def test_index_survives_fresh_full_reload(svc, radicale_url):
    t = svc.create({'title': 'Persisted', 'due': '2026-10-01'})
    fresh = CalDAVService(radicale_url, 'test', 'x', svc._calendar.get_display_name(), tz='America/Los_Angeles')
    fresh.refresh()
    assert fresh.get(t.uid).due == '2026-10-01'


def test_missing_calendar_is_an_error_not_a_fallback(radicale_url, svc):
    with pytest.raises(LookupError):
        CalDAVService(radicale_url, 'test', 'x', 'Does-Not-Exist')


def test_incremental_refresh_sees_external_changes(svc, other_client):
    a = svc.create({'title': 'A'})
    b = svc.create({'title': 'B'})
    other_client.request(svc._res(b.uid).href, 'DELETE')
    raw = svc.raw(a.uid).replace('SUMMARY:A', 'SUMMARY:A edited on phone')
    other_client.request(svc._res(a.uid).href, 'PUT', raw, {'Content-Type': 'text/calendar'})
    assert svc.refresh() is True
    assert svc.get(a.uid).title == 'A edited on phone'
    with pytest.raises(TaskNotFound):
        svc.get(b.uid)
    assert svc.refresh() is False


def test_conflicting_edit_is_merged_not_clobbered(svc, other_client):
    t = svc.create({'title': 'Original', 'notes': ''})
    # Another client edits the notes; our index is now stale.
    raw = svc.raw(t.uid).replace('SUMMARY:Original', 'SUMMARY:Original\r\nDESCRIPTION:from phone')
    other_client.request(svc._res(t.uid).href, 'PUT', raw, {'Content-Type': 'text/calendar'})
    u = svc.update(t.uid, {'title': 'Renamed'})      # 412 -> refresh -> re-apply
    assert u.title == 'Renamed' and u.notes == 'from phone'


def test_complete_and_reopen_single(svc):
    t = svc.create({'title': 'One-off'})
    done = svc.complete(t.uid)
    assert done.status == 'COMPLETED' and done.completed_at and done.percent == 100
    again = svc.reopen(t.uid)
    assert again.status == 'NEEDS-ACTION' and again.completed_at is None


def test_complete_after_done_rolls_forward(svc):
    t = svc.create({'title': 'Water plants', 'due': '2026-01-01', 'start': '2025-12-30',
                    'recur_after': 'P30D', 'alarms': [{'at': '2026-01-01T08:00:00-08:00'}]})
    r = svc.complete(t.uid)
    assert r.status == 'NEEDS-ACTION' and r.completed_at is None
    assert r.due == (today() + timedelta(days=30)).isoformat()
    assert r.start == (today() + timedelta(days=28)).isoformat()       # offset kept
    assert r.alarms[0]['at'].startswith((today() + timedelta(days=30)).isoformat())
    assert len(r.completions) == 1
    assert svc.complete(t.uid).completions.__len__() == 2


def test_after_done_reopens_completed_subtasks(svc):
    p = svc.create({'title': 'Clean car', 'recur_after': 'P2W', 'due': today().isoformat()})
    c1 = svc.create({'title': 'Vacuum', 'parent_uid': p.uid})
    svc.complete(c1.uid)
    svc.complete(p.uid)
    assert svc.get(c1.uid).status == 'NEEDS-ACTION'


def test_fixed_schedule_rolls_to_next_occurrence(svc):
    t = svc.create({'title': 'Pay rent', 'due': '2026-01-01', 'rrule': 'FREQ=MONTHLY;BYMONTHDAY=1'})
    r = svc.complete(t.uid)
    nxt = date.fromisoformat(r.due)
    assert nxt.day == 1 and nxt >= today() and r.rrule == 'FREQ=MONTHLY;BYMONTHDAY=1'


def test_fixed_schedule_last_occurrence_completes(svc):
    t = svc.create({'title': 'Twice', 'due': today().isoformat(), 'rrule': 'FREQ=DAILY;COUNT=1'})
    assert svc.complete(t.uid).status == 'COMPLETED'


def test_skip(svc):
    t = svc.create({'title': 'Skip me', 'due': today().isoformat(), 'recur_after': 'P7D'})
    r = svc.skip(t.uid)
    assert r.due == (today() + timedelta(days=7)).isoformat() and r.completions == []
    with pytest.raises(ValueError):
        svc.skip(svc.create({'title': 'plain'}).uid)


def test_reconcile_completion_made_in_reminders(svc, other_client):
    t = svc.create({'title': 'Filter', 'due': '2026-01-01', 'recur_after': 'P10D'})
    done_at = datetime.now(timezone.utc) - timedelta(days=2)
    stamp = done_at.strftime('%Y%m%dT%H%M%SZ')
    raw = svc.raw(t.uid).replace('STATUS:NEEDS-ACTION', f'STATUS:COMPLETED\r\nCOMPLETED:{stamp}')
    other_client.request(svc._res(t.uid).href, 'PUT', raw, {'Content-Type': 'text/calendar'})
    svc.refresh()
    assert svc.reconcile_external_completions() == [t.uid]
    r = svc.get(t.uid)
    expected = done_at.astimezone(LA).date() + timedelta(days=10)
    assert r.status == 'NEEDS-ACTION' and r.due == expected.isoformat() and len(r.completions) == 1
    assert svc.reconcile_external_completions() == []


def test_delete_orphan_and_cascade(svc):
    p = svc.create({'title': 'Parent'})
    c = svc.create({'title': 'Child', 'parent_uid': p.uid})
    g = svc.create({'title': 'Grandchild', 'parent_uid': c.uid})
    assert [t.uid for t in svc.descendants(p.uid)] == [c.uid, g.uid]
    assert svc.delete(p.uid) == [p.uid]
    assert svc.get(c.uid).parent_uid is None
    assert set(svc.delete(c.uid, children='delete')) == {c.uid, g.uid}
    assert svc.tasks() == []


def test_delete_stale_raises_conflict(svc, other_client):
    t = svc.create({'title': 'x'})
    raw = svc.raw(t.uid).replace('SUMMARY:x', 'SUMMARY:y')
    other_client.request(svc._res(t.uid).href, 'PUT', raw, {'Content-Type': 'text/calendar'})
    with pytest.raises(ConflictError):
        svc.delete(t.uid)


def test_location_alarm_survives_radicale_storage(svc, radicale_url):
    loc = {'title': 'Home', 'lat': 37.4, 'lon': -122.1, 'radius': 150, 'proximity': 'ARRIVE'}
    t = svc.create({'title': 'Geo', 'location_alarm': loc})
    fresh = CalDAVService(radicale_url, 'test', 'x', svc._calendar.get_display_name(), tz='America/Los_Angeles')
    fresh.refresh()
    la = fresh.get(t.uid).location_alarm
    assert (la['lat'], la['lon']) == (37.4, -122.1)


def test_reads_lat_only_geo_damaged_by_radicale(svc, other_client):
    t = svc.create({'title': 'Old'})
    raw = svc.raw(t.uid).replace('END:VTODO', 'BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER;VALUE=DATE-TIME:19760401T005545Z\r\n'
        'X-APPLE-PROXIMITY:ARRIVE\r\nX-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-TITLE=Home:geo:37.4\r\nEND:VALARM\r\nEND:VTODO')
    other_client.request(svc._res(t.uid).href, 'PUT', raw, {'Content-Type': 'text/calendar'})
    svc.refresh()
    la = svc.get(t.uid).location_alarm
    assert la['title'] == 'Home' and la['lat'] == 37.4 and la['lon'] is None
