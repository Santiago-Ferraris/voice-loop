"""What you meant, when the lexicon has never heard of it.

The phrase lists in `intents.py` are exact and instant, and they are what
resolve almost everything — but they only know the phrasings somebody thought
to write down. Measured against sixteen natural ways of saying things this
user says every day, fourteen fell through: `"give it to me"`, `"later"`,
`"skip it"`, `"show me"`, `"status"`, `"dale contame"`, `"ok dame"`, `"not
now"`, `"push it back"`, `"what's pending"`, `"tell me"`, `"read it"`, `"hold
on"`, `"what do I have"`. Every one of them arrived as *text*, which means
every one of them was typed into somebody's Claude session.

So this is the second half of a hybrid: whatever the lexicon resolves is
resolved without a packet leaving the machine, and only what it does not
understand is handed to `gpt-4o-mini`.

Three things make that safe rather than clever:

* **"Ninguno" is an answer.** A classifier that always picks something turns
  every dictated sentence into a random command. The prompt is built around
  refusing, and an empty `actions` list is the expected reply for anything that
  reads like an instruction for a coding session.
* **It is on a two-second leash.** One attempt, a hard timeout, and every
  failure path — no key, network down, malformed JSON — degrades to exactly
  what the lexicon alone would have done. It is an improvement, never a
  dependency.
* **It returns a list.** "Ok dámelo, y también abrí una ventana nueva y hacé X"
  is three things in one breath, and a contract that can only carry one of them
  would silently drop two.

Transport is `urllib` behind an injectable callable, the same shape as
`summarize.py`: no SDK, one dependency for the whole daemon, and a test suite
that can assert nothing touched the network.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from . import intents

log = logging.getLogger("voiceloop.classify")

API_URL = "https://api.openai.com/v1/chat/completions"

# Where a plan came from — logged, and asserted on in the tests, because "the
# lexicon did this without a network call" is a property worth keeping.
SOURCE_LEXICON = "lexicon"
SOURCE_LLM = "llm"
SOURCE_DOUBTFUL = "doubtful"
SOURCE_UNAVAILABLE = "unavailable"

# The intents the model may answer with. Kept as one table because it is both
# the prompt and the validator: a name that is not here is not an action.
INTENT_HELP: Mapping[str, str] = {
    intents.KIND_GIVE: "que le lea lo que quiere la ventana que acaba de avisar",
    intents.KIND_LATER: "posponer eso, mandarlo al final de la cola",
    intents.KIND_SKIP: "saltearlo, dejarlo donde está y seguir",
    intents.KIND_SHOW: "llevar el foco a esa ventana",
    intents.KIND_REPEAT: "repetir lo último que dijo el asistente",
    intents.KIND_WAIT: "esperar un momento, no es ni sí ni no",
    intents.KIND_PENDINGS: "leer la lista de pendientes",
    intents.KIND_STATUS: "el estado general: ventanas abiertas, trabajando, esperando",
    intents.KIND_CONFIRM: "sí, dale, confirmar lo último que preguntó el asistente",
    intents.KIND_CANCEL: "no, cancelar lo último que preguntó el asistente",
    intents.KIND_OPEN: "abrir una ventana nueva; en `text` va lo que hay que hacer ahí",
    intents.KIND_TELL: (
        "hablarle a una ventana por su nombre: `target` es el nombre y `text` el mensaje"
    ),
    intents.KIND_TEXT: (
        "dictado para la ventana en curso — usalo sólo si la frase es claramente "
        "una instrucción de trabajo dirigida a esa sesión"
    ),
}

SYSTEM_PROMPT = """\
Clasificás lo que una persona le dice por voz a un asistente que maneja varias \
sesiones de Claude Code abiertas en terminales. Habla español rioplatense \
mezclado con inglés técnico, y conjuga verbos ingleses en español (mergear, \
testear, deployar).

Respondés SIEMPRE un objeto JSON con una sola clave `actions`, que es una lista. \
Cada elemento tiene `intent`, y opcionalmente `text` y `target`.

Intents posibles:
{catalogue}

Reglas:
- Una frase puede pedir VARIAS cosas ("dámelo y abrí una ventana nueva y corré \
los tests"): devolvé una acción por cada una, EN EL ORDEN EN QUE LAS DIJO.
- Si la frase NO es una orden para el asistente sino trabajo para la sesión que \
está esperando —"mergealo cuando pasen los tests", "revisá el índice", "corré \
los tests de nuevo"— devolvé {{"actions": []}}. La lista vacía es una respuesta \
correcta y frecuente: preferí devolverla antes que inventar un intent.
- El `text` de cada acción es lo que dijo la persona, no algo que completes vos. \
Nunca repartas una sola frase en varias acciones con textos distintos que ella \
no dijo.
- No expliques nada, no agregues otras claves.

Ejemplos:
"give it to me" -> {{"actions": [{{"intent": "give"}}]}}
"not now, push it back" -> {{"actions": [{{"intent": "later"}}]}}
"what's pending" -> {{"actions": [{{"intent": "pendings"}}]}}
"ok dame, y abrí una ventana nueva y hacé el rebase" -> {{"actions": [{{"intent": \
"give"}}, {{"intent": "open", "text": "hacé el rebase"}}]}}
"decile a inbox realtime que espere" -> {{"actions": [{{"intent": "tell", \
"target": "inbox realtime", "text": "esperá"}}]}}
"mergealo cuando pasen los tests" -> {{"actions": []}}\
"""

# The list that was just read out loud, handed over with the phrase that was
# said on top of it. Without it "la última" names nothing: the model has no idea
# what was said one sentence ago, and a target it invents is a turn typed into
# somebody else's window.
PENDINGS_HEADER = "Ventanas pendientes, en el orden en que se las acabo de leer:"

# The counterexample is in the prompt on purpose, spelled out as JSON. Told
# only in prose ("no adivines"), the model kept doing the one thing prose does
# not name: it never answered with an empty target, it answered with *three*
# full ones — "decile que haga eso" came back as one `tell` per pending window,
# each carrying that window's summary rewritten as an order nobody dictated.
PENDINGS_RULE = (
    "Si la frase se refiere a una de esas ventanas por posición (\"la última\", "
    "\"la tercera\") o por lo que dice de ella (\"la de darwin e4\", \"la del "
    "alias\"), poné el NOMBRE EXACTO de la lista en `target`. Si no podés saber "
    "a cuál se refiere, devolvé UNA SOLA acción con `target` vacío: no adivines, "
    "y no la repartas entre todas.\n"
    "El `text` es siempre lo que dijo la persona, nunca el resumen de la ventana: "
    "si lo estás sacando de la lista, la frase no era para esa ventana.\n"
    "Contraejemplo, sobre una lista de darwin e5, cl audio y darwin e4: \"decile "
    "que haga eso\" NO es {\"actions\": [{\"intent\": \"tell\", \"target\": \"darwin "
    "e5\", \"text\": \"resolvé los conflictos\"}, {\"intent\": \"tell\", \"target\": "
    "\"cl audio\", \"text\": \"probá con trabajo real\"}, {\"intent\": \"tell\", "
    "\"target\": \"darwin e4\", \"text\": \"dejalo fijo en 4.8\"}]} — son tres "
    "instrucciones inventadas y ninguna la dijo. Es {\"actions\": [{\"intent\": "
    "\"tell\", \"target\": \"\", \"text\": \"hacé eso\"}]}.\n"
    "Varias ventanas sólo cuando lo pidió (\"decile a todas que paren\"), y ahí "
    "el `text` es EL MISMO en todas."
)

Transport = Callable[[str, dict, bytes, float], bytes]

# Past this it is dictation, not a command, and paying a round trip to be told
# so is a second of silence on every long sentence.
MAX_WORDS = 40


def pendings_block(pendings: Sequence[tuple[str, str]]) -> str:
    """The queue, numbered the way it was spoken, plus how to point at one."""
    lines = [PENDINGS_HEADER]
    for index, entry in enumerate(pendings, start=1):
        name, summary = entry[0], entry[1]
        lines.append(f"{index}. {name} — {summary}" if summary else f"{index}. {name}")
    lines.append(PENDINGS_RULE)
    return "\n".join(lines)


def _urllib_transport(url: str, headers: dict, body: bytes, timeout: float) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


@dataclass(frozen=True)
class Action:
    """One thing to do. A phrase can ask for several."""

    kind: str
    text: str = ""
    target: str = ""
    index: int | None = None
    indexes: tuple[int, ...] = ()

    @classmethod
    def of(cls, intent: intents.Intent) -> "Action":
        return cls(
            kind=intent.kind,
            text=intent.text,
            index=intent.index,
            indexes=intent.indexes,
        )

    def as_intent(self) -> intents.Intent:
        return intents.Intent(
            kind=self.kind, index=self.index, text=self.text, indexes=self.indexes
        )


@dataclass(frozen=True)
class Plan:
    """Everything one utterance asked for, in the order it asked."""

    actions: tuple[Action, ...] = ()
    source: str = SOURCE_LEXICON
    # What we think it was, when we are not sure enough to just do it. Two
    # things put a plan here and both are about the *transcript*, not the
    # meaning: a phrase that was nearly a control word ("jamelo" for "dámelo",
    # measured), and a recognizer that said out loud it was unsure. Either way
    # the caller asks — "Entendí: jamelo. ¿Querés que te lo lea?" — and never
    # acts, because both readings of a bad transcript are cheap to ask about
    # and expensive to guess.
    guess: Action | None = None

    @property
    def first(self) -> Action | None:
        return self.actions[0] if self.actions else None

    @classmethod
    def of(cls, intent: intents.Intent, source: str = SOURCE_LEXICON, **kwargs) -> "Plan":
        return cls(actions=(Action.of(intent),), source=source, **kwargs)


def parse_actions(payload: str) -> list[Action] | None:
    """The model's reply, or `None` when it is not a reply at all.

    An empty list is a real answer — "this is not a command" — and is returned
    as an empty list, not as `None`.
    """
    try:
        fields = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(fields, Mapping):
        return None
    raw = fields.get("actions")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return None
    actions: list[Action] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        kind = str(entry.get("intent") or "").strip()
        if kind not in INTENT_HELP:
            log.debug("dropping unknown intent %r", kind)
            continue
        actions.append(
            Action(
                kind=kind,
                text=" ".join(str(entry.get("text") or "").split()),
                target=" ".join(str(entry.get("target") or "").split()),
            )
        )
    return actions


@dataclass
class Classifier:
    """`gpt-4o-mini` on a two-second leash, for what the lexicon missed."""

    model: str = "gpt-4o-mini"
    timeout: float = 2.0
    api_key: str | None = None
    enabled: bool = True
    transport: Transport = _urllib_transport
    max_words: int = MAX_WORDS
    calls: int = field(default=0, init=False, repr=False)

    @classmethod
    def from_config(cls, config, *, transport: Transport | None = None) -> "Classifier":
        provider = str(config.get("understanding.provider", "openai"))
        return cls(
            model=str(config.get("understanding.model", "gpt-4o-mini")),
            timeout=float(config.get("understanding.timeout_seconds", 2)),
            max_words=int(config.get("understanding.max_words", MAX_WORDS)),
            api_key=config.api_key("openai"),
            enabled=provider == "openai",
            transport=transport or _urllib_transport,
        )

    @property
    def available(self) -> bool:
        return bool(self.enabled and self.api_key)

    def catalogue(self) -> str:
        return "\n".join(f"- {name}: {help}" for name, help in INTENT_HELP.items())

    def build_body(
        self,
        text: str,
        windows: Sequence[str] = (),
        pendings: Sequence[tuple[str, str]] = (),
    ) -> bytes:
        blocks = []
        if pendings:
            blocks.append(pendings_block(pendings))
        if windows:
            # The names are what "decile a inbox realtime que…" is matched
            # against, and a model that has not been told they exist will
            # cheerfully invent a target that names nothing.
            blocks.append(f"Ventanas abiertas: {', '.join(windows)}")
        blocks.append(text)
        content = "\n\n".join(blocks)
        payload = {
            "model": self.model,
            "max_tokens": 200,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT.format(catalogue=self.catalogue()),
                },
                {"role": "user", "content": content},
            ],
        }
        return json.dumps(payload).encode("utf-8")

    def classify(
        self,
        text: str,
        windows: Sequence[str] = (),
        pendings: Sequence[tuple[str, str]] = (),
    ) -> list[Action] | None:
        """The actions this phrase asks for. `None` means "could not tell".

        `None` and `[]` are different answers and the caller treats them
        differently: `[]` is the model saying this is dictation, `None` is the
        model not having been reachable, which degrades to the lexicon's own
        verdict.

        `pendings` is the list that was just read out loud, in order, and it is
        only ever passed when one was: it is the whole context "la última" has.
        """
        if not self.available:
            return None
        words = (text or "").split()
        if not words:
            return None
        if len(words) > self.max_words:
            # Said out loud, because a take this long is nearly always our own
            # voice that the echo filter failed to subtract, and a silent
            # `return None` is what made that impossible to see in the log.
            log.info(
                "too long to classify (%d words, limit %d): %r",
                len(words),
                self.max_words,
                text,
            )
            return None
        self.calls += 1
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            raw = self.transport(
                API_URL, headers, self.build_body(text, windows, pendings), self.timeout
            )
            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"]
        except (
            urllib.error.URLError,
            OSError,
            TimeoutError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
        ) as exc:
            # The type, not just the message: half of these say nothing on their
            # own — a bare `KeyError` prints one word, and a real bug inside a
            # transport reads exactly like a provider that timed out. It cost a
            # round of diagnosis once; the traceback is one `--debug` away.
            log.info(
                "classifier unavailable, falling back to the lexicon: %s: %s",
                type(exc).__name__,
                exc,
            )
            log.debug("classifier failure detail", exc_info=True)
            return None
        except Exception as exc:  # noqa: BLE001 - never break the microphone
            log.exception("classifier failed: %s", exc)
            return None
        return parse_actions(content if isinstance(content, str) else "")
