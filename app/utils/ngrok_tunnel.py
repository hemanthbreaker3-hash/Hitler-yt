from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
from datetime import datetime, timezone
from pathlib import Path

from app.database.connection import db, settings, settings_col
from app.utils.helpers import normalize_public_base_url, rewrite_page_groups_to_cloudflare, is_trycloudflare_url, is_duckdns_url, is_dynu_url

logger = logging.getLogger(__name__)
_monitor_task: asyncio.Task | None = None
_ngrok_tunnel = None
_cloudflared_proc: asyncio.subprocess.Process | None = None
_last_synced_url: str | None = None

# Trycloudflare quick-tunnel URL printed by cloudflared, e.g. https://abc-xyz.trycloudflare.com
_CF_URL_RE = re.compile(
    r"https://[a-z0-9-]+\.trycloudflare\.com",
    re.I,
)


def get_tunnel_provider() -> str:
    """
    Resolve active tunnel provider from environment.

    Preferred (shell export / .env):
      export TUNNEL_PROVIDER=cloudflare   # quick tunnel → *.trycloudflare.com
      export TUNNEL_PROVIDER=ngrok         # pyngrok → *.ngrok-free.app / .dev

    Aliases accepted: cf, cloudflare-tunnel, trycloudflare → cloudflare
                      ngrok-free, pyngrok → ngrok
    Default: cloudflare (no bandwidth cap like free ngrok).
    """
    raw = (
        os.environ.get("TUNNEL_PROVIDER")
        or os.environ.get("TUNNEL")
        or os.environ.get("SERVER_TUNNEL")
        or os.environ.get("URL_SERVER")
        or ""
    ).strip().lower()
    if raw in ("cloudflare", "cf", "cloudflare-tunnel", "trycloudflare", "cloudflared"):
        return "cloudflare"
    if raw in ("ngrok", "ngrok-free", "pyngrok"):
        return "ngrok"
    # Auto: prefer cloudflare when binary exists and no explicit ngrok token-only setup
    if raw in ("", "auto"):
        if shutil.which("cloudflared"):
            return "cloudflare"
        if (os.environ.get("NGROK_AUTHTOKEN") or "").strip():
            return "ngrok"
        return "cloudflare"
    return "cloudflare"


def _update_env_file_urls(tunnel_url: str) -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text().splitlines()
        keys = {"BACKEND_URL": False, "NGROK_URL": False, "CLOUDFLARE_URL": False}
        output = []
        for line in lines:
            key = line.split("=", 1)[0].strip() if "=" in line else ""
            if key in keys:
                if not keys[key]:
                    output.append(f'{key}="{tunnel_url}"')
                    keys[key] = True
            elif key == "BASE_URL":
                raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                if raw.startswith(("http://localhost", "http://127.0.0.1")) or is_trycloudflare_url(raw):
                    output.append(f'BASE_URL="{tunnel_url}"')
                else:
                    output.append(line)
            else:
                output.append(line)
        for key, found in keys.items():
            if not found:
                output.append(f'{key}="{tunnel_url}"')
        env_path.write_text("\n".join(output) + "\n")
    except Exception as exc:
        logger.warning("Could not persist tunnel URL to .env: %s", exc)


async def apply_auto_cloudflare_url(raw_url: str, source: str = "auto-tunnel") -> str | None:
    """Persist a public tunnel URL (ngrok or trycloudflare) and rewrite catalog media buttons."""
    global _last_synced_url
    try:
        tunnel_url = normalize_public_base_url(raw_url)
    except ValueError:
        return None
    if not is_trycloudflare_url(tunnel_url):
        return None

    settings.BACKEND_URL = tunnel_url
    os.environ["BACKEND_URL"] = tunnel_url
    os.environ["NGROK_URL"] = tunnel_url
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
        for key in ("ngrok_url", "backend_url", "base_url", "cloudflare_url"):
            await settings_col.update_one(
                {"key": key},
                {"$set": {"value": tunnel_url, "updated_by": source, "updated_at": now}},
                upsert=True,
            )
        web_doc = await settings_col.find_one({"key": "website_urls"}, {"value": 1})
        if web_doc and isinstance(web_doc.get("value"), list):
            cleaned = [u for u in web_doc["value"] if not is_trycloudflare_url(str(u))]
            if cleaned != web_doc["value"]:
                await settings_col.update_one(
                    {"key": "website_urls"},
                    {"$set": {"value": cleaned, "updated_at": now}},
                )
        rewritten = 0
        async for doc in db.catalog_pages.find({"groups.0": {"$exists": True}}, {"_id": 1, "groups": 1}):
            old = doc.get("groups") or []
            new = rewrite_page_groups_to_cloudflare(old, tunnel_url)
            if new != old:
                await db.catalog_pages.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"groups": new, "updated_at": now}},
                )
                rewritten += 1
        _update_env_file_urls(tunnel_url)
        _last_synced_url = tunnel_url
        logger.info(
            "Synced public tunnel URL (%s): %s; updated %d catalog pages",
            source,
            tunnel_url,
            rewritten,
        )
    except Exception as exc:
        logger.warning("Could not persist tunnel URL to MongoDB: %s", exc)
    return tunnel_url


def _extract_tunnel_url_from_logs() -> str | None:
    """Return the currently configured public tunnel URL."""
    return (
        os.environ.get("CLOUDFLARE_URL")
        or os.environ.get("NGROK_URL")
        or _last_synced_url
    )


async def _start_cloudflared_quick_tunnel(port: int) -> str | None:
    """
    Start Cloudflare quick tunnel (no account required):
      cloudflared tunnel --url http://127.0.0.1:PORT
    Parse the generated https://*.trycloudflare.com URL from process output.
    """
    global _cloudflared_proc
    binary = shutil.which("cloudflared")
    if not binary:
        logger.error(
            "cloudflared binary not found. Install it in the image/host, or set TUNNEL_PROVIDER=ngrok."
        )
        return None

    # Kill previous instance if any
    if _cloudflared_proc and _cloudflared_proc.returncode is None:
        try:
            _cloudflared_proc.terminate()
            await asyncio.wait_for(_cloudflared_proc.wait(), timeout=5)
        except Exception:
            try:
                _cloudflared_proc.kill()
            except Exception:
                pass
        _cloudflared_proc = None

    target = f"http://127.0.0.1:{port}"
    logger.info("Starting Cloudflare quick tunnel → %s", target)
    _cloudflared_proc = await asyncio.create_subprocess_exec(
        binary,
        "tunnel",
        "--url",
        target,
        "--no-autoupdate",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    url: str | None = None
    deadline = asyncio.get_event_loop().time() + 45

    async def _read_lines():
        nonlocal url
        assert _cloudflared_proc and _cloudflared_proc.stdout
        while url is None and _cloudflared_proc.returncode is None:
            try:
                line_b = await asyncio.wait_for(_cloudflared_proc.stdout.readline(), timeout=2)
            except asyncio.TimeoutError:
                if asyncio.get_event_loop().time() > deadline:
                    break
                continue
            if not line_b:
                break
            line = line_b.decode("utf-8", errors="replace").strip()
            if line:
                logger.debug("cloudflared: %s", line)
            m = _CF_URL_RE.search(line)
            if m:
                url = m.group(0).rstrip("/")
                break

    await _read_lines()

    if not url:
        logger.error(
            "Could not parse trycloudflare.com URL from cloudflared within timeout. "
            "Check cloudflared logs / network."
        )
        return None

    logger.info("Cloudflare quick tunnel URL: %s", url)
    return url


async def _cloudflare_monitor_loop() -> None:
    """Keep a Cloudflare quick tunnel alive and re-sync URL on reconnect."""
    global _cloudflared_proc
    await asyncio.sleep(2)
    port = int(os.environ.get("PORT", settings.PORT or 8000))
    while True:
        try:
            url = await _start_cloudflared_quick_tunnel(port)
            if url:
                await apply_auto_cloudflare_url(url, source="cloudflare-startup")
            else:
                await asyncio.sleep(15)
                continue

            # Supervise process; if it dies, restart
            while _cloudflared_proc and _cloudflared_proc.returncode is None:
                await asyncio.sleep(20)
                # Optionally re-affirm URL in Mongo every few minutes is handled by startup source only

            logger.warning("cloudflared exited (code=%s); restarting in 5s", getattr(_cloudflared_proc, "returncode", None))
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Cloudflare tunnel monitor error: %s", exc)
            await asyncio.sleep(15)


async def _ngrok_monitor_loop() -> None:
    global _ngrok_tunnel
    await asyncio.sleep(2)
    token = (os.environ.get("NGROK_AUTHTOKEN") or "").strip()
    if not token:
        logger.warning(
            "NGROK_AUTHTOKEN is not set; ngrok tunnel disabled. "
            "Set TUNNEL_PROVIDER=cloudflare or add NGROK_AUTHTOKEN to .env."
        )
        return
    try:
        from pyngrok import ngrok

        port = int(os.environ.get("PORT", settings.PORT or 8000))

        def open_tunnel():
            ngrok.set_auth_token(token)
            return ngrok.connect(addr=port, proto="http")

        _ngrok_tunnel = await asyncio.to_thread(open_tunnel)
        url = getattr(_ngrok_tunnel, "public_url", None)
        if url:
            await apply_auto_cloudflare_url(url, source="ngrok-startup")
            logger.info("ngrok public URL: %s", url)
        while True:
            await asyncio.sleep(30)
            try:
                tunnels = await asyncio.to_thread(ngrok.get_tunnels)
                active = next((t for t in tunnels if getattr(t, "public_url", None) == url), None)
                if not active:
                    _ngrok_tunnel = await asyncio.to_thread(open_tunnel)
                    url = getattr(_ngrok_tunnel, "public_url", None)
                    if url:
                        await apply_auto_cloudflare_url(url, source="ngrok-reconnect")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("ngrok monitor error: %s", exc)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Could not start ngrok tunnel: %s", exc)


def start_cloudflare_auto_tunnel() -> None:
    """
    Start the public tunnel based on TUNNEL_PROVIDER.

    Shell examples:
      export TUNNEL_PROVIDER=cloudflare   # generate trycloudflare.com and sync
      export TUNNEL_PROVIDER=ngrok         # use NGROK_AUTHTOKEN
    """
    global _monitor_task
    if _monitor_task is not None and not _monitor_task.done():
        return
    provider = get_tunnel_provider()
    logger.info("Public tunnel provider: %s", provider)
    if provider == "ngrok":
        _monitor_task = asyncio.create_task(_ngrok_monitor_loop())
    else:
        _monitor_task = asyncio.create_task(_cloudflare_monitor_loop())


async def stop_cloudflare_auto_tunnel() -> None:
    global _monitor_task, _ngrok_tunnel, _cloudflared_proc
    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()
        try:
            await _monitor_task
        except asyncio.CancelledError:
            pass
    _monitor_task = None

    if _cloudflared_proc is not None:
        try:
            if _cloudflared_proc.returncode is None:
                _cloudflared_proc.terminate()
                try:
                    await asyncio.wait_for(_cloudflared_proc.wait(), timeout=5)
                except Exception:
                    _cloudflared_proc.kill()
        except Exception:
            pass
        _cloudflared_proc = None

    if _ngrok_tunnel is not None:
        try:
            from pyngrok import ngrok

            await asyncio.to_thread(ngrok.disconnect, _ngrok_tunnel.public_url)
            await asyncio.to_thread(ngrok.kill)
        except Exception:
            pass
        _ngrok_tunnel = None
