"""
Apple location (geofence) alarms on VTODOs.

iOS Reminders stores "remind me when I arrive/leave" as a VALARM carrying
X-APPLE-PROXIMITY and X-APPLE-STRUCTURED-LOCATION (a geo: URI with radius/title
parameters) and a fixed placeholder trigger. The layout below matches alarms
iOS 26 wrote to the user's calendar (ACTION:DISPLAY, the 1976 placeholder TRIGGER,
UID + X-WR-ALARMUID). Apple writes X-APPLE-RADIUS=0 for "default radius".

Radicale (via vobject) treats an unescaped comma in this value as a list separator
and silently stores only 'geo:<lat>', dropping the longitude. So we write the comma
escaped ('geo:<lat>\\,<lon>'), which Radicale keeps, and read either form.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from icalendar import Alarm, Component, vUri

APPLE_PLACEHOLDER_TRIGGER = datetime(1976, 4, 1, 0, 55, 45, tzinfo=timezone.utc)
PROXIMITIES = ('ARRIVE', 'DEPART')


def is_location_alarm(alarm: Component) -> bool:
    return 'X-APPLE-PROXIMITY' in alarm or 'X-APPLE-STRUCTURED-LOCATION' in alarm


def location_alarm_to_dict(alarm: Component) -> Optional[dict]:
    loc = alarm.get('X-APPLE-STRUCTURED-LOCATION')
    lat = lon = None
    title = address = ''
    radius = None
    if loc is not None:
        params = getattr(loc, 'params', {})
        title = str(params.get('X-TITLE', ''))
        address = str(params.get('X-ADDRESS', ''))
        try:
            radius = float(params.get('X-APPLE-RADIUS')) if params.get('X-APPLE-RADIUS') else None
        except ValueError:
            radius = None
        raw = str(loc).replace('\\,', ',')
        if raw.lower().startswith('geo:'):
            coords = raw[4:].split(';')[0].split(',')
            try:
                lat = float(coords[0])
                lon = float(coords[1]) if len(coords) > 1 else None  # None: truncated by Radicale
            except ValueError:
                lat = lon = None
    return {
        'title': title,
        'address': address,
        'lat': lat,
        'lon': lon,
        'radius': radius,
        'proximity': str(alarm.get('X-APPLE-PROXIMITY', 'ARRIVE')).upper(),
    }


def location_alarm_from_dict(spec: dict) -> Alarm:
    proximity = str(spec.get('proximity') or 'ARRIVE').upper()
    if proximity not in PROXIMITIES:
        raise ValueError(f'proximity must be one of {PROXIMITIES}')
    if spec.get('lat') is None or spec.get('lon') is None:
        raise ValueError('location alarm needs lat and lon')

    loc = vUri(f"geo:{float(spec['lat'])}\\,{float(spec['lon'])}")  # escaped: see module docstring
    loc.params['VALUE'] = 'URI'
    loc.params['X-APPLE-RADIUS'] = str(float(spec.get('radius') or 100))
    if spec.get('title'):
        loc.params['X-TITLE'] = spec['title']
    if spec.get('address'):
        loc.params['X-ADDRESS'] = spec['address']

    alarm_uid = str(uuid.uuid4()).upper()
    a = Alarm()
    a.add('ACTION', 'DISPLAY')
    a.add('DESCRIPTION', 'Reminder')
    a.add('TRIGGER', APPLE_PLACEHOLDER_TRIGGER)
    a.add('UID', alarm_uid)
    a.add('X-WR-ALARMUID', alarm_uid)
    a.add('X-APPLE-PROXIMITY', proximity)
    a.add('X-APPLE-STRUCTURED-LOCATION', loc)
    return a
