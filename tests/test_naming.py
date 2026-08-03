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
