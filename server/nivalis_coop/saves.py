"""Savegames held by the server ("slots"). The shared world is a .sav file that lives here: joining a server
means downloading its newest version and loading it, saving means uploading a new version.

Uploads are chunked (a save is ~10 MB, a frame is at most 256 KiB), verified against a SHA-256 the client
announces, and only accepted from the version the client started from (``base``): a player who joined,
played and saves after somebody else has already saved gets "stale" instead of silently overwriting.
Downloads stream with back pressure so a slow client never fills the send buffer the tick loop shares.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import os
import re
import time
from typing import TYPE_CHECKING, Any

from .protocol import clean_text

if TYPE_CHECKING:
    from .server import CoopServer
    from .session import Session

log = logging.getLogger("nivalis.saves")

SLOT_RE = re.compile(r"[a-z0-9_]{1,24}")
SHA_RE = re.compile(r"[0-9a-f]{64}")
CHUNK = 128 * 1024
MAX_CHUNK_B64 = 4 * ((CHUNK + 2) // 3) + 4


class Upload:
    def __init__(self, slot: str, base: int, size: int, sha: str, path: str) -> None:
        self.slot, self.base, self.size, self.sha, self.path = slot, base, size, sha, path
        self.received = 0
        self.next_chunk = 0
        self.hash = hashlib.sha256()
        self.fh = open(path, "wb")
        self.note: str | None = None

    def close(self) -> None:
        if not self.fh.closed:
            self.fh.close()


class SaveService:
    def __init__(self, server: "CoopServer") -> None:
        self.server = server
        self.dir = os.path.join(server.cfg.data_dir, "saves")
        os.makedirs(self.dir, exist_ok=True)
        for leftover in self._leftovers():  # an upload that was cut by a crash or a restart
            os.unlink(leftover)

    def _leftovers(self) -> list[str]:
        found = []
        for slot in os.listdir(self.dir):
            sub = os.path.join(self.dir, slot)
            if os.path.isdir(sub):
                found += [os.path.join(sub, f) for f in os.listdir(sub) if f.endswith(".tmp")]
        return found

    def path_of(self, slot: str, version: int) -> str:
        return os.path.join(self.dir, slot, f"{version}.sav")

    def listing(self) -> list[dict[str, Any]]:
        return [self._public(r) for r in self.server.store.latest_saves()]

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        return {k: row[k] for k in ("slot", "version", "size", "sha256", "uploader", "ts", "note")}

    # upload ----------------------------------------------------------------
    def begin(self, s: "Session", msg: dict[str, Any]) -> None:
        cfg, store = self.server.cfg, self.server.store
        slot, size, sha, base = msg.get("slot"), msg.get("size"), msg.get("sha256"), msg.get("base", 0)
        if s._upload is not None:
            return self._reject(s, slot, "an upload is already running")
        if not isinstance(slot, str) or not SLOT_RE.fullmatch(slot):
            return self._reject(s, slot, "invalid slot name (a-z, 0-9, _, up to 24)")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= cfg.max_save_bytes:
            return self._reject(s, slot, f"invalid size (1..{cfg.max_save_bytes} bytes)")
        if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
            return self._reject(s, slot, "invalid sha256")
        if not isinstance(base, int) or isinstance(base, bool) or base < 0:
            return self._reject(s, slot, "invalid base")
        current = store.latest_save(slot)
        have = current["version"] if current else 0
        if base != have:
            return self._reject(s, slot, f"stale: the server already has version {have}, you started from {base}")
        if current is None and len(store.latest_saves()) >= cfg.max_save_slots:
            return self._reject(s, slot, "too many save slots")
        os.makedirs(os.path.join(self.dir, slot), exist_ok=True)
        tmp = os.path.join(self.dir, slot, f".upload-{s.id}.tmp")
        s._upload = Upload(slot, base, size, sha, tmp)
        s._upload.note = clean_text(msg.get("note"), 80)
        s.send({"t": "save_put_ok", "slot": slot})

    def data(self, s: "Session", msg: dict[str, Any]) -> None:
        up: Upload | None = s._upload
        if up is None or msg.get("slot") != up.slot:
            return self._reject(s, msg.get("slot"), "no upload in progress")
        raw = msg.get("data")
        if msg.get("n") != up.next_chunk or not isinstance(raw, str) or len(raw) > MAX_CHUNK_B64:
            return self._abort(s, "chunks out of order or too large")
        try:
            chunk = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            return self._abort(s, "chunk is not valid base64")
        if not chunk or len(chunk) > CHUNK or up.received + len(chunk) > up.size:
            return self._abort(s, "chunk size does not fit the announced size")
        up.fh.write(chunk)
        up.hash.update(chunk)
        up.received += len(chunk)
        up.next_chunk += 1

    def end(self, s: "Session", msg: dict[str, Any]) -> None:
        up: Upload | None = s._upload
        if up is None or msg.get("slot") != up.slot:
            return self._reject(s, msg.get("slot"), "no upload in progress")
        up.close()
        store = self.server.store
        if up.received != up.size:
            return self._abort(s, f"incomplete: got {up.received} of {up.size} bytes")
        if up.hash.hexdigest() != up.sha:
            return self._abort(s, "checksum mismatch")
        current = store.latest_save(up.slot)
        have = current["version"] if current else 0
        if have != up.base:
            return self._abort(s, f"stale: the server got version {have} while you were uploading")
        version = have + 1
        os.replace(up.path, self.path_of(up.slot, version))
        s._upload = None
        row = store.add_save(up.slot, version, up.size, up.sha, s.name, up.note)
        self._prune(up.slot)
        log.info("save %s v%d stored (%d bytes) by %s", up.slot, version, up.size, s.name)
        s.send({"t": "save_stored", **self._public(row)})
        self.server.broadcast({"t": "save_updated", **self._public(row)}, exclude=s)

    def abort(self, s: "Session") -> None:
        """The player left (or misbehaved) mid-upload: forget it, nothing was committed."""
        up: Upload | None = s._upload
        s._upload = None
        if up is not None:
            up.close()
            try:
                os.unlink(up.path)
            except OSError:
                pass

    def _abort(self, s: "Session", reason: str) -> None:
        slot = s._upload.slot if s._upload else None
        self.abort(s)
        self._reject(s, slot, reason)

    @staticmethod
    def _reject(s: "Session", slot: Any, reason: str) -> None:
        s.send({"t": "save_reject", "slot": slot if isinstance(slot, str) else None, "reason": reason})

    def _prune(self, slot: str) -> None:
        for row in self.server.store.drop_old_saves(slot, self.server.cfg.save_keep):
            try:
                os.unlink(self.path_of(slot, row["version"]))
            except OSError:
                pass

    # download --------------------------------------------------------------
    async def send(self, s: "Session", msg: dict[str, Any]) -> None:
        slot, version = msg.get("slot"), msg.get("version")
        store = self.server.store
        row = None
        if isinstance(slot, str) and SLOT_RE.fullmatch(slot):
            row = store.get_save(slot, version) if isinstance(version, int) and not isinstance(version, bool) else store.latest_save(slot)
        if row is None:
            return self._reject(s, slot, "no such save")
        path = self.path_of(row["slot"], row["version"])
        try:
            fh = open(path, "rb")
        except OSError:
            return self._reject(s, slot, "the save file is missing on the server")
        with fh:
            s.send({"t": "save_info", **self._public(row)})
            n = 0
            while chunk := fh.read(CHUNK):
                if s.closing:
                    return
                s.send({"t": "save_data", "slot": row["slot"], "n": n, "data": base64.b64encode(chunk).decode("ascii")})
                n += 1
                await s.writer.drain()  # back pressure: never queue more than a chunk or two per client
            s.send({"t": "save_done", "slot": row["slot"], "version": row["version"], "chunks": n})
