import asyncio
import os
import tempfile
import unittest
from unittest import mock

from nivalis_coop.config import Config
from nivalis_coop.protocol import ProtocolError, encode, read_frame, valid_key, valid_name
from nivalis_coop.store import Store
from nivalis_coop.world import World


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_trip(self):
        reader = asyncio.StreamReader()
        reader.feed_data(encode({"t": "move", "x": 1.5, "name": "Käthe"}))
        self.assertEqual(await read_frame(reader, 1000), {"t": "move", "x": 1.5, "name": "Käthe"})

    async def test_oversize_frame_is_refused(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b"\x00\x10\x00\x00" + b"x" * 10)
        with self.assertRaises(ProtocolError):
            await read_frame(reader, 4096)

    async def test_rejects_non_json_and_untyped_frames(self):
        for body in (b"not json", b"[1,2]", b'{"x":1}', b'{"t":5}'):
            reader = asyncio.StreamReader()
            reader.feed_data(len(body).to_bytes(4, "big") + body)
            with self.assertRaises(ProtocolError, msg=body):
                await read_frame(reader, 4096)

    def test_names(self):
        for ok in ("Kat", "Kat Kat", "a.b-c_d", "Müller", "x1"):
            self.assertTrue(valid_name(ok), ok)
        for bad in ("", "K", "a" * 25, " Kat", "Kat ", "ka\nt", "<b>", None, 5):
            self.assertFalse(valid_name(bad), bad)

    def test_keys(self):
        for ok in ("world/time", "orders/12/state", "shared/a.b-c"):
            self.assertTrue(valid_key(ok), ok)
        for bad in ("time", "World/x", "world/", "world//x", "world/a b", "w/" + "a" * 200, "a/b/c/d/e/f/g/h", 7):
            self.assertFalse(valid_key(bad), bad)


class WorldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "w.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def world(self, **kw):
        args = dict(max_keys=4, max_value_bytes=50, max_total_bytes=120)
        args.update(kw)
        return World(self.store, **args)

    def test_delta_collapses_and_resets(self):
        w = self.world()
        w.set("world/a", 1)
        w.set("world/a", 2)
        w.set("world/b", 3)
        w.delete("world/b")
        self.assertEqual(w.take_delta(), {"t": "state", "rev": 4, "set": {"world/a": 2}, "del": ["world/b"]})
        self.assertIsNone(w.take_delta())

    def test_limits(self):
        w = self.world()
        self.assertEqual(w.check_set("world/a", "x" * 60), "value too large")
        for i in range(4):
            w.set(f"world/k{i}", i)
        self.assertEqual(w.check_set("world/new", 1), "too many keys")
        self.assertIsNone(w.check_set("world/k0", 99))  # replacing is fine
        w2 = self.world(max_keys=100)
        w2.set("world/big1", "y" * 40)
        w2.set("world/big2", "y" * 40)
        self.assertEqual(w2.check_set("world/big3", "y" * 40), "world data is full")

    def test_save_and_reload(self):
        w = self.world(max_keys=100, max_value_bytes=1000, max_total_bytes=10000)
        w.set("world/a", {"n": 1})
        w.set("world/b", 2)
        w.save()
        w.delete("world/b")
        w.set("world/c", [3])
        self.assertEqual(w.save(), 2)
        again = self.world(max_keys=100, max_value_bytes=1000, max_total_bytes=10000)
        self.assertEqual(again.state, {"world/a": {"n": 1}, "world/c": [3]})
        self.assertEqual(again.rev, w.rev)


class ConfigTests(unittest.TestCase):
    def test_environment_overrides_file_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "server.toml")
            with open(path, "w") as fh:
                fh.write('name = "From file"\nport = 1111\nmax_players = 3\n')
            env = {"NIVALIS_COOP_PORT": "2222", "NIVALIS_COOP_WHITELIST": "true",
                   "NIVALIS_COOP_STATE_WRITE_PREFIXES": "a/, b/"}
            with mock.patch.dict(os.environ, env):
                cfg = Config.load(path)
            self.assertEqual((cfg.name, cfg.port, cfg.max_players), ("From file", 2222, 3))
            self.assertTrue(cfg.whitelist)
            self.assertEqual(cfg.state_write_prefixes, ["a/", "b/"])

    def test_typos_and_bad_values_are_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "server.toml")
            with open(path, "w") as fh:
                fh.write("max_playerz = 3\n")
            with self.assertRaises(ValueError):
                Config.load(path)
        with mock.patch.dict(os.environ, {"NIVALIS_COOP_PORT": "abc"}):
            with self.assertRaises(ValueError):
                Config.load(None)
        with mock.patch.dict(os.environ, {"NIVALIS_COOP_TICK_RATE": "500"}):
            with self.assertRaises(ValueError):
                Config.load(None)


if __name__ == "__main__":
    unittest.main()
