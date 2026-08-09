from __future__ import annotations

import pytest

from voiceloop import announce, events
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


# --- the heads-up ---------------------------------------------------------


def test_the_heads_up_is_the_name_and_nothing_else():
    """Heard dozens of times a day. Everything past the name is paid for every time."""
    result = announce.alert(item(), name="inbox realtime", blocking_chime="Ping")

    assert result.text == "Nuevo evento de inbox realtime."
    assert result.chime == "Ping"
    assert result.speak is True


def test_the_heads_up_carries_no_summary_and_no_countdown():
    result = announce.alert(
        item("stop", message="lo que sea"), name="indice", blocking_chime="Ping"
    )

    assert result.text == "Nuevo evento de indice."
    assert "Queda" not in result.text
    assert "espera" not in result.text


def test_a_menu_gets_the_same_heads_up_as_anything_else():
    """The question is what you get for asking; being told there is one is free."""
    payload = {
        "tool_input": {
            "questions": [{"question": "¿Qué base uso?", "options": [{"label": "SQLite"}]}]
        }
    }

    result = announce.alert(item("menu", **payload), name="indice")

    assert result.text == "Nuevo evento de indice."
    assert "SQLite" not in result.text


def test_an_unnamed_window_is_announced_by_the_name_claude_gave_it():
    """`darwin-21` is a bad name, but it is the name; the better one is offered later."""
    result = announce.alert(item(), name="darwin-21")

    assert result.text == "Nuevo evento de darwin 21."


def test_the_heads_up_goes_through_the_phonetic_dictionary():
    result = announce.alert(item(), name="merge worker", phonetic=PHONETIC)

    assert result.text == "Nuevo evento de merch worker."


def test_a_nameless_item_still_says_something():
    assert announce.alert(item(), name="").text == "Nuevo evento."


# --- "quedan N" -----------------------------------------------------------


def test_nothing_left_says_nothing():
    assert announce.remaining_phrase(0) == ""
    assert announce.remaining_phrase(-1) == ""


def test_one_left_is_singular():
    assert announce.remaining_phrase(1) == "Queda uno"


@pytest.mark.parametrize("count, expected", [(2, "Quedan 2"), (7, "Quedan 7"), (15, "Quedan 15")])
def test_several_left_is_plural(count, expected):
    assert announce.remaining_phrase(count) == expected


def test_the_detail_is_what_that_window_wants_and_nothing_else():
    """No name in front: it was the whole of the heads-up two seconds ago."""
    assert announce.detail(item(), summary="terminó el backfill") == "terminó el backfill."


def test_the_countdown_is_never_part_of_the_announcement():
    """It belongs to the end of the cycle: you answer, *then* you hear what is left.

    In the announcement of a window being offered a name it landed between the
    summary and the question — "…Queda uno. ¿La llamo fecha actual?" — which is
    what made the user ask which one was left.
    """
    text = announce.detail(
        item(), summary="terminó el backfill", naming_offer="indice viejo"
    )

    assert text == "terminó el backfill. ¿La llamo indice viejo?"
    assert "Queda" not in text


def test_a_stop_without_a_summary_uses_the_fallback_phrase():
    assert announce.detail(item(), summary=None) == "terminó y te espera."


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

    text = announce.detail(item("menu", **payload))

    assert text == "¿Qué base uso? Opciones: uno: SQLite, dos: Postgres, tres: Ninguna."


@pytest.mark.parametrize(
    "label, spoken",
    [
        ("Probalo sin hotkeys primero (Recomendado)", "Probalo sin hotkeys primero"),
        ("Actualizar Command Line Tools", "Actualizar Command Line Tools"),
        ("Postgres — ya corre en staging", "Postgres"),
        ("Migrar [beta] ahora", "Migrar ahora"),
        ("Adaptar a Hammerspoon; es lo más rápido", "Adaptar a Hammerspoon"),
        ("Migrar la base de datos vieja a Postgres", "Migrar la base de datos"),
        ("(Recomendado)", "(Recomendado)"),
        ("", ""),
    ],
)
def test_a_label_is_shortened_before_it_is_spoken(label, spoken):
    assert announce.short_label(label) == spoken


@pytest.mark.parametrize(
    "label, spoken",
    [
        # One construction end to end: whole, or not at all.
        ("fn + M / fn + B", "fn + M / fn + B"),
        ("⌃⌘ + M / ⌃⌘ + B", "⌃⌘ + M / ⌃⌘ + B"),
        ("⌥ + M / ⌥ + B", "⌥ + M / ⌥ + B"),
        ("F6 / F7 directas", "F6 / F7 directas"),
        # Long enough to cut, so the cut retreats to before the construction.
        ("Usá el modo A / B con el flag nuevo", "Usá el modo"),
        ("Configurar el atajo Enter -> Escape ahora", "Configurar el atajo"),
        # The construction fits inside the cut: nothing to retreat from.
        ("Mandar Enter -> Escape al terminar la sesión", "Mandar Enter -> Escape"),
        # A separator glued to its words is one token, and cuts like one.
        ("A/B testing con feature flags nuevos", "A/B testing con feature flags"),
    ],
)
def test_a_label_is_never_cut_inside_an_expression(label, spoken):
    """`"fn + M / fn"` is not a shorter way of saying it — it is a wrong answer."""
    assert announce.short_label(label) == spoken


def test_the_hotkey_menu_that_lost_half_of_every_option():
    """The live one: every label cut just after the slash, all four indistinct."""
    labels = ["fn + M / fn + B", "⌃⌘ + M / ⌃⌘ + B", "⌥ + M / ⌥ + B", "F6 / F7 directas"]

    spoken = announce.enumerate_options(labels)

    assert spoken == (
        "Opciones: uno: fn + M / fn + B, dos: ⌃⌘ + M / ⌃⌘ + B, "
        "tres: ⌥ + M / ⌥ + B, cuatro: F6 / F7 directas"
    )


def test_four_options_are_read_short_not_whole():
    """The live case: twenty-five seconds of audio for a five-second decision."""
    labels = [
        "Probalo sin hotkeys primero (Recomendado)",
        "Actualizar Command Line Tools",
        "Adaptar a Hammerspoon",
        "Adaptar a Shortcuts de macOS",
    ]

    spoken = announce.enumerate_options(labels)

    assert spoken == (
        "Opciones: uno: Probalo sin hotkeys primero, dos: Actualizar Command Line Tools, "
        "tres: Adaptar a Hammerspoon, cuatro: Adaptar a Shortcuts de macOS"
    )
    assert "Recomendado" not in spoken


def test_option_numbers_beyond_ten_fall_back_to_digits():
    assert [announce.number_word(n) for n in (1, 5, 10)] == ["uno", "cinco", "diez"]
    assert announce.number_word(11) == "11"


def test_a_menu_without_options_still_reads_the_question():
    payload = {"tool_input": {"questions": [{"question": "¿Seguimos?"}]}}

    assert announce.detail(item("menu", **payload)) == "¿Seguimos?"


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

    text = announce.detail(item("menu", **payload))

    assert "Primera" in text
    assert "Segunda" not in text
    assert "Y 2 preguntas más" in text


def test_a_single_extra_question_is_singular():
    payload = {"tool_input": {"questions": [{"question": "Primera"}, {"question": "Segunda"}]}}

    assert "Y una pregunta más" in announce.detail(item("menu", **payload))


def test_long_option_labels_are_clipped():
    payload = {
        "tool_input": {
            "questions": [{"question": "¿Cuál?", "options": [{"label": "x" * 200}]}]
        }
    }

    text = announce.detail(item("menu", **payload))

    assert "…" in text
    assert len(text) < 200


def test_an_option_without_a_label_uses_its_description():
    payload = {
        "tool_input": {
            "questions": [{"question": "¿Cuál?", "options": [{"description": "la segura"}]}]
        }
    }

    assert "uno: la segura" in announce.detail(item("menu", **payload))


def test_a_malformed_menu_payload_still_says_something():
    for payload in ({}, {"tool_input": {}}, {"tool_input": {"questions": []}},
                    {"tool_input": {"questions": "nope"}}):
        text = announce.detail(item("menu", **payload))
        assert "te está preguntando algo" in text


# --- plans ----------------------------------------------------------------


def test_a_plan_is_announced_by_its_first_heading():
    plan = "Some preamble\n\n## Migrar el índice\n\n1. Crear la tabla\n"

    text = announce.detail(item("menu", tool_input={"plan": plan}))

    assert text.startswith("pide aprobar un plan: Migrar el índice.")


def test_a_plan_reads_out_the_options_it_can_be_answered_with():
    """The plan menu's rows are Claude's, not the payload's — say them anyway."""
    text = announce.detail(item("menu", tool_input={"plan": "## Migrar"}))

    assert "Opciones: uno: aprobar y seguir en auto" in text
    assert "dos: aprobar revisando cada edición" in text


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
    assert "te está preguntando algo" in announce.detail(item("menu", tool_input={"plan": "   "}))


# --- notifications and milestones ----------------------------------------


def test_a_notification_reads_its_message():
    said = item("notification", message="Claude needs your permission to use Bash")

    assert announce.detail(said) == "Claude needs your permission to use Bash."
    assert announce.alert(said, name="workspace 21", blocking_chime="Ping").speak is True


def test_muting_an_idle_nudge_takes_the_chime_with_it():
    """Chime-only was the same interruption without the part that justified it."""
    result = announce.alert(
        item("notification", message="Claude is waiting for your input"),
        name="x",
        notification_events=False,
        blocking_chime="Ping",
    )

    assert result.speak is False
    assert result.silent is True
    assert result.chime is None


def test_muting_the_nudges_does_not_mute_a_permission_prompt():
    """The two arrive through the same hook and are not the same thing."""
    result = announce.alert(
        item("notification", message="Claude needs your permission to use Bash"),
        name="x",
        notification_events=False,
        blocking_chime="Ping",
    )

    assert result.speak is True
    assert result.chime == "Ping"


def test_a_notification_nobody_recognises_is_treated_as_a_block():
    result = announce.alert(
        item("notification", message="Claude tripped over something new"),
        name="x",
        notification_events=False,
        blocking_chime="Ping",
    )

    assert result.speak is True
    assert result.chime == "Ping"


@pytest.mark.parametrize(
    "message, idle",
    [
        ("Claude is waiting for your input", True),
        ("Claude is waiting for your input.", True),
        ("CLAUDE IS WAITING FOR YOUR INPUT", True),
        ("Claude needs your permission to use Bash", False),
        ("", False),
        (None, False),
        (42, False),
    ],
)
def test_only_the_idle_wording_reads_as_a_nudge(message, idle):
    assert events.is_idle_notification(message) is idle


def test_a_long_notification_is_clipped():
    assert len(announce.detail(item("notification", message="palabra " * 100))) < 200


def test_a_milestone_only_chimes():
    result = announce.alert(
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
    result = announce.alert(item(), name="draft-mode-changes")

    assert result.text == "Nuevo evento de draft mode changes."


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
    text = announce.detail(item(), summary="quiere hacer merge", phonetic=PHONETIC)

    assert text == "quiere hacer merch."


def test_a_trailing_period_in_the_summary_is_not_doubled():
    text = announce.detail(item(), summary="listo.", naming_offer="el del backfill")

    assert text == "listo. ¿La llamo el del backfill?"


# --- the queue, read out loud ----------------------------------------------


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0, "recién"),
        (59, "recién"),
        (60, "hace un minuto"),
        (600, "hace diez minutos"),
        (1500, "hace 25 minutos"),
        (3600, "hace una hora"),
        (7200, "hace dos horas"),
        (86400, "hace un día"),
        (172800, "hace dos días"),
        (-5, "recién"),
    ],
)
def test_how_long_a_window_has_been_waiting(seconds, expected):
    assert announce.ago_phrase(seconds) == expected


def test_the_pendings_list_is_numbered_because_the_number_is_the_answer():
    text = announce.describe_pendings(
        [("alpha", "espera tu aprobación", "hace diez minutos"), ("beta", "", "recién")]
    )

    assert text == (
        "Tenés dos pendientes. uno: alpha, espera tu aprobación, hace diez minutos. "
        "dos: beta, recién"
    )


def test_an_empty_pendings_list_is_one_sentence():
    assert announce.describe_pendings([]) == "No tenés nada pendiente."


def test_a_long_pendings_list_is_capped_with_a_tail():
    entries = [(f"win-{i}", "", "recién") for i in range(8)]

    text = announce.describe_pendings(entries, limit=5)

    assert "Tenés ocho pendientes" in text
    assert "win-5" not in text
    assert text.endswith("Y tres más")


def test_status_reads_open_working_and_waiting():
    text = announce.describe_status(windows=3, working=2, waiting=1)

    assert text == "Hay tres ventanas abiertas. dos trabajando. una te espera"


def test_status_with_nothing_open_still_says_something():
    text = announce.describe_status(windows=0, working=0, waiting=0)

    assert text == "No hay ventanas abiertas. ninguna trabajando. ninguna te espera"


def test_status_adds_milestones_and_the_mode():
    text = announce.describe_status(
        windows=1, working=0, waiting=1, milestones=[("CI green", 2)], paused=True
    )

    assert "dos con CI green" in text
    assert text.endswith("Estoy en pausa")


def test_pause_wins_over_busy_because_nothing_is_being_announced():
    text = announce.describe_status(windows=0, working=0, waiting=0, paused=True, busy=True)

    assert "modo ocupado" not in text


# --- the naming offer ------------------------------------------------------


def test_the_naming_offer_is_the_last_thing_asked():
    text = announce.detail(item(), summary="terminó los tests", naming_offer="tests worker")

    assert text == "terminó los tests. ¿La llamo tests worker?"


def test_no_offer_leaves_the_announcement_exactly_as_it_was():
    assert announce.detail(item(), summary="listo", naming_offer="") == "listo."


def test_the_offered_name_goes_through_the_phonetic_dictionary():
    text = announce.detail(
        item(), summary="listo", naming_offer="merge worker", phonetic=PHONETIC
    )

    assert text.endswith("¿La llamo merch worker?")


# --- how much piled up while you were busy ---------------------------------


@pytest.mark.parametrize(
    "count, expected",
    [(0, ""), (1, "Tenés un pendiente"), (3, "Tenés tres pendientes"), (12, "Tenés 12 pendientes")],
)
def test_the_count_on_its_own_is_what_busy_mode_owes_you(count, expected):
    assert announce.pendings_count(count) == expected
