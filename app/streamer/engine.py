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
MAX_CONCURRENT_PER_CLIENT = 32
MAX_PARALLEL_STRIPES = 8

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
    """Parse one RFC 7233 byte range. Multiple/invalid ranges are rejected safely."""
    header = (request.headers.get("range") or "").strip()
    if not header:
        return 0, max(file_size - 1, 0), False
    if file_size <= 0 or not header.lower().startswith("bytes="):
        raise HTTPException(status_code=416, detail="Requested range is not satisfiable",
                            headers={"Content-Range": f"bytes */{file_size}"})
    spec = header[6:].strip()
    if "," in spec or not re.fullmatch(r"\d*-\d*", spec):
        raise HTTPException(status_code=416, detail="Multiple or invalid byte ranges are not supported",
                            headers={"Content-Range": f"bytes */{file_size}"})
    start_s, end_s = spec.split("-", 1)
    if not start_s and not end_s:
        raise HTTPException(status_code=416, detail="Requested range is not satisfiable",
                            headers={"Content-Range": f"bytes */{file_size}"})
    if not start_s:
        suffix = int(end_s)
        if suffix <= 0:
            raise HTTPException(status_code=416, detail="Requested range is not satisfiable",
                                headers={"Content-Range": f"bytes */{file_size}"})
        start, end = max(file_size - suffix, 0), file_size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else file_size - 1
        if start >= file_size or end < start:
            raise HTTPException(status_code=416, detail="Requested range is not satisfiable",
                                headers={"Content-Range": f"bytes */{file_size}"})
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
    for attempt in range(3):
        try:
            msg = await client.get_messages(chat_id, message_id)
            if isinstance(msg, list):
                msg = msg[0] if msg else None
            if not msg:
                return None
            if _extract_media_from_msg(msg) is None and not getattr(msg, "media", None):
                return None
            return msg
        except Exception as e:
            wait = getattr(e, "value", None) or getattr(e, "x", None) or (0.4 + attempt * 0.4)
            try:
                wait = float(wait)
            except Exception:
                wait = 0.4 + attempt * 0.4
            wait = min(max(wait, 0.2), 6.0)
            logger.debug("get_messages(%s, %s) retry %s: %s", chat_id, message_id, attempt, e)
            await asyncio.sleep(wait)
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
        ordered = [bot_client] + [c for c in live if c is not bot_client]
        return ordered
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

    remaining = list(candidates)
    while remaining:
        if chat_id and chat_id > 0 and not storage_chat_id and bot_client in remaining:
            client = bot_client
            async with _work_lock:
                _work_loads[_cid(client)] = _work_loads.get(_cid(client), 0) + 1
        else:
            client = await _acquire_client(remaining)
        remaining = [c for c in remaining if c is not client]

        try:
            if storage_chat_id and storage_message_id:
                msg = await _resolve_message(client, storage_chat_id, storage_message_id)
                if msg is not None:
                    return client, msg

            msg = await _resolve_message(client, chat_id, message_id)
            if msg is not None:
                return client, msg

            if file_id and (client is bot_client or len(candidates) == 1):
                return client, file_id
        except Exception as exc:
            logger.warning("Client could not resolve media target: %s", exc)

        await _release_client(client)

    target_bot = bot_client or (candidates[0] if candidates else None)
    if file_id and target_bot is not None:
        async with _work_lock:
            _work_loads[_cid(target_bot)] = _work_loads.get(_cid(target_bot), 0) + 1
        return target_bot, file_id

    raise HTTPException(
        status_code=404,
        detail="Media is no longer accessible on Telegram or the bot lacks access to the source chat.",
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
            wait = getattr(e, "value", None) or getattr(e, "x", None) or (1 + attempt)
            try:
                wait = float(wait)
            except Exception:
                wait = 1.0 + attempt
            wait = min(max(wait, 0.4), 12.0)
            logger.warning("stream_media retry %s: %s (sleep %.1fs)", attempt, e, wait)
            await asyncio.sleep(wait)
    raise HTTPException(status_code=502, detail="Telegram stream failed after retries")


async def _fetch_single_tg_chunk(client: Client, media_target, chunk_index: int,
                                 expected_size: int) -> bytes:
    """Fetch and validate one Telegram chunk, retrying without duplicating output."""
    last_error = None
    for attempt in range(5):
        await _acquire_client([client])
        parts = []
        received = 0
        try:
            async for chunk in client.stream_media(media_target, offset=chunk_index, limit=1):
                if chunk:
                    data = bytes(chunk)
                    parts.append(data)
                    received += len(data)
            result = b"".join(parts)
            if len(result) == expected_size:
                return result
            raise IOError(f"Telegram returned {len(result)} bytes; expected {expected_size}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt < 4:
                delay = min(0.5 * (2 ** attempt), 8.0)
                logger.warning("Chunk %d retry %d/5 after %s", chunk_index, attempt + 1, exc)
                await asyncio.sleep(delay)
        finally:
            await _release_client(client)
    raise HTTPException(status_code=502, detail=f"Unable to retrieve complete download chunk: {last_error}")


async def _download_byte_range(client: Client, media_target, start: int, end: int):
    """Yield exact byte slice [start, end] sequentially with immediate TTFB."""
    if end < start:
        return
    content_length = end - start + 1
    bytes_to_skip = start % TG_CHUNK
    bytes_sent = 0

    async for chunk in _stream_file_chunks(
        client, media_target, offset=start, limit=content_length
    ):
        if bytes_to_skip > 0:
            if len(chunk) <= bytes_to_skip:
                bytes_to_skip -= len(chunk)
                continue
            chunk = chunk[bytes_to_skip:]
            bytes_to_skip = 0

        remaining = content_length - bytes_sent
        if len(chunk) > remaining:
            chunk = chunk[:remaining]
        if chunk:
            yield chunk
            bytes_sent += len(chunk)
        if bytes_sent >= content_length:
            return


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
            msg = await asyncio.wait_for(_resolve_message(c, target_chat, target_msg), timeout=3.5)
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
    file_size: int,
    all_clients: list[Client] | None = None,
    chat_id: int | None = None,
    message_id: int | None = None,
    storage_chat_id: int | None = None,
    storage_message_id: int | None = None,
):
    """Deliver an exact byte range in order; retries each chunk before emitting it."""
    total = end - start + 1
    try:
        capable = [(primary_client, primary_target)]
        if all_clients and len(all_clients) > 1 and total >= 4 * TG_CHUNK:
            discovered = await _discover_multi_bot_targets(
                clients=all_clients, primary_client=primary_client,
                primary_target=primary_target, chat_id=chat_id, message_id=message_id,
                storage_chat_id=storage_chat_id, storage_message_id=storage_message_id)
            if len(discovered) > 1:
                capable = discovered[:MAX_PARALLEL_STRIPES]

        first_idx, last_idx = start // TG_CHUNK, end // TG_CHUNK
        window = max(1, min(MAX_PARALLEL_STRIPES, len(capable) * 2))
        pending: dict[int, asyncio.Task] = {}
        next_schedule = first_idx

        def schedule(idx: int):
            client, target = capable[(idx - first_idx) % len(capable)]
            expected = min(TG_CHUNK, file_size - idx * TG_CHUNK)
            pending[idx] = asyncio.create_task(
                _fetch_single_tg_chunk(client, target, idx, expected))

        try:
            while next_schedule <= last_idx and len(pending) < window:
                schedule(next_schedule)
                next_schedule += 1
            for idx in range(first_idx, last_idx + 1):
                task = pending.pop(idx)
                chunk = await task
                if next_schedule <= last_idx:
                    schedule(next_schedule)
                    next_schedule += 1
                left = start % TG_CHUNK if idx == first_idx else 0
                right = (end % TG_CHUNK) + 1 if idx == last_idx else len(chunk)
                piece = chunk[left:right]
                if piece:
                    yield piece
        finally:
            for task in pending.values():
                if not task.done():
                    task.cancel()
            if pending:
                await asyncio.gather(*pending.values(), return_exceptions=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Download aborted before complete delivery: %s", exc)
        raise
    finally:
        await _release_client(primary_client)


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

    safe_name = re.sub(r'[\r\n"\\]', "_", filename or "media")[:180]
    is_download = request.url.path.startswith("/dl/")
    disposition = "attachment" if is_download else "inline"
    content_type = (mime_type or "application/octet-stream").strip()
    if is_download and not content_type:
        content_type = "application/octet-stream"

    content_length = end - start + 1
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(content_length),
        "Content-Disposition": (
            f'{disposition}; filename="{safe_name}"; filename*=UTF-8\'\'{quote(safe_name)}'
        ),
        "Accept-Ranges": "bytes",
        "Content-Encoding": "identity",
        "Cache-Control": "no-transform, public, max-age=3600",
        "X-Content-Type-Options": "nosniff",
        "X-Accel-Buffering": "no",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": "Range, Content-Type, Accept, Origin",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges, Content-Disposition",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

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
            file_size=file_size,
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
            "Access-Control-Allow-Origin": "*",
        },
    )
