"""Configuration loading.

`config.example.yml` (shipped, committed) is the single source of defaults;
`config.local.yml` (gitignored) is deep-merged on top of it, so a local file
only needs the keys it overrides.

API keys are deliberately *not* part of this: they are read from the process
environment and nowhere else. A config file that carries something key-shaped
is rejected outright rather than silently ignored, so a public repo can never
grow a committed secret by accident.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULTS_FILENAME = "config.example.yml"
LOCAL_FILENAMES = ("config.local.yml", "config.local.yaml")

STT_PROVIDERS = frozenset({"deepgram", "deepgram_ws", "openai", "whisper-cpp", "mock"})
SUMMARY_PROVIDERS = frozenset({"openai", "none"})

# Keys that must never appear in a config file — they belong in the environment.
SECRET_KEY_NAMES = frozenset({"api_key", "apikey", "key", "token", "secret", "password"})

# Values under these locations are path-like and get `~` expanded.
PATH_LOCATIONS = (("paths",), ("integrations", "milestone_file_watch", "dir"))

ENV_VAR_BY_SERVICE = {
    "openai": "OPENAI_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
}


class ConfigError(Exception):
    """Raised when a config file is malformed, or carries something it must not."""


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict:
    """Recursive dict merge. Non-dict values (including lists) replace wholesale."""
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _assert_no_secrets(node: Any, trail: tuple[str, ...] = ()) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            name = str(key).lower().replace("-", "_")
            if name in SECRET_KEY_NAMES:
                where = ".".join(trail + (str(key),))
                raise ConfigError(
                    f"{where}: API keys must come from the environment "
                    f"(e.g. {', '.join(sorted(ENV_VAR_BY_SERVICE.values()))}), not from config"
                )
            _assert_no_secrets(value, trail + (str(key),))
    elif isinstance(node, list):
        for item in node:
            _assert_no_secrets(item, trail)


def _expand_paths(data: dict) -> None:
    for location in PATH_LOCATIONS:
        node: Any = data
        for step in location[:-1]:
            node = node.get(step) if isinstance(node, Mapping) else None
            if node is None:
                break
        if not isinstance(node, dict):
            continue
        leaf = location[-1]
        target = node.get(leaf)
        if isinstance(target, dict):
            for key, value in target.items():
                if isinstance(value, str):
                    target[key] = os.path.expanduser(value)
        elif isinstance(target, str):
            node[leaf] = os.path.expanduser(target)


def _load_yaml(path: Path) -> dict:
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return raw


@dataclass(frozen=True)
class Config:
    data: dict = field(default_factory=dict)
    source_paths: tuple[Path, ...] = ()
    # Where `api_key` looks, when it must not be the process environment.
    # `doctor` sets this to the env file the daemon would have sourced; nothing
    # else does, so the default stays "the environment and nowhere else".
    env: Mapping[str, str] | None = None

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for step in dotted.split("."):
            if not isinstance(node, Mapping) or step not in node:
                return default
            node = node[step]
        return node

    # -- derived paths ----------------------------------------------------

    @property
    def state_dir(self) -> Path:
        return Path(self.get("paths.state_dir", "~/.local/state/voice-loop")).expanduser()

    @property
    def spool_dir(self) -> Path:
        return self.state_dir / "spool"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "queue.db"

    @property
    def socket_path(self) -> Path:
        return self.state_dir / "daemon.sock"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    # -- secrets ----------------------------------------------------------

    def api_key(self, service: str, env: Mapping[str, str] | None = None) -> str | None:
        """Read a provider key from the environment. Never from config."""
        var = ENV_VAR_BY_SERVICE.get(service)
        if var is None:
            return None
        source = env if env is not None else (self.env if self.env is not None else os.environ)
        value = (source.get(var) or "").strip()
        return value or None


def _validate(data: Mapping[str, Any]) -> None:
    stt = data.get("speech_to_text") or {}
    provider = stt.get("provider")
    if provider is not None and provider not in STT_PROVIDERS:
        raise ConfigError(
            f"speech_to_text.provider: unknown provider {provider!r} "
            f"(expected one of {', '.join(sorted(STT_PROVIDERS))})"
        )

    summaries = data.get("summaries") or {}
    summary_provider = summaries.get("provider")
    if summary_provider is not None and summary_provider not in SUMMARY_PROVIDERS:
        raise ConfigError(
            f"summaries.provider: unknown provider {summary_provider!r} "
            f"(expected one of {', '.join(sorted(SUMMARY_PROVIDERS))})"
        )

    max_words = summaries.get("max_words")
    if max_words is not None and (not isinstance(max_words, int) or max_words <= 0):
        raise ConfigError("summaries.max_words: expected a positive integer")

    timeout = summaries.get("timeout_seconds")
    if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
        raise ConfigError("summaries.timeout_seconds: expected a positive number")

    threshold = (data.get("delivery") or {}).get("confirm_below_confidence")
    if threshold is not None and (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not 0 <= threshold <= 1
    ):
        raise ConfigError("delivery.confirm_below_confidence: expected a number between 0 and 1")

    microphone = data.get("microphone") or {}
    for key in ("max_seconds", "open_timeout_seconds"):
        value = microphone.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0
        ):
            raise ConfigError(f"microphone.{key}: expected a positive number")


def load(repo_root: Path | str | None = None, local_path: Path | str | None = None) -> Config:
    """Load defaults + local overrides into a validated `Config`."""
    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    defaults_path = root / DEFAULTS_FILENAME
    if not defaults_path.is_file():
        raise ConfigError(f"missing {DEFAULTS_FILENAME} at {defaults_path}")

    sources = [defaults_path]
    data = _load_yaml(defaults_path)

    candidates: list[Path]
    if local_path is not None:
        candidates = [Path(local_path)]
    else:
        candidates = [root / name for name in LOCAL_FILENAMES]
    for candidate in candidates:
        if candidate.is_file():
            data = _deep_merge(data, _load_yaml(candidate))
            sources.append(candidate)
            break

    _assert_no_secrets(data)
    _validate(data)
    _expand_paths(data)
    return Config(data=data, source_paths=tuple(sources))
