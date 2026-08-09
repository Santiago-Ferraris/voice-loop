"""When it is *nearly* a command: it asks, and it never types.

The regression this file exists for is one line of a real `daemon.log`:

    11:38:15  heard 'dame al pendiente' -> deliver text to /dev/ttys004

"dame los pendientes" came back one word wrong, matched nothing, and so was a
sentence — and a sentence is typed into whatever window is waiting. The most
expensive failure the loop had, and the cheapest to fix: ask.
"""

from __future__ import annotations

import asyncio

from voiceloop import intents
from voiceloop.announce import near_miss_question
from voiceloop.daemon import REPLY_DELIVERED, REPLY_PENDING
from voiceloop.store import STATE_PENDING

from conftest import TTY
from test_reply_cycle import QUESTION, answer, queue


# --- what counts as a near miss --------------------------------------------


def test_the_phrase_from_the_log_is_recognised_as_a_near_miss():
    guess = intents.nearest_control("dame al pendiente")

    assert guess is not None
    assert guess.kind == intents.KIND_PENDINGS
    assert guess.phrase in intents.PENDINGS_PHRASES


def test_a_phrase_that_matched_exactly_has_nothing_to_ask_about():
    assert intents.nearest_control("dame los pendientes") is None
    assert intents.nearest_control("dámelo") is None


def test_an_instruction_for_a_window_is_not_a_near_miss():
    """The whole point is that this stays rare — see MAX_NEAR_MISS_WORDS."""
    for said in (
        "mergealo cuando pasen los tests",
        "corré los tests de nuevo",
        "cerrá la ventana",
        "revisá el diff",
        "abrí el PR",
        "subilo a prod",
    ):
        assert intents.nearest_control(said) is None, said


def test_silence_is_not_a_near_miss():
    assert intents.nearest_control("") is None


def test_the_question_names_what_it_thinks_you_meant():
    assert near_miss_question("dame al pendiente", intents.KIND_PENDINGS) == (
        "Entendí: dame al pendiente. ¿Querés los pendientes?"
    )


def test_a_kind_with_nothing_to_offer_asks_nothing():
    assert near_miss_question("dame al pendiente", "text") == ""
    assert near_miss_question("", intents.KIND_PENDINGS) == ""


# --- and what the daemon does with it --------------------------------------


def test_a_near_miss_is_asked_about_and_never_typed(build):
    daemon = build(["dame al pendiente", "no"])
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_PENDING
    assert daemon.delivery.sent == []
    assert daemon.speaker.spoken[0] == (
        "Entendí: dame al pendiente. ¿Querés los pendientes?"
    )
    assert daemon.store.get(item).state == STATE_PENDING


def test_one_word_confirms_it_and_the_command_runs(build):
    daemon = build(["dame al pendiente", "dale", "", ""])
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_PENDING
    assert daemon.delivery.sent == []
    # The queue was read out, which is what "los pendientes" means.
    assert any("pendiente" in said.lower() for said in daemon.speaker.spoken[1:])


def test_saying_nothing_to_the_question_types_nothing_anywhere(build):
    daemon = build(["dame al pendiente", ""])
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_PENDING
    assert daemon.delivery.sent == []


def test_a_correction_replaces_it_the_way_any_read_back_does(build):
    daemon = build(["dame al pendiente", "mergealo cuando pasen los tests"])
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_DELIVERED
    assert daemon.delivery.sent == [("text", TTY, "mergealo cuando pasen los tests")]


def test_a_sentence_that_is_not_close_to_anything_still_goes_straight_through(build):
    """This is not confirm-everything. A dictated answer is still a dictated answer."""
    daemon = build(["mergealo cuando pasen los tests"])
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_DELIVERED
    assert daemon.delivery.sent == [("text", TTY, "mergealo cuando pasen los tests")]
    assert daemon.speaker.spoken == []


def test_a_near_miss_on_the_heads_up_is_asked_by_name_not_by_window(build):
    """"¿Te lo mando a la ventana?" is not the question when you plainly meant me."""
    daemon = build(["dame al pendiente", "no"])
    queue(daemon, QUESTION)

    asyncio.run(daemon.announce_next())

    assert daemon.delivery.sent == []
    assert daemon.speaker.spoken[0] == (
        "Entendí: dame al pendiente. ¿Querés los pendientes?"
    )


def test_confirming_it_on_the_heads_up_runs_the_command(build):
    daemon = build(["dame al pendiente", "dale", "", ""])
    queue(daemon, QUESTION)

    asyncio.run(daemon.announce_next())

    assert daemon.delivery.sent == []
    assert any("pendiente" in said.lower() for said in daemon.speaker.spoken[1:])


# --- what a real microphone in a real room actually hands over --------------
#
# Measured against the installed daemon, synthetic voice through the speakers:
# "dámelo" came back as 'jamelo' at confidence 0.75, and "dame los pendientes"
# as 'dame los pendins' at 0.70. Neither matched the lexicon and neither did
# anything. Imperfect transcription is the norm here, not the edge.


def test_the_real_mis_hearings_are_recognised_as_near_misses():
    assert intents.nearest_control("jamelo").kind == intents.KIND_GIVE
    assert intents.nearest_control("dame los pendins").kind == intents.KIND_PENDINGS


def test_jamelo_is_asked_about_and_a_yes_reads_the_summary(build):
    daemon = build(["jamelo", "dale", "mergealo"], confidence=0.75)
    queue(daemon, kind="stop")

    asyncio.run(daemon.announce_next())

    assert daemon.speaker.spoken[0] == "Entendí: jamelo. ¿Querés que te lo lea?"
    assert daemon.delivery.sent == [("text", TTY, "mergealo")]


def test_and_a_no_types_nothing_anywhere(build):
    daemon = build(["jamelo", "no"], confidence=0.75)
    item = queue(daemon, kind="stop")

    asyncio.run(daemon.announce_next())

    assert daemon.delivery.sent == []
    assert daemon.store.get(item).state == STATE_PENDING


def test_a_command_the_recogniser_itself_doubted_is_asked_about(build):
    """Low confidence is a signal even when the model read straight through it.

    The model can make sense of "jamelo" — often it should — but a command
    built out of words the recognizer scored at 0.70 is asked about, not run.
    """
    import json

    class Model:
        def __call__(self, url, headers, body, timeout):
            said = json.loads(body)["messages"][-1]["content"]
            del said
            return json.dumps(
                {
                    "choices": [
                        {"message": {"content": json.dumps({"actions": [{"intent": "give"}]})}}
                    ]
                }
            ).encode("utf-8")

    from voiceloop.classify import SOURCE_DOUBTFUL, Classifier

    daemon = build([], classifier=Classifier(api_key="sk-test", transport=Model()))

    async def plan_for(text, confidence):
        from voiceloop.stt import Transcript

        return await daemon.classify(Transcript(text=text, confidence=confidence))

    unsure = asyncio.run(plan_for("poné el flag nuevo", 0.70))
    sure = asyncio.run(plan_for("poné el flag nuevo", 0.99))

    assert unsure.source == SOURCE_DOUBTFUL
    assert unsure.guess.kind == intents.KIND_GIVE
    assert sure.guess is None


def test_the_confidence_line_is_inclusive(build):
    """0.75 is exactly what a bad take measured at, so 0.75 counts as unsure."""
    from voiceloop.stt import Transcript

    daemon = build([])

    assert daemon._unsure(Transcript(text="x", confidence=0.75)) is True
    assert daemon._unsure(Transcript(text="x", confidence=0.76)) is False
    assert daemon._unsure(Transcript(text="x", confidence=None)) is False


def test_contain_never_reaches_a_window_again(build):
    """The one the user saw appear in a window he was working in.

    "contame" came back as `'contain'` at **0.96** confidence — the recognizer
    was sure, and it was right: it heard a sound that really does resemble
    "contain". The error is semantic, so no confidence threshold catches it,
    and the old policy ("deliver unless the recognizer was unsure") delivered
    it. The policy is now the other way round.
    """
    daemon = build(["contain", "no"], confidence=0.96)
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_PENDING
    assert daemon.delivery.sent == []
    assert daemon.speaker.spoken[0] == "Entendí: contain. ¿Querés que te lo lea?"
    assert daemon.store.get(item).state == STATE_PENDING


def test_and_a_yes_gets_you_what_you_asked_for(build):
    daemon = build(["contain", "dale", "mergealo"], confidence=0.96)
    queue(daemon, kind="stop")

    asyncio.run(daemon.announce_next())

    assert daemon.delivery.sent == [("text", TTY, "mergealo")]


def test_how_close_it_has_to_be_depends_on_how_much_is_at_stake():
    """Short is a command said wrong; long is a sentence that happens to rhyme."""
    # One word, 0.71 similar — asked about.
    assert intents.nearest_control("contain") is not None
    # Five words, more similar than that in places — delivered.
    assert intents.nearest_control("mergealo cuando pasen los tests") is None
    assert intents.nearest_control("cerrá la ventana") is None


def test_a_yes_in_front_of_an_instruction_is_not_part_of_it():
    """"dale, mergealo" is three quarters of "dale damelo" for no good reason."""
    assert intents.nearest_control("dale mergealo") is None
    assert intents.nearest_control("mergealo") is None


def test_an_ordinary_short_instruction_is_still_delivered(build):
    daemon = build(["mergealo"], confidence=0.96)
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_DELIVERED
    assert daemon.delivery.sent == [("text", TTY, "mergealo")]
    assert daemon.speaker.spoken == []
