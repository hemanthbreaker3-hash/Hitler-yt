# Google AdSense verification

Publisher ID: `ca-pub-6636601525186884`

This version includes the AdSense account meta tag and loader in the shared HTML
`<head>`, plus a FastAPI `/ads.txt` route at the domain root.

After deployment, verify on the exact production domain:

1. Open `/` and view page source. Confirm it contains:
   `<meta name="google-adsense-account" content="ca-pub-6636601525186884">`
2. Open `/ads.txt`. It must return HTTP 200 as plain text and contain exactly:
   `google.com, pub-6636601525186884, DIRECT, f08c47fec0942fa0`

Then retry verification in AdSense. Google must be able to reach the site without
authentication, bot challenges, IP restrictions, or a blocking redirect. Allow time
for Google to recrawl after publishing. Site verification is separate from AdSense approval.
