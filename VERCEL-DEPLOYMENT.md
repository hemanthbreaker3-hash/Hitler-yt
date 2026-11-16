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

`MONGODB_URI` and `DATABASE_NAME` must exactly match the VPS bot/backend values. Do not put Telegram tokens, API hashes, or panel passwords in Vercel frontend variables. The Vercel gateway reads public catalog data directly from this shared database. For rendered HTML pages, the admin panel, and Telegram media routes, a reachable public VPS/Cloudflare backend URL must also be saved in MongoDB as the `backend_url` setting (or supplied as the optional `BACKEND_URL` Vercel environment variable).

## Deploy

1. Import the repository into Vercel with the project root as the Root Directory.
2. Use the included `vercel.json`; no build command is required for the Node gateway.
3. Add the two environment variables above and redeploy.
4. Keep the VPS FastAPI/bot process online and confirm its `/health` endpoint responds through the Cloudflare Tunnel.
5. Open the Vercel domain. Its URL is registered in MongoDB automatically.
6. Open `/panel` and sign in with the Telegram-configured `/username` and `/password` credentials.

## Live content sync

Publish or edit catalog pages using Telegram `/search` or the website `/panel`. Both write to the same MongoDB `catalog_pages` collection. Category edits use `catalog_categories`. The Vercel catalog endpoints read directly from MongoDB; the VPS FastAPI site reads the same collections. Refresh the page to see the latest saved content. Do not use a different MongoDB database on Vercel.

## Troubleshooting

- Logo: confirm `/static/images/logo.jpg` returns HTTP 200 on the Vercel domain. The file is included in `public/static/images/logo.jpg`.
- `503 Backend URL is not configured`: confirm the VPS saved `settings.backend_url` in the same database, or set `BACKEND_URL` as a temporary fallback.
- MongoDB configuration errors: verify Atlas network access allows Vercel serverless outbound connections and that the database user has read/write access.
- Pages not updating: confirm both VPS and Vercel use the same `MONGODB_URI` and `DATABASE_NAME`, then refresh; dynamic responses are configured not to be cached.

## Shared catalog page synchronization

Catalog pages are stored in the MongoDB collection `catalog_pages` in the database selected by `DATABASE_NAME` (default: `tg_media_bot`). The Telegram bot's `/search` publish action and the web panel's create/edit/delete actions write to this same collection. Public catalog, search, and title routes read from that collection on each request; the Vercel gateway disables caching for proxied HTML/API responses. Therefore a page created or edited through Telegram, the VPS website, or the Vercel website is shared automatically without a Vercel redeploy.

For this to work, the VPS and Vercel must use the exact same `MONGODB_URI` and `DATABASE_NAME`. The Vercel public catalog endpoints (`/api/catalog`, `/api/categories`, `/api/site-settings`, `/api/public/title/{slug}`) read MongoDB directly, so title listing does not depend on the VPS proxy. The homepage, rendered `/title/{slug}` and `/search` HTML, and admin panel still use the VPS backend proxy. The rest of the rendered website, admin operations, and Telegram media streaming use the VPS/Cloudflare backend URL stored as `settings.key=backend_url`; keep that backend online and publicly reachable. If separate MongoDB databases or Atlas clusters are configured, page changes will not be shared.

The panel supports page creation, editing metadata/groups/buttons, and deleting pages. Page writes include `updated_at` timestamps. The Telegram bot's page publish and category assignment actions also update the shared collection.


## HTTP 500 / routing diagnostics

- The homepage rewrite `/` is normalized to the backend root `/` (not `/index`).
- Open `/api/health` to check whether the Vercel function can connect to MongoDB and whether a backend URL is configured.
- For function-side errors, inspect Vercel → Project → Deployments → Functions → Logs.
- A `503` from `/api/health` indicates MongoDB environment, Atlas network-access or database-permission configuration; a healthy database with `backend_configured: false` means the backend URL is missing.
- Never configure `BACKEND_URL` to the Vercel domain itself. That creates a proxy loop; use the separate public Cloudflare Tunnel hostname for the VPS.
- Cloudflare Pages can serve the same rendered website through the Pages gateway in `cloudflare/pages`; see its README for its `BACKEND_URL` setting.
