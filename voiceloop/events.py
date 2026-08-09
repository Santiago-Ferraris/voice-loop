"""The spool event — schema v1.

One of these is written as a JSON file by `hooks/vl-hook.sh` on every Claude
hook firing, and read back by the daemon. The hook writes the JSON by hand (it
runs on stdlib python3, without this package installed), so this module is the
*reader's* contract: anything that does not validate is quarantined rather than
crashing the ingest loop.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

SCHEMA_VERSION = 1

TYPE_STOP = "stop"
TYPE_MENU = "menu"
TYPE_NOTIFICATION = "notification"
TYPE_ACTIVITY = "activity"
TYPE_MILESTONE = "milestone"

EVENT_TYPES = frozenset(
    {TYPE_STOP, TYPE_MENU, TYPE_NOTIFICATION, TYPE_ACTIVITY, TYPE_MILESTONE}
)

# Event types that resolve whatever the session had outstanding.
RESOLVING_TYPES = frozenset({TYPE_ACTIVITY})

PR_CREATE_MILESTONE = "PR created"

_WHITESPACE = re.compile(r"\s+")

# Claude's `Notification` hook fires for two different things and says which in
# `message`: "Claude needs your permission to use Bash" is a window that cannot
# move until you answer, and "Claude is waiting for your input" is a nudge that
# repeats for as long as you leave a window alone — most often the one you are
# already sitting in front of.
#
# Claude Code's own English wording, not the user's, which is what makes it
# matchable at all. Narrow on purpose: this decides what gets *muted*, so a
# message nobody recognises has to fall through as a block, not as noise.
_IDLE_NOTIFICATION = re.compile(r"waiting for your input", re.IGNORECASE)


class EventError(ValueError):
    """A spool payload that does not satisfy schema v1."""


def is_pr_create_command(command: Any) -> bool:
    """True when a Bash command creates a pull request.

    Matched on the collapsed command line, so `gh  pr\\n  create` and
    `cd repo && gh pr create --fill` both count.
    """
    if not isinstance(command, str):
        return False
    return "gh pr create" in _WHITESPACE.sub(" ", command).strip()


def is_idle_notification(message: Any) -> bool:
    """True for the nudge, false for the block — and false for anything new.

    `announce.notification_events: false` turns on this classifier and nothing
    else: only what reads as an idle nudge goes quiet. A permission prompt still
    speaks, and so does a wording that has not been seen before, because an
    unrecognised notification is far more likely to be a new kind of block than
    a new kind of noise.
    """
    return isinstance(message, str) and bool(_IDLE_NOTIFICATION.search(message))


def _require_str(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise EventError(f"{key}: expected a non-empty string, got {value!r}")
    return value


def _optional_str(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise EventError(f"{key}: expected a string, got {value!r}")
    return value


@dataclass(frozen=True)
class Event:
    id: str
    ts: int
    type: str
    session_id: str
    tty: str = ""
    cwd: str = ""
    transcript_path: str = ""
    payload: dict = field(default_factory=dict)
    v: int = SCHEMA_VERSION

    @classmethod
    def new(
        cls,
        type: str,
        session_id: str,
        *,
        tty: str = "",
        cwd: str = "",
        transcript_path: str = "",
        payload: Mapping[str, Any] | None = None,
        ts: int | None = None,
    ) -> "Event":
        if type not in EVENT_TYPES:
            raise EventError(f"type: unknown event type {type!r}")
        return cls(
            id=str(uuid.uuid4()),
            ts=int(time.time()) if ts is None else int(ts),
            type=type,
            session_id=session_id,
            tty=tty,
            cwd=cwd,
            transcript_path=transcript_path,
            payload=dict(payload or {}),
        )

    @classmethod
    def from_dict(cls, raw: Any) -> "Event":
        if not isinstance(raw, Mapping):
            raise EventError(f"expected a JSON object, got {type(raw).__name__}")

        version = raw.get("v")
        if version != SCHEMA_VERSION:
            raise EventError(f"v: unsupported schema version {version!r}")

        event_type = _require_str(raw, "type")
        if event_type not in EVENT_TYPES:
            raise EventError(f"type: unknown event type {event_type!r}")

        ts = raw.get("ts")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            raise EventError(f"ts: expected a number, got {ts!r}")

        payload = raw.get("payload")
        if payload is None:
            payload = {}
        if not isinstance(payload, Mapping):
            raise EventError(f"payload: expected an object, got {payload!r}")

        return cls(
            id=_require_str(raw, "id"),
            ts=int(ts),
            type=event_type,
            session_id=_optional_str(raw, "session_id"),
            tty=_optional_str(raw, "tty"),
            cwd=_optional_str(raw, "cwd"),
            transcript_path=_optional_str(raw, "transcript_path"),
            payload=dict(payload),
        )

    def to_dict(self) -> dict:
        return {
            "v": self.v,
            "id": self.id,
            "ts": self.ts,
            "type": self.type,
            "session_id": self.session_id,
            "tty": self.tty,
            "cwd": self.cwd,
            "transcript_path": self.transcript_path,
            "payload": dict(self.payload),
        }
