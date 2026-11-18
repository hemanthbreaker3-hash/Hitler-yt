from __future__ import annotations

import datetime
import logging
import os
import re
from typing import Callable, List, Optional

import httpx
from wzgram import Client
from wzgram.types import BotCommand

from app.database.connection import db, settings, settings_col

logger = logging.getLogger(__name__)

_BOT_TOKEN_RE = re.compile(r"^\d{6,16}:[A-Za-z0-9_-]{25,70}$")


class SessionManager:
    def __init__(self):
        self.clients: List[Client] = []
        self.bot_client: Optional[Client] = None
        self.helper_bots: dict[int, Client] = {}
        self.helper_bot_meta: dict[int, dict] = {}
        self.filestore_client: Optional[Client] = None
        self.filestore_meta: dict = {}
        self._filestore_handler_registrar: Optional[Callable[[Client], None]] = None
        self._index = 0
        self._started = False

    def set_filestore_handler_registrar(self, registrar: Callable[[Client], None]) -> None:
        """Register callback that attaches /start deep-link delivery handlers to a FileStore bot client."""
        self._filestore_handler_registrar = registrar
        if self.filestore_client is not None and self.filestore_client is not self.bot_client:
            try:
                registrar(self.filestore_client)
            except Exception as exc:
                logger.warning("Could not attach FileStore handlers to existing client: %s", exc)

    def prepare_bot(self):
        """Create primary bot Client so handlers can be registered before start()."""
        if self.bot_client is not None:
            return
        self.bot_client = Client(
            name="hitler_bot",
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
        logger.info("HITLER primary bot client started (wzgram)")
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
                    name=f"hitler_user_{i}",
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

        # 3. Start saved FileStore bot token from MongoDB (configured via /filestore <bot_token>)
        await self.load_saved_filestore_bot()

        self._started = True
        logger.info(
            "Streaming clients ready: %d total (%d helper bots, filestore=%s)",
            len(self.clients),
            len(self.helper_bots),
            self.filestore_meta.get("username") or "none",
        )

    async def load_saved_filestore_bot(self) -> Optional[dict]:
        """Load and start the saved FileStore bot token from MongoDB or environment."""
        try:
            runtime = await db.site_settings.find_one({"_id": "runtime"}) or {}
            tok = str(runtime.get("filestore_bot_token") or os.environ.get("FILESTORE_BOT_TOKEN") or "").strip()
            uname = str(runtime.get("filestore_bot_username") or "").strip().lstrip("@")
            if tok and _BOT_TOKEN_RE.match(tok):
                return await self.set_filestore_bot(
                    tok,
                    added_by=runtime.get("filestore_updated_by"),
                    persist=True,
                )
            if uname:
                self.filestore_meta = {
                    "bot_id": runtime.get("filestore_bot_id"),
                    "username": uname,
                    "first_name": runtime.get("filestore_bot_name") or uname,
                    "token": "",
                    "connected": bool(self.bot_client and getattr(self.bot_client, "is_connected", False)),
                }
                return self.filestore_meta
        except Exception as exc:
            logger.warning("Saved FileStore bot failed to start on boot: %s", exc)
        return None

    async def _fetch_bot_info_via_http(self, token: str) -> Optional[dict]:
        """Fast Telegram Bot API getMe check to resolve username/id from a bot token."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.get(f"https://api.telegram.org/bot{token}/getMe")
                data = resp.json()
                if resp.status_code == 200 and data.get("ok") and isinstance(data.get("result"), dict):
                    res = data["result"]
                    return {
                        "bot_id": int(res.get("id") or 0),
                        "username": str(res.get("username") or "").strip(),
                        "first_name": str(res.get("first_name") or "FileStore Bot").strip(),
                    }
        except Exception as exc:
            logger.debug("Bot API getMe HTTP check failed: %s", exc)
        return None

    async def set_filestore_bot(
        self,
        token: str,
        added_by: int | None = None,
        persist: bool = True,
    ) -> dict:
        """
        Validate, start, and save the FileStore bot using its Bot Token (e.g. `/filestore 123456:ABC...`).
        Starts an active wzgram Client (`no_updates=False`) with `/start` deep-link delivery handlers
        so users clicking `https://t.me/<filestore_bot>?start=file_<short_code>` immediately receive
        the file inside the configured FileStore bot.
        """
        clean_token = str(token or "").strip().strip("'\"")
        if not _BOT_TOKEN_RE.match(clean_token):
            raise ValueError(
                "Invalid Bot Token format. Expected token from @BotFather: 123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
            )

        # Resolve bot metadata via Bot API HTTP first (and verify token is valid)
        api_info = await self._fetch_bot_info_via_http(clean_token)

        # Case 1: The provided token is the primary bot token
        if clean_token == settings.BOT_TOKEN:
            if self.filestore_client is not None and self.filestore_client is not self.bot_client:
                old_fs = self.filestore_client
                self.filestore_client = None
                self.clients = [c for c in self.clients if c is not old_fs]
                try:
                    if old_fs.is_connected:
                        await old_fs.stop()
                except Exception:
                    pass

            self.filestore_client = self.bot_client
            me = None
            if self.bot_client and getattr(self.bot_client, "is_connected", False):
                try:
                    me = await self.bot_client.get_me()
                except Exception:
                    me = None
            bot_id = int(getattr(me, "id", 0) or (api_info or {}).get("bot_id") or 0)
            username = (getattr(me, "username", None) or (api_info or {}).get("username") or "").strip().lstrip("@")
            first_name = (getattr(me, "first_name", None) or (api_info or {}).get("first_name") or "FileStore Bot").strip()
            if not username:
                raise ValueError("Could not resolve a public @username for this bot token.")
        else:
            # Case 2: Dedicated FileStore bot token
            # If this bot was previously registered as a no_updates=True helper bot, stop that instance
            bot_prefix_id = int(clean_token.split(":", 1)[0])
            if bot_prefix_id in self.helper_bots:
                old_helper = self.helper_bots.pop(bot_prefix_id, None)
                self.helper_bot_meta.pop(bot_prefix_id, None)
                if old_helper is not None:
                    self.clients = [c for c in self.clients if c is not old_helper]
                    try:
                        if old_helper.is_connected:
                            await old_helper.stop()
                    except Exception:
                        pass

            # Stop any previous dedicated filestore_client
            if self.filestore_client is not None and self.filestore_client is not self.bot_client:
                old_fs = self.filestore_client
                self.filestore_client = None
                self.clients = [c for c in self.clients if c is not old_fs]
                try:
                    if old_fs.is_connected:
                        await old_fs.stop()
                except Exception:
                    pass

            fs_client = Client(
                name=f"hitler_filestore_{bot_prefix_id}",
                api_id=settings.API_ID,
                api_hash=settings.API_HASH,
                bot_token=clean_token,
                in_memory=True,
                workers=32,
                max_concurrent_transmissions=32,
                no_updates=False,  # MUST receive /start deep-link updates!
            )
            if self._filestore_handler_registrar is not None:
                self._filestore_handler_registrar(fs_client)

            await fs_client.start()
            try:
                me = await fs_client.get_me()
                await fs_client.set_bot_commands([
                    BotCommand("start", "Start bot / receive requested file"),
                ])
            except Exception:
                if not api_info:
                    try:
                        await fs_client.stop()
                    except Exception:
                        pass
                    raise
                me = None

            bot_id = int(getattr(me, "id", 0) or (api_info or {}).get("bot_id") or bot_prefix_id)
            username = (getattr(me, "username", None) or (api_info or {}).get("username") or "").strip().lstrip("@")
            first_name = (getattr(me, "first_name", None) or (api_info or {}).get("first_name") or "FileStore Bot").strip()
            if not username:
                try:
                    await fs_client.stop()
                except Exception:
                    pass
                raise ValueError("This Bot Token is valid, but the bot does not have a public @username.")

            self.filestore_client = fs_client
            if all(fs_client is not c for c in self.clients):
                self.clients.append(fs_client)

        now = datetime.datetime.now(datetime.timezone.utc)
        meta = {
            "bot_id": bot_id,
            "username": username,
            "first_name": first_name,
            "token": clean_token,
            "added_by": added_by,
            "updated_at": now,
            "connected": True,
        }
        self.filestore_meta = meta

        if persist:
            await db.site_settings.update_one(
                {"_id": "runtime"},
                {
                    "$set": {
                        "filestore_bot_token": clean_token,
                        "filestore_bot_username": username,
                        "filestore_bot_id": bot_id,
                        "filestore_bot_name": first_name,
                        "filestore_updated_by": added_by,
                        "updated_at": now,
                    }
                },
                upsert=True,
            )
            await settings_col.update_one(
                {"key": "filestore_bot_username"},
                {"$set": {"value": username, "updated_by": added_by, "updated_at": now}},
                upsert=True,
            )

        logger.info("FileStore bot @%s (%s) active and listening for deep-link deliveries", username, bot_id)
        return meta

    async def clear_filestore_bot(self) -> None:
        """Stop dedicated FileStore bot client and remove FileStore configuration from MongoDB."""
        if self.filestore_client is not None and self.filestore_client is not self.bot_client:
            old_fs = self.filestore_client
            self.clients = [c for c in self.clients if c is not old_fs]
            try:
                if old_fs.is_connected:
                    await old_fs.stop()
            except Exception:
                pass
        self.filestore_client = None
        self.filestore_meta = {}
        now = datetime.datetime.now(datetime.timezone.utc)
        await db.site_settings.update_one(
            {"_id": "runtime"},
            {
                "$unset": {
                    "filestore_bot_username": "",
                    "filestore_bot_token": "",
                    "filestore_bot_id": "",
                    "filestore_bot_name": "",
                },
                "$set": {"updated_at": now},
            },
            upsert=True,
        )
        await settings_col.delete_one({"key": "filestore_bot_username"})

    def get_filestore_info(self) -> dict:
        if not self.filestore_meta:
            return {}
        client = self.filestore_client
        return {
            **self.filestore_meta,
            "connected": bool(client and getattr(client, "is_connected", False)),
        }

    async def load_saved_helper_bots(self) -> int:
        """Load and start all helper bot tokens stored in MongoDB db.helper_bots and HELPER_BOT_TOKENS."""
        started_count = 0
        tokens_to_load: list[str] = []

        runtime = await db.site_settings.find_one({"_id": "runtime"}) or {}
        fs_token = str(runtime.get("filestore_bot_token") or "").strip()

        env_tokens = os.environ.get("HELPER_BOT_TOKENS", "") or os.environ.get("MULTI_TOKENS", "")
        for raw in re.split(r"[,;\s]+", env_tokens):
            tok = raw.strip().strip("'\"")
            if tok and tok not in (settings.BOT_TOKEN, fs_token) and tok not in tokens_to_load:
                tokens_to_load.append(tok)

        try:
            await db.helper_bots.create_index("bot_id", unique=True)
            cursor = db.helper_bots.find({}, {"token": 1, "bot_id": 1})
            async for doc in cursor:
                tok = str(doc.get("token") or "").strip()
                if tok and tok not in (settings.BOT_TOKEN, fs_token) and tok not in tokens_to_load:
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
        if not _BOT_TOKEN_RE.match(clean_token):
            raise ValueError("Invalid bot token format. Expected format: 123456789:ABCdefGHI...")

        if clean_token == settings.BOT_TOKEN:
            raise ValueError("This token is already running as the primary bot.")

        if self.filestore_meta.get("token") == clean_token and self.filestore_client is not None:
            return self.filestore_meta

        # Check if already running
        for bot_id, meta in self.helper_bot_meta.items():
            if meta.get("token") == clean_token and bot_id in self.helper_bots:
                return meta

        bot_prefix = clean_token.split(":", 1)[0]
        client = Client(
            name=f"hitler_helper_{bot_prefix}_{len(self.helper_bots) + 1}",
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
        unique = {
            id(c): c
            for c in [*self.clients, *self.helper_bots.values(), self.filestore_client, self.bot_client]
            if c is not None
        }
        for client in unique.values():
            try:
                if client.is_connected:
                    await client.stop()
            except Exception:
                logger.exception("Error while stopping wzgram client")
        self.clients.clear()
        self.helper_bots.clear()
        self.helper_bot_meta.clear()
        self.filestore_client = None
        self.filestore_meta.clear()
        self.bot_client = None
        self._started = False

    async def register_bot_commands(self) -> None:
        """Register all bot slash commands with Telegram on startup."""
        if not self.bot_client:
            return
        commands = [
            BotCommand("start", "Start / welcome & how to use"),
            BotCommand("filestore", "Set FileStore bot token for deep-link downloads (admin)"),
            BotCommand("addbots", "Add unlimited helper bot tokens for speed boost (admin)"),
            BotCommand("panel", "Open control panel (admin)"),
            BotCommand("manage", "Edit website pages and bot settings (admin)"),
            BotCommand("baseurl", "Set public base / Cloudflare URL (admin)"),
            BotCommand("backendurl", "Set backend / Cloudflare URL (admin)"),
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
        for client in [*self.clients, *self.helper_bots.values(), self.filestore_client, self.bot_client]:
            if client is not None and getattr(client, "is_connected", False) and all(client is not x for x in result):
                result.append(client)
        return result


session_manager = SessionManager()
