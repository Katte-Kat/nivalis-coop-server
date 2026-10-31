"""Operator commands, reachable three ways: the admin socket (``ctl``, like
Minecraft's RCON but local only), in-game chat for ops (``/kick x``), and the
same executor for both."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import socket
import time
from typing import TYPE_CHECKING, Callable

from .protocol import valid_key, valid_name

if TYPE_CHECKING:
    from .server import CoopServer
    from .session import Session


class UsageError(Exception):
    pass


class Commands:
    def __init__(self, server: "CoopServer") -> None:
        self.server = server

    def execute(self, line: str, actor: "Session | None" = None) -> list[str]:
        """Run one command. ``actor`` is None for the console, else the op who typed it."""
        head = line.split(None, 3)
        if len(head) >= 2 and head[0].lower() == "state" and head[1].lower() == "set":
            parts = head  # the value is raw JSON: shlex would eat its quotes and split at its spaces
        else:
            try:
                parts = shlex.split(line)
            except ValueError as exc:
                return [f"parse error: {exc}"]
        if not parts:
            return []
        name, args = parts[0].lower(), parts[1:]
        fn: Callable[[list[str], "Session | None"], list[str]] | None = getattr(self, f"cmd_{name}", None)
        if fn is None:
            return [f"Unknown command {name!r}. Try 'help'."]
        try:
            return fn(args, actor)
        except UsageError as exc:
            return [f"usage: {exc}"]

    # helpers -------------------------------------------------------------
    def _online(self, name: str) -> "Session | None":
        return self.server.by_name.get(name.casefold())

    @staticmethod
    def _need(args: list[str], count: int, usage: str) -> None:
        if len(args) < count:
            raise UsageError(usage)

    # commands ------------------------------------------------------------
    def cmd_help(self, args, actor):
        return [
            "help | status | list | say <text> | save | stop (console only)",
            "kick <name> [reason] | ban <name> [reason] | pardon <name> | op <name> | deop <name>",
            "whitelist on|off|list|add <name>|remove <name>",
            "state list [prefix] | state get <key> | state set <key> <json> | state del <key>",
            "ledger [key] [count]   (latest bookings of shared counters)",
            "saves   (versions of the server-held savegames)",
        ]

    def cmd_status(self, args, actor):
        st = self.server.status()
        return [
            f"{st['name']} v{st['version']} - {st['online']}/{st['max']} players, rev {st['rev']}",
            f"tick {self.server.tick_n}: {st['tps']} tps, {st['mspt']} ms/tick, up {st['uptime']}s",
            f"password: {st['password']}, whitelist: {st['whitelist']}",
        ]

    def cmd_list(self, args, actor):
        rows = [
            f"{s.id:>3} {s.name} ping={s.ping_ms if s.ping_ms is not None else '-'}ms{' op' if s.is_op else ''} "
            f"at ({s.pose[0]:.1f}, {s.pose[1]:.1f}, {s.pose[2]:.1f}) {s.pose[5]}"
            for s in self.server.sessions.values()
        ]
        return rows or ["nobody is online"]

    def cmd_say(self, args, actor):
        self._need(args, 1, "say <text>")
        self.server.broadcast({"t": "chat", "id": 0, "name": "Server", "text": " ".join(args)[:256]})
        return []

    def cmd_kick(self, args, actor):
        self._need(args, 1, "kick <name> [reason]")
        target = self._online(args[0])
        if target is None:
            return [f"{args[0]} is not online"]
        target.kick(" ".join(args[1:]) or "Kicked by an operator")
        return [f"kicked {target.name}"]

    def cmd_ban(self, args, actor):
        self._need(args, 1, "ban <name> [reason]")
        if not valid_name(args[0]):
            return ["invalid name"]
        reason = " ".join(args[1:]) or None
        self.server.store.update(args[0], banned=1, ban_reason=reason)
        target = self._online(args[0])
        if target is not None:
            target.kick("You are banned" + (f": {reason}" if reason else ""))
        return [f"banned {args[0]}"]

    def cmd_pardon(self, args, actor):
        self._need(args, 1, "pardon <name>")
        self.server.store.update(args[0], banned=0, ban_reason=None)
        return [f"pardoned {args[0]}"]

    def _set_op(self, args, value: int):
        self._need(args, 1, "op|deop <name>")
        if not valid_name(args[0]):
            return ["invalid name"]
        self.server.store.update(args[0], op=value)
        target = self._online(args[0])
        if target is not None:
            target.is_op = bool(value)
        return [f"{args[0]} is {'now' if value else 'no longer'} an operator"]

    def cmd_op(self, args, actor):
        return self._set_op(args, 1)

    def cmd_deop(self, args, actor):
        return self._set_op(args, 0)

    def cmd_whitelist(self, args, actor):
        self._need(args, 1, "whitelist on|off|list|add <name>|remove <name>")
        sub, srv = args[0].lower(), self.server
        if sub in ("on", "off"):
            srv.whitelist_on = sub == "on"
            srv.store.set_meta("whitelist", "1" if srv.whitelist_on else "0")
            return [f"whitelist is {sub}"]
        if sub == "list":
            names = [p["name"] for p in srv.store.players() if p["whitelisted"]]
            return [", ".join(names) or "whitelist is empty"]
        if sub in ("add", "remove"):
            self._need(args, 2, f"whitelist {sub} <name>")
            if not valid_name(args[1]):
                return ["invalid name"]
            srv.store.update(args[1], whitelisted=1 if sub == "add" else 0)
            return [f"{args[1]} {'added to' if sub == 'add' else 'removed from'} the whitelist"]
        raise UsageError("whitelist on|off|list|add <name>|remove <name>")

    def cmd_ledger(self, args, actor):
        """ledger [key] [count]: the latest bookings of shared counters (who added how much)."""
        key = args[0] if args and not args[0].isdigit() else None
        count = int(args[-1]) if args and args[-1].isdigit() else 15
        rows = self.server.store.ledger_tail(key, max(1, min(count, 200)))
        if not rows:
            return ["no bookings yet"]
        return [
            f"{time.strftime('%H:%M:%S', time.localtime(r['ts']))} {r['player']:<14} {r['key']} "
            f"{r['delta']:+d}" + (f" (applied {r['applied']:+d})" if r["applied"] != r["delta"] else "")
            + f" -> {r['result']}" + (f"  [{r['reason']}]" if r["reason"] else "")
            for r in rows
        ]

    def cmd_saves(self, args, actor):
        """saves: every stored version of every server-held savegame."""
        rows = self.server.store.all_saves()
        if not rows:
            return ["no server savegames yet"]
        return [
            f"{r['slot']:<12} v{r['version']:<3} {r['size'] / 1e6:7.2f} MB  {time.strftime('%F %T', time.localtime(r['ts']))}  "
            f"by {r['uploader']}" + (f"  [{r['note']}]" if r["note"] else "") + f"  {r['sha256'][:12]}"
            for r in rows
        ]

    def cmd_save(self, args, actor):
        return [f"saved {self.server.world.save()} world keys"]

    def cmd_stop(self, args, actor):
        if actor is not None:
            return ["stop is only available on the console"]
        self.server.stop_event.set()
        return ["stopping"]

    def cmd_state(self, args, actor):
        self._need(args, 1, "state list|get|set|del ...")
        world, sub = self.server.world, args[0].lower()
        if sub == "list":
            prefix = args[1] if len(args) > 1 else ""
            keys = sorted(k for k in world.state if k.startswith(prefix))
            return [f"{len(keys)} keys (rev {world.rev})"] + keys[:50]
        self._need(args, 2, f"state {sub} <key>" + (" <json>" if sub == "set" else ""))
        key = args[1]
        if not valid_key(key):
            return ["invalid key"]
        if sub == "get":
            return [json.dumps(world.state[key])] if key in world.state else ["(not set)"]
        if sub == "del":
            return [f"deleted, rev {world.delete(key)}"] if key in world.state else ["(not set)"]
        if sub == "set":
            self._need(args, 3, "state set <key> <json>")
            try:
                value = json.loads(args[2])
            except ValueError:
                return ["value is not valid JSON"]
            if value is None:
                return ["null is not a value, use 'state del'"]
            reason = world.check_set(key, value)
            return [reason] if reason else [f"set, rev {world.set(key, value)}"]
        raise UsageError("state list|get|set|del ...")


class AdminSocket:
    """Line-based Unix socket. Access = whoever can open the file (mode 0600)."""

    def __init__(self, path: str, commands: Commands) -> None:
        self.path = path
        self.commands = commands
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        if os.path.exists(self.path):
            os.unlink(self.path)  # left over from a crash
        self._server = await asyncio.start_unix_server(self._handle, path=self.path)
        os.chmod(self.path, 0o600)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        if os.path.exists(self.path):
            os.unlink(self.path)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            out = self.commands.execute(line.decode("utf-8", "replace").strip())
            writer.write(("\n".join(out) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()


def ctl(path: str, line: str, timeout: float = 5.0) -> str:
    """Send one command to a running server's admin socket (blocking)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall(line.encode("utf-8") + b"\n")
        chunks = []
        while chunk := sock.recv(4096):
            chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", "replace")
