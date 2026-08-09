from __future__ import annotations

import asyncio
import time

import pytest

from voiceloop.announce import Announcement
from voiceloop.tts import CHIME_HEAD_SECONDS, Speaker, resolve_sound


class FakeRunner:
    """Records argv instead of spawning `say`/`afplay` (CI has neither)."""

    def __init__(self, returncode: int = 0, delay: float = 0.0):
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.delay = delay
        self.concurrent = 0
        self.max_concurrent = 0

    async def __call__(self, argv):
        self.calls.append(list(argv))
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        if self.delay:
            await asyncio.sleep(self.delay)
        self.concurrent -= 1
        return self.returncode


class TimedRunner:
    """Records when each process started and finished, so overlap is measurable."""

    def __init__(self, durations: dict[str, float] | None = None):
        self.durations = durations or {}
        self.spans: list[list] = []

    async def __call__(self, argv):
        index = len(self.spans)
        self.spans.append([argv[0], time.monotonic(), None])
        await asyncio.sleep(self.durations.get(argv[0], 0.0))
        self.spans[index][2] = time.monotonic()
        return 0

    def spans_of(self, binary: str) -> list[list]:
        """Every run of that binary, in the order they started."""
        return [span for span in self.spans if span[0] == binary]


def chime_file(tmp_path, name: str = "ping") -> str:
    sound = tmp_path / f"{name}.aiff"
    sound.write_bytes(b"fake audio")
    return str(sound)


def test_say_argv_carries_voice_and_rate():
    speaker = Speaker(voice="Paulina", rate=190)

    assert speaker.say_argv("hola") == ["say", "-v", "Paulina", "-r", "190", "--", "hola"]


def test_the_system_voice_sentinel_drops_the_voice_flag():
    speaker = Speaker(voice="system", rate=None)

    assert speaker.say_argv("hola") == ["say", "--", "hola"]


def test_text_is_an_argument_never_a_shell_string():
    """A summary is model output; it must not be able to reach a shell."""
    speaker = Speaker(voice="system")
    nasty = '"; rm -rf / #'

    argv = speaker.say_argv(nasty)

    assert argv[-1] == nasty
    assert argv[-2] == "--"


def test_a_leading_dash_is_not_read_as_a_flag():
    assert Speaker(voice="system").say_argv("-v Alex") == ["say", "--", "-v Alex"]


def test_speaking_runs_say():
    runner = FakeRunner()
    speaker = Speaker(voice="system", runner=runner)

    assert asyncio.run(speaker.speak("hola")) is True
    assert runner.calls == [["say", "--", "hola"]]


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_empty_text_is_not_spoken(text):
    runner = FakeRunner()

    assert asyncio.run(Speaker(runner=runner).speak(text)) is False
    assert runner.calls == []


def test_a_failing_say_reports_failure_without_raising():
    runner = FakeRunner(returncode=1)

    assert asyncio.run(Speaker(voice="system", runner=runner).speak("hola")) is False


def test_a_runner_that_explodes_does_not_propagate():
    async def boom(argv):
        raise RuntimeError("no audio device")

    assert asyncio.run(Speaker(runner=boom).speak("hola")) is False


def test_an_unknown_chime_is_skipped():
    runner = FakeRunner()

    assert asyncio.run(Speaker(runner=runner).chime("NotARealSound")) is False
    assert asyncio.run(Speaker(runner=runner).chime(None)) is False
    assert runner.calls == []


def test_an_absolute_chime_path_is_played(tmp_path):
    sound = tmp_path / "ping.aiff"
    sound.write_bytes(b"fake audio")
    runner = FakeRunner()

    assert asyncio.run(Speaker(runner=runner).chime(str(sound))) is True
    assert runner.calls == [["afplay", str(sound)]]


def test_resolve_sound_rejects_a_missing_absolute_path(tmp_path):
    assert resolve_sound(str(tmp_path / "nope.aiff")) is None
    assert resolve_sound("") is None
    assert resolve_sound(None) is None


def test_announcing_chimes_before_speaking(tmp_path):
    sound = tmp_path / "ping.aiff"
    sound.write_bytes(b"fake audio")
    runner = FakeRunner()
    speaker = Speaker(voice="system", runner=runner)

    asyncio.run(speaker.announce(Announcement(text="hola", chime=str(sound))))

    assert runner.calls == [["afplay", str(sound)], ["say", "--", "hola"]]


def test_a_silent_announcement_only_chimes(tmp_path):
    sound = tmp_path / "glass.aiff"
    sound.write_bytes(b"fake audio")
    runner = FakeRunner()
    speaker = Speaker(voice="system", runner=runner)

    asyncio.run(speaker.announce(Announcement(text="PR created", chime=str(sound), speak=False)))

    assert runner.calls == [["afplay", str(sound)]]


def test_two_sessions_never_talk_over_each_other():
    """The point of the whole project: one voice at a time."""
    runner = FakeRunner(delay=0.02)
    speaker = Speaker(voice="system", runner=runner)

    async def both():
        await asyncio.gather(
            speaker.speak("primera sesión"),
            speaker.speak("segunda sesión"),
            speaker.speak("tercera sesión"),
        )

    asyncio.run(both())

    assert runner.max_concurrent == 1
    assert len(runner.calls) == 3


# --- the chime and the voice overlap ---------------------------------------


def test_the_voice_starts_before_the_chime_has_finished(tmp_path):
    """`Ping` rings for 1.5 s and says nothing after the first tenth of it.

    Waiting for `afplay` to exit put ~2 s of silence between the cue and the
    sentence, on every announcement.
    """
    runner = TimedRunner({"afplay": 0.20})
    speaker = Speaker(voice="system", runner=runner, chime_head=0.02)

    asyncio.run(speaker.announce(Announcement(text="hola", chime=chime_file(tmp_path))))

    _, chime_start, chime_end = runner.spans_of("afplay")[0]
    _, say_start, _ = runner.spans_of("say")[0]
    assert say_start < chime_end  # the tail rings under the first syllables
    assert say_start - chime_start >= 0.02  # but the chime is heard first


def test_two_announcements_still_never_overlap(tmp_path):
    """The overlap is inside one announcement. Two windows still take turns."""
    sound = chime_file(tmp_path)
    runner = TimedRunner({"afplay": 0.10, "say": 0.05})
    speaker = Speaker(voice="system", runner=runner, chime_head=0.02)

    async def both():
        await asyncio.gather(
            speaker.announce(Announcement(text="primera", chime=sound)),
            speaker.announce(Announcement(text="segunda", chime=sound)),
        )

    asyncio.run(both())

    first_voice, second_voice = runner.spans_of("say")
    first_chime, second_chime = runner.spans_of("afplay")
    assert first_voice[2] <= second_voice[1]  # one voice at a time
    assert first_chime[2] <= second_voice[1]  # and no tail left ringing into it
    assert first_voice[2] <= second_chime[1]


def test_a_milestone_still_waits_out_its_chime(tmp_path):
    """Chime-only, unchanged: there is no voice to overlap it with."""
    runner = TimedRunner({"afplay": 0.05})
    speaker = Speaker(voice="system", runner=runner, chime_head=0.0)

    asyncio.run(
        speaker.announce(Announcement(text="PR created", chime=chime_file(tmp_path), speak=False))
    )

    assert [span[0] for span in runner.spans] == ["afplay"]
    assert runner.spans[0][2] is not None


def test_an_announcement_with_no_chime_waits_for_nothing(tmp_path):
    runner = TimedRunner()
    # A head this long would be unmistakable in the runtime if it were used.
    speaker = Speaker(voice="system", runner=runner, chime_head=5.0)

    asyncio.run(speaker.announce(Announcement(text="hola", chime=None)))

    assert [span[0] for span in runner.spans] == ["say"]


def test_from_config_reads_voice_and_rate(config):
    speaker = Speaker.from_config(config)

    assert speaker.voice == "Paulina"
    assert speaker.rate == 190
    assert speaker.chime_head == CHIME_HEAD_SECONDS
