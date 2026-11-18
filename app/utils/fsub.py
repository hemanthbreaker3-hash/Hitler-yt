from __future__ import annotations

import logging
from typing import Optional

from wzgram.enums import ChatMemberStatus
from wzgram.errors import UserNotParticipant, ChatAdminRequired, ChannelPrivate

from app.database.connection import settings, db

logger = logging.getLogger(__name__)

ACTIVE = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER,
    ChatMemberStatus.RESTRICTED,
}


async def get_all_fsub_channel_ids() -> list[int]:
    """Env FORCE_SUB_CHANNELS + runtime_fsub from MongoDB."""
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
            status = member.status
            if status not in ACTIVE:
                return False
        except UserNotParticipant:
            return False
        except (ChatAdminRequired, ChannelPrivate) as e:
            logger.error(
                "FSUB ERROR: cannot verify %s in %s (%s). "
                "Bot must be admin in the force-sub channel.",
                user_id, channel_id, e,
            )
            return False
        except Exception as e:
            logger.error("FSUB ERROR for %s in %s: %s", user_id, channel_id, e)
            return False
    return True


async def get_missing_fsub_channels(client, user_id: int) -> list[int]:
    """Return channel/group IDs the user has not joined."""
    missing: list[int] = []
    channels = await get_all_fsub_channel_ids()
    for channel_id in channels:
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
    return None


async def get_fsub_invite_buttons(client, user_id: int) -> list:
    """
    Build inline keyboard rows with Join buttons for channels the user has not joined.
    Falls back to UPDATE_CHANNEL_URL if no per-chat invite can be resolved.
    """
    from wzgram.types import InlineKeyboardButton

    missing = await get_missing_fsub_channels(client, user_id)
    rows = []
    if not missing:
        if settings.UPDATE_CHANNEL_URL:
            rows.append([InlineKeyboardButton("📢 Join Updates Channel", url=settings.UPDATE_CHANNEL_URL)])
        return rows

    for i, cid in enumerate(missing, start=1):
        link = await resolve_chat_invite_link(client, cid)
        label = f"📢 Join Channel {i}" if len(missing) > 1 else "📢 Join Channel / Group"
        if link:
            rows.append([InlineKeyboardButton(label, url=link)])
        elif settings.UPDATE_CHANNEL_URL:
            rows.append([InlineKeyboardButton(label, url=settings.UPDATE_CHANNEL_URL)])

    if not rows and settings.UPDATE_CHANNEL_URL:
        rows.append([InlineKeyboardButton("📢 Join Updates Channel", url=settings.UPDATE_CHANNEL_URL)])
    return rows
