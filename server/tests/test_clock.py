import asyncio

from test_server import ServerCase

RATE = 200.0   # game seconds per real second: fast, so a short wait moves the clock visibly


async def latest(client, seconds):
    """The newest time packet after listening for ``seconds`` (the clock sends one every 0.1 s, so 'until nothing comes' never ends)."""
    packet = None
    end = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < end:
        packet = await client.recv_type("time", timeout=1.0)
    return packet


class WorldClockTests(ServerCase):
    async def start_clock(self, **config):
        await self.start(clock_broadcast_seconds=0.1, **config)

    async def enter(self, name, world="main"):
        client = await self.join(name)
        client.send({"t": "join_world", "slot": world})
        return client

    async def test_the_first_player_is_asked_for_the_time_and_the_second_just_gets_it(self):
        await self.start_clock()
        a = await self.enter("Alice")
        await a.recv_type("time_need")
        a.send({"t": "time_init", "seconds": 90000, "rate": RATE})
        first = await a.recv_type("time")
        self.assertAlmostEqual(first["seconds"], 90000, delta=200)
        self.assertEqual((first["rate"], first["paused"], first["jump"]), (RATE, False, False))

        b = await self.enter("Bob")
        got = await b.recv_type("time", "time_need")
        self.assertEqual(got["t"], "time")            # a world that has a clock never asks again
        self.assertGreaterEqual(got["seconds"], 90000)

    async def test_the_clock_runs_on_the_server_at_the_reported_speed(self):
        await self.start_clock()
        a = await self.enter("Alice")
        await a.recv_type("time_need")
        a.send({"t": "time_init", "seconds": 1000, "rate": RATE})
        t0 = await a.recv_type("time")
        later = await latest(a, 0.6)
        self.assertIsNotNone(later)
        moved = later["seconds"] - t0["seconds"]
        self.assertGreater(moved, RATE * 0.3)          # about 0.6 s * 200 = 120, allow slack
        self.assertLess(moved, RATE * 1.0)

    async def test_a_second_init_does_not_change_a_running_clock(self):
        await self.start_clock()
        a, b = await self.enter("Alice"), await self.enter("Bob")
        await a.recv_type("time_need")
        a.send({"t": "time_init", "seconds": 5000, "rate": RATE})
        await b.recv_type("time")
        b.send({"t": "time_init", "seconds": 999999, "rate": 1.0})   # Bob loaded another save: he follows, he does not set
        news = await b.recv_type("time")
        self.assertLess(news["seconds"], 50000)
        self.assertEqual(news["rate"], RATE)

    async def test_a_pause_stops_the_clock_for_everybody_and_ending_it_starts_it_again(self):
        await self.start_clock()
        a, b = await self.enter("Alice"), await self.enter("Bob")
        await a.recv_type("time_need")
        a.send({"t": "time_init", "seconds": 0, "rate": RATE})
        await b.recv_type("time")
        a.send({"t": "time_pause", "on": True, "reason": "dialogue"})
        paused = await b.recv_type("time")
        while not paused["paused"]:
            paused = await b.recv_type("time")
        self.assertEqual(paused["reasons"], ["dialogue"])
        frozen = paused["seconds"]
        still = await latest(b, 0.4)
        self.assertAlmostEqual(still["seconds"], frozen, delta=1.0)
        self.assertTrue(still["paused"])
        a.send({"t": "time_pause", "on": False, "reason": "dialogue"})
        running = await b.recv_type("time")
        while running["paused"]:
            running = await b.recv_type("time")
        later = await latest(b, 0.4)
        self.assertGreater(later["seconds"], frozen + 20)

    async def test_two_pauses_need_two_releases(self):
        await self.start_clock()
        a, b = await self.enter("Alice"), await self.enter("Bob")
        await a.recv_type("time_need")
        a.send({"t": "time_init", "seconds": 0, "rate": RATE})
        await b.recv_type("time")
        a.send({"t": "time_pause", "on": True, "reason": "dialogue"})
        b.send({"t": "time_pause", "on": True, "reason": "dialogue"})
        await asyncio.sleep(0.2)
        a.send({"t": "time_pause", "on": False, "reason": "dialogue"})
        packet = await latest(a, 0.5)
        self.assertTrue(packet["paused"])   # Bob's pause still holds it

    async def test_a_player_who_leaves_takes_their_pause_with_them(self):
        await self.start_clock()
        a, b = await self.enter("Alice"), await self.enter("Bob")
        await a.recv_type("time_need")
        a.send({"t": "time_init", "seconds": 0, "rate": RATE})
        await b.recv_type("time")
        b.send({"t": "time_pause", "on": True, "reason": "dialogue"})
        await asyncio.sleep(0.2)
        await b.close()
        packet = await a.recv_type("time")
        while packet["paused"]:
            packet = await a.recv_type("time", timeout=1.0)
        self.assertFalse(packet["paused"])

    async def test_a_pause_nobody_ends_runs_out(self):
        await self.start_clock(pause_timeout_seconds=0.5)
        a = await self.enter("Alice")
        await a.recv_type("time_need")
        a.send({"t": "time_init", "seconds": 0, "rate": RATE})
        await a.recv_type("time")
        a.send({"t": "time_pause", "on": True, "reason": "dialogue"})
        packet = await a.recv_type("time")
        while not packet["paused"]:
            packet = await a.recv_type("time")
        gone = await a.recv_type("time", timeout=1.5)
        while gone["paused"]:
            gone = await a.recv_type("time", timeout=1.5)
        self.assertFalse(gone["paused"])

    async def test_time_only_jumps_forward_and_not_too_far(self):
        await self.start_clock(max_time_jump_seconds=10_000)
        a, b = await self.enter("Alice"), await self.enter("Bob")
        await a.recv_type("time_need")
        a.send({"t": "time_init", "seconds": 50_000, "rate": 1.0})
        await b.recv_type("time")
        a.send({"t": "time_jump", "to": 53_600})   # slept one hour
        jump = await b.recv_type("time")
        while not jump["jump"]:
            jump = await b.recv_type("time")
        self.assertAlmostEqual(jump["seconds"], 53_600, delta=2)
        a.send({"t": "time_jump", "to": 40_000})   # backwards: ignored
        seen = []
        end = asyncio.get_running_loop().time() + 0.5
        while asyncio.get_running_loop().time() < end:   # the clock keeps ticking every 0.1 s: look at half a second of it
            seen.append(await b.recv_type("time", timeout=0.5))
        self.assertTrue(seen)
        self.assertFalse(any(p["jump"] for p in seen))
        self.assertTrue(all(p["seconds"] > 53_000 for p in seen))
        a.send({"t": "time_jump", "to": 500_000})   # too far
        self.assertEqual((await a.recv_type("time_reject"))["reason"], "jump too far")

    async def test_bad_values_and_a_missing_world_are_refused(self):
        await self.start_clock()
        lonely = await self.join("Alice")
        lonely.send({"t": "time_init", "seconds": 1, "rate": 1.0})
        self.assertEqual((await lonely.recv_type("time_reject"))["reason"], "not in a world")
        lonely.send({"t": "join_world", "slot": "main"})
        await lonely.recv_type("time_need")
        for fields in (dict(seconds=-5, rate=1.0), dict(seconds="9", rate=1.0), dict(seconds=1, rate=0),
                       dict(seconds=1, rate=1e9), dict(seconds=True, rate=1.0), dict(seconds=1e30, rate=1.0)):
            lonely.send({"t": "time_init", **fields})
            self.assertEqual((await lonely.recv_type("time_reject"))["reason"], "invalid time", fields)
        lonely.send({"t": "time_pause", "on": True, "reason": "dialogue"})
        self.assertEqual((await lonely.recv_type("time_reject"))["reason"], "no clock")

    async def test_worlds_have_their_own_clocks_and_an_empty_world_stops(self):
        await self.start_clock()
        a, b = await self.enter("Alice", "one"), await self.enter("Bob", "two")
        await a.recv_type("time_need")
        await b.recv_type("time_need")
        a.send({"t": "time_init", "seconds": 1000, "rate": 10.0})
        b.send({"t": "time_init", "seconds": 777000, "rate": 10.0})
        self.assertLess((await a.recv_type("time"))["seconds"], 10_000)
        self.assertGreater((await b.recv_type("time"))["seconds"], 700_000)
        await a.close()
        await asyncio.sleep(0.3)
        c = await self.enter("Carol", "one")
        self.assertEqual((await c.recv_type("time", "time_need"))["t"], "time_need")   # nobody was in "one": the world stood still
