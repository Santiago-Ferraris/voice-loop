"""Does this process have the permissions it needs?

macOS grants microphone and Automation access to the *responsible* process, and
under launchd that is the agent, not your terminal. So "it worked when I ran it
by hand" proves nothing about the daemon, and the daemon failing proves nothing
about your clone. The same checks therefore run in both places:

    voice-loopctl doctor

runs them locally — which is also what makes macOS show you the two consent
dialogs, since a LaunchAgent may never get the chance to — and then asks the
daemon to run them from where it lives. Two columns, and the difference between
them is the bug.

Everything here is synchronous, short, and never raises: a check that blows up
is a failed check, not a failed daemon.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import iterm
from .audio import MIN_USABLE_BYTES, ffmpeg_argv
from .config import ENV_VAR_BY_SERVICE

PROBE_SECONDS = 1.0

# What a hung capture actually means, said once and reused by the daemon's own
# spoken warning.
CONSENT_TIMEOUT = (
    "capture timed out — waiting on the macOS microphone consent prompt. "
    "Answer it, or tick this process in System Settings → Privacy & Security "
    "→ Microphone, then run doctor again"
)

OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == OK

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def check_ffmpeg(binary: str = "ffmpeg") -> Check:
    found = shutil.which(binary)
    if not found:
        return Check("ffmpeg", FAILED, f"{binary} is not on PATH — brew install ffmpeg")
    return Check("ffmpeg", OK, found)


def check_microphone(
    *, binary: str = "ffmpeg", device: str = ":0", seconds: float = PROBE_SECONDS
) -> Check:
    """Record a second of audio. A TCC denial fails here and nowhere else."""
    if not shutil.which(binary):
        return Check("microphone", SKIPPED, "no ffmpeg")
    with tempfile.TemporaryDirectory(prefix="voiceloop-probe") as directory:
        target = Path(directory) / "probe.wav"
        argv = ffmpeg_argv(
            target, binary=binary, device=device, max_seconds=seconds, silence_detect=False
        )
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                argv, capture_output=True, text=True, timeout=seconds + 20, check=False
            )
        except subprocess.TimeoutExpired:
            # A one-second capture cannot take twenty. It is not slow, it is
            # parked on the consent prompt — and reporting that as a bare
            # timeout is indistinguishable from a broken ffmpeg.
            return Check("microphone", FAILED, CONSENT_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            return Check("microphone", FAILED, str(exc))
        size = target.stat().st_size if target.exists() else 0
        if size < MIN_USABLE_BYTES:
            tail = (completed.stderr or "").strip().splitlines()[-1:] or ["no audio captured"]
            return Check("microphone", FAILED, tail[0])
        return Check("microphone", OK, f"{device}, {size} bytes in {seconds:g}s")


def check_iterm(runner=None) -> Check:
    """Automation permission for iTerm2. Denial is AppleScript error -1743."""
    ok, detail = iterm.scripting_status(runner)
    return Check("iterm automation", OK if ok else FAILED, detail)


def check_stt(engine, *, env_file=None) -> Check:
    """A missing key names the file it is missing from, or there is no check.

    "API key missing from the environment" is true and useless: the key lives in
    `~/.config/voice-loop/env`, `voice-loopctl` does not source it, and the
    daemon does — so the same sentence meant three different things depending on
    which column you read it in. With the file parsed (see `envfile`), the three
    states are told apart by name.
    """
    if engine is None:
        return Check("speech-to-text", FAILED, "no provider — see speech_to_text.provider")
    if not engine.available:
        var = ENV_VAR_BY_SERVICE.get(engine.name)
        if env_file is not None and var:
            return Check("speech-to-text", FAILED, f"{engine.name}: {env_file.why_missing(var)}")
        return Check("speech-to-text", FAILED, f"{engine.name}: API key missing from the environment")
    return Check("speech-to-text", OK, engine.name)


def run_all(
    *, binary: str = "ffmpeg", device: str = ":0", engine=None, runner=None, env_file=None
) -> list[Check]:
    return [
        check_ffmpeg(binary),
        check_microphone(binary=binary, device=device),
        check_iterm(runner),
        check_stt(engine, env_file=env_file),
    ]
