"""Reading a Claude Code session transcript (JSONL).

Two things are needed from it:

* **The background-subagent gate.** When a turn ends with async agents still
  running, the user has nothing to answer yet, so the `stop` must not be
  announced. Ported from the same computation `tabcolor.sh` has been using:
  count `launched` ids minus `completed` ids. Every failure mode — missing
  file, unreadable, malformed line — returns **0**, i.e. fail *open*: we would
  rather announce something the user didn't need than swallow a real request
  because of a parse error.

* **The tail**, i.e. the last thing the assistant actually said, which is what
  gets summarised into the announcement.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator

LAUNCH_RE = re.compile(r"Async agent launched successfully")
AGENT_ID_RE = re.compile(r"agentId:\s*([0-9a-f]+)")
TASK_ID_RE = re.compile(r"<task-id>\s*([0-9a-f]+)\s*</task-id>")
TASK_NOTIFICATION = "<task-notification>"

DEFAULT_TAIL_CHARS = 2000


def _message_texts(message: Any) -> str:
    """Flatten every string a transcript message might carry into one blob."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    out: list[str] = []
    if isinstance(content, str):
        out.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if isinstance(block.get("text"), str):
                out.append(block["text"])
            nested = block.get("content")
            if isinstance(nested, str):
                out.append(nested)
            elif isinstance(nested, list):
                for inner in nested:
                    if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                        out.append(inner["text"])
    return "\n".join(out)


def _records(path: Path) -> Iterator[dict]:
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def pending_subagents(transcript_path: str | Path | None) -> int:
    """In-flight background agents. Any uncertainty answers 0 (fail-open)."""
    if not transcript_path:
        return 0
    path = Path(transcript_path)
    launched: set[str] = set()
    completed: set[str] = set()
    try:
        if not path.is_file():
            return 0
        for record in _records(path):
            text = _message_texts(record.get("message"))
            if not text:
                continue
            if LAUNCH_RE.search(text):
                launched.update(AGENT_ID_RE.findall(text))
            if TASK_NOTIFICATION in text:
                completed.update(TASK_ID_RE.findall(text))
    except OSError:
        return 0
    except Exception:  # noqa: BLE001 - never let a parse bug suppress an announcement
        return 0
    return max(0, len(launched - completed))


def _assistant_text(record: dict) -> str:
    """Plain prose from an assistant record — no thinking, no tool calls."""
    if record.get("type") != "assistant" or record.get("isSidechain"):
        return ""
    message = record.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return "\n".join(parts).strip()


def tail_text(
    transcript_path: str | Path | None, *, max_chars: int = DEFAULT_TAIL_CHARS
) -> str:
    """The last thing the assistant said in the main thread. '' when unavailable."""
    if not transcript_path:
        return ""
    path = Path(transcript_path)
    latest = ""
    try:
        if not path.is_file():
            return ""
        for record in _records(path):
            text = _assistant_text(record)
            if text:
                latest = text
    except OSError:
        return ""
    except Exception:  # noqa: BLE001 - a bad transcript degrades to no summary
        return ""
    if len(latest) > max_chars:
        # Keep the end: the ask is almost always in the last paragraph.
        latest = latest[-max_chars:]
    return latest
