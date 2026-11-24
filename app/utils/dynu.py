"""Dynu.com dynamic DNS auto-updater, startup auto-activator, and IP/reachability helpers."""
from __future__ import annotations

import asyncio
import datetime
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


async def get_dynu_config() -> tuple[str, str, str]:
    """Return (hostname, password_or_token, username)."""
    env_dom = normalize_dynu_domain(os.getenv("DYNU_DOMAIN") or os.getenv("DYNU_HOST") or "")
    env_pw = (os.getenv("DYNU_PASSWORD") or os.getenv("DYNU_TOKEN") or "").strip()
    env_user = (os.getenv("DYNU_USERNAME") or "").strip()
    if env_dom and env_pw:
        return env_dom, env_pw, env_user
    try:
        rows = await settings_col.find(
            {"key": {"$in": ["dynu_domain", "dynu_password", "dynu_username"]}},
            {"key": 1, "value": 1},
        ).to_list(10)
        stored = {r["key"]: r.get("value") for r in rows if isinstance(r, dict)}
        dom = normalize_dynu_domain(stored.get("dynu_domain") or env_dom)
        pw = str(stored.get("dynu_password") or env_pw).strip()
        user = str(stored.get("dynu_username") or env_user).strip()
        return dom, pw, user
    except Exception:
        return env_dom, env_pw, env_user


def _sync_dynu_update(hostname: str, password: str, username: str = "", ip: str = "") -> tuple[bool, str]:
    import base64
    params = {"hostname": hostname}
    if ip:
        params["myip"] = ip
    headers = {"User-Agent": "Dynu-Updater/1.0"}
    if username:
        token_b64 = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token_b64}"
    else:
        params["password"] = password
    qs = urllib.parse.urlencode(params)
    url = f"https://api.dynu.com/nic/update?{qs}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8", errors="replace").strip()
    low = body.lower()
    ok = low.startswith("good") or low.startswith("nochg")
    return ok, body


async def update_dynu_once(
    hostname: str, password: str, username: str = "", ip: str | None = None
) -> tuple[bool, str, str | None]:
    dom = normalize_dynu_domain(hostname)
    pw = (password or "").strip()
    user = (username or "").strip()
    if not dom or not pw:
        return False, "Missing Dynu hostname or password/token", None
    resolved_ip = (ip or "").strip() or (await get_public_ip()) or ""
    try:
        ok, body = await asyncio.to_thread(_sync_dynu_update, dom, pw, user, resolved_ip)
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
    dom, pw, user = await get_dynu_config()
    if not dom or not pw:
        return False, "Not configured", None, None, False
    ok, res, ip = await update_dynu_once(dom, pw, user)
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
    # Auto: prefer Dynu if configured, else DuckDNS
    dom, pw, _ = await get_dynu_config()
    if dom and pw:
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
                dom, pw, _ = await get_dynu_config()
                if dom and pw and (provider == "dynu" or not os.getenv("DUCKDNS_DOMAIN")):
                    ok, res, ip, best_url, reachable = await sync_and_activate_dynu()
                    if ok:
                        logger.info("Dynu synced: %s (%s) -> %s | url=%s (reachable=%s)", dom, ip or "auto", res, best_url, reachable)
                    else:
                        logger.warning("Dynu update failed for %s: %s", dom, res)
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
