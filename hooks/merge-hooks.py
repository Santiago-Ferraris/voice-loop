#!/usr/bin/env python3
"""Merge (or remove) voice-loop's hooks in a Claude Code settings.json.

Used by install.sh / uninstall.sh, and runnable on its own:

    python3 hooks/merge-hooks.py --settings ~/.claude/settings.json \
                                 --hook-script /path/to/hooks/vl-hook.sh
    python3 hooks/merge-hooks.py --settings ~/.claude/settings.json --remove

Two properties matter more than anything else here, because this edits a file
the user's fifteen live sessions read:

* **Idempotent.** Running it again is a no-op — same bytes out, no second
  backup. Re-running after moving the clone rewrites the stale path instead of
  adding a duplicate hook.
* **Surgical.** Only entries whose command points at `vl-hook.sh` are touched.
  Every other hook, and every unrelated setting, is preserved verbatim, and a
  timestamped backup is written before the first real change.

Stdlib only: this runs before the virtualenv exists.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
import time

MARKER = "vl-hook.sh"

# (hook event, matcher or None, argument passed to vl-hook.sh)
HOOKS = (
    ("Stop", None, "stop"),
    ("Notification", None, "notification"),
    ("UserPromptSubmit", None, "activity"),
    ("PreToolUse", "AskUserQuestion|ExitPlanMode", "menu"),
    ("PostToolUse", "AskUserQuestion|ExitPlanMode", "activity"),
    ("PostToolUse", "Bash", "bash"),
)


def _is_ours(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("command"), str)
        and MARKER in entry["command"]
    )


def _matcher_of(group: dict) -> str | None:
    matcher = group.get("matcher")
    if matcher in (None, "", "*"):
        return None
    return matcher


def strip_hooks(settings: dict) -> dict:
    """Remove every voice-loop hook, leaving everything else untouched."""
    result = copy.deepcopy(settings)
    hooks = result.get("hooks")
    if not isinstance(hooks, dict):
        return result

    for event in list(hooks.keys()):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            entries = group.get("hooks")
            if not isinstance(entries, list):
                kept_groups.append(group)
                continue
            kept = [entry for entry in entries if not _is_ours(entry)]
            if entries and not kept:
                # The group existed only for us — drop it entirely.
                continue
            kept_groups.append(dict(group, hooks=kept))
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]

    if not hooks:
        result.pop("hooks", None)
    return result


def merge_hooks(settings: dict, hook_script: str) -> dict:
    """Install our hooks, replacing any previous voice-loop entry."""
    result = strip_hooks(settings)
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit("settings.json: 'hooks' is not an object; refusing to touch it")

    for event, matcher, kind in HOOKS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise SystemExit(f"settings.json: hooks.{event} is not a list; refusing to touch it")
        target = None
        for group in groups:
            if isinstance(group, dict) and _matcher_of(group) == matcher:
                target = group
                break
        if target is None:
            target = {"hooks": []} if matcher is None else {"matcher": matcher, "hooks": []}
            groups.append(target)
        entries = target.setdefault("hooks", [])
        if not isinstance(entries, list):
            raise SystemExit(f"settings.json: hooks.{event}[].hooks is not a list")
        entries.append({"type": "command", "command": f"{hook_script} {kind}"})
    return result


def load_settings(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        text = handle.read().strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SystemExit(f"{path}: not valid JSON ({exc}); fix it before installing")
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a JSON object at the top level")
    return data


def dump(settings: dict) -> str:
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="merge voice-loop hooks into settings.json")
    parser.add_argument("--settings", required=True)
    parser.add_argument("--hook-script", default="")
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args(argv)

    if not options.remove and not options.hook_script:
        parser.error("--hook-script is required unless --remove is given")

    path = os.path.expanduser(options.settings)
    before = load_settings(path)
    after = strip_hooks(before) if options.remove else merge_hooks(before, options.hook_script)

    if after == before and os.path.exists(path):
        # Nothing to change — leave the user's file exactly as it is, formatting
        # included, and do not spawn a pointless backup.
        print("hooks: already up to date")
        return 0

    rendered = dump(after)
    current = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            current = handle.read()

    if rendered == current:
        print("hooks: already up to date")
        return 0
    if options.dry_run:
        print(rendered, end="")
        return 0

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if os.path.exists(path):
        backup = f"{path}.voice-loop-backup-{time.strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(path, backup)
        print(f"hooks: backed up {path} -> {backup}")

    temp = f"{path}.voice-loop.tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(rendered)
    os.replace(temp, path)
    print(f"hooks: {'removed from' if options.remove else 'installed into'} {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
