from zoneinfo import ZoneInfo

from icalendar import Calendar

from task_caldav_lib.model import apply_patch, parse_vtodo

LA = ZoneInfo('America/Los_Angeles')

# Shaped like real-world data: 2Do metadata, Apple extensions, a relative alarm.
SAMPLE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//2Do//EN
BEGIN:VTODO
UID:SAMPLE-1
SUMMARY:Change water filter
DESCRIPTION:Under the sink\\, left side
DUE;VALUE=DATE:20261001
PRIORITY:1
CATEGORIES:Home,Chores
X-2DOAPP-METADATA:<2Do Meta>%7B%22IsStarred%22%3A1%2C%22RecurrenceFrom%22%3A1%7D</2Do Meta>
X-APPLE-SORT-ORDER:123
RRULE:FREQ=DAILY;INTERVAL=90
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Reminder
TRIGGER;RELATED=END:-PT15M
END:VALARM
END:VTODO
END:VCALENDAR
"""


def _todo(text=SAMPLE):
    cal = Calendar.from_ical(text)
    return cal, next(iter(cal.walk('VTODO')))


def test_parse_sample():
    _, c = _todo()
    t = parse_vtodo(c, LA)
    assert t.title == 'Change water filter'
    assert t.notes == 'Under the sink, left side'
    assert t.due == '2026-10-01'
    assert t.priority == 1 and t.to_dict()['priority_band'] == 'high'
    assert t.tags == ['Home', 'Chores']
    assert t.starred is True                       # from 2Do metadata
    assert t.rrule == 'FREQ=DAILY;INTERVAL=90'
    assert t.alarms == [{'offset': '-PT15M', 'related': 'END'}]


def test_patch_preserves_unknown_properties():
    cal, c = _todo()
    apply_patch(c, {'title': 'Change fridge filter', 'starred': False}, LA)
    out = cal.to_ical().decode()
    assert 'X-APPLE-SORT-ORDER:123' in out
    assert 'RecurrenceFrom' in out                 # rest of 2Do blob kept
    assert '%22IsStarred%22%3A0' in out            # 2Do star kept in sync
    t = parse_vtodo(c, LA)
    assert t.title == 'Change fridge filter' and t.starred is False


def test_all_day_due_is_value_date():
    cal, c = _todo()
    apply_patch(c, {'due': '2026-12-25'}, LA)
    assert 'DUE;VALUE=DATE:20261225' in cal.to_ical().decode()
    assert parse_vtodo(c, LA).due == '2026-12-25'


def test_timed_due_round_trips_in_local_tz():
    cal, c = _todo()
    apply_patch(c, {'due': '2026-10-01T09:00:00-07:00'}, LA)
    assert 'DUE:20261001T160000Z' in cal.to_ical().decode()
    assert parse_vtodo(c, LA).due == '2026-10-01T09:00:00-07:00'


def test_naive_datetime_is_local_time():
    _, c = _todo()
    apply_patch(c, {'due': '2026-12-01T09:00'}, LA)
    assert parse_vtodo(c, LA).due == '2026-12-01T09:00:00-08:00'


def test_clear_fields():
    _, c = _todo()
    apply_patch(c, {'due': None, 'tags': [], 'rrule': None, 'alarms': []}, LA)
    t = parse_vtodo(c, LA)
    assert t.due is None and t.tags == [] and t.rrule is None and t.alarms == []


def test_location_alarm_round_trip_and_independent_of_time_alarms():
    cal, c = _todo()
    loc = {'title': 'Home', 'address': '1 Main St, Town', 'lat': 37.3318, 'lon': -122.0312,
           'radius': 150, 'proximity': 'DEPART'}
    apply_patch(c, {'location_alarm': loc}, LA)
    raw = cal.to_ical().decode().replace('\r\n ', '')
    assert 'X-APPLE-PROXIMITY:DEPART' in raw
    assert 'geo:37.3318\\,-122.0312' in raw         # escaped so Radicale keeps the longitude
    t = parse_vtodo(c, LA)
    assert t.location_alarm['lat'] == 37.3318 and t.location_alarm['title'] == 'Home'
    assert t.location_alarm['address'] == '1 Main St, Town'
    assert t.alarms == [{'offset': '-PT15M', 'related': 'END'}]   # untouched

    apply_patch(c, {'alarms': [{'at': '2026-10-01T08:00:00-07:00'}]}, LA)
    t = parse_vtodo(c, LA)
    assert t.alarms == [{'at': '2026-10-01T08:00:00-07:00'}]
    assert t.location_alarm is not None                            # untouched

    apply_patch(c, {'location_alarm': None}, LA)
    assert parse_vtodo(c, LA).location_alarm is None


def test_parent_and_url():
    _, c = _todo()
    apply_patch(c, {'parent_uid': 'P-1', 'url': 'https://example.com/a?b=c,d'}, LA)
    t = parse_vtodo(c, LA)
    assert t.parent_uid == 'P-1' and t.url == 'https://example.com/a?b=c,d'
    apply_patch(c, {'parent_uid': None}, LA)
    assert parse_vtodo(c, LA).parent_uid is None


def test_rejects_unknown_fields():
    import pytest
    _, c = _todo()
    with pytest.raises(ValueError):
        apply_patch(c, {'uid': 'nope'}, LA)
