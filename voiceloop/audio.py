"""The microphone — `ffmpeg -f avfoundation`, 16 kHz mono WAV to a temp file.

No SDK and no CoreAudio binding: `ffmpeg` is already on the box, it opens the
default input device by index, and it can do the voice activity detection for
us with the `silencedetect` filter, so rev 1 needs nothing else. Rev 2 replaces
that filter with Deepgram's server-side endpointing over a streaming socket —
the cutoff moves, the rest of this module does not.

Things the recorder has to get right:

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
* **But running out of time is not the same as saying nothing.** In a room
  whose floor never drops below `noise_db`, `silencedetect` never reports a
  silence, so it never reports speech either, and every take ends on the
  timeout looking empty while holding a full sentence. What a take holds is
  decided by measuring the file (`measure`), never by how the take ended.
* **The window is not a countdown.** `speech_timeout` is how long the mic waits
  for you to *start*; once you have started, what ends the take is you
  stopping. A fixed ceiling means an eight-second answer leaves you two seconds
  to finish it, and `max_seconds` goes back to being what it says it is — the
  safety net, not the way takes normally end.
* **A threshold in dB is a guess about a room.** So the cutoff does not use
  one: ffmpeg hands the same capture over a pipe as raw PCM, `VoiceGate`
  measures the room in the first half second of the take and treats anything
  `margin_db` above *that* as a voice. It is the only thing that survives a
  different room, a different input gain, or new headphones.

Termination is SIGTERM, which makes ffmpeg finalize the WAV header before
exiting — the file is playable, and its non-zero exit status is expected.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import logging
import math
import re
import shutil
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

log = logging.getLogger("voiceloop.audio")

DEFAULT_BINARY = "ffmpeg"
DEFAULT_DEVICE = ":0"
SAMPLE_RATE = 16_000

# WAV header plus ~0.3 s of 16-bit mono at 16 kHz. Anything shorter is noise.
MIN_USABLE_BYTES = 44 + int(SAMPLE_RATE * 2 * 0.3)

FULL_SCALE = 32768.0
# Nothing is reported quieter than this; digital silence has no dB value.
SILENCE_FLOOR_DB = -120.0
# Short enough that a single word lands inside a handful of windows.
LEVEL_WINDOW_SECONDS = 0.03
# How much audio above the threshold makes a take worth transcribing. Matched
# to MIN_USABLE_BYTES: a third of a second is the shortest thing worth a word.
MIN_SIGNAL_SECONDS = 0.3

# --- the relative cutoff ----------------------------------------------------
# How long you have to stop talking for the take to be over. Long enough to
# think mid-sentence, short enough that the mic is not still there when you
# have moved on.
DEFAULT_MIN_SILENCE_SECONDS = 3.0
# The room, read off the start of the take: long enough to average out a
# keystroke, short enough to be over before anybody answers a question.
CALIBRATION_SECONDS = 0.5
# How far above the room a window has to sit to be a voice. Measured on the
# desk this was written at: room -49 dB, voice -29 dB. Ten leaves the voice
# clear by ten and still ignores the chair.
FLOOR_MARGIN_DB = 10.0
# Outside these a measured floor is not a room. Below: digital silence and the
# dither around it, where floor + margin would make the dither into speech.
# Above: a room nothing could clear. Either way the configured absolute
# threshold is the better guess.
FLOOR_MIN_DB = -80.0
FLOOR_MAX_DB = -20.0
# The floor is the quietest stretch, and within a stretch the quiet part of it:
# a percentile, so one thump does not raise the room by 15 dB.
QUIET_PERCENTILE = 0.75
# A single window over the threshold is a chair creaking, not a word.
MIN_ACTIVE_SECONDS = 0.12
# How long after ffmpeg should have stopped itself we stop waiting for it.
MAX_DURATION_SLACK = 2.0
# An ffmpeg that exits this far into its own `-t` reached the cap; anything
# earlier is a capture that died and says so as `exited`.
CAP_REACHED_FRACTION = 0.9

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
class Level:
    """What a take holds, measured off the file.

    `loud_seconds` is the one that decides anything: how much of the take sat
    above the same threshold `silencedetect` was given. Peak and mean are for
    the log — they are what tells you afterwards whether the room was loud, the
    input gain was low, or nobody said anything.
    """

    seconds: float
    peak_db: float
    mean_db: float
    loud_seconds: float
    noise_db: float

    @property
    def has_signal(self) -> bool:
        return self.loud_seconds >= MIN_SIGNAL_SECONDS

    def __str__(self) -> str:
        return (
            f"peak {self.peak_db:.1f} dB, mean {self.mean_db:.1f} dB, "
            f"{self.loud_seconds:.1f}s above {self.noise_db:g} dB"
        )


def dbfs(amplitude: float) -> float:
    """A 16-bit sample amplitude as dB below full scale, the way ffmpeg reports it."""
    if amplitude <= 0:
        return SILENCE_FLOOR_DB
    return max(SILENCE_FLOOR_DB, 20 * math.log10(min(amplitude / FULL_SCALE, 1.0)))


def measure(path: Path | str, *, noise_db: float = -35.0) -> Level | None:
    """Read the captured WAV and report what is actually in it.

    `silencedetect` only ever reports what it *stopped* hearing, so in a room
    whose floor never dips under the threshold it reports nothing at all — no
    `silence_end`, no speech, and a take full of voice that looks exactly like
    an empty one. The file does not have that blind spot.

    `None` means the audio could not be measured: missing, truncated, or not
    the 16-bit PCM the recorder writes. Unknown is not silent, so callers fall
    back to what ffmpeg told them instead of throwing the take away.
    """
    try:
        with wave.open(str(path), "rb") as wav:
            if wav.getsampwidth() != 2:
                return None
            rate = wav.getframerate() or SAMPLE_RATE
            channels = max(1, wav.getnchannels())
            raw = wav.readframes(wav.getnframes())
    except (OSError, EOFError, ValueError, wave.Error):
        return None

    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - len(raw) % samples.itemsize])
    if sys.byteorder == "big":
        # WAV is little-endian; `array` is native.
        samples.byteswap()

    per_second = max(1, int(rate) * channels)
    window = max(1, int(per_second * LEVEL_WINDOW_SECONDS))
    peak = 0
    squares = 0
    loud = 0
    for start in range(0, len(samples), window):
        chunk = samples[start : start + window]
        chunk_squares = sum(value * value for value in chunk)
        squares += chunk_squares
        peak = max(peak, max(chunk), -min(chunk))
        if dbfs(math.sqrt(chunk_squares / len(chunk))) >= noise_db:
            loud += len(chunk)
    mean = math.sqrt(squares / len(samples)) if samples else 0.0
    return Level(
        seconds=len(samples) / per_second,
        peak_db=dbfs(peak),
        mean_db=dbfs(mean),
        loud_seconds=loud / per_second,
        noise_db=noise_db,
    )


@dataclass(frozen=True)
class Recording:
    path: Path
    seconds: float
    spoke: bool
    reason: str
    level: Level | None = None

    @property
    def usable(self) -> bool:
        try:
            if self.path.stat().st_size < MIN_USABLE_BYTES:
                return False
        except OSError:
            return False
        if self.spoke:
            return True
        # `spoke` is what `silencedetect` inferred, and how the take ended is
        # not evidence about what is in it: a take that ran out of time is a
        # listening window that closed, not a person who said nothing. Only the
        # audio can say that, so ask the audio before throwing it away.
        return self.level is not None and self.level.has_signal

    @property
    def summary(self) -> str:
        """One line for the log: how the take ended, and what was in it."""
        detail = "" if self.level is None else f", {self.level}"
        return f"closed by {self.reason}, {self.seconds:.1f}s{detail}"


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


class VoiceGate:
    """Voice activity measured against the room, not against a number in dB.

    `silencedetect` compares the signal to an absolute threshold, and that
    threshold is a guess about a room. Set it for a quiet one and a noisy one
    never falls under it: no `silence_start` ever, therefore no cutoff, and
    every take runs to the ceiling. Set it for the noisy one and a whisper is
    silence. What actually survives a different room, a different input gain or
    new headphones is the *distance* between the room and a voice — twenty-odd
    dB, wherever the pair of them happen to sit — so that is what is measured.

    The room is read off the take itself: the quietest `calibration_seconds`
    block seen before the first word, at `QUIET_PERCENTILE` within the block so
    one thump does not raise it. Anything `margin_db` above that, for longer
    than a creak, is you. When the measured floor is not a room at all — digital
    silence, or a floor nothing could clear — the configured absolute threshold
    is used instead, because a wrong measurement is worse than a guess.

    Everything is timed off the samples, never the clock: the cutoff lands
    `min_silence_seconds` after your last word *in the audio*, which is the same
    number whether ffmpeg delivered it early or late.
    """

    def __init__(
        self,
        *,
        sample_rate: int = SAMPLE_RATE,
        min_silence_seconds: float = DEFAULT_MIN_SILENCE_SECONDS,
        fallback_db: float = -35.0,
        calibration_seconds: float = CALIBRATION_SECONDS,
        margin_db: float = FLOOR_MARGIN_DB,
        armed: bool = True,
    ):
        self.sample_rate = max(1, int(sample_rate))
        self.min_silence_seconds = max(0.0, min_silence_seconds)
        self.fallback_db = fallback_db
        self.margin_db = margin_db
        self.window = max(1, int(self.sample_rate * LEVEL_WINDOW_SECONDS))
        self.window_seconds = self.window / self.sample_rate
        self.block = max(1, round(max(0.0, calibration_seconds) / self.window_seconds))
        self.active_windows = max(1, round(MIN_ACTIVE_SECONDS / self.window_seconds))
        self.armed = armed
        self.floor_db: float | None = None
        self.threshold_db = fallback_db
        self.spoke = False
        self.cut_off = False
        self.quiet_seconds = 0.0
        self._levels: list[float] = []
        self._tail = b""
        self._calibrated = 0

    @property
    def seconds(self) -> float:
        """How much audio has been through the gate, in the take's own time."""
        return len(self._levels) * self.window_seconds

    def arm(self) -> None:
        """Everything so far was our own voice. The room starts here."""
        if self.armed:
            return
        self.armed = True
        self._levels.clear()
        self._tail = b""
        self._calibrated = 0
        self.floor_db = None
        self.threshold_db = self.fallback_db
        self.spoke = False
        self.cut_off = False
        self.quiet_seconds = 0.0

    def feed(self, chunk: bytes) -> None:
        """Take in raw 16-bit mono PCM, straight off ffmpeg's second output."""
        if not self.armed or not chunk:
            return
        data = self._tail + chunk
        frame = self.window * 2
        whole = len(data) - len(data) % frame
        self._tail = data[whole:]
        if not whole:
            return
        for start in range(0, whole, frame):
            samples = array.array("h")
            samples.frombytes(data[start : start + frame])
            if sys.byteorder == "big":
                # The pipe is little-endian; `array` is native.
                samples.byteswap()
            squares = sum(value * value for value in samples)
            self._levels.append(dbfs(math.sqrt(squares / len(samples))))
        self._calibrate()
        self._evaluate()

    def _calibrate(self) -> None:
        """The floor is the quietest block of the take so far.

        Only until you speak: after that the room is whatever it was when you
        started, and re-reading it off your own voice would raise the bar past
        your next word. Blocks keep going in until then, so a take that opened
        under a cough still finds the room once the cough is over — and finds
        it retroactively, since the whole take is re-read at the new threshold.
        """
        if self.spoke:
            return
        while len(self._levels) - self._calibrated >= self.block:
            block = sorted(self._levels[self._calibrated : self._calibrated + self.block])
            self._calibrated += self.block
            quiet = block[min(len(block) - 1, int(len(block) * QUIET_PERCENTILE))]
            if self.floor_db is None or quiet < self.floor_db:
                self.floor_db = quiet
        if self.floor_db is None:
            return
        if FLOOR_MIN_DB <= self.floor_db <= FLOOR_MAX_DB:
            self.threshold_db = self.floor_db + self.margin_db
        else:
            self.threshold_db = self.fallback_db

    def _evaluate(self) -> None:
        """Re-read the whole take at the current threshold.

        Nothing is a voice until there is a room to compare it to — the first
        block of the take is the room, and jumping the gun with the fallback
        threshold would lock the floor in before it was ever measured.

        Cheap (a 30 ms window per entry, a minute is two thousand floats) and it
        is what makes the floor moving underneath harmless: the answer never
        depends on what the threshold happened to be when a window arrived. The
        floor only ever goes down, so a window that counts once counts forever.
        """
        if self.floor_db is None:
            return
        active = 0
        last_active: int | None = None
        for index, level in enumerate(self._levels):
            if level >= self.threshold_db:
                active += 1
                if active >= self.active_windows:
                    last_active = index + 1
            else:
                active = 0
        if last_active is None:
            self.quiet_seconds = 0.0
            return
        self.spoke = True
        self.quiet_seconds = (len(self._levels) - last_active) * self.window_seconds
        if self.quiet_seconds >= self.min_silence_seconds:
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
    min_silence_seconds: float = DEFAULT_MIN_SILENCE_SECONDS,
    silence_detect: bool = True,
) -> list[str]:
    """The capture, and — when the cutoff is on — the same audio over a pipe.

    Two outputs off one device: the WAV that goes to the recognizer, and raw
    PCM on stdout for `VoiceGate`, which is how the cutoff gets to compare you
    against your room instead of against a constant. `-t` goes on both, or the
    pipe outlives the file it was opened alongside.
    """
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
    if silence_detect:
        argv += [
            "-ac",
            "1",
            "-ar",
            str(int(sample_rate)),
            "-t",
            f"{max_seconds:g}",
            "-f",
            "s16le",
            "pipe:1",
        ]
    return argv


async def _default_spawn(argv: Sequence[str]):
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        # The monitoring output. Nothing is written here unless `ffmpeg_argv`
        # added `pipe:1`, but a pipe nobody drains is a capture that stalls, so
        # `record` reads it for as long as the process lives either way.
        stdout=asyncio.subprocess.PIPE,
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
    min_silence_seconds: float = DEFAULT_MIN_SILENCE_SECONDS
    min_speech_seconds: float = 0.6
    calibration_seconds: float = CALIBRATION_SECONDS
    margin_db: float = FLOOR_MARGIN_DB
    silence_detect: bool = True
    spawn: Spawn = _default_spawn

    @classmethod
    def from_config(cls, config, *, spawn: Spawn | None = None) -> "Recorder":
        speech_timeout = config.get("announce.mic_timeout_seconds", 8)
        silence = "microphone.silence"
        return cls(
            binary=str(config.get("microphone.ffmpeg", DEFAULT_BINARY)),
            device=str(config.get("microphone.device", DEFAULT_DEVICE)),
            max_seconds=float(config.get("microphone.max_seconds", 60)),
            open_timeout=float(config.get("microphone.open_timeout_seconds", 8)),
            speech_timeout=float(speech_timeout),
            noise_db=float(config.get(f"{silence}.noise_db", -35)),
            min_silence_seconds=float(
                config.get(f"{silence}.min_seconds", DEFAULT_MIN_SILENCE_SECONDS)
            ),
            min_speech_seconds=float(config.get(f"{silence}.min_speech_seconds", 0.6)),
            calibration_seconds=float(
                config.get(f"{silence}.calibration_seconds", CALIBRATION_SECONDS)
            ),
            margin_db=float(config.get(f"{silence}.margin_db", FLOOR_MARGIN_DB)),
            silence_detect=bool(config.get(f"{silence}.enabled", True)),
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
        """Capture until you stop talking, the toggle, the timeout, or `max_seconds`.

        `speech_timeout` overrides how long this one take waits for you to
        *start* — and, since `on_open` is awaited before the clock starts, it is
        also how long the mic stays open *after* whatever `on_open` said. Once
        you have started it stops applying: what ends the take from there is the
        cutoff, `min_silence_seconds` after your last word, with `max_seconds`
        behind it as the net.

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
        gate = VoiceGate(
            sample_rate=self.sample_rate,
            min_silence_seconds=self.min_silence_seconds,
            fallback_db=self.noise_db,
            calibration_seconds=self.calibration_seconds,
            margin_db=self.margin_db,
            armed=not arm_after_open,
        )

        try:
            process = await self.spawn(self.argv(path))
        except (OSError, ValueError) as exc:
            raise AudioUnavailable(f"could not start {self.binary}: {exc}") from exc

        opened = asyncio.Event()
        cut = asyncio.Event()
        heard = asyncio.Event()

        def started_talking() -> None:
            """Only once armed: before that the voice on the mic is ours."""
            heard.set()
            if speech is not None and not speech.is_set():
                speech.set()

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
                    if tracker.armed and tracker.heard_speech:
                        started_talking()
                    if tracker.cut_off:
                        cut.set()
                        return
                if not chunk:
                    break

        async def listen() -> None:
            """The same capture, as samples: the cutoff that knows the room."""
            stream = getattr(process, "stdout", None)
            if stream is None:
                return
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return
                gate.feed(chunk)
                if gate.spoke:
                    started_talking()
                if gate.cut_off:
                    cut.set()
                    return

        pumping = [asyncio.create_task(pump()), asyncio.create_task(listen())]
        try:
            reason = await self._wait_for_end(
                process,
                opened,
                cut,
                stop,
                on_open,
                speech_timeout,
                tracker,
                gate=gate,
                heard=heard,
                started=started,
            )
        finally:
            for task in pumping:
                task.cancel()
            for task in pumping:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await self._terminate(process)

        seconds = time.monotonic() - started
        if reason == REASON_EXITED and seconds >= self.max_seconds * CAP_REACHED_FRACTION:
            # ffmpeg stopping on its own that late is its own `-t`, not a
            # failure: the take is what the cap allowed, and it holds a voice.
            reason = REASON_MAX
        if self.silence_detect:
            spoke = tracker.ever_heard or gate.spoke
        else:
            spoke = reason != REASON_TIMEOUT
        if reason == REASON_TOGGLE:
            # You pressed the key to stop: assume you meant to say something.
            spoke = True
        if gate.floor_db is not None:
            log.debug(
                "room floor %.1f dB, voice over %.1f dB, %.1fs of audio",
                gate.floor_db,
                gate.threshold_db,
                gate.seconds,
            )
        # Always, whatever ffmpeg thought it heard: the measurement is what
        # rescues a take the filter never noticed, and what the log needs to
        # make a discarded one explainable instead of just "nothing".
        level = await asyncio.to_thread(measure, path, noise_db=self.noise_db)
        return Recording(
            path=path, seconds=seconds, spoke=spoke, reason=reason, level=level
        )

    async def _wait_for_end(
        self,
        process,
        opened: asyncio.Event,
        cut: asyncio.Event,
        stop: asyncio.Event | None,
        on_open: Callable[[], Awaitable[None]] | None,
        speech_timeout: float | None = None,
        tracker: SilenceTracker | None = None,
        *,
        gate: "VoiceGate | None" = None,
        heard: asyncio.Event | None = None,
        started: float | None = None,
    ) -> str:
        try:
            await asyncio.wait_for(opened.wait(), timeout=self.open_timeout)
        except asyncio.TimeoutError:
            raise MicConsentPending(
                f"{self.binary} did not open {self.device} within {self.open_timeout:g}s"
            ) from None
        if on_open is not None:
            await on_open()
        # Whatever `on_open` said is behind us; from here it is you. Nothing to
        # do when the take was armed from the start.
        if tracker is not None:
            tracker.arm()
        if gate is not None:
            gate.arm()

        waits = {
            asyncio.ensure_future(cut.wait()): REASON_SILENCE,
            asyncio.ensure_future(process.wait()): REASON_EXITED,
        }
        if stop is not None:
            waits[asyncio.ensure_future(stop.wait())] = REASON_TOGGLE
        if heard is not None:
            # Not an ending. The one thing here that lifts the deadline.
            waits[asyncio.ensure_future(heard.wait())] = None

        # Time to *start* talking. Once you have, the deadline stops being a
        # window and becomes the backstop for a capture that outlived its own
        # `-t` — normally ffmpeg gets there first and exits by itself.
        deadline = self.speech_timeout if speech_timeout is None else max(0.1, speech_timeout)
        reason = REASON_TIMEOUT
        try:
            while True:
                done, _ = await asyncio.wait(
                    waits.keys(), timeout=deadline, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    break
                ending = waits[next(iter(done))]
                if ending is None:
                    for task in [t for t, r in waits.items() if r is None]:
                        del waits[task]
                    elapsed = 0.0 if started is None else time.monotonic() - started
                    deadline = max(0.1, self.max_seconds - elapsed + MAX_DURATION_SLACK)
                    reason = REASON_MAX
                    continue
                reason = ending
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
