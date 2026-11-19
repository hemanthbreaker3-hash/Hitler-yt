from __future__ import annotations

import datetime
import logging
import os
import re
from typing import List, Optional

from wzgram import Client
from wzgram.types import BotCommand

from app.database.connection import db, settings

logger = logging.getLogger(__name__)


class SessionManager:
    def __init__(self):
        self.clients: List[Client] = []
        self.bot_client: Optional[Client] = None
        self.helper_bots: dict[int, Client] = {}
        self.helper_bot_meta: dict[int, dict] = {}
        self._index = 0
        self._started = False

    def prepare_bot(self):
        """Create primary bot Client so handlers can be registered before start()."""
        if self.bot_client is not None:
            return
        self.bot_client = Client(
            name="HITLER YT_bot",
            api_id=settings.API_ID,
            api_hash=settings.API_HASH,
            bot_token=settings.BOT_TOKEN,
            in_memory=True,
            workers=32,
            max_concurrent_transmissions=32,
        )

    async def start(self):
        if self._started and self.bot_client is not None and self.bot_client.is_connected:
            return
        self.clients.clear()
        self.helper_bots.clear()
        self.helper_bot_meta.clear()
        self._index = 0

        if self.bot_client is None:
            self.prepare_bot()
        await self.bot_client.start()
        logger.info("HITLER YT primary bot client started (wzgram)")
        await self.register_bot_commands()

        # Always include primary bot client in the streaming pool
        self.clients.append(self.bot_client)

        # 1. Start any exported user string sessions from SESSIONS
        raw_sessions = str(settings.SESSIONS or "").strip()
        session_strings = [
            item.strip().strip("'\"")
            for item in re.split(r"[,;\r\n]+", raw_sessions)
            if item.strip() and not item.strip().startswith("#")
        ]
        session_strings = list(dict.fromkeys(session_strings))
        for i, session_str in enumerate(session_strings):
            try:
                client = Client(
                    name=f"HITLER YT_user_{i}",
                    api_id=settings.API_ID,
                    api_hash=settings.API_HASH,
                    session_string=session_str,
                    in_memory=True,
                    workers=32,
                    max_concurrent_transmissions=32,
                    no_updates=True,
                )
                await client.start()
                self.clients.append(client)
                logger.info("User session %s started (wzgram)", i + 1)
            except Exception as e:
                logger.error("Session %s failed: %s", i + 1, e)

        # 2. Start all saved helper bot tokens from MongoDB (added via /addbots) and optional HELPER_BOT_TOKENS env
        await self.load_saved_helper_bots()

        self._started = True
        logger.info(
            "Streaming clients ready: %d total (%d helper bots)",
            len(self.clients),
            len(self.helper_bots),
        )

    async def load_saved_helper_bots(self) -> int:
        """Load and start all helper bot tokens stored in MongoDB db.helper_bots and HELPER_BOT_TOKENS."""
        started_count = 0
        tokens_to_load: list[str] = []

        env_tokens = os.environ.get("HELPER_BOT_TOKENS", "") or os.environ.get("MULTI_TOKENS", "")
        for raw in re.split(r"[,;\s]+", env_tokens):
            tok = raw.strip().strip("'\"")
            if tok and tok != settings.BOT_TOKEN and tok not in tokens_to_load:
                tokens_to_load.append(tok)

        try:
            await db.helper_bots.create_index("bot_id", unique=True)
            cursor = db.helper_bots.find({}, {"token": 1, "bot_id": 1})
            async for doc in cursor:
                tok = str(doc.get("token") or "").strip()
                if tok and tok != settings.BOT_TOKEN and tok not in tokens_to_load:
                    tokens_to_load.append(tok)
        except Exception as exc:
            logger.warning("Could not read saved helper bots from MongoDB: %s", exc)

        for tok in tokens_to_load:
            try:
                await self.add_helper_bot(tok, persist=True)
                started_count += 1
            except Exception as exc:
                logger.warning("Saved helper bot token failed to start: %s", exc)

        return started_count

    async def add_helper_bot(self, token: str, added_by: int | None = None, persist: bool = True) -> dict:
        """
        Validate, start, and register a new helper bot token in the live multi-client download pool.
        """
        clean_token = str(token or "").strip().strip("'\"")
        if not re.fullmatch(r"\d{6,16}:[A-Za-z0-9_-]{30,60}", clean_token):
            raise ValueError("Invalid bot token format. Expected format: 123456789:ABCdefGHI...")

        if clean_token == settings.BOT_TOKEN:
            raise ValueError("This token is already running as the primary bot.")

        # Check if already running
        for bot_id, meta in self.helper_bot_meta.items():
            if meta.get("token") == clean_token and bot_id in self.helper_bots:
                return meta

        bot_prefix = clean_token.split(":", 1)[0]
        client = Client(
            name=f"HITLER YT_helper_{bot_prefix}_{len(self.helper_bots) + 1}",
            api_id=settings.API_ID,
            api_hash=settings.API_HASH,
            bot_token=clean_token,
            in_memory=True,
            workers=32,
            max_concurrent_transmissions=32,
            no_updates=True,
        )
        await client.start()
        try:
            me = await client.get_me()
        except Exception:
            try:
                await client.stop()
            except Exception:
                pass
            raise

        bot_id = int(me.id)
        username = getattr(me, "username", "") or f"bot_{bot_id}"
        first_name = getattr(me, "first_name", "") or "Helper Bot"

        # If this bot_id was already connected with an older client instance, replace it cleanly
        if bot_id in self.helper_bots:
            old_client = self.helper_bots.pop(bot_id)
            self.clients = [c for c in self.clients if c is not old_client]
            try:
                if old_client.is_connected:
                    await old_client.stop()
            except Exception:
                pass

        self.helper_bots[bot_id] = client
        if all(client is not c for c in self.clients):
            self.clients.append(client)

        now = datetime.datetime.now(datetime.timezone.utc)
        meta = {
            "bot_id": bot_id,
            "username": username,
            "first_name": first_name,
            "token": clean_token,
            "added_by": added_by,
            "updated_at": now,
        }
        self.helper_bot_meta[bot_id] = meta

        if persist:
            await db.helper_bots.update_one(
                {"bot_id": bot_id},
                {"$set": meta, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )

        logger.info("Helper bot @%s (%d) started and added to download pool (total clients: %d)", username, bot_id, len(self.clients))
        return meta

    async def remove_helper_bot(self, bot_id: int) -> bool:
        """Stop and remove a helper bot from the pool and MongoDB."""
        removed = False
        client = self.helper_bots.pop(bot_id, None)
        self.helper_bot_meta.pop(bot_id, None)
        if client is not None:
            self.clients = [c for c in self.clients if c is not client]
            try:
                if client.is_connected:
                    await client.stop()
            except Exception:
                pass
            removed = True

        res = await db.helper_bots.delete_one({"bot_id": int(bot_id)})
        return removed or (res.deleted_count > 0)

    def list_helper_bots(self) -> list[dict]:
        """Return metadata for all active helper bots."""
        items = []
        for bot_id, meta in self.helper_bot_meta.items():
            client = self.helper_bots.get(bot_id)
            items.append({
                "bot_id": bot_id,
                "username": meta.get("username", ""),
                "first_name": meta.get("first_name", ""),
                "connected": bool(client and getattr(client, "is_connected", False)),
            })
        return items

    async def stop(self):
        unique = {id(c): c for c in [*self.clients, *self.helper_bots.values(), self.bot_client] if c is not None}
        for client in unique.values():
            try:
                if client.is_connected:
                    await client.stop()
            except Exception:
                logger.exception("Error while stopping wzgram client")
        self.clients.clear()
        self.helper_bots.clear()
        self.helper_bot_meta.clear()
        self.bot_client = None
        self._started = False

    async def register_bot_commands(self) -> None:
        """Register all bot slash commands with Telegram on startup."""
        if not self.bot_client:
            return
        commands = [
            BotCommand("start", "Start / welcome & how to use"),
            BotCommand("addbots", "Add unlimited helper bot tokens for speed boost (admin)"),
            BotCommand("panel", "Open control panel (admin)"),
            BotCommand("manage", "Edit website pages and bot settings (admin)"),
            BotCommand("baseurl", "Set public base / ngrok URL (admin)"),
            BotCommand("backendurl", "Set backend / ngrok URL (admin)"),
            BotCommand("search", "Search TMDB & publish catalog page (admin)"),
            BotCommand("categories", "Manage categories (admin)"),
            BotCommand("edit", "Open page editor (admin)"),
            BotCommand("ss", "Screenshots — reply to media"),
            BotCommand("sv", "Sample video — reply to media"),
            BotCommand("mi", "MediaInfo — reply to media or pass a download URL"),
            BotCommand("stats", "Bot statistics (admin)"),
            BotCommand("broadcast", "Broadcast message (admin)"),
            BotCommand("ban", "Ban user (admin)"),
            BotCommand("unban", "Unban user (admin)"),
            BotCommand("autodel", "Auto-delete timer (admin)"),
        ]
        try:
            await self.bot_client.set_bot_commands(commands)
            logger.info("Registered %s bot commands with Telegram", len(commands))
        except Exception as e:
            logger.warning("Failed to register bot commands: %s", e)

    def get_client(self) -> Optional[Client]:
        clients = self.get_all_clients()
        if not clients:
            return None
        client = clients[self._index % len(clients)]
        self._index = (self._index + 1) % len(clients)
        return client

    def get_all_clients(self) -> List[Client]:
        result: List[Client] = []
        for client in [*self.clients, *self.helper_bots.values(), self.bot_client]:
            if client is not None and getattr(client, "is_connected", False) and all(client is not x for x in result):
                result.append(client)
        return result


session_manager = SessionManager()
