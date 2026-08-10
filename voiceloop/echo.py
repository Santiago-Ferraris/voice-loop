"""Telling our own voice apart from yours, in the transcript.

The microphone is open while `say` is talking. On speakers that means every
take contains the announcement itself, and a recognizer that heard "Nuevo
evento de inbox realtime" will happily hand it over as if you had said it —
which is how an announcement ends up answering itself.

We know, textually, what was just spoken. So this is not "is that take an
echo?" but **subtraction**: our sentence lands somewhere inside the take, and
what is left over once it is taken out is you. Reading the pendings list takes
twenty-five seconds, the instruction is said the moment it ends, and the take
that comes back is seventy-five words of us followed by twenty of you. Deciding
that whole thing "is an echo" throws your instruction away; deciding it is not
one hands a ninety-word blob to a classifier that will refuse it for length.
Both readings lose the sentence. The remainder is the only useful answer.

Not equality, and not a prefix:

* **Folded, then aligned.** The recognizer gives back `opus cuatro punto ocho`
  where `say` was handed "Opus 4.8", `darwin-e4` where it was handed "darwin
  e4", and no punctuation at all. Both sides are folded to bare words and
  aligned token by token, so the stretches that survived recognition anchor the
  match and the mangled bits in between are carried along with them.
* **Anywhere in the take, not only at the head.** Interrupt us and the sentence
  runs on for a moment, which leaves your words on *both* sides of ours. The
  span our sentence occupies is cut out; the head and the tail are yours.
* **Only if it covers most of what we said.** A read-back asks "¿Es el nombre,
  o te lo mando a la ventana?" and the answer to it is "es el nombre" — three
  words that are all in the question. A real echo brings the *whole* sentence
  back, not three words of it. This is the rule that keeps the subtraction from
  eating an answer built out of our own words, and it is why "mergealo cuando
  pasen los tests" survives a window whose summary was "terminó los tests".
* **What is left has to say something.** Filtering "nuevo evento de inbox
  realtime y" down to "y" is not a phrase you said, it is the tail of the one
  we said, so a remainder of nothing but glue counts as silence.

Word limits belong *after* this, never before: the take that is too long to
classify is usually seventy-five words of our own voice.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Sequence

# Two tokens in a row. One is a coincidence; see the module docstring.
MIN_ECHO_RUN = 2

# How much of what we said the match has to account for. Below this it is you
# using our words back at us, which is what answering a question looks like.
ECHO_COVERAGE = 0.6

# The last resort, for an echo mangled past the point where its words line up
# with ours. High on purpose: "mandalo a la ventana" answered to "¿Te lo mando
# a la ventana?" is three quarters the same string and is not an echo.
WHOLE_UTTERANCE_RATIO = 0.85

# How many more words than we said a stretch of the take may run to before it
# stops being the same sentence, mangled, and starts being you. "Opus 4.8" is
# two words to `say` and comes back as three; a sentence of yours in the middle
# of ours is a dozen, and the alignment stops there rather than eat it.
GAP_SLACK = 2

# What is left after the echo is stripped, if it is only these, is nothing.
NOISE_TOKENS = frozenset({
    "a", "ah", "aha", "al", "algo", "de", "del", "e", "eh", "el", "em", "en",
    "es", "este", "esto", "la", "las", "le", "lo", "los", "mm", "mmm", "o",
    "por", "que", "se", "su", "un", "una", "uh", "um", "y",
})

_NON_WORD = re.compile(r"[^0-9a-z\s]+")
_WHITESPACE = re.compile(r"\s+")

# Zero to twenty-nine, then the tens. Enough for every number this daemon says
# out loud — how long ago a window blocked, how many are waiting, the digits of
# a version number. Anything bigger is left as digits and carried along by the
# alignment instead.
_CARDINALS = (
    "cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho",
    "nueve", "diez", "once", "doce", "trece", "catorce", "quince", "dieciseis",
    "diecisiete", "dieciocho", "diecinueve", "veinte", "veintiuno",
    "veintidos", "veintitres", "veinticuatro", "veinticinco", "veintiseis",
    "veintisiete", "veintiocho", "veintinueve",
)
_TENS = {
    3: "treinta", 4: "cuarenta", 5: "cincuenta", 6: "sesenta", 7: "setenta",
    8: "ochenta", 9: "noventa",
}


def fold(text: str) -> str:
    """Lowercase, unaccented, punctuation-free — the form both sides compare in."""
    decomposed = unicodedata.normalize("NFD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _WHITESPACE.sub(" ", _NON_WORD.sub(" ", stripped.lower())).strip()


def _tokens(text: str) -> list[str]:
    return [token for token in (text or "").split() if token]


def _spell(word: str) -> list[str]:
    """A number the way `say` reads it, because that is how it comes back.

    "hace 24 minutos" is ours in writing and `hace veinticuatro minutos` in the
    take, and "Opus 4.8" is `opus cuatro punto ocho` — the recognizer runs with
    `numerals=false` on purpose (see `spoken.py`), so every number it hears is
    spelled out. Comparing digits against words is comparing nothing, and it
    costs the sentence that follows the number.

    Accent-free on purpose: this is only ever compared against folded words.
    """
    if not word.isdigit():
        return [word]
    value = int(word)
    if value < len(_CARDINALS):
        return [_CARDINALS[value]]
    if value < 100:
        tens, unit = divmod(value, 10)
        return [_TENS[tens]] if not unit else [_TENS[tens], "y", _CARDINALS[unit]]
    return [word]


def _words(text: str) -> list[str]:
    return [part for token in fold(text).split() for part in _spell(token)]


def ratio(heard: str, said: str) -> float:
    """How much of one utterance is the other, 0 to 1, on folded characters."""
    left, right = fold(heard), fold(said)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def _meaningful(tokens: Sequence[str]) -> bool:
    return any(fold(token) not in NOISE_TOKENS for token in tokens if fold(token))


@dataclass(frozen=True)
class _Piece:
    """One word of the take as it was written, and where it sits once folded.

    Folding splits as well as strips: `darwin-e4` is one word in the transcript
    and two once folded, which is exactly the two `say` was given. The span is
    what lets a match found on folded words be given back in the original ones,
    accents and all.
    """

    raw: str
    start: int
    stop: int


def _pieces(text: str) -> tuple[list[_Piece], list[str]]:
    words: list[str] = []
    pieces: list[_Piece] = []
    for token in _tokens(text):
        parts = _words(token)
        pieces.append(_Piece(token, len(words), len(words) + len(parts)))
        words.extend(parts)
    return pieces, words


@dataclass(frozen=True)
class _Span:
    """Where our sentence landed in the take, on folded words."""

    # Into the take.
    start: int
    stop: int
    # Into what we said: how far the alignment got, and how much of it matched.
    said_start: int
    said_stop: int
    covered: int


def _echo_span(spoken: Sequence[str], heard: Sequence[str]) -> _Span | None:
    """The stretch of the take our sentence occupies, or `None` if it does not.

    `SequenceMatcher` aligns in order, so our sentence can only be consumed
    once: the "que lo deje fijo en" you say back at the end cannot be matched
    against the one we already spent at the start, which is what keeps your
    instruction out of the span.

    Anchoring starts at the first run long enough not to be a coincidence, and
    each later run is annexed only while it keeps looking like the same
    sentence — a gap in the take no wider than the gap in ours, plus slack for
    a number read out in words. A gap wider than that is you talking, and the
    alignment stops there rather than swallow it.
    """
    blocks = [
        block
        for block in SequenceMatcher(None, spoken, heard, autojunk=False).get_matching_blocks()
        if block.size
    ]
    anchor = next(
        (at for at, block in enumerate(blocks) if block.size >= MIN_ECHO_RUN), None
    )
    if anchor is None:
        return None
    first = last = blocks[anchor]
    covered = first.size
    for block in blocks[anchor + 1 :]:
        if block.b - (last.b + last.size) > block.a - (last.a + last.size) + GAP_SLACK:
            break
        last = block
        covered += block.size
    return _Span(
        start=first.b,
        stop=last.b + last.size,
        said_start=first.a,
        said_stop=last.a + last.size,
        covered=covered,
    )


def strip_echo(heard: str, said: str) -> str:
    """What is left of `heard` once everything we just said is taken out of it.

    Returns `""` when the whole take was our own voice — which the caller reads
    as "the microphone heard nothing", the same as silence.
    """
    pieces, words = _pieces(heard)
    if not pieces or not (said or "").strip():
        return (heard or "").strip()

    spoken = _words(said)
    if not spoken or not words:
        return (heard or "").strip()

    span = _echo_span(spoken, words)
    if span is None or span.covered < ECHO_COVERAGE * len(spoken):
        # Our sentence is not in there in any recognisable piece. Either the
        # take is yours, or it is us transcribed so badly that only the shape
        # of the whole string is left to go on.
        if ratio(heard, said) >= WHOLE_UTTERANCE_RATIO:
            return ""
        return (heard or "").strip()

    head = [piece.raw for piece in pieces if piece.stop <= span.start]
    tail = [piece.raw for piece in pieces if piece.start >= span.stop]
    # Our first and last words can fall outside the aligned span when the
    # recognizer mangled them — "realtime" coming back as "real time" is two
    # words that match neither. What sits past the span and reads like the rest
    # of our own sentence is ours too.
    if _is_ours(head, spoken[: span.said_start]):
        head = []
    if _is_ours(tail, spoken[span.said_stop :]):
        tail = []

    left = head + tail
    if not _meaningful(left):
        return ""
    return " ".join(left)


def _is_ours(left: Sequence[str], unsaid: Sequence[str]) -> bool:
    """Is what fell outside the span just the ragged edge of our own sentence?"""
    return bool(left) and ratio(" ".join(left), " ".join(unsaid)) >= WHOLE_UTTERANCE_RATIO


def is_echo(heard: str, said: str) -> bool:
    """Was that take nothing but our own voice coming back?"""
    return bool((heard or "").strip()) and not strip_echo(heard, said)
