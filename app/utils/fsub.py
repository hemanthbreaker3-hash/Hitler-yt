from __future__ import annotations

import logging
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
    ChatMemberStatus.RESTRICTED,
}


async def get_all_fsub_channel_ids() -> list[int]:
    """Env FORCE_SUB_CHANNELS + runtime_fsub from MongoDB (deduped, stable order)."""
    ids: list[int] = []
    seen = set()
    for channel_id in settings.fsub_list:
        try:
            i = int(channel_id)
            if i not in seen:
                ids.append(i)
                seen.add(i)
        except Exception:
            pass
    try:
        runtime = await db.site_settings.find_one({"_id": "runtime"}) or {}
        for channel_id in runtime.get("runtime_fsub") or []:
            try:
                i = int(channel_id)
                if i not in seen:
                    ids.append(i)
                    seen.add(i)
            except Exception:
                pass
    except Exception as e:
        logger.debug("runtime fsub read failed: %s", e)
    return ids


async def is_user_fsubbed(client, user_id: int) -> bool:
    channels = await get_all_fsub_channel_ids()
    if not channels:
        return True
    for channel_id in channels:
        try:
            member = await client.get_chat_member(channel_id, user_id)
            if member.status not in ACTIVE:
                return False
        except UserNotParticipant:
            return False
        except (ChatAdminRequired, ChannelPrivate) as e:
            logger.error(
                "FSUB ERROR: cannot verify %s in %s (%s). Bot must be admin in the force-sub channel.",
                user_id, channel_id, e,
            )
            return False
        except Exception as e:
            logger.error("FSUB ERROR for %s in %s: %s", user_id, channel_id, e)
            return False
    return True


async def get_missing_fsub_channels(client, user_id: int) -> list[int]:
    """Return channel/group IDs the user has not joined (all configured channels checked)."""
    missing: list[int] = []
    for channel_id in await get_all_fsub_channel_ids():
        try:
            member = await client.get_chat_member(channel_id, user_id)
            if member.status not in ACTIVE:
                missing.append(channel_id)
        except UserNotParticipant:
            missing.append(channel_id)
        except Exception as e:
            logger.debug("fsub check %s: %s", channel_id, e)
            missing.append(channel_id)
    return missing


async def resolve_chat_title(client, chat_id: int) -> str:
    """Human-readable channel/group title for button labels."""
    try:
        chat = await client.get_chat(chat_id)
        title = getattr(chat, "title", None) or getattr(chat, "first_name", None)
        if title:
            return str(title).strip()[:40]
        uname = getattr(chat, "username", None)
        if uname:
            return f"@{uname}"
    except Exception as e:
        logger.debug("get_chat title %s failed: %s", chat_id, e)
    return f"Channel {chat_id}"


async def resolve_chat_invite_link(client, chat_id: int) -> Optional[str]:
    """Best-effort invite / public link for a channel or group."""
    try:
        chat = await client.get_chat(chat_id)
        uname = getattr(chat, "username", None)
        if uname:
            return f"https://t.me/{uname}"
        link = getattr(chat, "invite_link", None)
        if link:
            return str(link)
    except Exception as e:
        logger.debug("get_chat for invite %s failed: %s", chat_id, e)

    try:
        if hasattr(client, "create_chat_invite_link"):
            inv = await client.create_chat_invite_link(chat_id, name="FSUB")
            link = getattr(inv, "invite_link", None) or getattr(inv, "link", None)
            if link:
                return str(link)
    except Exception:
        pass
    try:
        if hasattr(client, "export_chat_invite_link"):
            link = await client.export_chat_invite_link(chat_id)
            if link:
                return str(link)
    except Exception as e:
        logger.debug("export invite %s failed: %s", chat_id, e)

    if settings.UPDATE_CHANNEL_URL:
        return settings.UPDATE_CHANNEL_URL
    return None


async def get_fsub_invite_buttons(client, user_id: int, *, include_try_again: bool = True) -> list:
    """
    Keyboard for force-sub:
    - One Join button per configured channel the user has not joined
    - Label includes the channel/group title
    - Layout: up to 2 channel buttons per row
    - After each row of channel buttons, a Try Again button (callback fsub:check)
    """
    missing = await get_missing_fsub_channels(client, user_id)
    # If nothing missing but channels exist, user is fully subscribed
    if not missing:
        return []

    # Build (label, url) for each missing channel
    entries: list[tuple[str, str]] = []
    for cid in missing:
        title = await resolve_chat_title(client, cid)
        link = await resolve_chat_invite_link(client, cid)
        if not link:
            continue
        label = f"📢 {title}"
        if len(label) > 64:
            label = label[:61] + "…"
        entries.append((label, link))

    if not entries and settings.UPDATE_CHANNEL_URL:
        entries.append(("📢 Join Updates Channel", settings.UPDATE_CHANNEL_URL))

    rows: list[list] = []
    # Two channel buttons per row, then Try Again under that row
    i = 0
    while i < len(entries):
        pair = entries[i : i + 2]
        rows.append([InlineKeyboardButton(lab, url=url) for lab, url in pair])
        if include_try_again:
            rows.append([InlineKeyboardButton("🔄 Try Again", callback_data="fsub:check")])
        i += 2

    return rows


async def fsub_restriction_text(client, user_id: int) -> str:
    """Message body listing which channels must be joined."""
    missing = await get_missing_fsub_channels(client, user_id)
    if not missing:
        return (
            "🔒 **Access Restricted**\n\n"
            "> Join the required channel(s), then tap **Try Again**."
        )
    lines = []
    for cid in missing:
        title = await resolve_chat_title(client, cid)
        lines.append(f"• **{title}** (`{cid}`)")
    listed = "\n".join(lines)
    return (
        "🔒 **Access Restricted**\n\n"
        "> Join **all** channels/groups below, then tap **Try Again**.\n\n"
        f"{listed}"
    )
