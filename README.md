# HITLER YT — Telegram Media Bot + Vercel Website CMS

A Telegram media-link bot and responsive OTT-style website. The **Vercel website** serves the catalog, search, title pages, and admin control panel directly from shared **MongoDB** (so VPS firewall/IP rules never block website rendering), while **ngrok tunnel (`ngrok-free.app`)** handles all direct download links (`/dl/`), stream/watch links (`/watch/`, `/stream/`, `/remux/`), and title-page button URLs.

## Architecture

- **Vercel Website (`api/index.js` & `api/[...path].js`):** Serves the public website (`/`, `/search`, `/title/<slug>`), catalog APIs (`/api/catalog`, `/api/categories`, `/api/site-settings`, `/api/public/title/<slug>`), and the admin panel (`/panel`, `/api/admin/*`) directly from MongoDB without requiring proxy round-trips to the VPS.
- **ngrok tunnel (`https://*.ngrok-free.app`):** Exposes the VPS FastAPI streamer and Telegram bot over HTTPS. All download links (`/dl/<code>`), stream/watch links (`/watch/<code>`, `/stream/<code>`, `/remux/<code>`), and Watch/Download button URLs on title pages automatically use your active `ngrok-free.app` URL.
- **Shared MongoDB:** Stores media records (`files`), catalog pages (`catalog_pages`), categories (`catalog_categories`), runtime switches (`site_settings`), and shared URLs/credentials (`settings`).

## 1. Configuring the ngrok URL

Before deployment, run `export NGROK_AUTHTOKEN=YOUR_NGROK_AUTH_TOKEN` (replace the placeholder with your token from the [ngrok dashboard](https://dashboard.ngrok.com/get-started/your-authtoken)). The `deploy.vps` script saves the exported value into `.env`. You can alternatively set `NGROK_AUTHTOKEN` in `.env` directly. The app starts the tunnel automatically; check `docker compose logs -f web` for the generated HTTPS URL.


You can update your ngrok tunnel URL at any time in Telegram:

```text
/baseurl https://your-name.ngrok-free.app
```
or
```text
/backendurl https://your-name.ngrok-free.app
```

When you run `/baseurl` or `/backendurl` with a `https://*.ngrok-free.app` URL (or when the application starts its ngrok tunnel):
1. `ngrok_url`, `base_url`, `backend_url`, and the legacy `cloudflare_url` are updated in MongoDB.
2. All newly generated Telegram **Direct Download** (`/dl/<code>`) and **Watch Online** (`/watch/<code>`) links use the `ngrok-free.app` URL.
3. All existing and new **Watch** and **Download** button URLs on movie/series title pages (`/title/<slug>`) are automatically rewritten to use the active `ngrok-free.app` domain.

## 2. Deploy the Website to Vercel

1. Import the repository into Vercel and set the Root Directory to `HITLER YT-Main` (the folder containing `vercel.json`).
2. Add your Environment Variables in Vercel:
   - `MONGODB_URI` — the exact same MongoDB Atlas connection string used by your VPS bot.
   - `DATABASE_NAME` — optional (auto-detects `tg_media_bot` / `animovieszoneflix` automatically if omitted).
3. Deploy. Open `/`, `/api/health`, `/api/catalog`, `/search`, and `/panel` on your Vercel domain.

## Public tunnel (Cloudflare or ngrok)

Choose the VPS public tunnel with a shell export (or `.env`):

```bash
# Recommended: Cloudflare quick tunnel (*.trycloudflare.com) — no account token, no free-bandwidth cap
export TUNNEL_PROVIDER=cloudflare

# Or use ngrok (requires token)
export TUNNEL_PROVIDER=ngrok
export NGROK_AUTHTOKEN=YOUR_NGROK_AUTH_TOKEN
```

On startup the app generates the public HTTPS URL, writes it to MongoDB (`backend_url` / `ngrok_url` / `cloudflare_url`), rewrites catalog watch/download button domains, and keeps the tunnel alive. Watch logs: `docker compose logs -f web`.

## 3. Deploy the Bot & Streamer on the VPS

Configure `.env` (or run `./deploy.vps`) and start with Docker Compose:

```bash
docker compose up -d --build
```

## 4. Verification Checks

```bash
python3 -m compileall -q app
node --check 'api/[...path].js'
node --check 'api/index.js'
```


## Telegram message-link import

Send a public Telegram message link (for example `https://t.me/channelname/123`) or a private-channel message link (`https://t.me/c/123456789/123`) to the bot in a private chat. The bot attempts to fetch that exact message using its own Telegram access. If the message contains a supported document, video, audio, animation, photo, voice note, or video note, it creates the site's download and/or watch link against the original message.

This flow does **not** copy or forward the media into `CHANNEL_ID` or any storage channel. The bot must be able to access the source chat; for private channels, add the bot to the channel with permission to read messages. If Telegram denies access, the bot reports that instead of creating a broken link.





## Shortener key verification and external players

- Admins can use `/shorteners` to add multiple shortener API endpoints, enable/disable verification, and set key expiry (`300s`, `5m`, `2h`, `1d`, `1w`).
- With verification enabled, website GET pages require a page-scoped key. Keys remain reusable for that same page until expiry and cannot unlock a different path.
- The shortener API URL must be the provider's API endpoint and return a short URL as plain text or JSON (`shortenedUrl`, `short_url`, `shortlink`, or `url`). The API key is sent as the `api` query parameter and the destination as `url`.
- `/link` lets an admin request Download and Watch links by sending the command and then uploading/forwarding a Telegram media message. Existing media uploads continue to generate links automatically.
- The website panel's **Automatic player redirect** setting now opens the external-player chooser section on Android rather than silently cycling through apps. Android does not allow ordinary websites to enumerate installed packages, so the player list uses explicit Android intents and the OS handles unavailable apps.

## Shortener keys and existing Telegram message links

- Admins can use `/shorteners` to enable/disable verification, add multiple shortener API endpoints, and set key expiry (`300s`, `5m`, `2h`, `1d`, or `1w`).
- When enabled, website routes are gated by a reusable, page-scoped key. Keys are checked against the exact requested path and query string and stop working at their configured expiry.
- `/link https://t.me/channel/123` checks that the bot can access the Telegram message and that it contains supported media, then creates Download and Watch links against that original message. It does not copy the message into the storage channel. For private channel links, the bot must already have access to the channel.
- `/link` without an argument still accepts a file/media message in the next message.
- Automatic external-player chooser behavior is controlled by the existing **Automatic player redirect** setting in the admin panel. Web browsers cannot enumerate installed Android apps, so the page lists supported players and uses Android VIEW intents with package targeting and a browser fallback; the device decides whether the requested app is installed and can handle the media.


### Docker Compose compatibility

Use Docker Compose V2 (`docker compose`, with a space). Legacy `docker-compose` v1.29.2 may fail recreating containers with `KeyError: ContainerConfig`. For a one-time recovery, remove only the old web container with `docker-compose rm -sf web` and recreate it; do not delete volumes. See `UPGRADE-NOTES.md`.

### Admin page deletion from Telegram

Use `/del` in a private chat with the bot (admins only):

- `/del https://your-site/title/page-slug`
- `/del page-slug`
- `/del Exact Page Title`

Deletion is immediate with no confirmation step. The command uses exact slug or exact case-insensitive title matching; partial matches are rejected to prevent accidental deletion. It removes the page from the shared MongoDB `catalog_pages` collection.
