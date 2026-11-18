import ipaddress
import random
import re
import string
from urllib.parse import parse_qs, urlparse, urlunparse

TELEGRAM_HOSTS = {"t.me", "www.t.me", "telegram.me", "www.telegram.me", "telegram.dog", "www.telegram.dog"}


def generate_short_code(length: int = 8):
    characters = string.ascii_letters + string.digits
    return ''.join(random.choices(characters, k=length))


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
    """Validate and normalize a public URL (including *.trycloudflare.com Quick Tunnels) for Telegram buttons and links."""
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
        return host == "trycloudflare.com" or host.endswith(".trycloudflare.com")
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
        return host == "vercel.app" or host.endswith(".vercel.app")
    except Exception:
        return False


def is_telegram_url(value: str | None) -> bool:
    if not value or not isinstance(value, str):
        return False
    try:
        raw = value.strip()
        if raw.startswith("tg://"):
            return True
        if not raw.startswith(("http://", "https://")):
            return False
        host = (urlparse(raw).hostname or "").lower().rstrip(".")
        return host in TELEGRAM_HOSTS
    except Exception:
        return False


def extract_short_code_from_url(target_url: str | None) -> tuple[str | None, str | None]:
    """
    Extract (kind, short_code) from a media URL or Telegram FileStore deep link.
    kind is one of: 'dl', 'watch', 'stream', 'remux', 'tg_filestore', or None.
    """
    raw = str(target_url or "").strip()
    if not raw:
        return None, None
    try:
        parsed = urlparse(raw if "://" in raw or raw.startswith("/") else f"https://placeholder.local/{raw.lstrip('/')}")
        host = (parsed.hostname or "").lower().rstrip(".")
        if host in TELEGRAM_HOSTS:
            qs = parse_qs(parsed.query or "")
            start_vals = qs.get("start") or []
            if start_vals:
                payload = start_vals[0].strip()
                if payload.lower().startswith("file_"):
                    code = payload[5:].strip()
                elif payload.lower().startswith("dl_"):
                    code = payload[3:].strip()
                else:
                    code = payload
                if code and re.fullmatch(r"[A-Za-z0-9_-]{4,64}", code):
                    return "tg_filestore", code
            return None, None

        path = parsed.path or ""
        m = re.match(r"^/(dl|watch|stream|remux)/([A-Za-z0-9_-]{4,64})(?:/.*)?$", path)
        if m:
            return m.group(1).lower(), m.group(2)

        if path.rstrip("/") == "/watch":
            qs = parse_qs(parsed.query or "")
            for key in ("path", "code", "id", "v"):
                vals = qs.get(key) or []
                if vals and re.fullmatch(r"[A-Za-z0-9_-]{4,64}", vals[0].strip()):
                    return "watch", vals[0].strip()
    except Exception:
        pass
    return None, None


def build_filestore_deep_link(filestore_bot_username: str | None, short_code: str) -> str | None:
    uname = str(filestore_bot_username or "").strip().lstrip("@")
    code = str(short_code or "").strip()
    if not uname or not code:
        return None
    return f"https://t.me/{uname}?start=file_{code}"


def resolve_download_url(
    short_code: str,
    base_url: str | None,
    downloads_enabled: bool = True,
    filestore_bot_username: str | None = None,
) -> str | None:
    """
    Return the active download link for a file:
    - When downloads_enabled is True: returns `{base_url}/dl/{short_code}`
    - When downloads_enabled is False and filestore_bot_username is set:
      returns `https://t.me/{filestore_bot_username}?start=file_{short_code}`
    """
    code = str(short_code or "").strip()
    if not code:
        return None
    if downloads_enabled:
        clean_base = str(base_url or "").strip().rstrip("/")
        return f"{clean_base}/dl/{code}" if clean_base else f"/dl/{code}"
    return build_filestore_deep_link(filestore_bot_username, code)


def rewrite_media_or_button_url(
    target_url: str,
    cloudflare_base: str | None,
    force_replace_domain: bool = True,
    box: str | None = None,
    downloads_enabled: bool = True,
    filestore_bot_username: str | None = None,
) -> str:
    """
    Replace the domain of a button URL, download link (/dl/), stream link (/stream/),
    watch link (/watch/), or remux link (/remux/) with the active Cloudflare trycloudflare.com URL.
    When downloads_enabled is False and filestore_bot_username is configured, any /dl/ link or
    download-box button with a short_code is automatically converted into a FileStore deep link.
    """
    target = str(target_url or "").strip()
    if not target:
        return target

    kind, short_code = extract_short_code_from_url(target)
    fs_user = str(filestore_bot_username or "").strip().lstrip("@")

    # If downloads are disabled and a FileStore bot is configured, convert download links to deep links
    if not downloads_enabled and fs_user and short_code:
        if kind in ("dl", "tg_filestore") or (box == "download" and kind in ("watch", "stream")):
            return f"https://t.me/{fs_user}?start=file_{short_code}"

    # If downloads are enabled and a download button was previously a FileStore deep link, restore /dl/ if cf_base known
    if downloads_enabled and kind == "tg_filestore" and short_code and cloudflare_base:
        try:
            cf_base = normalize_public_base_url(cloudflare_base)
            return f"{cf_base}/dl/{short_code}"
        except ValueError:
            pass

    # Never rewrite Telegram deep links (t.me / telegram.me) to trycloudflare.com
    if is_telegram_url(target):
        if kind == "tg_filestore" and fs_user and short_code:
            return f"https://t.me/{fs_user}?start=file_{short_code}"
        return target

    if not cloudflare_base:
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
    if host in TELEGRAM_HOSTS:
        return target

    should_replace_domain = (
        force_replace_domain
        or parsed.path.startswith(media_prefixes)
        or parsed.path.rstrip("/") == "/watch"
        or host.endswith(".trycloudflare.com")
        or host.endswith(".vercel.app")
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


def rewrite_page_groups_to_cloudflare(
    groups: list | None,
    cloudflare_base: str | None,
    downloads_enabled: bool = True,
    filestore_bot_username: str | None = None,
) -> list:
    """Return a copy of page groups with button URLs rewritten to active Cloudflare tunnel or FileStore deep link."""
    if not isinstance(groups, list):
        return []
    updated_groups = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        g_copy = dict(group)
        box = "download" if str(g_copy.get("box") or "watch").strip().lower() == "download" else "watch"
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
                        box=box,
                        downloads_enabled=downloads_enabled,
                        filestore_bot_username=filestore_bot_username,
                    )
                new_buttons.append(b_copy)
            g_copy["buttons"] = new_buttons
        updated_groups.append(g_copy)
    return updated_groups
