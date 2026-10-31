"""Demo/test bot: joins, stands a few metres in front of another player and walks a small circle there.

    PYTHONPATH=server python3 server/tools/walkbot.py 127.0.0.1 27800 --follow KatTest

Useful to see name tags and interpolation in the game without a second player.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import random
import time

from nivalis_coop.client import Client


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("port", type=int)
    ap.add_argument("--follow", required=True, help="name of the player to stand in front of")
    ap.add_argument("--name", default=f"Bot{random.randint(1000, 9999)}")
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--password")
    args = ap.parse_args()

    client = await Client.open(args.host, args.port)
    ok = await client.login(args.name, password=args.password)
    if ok["t"] != "login_ok":
        print("refused:", ok.get("reason"))
        return 1
    target = next((p for p in ok["players"] if p["name"].casefold() == args.follow.casefold()), None)
    if target is None:
        print(f"{args.follow} is not online, players: {[p['name'] for p in ok['players']]}")
        return 1
    tid, pose = target["id"], target["pose"]
    print(f"{args.name} joined as id {client.id}; {args.follow} is id {tid} at {pose[:4]} in '{pose[5]}'")

    started, said = time.monotonic(), False
    while (elapsed := time.monotonic() - started) < args.seconds:
        # keep the latest pose of the player we follow (the server only sends changes)
        try:
            while True:
                msg = await client.recv(0.02)
                if msg["t"] == "tick" and str(tid) in msg["p"]:
                    pose = msg["p"][str(tid)]
                elif msg["t"] == "chat":
                    print("chat:", msg["name"], msg["text"])
        except TimeoutError:
            pass
        x, y, z, yaw = pose[0], pose[1], pose[2], math.radians(pose[3])
        angle = elapsed * 1.2
        ahead = 5.0  # metres in front of their camera
        bx = x + math.sin(yaw) * ahead + math.cos(angle) * 1.5
        bz = z + math.cos(yaw) * ahead + math.sin(angle) * 1.5
        client.send({"t": "move", "x": bx, "y": y - 1.0, "z": bz, "ry": (math.degrees(angle) + 90) % 360,
                     "anim": "walk", "scene": pose[5]})
        if not said and elapsed > 3:
            client.send({"t": "chat", "text": f"hello from {args.name}"})
            said = True
        await asyncio.sleep(1 / 15)
    client.send({"t": "bye"})
    await client.close()
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
