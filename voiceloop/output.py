"""Where the voice comes out — headphones, or the speakers the mic can hear.

The microphone is now open while `say` is talking, which makes this the
difference between two behaviours rather than a detail:

* **Headphones.** Nothing of ours reaches the microphone, so anything it hears
  is you, and the first syllable you speak stops the sentence mid-word.
* **Speakers.** The microphone hears our own voice. Treating that as an
  interruption would mean the assistant shuts itself up every time it opens its
  mouth, so barge-in is off and the echo is filtered out of the transcript
  instead (see `echo.py`).

Which one is in use has to be answered without the user configuring anything —
headphones come and go through the day — so it is probed from the system and
cached for a few seconds. `system_profiler SPAudioDataType` answers in under
0.2 s and is on every Mac; there is no CoreAudio binding to add.

The answer is deliberately biased. "I could not tell" is answered *speakers*:
assuming speakers costs an echo filter that would have been harmless anyway,
while assuming headphones lets the assistant hear itself and act on it.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

log = logging.getLogger("voiceloop.output")

PROBE_ARGV = ("system_profiler", "-json", "SPAudioDataType")
PROBE_TIMEOUT = 4.0

TRANSPORT_BLUETOOTH = "coreaudio_device_type_bluetooth"

# Substrings of a device name or output source that mean "on your head".
# `system_profiler` reports the built-in jack as an output source called
# "Headphones" and AirPods as a bluetooth transport, and a USB headset as
# neither — which is why the name is checked too.
PRIVATE_HINTS = (
    "headphone",
    "headset",
    "earphone",
    "earbud",
    "airpod",
    "auricular",
    "beats",
)

Runner = Callable[[], bytes]


def _run_probe() -> bytes:
    return subprocess.run(
        list(PROBE_ARGV),
        capture_output=True,
        timeout=PROBE_TIMEOUT,
        check=True,
    ).stdout


def _devices(payload: Any) -> list[Mapping[str, Any]]:
    """Every audio device `system_profiler` listed, whatever it nested them in."""
    found: list[Mapping[str, Any]] = []
    if isinstance(payload, Mapping):
        if "_name" in payload and any(key.startswith("coreaudio_") for key in payload):
            found.append(payload)
        for value in payload.values():
            found.extend(_devices(value))
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        for entry in payload:
            found.extend(_devices(entry))
    return found


def default_output(raw: bytes | str) -> tuple[str, str]:
    """(name, transport) of the device the system is playing through.

    `("", "")` when the payload says nothing useful — which the caller reads as
    "assume speakers", not as an error.
    """
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return "", ""
    for device in _devices(payload):
        if device.get("coreaudio_default_audio_output_device") != "spaudio_yes":
            continue
        source = str(device.get("coreaudio_output_source") or "")
        name = str(device.get("_name") or "")
        # The source names the jack ("Headphones"); the device names the box.
        label = source if source and not source.startswith("spaudio_") else name
        return label, str(device.get("coreaudio_device_transport") or "")
    return "", ""


def is_private(name: str, transport: str) -> bool:
    """Does the sound go somewhere the microphone cannot hear it?"""
    if transport == TRANSPORT_BLUETOOTH:
        return True
    folded = (name or "").lower()
    return any(hint in folded for hint in PRIVATE_HINTS)


@dataclass
class OutputProbe:
    """`private()` — is the voice going into headphones right now?

    Cached for `refresh_seconds`, because it is asked once per announcement and
    the answer only changes when someone puts headphones on. A probe that fails
    (no `system_profiler`, a timeout, a machine that is not a Mac) answers
    speakers and is not retried until the cache expires.
    """

    refresh_seconds: float = 30.0
    # Resolved at call time, not bound here: the suite replaces the module
    # function so no test's behaviour depends on this Mac's headphones.
    runner: Runner | None = None
    enabled: bool = True
    _cached: bool = field(default=False, init=False, repr=False)
    _checked_at: float = field(default=0.0, init=False, repr=False)
    probes: int = field(default=0, init=False, repr=False)

    @classmethod
    def from_config(cls, config, *, runner: Runner | None = None) -> "OutputProbe":
        return cls(
            refresh_seconds=float(config.get("barge_in.refresh_seconds", 30)),
            enabled=bool(config.get("barge_in.enabled", True)),
            runner=runner,
        )

    def private(self, *, now: float | None = None) -> bool:
        if not self.enabled:
            return False
        moment = time.monotonic() if now is None else now
        if self.probes and moment - self._checked_at < self.refresh_seconds:
            return self._cached
        self._checked_at = moment
        self.probes += 1
        self._cached = self._probe()
        return self._cached

    def _probe(self) -> bool:
        try:
            raw = (self.runner or _run_probe)()
        except Exception:  # noqa: BLE001 - a missing probe is "speakers", not a crash
            log.debug("could not read the audio output device", exc_info=True)
            return False
        name, transport = default_output(raw)
        private = is_private(name, transport)
        log.debug("audio output %r (%s) private=%s", name, transport, private)
        return private
