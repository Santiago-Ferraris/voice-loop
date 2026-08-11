-- Cheapest possible scripting call: it either answers, or macOS refuses and
-- says why. Used by the permission Doctor, never in the delivery path.
-- Automation denial surfaces as error -1743.
on run argv
  tell application "iTerm2"
    return (count of windows) as text
  end tell
end run
