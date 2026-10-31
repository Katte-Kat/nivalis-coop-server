import asyncio
import random

from test_server import ServerCase

ROUND = "main|tree-1|aaa|bbb"


class SharedConversations(ServerCase):
    async def players(self, names, world="main", **config):
        await self.start(dialogue_vote_seconds=0.5, **config)
        clients = []
        for name in names:
            client = await self.join(name)
            client.send({"t": "join_world", "slot": world})
            clients.append(client)
        await asyncio.sleep(0.15)  # the server has seen every join_world before the first vote opens a round
        return clients

    @staticmethod
    def vote(client, choice, options=3, round_id=ROUND):
        client.send({"t": "dlg_vote", "round": round_id, "choice": choice, "options": options})

    async def test_a_single_player_decides_at_once(self):
        (a,) = await self.players(["Alice"])
        self.vote(a, 1)
        result = await a.recv_type("dlg_result")
        self.assertEqual((result["round"], result["choice"], result["random"]), (ROUND, 1, False))

    async def test_when_everybody_agrees_the_answer_is_taken_as_soon_as_the_last_one_voted(self):
        a, b = await self.players(["Alice", "Bob"])
        self.vote(a, 2)
        with self.assertRaises(TimeoutError):  # Bob has not voted and nobody disagrees yet: wait
            await a.recv_type("dlg_result", timeout=0.2)
        self.vote(b, 2)
        for client in (a, b):
            result = await client.recv_type("dlg_result")
            self.assertEqual((result["choice"], result["random"]), (2, False))

    async def test_two_different_answers_are_drawn_at_random_without_waiting_for_the_rest(self):
        a, b, c = await self.players(["Alice", "Bob", "Carol"])
        self.server.dialogues.rng = random.Random(7)
        seen = set()
        for n in range(14):  # stays under the 20-votes burst limit of one player
            round_id = f"{ROUND}|{n}"
            self.vote(a, 0, round_id=round_id)
            self.vote(b, 1, round_id=round_id)   # Carol never votes
            result = await c.recv_type("dlg_result")
            self.assertTrue(result["random"])
            self.assertIn(result["choice"], (0, 1))   # only voted answers can win
            self.assertEqual(result["votes"], {str(a.id): 0, str(b.id): 1})
            seen.add(result["choice"])
        self.assertEqual(seen, {0, 1})   # both answers get drawn now and then

    async def test_a_voted_answer_no_other_answer_can_win(self):
        a, b, c = await self.players(["Alice", "Bob", "Carol"])
        for n in range(15):
            self.vote(a, 2, round_id=f"x{n}")
            self.vote(b, 0, round_id=f"x{n}")
            self.assertIn((await a.recv_type("dlg_result"))["choice"], (0, 2))   # never 1

    async def test_after_the_window_the_only_voted_answer_wins(self):
        a, b, c = await self.players(["Alice", "Bob", "Carol"])
        self.vote(a, 2)
        with self.assertRaises(TimeoutError):
            await b.recv_type("dlg_result", timeout=0.25)
        result = await b.recv_type("dlg_result", timeout=1.0)   # 0.5 s window since the first vote
        self.assertEqual((result["choice"], result["random"], list(result["votes"].values())), (2, False, [2]))

    async def test_the_others_see_who_voted_what_and_how_long_is_left(self):
        a, b = await self.players(["Alice", "Bob"])
        self.vote(a, 1)
        update = await b.recv_type("dlg_votes")
        self.assertEqual(update["votes"], {str(a.id): 1})
        self.assertEqual(sorted(update["expected"]), sorted([a.id, b.id]))
        self.assertTrue(0 < update["ms_left"] <= 500)

    async def test_a_late_client_gets_the_decision_again(self):
        a, b = await self.players(["Alice", "Bob"])
        self.vote(a, 0)
        self.vote(b, 1)
        decided = await a.recv_type("dlg_result")
        self.vote(b, 1)   # Bob's game was a little behind and asks for the same round again
        again = await b.recv_type("dlg_result")
        again = await b.recv_type("dlg_result") if again["round"] != ROUND else again
        self.assertEqual(again["choice"], decided["choice"])

    async def test_somebody_who_leaves_does_not_hold_the_others_up(self):
        a, b = await self.players(["Alice", "Bob"])
        self.vote(a, 1)
        await b.close()
        result = await a.recv_type("dlg_result", timeout=0.4)   # well before the 0.5 s window ends
        self.assertEqual(result["choice"], 1)

    async def test_everything_stays_inside_the_world(self):
        a, b = await self.players(["Alice", "Bob"], world="main")
        c = await self.join("Carol")
        c.send({"t": "join_world", "slot": "other"})
        await asyncio.sleep(0.1)
        self.vote(a, 0)
        self.vote(b, 0)
        await a.recv_type("dlg_result")
        with self.assertRaises(TimeoutError):
            await c.recv_type("dlg_votes", "dlg_result", timeout=0.3)
        self.vote(c, 1)   # the same round id in another world is another round
        self.assertEqual((await c.recv_type("dlg_result"))["choice"], 1)

    async def test_the_start_of_a_conversation_reaches_the_others_in_the_world_but_not_the_sender(self):
        a, b = await self.players(["Alice", "Bob"])
        c = await self.join("Carol")
        c.send({"t": "join_world", "slot": "other"})
        await asyncio.sleep(0.1)
        a.send({"t": "dlg_start", "tree": "tree-guid-1", "person": "person-guid-9", "prev": "pin-3"})
        start = await b.recv_type("dlg_start")
        self.assertEqual((start["from"], start["tree"], start["person"], start["prev"]), (a.id, "tree-guid-1", "person-guid-9", "pin-3"))
        for other in (a, c):
            with self.assertRaises(TimeoutError):
                await other.recv_type("dlg_start", timeout=0.25)

    async def test_bad_votes_are_refused_with_a_reason(self):
        await self.start(dialogue_vote_seconds=0.5)
        lonely = await self.join("Alice")
        self.vote(lonely, 0)    # no world joined yet
        self.assertIn("not in a world", (await lonely.recv_type("dlg_reject"))["reason"])
        lonely.send({"t": "join_world", "slot": "main"})
        await asyncio.sleep(0.1)
        for fields in (dict(choice=5, options=3), dict(choice=-1, options=3), dict(choice=0, options=1), dict(choice=True, options=3), dict(choice="1", options=3)):
            lonely.send({"t": "dlg_vote", "round": ROUND, **fields})
            self.assertEqual((await lonely.recv_type("dlg_reject"))["reason"], "invalid vote", fields)
        # two players disagree about how many answers there are: the later one is told
        bob = await self.join("Bob")
        bob.send({"t": "join_world", "slot": "main"})
        await asyncio.sleep(0.1)
        self.vote(lonely, 0, options=3, round_id="mismatch")
        self.vote(bob, 0, options=4, round_id="mismatch")
        self.assertIn("answers", (await bob.recv_type("dlg_reject"))["reason"])
