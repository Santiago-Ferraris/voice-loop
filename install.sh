#!/bin/sh
# install.sh — set voice-loop up on this Mac.
#
#   1. install the runtime under ~/.local/share/voice-loop (venv + wrappers)
#   2. create the state directory and the pointer the hooks read
#   3. create the env file for your API key (only if it does not exist)
#   4. render the launchd agent, load it, and prove the daemon came up
#   5. merge the hooks into ~/.claude/settings.json (backed up, idempotent)
#   6. run `doctor` once, from your terminal, so macOS asks for microphone and
#      Automation while you are sitting here to say yes
#
# Why the runtime is a copy and not the clone: macOS TCC keeps LaunchAgents out
# of ~/Documents, ~/Desktop and ~/Downloads. A clone in one of them cannot be
# *executed* by launchd at all — the agent dies with exit 126 and "Operation
# not permitted" before Python ever starts. So nothing launchd touches is
# allowed to live in the clone. The clone stays for development, and
# `voice-loopctl` warns you from there when the installed copy has fallen
# behind it.
#
# Safe to re-run, and you must after a `git pull` — that is what updates the
# runtime. Every step is idempotent, and the two files that could hold
# something you care about — the env file and settings.json — are never
# overwritten blindly.
#
# Flags:
#   --no-venv      skip the virtualenv (copy the package, use the system python3)
#   --no-launchd   write the plist but do not load it (nothing is verified)
#   --no-hooks     skip editing ~/.claude/settings.json
#   --no-doctor    skip the permission probe at the end
#
# Environment:
#   VOICE_LOOP_RUNTIME_DIR      where the runtime goes
#   VOICE_LOOP_PIP_INDEX_URL    package index (defaults to PyPI, see below)
#   VOICE_LOOP_STARTUP_TIMEOUT  seconds to wait for the daemon (default 25)
set -eu

REPO=$(cd "$(dirname "$0")" && pwd -P)

DO_VENV=1
DO_LAUNCHD=1
DO_HOOKS=1
DO_DOCTOR=1
for arg in "$@"; do
  case "$arg" in
    --no-venv) DO_VENV=0 ;;
    --no-launchd) DO_LAUNCHD=0 ;;
    --no-hooks) DO_HOOKS=0 ;;
    --no-doctor) DO_DOCTOR=0 ;;
    -h|--help) sed -n '2,35p' "$0"; exit 0 ;;
    *) echo "install.sh: unknown option $arg" >&2; exit 2 ;;
  esac
done

SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
CONFIG_DIR="${VOICE_LOOP_CONFIG_DIR:-$HOME/.config/voice-loop}"
ENV_FILE="${VOICE_LOOP_ENV_FILE:-$CONFIG_DIR/env}"
RUNTIME="${VOICE_LOOP_RUNTIME_DIR:-$HOME/.local/share/voice-loop}"
LAUNCHCTL="${VOICE_LOOP_LAUNCHCTL:-launchctl}"
STARTUP_TIMEOUT="${VOICE_LOOP_STARTUP_TIMEOUT:-25}"
PLIST_LABEL=com.voiceloop.daemon
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/$PLIST_LABEL.plist"

if [ "$REPO" = "$RUNTIME" ]; then
  echo "install.sh: the runtime directory cannot be the clone ($RUNTIME)" >&2
  exit 2
fi

# Your pip may be pointed at a private index (CodeArtifact and friends) whose
# token expired months ago — that is a 401 on `pip install` and has nothing to
# do with voice-loop. Drop every PIP_* variable and the pip config file, and
# install from PyPI. Set VOICE_LOOP_PIP_INDEX_URL if you really do need
# another index.
PIP_INDEX="${VOICE_LOOP_PIP_INDEX_URL:-https://pypi.org/simple}"
for _pip_var in $(env | sed -n 's/^\(PIP_[A-Za-z0-9_]*\)=.*/\1/p'); do
  unset "$_pip_var"
done
unset _pip_var

vl_pip() {
  PIP_CONFIG_FILE=/dev/null \
  PIP_INDEX_URL="$PIP_INDEX" \
  PIP_DISABLE_PIP_VERSION_CHECK=1 \
    "$PY" -m pip "$@"
}

# --- 1. runtime -----------------------------------------------------------

mkdir -p "$RUNTIME/bin"
cp "$REPO/bin/voice-loopd" "$REPO/bin/voice-loopctl" "$RUNTIME/bin/"
chmod 755 "$RUNTIME/bin/voice-loopd" "$RUNTIME/bin/voice-loopctl"
cp "$REPO/config.example.yml" "$RUNTIME/config.example.yml"
rm -f "$RUNTIME/config.local.yml" "$RUNTIME/config.local.yaml"
for name in config.local.yml config.local.yaml; do
  if [ -f "$REPO/$name" ]; then
    cp "$REPO/$name" "$RUNTIME/$name"
  fi
done
rm -rf "$RUNTIME/voiceloop"

PY=python3
MODE=venv
if [ "$DO_VENV" -eq 1 ]; then
  if [ ! -x "$RUNTIME/.venv/bin/python" ]; then
    echo "venv:  creating $RUNTIME/.venv"
    python3 -m venv "$RUNTIME/.venv"
  fi
  PY="$RUNTIME/.venv/bin/python"
  vl_pip install --quiet --upgrade pip
  vl_pip install --quiet "$REPO"
  # The version number does not change while you develop, so a plain install
  # would leave the old code in place and you would debug a bug you already
  # fixed. --force-reinstall is what makes `git pull && ./install.sh` mean
  # something.
  vl_pip install --quiet --force-reinstall --no-deps "$REPO"
else
  MODE=no-venv
  # Same rule as the wrappers: the daemon cannot import from the clone, so the
  # package is copied too.
  cp -R "$REPO/voiceloop" "$RUNTIME/voiceloop"
  find "$RUNTIME/voiceloop" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  PYTHONPATH="$RUNTIME${PYTHONPATH:+:$PYTHONPATH}"
  export PYTHONPATH
fi
echo "runtime: $RUNTIME ($MODE)"

# --- 2. state directory ---------------------------------------------------

STATE_DIR=$("$PY" -c 'import sys; from voiceloop.config import load; print(load(repo_root=sys.argv[1]).state_dir)' "$RUNTIME")
mkdir -p "$STATE_DIR/spool/bad" "$STATE_DIR/logs"
mkdir -p "$CONFIG_DIR"
printf '%s' "$STATE_DIR" > "$CONFIG_DIR/state_dir"
SOCKET="$STATE_DIR/daemon.sock"
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

# Rendered even with --no-launchd: the file is inert until something loads it,
# and it is worth having on disk to look at. render-plist refuses outright if
# any path in it sits somewhere launchd would be denied.
mkdir -p "$PLIST_DIR"
"$PY" -m voiceloop.runtime render-plist \
  --template "$REPO/launchd/$PLIST_LABEL.plist.template" \
  --output "$PLIST" \
  --runtime "$RUNTIME" \
  --home "$HOME" \
  --state-dir "$STATE_DIR"
chmod 644 "$PLIST"

"$PY" -m voiceloop.runtime write-manifest \
  --runtime "$RUNTIME" --source "$REPO" --mode "$MODE" >/dev/null

startup_failed() {
  echo >&2
  echo "install.sh: the launchd agent did not come up within ${STARTUP_TIMEOUT}s." >&2
  echo "  label:  $PLIST_LABEL" >&2
  echo "  plist:  $PLIST" >&2
  echo "  socket: $SOCKET" >&2
  for logfile in "$STATE_DIR/logs/stderr.log" "$STATE_DIR/logs/stdout.log" "$STATE_DIR/logs/daemon.log"; do
    if [ -s "$logfile" ]; then
      echo >&2
      echo "--- tail $logfile ---" >&2
      tail -n 20 "$logfile" >&2
    fi
  done
  echo >&2
  echo "--- launchctl print gui/$(id -u)/$PLIST_LABEL ---" >&2
  "$LAUNCHCTL" print "gui/$(id -u)/$PLIST_LABEL" 2>&1 | sed -n '1,25p' >&2 || true
  exit 1
}

if [ "$DO_LAUNCHD" -eq 1 ]; then
  "$LAUNCHCTL" bootout "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true

  # A daemon you started by hand (`nohup bin/voice-loopd &`) still owns the
  # socket. launchd's copy would exit "already running" straight into a restart
  # loop while `status` kept answering — an install that looks fine and is not.
  if [ -S "$SOCKET" ]; then
    echo "agent: something outside launchd owns $SOCKET — stopping it"
    "$PY" -m voiceloop.runtime stop-daemon --socket "$SOCKET" || true
  fi

  "$LAUNCHCTL" bootstrap "gui/$(id -u)" "$PLIST"
  "$LAUNCHCTL" enable "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true

  DAEMON_PID=$("$PY" -m voiceloop.runtime wait-for-daemon \
    --socket "$SOCKET" --timeout "$STARTUP_TIMEOUT") || startup_failed
  LAUNCHD_PID=$("$LAUNCHCTL" list "$PLIST_LABEL" 2>/dev/null |
    sed -n 's/.*"PID" *= *\([0-9][0-9]*\);.*/\1/p' | head -1)
  if [ -n "$LAUNCHD_PID" ] && [ "$LAUNCHD_PID" != "$DAEMON_PID" ]; then
    echo >&2
    echo "install.sh: the daemon answering on $SOCKET is pid $DAEMON_PID, but launchd" >&2
    echo "  started pid $LAUNCHD_PID. A stray daemon is holding the socket — kill it" >&2
    echo "  (kill $DAEMON_PID) and re-run this script." >&2
    exit 1
  fi
  echo "agent: $PLIST_LABEL up (pid $DAEMON_PID)"
else
  echo "agent: $PLIST written, not loaded (--no-launchd)"
fi

# --- 5. hooks -------------------------------------------------------------

chmod +x "$REPO/hooks/vl-hook.sh" "$REPO/bin/voice-loopd" "$REPO/bin/voice-loopctl"
if [ "$DO_HOOKS" -eq 1 ]; then
  "$PY" "$REPO/hooks/merge-hooks.py" --settings "$SETTINGS" --hook-script "$REPO/hooks/vl-hook.sh"
else
  echo "hooks: skipped"
fi

# --- 6. permissions -------------------------------------------------------
#
# macOS grants microphone and Automation access to the *responsible* process,
# and for a LaunchAgent that is launchd — which inherits nothing from the
# terminal you already granted, and may never manage to raise a consent dialog
# at all. Until the grant exists, every mic open parks on an invisible prompt
# and the hotkey looks dead.
#
# So the prompts are raised here, while you are sitting in front of the
# machine: `doctor` records one second of audio and talks to iTerm2, which is
# what makes macOS ask. Only from a terminal — with no tty there is nobody to
# answer, and the probe would just hang for its timeout.

if [ "$DO_DOCTOR" -eq 1 ] && [ -t 0 ]; then
  echo
  echo "doctor: probing permissions — say yes to any macOS dialog that appears"
  "$RUNTIME/bin/voice-loopctl" doctor || true
else
  echo "doctor: not run — do it once from a terminal: $REPO/bin/voice-loopctl doctor"
fi

cat <<EOF

voice-loop installed.

  1. put your key in $ENV_FILE   (OPENAI_API_KEY=…)
  2. $REPO/bin/voice-loopctl restart
  3. open a NEW Claude session — running ones do not pick up new hooks
  4. $REPO/bin/voice-loopctl status
  5. $REPO/bin/voice-loopctl doctor — and answer the microphone dialog.
     Until you do, every mic open waits on a prompt nobody sees, and the
     hotkey looks dead. If the daemon's column still fails afterwards, tick
     its python in System Settings → Privacy & Security → Microphone.

The daemon runs from $RUNTIME, a copy of this clone.
Re-run ./install.sh after every git pull; until you do, voice-loopctl says so.

Add $REPO/bin to your PATH to drop the full path.
EOF
