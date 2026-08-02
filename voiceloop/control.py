"""The control socket — a unix socket speaking NDJSON.

Request:  {"cmd": "pendings", "args": {}}
Response: {"ok": true, "data": …}  |  {"ok": false, "error": "…"}

One line in, one line out, connection stays open for as many round trips as the
client wants. A malformed line or an unknown command answers an error and the
server keeps running — a typo in the CLI must never take the daemon down with
it.

Binding the socket doubles as the single-instance lock: if something already
answers on it, this process is not the daemon.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path
from typing import Any, Awaitable, Callable

Dispatch = Callable[[str, dict], Awaitable[Any]]

MAX_LINE_BYTES = 1 << 20
DEFAULT_TIMEOUT = 5.0


class DaemonAlreadyRunning(RuntimeError):
    pass


class ControlError(RuntimeError):
    """The daemon answered, and the answer was an error."""


def is_running(socket_path: Path | str, timeout: float = 0.5) -> bool:
    """True when something is listening on the socket right now."""
    path = str(socket_path)
    if not os.path.exists(path):
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


def clear_stale(socket_path: Path | str) -> None:
    """Remove a socket file left behind by a crash. Refuses if a daemon is live."""
    path = Path(socket_path)
    if not path.exists():
        return
    if is_running(path):
        raise DaemonAlreadyRunning(f"a daemon is already listening on {path}")
    try:
        path.unlink()
    except OSError:
        pass


class ControlServer:
    def __init__(self, socket_path: Path | str, dispatch: Dispatch):
        self.socket_path = Path(socket_path)
        self._dispatch = dispatch
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        clear_stale(self.socket_path)
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self.socket_path)
        )
        os.chmod(self.socket_path, 0o600)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
            self._server = None
        try:
            self.socket_path.unlink()
        except OSError:
            pass

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    await self._write(writer, {"ok": False, "error": "line too long"})
                    break
                if not line:
                    break
                response = await self._respond(line)
                await self._write(writer, response)
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass

    async def _respond(self, line: bytes) -> dict:
        if len(line) > MAX_LINE_BYTES:
            return {"ok": False, "error": "request too large"}
        try:
            request = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return {"ok": False, "error": f"invalid json: {exc}"}
        if not isinstance(request, dict):
            return {"ok": False, "error": "expected a json object"}
        cmd = request.get("cmd")
        if not isinstance(cmd, str) or not cmd:
            return {"ok": False, "error": "missing cmd"}
        args = request.get("args")
        args = args if isinstance(args, dict) else {}
        try:
            data = await self._dispatch(cmd, args)
        except ControlError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - one bad command must not kill the server
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "data": data}

    @staticmethod
    async def _write(writer: asyncio.StreamWriter, payload: dict) -> None:
        writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()


def request(
    socket_path: Path | str,
    cmd: str,
    args: dict | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Blocking one-shot client, used by `voice-loopctl`."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(socket_path))
        payload = json.dumps({"cmd": cmd, "args": args or {}}, ensure_ascii=False)
        client.sendall((payload + "\n").encode("utf-8"))
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
    finally:
        client.close()
    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise ControlError("no response from daemon")
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ControlError(f"invalid response: {exc}") from exc
