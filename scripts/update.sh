#!/usr/bin/env bash
# update.sh — Pull the latest code and restart the service.
#
# Usage:
#   ./scripts/update.sh
#
# The script must be run from the root of the repository.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="email-to-motion"

echo "==> Pulling latest code…"
cd "$REPO_DIR"
git pull

echo "==> Restarting service…"
sudo systemctl restart "$SERVICE"

echo "==> Done. Service status:"
systemctl status "$SERVICE" --no-pager
