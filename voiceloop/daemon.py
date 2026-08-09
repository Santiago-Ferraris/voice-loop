"""The daemon — the only process that owns state, and the only one that talks.

Three cooperating loops:

* **ingest** (250 ms) drains the spool into SQLite. Cheap, and the only thing
  that has to keep up with fifteen sessions firing hooks at once.
* **announce** walks the queue in FIFO order and speaks the first item that is
  actually ready. "Ready" is where the subagent gate lives: a turn that ended
  with background agents still running is skipped — silently, keeping its
  place — until they finish.
* **milestones** (optional) polls external phase files for chime-only events.

Everything slow runs off the loop: transcript parsing, the summary call, the
transcription call and every AppleScript go through `to_thread`, and speech is
serialized behind the speaker's own lock.

The announce loop does not end at the announcement. Speaking an item opens the
microphone on it and stays there until the item is answered, skipped, or heard
nothing — so the queue is strictly one conversation at a time, which is the
only way fifteen windows can talk to one pair of ears. Everything that can end
a reply cycle puts the item back in `pendings`; nothing is ever dropped for
having been ignored.

Two modes sit on top. **Paused** stops announcing entirely. **Busy** keeps the
queue moving but chimes instead of speaking, and does not open the mic — the
hotkey still does, because "I am heads-down" and "I cannot answer" are
different things.
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
    delivery as delivery_mod,
    envfile,
    intents,
    iterm,
    naming,
    preflight,
    roster as roster_mod,
    spool,
)
from .audio import AudioUnavailable, MicConsentPending, Recorder
from .config import Config, ConfigError, load as load_config
from .control import ControlError, ControlServer, DaemonAlreadyRunning
from .delivery import Delivery, GatePolicy
from .events import TYPE_MENU, TYPE_MILESTONE, TYPE_STOP, Event
from .milestones import MilestoneWatcher
from .store import Item, Store
from .stt import SttError, SttNotImplemented, Transcript, create as create_stt
from .summarize import FALLBACK_SUMMARY, Summarizer
from .transcript import pending_subagents, tail_text
from .tts import Speaker

INGEST_INTERVAL = 0.25
ANNOUNCE_INTERVAL = 0.2
MILESTONE_INTERVAL = 1.0

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

# How many windows one "dame los pendientes" chain may hop through before the
# daemon stops following it. Not a limit on you — the hotkey starts a new chain.
MAX_SWITCHES = 5

# One reply cycle ends this way.
REPLY_DELIVERED = "delivered"
REPLY_PENDING = "pending"
REPLY_FAILED = "failed"

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
            asyncio.create_task(self._milestone_loop(), name="milestones"),
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
        try:
            live = set(roster_mod.load(self.roster_dir).keys())
        except OSError:
            live = set()
        gone = self.store.resolve_sessions_missing(live, RESOLVED_BY_GONE) if live else 0
        if recovered or gone:
            log.info("reconciled: %d requeued, %d resolved as gone", recovered, gone)

    # -- loops -------------------------------------------------------------

    async def _ingest_loop(self) -> None:
        while True:
            try:
                self.ingest_once()
            except Exception:  # noqa: BLE001 - a bad event must not kill ingest
                log.exception("ingest failed")
            await asyncio.sleep(INGEST_INTERVAL)

    def ingest_once(self) -> int:
        count = 0
        for path, event in spool.read_pending(self.config.spool_dir):
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

    async def _announce_loop(self) -> None:
        while True:
            try:
                await self.announce_next()
            except Exception:  # noqa: BLE001 - never let the announcer die
                log.exception("announce failed")
            await asyncio.sleep(ANNOUNCE_INTERVAL)

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
        if self.paused:
            return False
        for item in self.store.queued_items():
            session = self._session_for(item)
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

    async def _announce(self, item: Item, session, *, depth: int = 0) -> None:
        self.store.mark_announcing(item.id)
        name = self._name_for(item, session)
        self.store.set_name(item.id, name)
        summary, slug = await self._summary_and_slug(item, session)

        announcement = announce_mod.build(
            item,
            name=name,
            summary=summary,
            phonetic=self.phonetic,
            blocking_chime=self.blocking_chime,
            milestone_chime=self.milestone_chime,
            notification_events=self.notification_events,
            naming_offer=slug,
        )
        if self.busy:
            # Busy mode: you still get the chime, you just do not get talked at.
            announcement = dataclasses.replace(announcement, speak=False)
        log.info("announce %s [%s] %s", item.type, name, announcement.text)
        await self.speaker.announce(announcement)

        if item.type == TYPE_MILESTONE:
            # Chime only — there is nothing for the user to answer.
            self.store.resolve(item.id, RESOLVED_BY_MILESTONE)
            return

        self.store.mark_pending(item.id)
        if self.busy or announcement.silent:
            return
        # The naming answer comes first and gets its own take: it was the last
        # thing asked, and whatever is not a name is handed straight on to the
        # window's own reply cycle rather than thrown away.
        overheard = await self._settle_name(item, slug) if slug else None
        outcome = await self.reply_cycle(item, announcement.text, first=overheard)
        if outcome == REPLY_DELIVERED:
            await self.speak_remaining()
        await self._follow_switch(depth)

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
        them separately would read the same transcript twice and pay twice. An
        item that already has a summary is not re-summarised just to get a name
        offered — a replay costs nothing, and the offer comes back next time
        this window blocks.
        """
        if not self._wants_a_name(item, session) or item.summary:
            return await self._summary_for(item), ""
        tail = await asyncio.to_thread(tail_text, item.transcript_path)
        result = await asyncio.to_thread(self.summarizer.summarize_and_name, tail)
        if result.text != FALLBACK_SUMMARY:
            self.store.set_summary(item.id, result.text)
        return result.text, result.slug

    async def _settle_name(self, item: Item, slug: str) -> Transcript | None:
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

    async def _save_name(self, item: Item, slug: str) -> None:
        self.store.set_alias(item.session_id, slug, confirmed=True)
        self.store.set_name(item.id, slug)
        log.info("named %s -> %r", item.session_id[:8], slug)
        await self.speaker.speak(announce_mod.speakable(f"Listo, {slug}.", self.phonetic))

    # -- listening ---------------------------------------------------------

    def can_listen(self) -> bool:
        return bool(self.mic_enabled and self.stt is not None)

    def keyterms(self) -> list[str]:
        """Config vocabulary plus the names of the windows that exist right now.

        A session name only transcribes if the recognizer has been told it
        exists, and the names change every time you open a window — so they are
        collected per request rather than baked into the engine.
        """
        configured = self.config.get("keyterms") or []
        terms = [str(term) for term in configured] if isinstance(configured, (list, tuple)) else []
        try:
            sessions = roster_mod.load(self.roster_dir)
        except OSError:
            sessions = {}
        for session in sessions.values():
            if session.name:
                terms.append(session.name)
        terms.extend(self.store.aliases())
        return terms

    async def listen(self) -> Transcript | None:
        """One take: chime, record, transcribe. `None` means the mic itself failed."""
        if not self.can_listen():
            return None
        mic_dir = self.state_dir / "mic"
        path = mic_dir / f"{uuid.uuid4().hex}.wav"
        stop = asyncio.Event()
        self._mic_stop = stop
        try:
            recording = await self.recorder.record(
                path, stop=stop, on_open=lambda: self.speaker.chime(self.mic_open_chime)
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
        await self.speaker.chime(self.mic_close_chime)

        if not recording.usable:
            log.info("mic heard nothing (%s, %.1fs)", recording.reason, recording.seconds)
            self._discard(path)
            return Transcript(text="", provider=getattr(self.stt, "name", ""))
        try:
            transcript = await asyncio.to_thread(self.stt.transcribe, path, self.keyterms())
        except SttError as exc:
            log.error("transcription failed: %s", exc)
            await self.speaker.speak("No pude transcribir lo que dijiste.")
            return None
        finally:
            self._discard(path)
        log.info("heard %r (confidence=%s)", transcript.text, transcript.confidence)
        return transcript

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
            return outcome

    async def _converse(
        self, item: Item, announced: str, *, first: Transcript | None = None
    ) -> str:
        menus = self.menus_for(item)
        for position, menu in enumerate(menus or [None]):
            if position:
                # The payload had more than one question; Claude shows the next
                # one the moment the previous is answered.
                await self.speaker.speak(
                    announce_mod.speakable(
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
        for _ in range(self.mic_rounds):
            if first is not None:
                transcript, first = first, None
            else:
                transcript = await self.listen()
            if transcript is None:
                return REPLY_FAILED
            intent = intents.parse(
                transcript.text,
                menu.labels if menu else (),
                multi=bool(menu and menu.multi_select),
            )

            if intent.kind in (intents.KIND_SILENCE, intents.KIND_SKIP):
                return REPLY_PENDING
            if intent.kind == intents.KIND_REPEAT:
                await self.speaker.speak(announced or "No tengo nada que repetir.")
                continue
            if intent.kind == intents.KIND_SHOW:
                await asyncio.to_thread(self.delivery.focus, item.tty)
                continue
            if intent.kind == intents.KIND_STATUS:
                await self.speak_status()
                continue
            if intent.kind == intents.KIND_PENDINGS:
                chosen = await self.speak_pendings()
                if chosen is not None and chosen.id != item.id:
                    # Unwind first: this item's mic lock is in the way, and
                    # leaving it half-answered is what `pending` is for.
                    self._switch_to = chosen.id
                    return REPLY_PENDING
                continue
            if intent.kind == intents.KIND_EXPLAIN and menu is not None:
                await self.speaker.speak(
                    announce_mod.speakable(menu.describe(intent.index), self.phonetic)
                )
                continue

            if pending_action is not None:
                if intent.kind == intents.KIND_CONFIRM:
                    await self._perform(item, pending_action)
                    return REPLY_DELIVERED
                if intent.kind == intents.KIND_CANCEL:
                    await self.speaker.speak("Listo, no mando nada.")
                    return REPLY_PENDING
                pending_action = None  # a new utterance replaces the old one

            action, gate = self._plan_action(item, menu, intent, transcript)
            if action is None:
                await self.speaker.speak(gate.reason)
                continue
            if gate.required:
                pending_action = action
                await self.speaker.speak(
                    announce_mod.speakable(
                        f"{gate.reason}. Dijiste: {self._readback(action, menu)}. ¿Lo mando?",
                        self.phonetic,
                    )
                )
                continue
            await self._perform(item, action)
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
        if menu is not None:
            return ("menu_text", (menu.free_text_index, text)), gate
        return ("text", text), gate

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

    async def speak_status(self) -> str:
        """How the whole board looks, in one breath.

        Answerable from any mode, busy included: "what is going on" is the
        question you ask precisely when you have not been listening.
        """
        try:
            sessions = roster_mod.load(self.roster_dir, interactive_only=True)
        except OSError:
            sessions = {}
        text = announce_mod.describe_status(
            windows=len(sessions),
            working=sum(1 for s in sessions.values() if s.status == roster_mod.STATUS_BUSY),
            waiting=self.store.open_count(),
            milestones=sorted(self.watcher.current().items()),
            paused=self.paused,
            busy=self.busy,
        )
        await self.speaker.speak(announce_mod.speakable(text, self.phonetic))
        return text

    async def speak_pendings(self) -> Item | None:
        """Read the queue out in order, then take a pick — by number or by name.

        The pick is what makes this more than a report: the window you choose is
        re-announced and gets the microphone, exactly as if it had just blocked.
        Summaries missing from superseded items are filled in here (issue #3) —
        a list of names with nothing after them answers nothing.
        """
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
        await self.speaker.speak(
            announce_mod.speakable(announce_mod.describe_pendings(entries), self.phonetic)
        )
        if not items:
            return None

        transcript = await self.listen()
        if transcript is None:
            return None
        intent = intents.parse(transcript.text, names)
        if intent.kind != intents.KIND_SELECT or not 1 <= (intent.index or 0) <= len(items):
            return None
        chosen = items[intent.index - 1]
        log.info("picked %s [%s] off the pendings list", chosen.id[:8], names[intent.index - 1])
        return chosen

    async def _follow_switch(self, depth: int) -> None:
        """Serve the window that was picked off the list mid-conversation.

        The pick cannot be served where it is made: that code is holding the
        microphone lock for the item being answered. So it is parked here and
        acted on once that cycle has unwound — bounded, because a chain of
        switches is still one hotkey press away from being restarted by hand.
        """
        target, self._switch_to = self._switch_to, None
        if target is None:
            return
        if depth >= MAX_SWITCHES:
            log.warning("stopping after %d window switches in one go", depth)
            return
        item = self.store.get(target)
        if item is None:
            return
        await self._announce(item, self._session_for(item), depth=depth + 1)

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
        """A mic you opened yourself answers whatever spoke last — or asks me.

        The take comes first, before deciding what it was for: "estado" and
        "dame los pendientes" are questions for voice-loop, and in busy mode
        this hotkey is the only way to ask them. Anything else is handed to the
        last window that spoke, so nothing has to be said twice.
        """
        try:
            item = self.store.last_announced()
            async with self._mic_lock:
                transcript = await self.listen()
                if transcript is None:
                    return
                intent = intents.parse(transcript.text)
                chosen = None
                if intent.kind == intents.KIND_STATUS:
                    await self.speak_status()
                    return
                if intent.kind == intents.KIND_PENDINGS:
                    chosen = await self.speak_pendings()
            if chosen is not None:
                # `_announce` follows any further switch from here itself.
                await self._announce(chosen, self._session_for(chosen))
                return
            if intent.kind == intents.KIND_PENDINGS:
                return
            if item is None or not item.tty:
                await self.speaker.speak("No hay nada pendiente.")
                return
            outcome = await self.reply_cycle(item, item.summary or "", first=transcript)
            if outcome == REPLY_DELIVERED:
                await self.speak_remaining()
            await self._follow_switch(0)
        except Exception:  # noqa: BLE001 - a background task must not die silently
            log.exception("hotkey mic failed")

    async def cmd_busy_toggle(self, args: dict) -> dict:
        """Chime instead of speak. The mic hotkey keeps working — by design."""
        self.busy = not self.busy
        self.store.kv_set(KV_BUSY, self.busy)
        log.info("busy mode %s", "on" if self.busy else "off")
        await self.speaker.chime(self.busy_chime)
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
