"""Turning a queue item into something worth hearing.

Everything spoken goes through here, so the wording stays consistent and every
phrase is unit-testable without a speaker. Two rules that only matter out loud:

* **Numbers are words.** "uno: …, dos: …" — a menu read as "1: …, 2: …" is
  fine on screen and mush in a Spanish voice.
* **Phonetics before hyphens.** English technical terms are rewritten from the
  config dictionary (longest match wins, case-insensitive), and only then are
  the remaining hyphens turned into spaces, so a hyphenated entry in the
  dictionary still matches and `draft-mode-changes` is read as three words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .delivery import PLAN_LABELS
from .events import TYPE_MENU, TYPE_MILESTONE, TYPE_NOTIFICATION, TYPE_STOP
from .summarize import FALLBACK_SUMMARY

NUMBER_WORDS = (
    "uno",
    "dos",
    "tres",
    "cuatro",
    "cinco",
    "seis",
    "siete",
    "ocho",
    "nueve",
    "diez",
)

MAX_OPTIONS = 8
MAX_LABEL_CHARS = 70
MAX_MESSAGE_CHARS = 160
MAX_SPOKEN_LABEL_WORDS = 5

# A label ending on one of these was cut mid-phrase, and they say nothing alone.
DANGLING_WORDS = frozenset({
    "de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas", "y", "o",
    "u", "a", "al", "en", "con", "para", "por", "que", "sin", "su", "sus", "lo",
    "como", "desde", "sobre", "entre",
})

_INNER_HYPHEN = re.compile(r"(?<=\w)-(?=\w)")
_WHITESPACE = re.compile(r"\s+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
# Written for the eye, not the ear: "(Recomendado)", "[beta]", and everything
# hanging off a dash — "Postgres — ya corre en staging".
_ASIDE = re.compile(r"\s*[\(\[\{][^)\]\}]*[\)\]\}]")
_TRAILING_CLAUSE = re.compile(r"\s*(?:[;—–]|\s-\s).*$")


@dataclass(frozen=True)
class Announcement:
    text: str
    chime: str | None = None
    speak: bool = True

    @property
    def silent(self) -> bool:
        return not self.speak or not self.text


def number_word(index: int) -> str:
    """1-based ordinal as a spoken word; digits past ten."""
    if 1 <= index <= len(NUMBER_WORDS):
        return NUMBER_WORDS[index - 1]
    return str(index)


def build_phonetic(mapping: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    """Dictionary entries sorted longest-first, which is what makes `|` greedy."""
    if not isinstance(mapping, Mapping):
        return []
    pairs = [
        (str(key), str(value))
        for key, value in mapping.items()
        if str(key).strip() and value is not None
    ]
    return sorted(pairs, key=lambda pair: (-len(pair[0]), pair[0]))


def apply_phonetic(text: str, mapping: Mapping[str, Any] | None) -> str:
    pairs = build_phonetic(mapping)
    if not text or not pairs:
        return text
    lookup = {key.lower(): value for key, value in pairs}
    pattern = re.compile(
        r"(?<!\w)(" + "|".join(re.escape(key) for key, _ in pairs) + r")(?!\w)",
        re.IGNORECASE,
    )
    return pattern.sub(lambda match: lookup[match.group(1).lower()], text)


def despine(text: str) -> str:
    """`draft-mode-changes` -> `draft mode changes`."""
    return _INNER_HYPHEN.sub(" ", text or "")


def normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", (text or "").replace("\n", " ")).strip()


def speakable(text: str, phonetic: Mapping[str, Any] | None = None) -> str:
    return normalize(despine(apply_phonetic(normalize(text), phonetic)))


def remaining_phrase(remaining: int) -> str:
    """'Quedan N' — nothing at all when the queue empties out."""
    if remaining <= 0:
        return ""
    if remaining == 1:
        return "Queda uno"
    return f"Quedan {remaining}"


def join_sentences(parts: Sequence[str]) -> str:
    """Join clauses without ever producing '?.' — a question keeps its own mark."""
    out = ""
    for part in parts:
        clause = (part or "").strip()
        if not clause:
            continue
        if out:
            out += " " if out[-1] in ".?!" else ". "
        out += clause
    return out


def _clip(text: str, limit: int) -> str:
    flat = normalize(text)
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def first_heading(markdown: str) -> str:
    """First markdown heading of a plan, falling back to its first line."""
    for line in (markdown or "").splitlines():
        match = _HEADING.match(line)
        if match:
            return normalize(match.group(1))
    for line in (markdown or "").splitlines():
        stripped = normalize(line)
        if stripped:
            return stripped
    return ""


def _menu_options(options: Any) -> list[str]:
    if not isinstance(options, Sequence) or isinstance(options, (str, bytes)):
        return []
    labels: list[str] = []
    for option in options[:MAX_OPTIONS]:
        if isinstance(option, Mapping):
            label = option.get("label") or option.get("description") or ""
        else:
            label = option
        label = _clip(str(label), MAX_LABEL_CHARS)
        if label:
            labels.append(label)
    return labels


def short_label(label: str) -> str:
    """A menu option the way four of them in a row have to sound.

    Read whole, four options are twenty-five seconds of audio for a decision
    that takes five, and "(Recomendado)" is pure noise spoken. So asides go,
    what hangs off a dash goes, and what is left is cut to its first few words.

    Nothing is lost by it: the *full* label is still what a spoken keyword is
    matched against, and "explicame la dos" still reads the whole thing out.
    """
    core = normalize(_TRAILING_CLAUSE.sub("", normalize(_ASIDE.sub(" ", str(label or "")))))
    words = core.split(" ")[:MAX_SPOKEN_LABEL_WORDS]
    while words and words[-1].lower().strip(".,;:") in DANGLING_WORDS:
        words.pop()
    return " ".join(words) or normalize(str(label or ""))


def enumerate_options(labels: Sequence[str], *, multi_select: bool = False) -> str:
    """'Opciones: uno: …, dos: …' — numbered out loud, because that is the answer."""
    if not labels:
        return ""
    enumerated = ", ".join(
        f"{number_word(index)}: {_clip(short_label(label), MAX_LABEL_CHARS)}"
        for index, label in enumerate(labels[:MAX_OPTIONS], start=1)
    )
    lead = "Podés elegir varias" if multi_select else "Opciones"
    return f"{lead}: {enumerated}"


def describe_question(
    question: str,
    labels: Sequence[str],
    *,
    multi_select: bool = False,
    position: int = 1,
    total: int = 1,
) -> str:
    """One question of a menu, read the way it has to be answered."""
    parts: list[str] = []
    if total > 1:
        parts.append(f"Pregunta {number_word(position)} de {number_word(total)}")
    parts.append(_clip(question, 220) or "te está preguntando algo")
    options = enumerate_options(labels, multi_select=multi_select)
    if options:
        parts.append(options)
    return join_sentences(parts)


def describe_menu(payload: Mapping[str, Any]) -> str:
    """Spoken form of an `AskUserQuestion` / `ExitPlanMode` hook payload."""
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, Mapping) else {}

    plan = tool_input.get("plan")
    if isinstance(plan, str) and plan.strip():
        heading = first_heading(plan)
        opening = f"pide aprobar un plan: {heading}" if heading else "pide aprobar un plan"
        # The plan menu's rows are Claude's, not the payload's — but they still
        # have to be read out, or there is no way to answer by number.
        return join_sentences([opening, enumerate_options(PLAN_LABELS)])

    questions = tool_input.get("questions")
    if isinstance(questions, Sequence) and not isinstance(questions, (str, bytes)):
        entries = [q for q in questions if isinstance(q, Mapping)]
    else:
        entries = []
    if not entries:
        return "te está preguntando algo"

    first = entries[0]
    question = _clip(str(first.get("question") or first.get("header") or ""), 220)
    labels = _menu_options(first.get("options"))
    parts = [question] if question else ["te está preguntando algo"]
    options = enumerate_options(labels, multi_select=bool(first.get("multiSelect")))
    if options:
        parts.append(options)
    if len(entries) > 1:
        extra = len(entries) - 1
        parts.append("Y una pregunta más" if extra == 1 else f"Y {extra} preguntas más")
    return join_sentences(parts)


def ago_phrase(seconds: float) -> str:
    """How long a window has been waiting, rounded to something worth hearing."""
    minutes = int(max(0.0, seconds) // 60)
    if minutes < 1:
        return "recién"
    if minutes == 1:
        return "hace un minuto"
    if minutes < 60:
        return f"hace {number_word(minutes)} minutos"
    hours = minutes // 60
    if hours == 1:
        return "hace una hora"
    if hours < 24:
        return f"hace {number_word(hours)} horas"
    days = hours // 24
    return "hace un día" if days == 1 else f"hace {number_word(days)} días"


def describe_pendings(entries: Sequence[tuple[str, str, str]], *, limit: int = 5) -> str:
    """The queue, read out loud and numbered — because the number is the answer.

    `entries` are (name, summary, how long it has been waiting). Capped: past
    five you have stopped counting anyway, and the tail is one more sentence
    rather than a minute of enumeration.
    """
    if not entries:
        return "No tenés nada pendiente."
    total = len(entries)
    head = "Tenés un pendiente" if total == 1 else f"Tenés {number_word(total)} pendientes"
    parts = [head]
    for index, entry in enumerate(entries[:limit], start=1):
        name, summary, ago = entry
        clause = f"{number_word(index)}: {name or 'una sesión'}"
        if summary:
            clause += f", {summary}"
        if ago:
            clause += f", {ago}"
        parts.append(clause)
    extra = total - limit
    if extra > 0:
        parts.append("Y uno más" if extra == 1 else f"Y {number_word(extra)} más")
    return join_sentences(parts)


def _count_phrase(count: int, singular: str, plural: str, none: str) -> str:
    if count <= 0:
        return none
    if count == 1:
        return singular
    return plural.format(count=number_word(count))


def describe_status(
    *,
    windows: int,
    working: int,
    waiting: int,
    milestones: Sequence[tuple[str, int]] = (),
    paused: bool = False,
    busy: bool = False,
) -> str:
    """Where everything stands, in one breath: open, working, waiting on you."""
    parts = [
        _count_phrase(windows, "Hay una ventana abierta", "Hay {count} ventanas abiertas",
                      "No hay ventanas abiertas"),
        _count_phrase(working, "una trabajando", "{count} trabajando", "ninguna trabajando"),
        _count_phrase(waiting, "una te espera", "{count} te esperan", "ninguna te espera"),
    ]
    for label, count in milestones:
        parts.append(
            f"una con {label}" if count == 1 else f"{number_word(count)} con {label}"
        )
    if paused:
        parts.append("Estoy en pausa")
    elif busy:
        parts.append("Estoy en modo ocupado")
    return join_sentences(parts)


def name_question(slug: str, phonetic: Mapping[str, Any] | None = None) -> str:
    """The offer to christen a window Claude called `darwin-21`."""
    spoken = speakable(slug, phonetic)
    return f"¿La llamo {spoken}?" if spoken else ""


def build(
    item,
    *,
    name: str,
    summary: str | None = None,
    remaining: int = 0,
    phonetic: Mapping[str, Any] | None = None,
    blocking_chime: str | None = None,
    milestone_chime: str | None = None,
    notification_events: bool = True,
    naming_offer: str = "",
) -> Announcement:
    """Compose the full announcement for a queue item."""
    spoken_name = speakable(name, phonetic)

    if item.type == TYPE_MILESTONE:
        # Milestones never speak — a chime is the whole point.
        label = item.payload.get("label") or "hito"
        return Announcement(text=str(label), chime=milestone_chime, speak=False)

    if item.type == TYPE_MENU:
        body = describe_menu(item.payload)
    elif item.type == TYPE_NOTIFICATION:
        message = _clip(str(item.payload.get("message") or ""), MAX_MESSAGE_CHARS)
        body = message or "te necesita"
    elif item.type == TYPE_STOP:
        body = summary or FALLBACK_SUMMARY
    else:
        body = summary or FALLBACK_SUMMARY

    sentence = f"{spoken_name}: {speakable(body, phonetic)}".strip()
    if sentence and sentence[-1] not in ".?!":
        sentence += "."
    tail = remaining_phrase(remaining)
    text = f"{sentence} {tail}." if tail else sentence
    # Last, so it is the question the open microphone is answering.
    offer = name_question(naming_offer, phonetic)
    if offer:
        text = join_sentences([text, offer])

    speak = True
    if item.type == TYPE_NOTIFICATION and not notification_events:
        speak = False
    return Announcement(text=text, chime=blocking_chime, speak=speak)
