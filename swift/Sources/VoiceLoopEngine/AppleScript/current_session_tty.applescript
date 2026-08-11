-- The tty of the session the user is looking at right now. Used to tell whether
-- the focused iTerm2 session is a Claude window before ⌥N injects into it.
on run argv
  tell application "iTerm2"
    tell current session of current window
      return tty
    end tell
  end tell
end run
