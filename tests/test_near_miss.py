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
