from __future__ import annotations

from voiceloop.milestones import MilestoneWatcher

MILESTONES = {"pr": "PR created", "ci": "CI green"}


def watcher(directory, **overrides) -> MilestoneWatcher:
    options = {
        "enabled": True,
        "directory": directory,
        "pattern": "*.phase",
        "milestones": dict(MILESTONES),
    }
    options.update(overrides)
    return MilestoneWatcher(**options)


def phase(directory, name: str, value: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.phase").write_text(value, encoding="utf-8")


def test_disabled_by_default():
    assert MilestoneWatcher().active is False
    assert list(MilestoneWatcher().poll()) == []


def test_enabled_without_a_directory_stays_inactive():
    assert watcher(None).active is False


def test_enabled_without_milestones_stays_inactive(tmp_path):
    assert watcher(tmp_path, milestones={}).active is False


def test_an_explicitly_disabled_watcher_reports_nothing(tmp_path):
    phase(tmp_path, "ttys001", "pr")
    subject = watcher(tmp_path, enabled=False)
    subject.baseline()

    phase(tmp_path, "ttys001", "ci")

    assert list(subject.poll()) == []


def test_startup_does_not_chime_for_what_is_already_there(tmp_path):
    """Fifteen tabs already sitting on a milestone must produce silence."""
    for index in range(15):
        phase(tmp_path, f"ttys{index:03d}", "ci")
    subject = watcher(tmp_path)

    subject.baseline()

    assert list(subject.poll()) == []


def test_the_first_poll_baselines_itself_if_you_forget(tmp_path):
    phase(tmp_path, "ttys001", "ci")
    subject = watcher(tmp_path)

    assert list(subject.poll()) == []
    assert list(subject.poll()) == []


def test_a_transition_into_a_mapped_phase_is_a_milestone(tmp_path):
    phase(tmp_path, "ttys001", "working")
    subject = watcher(tmp_path)
    subject.baseline()

    phase(tmp_path, "ttys001", "ci")
    found = list(subject.poll())

    assert len(found) == 1
    assert (found[0].key, found[0].phase, found[0].label) == ("ttys001.phase", "ci", "CI green")


def test_a_transition_into_an_unmapped_phase_is_ignored(tmp_path):
    phase(tmp_path, "ttys001", "ci")
    subject = watcher(tmp_path)
    subject.baseline()

    phase(tmp_path, "ttys001", "working")

    assert list(subject.poll()) == []


def test_the_same_phase_written_again_is_not_a_transition(tmp_path):
    phase(tmp_path, "ttys001", "working")
    subject = watcher(tmp_path)
    subject.baseline()

    phase(tmp_path, "ttys001", "ci")
    assert len(list(subject.poll())) == 1

    phase(tmp_path, "ttys001", "ci")
    assert list(subject.poll()) == []


def test_going_back_and_forth_chimes_each_time_it_returns(tmp_path):
    phase(tmp_path, "ttys001", "ci")
    subject = watcher(tmp_path)
    subject.baseline()

    phase(tmp_path, "ttys001", "working")
    assert list(subject.poll()) == []

    phase(tmp_path, "ttys001", "ci")
    assert [m.label for m in subject.poll()] == ["CI green"]


def test_a_brand_new_file_already_on_a_milestone_counts(tmp_path):
    subject = watcher(tmp_path)
    subject.baseline()

    phase(tmp_path, "ttys009", "pr")

    assert [m.label for m in subject.poll()] == ["PR created"]


def test_several_tabs_transition_at_once(tmp_path):
    for name in ("ttys001", "ttys002", "ttys003"):
        phase(tmp_path, name, "working")
    subject = watcher(tmp_path)
    subject.baseline()

    phase(tmp_path, "ttys001", "ci")
    phase(tmp_path, "ttys003", "pr")

    assert sorted(m.label for m in subject.poll()) == ["CI green", "PR created"]


def test_the_pattern_is_respected(tmp_path):
    subject = watcher(tmp_path, pattern="*.state")
    subject.baseline()
    phase(tmp_path, "ttys001", "ci")
    (tmp_path / "ttys002.state").write_text("ci", encoding="utf-8")

    assert [m.key for m in subject.poll()] == ["ttys002.state"]


def test_whitespace_around_the_phase_is_trimmed(tmp_path):
    subject = watcher(tmp_path)
    subject.baseline()
    (tmp_path / "ttys001.phase").write_text("  ci\n", encoding="utf-8")

    assert [m.label for m in subject.poll()] == ["CI green"]


def test_a_missing_directory_is_not_an_error(tmp_path):
    subject = watcher(tmp_path / "nope")
    subject.baseline()

    assert list(subject.poll()) == []


def test_a_deleted_file_is_forgotten_and_can_fire_again(tmp_path):
    phase(tmp_path, "ttys001", "ci")
    subject = watcher(tmp_path)
    subject.baseline()

    (tmp_path / "ttys001.phase").unlink()
    assert list(subject.poll()) == []

    phase(tmp_path, "ttys001", "ci")
    assert [m.label for m in subject.poll()] == ["CI green"]


def test_from_config_is_off_with_the_shipped_defaults(config):
    subject = MilestoneWatcher.from_config(config)

    assert subject.enabled is False
    assert subject.active is False
    assert subject.pattern == "*.phase"
    assert subject.milestones


def test_from_config_expands_the_tilde_in_dir(config, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    config.data["integrations"]["milestone_file_watch"]["dir"] = "~/phases"

    subject = MilestoneWatcher.from_config(config)

    assert subject.directory == tmp_path / "phases"
