# voice-loop v2 (Swift)

A push-to-talk voice assistant for your Claude Code windows, as a native macOS
menu-bar app. This is the **v2 rewrite** of the Python daemon that lives (for
now) alongside it in the repo root; see "Transition" below.

Hold **⌥N** to dictate *verbatim* into the Claude window in front of you. Hold
**⌥M** to speak an instruction that gets routed by an LLM to the right window —
by name, focused, or a new tab — with a read-back only when it is ambiguous. A
quick ⌥M tap (no voice) confirms. Esc during a hold cancels.

## Architecture

Three modules, so the reusable logic is headless and another OS could write its
own UI:

- **`VoiceLoopCore`** — pure logic, no AppKit: post-STT normalization, vocabulary
  extraction, session naming, the roster model, the LLM router (protocol + both
  impls), the event schema, and the `ps` parsers. Fully unit-tested.
- **`VoiceLoopEngine`** — AppKit/CoreGraphics/AVFoundation: hotkeys (CGEventTap),
  mic capture, the Deepgram streaming client, iTerm2/AppleScript dispatch, TTS,
  the orchestrator state machine, and the event socket server.
- **`VoiceLoopApp`** — the UI: an `NSStatusItem` and a SwiftUI HUD. It is only a
  *subscriber* of the Engine's socket — the same contract `nc -U` reads.

The app runs as `LSUIElement` (menu bar, no Dock), registers itself as a login
item via `SMAppService`, and does **not** live in `~/Documents` — that avoids the
launchd/TCC traps that plagued v1. App sandbox is **off** (a global CGEventTap,
AppleScript to iTerm2, reading `~/.claude/*`, `ps`, and tty injection are all
incompatible with it), so distribution is direct + notarized, not the App Store.

## Build & test

```bash
cd swift
swift test                 # unit tests (Core + Engine + App)
xcodebuild -scheme VoiceLoop -destination 'platform=macOS' build   # builds the app binary
./build-app.sh release     # assembles + ad-hoc-signs build/VoiceLoop.app
```

`Package.swift` is xcodebuild-native (no hand-rolled `.xcodeproj`); `build-app.sh`
wraps the SPM executable into `VoiceLoop.app` with the Info.plist, entitlements
and the AppleScript resource bundle, then signs it. For a real release, replace
the ad-hoc signature with a Developer ID identity and notarize.

## Install (first run)

1. `./build-app.sh release`
2. `open build/VoiceLoop.app` — a mic icon appears in the menu bar (no Dock icon).
3. Menu → **Settings… → Run Doctor**. This fires the TCC prompts while you are
   sitting there (trap #8). Grant:
   - **Microphone** — the `NSMicrophoneUsageDescription` prompt.
   - **Accessibility** — required for the ⌥N/⌥M `CGEventTap`; without it the
     hotkeys are a silent no-op. macOS opens System Settings → Privacy &
     Security → Accessibility; enable VoiceLoop.
   - **Automation → iTerm2** — the AppleEvents prompt, the first time it types.
4. In Settings, pick your input device **by name** (e.g. *MacBook Pro
   Microphone* — never an index; Continuity puts the iPhone first). The Doctor
   reports the resolved device and the system input gain.
5. Keys: the Keychain is read first, falling back to `~/.config/voice-loop/env`,
   so the existing `OPENAI_API_KEY` / `DEEPGRAM_API_KEY` work with nothing typed
   in. To store them in the Keychain instead, paste them in Settings → Save keys.

The router defaults to **OpenAI gpt-4o-mini** (the key present out of the box).
Flip to **Anthropic Haiku 4.5** in Settings once an `ANTHROPIC_API_KEY` exists.

## Operation

- Config: `~/Library/Application Support/VoiceLoop/config.json`
- Event socket: `~/.local/state/voice-loop/engine.sock` (0600) — `nc -U` it.
- Keys: Keychain (`com.voiceloop.keys`), or `~/.config/voice-loop/env`.
- Event schema: `docs/event-schema.md`.

## Uninstall

Quit from the menu, delete `build/VoiceLoop.app`, and (if registered) remove the
login item via System Settings → General → Login Items. Remove
`~/Library/Application Support/VoiceLoop/` and `~/.local/state/voice-loop/`.

## Transition

The Python v1 (`voiceloop/`, `tests/`, `hooks/`, `bin/`, `launchd/`, `skhd/`,
`install.sh`) is kept **read-only as the source of knowledge** until the Swift
rewrite reaches feature parity, at which point it is deleted (Hito 8). All the
calibrated behavior — spoken-model repair, vocabulary extraction, naming,
roster, the AppleScript mechanics and their environment traps — has been ported
into `VoiceLoopCore`/`VoiceLoopEngine` with unit tests that pin it.
