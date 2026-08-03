"""The env file, parsed rather than sourced — see voiceloop/envfile.py."""

from __future__ import annotations

import os

from voiceloop import envfile


def write(tmp_path, body: str):
    path = tmp_path / "env"
    path.write_text(body, encoding="utf-8")
    return path


def test_a_plain_assignment_is_read(tmp_path):
    parsed = envfile.read(write(tmp_path, "DEEPGRAM_API_KEY=dg-123\n"))

    assert parsed.exists is True
    assert parsed.values["DEEPGRAM_API_KEY"] == "dg-123"
    assert parsed.has("DEEPGRAM_API_KEY")


def test_comments_blank_lines_exports_and_quotes(tmp_path):
    body = (
        "# voice-loop secrets\n"
        "\n"
        "export OPENAI_API_KEY='sk-quoted'\n"
        '  DEEPGRAM_API_KEY = "dg-spaced"  \n'
        "# OPENAI_API_KEY=sk-commented-out\n"
        "NOT AN ASSIGNMENT\n"
    )

    values = envfile.read(write(tmp_path, body)).values

    assert values == {"OPENAI_API_KEY": "sk-quoted", "DEEPGRAM_API_KEY": "dg-spaced"}


def test_a_trailing_comment_is_not_part_of_an_unquoted_value(tmp_path):
    parsed = envfile.read(write(tmp_path, "DEEPGRAM_API_KEY=dg-123 # the real one\n"))

    assert parsed.values["DEEPGRAM_API_KEY"] == "dg-123"


def test_a_missing_file_is_not_an_error(tmp_path):
    parsed = envfile.read(tmp_path / "nope")

    assert parsed.exists is False
    assert parsed.values == {}
    assert parsed.has("DEEPGRAM_API_KEY") is False


def test_the_three_states_are_told_apart(tmp_path):
    missing = envfile.read(tmp_path / "nope")
    empty = envfile.read(write(tmp_path, "OPENAI_API_KEY=sk-1\n"))
    present = envfile.read(write(tmp_path, "DEEPGRAM_API_KEY=dg-1\n"))

    assert "no env file at" in missing.why_missing("DEEPGRAM_API_KEY")
    assert "DEEPGRAM_API_KEY is not in" in empty.why_missing("DEEPGRAM_API_KEY")
    assert "did not take it" in present.why_missing("DEEPGRAM_API_KEY")


def test_the_path_honours_the_override(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_LOOP_ENV_FILE", str(tmp_path / "elsewhere"))

    assert envfile.path_for() == tmp_path / "elsewhere"


def test_the_default_path_is_outside_the_repo(monkeypatch):
    monkeypatch.delenv("VOICE_LOOP_ENV_FILE", raising=False)

    assert envfile.path_for().name == "env"
    assert envfile.path_for().parent.name == "voice-loop"


def test_the_overlay_lets_the_file_win_and_ignores_empty_values(tmp_path):
    parsed = envfile.read(write(tmp_path, "DEEPGRAM_API_KEY=dg-1\nOPENAI_API_KEY=\n"))

    merged = envfile.overlay(parsed, {"DEEPGRAM_API_KEY": "stale", "OPENAI_API_KEY": "sk-env"})

    # `set -a; . file` overwrites what the shell already had…
    assert merged["DEEPGRAM_API_KEY"] == "dg-1"
    # …but an empty assignment is not a key, and must not blank a real one.
    assert merged["OPENAI_API_KEY"] == "sk-env"


def test_the_overlay_does_not_touch_the_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "from-the-shell")
    parsed = envfile.read(write(tmp_path, "DEEPGRAM_API_KEY=dg-1\n"))

    assert envfile.overlay(parsed)["DEEPGRAM_API_KEY"] == "dg-1"
    assert os.environ["DEEPGRAM_API_KEY"] == "from-the-shell"
