# Movi Player autoplay fix

- Movi Player is now the default built-in engine in both the Flask watch template and the Vercel API-rendered watch page.
- A stale saved native-player preference no longer silently overrides Movi. `?engine=native` remains an explicit override.
- Autoplay is requested after the Movi custom element is loaded and its source is assigned, with readiness-event and delayed retries.
- Muted inline autoplay is used because mobile browsers commonly block autoplay with sound until a user gesture. Existing player controls, source switching, audio controls, external-player options, and download controls are retained.
