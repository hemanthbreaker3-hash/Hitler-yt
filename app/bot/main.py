from __future__ import annotations

import datetime
import html
import logging
import os
import mimetypes
import httpx
import secrets
import re

from wzgram import filters
from wzgram.enums import ParseMode
from wzgram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery, BotCommand

from app.database.connection import (
    files_col,
    users_col,
    settings_col,
    settings,
    db,
    get_active_cloudflare_url,
    get_active_website_url,
)
from app.admin.auth import make_password_hash
from app.cms import rewrite_all_stored_catalog_buttons
from app.models.schemas import FileMetadata, User
from app.utils.cloudflare_tunnel import _extract_tunnel_url_from_logs, apply_auto_cloudflare_url
from app.utils.helpers import (
    generate_short_code,
    format_bytes,
    is_trycloudflare_url,
    normalize_public_base_url,
)
from app.utils.fsub import is_user_fsubbed
from app.utils.rate_limit import check_rate_limit
from app.utils.media_tools import (
    cleanup_dir,
    download_message_media,
    generate_sample_video,
    generate_screenshots,
    make_work_dir,
    local_tg_stream_url,
)
from app.utils.mediainfo_fmt import extract_mediainfo, format_output
import io

logger = logging.getLogger(__name__)


def get_ist_greeting():
    ist_tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    now = datetime.datetime.now(ist_tz)
    hour = now.hour
    if 5 <= hour < 12:
        return "Good Morning 🌅"
    if 12 <= hour < 17:
        return "Good Afternoon ☀️"
    return "Good Evening 🌙"


def _kb(rows):
    return InlineKeyboardMarkup(rows)


async def is_access_allowed(user_id: int) -> bool:
    config = await db.site_settings.find_one({"_id": "runtime"}) or {}
    mode = config.get("access_mode", "all")
    if mode == "owners":
        return user_id == settings.OWNER_ID
    if mode == "admins":
        return user_id == settings.OWNER_ID or user_id in settings.admin_list
    return True


async def setup_bot_commands(bot):
    """Register all bot commands in the Telegram menu so they appear under the / button."""
    try:
        commands = [
            BotCommand("start", "Start / help"),
            BotCommand("panel", "Open control panel (admin)"),
            BotCommand("manage", "Edit website pages and bot settings (admin)"),
            BotCommand("username", "Set website panel username (admin)"),
            BotCommand("password", "Set website panel password (admin)"),
            BotCommand("backendurl", "Set backend / Cloudflare URL (admin)"),
            BotCommand("baseurl", "Set primary / Cloudflare URL (admin)"),
            BotCommand("categories", "Manage categories (admin)"),
            BotCommand("edit", "Open page editor (admin)"),
            BotCommand("search", "Search catalog (admin)"),
            BotCommand("stats", "Bot statistics (admin)"),
            BotCommand("broadcast", "Broadcast message (admin)"),
            BotCommand("ban", "Ban a user (admin)"),
            BotCommand("unban", "Unban a user (admin)"),
            BotCommand("autodel", "Auto-delete settings (admin)"),
            BotCommand("ss", "Generate screenshots"),
            BotCommand("sv", "Generate sample video"),
            BotCommand("mi", "MediaInfo for a file"),
        ]
        await bot.set_bot_commands(commands)
        logger.info("Bot commands registered successfully (%d cmds)", len(commands))
    except Exception:
        logger.exception("Failed to register bot commands")


def register_handlers(bot):
    """Register wzgram (Pyrogram-compatible) handlers on the bot client."""

    @bot.on_message(filters.command("start") & filters.private)
    async def start_handler(client, message: Message):
        user = message.from_user
        if not user or user.is_bot:
            return
        user_id = user.id
        access_cfg = await db.site_settings.find_one({"_id": "runtime"}) or {}
        access_mode = access_cfg.get("access_mode", "all")
        if access_mode == "owners" and user_id != settings.OWNER_ID:
            return await message.reply("🔒 This bot is currently restricted to its owner.")
        if access_mode == "admins" and user_id not in settings.admin_list and user_id != settings.OWNER_ID:
            return await message.reply("🔒 This bot is currently restricted to administrators.")

        user_data = await users_col.find_one({"user_id": user_id})
        if user_data and user_data.get("is_banned"):
            return await message.reply(
                "🚫 **Access Suspended**\n\n"
                "> Your account has been banned from using this service.\n"
                "> Contact support if you believe this is a mistake."
            )

        if not user_data:
            new_user = User(
                user_id=user_id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
            )
            await users_col.insert_one(new_user.dict())
            if settings.CHANNEL_ID:
                try:
                    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
                    await client.send_message(
                        settings.CHANNEL_ID,
                        f"👤 **#NewUser Alert**\n\n"
                        f"> 🆔 **ID:** `{user_id}`\n"
                        f"> 📛 **Name:** {name}\n"
                        f"> 🌐 **Username:** @{user.username or 'N/A'}",
                    )
                except Exception as e:
                    logger.error("Error sending new user log: %s", e)

        if not await is_user_fsubbed(client, user_id):
            return await message.reply(
                "🔒 **Access Restricted**\n\n"
                "> Join our official updates channel, then send /start again.",
                reply_markup=_kb([[InlineKeyboardButton("📢 Join Updates Channel", url=settings.UPDATE_CHANNEL_URL)]]),
            )

        first_name = user.first_name or "User"
        mention = f"[{first_name}](tg://user?id={user_id})"
        greeting = get_ist_greeting()
        start_text = (
            f"👑 **{greeting}, {mention}!**\n\n"
            f"> ⚡ **Welcome to Premium Direct Link & Streaming Service**\n"
            f">\n"
            f"> 🚀 *Upload or forward any Telegram file to generate ultra-fast direct download & streaming links instantly.*\n"
            f">\n"
            f"> 📸 `/ss` reply to media → screenshots\n"
            f"> 🎬 `/sv` reply to media → sample video\n"
            f"> 📊 `/mi` reply to media → MediaInfo"
        )
        await message.reply(
            start_text,
            reply_markup=_kb([[InlineKeyboardButton("📢 Join Updates Channel", url=settings.UPDATE_CHANNEL_URL)]]),
        )

    @bot.on_message((filters.document | filters.video | filters.audio | filters.animation | filters.photo | filters.voice | filters.video_note) & ~filters.channel)
    async def media_handler(client, message: Message):
        user = message.from_user
        if not user or user.is_bot:
            return

        access_cfg = await db.site_settings.find_one({"_id": "runtime"}) or {}
        access_mode = access_cfg.get("access_mode", "all")
        if access_mode == "owners" and user.id != settings.OWNER_ID:
            return await message.reply("🔒 This bot is currently restricted to its owner.")
        if access_mode == "admins" and user.id not in settings.admin_list and user.id != settings.OWNER_ID:
            return await message.reply("🔒 This bot is currently restricted to administrators.")
        runtime_cfg = await db.site_settings.find_one({"_id": "runtime"}) or {}
        downloads_enabled = runtime_cfg.get("downloads_enabled", True)
        streaming_enabled = runtime_cfg.get("streaming_enabled", True)
        if not downloads_enabled and not streaming_enabled:
            return await message.reply("🚫 **Service Disabled**\n\n> Downloads and streaming are currently disabled by the administrator.")
        user_data = await users_col.find_one({"user_id": user.id})
        if user_data and user_data.get("is_banned"):
            return await message.reply(
                "🚫 **Access Suspended**\n\n"
                "> Your account has been banned from using this service."
            )

        if not await check_rate_limit(user.id):
            return await message.reply(
                "⏳ **Rate Limit Exceeded**\n\n"
                "> Please wait a few seconds before uploading more files."
            )

        if not await is_user_fsubbed(client, user.id):
            return await message.reply(
                "🔒 **Access Restricted**\n\n"
                "> Join our official updates channel, then upload your file again.",
                reply_markup=_kb([[InlineKeyboardButton("📢 Join Updates Channel", url=settings.UPDATE_CHANNEL_URL)]]),
            )

        detecting_msg = await message.reply("⚡ **Analyzing media & preparing instant links...**")

        file_name = "file"
        file_size = 0
        mime_type = "application/octet-stream"
        file_id = ""

        if message.document:
            doc = message.document
            file_name = doc.file_name or "file"
            file_size = doc.file_size or 0
            mime_type = doc.mime_type or mimetypes.guess_type(file_name)[0] or "application/octet-stream"
            file_id = doc.file_id
        elif message.video:
            vid = message.video
            file_name = getattr(vid, "file_name", None) or f"video_{vid.file_unique_id}.mp4"
            file_size = vid.file_size or 0
            mime_type = vid.mime_type or "video/mp4"
            file_id = vid.file_id
        elif message.audio:
            aud = message.audio
            file_name = aud.file_name or f"audio_{aud.file_unique_id}.mp3"
            file_size = aud.file_size or 0
            mime_type = aud.mime_type or "audio/mpeg"
            file_id = aud.file_id
        elif message.animation:
            ani = message.animation
            file_name = getattr(ani, "file_name", None) or f"animation_{ani.file_unique_id}.mp4"
            file_size = ani.file_size or 0
            mime_type = ani.mime_type or "video/mp4"
            file_id = ani.file_id
        elif message.photo:
            photo = message.photo[-1] if isinstance(message.photo, (list, tuple)) else message.photo
            file_name = f"photo_{getattr(photo, 'file_unique_id', 'img')}.jpg"
            file_size = getattr(photo, "file_size", 0) or 0
            mime_type = "image/jpeg"
            file_id = getattr(photo, "file_id", "") or ""
        elif message.voice:
            voice = message.voice
            file_name = f"voice_{voice.file_unique_id}.ogg"
            file_size = voice.file_size or 0
            mime_type = voice.mime_type or "audio/ogg"
            file_id = voice.file_id
        elif message.video_note:
            vn = message.video_note
            file_name = f"videonote_{vn.file_unique_id}.mp4"
            file_size = vn.file_size or 0
            mime_type = "video/mp4"
            file_id = vn.file_id
        else:
            return await detecting_msg.edit_text(
                "❌ **Error:** Unsupported media type.\n\n"
                "Supported: documents, videos, audio, GIFs/animations, photos, voice notes, video notes."
            )

        if not file_id:
            return await detecting_msg.edit_text("❌ **Error:** Could not extract media details.")

        short_code = generate_short_code()
        file_meta = FileMetadata(
            file_id=file_id,
            file_unique_id=str(message.id),
            filename=file_name,
            mime_type=mime_type,
            file_size=file_size,
            uploader_id=user.id,
            short_code=short_code,
            chat_id=message.chat.id,
            message_id=message.id,
            expiry_time=None,
        )
        await files_col.insert_one(file_meta.dict())

        # Check live cloudflared log first, then MongoDB, so auto-set Cloudflare Try URL is always used
        log_cf = _extract_tunnel_url_from_logs()
        if log_cf:
            await apply_auto_cloudflare_url(log_cf, source="media-upload-check")

        media_base = await get_active_cloudflare_url()
        if media_base:
            base_url = media_base
            links_ready = True
        else:
            try:
                base_url = normalize_public_base_url(settings.BASE_URL)
                links_ready = True
            except ValueError as exc:
                logger.warning("Public BASE_URL not configured: %s", exc)
                base_url = None
                links_ready = False

        formatted_size = format_bytes(file_size) if file_size else "N/A"
        safe_name = html.escape(file_name)
        safe_mime = html.escape(mime_type)

        if links_ready:
            download_url = f"{base_url}/dl/{short_code}" if downloads_enabled else None
            stream_url = f"{base_url}/watch/{short_code}" if streaming_enabled else None
            active_links = []
            button_rows = []
            if downloads_enabled:
                active_links.append(f"⚡ <a href=\"{html.escape(download_url, quote=True)}\">Direct Download</a>")
                button_rows.append(InlineKeyboardButton("⚡ Direct Download", url=download_url))
            if streaming_enabled:
                active_links.append(f"🍿 <a href=\"{html.escape(stream_url, quote=True)}\">Watch Online</a>")
                button_rows.append(InlineKeyboardButton("🍿 Watch Online", url=stream_url))
            caption = (
                "💎 <b>Media Links Generated</b>\n\n"
                f"📂 <b>File Name:</b> <code>{safe_name}</code>\n"
                f"📦 <b>File Size:</b> <code>{html.escape(formatted_size)}</code>\n"
                f"⚙️ <b>MIME Type:</b> <code>{safe_mime}</code>\n\n"
                "🚀 <b>Available Links</b>\n" + "\n".join(active_links) + "\n\n"
                f"📢 <a href=\"{html.escape(settings.UPDATE_CHANNEL_URL, quote=True)}\">Updates Channel</a>"
            )
            user_buttons = _kb([button_rows, [InlineKeyboardButton("📢 Updates Channel", url=settings.UPDATE_CHANNEL_URL)], [InlineKeyboardButton("🗑️ Delete Link", callback_data=f"del_{short_code}")]])
            channel_buttons = _kb([button_rows, [InlineKeyboardButton("📢 Updates Channel", url=settings.UPDATE_CHANNEL_URL)]])
        else:
            caption = (
                "💎 <b>File Received Successfully</b>\n\n"
                f"📂 <b>File Name:</b> <code>{safe_name}</code>\n"
                f"📦 <b>File Size:</b> <code>{html.escape(formatted_size)}</code>\n"
                f"⚙️ <b>MIME Type:</b> <code>{safe_mime}</code>\n\n"
                "⚠️ <b>Public links are initializing...</b>\n"
                "The Cloudflare Quick Tunnel URL is being auto-detected. Try again in a few seconds or use `/baseurl https://your-tunnel.trycloudflare.com`.\n\n"
                f"📢 <a href=\"{html.escape(settings.UPDATE_CHANNEL_URL, quote=True)}\">Updates Channel</a>"
            )
            user_buttons = _kb([[InlineKeyboardButton("📢 Updates Channel", url=settings.UPDATE_CHANNEL_URL)], [InlineKeyboardButton("🗑️ Delete Link", callback_data=f"del_{short_code}")]])
            channel_buttons = _kb([[InlineKeyboardButton("📢 Updates Channel", url=settings.UPDATE_CHANNEL_URL)]])

        try:
            await detecting_msg.delete()
        except Exception:
            pass

        try:
            await message.copy(
                chat_id=message.chat.id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=user_buttons,
            )
        except Exception as e:
            logger.exception("Error sending file with buttons: %s", e)
            try:
                await message.reply(caption, parse_mode=ParseMode.HTML, reply_markup=user_buttons)
            except Exception as e2:
                logger.exception("Fallback reply failed: %s", e2)

        if settings.CHANNEL_ID and settings.CHANNEL_ID != message.chat.id:
            try:
                await message.copy(
                    chat_id=settings.CHANNEL_ID,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=channel_buttons,
                )
            except Exception as e:
                logger.exception("Error mirroring to log channel: %s", e)

    @bot.on_callback_query(filters.regex(r"^del_"))
    async def delete_callback(client, callback: CallbackQuery):
        user = callback.from_user
        if not user or user.is_bot:
            return
        if not await is_user_fsubbed(client, user.id):
            return await callback.answer("🔒 Please join the update channel first!", show_alert=True)

        short_code = callback.data.split("_", 1)[1]
        file_data = await files_col.find_one({"short_code": short_code})
        if file_data and file_data["uploader_id"] == user.id:
            await files_col.delete_one({"short_code": short_code})
            await callback.message.edit_text("🗑️ **Link deleted successfully!**")
        else:
            await callback.answer("🚫 You are not authorized to delete this link.", show_alert=True)

    @bot.on_message(filters.command("search"))
    async def cms_search_handler(client, message: Message):
        user = message.from_user
        if not user or user.is_bot or (user.id not in settings.admin_list and user.id != settings.OWNER_ID):
            return await message.reply("🚫 Admins only.")

        raw = (message.text or "").strip()
        parts = raw.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            return await message.reply(
                "🔎 **TMDB Catalog Search**\n\n"
                "> `/search movie name` — search movies and series\n"
                "> `/search movie movie name` — movies only\n"
                "> `/search tv series name` — TV series only"
            )

        query_text = parts[1].strip()
        media_filter = "multi"
        typed = re.match(r"^(movie|movies|tv|series)\s*:\s*(.+)$", query_text, re.I)
        if typed:
            media_filter = "movie" if typed.group(1).lower() in ("movie", "movies") else "tv"
            query_text = typed.group(2).strip()
        else:
            typed = re.match(r"^(movie|movies|tv|series)\s+(.+)$", query_text, re.I)
            if typed:
                media_filter = "movie" if typed.group(1).lower() in ("movie", "movies") else "tv"
                query_text = typed.group(2).strip()

        if not query_text:
            return await message.reply("❌ Please provide a movie or series name.")
        api_key = (getattr(settings, "TMDB_API_KEY", "") or os.getenv("TMDB_API_KEY", "")).strip()
        if not api_key:
            return await message.reply(
                "⚠️ **TMDB API key is not configured.**\n"
                "Add `TMDB_API_KEY` to the bot backend environment and restart the bot."
            )

        status = await message.reply(f"🔍 Searching TMDB for **{html.escape(query_text[:100])}**…")
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=8.0),
                headers={"Accept": "application/json"},
                follow_redirects=True,
            ) as http:
                response = await http.get(
                    f"https://api.themoviedb.org/3/search/{media_filter}",
                    params={"api_key": api_key, "query": query_text, "include_adult": "false", "page": 1},
                )
                response.raise_for_status()
                payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise ValueError("TMDB returned an unexpected response")

            rows = []
            saved = 0
            for item in payload["results"]:
                if not isinstance(item, dict):
                    continue
                media_type = item.get("media_type") if media_filter == "multi" else media_filter
                if media_type not in ("movie", "tv") or not item.get("id"):
                    continue
                title = (item.get("title") or item.get("name") or "").strip()
                if not title:
                    continue
                pending_id = secrets.token_hex(5)
                release_date = item.get("release_date") or item.get("first_air_date") or ""
                year = release_date[:4] if isinstance(release_date, str) and len(release_date) >= 4 else ""
                await db.cms_pending.insert_one({
                    "_id": pending_id,
                    "tmdb_id": int(item["id"]),
                    "media_type": media_type,
                    "title": title,
                    "overview": item.get("overview") or "",
                    "poster_path": item.get("poster_path") or "",
                    "release_date": release_date,
                    "rating": item.get("vote_average"),
                    "created_at": datetime.datetime.utcnow(),
                })
                label = f"{title} ({year})" if year else title
                kind = "Movie" if media_type == "movie" else "TV"
                rows.append([InlineKeyboardButton(f"{label[:42]} · {kind}", callback_data=f"cmscreate:{pending_id}")])
                saved += 1
                if saved >= 10:
                    break

            if not rows:
                return await status.edit_text(
                    f"🔎 No movie or TV results found for **{html.escape(query_text[:100])} and try again**.\n"
                    "Try a different spelling or use `/search movie: name` / `/search tv: name`."
                )
            await status.edit_text(
                f"🎬 **TMDB Search Results**\n"
                f"> Query: `{html.escape(query_text[:100])}`\n"
                f"> Matches: `{len(rows)}`\n\n"
                "Select a result to publish its catalog page:",
                reply_markup=_kb(rows),
            )
        except httpx.HTTPStatusError as exc:
            logger.warning("TMDB search returned HTTP %s", exc.response.status_code)
            detail = ""
            try:
                detail = (exc.response.json() or {}).get("status_message", "")
            except Exception:
                pass
            await status.edit_text(
                f"❌ TMDB search failed (HTTP {exc.response.status_code}). "
                f"{html.escape(str(detail)[:250]) or 'Check the API key and TMDB availability.'}"
            )
        except (httpx.TimeoutException, httpx.RequestError):
            logger.exception("TMDB search network failure")
            await status.edit_text("⏳ TMDB did not respond. Please try `/search` again in a moment.")
        except Exception as exc:
            logger.exception("TMDB search failed")
            await status.edit_text(f"❌ TMDB search failed: `{type(exc).__name__}`")

    @bot.on_callback_query(filters.regex(r"^cmscreate:"))
    async def cms_create_callback(client, callback: CallbackQuery):
        user = callback.from_user
        if not user or (user.id not in settings.admin_list and user.id != settings.OWNER_ID):
            return await callback.answer("Admins only", show_alert=True)
        data = getattr(callback, "data", "") or ""
        pending_id = data.split(":", 1)[1] if ":" in data else ""
        pending = await db.cms_pending.find_one({"_id": pending_id}) if pending_id else None
        if not pending or pending.get("media_type") not in ("movie", "tv") or not pending.get("tmdb_id"):
            return await callback.answer("Search result expired. Run /search again.", show_alert=True)
        slug = re.sub(r"[^a-z0-9]+", "-", pending["title"].lower()).strip("-") or secrets.token_hex(5)
        base, n = slug, 2
        while await db.catalog_pages.find_one({"slug": slug}):
            slug = f"{base}-{n}"
            n += 1
        category = "web-series" if pending["media_type"] == "tv" else "movies"
        extra = {"genres": [], "imdb_id": "", "runtime": None, "tagline": ""}
        if settings.TMDB_API_KEY:
            try:
                endpoint = "tv" if pending["media_type"] == "tv" else "movie"
                async with httpx.AsyncClient(timeout=15) as http:
                    detail = await http.get(
                        f"https://api.themoviedb.org/3/{endpoint}/{pending['tmdb_id']}",
                        params={"api_key": settings.TMDB_API_KEY, "append_to_response": "external_ids"},
                    )
                    detail.raise_for_status()
                    metadata = detail.json()
                extra = {
                    "genres": [g.get("name", "") for g in metadata.get("genres", [])],
                    "imdb_id": (metadata.get("external_ids") or {}).get("imdb_id") or metadata.get("imdb_id", ""),
                    "runtime": metadata.get("runtime") or (metadata.get("episode_run_time") or [None])[0],
                    "tagline": metadata.get("tagline", ""),
                }
            except Exception:
                logger.exception("TMDB details lookup failed; publishing search metadata only")
        await db.catalog_pages.insert_one({
            "slug": slug,
            "title": pending["title"],
            "overview": pending.get("overview", ""),
            "poster_path": pending.get("poster_path", ""),
            "release_date": pending.get("release_date", ""),
            "rating": pending.get("rating"),
            "media_type": pending["media_type"],
            "category_slugs": [category],
            "groups": [],
            "published": True,
            "created_at": datetime.datetime.utcnow(),
            "updated_at": datetime.datetime.utcnow(),
            **extra,
        })
        await db.cms_pending.delete_one({"_id": pending_id})
        web_base = (await get_active_website_url()) or settings.BASE_URL.rstrip("/")
        await callback.message.edit_text(
            f"✅ Page created: {web_base.rstrip('/')}/title/{slug}\n"
            "Use /edit while replying with this link, or open /panel to manage page groups and buttons."
        )
        await callback.answer("Published")

    @bot.on_callback_query(filters.regex(r"^cmscat:"))
    async def cms_category_callback(client, callback: CallbackQuery):
        user = callback.from_user
        if not user or (user.id not in settings.admin_list and user.id != settings.OWNER_ID):
            return await callback.answer("Admins only", show_alert=True)
        pending_id = callback.data.split(":", 1)[1]
        pending = await db.cms_pending.find_one({"_id": pending_id, "action": "category"})
        if not pending:
            return await callback.answer("Selection expired", show_alert=True)
        await db.catalog_pages.update_one(
            {"slug": pending["page_slug"]},
            {"$addToSet": {"category_slugs": pending["category_slug"]}, "$set": {"updated_at": datetime.datetime.utcnow()}},
        )
        await db.cms_pending.delete_one({"_id": pending_id})
        await callback.message.edit_text("✅ Page category updated. The website catalog is updated automatically.")
        await callback.answer("Category assigned")

    @bot.on_message(filters.command(["username", "password"]))
    async def panel_credentials_handler(client, message: Message):
        user = message.from_user
        if not user or (user.id not in settings.admin_list and user.id != settings.OWNER_ID):
            return await message.reply("🚫 Admins only.")
        parts = (message.text or "").split(maxsplit=1)
        command = parts[0].split("@")[0].lower() if parts else ""
        if len(parts) < 2 or not parts[1].strip():
            usage = "/username your_new_username" if command == "/username" else "/password your_new_password"
            return await message.reply(f"Usage: `{usage}`")
        value = parts[1].strip()
        try:
            if command == "/username":
                if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", value):
                    return await message.reply("Username must be 3–64 characters and use only letters, numbers, dot, underscore or hyphen.")
                await settings_col.update_one({"key": "panel_username"}, {"$set": {"value": value, "updated_by": user.id, "updated_at": datetime.datetime.utcnow()}}, upsert=True)
                reply = f"✅ Panel username updated to `{value}`."
            else:
                if len(value) < 10 or len(value) > 256:
                    return await message.reply("Password must be 10–256 characters. Choose a strong, unique password.")
                salt, digest = make_password_hash(value)
                now = datetime.datetime.utcnow()
                await settings_col.update_one({"key": "panel_password_salt"}, {"$set": {"value": salt, "updated_by": user.id, "updated_at": now}}, upsert=True)
                await settings_col.update_one({"key": "panel_password_hash"}, {"$set": {"value": digest, "updated_by": user.id, "updated_at": now}}, upsert=True)
                reply = "✅ Panel password updated. The password is stored as a salted hash and will not be shown again."
        finally:
            try:
                await message.delete()
            except Exception:
                pass
        return await message.reply(reply + "\n\nOpen `/panel` and sign in with your configured credentials.")

    @bot.on_message(filters.command(["categories", "edit", "panel", "manage"]))
    async def cms_admin_commands(client, message: Message):
        user = message.from_user
        if not user or (user.id not in settings.admin_list and user.id != settings.OWNER_ID):
            return await message.reply("🚫 Admins only.")
        web_base = (await get_active_website_url()) or settings.BASE_URL
        base = str(web_base).rstrip("/")
        cmd = (message.text or "").split(maxsplit=1)[0].split("@")[0].lower()
        if cmd == "/categories":
            reply = getattr(message, "reply_to_message", None)
            if reply:
                source_text = (getattr(reply, "text", None) or getattr(reply, "caption", None) or "")
                match = re.search(r"/title/([a-z0-9-]+)", source_text, re.I)
                if not match:
                    return await message.reply("Reply to a published /title/ page link to assign its category.")
                page_slug = match.group(1)
                if not await db.catalog_pages.find_one({"slug": page_slug}):
                    return await message.reply("That catalog page was not found.")
                available = await db.catalog_categories.find({}).to_list(None)
                rows = []
                for cat in available:
                    pending_id = secrets.token_hex(5)
                    await db.cms_pending.insert_one({"_id": pending_id, "action": "category", "page_slug": page_slug, "category_slug": cat["slug"]})
                    rows.append([InlineKeyboardButton(cat["name"], callback_data=f"cmscat:{pending_id}")])
                return await message.reply("Choose a category for this page:", reply_markup=_kb(rows))
            cats = await db.catalog_categories.find({}, {"_id": 0}).to_list(None)
            names = "\n".join(f"• {c['name']}" for c in cats) or "No categories created yet."
            return await message.reply(
                f"🗂️ **Categories**\n\n{names}\n\nCreate or manage unlimited categories in the dashboard.",
                reply_markup=_kb([[InlineKeyboardButton("➕ Create / Manage Categories", url=f"{base}/panel")]]),
            )
        if cmd == "/edit":
            return await message.reply(
                "✏️ Page editor\n\nEdit page metadata, poster, categories, unlimited groups and buttons in the dashboard.",
                reply_markup=_kb([[InlineKeyboardButton("✏️ Open Page Editor", url=f"{base}/panel")]]),
            )
        if cmd == "/manage":
            target = (message.text or "").split(maxsplit=1)[1].strip() if len((message.text or "").split(maxsplit=1)) > 1 else ""
            match = re.search(r"/title/([a-z0-9-]+)", target, re.I)
            if match:
                page = await db.catalog_pages.find_one({"slug": match.group(1).lower()}, {"_id": 0, "title": 1})
                if not page:
                    return await message.reply("❌ Page link not found in the shared catalog.")
                panel_url = f"{base}/panel?page={match.group(1).lower()}"
                return await message.reply(
                    f"✏️ **Editing: {page.get('title', match.group(1))}**",
                    reply_markup=_kb([[InlineKeyboardButton("Open selected page editor", url=panel_url)]]),
                )
            if target:
                safe = re.escape(target[:100])
                page = await db.catalog_pages.find_one({"title": {"$regex": safe, "$options": "i"}}, {"_id": 0, "slug": 1, "title": 1})
                if page:
                    return await message.reply(
                        f"✏️ **Editing: {page['title']}**",
                        reply_markup=_kb([[InlineKeyboardButton("Open selected page editor", url=f"{base}/panel?page={page['slug']}")]]),
                    )
            return await message.reply(
                "🛠️ **Manage website & bot**\nUse `/manage page name` or `/manage https://your-site/title/page-slug` to open a selected page. Open the dashboard for all settings and pages.",
                reply_markup=_kb([[InlineKeyboardButton("🛠️ Open Manage Panel", url=f"{base}/panel")]]),
            )
        current_cf = (await get_active_cloudflare_url()) or "Auto-detecting..."
        vercel_url = (await settings_col.find_one({"key": "vercel_url"}) or {}).get("value") or "Detected automatically on first Vercel visit"
        website_doc = await settings_col.find_one({"key": "website_urls"}) or {}
        website_urls = website_doc.get("value", [])
        if not isinstance(website_urls, list):
            website_urls = [str(website_urls)] if website_urls else []
        website_urls = [u for u in website_urls if not is_trycloudflare_url(str(u))]
        website_lines = "\n".join(f"• `{url}`" for url in website_urls) or "Waiting for first Vercel visit"
        return await message.reply(
            "🔐 **HITLER YT · Control Panel**\n\n"
            f"> ☁️ **Auto Cloudflare Stream/Download/Button URL:** `{current_cf}`\n"
            f"> ▲ **Vercel Website URL (Website Only):** `{vercel_url}`\n\n"
            f"**Registered Vercel website domains**\n{website_lines}\n\n"
            "**Panel login management**\n"
            "• `/username new_username` — change the website panel username\n"
            "• `/password new_password` — change the website panel password (10+ characters)\n\n"
            "The Cloudflare `trycloudflare.com` URL is auto-detected and automatically replaces all button URL domains and generates all download & stream links, while Vercel serves the website.",
            reply_markup=_kb([[InlineKeyboardButton("Open HITLER YT Website Panel", url=f"{base}/panel")]]),
        )

    @bot.on_message(filters.command("backendurl"))
    async def backendurl_handler(client, message: Message):
        user = message.from_user
        if not user or (user.id not in settings.admin_list and user.id != settings.OWNER_ID):
            return await message.reply("🚫 **Unauthorized Access**")
        args = (message.text or "").split(maxsplit=1)
        current = (await get_active_cloudflare_url()) or settings.BACKEND_URL or "Auto-detecting"
        if len(args) < 2:
            return await message.reply(
                "☁️ **Cloudflare Tunnel URL**\n\n"
                f"> Current Auto Cloudflare URL: `{current}`\n\n"
                "Usage: `/backendurl https://your-tunnel.trycloudflare.com`\n"
                "This URL is auto-detected by the VPS and automatically used for all download links, stream links, and button URLs."
            )
        candidate = args[1].strip()
        try:
            candidate = normalize_public_base_url(candidate)
            now = datetime.datetime.utcnow()
            await settings_col.update_one(
                {"key": "backend_url"},
                {"$set": {"value": candidate, "updated_by": user.id, "updated_at": now}},
                upsert=True,
            )
            await settings_col.update_one(
                {"key": "cloudflare_url"},
                {"$set": {"value": candidate, "updated_by": user.id, "updated_at": now}},
                upsert=True,
            )
            if is_trycloudflare_url(candidate) or settings.BASE_URL.startswith(("http://localhost", "http://127.0.0.1")) or is_trycloudflare_url(settings.BASE_URL):
                await settings_col.update_one(
                    {"key": "base_url"},
                    {"$set": {"value": candidate, "updated_by": user.id, "updated_at": now}},
                    upsert=True,
                )
                settings.BASE_URL = candidate
                os.environ["BASE_URL"] = candidate
            settings.BACKEND_URL = candidate
            rewritten = await rewrite_all_stored_catalog_buttons(candidate)
            await message.reply(
                "✅ **Cloudflare URL Saved & Synced!**\n\n"
                f"> ☁️ **Cloudflare URL:** `{candidate}`\n"
                f"> 🔄 **Catalog Pages Rewritten:** `{rewritten}`\n\n"
                "All download links (`/dl/`), stream links (`/watch/`, `/stream/`), and website button URLs now use this Cloudflare URL."
            )
        except ValueError as exc:
            await message.reply(f"❌ Invalid backend URL: `{html.escape(str(exc))}`")
        except Exception:
            logger.exception("Failed to save backend URL")
            await message.reply("❌ Could not save backend URL. Check the MongoDB connection.")

    @bot.on_message(filters.command("baseurl"))
    async def baseurl_handler(client, message: Message):
        user = message.from_user
        if not user or user.is_bot:
            return
        if user.id not in settings.admin_list and user.id != settings.OWNER_ID:
            return await message.reply(
                "🚫 **Unauthorized Access**\n\n"
                "> Only authorized bot administrators can update the Base URL."
            )
        args = (message.text or "").split(maxsplit=1)
        if len(args) < 2:
            current_cf = (await get_active_cloudflare_url()) or settings.BASE_URL
            return await message.reply(
                "🌐 **Cloudflare / Base URL Management**\n\n"
                f"> 🔗 **Current Active Cloudflare URL:** `{current_cf}`\n"
                f"> 📢 **Updates Channel:** `{settings.UPDATE_CHANNEL_URL}`\n\n"
                "*(Cloudflare Try URL is set automatically on startup, or override with `/baseurl https://name.trycloudflare.com`)*"
            )
        new_url = args[1].strip()
        try:
            new_url = normalize_public_base_url(new_url)
        except ValueError as exc:
            return await message.reply(
                "❌ **Invalid public Base URL**\n\n"
                f"> {html.escape(str(exc))}\n\n"
                "Use your public URL, for example: `/baseurl https://name.trycloudflare.com`"
            )
        try:
            now = datetime.datetime.utcnow()
            await settings_col.update_one(
                {"key": "base_url"},
                {"$set": {"value": new_url, "updated_by": user.id, "updated_at": now}},
                upsert=True,
            )
            settings.BASE_URL = new_url
            os.environ["BASE_URL"] = new_url
            rewritten = 0
            if is_trycloudflare_url(new_url) or not settings.BACKEND_URL or is_trycloudflare_url(settings.BACKEND_URL):
                settings.BACKEND_URL = new_url
                await settings_col.update_one(
                    {"key": "backend_url"},
                    {"$set": {"value": new_url, "updated_by": user.id, "updated_at": now}},
                    upsert=True,
                )
                await settings_col.update_one(
                    {"key": "cloudflare_url"},
                    {"$set": {"value": new_url, "updated_by": user.id, "updated_at": now}},
                    upsert=True,
                )
                rewritten = await rewrite_all_stored_catalog_buttons(new_url)
            else:
                await settings_col.update_one(
                    {"key": "website_urls"},
                    {"$addToSet": {"value": new_url}, "$set": {"updated_at": now}},
                    upsert=True,
                )
            await message.reply(
                "🌐 **Base URL Updated Successfully!**\n\n"
                f"> 🔗 **New URL:** `{new_url}`\n"
                f"> 🔄 **Catalog Pages Rewritten:** `{rewritten}`\n"
                f"> 👤 **Updated By:** `{user.id}`\n\n"
                "✨ *All download links, stream links, and website button URLs now use this Cloudflare URL.*"
            )
        except Exception as e:
            logger.error("Error updating baseurl: %s", e)
            await message.reply(f"❌ **Error updating Base URL:** `{e}`")

    @bot.on_message(filters.command("stats"))
    async def stats_handler(client, message: Message):
        user = message.from_user
        if not user or user.is_bot:
            return
        if user.id not in settings.admin_list and user.id != settings.OWNER_ID:
            return
        total_files = await files_col.count_documents({})
        total_users = await users_col.count_documents({})
        await message.reply(
            f"📊 **System Statistics**\n\n"
            f"> 👥 **Total Users:** `{total_users}`\n"
            f"> 📁 **Total Files Generated:** `{total_files}`\n"
            f"> ⚡ **System Status:** `Online & High-Speed`\n"
            f"> 🔧 **Engine:** `wzgram[fast]`"
        )

    @bot.on_message(filters.command("broadcast"))
    async def broadcast_handler(client, message: Message):
        user = message.from_user
        if not user or user.is_bot:
            return
        if user.id not in settings.admin_list and user.id != settings.OWNER_ID:
            return
        if not message.reply_to_message:
            return await message.reply("⚠️ **Usage:** Please reply to a message to broadcast it.")
        users = await users_col.find().to_list(None)
        status = await message.reply(
            f"📢 **Broadcast Initiated...**\n\n> 🎯 **Target:** `{len(users)}` users"
        )
        done = failed = 0
        for u in users:
            try:
                await message.reply_to_message.copy(u["user_id"])
                done += 1
            except Exception:
                failed += 1
            if done % 20 == 0:
                await status.edit_text(
                    f"🔄 **Broadcast In Progress...**\n\n"
                    f"> ✅ **Sent:** `{done}`\n> ❌ **Failed:** `{failed}`"
                )
        await status.edit_text(
            f"🎉 **Broadcast Completed!**\n\n"
            f"> 👥 **Total Target:** `{len(users)}` users\n"
            f"> ✅ **Success:** `{done}`\n"
            f"> ❌ **Failed:** `{failed}`"
        )

    @bot.on_message(filters.command("ban"))
    async def ban_handler(client, message: Message):
        user = message.from_user
        if not user or user.is_bot:
            return
        if user.id not in settings.admin_list and user.id != settings.OWNER_ID:
            return
        try:
            target = int((message.text or "").split()[1])
            await users_col.update_one({"user_id": target}, {"$set": {"is_banned": True}})
            await message.reply(
                f"🛡️ **User Banned**\n\n> User ID `{target}` has been banned from using the bot."
            )
        except Exception:
            await message.reply("⚠️ **Usage:** `/ban USER_ID`")

    @bot.on_message(filters.command("unban"))
    async def unban_handler(client, message: Message):
        user = message.from_user
        if not user or user.is_bot:
            return
        if user.id not in settings.admin_list and user.id != settings.OWNER_ID:
            return
        try:
            target = int((message.text or "").split()[1])
            await users_col.update_one({"user_id": target}, {"$set": {"is_banned": False}})
            await message.reply(
                f"🛡️ **User Unbanned**\n\n> User ID `{target}` has been restored and unbanned."
            )
        except Exception:
            await message.reply("⚠️ **Usage:** `/unban USER_ID`")

    @bot.on_message(filters.command("autodel"))
    async def autodel_handler(client, message: Message):
        user = message.from_user
        if not user or user.is_bot:
            return
        if user.id not in settings.admin_list and user.id != settings.OWNER_ID:
            return
        try:
            args = (message.text or "").split()
            if len(args) < 2:
                return await message.reply("⚠️ **Usage:** `/autodel 24h` or `/autodel off`")
            val = args[1].lower()
            if val == "off":
                await message.reply(
                    "⏳ **Auto-Delete Configured**\n\n> Auto-delete feature has been disabled."
                )
            else:
                hours = int(val.replace("h", ""))
                await message.reply(
                    f"⏳ **Auto-Delete Configured**\n\n> Link expiration set to `{hours}` hours."
                )
        except Exception:
            await message.reply("⚠️ **Usage:** `/autodel 24h` or `/autodel off`")

    def _is_media_message(msg: Message) -> bool:
        if not msg:
            return False
        return bool(
            msg.document or msg.video or msg.animation or msg.audio
            or msg.voice or msg.video_note or msg.photo
        )

    @bot.on_message(filters.command("ss"))
    async def ss_command(client, message: Message):
        user = message.from_user
        if not user or user.is_bot:
            return
        if not await is_access_allowed(user.id):
            return await message.reply("🔒 This bot is restricted by the administrator.")
        if not await is_user_fsubbed(client, user.id):
            return await message.reply(
                "🔒 **Access Restricted**\n\n> Join the updates channel first.",
                reply_markup=_kb([[InlineKeyboardButton("📢 Join Updates Channel", url=settings.UPDATE_CHANNEL_URL)]]),
            )
        target = message.reply_to_message
        if not target or not _is_media_message(target):
            return await message.reply(
                "📸 **Screenshots**\n\n"
                "> Reply to a **video / document / animation** with `/ss`,\n"
                "> then pick how many screenshots to generate."
            )
        chat_id = target.chat.id
        msg_id = target.id
        rows = [
            [
                InlineKeyboardButton("1", callback_data=f"ss:{chat_id}:{msg_id}:1"),
                InlineKeyboardButton("3", callback_data=f"ss:{chat_id}:{msg_id}:3"),
                InlineKeyboardButton("5", callback_data=f"ss:{chat_id}:{msg_id}:5"),
            ],
            [
                InlineKeyboardButton("10", callback_data=f"ss:{chat_id}:{msg_id}:10"),
                InlineKeyboardButton("20", callback_data=f"ss:{chat_id}:{msg_id}:20"),
                InlineKeyboardButton("30", callback_data=f"ss:{chat_id}:{msg_id}:30"),
            ],
        ]
        await message.reply(
            "📸 **How many screenshots?**\n\n"
            "> Choose a count. Frames are taken evenly across the media.",
            reply_markup=_kb(rows),
        )

    @bot.on_message(filters.command("sv"))
    async def sv_command(client, message: Message):
        user = message.from_user
        if not user or user.is_bot:
            return
        if not await is_access_allowed(user.id):
            return await message.reply("🔒 This bot is restricted by the administrator.")
        if not await is_user_fsubbed(client, user.id):
            return await message.reply(
                "🔒 **Access Restricted**\n\n> Join the updates channel first.",
                reply_markup=_kb([[InlineKeyboardButton("📢 Join Updates Channel", url=settings.UPDATE_CHANNEL_URL)]]),
            )
        target = message.reply_to_message
        if not target or not _is_media_message(target):
            return await message.reply(
                "🎬 **Sample Video**\n\n"
                "> Reply to a **video / document / media** with `/sv`,\n"
                "> then pick the sample length in seconds."
            )
        chat_id = target.chat.id
        msg_id = target.id
        rows = [
            [
                InlineKeyboardButton("5s", callback_data=f"sv:{chat_id}:{msg_id}:5"),
                InlineKeyboardButton("10s", callback_data=f"sv:{chat_id}:{msg_id}:10"),
                InlineKeyboardButton("20s", callback_data=f"sv:{chat_id}:{msg_id}:20"),
            ],
            [
                InlineKeyboardButton("30s", callback_data=f"sv:{chat_id}:{msg_id}:30"),
                InlineKeyboardButton("60s", callback_data=f"sv:{chat_id}:{msg_id}:60"),
                InlineKeyboardButton("120s", callback_data=f"sv:{chat_id}:{msg_id}:120"),
            ],
        ]
        await message.reply(
            "🎬 **Sample length?**\n\n"
            "> Choose how many seconds from the start of the media.",
            reply_markup=_kb(rows),
        )

    @bot.on_callback_query(filters.regex(r"^ss:"))
    async def ss_callback(client, callback: CallbackQuery):
        user = callback.from_user
        if not user or user.is_bot:
            return
        if not await is_access_allowed(user.id):
            return await callback.answer("Restricted by administrator", show_alert=True)
        try:
            _, chat_s, msg_s, count_s = callback.data.split(":", 3)
            chat_id, msg_id, count = int(chat_s), int(msg_s), int(count_s)
        except Exception:
            return await callback.answer("Invalid request", show_alert=True)

        await callback.answer("Generating screenshots…")
        status = await callback.message.reply(f"📸 Generating **{count}** screenshot(s)…")
        work = None
        try:
            src_msg = await client.get_messages(chat_id, msg_id)
            if isinstance(src_msg, list):
                src_msg = src_msg[0] if src_msg else None
            if not src_msg or not _is_media_message(src_msg):
                return await status.edit_text("❌ Media not found or no longer available.")

            source = local_tg_stream_url(chat_id, msg_id)
            work = make_work_dir("ss_")
            await status.edit_text(f"🖼️ Random frames from stream (anywhere)…")
            shots = await generate_screenshots(source, count, os.path.join(work, "out"), region="anywhere")
            if not shots:
                await status.edit_text("⬇️ Stream seek failed — downloading once…")
                media_path = os.path.join(work, "input.bin")
                await download_message_media(client, src_msg, media_path)
                shots = await generate_screenshots(media_path, count, os.path.join(work, "out2"), region="anywhere")
            if not shots:
                return await status.edit_text("❌ Could not extract screenshots from this file.")

            from wzgram.types import InputMediaPhoto
            for i in range(0, len(shots), 10):
                batch = shots[i : i + 10]
                media = []
                for j, p in enumerate(batch):
                    cap = f"Screenshot {i + j + 1}/{len(shots)}" if j == 0 else None
                    media.append(InputMediaPhoto(p, caption=cap))
                await client.send_media_group(callback.message.chat.id, media)

            await status.edit_text(f"✅ Sent **{len(shots)}** screenshot(s).")
        except Exception as e:
            logger.exception("ss_callback failed")
            try:
                await status.edit_text(f"❌ Screenshot failed: `{e}`")
            except Exception:
                pass
        finally:
            cleanup_dir(work)

    @bot.on_callback_query(filters.regex(r"^sv:"))
    async def sv_callback(client, callback: CallbackQuery):
        user = callback.from_user
        if not user or user.is_bot:
            return
        if not await is_access_allowed(user.id):
            return await callback.answer("Restricted by administrator", show_alert=True)
        try:
            _, chat_s, msg_s, sec_s = callback.data.split(":", 3)
            chat_id, msg_id, seconds = int(chat_s), int(msg_s), int(sec_s)
        except Exception:
            return await callback.answer("Invalid request", show_alert=True)

        await callback.answer("Generating sample…")
        status = await callback.message.reply(f"🎬 Generating **{seconds}s** sample…")
        work = None
        try:
            src_msg = await client.get_messages(chat_id, msg_id)
            if isinstance(src_msg, list):
                src_msg = src_msg[0] if src_msg else None
            if not src_msg or not _is_media_message(src_msg):
                return await status.edit_text("❌ Media not found or no longer available.")

            source = local_tg_stream_url(chat_id, msg_id)
            work = make_work_dir("sv_")
            out_path = os.path.join(work, "sample.mp4")
            await status.edit_text(f"✂️ Random mid→end {seconds}s sample via stream…")
            try:
                await generate_sample_video(source, seconds, out_path, region="middle_last")
            except Exception:
                await status.edit_text("⬇️ Stream cut failed — downloading once…")
                media_path = os.path.join(work, "input.bin")
                await download_message_media(client, src_msg, media_path)
                await generate_sample_video(media_path, seconds, out_path, region="middle_last")

            await status.edit_text("📤 Uploading sample…")
            await client.send_video(
                callback.message.chat.id,
                out_path,
                caption=f"🎬 Sample · **{seconds}s**",
                supports_streaming=True,
            )
            await status.edit_text(f"✅ Sample video (**{seconds}s**) sent.")
        except Exception as e:
            logger.exception("sv_callback failed")
            try:
                await status.edit_text(f"❌ Sample failed: `{e}`")
            except Exception:
                pass
        finally:
            cleanup_dir(work)

    @bot.on_message(filters.command("mi"))
    async def mi_command(client, message: Message):
        user = message.from_user
        if not user or user.is_bot:
            return
        if not await is_access_allowed(user.id):
            return await message.reply("🔒 This bot is restricted by the administrator.")
        if not await is_user_fsubbed(client, user.id):
            return await message.reply(
                "🔒 **Access Restricted**\n\n> Join the updates channel first.",
                reply_markup=_kb([[InlineKeyboardButton("📢 Join Updates Channel", url=settings.UPDATE_CHANNEL_URL)]]),
            )
        target = message.reply_to_message
        args = (message.text or "").split(maxsplit=1)
        direct_url = args[1].strip() if len(args) > 1 else ""
        if direct_url and not direct_url.lower().startswith(("http://", "https://")):
            return await message.reply("❌ Send a valid downloadable HTTP/HTTPS URL.")
        if not direct_url and (not target or not _is_media_message(target)):
            return await message.reply(
                "📊 **MediaInfo**\n\n"
                "> Reply to Telegram media with `/mi`, or send `/mi https://example.com/file.mkv`.\n"
                "> Direct downloadable HTTP/HTTPS links are supported."
            )

        status = await message.reply(
            "🔍 **Extracting MediaInfo…**\n> " +
            ("Probing downloadable URL (no full download)." if direct_url else "Probing Telegram media via stream.")
        )
        work = None
        try:
            if direct_url:
                from urllib.parse import urlparse, unquote
                from app.utils.mediainfo_fmt import validate_download_url
                await validate_download_url(direct_url)
                source = direct_url
                raw_name = os.path.basename(urlparse(direct_url).path)
                name = unquote(raw_name) or "download"
                data = await extract_mediainfo(source)
            else:
                chat_id = target.chat.id
                msg_id = target.id
                source = local_tg_stream_url(chat_id, msg_id)
                try:
                    data = await extract_mediainfo(source)
                except Exception:
                    await status.edit_text("⬇️ Stream probe failed — downloading Telegram media once…")
                    work = make_work_dir("mi_")
                    media_path = os.path.join(work, "input.bin")
                    await download_message_media(client, target, media_path)
                    data = await extract_mediainfo(media_path)

                media = target.video or target.audio or target.document or target.animation
                name = getattr(media, "file_name", None) if media else None
                if not name and target.video:
                    name = "video"
                name = name or "file"

            name = name.replace("\n", " ").replace("\r", " ")[:180]

            body = format_output(data)
            header = f"📊 **MediaInfo for** `{name}`\n\n"
            full = header + f"```\n{body}\n```"
            if len(full) > 4000:
                bio = io.BytesIO(body.encode("utf-8"))
                bio.name = f"{name}.mediainfo.txt"
                await status.edit_text(
                    f"📊 **MediaInfo for** `{name}` is long — sending as file…"
                )
                await message.reply_document(
                    document=bio,
                    caption=f"📊 MediaInfo · `{name}`",
                )
            else:
                await status.edit_text(full)
        except Exception as e:
            logger.exception("mi_command failed")
            try:
                await status.edit_text(f"❌ **MediaInfo failed:** `{e}`")
            except Exception:
                pass
        finally:
            cleanup_dir(work)
