"""Deepgram `nova-3`, batch REST.

Four query parameters carry the whole result, and three of them are not
defaults — they came out of benchmarking code-switched speech and none of them
is optional:

* `language=multi` — the input is Spanish sentences with English technical
  terms, in the same breath.
* `keyterm=` (repeated) — without it "mergealo" comes back as "MGalo". The
  terms are your config vocabulary *plus the names of the sessions that are
  live right now*, injected per request, because "contestale a draft-mode" only
  transcribes if the recognizer has heard of `draft-mode`.
* `smart_format=false` and `numerals=false` — with the defaults on, "fijate
  primero" arrives as "fijate 1º". That string is what Claude would receive.
  There is a test whose only job is to fail if either flag goes away.

Plain `urllib`, like the summarizer: no SDK, one dependency for the whole
daemon. The streaming socket (`deepgram_ws`), which moves the endpointing
server-side and retires the local VAD, slots in behind the same contract.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .base import SttEngine, SttError, SttNotConfigured, Transcript

API_URL = "https://api.deepgram.com/v1/listen"

# Deepgram rejects an over-long query string; the tail of the vocabulary is the
# least useful part of it anyway.
MAX_KEYTERMS = 100

Transport = Callable[[str, dict, bytes, float], bytes]


def _urllib_transport(url: str, headers: dict, body: bytes, timeout: float) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


def normalize_keyterms(terms: Sequence[str]) -> list[str]:
    """Trim, drop blanks, de-duplicate case-insensitively, keep the given order."""
    seen: set[str] = set()
    out: list[str] = []
    for term in terms or ():
        cleaned = " ".join(str(term or "").split())
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) >= MAX_KEYTERMS:
            break
    return out


@dataclass
class DeepgramStt(SttEngine):
    name: str = "deepgram"
    model: str = "nova-3"
    language: str = "multi"
    api_key: str | None = None
    timeout: float = 20.0
    base_keyterms: tuple[str, ...] = ()
    transport: Transport = _urllib_transport
    url: str = API_URL
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_config(cls, config, *, transport: Transport | None = None) -> "DeepgramStt":
        keyterms = config.get("keyterms") or []
        if not isinstance(keyterms, (list, tuple)):
            keyterms = []
        return cls(
            model=str(config.get("speech_to_text.deepgram.model", "nova-3")),
            language=str(config.get("speech_to_text.deepgram.language", "multi")),
            api_key=config.api_key("deepgram"),
            timeout=float(config.get("speech_to_text.timeout_seconds", 20)),
            base_keyterms=tuple(str(term) for term in keyterms),
            transport=transport or _urllib_transport,
        )

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def build_url(self, keyterms: Sequence[str] = ()) -> str:
        params: list[tuple[str, str]] = [
            ("model", self.model),
            ("language", self.language),
            # Both false on purpose. See the module docstring.
            ("smart_format", "false"),
            ("numerals", "false"),
        ]
        for key, value in sorted(self.extra.items()):
            params.append((str(key), str(value)))
        for term in normalize_keyterms([*self.base_keyterms, *keyterms]):
            params.append(("keyterm", term))
        return f"{self.url}?{urllib.parse.urlencode(params)}"

    def transcribe(self, audio_path: Path | str, keyterms: Sequence[str] = ()) -> Transcript:
        if not self.available:
            raise SttNotConfigured("DEEPGRAM_API_KEY is not set")
        try:
            body = Path(audio_path).read_bytes()
        except OSError as exc:
            raise SttError(f"cannot read {audio_path}: {exc}") from exc

        headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": "audio/wav",
        }
        try:
            raw = self.transport(self.build_url(keyterms), headers, body, self.timeout)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise SttError(f"deepgram unreachable: {exc}") from exc
        return self.parse(raw)

    def parse(self, raw: bytes) -> Transcript:
        try:
            data = json.loads(raw)
            alternative = data["results"]["channels"][0]["alternatives"][0]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise SttError(f"unexpected deepgram response: {exc}") from exc
        confidence = alternative.get("confidence")
        return Transcript(
            text=self._clean(alternative.get("transcript")),
            confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
            provider=self.name,
            raw=data if isinstance(data, dict) else {},
        )
