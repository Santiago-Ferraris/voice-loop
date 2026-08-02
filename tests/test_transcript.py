from __future__ import annotations

import json

import pytest

from voiceloop.transcript import pending_subagents, tail_text

LAUNCH = "Async agent launched successfully. (internal metadata)\nagentId: {}"
NOTIFICATION = (
    "<task-notification>\n<task-id>{}</task-id>\n<status>completed</status>\n"
    "</task-notification>"
)


def tool_result(text: str) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"tool_use_id": "toolu_1", "type": "tool_result",
                 "content": [{"type": "text", "text": text}]}
            ],
        },
    }


def assistant(text: str, *, sidechain: bool = False) -> dict:
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def write_jsonl(path, records) -> str:
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )
    return str(path)


# --- the gate -------------------------------------------------------------


def test_no_launches_means_nothing_in_flight(tmp_path):
    path = write_jsonl(tmp_path / "t.jsonl", [assistant("Listo.")])

    assert pending_subagents(path) == 0


def test_launched_minus_completed(fixtures):
    assert pending_subagents(fixtures / "transcripts/one_agent_in_flight.jsonl") == 1
    assert pending_subagents(fixtures / "transcripts/no_agents_pending.jsonl") == 0


def test_several_agents_in_flight(tmp_path):
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [tool_result(LAUNCH.format(agent)) for agent in ("aa11", "bb22", "cc33")],
    )

    assert pending_subagents(path) == 3


def test_completions_in_any_order_still_cancel_out(tmp_path):
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [
            tool_result(LAUNCH.format("aa11")),
            tool_result(LAUNCH.format("bb22")),
            tool_result(NOTIFICATION.format("bb22")),
            tool_result(NOTIFICATION.format("aa11")),
        ],
    )

    assert pending_subagents(path) == 0


def test_a_completion_without_a_launch_never_goes_negative(tmp_path):
    path = write_jsonl(tmp_path / "t.jsonl", [tool_result(NOTIFICATION.format("aa11"))])

    assert pending_subagents(path) == 0


def test_the_same_agent_launched_twice_counts_once(tmp_path):
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [tool_result(LAUNCH.format("aa11")), tool_result(LAUNCH.format("aa11"))],
    )

    assert pending_subagents(path) == 1


def test_launch_and_completion_in_the_same_message(tmp_path):
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [tool_result(LAUNCH.format("aa11") + "\n" + NOTIFICATION.format("aa11"))],
    )

    assert pending_subagents(path) == 0


def test_plain_string_content_is_searched_too(tmp_path):
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [{"type": "user", "message": {"role": "user", "content": LAUNCH.format("aa11")}}],
    )

    assert pending_subagents(path) == 1


@pytest.mark.parametrize("missing", ["", None, "/nonexistent/path.jsonl"])
def test_a_missing_transcript_fails_open(missing):
    assert pending_subagents(missing) == 0


def test_a_malformed_transcript_fails_open(fixtures):
    """Announcing too much beats swallowing a request over a parse error."""
    assert pending_subagents(fixtures / "transcripts/malformed.jsonl") == 0


def test_an_empty_transcript_fails_open(fixtures):
    assert pending_subagents(fixtures / "transcripts/empty.jsonl") == 0


def test_a_directory_instead_of_a_file_fails_open(tmp_path):
    assert pending_subagents(tmp_path) == 0


def test_a_garbage_line_between_good_ones_is_skipped(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps(tool_result(LAUNCH.format("aa11")))
        + "\n{ truncated\n\n"
        + json.dumps(tool_result(LAUNCH.format("bb22")))
        + "\n",
        encoding="utf-8",
    )

    assert pending_subagents(path) == 2


# --- the tail -------------------------------------------------------------


def test_tail_is_the_last_assistant_message(tmp_path):
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [assistant("First answer."), assistant("Second and final answer.")],
    )

    assert tail_text(path) == "Second and final answer."


def test_tail_skips_thinking_and_tool_calls(fixtures):
    tail = tail_text(fixtures / "transcripts/no_agents_pending.jsonl")

    assert tail == "Both agents are done. Do you want me to open the pull request?"
    assert "internal reasoning" not in tail


def test_tail_ignores_subagent_sidechains(tmp_path):
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [assistant("Main thread answer."), assistant("Subagent noise.", sidechain=True)],
    )

    assert tail_text(path) == "Main thread answer."


def test_tail_joins_multiple_text_blocks_of_one_message(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "isSidechain": False,
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Part one."},
                        {"type": "text", "text": "Part two."},
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert tail_text(path) == "Part one.\nPart two."


def test_tail_keeps_the_end_when_the_answer_is_long(tmp_path):
    long_answer = ("a" * 5000) + " ¿seguimos?"
    path = write_jsonl(tmp_path / "t.jsonl", [assistant(long_answer)])

    tail = tail_text(path, max_chars=100)

    assert len(tail) == 100
    assert tail.endswith("¿seguimos?")


@pytest.mark.parametrize("missing", ["", None, "/nonexistent/path.jsonl"])
def test_tail_of_a_missing_transcript_is_empty(missing):
    assert tail_text(missing) == ""


def test_tail_of_a_malformed_transcript_is_empty(fixtures):
    assert tail_text(fixtures / "transcripts/malformed.jsonl") == ""


def test_tail_of_a_transcript_without_assistant_text_is_empty(tmp_path):
    path = write_jsonl(tmp_path / "t.jsonl", [tool_result(LAUNCH.format("aa11"))])

    assert tail_text(path) == ""
