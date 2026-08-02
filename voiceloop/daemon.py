"""The daemon — the only process that owns state, and the only one that talks.

Three cooperating loops:

* **ingest** (250 ms) drains the spool into SQLite. Cheap, and the only thing
  that has to keep up with fifteen sessions firing hooks at once.
* **announce** walks the queue in FIFO order and speaks the first item that is
  actually ready. "Ready" is where the subagent gate lives: a turn that ended
  with background agents still running is skipped — silently, keeping its
  place — until they finish.
* **milestones** (optional) polls external phase files for chime-only events.

Everything slow runs off the loop: transcript parsing and the summary call go
through `to_thread`, and speech is serialized behind the speaker's own lock.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__, announce as announce_mod, roster as roster_mod, spool
from .config import Config, ConfigError, load as load_config
from .control import ControlError, ControlServer, DaemonAlreadyRunning
from .events import TYPE_MILESTONE, TYPE_STOP, Event
from .milestones import MilestoneWatcher
from .store import Item, Store
from .summarize import Summarizer
from .transcript import pending_subagents, tail_text
from .tts import Speaker

INGEST_INTERVAL = 0.25
ANNOUNCE_INTERVAL = 0.2
MILESTONE_INTERVAL = 1.0

RESOLVED_BY_MILESTONE = "milestone"
RESOLVED_BY_BACKGROUND = "background-session"
RESOLVED_BY_GONE = "session-gone"

KV_PAUSED = "paused"

NOT_IMPLEMENTED = {
    "mic-toggle": "phase 2",
    "busy-toggle": "phase 3",
}

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

        self.phonetic = config.get("text_to_speech.phonetic") or {}
        self.blocking_chime = config.get("announce.blocking_chime")
        self.milestone_chime = config.get("announce.milestone_chime")
        self.notification_events = bool(config.get("announce.notification_events", True))

        self.started_at = time.time()
        self.paused = bool(self.store.kv_get(KV_PAUSED, False))
        self._stop = asyncio.Event()
        self._restart = False
        self._gate_cache: dict[str, tuple[tuple[int, int], int]] = {}
        self._server: ControlServer | None = None

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
            for task in tasks:
                task.cancel()
            for task in tasks:
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
        self.store.set_summary(item.id, summary)
        return summary

    async def _announce(self, item: Item, session) -> None:
        self.store.mark_announcing(item.id)
        name = self._name_for(item, session)
        self.store.set_name(item.id, name)
        summary = await self._summary_for(item)

        announcement = announce_mod.build(
            item,
            name=name,
            summary=summary,
            remaining=self.store.queued_count(),
            phonetic=self.phonetic,
            blocking_chime=self.blocking_chime,
            milestone_chime=self.milestone_chime,
            notification_events=self.notification_events,
        )
        log.info("announce %s [%s] %s", item.type, name, announcement.text)
        await self.speaker.announce(announcement)

        if item.type == TYPE_MILESTONE:
            # Chime only — there is nothing for the user to answer.
            self.store.resolve(item.id, RESOLVED_BY_MILESTONE)
        else:
            self.store.mark_pending(item.id)

    # -- control surface ---------------------------------------------------

    async def dispatch(self, cmd: str, args: dict) -> Any:
        if cmd in NOT_IMPLEMENTED:
            raise ControlError(f"not implemented: {cmd} lands in {NOT_IMPLEMENTED[cmd]}")
        handler = getattr(self, f"cmd_{cmd.replace('-', '_')}", None)
        if handler is None:
            raise ControlError(f"unknown command: {cmd}")
        return handler(args)

    def cmd_status(self, args: dict) -> dict:
        return {
            "version": __version__,
            "pid": os.getpid(),
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "paused": self.paused,
            "queued": self.store.queued_count(),
            "open": self.store.open_count(),
            "state_dir": str(self.state_dir),
            "summaries": "openai" if self.summarizer.available else "fallback",
            "voice": self.speaker.voice,
            "milestone_watch": self.watcher.active,
        }

    def cmd_pendings(self, args: dict) -> list[dict]:
        return [
            {
                "id": item.id,
                "ts": item.ts,
                "type": item.type,
                "state": item.state,
                "name": item.name or "",
                "session_id": item.session_id,
                "tty": item.tty,
                "summary": item.summary or "",
                "announced_at": item.announced_at,
            }
            for item in self.store.pendings()
        ]

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
