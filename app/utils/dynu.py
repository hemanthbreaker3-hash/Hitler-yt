"""Dynu.com dynamic DNS auto-updater (API Key, OAuth2, and IP Update Password)."""
from __future__ import annotations

import asyncio
import base64
import datetime
import json
import logging
import os
import urllib.parse
import urllib.request

from app.database.connection import settings, settings_col
from app.utils.duckdns import get_public_ip, _sync_probe_health

logger = logging.getLogger(__name__)

_dynu_task: asyncio.Task | None = None
_dynu_stop = False


def normalize_dynu_domain(raw: str | None) -> str:
    val = (raw or "").strip().lower()
    if not val:
        return ""
    if val.startswith(("http://", "https://")):
        val = urllib.parse.urlparse(val).hostname or ""
    val = val.strip(".")
    if val and "." not in val:
        val = f"{val}.dynu.net"
    return val


async def get_dns_provider() -> str:
    raw = (os.getenv("DNS_PROVIDER") or os.getenv("dns") or os.getenv("DNS") or "").strip().lower()
    if raw in ("duck", "duckdns"):
        return "duck"
    if raw in ("dynu", "dynu.com", "dynu.net"):
        return "dynu"
    try:
        doc = await settings_col.find_one({"key": "dns_provider"})
        val = str((doc or {}).get("value") or "").strip().lower()
        if val in ("duck", "duckdns"):
            return "duck"
        if val in ("dynu", "dynu.com", "dynu.net"):
            return "dynu"
    except Exception:
        pass
    return "auto"


async def get_dynu_config() -> dict[str, str]:
    """Return dict with domain, password, api_key, client_id, client_secret, username."""
    env_cfg = {
        "domain": normalize_dynu_domain(os.getenv("DYNU_DOMAIN") or os.getenv("DYNU_HOST") or ""),
        "password": (os.getenv("DYNU_PASSWORD") or os.getenv("DYNU_TOKEN") or "").strip(),
        "api_key": (os.getenv("DYNU_API_KEY") or "").strip(),
        "client_id": (os.getenv("DYNU_CLIENT_ID") or "").strip(),
        "client_secret": (os.getenv("DYNU_CLIENT_SECRET") or "").strip(),
        "username": (os.getenv("DYNU_USERNAME") or "").strip(),
    }
    if env_cfg["domain"] and (env_cfg["api_key"] or env_cfg["password"] or (env_cfg["client_id"] and env_cfg["client_secret"])):
        return env_cfg
    try:
        rows = await settings_col.find(
            {"key": {"$in": ["dynu_domain", "dynu_password", "dynu_api_key", "dynu_client_id", "dynu_client_secret", "dynu_username"]}},
            {"key": 1, "value": 1},
        ).to_list(15)
        stored = {r["key"]: str(r.get("value") or "").strip() for r in rows if isinstance(r, dict)}
        return {
            "domain": normalize_dynu_domain(stored.get("dynu_domain") or env_cfg["domain"]),
            "password": stored.get("dynu_password") or env_cfg["password"],
            "api_key": stored.get("dynu_api_key") or env_cfg["api_key"],
            "client_id": stored.get("dynu_client_id") or env_cfg["client_id"],
            "client_secret": stored.get("dynu_client_secret") or env_cfg["client_secret"],
            "username": stored.get("dynu_username") or env_cfg["username"],
        }
    except Exception:
        return env_cfg


def _dynu_v2_request(path: str, auth_headers: dict[str, str], payload: dict | None = None) -> dict:
    url = f"https://api.dynu.com/v2{path}"
    headers = {"Accept": "application/json", "User-Agent": "Dynu-Updater/2.0", **auth_headers}
    data = None
    method = "GET"
    if payload is not None:
        method = "POST"
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace") or "{}")


def _dynu_get_oauth_token(client_id: str, client_secret: str) -> str:
    b64 = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        "https://api.dynu.com/v2/oauth2/token",
        headers={"Accept": "application/json", "Authorization": f"Basic {b64}", "User-Agent": "Dynu-Updater/2.0"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
    tok = str(data.get("access_token") or "").strip()
    if not tok:
        raise RuntimeError("Dynu OAuth2 did not return an access_token")
    return tok


def _dynu_v2_update_ip(hostname: str, ip: str, auth_headers: dict[str, str]) -> tuple[bool, str]:
    domains_res = _dynu_v2_request("/dns", auth_headers)
    dom_list = domains_res.get("domains") or []
    target = None
    for d in dom_list:
        if str(d.get("name") or "").strip().lower() == hostname:
            target = d
            break
    if not target:
        for d in dom_list:
            name = str(d.get("name") or "").strip().lower()
            if hostname == name or hostname.endswith("." + name):
                target = d
                break
    if not target or not target.get("id"):
        return False, f"Hostname {hostname} not found in Dynu account"
    dom_id = target["id"]
    cur_ip = str(target.get("ipv4Address") or "").strip()
    if cur_ip == ip:
        return True, f"nochg {ip}"
    body = {
        "name": target.get("name") or hostname,
        "group": target.get("group") or "",
        "ipv4Address": ip,
        "ipv6Address": target.get("ipv6Address"),
        "ttl": target.get("ttl") or 90,
        "ipv4": True,
        "ipv6": bool(target.get("ipv6", False)),
        "ipv4WildcardAlias": bool(target.get("ipv4WildcardAlias", True)),
        "ipv6WildcardAlias": bool(target.get("ipv6WildcardAlias", False)),
        "allowZoneTransfer": bool(target.get("allowZoneTransfer", False)),
        "dnssec": bool(target.get("dnssec", False)),
    }
    _dynu_v2_request(f"/dns/{dom_id}", auth_headers, payload=body)
    return True, f"good {ip}"


def _sync_dynu_update(
    hostname: str,
    password: str = "",
    username: str = "",
    ip: str = "",
    api_key: str = "",
    client_id: str = "",
    client_secret: str = "",
) -> tuple[bool, str]:
    # 1. OAuth2 Client ID + Secret
    if client_id and client_secret:
        tok = _dynu_get_oauth_token(client_id, client_secret)
        return _dynu_v2_update_ip(hostname, ip, {"Authorization": f"Bearer {tok}"})

    # 2. Explicit Dynu API Key
    if api_key:
        return _dynu_v2_update_ip(hostname, ip, {"API-Key": api_key})

    # 3. Classic DDNS /nic/update (with automatic fallback to v2 API-Key if password is an API Key)
    params = {"hostname": hostname}
    if ip:
        params["myip"] = ip
    headers = {"User-Agent": "Dynu-Updater/2.0"}
    if username:
        b64 = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {b64}"
    else:
        params["password"] = password
    qs = urllib.parse.urlencode(params)
    url = f"https://api.dynu.com/nic/update?{qs}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8", errors="replace").strip()
    low = body.lower()
    if low.startswith("good") or low.startswith("nochg"):
        return True, body
    if password and not username and len(password) >= 20:
        try:
            return _dynu_v2_update_ip(hostname, ip, {"API-Key": password})
        except Exception:
            pass
    return False, body


async def update_dynu_once(
    hostname: str,
    password: str = "",
    username: str = "",
    ip: str | None = None,
    api_key: str = "",
    client_id: str = "",
    client_secret: str = "",
) -> tuple[bool, str, str | None]:
    dom = normalize_dynu_domain(hostname)
    if not dom or not (api_key or password or (client_id and client_secret)):
        return False, "Missing Dynu hostname or credentials", None
    resolved_ip = (ip or "").strip() or (await get_public_ip()) or ""
    try:
        ok, body = await asyncio.to_thread(
            _sync_dynu_update,
            dom,
            (password or "").strip(),
            (username or "").strip(),
            resolved_ip,
            (api_key or "").strip(),
            (client_id or "").strip(),
            (client_secret or "").strip(),
        )
        return ok, body, (resolved_ip or None)
    except Exception as exc:
        return False, str(exc), (resolved_ip or None)


async def probe_dynu_url(hostname: str, port: int | None = None) -> tuple[str, bool]:
    dom = normalize_dynu_domain(hostname)
    p = int(port or os.getenv("PORT") or 8000)
    candidates = [f"https://{dom}", f"http://{dom}"]
    for cand_port in (p, 8001, 8000):
        u = f"http://{dom}" if cand_port == 80 else f"http://{dom}:{cand_port}"
        if u not in candidates:
            candidates.append(u)
    for cand in candidates:
        if await asyncio.to_thread(_sync_probe_health, cand):
            return cand, True
    fallback = f"http://{dom}" if p == 80 else f"http://{dom}:{p}"
    return fallback, False


async def sync_and_activate_dynu(updated_by: int | None = None) -> tuple[bool, str, str | None, str | None, bool]:
    cfg = await get_dynu_config()
    dom = cfg["domain"]
    if not dom or not (cfg["api_key"] or cfg["password"] or (cfg["client_id"] and cfg["client_secret"])):
        return False, "Not configured", None, None, False
    ok, res, ip = await update_dynu_once(
        dom,
        password=cfg["password"],
        username=cfg["username"],
        api_key=cfg["api_key"],
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
    )
    if not ok:
        return False, res, ip, None, False
    port = int(str(os.getenv("PORT") or getattr(settings, "PORT", None) or "8000").strip())
    best_url, reachable = await probe_dynu_url(dom, port)
    now = datetime.datetime.utcnow()
    meta = {"value": best_url, "updated_at": now}
    if updated_by is not None:
        meta["updated_by"] = updated_by
    await settings_col.update_one({"key": "dynu_url"}, {"$set": meta}, upsert=True)
    if reachable:
        settings.BACKEND_URL = best_url
        os.environ["BACKEND_URL"] = best_url
        await settings_col.update_one({"key": "backend_url"}, {"$set": meta}, upsert=True)
        await settings_col.update_one({"key": "cloudflare_url"}, {"$set": meta}, upsert=True)
        base_doc = await settings_col.find_one({"key": "base_url"}, {"value": 1}) or {}
        cur_base = str(base_doc.get("value") or "").strip().lower()
        is_explicit_pages_or_custom = (
            ".pages.dev" in cur_base
            or (
                cur_base.startswith(("http://", "https://"))
                and "vercel.app" not in cur_base
                and "trycloudflare.com" not in cur_base
                and "duckdns.org" not in cur_base
                and "dynu." not in cur_base
            )
        )
        if not is_explicit_pages_or_custom:
            settings.BASE_URL = best_url
            os.environ["BASE_URL"] = best_url
            await settings_col.update_one({"key": "base_url"}, {"$set": meta}, upsert=True)
            await settings_col.update_one({"key": "vercel_url"}, {"$set": meta}, upsert=True)
    return True, res, ip, best_url, reachable


async def sync_and_activate_active_ddns() -> None:
    """Activate whichever DDNS provider is selected via DNS_PROVIDER (duck | dynu | auto)."""
    provider = await get_dns_provider()
    if provider == "dynu":
        await sync_and_activate_dynu()
        return
    if provider == "duck":
        from app.utils.duckdns import sync_and_activate_duckdns
        await sync_and_activate_duckdns()
        return
    cfg = await get_dynu_config()
    if cfg["domain"] and (cfg["api_key"] or cfg["password"] or (cfg["client_id"] and cfg["client_secret"])):
        await sync_and_activate_dynu()
        return
    from app.utils.duckdns import sync_and_activate_duckdns
    await sync_and_activate_duckdns()


async def _dynu_loop() -> None:
    while not _dynu_stop:
        reachable = True
        try:
            provider = await get_dns_provider()
            if provider in ("dynu", "auto"):
                cfg = await get_dynu_config()
                has_creds = bool(cfg["domain"] and (cfg["api_key"] or cfg["password"] or (cfg["client_id"] and cfg["client_secret"])))
                if has_creds and (provider == "dynu" or not os.getenv("DUCKDNS_DOMAIN")):
                    ok, res, ip, best_url, reachable = await sync_and_activate_dynu()
                    if ok:
                        logger.info("Dynu synced: %s (%s) -> %s | url=%s (reachable=%s)", cfg["domain"], ip or "auto", res, best_url, reachable)
                    else:
                        logger.warning("Dynu update failed for %s: %s", cfg["domain"], res)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Dynu loop error: %s", exc)
        sleep_secs = 300 if reachable else 60
        for _ in range(sleep_secs):
            if _dynu_stop:
                break
            await asyncio.sleep(1)


def start_dynu_updater() -> None:
    global _dynu_task, _dynu_stop
    _dynu_stop = False
    if _dynu_task is None or _dynu_task.done():
        _dynu_task = asyncio.create_task(_dynu_loop())


async def stop_dynu_updater() -> None:
    global _dynu_task, _dynu_stop
    _dynu_stop = True
    if _dynu_task and not _dynu_task.done():
        _dynu_task.cancel()
        try:
            await _dynu_task
        except asyncio.CancelledError:
            pass
    _dynu_task = None
