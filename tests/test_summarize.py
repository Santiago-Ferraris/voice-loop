from __future__ import annotations

import json
import urllib.error

import pytest

from voiceloop.summarize import (
    API_URL,
    FALLBACK_SUMMARY,
    Summarizer,
    SummaryUnavailable,
    clean,
)

TAIL = "Terminé la migración del índice. ¿La aplico también en staging?"


def reply(content: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")


class Recorder:
    """Stand-in transport: records the call, returns a canned body."""

    def __init__(self, body: bytes | None = None, error: Exception | None = None):
        self.body = body if body is not None else reply("pide aprobar la migración")
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append(
            {"url": url, "headers": headers, "body": json.loads(body), "timeout": timeout}
        )
        if self.error is not None:
            raise self.error
        return self.body


def make(transport, **kwargs) -> Summarizer:
    options = {"api_key": "sk-test", "max_words": 12, "timeout": 5.0}
    options.update(kwargs)
    return Summarizer(transport=transport, **options)


def test_a_successful_call_returns_the_summary():
    transport = Recorder(reply("pide aprobar la migración en staging"))

    assert make(transport).summarize(TAIL) == "pide aprobar la migración en staging"
    assert len(transport.calls) == 1


def test_the_request_carries_model_prompt_and_tail():
    transport = Recorder()

    make(transport, model="gpt-4o-mini", max_words=9).summarize(TAIL)

    call = transport.calls[0]
    assert call["url"] == API_URL
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert call["body"]["model"] == "gpt-4o-mini"
    system, user = call["body"]["messages"]
    assert system["role"] == "system" and "9 palabras" in system["content"]
    assert user == {"role": "user", "content": TAIL}


def test_the_configured_timeout_is_passed_to_the_transport():
    transport = Recorder()

    make(transport, timeout=2.5).summarize(TAIL)

    assert transport.calls[0]["timeout"] == 2.5


def test_a_timeout_retries_once_and_then_falls_back():
    transport = Recorder(error=TimeoutError("timed out"))

    assert make(transport).summarize(TAIL) == FALLBACK_SUMMARY
    assert len(transport.calls) == 2  # one attempt plus one retry, no more


def test_a_transient_failure_is_recovered_by_the_retry():
    class Flaky(Recorder):
        def __call__(self, url, headers, body, timeout):
            super().__call__(url, headers, body, timeout=timeout)
            if len(self.calls) == 1:
                raise urllib.error.URLError("connection reset")
            return reply("quiere confirmar el despliegue")

    transport = Flaky()
    transport.error = None

    assert make(transport).summarize(TAIL) == "quiere confirmar el despliegue"
    assert len(transport.calls) == 2


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("no route"),
        OSError("socket blew up"),
        ValueError("something odd"),
    ],
)
def test_every_transport_failure_falls_back(error):
    assert make(Recorder(error=error)).summarize(TAIL) == FALLBACK_SUMMARY


@pytest.mark.parametrize(
    "body",
    [b"not json", b"{}", json.dumps({"choices": []}).encode(), reply("   ")],
)
def test_an_unusable_response_falls_back(body):
    assert make(Recorder(body)).summarize(TAIL) == FALLBACK_SUMMARY


def test_without_a_key_nothing_is_sent_at_all():
    transport = Recorder()

    summarizer = make(transport, api_key=None)

    assert summarizer.available is False
    assert summarizer.summarize(TAIL) == FALLBACK_SUMMARY
    assert transport.calls == []


def test_a_disabled_provider_never_calls_out():
    transport = Recorder()

    assert make(transport, enabled=False).summarize(TAIL) == FALLBACK_SUMMARY
    assert transport.calls == []


def test_an_empty_tail_does_not_burn_a_request():
    transport = Recorder()

    assert make(transport).summarize("   ") == FALLBACK_SUMMARY
    assert transport.calls == []


def test_the_summary_is_capped_at_max_words():
    transport = Recorder(reply(" ".join(f"palabra{i}" for i in range(40))))

    summary = make(transport, max_words=5).summarize(TAIL)

    assert len(summary.split()) == 5


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("  hola   mundo \n", "hola mundo"),
        ('"entre comillas"', "entre comillas"),
        ("«guillemets»", "guillemets"),
        ("con `backticks`", "con backticks"),
        ("termina en punto.", "termina en punto"),
        ("línea uno\nlínea dos", "línea uno línea dos"),
    ],
)
def test_clean_makes_text_speakable(raw, expected):
    assert clean(raw, 20) == expected


def test_from_config_reads_model_and_timeout(config, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")

    summarizer = Summarizer.from_config(config)

    assert summarizer.model == "gpt-4o-mini"
    assert summarizer.timeout == 5
    assert summarizer.max_words == 12
    assert summarizer.enabled is True


def test_from_config_without_a_key_is_unavailable(config):
    assert Summarizer.from_config(config).available is False


def test_summary_unavailable_is_an_exception_type():
    with pytest.raises(SummaryUnavailable):
        raise SummaryUnavailable("boom")
