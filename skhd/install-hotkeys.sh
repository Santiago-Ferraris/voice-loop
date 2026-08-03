#!/bin/sh
# install-hotkeys.sh — register voice-loop's two global hotkeys with skhd.
#
# Deliberately separate from install.sh: skhd is a third-party daemon that
# wants its own Accessibility permission, and the loop works without it (the
# mic still opens after every announcement). Run this when you want the keys.
#
#   ./skhd/install-hotkeys.sh
#   ./skhd/install-hotkeys.sh --mic-key 'ctrl + alt - space'
#   ./skhd/install-hotkeys.sh --remove
#
# The two lines go into ~/.config/skhd/skhdrc between markers, so re-running
# updates them in place and --remove takes them back out — the rest of your
# skhdrc is never touched, and it is backed up before the first real change.
#
# Flags:
#   --mic-key KEY     default "ctrl + alt + cmd - m"
#   --busy-key KEY    default "ctrl + alt + cmd - b"
#   --ctl PATH        the voice-loopctl to call (default: the installed runtime)
#   --skhdrc PATH     default "$HOME/.config/skhd/skhdrc"
#   --remove          take the block out again
#   --no-service      do not brew-install skhd or (re)start its service
set -eu

REPO=$(cd "$(dirname "$0")/.." && pwd -P)

MIC_KEY="ctrl + alt + cmd - m"
BUSY_KEY="ctrl + alt + cmd - b"
RUNTIME="${VOICE_LOOP_RUNTIME_DIR:-$HOME/.local/share/voice-loop}"
CTL=""
SKHDRC="${SKHDRC:-$HOME/.config/skhd/skhdrc}"
DO_REMOVE=0
DO_SERVICE=1

BEGIN='# >>> voice-loop hotkeys >>>'
END='# <<< voice-loop hotkeys <<<'

while [ "$#" -gt 0 ]; do
  case "$1" in
    --mic-key) MIC_KEY="${2:?--mic-key needs a value}"; shift 2 ;;
    --busy-key) BUSY_KEY="${2:?--busy-key needs a value}"; shift 2 ;;
    --ctl) CTL="${2:?--ctl needs a value}"; shift 2 ;;
    --skhdrc) SKHDRC="${2:?--skhdrc needs a value}"; shift 2 ;;
    --remove) DO_REMOVE=1; shift ;;
    --no-service) DO_SERVICE=0; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "install-hotkeys.sh: unknown option $1" >&2; exit 2 ;;
  esac
done

# The installed runtime is what launchd runs; fall back to the clone so this
# works before install.sh has ever been run.
if [ -z "$CTL" ]; then
  if [ -x "$RUNTIME/bin/voice-loopctl" ]; then
    CTL="$RUNTIME/bin/voice-loopctl"
  else
    CTL="$REPO/bin/voice-loopctl"
  fi
fi

# --- 1. skhd itself --------------------------------------------------------

if [ "$DO_SERVICE" -eq 1 ] && [ "$DO_REMOVE" -eq 0 ]; then
  if ! command -v skhd >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
      echo "skhd:  installing via homebrew"
      brew install koekeishiya/formulae/skhd
    else
      echo "install-hotkeys.sh: skhd is not installed and there is no brew." >&2
      echo "  brew install koekeishiya/formulae/skhd" >&2
      exit 1
    fi
  fi
fi

# --- 2. the skhdrc block ---------------------------------------------------

mkdir -p "$(dirname "$SKHDRC")"
[ -e "$SKHDRC" ] || : > "$SKHDRC"

BLOCK=""
if [ "$DO_REMOVE" -eq 0 ]; then
  BLOCK=$(printf '%s\n%s : %s mic-toggle\n%s : %s busy-toggle\n%s\n' \
    "$BEGIN" "$MIC_KEY" "$CTL" "$BUSY_KEY" "$CTL" "$END")
fi

TMP="$SKHDRC.voice-loop.tmp"
BEGIN="$BEGIN" END="$END" BLOCK="$BLOCK" awk '
  BEGIN { begin = ENVIRON["BEGIN"]; end = ENVIRON["END"]; block = ENVIRON["BLOCK"]; seen = 0 }
  $0 == begin { inside = 1; if (block != "") { print block; seen = 1 } ; next }
  $0 == end   { inside = 0; next }
  !inside     { print }
  END { if (!seen && block != "") { print block } }
' "$SKHDRC" > "$TMP"

if cmp -s "$TMP" "$SKHDRC"; then
  rm -f "$TMP"
  echo "skhdrc: already up to date ($SKHDRC)"
else
  cp "$SKHDRC" "$SKHDRC.voice-loop-backup-$(date +%Y%m%d%H%M%S)"
  mv "$TMP" "$SKHDRC"
  if [ "$DO_REMOVE" -eq 1 ]; then
    echo "skhdrc: removed the voice-loop block from $SKHDRC"
  else
    echo "skhdrc: wrote the voice-loop block into $SKHDRC"
  fi
fi

# --- 3. the service --------------------------------------------------------

if [ "$DO_SERVICE" -eq 1 ] && command -v skhd >/dev/null 2>&1; then
  if [ "$DO_REMOVE" -eq 1 ]; then
    skhd --reload 2>/dev/null || true
  else
    skhd --start-service 2>/dev/null || skhd --restart-service 2>/dev/null || true
    skhd --reload 2>/dev/null || true
  fi
fi

if [ "$DO_REMOVE" -eq 0 ]; then
  cat <<EOF

Hotkeys registered:
  $MIC_KEY   open or close the mic
  $BUSY_KEY   toggle busy mode (chime only)

skhd needs Accessibility permission the first time — macOS will ask, and the
keys do nothing until you grant it (System Settings > Privacy & Security >
Accessibility). Check the wiring with:

  $CTL status
EOF
fi
