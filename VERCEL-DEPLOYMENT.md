# Vercel Deployment & ngrok Tunnel Integration

## How It Works

1. **Website on Vercel Only (Direct MongoDB):**
   - The Vercel serverless runtime (`api/index.js` and `api/[...path].js`) serves `/`, `/search`, `/title/<slug>`, `/panel`, `/api/catalog`, `/api/categories`, `/api/site-settings`, and `/api/admin/*` directly from MongoDB.
   - Because Vercel renders the website pages and catalog directly from MongoDB instead of proxying HTML requests through the VPS, VPS firewall blocking or tunnel restarts never cause `Catalog API returned HTTP 500` on the Vercel website.
2. **Download, Stream & Button URLs through ngrok (`ngrok-free.app`):**
   - All download links (`/dl/<code>`), stream/watch links (`/watch/<code>`, `/stream/<code>`, `/remux/<code>`), and button URLs inside Watch/Download groups on `/title/<slug>` automatically use your active `https://*.ngrok-free.app` URL stored in MongoDB (`settings`).
   - Updating the tunnel URL via `/baseurl https://name.ngrok-free.app`, `/backendurl https://name.ngrok-free.app`, `/panel`, or `ngrok tunnel manager` immediately rewrites all catalog button URLs to the new ngrok `ngrok-free.app` domain.

## Required Vercel Environment Variables

```env
MONGODB_URI=mongodb+srv://...
DATABASE_NAME=tg_media_bot
```

*(Note: If `DATABASE_NAME` is omitted, the Vercel gateway automatically detects the populated database between `tg_media_bot` and `animovieszoneflix`.)*

## Updated Vercel routing and streaming configuration

- `vercel.json` routes the homepage, API endpoints, and application pages through the shared Node.js API handler while leaving `/static/*` assets available as static files.
- The API handler disables automatic body parsing so request bodies and byte-range media requests are not consumed before proxying.
- The handler opts out of the response-size warning for streamed media responses and retains the configured 300-second function duration. Actual maximum duration is still subject to the Vercel plan and project limits.
- Static assets use short-lived browser caching with stale-while-revalidate. Basic security response headers are applied without a restrictive Content-Security-Policy that could break embedded players.

Set `MONGODB_URI` (or `DATABASE_URL` / `DATABASE_URI`) and, where needed, `DATABASE_NAME` in the Vercel project Environment Variables. Redeploy after changing environment variables. Verify `/api/health` after deployment.
