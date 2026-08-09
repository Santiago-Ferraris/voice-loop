"""The microphone — `ffmpeg -f avfoundation`, 16 kHz mono WAV to a temp file.

No SDK and no CoreAudio binding: `ffmpeg` is already on the box, it opens the
default input device by index, and it can do the voice activity detection for
us with the `silencedetect` filter, so rev 1 needs nothing else. Rev 2 replaces
that filter with Deepgram's server-side endpointing over a streaming socket —
the cutoff moves, the rest of this module does not.

Three things the recorder has to get right:

* **Opening the device is slow.** avfoundation takes over a second to hand over
  the first sample. The "speak now" chime must fire when capture actually
  starts, not when the process is spawned, or the first word is lost — so
  `record()` calls back the moment ffmpeg reports its output stream.
* **Silence is only a cutoff once you have spoken.** `silencedetect` reports
  the silence *before* your first word too. Stopping on that would close the
  mic instantly every time. Speech is what a `silence_end` means — or a
  `silence_start` far enough into the take that something must have preceded
  it.
* **Saying nothing is a normal outcome.** The announce opens the mic whether
  you meant to answer or not. No speech inside `speech_timeout` is not an
  error: the item goes back to pending and the queue moves on.

Termination is SIGTERM, which makes ffmpeg finalize the WAV header before
exiting — the file is playable, and its non-zero exit status is expected.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

log = logging.getLogger("voiceloop.audio")

DEFAULT_BINARY = "ffmpeg"
DEFAULT_DEVICE = ":0"
SAMPLE_RATE = 16_000

# WAV header plus ~0.3 s of 16-bit mono at 16 kHz. Anything shorter is noise.
MIN_USABLE_BYTES = 44 + int(SAMPLE_RATE * 2 * 0.3)

REASON_SILENCE = "silence"
REASON_TOGGLE = "toggle"
REASON_TIMEOUT = "timeout"
REASON_MAX = "max-duration"
REASON_EXITED = "exited"
REASON_FAILED = "failed"

_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")
_OUTPUT_OPEN = re.compile(r"^Output #\d")

Spawn = Callable[[Sequence[str]], Awaitable[Any]]


class AudioUnavailable(RuntimeError):
    """ffmpeg is missing, or the capture device could not be opened."""


class MicConsentPending(AudioUnavailable):
    """ffmpeg started and then never delivered a sample.

    Opening avfoundation takes about a second. A process that has not managed
    it in eight is not slow, it is parked on the macOS microphone consent
    prompt — and under launchd that prompt may never be presented at all, since
    the responsible process is the agent and not the terminal you granted.

    Separate from its parent because it is the one audio failure with a fix the
    user has to perform, so it is worth saying out loud instead of "no pude
    abrir el micrófono".
    """


@dataclass(frozen=True)
class Recording:
    path: Path
    seconds: float
    spoke: bool
    reason: str

    @property
    def usable(self) -> bool:
        if not self.spoke:
            return False
        try:
            return self.path.stat().st_size >= MIN_USABLE_BYTES
        except OSError:
            return False


class SilenceTracker:
    """Turns `silencedetect` chatter into two booleans: opened, and cut off.

    `armed` is the third state, and it exists for the microphone that is open
    while we are talking. On speakers everything `silencedetect` reports during
    the announcement is our own voice, so the take must not end on it — and the
    pause the moment `say` stops is not you finishing a sentence, it is the
    sentence we just finished. `arm()` draws the line: whatever was heard
    before it is remembered (`ever_heard`, so the audio is still worth
    transcribing) but cannot cut, and the first cut after it has to be preceded
    by real speech.
    """

    def __init__(self, *, min_speech_seconds: float = 0.6, armed: bool = True):
        self.min_speech_seconds = min_speech_seconds
        self.heard_speech = False
        self.ever_heard = False
        self.opened = False
        self.cut_off = False
        self.armed = armed
        # After a late arming, leading silence is not measured against the
        # start of the take any more — the timestamps are already far past it.
        # Nothing cuts until a `silence_end` proves somebody started talking.
        self._needs_speech_first = not armed

    def arm(self) -> None:
        """Everything heard so far was ours. Start listening for you."""
        if self.armed:
            return
        self.armed = True
        self.heard_speech = False
        self.cut_off = False
        self._needs_speech_first = True

    def feed(self, line: str) -> None:
        text = (line or "").strip()
        if not text:
            return
        if not self.opened and _OUTPUT_OPEN.match(text):
            self.opened = True
            return
        if _SILENCE_END.search(text):
            # Silence ended, so something was said in between.
            self.ever_heard = True
            self.heard_speech = True
            return
        match = _SILENCE_START.search(text)
        if match is None:
            return
        try:
            started_at = float(match.group(1))
        except ValueError:
            return
        if not self.heard_speech and (
            self._needs_speech_first or started_at < self.min_speech_seconds
        ):
            # Leading silence: the mic is open and you have not started yet.
            return
        self.ever_heard = True
        self.heard_speech = True
        if self.armed:
            self.cut_off = True


def split_stream(buffer: str) -> tuple[list[str], str]:
    """ffmpeg separates progress with CR and messages with LF. Split on both."""
    parts = re.split(r"[\r\n]", buffer)
    return parts[:-1], parts[-1]


def ffmpeg_argv(
    destination: Path | str,
    *,
    binary: str = DEFAULT_BINARY,
    device: str = DEFAULT_DEVICE,
    sample_rate: int = SAMPLE_RATE,
    max_seconds: float = 60.0,
    noise_db: float = -35.0,
    min_silence_seconds: float = 1.2,
    silence_detect: bool = True,
) -> list[str]:
    argv = [
        binary,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "info",
        "-f",
        "avfoundation",
        "-i",
        device,
        "-ac",
        "1",
        "-ar",
        str(int(sample_rate)),
    ]
    if silence_detect:
        argv += ["-af", f"silencedetect=noise={noise_db}dB:d={min_silence_seconds}"]
    argv += ["-t", f"{max_seconds:g}", "-y", str(destination)]
    return argv


async def _default_spawn(argv: Sequence[str]):
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )


@dataclass
class Recorder:
    binary: str = DEFAULT_BINARY
    device: str = DEFAULT_DEVICE
    sample_rate: int = SAMPLE_RATE
    max_seconds: float = 60.0
    open_timeout: float = 8.0
    speech_timeout: float = 8.0
    noise_db: float = -35.0
    min_silence_seconds: float = 1.2
    min_speech_seconds: float = 0.6
    silence_detect: bool = True
    spawn: Spawn = _default_spawn

    @classmethod
    def from_config(cls, config, *, spawn: Spawn | None = None) -> "Recorder":
        speech_timeout = config.get("announce.mic_timeout_seconds", 8)
        return cls(
            binary=str(config.get("microphone.ffmpeg", DEFAULT_BINARY)),
            device=str(config.get("microphone.device", DEFAULT_DEVICE)),
            max_seconds=float(config.get("microphone.max_seconds", 60)),
            open_timeout=float(config.get("microphone.open_timeout_seconds", 8)),
            speech_timeout=float(speech_timeout),
            noise_db=float(config.get("microphone.silence.noise_db", -35)),
            min_silence_seconds=float(config.get("microphone.silence.min_seconds", 1.2)),
            min_speech_seconds=float(config.get("microphone.silence.min_speech_seconds", 0.6)),
            silence_detect=bool(config.get("microphone.silence.enabled", True)),
            spawn=spawn or _default_spawn,
        )

    @property
    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def argv(self, destination: Path | str) -> list[str]:
        return ffmpeg_argv(
            destination,
            binary=self.binary,
            device=self.device,
            sample_rate=self.sample_rate,
            max_seconds=self.max_seconds,
            noise_db=self.noise_db,
            min_silence_seconds=self.min_silence_seconds,
            silence_detect=self.silence_detect,
        )

    async def record(
        self,
        destination: Path | str,
        *,
        stop: asyncio.Event | None = None,
        on_open: Callable[[], Awaitable[None]] | None = None,
        speech_timeout: float | None = None,
        speech: asyncio.Event | None = None,
        arm_after_open: bool = False,
    ) -> Recording:
        """Capture until silence, the toggle, the timeout, or `max_seconds`.

        `speech_timeout` overrides how long this one take waits for you to
        start — and, since `on_open` is awaited before the clock starts, it is
        also how long the mic stays open *after* whatever `on_open` said.

        `speech` is set the first time the recorder hears a voice, which is
        what barge-in waits on: on headphones the first syllable stops the
        sentence being spoken.

        `arm_after_open` is the speakers case. Everything heard while `on_open`
        was talking is our own announcement, so it must not end the take; the
        audio is kept — the echo is filtered out of the transcript, not out of
        the recording — but the cutoff only starts counting once we stop.
        """
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        tracker = SilenceTracker(
            min_speech_seconds=self.min_speech_seconds, armed=not arm_after_open
        )

        try:
            process = await self.spawn(self.argv(path))
        except (OSError, ValueError) as exc:
            raise AudioUnavailable(f"could not start {self.binary}: {exc}") from exc

        opened = asyncio.Event()
        cut = asyncio.Event()

        async def pump() -> None:
            buffer = ""
            stream = process.stderr
            if stream is None:
                return
            while True:
                chunk = await stream.read(1024)
                if not chunk:
                    # ffmpeg's last message may not be newline-terminated.
                    lines, buffer = [buffer], ""
                else:
                    buffer += chunk.decode("utf-8", "replace")
                    lines, buffer = split_stream(buffer)
                for line in lines:
                    tracker.feed(line)
                    if tracker.opened and not opened.is_set():
                        opened.set()
                    if speech is not None and tracker.heard_speech and not speech.is_set():
                        speech.set()
                    if tracker.cut_off:
                        cut.set()
                        return
                if not chunk:
                    break

        pumping = asyncio.create_task(pump())
        try:
            reason = await self._wait_for_end(
                process, opened, cut, stop, on_open, speech_timeout, tracker
            )
        finally:
            pumping.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pumping
            await self._terminate(process)

        seconds = time.monotonic() - started
        spoke = tracker.ever_heard if self.silence_detect else reason != REASON_TIMEOUT
        if reason == REASON_TOGGLE:
            # You pressed the key to stop: assume you meant to say something.
            spoke = True
        return Recording(path=path, seconds=seconds, spoke=spoke, reason=reason)

    async def _wait_for_end(
        self,
        process,
        opened: asyncio.Event,
        cut: asyncio.Event,
        stop: asyncio.Event | None,
        on_open: Callable[[], Awaitable[None]] | None,
        speech_timeout: float | None = None,
        tracker: SilenceTracker | None = None,
    ) -> str:
        try:
            await asyncio.wait_for(opened.wait(), timeout=self.open_timeout)
        except asyncio.TimeoutError:
            raise MicConsentPending(
                f"{self.binary} did not open {self.device} within {self.open_timeout:g}s"
            ) from None
        if on_open is not None:
            await on_open()
        if tracker is not None:
            # Whatever `on_open` said is behind us; from here it is you. Nothing
            # to do when the take was armed from the start.
            tracker.arm()

        waits = {
            asyncio.ensure_future(cut.wait()): REASON_SILENCE,
            asyncio.ensure_future(process.wait()): REASON_EXITED,
        }
        if stop is not None:
            waits[asyncio.ensure_future(stop.wait())] = REASON_TOGGLE

        # No speech at all closes the mic early; speech extends it to the cap.
        deadline = self.speech_timeout if speech_timeout is None else max(0.1, speech_timeout)
        reason = REASON_TIMEOUT
        try:
            while True:
                done, _ = await asyncio.wait(
                    waits.keys(), timeout=deadline, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    break
                reason = waits[next(iter(done))]
                break
        finally:
            for task in waits:
                task.cancel()
            for task in waits:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        return reason

    @staticmethod
    async def _terminate(process) -> None:
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, OSError):
            process.terminate()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5)
