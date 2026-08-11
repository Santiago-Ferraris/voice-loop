-- item 1 = tty. Returns the foreground job on the first line and whatever is on
-- the screen after it — the two things that say whether a tab we just opened is
-- ready to be typed into, in one round trip instead of two.
--
-- `jobName` is an iTerm2 session variable read from the process group of the
-- tty, so it works in a tab that never sourced anything. Missing on an old
-- iTerm2, hence the `try`.
on run argv
  set targetTty to item 1 of argv
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if (tty of s) is targetTty then
            tell s
              set jobText to ""
              try
                set jobText to (variable named "jobName") as text
              end try
              return jobText & linefeed & (contents of it)
            end tell
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return ""
end run
