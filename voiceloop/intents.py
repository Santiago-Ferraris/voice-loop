"""What you said, classified — as little as possible.

The rule from the design is that dictation is **literal**: Claude reads
disfluent speech better than any parser here would, so the only job of this
module is to catch the handful of phrases that mean something to voice-loop
itself and let everything else through untouched. When in doubt, it is text.

That cuts both ways, and the ambiguity is deliberate. "dale" is a control word
while a read-back is waiting for confirmation, and a perfectly good answer to a
session that just asked "¿lo mergeo?". So `parse` only *classifies*; the daemon
decides whether the classification applies in the state it is in. A `confirm`
with nothing to confirm is delivered as the word "dale".

Menu answers are the one place a number means something. They are recognized
only when a menu is actually open, only when the whole utterance is that
number (with the usual "la", "opción", "por favor" around it), and only when
the number is one the payload offers — "dos cosas: arreglá el índice" is text.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Mapping, Sequence

KIND_TEXT = "text"
KIND_SELECT = "select"
KIND_EXPLAIN = "explain"
KIND_CONFIRM = "confirm"
KIND_CANCEL = "cancel"
KIND_REPEAT = "repeat"
KIND_SKIP = "skip"
KIND_SHOW = "show"
KIND_SILENCE = "silence"
KIND_PENDINGS = "pendings"
KIND_STATUS = "status"
KIND_WAIT = "wait"
KIND_GIVE = "give"
KIND_LATER = "later"

NUMBERS: Mapping[str, int] = {
    "uno": 1, "una": 1, "un": 1, "primero": 1, "primera": 1, "primer": 1,
    "dos": 2, "segundo": 2, "segunda": 2,
    "tres": 3, "tercero": 3, "tercera": 3, "tercer": 3,
    "cuatro": 4, "cuarto": 4, "cuarta": 4,
    "cinco": 5, "quinto": 5, "quinta": 5,
    "seis": 6, "sexto": 6, "sexta": 6,
    "siete": 7, "septimo": 7, "septima": 7,
    "ocho": 8, "octavo": 8, "octava": 8,
    "nueve": 9, "noveno": 9, "novena": 9,
    "diez": 10, "decimo": 10, "decima": 10,
}

CONFIRM_PHRASES = frozenset({
    "si", "si si", "sisi", "dale", "dale si", "si dale", "ok", "oka", "okey", "okay",
    "correcto", "confirmo", "confirma", "confirmalo", "confirmado", "exacto",
    "adelante", "mandale", "mandalo", "mandala", "hacelo", "hacela", "de una",
    "obvio", "perfecto", "listo", "vamos", "afirmativo", "claro", "claro que si",
    "sip", "yes", "va",
})

# The first word of a yes, for the one place a yes can carry something after
# it: the naming offer. "sí, llamala fecha actual" is an acceptance with the
# answer attached, and whole-phrase matching cannot see that — it reads as a
# sentence, which is how an acceptance ends up typed into somebody's window.
# Only the unambiguous ones: "claro que no" and "listo, mandale eso" are not
# yeses with a payload, and they are already whole phrases above.
CONFIRM_LEAD = frozenset({
    "si", "sisi", "sip", "dale", "ok", "oka", "okey", "okay",
    "correcto", "confirmo", "confirmado", "exacto", "obvio", "perfecto",
    "afirmativo", "yes",
})

CANCEL_PHRASES = frozenset({
    "no", "no no", "nop", "cancela", "cancelalo", "cancelala", "cancelar",
    "olvidalo", "olvidate", "nada", "negativo", "mejor no", "no gracias",
    "para", "pare", "abortar", "aborta", "stop",
})

REPEAT_PHRASES = frozenset({
    "repeti", "repetilo", "repetila", "repetime", "repetimelo", "repetimela",
    "repetir", "de nuevo", "otra vez", "una vez mas", "como era", "que dijiste",
    "que me dijiste", "que dijiste recien", "como dijiste", "que decias",
    "que era", "que era eso", "no escuche", "no te escuche", "no entendi",
    "no te entendi", "volve a decirlo", "perdon que dijiste",
})

# "esperá" is neither an answer nor a refusal — it is you asking for a beat.
# Typed into a window it is a turn nobody meant to take.
WAIT_PHRASES = frozenset({
    "espera", "esperate", "esperame", "espera un momento", "espera un segundo",
    "pera", "un momento", "un segundo", "un minuto", "momento", "aguanta",
    "aguantame", "dame un segundo", "dame un momento", "dame un minuto",
    "ahi voy", "ya voy",
})

SKIP_PHRASES = frozenset({
    "saltea", "saltealo", "salteala", "saltear", "salta", "saltalo",
    "siguiente", "el siguiente", "proximo", "el proximo",
    "paso", "dejalo", "next", "skip",
})

# The answer to a heads-up that is neither "tell me" nor silence: not now, and
# not first either. "después" and "mandalo al fondo" are the same instruction —
# there is no snooze by the clock, only a place in the line — which is why they
# are one set and not two.
LATER_PHRASES = frozenset({
    "despues", "mas tarde", "ahora no", "luego", "en un rato", "al rato",
    "despues lo veo", "despues la veo", "despues me lo decis", "despues me decis",
    "despues lo vemos", "dejalo para despues", "dejala para despues",
    "dejalo para mas tarde", "al fondo", "al fondo de la cola", "al final",
    "al final de la cola", "mandalo al fondo", "mandala al fondo",
    "mandalo al final", "mandala al final", "ponelo al fondo", "ponela al fondo",
    "ponelo al final", "ultimo", "de ultimo", "ahora no puedo",
})

# "dámelo": read me the one you just announced. The heads-up says a name and
# nothing else, so this is the word that asks for the rest of it.
GIVE_PHRASES = frozenset({
    "damelo", "damela", "dame", "dame eso", "damelo ya", "damelo ahora",
    "dale damelo", "si damelo", "contamelo", "contamela", "contame", "contame eso",
    "decime", "decimelo", "decimela", "decime eso", "leelo", "leela", "leemelo",
    "leemela", "que dice", "que quiere", "que necesita", "que paso", "que paso ahi",
    "que hay ahi", "escuchemos", "a ver", "a ver eso",
    "el resumen", "dame el resumen",
})

SHOW_PHRASES = frozenset({
    "mostrame", "mostramelo", "mostramela", "mostralo", "mostrala", "mostrar",
    "llevame", "llevame ahi", "abrila", "abrilo", "enfocala", "enfocalo",
    "enfoca", "vamos ahi", "ir ahi", "traeme", "mostrame eso",
    "mostrame la ventana", "quiero verlo", "quiero verla",
})

# Asked *of voice-loop*, never of a window: what is in the queue, and how the
# whole board looks. They work from any mode — including busy, where the hotkey
# is the only way in — so they are classified like any other control phrase.
PENDINGS_PHRASES = frozenset({
    "pendientes", "los pendientes", "las pendientes", "dame los pendientes",
    "damelos", "dame pendientes", "que tengo pendiente", "que tengo pendientes",
    "que hay pendiente", "que hay pendientes", "que queda pendiente",
    "que esta pendiente", "que me falta", "que me queda", "que tengo",
    "lista de pendientes", "leeme los pendientes", "decime los pendientes",
    "quien me espera", "que ventanas me esperan",
    # Asked back at the countdown, which is exactly when it is asked.
    "cual queda", "cual falta", "cuales quedan", "cuales faltan",
    "cuantas quedan", "cuantos quedan", "cuantas faltan", "cuantos faltan",
    "cuantas me quedan", "cuantos me quedan", "cual me queda", "que queda",
    "que quedan", "que falta", "que faltan", "cuantas ventanas quedan",
    "cual es el que queda", "cual es la que queda",
})

STATUS_PHRASES = frozenset({
    "estado", "el estado", "estado general", "como venimos", "como vamos",
    "como va todo", "como viene la mano", "que esta pasando", "que pasa",
    "que hay", "situacion", "como estamos", "resumen", "panorama",
})

# A question that no phrase above matched, but that is plainly about
# voice-loop's own business rather than about anybody's code. Both halves have
# to be true — it opens like a question *and* it names something only
# voice-loop has — because the fallback is a read-back, and a read-back on
# every sentence was rejected out loud: the cost of asking is one round, the
# cost of not asking is a stray turn typed into somebody's session.
QUESTION_WORDS = frozenset({
    "que", "cual", "cuales", "cuanto", "cuanta", "cuantos", "cuantas",
    "quien", "quienes", "como", "donde", "cuando",
})

SYSTEM_WORDS = frozenset({
    "queda", "quedan", "quedaban", "falta", "faltan", "pendiente", "pendientes",
    "ventana", "ventanas", "sesion", "sesiones", "cola", "dijiste", "decias",
    "escuchaste", "entendiste", "escuchando", "hablando", "anunciaste",
    "leiste", "preguntaste", "microfono", "mic", "espera", "esperan",
})

MAX_SYSTEMWARD_WORDS = 6

# The two halves of "¿es el nombre, o te lo mando a la ventana?". Checked in
# this order because "mandalo" is a confirmation everywhere else in this file
# and here it means the opposite half — you are not confirming the name, you
# are telling me where the phrase was going.
ANSWER_NAME = "name"
ANSWER_WINDOW = "window"

FOR_THE_WINDOW_PHRASES = frozenset({
    "a la ventana", "para la ventana", "la ventana", "mandalo a la ventana",
    "mandala a la ventana", "es para la ventana", "mandalo", "mandala",
    "mandale", "mandaselo", "a la sesion", "para la sesion", "es la respuesta",
    "es para la ventana no", "es un mensaje", "es mi respuesta",
})

AS_THE_NAME_PHRASES = frozenset({
    "el nombre", "es el nombre", "nombre", "un nombre", "es un nombre",
    "el nombre si", "si el nombre", "ese nombre", "ponele ese nombre",
    "llamala asi", "llamalo asi", "asi", "asi esta bien", "es el nombre si",
    "es el nombre de la ventana", "el de la ventana",
})

EXPLAIN_VERBS = (
    "explicame", "explicamela", "explicamelo", "explica", "explicar",
    "contame", "detallame", "ampliame", "que es", "de que se trata",
)

# Dropped from the edges before looking for a number or an option keyword.
LEADING_FILLER = frozenset({
    "la", "el", "lo", "las", "los", "opcion", "opciones", "numero", "respuesta",
    "elijo", "elegi", "elegimos", "quiero", "dame", "poneme", "andale", "esa", "ese",
})
TRAILING_FILLER = frozenset({"por", "favor", "porfa", "gracias", "nomas", "dale", "obvio"})

# A distinctive first word is enough to pick an option ("postgres" for
# "Postgres — ya corre en staging"). Short words are not distinctive.
MIN_KEYWORD_CHARS = 4

_NON_WORD = re.compile(r"[^0-9a-z\s]+")
_WHITESPACE = re.compile(r"\s+")
# "uno y tres", "uno, dos" — the only way to name several options at once.
_SEPARATOR = re.compile(r"\s*[,;]\s*|\s+[yY]\s+")


@dataclass(frozen=True)
class Intent:
    kind: str
    index: int | None = None
    text: str = ""
    indexes: tuple[int, ...] = ()

    @property
    def is_control(self) -> bool:
        return self.kind not in (KIND_TEXT, KIND_SILENCE)


def _selection(indexes: Sequence[int], text: str) -> Intent:
    ordered = tuple(sorted(dict.fromkeys(indexes)))
    return Intent(KIND_SELECT, index=ordered[0], text=text, indexes=ordered)


def fold(text: str) -> str:
    """Lowercase, unaccented, punctuation-free — the form every set is written in."""
    decomposed = unicodedata.normalize("NFD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _WHITESPACE.sub(" ", _NON_WORD.sub(" ", stripped.lower())).strip()


def _strip_filler(tokens: Sequence[str]) -> list[str]:
    out = list(tokens)
    while out and out[0] in LEADING_FILLER:
        out.pop(0)
    while out and out[-1] in TRAILING_FILLER:
        out.pop()
    return out


def as_number(token: str) -> int | None:
    if token in NUMBERS:
        return NUMBERS[token]
    if token.isdigit():
        value = int(token)
        return value if value > 0 else None
    return None


def option_keys(labels: Sequence[str]) -> dict[str, int]:
    """Folded label -> index: the whole label, any leading part, any distinct word.

    Three shapes, because there are three ways to name an option out loud. The
    whole label is what you say reading it off the screen. A **leading part** is
    what you say repeating what you *heard*, since options are spoken short —
    "Probalo sin hotkeys primero (Recomendado)" is read out without the aside.
    A single word is what you say when only one option had that word in it.

    Anything two options could both mean is dropped rather than guessed.
    """
    keys: dict[str, int] = {}
    collisions: set[str] = set()

    def offer(key: str, index: int) -> None:
        if not key or key in collisions:
            return
        if key in keys and keys[key] != index:
            del keys[key]
            collisions.add(key)
            return
        keys[key] = index

    for index, label in enumerate(labels, start=1):
        folded = fold(str(label))
        if not folded:
            continue
        words = folded.split(" ")
        offer(folded, index)
        for count in range(1, len(words)):
            # A one-word prefix has to be distinctive; two words already are.
            if count > 1 or len(words[0]) >= MIN_KEYWORD_CHARS:
                offer(" ".join(words[:count]), index)
        for word in words[1:]:
            if len(word) >= MIN_KEYWORD_CHARS:
                offer(word, index)
    return keys


def _phrase(tokens: Sequence[str]) -> str:
    return " ".join(tokens)


def _explain_index(folded: str, option_count: int) -> int | None:
    for verb in EXPLAIN_VERBS:
        if not folded.startswith(verb + " "):
            continue
        rest = _strip_filler(folded[len(verb) :].split())
        if len(rest) != 1:
            continue
        index = as_number(rest[0])
        if index is not None and 1 <= index <= option_count:
            return index
    return None


def _resolve_one(phrase: str, labels: Sequence[str], keys: Mapping[str, int]) -> int | None:
    core = _strip_filler(phrase.split())
    if len(core) == 1:
        number = as_number(core[0])
        if number is not None and 1 <= number <= len(labels):
            return number
    return keys.get(_phrase(core))


def _multi_selection(raw: str, labels: Sequence[str], keys: Mapping[str, int]) -> list[int]:
    """'uno y tres' -> [1, 3]. Every part has to resolve, or none of it counts.

    Split before folding: folding is what turns the comma in "uno, dos" into a
    space, and by then the two answers look like one phrase.
    """
    pieces = [fold(part) for part in _SEPARATOR.split(raw)]
    parts = [part for part in pieces if part]
    if len(parts) < 2:
        return []
    found: list[int] = []
    for part in parts:
        index = _resolve_one(part, labels, keys)
        if index is None:
            return []
        found.append(index)
    return found


def confirmation_tail(text: str) -> str | None:
    """What a yes carried with it. `None` when the utterance is not a yes at all.

    `""` is a bare confirmation. Anything else is what came after it — the name
    the answer proposes, or a sentence that was meant for the window and got a
    "dale" in front of it. The caller decides which, because only the caller
    knows what was asked.
    """
    tokens = [
        token for token in fold(text).split() if token not in ("por", "favor", "porfa")
    ]
    if not tokens or tokens[0] not in CONFIRM_LEAD:
        return None
    rest = tokens[1:]
    while rest and rest[0] in CONFIRM_LEAD:
        rest.pop(0)
    return _phrase(rest)


def name_or_window(text: str) -> str | None:
    """Which half of "¿es el nombre, o te lo mando a la ventana?" was answered.

    `None` is neither — a fresh utterance, which the caller treats the way a
    read-back treats one anywhere else: it replaces what was being asked about.
    A bare "sí" takes the first half, because that is the half the question
    opens with; a bare "no" takes the second.
    """
    folded = fold(text)
    if not folded:
        return None
    bare = _phrase([token for token in folded.split() if token not in ("por", "favor", "porfa")])
    if folded in FOR_THE_WINDOW_PHRASES or bare in FOR_THE_WINDOW_PHRASES:
        return ANSWER_WINDOW
    if folded in AS_THE_NAME_PHRASES or bare in AS_THE_NAME_PHRASES:
        return ANSWER_NAME
    kind = parse(text).kind
    if kind == KIND_CONFIRM:
        return ANSWER_NAME
    if kind == KIND_CANCEL:
        return ANSWER_WINDOW
    return None


def looks_systemward(text: str) -> bool:
    """Might this short question have been meant for voice-loop, not the window?

    Only a hint, and a deliberately narrow one: the caller reads it back and
    asks rather than acting on it. "cuál queda" is a phrase above and never
    gets here; "cuántas ventanas quedan abiertas" is not, and typing it into
    somebody's session is the failure this exists to avoid.
    """
    words = fold(text).split()
    if not words or len(words) > MAX_SYSTEMWARD_WORDS:
        return False
    if words[0] not in QUESTION_WORDS:
        return False
    return any(word in SYSTEM_WORDS for word in words)


@dataclass(frozen=True)
class NearMiss:
    """A control phrase the recognizer very nearly gave us."""

    kind: str
    phrase: str
    ratio: float


# Every set matched whole, in one place, so a near miss can be looked for
# against exactly the same vocabulary an exact match is.
CONTROL_SETS: tuple[tuple[frozenset, str], ...] = (
    (GIVE_PHRASES, KIND_GIVE),
    (LATER_PHRASES, KIND_LATER),
    (CONFIRM_PHRASES, KIND_CONFIRM),
    (CANCEL_PHRASES, KIND_CANCEL),
    (REPEAT_PHRASES, KIND_REPEAT),
    (SKIP_PHRASES, KIND_SKIP),
    (WAIT_PHRASES, KIND_WAIT),
    (SHOW_PHRASES, KIND_SHOW),
    (PENDINGS_PHRASES, KIND_PENDINGS),
    (STATUS_PHRASES, KIND_STATUS),
)

# Measured against the phrases people actually dictate: "dame al pendiente"
# scores 0.89 against "dame los pendientes", and the closest a real instruction
# gets is "cerrá la ventana" at 0.74 against "mostrame la ventana". Above this
# line it is a command the recognizer fumbled; below it, it is your sentence.
NEAR_MISS_RATIO = 0.8

# A near miss is a short phrase said wrong. A long one is a sentence that
# happens to rhyme with something, and asking about every sentence was rejected
# out loud — the point of asking is that it is rare enough to be worth it.
MAX_NEAR_MISS_WORDS = 5


def nearest_control(text: str) -> NearMiss | None:
    """The command this almost was, when it is not a command at all.

    "dame al pendiente" is what Deepgram heard for "dame los pendientes", and
    the whole cost of the miss was paid downstream: it matched nothing, so it
    was a sentence, so it was typed into somebody's Claude session. Asking
    costs one round. Not asking costs a turn nobody meant to take, in a window
    that was already waiting on an answer.

    `None` for anything that matched exactly (there is nothing to ask about)
    and for anything long enough to be a real instruction.
    """
    folded = fold(text)
    if not folded or len(folded.split()) > MAX_NEAR_MISS_WORDS:
        return None
    best: NearMiss | None = None
    for phrases, kind in CONTROL_SETS:
        if folded in phrases:
            return None
        for phrase in phrases:
            score = SequenceMatcher(None, folded, phrase, autojunk=False).ratio()
            if score < NEAR_MISS_RATIO:
                continue
            if best is None or score > best.ratio:
                best = NearMiss(kind=kind, phrase=phrase, ratio=score)
    return best


def parse(
    text: str,
    options: Sequence[str] = (),
    *,
    heard: bool = True,
    multi: bool = False,
) -> Intent:
    """Classify one utterance. `options` are the labels the payload offers."""
    raw = (text or "").strip()
    folded = fold(raw)
    if not heard or not folded:
        return Intent(KIND_SILENCE, text=raw)

    # Control phrases are matched whole, with the politeness trimmed off.
    tokens = folded.split()
    bare = _phrase([token for token in tokens if token not in ("por", "favor", "porfa")])
    for phrases, kind in (
        (GIVE_PHRASES, KIND_GIVE),
        (LATER_PHRASES, KIND_LATER),
        (CONFIRM_PHRASES, KIND_CONFIRM),
        (CANCEL_PHRASES, KIND_CANCEL),
        (REPEAT_PHRASES, KIND_REPEAT),
        (SKIP_PHRASES, KIND_SKIP),
        (WAIT_PHRASES, KIND_WAIT),
        (SHOW_PHRASES, KIND_SHOW),
        (PENDINGS_PHRASES, KIND_PENDINGS),
        (STATUS_PHRASES, KIND_STATUS),
    ):
        if folded in phrases or bare in phrases:
            return Intent(kind, text=raw)

    labels = [str(option) for option in options]
    if labels:
        index = _explain_index(folded, len(labels))
        if index is not None:
            return Intent(KIND_EXPLAIN, index=index, text=raw)

        keys = option_keys(labels)
        single = _resolve_one(folded, labels, keys)
        if single is not None:
            return _selection([single], raw)
        if multi:
            # Only a multi-select menu can take "uno y tres"; anywhere else that
            # is a sentence, and sentences go through untouched. Tried second so
            # an option actually labelled "Rojo y Verde" still wins as itself.
            several = _multi_selection(raw, labels, keys)
            if several:
                return _selection(several, raw)

    return Intent(KIND_TEXT, text=raw)
