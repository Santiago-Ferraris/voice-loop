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

> **Status: design complete, implementation in progress.** The routing spike is verified;
> see [Current state](#current-state).

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

Hooks are deliberately dumb: they append an event and exit in milliseconds, so they never
delay your prompt. Everything slow — summarizing, speaking, listening — happens in the daemon.

Windows identify themselves by Claude's own session name, so what you hear matches what you see
in the prompt box and in `/resume`. A session that's still running an auto-generated name gets
announced with a summary and a proposed name; say "dale" and it sticks.

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
| Speech in | Deepgram `nova-3` (`language=multi`) or OpenAI `gpt-4o-transcribe`, behind a swappable adapter |
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
- iTerm2
- Claude Code
- [`skhd`](https://github.com/koekeishiya/skhd) for the global hotkeys
- An API key for your chosen speech-to-text provider

## Configuration

```sh
cp config.example.yml config.local.yml
```

`config.local.yml` is gitignored. It holds your project vocabulary — service names, tools, the
verbs you actually say — which is the single biggest lever on transcription accuracy. API keys
are read from the environment, never from the config file and never committed.

## Current state

- [x] Design locked — 20 decisions, see the table above
- [x] Speech-to-text and summary models benchmarked on code-switched speech
- [x] **Routing spike verified** — AppleScript `write text` delivers into Claude Code's
      fullscreen TUI as a real user turn, Enter included, without stealing focus
- [ ] Phase 1 — hooks, queue, TTS, summaries *(it talks to you; doesn't listen yet)*
- [ ] Phase 2 — hotkeys, speech-to-text, delivery
- [ ] Phase 3 — naming, pendings, busy mode, dictionaries
