"""OpenAI `gpt-4o-transcribe`, batch REST.

Tied with Deepgram on accuracy for code-switched speech once both are given
the vocabulary; Deepgram is the default only because of free credit and real
streaming. This is the drop-in if you already have an `OPENAI_API_KEY` and do
not want a second account.

Two provider differences that matter above the contract:

* The vocabulary is a **prompt**, not a parameter — the keyterms are joined
  into one hint sentence.
* There is no confidence field. Asking for `logprobs` gets one per token, and
  the geometric mean of those probabilities is what the delivery gate reads. If
  the model returns none, confidence is `None`: no opinion, no read-back.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .base import SttEngine, SttError, SttNotConfigured, Transcript

API_URL = "https://api.openai.com/v1/audio/transcriptions"

PROMPT_PREFIX = "Vocabulario del proyecto: "
MAX_PROMPT_TERMS = 100

Transport = Callable[[str, dict, bytes, float], bytes]


def _urllib_transport(url: str, headers: dict, body: bytes, timeout: float) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


def build_prompt(keyterms: Sequence[str]) -> str:
    terms = []
    seen: set[str] = set()
    for term in keyterms or ():
        cleaned = " ".join(str(term or "").split())
        if not cleaned or cleaned.lower() in seen:
            continue
        seen.add(cleaned.lower())
        terms.append(cleaned)
        if len(terms) >= MAX_PROMPT_TERMS:
            break
    return f"{PROMPT_PREFIX}{', '.join(terms)}." if terms else ""


def encode_multipart(fields: Sequence[tuple[str, str]], filename: str, audio: bytes) -> tuple[str, bytes]:
    """(content-type, body). Field names are fixed constants, never user input."""
    boundary = f"----voiceloop{uuid.uuid4().hex}"
    marker = f"--{boundary}\r\n".encode("utf-8")
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.append(marker)
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        )
        chunks.append(f"{value}\r\n".encode("utf-8"))
    chunks.append(marker)
    # The name is generated, not taken from the recording's path.
    chunks.append(
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: audio/wav\r\n\r\n".encode("utf-8")
    )
    chunks.append(audio)
    chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


def confidence_from_logprobs(entries) -> float | None:
    """Geometric mean of the per-token probabilities."""
    if not isinstance(entries, list) or not entries:
        return None
    values = [
        entry["logprob"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("logprob"), (int, float))
    ]
    if not values:
        return None
    return math.exp(sum(values) / len(values))


@dataclass
class OpenAiStt(SttEngine):
    name: str = "openai"
    model: str = "gpt-4o-transcribe"
    language: str = "es"
    api_key: str | None = None
    timeout: float = 20.0
    base_keyterms: tuple[str, ...] = ()
    transport: Transport = _urllib_transport
    url: str = API_URL

    @classmethod
    def from_config(cls, config, *, transport: Transport | None = None) -> "OpenAiStt":
        keyterms = config.get("keyterms") or []
        if not isinstance(keyterms, (list, tuple)):
            keyterms = []
        return cls(
            model=str(config.get("speech_to_text.openai.model", "gpt-4o-transcribe")),
            language=str(config.get("speech_to_text.openai.language", "es")),
            api_key=config.api_key("openai"),
            timeout=float(config.get("speech_to_text.timeout_seconds", 20)),
            base_keyterms=tuple(str(term) for term in keyterms),
            transport=transport or _urllib_transport,
        )

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def build_fields(self, keyterms: Sequence[str] = ()) -> list[tuple[str, str]]:
        fields = [
            ("model", self.model),
            ("response_format", "json"),
            ("include[]", "logprobs"),
        ]
        if self.language:
            fields.append(("language", self.language))
        prompt = build_prompt([*self.base_keyterms, *keyterms])
        if prompt:
            fields.append(("prompt", prompt))
        return fields

    def transcribe(self, audio_path: Path | str, keyterms: Sequence[str] = ()) -> Transcript:
        if not self.available:
            raise SttNotConfigured("OPENAI_API_KEY is not set")
        try:
            audio = Path(audio_path).read_bytes()
        except OSError as exc:
            raise SttError(f"cannot read {audio_path}: {exc}") from exc

        content_type, body = encode_multipart(self.build_fields(keyterms), "reply.wav", audio)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": content_type}
        try:
            raw = self.transport(self.url, headers, body, self.timeout)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise SttError(f"openai unreachable: {exc}") from exc
        return self.parse(raw)

    def parse(self, raw: bytes) -> Transcript:
        try:
            data = json.loads(raw)
            text = data["text"]
        except (ValueError, KeyError, TypeError) as exc:
            raise SttError(f"unexpected openai response: {exc}") from exc
        return Transcript(
            text=self._clean(text),
            confidence=confidence_from_logprobs(data.get("logprobs")),
            provider=self.name,
            raw=data if isinstance(data, dict) else {},
        )
