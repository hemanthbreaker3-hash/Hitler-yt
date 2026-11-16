#!/usr/bin/env bash
# Repair the legacy docker-compose v1 ContainerConfig failure safely.
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash fix-docker-compose.sh" >&2
  exit 1
fi
command -v docker >/dev/null 2>&1 || { echo "Docker Engine is not installed." >&2; exit 1; }

if ! docker compose version >/dev/null 2>&1; then
  echo "Installing Docker Compose v2 plugin..."
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y docker-compose-plugin
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y docker-compose-plugin
  else
    echo "Install the Docker Compose v2 plugin for your Linux distribution, then rerun this script." >&2
    exit 1
  fi
fi

docker compose version
[[ -f docker-compose.yml ]] || { echo "docker-compose.yml not found; run this from the File2link project folder." >&2; exit 1; }
[[ -f .env ]] || { echo ".env not found. Create it from .env.example and configure your credentials first." >&2; exit 1; }
docker compose --env-file .env config --quiet

echo "Removing only the old web service container. Named volumes are preserved; no volume deletion is performed."
docker compose --env-file .env rm -sf web || true
echo "Rebuilding and starting services with Compose v2..."
docker compose --env-file .env up -d --build --remove-orphans
echo "Current service status:"
docker compose --env-file .env ps
echo "Recent web logs:"
docker compose --env-file .env logs --tail=80 web
