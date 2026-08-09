"""Telling our own voice apart from yours in a transcript.

Every string here is the shape a real take has: what `say` was given, and what
Deepgram handed back after hearing it off a laptop speaker.
"""

from __future__ import annotations

import pytest

from voiceloop.echo import is_echo, ratio, strip_echo

ALERT = "Nuevo evento de inbox realtime."
DOUBT = "¿Es el nombre, o te lo mando a la ventana?"


# --- our voice coming back -------------------------------------------------


def test_the_announcement_coming_back_is_not_something_you_said():
    assert strip_echo("nuevo evento de inbox realtime", ALERT) == ""
    assert is_echo("nuevo evento de inbox realtime", ALERT) is True


def test_an_echo_the_recognizer_mangled_is_still_an_echo():
    """`say` is heard through a speaker and a room; nothing comes back verbatim."""
    assert strip_echo("nuevo evento the inbox real time", ALERT) == ""


def test_what_you_said_over_the_top_of_it_survives():
    assert strip_echo("nuevo evento de inbox realtime dámelo", ALERT) == "dámelo"


def test_a_tail_of_nothing_but_glue_is_not_a_phrase_you_said():
    assert strip_echo("nuevo evento de inbox realtime y", ALERT) == ""


# --- and everything that must not be mistaken for it ------------------------


def test_a_sentence_that_happens_to_share_words_is_left_alone():
    """The one that stored somebody's answer as a window name.

    "los tests" is in both, and taking it out turned "mergealo cuando pasen los
    tests" into a perfectly plausible three-word name for the window.
    """
    said = "terminó los tests del event processor. ¿La llamo tests event processor?"

    assert strip_echo("mergealo cuando pasen los tests", said) == (
        "mergealo cuando pasen los tests"
    )


def test_answering_a_question_with_its_own_words_is_not_an_echo():
    """"¿Es el nombre…?" is answered "es el nombre". All three words are ours."""
    assert strip_echo("es el nombre", DOUBT) == "es el nombre"
    assert strip_echo("mandalo a la ventana", "¿Te lo mando a la ventana?") == (
        "mandalo a la ventana"
    )


def test_the_whole_question_back_plus_the_answer_keeps_the_answer():
    heard = "es el nombre o te lo mando a la ventana es el nombre"

    assert strip_echo(heard, DOUBT) == "es el nombre"


def test_one_word_in_common_is_a_coincidence_not_an_echo():
    """"dos" answered to a menu, right after "Quedan dos" was said."""
    assert strip_echo("dos", "Quedan dos") == "dos"


def test_a_take_with_nothing_said_before_it_is_never_filtered():
    assert strip_echo("dame los pendientes", "") == "dame los pendientes"
    assert is_echo("dame los pendientes", "") is False


def test_silence_is_not_an_echo():
    assert strip_echo("", ALERT) == ""
    assert is_echo("", ALERT) is False


@pytest.mark.parametrize(
    "heard",
    ["dámelo", "después", "mergealo cuando pasen los tests", "la dos", "mostrame"],
)
def test_the_things_you_actually_say_are_never_eaten(heard):
    assert strip_echo(heard, ALERT) == heard


def test_similarity_is_a_number_between_zero_and_one():
    assert ratio("", ALERT) == 0.0
    assert ratio(ALERT, ALERT) == 1.0
    assert 0.0 < ratio("nuevo evento", ALERT) < 1.0
