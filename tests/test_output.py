"""Headphones or speakers, read off the system rather than out of a config key.

The payloads are `system_profiler -json SPAudioDataType` verbatim, trimmed to
the devices that matter.
"""

from __future__ import annotations

import json

import pytest

from voiceloop.output import OutputProbe, default_output, is_private

SPEAKERS = {
    "_name": "MacBook Pro Speakers",
    "_properties": "coreaudio_default_audio_system_device",
    "coreaudio_default_audio_output_device": "spaudio_yes",
    "coreaudio_device_transport": "coreaudio_device_type_builtin",
    "coreaudio_output_source": "MacBook Pro Speakers",
}

AIRPODS = {
    "_name": "AirPods Pro",
    "coreaudio_default_audio_output_device": "spaudio_yes",
    "coreaudio_device_transport": "coreaudio_device_type_bluetooth",
    "coreaudio_output_source": "spaudio_default",
}

JACK = {
    "_name": "MacBook Pro Speakers",
    "coreaudio_default_audio_output_device": "spaudio_yes",
    "coreaudio_device_transport": "coreaudio_device_type_builtin",
    "coreaudio_output_source": "Headphones",
}

DOCK = {
    "_name": "DL-Dock",
    "coreaudio_device_transport": "coreaudio_device_type_usb",
    "coreaudio_output_source": "spaudio_default",
}

MICROPHONE = {
    "_name": "MacBook Pro Microphone",
    "coreaudio_default_audio_input_device": "spaudio_yes",
    "coreaudio_device_transport": "coreaudio_device_type_builtin",
}


def payload(*devices) -> bytes:
    return json.dumps(
        {"SPAudioDataType": [{"_items": list(devices), "_name": "coreaudio_device"}]}
    ).encode("utf-8")


# --- reading the payload ---------------------------------------------------


def test_the_default_output_is_picked_out_of_everything_plugged_in():
    name, transport = default_output(payload(DOCK, MICROPHONE, SPEAKERS))

    assert name == "MacBook Pro Speakers"
    assert transport == "coreaudio_device_type_builtin"


def test_a_device_with_no_source_of_its_own_is_named_by_its_name():
    name, _ = default_output(payload(AIRPODS))

    assert name == "AirPods Pro"


def test_a_payload_that_says_nothing_useful_says_nothing():
    assert default_output(b"not json") == ("", "")
    assert default_output(payload(MICROPHONE)) == ("", "")


# --- and deciding what it means --------------------------------------------


def test_bluetooth_is_on_your_head():
    assert is_private(*default_output(payload(AIRPODS))) is True


def test_so_is_the_headphone_jack():
    assert is_private(*default_output(payload(JACK))) is True


@pytest.mark.parametrize("name", ["USB Headset", "Beats Studio", "Sony Earbuds"])
def test_and_so_is_anything_that_says_so_in_its_name(name):
    assert is_private(name, "coreaudio_device_type_usb") is True


def test_the_built_in_speakers_are_not():
    assert is_private(*default_output(payload(SPEAKERS))) is False


def test_not_being_able_to_tell_is_answered_speakers():
    """The bias that matters: mistaking speakers for headphones lets the
    assistant hear its own voice and act on it. The other way round costs an
    echo filter that would have changed nothing."""
    assert is_private("", "") is False


# --- the probe itself ------------------------------------------------------


def test_the_answer_is_cached_rather_than_asked_once_per_sentence():
    calls = []

    def runner():
        calls.append(1)
        return payload(AIRPODS)

    probe = OutputProbe(runner=runner, refresh_seconds=30)

    assert probe.private(now=100.0) is True
    assert probe.private(now=110.0) is True
    assert len(calls) == 1


def test_headphones_that_come_off_are_noticed_when_the_cache_expires():
    answers = [payload(AIRPODS), payload(SPEAKERS)]
    probe = OutputProbe(runner=lambda: answers.pop(0), refresh_seconds=30)

    assert probe.private(now=100.0) is True
    assert probe.private(now=200.0) is False


def test_a_probe_that_cannot_run_answers_speakers_instead_of_raising():
    def runner():
        raise OSError("system_profiler: not found")

    assert OutputProbe(runner=runner).private() is False


def test_barge_in_can_be_turned_off_without_probing_anything():
    def runner():  # pragma: no cover - must never be reached
        raise AssertionError("probed with barge-in disabled")

    probe = OutputProbe(runner=runner, enabled=False)

    assert probe.private() is False
    assert probe.probes == 0


def test_the_probe_reads_its_settings_from_config(config):
    probe = OutputProbe.from_config(config)

    assert probe.enabled is True
    assert probe.refresh_seconds == 30
