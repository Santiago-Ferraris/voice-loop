from __future__ import annotations

import pytest

from voiceloop.events import Event
from voiceloop.store import (
    INGEST_COALESCED,
    INGEST_DUPLICATE,
    INGEST_INSERTED,
    INGEST_RESOLVED,
    STATE_ANNOUNCING,
    STATE_PENDING,
    STATE_QUEUED,
    STATE_RESOLVED,
    RESOLVED_BY_ACTIVITY,
    Store,
)


def stop(session: str = "session-1", *, ts: int = 1000, **payload) -> Event:
    return Event.new(
        "stop", session, ts=ts, transcript_path=f"/tmp/{session}.jsonl", payload=payload
    )


def test_a_fresh_event_is_queued(store):
    event = stop()

    assert store.ingest(event) == INGEST_INSERTED

    item = store.next_queued()
    assert item.id == event.id
    assert item.state == STATE_QUEUED
    assert item.transcript_path == "/tmp/session-1.jsonl"


def test_replaying_the_same_event_id_changes_nothing(store):
    event = stop()
    store.ingest(event)

    assert store.ingest(event) == INGEST_DUPLICATE
    assert store.queued_count() == 1


def test_queue_is_fifo_by_arrival(store):
    for index, session in enumerate(["a", "b", "c"]):
        store.ingest(stop(session, ts=1000 + index))

    assert [item.session_id for item in store.queued_items()] == ["a", "b", "c"]
    assert store.next_queued().session_id == "a"


def test_same_second_arrivals_keep_insertion_order(store):
    for session in ["a", "b", "c"]:
        store.ingest(stop(session, ts=1000))

    assert [item.session_id for item in store.queued_items()] == ["a", "b", "c"]


def test_a_second_stop_coalesces_and_keeps_its_place(store):
    store.ingest(stop("first", ts=1000))
    store.ingest(stop("second", ts=1001))
    second = stop("second", ts=1005, note="newer")

    assert store.ingest(second) == INGEST_COALESCED

    items = store.queued_items()
    assert [item.session_id for item in items] == ["first", "second"]
    assert items[1].id == second.id
    assert items[1].payload == {"note": "newer"}
    assert items[1].ts == 1001  # original position, not the newer timestamp
    assert store.queued_count() == 2


def test_coalescing_clears_a_stale_summary(store):
    first = stop("session-1")
    store.ingest(first)
    store.set_summary(first.id, "an answer about the old turn")

    store.ingest(stop("session-1", ts=1005))

    assert store.next_queued().summary is None


def test_coalescing_keeps_the_previous_transcript_when_the_new_one_is_blank(store):
    first = stop("session-1")
    store.ingest(first)

    store.ingest(Event.new("stop", "session-1", ts=1005))

    assert store.next_queued().transcript_path == "/tmp/session-1.jsonl"


def test_an_announced_item_is_superseded_not_duplicated(store):
    """Observed in the wild: four `pending` rows for one window, four announces.

    A session has one open state — "waiting, and this is the last thing it
    said". Blocking again refreshes it; it does not queue a second copy.
    """
    first = stop("session-1", ts=1000)
    store.ingest(first)
    store.mark_announcing(first.id)
    store.mark_pending(first.id)
    second = stop("session-1", ts=1005, note="newer")

    assert store.ingest(second) == INGEST_COALESCED

    assert store.open_count() == 1
    item = store.pendings()[0]
    assert item.id == second.id
    assert item.payload == {"note": "newer"}


def test_a_superseded_item_is_not_announced_again(store):
    """It stays `pending`: you already heard about that window."""
    first = stop("session-1", ts=1000)
    store.ingest(first)
    store.mark_announcing(first.id)
    store.mark_pending(first.id, now=4061)

    store.ingest(stop("session-1", ts=1005))

    item = store.pendings()[0]
    assert item.state == STATE_PENDING
    assert item.announced_at == 4061
    assert store.next_queued() is None


def test_superseding_keeps_the_original_place_in_line(store):
    store.ingest(stop("first", ts=1000))
    second = stop("second", ts=1001)
    store.ingest(second)
    store.mark_announcing(second.id)
    store.mark_pending(second.id)

    store.ingest(stop("second", ts=9999))

    assert [item.ts for item in store.pendings()] == [1000, 1001]


def test_superseding_an_announced_item_drops_the_stale_summary(store):
    first = stop("session-1", ts=1000)
    store.ingest(first)
    store.mark_announcing(first.id)
    store.mark_pending(first.id)
    store.set_summary(first.id, "quiere que revises el PR")

    store.ingest(stop("session-1", ts=1005))

    assert store.pendings()[0].summary is None


def test_a_stop_mid_announce_keeps_the_id_the_daemon_is_holding(store):
    """Swapping the id under a running announce would strand the row."""
    first = stop("session-1", ts=1000)
    store.ingest(first)
    store.mark_announcing(first.id)

    store.ingest(stop("session-1", ts=1005, note="newer"))

    assert store.get(first.id).payload == {"note": "newer"}
    store.mark_pending(first.id)
    assert store.pendings()[0].state == STATE_PENDING
    assert store.open_count() == 1


def test_ten_blocks_from_one_window_are_still_one_item(store):
    for tick in range(10):
        event = stop("session-1", ts=1000 + tick)
        store.ingest(event)
        store.mark_announcing(event.id)
        store.mark_pending(event.id)

    assert store.open_count() == 1


def test_two_windows_never_supersede_each_other(store):
    first = stop("session-1", ts=1000)
    store.ingest(first)
    store.mark_announcing(first.id)
    store.mark_pending(first.id)

    assert store.ingest(stop("session-2", ts=1005)) == INGEST_INSERTED
    assert store.open_count() == 2


def test_a_resolved_item_does_not_absorb_the_next_stop(store):
    first = stop("session-1", ts=1000)
    store.ingest(first)
    store.resolve(first.id, RESOLVED_BY_ACTIVITY)

    assert store.ingest(stop("session-1", ts=1005)) == INGEST_INSERTED
    assert store.open_count() == 1


def test_a_menu_does_not_coalesce_into_a_stop(store):
    store.ingest(stop("session-1"))

    assert store.ingest(Event.new("menu", "session-1")) == INGEST_INSERTED
    assert store.queued_count() == 2


def test_events_without_a_session_never_coalesce(store):
    store.ingest(Event.new("milestone", "", payload={"label": "PR created"}))
    store.ingest(Event.new("milestone", "", payload={"label": "CI green"}))

    assert store.queued_count() == 2


def test_activity_resolves_everything_open_for_that_session(store):
    first = stop("session-1", ts=1000)
    menu = Event.new("menu", "session-1", ts=1001)
    other = stop("session-2", ts=1002)
    for event in (first, menu, other):
        store.ingest(event)
    store.mark_announcing(first.id)
    store.mark_pending(first.id)

    assert store.ingest(Event.new("activity", "session-1")) == INGEST_RESOLVED

    assert store.get(first.id).state == STATE_RESOLVED
    assert store.get(first.id).resolved_by == RESOLVED_BY_ACTIVITY
    assert store.get(menu.id).state == STATE_RESOLVED
    assert store.get(other.id).state == STATE_QUEUED


def test_activity_for_an_unknown_session_is_harmless(store):
    store.ingest(stop("session-1"))

    store.ingest(Event.new("activity", "session-nobody"))

    assert store.queued_count() == 1


def test_activity_is_never_queued_itself(store):
    store.ingest(Event.new("activity", "session-1"))

    assert store.queued_count() == 0
    assert store.pendings() == []


def test_state_machine_walks_queued_announcing_pending_resolved(store):
    event = stop()
    store.ingest(event)

    store.mark_announcing(event.id)
    assert store.get(event.id).state == STATE_ANNOUNCING
    assert store.queued_count() == 0

    store.mark_pending(event.id, now=1234)
    item = store.get(event.id)
    assert (item.state, item.announced_at) == (STATE_PENDING, 1234)

    store.resolve(event.id, "voice", now=2345)
    item = store.get(event.id)
    assert (item.state, item.resolved_at, item.resolved_by) == (STATE_RESOLVED, 2345, "voice")


def test_resolving_twice_does_not_rewrite_the_first_resolution(store):
    event = stop()
    store.ingest(event)
    store.resolve(event.id, "voice", now=1)

    assert store.resolve(event.id, "activity", now=2) == 0
    assert store.get(event.id).resolved_by == "voice"


def test_an_ignored_item_stays_pending_forever(store):
    """Skipping an announcement must never drop the request."""
    event = stop()
    store.ingest(event)
    store.mark_announcing(event.id)
    store.mark_pending(event.id)

    for _ in range(5):
        store.ingest(stop("someone-else"))

    assert [item.id for item in store.pendings() if item.id == event.id] == [event.id]
    assert store.get(event.id).state == STATE_PENDING


def test_pendings_lists_open_items_oldest_first(store):
    ids = []
    for index in range(3):
        event = stop(f"session-{index}", ts=1000 + index)
        store.ingest(event)
        ids.append(event.id)
    store.resolve(ids[1], "voice")

    assert [item.id for item in store.pendings()] == [ids[0], ids[2]]


def test_requeue_puts_an_item_back_in_line(store):
    event = stop()
    store.ingest(event)
    store.mark_announcing(event.id)
    store.mark_pending(event.id)

    store.requeue(event.id)

    item = store.get(event.id)
    assert (item.state, item.announced_at) == (STATE_QUEUED, None)


def test_requeue_refuses_a_resolved_item(store):
    event = stop()
    store.ingest(event)
    store.resolve(event.id, "voice")

    store.requeue(event.id)

    assert store.get(event.id).state == STATE_RESOLVED


def test_recover_in_flight_requeues_a_crashed_announce(store):
    event = stop()
    store.ingest(event)
    store.mark_announcing(event.id)

    assert store.recover_in_flight() == 1
    assert store.get(event.id).state == STATE_QUEUED


def test_last_announced_is_what_replay_repeats(store):
    first, second = stop("a", ts=1000), stop("b", ts=1001)
    for event in (first, second):
        store.ingest(event)
        store.mark_announcing(event.id)
    store.mark_pending(first.id, now=10)
    store.mark_pending(second.id, now=20)

    assert store.last_announced().id == second.id


def test_last_announced_ignores_resolved_items(store):
    event = stop()
    store.ingest(event)
    store.mark_pending(event.id, now=10)
    store.resolve(event.id, "voice")

    assert store.last_announced() is None


def test_items_for_dead_sessions_are_resolved(store):
    alive, dead = stop("alive"), stop("dead")
    store.ingest(alive)
    store.ingest(dead)

    assert store.resolve_sessions_missing({"alive"}, "session-gone") == 1
    assert store.get(dead.id).state == STATE_RESOLVED
    assert store.get(alive.id).state == STATE_QUEUED


def test_aliases_round_trip_and_update(store):
    assert store.get_alias("session-1") is None

    store.set_alias("session-1", "the migration one")
    assert store.get_alias("session-1") == "the migration one"

    store.set_alias("session-1", "renamed", confirmed=True)
    assert store.get_alias("session-1") == "renamed"


@pytest.mark.parametrize("value", [True, 3, "hola", {"a": [1, 2]}, None])
def test_kv_round_trips_json_values(store, value):
    store.kv_set("k", value)

    assert store.kv_get("k") == value


def test_kv_returns_the_default_when_unset(store):
    assert store.kv_get("missing", "fallback") == "fallback"


def test_kv_overwrites(store):
    store.kv_set("paused", True)
    store.kv_set("paused", False)

    assert store.kv_get("paused") is False


def test_state_survives_reopening_the_database(tmp_path):
    path = tmp_path / "queue.db"
    event = stop()
    with Store(path) as first:
        first.ingest(event)
        first.kv_set("paused", True)

    with Store(path) as second:
        assert second.next_queued().id == event.id
        assert second.kv_get("paused") is True


def test_database_runs_in_wal_mode(store):
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]

    assert mode.lower() == "wal"


def test_display_name_falls_back_to_the_session_id(store):
    event = stop("abcdef123456")
    store.ingest(event)

    assert store.get(event.id).display_name == "abcdef12"
