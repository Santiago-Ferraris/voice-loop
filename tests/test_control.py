from __future__ import annotations

import asyncio
import json
import os
import socket

import pytest

from voiceloop.control import (
    ControlError,
    ControlServer,
    DaemonAlreadyRunning,
    clear_stale,
    is_running,
    request,
)


class Handlers:
    def __init__(self):
        self.seen: list[tuple[str, dict]] = []

    async def __call__(self, cmd: str, args: dict):
        self.seen.append((cmd, args))
        if cmd == "status":
            return {"paused": False, "queued": 2}
        if cmd == "echo":
            return args
        if cmd == "mic-toggle":
            raise ControlError("not implemented: mic-toggle lands in phase 2")
        if cmd == "boom":
            raise ZeroDivisionError("division by zero")
        raise ControlError(f"unknown command: {cmd}")


async def serve(socket_path, body):
    handlers = Handlers()
    server = ControlServer(socket_path, handlers)
    await server.start()
    try:
        return await asyncio.to_thread(body), handlers
    finally:
        await server.close()


def run(socket_path, body):
    return asyncio.run(serve(socket_path, body))


def test_a_command_round_trips(sock_path):
    path = sock_path

    (response, _), handlers = run(path, lambda: (request(path, "status"), None))

    assert response == {"ok": True, "data": {"paused": False, "queued": 2}}
    assert handlers.seen == [("status", {})]


def test_arguments_reach_the_handler(sock_path):
    path = sock_path

    (response, _), handlers = run(
        path, lambda: (request(path, "echo", {"label": "CI green"}), None)
    )

    assert response["data"] == {"label": "CI green"}
    assert handlers.seen == [("echo", {"label": "CI green"})]


def test_an_unknown_command_is_an_error_not_a_crash(sock_path):
    path = sock_path

    def body():
        first = request(path, "nope")
        second = request(path, "status")  # the server is still there
        return first, second

    (first, second), _ = run(path, body)

    assert first == {"ok": False, "error": "unknown command: nope"}
    assert second["ok"] is True


def test_a_handler_exception_is_reported_and_survivable(sock_path):
    path = sock_path

    def body():
        return request(path, "boom"), request(path, "status")

    (first, second), _ = run(path, body)

    assert first["ok"] is False
    assert "ZeroDivisionError" in first["error"]
    assert second["ok"] is True


def test_the_frozen_phase_two_commands_answer_not_implemented(sock_path):
    path = sock_path

    (response, _), _ = run(path, lambda: (request(path, "mic-toggle"), None))

    assert response == {"ok": False, "error": "not implemented: mic-toggle lands in phase 2"}


def test_malformed_json_does_not_take_the_server_down(sock_path):
    path = sock_path

    def body():
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5)
        client.connect(str(path))
        client.sendall(b"{not json\n")
        first = json.loads(client.recv(65536).decode().splitlines()[0])
        client.close()
        return first, request(path, "status")

    (first, second), _ = run(path, body)

    assert first["ok"] is False and "invalid json" in first["error"]
    assert second["ok"] is True


@pytest.mark.parametrize("payload", [b"[1,2,3]\n", b'"hello"\n', b"{}\n", b'{"cmd": 5}\n'])
def test_requests_that_are_not_commands_are_rejected(sock_path, payload):
    path = sock_path

    def body():
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5)
        client.connect(str(path))
        client.sendall(payload)
        answer = json.loads(client.recv(65536).decode().splitlines()[0])
        client.close()
        return answer, None

    (answer, _), _ = run(path, body)

    assert answer["ok"] is False


def test_one_connection_can_carry_several_requests(sock_path):
    path = sock_path

    def body():
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5)
        client.connect(str(path))
        for _ in range(3):
            client.sendall(b'{"cmd": "status"}\n')
        data = b""
        while data.count(b"\n") < 3:
            data += client.recv(65536)
        client.close()
        return data.decode().strip().splitlines(), None

    (lines, _), handlers = run(path, body)

    assert len(lines) == 3
    assert all(json.loads(line)["ok"] for line in lines)
    assert len(handlers.seen) == 3


def test_the_socket_file_is_removed_on_close(sock_path):
    path = sock_path

    run(path, lambda: (request(path, "status"), None))

    assert not path.exists()


def test_socket_permissions_are_owner_only(sock_path):
    path = sock_path
    modes = []

    async def body():
        server = ControlServer(path, Handlers())
        await server.start()
        modes.append(os.stat(path).st_mode & 0o777)
        await server.close()

    asyncio.run(body())

    assert modes == [0o600]


def test_is_running_detects_a_live_server(sock_path):
    path = sock_path
    seen = []

    async def body():
        server = ControlServer(path, Handlers())
        await server.start()
        seen.append(await asyncio.to_thread(is_running, path))
        await server.close()
        seen.append(await asyncio.to_thread(is_running, path))

    asyncio.run(body())

    assert seen == [True, False]


def test_is_running_is_false_for_a_missing_socket(tmp_path):
    assert is_running(tmp_path / "nope.sock") is False


def test_a_stale_socket_file_is_replaced(sock_path):
    path = sock_path
    path.write_text("left over from a crash", encoding="utf-8")

    clear_stale(path)

    assert not path.exists()


def test_a_second_daemon_refuses_to_start(sock_path):
    path = sock_path
    errors = []

    async def body():
        first = ControlServer(path, Handlers())
        await first.start()
        second = ControlServer(path, Handlers())
        try:
            await second.start()
        except DaemonAlreadyRunning as exc:
            errors.append(str(exc))
        await first.close()

    asyncio.run(body())

    assert errors and "already listening" in errors[0]


def test_the_client_reports_a_missing_daemon(tmp_path):
    with pytest.raises(OSError):
        request(tmp_path / "nope.sock", "status", timeout=0.5)
