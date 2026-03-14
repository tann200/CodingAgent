import pytest
from src.core.orchestration.orchestrator import Orchestrator
from src.tools import role_tools
from src.core.orchestration.event_bus import get_event_bus


def test_set_role_applies_to_orchestrator():
    o = Orchestrator()
    assert getattr(o, 'current_role', None) is None
    res = role_tools.set_role('tester', orchestrator=o)
    assert res['status'] == 'ok'
    assert res['role'] == 'tester'
    assert o.current_role == 'tester'


def test_role_event_published(monkeypatch):
    bus = get_event_bus()
    events = []

    def cb(payload):
        events.append(payload)

    bus.subscribe('role.changed', cb)
    # Call set_role without orchestrator to trigger publish
    res = role_tools.set_role('auditor', orchestrator=None)
    assert res['status'] == 'ok'
    assert res['role'] == 'auditor'
    # allow small time for synchronous publish
    found = False
    for e in events:
        if isinstance(e, dict) and e.get('role') == 'auditor':
            found = True
            break
        if isinstance(e, dict) and isinstance(e.get('payload'), dict) and e.get('payload').get('role') == 'auditor':
            found = True
            break
    assert found
