from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse, RedirectResponse
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
from app.utils.cloudflare_tunnel import (
    apply_auto_cloudflare_url,
    start_cloudflare_auto_tunnel,
    stop_cloudflare_auto_tunnel,
)
from app.utils.helpers import is_trycloudflare_url
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import asyncio
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
    """
    1. Automatically detect any incoming *.trycloudflare.com Host header, save it to MongoDB,
       and replace all catalog button URL domains with that Cloudflare try URL.
    2. If a user accesses website-only catalog pages (/, /search, /title/*) on a trycloudflare.com
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

        # Website is strictly on Vercel when vercel_url is configured; keep trycloudflare.com for download & stream links
        path = request.url.path
        if path in ("/", "/search") or path.startswith("/title/"):
            vercel_site = await get_active_vercel_website_url()
            if vercel_site:
                query = f"?{request.url.query}" if request.url.query else ""
                return RedirectResponse(url=f"{vercel_site}{path}{query}", status_code=307)

    return await call_next(request)


# Include Routers
app.include_router(admin_router)
app.include_router(cms_router)

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Enable Compression
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Mount Static Files & Templates
APP_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


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
        "service": "HITLER",
        "database": mongo_status,
        "telegram_sessions": sessions,
        "download_ready": mongo_status == "ok" and sessions > 0,
        "cloudflare_url": cf_url or "",
        "vercel_url": vercel_url or "",
    }


@app.get("/")
async def index(request: Request):
    initial_items = []
    initial_categories = []
    try:
        cf_base = await get_active_cloudflare_url()
        raw_items = await db.catalog_pages.find({"published": True}, {"_id": 0}).sort("created_at", -1).to_list(100)
        initial_items = [serialize_catalog_doc(r, cf_base) for r in raw_items]
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


@app.get("/watch/{short_code}")
async def watch_page(request: Request, short_code: str):
    file_data = await files_col.find_one({"short_code": short_code})
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


@app.get("/dl/{short_code}")
@app.get("/stream/{short_code}")
async def stream_file(request: Request, short_code: str):
    runtime = await get_settings()
    if request.url.path.startswith("/dl/") and not runtime["downloads_enabled"]:
        raise HTTPException(status_code=403, detail="Downloads are currently disabled")
    if request.url.path.startswith("/stream/") and not runtime["streaming_enabled"]:
        raise HTTPException(status_code=403, detail="Streaming is currently disabled")
    file_data = await files_col.find_one({"short_code": short_code})
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")

    await files_col.update_one({"short_code": short_code}, {"$inc": {"access_count": 1}})

    all_clients = session_manager.get_all_clients()
    if not all_clients:
        raise HTTPException(status_code=503, detail="Telegram sessions are unavailable")

    return await get_streaming_response(
        clients=all_clients,
        chat_id=file_data["chat_id"],
        message_id=file_data["message_id"],
        file_size=file_data["file_size"],
        filename=file_data["filename"],
        mime_type=file_data["mime_type"],
        request=request,
        chunk_size=settings.STREAM_CHUNK_SIZE,
    )


@app.get("/api/tracks/{short_code}")
async def get_tracks(short_code: str):
    """Return available audio/video/subtitle tracks for a media file."""
    file_data = await files_col.find_one({"short_code": short_code})
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")

    cached = file_data.get('tracks_info')
    if cached and not cached.get('error'):
        return JSONResponse(cached)

    client = session_manager.get_client()

    try:
        msg = await client.get_messages(file_data['chat_id'], file_data['message_id'])
        if isinstance(msg, list):
            msg = msg[0] if msg else None
        if not msg:
            raise HTTPException(status_code=404, detail="Media no longer available")

        if msg.photo and not (msg.document or msg.video or msg.audio):
            return JSONResponse({
                "video_tracks": [],
                "audio_tracks": [],
                "subtitle_tracks": [],
                "has_multiple_audio": False
            })

        tracks_info = await probe_tracks(client, msg, file_data['file_size'])

        if not tracks_info.get('error'):
            await files_col.update_one(
                {"short_code": short_code},
                {"$set": {"tracks_info": tracks_info}}
            )

        return JSONResponse(tracks_info)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Track probing error: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Failed to probe tracks")


@app.get("/remux/{short_code}")
async def remux_file(request: Request, short_code: str, audio: int = 0, aac: int = 1):
    """Browser-friendly fragmented MP4; AAC by default for universal audio."""
    if not (await get_settings())["streaming_enabled"]:
        raise HTTPException(status_code=403, detail="Streaming is currently disabled")
    file_data = await files_col.find_one({"short_code": short_code})
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")
    if audio < 0 or audio > 32:
        raise HTTPException(status_code=400, detail="Invalid audio track")

    clients = session_manager.get_all_clients()
    if not clients:
        raise HTTPException(status_code=503, detail="Telegram sessions are unavailable")
    try:
        return await get_remux_response(
            clients=clients,
            chat_id=file_data["chat_id"],
            message_id=file_data["message_id"],
            file_size=file_data["file_size"],
            filename=file_data["filename"],
            audio_track=audio,
            force_aac=bool(aac),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Remux error: %s", e)
        raise HTTPException(status_code=500, detail="Error starting remux stream")


@app.get("/_tgstream/{chat_id}/{message_id}")
async def internal_tg_stream(request: Request, chat_id: int, message_id: int):
    """Local-only style stream of a Telegram message media for mediainfo/ffmpeg (Range supported)."""
    clients = session_manager.get_all_clients()
    if not clients:
        raise HTTPException(status_code=503, detail="No Telegram sessions")
    client = clients[0]
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
            file_size = msg.photo.file_size or 0
            filename = f"photo_{message_id}.jpg"
            mime = "image/jpeg"
        else:
            file_size = getattr(media, "file_size", None) or 0
            filename = getattr(media, "file_name", None) or f"file_{message_id}"
            mime = getattr(media, "mime_type", None) or "application/octet-stream"
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
