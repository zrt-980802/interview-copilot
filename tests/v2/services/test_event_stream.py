from concurrent.futures import ThreadPoolExecutor
import json
import queue

import pytest

from interview_copilot.services.event_stream import (
    ReplayEventHub, SubscriptionClosed, SubscriptionOverflow,
)


def test_replay_and_live_subscription_have_one_boundary():
    hub = ReplayEventHub()
    first = hub.publish({"type": "answer_start"})
    second = hub.publish({"type": "answer_delta", "delta": "part"})
    sub, replay = hub.subscribe(first.event_id)
    third = hub.publish({"type": "answer_done", "full": "part"})
    assert replay == (second,)
    assert hub.read(sub, timeout=0) == third
    with pytest.raises(queue.Empty):
        hub.read(sub, timeout=0)
    hub.unsubscribe(sub)
    assert hub.stats()["subscribers"] == 0


@pytest.mark.parametrize("cursor", [None, "bad", "other:1", "0" * 32 + ":999999", "0" * 32 + ":-1"])
def test_unknown_cursor_requires_snapshot(cursor):
    hub = ReplayEventHub()
    hub.publish({"type": "status"})
    sub, replay = hub.subscribe(cursor)
    assert replay is None
    hub.unsubscribe(sub)


def test_future_cursor_from_this_stream_requires_snapshot():
    hub = ReplayEventHub()
    prefix = hub.cursor.rpartition(":")[0]
    sub, replay = hub.subscribe(prefix + ":5")
    assert replay is None
    hub.unsubscribe(sub)


def test_retention_limits_events_and_bytes_without_claiming_complete_replay():
    hub = ReplayEventHub(max_events=2, max_bytes=1500)
    old = hub.publish({"type": "status"})
    for _ in range(3):
        hub.publish({"type": "answer_delta", "delta": "a" * 400})
    sub, replay = hub.subscribe(old.event_id)
    assert replay is None
    assert hub.stats()["events"] <= 2
    assert hub.stats()["bytes"] <= 1500
    hub.unsubscribe(sub)


def test_slow_subscriber_requires_resync_and_does_not_block_other_clients():
    hub = ReplayEventHub(subscriber_events=2)
    slow, _ = hub.subscribe()
    fast, _ = hub.subscribe()
    for i in range(3):
        event = hub.publish({"type": "answer_delta", "delta": str(i)})
        assert hub.read(fast, timeout=0) == event
    with pytest.raises(SubscriptionOverflow):
        hub.read(slow, timeout=0)
    assert hub.resync(slow) == event.event_id
    next_event = hub.publish({"type": "answer_done", "full": "012"})
    assert hub.read(slow, timeout=0) == next_event
    hub.unsubscribe(slow)
    hub.unsubscribe(fast)


def test_oversized_event_triggers_snapshot_instead_of_silent_loss():
    hub = ReplayEventHub(max_bytes=300, subscriber_bytes=300)
    sub, _ = hub.subscribe()
    event = hub.publish({"type": "answer_done", "full": "a" * 1000})
    assert hub.stats()["bytes"] == 0
    with pytest.raises(SubscriptionOverflow):
        hub.read(sub, timeout=0)
    sub2, replay = hub.subscribe(event.event_id)
    assert replay == ()
    hub.unsubscribe(sub2)
    hub.unsubscribe(sub)


def test_unsubscribe_wakes_reader_and_releases_subscriber():
    hub = ReplayEventHub()
    sub, _ = hub.subscribe()
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(hub.read, sub, timeout=5)
        hub.unsubscribe(sub)
        with pytest.raises(SubscriptionClosed):
            result.result(timeout=1)
    assert hub.stats()["subscribers"] == 0


def test_disconnect_all_closes_existing_readers_but_allows_reconnect():
    hub = ReplayEventHub()
    first, _ = hub.subscribe()
    second, _ = hub.subscribe()
    hub.disconnect_all()
    for sub in (first, second):
        with pytest.raises(SubscriptionClosed):
            hub.read(sub, timeout=0)
    reconnected, replay = hub.subscribe(hub.cursor)
    assert replay == ()
    event = hub.publish({"type": "status"})
    assert hub.read(reconnected, timeout=0) == event
    hub.unsubscribe(reconnected)


def test_concurrent_publish_serializes_copied_payloads_in_sequence():
    hub = ReplayEventHub()
    sub, _ = hub.subscribe()
    with ThreadPoolExecutor(max_workers=4) as pool:
        events = tuple(pool.map(lambda i: hub.publish({"type": "status", "payload": {"value": i}}), range(30)))
    received = [hub.read(sub, timeout=0) for _ in events]
    assert [int(event.event_id.rsplit(":", 1)[1]) for event in received] == list(range(1, 31))
    assert len({event.event_id for event in events}) == 30
    payload = {"type": "status", "payload": {"value": "original"}}
    event = hub.publish(payload)
    payload["payload"]["value"] = "changed"
    assert json.loads(event.data)["payload"]["value"] == "original"
    hub.unsubscribe(sub)
