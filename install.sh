#!/bin/sh
# install.sh — set voice-loop up on this Mac.
#
#   1. create .venv and install the package
#   2. create the state directory and the pointer the hooks read
#   3. create the env file for your API key (only if it does not exist)
#   4. render and bootstrap the launchd agent
#   5. merge the hooks into ~/.claude/settings.json (backed up, idempotent)
#
# Safe to re-run: every step is idempotent, and the two files that could
# contain something you care about — the env file and settings.json — are
# never overwritten blindly.
#
# Flags:
#   --no-venv      skip the virtualenv (use the system python3)
#   --no-launchd   skip the launchd agent
#   --no-hooks     skip editing ~/.claude/settings.json
set -eu

REPO=$(cd "$(dirname "$0")" && pwd -P)

DO_VENV=1
DO_LAUNCHD=1
DO_HOOKS=1
for arg in "$@"; do
  case "$arg" in
    --no-venv) DO_VENV=0 ;;
    --no-launchd) DO_LAUNCHD=0 ;;
    --no-hooks) DO_HOOKS=0 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "install.sh: unknown option $arg" >&2; exit 2 ;;
  esac
done

SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
CONFIG_DIR="${VOICE_LOOP_CONFIG_DIR:-$HOME/.config/voice-loop}"
ENV_FILE="${VOICE_LOOP_ENV_FILE:-$CONFIG_DIR/env}"
PLIST_LABEL=com.voiceloop.daemon
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/$PLIST_LABEL.plist"

# --- 1. virtualenv --------------------------------------------------------

PY=python3
if [ "$DO_VENV" -eq 1 ]; then
  if [ ! -x "$REPO/.venv/bin/python" ]; then
    echo "venv: creating $REPO/.venv"
    python3 -m venv "$REPO/.venv"
  fi
  PY="$REPO/.venv/bin/python"
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet -e "$REPO"
  echo "venv: ready"
else
  PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
  export PYTHONPATH
fi

# --- 2. state directory ---------------------------------------------------

STATE_DIR=$("$PY" -c 'import sys; from voiceloop.config import load; print(load(repo_root=sys.argv[1]).state_dir)' "$REPO")
mkdir -p "$STATE_DIR/spool/bad" "$STATE_DIR/logs"
mkdir -p "$CONFIG_DIR"
printf '%s' "$STATE_DIR" > "$CONFIG_DIR/state_dir"
echo "state: $STATE_DIR"

# --- 3. env file (never overwritten) --------------------------------------

if [ -e "$ENV_FILE" ]; then
  echo "env:   $ENV_FILE already exists — left untouched"
else
  umask 077
  cat > "$ENV_FILE" <<'ENVEOF'
# voice-loop secrets. Sourced by bin/voice-loopd, never by anything in the repo.
# Keep this file outside the repo and chmod 600.
#
# OPENAI_API_KEY=sk-...
# DEEPGRAM_API_KEY=...
ENVEOF
  chmod 600 "$ENV_FILE"
  echo "env:   created $ENV_FILE (add your OPENAI_API_KEY)"
fi

# --- 4. launchd -----------------------------------------------------------

if [ "$DO_LAUNCHD" -eq 1 ]; then
  mkdir -p "$PLIST_DIR"
  "$PY" - "$REPO/launchd/$PLIST_LABEL.plist.template" "$PLIST" "$REPO" "$HOME" "$STATE_DIR" <<'PYEOF'
import sys
template, target, repo, home, state_dir = sys.argv[1:6]
with open(template, encoding="utf-8") as fh:
    body = fh.read()
body = body.replace("__REPO__", repo).replace("__HOME__", home).replace("__STATE_DIR__", state_dir)
with open(target, "w", encoding="utf-8") as fh:
    fh.write(body)
PYEOF
  chmod 644 "$PLIST"
  launchctl bootout "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  launchctl enable "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true
  echo "agent: bootstrapped $PLIST_LABEL"
else
  echo "agent: skipped"
fi

# --- 5. hooks -------------------------------------------------------------

chmod +x "$REPO/hooks/vl-hook.sh" "$REPO/bin/voice-loopd" "$REPO/bin/voice-loopctl"
if [ "$DO_HOOKS" -eq 1 ]; then
  "$PY" "$REPO/hooks/merge-hooks.py" --settings "$SETTINGS" --hook-script "$REPO/hooks/vl-hook.sh"
else
  echo "hooks: skipped"
fi

cat <<EOF

voice-loop installed.

  1. put your key in $ENV_FILE   (OPENAI_API_KEY=…)
  2. $REPO/bin/voice-loopctl restart
  3. open a NEW Claude session — running ones do not pick up new hooks
  4. $REPO/bin/voice-loopctl status

Add $REPO/bin to your PATH to drop the full path.
EOF
