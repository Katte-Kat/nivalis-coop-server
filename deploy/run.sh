#!/usr/bin/env bash
# Start (or replace) the Nivalis Coop server as a rootless container.
# Settings come from deploy/server.env (see server.env.example), like the
# itzg/minecraft-server image takes them from environment variables.
set -euo pipefail

NAME=${NAME:-nivalis-coop}
IMAGE=${IMAGE:-localhost/nivalis-coop:latest}
BIND=${BIND:-0.0.0.0}
PORT=${PORT:-27800}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

env_args=()
[[ -f "$HERE/server.env" ]] && env_args=(--env-file "$HERE/server.env")

podman rm -f "$NAME" >/dev/null 2>&1 || true

# :U gives the named volume to the container user (a subuid of this user);
# the root filesystem is read-only, no capabilities, hard memory/pid limits.
podman run -d --name "$NAME" \
  --restart unless-stopped \
  --read-only --tmpfs /tmp:rw,size=16m \
  --cap-drop=ALL --security-opt no-new-privileges \
  --memory 256m --cpus 1 --pids-limit 100 \
  --health-cmd 'python -m nivalis_coop --healthcheck' \
  --health-interval 30s --health-start-period 10s --health-retries 3 \
  -e NIVALIS_COOP_PORT=27800 \
  "${env_args[@]}" \
  -v nivalis-coop-data:/data:U \
  -p "$BIND:$PORT:27800/tcp" \
  "$IMAGE"

echo "started $NAME on $BIND:$PORT  (logs: podman logs -f $NAME, console: podman exec $NAME python -m nivalis_coop ctl help)"
