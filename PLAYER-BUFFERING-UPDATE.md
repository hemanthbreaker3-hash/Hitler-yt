# Built-in player buffering update

- Changed the built-in player to prefer the browser's native media engine for normal browser-playable files.
- Retained the movi-player/WebAssembly engine for MKV files and explicit `?engine=wasm` selection.
- Removed the startup 64 KiB warm-up fetch from the generated Vercel watch page so it no longer makes a redundant media request before playback.
- Improved native-player buffering feedback, remux fallback, and playback-position restoration.
- Kept the existing external-player choices, download actions, quality/source controls, branding, and player symbols unchanged.

The supplied DDStream URL is a watch-page reference, not a direct media URL. Its `path` identifies a different file, so it is not substituted for this site's per-file stream URL.
