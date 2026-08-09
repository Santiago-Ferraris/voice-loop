from __future__ import annotations

import json
import urllib.error

import pytest

from voiceloop.summarize import (
    API_URL,
    FALLBACK_SUMMARY,
    Summarizer,
    Summary,
    SummaryUnavailable,
    clean,
    describes_no_question,
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


def test_the_prompt_asks_for_what_the_turn_did_when_it_asks_nothing():
    """The live failure: "No hay expectativa de la persona en este mensaje."."""
    transport = Recorder()

    make(transport).summarize(TAIL)

    prompt = transport.calls[0]["body"]["messages"][0]["content"]
    assert "Si no pregunta nada, contá qué hizo la sesión" in prompt
    assert "Nunca describas la ausencia de una pregunta" in prompt


@pytest.mark.parametrize(
    "said",
    [
        "No hay expectativa de la persona en este mensaje",
        "no hay ninguna pregunta",
        "No hay ninguna consulta pendiente",
        "El mensaje no pregunta nada",
        "no espera nada de la persona",
        "La sesión no espera una respuesta",
    ],
)
def test_a_remark_about_there_being_no_question_is_not_a_summary(said):
    assert describes_no_question(said) is True


@pytest.mark.parametrize(
    "said",
    [
        "terminó de listar los archivos del worker",
        "corrió los tests y pasaron todos",
        "no pudo aplicar la migración, pregunta si sigue igual",
        "no hay tests para el índice nuevo, quiere escribirlos",
        "",
    ],
)
def test_a_summary_that_merely_starts_with_no_is_left_alone(said):
    assert describes_no_question(said) is False


def test_a_summary_of_nothing_is_retried_and_then_given_up_on():
    """Better "terminó y te espera" than a remark about the prompt."""
    transport = Recorder(reply("No hay expectativa de la persona en este mensaje."))

    assert make(transport).summarize(TAIL) == FALLBACK_SUMMARY
    assert len(transport.calls) == 2


def test_the_same_goes_for_the_call_that_also_names_the_window():
    transport = Recorder(named("No hay ninguna pregunta en el mensaje", "indice viejo"))

    assert make(transport).summarize_and_name(TAIL) == Summary(text=FALLBACK_SUMMARY)


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


# --- the summary and the name, in one call ---------------------------------


def named(summary: str, slug: str) -> bytes:
    return reply(json.dumps({"summary": summary, "slug": slug}))


def test_one_request_answers_with_both_the_summary_and_a_name():
    """The name and the summary come out of the same paragraph; one call, not two."""
    transport = Recorder(named("pide aprobar la migración", "indice migracion"))

    result = make(transport).summarize_and_name(TAIL)

    assert result == Summary(text="pide aprobar la migración", slug="indice migracion")
    assert len(transport.calls) == 1


def test_the_naming_request_asks_for_json_and_says_what_a_slug_is():
    transport = Recorder(named("pide aprobar", "indice migracion"))

    make(transport).summarize_and_name(TAIL)

    body = transport.calls[0]["body"]
    assert body["response_format"] == {"type": "json_object"}
    assert "slug" in body["messages"][0]["content"]


def test_a_model_that_ignores_the_slug_rules_is_corrected():
    transport = Recorder(named("pide aprobar", "Índice-De-Migración Del Worker Viejo"))

    assert make(transport).summarize_and_name(TAIL).slug == "indice de migracion del"


def test_a_response_that_is_not_json_falls_back_without_losing_the_announcement():
    transport = Recorder(reply("pide aprobar la migración"))

    result = make(transport).summarize_and_name(TAIL)

    assert result == Summary(text=FALLBACK_SUMMARY, slug="")
    assert len(transport.calls) == 2  # retried once, like every other failure


def test_a_missing_slug_still_yields_the_summary():
    """No name to offer is a normal outcome; no summary is not."""
    transport = Recorder(reply(json.dumps({"summary": "pide aprobar la migración"})))

    result = make(transport).summarize_and_name(TAIL)

    assert result == Summary(text="pide aprobar la migración", slug="")


def test_naming_without_a_key_never_calls_out():
    transport = Recorder()

    assert make(transport, api_key=None).summarize_and_name(TAIL) == Summary(FALLBACK_SUMMARY)
    assert transport.calls == []


def test_summary_unavailable_is_an_exception_type():
    with pytest.raises(SummaryUnavailable):
        raise SummaryUnavailable("boom")
