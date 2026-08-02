from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from voiceloop import spool
from voiceloop.events import Event


def make(index: int, event_type: str = "stop") -> Event:
    return Event.new(event_type, f"session-{index}", tty=f"/dev/ttys{index:03d}")


def test_write_then_read_round_trip(tmp_path):
    event = make(1, "menu")

    path = spool.write(tmp_path, event)

    assert path.exists()
    (read_path, read_event), = spool.read_pending(tmp_path)
    assert read_path == path
    assert read_event == event


def test_reader_ignores_partially_written_files(tmp_path):
    """The writer's temp file is invisible to the glob until the rename lands."""
    spool.ensure_dirs(tmp_path)
    (tmp_path / ".0000000000000000001-1-abcd.json.tmp").write_text('{"v": 1, "id"')

    assert spool.read_pending(tmp_path) == []
    assert spool.list_files(tmp_path) == []


def test_events_come_back_in_arrival_order(tmp_path):
    written = []
    for index in range(6):
        written.append(spool.write(tmp_path, make(index)))
        time.sleep(0.001)

    read = [event.session_id for _, event in spool.read_pending(tmp_path)]

    assert read == [f"session-{index}" for index in range(6)]
    assert [path.name for path in written] == [
        path.name for path, _ in spool.read_pending(tmp_path)
    ]


def test_ordering_is_numeric_not_lexicographic(tmp_path):
    spool.ensure_dirs(tmp_path)
    for stamp in ("0000000000000000002", "0000000000000000010"):
        payload = Event.new("stop", f"s{stamp}").to_dict()
        (tmp_path / f"{stamp}-1-aaaa.json").write_text(json.dumps(payload))

    order = [path.name.split("-")[0] for path, _ in spool.read_pending(tmp_path)]

    assert order == ["0000000000000000002", "0000000000000000010"]


def test_malformed_json_is_quarantined_and_not_re_read(tmp_path):
    spool.ensure_dirs(tmp_path)
    bad = tmp_path / "0000000000000000001-1-dead.json"
    bad.write_text("{not json", encoding="utf-8")
    spool.write(tmp_path, make(2))

    events = spool.read_pending(tmp_path)

    assert [event.session_id for _, event in events] == ["session-2"]
    assert not bad.exists()
    assert (tmp_path / spool.BAD_DIRNAME / bad.name).exists()
    assert spool.read_pending(tmp_path)  # the good one is still there
    assert len(spool.read_pending(tmp_path)) == 1


def test_schema_violations_are_quarantined_with_a_reason(tmp_path):
    spool.ensure_dirs(tmp_path)
    bad = tmp_path / "0000000000000000001-1-beef.json"
    bad.write_text(json.dumps({"v": 99, "id": "x", "ts": 1, "type": "stop"}))

    assert spool.read_pending(tmp_path) == []
    why = tmp_path / spool.BAD_DIRNAME / f"{bad.name}.why"
    assert "schema" in why.read_text()


def test_quarantine_does_not_clobber_a_same_named_file(tmp_path):
    spool.ensure_dirs(tmp_path)
    for _ in range(2):
        bad = tmp_path / "0000000000000000001-1-dead.json"
        bad.write_text("{nope", encoding="utf-8")
        spool.read_pending(tmp_path)

    quarantined = list((tmp_path / spool.BAD_DIRNAME).glob("*.json"))
    assert len(quarantined) == 2


def test_quarantine_directory_is_not_scanned_as_spool(tmp_path):
    spool.ensure_dirs(tmp_path)
    (tmp_path / spool.BAD_DIRNAME / "0000000000000000001-1-aaaa.json").write_text("{")

    assert spool.list_files(tmp_path) == []


def test_discard_removes_only_what_it_is_given(tmp_path):
    first = spool.write(tmp_path, make(1))
    spool.write(tmp_path, make(2))

    spool.discard([first])

    assert [event.session_id for _, event in spool.read_pending(tmp_path)] == ["session-2"]


def test_discarding_a_vanished_file_is_not_an_error(tmp_path):
    spool.discard([tmp_path / "gone.json"])


def test_reading_a_missing_directory_is_empty(tmp_path):
    assert spool.list_files(tmp_path / "nope") == []
    assert spool.read_pending(tmp_path / "nope") == []


def test_concurrent_writers_do_not_lose_or_corrupt_events(tmp_path):
    """Fifteen sessions can fire hooks at the same instant."""
    total = 90
    spool.ensure_dirs(tmp_path)

    with ThreadPoolExecutor(max_workers=15) as pool:
        list(pool.map(lambda index: spool.write(tmp_path, make(index)), range(total)))

    events = spool.read_pending(tmp_path)

    assert len(events) == total
    assert {event.session_id for _, event in events} == {
        f"session-{index}" for index in range(total)
    }
    assert not list((tmp_path / spool.BAD_DIRNAME).glob("*"))


def test_written_file_is_valid_json_on_disk(tmp_path):
    path = spool.write(tmp_path, make(3, "notification"))

    raw = json.loads(path.read_text(encoding="utf-8"))

    assert raw["v"] == 1
    assert raw["type"] == "notification"
    assert set(raw) == {
        "v",
        "id",
        "ts",
        "type",
        "session_id",
        "tty",
        "cwd",
        "transcript_path",
        "payload",
    }


def test_filenames_carry_the_writing_pid(tmp_path):
    path = spool.write(tmp_path, make(1))

    assert path.name.split("-")[1] == str(os.getpid())
