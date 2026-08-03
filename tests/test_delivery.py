"""Routing an answer into a window.

The rule these tests defend is *derive the index from the payload*. The menu on
screen has rows the payload does not — "Type something.", "Chat about this",
and for a plan four rows for a payload that carries none — so counting what is
rendered would pick the wrong one. Every index here comes from `tool_input`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from voiceloop import delivery as delivery_mod, iterm
from voiceloop.delivery import (
    Delivery,
    GatePolicy,
    Menu,
    free_text_keystrokes,
    menus_from_payload,
    multi_select_keystrokes,
    select_keystrokes,
)

DOWN = iterm.ARROW_DOWN
RIGHT = iterm.ARROW_RIGHT
CR = iterm.ENTER
SPACE = iterm.SPACE

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hooks"


def hook_payload(name: str) -> dict:
    raw = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return {"tool": raw.get("tool_name", ""), "tool_input": raw.get("tool_input", {})}


class FakeOsascript:
    def __init__(self, *results: str):
        self.results = list(results) or ["found"]
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        result = self.results[0] if len(self.results) == 1 else self.results.pop(0)
        return subprocess.CompletedProcess(argv, 0, result, "")

    @property
    def keystrokes(self) -> list[list[str]]:
        """Just the argv tails, i.e. what was typed, with the tty dropped."""
        return [call[3:] for call in self.calls]


# --- keystroke builders ----------------------------------------------------


def test_the_first_option_is_just_enter():
    """The cursor already sits on option 1."""
    assert select_keystrokes(1) == [CR]


def test_option_n_takes_n_minus_one_arrows():
    assert select_keystrokes(2) == [DOWN, CR]
    assert select_keystrokes(3) == [DOWN, DOWN, CR]
    assert select_keystrokes(5) == [DOWN] * 4 + [CR]


def test_an_index_that_is_not_one_based_is_a_bug():
    for bad in (0, -1):
        with pytest.raises(ValueError, match="1-based"):
            select_keystrokes(bad)


def test_multi_select_toggles_with_space_on_the_way_down_then_submits():
    """Space toggles; Enter would toggle too. Submitting means the review tab."""
    assert multi_select_keystrokes([1, 3]) == [SPACE, DOWN, DOWN, SPACE, RIGHT, CR]


def test_multi_select_walks_the_list_once_in_order():
    assert multi_select_keystrokes([3, 1]) == [SPACE, DOWN, DOWN, SPACE, RIGHT, CR]
    assert multi_select_keystrokes([2]) == [DOWN, SPACE, RIGHT, CR]


def test_multi_select_ignores_a_repeated_option():
    assert multi_select_keystrokes([2, 2]) == [DOWN, SPACE, RIGHT, CR]


def test_multi_select_needs_something_to_select():
    with pytest.raises(ValueError, match="at least one"):
        multi_select_keystrokes([])


def test_the_free_text_row_is_navigated_to_but_not_confirmed():
    """Typing happens after these; the CR comes with the text."""
    assert free_text_keystrokes(4) == [DOWN, DOWN, DOWN]
    assert free_text_keystrokes(1) == []


# --- payload -> menu -------------------------------------------------------


def test_a_question_menu_comes_out_of_the_payload():
    menus = menus_from_payload(hook_payload("pre_ask_user_question.json"))

    assert len(menus) == 1
    assert menus[0].prompt == "¿Qué base de datos uso para el índice?"
    assert menus[0].labels == ["SQLite", "Postgres"]
    assert menus[0].multi_select is False


def test_the_free_text_row_sits_one_past_the_options_the_payload_lists():
    """On screen that row is "Type something." — it is never in `tool_input`."""
    menus = menus_from_payload(hook_payload("pre_ask_user_question.json"))

    assert menus[0].free_text_index == 3


def test_the_render_has_rows_the_payload_does_not_and_they_are_ignored():
    payload = {
        "tool": "AskUserQuestion",
        "tool_input": {"questions": [{"question": "¿?", "options": ["Rojo", "Verde", "Azul"]}]},
    }

    menu = menus_from_payload(payload)[0]

    # Three options in the payload; the menu also renders 4 and 5.
    assert select_keystrokes(3) == [DOWN, DOWN, CR]
    assert menu.free_text_index == 4


def test_multi_select_is_read_off_the_payload():
    payload = {
        "tool": "AskUserQuestion",
        "tool_input": {
            "questions": [{"question": "¿?", "options": ["Pera", "Uva"], "multiSelect": True}]
        },
    }

    assert menus_from_payload(payload)[0].multi_select is True


def test_every_question_of_a_multi_question_payload_becomes_a_menu():
    payload = {
        "tool": "AskUserQuestion",
        "tool_input": {
            "questions": [
                {"question": "una", "options": ["a", "b"]},
                {"question": "otra", "options": ["c"]},
            ]
        },
    }

    menus = menus_from_payload(payload)

    assert [menu.prompt for menu in menus] == ["una", "otra"]
    assert [(menu.position, menu.total) for menu in menus] == [(1, 2), (2, 2)]


def test_a_plan_menu_has_no_options_in_the_payload_so_they_are_known_not_read():
    menus = menus_from_payload(hook_payload("pre_exit_plan_mode.json"))

    assert len(menus) == 1
    assert menus[0].kind == delivery_mod.KIND_PLAN
    assert menus[0].labels == list(delivery_mod.PLAN_LABELS)
    assert menus[0].free_text_index == delivery_mod.PLAN_FEEDBACK


def test_the_plan_free_text_row_is_configurable_because_it_cannot_be_derived():
    menus = menus_from_payload(hook_payload("pre_exit_plan_mode.json"), plan_feedback=3)

    assert menus[0].free_text_index == 3


def test_a_payload_with_nothing_menu_shaped_produces_no_menu():
    assert menus_from_payload({}) == []
    assert menus_from_payload({"tool_input": {"questions": "nope"}}) == []


def test_option_descriptions_are_kept_for_explaining():
    menu = menus_from_payload(hook_payload("pre_ask_user_question.json"))[0]

    assert menu.describe(1) == "Un archivo local, cero servicios."
    assert menu.describe(9) == ""


# --- the confirmation gate -------------------------------------------------


def policy(**kwargs) -> GatePolicy:
    defaults = dict(threshold=0.75, patterns=(r"\bdrop\b", r"\bdelete\b", "force push", r"\bprod\b"))
    defaults.update(kwargs)
    return GatePolicy(**defaults)


@pytest.mark.parametrize(
    "said",
    [
        "hacé un drop de la tabla",
        "delete todo eso",
        "force push a main",
        "corré eso en prod",
    ],
)
def test_every_blacklisted_phrase_is_read_back_instead_of_sent(said):
    gate = policy().check(said, 0.99)

    assert gate.required is True
    assert gate.reason


def test_a_confident_ordinary_phrase_goes_straight_through():
    assert policy().check("mergealo cuando pasen los tests", 0.98).required is False


def test_low_confidence_is_read_back():
    assert policy().check("mergealo", 0.5).required is True


def test_the_threshold_is_exclusive_at_the_boundary():
    assert policy(threshold=0.75).check("hola", 0.75).required is False
    assert policy(threshold=0.75).check("hola", 0.749).required is True


def test_no_confidence_at_all_is_not_a_low_score():
    """`None` means the provider has no opinion — OpenAI without logprobs."""
    assert policy().check("mergealo", None).required is False


def test_a_blacklisted_phrase_is_stopped_even_at_full_confidence():
    assert policy().check("borrá la base en prod", 1.0).required is True


def test_a_menu_answer_skips_the_blacklist_but_not_the_threshold():
    """Picking an option Claude offered cannot be destructive, only misheard."""
    assert policy().check_choice(0.99).required is False
    assert policy().check_choice(0.5).required is True


def test_a_typo_in_the_blacklist_does_not_take_the_daemon_down():
    subject = policy(patterns=("[unclosed", r"\bprod\b"))

    assert subject.check("tocá prod", 0.99).required is True
    assert subject.check("todo bien", 0.99).required is False


def test_the_blacklist_comes_from_config(config):
    subject = GatePolicy.from_config(config)

    assert subject.threshold == 0.75
    assert subject.check("force push", 1.0).required is True


# --- delivering ------------------------------------------------------------


def test_free_text_is_typed_then_submitted():
    runner = FakeOsascript("found", "sent", "sent")

    Delivery(runner=runner).send_text("/dev/ttys012", "mergealo")

    assert runner.keystrokes == [[], ["mergealo"], ["13"]]


def test_a_menu_answer_is_arrows_and_a_carriage_return():
    runner = FakeOsascript("found", "sent")

    Delivery(runner=runner).send_choice("/dev/ttys012", 3)

    assert runner.keystrokes[-1] == ["27,91,66", "27,91,66", "13"]


def test_a_multi_select_answer_toggles_then_submits_from_the_tab():
    runner = FakeOsascript("found", "sent")

    Delivery(runner=runner).send_choices("/dev/ttys012", [1, 3])

    assert runner.keystrokes[-1] == ["32", "27,91,66", "27,91,66", "32", "27,91,67", "13"]


def test_menu_free_text_lands_on_the_row_before_it_types():
    """Text typed while another row is selected is swallowed by the menu."""
    runner = FakeOsascript("found", "sent", "sent", "sent")

    Delivery(runner=runner).send_menu_text("/dev/ttys012", 4, "sumale un paso tres")

    assert runner.keystrokes == [
        [],
        ["27,91,66", "27,91,66", "27,91,66"],
        ["sumale un paso tres"],
        ["13"],
    ]


@pytest.mark.parametrize(
    "call",
    [
        lambda subject: subject.send_text("/dev/ttys999", "hola"),
        lambda subject: subject.send_choice("/dev/ttys999", 1),
        lambda subject: subject.send_choices("/dev/ttys999", [1]),
        lambda subject: subject.send_menu_text("/dev/ttys999", 2, "hola"),
    ],
)
def test_nothing_is_delivered_to_a_window_that_closed(call):
    """The tty may already belong to something else. Check first, always."""
    runner = FakeOsascript("missing")
    subject = Delivery(runner=runner)

    with pytest.raises(iterm.SessionGone):
        call(subject)

    assert len(runner.calls) == 1  # the liveness check, and nothing after it


def test_the_liveness_check_runs_before_every_delivery():
    runner = FakeOsascript("found", "sent", "sent")

    Delivery(runner=runner).send_text("/dev/ttys012", "hola")

    assert runner.calls[0][:3] == ["osascript", "-", "/dev/ttys012"]


def test_focus_is_the_only_thing_that_moves_the_window():
    runner = FakeOsascript("focused")

    assert Delivery(runner=runner).focus("/dev/ttys012") is True


def test_a_menu_carries_its_labels_for_the_parser():
    menu = Menu(prompt="¿?", options=(delivery_mod.MenuOption("Sí"),))

    assert menu.labels == ["Sí"]
