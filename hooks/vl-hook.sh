#!/bin/sh
# vl-hook.sh — the one hook entrypoint voice-loop registers with Claude Code.
#
#   vl-hook.sh stop | menu | notification | activity | bash
#
# It resolves where the daemon keeps its state and which tty the session runs
# in, then hands both to vl-spool.py, which reads the hook JSON from stdin and
# drops a single file in the spool. No database, no network, no lock.
#
# It exits in milliseconds and **always exits 0** — a broken voice-loop must
# never be able to disturb a Claude session.
set -u

KIND="${1:-}"
[ -n "$KIND" ] || exit 0

HERE=$(cd "$(dirname "$0")" 2>/dev/null && pwd -P) || exit 0

# Where the daemon keeps its state: env override, then the pointer file written
# by install.sh, then the documented default.
STATE_DIR="${VOICE_LOOP_STATE_DIR:-}"
if [ -z "$STATE_DIR" ]; then
  POINTER="${VOICE_LOOP_CONFIG_DIR:-$HOME/.config/voice-loop}/state_dir"
  if [ -r "$POINTER" ]; then
    STATE_DIR=$(cat "$POINTER" 2>/dev/null) || STATE_DIR=""
  fi
fi
[ -n "$STATE_DIR" ] || STATE_DIR="$HOME/.local/state/voice-loop"

# The hook subprocess has no controlling tty of its own — walk up the process
# tree until we hit the terminal the session runs in.
TTY_DEV=""
pid=$$
depth=0
while [ "$depth" -lt 12 ]; do
  depth=$((depth + 1))
  [ "$pid" -gt 1 ] 2>/dev/null || break
  # shellcheck disable=SC2046  # deliberate word splitting of "<ppid> <tty>"
  set -- $(ps -o ppid=,tty= -p "$pid" 2>/dev/null)
  [ "$#" -ge 2 ] || break
  case "$2" in
    ttys*) TTY_DEV="/dev/$2"; break ;;
  esac
  pid="$1"
done

# Prefer the system interpreter: a version-manager shim (pyenv, asdf) costs
# ~600 ms of startup, and this runs on every prompt the user submits.
PY="${VOICE_LOOP_PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x /usr/bin/python3 ]; then
    PY=/usr/bin/python3
  else
    PY=$(command -v python3 2>/dev/null) || PY=""
  fi
fi
[ -n "$PY" ] || exit 0

# stdin (the hook JSON) passes straight through to the spool writer.
"$PY" "$HERE/vl-spool.py" "$KIND" "$STATE_DIR" "$TTY_DEV" 2>/dev/null

exit 0
