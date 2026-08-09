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
    KIND_GIVE,
    KIND_LATER,
    KIND_REPEAT,
    KIND_SELECT,
    KIND_PENDINGS,
    KIND_SHOW,
    KIND_STATUS,
    KIND_SILENCE,
    KIND_SKIP,
    KIND_TEXT,
    KIND_WAIT,
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


HOTKEYS = [
    "Probalo sin hotkeys primero (Recomendado)",
    "Actualizar Command Line Tools",
    "Adaptar a Hammerspoon",
]


@pytest.mark.parametrize(
    "said",
    [
        "probalo sin hotkeys primero recomendado",  # read off the screen
        "probalo sin hotkeys primero",  # repeated back from what was spoken
        "probalo",
        "hotkeys",
        "el recomendado",
    ],
)
def test_a_shortened_label_still_matches_the_whole_one(said):
    """Options are spoken short; every way of naming this one picks it."""
    assert parse(said, HOTKEYS).index == 1


def test_a_word_two_options_share_is_still_not_a_selection():
    assert parse("adaptar", ["Adaptar a Hammerspoon", "Adaptar a Shortcuts"]).kind == KIND_TEXT


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


@pytest.mark.parametrize("said", ["salteá", "salteala", "siguiente", "paso"])
def test_skipping_is_recognized(said):
    assert parse(said).kind == KIND_SKIP


@pytest.mark.parametrize(
    "said",
    [
        "después",
        "más tarde",
        "ahora no",
        "en un rato",
        "dejalo para después",
        # The same instruction said the other way round: there is no snooze by
        # the clock, only a place in the line.
        "mandalo al fondo",
        "al fondo de la cola",
        "ponelo al final",
    ],
)
def test_putting_it_at_the_back_of_the_line_is_recognized(said):
    assert parse(said).kind == KIND_LATER


@pytest.mark.parametrize(
    "said",
    ["dámelo", "damela", "dame", "contame", "decime", "leemelo", "qué dice", "a ver"],
)
def test_asking_for_the_one_that_was_announced_is_recognized(said):
    assert parse(said).kind == KIND_GIVE


def test_asking_for_the_list_is_not_asking_for_this_one():
    """"damelos" is the queue; "dámelo" is the item that just chimed."""
    assert parse("dame los pendientes").kind == KIND_PENDINGS
    assert parse("damelos").kind == KIND_PENDINGS
    assert parse("dámelo").kind == KIND_GIVE


def test_a_give_word_inside_a_sentence_is_still_dictation():
    assert parse("dame un minuto más con el índice").kind == KIND_TEXT
    assert parse("contame qué hiciste con el worker").kind == KIND_TEXT


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


@pytest.mark.parametrize(
    "said",
    ["cuál queda", "cuántas quedan", "cuántos faltan", "cuál me queda", "qué queda",
     "Cuál queda?"],
)
def test_asking_which_one_is_left_asks_the_queue(said):
    """Verbatim from the first real run, where it was typed into the window."""
    assert parse(said).kind == KIND_PENDINGS


@pytest.mark.parametrize(
    "said",
    ["qué dijiste", "qué me dijiste", "cómo dijiste", "no te entendí", "una vez más"],
)
def test_asking_what_was_said_is_a_repeat(said):
    assert parse(said).kind == KIND_REPEAT


@pytest.mark.parametrize(
    "said",
    ["esperá", "esperame", "un segundo", "momento", "dame un segundo", "aguantame"],
)
def test_asking_for_a_beat_is_neither_an_answer_nor_a_refusal(said):
    assert parse(said).kind == KIND_WAIT


# --- "¿es el nombre, o te lo mando a la ventana?" ---------------------------


@pytest.mark.parametrize(
    "said",
    ["el nombre", "es el nombre", "nombre", "así está bien", "llamala así",
     "sí", "dale", "ese nombre"],
)
def test_answering_that_it_was_the_name(said):
    assert intents.name_or_window(said) == intents.ANSWER_NAME


@pytest.mark.parametrize(
    "said",
    ["a la ventana", "para la ventana", "mandalo", "mandale",
     "mandalo a la ventana", "no", "es la respuesta"],
)
def test_answering_that_it_was_for_the_window(said):
    """"mandalo" is a confirmation everywhere else; here it is the other half."""
    assert intents.name_or_window(said) == intents.ANSWER_WINDOW


@pytest.mark.parametrize(
    "said",
    ["mejor corré los tests primero", "no sé", "", "el índice viejo"],
)
def test_answering_something_else_entirely(said):
    assert intents.name_or_window(said) is None


# --- the ones that only *might* have been for voice-loop --------------------


@pytest.mark.parametrize(
    "said",
    [
        "cuántas ventanas quedan abiertas",
        "qué sesión te falta",
        "cuál ventana está esperando",
        "qué dijiste de la cola",
    ],
)
def test_a_short_question_about_the_queue_is_flagged(said):
    assert intents.looks_systemward(said) is True


@pytest.mark.parametrize(
    "said",
    [
        "qué base uso",  # a question, but about the work
        "cuántos tests corriste",
        "mergealo cuando pasen los tests",
        "qué te parece si dejamos la ventana de la izquierda para después",  # too long
        "",
    ],
)
def test_everything_else_is_not(said):
    """A read-back on every sentence was rejected out loud; this stays narrow."""
    assert intents.looks_systemward(said) is False


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
