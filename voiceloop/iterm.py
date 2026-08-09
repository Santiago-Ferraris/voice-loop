"""Addressing an iTerm2 session by its tty, and typing into it.

The tty is the only stable handle we have on "the window this Claude session
runs in" — it survives tab reordering, renaming and window moves, and the hook
already resolves it by walking the process tree.

Every script is run as `osascript - <args>` against an `on run argv` handler,
so the tty, the text to deliver and the keystrokes travel as **arguments**.
Nothing is ever interpolated into the script source: an AppleScript built by
string concatenation is an injection waiting to happen, and here the "user
input" is whatever a speech recognizer just heard.

Keystrokes go one step further. Rather than putting raw control bytes in argv,
each keystroke is passed as a comma-separated list of decimal code points
(`27,91,66` for ESC `[` `B`) and rebuilt inside AppleScript with `character
id`. Only digits and commas ever cross the boundary.

Three mechanics, all verified against Claude Code 2.1.220 in a fullscreen TUI:

* **Free text** goes in with `newline no`, and the Enter is a *separate*
  keystroke. `write text`'s own trailing newline submits a short prompt but not
  a long one — a 150-character reply lands in the input box and just sits
  there. A separate CR submits both.
* **Menus** ignore typed text entirely; the selector is driven with arrow keys.
  The cursor starts on option 1, so option N takes N-1 `ESC [ B` then CR.
* **Focus** is never touched by either. `focus()` exists for "mostrame" and is
  the only function here that moves anything.
"""

from __future__ import annotations

import subprocess
from typing import Callable, Sequence

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

# Keystrokes, as the raw sequences a terminal sends.
ARROW_UP = "\x1b[A"
ARROW_DOWN = "\x1b[B"
ARROW_RIGHT = "\x1b[C"
ARROW_LEFT = "\x1b[D"
ENTER = "\r"
SPACE = " "
ESCAPE = "\x1b"

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

# Cheapest possible scripting call: it either answers, or macOS refuses and
# says why. Used by the permission preflight, never in the delivery path.
PING = """
on run argv
  tell application "iTerm2"
    return (count of windows) as text
  end tell
end run
"""

# item 1 = tty, item 2 = the text. Always `newline no`; Enter is a keystroke.
WRITE_TEXT = """
on run argv
  set targetTty to item 1 of argv
  set body to item 2 of argv
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if (tty of s) is targetTty then
            tell s to write text body newline no
            return "sent"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "missing"
end run
"""

# item 1 = tty, items 2..n = one keystroke each, as "27,91,66".
#
# The delay is not politeness. Sent back to back, the whole sequence lands in
# the TUI as a single stdin read, and a plain character sitting in front of an
# escape sequence in that read is swallowed: "space, down, down, space, right,
# enter" submitted a multi-select menu with nothing ticked, because both spaces
# were eaten while every arrow survived. One keystroke per read fixes it.
SEND_KEYS = """
on run argv
  set targetTty to item 1 of argv
  set chunks to items 2 thru -1 of argv
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if (tty of s) is targetTty then
            set first_one to true
            repeat with chunk in chunks
              if not first_one then delay 0.05
              set first_one to false
              set seq to my decode(chunk as text)
              tell s to write text seq newline no
            end repeat
            return "sent"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "missing"
end run

on decode(spec)
  set out to ""
  set saved to AppleScript's text item delimiters
  set AppleScript's text item delimiters to ","
  set parts to text items of spec
  set AppleScript's text item delimiters to saved
  repeat with p in parts
    set out to out & (character id ((p as text) as integer))
  end repeat
  return out
end decode
"""

# The only script that moves anything. "mostrame" is the only caller.
FOCUS_SESSION = """
on run argv
  set targetTty to item 1 of argv
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if (tty of s) is targetTty then
            select w
            tell w to select t
            tell t to select s
            activate
            return "focused"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "missing"
end run
"""


# item 1 = what to run in the new tab (may be empty), item 2 = what to type
# after it (may be empty). A tab in the window you already have, never a new
# window: a new window is somewhere else on the desktop, which is exactly what
# you were trying not to go looking for.
#
# `startupCmd`, not `startup`: `startup` is *startup items folder* to
# AppleScript, so `set startup to …` is not a variable assignment, it is an
# attempt to move a system folder — "Access not allowed (-10003)", at compile
# time, every time. The script never ran once. A mocked runner cannot see that,
# which is why `test_iterm` compiles every script in this module for real.
OPEN_TAB = """
on run argv
  set startupCmd to item 1 of argv
  set followUp to item 2 of argv
  tell application "iTerm2"
    if (count of windows) is 0 then
      create window with default profile
    else
      tell current window to create tab with default profile
    end if
    tell current session of current tab of current window
      if startupCmd is not "" then write text startupCmd
      if followUp is not "" then write text followUp
    end tell
  end tell
  return "opened"
end run
"""


class AppleScriptError(RuntimeError):
    pass


class SessionGone(AppleScriptError):
    """The tty no longer belongs to any iTerm2 session — the window is closed."""


def _default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        list(argv), capture_output=True, text=True, timeout=10, check=False
    )


def osascript_argv(script: str, args: Sequence[str]) -> list[str]:
    """`osascript - arg…` — the script on stdin, everything else as argv."""
    return ["osascript", "-", *args]


def encode_keystroke(sequence: str) -> str:
    """`ESC [ B` -> `"27,91,66"`. Digits and commas are all that reach argv."""
    if not sequence:
        raise ValueError("empty keystroke")
    return ",".join(str(ord(character)) for character in sequence)


def keys_argv(tty: str, keystrokes: Sequence[str]) -> list[str]:
    return [tty, *(encode_keystroke(stroke) for stroke in keystrokes)]


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


def scripting_status(runner: Runner | None = None) -> tuple[bool, str]:
    """(can we drive iTerm2, why not). Automation denial is error -1743."""
    try:
        return True, f"iTerm2 reachable, {run_script(PING, [], runner)} window(s)"
    except AppleScriptError as exc:
        return False, str(exc)


def _require_hit(result: str, tty: str) -> None:
    if result != "sent":
        raise SessionGone(f"no iTerm2 session on {tty}")


def send_keys(tty: str, keystrokes: Sequence[str], runner: Runner | None = None) -> None:
    """Deliver keystrokes, one `write text … newline no` each, in order."""
    if not tty:
        raise SessionGone("no tty")
    strokes = list(keystrokes)
    if not strokes:
        return
    _require_hit(run_script(SEND_KEYS, keys_argv(tty, strokes), runner), tty)


def write_text(tty: str, text: str, *, newline: bool = True, runner: Runner | None = None) -> None:
    """Type `text` into the session, and submit it unless `newline` is off.

    The Enter is a separate keystroke on purpose — see the module docstring.
    """
    if not tty:
        raise SessionGone("no tty")
    if text:
        _require_hit(run_script(WRITE_TEXT, [tty, text], runner), tty)
    if newline:
        send_keys(tty, [ENTER], runner)


def open_tab(command: str = "", text: str = "", runner: Runner | None = None) -> bool:
    """A new tab in the window you already have, optionally running something.

    `command` is what starts there (`windows.new_tab_command` — normally the
    thing that launches Claude); `text` is what gets typed into it afterwards,
    which is the "y hacé X" half of "abrí una ventana nueva y hacé X".
    """
    return run_script(OPEN_TAB, [command or "", text or ""], runner) == "opened"


def focus(tty: str, runner: Runner | None = None) -> bool:
    """Bring the session's tab to the front. The only thing here that steals focus."""
    if not tty:
        return False
    try:
        return run_script(FOCUS_SESSION, [tty], runner) == "focused"
    except AppleScriptError:
        return False
