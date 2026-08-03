"""A stand-in daemon for the install tests: binds the control socket, answers
`status` with its own pid, and exits on `restart` the way the real one does.

Prints its pid on stdout once the socket is bound, so a caller can tell the
difference between "starting" and "up" without polling for a file.
"""

from __future__ import annotations

import asyncio
import os
import sys

from voiceloop.control import ControlServer


async def dispatch(cmd: str, args: dict):
    if cmd == "status":
        return {"pid": os.getpid(), "version": "test"}
    if cmd == "restart":
        asyncio.get_running_loop().call_later(0.05, lambda: os._exit(0))
        return {"restarting": True}
    return {}


async def serve(socket_path: str) -> None:
    server = ControlServer(socket_path, dispatch)
    await server.start()
    print(os.getpid(), flush=True)
    await asyncio.sleep(120)


if __name__ == "__main__":
    asyncio.run(serve(sys.argv[1]))
