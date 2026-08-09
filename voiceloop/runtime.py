"""The installed runtime — where launchd is allowed to look, and why not the clone.

macOS TCC guards `~/Documents`, `~/Desktop` and `~/Downloads`. A LaunchAgent is
not a Terminal, has no consent for any of them, and does not merely fail to
*read* files there — it cannot **execute** them either. A clone in `~/Documents`
therefore produces exactly this, forever, with the plist perfectly valid:

    /bin/sh: …/bin/voice-loopd: Operation not permitted   (exit 126)

So `install.sh` copies everything launchd touches — the wrappers, the config
defaults and the virtualenv — into `~/.local/share/voice-loop`, which no
protection covers, and points the plist there. The clone stays for development.

That buys a second problem: two copies of the same program. `fingerprint` and
the manifest are how they stay honest — `voice-loopctl`, run from the clone,
compares them and says so when the daemon is running code you already changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from . import __version__
from .control import ControlError, is_running, request

RUNTIME_DIR_ENV = "VOICE_LOOP_RUNTIME_DIR"
DEFAULT_RUNTIME_DIR = "~/.local/share/voice-loop"
MANIFEST_NAME = "manifest.json"
PLIST_LABEL = "com.voiceloop.daemon"

# The three folders macOS hides from a LaunchAgent. Nothing the plist names may
# live under one of them — that is the whole reason this module exists.
PROTECTED_DIR_NAMES = ("Documents", "Desktop", "Downloads")

# What has to be identical between the clone and the runtime for the daemon to
# be running the code you are reading. Hooks are deliberately absent: they run
# from the clone as children of your terminal, which does have TCC consent.
FINGERPRINT_FILES = (
    "pyproject.toml",
    "config.example.yml",
    "config.local.yml",
    "config.local.yaml",
    "bin/voice-loopd",
    "bin/voice-loopctl",
)
FINGERPRINT_GLOBS = ("voiceloop/*.py",)

_PLIST_STRING = re.compile(r"<string>([^<]*)</string>")


class TccError(RuntimeError):
    """A path launchd would be denied at runtime was about to be written into the plist."""


def _norm(path: Path | str) -> Path:
    """Absolute *and* symlink-free.

    The wrappers hand us `pwd -P` output while `$HOME` may still be the
    unresolved spelling (`/tmp` vs `/private/tmp` on macOS). Comparing the two
    without resolving makes the runtime look like a foreign clone to itself.
    """
    expanded = Path(os.path.expanduser(str(path)))
    try:
        return expanded.resolve()
    except OSError:  # pragma: no cover - symlink loop
        return Path(os.path.abspath(expanded))


def _expand(raw: str, env: Mapping[str, str]) -> Path:
    """`~` expansion against the *given* environment, not the ambient one."""
    if raw.startswith("~"):
        home = env.get("HOME")
        if home:
            return Path(home + raw[1:])
    return Path(raw).expanduser()


def runtime_dir(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    return _expand(source.get(RUNTIME_DIR_ENV) or DEFAULT_RUNTIME_DIR, source)


# --- TCC ------------------------------------------------------------------


def is_tcc_protected(path: Path | str, home: Path | str) -> bool:
    """True when launchd would be denied access to `path`."""
    candidates = {_norm(path)}
    try:
        candidates.add(Path(path).expanduser().resolve())
    except OSError:  # pragma: no cover - resolve() is non-strict, but symlink loops exist
        pass

    guarded = []
    for name in PROTECTED_DIR_NAMES:
        guarded.append(_norm(Path(home) / name))
        try:
            guarded.append((Path(home) / name).expanduser().resolve())
        except OSError:  # pragma: no cover
            pass

    return any(
        candidate == directory or candidate.is_relative_to(directory)
        for candidate in candidates
        for directory in guarded
    )


def plist_paths(body: str) -> list[str]:
    """Every absolute path a rendered plist names, PATH entries included."""
    found = []
    for value in _PLIST_STRING.findall(body):
        for part in value.split(":"):
            if part.startswith("/"):
                found.append(part)
    return found


DEFAULT_LABEL = "com.voiceloop.daemon"


def render_plist(
    template: str,
    *,
    runtime: Path | str,
    home: Path | str,
    state_dir: Path | str,
    label: str = DEFAULT_LABEL,
) -> str:
    """Substitute the launchd template and refuse to hand launchd a path it cannot use."""
    body = (
        template.replace("__RUNTIME__", str(runtime))
        .replace("__HOME__", str(home))
        .replace("__STATE_DIR__", str(state_dir))
        .replace("__LABEL__", str(label or DEFAULT_LABEL))
    )
    offenders = sorted({path for path in plist_paths(body) if is_tcc_protected(path, home)})
    if offenders:
        raise TccError(
            "launchd cannot use these paths — macOS keeps LaunchAgents out of "
            + ", ".join(f"~/{name}" for name in PROTECTED_DIR_NAMES)
            + ":\n  "
            + "\n  ".join(offenders)
        )
    return body


# --- fingerprint & manifest ----------------------------------------------


def fingerprint_members(root: Path | str) -> list[str]:
    root = Path(root)
    names = {name for name in FINGERPRINT_FILES if (root / name).is_file()}
    for pattern in FINGERPRINT_GLOBS:
        for path in sorted(root.glob(pattern)):
            if path.is_file():
                names.add(path.relative_to(root).as_posix())
    return sorted(names)


def fingerprint(root: Path | str) -> str:
    """A digest of the files that decide what the daemon actually runs."""
    root = Path(root)
    digest = hashlib.sha256()
    for name in fingerprint_members(root):
        try:
            content = hashlib.sha256((root / name).read_bytes()).hexdigest()
        except OSError as exc:
            # An unreadable file (TCC again) must not crash a status line.
            content = f"unreadable:{type(exc).__name__}"
        digest.update(f"{name}\0{content}\n".encode("utf-8"))
    return digest.hexdigest()


def manifest_path(runtime: Path | str) -> Path:
    return Path(runtime) / MANIFEST_NAME


def read_manifest(runtime: Path | str) -> dict | None:
    try:
        raw = manifest_path(runtime).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def write_manifest(runtime: Path | str, source: Path | str, *, mode: str = "venv") -> dict:
    manifest = {
        "version": __version__,
        "source": str(_norm(source)),
        "fingerprint": fingerprint(source),
        "mode": mode,
        "installed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    path = manifest_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def staleness_warning(
    source_root: Path | str | None = None,
    runtime: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Why the daemon is not running the code in this clone — or None when it is.

    The trap this closes: you fix a bug, re-run `voice-loopctl`, and debug the
    old code because the runtime is a copy and nobody told you.
    """
    installed = _norm(runtime if runtime is not None else runtime_dir(env))
    source = _norm(source_root if source_root is not None else Path(__file__).resolve().parent.parent)
    if source == installed:
        # This *is* the runtime (the copied wrapper); there is nothing to drift from.
        return None

    manifest = read_manifest(installed)
    if manifest is None:
        return None
    installed_from = manifest.get("source")
    if not installed_from:
        return None

    if _norm(installed_from) != source:
        return (
            f"the installed runtime was built from {installed_from}, not from {source} — "
            "changes here are not live. Run ./install.sh from this clone to take it over."
        )
    if manifest.get("fingerprint") != fingerprint(source):
        return (
            f"the installed runtime is out of date (built {manifest.get('installed_at', '?')}) — "
            f"the daemon is running older code than this clone. Run {source}/install.sh."
        )
    return None


# --- talking to a daemon that may not be there yet ------------------------


def wait_for_daemon(socket_path: Path | str, timeout: float = 20.0, interval: float = 0.25) -> dict:
    """Block until the daemon answers `status`, and return what it said.

    `install.sh` uses this to stop claiming success while the agent is dead.
    """
    deadline = time.monotonic() + max(timeout, 0.0)
    last: Exception | None = None
    while True:
        try:
            response = request(socket_path, "status", timeout=2.0)
            if response.get("ok") and isinstance(response.get("data"), dict):
                return response["data"]
            last = ControlError(str(response.get("error", "status failed")))
        except (ControlError, OSError) as exc:
            last = exc
        if time.monotonic() >= deadline:
            raise TimeoutError(f"no daemon on {socket_path}: {last}")
        time.sleep(interval)


def stop_daemon(socket_path: Path | str, timeout: float = 5.0) -> bool:
    """Ask whoever owns the socket to exit, and wait for it to let go.

    A daemon started by hand (`nohup bin/voice-loopd &`) keeps the socket, and
    launchd's copy then dies with "already running" straight into a restart
    loop — while `status` keeps answering, so the install looks fine.
    """
    try:
        request(socket_path, "restart", timeout=2.0)
    except (ControlError, OSError):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running(socket_path):
            return True
        time.sleep(0.1)
    return not is_running(socket_path)


# --- CLI (install.sh) -----------------------------------------------------


def _cmd_render_plist(options: argparse.Namespace) -> int:
    template = Path(options.template).read_text(encoding="utf-8")
    body = render_plist(
        template,
        runtime=options.runtime,
        home=options.home,
        state_dir=options.state_dir,
        label=options.label,
    )
    if options.output == "-":
        sys.stdout.write(body)
    else:
        Path(options.output).parent.mkdir(parents=True, exist_ok=True)
        Path(options.output).write_text(body, encoding="utf-8")
    return 0


def _cmd_write_manifest(options: argparse.Namespace) -> int:
    manifest = write_manifest(options.runtime, options.source, mode=options.mode)
    print(manifest["fingerprint"])
    return 0


def _cmd_wait_for_daemon(options: argparse.Namespace) -> int:
    try:
        status = wait_for_daemon(options.socket, timeout=options.timeout)
    except TimeoutError as exc:
        print(f"voice-loop: {exc}", file=sys.stderr)
        return 1
    print(status.get("pid", ""))
    return 0


def _cmd_stop_daemon(options: argparse.Namespace) -> int:
    return 0 if stop_daemon(options.socket) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m voiceloop.runtime", description="installer-side helpers"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    render = sub.add_parser("render-plist")
    render.add_argument("--template", required=True)
    render.add_argument("--output", required=True, help="target path, or - for stdout")
    render.add_argument("--runtime", required=True)
    render.add_argument("--home", required=True)
    render.add_argument("--state-dir", required=True)
    render.add_argument("--label", default=DEFAULT_LABEL)
    render.set_defaults(func=_cmd_render_plist)

    manifest = sub.add_parser("write-manifest")
    manifest.add_argument("--runtime", required=True)
    manifest.add_argument("--source", required=True)
    manifest.add_argument("--mode", default="venv")
    manifest.set_defaults(func=_cmd_write_manifest)

    wait = sub.add_parser("wait-for-daemon")
    wait.add_argument("--socket", required=True)
    wait.add_argument("--timeout", type=float, default=20.0)
    wait.set_defaults(func=_cmd_wait_for_daemon)

    stop = sub.add_parser("stop-daemon")
    stop.add_argument("--socket", required=True)
    stop.set_defaults(func=_cmd_stop_daemon)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    options = build_parser().parse_args(argv)
    try:
        return options.func(options)
    except TccError as exc:
        print(f"voice-loop: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"voice-loop: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
