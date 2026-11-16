# Vercel Deployment & Cloudflare Try URL Integration

## How It Works

1. **Website on Vercel Only (Direct MongoDB):**
   - The Vercel serverless runtime (`api/index.js` and `api/[...path].js`) serves `/`, `/search`, `/title/<slug>`, `/panel`, `/api/catalog`, `/api/categories`, `/api/site-settings`, and `/api/admin/*` directly from MongoDB.
   - Because Vercel renders the website pages and catalog directly from MongoDB instead of proxying HTML requests through the VPS, VPS firewall blocking or tunnel restarts never cause `Catalog API returned HTTP 500` on the Vercel website.
2. **Download, Stream & Button URLs on Cloudflare (`trycloudflare.com`):**
   - All download links (`/dl/<code>`), stream/watch links (`/watch/<code>`, `/stream/<code>`, `/remux/<code>`), and button URLs inside Watch/Download groups on `/title/<slug>` automatically use your active `https://*.trycloudflare.com` URL stored in MongoDB (`settings`).
   - Updating the tunnel URL via `/baseurl https://name.trycloudflare.com`, `/backendurl https://name.trycloudflare.com`, `/panel`, or `cloudflare-quick-tunnel.sh` immediately rewrites all catalog button URLs to the new Cloudflare `trycloudflare.com` domain.

## Required Vercel Environment Variables

```env
MONGODB_URI=mongodb+srv://...
DATABASE_NAME=tg_media_bot
```

*(Note: If `DATABASE_NAME` is omitted, the Vercel gateway automatically detects the populated database between `tg_media_bot` and `file2link`.)*
