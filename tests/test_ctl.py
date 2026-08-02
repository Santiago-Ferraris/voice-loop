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
    for command in ctl.COMMANDS:
        argv = [command]
        if command == "milestone":
            argv.append("CI green")
        assert run(*argv) == 0

    assert [cmd for _, cmd, _ in calls] == list(ctl.COMMANDS)


def test_the_milestone_label_is_forwarded(calls):
    run("milestone", "CI green")

    assert calls[-1][2] == {"label": "CI green"}


def test_replay_forwards_an_optional_id(calls):
    run("replay")
    run("replay", "abc-123")

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
