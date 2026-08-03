from __future__ import annotations

import pytest

from voiceloop import config as config_mod
from voiceloop.config import ConfigError

from conftest import REPO_ROOT


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def fake_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    write(
        root / "config.example.yml",
        """
paths:
  state_dir: ~/.local/state/voice-loop
speech_to_text:
  provider: deepgram
  deepgram:
    model: nova-3
    language: multi
summaries:
  provider: openai
  model: gpt-4o-mini
  max_words: 12
text_to_speech:
  voice: Paulina
  rate: 190
  phonetic:
    merge: merch
keyterms:
  - alpha
  - beta
""",
    )
    return root


def test_defaults_load_without_a_local_file(fake_repo):
    config = config_mod.load(repo_root=fake_repo)

    assert config.get("speech_to_text.provider") == "deepgram"
    assert config.get("summaries.max_words") == 12
    assert config.source_paths == (fake_repo / "config.example.yml",)


def test_local_file_deep_merges_without_dropping_siblings(fake_repo):
    write(
        fake_repo / "config.local.yml",
        "speech_to_text:\n  deepgram:\n    model: nova-2\nsummaries:\n  max_words: 6\n",
    )

    config = config_mod.load(repo_root=fake_repo)

    assert config.get("speech_to_text.deepgram.model") == "nova-2"
    # the sibling key inside the same nested dict survives the merge
    assert config.get("speech_to_text.deepgram.language") == "multi"
    assert config.get("speech_to_text.provider") == "deepgram"
    assert config.get("summaries.max_words") == 6
    assert config.get("summaries.model") == "gpt-4o-mini"


def test_lists_are_replaced_not_appended(fake_repo):
    write(fake_repo / "config.local.yml", "keyterms:\n  - gamma\n")

    config = config_mod.load(repo_root=fake_repo)

    assert config.get("keyterms") == ["gamma"]


def test_tilde_in_state_dir_is_expanded(fake_repo, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    config = config_mod.load(repo_root=fake_repo)

    assert str(config.get("paths.state_dir")).startswith(str(tmp_path / "home"))
    assert "~" not in str(config.state_dir)


def test_derived_paths_hang_off_state_dir(fake_repo, tmp_path):
    write(fake_repo / "config.local.yml", f"paths:\n  state_dir: {tmp_path / 'st'}\n")

    config = config_mod.load(repo_root=fake_repo)

    assert config.state_dir == tmp_path / "st"
    assert config.spool_dir == tmp_path / "st" / "spool"
    assert config.db_path == tmp_path / "st" / "queue.db"
    assert config.socket_path == tmp_path / "st" / "daemon.sock"
    assert config.log_dir == tmp_path / "st" / "logs"


@pytest.mark.parametrize(
    "snippet",
    [
        "speech_to_text:\n  deepgram:\n    api_key: sk-not-allowed\n",
        "summaries:\n  token: abc123\n",
        "openai:\n  secret: hunter2\n",
        "speech_to_text:\n  API-KEY: nope\n",
    ],
)
def test_a_key_shaped_entry_in_config_is_rejected(fake_repo, snippet):
    write(fake_repo / "config.local.yml", snippet)

    with pytest.raises(ConfigError, match="environment"):
        config_mod.load(repo_root=fake_repo)


def test_keyterms_named_like_a_secret_are_fine(fake_repo):
    # the guard looks at keys, not values — a vocabulary entry may say "token"
    write(fake_repo / "config.local.yml", "keyterms:\n  - token bucket\n")

    config = config_mod.load(repo_root=fake_repo)

    assert config.get("keyterms") == ["token bucket"]


def test_api_key_comes_from_the_environment_only(fake_repo, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)

    config = config_mod.load(repo_root=fake_repo)

    assert config.api_key("openai") == "sk-from-env"
    assert config.api_key("deepgram") is None
    assert config.api_key("unknown-provider") is None


def test_blank_env_var_counts_as_missing(fake_repo, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "   ")

    assert config_mod.load(repo_root=fake_repo).api_key("openai") is None


def test_unknown_stt_provider_is_rejected(fake_repo):
    write(fake_repo / "config.local.yml", "speech_to_text:\n  provider: telepathy\n")

    with pytest.raises(ConfigError, match="speech_to_text.provider"):
        config_mod.load(repo_root=fake_repo)


def test_whisper_cpp_is_an_accepted_provider(fake_repo):
    write(fake_repo / "config.local.yml", "speech_to_text:\n  provider: whisper-cpp\n")

    assert config_mod.load(repo_root=fake_repo).get("speech_to_text.provider") == "whisper-cpp"


def test_unknown_summary_provider_is_rejected(fake_repo):
    write(fake_repo / "config.local.yml", "summaries:\n  provider: oracle\n")

    with pytest.raises(ConfigError, match="summaries.provider"):
        config_mod.load(repo_root=fake_repo)


@pytest.mark.parametrize("value", ["many", 0, -3])
def test_max_words_must_be_a_positive_integer(fake_repo, value):
    write(fake_repo / "config.local.yml", f"summaries:\n  max_words: {value}\n")

    with pytest.raises(ConfigError, match="max_words"):
        config_mod.load(repo_root=fake_repo)


def test_timeout_must_be_positive(fake_repo):
    write(fake_repo / "config.local.yml", "summaries:\n  timeout_seconds: 0\n")

    with pytest.raises(ConfigError, match="timeout_seconds"):
        config_mod.load(repo_root=fake_repo)


def test_broken_yaml_names_the_file(fake_repo):
    write(fake_repo / "config.local.yml", "summaries:\n  - [unbalanced\n")

    with pytest.raises(ConfigError, match="config.local.yml"):
        config_mod.load(repo_root=fake_repo)


def test_missing_defaults_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="config.example.yml"):
        config_mod.load(repo_root=tmp_path)


def test_get_returns_the_default_for_a_missing_path(fake_repo):
    config = config_mod.load(repo_root=fake_repo)

    assert config.get("nope.not.here", "fallback") == "fallback"
    assert config.get("summaries.nope") is None


def test_shipped_example_config_is_valid_and_complete(repo_root, clean_env):
    """The committed defaults are what a fresh clone runs on."""
    config = config_mod.load(repo_root=repo_root, local_path=repo_root / "does-not-exist.yml")

    assert config.get("paths.state_dir")
    assert config.get("announce.notification_events") is True
    assert config.get("summaries.timeout_seconds") == 5
    assert config.get("integrations.milestone_file_watch.enabled") is False
    assert config.api_key("openai") is None


# --- phase 2 keys ----------------------------------------------------------


def test_the_streaming_and_mock_providers_are_accepted_names(tmp_path, clean_env):
    for provider in ("deepgram_ws", "mock"):
        local = tmp_path / f"{provider}.yml"
        local.write_text(f"speech_to_text:\n  provider: {provider}\n", encoding="utf-8")

        loaded = config_mod.load(repo_root=REPO_ROOT, local_path=local)

        assert loaded.get("speech_to_text.provider") == provider


def test_a_confidence_threshold_outside_zero_to_one_is_rejected(tmp_path, clean_env):
    local = tmp_path / "c.yml"
    local.write_text("delivery:\n  confirm_below_confidence: 42\n", encoding="utf-8")

    with pytest.raises(config_mod.ConfigError, match="between 0 and 1"):
        config_mod.load(repo_root=REPO_ROOT, local_path=local)


@pytest.mark.parametrize("key", ["max_seconds", "open_timeout_seconds"])
def test_a_non_positive_microphone_duration_is_rejected(tmp_path, clean_env, key):
    local = tmp_path / "m.yml"
    local.write_text(f"microphone:\n  {key}: 0\n", encoding="utf-8")

    with pytest.raises(config_mod.ConfigError, match=f"microphone.{key}"):
        config_mod.load(repo_root=REPO_ROOT, local_path=local)


def test_the_shipped_defaults_cover_every_phase_two_key(config):
    assert config.get("microphone.device") == ":0"
    assert config.get("microphone.silence.enabled") is True
    assert config.get("announce.mic_open_chime")
    assert config.get("announce.mic_close_chime")
    assert config.get("delivery.max_mic_rounds") == 3
    assert config.get("delivery.plan_menu.feedback_index") == 4
