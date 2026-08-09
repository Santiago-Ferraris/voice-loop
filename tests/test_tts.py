from __future__ import annotations

import asyncio
import time

import pytest

from voiceloop.announce import Announcement
from voiceloop.tts import CHIME_HEAD_SECONDS, Speaker, resolve_sound

from conftest import TimedRunner, chime_file


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


# --- the floor the microphone opens on --------------------------------------


def test_the_floor_waits_out_the_announcement_it_follows(tmp_path):
    """The mic may not open under a voice: `say` is played into the mic."""
    runner = TimedRunner({"say": 0.1, "afplay": 0.02})
    speaker = Speaker(voice="system", runner=runner, chime_head=0.0)
    taken: list[float] = []

    async def scenario():
        async def open_mic():
            await asyncio.sleep(0.01)  # after the announcement has the lock
            async with speaker.floor():
                taken.append(time.monotonic())

        await asyncio.gather(
            speaker.announce(Announcement(text="hola", chime=chime_file(tmp_path))),
            open_mic(),
        )

    asyncio.run(scenario())

    voice, = runner.spans_of("say")
    assert taken[0] >= voice[2]


def test_the_cue_plays_on_the_floor_instead_of_queueing_behind_it(tmp_path):
    """The chime that says "speak now" cannot be the one thing waiting its turn."""
    runner = TimedRunner({"say": 0.1, "afplay": 0.02})
    speaker = Speaker(voice="system", runner=runner, chime_head=0.0)
    cue = chime_file(tmp_path, "tink")

    async def scenario():
        async with speaker.floor() as floor:
            # Someone else wants to talk, and must not get in first.
            queued = asyncio.ensure_future(speaker.speak("otra ventana"))
            await asyncio.sleep(0)
            assert await floor.cue(cue) is True
            assert floor.held is False
            await queued

    asyncio.run(scenario())

    assert [span[0] for span in runner.spans] == ["afplay", "say"]


def test_the_floor_is_handed_back_before_the_take_not_after(tmp_path):
    """Held across a minute of recording, a busy-mode chime would sit behind it."""
    speaker = Speaker(voice="system", runner=FakeRunner(), chime_head=0.0)
    cue = chime_file(tmp_path)

    async def scenario():
        async with speaker.floor() as floor:
            await floor.cue(cue)
            assert speaker._lock.locked() is False

    asyncio.run(scenario())


def test_an_unresolvable_cue_still_hands_the_floor_back(tmp_path):
    speaker = Speaker(voice="system", runner=FakeRunner(), chime_head=0.0)

    async def scenario():
        async with speaker.floor() as floor:
            assert await floor.cue("NoSuchSound") is False
        assert speaker._lock.locked() is False

    asyncio.run(scenario())


def test_the_floor_is_released_when_the_mic_blows_up(tmp_path):
    """A take that raises must not leave the daemon permanently mute."""
    speaker = Speaker(voice="system", runner=FakeRunner(), chime_head=0.0)

    async def scenario():
        with pytest.raises(RuntimeError):
            async with speaker.floor():
                raise RuntimeError("ffmpeg is gone")
        assert speaker._lock.locked() is False
        assert await speaker.speak("y sigo hablando") is True

    asyncio.run(scenario())


def test_from_config_reads_voice_and_rate(config):
    speaker = Speaker.from_config(config)

    assert speaker.voice == "Paulina"
    assert speaker.rate == 190
    assert speaker.chime_head == CHIME_HEAD_SECONDS


# --- the floor that keeps talking, and the sentence you can cut off ---------


class FakeChild:
    """A `say` that runs until something kills it."""

    def __init__(self):
        self.terminated = False
        self.done = asyncio.Event()

    def terminate(self) -> None:
        self.terminated = True
        self.done.set()

    async def wait(self) -> int:
        await self.done.wait()
        return 0


class KillableRunner:
    """A runner that hands its child over, the way `run_process` does."""

    def __init__(self):
        self.children: list[FakeChild] = []

    async def __call__(self, argv, *, register=None):
        child = FakeChild()
        self.children.append(child)
        if register is not None:
            register(child)
        try:
            return await child.wait()
        finally:
            if register is not None:
                register(None)


def test_a_sentence_can_be_cut_off_mid_word():
    runner = KillableRunner()
    speaker = Speaker(voice="system", runner=runner)

    async def scenario():
        talking = asyncio.ensure_future(speaker.speak("una frase larga"))
        while not runner.children:
            await asyncio.sleep(0)
        cut = await speaker.interrupt()
        await talking
        return cut

    assert asyncio.run(scenario()) is True
    assert runner.children[0].terminated is True
    assert speaker.interruptions == 1


def test_there_is_nothing_to_cut_off_when_nobody_is_talking():
    speaker = Speaker(voice="system", runner=KillableRunner())

    assert asyncio.run(speaker.interrupt()) is False
    assert speaker.interruptions == 0


def test_a_runner_that_cannot_hand_over_its_child_simply_cannot_be_cut_off():
    """Every fake in the suite is one of these. Better than a silent no-op."""
    speaker = Speaker(voice="system", runner=TimedRunner())

    asyncio.run(speaker.speak("hola"))

    assert asyncio.run(speaker.interrupt()) is False


def test_the_floor_speaks_on_the_lock_it_is_already_holding(tmp_path):
    """The mic's whole sequence: open, chime, and talk — without letting go.

    Releasing between the chime and the voice would put the announcement back
    in the queue behind whatever else wanted to talk, and the microphone would
    be recording the wait.
    """
    runner = TimedRunner({"say": 0.05, "afplay": 0.05})
    speaker = Speaker(voice="system", runner=runner, chime_head=0.0)
    sound = chime_file(tmp_path)

    async def scenario():
        async with speaker.floor() as floor:
            await floor.play(sound)
            await floor.say("lo que quiere esa ventana")
            assert floor.held is True
            floor.release()

    asyncio.run(scenario())

    assert [span[0] for span in runner.spans] == ["afplay", "say"]


def test_the_floor_announces_chime_and_voice_together(tmp_path):
    runner = TimedRunner({"say": 0.05, "afplay": 0.2})
    speaker = Speaker(voice="system", runner=runner, chime_head=0.0)

    async def scenario():
        async with speaker.floor() as floor:
            await floor.announce(
                Announcement(text="Nuevo evento de indice.", chime=chime_file(tmp_path))
            )

    asyncio.run(scenario())

    chime, voice = runner.spans_of("afplay")[0], runner.spans_of("say")[0]
    assert voice[1] < chime[2]  # the voice starts while the chime still rings


def test_a_floor_already_let_go_of_falls_back_to_taking_the_lock(tmp_path):
    speaker = Speaker(voice="system", runner=TimedRunner(), chime_head=0.0)

    async def scenario():
        async with speaker.floor() as floor:
            floor.release()
            return await floor.say("igual se dice")

    assert asyncio.run(scenario()) is True
