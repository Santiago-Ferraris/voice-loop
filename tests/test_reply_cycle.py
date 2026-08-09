"""The loop closing: announce -> mic -> transcript -> intent -> delivery.

The microphone is a stub that always "hears something" and `MockStt` supplies
the words, so the whole path runs — including the real intent parser, the real
gate and the real keystroke builders — with nothing but the two ends faked.
`RecordingDelivery` captures what would have been typed into the window.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from voiceloop import iterm
from voiceloop.audio import AudioUnavailable, MicConsentPending, Recording
from voiceloop.daemon import REPLY_DELIVERED, REPLY_FAILED, REPLY_PENDING, Daemon
from voiceloop.events import Event
from voiceloop.milestones import MilestoneWatcher
from voiceloop.store import (
    STATE_DELIVERED,
    STATE_PENDING,
    STATE_RESOLVED,
    Store,
)
from voiceloop.stt.mock import MockStt

from conftest import FakeSpeaker, write_roster

TTY = "/dev/ttys012"

QUESTION = {
    "tool": "AskUserQuestion",
    "tool_input": {
        "questions": [
            {
                "question": "¿Qué base uso?",
                "options": [
                    {"label": "SQLite", "description": "Un archivo local."},
                    {"label": "Postgres", "description": "Ya corre en staging."},
                ],
            }
        ]
    },
}

MULTI = {
    "tool": "AskUserQuestion",
    "tool_input": {
        "questions": [
            {
                "question": "¿Qué frutas?",
                "multiSelect": True,
                "options": ["Pera", "Uva", "Kiwi"],
            }
        ]
    },
}

TWO_QUESTIONS = {
    "tool": "AskUserQuestion",
    "tool_input": {
        "questions": [
            {"question": "¿Base?", "options": ["SQLite", "Postgres"]},
            {"question": "¿Índice?", "options": ["Sí", "No"]},
        ]
    },
}

PLAN = {"tool": "ExitPlanMode", "tool_input": {"plan": "## Migrar el índice\n\n1. Nada\n"}}

# The live one, verbatim: four labels read whole are twenty-five seconds.
HOTKEYS = {
    "tool": "AskUserQuestion",
    "tool_input": {
        "questions": [
            {
                "question": "¿Cómo seguimos con las hotkeys?",
                "options": [
                    {
                        "label": "Probalo sin hotkeys primero (Recomendado)",
                        "description": "No hace falta tocar las Command Line Tools.",
                    },
                    {"label": "Actualizar Command Line Tools"},
                ],
            }
        ]
    },
}


class StubRecorder:
    """A mic that always captures something, unless told otherwise."""

    binary = "ffmpeg"
    device = ":0"
    available = True

    def __init__(self, *, spoke: bool = True, error: Exception | None = None):
        self.spoke = spoke
        self.error = error
        self.takes = 0

    async def record(self, destination, *, stop=None, on_open=None):
        self.takes += 1
        if self.error is not None:
            raise self.error
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0" * 200_000)
        if on_open is not None:
            await on_open()
        return Recording(path=path, seconds=1.0, spoke=self.spoke, reason="silence")


class RecordingDelivery:
    """Everything that would have gone into someone else's window."""

    def __init__(self, *, alive: bool = True):
        self._alive = alive
        self.sent: list[tuple] = []
        self.focused: list[str] = []

    def alive(self, tty: str) -> bool:
        return self._alive

    def _guard(self, tty: str) -> None:
        if not self._alive:
            raise iterm.SessionGone(f"no iTerm2 session on {tty}")

    def send_text(self, tty, text):
        self._guard(tty)
        self.sent.append(("text", tty, text))

    def send_choice(self, tty, index):
        self._guard(tty)
        self.sent.append(("choice", tty, index))

    def send_choices(self, tty, indexes):
        self._guard(tty)
        self.sent.append(("choices", tty, tuple(indexes)))

    def send_menu_text(self, tty, index, text):
        self._guard(tty)
        self.sent.append(("menu_text", tty, index, text))

    def focus(self, tty):
        self.focused.append(tty)
        return True


@pytest.fixture
def build(config, tmp_path):
    roster = tmp_path / "sessions"
    roster.mkdir()
    write_roster(roster, sessionId="session-1", name="indice", kind="interactive")
    made: list[Daemon] = []

    def factory(replies, *, recorder=None, delivery=None, confidence=0.99, **kwargs):
        engine = MockStt(replies=list(replies), confidence=confidence)
        subject = Daemon(
            config,
            store=Store(tmp_path / f"queue{len(made)}.db"),
            speaker=FakeSpeaker(),
            watcher=MilestoneWatcher(),
            roster_dir=roster,
            recorder=recorder or StubRecorder(),
            stt=engine,
            delivery=delivery or RecordingDelivery(),
            **kwargs,
        )
        made.append(subject)
        return subject

    try:
        yield factory
    finally:
        for subject in made:
            subject.store.close()


def queue(daemon: Daemon, payload=None, *, kind="menu", session="session-1") -> str:
    event = Event.new(kind, session, tty=TTY, payload=payload or {})
    daemon.store.ingest(event)
    return event.id


def answer(daemon: Daemon, item_id: str) -> str:
    return asyncio.run(daemon.reply_cycle(daemon.store.get(item_id)))


# --- the happy paths -------------------------------------------------------


def test_a_dictated_answer_is_typed_into_that_window(build):
    daemon = build(["mergealo cuando pasen los tests"])
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_DELIVERED
    assert daemon.delivery.sent == [("text", TTY, "mergealo cuando pasen los tests")]


def test_a_spoken_number_becomes_arrow_keys_on_that_menu(build):
    daemon = build(["la dos"])
    item = queue(daemon, QUESTION)

    assert answer(daemon, item) == REPLY_DELIVERED
    assert daemon.delivery.sent == [("choice", TTY, 2)]


def test_a_spoken_keyword_picks_the_option_too(build):
    daemon = build(["postgres"])
    item = queue(daemon, QUESTION)

    answer(daemon, item)

    assert daemon.delivery.sent == [("choice", TTY, 2)]


def test_several_options_at_once_on_a_multi_select_menu(build):
    daemon = build(["pera y kiwi"])
    item = queue(daemon, MULTI)

    answer(daemon, item)

    assert daemon.delivery.sent == [("choices", TTY, (1, 3))]


def test_a_plan_is_approved_by_number(build):
    daemon = build(["uno"])
    item = queue(daemon, PLAN)

    answer(daemon, item)

    assert daemon.delivery.sent == [("choice", TTY, 1)]


def test_free_text_on_a_plan_goes_to_the_row_that_takes_feedback(build):
    daemon = build(["sumale un paso que corra los tests"])
    item = queue(daemon, PLAN)

    answer(daemon, item)

    assert daemon.delivery.sent == [
        ("menu_text", TTY, 4, "sumale un paso que corra los tests")
    ]


def test_free_text_on_a_question_goes_to_the_row_past_the_options(build):
    """The payload has two options, so "Type something." is row three."""
    daemon = build(["ninguna de las dos, usá duckdb"])
    item = queue(daemon, QUESTION)

    answer(daemon, item)

    assert daemon.delivery.sent == [("menu_text", TTY, 3, "ninguna de las dos, usá duckdb")]


def test_an_option_is_read_short_and_answered_by_what_was_heard(build):
    """Shortening the labels must not put the answer out of reach."""
    daemon = build(["probalo sin hotkeys primero"])
    queue(daemon, HOTKEYS)

    asyncio.run(daemon.announce_next())

    assert "Recomendado" not in daemon.speaker.texts[0]
    assert daemon.delivery.sent == [("choice", TTY, 1)]


def test_the_full_label_is_still_there_for_anyone_who_asks(build):
    daemon = build(["explicame la uno", "uno"])
    item = queue(daemon, HOTKEYS)

    answer(daemon, item)

    assert "No hace falta tocar las Command Line Tools." in daemon.speaker.spoken


def test_each_question_of_a_multi_question_menu_is_asked_in_turn(build):
    daemon = build(["uno", "dos"])
    item = queue(daemon, TWO_QUESTIONS)

    assert answer(daemon, item) == REPLY_DELIVERED
    assert daemon.delivery.sent == [("choice", TTY, 1), ("choice", TTY, 2)]
    assert any("Pregunta dos de dos" in said for said in daemon.speaker.spoken)


# --- the states it leaves behind -------------------------------------------


def test_a_delivered_item_waits_for_the_session_to_confirm_it_landed(build):
    """`delivered` is not resolved: the injected turn's own activity does that."""
    daemon = build(["dale, mergealo"])
    item = queue(daemon, kind="stop")

    answer(daemon, item)

    assert daemon.store.get(item).state == STATE_DELIVERED


def test_the_activity_the_injection_triggers_is_what_resolves_it(build):
    daemon = build(["dale, mergealo"])
    item = queue(daemon, kind="stop")
    answer(daemon, item)

    daemon.store.ingest(Event.new("activity", "session-1"))

    assert daemon.store.get(item).state == STATE_RESOLVED


def test_saying_nothing_leaves_the_item_pending_and_moves_on(build):
    daemon = build([], recorder=StubRecorder(spoke=False))
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_PENDING
    assert daemon.store.get(item).state == STATE_PENDING
    assert daemon.delivery.sent == []


def test_skipping_leaves_the_item_pending_too(build):
    daemon = build(["después"])
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_PENDING
    assert daemon.store.get(item).state == STATE_PENDING


def test_an_unanswered_second_question_keeps_the_whole_item_pending(build):
    """That window is still blocking, however much of the menu we got through."""
    daemon = build(["uno"], recorder=StubRecorder())
    daemon.stt.replies = ["uno"]
    daemon.stt.default = ""
    item = queue(daemon, TWO_QUESTIONS)

    assert answer(daemon, item) == REPLY_PENDING
    assert daemon.store.get(item).state == STATE_PENDING


# --- the confirmation gate -------------------------------------------------


def test_a_destructive_phrase_is_read_back_before_it_is_sent(build):
    daemon = build(["borrá la base en prod", "dale"])
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_DELIVERED
    assert daemon.delivery.sent == [("text", TTY, "borrá la base en prod")]
    assert any("¿Lo mando?" in said for said in daemon.speaker.spoken)


def test_a_read_back_that_is_cancelled_sends_nothing(build):
    daemon = build(["borrá la base en prod", "no"])
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_PENDING
    assert daemon.delivery.sent == []


def test_a_low_confidence_transcript_is_read_back(build):
    daemon = build(["mergealo", "dale"], confidence=0.4)
    item = queue(daemon, kind="stop")

    answer(daemon, item)

    assert any("No te escuché bien" in said for said in daemon.speaker.spoken)
    assert daemon.delivery.sent == [("text", TTY, "mergealo")]


def test_a_low_confidence_menu_answer_is_read_back_with_the_label(build):
    daemon = build(["dos", "dale"], confidence=0.4)
    item = queue(daemon, QUESTION)

    answer(daemon, item)

    assert any("opción dos: Postgres" in said for said in daemon.speaker.spoken)
    assert daemon.delivery.sent == [("choice", TTY, 2)]


def test_saying_something_else_during_a_read_back_replaces_it(build):
    daemon = build(["borrá prod", "mejor corré los tests"])
    item = queue(daemon, kind="stop")

    answer(daemon, item)

    assert daemon.delivery.sent == [("text", TTY, "mejor corré los tests")]


# --- the conversational bits -----------------------------------------------


def test_asking_for_a_repeat_says_it_again_and_keeps_listening(build):
    daemon = build(["repetí", "dale, mergealo"])
    item = queue(daemon, kind="stop")

    outcome = asyncio.run(
        daemon.reply_cycle(daemon.store.get(item), "indice: terminó y te espera.")
    )

    assert outcome == REPLY_DELIVERED
    assert "indice: terminó y te espera." in daemon.speaker.spoken
    assert daemon.delivery.sent == [("text", TTY, "dale, mergealo")]


def test_mostrame_focuses_the_window_without_answering_it(build):
    daemon = build(["mostrame", "la uno"])
    item = queue(daemon, QUESTION)

    answer(daemon, item)

    assert daemon.delivery.focused == [TTY]
    assert daemon.delivery.sent == [("choice", TTY, 1)]


def test_asking_for_detail_reads_the_option_description(build):
    daemon = build(["explicame la dos", "dos"])
    item = queue(daemon, QUESTION)

    answer(daemon, item)

    assert "Ya corre en staging." in daemon.speaker.spoken


def test_a_bare_yes_on_a_menu_asks_which_one_instead_of_guessing(build):
    """Picking option 1 for you is how a menu answers itself wrong."""
    daemon = build(["dale", "dos"])
    item = queue(daemon, QUESTION)

    answer(daemon, item)

    assert "¿Cuál opción?" in daemon.speaker.spoken
    assert daemon.delivery.sent == [("choice", TTY, 2)]


def test_the_mic_gives_up_after_a_few_rounds(build):
    daemon = build(["repetí", "repetí", "repetí", "repetí", "dos"])
    item = queue(daemon, QUESTION)

    assert answer(daemon, item) == REPLY_PENDING
    assert daemon.delivery.sent == []
    assert daemon.recorder.takes == daemon.mic_rounds


# --- when things break -----------------------------------------------------


def test_a_window_that_closed_is_never_typed_into(build):
    daemon = build(["mergealo"], delivery=RecordingDelivery(alive=False))
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_FAILED
    assert daemon.store.get(item).state == STATE_PENDING
    assert daemon.delivery.sent == []


def test_a_broken_microphone_says_so_and_leaves_the_item_alone(build):
    daemon = build([], recorder=StubRecorder(error=AudioUnavailable("no device")))
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_FAILED
    assert daemon.store.get(item).state == STATE_PENDING
    assert any("micrófono" in said for said in daemon.speaker.spoken)


def test_a_mic_waiting_on_consent_says_what_to_do_about_it(build):
    """Issue #7: the user is not looking at a terminal — that is the premise."""
    daemon = build([], recorder=StubRecorder(error=MicConsentPending("did not open :0")))
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_FAILED
    said = " ".join(daemon.speaker.spoken)
    assert "permiso de micrófono" in said
    assert "doctor" in said


def test_an_item_with_no_tty_never_opens_the_mic(build):
    daemon = build(["mergealo"])
    event = Event.new("stop", "session-1", tty="", payload={})
    daemon.store.ingest(event)

    assert asyncio.run(daemon.reply_cycle(daemon.store.get(event.id))) == REPLY_PENDING
    assert daemon.recorder.takes == 0


def test_the_recording_is_deleted_once_it_has_been_transcribed(build, config):
    daemon = build(["mergealo"])
    item = queue(daemon, kind="stop")

    answer(daemon, item)

    assert list((config.state_dir / "mic").glob("*.wav")) == []


def test_recordings_can_be_kept_for_debugging(build, config):
    daemon = build(["mergealo"])
    daemon.keep_recordings = True
    item = queue(daemon, kind="stop")

    answer(daemon, item)

    assert len(list((config.state_dir / "mic").glob("*.wav"))) == 1


# --- what the recognizer is told exists ------------------------------------


def test_the_live_session_names_are_sent_as_vocabulary(build):
    daemon = build(["mergealo"])
    item = queue(daemon, kind="stop")

    answer(daemon, item)

    _, keyterms = daemon.stt.calls[-1]
    assert "indice" in keyterms  # the live window's own name
    assert "rate-limiter" in keyterms  # and the configured vocabulary


def test_names_you_gave_a_window_yourself_are_vocabulary_too(build):
    daemon = build(["mergealo"])
    daemon.store.set_alias("session-1", "el del índice")
    item = queue(daemon, kind="stop")

    answer(daemon, item)

    assert "el del índice" in daemon.stt.calls[-1][1]


# --- the loop, end to end --------------------------------------------------


def test_announcing_an_item_opens_the_mic_on_it_and_delivers_the_answer(build):
    """The whole phase: announce, listen, transcribe, route, type."""
    daemon = build(["la dos"])
    queue(daemon, QUESTION)

    assert asyncio.run(daemon.announce_next()) is True

    assert daemon.speaker.texts == [
        "indice: ¿Qué base uso? Opciones: uno: SQLite, dos: Postgres."
    ]
    assert daemon.recorder.takes == 1
    assert daemon.delivery.sent == [("choice", TTY, 2)]


def test_the_mic_chimes_open_and_closed_around_the_take(build):
    daemon = build(["la dos"])
    queue(daemon, QUESTION)

    asyncio.run(daemon.announce_next())

    assert daemon.speaker.chimes == ["Tink", "Pop"]


def test_busy_mode_chimes_and_does_not_open_the_mic(build):
    daemon = build(["la dos"])
    daemon.busy = True
    queue(daemon, QUESTION)

    assert asyncio.run(daemon.announce_next()) is True

    assert daemon.speaker.texts == []
    assert daemon.recorder.takes == 0
    assert daemon.store.get(daemon.store.pendings()[0].id).state == STATE_PENDING


def test_a_milestone_never_opens_the_mic(build):
    daemon = build(["la dos"])
    queue(daemon, {"label": "PR created"}, kind="milestone", session="")

    asyncio.run(daemon.announce_next())

    assert daemon.recorder.takes == 0


def test_the_hotkey_answers_whatever_spoke_last(build):
    daemon = build(["la dos"], recorder=StubRecorder(spoke=False))
    queue(daemon, QUESTION)
    asyncio.run(daemon.announce_next())  # heard nothing; item is pending
    daemon.recorder.spoke = True
    daemon.stt.replies = ["la dos"]

    async def body():
        assert await daemon.dispatch("mic-toggle", {}) == {"mic": "opening"}
        for task in list(daemon._mic_tasks):
            await task

    asyncio.run(body())

    assert daemon.delivery.sent == [("choice", TTY, 2)]


def test_the_hotkey_says_so_when_there_is_nothing_pending(build):
    daemon = build([])

    async def body():
        await daemon.dispatch("mic-toggle", {})
        for task in list(daemon._mic_tasks):
            await task

    asyncio.run(body())

    assert "No hay nada pendiente." in daemon.speaker.spoken


def test_the_hotkey_closes_a_mic_that_is_already_open(build):
    daemon = build([])
    daemon._mic_stop = asyncio.Event()

    assert asyncio.run(daemon.dispatch("mic-toggle", {})) == {"mic": "closing"}
    assert daemon._mic_stop.is_set()


def test_status_reports_the_recognizer_and_the_mic(build):
    daemon = build([])

    status = asyncio.run(daemon.dispatch("status", {}))

    assert status["speech_to_text"] == "mock"
    assert status["mic"] == "idle"
    assert status["busy"] is False
