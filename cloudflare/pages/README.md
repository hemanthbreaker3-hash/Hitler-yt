# Cloudflare Pages Deployment (Full Website + Edge Caching)

This project supports **Cloudflare Pages** directly from the repository root (recommended) or from `cloudflare/pages`.

## Option 1: Deploy from Repository Root (Recommended)
1. Push this project to GitHub/GitLab and connect it in **Cloudflare Dashboard → Workers & Pages → Create → Pages → Connect to Git**.
2. Configure build settings:
   - **Framework preset**: `None`
   - **Build command**: *(leave empty)*
   - **Build output directory**: `public`
   - **Root directory**: `/` *(default)*
3. Add Environment Variable in **Settings → Environment variables** (Production & Preview):
   - `BACKEND_URL` = your active HTTPS backend/tunnel URL (e.g. `https://your-tunnel.trycloudflare.com` or your VPS/Vercel origin). Do **not** point it at the `*.pages.dev` domain itself.
4. Deploy! Cloudflare Pages automatically serves `/static/*` from its global CDN and routes `/`, `/title/*`, `/watch/*`, `/panel`, `/stream/*`, `/dl/*`, `/remux/*`, and `/api/*` through `functions/[[path]].js` with Edge caching.

## Option 2: CLI Deploy with Wrangler
```bash
npx wrangler pages deploy public --project-name=hitler-yt-pages
```
