"""``python -m nivalis_coop``        run the server
``python -m nivalis_coop ctl ...``  send a command to a running server
``python -m nivalis_coop --healthcheck``  exit 0 if the server answers a status ping"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from . import __version__
from .admin import ctl
from .client import status
from .config import Config
from .server import CoopServer


def _default_config_path() -> str:
    return os.path.join(os.environ.get("NIVALIS_COOP_DATA_DIR", "/data"), "server.toml")


async def _run(cfg: Config) -> None:
    server = CoopServer(cfg)
    await server.start()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, server.stop_event.set)
    await server.stop_event.wait()
    await server.stop()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "ctl":
        cfg = Config.load(_default_config_path())
        if len(argv) < 2:
            print("usage: nivalis_coop ctl <command ...>   (try: ctl help)", file=sys.stderr)
            return 2
        try:
            print(ctl(cfg.admin_path, " ".join(argv[1:])), end="")
        except OSError as exc:
            print(f"cannot reach the server ({cfg.admin_path}): {exc}", file=sys.stderr)
            return 1
        return 0

    parser = argparse.ArgumentParser(prog="nivalis_coop", description="Nivalis Coop game server")
    parser.add_argument("--config", default=_default_config_path(), help="TOML settings file (default: <data_dir>/server.toml)")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--healthcheck", action="store_true", help="ping the local server and exit 0/1")
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0

    try:
        cfg = Config.load(args.config)
    except (ValueError, OSError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if args.healthcheck:
        try:
            result = asyncio.run(status("127.0.0.1", cfg.port))
        except Exception as exc:
            print(f"unhealthy: {exc}", file=sys.stderr)
            return 1
        print(f"ok: {result['online']}/{result['max']} players, {result['tps']} tps")
        return 0

    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    asyncio.run(_run(cfg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
