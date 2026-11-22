# HITLER YT — Cloudflare Pages 100% Deployment Guide

This project runs **natively on Cloudflare Pages** and fetches all website credentials and settings (`cloudflare_url`, `filestore_bot_username`, `downloads_enabled`, `admin_panel_token`, etc.) directly from MongoDB.

## Step 1: Push Code to GitHub / GitLab
Extract this ZIP and push all files to your GitHub or GitLab repository.

## Step 2: Create Cloudflare Pages Project
1. Open **Cloudflare Dashboard → Workers & Pages → Create → Pages → Connect to Git**.
2. Select your repository and configure **Build settings**:
   - **Framework preset**: `None`
   - **Build command**: *(leave completely empty)*
   - **Build output directory**: `public`
   - **Root directory**: `/` *(default)*

## Step 3: Add the 3 Required Environment Variables
Go to **Pages Project → Settings → Environment variables** and add these for **both Production and Preview**:
- `MONGODB_URI` = your MongoDB connection string (`mongodb+srv://...` or `mongodb://...`)
- `MONGODB_DB_NAME` = your MongoDB database name (for example `tg_media_bot`)
- `TMDB_API_KEY` = your TMDB v3 API key

> You do **not** need to add `BACKEND_URL`, bot tokens, or tunnel URLs in Cloudflare Pages — they are automatically fetched from MongoDB.

## Step 4: Verify Compatibility Flag
`wrangler.toml` already sets:
- `compatibility_date = "2025-10-01"`
- `compatibility_flags = ["nodejs_compat"]`

In **Settings → Functions → Compatibility flags**, ensure `nodejs_compat` is enabled for Production & Preview, then click **Retry deployment** (or push a commit).

## Step 5: Verify Live Endpoints
After deployment finishes, test:
- `https://your-project.pages.dev/health`
- `https://your-project.pages.dev/`
- `https://your-project.pages.dev/panel`
- `https://your-project.pages.dev/watch/<short_code>`

## Optional: Deploy via CLI (Wrangler)
```bash
npm install
npx wrangler pages deploy public --project-name=hitler-yt-pages
```
