# voice-loop

Hands-free voice control for parallel [Claude Code](https://claude.com/claude-code) sessions in iTerm2.

If you run one Claude session, you watch it. If you run a dozen, you stop watching and start
scanning tabs — and the bottleneck becomes *noticing* that a window is waiting for you.
voice-loop closes that gap without a GUI:

1. A session blocks (asks a question, finishes a turn, wants a plan approved).
2. You hear its **name** and a one-sentence summary of what it wants.
3. The mic opens. You answer out loud.
4. Your reply is typed into **that** window — not the focused one — and submitted.
5. *"Quedan 2."* Next in the queue.

Nothing steals your focus. Requests are answered one at a time, in order, and anything you
skip stays reachable — ask for your pendings whenever you want.

> **Status: phase 1 shipped — it talks, it doesn't listen yet.** Steps 1 and 2 above work
> today; the mic lands in phase 2. See [Current state](#current-state).

## Why not just use dictation?

Claude Code has built-in voice dictation, and it's good — but it types into the window you're
already looking at. It doesn't tell you a window needs you, doesn't keep multiple requests from
colliding, and doesn't know which of your fifteen sessions you mean. Those three things are the
actual problem, and they're what this handles.

## How it works

```
Claude sessions (N windows)
   │ Notification + Stop hooks  ─→  queue  ←─  session roster (live name + status)
   ↓                                  │
                          ┌───────────┴──────────┐
                          │        daemon        │  ← 2 global hotkeys (skhd)
                          └───────────┬──────────┘
        ┌───────────────┬─────────────┼─────────────┬───────────────┐
     summary          say(1)         STT       AppleScript      focus tab
      (LLM)         offline TTS   spanglish    write text      (on request)
```

Hooks are deliberately dumb: each one writes a single JSON file into a spool directory and
exits in tens of milliseconds, so a dozen sessions firing at once never contend on anything and
your prompt is never delayed. They don't even open the database. The daemon is the only process
that owns state, and the only one that does anything slow — summarizing, speaking, listening.

Windows identify themselves by Claude's own session name, so what you hear matches what you see
in the prompt box and in `/resume`. A session that's still running an auto-generated name gets
announced with a summary and a proposed name; say "dale" and it sticks *(phase 3)*.

## Design decisions

| Area | Decision |
|---|---|
| Mic | Opens automatically after each announce; also on a global hotkey. Toggle, with silence cutoff — not push-to-hold |
| Hotkeys | Two, via `skhd` (no GUI, no menu bar icon): open mic, and toggle busy mode |
| Busy mode | Chime only, no speech. The mic still works — you can ask for your pendings at any time |
| Queue | FIFO, auto-chaining. Skipped items are never dropped |
| Focus | Never moves on its own. "mostrame" focuses the tab on request |
| Delivery | Submits automatically; reads back first when the recognizer was unsure or the phrase looks destructive |
| Dictation | Passed through verbatim — Claude handles disfluent speech fine. Only control commands are intercepted |
| Events | Blocking events speak; milestones (PR opened, CI green) only chime |
| Speech in | Deepgram `nova-3` (`language=multi`) or OpenAI `gpt-4o-transcribe`, behind a swappable adapter (`whisper-cpp` is *planned*, not implemented) |
| Speech out | macOS `say` — offline, no latency, no cost |
| Summaries | `gpt-4o-mini` |

### Getting spanglish right

The design target is code-switched speech — Spanish sentences with English technical terms —
because that's how the author actually talks to a terminal. Two findings from benchmarking,
both baked into `config.example.yml`:

- **Vocabulary hints are not optional.** Without them, a conjugated loanword like *"mergealo"*
  comes back as *"MGalo"*. With Deepgram's `keyterm` params (or OpenAI's prompt), it's exact.
- **Turn off smart formatting.** Deepgram's `smart_format` rewrites spoken ordinals, so
  *"fijate primero"* arrives as *"fijate 1º"* — and that's what Claude would receive.

With both applied, Deepgram and OpenAI tied on accuracy across the test phrases. Deepgram is
the default for its free credit, real streaming, and server-side endpointing, which supplies
the silence cutoff for free.

## Requirements

- macOS (uses `say`, AppleScript and iTerm2's scripting interface)
- Python 3.10+
- iTerm2
- Claude Code
- An `OPENAI_API_KEY` for the summaries — optional; without one, announcements fall back to
  a fixed phrase instead of a summary
- *Phase 2:* [`skhd`](https://github.com/koekeishiya/skhd) for the global hotkeys, and an API
  key for your speech-to-text provider

## Install

```sh
git clone https://github.com/Santiago-Ferraris/voice-loop.git
cd voice-loop
./install.sh
```

That creates a virtualenv, installs a launchd agent (`com.voiceloop.daemon`), and merges the
hooks into `~/.claude/settings.json` — backing it up first and touching nothing else. Re-running
it is a no-op. `./uninstall.sh` reverses all of it.

Then add your key and restart the daemon:

```sh
$EDITOR ~/.config/voice-loop/env     # OPENAI_API_KEY=sk-…
bin/voice-loopctl restart
```

Hooks are read when a session starts, so **open a new Claude window** to see it work. Sessions
that were already running keep their old hook set until you restart them.

```sh
bin/voice-loopctl status     # is it up, what's queued
bin/voice-loopctl pendings   # everything still waiting on you
bin/voice-loopctl pause      # silence without losing the queue
```

## Configuration

```sh
cp config.example.yml config.local.yml
```

`config.example.yml` holds the defaults and is the documentation; `config.local.yml` is
gitignored and deep-merged on top, so it only needs the keys you override. It holds your project
vocabulary — service names, tools, the verbs you actually say — which is the single biggest lever
on transcription accuracy.

**API keys never live in either file.** The daemon reads them from its environment, which
`bin/voice-loopd` sources from `~/.config/voice-loop/env` (created empty, `chmod 600`, outside the
repo) because launchd does not inherit your shell. A config file carrying a key-shaped entry is
rejected at startup rather than silently ignored.

Runtime state — the spool, the SQLite queue, the control socket and the logs — lives under
`paths.state_dir` (`~/.local/state/voice-loop` by default).

## How phase 1 behaves

- A turn that ends while **background subagents are still running** is not announced. You have
  nothing to answer yet, so the item waits — keeping its place in line — until they finish.
- Background (`claude agents`) sessions never speak.
- Answering a session, by voice or by typing, resolves everything it had queued.
- Ignored announcements are never dropped: `voice-loopctl pendings` still lists them.
- Milestones (a PR being created) only chime. If some other tool of yours already tracks a
  per-terminal phase in a file, point `integrations.milestone_file_watch` at it — off by default.

## Current state

- [x] Design locked — 20 decisions, see the table above
- [x] Speech-to-text and summary models benchmarked on code-switched speech
- [x] **Routing spike verified** — AppleScript `write text` delivers into Claude Code's
      fullscreen TUI as a real user turn, Enter included, without stealing focus
- [x] Phase 1 — hooks, queue, TTS, summaries *(it talks to you; doesn't listen yet)*
- [ ] Phase 2 — hotkeys, speech-to-text, delivery
- [ ] Phase 3 — naming, pendings, busy mode, dictionaries
