from test_server import ServerCase


class AvatarTests(ServerCase):
    async def enter(self, name, **kw):
        client = await self.connect()
        msg = await client.login(name, **kw)
        self.assertEqual(msg["t"], "login_ok", msg)
        client.login_msg = msg
        return client

    async def test_the_first_login_has_no_avatar_and_a_chosen_one_is_kept_and_shared(self):
        await self.start()
        a = await self.enter("Alice")
        self.assertIsNone(a.login_msg["avatar"])
        b = await self.enter("Bob")
        a.send({"t": "avatar_set", "avatar": "NPC_Female_Punk_03"})
        got = await b.recv_type("player_avatar")
        self.assertEqual((got["id"], got["avatar"]), (a.id, "NPC_Female_Punk_03"))
        await a.close()
        again = await self.enter("Alice", token=a.token)
        self.assertEqual(again.login_msg["avatar"], "NPC_Female_Punk_03")       # it is hers at every login
        joined = await b.recv_type("player_join")
        self.assertEqual(joined["avatar"], "NPC_Female_Punk_03")
        c = await self.enter("Cleo")
        listed = {p["name"]: p["avatar"] for p in c.login_msg["players"]}
        self.assertEqual(listed["Alice"], "NPC_Female_Punk_03")

    async def test_bad_names_are_refused(self):
        await self.start()
        a = await self.enter("Alice")
        a.send({"t": "avatar_set", "avatar": "../../etc/passwd"})
        got = await a.recv_type("avatar_reject")
        self.assertEqual(got["reason"], "bad avatar name")
