"""skhd/install-hotkeys.sh, run for real against a throwaway skhdrc.

The script edits a file the user wrote by hand, so the properties worth
testing are the destructive ones: it only ever touches its own block, it can
update and remove that block, and re-running changes nothing.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "skhd" / "install-hotkeys.sh"

EXISTING = "# my own bindings\ncmd - z : echo hi\n"


@pytest.fixture
def skhdrc(tmp_path) -> Path:
    path = tmp_path / "skhdrc"
    path.write_text(EXISTING, encoding="utf-8")
    return path


def run(skhdrc: Path, *args) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = str(skhdrc.parent)
    return subprocess.run(
        [str(SCRIPT), "--skhdrc", str(skhdrc), "--no-service", "--ctl", "/opt/ctl", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def test_the_two_hotkeys_are_written_between_markers(skhdrc):
    assert run(skhdrc).returncode == 0

    body = skhdrc.read_text(encoding="utf-8")
    assert "# >>> voice-loop hotkeys >>>" in body
    assert "ctrl + alt + cmd - m : /opt/ctl mic-toggle" in body
    assert "ctrl + alt + cmd - b : /opt/ctl busy-toggle" in body
    assert "# <<< voice-loop hotkeys <<<" in body


def test_what_was_already_in_the_file_is_left_alone(skhdrc):
    run(skhdrc)

    assert skhdrc.read_text(encoding="utf-8").startswith(EXISTING)


def test_running_it_again_changes_nothing(skhdrc):
    run(skhdrc)
    first = skhdrc.read_text(encoding="utf-8")

    result = run(skhdrc)

    assert skhdrc.read_text(encoding="utf-8") == first
    assert "already up to date" in result.stdout


def test_a_new_key_replaces_the_block_instead_of_appending_a_second_one(skhdrc):
    run(skhdrc)

    run(skhdrc, "--mic-key", "ctrl - space")

    body = skhdrc.read_text(encoding="utf-8")
    assert body.count("mic-toggle") == 1
    assert "ctrl - space : /opt/ctl mic-toggle" in body


def test_removing_takes_the_block_out_and_leaves_the_rest(skhdrc):
    run(skhdrc)

    run(skhdrc, "--remove")

    assert skhdrc.read_text(encoding="utf-8") == EXISTING


def test_the_file_is_backed_up_before_it_is_changed(skhdrc):
    run(skhdrc)

    backups = list(skhdrc.parent.glob("skhdrc.voice-loop-backup-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == EXISTING


def test_a_missing_skhdrc_is_created(tmp_path):
    path = tmp_path / "nested" / "skhdrc"

    assert run(path).returncode == 0
    assert "mic-toggle" in path.read_text(encoding="utf-8")


def test_an_unknown_flag_is_refused(skhdrc):
    result = run(skhdrc, "--turbo")

    assert result.returncode == 2
    assert skhdrc.read_text(encoding="utf-8") == EXISTING


def test_the_default_command_points_at_the_installed_runtime(tmp_path):
    """The daemon runs from the runtime, so the hotkeys must talk to that one."""
    home = tmp_path / "home"
    runtime = home / ".local" / "share" / "voice-loop" / "bin"
    runtime.mkdir(parents=True)
    ctl = runtime / "voice-loopctl"
    ctl.write_text("#!/bin/sh\n", encoding="utf-8")
    ctl.chmod(0o755)
    path = home / "skhdrc"
    path.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env["HOME"] = str(home)
    env.pop("VOICE_LOOP_RUNTIME_DIR", None)
    subprocess.run(
        [str(SCRIPT), "--skhdrc", str(path), "--no-service"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=True,
    )

    assert f"{ctl} mic-toggle" in path.read_text(encoding="utf-8")
