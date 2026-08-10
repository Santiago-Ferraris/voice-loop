"""The daemon — the only process that owns state, and the only one that talks.

Four cooperating loops:

* **ingest** (250 ms) drains the spool into SQLite, and starts a summary for
  whatever just arrived. Cheap, and the only thing that has to keep up with
  fifteen sessions firing hooks at once.
* **announce** walks the queue in FIFO order and announces the first item that
  is actually ready. "Ready" is where the subagent gate lives: a turn that
  ended with background agents still running is skipped — silently, keeping
  its place — until they finish.
* **reconcile** (5 s) drops whatever belongs to a window that has closed.
* **milestones** (optional) polls external phase files for chime-only events.

Everything slow runs off the loop: transcript parsing, the summary call, the
transcription call and every AppleScript go through `to_thread`, and speech is
serialized behind the speaker's own lock.

**The announcement is a heads-up, not a report.** A window that blocks gets a
chime and its name — "Nuevo evento de inbox realtime" — and then four seconds
of microphone, and that is the entire interruption. "dámelo" gets you the
summary and the mic to answer it with; "después" sends it to the back of the
line; saying nothing leaves it in the queue and closes the mic. Nothing is ever
announced twice and nothing is ever reminded about: an ignored window waits in
`pendings` until you ask for it, silently, for as long as that takes.

Everything that can end a cycle leaves the item in `pendings`; nothing is ever
dropped for having been ignored. The one thing that *is* dropped is an event
whose window has closed — there is nobody left to answer it.

Two modes sit on top. **Paused** stops announcing entirely. **Busy** is
silence: no chime, no voice, no microphone, and the queue simply grows; on the
way out it tells you how much did. The hotkey keeps working in busy, because
"I am in a meeting" and "I cannot answer you" are different things.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import inspect
import logging
import logging.handlers
import os
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

from . import (
    __version__,
    announce as announce_mod,
    classify as classify_mod,
    delivery as delivery_mod,
    echo,
    envfile,
    intents,
    iterm,
    naming,
    output as output_mod,
    preflight,
    roster as roster_mod,
    spoken,
    spool,
    vocabulary,
)
from .audio import REASON_TIMEOUT, AudioUnavailable, MicConsentPending, Recorder
from .config import Config, ConfigError, load as load_config
from .control import ControlError, ControlServer, DaemonAlreadyRunning
from .delivery import Delivery, GatePolicy
from .events import (
    TYPE_MENU,
    TYPE_MILESTONE,
    TYPE_NOTIFICATION,
    TYPE_STOP,
    Event,
    is_idle_notification,
)
from .milestones import MilestoneWatcher
from .store import Item, Store
from .stt import SttError, SttNotImplemented, Transcript, create as create_stt
from .summarize import FALLBACK_SUMMARY, Summarizer
from .transcript import pending_subagents, tail_text
from .tts import Speaker

INGEST_INTERVAL = 0.25
ANNOUNCE_INTERVAL = 0.2
MILESTONE_INTERVAL = 1.0
# The vocabulary is derived from months of transcripts; checking whether it is
# due costs a stat, and recomputing it is minutes-old-at-worst either way.
VOCABULARY_INTERVAL = 300.0
# Windows close all the time and say nothing about it. Often enough that
# `pendings` never lists a dead one, rarely enough to be a few JSON reads.
RECONCILE_INTERVAL = 5.0

RESOLVED_BY_MILESTONE = "milestone"
RESOLVED_BY_BACKGROUND = "background-session"
RESOLVED_BY_GONE = "session-gone"
RESOLVED_BY_SKIP = "skip"

KV_PAUSED = "paused"
KV_BUSY = "busy"

# Said out loud, not logged: a mic that never opens looks exactly like a daemon
# that stopped caring, and the log is the last place anyone will look.
MIC_CONSENT_SPOKEN = (
    "No pude abrir el micrófono: falta el permiso de micrófono para el daemon. "
    "Corré voice loop control doctor en una terminal."
)

# Short on purpose: it is said after a list that already took twenty-five
# seconds, and its whole job is to be the difference between "I did not
# understand that" and a daemon that has stopped answering.
NO_PICK_SPOKEN = "No te entendí. Decime el número o el nombre."

# The other half of not understanding: the instruction was plain, the window it
# was about was not. Guessing right seven times out of ten is ten sentences
# typed into the wrong session a day, and one question costs a second.
WHICH_WINDOW_SPOKEN = "¿A cuál?"

# How many windows one "dame los pendientes" chain may hop through before the
# daemon stops following it. Not a limit on you — the hotkey starts a new chain.
MAX_SWITCHES = 5

# One reply cycle ends this way.
REPLY_DELIVERED = "delivered"
REPLY_PENDING = "pending"
REPLY_FAILED = "failed"

# And this is what the four seconds after a heads-up came back with.
ALERT_GIVE = "give"          # "dámelo": read it out and open the mic on it
ALERT_LATER = "later"        # "después": to the back of the line
ALERT_ANSWER = "answer"      # a sentence, confirmed: it was the answer already
ALERT_NONE = "none"          # silence, "salteá", or a question asked and answered

log = logging.getLogger("voiceloop")


def setup_logging(log_dir: Path, *, level: int = logging.INFO) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "daemon.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
    )
    root = logging.getLogger("voiceloop")
    root.setLevel(level)
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)


class Daemon:
    def __init__(
        self,
        config: Config,
        *,
        store: Store | None = None,
        speaker: Speaker | None = None,
        summarizer: Summarizer | None = None,
        watcher: MilestoneWatcher | None = None,
        roster_dir: Path | str | None = None,
        recorder: Recorder | None = None,
        stt: Any = None,
        delivery: Delivery | None = None,
        output: output_mod.OutputProbe | None = None,
        classifier: classify_mod.Classifier | None = None,
    ):
        self.config = config
        self.state_dir = config.state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        spool.ensure_dirs(config.spool_dir)

        self.store = store or Store(config.db_path)
        self.speaker = speaker or Speaker.from_config(config)
        self.summarizer = summarizer or Summarizer.from_config(config)
        self.watcher = watcher or MilestoneWatcher.from_config(config)
        self.roster_dir = roster_dir

        self.recorder = recorder or Recorder.from_config(config)
        self.delivery = delivery or Delivery()
        self.gate = GatePolicy.from_config(config)
        self.stt = stt if stt is not None else self._build_stt(config)

        self.phonetic = config.get("text_to_speech.phonetic") or {}
        self.blocking_chime = config.get("announce.blocking_chime")
        self.milestone_chime = config.get("announce.milestone_chime")
        self.mic_open_chime = config.get("announce.mic_open_chime")
        self.mic_close_chime = config.get("announce.mic_close_chime")
        self.busy_chime = config.get("announce.busy_chime")
        self.notification_events = bool(config.get("announce.notification_events", True))

        self.mic_enabled = bool(config.get("microphone.enabled", True))
        self.keep_recordings = bool(config.get("microphone.keep_recordings", False))
        self.mic_rounds = max(1, int(config.get("delivery.max_mic_rounds", 3)))
        # How long the mic stays open after the last word, not after the chime.
        self.mic_grace = float(config.get("announce.mic_grace_seconds", 10))
        self.output = output or output_mod.OutputProbe.from_config(config)
        self.classifier = classifier or classify_mod.Classifier.from_config(config)
        self.new_tab_command = str(config.get("windows.new_tab_command", "") or "")
        self.vocabulary_enabled = bool(config.get("vocabulary.enabled", True))
        self.vocabulary_pattern = str(
            config.get("vocabulary.transcripts", vocabulary.DEFAULT_TRANSCRIPT_GLOB)
        )
        self.vocabulary_max_age = float(config.get("vocabulary.refresh_hours", 6)) * 3600
        self.vocabulary_limit = int(config.get("vocabulary.limit", vocabulary.DEFAULT_LIMIT))
        self.vocabulary_min_count = int(
            config.get("vocabulary.min_count", vocabulary.DEFAULT_MIN_COUNT)
        )
        self.plan_feedback_index = int(
            config.get("delivery.plan_menu.feedback_index", delivery_mod.PLAN_FEEDBACK)
        )

        self.started_at = time.time()
        self.paused = bool(self.store.kv_get(KV_PAUSED, False))
        self.busy = bool(self.store.kv_get(KV_BUSY, False))
        self._stop = asyncio.Event()
        self._restart = False
        self._gate_cache: dict[str, tuple[tuple[int, int], int]] = {}
        self._server: ControlServer | None = None
        self._mic_lock = asyncio.Lock()
        self._mic_stop: asyncio.Event | None = None
        self._mic_tasks: set[asyncio.Task] = set()
        self._switch_to: str | None = None
        # What the rest of one breath asked for, run once the thing it asked
        # for first has finished. See `_queue_actions`.
        self._afterwards: list[classify_mod.Action] = []
        # Items a summary has already been started for. Once each: the fallback
        # is not stored, so "has no summary" stays true for an item the
        # provider could not answer about, and retrying it every 250 ms is a
        # loop rather than a retry.
        self._summarised: set[str] = set()

    @staticmethod
    def _build_stt(config: Config):
        """A provider that is planned but not written must not silently degrade."""
        try:
            return create_stt(config)
        except SttNotImplemented as exc:
            log.error("speech-to-text disabled: %s", exc)
            return None

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> int:
        self._server = ControlServer(self.config.socket_path, self.dispatch)
        await self._server.start()
        log.info("voice-loop %s listening on %s", __version__, self.config.socket_path)

        self.reconcile()
        self.watcher.baseline()

        tasks = [
            asyncio.create_task(self._ingest_loop(), name="ingest"),
            asyncio.create_task(self._announce_loop(), name="announce"),
            asyncio.create_task(self._reconcile_loop(), name="reconcile"),
            asyncio.create_task(self._milestone_loop(), name="milestones"),
            asyncio.create_task(self._vocabulary_loop(), name="vocabulary"),
        ]
        try:
            await self._stop.wait()
        finally:
            for task in (*tasks, *self._mic_tasks):
                task.cancel()
            for task in (*tasks, *self._mic_tasks):
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await self._server.close()
            self.store.close()
        log.info("stopped (restart=%s)", self._restart)
        return 0

    def stop(self, restart: bool = False) -> None:
        self._restart = restart
        self._stop.set()

    def reconcile(self) -> None:
        """Startup housekeeping against the roster and any half-done announce."""
        recovered = self.store.recover_in_flight()
        gone = self.sweep_gone()
        if recovered or gone:
            log.info("reconciled: %d requeued, %d resolved as gone", recovered, gone)

    def _live_sessions(self) -> dict:
        try:
            return roster_mod.load(self.roster_dir)
        except OSError:
            return {}

    def sweep_gone(self) -> int:
        """Resolve everything whose window has closed — live, not only at startup.

        A tab that closed, by hand or otherwise, takes its events with it: there
        is nobody left to answer them, so announcing one is talking to an empty
        room and listing one in `pendings` is an errand you can never run. Run
        before announcing, before reading the list, and on a timer, because a
        window can close at any of those moments and says nothing when it does.

        An empty roster is not "everything is gone", it is "I cannot tell" —
        that is the one case where nothing is dropped.
        """
        live = self._live_sessions()
        if not live:
            return 0
        gone = self.store.resolve_sessions_missing(live.keys(), RESOLVED_BY_GONE)
        if gone:
            log.info("dropped %d item(s) whose window is gone", gone)
        return gone

    # -- loops -------------------------------------------------------------

    async def _ingest_loop(self) -> None:
        while True:
            try:
                self.ingest_once()
                self.prefetch_summaries()
            except Exception:  # noqa: BLE001 - a bad event must not kill ingest
                log.exception("ingest failed")
            await asyncio.sleep(INGEST_INTERVAL)

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(RECONCILE_INTERVAL)
            try:
                self.sweep_gone()
            except Exception:  # noqa: BLE001
                log.exception("reconcile failed")

    def ingest_once(self) -> int:
        count = 0
        for path, event in spool.read_pending(self.config.spool_dir):
            if self._muted(event):
                log.debug("muted idle notification from %s", event.session_id[:8])
                spool.discard([path])
                continue
            try:
                outcome = self.store.ingest(event)
            except Exception:  # noqa: BLE001 - quarantine rather than replay forever
                log.exception("could not ingest %s", path.name)
                spool.quarantine(path, "ingest failed")
                continue
            log.debug("ingest %s %s -> %s", event.type, event.session_id[:8], outcome)
            spool.discard([path])
            count += 1
        return count

    def prefetch_summaries(self) -> int:
        """Start a summary for anything waiting without one, in the background.

        The heads-up says a name and stops, so "dámelo" is the moment the
        summary is needed — and computing it *then* is five seconds of silence
        in the middle of a sentence you started. It is computed when the item
        arrives instead, off the loop, and read out of the row later. A window
        you never ask about costs one call it would have cost anyway the first
        time you did.

        Once per item, deliberately: a provider that is down returns the
        fallback, which is not stored, so "has no summary" stays true and a
        retry here would be a loop.
        """
        started = 0
        for item in self.store.pendings():
            if item.type != TYPE_STOP or item.summary or not item.transcript_path:
                continue
            if item.id in self._summarised:
                continue
            self._summarised.add(item.id)
            self._spawn(self._prefetch_one(item.id))
            started += 1
        return started

    async def _prefetch_one(self, item_id: str) -> None:
        item = self.store.get(item_id)
        if item is None or item.summary:
            return
        try:
            await self._summary_and_slug(item, self._session_for(item))
        except Exception:  # noqa: BLE001 - a background task must not die silently
            log.exception("could not pre-summarise %s", item_id[:8])

    def _muted(self, event: Event) -> bool:
        """Dropped on arrival, not announced quietly.

        `notification_events: false` used to mean chime-only, and a chime every
        time is the same interruption without the words. It now means silence,
        and silence has to start here: an event that reaches the queue is a
        pendiente — it is counted in "quedan dos", it comes back when you ask
        what is waiting, and it holds a slot the announce loop keeps checking.

        Only the idle nudge. A permission prompt is a window that cannot move
        until you answer it, and so is anything whose wording we do not know.
        """
        return (
            event.type == TYPE_NOTIFICATION
            and not self.notification_events
            and is_idle_notification(event.payload.get("message"))
        )

    async def _announce_loop(self) -> None:
        while True:
            try:
                await self.announce_next()
            except Exception:  # noqa: BLE001 - never let the announcer die
                log.exception("announce failed")
            await asyncio.sleep(ANNOUNCE_INTERVAL)

    async def _vocabulary_loop(self) -> None:
        while True:
            try:
                await self.refresh_vocabulary()
            except Exception:  # noqa: BLE001
                log.exception("vocabulary refresh failed")
            await asyncio.sleep(VOCABULARY_INTERVAL)

    async def _milestone_loop(self) -> None:
        while True:
            try:
                for milestone in list(self.watcher.poll()):
                    log.info("milestone %s: %s", milestone.key, milestone.label)
                    self.store.ingest(
                        Event.new(
                            TYPE_MILESTONE,
                            session_id="",
                            payload={"label": milestone.label, "source": milestone.key},
                        )
                    )
            except Exception:  # noqa: BLE001
                log.exception("milestone watch failed")
            await asyncio.sleep(MILESTONE_INTERVAL)

    # -- announcing --------------------------------------------------------

    async def announce_next(self) -> bool:
        """Announce the first item that is ready. Busy is silence, not a chime.

        Busy stops here rather than further down: an item that is never
        announced stays `queued`, so nothing is missed and nothing is heard —
        the whole meeting arrives at once when you come back out.
        """
        if self.paused or self.busy:
            return False
        live = self._live_sessions()
        for item in self.store.queued_items():
            session = live.get(item.session_id) if item.session_id else None
            if item.session_id and live and session is None:
                # That window is closed. Announcing it is talking to a room
                # with nobody in it — and the answer would have nowhere to go.
                self.store.resolve(item.id, RESOLVED_BY_GONE)
                log.info("dropped %s: window %s is gone", item.id[:8], item.session_id[:8])
                continue
            if session is not None and not session.is_interactive:
                # Background agents have no window to answer in.
                self.store.resolve(item.id, RESOLVED_BY_BACKGROUND)
                log.info("skipped bg session %s", item.session_id[:8])
                continue
            if not await self._ready(item):
                continue
            await self._announce(item, session)
            return True
        return False

    def _session_for(self, item: Item):
        if not item.session_id:
            return None
        try:
            return roster_mod.find(item.session_id, self.roster_dir)
        except OSError:
            return None

    async def _ready(self, item: Item) -> bool:
        """A stop with background agents still running is not the user's turn yet."""
        if item.type != TYPE_STOP or not item.transcript_path:
            return True
        pending = await asyncio.to_thread(self._gated_count, item.transcript_path)
        if pending > 0:
            log.debug("gate: %d subagents in flight for %s", pending, item.id[:8])
            return False
        return True

    def _gated_count(self, transcript_path: str) -> int:
        try:
            stat = os.stat(transcript_path)
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return 0
        cached = self._gate_cache.get(transcript_path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        count = pending_subagents(transcript_path)
        self._gate_cache[transcript_path] = (stamp, count)
        return count

    def _name_for(self, item: Item, session) -> str:
        alias = self.store.get_alias(item.session_id) if item.session_id else None
        if alias:
            return alias
        if session is not None and session.name:
            return session.name
        if item.name:
            return item.name
        if item.cwd:
            return os.path.basename(item.cwd.rstrip("/")) or "una sesión"
        return item.session_id[:8] or "una sesión"

    async def _summary_for(self, item: Item) -> str | None:
        if item.type != TYPE_STOP:
            return None
        if item.summary:
            return item.summary
        tail = await asyncio.to_thread(tail_text, item.transcript_path)
        summary = await asyncio.to_thread(self.summarizer.summarize, tail)
        if summary != FALLBACK_SUMMARY:
            # The fallback is not a summary, it is the absence of one. Storing
            # it would make "terminó y te espera" permanent for an item whose
            # only problem was that the key was missing for five seconds.
            self.store.set_summary(item.id, summary)
        return summary

    async def summarize_missing(self, items: Sequence[Item]) -> dict[str, str]:
        """Fill in the summaries that were dropped when a `stop` was superseded.

        Issue #3: superseding clears the summary — it described a turn that is
        no longer the last one — and nothing recomputed it, so `pendings` ended
        up with ten items and not one word about any of them. The command whose
        entire job is telling you which window wants you said nothing.

        Recomputed **on read**, deliberately: the announce path stays free of
        extra latency, and nothing is spent summarising items you never ask
        about. The alternative — recompute in the background on every supersede
        — pays OpenAI for turns that get superseded again thirty seconds later,
        which is what got it rejected in PR #2 to begin with.

        Concurrently, because ten stale items at five seconds each is not a
        list, it is a hang.
        """
        stale = [
            item
            for item in items
            if item.type == TYPE_STOP and not item.summary and item.transcript_path
        ]
        if not stale:
            return {}
        log.info("recomputing %d missing summar%s", len(stale), "y" if len(stale) == 1 else "ies")
        filled = await asyncio.gather(*(self._summary_for(item) for item in stale))
        return {item.id: text for item, text in zip(stale, filled) if text}

    async def _announce(
        self, item: Item, session, *, depth: int = 0, heads_up: bool = True
    ) -> None:
        """Chime, name the window, and take one short answer about what to do.

        `heads_up=False` serves an item you asked for by name — picked off the
        pendings list, or handed to the hotkey. You already know which one it
        is and you have already said you want it; announcing it again and
        waiting for "dámelo" would be making you ask twice.
        """
        self.store.mark_announcing(item.id)
        name = self._name_for(item, session)
        self.store.set_name(item.id, name)

        if item.type == TYPE_MILESTONE:
            # Chime only — there is nothing for the user to answer.
            await self.speaker.announce(
                announce_mod.alert(item, name=name, milestone_chime=self.milestone_chime)
            )
            self.store.resolve(item.id, RESOLVED_BY_MILESTONE)
            return

        answer, carried = ALERT_GIVE, None
        if heads_up:
            alert = announce_mod.alert(
                item,
                name=name,
                phonetic=self.phonetic,
                blocking_chime=self.blocking_chime,
                notification_events=self.notification_events,
            )
            log.info("alert %s [%s] %s", item.type, name, alert.text)
            if alert.silent or not self.can_listen() or not item.tty:
                await self.speaker.announce(alert)
                self.store.mark_pending(item.id)
                if alert.silent:
                    return
                if not self.can_listen():
                    # A heads-up only works because "dámelo" is available. With
                    # no recognizer there is no way to ask, so the summary comes
                    # anyway rather than leaving you with a name and no
                    # recourse.
                    await self._say_detail(item, session)
                return
            # The mic opens *with* the chime and outlives the sentence, so an
            # answer that starts before the name is over is still an answer.
            self.store.mark_pending(item.id)
            answer, carried = await self._ask_what_to_do(item, alert)
        else:
            self.store.mark_pending(item.id)

        if answer == ALERT_ANSWER:
            outcome = await self.reply_cycle(item, "", first=carried)
        elif answer == ALERT_GIVE:
            outcome = await self._give(item, session)
        else:
            # "después", "salteá", silence, or a question that has been
            # answered: the item is in `pendings` and nothing else is owed.
            await self._follow_switch(depth)
            return

        if outcome == REPLY_DELIVERED:
            await self.speak_remaining()
        await self._follow_switch(depth)

    async def _ask_what_to_do(self, item: Item, alert) -> tuple[str, Transcript | None]:
        """Say "Nuevo evento de X" into an open microphone, and take the answer.

        The announcement is spoken from inside the take, not before it, so the
        answer may have been said over the top of it. Every later round is a
        plain mic with the same grace.

        Two words end it — "dámelo" or "después" — and saying nothing is the
        third answer: you were not there, the item stays in the queue, and
        nothing will bring it up again until you ask. Every other control
        phrase works too, because the mic being open is not a promise about
        what you may say into it: "estado", "pendientes", "mostrame".

        A **sentence** is the one thing that is not acted on. Nothing has been
        read out yet, so a sentence here is as likely to be a word to somebody
        in the room as an answer for a window nobody has heard about — and
        typing it into somebody's session is the failure this whole read-back
        machinery exists to avoid. So it is read back and asked about.
        """
        gate = delivery_mod.Gate(
            True, "No sé si eso era para mí", "¿Te lo mando a la ventana?"
        )
        said = alert.text
        carried: Transcript | None = None
        follow: Transcript | None = None
        queued: list[classify_mod.Action] = []
        rounds = 0
        async with self._mic_lock:
            while queued or rounds < self.mic_rounds:
                if not queued:
                    if rounds == 0:
                        transcript = await self.say_and_listen(announcement=alert)
                    elif follow is not None:
                        transcript, follow = follow, None
                    else:
                        transcript = await self.listen(timeout=self.mic_grace)
                    rounds += 1
                    if transcript is None:
                        return ALERT_NONE, None
                    plan = await self.classify(transcript)
                    queued = list(plan.actions)
                    guess = plan.guess
                else:
                    guess = None
                more_rounds = rounds < self.mic_rounds
                action = queued.pop(0)
                intent = action.as_intent()

                if guess is not None and carried is None:
                    # Not "was that for the window?" — it was plainly for me,
                    # and only the recognizer disagrees. Ask by name, before
                    # anything is acted on.
                    question = announce_mod.speakable(
                        announce_mod.near_miss_question(transcript.text, guess.kind),
                        self.phonetic,
                    )
                    answer = await self._say_then(question, listening=more_rounds)
                    if answer is not None and intents.parse(answer.text).kind == (
                        intents.KIND_CONFIRM
                    ):
                        queued = [guess]
                        follow = None
                        guess = None
                        continue
                    follow = answer
                    queued = []
                    continue

                if carried is not None:
                    if intent.kind == intents.KIND_CONFIRM:
                        return ALERT_ANSWER, carried
                    if intent.kind == intents.KIND_CANCEL:
                        await self.speaker.speak("Listo, no mando nada.")
                        return ALERT_NONE, None
                    carried = None  # a new utterance replaces the old one

                if intent.kind in (intents.KIND_GIVE, intents.KIND_CONFIRM):
                    # Whatever else the same breath asked for runs once the
                    # summary and its own microphone are done with.
                    self._queue_actions(queued)
                    return ALERT_GIVE, None
                if intent.kind == intents.KIND_LATER:
                    self.store.defer(item.id)
                    log.info("deferred %s [%s]", item.id[:8], item.display_name)
                    self._queue_actions(queued)
                    return ALERT_LATER, None
                if intent.kind in (
                    intents.KIND_SILENCE,
                    intents.KIND_SKIP,
                    intents.KIND_CANCEL,
                ):
                    self._queue_actions(queued)
                    return ALERT_NONE, None
                if intent.kind == intents.KIND_REPEAT:
                    follow = await self._say_then(said, listening=more_rounds and not queued)
                    continue
                if intent.kind == intents.KIND_WAIT:
                    follow = await self._say_then(
                        "Dale, espero.", listening=more_rounds and not queued
                    )
                    continue
                if intent.kind == intents.KIND_SHOW:
                    if item.tty:
                        await self._safely(self.delivery.focus, item.tty)
                    continue
                if intent.kind in (intents.KIND_OPEN, intents.KIND_TELL):
                    await self._perform_side(action)
                    continue
                if intent.kind == intents.KIND_STATUS:
                    follow = await self._speak_status_and_listen(
                        listening=more_rounds and not queued
                    )
                    continue
                if intent.kind == intents.KIND_PENDINGS:
                    chosen = await self.speak_pendings()
                    if chosen is None:
                        continue
                    self._queue_actions(queued)
                    if chosen.id == item.id:
                        return ALERT_GIVE, None
                    # Served once this cycle has unwound; see `_follow_switch`.
                    self._switch_to = chosen.id
                    return ALERT_NONE, None

                carried = dataclasses.replace(transcript, text=intent.text or transcript.text)
                follow = await self._say_then(
                    self._readback_sentence(gate, carried.text), listening=more_rounds
                )
        return ALERT_NONE, None

    async def _say_detail(
        self, item: Item, session, *, listening: bool = False
    ) -> tuple[Item, str, str, Transcript | None]:
        """Speak what that window wants.

        Returns (the item as it is now, what was said, the name to offer, and —
        when `listening` — whatever was said back into the mic that was open
        the whole time it was talking).
        """
        # Re-read: the summary was very likely written by the background
        # prefetch after this item came off the queue.
        current = self.store.get(item.id) or item
        summary, slug = await self._summary_and_slug(current, session)
        text = announce_mod.detail(
            current, summary=summary, phonetic=self.phonetic, naming_offer=slug
        )
        log.info("detail [%s] %s", current.display_name, text)
        if not listening:
            await self.speaker.speak(text)
            return current, text, slug, None
        heard = await self.say_and_listen(text=text)
        return current, text, slug, heard

    async def _give(self, item: Item, session) -> str:
        """"dámelo": what that window wants, said into an already-open mic.

        The summary and the naming offer are one sentence, and the answer to
        them is routinely started before that sentence is over — so the take
        that carries it is the one that ran underneath.
        """
        current, text, slug, heard = await self._say_detail(item, session, listening=True)
        # The naming answer comes first and gets its own take: it was the last
        # thing asked, and whatever is not a name is handed straight on to the
        # window's own reply cycle rather than thrown away.
        if slug:
            overheard = await self._settle_name(current, slug, first=heard)
        else:
            overheard = heard
        return await self.reply_cycle(current, text, first=overheard)

    async def speak_remaining(self) -> str:
        """"Quedan dos" — after you answer, which is the only moment it means it.

        In the announcement it landed before there was anything to count down
        from, and on a window being offered a name it wedged itself between the
        summary and the question: "No hay expectativa… Queda uno. ¿La llamo
        fecha actual?" is what made the user ask which one was left.
        """
        phrase = announce_mod.remaining_phrase(self.store.queued_count())
        if phrase:
            await self.speaker.speak(f"{phrase}.")
        return phrase

    # -- naming windows -----------------------------------------------------

    def _wants_a_name(self, item: Item, session) -> bool:
        """Only an unnamed window, only once, and only if you could answer.

        Claude's own `<dir>-<2 hex>` is the tell: the user never named this one.
        A window you named yourself — in Claude or here — is never second-guessed.
        """
        return (
            item.type == TYPE_STOP
            and bool(item.session_id)
            and session is not None
            and session.has_autogenerated_name
            and not self.busy
            and self.can_listen()
            and not self.store.alias_asked(item.session_id)
        )

    async def _summary_and_slug(self, item: Item, session) -> tuple[str | None, str]:
        """(summary, name to offer). One request for both, or no request at all.

        The name and the summary come out of the same paragraph, so asking for
        them separately would read the same transcript twice and pay twice.
        Both are stored, and that is what makes the offer survive the wait: the
        summary is normally computed the moment the item arrives, and by the
        time you say "dámelo" — which may be an hour and a "después" later —
        recomputing the pair just to have a name to offer would be a second
        call for an answer that is already in the row.

        An item that was summarised without a name proposal keeps the
        announcement and loses the offer; it comes back next time that window
        blocks.
        """
        wants_a_name = self._wants_a_name(item, session)
        if item.summary:
            return item.summary, (item.slug or "") if wants_a_name else ""
        if not wants_a_name:
            return await self._summary_for(item), ""
        tail = await asyncio.to_thread(tail_text, item.transcript_path)
        result = await asyncio.to_thread(self.summarizer.summarize_and_name, tail)
        if result.text != FALLBACK_SUMMARY:
            self.store.set_summary(item.id, result.text)
            self.store.set_slug(item.id, result.slug)
        return result.text, result.slug

    async def _settle_name(
        self, item: Item, slug: str, *, first: Transcript | None = None
    ) -> Transcript | None:
        """Take one answer to "¿la llamo …?". Returns anything meant elsewhere.

        Three outcomes, and the third is the one that matters: "dale" saves the
        proposal, a short phrase saves that instead, and **anything else** is
        not a name — it is the answer to the window, spoken into the mic that
        happened to be open. That gets handed back so the reply cycle uses it,
        instead of being stored as a window called "mergealo cuando pasen los
        tests".

        A yes is checked before any of that, and it is checked as a *prefix*:
        "sí, llamala fecha actual" is how a person accepts an offer out loud,
        and the length rule alone reads it as a sentence — which is how, on the
        first real run, an acceptance was declined and then typed into the
        window it was accepting for.

        Either way the offer is recorded as made. Being asked to name the same
        window at every announcement is worse than the autogenerated name.
        """
        if first is not None:
            transcript = first
        else:
            async with self._mic_lock:
                transcript = await self.listen()
        if transcript is None:
            # The mic is broken, not the offer refused. Ask again next time.
            return None

        # No menu labels here on purpose: only a `stop` is ever offered a name,
        # so there is no open menu whose options this could be answering.
        intent = intents.parse(transcript.text)
        tail = intents.confirmation_tail(transcript.text)
        if tail is not None:
            named = naming.dictated(tail) if tail else ""
            if self._name_is_doubtful(named, tail, slug):
                return await self._settle_doubtful_name(item, slug, named, transcript, tail)
            await self._save_name(item, named or slug)
            if tail and not named:
                # The yes was real and what followed was not a name: it was the
                # answer to the window, said in the same breath.
                return dataclasses.replace(transcript, text=tail)
            return None

        dictated = (
            naming.slugify(intent.text)
            if intent.kind == intents.KIND_TEXT and naming.is_plausible(intent.text)
            else ""
        )
        if intent.kind == intents.KIND_CONFIRM or dictated:
            await self._save_name(item, dictated or slug)
            return None

        self.store.decline_alias(item.session_id)
        log.info("naming declined for %s (heard %r)", item.session_id[:8], transcript.text)
        if intent.kind == intents.KIND_CANCEL:
            # "no" answers the name, not the window — keep the mic cycle going.
            return None
        # Silence included: handing it on ends the cycle at once instead of
        # opening a second mic on somebody who is not there.
        return transcript

    @staticmethod
    def _name_is_doubtful(named: str, tail: str, slug: str) -> bool:
        """Is "dale, X" the name X, or a yes and then an answer for the window?

        Only this band asks, and it is narrow on purpose — a read-back on
        everything was rejected out loud. Three things have to be true at once:

        * something short enough to be a name came after the yes (a whole
          sentence is not a name, and is already handled);
        * it is **not** the name that was just offered — "sí, llamala fecha
          actual" against an offer of "fecha actual" is agreement, not news;
        * it does **not** say it is a name — "llamala índice" left no doubt.
        """
        return bool(named) and named != slug and not naming.says_it_is_a_name(tail)

    async def _settle_doubtful_name(
        self, item: Item, slug: str, named: str, transcript: Transcript, tail: str
    ) -> Transcript | None:
        """Read the doubt back rather than pick a reading of it.

        Both readings of "dale, mergealo" are ordinary Spanish, and guessing
        costs something either way: one stores the window's answer as its name,
        the other swallows the answer. So it is asked, in the same shape every
        other read-back uses.

        Whatever comes back, the offer is settled: "it is the name" saves it,
        and everything else keeps the name that was offered — the yes was real
        — differing only in what is handed on to the window. Nothing is typed
        into anybody's session without an answer to this question.
        """
        gate = delivery_mod.Gate(
            True, "No sé si eso era el nombre", "¿Es el nombre, o te lo mando a la ventana?"
        )
        async with self._mic_lock:
            answer = await self.say_and_listen(
                text=self._readback_sentence(gate, named)
            )
        if answer is None:
            # The mic broke mid-question. Nothing is stored and nothing is
            # sent: the offer comes back the next time this window blocks.
            return None

        choice = intents.name_or_window(answer.text)
        if choice == intents.ANSWER_NAME:
            await self._save_name(item, named)
            return None

        await self._save_name(item, slug)
        if choice == intents.ANSWER_WINDOW:
            # The phrase itself goes on, with the confidence it was heard with.
            return dataclasses.replace(transcript, text=tail)
        # Anything else replaces it, exactly as it would during a read-back —
        # silence included, which ends the cycle without typing anything.
        return answer

    async def _save_name(self, item: Item, slug: str) -> None:
        self.store.set_alias(item.session_id, slug, confirmed=True)
        self.store.set_name(item.id, slug)
        log.info("named %s -> %r", item.session_id[:8], slug)
        await self.speaker.speak(announce_mod.speakable(f"Listo, {slug}.", self.phonetic))

    # -- listening ---------------------------------------------------------

    def can_listen(self) -> bool:
        return bool(self.mic_enabled and self.stt is not None)

    def keyterms(self) -> list[str]:
        """Everything the recognizer should have heard of before you say it.

        Four sources, and only the first is hand-kept: the `keyterms` list in
        the config, the model names, the vocabulary extracted from your own
        Claude transcripts (see `vocabulary.py` — this is where `mergeado` and
        `lambda` come from), and the names of the windows that exist *right
        now*. The last one is why this is collected per request rather than
        baked into the engine: a session name only transcribes if the
        recognizer has been told it exists, and the names change every time you
        open a tab.

        The model names are the one thing the extraction cannot give you.
        `opus` was said five times in the whole history against a cutoff of
        nine hundred — rank 6735 of a list that keeps eighty — because it is a
        word you *say* to a machine, not one you type. So "opus 4.8" came back
        as `opu cuatro punto ocho`, and it is pinned here instead.
        """
        configured = self.config.get("keyterms") or []
        terms = [str(term) for term in configured] if isinstance(configured, (list, tuple)) else []
        terms.extend(spoken.MODEL_NAMES)
        if self.vocabulary_enabled:
            terms.extend(vocabulary.load(self.state_dir))
        try:
            sessions = roster_mod.load(self.roster_dir)
        except OSError:
            sessions = {}
        for session in sessions.values():
            if session.name:
                terms.append(session.name)
        terms.extend(self.store.aliases())
        return terms

    async def refresh_vocabulary(self, *, force: bool = False) -> list[str]:
        """Recompute the extracted vocabulary if it is stale. Off the loop.

        Never fatal and never blocking: a machine with no transcripts, or a
        read that fails halfway, leaves the config list and the window names
        doing exactly what they did before this existed.
        """
        if not self.vocabulary_enabled:
            return []
        now = time.time()
        if not force and vocabulary.age_seconds(self.state_dir, now=now) < self.vocabulary_max_age:
            return []
        try:
            return await asyncio.to_thread(
                vocabulary.refresh,
                self.state_dir,
                now=now,
                pattern=self.vocabulary_pattern,
                min_count=self.vocabulary_min_count,
                limit=self.vocabulary_limit,
            )
        except Exception:  # noqa: BLE001 - vocabulary is a nicety, not a dependency
            log.exception("could not refresh the vocabulary")
            return []

    async def listen(self, *, timeout: float | None = None) -> Transcript | None:
        """One take with nothing to say first: chime, record, transcribe.

        `None` means the mic itself failed. The take starts by taking the
        floor, so the whole sequence is exactly this and nothing may interleave
        with it: **floor -> mic open -> "speak now" chime -> floor released ->
        record**. Opening under a voice recorded the announcement and left the
        cue queued behind it.
        """
        return await self._take(timeout=timeout)

    async def say_and_listen(
        self,
        *,
        announcement=None,
        text: str = "",
        grace: float | None = None,
    ) -> Transcript | None:
        """Speak with the microphone already open.

        This is the whole of the v3 microphone. The chime no longer means
        "your turn now" — it means "I am about to say something *and* you can
        talk to me". Capture starts before the first syllable, runs under the
        entire sentence, and waits `grace` seconds afterwards for you to start.
        Start speaking late and the take waits for you; start speaking early —
        over us, on headphones — and the sentence stops mid-word.

        `grace` is time to start, not time to talk: once you have said a word
        the take ends when you stop, not on this clock.

        The old shape opened a four-second window *after* the announcement,
        which is three chances a morning to miss somebody who was still drawing
        breath.
        """
        return await self._take(
            announcement=announcement,
            text=text,
            timeout=self.mic_grace if grace is None else grace,
        )

    async def _take(
        self,
        *,
        announcement=None,
        text: str = "",
        timeout: float | None = None,
    ) -> Transcript | None:
        speaking = announcement is not None or bool((text or "").strip())
        if not self.can_listen():
            # No recognizer: there is still something to say, and saying it is
            # the part that must not depend on being able to hear an answer.
            if announcement is not None:
                await self.speaker.announce(announcement)
            elif text:
                await self.speaker.speak(text)
            return None

        said = announcement.text if announcement is not None else (text or "")
        private = await self.headphones() if speaking else False
        path = self.state_dir / "mic" / f"{uuid.uuid4().hex}.wav"
        stop = asyncio.Event()
        self._mic_stop = stop
        barged = asyncio.Event()

        try:
            # `__aexit__` runs before any handler below, so the floor is long
            # gone by the time one of them tries to speak.
            async with self.speaker.floor() as floor:

                async def open_the_mic() -> None:
                    """What is said while the take is already running."""
                    if not speaking:
                        await floor.cue(self.mic_open_chime)
                        return
                    watching = (
                        asyncio.ensure_future(self._cut_the_voice(barged))
                        if private
                        else None
                    )
                    try:
                        if announcement is not None:
                            await floor.announce(announcement)
                        else:
                            await floor.play(self.mic_open_chime)
                            await floor.say(text)
                    finally:
                        if watching is not None:
                            watching.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await watching
                        floor.release()

                recording = await self.recorder.record(
                    path,
                    stop=stop,
                    on_open=open_the_mic,
                    speech_timeout=timeout,
                    # Barge-in only where our own voice cannot reach the mic.
                    speech=barged if private else None,
                    # On speakers everything the mic hears while `on_open` is
                    # running is us — the sentence, and the cue chime of a mic
                    # that had nothing to say. Neither is you starting to talk,
                    # and now that the cutoff waits for you to *stop*, counting
                    # the chime would end the take three seconds after it rang.
                    arm_after_open=not private,
                )
        except MicConsentPending as exc:
            # The one mic failure with a fix only the user can perform — and
            # they are not looking at a terminal, which is the whole premise.
            log.error("microphone consent pending: %s — %s", exc, preflight.CONSENT_TIMEOUT)
            await self.speaker.speak(MIC_CONSENT_SPOKEN)
            return None
        except AudioUnavailable as exc:
            log.error("microphone unavailable: %s", exc)
            await self.speaker.speak("No pude abrir el micrófono.")
            return None
        finally:
            self._mic_stop = None
        # Always, and always a different sound from the one that opened it:
        # a mic that closes in silence is a mic you cannot tell is still open.
        await self.speaker.chime(self.mic_close_chime)

        if not recording.usable:
            # "Nothing" is now a statement about the audio, not about the clock:
            # a take only gets here empty or measurably silent.
            log.info("mic heard nothing (%s)", recording.summary)
            self._discard(path)
            return Transcript(text="", provider=getattr(self.stt, "name", ""))
        if recording.reason == REASON_TIMEOUT:
            # The window closed on its own with audio inside it — the take that
            # used to be thrown away as "heard nothing". The measured level is
            # here so a mic problem can be told apart from a quiet room.
            log.info("mic ran out of time with audio in it (%s)", recording.summary)
        try:
            transcript = await asyncio.to_thread(self.stt.transcribe, path, self.keyterms())
        except SttError as exc:
            log.error("transcription failed: %s", exc)
            await self.speaker.speak("No pude transcribir lo que dijiste.")
            return None
        finally:
            self._discard(path)
        log.info("heard %r (confidence=%s)", transcript.text, transcript.confidence)
        return self._as_written(self._without_echo(transcript, said))

    def _as_written(self, transcript: Transcript) -> Transcript:
        """Model names and version numbers, spelled the way they are written.

        `numerals=false` means every number arrives in words, which is right
        for everything except the one place a number is a *name*: `opu cuatro
        punto ocho` is `opus 4.8`, and Claude cannot read the first one. See
        `spoken.py` for why nothing else is touched.
        """
        written = spoken.normalize(transcript.text)
        if written == transcript.text:
            return transcript
        log.info("spelled out: %r -> %r", transcript.text, written)
        return dataclasses.replace(transcript, text=written)

    def _without_echo(self, transcript: Transcript, said: str) -> Transcript:
        """Take our own voice out of what came back.

        On speakers the take contains the announcement, word for word, and a
        recognizer has no way to know it was not you. We do: we know exactly
        what `say` was given.
        """
        if not said or not transcript.text.strip():
            return transcript
        kept = echo.strip_echo(transcript.text, said)
        if kept == transcript.text:
            return transcript
        log.info("echo filtered: %r -> %r", transcript.text, kept)
        return dataclasses.replace(transcript, text=kept)

    def _settle_guess(self, guess, intent, transcript, menu):
        """What the answer to "¿Querés los pendientes?" turned the utterance into.

        A yes replaces it with the command it was nearly saying. Anything else
        replaces the utterance outright — a correction is a new thing said, not
        a refusal — and silence resolves to nothing at all, which is the whole
        point: a phrase we were unsure about is never typed anywhere.
        """
        if intent.kind == intents.KIND_CONFIRM:
            log.info("doubt settled: %r -> %s", transcript.text, guess.kind)
            del menu
            return guess.as_intent(), transcript, None
        if intent.kind in (intents.KIND_SILENCE, intents.KIND_CANCEL):
            # "no" answers the question, and there is nothing behind it: what
            # we were unsure about is dropped, never sent on to a window.
            return None, transcript, None
        return intent, transcript, None

    async def classify(
        self,
        transcript: Transcript,
        *,
        menu=None,
        pendings: Sequence[tuple[str, str]] = (),
    ) -> classify_mod.Plan:
        """What one utterance asked for — lexicon first, model only if it missed.

        The order is the whole design. Anything the phrase lists resolve is
        resolved here, instantly and offline; only what they have never heard
        of costs a round trip. And a phrase that is *nearly* a control word
        never reaches the model at all: a transcript we already distrust is not
        made trustworthy by a second opinion on its wording — it is asked about.

        `pendings` is the list that was just read out, passed only by the one
        caller that just read one: "la última" means nothing without it.
        """
        labels = menu.labels if menu else ()
        intent = intents.parse(
            transcript.text, labels, multi=bool(menu and menu.multi_select)
        )
        if intent.kind != intents.KIND_TEXT:
            return classify_mod.Plan.of(intent)

        near = intents.nearest_control(transcript.text)
        if near is not None:
            return classify_mod.Plan.of(
                intent,
                classify_mod.SOURCE_DOUBTFUL,
                guess=classify_mod.Action(kind=near.kind),
            )

        actions = await asyncio.to_thread(
            self.classifier.classify, transcript.text, self.window_names(), pendings
        )
        if actions is None:
            # No key, no network, or nothing usable came back. Exactly what the
            # lexicon alone would have done, which is the point of the leash.
            return classify_mod.Plan.of(intent, classify_mod.SOURCE_UNAVAILABLE)
        if not actions:
            # Asked, and told this is not a command — which is a real answer,
            # and the one that keeps dictation from becoming a random command.
            return classify_mod.Plan.of(intent, classify_mod.SOURCE_LLM)
        resolved = tuple(
            action
            if action.kind != intents.KIND_TEXT or action.text
            else dataclasses.replace(action, text=transcript.text)
            for action in actions
        )
        log.info(
            "classified %r as %s",
            transcript.text,
            ", ".join(action.kind for action in resolved),
        )
        if self._unsure(transcript) and resolved[0].kind != intents.KIND_TEXT:
            # Measured on the real thing: "dámelo" came back as "jamelo" at
            # 0.75 and "dame los pendientes" as "dame los pendins" at 0.70. A
            # model can read through that, and often should — but a command
            # built on words the recognizer itself doubted is asked about, not
            # run. Dictation is exempt: it has its own gate downstream.
            return classify_mod.Plan.of(
                intent, classify_mod.SOURCE_DOUBTFUL, guess=resolved[0]
            )
        return classify_mod.Plan(resolved, classify_mod.SOURCE_LLM)

    def _unsure(self, transcript: Transcript) -> bool:
        """Did the recognizer itself say it was not sure? Inclusive of the line.

        The delivery gate uses the same number to decide whether to read a
        phrase back before typing it, and the boundary is `<=` here because
        0.75 *is* what a bad take measured at.
        """
        confidence = transcript.confidence
        return confidence is not None and confidence <= self.gate.threshold

    def window_names(self) -> list[str]:
        """Every window that can be addressed by name, for "decile a X que…"."""
        names: list[str] = []
        try:
            sessions = roster_mod.load(self.roster_dir, interactive_only=True)
        except OSError:
            sessions = {}
        for session in sessions.values():
            alias = self.store.get_alias(session.session_id)
            name = alias or session.name
            if name and name not in names:
                names.append(name)
        return names

    def window_named(self, spoken: str) -> tuple[str, str]:
        """(session id, tty) of the window you named out loud, or ("", "").

        Matched on the folded name, and then on a name that merely contains
        what you said: window names are two to four words and nobody says all
        of them — "decile a inbox que espere" is about `inbox realtime`.
        """
        wanted = intents.fold(spoken)
        if not wanted:
            return "", ""
        try:
            sessions = roster_mod.load(self.roster_dir, interactive_only=True)
        except OSError:
            sessions = {}
        partial: tuple[str, str] | None = None
        for session in sessions.values():
            alias = self.store.get_alias(session.session_id)
            name = intents.fold(alias or session.name)
            if not name:
                continue
            found = (session.session_id, self.store.tty_for(session.session_id))
            if name == wanted:
                return found
            if partial is None and (wanted in name or name in wanted):
                partial = found
        return partial or ("", "")

    # -- everything one breath asked for, in order --------------------------

    def _queue_actions(self, actions: Sequence[classify_mod.Action]) -> None:
        """Park what is left of a compound phrase until the first part is done.

        "Ok dámelo, y también abrí una ventana nueva y hacé X" is three things,
        and the first of them holds the microphone for a whole reply cycle. So
        the rest wait here rather than running inside it, and are drained the
        moment that cycle unwinds — still in the order they were said.
        """
        for action in actions:
            self._afterwards.append(action)

    async def _drain_actions(self) -> None:
        """Run what the rest of the phrase asked for. A failure stops nothing.

        Deliberately: "abrí una ventana y decile a inbox que espere" with a
        window that has since closed should still open the window. Aborting the
        rest of a sentence because one part of it failed is how one dead tty
        swallows everything else you said.
        """
        pending, self._afterwards = self._afterwards, []
        for action in pending:
            await self._perform_side(action)

    async def _perform_side(self, action: classify_mod.Action) -> bool:
        """One action that is not an answer to the window in front of you."""
        if action.kind == intents.KIND_OPEN:
            return await self._open_window(action.text)
        if action.kind == intents.KIND_TELL:
            return await self._tell_window(action.target, action.text)
        if action.kind == intents.KIND_SHOW:
            session_id, tty = self.window_named(action.target or action.text)
            del session_id
            return bool(tty) and bool(await self._safely(self.delivery.focus, tty))
        if action.kind == intents.KIND_STATUS:
            await self.speak_status()
            return True
        if action.kind == intents.KIND_PENDINGS:
            await self.speak_pendings()
            return True
        log.info("nothing to do for a queued %s", action.kind)
        return False

    async def _open_window(self, text: str) -> bool:
        opened = await self._safely(
            self.delivery.open_tab, self.new_tab_command, text
        )
        if not opened:
            await self.speaker.speak("No pude abrir la ventana.")
            return False
        log.info("opened a tab%s", f" and sent {text!r}" if text else "")
        return True

    async def _tell_window(self, target: str, text: str) -> bool:
        if not text:
            return False
        session_id, tty = self.window_named(target)
        del session_id
        if not tty:
            spoken = announce_mod.speakable(target, self.phonetic)
            await self.speaker.speak(
                f"No encontré la ventana {spoken}." if spoken else "No sé a qué ventana."
            )
            return False
        return await self._write_to(tty, text)

    async def _write_to(self, tty: str, text: str) -> bool:
        """Type a phrase into a window that is already known — and say if it failed.

        Split out because the window is not always found by name: one picked off
        the list carries its own tty, and looking that name up again in the
        roster is one alias rename away from writing somewhere else.
        """
        if not tty:
            await self.speaker.speak("No pude escribir en esa ventana.")
            return False
        if await self._safely(self.delivery.send_text, tty, text) is None:
            await self.speaker.speak("No pude escribir en esa ventana.")
            return False
        log.info("told %s: %r", tty, text)
        return True

    async def _safely(self, call, *args):
        """Run a blocking call off the loop. `None` means it failed, loudly logged.

        Every action of a compound phrase goes through here, because one that
        raises must not take the rest of the sentence with it.
        """
        try:
            return await asyncio.to_thread(call, *args)
        except Exception:  # noqa: BLE001 - one failed action, not a failed phrase
            log.exception("action failed: %s%r", getattr(call, "__name__", call), args)
            return None

    async def _say_then(self, text: str, *, listening: bool) -> Transcript | None:
        """Say something in the middle of a cycle, with the mic open if it is worth it.

        On the last round there is nobody left to answer — the cycle ends the
        moment this returns — so opening a microphone on it would be a take
        spent on a question already closed.
        """
        if not listening:
            await self.speaker.speak(text)
            return None
        return await self.say_and_listen(text=text)

    async def headphones(self) -> bool:
        """Is the voice going somewhere the microphone cannot hear it?"""
        return await asyncio.to_thread(self.output.private)

    async def _cut_the_voice(self, barged: asyncio.Event) -> None:
        """Stop talking the instant you start. Headphones only — see `output.py`."""
        await barged.wait()
        if await self.speaker.interrupt():
            log.info("barge-in: stopped talking")

    def _discard(self, path: Path) -> None:
        if self.keep_recordings:
            return
        with contextlib.suppress(OSError):
            path.unlink()

    # -- answering ----------------------------------------------------------

    def menus_for(self, item: Item) -> list:
        if item.type != TYPE_MENU:
            return []
        return delivery_mod.menus_from_payload(
            item.payload, plan_feedback=self.plan_feedback_index
        )

    async def reply_cycle(
        self, item: Item, announced: str = "", *, first: Transcript | None = None
    ) -> str:
        """Hold the mic on one item until it is answered, skipped, or silent.

        `first` is an utterance already captured for this item — the answer that
        came back to a naming offer, or the one the hotkey took before it knew
        which item it belonged to. It stands in for the first take so nothing
        anyone said has to be said twice.
        """
        if not self.can_listen() or not item.tty:
            return REPLY_PENDING
        async with self._mic_lock:
            self.store.mark_awaiting_reply(item.id)
            try:
                outcome = await self._converse(item, announced, first=first)
            except iterm.SessionGone as exc:
                log.warning("delivery aborted: %s", exc)
                await self.speaker.speak("Esa ventana ya no está.")
                outcome = REPLY_FAILED
            except Exception:  # noqa: BLE001 - a bad reply must not kill the loop
                log.exception("reply cycle failed")
                await self.speaker.speak("Hubo un error entregando la respuesta.")
                outcome = REPLY_FAILED
            if outcome == REPLY_DELIVERED:
                self.store.mark_delivered(item.id)
            else:
                # Back in line, reachable from `pendings`, never dropped.
                self.store.mark_pending(item.id)
        # Outside the lock: whatever else that breath asked for may want to
        # talk, and the cycle it belonged to is over.
        await self._drain_actions()
        return outcome

    async def _converse(
        self, item: Item, announced: str, *, first: Transcript | None = None
    ) -> str:
        menus = self.menus_for(item)
        for position, menu in enumerate(menus or [None]):
            if position:
                # The payload had more than one question; Claude shows the next
                # one the moment the previous is answered. Read with the mic
                # open, so an answer given over the options still lands.
                first = await self.say_and_listen(
                    text=announce_mod.speakable(
                        announce_mod.describe_question(
                            menu.prompt,
                            menu.labels,
                            multi_select=menu.multi_select,
                            position=menu.position,
                            total=menu.total,
                        ),
                        self.phonetic,
                    )
                )
                if first is None:
                    return REPLY_FAILED
            # Only the question that was actually open when it was said.
            outcome = await self._answer_one(item, menu, announced, first=first)
            first = None
            if outcome != REPLY_DELIVERED:
                # A menu with a question left unanswered is still blocking that
                # window, so the item stays pending however much of it we got.
                return outcome
        return REPLY_DELIVERED

    async def _answer_one(
        self, item: Item, menu, announced: str, *, first: Transcript | None = None
    ) -> str:
        pending_action = None
        pending_guess: intents.NearMiss | None = None
        queued: list[classify_mod.Action] = []
        rounds = 0
        while queued or rounds < self.mic_rounds:
            if not queued:
                if first is not None:
                    transcript, first = first, None
                else:
                    transcript = await self.listen()
                rounds += 1
                if transcript is None:
                    return REPLY_FAILED
                plan = await self.classify(transcript, menu=menu)
                queued = list(plan.actions)
                guess = plan.guess
            else:
                guess = None
            more_rounds = rounds < self.mic_rounds
            action = queued.pop(0)
            intent = action.as_intent()

            if pending_guess is not None:
                intent, transcript, pending_guess = self._settle_guess(
                    pending_guess, intent, transcript, menu
                )
                if intent is None:
                    # Neither a yes nor anything else worth acting on. The
                    # doubtful phrase is dropped rather than delivered.
                    return REPLY_PENDING
            elif guess is not None:
                # Before anything is dispatched: a command built out of words
                # the recognizer doubted is asked about, never run.
                pending_guess = guess
                self._queue_actions(queued)
                queued = []
                first = await self._say_then(
                    announce_mod.speakable(
                        announce_mod.near_miss_question(transcript.text, guess.kind),
                        self.phonetic,
                    ),
                    listening=more_rounds,
                )
                continue

            if intent.kind == intents.KIND_LATER:
                # Same instruction as at the heads-up: not now, and not first.
                self.store.defer(item.id)
                self._queue_actions(queued)
                return REPLY_PENDING
            if intent.kind in (intents.KIND_SILENCE, intents.KIND_SKIP):
                self._queue_actions(queued)
                return REPLY_PENDING
            if intent.kind == intents.KIND_REPEAT:
                first = await self._say_then(
                    announced or "No tengo nada que repetir.",
                    listening=more_rounds and not queued,
                )
                continue
            if intent.kind == intents.KIND_SHOW:
                await self._safely(self.delivery.focus, item.tty)
                continue
            if intent.kind in (intents.KIND_OPEN, intents.KIND_TELL):
                await self._perform_side(action)
                continue
            if intent.kind == intents.KIND_WAIT:
                first = await self._say_then(
                    "Dale, espero.", listening=more_rounds and not queued
                )
                continue
            if intent.kind == intents.KIND_STATUS:
                first = await self._speak_status_and_listen(
                    listening=more_rounds and not queued
                )
                continue
            if intent.kind == intents.KIND_PENDINGS:
                chosen = await self.speak_pendings()
                if chosen is not None and chosen.id != item.id:
                    # Unwind first: this item's mic lock is in the way, and
                    # leaving it half-answered is what `pending` is for.
                    self._switch_to = chosen.id
                    self._queue_actions(queued)
                    return REPLY_PENDING
                continue
            if intent.kind == intents.KIND_EXPLAIN and menu is not None:
                first = await self._say_then(
                    announce_mod.speakable(menu.describe(intent.index), self.phonetic),
                    listening=more_rounds and not queued,
                )
                continue

            if pending_action is not None:
                if intent.kind == intents.KIND_CONFIRM:
                    await self._perform(item, pending_action)
                    self._queue_actions(queued)
                    return REPLY_DELIVERED
                if intent.kind == intents.KIND_CANCEL:
                    await self.speaker.speak("Listo, no mando nada.")
                    self._queue_actions(queued)
                    return REPLY_PENDING
                pending_action = None  # a new utterance replaces the old one

            planned, gate = self._plan_action(item, menu, intent, transcript)
            if planned is None:
                first = await self._say_then(
                    gate.reason, listening=more_rounds and not queued
                )
                continue
            if gate.required:
                pending_action = planned
                self._queue_actions(queued)
                queued = []
                first = await self._say_then(
                    self._readback_sentence(gate, self._readback(planned, menu)),
                    listening=more_rounds,
                )
                continue
            await self._perform(item, planned)
            self._queue_actions(queued)
            return REPLY_DELIVERED
        return REPLY_PENDING

    def _plan_action(self, item: Item, menu, intent, transcript):
        """(action, gate). A `None` action means say `gate.reason` and listen again."""
        if intent.kind == intents.KIND_SELECT:
            gate = self.gate.check_choice(transcript.confidence)
            if menu is not None and menu.multi_select:
                return ("choices", tuple(intent.indexes)), gate
            return ("choice", intent.index), gate

        if intent.kind == intents.KIND_CANCEL and menu is not None:
            return None, delivery_mod.Gate(False, "Lo dejo pendiente.")
        if intent.kind == intents.KIND_CONFIRM and menu is not None:
            # "dale" does not name an option, and picking one for you is how a
            # menu answers itself wrong.
            return None, delivery_mod.Gate(False, "¿Cuál opción?")

        text = intent.text
        gate = self.gate.check(text, transcript.confidence)
        if not gate.required and intents.looks_systemward(text):
            # A question about the queue that no control phrase caught. Asking
            # costs one round; typing "cuál queda" into a session costs the
            # session — which is what happened on the first real run.
            gate = delivery_mod.Gate(
                True, "No sé si eso era para mí", "¿Te lo mando a la ventana?"
            )
        if menu is not None:
            return ("menu_text", (menu.free_text_index, text)), gate
        return ("text", text), gate

    def _readback_sentence(self, gate, said: str) -> str:
        """One shape for every read-back: what I doubt, what I heard, what I ask."""
        return announce_mod.speakable(
            f"{gate.reason}. Dijiste: {said}. {gate.question}", self.phonetic
        )

    def _readback(self, action, menu) -> str:
        kind, payload = action
        if kind == "choice":
            label = menu.labels[payload - 1] if menu and payload <= len(menu.labels) else ""
            return f"opción {announce_mod.number_word(payload)}{': ' + label if label else ''}"
        if kind == "choices":
            return ", ".join(
                f"opción {announce_mod.number_word(index)}" for index in payload
            )
        if kind == "menu_text":
            return payload[1]
        return str(payload)

    async def _perform(self, item: Item, action) -> None:
        kind, payload = action
        log.info("deliver %s to %s [%s]", kind, item.tty, item.name or item.session_id[:8])
        if kind == "choice":
            await asyncio.to_thread(self.delivery.send_choice, item.tty, payload)
        elif kind == "choices":
            await asyncio.to_thread(self.delivery.send_choices, item.tty, payload)
        elif kind == "menu_text":
            index, text = payload
            await asyncio.to_thread(self.delivery.send_menu_text, item.tty, index, text)
        else:
            await asyncio.to_thread(self.delivery.send_text, item.tty, payload)

    # -- asking voice-loop itself -------------------------------------------

    def status_sentence(self) -> str:
        """How the whole board looks, in one breath."""
        try:
            sessions = roster_mod.load(self.roster_dir, interactive_only=True)
        except OSError:
            sessions = {}
        return announce_mod.describe_status(
            windows=len(sessions),
            working=sum(1 for s in sessions.values() if s.status == roster_mod.STATUS_BUSY),
            waiting=self.store.open_count(),
            milestones=sorted(self.watcher.current().items()),
            paused=self.paused,
            busy=self.busy,
        )

    async def speak_status(self) -> str:
        """Answerable from any mode, busy included: "what is going on" is the
        question you ask precisely when you have not been listening.
        """
        text = self.status_sentence()
        # Logged because from outside the machine a spoken answer and no answer
        # at all look identical, and this is the one command whose whole output
        # is a sentence nobody can grep for afterwards.
        log.info("status: %s", text)
        await self.speaker.speak(announce_mod.speakable(text, self.phonetic))
        return text

    async def _speak_status_and_listen(self, *, listening: bool = True) -> Transcript | None:
        """The same answer, with the mic open under it — you usually ask twice."""
        text = announce_mod.speakable(self.status_sentence(), self.phonetic)
        return await self._say_then(text, listening=listening)

    async def speak_pendings(self) -> Item | None:
        """Read the queue out in order, then take what was said over the top of it.

        A **pick** — by number, by name, by "la última" — is what makes this more
        than a report: the window you choose is returned, re-announced and given
        the microphone, exactly as if it had just blocked. Resolved by the
        lexicon alone: instant, offline, and never read back.

        Anything longer is an **instruction about** one of them, and that is the
        other half of the same sentence — "decile a la última que…". It goes to
        `_instruct_over_pendings`, which is the only path here that writes
        anywhere, and the only one that asks first.

        Summaries missing from superseded items are filled in here (issue #3) —
        a list of names with nothing after them answers nothing. Windows that
        have closed since are dropped first: a list you cannot act on any more
        is worse than a short one.
        """
        self.sweep_gone()
        items = self.store.pendings()
        recomputed = await self.summarize_missing(items)
        now = time.time()
        names = [self._name_for(item, self._session_for(item)) or "" for item in items]
        entries = [
            (
                names[index],
                item.summary or recomputed.get(item.id, ""),
                announce_mod.ago_phrase(now - item.ts),
            )
            for index, item in enumerate(items)
        ]
        spoken = announce_mod.speakable(
            announce_mod.describe_pendings(entries), self.phonetic
        )
        log.info("pendings: %d item(s) — %s", len(items), spoken)
        if not items:
            await self.speaker.speak(spoken)
            return None

        # The list is long and the pick is usually said over the top of it.
        transcript = await self.say_and_listen(text=spoken)
        if transcript is None:
            return None
        intent = intents.parse(transcript.text, names)
        if intent.kind == intents.KIND_SELECT and 1 <= (intent.index or 0) <= len(items):
            chosen = items[intent.index - 1]
            log.info(
                "picked %s [%s] off the pendings list", chosen.id[:8], names[intent.index - 1]
            )
            return chosen
        if intent.kind == intents.KIND_TEXT:
            # A sentence, not a pick — and the sentence is usually the point.
            await self._instruct_over_pendings(transcript, items, entries)
        return None

    async def _instruct_over_pendings(
        self,
        transcript: Transcript,
        items: Sequence[Item],
        entries: Sequence[tuple[str, str, str]],
    ) -> None:
        """A whole instruction said over the list, instead of a pick off it.

        "decile a la última que lo deje fijo en cuatro punto ocho" is the flow
        the list exists for, and it is the one thing the list could not do: the
        pick is resolved by `intents.parse` alone, which knows a number and a
        name and nothing else, so a reference *inside* a sentence fell through
        to "no te entendí" and the instruction went nowhere.

        The list is the context that reference needs — position, name and
        summary of each item, exactly as they were just read out — so it goes
        to the classifier with the phrase. Two rules hold the risk down:

        * **Nothing is written without a read-back.** This is the most
          ambiguous input the system takes: a long sentence dictated over
          twenty-five seconds of our own voice, about windows that were named
          out loud a moment ago. "Entendí: … ¿Lo mando?", and a yes is the only
          thing that sends it.
        * **A window we cannot pin down is asked about, never guessed.** The
          model is told to leave `target` empty rather than invent one, and
          anything that does not resolve against the list gets "¿A cuál?".
        """
        plan = await self.classify(
            transcript, pendings=[(name, summary) for name, summary, _ in entries]
        )
        if plan.source != classify_mod.SOURCE_LLM:
            # The model was never reached, or the transcript is one we already
            # distrust. Either way there is nothing here the lexicon has not
            # already failed to resolve, and this is what it says.
            log.info("nothing picked off the pendings list: %r", transcript.text)
            await self.speaker.speak(NO_PICK_SPOKEN)
            return
        names = [name for name, _, _ in entries]
        aimed, adrift = self._aim_over_pendings(plan.actions, names, transcript.text)
        if adrift or not aimed:
            log.info(
                "nothing to do off the pendings list: %r (%s)",
                transcript.text,
                "no window" if adrift else "no action",
            )
            await self.speaker.speak(WHICH_WINDOW_SPOKEN if adrift else NO_PICK_SPOKEN)
            return
        if not await self._confirm_over_pendings(aimed, names):
            return
        for action in aimed:
            await self._perform_aimed(action, items)

    def _aim_over_pendings(
        self,
        actions: Sequence[classify_mod.Action],
        names: Sequence[str],
        said: str = "",
    ) -> tuple[list[classify_mod.Action], bool]:
        """(what to do, whether something was about a window we could not find).

        Everything that needs a window carries the position it landed on; the
        rest — opening a tab, reading the board back — needs none and passes
        through. A second flag rather than a shorter list, because half a
        sentence acted on is worse than none of it: "decile a la última que X y
        abrí una ventana" with an unresolvable "la última" asks, it does not
        quietly do the half it understood.

        `said` is the phrase itself, and it is here for the one thing the
        actions alone cannot tell you: whether the windows were pointed at out
        loud or picked for us. See `_fanned_out`.
        """
        aimed: list[classify_mod.Action] = []
        adrift = False
        for action in actions:
            if action.kind == intents.KIND_TELL and not action.text:
                continue
            if action.kind in (intents.KIND_TELL, intents.KIND_SHOW):
                index = self._listed_index(action.target, names)
                if index is None:
                    adrift = True
                    continue
                aimed.append(dataclasses.replace(action, index=index))
            elif action.kind in (intents.KIND_OPEN, intents.KIND_STATUS):
                # Both end where they start. Reading the *list* back is not
                # here on purpose: it is the function we are already inside,
                # and a queue that can ask itself for the queue is a loop with
                # a microphone in it.
                aimed.append(action)
            elif action.kind == intents.KIND_TEXT:
                # Dictation, said over a list of windows. It is work for one of
                # them and the model could not tell which — which is a question,
                # not a reason to hand it to whoever spoke last.
                adrift = True
        if self._fanned_out(aimed, names, said):
            log.info("a fan-out nobody asked for, off the pendings list: %r", said)
            return [], True
        return aimed, adrift

    @classmethod
    def _fanned_out(
        cls,
        aimed: Sequence[classify_mod.Action],
        names: Sequence[str],
        said: str,
    ) -> bool:
        """Did one dictated sentence turn into a different message per window?

        Measured, off the list of 2026-08-09: "decile que haga eso" — which
        points at nothing — came back as three `tell`s, one per pending window,
        each carrying *that window's summary* rewritten as an order. None of
        those sentences was said. The prompt already says not to, and the guard
        for an unresolvable reference never fired, because the model does not
        answer with an empty target: it answers with three full ones. That is
        worse than the silence this path was built to fix — silence does
        nothing, and this does three wrong things after one distracted yes.

        The signature is the *shape*, not the wording: one breath split into N
        messages with N different texts. Two things are deliberately not that,
        and both stay:

        * **The same text repeated.** "decile a todas que paren" really is for
          several windows, and what arrives at each of them is identical.
        * **Windows named out loud.** "decile a e5 que X y a cl audio que Y"
          asks for two different things and says so — the references are in the
          phrase, not inferred from a list of summaries.
        """
        tells = [action for action in aimed if action.kind == intents.KIND_TELL]
        if len(tells) < 2:
            return False
        if len({intents.fold(action.text) for action in tells}) == 1:
            return False
        return not all(cls._named_aloud(action, names, said) for action in tells)

    @staticmethod
    def _named_aloud(
        action: classify_mod.Action, names: Sequence[str], said: str
    ) -> bool:
        """Was this window pointed at in the phrase, or chosen for us off the list?

        The name as it was spoken, or the position — "la primera", "la tres",
        "la última". A window recognized only by what its summary says is not
        named aloud: that is the model reading the list, which is exactly what
        it does when it invents a fan-out.
        """
        spoken = intents.fold(said)
        if not spoken:
            return False
        target = intents.fold(action.target)
        if target and target in spoken:
            return True
        if action.index is None:
            return False
        tokens = spoken.split()
        if any(intents.as_number(token) == action.index for token in tokens):
            return True
        return action.index == len(names) and any(
            token in intents.LAST_WORDS for token in tokens
        )

    @staticmethod
    def _listed_index(target: str, names: Sequence[str]) -> int | None:
        """Which of the windows just read out that names, or `None` — which asks.

        The model is told to answer with the exact name off the list, and mostly
        does; "la última" and "3" come back often enough to be worth resolving
        here too. A partial has to be *unique* — two windows it could equally
        mean is exactly the case where guessing is the whole failure.
        """
        wanted = intents.fold(target)
        if not wanted or not names:
            return None
        intent = intents.parse(target, names)
        if intent.kind == intents.KIND_SELECT and 1 <= (intent.index or 0) <= len(names):
            return intent.index
        found = []
        for index, name in enumerate(names, start=1):
            folded = intents.fold(name)
            if folded and (wanted in folded or folded in wanted):
                found.append(index)
        return found[0] if len(found) == 1 else None

    async def _confirm_over_pendings(
        self, aimed: Sequence[classify_mod.Action], names: Sequence[str]
    ) -> bool:
        """Read the plan back and wait for a yes. Anything else drops all of it.

        Only what *writes* is asked about. Focusing a window or reading the
        board back changes nothing and undoes itself, and a read-back on those
        is the nagging that got read-backs rationed in the first place.
        """
        if not any(
            action.kind in (intents.KIND_TELL, intents.KIND_OPEN) for action in aimed
        ):
            return True
        question = announce_mod.plan_question(
            [
                announce_mod.describe_action(
                    action.kind,
                    names[action.index - 1] if action.index else action.target,
                    action.text,
                )
                for action in aimed
            ]
        )
        if not question:
            await self.speaker.speak(NO_PICK_SPOKEN)
            return False
        answer = await self.say_and_listen(
            text=announce_mod.speakable(question, self.phonetic)
        )
        if answer is not None and intents.parse(answer.text).kind == intents.KIND_CONFIRM:
            return True
        log.info("dropped what was said over the pendings list: %s", question)
        if answer is not None:
            await self.speaker.speak("Listo, no mando nada.")
        return False

    async def _perform_aimed(self, action: classify_mod.Action, items: Sequence[Item]) -> bool:
        """One confirmed action, sent to the window it was aimed at by position."""
        if action.index is None:
            return await self._perform_side(action)
        item = items[action.index - 1]
        if action.kind == intents.KIND_SHOW:
            return bool(item.tty) and bool(await self._safely(self.delivery.focus, item.tty))
        return await self._write_to(item.tty, action.text)

    async def _follow_switch(self, depth: int) -> None:
        """Serve the window that was picked off the list mid-conversation.

        The pick cannot be served where it is made: that code is holding the
        microphone lock for the item being answered. So it is parked here and
        acted on once that cycle has unwound — bounded, because a chain of
        switches is still one hotkey press away from being restarted by hand.
        """
        await self._drain_actions()
        target, self._switch_to = self._switch_to, None
        if target is None:
            return
        if depth >= MAX_SWITCHES:
            log.warning("stopping after %d window switches in one go", depth)
            return
        item = self.store.get(target)
        if item is None:
            return
        # You picked it by name off the list; you do not have to ask twice.
        await self._announce(
            item, self._session_for(item), depth=depth + 1, heads_up=False
        )

    # -- control surface ---------------------------------------------------

    async def dispatch(self, cmd: str, args: dict) -> Any:
        handler = getattr(self, f"cmd_{cmd.replace('-', '_')}", None)
        if handler is None:
            raise ControlError(f"unknown command: {cmd}")
        result = handler(args)
        if inspect.isawaitable(result):
            result = await result
        return result

    def cmd_status(self, args: dict) -> dict:
        return {
            "version": __version__,
            "pid": os.getpid(),
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "paused": self.paused,
            "busy": self.busy,
            "queued": self.store.queued_count(),
            "open": self.store.open_count(),
            "state_dir": str(self.state_dir),
            "summaries": "openai" if self.summarizer.available else "fallback",
            "voice": self.speaker.voice,
            "milestone_watch": self.watcher.active,
            "speech_to_text": self._stt_status(),
            "mic": "listening" if self._mic_stop is not None else "idle",
        }

    def _stt_status(self) -> str:
        if self.stt is None:
            return "unavailable"
        if not self.mic_enabled:
            return f"{self.stt.name} (mic off)"
        return self.stt.name if self.stt.available else f"{self.stt.name} (no key)"

    async def cmd_pendings(self, args: dict) -> list[dict]:
        # A window that has closed since is not pending on anybody.
        self.sweep_gone()
        items = self.store.pendings()
        recomputed = await self.summarize_missing(items)
        listed = []
        for item in items:
            # Resolved here, not only at announce time: an item still queued has
            # never been through `_announce`, and listing it as `5cbf3ac9`
            # breaks the one command whose job is telling you which window
            # wants you. Milestones have no session and keep whatever they had.
            name = self._name_for(item, self._session_for(item)) if item.session_id else item.name
            listed.append(
                {
                    "id": item.id,
                    "ts": item.ts,
                    "type": item.type,
                    "state": item.state,
                    "name": name or "",
                    "session_id": item.session_id,
                    "tty": item.tty,
                    "summary": item.summary or recomputed.get(item.id, ""),
                    "announced_at": item.announced_at,
                }
            )
        return listed

    async def cmd_mic_toggle(self, args: dict) -> dict:
        """The hotkey. Closes an open mic; otherwise opens one on the last item.

        Returns immediately either way — the hotkey must not sit there holding
        the socket for the length of a sentence.
        """
        if self._mic_stop is not None:
            self._mic_stop.set()
            return {"mic": "closing"}
        if not self.can_listen():
            raise ControlError(
                "microphone unavailable: " + self._stt_status()
                if self.stt is not None
                else "microphone unavailable: no speech-to-text provider"
            )
        self._spawn(self._hotkey_listen())
        return {"mic": "opening"}

    def _spawn(self, coroutine) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._mic_tasks.add(task)
        task.add_done_callback(self._mic_tasks.discard)

    async def _hotkey_listen(self) -> None:
        """A mic you opened yourself. With nothing in flight, you are talking to me.

        The take comes first, before deciding what it was for, because in busy
        mode this hotkey is the only way to ask anything at all. Then the
        phrase is walked in the order it was said: everything that is voice-
        loop's own business — the queue, the board, opening a window, telling a
        window by name — happens here and now, and the first thing that needs a
        window in front of you is handed to whichever one spoke last.

        With **nothing** in flight there is no such window, and a sentence is
        not typed into the last one out of hopefulness: it says so. Naming the
        window is how you reach one — "decile a inbox realtime que…".
        """
        try:
            item = self.store.last_announced()
            chosen = None
            handled = False
            for_the_window: classify_mod.Action | None = None
            async with self._mic_lock:
                transcript = await self.listen()
                if transcript is None:
                    return
                plan = await self.classify(transcript)
                for position, action in enumerate(plan.actions):
                    if action.kind == intents.KIND_PENDINGS:
                        handled = True
                        chosen = await self.speak_pendings()
                        if chosen is not None:
                            self._queue_actions(plan.actions[position + 1 :])
                            break
                        continue
                    if self._is_assistant_action(action):
                        handled = True
                        await self._perform_side(action)
                        continue
                    if action.kind == intents.KIND_SILENCE:
                        continue
                    for_the_window = action
                    self._queue_actions(plan.actions[position + 1 :])
                    break
            if chosen is not None:
                # `_announce` follows any further switch from here itself.
                await self._announce(chosen, self._session_for(chosen), heads_up=False)
                return
            if for_the_window is None:
                await self._drain_actions()
                if not handled:
                    # Nothing was said. In busy mode nothing has been announced
                    # at all, so this says nothing about the queue beyond how
                    # much of it there is.
                    await self.speaker.speak(self._how_much_is_waiting())
                return
            if item is None or not item.tty:
                # A sentence with no window in front of it. It is *not* typed
                # into whichever one spoke last hours ago — naming the window
                # is how you reach one: "decile a inbox realtime que…".
                await self._drain_actions()
                await self.speaker.speak(
                    f"{self._how_much_is_waiting()} Decime a cuál le hablo."
                )
                return
            outcome = await self.reply_cycle(
                item,
                item.summary or "",
                first=dataclasses.replace(
                    transcript, text=for_the_window.text or transcript.text
                ),
            )
            if outcome == REPLY_DELIVERED:
                await self.speak_remaining()
            await self._follow_switch(0)
        except Exception:  # noqa: BLE001 - a background task must not die silently
            log.exception("hotkey mic failed")

    def _how_much_is_waiting(self) -> str:
        piled_up = announce_mod.pendings_count(self.store.open_count())
        return f"{piled_up}." if piled_up else "No hay nada pendiente."

    def _is_assistant_action(self, action: classify_mod.Action) -> bool:
        """Is this voice-loop's own business, or does it need a window in front of you?

        `show` is the one that is both: "mostrame" is about the window that
        just spoke, and "mostrame inbox realtime" is about a window by name.
        """
        if action.kind in (intents.KIND_STATUS, intents.KIND_OPEN, intents.KIND_TELL):
            return True
        return action.kind == intents.KIND_SHOW and bool(action.target)

    async def cmd_busy_toggle(self, args: dict) -> dict:
        """Silence, and on the way out, how much of it there was.

        Busy is no longer "chime instead of speak": a chime every time is the
        same interruption with the words taken out. Nothing arrives at all —
        no chime, no voice, no microphone — and the queue simply grows.

        Which leaves the toggle as the only thing that can say which mode you
        are in, so it does, in one word. It used to answer with a chime whose
        two directions sound identical, and the honest answer to "am I in busy
        mode?" was to go and read the logs. The mic hotkey keeps working in
        busy — that is how you ask for the queue in a meeting.
        """
        self.busy = not self.busy
        self.store.kv_set(KV_BUSY, self.busy)
        log.info("busy mode %s", "on" if self.busy else "off")
        await self.speaker.chime(self.busy_chime)
        if self.busy:
            await self.speaker.speak("Ocupado.")
        else:
            piled_up = announce_mod.pendings_count(self.store.open_count())
            await self.speaker.speak(f"Te escucho. {piled_up}." if piled_up else "Te escucho.")
        return {"busy": self.busy}

    async def cmd_selfcheck(self, args: dict) -> list[dict]:
        """What the daemon can actually do from where launchd put it.

        Microphone and Automation permissions are granted per responsible
        process, so "it works in my terminal" says nothing about the agent.
        This is the answer from inside.
        """
        checks = await asyncio.to_thread(
            preflight.run_all,
            binary=self.recorder.binary,
            device=self.recorder.device,
            engine=self.stt,
            env_file=envfile.read(),
        )
        return [check.as_dict() for check in checks]

    def cmd_pause(self, args: dict) -> dict:
        self.paused = True
        self.store.kv_set(KV_PAUSED, True)
        return {"paused": True}

    def cmd_resume(self, args: dict) -> dict:
        self.paused = False
        self.store.kv_set(KV_PAUSED, False)
        return {"paused": False}

    def cmd_replay(self, args: dict) -> dict:
        event_id = args.get("id")
        item = self.store.get(event_id) if event_id else self.store.last_announced()
        if item is None:
            raise ControlError("nothing to replay")
        self.store.requeue(item.id)
        return {"replaying": item.id, "name": item.name or ""}

    def cmd_skip(self, args: dict) -> dict:
        """Drop one item off the pendings list.

        Until now the only way out of `pendings` was an `activity` event from
        that session, so an item whose window died — or that simply stopped
        mattering — stayed there for ever.
        """
        event_id = args.get("id")
        item = self.store.get(event_id) if event_id else self.store.last_announced()
        if item is None:
            raise ControlError(f"no such item: {event_id}" if event_id else "nothing to skip")
        if not self.store.resolve(item.id, RESOLVED_BY_SKIP):
            raise ControlError(f"already resolved: {item.id}")
        log.info("skipped %s [%s]", item.id[:8], item.display_name)
        return {"skipped": item.id, "name": item.name or ""}

    def cmd_milestone(self, args: dict) -> dict:
        label = str(args.get("label") or "").strip()
        if not label:
            raise ControlError("milestone needs a label")
        event = Event.new(TYPE_MILESTONE, session_id="", payload={"label": label})
        self.store.ingest(event)
        return {"queued": event.id, "label": label}

    def cmd_restart(self, args: dict) -> dict:
        # Exit cleanly; launchd (KeepAlive) brings us straight back up. Deferred
        # so the reply reaches the client before the socket goes away.
        asyncio.get_running_loop().call_later(0.25, self.stop, True)
        return {"restarting": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="voice-loopd", description="voice-loop daemon")
    parser.add_argument("--config", help="path to config.local.yml", default=None)
    parser.add_argument("--repo-root", help="directory holding config.example.yml", default=None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    options = parser.parse_args(argv)

    try:
        config = load_config(repo_root=options.repo_root, local_path=options.config)
    except ConfigError as exc:
        print(f"voice-loopd: {exc}", file=sys.stderr)
        return 2

    setup_logging(config.log_dir, level=logging.DEBUG if options.verbose else logging.INFO)
    logging.getLogger("voiceloop").info("starting voice-loop %s", __version__)

    daemon = Daemon(config)

    async def runner() -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, daemon.stop)
        return await daemon.run()

    try:
        return asyncio.run(runner())
    except DaemonAlreadyRunning as exc:
        print(f"voice-loopd: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
