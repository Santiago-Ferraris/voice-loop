"""One-sentence summaries of what a session is waiting for.

`gpt-4o-mini` over plain `urllib` — no SDK, so the daemon has exactly one
third-party dependency (PyYAML) and starts instantly.

The queue must never be held hostage by this. Timeout is short, there is a
single retry, and *every* failure path — no key, network down, malformed
response, provider disabled — degrades to a fixed phrase and the announcement
goes out anyway. A late announcement is a bug; a missing one is a disaster.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from .naming import fold, slugify

API_URL = "https://api.openai.com/v1/chat/completions"

FALLBACK_SUMMARY = "terminó y te espera"

SYSTEM_PROMPT = (
    "Resumís en español, en una sola frase de máximo {max_words} palabras, qué está "
    "esperando de la persona una sesión de Claude Code, a partir de su último mensaje. "
    "Lo va a leer un sintetizador de voz: sin markdown, sin comillas, sin rutas de "
    "archivo, sin código y sin emojis. Si el mensaje termina en una pregunta, resumí "
    "la pregunta. Si no pregunta nada, contá qué hizo la sesión: terminó de listar los "
    "archivos, corrió los tests y pasaron, aplicó la migración. Nunca describas la "
    "ausencia de una pregunta: decir que no hay pregunta, que no hay expectativa o que "
    "no espera nada no le dice a nadie qué pasó en esa ventana. No agregues preámbulo."
)

# The same request, asked for one field more. A window's name and the summary of
# what it is waiting for come from the same paragraph, and the second call would
# read exactly the same input to answer a smaller question.
NAMING_PROMPT = (
    SYSTEM_PROMPT
    + " Respondés un objeto JSON con dos claves. `summary`: ese resumen. `slug`: un "
    "nombre corto para esta ventana, de dos a cuatro palabras en español, todo en "
    "minúsculas, sin acentos, sin guiones y sin puntuación. El slug describe en qué "
    "trabaja la ventana, no lo que pregunta ahora, y tiene que poder decirse en voz "
    "alta: nada de rutas, siglas sueltas ni nombres de archivo."
)

Transport = Callable[[str, dict, bytes, float], bytes]

_WHITESPACE = re.compile(r"\s+")

# "No hay expectativa de la persona en este mensaje" — what came back on the
# first real run for a turn that had simply finished doing something. It is a
# comment on the prompt, not a summary: it says nothing about what the window
# did, which is the one thing the announcement exists to say.
_ABOUT_NOTHING = re.compile(
    r"^(?:"
    r"no (?:hay|tiene|existe|se plantea|aparece) (?:ninguna |ningun |nada )?"
    r"(?:pregunta|expectativa|consulta|duda|solicitud|peticion|requerimiento)"
    r"|no (?:pregunta|espera|pide|solicita|requiere|necesita) nada"
    r"|(?:el mensaje|el ultimo mensaje|la sesion|esta sesion) no "
    r"(?:pregunta|espera|pide|requiere|plantea)"
    r")"
)


class SummaryUnavailable(Exception):
    """The provider could not be reached or answered with something unusable."""


@dataclass(frozen=True)
class Summary:
    """What one call came back with. `slug` is empty unless a name was asked for."""

    text: str
    slug: str = ""


def _urllib_transport(url: str, headers: dict, body: bytes, timeout: float) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


def clean(text: str, max_words: int) -> str:
    """Flatten to a single speakable line and enforce the word budget."""
    flat = _WHITESPACE.sub(" ", (text or "").replace("`", "")).strip()
    flat = flat.strip('"“”«»')
    words = flat.split(" ")
    if max_words > 0 and len(words) > max_words:
        flat = " ".join(words[:max_words])
    return flat.strip().rstrip(".")


def describes_no_question(text: str) -> bool:
    """Is this a summary of the turn, or a remark about there not being a question?"""
    return bool(_ABOUT_NOTHING.match(fold(text)))


@dataclass
class Summarizer:
    model: str = "gpt-4o-mini"
    max_words: int = 12
    timeout: float = 5.0
    api_key: str | None = None
    enabled: bool = True
    transport: Transport = _urllib_transport
    attempts: int = 2

    @classmethod
    def from_config(cls, config, *, transport: Transport | None = None) -> "Summarizer":
        provider = config.get("summaries.provider", "openai")
        return cls(
            model=config.get("summaries.model", "gpt-4o-mini"),
            max_words=int(config.get("summaries.max_words", 12)),
            timeout=float(config.get("summaries.timeout_seconds", 5)),
            api_key=config.api_key("openai"),
            enabled=provider == "openai",
            transport=transport or _urllib_transport,
        )

    @property
    def available(self) -> bool:
        return bool(self.enabled and self.api_key)

    def build_body(self, text: str, *, naming: bool = False) -> bytes:
        prompt = NAMING_PROMPT if naming else SYSTEM_PROMPT
        payload: dict = {
            "model": self.model,
            "max_tokens": 200 if naming else 120,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": prompt.format(max_words=self.max_words)},
                {"role": "user", "content": text},
            ],
        }
        if naming:
            payload["response_format"] = {"type": "json_object"}
        return json.dumps(payload).encode("utf-8")

    def _content(self, text: str, *, naming: bool = False) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        raw = self.transport(API_URL, headers, self.build_body(text, naming=naming), self.timeout)
        try:
            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise SummaryUnavailable(f"unexpected response: {exc}") from exc
        return content if isinstance(content, str) else ""

    def _usable(self, summary: str) -> str:
        """One retry, then the fallback — for both ways a summary can be no summary."""
        if not summary:
            raise SummaryUnavailable("empty summary")
        if describes_no_question(summary):
            raise SummaryUnavailable(f"no summary, just the lack of a question: {summary!r}")
        return summary

    def _call(self, text: str) -> str:
        return self._usable(clean(self._content(text), self.max_words))

    def _call_named(self, text: str) -> Summary:
        """The summary and a proposed window name, from one request."""
        try:
            fields = json.loads(self._content(text, naming=True))
        except ValueError as exc:
            raise SummaryUnavailable(f"not JSON: {exc}") from exc
        if not isinstance(fields, dict):
            raise SummaryUnavailable("expected a JSON object")
        summary = self._usable(clean(str(fields.get("summary") or ""), self.max_words))
        # A bad name is not worth a retry — the summary is the part the
        # announcement cannot do without, and no slug just means no offer.
        return Summary(text=summary, slug=slugify(str(fields.get("slug") or "")))

    def _attempt(self, call, fallback):
        last: Exception | None = None
        for _ in range(max(1, self.attempts)):
            try:
                return call()
            except (SummaryUnavailable, urllib.error.URLError, OSError, TimeoutError) as exc:
                last = exc
            except Exception as exc:  # noqa: BLE001 - never break the queue
                last = exc
        del last
        return fallback

    def summarize(self, text: str) -> str:
        """Best-effort summary. Returns `FALLBACK_SUMMARY` instead of raising."""
        if not self.available or not (text or "").strip():
            return FALLBACK_SUMMARY
        return self._attempt(lambda: self._call(text), FALLBACK_SUMMARY)

    def summarize_and_name(self, text: str) -> Summary:
        """Best-effort summary *and* a name to offer. Never raises, never blocks.

        A degraded call gives up the name and keeps the announcement: a window
        that is waiting for you must be announced whatever the provider is
        doing, and an offer to name it is the first thing worth losing.
        """
        if not self.available or not (text or "").strip():
            return Summary(text=FALLBACK_SUMMARY)
        return self._attempt(lambda: self._call_named(text), Summary(text=FALLBACK_SUMMARY))
