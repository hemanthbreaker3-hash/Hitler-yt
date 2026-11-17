"""Site-wide shortener verification keys (valid for all /title/* pages until expiry)."""
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

    When shortener is enabled, a valid access key (cookie) unlocks ALL /title/*
    pages until the admin-configured expiry (e.g. 4 hours). Homepage, /search,
    /watch, media routes, APIs, and static assets are never gated.
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
        # Site-wide: any non-expired token unlocks every title page
        rec = await db.access_keys.find_one({"token": token, "expires_at": {"$gt": now}})
        if rec:
            return None
    return HTMLResponse(_page_key_html(target), status_code=200)

async def verify_key(request: Request):
    form = await request.form()
    key = str(form.get("key", "")).strip()
    target = str(form.get("target", "/"))
    if not is_protected_title_target(target, request.url.hostname):
        raise HTTPException(400, "Shortener verification is available only for title pages")
    now = int(time.time())
    # Site-wide: key is valid for all pages until expiry
    rec = await db.access_keys.find_one({"key": key, "expires_at": {"$gt": now}})
    if not rec:
        return HTMLResponse(_page_key_html(target, "Invalid or expired key. Generate a new one with Get Key."), status_code=403)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie("site_access", rec["token"], max_age=max(1, rec["expires_at"]-now), httponly=True, samesite="lax", secure=True)
    return response

_BLOCKED_HOSTS = {
    "www.w3.org", "w3.org", "schema.org", "example.com", "example.org",
    "localhost", "127.0.0.1",
}
_ASSET_SUFFIXES = (".svg", ".ico", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".css", ".js", ".map")


def _is_junk_short_url(candidate: str, destination: str, provider_host: str | None) -> bool:
    try:
        parsed = urllib.parse.urlsplit(candidate.strip())
    except Exception:
        return True
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return True
    host = parsed.netloc.lower().split(":")[0]
    path = (parsed.path or "").lower()
    href = candidate.lower()
    if host in _BLOCKED_HOSTS or host.endswith(".w3.org"):
        return True
    if "/2000/svg" in path or path.endswith(_ASSET_SUFFIXES) or "favicon" in path:
        return True
    if "xmlns" in href or "/svg" in path:
        return True
    if "/get-key" in path:
        return True
    try:
        dest = urllib.parse.urlsplit(str(destination))
        if dest.netloc and parsed.netloc == dest.netloc and parsed.path == dest.path:
            return True
    except Exception:
        pass
    return False


def _score_short_url(candidate: str, provider_host: str | None) -> int:
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except Exception:
        return -1
    host = (parsed.netloc or "").lower().split(":")[0]
    path = parsed.path or "/"
    score = 10
    if provider_host:
        ph = provider_host.lower()
        if host == ph or host.endswith("." + ph) or ph.endswith("." + host):
            score += 50
        if "shortlink" in ph and "short" in host:
            score += 20
    if 1 < len(path) <= 40 and "." not in path:
        score += 15
    if len([p for p in path.split("/") if p]) <= 2:
        score += 5
    if parsed.scheme == "https":
        score += 3
    return score


def _usable_short_url(value, destination: str = "", provider_host: str | None = None):
    if not isinstance(value, str):
        return None
    candidate = value.strip().strip('"').strip("'")
    if not candidate:
        return None
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        if _is_junk_short_url(candidate, destination, provider_host):
            return None
        return candidate
    return None


async def create_shortlink(base_url: str, api_key: str, destination: str):
    """Call shortlinkgenerator-compatible API: ?api=KEY&url=DEST&format=json and return only a real short URL."""
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

    parts = urlsplit(str(base_url).strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("Invalid shortener API endpoint")
    if not str(api_key).strip():
        raise ValueError("Shortener API key is empty")

    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    params.update({"api": str(api_key).strip(), "url": str(destination), "format": "json"})
    endpoint = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), ""))
    provider_host = parts.netloc.lower().split(":")[0]

    async with httpx.AsyncClient(
        timeout=25,
        follow_redirects=True,
        headers={"Accept": "application/json, text/plain, */*", "User-Agent": "HITLER-YT-Shortener/1.0"},
    ) as client:
        response = await client.get(endpoint)
        response.raise_for_status()
        try:
            data = response.json()
        except Exception:
            data = response.text

    priority_fields = [
        "shortenedurl", "shortenedUrl", "shorturl", "short_url", "shortened_url",
        "shortlink", "short_link", "shortenedlink", "shortened_link", "shortened",
        "short", "result", "link", "url",
    ]
    ranked: list[tuple[int, str]] = []

    def push(value, bonus=0):
        if value is None:
            return
        raw = str(value).strip().strip('"').strip("'")
        if not raw:
            return
        direct = _usable_short_url(raw, destination, provider_host)
        if direct:
            ranked.append((_score_short_url(direct, provider_host) + bonus, direct))
            return
        for m in re.findall(r"https?://[^\s\"'<>\]]+", raw):
            m = m.rstrip("),.;")
            ok = _usable_short_url(m, destination, provider_host)
            if ok:
                ranked.append((_score_short_url(ok, provider_host) + bonus, ok))

    if isinstance(data, dict):
        lower_map = {str(k).lower(): v for k, v in data.items()}
        for field in priority_fields:
            if field.lower() in lower_map:
                push(lower_map[field.lower()], 100)
        for nest_key in ("data", "result", "response", "shortener", "payload"):
            nest = data.get(nest_key)
            if isinstance(nest, dict):
                nest_lower = {str(k).lower(): v for k, v in nest.items()}
                for field in priority_fields:
                    if field.lower() in nest_lower:
                        push(nest_lower[field.lower()], 80)

    if isinstance(data, str):
        trimmed = data.strip()
        if trimmed.lower().startswith("http") and "<" not in trimmed and len(trimmed) < 500:
            push(trimmed, 90)
        else:
            push(trimmed, 0)

    if isinstance(data, dict):
        def walk(value, depth=0):
            if depth > 5 or value is None:
                return
            if isinstance(value, str):
                push(value, 0)
            elif isinstance(value, list):
                for item in value:
                    walk(item, depth + 1)
            elif isinstance(value, dict):
                for item in value.values():
                    walk(item, depth + 1)
        walk(data)

    if ranked:
        ranked.sort(key=lambda x: x[0], reverse=True)
        return ranked[0][1]

    message = ""
    if isinstance(data, dict):
        message = str(data.get("message") or data.get("error") or data.get("detail") or "")[:180]
    if not message:
        diagnostic = " ".join(str(data if isinstance(data, str) else getattr(response, "text", "")).split())[:240]
        message = f"Shortener API returned no valid short URL. Response: {diagnostic or '(empty)'}"
    raise ValueError(message)


async def get_key_page(request: Request, token: str):
    now = int(time.time())
    rec = await db.access_keys.find_one({"token": token, "expires_at": {"$gt": now}})
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
    remaining = max(0, int(rec["expires_at"]) - now)
    if remaining >= 3600:
        remain_label = f"{remaining // 3600}h {(remaining % 3600) // 60}m"
    elif remaining >= 60:
        remain_label = f"{remaining // 60} minutes"
    else:
        remain_label = f"{remaining} seconds"
    return HTMLResponse(f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0a0a0a"><title>Your Access Key · HITLER YT</title><style>{_KEY_PAGE_STYLE}
  #key{{text-align:center;font:700 16px "Space Grotesk",sans-serif;letter-spacing:.5px}}
  .hint{{color:#a3e635;font-size:13px;margin-top:10px;text-align:center}}
  </style></head><body><main><h1>✅ Your key is ready</h1><p class="lead">This key unlocks <b>all title pages</b> on the site for the remaining time. Copy it, then continue.</p><input id="key" readonly value="{key}"><button id="copy">📋 Copy Key &amp; Continue</button><a class="link-btn" href="{safe_target}" id="return-link">Open requested page</a><p class="hint">Valid for ~{remain_label} · works on every /title page</p><p class="msg" id="msg">Tap Copy Key to continue automatically.</p></main><script>document.getElementById('copy').addEventListener('click',async()=>{{const field=document.getElementById('key');try{{await navigator.clipboard.writeText(field.value);document.getElementById('msg').textContent='Copied! Redirecting…';location.href='/key-copied/{safe_token}';}}catch(e){{field.select();document.getElementById('msg').textContent='Clipboard blocked. Select and copy the key, then open the page.';}}}});</script></body></html>''')
