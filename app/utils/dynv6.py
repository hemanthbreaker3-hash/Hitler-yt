"""Dynv6 (dynv6.com) dynamic DNS auto-updater, startup auto-activator, and IP/reachability helpers."""
from __future__ import annotations

import asyncio
import datetime
import ipaddress
import logging
import os
import urllib.parse
import urllib.request

from app.database.connection import get_admin_base_url, settings, settings_col

logger = logging.getLogger(__name__)

_dynv6_task: asyncio.Task | None = None
_dynv6_stop = False

_IP_ENDPOINTS = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
    "https://checkip.amazonaws.com",
)


def normalize_dynv6_zone(raw: str | None) -> str:
    val = (raw or "").strip().lower()
    if val.startswith(("http://", "https://")):
        val = urllib.parse.urlparse(val).hostname or ""
    val = val.split("/", 1)[0].split(":", 1)[0].strip(".")
    return val


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
            headers={"User-Agent": "Dynv6-Probe/1.0"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            return 200 <= getattr(resp, "status", 200) < 400
    except Exception:
        return False


async def probe_dynv6_url(zone: str, port: int | None = None) -> tuple[str, bool]:
    """Return (best_url, is_externally_reachable) for <zone>."""
    dom = normalize_dynv6_zone(zone)
    p = int(port or os.getenv("PORT") or 8000)
    candidates = [
        f"https://{dom}",
        f"http://{dom}",
    ]
    for cand_port in (p, 8001, 8000):
        u = f"http://{dom}" if cand_port == 80 else f"http://{dom}:{cand_port}"
        if u not in candidates:
            candidates.append(u)
    for cand in candidates:
        if await asyncio.to_thread(_sync_probe_health, cand):
            return cand, True
    fallback = f"http://{dom}" if p == 80 else f"http://{dom}:{p}"
    return fallback, False


async def get_dynv6_config() -> tuple[str, str]:
    env_zone = normalize_dynv6_zone(os.getenv("DYNV6_ZONE") or os.getenv("DYNV6_HOSTNAME") or "")
    env_tok = os.getenv("DYNV6_TOKEN", "").strip()
    if env_zone and env_tok:
        return env_zone, env_tok
    try:
        doc_z = await settings_col.find_one({"key": "dynv6_zone"})
        doc_t = await settings_col.find_one({"key": "dynv6_token"})
        zone = normalize_dynv6_zone((doc_z or {}).get("value") or env_zone)
        tok = str((doc_t or {}).get("value") or env_tok).strip()
        return zone, tok
    except Exception:
        return env_zone, env_tok


def _sync_dynv6_update(zone: str, token: str, ip: str = "") -> tuple[bool, str]:
    params = {"zone": zone, "token": token, "ipv4": ip or "auto"}
    qs = urllib.parse.urlencode(params)
    url = f"https://ipv4.dynv6.com/api/update?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "Dynv6-Updater/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        status = getattr(resp, "status", 200)
        body = resp.read().decode("utf-8", errors="replace").strip()
    low = body.lower()
    ok = (200 <= status < 300) and (
        "updated" in low or "unchanged" in low or low == "ok" or not low
    )
    return ok, (body or f"HTTP {status}")


async def update_dynv6_once(zone: str, token: str, ip: str | None = None) -> tuple[bool, str, str | None]:
    dom = normalize_dynv6_zone(zone)
    tok = (token or "").strip()
    if not dom or not tok:
        return False, "Missing Dynv6 zone/hostname or token", None
    resolved_ip = (ip or "").strip() or (await get_public_ip()) or "auto"
    try:
        ok, body = await asyncio.to_thread(_sync_dynv6_update, dom, tok, resolved_ip)
        return ok, body, (None if resolved_ip == "auto" else resolved_ip)
    except Exception as exc:
        return False, str(exc), (None if resolved_ip == "auto" else resolved_ip)


async def sync_and_activate_dynv6(updated_by: int | None = None) -> tuple[bool, str, str | None, str | None, bool]:
    """Sync VPS IP to Dynv6 and automatically activate backend/website URLs if reachable."""
    zone, tok = await get_dynv6_config()
    if not zone or not tok:
        return False, "Not configured", None, None, False
    ok, res, ip = await update_dynv6_once(zone, tok)
    if not ok:
        return False, res, ip, None, False
    port = int(str(os.getenv("PORT") or getattr(settings, "PORT", None) or "8000").strip())
    best_url, reachable = await probe_dynv6_url(zone, port)
    now = datetime.datetime.utcnow()
    meta = {"value": best_url, "updated_at": now}
    if updated_by is not None:
        meta["updated_by"] = updated_by
    await settings_col.update_one({"key": "dynv6_url"}, {"$set": meta}, upsert=True)
    await settings_col.update_one(
        {"key": "dynv6_last_status"},
        {"$set": {"value": f"{res} ({best_url})", "updated_at": now}},
        upsert=True,
    )
    if reachable:
        admin_override = await get_admin_base_url()
        if not admin_override:
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
                    and "dynv6.net" not in cur_base
                    and "dns.army" not in cur_base
                    and "dns.navy" not in cur_base
                )
            )
            if not is_explicit_pages_or_custom:
                settings.BASE_URL = best_url
                os.environ["BASE_URL"] = best_url
                await settings_col.update_one({"key": "base_url"}, {"$set": meta}, upsert=True)
                await settings_col.update_one({"key": "vercel_url"}, {"$set": meta}, upsert=True)
    return True, res, ip, best_url, reachable


def is_dynv6_enabled_for_deployment() -> bool:
    raw = os.getenv("DNS_PROVIDER") or os.getenv("dns") or os.getenv("DNS") or getattr(settings, "DNS_PROVIDER", "auto")
    val = str(raw or "").strip().lower()
    if val in ("duck", "duckdns", "duckdns.org", "dynu", "dyno", "dynu.com", "dynu.net", "none", "off", "disabled", "false", "0"):
        return False
    return True


async def _dynv6_loop() -> None:
    while not _dynv6_stop:
        reachable = True
        try:
            if is_dynv6_enabled_for_deployment():
                zone, tok = await get_dynv6_config()
                if zone and tok:
                    ok, res, ip, best_url, reachable = await sync_and_activate_dynv6()
                    if ok:
                        logger.info(
                            "Dynv6 synced: %s (%s) -> %s | url=%s (reachable=%s)",
                            zone, ip or "auto", res, best_url, reachable,
                        )
                    else:
                        logger.warning("Dynv6 update failed for %s: %s", zone, res)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Dynv6 loop error: %s", exc)
        sleep_secs = 300 if reachable else 60
        for _ in range(sleep_secs):
            if _dynv6_stop:
                break
            await asyncio.sleep(1)


def start_dynv6_updater() -> None:
    global _dynv6_task, _dynv6_stop
    _dynv6_stop = False
    if _dynv6_task is None or _dynv6_task.done():
        _dynv6_task = asyncio.create_task(_dynv6_loop())


async def stop_dynv6_updater() -> None:
    global _dynv6_task, _dynv6_stop
    _dynv6_stop = True
    if _dynv6_task and not _dynv6_task.done():
        _dynv6_task.cancel()
        try:
            await _dynv6_task
        except asyncio.CancelledError:
            pass
    _dynv6_task = None
