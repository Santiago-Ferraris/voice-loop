-- item 1 = tty, item 2 = the text. Always `newline no`; the Enter is a
-- separate keystroke (see send_keys). `write text`'s own trailing newline
-- submits a short prompt but not a long one, so a 150-char reply would land in
-- the input box and just sit there. Trap #10.
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
