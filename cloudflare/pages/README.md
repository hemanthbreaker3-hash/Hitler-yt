# Cloudflare Pages deployment (same website as Vercel)

This Pages project is a thin reverse-proxy to the same always-on FastAPI backend used by Vercel. It serves the exact same templates, title pages, admin panel, catalog APIs, player and download routes; MongoDB remains the shared source of truth. It does not run the Telegram bot inside Pages.

1. Create a Cloudflare Pages project and connect this repository.
2. Set the Pages project root/build output to `cloudflare/pages` (no build command; output directory `public`).
3. Add the Pages environment variable `BACKEND_URL` with the public HTTPS Cloudflare Tunnel/VPS backend origin, e.g. `https://your-tunnel.example.com`. Do not point it at the Pages domain itself.
4. Deploy and test `/health`, `/`, `/panel`, `/api/site-settings`, `/watch/<code>` and `/dl/<code>`.
5. Keep the VPS backend and MongoDB running. Download/watch traffic is streamed from the VPS backend; Pages is not a media-storage service.

The `BACKEND_URL` must be stable and reachable from Cloudflare. For production, use a named Cloudflare Tunnel hostname rather than an ephemeral `trycloudflare.com` quick tunnel.
