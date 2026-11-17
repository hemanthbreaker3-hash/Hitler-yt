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


## Monetag service worker (Vercel)

The supplied Monetag `sw.js` is served from the required root URL. See [MONETAG-VERCEL-SETUP.md](MONETAG-VERCEL-SETUP.md) for production checks and the required dashboard ad-tag note.


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

## Streaming and download reliability notes

- Browser and external-player `/stream/` requests use the direct Telegram streaming path; helper-bot discovery/parallel striping is reserved for large `/dl/` requests so player startup and seeking are not delayed by probing every helper session.
- Large downloads may use parallel helper sessions, but any failed/empty helper chunk now falls back to the primary Telegram session for the remaining byte range rather than silently ending a partial response.
- The watch player uses metadata preload so opening a watch page does not immediately request an unnecessarily large playback buffer. The browser controls subsequent buffering and byte-range seeks.
- Keep reverse-proxy response buffering disabled for streaming endpoints. The included Nginx config already sets `proxy_buffering off`, `proxy_request_buffering off`, and HTTP/1.1 for upstream requests.
