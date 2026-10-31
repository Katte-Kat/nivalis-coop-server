"""Shared conversations: when somebody talks to a character, everybody in the same world sees the same
conversation, and every answer is a vote.

The game's conversations are deterministic given the same world state, so the clients run the conversation
themselves in lockstep; the server only (1) tells the others that a conversation started and (2) decides every
choice. A choice is decided as soon as

* everybody who was in the world when the vote opened has voted, or
* at least two different answers have been voted for, or
* the vote window (default 5 s, counted from the first vote) has run out;

if more than one answer got votes, one of the voted answers is drawn at random (each voted answer is equally
likely, the number of votes does not weigh).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .server import CoopServer
    from .session import Session

log = logging.getLogger("nivalis.dialogue")

MAX_OPTIONS = 16
KEEP_CLOSED = 256


class Round:
    def __init__(self, world: str, round_id: str, options: int, expected: set[int]) -> None:
        self.world, self.id, self.options, self.expected = world, round_id, options, expected
        self.votes: dict[int, int] = {}
        self.timer: asyncio.TimerHandle | None = None
        self.deadline: float | None = None  # monotonic time at which the window closes


class DialogueVotes:
    def __init__(self, server: "CoopServer", rng: random.Random | None = None) -> None:
        self.server = server
        self.rng = rng or random.SystemRandom()
        self.rounds: dict[tuple[str, str], Round] = {}
        self._closed: "OrderedDict[tuple[str, str], dict[str, Any]]" = OrderedDict()

    # ------------------------------------------------------------------ helpers
    def _members(self, world: str) -> list["Session"]:
        return [s for s in self.server.sessions.values() if s.world == world]

    def _send_world(self, world: str, msg: dict[str, Any], exclude: "Session | None" = None) -> None:
        for s in self._members(world):
            if s is not exclude:
                s.send(msg)

    # ------------------------------------------------------------------ start of a conversation
    def start(self, s: "Session", tree: str, person: str | None, prev: str | None) -> None:
        """Tell everybody else in the world that this player started a conversation."""
        log.info("conversation %s started by %s in world %s", tree, s.name, s.world)
        self._send_world(s.world, {"t": "dlg_start", "from": s.id, "tree": tree, "person": person, "prev": prev}, exclude=s)

    # ------------------------------------------------------------------ voting
    def vote(self, s: "Session", round_id: str, choice: int, options: int) -> str | None:
        """Record a vote. Returns a reason if it is refused."""
        world = s.world
        key = (world, round_id)
        done = self._closed.get(key)
        if done is not None:  # a client that is a little behind asks for a round that is decided already
            s.send(done)
            return None
        r = self.rounds.get(key)
        if r is None:
            r = Round(world, round_id, options, {m.id for m in self._members(world)})
            self.rounds[key] = r
        if r.options != options:
            return f"the choice has {r.options} answers for the others, not {options}"
        first = not r.votes
        r.votes[s.id] = choice
        if first:
            loop = asyncio.get_running_loop()
            window = self.server.cfg.dialogue_vote_seconds
            r.deadline = time.monotonic() + window
            r.timer = loop.call_later(window, self._timeout, key)
        self._publish(r)
        self._evaluate(r)
        return None

    def _publish(self, r: Round) -> None:
        left = max(0.0, (r.deadline - time.monotonic())) if r.deadline else 0.0
        self._send_world(r.world, {
            "t": "dlg_votes", "round": r.id, "options": r.options,
            "votes": {str(pid): c for pid, c in r.votes.items()},
            "expected": sorted(r.expected), "ms_left": int(left * 1000),
        })

    def _evaluate(self, r: Round) -> None:
        distinct = set(r.votes.values())
        if distinct and (r.expected <= set(r.votes) or len(distinct) >= 2):
            self._resolve(r)

    def _timeout(self, key: tuple[str, str]) -> None:
        r = self.rounds.get(key)
        if r is not None and r.votes:
            self._resolve(r)

    def _resolve(self, r: Round) -> None:
        distinct = sorted(set(r.votes.values()))
        choice = distinct[0] if len(distinct) == 1 else self.rng.choice(distinct)
        if r.timer is not None:
            r.timer.cancel()
        key = (r.world, r.id)
        self.rounds.pop(key, None)
        result = {
            "t": "dlg_result", "round": r.id, "choice": choice, "random": len(distinct) > 1,
            "votes": {str(pid): c for pid, c in r.votes.items()},
        }
        self._closed[key] = result
        while len(self._closed) > KEEP_CLOSED:
            self._closed.popitem(last=False)
        log.info("choice %s decided: answer %d of %s (votes %s)", r.id[-40:], choice, r.options, r.votes)
        self._send_world(r.world, result)

    # ------------------------------------------------------------------ players coming and going
    def player_left(self, s: "Session") -> None:
        """A player left (or went to another world): nobody waits for them any more."""
        for key, r in list(self.rounds.items()):
            if s.id in r.expected or s.id in r.votes:
                r.expected.discard(s.id)
                r.votes.pop(s.id, None)
                if not r.votes:
                    if r.timer is not None:
                        r.timer.cancel()
                        r.timer = None
                    r.deadline = None
                    if not r.expected:
                        self.rounds.pop(key, None)
                else:
                    self._publish(r)
                    self._evaluate(r)
