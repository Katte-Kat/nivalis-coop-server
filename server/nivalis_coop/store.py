"""SQLite persistence: player records (tokens, ops, whitelist, bans), the shared
world state and a few server flags. One connection, used from the event loop."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    name_key    TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    token_hash  TEXT,
    first_seen  REAL,
    last_seen   REAL,
    op          INTEGER NOT NULL DEFAULT 0,
    whitelisted INTEGER NOT NULL DEFAULT 0,
    banned      INTEGER NOT NULL DEFAULT 0,
    ban_reason  TEXT
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    player  TEXT NOT NULL,
    key     TEXT NOT NULL,
    delta   INTEGER NOT NULL,
    applied INTEGER NOT NULL,
    result  INTEGER NOT NULL,
    reason  TEXT
);
CREATE INDEX IF NOT EXISTS ledger_key ON ledger(key, id);
CREATE TABLE IF NOT EXISTS saves (
    slot     TEXT NOT NULL,
    version  INTEGER NOT NULL,
    size     INTEGER NOT NULL,
    sha256   TEXT NOT NULL,
    uploader TEXT NOT NULL,
    ts       REAL NOT NULL,
    note     TEXT,
    PRIMARY KEY (slot, version)
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

_FLAGS = {"op", "whitelisted", "banned", "ban_reason"}


class Store:
    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    # players -------------------------------------------------------------
    def player(self, name: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM players WHERE name_key=?", (name.casefold(),)).fetchone()
        return dict(row) if row else None

    def ensure_player(self, name: str) -> dict[str, Any]:
        self.db.execute(
            "INSERT OR IGNORE INTO players(name_key, name, first_seen) VALUES(?,?,?)",
            (name.casefold(), name, time.time()),
        )
        return self.player(name)  # type: ignore[return-value]

    def claim_token(self, name: str, token_hash: str) -> bool:
        """Bind a token to a name. False if somebody claimed it first."""
        self.ensure_player(name)
        cur = self.db.execute(
            "UPDATE players SET token_hash=?, name=? WHERE name_key=? AND token_hash IS NULL",
            (token_hash, name, name.casefold()),
        )
        return cur.rowcount == 1

    def touch(self, name: str) -> None:
        self.db.execute("UPDATE players SET last_seen=? WHERE name_key=?", (time.time(), name.casefold()))

    def update(self, name: str, **flags: Any) -> dict[str, Any]:
        self.ensure_player(name)
        for key, value in flags.items():
            if key not in _FLAGS:
                raise KeyError(key)
            self.db.execute(f"UPDATE players SET {key}=? WHERE name_key=?", (value, name.casefold()))
        return self.player(name)  # type: ignore[return-value]

    def players(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM players ORDER BY name_key")]

    # world state ---------------------------------------------------------
    def load_state(self) -> tuple[dict[str, Any], int]:
        state = {r["key"]: json.loads(r["value"]) for r in self.db.execute("SELECT key, value FROM state")}
        return state, int(self.meta("rev", "0"))

    def flush_state(self, sets: dict[str, Any], dels: set[str], rev: int) -> None:
        self.db.execute("BEGIN")
        try:
            self.db.executemany(
                "INSERT INTO state(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(k, json.dumps(v, separators=(",", ":"), ensure_ascii=False)) for k, v in sets.items()],
            )
            self.db.executemany("DELETE FROM state WHERE key=?", [(k,) for k in dels])
            self.db.execute(
                "INSERT INTO meta(k, v) VALUES('rev', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(rev),)
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    # ledger --------------------------------------------------------------
    def log_ledger(self, player: str, key: str, delta: int, applied: int, result: int, reason: str | None, keep: int) -> None:
        self.db.execute(
            "INSERT INTO ledger(ts, player, key, delta, applied, result, reason) VALUES(?,?,?,?,?,?,?)",
            (time.time(), player, key, delta, applied, result, reason),
        )
        # keep the table bounded; checking every so often is enough
        if keep > 0:
            self.db.execute("DELETE FROM ledger WHERE id <= (SELECT MAX(id) FROM ledger) - ?", (keep,))

    def ledger_tail(self, key: str | None, count: int) -> list[dict[str, Any]]:
        if key:
            rows = self.db.execute("SELECT * FROM ledger WHERE key=? ORDER BY id DESC LIMIT ?", (key, count))
        else:
            rows = self.db.execute("SELECT * FROM ledger ORDER BY id DESC LIMIT ?", (count,))
        return [dict(r) for r in rows][::-1]

    # savegames (the files live on disk, see saves.py) ----------------------
    def add_save(self, slot: str, version: int, size: int, sha: str, uploader: str, note: str | None) -> dict[str, Any]:
        self.db.execute(
            "INSERT INTO saves(slot, version, size, sha256, uploader, ts, note) VALUES(?,?,?,?,?,?,?)",
            (slot, version, size, sha, uploader, time.time(), note),
        )
        return self.get_save(slot, version)  # type: ignore[return-value]

    def get_save(self, slot: str, version: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM saves WHERE slot=? AND version=?", (slot, version)).fetchone()
        return dict(row) if row else None

    def latest_save(self, slot: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM saves WHERE slot=? ORDER BY version DESC LIMIT 1", (slot,)).fetchone()
        return dict(row) if row else None

    def latest_saves(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT s.* FROM saves s JOIN (SELECT slot, MAX(version) v FROM saves GROUP BY slot) m "
            "ON s.slot = m.slot AND s.version = m.v ORDER BY s.slot"
        )
        return [dict(r) for r in rows]

    def drop_old_saves(self, slot: str, keep: int) -> list[dict[str, Any]]:
        """Forget all but the newest ``keep`` versions; returns the dropped rows so their files can be deleted."""
        rows = [dict(r) for r in self.db.execute(
            "SELECT * FROM saves WHERE slot=? ORDER BY version DESC LIMIT -1 OFFSET ?", (slot, max(1, keep)))]
        for row in rows:
            self.db.execute("DELETE FROM saves WHERE slot=? AND version=?", (slot, row["version"]))
        return rows

    def all_saves(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM saves ORDER BY slot, version")]

    # flags ---------------------------------------------------------------
    def meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value)
        )

    def close(self) -> None:
        self.db.close()
