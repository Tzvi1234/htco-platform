"""The worker's health port (ER-OPS-01): `GET /healthz` answers 200 while the loop is alive.

A few lines of HTTP over a TCP listener, so the worker needs no web framework. It reads one
request line, ignores the rest and closes the connection; anything but `GET /healthz` is 404.
"""

from collections.abc import Callable

import anyio
from anyio.abc import SocketStream

_MAX_REQUEST_LINE = 1024


def _response(status: str, body: str) -> bytes:
    payload = body.encode("ascii")
    return (
        f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nContent-Length: {len(payload)}\r\n"
        "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
    ).encode("ascii") + payload


def health_answer(request_line: bytes, *, alive: bool) -> bytes:
    parts = request_line.split(b" ")
    if len(parts) < 2 or parts[0] != b"GET" or parts[1] != b"/healthz":  # noqa: PLR2004
        return _response("404 Not Found", "not found\n")
    if alive:
        return _response("200 OK", "ok\n")
    return _response("503 Service Unavailable", "stalled\n")


async def serve_health(port: int, *, alive: Callable[[], bool], host: str = "0.0.0.0") -> None:  # noqa: S104 - the platform's health checker connects from outside the container
    listener = await anyio.create_tcp_listener(local_host=host, local_port=port)

    async def answer(stream: SocketStream) -> None:
        async with stream:
            with anyio.move_on_after(2):
                line = b""
                try:
                    while b"\r\n" not in line and len(line) < _MAX_REQUEST_LINE:
                        line += await stream.receive(_MAX_REQUEST_LINE)
                    await stream.send(health_answer(line.split(b"\r\n")[0], alive=alive()))
                except anyio.EndOfStream, anyio.BrokenResourceError:
                    return

    await listener.serve(answer)
