"""Page-scoped shortener verification keys."""
import secrets, time, urllib.parse
import httpx
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from app.database.connection import db

async def config():
    return await db.site_settings.find_one({"_id":"shortener_config"}) or {"enabled":False,"shorteners":[],"expiry_seconds":300}

def _page_key_html(path, error=""):
    return f'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Key Verification</title><style>body{{margin:0;background:#080d1b;color:#fff;font:16px Arial;display:grid;place-items:center;min-height:100vh}}main{{width:min(420px,88vw);padding:28px;border:1px solid #244b83;border-radius:18px;background:#101b32;box-shadow:0 0 35px #087bff33}}input,button{{width:100%;padding:14px;margin-top:12px;border-radius:10px;border:1px solid #31527c;background:#091426;color:white;box-sizing:border-box}}button{{background:#087bff;border:0;font-weight:bold}}a{{display:block;text-align:center;color:#8bc5ff;margin-top:15px}}.err{{color:#ff8585}}</style></head><body><main><h2>🔐 Key Verification</h2><p>Enter a valid key to open this page.</p><form method="post" action="/verify-key"><input type="hidden" name="target" value="{urllib.parse.quote(path, safe='/')}"/><input name="key" placeholder="Paste your key here" required autocomplete="off"/><button type="submit">Open Link</button></form><a href="/get-key-start?target={urllib.parse.quote(path, safe='')}" >Get Key</a><p class="err">{error}</p></main></body></html>'''

async def gate_request(request: Request):
    cfg = await config()
    if not cfg.get("enabled") or request.method not in ("GET", "HEAD"):
        return None
    path = request.url.path
    if path.startswith(("/static/", "/api/health", "/health", "/key/", "/get-key", "/verify-key", "/key-copied/", "/favicon")):
        return None
    token = request.cookies.get("site_access", "")
    if token:
        rec = await db.access_keys.find_one({"token":token,"target":path,"expires_at":{"$gt":int(time.time())}})
        if rec:
            return None
    return HTMLResponse(_page_key_html(path), status_code=200)

async def verify_key(request: Request):
    form = await request.form()
    key = str(form.get("key", "")).strip()
    target = str(form.get("target", "/"))
    if not target.startswith("/") or target.startswith("//"):
        raise HTTPException(400, "Invalid target")
    now = int(time.time())
    rec = await db.access_keys.find_one({"key":key,"target":target,"expires_at":{"$gt":now}})
    if not rec:
        return HTMLResponse(_page_key_html(target, "Invalid, expired, or page-specific key."), status_code=403)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie("site_access", rec["token"], max_age=max(1, rec["expires_at"]-now), httponly=True, samesite="lax", secure=True)
    return response

async def create_shortlink(base_url: str, api_key: str, destination: str):
    # Shortlino-compatible API convention; accept either JSON or plain-text URL responses.
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        r = await client.get(base_url, params={"api":api_key,"url":destination,"format":"json"})
        r.raise_for_status()
        try:
            data = r.json()
            if isinstance(data, dict):
                for k in ("shortenedUrl","shortURL","short_url","shortlink","shortened_url","shorturl","url"):
                    if data.get(k): return str(data[k])
                for value in data.values():
                    if isinstance(value, dict):
                        for k in ("shortenedUrl","short_url","shortlink","url"):
                            if value.get(k): return str(value[k])
        except Exception:
            pass
        text = r.text.strip().strip('"')
        if text.startswith(("https://","http://")): return text
    raise ValueError("Shortener API did not return a short URL")

async def get_key_page(request: Request, token: str):
    rec = await db.access_keys.find_one({"token":token,"expires_at":{"$gt":int(time.time())}})
    if not rec: return HTMLResponse("Key request expired. Return to the page and request a new key.", status_code=410)
    key = rec["key"]
    return HTMLResponse(f'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Your Access Key</title></head><body style="background:#08101f;color:white;font:16px Arial;display:grid;place-items:center;min-height:100vh"><main style="background:#14223b;padding:28px;border-radius:16px;width:min(420px,85vw)"><h2>✅ Your key is ready</h2><input id="key" readonly value="{key}" style="width:100%;padding:14px;box-sizing:border-box"><button onclick="navigator.clipboard.writeText(document.getElementById('key').value).then(()=>{{document.getElementById('msg').textContent='Copied!';location.href='/key-copied/{token}'}})" style="width:100%;padding:14px;margin-top:12px">Copy Key</button><p id="msg">Copy the key, then return to the page you requested.</p><a href="{rec['target']}" style="color:#8bc5ff">Return to requested page</a></main></body></html>''')
