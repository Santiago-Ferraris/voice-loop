from __future__ import annotations

import pytest

from voiceloop import announce
from voiceloop.store import STATE_QUEUED, Item

PHONETIC = {
    "pull request": "pul rikuest",
    "pr": "pi ar",
    "merge": "merch",
    "deploy": "diploi",
}


def item(event_type: str = "stop", **payload) -> Item:
    return Item(
        id="id-1",
        ts=1000,
        type=event_type,
        session_id="session-1",
        tty="/dev/ttys012",
        cwd="/tmp/projects/workspace",
        transcript_path="/tmp/t.jsonl",
        name=None,
        state=STATE_QUEUED,
        summary=None,
        payload=payload,
        announced_at=None,
        resolved_at=None,
        resolved_by=None,
    )


# --- "quedan N" -----------------------------------------------------------


def test_nothing_left_says_nothing():
    assert announce.remaining_phrase(0) == ""
    assert announce.remaining_phrase(-1) == ""


def test_one_left_is_singular():
    assert announce.remaining_phrase(1) == "Queda uno"


@pytest.mark.parametrize("count, expected", [(2, "Quedan 2"), (7, "Quedan 7"), (15, "Quedan 15")])
def test_several_left_is_plural(count, expected):
    assert announce.remaining_phrase(count) == expected


def test_an_empty_queue_ends_the_announcement_cleanly():
    result = announce.build(item(), name="migration", summary="terminó el backfill", remaining=0)

    assert result.text == "migration: terminó el backfill."


def test_the_queue_tail_is_appended():
    result = announce.build(item(), name="migration", summary="terminó el backfill", remaining=3)

    assert result.text == "migration: terminó el backfill. Quedan 3."


def test_one_remaining_reads_as_a_word():
    result = announce.build(item(), name="migration", summary="listo", remaining=1)

    assert result.text.endswith("Queda uno.")


def test_a_stop_without_a_summary_uses_the_fallback_phrase():
    result = announce.build(item(), name="migration", summary=None, remaining=0)

    assert result.text == "migration: terminó y te espera."


# --- menus ----------------------------------------------------------------


def test_menu_options_are_enumerated_as_words():
    payload = {
        "tool": "AskUserQuestion",
        "tool_input": {
            "questions": [
                {
                    "question": "¿Qué base uso?",
                    "options": [{"label": "SQLite"}, {"label": "Postgres"}, {"label": "Ninguna"}],
                }
            ]
        },
    }

    result = announce.build(item("menu", **payload), name="indice", remaining=0)

    assert result.text == (
        "indice: ¿Qué base uso? Opciones: uno: SQLite, dos: Postgres, tres: Ninguna."
    )


def test_option_numbers_beyond_ten_fall_back_to_digits():
    assert [announce.number_word(n) for n in (1, 5, 10)] == ["uno", "cinco", "diez"]
    assert announce.number_word(11) == "11"


def test_a_menu_without_options_still_reads_the_question():
    payload = {"tool_input": {"questions": [{"question": "¿Seguimos?"}]}}

    result = announce.build(item("menu", **payload), name="indice", remaining=0)

    assert result.text == "indice: ¿Seguimos?"


def test_extra_questions_are_flagged_not_read():
    payload = {
        "tool_input": {
            "questions": [
                {"question": "Primera", "options": [{"label": "Sí"}]},
                {"question": "Segunda"},
                {"question": "Tercera"},
            ]
        }
    }

    text = announce.build(item("menu", **payload), name="x", remaining=0).text

    assert "Primera" in text
    assert "Segunda" not in text
    assert "Y 2 preguntas más" in text


def test_a_single_extra_question_is_singular():
    payload = {"tool_input": {"questions": [{"question": "Primera"}, {"question": "Segunda"}]}}

    assert "Y una pregunta más" in announce.build(item("menu", **payload), name="x").text


def test_long_option_labels_are_clipped():
    payload = {
        "tool_input": {
            "questions": [{"question": "¿Cuál?", "options": [{"label": "x" * 200}]}]
        }
    }

    text = announce.build(item("menu", **payload), name="x").text

    assert "…" in text
    assert len(text) < 200


def test_an_option_without_a_label_uses_its_description():
    payload = {
        "tool_input": {
            "questions": [{"question": "¿Cuál?", "options": [{"description": "la segura"}]}]
        }
    }

    assert "uno: la segura" in announce.build(item("menu", **payload), name="x").text


def test_a_malformed_menu_payload_still_says_something():
    for payload in ({}, {"tool_input": {}}, {"tool_input": {"questions": []}},
                    {"tool_input": {"questions": "nope"}}):
        text = announce.build(item("menu", **payload), name="x").text
        assert text.startswith("x: ")
        assert len(text) > 4


# --- plans ----------------------------------------------------------------


def test_a_plan_is_announced_by_its_first_heading():
    plan = "Some preamble\n\n## Migrar el índice\n\n1. Crear la tabla\n"

    result = announce.build(item("menu", tool_input={"plan": plan}), name="indice")

    assert result.text == "indice: pide aprobar un plan: Migrar el índice."


@pytest.mark.parametrize(
    "markdown, expected",
    [
        ("# Título\ntexto", "Título"),
        ("### Tercer nivel ###", "Tercer nivel"),
        ("   ## Indentado", "Indentado"),
        ("sin heading\nsegunda línea", "sin heading"),
        ("", ""),
        ("\n\n\n", ""),
        ("#nospace\n## con espacio", "con espacio"),
    ],
)
def test_first_heading_extraction(markdown, expected):
    assert announce.first_heading(markdown) == expected


def test_a_plan_without_any_text_still_announces():
    result = announce.build(item("menu", tool_input={"plan": "   "}), name="x")

    assert "te está preguntando algo" in result.text


# --- notifications and milestones ----------------------------------------


def test_a_notification_reads_its_message():
    result = announce.build(
        item("notification", message="Claude needs your permission to use Bash"),
        name="workspace 21",
        blocking_chime="Ping",
    )

    assert result.text == "workspace 21: Claude needs your permission to use Bash."
    assert result.speak is True
    assert result.chime == "Ping"


def test_notifications_can_be_downgraded_to_a_chime():
    result = announce.build(
        item("notification", message="algo"),
        name="x",
        notification_events=False,
        blocking_chime="Ping",
    )

    assert result.speak is False
    assert result.silent is True
    assert result.chime == "Ping"


def test_a_long_notification_is_clipped():
    result = announce.build(item("notification", message="palabra " * 100), name="x")

    assert len(result.text) < 200


def test_a_milestone_only_chimes():
    result = announce.build(
        item("milestone", label="PR created"), name="x", milestone_chime="Glass"
    )

    assert result.speak is False
    assert result.silent is True
    assert result.chime == "Glass"
    assert result.text == "PR created"


# --- phonetics and hyphens ------------------------------------------------


def test_the_longest_matching_phonetic_entry_wins():
    assert announce.apply_phonetic("abrí el pull request", PHONETIC) == "abrí el pul rikuest"


def test_phonetic_matching_ignores_case():
    assert announce.apply_phonetic("Merge y DEPLOY", PHONETIC) == "merch y diploi"


def test_phonetics_do_not_fire_inside_a_longer_word():
    assert announce.apply_phonetic("mergealo y prometeo", PHONETIC) == "mergealo y prometeo"


def test_a_phonetic_key_with_a_hyphen_matches_before_hyphens_are_stripped():
    assert announce.speakable("corré pre-commit", {"pre-commit": "pri comit"}) == "corré pri comit"


def test_hyphens_inside_names_become_spaces():
    assert announce.despine("draft-mode-changes") == "draft mode changes"


def test_hyphens_are_stripped_in_the_announced_name():
    result = announce.build(item(), name="draft-mode-changes", summary="listo", remaining=0)

    assert result.text == "draft mode changes: listo."


def test_an_empty_phonetic_dictionary_changes_nothing():
    for mapping in (None, {}, "not a dict"):
        assert announce.apply_phonetic("pull request", mapping) == "pull request"


def test_phonetic_keys_are_regex_escaped():
    assert announce.apply_phonetic("a.b", {"a.b": "ok"}) == "ok"
    assert announce.apply_phonetic("axb", {"a.b": "ok"}) == "axb"


def test_build_phonetic_orders_longest_first():
    ordered = announce.build_phonetic({"pr": "pi ar", "pull request": "pul rikuest"})

    assert [key for key, _ in ordered] == ["pull request", "pr"]


def test_speakable_collapses_newlines_and_whitespace():
    assert announce.speakable("una\nfrase   larga\t") == "una frase larga"


def test_the_summary_goes_through_the_phonetic_dictionary():
    result = announce.build(
        item(), name="x", summary="quiere hacer merge", phonetic=PHONETIC, remaining=0
    )

    assert result.text == "x: quiere hacer merch."


def test_a_trailing_period_in_the_summary_is_not_doubled():
    result = announce.build(item(), name="x", summary="listo.", remaining=2)

    assert result.text == "x: listo. Quedan 2."
