"""The speech-to-text contract.

One method, one return type, no provider details above this line. That is what
lets the local `silencedetect` cutoff be swapped for Deepgram's server-side
endpointing later without touching the daemon: a streaming engine implements
the same `transcribe`, it just stops recording earlier.

`confidence` is the only field with policy attached — the delivery gate reads
it back to you instead of sending when the recognizer was unsure. Providers
that report nothing usable return `None`, which means "no opinion", not "bad":
a `None` never triggers a read-back on its own, but the destructive-phrase
blacklist still applies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence


class SttError(RuntimeError):
    """Transcription failed. The item goes back to pending, nothing is delivered."""


class SttNotConfigured(SttError):
    """The provider exists but cannot run — usually a missing API key."""


class SttNotImplemented(SttError):
    """A provider name the config accepts but this build does not ship."""


@dataclass(frozen=True)
class Transcript:
    text: str
    confidence: float | None = None
    provider: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def empty(self) -> bool:
        return not self.text.strip()


class SttEngine:
    """Base class. Subclasses implement `transcribe` and set `name`."""

    name = "stt"

    @property
    def available(self) -> bool:
        return True

    def transcribe(self, audio_path: Path | str, keyterms: Sequence[str] = ()) -> Transcript:
        raise NotImplementedError

    @staticmethod
    def _clean(text: Any) -> str:
        return " ".join(str(text or "").split())
