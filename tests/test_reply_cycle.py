"""The loop closing: announce -> mic -> transcript -> intent -> delivery.

The microphone is a stub that always "hears something" and `MockStt` supplies
the words, so the whole path runs — including the real intent parser, the real
gate and the real keystroke builders — with nothing but the two ends faked.
`RecordingDelivery` captures what would have been typed into the window.
"""

from __future__ import annotations

import asyncio

from voiceloop.announce import Announcement
from voiceloop.audio import AudioUnavailable, MicConsentPending
from voiceloop.daemon import REPLY_DELIVERED, REPLY_FAILED, REPLY_PENDING, Daemon
from voiceloop.events import Event
from voiceloop.store import (
    STATE_DELIVERED,
    STATE_PENDING,
    STATE_QUEUED,
    STATE_RESOLVED,
)
from voiceloop.tts import Speaker

from conftest import (
    TTY,
    RecordingDelivery,
    StubRecorder,
    TimedRecorder,
    TimedRunner,
    chime_file,
    write_roster,
)

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


EXPRESSION = {
    "tool": "AskUserQuestion",
    "tool_input": {
        "questions": [
            {
                "question": "¿Cómo mando el atajo?",
                "options": [
                    {"label": "Usá el modo A / B con el flag nuevo"},
                    {"label": "Dejalo como está"},
                ],
            }
        ]
    },
}


def queue(daemon: Daemon, payload=None, *, kind="menu", session="session-1", ts=None) -> str:
    event = Event.new(kind, session, tty=TTY, payload=payload or {}, **({"ts": ts} if ts else {}))
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


def test_a_keyword_from_the_half_that_is_never_spoken_still_picks_it(build):
    """Options are read short; the *full* label is still what is matched."""
    daemon = build(["flag"])
    item = queue(daemon, EXPRESSION)

    answer(daemon, item)

    assert daemon.delivery.sent == [("choice", TTY, 1)]


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
    daemon = build(["dámelo", "probalo sin hotkeys primero"])
    queue(daemon, HOTKEYS)

    asyncio.run(daemon.announce_next())

    assert "Recomendado" not in daemon.speaker.spoken[0]
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


def test_a_question_that_might_have_been_for_voice_loop_is_read_back(build):
    """Asking costs a round; typing it into somebody's session costs the session."""
    daemon = build(["cuántas sesiones tengo esperando ahora", "no"])
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_PENDING
    assert daemon.delivery.sent == []
    assert any("¿Te lo mando a la ventana?" in said for said in daemon.speaker.spoken)


def test_and_it_goes_through_if_you_say_it_was_for_the_window(build):
    daemon = build(["cuántas sesiones tengo esperando ahora", "dale"])
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_DELIVERED
    assert daemon.delivery.sent == [("text", TTY, "cuántas sesiones tengo esperando ahora")]


def test_an_ordinary_question_for_the_window_is_not_second_guessed(build):
    """The read-back is for doubt, not for questions — this is not confirm-everything."""
    daemon = build(["qué base te parece mejor"])
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_DELIVERED
    assert daemon.delivery.sent == [("text", TTY, "qué base te parece mejor")]
    assert daemon.speaker.spoken == []


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


def test_asking_which_one_is_left_reads_the_queue_instead_of_typing_it(build):
    """Verbatim from the first real run: "cuál queda" was typed into the window."""
    daemon = build(["cuál queda"])
    daemon.stt.default = ""
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_PENDING
    assert daemon.delivery.sent == []
    assert any("pendiente" in said for said in daemon.speaker.spoken)


def test_asking_for_a_beat_holds_the_mic_instead_of_answering(build):
    daemon = build(["esperá", "mergealo"])
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_DELIVERED
    assert daemon.delivery.sent == [("text", TTY, "mergealo")]
    assert "Dale, espero." in daemon.speaker.spoken


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
    """The whole phase: heads-up, "dámelo", listen, transcribe, route, type."""
    daemon = build(["dámelo", "la dos"])
    queue(daemon, QUESTION)

    assert asyncio.run(daemon.announce_next()) is True

    assert daemon.speaker.texts == ["Nuevo evento de indice."]
    assert daemon.speaker.spoken[0] == "¿Qué base uso? Opciones: uno: SQLite, dos: Postgres."
    assert daemon.recorder.takes == 2
    assert daemon.delivery.sent == [("choice", TTY, 2)]


def test_the_heads_up_says_which_window_and_not_what_it_wants(build):
    """The regression the whole flow is about: a name, and then a mic."""
    daemon = build([], recorder=StubRecorder(spoke=False))
    queue(daemon, QUESTION)

    asyncio.run(daemon.announce_next())

    assert daemon.speaker.texts == ["Nuevo evento de indice."]
    assert daemon.speaker.spoken == []
    assert daemon.recorder.takes == 1
    assert daemon.delivery.sent == []


def test_the_heads_up_mic_opens_with_the_chime_and_outlives_the_sentence(build):
    """The v3 microphone: no window to catch, a grace period to finish in.

    The old shape opened four seconds *after* the announcement, which is how a
    morning produced three "mic heard nothing" in a row — the mic had opened
    and shut again before anyone got a word out.
    """
    daemon = build(["dámelo", "la dos"])
    queue(daemon, QUESTION)

    asyncio.run(daemon.announce_next())

    # Both takes ran under a voice, so both get the grace and not the timeout.
    assert daemon.recorder.windows == [daemon.mic_grace, daemon.mic_grace]
    assert daemon.mic_grace == 10
    # And both ignored what they heard until the voice stopped: on speakers
    # every syllable of it comes back down the microphone.
    assert daemon.recorder.armings == [True, True]


def test_three_windows_blocking_at_once_get_three_heads_ups(build):
    """One after another, each with its own mic. Nothing is grouped."""
    daemon = build([], recorder=StubRecorder(spoke=False))
    for index, session in enumerate(("session-1", "session-2", "session-3")):
        write_roster(daemon.roster_dir, sessionId=session, name=f"win-{index}")
        queue(daemon, QUESTION, session=session)

    async def drain():
        while await daemon.announce_next():
            pass

    asyncio.run(drain())

    assert daemon.speaker.texts == [
        "Nuevo evento de win 0.",
        "Nuevo evento de win 1.",
        "Nuevo evento de win 2.",
    ]
    assert daemon.recorder.takes == 3


def test_asking_for_it_reads_it_out_and_opens_the_mic_to_answer(build):
    daemon = build(["dámelo", "mergealo"])
    queue(daemon, kind="stop")

    asyncio.run(daemon.announce_next())

    assert daemon.delivery.sent == [("text", TTY, "mergealo")]


def test_after_is_the_back_of_the_line_and_says_nothing_about_the_item(build):
    daemon = build(["después"])
    first = queue(daemon, kind="stop", ts=1000)
    write_roster(daemon.roster_dir, sessionId="session-2", name="beta")
    second = queue(daemon, QUESTION, session="session-2", ts=1001)

    asyncio.run(daemon.announce_next())

    assert daemon.speaker.spoken == []  # not one word about what it wanted
    assert daemon.delivery.sent == []
    assert [item.id for item in daemon.store.pendings()] == [second, first]


def test_saying_nothing_leaves_it_in_the_queue_and_closes_the_mic(build):
    """No reminder, ever: it waits there until you ask for it."""
    daemon = build([], recorder=StubRecorder(spoke=False))
    item = queue(daemon, kind="stop")

    asyncio.run(daemon.announce_next())

    assert daemon.store.get(item).state == STATE_PENDING
    assert daemon.store.get(item).deferred_at is None
    assert daemon.recorder.takes == 1
    assert asyncio.run(daemon.announce_next()) is False  # and it is not announced again


def test_a_sentence_at_the_heads_up_is_asked_about_not_delivered(build):
    """Nothing has been read out yet, so a sentence here may not be for anyone."""
    daemon = build(["mergealo cuando pasen los tests", "no"])
    queue(daemon, kind="stop")

    asyncio.run(daemon.announce_next())

    assert any("¿Te lo mando a la ventana?" in said for said in daemon.speaker.spoken)
    assert daemon.delivery.sent == []


def test_and_it_goes_through_once_you_say_it_was(build):
    daemon = build(["mergealo cuando pasen los tests", "dale"])
    queue(daemon, kind="stop")

    asyncio.run(daemon.announce_next())

    assert daemon.delivery.sent == [("text", TTY, "mergealo cuando pasen los tests")]


def test_what_is_left_is_said_when_the_cycle_closes_not_when_it_opens(build):
    """You answer, *then* you hear how many are left — the heads-up is not it."""
    daemon = build(["dámelo", "la dos"])
    queue(daemon, QUESTION)
    write_roster(daemon.roster_dir, sessionId="session-2", name="beta")
    queue(daemon, QUESTION, session="session-2")

    asyncio.run(daemon.announce_next())

    assert "Queda" not in daemon.speaker.texts[0]
    assert daemon.speaker.spoken[-1] == "Queda uno."


def test_an_empty_queue_counts_down_to_nothing(build):
    daemon = build(["dámelo", "la dos"])
    queue(daemon, QUESTION)

    asyncio.run(daemon.announce_next())

    assert not any("Queda" in said for said in daemon.speaker.spoken)


def test_nothing_is_counted_down_when_nobody_answered(build):
    daemon = build([], recorder=StubRecorder(spoke=False))
    queue(daemon, QUESTION)
    write_roster(daemon.roster_dir, sessionId="session-2", name="beta")
    queue(daemon, QUESTION, session="session-2")

    asyncio.run(daemon.announce_next())

    assert not any("Queda" in said for said in daemon.speaker.spoken)


def test_the_heads_up_chime_is_the_mic_opening_and_the_close_has_its_own(build):
    """One chime opens the take; a different one says it is over.

    The announcement's chime no longer means "your turn now" — the take is
    already running when it rings — so the only cue left that the mic has shut
    is the closing one, and it has to be a sound of its own. Closing in
    silence is the "no sé cuándo tengo que hablar" complaint, verbatim.
    """
    daemon = build([], recorder=StubRecorder(spoke=False))
    queue(daemon, QUESTION)

    asyncio.run(daemon.announce_next())

    assert daemon.speaker.chimes == ["Pop"]
    assert [item.chime for item in daemon.speaker.said] == ["Ping"]
    assert daemon.mic_open_chime != daemon.mic_close_chime


def test_the_mic_does_not_open_under_a_voice_that_is_still_talking(build, tmp_path):
    """The hotkey opens a mic from a task of its own — mid-announcement, if it can.

    Which recorded the announcement into the take, and left the "speak now"
    chime queued behind the very sentence it was supposed to follow: no cue,
    and a window to answer in that was spent on audio nobody could talk over.
    """
    runner = TimedRunner({"say": 0.15})
    speaker = Speaker(voice="system", runner=runner, chime_head=0.0)
    recorder = TimedRecorder()
    daemon = build(["la dos"], recorder=recorder, speaker=speaker)
    daemon.mic_open_chime = chime_file(tmp_path, "tink")
    daemon.mic_close_chime = None

    async def scenario():
        talking = asyncio.ensure_future(
            speaker.announce(Announcement(text="otra ventana habla", chime=None))
        )
        await asyncio.sleep(0.01)  # the announcement has the floor
        await daemon.listen()
        await talking

    asyncio.run(scenario())

    voice, = runner.spans_of("say")
    assert recorder.started_at >= voice[2]
    assert [span[0] for span in runner.spans] == ["say", "afplay"]


def test_busy_mode_is_silent_and_lets_the_queue_pile_up(build):
    """Not "a chime instead of the words" — that was the same interruption."""
    daemon = build(["la dos"])
    daemon.busy = True
    queue(daemon, QUESTION)

    assert asyncio.run(daemon.announce_next()) is False

    assert daemon.speaker.said == []
    assert daemon.speaker.chimes == []
    assert daemon.recorder.takes == 0
    assert daemon.store.get(daemon.store.pendings()[0].id).state == STATE_QUEUED


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

    assert any("No hay nada pendiente." in said for said in daemon.speaker.spoken)


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


# --- busy mode: silence, and what it cost -----------------------------------


def test_everything_that_arrives_in_busy_mode_waits_in_the_queue(build):
    daemon = build([], recorder=StubRecorder(spoke=False))
    daemon.busy = True
    queue(daemon, QUESTION, ts=1000)
    write_roster(daemon.roster_dir, sessionId="session-2", name="beta")
    queue(daemon, QUESTION, session="session-2", ts=1001)

    async def drain():
        while await daemon.announce_next():
            pass

    asyncio.run(drain())

    assert daemon.store.queued_count() == 2
    assert daemon.speaker.said == []
    assert daemon.speaker.chimes == []


def test_and_arrives_all_at_once_when_you_come_back_out(build):
    daemon = build([], recorder=StubRecorder(spoke=False))
    daemon.busy = True
    queue(daemon, QUESTION, ts=1000)
    write_roster(daemon.roster_dir, sessionId="session-2", name="beta")
    queue(daemon, QUESTION, session="session-2", ts=1001)

    async def body():
        await daemon.announce_next()
        await daemon.dispatch("busy-toggle", {})
        while await daemon.announce_next():
            pass

    asyncio.run(body())

    assert daemon.speaker.texts == ["Nuevo evento de indice.", "Nuevo evento de beta."]


def test_the_toggle_says_which_mode_it_left_you_in(build):
    """A chime sounds the same in both directions, which is no answer at all."""
    daemon = build([])

    asyncio.run(daemon.dispatch("busy-toggle", {}))
    assert daemon.speaker.spoken == ["Ocupado."]

    asyncio.run(daemon.dispatch("busy-toggle", {}))
    assert daemon.speaker.spoken[-1] == "Te escucho."


def test_leaving_busy_mode_says_how_much_piled_up(build):
    daemon = build([])
    daemon.busy = True
    queue(daemon, QUESTION, ts=1000)
    write_roster(daemon.roster_dir, sessionId="session-2", name="beta")
    queue(daemon, QUESTION, session="session-2", ts=1001)

    asyncio.run(daemon.dispatch("busy-toggle", {}))

    assert daemon.speaker.spoken[-1] == "Te escucho. Tenés dos pendientes."


def test_the_hotkey_still_opens_the_mic_in_busy_mode(build):
    """"I am in a meeting" and "I cannot answer you" are different things."""
    daemon = build(["la dos"])
    daemon.busy = True
    queue(daemon, QUESTION)
    daemon.store.mark_pending(daemon.store.pendings()[0].id)

    async def body():
        await daemon.dispatch("mic-toggle", {})
        for task in list(daemon._mic_tasks):
            await task

    asyncio.run(body())

    assert daemon.delivery.sent == [("choice", TTY, 2)]


def test_without_a_recognizer_the_summary_comes_anyway(build):
    """The heads-up only works because "dámelo" is available. With no mic it is not."""
    daemon = build([])
    daemon.stt = None
    queue(daemon, kind="stop")

    asyncio.run(daemon.announce_next())

    assert daemon.speaker.texts == ["Nuevo evento de indice."]
    assert daemon.speaker.spoken == ["terminó y te espera."]
    assert daemon.recorder.takes == 0
