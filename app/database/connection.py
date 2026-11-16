from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
import logging
import os
from urllib.parse import urlparse

from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.utils.helpers import (
    is_trycloudflare_url,
    is_vercel_url,
    normalize_public_base_url,
    rewrite_page_groups_to_cloudflare,
)


class Settings(BaseSettings):
    MONGODB_URI: Optional[str] = None
    DATABASE_URL: Optional[str] = None
    DATABASE_NAME: str = "tg_media_bot"
    REDIS_URL: str = "redis://localhost:6379/0"
    API_ID: int
    API_HASH: str
    BOT_TOKEN: str
    OWNER_ID: int
    ADMINS: str = ""
    FORCE_SUB_CHANNELS: str = ""
    BASE_URL: str = "http://localhost:8000"
    BACKEND_URL: Optional[str] = None
    WEBSITE_URL: Optional[str] = None
    CHANNEL_ID: Optional[int] = None
    UPDATE_CHANNEL_URL: str = "https://t.me/HiTLERYT"
    PORT: int = 8000
    DEBUG: bool = False
    SESSIONS: str = ""
    ADMIN_PANEL_TOKEN: str = ""
    TMDB_API_KEY: str = ""
    STREAM_CHUNK_SIZE: int = 4194304

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=True)

    @field_validator("CHANNEL_ID", mode="before")
    @classmethod
    def parse_optional_int(cls, value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            cleaned = value.split("#", 1)[0].strip()
            return int(cleaned) if cleaned else None
        raise ValueError("CHANNEL_ID must be an integer")

    @field_validator("BASE_URL")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            value = "https://" + value
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("BASE_URL must be a valid http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("BASE_URL cannot contain credentials, query, or fragment")
        return value

    @field_validator("STREAM_CHUNK_SIZE")
    @classmethod
    def validate_chunk_size(cls, value: int) -> int:
        if not 64 * 1024 <= value <= 4 * 1024 * 1024:
            raise ValueError("STREAM_CHUNK_SIZE must be between 65536 and 4194304")
        return value

    @property
    def mongo_uri(self) -> str:
        return self.MONGODB_URI or self.DATABASE_URL or "mongodb://localhost:27017"

    @property
    def admin_list(self) -> list[int]:
        return [int(x.strip()) for x in self.ADMINS.split(",") if x.strip()]

    @property
    def fsub_list(self) -> list[int]:
        return [int(x.strip()) for x in self.FORCE_SUB_CHANNELS.split(",") if x.strip()]


logger = logging.getLogger(__name__)
settings = Settings()
client = AsyncIOMotorClient(
    settings.mongo_uri,
    serverSelectionTimeoutMS=10000,
    connectTimeoutMS=10000,
    maxPoolSize=100,
    minPoolSize=5,
)
db = client[settings.DATABASE_NAME]
files_col = db.files
users_col = db.users
settings_col = db.settings
logs_col = db.logs


async def create_indexes() -> None:
    await files_col.create_index("short_code", unique=True)
    await files_col.create_index("file_id")
    await files_col.create_index("uploader_id")
    try:
        await files_col.drop_index("expiry_time_1")
    except Exception as exc:
        if "index not found" not in str(exc).lower() and "ns not found" not in str(exc).lower():
            logger.warning("Could not remove legacy expiry TTL index: %s", exc)
    await users_col.create_index("user_id", unique=True)
    await settings_col.create_index("key", unique=True)


def _valid_public_candidate(value: str | None) -> str | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return normalize_public_base_url(value)
    except ValueError:
        return None


async def get_active_cloudflare_url() -> str | None:
    """
    Resolve the active Cloudflare tunnel URL (https://*.trycloudflare.com) used exclusively
    for download links (/dl/), stream/watch links (/watch/, /stream/, /remux/), and catalog button URLs.
    """
    try:
        rows = await settings_col.find(
            {"key": {"$in": ["cloudflare_url", "backend_url", "base_url"]}},
            {"key": 1, "value": 1},
        ).to_list(10)
        stored = {r["key"]: r.get("value") for r in rows if isinstance(r, dict) and "key" in r}
    except Exception:
        stored = {}

    candidates = [
        stored.get("cloudflare_url"),
        stored.get("backend_url"),
        os.environ.get("CLOUDFLARE_URL"),
        settings.BACKEND_URL,
        stored.get("base_url"),
        settings.BASE_URL,
    ]

    valid_candidates = []
    for c in candidates:
        norm = _valid_public_candidate(str(c) if c else None)
        if norm and norm not in valid_candidates:
            valid_candidates.append(norm)

    # 1. Always prefer any *.trycloudflare.com URL
    for c in valid_candidates:
        if is_trycloudflare_url(c):
            return c

    # 2. Next prefer any non-Vercel backend URL
    for c in valid_candidates:
        if not is_vercel_url(c):
            return c

    return valid_candidates[0] if valid_candidates else None


async def get_active_vercel_website_url() -> str | None:
    """
    Resolve the Vercel website URL strictly for the website (catalog, search, title pages, panel).
    """
    try:
        rows = await settings_col.find(
            {"key": {"$in": ["vercel_url", "website_urls", "base_url"]}},
            {"key": 1, "value": 1},
        ).to_list(10)
        stored = {r["key"]: r.get("value") for r in rows if isinstance(r, dict) and "key" in r}
    except Exception:
        stored = {}

    candidates = [
        stored.get("vercel_url"),
        settings.WEBSITE_URL,
        os.getenv("WEBSITE_URL"),
    ]
    website_urls = stored.get("website_urls")
    if isinstance(website_urls, list):
        for u in website_urls:
            if not is_trycloudflare_url(str(u)):
                candidates.append(u)
    base_val = stored.get("base_url") or settings.BASE_URL
    if base_val and not is_trycloudflare_url(str(base_val)):
        candidates.append(base_val)

    for c in candidates:
        norm = _valid_public_candidate(str(c) if c else None)
        if norm and not is_trycloudflare_url(norm):
            return norm
    return None


async def get_active_website_url() -> str | None:
    """
    Return the active Vercel website URL if configured, otherwise fallback to any configured public URL.
    """
    vercel_site = await get_active_vercel_website_url()
    if vercel_site:
        return vercel_site
    cf_url = await get_active_cloudflare_url()
    if cf_url:
        return cf_url
    return settings.BASE_URL.rstrip("/")


async def load_custom_settings() -> None:
    """Load and synchronize runtime URLs and shared secrets with MongoDB so VPS and Vercel share one source of truth."""
    now = datetime.now(timezone.utc)
    for key, attribute in (("base_url", "BASE_URL"), ("backend_url", "BACKEND_URL")):
        custom = await settings_col.find_one({"key": key}, {"value": 1})
        if custom and custom.get("value"):
            candidate = _valid_public_candidate(str(custom["value"]))
            if candidate:
                setattr(settings, attribute, candidate)
            else:
                logger.warning("Ignoring invalid persisted %s; retaining environment configuration", key)

    cf_doc = await settings_col.find_one({"key": "cloudflare_url"}, {"value": 1})
    if cf_doc and cf_doc.get("value"):
        cf_candidate = _valid_public_candidate(str(cf_doc["value"]))
        if cf_candidate:
            settings.BACKEND_URL = cf_candidate

    env_base = _valid_public_candidate(settings.BASE_URL)
    env_backend = _valid_public_candidate(settings.BACKEND_URL)
    active_cf = None

    if env_backend:
        await settings_col.update_one(
            {"key": "backend_url"},
            {"$set": {"value": env_backend, "updated_at": now}},
            upsert=True,
        )
        if is_trycloudflare_url(env_backend):
            active_cf = env_backend
            await settings_col.update_one(
                {"key": "cloudflare_url"},
                {"$set": {"value": env_backend, "updated_at": now}},
                upsert=True,
            )

    if env_base:
        await settings_col.update_one(
            {"key": "base_url"},
            {"$set": {"value": env_base, "updated_at": now}},
            upsert=True,
        )
        if is_trycloudflare_url(env_base):
            active_cf = env_base
            await settings_col.update_one(
                {"key": "cloudflare_url"},
                {"$set": {"value": env_base, "updated_at": now}},
                upsert=True,
            )
            if not env_backend:
                settings.BACKEND_URL = env_base
                await settings_col.update_one(
                    {"key": "backend_url"},
                    {"$set": {"value": env_base, "updated_at": now}},
                    upsert=True,
                )
        elif is_vercel_url(env_base):
            await settings_col.update_one(
                {"key": "vercel_url"},
                {"$set": {"value": env_base, "updated_at": now}},
                upsert=True,
            )

    # Clean any *.trycloudflare.com entries out of website_urls so website_urls stays strictly for Vercel
    web_doc = await settings_col.find_one({"key": "website_urls"}, {"value": 1})
    if web_doc and isinstance(web_doc.get("value"), list):
        cleaned = [u for u in web_doc["value"] if not is_trycloudflare_url(str(u))]
        if cleaned != web_doc["value"]:
            await settings_col.update_one(
                {"key": "website_urls"},
                {"$set": {"value": cleaned, "updated_at": now}},
            )

    # If an active Cloudflare URL is present on startup, ensure all catalog button URLs use it
    if active_cf:
        cursor = db.catalog_pages.find({"groups.0": {"$exists": True}}, {"_id": 1, "groups": 1})
        async for doc in cursor:
            old_groups = doc.get("groups") or []
            new_groups = rewrite_page_groups_to_cloudflare(old_groups, active_cf)
            if new_groups != old_groups:
                await db.catalog_pages.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"groups": new_groups, "updated_at": now}},
                )

    if settings.TMDB_API_KEY and settings.TMDB_API_KEY.strip():
        await settings_col.update_one(
            {"key": "tmdb_api_key"},
            {"$set": {"value": settings.TMDB_API_KEY.strip(), "updated_at": now}},
            upsert=True,
        )
    if settings.ADMIN_PANEL_TOKEN and settings.ADMIN_PANEL_TOKEN.strip():
        await settings_col.update_one(
            {"key": "admin_panel_token"},
            {"$set": {"value": settings.ADMIN_PANEL_TOKEN.strip(), "updated_at": now}},
            upsert=True,
        )
    if settings.UPDATE_CHANNEL_URL and settings.UPDATE_CHANNEL_URL.strip():
        await settings_col.update_one(
            {"key": "update_channel_url"},
            {"$set": {"value": settings.UPDATE_CHANNEL_URL.strip(), "updated_at": now}},
            upsert=True,
        )
