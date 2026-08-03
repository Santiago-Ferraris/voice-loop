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

> **Status: phase 2 shipped — the loop is closed.** All five steps above work. What is left is
> polish: naming unnamed windows by voice, asking for your pendings out loud, and the phonetic
> dictionary. See [Current state](#current-state).

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
| Speech in | Deepgram `nova-3` (`language=multi`) or OpenAI `gpt-4o-transcribe`, behind a swappable adapter (`whisper-cpp` and Deepgram streaming are *planned*, not implemented) |
| Menus | Answer by number or keyword; "explicame la dos" reads the option's description. Multi-select takes "uno y tres" |
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
- `ffmpeg`, for the microphone — `brew install ffmpeg`
- A `DEEPGRAM_API_KEY` (or `OPENAI_API_KEY`) for speech-to-text
- [`skhd`](https://github.com/koekeishiya/skhd) for the global hotkeys — optional, installed by
  `skhd/install-hotkeys.sh`. Without it the mic still opens after every announcement

## Install

```sh
git clone https://github.com/Santiago-Ferraris/voice-loop.git
cd voice-loop
./install.sh
```

That installs the **runtime** into `~/.local/share/voice-loop` (a virtualenv plus a copy of the
wrappers and your config), registers a launchd agent (`com.voiceloop.daemon`) pointing at it,
waits until the daemon actually answers on its control socket, and merges the hooks into
`~/.claude/settings.json` — backing it up first and touching nothing else. `./uninstall.sh`
reverses all of it, runtime included.

Then add your key and restart the daemon:

```sh
$EDITOR ~/.config/voice-loop/env     # OPENAI_API_KEY=sk-…
bin/voice-loopctl restart
```

**Then grant the microphone — this is a step, not a footnote.** `install.sh` ends by running
`doctor` from your terminal for exactly this reason: it records one second of audio, which is
what makes macOS raise the consent dialog while you are still sitting there. Until it is
answered, every mic open parks on an invisible prompt, the hotkey looks dead, and nothing in the
log says why. If you skipped it (or installed over SSH), do it by hand:

```sh
bin/voice-loopctl doctor     # answer the dialog, then run it again
```

See [Permissions](#permissions) for what to do when the *daemon's* column keeps failing after
your terminal's has gone green — they are two different grants.

Hooks are read when a session starts, so **open a new Claude window** to see it work. Sessions
that were already running keep their old hook set until you restart them.

```sh
bin/voice-loopctl status     # is it up, what's queued, is the mic live
bin/voice-loopctl doctor     # can it reach the mic and iTerm2 — from both sides
bin/voice-loopctl pendings   # everything still waiting on you
bin/voice-loopctl skip       # drop the last announcement off the list
bin/voice-loopctl pause      # silence without losing the queue
```

### Hotkeys

```sh
./skhd/install-hotkeys.sh                            # the two defaults
./skhd/install-hotkeys.sh --mic-key 'ctrl + alt - space'
./skhd/install-hotkeys.sh --remove
```

| Key | What it does |
|---|---|
| `ctrl + alt + cmd - m` | Open the mic — or close one that is already open, which is how you send |
| `ctrl + alt + cmd - b` | Busy mode: announcements chime instead of speaking, and stop opening the mic on their own |

The block goes into `~/.config/skhd/skhdrc` between markers, so re-running updates it in place
and `--remove` takes it back out; the rest of your file is never touched, and it is backed up
first. skhd needs Accessibility permission the first time — macOS asks, and the keys do nothing
until you grant it.

### Permissions

The microphone and iTerm2 Automation prompts are granted to the *responsible process*, and under
launchd that is the agent, not your terminal. So the first `doctor` run matters:

```sh
bin/voice-loopctl doctor
```

It runs the checks locally — which is what makes macOS show you the two consent dialogs, since a
LaunchAgent may never get the chance to — and then asks the daemon to run the same ones from
where it lives. Two columns; the difference between them is the bug. A denied Automation prompt
shows up as AppleScript error `-1743`, and a denied microphone as an ffmpeg capture with no
samples in it.

**A capture that hangs is not a slow capture.** A one-second probe that times out after twenty
is a process parked on a consent prompt nobody answered — `doctor` says so in those words. It is
the normal state of a fresh install *before* the first grant, and it self-corrects the moment you
say yes.

**If your terminal's column is green and the daemon's is not**, that is the grant working exactly
as macOS intends: permission belongs to the responsible process, and for a LaunchAgent that is
launchd rather than iTerm2. Tick the runtime's interpreter
(`~/.local/share/voice-loop/.venv/bin/python3`) in System Settings → Privacy & Security →
Microphone, then `bin/voice-loopctl restart`. When the daemon hits this at runtime it says so out
loud rather than only logging it — you are not looking at a terminal, which is the whole premise
of the project.

The key itself lives in `~/.config/voice-loop/env`, which the daemon's launcher sources and
`voice-loopctl` does not. `doctor` reads that file directly, so a configured key is never
reported as missing, and a missing one is reported by name and path.

### Why the daemon does not run from the clone

macOS TCC does not let a LaunchAgent touch `~/Documents`, `~/Desktop` or `~/Downloads` — it
cannot even *execute* a file there. A clone in one of them gives you a launchd agent that dies
instantly with `Operation not permitted` and exit 126, with a perfectly valid plist:

```
$ launchctl list | grep voiceloop
-	126	com.voiceloop.daemon
```

So `install.sh` copies everything launchd touches out to `~/.local/share/voice-loop` and points
the plist there; the renderer refuses outright to write a plist naming a protected path. The
clone stays where you want it and is what you develop in.

The price is two copies, so **re-run `./install.sh` after every `git pull`** — that one command
is what updates the runtime. Until you do, `voice-loopctl` prints a warning from the clone
saying the daemon is running older code, because the alternative is an hour spent debugging a
bug you already fixed.

### Checking it really is launchd running it

```sh
launchctl list | grep voiceloop          # a pid in the first column, not "-"
launchctl kickstart -k gui/$(id -u)/com.voiceloop.daemon
bin/voice-loopctl status                 # answers again after the restart
```

A `-` in the first column means nothing is running; the second column is the last exit status
(126 is the TCC failure above). If the daemon does not come up, `install.sh` fails and prints
the tail of `~/.local/state/voice-loop/logs/stderr.log` rather than claiming success.

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
`paths.state_dir` (`~/.local/state/voice-loop` by default). The installed program itself lives
under `~/.local/share/voice-loop`; `config.local.yml` is copied there by `install.sh`, so
editing it is another reason to re-run the installer.

| Variable | What it moves |
|---|---|
| `VOICE_LOOP_RUNTIME_DIR` | where the runtime is installed |
| `VOICE_LOOP_ENV_FILE` | where the API keys are read from |
| `VOICE_LOOP_PIP_INDEX_URL` | the package index `install.sh` uses (defaults to PyPI, ignoring your pip config — a stale token on a private index is not voice-loop's problem to inherit) |

## Answering out loud

The mic opens by itself after every announcement, and by hotkey whenever you want it. It is a
**toggle**, not push-to-talk: it closes on its own after a beat of silence, or when you press
the key again. Say nothing and the item simply stays in `pendings` — the queue moves on.

| You say | What happens |
|---|---|
| anything at all | Typed into that window verbatim and submitted. Claude reads disfluent speech fine |
| "dos", "la dos", "postgres" | Picks that option off the menu |
| "uno y tres" | Both, on a multi-select menu |
| "explicame la dos" | Reads you that option's description, mic stays open |
| "repetí" | Says the announcement again |
| "mostrame" | Focuses that tab. Nothing else ever moves your focus |
| "salteá" / "después" | Leaves it pending and moves on |
| "dale" / "no" | Confirms or cancels a read-back. Anywhere else it is just a word, and gets typed |

**Read-backs.** A transcript the recognizer was unsure about, or one matching
`delivery.confirm_if_matches`, is read back to you before it is sent. Say "dale" to send it, "no"
to drop it, or just say something else — that replaces it.

## How delivery works

Free text is typed with `write text` and submitted with a separate carriage return. That is not
belt-and-braces: `write text`'s own trailing newline submits a short prompt but **not** a long
one — a 150-character reply lands in the input box and sits there. Verified on Claude Code
2.1.220.

Menus ignore typed text entirely, so the selector is driven with arrow keys, and the index always
comes from the hook payload — never from what is on screen. The rendered menu has rows the
payload does not (`Type something.`, `Chat about this`), and a plan menu renders four rows for a
payload that carries none at all.

| Case | Keystrokes |
|---|---|
| Option N | N-1 × `↓`, then `⏎` — the cursor starts on option 1 |
| Multi-select | `space` on each option on the way down, then `→` onto the review tab and `⏎` |
| Free text into a menu | Navigate to the text row *first*, then type — text typed on any other row is swallowed |

A plan menu's rows belong to Claude, not to the payload, so they are the one thing that cannot be
derived: `1` approve with auto mode, `2` approve reviewing each edit, `4` "Tell Claude what to
change", which is where spoken feedback goes. If a future version of Claude Code reorders them,
`delivery.plan_menu.feedback_index` is the escape hatch.

Before anything is delivered, the session has to still exist — a window closed between the
announcement and your answer must not have keystrokes delivered to whatever inherited its tty.

## How phase 1 behaves

- A turn that ends while **background subagents are still running** is not announced. You have
  nothing to answer yet, so the item waits — keeping its place in line — until they finish.
- Background (`claude agents`) sessions never speak.
- Answering a session, by voice or by typing, resolves everything it had queued.
- **One open item per window.** A session that blocks again supersedes what it already had
  waiting instead of queueing a second copy — you hear each window once, and `pendings` never
  fills up with four rows for the same one. The item keeps its place in line and its "waiting
  since" time; only the content is refreshed.
- Ignored announcements are never dropped: `voice-loopctl pendings` still lists them, by window
  name, whether or not they have been announced yet. `voice-loopctl skip [id]` is the way out
  for an item that stopped mattering — without it, only real activity in that session clears it.
- Milestones (a PR being created) only chime. If some other tool of yours already tracks a
  per-terminal phase in a file, point `integrations.milestone_file_watch` at it — off by default.

## Developing

`install.sh` builds the runtime, not a dev environment. For the test suite:

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

`bin/voice-loopctl` prefers that venv when it exists and falls back to the installed runtime,
so it works from the clone either way — and it is the same daemon on the other end of the
socket. After changing anything the daemon runs, `./install.sh` again.

## Current state

- [x] Design locked — 20 decisions, see the table above
- [x] Speech-to-text and summary models benchmarked on code-switched speech
- [x] **Routing spike verified** — AppleScript `write text` delivers into Claude Code's
      fullscreen TUI as a real user turn, Enter included, without stealing focus
- [x] Phase 1 — hooks, queue, TTS, summaries *(it talks to you)*
- [x] Phase 2 — hotkeys, speech-to-text, delivery *(it listens, and answers the right window)*
- [ ] Phase 3 — naming unnamed windows by voice, asking for pendings out loud, phonetic
      dictionary, Deepgram streaming instead of the local silence cutoff
