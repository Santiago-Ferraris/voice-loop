"""The installed runtime: the TCC guard, the fingerprint, and the drift warning.

The guard is the regression that matters. A plist naming anything under
~/Documents, ~/Desktop or ~/Downloads is not a cosmetic problem — launchd
cannot execute it, the agent dies with exit 126 before Python starts, and
`launchctl list` shows a dash where the pid should be.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from voiceloop import runtime
from voiceloop.runtime import (
    PROTECTED_DIR_NAMES,
    TccError,
    fingerprint,
    is_tcc_protected,
    plist_paths,
    read_manifest,
    render_plist,
    runtime_dir,
    staleness_warning,
    stop_daemon,
    wait_for_daemon,
    write_manifest,
)

TEMPLATE = (Path(__file__).resolve().parent.parent / "launchd" / "com.voiceloop.daemon.plist.template").read_text(
    encoding="utf-8"
)


@pytest.fixture
def home(tmp_path):
    fake = tmp_path / "home"
    fake.mkdir()
    return fake


# --- where the runtime lives ---------------------------------------------


def test_the_runtime_defaults_under_local_share():
    assert runtime_dir({"HOME": "/Users/someone"}) == Path("/Users/someone/.local/share/voice-loop")


def test_the_runtime_can_be_moved_with_an_env_var():
    env = {"HOME": "/Users/someone", "VOICE_LOOP_RUNTIME_DIR": "~/elsewhere/vl"}

    assert runtime_dir(env) == Path("/Users/someone/elsewhere/vl")


def test_the_default_runtime_is_not_itself_protected():
    home = "/Users/someone"

    assert not is_tcc_protected(runtime_dir({"HOME": home}), home)


# --- the TCC guard --------------------------------------------------------


@pytest.mark.parametrize("folder", PROTECTED_DIR_NAMES)
def test_every_protected_folder_is_recognised(folder, home):
    assert is_tcc_protected(home / folder / "voice-loop" / "bin" / "voice-loopd", home)


def test_the_protected_folder_itself_counts(home):
    assert is_tcc_protected(home / "Documents", home)


def test_a_lookalike_sibling_is_not_protected(home):
    assert not is_tcc_protected(home / "Documentsx" / "voice-loop", home)


def test_another_users_documents_is_not_ours(home):
    assert not is_tcc_protected("/Users/somebody-else/Documents/voice-loop", home)


def test_a_relative_step_out_of_a_safe_folder_is_caught(home):
    sneaky = home / ".local" / "share" / ".." / ".." / "Documents" / "voice-loop"

    assert is_tcc_protected(sneaky, home)


def test_a_symlink_into_documents_is_caught(home):
    (home / "Documents" / "voice-loop").mkdir(parents=True)
    (home / ".local").mkdir()
    (home / ".local" / "share").symlink_to(home / "Documents" / "voice-loop")

    assert is_tcc_protected(home / ".local" / "share" / "bin", home)


def test_state_and_share_are_left_alone(home):
    assert not is_tcc_protected(home / ".local" / "share" / "voice-loop", home)
    assert not is_tcc_protected(home / ".local" / "state" / "voice-loop", home)


# --- rendering ------------------------------------------------------------


def render(home, **overrides):
    kwargs = {
        "runtime": home / ".local" / "share" / "voice-loop",
        "home": home,
        "state_dir": home / ".local" / "state" / "voice-loop",
    }
    kwargs.update(overrides)
    return render_plist(TEMPLATE, **kwargs)


def test_a_rendered_plist_has_no_placeholders_left(home):
    body = render(home)

    assert "__RUNTIME__" not in body
    assert "__HOME__" not in body
    assert "__STATE_DIR__" not in body


def test_the_program_is_the_runtime_wrapper(home):
    body = render(home)

    assert f"<string>{home}/.local/share/voice-loop/bin/voice-loopd</string>" in body


def test_a_runtime_under_documents_is_refused(home):
    with pytest.raises(TccError) as raised:
        render(home, runtime=home / "Documents" / "voice-loop")

    assert "Documents" in str(raised.value)
    assert str(home / "Documents" / "voice-loop" / "bin" / "voice-loopd") in str(raised.value)


@pytest.mark.parametrize("folder", PROTECTED_DIR_NAMES)
def test_a_state_dir_in_any_protected_folder_is_refused(folder, home):
    with pytest.raises(TccError):
        render(home, state_dir=home / folder / "vl-state")


def test_a_safe_render_survives_the_guard(home):
    body = render(home)

    assert [path for path in plist_paths(body) if is_tcc_protected(path, home)] == []


def test_the_guard_reads_every_path_in_the_file(home):
    body = render(home)

    assert f"{home}/.local/state/voice-loop/logs/stderr.log" in plist_paths(body)
    # PATH is colon-joined and still gets checked entry by entry
    assert "/opt/homebrew/bin" in plist_paths(body)
    assert "/usr/bin" in plist_paths(body)


def test_relative_strings_are_not_mistaken_for_paths():
    assert plist_paths("<string>com.voiceloop.daemon</string>") == []


# --- fingerprint ----------------------------------------------------------


def clone_like(root: Path) -> Path:
    (root / "voiceloop").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "voiceloop" / "daemon.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "config.example.yml").write_text("summaries:\n  provider: none\n", encoding="utf-8")
    (root / "bin" / "voice-loopd").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='voice-loop'\n", encoding="utf-8")
    return root


def test_the_fingerprint_is_stable(tmp_path):
    source = clone_like(tmp_path / "clone")

    assert fingerprint(source) == fingerprint(source)


def test_editing_the_code_changes_the_fingerprint(tmp_path):
    source = clone_like(tmp_path / "clone")
    before = fingerprint(source)

    (source / "voiceloop" / "daemon.py").write_text("print('bye')\n", encoding="utf-8")

    assert fingerprint(source) != before


def test_adding_a_module_changes_the_fingerprint(tmp_path):
    source = clone_like(tmp_path / "clone")
    before = fingerprint(source)

    (source / "voiceloop" / "extra.py").write_text("x = 1\n", encoding="utf-8")

    assert fingerprint(source) != before


def test_local_config_is_part_of_the_fingerprint(tmp_path):
    """It ships into the runtime, so changing it is also a reinstall."""
    source = clone_like(tmp_path / "clone")
    before = fingerprint(source)

    (source / "config.local.yml").write_text("voice:\n  name: Paulina\n", encoding="utf-8")

    assert fingerprint(source) != before


def test_untracked_noise_does_not_change_the_fingerprint(tmp_path):
    source = clone_like(tmp_path / "clone")
    before = fingerprint(source)

    (source / "README.md").write_text("hello\n", encoding="utf-8")
    (source / "voiceloop" / "notes.txt").write_text("hello\n", encoding="utf-8")

    assert fingerprint(source) == before


def test_an_unreadable_file_does_not_raise(tmp_path):
    source = clone_like(tmp_path / "clone")
    (source / "voiceloop" / "daemon.py").chmod(0o000)
    try:
        assert isinstance(fingerprint(source), str)
    finally:
        (source / "voiceloop" / "daemon.py").chmod(0o644)


# --- manifest & drift -----------------------------------------------------


def test_the_manifest_records_where_the_runtime_came_from(tmp_path):
    source = clone_like(tmp_path / "clone")
    installed = tmp_path / "runtime"

    write_manifest(installed, source, mode="venv")

    manifest = read_manifest(installed)
    assert manifest["source"] == str(source)
    assert manifest["fingerprint"] == fingerprint(source)
    assert manifest["mode"] == "venv"
    assert manifest["installed_at"].endswith("+00:00")


def test_a_missing_manifest_reads_as_none(tmp_path):
    assert read_manifest(tmp_path / "nowhere") is None


def test_a_corrupt_manifest_reads_as_none(tmp_path):
    (tmp_path / "manifest.json").write_text("{not json", encoding="utf-8")

    assert read_manifest(tmp_path) is None


def test_a_freshly_installed_runtime_is_not_stale(tmp_path):
    source = clone_like(tmp_path / "clone")
    installed = tmp_path / "runtime"
    write_manifest(installed, source)

    assert staleness_warning(source, installed) is None


def test_editing_the_clone_makes_the_runtime_stale(tmp_path):
    source = clone_like(tmp_path / "clone")
    installed = tmp_path / "runtime"
    write_manifest(installed, source)

    (source / "voiceloop" / "daemon.py").write_text("print('fixed')\n", encoding="utf-8")

    warning = staleness_warning(source, installed)
    assert warning is not None
    assert "out of date" in warning
    assert "install.sh" in warning


def test_a_runtime_installed_from_another_clone_says_so(tmp_path):
    installed_from = clone_like(tmp_path / "clone-a")
    other = clone_like(tmp_path / "clone-b")
    installed = tmp_path / "runtime"
    write_manifest(installed, installed_from)

    warning = staleness_warning(other, installed)
    assert warning is not None
    assert str(installed_from) in warning


def test_the_runtime_never_warns_about_itself(tmp_path):
    """The copied wrapper passes --repo-root <runtime>; that is not drift."""
    installed = clone_like(tmp_path / "runtime")
    write_manifest(installed, tmp_path / "clone")

    assert staleness_warning(installed, installed) is None


def test_the_runtime_recognises_itself_through_a_symlink(tmp_path):
    """`/tmp` is `/private/tmp` on macOS: the wrapper resolves, `$HOME` may not."""
    installed = clone_like(tmp_path / "real" / "runtime")
    write_manifest(installed, tmp_path / "clone")
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real")

    assert staleness_warning(link / "runtime", installed) is None


def test_a_clone_reached_through_a_symlink_is_still_the_same_clone(tmp_path):
    source = clone_like(tmp_path / "real" / "clone")
    installed = tmp_path / "runtime"
    write_manifest(installed, source)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real")

    assert staleness_warning(link / "clone", installed) is None


def test_no_manifest_means_no_warning(tmp_path):
    source = clone_like(tmp_path / "clone")

    assert staleness_warning(source, tmp_path / "never-installed") is None


def test_the_runtime_dir_comes_from_the_environment(tmp_path, monkeypatch):
    source = clone_like(tmp_path / "clone")
    installed = tmp_path / "runtime"
    write_manifest(installed, source)
    (source / "voiceloop" / "daemon.py").write_text("changed\n", encoding="utf-8")
    monkeypatch.setenv("VOICE_LOOP_RUNTIME_DIR", str(installed))

    assert "out of date" in (staleness_warning(source) or "")


# --- waiting for a daemon that may never arrive ---------------------------

FAKE_DAEMON = Path(__file__).resolve().parent / "fake_daemon.py"


@pytest.fixture
def fake_daemon(sock_path):
    """A process that answers `status` on the socket, and dies on `restart`."""
    started = []

    def start():
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        process = subprocess.Popen(
            [sys.executable, str(FAKE_DAEMON), str(sock_path)],
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )
        started.append(process)
        process.stdout.readline()  # the pid line: the socket is bound by now
        return process

    try:
        yield start
    finally:
        for process in started:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_waiting_returns_the_status_of_a_live_daemon(sock_path, fake_daemon):
    process = fake_daemon()

    status = wait_for_daemon(sock_path, timeout=10)

    assert status["pid"] == process.pid


def test_waiting_on_nothing_times_out(sock_path):
    started = time.monotonic()

    with pytest.raises(TimeoutError) as raised:
        wait_for_daemon(sock_path, timeout=0.5, interval=0.05)

    assert str(sock_path) in str(raised.value)
    assert time.monotonic() - started < 5


def test_a_daemon_that_arrives_late_is_still_caught(sock_path, fake_daemon, monkeypatch):
    calls = {"n": 0}
    real_request = runtime.request

    def slow(socket_path, cmd, args=None, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("not yet")
        return real_request(socket_path, cmd, args, **kwargs)

    fake_daemon()
    monkeypatch.setattr(runtime, "request", slow)

    assert wait_for_daemon(sock_path, timeout=10, interval=0.01)["version"] == "test"
    assert calls["n"] == 3


def test_stopping_a_stray_daemon_frees_the_socket(sock_path, fake_daemon):
    process = fake_daemon()

    assert stop_daemon(sock_path) is True
    process.wait(timeout=5)


def test_stopping_nothing_is_false(sock_path):
    assert stop_daemon(sock_path) is False


# --- the CLI install.sh drives -------------------------------------------


def test_the_cli_writes_a_plist(tmp_path, home):
    template = tmp_path / "template.plist"
    template.write_text(TEMPLATE, encoding="utf-8")
    output = tmp_path / "out.plist"

    code = runtime.main(
        [
            "render-plist",
            "--template", str(template),
            "--output", str(output),
            "--runtime", str(home / ".local/share/voice-loop"),
            "--home", str(home),
            "--state-dir", str(home / ".local/state/voice-loop"),
        ]
    )

    assert code == 0
    assert "__RUNTIME__" not in output.read_text(encoding="utf-8")


def test_the_cli_refuses_a_protected_runtime(tmp_path, home, capsys):
    template = tmp_path / "template.plist"
    template.write_text(TEMPLATE, encoding="utf-8")
    output = tmp_path / "out.plist"

    code = runtime.main(
        [
            "render-plist",
            "--template", str(template),
            "--output", str(output),
            "--runtime", str(home / "Documents/voice-loop"),
            "--home", str(home),
            "--state-dir", str(home / ".local/state/voice-loop"),
        ]
    )

    assert code == 2
    assert not output.exists()
    assert "Documents" in capsys.readouterr().err


def test_the_cli_writes_the_manifest(tmp_path, capsys):
    source = clone_like(tmp_path / "clone")
    installed = tmp_path / "runtime"

    code = runtime.main(
        ["write-manifest", "--runtime", str(installed), "--source", str(source), "--mode", "no-venv"]
    )

    assert code == 0
    assert capsys.readouterr().out.strip() == fingerprint(source)
    assert json.loads((installed / "manifest.json").read_text(encoding="utf-8"))["mode"] == "no-venv"


def test_the_cli_reports_a_daemon_that_never_came_up(sock_path, capsys):
    code = runtime.main(["wait-for-daemon", "--socket", str(sock_path), "--timeout", "0"])

    assert code == 1
    assert "no daemon on" in capsys.readouterr().err


def test_the_cli_prints_the_pid_it_found(sock_path, fake_daemon, capsys):
    process = fake_daemon()

    code = runtime.main(["wait-for-daemon", "--socket", str(sock_path), "--timeout", "10"])

    assert code == 0
    assert capsys.readouterr().out.strip() == str(process.pid)
