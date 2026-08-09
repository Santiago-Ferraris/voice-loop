"""Turning "¿la llamo…?" into a name you can say and the recognizer can hear.

Claude names an unnamed window after its directory plus two hex characters —
`darwin-21`, `darwin-ae`. Out loud that is worthless: three windows in the same
repo are three near-identical noises, and none of them says what the window is
doing. So the first time such a window announces itself, voice-loop proposes a
name and asks.

A name has to survive three round trips: spoken by `say`, heard by you,
transcribed by the recognizer when you say it back. That rules out punctuation,
accents, casing and hyphens, and it rules out length — four words is already a
mouthful, and past that nobody repeats it the same way twice. What is left is
two to four plain lowercase words, which is also exactly what reads well as a
keyterm.

The word cap does one more job. The naming question is asked with the
microphone open on a window that is *also* waiting for an answer, so "mergealo
cuando pasen los tests" is a thing you will say into it. A dictated name is
short by construction; anything longer was meant for the window, and the caller
hands it on rather than storing it as a name.
"""

from __future__ import annotations

import re
import unicodedata

MIN_WORDS = 1
MAX_WORDS = 4
MAX_CHARS = 40

# Said before the name in an offer ("la llamo…", "ponele…"), and worth nothing
# inside it.
LEADING_NOISE = frozenset(
    {
        "la", "el", "lo", "las", "los", "una", "un", "que", "se", "sea", "es",
        "llama", "llamala", "llamalo", "llamale", "ponele", "poneme", "ponle",
        "decile", "nombrala", "nombralo", "mejor", "aa", "eh", "este", "esta",
    }
)

_NON_WORD = re.compile(r"[^0-9a-z]+")


def fold(text: str) -> str:
    """Unaccented lowercase words — `Índice` and `indice` must be one name."""
    decomposed = unicodedata.normalize("NFD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NON_WORD.sub(" ", stripped.lower()).strip()


def _words(text: str) -> list[str]:
    words = [word for word in fold(text).split(" ") if word]
    while words and words[0] in LEADING_NOISE:
        words.pop(0)
    return words


def slugify(text: str) -> str:
    """A spoken name: lowercase words, no accents, no punctuation, at most four.

    Words, not dashes: this is read out by `say` and matched against what you
    say back, and `tests-event-processor` only becomes three words again by
    going through the hyphen rule in `announce`.
    """
    return " ".join(_words(text)[:MAX_WORDS])[:MAX_CHARS].strip()


def dictated(text: str) -> str:
    """The name inside an answer to the offer, or `""` when there is none.

    `slugify` truncates, which is what makes it safe on a name and wrong on a
    sentence: "mergealo cuando pasen los tests" comes back from it as a
    perfectly plausible four-word name. So the cap is checked on what is left
    once the lead-in is dropped — "llamala fecha actual" is two words and a
    name, "mergealo cuando pasen los tests" is five and is not.
    """
    words = _words(text)
    if not MIN_WORDS <= len(words) <= MAX_WORDS:
        return ""
    return " ".join(words)[:MAX_CHARS].strip()


def is_plausible(text: str) -> bool:
    """Could this utterance have been meant as a name at all?

    Length is the only signal available — and the only one that matters, since
    the alternative reading is "an answer for the window", which is a sentence.
    """
    words = [word for word in fold(text).split(" ") if word]
    return MIN_WORDS <= len(words) <= MAX_WORDS
