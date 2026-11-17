from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import shutil

from app.database.connection import db, settings, settings_col
from app.utils.helpers import (
    is_trycloudflare_url,
    normalize_public_base_url,
    rewrite_page_groups_to_cloudflare,
)

logger = logging.getLogger(__name__)

_TUNNEL_REGEX = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
_monitor_task: asyncio.Task | None = None
_cloudflared_proc: asyncio.subprocess.Process | None = None
_last_synced_url: str | None = None


def _update_env_file_urls(tunnel_url: str) -> None:
    """Persist the auto-detected Cloudflare try URL to .env if writable."""
    env_path = Path(".env")
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text().splitlines()
        new_lines = []
        found_backend = False
        found_cf = False
        for line in lines:
            if re.match(r"^\s*BACKEND_URL\s*=", line):
                if not found_backend:
                    new_lines.append("BACKEND_URL=" + json.dumps(tunnel_url))
                    found_backend = True
            elif re.match(r"^\s*CLOUDFLARE_URL\s*=", line):
                if not found_cf:
                    new_lines.append("CLOUDFLARE_URL=" + json.dumps(tunnel_url))
                    found_cf = True
            elif re.match(r"^\s*BASE_URL\s*=", line):
                # Update BASE_URL if it was localhost or a previous trycloudflare URL
                raw_val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if raw_val.startswith(("http://localhost", "http://127.0.0.1")) or is_trycloudflare_url(raw_val):
                    new_lines.append("BASE_URL=" + json.dumps(tunnel_url))
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)
        if not found_backend:
            new_lines.append("BACKEND_URL=" + json.dumps(tunnel_url))
        if not found_cf:
            new_lines.append("CLOUDFLARE_URL=" + json.dumps(tunnel_url))
        env_path.write_text("\n".join(new_lines) + "\n")
    except Exception as exc:
        logger.debug("Could not update .env with tunnel URL: %s", exc)


async def apply_auto_cloudflare_url(raw_url: str, source: str = "auto-cloudflare") -> str | None:
    """
    Automatically save a detected Cloudflare trycloudflare.com URL into runtime memory and MongoDB,
    and replace all button URL domains across catalog_pages in MongoDB.
    """
    global _last_synced_url
    try:
        tunnel_url = normalize_public_base_url(raw_url)
    except ValueError:
        return None

    if not is_trycloudflare_url(tunnel_url):
        return None

    settings.BACKEND_URL = tunnel_url
    os.environ["BACKEND_URL"] = tunnel_url
    os.environ["CLOUDFLARE_URL"] = tunnel_url
    if (
        not settings.BASE_URL
        or settings.BASE_URL.startswith(("http://localhost", "http://127.0.0.1"))
        or is_trycloudflare_url(settings.BASE_URL)
    ):
        settings.BASE_URL = tunnel_url
        os.environ["BASE_URL"] = tunnel_url

    if _last_synced_url == tunnel_url:
        return tunnel_url

    now = datetime.now(timezone.utc)
    try:
        for key in ("cloudflare_url", "backend_url", "base_url"):
            await settings_col.update_one(
                {"key": key},
                {"$set": {"value": tunnel_url, "updated_by": source, "updated_at": now}},
                upsert=True,
            )

        # Keep website_urls exclusively for Vercel / non-tunnel website domains
        web_doc = await settings_col.find_one({"key": "website_urls"}, {"value": 1})
        if web_doc and isinstance(web_doc.get("value"), list):
            cleaned_web = [u for u in web_doc["value"] if not is_trycloudflare_url(str(u))]
            if cleaned_web != web_doc["value"]:
                await settings_col.update_one(
                    {"key": "website_urls"},
                    {"$set": {"value": cleaned_web, "updated_at": now}},
                )

        # Automatically replace all button URL domains in catalog_pages with the new Cloudflare try URL
        rewritten_pages = 0
        cursor = db.catalog_pages.find({"groups.0": {"$exists": True}}, {"_id": 1, "groups": 1})
        async for doc in cursor:
            old_groups = doc.get("groups") or []
            new_groups = rewrite_page_groups_to_cloudflare(old_groups, tunnel_url)
            if new_groups != old_groups:
                await db.catalog_pages.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"groups": new_groups, "updated_at": now}},
                )
                rewritten_pages += 1

        _update_env_file_urls(tunnel_url)
        _last_synced_url = tunnel_url
        logger.info(
            "Auto-set Cloudflare URL (%s): %s (rewrote button domains on %d catalog pages)",
            source,
            tunnel_url,
            rewritten_pages,
        )
    except Exception as exc:
        logger.warning("Failed to persist auto Cloudflare URL to MongoDB: %s", exc)

    return tunnel_url


def _extract_tunnel_url_from_logs() -> str | None:
    """Check known cloudflared log locations for the most recent trycloudflare.com URL."""
    candidates = [
        Path("cloudflared-quick-tunnel.log"),
        Path("/app/cloudflared-quick-tunnel.log"),
        Path("/tmp/cloudflared-quick-tunnel.log"),
    ]
    for path in candidates:
        try:
            if path.exists():
                content = path.read_text(errors="ignore")
                matches = _TUNNEL_REGEX.findall(content)
                if matches:
                    return matches[-1]
        except Exception:
            continue
    return None


async def _spawn_internal_cloudflared(port: int) -> None:
    """Spawn cloudflared inside the container/process if no external host tunnel log is present."""
    global _cloudflared_proc
    cf_bin = shutil.which("cloudflared") or (
        "/usr/local/bin/cloudflared" if os.path.exists("/usr/local/bin/cloudflared") else None
    )
    if not cf_bin:
        return

    # Origin host for --url. Prefer TRYURL/tryurl (e.g. export tryurl=138.68.175.206)
    # so the Quick Tunnel targets the public VPS IP instead of loopback.
    origin_host = (
        os.environ.get("TRYURL")
        or os.environ.get("tryurl")
        or "127.0.0.1"
    ).strip()
    origin_host = origin_host.removeprefix("http://").removeprefix("https://")
    origin_host = origin_host.split("/")[0].split(":")[0] or "127.0.0.1"
    origin_url = f"http://{origin_host}:{port}"

    log_path = Path("/tmp/cloudflared-quick-tunnel.log")
    try:
        log_file = log_path.open("w", encoding="utf-8")
        _cloudflared_proc = await asyncio.create_subprocess_exec(
            cf_bin,
            "tunnel",
            "--no-autoupdate",
            "--loglevel",
            "info",
            "--url",
            origin_url,
            stdout=log_file,
            stderr=asyncio.subprocess.STDOUT,
        )
        logger.info(
            "Started internal cloudflared Quick Tunnel -> %s (pid=%s)",
            origin_url,
            _cloudflared_proc.pid,
        )
    except Exception as exc:
        logger.warning("Could not start internal cloudflared process: %s", exc)


async def _cloudflare_monitor_loop() -> None:
    """Background loop that auto-detects, spawns if needed, and syncs the trycloudflare.com URL to MongoDB."""
    port = int(os.environ.get("PORT", settings.PORT or 8000))
    await asyncio.sleep(2)

    # First check if a trycloudflare URL is already in environment or host log
    for env_candidate in (os.environ.get("CLOUDFLARE_URL"), settings.BACKEND_URL, settings.BASE_URL):
        if env_candidate and is_trycloudflare_url(env_candidate):
            await apply_auto_cloudflare_url(env_candidate, source="startup-env")
            break

    log_url = _extract_tunnel_url_from_logs()
    if log_url:
        await apply_auto_cloudflare_url(log_url, source="host-log")
    elif os.environ.get("DISABLE_AUTO_CLOUDFLARED", "").lower() not in ("1", "true", "yes"):
        # Wait briefly in case host systemd service is writing cloudflared-quick-tunnel.log
        await asyncio.sleep(3)
        log_url = _extract_tunnel_url_from_logs()
        if log_url:
            await apply_auto_cloudflare_url(log_url, source="host-log")
        elif not _last_synced_url:
            await _spawn_internal_cloudflared(port)

    while True:
        try:
            url = _extract_tunnel_url_from_logs()
            if url:
                await apply_auto_cloudflare_url(url, source="log-watcher")
            elif _cloudflared_proc and _cloudflared_proc.returncode is not None:
                logger.warning("Internal cloudflared exited with code %s; restarting...", _cloudflared_proc.returncode)
                await _spawn_internal_cloudflared(port)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug("Cloudflare monitor check error: %s", exc)
        await asyncio.sleep(10)


def start_cloudflare_auto_tunnel() -> None:
    global _monitor_task
    if _monitor_task is None or _monitor_task.done():
        _monitor_task = asyncio.create_task(_cloudflare_monitor_loop())


async def stop_cloudflare_auto_tunnel() -> None:
    global _monitor_task, _cloudflared_proc
    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()
        try:
            await _monitor_task
        except asyncio.CancelledError:
            pass
    _monitor_task = None
    if _cloudflared_proc and _cloudflared_proc.returncode is None:
        try:
            _cloudflared_proc.terminate()
            await asyncio.wait_for(_cloudflared_proc.wait(), timeout=5)
        except Exception:
            pass
    _cloudflared_proc = None
