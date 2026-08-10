"""Asking voice-loop itself: "dame los pendientes" and "estado".

Every other spoken phrase is aimed at a window. These two are aimed at the
daemon, and they have to work from any mode — busy included, where the hotkey is
the only microphone you get.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from voiceloop.daemon import NO_PICK_SPOKEN, Daemon
from voiceloop.events import Event
from voiceloop.milestones import MilestoneWatcher
from voiceloop.store import STATE_PENDING, Store
from voiceloop.stt.mock import MockStt
from voiceloop.summarize import Summarizer

from conftest import TTY, FakeSpeaker, RecordingDelivery, StubRecorder, write_roster


class StubSummarizer(Summarizer):
    def __init__(self, answer: str = "quiere que revises el diff"):
        super().__init__(api_key="sk-test")
        self.answer = answer
        self.seen: list[str] = []

    def _call(self, text: str) -> str:
        self.seen.append(text)
        return self.answer


@pytest.fixture
def build(config, tmp_path):
    roster = tmp_path / "sessions"
    roster.mkdir()
    made: list[Daemon] = []

    def factory(replies, **kwargs):
        subject = Daemon(
            config,
            store=Store(tmp_path / f"queue{len(made)}.db"),
            speaker=FakeSpeaker(),
            summarizer=kwargs.pop("summarizer", None) or StubSummarizer(),
            watcher=kwargs.pop("watcher", None) or MilestoneWatcher(),
            roster_dir=roster,
            recorder=StubRecorder(),
            stt=MockStt(replies=list(replies), confidence=0.99),
            delivery=RecordingDelivery(),
            **kwargs,
        )
        subject.roster_path = roster
        made.append(subject)
        return subject

    try:
        yield factory
    finally:
        for subject in made:
            subject.store.close()


def transcript(tmp_path, name: str, tail: str = "Terminé. ¿Lo mergeo?") -> str:
    path = tmp_path / f"{name}.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "isSidechain": False,
                "message": {"role": "assistant", "content": [{"type": "text", "text": tail}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return str(path)


def waiting(daemon: Daemon, session: str, name: str, *, ts: int, summary: str | None = None):
    """A window that has already been announced and is still waiting on you."""
    write_roster(daemon.roster_path, sessionId=session, name=name, kind="interactive")
    event = Event.new("stop", session, ts=ts, tty=TTY, payload={})
    daemon.store.ingest(event)
    daemon.store.mark_pending(event.id, now=ts)
    if summary is not None:
        daemon.store.set_summary(event.id, summary)
    return event.id


def hotkey(daemon: Daemon) -> None:
    async def body():
        await daemon.dispatch("mic-toggle", {})
        for task in list(daemon._mic_tasks):
            await task

    asyncio.run(body())


def said(daemon: Daemon) -> str:
    return " ".join(daemon.speaker.spoken)


# --- reading the list ------------------------------------------------------


def test_the_list_is_read_oldest_first_with_name_summary_and_wait(build):
    daemon = build(["dame los pendientes", ""])
    now = int(time.time())
    waiting(daemon, "s1", "alpha", ts=now - 600, summary="espera tu aprobación")
    waiting(daemon, "s2", "beta", ts=now - 60, summary="terminó el backfill")

    hotkey(daemon)

    spoken = said(daemon)
    assert "Tenés dos pendientes" in spoken
    assert spoken.index("uno: alpha") < spoken.index("dos: beta")
    assert "espera tu aprobación" in spoken
    assert "hace diez minutos" in spoken
    assert "hace un minuto" in spoken


def test_an_empty_queue_says_so_and_asks_nothing(build):
    daemon = build(["qué tengo pendiente"])

    hotkey(daemon)

    assert "No tenés nada pendiente." in said(daemon)
    assert daemon.recorder.takes == 1  # the question itself, and no follow-up


def test_the_list_works_in_busy_mode(build):
    """Busy silences announcements, not the hotkey — that is the whole point."""
    daemon = build(["qué me falta", ""])
    daemon.busy = True
    waiting(daemon, "s1", "alpha", ts=1000, summary="espera tu aprobación")

    hotkey(daemon)

    assert "Tenés un pendiente" in said(daemon)


def test_a_missing_summary_is_computed_while_reading_the_list(build, tmp_path):
    """Issue #3, out loud: a name with nothing after it answers nothing."""
    daemon = build(["dame los pendientes", ""])
    event = Event.new("stop", "s1", ts=1000, tty=TTY, transcript_path=transcript(tmp_path, "s1"))
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    daemon.store.ingest(event)
    daemon.store.mark_pending(event.id, now=1000)

    hotkey(daemon)

    assert "quiere que revises el diff" in said(daemon)


# --- picking one off it ----------------------------------------------------


def test_a_window_picked_by_number_is_served_at_once_without_asking_twice(build):
    """You picked it by name off the list; being asked "dámelo" now is absurd."""
    daemon = build(["dame los pendientes", "la dos", "mergealo"])
    waiting(daemon, "s1", "alpha", ts=1000, summary="espera tu aprobación")
    waiting(daemon, "s2", "beta", ts=2000, summary="terminó el backfill")

    hotkey(daemon)

    assert daemon.speaker.spoken[-1] == "terminó el backfill."
    assert daemon.speaker.texts == []  # no second heads-up for the one you asked for
    assert daemon.delivery.sent == [("text", TTY, "mergealo")]


def test_a_window_picked_by_name_is_the_one_served(build):
    daemon = build(["dame los pendientes", "alpha", "mergealo"])
    waiting(daemon, "s1", "alpha", ts=1000, summary="espera tu aprobación")
    waiting(daemon, "s2", "beta", ts=2000, summary="terminó el backfill")

    hotkey(daemon)

    assert "espera tu aprobación." in daemon.speaker.spoken


def test_picking_nothing_leaves_the_queue_alone(build):
    daemon = build(["dame los pendientes", "no"])
    item = waiting(daemon, "s1", "alpha", ts=1000, summary="espera tu aprobación")

    hotkey(daemon)

    assert daemon.delivery.sent == []
    assert daemon.store.get(item).state == STATE_PENDING


def test_a_sentence_instead_of_a_pick_is_said_out_loud_not_swallowed(build):
    """The list takes twenty-five seconds; the silence after it reads as dead.

    A phrase that picks nothing goes nowhere — nothing downstream acts on it
    and nothing downstream says so — which is exactly when "I did not
    understand you" is indistinguishable from a daemon that stopped listening.
    """
    daemon = build(["dame los pendientes", "decile que lo deje fijo en el alias"])
    item = waiting(daemon, "s1", "alpha", ts=1000, summary="espera tu aprobación")

    hotkey(daemon)

    assert daemon.speaker.spoken[-1] == NO_PICK_SPOKEN
    assert daemon.delivery.sent == []
    assert daemon.store.get(item).state == STATE_PENDING


def test_saying_no_to_the_list_is_an_answer_and_is_not_argued_with(build):
    """"no" picked nothing on purpose. Telling you so is nagging, not feedback."""
    daemon = build(["dame los pendientes", "no"])
    waiting(daemon, "s1", "alpha", ts=1000, summary="espera tu aprobación")

    hotkey(daemon)

    assert NO_PICK_SPOKEN not in daemon.speaker.spoken


def test_the_list_can_be_asked_for_in_the_middle_of_answering(build):
    """And the window you pick takes over, with the one you left still pending."""
    daemon = build(["dame los pendientes", "la dos", "mergealo"])
    first = waiting(daemon, "s1", "alpha", ts=1000, summary="espera tu aprobación")
    waiting(daemon, "s2", "beta", ts=2000, summary="terminó el backfill")
    daemon.store.requeue(first)  # alpha is the one being announced

    asyncio.run(daemon.announce_next())

    assert daemon.speaker.texts == ["Nuevo evento de alpha."]
    assert "terminó el backfill." in daemon.speaker.spoken
    assert daemon.delivery.sent == [("text", TTY, "mergealo")]
    assert daemon.store.get(first).state == STATE_PENDING


# --- the state of the board ------------------------------------------------


def test_status_counts_windows_working_and_waiting(build):
    daemon = build(["estado"])
    write_roster(daemon.roster_path, sessionId="s1", name="alpha", status="busy")
    write_roster(daemon.roster_path, sessionId="s2", name="beta", status="idle")
    waiting(daemon, "s2", "beta", ts=1000, summary="terminó el backfill")

    hotkey(daemon)

    spoken = said(daemon)
    assert "Hay dos ventanas abiertas" in spoken
    assert "una trabajando" in spoken
    assert "una te espera" in spoken


def test_status_mentions_the_modes_you_are_in(build):
    daemon = build(["cómo venimos"])
    daemon.busy = True

    hotkey(daemon)

    assert "modo ocupado" in said(daemon)


def test_status_reports_milestones_when_that_bridge_is_on(build, tmp_path):
    phases = tmp_path / "phases"
    phases.mkdir()
    (phases / "a.phase").write_text("ci", encoding="utf-8")
    (phases / "b.phase").write_text("ci", encoding="utf-8")
    (phases / "c.phase").write_text("pr", encoding="utf-8")
    watcher = MilestoneWatcher(
        enabled=True, directory=phases, milestones={"ci": "CI green", "pr": "PR opened"}
    )
    daemon = build(["estado"], watcher=watcher)

    hotkey(daemon)

    spoken = said(daemon)
    assert "dos con CI green" in spoken
    assert "una con PR opened" in spoken


def test_status_says_nothing_about_milestones_when_the_bridge_is_off(build):
    daemon = build(["estado"])

    hotkey(daemon)

    assert "con CI" not in said(daemon)


# --- what a "después" costs -------------------------------------------------


def test_a_postponed_item_is_not_summarised_a_second_time(build, tmp_path):
    """The summary computed on arrival is still the summary an hour later."""
    daemon = build(["después", "dame los pendientes", "la uno", "mergealo"])
    write_roster(daemon.roster_path, sessionId="s1", name="alpha")
    daemon.store.ingest(
        Event.new("stop", "s1", ts=1000, tty=TTY, transcript_path=transcript(tmp_path, "s1"))
    )

    async def body():
        daemon.prefetch_summaries()
        for task in list(daemon._mic_tasks):
            await task
        await daemon.announce_next()  # heads-up, and "después"
        await daemon.dispatch("mic-toggle", {})  # ask for the list, pick it, answer it
        for task in list(daemon._mic_tasks):
            await task

    asyncio.run(body())

    assert daemon.store.pendings()[0].deferred_at is not None  # it really was postponed
    assert daemon.summarizer.seen == ["Terminé. ¿Lo mergeo?"]
    assert "quiere que revises el diff." in daemon.speaker.spoken
    assert daemon.delivery.sent == [("text", TTY, "mergealo")]


def test_the_hotkey_says_how_many_are_waiting_when_nothing_has_been_announced(build):
    """Busy announces nothing, so "nothing spoke last" says nothing about the queue."""
    daemon = build(["algo que no es un comando"])
    daemon.busy = True
    daemon.store.ingest(Event.new("stop", "s1", ts=1000, tty=TTY, payload={}))

    hotkey(daemon)

    assert "Tenés un pendiente." in said(daemon)
