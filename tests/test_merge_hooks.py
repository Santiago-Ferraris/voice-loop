"""`hooks/merge-hooks.py` edits the file fifteen live Claude sessions read from,
so the bar is: idempotent, surgical, and never destructive.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = "/opt/voice-loop/hooks/vl-hook.sh"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "merge_hooks", REPO_ROOT / "hooks" / "merge-hooks.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


merge_hooks = load_module()


def commands(settings: dict) -> list[str]:
    found = []
    for groups in (settings.get("hooks") or {}).values():
        for group in groups:
            for entry in group.get("hooks", []):
                found.append(entry.get("command"))
    return found


def run(tmp_path, *args) -> int:
    return merge_hooks.main(list(args))


def settings_path(tmp_path) -> Path:
    return tmp_path / ".claude" / "settings.json"


def write_settings(tmp_path, data) -> Path:
    path = settings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        data if isinstance(data, str) else json.dumps(data, indent=2), encoding="utf-8"
    )
    return path


def read_settings(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --- the merge ------------------------------------------------------------


def test_all_six_hooks_are_installed(tmp_path):
    path = write_settings(tmp_path, {})

    run(tmp_path, "--settings", str(path), "--hook-script", HOOK_SCRIPT)

    settings = read_settings(path)
    assert sorted(settings["hooks"]) == [
        "Notification",
        "PostToolUse",
        "PreToolUse",
        "Stop",
        "UserPromptSubmit",
    ]
    assert sorted(commands(settings)) == sorted(
        [
            f"{HOOK_SCRIPT} stop",
            f"{HOOK_SCRIPT} notification",
            f"{HOOK_SCRIPT} activity",
            f"{HOOK_SCRIPT} menu",
            f"{HOOK_SCRIPT} activity",
            f"{HOOK_SCRIPT} bash",
        ]
    )


def test_matchers_are_set_where_they_matter(tmp_path):
    path = write_settings(tmp_path, {})

    run(tmp_path, "--settings", str(path), "--hook-script", HOOK_SCRIPT)

    settings = read_settings(path)
    pre = settings["hooks"]["PreToolUse"]
    assert [group["matcher"] for group in pre] == ["AskUserQuestion|ExitPlanMode"]
    post = {group["matcher"]: group for group in settings["hooks"]["PostToolUse"]}
    assert sorted(post) == ["AskUserQuestion|ExitPlanMode", "Bash"]
    assert "matcher" not in settings["hooks"]["Stop"][0]


def test_a_missing_settings_file_is_created(tmp_path):
    path = settings_path(tmp_path)

    run(tmp_path, "--settings", str(path), "--hook-script", HOOK_SCRIPT)

    assert path.exists()
    assert len(commands(read_settings(path))) == 6


def test_an_empty_settings_file_is_handled(tmp_path):
    path = write_settings(tmp_path, "")

    run(tmp_path, "--settings", str(path), "--hook-script", HOOK_SCRIPT)

    assert len(commands(read_settings(path))) == 6


# --- idempotency ----------------------------------------------------------


def test_running_twice_produces_identical_bytes(tmp_path):
    path = write_settings(tmp_path, {"model": "opus"})
    run(tmp_path, "--settings", str(path), "--hook-script", HOOK_SCRIPT)
    first = path.read_text(encoding="utf-8")

    run(tmp_path, "--settings", str(path), "--hook-script", HOOK_SCRIPT)

    assert path.read_text(encoding="utf-8") == first
    assert len(commands(read_settings(path))) == 6


def test_a_no_op_run_does_not_leave_a_second_backup(tmp_path):
    path = write_settings(tmp_path, {})
    run(tmp_path, "--settings", str(path), "--hook-script", HOOK_SCRIPT)
    backups = list(path.parent.glob("*voice-loop-backup*"))

    run(tmp_path, "--settings", str(path), "--hook-script", HOOK_SCRIPT)

    assert list(path.parent.glob("*voice-loop-backup*")) == backups


def test_moving_the_clone_rewrites_the_path_instead_of_duplicating(tmp_path):
    path = write_settings(tmp_path, {})
    run(tmp_path, "--settings", str(path), "--hook-script", "/old/place/hooks/vl-hook.sh")

    run(tmp_path, "--settings", str(path), "--hook-script", "/new/place/hooks/vl-hook.sh")

    found = commands(read_settings(path))
    assert len(found) == 6
    assert all(command.startswith("/new/place") for command in found)


# --- not touching anything else -------------------------------------------


def test_unrelated_settings_survive(tmp_path):
    path = write_settings(
        tmp_path,
        {"model": "opus", "permissions": {"allow": ["Bash(ls:*)"]}, "theme": "dark"},
    )

    run(tmp_path, "--settings", str(path), "--hook-script", HOOK_SCRIPT)

    settings = read_settings(path)
    assert settings["model"] == "opus"
    assert settings["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert settings["theme"] == "dark"


def test_someone_elses_hooks_survive_and_share_the_group(tmp_path):
    path = write_settings(
        tmp_path,
        {
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "/other/tool.sh on-stop"}]}],
                "SessionStart": [{"hooks": [{"type": "command", "command": "/other/start.sh"}]}],
            }
        },
    )

    run(tmp_path, "--settings", str(path), "--hook-script", HOOK_SCRIPT)

    settings = read_settings(path)
    stop_commands = [entry["command"] for entry in settings["hooks"]["Stop"][0]["hooks"]]
    assert stop_commands == ["/other/tool.sh on-stop", f"{HOOK_SCRIPT} stop"]
    assert settings["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "/other/start.sh"


def test_an_existing_matcher_group_is_reused_not_duplicated(tmp_path):
    path = write_settings(
        tmp_path,
        {
            "hooks": {
                "PostToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "/other.sh"}]}
                ]
            }
        },
    )

    run(tmp_path, "--settings", str(path), "--hook-script", HOOK_SCRIPT)

    post = read_settings(path)["hooks"]["PostToolUse"]
    bash_groups = [group for group in post if group.get("matcher") == "Bash"]
    assert len(bash_groups) == 1
    assert len(bash_groups[0]["hooks"]) == 2


def test_a_wildcard_matcher_counts_as_no_matcher(tmp_path):
    path = write_settings(
        tmp_path,
        {"hooks": {"Stop": [{"matcher": "*", "hooks": [{"type": "command", "command": "/o.sh"}]}]}},
    )

    run(tmp_path, "--settings", str(path), "--hook-script", HOOK_SCRIPT)

    assert len(read_settings(path)["hooks"]["Stop"]) == 1


# --- backups and safety ---------------------------------------------------


def test_the_previous_settings_are_backed_up_verbatim(tmp_path):
    original = {"model": "opus", "hooks": {"Stop": [{"hooks": [{"command": "/other.sh"}]}]}}
    path = write_settings(tmp_path, original)
    before = path.read_text(encoding="utf-8")

    run(tmp_path, "--settings", str(path), "--hook-script", HOOK_SCRIPT)

    backup, = path.parent.glob("*voice-loop-backup*")
    assert backup.read_text(encoding="utf-8") == before


def test_invalid_json_is_refused_rather_than_overwritten(tmp_path):
    path = write_settings(tmp_path, '{"model": "opus",,,}')

    with pytest.raises(SystemExit, match="not valid JSON"):
        run(tmp_path, "--settings", str(path), "--hook-script", HOOK_SCRIPT)

    assert path.read_text(encoding="utf-8") == '{"model": "opus",,,}'


def test_a_settings_file_that_is_not_an_object_is_refused(tmp_path):
    path = write_settings(tmp_path, "[1, 2, 3]")

    with pytest.raises(SystemExit, match="JSON object"):
        run(tmp_path, "--settings", str(path), "--hook-script", HOOK_SCRIPT)


def test_a_hooks_key_of_the_wrong_shape_is_refused(tmp_path):
    path = write_settings(tmp_path, {"hooks": "nope"})

    with pytest.raises(SystemExit, match="not an object"):
        run(tmp_path, "--settings", str(path), "--hook-script", HOOK_SCRIPT)


def test_dry_run_changes_nothing(tmp_path, capsys):
    path = write_settings(tmp_path, {"model": "opus"})
    before = path.read_text(encoding="utf-8")

    run(tmp_path, "--settings", str(path), "--hook-script", HOOK_SCRIPT, "--dry-run")

    assert path.read_text(encoding="utf-8") == before
    assert HOOK_SCRIPT in capsys.readouterr().out


# --- removal --------------------------------------------------------------


def test_remove_takes_out_only_our_hooks(tmp_path):
    path = write_settings(
        tmp_path,
        {
            "model": "opus",
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/other.sh"}]}]},
        },
    )
    run(tmp_path, "--settings", str(path), "--hook-script", HOOK_SCRIPT)

    run(tmp_path, "--settings", str(path), "--remove")

    settings = read_settings(path)
    assert commands(settings) == ["/other.sh"]
    assert settings["model"] == "opus"


def test_remove_restores_the_original_shape(tmp_path):
    path = write_settings(tmp_path, {"model": "opus"})
    before = path.read_text(encoding="utf-8")
    run(tmp_path, "--settings", str(path), "--hook-script", HOOK_SCRIPT)

    run(tmp_path, "--settings", str(path), "--remove")

    assert json.loads(path.read_text()) == json.loads(before)
    assert "hooks" not in read_settings(path)


def test_removing_twice_is_a_no_op(tmp_path):
    path = write_settings(tmp_path, {"model": "opus"})
    run(tmp_path, "--settings", str(path), "--hook-script", HOOK_SCRIPT)
    run(tmp_path, "--settings", str(path), "--remove")
    after = path.read_text(encoding="utf-8")

    run(tmp_path, "--settings", str(path), "--remove")

    assert path.read_text(encoding="utf-8") == after


def test_remove_on_a_file_we_never_touched_is_harmless(tmp_path):
    path = write_settings(tmp_path, {"model": "opus"})
    before = path.read_text(encoding="utf-8")

    run(tmp_path, "--settings", str(path), "--remove")

    assert path.read_text(encoding="utf-8") == before


def test_hook_script_is_required_unless_removing(tmp_path):
    path = write_settings(tmp_path, {})

    with pytest.raises(SystemExit):
        run(tmp_path, "--settings", str(path))
