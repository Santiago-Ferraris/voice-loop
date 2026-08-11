-- item 1 = tty, items 2..n = one keystroke each, as "27,91,66".
--
-- The delay is not politeness. Sent back to back, the whole sequence lands in
-- the TUI as a single stdin read, and a plain character in front of an escape
-- sequence in that read is swallowed. One keystroke per read fixes it. Trap #10.
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
