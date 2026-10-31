"""Test bot for the shared world clock: a second player who watches the clock and can hold it or skip time.

    PYTHONPATH=server python3 server/tools/timebot.py 127.0.0.1 27850 --world main --script

  default   only print what the server clock does (game time as HH:MM, speed, paused)
  --script  after 20 s hold the clock for 15 s ("dialogue" pause), 15 s after the release skip one hour ahead
"""
from __future__ import annotations

import argparse
import asyncio
import random
import time

from nivalis_coop.client import Client


def clock(seconds: float) -> str:
    day, rest = divmod(int(seconds), 86400)
    return f"day {day + 1} {rest // 3600:02d}:{rest % 3600 // 60:02d}:{rest % 60:02d}"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("port", type=int)
    ap.add_argument("--world", default="main")
    ap.add_argument("--name", default=f"Clock{random.randint(100, 999)}")
    ap.add_argument("--seconds", type=float, default=300)
    ap.add_argument("--script", action="store_true")
    args = ap.parse_args()

    c = await Client.open(args.host, args.port)
    ok = await c.login(args.name)
    if ok["t"] != "login_ok":
        print("refused:", ok.get("reason"), flush=True)
        return 1
    c.send({"t": "join_world", "slot": args.world})
    print(f"{args.name} in world '{args.world}'", flush=True)

    start = time.monotonic()
    last_print = 0.0
    step = 0
    last = None
    while time.monotonic() - start < args.seconds:
        el = time.monotonic() - start
        if args.script:
            if step == 0 and el > 20:
                step = 1
                c.send({"t": "time_pause", "on": True, "reason": "dialogue"})
                print(f"[{el:5.1f}s] >>> I HOLD the clock", flush=True)
            elif step == 1 and el > 35:
                step = 2
                c.send({"t": "time_pause", "on": False, "reason": "dialogue"})
                print(f"[{el:5.1f}s] >>> I let the clock run again", flush=True)
            elif step == 2 and el > 50 and last:
                step = 3
                c.send({"t": "time_jump", "to": last["seconds"] + 3600})
                print(f"[{el:5.1f}s] >>> I skip one hour ahead (sleep)", flush=True)
        try:
            msg = await c.recv(0.5)
        except TimeoutError:
            continue
        if msg["t"] == "time_need":
            print(f"[{el:5.1f}s] the world has no clock yet (waiting for a game to start it)", flush=True)
        elif msg["t"] == "time":
            last = msg
            if msg["jump"] or el - last_print >= 2.0:
                last_print = el
                print(f"[{el:5.1f}s] server clock {clock(msg['seconds'])}  rate {msg['rate']:.2f}  "
                      f"{'PAUSED ' + ','.join(msg['reasons']) if msg['paused'] else 'running'}{'  JUMP' if msg['jump'] else ''}", flush=True)
        elif msg["t"] == "time_reject":
            print("refused:", msg, flush=True)
    c.send({"t": "bye"})
    await c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
