from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

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
    """
    monkeypatch.setenv("VOICE_LOOP_RUNTIME_DIR", str(tmp_path / "not-installed"))


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
