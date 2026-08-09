from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from voiceloop.audio import Recording
from voiceloop.daemon import Daemon
from voiceloop.milestones import MilestoneWatcher
from voiceloop.store import Store
from voiceloop.stt.mock import MockStt
from voiceloop import iterm

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(autouse=True)
def isolated_runtime_dir(monkeypatch, tmp_path):
    """Never let a test read the runtime actually installed on this machine.

    Otherwise `voice-loopctl`'s drift warning would fire — or not — depending on
    whether the developer running the suite has run install.sh today.

    Same for the env file: `doctor` reads it now, so a developer with a real
    DEEPGRAM_API_KEY on disk would silently pass a test that asserts the key is
    missing — and it would fail on anyone else's machine.
    """
    monkeypatch.setenv("VOICE_LOOP_RUNTIME_DIR", str(tmp_path / "not-installed"))
    monkeypatch.setenv("VOICE_LOOP_ENV_FILE", str(tmp_path / "no-env-file"))


def audio_payload(*, private: bool) -> bytes:
    """What `system_profiler` says about a Mac on AirPods, or on its speakers."""
    device = {
        "_name": "AirPods Pro" if private else "MacBook Pro Speakers",
        "coreaudio_default_audio_output_device": "spaudio_yes",
        "coreaudio_device_transport": (
            "coreaudio_device_type_bluetooth" if private else "coreaudio_device_type_builtin"
        ),
        "coreaudio_output_source": "spaudio_default" if private else "MacBook Pro Speakers",
    }
    return json.dumps({"SPAudioDataType": [{"_items": [device], "_name": "coreaudio_device"}]}).encode()


def audio_output(*, private: bool = False):
    """An output probe that answers without asking this machine anything."""
    from voiceloop.output import OutputProbe

    payload = audio_payload(private=private)
    return OutputProbe(runner=lambda: payload)


@pytest.fixture(autouse=True)
def speakers_by_default(monkeypatch):
    """No test's behaviour may depend on whether headphones are plugged in here.

    The daemon probes the real audio output, so without this the suite would
    take the barge-in path on a developer wearing AirPods and the echo path on
    everyone else — the same test, two different code paths, neither reported.
    """
    from voiceloop import output as output_mod

    monkeypatch.setattr(output_mod, "_run_probe", lambda: audio_payload(private=False))


@pytest.fixture
def clean_env(monkeypatch):
    """No provider keys leak in from the developer's shell."""
    for var in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


@pytest.fixture
def config(clean_env, tmp_path):
    """Shipped defaults, with state redirected into a tmp dir."""
    from voiceloop import config as config_mod

    local = tmp_path / "config.local.yml"
    local.write_text(
        f"paths:\n  state_dir: {tmp_path / 'state'}\n",
        encoding="utf-8",
    )
    return config_mod.load(repo_root=REPO_ROOT, local_path=local)


@pytest.fixture
def sock_path():
    """A unix socket path short enough for AF_UNIX's 104-byte limit.

    pytest's tmp_path lives under /var/folders/... on macOS, which blows the
    limit on its own.
    """
    directory = tempfile.mkdtemp(prefix="vl", dir="/tmp")
    try:
        yield Path(directory) / "d.sock"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def store(tmp_path):
    from voiceloop.store import Store

    with Store(tmp_path / "queue.db") as opened:
        yield opened


class TimedRunner:
    """Records when each process started and finished, so overlap is measurable."""

    def __init__(self, durations: dict[str, float] | None = None):
        self.durations = durations or {}
        self.spans: list[list] = []

    async def __call__(self, argv):
        index = len(self.spans)
        self.spans.append([argv[0], time.monotonic(), None])
        await asyncio.sleep(self.durations.get(argv[0], 0.0))
        self.spans[index][2] = time.monotonic()
        return 0

    def spans_of(self, binary: str) -> list[list]:
        """Every run of that binary, in the order they started."""
        return [span for span in self.spans if span[0] == binary]


def chime_file(tmp_path, name: str = "ping") -> str:
    sound = tmp_path / f"{name}.aiff"
    sound.write_bytes(b"fake audio")
    return str(sound)


class FakeFloor:
    """The mic's hold on a `FakeSpeaker`, with the same cue-then-release shape."""

    def __init__(self, speaker: "FakeSpeaker"):
        self._speaker = speaker
        self._held = True

    @property
    def held(self) -> bool:
        return self._held

    async def cue(self, name) -> bool:
        try:
            return await self.play(name)
        finally:
            self.release()

    async def play(self, name) -> bool:
        self._speaker.chimes.append(name)
        return True

    async def say(self, text) -> bool:
        return await self._speaker.speak(text)

    async def announce(self, announcement) -> None:
        await self._speaker.announce(announcement)

    def release(self) -> None:
        self._held = False


class FakeSpeaker:
    """Records what would have been said, in order, chimes included."""

    voice = "system"

    def __init__(self):
        self.said: list = []
        self.spoken: list[str] = []
        self.chimes: list = []
        self.floors = 0
        self.interruptions = 0

    async def announce(self, announcement):
        self.said.append(announcement)

    async def speak(self, text: str) -> bool:
        self.spoken.append(text)
        return True

    async def chime(self, name) -> bool:
        self.chimes.append(name)
        return True

    async def interrupt(self) -> bool:
        self.interruptions += 1
        return True

    @contextlib.asynccontextmanager
    async def floor(self):
        self.floors += 1
        floor = FakeFloor(self)
        try:
            yield floor
        finally:
            floor.release()

    @property
    def texts(self) -> list[str]:
        return [item.text for item in self.said if not item.silent]


def write_roster(directory: Path, **overrides) -> Path:
    """A roster entry for a session that is alive (this test process)."""
    directory.mkdir(parents=True, exist_ok=True)
    entry = {
        "pid": os.getpid(),
        "sessionId": "session-1",
        "cwd": "/tmp/projects/workspace",
        "kind": "interactive",
        "name": "workspace-21",
        "status": "idle",
        "version": "2.1.220",
    }
    entry.update(overrides)
    path = directory / f"{entry['pid']}-{entry['sessionId']}.json"
    path.write_text(json.dumps(entry), encoding="utf-8")
    return path


# --- the daemon under test, with both ends faked ---------------------------


TTY = "/dev/ttys012"


class StubRecorder:
    """A mic that always captures something, unless told otherwise."""

    binary = "ffmpeg"
    device = ":0"
    available = True

    def __init__(self, *, spoke: bool = True, error: Exception | None = None):
        self.spoke = spoke
        self.error = error
        self.takes = 0
        # How long each take was given, in order. A mic that opened under a
        # sentence gets the grace; one that opened on its own gets the timeout.
        self.windows: list = []
        # Whether each take ignored what it heard until the voice stopped.
        self.armings: list = []
        # Whether each take was told to report the first syllable (barge-in).
        self.barges: list = []

    async def record(
        self,
        destination,
        *,
        stop=None,
        on_open=None,
        speech_timeout=None,
        speech=None,
        arm_after_open=False,
    ):
        self.takes += 1
        self.windows.append(speech_timeout)
        self.armings.append(arm_after_open)
        self.barges.append(speech is not None)
        if self.error is not None:
            raise self.error
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0" * 200_000)
        if on_open is not None:
            await on_open()
        return Recording(path=path, seconds=1.0, spoke=self.spoke, reason="silence")


class TimedRecorder(StubRecorder):
    """A mic that remembers when it was actually spawned."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.started_at: float | None = None

    async def record(self, destination, **kwargs):
        self.started_at = time.monotonic()
        return await super().record(destination, **kwargs)


class RecordingDelivery:
    """Everything that would have gone into someone else's window."""

    def __init__(self, *, alive: bool = True):
        self._alive = alive
        self.sent: list[tuple] = []
        self.focused: list[str] = []

    def alive(self, tty: str) -> bool:
        return self._alive

    def _guard(self, tty: str) -> None:
        if not self._alive:
            raise iterm.SessionGone(f"no iTerm2 session on {tty}")

    def send_text(self, tty, text):
        self._guard(tty)
        self.sent.append(("text", tty, text))

    def send_choice(self, tty, index):
        self._guard(tty)
        self.sent.append(("choice", tty, index))

    def send_choices(self, tty, indexes):
        self._guard(tty)
        self.sent.append(("choices", tty, tuple(indexes)))

    def send_menu_text(self, tty, index, text):
        self._guard(tty)
        self.sent.append(("menu_text", tty, index, text))

    def focus(self, tty):
        self.focused.append(tty)
        return True


@pytest.fixture
def build(config, tmp_path):
    roster = tmp_path / "sessions"
    roster.mkdir()
    write_roster(roster, sessionId="session-1", name="indice", kind="interactive")
    made: list[Daemon] = []

    def factory(
        replies, *, recorder=None, delivery=None, speaker=None, confidence=0.99, **kwargs
    ):
        engine = MockStt(replies=list(replies), confidence=confidence)
        subject = Daemon(
            config,
            store=Store(tmp_path / f"queue{len(made)}.db"),
            speaker=speaker or FakeSpeaker(),
            watcher=MilestoneWatcher(),
            roster_dir=roster,
            recorder=recorder or StubRecorder(),
            stt=engine,
            delivery=delivery or RecordingDelivery(),
            **kwargs,
        )
        made.append(subject)
        return subject

    try:
        yield factory
    finally:
        for subject in made:
            subject.store.close()
