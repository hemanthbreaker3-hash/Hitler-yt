"""
High-speed Telegram media streaming & downloading for Chrome, 1DM, ADM, IDM, and external video players.
Supports:
  - GET and HEAD requests + RFC 7233 Byte Ranges (206 Partial Content)
  - Multi-bot sliding-window parallel 1 MiB chunk prefetching (boosts single-connection Chrome downloads AND multi-connection 1DM downloads using all /addbots helper bots!)
  - Direct file_id streaming fallback when source messages are in private bot chats
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from wzgram import Client

logger = logging.getLogger(__name__)

TG_CHUNK = 1024 * 1024
DEFAULT_CHUNK = 4 * TG_CHUNK
MAX_CONCURRENT_PER_CLIENT = 48
MAX_PARALLEL_STRIPES = 8
MEDIA_TARGET_CACHE_TTL = 1800
_resolved_media_targets: dict[tuple, tuple[Client, object, float]] = {}
_resolved_media_lock = asyncio.Lock()
_resolution_locks: dict[tuple, asyncio.Lock] = {}

_work_loads: dict[int, int] = {}
_work_lock = asyncio.Lock()


def _cid(client: Client) -> int:
    return id(client)


async def _acquire_client(clients: list[Client]) -> Client:
    async with _work_lock:
        for c in clients:
            _work_loads.setdefault(_cid(c), 0)
        under = [(c, _work_loads[_cid(c)]) for c in clients if _work_loads[_cid(c)] < MAX_CONCURRENT_PER_CLIENT]
        if under:
            client = min(under, key=lambda x: x[1])[0]
        else:
            client = min(clients, key=lambda c: _work_loads.get(_cid(c), 0))
        _work_loads[_cid(client)] = _work_loads.get(_cid(client), 0) + 1
        return client


async def _release_client(client: Client) -> None:
    async with _work_lock:
        k = _cid(client)
        _work_loads[k] = max(0, _work_loads.get(k, 0) - 1)


def parse_range_header(request: Request, file_size: int) -> tuple[int, int, bool]:
    """Parse HTTP Range header for Chrome, 1DM, ADM, IDM, and video players."""
    header = (request.headers.get("range") or request.headers.get("Range") or "").strip()
    if not header:
        return 0, max(file_size - 1, 0), False

    match = re.search(r"bytes=(?P<start>\d*)-(?P<end>\d*)", header, flags=re.I)
    if not match or file_size <= 0:
        return 0, max(file_size - 1, 0), False

    start_s, end_s = match.group("start"), match.group("end")
    if start_s == "" and end_s == "":
        return 0, max(file_size - 1, 0), False

    if start_s == "":
        suffix = int(end_s or 0)
        if suffix <= 0:
            raise HTTPException(
                status_code=416,
                detail="Requested range is not satisfiable",
                headers={"Content-Range": f"bytes */{file_size}"},
            )
        start, end = max(file_size - suffix, 0), file_size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else file_size - 1
        if start >= file_size or end < start:
            raise HTTPException(
                status_code=416,
                detail="Requested range is not satisfiable",
                headers={"Content-Range": f"bytes */{file_size}"},
            )
        end = min(end, file_size - 1)

    return start, end, True


def _extract_media_from_msg(msg):
    if not msg:
        return None
    return (
        getattr(msg, "document", None)
        or getattr(msg, "video", None)
        or getattr(msg, "audio", None)
        or getattr(msg, "animation", None)
        or getattr(msg, "voice", None)
        or getattr(msg, "video_note", None)
        or getattr(msg, "photo", None)
    )


async def _resolve_message(client: Client, chat_id: int | None, message_id: int | None):
    if not chat_id or not message_id:
        return None
    try:
        msg = await client.get_messages(chat_id, message_id)
        if isinstance(msg, list):
            msg = msg[0] if msg else None
        if not msg:
            return None
        if _extract_media_from_msg(msg) is None and not getattr(msg, "media", None):
            return None
        return msg
    except Exception as exc:
        is_flood_wait = "flood" in type(exc).__name__.lower() or "flood_wait" in str(exc).lower()
        if is_flood_wait:
            wait = getattr(exc, "value", None) or getattr(exc, "x", None) or 1
            try:
                wait = max(1, int(float(wait)))
            except (TypeError, ValueError):
                wait = 1
            logger.warning("Telegram FloodWait while resolving %s/%s; trying another authorized session", chat_id, message_id)
            raise HTTPException(
                status_code=429,
                detail="Telegram temporarily rate-limited this session. Retry after the indicated delay.",
                headers={"Retry-After": str(wait)},
            ) from exc
        logger.debug("Could not resolve Telegram message %s/%s: %s", chat_id, message_id, exc)
        return None



def _ordered_candidate_clients(
    clients: list[Client],
    bot_client: Client | None,
    chat_id: int | None,
    storage_chat_id: int | None,
) -> list[Client]:
    live = [c for c in clients if c is not None and getattr(c, "is_connected", True)]
    if bot_client is not None and getattr(bot_client, "is_connected", True) and all(bot_client is not c for c in live):
        live.append(bot_client)

    if chat_id and chat_id > 0 and not storage_chat_id and bot_client is not None:
        # A private chat with this bot is best served by its own bot session/file_id.
        return [bot_client] + [c for c in live if c is not bot_client]
    if chat_id and chat_id < 0:
        # Public groups/channels are normally accessible through an authorized user session.
        users = [c for c in live if not getattr(c, "bot_token", None)]
        bots = [c for c in live if getattr(c, "bot_token", None)]
        return users + bots
    return live


async def _acquire_media_target(
    clients: list[Client],
    chat_id: int | None,
    message_id: int | None,
    file_id: str | None = None,
    storage_chat_id: int | None = None,
    storage_message_id: int | None = None,
    bot_client: Client | None = None,
):
    candidates = _ordered_candidate_clients(clients, bot_client, chat_id, storage_chat_id)
    if not candidates:
        raise HTTPException(status_code=503, detail="No connected Telegram sessions available")

    # A file_id produced by this bot can be used directly for media in its private chat.
    if file_id and chat_id and chat_id > 0 and not storage_chat_id and bot_client is not None:
        if getattr(bot_client, "is_connected", False):
            async with _work_lock:
                _work_loads[_cid(bot_client)] = _work_loads.get(_cid(bot_client), 0) + 1
            return bot_client, file_id

    target_chat = storage_chat_id or chat_id
    target_msg = storage_message_id or message_id
    cache_key = (int(target_chat or 0), int(target_msg or 0), str(file_id or ""))
    async with _resolved_media_lock:
        key_lock = _resolution_locks.setdefault(cache_key, asyncio.Lock())

    # Single-flight resolution: simultaneous Range requests from browsers/download managers
    # share one Telegram get_messages call instead of hammering Telegram in parallel.
    async with key_lock:
        now = asyncio.get_running_loop().time()
        async with _resolved_media_lock:
            cached = _resolved_media_targets.get(cache_key)
            if cached:
                cached_client, cached_target, expires = cached
                if expires > now and getattr(cached_client, "is_connected", False):
                    async with _work_lock:
                        _work_loads[_cid(cached_client)] = _work_loads.get(_cid(cached_client), 0) + 1
                    return cached_client, cached_target
                _resolved_media_targets.pop(cache_key, None)

        rate_limited_for = None
        for client in candidates:
            await _acquire_client([client])
            try:
                msg = None
                if storage_chat_id and storage_message_id:
                    msg = await asyncio.wait_for(
                        _resolve_message(client, storage_chat_id, storage_message_id), timeout=5.0
                    )
                if msg is None:
                    msg = await asyncio.wait_for(
                        _resolve_message(client, chat_id, message_id), timeout=5.0
                    )
                if msg is not None:
                    async with _resolved_media_lock:
                        _resolved_media_targets[cache_key] = (
                            client, msg, asyncio.get_running_loop().time() + MEDIA_TARGET_CACHE_TTL
                        )
                        if len(_resolved_media_targets) > 5000:
                            oldest = sorted(_resolved_media_targets.items(), key=lambda item: item[1][2])[:1000]
                            for key, _ in oldest:
                                _resolved_media_targets.pop(key, None)
                    return client, msg
                if file_id and client is bot_client:
                    return client, file_id
            except asyncio.TimeoutError:
                logger.warning("Timed out resolving Telegram media target chat=%s message=%s", target_chat, target_msg)
            except HTTPException as exc:
                if exc.status_code == 429:
                    rate_limited_for = (exc.headers or {}).get("Retry-After", "1")
                logger.warning("Client could not resolve Telegram media target: %s", exc.detail)
            except Exception as exc:
                logger.warning("Client could not resolve Telegram media target: %s", exc)
            await _release_client(client)

        if file_id and bot_client is not None and getattr(bot_client, "is_connected", False):
            async with _work_lock:
                _work_loads[_cid(bot_client)] = _work_loads.get(_cid(bot_client), 0) + 1
            return bot_client, file_id

        if rate_limited_for is not None:
            raise HTTPException(
                status_code=429,
                detail="All configured Telegram sessions are temporarily rate-limited. Retry shortly.",
                headers={"Retry-After": str(rate_limited_for)},
            )

        raise HTTPException(
            status_code=404,
            detail="Media is not accessible to the configured Telegram bot/user sessions. Add the bot to the source chat or configure an authorized user session with access.",
        )


async def _stream_file_chunks(
    client: Client,
    media_target,
    offset: int = 0,
    limit: int = 0,
):
    chunk_offset = offset // TG_CHUNK
    chunk_limit = ((offset % TG_CHUNK) + limit + TG_CHUNK - 1) // TG_CHUNK if limit > 0 else 0

    for attempt in range(4):
        yielded_any = False
        try:
            async for chunk in client.stream_media(
                media_target, offset=chunk_offset, limit=chunk_limit
            ):
                if chunk:
                    yielded_any = True
                    yield bytes(chunk)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if yielded_any:
                logger.warning("stream_media interrupted mid-stream: %s", e)
                raise
            is_flood_wait = "flood" in type(e).__name__.lower() or "flood_wait" in str(e).lower()
            wait = getattr(e, "value", None) or getattr(e, "x", None)
            try:
                wait = float(wait) if wait is not None else (1.0 + attempt)
            except (TypeError, ValueError):
                wait = 1.0 + attempt
            wait = min(max(wait, 0.5), 60.0 if is_flood_wait else 12.0)
            logger.warning("stream_media retry %s (flood_wait=%s): %s (sleep %.1fs)", attempt + 1, is_flood_wait, e, wait)
            await asyncio.sleep(wait)
    raise HTTPException(status_code=502, detail="Telegram stream failed after retries")


async def _fetch_single_tg_chunk(client: Client, media_target, chunk_index: int) -> bytes:
    """Fetch a single 1 MiB Telegram chunk at `chunk_index` using `client`."""
    await _acquire_client([client])
    try:
        parts = []
        async for chunk in client.stream_media(media_target, offset=chunk_index, limit=1):
            if chunk:
                parts.append(bytes(chunk))
        return b"".join(parts)
    finally:
        await _release_client(client)


async def _download_byte_range(client: Client, media_target, start: int, end: int):
    """Yield an exact byte range and resume Telegram reads after transient interruptions."""
    if end < start:
        return

    total = end - start + 1
    sent = 0
    attempt = 0
    retry_limit = 6

    while sent < total:
        absolute_offset = start + sent
        remaining = total - sent
        skip = absolute_offset % TG_CHUNK
        try:
            async for chunk in _stream_file_chunks(
                client, media_target, offset=absolute_offset, limit=remaining
            ):
                if not chunk:
                    continue
                if skip:
                    if len(chunk) <= skip:
                        skip -= len(chunk)
                        continue
                    chunk = chunk[skip:]
                    skip = 0
                needed = total - sent
                if len(chunk) > needed:
                    chunk = chunk[:needed]
                if chunk:
                    sent += len(chunk)
                    yield chunk
                if sent >= total:
                    return

            if sent < total:
                raise IOError(f"Telegram stream ended early ({sent}/{total} bytes delivered)")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            attempt += 1
            if attempt > retry_limit:
                logger.exception("Telegram range transfer exhausted retries at byte %d/%d", sent, total)
                raise
            delay = min(0.5 * (2 ** (attempt - 1)), 8.0)
            logger.warning(
                "Resuming Telegram range at byte %d/%d (attempt %d/%d) after %s; retry in %.1fs",
                sent, total, attempt, retry_limit, type(exc).__name__, delay,
            )
            await asyncio.sleep(delay)


async def _discover_multi_bot_targets(
    clients: list[Client],
    primary_client: Client,
    primary_target,
    chat_id: int | None,
    message_id: int | None,
    storage_chat_id: int | None,
    storage_message_id: int | None,
) -> list[tuple[Client, object]]:
    """
    Find all connected helper bots / sessions that can access the channel message so we can
    stripe 1 MiB chunks across all of them in parallel for maximum download speed.
    """
    pairs: list[tuple[Client, object]] = [(primary_client, primary_target)]
    target_chat = storage_chat_id or (chat_id if (chat_id and chat_id < 0) else None)
    target_msg = storage_message_id if storage_chat_id else (message_id if target_chat else None)
    if not target_chat or not target_msg:
        return pairs

    other_clients = [
        c for c in clients
        if c is not None and c is not primary_client and getattr(c, "is_connected", False)
    ][: MAX_PARALLEL_STRIPES - 1]
    if not other_clients:
        return pairs

    async def probe_client(c: Client):
        try:
            msg = await asyncio.wait_for(_resolve_message(c, target_chat, target_msg), timeout=1.0)
            if msg is not None:
                return (c, msg)
        except Exception:
            pass
        return None

    results = await asyncio.gather(*(probe_client(c) for c in other_clients), return_exceptions=True)
    for r in results:
        if isinstance(r, tuple) and len(r) == 2:
            pairs.append(r)
    return pairs


async def media_streamer(
    primary_client: Client,
    primary_target,
    start: int,
    end: int,
    all_clients: list[Client] | None = None,
    chat_id: int | None = None,
    message_id: int | None = None,
    storage_chat_id: int | None = None,
    storage_message_id: int | None = None,
):
    """
    Stream bytes [start, end] with immediate first-byte response from primary client.
    When multiple helper bots/sessions are available, subsequent 1 MiB stripes are
    load-balanced across them (round-robin) for higher single-connection throughput
    (Chrome) while multi-connection download managers already benefit from per-Range
    client selection. Auto-resume on transient Telegram errors is preserved.
    Inspired by FileToLink multi-client load balancing for speed + low latency.
    """
    total = end - start + 1
    pairs: list[tuple[Client, object]] = [(primary_client, primary_target)]
    discover_task = None
    extra_acquired: list[Client] = []

    if all_clients and len(all_clients) > 1 and total > TG_CHUNK:
        discover_task = asyncio.create_task(
            _discover_multi_bot_targets(
                clients=all_clients,
                primary_client=primary_client,
                primary_target=primary_target,
                chat_id=chat_id,
                message_id=message_id,
                storage_chat_id=storage_chat_id,
                storage_message_id=storage_message_id,
            )
        )

    try:
        current = start
        stripe_idx = 0
        while current <= end:
            stripe_end = min(current + TG_CHUNK - 1, end)

            # Opportunistically adopt discovered multi-bot targets (non-blocking after first stripe)
            if discover_task is not None:
                if stripe_idx == 0:
                    # First stripe always from primary for minimal TTFB; give discover a tiny head-start
                    try:
                        await asyncio.wait_for(asyncio.shield(discover_task), timeout=0.05)
                    except (asyncio.TimeoutError, Exception):
                        pass
                if discover_task.done():
                    try:
                        discovered = discover_task.result()
                        if discovered and len(discovered) > 1:
                            pairs = discovered
                    except Exception:
                        pass
                    discover_task = None

            pair = pairs[stripe_idx % len(pairs)]
            client, target = pair
            # Primary already acquired by caller; acquire extras so work_loads stays accurate
            if client is not primary_client and client not in extra_acquired:
                async with _work_lock:
                    _work_loads[_cid(client)] = _work_loads.get(_cid(client), 0) + 1
                extra_acquired.append(client)

            async for chunk in _download_byte_range(client, target, current, stripe_end):
                yield chunk

            current = stripe_end + 1
            stripe_idx += 1
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Error during media_streamer byte transfer: %s", exc)
        raise
    finally:
        if discover_task is not None and not discover_task.done():
            discover_task.cancel()
            try:
                await discover_task
            except Exception:
                pass
        await _release_client(primary_client)
        for c in extra_acquired:
            await _release_client(c)


async def get_streaming_response(
    clients: list[Client],
    chat_id: int,
    message_id: int,
    file_size: int,
    filename: str,
    mime_type: str,
    request: Request,
    chunk_size: int = DEFAULT_CHUNK,
    file_id: str | None = None,
    storage_chat_id: int | None = None,
    storage_message_id: int | None = None,
    bot_client: Client | None = None,
):
    if not clients and bot_client is None:
        raise HTTPException(status_code=503, detail="Telegram streaming session is unavailable")

    client, media_target = await _acquire_media_target(
        clients=clients,
        chat_id=chat_id,
        message_id=message_id,
        file_id=file_id,
        storage_chat_id=storage_chat_id,
        storage_message_id=storage_message_id,
        bot_client=bot_client,
    )

    try:
        if file_size <= 0 and not isinstance(media_target, str):
            media_obj = _extract_media_from_msg(media_target)
            if media_obj is not None:
                if isinstance(media_obj, (list, tuple)) and media_obj:
                    media_obj = media_obj[-1]
                file_size = int(getattr(media_obj, "file_size", 0) or 0)

        if file_size <= 0:
            await _release_client(client)
            raise HTTPException(status_code=422, detail="Media file has an invalid size")

        start, end, partial = parse_range_header(request, file_size)
    except BaseException:
        await _release_client(client)
        raise

    safe_name = re.sub(r'[\r\n/\\"]', "_", filename or "media")[:180]
    is_download = request.url.path.startswith("/dl/")
    is_stream = (
        request.url.path.startswith("/stream/")
        or request.url.path.startswith("/watch/")
        or request.url.path.startswith("/remux/")
    )
    disposition = "attachment" if is_download else "inline"
    content_type = (mime_type or "application/octet-stream").strip()
    if is_download and not content_type:
        content_type = "application/octet-stream"
    # Prefer a concrete video type for external players (VLC/MX/Kodi/etc.) when unknown
    if is_stream and (not content_type or content_type == "application/octet-stream"):
        content_type = "video/mp4"

    content_length = end - start + 1
    # Streams: no-store so proxies/players do not cache partial ranges incorrectly.
    # Full downloads may be briefly cached.
    if is_download and not partial:
        cache_control = "public, max-age=3600, no-transform"
    else:
        cache_control = "no-store, no-transform, must-revalidate"

    headers = {
        "Content-Type": content_type,
        "Content-Length": str(content_length),
        "Content-Disposition": (
            f'{disposition}; filename="{safe_name.encode("ascii", "replace").decode("ascii")}"; filename*=UTF-8\'\'{quote(safe_name, safe="")}'
        ),
        "Accept-Ranges": "bytes",
        "Content-Encoding": "identity",
        "Cache-Control": cache_control,
        "Connection": "keep-alive",
        "Keep-Alive": "timeout=120, max=1000",
        "X-Content-Type-Options": "nosniff",
        "X-Accel-Buffering": "no",
        "X-Download-Options": "noopen",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": "Range, Content-Type, Accept, Origin",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges, Content-Disposition, Content-Type",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    elif file_size > 0:
        # Advertise full size on 200 so external players can compute duration/seek early
        headers["Content-Range"] = f"bytes 0-{file_size - 1}/{file_size}"

    status_code = 206 if partial else 200

    if request.method.upper() == "HEAD":
        await _release_client(client)
        return Response(status_code=status_code, headers=headers, media_type=content_type)

    return StreamingResponse(
        media_streamer(
            primary_client=client,
            primary_target=media_target,
            start=start,
            end=end,
            all_clients=clients,
            chat_id=chat_id,
            message_id=message_id,
            storage_chat_id=storage_chat_id,
            storage_message_id=storage_message_id,
        ),
        status_code=status_code,
        headers=headers,
        media_type=content_type,
    )


def _build_ffmpeg_cmd(audio_track: int, force_aac: bool = True) -> list[str]:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts+discardcorrupt", "-threads", "0",
        "-i", "pipe:0",
        "-map", "0:v:0?", "-map", f"0:a:{audio_track}?",
        "-c:v", "copy",
    ]
    if force_aac:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000", "-profile:a", "aac_low"]
    else:
        cmd += ["-c:a", "copy"]
    cmd += ["-movflags", "frag_keyframe+empty_moov+default_base_moof+faststart", "-f", "mp4", "pipe:1"]
    return cmd


async def remux_streamer(
    clients: list[Client],
    chat_id: int,
    message_id: int,
    file_size: int,
    audio_track: int = 0,
    force_aac: bool = True,
    file_id: str | None = None,
    storage_chat_id: int | None = None,
    storage_message_id: int | None = None,
    bot_client: Client | None = None,
):
    if audio_track < 0 or audio_track > 32:
        raise HTTPException(status_code=400, detail="Invalid audio track")

    client, media_target = await _acquire_media_target(
        clients=clients,
        chat_id=chat_id,
        message_id=message_id,
        file_id=file_id,
        storage_chat_id=storage_chat_id,
        storage_message_id=storage_message_id,
        bot_client=bot_client,
    )

    cmd = _build_ffmpeg_cmd(audio_track, force_aac=force_aac)
    logger.info("Starting remux ffmpeg: %s", " ".join(cmd))

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def feed():
        try:
            async for chunk in _download_byte_range(client, media_target, 0, max(file_size - 1, 0)):
                if process.returncode is not None or process.stdin is None:
                    break
                process.stdin.write(chunk)
                await process.stdin.drain()
        except Exception as e:
            logger.warning("Remux feed error: %s", e)
        finally:
            if process.stdin and not process.stdin.is_closing():
                try:
                    process.stdin.close()
                except Exception:
                    pass

    feed_task = asyncio.create_task(feed())
    try:
        while True:
            chunk = await process.stdout.read(512 * 1024)
            if not chunk:
                break
            yield chunk
        await feed_task
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
        except asyncio.TimeoutError:
            stderr = b""
        if process.returncode:
            logger.error("FFmpeg remux failed: %s", (stderr or b"").decode(errors="replace")[-1500:])
    except asyncio.CancelledError:
        raise
    finally:
        if not feed_task.done():
            feed_task.cancel()
            try:
                await feed_task
            except asyncio.CancelledError:
                pass
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await process.wait()
        except Exception:
            pass
        await _release_client(client)


async def get_remux_response(
    clients: list[Client],
    chat_id: int,
    message_id: int,
    file_size: int,
    filename: str,
    audio_track: int = 0,
    force_aac: bool = True,
    file_id: str | None = None,
    storage_chat_id: int | None = None,
    storage_message_id: int | None = None,
    bot_client: Client | None = None,
):
    base_name = (filename or "media").rsplit(".", 1)[0]
    safe_name = re.sub(r'[\r\n"\\]', "_", f"{base_name}.mp4")[:180]
    return StreamingResponse(
        remux_streamer(
            clients=clients,
            chat_id=chat_id,
            message_id=message_id,
            file_size=file_size,
            audio_track=audio_track,
            force_aac=force_aac,
            file_id=file_id,
            storage_chat_id=storage_chat_id,
            storage_message_id=storage_message_id,
            bot_client=bot_client,
        ),
        media_type="video/mp4",
        headers={
            "Content-Disposition": f'inline; filename="{safe_name}"',
            "Content-Encoding": "identity",
            "Cache-Control": "no-store, no-transform",
            "Accept-Ranges": "none",
            "X-Accel-Buffering": "no",
        "X-Download-Options": "noopen",
            "Access-Control-Allow-Origin": "*",
        },
    )
