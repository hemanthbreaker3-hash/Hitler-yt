# HITLER YT — Telegram Media Bot & OTT Website

A Telegram media-link bot and streaming backend with a blue, responsive HITLER YT website, MongoDB-backed catalog, TMDB publishing, and a Vercel gateway.

> **Deployment architecture:** Vercel hosts the public website/gateway. The Telegram bot and media backend run continuously on your VPS. MongoDB is the shared source of truth for the Vercel website URL and Cloudflare Tunnel backend URL. Vercel serverless functions do not run the Telegram bot or handle high-bandwidth media streams.

## Features

- Telegram bot media link generation, force-subscription checks, rate limits, media information, screenshots, sample video, deletion, admin stats and broadcast tools.
- FastAPI backend with Telegram media streaming, byte-range support, track discovery and remux endpoints.
- Responsive blue HITLER YT website and watch page.
- MongoDB-backed runtime settings and title catalog.
- Basic-auth protected `/panel` dashboard and `/admin` dashboard.
- Download, streaming, built-in player and best-effort Android VLC handoff settings.
- All-users/admins-only/owners-only access mode setting.
- TMDB search and one-click page publishing from Telegram `/search` and the admin dashboard.
- `/categories`, `/edit`, and `/panel` admin commands link to the secure web CMS.
- Default Movies and Web Series categories; custom categories can be created and deleted (default categories are protected).
- Movie/series pages support metadata, categories, groups and multiple buttons per group through the catalog API.

## 1. Configure the backend

Copy `.env.example` to `.env` and configure at minimum:

```env
API_ID=your_telegram_api_id
API_HASH=your_telegram_api_hash
BOT_TOKEN=your_bot_token
OWNER_ID=your_telegram_user_id
ADMINS=123456789,987654321
MONGODB_URI=mongodb+srv://...
DATABASE_NAME=tg_media_bot
BASE_URL=https://your-project.vercel.app
BACKEND_URL=
ADMIN_PANEL_TOKEN=use-a-long-random-secret
TMDB_API_KEY=your_tmdb_v3_api_key
```

`ADMIN_PANEL_TOKEN` protects the website dashboard using HTTP Basic authentication (username: `admin`). The VPS deploy script generates a strong token if one is not set; retrieve it from the private backend `.env` file. Never commit `.env` or expose the token in client-side code. TMDB search is disabled until `TMDB_API_KEY` is configured.

Optional settings are documented in `.env.example`. MongoDB must be reachable from the backend host. Redis is optional for deployments that do not use the cache integration.

## 2. Deploy the backend (VPS / Docker)

The bot and streaming backend must remain online. On a Linux VPS with Docker and Docker Compose installed:

```bash
cp .env.example .env
nano .env

docker compose up -d --build
```

Check the backend:

```bash
curl http://127.0.0.1:8000/health
```

Put the VPS backend behind HTTPS using Cloudflare Tunnel (for example `https://your-name.trycloudflare.com`) and forward it to the backend port (normally `8000`). Ensure Cloudflare Tunnel permits streaming responses and HTTP Range requests.

Alternatively, install Python dependencies and run `uvicorn app.main:app --host 0.0.0.0 --port 8000`. This is suitable for development; use a process supervisor for production.

## 3. Deploy the website gateway to Vercel (repository root)

1. Import this repository at [Vercel](https://vercel.com/new).
2. Set **Root Directory** to the repository root (do not select `vercel/`).
3. Add `MONGODB_URI` and `DATABASE_NAME` to Vercel, using exactly the same MongoDB URI and database name as the VPS bot. This is the only required Vercel configuration for URL discovery; the Vercel gateway reads the Cloudflare `backend_url` from MongoDB.
4. Deploy. No custom build command is required; the root `vercel.json` routes requests through the Node.js proxy in `api/[...path].js`.
5. On VPS deployment, the Cloudflare Quick Tunnel service automatically detects its generated HTTPS URL and saves it as both `base_url` and `backend_url` in MongoDB. You do not need to run `/backendurl` manually. When the Vercel website is first opened, it automatically registers its production and custom domains in MongoDB. `/panel` displays the detected domains. `/backendurl` is only an optional manual override.

The Vercel gateway redirects `/stream`, `/dl`, and `/remux` traffic to the backend so media bytes do not pass through a serverless function. Other requests (home page, watch page, static files, catalog and admin API) are proxied to the backend.

The Vercel gateway reads `settings.backend_url` from the shared MongoDB database on each request (with `BACKEND_URL` as an optional fallback). The VPS Cloudflare service refreshes this database value automatically whenever its Quick Tunnel URL changes. Changing `/backendurl` in Telegram is only needed for a manual override. Do not use the Vercel website URL as the backend URL: the backend URL must be the Cloudflare Tunnel URL that reaches the VPS.

## 4. Admin dashboard and commands

Open `https://your-project.vercel.app/panel` and authenticate with username `admin` and the `ADMIN_PANEL_TOKEN` password from the VPS `.env`. Vercel must have the same `MONGODB_URI` and `DATABASE_NAME` as the VPS; Vercel cannot discover the generated tunnel URL without access to that shared database. The URL card in the dashboard can also update both MongoDB URL settings.

- Toggle downloads and streaming independently.
- Toggle the built-in watch-page player.
- Toggle best-effort Android VLC auto handoff.
- Select the configured access mode.
- Create/delete custom categories.
- Search TMDB and publish movie/series pages. Movie pages are assigned to Movies; TV pages to Web Series.

Telegram admin commands:

- `/search title` — search TMDB and select a result to publish.
- `/categories` — open category management guidance.
- `/edit` — open page editor guidance.
- `/panel` — show primary Cloudflare website URL, Cloudflare backend URL, detected Vercel URL and all registered website domains.
- `/baseurl https://your-public-site.com` — manually set the primary public website URL in MongoDB.
- `/backendurl https://your-tunnel.trycloudflare.com` — manually set the Cloudflare Tunnel URL used by the Vercel gateway. VPS deployment normally sets this automatically.

Only Telegram IDs in `ADMINS` or the `OWNER_ID` can use these CMS commands. The web dashboard uses its separate Basic-auth secret.

## 5. Catalog API

- `GET /api/catalog` — published catalog.
- `GET /api/catalog?q=salaar` — title search.
- `GET /api/catalog?category=movies` — category filter.
- `GET /title/{slug}` — dedicated title page.
- `GET /api/site-settings` — public playback settings.
- `GET /api/admin/categories` — protected category list.
- `POST /api/admin/categories` — protected category creation.
- `DELETE /api/admin/categories/{slug}` — protected category deletion.
- `GET /api/admin/tmdb-search?q=title` — protected TMDB search.
- `POST /api/admin/pages` and `PUT /api/admin/pages/{slug}` — protected page publishing/editing.
- `PUT /api/admin/settings` — protected runtime settings.

All CMS writes are stored in MongoDB and survive process restarts. The page data model includes `groups: [{name, buttons: [{name, url}]}]`; keep button URLs absolute HTTP(S) URLs or relative same-site paths. Internal absolute links are rendered against the current forwarded website domain, so changing the Vercel domain does not require rewriting every internal button. Do not enter JavaScript URLs.

## 6. Player behavior and limitations

The built-in player can be hidden while external-player options remain on the Watch Online page. Auto handoff is best-effort: browsers do not expose a reliable API for enumerating installed external players. The current implementation attempts Android's VLC intent and uses the normal page as browser fallback. On iOS and desktop it leaves the normal page in place rather than claiming to detect an installed app it cannot verify.

## 7. Development and tests

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

For a local backend, set the environment variables above before starting the app. `/health` is the health-check endpoint.

## Security notes

- Keep bot tokens, API hashes, database URIs, TMDB keys and admin secrets in environment variables.
- Use HTTPS for both Vercel and the backend.
- Restrict backend ingress to trusted infrastructure where possible.
- Use a strong `ADMIN_PANEL_TOKEN`; rotate it if it has ever been shared.
- Configure `ADMINS` and `OWNER_ID` with numeric Telegram IDs.
- TMDB metadata is provided by TMDB; attribution and applicable TMDB terms should be followed.
