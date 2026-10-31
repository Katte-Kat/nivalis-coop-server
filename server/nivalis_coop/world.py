"""The shared world: a revisioned key/value document that every client mirrors.

Clients propose changes, the server validates and applies them in arrival
order (single event loop = a total order), bumps the revision and sends the
delta to everybody on the next tick. That is the Minecraft model in small:
the server owns the truth, the clients only render and ask.
"""
from __future__ import annotations

from typing import Any

from .protocol import json_size
from .store import Store


class World:
    def __init__(self, store: Store, max_keys: int, max_value_bytes: int, max_total_bytes: int) -> None:
        self.store = store
        self.max_keys = max_keys
        self.max_value_bytes = max_value_bytes
        self.max_total_bytes = max_total_bytes
        self.state, self.rev = store.load_state()
        self._sizes = {k: json_size(v) for k, v in self.state.items()}
        self._total = sum(self._sizes.values())
        self._dirty: set[str] = set()
        self._delta_set: dict[str, Any] = {}
        self._delta_del: set[str] = set()

    def check_set(self, key: str, value: Any) -> str | None:
        """Reason the write is refused, or None if it fits."""
        size = json_size(value)
        if size > self.max_value_bytes:
            return "value too large"
        if key not in self.state and len(self.state) >= self.max_keys:
            return "too many keys"
        if self._total - self._sizes.get(key, 0) + size > self.max_total_bytes:
            return "world data is full"
        return None

    def set(self, key: str, value: Any) -> int:
        self._total += json_size(value) - self._sizes.get(key, 0)
        self._sizes[key] = json_size(value)
        self.state[key] = value
        self.rev += 1
        self._dirty.add(key)
        self._delta_set[key] = value
        self._delta_del.discard(key)
        return self.rev

    def delete(self, key: str) -> int | None:
        if key not in self.state:
            return None
        del self.state[key]
        self._total -= self._sizes.pop(key, 0)
        self.rev += 1
        self._dirty.add(key)
        self._delta_set.pop(key, None)
        self._delta_del.add(key)
        return self.rev

    def take_delta(self) -> dict[str, Any] | None:
        """Everything that changed since the last call, as a ``state`` packet."""
        if not self._delta_set and not self._delta_del:
            return None
        packet = {"t": "state", "rev": self.rev, "set": self._delta_set, "del": sorted(self._delta_del)}
        self._delta_set = {}
        self._delta_del = set()
        return packet

    def save(self) -> int:
        """Flush changed keys to disk; returns how many keys were written or removed."""
        count = len(self._dirty)
        sets = {k: self.state[k] for k in self._dirty if k in self.state}
        dels = {k for k in self._dirty if k not in self.state}
        self.store.flush_state(sets, dels, self.rev)
        self._dirty.clear()
        return count
