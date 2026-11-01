"""Who simulates a server world: the session leader (Unity Netcode calls it "distributed authority").

The game simulates its people (customers, staff, background NPCs) inside the game itself; the server cannot run that
code. So in every world one player's game is the **session leader**: it keeps simulating as in single player and
sends a snapshot of its people (``npc_state``) several times a second. The server relays the snapshot to everybody
else in the world, whose games switch their own simulation of people off and show the people where the leader has
them. Nobody picks a host:

* the first player who enters a world becomes its leader;
* when the leader leaves, the player who has been in the world longest takes over and starts from the last snapshot
  the server kept;
* a player who comes later gets the leader and the last snapshot at once (late join).

Players who are not the leader ask for things that touch the simulation (talking to a person, for example) with
``npc_act``; the server forwards that to the leader, whose game does it, and everybody sees the result.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from .protocol import json_size

if TYPE_CHECKING:
    from .server import CoopServer
    from .session import Session

log = logging.getLogger("nivalis.authority")


class Authority:
    def __init__(self, server: "CoopServer") -> None:
        self.server = server
        self.leaders: dict[str, int] = {}            # world -> session id of the leader
        self.order: dict[str, list[int]] = {}        # world -> session ids by the time they came
        self.snapshots: dict[str, dict[str, Any]] = {}  # world -> last npc_state packet (as sent to clients)

    # ------------------------------------------------------------------ helpers
    def _members(self, world: str) -> list["Session"]:
        return [s for s in self.server.sessions.values() if s.world == world]

    def leader_of(self, world: str | None) -> int | None:
        return self.leaders.get(world) if world is not None else None

    def _announce(self, world: str, handover: bool) -> None:
        lid = self.leaders.get(world)
        leader = self.server.sessions.get(lid) if lid is not None else None
        packet = {"t": "leader", "world": world, "id": lid, "name": leader.name if leader else None, "handover": handover}
        for s in self._members(world):
            s.send(packet)

    # ------------------------------------------------------------------ players coming and going
    def on_join(self, s: "Session") -> None:
        world = s.world
        order = self.order.setdefault(world, [])
        if s.id not in order:
            order.append(s.id)
        if world not in self.leaders:
            self.leaders[world] = s.id
            log.info("%s is the session leader of world %s", s.label, world)
            self._announce(world, handover=False)
        else:
            lid = self.leaders[world]
            leader = self.server.sessions.get(lid)
            s.send({"t": "leader", "world": world, "id": lid, "name": leader.name if leader else None, "handover": False})
        snap = self.snapshots.get(world)
        if snap is not None and self.leaders.get(world) != s.id:
            s.send(snap)

    def player_left(self, s: "Session", world: str | None = None) -> None:
        world = world or s.world
        if world is None:
            return
        order = self.order.get(world, [])
        if s.id in order:
            order.remove(s.id)
        if self.leaders.get(world) != s.id:
            return
        # the leader went: the one who has been here longest takes over
        alive = [i for i in order if i in self.server.sessions and self.server.sessions[i].world == world]
        if not alive:
            del self.leaders[world]
            self.order.pop(world, None)
            log.info("world %s has no players, no session leader", world)
            return  # the snapshot stays: whoever comes next starts from it
        self.leaders[world] = alive[0]
        log.info("session leader of world %s went, %s takes over", world, self.server.sessions[alive[0]].label)
        self._announce(world, handover=True)
        snap = self.snapshots.get(world)
        if snap is not None:
            self.server.sessions[alive[0]].send(snap)  # start from where the old leader was

    # ------------------------------------------------------------------ packets from clients
    def npc_state(self, s: "Session", msg: dict[str, Any]) -> str | None:
        """The leader's snapshot of the people in its world. Anything but a list of objects is refused."""
        if s.world is None:
            return "not in a world"
        if self.leaders.get(s.world) != s.id:
            return "not the session leader"
        npcs = msg.get("npcs")
        if not isinstance(npcs, list) or not all(isinstance(n, dict) for n in npcs):
            return "bad npcs"
        if len(npcs) > self.server.cfg.max_npcs or json_size(npcs) > self.server.cfg.max_npc_bytes:
            return "too many npcs"
        tick = msg.get("tick")
        packet = {"t": "npc_state", "from": s.id, "tick": tick if isinstance(tick, int) else 0,
                  "full": bool(msg.get("full", True)), "npcs": npcs, "at": round(time.time(), 3)}
        if packet["full"]:
            self.snapshots[s.world] = packet
        elif s.world in self.snapshots:
            self._merge(self.snapshots[s.world], npcs)
        for m in self._members(s.world):
            if m is not s:
                m.send(packet, lossy=True)  # a newer snapshot follows soon; drop rather than queue
        return None

    @staticmethod
    def _merge(snapshot: dict[str, Any], changes: list[dict[str, Any]]) -> None:
        """A partial update replaces the people with the same id and keeps the others (late joiners get the sum)."""
        by_id = {n.get("id"): n for n in snapshot["npcs"]}
        for n in changes:
            if n.get("gone"):
                by_id.pop(n.get("id"), None)
            else:
                by_id[n.get("id")] = n
        snapshot["npcs"] = list(by_id.values())

    def npc_act(self, s: "Session", msg: dict[str, Any]) -> str | None:
        """A player who is not the leader wants to do something with a person: the leader's game does it."""
        if s.world is None:
            return "not in a world"
        lid = self.leaders.get(s.world)
        if lid is None:
            return "no session leader"
        action, data = msg.get("action"), msg.get("data")
        if not isinstance(action, str) or not 1 <= len(action) <= 32 or json_size(data) > 2048:
            return "bad action"
        packet = {"t": "npc_act", "from": s.id, "npc": msg.get("npc"), "action": action, "data": data}
        if lid == s.id:  # the leader may send it too (it then simply does it itself)
            return None
        leader = self.server.sessions.get(lid)
        if leader is not None:
            leader.send(packet)
        return None
