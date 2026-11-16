# AdSense verification on Vercel

Important: this project serves its Vercel production pages from `api/[...path].js`.
The Python/Jinja templates are not the renderer used by the Vercel rewrite. This fix
therefore adds the AdSense verification meta tag and loader to the JavaScript HTML
renderer, and serves `/ads.txt` and `/robots.txt` directly from the Vercel function
before MongoDB initialization.

After deploying the project to the production Vercel environment, test:

- `https://YOUR-PRODUCTION-DOMAIN/` — View Page Source and find
  `<meta name="google-adsense-account" content="ca-pub-6636601525186884">`
- `https://YOUR-PRODUCTION-DOMAIN/ads.txt` — must return HTTP 200 and the exact line:
  `google.com, pub-6636601525186884, DIRECT, f08c47fec0942fa0`
- `https://YOUR-PRODUCTION-DOMAIN/robots.txt` — must return HTTP 200 and allow `/`

The endpoints are handled before MongoDB access so a database outage cannot block
Google's ownership check. Confirm that Vercel has deployed this commit to Production
(not only Preview), that the domain in AdSense matches the live hostname, and that
Vercel Deployment Protection / authentication / bot challenges are not blocking Google.
Then retry verification in AdSense. Verification does not guarantee ad serving or approval.
