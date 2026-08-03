"""Getting the answer into the right window.

Two mechanics, and the hook payload — never the screen — says which one to use.

**Free text** is typed with `newline no` and submitted with a separate CR. It
arrives as a real user turn, which is also how the item resolves itself: the
`UserPromptSubmit` that the injection triggers fires the `activity` hook, and
the queue closes the item without anyone telling it to.

**Menus** ignore typed text. The selector is driven with arrow keys, and the
index comes from `tool_input`:

* single choice — option N is N-1 `ESC [ B` then CR;
* multiple choice — walk down toggling with space, then `ESC [ C` onto the
  Submit tab and CR;
* neither of the offered answers — the menu's own free-text row, which sits at
  `len(options) + 1` for a question and at a configured index for a plan. You
  must land on the row *before* typing; text typed while another row is
  selected is swallowed.

Never read the index off the render. The menu shows rows the payload does not
have ("Type something.", "Chat about this"), and a plan menu shows four rows
for a payload that carries none at all.

Before any of it, two guards. The session has to still exist — a window closed
between the announce and the answer must not have its keystrokes delivered to
whatever inherited the tty. And the phrase has to clear the confirmation gate:
low recognizer confidence, or a match against the destructive-phrase list,
means it gets read back to you instead of sent. That list is the only thing
standing between a mumbled "borrá prod" and `defaultMode: auto`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from . import iterm
from .iterm import ARROW_DOWN, ARROW_RIGHT, ENTER, SPACE

log = logging.getLogger("voiceloop.delivery")

KIND_QUESTION = "question"
KIND_PLAN = "plan"

# The plan menu is Claude's own, so its rows are not in the payload. Verified
# against Claude Code 2.1.220: 1 "Yes, and use auto mode", 2 "Yes, manually
# approve edits", 3 refine elsewhere, 4 "Tell Claude what to change". Only the
# two approvals and the text row are offered by voice; row 3 is a hand-off to
# another tool and has nothing to say out loud. The text row is configurable
# because it is the one index we cannot derive from a payload.
PLAN_FEEDBACK = 4

PLAN_LABELS = ("aprobar y seguir en auto", "aprobar revisando cada edición")


@dataclass(frozen=True)
class MenuOption:
    label: str
    description: str = ""


@dataclass(frozen=True)
class Menu:
    prompt: str
    options: tuple[MenuOption, ...] = ()
    multi_select: bool = False
    kind: str = KIND_QUESTION
    position: int = 1
    total: int = 1
    free_text_index: int = 0

    @property
    def labels(self) -> list[str]:
        return [option.label for option in self.options]

    def describe(self, index: int) -> str:
        if 1 <= index <= len(self.options):
            option = self.options[index - 1]
            return option.description or option.label
        return ""


@dataclass(frozen=True)
class Gate:
    required: bool
    reason: str = ""


@dataclass
class GatePolicy:
    threshold: float = 0.75
    patterns: tuple[str, ...] = ()
    _compiled: list = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        for pattern in self.patterns:
            try:
                self._compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error:
                # A typo in the user's blacklist must not take the daemon down.
                log.warning("delivery.confirm_if_matches: skipping invalid regex %r", pattern)

    @classmethod
    def from_config(cls, config) -> "GatePolicy":
        patterns = config.get("delivery.confirm_if_matches") or []
        if not isinstance(patterns, (list, tuple)):
            patterns = []
        return cls(
            threshold=float(config.get("delivery.confirm_below_confidence", 0.75)),
            patterns=tuple(str(pattern) for pattern in patterns),
        )

    def check(self, text: str, confidence: float | None) -> Gate:
        for compiled in self._compiled:
            if compiled.search(text or ""):
                return Gate(True, "Ojo con esto")
        return self.check_choice(confidence)

    def check_choice(self, confidence: float | None) -> Gate:
        """Picking an option Claude offered cannot be destructive — only misheard.

        So the blacklist does not apply to a menu answer; the confidence
        threshold still does, because "dos" and "doce" sound alike.
        `None` means the provider has no opinion, which is not a low score.
        """
        if confidence is not None and confidence < self.threshold:
            return Gate(True, "No te escuché bien")
        return Gate(False)


def _as_options(raw: Any) -> tuple[MenuOption, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    options: list[MenuOption] = []
    for entry in raw:
        if isinstance(entry, Mapping):
            label = str(entry.get("label") or entry.get("description") or "").strip()
            description = str(entry.get("description") or "").strip()
        else:
            label, description = str(entry).strip(), ""
        if label:
            options.append(MenuOption(label=label, description=description))
    return tuple(options)


def menus_from_payload(payload: Mapping[str, Any], *, plan_feedback: int = PLAN_FEEDBACK) -> list[Menu]:
    """Every menu the payload describes, in the order Claude will show them."""
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, Mapping) else {}

    plan = tool_input.get("plan")
    is_plan = isinstance(plan, str) or str(payload.get("tool") or "") == "ExitPlanMode"
    if is_plan:
        return [
            Menu(
                prompt=str(plan or "").strip(),
                options=tuple(MenuOption(label) for label in PLAN_LABELS),
                kind=KIND_PLAN,
                free_text_index=plan_feedback,
            )
        ]

    questions = tool_input.get("questions")
    if not isinstance(questions, Sequence) or isinstance(questions, (str, bytes)):
        return []
    entries = [entry for entry in questions if isinstance(entry, Mapping)]
    menus: list[Menu] = []
    for position, entry in enumerate(entries, start=1):
        options = _as_options(entry.get("options"))
        menus.append(
            Menu(
                prompt=str(entry.get("question") or entry.get("header") or "").strip(),
                options=options,
                multi_select=bool(entry.get("multiSelect")),
                kind=KIND_QUESTION,
                position=position,
                total=len(entries),
                # The menu's own "Type something." row, always one past the
                # options the payload lists.
                free_text_index=len(options) + 1,
            )
        )
    return menus


# -- keystroke builders -----------------------------------------------------


def select_keystrokes(index: int) -> list[str]:
    """The cursor starts on option 1, so option N takes N-1 arrows, then CR."""
    if index < 1:
        raise ValueError(f"option index must be 1-based, got {index}")
    return [ARROW_DOWN] * (index - 1) + [ENTER]


def multi_select_keystrokes(indexes: Sequence[int]) -> list[str]:
    """Toggle each option with space on the way down, then submit from the tab."""
    targets = sorted({int(index) for index in indexes})
    if not targets:
        raise ValueError("multi-select needs at least one option")
    if targets[0] < 1:
        raise ValueError(f"option index must be 1-based, got {targets[0]}")
    strokes: list[str] = []
    position = 1
    for target in targets:
        strokes += [ARROW_DOWN] * (target - position)
        strokes.append(SPACE)
        position = target
    # Right moves off the question onto the review tab, where CR submits.
    strokes += [ARROW_RIGHT, ENTER]
    return strokes


def free_text_keystrokes(index: int) -> list[str]:
    """Land on the menu's text row. The typing happens after these."""
    if index < 1:
        raise ValueError(f"option index must be 1-based, got {index}")
    return [ARROW_DOWN] * (index - 1)


# -- the delivery itself ----------------------------------------------------


class Delivery:
    """Every write into someone else's window goes through here."""

    def __init__(self, *, runner=None):
        self.runner = runner

    def alive(self, tty: str) -> bool:
        return iterm.session_exists(tty, self.runner)

    def _require_alive(self, tty: str) -> None:
        if not self.alive(tty):
            raise iterm.SessionGone(f"no iTerm2 session on {tty or '(no tty)'}")

    def send_text(self, tty: str, text: str) -> None:
        self._require_alive(tty)
        iterm.write_text(tty, text, newline=True, runner=self.runner)

    def send_choice(self, tty: str, index: int) -> None:
        self._require_alive(tty)
        iterm.send_keys(tty, select_keystrokes(index), self.runner)

    def send_choices(self, tty: str, indexes: Sequence[int]) -> None:
        self._require_alive(tty)
        iterm.send_keys(tty, multi_select_keystrokes(indexes), self.runner)

    def send_menu_text(self, tty: str, index: int, text: str) -> None:
        """Navigate to the menu's text row first — typing before that is lost."""
        self._require_alive(tty)
        iterm.send_keys(tty, free_text_keystrokes(index), self.runner)
        iterm.write_text(tty, text, newline=True, runner=self.runner)

    def focus(self, tty: str) -> bool:
        return iterm.focus(tty, self.runner)
