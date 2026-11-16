# Download engine reliability upgrade

The `/dl/{short_code}` and `/stream/{short_code}` endpoints now use strict single-range parsing and exact Telegram chunk assembly.

## HTTP behavior

- `GET` and `HEAD` are supported.
- Full downloads return `200`; valid byte-range requests return `206`.
- `Content-Length`, `Content-Range`, `Accept-Ranges: bytes`, and identity encoding are set for byte-accurate clients.
- Suffix ranges and open-ended ranges are supported.
- Unsatisfiable, malformed, and multi-range requests return `416` with `Content-Range: bytes */<size>`.
- A resumed client can request `Range: bytes=<last-byte>-` after a dropped connection. Resume is client-driven as required by HTTP; the server does not maintain per-user partial-download state.

## Transfer reliability

- Telegram data is fetched in 1 MiB chunks and emitted in original byte order.
- Each chunk is buffered and validated against its expected size before it is sent, preventing duplicate or short chunks from being silently accepted.
- Failed chunks are retried up to five attempts with exponential backoff (0.5, 1, 2, 4 seconds; capped at 8 seconds).
- When multiple configured helper bots can access the same source message, chunks are fetched concurrently and striped across those bots. Otherwise the primary client is used.
- The response aborts on a chunk that cannot be recovered; it does not report a silently truncated response as complete.

## Compatibility

The endpoint retains attachment disposition for `/dl/`, inline disposition for `/stream/`, CORS exposure for range headers, and disabled content transformation. These are important for Chrome, Android download managers, IDM, and external players.

## Validation performed

- Python compilation of the application: passed.
- Range parser checks: valid normal, open-ended, suffix, and clamped ranges passed; malformed, multi-range, zero suffix, reversed, and out-of-file ranges returned `416`.
- Synthetic Telegram streamer test: verified exact byte output across chunk boundaries and successful retry after a simulated connection interruption.

A live Telegram/Cloudflare end-to-end throughput test still requires deployment with the project's actual WZGram credentials and helper bots.

## Admin link controls

The bot owner and configured admins can globally control public link types:

- `/dl off` blocks every existing and newly generated `/dl/{short_code}` URL with HTTP 403. `/dl on` re-enables them.
- `/stream off` blocks every existing and newly generated `/stream/{short_code}` URL and remux stream endpoints with HTTP 403. `/stream on` re-enables them.

These switches are stored in MongoDB (`site_settings`, `_id: runtime`) and survive restarts. They do not delete files or revoke short codes; re-enabling restores access to the same links. The download and streaming switches are independent. The controls are restricted to the configured owner/admin IDs.
