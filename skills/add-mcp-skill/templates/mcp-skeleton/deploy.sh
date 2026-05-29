#!/usr/bin/env bash
# =============================================================================
# deploy.sh — the human/code deploy path for the MCP server.
#
# Code/Dockerfile changes (server.py, requirements.txt, docker-compose.yml) need
# a rebuild; this is that path. Content-only skill-doc edits do NOT need this —
# the write tools push them to origin/main and the container reads them live off
# the mounted clone (see mcp-github-writeback.md).
#
# Invariants (shared with the write tools so both stay coherent on origin/main):
#   - refuse to deploy a dirty working tree
#   - refuse if local HEAD != origin/main  (commit + push first)
# On the VPS, it then does: git fetch + git reset --hard origin/main, rebuild.
#
# Usage:  ./deploy.sh            (run on the VPS, in the mcp-server dir)
# =============================================================================
set -euo pipefail

BRANCH="${BRANCH:-main}"

echo "[deploy] preflight: checking repo state..."

# --- preflight 1: clean working tree -----------------------------------------
if [ -n "$(git status --porcelain)" ]; then
  echo "[abort] working tree is dirty. Commit or stash before deploying." >&2
  git status --short >&2
  exit 1
fi

# --- preflight 2: local HEAD == origin/<branch> ------------------------------
git fetch origin "$BRANCH"
LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/$BRANCH")"
if [ "$LOCAL" != "$REMOTE" ]; then
  echo "[abort] local HEAD ($LOCAL) != origin/$BRANCH ($REMOTE)." >&2
  echo "        Push your commits (or pull) before deploying." >&2
  exit 1
fi

echo "[deploy] preflight OK (clean tree, HEAD == origin/$BRANCH)."

# --- sync to origin (authoritative) ------------------------------------------
# Hard-reset matches the write-tool invariant: origin/main is the source of truth.
echo "[deploy] syncing to origin/$BRANCH ..."
git reset --hard "origin/$BRANCH"

# --- rebuild + restart the container -----------------------------------------
echo "[deploy] rebuilding and restarting the MCP container ..."
docker compose build mcp
docker compose up -d mcp

echo "[deploy] done. Recent logs:"
docker logs mcp 2>&1 | tail -20
