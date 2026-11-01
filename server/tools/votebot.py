"""Test bot: a second player in a server world who takes part in conversation votes.

    PYTHONPATH=server python3 server/tools/votebot.py 127.0.0.1 27850 --world main --mode other

  --mode other   votes for a different answer than the first voter (forces the random draw)
  --mode same    votes for the same answer as the first voter
  --mode silent  never votes (the vote window decides)
  --start TREE   starts that conversation itself after --start-after seconds (with --person, the person's guid):
                 every game in the world must open it
Prints every conversation packet it sees.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import time

from nivalis_coop.client import Client


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("port", type=int)
    ap.add_argument("--world", default="main")
    ap.add_argument("--name", default=f"Buddy{random.randint(100, 999)}")
    ap.add_argument("--mode", choices=["other", "same", "silent"], default="other")
    ap.add_argument("--seconds", type=float, default=240)
    ap.add_argument("--password")
    ap.add_argument("--start")
    ap.add_argument("--person")
    ap.add_argument("--start-after", type=float, default=20)
    args = ap.parse_args()

    client = await Client.open(args.host, args.port)
    ok = await client.login(args.name, password=args.password)
    if ok["t"] != "login_ok":
        print("refused:", ok.get("reason"), flush=True)
        return 1
    client.send({"t": "join_world", "slot": args.world})
    print(f"{args.name} (id {client.id}) is in world '{args.world}', mode {args.mode}", flush=True)

    voted: set[str] = set()
    end = time.monotonic() + args.seconds
    start_at = time.monotonic() + args.start_after if args.start else None
    while time.monotonic() < end:
        if start_at is not None and time.monotonic() >= start_at:
            start_at = None
            client.send({"t": "dlg_start", "tree": args.start, "person": args.person, "prev": None})
            print(f"started conversation {args.start}", flush=True)
        try:
            msg = await client.recv(1.0)
        except TimeoutError:
            continue
        t = msg["t"]
        if t == "dlg_start":
            print(f"[{time.strftime('%T')}] conversation started by player {msg['from']}: tree {msg['tree']}, person {msg['person']}", flush=True)
        elif t == "dlg_votes":
            print(f"[{time.strftime('%T')}] votes {msg['votes']} of {msg['options']} answers, {msg['ms_left']} ms left, round {msg['round'][-30:]}", flush=True)
            if msg["round"] not in voted and args.mode != "silent":
                others = [c for pid, c in msg["votes"].items() if int(pid) != client.id]
                if others:
                    first = others[0]
                    mine = first if args.mode == "same" else next(c for c in range(msg["options"]) if c != first)
                    voted.add(msg["round"])
                    client.send({"t": "dlg_vote", "round": msg["round"], "choice": mine, "options": msg["options"]})
                    print(f"[{time.strftime('%T')}] I vote for answer {mine}", flush=True)
        elif t == "dlg_result":
            print(f"[{time.strftime('%T')}] DECIDED answer {msg['choice']} ({'drawn' if msg['random'] else 'agreed'}), votes {msg['votes']}", flush=True)
        elif t == "dlg_reject":
            print("refused:", msg, flush=True)
    client.send({"t": "bye"})
    await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
