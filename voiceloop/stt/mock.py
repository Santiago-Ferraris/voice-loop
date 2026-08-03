"""A recognizer that returns what you told it to.

Two users: the end-to-end test, which drives the whole announce → mic →
transcribe → deliver pipeline without a microphone, and `provider: mock` in
`config.local.yml`, which lets you exercise routing and delivery on a real
machine — mic still opens, nothing is sent to a paid API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .base import SttEngine, Transcript

DEFAULT_REPLY = "dale, mandale"


@dataclass
class MockStt(SttEngine):
    name: str = "mock"
    replies: list = field(default_factory=list)
    confidence: float | None = 0.99
    default: str = DEFAULT_REPLY
    calls: list = field(default_factory=list)

    @classmethod
    def from_config(cls, config) -> "MockStt":
        replies = config.get("speech_to_text.mock.replies") or []
        if not isinstance(replies, (list, tuple)):
            replies = []
        return cls(
            replies=[str(reply) for reply in replies],
            default=str(config.get("speech_to_text.mock.default", DEFAULT_REPLY)),
        )

    def transcribe(self, audio_path: Path | str, keyterms: Sequence[str] = ()) -> Transcript:
        self.calls.append((str(audio_path), list(keyterms)))
        if self.replies:
            reply = self.replies.pop(0)
        else:
            reply = self.default
        if isinstance(reply, Transcript):
            return reply
        return Transcript(text=self._clean(reply), confidence=self.confidence, provider=self.name)
