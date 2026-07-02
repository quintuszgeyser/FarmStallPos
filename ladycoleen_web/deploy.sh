#!/bin/bash
# Deploy ladycoleen_web via GitHub → server (same pattern as POS).
# Usage: ./deploy.sh
#   1. Commits + pushes to GitHub main
#   2. SSHs to server and runs deploy.sh web

set -e
cd "$(dirname "$0")"

if [[ -z "$(git status --porcelain)" ]]; then
  echo "[deploy] Nothing to commit - pushing current HEAD to GitHub..."
else
  MSG="${1:-Update $(date '+%Y-%m-%d %H:%M')}"
  git add -A
  git commit -m "$MSG"
fi

echo "[deploy] Pushing to GitHub..."
git push origin main

echo "[deploy] Deploying on server (GitHub pull + docker build)..."
ssh farmpc "~/farmpos-docker/deploy.sh web"

echo "[deploy] Done."
