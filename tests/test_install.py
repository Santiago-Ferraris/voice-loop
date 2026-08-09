"""install.sh, exercised end to end in a throwaway HOME.

launchd and the virtualenv are skipped by default (a test box has no user
session bus, and the venv is already there), but everything that touches the
user's files — the runtime copy, the state directory, the pointer the hooks
read, the env file, and settings.json — runs for real. The launchd and pip
paths get their own stubs further down, because "it printed *installed* while
the agent was dead" is the bug this file now exists to prevent.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from voiceloop.runtime import fingerprint, is_tcc_protected, plist_paths

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL = REPO_ROOT / "install.sh"
UNINSTALL = REPO_ROOT / "uninstall.sh"
# The suite installs an agent of its own, under a label nothing else answers
# to. `launchctl` addresses agents in `gui/$UID` — a domain that does **not**
# follow $HOME — so a throwaway HOME is not isolation: without this, every run
# of this file booted out the agent of whoever was using the machine, which is
# a service stopping under somebody mid-sentence. Belt and braces: the label is
# ours *and* `launchctl` itself is stubbed out by default.
TEST_LABEL = "com.voiceloop.daemon.test"
PLIST_RELATIVE = Path("Library") / "LaunchAgents" / f"{TEST_LABEL}.plist"

# Answers to everything, does nothing, and touches no domain at all. Tests that
# care what launchctl was asked pass their own stub through `extra_env`.
INERT_LAUNCHCTL = "#!/bin/sh\nexit 0\n"

# Everything install.sh reads out of a clone. Copied, rather than pointed at,
# so a test can put a clone where macOS would hide it.
CLONE_MEMBERS = (
    "bin",
    "hooks",
    "launchd",
    "voiceloop",
    "config.example.yml",
    "install.sh",
    "uninstall.sh",
    "pyproject.toml",
)


@pytest.fixture
def home(tmp_path):
    fake = tmp_path / "home"
    fake.mkdir()
    return fake


@pytest.fixture
def clone(home):
    """A clone in ~/Documents — where this project actually lives, and where
    macOS TCC makes it unusable to launchd."""
    target = home / "Documents" / "voice-loop"
    target.mkdir(parents=True)
    for name in CLONE_MEMBERS:
        source = REPO_ROOT / name
        if source.is_dir():
            shutil.copytree(source, target / name, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, target / name)
    return target


@pytest.fixture
def short_state_dir(clone):
    """State outside the deep pytest tmp tree: AF_UNIX paths cap at 104 bytes."""
    directory = Path(tempfile.mkdtemp(prefix="vlstate", dir="/tmp"))
    (clone / "config.local.yml").write_text(
        f"paths:\n  state_dir: {directory}\n", encoding="utf-8"
    )
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(autouse=True)
def never_the_real_launchctl(tmp_path_factory, monkeypatch):
    """No test in this file may reach the user's own launchd domain."""
    stub = tmp_path_factory.mktemp("launchctl") / "launchctl"
    stub.write_text(INERT_LAUNCHCTL, encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("VOICE_LOOP_LAUNCHCTL", str(stub))
    monkeypatch.setenv("VOICE_LOOP_LABEL", TEST_LABEL)


def run_script(script: Path, home: Path, *args, extra_env=None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["CLAUDE_SETTINGS"] = str(home / ".claude" / "settings.json")
    env["VOICE_LOOP_CONFIG_DIR"] = str(home / ".config" / "voice-loop")
    env["VOICE_LOOP_ENV_FILE"] = str(home / ".config" / "voice-loop" / "env")
    env["PYTHONPATH"] = str(REPO_ROOT)
    # the runtime follows the fake HOME, exactly as it would on a real machine
    env.pop("VOICE_LOOP_RUNTIME_DIR", None)
    env.update(extra_env or {})
    return subprocess.run(
        [str(script), *args], capture_output=True, text=True, env=env, timeout=180, check=False
    )


def install_clone(clone: Path, home: Path, *args, extra_env=None) -> subprocess.CompletedProcess:
    env = {"PYTHONPATH": str(clone)}
    env.update(extra_env or {})
    return run_script(clone / "install.sh", home, *args, extra_env=env)


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


def test_the_microphone_grant_is_a_numbered_step_not_a_footnote(home):
    """Issue #7: the mitigation existed and was written down nowhere."""
    result = install(home)

    assert "voice-loopctl doctor" in result.stdout
    assert "microphone dialog" in result.stdout
    assert "Privacy & Security" in result.stdout


def test_the_probe_is_skipped_when_there_is_nobody_to_answer_the_dialog(home):
    """No tty means no human: probing would hang on a prompt nobody sees."""
    result = install(home)

    assert "doctor: not run" in result.stdout


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


# --- the runtime copy, and the TCC trap it exists for ---------------------
#
# The clone lives in ~/Documents. macOS TCC does not let a LaunchAgent read or
# even *execute* anything there, so the agent used to die with exit 126 and
# "Operation not permitted" while install.sh printed "voice-loop installed."


def plist_body(home: Path) -> str:
    return (home / PLIST_RELATIVE).read_text(encoding="utf-8")


def test_the_plist_never_names_a_folder_launchd_cannot_reach(home, clone):
    """The regression. Every path in the plist must survive TCC."""
    result = install_clone(clone, home, "--no-venv", "--no-launchd")

    assert result.returncode == 0, result.stderr
    offenders = [path for path in plist_paths(plist_body(home)) if is_tcc_protected(path, home)]
    assert offenders == []


def test_the_plist_points_at_the_runtime_and_never_at_the_clone(home, clone):
    install_clone(clone, home, "--no-venv", "--no-launchd")

    body = plist_body(home)
    runtime = home / ".local" / "share" / "voice-loop"
    assert f"<string>{runtime}/bin/voice-loopd</string>" in body
    assert f"<string>{runtime}</string>" in body  # WorkingDirectory
    assert str(clone) not in body


def test_the_plist_is_written_even_when_it_is_not_loaded(home, clone):
    result = install_clone(clone, home, "--no-venv", "--no-launchd")

    assert (home / PLIST_RELATIVE).is_file()
    assert "not loaded" in result.stdout


def test_a_state_dir_launchd_cannot_reach_is_refused(home, clone):
    """state_dir lands in the plist as the log paths — same trap, config-driven."""
    (clone / "config.local.yml").write_text(
        f"paths:\n  state_dir: {home}/Desktop/vl-state\n", encoding="utf-8"
    )

    result = install_clone(clone, home, "--no-venv", "--no-launchd")

    assert result.returncode != 0
    assert "Desktop" in result.stderr
    assert not (home / PLIST_RELATIVE).exists()


def test_the_runtime_is_a_complete_copy(home, clone):
    install_clone(clone, home, "--no-venv", "--no-launchd")

    runtime = home / ".local" / "share" / "voice-loop"
    assert (runtime / "bin" / "voice-loopd").stat().st_mode & stat.S_IXUSR
    assert (runtime / "bin" / "voice-loopctl").stat().st_mode & stat.S_IXUSR
    assert (runtime / "config.example.yml").is_file()
    # --no-venv: the package is copied too, because the daemon cannot import
    # anything out of ~/Documents either
    assert (runtime / "voiceloop" / "daemon.py").is_file()
    assert not (runtime / "voiceloop" / "__pycache__").exists()


def test_the_copied_wrapper_is_byte_identical(home, clone):
    install_clone(clone, home, "--no-venv", "--no-launchd")

    runtime = home / ".local" / "share" / "voice-loop"
    assert (runtime / "bin" / "voice-loopd").read_bytes() == (clone / "bin" / "voice-loopd").read_bytes()


def test_local_config_travels_into_the_runtime(home, clone):
    (clone / "config.local.yml").write_text("summaries:\n  provider: none\n", encoding="utf-8")

    install_clone(clone, home, "--no-venv", "--no-launchd")

    copied = home / ".local" / "share" / "voice-loop" / "config.local.yml"
    assert copied.read_text(encoding="utf-8") == "summaries:\n  provider: none\n"


def test_deleting_local_config_deletes_the_installed_one(home, clone):
    (clone / "config.local.yml").write_text("summaries:\n  provider: none\n", encoding="utf-8")
    install_clone(clone, home, "--no-venv", "--no-launchd")
    (clone / "config.local.yml").unlink()

    install_clone(clone, home, "--no-venv", "--no-launchd")

    assert not (home / ".local/share/voice-loop/config.local.yml").exists()


def test_reinstalling_after_an_edit_refreshes_the_runtime(home, clone):
    """`git pull && ./install.sh` has to mean something."""
    install_clone(clone, home, "--no-venv", "--no-launchd")
    (clone / "voiceloop" / "iterm.py").write_text("MARKER = 'fixed'\n", encoding="utf-8")

    install_clone(clone, home, "--no-venv", "--no-launchd")

    installed = home / ".local/share/voice-loop/voiceloop/iterm.py"
    assert installed.read_text(encoding="utf-8") == "MARKER = 'fixed'\n"


def test_the_manifest_pins_the_runtime_to_this_clone(home, clone):
    install_clone(clone, home, "--no-venv", "--no-launchd")

    manifest = json.loads((home / ".local/share/voice-loop/manifest.json").read_text(encoding="utf-8"))
    assert manifest["source"] == str(clone)
    assert manifest["fingerprint"] == fingerprint(clone)
    assert manifest["mode"] == "no-venv"


def test_the_hooks_still_run_from_the_clone(home, clone):
    """Hooks are children of your terminal, which does have TCC consent."""
    install_clone(clone, home, "--no-venv", "--no-launchd")

    commands = hook_commands(settings_of(home))
    assert commands
    assert all(command.startswith(str(clone / "hooks" / "vl-hook.sh") + " ") for command in commands)


def test_the_runtime_may_not_be_the_clone(home, clone):
    result = install_clone(
        clone, home, "--no-venv", "--no-launchd", extra_env={"VOICE_LOOP_RUNTIME_DIR": str(clone)}
    )

    assert result.returncode == 2
    assert "cannot be the clone" in result.stderr


# --- proving the agent actually came up -----------------------------------

FAKE_DAEMON = Path(__file__).resolve().parent / "fake_daemon.py"

LAUNCHCTL_STUB = """#!/bin/sh
# stub launchctl: optionally starts the fake daemon on bootstrap, and reports
# whatever pid the test wants `list` to claim.
echo "launchctl $*" >> "$VL_LAUNCHCTL_LOG"
case "${1-}" in
  bootstrap)
    if [ -n "${VL_FAKE_DAEMON-}" ]; then
      "$VL_REAL_PYTHON" "$VL_FAKE_DAEMON" "$VL_SOCKET" > "$VL_PIDFILE" 2>/dev/null &
    fi
    ;;
  list)
    if [ -n "${VL_LAUNCHD_PID-}" ]; then
      printf '\t"PID" = %s;\n' "$VL_LAUNCHD_PID"
    elif [ -s "${VL_PIDFILE-/nonexistent}" ]; then
      printf '\t"PID" = %s;\n' "$(cat "$VL_PIDFILE")"
    fi
    ;;
esac
exit 0
"""

PYTHON_STUB = """#!/bin/sh
# stub python3: records every pip invocation and the pip environment it ran
# with, fakes venv creation, and delegates everything else to a real python.
case "$1 ${2-}" in
  "-m venv")
    mkdir -p "$3/bin"
    cp "$0" "$3/bin/python"
    chmod +x "$3/bin/python"
    exit 0
    ;;
  "-m pip")
    {
      echo "argv: $*"
      echo "PIP_INDEX_URL=${PIP_INDEX_URL-<unset>}"
      echo "PIP_CONFIG_FILE=${PIP_CONFIG_FILE-<unset>}"
      echo "PIP_EXTRA_INDEX_URL=${PIP_EXTRA_INDEX_URL-<unset>}"
      echo "PIP_REQUIRE_VIRTUALENV=${PIP_REQUIRE_VIRTUALENV-<unset>}"
      echo "--"
    } >> "$VL_PIP_LOG"
    case "${PIP_INDEX_URL-}" in
      *codeartifact*)
        echo "ERROR: HTTP error 401 while getting ${PIP_INDEX_URL}" >&2
        exit 1
        ;;
    esac
    exit 0
    ;;
esac
exec "$VL_REAL_PYTHON" "$@"
"""


def write_stub(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def launchctl(tmp_path):
    """A stubbed launchctl, plus the env that tells it how to behave."""
    stub = write_stub(tmp_path / "launchctl", LAUNCHCTL_STUB)
    env = {
        "VOICE_LOOP_LAUNCHCTL": str(stub),
        "VOICE_LOOP_STARTUP_TIMEOUT": "10",
        "VL_LAUNCHCTL_LOG": str(tmp_path / "launchctl.log"),
        "VL_REAL_PYTHON": sys.executable,
        "VL_PIDFILE": str(tmp_path / "daemon.pid"),
    }
    yield env
    pid_file = tmp_path / "daemon.pid"
    if pid_file.is_file() and pid_file.read_text().strip():
        with contextlib.suppress(OSError, ValueError):
            os.kill(int(pid_file.read_text().split()[0]), signal.SIGKILL)


def start_stray_daemon(socket_path: Path):
    process = subprocess.Popen(
        [sys.executable, str(FAKE_DAEMON), str(socket_path)],
        stdout=subprocess.PIPE,
        text=True,
        env=dict(os.environ, PYTHONPATH=str(REPO_ROOT)),
    )
    process.stdout.readline()  # bound
    return process


def test_install_fails_loudly_when_the_agent_never_comes_up(home, clone, short_state_dir, launchctl):
    (short_state_dir / "logs").mkdir(parents=True, exist_ok=True)
    (short_state_dir / "logs" / "stderr.log").write_text(
        "/bin/sh: /Users/x/Documents/voice-loop/bin/voice-loopd: Operation not permitted\n",
        encoding="utf-8",
    )
    launchctl["VOICE_LOOP_STARTUP_TIMEOUT"] = "1"

    result = install_clone(clone, home, "--no-venv", extra_env=launchctl)

    assert result.returncode == 1
    assert "did not come up" in result.stderr
    # the real error, not a shrug
    assert "Operation not permitted" in result.stderr
    assert "voice-loop installed." not in result.stdout


def test_install_reports_the_pid_when_the_agent_answers(home, clone, short_state_dir, launchctl):
    launchctl["VL_FAKE_DAEMON"] = str(FAKE_DAEMON)
    launchctl["VL_SOCKET"] = str(short_state_dir / "daemon.sock")

    result = install_clone(clone, home, "--no-venv", extra_env=launchctl)

    assert result.returncode == 0, result.stderr
    assert f"{TEST_LABEL} up (pid" in result.stdout
    assert "voice-loop installed." in result.stdout


def test_a_daemon_that_is_not_the_agents_is_refused(home, clone, short_state_dir, launchctl):
    """Something answers, but launchd started something else. Not an install."""
    launchctl["VL_FAKE_DAEMON"] = str(FAKE_DAEMON)
    launchctl["VL_SOCKET"] = str(short_state_dir / "daemon.sock")
    launchctl["VL_LAUNCHD_PID"] = "999999"

    result = install_clone(clone, home, "--no-venv", extra_env=launchctl)

    assert result.returncode == 1
    assert "stray daemon" in result.stderr
    assert "voice-loop installed." not in result.stdout


def test_a_hand_started_daemon_does_not_count_as_a_working_install(
    home, clone, short_state_dir, launchctl
):
    """`nohup bin/voice-loopd &` owns the socket; launchd's copy cannot bind."""
    stray = start_stray_daemon(short_state_dir / "daemon.sock")
    launchctl["VOICE_LOOP_STARTUP_TIMEOUT"] = "2"
    try:
        result = install_clone(clone, home, "--no-venv", extra_env=launchctl)
    finally:
        if stray.poll() is None:
            stray.kill()
        stray.wait(timeout=5)

    assert result.returncode == 1
    assert "outside launchd" in result.stdout
    assert "did not come up" in result.stderr


# --- pip, and the user's private index ------------------------------------

CODEARTIFACT = "https://aws:expired@darwin-1.d.codeartifact.us-east-1.amazonaws.com/pypi/py/simple/"


@pytest.fixture
def pip_stub(tmp_path):
    stub_dir = tmp_path / "stub-bin"
    write_stub(stub_dir / "python3", PYTHON_STUB)
    log = tmp_path / "pip.log"
    env = {
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "VL_REAL_PYTHON": sys.executable,
        "VL_PIP_LOG": str(log),
    }
    return env, log


def test_pip_ignores_the_index_the_users_shell_points_at(home, clone, pip_stub):
    """A stale CodeArtifact token is a 401 that has nothing to do with us."""
    env, log = pip_stub
    env.update(
        {
            "PIP_INDEX_URL": CODEARTIFACT,
            "PIP_EXTRA_INDEX_URL": "https://another-private-one/simple",
            "PIP_REQUIRE_VIRTUALENV": "true",
        }
    )

    result = install_clone(clone, home, "--no-launchd", extra_env=env)

    assert result.returncode == 0, result.stderr
    body = log.read_text(encoding="utf-8")
    assert "codeartifact" not in body
    assert "PIP_INDEX_URL=https://pypi.org/simple" in body
    assert "PIP_CONFIG_FILE=/dev/null" in body
    assert "PIP_EXTRA_INDEX_URL=<unset>" in body
    assert "PIP_REQUIRE_VIRTUALENV=<unset>" in body


def test_the_package_is_reinstalled_even_at_the_same_version(home, clone, pip_stub):
    env, log = pip_stub

    install_clone(clone, home, "--no-launchd", extra_env=env)

    assert "--force-reinstall --no-deps" in log.read_text(encoding="utf-8")


def test_the_index_can_still_be_chosen_on_purpose(home, clone, pip_stub):
    env, log = pip_stub
    env["VOICE_LOOP_PIP_INDEX_URL"] = "https://mirror.internal/simple"

    result = install_clone(clone, home, "--no-launchd", extra_env=env)

    assert result.returncode == 0, result.stderr
    assert "PIP_INDEX_URL=https://mirror.internal/simple" in log.read_text(encoding="utf-8")


def test_an_index_that_rejects_us_stops_the_install(home, clone, pip_stub):
    env, _ = pip_stub
    env["VOICE_LOOP_PIP_INDEX_URL"] = CODEARTIFACT

    result = install_clone(clone, home, "--no-launchd", extra_env=env)

    assert result.returncode != 0
    assert "401" in result.stderr
    assert "voice-loop installed." not in result.stdout


# --- uninstall ------------------------------------------------------------


def test_uninstall_removes_the_runtime(home, clone):
    install_clone(clone, home, "--no-venv", "--no-launchd")
    runtime = home / ".local" / "share" / "voice-loop"
    assert runtime.is_dir()

    result = run_script(clone / "uninstall.sh", home, extra_env={"PYTHONPATH": str(clone)})

    assert result.returncode == 0, result.stderr
    assert not runtime.exists()
    assert f"removed {runtime}" in result.stdout


# --- the label, and why it is a variable -----------------------------------


def test_the_agent_this_suite_installs_is_not_the_one_you_are_running():
    """`launchctl` addresses `gui/$UID`, which does not follow $HOME.

    So installing into a throwaway HOME is not isolation: `bootout` reached the
    real user's agent and stopped it, three times, on a machine somebody was
    using at the time. The label has to be ours.
    """
    from voiceloop.runtime import DEFAULT_LABEL

    assert TEST_LABEL != DEFAULT_LABEL


def test_the_label_is_what_install_writes_into_the_plist(home, clone):
    install_clone(clone, home, "--no-venv", "--no-launchd")

    body = plist_body(home)

    assert f"<string>{TEST_LABEL}</string>" in body
    assert "<string>com.voiceloop.daemon</string>" not in body


def test_the_renderer_defaults_to_the_real_label(repo_root):
    from voiceloop.runtime import DEFAULT_LABEL, render_plist

    template = (repo_root / "launchd" / "com.voiceloop.daemon.plist.template").read_text()
    body = render_plist(template, runtime="/r", home="/h", state_dir="/s")

    assert f"<string>{DEFAULT_LABEL}</string>" in body
    assert "__LABEL__" not in body
