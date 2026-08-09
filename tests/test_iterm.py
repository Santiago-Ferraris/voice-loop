from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

from voiceloop import iterm


class FakeOsascript:
    def __init__(self, stdout: str = "found", returncode: int = 0, error: Exception | None = None):
        self.stdout = stdout
        self.returncode = returncode
        self.error = error
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "")


def test_the_tty_travels_as_an_argument_not_in_the_script():
    argv = iterm.osascript_argv(iterm.FIND_SESSION, ["/dev/ttys012"])

    assert argv == ["osascript", "-", "/dev/ttys012"]


def test_the_script_never_interpolates_its_arguments():
    """An AppleScript built by concatenation is an injection waiting to happen."""
    assert "on run argv" in iterm.FIND_SESSION
    assert "item 1 of argv" in iterm.FIND_SESSION
    assert "%s" not in iterm.FIND_SESSION and "{" not in iterm.FIND_SESSION


def test_session_exists_reports_a_match():
    runner = FakeOsascript("found")

    assert iterm.session_exists("/dev/ttys012", runner) is True
    assert runner.calls == [["osascript", "-", "/dev/ttys012"]]


def test_session_exists_reports_a_miss():
    assert iterm.session_exists("/dev/ttys999", FakeOsascript("missing")) is False


def test_an_empty_tty_never_runs_osascript():
    runner = FakeOsascript()

    assert iterm.session_exists("", runner) is False
    assert runner.calls == []


def test_a_failing_osascript_is_reported_as_no_match():
    assert iterm.session_exists("/dev/ttys012", FakeOsascript(returncode=1)) is False


def test_osascript_missing_from_the_box_does_not_raise():
    runner = FakeOsascript(error=FileNotFoundError("osascript"))

    assert iterm.session_exists("/dev/ttys012", runner) is False


def test_run_script_raises_on_failure():
    runner = FakeOsascript(returncode=1)

    try:
        iterm.run_script(iterm.FIND_SESSION, ["/dev/ttys012"], runner)
    except iterm.AppleScriptError:
        return
    raise AssertionError("expected AppleScriptError")


def test_looking_a_session_up_never_moves_focus():
    assert "select" not in iterm.FIND_SESSION
    assert "activate" not in iterm.FIND_SESSION


# --- delivery mechanics ----------------------------------------------------


def test_a_keystroke_crosses_the_boundary_as_decimal_code_points():
    """Only digits and commas reach argv — never a raw control byte."""
    assert iterm.encode_keystroke(iterm.ARROW_DOWN) == "27,91,66"
    assert iterm.encode_keystroke(iterm.ENTER) == "13"
    assert iterm.encode_keystroke(iterm.SPACE) == "32"
    assert iterm.encode_keystroke(iterm.ARROW_RIGHT) == "27,91,67"


def test_keys_argv_puts_the_tty_first_and_one_argument_per_keystroke():
    argv = iterm.keys_argv("/dev/ttys012", [iterm.ARROW_DOWN, iterm.ARROW_DOWN, iterm.ENTER])

    assert argv == ["/dev/ttys012", "27,91,66", "27,91,66", "13"]


def test_every_script_reads_its_arguments_from_argv():
    for script in (iterm.FIND_SESSION, iterm.WRITE_TEXT, iterm.SEND_KEYS, iterm.FOCUS_SESSION):
        assert "on run argv" in script
        assert "%s" not in script and "{" not in script


def test_the_keystroke_script_rebuilds_the_bytes_itself():
    """`character id` inside AppleScript, so argv stays printable."""
    assert "character id" in iterm.SEND_KEYS
    assert "\x1b" not in iterm.SEND_KEYS


def test_an_empty_keystroke_is_a_bug_not_a_no_op():
    try:
        iterm.encode_keystroke("")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_free_text_is_typed_without_a_newline_then_submitted_separately():
    """`write text`'s own newline does not submit a long prompt. A CR does."""
    runner = FakeOsascript("sent")

    iterm.write_text("/dev/ttys012", "hola", runner=runner)

    assert runner.calls == [
        ["osascript", "-", "/dev/ttys012", "hola"],
        ["osascript", "-", "/dev/ttys012", "13"],
    ]
    assert "newline no" in iterm.WRITE_TEXT


def test_text_without_a_newline_sends_no_enter():
    runner = FakeOsascript("sent")

    iterm.write_text("/dev/ttys012", "hola", newline=False, runner=runner)

    assert runner.calls == [["osascript", "-", "/dev/ttys012", "hola"]]


def test_empty_text_still_submits_when_asked_to():
    runner = FakeOsascript("sent")

    iterm.write_text("/dev/ttys012", "", runner=runner)

    assert runner.calls == [["osascript", "-", "/dev/ttys012", "13"]]


def test_delivering_to_a_dead_session_raises_rather_than_typing_into_nothing():
    runner = FakeOsascript("missing")

    for call in (
        lambda: iterm.write_text("/dev/ttys999", "hola", runner=runner),
        lambda: iterm.send_keys("/dev/ttys999", [iterm.ENTER], runner),
    ):
        try:
            call()
        except iterm.SessionGone:
            continue
        raise AssertionError("expected SessionGone")


def test_delivering_without_a_tty_never_runs_osascript():
    runner = FakeOsascript("sent")

    for call in (
        lambda: iterm.write_text("", "hola", runner=runner),
        lambda: iterm.send_keys("", [iterm.ENTER], runner),
    ):
        try:
            call()
        except iterm.SessionGone:
            pass
    assert runner.calls == []


def test_sending_no_keystrokes_is_a_no_op():
    runner = FakeOsascript("sent")

    iterm.send_keys("/dev/ttys012", [], runner)

    assert runner.calls == []


def test_only_the_focus_script_moves_anything():
    for script in (iterm.FIND_SESSION, iterm.WRITE_TEXT, iterm.SEND_KEYS):
        assert "activate" not in script
        assert "select" not in script
    assert "activate" in iterm.FOCUS_SESSION


def test_focus_reports_whether_it_found_the_window():
    assert iterm.focus("/dev/ttys012", FakeOsascript("focused")) is True
    assert iterm.focus("/dev/ttys999", FakeOsascript("missing")) is False
    assert iterm.focus("", FakeOsascript("focused")) is False


def test_scripting_status_reports_the_refusal_verbatim():
    """A denied Automation prompt is error -1743, and the user needs to see it."""
    runner = FakeOsascript(returncode=1)
    runner.stdout = ""

    ok, detail = iterm.scripting_status(runner)

    assert ok is False
    assert detail


def test_scripting_status_is_happy_when_iterm_answers():
    ok, detail = iterm.scripting_status(FakeOsascript("3"))

    assert ok is True
    assert "3" in detail


# --- the scripts have to compile, not just look right ----------------------
#
# `open_tab` shipped with `set startup to item 1 of argv`, which is not a
# variable assignment: `startup` is *startup items folder*, so the script
# failed to compile — "Access not allowed (-10003)" — every single time it was
# asked to open a window. Every test it had ran against a fake runner, which
# never sees the difference between a script that works and one AppleScript
# refuses to read.


def applescripts() -> dict:
    """Every script in the module, by name. New ones are covered for free."""
    return {
        name: value
        for name, value in vars(iterm).items()
        if name.isupper() and isinstance(value, str) and "on run argv" in value
    }


# Terms AppleScript or System Events already owns, that read like the obvious
# name for a local. `startup` is the one that shipped; the rest are the ones
# most likely to be reached for next.
RESERVED_TERMS = frozenset(
    """
    application character class color contents count data date day desktop disk
    document file folder front home hours id index item length line list menu
    minutes month name number page paragraph path point process properties
    quote record reference result return row script second seconds selection
    service size space startup string tab text time trash user value version
    volume weekday window word year
    """.split()
)

ASSIGNMENT = re.compile(r"^\s*set\s+([A-Za-z_][A-Za-z0-9_]*)\s+to\b", re.MULTILINE)


@pytest.mark.parametrize("name", sorted(applescripts()))
def test_no_script_names_a_local_after_a_term_applescript_owns(name):
    assert not set(ASSIGNMENT.findall(applescripts()[name])) & RESERVED_TERMS


def test_the_locals_of_a_script_are_the_ones_it_actually_names():
    """The guard above is only worth having if it reads the scripts correctly."""
    assert set(ASSIGNMENT.findall(iterm.OPEN_TAB)) == {"startupCmd", "followUp"}
    # The line that shipped, which is what the term list has to catch.
    assert ASSIGNMENT.findall("  set startup to item 1 of argv") == ["startup"]


@pytest.mark.skipif(
    shutil.which("osacompile") is None, reason="the AppleScript compiler is macOS-only"
)
@pytest.mark.parametrize("name", sorted(applescripts()))
def test_every_script_in_the_module_compiles(name):
    """The check no mocked runner can make. Compiling runs nothing."""
    completed = subprocess.run(
        ["osacompile", "-o", os.devnull, "-"],
        input=applescripts()[name],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.strip()


def test_open_tab_passes_the_command_and_the_text_as_arguments():
    runner = FakeOsascript("opened")

    assert iterm.open_tab("claude", "arreglá el build", runner) is True
    assert runner.calls == [["osascript", "-", "claude", "arreglá el build"]]


def test_open_tab_takes_neither_a_command_nor_a_text():
    """"abrí una ventana nueva" on its own: a tab, and nothing typed into it."""
    runner = FakeOsascript("opened")

    assert iterm.open_tab(runner=runner) is True
    assert runner.calls == [["osascript", "-", "", ""]]
