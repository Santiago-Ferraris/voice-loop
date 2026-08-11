-- item 1 = tty. Walks every session of every tab of every window until the tty
-- matches. "found" or "missing".
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
