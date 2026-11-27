from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional

from wzgram.enums import ChatMemberStatus
from wzgram.errors import UserNotParticipant, ChatAdminRequired, ChannelPrivate
from wzgram.types import InlineKeyboardButton

from app.database.connection import settings, db

logger = logging.getLogger(__name__)

ACTIVE = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER,
}

_CHAT_META_CACHE: dict[str, tuple[float, str, Optional[str]]] = {}
_CHAT_META_TTL = 600.0


def _chat_ref(value):
    """Normalize a numeric chat ID, @username, or t.me chat URL."""
    if isinstance(value, int):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.split("?", 1)[0].rstrip("/")
    if "t.me/" in raw:
        part = raw.split("t.me/", 1)[1].split("/", 1)[0]
        if part.startswith("+"):
            return None
        raw = part
    if raw.startswith("@"):
        return raw
    try:
        return int(raw)
    except (TypeError, ValueError):
        return "@" + raw if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", raw) else None


async def get_all_fsub_channel_ids() -> list[int | str]:
    """Env FORCE_SUB_CHANNELS + runtime_fsub from MongoDB (deduped, stable order)."""
    ids: list[int | str] = []
    seen = set()
    for channel_id in settings.fsub_list:
        ref = _chat_ref(channel_id)
        if ref is not None and str(ref).lower() not in seen:
            ids.append(ref)
            seen.add(str(ref).lower())
    try:
        runtime = await db.site_settings.find_one({"_id": "runtime"}) or {}
        for channel_id in runtime.get("runtime_fsub") or []:
            ref = _chat_ref(channel_id)
            if ref is not None and str(ref).lower() not in seen:
                ids.append(ref)
                seen.add(str(ref).lower())
    except Exception as e:
        logger.debug("runtime fsub read failed: %s", e)
    return ids


def _get_fallback_client(client):
    try:
        from app.streamer.manager import session_manager
        primary = getattr(session_manager, "bot_client", None)
        if primary is not None and primary is not client and getattr(primary, "is_connected", False):
            return primary
    except Exception:
        pass
    return None


async def _check_single_channel(client, fallback_client, channel_id, user_id: int) -> bool:
    for cli in (client, fallback_client):
        if cli is None:
            continue
        try:
            member = await cli.get_chat_member(channel_id, user_id)
            if member.status in ACTIVE:
                return True
            st_val = str(getattr(member.status, "value", member.status)).lower()
            if st_val in ("restricted", "chatmemberstatus.restricted") and bool(getattr(member, "is_member", False)):
                return True
            return False
        except UserNotParticipant:
            return False
        except (ChatAdminRequired, ChannelPrivate) as e:
            if cli is client and fallback_client is not None:
                continue
            logger.error(
                "FSUB ERROR: cannot verify %s in %s (%s). Bot must be admin in the force-sub channel.",
                user_id, channel_id, e,
            )
            return False
        except Exception as e:
            if cli is client and fallback_client is not None:
                continue
            logger.debug("fsub check %s/%s: %s", channel_id, user_id, e)
            return False
    return False


async def get_missing_fsub_channels(client, user_id: int) -> list[int | str]:
    """Return channel/group IDs the user has not joined (checked concurrently)."""
    channels = await get_all_fsub_channel_ids()
    if not channels:
        return []
    fallback = _get_fallback_client(client)
    results = await asyncio.gather(
        *(_check_single_channel(client, fallback, cid, user_id) for cid in channels),
        return_exceptions=True,
    )
    missing: list[int | str] = []
    for cid, ok in zip(channels, results):
        if ok is not True:
            missing.append(cid)
    return missing


async def is_user_fsubbed(client, user_id: int) -> bool:
    channels = await get_all_fsub_channel_ids()
    if not channels:
        return True
    missing = await get_missing_fsub_channels(client, user_id)
    return len(missing) == 0


async def _resolve_chat_meta(client, chat_id) -> tuple[str, Optional[str]]:
    key = str(chat_id)
    now = time.monotonic()
    cached = _CHAT_META_CACHE.get(key)
    if cached and (now - cached[0]) < _CHAT_META_TTL:
        return cached[1], cached[2]

    fallback = _get_fallback_client(client)
    title = f"Channel {chat_id}"
    link: Optional[str] = None

    for cli in (client, fallback):
        if cli is None:
            continue
        try:
            chat = await cli.get_chat(chat_id)
            raw_title = getattr(chat, "title", None) or getattr(chat, "first_name", None)
            uname = getattr(chat, "username", None)
            if raw_title:
                title = str(raw_title).strip()[:40]
            elif uname:
                title = f"@{uname}"
            if uname:
                link = f"https://t.me/{uname}"
            elif getattr(chat, "invite_link", None):
                link = str(chat.invite_link)
            if not link and hasattr(cli, "create_chat_invite_link"):
                try:
                    inv = await cli.create_chat_invite_link(chat_id, name="FSUB")
                    link = str(getattr(inv, "invite_link", None) or getattr(inv, "link", None) or "") or None
                except Exception:
                    pass
            if not link and hasattr(cli, "export_chat_invite_link"):
                try:
                    link = str(await cli.export_chat_invite_link(chat_id) or "") or None
                except Exception:
                    pass
            if link:
                break
        except Exception as e:
            logger.debug("resolve_chat_meta %s via %s failed: %s", chat_id, type(cli).__name__, e)

    if not link and settings.UPDATE_CHANNEL_URL:
        link = settings.UPDATE_CHANNEL_URL

    _CHAT_META_CACHE[key] = (now, title, link)
    return title, link


async def resolve_chat_title(client, chat_id) -> str:
    title, _ = await _resolve_chat_meta(client, chat_id)
    return title


async def resolve_chat_invite_link(client, chat_id) -> Optional[str]:
    _, link = await _resolve_chat_meta(client, chat_id)
    return link


async def check_fsub_status(
    client,
    user_id: int,
    *,
    include_try_again: bool = True,
    payload_code: str | None = None,
) -> tuple[bool, list, str]:
    """Single-pass FSub check returning (is_subscribed, inline_keyboard_rows, message_text)."""
    missing = await get_missing_fsub_channels(client, user_id)
    if not missing:
        return True, [], ""

    metas = await asyncio.gather(*(_resolve_chat_meta(client, cid) for cid in missing))
    entries: list[tuple[str, str]] = []
    lines: list[str] = []
    for cid, (title, link) in zip(missing, metas):
        lines.append(f"• **{title}** (`{cid}`)")
        if link:
            label = f"📢 {title}"
            if len(label) > 64:
                label = label[:61] + "…"
            entries.append((label, link))

    if not entries and settings.UPDATE_CHANNEL_URL:
        entries.append(("📢 Join Updates Channel", settings.UPDATE_CHANNEL_URL))

    cb_data = f"fsub:check:{payload_code}" if payload_code else "fsub:check"
    rows: list[list] = []
    i = 0
    while i < len(entries):
        pair = entries[i : i + 2]
        rows.append([InlineKeyboardButton(lab, url=url) for lab, url in pair])
        i += 2
    if include_try_again and rows:
        rows.append([InlineKeyboardButton("🔄 Try Again", callback_data=cb_data)])

    listed = "\n".join(lines)
    body = (
        "🔒 **Access Restricted**\n\n"
        "> Join **all** channels/groups below, then tap **Try Again**.\n\n"
        f"{listed}"
    )
    return False, rows, body


async def get_fsub_invite_buttons(client, user_id: int, *, include_try_again: bool = True, payload_code: str | None = None) -> list:
    _, rows, _ = await check_fsub_status(client, user_id, include_try_again=include_try_again, payload_code=payload_code)
    return rows


async def fsub_restriction_text(client, user_id: int) -> str:
    _, _, body = await check_fsub_status(client, user_id, include_try_again=False)
    if not body:
        return "🔒 **Access Restricted**\n\n> Join the required channel(s), then tap **Try Again**."
    return body
