"""Model names and versions, and everything that must survive them untouched.

The line from the log this file exists for:

    20:43:47  heard 'abrí una nueva ventana y decile a claude que que modifique
              el alias ... e inicie las sesiones con opu cuatro punto ocho'

"opus 4.8", said out loud, reaches Claude as `opu cuatro punto ocho` — a name
the recognizer had never been told and a number `numerals=false` leaves in
words. Both halves are fixed, and the second half is the dangerous one: the
same daemon answers menus with "la dos", and a normalizer that rewrote *that*
would break the thing that already works to fix the thing that does not.
"""

from __future__ import annotations

import pytest

from voiceloop import spoken


# --- what it fixes ---------------------------------------------------------


def test_the_phrase_from_the_log():
    assert spoken.normalize("opu cuatro punto ocho") == "opus 4.8"


def test_a_name_that_came_back_whole_is_left_whole():
    assert spoken.normalize("opus cuatro punto ocho") == "opus 4.8"


def test_the_whole_sentence_it_arrived_in():
    heard = (
        "modifique el alias que tengo de claude e inicie las sesiones con "
        "opu cuatro punto ocho por default"
    )

    assert spoken.normalize(heard) == (
        "modifique el alias que tengo de claude e inicie las sesiones con "
        "opus 4.8 por default"
    )


@pytest.mark.parametrize(
    "heard, written",
    [
        ("sonet cuatro punto cinco", "sonnet 4.5"),
        ("sonnet cuatro punto cinco", "sonnet 4.5"),
        ("haiko tres punto cinco", "haiku 3.5"),
        ("claude cuatro punto cinco", "claude 4.5"),
        ("gemini dos punto cinco", "gemini 2.5"),
        ("gpt cinco", "gpt 5"),
        ("opus cuatro punto ocho punto uno", "opus 4.8.1"),
        ("opus cuatro", "opus 4"),
        ("la version dos punto uno", "la version 2.1"),
        ("la versión dos punto uno", "la versión 2.1"),
    ],
)
def test_the_names_and_the_shapes_a_version_comes_in(heard, written):
    assert spoken.normalize(heard) == written


def test_a_number_that_already_came_back_as_digits_is_joined_up_anyway():
    assert spoken.normalize("opus 4 punto 8") == "opus 4.8"


def test_the_name_is_written_the_way_it_was_said():
    assert spoken.normalize("Opus cuatro punto ocho") == "Opus 4.8"
    assert spoken.normalize("Opu cuatro punto ocho") == "Opus 4.8"


# --- and everything it must not touch --------------------------------------


def test_choosing_an_option_out_of_a_menu_is_never_rewritten():
    """"la dos" is how a menu gets answered. It is not a version of anything."""
    for said in ("la dos", "dame la dos", "dos", "la primera", "opción tres"):
        assert spoken.normalize(said) == said


def test_a_number_that_is_a_quantity_stays_a_quantity():
    for said in ("esperá cinco minutos", "corré los tests dos veces", "hace diez minutos"):
        assert spoken.normalize(said) == said


def test_a_model_name_with_nothing_behind_it_is_left_alone():
    for said in ("usá opus", "opu", "pasalo a sonnet", "claude no contesta"):
        assert spoken.normalize(said) == said


def test_a_number_in_another_clause_does_not_belong_to_the_name():
    """Only a space joins a name to its version — a comma is a new thing said."""
    assert spoken.normalize("usá opus, dos veces") == "usá opus, dos veces"


def test_a_word_that_merely_resembles_a_model_name_is_not_one():
    for said in ("claro cuatro", "opas cuatro", "claudia dos", "las tres"):
        assert spoken.normalize(said) == said


def test_silence_stays_silence():
    assert spoken.normalize("") == ""
    assert spoken.normalize("   ") == "   "


# --- the pieces the rest of the daemon uses --------------------------------


def test_the_model_names_it_knows():
    assert spoken.model_name("opu") == "opus"
    assert spoken.model_name("Sonnet") == "sonnet"
    assert spoken.model_name("sonet") == "sonnet"
    assert spoken.model_name("mergealo") is None
    assert spoken.model_name("") is None


def test_a_three_letter_name_is_exact_or_nothing():
    """One edit away from "gpt" is one edit away from too much Spanish."""
    assert spoken.model_name("gpt") == "gpt"
    assert spoken.model_name("gps") is None
