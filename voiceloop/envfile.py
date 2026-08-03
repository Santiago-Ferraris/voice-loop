"""The file the keys live in, read by the one command that has to see them.

`bin/voice-loopd` sources `~/.config/voice-loop/env` before exec'ing the daemon,
because launchd does not inherit your shell and a key in `.zshrc` would never
reach it. `bin/voice-loopctl` deliberately does *not* source it: talking to a
socket needs no key, and the fewer processes that hold one the better.

`doctor` is the exception, and the reason this module exists. Its whole job is
answering "can this machine transcribe", which it cannot do without seeing what
the daemon sees — and reporting a configured key as missing is worse than not
checking at all, because it is the command you run when the microphone is
broken and it sends you to fix the wrong thing.

So the file is parsed here rather than sourced by the wrapper: the key stays
out of the environment of every other subcommand, and the check can tell the
three real states apart — no file, file without the key, key present.

This is a parser, not a shell: `KEY=value`, an optional `export`, `#` comments,
and one level of quoting. Anything cleverer in that file (command substitution,
`$VAR` expansion) is honoured by `voice-loopd`, which really does source it, and
ignored here.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

DEFAULT_PATH = "~/.config/voice-loop/env"
PATH_VAR = "VOICE_LOOP_ENV_FILE"

_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


@dataclass(frozen=True)
class EnvFile:
    path: Path
    exists: bool
    values: dict[str, str] = field(default_factory=dict)

    def has(self, name: str) -> bool:
        return bool(self.values.get(name))

    def why_missing(self, name: str) -> str:
        """A sentence that names the file, not just the absence."""
        if not self.exists:
            return f"no env file at {self.path} — put {name}=… there"
        if not self.has(name):
            return f"{name} is not in {self.path}"
        return f"{name} is in {self.path} but the engine did not take it"


def path_for(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    return Path(os.path.expanduser(source.get(PATH_VAR) or DEFAULT_PATH))


def unquote(raw: str) -> str:
    """Strip one matching pair of quotes, and an unquoted trailing comment."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value.split(" #")[0].strip()


def parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (text or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _ASSIGNMENT.match(line)
        if match is None:
            continue
        values[match.group(1)] = unquote(match.group(2))
    return values


def read(path: Path | str | None = None, *, env: Mapping[str, str] | None = None) -> EnvFile:
    """Never raises: an unreadable env file is a missing one, and doctor says so."""
    target = Path(os.path.expanduser(str(path))) if path is not None else path_for(env)
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return EnvFile(path=target, exists=target.is_file(), values={})
    return EnvFile(path=target, exists=True, values=parse(text))


def overlay(env_file: EnvFile, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment the daemon would have — the file wins, as `set -a; . file` does.

    Returned rather than exported: `doctor` needs one config object to see the
    key, not a process whose environment has been quietly rewritten underneath
    everything else it does.
    """
    merged = dict(os.environ if environ is None else environ)
    merged.update({name: value for name, value in env_file.values.items() if value})
    return merged
