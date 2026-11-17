# Download and Cloudflare recovery

- Public `/dl/` and `/stream/` responses continue to advertise byte ranges and exact content lengths for browsers and download managers.
- Telegram transfers now reopen from the first byte not yet sent after transient Telegram/DC interruptions, with bounded exponential retries. Existing HTTP Range requests remain compatible with manual and automatic resume in Chrome, 1DM, ADM, and IDM.
- Cloudflare Quick Tunnel uses HTTP/2 to the edge and checks its public `/health` endpoint. After three consecutive failures (including Cloudflare 520/502/521/522 responses), the tunnel service exits so systemd can recreate the temporary tunnel and synchronize its newly assigned URL to MongoDB and `.env`.
- Uvicorn trusts forwarded headers from the container ingress so generated public links and request scheme handling remain consistent.

## Important production note

`trycloudflare.com` Quick Tunnels are temporary best-effort endpoints, not a guaranteed production hostname. The watchdog can recover from a broken tunnel, but it cannot prevent Cloudflare edge incidents. For a stable public URL, configure a Cloudflare Named Tunnel on a domain you control and point it to `http://127.0.0.1:8000`; use that fixed HTTPS hostname as `BACKEND_URL`/`CLOUDFLARE_URL`.
