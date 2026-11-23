from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.responses import HTMLResponse
from app.database.connection import (
    settings,
    files_col,
    client as mongo_client,
    create_indexes,
    load_custom_settings,
    get_active_cloudflare_url,
    get_active_vercel_website_url,
    get_active_website_url,
    db,
)
from app.streamer.manager import session_manager
from app.streamer.engine import get_streaming_response, get_remux_response
from app.streamer.probe import probe_tracks
from app.bot.main import register_handlers
from app.admin.routes import router as admin_router
from app.cms import router as cms_router, get_settings, seed_categories, serialize_catalog_doc, backfill_tmdb_backdrops
from app.shorteners import (
    gate_request, verify_key, config as shortener_config, create_shortlink,
    get_key_page, is_protected_title_target,
)
from app.utils.ngrok_tunnel import (
    apply_auto_cloudflare_url,
    start_cloudflare_auto_tunnel,
    stop_cloudflare_auto_tunnel,
)
from app.utils.helpers import is_trycloudflare_url
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import asyncio
import time
import logging
import os
import sys
from pathlib import Path

# Configure Windows Event Loop Policy for subprocess support
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic
    try:
        await mongo_client.admin.command("ping")
        await create_indexes()
        await load_custom_settings()
        await seed_categories()
        # Background: fill landscape backdrops for existing catalog pages from TMDB
        async def _bg_backfill():
            try:
                await backfill_tmdb_backdrops(limit=100)
            except Exception:
                pass
        import asyncio as _asyncio
        _asyncio.create_task(_bg_backfill())
        from app.utils.duckdns import start_duckdns_updater, sync_and_activate_duckdns
        try:
            await sync_and_activate_duckdns()
        except Exception as _duck_err:
            logger.warning('Startup DuckDNS activation skipped: %s', _duck_err)
        start_cloudflare_auto_tunnel()
        start_duckdns_updater()
        session_manager.prepare_bot()
        register_handlers(session_manager.bot_client)
        await session_manager.start()
        from app.bot.main import setup_bot_commands, start_filestore_client, stop_filestore_client, refresh_runtime_admin_cache
        await setup_bot_commands(session_manager.bot_client)
        try:
            await refresh_runtime_admin_cache()
        except Exception as e:
            logger.warning("Admin cache refresh failed: %s", e)
        # Restore FileStore secondary bot if a token was previously saved
        try:
            runtime = await db.site_settings.find_one({"_id": "runtime"}) or {}
            fs_token = (runtime.get("filestore_bot_token") or "").strip()
            fs_user = (runtime.get("filestore_bot_username") or "").strip().lstrip("@")
            if fs_token:
                info = await start_filestore_client(fs_token, username=fs_user or None)
                logger.info("FileStore restore: %s", info)
        except Exception as e:
            logger.warning("Could not restore FileStore secondary bot: %s", e)
        if getattr(settings, "BASE_URL", None):
            os.environ["BASE_URL"] = str(settings.BASE_URL).rstrip("/")
        logger.info("HITLER YT service started (wzgram)")
        yield
    finally:
        try:
            from app.bot.main import stop_filestore_client
            await stop_filestore_client()
        except Exception:
            pass
        from app.utils.duckdns import stop_duckdns_updater
        await stop_duckdns_updater()
        await stop_cloudflare_auto_tunnel()
        await session_manager.stop()
        mongo_client.close()
        logger.info("HITLER YT service stopped")


app = FastAPI(title="HITLER YT Media & Anime Service", lifespan=lifespan)


@app.middleware("http")
async def auto_cloudflare_and_vercel_routing_middleware(request: Request, call_next):
    """
    1. Automatically detect any incoming *.ngrok-free.app Host header, save it to MongoDB,
       and replace all catalog button URL domains with that ngrok public URL.
    2. If a user accesses website-only catalog pages (/, /search, /title/*) on a ngrok-free.app
       hostname and a Vercel website URL is registered, redirect them to the Vercel website.
    """
    raw_host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or ""
    ).split(",")[0].strip()

    if raw_host and is_trycloudflare_url(raw_host):
        cf_url = f"https://{raw_host}"
        if cf_url != getattr(settings, "BACKEND_URL", None):
            asyncio.create_task(apply_auto_cloudflare_url(cf_url, source="incoming-request"))

        path = request.url.path
        if path in ("/", "/search") or path.startswith("/title/"):
            target_site = await get_active_website_url()
            if target_site and not target_site.rstrip("/").endswith(raw_host):
                query = f"?{request.url.query}" if request.url.query else ""
                return RedirectResponse(url=f"{target_site}{path}{query}", status_code=307)

    gated = await gate_request(request)
    if gated is not None:
        return gated

    return await call_next(request)


# Include Routers
app.include_router(admin_router)
app.include_router(cms_router)

# Enable CORS with exposed Range headers for Chrome, 1DM, ADM, IDM, and web players
# Note: Do NOT enable GZipMiddleware because gzip-compressing binary /dl/ and /stream/ responses
# breaks Content-Length / 206 Partial Content byte ranges in Chrome and 1DM!
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Length", "Content-Range", "Accept-Ranges", "Content-Disposition"],
)

# Mount Static Files & Templates
APP_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.globals["BRANDING"] = settings.BRANDING
templates.env.globals["LOGO_URL"] = settings.LOGO_URL


@app.post("/verify-key")
async def verify_site_key(request: Request):
    return await verify_key(request)

@app.get("/get-key-start")
async def start_key_flow(request: Request, target: str = "/"):
    import secrets, time
    if not is_protected_title_target(target, request.url.hostname):
        raise HTTPException(400, "Shortener verification is available only for /title/* pages")
    cfg = await shortener_config()
    providers = cfg.get("shorteners", [])
    providers = [x for x in providers if x.get("url") and x.get("api_key")]
    if not cfg.get("enabled") or not providers:
        raise HTTPException(503, "No active shortener configured")
    import random
    provider = random.choice(providers)
    token, key = secrets.token_urlsafe(24), secrets.token_urlsafe(9)
    expiry = max(1, int(cfg.get("expiry_seconds", 300)))
    now = int(time.time())
    host = str(request.base_url).rstrip("/")
    landing = f"{host}/get-key/{token}"
    await db.access_keys.insert_one({
        "token": token,
        "key": key,
        "target": target,
        "scope": "site",
        "created_at": now,
        "expires_at": now + expiry,
    })
    try:
        # Format: https://shortlinkgenerator.onrender.com/api?api=KEY&url=DEST&format=json
        short_url = await create_shortlink(provider["url"], provider["api_key"], landing)
    except Exception as exc:
        logger.exception("Shortener API failed")
        raise HTTPException(502, f"Shortener API failed: {type(exc).__name__}: {exc}")
    return RedirectResponse(short_url, status_code=302)

@app.get("/get-key/{token}")
async def show_generated_key(request: Request, token: str):
    return await get_key_page(request, token)

@app.get("/key-copied/{token}")
async def key_copied(token: str):
    import time
    rec = await db.access_keys.find_one({"token":token,"expires_at":{"$gt":int(time.time())}})
    if not rec: raise HTTPException(410, "Key expired")
    response = RedirectResponse(rec["target"], status_code=303)
    response.set_cookie("site_access", token, max_age=max(1,rec["expires_at"]-int(time.time())), httponly=True, samesite="lax", secure=True)
    return response

@app.get("/health")
@app.get("/api/health")
async def health():
    mongo_status = "ok"
    try:
        await mongo_client.admin.command("ping")
    except Exception as exc:
        mongo_status = f"error:{type(exc).__name__}"
    sessions = len(session_manager.get_all_clients())
    cf_url = await get_active_cloudflare_url()
    vercel_url = await get_active_vercel_website_url()
    return {
        "status": "ok" if mongo_status == "ok" else "degraded",
        "service": "HITLER YT",
        "database": mongo_status,
        "telegram_sessions": sessions,
        "download_ready": mongo_status == "ok" and sessions > 0,
        "cloudflare_url": cf_url or "",
        "vercel_url": vercel_url or "",
    }


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"error": "Internal Server Error", "detail": f"{type(exc).__name__}: {exc}"}, status_code=500)

@app.get("/")
async def index(request: Request):
    initial_items = []
    initial_categories = []
    try:
        cf_base = await get_active_cloudflare_url()
        raw_items = await db.catalog_pages.find({"published": True}, {"_id": 0, "overview": 0}).sort("created_at", -1).to_list(1000)
        initial_items = []
        for r in raw_items:
            sd = serialize_catalog_doc(r, cf_base)
            sd.pop("groups", None)
            initial_items.append(sd)
        raw_cats = await db.catalog_categories.find({}, {"_id": 0}).sort("name", 1).to_list(None)
        for c in raw_cats:
            c_copy = dict(c)
            if hasattr(c_copy.get("created_at"), "isoformat"):
                c_copy["created_at"] = c_copy["created_at"].isoformat()
            initial_categories.append(c_copy)
    except Exception as exc:
        logger.warning("Could not pre-load initial catalog for index: %s", exc)

    import json as _json
    initial_items = _json.loads(_json.dumps(initial_items, default=str))
    initial_categories = _json.loads(_json.dumps(initial_categories, default=str))
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "initial_items": initial_items,
            "initial_categories": initial_categories,
        },
    )


@app.api_route("/watch/{short_code}", methods=["GET", "HEAD"])
async def watch_page(request: Request, short_code: str):
    file_data = await files_col.find_one({"short_code": short_code})
    if not file_data:
        raise HTTPException(status_code=404, detail="Link not found")

    cf_base = (await get_active_cloudflare_url()) or settings.BASE_URL.rstrip("/")
    player_settings = await get_settings()

    # Optional catalog title context: ?title=<slug>&g=<group>&b=<button>
    title_slug = (request.query_params.get("title") or "").strip()
    try:
        group_index = max(0, int(request.query_params.get("g") or 0))
    except (TypeError, ValueError):
        group_index = 0
    try:
        button_index = max(0, int(request.query_params.get("b") or 0))
    except (TypeError, ValueError):
        button_index = 0

    watch_groups = []
    title_page = None
    try:
        raw_page = None
        if title_slug:
            raw_page = await db.catalog_pages.find_one(
                {"slug": title_slug, "published": True}, {"_id": 0}
            )
        if not raw_page and short_code:
            # Resolve title page by matching this short code in any watch button URL
            import re as _re
            code_re = _re.escape(short_code)
            raw_page = await db.catalog_pages.find_one(
                {"published": True, "groups.buttons.url": {"$regex": code_re, "$options": "i"}},
                {"_id": 0},
            )
            if not raw_page:
                async for cand in db.catalog_pages.find(
                    {"published": True, "groups.0": {"$exists": True}},
                    {"_id": 0, "slug": 1, "groups": 1, "poster_path": 1, "title": 1, "name": 1},
                ).limit(200):
                    groups = cand.get("groups") or []
                    hit = any(
                        short_code in str((b or {}).get("url") or (b or {}).get("link") or "")
                        for g in groups for b in ((g or {}).get("buttons") or [])
                    )
                    if hit:
                        raw_page = cand
                        break
        if raw_page:
            title_page = serialize_catalog_doc(raw_page, cf_base)
            if not title_slug:
                title_slug = str(title_page.get("slug") or "")
            all_groups = title_page.get("groups") or []
            for g in all_groups:
                box = str((g or {}).get("box") or "watch").strip().lower()
                if box not in ("watch", ""):
                    continue
                buttons = []
                for btn in (g.get("buttons") or []):
                    if not isinstance(btn, dict):
                        continue
                    url = (btn.get("url") or btn.get("link") or "").strip()
                    name = (btn.get("name") or btn.get("text") or "Watch").strip() or "Watch"
                    if url:
                        buttons.append({"name": name, "url": url})
                if buttons:
                    watch_groups.append({
                        "name": (g.get("name") or "Watch Group").strip() or "Watch Group",
                        "buttons": buttons,
                    })
            if group_index >= len(watch_groups):
                group_index = 0
            if watch_groups:
                btns = watch_groups[group_index].get("buttons") or []
                if button_index >= len(btns):
                    button_index = 0
    except Exception as exc:
        logger.warning("watch title context load failed: %s", exc)

    fs_user = (player_settings.get("filestore_bot_username") or "").strip().lstrip("@")

    return templates.TemplateResponse(
        request=request,
        name="watch.html",
        context={
            "request": request,
            "file": file_data,
            "base_url": cf_base,
            "stream_base_url": cf_base,
            "player_settings": player_settings,
            "title_slug": title_slug or "",
            "title_page": title_page,
            "watch_groups": watch_groups,
            "group_index": group_index,
            "button_index": button_index,
            "filestore_bot_username": fs_user,
        },
    )


@app.post("/api/views/{short_code}")
async def register_view(short_code: str, request: Request):
    """Count one playback view per browser token per file; retries are idempotent."""
    payload = await request.json()
    token = str(payload.get("visitor_token") or "").strip()
    if len(token) < 20 or len(token) > 128:
        raise HTTPException(status_code=400, detail="Invalid visitor token")
    file_data = await files_col.find_one({"short_code": short_code}, {"_id": 1, "access_count": 1})
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")
    views_col = db.playback_views
    try:
        await views_col.create_index([("short_code", 1), ("visitor_token", 1)], unique=True)
        await views_col.insert_one({"short_code": short_code, "visitor_token": token, "created_at": datetime.utcnow()})
        await files_col.update_one({"short_code": short_code}, {"$inc": {"access_count": 1}})
    except Exception as exc:
        # Duplicate token means this browser already counted this file. Other DB errors
        # are logged but do not interrupt playback.
        if exc.__class__.__name__ not in ("DuplicateKeyError",):
            logger.warning("View registration failed for %s: %s", short_code, exc)
    latest = await files_col.find_one({"short_code": short_code}, {"access_count": 1})
    return {"views": int((latest or {}).get("access_count") or 0)}


_FILE_META_CACHE: dict[str, tuple[dict, float]] = {}
_REMUX_SEM = asyncio.Semaphore(4)
_TRACKS_CACHE: dict[str, tuple[float, dict]] = {}
_TRACKS_CACHE_TTL = 600.0


async def _get_file_cached(short_code: str):
    import time as _t
    now = _t.monotonic()
    hit = _FILE_META_CACHE.get(short_code)
    if hit and (now - hit[1]) < 60.0:
        return hit[0]
    doc = await files_col.find_one({"short_code": short_code})
    if doc:
        if len(_FILE_META_CACHE) > 5000:
            for k in list(_FILE_META_CACHE.keys())[:1500]:
                _FILE_META_CACHE.pop(k, None)
        _FILE_META_CACHE[short_code] = (doc, now)
    return doc


@app.api_route("/dl/{short_code}", methods=["GET", "HEAD"])
@app.api_route("/dl/{short_code}/{extra_name:path}", methods=["GET", "HEAD"])
@app.api_route("/stream/{short_code}", methods=["GET", "HEAD"])
@app.api_route("/stream/{short_code}/{extra_name:path}", methods=["GET", "HEAD"])

async def stream_file(request: Request, short_code: str, extra_name: str = ""):
    """
    Public download (/dl/) and stream (/stream/) endpoint for any user in Chrome, 1DM, ADM, IDM, or players.
    Supports both GET and HEAD requests and RFC 7233 Byte Ranges.
    """
    runtime = await get_settings()
    if request.url.path.startswith("/dl/") and runtime.get("downloads_enabled") is False:
        fs = (runtime.get("filestore_bot_username") or "").strip().lstrip("@")
        if fs:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(
                url=f"https://t.me/{fs}?start=file_{short_code}",
                status_code=302,
            )
        raise HTTPException(status_code=403, detail="Downloads are currently disabled")
    if request.url.path.startswith("/stream/") and runtime.get("streaming_enabled") is False:
        raise HTTPException(status_code=403, detail="Streaming is currently disabled")

    file_data = await _get_file_cached(short_code)
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")
    ext_url = file_data.get("external_url")
    if ext_url:
        import httpx as _httpx
        from starlette.responses import StreamingResponse
        from starlette.background import BackgroundTask
        req_headers = {}
        if request.headers.get("range"):
            req_headers["Range"] = request.headers["range"]
        _client = _httpx.AsyncClient(follow_redirects=True, timeout=30.0)
        try:
            _req = _client.build_request(request.method, ext_url, headers=req_headers)
            _resp = await _client.send(_req, stream=True)
            if _resp.status_code >= 400:
                await _resp.aclose()
                await _client.aclose()
                raise HTTPException(status_code=410, detail="Download link is dead")
            resp_headers = {}
            for _h in ("content-type", "content-length", "content-range", "accept-ranges", "content-disposition"):
                if _resp.headers.get(_h):
                    resp_headers[_h] = _resp.headers[_h]
            if "accept-ranges" not in resp_headers:
                resp_headers["accept-ranges"] = "bytes"
            async def _cleanup():
                await _resp.aclose()
                await _client.aclose()
            return StreamingResponse(
                _resp.aiter_bytes(chunk_size=131072),
                status_code=_resp.status_code,
                headers=resp_headers,
                media_type=resp_headers.get("content-type") or file_data.get("mime_type") or "video/mp4",
                background=BackgroundTask(_cleanup),
            )
        except HTTPException:
            raise
        except Exception as _exc:
            await _client.aclose()
            raise HTTPException(status_code=410, detail="Download link is dead or unreachable") from _exc
    ext_url = file_data.get("external_url")
    if ext_url:
        import httpx as _httpx
        try:
            async with _httpx.AsyncClient(follow_redirects=True, timeout=6.0) as _hc:
                _r = await _hc.head(ext_url)
                if _r.status_code in (403, 405, 501):
                    async with _hc.stream("GET", ext_url, headers={"Range": "bytes=0-0"}) as _gr:
                        _st = _gr.status_code
                else:
                    _st = _r.status_code
                if _st >= 400:
                    raise HTTPException(status_code=410, detail="Watch link is dead (source download link is offline)")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=410, detail="Watch link is dead (source download link is unreachable)")

    # Views are counted only when playback starts via /api/views/{short_code}.
    # Stream, HEAD and byte-range requests are intentionally not counted.

    all_clients = session_manager.get_all_clients()
    if not all_clients and session_manager.bot_client is None:
        raise HTTPException(status_code=503, detail="Telegram sessions are unavailable")

    try:
        return await get_streaming_response(
            clients=all_clients,
            chat_id=file_data.get("chat_id"),
            message_id=file_data.get("message_id"),
            file_size=int(file_data.get("file_size") or 0),
            filename=file_data.get("filename") or extra_name or "media",
            mime_type=file_data.get("mime_type") or "application/octet-stream",
            request=request,
            chunk_size=settings.STREAM_CHUNK_SIZE,
            file_id=file_data.get("file_id"),
            storage_chat_id=file_data.get("storage_chat_id"),
            storage_message_id=file_data.get("storage_message_id"),
            bot_client=session_manager.bot_client,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Download/stream setup failed for short_code=%s path=%s", short_code, request.url.path)
        raise HTTPException(status_code=503, detail="Telegram media is temporarily unavailable. Please retry shortly.") from exc


@app.get("/api/tracks/{short_code}")
async def get_tracks(short_code: str):
    """Return available audio/video/subtitle tracks for a media file."""
    now = time.monotonic()
    mem_cached = _TRACKS_CACHE.get(short_code)
    if mem_cached and (now - mem_cached[0]) < _TRACKS_CACHE_TTL:
        return JSONResponse(mem_cached[1], headers={"Cache-Control": "public, max-age=3600, s-maxage=3600"})
    file_data = await _get_file_cached(short_code)
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")

    cached = file_data.get('tracks_info')
    if cached and not cached.get('error'):
        if len(_TRACKS_CACHE) > 5000:
            _TRACKS_CACHE.clear()
        _TRACKS_CACHE[short_code] = (now, cached)
        return JSONResponse(cached, headers={"Cache-Control": "public, max-age=3600, s-maxage=3600"})

    client = session_manager.bot_client or session_manager.get_client()
    if not client:
        raise HTTPException(status_code=503, detail="Telegram session unavailable")

    try:
        # Prefer original source message; storage channel is optional
        chat_id = file_data.get("chat_id") or file_data.get("storage_chat_id")
        msg_id = file_data.get("message_id") or file_data.get("storage_message_id")
        msg = await client.get_messages(chat_id, msg_id)
        if isinstance(msg, list):
            msg = msg[0] if msg else None
        if (not msg or getattr(msg, "empty", False)) and file_data.get("storage_chat_id") and file_data.get("storage_chat_id") != chat_id:
            msg = await client.get_messages(file_data["storage_chat_id"], file_data.get("storage_message_id"))
            if isinstance(msg, list):
                msg = msg[0] if msg else None
        if not msg or getattr(msg, "empty", False):
            raise HTTPException(status_code=404, detail="Media no longer available (bot needs access to the source chat)")

        if msg.photo and not (msg.document or msg.video or msg.audio):
            return JSONResponse({
                "video_tracks": [],
                "audio_tracks": [],
                "subtitle_tracks": [],
                "has_multiple_audio": False
            })

        tracks_info = await probe_tracks(client, msg, file_data['file_size'])

        if not tracks_info.get('error'):
            if len(_TRACKS_CACHE) > 5000:
                _TRACKS_CACHE.clear()
            _TRACKS_CACHE[short_code] = (now, tracks_info)
            await files_col.update_one(
                {"short_code": short_code},
                {"$set": {"tracks_info": tracks_info}}
            )

        return JSONResponse(tracks_info, headers={"Cache-Control": "public, max-age=3600, s-maxage=3600"})

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Track probing error: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Failed to probe tracks")


@app.api_route("/remux/{short_code}", methods=["GET", "HEAD"])
async def remux_file(request: Request, short_code: str, audio: int = 0, aac: int = 1):
    """Browser-friendly fragmented MP4; AAC by default for universal audio."""
    if (await get_settings()).get("streaming_enabled") is False:
        raise HTTPException(status_code=403, detail="Streaming is currently disabled")
    file_data = await files_col.find_one({"short_code": short_code})
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")
    if audio < 0 or audio > 32:
        raise HTTPException(status_code=400, detail="Invalid audio track")

    clients = session_manager.get_all_clients()
    if not clients and session_manager.bot_client is None:
        raise HTTPException(status_code=503, detail="Telegram sessions are unavailable")
    try:
        async with _REMUX_SEM:
            return await get_remux_response(
                clients=clients,
                chat_id=file_data["chat_id"],
                message_id=file_data["message_id"],
                file_size=int(file_data.get("file_size") or 0),
                filename=file_data.get("filename") or "media.mp4",
                audio_track=audio,
                force_aac=bool(aac),
                file_id=file_data.get("file_id"),
                storage_chat_id=file_data.get("storage_chat_id"),
                storage_message_id=file_data.get("storage_message_id"),
                bot_client=session_manager.bot_client,
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Remux error: %s", e)
        raise HTTPException(status_code=500, detail="Error starting remux stream")


@app.api_route("/_tgstream/{chat_id}/{message_id}", methods=["GET", "HEAD"])
async def internal_tg_stream(request: Request, chat_id: int, message_id: int):
    """Local-only style stream of a Telegram message media for mediainfo/ffmpeg (Range supported)."""
    clients = session_manager.get_all_clients()
    if not clients and session_manager.bot_client is None:
        raise HTTPException(status_code=503, detail="No Telegram sessions")
    client = session_manager.bot_client or clients[0]
    try:
        msg = await client.get_messages(chat_id, message_id)
        if isinstance(msg, list):
            msg = msg[0] if msg else None
        if not msg:
            raise HTTPException(status_code=404, detail="Message not found")
        media = msg.video or msg.document or msg.audio or msg.animation or msg.voice or msg.video_note
        if not media and not msg.photo:
            raise HTTPException(status_code=404, detail="No media")
        if msg.photo and not media:
            photo = msg.photo[-1] if isinstance(msg.photo, (list, tuple)) else msg.photo
            file_size = getattr(photo, "file_size", 0) or 0
            filename = f"photo_{message_id}.jpg"
            mime = "image/jpeg"
            fid = getattr(photo, "file_id", None)
        else:
            file_size = getattr(media, "file_size", None) or 0
            filename = getattr(media, "file_name", None) or f"file_{message_id}"
            mime = getattr(media, "mime_type", None) or "application/octet-stream"
            fid = getattr(media, "file_id", None)
        if file_size <= 0:
            raise HTTPException(status_code=422, detail="Unknown file size")
        return await get_streaming_response(
            clients=clients,
            chat_id=chat_id,
            message_id=message_id,
            file_size=file_size,
            filename=filename,
            mime_type=mime,
            request=request,
            chunk_size=settings.STREAM_CHUNK_SIZE,
            file_id=fid,
            bot_client=session_manager.bot_client,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("internal_tg_stream")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False, loop="asyncio")
