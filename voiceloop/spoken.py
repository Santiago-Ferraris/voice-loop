"""Model names and version numbers, back into the form they are written in.

Dictating "opus 4.8" gets you `opu cuatro punto ocho`, and that string is what
Claude receives. Two things went wrong in it and both are fixed here, after the
transcription rather than inside it:

* **The name.** `opus` is in the recognizer's vocabulary now (see
  `vocabulary.MODEL_NAMES`), which is the real fix; this is the second line of
  defence, because a keyterm makes a word *likely*, not certain. A token one
  edit away from a model name, with a number behind it, was that model name.
* **The number.** `numerals=false` is deliberate — with Deepgram's formatting
  on, "fijate primero" arrives as "fijate 1º" — so every number comes back
  spelled out, and a version number spelled out is unreadable.

The conversion is deliberately narrow: a number becomes digits **only** when it
follows a model name or the word "versión". Everything else stays exactly as it
was said, because the numbers this daemon hears are mostly not versions —
"la dos" picks option 2 out of a menu and "esperá cinco minutos" is not a
quantity anybody wants rewritten. Leaving a version in words costs a
re-dictation; rewriting a menu answer breaks the thing that already works.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from .intents import fold

# The names worth repairing and pinning into the recognizer's vocabulary. They
# are here rather than in `vocabulary` because the repair below needs them too,
# and one list cannot disagree with itself.
MODEL_NAMES: tuple[str, ...] = ("opus", "sonnet", "haiku", "claude", "gemini", "gpt")

# The other word that licenses a version number: "la versión cuatro punto dos".
VERSION_WORDS: frozenset[str] = frozenset({"version"})

# Cardinals only, and not `intents.NUMBERS`: that map is for picking an option
# out of a menu, so it also answers to "primero" and "cuarta", which are not
# how anybody says a version. Twenty is far past every model that exists.
DIGITS: dict[str, str] = {
    "cero": "0", "uno": "1", "un": "1", "una": "1", "dos": "2", "tres": "3",
    "cuatro": "4", "cinco": "5", "seis": "6", "siete": "7", "ocho": "8",
    "nueve": "9", "diez": "10", "once": "11", "doce": "12", "trece": "13",
    "catorce": "14", "quince": "15", "dieciseis": "16", "diecisiete": "17",
    "dieciocho": "18", "diecinueve": "19", "veinte": "20",
}

DOT_WORDS: frozenset[str] = frozenset({"punto"})

# A misheard name has to be *nearly* the name. "opu" scores 0.86 against
# "opus" and "sonet" 0.91 against "sonnet"; "opas" (0.75) and "claudia" (0.77)
# do not, which is the point. Short names are exact-match only: three letters
# are one edit away from too much ordinary Spanish.
NEAR_ENOUGH = 0.8
MIN_HEARD_CHARS = 3
MIN_NAME_CHARS = 4

_WORD = re.compile(r"\w+|\W+", re.UNICODE)


def _is_word(piece: str) -> bool:
    return bool(piece) and piece[0].isalnum()


def model_name(token: str) -> str | None:
    """The model this token is, spelled properly — or `None` if it is not one."""
    folded = fold(token)
    if not folded:
        return None
    if folded in MODEL_NAMES:
        return folded
    if len(folded) < MIN_HEARD_CHARS:
        return None
    best, score = None, NEAR_ENOUGH
    for name in MODEL_NAMES:
        if len(name) < MIN_NAME_CHARS:
            continue
        ratio = SequenceMatcher(None, folded, name, autojunk=False).ratio()
        if ratio >= score:
            best, score = name, ratio
    return best


def _spelled(heard: str, name: str) -> str:
    """Keep what was heard when it was already right; repairs are lowercase."""
    if fold(heard) == name:
        return heard
    return name.capitalize() if heard[:1].isupper() else name


def _digits(piece: str) -> str | None:
    folded = fold(piece)
    if folded in DIGITS:
        return DIGITS[folded]
    return folded if folded.isdigit() else None


def _version_after(pieces: list[str], start: int) -> tuple[str, int]:
    """The version number that begins at `start`, and how many pieces it ate.

    `("4.8", 4)` for the ` cuatro punto ocho` behind a model name. Only spaces
    are allowed to separate its parts: a number on the far side of a comma
    belongs to another clause.
    """
    parts: list[str] = []
    index, consumed = start, 0

    def take_gap(at: int) -> int | None:
        return at + 1 if at < len(pieces) and pieces[at].isspace() else None

    while True:
        after_gap = take_gap(index)
        if after_gap is None or after_gap >= len(pieces):
            break
        digit = _digits(pieces[after_gap]) if _is_word(pieces[after_gap]) else None
        if digit is None:
            break
        parts.append(digit)
        index, consumed = after_gap + 1, after_gap + 1 - start
        dot = take_gap(index)
        if dot is None or dot >= len(pieces) or fold(pieces[dot]) not in DOT_WORDS:
            break
        index = dot + 1
    return ".".join(parts), consumed


def normalize(text: str) -> str:
    """`opu cuatro punto ocho` -> `opus 4.8`. Everything else is left alone."""
    if not text or not text.strip():
        return text
    pieces = _WORD.findall(text)
    out: list[str] = []
    index = 0
    while index < len(pieces):
        piece = pieces[index]
        if not _is_word(piece):
            out.append(piece)
            index += 1
            continue
        name = model_name(piece)
        folded = fold(piece)
        if name is None and folded not in VERSION_WORDS:
            out.append(piece)
            index += 1
            continue
        version, consumed = _version_after(pieces, index + 1)
        if not version:
            out.append(piece)
            index += 1
            continue
        out.append(_spelled(piece, name) if name else piece)
        out.append(" ")
        out.append(version)
        index += 1 + consumed
    return "".join(out)
