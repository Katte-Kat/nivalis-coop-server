"""Test bot for the session leader: enters a world first (so it leads) and sends people snapshots.

    PYTHONPATH=server python3 server/tools/leaderbot.py HOST PORT --world main --near PlayerName --seconds 60

It sends one made-up person ("bot-person-1") walking a circle in front of --near, and otherwise no people, so a
game that follows it hides its own people and shows a stand-in. When it leaves, that game becomes the leader.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import time

from nivalis_coop.client import Client


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("port", type=int)
    ap.add_argument("--world", default="main")
    ap.add_argument("--near", required=True, help="name of the player to walk in front of")
    ap.add_argument("--name", default="LeaderBot")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--password")
    args = ap.parse_args()

    client = await Client.open(args.host, args.port)
    ok = await client.login(args.name, password=args.password)
    if ok["t"] != "login_ok":
        print("refused:", ok.get("reason"))
        return 1
    client.send({"t": "join_world", "slot": args.world})
    leader = await client.recv_type("leader")
    print("leader of", args.world, "is", leader["name"])

    target_id, centre, tick = None, None, 0
    end = time.monotonic() + args.seconds
    while time.monotonic() < end:
        try:
            msg = await client.recv_type("tick", "player_join", "npc_act", timeout=0.2)
        except asyncio.TimeoutError:
            msg = None
        if msg and msg["t"] == "player_join" and msg["name"] == args.near:
            target_id = msg["id"]
        if msg and msg["t"] == "tick" and target_id is not None:
            pose = msg["p"].get(str(target_id))
            if pose:
                yaw = math.radians(pose[3])
                centre = (pose[0] + 4 * math.sin(yaw), pose[1] - 1.6, pose[2] + 4 * math.cos(yaw))
        if msg and msg["t"] == "npc_act":
            print("asked:", msg)
        if centre is not None:
            a = time.monotonic() * 0.8
            tick += 1
            person = {"id": "bot-person-1", "p": [round(centre[0] + math.cos(a), 2), round(centre[1], 2), round(centre[2] + math.sin(a), 2)],
                      "r": round(math.degrees(-a)) % 360, "w": 1}
            client.send({"t": "npc_state", "tick": tick, "full": True, "npcs": [person]})
    await client.close()
    print("left after", tick, "snapshots")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
