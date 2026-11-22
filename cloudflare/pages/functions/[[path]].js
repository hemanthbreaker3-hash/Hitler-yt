import { PassThrough, Readable } from 'node:stream';
import handler from '../api/[...path].js';

function syncEnvToProcess(env) {
  process.env.CF_PAGES = '1';
  if (!env || typeof env !== 'object') return;
  for (const [k, v] of Object.entries(env)) {
    if (typeof v === 'string' && v.length > 0 && !process.env[k]) {
      process.env[k] = v;
    }
  }
  // Normalize common aliases so MONGODB_URI / DATABASE_NAME / TMDB_API_KEY always resolve
  if (!process.env.MONGODB_URI) {
    process.env.MONGODB_URI = String(
      env.MONGODB_URI || env.MONGO_URL || env.MONGO_URI || env.DATABASE_URL || env.DATABASE_URI || ''
    ).trim();
  }
  if (!process.env.DATABASE_NAME) {
    process.env.DATABASE_NAME = String(
      env.DATABASE_NAME || env.MONGODB_DB_NAME || env.DB_NAME || env.MONGO_DB_NAME || ''
    ).trim();
  }
  if (!process.env.TMDB_API_KEY && env.TMDB_API_KEY) {
    process.env.TMDB_API_KEY = String(env.TMDB_API_KEY).trim();
  }
}

async function runNodeHandler(request, env) {
  syncEnvToProcess(env);
  const url = new URL(request.url);
  const reqHeaders = {};
  for (const [k, v] of request.headers.entries()) {
    reqHeaders[k.toLowerCase()] = v;
  }
  if (!reqHeaders.host) reqHeaders.host = url.host;

  const hasBody = !['GET', 'HEAD'].includes(request.method.toUpperCase()) && request.body;
  const reqStream = new PassThrough();
  reqStream.method = request.method.toUpperCase();
  reqStream.url = url.pathname + url.search;
  reqStream.headers = reqHeaders;
  reqStream.socket = { encrypted: true };

  if (hasBody) {
    const ab = await request.arrayBuffer();
    reqStream.end(Buffer.from(ab));
  } else {
    reqStream.end();
  }

  const outStream = new PassThrough();
  const outHeaders = new Headers();
  let statusCode = 200;
  let headersResolved = false;
  let resolveHeaders;
  const headersReady = new Promise((r) => { resolveHeaders = r; });

  function flushHeaders() {
    if (!headersResolved) {
      headersResolved = true;
      resolveHeaders();
    }
  }

  outStream.statusCode = 200;
  outStream.headersSent = false;
  outStream.setHeader = (name, value) => {
    if (outStream.headersSent) return;
    if (Array.isArray(value)) {
      outHeaders.delete(name);
      value.forEach((v) => outHeaders.append(name, String(v)));
    } else {
      outHeaders.set(name, String(value));
    }
  };
  outStream.getHeader = (name) => outHeaders.get(name);
  outStream.removeHeader = (name) => outHeaders.delete(name);
  outStream.writeHead = (code, hdrs) => {
    statusCode = Number(code) || 200;
    outStream.statusCode = statusCode;
    if (hdrs && typeof hdrs === 'object') {
      for (const [k, v] of Object.entries(hdrs)) {
        if (v !== undefined) outStream.setHeader(k, v);
      }
    }
    outStream.headersSent = true;
    flushHeaders();
    return outStream;
  };

  const origWrite = outStream.write.bind(outStream);
  outStream.write = (chunk, encoding, cb) => {
    if (!outStream.headersSent) {
      statusCode = Number(outStream.statusCode) || 200;
      outStream.headersSent = true;
      flushHeaders();
    }
    return origWrite(chunk, encoding, cb);
  };

  const origEnd = outStream.end.bind(outStream);
  outStream.end = (chunk, encoding, cb) => {
    if (!outStream.headersSent) {
      statusCode = Number(outStream.statusCode) || 200;
      outStream.headersSent = true;
      flushHeaders();
    }
    return origEnd(chunk, encoding, cb);
  };

  const handlerPromise = Promise.resolve(handler(reqStream, outStream)).catch((err) => {
    if (!outStream.headersSent) {
      statusCode = 500;
      outHeaders.set('content-type', 'application/json; charset=utf-8');
      outStream.headersSent = true;
      flushHeaders();
      origEnd(JSON.stringify({ error: 'Cloudflare Pages handler error', detail: String(err?.message || err) }));
    } else {
      outStream.destroy(err);
    }
  });

  await headersReady;
  const webBody = Readable.toWeb(outStream);
  // Keep handler promise alive while response streams
  void handlerPromise;
  return new Response(webBody, { status: statusCode, headers: outHeaders });
}

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

  // Standalone MongoDB + TMDB mode (fetches all other credentials/settings from MongoDB)
  const hasMongo = Boolean(
    env.MONGODB_URI || env.MONGO_URL || env.MONGO_URI || env.DATABASE_URL || env.DATABASE_URI || process.env.MONGODB_URI
  );
  if (hasMongo) {
    return runNodeHandler(request, env);
  }

  // Optional fallback: proxy to BACKEND_URL if MongoDB URI is not configured
  const backend = String(env.BACKEND_URL || env.ORIGIN_URL || env.VPS_URL || '').trim().replace(/\/+$/, '');
  if (!backend) {
    return new Response(
      JSON.stringify({
        error: 'Cloudflare Pages environment variables missing',
        required: ['MONGODB_URI', 'DATABASE_NAME (or MONGODB_DB_NAME)', 'TMDB_API_KEY'],
      }),
      { status: 503, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' } }
    );
  }

  const base = new URL(backend.startsWith('http') ? backend : 'https://' + backend);
  const upstreamUrl = new URL(base.origin + pathname + url.search);
  const headers = new Headers(request.headers);
  headers.set('x-forwarded-host', url.host);
  headers.set('x-forwarded-proto', 'https');
  headers.set('ngrok-skip-browser-warning', '1');
  headers.delete('host');

  const isGetHead = request.method === 'GET' || request.method === 'HEAD';
  const response = await fetch(upstreamUrl.toString(), {
    method: request.method,
    headers,
    body: isGetHead ? undefined : request.body,
    redirect: 'manual',
  });
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers: response.headers });
}
