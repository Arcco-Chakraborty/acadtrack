#!/usr/bin/env bash
# Example: VM=operator@host.example KEY=/path/to/private-key bash deploy/push.sh
# Set DOMAIN only when updating the OAuth callback. Review before server use.
set -euo pipefail
VM=${VM:?set VM to your deployment user and host}
KEY=${KEY:?set KEY to your SSH private-key path}
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH=(ssh -i "$KEY" -o ConnectTimeout=25)
STAGE=/tmp/acadtrack-stage
tar -C "$SRC" -czf - \
  --exclude=./data --exclude=./private --exclude=./.env --exclude=./.venv --exclude=./.git \
  --exclude=__pycache__ --exclude=.pytest_cache --exclude=./handouts \
  --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' --exclude='client_secret_*.json' \
  . | "${SSH[@]}" "$VM" "mkdir -p $STAGE && tar -C $STAGE -xzf -"
"${SSH[@]}" "$VM" "sudo rsync -a --delete --exclude data/ --exclude private/ --exclude .env --exclude .venv/ \
  $STAGE/ /opt/acadtrack/ && sudo DOMAIN='${DOMAIN:-}' bash /opt/acadtrack/deploy/install.sh"
"${SSH[@]}" "$VM" "systemctl is-active acadtrack"
