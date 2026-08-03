"""Speech-to-text adapters against canned responses.

The Deepgram tests are mostly about four query parameters. Three of them are
not defaults and each one was paid for with a benchmark: without `keyterm`,
"mergealo" comes back "MGalo"; with `smart_format` or `numerals` on, "fijate
primero" arrives as "fijate 1º" and *that* is what Claude receives. There is a
test below whose entire job is to fail if either flag is ever dropped.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse

import pytest

from voiceloop import stt
from voiceloop.stt import SttError, SttNotConfigured, SttNotImplemented
from voiceloop.stt.deepgram import DeepgramStt, normalize_keyterms
from voiceloop.stt.mock import MockStt
from voiceloop.stt.openai import (
    OpenAiStt,
    build_prompt,
    confidence_from_logprobs,
    encode_multipart,
)

DEEPGRAM_BODY = {
    "results": {
        "channels": [
            {
                "alternatives": [
                    {
                        "transcript": "fijate primero si el rate-limiter anda y después mergealo",
                        "confidence": 0.99853516,
                    }
                ]
            }
        ]
    }
}


class FakeTransport:
    def __init__(self, payload=None, error: Exception | None = None):
        self.payload = payload if payload is not None else DEEPGRAM_BODY
        self.error = error
        self.calls: list[tuple] = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, body, timeout))
        if self.error is not None:
            raise self.error
        return json.dumps(self.payload).encode("utf-8")

    @property
    def query(self) -> dict:
        parsed = urllib.parse.urlparse(self.calls[-1][0])
        return urllib.parse.parse_qs(parsed.query)


@pytest.fixture
def wav(tmp_path):
    path = tmp_path / "reply.wav"
    path.write_bytes(b"RIFF....WAVEfmt ")
    return path


def deepgram(transport, **kwargs) -> DeepgramStt:
    return DeepgramStt(api_key="dg-test", transport=transport, **kwargs)


# --- Deepgram: the parameters that are not optional ------------------------


def test_smart_format_and_numerals_are_always_off(wav):
    """Turn either on and "fijate primero" reaches Claude as "fijate 1º"."""
    transport = FakeTransport()

    deepgram(transport).transcribe(wav)

    assert transport.query["smart_format"] == ["false"]
    assert transport.query["numerals"] == ["false"]


def test_the_model_and_the_multilingual_flag_are_sent(wav):
    transport = FakeTransport()

    deepgram(transport).transcribe(wav)

    assert transport.query["model"] == ["nova-3"]
    assert transport.query["language"] == ["multi"]


def test_every_keyterm_is_its_own_parameter(wav):
    """Without these, a conjugated loanword comes back invented."""
    transport = FakeTransport()

    deepgram(transport, base_keyterms=("rate-limiter",)).transcribe(wav, ["mergealo"])

    assert transport.query["keyterm"] == ["rate-limiter", "mergealo"]


def test_live_session_names_are_appended_to_the_configured_vocabulary(wav):
    transport = FakeTransport()

    deepgram(transport, base_keyterms=("worker-queue",)).transcribe(
        wav, ["draft-mode-changes", "sidepanel-test"]
    )

    assert transport.query["keyterm"] == [
        "worker-queue",
        "draft-mode-changes",
        "sidepanel-test",
    ]


def test_keyterms_are_deduplicated_case_insensitively_in_order():
    assert normalize_keyterms(["Prod", " prod ", "", None, "queue"]) == ["Prod", "queue"]


def test_the_keyterm_list_is_capped_so_the_url_stays_legal():
    assert len(normalize_keyterms([f"term-{n}" for n in range(500)])) == 100


def test_the_key_travels_in_the_authorization_header_never_the_url(wav):
    transport = FakeTransport()

    deepgram(transport).transcribe(wav)

    url, headers, body, _ = transport.calls[-1]
    assert headers["Authorization"] == "Token dg-test"
    assert "dg-test" not in url
    assert body == wav.read_bytes()


# --- Deepgram: reading the answer ------------------------------------------


def test_the_transcript_and_its_confidence_come_back(wav):
    result = deepgram(FakeTransport()).transcribe(wav)

    assert result.text == "fijate primero si el rate-limiter anda y después mergealo"
    assert result.confidence == pytest.approx(0.9985, abs=1e-3)
    assert result.provider == "deepgram"


def test_a_take_with_no_words_in_it_is_an_empty_transcript(wav):
    payload = {"results": {"channels": [{"alternatives": [{"transcript": "", "confidence": 0}]}]}}

    result = deepgram(FakeTransport(payload)).transcribe(wav)

    assert result.empty is True


def test_a_response_shaped_wrong_is_an_error_not_a_silent_empty(wav):
    with pytest.raises(SttError, match="unexpected deepgram response"):
        deepgram(FakeTransport({"results": {}})).transcribe(wav)


def test_a_network_failure_is_an_error(wav):
    transport = FakeTransport(error=urllib.error.URLError("down"))

    with pytest.raises(SttError, match="deepgram unreachable"):
        deepgram(transport).transcribe(wav)


def test_a_missing_key_says_which_variable_to_set(wav):
    with pytest.raises(SttNotConfigured, match="DEEPGRAM_API_KEY"):
        DeepgramStt(api_key=None, transport=FakeTransport()).transcribe(wav)


def test_a_recording_that_vanished_is_an_error(tmp_path):
    with pytest.raises(SttError, match="cannot read"):
        deepgram(FakeTransport()).transcribe(tmp_path / "gone.wav")


# --- OpenAI ----------------------------------------------------------------


def test_openai_folds_the_vocabulary_into_a_prompt():
    prompt = build_prompt(["rate-limiter", "mergealo", "rate-limiter"])

    assert prompt == "Vocabulario del proyecto: rate-limiter, mergealo."


def test_openai_asks_for_logprobs_because_it_has_no_confidence_field(wav):
    transport = FakeTransport({"text": "hola"})

    OpenAiStt(api_key="sk-test", transport=transport).transcribe(wav)

    body = transport.calls[-1][2].decode("utf-8", "replace")
    assert 'name="include[]"' in body and "logprobs" in body
    assert 'name="model"' in body


def test_openai_confidence_is_the_geometric_mean_of_the_token_probabilities():
    assert confidence_from_logprobs([{"logprob": 0.0}, {"logprob": 0.0}]) == 1.0
    assert confidence_from_logprobs([{"logprob": -1.0}]) == pytest.approx(0.3678, abs=1e-3)
    assert confidence_from_logprobs([]) is None
    assert confidence_from_logprobs("nope") is None


def test_openai_without_logprobs_has_no_opinion_rather_than_a_low_score(wav):
    """`None` must not trip the read-back gate — that is a different meaning."""
    result = OpenAiStt(api_key="sk-test", transport=FakeTransport({"text": "hola"})).transcribe(wav)

    assert result.text == "hola"
    assert result.confidence is None


def test_openai_parses_the_transcript_and_its_logprobs(wav):
    payload = {"text": " dale  mandale ", "logprobs": [{"logprob": -0.02}]}

    result = OpenAiStt(api_key="sk-test", transport=FakeTransport(payload)).transcribe(wav)

    assert result.text == "dale mandale"
    assert result.confidence == pytest.approx(0.980, abs=1e-3)


def test_the_multipart_body_carries_the_audio_and_closes_its_boundary():
    content_type, body = encode_multipart([("model", "m")], "reply.wav", b"AUDIO")

    boundary = content_type.split("boundary=")[1]
    assert body.startswith(f"--{boundary}\r\n".encode())
    assert body.endswith(f"\r\n--{boundary}--\r\n".encode())
    assert b"AUDIO" in body
    assert b'filename="reply.wav"' in body


def test_a_bad_openai_response_is_an_error(wav):
    with pytest.raises(SttError, match="unexpected openai response"):
        OpenAiStt(api_key="sk-test", transport=FakeTransport({"oops": 1})).transcribe(wav)


# --- the mock --------------------------------------------------------------


def test_the_mock_hands_back_its_script_in_order(wav):
    engine = MockStt(replies=["uno", "dale"])

    assert engine.transcribe(wav).text == "uno"
    assert engine.transcribe(wav).text == "dale"
    assert engine.transcribe(wav).text == MockStt.default


def test_the_mock_records_the_keyterms_it_was_given(wav):
    engine = MockStt()

    engine.transcribe(wav, ["draft-mode"])

    assert engine.calls == [(str(wav), ["draft-mode"])]


# --- the registry ----------------------------------------------------------


def test_the_provider_comes_from_config(config):
    assert stt.create(config).name == "deepgram"


def test_openai_is_selectable(config):
    config.data["speech_to_text"]["provider"] = "openai"

    assert stt.create(config).name == "openai"


@pytest.mark.parametrize("provider", ["whisper-cpp", "deepgram_ws"])
def test_a_planned_provider_says_so_instead_of_falling_back(config, provider):
    """Silently transcribing with a different engine is a debugging trap."""
    config.data["speech_to_text"]["provider"] = provider

    with pytest.raises(SttNotImplemented, match="planned, not implemented"):
        stt.create(config)


def test_an_unknown_provider_is_rejected(config):
    config.data["speech_to_text"]["provider"] = "telepathy"

    with pytest.raises(SttNotImplemented, match="unknown provider"):
        stt.create(config)


def test_the_configured_vocabulary_reaches_the_engine(config):
    engine = stt.create(config)

    assert "rate-limiter" in engine.base_keyterms
