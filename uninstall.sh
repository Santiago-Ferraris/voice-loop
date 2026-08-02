#!/bin/sh
# uninstall.sh — undo install.sh.
#
# Removes the launchd agent and the hooks from ~/.claude/settings.json (with a
# backup, same as install). Your state directory, config and env file are left
# alone unless you ask: `--purge-state` drops the queue and logs, and the env
# file is never touched, because it is yours and it holds a key.
set -eu

REPO=$(cd "$(dirname "$0")" && pwd -P)

PURGE_STATE=0
DO_HOOKS=1
for arg in "$@"; do
  case "$arg" in
    --purge-state) PURGE_STATE=1 ;;
    --no-hooks) DO_HOOKS=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "uninstall.sh: unknown option $arg" >&2; exit 2 ;;
  esac
done

SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
CONFIG_DIR="${VOICE_LOOP_CONFIG_DIR:-$HOME/.config/voice-loop}"
PLIST_LABEL=com.voiceloop.daemon
PLIST="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"

if command -v launchctl >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true
fi
rm -f "$PLIST"
echo "agent: removed"

if [ -x "$REPO/.venv/bin/python" ]; then
  PY="$REPO/.venv/bin/python"
else
  PY=python3
fi

if [ "$DO_HOOKS" -eq 1 ]; then
  "$PY" "$REPO/hooks/merge-hooks.py" --settings "$SETTINGS" --remove
else
  echo "hooks: skipped"
fi

if [ "$PURGE_STATE" -eq 1 ]; then
  STATE_DIR=""
  if [ -r "$CONFIG_DIR/state_dir" ]; then
    STATE_DIR=$(cat "$CONFIG_DIR/state_dir")
  fi
  if [ -n "$STATE_DIR" ] && [ -d "$STATE_DIR" ]; then
    rm -rf "$STATE_DIR"
    echo "state: removed $STATE_DIR"
  fi
fi
rm -f "$CONFIG_DIR/state_dir"

echo
echo "voice-loop uninstalled. The env file was left in place."
echo "Restart any running Claude session to drop the hooks from it."
