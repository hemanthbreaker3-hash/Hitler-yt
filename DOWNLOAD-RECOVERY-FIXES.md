# Download and Cloudflare recovery

- Public `/dl/` and `/stream/` responses continue to advertise byte ranges and exact content lengths for browsers and download managers.
- Telegram transfers now reopen from the first byte not yet sent after transient Telegram/DC interruptions, with bounded exponential retries. Existing HTTP Range requests remain compatible with manual and automatic resume in Chrome, 1DM, ADM, and IDM.
- ngrok tunnel uses HTTP/2 to the edge and checks its public `/health` endpoint. After three consecutive failures (including Cloudflare 520/502/521/522 responses), the tunnel service exits so systemd can recreate the temporary tunnel and synchronize its newly assigned URL to MongoDB and `.env`.
- Uvicorn trusts forwarded headers from the container ingress so generated public links and request scheme handling remain consistent.

## Important production note

`ngrok-free.app` Quick Tunnels are temporary best-effort endpoints, not a guaranteed production hostname. The watchdog can recover from a broken tunnel, but it cannot prevent Cloudflare edge incidents. For a stable public URL, configure a Cloudflare Named Tunnel on a domain you control and point it to `http://127.0.0.1:8000`; use that fixed HTTPS hostname as `BACKEND_URL`/`CLOUDFLARE_URL`.

## v7 Speed + Response Update (FileToLink inspired)

- Download/stream first-byte latency kept minimal: primary client starts immediately.
- Subsequent 1 MiB stripes load-balanced round-robin across discovered helper bots/sessions when available (boosts Chrome single-connection speed).
- Multi-connection download managers (1DM/IDM/ADM) continue to benefit from per-Range client selection + higher MAX_CONCURRENT_PER_CLIENT (48).
- Response headers improved: Connection: keep-alive, Keep-Alive timeout, Cache-Control public max-age=3600 for better intermediary caching where safe.
- Multi-bot discovery probe timeout reduced to 1.0s to limit impact on TTFB.
- Download link structure unchanged: /dl/{short_code} (and /stream/, /watch/).

## v7.1 Buffering + Permanent Links

### External players & watch buffering
- `/stream/` and `/dl/` responses advertise full size via Content-Range on 200 responses so VLC / MX / Kodi / Just Player can seek and show duration immediately.
- Stream responses use `Cache-Control: no-store, no-transform, must-revalidate` to avoid stale partial-range caching that causes rebuffering.
- `X-Accel-Buffering: no` + keep-alive retained so reverse proxies do not buffer the body.
- Concrete `video/mp4` Content-Type is preferred for stream paths when the stored mime is generic, improving external player compatibility.
- Built-in watch player: `preload="auto"`, stalled/waiting status, and automatic reconnect with resume on transient media errors (up to 4 attempts).

### Links work permanently
- `short_code` entries in MongoDB have no TTL / expiry (legacy expiry index is dropped on startup). Links remain valid forever as long as the file record and Telegram media exist.
- Watch page always rewrites stream/download origins to the hostname currently serving the page (trycloudflare / named tunnel / localhost). Opening an old watch URL on a new tunnel still produces working player and external-player links.
- For a stable public hostname (recommended for production), configure a Cloudflare Named Tunnel and set `BACKEND_URL` / `CLOUDFLARE_URL` to that fixed HTTPS domain instead of relying on temporary ngrok-free.app URLs.
