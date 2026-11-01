from test_server import ServerCase


class PlayerDataTests(ServerCase):
    async def enter(self, name, world="main", **kw):
        client = await self.connect()
        msg = await client.login(name, **kw)
        self.assertEqual(msg["t"], "login_ok", msg)
        client.send({"t": "join_world", "slot": world})
        got = await client.recv_type("pdata")
        self.assertEqual(got["world"], world)
        return client, got["data"]

    async def test_each_player_keeps_their_own_inventory_per_world(self):
        await self.start()
        a, data = await self.enter("Alice")
        self.assertEqual(data, {})
        a.send({"t": "pdata_set", "key": "inv", "value": [["Apple", 3], ["Knife", 1]]})
        b, data_b = await self.enter("Bob")
        self.assertEqual(data_b, {})                              # Bob does not get Alice's things
        await a.close()
        a2, again = await self.enter("Alice", token=a.token)
        self.assertEqual(again, {"inv": [["Apple", 3], ["Knife", 1]]})
        a2.send({"t": "join_world", "slot": "other"})
        other = await a2.recv_type("pdata")
        self.assertEqual((other["world"], other["data"]), ("other", {}))   # every world has its own

    async def test_outside_a_world_or_with_a_bad_key_it_is_refused(self):
        await self.start()
        c = await self.connect()
        await c.login("Cleo")
        c.send({"t": "pdata_set", "key": "inv", "value": []})
        self.assertEqual((await c.recv_type("pdata_reject"))["reason"], "not in a world")
        c.send({"t": "join_world", "slot": "main"})
        await c.recv_type("pdata")
        c.send({"t": "pdata_set", "key": "../x", "value": 1})
        self.assertEqual((await c.recv_type("pdata_reject"))["reason"], "bad key")


class OldModTests(ServerCase):
    async def test_an_old_mod_is_told_in_the_chat(self):
        await self.start(recommended_mod="0.5.0")
        from nivalis_coop.session import _older_mod
        self.assertEqual(_older_mod("NivalisCoop/0.1.0", "0.5.0"), "0.1.0")
        self.assertIsNone(_older_mod("NivalisCoop/0.5.0", "0.5.0"))
        self.assertIsNone(_older_mod("python-bot", "0.5.0"))
