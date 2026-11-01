"""One client connection: handshake, login, then the play loop."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

from .protocol import (
    KIND_RE,
    AVATAR_RE,
    PDATA_KEY_RE,
    MAX_FRAME_PLAY,
    MAX_FRAME_PRELOGIN,
    PROTOCOL_VERSION,
    ProtocolError,
    clean_text,
    encode,
    is_number,
    json_size,
    read_frame,
    valid_key,
    valid_name,
)

if TYPE_CHECKING:
    from .server import CoopServer

log = logging.getLogger("nivalis.session")

SEND_BUFFER_DROP = 256 * 1024  # lossy packets (positions) are dropped above this
SEND_BUFFER_KICK = 4 * 1024 * 1024  # a client that cannot keep up is kicked
STRIKE_LIMIT = 100


class Bucket:
    """Token bucket: ``rate`` per second, bursts up to ``burst``."""

    def __init__(self, rate: float, burst: float) -> None:
        self.rate, self.burst, self.tokens, self.stamp = rate, burst, burst, time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


def _older_mod(client_mod: str, recommended: str) -> str | None:
    """The client's version if its "NivalisCoop/x.y.z" is older than ``recommended``, else None."""
    if not recommended or "/" not in client_mod:
        return None
    version = client_mod.split("/", 1)[1]
    try:
        have = tuple(int(p) for p in version.split("."))
        want = tuple(int(p) for p in recommended.split("."))
    except ValueError:
        return None
    return version if have < want else None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Session:
    def __init__(self, server: "CoopServer", reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.server = server
        self.reader = reader
        self.writer = writer
        peer = writer.get_extra_info("peername")
        self.peer = f"{peer[0]}:{peer[1]}" if peer else "?"
        self.id: int | None = None
        self.name = ""
        self.is_op = False
        self.client_mod = "?"
        self.avatar: str | None = None  # the game model this player is shown as
        self.world: str | None = None  # the server world this player plays in (scopes shared conversations)
        self.pose: list[Any] = [0.0, 0.0, 0.0, 0.0, "idle", ""]
        self.moved = False
        self.closing = False
        self.ping_ms: int | None = None
        self._ka_id = 0
        self._ka_sent = 0.0
        self._last_ack = 0.0
        self._ka_task: asyncio.Task | None = None
        self._strikes = 0
        self._move_bucket = Bucket(40, 10)
        self._chat_bucket = Bucket(1.0, 5)
        self._state_bucket = Bucket(30, 30)
        self._event_bucket = Bucket(30, 30)
        self._save_bucket = Bucket(150, 150)  # 150 chunks/s is ~19 MB/s, far above any real link
        self._upload = None  # saves.Upload while this player is uploading a savegame
        self._dlg_bucket = Bucket(20, 20)
        self._npc_bucket = Bucket(25, 25)  # snapshots ~10/s plus actions
        self._download_task: asyncio.Task | None = None
        self._handlers = {
            "keepalive": self._on_keepalive,
            "move": self._on_move,
            "chat": self._on_chat,
            "state_set": self._on_state_set,
            "state_del": self._on_state_del,
            "state_add": self._on_state_add,
            "event": self._on_event,
            "save_put": self._on_save_put,
            "save_data": self._on_save_data,
            "save_end": self._on_save_end,
            "save_get": self._on_save_get,
            "save_list": self._on_save_list,
            "join_world": self._on_join_world,
            "dlg_start": self._on_dlg_start,
            "dlg_vote": self._on_dlg_vote,
            "time_init": self._on_time_init,
            "time_pause": self._on_time_pause,
            "time_jump": self._on_time_jump,
            "npc_state": self._on_npc_state,
            "avatar_set": self._on_avatar_set,
            "pdata_set": self._on_pdata_set,
            "npc_act": self._on_npc_act,
            "bye": self._on_bye,
        }

    @property
    def label(self) -> str:
        return f"{self.name or '?'}@{self.peer}"

    # output --------------------------------------------------------------
    def send(self, msg: dict[str, Any], lossy: bool = False) -> None:
        self.send_raw(encode(msg), lossy)

    def send_raw(self, data: bytes, lossy: bool = False) -> None:
        if self.closing:
            return
        size = self.writer.transport.get_write_buffer_size()
        if size > SEND_BUFFER_KICK:
            self.kick("Too slow: send buffer is full", abort=True)
            return
        if lossy and size > SEND_BUFFER_DROP:
            return
        self.writer.write(data)

    def reply(self, text: str) -> None:
        self.send({"t": "chat", "id": 0, "name": "Server", "text": text})

    def kick(self, reason: str, abort: bool = False) -> None:
        if self.closing:
            return
        log.info("kick %s: %s", self.label, reason)
        if not abort:
            try:
                self.writer.write(encode({"t": "kick", "reason": reason}))
            except Exception:  # transport already gone
                pass
        self.closing = True
        if abort:
            self.writer.transport.abort()
        else:
            self.writer.close()  # flushes the buffered kick packet first

    # lifecycle -----------------------------------------------------------
    async def run(self) -> None:
        try:
            if await self._login():
                await self._play()
        except TimeoutError:  # before OSError: TimeoutError is an OSError
            self.kick("Timed out")
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        except ProtocolError as exc:
            self.kick(f"Protocol error: {exc}")
        except Exception:
            log.exception("session crashed: %s", self.label)
            self.kick("Internal server error")
        finally:
            await self._close()

    async def _close(self) -> None:
        if self._ka_task:
            self._ka_task.cancel()
        if self._download_task:
            self._download_task.cancel()
        self.server.saves.abort(self)
        if self.id is not None and self.world is not None:
            self.server.dialogues.player_left(self)
            self.server.clocks.player_left(self)
            self.server.authority.player_left(self)
        if self.id is not None:
            self.server.unregister(self)
        if not self.closing:
            self.closing = True
            self.writer.close()
        try:
            await asyncio.wait_for(self.writer.wait_closed(), 2)
        except Exception:
            self.writer.transport.abort()

    async def _read(self, max_len: int, timeout: float | None = None) -> dict[str, Any]:
        coro = read_frame(self.reader, max_len)
        return await (asyncio.wait_for(coro, timeout) if timeout else coro)

    # handshake + login ---------------------------------------------------
    async def _login(self) -> bool:
        server, cfg, store = self.server, self.server.cfg, self.server.store
        hello = await self._read(MAX_FRAME_PRELOGIN, cfg.login_timeout)
        if hello["t"] != "hello":
            raise ProtocolError("expected hello")
        self.client_mod = clean_text(hello.get("mod"), 32) or "?"
        if hello.get("intent") == "status":
            self.send(server.status())
            return False
        if hello.get("proto") != PROTOCOL_VERSION:
            self.kick(f"Protocol mismatch: server {PROTOCOL_VERSION}, client {hello.get('proto')!r}")
            return False

        msg = await self._read(MAX_FRAME_PRELOGIN, cfg.login_timeout)
        if msg["t"] != "login":
            raise ProtocolError("expected login")
        name = msg.get("name")
        if not valid_name(name):
            self.kick("Invalid name: 2-24 characters, letters, digits, space . - _")
            return False
        if cfg.password and not hmac.compare_digest(str(msg.get("password", "")).encode(), cfg.password.encode()):
            self.kick("Wrong server password")
            return False
        rec = store.player(name)
        if rec and rec["banned"]:
            self.kick("You are banned" + (f": {rec['ban_reason']}" if rec["ban_reason"] else ""))
            return False
        if server.whitelist_on and not (rec and (rec["whitelisted"] or rec["op"])):
            self.kick("You are not on the whitelist")
            return False
        new_token: str | None = None
        if rec and rec["token_hash"]:
            presented = msg.get("token")
            if not isinstance(presented, str) or not hmac.compare_digest(hash_token(presented), rec["token_hash"]):
                self.kick("This name belongs to another player (token missing or wrong)")
                return False
        else:
            new_token = secrets.token_urlsafe(24)
        is_op = bool(rec and rec["op"])
        others = [s for s in server.sessions.values() if s.name.casefold() != name.casefold()]
        if len(others) >= cfg.max_players and not is_op:
            self.kick("Server is full")
            return False
        if new_token and not store.claim_token(name, hash_token(new_token)):
            self.kick("This name was just taken by another player")
            return False

        old = server.by_name.get(name.casefold())
        if old is not None:
            old.kick("Logged in from another location")
        self.name = rec["name"] if rec else name
        self.is_op = is_op
        self.avatar = (rec or {}).get("avatar")
        store.touch(self.name)
        self.id = server.register(self)
        self._last_ack = time.monotonic()
        ok: dict[str, Any] = {
            "t": "login_ok",
            "id": self.id,
            "name": self.name,
            "op": is_op,
            "avatar": self.avatar,  # null on the first login: let the player choose one
            "server": cfg.name,
            "motd": cfg.motd,
            "tick_rate": cfg.tick_rate,
            "tick": server.tick_n,
            "server_time": int(time.time() * 1000),
            "keepalive_interval": cfg.keepalive_interval,
            "players": server.player_list(exclude=self),
            "rev": server.world.rev,
            "state": server.world.state,
            "saves": server.saves.listing(),
        }
        if new_token:
            ok["token"] = new_token
        self.send(ok)
        server.broadcast({"t": "player_join", "id": self.id, "name": self.name, "pose": self.pose, "avatar": self.avatar}, exclude=self)
        log.info("join %s (id %s, mod %s)", self.label, self.id, self.client_mod)
        old = _older_mod(self.client_mod, cfg.recommended_mod)
        if old:
            self.send({"t": "chat", "id": 0, "name": "Server", "text": f"Your mod {old} is older than {cfg.recommended_mod}: "
                       "other players and shared people may not work for you. Get the new one from the server owner."})
        return True

    # play ----------------------------------------------------------------
    async def _play(self) -> None:
        self._ka_task = asyncio.create_task(self._keepalive_loop())
        while not self.closing:
            msg = await self._read(MAX_FRAME_PLAY)
            handler = self._handlers.get(msg["t"])
            if handler is None:
                self._strike(f"unknown packet {msg['t'][:20]!r}")
                continue
            if handler(msg) is False:
                return

    async def _keepalive_loop(self) -> None:
        cfg = self.server.cfg
        while True:
            await asyncio.sleep(cfg.keepalive_interval)
            now = time.monotonic()
            if now - self._last_ack > cfg.keepalive_timeout:
                self.kick("Timed out")
                return
            self._ka_id += 1
            self._ka_sent = now
            self._strikes = max(0, self._strikes - 10)
            self.send({"t": "keepalive", "id": self._ka_id, "st": int(time.time() * 1000)})

    def _strike(self, reason: str) -> None:
        self._strikes += 1
        if self._strikes >= STRIKE_LIMIT:
            self.kick(f"Flooding ({reason})")

    def _may_write(self, key: str) -> bool:
        return self.is_op or any(key.startswith(p) for p in self.server.cfg.state_write_prefixes)

    def _reject(self, key: Any, reason: str) -> None:
        world = self.server.world
        self.send(
            {
                "t": "state_reject",
                "key": key if isinstance(key, str) else None,
                "reason": reason,
                "rev": world.rev,
                "value": world.state.get(key) if isinstance(key, str) else None,
            }
        )

    # handlers ------------------------------------------------------------
    def _on_keepalive(self, msg: dict[str, Any]) -> None:
        if msg.get("id") == self._ka_id and self._ka_id:
            now = time.monotonic()
            self.ping_ms = round((now - self._ka_sent) * 1000)
            self._last_ack = now

    def _on_move(self, msg: dict[str, Any]) -> None:
        if not self._move_bucket.take():
            return  # positions are lossy by design, extra ones are just dropped
        vals = [msg.get(k) for k in ("x", "y", "z", "ry")]
        if not all(is_number(v) for v in vals):
            self._strike("bad move")
            return
        self.pose = [round(float(v), 3) for v in vals] + [
            clean_text(msg.get("anim"), 32) or "idle",
            clean_text(msg.get("scene"), 96) or "",
        ]
        self.moved = True

    def _on_chat(self, msg: dict[str, Any]) -> None:
        text = clean_text(msg.get("text"), 256)
        if text is None:
            return
        if not self._chat_bucket.take():
            self.reply("You are sending messages too fast.")
            self._strike("chat")
            return
        if text.startswith("/"):
            if not self.is_op:
                self.reply("You do not have permission to use commands.")
                return
            for line in self.server.commands.execute(text[1:], actor=self):
                self.reply(line)
            return
        self.server.chat(self, text)

    def _on_state_set(self, msg: dict[str, Any]) -> None:
        if not self._state_bucket.take():
            self._strike("state flood")
            return
        key, value = msg.get("key"), msg.get("value")
        world = self.server.world
        if not valid_key(key):
            return self._reject(key, "invalid key")
        if value is None:
            return self._reject(key, "value missing (use state_del to remove a key)")
        if not self._may_write(key):
            return self._reject(key, "not allowed")
        if "expect" in msg and world.state.get(key) != msg["expect"]:
            return self._reject(key, "conflict")
        reason = world.check_set(key, value)
        if reason:
            return self._reject(key, reason)
        world.set(key, value)

    def _on_state_del(self, msg: dict[str, Any]) -> None:
        if not self._state_bucket.take():
            self._strike("state flood")
            return
        key = msg.get("key")
        world = self.server.world
        if not valid_key(key):
            return self._reject(key, "invalid key")
        if not self._may_write(key):
            return self._reject(key, "not allowed")
        if "expect" in msg and world.state.get(key) != msg["expect"]:
            return self._reject(key, "conflict")
        world.delete(key)

    def _on_state_add(self, msg: dict[str, Any]) -> None:
        """Add ``delta`` to an integer key (a shared account). Adds from several players never overwrite
        each other, the server just sums them in arrival order and clamps into [counter_min, counter_max]."""
        if not self._state_bucket.take():
            self._strike("state flood")
            return
        cfg, world = self.server.cfg, self.server.world
        key, delta, cid = msg.get("key"), msg.get("delta"), msg.get("cid")
        cid = cid if isinstance(cid, int) and not isinstance(cid, bool) else None
        if not valid_key(key):
            return self._reject(key, "invalid key")
        if not isinstance(delta, int) or isinstance(delta, bool) or abs(delta) > cfg.counter_max:
            return self._reject(key, "invalid delta")
        if not self._may_write(key):
            return self._reject(key, "not allowed")
        current = world.state.get(key, 0)
        if not isinstance(current, int) or isinstance(current, bool):
            return self._reject(key, "not a counter")
        result = max(cfg.counter_min, min(cfg.counter_max, current + delta))
        if result != current or key not in world.state:
            reason = world.check_set(key, result)
            if reason:
                return self._reject(key, reason)
            world.set(key, result)
        applied = result - current
        self.server.store.log_ledger(
            self.name, key, delta, applied, result, clean_text(msg.get("reason"), 64), cfg.ledger_keep
        )
        if cid is not None:
            # "rev": the world revision that contains this booking. The ack arrives at once, the matching
            # "state" delta only on the next tick, so a client must keep counting the booking as "on its way"
            # until it has seen a state delta with rev >= this one.
            self.send({"t": "state_ack", "cid": cid, "key": key, "value": result, "applied": applied, "rev": world.rev})

    # shared conversations ------------------------------------------------------
    def _on_join_world(self, msg: dict[str, Any]) -> None:
        from .saves import SLOT_RE

        slot = msg.get("slot")
        if slot is not None and not (isinstance(slot, str) and SLOT_RE.fullmatch(slot)):
            return self._strike("bad world name")
        if slot != self.world:
            if self.world is not None:
                old = self.world
                self.server.dialogues.player_left(self)
                self.server.clocks.player_left(self, old)
                self.server.authority.player_left(self, old)
                self.world = None
                log.info("%s left world %s", self.label, old)
            self.world = slot
            if slot is not None:
                log.info("%s joined world %s", self.label, slot)
                self.server.clocks.on_join(self)
                self.server.authority.on_join(self)
                self.send({"t": "pdata", "world": slot, "data": self.server.store.player_data(self.name, slot)})

    def _dlg_ok(self) -> bool:
        if self.world is None:
            self.send({"t": "dlg_reject", "round": None, "reason": "not in a world"})
            return False
        if not self._dlg_bucket.take():
            self._strike("dialogue flood")
            return False
        return True

    def _time_ok(self) -> bool:
        if self.world is None:
            self.send({"t": "time_reject", "reason": "not in a world"})
            return False
        if not self._dlg_bucket.take():
            self._strike("time flood")
            return False
        return True

    def _time_reply(self, reason: str | None) -> None:
        if reason:
            self.send({"t": "time_reject", "reason": reason})

    def _on_time_init(self, msg: dict[str, Any]) -> None:
        if self._time_ok():
            self._time_reply(self.server.clocks.init(self, msg.get("seconds"), msg.get("rate")))

    def _on_time_pause(self, msg: dict[str, Any]) -> None:
        if self._time_ok():
            self._time_reply(self.server.clocks.pause(self, msg.get("on"), msg.get("reason")))

    def _on_time_jump(self, msg: dict[str, Any]) -> None:
        if self._time_ok():
            self._time_reply(self.server.clocks.jump(self, msg.get("to")))

    def _on_dlg_start(self, msg: dict[str, Any]) -> None:
        if not self._dlg_ok():
            return
        tree, person, prev = clean_text(msg.get("tree"), 80), clean_text(msg.get("person"), 80), clean_text(msg.get("prev"), 80)
        if tree is None:
            return self._strike("bad dialogue start")
        self.server.dialogues.start(self, tree, person, prev)

    def _on_dlg_vote(self, msg: dict[str, Any]) -> None:
        if not self._dlg_ok():
            return
        round_id, choice, options = clean_text(msg.get("round"), 200), msg.get("choice"), msg.get("options")
        if (round_id is None or not isinstance(choice, int) or isinstance(choice, bool)
                or not isinstance(options, int) or isinstance(options, bool)
                or not 2 <= options <= 16 or not 0 <= choice < options):
            self.send({"t": "dlg_reject", "round": round_id, "reason": "invalid vote"})
            return
        reason = self.server.dialogues.vote(self, round_id, choice, options)
        if reason:
            self.send({"t": "dlg_reject", "round": round_id, "reason": reason})

    # server-held savegames ----------------------------------------------------
    def _save_ok(self) -> bool:
        if self._save_bucket.take():
            return True
        self._strike("save flood")
        return False

    def _on_save_put(self, msg: dict[str, Any]) -> None:
        if self._save_ok():
            self.server.saves.begin(self, msg)

    def _on_save_data(self, msg: dict[str, Any]) -> None:
        if self._save_ok():
            self.server.saves.data(self, msg)

    def _on_save_end(self, msg: dict[str, Any]) -> None:
        if self._save_ok():
            self.server.saves.end(self, msg)

    def _on_save_list(self, msg: dict[str, Any]) -> None:
        if self._save_ok():
            self.send({"t": "save_list", "saves": self.server.saves.listing()})

    def _on_save_get(self, msg: dict[str, Any]) -> None:
        if not self._save_ok():
            return
        if self._download_task is not None and not self._download_task.done():
            self.server.saves._reject(self, msg.get("slot"), "a download is already running")
            return
        self._download_task = asyncio.create_task(self._run_download(msg))

    async def _run_download(self, msg: dict[str, Any]) -> None:
        try:
            await self.server.saves.send(self, msg)
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        except Exception:
            log.exception("download failed for %s", self.label)

    def _on_event(self, msg: dict[str, Any]) -> None:
        if not self._event_bucket.take():
            self._strike("event flood")
            return
        kind, data = msg.get("kind"), msg.get("data")
        if not isinstance(kind, str) or not KIND_RE.fullmatch(kind) or json_size(data) > 32 * 1024:
            self._strike("bad event")
            return
        packet = {"t": "event", "from": self.id, "kind": kind, "data": data}
        target = msg.get("to")
        if target is None:
            self.server.broadcast(packet, exclude=self)
        elif isinstance(target, int) and not isinstance(target, bool):
            peer = self.server.sessions.get(target)
            if peer is not None:
                peer.send(packet)

    # avatar ----------------------------------------------------------------------
    def _on_avatar_set(self, msg: dict[str, Any]) -> None:
        """The game model this player is shown as. Kept with the player, so it is theirs at every login."""
        if not self._chat_bucket.take():
            return self._strike("avatar flood")
        avatar = msg.get("avatar")
        if not (isinstance(avatar, str) and AVATAR_RE.fullmatch(avatar)):
            self.send({"t": "avatar_reject", "reason": "bad avatar name"})
            return
        self.avatar = avatar
        self.server.store.update(self.name, avatar=avatar)
        log.info("%s is shown as %s", self.label, avatar)
        self.server.broadcast({"t": "player_avatar", "id": self.id, "avatar": avatar})

    # per player data -------------------------------------------------------------
    def _on_pdata_set(self, msg: dict[str, Any]) -> None:
        """Something that is this player's own in their world (their inventory): kept by the server, not in the save."""
        if not self._state_bucket.take():
            return self._strike("pdata flood")
        key = msg.get("key")
        if self.world is None or not (isinstance(key, str) and PDATA_KEY_RE.fullmatch(key)):
            self.send({"t": "pdata_reject", "reason": "not in a world" if self.world is None else "bad key"})
            return
        if json_size(msg.get("value")) > self.server.cfg.max_player_data_bytes:
            self.send({"t": "pdata_reject", "reason": "too big"})
            return
        self.server.store.set_player_data(self.name, self.world, key, msg.get("value"))

    # session leader ------------------------------------------------------------
    def _on_npc_state(self, msg: dict[str, Any]) -> None:
        if not self._npc_bucket.take():
            return self._strike("npc flood")
        reason = self.server.authority.npc_state(self, msg)
        if reason is not None:
            self.send({"t": "npc_reject", "reason": reason})

    def _on_npc_act(self, msg: dict[str, Any]) -> None:
        if not self._npc_bucket.take():
            return self._strike("npc flood")
        reason = self.server.authority.npc_act(self, msg)
        if reason is not None:
            self.send({"t": "npc_reject", "reason": reason})

    def _on_bye(self, msg: dict[str, Any]) -> bool:
        return False
