"""Small asyncio client: used by the tests, the health check and the demo bots."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from .protocol import MAX_FRAME_FROM_SERVER, PROTOCOL_VERSION, encode, read_frame


class Client:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, auto_ack: bool = True) -> None:
        self.reader, self.writer, self.auto_ack = reader, writer, auto_ack
        self.id: int | None = None
        self.token: str | None = None

    @classmethod
    async def open(cls, host: str, port: int, auto_ack: bool = True) -> "Client":
        reader, writer = await asyncio.open_connection(host, port, limit=MAX_FRAME_FROM_SERVER)
        return cls(reader, writer, auto_ack)

    def send(self, msg: dict[str, Any]) -> None:
        self.writer.write(encode(msg))

    async def recv(self, timeout: float = 2.0) -> dict[str, Any]:
        msg = await asyncio.wait_for(read_frame(self.reader, MAX_FRAME_FROM_SERVER), timeout)
        if self.auto_ack and msg["t"] == "keepalive":
            self.send({"t": "keepalive", "id": msg["id"]})
        return msg

    async def recv_type(self, *types: str, timeout: float = 2.0) -> dict[str, Any]:
        """Next packet of one of ``types``; everything else is skipped."""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"no {types} packet within {timeout}s")
            msg = await self.recv(left)
            if msg["t"] in types:
                return msg

    async def login(
        self, name: str, token: str | None = None, password: str | None = None, proto: int = PROTOCOL_VERSION
    ) -> dict[str, Any]:
        """Hello + login. Returns the ``login_ok`` or ``kick`` packet."""
        self.send({"t": "hello", "proto": proto, "mod": "test-client", "intent": "login"})
        login: dict[str, Any] = {"t": "login", "name": name}
        if token is not None:
            login["token"] = token
        if password is not None:
            login["password"] = password
        self.send(login)
        msg = await self.recv_type("login_ok", "kick")
        if msg["t"] == "login_ok":
            self.id = msg["id"]
            self.token = msg.get("token", token)
        return msg

    async def upload_save(self, slot: str, data: bytes, base: int = 0, note: str | None = None, chunk: int = 128 * 1024) -> dict[str, Any]:
        """Upload a savegame in chunks; returns the ``save_stored`` or ``save_reject`` packet."""
        import base64
        import hashlib

        self.send({"t": "save_put", "slot": slot, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                   "base": base, **({"note": note} if note else {})})
        first = await self.recv_type("save_put_ok", "save_reject")
        if first["t"] == "save_reject":
            return first
        for n, start in enumerate(range(0, len(data), chunk)):
            self.send({"t": "save_data", "slot": slot, "n": n,
                       "data": base64.b64encode(data[start:start + chunk]).decode("ascii")})
        self.send({"t": "save_end", "slot": slot})
        return await self.recv_type("save_stored", "save_reject", timeout=10)

    async def download_save(self, slot: str, version: int | None = None) -> tuple[dict[str, Any], bytes]:
        """Download a savegame; returns (``save_info`` packet, bytes). Raises if the server refuses."""
        import base64

        self.send({"t": "save_get", "slot": slot, **({"version": version} if version else {})})
        info = await self.recv_type("save_info", "save_reject")
        if info["t"] == "save_reject":
            raise LookupError(info["reason"])
        parts: list[bytes] = []
        while True:
            msg = await self.recv_type("save_data", "save_done", timeout=10)
            if msg["t"] == "save_done":
                return info, b"".join(parts)
            parts.append(base64.b64decode(msg["data"]))

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass


async def status(host: str, port: int, timeout: float = 3.0) -> dict[str, Any]:
    """The server-list ping: one hello, one status packet, done."""
    client = await asyncio.wait_for(Client.open(host, port), timeout)
    try:
        client.send({"t": "hello", "proto": PROTOCOL_VERSION, "mod": "status", "intent": "status"})
        return await client.recv_type("status", timeout=timeout)
    finally:
        await client.close()
