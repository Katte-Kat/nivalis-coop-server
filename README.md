# Nivalis Coop server

A small dedicated server for **Nivalis Nights** multiplayer, in the spirit of a Minecraft server: it owns the
shared world, and every player's game joins it with the companion mod (see the `nivalis-coop-mod` repository).

What the server keeps for everyone: world saves (versioned slots), the shared money account, shared
state keys (businesses, placed furniture), conversations with votes and a random draw on disagreement, the world clock
(pauses and time jumps), and chat. Only inventories stay per player.

There is no host player. The server picks a **session leader** per world (the first player in it; when they leave,
the next one takes over by itself). That player's game simulates the people of the world and the server mirrors them
to everybody else, see "Session leader" in [PROTOCOL.md](PROTOCOL.md).

- Python 3.12, standard library only, one asyncio process, SQLite storage
- Length-prefixed JSON over TCP, 20 ticks/s, protocol in [PROTOCOL.md](PROTOCOL.md)
- Runs as a rootless Podman container: read-only root, all capabilities dropped, 256 MB, one named volume

## Quick start (Podman)

```bash
podman build -f deploy/Containerfile -t localhost/nivalis-coop:latest server/
cp deploy/server.env.example deploy/server.env      # set NIVALIS_COOP_PASSWORD at least
deploy/run.sh
```

The server listens on TCP **27800**. Open that port in your firewall (and forward it on the router for friends
outside your network). `podman logs -f nivalis-coop` shows the log.

To start it at boot, copy `deploy/nivalis-coop.container` to `~/.config/containers/systemd/` (Quadlet; needs
`loginctl enable-linger $USER`).

`deploy/deploy.sh` copies the sources to another machine over ssh and rebuilds there: `HOST=user@machine deploy/deploy.sh`.

## Settings

Environment variables `NIVALIS_COOP_<SETTING>`, listed in `deploy/server.env.example` with their defaults in
`server/nivalis_coop/config.py`: name, MOTD, max players, password, whitelist, tick rate, autosave interval,
vote time (`dialogue_vote_seconds`, 5 s), clock settings, log level.

## Operating

Console commands through the admin socket inside the container:

```bash
podman exec nivalis-coop python -m nivalis_coop ctl help
podman exec nivalis-coop python -m nivalis_coop ctl whitelist add SomeName
```

Players are identified by name plus a token the server hands out on the first join (trust on first use).

## Tests

```bash
cd server && PYTHONPATH=. python3 -m unittest discover -s tests
```

`server/tools/` has small bots (`walkbot`, `votebot`, `timebot`) that act as extra players for manual testing.

## Security notes

There is **no TLS**: the password and chat travel in clear text. Use a throwaway password, not one you use
elsewhere, and prefer a VPN or SSH tunnel for play outside your LAN. The server rate-limits clients and drops
those that misbehave, but it is a hobby server: do not expose it with an empty password to the internet.

## License

MIT, see [LICENSE](LICENSE).
