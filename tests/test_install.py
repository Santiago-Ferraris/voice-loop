"""install.sh, exercised end to end in a throwaway HOME.

launchd and the virtualenv are skipped (a test box has no user session bus and
the venv is already there), but everything that touches the user's files — the
state directory, the pointer the hooks read, the env file, and settings.json —
runs for real.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL = REPO_ROOT / "install.sh"
UNINSTALL = REPO_ROOT / "uninstall.sh"


@pytest.fixture
def home(tmp_path):
    fake = tmp_path / "home"
    fake.mkdir()
    return fake


def run_script(script: Path, home: Path, *args, extra_env=None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["CLAUDE_SETTINGS"] = str(home / ".claude" / "settings.json")
    env["VOICE_LOOP_CONFIG_DIR"] = str(home / ".config" / "voice-loop")
    env["VOICE_LOOP_ENV_FILE"] = str(home / ".config" / "voice-loop" / "env")
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.update(extra_env or {})
    return subprocess.run(
        [str(script), *args], capture_output=True, text=True, env=env, timeout=180, check=False
    )


def install(home: Path, *extra) -> subprocess.CompletedProcess:
    return run_script(INSTALL, home, "--no-venv", "--no-launchd", *extra)


def settings_of(home: Path) -> dict:
    return json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))


def hook_commands(settings: dict) -> list[str]:
    found = []
    for groups in (settings.get("hooks") or {}).values():
        for group in groups:
            for entry in group.get("hooks", []):
                found.append(entry["command"])
    return found


def test_install_succeeds_in_a_fresh_home(home):
    result = install(home)

    assert result.returncode == 0, result.stderr


def test_the_state_directory_is_created(home):
    install(home)

    state = home / ".local" / "state" / "voice-loop"
    assert (state / "spool").is_dir()
    assert (state / "spool" / "bad").is_dir()
    assert (state / "logs").is_dir()


def test_the_pointer_the_hooks_read_matches_the_config(home):
    install(home)

    pointer = home / ".config" / "voice-loop" / "state_dir"
    assert pointer.read_text(encoding="utf-8") == str(home / ".local/state/voice-loop")


def test_all_hooks_land_in_settings_with_absolute_paths(home):
    install(home)

    commands = hook_commands(settings_of(home))
    assert len(commands) == 6
    assert all(command.startswith(str(REPO_ROOT / "hooks" / "vl-hook.sh") + " ") for command in commands)


def test_installing_twice_leaves_settings_byte_identical(home):
    install(home)
    first = (home / ".claude" / "settings.json").read_text(encoding="utf-8")

    result = install(home)

    assert result.returncode == 0, result.stderr
    assert (home / ".claude" / "settings.json").read_text(encoding="utf-8") == first
    assert len(hook_commands(settings_of(home))) == 6


def test_existing_settings_are_preserved_and_backed_up(home):
    settings_file = home / ".claude" / "settings.json"
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(json.dumps({"model": "opus", "theme": "dark"}), encoding="utf-8")

    install(home)

    assert settings_of(home)["model"] == "opus"
    assert list(settings_file.parent.glob("*voice-loop-backup*"))


def test_the_hooks_can_be_skipped(home):
    install(home, "--no-hooks")

    assert not (home / ".claude" / "settings.json").exists()


# --- the env file ---------------------------------------------------------


def test_the_env_file_is_created_empty_and_private(home):
    install(home)

    env_file = home / ".config" / "voice-loop" / "env"
    assert env_file.is_file()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    body = env_file.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY" in body
    # a placeholder only — the installer must never invent a value
    assert all(line.startswith("#") or not line.strip() for line in body.splitlines())


def test_an_existing_env_file_is_never_overwritten(home):
    """The user's key already lives there. Clobbering it is unforgivable."""
    env_file = home / ".config" / "voice-loop" / "env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("OPENAI_API_KEY=sk-already-here\n", encoding="utf-8")
    env_file.chmod(0o600)

    result = install(home)

    assert result.returncode == 0, result.stderr
    assert env_file.read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-already-here\n"
    assert "left untouched" in result.stdout


def test_an_existing_env_file_survives_repeated_installs(home):
    env_file = home / ".config" / "voice-loop" / "env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("OPENAI_API_KEY=sk-already-here\n", encoding="utf-8")

    install(home)
    install(home)

    assert env_file.read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-already-here\n"


def test_an_empty_existing_env_file_is_still_left_alone(home):
    env_file = home / ".config" / "voice-loop" / "env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("", encoding="utf-8")

    install(home)

    assert env_file.read_text(encoding="utf-8") == ""


# --- the installed hook actually works ------------------------------------


def test_a_hook_installed_by_install_sh_spools_into_the_right_place(home, fixtures):
    install(home)
    payload = (fixtures / "hooks" / "stop.json").read_text(encoding="utf-8")
    command = hook_commands(settings_of(home))[0].rsplit(" ", 1)[0]

    env = dict(os.environ, HOME=str(home))
    env["VOICE_LOOP_CONFIG_DIR"] = str(home / ".config" / "voice-loop")
    env.pop("VOICE_LOOP_STATE_DIR", None)
    result = subprocess.run(
        [command, "stop"], input=payload, capture_output=True, text=True, env=env, check=False
    )

    assert result.returncode == 0
    spooled = list((home / ".local/state/voice-loop/spool").glob("*.json"))
    assert len(spooled) == 1


def test_the_hook_script_is_made_executable(home):
    install(home)

    assert (REPO_ROOT / "hooks" / "vl-hook.sh").stat().st_mode & stat.S_IXUSR


# --- uninstall ------------------------------------------------------------


def test_uninstall_removes_the_hooks_and_keeps_the_rest(home):
    settings_file = home / ".claude" / "settings.json"
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    install(home)

    result = run_script(UNINSTALL, home)

    assert result.returncode == 0, result.stderr
    assert hook_commands(settings_of(home)) == []
    assert settings_of(home)["model"] == "opus"


def test_uninstall_leaves_the_env_file_alone(home):
    env_file = home / ".config" / "voice-loop" / "env"
    install(home)
    env_file.write_text("OPENAI_API_KEY=sk-mine\n", encoding="utf-8")

    run_script(UNINSTALL, home)

    assert env_file.read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-mine\n"


def test_uninstall_keeps_state_unless_asked(home):
    install(home)
    state = home / ".local" / "state" / "voice-loop"

    run_script(UNINSTALL, home)
    assert state.is_dir()

    install(home)
    run_script(UNINSTALL, home, "--purge-state")
    assert not state.exists()


def test_uninstall_on_a_machine_that_never_installed_is_harmless(home):
    result = run_script(UNINSTALL, home)

    assert result.returncode == 0, result.stderr
