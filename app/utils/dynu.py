"""Dynu DDNS & REST API v2 updater, OAuth2 token manager, secure credential storage, and verification helpers.

Official Dynu references:
- REST API v2: https://www.dynu.com/en-US/Support/API
- IP Update Protocol: https://www.dynu.com/en-US/DynamicDNS/IP-Update-Protocol
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from app.database.connection import settings, settings_col

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_stop = False
_maintenance_until: float = 0.0

DYNU_API_V2_BASE = "https://api.dynu.com/v2"
DYNU_OAUTH_TOKEN_URL = "https://api.dynu.com/v2/oauth2/token"
DYNU_NIC_UPDATE_URL = "https://api.dynu.com/nic/update"
USER_AGENT = "AniMovieszoneflix-Dynu/3.0"

_IP_ENDPOINTS = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
    "https://checkip.amazonaws.com",
)

DYNU_ALL_SETTINGS_KEYS = (
    "dynu_method",
    "dynu_hostname",
    "dynu_api_key",
    "dynu_oauth_client_id",
    "dynu_oauth_client_secret",
    "dynu_oauth_token",
    "dynu_oauth_expires_at",
    "dynu_username",
    "dynu_password",
    "dynu_url",
    "dynu_last_status",
)

# Official IP Update Protocol response code descriptions
_NIC_CODE_MESSAGES = {
    "good": "IP address updated successfully.",
    "nochg": "IP address is already up to date (unchanged).",
    "badauth": "Authentication failed: invalid Dynu username or DDNS/IP update password.",
    "nohost": "Hostname or username not found in your Dynu account.",
    "notfqdn": "Hostname is not a valid fully qualified domain name (FQDN).",
    "numhost": "Too many hostnames specified (maximum 20 per request).",
    "abuse": "Update blocked due to abusive/excessive update frequency.",
    "dnserr": "Dynu server-side DNS error; retry later.",
    "911": "Dynu scheduled maintenance (911); updates suspended for 10 minutes.",
    "servererror": "Dynu server-side error encountered; retry later.",
    "!donator": "Requested feature is only available to Dynu members.",
}


class DynuAPIError(Exception):
    """Structured error from Dynu REST API v2 or IP Update Protocol."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_type: str | None = None,
        code: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type
        self.code = code
        self.retry_after = retry_after


# ── Secure Credential Storage (AES-256-GCM via pycryptodome) ─────────────────

def _derive_encryption_key() -> bytes:
    secret_parts = [
        str(getattr(settings, "ADMIN_PANEL_TOKEN", "") or os.getenv("ADMIN_PANEL_TOKEN", "")).strip(),
        str(getattr(settings, "BOT_TOKEN", "") or os.getenv("BOT_TOKEN", "")).strip(),
        str(getattr(settings, "API_HASH", "") or os.getenv("API_HASH", "")).strip(),
    ]
    material = "|".join(p for p in secret_parts if p) or "animovieszoneflix-fallback-key"
    return hashlib.sha256(b"animovieszoneflix-dynu-cred-v1:" + material.encode("utf-8")).digest()


def encrypt_secret(plaintext: str | None) -> str:
    val = (plaintext or "").strip()
    if not val:
        return ""
    if val.startswith(("enc:v1:", "enc:v1-fb:")):
        return val
    key = _derive_encryption_key()
    try:
        from Crypto.Cipher import AES  # type: ignore

        cipher = AES.new(key, AES.MODE_GCM)
        ct, tag = cipher.encrypt_and_digest(val.encode("utf-8"))
        blob = cipher.nonce + tag + ct
        return "enc:v1:" + base64.urlsafe_b64encode(blob).decode("ascii")
    except Exception:
        # Authenticated stream fallback if pycryptodome is unavailable
        nonce = os.urandom(16)
        raw = val.encode("utf-8")
        stream = b"".join(
            hashlib.sha256(key + nonce + i.to_bytes(4, "big")).digest()
            for i in range((len(raw) + 31) // 32)
        )[: len(raw)]
        ct = bytes(a ^ b for a, b in zip(raw, stream))
        mac = hmac.new(key, nonce + ct, hashlib.sha256).digest()
        return "enc:v1-fb:" + base64.urlsafe_b64encode(nonce + mac + ct).decode("ascii")


def decrypt_secret(stored: str | None) -> str:
    val = str(stored or "").strip()
    if not val:
        return ""
    key = _derive_encryption_key()
    if val.startswith("enc:v1:"):
        try:
            from Crypto.Cipher import AES  # type: ignore

            raw = base64.urlsafe_b64decode(val[len("enc:v1:") :].encode("ascii"))
            nonce, tag, ct = raw[:16], raw[16:32], raw[32:]
            cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
            return cipher.decrypt_and_verify(ct, tag).decode("utf-8")
        except Exception as exc:
            logger.warning("Failed to decrypt Dynu AES-GCM secret: %s", exc)
            return ""
    if val.startswith("enc:v1-fb:"):
        try:
            raw = base64.urlsafe_b64decode(val[len("enc:v1-fb:") :].encode("ascii"))
            nonce, mac, ct = raw[:16], raw[16:48], raw[48:]
            expected = hmac.new(key, nonce + ct, hashlib.sha256).digest()
            if not hmac.compare_digest(mac, expected):
                return ""
            stream = b"".join(
                hashlib.sha256(key + nonce + i.to_bytes(4, "big")).digest()
                for i in range((len(ct) + 31) // 32)
            )[: len(ct)]
            return bytes(a ^ b for a, b in zip(ct, stream)).decode("utf-8")
        except Exception as exc:
            logger.warning("Failed to decrypt Dynu fallback secret: %s", exc)
            return ""
    # Legacy plaintext compatibility
    return val


def mask_secret(secret: str | None) -> str:
    val = (secret or "").strip()
    if not val:
        return "not set"
    if len(val) <= 6:
        return "****"
    return f"{val[:3]}****{val[-3:]}"


# ── Hostname, Public IP & DNS Verification Helpers ───────────────────────────

def normalize_dynu_host(raw: str | None) -> str:
    val = (raw or "").strip().lower()
    if val.startswith(("http://", "https://")):
        val = urllib.parse.urlparse(val).hostname or ""
    if "/" in val:
        val = val.split("/", 1)[0]
    if ":" in val:
        val = val.split(":", 1)[0]
    return val.strip().strip(".")


def is_valid_fqdn(host: str | None) -> bool:
    h = normalize_dynu_host(host)
    if not h or "." not in h or len(h) > 253:
        return False
    labels = h.split(".")
    if any(not lbl or len(lbl) > 63 for lbl in labels):
        return False
    for lbl in labels:
        if lbl.startswith("-") or lbl.endswith("-"):
            return False
        if not all(c.isalnum() or c == "-" for c in lbl):
            return False
    return True


def _public_ip() -> str | None:
    for url in _IP_ENDPOINTS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=6) as r:
                ip = r.read().decode().strip()
            ipaddress.ip_address(ip)
            return ip
        except Exception:
            continue
    return None


async def get_public_ip() -> str | None:
    return await asyncio.to_thread(_public_ip)


def _resolve_dns_ips_sync(hostname: str) -> list[str]:
    ips: list[str] = []
    for family in (socket.AF_INET, socket.AF_UNSPEC):
        try:
            infos = socket.getaddrinfo(hostname, None, family, socket.SOCK_STREAM)
            for info in infos:
                addr = info[4][0]
                if addr and addr not in ips:
                    ips.append(addr)
        except Exception:
            continue
    return ips


async def verify_dns_ip(hostname: str, expected_ip: str | None = None) -> dict[str, Any]:
    host = normalize_dynu_host(hostname)
    if not host:
        return {"resolved_ips": [], "expected_ip": expected_ip, "matches": False}
    resolved = await asyncio.to_thread(_resolve_dns_ips_sync, host)
    matches = bool(expected_ip and expected_ip in resolved)
    return {
        "resolved_ips": resolved,
        "expected_ip": expected_ip,
        "matches": matches,
    }


# ── Configuration Persistence ────────────────────────────────────────────────

async def save_dynu_setting(key: str, value: Any, *, encrypt: bool = False, updated_by: int | None = None) -> None:
    stored_val = encrypt_secret(str(value)) if encrypt else value
    doc: dict[str, Any] = {
        "value": stored_val,
        "encrypted": bool(encrypt),
        "updated_at": datetime.datetime.utcnow(),
    }
    if updated_by is not None:
        doc["updated_by"] = updated_by
    await settings_col.update_one({"key": key}, {"$set": doc}, upsert=True)


async def clear_dynu_config() -> None:
    await settings_col.delete_many({"key": {"$in": list(DYNU_ALL_SETTINGS_KEYS)}})


async def get_full_config() -> dict[str, Any]:
    try:
        rows = await settings_col.find({"key": {"$in": list(DYNU_ALL_SETTINGS_KEYS)}}).to_list(length=30)
        docs = {d["key"]: d.get("value") for d in rows if isinstance(d, dict) and "key" in d}
    except Exception:
        docs = {}

    host = normalize_dynu_host(docs.get("dynu_hostname"))
    api_key = decrypt_secret(docs.get("dynu_api_key"))
    oauth_client_id = decrypt_secret(docs.get("dynu_oauth_client_id"))
    oauth_client_secret = decrypt_secret(docs.get("dynu_oauth_client_secret"))
    oauth_token = decrypt_secret(docs.get("dynu_oauth_token"))
    oauth_expires_at = float(docs.get("dynu_oauth_expires_at") or 0.0)
    username = str(docs.get("dynu_username") or "").strip()
    password = decrypt_secret(docs.get("dynu_password"))
    method = str(docs.get("dynu_method") or "").strip().lower()

    if method not in ("api_key", "oauth2", "ddns"):
        if api_key:
            method = "api_key"
        elif oauth_client_id and oauth_client_secret:
            method = "oauth2"
        elif username and password:
            method = "ddns"
        else:
            method = ""

    return {
        "method": method,
        "hostname": host,
        "api_key": api_key,
        "oauth_client_id": oauth_client_id,
        "oauth_client_secret": oauth_client_secret,
        "oauth_token": oauth_token,
        "oauth_expires_at": oauth_expires_at,
        "username": username,
        "password": password,
        "dynu_url": str(docs.get("dynu_url") or "").strip(),
        "last_status": docs.get("dynu_last_status") if isinstance(docs.get("dynu_last_status"), dict) else {},
    }



def normalize_dns_provider(raw: str | None) -> str:
    val = (raw or "").strip().lower()
    if val in ("duck", "duckdns", "duckdns.org"):
        return "duckdns"
    if val in ("dynu", "dyno", "dynu.com", "dynu.net"):
        return "dynu"
    if val in ("both", "all"):
        return "both"
    if val in ("none", "off", "disabled", "false", "0"):
        return "none"
    return "auto"


def get_active_dns_provider() -> str:
    raw = os.getenv("DNS_PROVIDER") or os.getenv("dns") or os.getenv("DNS") or getattr(settings, "DNS_PROVIDER", "auto")
    return normalize_dns_provider(str(raw))


def is_dynu_enabled_for_deployment() -> bool:
    return get_active_dns_provider() in ("dynu", "both", "auto")


async def get_dynu_config() -> dict[str, Any]:
    """Backward-compatible helper returning Dynu config dict."""
    cfg = await get_full_config()
    return {"domain": cfg["hostname"], **cfg}


async def get_config() -> tuple[str, str, str]:
    """Backward-compatible helper returning (hostname, username, password)."""
    cfg = await get_full_config()
    return cfg["hostname"], cfg["username"], cfg["password"]


def is_method_ready(cfg: dict[str, Any]) -> tuple[bool, str]:
    host = cfg.get("hostname") or ""
    method = cfg.get("method") or ""
    if not host:
        return False, "Missing hostname. Set it with `/dynuhost <hostname>`."
    if method == "api_key":
        if not cfg.get("api_key"):
            return False, "Missing API Key. Set it with `/dynukey <api_key>`."
        return True, ""
    if method == "oauth2":
        if not cfg.get("oauth_client_id") or not cfg.get("oauth_client_secret"):
            return False, "Missing OAuth2 credentials. Set them with `/dynuoauth <client_id> <client_secret>`."
        return True, ""
    if method == "ddns":
        if not cfg.get("username") or not cfg.get("password"):
            return False, "Missing DDNS username/password. Set them with `/dynuuser <username>` and `/dynupass <password>`."
        return True, ""
    return False, (
        "No Dynu authentication method configured. Use `/dynukey`, `/dynuoauth`, or `/dynuuser` + `/dynupass`."
    )


# ── HTTP / REST API v2 Helpers ───────────────────────────────────────────────

def _parse_api_v2_error(status: int, raw_body: str) -> DynuAPIError:
    try:
        payload = json.loads(raw_body) if raw_body else {}
    except Exception:
        payload = {}
    err_type = str(payload.get("type") or "").strip()
    err_msg = str(payload.get("message") or "").strip()
    api_code = payload.get("statusCode") or status
    if status == 401 or api_code == 401:
        msg = err_msg or "Authentication failed (HTTP 401). Check your API Key or OAuth2 Client ID/Secret."
    elif err_type or err_msg:
        msg = f"{err_type}: {err_msg}".strip(": ") + f" (code {api_code})"
    else:
        msg = f"Dynu API HTTP {status}: {raw_body[:200] or 'empty error response'}"
    return DynuAPIError(msg, status_code=int(api_code or status), error_type=err_type or None)


def _http_json_sync(
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req_headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        **headers,
    }
    if data is not None:
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace").strip()
            return json.loads(raw) if raw else {"statusCode": resp.status}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace").strip()
        raise _parse_api_v2_error(exc.code, raw) from exc
    except urllib.error.URLError as exc:
        raise DynuAPIError(f"Network error contacting Dynu API: {exc.reason}") from exc
    except Exception as exc:
        raise DynuAPIError(f"Dynu API request failed: {exc}") from exc


def _fetch_oauth2_token_sync(client_id: str, client_secret: str) -> tuple[str, int]:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    payload = _http_json_sync(
        "GET",
        DYNU_OAUTH_TOKEN_URL,
        headers={"Authorization": f"Basic {basic}"},
        timeout=20,
    )
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise DynuAPIError("Dynu OAuth2 response did not include access_token.")
    expires_in = int(payload.get("expires_in") or 28800)
    return token, expires_in


async def get_oauth2_access_token(
    cfg: dict[str, Any],
    *,
    force_refresh: bool = False,
    updated_by: int | None = None,
) -> str:
    client_id = cfg.get("oauth_client_id") or ""
    client_secret = cfg.get("oauth_client_secret") or ""
    if not client_id or not client_secret:
        raise DynuAPIError("OAuth2 Client ID and Secret are required.")

    now = time.time()
    cached_token = str(cfg.get("oauth_token") or "").strip()
    expires_at = float(cfg.get("oauth_expires_at") or 0.0)
    # Refresh 120s before expiry (Dynu tokens expire in 8 hours / 28800s)
    if not force_refresh and cached_token and expires_at > (now + 120):
        return cached_token

    token, expires_in = await asyncio.to_thread(_fetch_oauth2_token_sync, client_id, client_secret)
    new_expires_at = now + max(60, expires_in)
    cfg["oauth_token"] = token
    cfg["oauth_expires_at"] = new_expires_at
    await save_dynu_setting("dynu_oauth_token", token, encrypt=True, updated_by=updated_by)
    await save_dynu_setting("dynu_oauth_expires_at", new_expires_at, encrypt=False, updated_by=updated_by)
    return token


async def _rest_api_call(
    cfg: dict[str, Any],
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    updated_by: int | None = None,
) -> dict[str, Any]:
    url = f"{DYNU_API_V2_BASE}{path}"
    auth_method = cfg.get("method")
    if auth_method == "api_key":
        headers = {"API-Key": cfg["api_key"]}
        return await asyncio.to_thread(_http_json_sync, method, url, headers, body)

    if auth_method == "oauth2":
        token = await get_oauth2_access_token(cfg, force_refresh=False, updated_by=updated_by)
        try:
            return await asyncio.to_thread(
                _http_json_sync, method, url, {"Authorization": f"Bearer {token}"}, body
            )
        except DynuAPIError as exc:
            if exc.status_code == 401:
                token = await get_oauth2_access_token(cfg, force_refresh=True, updated_by=updated_by)
                return await asyncio.to_thread(
                    _http_json_sync, method, url, {"Authorization": f"Bearer {token}"}, body
                )
            raise

    raise DynuAPIError(f"Unsupported REST authentication method: {auth_method}")


async def _update_via_rest_api(
    cfg: dict[str, Any],
    host: str,
    ip: str,
    *,
    updated_by: int | None = None,
) -> tuple[bool, str]:
    # 1. Verify connectivity with /v2/ping (optional fast check if needed) & resolve root domain
    try:
        root_info = await _rest_api_call(
            cfg, "GET", f"/dns/getroot/{urllib.parse.quote(host)}", updated_by=updated_by
        )
        domain_id = root_info.get("id")
        domain_name = normalize_dynu_host(root_info.get("domainName") or host)
        node = str(root_info.get("node") or "").strip().lower()
    except DynuAPIError as exc:
        if exc.status_code == 401:
            raise
        # Fallback: list all domains and match host
        listing = await _rest_api_call(cfg, "GET", "/dns", updated_by=updated_by)
        domains = listing.get("domains") or []
        matched = None
        for d in domains:
            dname = normalize_dynu_host(d.get("name"))
            if host == dname or host.endswith("." + dname):
                matched = d
                break
        if not matched:
            return False, f"Hostname `{host}` not found in your Dynu DNS account ({exc.message})."
        domain_id = matched.get("id")
        domain_name = normalize_dynu_host(matched.get("name"))
        node = "" if host == domain_name else host[: -(len(domain_name) + 1)]

    if not domain_id:
        return False, f"Dynu did not return a domain ID for `{host}`."

    # 2. Fetch full domain details so existing flags (TTL, wildcard, IPv6, DNSSEC) are preserved
    details = await _rest_api_call(cfg, "GET", f"/dns/{domain_id}", updated_by=updated_by)

    # If hostname is the root domain itself, update domain IPv4 via POST /v2/dns/{id}
    if not node or host == domain_name:
        current_ip = str(details.get("ipv4Address") or "").strip()
        if current_ip == ip and details.get("ipv4") is True:
            return True, f"nochg {ip} ({cfg['method']})"
        payload = {
            "name": details.get("name") or domain_name,
            "group": details.get("group") or "",
            "ipv4Address": ip,
            "ipv6Address": details.get("ipv6Address"),
            "ttl": int(details.get("ttl") or 90),
            "ipv4": True,
            "ipv6": bool(details.get("ipv6", False)),
            "ipv4WildcardAlias": bool(details.get("ipv4WildcardAlias", False)),
            "ipv6WildcardAlias": bool(details.get("ipv6WildcardAlias", False)),
            "allowZoneTransfer": bool(details.get("allowZoneTransfer", False)),
            "dnssec": bool(details.get("dnssec", False)),
        }
        await _rest_api_call(cfg, "POST", f"/dns/{domain_id}", body=payload, updated_by=updated_by)
        return True, f"good {ip} ({cfg['method']})"

    # 3. Subdomain / node under a Dynu domain: update or create A record
    records_resp = await _rest_api_call(cfg, "GET", f"/dns/{domain_id}/record", updated_by=updated_by)
    records = records_resp.get("dnsRecords") or records_resp.get("records") or []
    a_rec = next(
        (
            r
            for r in records
            if str(r.get("recordType") or "").upper() == "A"
            and str(r.get("nodeName") or "").strip().lower() == node
        ),
        None,
    )
    if a_rec:
        if str(a_rec.get("ipv4Address") or "").strip() == ip and a_rec.get("state") is True:
            return True, f"nochg {ip} ({cfg['method']} record)"
        rec_id = a_rec.get("id")
        rec_payload = {
            "nodeName": node,
            "recordType": "A",
            "ttl": int(a_rec.get("ttl") or 90),
            "state": True,
            "group": a_rec.get("group") or "",
            "ipv4Address": ip,
        }
        await _rest_api_call(
            cfg, "POST", f"/dns/{domain_id}/record/{rec_id}", body=rec_payload, updated_by=updated_by
        )
        return True, f"good {ip} ({cfg['method']} record)"

    create_payload = {
        "nodeName": node,
        "recordType": "A",
        "ttl": 90,
        "state": True,
        "group": "",
        "ipv4Address": ip,
    }
    await _rest_api_call(cfg, "POST", f"/dns/{domain_id}/record", body=create_payload, updated_by=updated_by)
    return True, f"good {ip} ({cfg['method']} record created)"


# ── DDNS IP Update Protocol (/nic/update) ────────────────────────────────────

def _nic_update_sync(host: str, user: str, password: str, ip: str) -> tuple[bool, str, int | None]:
    params: dict[str, str] = {"hostname": host}
    if ip:
        params["myip"] = ip
    url = f"{DYNU_NIC_UPDATE_URL}?{urllib.parse.urlencode(params)}"
    basic = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Basic {basic}",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            result = r.read().decode("utf-8", errors="replace").strip()
    except urllib.error.HTTPError as e:
        result = e.read().decode("utf-8", errors="replace").strip() or f"HTTP {e.code}"
    except Exception as e:
        return False, f"Network error contacting Dynu DDNS endpoint: {e}", None

    code = result.split()[0].lower() if result else ""
    if code in ("good", "nochg"):
        return True, result, None
    if code == "911":
        return False, f"911 — {_NIC_CODE_MESSAGES['911']}", 600
    desc = _NIC_CODE_MESSAGES.get(code)
    if desc:
        return False, f"{result} — {desc}", None
    return False, result or "Empty response from Dynu DDNS endpoint", None


# ── Unified Update & Active URL Probing ──────────────────────────────────────

async def update_once(
    host: str | None = None,
    user: str | None = None,
    password: str | None = None,
    ip: str | None = None,
    *,
    cfg: dict[str, Any] | None = None,
    updated_by: int | None = None,
) -> tuple[bool, str, str | None]:
    global _maintenance_until

    if cfg is None:
        cfg = await get_full_config()

    target_host = normalize_dynu_host(host or cfg.get("hostname"))
    if user is not None and password is not None:
        # Direct DDNS invocation
        cfg = {**cfg, "method": "ddns", "hostname": target_host, "username": user.strip(), "password": password.strip()}
    else:
        cfg = {**cfg, "hostname": target_host}

    ready, err_msg = is_method_ready(cfg)
    if not ready:
        return False, err_msg, None

    if time.time() < _maintenance_until:
        remaining = int(_maintenance_until - time.time())
        return False, f"Dynu 911 maintenance backoff active ({remaining}s remaining).", None

    resolved_ip = (ip or await get_public_ip() or "").strip()
    if not resolved_ip:
        return False, "Could not detect VPS public IPv4 address.", None

    method = cfg["method"]
    try:
        if method in ("api_key", "oauth2"):
            ok, msg = await _update_via_rest_api(cfg, target_host, resolved_ip, updated_by=updated_by)
            return ok, msg, resolved_ip
        ok, msg, retry_after = await asyncio.to_thread(
            _nic_update_sync, target_host, cfg["username"], cfg["password"], resolved_ip
        )
        if retry_after:
            _maintenance_until = time.time() + retry_after
        return ok, msg, resolved_ip
    except DynuAPIError as exc:
        return False, exc.message, resolved_ip
    except Exception as exc:
        return False, str(exc), resolved_ip


def _probe_url_sync(url: str) -> tuple[bool, int | None, str]:
    req = urllib.request.Request(url.rstrip("/") + "/health", headers={"User-Agent": "Dynu-Probe/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            status = int(getattr(r, "status", 200))
            body = r.read(256).decode("utf-8", errors="replace").strip()
            return 200 <= status < 400, status, body
    except urllib.error.HTTPError as exc:
        return False, int(exc.code), str(exc.reason or "")
    except Exception as exc:
        return False, None, str(exc)


async def probe(host: str, port: int | None = None) -> tuple[str, bool]:
    """Probe HTTPS/HTTP /health endpoints and return (best_url, is_reachable)."""
    url, reachable, _ = await probe_with_details(host, port)
    return url, reachable


async def probe_with_details(host: str, port: int | None = None) -> tuple[str, bool, dict[str, Any]]:
    norm_host = normalize_dynu_host(host)
    p = int(port or 8000)
    local_ok, local_status, _ = await asyncio.to_thread(_probe_url_sync, f"http://127.0.0.1:{p}")
    candidates = [f"https://{norm_host}", f"http://{norm_host}"]
    for x in (p, 8000, 8001):
        u = f"http://{norm_host}" if x == 80 else f"http://{norm_host}:{x}"
        if u not in candidates:
            candidates.append(u)

    attempts: list[dict[str, Any]] = []
    for url in candidates:
        ok, status, detail = await asyncio.to_thread(_probe_url_sync, url)
        attempts.append({"url": url, "ok": ok, "status": status})
        if ok:
            return url, True, {
                "status_code": status,
                "health_body": detail[:120],
                "local_healthy": local_ok,
                "local_status": local_status,
                "attempts": attempts,
            }

    fallback = f"http://{norm_host}" if p == 80 else f"http://{norm_host}:{p}"
    return fallback, False, {
        "status_code": None,
        "health_body": "",
        "local_healthy": local_ok,
        "local_status": local_status,
        "attempts": attempts,
    }


async def sync_activate(updated_by: int | None = None) -> tuple[bool, str, str | None, str | None, bool]:
    """Update Dynu via active method, verify DNS/IP, probe Active URL /health, and sync app URLs."""
    ok, res, ip, url, reachable, _ = await sync_activate_detailed(updated_by=updated_by)
    return ok, res, ip, url, reachable


async def sync_activate_detailed(
    updated_by: int | None = None,
) -> tuple[bool, str, str | None, str | None, bool, dict[str, Any]]:
    cfg = await get_full_config()
    ready, err_msg = is_method_ready(cfg)
    if not ready:
        return False, err_msg or "Not configured", None, None, False, {"method": cfg.get("method", "")}

    ok, res, ip = await update_once(cfg=cfg, updated_by=updated_by)
    if not ok:
        status_doc = {
            "ok": False,
            "method": cfg["method"],
            "message": res,
            "public_ip": ip,
            "checked_at": datetime.datetime.utcnow().isoformat() + "Z",
        }
        await save_dynu_setting("dynu_last_status", status_doc, encrypt=False, updated_by=updated_by)
        return False, res, ip, None, False, status_doc

    host = cfg["hostname"]
    dns_check = await verify_dns_ip(host, ip)
    port = int(str(os.getenv("PORT") or getattr(settings, "PORT", None) or "8000"))
    url, reachable, probe_info = await probe_with_details(host, port)

    now = datetime.datetime.utcnow()
    meta: dict[str, Any] = {"value": url, "updated_at": now}
    if updated_by is not None:
        meta["updated_by"] = updated_by
    await settings_col.update_one({"key": "dynu_url"}, {"$set": meta}, upsert=True)

    if reachable:
        settings.BACKEND_URL = url
        os.environ["BACKEND_URL"] = url
        await settings_col.update_one({"key": "backend_url"}, {"$set": meta}, upsert=True)
        await settings_col.update_one({"key": "cloudflare_url"}, {"$set": meta}, upsert=True)
        base = await settings_col.find_one({"key": "base_url"}) or {}
        current = str(base.get("value") or "").lower()
        is_explicit_custom = ".pages.dev" in current or (
            current.startswith(("http://", "https://"))
            and "vercel.app" not in current
            and "trycloudflare.com" not in current
            and host not in current
        )
        if not is_explicit_custom:
            settings.BASE_URL = url
            os.environ["BASE_URL"] = url
            await settings_col.update_one({"key": "base_url"}, {"$set": meta}, upsert=True)
            await settings_col.update_one({"key": "vercel_url"}, {"$set": meta}, upsert=True)
        try:
            from app.cms import rewrite_all_stored_catalog_buttons

            await rewrite_all_stored_catalog_buttons(url)
        except Exception:
            logger.exception("Dynu catalog URL rewrite failed")

    details = {
        "ok": True,
        "method": cfg["method"],
        "message": res,
        "public_ip": ip,
        "dns_ips": dns_check["resolved_ips"],
        "dns_match": dns_check["matches"],
        "active_url": url,
        "reachable": reachable,
        "health_status": probe_info.get("status_code"),
        "local_healthy": probe_info.get("local_healthy", False),
        "checked_at": now.isoformat() + "Z",
    }
    await save_dynu_setting("dynu_last_status", details, encrypt=False, updated_by=updated_by)
    return True, res, ip, url, reachable, details


# ── Background Updater Loop ──────────────────────────────────────────────────

async def _loop() -> None:
    global _stop
    while not _stop:
        reachable = True
        try:
            if is_dynu_enabled_for_deployment():
                cfg = await get_full_config()
                ready, _ = is_method_ready(cfg)
                if ready:
                    ok, res, ip, url, reachable = await sync_activate()
                    if ok:
                        logger.info(
                            "Dynu [%s] updated %s -> %s; url=%s reachable=%s",
                            cfg["method"],
                            cfg["hostname"],
                            ip,
                            url,
                            reachable,
                        )
                    else:
                        logger.warning("Dynu [%s] update failed for %s: %s", cfg["method"], cfg["hostname"], res)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Dynu loop error")

        sleep_secs = 600 if time.time() < _maintenance_until else (300 if reachable else 90)
        for _ in range(sleep_secs):
            if _stop:
                break
            await asyncio.sleep(1)


def start_updater() -> None:
    global _task, _stop
    _stop = False
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


async def stop_updater() -> None:
    global _task, _stop
    _stop = True
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None
