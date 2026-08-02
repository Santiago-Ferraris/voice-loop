from __future__ import annotations

import asyncio

import pytest

from voiceloop.announce import Announcement
from voiceloop.tts import Speaker, resolve_sound


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


def test_from_config_reads_voice_and_rate(config):
    speaker = Speaker.from_config(config)

    assert speaker.voice == "Paulina"
    assert speaker.rate == 190
