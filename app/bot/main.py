from __future__ import annotations

import asyncio
import datetime
import html
import logging
import os
import mimetypes
import httpx
import secrets
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from wzgram import Client, filters
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
    get_watch_page_base_url,
)
from app.admin.auth import make_password_hash
from app.cms import rewrite_all_stored_catalog_buttons
from app.models.schemas import FileMetadata, User
from app.streamer.manager import session_manager
from app.utils.ngrok_tunnel import _extract_tunnel_url_from_logs, apply_auto_cloudflare_url
from app.utils.helpers import (
    generate_short_code,
    format_bytes,
    is_trycloudflare_url,
    normalize_public_base_url,
    extract_short_code_from_media_url,
    filestore_deep_link,
)
from app.utils.fsub import is_user_fsubbed, get_fsub_invite_buttons, fsub_restriction_text
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
import json
import zipfile
import shutil
from typing import Optional

logger = logging.getLogger(__name__)

# Tracks admin user IDs currently waiting to send bot token(s) after clicking [➕ Add Bot Token] in /addbots
_awaiting_bot_token_admins: set[int] = set()
_BOT_TOKEN_PATTERN = re.compile(r"\d{6,16}:[A-Za-z0-9_-]{30,60}")
_awaiting_shortener: dict[int, str] = {}
_shortener_draft: dict[int, dict] = {}
_awaiting_link_admins: set[int] = set()

# /addgroups state: user_id -> {"page_slug": str, "box": "watch"|"download"}
_awaiting_addgroups: dict[int, dict] = {}
# Short tokens for callback_data (Telegram limit 64 bytes) -> page slug
# token is ~8 hex chars so callback stays well under the limit even with long slugs
_addgroups_slug_tokens: dict[str, str] = {}
# /save restore: set of admin user_ids waiting for a backup ZIP
_awaiting_restore: set[int] = set()

# /fspanel await states: user_id -> action
_awaiting_fspanel: dict[int, str] = {}

# FileStore secondary bot (when admin sets a different bot token via /filestore)
_filestore_client: Optional[Client] = None
_filestore_client_token: Optional[str] = None
_DEFAULT_FILESTORE_AUTO_DELETE_SECONDS = 5 * 60  # 5 minutes default


async def get_runtime_admins() -> list[int]:
    """Owner + env admins + runtime admins from MongoDB."""
    ids = {int(settings.OWNER_ID)}
    for a in settings.admin_list:
        try:
            ids.add(int(a))
        except Exception:
            pass
    try:
        runtime = await db.site_settings.find_one({"_id": "runtime"}) or {}
        for a in runtime.get("runtime_admins") or []:
            try:
                ids.add(int(a))
            except Exception:
                pass
    except Exception:
        pass
    return sorted(ids)


async def get_filestore_auto_delete_seconds() -> int:
    """Seconds before FileStore-delivered messages are deleted (0 = disabled)."""
    try:
        runtime = await db.site_settings.find_one({"_id": "runtime"}) or {}
        val = runtime.get("filestore_auto_delete_seconds")
        if val is None:
            return _DEFAULT_FILESTORE_AUTO_DELETE_SECONDS
        return max(0, int(val))
    except Exception:
        return _DEFAULT_FILESTORE_AUTO_DELETE_SECONDS


async def get_runtime_fsub_ids() -> list[int]:
    """Env force-sub channels + runtime fsub list from MongoDB."""
    ids: list[int] = []
    seen = set()
    for cid in settings.fsub_list:
        try:
            i = int(cid)
            if i not in seen:
                ids.append(i)
                seen.add(i)
        except Exception:
            pass
    try:
        runtime = await db.site_settings.find_one({"_id": "runtime"}) or {}
        for cid in runtime.get("runtime_fsub") or []:
            try:
                i = int(cid)
                if i not in seen:
                    ids.append(i)
                    seen.add(i)
            except Exception:
                pass
    except Exception:
        pass
    return ids


async def _auto_delete_messages(client, chat_id: int, message_ids: list[int], delay: int | None = None):
    """Delete messages after delay (runtime FileStore setting, default 5 minutes)."""
    if delay is None:
        delay = await get_filestore_auto_delete_seconds()
    if not delay or delay <= 0:
        return
    try:
        await asyncio.sleep(int(delay))
        for mid in message_ids:
            if mid is None:
                continue
            try:
                await client.delete_messages(chat_id, mid)
            except Exception:
                pass
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.debug("auto-delete failed: %s", e)


async def deliver_filestore_file(client, user_id: int, file_data: dict, reply_to_message: Message | None = None) -> bool:
    """
    Deliver the stored Telegram file to the user with Watch/Download buttons.

    Does NOT require CHANNEL_ID. Priority:
      1) Original source chat_id / message_id (batch / link / upload source)
      2) storage_chat_id / storage_message_id (optional mirror)
      3) settings.CHANNEL_ID + stored message ids (optional)
      4) Live get_messages → fresh file_id → send_*
      5) Stored file_id send_* fallback
    """
    short_code = file_data.get("short_code") or ""
    fname = file_data.get("filename") or "file"
    file_id = (file_data.get("file_id") or "").strip()
    mime = (file_data.get("mime_type") or "").lower()

    storage_chat = file_data.get("storage_chat_id")
    storage_msg = file_data.get("storage_message_id")
    orig_chat = file_data.get("chat_id")
    orig_msg = file_data.get("message_id")

    candidates: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()

    def _add(cid, mid):
        if cid is None or mid is None:
            return
        try:
            pair = (int(cid), int(mid))
        except (TypeError, ValueError):
            return
        if pair not in seen:
            seen.add(pair)
            candidates.append(pair)

    # Prefer the original source the bot already has access to (batch/link).
    # Storage channel is optional, not required.
    _add(orig_chat, orig_msg)
    _add(storage_chat, storage_msg)
    if settings.CHANNEL_ID and storage_msg is not None:
        _add(settings.CHANNEL_ID, storage_msg)
    if settings.CHANNEL_ID and orig_msg is not None:
        _add(settings.CHANNEL_ID, orig_msg)

    async def _send_by_file_id(fid: str, media_mime: str):
        send_kwargs = {"chat_id": user_id, "caption": f"📂 {fname}"}
        m = (media_mime or mime or "").lower()
        if m.startswith("video/") or "video" in m:
            return await client.send_video(video=fid, **send_kwargs)
        if m.startswith("audio/") or "audio" in m:
            return await client.send_audio(audio=fid, **send_kwargs)
        if m.startswith("image/") or "photo" in m:
            try:
                return await client.send_photo(photo=fid, **send_kwargs)
            except Exception:
                return await client.send_document(document=fid, **send_kwargs)
        return await client.send_document(document=fid, **send_kwargs)

    copied = None
    last_err: Exception | None = None

    for from_chat, mid in candidates:
        # 1) copy
        try:
            copied = await client.copy_message(
                chat_id=user_id,
                from_chat_id=from_chat,
                message_id=mid,
            )
            if copied is not None:
                logger.info(
                    "FileStore delivered short_code=%s via copy from %s/%s",
                    short_code, from_chat, mid,
                )
                break
        except Exception as e:
            last_err = e
            logger.warning(
                "FileStore copy failed short_code=%s from %s/%s: %s",
                short_code, from_chat, mid, e,
            )

        # 2) forward
        try:
            if hasattr(client, "forward_messages"):
                copied = await client.forward_messages(
                    chat_id=user_id,
                    from_chat_id=from_chat,
                    message_ids=mid,
                )
                if isinstance(copied, list):
                    copied = copied[0] if copied else None
                if copied is not None:
                    logger.info(
                        "FileStore delivered short_code=%s via forward from %s/%s",
                        short_code, from_chat, mid,
                    )
                    break
        except Exception as e2:
            last_err = e2
            logger.warning(
                "FileStore forward failed short_code=%s from %s/%s: %s",
                short_code, from_chat, mid, e2,
            )

        # 3) get_messages → fresh file_id → send (works when bot can read the source)
        try:
            src = await client.get_messages(from_chat, mid)
            if isinstance(src, list):
                src = src[0] if src else None
            if src and not getattr(src, "empty", False):
                media = (
                    src.document or src.video or src.audio or src.animation
                    or src.voice or src.video_note or src.photo
                )
                if src.photo and isinstance(src.photo, (list, tuple)):
                    media = src.photo[-1]
                live_fid = getattr(media, "file_id", None) if media else None
                live_mime = getattr(media, "mime_type", None) if media else None
                if live_fid:
                    copied = await _send_by_file_id(str(live_fid), live_mime or "")
                    if copied is not None:
                        logger.info(
                            "FileStore delivered short_code=%s via live file_id from %s/%s",
                            short_code, from_chat, mid,
                        )
                        # Persist fresher file_id for next time
                        try:
                            await files_col.update_one(
                                {"short_code": short_code},
                                {"$set": {
                                    "file_id": str(live_fid),
                                    "file_unique_id": str(getattr(media, "file_unique_id", "") or ""),
                                    "mime_type": live_mime or file_data.get("mime_type"),
                                    "chat_id": from_chat,
                                    "message_id": mid,
                                }},
                            )
                        except Exception:
                            pass
                        break
        except Exception as e3:
            last_err = e3
            logger.warning(
                "FileStore get_messages/send failed short_code=%s from %s/%s: %s",
                short_code, from_chat, mid, e3,
            )

    # Last resort: stored file_id
    if copied is None and file_id:
        try:
            copied = await _send_by_file_id(file_id, mime)
            if copied is not None:
                logger.info("FileStore delivered short_code=%s via stored file_id", short_code)
        except Exception as e:
            last_err = e
            logger.warning("FileStore send by file_id failed short_code=%s: %s", short_code, e)
            try:
                copied = await client.send_document(
                    chat_id=user_id,
                    document=file_id,
                    caption=f"📂 {fname}",
                )
            except Exception as e3:
                last_err = e3
                logger.warning("FileStore document fallback failed: %s", e3)

    if copied is None:
        logger.error(
            "FileStore delivery exhausted for short_code=%s candidates=%s last_err=%s",
            short_code, candidates, last_err,
        )
        detail = str(last_err) if last_err else "no reachable source"
        raise RuntimeError(
            f"Could not deliver file (bot needs access to the source message). "
            f"Tried: {candidates or 'none'}. Last error: {detail}"
        )

    button_rows = []
    try:
        # Watch Online → Vercel website (proxies stream to VPS). Download can use same.
        watch_base = await get_watch_page_base_url()
        media_base = await get_active_cloudflare_url()
        if not watch_base:
            try:
                watch_base = normalize_public_base_url(settings.BASE_URL or settings.WEBSITE_URL or "")
            except Exception:
                watch_base = (settings.BASE_URL or settings.WEBSITE_URL or "").rstrip("/")
        dl_base = watch_base or media_base
        runtime = await db.site_settings.find_one({"_id": "runtime"}) or {}
        downloads_enabled = runtime.get("downloads_enabled", True)
        streaming_enabled = runtime.get("streaming_enabled", True)
        fs_username = (runtime.get("filestore_bot_username") or "").strip().lstrip("@")

        if streaming_enabled and watch_base and short_code:
            watch_url = f"{watch_base.rstrip('/')}/watch/{short_code}"
            button_rows.append(InlineKeyboardButton("🍿 Watch Online", url=watch_url))
        if dl_base and short_code:
            if downloads_enabled:
                dl_url = f"{dl_base.rstrip('/')}/dl/{short_code}"
                button_rows.append(InlineKeyboardButton("⚡ Direct Download", url=dl_url))
            elif fs_username:
                dl_url = filestore_deep_link(fs_username, short_code)
                button_rows.append(InlineKeyboardButton("📥 Get File in Bot", url=dl_url))
    except Exception as e:
        logger.debug("Could not build filestore buttons: %s", e)

    if settings.UPDATE_CHANNEL_URL:
        button_rows.append(InlineKeyboardButton("📢 Updates Channel", url=settings.UPDATE_CHANNEL_URL))

    kb = _kb([button_rows]) if button_rows else None
    delay_sec = await get_filestore_auto_delete_seconds()
    if delay_sec and delay_sec > 0:
        if delay_sec % 60 == 0:
            del_txt = f"{delay_sec // 60} minute(s)"
        else:
            del_txt = f"{delay_sec} second(s)"
        auto_line = f"⏱ This message will auto-delete in **{del_txt}**.\nSave the file above before it disappears."
    else:
        auto_line = "Save the file from the message above."

    info_msg = await client.send_message(
        user_id,
        f"✅ **File delivered**\n\n📂 `{html.escape(str(fname))}`\n\n{auto_line}",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
        reply_to_message_id=getattr(copied, "id", None) if copied else None,
    )

    ids_to_delete = []
    if copied is not None and getattr(copied, "id", None):
        ids_to_delete.append(copied.id)
    if info_msg is not None and getattr(info_msg, "id", None):
        ids_to_delete.append(info_msg.id)
    if ids_to_delete and delay_sec and delay_sec > 0:
        asyncio.create_task(_auto_delete_messages(client, user_id, ids_to_delete, delay_sec))
    return True



async def stop_filestore_client():
    """Stop the secondary FileStore bot client if running."""
    global _filestore_client, _filestore_client_token
    client = _filestore_client
    _filestore_client = None
    _filestore_client_token = None
    if client is None:
        return
    try:
        if getattr(client, "is_connected", False):
            await client.stop()
    except Exception as e:
        logger.warning("Error stopping filestore client: %s", e)


async def start_filestore_client(token: str, username: str | None = None) -> dict:
    """
    Start a dedicated FileStore bot client with the given token so deep-link
    /start file_<code> is handled even when the FileStore bot is not the primary bot.
    If the token is the same as the primary bot, do nothing (primary already handles it).
    """
    global _filestore_client, _filestore_client_token
    clean = (token or "").strip()
    if not clean or ":" not in clean:
        raise ValueError("Invalid bot token")

    if clean == (settings.BOT_TOKEN or "").strip():
        await stop_filestore_client()
        return {"same_as_primary": True, "username": username or ""}

    if _filestore_client is not None and _filestore_client_token == clean:
        me = None
        try:
            me = await _filestore_client.get_me()
        except Exception:
            pass
        return {
            "same_as_primary": False,
            "username": getattr(me, "username", None) or username or "",
            "already_running": True,
        }

    await stop_filestore_client()

    client = Client(
        name=f"HITLER YT_filestore_{clean.split(':', 1)[0]}",
        api_id=settings.API_ID,
        api_hash=settings.API_HASH,
        bot_token=clean,
        in_memory=True,
        workers=16,
        max_concurrent_transmissions=16,
    )

    @client.on_message(filters.command("start") & filters.private)
    async def filestore_start_handler(c, message: Message):
        user = message.from_user
        if not user or user.is_bot:
            return
        uid = user.id
        payload = ""
        try:
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) > 1:
                payload = parts[1].strip()
        except Exception:
            payload = ""
        if not payload:
            await message.reply(
                "📦 **FileStore Bot**\n\n"
                "Open a file deep-link from the website to receive the file here."
            )
            return
        code = payload
        if code.lower().startswith("file_"):
            code = code[5:]
        code = code.strip()
        if not code:
            return
        file_data = await files_col.find_one({"short_code": code})
        if not file_data:
            await message.reply("❌ File not found or link expired.")
            return
        if not await is_user_fsubbed(c, uid):
            invite_rows = await get_fsub_invite_buttons(c, uid)
            body = await fsub_restriction_text(c, uid)
            await message.reply(
                body + "\n\n> Then open the download link again.",
                reply_markup=_kb(invite_rows) if invite_rows else None,
            )
            return
        try:
            await deliver_filestore_file(c, uid, file_data, message)
        except Exception as e:
            logger.exception("FileStore secondary delivery failed: %s", e)
            err = html.escape(str(e)[:220])
            await message.reply(
                "❌ **Could not deliver this file**\n\n"
                f"<code>{err}</code>\n\n"
                "> Possible causes:\n"
                "> • Bot cannot access the **source** channel/message (add bot as member/admin there)\n"
                "> • Original message was deleted from Telegram\n"
                "> • file_id expired — re-run `/batch` or `/link` on the source\n\n"
                "> Storage channel (`CHANNEL_ID`) is **optional**. Access to the source chat is enough.",
                parse_mode=ParseMode.HTML,
            )

    await client.start()
    try:
        me = await client.get_me()
        resolved_username = getattr(me, "username", None) or username or ""
    except Exception as e:
        try:
            await client.stop()
        except Exception:
            pass
        raise RuntimeError(f"FileStore bot getMe failed: {e}") from e

    # Minimal admin commands on secondary FileStore bot
    try:
        await client.set_bot_commands([
            BotCommand("start", "Get file from deep link"),
            BotCommand("fspanel", "Manage admins, fsub, auto-delete"),
        ])
    except Exception as e:
        logger.debug("Could not set FileStore bot commands: %s", e)

    # Register /fspanel on secondary client (reuse same logic via message routing is hard;
    # re-bind a compact panel handler here)
    @client.on_message(filters.command("fspanel") & filters.private)
    async def secondary_fspanel(c, message: Message):
        user = message.from_user
        if not user or user.is_bot:
            return
        await refresh_runtime_admin_cache()
        if not _is_admin_user(user.id):
            return await message.reply("🚫 Admins only.")
        runtime = await db.site_settings.find_one({"_id": "runtime"}) or {}
        admins = await get_runtime_admins()
        fsubs = await get_runtime_fsub_ids()
        adel = await get_filestore_auto_delete_seconds()
        fs_user = (runtime.get("filestore_bot_username") or resolved_username or "").strip().lstrip("@")
        adel_txt = "off" if adel <= 0 else (f"{adel // 60} min" if adel % 60 == 0 else f"{adel} sec")
        await message.reply(
            "🛠 **FileStore Panel**\n\n"
            f"> Bot: `@{fs_user}`\n"
            f"> Auto-delete: `{adel_txt}`\n"
            f"> Admins: {', '.join('`'+str(a)+'`' for a in admins)}\n"
            f"> FSub IDs: {', '.join('`'+str(x)+'`' for x in fsubs) or '*(none)*'}\n\n"
            "Use the primary bot `/fspanel` with buttons to add/remove admins, FSub channels, and auto-delete time.\n"
            "Or send commands here:\n"
            "• `/fspanel` — show status\n"
            "Manage changes are stored in the shared database."
        )

    _filestore_client = client
    _filestore_client_token = clean
    logger.info("FileStore secondary bot started as @%s", resolved_username)
    return {"same_as_primary": False, "username": resolved_username, "already_running": False}


async def resolve_bot_username_from_token(token: str) -> str:
    """Temporarily start a client to resolve public username from a bot token."""
    clean = (token or "").strip()
    if clean == (settings.BOT_TOKEN or "").strip():
        try:
            me = await session_manager.bot_client.get_me()
            return (getattr(me, "username", None) or "").strip()
        except Exception:
            return ""

    client = Client(
        name=f"HITLER YT_fs_resolve_{clean.split(':', 1)[0]}",
        api_id=settings.API_ID,
        api_hash=settings.API_HASH,
        bot_token=clean,
        in_memory=True,
        workers=4,
        no_updates=True,
    )
    await client.start()
    try:
        me = await client.get_me()
        return (getattr(me, "username", None) or "").strip()
    finally:
        try:
            await client.stop()
        except Exception:
            pass


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


# Cached runtime admin IDs (refreshed from Mongo on panel changes / startup)
_runtime_admin_cache: set[int] = set()


def _is_admin_user(user_id: int) -> bool:
    if user_id == settings.OWNER_ID or user_id in settings.admin_list:
        return True
    return int(user_id) in _runtime_admin_cache


async def refresh_runtime_admin_cache() -> None:
    global _runtime_admin_cache
    try:
        ids = await get_runtime_admins()
        _runtime_admin_cache = set(int(x) for x in ids)
    except Exception:
        pass


def extract_filename_from_caption(caption: str | None, fallback: str) -> str:
    """Extract real filename from caption: first letter through extension, ignore emojis/metadata."""
    if not caption:
        return fallback
    # Prefer .mkv then common video/audio extensions
    m = re.search(
        r"([A-Za-z0-9][\w\s.\-()\[\],+@]*?\.(?:mkv|mp4|avi|mov|webm|m4v|ts|m2ts|mpg|mpeg|wmv|flv|3gp|mp3|m4a|flac|wav|aac|ogg))",
        caption,
        re.I,
    )
    if m:
        return m.group(1).strip()
    return fallback


async def find_catalog_page(slug_or_title: str) -> dict | None:
    """Resolve a catalog page by slug (exact, then case-insensitive) or title fragment.

    Searches the bot DATABASE_NAME first, then alternate names Vercel may use
    (tg_media_bot / animovieszoneflix) so website pages are findable even if env differs slightly.
    """
    from app.database.connection import client as mongo_client

    raw = (slug_or_title or "").strip()
    if not raw:
        return None
    slug = raw.lower().strip("/")
    title_q = re.sub(r"[-_]+", " ", slug).strip()

    primary_name = getattr(settings, "DATABASE_NAME", None) or "tg_media_bot"
    names = []
    for n in (primary_name, "tg_media_bot", "animovieszoneflix"):
        if n and n not in names:
            names.append(n)

    for db_name in names:
        try:
            coll = mongo_client[db_name]["catalog_pages"]
        except Exception:
            continue
        page = await coll.find_one({"slug": slug})
        if page:
            page["_db_name"] = db_name
            return page
        page = await coll.find_one({"slug": {"$regex": f"^{re.escape(slug)}$", "$options": "i"}})
        if page:
            page["_db_name"] = db_name
            return page
        if title_q:
            page = await coll.find_one({"title": {"$regex": re.escape(title_q), "$options": "i"}})
            if page:
                page["_db_name"] = db_name
                return page
    return None


async def catalog_page_suggestions(query: str, limit: int = 8) -> list[str]:
    """Return short suggestion lines when a page lookup fails (searches primary + alternate DBs)."""
    from app.database.connection import client as mongo_client

    q = (query or "").strip()
    lines: list[str] = []
    seen_slugs: set[str] = set()
    primary_name = getattr(settings, "DATABASE_NAME", None) or "tg_media_bot"
    names = []
    for n in (primary_name, "tg_media_bot", "animovieszoneflix"):
        if n and n not in names:
            names.append(n)
    try:
        filt: dict = {}
        if q:
            safe = re.escape(re.sub(r"[-_]+", " ", q.lower()))
            filt = {
                "$or": [
                    {"slug": {"$regex": re.escape(q), "$options": "i"}},
                    {"title": {"$regex": safe, "$options": "i"}},
                ]
            }
        for db_name in names:
            try:
                coll = mongo_client[db_name]["catalog_pages"]
                cursor = coll.find(filt or {}, {"slug": 1, "title": 1, "published": 1}).sort("updated_at", -1).limit(limit)
                async for doc in cursor:
                    slug = doc.get("slug") or ""
                    if slug in seen_slugs:
                        continue
                    seen_slugs.add(slug)
                    pub = "✓" if doc.get("published") else "draft"
                    lines.append(f"• `{slug}` — {doc.get('title') or '?'} ({pub}) [{db_name}]")
                    if len(lines) >= limit:
                        return lines
            except Exception:
                continue
    except Exception as e:
        logger.warning("catalog suggestions failed: %s", e)
    return lines


def parse_groups_text(text: str, box: str = "watch") -> list[dict]:
    """
    Parse admin group/button text into structured groups.
    Format:
      1. Group name
         Button name: Button URL
      2. Another group
         Btn: https://...
    """
    groups: list[dict] = []
    current: dict | None = None
    lines = (text or "").splitlines()
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        # New group: "1. Name" or "1) Name" or just numbered
        gm = re.match(r"^\d+[\.\)]\s*(.+)$", line)
        if gm:
            if current and current.get("buttons"):
                groups.append(current)
            gname = gm.group(1).strip()
            current = {"box": box, "name": gname or f"{'Watch' if box == 'watch' else 'Download'} Group", "buttons": []}
            continue
        # Button: "Name: URL" or "Name - URL"
        bm = re.match(r"^(.+?)\s*[:：\-–]\s*(https?://\S+)\s*$", line, re.I)
        if bm and current is not None:
            bname = bm.group(1).strip()
            burl = bm.group(2).strip()
            if bname and burl:
                current["buttons"].append({"name": bname, "url": burl})
            continue
        # If we have a group but no buttons yet and line looks like a name only, treat as group name refresh
        if current is None and line and not line.startswith("http"):
            current = {"box": box, "name": line, "buttons": []}
    if current and current.get("buttons"):
        groups.append(current)
    return groups


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
            BotCommand("addbots", "Add unlimited bot tokens to boost download speed (admin)"),
            BotCommand("shorteners", "Manage link shorteners and key expiry (admin)"),
            BotCommand("link", "Generate download and watch links for media (admin)"),
            BotCommand("panel", "Open control panel (admin)"),
            BotCommand("manage", "Edit website pages and bot settings (admin)"),
            BotCommand("username", "Set website panel username (admin)"),
            BotCommand("password", "Set website panel password (admin)"),
            BotCommand("backendurl", "Set backend / ngrok URL (admin)"),
            BotCommand("baseurl", "Set primary / ngrok URL (admin)"),
            BotCommand("filestore", "Set FileStore bot for deep-link downloads (admin)"),
            BotCommand("fspanel", "FileStore panel: admins, fsub, auto-delete (admin)"),
            BotCommand("categories", "Manage categories (admin)"),
            BotCommand("edit", "Open page editor (admin)"),
            BotCommand("search", "Search catalog (admin)"),
            BotCommand("stats", "Bot statistics (admin)"),
            BotCommand("broadcast", "Broadcast message (admin)"),
            BotCommand("ban", "Ban a user (admin)"),
            BotCommand("unban", "Unban a user (admin)"),
            BotCommand("autodel", "Auto-delete settings (admin)"),
            BotCommand("addgroups", "Add download/watch groups to a page (admin)"),
            BotCommand("del", "Delete a website page by link, title, or slug (admin)"),
            BotCommand("save", "Backup or restore full project data (admin)"),
            BotCommand("batch", "Generate links for a message ID range (admin)"),
            BotCommand("ss", "Generate screenshots"),
            BotCommand("sv", "Generate sample video"),
            BotCommand("mi", "MediaInfo for a file"),
        ]
        await bot.set_bot_commands(commands)
        logger.info("Bot commands registered successfully (%d cmds)", len(commands))
    except Exception:
        logger.exception("Failed to register bot commands")


def _build_addbots_summary_and_kb() -> tuple[str, InlineKeyboardMarkup]:
    helper_list = session_manager.list_helper_bots()
    total_clients = len(session_manager.get_all_clients())
    if helper_list:
        lines = []
        for idx, b in enumerate(helper_list, start=1):
            status_icon = "🟢" if b.get("connected") else "🔴"
            uname = f"@{b['username']}" if b.get("username") else b.get("first_name") or f"ID {b['bot_id']}"
            lines.append(f"{idx}. {status_icon} **{html.escape(uname)}** (`{b['bot_id']}`)")
        bots_text = "\n".join(lines)
    else:
        bots_text = "_No extra helper bots added yet._"

    channel_note = (
        f"📌 **Storage Channel (`CHANNEL_ID`):** `{settings.CHANNEL_ID}`\n"
        "> *(Make sure every helper bot is added as an Admin in your storage channel so all bots stream in parallel!)*"
        if settings.CHANNEL_ID
        else "💡 **Tip:** Set `CHANNEL_ID` in `.env` and add your helper bots as admins in that channel so all helper bots can parallel-stream user uploads!"
    )

    text = (
        "🚀 **Multi-Bot Download Speed Booster (`/addbots`)**\n\n"
        f"> ⚡ **Active Streaming Clients Pool:** `{total_clients}`\n"
        f"> 🤖 **Added Helper Bots:** `{len(helper_list)}` (Unlimited supported)\n\n"
        f"**Connected Helper Bots:**\n{bots_text}\n\n"
        f"{channel_note}\n\n"
        "Click **➕ Add Bot Token** below to add one or more bot tokens and boost download speed:"
    )

    rows = [
        [InlineKeyboardButton("➕ Add Bot Token", callback_data="addbots:add")],
    ]
    if helper_list:
        rows.append([
            InlineKeyboardButton("🗑️ Remove a Bot", callback_data="addbots:remove_menu"),
            InlineKeyboardButton("🔄 Refresh Status", callback_data="addbots:refresh"),
        ])
    else:
        rows.append([InlineKeyboardButton("🔄 Refresh Status", callback_data="addbots:refresh")])
    return text, _kb(rows)


async def _process_and_save_bot_tokens(raw_text: str, user_id: int) -> tuple[list[dict], list[str]]:
    tokens = list(dict.fromkeys(_BOT_TOKEN_PATTERN.findall(raw_text or "")))
    added = []
    errors = []
    for tok in tokens:
        try:
            meta = await session_manager.add_helper_bot(tok, added_by=user_id, persist=True)
            added.append(meta)
        except Exception as exc:
            masked = tok.split(":", 1)[0] + ": ***"
            errors.append(f"`{masked}` → {html.escape(str(exc))}")
    return added, errors


def register_handlers(bot):
    """Register wzgram (Pyrogram-compatible) handlers on the bot client."""

    async def _show_shortener_settings(message):
        cfg = await db.site_settings.find_one({"_id":"shortener_config"}) or {}
        providers = cfg.get("shorteners", [])
        state = "🟢 Enabled" if cfg.get("enabled") else "🔴 Disabled"
        lines = [f"🔗 **Shortener Settings**", f"Status: {state}", f"Configured: {len(providers)}", f"Key expiry: `{cfg.get('expiry_seconds', 300)} seconds`"]
        rows = [
            [InlineKeyboardButton("🟢 Enable" if not cfg.get("enabled") else "🔴 Disable", callback_data="shrt:toggle")],
            [InlineKeyboardButton("➕ Add", callback_data="shrt:add"), InlineKeyboardButton("⏱ Expiry Time", callback_data="shrt:expiry")],
        ]
        for i, item in enumerate(providers):
            url = html.escape(str(item.get("url", "")))
            lines.append(f"{i + 1}. `{url}`")
            rows.append([InlineKeyboardButton(f"🗑 Delete #{i + 1}", callback_data=f"shrt:delete:{i}")])
        if not providers:
            lines.append("No shorteners configured yet.")
        rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="shrt:refresh")])
        await message.reply("\n".join(lines), reply_markup=_kb(rows))

    @bot.on_message(filters.command("shorteners") & filters.private)
    async def shorteners_command(client, message: Message):
        if not message.from_user or not _is_admin_user(message.from_user.id):
            return await message.reply("🚫 Admins only.")
        await _show_shortener_settings(message)

    @bot.on_callback_query(filters.regex(r"^shrt:(toggle|add|expiry|refresh|delete:\d+)$"))
    async def shortener_callback(client, callback: CallbackQuery):
        user = callback.from_user
        if not user or not _is_admin_user(user.id):
            return await callback.answer("Admins only", show_alert=True)
        action = callback.data.split(":",1)[1]
        cfg = await db.site_settings.find_one({"_id":"shortener_config"}) or {"_id":"shortener_config","enabled":False,"shorteners":[],"expiry_seconds":300}
        if action.startswith("delete:"):
            try:
                index = int(action.split(":", 1)[1])
            except (TypeError, ValueError):
                return await callback.answer("Invalid shortener selection", show_alert=True)
            providers = list(cfg.get("shorteners") or [])
            if index < 0 or index >= len(providers):
                await callback.answer("That shortener was already removed. Refreshing…", show_alert=True)
            else:
                removed = providers.pop(index)
                await db.site_settings.update_one(
                    {"_id": "shortener_config"},
                    {"$set": {"shorteners": providers}},
                    upsert=True,
                )
                await callback.answer("Shortener deleted")
                logger.info("Admin %s deleted configured shortener %s", user.id, removed.get("url", "<unknown>"))
            await _show_shortener_settings(callback.message)
            return
        if action == "toggle":
            new_enabled = not bool(cfg.get("enabled", False))
            await db.site_settings.update_one(
                {"_id": "shortener_config"},
                {
                    "$set": {
                        "enabled": new_enabled,
                        "shorteners": list(cfg.get("shorteners") or []),
                        "expiry_seconds": int(cfg.get("expiry_seconds") or 300),
                    }
                },
                upsert=True,
            )
            logger.info("Admin %s set shortener enabled=%s", user.id, new_enabled)
        elif action == "add":
            _awaiting_shortener[user.id] = "url"
            return await callback.message.reply(
                "Send the shortener API URL.\n"
                "Example: `https://shortlinkgenerator.onrender.com/api`"
            )
        elif action == "expiry":
            _awaiting_shortener[user.id] = "expiry"
            return await callback.message.reply("Send key expiry, e.g. `300s`, `5m`, `2h`, `1d` or `300` (seconds).")
        await callback.answer("Updated")
        await _show_shortener_settings(callback.message)

    @bot.on_message(filters.command("start") & filters.private)
    async def start_handler(client, message: Message):
        user = message.from_user
        if not user or user.is_bot:
            return
        user_id = user.id
        _awaiting_bot_token_admins.discard(user_id)
        access_cfg = await db.site_settings.find_one({"_id": "runtime"}) or {}
        access_mode = access_cfg.get("access_mode", "all")
        if access_mode == "owners" and user_id != settings.OWNER_ID:
            return await message.reply("🔒 This bot is currently restricted to its owner.")
        if access_mode == "admins" and not _is_admin_user(user_id):
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

        # ── FileStore deep-link: /start <short_code> or /start file_<short_code> ──
        # Users open t.me/Bot?start=file_CODE and receive the file + Watch/Download buttons.
        # Messages auto-delete after 5 minutes.
        payload = ""
        try:
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) > 1:
                payload = parts[1].strip()
        except Exception:
            payload = ""
        if payload:
            code = payload
            if code.lower().startswith("file_"):
                code = code[5:]
            code = code.strip()
            if code:
                file_data = await files_col.find_one({"short_code": code})
                if file_data:
                    if not await is_user_fsubbed(client, user_id):
                        invite_rows = await get_fsub_invite_buttons(client, user_id)
                        body = await fsub_restriction_text(client, user_id)
                        return await message.reply(
                            body + "\n\n> Then open the download link again.",
                            reply_markup=_kb(invite_rows) if invite_rows else None,
                        )
                    try:
                        ok = await deliver_filestore_file(client, user_id, file_data, message)
                        if ok:
                            return
                        return await message.reply(
                            "❌ **Could not deliver this file**\n\n"
                            "> Possible causes:\n"
                            "> • Bot cannot access the **source** channel/message (add bot as member/admin there)\n"
                            "> • Original message was deleted from Telegram\n"
                            "> • file_id expired — re-run `/batch` or `/link` on the source\n\n"
                            "> Storage channel (`CHANNEL_ID`) is **optional**. Access to the source chat is enough."
                        )
                    except Exception as e:
                        logger.exception("FileStore delivery failed for short_code=%s: %s", code, e)
                        err = html.escape(str(e)[:220])
                        return await message.reply(
                            "❌ **Could not deliver this file**\n\n"
                            f"<code>{err}</code>\n\n"
                            "> Possible causes:\n"
                            "> • Bot cannot access the **source** channel/message (add bot as member/admin there)\n"
                            "> • Original message was deleted from Telegram\n"
                            "> • file_id expired — re-run `/batch` or `/link` on the source\n\n"
                            "> Storage channel (`CHANNEL_ID`) is **optional**. Access to the source chat is enough.",
                            parse_mode=ParseMode.HTML,
                        )
                else:
                    # Unknown code — fall through to normal greeting (avoid leaking existence)
                    pass

        if not await is_user_fsubbed(client, user_id):
            invite_rows = await get_fsub_invite_buttons(client, user_id)
            body = await fsub_restriction_text(client, user_id)
            return await message.reply(
                body + "\n\n> Then send /start again.",
                reply_markup=_kb(invite_rows) if invite_rows else None,
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

    # ─── /addbots Command & Interactive Inline Button Flow ────────────────────

    @bot.on_message(filters.command("addbots") & filters.private)
    async def addbots_command_handler(client, message: Message):
        user = message.from_user
        if not user or user.is_bot or not _is_admin_user(user.id):
            return await message.reply("🚫 **Unauthorized Access** — Only bot admins can manage helper bots.")

        raw = (message.text or "").strip()
        parts = raw.split(maxsplit=1)
        # If the admin passed tokens directly with /addbots <token1> <token2>...
        if len(parts) > 1 and _BOT_TOKEN_PATTERN.search(parts[1]):
            try:
                await message.delete()
            except Exception:
                pass
            status_msg = await message.reply("⚡ **Validating & starting helper bot token(s)...**")
            added, errors = await _process_and_save_bot_tokens(parts[1], user.id)
            summary_lines = []
            if added:
                summary_lines.append(f"✅ **Added & Started {len(added)} Helper Bot(s):**")
                for m in added:
                    summary_lines.append(f"• @{html.escape(m.get('username') or '')} (`{m['bot_id']}`)")
            if errors:
                summary_lines.append("\n⚠️ **Failed Tokens:**")
                summary_lines.extend(f"• {e}" for e in errors)
            summary_lines.append(f"\n🚀 **Total Active Download Booster Clients:** `{len(session_manager.get_all_clients())}`")
            return await status_msg.edit_text(
                "\n".join(summary_lines),
                reply_markup=_kb([
                    [InlineKeyboardButton("➕ Add More Bot Tokens", callback_data="addbots:add")],
                    [InlineKeyboardButton("📋 View All Helper Bots", callback_data="addbots:refresh")],
                ]),
            )

        text, kb = _build_addbots_summary_and_kb()
        await message.reply(text, reply_markup=kb)

    @bot.on_callback_query(filters.regex(r"^addbots:"))
    async def addbots_callback_handler(client, callback: CallbackQuery):
        user = callback.from_user
        if not user or not _is_admin_user(user.id):
            return await callback.answer("🚫 Admins only", show_alert=True)

        action = (callback.data or "").split(":", 1)[1]

        if action == "add":
            _awaiting_bot_token_admins.add(user.id)
            await callback.answer("Send the Bot Token now")
            return await callback.message.edit_text(
                "🤖 **Add Helper Bot Token (Unlimited)**\n\n"
                "> Send the **Bot Token** from @BotFather now (e.g. `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`).\n"
                "> 💡 *You can send a single token or paste multiple bot tokens at once (separated by spaces or newlines).* \n\n"
                "All added bots are saved to MongoDB and used in parallel to boost download & streaming speeds.",
                reply_markup=_kb([[InlineKeyboardButton("❌ Cancel", callback_data="addbots:cancel")]]),
            )

        if action == "cancel":
            _awaiting_bot_token_admins.discard(user.id)
            await callback.answer("Cancelled")
            text, kb = _build_addbots_summary_and_kb()
            return await callback.message.edit_text(text, reply_markup=kb)

        if action == "refresh":
            _awaiting_bot_token_admins.discard(user.id)
            await session_manager.load_saved_helper_bots()
            await callback.answer("Refreshed helper bot pool")
            text, kb = _build_addbots_summary_and_kb()
            return await callback.message.edit_text(text, reply_markup=kb)

        if action == "remove_menu":
            helper_list = session_manager.list_helper_bots()
            if not helper_list:
                await callback.answer("No helper bots to remove", show_alert=True)
                text, kb = _build_addbots_summary_and_kb()
                return await callback.message.edit_text(text, reply_markup=kb)
            rows = []
            for b in helper_list:
                uname = f"@{b['username']}" if b.get("username") else f"ID {b['bot_id']}"
                rows.append([InlineKeyboardButton(f"🗑️ Remove {uname}", callback_data=f"addbots:del:{b['bot_id']}")])
            rows.append([InlineKeyboardButton("⬅️ Back", callback_data="addbots:refresh")])
            await callback.answer()
            return await callback.message.edit_text(
                "🗑️ **Remove a Helper Bot**\n\nSelect a bot below to stop and remove it from the download pool:",
                reply_markup=_kb(rows),
            )

        if action.startswith("del:"):
            try:
                bot_id = int(action.split(":", 1)[1])
                await session_manager.remove_helper_bot(bot_id)
                await callback.answer("Helper bot removed")
            except Exception as exc:
                await callback.answer(f"Failed: {exc}", show_alert=True)
            text, kb = _build_addbots_summary_and_kb()
            return await callback.message.edit_text(text, reply_markup=kb)

    async def _maybe_handle_bot_token_input(client, message: Message, user) -> bool:
        """Handle bot token input sent by an admin after clicking [➕ Add Bot Token] in /addbots.

        Returns True if this message was claimed and fully handled (caller must stop),
        False if it should fall through to the next text-input handler (e.g. shorteners,
        ad-free text flows, or a t.me link). This is a plain helper (not a registered
        handler) so that only ONE @bot.on_message(filters.text & filters.private & ~^/)
        handler exists — registering two handlers on the same overlapping filter means
        only the first one ever runs and silently swallows every other admin text input.
        """
        if not user or user.is_bot or not _is_admin_user(user.id):
            return False
        raw_text = (message.text or "").strip()
        if user.id not in _awaiting_bot_token_admins and not _BOT_TOKEN_PATTERN.fullmatch(raw_text):
            return False

        matches = _BOT_TOKEN_PATTERN.findall(raw_text)
        if not matches:
            await message.reply(
                "❌ **No valid Telegram Bot Token detected.**\n\n"
                "Please send a valid Bot Token from @BotFather (format: `123456789:ABCdefGHI...`), or click **Cancel**.",
                reply_markup=_kb([[InlineKeyboardButton("❌ Cancel", callback_data="addbots:cancel")]]),
            )
            return True

        _awaiting_bot_token_admins.discard(user.id)
        try:
            await message.delete()
        except Exception:
            pass

        status_msg = await message.reply(f"⚡ **Connecting & saving {len(matches)} helper bot token(s)...**")
        added, errors = await _process_and_save_bot_tokens(raw_text, user.id)

        lines = []
        if added:
            lines.append(f"✅ **Successfully Added {len(added)} Helper Bot(s) to Download Booster Pool:**")
            for m in added:
                lines.append(f"• 🤖 @{html.escape(m.get('username') or '')} (`{m['bot_id']}`)")
        if errors:
            lines.append("\n⚠️ **Could Not Start:**")
            lines.extend(f"• {e}" for e in errors)

        lines.append(f"\n🚀 **Total Active Download Booster Clients:** `{len(session_manager.get_all_clients())}`")
        if settings.CHANNEL_ID:
            lines.append(f"📌 *Remember to add new helper bots as Admin in your storage channel (`{settings.CHANNEL_ID}`) for parallel multi-bot speed!*")

        await status_msg.edit_text(
            "\n".join(lines),
            reply_markup=_kb([
                [InlineKeyboardButton("➕ Add Another Bot Token", callback_data="addbots:add")],
                [InlineKeyboardButton("📋 View All Helper Bots", callback_data="addbots:refresh")],
            ]),
        )
        return True

    @bot.on_message(filters.command("link") & filters.private)
    async def link_command(client, message: Message):
        user = message.from_user
        if not user or not _is_admin_user(user.id):
            return await message.reply("🚫 Admins only.")
        command_parts = (message.text or "").split(maxsplit=1)
        if len(command_parts) > 1 and command_parts[1].strip():
            # Process an existing public/private Telegram message URL directly; no storage copy.
            return await telegram_message_link_handler(client, message, raw_override=command_parts[1].strip())
        _awaiting_link_admins.add(user.id)
        await message.reply("📎 Send or forward the Telegram file, video, audio, photo, or document now. I will generate its Download and Watch links. You can also use /link https://t.me/channel/123")

    @bot.on_message((filters.document | filters.video | filters.audio | filters.animation | filters.photo | filters.voice | filters.video_note) & ~filters.channel)
    async def media_handler(client, message: Message):
        user = message.from_user
        if not user or user.is_bot:
            return
        # /save → Restore: handle ZIP here so media_handler never swallows the upload
        if user.id in _awaiting_restore and message.document:
            fname = (message.document.file_name or "").lower()
            mime = (message.document.mime_type or "").lower()
            if fname.endswith(".zip") or "zip" in mime:
                await _handle_restore_zip(client, message)
                return
        explicit_link_request = user.id in _awaiting_link_admins
        if explicit_link_request:
            _awaiting_link_admins.discard(user.id)

        access_cfg = await db.site_settings.find_one({"_id": "runtime"}) or {}
        access_mode = access_cfg.get("access_mode", "all")
        if access_mode == "owners" and user.id != settings.OWNER_ID:
            return await message.reply("🔒 This bot is currently restricted to its owner.")
        if access_mode == "admins" and not _is_admin_user(user.id):
            return await message.reply("🔒 This bot is currently restricted to administrators.")
        runtime_cfg = await db.site_settings.find_one({"_id": "runtime"}) or {}
        downloads_enabled = runtime_cfg.get("downloads_enabled", True)
        streaming_enabled = runtime_cfg.get("streaming_enabled", True)
        fs_configured = bool((runtime_cfg.get("filestore_bot_username") or "").strip())
        if explicit_link_request:
            downloads_enabled = streaming_enabled = True
        if not downloads_enabled and not streaming_enabled and not fs_configured:
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
            invite_rows = await get_fsub_invite_buttons(client, user.id)
            body = await fsub_restriction_text(client, user.id)
            return await message.reply(
                body + "\n\n> Then upload your file again.",
                reply_markup=_kb(invite_rows) if invite_rows else None,
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

        log_cf = _extract_tunnel_url_from_logs()
        if log_cf:
            await apply_auto_cloudflare_url(log_cf, source="media-upload-check")

        # Prefer Vercel website for Watch Online; tunnel still used as fallback
        media_base = await get_active_cloudflare_url()
        watch_base = await get_watch_page_base_url()
        if watch_base or media_base:
            base_url = watch_base or media_base
            links_ready = True
        else:
            try:
                base_url = normalize_public_base_url(settings.BASE_URL or settings.WEBSITE_URL or "")
                links_ready = True
            except ValueError as exc:
                logger.warning("Public BASE_URL not configured: %s", exc)
                base_url = None
                links_ready = False

        formatted_size = format_bytes(file_size) if file_size else "N/A"
        safe_name = html.escape(file_name)
        safe_mime = html.escape(mime_type)

        if links_ready:
            # When downloads are disabled, replace /dl/ with FileStore deep link if configured
            fs_username = (runtime_cfg.get("filestore_bot_username") or "").strip().lstrip("@")
            if downloads_enabled:
                download_url = f"{base_url}/dl/{short_code}"
            elif fs_username:
                download_url = f"https://t.me/{fs_username}?start=file_{short_code}"
            else:
                download_url = None
            stream_url = f"{base_url}/watch/{short_code}" if streaming_enabled else None
            active_links = []
            button_rows = []
            if download_url:
                if downloads_enabled:
                    label = "⚡ Direct Download"
                    link_text = "Direct Download"
                else:
                    label = "📥 Get File in Bot"
                    link_text = "Get File in Bot"
                active_links.append(f"📥 <a href=\"{html.escape(download_url, quote=True)}\">{link_text}</a>" if not downloads_enabled else f"⚡ <a href=\"{html.escape(download_url, quote=True)}\">{link_text}</a>")
                button_rows.append(InlineKeyboardButton(label, url=download_url))
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
                "The ngrok tunnel URL is being auto-detected. Try again in a few seconds or use `/baseurl https://your-domain.ngrok-free.app`.\n\n"
                f"📢 <a href=\"{html.escape(settings.UPDATE_CHANNEL_URL, quote=True)}\">Updates Channel</a>"
            )
            user_buttons = _kb([[InlineKeyboardButton("📢 Updates Channel", url=settings.UPDATE_CHANNEL_URL)], [InlineKeyboardButton("🗑️ Delete Link", callback_data=f"del_{short_code}")]])
            channel_buttons = _kb([[InlineKeyboardButton("📢 Updates Channel", url=settings.UPDATE_CHANNEL_URL)]])

        try:
            await detecting_msg.delete()
        except Exception:
            pass

        if settings.CHANNEL_ID and settings.CHANNEL_ID != message.chat.id:
            try:
                log_msg = await message.copy(
                    chat_id=settings.CHANNEL_ID,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=channel_buttons,
                )
                if log_msg and getattr(log_msg, "id", None):
                    await files_col.update_one(
                        {"short_code": short_code},
                        {"$set": {"storage_chat_id": settings.CHANNEL_ID, "storage_message_id": log_msg.id}},
                    )
            except Exception as e:
                logger.exception("Error mirroring to log channel: %s", e)

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


    @bot.on_message(filters.text & filters.private & ~filters.regex(r"^/"))
    async def telegram_message_link_handler(client, message: Message, raw_override: str | None = None):
        """Create links for media already hosted in a Telegram message; never mirror it to storage."""
        user = message.from_user
        if not user or user.is_bot:
            return
        # Single owner of filters.text & filters.private & ~^/: check every pending
        # admin text-input state here, in order, before falling through to t.me link
        # parsing. Two handlers registered on the same (overlapping) filter would mean
        # only the first ever fires — see _maybe_handle_bot_token_input's docstring.
        if raw_override is None and await _maybe_handle_bot_token_input(client, message, user):
            return
        raw = (raw_override if raw_override is not None else (message.text or "")).strip()
        pending = _awaiting_shortener.get(user.id) if raw_override is None else None
        if pending and _is_admin_user(user.id):
            if pending == "url":
                if not raw.startswith(("http://", "https://")):
                    return await message.reply("Send a valid http:// or https:// API URL.")
                _awaiting_shortener[user.id] = "api"
                _shortener_draft[user.id] = {"url": raw}
                return await message.reply("Now send the shortener API key.")
            if pending == "api":
                draft = _shortener_draft.pop(user.id, {})
                if not draft:
                    _awaiting_shortener.pop(user.id, None)
                    return await message.reply("Setup expired. Run /shorteners and Add again.")
                cfg = await db.site_settings.find_one({"_id":"shortener_config"}) or {"_id":"shortener_config","shorteners":[],"enabled":False,"expiry_seconds":300}
                providers = cfg.get("shorteners", [])
                providers.append({"url":draft["url"],"api_key":raw})
                await db.site_settings.update_one({"_id":"shortener_config"},{"$set":{"shorteners":providers}},upsert=True)
                _awaiting_shortener.pop(user.id, None)
                return await _show_shortener_settings(message)
            if pending == "expiry":
                m = re.fullmatch(r"\s*(\d+)\s*([smhdw]?)\s*", raw.lower())
                if not m:
                    return await message.reply("Invalid duration. Use 300s, 5m, 2h, 1d or 1w.")
                amount, unit = int(m.group(1)), m.group(2)
                seconds = amount * {"":1,"s":1,"m":60,"h":3600,"d":86400,"w":604800}[unit]
                if seconds < 1 or seconds > 31536000:
                    return await message.reply("Expiry must be between 1 second and 365 days.")
                await db.site_settings.update_one({"_id":"shortener_config"},{"$set":{"expiry_seconds":seconds}},upsert=True)
                _awaiting_shortener.pop(user.id, None)
                return await _show_shortener_settings(message)

        # /addgroups text input (groups + buttons)
        if raw_override is None and user.id in _awaiting_addgroups and _is_admin_user(user.id):
            state = _awaiting_addgroups.get(user.id) or {}
            page_slug = state.get("page_slug")
            box = state.get("box") or "watch"
            if not page_slug:
                _awaiting_addgroups.pop(user.id, None)
                return await message.reply("Session expired. Reply to a page link with /addgroups again.")
            page = await find_catalog_page(page_slug)
            if not page:
                _awaiting_addgroups.pop(user.id, None)
                return await message.reply(
                    f"Catalog page `{page_slug}` not found in bot DB.\n"
                    "Confirm `DATABASE_NAME` matches the database used by the Vercel website."
                )
            page_slug = page.get("slug") or page_slug
            target_db_name = page.get("_db_name") or getattr(settings, "DATABASE_NAME", None) or "tg_media_bot"
            new_groups = parse_groups_text(raw, box=box)
            if not new_groups:
                return await message.reply(
                    "❌ Could not parse any groups/buttons.\n\n"
                    "Use this format:\n\n"
                    "`1. Group Name`\n"
                    "`Button Name: https://example.com`\n\n"
                    "`2. Another Group`\n"
                    "`Watch Now: https://...`"
                )
            existing = list(page.get("groups") or [])
            # /addgroups is append-only: preserve every previously saved group.
            # Skip only exact duplicates (same box, normalized name, and buttons/URLs).
            merged = list(existing)
            def _group_signature(group):
                group_box = (group.get("box") or "watch").strip().lower()
                group_name = (group.get("name") or "").strip().casefold()
                buttons = tuple(
                    ((button.get("name") or button.get("text") or "").strip().casefold(),
                     (button.get("url") or button.get("link") or "").strip())
                    for button in (group.get("buttons") or [])
                )
                return group_box, group_name, buttons
            existing_signatures = {_group_signature(group) for group in existing}
            added_groups = []
            for group in new_groups:
                signature = _group_signature(group)
                if signature in existing_signatures:
                    continue
                merged.append(group)
                existing_signatures.add(signature)
                added_groups.append(group)
            # Rewrite URLs to active Cloudflare base if configured
            cf = await get_active_cloudflare_url()
            if cf:
                from app.utils.helpers import rewrite_page_groups_to_cloudflare
                merged = rewrite_page_groups_to_cloudflare(merged, cf)
            from app.database.connection import client as mongo_client
            target_coll = mongo_client[target_db_name]["catalog_pages"]
            await target_coll.update_one(
                {"slug": page_slug},
                {"$set": {"groups": merged, "updated_at": datetime.datetime.now(datetime.timezone.utc)}},
            )
            _awaiting_addgroups.pop(user.id, None)
            total_btns = sum(len(g.get("buttons") or []) for g in added_groups)
            return await message.reply(
                f"✅ **Groups saved** for `/{page_slug}`\n\n"
                f"> Box: **{box.upper()}**\n"
                f"> Groups added: `{len(added_groups)}`\n"
                f"> Existing groups kept: `{len(existing)}`\n"
                f"> Buttons added: `{total_btns}`\n"
                f"> Duplicate groups skipped: `{len(new_groups) - len(added_groups)}`\n"
                f"> DB: `{target_db_name}`\n\n"
                f"They are now live on the page Watch/Download section."
            )

        match = re.search(r"https?://t\.me/(?:c/(\d+)/(\d+)|([A-Za-z0-9_]+)/(\d+))(?:\?.*)?$", raw)
        if not match:
            return
        if not _is_admin_user(user.id) and not await is_access_allowed(user.id):
            return await message.reply("🔒 This bot is restricted by the administrator.")
        user_data = await users_col.find_one({"user_id": user.id})
        if user_data and user_data.get("is_banned"):
            return await message.reply("🚫 Your account has been suspended.")
        if not _is_admin_user(user.id) and not await check_rate_limit(user.id):
            return await message.reply("⏳ Please wait a few seconds before requesting another link.")
        if not _is_admin_user(user.id) and not await is_user_fsubbed(client, user.id):
            invite_rows = await get_fsub_invite_buttons(client, user.id)
            body = await fsub_restriction_text(client, user.id)
            return await message.reply(
                body,
                reply_markup=_kb(invite_rows) if invite_rows else None,
            )
        runtime_cfg = await db.site_settings.find_one({"_id": "runtime"}) or {}
        downloads_enabled = runtime_cfg.get("downloads_enabled", True)
        streaming_enabled = runtime_cfg.get("streaming_enabled", True)
        fs_configured = bool((runtime_cfg.get("filestore_bot_username") or "").strip())
        if _is_admin_user(user.id):
            downloads_enabled = streaming_enabled = True
        if not downloads_enabled and not streaming_enabled and not fs_configured:
            return await message.reply("🚫 Downloads and streaming are currently disabled.")
        status = await message.reply("🔎 Checking Telegram message access…")
        try:
            if match.group(1):
                chat_ref = int("-100" + match.group(1))
                msg_id = int(match.group(2))
            else:
                chat_ref = "@" + match.group(3)
                msg_id = int(match.group(4))
            source_msg = await client.get_messages(chat_ref, msg_id)
            if isinstance(source_msg, list):
                source_msg = source_msg[0] if source_msg else None
            if not source_msg or not _is_media_message(source_msg):
                return await status.edit_text(
                    "❌ **No accessible media found.**\n\n"
                    "The message may be deleted, unsupported, or the bot may not have permission to view that channel."
                )

            media = (source_msg.document or source_msg.video or source_msg.audio or
                     source_msg.animation or source_msg.photo or source_msg.voice or
                     source_msg.video_note)
            if source_msg.document:
                file_name = media.file_name or "file"
                mime_type = media.mime_type or mimetypes.guess_type(file_name)[0] or "application/octet-stream"
            elif source_msg.video:
                file_name = getattr(media, "file_name", None) or f"video_{media.file_unique_id}.mp4"
                mime_type = media.mime_type or "video/mp4"
            elif source_msg.audio:
                file_name = media.file_name or f"audio_{media.file_unique_id}.mp3"
                mime_type = media.mime_type or "audio/mpeg"
            elif source_msg.animation:
                file_name = getattr(media, "file_name", None) or f"animation_{media.file_unique_id}.mp4"
                mime_type = media.mime_type or "video/mp4"
            elif source_msg.photo:
                media = source_msg.photo[-1] if isinstance(source_msg.photo, (list, tuple)) else source_msg.photo
                file_name, mime_type = f"photo_{media.file_unique_id}.jpg", "image/jpeg"
            elif source_msg.voice:
                file_name, mime_type = f"voice_{media.file_unique_id}.ogg", media.mime_type or "audio/ogg"
            else:
                file_name, mime_type = f"video_note_{media.file_unique_id}.mp4", "video/mp4"

            file_id = getattr(media, "file_id", None)
            if not file_id:
                return await status.edit_text("❌ Telegram did not provide a downloadable file reference.")
            existing = await files_col.find_one({
                "chat_id": source_msg.chat.id,
                "message_id": source_msg.id,
                "file_id": file_id,
            })
            if existing:
                short_code = existing["short_code"]
            else:
                short_code = generate_short_code()
                file_meta = FileMetadata(
                    file_id=file_id,
                    file_unique_id=str(getattr(media, "file_unique_id", source_msg.id)),
                    filename=file_name,
                    mime_type=mime_type,
                    file_size=getattr(media, "file_size", 0) or 0,
                    uploader_id=user.id,
                    short_code=short_code,
                    chat_id=source_msg.chat.id,
                    message_id=source_msg.id,
                    expiry_time=None,
                    storage_chat_id=None,
                    storage_message_id=None,
                )
                await files_col.insert_one(file_meta.dict())

            log_cf = _extract_tunnel_url_from_logs()
            if log_cf:
                await apply_auto_cloudflare_url(log_cf, source="telegram-message-link")
            base_url = await get_watch_page_base_url()
            if not base_url:
                base_url = await get_active_cloudflare_url()
            if not base_url:
                try:
                    base_url = normalize_public_base_url(settings.BASE_URL or settings.WEBSITE_URL or "")
                except ValueError:
                    base_url = None
            if not base_url:
                return await status.edit_text("⚠️ Media access is confirmed, but the public link host is not configured yet. Set /baseurl and retry.")

            links, buttons = [], []
            fs_username = (runtime_cfg.get("filestore_bot_username") or "").strip().lstrip("@")
            if downloads_enabled:
                dl = f"{base_url}/dl/{short_code}"
                links.append(f'⬇️ <a href="{html.escape(dl, quote=True)}">Direct Download</a>')
                buttons.append(InlineKeyboardButton("⬇️ Download", url=dl))
            elif fs_username:
                dl = f"https://t.me/{fs_username}?start=file_{short_code}"
                links.append(f'📥 <a href="{html.escape(dl, quote=True)}">Get File in Bot</a>')
                buttons.append(InlineKeyboardButton("📥 Get File in Bot", url=dl))
            if streaming_enabled:
                watch = f"{base_url}/watch/{short_code}"
                links.append(f'▶️ <a href="{html.escape(watch, quote=True)}">Watch / Stream</a>')
                buttons.append(InlineKeyboardButton("▶️ Watch Online", url=watch))
            size = format_bytes(getattr(media, "file_size", 0) or 0)
            caption = (
                "✅ <b>Telegram message verified</b>\n\n"
                f"📁 <b>File:</b> <code>{html.escape(file_name)}</code>\n"
                f"📦 <b>Size:</b> <code>{html.escape(size)}</code>\n\n"
                "🔗 <b>Generated links</b>\n" + "\n".join(links) +
                "\n\n<i>Source: original Telegram message. No storage-channel copy was created.</i>"
            )
            await status.edit_text(caption, parse_mode=ParseMode.HTML,
                reply_markup=_kb([buttons]) if buttons else None,
                disable_web_page_preview=True)
        except Exception as exc:
            logger.info("Telegram source link could not be accessed for user %s: %s", user.id, exc)
            reason = str(exc).lower()
            if any(term in reason for term in ("peer_id_invalid", "chat not found", "channel_private", "user_not_participant", "private")):
                detail = "The bot cannot access this private channel/chat. Add the bot to the channel with permission to read messages, then retry."
            else:
                detail = "Telegram could not return that message to this bot. Check that the link is correct and the bot can access the channel."
            try:
                await status.edit_text("❌ **Unable to access the Telegram message.**\n\n" + detail)
            except Exception:
                pass


    @bot.on_callback_query(filters.regex(r"^del_"))
    async def delete_callback(client, callback: CallbackQuery):
        user = callback.from_user
        if not user or user.is_bot:
            return
        if not await is_user_fsubbed(client, user.id):
            return await callback.answer("🔒 Join the required channel(s) first, then try again.", show_alert=True)

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
        if not user or user.is_bot or not _is_admin_user(user.id):
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
        if not user or not _is_admin_user(user.id):
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
        if not user or not _is_admin_user(user.id):
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
        if not user or not _is_admin_user(user.id):
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

    
    @bot.on_message(filters.command("panel") & filters.private)
    async def panel_command_handler(client, message: Message):
        """Website control panel + quick FileStore panel link (admins)."""
        user = message.from_user
        if not user or user.is_bot:
            return
        await refresh_runtime_admin_cache()
        if not _is_admin_user(user.id):
            return await message.reply("🚫 Admins only.")
        try:
            web_base = (await get_active_website_url()) or settings.BASE_URL
            base = str(web_base or "").rstrip("/") or "https://example.com"
        except Exception:
            base = str(getattr(settings, "BASE_URL", "") or "").rstrip("/") or "https://example.com"
        current_cf = (await get_active_cloudflare_url()) or "Auto-detecting..."
        try:
            vercel_doc = await settings_col.find_one({"key": "vercel_url"}) or {}
            vercel_url = vercel_doc.get("value") or "Detected on first Vercel visit"
        except Exception:
            vercel_url = "—"
        runtime = await db.site_settings.find_one({"_id": "runtime"}) or {}
        fs_user = (runtime.get("filestore_bot_username") or "").strip().lstrip("@") or "*(not set)*"
        await message.reply(
            "🔐 **HITLER YT · Control Panel**\n\n"
            f"> ☁️ ngrok media URL: `{current_cf}`\n"
            f"> ▲ Vercel website: `{vercel_url}`\n"
            f"> 📦 FileStore bot: `@{fs_user}`\n"
            f"> 🤖 Clients: `{len(session_manager.get_all_clients())}`\n\n"
            "**Commands**\n"
            "• `/fspanel` — FileStore admins, FSub channels, auto-delete\n"
            "• `/filestore @Bot` — set FileStore bot\n"
            "• `/addbots` — helper bot tokens\n"
            "• `/username` / `/password` — panel login\n\n"
            "Open the website dashboard for pages, groups, and buttons.",
            reply_markup=_kb([
                [InlineKeyboardButton("🛠️ Open Website Panel", url=f"{base}/panel")],
                [InlineKeyboardButton("📦 FileStore /fspanel", callback_data="fsp:open")],
            ]),
        )


    @bot.on_message(filters.command(["categories", "edit", "manage"]))
    async def cms_admin_commands(client, message: Message):
        user = message.from_user
        if not user or not _is_admin_user(user.id):
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
            f"> ☁️ **Auto ngrok Stream/Download/Button URL:** `{current_cf}`\n"
            f"> ▲ **Vercel Website URL (Website Only):** `{vercel_url}`\n"
            f"> 🤖 **Active Booster Clients:** `{len(session_manager.get_all_clients())}` (use `/addbots` to add more)\n\n"
            f"**Registered Vercel website domains**\n{website_lines}\n\n"
            "**Panel login management**\n"
            "• `/username new_username` — change the website panel username\n"
            "• `/password new_password` — change the website panel password (10+ characters)\n"
            "• `/addbots` — add unlimited bot tokens to boost download speeds\n\n"
            "The ngrok public URL is auto-detected and automatically replaces all button URL domains and generates all download & stream links, while Vercel serves the website.",
            reply_markup=_kb([[InlineKeyboardButton("Open HITLER YT Website Panel", url=f"{base}/panel")]]),
        )

    @bot.on_message(filters.command("backendurl"))
    async def backendurl_handler(client, message: Message):
        user = message.from_user
        if not user or not _is_admin_user(user.id):
            return await message.reply("🚫 **Unauthorized Access**")
        args = (message.text or "").split(maxsplit=1)
        current = (await get_active_cloudflare_url()) or settings.BACKEND_URL or "Auto-detecting"
        if len(args) < 2:
            return await message.reply(
                "☁️ **ngrok Tunnel URL**\n\n"
                f"> Current Auto ngrok URL: `{current}`\n\n"
                "Usage: `/backendurl https://your-domain.ngrok-free.app`\n"
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
                "✅ **ngrok URL Saved & Synced!**\n\n"
                f"> ☁️ **ngrok URL:** `{candidate}`\n"
                f"> 🔄 **Catalog Pages Rewritten:** `{rewritten}`\n\n"
                "All download links (`/dl/`), stream links (`/watch/`, `/stream/`), and website button URLs now use this ngrok URL."
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
        if not _is_admin_user(user.id):
            return await message.reply(
                "🚫 **Unauthorized Access**\n\n"
                "> Only authorized bot administrators can update the Base URL."
            )
        args = (message.text or "").split(maxsplit=1)
        if len(args) < 2:
            current_cf = (await get_active_cloudflare_url()) or settings.BASE_URL
            return await message.reply(
                "🌐 **ngrok / Base URL Management**\n\n"
                f"> 🔗 **Current Active ngrok URL:** `{current_cf}`\n"
                f"> 📢 **Updates Channel:** `{settings.UPDATE_CHANNEL_URL}`\n\n"
                "*(ngrok URL is set automatically on startup, or override with `/baseurl https://your-ngrok-domain.ngrok-free.app`)*"
            )
        new_url = args[1].strip()
        try:
            new_url = normalize_public_base_url(new_url)
        except ValueError as exc:
            return await message.reply(
                "❌ **Invalid public Base URL**\n\n"
                f"> {html.escape(str(exc))}\n\n"
                "Use your public URL, for example: `/baseurl https://your-ngrok-domain.ngrok-free.app`"
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
                "✨ *All download links, stream links, and website button URLs now use this ngrok URL.*"
            )
        except Exception as e:
            logger.error("Error updating baseurl: %s", e)
            await message.reply(f"❌ **Error updating Base URL:** `{e}`")

    @bot.on_message(filters.command("filestore") & filters.private)
    async def filestore_handler(client, message: Message):
        """Configure FileStore bot for deep-link file delivery.

        Usage:
          /filestore                        → show current setting
          /filestore @BotUsername           → set by username
          /filestore BotUsername            → same (without @)
          /filestore <bot_token>            → set by token, resolve username, start that bot
          /filestore off                    → clear / disable
        """
        user = message.from_user
        if not user or user.is_bot:
            return
        if not _is_admin_user(user.id):
            return await message.reply(
                "🚫 **Unauthorized Access**\n\n"
                "> Only authorized bot administrators can configure FileStore."
            )

        args = (message.text or "").split(maxsplit=1)
        runtime = await db.site_settings.find_one({"_id": "runtime"}) or {}
        current = (runtime.get("filestore_bot_username") or "").strip().lstrip("@")
        has_token = bool((runtime.get("filestore_bot_token") or "").strip())

        if len(args) < 2:
            status = f"@{current}" if current else "*(not set)*"
            token_note = " · token stored & bot started" if has_token else ""
            return await message.reply(
                "📦 **FileStore Bot**\n\n"
                f"> Current: `{status}`{token_note}\n\n"
                "Deep links look like:\n"
                f"`https://t.me/{current or 'YourBot'}?start=file_<code>`\n\n"
                "When a user opens that link, the FileStore bot sends the file in chat "
                "with **Watch** + **Download** buttons, then **auto-deletes after 5 minutes**.\n\n"
                "On title pages that have a **Watch** box, a matching **Telegram** box is "
                "generated automatically — same group/button names, but URLs become deep links "
                "(only for buttons whose URLs are this bot’s `/watch/` links).\n\n"
                "**Commands**\n"
                "• `/filestore @YourBotUsername` — set by username\n"
                "• `/filestore <bot_token>` — set by token, resolve username, **start** that bot\n"
                "• `/filestore off` — clear setting and stop secondary bot\n\n"
                "Tip: you can use the *same* bot (this one). Deep links with "
                "`/start file_<short_code>` are handled automatically."
            )

        value = args[1].strip()
        if value.lower() in ("off", "none", "disable", "clear", "0"):
            await stop_filestore_client()
            await db.site_settings.update_one(
                {"_id": "runtime"},
                {"$unset": {"filestore_bot_username": "", "filestore_bot_token": ""},
                 "$set": {"updated_at": datetime.datetime.utcnow()}},
                upsert=True,
            )
            return await message.reply("✅ FileStore cleared. Deep-links disabled; secondary bot stopped.")

        username = value
        token = None
        started_note = ""

        # Bot token form: 123456:ABC...
        if ":" in value and value.split(":", 1)[0].isdigit() and re.fullmatch(
            r"\d{6,16}:[A-Za-z0-9_-]{30,60}", value.strip()
        ):
            token = value.strip()
            status_msg = await message.reply("⏳ Resolving bot token and starting FileStore bot…")
            try:
                resolved = await resolve_bot_username_from_token(token)
                if not resolved:
                    # Fall back: if token is primary bot, use primary username
                    if token == (settings.BOT_TOKEN or "").strip():
                        me = await client.get_me()
                        resolved = (getattr(me, "username", None) or current or "").strip()
                if not resolved:
                    return await status_msg.edit_text(
                        "❌ Could not resolve a public username from that token.\n"
                        "Make sure the bot has a @username set in @BotFather.\n"
                        "Or use: `/filestore @YourBotUsername`"
                    )
                username = resolved
                start_info = await start_filestore_client(token, username=username)
                if start_info.get("same_as_primary"):
                    started_note = "\n> Using **this bot** (primary) for deep-link delivery."
                elif start_info.get("already_running"):
                    started_note = f"\n> Secondary FileStore bot **@{(start_info.get('username') or username)}** already running."
                else:
                    started_note = f"\n> Secondary FileStore bot **@{username}** started and listening for deep links."
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            except Exception as e:
                logger.exception("FileStore token setup failed: %s", e)
                return await status_msg.edit_text(
                    f"❌ Failed to start FileStore bot from token:\n`{html.escape(str(e))}`"
                )
        else:
            username = username.lstrip("@").strip()

        username = username.lstrip("@").strip()
        if not username or not re.match(r"^[A-Za-z0-9_]{5,32}$", username):
            return await message.reply(
                "❌ Invalid bot username.\n"
                "Use: `/filestore @YourBotUsername`\n"
                "Or: `/filestore <bot_token>`"
            )

        update = {
            "filestore_bot_username": username,
            "updated_at": datetime.datetime.utcnow(),
        }
        if token:
            update["filestore_bot_token"] = token
        await db.site_settings.update_one(
            {"_id": "runtime"},
            {"$set": update},
            upsert=True,
        )
        await message.reply(
            "✅ **FileStore saved**\n\n"
            f"> Bot: `@{username}`{started_note}\n\n"
            "Deep links:\n"
            f"`https://t.me/{username}?start=file_<short_code>`\n\n"
            "• Title pages with a **Watch** box get a matching **Telegram** box "
            "(same group/button names; only buttons whose URLs are this bot’s `/watch/` links).\n"
            "• Delivered files include **Watch** + **Download** buttons and **auto-delete in 5 minutes**."
        )


    async def _fspanel_home_text() -> tuple[str, InlineKeyboardMarkup]:
        runtime = await db.site_settings.find_one({"_id": "runtime"}) or {}
        admins = await get_runtime_admins()
        fsubs = await get_runtime_fsub_ids()
        adel = await get_filestore_auto_delete_seconds()
        fs_user = (runtime.get("filestore_bot_username") or "").strip().lstrip("@") or "*(not set)*"
        if adel <= 0:
            adel_txt = "off"
        elif adel % 60 == 0:
            adel_txt = f"{adel // 60} min"
        else:
            adel_txt = f"{adel} sec"
        admin_lines = ", ".join(f"`{a}`" for a in admins) or "*(none)*"
        fsub_lines = ", ".join(f"`{c}`" for c in fsubs) or "*(none)*"
        text = (
            "🛠 **FileStore Panel** (admins only)\n\n"
            f"> 📦 FileStore bot: `@{fs_user}`\n"
            f"> ⏱ Auto-delete: `{adel_txt}`\n"
            f"> 👑 Admins: {admin_lines}\n"
            f"> 📢 FSub channels/groups: {fsub_lines}\n\n"
            "Use the buttons below to manage settings."
        )
        kb = _kb([
            [InlineKeyboardButton("➕ Add Admin", callback_data="fsp:addadmin"),
             InlineKeyboardButton("➖ Remove Admin", callback_data="fsp:deladmin")],
            [InlineKeyboardButton("🔄 Replace Admins", callback_data="fsp:setadmins")],
            [InlineKeyboardButton("➕ Add FSub", callback_data="fsp:addfsub"),
             InlineKeyboardButton("➖ Remove FSub", callback_data="fsp:delfsub")],
            [InlineKeyboardButton("🔄 Replace FSub list", callback_data="fsp:setfsub")],
            [InlineKeyboardButton("⏱ Auto-delete time", callback_data="fsp:autodel")],
            [InlineKeyboardButton("🔄 Refresh", callback_data="fsp:refresh")],
        ])
        return text, kb


    @bot.on_callback_query(filters.regex(r"^fsub:check$"))
    async def fsub_try_again_callback(client, callback: CallbackQuery):
        """Try Again after joining force-sub channels."""
        user = callback.from_user
        if not user or user.is_bot:
            return
        ok = await is_user_fsubbed(client, user.id)
        if ok:
            try:
                await callback.answer("✅ Verified! You can use the bot now.", show_alert=True)
            except Exception:
                pass
            try:
                await callback.message.edit_text(
                    "✅ **Access granted**\n\n"
                    "> You joined all required channels.\n"
                    "> Send /start or open your download link again."
                )
            except Exception:
                await callback.message.reply(
                    "✅ **Access granted**\n\n"
                    "> You joined all required channels.\n"
                    "> Send /start or open your download link again."
                )
            return
        invite_rows = await get_fsub_invite_buttons(client, user.id)
        body = await fsub_restriction_text(client, user.id)
        try:
            await callback.answer("Still missing a channel — join all, then Try Again.", show_alert=True)
        except Exception:
            pass
        try:
            await callback.message.edit_text(
                body,
                reply_markup=_kb(invite_rows) if invite_rows else None,
            )
        except Exception:
            await callback.message.reply(
                body,
                reply_markup=_kb(invite_rows) if invite_rows else None,
            )

    @bot.on_message(filters.command("fspanel") & filters.private)
    async def fspanel_handler(client, message: Message):
        """FileStore management panel: admins, fsub, auto-delete (admin only)."""
        user = message.from_user
        if not user or user.is_bot:
            return
        await refresh_runtime_admin_cache()
        if not _is_admin_user(user.id):
            return await message.reply("🚫 Admins only.")
        text, kb = await _fspanel_home_text()
        await message.reply(text, reply_markup=kb)

    @bot.on_callback_query(filters.regex(r"^fsp:"))
    async def fspanel_callback(client, callback: CallbackQuery):
        user = callback.from_user
        if not user:
            return
        await refresh_runtime_admin_cache()
        if not _is_admin_user(user.id):
            return await callback.answer("Admins only", show_alert=True)
        data = (callback.data or "").split(":", 1)[-1]
        if data in ("refresh", "open"):
            text, kb = await _fspanel_home_text()
            try:
                if data == "open":
                    await callback.message.reply(text, reply_markup=kb)
                else:
                    await callback.message.edit_text(text, reply_markup=kb)
            except Exception:
                await callback.message.reply(text, reply_markup=kb)
            return await callback.answer("FileStore panel" if data == "open" else "Refreshed")
        prompts = {
            "addadmin": ("Send the **user ID** to add as admin (numbers only).", "addadmin"),
            "deladmin": ("Send the **user ID** to remove from admins.", "deladmin"),
            "setadmins": ("Send admin user IDs separated by comma or space (replaces runtime list).\nOwner is always kept.", "setadmins"),
            "addfsub": ("Send the **channel/group ID** to add for force-subscribe\n(e.g. `-1001234567890`). Bot must be admin there.", "addfsub"),
            "delfsub": ("Send the **channel/group ID** to remove from FSub.", "delfsub"),
            "setfsub": ("Send channel/group IDs separated by comma or space (replaces runtime FSub list).", "setfsub"),
            "autodel": ("Send auto-delete time for FileStore deliveries:\n`5m` · `10m` · `1h` · `300` (seconds) · `off`", "autodel"),
        }
        if data not in prompts:
            return await callback.answer("Unknown action", show_alert=True)
        prompt, state = prompts[data]
        _awaiting_fspanel[user.id] = state
        await callback.answer()
        await callback.message.reply(f"✏️ {prompt}\n\nSend /cancel to abort.")

    # IMPORTANT: exclude every command that has its own handler registered later.
    # Overlapping filters on the same group mean only the first matching handler runs
    # (see comment near telegram_message_link_handler). Without excluding "batch" etc.,
    # those commands were swallowed here with a silent return.
    @bot.on_message(
        filters.private
        & filters.text
        & ~filters.command([
            "start", "cancel", "fspanel", "panel", "filestore",
            "batch", "ss", "sv", "mi", "stats", "broadcast", "ban", "unban",
            "autodel", "del", "addgroups", "save", "link", "addbots", "shorteners",
            "search", "username", "password", "backendurl", "baseurl",
            "categories", "edit", "manage",
        ])
    )
    async def fspanel_text_input(client, message: Message):
        user = message.from_user
        if not user or user.id not in _awaiting_fspanel:
            return
        if not _is_admin_user(user.id):
            _awaiting_fspanel.pop(user.id, None)
            return
        state = _awaiting_fspanel.pop(user.id, None)
        raw = (message.text or "").strip()
        if not raw or raw.lower() in ("/cancel", "cancel"):
            return await message.reply("Cancelled.")

        runtime = await db.site_settings.find_one({"_id": "runtime"}) or {}
        now = datetime.datetime.utcnow()

        def parse_ids(s: str) -> list[int]:
            out = []
            for part in re.split(r"[\s,;]+", s.strip()):
                part = part.strip()
                if not part:
                    continue
                try:
                    out.append(int(part))
                except ValueError:
                    continue
            return out

        if state == "addadmin":
            ids = parse_ids(raw)
            if not ids:
                return await message.reply("❌ Send a valid numeric user ID.")
            cur = list(runtime.get("runtime_admins") or [])
            for i in ids:
                if i not in cur:
                    cur.append(i)
            await db.site_settings.update_one(
                {"_id": "runtime"},
                {"$set": {"runtime_admins": cur, "updated_at": now}},
                upsert=True,
            )
            await refresh_runtime_admin_cache()
            return await message.reply(f"✅ Added admin(s): {', '.join(str(i) for i in ids)}")

        if state == "deladmin":
            ids = set(parse_ids(raw))
            if not ids:
                return await message.reply("❌ Send a valid numeric user ID.")
            if settings.OWNER_ID in ids:
                return await message.reply("❌ Cannot remove the owner.")
            cur = [i for i in (runtime.get("runtime_admins") or []) if int(i) not in ids]
            await db.site_settings.update_one(
                {"_id": "runtime"},
                {"$set": {"runtime_admins": cur, "updated_at": now}},
                upsert=True,
            )
            await refresh_runtime_admin_cache()
            return await message.reply(f"✅ Removed admin(s): {', '.join(str(i) for i in ids)}")

        if state == "setadmins":
            ids = parse_ids(raw)
            # keep owner always
            cleaned = []
            seen = set()
            for i in ids:
                if i == settings.OWNER_ID:
                    continue
                if i not in seen:
                    cleaned.append(i)
                    seen.add(i)
            await db.site_settings.update_one(
                {"_id": "runtime"},
                {"$set": {"runtime_admins": cleaned, "updated_at": now}},
                upsert=True,
            )
            await refresh_runtime_admin_cache()
            return await message.reply(f"✅ Runtime admins replaced ({len(cleaned)} + owner).")

        if state == "addfsub":
            ids = parse_ids(raw)
            if not ids:
                return await message.reply("❌ Send a valid channel/group ID (e.g. `-1001234567890`).")
            cur = list(runtime.get("runtime_fsub") or [])
            for i in ids:
                if i not in cur:
                    cur.append(i)
            await db.site_settings.update_one(
                {"_id": "runtime"},
                {"$set": {"runtime_fsub": cur, "updated_at": now}},
                upsert=True,
            )
            return await message.reply(
                f"✅ FSub added: {', '.join(str(i) for i in ids)}\n"
                "Make sure the bot is **admin** in those chats and can invite users."
            )

        if state == "delfsub":
            ids = set(parse_ids(raw))
            if not ids:
                return await message.reply("❌ Send a valid channel/group ID.")
            cur = [i for i in (runtime.get("runtime_fsub") or []) if int(i) not in ids]
            await db.site_settings.update_one(
                {"_id": "runtime"},
                {"$set": {"runtime_fsub": cur, "updated_at": now}},
                upsert=True,
            )
            return await message.reply(f"✅ FSub removed: {', '.join(str(i) for i in ids)}")

        if state == "setfsub":
            ids = parse_ids(raw)
            cleaned = []
            seen = set()
            for i in ids:
                if i not in seen:
                    cleaned.append(i)
                    seen.add(i)
            await db.site_settings.update_one(
                {"_id": "runtime"},
                {"$set": {"runtime_fsub": cleaned, "updated_at": now}},
                upsert=True,
            )
            return await message.reply(f"✅ FSub list replaced ({len(cleaned)} chats).")

        if state == "autodel":
            v = raw.lower().strip()
            if v in ("off", "0", "disable", "none"):
                seconds = 0
            else:
                m = re.match(r"^(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours)?$", v)
                if not m:
                    return await message.reply("❌ Invalid time. Examples: `5m`, `10m`, `1h`, `300`, `off`")
                num = int(m.group(1))
                unit = (m.group(2) or "s").lower()
                if unit.startswith("h"):
                    seconds = num * 3600
                elif unit.startswith("m"):
                    seconds = num * 60
                else:
                    seconds = num
            await db.site_settings.update_one(
                {"_id": "runtime"},
                {"$set": {"filestore_auto_delete_seconds": seconds, "updated_at": now}},
                upsert=True,
            )
            if seconds <= 0:
                return await message.reply("✅ FileStore auto-delete **disabled**.")
            return await message.reply(f"✅ FileStore auto-delete set to **{seconds} seconds**.")

    
    @bot.on_message(filters.command("stats"))
    async def stats_handler(client, message: Message):
        user = message.from_user
        if not user or user.is_bot or not _is_admin_user(user.id):
            return
        total_files = await files_col.count_documents({})
        total_users = await users_col.count_documents({})
        await message.reply(
            f"📊 **System Statistics**\n\n"
            f"> 👥 **Total Users:** `{total_users}`\n"
            f"> 📁 **Total Files Generated:** `{total_files}`\n"
            f"> 🤖 **Active Booster Clients:** `{len(session_manager.get_all_clients())}` (`{len(session_manager.helper_bots)}` helper bots)\n"
            f"> ⚡ **System Status:** `Online & High-Speed`\n"
            f"> 🔧 **Engine:** `wzgram[fast]`"
        )

    @bot.on_message(filters.command("broadcast"))
    async def broadcast_handler(client, message: Message):
        user = message.from_user
        if not user or user.is_bot or not _is_admin_user(user.id):
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
        if not user or user.is_bot or not _is_admin_user(user.id):
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
        if not user or user.is_bot or not _is_admin_user(user.id):
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
        if not user or user.is_bot or not _is_admin_user(user.id):
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


    @bot.on_message(filters.command("del") & filters.private)
    async def delete_catalog_page_command(client, message: Message):
        user = message.from_user
        if not user or not _is_admin_user(user.id):
            return await message.reply("🚫 Admins only.")

        args = (message.text or "").split(maxsplit=1)
        target = args[1].strip() if len(args) > 1 else ""
        if not target:
            return await message.reply(
                "🗑️ **Delete website page**\n\n"
                "Usage:\n"
                "• `/del https://your-site/title/page-slug`\n"
                "• `/del page-slug`\n"
                "• `/del Exact Page Title`\n\n"
                "The matching page is deleted immediately, without a confirmation step. "
                "Only an exact title or slug match is accepted."
            )

        # Accept a title slug, /title/slug URL, or exact page title. Never use fuzzy
        # matching for destructive actions; ambiguous or partial names are rejected.
        slug = None
        url_match = re.search(r"/title/([A-Za-z0-9][A-Za-z0-9_-]{0,500})", target, re.I)
        if url_match:
            slug = url_match.group(1).lower()
        else:
            candidate = target.strip().strip("/").split("?", 1)[0].split("#", 1)[0]
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,500}", candidate):
                slug = candidate.lower()

        page = None
        if slug:
            page = await db.catalog_pages.find_one({"slug": slug})
        if page is None:
            # Exact, case-insensitive title lookup (escaped to prevent regex injection).
            safe_title = re.escape(target[:180])
            matches = await db.catalog_pages.find(
                {"title": {"$regex": f"^{safe_title}$", "$options": "i"}},
                {"_id": 0, "slug": 1, "title": 1},
            ).to_list(3)
            if len(matches) == 1:
                page = matches[0]
            elif len(matches) > 1:
                return await message.reply(
                    "⚠️ More than one page matches that title. Use its exact page URL or slug instead."
                )

        if not page:
            return await message.reply(
                "❌ Page not found. Nothing was deleted. Use the exact page title, slug, or `/title/` URL."
            )

        page_slug = page.get("slug")
        page_title = page.get("title") or page_slug or "Untitled"
        if not page_slug:
            return await message.reply("❌ This page record has no slug. Nothing was deleted.")

        result = await db.catalog_pages.delete_one({"slug": page_slug})
        if not result.deleted_count:
            return await message.reply("❌ Page was not deleted (it may have already been removed).")

        await message.reply(
            "🗑️ **Page deleted successfully**\n\n"
            f"**Title:** {page_title}\n"
            f"**Slug:** `{page_slug}`\n\n"
            "The page is now removed from the catalog database and will no longer appear in public catalog queries."
        )

    @bot.on_message(filters.command("addgroups") & filters.private)
    async def addgroups_command(client, message: Message):
        user = message.from_user
        if not user or not _is_admin_user(user.id):
            return await message.reply("🚫 Admins only.")
        reply = message.reply_to_message
        source_text = ""
        if reply:
            source_text = (getattr(reply, "text", None) or getattr(reply, "caption", None) or "")
            # Prefer href from entities if the link is hyperlinked
            for ent in (getattr(reply, "entities", None) or getattr(reply, "caption_entities", None) or []):
                url = getattr(ent, "url", None)
                if url and "/title/" in url:
                    source_text = f"{source_text} {url}"
        # Keep full argument text — do not truncate long slugs/URLs
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) > 1:
            source_text = (source_text + " " + parts[1]).strip()

        page_slug = None
        # Support arbitrarily long slugs (letters, digits, hyphen, underscore)
        match = re.search(r"/title/([A-Za-z0-9][A-Za-z0-9_-]{0,500})", source_text or "", re.I)
        if match:
            page_slug = match.group(1).lower().rstrip("/")
        elif parts and len(parts) > 1:
            # Bare slug or full URL without needing /title/ only once
            bare = parts[1].strip()
            # If it's a full URL, extract last path segment
            url_m = re.search(r"https?://[^\s]+/title/([A-Za-z0-9][A-Za-z0-9_-]{0,500})", bare, re.I)
            if url_m:
                page_slug = url_m.group(1).lower().rstrip("/")
            else:
                bare = bare.strip("/").split("?")[0].split("#")[0]
                # Allow very long slug names
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,500}", bare):
                    page_slug = bare.lower()

        if not page_slug:
            suggestions = await catalog_page_suggestions("", limit=8)
            tip = "\n".join(suggestions) if suggestions else "_No pages in database yet. Create one with /search or the website panel._"
            return await message.reply(
                "📌 **Usage**\n"
                "• Reply to a `/title/...` page link with `/addgroups`\n"
                "• Or: `/addgroups https://yoursite.vercel.app/title/very-long-slug-name-here`\n"
                "• Or: `/addgroups very-long-slug-name-here`\n\n"
                "Long slug names are fully supported.\n\n"
                f"**Recent pages:**\n{tip}"
            )

        page = await find_catalog_page(page_slug)
        if not page:
            suggestions = await catalog_page_suggestions(page_slug, limit=8)
            tip = "\n".join(suggestions) if suggestions else (
                "_No matching pages. Check `DATABASE_NAME` matches the Vercel MongoDB "
                "(often `tg_media_bot` or `animovieszoneflix`), and that the page was published._"
            )
            return await message.reply(
                f"❌ Catalog page `{page_slug}` not found in the bot database.\n\n"
                f"**Closest matches:**\n{tip}\n\n"
                "If the website shows this page but the bot cannot, the bot and Vercel "
                "may be using different MongoDB database names."
            )

        page_slug = page.get("slug") or page_slug
        # Telegram callback_data max 64 bytes — never put long slugs in it.
        # Use a short token mapped to the full slug.
        token = secrets.token_hex(4)
        _addgroups_slug_tokens[token] = page_slug
        # Prevent unbounded growth
        if len(_addgroups_slug_tokens) > 500:
            for k in list(_addgroups_slug_tokens.keys())[:200]:
                _addgroups_slug_tokens.pop(k, None)

        kb = _kb([
            [
                InlineKeyboardButton("📥 Download", callback_data=f"addg:dl:{token}"),
                InlineKeyboardButton("▶️ Watch", callback_data=f"addg:wt:{token}"),
            ]
        ])
        title = page.get("title") or page_slug
        await message.reply(
            f"📁 **Add Groups** for **{html.escape(str(title))}**\n"
            f"`/title/{html.escape(page_slug)}`\n\n"
            f"Choose which section to configure:",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )

    @bot.on_callback_query(filters.regex(r"^addg:(dl|wt):"))
    async def addgroups_box_callback(client, callback: CallbackQuery):
        user = callback.from_user
        if not user or not _is_admin_user(user.id):
            return await callback.answer("Admins only", show_alert=True)
        parts = (callback.data or "").split(":")
        if len(parts) < 3:
            return await callback.answer("Invalid data", show_alert=True)
        box = "download" if parts[1] == "dl" else "watch"
        token_or_slug = parts[2]
        # Resolve short token → full slug (supports unlimited-length slugs)
        page_slug = _addgroups_slug_tokens.get(token_or_slug) or token_or_slug
        page = await find_catalog_page(page_slug)
        if not page:
            return await callback.answer("Page not found in bot DB", show_alert=True)
        page_slug = page.get("slug") or page_slug
        _awaiting_addgroups[user.id] = {"page_slug": page_slug, "box": box}
        # Escape for HTML; long names OK in message text (4096 limit)
        safe_slug = html.escape(page_slug)
        try:
            await callback.message.edit_text(
                f"✏️ <b>Configure {box.upper()} groups</b> for <code>{safe_slug}</code>\n\n"
                f"Send groups and buttons in this format (unlimited length):\n\n"
                f"<code>1. Group Name</code>\n"
                f"<code>Button Name: https://example.com/link</code>\n\n"
                f"<code>2. Another Group</code>\n"
                f"<code>Stream: https://...</code>\n"
                f"<code>Mirror: https://...</code>\n\n"
                f"Send everything in <b>one message</b>. After saving, buttons appear in the {box} section of the page.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await callback.message.reply(
                f"✏️ Send groups/buttons for <b>{box}</b> on <code>{safe_slug}</code> using the format above.",
                parse_mode=ParseMode.HTML,
            )
        await callback.answer()

    @bot.on_message(filters.command("save") & filters.private)
    async def save_command(client, message: Message):
        user = message.from_user
        if not user or not _is_admin_user(user.id):
            return await message.reply("🚫 Admins only.")
        kb = _kb([
            [
                InlineKeyboardButton("📦 Backup", callback_data="save:backup"),
                InlineKeyboardButton("♻️ Restore", callback_data="save:restore"),
            ]
        ])
        await message.reply(
            "💾 **Project Backup / Restore**\n\n"
            "• **Backup** — export databases, settings, catalog, files, groups & configs to `data.zip`\n"
            "• **Restore** — upload a previous `data.zip` to restore supported data\n\n"
            "Choose an action:",
            reply_markup=kb,
        )

    @bot.on_callback_query(filters.regex(r"^save:(backup|restore)$"))
    async def save_action_callback(client, callback: CallbackQuery):
        user = callback.from_user
        if not user or not _is_admin_user(user.id):
            return await callback.answer("Admins only", show_alert=True)
        action = (callback.data or "").split(":")[-1]
        if action == "restore":
            _awaiting_restore.add(user.id)
            try:
                await callback.message.edit_text(
                    "♻️ **Restore mode**\n\n"
                    "Upload the `data.zip` backup file now.\n"
                    "Supported: MongoDB collections export (files, users, catalog_pages, site_settings, access_keys, etc.)."
                )
            except Exception:
                await callback.message.reply("♻️ Upload the `data.zip` backup file now.")
            return await callback.answer()

        # Backup
        await callback.answer("Building backup…")
        status = None
        try:
            status = await callback.message.reply("📦 Collecting project data…")
            work = tempfile.mkdtemp(prefix="hyt_backup_")
            export_dir = os.path.join(work, "data")
            os.makedirs(export_dir, exist_ok=True)

            collections = [
                "files", "users", "catalog_pages", "catalog_categories",
                "site_settings", "access_keys", "cms_pending", "settings",
            ]
            meta = {"version": 1, "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "collections": []}
            for coll_name in collections:
                try:
                    coll = db[coll_name]
                    docs = []
                    async for doc in coll.find({}):
                        # Make JSON-serializable
                        d = {}
                        for k, v in doc.items():
                            if k == "_id":
                                d["_id"] = str(v)
                            elif isinstance(v, datetime.datetime):
                                d[k] = v.isoformat()
                            else:
                                try:
                                    json.dumps(v)
                                    d[k] = v
                                except (TypeError, ValueError):
                                    d[k] = str(v)
                        docs.append(d)
                    out_path = os.path.join(export_dir, f"{coll_name}.json")
                    with open(out_path, "w", encoding="utf-8") as f:
                        json.dump(docs, f, ensure_ascii=False, indent=2)
                    meta["collections"].append({"name": coll_name, "count": len(docs)})
                except Exception as e:
                    logger.warning("Backup collection %s failed: %s", coll_name, e)
                    meta["collections"].append({"name": coll_name, "error": str(e)})

            with open(os.path.join(export_dir, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)

            zip_path = os.path.join(work, "data.zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, _, files in os.walk(export_dir):
                    for name in files:
                        full = os.path.join(root, name)
                        arc = os.path.relpath(full, work)
                        zf.write(full, arcname=arc)

            await status.edit_text("📤 Uploading backup…")
            await client.send_document(
                chat_id=message.chat.id if False else callback.message.chat.id,
                document=zip_path,
                caption=(
                    "📦 **Project Backup (`data.zip`)**\n\n"
                    f"Collections: {', '.join(c['name'] for c in meta['collections'] if 'count' in c)}\n"
                    f"Exported: `{meta['exported_at']}`\n\n"
                    "Keep this file safe. Use /save → Restore to import it later."
                ),
            )
            try:
                await status.delete()
            except Exception:
                pass
        except Exception as e:
            logger.exception("Backup failed")
            try:
                if status:
                    await status.edit_text(f"❌ Backup failed: `{e}`")
                else:
                    await callback.message.reply(f"❌ Backup failed: `{e}`")
            except Exception:
                pass
        finally:
            try:
                shutil.rmtree(work, ignore_errors=True)
            except Exception:
                pass


    async def _handle_restore_zip(client, message: Message):
        """Restore MongoDB collections from an uploaded data.zip backup."""
        user = message.from_user
        if not user or not _is_admin_user(user.id):
            return
        doc = message.document
        if not doc:
            return await message.reply("❌ No document attached.")

        fname = (doc.file_name or "").lower()
        mime = (doc.mime_type or "").lower()
        if not (fname.endswith(".zip") or "zip" in mime):
            return await message.reply("Please upload a `.zip` backup file (`data.zip`).")

        _awaiting_restore.discard(user.id)
        status = await message.reply("♻️ Downloading backup…")
        work = None
        try:
            work = tempfile.mkdtemp(prefix="hyt_restore_")
            zip_path = os.path.join(work, "upload.zip")

            # Pyrogram/wzgram: download to an explicit path
            downloaded = await client.download_media(message, file_name=zip_path)
            if not downloaded:
                # Fallback: download into work dir
                downloaded = await client.download_media(message, file_name=work)
            if isinstance(downloaded, str) and downloaded and os.path.isfile(downloaded):
                zip_path = downloaded
            elif not os.path.isfile(zip_path):
                # Search for any downloaded zip in work
                found = None
                for root, _, files in os.walk(work):
                    for n in files:
                        if n.lower().endswith(".zip"):
                            found = os.path.join(root, n)
                            break
                    if found:
                        break
                if not found:
                    return await status.edit_text("❌ Failed to download the ZIP file. Try sending it again.")
                zip_path = found

            await status.edit_text("♻️ Extracting and validating backup…")
            extract_dir = os.path.join(work, "extracted")
            os.makedirs(extract_dir, exist_ok=True)
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    # Basic zip-slip protection
                    for info in zf.infolist():
                        dest = os.path.realpath(os.path.join(extract_dir, info.filename))
                        if not dest.startswith(os.path.realpath(extract_dir) + os.sep) and dest != os.path.realpath(extract_dir):
                            raise ValueError(f"Unsafe path in zip: {info.filename}")
                    zf.extractall(extract_dir)
            except zipfile.BadZipFile:
                return await status.edit_text("❌ Invalid ZIP file. Upload a valid `data.zip` backup.")

            # Collect all *.json collection dumps (data/ or nested)
            json_files: list[tuple[str, str]] = []  # (coll_name, path)
            for root, _, files in os.walk(extract_dir):
                for n in files:
                    if not n.endswith(".json") or n == "manifest.json":
                        continue
                    coll_name = n[:-5]
                    # skip junk names
                    if not coll_name or "/" in coll_name or "\\" in coll_name:
                        continue
                    json_files.append((coll_name, os.path.join(root, n)))

            if not json_files:
                return await status.edit_text(
                    "❌ No collection JSON files found in the ZIP.\n"
                    "Expected structure: `data/files.json`, `data/users.json`, …"
                )

            # Optional ObjectId support
            try:
                from bson import ObjectId  # type: ignore
            except Exception:
                ObjectId = None  # type: ignore

            def coerce_id(value):
                if value is None:
                    return None
                if ObjectId is not None and isinstance(value, str) and len(value) == 24:
                    try:
                        return ObjectId(value)
                    except Exception:
                        return value
                return value

            restored: list[str] = []
            total_docs = 0
            for coll_name, fpath in json_files:
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        docs = json.load(f)
                    if not isinstance(docs, list):
                        restored.append(f"{coll_name} (skipped: not a list)")
                        continue
                    coll = db[coll_name]
                    count = 0
                    for d in docs:
                        if not isinstance(d, dict):
                            continue
                        oid = d.get("_id")
                        payload = {k: v for k, v in d.items() if k != "_id"}
                        # Restore ISO datetime strings lightly for known fields
                        for dk, dv in list(payload.items()):
                            if isinstance(dv, str) and len(dv) >= 20 and "T" in dv and dv.endswith(("Z", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9")):
                                try:
                                    payload[dk] = datetime.datetime.fromisoformat(dv.replace("Z", "+00:00"))
                                except Exception:
                                    pass
                        if oid is not None:
                            cid = coerce_id(oid)
                            await coll.update_one({"_id": cid}, {"$set": payload}, upsert=True)
                            # Also try string _id if ObjectId path used
                            if cid is not oid:
                                try:
                                    await coll.update_one({"_id": oid}, {"$set": payload}, upsert=True)
                                except Exception:
                                    pass
                        else:
                            await coll.insert_one(payload)
                        count += 1
                    total_docs += count
                    restored.append(f"`{coll_name}` — {count} docs")
                except Exception as e:
                    logger.warning("Restore %s failed: %s", coll_name, e)
                    restored.append(f"`{coll_name}` — error: {e}")

            body = "✅ **Restore complete**\n\n" + "\n".join(f"• {r}" for r in restored)
            body += f"\n\n📦 Total documents upserted: `{total_docs}`"
            body += "\n\n_Restart the bot if settings (shorteners, URLs) do not apply immediately._"
            await status.edit_text(body)
        except Exception as e:
            logger.exception("Restore failed")
            try:
                await status.edit_text(f"❌ Restore failed: `{e}`")
            except Exception:
                await message.reply(f"❌ Restore failed: `{e}`")
        finally:
            if work:
                shutil.rmtree(work, ignore_errors=True)

    @bot.on_message(filters.document & filters.private)
    async def restore_zip_handler(client, message: Message):
        """Fallback handler if media_handler does not catch the ZIP."""
        user = message.from_user
        if not user or user.id not in _awaiting_restore or not _is_admin_user(user.id):
            return
        await _handle_restore_zip(client, message)

    @bot.on_message(filters.command("batch") & filters.private)
    async def batch_command(client, message: Message):
        """Generate download/watch links for a range of messages in any channel/group
        the bot can access. Does NOT require CHANNEL_ID / storage channel for generation.
        """
        user = message.from_user
        if not user or not _is_admin_user(user.id):
            return await message.reply("🚫 Admins only.")

        # Immediate ack so the user always sees the command was received
        status = await message.reply("⏳ Parsing batch range…")
        text = (message.text or message.caption or "").strip()

        try:
            # --- parse links / range -------------------------------------------------
            link_re = re.compile(
                r"https?://t\.me/(?:c/(\d+)/(\d+)|([A-Za-z0-9_]+)/(\d+))(?:\?[^\s]*)?",
                re.I,
            )
            range_re = re.compile(
                r"https?://t\.me/(?:c/(\d+)/(\d+)|([A-Za-z0-9_]+)/(\d+))(?:\?[^\s\-]*)?"
                r"\s*[-–]\s*"
                r"(?:https?://t\.me/(?:c/(\d+)/(\d+)|([A-Za-z0-9_]+)/(\d+))(?:\?[^\s]*)?|(\d+))",
                re.I,
            )

            chat_ref = None  # str "@name" or int -100...
            start_id = end_id = None

            range_m = range_re.search(text)
            if range_m:
                if range_m.group(1):
                    chat_ref = int("-100" + range_m.group(1))
                    start_id = int(range_m.group(2))
                else:
                    chat_ref = "@" + range_m.group(3)
                    start_id = int(range_m.group(4))
                if range_m.group(5):
                    end_chat = int("-100" + range_m.group(5))
                    end_id = int(range_m.group(6))
                    if end_chat != chat_ref:
                        return await status.edit_text("❌ Start and end must be in the same channel.")
                elif range_m.group(7):
                    end_chat_name = range_m.group(7)
                    end_id = int(range_m.group(8))
                    if ("@" + end_chat_name) != chat_ref and end_chat_name != str(chat_ref).lstrip("@"):
                        return await status.edit_text("❌ Start and end must be in the same channel.")
                else:
                    end_id = int(range_m.group(9))
            else:
                matches = list(link_re.finditer(text))
                if len(matches) >= 2:
                    def _parse_link(m):
                        if m.group(1):
                            return int("-100" + m.group(1)), int(m.group(2))
                        return "@" + m.group(3), int(m.group(4))

                    chat1, id1 = _parse_link(matches[0])
                    chat2, id2 = _parse_link(matches[-1])
                    if chat1 != chat2:
                        return await status.edit_text("❌ Start and end must be in the same channel.")
                    chat_ref, start_id, end_id = chat1, id1, id2
                elif len(matches) == 1:
                    m = matches[0]
                    if m.group(1):
                        chat_ref = int("-100" + m.group(1))
                        start_id = int(m.group(2))
                    else:
                        chat_ref = "@" + m.group(3)
                        start_id = int(m.group(4))
                    bare = re.search(r"(\d+)\s*[-–]\s*(\d+)", text)
                    if bare:
                        a, b = int(bare.group(1)), int(bare.group(2))
                        start_id, end_id = min(a, b), max(a, b)

            if chat_ref is None or start_id is None or end_id is None:
                return await status.edit_text(
                    "📌 <b>Usage</b>\n\n"
                    "<code>/batch https://t.me/username/14816-https://t.me/username/14851</code>\n\n"
                    "Or compact:\n"
                    "<code>/batch https://t.me/c/1234567890/2081-2083 The Ramparts of Ice</code>\n\n"
                    "• Add an optional filename filter after the range (case-insensitive)\n"
                    "• Works with public (@username) and private (c/ID) links\n"
                    "• No hard-coded range cap; large ranges are fetched in chunks\n"
                    "• Bot only needs to be a member/admin of the source channel\n"
                    "• <b>No storage channel (CHANNEL_ID) required</b> for generating links\n"
                    "• Query params like <code>?single</code> are fine",
                    parse_mode=ParseMode.HTML,
                )

            if end_id < start_id:
                start_id, end_id = end_id, start_id

            # --- resolve peer so get_messages is reliable for public channels ------
            try:
                chat_obj = await client.get_chat(chat_ref)
                resolved_chat_id = chat_obj.id
                chat_title = getattr(chat_obj, "title", None) or getattr(chat_obj, "username", None) or str(resolved_chat_id)
            except Exception as e:
                return await status.edit_text(
                    f"❌ Cannot access chat <code>{html.escape(str(chat_ref))}</code>\n\n"
                    f"<code>{html.escape(type(e).__name__)}: {html.escape(str(e)[:200])}</code>\n\n"
                    "Add the bot as a member (or admin) of that channel/group and try again.\n"
                    "No storage channel is required — only access to the source chat.",
                    parse_mode=ParseMode.HTML,
                )

            # Everything after the message range is an optional filename/title filter.
            # Remove command syntax and Telegram URLs while preserving the user's words.
            filter_text = re.sub(r"^\s*/batch\b", "", text, flags=re.I)
            filter_text = re.sub(r"https?://t\.me/(?:c/\d+|[A-Za-z0-9_]+)/\d+(?:\?[^\s]*)?", " ", filter_text, flags=re.I)
            # Also strip the compact second endpoint in /batch <link>-<message_id>.
            filter_text = re.sub(r"(?<!\w)[-–]\s*\d+\b", " ", filter_text, count=1)
            filter_text = re.sub(r"\b\d+\s*[-–]\s*\d+\b", " ", filter_text, count=1)
            filter_text = re.sub(r"\s+", " ", filter_text).strip(" \t\r\n-–|:")
            filter_key = filter_text.casefold()

            await status.edit_text(
                f"🔄 Batch processing <b>{html.escape(str(chat_title))}</b>\n"
                f"Messages <code>{start_id}</code>–<code>{end_id}</code>…\n"
                + (f"Filename filter: <b>{html.escape(filter_text)}</b>\n" if filter_text else "")
                + "<i>Storage channel not required for link generation.</i>",
                parse_mode=ParseMode.HTML,
            )

            # Prefer Vercel website for Watch Online links (stream proxied to VPS)
            base_url = (
                (await get_watch_page_base_url())
                or (await get_active_cloudflare_url())
                or (await get_active_website_url())
                or settings.BASE_URL
                or settings.WEBSITE_URL
            )
            base_url = str(base_url or "").rstrip("/")
            if not base_url:
                return await status.edit_text(
                    "❌ No public website URL configured. "
                    "Set WEBSITE_URL / BASE_URL to your Vercel site "
                    "(e.g. https://animovieszoneflix.vercel.app) or start the tunnel."
                )

            runtime_cfg = await db.site_settings.find_one({"_id": "runtime"}) or {}
            downloads_enabled = runtime_cfg.get("downloads_enabled", True)
            streaming_enabled = runtime_cfg.get("streaming_enabled", True)
            fs_username = (runtime_cfg.get("filestore_bot_username") or "").strip().lstrip("@")

            results: list[str] = []
            errors: list[str] = []
            idx = 0
            total = end_id - start_id + 1

            async def iter_range_messages():
                """Fetch message IDs in bounded chunks for speed without loading huge ranges at once."""
                position = 0
                for chunk_start in range(start_id, end_id + 1, 100):
                    chunk_ids = list(range(chunk_start, min(chunk_start + 100, end_id + 1)))
                    try:
                        fetched = await client.get_messages(resolved_chat_id, chunk_ids)
                        if not isinstance(fetched, list):
                            fetched = [fetched]
                        by_id = {getattr(item, "id", None): item for item in fetched if item is not None}
                        for msg_id in chunk_ids:
                            yield position, msg_id, by_id.get(msg_id)
                            position += 1
                    except Exception as batch_error:
                        logger.warning("batch chunk %s-%s failed, falling back to individual fetches: %s", chunk_ids[0], chunk_ids[-1], batch_error)
                        for msg_id in chunk_ids:
                            try:
                                item = await client.get_messages(resolved_chat_id, msg_id)
                                if isinstance(item, list):
                                    item = item[0] if item else None
                            except Exception as item_error:
                                logger.warning("batch msg %s fetch failed: %s", msg_id, item_error)
                                item = None
                            yield position, msg_id, item
                            position += 1

            async for i, msg_id, source_msg in iter_range_messages():
                if i > 0 and i % 100 == 0:
                    try:
                        await status.edit_text(
                            f"🔄 Batch processing… {i}/{total} (found {idx} matching media)",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass
                try:
                    if not source_msg or getattr(source_msg, "empty", False) or not _is_media_message(source_msg):
                        continue

                    media = (
                        source_msg.document or source_msg.video or source_msg.audio
                        or source_msg.animation or source_msg.photo or source_msg.voice
                        or source_msg.video_note
                    )
                    if source_msg.document:
                        file_name = media.file_name or "file"
                        mime_type = media.mime_type or mimetypes.guess_type(file_name)[0] or "application/octet-stream"
                        file_size = getattr(media, "file_size", None) or 0
                    elif source_msg.video:
                        file_name = getattr(media, "file_name", None) or f"video_{media.file_unique_id}.mp4"
                        mime_type = media.mime_type or "video/mp4"
                        file_size = getattr(media, "file_size", None) or 0
                    elif source_msg.audio:
                        file_name = media.file_name or f"audio_{media.file_unique_id}.mp3"
                        mime_type = media.mime_type or "audio/mpeg"
                        file_size = getattr(media, "file_size", None) or 0
                    elif source_msg.animation:
                        file_name = getattr(media, "file_name", None) or f"animation_{media.file_unique_id}.mp4"
                        mime_type = media.mime_type or "video/mp4"
                        file_size = getattr(media, "file_size", None) or 0
                    elif source_msg.photo:
                        media = source_msg.photo[-1] if isinstance(source_msg.photo, (list, tuple)) else source_msg.photo
                        file_name, mime_type = f"photo_{media.file_unique_id}.jpg", "image/jpeg"
                        file_size = getattr(media, "file_size", None) or 0
                    elif source_msg.voice:
                        file_name, mime_type = f"voice_{media.file_unique_id}.ogg", media.mime_type or "audio/ogg"
                        file_size = getattr(media, "file_size", None) or 0
                    else:
                        file_name, mime_type = f"video_note_{media.file_unique_id}.mp4", "video/mp4"
                        file_size = getattr(media, "file_size", None) or 0

                    caption = getattr(source_msg, "caption", None) or getattr(source_msg, "text", None) or ""
                    file_name = extract_filename_from_caption(caption, file_name)

                    # Match against both the caption text and the extracted filename.
                    # This handles captions that contain a title even without an extension.
                    searchable_name = f"{file_name}\n{caption}".casefold()
                    if filter_key and filter_key not in searchable_name:
                        continue

                    file_id = getattr(media, "file_id", None)
                    if not file_id:
                        continue

                    # Prefer source chat + message (no storage channel needed)
                    src_chat_id = source_msg.chat.id if source_msg.chat else resolved_chat_id
                    existing = await files_col.find_one({
                        "chat_id": src_chat_id,
                        "message_id": source_msg.id,
                    })
                    if existing and existing.get("short_code"):
                        short_code = existing["short_code"]
                    else:
                        short_code = generate_short_code()
                        file_meta = FileMetadata(
                            file_id=file_id,
                            file_unique_id=str(getattr(media, "file_unique_id", source_msg.id)),
                            filename=file_name,
                            mime_type=mime_type,
                            file_size=file_size or 0,
                            uploader_id=user.id,
                            short_code=short_code,
                            chat_id=src_chat_id,
                            message_id=source_msg.id,
                            expiry_time=None,
                        )
                        await files_col.insert_one(file_meta.dict())

                    idx += 1
                    size_str = format_bytes(file_size) if file_size else "—"
                    block = (
                        f"{idx}. 📁 File: {html.escape(file_name)}\n\n"
                        f"📦 Size: {size_str}\n\n"
                        f"🔗 Generated Links\n\n"
                    )
                    if downloads_enabled and base_url:
                        dl = f"{base_url}/dl/{short_code}"
                        block += f"⬇️ Direct Download: <code>{html.escape(dl)}</code>\n\n"
                    elif fs_username:
                        dl = f"https://t.me/{fs_username}?start=file_{short_code}"
                        block += f"📥 Get File in Bot: <code>{html.escape(dl)}</code>\n\n"
                    if streaming_enabled and base_url:
                        watch = f"{base_url}/watch/{short_code}"
                        block += f"▶️ Watch / Stream: <code>{html.escape(watch)}</code>\n"
                    results.append(block)
                except Exception as e:
                    err_txt = f"{type(e).__name__}: {e}"
                    logger.warning("batch msg %s failed: %s", msg_id, err_txt)
                    if len(errors) < 6:
                        errors.append(f"• msg {msg_id}: {html.escape(err_txt[:140])}")
                    continue

            if not results:
                err_block = ("\n\n" + "\n".join(errors)) if errors else ""
                return await status.edit_text(
                    f"❌ No matching media found in messages <code>{start_id}</code>–<code>{end_id}</code> "
                    f"of <b>{html.escape(str(chat_title))}</b>."
                    + (f"\nFilename filter: <b>{html.escape(filter_text)}</b>" if filter_text else "")
                    + f"{err_block}\n\n"
                    "Checklist:\n"
                    "• Bot is a member/admin of the channel\n"
                    "• Message IDs are correct and contain media\n"
                    "• Filename filter matches the file caption/name\n"
                    "• No storage channel is required for this command",
                    parse_mode=ParseMode.HTML,
                )

            header = f"✅ <b>Batch Generated</b> ({idx} file{'s' if idx != 1 else ''})\n\n"
            chunks: list[str] = []
            current = header
            for block in results:
                if len(current) + len(block) + 2 > 3500:
                    chunks.append(current.rstrip())
                    current = header + "<i>(continued)</i>\n\n" + block + "\n"
                else:
                    current += block + "\n"
            if current.strip():
                chunks.append(current.rstrip())

            try:
                await status.delete()
            except Exception:
                pass
            for chunk in chunks:
                await message.reply(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

        except Exception as e:
            logger.exception("batch_command failed: %s", e)
            err = f"❌ <b>Batch failed</b>\n\n<code>{html.escape(type(e).__name__)}: {html.escape(str(e)[:300])}</code>"
            try:
                if status:
                    await status.edit_text(err, parse_mode=ParseMode.HTML)
                else:
                    await message.reply(err, parse_mode=ParseMode.HTML)
            except Exception:
                try:
                    await message.reply(f"Batch failed: {type(e).__name__}: {e}")
                except Exception:
                    pass

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
        if not _is_admin_user(user.id) and not await is_access_allowed(user.id):
            return await message.reply("🔒 This bot is restricted by the administrator.")
        if not await is_user_fsubbed(client, user.id):
            invite_rows = await get_fsub_invite_buttons(client, user.id)
            body = await fsub_restriction_text(client, user.id)
            return await message.reply(
                body,
                reply_markup=_kb(invite_rows) if invite_rows else None,
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
        if not _is_admin_user(user.id) and not await is_access_allowed(user.id):
            return await message.reply("🔒 This bot is restricted by the administrator.")
        if not await is_user_fsubbed(client, user.id):
            invite_rows = await get_fsub_invite_buttons(client, user.id)
            body = await fsub_restriction_text(client, user.id)
            return await message.reply(
                body,
                reply_markup=_kb(invite_rows) if invite_rows else None,
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
        if not _is_admin_user(user.id) and not await is_access_allowed(user.id):
            return await message.reply("🔒 This bot is restricted by the administrator.")
        if not await is_user_fsubbed(client, user.id):
            invite_rows = await get_fsub_invite_buttons(client, user.id)
            body = await fsub_restriction_text(client, user.id)
            return await message.reply(
                body,
                reply_markup=_kb(invite_rows) if invite_rows else None,
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
