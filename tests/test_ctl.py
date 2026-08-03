from __future__ import annotations

import json

import pytest

from voiceloop import ctl


@pytest.fixture
def calls(monkeypatch):
    recorded: list[tuple] = []

    def fake_request(socket_path, cmd, args=None, **kwargs):
        recorded.append((str(socket_path), cmd, args))
        return {"ok": True, "data": {"echoed": cmd}}

    monkeypatch.setattr(ctl, "request", fake_request)
    return recorded


def run(*argv, socket="/tmp/vl.sock") -> int:
    return ctl.main(["--socket", socket, *argv])


def test_every_frozen_command_is_accepted(calls):
    forwarded = [cmd for cmd in ctl.COMMANDS if cmd not in ctl.LOCAL_COMMANDS]
    for command in forwarded:
        argv = [command]
        if command == "milestone":
            argv.append("CI green")
        assert run(*argv) == 0

    assert [cmd for _, cmd, _ in calls] == forwarded


def test_the_milestone_label_is_forwarded(calls):
    run("milestone", "CI green")

    assert calls[-1][2] == {"label": "CI green"}


def test_replay_forwards_an_optional_id(calls):
    run("replay")
    run("replay", "abc-123")

    assert calls[0][2] == {}
    assert calls[1][2] == {"id": "abc-123"}


def test_skip_forwards_an_optional_id(calls):
    run("skip")
    run("skip", "abc-123")

    assert [cmd for _, cmd, _ in calls] == ["skip", "skip"]
    assert calls[0][2] == {}
    assert calls[1][2] == {"id": "abc-123"}


def test_an_unknown_command_is_rejected_by_the_parser(calls):
    with pytest.raises(SystemExit):
        run("teleport")


def test_the_socket_override_is_used(calls):
    run("status", socket="/tmp/other.sock")

    assert calls[0][0] == "/tmp/other.sock"


def test_the_socket_path_defaults_to_the_config(monkeypatch, config, capsys):
    seen = []

    monkeypatch.setattr(ctl, "load_config", lambda **kwargs: config)
    monkeypatch.setattr(
        ctl, "request", lambda path, cmd, args=None, **kw: seen.append(str(path)) or {"ok": True}
    )

    ctl.main(["status"])

    assert seen == [str(config.socket_path)]


def test_status_is_printed_for_humans(monkeypatch, capsys):
    monkeypatch.setattr(
        ctl,
        "request",
        lambda *a, **k: {
            "ok": True,
            "data": {
                "version": "0.1.0",
                "pid": 42,
                "uptime_seconds": 12.5,
                "paused": False,
                "queued": 3,
                "open": 4,
                "summaries": "openai",
                "voice": "Paulina",
                "milestone_watch": False,
                "state_dir": "/tmp/state",
            },
        },
    )

    assert run("status") == 0

    out = capsys.readouterr().out
    assert "voice-loop 0.1.0" in out
    assert "queued:     3" in out
    assert "milestones: off" in out


def test_pendings_are_numbered(monkeypatch, capsys):
    monkeypatch.setattr(
        ctl,
        "request",
        lambda *a, **k: {
            "ok": True,
            "data": [
                {"id": "1", "ts": 1785700000, "type": "stop", "state": "pending",
                 "name": "alpha", "session_id": "s1", "summary": "quiere que revises"},
                {"id": "2", "ts": 1785700100, "type": "menu", "state": "queued",
                 "name": "", "session_id": "abcdef123456", "summary": ""},
            ],
        },
    )

    run("pendings")

    out = capsys.readouterr().out
    assert " 1. [pending   ]" in out
    assert "alpha (stop) — quiere que revises" in out
    assert "abcdef12 (menu)" in out


def test_an_empty_pendings_list_says_so(monkeypatch, capsys):
    monkeypatch.setattr(ctl, "request", lambda *a, **k: {"ok": True, "data": []})

    run("pendings")

    assert capsys.readouterr().out.strip() == "nothing pending"


def test_an_error_response_exits_non_zero(monkeypatch, capsys):
    monkeypatch.setattr(
        ctl, "request", lambda *a, **k: {"ok": False, "error": "not implemented: mic-toggle"}
    )

    assert run("mic-toggle") == 1
    assert "not implemented" in capsys.readouterr().err


def test_an_unreachable_daemon_exits_non_zero(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise ConnectionRefusedError("nobody home")

    monkeypatch.setattr(ctl, "request", boom)

    assert run("status") == 1
    assert "daemon unreachable" in capsys.readouterr().err


def test_json_mode_prints_the_raw_response(monkeypatch, capsys):
    monkeypatch.setattr(ctl, "request", lambda *a, **k: {"ok": True, "data": {"queued": 2}})

    assert run("--json", "status") == 0

    assert json.loads(capsys.readouterr().out) == {"ok": True, "data": {"queued": 2}}


def test_json_mode_still_signals_failure(monkeypatch, capsys):
    monkeypatch.setattr(ctl, "request", lambda *a, **k: {"ok": False, "error": "nope"})

    assert run("--json", "status") == 1


# --- the drift warning ----------------------------------------------------
#
# The daemon runs an installed copy of the clone (macOS TCC keeps LaunchAgents
# out of ~/Documents). Silence about that is how you spend an hour on a bug you
# already fixed.


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A clone and the runtime that was installed from it."""
    from voiceloop.runtime import write_manifest

    clone = tmp_path / "clone"
    (clone / "voiceloop").mkdir(parents=True)
    (clone / "voiceloop" / "daemon.py").write_text("x = 1\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    write_manifest(runtime, clone)
    monkeypatch.setenv("VOICE_LOOP_RUNTIME_DIR", str(runtime))
    return clone


def test_a_matching_runtime_says_nothing(calls, installed, capsys):
    ctl.main(["--socket", "/tmp/vl.sock", "--repo-root", str(installed), "status"])

    assert "install.sh" not in capsys.readouterr().err


def test_an_out_of_date_runtime_is_called_out(calls, installed, capsys):
    (installed / "voiceloop" / "daemon.py").write_text("x = 2  # the fix\n", encoding="utf-8")

    ctl.main(["--socket", "/tmp/vl.sock", "--repo-root", str(installed), "status"])

    err = capsys.readouterr().err
    assert "out of date" in err
    assert "install.sh" in err


def test_the_warning_does_not_stop_the_command(calls, installed, capsys):
    (installed / "voiceloop" / "daemon.py").write_text("x = 2\n", encoding="utf-8")

    code = ctl.main(["--socket", "/tmp/vl.sock", "--repo-root", str(installed), "status"])

    assert code == 0
    assert [cmd for _, cmd, _ in calls] == ["status"]


def test_the_warning_stays_off_stdout(installed, monkeypatch, capsys):
    (installed / "voiceloop" / "daemon.py").write_text("x = 2\n", encoding="utf-8")
    monkeypatch.setattr(ctl, "request", lambda *a, **k: {"ok": True, "data": {"queued": 2}})

    ctl.main(["--socket", "/tmp/vl.sock", "--repo-root", str(installed), "--json", "status"])

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ok": True, "data": {"queued": 2}}
    assert "out of date" in captured.err


# --- doctor ----------------------------------------------------------------


@pytest.fixture
def local_checks(monkeypatch):
    """The two OS probes, stubbed. Running them for real records audio."""
    from voiceloop import preflight

    recorded: list[dict] = []

    def run_all(**kwargs):
        recorded.append(kwargs)
        return [preflight.Check("microphone", preflight.OK, ":0")]

    monkeypatch.setattr(preflight, "run_all", run_all)
    return recorded


@pytest.fixture
def selfcheck(monkeypatch):
    """A daemon that answers `selfcheck`, and records that it was asked."""
    asked: list[str] = []

    def respond(socket_path, cmd, args=None, **kwargs):
        asked.append(cmd)
        return {
            "ok": True,
            "data": [{"name": "iterm automation", "status": "ok", "detail": "2 windows"}],
        }

    monkeypatch.setattr(ctl, "request", respond)
    return asked


def test_doctor_checks_here_and_asks_the_daemon_too(selfcheck, local_checks, capsys):
    """Two columns: the permissions are granted per process, not per machine."""
    assert run("doctor") == 0

    assert selfcheck == ["selfcheck"]
    out = capsys.readouterr().out
    assert "here (your terminal):" in out
    assert "there (the daemon):" in out
    assert "2 windows" in out


def test_doctor_reports_a_daemon_that_is_not_running(monkeypatch, local_checks, capsys):
    def refuse(*args, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(ctl, "request", refuse)

    assert run("doctor") == 1
    assert "unreachable" in capsys.readouterr().out


def test_doctor_fails_when_a_local_check_fails(selfcheck, monkeypatch, capsys):
    from voiceloop import preflight

    monkeypatch.setattr(
        preflight,
        "run_all",
        lambda **kwargs: [preflight.Check("microphone", preflight.FAILED, "denied")],
    )

    assert run("doctor") == 1
    assert "denied" in capsys.readouterr().out


def test_doctor_fails_when_the_daemon_reports_a_problem(monkeypatch, local_checks, capsys):
    monkeypatch.setattr(
        ctl,
        "request",
        lambda *a, **k: {
            "ok": True,
            "data": [{"name": "iterm automation", "status": "failed", "detail": "-1743"}],
        },
    )

    assert run("doctor") == 1
    assert "-1743" in capsys.readouterr().out


def test_doctor_probes_the_configured_device(selfcheck, local_checks):
    run("doctor")

    assert local_checks[0]["device"] == ":0"
    assert local_checks[0]["binary"] == "ffmpeg"


def test_doctor_reads_the_env_file_the_wrapper_does_not_source(
    selfcheck, local_checks, monkeypatch, tmp_path
):
    """Issue #6: the key is in the file; only `doctor` needs to see it."""
    env_path = tmp_path / "env"
    env_path.write_text("DEEPGRAM_API_KEY=dg-from-the-file\n", encoding="utf-8")
    monkeypatch.setenv("VOICE_LOOP_ENV_FILE", str(env_path))
    monkeypatch.setenv("DEEPGRAM_API_KEY", "")

    run("doctor")

    assert local_checks[0]["engine"].available is True
    assert local_checks[0]["env_file"].path == env_path


def test_doctor_without_an_env_file_reports_the_path_it_looked_at(
    selfcheck, local_checks, monkeypatch, tmp_path
):
    monkeypatch.setenv("VOICE_LOOP_ENV_FILE", str(tmp_path / "nope"))
    monkeypatch.setenv("DEEPGRAM_API_KEY", "")

    run("doctor")

    assert local_checks[0]["engine"].available is False
    assert local_checks[0]["env_file"].exists is False


def test_status_shows_busy_mode_and_the_recognizer(monkeypatch, capsys):
    monkeypatch.setattr(
        ctl,
        "request",
        lambda *a, **k: {
            "ok": True,
            "data": {"busy": True, "speech_to_text": "deepgram", "mic": "listening"},
        },
    )

    run("status")

    out = capsys.readouterr().out
    assert "busy:       True" in out
    assert "speech in:  deepgram" in out
    assert "mic:        listening" in out
