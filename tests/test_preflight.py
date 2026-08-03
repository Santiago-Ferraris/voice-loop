"""The permission checks, with the two OS calls faked.

What matters here is that a denial is *reported*, not swallowed: a daemon that
cannot open the mic or drive iTerm2 looks exactly like a daemon that is working
until you ask it.
"""

from __future__ import annotations

import subprocess

import pytest

from voiceloop import preflight
from voiceloop.audio import MIN_USABLE_BYTES
from voiceloop.preflight import FAILED, OK, SKIPPED, Check
from voiceloop.stt.mock import MockStt

DENIED = (
    "execution error: Not authorized to send Apple events to iTerm2. (-1743)"
)


class FakeOsascript:
    def __init__(self, stdout: str = "2", returncode: int = 0, stderr: str = ""):
        self.result = subprocess.CompletedProcess([], returncode, stdout, stderr)

    def __call__(self, argv):
        return self.result


def fake_capture(monkeypatch, *, bytes_written: int, stderr: str = ""):
    def run(argv, **kwargs):
        target = argv[-1]
        if bytes_written:
            with open(target, "wb") as handle:
                handle.write(b"\0" * bytes_written)
        return subprocess.CompletedProcess(argv, 0 if bytes_written else 1, "", stderr)

    monkeypatch.setattr(preflight.subprocess, "run", run)
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/opt/homebrew/bin/ffmpeg")


# --- ffmpeg ----------------------------------------------------------------


def test_a_missing_ffmpeg_says_how_to_get_it(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)

    check = preflight.check_ffmpeg()

    assert check.status == FAILED
    assert "brew install ffmpeg" in check.detail


def test_ffmpeg_on_the_path_reports_where(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/local/bin/ffmpeg")

    assert preflight.check_ffmpeg() == Check("ffmpeg", OK, "/usr/local/bin/ffmpeg")


# --- the microphone --------------------------------------------------------


def test_a_capture_with_audio_in_it_passes(monkeypatch):
    fake_capture(monkeypatch, bytes_written=MIN_USABLE_BYTES + 1)

    assert preflight.check_microphone().status == OK


def test_a_denied_microphone_fails_with_what_ffmpeg_said(monkeypatch):
    fake_capture(monkeypatch, bytes_written=0, stderr="Input/output error\nabort")

    check = preflight.check_microphone()

    assert check.status == FAILED
    assert check.detail == "abort"


def test_a_silent_capture_of_nothing_at_all_fails(monkeypatch):
    """A permission denial writes a header and no samples."""
    fake_capture(monkeypatch, bytes_written=44)

    assert preflight.check_microphone().status == FAILED


def test_the_microphone_check_is_skipped_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)

    assert preflight.check_microphone().status == SKIPPED


def test_the_configured_device_is_the_one_probed(monkeypatch):
    seen: list = []

    def run(argv, **kwargs):
        seen.append(argv)
        with open(argv[-1], "wb") as handle:
            handle.write(b"\0" * (MIN_USABLE_BYTES + 1))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(preflight.subprocess, "run", run)
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/bin/ffmpeg")

    preflight.check_microphone(device=":2")

    assert seen[0][seen[0].index("-i") + 1] == ":2"


def test_a_capture_that_hangs_names_the_consent_prompt(monkeypatch):
    """Issue #7: a one-second capture that takes twenty is not slow, it is blocked."""
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/bin/ffmpeg")

    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 21.0))

    monkeypatch.setattr(preflight.subprocess, "run", run)

    check = preflight.check_microphone()

    assert check.status == FAILED
    assert "consent" in check.detail
    assert "Privacy & Security" in check.detail


def test_an_ffmpeg_that_cannot_even_start_is_a_failed_check(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/bin/ffmpeg")

    def run(argv, **kwargs):
        raise OSError("no such thing")

    monkeypatch.setattr(preflight.subprocess, "run", run)

    assert preflight.check_microphone().status == FAILED


# --- automation ------------------------------------------------------------


def test_automation_denial_is_reported_verbatim():
    check = preflight.check_iterm(FakeOsascript(returncode=1, stderr=DENIED))

    assert check.status == FAILED
    assert "-1743" in check.detail


def test_automation_granted_reports_the_window_count():
    check = preflight.check_iterm(FakeOsascript("2"))

    assert check.status == OK
    assert "2" in check.detail


# --- the recognizer --------------------------------------------------------


def test_a_recognizer_without_a_key_is_a_failed_check():
    from voiceloop.stt.deepgram import DeepgramStt

    check = preflight.check_stt(DeepgramStt(api_key=None))

    assert check.status == FAILED
    assert "API key" in check.detail


def test_no_recognizer_at_all_is_a_failed_check():
    assert preflight.check_stt(None).status == FAILED


def test_a_missing_key_names_the_file_it_is_missing_from(tmp_path):
    """Issue #6: "missing from the environment" sent people to fix the wrong thing."""
    from voiceloop import envfile
    from voiceloop.stt.deepgram import DeepgramStt

    check = preflight.check_stt(
        DeepgramStt(api_key=None), env_file=envfile.read(tmp_path / "nope")
    )

    assert check.status == FAILED
    assert f"no env file at {tmp_path / 'nope'}" in check.detail


def test_a_key_absent_from_a_file_that_exists_says_so(tmp_path):
    from voiceloop import envfile
    from voiceloop.stt.deepgram import DeepgramStt

    path = tmp_path / "env"
    path.write_text("OPENAI_API_KEY=sk-1\n", encoding="utf-8")

    check = preflight.check_stt(DeepgramStt(api_key=None), env_file=envfile.read(path))

    assert f"DEEPGRAM_API_KEY is not in {path}" in check.detail


def test_a_configured_recognizer_passes():
    assert preflight.check_stt(MockStt()) == Check("speech-to-text", OK, "mock")


# --- all of them -----------------------------------------------------------


def test_running_everything_returns_one_check_per_thing(monkeypatch):
    fake_capture(monkeypatch, bytes_written=MIN_USABLE_BYTES + 1)

    checks = preflight.run_all(engine=MockStt(), runner=FakeOsascript("1"))

    assert [check.name for check in checks] == [
        "ffmpeg",
        "microphone",
        "iterm automation",
        "speech-to-text",
    ]
    assert all(check.ok for check in checks)


def test_a_check_serializes_for_the_control_socket():
    assert Check("mic", OK, "fine").as_dict() == {
        "name": "mic",
        "status": "ok",
        "detail": "fine",
    }


@pytest.mark.parametrize("status", [FAILED, SKIPPED])
def test_only_ok_counts_as_ok(status):
    assert Check("x", status).ok is False
