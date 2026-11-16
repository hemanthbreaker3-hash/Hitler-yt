export async function onRequest(context) {
  const { request, env } = context;
  const backend = String(env.BACKEND_URL || '').trim().replace(/\/$/, '');
  if (!backend) return new Response(JSON.stringify({ error: 'BACKEND_URL is not configured for Cloudflare Pages' }), { status: 503, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' } });
  let target;
  try { target = new URL(request.url); target.host = new URL(backend).host; target.protocol = new URL(backend).protocol; target.username = ''; target.password = ''; }
  catch { return new Response(JSON.stringify({ error: 'Invalid BACKEND_URL' }), { status: 500, headers: { 'content-type': 'application/json; charset=utf-8' } }); }
  const upstream = new URL(backend + target.pathname + target.search);
  const headers = new Headers(request.headers);
  headers.set('x-forwarded-host', new URL(request.url).host);
  headers.set('x-forwarded-proto', 'https');
  headers.delete('host');
  try {
    const response = await fetch(upstream.toString(), { method: request.method, headers, body: ['GET','HEAD'].includes(request.method) ? undefined : request.body, redirect: 'manual' });
    const out = new Headers(response.headers);
    out.set('cache-control', 'no-store, no-cache, must-revalidate, max-age=0');
    return new Response(response.body, { status: response.status, statusText: response.statusText, headers: out });
  } catch (error) {
    return new Response(JSON.stringify({ error: 'Cloudflare Pages could not reach the backend', detail: String(error?.message || error) }), { status: 502, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' } });
  }
}
