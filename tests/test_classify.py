"""The hybrid: phrase lists for what they know, a model for what they do not.

The sixteen phrases below are natural ways this user says things he says every
day. Fourteen of them fell through the lexicon before this existed — every one
of them arriving as *text*, which means every one of them typed into somebody's
Claude session. The two that already worked are here to hold the other half of
the contract: they must still resolve **without a packet leaving the machine**.

The transport is a fake throughout. `NoNetwork` fails the test if anything
tries to reach OpenAI at all.
"""

from __future__ import annotations

import json
import logging

import pytest

from voiceloop import intents
from voiceloop.classify import Action, Classifier, parse_actions

# Measured against `intents.parse`: all fourteen come back as plain text today.
FALLS_THROUGH = {
    "give it to me": intents.KIND_GIVE,
    "later": intents.KIND_LATER,
    "skip it": intents.KIND_SKIP,
    "show me": intents.KIND_SHOW,
    "status": intents.KIND_STATUS,
    "dale contame": intents.KIND_GIVE,
    "ok dame": intents.KIND_GIVE,
    "not now": intents.KIND_LATER,
    "push it back": intents.KIND_LATER,
    "what's pending": intents.KIND_PENDINGS,
    "tell me": intents.KIND_GIVE,
    "read it": intents.KIND_GIVE,
    "hold on": intents.KIND_WAIT,
    "what do I have": intents.KIND_PENDINGS,
}

# And the two the lexicon already knows, which must never cost a round trip.
ALREADY_WORKS = {
    "skip": intents.KIND_SKIP,
    "dámelo": intents.KIND_GIVE,
}


class NoNetwork:
    """A transport that fails the test rather than reaching anything."""

    def __call__(self, url, headers, body, timeout):  # pragma: no cover - must not run
        raise AssertionError(f"the lexicon should have answered this without {url}")


class FakeOpenAi:
    """Answers with whatever the test says the model said."""

    def __init__(self, answers=None, error: Exception | None = None):
        self.answers = dict(answers or {})
        self.error = error
        self.asked: list[str] = []
        self.timeouts: list[float] = []

    def __call__(self, url, headers, body, timeout):
        payload = json.loads(body)
        said = payload["messages"][-1]["content"]
        self.asked.append(said)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        answer = self.answers.get(said.split("\n")[-1], {"actions": []})
        return json.dumps(
            {"choices": [{"message": {"content": json.dumps(answer)}}]}
        ).encode("utf-8")


def classifier(transport, **kwargs) -> Classifier:
    kwargs.setdefault("api_key", "sk-test")
    return Classifier(transport=transport, **kwargs)


# --- the fourteen ----------------------------------------------------------


@pytest.mark.parametrize("said,kind", sorted(FALLS_THROUGH.items()))
def test_the_lexicon_alone_misses_all_of_these(said, kind):
    """The measurement this whole module exists because of."""
    assert intents.parse(said).kind == intents.KIND_TEXT


@pytest.mark.parametrize("said,kind", sorted(FALLS_THROUGH.items()))
def test_and_the_model_resolves_every_one_of_them(said, kind):
    fake = FakeOpenAi({said: {"actions": [{"intent": kind}]}})

    actions = classifier(fake).classify(said)

    assert actions == [Action(kind=kind)]


@pytest.mark.parametrize("said,kind", sorted(ALREADY_WORKS.items()))
def test_what_the_lexicon_knows_never_reaches_the_network(said, kind):
    """Half the design: instant and offline for everything already covered."""
    assert intents.parse(said).kind == kind


# --- and the answer that keeps it honest -----------------------------------


def test_a_real_instruction_is_not_a_command_at_all():
    """"Ninguno" has to be available, or every dictated sentence becomes one."""
    said = "mergealo cuando pasen los tests"
    fake = FakeOpenAi({said: {"actions": []}})

    assert classifier(fake).classify(said) == []


def test_an_empty_list_is_not_the_same_as_no_answer():
    """`[]` is "that is dictation"; `None` is "I could not ask"."""
    fake = FakeOpenAi({"hola": {"actions": []}})

    assert classifier(fake).classify("hola") == []
    assert classifier(fake, api_key=None).classify("hola") is None


# --- degrading ------------------------------------------------------------


def test_the_network_being_down_degrades_to_the_lexicon():
    fake = FakeOpenAi(error=TimeoutError("timed out"))

    assert classifier(fake).classify("give it to me") is None


def test_so_does_a_reply_that_is_not_json():
    class Garbage:
        def __call__(self, url, headers, body, timeout):
            return b"<html>502</html>"

    assert classifier(Garbage()).classify("give it to me") is None


def test_so_does_a_provider_that_is_turned_off(config):
    subject = Classifier.from_config(config, transport=NoNetwork())

    assert subject.available is False
    assert subject.classify("give it to me") is None


def test_the_leash_is_short():
    fake = FakeOpenAi({"later": {"actions": [{"intent": "later"}]}})

    classifier(fake, timeout=2.0).classify("later")

    assert fake.timeouts == [2.0]


def test_dictation_long_enough_to_be_dictation_is_never_asked_about():
    subject = classifier(NoNetwork(), max_words=5)

    assert subject.classify("uno dos tres cuatro cinco seis") is None
    assert subject.calls == 0


def test_a_take_dropped_for_its_length_says_so_in_the_log(caplog):
    """It used to vanish without a line, and a ninety-word take is nearly
    always our own voice that the echo filter failed to subtract."""
    subject = classifier(NoNetwork(), max_words=5)

    with caplog.at_level(logging.INFO, logger="voiceloop.classify"):
        assert subject.classify("uno dos tres cuatro cinco seis") is None

    assert "too long to classify (6 words, limit 5)" in caplog.text


def test_silence_is_never_asked_about():
    assert classifier(NoNetwork()).classify("") is None


# --- several things in one breath ------------------------------------------


def test_one_phrase_can_ask_for_several_things_in_order():
    said = "ok dámelo, y también abrí una ventana nueva y hacé el rebase"
    fake = FakeOpenAi(
        {
            said: {
                "actions": [
                    {"intent": "give"},
                    {"intent": "open", "text": "hacé el rebase"},
                ]
            }
        }
    )

    assert classifier(fake).classify(said) == [
        Action(kind=intents.KIND_GIVE),
        Action(kind=intents.KIND_OPEN, text="hacé el rebase"),
    ]


def test_a_window_is_addressed_by_name():
    said = "decile a inbox realtime que espere"
    fake = FakeOpenAi(
        {
            said: {
                "actions": [
                    {"intent": "tell", "target": "inbox realtime", "text": "esperá"}
                ]
            }
        }
    )

    assert classifier(fake).classify(said) == [
        Action(kind=intents.KIND_TELL, target="inbox realtime", text="esperá")
    ]


def test_the_windows_that_exist_are_named_in_the_prompt():
    """A model that has not been told they exist invents targets that name nothing."""
    fake = FakeOpenAi()

    classifier(fake).classify("decile a inbox que espere", ["inbox realtime", "kb guard"])

    assert "inbox realtime" in fake.asked[0]
    assert "kb guard" in fake.asked[0]


# --- parsing what came back ------------------------------------------------


def test_an_intent_nobody_has_heard_of_is_dropped_not_obeyed():
    assert parse_actions('{"actions": [{"intent": "rm -rf"}, {"intent": "give"}]}') == [
        Action(kind=intents.KIND_GIVE)
    ]


@pytest.mark.parametrize(
    "payload", ["", "not json", "[]", '{"actions": "give"}', '{"nope": []}']
)
def test_a_reply_of_the_wrong_shape_is_no_reply(payload):
    assert parse_actions(payload) is None


def test_the_body_asks_for_json_and_nothing_creative():
    body = json.loads(classifier(FakeOpenAi()).build_body("later"))

    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 0
    assert body["model"] == "gpt-4o-mini"


def test_every_intent_it_may_answer_with_is_in_the_prompt():
    subject = classifier(FakeOpenAi())
    catalogue = subject.catalogue()

    for kind in (intents.KIND_GIVE, intents.KIND_OPEN, intents.KIND_TELL):
        assert kind in catalogue
