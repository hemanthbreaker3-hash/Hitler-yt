# Cloudflare Pages Deployment (MongoDB + TMDB Direct Mode)

This project runs **directly on Cloudflare Pages** using the exact same MongoDB-backed credentials & settings as Vercel — you only need to set **3 environment variables**:

1. Push this project to GitHub/GitLab and connect it in **Cloudflare Dashboard → Workers & Pages → Create → Pages → Connect to Git**.
2. Build settings:
   - **Framework preset**: `None`
   - **Build command**: *(leave empty)*
   - **Build output directory**: `public`
   - **Compatibility flags** (Settings → Functions → Compatibility flags): add `nodejs_compat` (also set in `wrangler.toml`).
3. Add **only these 3 Environment Variables** in **Settings → Environment variables**:
   - `MONGODB_URI` (or `MONGO_URL`)
   - `MONGODB_DB_NAME` (or `DATABASE_NAME`)
   - `TMDB_API_KEY`
4. Deploy! Cloudflare Pages fetches all remaining credentials and settings (`cloudflare_url` / tunnel URL, `filestore_bot_username`, `downloads_enabled`, `admin_panel_token`, etc.) automatically from MongoDB.
