"""Slugs you can say, hear, and say back — see voiceloop/naming.py."""

from __future__ import annotations

import pytest

from voiceloop import naming


@pytest.mark.parametrize(
    "spoken, expected",
    [
        ("Tests event processor", "tests event processor"),
        ("índice de migración", "indice de migracion"),
        ("draft-mode-changes", "draft mode changes"),
        ("El de los hooks", "de los hooks"),
        ("  Deploy,  staging!  ", "deploy staging"),
        ("uno dos tres cuatro cinco seis", "uno dos tres cuatro"),
        ("", ""),
        ("la", ""),
    ],
)
def test_a_slug_is_lowercase_unaccented_words(spoken, expected):
    assert naming.slugify(spoken) == expected


def test_a_slug_never_keeps_hyphens():
    """`say` reads a hyphen as a pause, and nobody dictates one back."""
    assert "-" not in naming.slugify("event-processor-tests")


def test_a_long_slug_is_capped():
    assert len(naming.slugify("a" * 80)) <= naming.MAX_CHARS


@pytest.mark.parametrize(
    "utterance",
    ["índice", "tests event processor", "el de los hooks"],
)
def test_a_short_phrase_could_be_a_name(utterance):
    assert naming.is_plausible(utterance) is True


@pytest.mark.parametrize(
    "utterance",
    [
        "mergealo cuando pasen los tests",
        "no, mejor esperá a que termine el deploy de staging",
        "",
    ],
)
def test_a_sentence_is_not_a_name(utterance):
    """The mic is open on a window that is also waiting for an answer."""
    assert naming.is_plausible(utterance) is False


# --- the name inside an answer ---------------------------------------------


@pytest.mark.parametrize(
    "spoken, expected",
    [
        ("llama la fecha actual", "fecha actual"),
        ("llamala índice de migración", "indice de migracion"),
        ("fecha actual", "fecha actual"),
        ("ponele el de los hooks", "de los hooks"),
    ],
)
def test_the_lead_in_is_not_part_of_the_name(spoken, expected):
    assert naming.dictated(spoken) == expected


@pytest.mark.parametrize(
    "spoken",
    ["llamala índice", "llama la fecha actual", "ponele el de los hooks", "nombrala x"],
)
def test_a_phrase_can_say_outright_that_it_is_a_name(spoken):
    assert naming.says_it_is_a_name(spoken) is True


@pytest.mark.parametrize(
    "spoken",
    ["mergealo", "índice de migración", "mergealo cuando pasen los tests", ""],
)
def test_and_when_it_does_not_say_so_it_is_doubtful(spoken):
    assert naming.says_it_is_a_name(spoken) is False


@pytest.mark.parametrize(
    "spoken",
    [
        "mergealo cuando pasen los tests",
        "llamala cuando termine el deploy de staging",
        "la",
        "",
    ],
)
def test_a_sentence_is_never_read_as_a_dictated_name(spoken):
    """`slugify` would happily truncate this one into a four-word name."""
    assert naming.dictated(spoken) == ""
