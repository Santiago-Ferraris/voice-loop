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

_INNER_HYPHEN = re.compile(r"(?<=\w)-(?=\w)")
_WHITESPACE = re.compile(r"\s+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")


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


def enumerate_options(labels: Sequence[str], *, multi_select: bool = False) -> str:
    """'Opciones: uno: …, dos: …' — numbered out loud, because that is the answer."""
    if not labels:
        return ""
    enumerated = ", ".join(
        f"{number_word(index)}: {_clip(str(label), MAX_LABEL_CHARS)}"
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

    speak = True
    if item.type == TYPE_NOTIFICATION and not notification_events:
        speak = False
    return Announcement(text=text, chime=blocking_chime, speak=speak)
