"""The spool — a maildir-style handoff between the hooks and the daemon.

Every hook writes one small JSON file and exits; the daemon is the only reader.
That is the whole reason the ~15 concurrent Claude sessions never contend on
anything: no locks, no database handle, no daemon required to be running. A
write is `create temp file` + `rename`, which is atomic on APFS, so the reader
can never observe a half-written event.

Files are named `<ts_ns>-<pid>-<rand>.json` and read back in that order, which
is the arrival order the queue's FIFO depends on.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterable

from .events import Event, EventError

SUFFIX = ".json"
BAD_DIRNAME = "bad"


def ensure_dirs(spool_dir: Path | str) -> Path:
    path = Path(spool_dir)
    path.mkdir(parents=True, exist_ok=True)
    (path / BAD_DIRNAME).mkdir(exist_ok=True)
    return path


def filename_for(*, pid: int | None = None, ts_ns: int | None = None) -> str:
    stamp = time.time_ns() if ts_ns is None else ts_ns
    owner = os.getpid() if pid is None else pid
    return f"{stamp:019d}-{owner}-{os.urandom(4).hex()}{SUFFIX}"


def write(spool_dir: Path | str, event: Event) -> Path:
    """Write one event atomically. Returns the final path."""
    directory = ensure_dirs(spool_dir)
    name = filename_for()
    final = directory / name
    # Leading dot keeps the partial file out of the reader's glob.
    temp = directory / f".{name}.tmp"
    payload = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.rename(temp, final)
    return final


def _sort_key(path: Path) -> tuple[int, str]:
    head = path.name.split("-", 1)[0]
    try:
        return (int(head), path.name)
    except ValueError:
        return (0, path.name)


def list_files(spool_dir: Path | str) -> list[Path]:
    """Every complete spool file, in arrival order."""
    directory = Path(spool_dir)
    if not directory.is_dir():
        return []
    files = [
        entry
        for entry in directory.iterdir()
        if entry.is_file() and entry.suffix == SUFFIX and not entry.name.startswith(".")
    ]
    return sorted(files, key=_sort_key)


def quarantine(path: Path | str, reason: str = "") -> Path | None:
    """Move an unreadable spool file into `bad/` so it is not re-read forever."""
    source = Path(path)
    bad_dir = source.parent / BAD_DIRNAME
    try:
        bad_dir.mkdir(exist_ok=True)
        target = bad_dir / source.name
        if target.exists():
            target = bad_dir / f"{source.stem}-{time.time_ns()}{source.suffix}"
        os.rename(source, target)
        if reason:
            target.with_suffix(target.suffix + ".why").write_text(reason, encoding="utf-8")
        return target
    except OSError:
        return None


def read_pending(spool_dir: Path | str) -> list[tuple[Path, Event]]:
    """Parse every spool file in order; quarantine the ones that don't validate.

    The file is *not* removed — the caller discards it once the event is durably
    stored, so a crash between reading and storing replays instead of losing.
    """
    out: list[tuple[Path, Event]] = []
    for path in list_files(spool_dir):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            quarantine(path, f"unreadable: {exc}")
            continue
        try:
            event = Event.from_dict(raw)
        except EventError as exc:
            quarantine(path, f"schema: {exc}")
            continue
        out.append((path, event))
    return out


def discard(paths: Iterable[Path | str]) -> None:
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass
