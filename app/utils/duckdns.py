"""DuckDNS dynamic DNS auto-updater, startup auto-activator, and IP/reachability helpers."""
from __future__ import annotations

import asyncio
import datetime
import ipaddress
import logging
import os
import urllib.parse
import urllib.request

from app.database.connection import get_active_vercel_website_url, settings, settings_col

logger = logging.getLogger(__name__)

_duckdns_task: asyncio.Task | None = None
_duckdns_stop = False

_IP_ENDPOINTS = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
    "https://checkip.amazonaws.com",
)


def normalize_duckdns_domain(raw: str | None) -> str:
    val = (raw or "").strip().lower()
    if val.startswith(("http://", "https://")):
        val = urllib.parse.urlparse(val).hostname or ""
    val = val.strip(".")
    if val.endswith(".duckdns.org"):
        val = val[: -len(".duckdns.org")]
    return val.strip(".")


def _sync_get_public_ip() -> str | None:
    for url in _IP_ENDPOINTS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                candidate = resp.read().decode("utf-8", errors="replace").strip()
            ipaddress.ip_address(candidate)
            return candidate
        except Exception:
            continue
    return None


async def get_public_ip() -> str | None:
    return await asyncio.to_thread(_sync_get_public_ip)


def _sync_probe_health(base_url: str) -> bool:
    try:
        req = urllib.request.Request(
            base_url.rstrip("/") + "/health",
            headers={"User-Agent": "DuckDNS-Probe/1.0"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            return 200 <= getattr(resp, "status", 200) < 400
    except Exception:
        return False


async def probe_duckdns_url(domain: str, port: int | None = None) -> tuple[str, bool]:
    """Return (best_url, is_externally_reachable) for <domain>.duckdns.org."""
    dom = normalize_duckdns_domain(domain)
    p = int(port or os.getenv("PORT") or 8000)
    candidates = [
        f"https://{dom}.duckdns.org",
        f"http://{dom}.duckdns.org",
    ]
    for cand_port in (p, 8001, 8000):
        u = f"http://{dom}.duckdns.org" if cand_port == 80 else f"http://{dom}.duckdns.org:{cand_port}"
        if u not in candidates:
            candidates.append(u)
    for cand in candidates:
        if await asyncio.to_thread(_sync_probe_health, cand):
            return cand, True
    fallback = f"http://{dom}.duckdns.org" if p == 80 else f"http://{dom}.duckdns.org:{p}"
    return fallback, False


async def get_duckdns_config() -> tuple[str, str]:
    env_dom = normalize_duckdns_domain(os.getenv("DUCKDNS_DOMAIN", ""))
    env_tok = os.getenv("DUCKDNS_TOKEN", "").strip()
    if env_dom and env_tok:
        return env_dom, env_tok
    try:
        doc_d = await settings_col.find_one({"key": "duckdns_domain"})
        doc_t = await settings_col.find_one({"key": "duckdns_token"})
        dom = normalize_duckdns_domain((doc_d or {}).get("value") or env_dom)
        tok = str((doc_t or {}).get("value") or env_tok).strip()
        return dom, tok
    except Exception:
        return env_dom, env_tok


def _sync_duckdns_update(domain: str, token: str, ip: str = "") -> tuple[bool, str]:
    qs = urllib.parse.urlencode({"domains": domain, "token": token, "ip": ip})
    url = f"https://www.duckdns.org/update?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "DuckDNS-Updater/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8", errors="replace").strip()
    return body.upper().startswith("OK"), body


async def update_duckdns_once(domain: str, token: str, ip: str | None = None) -> tuple[bool, str, str | None]:
    dom = normalize_duckdns_domain(domain)
    tok = (token or "").strip()
    if not dom or not tok:
        return False, "Missing DuckDNS domain or token", None
    resolved_ip = (ip or "").strip() or (await get_public_ip()) or ""
    try:
        ok, body = await asyncio.to_thread(_sync_duckdns_update, dom, tok, resolved_ip)
        return ok, body, (resolved_ip or None)
    except Exception as exc:
        return False, str(exc), (resolved_ip or None)


async def sync_and_activate_duckdns(updated_by: int | None = None) -> tuple[bool, str, str | None, str | None, bool]:
    """Sync VPS IP to DuckDNS and automatically activate backend/website URLs if reachable."""
    dom, tok = await get_duckdns_config()
    if not dom or not tok:
        return False, "Not configured", None, None, False
    ok, res, ip = await update_duckdns_once(dom, tok)
    if not ok:
        return False, res, ip, None, False
    port = int(str(os.getenv("PORT") or getattr(settings, "PORT", None) or "8000").strip())
    best_url, reachable = await probe_duckdns_url(dom, port)
    now = datetime.datetime.utcnow()
    meta = {"value": best_url, "updated_at": now}
    if updated_by is not None:
        meta["updated_by"] = updated_by
    await settings_col.update_one({"key": "duckdns_url"}, {"$set": meta}, upsert=True)
    if reachable:
        settings.BACKEND_URL = best_url
        os.environ["BACKEND_URL"] = best_url
        await settings_col.update_one({"key": "backend_url"}, {"$set": meta}, upsert=True)
        await settings_col.update_one({"key": "cloudflare_url"}, {"$set": meta}, upsert=True)
        # Use DuckDNS as the active website URL too unless an explicit .pages.dev / custom non-preview domain is set in base_url
        base_doc = await settings_col.find_one({"key": "base_url"}, {"value": 1}) or {}
        cur_base = str(base_doc.get("value") or "").strip().lower()
        is_explicit_pages_or_custom = (
            ".pages.dev" in cur_base
            or (cur_base.startswith(("http://", "https://")) and "vercel.app" not in cur_base and "trycloudflare.com" not in cur_base and "duckdns.org" not in cur_base)
        )
        if not is_explicit_pages_or_custom:
            settings.BASE_URL = best_url
            os.environ["BASE_URL"] = best_url
            await settings_col.update_one({"key": "base_url"}, {"$set": meta}, upsert=True)
            await settings_col.update_one({"key": "vercel_url"}, {"$set": meta}, upsert=True)
    return True, res, ip, best_url, reachable


async def _duckdns_loop() -> None:
    while not _duckdns_stop:
        try:
            from app.utils.dynu import get_dns_provider
            if await get_dns_provider() == "dynu":
                await asyncio.sleep(60)
                continue
            dom, tok = await get_duckdns_config()
            if dom and tok:
                ok, res, ip, best_url, reachable = await sync_and_activate_duckdns()
                if ok:
                    logger.info(
                        "DuckDNS synced: %s.duckdns.org (%s) -> %s | url=%s (reachable=%s)",
                        dom, ip or "auto", res, best_url, reachable,
                    )
                else:
                    logger.warning("DuckDNS update failed for %s.duckdns.org: %s", dom, res)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("DuckDNS loop error: %s", exc)
        sleep_secs = 300 if locals().get('reachable', True) else 60
        for _ in range(sleep_secs):
            if _duckdns_stop:
                break
            await asyncio.sleep(1)


def start_duckdns_updater() -> None:
    global _duckdns_task, _duckdns_stop
    _duckdns_stop = False
    if _duckdns_task is None or _duckdns_task.done():
        _duckdns_task = asyncio.create_task(_duckdns_loop())


async def stop_duckdns_updater() -> None:
    global _duckdns_task, _duckdns_stop
    _duckdns_stop = True
    if _duckdns_task and not _duckdns_task.done():
        _duckdns_task.cancel()
        try:
            await _duckdns_task
        except asyncio.CancelledError:
            pass
    _duckdns_task = None
