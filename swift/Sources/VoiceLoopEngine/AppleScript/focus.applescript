-- item 1 = tty. The only script that moves anything. "mostrame" is the only
-- caller.
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
