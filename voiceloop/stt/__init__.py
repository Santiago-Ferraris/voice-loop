"""Speech-to-text engines, looked up by `speech_to_text.provider`.

Every name the config validator accepts resolves to something here — including
the two that are not written yet. A planned provider raises a sentence that
says so instead of falling back to a different engine behind your back:
silently transcribing with the wrong model is how you end up debugging
accuracy that was never yours to begin with.
"""

from __future__ import annotations

from .base import (
    SttEngine,
    SttError,
    SttNotConfigured,
    SttNotImplemented,
    Transcript,
)
from .deepgram import DeepgramStt
from .mock import MockStt
from .openai import OpenAiStt

__all__ = [
    "DeepgramStt",
    "MockStt",
    "OpenAiStt",
    "SttEngine",
    "SttError",
    "SttNotConfigured",
    "SttNotImplemented",
    "Transcript",
    "PLANNED",
    "create",
]

# Accepted in config, not shipped. `deepgram_ws` is the streaming socket that
# moves endpointing server-side and retires the local VAD.
PLANNED = {
    "whisper-cpp": "local whisper.cpp transcription",
    "deepgram_ws": "Deepgram streaming with server-side endpointing",
}


def create(config, *, transport=None) -> SttEngine:
    provider = str(config.get("speech_to_text.provider", "deepgram"))
    if provider in PLANNED:
        raise SttNotImplemented(
            f"speech_to_text.provider={provider}: {PLANNED[provider]} is planned, "
            "not implemented — use deepgram or openai"
        )
    if provider == "deepgram":
        return DeepgramStt.from_config(config, transport=transport)
    if provider == "openai":
        return OpenAiStt.from_config(config, transport=transport)
    if provider == "mock":
        return MockStt.from_config(config)
    raise SttNotImplemented(f"speech_to_text.provider={provider}: unknown provider")
