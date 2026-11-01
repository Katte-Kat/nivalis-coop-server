"""The server: accepts connections, runs the tick loop, autosaves the world."""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections import deque
from typing import Any

from . import __version__
from .admin import AdminSocket, Commands
from .authority import Authority
from .config import Config
from .clock import WorldClocks
from .dialogue import DialogueVotes
from .saves import SaveService
from .protocol import PROTOCOL_VERSION, encode
from .session import Session
from .store import Store
from .world import World

log = logging.getLogger("nivalis.server")


class CoopServer:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        os.makedirs(cfg.data_dir, exist_ok=True)
        self.store = Store(cfg.db_path)
        self.world = World(self.store, cfg.max_state_keys, cfg.max_state_value_bytes, cfg.max_state_total_bytes)
        saved = self.store.meta("whitelist")
        self.whitelist_on = cfg.whitelist if saved is None else saved == "1"
        self.sessions: dict[int, Session] = {}
        self.by_name: dict[str, Session] = {}
        self.saves = SaveService(self)
        self.dialogues = DialogueVotes(self)
        self.clocks = WorldClocks(self)
        self.authority = Authority(self)
        self.commands = Commands(self)
        self.admin = AdminSocket(cfg.admin_path, self.commands)
        self.stop_event = asyncio.Event()
        self.tick_n = 0
        self.port = cfg.port
        self._started = time.monotonic()
        self._next_id = 1
        self._intervals: deque[float] = deque(maxlen=100)
        self._durations: deque[float] = deque(maxlen=100)
        self._connections: set[Session] = set()
        self._tasks: list[asyncio.Task] = []
        self._server: asyncio.Server | None = None

    # lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_connect, self.cfg.host, self.cfg.port, backlog=64)
        self.port = self._server.sockets[0].getsockname()[1]
        self._started = time.monotonic()
        self._tasks = [asyncio.create_task(self._tick_loop()), asyncio.create_task(self._autosave_loop())]
        await self.admin.start()
        log.info(
            "%r listening on %s:%s (%d ticks/s, max %d players, %d world keys, rev %d)",
            self.cfg.name, self.cfg.host, self.port, self.cfg.tick_rate, self.cfg.max_players,
            len(self.world.state), self.world.rev,
        )

    async def stop(self) -> None:
        log.info("shutting down")
        if self._server:
            self._server.close()
        for session in list(self._connections):
            session.kick("Server closing")
        for task in self._tasks:
            task.cancel()
        await self.admin.stop()
        if self._connections:
            await asyncio.sleep(0.3)  # let the kick packets leave
        for session in list(self._connections):
            # a peer that does not hang up (half-open connection, e.g. a killed game behind an SSH tunnel)
            # must not hold the shutdown back: podman gives us 10 s before SIGKILL
            session.writer.transport.abort()
        if self._server:
            try:
                await asyncio.wait_for(self._server.wait_closed(), 3)
            except Exception:
                log.warning("some connections did not close in time")
        written = self.world.save()
        self.store.close()
        log.info("world saved (%d keys written), bye", written)

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        session = Session(self, reader, writer)
        self._connections.add(session)
        try:
            await session.run()
        finally:
            self._connections.discard(session)

    # registry ------------------------------------------------------------
    def register(self, session: Session) -> int:
        sid = self._next_id
        self._next_id += 1
        self.sessions[sid] = session
        self.by_name[session.name.casefold()] = session
        return sid

    def unregister(self, session: Session) -> None:
        self.sessions.pop(session.id, None)  # type: ignore[arg-type]
        if self.by_name.get(session.name.casefold()) is session:
            del self.by_name[session.name.casefold()]
        log.info("leave %s (id %s)", session.label, session.id)
        self.broadcast({"t": "player_leave", "id": session.id, "name": session.name})

    def player_list(self, exclude: Session | None = None) -> list[dict[str, Any]]:
        return [
            {"id": s.id, "name": s.name, "pose": s.pose} for s in self.sessions.values() if s is not exclude
        ]

    def broadcast(self, msg: dict[str, Any], exclude: Session | None = None) -> None:
        data = encode(msg)
        for session in list(self.sessions.values()):
            if session is not exclude:
                session.send_raw(data)

    def chat(self, sender: Session, text: str) -> None:
        log.info("chat <%s> %s", sender.name, text)
        self.broadcast({"t": "chat", "id": sender.id, "name": sender.name, "text": text})

    def status(self) -> dict[str, Any]:
        total = sum(self._intervals)
        tps = min(float(self.cfg.tick_rate), len(self._intervals) / total) if total > 0 else float(self.cfg.tick_rate)
        mspt = sum(self._durations) / len(self._durations) * 1000 if self._durations else 0.0
        return {
            "t": "status",
            "proto": PROTOCOL_VERSION,
            "version": __version__,
            "name": self.cfg.name,
            "motd": self.cfg.motd,
            "online": len(self.sessions),
            "max": self.cfg.max_players,
            "players": [s.name for s in self.sessions.values()][:20],
            "password": bool(self.cfg.password),
            "whitelist": self.whitelist_on,
            "tick_rate": self.cfg.tick_rate,
            "tps": round(tps, 2),
            "mspt": round(mspt, 2),
            "uptime": int(time.monotonic() - self._started),
            "rev": self.world.rev,
        }

    # loops ---------------------------------------------------------------
    async def _tick_loop(self) -> None:
        step = 1.0 / self.cfg.tick_rate
        next_at = last = time.monotonic()
        while True:
            next_at += step
            began = time.monotonic()
            if self.tick_n:
                self._intervals.append(began - last)
            last = began
            try:
                self._tick()
            except Exception:
                log.exception("tick failed")
            ended = time.monotonic()
            self._durations.append(ended - began)
            delay = next_at - ended
            if delay > 0:
                await asyncio.sleep(delay)
                continue
            if delay < -1.0:  # Minecraft's "Can't keep up!": skip instead of catching up in a burst
                log.warning("Can't keep up! %.0f ms behind, skipping ticks", -delay * 1000)
                next_at = ended
            await asyncio.sleep(0)

    def _tick(self) -> None:
        self.tick_n += 1
        sessions = list(self.sessions.values())
        moved = {str(s.id): s.pose for s in sessions if s.moved}
        for s in sessions:
            s.moved = False
        if moved:
            for s in sessions:
                others = {k: v for k, v in moved.items() if k != str(s.id)}
                if others:
                    s.send({"t": "tick", "n": self.tick_n, "p": others}, lossy=True)
        self.clocks.tick()
        delta = self.world.take_delta()
        if delta:
            data = encode(delta)
            for s in sessions:
                s.send_raw(data)

    async def _autosave_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.autosave_seconds)
            written = self.world.save()
            if written:
                log.info("autosave: %d world keys written (rev %d)", written, self.world.rev)
