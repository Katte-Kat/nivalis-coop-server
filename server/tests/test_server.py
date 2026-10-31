import asyncio
import struct
import tempfile
import unittest

from nivalis_coop.admin import ctl
from nivalis_coop.client import Client, status
from nivalis_coop.config import Config
from nivalis_coop.server import CoopServer


class ServerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.clients: list[Client] = []
        self.server: CoopServer | None = None

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        if self.server:
            await self.server.stop()
        self._tmp.cleanup()

    async def start(self, **overrides) -> CoopServer:
        cfg = Config(host="127.0.0.1", port=0, data_dir=self._tmp.name, tick_rate=50, **overrides)
        self.server = CoopServer(cfg)
        await self.server.start()
        return self.server

    async def connect(self, auto_ack: bool = True) -> Client:
        client = await Client.open("127.0.0.1", self.server.port, auto_ack)
        self.clients.append(client)
        return client

    async def join(self, name: str, **kw) -> Client:
        client = await self.connect()
        msg = await client.login(name, **kw)
        self.assertEqual(msg["t"], "login_ok", msg)
        return client


class StatusAndLogin(ServerCase):
    async def test_status_ping(self):
        await self.start(name="Test", max_players=3, password="pw")
        st = await status("127.0.0.1", self.server.port)
        self.assertEqual((st["name"], st["max"], st["online"], st["proto"]), ("Test", 3, 0, 1))
        self.assertTrue(st["password"])

    async def test_first_login_issues_token_and_protects_the_name(self):
        await self.start()
        a = await self.connect()
        ok = await a.login("Kat")
        self.assertEqual(ok["t"], "login_ok")
        token = ok["token"]
        impostor = await self.connect()
        res = await impostor.login("kat")
        self.assertEqual(res["t"], "kick")
        self.assertIn("belongs to another player", res["reason"])
        await a.close()
        again = await self.connect()
        ok2 = await again.login("Kat", token=token)
        self.assertEqual(ok2["t"], "login_ok")
        self.assertNotIn("token", ok2)

    async def test_protocol_mismatch(self):
        await self.start()
        res = await (await self.connect()).login("Kat", proto=99)
        self.assertEqual(res["t"], "kick")
        self.assertIn("mismatch", res["reason"])

    async def test_password(self):
        await self.start(password="secret")
        self.assertIn("password", (await (await self.connect()).login("Kat"))["reason"])
        self.assertIn("password", (await (await self.connect()).login("Kat", password="nope"))["reason"])
        self.assertEqual((await (await self.connect()).login("Kat", password="secret"))["t"], "login_ok")

    async def test_whitelist_and_ban(self):
        srv = await self.start(whitelist=True)
        self.assertIn("whitelist", (await (await self.connect()).login("Kat"))["reason"])
        srv.commands.execute("whitelist add Kat")
        kat = await self.join("Kat")
        srv.commands.execute("ban Kat cheating")
        self.assertIn("banned", (await kat.recv_type("kick"))["reason"])
        res = await (await self.connect()).login("Kat", token=kat.token)
        self.assertIn("cheating", res["reason"])

    async def test_server_full(self):
        await self.start(max_players=1)
        await self.join("Alice")
        res = await (await self.connect()).login("Bob")
        self.assertIn("full", res["reason"])

    async def test_second_login_replaces_the_first(self):
        await self.start()
        first = await self.join("Kat")
        second = await self.join("Kat", token=first.token)
        self.assertIn("another location", (await first.recv_type("kick"))["reason"])
        self.assertEqual(len(self.server.sessions), 1)
        self.assertIsNotNone(second.id)

    async def test_invalid_name_and_garbage_before_login(self):
        await self.start()
        self.assertIn("Invalid name", (await (await self.connect()).login("<script>"))["reason"])
        reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
        writer.write(struct.pack(">I", 10_000_000))
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), 2)  # kick packet, then EOF
        self.assertIn(b"Protocol error", data)
        writer.close()


class Play(ServerCase):
    async def test_movement_reaches_the_others_but_not_the_sender(self):
        await self.start()
        a, b = await self.join("Alice"), await self.join("Bob")
        self.assertEqual((await a.recv_type("player_join"))["name"], "Bob")
        a.send({"t": "move", "x": 1, "y": 2, "z": 3, "ry": 90, "anim": "walk", "scene": "cafe"})
        tick = await b.recv_type("tick")
        self.assertEqual(tick["p"][str(a.id)], [1.0, 2.0, 3.0, 90.0, "walk", "cafe"])
        b.send({"t": "move", "x": 0, "y": 0, "z": 0, "ry": 0})
        tick_a = await a.recv_type("tick")
        self.assertNotIn(str(a.id), tick_a["p"])
        self.assertEqual(tick_a["p"][str(b.id)][4], "idle")

    async def test_bad_moves_are_ignored(self):
        await self.start()
        a, b = await self.join("Alice"), await self.join("Bob")
        a.send({"t": "move", "x": "NaN", "y": 0, "z": 0, "ry": 0})
        a.send({"t": "move", "x": 1e12, "y": 0, "z": 0, "ry": 0})
        with self.assertRaises(TimeoutError):
            await b.recv_type("tick", timeout=0.3)

    async def test_late_joiner_sees_players_and_leave_is_announced(self):
        await self.start()
        a = await self.join("Alice")
        a.send({"t": "move", "x": 5, "y": 0, "z": 5, "ry": 0})
        await asyncio.sleep(0.1)
        b = await self.connect()
        ok = await b.login("Bob")
        self.assertEqual([p["name"] for p in ok["players"]], ["Alice"])
        self.assertEqual(ok["players"][0]["pose"][0], 5.0)
        b.send({"t": "bye"})
        self.assertEqual((await a.recv_type("player_leave"))["name"], "Bob")

    async def test_chat_and_operator_commands(self):
        srv = await self.start()
        a, b = await self.join("Alice"), await self.join("Bob")
        a.send({"t": "chat", "text": "hello  there"})
        for client in (a, b):
            msg = await client.recv_type("chat")
            self.assertEqual((msg["name"], msg["text"]), ("Alice", "hello  there"))
        a.send({"t": "chat", "text": "/say nope"})
        self.assertIn("permission", (await a.recv_type("chat"))["text"])
        srv.commands.execute("op Alice")
        a.send({"t": "chat", "text": "/say closing time"})
        for client in (a, b):  # /say reaches everybody, the sender included
            self.assertEqual((await client.recv_type("chat"))["text"], "closing time")
        a.send({"t": "chat", "text": "/stop"})
        self.assertIn("console", (await a.recv_type("chat"))["text"])
        a.send({"t": "chat", "text": "/kick Bob out you go"})
        self.assertEqual((await b.recv_type("kick"))["reason"], "out you go")

    async def test_events(self):
        await self.start()
        a, b, c = await self.join("Alice"), await self.join("Bob"), await self.join("Carol")
        a.send({"t": "event", "kind": "trade.request", "data": {"item": "tray"}, "to": b.id})
        msg = await b.recv_type("event")
        self.assertEqual((msg["from"], msg["kind"], msg["data"]), (a.id, "trade.request", {"item": "tray"}))
        with self.assertRaises(TimeoutError):
            await c.recv_type("event", timeout=0.3)
        a.send({"t": "event", "kind": "wave", "data": None})
        self.assertEqual((await c.recv_type("event"))["kind"], "wave")
        self.assertEqual((await b.recv_type("event"))["kind"], "wave")  # broadcast reaches everybody but the sender
        a.send({"t": "event", "kind": "Bad Kind!", "data": 1})
        with self.assertRaises(TimeoutError):
            await b.recv_type("event", timeout=0.3)

    async def test_flooding_gets_kicked(self):
        await self.start()
        a = await self.join("Alice")
        for i in range(300):
            a.send({"t": "state_set", "key": "world/x", "value": i})
        self.assertIn("Flooding", (await a.recv_type("kick", timeout=3))["reason"])


class SharedState(ServerCase):
    async def test_set_delete_reject_and_compare_and_swap(self):
        await self.start()
        a, b = await self.join("Alice"), await self.join("Bob")
        a.send({"t": "state_set", "key": "world/time", "value": {"h": 9}})
        delta = await b.recv_type("state")
        self.assertEqual(delta["set"], {"world/time": {"h": 9}})

        a.send({"t": "state_set", "key": "admin/secret", "value": 1})
        self.assertEqual((await a.recv_type("state_reject"))["reason"], "not allowed")
        a.send({"t": "state_set", "key": "world/x", "value": None})
        self.assertIn("value missing", (await a.recv_type("state_reject"))["reason"])

        # two players race for the same order: the first one wins, the second gets the winner back
        a.send({"t": "state_set", "key": "world/order/1", "value": "Alice", "expect": None})
        b.send({"t": "state_set", "key": "world/order/1", "value": "Bob", "expect": None})
        rej = await b.recv_type("state_reject")
        self.assertEqual((rej["reason"], rej["value"]), ("conflict", "Alice"))
        self.assertEqual(self.server.world.state["world/order/1"], "Alice")

        a.send({"t": "state_del", "key": "world/order/1", "expect": "Alice"})
        while "world/order/1" not in (d := await b.recv_type("state"))["del"]:
            pass
        self.assertNotIn("world/order/1", self.server.world.state)

    async def test_world_survives_a_restart(self):
        await self.start()
        a = await self.join("Alice")
        a.send({"t": "state_set", "key": "world/day", "value": 7})
        await a.recv_type("state")
        token = a.token
        await a.close()
        await self.server.stop()
        self.server = None

        await self.start()
        ok = await (await self.connect()).login("Alice", token=token)
        self.assertEqual(ok["t"], "login_ok")
        self.assertEqual(ok["state"], {"world/day": 7})
        self.assertGreaterEqual(ok["rev"], 1)

    async def test_late_joiner_gets_the_snapshot(self):
        srv = await self.start()
        srv.commands.execute("state set world/music \"jazz\"")
        ok = await (await self.connect()).login("Bob")
        self.assertEqual(ok["state"], {"world/music": "jazz"})


class Shutdown(ServerCase):
    async def test_stop_is_quick_even_if_a_client_never_answers(self):
        import time
        srv = await self.start()
        quiet = await self.join("Alice")
        quiet.writer.transport.pause_reading()   # stops reading, so it neither answers nor hangs up
        await self.join("Bob")
        began = time.monotonic()
        await srv.stop()
        self.server = None
        self.assertLess(time.monotonic() - began, 4.0)


class SharedAccount(ServerCase):
    async def test_adds_from_two_players_are_summed_not_overwritten(self):
        srv = await self.start()
        a, b = await self.join("Alice"), await self.join("Bob")
        a.send({"t": "state_add", "key": "shared/bank/cash", "delta": 100, "cid": 1, "reason": "sold lunch"})
        b.send({"t": "state_add", "key": "shared/bank/cash", "delta": -30, "cid": 7})
        ack_a, ack_b = await a.recv_type("state_ack"), await b.recv_type("state_ack")
        self.assertEqual((ack_a["cid"], ack_a["applied"]), (1, 100))
        self.assertGreaterEqual(ack_b["rev"], ack_a["rev"])  # the revision that contains the booking
        self.assertEqual(ack_b["rev"], srv.world.rev)
        self.assertEqual((ack_b["cid"], ack_b["applied"], ack_b["value"]), (7, -30, 70))
        self.assertEqual(srv.world.state["shared/bank/cash"], 70)
        for client in (a, b):  # everybody converges on the total through the normal state delta
            delta = await client.recv_type("state")
            while delta["set"].get("shared/bank/cash") != 70:
                delta = await client.recv_type("state")
        book = srv.commands.execute("ledger shared/bank/cash")
        self.assertEqual(len(book), 2)
        self.assertIn("Alice", book[0])
        self.assertIn("+100", book[0])
        self.assertIn("[sold lunch]", book[0])
        self.assertIn("Bob", book[1])
        self.assertIn("-30 -> 70", book[1])

    async def test_balance_never_goes_below_the_minimum(self):
        await self.start()
        a = await self.join("Alice")
        a.send({"t": "state_add", "key": "shared/bank/cash", "delta": 40, "cid": 1})
        await a.recv_type("state_ack")
        a.send({"t": "state_add", "key": "shared/bank/cash", "delta": -100, "cid": 2})
        ack = await a.recv_type("state_ack")
        self.assertEqual((ack["value"], ack["applied"]), (0, -40))  # clamped: only 40 was there to take
        self.assertIn("(applied -40)", self.server.commands.execute("ledger")[-1])

    async def test_bad_adds_are_refused(self):
        await self.start()
        a = await self.join("Alice")
        a.send({"t": "state_set", "key": "shared/name", "value": "text"})
        await a.recv_type("state")
        cases = [
            ({"key": "shared/bank/cash", "delta": 1.5}, "invalid delta"),
            ({"key": "shared/bank/cash", "delta": True}, "invalid delta"),
            ({"key": "shared/bank/cash", "delta": 10**12}, "invalid delta"),
            ({"key": "admin/bank", "delta": 5}, "not allowed"),
            ({"key": "shared/name", "delta": 5}, "not a counter"),
            ({"key": "Shared/Bad Key", "delta": 5}, "invalid key"),
        ]
        for fields, reason in cases:
            a.send({"t": "state_add", **fields})
            self.assertEqual((await a.recv_type("state_reject"))["reason"], reason, fields)
        self.assertNotIn("shared/bank/cash", self.server.world.state)

    async def test_balance_and_bookings_survive_a_restart(self):
        await self.start()
        a = await self.join("Alice")
        a.send({"t": "state_add", "key": "shared/bank/cash", "delta": 250, "cid": 1, "reason": "day 1"})
        await a.recv_type("state_ack")
        token = a.token
        await a.close()
        await self.server.stop()
        self.server = None

        await self.start()
        ok = await (await self.connect()).login("Alice", token=token)
        self.assertEqual(ok["state"]["shared/bank/cash"], 250)
        self.assertIn("[day 1]", self.server.commands.execute("ledger")[0])


class Keepalive(ServerCase):
    async def test_silent_client_is_timed_out(self):
        await self.start(keepalive_interval=0.05, keepalive_timeout=0.3)
        ghost = await self.connect(auto_ack=False)
        self.assertEqual((await ghost.login("Ghost"))["t"], "login_ok")
        self.assertIn("Timed out", (await ghost.recv_type("kick", timeout=2))["reason"])

    async def test_answering_client_stays_and_ping_is_measured(self):
        await self.start(keepalive_interval=0.05, keepalive_timeout=0.3)
        a = await self.join("Alice")
        with self.assertRaises(TimeoutError):  # keeps acking while it waits
            await a.recv_type("chat", timeout=0.8)
        a.send({"t": "chat", "text": "still here"})
        self.assertEqual((await a.recv_type("chat"))["text"], "still here")
        self.assertIsNotNone(self.server.sessions[a.id].ping_ms)


class Admin(ServerCase):
    async def test_admin_socket(self):
        srv = await self.start()
        await self.join("Alice")
        out = await asyncio.to_thread(ctl, srv.cfg.admin_path, "list")
        self.assertIn("Alice", out)
        out = await asyncio.to_thread(ctl, srv.cfg.admin_path, "status")
        self.assertIn("1/8 players", out)
        out = await asyncio.to_thread(ctl, srv.cfg.admin_path, "state set world/a 1")
        self.assertIn("rev", out)
        self.assertEqual(srv.world.state["world/a"], 1)
        await asyncio.to_thread(ctl, srv.cfg.admin_path, "stop")
        self.assertTrue(srv.stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
