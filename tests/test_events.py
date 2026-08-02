from __future__ import annotations

import pytest

from voiceloop.events import (
    EVENT_TYPES,
    SCHEMA_VERSION,
    Event,
    EventError,
    is_pr_create_command,
)


def valid_payload(**overrides) -> dict:
    raw = {
        "v": 1,
        "id": "6f1c2f7e-0000-4000-8000-000000000000",
        "ts": 1785700000,
        "type": "stop",
        "session_id": "session-1",
        "tty": "/dev/ttys012",
        "cwd": "/tmp/projects/workspace",
        "transcript_path": "/tmp/t.jsonl",
        "payload": {},
    }
    raw.update(overrides)
    return raw


def test_round_trip_preserves_every_field():
    event = Event.from_dict(valid_payload(payload={"message": "hola"}))

    assert Event.from_dict(event.to_dict()) == event
    assert event.to_dict()["v"] == SCHEMA_VERSION


def test_new_stamps_an_id_and_a_timestamp():
    first = Event.new("menu", "session-1", payload={"tool": "AskUserQuestion"})
    second = Event.new("menu", "session-1")

    assert first.id != second.id
    assert first.ts > 0
    assert first.payload == {"tool": "AskUserQuestion"}
    assert second.payload == {}


def test_new_rejects_an_unknown_type():
    with pytest.raises(EventError, match="unknown event type"):
        Event.new("explode", "session-1")


def test_every_declared_type_parses():
    for event_type in EVENT_TYPES:
        assert Event.from_dict(valid_payload(type=event_type)).type == event_type


@pytest.mark.parametrize(
    "override, expected",
    [
        ({"v": 2}, "schema version"),
        ({"v": None}, "schema version"),
        ({"type": "explode"}, "unknown event type"),
        ({"type": ""}, "non-empty string"),
        ({"id": ""}, "non-empty string"),
        ({"id": 17}, "non-empty string"),
        ({"ts": "recently"}, "expected a number"),
        ({"ts": True}, "expected a number"),
        ({"payload": "nope"}, "expected an object"),
        ({"tty": 12}, "expected a string"),
    ],
)
def test_schema_violations_are_rejected(override, expected):
    with pytest.raises(EventError, match=expected):
        Event.from_dict(valid_payload(**override))


def test_a_non_object_is_rejected():
    with pytest.raises(EventError, match="JSON object"):
        Event.from_dict([1, 2, 3])


def test_optional_fields_default_to_empty():
    raw = {"v": 1, "id": "x", "ts": 1, "type": "activity"}

    event = Event.from_dict(raw)

    assert (event.session_id, event.tty, event.cwd, event.transcript_path) == ("", "", "", "")
    assert event.payload == {}


def test_float_timestamps_are_truncated_to_seconds():
    assert Event.from_dict(valid_payload(ts=1785700000.99)).ts == 1785700000


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create",
        "gh pr create --fill --base main",
        "cd repo && gh pr create --fill",
        "gh  pr   create --draft",
        "gh pr\n  create --title x",
        'git push && gh pr create --body "$(cat body.md)"',
    ],
)
def test_pr_creating_commands_are_detected(command):
    assert is_pr_create_command(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "",
        "gh pr view 12",
        "gh pr list",
        "gh pr checks --watch",
        "echo gh pr",
        "ghpr create",
        "gh_pr_create",
        None,
        42,
    ],
)
def test_other_commands_are_not_milestones(command):
    assert is_pr_create_command(command) is False
