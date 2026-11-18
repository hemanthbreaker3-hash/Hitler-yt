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

## v7.2 — FileStore Bot Token (`/filestore`) & Official Movi-Player Multi-Audio Upgrade

- **FileStore Bot Token (`/filestore <BOT_TOKEN>`):**
  - Run `/filestore 123456789:ABCdefGHI...` (or `/filestore` → **🔑 Set / Change FileStore Bot Token**) to validate, save to MongoDB, and start the configured FileStore bot automatically with live `/start` deep-link listeners (`no_updates=False`).
  - When **Downloads** are disabled in `/panel` or via `/filestore`, every generated download link (`/link`, media uploads, `/batch`), `/dl/<short_code>` route, and title/watch page Download button automatically uses `https://t.me/<filestore_bot_username>?start=file_<short_code>`.
  - Clicking the deep link redirects the user to the configured FileStore bot and delivers the file directly in Telegram chat.
- **Official Movi-Player v0.4.1 Multi-Audio & Built-In Player ([MrUjjwalG/movi-player](https://github.com/mrujjwalg/movi-player) · [moviplayer.com](https://moviplayer.com/)):**
  - Integrated `<movi-player>` v0.4.1 (`https://cdn.jsdelivr.net/npm/movi-player@0.4.1/dist/element.global.js` and `https://moviplayer.com/dist/element.js`) with `engine="wasm shaka hlsjs dashjs native"`, `sw="auto"`, `fallback="native"`, `buffersize="256"`, `probesize="2mb"`, `probeduration="5000"`, `thumb`, `resume`, `fastseek`, `doubletap`, `stablevolume`, `hdr`, `ambientmode`, and `subtitlepicker`.
  - Supports both in-player multi-audio/subtitle switching (FFmpeg WASM + WebCodecs for MKV/MP4/HEVC/AV1/AC3/E-AC3/DTS/SRT/ASS) and an on-page **Audio Track Selector** + **Server Remux (AAC Multi-Audio)** mode with instant timestamp seeking over your active Cloudflare `trycloudflare.com` URL.
  - Supports both `/watch/<short_code>` (including `?hash=...`) and `/watch?path=<short_code>` routes while keeping full **HITLER YT** branding.
