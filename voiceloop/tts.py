"""Speech out — macOS `say`, plus `afplay` for chimes.

Offline, free, and instant, which is the whole reason it beats a cloud voice
here: the announcement has to land while you are still wondering why a tab went
quiet.

Everything is serialized behind one lock. Two sessions finishing at the same
instant must not talk over each other — that is the failure mode this whole
project exists to avoid.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Sequence

SYSTEM_VOICE = "system"
SOUNDS_DIRS = ("/System/Library/Sounds", "/Library/Sounds")
SOUND_SUFFIXES = (".aiff", ".aif", ".wav", ".m4a")

Runner = Callable[[Sequence[str]], Awaitable[int]]


async def run_process(argv: Sequence[str]) -> int:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await process.wait()


def resolve_sound(name: str | None) -> Path | None:
    """A configured chime name (`Glass`) or an absolute path to a sound file."""
    if not name:
        return None
    candidate = Path(name).expanduser()
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None
    for directory in SOUNDS_DIRS:
        for suffix in SOUND_SUFFIXES:
            path = Path(directory) / f"{name}{suffix}"
            if path.is_file():
                return path
    return None


class Speaker:
    def __init__(
        self,
        *,
        voice: str = SYSTEM_VOICE,
        rate: int | None = None,
        runner: Runner | None = None,
        say_binary: str = "say",
        play_binary: str = "afplay",
    ):
        self.voice = voice
        self.rate = rate
        self._runner = runner or run_process
        self._say = say_binary
        self._play = play_binary
        self._lock = asyncio.Lock()

    @classmethod
    def from_config(cls, config, *, runner: Runner | None = None) -> "Speaker":
        rate = config.get("text_to_speech.rate")
        return cls(
            voice=str(config.get("text_to_speech.voice", SYSTEM_VOICE)),
            rate=int(rate) if isinstance(rate, (int, float)) else None,
            runner=runner,
        )

    def say_argv(self, text: str) -> list[str]:
        argv = [self._say]
        if self.voice and self.voice != SYSTEM_VOICE:
            argv += ["-v", self.voice]
        if self.rate:
            argv += ["-r", str(self.rate)]
        # `--` keeps a sentence that happens to start with a dash from being
        # read as a flag. The text is an exec argument, never a shell string.
        argv += ["--", text]
        return argv

    async def _run(self, argv: Sequence[str]) -> int:
        try:
            return await self._runner(argv)
        except (OSError, asyncio.CancelledError):
            raise
        except Exception:  # noqa: BLE001 - a dead speaker must not stall the queue
            return -1

    async def chime(self, name: str | None) -> bool:
        sound = resolve_sound(name)
        if sound is None:
            return False
        async with self._lock:
            try:
                return await self._run([self._play, str(sound)]) == 0
            except OSError:
                return False

    async def speak(self, text: str) -> bool:
        if not (text or "").strip():
            return False
        async with self._lock:
            try:
                return await self._run(self.say_argv(text)) == 0
            except OSError:
                return False

    async def announce(self, announcement) -> None:
        """Chime, then speak — in that order, never overlapping another item."""
        await self.chime(announcement.chime)
        if not announcement.silent:
            await self.speak(announcement.text)
