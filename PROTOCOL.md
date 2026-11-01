# Nivalis Coop protocol, version 1

Transport: TCP. Framing: 4 byte big-endian length, then one UTF-8 JSON object that has a string field `t`
(the packet type). Frames from the client are limited to 4 KiB before login and 256 KiB afterwards, frames
from the server to 8 MiB. Unknown packet types are ignored by clients and counted as a strike by the server,
so either side can add packets without breaking the other.

Phases, as in Minecraft: **handshake** (`hello`) -> **status** (server-list ping, then the server hangs up)
or **login** -> **play**.

## Client -> server

| `t` | fields | meaning |
|---|---|---|
| `hello` | `proto` (int), `mod` (string), `intent` = `"login"` or `"status"` | first packet of every connection |
| `login` | `name`, `token`?, `password`? | second packet when intent is login |
| `keepalive` | `id` | answer to the server's keepalive (must echo its `id`) |
| `move` | `x`,`y`,`z`,`ry` (numbers), `anim`? (<= 32), `scene`? (<= 48) | own pose; lossy, rate limited |
| `chat` | `text` (<= 256) | chat line; a leading `/` is an operator command |
| `state_set` | `key`, `value` (not null), `expect`? | write a shared-world key; with `expect` it only applies if the current value equals it (`null` = key must be unset) |
| `state_del` | `key`, `expect`? | remove a shared-world key |
| `state_add` | `key`, `delta` (int), `cid`?, `reason`? | add to an integer key (a shared account); adds from several players are summed, the result is clamped to `counter_min`..`counter_max`; answered with `state_ack` when `cid` is set |
| `event` | `kind`, `data`?, `to`? | one-shot message to everybody else, or to player id `to` |
| `save_put` | `slot`, `size`, `sha256`, `base`, `note`? | start uploading a savegame; `base` is the version the sender started from (0 = none) |
| `save_data` | `slot`, `n`, `data` (base64, <= 128 KiB raw) | next chunk of the upload, `n` counts from 0 |
| `save_end` | `slot` | finish the upload; the server checks size and SHA-256 and stores the next version |
| `save_get` | `slot`, `version`? | download a savegame (default: newest) |
| `save_list` | | list the newest version of every savegame |
| `join_world` | `slot` (or `null`) | the server world you play in; shared conversations only reach players of the same world |
| `dlg_start` | `tree`, `person`?, `prev`? | you started a conversation (guids from the game): the others in your world are told |
| `dlg_vote` | `round`, `choice`, `options` | vote for an answer; `round` is any string that is the same on every client for the same choice, `options` the number of answers (2-16) |
| `time_init` | `seconds`, `rate` | you are the first in the world (the server sent `time_need`): start its clock with your game's time (game seconds) and speed (game seconds per real second) |
| `time_pause` | `on`, `reason`? | hold the world's clock (a conversation) or release your hold; the server keeps one pause per player |
| `time_jump` | `to` | the world jumped forward on your side (sleep, fast-forward); only forward, at most `max_time_jump_seconds` |
| `npc_state` | `tick`, `full`, `npcs` | session leader only: the people near the players. `npcs` = list of `{id, p:[x,y,z], r, w}` (ghost id, position, heading in degrees, walk animation) or `{id, gone:true}`; `full: false` = only what changed |
| `npc_act` | `npc`, `action`, `data` | ask the session leader's game to do something with a person (forwarded to the leader) |
| `avatar_set` | `avatar` | the game model you want to be shown as (a model name, kept for your next logins) |
| `pdata_set` | `key`, `value` | keep something that is yours alone in your current world (key `[a-z][a-z0-9_]*`, at most `max_player_data_bytes`); the mod keeps your inventory as `inv` |
| `bye` | | clean disconnect |

## Server -> client

| `t` | fields | meaning |
|---|---|---|
| `status` | `proto`, `version`, `name`, `motd`, `online`, `max`, `players`, `password`, `whitelist`, `tick_rate`, `tps`, `mspt`, `uptime`, `rev` | answer to `hello` with intent status |
| `login_ok` | `id`, `name`, `op`, `server`, `motd`, `tick_rate`, `tick`, `server_time`, `keepalive_interval`, `players` (`[{id,name,pose}]`), `rev`, `state` (whole shared world), `saves` (newest version of every server savegame), `token`? (only when the name was just claimed) | login accepted |
| `kick` | `reason` | the server closes the connection after this |
| `keepalive` | `id`, `st` (server unix ms) | every `keepalive_interval` s; no answer for `keepalive_timeout` s = kicked |
| `player_join` / `player_leave` | `id`, `name` (+ `pose` on join) | |
| `tick` | `n`, `p`: `{"<id>": [x,y,z,ry,anim,scene]}` | poses of players who moved since the last tick, never your own |
| `state` | `rev`, `set` (`{key: value}`), `del` (`[key]`) | delta of the shared world, sent on the next tick, also to the writer |
| `state_reject` | `key`, `reason`, `rev`, `value` | write refused: `invalid key`, `not allowed`, `conflict`, `value too large`, `too many keys`, `world data is full`; `value` is the current value |
| `state_ack` | `cid`, `key`, `value`, `applied`, `rev` | answer to `state_add`: new total, how much of `delta` was applied (clamping), and the world revision that contains it (the total reaches you in a `state` delta with `rev` >= this one) |
| `save_put_ok` / `save_stored` / `save_reject` | `slot`, ... | upload accepted / finished (`version`, `size`, `sha256`, `uploader`, `ts`, `note`) / refused (`reason`: `stale: ...`, `checksum mismatch`, `incomplete`, `invalid ...`, `no such save`) |
| `save_info`, `save_data`, `save_done` | `slot`, `version`, `size`, `sha256`, ... | the download: info, then the chunks, then done |
| `save_updated` | like `save_stored` | somebody else stored a new version |
| `save_list` | `saves` | answer to `save_list`; `login_ok` carries the same list as `saves` |
| `dlg_start` | `from`, `tree`, `person`, `prev` | another player in your world started this conversation (not sent to the starter) |
| `dlg_votes` | `round`, `options`, `votes` (`{player id: answer}`), `expected` (player ids), `ms_left` | the vote so far, after every vote; `ms_left` is the time until the window closes |
| `dlg_result` | `round`, `choice`, `random`, `votes` | the decision. Also sent again when somebody votes a round that is decided already (a client that was behind) |
| `dlg_reject` | `round`, `reason` | `not in a world`, `invalid vote`, or the number of answers differs from the others |
| `time_need` | | your world has no clock yet: send `time_init` |
| `time` | `seconds`, `rate`, `paused`, `reasons`, `jump` | the clock of your world, once a second and on every change; `jump: true` = somebody skipped time, set yours now |
| `time_reject` | `reason` | `not in a world`, `invalid time`, `no clock`, `jump too far` |
| `leader` | `world`, `id`, `name`, `handover` | who is the session leader of your world; sent when you enter and when it changes (`handover: true` = the old one left) |
| `npc_state` | `from`, `tick`, `full`, `npcs`, `at` | the leader's people (relayed, may be dropped when you are slow); a late joiner and a new leader get the last full one, with later partial updates merged in |
| `npc_act` | `from`, `npc`, `action`, `data` | to the leader only |
| `npc_reject` | `reason` | `not in a world`, `not the session leader`, `bad npcs`, `too many npcs` (`max_npcs`, `max_npc_bytes`), `no session leader`, `bad action` |
| `player_avatar` | `id`, `avatar` | a player chose a model (also sent back to you) |
| `avatar_reject` | `reason` | `bad avatar name` |
| `pdata` | `world`, `data` | everything the server keeps for you in the world you just entered (`join_world`) |
| `pdata_reject` | `reason` | `not in a world`, `bad key`, `too big` |
| `chat` | `id` (0 = server), `name`, `text` | |
| `event` | `from`, `kind`, `data` | |

## Rules

* **Names**: 2-24 characters, letters/digits/space/`.`/`-`/`_`. The first login of a name returns a `token`; the
  name is bound to it (the server stores only its SHA-256). Later logins must send it.
* **Shared-world keys**: `namespace/part[/part...]`, at most 128 characters, namespace `[a-z][a-z0-9_]*`.
  Players may write under the prefixes in `state_write_prefixes` (default `world/`, `shared/`), operators anywhere.
  Value <= 16 KiB, at most 4096 keys and 4 MiB in total. Event `data` <= 4 KiB.
* **Rate limits** (token buckets): moves 40/s (extra ones are dropped silently), chat 1/s with burst 5,
  `state_*` and `event` 30/s. Every refused packet is a strike; 100 strikes (10 are forgiven per keepalive
  interval) kick the player for flooding.
* **Ordering**: the server handles packets in arrival order on one event loop, so writes to the shared world
  have a total order and `expect` gives atomic claims (two players taking the same order: one wins, the other
  gets `state_reject` with `conflict` and the winner's value).

## Shared accounts and server savegames

* **Counters**: a key written only with `state_add` is a counter. Several players booking at the same moment all
  count, in arrival order; the server keeps an audit log (`ctl ledger`). A client that mirrors a counter into a
  game must keep its own bookings that are acknowledged but not yet inside the `state` delta it has seen
  (`ack.rev > last state rev`) added on top of the total, otherwise the value flickers.
* **Savegames**: slots hold up to `save_keep` versions (default 5). An upload only succeeds from the newest
  version (`base`), so nobody overwrites a newer save without having loaded it. Uploads and downloads never block
  the tick loop; a download streams with back pressure.

## Shared conversations

The clients run a conversation themselves (it is deterministic for the same world state); the server tells the others
that one started and decides every choice. A choice is decided as soon as **everybody who was in the world when the
vote opened has voted**, or **two different answers have been voted for**, or **`dialogue_vote_seconds` (default 5)
have passed since the first vote**. If the voters disagree, one of the *voted* answers is drawn at random, each voted
answer equally likely (the number of votes does not weigh). A player who leaves is no longer waited for.

## Shared world time

The server owns the clock of every world (like Minecraft's world time). The first client in a world starts it from
its loaded game (`time_init`: game seconds since the start and the speed it measured); from then on it runs on the
server while somebody is in the world and is sent to all once a second. A conversation holds it for everybody
(`time_pause`, one hold per player, expiring after `pause_timeout_seconds`); sleeping is a jump forward (`time_jump`).
Time never runs backwards. A client that is behind adds the missing seconds; one that is ahead holds its own time
(the game's pause lock) until the server has caught up. A personal menu pauses only that game, afterwards it catches
up. When the last player leaves, the world's clock is dropped (the world stands still, nobody is there).


## Session leader (shared people)

The game simulates its people (customers, staff, background NPCs) itself; the server cannot run that code. So one
game per world is the **session leader** (Unity Netcode calls this distributed authority): the first player who
enters the world. Its game simulates as in single player and sends `npc_state` about five times a second, only the
people near a player (interest management: those the game shows as a Character, plus the ghosts around players far
from the leader, the nearest first, at most 300). Every other game pauses its own simulation of people
(AgentGhostSimulator), puts its people where the leader has them, hides the ones the leader does not have and shows a
stand-in for the ones it has not got. When the leader leaves, the player who has been in the world longest takes
over (`leader` with `handover: true`) and gets the last snapshot to start from.

In a server world the games never save into the normal single-player saves: their saves and autosaves are stopped,
and the session leader's game uploads the world to the server instead (at most once a minute).

## Avatars

`login_ok` carries `avatar`: the model you chose on this server, or null on your first login (the mod then lets you
choose from the game's people models). `players` in `login_ok` and `player_join` carry every player's `avatar`.
`avatar_set` stores a new choice with the player and tells everybody (`player_avatar`).

## One savegame for everybody

Only the session leader's game saves a server world, so everything another player changes must also happen in the
leader's game:

* **Game functions** (buy, sell or rent a venue, apartment or greenhouse; hire, fire or re-task staff; buy ingredients,
  restock, buy furniture for a venue; greenhouse upgrades; unlocking places): the mod sends `npc_act` with
  `action: "rpc"` and `data: {m, i, a}` (the function, the object it is called on, the arguments as ids: data objects
  by name and type, people by guid, ghosts by id, managers by type, the acting player as `{"me": ...}`). The leader's
  game calls the same function; the money it spends doing so is given back, the actor already booked it.
* **Venue settings** (menu and prices, open or closed, every staff member's working hours, tasks and wage): the game
  that changed them sends an `event` `coop.venue` with the venue's whole settings; every game in the world, the
  leader's among them, takes them over.
* **Inventories** stay per player: the server keeps each player's inventory per world (`pdata` key `inv`) and the mod
  puts it into the game after the world has loaded.

After taking over somebody else's change the leader's game stores the world on the server (at most once a minute).
