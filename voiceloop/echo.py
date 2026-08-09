"""Telling our own voice apart from yours, in the transcript.

The microphone is open while `say` is talking. On speakers that means every
take contains the announcement itself, and a recognizer that heard "Nuevo
evento de inbox realtime" will happily hand it over as if you had said it —
which is how an announcement ends up answering itself.

We know, textually, what was just spoken. So recognizing the echo is comparing
strings. Not equality: the recognizer never gives back what `say` said word for
word ("Nuevo evento de" comes back as "nuevo evento the"), and the phonetic
dictionary means the audio does not even match the text we started from. So it
is a similarity, run over folded tokens.

Three rules keep it from eating what you actually said, and all three come from
the same fact: **the echo is our whole sentence, at the head of the take.** We
start talking first and we say all of it, so anything else is you.

* **Only a prefix.** A match in the middle of what you said is a coincidence,
  and a costly one: "mergealo cuando pasen los tests" answered to a window
  whose summary was "terminó los tests" shares "los tests" with it, and
  deleting that turns your sentence into a different one.
* **Only if it covers most of what we said.** A read-back asks "¿Es el nombre,
  o te lo mando a la ventana?" and the answer to it is "es el nombre" — three
  words that are all in the question. A real echo brings the *whole* question
  back, not three words of it, so the prefix has to account for most of what
  was spoken before it counts as us.
* **What is left has to say something.** Filtering "nuevo evento de inbox
  realtime y" down to "y" is not a phrase you said, it is the tail of the one
  we said, so a remainder of nothing but glue counts as silence.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Sequence

# Two tokens in a row. One is a coincidence; see the module docstring.
MIN_ECHO_RUN = 2

# How much of what we said the prefix has to account for. Below this it is you
# using our words back at us, which is what answering a question looks like.
ECHO_COVERAGE = 0.6

# The last resort, for an echo mangled past the point where its words line up
# with ours. High on purpose: "mandalo a la ventana" answered to "¿Te lo mando
# a la ventana?" is three quarters the same string and is not an echo.
WHOLE_UTTERANCE_RATIO = 0.85

# What is left after the echo is stripped, if it is only these, is nothing.
NOISE_TOKENS = frozenset({
    "a", "ah", "aha", "al", "algo", "de", "del", "e", "eh", "el", "em", "en",
    "es", "este", "esto", "la", "las", "le", "lo", "los", "mm", "mmm", "o",
    "por", "que", "se", "su", "un", "una", "uh", "um", "y",
})

_NON_WORD = re.compile(r"[^0-9a-z\s]+")
_WHITESPACE = re.compile(r"\s+")


def fold(text: str) -> str:
    """Lowercase, unaccented, punctuation-free — the form both sides compare in."""
    decomposed = unicodedata.normalize("NFD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _WHITESPACE.sub(" ", _NON_WORD.sub(" ", stripped.lower())).strip()


def _tokens(text: str) -> list[str]:
    return [token for token in (text or "").split() if token]


def ratio(heard: str, said: str) -> float:
    """How much of one utterance is the other, 0 to 1, on folded characters."""
    left, right = fold(heard), fold(said)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def _meaningful(tokens: Sequence[str]) -> bool:
    return any(fold(token) not in NOISE_TOKENS for token in tokens if fold(token))


def strip_echo(heard: str, said: str) -> str:
    """What is left of `heard` once everything we just said is taken out of it.

    Returns `""` when the whole take was our own voice — which the caller reads
    as "the microphone heard nothing", the same as silence.
    """
    raw = _tokens(heard)
    if not raw or not (said or "").strip():
        return (heard or "").strip()

    spoken = [token for token in (fold(token) for token in _tokens(said)) if token]
    folded = [fold(token) for token in raw]
    if not spoken or not any(folded):
        return (heard or "").strip()

    cut, covered = _echo_prefix(spoken, folded)
    if cut < MIN_ECHO_RUN or covered < ECHO_COVERAGE * len(spoken):
        # Nothing lined up at the head. Either it is you, or it is us so badly
        # transcribed that only the shape of the whole string is left to go on.
        if ratio(heard, said) >= WHOLE_UTTERANCE_RATIO:
            return ""
        return (heard or "").strip()

    left = raw[cut:]
    if not _meaningful(left):
        return ""
    return " ".join(left)


def _echo_prefix(spoken: Sequence[str], folded: Sequence[str]) -> tuple[int, int]:
    """How many leading tokens of the take are us, and how much of us they are.

    Walked block by block from the head: our sentence comes back with words
    dropped and mangled in the middle, so the prefix is "everything up to the
    last stretch that still matches", not one contiguous run.
    """
    cut = 0
    covered = 0
    while cut < len(folded):
        matcher = SequenceMatcher(None, spoken, folded[cut:], autojunk=False)
        block = next(
            (
                found
                for found in matcher.get_matching_blocks()
                if found.b == 0 and found.size >= MIN_ECHO_RUN
            ),
            None,
        )
        if block is None:
            break
        cut += block.size
        covered += block.size
        if covered >= len(spoken):
            # All of it is behind us. Anything past here is you saying our
            # words back — which is exactly what answering a question is.
            break
    return cut, covered


def is_echo(heard: str, said: str) -> bool:
    """Was that take nothing but our own voice coming back?"""
    return bool((heard or "").strip()) and not strip_echo(heard, said)
