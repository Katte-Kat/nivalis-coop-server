"""The clock of a server world. Like in Minecraft the server owns the time and every client follows it.

The game counts time in whole "game seconds" since the start of the game (TimeStamp.TotalGameSeconds) and runs
them at some speed (game seconds per real second). The server does not know that speed, so the first client in a
world reports both its time and the speed it measured (``time_init``); from then on the clock advances on the
server for as long as somebody is in the world, and it is sent to everybody once a second.

* A conversation pauses the clock for everybody (``time_pause``): all clients are in the same conversation.
  A personal menu does not: that client simply catches up afterwards.
* Sleeping or fast-forwarding is a jump forward (``time_jump``) that everybody follows. Time never runs backwards.
* A pause that nobody ends (a crashed game, a lost packet) expires after ``pause_timeout_seconds``.
"""
from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .server import CoopServer
    from .session import Session

log = logging.getLogger("nivalis.clock")

MIN_RATE, MAX_RATE = 0.01, 100_000.0
MAX_SECONDS = 10**10


class Clock:
    def __init__(self, seconds: float, rate: float, now: float) -> None:
        self.seconds = float(seconds)
        self.rate = float(rate)
        self.last = now
        self.pauses: dict[int, tuple[str, float]] = {}  # player id -> (reason, since)

    @property
    def paused(self) -> bool:
        return bool(self.pauses)

    def advance(self, now: float) -> None:
        if not self.pauses:
            self.seconds += (now - self.last) * self.rate
        self.last = now


class WorldClocks:
    def __init__(self, server: "CoopServer") -> None:
        self.server = server
        self.clocks: dict[str, Clock] = {}
        self._next_broadcast = 0.0

    # ------------------------------------------------------------------ helpers
    def _members(self, world: str) -> list["Session"]:
        return [s for s in self.server.sessions.values() if s.world == world]

    def _packet(self, world: str, clock: Clock, jump: bool = False) -> dict[str, Any]:
        return {
            "t": "time", "seconds": round(clock.seconds, 3), "rate": clock.rate,
            "paused": clock.paused, "reasons": sorted({r for r, _ in clock.pauses.values()}), "jump": jump,
        }

    def _send_all(self, world: str, clock: Clock, jump: bool = False) -> None:
        packet = self._packet(world, clock, jump)
        for s in self._members(world):
            s.send(packet)

    # ------------------------------------------------------------------ players coming and going
    def on_join(self, s: "Session") -> None:
        """Somebody entered a world: give them the clock, or ask them to start it."""
        clock = self.clocks.get(s.world)
        if clock is None:
            s.send({"t": "time_need"})
        else:
            clock.advance(time.monotonic())
            s.send(self._packet(s.world, clock))

    def player_left(self, s: "Session", world: str | None = None) -> None:
        world = world or s.world
        if world is None:
            return
        clock = self.clocks.get(world)
        if clock is None:
            return
        members = [m for m in self._members(world) if m is not s]
        if not members:  # nobody left: the world stands still until somebody comes back
            del self.clocks[world]
            log.info("clock of world %s stopped at %.0f", world, clock.seconds)
            return
        if clock.pauses.pop(s.id, None) is not None:
            clock.advance(time.monotonic())
            self._send_all(world, clock)

    # ------------------------------------------------------------------ packets from clients
    def init(self, s: "Session", seconds: Any, rate: Any) -> str | None:
        if not _number(seconds) or not 0 <= seconds <= MAX_SECONDS or not _number(rate) or not MIN_RATE <= rate <= MAX_RATE:
            return "invalid time"
        if s.world in self.clocks:
            self.on_join(s)  # somebody was faster: just follow that clock
            return None
        clock = Clock(seconds, rate, time.monotonic())
        self.clocks[s.world] = clock
        log.info("clock of world %s started by %s at %.0f (%.2f game seconds per second)", s.world, s.name, seconds, rate)
        self._send_all(s.world, clock)
        return None

    def pause(self, s: "Session", on: Any, reason: Any) -> str | None:
        clock = self.clocks.get(s.world)
        if clock is None or not isinstance(on, bool):
            return "no clock" if clock is None else "invalid pause"
        reason = reason if isinstance(reason, str) and 0 < len(reason) <= 24 else "other"
        now = time.monotonic()
        clock.advance(now)
        if on:
            clock.pauses[s.id] = (reason, now)
        else:
            clock.pauses.pop(s.id, None)
        clock.last = now
        self._send_all(s.world, clock)
        return None

    def jump(self, s: "Session", to: Any) -> str | None:
        clock = self.clocks.get(s.world)
        if clock is None:
            return "no clock"
        if not _number(to):
            return "invalid time"
        now = time.monotonic()
        clock.advance(now)
        if to <= clock.seconds:
            return None  # time never runs backwards; somebody else was already further
        if to - clock.seconds > self.server.cfg.max_time_jump_seconds:
            return "jump too far"
        clock.seconds = float(to)
        log.info("clock of world %s jumped to %.0f by %s", s.world, to, s.name)
        self._send_all(s.world, clock, jump=True)
        return None

    # ------------------------------------------------------------------ called every server tick
    def tick(self) -> None:
        if not self.clocks:
            return
        now = time.monotonic()
        timeout = self.server.cfg.pause_timeout_seconds
        for world, clock in list(self.clocks.items()):
            expired = [pid for pid, (_, since) in clock.pauses.items() if now - since > timeout]
            if expired:
                clock.advance(now)
                for pid in expired:
                    del clock.pauses[pid]
                    log.warning("a pause in world %s ran out after %.0f s", world, timeout)
                clock.last = now
            clock.advance(now)
        if now >= self._next_broadcast:
            self._next_broadcast = now + self.server.cfg.clock_broadcast_seconds
            for world, clock in self.clocks.items():
                self._send_all(world, clock)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
