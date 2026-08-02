from __future__ import annotations

import subprocess

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
