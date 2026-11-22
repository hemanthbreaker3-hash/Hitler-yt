export async function onRequest(context) {
  const { request, env } = context;
  const url = new URL(request.url);
  const pathname = url.pathname;

  // Serve static assets directly from Cloudflare Pages CDN when available
  if (pathname.startsWith('/static/') && env.ASSETS) {
    try {
      const assetRes = await env.ASSETS.fetch(request);
      if (assetRes && assetRes.status !== 404) return assetRes;
    } catch (_) {}
  }

  // CORS preflight for media/track endpoints
  if (request.method === 'OPTIONS' && (/^\/(stream|dl|remux|api\/tracks)\//.test(pathname))) {
    return new Response(null, {
      status: 204,
      headers: {
        'access-control-allow-origin': '*',
        'access-control-allow-methods': 'GET, HEAD, OPTIONS',
        'access-control-allow-headers': 'Range, Content-Type, Accept, Origin, ngrok-skip-browser-warning',
        'access-control-expose-headers': 'Content-Length, Content-Range, Accept-Ranges, Content-Disposition, Content-Type',
        'access-control-max-age': '86400',
      },
    });
  }

  const backend = String(env.BACKEND_URL || env.ORIGIN_URL || env.VPS_URL || env.VERCEL_URL || '')
    .trim()
    .replace(/\/+$/, '');
  if (!backend) {
    return new Response(
      JSON.stringify({
        error: 'BACKEND_URL is not configured for Cloudflare Pages',
        hint: 'Set BACKEND_URL in Cloudflare Pages -> Settings -> Environment variables to your HTTPS tunnel or Vercel origin.',
      }),
      { status: 503, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' } }
    );
  }

  let upstreamUrl;
  try {
    const base = new URL(backend.startsWith('http') ? backend : 'https://' + backend);
    if (base.host === url.host) {
      return new Response(JSON.stringify({ error: 'BACKEND_URL cannot point to the Pages domain itself' }), {
        status: 500,
        headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
      });
    }
    upstreamUrl = new URL(base.origin + pathname + url.search);
  } catch {
    return new Response(JSON.stringify({ error: 'Invalid BACKEND_URL' }), {
      status: 500,
      headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
    });
  }

  const headers = new Headers(request.headers);
  headers.set('x-forwarded-host', url.host);
  headers.set('x-forwarded-proto', 'https');
  headers.set('ngrok-skip-browser-warning', '1');
  headers.delete('host');

  const isGetHead = request.method === 'GET' || request.method === 'HEAD';
  const isCacheablePage = isGetHead && (pathname === '/' || pathname.startsWith('/title/') || pathname.startsWith('/watch/'));
  const isCacheableTracks = isGetHead && pathname.startsWith('/api/tracks/');

  const fetchOpts = {
    method: request.method,
    headers,
    body: isGetHead ? undefined : request.body,
    redirect: 'manual',
  };
  if (isCacheablePage) {
    fetchOpts.cf = { cacheEverything: true, cacheTtl: 30 };
  } else if (isCacheableTracks) {
    fetchOpts.cf = { cacheEverything: true, cacheTtl: 3600 };
  }

  try {
    const response = await fetch(upstreamUrl.toString(), fetchOpts);
    const out = new Headers(response.headers);
    if (isCacheableTracks && response.status === 200) {
      out.set('cache-control', 'public, max-age=3600, s-maxage=3600');
    } else if (isCacheablePage && response.status === 200) {
      if (!out.get('cache-control') || out.get('cache-control').includes('no-store')) {
        out.set('cache-control', 'public, s-maxage=30, stale-while-revalidate=180');
      }
    } else if (!/^\/(stream|dl|remux)\//.test(pathname)) {
      out.set('cache-control', 'no-store, no-cache, must-revalidate, max-age=0');
    }
    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: out,
    });
  } catch (error) {
    return new Response(
      JSON.stringify({
        error: 'Cloudflare Pages could not reach the backend',
        detail: String(error?.message || error),
      }),
      { status: 502, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' } }
    );
  }
}
