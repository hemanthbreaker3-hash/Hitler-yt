"""DuckDNS dynamic DNS auto-updater for VPS deployments."""
from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse
import urllib.request

from app.database.connection import settings_col

logger = logging.getLogger(__name__)

_duckdns_task: asyncio.Task | None = None
_duckdns_stop = False


def normalize_duckdns_domain(raw: str | None) -> str:
    val = (raw or "").strip().lower()
    if val.startswith(("http://", "https://")):
        val = urllib.parse.urlparse(val).hostname or ""
    val = val.strip(".")
    if val.endswith(".duckdns.org"):
        val = val[: -len(".duckdns.org")]
    return val.strip(".")


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


def _sync_duckdns_update(domain: str, token: str) -> tuple[bool, str]:
    qs = urllib.parse.urlencode({"domains": domain, "token": token, "ip": ""})
    url = f"https://www.duckdns.org/update?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "DuckDNS-Updater/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8", errors="replace").strip()
    return body.upper().startswith("OK"), body


async def update_duckdns_once(domain: str, token: str) -> tuple[bool, str]:
    dom = normalize_duckdns_domain(domain)
    tok = (token or "").strip()
    if not dom or not tok:
        return False, "Missing DuckDNS domain or token"
    try:
        return await asyncio.to_thread(_sync_duckdns_update, dom, tok)
    except Exception as exc:
        return False, str(exc)


async def _duckdns_loop() -> None:
    while not _duckdns_stop:
        try:
            dom, tok = await get_duckdns_config()
            if dom and tok:
                ok, res = await update_duckdns_once(dom, tok)
                if ok:
                    logger.info("DuckDNS updated: %s.duckdns.org -> %s", dom, res)
                else:
                    logger.warning("DuckDNS update failed for %s.duckdns.org: %s", dom, res)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("DuckDNS loop error: %s", exc)
        for _ in range(300):
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
