#!/usr/bin/env bash
# Managed by deploy.vps. Starts a Cloudflare Quick Tunnel and automatically synchronizes its
# generated HTTPS trycloudflare.com URL into .env and MongoDB for download, stream, and button URLs.
set -Eeuo pipefail

SCRIPT_DIR="${FILE2LINK_DIR:-$(pwd)}"
if [[ ! -f "$SCRIPT_DIR/docker-compose.yml" ]]; then
  SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
fi
cd "$SCRIPT_DIR"
PORT_VALUE="${PORT:-8000}"
# Origin host for cloudflared --url. Use a public VPS IP when the app must be
# reached via that interface instead of loopback, e.g.:
#   export tryurl=138.68.175.206
#   export TRYURL=138.68.175.206
# Default remains 127.0.0.1 for local / same-host tunnels.
ORIGIN_HOST="${TRYURL:-${tryurl:-127.0.0.1}}"
ORIGIN_HOST="${ORIGIN_HOST#http://}"
ORIGIN_HOST="${ORIGIN_HOST#https://}"
ORIGIN_HOST="${ORIGIN_HOST%%/*}"
ORIGIN_HOST="${ORIGIN_HOST%%:*}"
ORIGIN_URL="http://${ORIGIN_HOST}:${PORT_VALUE}"
LOG_FILE="$SCRIPT_DIR/cloudflared-quick-tunnel.log"
COMPOSE=(docker compose --project-directory "$SCRIPT_DIR" -f "$SCRIPT_DIR/docker-compose.yml" --env-file "$SCRIPT_DIR/.env")
if ! docker info >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
  COMPOSE=(sudo docker compose --project-directory "$SCRIPT_DIR" -f "$SCRIPT_DIR/docker-compose.yml" --env-file "$SCRIPT_DIR/.env")
fi

log() { printf '[File2link Cloudflare] %s\n' "$*"; }

if [[ ! -x /usr/local/bin/cloudflared ]]; then
  log 'cloudflared binary is missing; the deployment installer must install it first.'
  exit 1
fi

log "Health-check origin: ${ORIGIN_URL}/health (TRYURL/tryurl=${ORIGIN_HOST})"
for _ in $(seq 1 90); do
  if curl -fsS "${ORIGIN_URL}/health" >/dev/null 2>&1; then
    break
  fi
  # Also accept loopback readiness if the public IP is not yet bound/routable locally
  if curl -fsS "http://127.0.0.1:${PORT_VALUE}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
if ! curl -fsS "${ORIGIN_URL}/health" >/dev/null 2>&1 && ! curl -fsS "http://127.0.0.1:${PORT_VALUE}/health" >/dev/null 2>&1; then
  log "File2link health endpoint is not ready on ${ORIGIN_URL} or 127.0.0.1:${PORT_VALUE}; refusing to start the public tunnel."
  exit 1
fi

: > "$LOG_FILE"
log "Starting Quick Tunnel to ${ORIGIN_URL}"
/usr/local/bin/cloudflared tunnel --no-autoupdate --protocol http2 --loglevel info --url "${ORIGIN_URL}" >"$LOG_FILE" 2>&1 &
CF_PID=$!
cleanup() {
  if kill -0 "$CF_PID" 2>/dev/null; then kill "$CF_PID" 2>/dev/null || true; fi
  wait "$CF_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

TUNNEL_URL=''
for _ in $(seq 1 90); do
  if ! kill -0 "$CF_PID" 2>/dev/null; then
    log 'cloudflared exited before creating a tunnel.'
    tail -n 80 "$LOG_FILE" || true
    exit 1
  fi
  TUNNEL_URL="$(grep -Eo 'https://[[:alnum:]-]+\.trycloudflare\.com' "$LOG_FILE" | tail -n 1 || true)"
  if [[ -n "$TUNNEL_URL" ]]; then break; fi
  sleep 2
done

if [[ -z "$TUNNEL_URL" ]]; then
  log 'Timed out waiting for the trycloudflare.com URL. Recent cloudflared output:'
  tail -n 100 "$LOG_FILE" || true
  exit 1
fi

log "Generated public Cloudflare Try URL: $TUNNEL_URL"

# Update .env with the generated Cloudflare Try URL.
python3 - "$SCRIPT_DIR/.env" "$TUNNEL_URL" <<'PY'
import json, pathlib, re, sys
path = pathlib.Path(sys.argv[1])
url = sys.argv[2]
lines = path.read_text().splitlines() if path.exists() else []
new_lines = []
found_base = False
found_backend = False
found_cf = False
for line in lines:
    if re.match(r'^\s*BASE_URL\s*=', line):
        if not found_base:
            new_lines.append('BASE_URL=' + json.dumps(url))
            found_base = True
    elif re.match(r'^\s*BACKEND_URL\s*=', line):
        if not found_backend:
            new_lines.append('BACKEND_URL=' + json.dumps(url))
            found_backend = True
    elif re.match(r'^\s*CLOUDFLARE_URL\s*=', line):
        if not found_cf:
            new_lines.append('CLOUDFLARE_URL=' + json.dumps(url))
            found_cf = True
    else:
        new_lines.append(line)
if not found_base:
    new_lines.append('BASE_URL=' + json.dumps(url))
if not found_backend:
    new_lines.append('BACKEND_URL=' + json.dumps(url))
if not found_cf:
    new_lines.append('CLOUDFLARE_URL=' + json.dumps(url))
path.write_text('\n'.join(new_lines) + '\n')
path.chmod(0o600)
PY

# Persist the Cloudflare Try URL in MongoDB and automatically rewrite all catalog button URLs.
# Note: website_urls is kept strictly for the Vercel website (never trycloudflare.com).
if ! "${COMPOSE[@]}" exec -T web python -c '
import asyncio, datetime, os, sys
from motor.motor_asyncio import AsyncIOMotorClient
from app.utils.helpers import is_trycloudflare_url, rewrite_page_groups_to_cloudflare

async def main():
    uri = os.environ.get("MONGODB_URI") or os.environ.get("DATABASE_URL")
    if not uri:
        raise RuntimeError("MONGODB_URI/DATABASE_URL is missing inside web container")
    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=15000)
    await client.admin.command("ping")
    db = client[os.environ.get("DATABASE_NAME", "tg_media_bot")]
    now = datetime.datetime.now(datetime.timezone.utc)
    tunnel_url = sys.argv[1].strip().rstrip("/")
    for key in ("base_url", "backend_url", "cloudflare_url"):
        await db.settings.update_one(
            {"key": key},
            {"$set": {"value": tunnel_url, "updated_by": "cloudflare-deployment", "updated_at": now}},
            upsert=True,
        )
    web_doc = await db.settings.find_one({"key": "website_urls"}, {"value": 1})
    if web_doc and isinstance(web_doc.get("value"), list):
        cleaned = [u for u in web_doc["value"] if not is_trycloudflare_url(str(u))]
        await db.settings.update_one(
            {"key": "website_urls"},
            {"$set": {"value": cleaned, "updated_at": now}},
        )
    async for doc in db.catalog_pages.find({"groups.0": {"$exists": True}}, {"_id": 1, "groups": 1}):
        old_groups = doc.get("groups") or []
        new_groups = rewrite_page_groups_to_cloudflare(old_groups, tunnel_url)
        if new_groups != old_groups:
            await db.catalog_pages.update_one(
                {"_id": doc["_id"]},
                {"$set": {"groups": new_groups, "updated_at": now}},
            )
    client.close()

asyncio.run(main())
' "$TUNNEL_URL"; then
  log 'Could not persist Cloudflare URL via compose exec; internal FastAPI log watcher will synchronize it automatically.'
fi

log "Auto-set Cloudflare URL in .env and MongoDB: $TUNNEL_URL"
log "All download links, stream links, and title-page button URLs now use this Cloudflare Try URL, while Vercel serves the website."

# Quick Tunnels are ephemeral and can occasionally lose their edge-to-origin connection.
# Watch the public health endpoint; after repeated failures exit so systemd recreates the
# tunnel, obtains a fresh hostname, and synchronizes it to MongoDB/.env automatically.
FAILURES=0
while kill -0 "$CF_PID" 2>/dev/null; do
  sleep 20
  if ! kill -0 "$CF_PID" 2>/dev/null; then
    break
  fi
  HTTP_CODE="$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 8 --max-time 15 "$TUNNEL_URL/health" 2>/dev/null || true)"
  if [[ "$HTTP_CODE" == "200" ]]; then
    FAILURES=0
  else
    FAILURES=$((FAILURES + 1))
    log "Public tunnel health check failed (HTTP ${HTTP_CODE:-no-response}, ${FAILURES}/3)."
    if (( FAILURES >= 3 )); then
      log 'Public tunnel is unhealthy; restarting it through systemd to obtain a fresh URL.'
      kill "$CF_PID" 2>/dev/null || true
      wait "$CF_PID" 2>/dev/null || true
      exit 1
    fi
  fi
done
log 'cloudflared process exited; systemd will restart the tunnel and resynchronize its URL.'
exit 1
