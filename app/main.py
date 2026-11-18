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
    db,
)
from app.streamer.manager import session_manager
from app.streamer.engine import get_streaming_response, get_remux_response
from app.streamer.probe import probe_tracks
from app.bot.main import register_handlers
from app.admin.routes import router as admin_router
from app.cms import router as cms_router, get_settings, seed_categories, serialize_catalog_doc
from app.shorteners import (
    gate_request, verify_key, config as shortener_config, create_shortlink,
    get_key_page, is_protected_title_target,
)
from app.utils.cloudflare_tunnel import (
    apply_auto_cloudflare_url,
    start_cloudflare_auto_tunnel,
    stop_cloudflare_auto_tunnel,
)
from app.utils.helpers import is_trycloudflare_url
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import asyncio
import logging
import os
import sys
from pathlib import Path

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await mongo_client.admin.command("ping")
        await create_indexes()
        await load_custom_settings()
        await seed_categories()
        start_cloudflare_auto_tunnel()
        session_manager.prepare_bot()
        register_handlers(session_manager.bot_client)
        await session_manager.start()
        from app.bot.main import setup_bot_commands
        await setup_bot_commands(session_manager.bot_client)
        if getattr(settings, "BASE_URL", None):
            os.environ["BASE_URL"] = str(settings.BASE_URL).rstrip("/")
        logger.info("HITLER service started (wzgram)")
        yield
    finally:
        await stop_cloudflare_auto_tunnel()
        await session_manager.stop()
        mongo_client.close()
        logger.info("HITLER service stopped")


app = FastAPI(title="HITLER Media Link Service", lifespan=lifespan)


@app.middleware("http")
async def auto_cloudflare_and_vercel_routing_middleware(request: Request, call_next):
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
            vercel_site = await get_active_vercel_website_url()
            if vercel_site:
                query = f"?{request.url.query}" if request.url.query else ""
                return RedirectResponse(url=f"{vercel_site}{path}{query}", status_code=307)

    gated = await gate_request(request)
    if gated is not None:
        return gated

    return await call_next(request)


app.include_router(admin_router)
app.include_router(cms_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Length", "Content-Range", "Accept-Ranges", "Content-Disposition", "Content-Type"],
)

APP_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


@app.post("/verify-key")
async def verify_site_key(request: Request):
    return await verify_key(request)


@app.get("/get-key-start")
async def start_key_flow(request: Request, target: str = "/"):
    import secrets, time, random
    if not is_protected_title_target(target, request.url.hostname):
        raise HTTPException(400, "Shortener verification is available only for /title/* pages")
    cfg = await shortener_config()
    providers = cfg.get("shorteners", [])
    providers = [x for x in providers if x.get("url") and x.get("api_key")]
    if not cfg.get("enabled") or not providers:
        raise HTTPException(503, "No active shortener configured")
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
    rec = await db.access_keys.find_one({"token": token, "expires_at": {"$gt": int(time.time())}})
    if not rec:
        raise HTTPException(410, "Key expired")
    response = RedirectResponse(rec["target"], status_code=303)
    response.set_cookie("site_access", token, max_age=max(1, rec["expires_at"] - int(time.time())), httponly=True, samesite="lax", secure=True)
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
    fs_info = session_manager.get_filestore_info()
    return {
        "status": "ok" if mongo_status == "ok" else "degraded",
        "service": "HITLER",
        "database": mongo_status,
        "telegram_sessions": sessions,
        "download_ready": mongo_status == "ok" and sessions > 0,
        "cloudflare_url": cf_url or "",
        "vercel_url": vercel_url or "",
        "filestore_bot": fs_info.get("username") or "",
    }


@app.get("/")
async def index(request: Request):
    initial_items = []
    initial_categories = []
    try:
        s = await get_settings()
        cf_base = s.get("cloudflare_url") or await get_active_cloudflare_url()
        dl_on = bool(s.get("downloads_enabled", True))
        fs_user = s.get("filestore_bot_username") or ""
        raw_items = await db.catalog_pages.find({"published": True}, {"_id": 0}).sort("created_at", -1).to_list(100)
        initial_items = [serialize_catalog_doc(r, cf_base, downloads_enabled=dl_on, filestore_bot_username=fs_user) for r in raw_items]
        raw_cats = await db.catalog_categories.find({}, {"_id": 0}).sort("name", 1).to_list(None)
        for c in raw_cats:
            c_copy = dict(c)
            if hasattr(c_copy.get("created_at"), "isoformat"):
                c_copy["created_at"] = c_copy["created_at"].isoformat()
            initial_categories.append(c_copy)
    except Exception as exc:
        logger.warning("Could not pre-load initial catalog for index: %s", exc)

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "initial_items": initial_items,
            "initial_categories": initial_categories,
        },
    )


async def _render_watch_for_code(request: Request, short_code: str):
    code = (short_code or "").strip()
    if not code:
        raise HTTPException(status_code=404, detail="Link not found")
    file_data = await files_col.find_one({"short_code": code})
    if not file_data:
        raise HTTPException(status_code=404, detail="Link not found")

    cf_base = (await get_active_cloudflare_url()) or settings.BASE_URL.rstrip("/")
    return templates.TemplateResponse(
        request=request,
        name="watch.html",
        context={
            "request": request,
            "file": file_data,
            "base_url": cf_base,
            "stream_base_url": cf_base,
            "player_settings": await get_settings(),
        },
    )


@app.api_route("/watch", methods=["GET", "HEAD"])
async def watch_query_page(request: Request, path: str = "", code: str = "", id: str = ""):
    """Support ddstream-style /watch?path=<short_code> alongside /watch/<short_code>?hash=<hash>."""
    target_code = (path or code or id or "").strip()
    return await _render_watch_for_code(request, target_code)


@app.api_route("/watch/{short_code}", methods=["GET", "HEAD"])
async def watch_page(request: Request, short_code: str):
    return await _render_watch_for_code(request, short_code)


@app.api_route("/dl/{short_code}", methods=["GET", "HEAD"])
@app.api_route("/dl/{short_code}/{extra_name:path}", methods=["GET", "HEAD"])
@app.api_route("/stream/{short_code}", methods=["GET", "HEAD"])
@app.api_route("/stream/{short_code}/{extra_name:path}", methods=["GET", "HEAD"])
async def stream_file(request: Request, short_code: str, extra_name: str = ""):
    """
    Public download (/dl/) and stream (/stream/) endpoint for Chrome, 1DM, ADM, IDM, Movi-Player, and external players.
    When downloads are disabled and a FileStore bot is configured, /dl/{short_code} redirects directly to the
    configured FileStore bot deep link so the user receives the file in Telegram chat.
    """
    runtime = await get_settings()
    if request.url.path.startswith("/dl/") and runtime.get("downloads_enabled") is False:
        fs = (runtime.get("filestore_bot_username") or "").strip().lstrip("@")
        if fs:
            return RedirectResponse(
                url=f"https://t.me/{fs}?start=file_{short_code}",
                status_code=302,
            )
        raise HTTPException(status_code=403, detail="Downloads are currently disabled")
    if request.url.path.startswith("/stream/") and runtime.get("streaming_enabled") is False:
        raise HTTPException(status_code=403, detail="Streaming is currently disabled")

    file_data = await files_col.find_one({"short_code": short_code})
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")

    if request.method.upper() == "GET" and not request.headers.get("range"):
        try:
            await files_col.update_one({"short_code": short_code}, {"$inc": {"access_count": 1}})
        except Exception:
            logger.debug("Could not update access counter for %s", short_code)

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
    file_data = await files_col.find_one({"short_code": short_code})
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")

    cached = file_data.get("tracks_info")
    if cached and not cached.get("error") and cached.get("probe_version", 0) >= 3:
        return JSONResponse(cached)

    client = session_manager.bot_client or session_manager.get_client()
    if not client:
        raise HTTPException(status_code=503, detail="Telegram session unavailable")

    try:
        chat_id = file_data.get("storage_chat_id") or file_data.get("chat_id")
        msg_id = file_data.get("storage_message_id") or file_data.get("message_id")
        msg = await client.get_messages(chat_id, msg_id)
        if isinstance(msg, list):
            msg = msg[0] if msg else None
        if not msg and file_data.get("chat_id") != chat_id:
            chat_id = file_data.get("chat_id")
            msg_id = file_data.get("message_id")
            msg = await client.get_messages(chat_id, msg_id)
            if isinstance(msg, list):
                msg = msg[0] if msg else None
        if not msg:
            raise HTTPException(status_code=404, detail="Media no longer available")

        if msg.photo and not (msg.document or msg.video or msg.audio):
            return JSONResponse({
                "video_tracks": [],
                "audio_tracks": [],
                "subtitle_tracks": [],
                "has_multiple_audio": False,
            })

        tracks_info = await probe_tracks(
            client,
            msg,
            int(file_data.get("file_size") or 0),
            chat_id=chat_id,
            message_id=msg_id,
        )

        if not tracks_info.get("error"):
            await files_col.update_one(
                {"short_code": short_code},
                {"$set": {"tracks_info": tracks_info}},
            )

        return JSONResponse(tracks_info)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Track probing error: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Failed to probe tracks")


@app.api_route("/remux/{short_code}", methods=["GET", "HEAD"])
async def remux_file(request: Request, short_code: str, audio: int = 0, aac: int = 1, start: float = 0.0):
    """Browser-friendly fragmented MP4 with selectable multi-audio track and optional start timestamp."""
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
        return await get_remux_response(
            clients=clients,
            chat_id=file_data["chat_id"],
            message_id=file_data["message_id"],
            file_size=int(file_data.get("file_size") or 0),
            filename=file_data.get("filename") or "media.mp4",
            audio_track=audio,
            force_aac=bool(aac),
            start_seconds=max(0.0, float(start or 0.0)),
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
    """Local-only style stream of a Telegram message media for mediainfo/ffprobe/ffmpeg (Range supported)."""
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
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False, loop="asyncio")
