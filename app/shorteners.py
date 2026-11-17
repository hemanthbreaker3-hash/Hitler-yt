"""Page-scoped shortener verification keys."""
import secrets, time, urllib.parse, html, re, os
import httpx
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from app.database.connection import db

async def config():
    return await db.site_settings.find_one({"_id":"shortener_config"}) or {"enabled":False,"shorteners":[],"expiry_seconds":300}

_KEY_PAGE_STYLE = '''
  @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700;800&family=Space+Grotesk:wght@600;700&display=swap');
  *{box-sizing:border-box;margin:0;padding:0}
  body{min-height:100vh;display:grid;place-items:center;background:radial-gradient(ellipse at 12% -10%,rgba(250,204,21,.07),transparent 34%),#0a0a0a;color:#f5f5f5;font:500 15px/1.55 "DM Sans",system-ui,sans-serif;padding:24px}
  main{width:min(420px,92vw);padding:30px 26px;border:1px solid #2a2a2a;border-radius:16px;background:linear-gradient(155deg,rgba(22,22,22,.98),rgba(13,13,13,.98))}
  h1{font:800 20px/1.3 "Space Grotesk",sans-serif;margin-bottom:6px;display:flex;align-items:center;gap:8px}
  p.lead{color:#9ca3af;font-size:13.5px;margin-bottom:20px}
  input{width:100%;padding:13px 14px;border-radius:10px;border:1px solid #2a2a2a;background:#141414;color:#fff;font:inherit;outline:none;transition:border-color .15s}
  input:focus{border-color:#facc15}
  button{width:100%;padding:13px 14px;margin-top:12px;border-radius:10px;border:none;cursor:pointer;background:#facc15;color:#111;font:700 14.5px "DM Sans",sans-serif;transition:background .15s,transform .15s}
  button:hover{background:#eab308;transform:translateY(-1px)}
  a.link-btn{display:block;text-align:center;margin-top:12px;padding:13px 14px;border-radius:10px;border:1px solid #2a2a2a;color:#f5f5f5;font-weight:700;transition:border-color .15s,color .15s}
  a.link-btn:hover{border-color:#facc15;color:#facc15}
  .err{color:#f87171;font-size:13px;margin-top:14px;text-align:center}
  .msg{color:#9ca3af;font-size:13px;margin-top:16px;text-align:center}
'''

PROTECTED_TITLE_PREFIX = "/title/"
PROTECTED_TITLE_HOST = "hitler-yt.vercel.app"

def _is_vercel_title_host(hostname: str | None) -> bool:
    """Match the production Vercel host and configured Vercel aliases."""
    if hostname is None:
        return True
    host = str(hostname).strip().lower().split(",", 1)[0].split(":", 1)[0].rstrip(".")
    configured = (os.getenv("VERCEL_PROJECT_PRODUCTION_URL") or os.getenv("VERCEL_URL") or "").lower()
    configured = configured.removeprefix("https://").removeprefix("http://").split("/", 1)[0].split(":", 1)[0]
    allowed = {PROTECTED_TITLE_HOST, f"www.{PROTECTED_TITLE_HOST}"}
    if configured:
        allowed.update({configured, f"www.{configured}"})
    return host in allowed or host.endswith(".vercel.app")


def is_protected_title_target(target: str, hostname: str | None = None) -> bool:
    """Only /title/* pages on the public Vercel website can use shortener keys."""
    if not isinstance(target, str) or not target.startswith("/") or target.startswith("//"):
        return False
    parsed = urllib.parse.urlsplit(target)
    return (
        _is_vercel_title_host(hostname)
        and not parsed.scheme
        and not parsed.netloc
        and parsed.path.startswith(PROTECTED_TITLE_PREFIX)
        and len(parsed.path) > len(PROTECTED_TITLE_PREFIX)
    )

def _page_key_html(path, error=""):
    # Keep the exact path + query as the scope; never accept a caller-supplied external URL.
    safe_path = html.escape(path, quote=True)
    safe_error = html.escape(str(error), quote=True)
    get_key_target = urllib.parse.quote(path, safe="")
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0a0a0a"><title>Key Verification · HITLER YT</title><style>{_KEY_PAGE_STYLE}</style></head><body><main><h1>🔐 Key Verification</h1><p class="lead">This page is protected. Enter a valid access key to continue, or get one below.</p><form method="post" action="/verify-key"><input type="hidden" name="target" value="{safe_path}"/><input name="key" placeholder="Paste your key here" required autocomplete="off"/><button type="submit">Open Link</button></form><a class="link-btn" href="/get-key-start?target={get_key_target}">Get Key</a>{f'<p class="err">{safe_error}</p>' if safe_error else ''}</main></body></html>'''

async def gate_request(request: Request):
    """Require a shortener key for every title page.

    The shortener is applied to every public /title/<slug> page on the Vercel
    website only when the admin toggle is enabled. When disabled, all title pages
    load normally. The homepage, /search, /watch, media routes, APIs, and static
    assets are never gated.
    """
    path = request.url.path
    if not is_protected_title_target(path, request.url.hostname):
        return None
    if request.method not in ("GET", "HEAD"):
        return None
    cfg = await config()
    if not cfg.get("enabled"):
        return None
    target = path + (f"?{request.url.query}" if request.url.query else "")
    token = request.cookies.get("site_access", "")
    if token:
        now = int(time.time())
        rec = await db.access_keys.find_one({"token":token,"target":target,"expires_at":{"$gt":now}})
        if rec:
            return None
        # Same-page assets/API/media requests inherit the authorization of their same-origin
        # referring page. This keeps a protected watch page playable without granting access
        # to a different page or a different media short code.
        referer = request.headers.get("referer", "")
        if referer:
            parsed = urllib.parse.urlsplit(referer)
            if parsed.scheme in ("http", "https") and parsed.netloc == request.url.netloc:
                parent_target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
                parent_rec = await db.access_keys.find_one({"token":token,"target":parent_target,"expires_at":{"$gt":now}})
                if parent_rec:
                    return None
    return HTMLResponse(_page_key_html(target), status_code=200)

async def verify_key(request: Request):
    form = await request.form()
    key = str(form.get("key", "")).strip()
    target = str(form.get("target", "/"))
    if not is_protected_title_target(target, request.url.hostname):
        raise HTTPException(400, "Shortener verification is available only for title pages")
    now = int(time.time())
    rec = await db.access_keys.find_one({"key":key,"target":target,"expires_at":{"$gt":now}})
    if not rec:
        return HTMLResponse(_page_key_html(target, "Invalid, expired, or page-specific key."), status_code=403)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie("site_access", rec["token"], max_age=max(1, rec["expires_at"]-now), httponly=True, samesite="lax", secure=True)
    return response

def _usable_short_url(value):
    if not isinstance(value, str):
        return None
    candidate = value.strip().strip('"')
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return candidate
    return None


async def create_shortlink(base_url: str, api_key: str, destination: str):
    """Call the shortlinkgenerator-compatible API and extract its URL robustly."""
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

    parts = urlsplit(str(base_url).strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("Invalid shortener API endpoint")
    # Replace existing values rather than appending duplicate api/url/format parameters.
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    params.update({"api": str(api_key).strip(), "url": str(destination), "format": "json"})
    endpoint = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), ""))
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers={"Accept": "application/json, text/plain, */*"}) as client:
        response = await client.get(endpoint)
        response.raise_for_status()
        try:
            data = response.json()
        except Exception:
            data = response.text

    url_fields = {
        "shortenedurl", "shorturl", "short_url", "shortened_url", "shortlink",
        "short_link", "shortenedlink", "shortened_link", "link", "url", "result",
    }
    candidates = []
    def collect(value, depth=0):
        if depth > 6 or value is None:
            return
        if isinstance(value, str):
            candidates.append(value)
            # Also accept a URL embedded in text, e.g. "Short URL: https://...".
            candidates.extend(re.findall(r"https?://[^\s\"'<>\]]+", value))
            if value.lstrip().startswith(("{", "[")):
                try:
                    import json
                    collect(json.loads(value), depth + 1)
                except Exception:
                    pass
        elif isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in url_fields:
                    collect(item, depth + 1)
            for key, item in value.items():
                if isinstance(item, (dict, list)) and str(key).lower() not in url_fields:
                    collect(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                collect(item, depth + 1)

    collect(data)
    for candidate in candidates:
        result = _usable_short_url(candidate)
        if result:
            return result
    message = ""
    if isinstance(data, dict):
        message = str(data.get("message") or data.get("error") or data.get("detail") or "")[:180]
    if not message:
        diagnostic = " ".join(str(data if isinstance(data, str) else response.text).split())[:240]
        message = f"Shortener API returned no valid URL. Provider response: {diagnostic or '(empty response)'}"
    raise ValueError(message)


async def get_key_page(request: Request, token: str):
    rec = await db.access_keys.find_one({"token":token,"expires_at":{"$gt":int(time.time())}})
    if not rec:
        return HTMLResponse(
            f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Expired · HITLER YT</title><style>{_KEY_PAGE_STYLE}</style></head><body><main>'
            f'<h1>⌛ Key request expired</h1><p class="lead">Return to the page and request a new key.</p></main></body></html>',
            status_code=410,
        )
    key = html.escape(str(rec["key"]), quote=True)
    safe_token = html.escape(token, quote=True)
    safe_target = html.escape(str(rec["target"]), quote=True)
    return HTMLResponse(f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0a0a0a"><title>Your Access Key · HITLER YT</title><style>{_KEY_PAGE_STYLE}
  #key{{text-align:center;font:700 16px "Space Grotesk",sans-serif;letter-spacing:.5px}}
  </style></head><body><main><h1>✅ Your key is ready</h1><p class="lead">Copy the key below, then continue to your requested page.</p><input id="key" readonly value="{key}"><button id="copy">📋 Copy Key</button><a class="link-btn" href="{safe_target}" id="return-link">Return to requested page</a><p class="msg" id="msg">Tap Copy Key to continue automatically.</p></main><script>document.getElementById('copy').addEventListener('click',async()=>{{const field=document.getElementById('key');try{{await navigator.clipboard.writeText(field.value);document.getElementById('msg').textContent='Copied! Redirecting…';location.href='/key-copied/{safe_token}';}}catch(e){{field.select();document.getElementById('msg').textContent='Clipboard access was blocked. Select and copy the key, then use Return to requested page.';}}}});</script></body></html>''')
