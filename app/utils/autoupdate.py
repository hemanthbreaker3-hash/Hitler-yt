"""Auto-Update Channel broadcaster for newly added / updated catalog pages."""
from __future__ import annotations

import datetime
import html
import logging
import re
from typing import Any

from wzgram.enums import ChatMemberStatus, ParseMode
from wzgram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.database.connection import (
    db,
    get_active_cloudflare_url,
    get_watch_page_base_url,
    settings,
)

logger = logging.getLogger(__name__)

_SETTINGS_ID = "autoupdate"

_SEASON_EP_PATTERNS = (
    re.compile(r"S0*([0-9]{1,3})[ ._\-]*E(?:p|pisode)?[ ._\-]*0*([0-9]{1,4})", re.I),
    re.compile(r"0*([0-9]{1,3})[ ]*[xX][ ]*0*([0-9]{1,4})"),
    re.compile(r"Season[ ._\-]*0*([0-9]{1,3})[ ._\-]*(?:Episode|Ep|E)[ ._\-]*0*([0-9]{1,4})", re.I),
)
_EP_ONLY_RE = re.compile(r"(?:Episode|Ep)[ ._\-]*0*([0-9]{1,4})", re.I)

_LANG_MAP = [
    (re.compile(r"\b(tel|telugu)\b", re.I), "TEL"),
    (re.compile(r"\b(tam|tamil)\b", re.I), "TAM"),
    (re.compile(r"\b(hin|hindi)\b", re.I), "HIN"),
    (re.compile(r"\b(eng|english)\b", re.I), "ENG"),
    (re.compile(r"\b(jpn|jap|japanese)\b", re.I), "JPN"),
    (re.compile(r"\b(kan|kannada)\b", re.I), "KAN"),
    (re.compile(r"\b(mal|malayalam)\b", re.I), "MAL"),
    (re.compile(r"\b(kor|korean)\b", re.I), "KOR"),
]


async def get_autoupdate_config() -> dict[str, Any]:
    doc = await db.site_settings.find_one({"_id": _SETTINGS_ID}) or {}
    channels = doc.get("channels") if isinstance(doc.get("channels"), list) else []
    clean_channels = []
    seen = set()
    for ch in channels:
        if not isinstance(ch, dict) or ch.get("chat_id") is None:
            continue
        cid = int(ch["chat_id"])
        if cid in seen:
            continue
        seen.add(cid)
        clean_channels.append({
            "chat_id": cid,
            "title": str(ch.get("title") or cid),
            "username": ch.get("username") or None,
        })
    return {
        "enabled": bool(doc.get("enabled", False)),
        "channels": clean_channels,
    }


async def save_autoupdate_config(enabled: bool, channels: list[dict]) -> None:
    await db.site_settings.update_one(
        {"_id": _SETTINGS_ID},
        {"$set": {"enabled": bool(enabled), "channels": channels, "updated_at": datetime.datetime.utcnow()}},
        upsert=True,
    )


async def verify_channel_post_permission(client, chat_ref) -> tuple[bool, dict | None, str]:
    """Verify the bot is in the channel/group and has permission to post messages."""
    try:
        chat = await client.get_chat(chat_ref)
    except Exception as exc:
        return False, None, f"Cannot access chat `{chat_ref}`: {type(exc).__name__}: {exc}"

    chat_id = getattr(chat, "id", None)
    if not chat_id:
        return False, None, "Could not resolve chat ID."

    try:
        me = await client.get_me()
        member = await client.get_chat_member(chat_id, me.id)
    except Exception as exc:
        return False, None, f"Bot is not in `{getattr(chat, 'title', chat_id)}` or cannot verify permissions ({type(exc).__name__})."

    status = getattr(member, "status", None)
    chat_type = str(getattr(getattr(chat, "type", ""), "value", getattr(chat, "type", ""))).lower()

    if "channel" in chat_type:
        if status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return False, None, "Bot must be an **Administrator** in the channel with **Post Messages** permission."
        priv = getattr(member, "privileges", None)
        can_post = getattr(priv, "can_post_messages", None) if priv is not None else getattr(member, "can_post_messages", None)
        if can_post is False:
            return False, None, "Bot lacks **Post Messages** permission in that channel."
    else:
        if status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return False, None, "Bot must be a member/admin with permission to send messages."
        priv = getattr(member, "privileges", None)
        can_send = getattr(priv, "can_send_messages", None) if priv is not None else getattr(member, "can_send_messages", None)
        if can_send is False:
            return False, None, "Bot does not have permission to send messages in that chat."

    info = {
        "chat_id": int(chat_id),
        "title": str(getattr(chat, "title", None) or getattr(chat, "username", None) or chat_id),
        "username": getattr(chat, "username", None) or None,
    }
    return True, info, "OK"


def _parse_seasons_and_episodes(groups: list[dict]) -> tuple[dict[int, int], int]:
    season_counts: dict[int, int] = {}
    unnumbered = 0
    for g in groups:
        if not isinstance(g, dict):
            continue
        name = str(g.get("name") or "").strip()
        matched = False
        for pat in _SEASON_EP_PATTERNS:
            m = pat.search(name)
            if m:
                s_num = int(m.group(1))
                if s_num > 0:
                    season_counts[s_num] = season_counts.get(s_num, 0) + 1
                    matched = True
                    break
        if not matched:
            m_ep = _EP_ONLY_RE.search(name)
            if m_ep:
                season_counts[1] = season_counts.get(1, 0) + 1
            else:
                unnumbered += 1
    return season_counts, unnumbered


def _detect_languages(page_doc: dict, groups: list[dict]) -> str:
    parts = [str(page_doc.get("title") or ""), str(page_doc.get("overview") or "")]
    for g in groups:
        if not isinstance(g, dict):
            continue
        parts.append(str(g.get("name") or ""))
        for b in (g.get("buttons") or []):
            if isinstance(b, dict):
                parts.append(str(b.get("name") or b.get("text") or ""))
    blob = " ".join(parts)
    found: list[str] = []
    for pat, code in _LANG_MAP:
        if pat.search(blob) and code not in found:
            found.append(code)
    if found:
        if len(found) > 1:
            return f"Multi-Audio ({'+'.join(found)})"
        return found[0]
    return "Multi-Audio (TEL+TAM+HIN)"


def format_autoupdate_post(page_doc: dict, base_url: str) -> str:
    title = str(page_doc.get("title") or page_doc.get("name") or page_doc.get("slug") or "Untitled").strip()
    slug = str(page_doc.get("slug") or "").strip()
    raw_date = str(page_doc.get("release_date") or page_doc.get("first_air_date") or "").strip()
    today_year = datetime.datetime.now().year
    start_year = raw_date[:4] if len(raw_date) >= 4 and raw_date[:4].isdigit() else str(today_year)
    today_2d = str(today_year)[-2:]
    if start_year != str(today_year):
        header_year = f"{start_year}-{today_2d}"
        year_line = f"{start_year} - {today_year}"
    else:
        header_year = str(today_year)
        year_line = str(today_year)

    all_groups = [g for g in (page_doc.get("groups") or []) if isinstance(g, dict)]
    watch_groups = [g for g in all_groups if str(g.get("box") or "watch").strip().lower() in ("watch", "")]
    target_groups = watch_groups or all_groups

    season_counts, unnumbered = _parse_seasons_and_episodes(target_groups)
    seasons_sorted = sorted(season_counts.keys())
    if len(seasons_sorted) == 1:
        season_line = f"Season {seasons_sorted[0]}"
    elif len(seasons_sorted) > 1:
        season_line = "Season " + ", ".join(str(s) for s in seasons_sorted[:-1]) + f" and {seasons_sorted[-1]}"
    else:
        season_line = "Movie / Special"

    if seasons_sorted:
        ep_parts = [f"S{s:02d} - {season_counts[s]} EPISODES" for s in seasons_sorted]
        if len(ep_parts) == 1:
            episodes_line = ep_parts[0]
        else:
            episodes_line = ", ".join(ep_parts[:-1]) + " AND " + ep_parts[-1]
    else:
        episodes_line = f"{max(1, len(target_groups))} Link Group(s)"

    genres_list = [str(g).strip() for g in (page_doc.get("genres") or []) if str(g).strip()]
    genres_line = ", ".join(genres_list) if genres_list else "Anime"
    langs_line = "Multi Audio"
    page_url = f"{base_url.rstrip('/')}/title/{slug}" if slug else base_url.rstrip("/")

    return (
        f"<b>{html.escape(title.upper())} ({header_year})\n\n"
        f"🎬 Name: {html.escape(title)}\n"
        f"📅 Year: {year_line}\n"
        f"📺 Season: {season_line}\n"
        f"🎞️ Episodes: {episodes_line}\n"
        f"🎭 Genres: {html.escape(genres_line)}\n"
        f"🌐 Languages: {html.escape(langs_line)}\n\n"
        f"📥 Watch &amp; Download:</b>\n"
        f"🌐 {html.escape(page_url)}"
    )


async def broadcast_autoupdate(page_doc: dict | None, client=None) -> int:
    """Post the formatted update to all configured channels when Auto-Update is enabled."""
    try:
        return await _broadcast_autoupdate_inner(page_doc, client=client)
    except Exception as top_exc:
        logger.warning("broadcast_autoupdate error: %s", top_exc)
        return 0


async def _broadcast_autoupdate_inner(page_doc: dict | None, client=None) -> int:
    if not isinstance(page_doc, dict) or not page_doc.get("published"):
        return 0
    cfg = await get_autoupdate_config()
    if not cfg.get("enabled") or not cfg.get("channels"):
        return 0

    if client is None:
        try:
            from app.streamer.manager import session_manager
            client = getattr(session_manager, "bot_client", None)
        except Exception:
            client = None
    if client is None:
        return 0

    base_url = (
        (await get_watch_page_base_url())
        or (await get_active_cloudflare_url())
        or settings.BASE_URL
        or settings.WEBSITE_URL
        or ""
    ).rstrip("/")
    if not base_url:
        return 0

    text = format_autoupdate_post(page_doc, base_url)
    slug = str(page_doc.get("slug") or "").strip()
    page_url = f"{base_url}/title/{slug}" if slug else base_url
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📥 Watch & Download", url=page_url)]])

    img = page_doc.get("backdrop_path") or page_doc.get("poster_path") or ""
    if img and not str(img).startswith("http"):
        img = f"https://image.tmdb.org/t/p/w1280{img}"

    sent = 0
    for ch in cfg["channels"]:
        cid = ch["chat_id"]
        try:
            if img and len(text) <= 1024:
                try:
                    await client.send_photo(chat_id=cid, photo=str(img), caption=text, reply_markup=kb, parse_mode=ParseMode.HTML)
                    sent += 1
                    continue
                except Exception:
                    pass
            await client.send_message(chat_id=cid, text=text, reply_markup=kb, disable_web_page_preview=False, parse_mode=ParseMode.HTML)
            sent += 1
        except Exception as exc:
            logger.warning("Auto-update post to %s failed: %s", cid, exc)
    return sent
