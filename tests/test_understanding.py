"""The hybrid and the compounds, from the microphone to the window.

Same harness as the reply cycle — a stub mic, `MockStt` for the words — with
the classifier's transport faked so the model's answers are fixtures. What is
being checked here is not the classifier (that is `test_classify.py`) but what
the daemon *does* with a plan: acts on it, in order, without letting one failed
part of a sentence cancel the rest.
"""

from __future__ import annotations

import asyncio
import json
import logging

from voiceloop import intents
from voiceloop.classify import (
    SOURCE_DOUBTFUL,
    SOURCE_LEXICON,
    SOURCE_LLM,
    SOURCE_UNAVAILABLE,
    Classifier,
)
from voiceloop.daemon import REPLY_DELIVERED, REPLY_PENDING

from conftest import TTY, RecordingDelivery
from test_echo import INSTRUCTION, PENDINGS, PENDINGS_HEARD
from test_reply_cycle import answer, queue


class NoNetwork:
    """Fails the test rather than reaching OpenAI."""

    def __call__(self, url, headers, body, timeout):  # pragma: no cover
        raise AssertionError("the lexicon should have answered this offline")


class FakeModel:
    def __init__(self, answers):
        self.answers = dict(answers)
        self.asked: list[str] = []

    def __call__(self, url, headers, body, timeout):
        said = json.loads(body)["messages"][-1]["content"].split("\n")[-1]
        self.asked.append(said)
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"actions": self.answers.get(said, [])}
                            )
                        }
                    }
                ]
            }
        ).encode("utf-8")


def understanding(answers=None, *, transport=None) -> Classifier:
    return Classifier(
        api_key="sk-test", transport=transport or FakeModel(answers or {})
    )


# --- the model filling in for the lexicon ----------------------------------


def test_a_phrase_the_lexicon_never_heard_of_still_asks_for_the_summary(build):
    """"give it to me" is "dámelo", and it used to be typed into the window."""
    daemon = build(
        ["give it to me", "mergealo"],
        classifier=understanding({"give it to me": [{"intent": "give"}]}),
    )
    queue(daemon, kind="stop")

    asyncio.run(daemon.announce_next())

    assert daemon.delivery.sent == [("text", TTY, "mergealo")]


def test_what_the_lexicon_knows_never_reaches_the_network(build):
    """The assertion, not the aspiration: the transport fails the test if used."""
    daemon = build(["dámelo", "mergealo"], classifier=understanding(transport=NoNetwork()))
    queue(daemon, kind="stop")

    asyncio.run(daemon.announce_next())

    assert daemon.delivery.sent == [("text", TTY, "mergealo")]


def test_a_real_instruction_is_still_a_real_instruction(build):
    """The model answered "that is not a command", so it is dictation."""
    model = FakeModel({"mergealo cuando pasen los tests": []})
    daemon = build(["mergealo cuando pasen los tests"], classifier=understanding(transport=model))
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_DELIVERED
    assert daemon.delivery.sent == [("text", TTY, "mergealo cuando pasen los tests")]
    assert model.asked == ["mergealo cuando pasen los tests"]


def test_a_model_that_is_down_behaves_exactly_as_before(build):
    class Dead:
        def __call__(self, url, headers, body, timeout):
            raise TimeoutError("timed out")

    daemon = build(["mergealo cuando pasen los tests"], classifier=understanding(transport=Dead()))
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_DELIVERED
    assert daemon.delivery.sent == [("text", TTY, "mergealo cuando pasen los tests")]


def test_a_doubtful_transcript_is_asked_about_instead_of_being_modelled(build):
    """A near miss never reaches the model: we distrust the words, not the meaning."""
    daemon = build(
        ["dame al pendiente", "no"], classifier=understanding(transport=NoNetwork())
    )
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_PENDING
    assert daemon.delivery.sent == []


# --- several things in one breath ------------------------------------------


def test_two_things_in_one_phrase_both_happen_in_order(build):
    said = "mergealo y abrí una ventana nueva"
    daemon = build(
        [said],
        classifier=understanding(
            {
                said: [
                    {"intent": "text", "text": "mergealo"},
                    {"intent": "open", "text": "corré los tests"},
                ]
            }
        ),
    )
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_DELIVERED
    assert daemon.delivery.sent == [("text", TTY, "mergealo")]
    assert daemon.delivery.opened == [("", "corré los tests")]


def test_one_that_fails_does_not_cancel_the_others(build):
    class HalfBrokenDelivery(RecordingDelivery):
        def open_tab(self, command="", text=""):
            raise RuntimeError("iTerm2 said no")

    said = "abrí una ventana nueva y mostrame la ventana"
    daemon = build(
        [said, ""],
        delivery=HalfBrokenDelivery(),
        classifier=understanding(
            {said: [{"intent": "open"}, {"intent": "show"}]}
        ),
    )
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_PENDING
    assert daemon.delivery.focused == [TTY]
    assert any("No pude abrir la ventana." in said for said in daemon.speaker.spoken)


def test_the_destructive_half_of_a_compound_still_confirms(build):
    said = "subilo a prod y mostrame la ventana"
    daemon = build(
        [said, "no"],
        classifier=understanding(
            {said: [{"intent": "text", "text": "subilo a prod"}, {"intent": "show"}]}
        ),
    )
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_PENDING
    assert daemon.delivery.sent == []
    assert any("Ojo con esto" in spoken for spoken in daemon.speaker.spoken)


# --- talking to a window by name -------------------------------------------


def test_a_window_can_be_addressed_by_the_name_you_gave_it(build):
    said = "decile a indice que espere"
    daemon = build(
        [said, ""],
        classifier=understanding(
            {said: [{"intent": "tell", "target": "indice", "text": "esperá"}]}
        ),
    )
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_PENDING
    assert daemon.delivery.sent == [("text", TTY, "esperá")]


def test_a_window_that_does_not_exist_is_said_out_loud_not_guessed(build):
    said = "decile a la que no existe que espere"
    daemon = build(
        [said, ""],
        classifier=understanding(
            {said: [{"intent": "tell", "target": "no existe", "text": "esperá"}]}
        ),
    )
    item = queue(daemon, kind="stop")

    assert answer(daemon, item) == REPLY_PENDING
    assert daemon.delivery.sent == []
    assert any("No encontré la ventana" in spoken for spoken in daemon.speaker.spoken)


def test_the_names_of_the_live_windows_are_what_it_matches_against(build):
    daemon = build([])

    assert daemon.window_names() == ["indice"]
    assert daemon.window_named("indice")[0] == "session-1"
    # Nobody says all four words of a window name out loud.
    assert daemon.window_named("ind")[0] == "session-1"
    assert daemon.window_named("otra cosa") == ("", "")


def test_a_window_is_reachable_at_the_last_tty_it_was_seen_on(build):
    daemon = build([])
    queue(daemon, kind="stop")

    assert daemon.store.tty_for("session-1") == TTY
    assert daemon.store.tty_for("nobody") == ""


def test_where_each_answer_came_from_is_on_the_plan(build):
    """The property worth keeping: what the lexicon knows costs no round trip."""
    from voiceloop.stt import Transcript

    daemon = build(
        [],
        classifier=understanding({"give it to me": [{"intent": "give"}]}),
    )

    def plan_for(said):
        return asyncio.run(daemon.classify(Transcript(text=said, confidence=0.99)))

    assert plan_for("dámelo").source == SOURCE_LEXICON
    assert plan_for("give it to me").source == SOURCE_LLM
    assert plan_for("mergealo cuando pasen los tests").source == SOURCE_LLM
    assert plan_for("dame al pendiente").source == SOURCE_DOUBTFUL

    daemon.classifier = Classifier(api_key=None, transport=NoNetwork())
    assert plan_for("give it to me").source == SOURCE_UNAVAILABLE


def test_an_intent_the_lexicon_cannot_produce_only_ever_comes_from_the_model():
    for kind in (intents.KIND_OPEN, intents.KIND_TELL):
        assert intents.parse("abrí una ventana nueva").kind != kind


# --- ⌥M with nothing in flight ---------------------------------------------


def hotkey(daemon) -> None:
    async def body():
        await daemon.dispatch("mic-toggle", {})
        for task in list(daemon._mic_tasks):
            await task

    asyncio.run(body())


def test_the_hotkey_opens_a_window_with_nothing_in_flight(build):
    said = "abrí una ventana nueva y hacé el rebase"
    daemon = build(
        [said],
        classifier=understanding({said: [{"intent": "open", "text": "hacé el rebase"}]}),
    )

    hotkey(daemon)

    assert daemon.delivery.opened == [("", "hacé el rebase")]
    assert daemon.delivery.sent == []


def test_the_hotkey_reaches_a_window_you_name(build):
    said = "decile a indice que espere"
    daemon = build(
        [said],
        classifier=understanding(
            {said: [{"intent": "tell", "target": "indice", "text": "esperá"}]}
        ),
    )
    # The tty is only ever learned from an event; the roster does not carry one.
    queue(daemon, kind="stop")

    hotkey(daemon)

    assert daemon.delivery.sent == [("text", TTY, "esperá")]


def test_a_sentence_with_no_window_in_front_of_it_is_never_typed(build):
    """The last window to speak was an hour ago. It is not the one you meant."""
    daemon = build(["revisá el índice"], classifier=understanding({}))

    hotkey(daemon)

    assert daemon.delivery.sent == []
    assert any("Decime a cuál le hablo." in spoken for spoken in daemon.speaker.spoken)


def test_the_hotkey_still_answers_the_window_that_spoke_last(build):
    daemon = build(["dámelo", "mergealo"], classifier=understanding({}))
    queue(daemon, kind="stop")
    asyncio.run(daemon.announce_next())
    daemon.delivery.sent.clear()

    daemon.stt.replies = ["y agregá un test"]
    hotkey(daemon)

    assert daemon.delivery.sent == [("text", TTY, "y agregá un test")]


# --- the two holes, measured on the installed system ------------------------
#
# Synthetic voice through the speakers, against the daemon as it ships today:
#
#   "dame los pendientes" -> 'dame los pendientes'  exact, answered.  works
#   "estado"              -> 'estado'               exact, answered.  works
#   "dámelo"              -> 'chamelo'              -> text -> nothing happened
#   "later"               -> 'later'                -> text -> nothing happened
#
# What is in the lexicon and transcribes cleanly already works. The two holes
# are imperfect transcription and phrasing the lexicon never heard of, and they
# are the two halves of this module.


def test_chamelo_is_understood_and_then_asked_about(build):
    """The recognizer mangled it; the model reads through it; we still ask.

    "chamelo" scores 0.77 against "damelo" — under the near-miss line — so it
    reaches the model, which has no trouble with it. But it arrived at 0.75
    confidence, and a command built out of words the recognizer doubted is
    asked about before it runs.
    """
    daemon = build(
        ["chamelo", "dale", "mergealo"],
        confidence=0.75,
        classifier=understanding({"chamelo": [{"intent": "give"}]}),
    )
    queue(daemon, kind="stop")

    asyncio.run(daemon.announce_next())

    assert daemon.speaker.spoken[0] == "Entendí: chamelo. ¿Querés que te lo lea?"
    assert daemon.delivery.sent == [("text", TTY, "mergealo")]


def test_later_is_understood_without_being_in_the_lexicon(build):
    """Transcribed perfectly, and the lexicon has no English. It did nothing."""
    item_ids = []
    daemon = build(
        ["later"], classifier=understanding({"later": [{"intent": "later"}]})
    )
    item_ids.append(queue(daemon, kind="stop"))

    asyncio.run(daemon.announce_next())

    assert daemon.delivery.sent == []
    assert daemon.store.get(item_ids[0]).deferred_at is not None


def test_what_already_worked_still_works_and_still_offline(build):
    """The other half of the baseline: do not regress what was answering."""
    daemon = build(["dame los pendientes", ""], classifier=understanding(transport=NoNetwork()))
    queue(daemon, kind="stop")

    asyncio.run(daemon.announce_next())

    assert any("pendiente" in spoken.lower() for spoken in daemon.speaker.spoken)


# --- the word limit, and where the echo has to be taken out first -----------


def test_the_word_limit_is_measured_on_what_is_left_of_the_take(build):
    """The one that made the whole thing unusable, end to end.

    Reading three pendings out loud is twenty-five seconds under an open
    microphone, so the take that comes back is seventy-five words of our own
    voice with the instruction on the end of it. Handed over whole it is too
    long to classify and nothing happens at all; the limit only means anything
    once our own sentence has been subtracted, which is why the order is this
    way round in `listen`.
    """
    model = FakeModel({INSTRUCTION: [{"intent": "tell", "target": "darwin e4"}]})
    daemon = build([PENDINGS_HEARD], classifier=understanding(transport=model))

    transcript = asyncio.run(daemon.say_and_listen(text=PENDINGS))
    plan = asyncio.run(daemon.classify(transcript))

    assert transcript.text == INSTRUCTION
    assert model.asked == [INSTRUCTION]
    assert [action.kind for action in plan.actions] == [intents.KIND_TELL]


def test_what_was_subtracted_and_what_was_kept_are_both_in_the_log(build, caplog):
    """There was no line at all for the take that lost the instruction.

    Both halves have to be visible: the take as it arrived says what was
    subtracted, and the remainder says what survived. One without the other
    leaves you guessing which half the filter got wrong.
    """
    daemon = build([PENDINGS_HEARD], classifier=understanding(transport=NoNetwork()))

    with caplog.at_level(logging.INFO, logger="voiceloop"):
        asyncio.run(daemon.say_and_listen(text=PENDINGS))

    assert f"echo filtered: {PENDINGS_HEARD!r} -> {INSTRUCTION!r}" in caplog.text


def test_a_take_with_no_echo_in_it_reaches_the_model_word_for_word(build):
    said = "Nuevo evento de darwin e4."
    heard = "decile que deje el modelo fijo y que no vuelva a tocar el alias"
    model = FakeModel({heard: []})
    daemon = build([heard], classifier=understanding(transport=model))

    transcript = asyncio.run(daemon.say_and_listen(text=said))
    asyncio.run(daemon.classify(transcript))

    assert transcript.text == heard
    assert model.asked == [heard]
