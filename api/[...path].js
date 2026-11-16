import http from 'node:http';
import https from 'node:https';
import { MongoClient } from 'mongodb';

// Vercel reads the same MongoDB settings collection as the VPS bot.
// Keep the client warm between serverless invocations when the runtime is reused.
let mongoClientPromise;
async function getMongoClient() {
  if (!process.env.MONGODB_URI) return null;
  if (!mongoClientPromise) {
    const client = new MongoClient(process.env.MONGODB_URI, {
      serverSelectionTimeoutMS: 8000,
      connectTimeoutMS: 8000,
      maxPoolSize: 5,
    });
    mongoClientPromise = client.connect().catch((error) => {
      mongoClientPromise = null;
      throw error;
    });
  }
  return mongoClientPromise;
}

function validPublicUrl(value) {
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol)) return null;
    if (url.username || url.password || url.search || url.hash) return null;
    if (!url.hostname || ['localhost', '127.0.0.1', '0.0.0.0'].includes(url.hostname)) return null;
    if (url.hostname.endsWith('.local') || url.hostname.endsWith('.internal')) return null;
    return url.origin + url.pathname.replace(/\/+$/, '');
  } catch {
    return null;
  }
}

async function syncVercelWebsiteUrls(req) {
  // Vercel supplies VERCEL_URL automatically (without a scheme). Also trust the
  // incoming host so custom domains and preview deployments are registered.
  if (!process.env.MONGODB_URI) return;
  const client = await getMongoClient();
  const db = client.db(process.env.DATABASE_NAME || 'tg_media_bot');
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
  const productionCandidate = process.env.WEBSITE_URL || (process.env.VERCEL_PROJECT_PRODUCTION_URL ? `https://${process.env.VERCEL_PROJECT_PRODUCTION_URL}` : (process.env.VERCEL_ENV === 'production' && req.headers.host ? `https://${String(req.headers.host).split(',')[0].trim()}` : ''));
  const production = validPublicUrl(productionCandidate);
  if (production) {
    await db.collection('settings').updateOne(
      { key: 'vercel_url' },
      { $set: { value: production, updated_at: new Date() } },
      { upsert: true },
    );
  }
}

async function getBackendUrl() {
  // MongoDB is authoritative so /backendurl in Telegram takes effect without redeploying Vercel.
  if (process.env.MONGODB_URI) {
    const client = await getMongoClient();
    const dbName = process.env.DATABASE_NAME || 'tg_media_bot';
    const doc = await client.db(dbName).collection('settings').findOne({ key: 'backend_url' }, { projection: { value: 1 } });
    const fromMongo = doc?.value ? validPublicUrl(String(doc.value).trim()) : null;
    if (fromMongo) return fromMongo;
  }
  return process.env.BACKEND_URL ? validPublicUrl(process.env.BACKEND_URL.trim()) : null;
}

export default async function handler(req, res) {
  let backendUrl;
  try {
    await syncVercelWebsiteUrls(req);
    backendUrl = await getBackendUrl();
  } catch (error) {
    console.error('Could not read backend_url from MongoDB:', error);
    res.status(503).json({
      error: 'Backend configuration unavailable',
      message: 'Vercel could not read the shared MongoDB settings. Check MONGODB_URI and DATABASE_NAME.',
    });
    return;
  }

  if (!backendUrl) {
    res.status(503).json({
      error: 'Backend URL is not configured',
      message: 'Send /backendurl https://your-cloudflare-tunnel.trycloudflare.com to the bot, or configure BACKEND_URL in Vercel as a fallback.',
    });
    return;
  }

  const incomingUrl = new URL(req.url || '/', 'http://vercel.local');
  const routeSegments = req.query && req.query.path;
  let routedPath;
  if (Array.isArray(routeSegments)) routedPath = '/' + routeSegments.join('/');
  else if (routeSegments === 'index') routedPath = '/';
  else if (typeof routeSegments === 'string') routedPath = '/' + routeSegments;
  else if (incomingUrl.pathname === '/api/index') routedPath = '/';
  else if (incomingUrl.pathname.startsWith('/api/')) routedPath = incomingUrl.pathname.slice(4);
  else routedPath = incomingUrl.pathname || '/';
  const requestPath = routedPath + incomingUrl.search;

  // Large media responses go directly to the VPS/Cloudflare endpoint to avoid Vercel limits.
  if (requestPath.startsWith('/stream/') || requestPath.startsWith('/dl/') || requestPath.startsWith('/remux/')) {
    res.writeHead(307, { Location: `${backendUrl}${requestPath}` });
    res.end();
    return;
  }

  const targetUrl = new URL(`${backendUrl}${requestPath}`);
  const transport = targetUrl.protocol === 'https:' ? https : http;
  const headers = { ...req.headers, host: targetUrl.host };
  delete headers['content-length'];
  headers['x-forwarded-host'] = req.headers.host || '';
  headers['x-forwarded-proto'] = 'https';

  const clientReq = transport.request(targetUrl, { method: req.method, headers }, (backendRes) => {
    res.writeHead(backendRes.statusCode || 502, backendRes.headers);
    backendRes.pipe(res);
  });
  clientReq.setTimeout(30000, () => clientReq.destroy(new Error('Backend request timed out')));
  clientReq.on('error', (error) => {
    console.error('Error proxying request to VPS backend:', error);
    if (!res.headersSent) res.status(502).json({ error: 'Bad Gateway', message: 'Could not connect to the configured Cloudflare/VPS backend.' });
    else res.destroy(error);
  });
  if (['POST', 'PUT', 'PATCH'].includes(req.method)) req.pipe(clientReq);
  else clientReq.end();
}
