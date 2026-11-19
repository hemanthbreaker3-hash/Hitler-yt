#!/usr/bin/env bash
set -Eeuo pipefail

# Rebuild/recreate ONLY the web service. Redis and named volumes are preserved.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
elif command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
  DOCKER=(sudo docker)
else
  echo "Docker daemon unavailable. Check: sudo systemctl status docker" >&2
  exit 1
fi

if "${DOCKER[@]}" compose version >/dev/null 2>&1; then
  COMPOSE=("${DOCKER[@]}" compose)
  echo "Using Docker Compose V2."
elif command -v docker-compose >/dev/null 2>&1; then
  if [[ "${DOCKER[0]}" == sudo ]]; then COMPOSE=(sudo docker-compose); else COMPOSE=(docker-compose); fi
  echo "Using legacy docker-compose. Removing stale web container first."
else
  echo "Docker Compose is not installed. Install Compose V2 and retry." >&2
  exit 1
fi

# Remove only this Compose project's web containers, never Redis or volumes.
PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$SCRIPT_DIR")}"
mapfile -t OLD_WEB < <("${DOCKER[@]}" ps -aq \
  --filter "label=com.docker.compose.project=${PROJECT}" \
  --filter "label=com.docker.compose.service=web")

if ((${#OLD_WEB[@])); then
  echo "Removing ${#OLD_WEB[@]} old web container(s): ${OLD_WEB[*]}"
  "${DOCKER[@]}" rm -f "${OLD_WEB[@]}"
else
  echo "No old web container found."
fi

echo "Building and starting upgraded web service..."
"${COMPOSE[@]}" up -d --build --no-deps web

echo
echo "Deployment status:"
"${COMPOSE[@]}" ps web
echo
echo "Recent web logs:"
"${COMPOSE[@]}" logs --tail=60 web
