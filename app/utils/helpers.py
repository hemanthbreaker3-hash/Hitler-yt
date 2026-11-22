import ipaddress
import random
import re
import string
from urllib.parse import urlparse, urlunparse


def generate_short_code(length: int = 8):
    characters = string.ascii_letters + string.digits
    return ''.join(random.choices(characters, k=length))


# Match generated watch/dl/stream/remux URLs from this service (short codes 3–64 chars)
_WATCH_URL_CODE_RE = re.compile(
    r"(?:^|/)(?:watch|dl|stream|remux)/([A-Za-z0-9_-]{3,64})(?:/|$|\?|#)",
    re.I,
)


def extract_short_code_from_media_url(url: str | None) -> str | None:
    """Return short_code if URL is a generated watch/dl/stream/remux link from this bot; else None."""
    if not url or not isinstance(url, str):
        return None
    raw = url.strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw if "://" in raw or raw.startswith("/") else f"https://x{raw}")
        path = parsed.path or ""
    except Exception:
        path = raw
    if not path and raw:
        path = raw
    m = _WATCH_URL_CODE_RE.search(path)
    if m:
        return m.group(1)
    # Full URL without relying on path parse (e.g. unusual hosts)
    m2 = _WATCH_URL_CODE_RE.search(raw)
    if m2:
        return m2.group(1)
    # Relative path
    m3 = re.search(r"^/(?:watch|dl|stream|remux)/([A-Za-z0-9_-]{3,64})(?:/|$|\?|#)", path or raw, re.I)
    return m3.group(1) if m3 else None


def filestore_deep_link(username: str, short_code: str) -> str:
    uname = (username or "").strip().lstrip("@")
    code = (short_code or "").strip()
    return f"https://t.me/{uname}?start=file_{code}"


def extract_watch_short_code(url: str | None) -> str | None:
    """Return short_code only for /watch/<code> URLs (not dl/stream/remux)."""
    if not url or not isinstance(url, str):
        return None
    raw = url.strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw if "://" in raw or raw.startswith("/") else f"https://x{raw}")
        path = parsed.path or ""
    except Exception:
        path = raw
    if not path and raw:
        path = raw
    m = re.search(r"(?:^|/)watch/([A-Za-z0-9_-]{3,64})(?:/|$|\?|#)", path, re.I)
    if m:
        return m.group(1)
    m2 = re.search(r"(?:^|/)watch/([A-Za-z0-9_-]{3,64})(?:/|$|\?|#)", raw, re.I)
    return m2.group(1) if m2 else None




def format_bytes(size: int):
    if not size or size <= 0:
        return "0 B"
    power = 1024
    n = 0
    power_labels = {0: '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size >= power and n < 4:
        size /= power
        n += 1
    if n == 0:
        return f"{int(size)} B"
    return f"{size:.2f} {power_labels[n]}B"


def normalize_public_base_url(value: str) -> str:
    """Validate and normalize a public URL (including ngrok public tunnel domains) for Telegram buttons and links."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("BASE_URL is empty. Set it to a public HTTPS URL using /baseurl.")

    value = value.strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("BASE_URL must start with http:// or https:// and include a hostname.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("BASE_URL must not contain credentials, query parameters, or fragments.")

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("BASE_URL must be publicly reachable; localhost/private URLs cannot be Telegram buttons.")

    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("BASE_URL contains an invalid port.") from exc

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise ValueError("BASE_URL must use a public hostname or public IP address.")
    else:
        if not address.is_global:
            raise ValueError("BASE_URL must use a public IP address, not a private or reserved IP.")

    return value


def is_trycloudflare_url(value: str | None) -> bool:
    if not value or not isinstance(value, str):
        return False
    try:
        raw = value.strip()
        if not raw.startswith(("http://", "https://")):
            raw = "https://" + raw
        host = (urlparse(raw).hostname or "").lower().rstrip(".")
        return (
            host == "ngrok-free.app" or host.endswith(".ngrok-free.app")
            or host == "ngrok-free.dev" or host.endswith(".ngrok-free.dev")
            or host == "ngrok.io" or host.endswith(".ngrok.io")
            or host == "trycloudflare.com" or host.endswith((".trycloudflare.com", ".ngrok-free.app", ".ngrok-free.dev", ".ngrok.io"))
        )
    except Exception:
        return False


def is_vercel_url(value: str | None) -> bool:
    if not value or not isinstance(value, str):
        return False
    try:
        raw = value.strip()
        if not raw.startswith(("http://", "https://")):
            raw = "https://" + raw
        host = (urlparse(raw).hostname or "").lower().rstrip(".")
        return host in ("vercel.app", "pages.dev") or host.endswith((".vercel.app", ".pages.dev"))
    except Exception:
        return False


def rewrite_media_or_button_url(target_url: str, cloudflare_base: str | None, force_replace_domain: bool = True) -> str:
    """
    Replace the domain of a button URL, download link (/dl/), stream link (/stream/),
    watch link (/watch/), or remux link (/remux/) with the active public ngrok tunnel URL.
    """
    target = str(target_url or "").strip()
    if not target or not cloudflare_base:
        return target
    try:
        cf_base = normalize_public_base_url(cloudflare_base)
    except ValueError:
        return target

    cf_parsed = urlparse(cf_base)
    media_prefixes = ("/dl/", "/stream/", "/watch/", "/remux/")

    if target.startswith("/"):
        return cf_base + target

    parsed = urlparse(target)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return target

    host = (parsed.hostname or "").lower().rstrip(".")
    should_replace_domain = (
        force_replace_domain
        or parsed.path.startswith(media_prefixes)
        or host.endswith((".trycloudflare.com", ".ngrok-free.app", ".ngrok-free.dev", ".ngrok.io"))
        or host.endswith((".vercel.app", ".pages.dev"))
    )
    if should_replace_domain:
        return urlunparse((
            cf_parsed.scheme,
            cf_parsed.netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        ))
    return target


def rewrite_page_groups_to_cloudflare(groups: list | None, cloudflare_base: str | None) -> list:
    """Return a copy of page groups with ALL button URL domains replaced by the active Cloudflare tunnel domain."""
    if not isinstance(groups, list):
        return []
    updated_groups = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        g_copy = dict(group)
        buttons = g_copy.get("buttons")
        if isinstance(buttons, list):
            new_buttons = []
            for btn in buttons:
                if not isinstance(btn, dict):
                    continue
                b_copy = dict(btn)
                if b_copy.get("url"):
                    b_copy["url"] = rewrite_media_or_button_url(
                        str(b_copy["url"]),
                        cloudflare_base,
                        force_replace_domain=True,
                    )
                new_buttons.append(b_copy)
            g_copy["buttons"] = new_buttons
        updated_groups.append(g_copy)
    return updated_groups
