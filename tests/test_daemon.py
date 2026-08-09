from __future__ import annotations

import asyncio
import json

import pytest

from voiceloop import spool
from voiceloop.control import ControlError
from voiceloop.daemon import Daemon
from voiceloop.events import Event
from voiceloop.milestones import MilestoneWatcher
from voiceloop.store import STATE_PENDING, STATE_QUEUED, STATE_RESOLVED, Store
from voiceloop.summarize import FALLBACK_SUMMARY, Summarizer

from conftest import FakeSpeaker, write_roster

LAUNCH = "Async agent launched successfully.\nagentId: aa11bb22"
DONE = "<task-notification>\n<task-id>aa11bb22</task-id>\n</task-notification>"


class FakeSummarizer(Summarizer):
    def __init__(self, answer: str = "quiere que revises el diff"):
        super().__init__(api_key="sk-test")
        self.answer = answer
        self.seen: list[str] = []

    def _call(self, text: str) -> str:
        # Overriding the transport, not the policy: an empty tail still takes
        # the real fallback path.
        self.seen.append(text)
        return self.answer


def transcript(tmp_path, name: str, *, launched: bool = False, done: bool = False,
               tail: str = "Ya está, ¿lo mergeo?") -> str:
    records = []
    if launched:
        records.append(
            {"type": "user", "message": {"role": "user", "content": LAUNCH}}
        )
    if done:
        records.append({"type": "user", "message": {"role": "user", "content": DONE}})
    records.append(
        {
            "type": "assistant",
            "isSidechain": False,
            "message": {"role": "assistant", "content": [{"type": "text", "text": tail}]},
        }
    )
    path = tmp_path / f"{name}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return str(path)


@pytest.fixture
def daemon(config, tmp_path):
    roster_dir = tmp_path / "sessions"
    roster_dir.mkdir()
    subject = Daemon(
        config,
        store=Store(tmp_path / "queue.db"),
        speaker=FakeSpeaker(),
        summarizer=FakeSummarizer(),
        watcher=MilestoneWatcher(),
        roster_dir=roster_dir,
    )
    subject.roster_path = roster_dir
    try:
        yield subject
    finally:
        subject.store.close()


def stop_event(session="session-1", **kwargs) -> Event:
    return Event.new("stop", session, **kwargs)


# --- ingest ---------------------------------------------------------------


def test_ingest_drains_the_spool_into_the_queue(daemon, config):
    spool.write(config.spool_dir, stop_event("a"))
    spool.write(config.spool_dir, stop_event("b"))

    assert daemon.ingest_once() == 2
    assert [item.session_id for item in daemon.store.queued_items()] == ["a", "b"]
    assert spool.list_files(config.spool_dir) == []


def test_ingest_is_idempotent_if_a_file_is_replayed(daemon, config):
    event = stop_event("a")
    path = spool.write(config.spool_dir, event)
    daemon.ingest_once()
    spool.write(config.spool_dir, event)  # same id, arriving twice

    daemon.ingest_once()

    assert daemon.store.queued_count() == 1
    assert not path.exists()


def test_ingest_quarantines_what_it_cannot_parse(daemon, config):
    spool.ensure_dirs(config.spool_dir)
    (config.spool_dir / "0000000000000000001-1-bad.json").write_text("{oops")

    assert daemon.ingest_once() == 0
    assert list((config.spool_dir / "bad").glob("*.json"))


# --- announcing -----------------------------------------------------------


def test_a_stop_is_announced_with_name_and_summary(daemon, tmp_path):
    write_roster(daemon.roster_path, sessionId="s1", name="index-migration")
    daemon.store.ingest(stop_event("s1", transcript_path=transcript(tmp_path, "s1")))

    assert asyncio.run(daemon.announce_next()) is True

    assert daemon.speaker.texts == ["index migration: quiere que revises el diff."]
    assert daemon.store.get_alias("s1") is None
    assert daemon.store.pendings()[0].state == STATE_PENDING


def test_the_summary_is_built_from_the_transcript_tail(daemon, tmp_path):
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    path = transcript(tmp_path, "s1", tail="Terminé el backfill. ¿Sigo con staging?")
    daemon.store.ingest(stop_event("s1", transcript_path=path))

    asyncio.run(daemon.announce_next())

    assert daemon.summarizer.seen == ["Terminé el backfill. ¿Sigo con staging?"]


def test_the_summary_is_stored_so_a_replay_does_not_pay_twice(daemon, tmp_path):
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    event = stop_event("s1", transcript_path=transcript(tmp_path, "s1"))
    daemon.store.ingest(event)
    asyncio.run(daemon.announce_next())

    daemon.store.requeue(event.id)
    asyncio.run(daemon.announce_next())

    assert len(daemon.summarizer.seen) == 1
    assert len(daemon.speaker.texts) == 2


def test_the_queue_is_announced_in_order_and_never_counts_down_up_front(daemon, tmp_path):
    for index, name in enumerate(("alpha", "beta", "gamma")):
        write_roster(daemon.roster_path, sessionId=name, name=name)
        daemon.store.ingest(
            Event.new("stop", name, ts=1000 + index, transcript_path=transcript(tmp_path, name))
        )

    async def drain():
        while await daemon.announce_next():
            pass

    asyncio.run(drain())

    assert daemon.speaker.texts == [
        "alpha: quiere que revises el diff.",
        "beta: quiere que revises el diff.",
        "gamma: quiere que revises el diff.",
    ]


def test_a_window_that_blocks_again_is_not_announced_twice(daemon, tmp_path):
    """The live failure: one window, four announces, four rows in `pendings`."""
    write_roster(daemon.roster_path, sessionId="s1", name="darwin-96")
    path = transcript(tmp_path, "s1")
    daemon.store.ingest(stop_event("s1", ts=1000, transcript_path=path))
    assert asyncio.run(daemon.announce_next()) is True

    daemon.store.ingest(stop_event("s1", ts=1005, transcript_path=path))

    assert asyncio.run(daemon.announce_next()) is False
    assert len(daemon.speaker.texts) == 1
    assert len(daemon.store.pendings()) == 1


def test_the_refreshed_turn_is_what_replay_speaks(daemon, tmp_path):
    """Superseding keeps the item quiet but must not leave it stale."""
    write_roster(daemon.roster_path, sessionId="s1", name="darwin-96")
    daemon.store.ingest(
        stop_event("s1", ts=1000, transcript_path=transcript(tmp_path, "s1", tail="¿lo mergeo?"))
    )
    asyncio.run(daemon.announce_next())
    daemon.summarizer.answer = "quiere que decidas la migración"

    daemon.store.ingest(
        stop_event("s1", ts=1005, transcript_path=transcript(tmp_path, "s1", tail="¿migro ya?"))
    )
    asyncio.run(daemon.dispatch("replay", {}))
    asyncio.run(daemon.announce_next())

    assert "quiere que decidas la migración" in daemon.speaker.texts[-1]


def test_nothing_to_announce_returns_false(daemon):
    assert asyncio.run(daemon.announce_next()) is False


def test_a_paused_daemon_queues_but_stays_quiet(daemon, tmp_path):
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    daemon.store.ingest(stop_event("s1", transcript_path=transcript(tmp_path, "s1")))
    daemon.cmd_pause({})

    assert asyncio.run(daemon.announce_next()) is False
    assert daemon.speaker.said == []
    assert daemon.store.queued_count() == 1

    daemon.cmd_resume({})
    assert asyncio.run(daemon.announce_next()) is True


# --- the subagent gate ----------------------------------------------------


def test_a_stop_with_a_subagent_in_flight_is_not_announced(daemon, tmp_path):
    """The turn ended, but the user has nothing to answer yet."""
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    path = transcript(tmp_path, "s1", launched=True)
    daemon.store.ingest(stop_event("s1", transcript_path=path))

    assert asyncio.run(daemon.announce_next()) is False
    assert daemon.speaker.said == []
    assert daemon.store.queued_count() == 1  # still there, still first in line


def test_the_gated_item_is_announced_once_the_agent_finishes(daemon, tmp_path):
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    path = transcript(tmp_path, "s1", launched=True)
    daemon.store.ingest(stop_event("s1", transcript_path=path))
    asyncio.run(daemon.announce_next())

    transcript(tmp_path, "s1", launched=True, done=True)

    assert asyncio.run(daemon.announce_next()) is True
    assert daemon.speaker.texts == ["alpha: quiere que revises el diff."]


def test_a_gated_item_does_not_block_the_rest_of_the_queue(daemon, tmp_path):
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    write_roster(daemon.roster_path, sessionId="s2", name="beta")
    daemon.store.ingest(
        Event.new("stop", "s1", ts=1000, transcript_path=transcript(tmp_path, "s1", launched=True))
    )
    daemon.store.ingest(
        Event.new("stop", "s2", ts=1001, transcript_path=transcript(tmp_path, "s2"))
    )

    assert asyncio.run(daemon.announce_next()) is True

    assert [item.text.split(":")[0] for item in daemon.speaker.said] == ["beta"]
    assert daemon.store.queued_items()[0].session_id == "s1"


def test_the_gate_only_applies_to_stops(daemon, tmp_path):
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    daemon.store.ingest(
        Event.new(
            "menu",
            "s1",
            transcript_path=transcript(tmp_path, "s1", launched=True),
            payload={"tool_input": {"questions": [{"question": "¿Seguimos?"}]}},
        )
    )

    assert asyncio.run(daemon.announce_next()) is True


def test_a_missing_transcript_does_not_gate(daemon):
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    daemon.store.ingest(stop_event("s1", transcript_path="/nonexistent.jsonl"))

    assert asyncio.run(daemon.announce_next()) is True
    assert daemon.speaker.texts == [f"alpha: {FALLBACK_SUMMARY}."]


def test_the_gate_result_is_cached_until_the_transcript_changes(daemon, tmp_path):
    path = transcript(tmp_path, "s1", launched=True)
    calls = []
    real = daemon._gated_count

    def counting(argument):
        calls.append(argument)
        return real(argument)

    daemon._gated_count = counting
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    daemon.store.ingest(stop_event("s1", transcript_path=path))

    for _ in range(3):
        asyncio.run(daemon.announce_next())

    assert len(calls) == 3
    assert len(daemon._gate_cache) == 1


# --- roster filtering -----------------------------------------------------


def test_a_background_agent_is_resolved_without_speaking(daemon, tmp_path):
    write_roster(daemon.roster_path, sessionId="bg1", kind="bg", name="Some background job")
    event = stop_event("bg1", transcript_path=transcript(tmp_path, "bg1"))
    daemon.store.ingest(event)

    assert asyncio.run(daemon.announce_next()) is False

    assert daemon.speaker.said == []
    assert daemon.store.get(event.id).state == STATE_RESOLVED
    assert daemon.store.get(event.id).resolved_by == "background-session"


def test_a_session_missing_from_the_roster_is_still_announced(daemon, tmp_path):
    """Fail open: better a stray announcement than a silently swallowed one."""
    daemon.store.ingest(
        stop_event("ghost-session", cwd="/tmp/projects/rescue",
                   transcript_path=transcript(tmp_path, "g"))
    )

    assert asyncio.run(daemon.announce_next()) is True
    assert daemon.speaker.texts == ["rescue: quiere que revises el diff."]


def test_an_alias_wins_over_the_roster_name(daemon, tmp_path):
    write_roster(daemon.roster_path, sessionId="s1", name="workspace-21")
    daemon.store.set_alias("s1", "el de la migración")
    daemon.store.ingest(stop_event("s1", transcript_path=transcript(tmp_path, "s1")))

    asyncio.run(daemon.announce_next())

    assert daemon.speaker.texts[0].startswith("el de la migración:")


# --- menus, notifications, milestones -------------------------------------


def test_a_menu_is_read_without_calling_the_summariser(daemon):
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    daemon.store.ingest(
        Event.new(
            "menu",
            "s1",
            payload={
                "tool": "AskUserQuestion",
                "tool_input": {
                    "questions": [
                        {"question": "¿Qué base uso?", "options": [{"label": "SQLite"}]}
                    ]
                },
            },
        )
    )

    asyncio.run(daemon.announce_next())

    assert daemon.speaker.texts == ["alpha: ¿Qué base uso? Opciones: uno: SQLite."]
    assert daemon.summarizer.seen == []


def test_a_milestone_chimes_and_resolves_itself(daemon):
    event = Event.new("milestone", "", payload={"label": "PR created"})
    daemon.store.ingest(event)

    assert asyncio.run(daemon.announce_next()) is True

    announcement, = daemon.speaker.said
    assert announcement.silent is True
    assert announcement.chime == "Glass"
    assert daemon.store.get(event.id).state == STATE_RESOLVED


def test_a_notification_speaks_and_waits_for_you(daemon):
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    event = Event.new("notification", "s1", payload={"message": "necesita permiso"})
    daemon.store.ingest(event)

    asyncio.run(daemon.announce_next())

    assert daemon.speaker.texts == ["alpha: necesita permiso."]
    assert daemon.store.get(event.id).state == STATE_PENDING


def test_notifications_can_be_muted_to_a_chime(daemon):
    daemon.notification_events = False
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    daemon.store.ingest(Event.new("notification", "s1", payload={"message": "necesita permiso"}))

    asyncio.run(daemon.announce_next())

    assert daemon.speaker.texts == []
    assert daemon.speaker.said[0].chime == "Ping"


def test_activity_resolves_a_pending_item_end_to_end(daemon, config, tmp_path):
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    event = stop_event("s1", transcript_path=transcript(tmp_path, "s1"))
    spool.write(config.spool_dir, event)
    daemon.ingest_once()
    asyncio.run(daemon.announce_next())

    spool.write(config.spool_dir, Event.new("activity", "s1"))
    daemon.ingest_once()

    assert daemon.store.get(event.id).state == STATE_RESOLVED
    assert daemon.store.pendings() == []


# --- startup reconciliation -----------------------------------------------


def test_reconcile_requeues_an_interrupted_announce(daemon):
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    event = stop_event("s1")
    daemon.store.ingest(event)
    daemon.store.mark_announcing(event.id)

    daemon.reconcile()

    assert daemon.store.get(event.id).state == STATE_QUEUED


def test_reconcile_resolves_items_whose_window_is_gone(daemon):
    write_roster(daemon.roster_path, sessionId="alive", name="alpha")
    gone = stop_event("closed-window")
    alive = stop_event("alive")
    daemon.store.ingest(gone)
    daemon.store.ingest(alive)

    daemon.reconcile()

    assert daemon.store.get(gone.id).state == STATE_RESOLVED
    assert daemon.store.get(gone.id).resolved_by == "session-gone"
    assert daemon.store.get(alive.id).state == STATE_QUEUED


def test_reconcile_keeps_everything_when_the_roster_is_empty(daemon):
    """An empty roster means we cannot tell — do not resolve the whole queue."""
    event = stop_event("s1")
    daemon.store.ingest(event)

    daemon.reconcile()

    assert daemon.store.get(event.id).state == STATE_QUEUED


# --- the control surface --------------------------------------------------


def test_status_reports_the_queue(daemon):
    daemon.store.ingest(stop_event("s1"))

    status = asyncio.run(daemon.dispatch("status", {}))

    assert status["queued"] == 1
    assert status["open"] == 1
    assert status["paused"] is False
    assert status["summaries"] == "openai"
    assert status["milestone_watch"] is False


def test_pendings_lists_what_is_waiting(daemon, tmp_path):
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    daemon.store.ingest(stop_event("s1", transcript_path=transcript(tmp_path, "s1")))
    asyncio.run(daemon.announce_next())

    pendings = asyncio.run(daemon.dispatch("pendings", {}))

    assert len(pendings) == 1
    assert pendings[0]["name"] == "alpha"
    assert pendings[0]["state"] == STATE_PENDING
    assert pendings[0]["summary"] == "quiere que revises el diff"


def test_a_superseded_item_gets_its_summary_back_when_you_ask_for_it(daemon, tmp_path):
    """Issue #3: ten of ten pendings had no summary, so the list said nothing.

    Superseding drops the summary on purpose — it described a turn that is no
    longer the last one — and the item deliberately does not go back in the
    queue, so the announce path never recomputes it. Reading is the only moment
    left that can.
    """
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    first = stop_event("s1", transcript_path=transcript(tmp_path, "s1"))
    daemon.store.ingest(first)
    asyncio.run(daemon.announce_next())
    daemon.summarizer.answer = "quiere que apruebes el plan nuevo"
    newer = transcript(tmp_path, "s1-newer", tail="Cambió todo. ¿Apruebo el plan?")
    daemon.store.ingest(stop_event("s1", transcript_path=newer))

    assert daemon.store.pendings()[0].summary is None

    pendings = asyncio.run(daemon.dispatch("pendings", {}))

    assert pendings[0]["summary"] == "quiere que apruebes el plan nuevo"
    # …and it is written back, so asking twice costs one call.
    assert daemon.store.pendings()[0].summary == "quiere que apruebes el plan nuevo"
    assert daemon.summarizer.seen[-1] == "Cambió todo. ¿Apruebo el plan?"


def test_a_summary_that_is_already_there_is_not_recomputed(daemon, tmp_path):
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    daemon.store.ingest(stop_event("s1", transcript_path=transcript(tmp_path, "s1")))
    asyncio.run(daemon.announce_next())

    asyncio.run(daemon.dispatch("pendings", {}))

    assert len(daemon.summarizer.seen) == 1


def test_every_stale_summary_is_recomputed_at_once(daemon, tmp_path):
    """Ten items at five seconds each, one after the other, is not a list."""
    for name in ("a", "b", "c"):
        write_roster(daemon.roster_path, sessionId=name, name=f"win-{name}")
        daemon.store.ingest(stop_event(name, transcript_path=transcript(tmp_path, name)))

    pendings = asyncio.run(daemon.dispatch("pendings", {}))

    assert [entry["summary"] for entry in pendings] == ["quiere que revises el diff"] * 3


def test_a_summariser_that_is_down_degrades_to_the_template(daemon, tmp_path):
    """And leaves the row empty, so the next read tries again instead of lying."""
    daemon.summarizer = Summarizer(api_key=None)
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    item = stop_event("s1", transcript_path=transcript(tmp_path, "s1"))
    daemon.store.ingest(item)

    pendings = asyncio.run(daemon.dispatch("pendings", {}))

    assert pendings[0]["summary"] == FALLBACK_SUMMARY
    assert daemon.store.get(item.id).summary is None


def test_a_queued_item_is_listed_by_name_not_by_session_id(daemon, tmp_path):
    """`pendings` exists to tell you which window wants you. `5cbf3ac9` does not."""
    write_roster(daemon.roster_path, sessionId="s1", name="darwin-96")
    daemon.store.ingest(stop_event("s1", transcript_path=transcript(tmp_path, "s1")))

    pendings = asyncio.run(daemon.dispatch("pendings", {}))

    assert pendings[0]["state"] == STATE_QUEUED
    assert pendings[0]["name"] == "darwin-96"


def test_a_listed_name_follows_the_roster_as_it_changes(daemon, tmp_path):
    write_roster(daemon.roster_path, sessionId="s1", name="darwin-96")
    daemon.store.ingest(stop_event("s1"))
    write_roster(daemon.roster_path, sessionId="s1", name="tcc-fix")

    pendings = asyncio.run(daemon.dispatch("pendings", {}))

    assert pendings[0]["name"] == "tcc-fix"


def test_an_alias_still_wins_when_listing(daemon):
    write_roster(daemon.roster_path, sessionId="s1", name="darwin-96")
    daemon.store.ingest(stop_event("s1"))
    daemon.store.set_alias("s1", "el de los hooks")

    pendings = asyncio.run(daemon.dispatch("pendings", {}))

    assert pendings[0]["name"] == "el de los hooks"


def test_a_session_off_the_roster_falls_back_to_its_directory(daemon):
    daemon.store.ingest(stop_event("s1", cwd="/Users/me/Documents/darwin/voice-loop"))

    pendings = asyncio.run(daemon.dispatch("pendings", {}))

    assert pendings[0]["name"] == "voice-loop"


def test_a_milestone_is_listed_without_inventing_a_window(daemon):
    asyncio.run(daemon.dispatch("milestone", {"label": "CI green"}))

    pendings = asyncio.run(daemon.dispatch("pendings", {}))

    assert pendings[0]["name"] == ""


# --- skip -----------------------------------------------------------------


def test_skip_drops_the_last_announcement(daemon, tmp_path):
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    event = stop_event("s1", transcript_path=transcript(tmp_path, "s1"))
    daemon.store.ingest(event)
    asyncio.run(daemon.announce_next())

    result = asyncio.run(daemon.dispatch("skip", {}))

    assert result["skipped"] == event.id
    assert daemon.store.get(event.id).state == STATE_RESOLVED
    assert daemon.store.get(event.id).resolved_by == "skip"
    assert asyncio.run(daemon.dispatch("pendings", {})) == []


def test_skip_accepts_an_explicit_id(daemon):
    """The reason it exists: a window that died, listed and never leaving."""
    first = stop_event("s1")
    second = stop_event("s2")
    daemon.store.ingest(first)
    daemon.store.ingest(second)

    asyncio.run(daemon.dispatch("skip", {"id": first.id}))

    assert daemon.store.get(first.id).state == STATE_RESOLVED
    assert daemon.store.get(second.id).state == STATE_QUEUED


def test_skipping_does_not_disturb_the_rest_of_the_queue(daemon):
    daemon.store.ingest(stop_event("s1", ts=1000))
    target = stop_event("s2", ts=1001)
    daemon.store.ingest(target)
    daemon.store.ingest(stop_event("s3", ts=1002))

    asyncio.run(daemon.dispatch("skip", {"id": target.id}))

    assert [item["session_id"] for item in asyncio.run(daemon.dispatch("pendings", {}))] == ["s1", "s3"]


def test_skip_with_nothing_to_skip_is_an_error(daemon):
    with pytest.raises(ControlError, match="nothing to skip"):
        asyncio.run(daemon.dispatch("skip", {}))


def test_skipping_an_unknown_id_is_an_error(daemon):
    with pytest.raises(ControlError, match="no such item"):
        asyncio.run(daemon.dispatch("skip", {"id": "does-not-exist"}))


def test_skipping_twice_is_an_error_not_a_silent_no_op(daemon):
    event = stop_event("s1")
    daemon.store.ingest(event)
    asyncio.run(daemon.dispatch("skip", {"id": event.id}))

    with pytest.raises(ControlError, match="already resolved"):
        asyncio.run(daemon.dispatch("skip", {"id": event.id}))


def test_a_skipped_session_can_come_back(daemon):
    first = stop_event("s1", ts=1000)
    daemon.store.ingest(first)
    asyncio.run(daemon.dispatch("skip", {"id": first.id}))

    daemon.store.ingest(stop_event("s1", ts=1005))

    assert len(asyncio.run(daemon.dispatch("pendings", {}))) == 1


def test_pause_and_resume_survive_a_restart(daemon, config, tmp_path):
    asyncio.run(daemon.dispatch("pause", {}))

    revived = Daemon(
        config,
        store=Store(tmp_path / "queue.db"),
        speaker=FakeSpeaker(),
        summarizer=FakeSummarizer(),
        watcher=MilestoneWatcher(),
        roster_dir=daemon.roster_path,
    )
    try:
        assert revived.paused is True
    finally:
        revived.store.close()


def test_replay_puts_the_last_announcement_back_in_the_queue(daemon, tmp_path):
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    event = stop_event("s1", transcript_path=transcript(tmp_path, "s1"))
    daemon.store.ingest(event)
    asyncio.run(daemon.announce_next())

    result = asyncio.run(daemon.dispatch("replay", {}))

    assert result["replaying"] == event.id
    assert daemon.store.get(event.id).state == STATE_QUEUED


def test_replay_accepts_an_explicit_id(daemon, tmp_path):
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    event = stop_event("s1")
    daemon.store.ingest(event)
    daemon.store.mark_pending(event.id)

    asyncio.run(daemon.dispatch("replay", {"id": event.id}))

    assert daemon.store.get(event.id).state == STATE_QUEUED


def test_replay_with_nothing_to_replay_is_an_error(daemon):
    with pytest.raises(ControlError, match="nothing to replay"):
        asyncio.run(daemon.dispatch("replay", {}))


def test_the_milestone_command_queues_a_chime(daemon):
    result = asyncio.run(daemon.dispatch("milestone", {"label": "CI green"}))

    assert result["label"] == "CI green"
    asyncio.run(daemon.announce_next())
    assert daemon.speaker.said[0].silent is True
    assert daemon.speaker.said[0].chime == "Glass"


@pytest.mark.parametrize("args", [{}, {"label": ""}, {"label": "   "}])
def test_a_milestone_without_a_label_is_refused(daemon, args):
    with pytest.raises(ControlError, match="needs a label"):
        asyncio.run(daemon.dispatch("milestone", args))


def test_an_unknown_command_is_refused(daemon):
    with pytest.raises(ControlError, match="unknown command"):
        asyncio.run(daemon.dispatch("teleport", {}))


def test_busy_toggle_is_a_real_command_now(daemon):
    assert asyncio.run(daemon.dispatch("busy-toggle", {})) == {"busy": True}
    assert asyncio.run(daemon.dispatch("busy-toggle", {})) == {"busy": False}


def test_mic_toggle_says_why_when_there_is_no_recognizer(daemon):
    daemon.stt = None

    with pytest.raises(ControlError, match="microphone unavailable"):
        asyncio.run(daemon.dispatch("mic-toggle", {}))


def test_private_attributes_are_not_reachable_as_commands(daemon):
    for cmd in ("_announce", "run", "stop", "dispatch", "store"):
        with pytest.raises(ControlError, match="unknown command"):
            asyncio.run(daemon.dispatch(cmd, {}))


def test_restart_stops_the_loop(daemon):
    async def body():
        result = await daemon.dispatch("restart", {})
        await asyncio.sleep(0.4)
        return result

    result = asyncio.run(body())

    assert result == {"restarting": True}
    assert daemon._stop.is_set()
    assert daemon._restart is True


# --- the permission self-check --------------------------------------------


def test_selfcheck_reports_what_the_daemon_can_do_from_where_it_runs(daemon, monkeypatch):
    """launchd is a different responsible process, so its answer is the one that counts."""
    from voiceloop import preflight

    monkeypatch.setattr(
        preflight,
        "run_all",
        lambda **kwargs: [preflight.Check("microphone", preflight.FAILED, "denied")],
    )

    assert asyncio.run(daemon.dispatch("selfcheck", {})) == [
        {"name": "microphone", "status": "failed", "detail": "denied"}
    ]


def test_a_planned_recognizer_disables_speech_instead_of_falling_back(config):
    """Silently using a different engine is how you debug the wrong accuracy."""
    config.data["speech_to_text"]["provider"] = "whisper-cpp"

    assert Daemon._build_stt(config) is None
