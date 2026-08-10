from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time

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
    assert set(ASSIGNMENT.findall(iterm.OPEN_TAB)) == {"startupCmd", "jobText"}
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


# --- opening a tab and typing into it --------------------------------------
#
# The second bug this file exists for. `open_tab` handed the follow-up text to
# `write text` with its own newline, which is precisely the thing the rest of
# the module knows does not submit a long prompt: a 150-character sentence was
# typed into a brand new Claude and left sitting in the input box, unsent, and
# every test of it passed because a fake runner cannot tell a typed prompt from
# a submitted one.


class FakeTab:
    """A new tab, as a runner sees it — and a runner only sees argv.

    So the calls are told apart by shape: the first one is the open, one
    argument is a state poll, two where the second is digits is a keystroke,
    and anything else is a write. The screen grows as text is typed into it,
    which is what makes the "did it show up" wait testable at all.
    """

    KEYSTROKE = re.compile(r"[\d,]+\Z")

    def __init__(self, *, tty="/dev/ttys077", shell="zsh", job="node", starts_after=0, echoes=True):
        self.tty, self.shell, self.job = tty, shell, job
        self.starts_after, self.echoes = starts_after, echoes
        self.calls: list[list[str]] = []
        self.typed: list[str] = []
        self.keys: list[str] = []
        self.screen = ""
        self.polls = 0
        self.polls_when_typed: int | None = None
        self._opened = False

    def __call__(self, argv):
        self.calls.append(list(argv))
        args = list(argv[2:])
        if not self._opened:
            self._opened = True
            return self._done(f"{self.tty}\n{self.shell}")
        if len(args) == 1:
            self.polls += 1
            running = self.job if self.polls > self.starts_after else self.shell
            return self._done(f"{running}\n{self.screen}")
        if self.KEYSTROKE.match(args[1]):
            self.keys.append(args[1])
            return self._done("sent")
        self.typed.append(args[1])
        self.polls_when_typed = self.polls
        if self.echoes:
            self.screen += args[1]
        return self._done("sent")

    def _done(self, stdout):
        return subprocess.CompletedProcess([], 0, stdout, "")


def never_sleep(_seconds):
    return None


LONG = (
    "modifique el alias que tengo de claude e inicie las sesiones con opus 4.8 "
    "por default no saques ninguno de los parametros que tiene"
)


def test_the_text_is_typed_without_a_newline_and_submitted_with_a_separate_cr():
    """The bug: `write text followUp` left a 150-character prompt unsent."""
    tab = FakeTab()

    assert iterm.open_tab("claude", LONG, tab, sleep=never_sleep) is True

    assert tab.typed == [LONG]
    assert tab.keys == [str(ord(iterm.ENTER))]
    assert "followUp" not in iterm.OPEN_TAB


def test_the_command_travels_as_the_only_argument_of_the_open():
    tab = FakeTab()

    iterm.open_tab("claude", LONG, tab, sleep=never_sleep)

    assert tab.calls[0] == ["osascript", "-", "claude"]
    assert ["osascript", "-", tab.tty, LONG] in tab.calls


def test_the_startup_command_keeps_the_newline_the_text_does_not():
    """It is a shell command line, and a shell submits what it is handed."""
    assert "write text startupCmd" in iterm.OPEN_TAB
    assert "newline no" not in iterm.OPEN_TAB


def test_nothing_is_typed_until_the_command_has_taken_the_shell_over():
    """Typed too early it goes to a shell, or into a TUI still taking over."""
    tab = FakeTab(starts_after=3)

    iterm.open_tab("claude", LONG, tab, sleep=never_sleep)

    assert tab.polls_when_typed == 4
    assert tab.keys == [str(ord(iterm.ENTER))]


def test_a_command_that_never_starts_leaves_the_text_typed_but_unsent():
    """A dictated sentence is never handed to a shell to run."""
    tab = FakeTab(job="zsh")

    assert iterm.open_tab("claude", LONG, tab, launch_timeout=0, sleep=never_sleep) is True

    assert tab.typed == [LONG]
    assert tab.keys == []


def test_text_that_never_reaches_the_screen_is_not_submitted():
    """Half a sentence submitted is worse than a whole one sitting there."""
    tab = FakeTab(echoes=False)

    assert iterm.open_tab("claude", LONG, tab, echo_timeout=0, sleep=never_sleep) is True

    assert tab.typed == [LONG]
    assert tab.keys == []


def test_the_wait_ends_the_moment_the_text_shows_up():
    tab = FakeTab()

    iterm.open_tab("claude", LONG, tab, sleep=never_sleep)

    # One poll to see the command start, one to see the text land.
    assert tab.polls == 2


def test_a_tab_with_nothing_to_type_is_opened_and_left_alone():
    """"abrí una ventana nueva" on its own: a tab, and nothing typed into it."""
    tab = FakeTab()

    assert iterm.open_tab(runner=tab, sleep=never_sleep) is True
    assert tab.calls == [["osascript", "-", ""]]

    blank = FakeTab()
    assert iterm.open_tab("claude", "   ", blank, sleep=never_sleep) is True
    assert blank.typed == []


def test_a_tab_with_no_command_types_into_whatever_is_there():
    """Nothing was launched, so there is nothing to wait for — only the echo."""
    tab = FakeTab()

    assert iterm.open_tab("", "hola", tab, sleep=never_sleep) is True

    assert tab.typed == ["hola"]
    assert tab.keys == [str(ord(iterm.ENTER))]


def test_an_iterm_that_names_no_tty_is_still_a_tab():
    """Nothing to type into, but the window did open. Say so and stop."""
    assert iterm.open_tab("claude", LONG, FakeOsascript(""), sleep=never_sleep) is False


def test_session_state_splits_the_job_from_the_screen():
    job, screen = iterm.session_state("/dev/ttys012", FakeOsascript("node\n❯ hola"))

    assert (job, screen) == ("node", "❯ hola")


def test_session_state_of_a_window_that_is_gone_is_empty():
    assert iterm.session_state("/dev/ttys999", FakeOsascript(returncode=1)) == ("", "")
    assert iterm.session_state("", FakeOsascript("node\nhola")) == ("", "")


def test_reading_the_state_of_a_session_never_moves_focus():
    assert "activate" not in iterm.SESSION_STATE
    assert "select" not in iterm.SESSION_STATE


# --- the check no mocked runner can make -----------------------------------

LIVE_ITERM = (
    sys.platform == "darwin"
    and shutil.which("osascript") is not None
    and iterm.scripting_status()[0]
)


# A stand-in for the TUI, because the TUI is what the fakes cannot be. Three
# things about it are not decoration — they are the three the bug turned on:
# it reads the terminal **raw**, so it is submitted by CR and not by the LF
# `write text` appends; it **paints what it is typed**, which is the only
# reason "is it on the screen yet" is answerable at all; and it writes a file
# only once it has a whole line, so the file existing *is* the assertion.
TUI_ENOUGH = """
import os, pathlib, signal, sys, tty

signal.alarm(30)
fd = sys.stdin.fileno()
tty.setraw(fd)
typed = b""
while b"\\r" not in typed:
    chunk = os.read(fd, 1024)
    if not chunk:
        break
    os.write(1, chunk)
    typed += chunk
pathlib.Path(sys.argv[1]).write_text(typed.split(b"\\r")[0].decode())
"""


@pytest.mark.skipif(not LIVE_ITERM, reason="needs macOS, iTerm2 and Automation permission")
def test_a_real_tab_receives_the_text_submitted_not_left_in_the_input(tmp_path):
    """A real tab, a real program, and a file that only exists if Enter landed.

    Every test above this one runs against a fake, and a fake said "opened" for
    the whole life of the bug. This one opens an actual tab and types into an
    actual program that behaves like the thing that broke: the file appears
    only if a CR arrived after the text, which is exactly what the shipped
    `write text followUp` never sent.

    `exec` is what cleans up: the reader replaces the shell, so when it returns
    the session has nothing left to run and iTerm2 closes the tab. The alarm is
    the same thing for the case where nothing is ever typed.
    """
    sink = tmp_path / "sink.py"
    sink.write_text(TUI_ENOUGH)
    landed = tmp_path / "landed.txt"
    command = " ".join(
        ["exec", shlex.quote(sys.executable), shlex.quote(str(sink)), shlex.quote(str(landed))]
    )

    assert iterm.open_tab(command, LONG) is True

    deadline = time.monotonic() + 20
    while not landed.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert landed.exists(), "the tab never got a submitted line — the text sat in the input"
    assert landed.read_text().strip() == LONG
