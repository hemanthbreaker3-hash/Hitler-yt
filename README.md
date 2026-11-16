# HITLER YT — Telegram Media Bot + Vercel Website CMS

A Telegram media-link bot and responsive OTT-style website. The **Vercel website** serves the catalog, search, title pages, and admin control panel directly from shared **MongoDB** (so VPS firewall/IP rules never block website rendering), while **Cloudflare Tunnel (`trycloudflare.com`)** handles all direct download links (`/dl/`), stream/watch links (`/watch/`, `/stream/`, `/remux/`), and title-page button URLs.

## Architecture

- **Vercel Website (`api/index.js` & `api/[...path].js`):** Serves the public website (`/`, `/search`, `/title/<slug>`), catalog APIs (`/api/catalog`, `/api/categories`, `/api/site-settings`, `/api/public/title/<slug>`), and the admin panel (`/panel`, `/api/admin/*`) directly from MongoDB without requiring proxy round-trips to the VPS.
- **Cloudflare Quick Tunnel (`https://*.trycloudflare.com`):** Exposes the VPS FastAPI streamer and Telegram bot over HTTPS. All download links (`/dl/<code>`), stream/watch links (`/watch/<code>`, `/stream/<code>`, `/remux/<code>`), and Watch/Download button URLs on title pages automatically use your active `trycloudflare.com` URL.
- **Shared MongoDB:** Stores media records (`files`), catalog pages (`catalog_pages`), categories (`catalog_categories`), runtime switches (`site_settings`), and shared URLs/credentials (`settings`).

## 1. Updating the Cloudflare `trycloudflare.com` URL

You can update your Cloudflare Quick Tunnel URL at any time in Telegram:

```text
/baseurl https://your-name.trycloudflare.com
```
or
```text
/backendurl https://your-name.trycloudflare.com
```

When you run `/baseurl` or `/backendurl` with a `https://*.trycloudflare.com` URL (or when `cloudflare-quick-tunnel.sh` starts on the VPS):
1. `base_url`, `backend_url`, and `cloudflare_url` are updated in MongoDB.
2. All newly generated Telegram **Direct Download** (`/dl/<code>`) and **Watch Online** (`/watch/<code>`) links use the `trycloudflare.com` URL.
3. All existing and new **Watch** and **Download** button URLs on movie/series title pages (`/title/<slug>`) are automatically rewritten to use the active `trycloudflare.com` domain.

## 2. Deploy the Website to Vercel

1. Import the repository into Vercel and set the Root Directory to `File2link-Main` (the folder containing `vercel.json`).
2. Add your Environment Variables in Vercel:
   - `MONGODB_URI` — the exact same MongoDB Atlas connection string used by your VPS bot.
   - `DATABASE_NAME` — optional (auto-detects `tg_media_bot` / `file2link` automatically if omitted).
3. Deploy. Open `/`, `/api/health`, `/api/catalog`, `/search`, and `/panel` on your Vercel domain.

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
