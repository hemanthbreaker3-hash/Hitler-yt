# Monetag Service Worker on Vercel

The supplied `sw.js` is served at `https://YOUR-DOMAIN/sw.js` by the Vercel
serverless handler before MongoDB initialization. This makes the exact worker
file available at the required website-root URL.

Supplied Monetag configuration:
- Domain: `3nbf4.com`
- Zone ID: `12276863`

After deploying, verify that `https://YOUR-DOMAIN/sw.js` returns HTTP 200 and
contains the supplied worker code. Use Monetag's dashboard Installation Check.

Important: Monetag's publisher instructions require the corresponding ad tag
from the Monetag dashboard in the page `<head>`; Push Notifications and MultiTag
also require this root `sw.js` file. The uploaded file contains the worker only,
not the ad-tag snippet. I have not invented a tag because Monetag says to copy the
exact code generated for the zone. Add that exact dashboard snippet to the site's
`<head>` if it is not already installed.

The worker imports Monetag's remote script from `https://3nbf4.com`.
Keep the supplied domain and zone ID unchanged unless Monetag provides replacements.
