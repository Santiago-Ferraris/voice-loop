-- item 1 = what to run in the new tab (may be empty). A tab in the window you
-- already have, never a new window.
--
-- `startupCmd`, not `startup`: `startup` is *startup items folder* to
-- AppleScript, so `set startup to …` is an attempt to move a system folder —
-- "Access not allowed (-10003)", at compile time, every time. Trap #9. A mocked
-- runner cannot see that, which is why the tests compile every script for real.
--
-- The startup command keeps `write text`'s own newline — it is a shell command
-- line and a shell submits whatever it is handed. The dictated text is typed
-- afterwards over the tty this returns, by the same `newline no` + CR the rest
-- of this module uses. Returns tty then the job running before the command.
on run argv
  set startupCmd to item 1 of argv
  tell application "iTerm2"
    if (count of windows) is 0 then
      create window with default profile
    else
      tell current window to create tab with default profile
    end if
    tell current session of current tab of current window
      set jobText to ""
      try
        set jobText to (variable named "jobName") as text
      end try
      if startupCmd is not "" then write text startupCmd
      return (tty of it) & linefeed & jobText
    end tell
  end tell
end run
