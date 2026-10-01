import socket
import subprocess
import sys
import time
import uuid

import pytest

from task_caldav_lib import CalDAVService

TZ = 'America/Los_Angeles'


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture(scope='session')
def radicale_url(tmp_path_factory):
    """A throwaway Radicale server (no auth) for the whole test session."""
    port = _free_port()
    storage = tmp_path_factory.mktemp('radicale')
    proc = subprocess.Popen(
        [sys.executable, '-m', 'radicale', f'--storage-filesystem-folder={storage}',
         '--auth-type=none', f'--server-hosts=127.0.0.1:{port}', '--logging-level=warning'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            socket.create_connection(('127.0.0.1', port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f'http://127.0.0.1:{port}/'
    proc.terminate()
    proc.wait()


@pytest.fixture
def svc(radicale_url):
    """A service bound to a fresh, empty calendar."""
    s = CalDAVService(radicale_url, 'test', 'x', f'Tasks-{uuid.uuid4().hex[:8]}', tz=TZ, create_calendar=True)
    s.refresh()
    return s


@pytest.fixture
def other_client(svc):
    """Simulates another CalDAV client (e.g. iPhone Reminders) writing the same calendar."""
    return svc._client
