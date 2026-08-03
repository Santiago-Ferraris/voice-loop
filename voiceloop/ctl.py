"""`voice-loopctl` — the CLI over the control socket.

Two commands are more than a thin wrapper. `mic-toggle` and `busy-toggle` are
what the global hotkeys run, so they answer immediately and let the daemon do
the talking; and `doctor` runs its permission checks **twice** — once here, in
your terminal, and once inside the daemon — because macOS grants microphone
and Automation access per responsible process and those are two different
processes. Running it here is also what makes the consent dialogs appear at
all, which a LaunchAgent may never manage on its own.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import __version__
from .config import ConfigError, load as load_config
from .control import ControlError, request
from .runtime import staleness_warning

COMMANDS = (
    "status",
    "pendings",
    "pause",
    "resume",
    "replay",
    "skip",
    "milestone",
    "restart",
    "mic-toggle",
    "busy-toggle",
    "doctor",
)

# Answered locally; the daemon is asked separately, and may not be up at all.
LOCAL_COMMANDS = ("doctor",)


def _format_status(data: dict) -> str:
    lines = [
        f"voice-loop {data.get('version', '?')}  pid {data.get('pid', '?')}"
        f"  up {data.get('uptime_seconds', 0)}s",
        f"  paused:     {data.get('paused')}",
        f"  busy:       {data.get('busy')}",
        f"  queued:     {data.get('queued')}",
        f"  open:       {data.get('open')}",
        f"  summaries:  {data.get('summaries')}",
        f"  speech in:  {data.get('speech_to_text')}",
        f"  mic:        {data.get('mic')}",
        f"  voice:      {data.get('voice')}",
        f"  milestones: {'watching' if data.get('milestone_watch') else 'off'}",
        f"  state:      {data.get('state_dir')}",
    ]
    return "\n".join(lines)


def _format_checks(title: str, checks: list) -> str:
    lines = [title]
    for check in checks:
        mark = "ok  " if check.get("status") == "ok" else check.get("status", "?")[:4].ljust(4)
        lines.append(f"  [{mark}] {check.get('name', '?'):<16} {check.get('detail', '')}")
    return "\n".join(lines)


def run_doctor(options) -> int:
    """Local checks first (they trigger the consent dialogs), then the daemon's."""
    from . import preflight
    from .stt import SttNotImplemented, create as create_stt

    try:
        config = load_config(repo_root=options.repo_root, local_path=options.config)
    except ConfigError as exc:
        print(f"voice-loopctl: {exc}", file=sys.stderr)
        return 2

    try:
        engine = create_stt(config)
    except SttNotImplemented as exc:
        engine = None
        print(f"voice-loopctl: {exc}", file=sys.stderr)

    local = preflight.run_all(
        binary=str(config.get("microphone.ffmpeg", "ffmpeg")),
        device=str(config.get("microphone.device", ":0")),
        engine=engine,
    )
    print(_format_checks("here (your terminal):", [check.as_dict() for check in local]))

    socket_path = options.socket or str(config.socket_path)
    try:
        response = request(socket_path, "selfcheck", {})
    except (ControlError, OSError) as exc:
        print(f"\ndaemon: unreachable on {socket_path}: {exc}")
        return 1 if all(check.ok for check in local) else 2
    if not response.get("ok"):
        print(f"\ndaemon: {response.get('error', 'failed')}")
        return 1
    remote = response.get("data") or []
    print("\n" + _format_checks("there (the daemon):", remote))
    failures = [check for check in local if not check.ok]
    failures += [check for check in remote if check.get("status") != "ok"]
    return 1 if failures else 0


def _format_pendings(items: list) -> str:
    if not items:
        return "nothing pending"
    lines = []
    for index, item in enumerate(items, start=1):
        stamp = datetime.fromtimestamp(item.get("ts", 0), tz=timezone.utc).astimezone()
        name = item.get("name") or (item.get("session_id", "")[:8] or "?")
        summary = item.get("summary") or ""
        lines.append(
            f"{index:>2}. [{item.get('state', '?'):<10}] {stamp:%H:%M} "
            f"{name} ({item.get('type')}){' — ' + summary if summary else ''}"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voice-loopctl", description="control voice-loop")
    parser.add_argument("--config", default=None, help="path to config.local.yml")
    parser.add_argument("--repo-root", default=None, help="directory holding config.example.yml")
    parser.add_argument("--socket", default=None, help="override the control socket path")
    parser.add_argument("--json", action="store_true", help="print the raw response")
    parser.add_argument("--version", action="version", version=__version__)

    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in COMMANDS:
        command = sub.add_parser(name)
        if name in ("replay", "skip"):
            command.add_argument("id", nargs="?", help="event id; defaults to the last one")
        if name == "milestone":
            command.add_argument("label", help="what to chime about")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(argv)

    # The daemon runs an installed copy of this clone. Say so before anything
    # else when the two have drifted — otherwise the next hour goes into
    # debugging a bug that is already fixed on disk.
    drift = staleness_warning(options.repo_root)
    if drift:
        print(f"voice-loopctl: {drift}", file=sys.stderr)

    if options.cmd in LOCAL_COMMANDS:
        return run_doctor(options)

    socket_path = options.socket
    if socket_path is None:
        try:
            socket_path = str(
                load_config(repo_root=options.repo_root, local_path=options.config).socket_path
            )
        except ConfigError as exc:
            print(f"voice-loopctl: {exc}", file=sys.stderr)
            return 2

    args: dict = {}
    if options.cmd in ("replay", "skip") and getattr(options, "id", None):
        args["id"] = options.id
    if options.cmd == "milestone":
        args["label"] = options.label

    try:
        response = request(socket_path, options.cmd, args)
    except (ControlError, OSError) as exc:
        print(f"voice-loopctl: daemon unreachable on {socket_path}: {exc}", file=sys.stderr)
        return 1

    if options.json:
        print(json.dumps(response, ensure_ascii=False, indent=2))
        return 0 if response.get("ok") else 1

    if not response.get("ok"):
        print(f"voice-loopctl: {response.get('error', 'failed')}", file=sys.stderr)
        return 1

    data = response.get("data")
    if options.cmd == "status" and isinstance(data, dict):
        print(_format_status(data))
    elif options.cmd == "pendings" and isinstance(data, list):
        print(_format_pendings(data))
    elif isinstance(data, (dict, list)):
        print(json.dumps(data, ensure_ascii=False))
    else:
        print(data if data is not None else "ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
