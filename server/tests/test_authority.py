import asyncio

from test_server import ServerCase


def person(i, x=0.0):
    return {"id": f"p{i}", "pos": [x, 0.0, 0.0], "ry": 0.0, "anim": "idle"}


class SessionLeaderTests(ServerCase):
    async def enter(self, name, world="main"):
        client = await self.join(name)
        client.send({"t": "join_world", "slot": world})
        return client

    async def test_the_first_player_in_a_world_leads_and_the_next_one_is_told(self):
        await self.start()
        a = await self.enter("Alice")
        got = await a.recv_type("leader")
        self.assertEqual((got["world"], got["name"], got["handover"]), ("main", "Alice", False))
        b = await self.enter("Bob")
        got = await b.recv_type("leader")
        self.assertEqual(got["name"], "Alice")

    async def test_snapshots_of_the_leader_reach_the_others_but_not_from_others(self):
        await self.start()
        a = await self.enter("Alice")
        await a.recv_type("leader")
        b = await self.enter("Bob")
        await b.recv_type("leader")
        a.send({"t": "npc_state", "tick": 1, "npcs": [person(1), person(2)]})
        got = await b.recv_type("npc_state")
        self.assertEqual([n["id"] for n in got["npcs"]], ["p1", "p2"])

        b.send({"t": "npc_state", "tick": 1, "npcs": [person(9)]})   # Bob does not simulate
        refused = await b.recv_type("npc_reject")
        self.assertEqual(refused["reason"], "not the session leader")

    async def test_a_late_player_gets_the_last_snapshot_with_partial_updates_merged(self):
        await self.start()
        a = await self.enter("Alice")
        await a.recv_type("leader")
        a.send({"t": "npc_state", "tick": 1, "npcs": [person(1), person(2)]})
        a.send({"t": "npc_state", "tick": 2, "full": False, "npcs": [person(2, x=5.0), {"id": "p1", "gone": True}, person(3)]})
        await asyncio.sleep(0.2)
        c = await self.enter("Cleo")
        await c.recv_type("leader")
        snap = await c.recv_type("npc_state")
        by_id = {n["id"]: n for n in snap["npcs"]}
        self.assertEqual(sorted(by_id), ["p2", "p3"])
        self.assertEqual(by_id["p2"]["pos"][0], 5.0)

    async def test_when_the_leader_goes_the_longest_present_player_takes_over_with_the_snapshot(self):
        await self.start()
        a = await self.enter("Alice")
        await a.recv_type("leader")
        b = await self.enter("Bob")
        await b.recv_type("leader")
        c = await self.enter("Cleo")
        await c.recv_type("leader")
        a.send({"t": "npc_state", "tick": 7, "npcs": [person(1)]})
        await b.recv_type("npc_state")
        await a.close()
        got = await b.recv_type("leader")
        self.assertEqual((got["name"], got["handover"]), ("Bob", True))
        snap = await b.recv_type("npc_state")                       # the new leader starts from here
        self.assertEqual(snap["tick"], 7)
        seen = await c.recv_type("leader")
        self.assertEqual(seen["name"], "Bob")
        b.send({"t": "npc_state", "tick": 8, "npcs": [person(1, x=1.0)]})
        got = await c.recv_type("npc_state")
        self.assertEqual(got["tick"], 8)

    async def test_actions_of_others_go_to_the_leader_only(self):
        await self.start()
        a = await self.enter("Alice")
        await a.recv_type("leader")
        b = await self.enter("Bob")
        await b.recv_type("leader")
        b.send({"t": "npc_act", "npc": "p1", "action": "talk", "data": {"x": 1}})
        got = await a.recv_type("npc_act")
        self.assertEqual((got["npc"], got["action"], got["from"]), ("p1", "talk", b.id))

    async def test_worlds_have_their_own_leaders(self):
        await self.start()
        a = await self.enter("Alice", "main")
        await a.recv_type("leader")
        b = await self.enter("Bob", "other")
        got = await b.recv_type("leader")
        self.assertEqual(got["name"], "Bob")
        a.send({"t": "npc_state", "tick": 1, "npcs": [person(1)]})
        with self.assertRaises(asyncio.TimeoutError):
            await b.recv_type("npc_state", timeout=0.4)

    async def test_oversized_snapshots_are_refused(self):
        await self.start(max_npcs=3)
        a = await self.enter("Alice")
        await a.recv_type("leader")
        a.send({"t": "npc_state", "tick": 1, "npcs": [person(i) for i in range(4)]})
        got = await a.recv_type("npc_reject")
        self.assertEqual(got["reason"], "too many npcs")
