"""Shared browser-panel authentication backed by MongoDB settings."""
import hashlib
import hmac
import secrets
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from app.database.connection import settings, settings_col

security = HTTPBasic(auto_error=False)
_ITERATIONS = 310_000


def make_password_hash(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS).hex()
    return salt, digest


async def verify_panel_credentials(credentials: HTTPBasicCredentials | None) -> bool:
    if credentials is None:
        return False
    username_doc = await settings_col.find_one({"key": "panel_username"}, {"value": 1})
    salt_doc = await settings_col.find_one({"key": "panel_password_salt"}, {"value": 1})
    hash_doc = await settings_col.find_one({"key": "panel_password_hash"}, {"value": 1})
    expected_user = str(username_doc.get("value")) if username_doc else "admin"
    if salt_doc and hash_doc:
        expected_hash = str(hash_doc.get("value", ""))
        salt = str(salt_doc.get("value", ""))
        try:
            _, actual_hash = make_password_hash(credentials.password, salt)
        except (ValueError, TypeError):
            return False
        password_valid = hmac.compare_digest(actual_hash, expected_hash)
    else:
        # Backwards-compatible first-run password from the deployment secret.
        token = settings.ADMIN_PANEL_TOKEN
        password_valid = bool(token) and hmac.compare_digest(credentials.password, token)
    return hmac.compare_digest(credentials.username, expected_user) and password_valid


async def require_panel_admin(credentials: HTTPBasicCredentials | None = Depends(security)):
    if not await verify_panel_credentials(credentials):
        raise HTTPException(401, "Invalid panel username or password", headers={"WWW-Authenticate": "Basic realm=HITLER-YT-Panel"})
    return True
