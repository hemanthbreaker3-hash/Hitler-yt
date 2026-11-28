# HITLER YT

High-speed Telegram media streaming, FileStore delivery, and modern catalog website powered by **FastAPI**, **wzgram**, **MongoDB**, **Redis**, **Movi-Player**, and **Caddy / Vercel / Cloudflare Pages**.

- **Updates Channel:** https://t.me/HiTLERYT

---

## Key Features

- **Watch Player (`app/templates/watch.html`)**
  - Defaults to **Movi-Player v0.4.1** with automatic **Built-in HTML5 `<video>`** fallback.
  - Full-bleed edge-to-edge mobile vertical player, responsive landscape fit, touch-scrollable **Switch** & **External Player** bottom sheets, **Fit/Fill**, **Retry**, **Hide/Show Controls**, **Fullscreen** (native + CSS fallback), and dynamic **Episodes / Seasons / EXTRA** selector.
  - Multi-connection HTTP Range (`206 Partial Content`) streaming and live FFmpeg AAC `/remux/` fallback.
- **Catalog & Home UI (`app/templates/base.html`, `index.html`, `search.html`, `api/[...path].js`)**
  - Clean 2-cards-per-row responsive grid across mobile, tablet, and desktop with `1080P`, `★` rating, title, `Year · MOVIE/SERIES`, and genre pills.
  - Automatically prioritizes newly added-groups and recently updated pages (`updated_at DESC, created_at DESC`).
  - Supports **Vercel** (`api/index.js`, `api/[...path].js`) and **Cloudflare Pages** (`functions/[[path]].js`) frontends sharing MongoDB.
- **Ultra-Fast `/batch` & Persistent Cache**
  - Caches scanned message IDs in MongoDB (`batch_msg_cache`), skips cached IDs on repeat runs, scans only missing IDs in parallel 200-message chunks, supports `-new` to force a fresh scan, and exports output >3 split messages as a `.txt` file.
- **Dedicated FileStore Bot (`/filestore`)**
  - Separate client with deduplication locks, single-pass parallel **FSub** checks + `Try Again` auto-delivery, `/genlink` (reply to any file, media, or message), `/stats`, `/fspanel`, `/help`, `/ping`, and configurable auto-delete.
- **Auto-Update Channel Broadcaster (`/autoubdate`)**
  - Add unlimited channels (with bot admin/post-message permission verification), enable/disable/replace/remove via inline buttons, and auto-post formatted release cards whenever pages or groups are published/updated.
- **Dynamic DNS & Automatic HTTPS (`deploy.vps`)**
  - Supports `export dns=dyn6` (**Dynv6** `/dyn6`), `export dns=duckdns` (**DuckDNS** `/duckdns`), `export dns=dynu` (**Dynu** `/dynu`), Cloudflare Quick Tunnel, and `/baseurl` admin override (with `/baseurl off` to reset).
  - Multi-deployment safe Caddy reverse proxy (`/etc/caddy/conf.d/hitler-yt.caddy`) with automatic port conflict resolution.

---

## Bot Admin Commands

| Command | Description |
| --- | --- |
| `/start` | Start bot or deliver `/start file_<code>` |
| `/batch <range> [filter] [-new]` | Ultra-fast cached batch link generator (`.txt` if >3 splits) |
| `/genlink` | Reply to any file, media, or text message to generate a deep link |
| `/addgroups <slug>` + `/done` | Collect and publish Watch/Download episode groups |
| `/autoubdate` | Manage auto-update channels (Add / Replace / Remove / Enable / Disable) |
| `/baseurl <url>` | Set admin Base URL everywhere (`/baseurl off` to reset) |
| `/filestore <token>` | Configure dedicated FileStore bot |
| `/fspanel` | Manage admins, FSub channels, and auto-delete timer |
| `/stats` | View main bot, FileStore user/delivery, and batch cache statistics |
| `/addbots` | Pool multiple helper bot tokens to boost streaming/download speed |
| `/dyn6` / `/duckdns` / `/dynu` | Configure DDNS and automatic HTTPS |

---

## Quick VPS Deployment

```bash
# Example with Dynv6 DDNS + Caddy HTTPS:
export dns=dyn6
export DYNV6_ZONE="yourname.dynv6.net"
export DYNV6_TOKEN="your-dynv6-token"
bash deploy.vps
```

Copy `.env.example` to `.env` and fill in `API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID`, `MONGODB_URI`, and `DATABASE_NAME=tg_media_bot` before running `bash deploy.vps`.
