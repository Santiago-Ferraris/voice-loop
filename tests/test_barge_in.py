"""Talking while it is talking — and it talking to itself.

The microphone is open under every sentence now, which is only safe because the
two ways that can go wrong are handled separately:

* on **headphones** nothing of ours reaches the mic, so a voice is you and the
  sentence stops mid-word;
* on **speakers** the mic hears us, so nothing is interrupted and our own words
  are subtracted from the transcript instead of being obeyed.
"""

from __future__ import annotations

import asyncio

from voiceloop.store import STATE_PENDING

from conftest import TTY, FakeSpeaker, StubRecorder, audio_output
from test_reply_cycle import QUESTION, queue


class SlowSpeaker(FakeSpeaker):
    """A voice that takes long enough to be interrupted partway through."""

    def __init__(self, seconds: float = 0.05):
        super().__init__()
        self.seconds = seconds

    async def announce(self, announcement):
        await asyncio.sleep(self.seconds)
        await super().announce(announcement)

    async def speak(self, text: str) -> bool:
        await asyncio.sleep(self.seconds)
        return await super().speak(text)


class BargingRecorder(StubRecorder):
    """A mic that hears a voice the moment the sentence starts."""

    async def record(self, destination, **kwargs):
        speech = kwargs.get("speech")

        async def talk_over_it():
            await asyncio.sleep(0)
            if speech is not None:
                speech.set()

        talking = asyncio.ensure_future(talk_over_it())
        try:
            return await super().record(destination, **kwargs)
        finally:
            await talking


# --- headphones ------------------------------------------------------------


def test_on_headphones_your_first_syllable_stops_the_sentence(build):
    daemon = build(
        ["dámelo", "la dos"],
        recorder=BargingRecorder(),
        speaker=SlowSpeaker(),
        output=audio_output(private=True),
    )
    queue(daemon, QUESTION)

    asyncio.run(daemon.announce_next())

    assert daemon.speaker.interruptions >= 1


def test_on_headphones_the_take_is_armed_from_the_first_sample(build):
    """Nothing of ours reaches the mic, so nothing has to be discarded."""
    daemon = build(["dámelo", "la dos"], output=audio_output(private=True))
    queue(daemon, QUESTION)

    asyncio.run(daemon.announce_next())

    assert daemon.recorder.armings == [False, False]
    assert daemon.recorder.barges == [True, True]


# --- speakers --------------------------------------------------------------


def test_on_speakers_the_announcement_does_not_answer_itself(build):
    """The take comes back holding our own sentence. Nothing is delivered.

    Without the filter this is an announcement that hears "Nuevo evento de
    indice", fails to classify it, and types it into the very window it was
    announcing.
    """
    daemon = build(["Nuevo evento de indice"], recorder=StubRecorder())
    item = queue(daemon, QUESTION)

    asyncio.run(daemon.announce_next())

    assert daemon.delivery.sent == []
    assert daemon.speaker.spoken == []  # not even a read-back about it
    assert daemon.store.get(item).state == STATE_PENDING


def test_on_speakers_nothing_is_ever_interrupted(build):
    daemon = build(["Nuevo evento de indice"], recorder=BargingRecorder())
    queue(daemon, QUESTION)

    asyncio.run(daemon.announce_next())

    assert daemon.speaker.interruptions == 0
    assert daemon.recorder.barges == [False]


def test_on_speakers_what_you_said_over_the_echo_still_counts(build):
    """Half the take is us; the half that is you is the answer."""
    daemon = build(["Nuevo evento de indice dámelo", "la dos"])
    queue(daemon, QUESTION)

    asyncio.run(daemon.announce_next())

    assert daemon.delivery.sent == [("choice", TTY, 2)]


def test_on_speakers_every_take_under_a_voice_ignores_it(build):
    daemon = build(["dámelo", "la dos"])
    queue(daemon, QUESTION)

    asyncio.run(daemon.announce_next())

    assert daemon.recorder.armings == [True, True]


# --- and the setting that turns it all off ---------------------------------


def test_barge_in_off_is_the_speakers_path_whatever_is_plugged_in(build):
    from voiceloop.output import OutputProbe

    daemon = build(
        ["dámelo", "la dos"],
        recorder=BargingRecorder(),
        speaker=SlowSpeaker(),
        output=OutputProbe(enabled=False),
    )
    queue(daemon, QUESTION)

    asyncio.run(daemon.announce_next())

    assert daemon.speaker.interruptions == 0
    assert daemon.recorder.armings == [True, True]


# --- the mic that opens with nothing to say --------------------------------


def test_on_speakers_the_cue_chime_is_not_you_starting_to_talk(build):
    """The hotkey mic still makes a noise, and the mic hears it on speakers.

    Now that a take ends when *you* stop talking, a chime counted as speech is
    a take that closes three seconds after it rang — before you have said a
    word, and with the window it was supposed to give you already gone.
    """
    daemon = build(["la dos"])

    asyncio.run(daemon.listen())

    assert daemon.recorder.armings == [True]
    assert daemon.recorder.barges == [False]


def test_the_cue_chime_goes_behind_the_arm_point_on_headphones_too(build):
    """Nothing of ours is being *said* on this path, so there is no sentence to
    barge in on and nothing is lost by waiting out the cue either way."""
    daemon = build(["la dos"], output=audio_output(private=True))

    asyncio.run(daemon.listen())

    assert daemon.recorder.armings == [True]
