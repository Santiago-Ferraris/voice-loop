"""The words you actually use, taken from the words you actually used.

`keyterms` is what stops the recognizer inventing domain vocabulary — without
it "mergealo" comes back as "MGalo" — and it has been a hand-kept list in
`config.local.yml`, which is a list that is wrong the week after you write it.
It also cannot contain the terms that matter most, because those are whatever
you happen to be working on this month.

So it is derived instead. Claude Code already keeps every prompt you have ever
typed, one JSON line at a time, under `~/.claude/projects/*/*.jsonl`. Counting
the words in your own messages produces exactly the list: `pr`, `test`,
`issue`, `draft`, `stage`, `fix`, `lambda`, `repo`, `feature`, `merge`,
`testear`, `queries`, `deployment`, `mergear`, `mergeado`.

Two filters do the whole job:

* **Only your side of the conversation.** Assistant turns and tool results are
  a hundred times the volume and none of it is how *you* speak.
* **Only what is not ordinary Spanish.** `COMMON` is a stop list of function
  words and everyday verbs; what survives it is, by construction, the jargon —
  which is precisely what a recognizer needs told.

The daemon merges this with the names of the windows that are open right now
(`Daemon.keyterms`), because a session name only transcribes if the recognizer
has heard of it, and those change every time you open a tab.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

log = logging.getLogger("voiceloop.vocabulary")

DEFAULT_TRANSCRIPT_GLOB = "~/.claude/projects/*/*.jsonl"

# Deepgram rejects an over-long query string, and the tail of the list is the
# least useful part of it anyway.
DEFAULT_LIMIT = 80
DEFAULT_MIN_COUNT = 3
MIN_TERM_CHARS = 2
MAX_TERM_CHARS = 24

# Lines Claude writes into the user's own turn: slash-command envelopes, the
# resumed-session caveat, hook output. None of it was said by anybody.
_INJECTED = re.compile(
    r"^\s*(?:<(?:command-name|command-message|command-args|local-command|"
    r"user-prompt-submit-hook|system-reminder|bash-input)|Caveat:)",
    re.IGNORECASE,
)

_NON_WORD = re.compile(r"[^0-9a-z]+")
_HAS_DIGIT = re.compile(r"\d")

# Ordinary Spanish and the English glue around it. What survives is the jargon.
COMMON = frozenset("""
a acá ahi ahora al algo alguna algunas alguno algunos alla alli alto ambos and
antes any aquel aquella aquello aqui arriba asi aun aunque bajo bien but cada
casi como con contra cosa cosas creo cual cuales cuando cuanto cual da dale dar
de debe debajo decir dejar del demas demasiado dentro desde despues dice dicho
dio donde dos e el ella ellas ello ellos en encima entonces entre era eran eres
es esa esas ese eso esos esta estaba estan estar estas este esto estos estoy
falta fin for fue fueron gracias ha habia hace hacen hacer hacia hago han hasta
hay he hecho hizo hola hoy igual in is la las le les listo lo los luego mas me
mejor menos mi mientras mio misma mismo mucho muchos muy nada nadie ni no nos
nosotros nuestra nuestro nueva nuevo nunca o of ok on or otra otras otro otros
para pero pesar poco podemos poder podes podria por porque pues que queda quedo
queres quien quiere quiero se sea segun sen ser si sido siempre sin sino sobre
solo son soy su sus tal tambien tampoco tan tanto te tenemos tener tenes tengo
the tiene tienen toda todas todo todos tu tus un una uno unos usa usar va vamos
van vas ver vez viene vos voy y ya yo
""".split())

# Words that are ordinary Spanish *here* — the ones a person says to a terminal
# all day, which are frequent and carry nothing a recognizer needs told.
COMMON_WORK = frozenset("""
abri abrir acordate agrega agregar anda andar arregla arreglar borra borrar
busca buscar cambia cambiar cerra cerrar chequea checa corre correr corri
dejalo escribi fijate hace haga hagamos leelo lee leer mandale manda mira mostra
mostrame necesito pone poner probalo proba probar quiero revisa revisar saca
sacar segui seguir termina terminar tira tirar usa vemos veo
""".split())

STOP = COMMON | COMMON_WORK


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.lower()


def transcript_paths(pattern: str = DEFAULT_TRANSCRIPT_GLOB) -> list[Path]:
    """Every Claude transcript on this machine, newest first."""
    expanded = Path(pattern).expanduser()
    root = expanded.anchor or "/"
    relative = str(expanded).removeprefix(root)
    try:
        found = list(Path(root).glob(relative))
    except (OSError, ValueError):
        return []
    return sorted(found, key=lambda path: _mtime(path), reverse=True)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _content_text(content: Any) -> str:
    """The text of one user turn. Tool results and images are not speech."""
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts = [
            str(block.get("text") or "")
            for block in content
            if isinstance(block, Mapping) and block.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part)
    return ""


def user_messages(path: Path | str) -> Iterator[str]:
    """Everything *you* typed in one transcript, envelopes and sidechains left out."""
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, Mapping) or entry.get("type") != "user":
                continue
            if entry.get("isSidechain") or entry.get("isMeta"):
                continue
            message = entry.get("message")
            if not isinstance(message, Mapping):
                continue
            text = _content_text(message.get("content")).strip()
            if not text or _INJECTED.match(text):
                continue
            yield text


def terms_in(text: str) -> Iterator[str]:
    for token in _NON_WORD.sub(" ", fold(text)).split():
        if not MIN_TERM_CHARS <= len(token) <= MAX_TERM_CHARS:
            continue
        if token in STOP or _HAS_DIGIT.search(token):
            continue
        yield token


def count(texts: Iterable[str]) -> Counter:
    tally: Counter = Counter()
    for text in texts:
        tally.update(terms_in(text))
    return tally


def extract(
    paths: Sequence[Path | str],
    *,
    min_count: int = DEFAULT_MIN_COUNT,
    limit: int = DEFAULT_LIMIT,
) -> list[str]:
    """The vocabulary those transcripts are written in, most used first."""
    tally: Counter = Counter()
    for path in paths:
        tally.update(count(user_messages(path)))
    ranked = [
        term
        for term, seen in sorted(tally.items(), key=lambda pair: (-pair[1], pair[0]))
        if seen >= min_count
    ]
    return ranked[:limit]


def store_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / "keyterms.json"


def save(state_dir: Path | str, terms: Sequence[str], *, now: float) -> Path:
    path = store_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"generated_at": now, "terms": list(terms)}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def load(state_dir: Path | str) -> list[str]:
    """The last extraction, or nothing at all. Never raises: this is a nicety."""
    try:
        data = json.loads(store_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    terms = data.get("terms") if isinstance(data, Mapping) else None
    if not isinstance(terms, Sequence) or isinstance(terms, (str, bytes)):
        return []
    return [str(term) for term in terms if str(term).strip()]


def age_seconds(state_dir: Path | str, *, now: float) -> float:
    """How long ago it was recomputed. Infinite when it never was."""
    try:
        data = json.loads(store_path(state_dir).read_text(encoding="utf-8"))
        generated = float(data["generated_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return float("inf")
    return max(0.0, now - generated)


def refresh(
    state_dir: Path | str,
    *,
    now: float,
    pattern: str = DEFAULT_TRANSCRIPT_GLOB,
    min_count: int = DEFAULT_MIN_COUNT,
    limit: int = DEFAULT_LIMIT,
    files: int = 200,
) -> list[str]:
    """Recompute and store. Bounded by `files`, newest first — years of history
    would be read on every refresh to learn nothing about this month."""
    paths = transcript_paths(pattern)[:files]
    terms = extract(paths, min_count=min_count, limit=limit)
    save(state_dir, terms, now=now)
    log.info("vocabulary: %d term(s) from %d transcript(s)", len(terms), len(paths))
    return terms
