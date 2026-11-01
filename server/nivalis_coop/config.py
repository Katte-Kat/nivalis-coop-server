"""Server settings: defaults, an optional TOML file, then NIVALIS_COOP_* variables.

The environment layer plays the role the itzg/minecraft-server image gives to
its variables: a container can be configured without editing any file.
"""
from __future__ import annotations

import os
import tomllib
import typing
from dataclasses import dataclass, field, fields
from pathlib import Path

ENV_PREFIX = "NIVALIS_COOP_"


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 27800
    name: str = "Nivalis Coop"
    motd: str = "Welcome to the restaurant"
    max_players: int = 8
    tick_rate: int = 20
    password: str = ""
    whitelist: bool = False
    data_dir: str = "/data"
    autosave_seconds: float = 300.0
    keepalive_interval: float = 10.0
    keepalive_timeout: float = 30.0
    login_timeout: float = 10.0
    # keys under these prefixes may be written by every player, others only by ops
    state_write_prefixes: list[str] = field(default_factory=lambda: ["world/", "shared/"])
    max_state_keys: int = 4096
    max_state_value_bytes: int = 16 * 1024
    max_state_total_bytes: int = 4 * 1024 * 1024
    # shared counters (state_add), e.g. a shared bank account: results are clamped into this range
    counter_min: int = 0
    counter_max: int = 2_000_000_000
    ledger_keep: int = 20000  # bookings kept in the audit log
    # server-held savegames
    max_save_bytes: int = 64 * 1024 * 1024
    max_save_slots: int = 8
    save_keep: int = 5  # versions kept per slot
    # shared conversations: how long the others get to vote after the first vote
    dialogue_vote_seconds: float = 5.0
    # world clocks: how often the time is sent, how long a pause may last without being ended, the longest jump (sleep)
    clock_broadcast_seconds: float = 1.0
    pause_timeout_seconds: float = 600.0
    max_time_jump_seconds: int = 3 * 86400
    # session leader: the limits of one snapshot of the people in a world (npc_state)
    max_npcs: int = 1000
    max_npc_bytes: int = 512 * 1024
    # what one player keeps per world (inventory): bytes per entry
    max_player_data_bytes: int = 256 * 1024
    # players whose mod is older are told so in the chat when they log in (empty: never)
    recommended_mod: str = "0.6.1"
    log_level: str = "INFO"
    admin_socket: str = ""

    @property
    def admin_path(self) -> str:
        return self.admin_socket or os.path.join(self.data_dir, "admin.sock")

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "world.db")

    def validate(self) -> None:
        if not 0 <= self.port <= 65535:
            raise ValueError("port must be 0-65535")
        if not 1 <= self.tick_rate <= 60:
            raise ValueError("tick_rate must be 1-60")
        if not 1 <= self.max_players <= 100:
            raise ValueError("max_players must be 1-100")
        if self.keepalive_timeout <= self.keepalive_interval:
            raise ValueError("keepalive_timeout must be larger than keepalive_interval")
        if not 0.05 <= self.dialogue_vote_seconds <= 120:
            raise ValueError("dialogue_vote_seconds must be 0.05-120")
        if self.counter_min > self.counter_max:
            raise ValueError("counter_min must not exceed counter_max")
        if self.max_state_value_bytes > self.max_state_total_bytes:
            raise ValueError("max_state_value_bytes must not exceed max_state_total_bytes")

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        hints = typing.get_type_hints(cls)
        known = {f.name for f in fields(cls)}
        values: dict[str, object] = {}
        if path and Path(path).is_file():
            with open(path, "rb") as fh:
                for key, raw in tomllib.load(fh).items():
                    if key not in known:
                        raise ValueError(f"unknown setting {key!r} in {path}")
                    values[key] = _coerce(key, raw, hints[key])
        for key in known:
            raw = os.environ.get(ENV_PREFIX + key.upper())
            if raw is not None and raw != "":
                values[key] = _coerce(key, raw, hints[key])
        cfg = cls(**values)  # type: ignore[arg-type]
        cfg.validate()
        return cfg


def _coerce(key: str, raw: object, tp: object) -> object:
    try:
        if tp is bool:
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        if tp is int:
            if isinstance(raw, bool):
                raise ValueError
            return int(raw)  # type: ignore[arg-type]
        if tp is float:
            return float(raw)  # type: ignore[arg-type]
        if tp is str:
            return str(raw)
        if typing.get_origin(tp) is list:
            if isinstance(raw, list):
                return [str(item) for item in raw]
            return [part.strip() for part in str(raw).split(",") if part.strip()]
    except (TypeError, ValueError):
        raise ValueError(f"setting {key!r}: cannot use {raw!r}") from None
    raise ValueError(f"setting {key!r}: unsupported type")
