# Movi-only built-in player

- Removed the native HTML video fallback and the basic-player switch from both Flask and Vercel watch pages.
- The built-in player now initializes only the official `movi-player` custom element.
- Kept source/quality controls, episode navigation, download actions, and external-player shortcuts (MX Player, PLAYit, VLC) as separate options; they are not built-in playback engines.
- Playback errors remain on Movi Player and show a retry message instead of silently switching engines.
