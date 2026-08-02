"""The hook runs inside every Claude session, so it is tested as Claude runs it:
as a subprocess, with the real JSON on stdin and a throwaway HOME.

Two properties are non-negotiable and asserted everywhere below: it exits 0 no
matter what, and it comes back fast.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "vl-hook.sh"

# Generous: the real budget is tens of milliseconds, but CI runners are slow and
# a flaky timing test is worse than no timing test.
LATENCY_BUDGET_SECONDS = 3.0


@pytest.fixture
def home(tmp_path):
    fake = tmp_path / "home"
    (fake / ".config" / "voice-loop").mkdir(parents=True)
    return fake


def run_hook(kind: str, payload, home: Path, *, state_dir: Path | None = None, env=None):
    import subprocess

    environ = dict(os.environ)
    environ["HOME"] = str(home)
    environ.pop("VOICE_LOOP_STATE_DIR", None)
    if state_dir is not None:
        environ["VOICE_LOOP_STATE_DIR"] = str(state_dir)
    environ.update(env or {})

    body = payload if isinstance(payload, str) else json.dumps(payload)
    started = time.monotonic()
    completed = subprocess.run(
        [str(HOOK), kind],
        input=body,
        capture_output=True,
        text=True,
        env=environ,
        timeout=30,
        check=False,
    )
    return completed, time.monotonic() - started


def spooled(state_dir: Path) -> list[dict]:
    spool = state_dir / "spool"
    if not spool.is_dir():
        return []
    files = sorted(entry for entry in spool.glob("*.json"))
    return [json.loads(entry.read_text(encoding="utf-8")) for entry in files]


def load_fixture(fixtures: Path, name: str) -> dict:
    return json.loads((fixtures / "hooks" / name).read_text(encoding="utf-8"))


def test_the_hook_is_executable():
    assert HOOK.stat().st_mode & stat.S_IXUSR


def test_a_stop_is_spooled(home, tmp_path, fixtures):
    state = tmp_path / "state"

    completed, elapsed = run_hook("stop", load_fixture(fixtures, "stop.json"), home, state_dir=state)

    assert completed.returncode == 0
    assert elapsed < LATENCY_BUDGET_SECONDS
    events = spooled(state)
    assert len(events) == 1
    assert events[0]["v"] == 1
    assert events[0]["type"] == "stop"
    assert events[0]["session_id"] == "11111111-2222-3333-4444-555555555555"
    assert events[0]["transcript_path"] == "/tmp/voice-loop-test/transcript.jsonl"
    assert events[0]["cwd"] == "/tmp/voice-loop-test/project"
    assert events[0]["payload"] == {}


def test_the_hook_stays_out_of_the_way(home, tmp_path, fixtures):
    """It runs on every prompt the user submits, so slow here is felt directly."""
    state = tmp_path / "state"
    payload = load_fixture(fixtures, "stop.json")
    run_hook("stop", payload, home, state_dir=state)  # warm the interpreter cache

    timings = sorted(run_hook("stop", payload, home, state_dir=state)[1] for _ in range(5))

    assert timings[2] < 1.0


def test_the_spool_filename_is_sortable_and_unique(home, tmp_path, fixtures):
    state = tmp_path / "state"
    payload = load_fixture(fixtures, "stop.json")

    for _ in range(3):
        run_hook("stop", payload, home, state_dir=state)

    names = sorted(entry.name for entry in (state / "spool").glob("*.json"))
    assert len(names) == 3
    assert len(set(names)) == 3
    assert all(name.split("-")[0].isdigit() for name in names)


def test_a_notification_carries_its_message(home, tmp_path, fixtures):
    state = tmp_path / "state"

    run_hook("notification", load_fixture(fixtures, "notification.json"), home, state_dir=state)

    event, = spooled(state)
    assert event["type"] == "notification"
    assert event["payload"]["message"] == "Claude needs your permission to use Bash"


def test_a_prompt_submit_becomes_activity(home, tmp_path, fixtures):
    state = tmp_path / "state"

    run_hook("activity", load_fixture(fixtures, "user_prompt_submit.json"), home, state_dir=state)

    event, = spooled(state)
    assert event["type"] == "activity"
    assert event["payload"]["trigger"] == "UserPromptSubmit"


def test_a_question_menu_keeps_its_structured_payload(home, tmp_path, fixtures):
    state = tmp_path / "state"

    run_hook("menu", load_fixture(fixtures, "pre_ask_user_question.json"), home, state_dir=state)

    event, = spooled(state)
    assert event["type"] == "menu"
    assert event["payload"]["tool"] == "AskUserQuestion"
    question = event["payload"]["tool_input"]["questions"][0]
    assert question["question"].startswith("¿Qué base de datos")
    assert [option["label"] for option in question["options"]] == ["SQLite", "Postgres"]


def test_a_plan_menu_keeps_the_plan(home, tmp_path, fixtures):
    state = tmp_path / "state"

    run_hook("menu", load_fixture(fixtures, "pre_exit_plan_mode.json"), home, state_dir=state)

    event, = spooled(state)
    assert "Migrar el índice" in event["payload"]["tool_input"]["plan"]


def test_a_pr_creating_bash_command_is_a_milestone(home, tmp_path, fixtures):
    state = tmp_path / "state"

    run_hook("bash", load_fixture(fixtures, "post_bash_pr_create.json"), home, state_dir=state)

    event, = spooled(state)
    assert event["type"] == "milestone"
    assert event["payload"]["label"] == "PR created"


def test_an_ordinary_bash_command_spools_nothing(home, tmp_path, fixtures):
    state = tmp_path / "state"

    completed, _ = run_hook(
        "bash", load_fixture(fixtures, "post_bash_plain.json"), home, state_dir=state
    )

    assert completed.returncode == 0
    assert spooled(state) == []


def test_the_state_dir_pointer_file_is_honoured(home, tmp_path, fixtures):
    state = tmp_path / "pointed-at"
    (home / ".config" / "voice-loop" / "state_dir").write_text(str(state), encoding="utf-8")

    completed, _ = run_hook("stop", load_fixture(fixtures, "stop.json"), home)

    assert completed.returncode == 0
    assert len(spooled(state)) == 1


def test_without_a_pointer_it_falls_back_to_the_default_under_home(home, tmp_path, fixtures):
    completed, _ = run_hook("stop", load_fixture(fixtures, "stop.json"), home)

    assert completed.returncode == 0
    assert len(spooled(home / ".local" / "state" / "voice-loop")) == 1


def test_the_env_override_wins_over_the_pointer(home, tmp_path, fixtures):
    pointed = tmp_path / "pointed-at"
    override = tmp_path / "override"
    (home / ".config" / "voice-loop" / "state_dir").write_text(str(pointed), encoding="utf-8")

    run_hook("stop", load_fixture(fixtures, "stop.json"), home, state_dir=override)

    assert spooled(pointed) == []
    assert len(spooled(override)) == 1


@pytest.mark.parametrize(
    "payload",
    ["", "   ", "not json at all", "[1, 2, 3]", '{"truncated": ', '"a string"'],
)
def test_garbage_on_stdin_never_fails_the_hook(home, tmp_path, payload):
    state = tmp_path / "state"

    completed, elapsed = run_hook("stop", payload, home, state_dir=state)

    assert completed.returncode == 0
    assert elapsed < LATENCY_BUDGET_SECONDS


def test_an_unparseable_payload_still_spools_a_usable_event(home, tmp_path):
    state = tmp_path / "state"

    run_hook("stop", "not json", home, state_dir=state)

    event, = spooled(state)
    assert event["type"] == "stop"
    assert event["session_id"] == ""


def test_a_wrong_typed_payload_does_not_break_the_hook(home, tmp_path):
    state = tmp_path / "state"

    completed, _ = run_hook(
        "notification", {"session_id": 5, "message": ["a", "list"], "cwd": None}, home,
        state_dir=state,
    )

    assert completed.returncode == 0
    event, = spooled(state)
    assert event["session_id"] == ""
    assert event["payload"]["message"] == ""


def test_an_unknown_kind_spools_nothing_and_exits_zero(home, tmp_path, fixtures):
    state = tmp_path / "state"

    completed, _ = run_hook("nonsense", load_fixture(fixtures, "stop.json"), home, state_dir=state)

    assert completed.returncode == 0
    assert spooled(state) == []


def test_no_argument_at_all_exits_zero(home, tmp_path):
    import subprocess

    environ = dict(os.environ, HOME=str(home))
    completed = subprocess.run(
        [str(HOOK)], input="{}", capture_output=True, text=True, env=environ, check=False
    )

    assert completed.returncode == 0


def test_an_unwritable_state_dir_does_not_fail_the_hook(home, tmp_path, fixtures):
    """If voice-loop is broken, Claude must not even notice."""
    blocked = tmp_path / "blocked"
    blocked.write_text("I am a file, not a directory", encoding="utf-8")

    completed, _ = run_hook("stop", load_fixture(fixtures, "stop.json"), home, state_dir=blocked)

    assert completed.returncode == 0


def test_a_huge_payload_is_clipped_not_spooled_whole(home, tmp_path, fixtures):
    state = tmp_path / "state"
    payload = load_fixture(fixtures, "pre_exit_plan_mode.json")
    payload["tool_input"]["plan"] = "x" * 50_000

    completed, elapsed = run_hook("menu", payload, home, state_dir=state)

    assert completed.returncode == 0
    assert elapsed < LATENCY_BUDGET_SECONDS
    event, = spooled(state)
    assert len(event["payload"]["tool_input"]["plan"]) == 8000


def test_the_tty_is_resolved_by_walking_up_the_process_tree(home, tmp_path, fixtures):
    """`ps` is faked so this works the same on macOS and on a Linux CI box."""
    state = tmp_path / "state"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_ps = bindir / "ps"
    fake_ps.write_text(
        "#!/bin/sh\n"
        # first hop: no tty (the hook's own subprocess), second: the claude session
        'if [ -f "$TMPDIR_MARKER" ]; then echo "1 ttys042"; else : > "$TMPDIR_MARKER"; '
        'echo "4242 ??"; fi\n',
        encoding="utf-8",
    )
    fake_ps.chmod(0o755)

    completed, _ = run_hook(
        "stop",
        load_fixture(fixtures, "stop.json"),
        home,
        state_dir=state,
        env={
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "TMPDIR_MARKER": str(tmp_path / "marker"),
        },
    )

    assert completed.returncode == 0
    event, = spooled(state)
    assert event["tty"] == "/dev/ttys042"


def test_no_tty_anywhere_up_the_tree_is_not_fatal(home, tmp_path, fixtures):
    state = tmp_path / "state"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_ps = bindir / "ps"
    fake_ps.write_text('#!/bin/sh\necho "1 ??"\n', encoding="utf-8")
    fake_ps.chmod(0o755)

    completed, _ = run_hook(
        "stop",
        load_fixture(fixtures, "stop.json"),
        home,
        state_dir=state,
        env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"},
    )

    assert completed.returncode == 0
    event, = spooled(state)
    assert event["tty"] == ""
