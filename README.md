# voice-loop

Hands-free voice control for parallel [Claude Code](https://claude.com/claude-code) sessions in iTerm2.

If you run one Claude session, you watch it. If you run a dozen, you stop watching and start
scanning tabs — and the bottleneck becomes *noticing* that a window is waiting for you.
voice-loop closes that gap without a GUI:

1. A session blocks (asks a question, finishes a turn, wants a plan approved).
2. You hear its **name** and a one-sentence summary of what it wants.
3. The mic opens. You answer out loud.
4. Your reply is typed into **that** window — not the focused one — and submitted.
5. *"Quedan 2."* — once your answer is in, not before it. Next in the queue.

Nothing steals your focus. Requests are answered one at a time, in order, and anything you
skip stays reachable — ask for your pendings whenever you want.

> **Status: phase 3 shipped — the loop is closed and it knows its own queue.** All five steps
> above work; windows name themselves out loud, and "dame los pendientes" / "estado" answer from
> any mode. What is left is Deepgram streaming instead of the local silence cutoff. See
> [Current state](#current-state).

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
in the prompt box and in `/resume`. A session still running an auto-generated name (`darwin-21`)
is announced as "darwin 21" and offered a better one the moment you ask what it wants; say "dale"
and it sticks. See [Naming windows out loud](#naming-windows-out-loud).

**The announcement is a heads-up, not a report.** A window that blocks gets a chime, its name,
and four seconds of microphone:

```
ventana se bloquea
  → chime + "Nuevo evento de inbox realtime"
  → el mic se abre solo, ~4 s
     → "dámelo"   → el resumen, y el mic para contestarlo → "quedan N"
     → "después"  → al fondo de la cola
     → silencio   → se queda en la cola y el mic se cierra
```

That is the whole interruption. Nothing is ever announced twice and nothing is ever reminded
about: an ignored window waits in `pendings`, silently, until you ask for it. See
[Answering out loud](#answering-out-loud).

## Design decisions

| Area | Decision |
|---|---|
| Announcing | A heads-up: a chime and the window's name. What it wants is what "dámelo" is for |
| Mic | Opens automatically after each announce — four seconds on the heads-up, longer once you are answering — and on a global hotkey. Toggle, with silence cutoff, not push-to-hold |
| Hotkeys | Two, via `skhd` (no GUI, no menu bar icon): open mic, and toggle busy mode |
| Busy mode | Silence: no chime, no voice, no mic. The queue piles up and the toggle says how much. The hotkey still works — you can ask for your pendings at any time |
| Reminders | None. An announcement happens once; after that the item waits to be asked for |
| Queue | FIFO, auto-chaining. Skipped items are never dropped |
| Focus | Never moves on its own. "mostrame" focuses the tab on request |
| Delivery | Submits automatically; reads back first when the recognizer was unsure or the phrase looks destructive |
| Dictation | Passed through verbatim — Claude handles disfluent speech fine. Only control commands are intercepted |
| Events | Blocking events speak; milestones (PR opened, CI green) only chime |
| Speech in | Deepgram `nova-3` (`language=multi`) or OpenAI `gpt-4o-transcribe`, behind a swappable adapter (`whisper-cpp` and Deepgram streaming are *planned*, not implemented) |
| Menus | Answer by number or keyword; "explicame la dos" reads the option's description. Multi-select takes "uno y tres" |
| Speech out | macOS `say` — offline, no latency, no cost |
| Summaries | `gpt-4o-mini`. The proposed name for an unnamed window rides the same request — one call, two fields |
| Names | Offered once per window, never twice. Confirmed names are what you hear *and* vocabulary for the recognizer |

### Understanding you, not the phrasing you were supposed to use

The phrase lists in `intents.py` are exact and instant, and they resolve almost everything —
but they only know the wordings somebody thought to write down. Measured against sixteen
natural ways of saying things this user says daily, **fourteen fell through**: `"give it to
me"`, `"later"`, `"skip it"`, `"show me"`, `"status"`, `"dale contame"`, `"ok dame"`, `"not
now"`, `"push it back"`, `"what's pending"`, `"tell me"`, `"read it"`, `"hold on"`, `"what do
I have"`. Every one arrived as *text* — which means every one was typed into a Claude session.

So classification is a hybrid. The lists answer what they know, offline and instantly;
whatever falls through goes to `gpt-4o-mini` on a two-second leash. Three things keep that
honest:

- **"Not a command" is an answer.** A classifier that always picks something turns every
  dictated sentence into a random command, so the prompt is built around refusing, and
  `"mergealo cuando pasen los tests"` is expected to come back as an empty list.
- **It degrades to what it replaced.** No key, no network, bad JSON — all of them fall back to
  the lexicon's own verdict. It is an improvement, never a dependency (`understanding.provider:
  none` turns it off entirely).
- **It returns a list.** "Ok dámelo, y también abrí una ventana nueva y hacé X" is three
  things, and they run in the order you said them. A failure in one is spoken and the rest
  still run; anything on the destructive list still gets read back first.

**A near miss never reaches the model.** It is asked about by name — *"Entendí: dame al
pendiente. ¿Querés los pendientes?"* — and a yes runs the command, while a no, silence, or
anything else drops it. It is never delivered.

How close it has to be depends on how much is at stake, because **recognizer confidence does
not detect this failure and never will**. Measured out loud on a machine somebody was working
on:

| said | heard | confidence |
|---|---|---|
| "contame" | `'contain'` | **0.96** |
| "dámelo" | `'chamelo'` / `'jamelo'` | 0.75 |
| "dame los pendientes" | `'dame los pendins'` | 0.70 |

`'contain'` was typed into a working window and the user asked what it was. The recognizer was
*right* — it heard a sound that genuinely resembles "contain" — so the error is semantic and
the score was 0.96. So the policy is inverted: not "deliver unless the recognizer was unsure",
but **"deliver only when this is plausibly something you would dictate"**. A phrase long enough
to be an instruction has to look *a lot* like a command (0.8) before we doubt it; one or two
words has only to look *somewhat* like one (0.7), because nobody dictates two words to Claude.
A yes in front — "dale, mergealo" — is stripped before comparing, or every instruction that
opens with "dale" would be asked about forever.

### The vocabulary writes itself

`keyterms` is what stops the recognizer inventing domain words — without it *"mergealo"* comes
back as *"MGalo"* — and a hand-kept list is wrong the week after you write it. So most of it is
derived: Claude already stores every prompt you have typed under `~/.claude/projects/*/*.jsonl`,
and counting the words in **your own** messages, minus ordinary Spanish, produces the real list
(`pr`, `test`, `issue`, `draft`, `stage`, `mergeado`, `lambda`…). Recomputed in the background
every `vocabulary.refresh_hours`, and merged with your `keyterms` and the names of the windows
open right now.

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
| `ctrl + alt + cmd - b` | Busy mode: nothing is announced at all and the queue piles up. It says which mode it left you in — "ocupado" going in, "te escucho" and how much piled up coming out |

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

**The mic is not a window you have to catch.** It opens *with* the chime, stays open under
every word of whatever is being said, and keeps listening for `announce.mic_grace_seconds`
(ten) after the last one. Start talking late and it waits for you to finish; start talking
early and you are talking over it, which is allowed. Say nothing and the item simply stays in
`pendings` — the queue moves on. It closes with a chime of its own (`mic_close_chime`), which
is the only way to know it is no longer listening.

So the announcement chime no longer means "your turn now". It means *"I am about to say
something, and you can talk to me"*.

**Talking over it** depends on where the sound is coming out, which voice-loop reads off the
system rather than asking you to configure:

- **Headphones** (bluetooth, the jack, anything whose name says headset) — the mic cannot hear
  `say` at all, so your first syllable kills the sentence mid-word and it listens instead.
- **Speakers** — the mic *does* hear `say`, so nothing is interrupted (it would shut itself up
  every time it opened its mouth) and our own words are subtracted from the transcript instead.
  We know exactly what `say` was given, so recognising it coming back is a string comparison.
  See `voiceloop/echo.py` for the three rules that keep it from eating what you actually said.

Everything below works in the heads-up mic — it is a microphone, not a menu — with one
difference: a **sentence** is read back rather than delivered. Nothing has been said about that
window yet, so a sentence there is as likely to be a word to somebody in the room as an answer
for a window you have not heard about.

| You say | What happens |
|---|---|
| "dámelo" / "contame" / "a ver" | Reads out what that window wants, and opens the mic to answer it |
| "después" / "mandalo al fondo" | To the back of the line, without a word about what it wanted. There is no snooze by the clock |
| anything at all | Typed into that window verbatim and submitted. Claude reads disfluent speech fine |
| "dos", "la dos", "postgres" | Picks that option off the menu |
| "uno y tres" | Both, on a multi-select menu |
| "explicame la dos" | Reads you that option's description, mic stays open |
| "repetí" | Says the last thing it said again |
| "mostrame" | Focuses that tab. Nothing else ever moves your focus |
| "salteá" | Leaves it where it is and moves on |
| "dale" / "no" | Confirms or cancels a read-back. On a heads-up, "dale" means "dámelo". Anywhere else it is just a word, and gets typed |
| "dame los pendientes" / "cuál queda" | Reads the queue out — name, what it wants, how long it has waited — then takes a pick |
| "abrí una ventana nueva y hacé X" | New tab in the window you already have, running `windows.new_tab_command`, then X typed into it |
| "decile a inbox realtime que espere" | Types into the window with that name, whichever one you are on |
| "dámelo, y también abrí una ventana y hacé X" | Several things in one breath, in the order you said them. One that fails does not cancel the rest |
| "estado" / "cómo venimos" | Windows open, how many are working, how many are waiting on you |
| "qué dijiste" / "no te entendí" | Says the last thing it said again |
| "esperá" / "un segundo" | Holds the mic. Not an answer and not a refusal — nothing is typed, nothing is dropped |

**Menus are read short.** A question plus its labels, and the labels only as far as they say
something: "(Recomendado)", "[beta]" and anything hanging off a dash are dropped, and what is
left is cut to its first few words. Four labels read whole are twenty-five seconds of audio for a
decision that takes five. Nothing goes out of reach — "explicame la dos" reads the option in
full, and a spoken keyword is still matched against the *whole* label, whichever part of it you
say back.

**Read-backs.** A transcript the recognizer was unsure about, one matching
`delivery.confirm_if_matches`, or one that sounds like it was a question *for voice-loop* — a
short question naming the queue, the windows or what was just said — is read back to you before
it is sent: *"No sé si eso era para mí. Dijiste: cuántas ventanas quedan abiertas. ¿Te lo mando a
la ventana?"* Say "dale" to send it, "no" to drop it, or just say something else — that replaces
it. The heuristic is deliberately narrow, and both halves have to hold: this is not a read-back
on everything you say, which would be worse than the problem. "cuántos tests corriste" goes
straight through.

**The queue, out loud.** "dame los pendientes" works from anywhere, busy mode included, where the
hotkey is the only microphone you get. It reads the list in the order things arrived and then
waits for a pick — "la dos", or the window's name. The window you pick is served **at once**, with
no heads-up and no second "dámelo": you just said which one you wanted. The one you were on stays
pending, reachable, and in its place in line. Any item whose summary is missing is summarised as
the list is read, so no entry is ever just a name — and windows that have closed since are
dropped before the list is read at all.

## Naming windows out loud

Claude names an unnamed window after its directory and two hex characters — `darwin-21`,
`darwin-ae`. Spoken, three of those in one repo are three identical noises. So the first time
such a window announces itself you hear *"Nuevo evento de darwin 21"* — the autogenerated name
is a bad name, but it is the name, and the heads-up has no room for anything else. The offer
comes with the summary, when you ask for it:

> *"Terminó los tests del event processor. ¿La llamo tests event processor?"*

- **"dale"** keeps that name. **A short phrase** — "índice de migración" — keeps yours instead.
- **A yes with the name attached** — "sí, llamala fecha actual" — is read as the yes it is. That
  is how people accept an offer out loud, and the length rule on its own reads it as a sentence:
  on the first real run it declined the name *and* typed the acceptance into the window it was
  accepting for.
- **A yes and something short that could be either** — "dale, mergealo" — is asked about rather
  than guessed: *"No sé si eso era el nombre. Dijiste: mergealo. ¿Es el nombre, o te lo mando a
  la ventana?"* Both readings are ordinary Spanish and guessing costs something either way — one
  stores the window's answer as its name, the other swallows the answer. Say "el nombre" (or
  "sí") and it is the name; say "a la ventana" (or "no") and the offered name stands while the
  phrase is delivered. This is the *only* band that asks: a bare yes, a yes repeating the name
  that was offered, a phrase that says it is a name ("llamala índice"), a yes plus a whole
  sentence, "no" and silence are all unambiguous and never ask you anything.
- **Anything longer that does not start with a yes** was meant for the window, not for the
  question: it goes straight through to that window as your answer, rather than becoming a window
  called "mergealo cuando pasen los tests". Silence goes through too, which ends the turn instead
  of opening a second mic on somebody who is not there. A yes with a sentence after it does both
  — the offered name is kept and the sentence is delivered.
- **"no" or silence is remembered.** You are not asked about that window again — being asked at
  every announcement is worse than `darwin-21`.

A window you named yourself, in Claude's prompt box, is never second-guessed. Names you confirm
become what the announcement says *and* vocabulary for the recognizer, so saying the name of a
window transcribes as the name and not as three unrelated words.

The proposal costs nothing extra: it is one more field on the `gpt-4o-mini` request that was
already summarising that turn — two readings of the same paragraph, one call. Both are stored,
so an item you postpone and come back to an hour later still has its name to offer without
paying for the paragraph twice. If the model is down you lose the offer and keep the summary.

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
- **Summaries are computed on arrival**, off the loop, so "dámelo" answers at once instead of
  going quiet for five seconds in the middle of a sentence you started. Once per item: the
  fallback is not stored, so a provider that is down is not retried four times a second. A
  superseded item loses its summary on purpose — it described a turn that is no longer the last
  one — and gets a fresh one on arrival of the turn that replaced it, or when the list is read.
- **An event whose window has closed is dropped, not announced.** There is nobody left to answer
  it, and listing it in `pendings` is an errand you can never run. Checked before announcing,
  before reading the list, and on a five-second timer — tabs close while you work, and say
  nothing when they do. An unreadable roster means "I cannot tell", and drops nothing.
- Milestones (a PR being created) only chime. If some other tool of yours already tracks a
  per-terminal phase in a file, point `integrations.milestone_file_watch` at it — off by default.
- **The chime and the voice overlap on purpose.** A chime is an attack and then a tail that says
  nothing — `Ping` rings for 1.5 s — and waiting for `afplay` to exit before starting `say` (which
  takes ~0.5 s to open its mouth) put about two seconds of silence between the cue and the
  sentence, every time. The voice now starts a quarter of a second in, under the tail. Two
  *different* announcements never overlap: the lock is held across the pair, so the chime is also
  waited out before the next item gets the speaker.

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
- [x] Phase 3 — naming unnamed windows by voice, the queue and the board read out loud, lazy
      re-summarising of superseded items, the microphone grant made an install step
- [ ] Next — Deepgram streaming with server-side endpointing, instead of the local silence cutoff
