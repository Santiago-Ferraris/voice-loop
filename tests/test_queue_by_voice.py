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

from voiceloop import intents
from voiceloop.classify import Action
from voiceloop.daemon import NO_PICK_SPOKEN, WHICH_WINDOW_SPOKEN, Daemon
from voiceloop.events import Event
from voiceloop.milestones import MilestoneWatcher
from voiceloop.store import STATE_PENDING, Store
from voiceloop.stt.mock import MockStt
from voiceloop.summarize import Summarizer

from conftest import TTY, FakeSpeaker, RecordingDelivery, StubRecorder, write_roster
from test_echo import INSTRUCTION, PENDINGS_HEARD
from test_understanding import FakeModel, understanding


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


def waiting(
    daemon: Daemon,
    session: str,
    name: str,
    *,
    ts: int,
    summary: str | None = None,
    tty: str = TTY,
):
    """A window that has already been announced and is still waiting on you."""
    write_roster(daemon.roster_path, sessionId=session, name=name, kind="interactive")
    event = Event.new("stop", session, ts=ts, tty=tty, payload={})
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


# --- an instruction said over the list, instead of a pick off it ------------

TTY_E5 = "/dev/ttys021"
TTY_AUDIO = "/dev/ttys022"
TTY_E4 = "/dev/ttys023"

# What the model comes back with for the phrase that started all of this.
FIX_THE_MODEL = "dejá el modelo fijo en 4.8 en el alias con --model"


def the_three_pendings(daemon: Daemon) -> None:
    """The queue of 2026-08-09 21:42, verbatim — names, summaries and ages.

    Rebuilt exactly so the list the daemon reads out is the one in `test_echo`,
    which is what makes `PENDINGS_HEARD` a real take and not a fixture: the
    instruction on the end of it only survives if our own seventy-five words
    are subtracted first.
    """
    now = int(time.time())
    waiting(
        daemon, "s1", "darwin e5", ts=now - 8 * 3600, tty=TTY_E5,
        summary="Esperan que resuelva los conflictos y despliegue la rama en dev",
    )
    waiting(
        daemon, "s2", "cl audio", ts=now - 31 * 60, tty=TTY_AUDIO,
        summary="Esperan que pruebes con trabajo real y envíes el enter",
    )
    waiting(
        daemon, "s3", "darwin e4", ts=now - 24 * 60, tty=TTY_E4,
        summary="¿Querés que lo deje fijo en Opus 4.8 o investigue más?",
    )


def test_an_instruction_said_over_the_list_reaches_the_window_it_named(build):
    """The phrase this whole path exists for, from the log, end to end.

    "le dije en qué tab quería que haga las cosas y no hizo nada": the list was
    read, the instruction landed clean, and it resolved nothing because a pick
    is all the list could take. Now it goes to the classifier with the list
    under it, and "la última" is darwin e4.
    """
    model = FakeModel(
        {INSTRUCTION: [{"intent": "tell", "target": "darwin e4", "text": FIX_THE_MODEL}]}
    )
    daemon = build(
        ["dame los pendientes", PENDINGS_HEARD, "sí"],
        classifier=understanding(transport=model),
    )
    the_three_pendings(daemon)

    hotkey(daemon)

    assert model.asked == [INSTRUCTION]  # our own voice was subtracted first
    assert daemon.delivery.sent == [("text", TTY_E4, FIX_THE_MODEL)]


def test_the_list_that_was_just_read_is_the_context_the_model_gets(build):
    """"la última" is a position, and only the list says what it is a position in."""
    model = FakeModel(
        {INSTRUCTION: [{"intent": "tell", "target": "darwin e4", "text": FIX_THE_MODEL}]}
    )
    daemon = build(
        ["dame los pendientes", PENDINGS_HEARD, "sí"],
        classifier=understanding(transport=model),
    )
    the_three_pendings(daemon)

    hotkey(daemon)

    prompt = model.prompts[0]
    assert "1. darwin e5 — Esperan que resuelva los conflictos" in prompt
    assert "2. cl audio — Esperan que pruebes con trabajo real" in prompt
    assert "3. darwin e4 — ¿Querés que lo deje fijo en Opus 4.8" in prompt


def test_nothing_is_written_anywhere_until_it_has_been_read_back(build):
    model = FakeModel(
        {INSTRUCTION: [{"intent": "tell", "target": "darwin e4", "text": FIX_THE_MODEL}]}
    )
    daemon = build(
        ["dame los pendientes", PENDINGS_HEARD, "sí"],
        classifier=understanding(transport=model),
    )
    the_three_pendings(daemon)

    hotkey(daemon)

    assert f"Entendí: decile a darwin e4 que {FIX_THE_MODEL}. ¿Lo mando?" in (
        daemon.speaker.spoken
    )


def test_a_no_to_the_read_back_sends_nothing_anywhere(build):
    """The whole point of asking: the answer is allowed to be no."""
    model = FakeModel(
        {INSTRUCTION: [{"intent": "tell", "target": "darwin e4", "text": FIX_THE_MODEL}]}
    )
    daemon = build(
        ["dame los pendientes", PENDINGS_HEARD, "no"],
        classifier=understanding(transport=model),
    )
    the_three_pendings(daemon)

    hotkey(daemon)

    assert daemon.delivery.sent == []
    assert "Listo, no mando nada." in daemon.speaker.spoken


def test_saying_nothing_at_the_read_back_sends_nothing_either(build):
    """Silence is not a yes anywhere else in this daemon, and not here either."""
    model = FakeModel(
        {INSTRUCTION: [{"intent": "tell", "target": "darwin e4", "text": FIX_THE_MODEL}]}
    )
    daemon = build(
        ["dame los pendientes", PENDINGS_HEARD, ""],
        classifier=understanding(transport=model),
    )
    the_three_pendings(daemon)

    hotkey(daemon)

    assert daemon.delivery.sent == []


def test_a_window_named_inside_an_instruction_is_the_one_written_to(build):
    """Not a position — the name, said in the middle of a sentence."""
    said = "decile a darwin e5 que abandone el rebase"
    model = FakeModel(
        {said: [{"intent": "tell", "target": "darwin e5", "text": "abandoná el rebase"}]}
    )
    daemon = build(
        ["dame los pendientes", said, "dale"], classifier=understanding(transport=model)
    )
    the_three_pendings(daemon)

    hotkey(daemon)

    assert daemon.delivery.sent == [("text", TTY_E5, "abandoná el rebase")]


def test_a_window_that_is_not_on_the_list_is_asked_about_not_guessed(build):
    """Seven times out of ten right is three sentences a day in the wrong window."""
    said = "decile a la del índice que espere"
    model = FakeModel(
        {said: [{"intent": "tell", "target": "inbox realtime", "text": "esperá"}]}
    )
    daemon = build(
        ["dame los pendientes", said], classifier=understanding(transport=model)
    )
    the_three_pendings(daemon)

    hotkey(daemon)

    assert daemon.speaker.spoken[-1] == WHICH_WINDOW_SPOKEN
    assert daemon.delivery.sent == []


def test_a_reference_two_windows_could_equally_be_is_asked_about_too(build):
    said = "decile a darwin que espere"
    model = FakeModel({said: [{"intent": "tell", "target": "darwin", "text": "esperá"}]})
    daemon = build(
        ["dame los pendientes", said], classifier=understanding(transport=model)
    )
    the_three_pendings(daemon)

    hotkey(daemon)

    assert daemon.speaker.spoken[-1] == WHICH_WINDOW_SPOKEN
    assert daemon.delivery.sent == []


def test_an_instruction_that_names_no_window_at_all_asks_which_one(build):
    """The model says it is work for a session. Which session is the question."""
    said = "mergealo cuando pasen los tests"
    model = FakeModel({said: []})
    daemon = build(
        ["dame los pendientes", said], classifier=understanding(transport=model)
    )
    the_three_pendings(daemon)

    hotkey(daemon)

    assert daemon.speaker.spoken[-1] == WHICH_WINDOW_SPOKEN
    assert daemon.delivery.sent == []


def test_everything_one_breath_asked_for_happens_after_one_yes(build):
    said = "decile a la última que deje el modelo fijo y abrí una ventana nueva"
    model = FakeModel(
        {
            said: [
                {"intent": "tell", "target": "darwin e4", "text": "dejá el modelo fijo"},
                {"intent": "open", "text": "corré los tests"},
            ]
        }
    )
    daemon = build(
        ["dame los pendientes", said, "dale"], classifier=understanding(transport=model)
    )
    the_three_pendings(daemon)

    hotkey(daemon)

    assert daemon.delivery.sent == [("text", TTY_E4, "dejá el modelo fijo")]
    assert daemon.delivery.opened == [("", "corré los tests")]


def test_half_a_sentence_it_could_not_aim_does_none_of_the_sentence(build):
    """Doing the half you understood is how you learn which half it got wrong."""
    said = "decile a esa que espere y abrí una ventana nueva"
    model = FakeModel(
        {
            said: [
                {"intent": "tell", "target": "", "text": "esperá"},
                {"intent": "open", "text": "corré los tests"},
            ]
        }
    )
    daemon = build(
        ["dame los pendientes", said], classifier=understanding(transport=model)
    )
    the_three_pendings(daemon)

    hotkey(daemon)

    assert daemon.speaker.spoken[-1] == WHICH_WINDOW_SPOKEN
    assert daemon.delivery.sent == []
    assert daemon.delivery.opened == []


# --- a reference that points at nothing asks, it does not fan out -----------

# What the model actually came back with for "decile que haga eso", verbatim:
# one `tell` per pending window, each carrying that window's *summary* rewritten
# as an order. Not one of those three sentences was said out loud.
FABRICATED = [
    {
        "intent": "tell",
        "target": "darwin e5",
        "text": "resuelve los conflictos y despliega la rama en dev",
    },
    {
        "intent": "tell",
        "target": "cl audio",
        "text": "prueba con trabajo real y envía el Enter",
    },
    {
        "intent": "tell",
        "target": "darwin e4",
        "text": "dejalo fijo en Opus 4.8 o investiga más",
    },
]


def test_a_phrase_that_points_at_no_window_asks_instead_of_telling_all_three(build):
    """The failure this section exists for, from the log of 2026-08-09.

    Worse than the silence it replaced: silence does nothing, and this proposes
    three orders nobody dictated, one distracted "dale" away from three windows.
    """
    said = "decile que haga eso"
    model = FakeModel({said: FABRICATED})
    daemon = build(
        ["dame los pendientes", said], classifier=understanding(transport=model)
    )
    the_three_pendings(daemon)

    hotkey(daemon)

    assert daemon.speaker.spoken[-1] == WHICH_WINDOW_SPOKEN
    assert daemon.delivery.sent == []
    assert not any("Entendí" in spoken for spoken in daemon.speaker.spoken)


def test_the_same_message_to_every_window_is_a_fan_out_that_was_asked_for(build):
    """"decile a todas que paren" is deliberate, and what arrives is identical."""
    said = "decile a todas que paren"
    model = FakeModel(
        {
            said: [
                {"intent": "tell", "target": "darwin e5", "text": "pará"},
                {"intent": "tell", "target": "cl audio", "text": "pará"},
                {"intent": "tell", "target": "darwin e4", "text": "pará"},
            ]
        }
    )
    daemon = build(
        ["dame los pendientes", said, "dale"], classifier=understanding(transport=model)
    )
    the_three_pendings(daemon)

    hotkey(daemon)

    assert daemon.delivery.sent == [
        ("text", TTY_E5, "pará"),
        ("text", TTY_AUDIO, "pará"),
        ("text", TTY_E4, "pará"),
    ]


def test_two_windows_named_out_loud_get_the_two_things_they_were_told(build):
    """Different texts are fine when the phrase itself says who each one is for."""
    said = "decile a darwin e5 que corra los tests y a cl audio que espere"
    model = FakeModel(
        {
            said: [
                {"intent": "tell", "target": "darwin e5", "text": "corré los tests"},
                {"intent": "tell", "target": "cl audio", "text": "esperá"},
            ]
        }
    )
    daemon = build(
        ["dame los pendientes", said, "dale"], classifier=understanding(transport=model)
    )
    the_three_pendings(daemon)

    hotkey(daemon)

    assert daemon.delivery.sent == [
        ("text", TTY_E5, "corré los tests"),
        ("text", TTY_AUDIO, "esperá"),
    ]


def test_a_window_named_by_what_its_summary_says_is_the_one_written_to(build):
    """The third way to point at one: not the position, not the name, the reason."""
    said = "decile al de los conflictos que corra los tests"
    model = FakeModel(
        {said: [{"intent": "tell", "target": "darwin e5", "text": "corré los tests"}]}
    )
    daemon = build(
        ["dame los pendientes", said, "dale"], classifier=understanding(transport=model)
    )
    the_three_pendings(daemon)

    hotkey(daemon)

    assert daemon.delivery.sent == [("text", TTY_E5, "corré los tests")]


# --- and the same net, with no model in the picture at all ------------------

THREE_WINDOWS = ["darwin e5", "cl audio", "darwin e4"]


def three_tells(*texts: str) -> list[Action]:
    return [
        Action(kind=intents.KIND_TELL, target=name, text=text)
        for name, text in zip(THREE_WINDOWS, texts)
    ]


def test_one_phrase_split_into_three_different_messages_never_reaches_a_window(build):
    """The guard is in the code, not only in the prompt: no transport is asked."""
    daemon = build([])

    aimed, adrift = daemon._aim_over_pendings(
        three_tells("resolvé los conflictos", "probá con trabajo real", "dejalo fijo"),
        THREE_WINDOWS,
        "decile que haga eso",
    )

    assert aimed == []
    assert adrift is True


def test_one_phrase_repeated_to_three_windows_is_left_alone(build):
    daemon = build([])

    aimed, adrift = daemon._aim_over_pendings(
        three_tells("pará", "pará", "pará"),
        THREE_WINDOWS,
        "decile a todas que paren",
    )

    assert [action.index for action in aimed] == [1, 2, 3]
    assert adrift is False


def test_two_positions_said_out_loud_count_as_naming_them(build):
    """"la primera" and "la última" are references too, and they are in the phrase."""
    daemon = build([])

    aimed, adrift = daemon._aim_over_pendings(
        [
            Action(kind=intents.KIND_TELL, target="darwin e5", text="corré los tests"),
            Action(kind=intents.KIND_TELL, target="darwin e4", text="esperá"),
        ],
        THREE_WINDOWS,
        "decile a la primera que corra los tests y a la última que espere",
    )

    assert [action.index for action in aimed] == [1, 3]
    assert adrift is False


def test_a_model_that_never_answers_says_exactly_what_it_always_said(build):
    """The leash: with nobody to ask, this is the lexicon's own verdict."""

    class Dead:
        def __call__(self, url, headers, body, timeout):
            raise TimeoutError("timed out")

    daemon = build(
        ["dame los pendientes", "decile a la última que lo deje fijo"],
        classifier=understanding(transport=Dead()),
    )
    the_three_pendings(daemon)

    hotkey(daemon)

    assert daemon.speaker.spoken[-1] == NO_PICK_SPOKEN
    assert daemon.delivery.sent == []


# --- and what a pick still costs: nothing -----------------------------------


@pytest.mark.parametrize(
    "said, tty",
    [
        ("la dos", TTY_AUDIO),
        ("dame la última", TTY_E4),
        ("darwin e5", TTY_E5),
        ("la de darwin e4", TTY_E4),
    ],
)
def test_a_pick_off_the_list_costs_no_round_trip_and_no_read_back(build, said, tty):
    """A pick was never the ambiguous case, and nothing about it has changed."""
    model = FakeModel({})
    daemon = build(
        ["dame los pendientes", said, "mergealo"],
        classifier=understanding(transport=model),
    )
    the_three_pendings(daemon)

    hotkey(daemon)

    assert daemon.delivery.sent == [("text", tty, "mergealo")]
    assert said not in model.asked  # the lexicon resolved the pick, offline
    assert not any("Entendí" in spoken for spoken in daemon.speaker.spoken)


def test_focusing_a_window_off_the_list_changes_nothing_so_it_asks_nothing(build):
    """Read-backs are rationed: they are for what gets typed somewhere."""
    said = "llevame a la de darwin e4"
    model = FakeModel({said: [{"intent": "show", "target": "darwin e4"}]})
    daemon = build(
        ["dame los pendientes", said], classifier=understanding(transport=model)
    )
    the_three_pendings(daemon)

    hotkey(daemon)

    assert daemon.delivery.focused == [TTY_E4]
    assert not any("Entendí" in spoken for spoken in daemon.speaker.spoken)


def test_the_list_never_asks_itself_for_the_list(build):
    """A queue that can ask for the queue is a loop with a microphone in it."""
    said = "leeme la cola de vuelta"
    model = FakeModel({said: [{"intent": "pendings"}]})
    daemon = build(
        ["dame los pendientes", said], classifier=understanding(transport=model)
    )
    the_three_pendings(daemon)

    hotkey(daemon)

    assert daemon.speaker.spoken[-1] == NO_PICK_SPOKEN
    assert daemon.recorder.takes == 2  # the hotkey, and the list. Not a third


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
