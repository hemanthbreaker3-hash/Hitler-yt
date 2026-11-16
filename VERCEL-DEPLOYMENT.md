# Vercel deployment and live MongoDB sync

## Architecture

- Vercel serves the website domain and the bundled `/static/images/*` assets (including the logo), then proxies dynamic pages and APIs to the always-on FastAPI service.
- The VPS FastAPI service and Telegram bot use the same MongoDB database. The catalog collections are `catalog_pages` and `catalog_categories`; runtime player settings are in `site_settings`; shared URLs and panel credentials are in `settings`.
- The Vercel gateway reads the active `backend_url` from the same `settings` collection on each request. It does not require a redeploy when the Cloudflare Tunnel URL changes.
- HTML, catalog and settings responses are sent with no-cache headers so MongoDB changes appear on the next request/refresh. The logo and poster are packaged as Vercel public assets so they load even if the VPS static route is unavailable.

## Required Vercel environment variables

Set these for Production (and Preview if previews should work):

```env
MONGODB_URI=mongodb+srv://...
DATABASE_NAME=tg_media_bot
```

`MONGODB_URI` and `DATABASE_NAME` must exactly match the VPS bot/backend values. Do not put Telegram tokens, API hashes, or panel passwords in Vercel frontend variables.

## Deploy

1. Import the repository into Vercel with the project root as the Root Directory.
2. Use the included `vercel.json`; no build command is required for the Node gateway.
3. Add the two environment variables above and redeploy.
4. Keep the VPS FastAPI/bot process online and confirm its `/health` endpoint responds through the Cloudflare Tunnel.
5. Open the Vercel domain. Its URL is registered in MongoDB automatically.
6. Open `/panel` and sign in with the Telegram-configured `/username` and `/password` credentials.

## Live content sync

Publish or edit catalog pages using Telegram `/search` or the website `/panel`. Both write to the same MongoDB `catalog_pages` collection. Category edits use `catalog_categories`. The website reads the catalog from MongoDB through the FastAPI backend; refresh the page to see the latest saved content. Do not use a different MongoDB database on Vercel.

## Troubleshooting

- Logo: confirm `/static/images/logo.jpg` returns HTTP 200 on the Vercel domain. The file is included in `public/static/images/logo.jpg`.
- `503 Backend URL is not configured`: confirm the VPS saved `settings.backend_url` in the same database, or set `BACKEND_URL` as a temporary fallback.
- MongoDB configuration errors: verify Atlas network access allows Vercel serverless outbound connections and that the database user has read/write access.
- Pages not updating: confirm both VPS and Vercel use the same `MONGODB_URI` and `DATABASE_NAME`, then refresh; dynamic responses are configured not to be cached.
