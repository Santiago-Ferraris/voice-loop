"""What counts as a command, and — mostly — what does not.

The bias under test is that dictation goes through literally. Every case below
that expects `text` is protecting that: a sentence with a number in it, a
confirmation with nothing to confirm, an option keyword buried in a phrase.
Only the whole utterance, and only against an open menu, means a selection.
"""

from __future__ import annotations

import pytest

from voiceloop import intents
from voiceloop.intents import (
    KIND_CANCEL,
    KIND_CONFIRM,
    KIND_EXPLAIN,
    KIND_REPEAT,
    KIND_SELECT,
    KIND_PENDINGS,
    KIND_SHOW,
    KIND_STATUS,
    KIND_SILENCE,
    KIND_SKIP,
    KIND_TEXT,
)

OPTIONS = ["SQLite", "Postgres", "Ninguna de las dos"]


def parse(text, options=(), **kwargs):
    return intents.parse(text, options, **kwargs)


# --- numbers ---------------------------------------------------------------


@pytest.mark.parametrize(
    "said,index",
    [
        ("dos", 2),
        ("la dos", 2),
        ("opción dos", 2),
        ("la opción dos", 2),
        ("el número tres", 3),
        ("2", 2),
        ("dos por favor", 2),
        ("la primera", 1),
        ("la tercera", 3),
        ("quiero la uno", 1),
    ],
)
def test_a_number_picks_an_option(said, index):
    assert parse(said, OPTIONS) == intents.Intent(
        KIND_SELECT, index=index, text=said, indexes=(index,)
    )


def test_accents_and_punctuation_do_not_matter():
    assert parse("¿La opción DOS?", OPTIONS).index == 2


def test_a_number_with_no_menu_open_is_just_a_word():
    assert parse("dos").kind == KIND_TEXT


def test_a_number_the_payload_does_not_offer_is_not_a_selection():
    assert parse("cinco", OPTIONS).kind == KIND_TEXT


def test_a_sentence_that_happens_to_start_with_a_number_is_dictation():
    intent = parse("dos cosas: arreglá el índice y corré los tests", OPTIONS)

    assert intent.kind == KIND_TEXT
    assert intent.text == "dos cosas: arreglá el índice y corré los tests"


# --- keywords --------------------------------------------------------------


def test_an_option_label_picks_it():
    assert parse("postgres", OPTIONS).index == 2


def test_a_distinctive_first_word_is_enough():
    assert parse("ninguna", OPTIONS).index == 3


def test_a_keyword_shared_by_two_options_is_not_a_selection():
    """Ambiguity means dictation, not a coin flip."""
    assert parse("postgres", ["Postgres nueve", "Postgres quince"]).kind == KIND_TEXT


def test_a_short_first_word_is_not_distinctive_enough():
    assert parse("de", ["de una manera", "otra cosa"]).kind == KIND_TEXT


def test_a_label_inside_a_longer_sentence_stays_dictation():
    assert parse("usá postgres pero con el índice viejo", OPTIONS).kind == KIND_TEXT


# --- multi-select ----------------------------------------------------------


def test_several_options_at_once_only_on_a_multi_select_menu():
    intent = parse("uno y tres", OPTIONS, multi=True)

    assert intent.kind == KIND_SELECT
    assert intent.indexes == (1, 3)
    assert intent.index == 1


def test_commas_separate_options_too():
    assert parse("uno, dos", OPTIONS, multi=True).indexes == (1, 2)


def test_keywords_can_be_combined_as_well():
    assert parse("sqlite y postgres", OPTIONS, multi=True).indexes == (1, 2)


def test_a_list_where_one_part_is_not_an_option_is_dictation():
    assert parse("uno y el resto no", OPTIONS, multi=True).kind == KIND_TEXT


def test_the_same_phrase_on_a_single_choice_menu_is_dictation():
    assert parse("uno y tres", OPTIONS).kind == KIND_TEXT


def test_a_single_choice_still_works_on_a_multi_select_menu():
    assert parse("dos", OPTIONS, multi=True).indexes == (2,)


# --- explaining ------------------------------------------------------------


@pytest.mark.parametrize(
    "said", ["explicame la dos", "explicame dos", "contame la segunda", "que es la dos"]
)
def test_asking_for_detail_names_the_option(said):
    intent = parse(said, OPTIONS)

    assert intent.kind == KIND_EXPLAIN
    assert intent.index == 2


def test_explaining_an_option_that_does_not_exist_is_dictation():
    assert parse("explicame la nueve", OPTIONS).kind == KIND_TEXT


# --- control words ---------------------------------------------------------


@pytest.mark.parametrize("said", ["dale", "sí", "ok", "confirmo", "de una", "dale sí"])
def test_confirmation_is_recognized(said):
    assert parse(said).kind == KIND_CONFIRM


@pytest.mark.parametrize("said", ["no", "cancelá", "olvidalo", "mejor no"])
def test_cancellation_is_recognized(said):
    assert parse(said).kind == KIND_CANCEL


@pytest.mark.parametrize("said", ["repetí", "de nuevo", "otra vez", "no te escuché"])
def test_asking_for_a_repeat_is_recognized(said):
    assert parse(said).kind == KIND_REPEAT


@pytest.mark.parametrize("said", ["salteá", "después", "ahora no", "siguiente"])
def test_skipping_is_recognized(said):
    assert parse(said).kind == KIND_SKIP


@pytest.mark.parametrize("said", ["mostrame", "mostrámelo", "llevame", "quiero verlo"])
def test_asking_to_be_shown_the_window_is_recognized(said):
    assert parse(said).kind == KIND_SHOW


def test_politeness_does_not_hide_a_control_word():
    assert parse("dale por favor").kind == KIND_CONFIRM


def test_a_control_word_inside_a_sentence_is_dictation():
    """"dale, mergealo" is an answer, not a confirmation."""
    assert parse("dale, mergealo").kind == KIND_TEXT
    assert parse("no, mejor usá el índice viejo").kind == KIND_TEXT


def test_control_words_are_recognized_with_a_menu_open_too():
    """The daemon decides whether they apply; parsing does not."""
    assert parse("mostrame", OPTIONS).kind == KIND_SHOW
    assert parse("dale", OPTIONS).kind == KIND_CONFIRM


# --- everything else -------------------------------------------------------


def test_silence_is_its_own_outcome():
    assert parse("").kind == KIND_SILENCE
    assert parse("   ").kind == KIND_SILENCE
    assert parse("hola", heard=False).kind == KIND_SILENCE


def test_dictation_keeps_the_original_text_untouched():
    said = "  Fijate primero si el rate-limiter anda, ¿sí?  "

    intent = parse(said, OPTIONS)

    assert intent.kind == KIND_TEXT
    assert intent.text == said.strip()


def test_only_dictation_and_silence_are_not_control():
    assert parse("dale").is_control is True
    assert parse("mergealo eso").is_control is False
    assert parse("").is_control is False


# --- the folding helper ----------------------------------------------------


def test_folding_strips_accents_case_and_punctuation():
    assert intents.fold("¿Explicáme, la SEGUNDA?") == "explicame la segunda"


def test_option_keys_map_labels_and_distinctive_first_words():
    keys = intents.option_keys(["SQLite", "Postgres quince"])

    assert keys["sqlite"] == 1
    assert keys["postgres quince"] == 2
    assert keys["postgres"] == 2


# --- questions for voice-loop, not for a window -----------------------------


@pytest.mark.parametrize(
    "said",
    ["dame los pendientes", "qué tengo pendiente", "qué me falta", "pendientes",
     "Dame los pendientes, por favor", "quién me espera"],
)
def test_asking_for_the_queue(said):
    assert parse(said).kind == KIND_PENDINGS


@pytest.mark.parametrize(
    "said",
    ["estado", "cómo venimos", "qué está pasando", "cómo vamos", "Estado."],
)
def test_asking_how_things_are_going(said):
    assert parse(said).kind == KIND_STATUS


def test_a_sentence_that_merely_mentions_pendings_is_still_dictation():
    """Control phrases are whole utterances; everything else is for the window."""
    assert parse("dejá los pendientes para mañana").kind == KIND_TEXT


# --- a yes with something after it ------------------------------------------


@pytest.mark.parametrize(
    "said, tail",
    [
        ("sí", ""),
        ("dale", ""),
        ("sí, dale", ""),
        ("dale por favor", ""),
        ("sí llama la fecha actual", "llama la fecha actual"),
        ("sí, llamala índice", "llamala indice"),
        ("dale, índice de migración", "indice de migracion"),
        ("ok, mergealo cuando pasen los tests", "mergealo cuando pasen los tests"),
    ],
)
def test_a_leading_yes_hands_back_whatever_followed_it(said, tail):
    assert intents.confirmation_tail(said) == tail


@pytest.mark.parametrize(
    "said",
    [
        "mergealo cuando pasen los tests",
        "no",
        "no, mejor usá el índice viejo",
        "claro que no",
        "",
    ],
)
def test_an_utterance_that_does_not_start_with_a_yes_is_not_one(said):
    assert intents.confirmation_tail(said) is None
