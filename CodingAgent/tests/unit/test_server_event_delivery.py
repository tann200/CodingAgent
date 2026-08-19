import asyncio

from src.server.event_delivery import enqueue_with_drop_policy, record_dropped_event


def test_enqueue_with_drop_policy_enqueues_when_space_available():
    q = asyncio.Queue(maxsize=1)

    dropped = []
    assert enqueue_with_drop_policy(
        q,
        ("agent.start", {"ok": True}),
        drop_policy="drop_oldest",
        on_drop=dropped.append,
    )
    assert q.get_nowait() == ("agent.start", {"ok": True})
    assert dropped == []


def test_enqueue_with_drop_policy_drop_oldest_evicts_prior_item():
    q = asyncio.Queue(maxsize=1)
    q.put_nowait(("old.event", {"seq": 1}))

    dropped = []
    assert enqueue_with_drop_policy(
        q,
        ("new.event", {"seq": 2}),
        drop_policy="drop_oldest",
        on_drop=dropped.append,
        on_evict=dropped.append,
    )
    assert q.get_nowait() == ("new.event", {"seq": 2})
    assert dropped == ["old.event"]


def test_enqueue_with_drop_policy_drop_new_drops_incoming_item():
    q = asyncio.Queue(maxsize=1)
    q.put_nowait(("old.event", {"seq": 1}))

    dropped = []
    assert not enqueue_with_drop_policy(
        q,
        ("new.event", {"seq": 2}),
        drop_policy="drop_new",
        on_drop=dropped.append,
    )
    assert q.get_nowait() == ("old.event", {"seq": 1})
    assert dropped == ["new.event"]


def test_enqueue_with_drop_policy_unknown_policy_behaves_like_drop_new():
    q = asyncio.Queue(maxsize=1)
    q.put_nowait(("old.event", {"seq": 1}))

    dropped = []
    assert not enqueue_with_drop_policy(
        q,
        ("new.event", {"seq": 2}),
        drop_policy="unexpected",
        on_drop=dropped.append,
    )
    assert q.get_nowait() == ("old.event", {"seq": 1})
    assert dropped == ["new.event"]


def test_record_dropped_event_updates_both_counters():
    aggregate = []
    per_client = []

    record_dropped_event(
        "agent.start",
        "session-1",
        inc_event_dropped_counter=aggregate.append,
        inc_client_event_dropped_counter=lambda event_name, session_id: per_client.append(
            (event_name, session_id)
        ),
    )

    assert aggregate == ["agent.start"]
    assert per_client == [("agent.start", "session-1")]
