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

from conftest import write_roster

LAUNCH = "Async agent launched successfully.\nagentId: aa11bb22"
DONE = "<task-notification>\n<task-id>aa11bb22</task-id>\n</task-notification>"


class FakeSpeaker:
    voice = "system"

    def __init__(self):
        self.said: list = []

    async def announce(self, announcement):
        self.said.append(announcement)

    @property
    def texts(self) -> list[str]:
        return [item.text for item in self.said if not item.silent]


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


def test_the_queue_is_announced_in_order_with_a_countdown(daemon, tmp_path):
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
        "alpha: quiere que revises el diff. Quedan 2.",
        "beta: quiere que revises el diff. Queda uno.",
        "gamma: quiere que revises el diff.",
    ]


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


@pytest.mark.parametrize("cmd", ["mic-toggle", "busy-toggle"])
def test_the_phase_two_commands_are_reserved_not_unknown(daemon, cmd):
    with pytest.raises(ControlError, match="not implemented"):
        asyncio.run(daemon.dispatch(cmd, {}))


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
