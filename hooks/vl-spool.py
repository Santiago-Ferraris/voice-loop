#!/usr/bin/env python3
"""Write one spool file from a Claude Code hook payload.

    vl-spool.py <kind> <state_dir> <tty>   # hook JSON on stdin

Called only by `vl-hook.sh`. Stdlib only and no imports from `voiceloop`: this
runs on whatever `python3` the session has, before (and regardless of) the
daemon's virtualenv.

Never raises and never blocks. The event format is schema v1, the same one
`voiceloop/events.py` validates on the way in.
"""

import json
import os
import sys
import time
import uuid

SCHEMA_VERSION = 1
MAX_STR = 8000
MAX_ITEMS = 64
MAX_DEPTH = 6
PR_CREATE_MILESTONE = "PR created"


def clip(node, depth=0):
    """Bound the payload: a plan or a tool result can be enormous."""
    if isinstance(node, str):
        return node[:MAX_STR]
    if depth > MAX_DEPTH:
        return None
    if isinstance(node, dict):
        return {str(k): clip(v, depth + 1) for k, v in list(node.items())[:MAX_ITEMS]}
    if isinstance(node, list):
        return [clip(v, depth + 1) for v in node[:MAX_ITEMS]]
    if node is None or isinstance(node, (bool, int, float)):
        return node
    return str(node)[:MAX_STR]


def build_event(kind, raw, tty):
    """(event dict) or None when this hook firing is not interesting."""
    tool_input = raw.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    tool_name = raw.get("tool_name") if isinstance(raw.get("tool_name"), str) else ""

    if kind == "bash":
        command = tool_input.get("command")
        command = command if isinstance(command, str) else ""
        if "gh pr create" not in " ".join(command.split()):
            return None
        event_type = "milestone"
        payload = {"label": PR_CREATE_MILESTONE, "command": command[:MAX_STR]}
    elif kind == "menu":
        event_type = "menu"
        payload = {"tool": tool_name, "tool_input": clip(tool_input)}
    elif kind == "notification":
        message = raw.get("message")
        event_type = "notification"
        payload = {"message": message[:MAX_STR] if isinstance(message, str) else ""}
    elif kind == "activity":
        event_type = "activity"
        trigger = tool_name or raw.get("hook_event_name")
        payload = {"trigger": trigger if isinstance(trigger, str) else ""}
    elif kind == "stop":
        event_type = "stop"
        payload = {}
    else:
        return None

    def text(key):
        value = raw.get(key)
        return value if isinstance(value, str) else ""

    return {
        "v": SCHEMA_VERSION,
        "id": str(uuid.uuid4()),
        "ts": int(time.time()),
        "type": event_type,
        "session_id": text("session_id"),
        "tty": tty,
        "cwd": text("cwd"),
        "transcript_path": text("transcript_path"),
        "payload": payload,
    }


def write_event(state_dir, event):
    """Temp file + rename: the reader never sees a partial event."""
    spool = os.path.join(state_dir, "spool")
    os.makedirs(spool, exist_ok=True)
    name = "%019d-%d-%s.json" % (time.time_ns(), os.getpid(), os.urandom(4).hex())
    temp = os.path.join(spool, "." + name + ".tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        handle.flush()
        os.fsync(handle.fileno())
    final = os.path.join(spool, name)
    os.rename(temp, final)
    return final


def main(argv):
    if len(argv) < 3:
        return 0
    kind, state_dir = argv[1], argv[2]
    tty = argv[3] if len(argv) > 3 else ""

    try:
        raw = json.load(sys.stdin)
        if not isinstance(raw, dict):
            raw = {}
    except Exception:
        raw = {}

    event = build_event(kind, raw, tty)
    if event is None:
        return 0

    try:
        write_event(state_dir, event)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:
        sys.exit(0)
