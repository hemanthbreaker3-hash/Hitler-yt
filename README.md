# HITLER YT — Telegram Media Bot + Website CMS

A Telegram media-link bot and responsive OTT-style website. The VPS runs the Telegram bot and FastAPI media backend; MongoDB stores media-link records, website pages, categories and shared settings. Vercel and Cloudflare Pages are two public website gateways to the same backend and MongoDB data.

## Important architecture

- **VPS / Docker:** runs the Telegram bot, FastAPI website, streaming/download endpoints and Telegram sessions. This process must remain online.
- **MongoDB:** persistent shared database for link metadata, catalog pages, categories, panel credentials and settings. Use the exact same cluster and `DATABASE_NAME` on the VPS and Vercel.
- **Vercel:** Node.js gateway. It reads public catalog data from MongoDB and proxies rendered pages, admin actions and media redirects to the VPS backend. It does not run the Telegram bot or store media bytes.
- **Cloudflare Pages:** optional alternate website gateway in `cloudflare/pages`. It proxies all routes to the same FastAPI backend, so the pages and content match the Vercel website.
- **Cloudflare Tunnel:** exposes the VPS backend over HTTPS. Use a stable named tunnel hostname in production.

A download/watch link has no application-level expiry by default. It remains usable while its MongoDB record exists, the original Telegram message/file remains accessible, the Telegram session is authorized, and the VPS backend is online. This cannot protect against deletion of the source media or an upstream Telegram/account outage.

## 1. Configure the VPS

Copy `.env.example` to `.env` and fill in your real values:

```env
API_ID=your_telegram_api_id
API_HASH=your_telegram_api_hash
BOT_TOKEN=your_bot_token
OWNER_ID=your_telegram_user_id
ADMINS=123456789,987654321
MONGODB_URI=mongodb+srv://USER:PASSWORD@CLUSTER/...
DATABASE_NAME=tg_media_bot
BASE_URL=https://your-public-website.example
BACKEND_URL=https://your-stable-cloudflare-tunnel.example
ADMIN_PANEL_TOKEN=use-a-long-random-secret
TMDB_API_KEY=your_tmdb_v3_api_key
```

Generated links do not receive an expiry timestamp. The application removes its legacy MongoDB TTL index at startup so MongoDB cannot silently delete records due to an old expiry index. A legacy `DEFAULT_EXPIRY` environment variable is ignored.

Install and start:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

For Docker, configure `.env` and run `docker compose up -d --build`. Check `https://YOUR_BACKEND/health` returns `{"status":"ok","service":"HITLER"}`.

## 2. Deploy to Vercel

1. Import the repository and set the project root to `File2link-Main` (the folder containing `vercel.json`).
2. Set Vercel environment variables for Production (and Preview if required):
   - `MONGODB_URI` — same MongoDB connection string used by the VPS.
   - `DATABASE_NAME` — same database name used by the VPS, normally `tg_media_bot`.
   - Optional `BACKEND_URL` — stable public HTTPS URL of the VPS/Cloudflare Tunnel. The preferred value is the MongoDB `settings` record with `key=backend_url`.
3. Ensure MongoDB Atlas Network Access allows Vercel connections and the database user can read/write the configured database.
4. Deploy. The Node gateway requires no build command. `/api/health` checks MongoDB connectivity and whether the backend URL is configured.
5. Test `/`, `/api/health`, `/api/catalog`, `/panel`, `/watch/<code>` and `/dl/<code>`.

If Vercel reports a 500, inspect **Vercel → Project → Deployments → Functions → Logs**. `/api/health` helps distinguish missing/invalid MongoDB configuration from a backend URL issue. Do not use the Vercel website URL as `BACKEND_URL`; that causes a proxy loop. The backend URL must point to the VPS service through a separate tunnel hostname.

## 3. Deploy the same website on Cloudflare Pages (optional)

1. Create a Cloudflare Pages project connected to this repository.
2. Set the project root to `cloudflare/pages`, build command empty, and output directory `public`.
3. Add the Pages environment variable `BACKEND_URL` with the stable public HTTPS Cloudflare Tunnel URL that reaches the VPS. It must not be the Pages website URL.
4. Deploy. The Pages Function proxies every route to the same FastAPI backend, so `/`, `/search`, `/title/...`, `/panel`, APIs, player, downloads and watch pages use the same templates and MongoDB content as Vercel.

Cloudflare Pages is a website gateway, not the place to run the Python Telegram bot or hold Telegram media. Keep the VPS service online.

## 4. Website editor and Telegram commands

Open `/panel` and sign in with your configured panel username/password. For first login, the default username is `admin` and the password is `ADMIN_PANEL_TOKEN` until custom credentials are set. Set credentials using the Telegram owner/admin commands:

- `/username your_username`
- `/password your_strong_password`
- `/panel` — display website/backend URLs and panel link.
- `/manage` — open the management dashboard.
- `/manage page title` — search for a page by title and open its editor.
- `/manage https://site.example/title/page-slug` — select a page by pasted title-page URL.
- `/edit` — open the page editor.
- `/search title` — search TMDB and publish a movie/series page.
- `/categories` — category management.
- `/baseurl https://your-site.example` — set the public website base URL.
- `/backendurl https://your-tunnel.example` — set the VPS backend URL.

The panel page editor can search by title/slug or accept a pasted `/title/<slug>` URL. It supports page metadata, categories, Watch/Download groups and multiple buttons per group. Changes are stored in MongoDB and shared across both gateways.

## 5. Permanent media links and player behavior

New media records use `expiry_time: null` by default. `/watch/<code>`, `/dl/<code>`, `/stream/<code>` and track/remux lookups no longer reject a record just because an old expiry timestamp is present. The MongoDB TTL index is removed during backend startup. Links can still stop working if the source Telegram message is deleted, Telegram access is revoked, the bot session is invalid, the MongoDB record is deleted, or the backend is offline.

The automatic Android external-player option is best-effort: it attempts Playit, then MX Player, then VLC. Browsers do not provide a dependable installed-app inventory or guaranteed app-launch result. iOS and desktop browsers remain on the built-in/browser playback path. Users can still choose external-player links manually where available.

## 6. Maintenance checks

Run `python -m compileall -q app` and `node --check 'api/[...path].js'` after code changes. Keep deployment secrets out of source control.

Do not commit `.env`, Telegram credentials, MongoDB URIs, TMDB keys or panel passwords. Use HTTPS, a stable tunnel hostname and a strong unique panel password.
