"""Track probe via ffprobe using local HTTP Range stream or partial download from Telegram (wzgram)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tempfile
import traceback

from app.utils.media_tools import local_tg_stream_url

logger = logging.getLogger(__name__)
PROBE_DOWNLOAD_BYTES = 8 * 1024 * 1024
PROBE_DOWNLOAD_CHUNKS = 8  # 8 x 1 MiB chunks


async def probe_tracks(
    client,
    message,
    file_size: int,
    chat_id: int | None = None,
    message_id: int | None = None,
) -> dict:
    # 1. Try probing via the local HTTP Range endpoint first (handles both MKV headers and MP4 trailing moov atoms)
    eff_chat_id = chat_id or getattr(getattr(message, "chat", None), "id", None)
    eff_msg_id = message_id or getattr(message, "id", None)
    if eff_chat_id and eff_msg_id:
        try:
            url = local_tg_stream_url(int(eff_chat_id), int(eff_msg_id))
            via_http = await _run_ffprobe(url, timeout=18.0)
            if not via_http.get("error") and (via_http.get("video_tracks") or via_http.get("audio_tracks")):
                return via_http
        except Exception as exc:
            logger.debug("HTTP Range ffprobe fallback to partial download: %s", exc)

    # 2. Fallback: stream the first 8 MiB (8 chunks of 1 MiB) to a temp file and run ffprobe
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(suffix=".bin")
        os.close(fd)
        probe_bytes = min(PROBE_DOWNLOAD_BYTES, max(file_size, 1024 * 1024))
        chunk_limit = max(1, min(PROBE_DOWNLOAD_CHUNKS, (probe_bytes + (1024 * 1024) - 1) // (1024 * 1024)))
        downloaded = 0

        with open(temp_path, "wb") as f:
            async for chunk in client.stream_media(message, offset=0, limit=chunk_limit):
                if not chunk:
                    continue
                data = bytes(chunk)
                f.write(data)
                downloaded += len(data)
                if downloaded >= probe_bytes:
                    break

        logger.info("Downloaded %.1f KB for track probing", downloaded / 1024)
        if downloaded == 0:
            return {
                "video_tracks": [],
                "audio_tracks": [],
                "subtitle_tracks": [],
                "has_multiple_audio": False,
                "error": "Downloaded 0 bytes from Telegram",
            }
        return await _run_ffprobe(temp_path, timeout=25.0)
    except Exception as e:
        logger.error("Track probing failed: %s\n%s", e, traceback.format_exc())
        return {
            "video_tracks": [],
            "audio_tracks": [],
            "subtitle_tracks": [],
            "has_multiple_audio": False,
            "error": f"{type(e).__name__}: {e}",
        }
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except Exception:
                pass


async def _run_ffprobe(target: str, timeout: float = 25.0) -> dict:
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-probesize", "5M",
        "-analyzeduration", "10000000",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        target,
    ]
    try:
        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, timeout=timeout, check=False
        )
    except Exception as e:
        return {
            "video_tracks": [], "audio_tracks": [], "subtitle_tracks": [],
            "has_multiple_audio": False, "error": f"ffprobe execution failed: {e}",
        }
    if result.returncode != 0:
        return {
            "video_tracks": [], "audio_tracks": [], "subtitle_tracks": [],
            "has_multiple_audio": False,
            "error": f"ffprobe error: {result.stderr.decode(errors='replace').strip()}",
        }
    try:
        probe_data = json.loads(result.stdout.decode(errors="replace"))
    except json.JSONDecodeError:
        return {
            "video_tracks": [], "audio_tracks": [], "subtitle_tracks": [],
            "has_multiple_audio": False, "error": "Failed to parse ffprobe output",
        }
    return _parse_probe_data(probe_data)


def _parse_probe_data(probe_data: dict) -> dict:
    video_tracks, audio_tracks, subtitle_tracks = [], [], []
    audio_index = 0
    sub_index = 0
    fmt = probe_data.get("format") or {}
    duration = 0.0
    try:
        duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0

    for stream in probe_data.get("streams", []):
        codec_type = stream.get("codec_type", "")
        tags = stream.get("tags", {}) or {}
        disposition = stream.get("disposition", {}) or {}
        language = (tags.get("language") or tags.get("LANGUAGE") or "und").strip().lower()
        title = (tags.get("title") or tags.get("TITLE") or "").strip()
        if codec_type == "video":
            if disposition.get("attached_pic"):
                continue
            video_tracks.append({
                "index": stream.get("index", 0),
                "codec": stream.get("codec_name", "unknown"),
                "profile": stream.get("profile", ""),
                "width": stream.get("width", 0),
                "height": stream.get("height", 0),
                "fps": _parse_fps(stream.get("r_frame_rate", "0/1")),
                "pix_fmt": stream.get("pix_fmt", ""),
                "language": language,
                "title": title,
            })
        elif codec_type == "audio":
            audio_tracks.append({
                "index": stream.get("index", 0),
                "audio_index": audio_index,
                "codec": stream.get("codec_name", "unknown"),
                "channels": stream.get("channels", 2),
                "channel_layout": stream.get("channel_layout", ""),
                "sample_rate": stream.get("sample_rate", ""),
                "bit_rate": stream.get("bit_rate", ""),
                "language": language,
                "title": title,
                "default": bool(disposition.get("default")),
                "label": _build_audio_label(language, title, stream, audio_index),
            })
            audio_index += 1
        elif codec_type == "subtitle":
            subtitle_tracks.append({
                "index": stream.get("index", 0),
                "subtitle_index": sub_index,
                "codec": stream.get("codec_name", "unknown"),
                "language": language,
                "title": title,
                "default": bool(disposition.get("default")),
                "forced": bool(disposition.get("forced")),
                "label": _build_subtitle_label(language, title, stream, sub_index),
            })
            sub_index += 1

    return {
        "probe_version": 3,
        "duration": duration,
        "format_name": fmt.get("format_name", ""),
        "video_tracks": video_tracks,
        "audio_tracks": audio_tracks,
        "subtitle_tracks": subtitle_tracks,
        "has_multiple_audio": len(audio_tracks) > 1,
    }


LANG_NAMES = {
    "eng": "English", "en": "English",
    "hin": "Hindi", "hi": "Hindi",
    "tel": "Telugu", "te": "Telugu",
    "tam": "Tamil", "ta": "Tamil",
    "mal": "Malayalam", "ml": "Malayalam",
    "kan": "Kannada", "kn": "Kannada",
    "ben": "Bengali", "bn": "Bengali",
    "mar": "Marathi", "mr": "Marathi",
    "guj": "Gujarati", "gu": "Gujarati",
    "pan": "Punjabi", "pa": "Punjabi",
    "urd": "Urdu", "ur": "Urdu",
    "jpn": "Japanese", "ja": "Japanese",
    "kor": "Korean", "ko": "Korean",
    "chi": "Chinese", "zho": "Chinese", "zh": "Chinese",
    "spa": "Spanish", "es": "Spanish",
    "fre": "French", "fra": "French", "fr": "French",
    "ger": "German", "deu": "German", "de": "German",
    "ita": "Italian", "it": "Italian",
    "por": "Portuguese", "pt": "Portuguese",
    "rus": "Russian", "ru": "Russian",
    "ara": "Arabic", "ar": "Arabic",
    "ind": "Indonesian", "id": "Indonesian",
    "tha": "Thai", "th": "Thai",
    "vie": "Vietnamese", "vi": "Vietnamese",
    "und": "Track",
}


def _build_audio_label(language: str, title: str, stream: dict, audio_index: int = 0) -> str:
    lang_display = LANG_NAMES.get(language, language.upper() if language and language != "und" else f"Audio {audio_index + 1}")
    codec = (stream.get("codec_name") or "").upper()
    channels = stream.get("channels", 2) or 2
    if channels >= 8:
        ch_label = "7.1"
    elif channels >= 6:
        ch_label = "5.1"
    elif channels == 2:
        ch_label = "Stereo"
    elif channels == 1:
        ch_label = "Mono"
    else:
        ch_label = f"{channels}ch"
    parts = [lang_display]
    if title and title.lower() not in (language.lower(), lang_display.lower()):
        parts.append(f"· {title}")
    if codec:
        parts.append(f"({codec} {ch_label})")
    else:
        parts.append(f"({ch_label})")
    return " ".join(parts)


def _build_subtitle_label(language: str, title: str, stream: dict, sub_index: int = 0) -> str:
    lang_display = LANG_NAMES.get(language, language.upper() if language and language != "und" else f"Subtitle {sub_index + 1}")
    codec = (stream.get("codec_name") or "").upper()
    parts = [lang_display]
    if title and title.lower() not in (language.lower(), lang_display.lower()):
        parts.append(f"· {title}")
    if codec:
        parts.append(f"({codec})")
    return " ".join(parts)


def _parse_fps(fps_str: str) -> float:
    try:
        if "/" in fps_str:
            num, den = fps_str.split("/")
            return round(float(num) / float(den), 2)
        return float(fps_str)
    except (ValueError, ZeroDivisionError):
        return 0.0
