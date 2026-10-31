#!/usr/bin/env bash
# Copy the sources to a machine over ssh, build the image there and (re)start the container.
#   HOST=user@machine deploy/deploy.sh
# Optional: DENY=regex makes the script refuse hosts that match (a guard against deploying to the wrong machine).
set -euo pipefail

HOST=${HOST:?set HOST=user@machine}
if [[ -n "${DENY:-}" && "$HOST" =~ $DENY ]]; then echo "refusing: $HOST matches DENY" >&2; exit 1; fi

cd "$(dirname "$0")/.."
ssh "$HOST" 'mkdir -p ~/nivalis-coop'
rsync -a --delete --exclude __pycache__ --exclude '*.pyc' --exclude server.env server deploy "$HOST:nivalis-coop/"
ssh "$HOST" 'cd ~/nivalis-coop && chmod +x deploy/*.sh \
  && podman build -q -f deploy/Containerfile -t localhost/nivalis-coop:latest server/ \
  && deploy/run.sh'
