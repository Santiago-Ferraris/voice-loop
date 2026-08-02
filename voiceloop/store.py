"""The queue — SQLite, single owner.

Only the daemon ever opens this database. The hooks write files (see
`spool.py`); nothing else holds a handle. That is what keeps a dozen concurrent
sessions from ever waiting on a writer lock.

Lifecycle of an item:

    queued -> announcing -> pending -> resolved

`awaiting_reply` and `delivered` are declared but unused in phase 1: phase 2
inserts them between `pending` and `resolved`, and declaring them now means
that phase ships without a schema migration.

Two rules the rest of the system leans on:

* **Coalescing.** A fresh `stop` for a session that already has an unannounced
  `queued` item folds into it, keeping the original FIFO position — so a
  session that finishes twice while you are busy elsewhere doesn't announce
  twice, and doesn't jump the line either.
* **Nothing is ever dropped.** Items leave the queue only by being resolved:
  explicitly, or because the session saw real user activity. There is no
  delete path.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .events import RESOLVING_TYPES, TYPE_STOP, Event

STATE_QUEUED = "queued"
STATE_ANNOUNCING = "announcing"
STATE_PENDING = "pending"
STATE_RESOLVED = "resolved"
# Phase 2 — declared so the schema does not have to change later.
STATE_AWAITING_REPLY = "awaiting_reply"
STATE_DELIVERED = "delivered"

OPEN_STATES = (
    STATE_QUEUED,
    STATE_ANNOUNCING,
    STATE_PENDING,
    STATE_AWAITING_REPLY,
    STATE_DELIVERED,
)

RESOLVED_BY_ACTIVITY = "activity"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id              TEXT PRIMARY KEY,
    ts              INTEGER NOT NULL,
    type            TEXT NOT NULL,
    session_id      TEXT NOT NULL DEFAULT '',
    tty             TEXT NOT NULL DEFAULT '',
    cwd             TEXT NOT NULL DEFAULT '',
    transcript_path TEXT NOT NULL DEFAULT '',
    name            TEXT,
    state           TEXT NOT NULL,
    summary         TEXT,
    payload         TEXT NOT NULL DEFAULT '{}',
    announced_at    INTEGER,
    resolved_at     INTEGER,
    resolved_by     TEXT
);
CREATE INDEX IF NOT EXISTS events_state_ts ON events (state, ts);
CREATE INDEX IF NOT EXISTS events_session_state ON events (session_id, state);

CREATE TABLE IF NOT EXISTS aliases (
    session_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    confirmed  INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

INGEST_INSERTED = "inserted"
INGEST_COALESCED = "coalesced"
INGEST_DUPLICATE = "duplicate"
INGEST_RESOLVED = "resolved"


@dataclass(frozen=True)
class Item:
    id: str
    ts: int
    type: str
    session_id: str
    tty: str
    cwd: str
    transcript_path: str
    name: str | None
    state: str
    summary: str | None
    payload: dict
    announced_at: int | None
    resolved_at: int | None
    resolved_by: str | None

    @property
    def display_name(self) -> str:
        return self.name or self.session_id[:8] or "sesión"


def _row_to_item(row: sqlite3.Row) -> Item:
    try:
        payload = json.loads(row["payload"] or "{}")
    except ValueError:
        payload = {}
    return Item(
        id=row["id"],
        ts=row["ts"],
        type=row["type"],
        session_id=row["session_id"],
        tty=row["tty"],
        cwd=row["cwd"],
        transcript_path=row["transcript_path"],
        name=row["name"],
        state=row["state"],
        summary=row["summary"],
        payload=payload if isinstance(payload, dict) else {},
        announced_at=row["announced_at"],
        resolved_at=row["resolved_at"],
        resolved_by=row["resolved_by"],
    )


class Store:
    def __init__(self, path: Path | str, *, timeout: float = 5.0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=timeout, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=3000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- ingest ------------------------------------------------------------

    def ingest(self, event: Event, *, now: int | None = None) -> str:
        """Fold one spool event into the queue. Idempotent on `event.id`."""
        stamp = int(time.time()) if now is None else now

        if self._exists(event.id):
            return INGEST_DUPLICATE

        if event.type in RESOLVING_TYPES:
            self.resolve_session(event.session_id, RESOLVED_BY_ACTIVITY, now=stamp)
            return INGEST_RESOLVED

        if event.type == TYPE_STOP:
            target = self._coalescible_stop(event.session_id)
            if target is not None:
                self._conn.execute(
                    """
                    UPDATE events
                       SET id = ?, type = ?, tty = ?, cwd = ?, transcript_path = ?,
                           payload = ?, summary = NULL
                     WHERE id = ?
                    """,
                    (
                        event.id,
                        event.type,
                        event.tty or target.tty,
                        event.cwd or target.cwd,
                        event.transcript_path or target.transcript_path,
                        json.dumps(event.payload, ensure_ascii=False),
                        target.id,
                    ),
                )
                return INGEST_COALESCED

        self._conn.execute(
            """
            INSERT INTO events (id, ts, type, session_id, tty, cwd, transcript_path,
                                state, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.ts,
                event.type,
                event.session_id,
                event.tty,
                event.cwd,
                event.transcript_path,
                STATE_QUEUED,
                json.dumps(event.payload, ensure_ascii=False),
            ),
        )
        return INGEST_INSERTED

    def _exists(self, event_id: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone()
        return row is not None

    def _coalescible_stop(self, session_id: str) -> Item | None:
        if not session_id:
            return None
        row = self._conn.execute(
            """
            SELECT * FROM events
             WHERE session_id = ? AND type = ? AND state = ? AND announced_at IS NULL
             ORDER BY ts, rowid
             LIMIT 1
            """,
            (session_id, TYPE_STOP, STATE_QUEUED),
        ).fetchone()
        return _row_to_item(row) if row else None

    # -- queue reads --------------------------------------------------------

    def get(self, event_id: str) -> Item | None:
        row = self._conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return _row_to_item(row) if row else None

    def next_queued(self) -> Item | None:
        row = self._conn.execute(
            "SELECT * FROM events WHERE state = ? ORDER BY ts, rowid LIMIT 1",
            (STATE_QUEUED,),
        ).fetchone()
        return _row_to_item(row) if row else None

    def queued_items(self) -> list[Item]:
        """Everything still waiting to be announced, in FIFO order."""
        rows = self._conn.execute(
            "SELECT * FROM events WHERE state = ? ORDER BY ts, rowid",
            (STATE_QUEUED,),
        ).fetchall()
        return [_row_to_item(row) for row in rows]

    def last_announced(self) -> Item | None:
        """Most recently announced item that is still open — what `replay` repeats."""
        placeholders = ",".join("?" for _ in OPEN_STATES)
        row = self._conn.execute(
            f"""
            SELECT * FROM events
             WHERE announced_at IS NOT NULL AND state IN ({placeholders})
             ORDER BY announced_at DESC, rowid DESC
             LIMIT 1
            """,
            OPEN_STATES,
        ).fetchone()
        return _row_to_item(row) if row else None

    def pendings(self) -> list[Item]:
        """Everything still waiting on the user, oldest first."""
        placeholders = ",".join("?" for _ in OPEN_STATES)
        rows = self._conn.execute(
            f"SELECT * FROM events WHERE state IN ({placeholders}) ORDER BY ts, rowid",
            OPEN_STATES,
        ).fetchall()
        return [_row_to_item(row) for row in rows]

    def open_count(self) -> int:
        placeholders = ",".join("?" for _ in OPEN_STATES)
        row = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM events WHERE state IN ({placeholders})",
            OPEN_STATES,
        ).fetchone()
        return int(row["n"])

    def queued_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE state = ?", (STATE_QUEUED,)
        ).fetchone()
        return int(row["n"])

    # -- queue writes -------------------------------------------------------

    def mark_announcing(self, event_id: str) -> None:
        self._conn.execute(
            "UPDATE events SET state = ? WHERE id = ? AND state = ?",
            (STATE_ANNOUNCING, event_id, STATE_QUEUED),
        )

    def mark_pending(self, event_id: str, *, now: int | None = None) -> None:
        stamp = int(time.time()) if now is None else now
        self._conn.execute(
            "UPDATE events SET state = ?, announced_at = ? WHERE id = ?",
            (STATE_PENDING, stamp, event_id),
        )

    def requeue(self, event_id: str) -> None:
        """Put an item back at its original FIFO position (announce failed, or `replay`)."""
        self._conn.execute(
            "UPDATE events SET state = ?, announced_at = NULL WHERE id = ? AND state != ?",
            (STATE_QUEUED, event_id, STATE_RESOLVED),
        )

    def set_name(self, event_id: str, name: str | None) -> None:
        self._conn.execute("UPDATE events SET name = ? WHERE id = ?", (name, event_id))

    def set_summary(self, event_id: str, summary: str | None) -> None:
        self._conn.execute("UPDATE events SET summary = ? WHERE id = ?", (summary, event_id))

    def resolve(self, event_id: str, by: str, *, now: int | None = None) -> int:
        stamp = int(time.time()) if now is None else now
        cursor = self._conn.execute(
            """
            UPDATE events SET state = ?, resolved_at = ?, resolved_by = ?
             WHERE id = ? AND state != ?
            """,
            (STATE_RESOLVED, stamp, by, event_id, STATE_RESOLVED),
        )
        return cursor.rowcount

    def resolve_session(self, session_id: str, by: str, *, now: int | None = None) -> int:
        if not session_id:
            return 0
        stamp = int(time.time()) if now is None else now
        placeholders = ",".join("?" for _ in OPEN_STATES)
        cursor = self._conn.execute(
            f"""
            UPDATE events SET state = ?, resolved_at = ?, resolved_by = ?
             WHERE session_id = ? AND state IN ({placeholders})
            """,
            (STATE_RESOLVED, stamp, by, session_id, *OPEN_STATES),
        )
        return cursor.rowcount

    # -- aliases ------------------------------------------------------------

    def set_alias(self, session_id: str, name: str, *, confirmed: bool = False) -> None:
        self._conn.execute(
            """
            INSERT INTO aliases (session_id, name, confirmed, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET name = excluded.name,
                                                  confirmed = excluded.confirmed
            """,
            (session_id, name, 1 if confirmed else 0, int(time.time())),
        )

    def get_alias(self, session_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT name FROM aliases WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row["name"] if row else None

    # -- kv -----------------------------------------------------------------

    def kv_set(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    # -- startup reconciliation --------------------------------------------

    def recover_in_flight(self) -> int:
        """After a restart, anything caught mid-announce goes back in the queue."""
        cursor = self._conn.execute(
            "UPDATE events SET state = ? WHERE state = ?",
            (STATE_QUEUED, STATE_ANNOUNCING),
        )
        return cursor.rowcount

    def resolve_sessions_missing(
        self, live_session_ids: Iterable[str], by: str, *, now: int | None = None
    ) -> int:
        """Resolve items whose session is no longer in the roster (window closed)."""
        live = set(live_session_ids)
        resolved = 0
        for item in self.pendings():
            if item.session_id and item.session_id not in live:
                resolved += self.resolve(item.id, by, now=now)
        return resolved
