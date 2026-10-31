import asyncio
import base64
import hashlib
import os

from test_server import ServerCase

BLOB = os.urandom(300 * 1024 + 123)  # three chunks, the last one odd-sized


class ServerSaves(ServerCase):
    async def test_upload_then_download_returns_the_same_bytes(self):
        srv = await self.start()
        a = await self.join("Alice")
        stored = await a.upload_save("main", BLOB, note="day 28")
        self.assertEqual((stored["t"], stored["version"], stored["size"]), ("save_stored", 1, len(BLOB)))
        self.assertEqual(stored["sha256"], hashlib.sha256(BLOB).hexdigest())

        b = await self.connect()
        ok = await b.login("Bob")  # a late joiner learns about the save with the login
        self.assertEqual([(s["slot"], s["version"], s["uploader"]) for s in ok["saves"]], [("main", 1, "Alice")])
        info, data = await b.download_save("main")
        self.assertEqual(data, BLOB)
        self.assertEqual((info["version"], info["note"]), (1, "day 28"))
        self.assertIn("by Alice", srv.commands.execute("saves")[0])

    async def test_everybody_else_is_told_about_a_new_version(self):
        await self.start()
        a, b = await self.join("Alice"), await self.join("Bob")
        await a.upload_save("main", BLOB[:1000])
        news = await b.recv_type("save_updated")
        self.assertEqual((news["slot"], news["version"]), ("main", 1))

    async def test_saving_from_an_old_version_is_refused_instead_of_overwriting(self):
        await self.start()
        a, b = await self.join("Alice"), await self.join("Bob")
        await a.upload_save("main", b"first" * 100)
        late = await b.upload_save("main", b"mine" * 100, base=0)   # Bob started from nothing
        self.assertEqual(late["t"], "save_reject")
        self.assertIn("stale", late["reason"])
        again = await b.upload_save("main", b"mine" * 100, base=1)  # after loading v1 he may continue it
        self.assertEqual(again["version"], 2)
        _, data = await a.download_save("main")
        self.assertEqual(data, b"mine" * 100)

    async def test_two_uploads_racing_for_the_same_base_only_one_wins(self):
        await self.start()
        a, b = await self.join("Alice"), await self.join("Bob")
        results = await asyncio.gather(a.upload_save("main", b"A" * 200_000), b.upload_save("main", b"B" * 200_000))
        self.assertEqual(sorted(r["t"] for r in results), ["save_reject", "save_stored"])
        loser = next(r for r in results if r["t"] == "save_reject")
        self.assertIn("stale", loser["reason"])

    async def test_old_versions_are_pruned_and_their_files_removed(self):
        srv = await self.start(save_keep=2)
        a = await self.join("Alice")
        for base in range(3):
            await a.upload_save("main", bytes([base]) * 1000, base=base)
        files = sorted(os.listdir(os.path.join(srv.cfg.data_dir, "saves", "main")))
        self.assertEqual(files, ["2.sav", "3.sav"])
        with self.assertRaises(LookupError):
            await a.download_save("main", version=1)
        _, data = await a.download_save("main", version=2)
        self.assertEqual(data, bytes([1]) * 1000)

    async def test_corrupt_incomplete_and_oversized_uploads_are_refused(self):
        srv = await self.start(max_save_bytes=1_000_000)
        a = await self.join("Alice")
        # wrong checksum
        a.send({"t": "save_put", "slot": "main", "size": 10, "sha256": "0" * 64, "base": 0})
        await a.recv_type("save_put_ok")
        a.send({"t": "save_data", "slot": "main", "n": 0, "data": base64.b64encode(b"0123456789").decode()})
        a.send({"t": "save_end", "slot": "main"})
        self.assertEqual((await a.recv_type("save_reject"))["reason"], "checksum mismatch")
        # announced 100 bytes, sends 10
        sha = hashlib.sha256(b"x" * 100).hexdigest()
        a.send({"t": "save_put", "slot": "main", "size": 100, "sha256": sha, "base": 0})
        await a.recv_type("save_put_ok")
        a.send({"t": "save_data", "slot": "main", "n": 0, "data": base64.b64encode(b"x" * 10).decode()})
        a.send({"t": "save_end", "slot": "main"})
        self.assertIn("incomplete", (await a.recv_type("save_reject"))["reason"])
        # chunk out of order, not base64, more than announced
        for bad, why in ((dict(n=5, data="AAAA"), "out of order"), (dict(n=0, data="***"), "base64"),
                         (dict(n=0, data=base64.b64encode(b"y" * 20).decode()), "size")):
            a.send({"t": "save_put", "slot": "main", "size": 10, "sha256": sha, "base": 0})
            await a.recv_type("save_put_ok")
            a.send({"t": "save_data", "slot": "main", **bad})
            self.assertIn(why, (await a.recv_type("save_reject"))["reason"])
        # too large, bad slot name, bad checksum format
        for fields, why in ((dict(slot="main", size=2_000_000, sha256=sha), "invalid size"),
                            (dict(slot="Bad Slot", size=10, sha256=sha), "invalid slot"),
                            (dict(slot="main", size=10, sha256="xyz"), "invalid sha256")):
            a.send({"t": "save_put", "base": 0, **fields})
            self.assertIn(why, (await a.recv_type("save_reject"))["reason"])
        self.assertEqual(srv.store.all_saves(), [])
        self.assertEqual(os.listdir(os.path.join(srv.cfg.data_dir, "saves", "main")), [])  # no temp file left behind

    async def test_leaving_in_the_middle_of_an_upload_commits_nothing(self):
        srv = await self.start()
        a = await self.join("Alice")
        a.send({"t": "save_put", "slot": "main", "size": 500_000, "sha256": "a" * 64, "base": 0})
        await a.recv_type("save_put_ok")
        a.send({"t": "save_data", "slot": "main", "n": 0, "data": base64.b64encode(b"z" * 1000).decode()})
        await asyncio.sleep(0.1)
        await a.close()
        for _ in range(40):
            if not srv.sessions:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(srv.store.all_saves(), [])
        self.assertEqual(os.listdir(os.path.join(srv.cfg.data_dir, "saves", "main")), [])

    async def test_saves_survive_a_restart_and_unknown_slots_are_reported(self):
        await self.start()
        a = await self.join("Alice")
        await a.upload_save("main", BLOB)
        token = a.token
        await a.close()
        await self.server.stop()
        self.server = None

        await self.start()
        b = await self.connect()
        ok = await b.login("Alice", token=token)
        self.assertEqual(ok["saves"][0]["version"], 1)
        _, data = await b.download_save("main")
        self.assertEqual(data, BLOB)
        with self.assertRaises(LookupError):
            await b.download_save("nothere")

    async def test_a_download_does_not_stall_the_other_players(self):
        await self.start()
        a, b = await self.join("Alice"), await self.join("Bob")
        await a.upload_save("main", os.urandom(2_000_000))
        a.send({"t": "save_get", "slot": "main"})       # a big download is running ...
        b.send({"t": "chat", "text": "still there?"})  # ... while chat and ticks keep flowing
        self.assertEqual((await b.recv_type("chat"))["text"], "still there?")
        await a.recv_type("save_done", timeout=10)
