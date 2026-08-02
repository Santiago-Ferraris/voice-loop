"""Addressing an iTerm2 session by its tty.

The tty is the only stable handle we have on "the window this Claude session
runs in" — it survives tab reordering, renaming and window moves, and the hook
already resolves it by walking the process tree.

Every script is run as `osascript - <args>` against an `on run argv` handler,
so the tty (and, in phase 2, the text to deliver) travels as an **argument**.
It is never interpolated into the script source: an AppleScript built by string
concatenation is an injection waiting to happen, and here the "user input" is
whatever a model just typed.

Phase 1 only needs to look a session up. Delivery (`write_text` for free text,
arrow keys plus CR for menus) and focus-on-request land in phases 2 and 3 on
top of this same addressing.
"""

from __future__ import annotations

import subprocess
from typing import Callable, Sequence

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

# Walks every session of every tab of every window until the tty matches.
FIND_SESSION = """
on run argv
  set targetTty to item 1 of argv
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if (tty of s) is targetTty then return "found"
        end repeat
      end repeat
    end repeat
  end tell
  return "missing"
end run
"""


class AppleScriptError(RuntimeError):
    pass


def _default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        list(argv), capture_output=True, text=True, timeout=10, check=False
    )


def osascript_argv(script: str, args: Sequence[str]) -> list[str]:
    """`osascript - arg…` — the script on stdin, everything else as argv."""
    return ["osascript", "-", *args]


def run_script(script: str, args: Sequence[str], runner: Runner | None = None) -> str:
    argv = osascript_argv(script, args)
    execute = runner or _default_runner
    try:
        if execute is _default_runner:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                argv,
                input=script,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        else:
            completed = execute(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AppleScriptError(str(exc)) from exc
    if completed.returncode != 0:
        raise AppleScriptError((completed.stderr or "").strip() or "osascript failed")
    return (completed.stdout or "").strip()


def session_exists(tty: str, runner: Runner | None = None) -> bool:
    if not tty:
        return False
    try:
        return run_script(FIND_SESSION, [tty], runner) == "found"
    except AppleScriptError:
        return False
