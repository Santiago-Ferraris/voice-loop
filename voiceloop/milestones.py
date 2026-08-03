"""Optional bridge: milestones from an external phase file.

Some setups already track a per-terminal workflow phase in a small file (one
file per tty, whose contents are the phase name). If you point voice-loop at
that directory, a transition into a phase you listed becomes a chime — no
speech, no queue item you have to answer.

Off by default, and deliberately read-only: nothing here writes to, or depends
on, whatever produces those files.

The one thing that matters for it to be usable at all: **baseline on startup**.
A directory of fifteen files that are already in the `ci` phase must produce
silence when the daemon starts, not fifteen chimes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

DEFAULT_PATTERN = "*.phase"


@dataclass(frozen=True)
class Milestone:
    key: str
    phase: str
    label: str


@dataclass
class MilestoneWatcher:
    enabled: bool = False
    directory: Path | None = None
    pattern: str = DEFAULT_PATTERN
    milestones: dict = field(default_factory=dict)
    _seen: dict = field(default_factory=dict, init=False, repr=False)
    _baselined: bool = field(default=False, init=False, repr=False)

    @classmethod
    def from_config(cls, config) -> "MilestoneWatcher":
        section = config.get("integrations.milestone_file_watch") or {}
        directory = section.get("dir")
        milestones = section.get("milestones") or {}
        return cls(
            enabled=bool(section.get("enabled", False)),
            directory=Path(directory).expanduser() if directory else None,
            pattern=str(section.get("pattern") or DEFAULT_PATTERN),
            milestones={str(k): str(v) for k, v in dict(milestones).items()},
        )

    @property
    def active(self) -> bool:
        return bool(self.enabled and self.directory and self.milestones)

    def _scan(self) -> dict:
        if not self.active or self.directory is None:
            return {}
        current: dict = {}
        try:
            entries = sorted(self.directory.glob(self.pattern))
        except OSError:
            return {}
        for entry in entries:
            if not entry.is_file():
                continue
            try:
                value = entry.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            current[entry.name] = value
        return current

    def baseline(self) -> None:
        """Snapshot what's already there, so startup is silent."""
        self._seen = self._scan()
        self._baselined = True

    def current(self) -> dict[str, int]:
        """How many watched files sit in each milestone phase right now.

        `poll` reports transitions and forgets them; the spoken status needs the
        standing picture — "two with CI green" — which is only in the files.
        Empty when the bridge is off, and the caller says nothing rather than
        inventing a zero.
        """
        counts: dict[str, int] = {}
        for phase in self._scan().values():
            label = self.milestones.get(phase)
            if label:
                counts[label] = counts.get(label, 0) + 1
        return counts

    def poll(self) -> Iterator[Milestone]:
        """Milestones since the last poll. Empty until `baseline()` has run."""
        if not self.active:
            return
        if not self._baselined:
            self.baseline()
            return
        current = self._scan()
        for key, phase in current.items():
            previous = self._seen.get(key)
            if phase == previous:
                continue
            label = self.milestones.get(phase)
            if label:
                yield Milestone(key=key, phase=phase, label=label)
        self._seen = current
