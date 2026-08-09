"""The microphone, without a microphone.

`ffmpeg` is replaced by a fake process that emits the stderr it really emits —
the `silencedetect` lines below are copied from an actual capture — so the
cutoff logic is exercised for real while the suite stays offline and silent.
"""

from __future__ import annotations

import asyncio

import pytest

from voiceloop import audio
from voiceloop.audio import (
    AudioUnavailable,
    MicConsentPending,
    Recorder,
    Recording,
    SilenceTracker,
)

OPEN_LINE = "Output #0, wav, to 'reply.wav':"
PROGRESS = "size=      12KiB time=00:00:00.50 bitrate= 197.2kbits/s speed=0.993x"


def silence_start(at: float) -> str:
    return f"[Parsed_silencedetect_0 @ 0x717205080] silence_start: {at}"


def silence_end(at: float, duration: float = 1.0) -> str:
    return (
        f"[Parsed_silencedetect_0 @ 0x717205080] silence_end: {at} "
        f"| silence_duration: {duration}"
    )


class FakeStderr:
    """Hands out canned output, then blocks forever like a live capture would."""

    def __init__(self, lines, separator: str = "\n"):
        self.pending = "".join(f"{line}{separator}" for line in lines).encode("utf-8")
        self.exhausted = asyncio.Event()

    async def read(self, size: int = 1024) -> bytes:
        if self.pending:
            chunk, self.pending = self.pending[:size], self.pending[size:]
            if not self.pending:
                self.exhausted.set()
            return chunk
        self.exhausted.set()
        await asyncio.sleep(30)
        return b""


class FakeProcess:
    def __init__(self, lines, separator: str = "\n"):
        self.stderr = FakeStderr(lines, separator)
        self.returncode = None
        self.terminated = False
        self._exit = asyncio.Event()

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 255
        self._exit.set()

    async def wait(self) -> int:
        await self._exit.wait()
        return self.returncode or 0


def recorder(lines, *, separator: str = "\n", **kwargs) -> tuple[Recorder, list]:
    spawned: list = []

    async def spawn(argv):
        process = FakeProcess(lines, separator)
        spawned.append((list(argv), process))
        return process

    defaults = dict(open_timeout=2.0, speech_timeout=0.4, spawn=spawn)
    defaults.update(kwargs)
    return Recorder(**defaults), spawned


def run(subject: Recorder, path, **kwargs) -> Recording:
    return asyncio.run(subject.record(path, **kwargs))


# --- the argv --------------------------------------------------------------


def test_the_capture_is_sixteen_kilohertz_mono_wav(tmp_path):
    argv = audio.ffmpeg_argv(tmp_path / "r.wav")

    assert "-f" in argv and argv[argv.index("-f") + 1] == "avfoundation"
    assert argv[argv.index("-ac") + 1] == "1"
    assert argv[argv.index("-ar") + 1] == "16000"
    assert argv[-1] == str(tmp_path / "r.wav")


def test_silence_detection_is_a_filter_on_the_capture(tmp_path):
    argv = audio.ffmpeg_argv(tmp_path / "r.wav", noise_db=-40, min_silence_seconds=2)

    assert argv[argv.index("-af") + 1] == "silencedetect=noise=-40dB:d=2"


def test_silence_detection_can_be_turned_off(tmp_path):
    assert "-af" not in audio.ffmpeg_argv(tmp_path / "r.wav", silence_detect=False)


def test_ffmpeg_never_reads_our_stdin(tmp_path):
    """It shares a process group with the daemon; a stolen stdin is a hang."""
    assert "-nostdin" in audio.ffmpeg_argv(tmp_path / "r.wav")


def test_the_device_and_the_hard_cap_come_from_config(tmp_path, config):
    subject = Recorder.from_config(config)
    argv = subject.argv(tmp_path / "r.wav")

    assert argv[argv.index("-i") + 1] == ":0"
    assert argv[argv.index("-t") + 1] == "60"
    # `announce.mic_timeout_seconds` is how long it waits for you to start.
    assert subject.speech_timeout == 8


# --- the silence tracker ---------------------------------------------------


def test_silence_before_you_start_talking_is_not_a_cutoff():
    tracker = SilenceTracker()

    tracker.feed(silence_start(0.26))

    assert tracker.cut_off is False
    assert tracker.heard_speech is False


def test_silence_after_you_stop_talking_is_a_cutoff():
    tracker = SilenceTracker()

    tracker.feed(silence_start(0.26))
    tracker.feed(silence_end(2.1))
    tracker.feed(silence_start(5.4))

    assert tracker.heard_speech is True
    assert tracker.cut_off is True


def test_speaking_from_the_very_first_sample_still_cuts_off():
    """No leading silence means no `silence_end` — the timestamp is the tell."""
    tracker = SilenceTracker(min_speech_seconds=0.6)

    tracker.feed(silence_start(3.0))

    assert tracker.cut_off is True


def test_the_open_marker_is_the_output_stream():
    tracker = SilenceTracker()
    tracker.feed("Stream mapping:")
    assert tracker.opened is False

    tracker.feed(OPEN_LINE)

    assert tracker.opened is True


def test_garbage_in_the_stderr_is_ignored():
    tracker = SilenceTracker()

    for line in ("", "   ", "silence_start: not-a-number", PROGRESS):
        tracker.feed(line)

    assert (tracker.opened, tracker.heard_speech, tracker.cut_off) == (False, False, False)


def test_progress_and_messages_are_split_on_both_separators():
    """ffmpeg ends progress with CR and messages with LF, in the same stream."""
    lines, rest = audio.split_stream("size=1\rsize=2\nsilence_start: 1.0\rpartial")

    assert lines == ["size=1", "size=2", "silence_start: 1.0"]
    assert rest == "partial"


# --- recording -------------------------------------------------------------


def test_the_chime_waits_until_the_device_is_actually_capturing(tmp_path):
    """avfoundation takes a second to open; chiming early loses your first word."""
    order: list[str] = []

    async def on_open():
        order.append("chime")

    subject, _ = recorder([PROGRESS, OPEN_LINE, silence_end(1.0), silence_start(2.0)])
    run(subject, tmp_path / "r.wav", on_open=on_open)

    assert order == ["chime"]


def test_the_window_only_starts_once_the_chime_is_out_of_the_way(tmp_path):
    """`mic_timeout_seconds` is time to answer in, not time the cue spent ringing."""
    async def on_open():
        await asyncio.sleep(0.3)

    subject, _ = recorder([OPEN_LINE], speech_timeout=0.4)
    recording = run(subject, tmp_path / "r.wav", on_open=on_open)

    assert recording.reason == audio.REASON_TIMEOUT
    assert recording.seconds >= 0.7


def test_silence_ends_the_take(tmp_path):
    subject, _ = recorder([OPEN_LINE, silence_end(1.0), silence_start(2.5)])

    recording = run(subject, tmp_path / "r.wav")

    assert recording.reason == audio.REASON_SILENCE
    assert recording.spoke is True


def test_saying_nothing_is_a_normal_outcome_not_an_error(tmp_path):
    subject, _ = recorder([OPEN_LINE, silence_start(0.1)])

    recording = run(subject, tmp_path / "r.wav")

    assert recording.reason == audio.REASON_TIMEOUT
    assert recording.spoke is False
    assert recording.usable is False


def test_one_take_can_be_given_a_shorter_window_than_the_recorder_has(tmp_path):
    """The heads-up mic: two words for an answer, so four seconds is generous."""
    subject, _ = recorder([OPEN_LINE], speech_timeout=30)

    recording = run(subject, tmp_path / "r.wav", speech_timeout=0.2)

    assert recording.reason == audio.REASON_TIMEOUT
    assert recording.seconds < 5


def test_the_recorder_keeps_its_own_window_when_no_override_is_given(tmp_path):
    subject, _ = recorder([OPEN_LINE], speech_timeout=0.2)

    recording = run(subject, tmp_path / "r.wav", speech_timeout=None)

    assert recording.reason == audio.REASON_TIMEOUT
    assert recording.seconds < 5


def test_the_toggle_closes_the_mic_and_counts_as_speech(tmp_path):
    subject, _ = recorder([OPEN_LINE], speech_timeout=30)
    stop = asyncio.Event()

    async def body():
        asyncio.get_running_loop().call_later(0.05, stop.set)
        return await subject.record(tmp_path / "r.wav", stop=stop)

    recording = asyncio.run(body())

    assert recording.reason == audio.REASON_TOGGLE
    assert recording.spoke is True


def test_the_capture_is_always_terminated(tmp_path):
    """SIGTERM is what makes ffmpeg finalize the WAV header before it exits."""
    subject, spawned = recorder([OPEN_LINE, silence_end(1.0), silence_start(2.0)])

    run(subject, tmp_path / "r.wav")

    assert spawned[0][1].terminated is True


def test_a_device_that_never_opens_is_reported_not_awaited(tmp_path):
    """Issue #7: opening takes a second; not opening at all is the consent prompt."""
    subject, _ = recorder([PROGRESS], open_timeout=0.2)

    with pytest.raises(MicConsentPending, match="did not open"):
        run(subject, tmp_path / "r.wav")


def test_a_device_that_never_opens_is_still_an_audio_failure(tmp_path):
    """Callers that only care that the mic is unusable keep working."""
    subject, _ = recorder([PROGRESS], open_timeout=0.2)

    with pytest.raises(AudioUnavailable):
        run(subject, tmp_path / "r.wav")


def test_a_missing_ffmpeg_is_reported_as_audio_unavailable(tmp_path):
    async def spawn(argv):
        raise FileNotFoundError("ffmpeg")

    subject = Recorder(spawn=spawn)

    with pytest.raises(AudioUnavailable, match="could not start"):
        run(subject, tmp_path / "r.wav")


def test_a_recording_too_short_to_hold_words_is_not_usable(tmp_path):
    path = tmp_path / "r.wav"
    path.write_bytes(b"\0" * 100)

    assert Recording(path=path, seconds=0.2, spoke=True, reason="silence").usable is False


def test_a_recording_with_audio_in_it_is_usable(tmp_path):
    path = tmp_path / "r.wav"
    path.write_bytes(b"\0" * (audio.MIN_USABLE_BYTES + 1))

    assert Recording(path=path, seconds=2.0, spoke=True, reason="silence").usable is True


# --- the mic that is already open while we are talking ----------------------


class GatedStderr:
    """Two batches: what the mic hears while we talk, and what it hears after.

    The first batch is handed over immediately, the way `silencedetect` reports
    our own voice coming back off the speakers. The second waits on an event,
    which the take's `on_open` sets when the sentence is over.
    """

    def __init__(self, lines, later, gate: asyncio.Event):
        self.pending = "".join(f"{line}\n" for line in lines).encode("utf-8")
        self.later = "".join(f"{line}\n" for line in later).encode("utf-8")
        self.gate = gate
        self.released = False

    async def read(self, size: int = 1024) -> bytes:
        if self.pending:
            chunk, self.pending = self.pending[:size], self.pending[size:]
            return chunk
        if not self.released:
            await self.gate.wait()
            self.released = True
            self.pending = self.later
            return await self.read(size)
        await asyncio.sleep(30)
        return b""


def gated_recorder(lines, later, gate, **kwargs) -> Recorder:
    async def spawn(argv):
        process = FakeProcess([])
        process.stderr = GatedStderr(lines, later, gate)
        return process

    defaults = dict(open_timeout=2.0, speech_timeout=0.4, spawn=spawn)
    defaults.update(kwargs)
    return Recorder(**defaults)


def test_our_own_voice_does_not_end_the_take_it_is_running_under(tmp_path):
    """The speakers case: everything heard under the announcement is the announcement.

    `silencedetect` reports our voice, and then the pause the moment `say`
    stops — which is not you finishing a sentence, it is us finishing ours. A
    take that ended on it would give the grace period away to the echo.
    """
    gate = asyncio.Event()
    subject = gated_recorder(
        [OPEN_LINE, silence_end(0.4), silence_start(2.0)],
        [],
        gate,
        speech_timeout=0.3,
    )

    async def on_open():
        await asyncio.sleep(0.05)
        gate.set()

    recording = asyncio.run(
        subject.record(tmp_path / "r.wav", on_open=on_open, arm_after_open=True)
    )

    assert recording.reason == audio.REASON_TIMEOUT
    # Still worth transcribing: the echo filter is what takes our words out,
    # not the recorder — and you may have spoken over the top of us.
    assert recording.spoke is True


def test_a_voice_that_starts_after_the_sentence_still_closes_the_mic(tmp_path):
    gate = asyncio.Event()
    subject = gated_recorder(
        [OPEN_LINE, silence_end(0.4), silence_start(2.0)],
        [silence_end(3.0), silence_start(5.0)],
        gate,
        speech_timeout=5.0,
    )

    async def on_open():
        gate.set()

    recording = asyncio.run(
        subject.record(tmp_path / "r.wav", on_open=on_open, arm_after_open=True)
    )

    assert recording.reason == audio.REASON_SILENCE
    assert recording.spoke is True


def test_the_pause_our_own_voice_leaves_behind_is_not_you_stopping():
    """Armed late, nothing cuts until real speech has been heard since.

    Otherwise the `silence_start` that `say` leaves in its wake — reported at a
    timestamp far past the beginning of the take, so the "you have not started
    yet" rule does not catch it — reads as "they finished talking" the instant
    the arming happens, and the grace period is over before it began.
    """
    tracker = SilenceTracker(min_speech_seconds=0.6, armed=False)
    tracker.feed(silence_end(0.4))
    tracker.arm()

    tracker.feed(silence_start(9.0))

    assert tracker.cut_off is False

    tracker.feed(silence_end(11.0))
    tracker.feed(silence_start(13.0))

    assert tracker.cut_off is True


def test_the_first_syllable_is_reported_while_the_take_is_still_running(tmp_path):
    """What barge-in waits on: not the end of your sentence, the start of it."""
    heard = asyncio.Event()
    subject, _ = recorder([OPEN_LINE, silence_end(1.0), silence_start(2.5)])

    async def scenario():
        recording = await subject.record(tmp_path / "r.wav", speech=heard)
        return recording

    recording = asyncio.run(scenario())

    assert heard.is_set() is True
    assert recording.reason == audio.REASON_SILENCE


def test_a_take_nobody_spoke_into_never_reports_speech(tmp_path):
    heard = asyncio.Event()
    subject, _ = recorder([OPEN_LINE, silence_start(0.1)], speech_timeout=0.2)

    asyncio.run(subject.record(tmp_path / "r.wav", speech=heard))

    assert heard.is_set() is False


def test_arming_forgets_what_was_heard_but_not_that_something_was(tmp_path):
    tracker = SilenceTracker(min_speech_seconds=0.6, armed=False)
    tracker.feed(silence_end(0.4))
    tracker.feed(silence_start(2.0))

    assert tracker.cut_off is False
    assert tracker.ever_heard is True

    tracker.arm()

    assert tracker.heard_speech is False
    assert tracker.ever_heard is True
    assert tracker.cut_off is False


def test_a_tracker_armed_from_the_start_is_what_it_always_was():
    tracker = SilenceTracker(min_speech_seconds=0.6)
    tracker.feed(silence_end(1.0))
    tracker.feed(silence_start(2.0))

    assert tracker.cut_off is True
    tracker.arm()  # idempotent: nothing to forget
    assert tracker.cut_off is True
