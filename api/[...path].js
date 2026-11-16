import http from 'node:http';
import https from 'node:https';
import { MongoClient } from 'mongodb';

// Vercel forwards request streams unchanged to the VPS for authenticated admin writes.
export const config = { api: { bodyParser: false } };

let mongoClientPromise;
async function getMongoClient() {
  if (!process.env.MONGODB_URI) throw new Error('MONGODB_URI is not configured in Vercel');
  if (!mongoClientPromise) {
    const client = new MongoClient(process.env.MONGODB_URI, {
      serverSelectionTimeoutMS: 8000,
      connectTimeoutMS: 8000,
      maxPoolSize: 8,
    });
    mongoClientPromise = client.connect().catch((error) => {
      mongoClientPromise = null;
      throw error;
    });
  }
  return mongoClientPromise;
}

async function getDatabase() {
  const client = await getMongoClient();
  // DATABASE_NAME must be identical on VPS and Vercel. No silent database fallback.
  const name = String(process.env.DATABASE_NAME || '').trim();
  if (!name) throw new Error('DATABASE_NAME is not configured in Vercel');
  return client.db(name);
}

function sendJson(res, status, payload) {
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0');
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.end(JSON.stringify(payload));
}

function validPublicUrl(value) {
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) return null;
    if (!url.hostname || ['localhost', '127.0.0.1', '0.0.0.0'].includes(url.hostname)) return null;
    if (url.hostname.endsWith('.local') || url.hostname.endsWith('.internal')) return null;
    return url.origin + url.pathname.replace(/\/+$/, '');
  } catch { return null; }
}

async function syncVercelWebsiteUrls(req, db) {
  const candidates = [
    req.headers.host ? `https://${String(req.headers.host).split(',')[0].trim()}` : '',
    process.env.VERCEL_PROJECT_PRODUCTION_URL ? `https://${process.env.VERCEL_PROJECT_PRODUCTION_URL}` : '',
    process.env.VERCEL_URL ? `https://${process.env.VERCEL_URL}` : '',
    process.env.WEBSITE_URL || '',
  ];
  const urls = [...new Set(candidates.map(validPublicUrl).filter(Boolean))];
  for (const url of urls) {
    await db.collection('settings').updateOne(
      { key: 'website_urls' },
      { $addToSet: { value: url }, $set: { updated_at: new Date() } },
      { upsert: true },
    );
  }
  const productionCandidate = process.env.WEBSITE_URL ||
    (process.env.VERCEL_PROJECT_PRODUCTION_URL ? `https://${process.env.VERCEL_PROJECT_PRODUCTION_URL}` :
      (process.env.VERCEL_ENV === 'production' && req.headers.host ? `https://${String(req.headers.host).split(',')[0].trim()}` : ''));
  const production = validPublicUrl(productionCandidate);
  if (production) await db.collection('settings').updateOne(
    { key: 'vercel_url' }, { $set: { value: production, updated_at: new Date() } }, { upsert: true },
  );
}

function getRequestPath(req) {
  const incoming = new URL(req.url || '/', 'https://vercel.invalid');
  const parts = req.query && req.query.path;
  let pathname = incoming.pathname || '/';
  if (Array.isArray(parts) && parts.length) {
    const clean = parts.map((part) => encodeURIComponent(String(part))).join('/');
    // /api/catalog is rewritten to /api/api/catalog; keep the inner /api prefix.
    pathname = parts[0] === 'api' ? `/api/${clean.slice(4)}` : `/${clean}`;
  } else if (typeof parts === 'string' && parts) {
    pathname = parts === 'index' ? '/' : (parts.startsWith('api/') ? `/${parts}` : `/${parts}`);
  } else if (incoming.pathname === '/api/index') {
    pathname = '/';
  } else if (incoming.pathname.startsWith('/api/')) {
    pathname = incoming.pathname.slice(4);
  }
  return { pathname, search: incoming.search };
}

async function handleMongoPublicApi(req, res, pathname, search) {
  if (req.method !== 'GET') return false;
  if (!['/api/catalog', '/api/categories', '/api/site-settings'].includes(pathname) && !pathname.startsWith('/api/public/title/')) return false;
  try {
    const db = await getDatabase();
    await syncVercelWebsiteUrls(req, db);
    if (pathname === '/api/catalog') {
      const params = new URLSearchParams(search);
      const query = { published: true };
      const category = (params.get('category') || '').trim().slice(0, 60);
      const q = (params.get('q') || '').trim().slice(0, 100);
      if (category) query.category_slugs = category;
      if (q) query.title = { $regex: q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), $options: 'i' };
      const items = await db.collection('catalog_pages').find(query, { projection: { _id: 0 } }).sort({ created_at: -1 }).limit(100).toArray();
      sendJson(res, 200, { items });
      return true;
    }
    if (pathname === '/api/categories') {
      const items = await db.collection('catalog_categories').find({}, { projection: { _id: 0 } }).sort({ name: 1 }).toArray();
      sendJson(res, 200, { items });
      return true;
    }
    if (pathname === '/api/site-settings') {
      const runtime = await db.collection('site_settings').findOne({ _id: 'runtime' }) || {};
      const settingRows = await db.collection('settings').find(
        { key: { $in: ['base_url', 'backend_url', 'vercel_url', 'website_urls'] } },
        { projection: { key: 1, value: 1 } },
      ).toArray();
      const values = Object.fromEntries(settingRows.map((row) => [row.key, row.value]));
      sendJson(res, 200, {
        downloads_enabled: runtime.downloads_enabled !== false,
        streaming_enabled: runtime.streaming_enabled !== false,
        built_in_player_enabled: runtime.built_in_player_enabled !== false,
        auto_player_redirect: runtime.auto_player_redirect === true,
        base_url: values.base_url || '', backend_url: values.backend_url || '',
        vercel_url: values.vercel_url || '', website_urls: values.website_urls || [],
      });
      return true;
    }
    const slug = decodeURIComponent(pathname.slice('/api/public/title/'.length));
    const page = await db.collection('catalog_pages').findOne({ slug, published: true }, { projection: { _id: 0 } });
    if (!page) sendJson(res, 404, { error: 'Title not found' });
    else sendJson(res, 200, page);
    return true;
  } catch (error) {
    console.error('Vercel MongoDB public API error:', error);
    sendJson(res, 503, {
      error: 'Shared MongoDB unavailable',
      message: 'Check that Vercel MONGODB_URI and DATABASE_NAME exactly match the VPS values and that MongoDB Atlas allows Vercel connections.',
    });
    return true;
  }
}

async function getBackendUrl(db) {
  const doc = await db.collection('settings').findOne({ key: 'backend_url' }, { projection: { value: 1 } });
  const fromMongo = doc?.value ? validPublicUrl(String(doc.value).trim()) : null;
  if (fromMongo) return fromMongo;
  return process.env.BACKEND_URL ? validPublicUrl(process.env.BACKEND_URL.trim()) : null;
}

export default async function handler(req, res) {
  if (req.method === 'OPTIONS') {
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,PATCH,DELETE,OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
    res.statusCode = 204; res.end(); return;
  }
  const { pathname, search } = getRequestPath(req);
  if (await handleMongoPublicApi(req, res, pathname, search)) return;

  let backendUrl;
  try {
    const db = await getDatabase();
    await syncVercelWebsiteUrls(req, db);
    backendUrl = await getBackendUrl(db);
  } catch (error) {
    console.error('Could not read shared MongoDB configuration:', error);
    sendJson(res, 503, {
      error: 'Shared database configuration unavailable',
      message: 'Set MONGODB_URI and DATABASE_NAME in Vercel to exactly the same MongoDB cluster and database used by the VPS.',
    });
    return;
  }
  if (!backendUrl) {
    sendJson(res, 503, {
      error: 'VPS backend URL is not configured',
      message: 'Save the public Cloudflare/VPS backend URL in the Telegram bot or admin panel. The Vercel site uses MongoDB for shared content and the VPS backend for rendered pages and Telegram media.',
    });
    return;
  }

  const requestPath = pathname + search;
  if (requestPath.startsWith('/stream/') || requestPath.startsWith('/dl/') || requestPath.startsWith('/remux/')) {
    res.writeHead(307, { Location: `${backendUrl}${requestPath}` }); res.end(); return;
  }
  const targetUrl = new URL(`${backendUrl}${requestPath}`);
  const transport = targetUrl.protocol === 'https:' ? https : http;
  const headers = { ...req.headers, host: targetUrl.host };
  delete headers['content-length']; delete headers['accept-encoding'];
  headers['x-forwarded-host'] = req.headers.host || '';
  headers['x-forwarded-proto'] = 'https';
  headers['x-forwarded-for'] = req.headers['x-forwarded-for'] || req.socket?.remoteAddress || '';
  const clientReq = transport.request(targetUrl, { method: req.method, headers }, (backendRes) => {
    const responseHeaders = { ...backendRes.headers };
    responseHeaders['cache-control'] = 'no-store, no-cache, must-revalidate, max-age=0';
    responseHeaders.pragma = 'no-cache';
    res.writeHead(backendRes.statusCode || 502, responseHeaders);
    backendRes.pipe(res);
  });
  clientReq.setTimeout(30000, () => clientReq.destroy(new Error('Backend request timed out')));
  clientReq.on('error', (error) => {
    console.error('Error proxying request to VPS backend:', error);
    if (!res.headersSent) sendJson(res, 502, { error: 'Bad Gateway', message: 'Could not connect to the configured Cloudflare/VPS backend.' });
    else res.destroy(error);
  });
  if (['POST', 'PUT', 'PATCH'].includes(req.method)) req.pipe(clientReq); else clientReq.end();
}
