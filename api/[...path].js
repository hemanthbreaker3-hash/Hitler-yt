import crypto from 'node:crypto';
import http from 'node:http';
import https from 'node:https';
import { MongoClient } from 'mongodb';

export const config = { api: { bodyParser: false } };

const API_SUBPATHS = new Set([
  'catalog',
  'categories',
  'site-settings',
  'public',
  'health',
  'admin',
  'tracks',
]);

let mongoClientPromise = null;
let resolvedDbName = null;

function getMongoUri() {
  return String(
    process.env.MONGODB_URI ||
    process.env.DATABASE_URL ||
    process.env.DATABASE_URI ||
    ''
  ).trim();
}

function extractDbNameFromUri(uri) {
  try {
    const parsed = new URL(uri);
    const name = (parsed.pathname || '').replace(/^\/+/, '').split('/')[0].trim();
    if (name && name !== 'admin') return decodeURIComponent(name);
  } catch {}
  return '';
}

async function getMongoClient() {
  const uri = getMongoUri();
  if (!uri) throw new Error('MONGODB_URI is not configured in Vercel');
  if (!mongoClientPromise) {
    const client = new MongoClient(uri, {
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
  if (resolvedDbName) return client.db(resolvedDbName);

  const envName = String(process.env.DATABASE_NAME || '').trim();
  const uriName = extractDbNameFromUri(getMongoUri());
  // Match the Python bot's DATABASE_NAME/MONGODB_URI selection.  The shared
  // configuration is stored in site_settings (not the legacy `settings` collection).
  const candidates = [...new Set([envName, uriName, 'tg_media_bot', 'file2link'].filter(Boolean))];
  const primary = envName || uriName || 'tg_media_bot';

  try {
    for (const candidate of candidates) {
      const db = client.db(candidate);
      const counts = await Promise.all([
        db.collection('catalog_pages').estimatedDocumentCount().catch(() => 0),
        db.collection('site_settings').estimatedDocumentCount().catch(() => 0),
        db.collection('files').estimatedDocumentCount().catch(() => 0),
        db.collection('users').estimatedDocumentCount().catch(() => 0),
      ]);
      if (counts.some((count) => count > 0)) {
        resolvedDbName = candidate;
        return db;
      }
    }
  } catch {}

  resolvedDbName = primary;
  return client.db(primary);
}

function sendJson(res, status, payload) {
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0');
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.end(JSON.stringify(payload));
}

function sendHtml(res, status, html) {
  res.statusCode = status;
  res.setHeader('Content-Type', 'text/html; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0');
  res.end(html);
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[c]));
}

function safeJsonForScript(value) {
  return JSON.stringify(value ?? null)
    .replace(/</g, '\\u003c')
    .replace(/>/g, '\\u003e')
    .replace(/&/g, '\\u0026');
}

function validPublicUrl(value) {
  if (!value || typeof value !== 'string') return null;
  let trimmed = value.trim().replace(/\/+$/, '');
  if (!trimmed) return null;
  if (!/^https?:\/\//i.test(trimmed)) trimmed = 'https://' + trimmed;
  try {
    const url = new URL(trimmed);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) return null;
    const host = (url.hostname || '').toLowerCase().replace(/\.$/, '');
    if (!host || ['localhost', '127.0.0.1', '0.0.0.0'].includes(host)) return null;
    if (host.endsWith('.local') || host.endsWith('.internal') || !host.includes('.')) return null;
    return url.origin + url.pathname.replace(/\/+$/, '');
  } catch {
    return null;
  }
}

function isTryCloudflareUrl(value) {
  const valid = validPublicUrl(value);
  if (!valid) return false;
  try {
    const host = new URL(valid).hostname.toLowerCase();
    return host === 'trycloudflare.com' || host.endsWith('.trycloudflare.com');
  } catch {
    return false;
  }
}

function isVercelUrl(value) {
  const valid = validPublicUrl(value);
  if (!valid) return false;
  try {
    const host = new URL(valid).hostname.toLowerCase();
    return host === 'vercel.app' || host.endsWith('.vercel.app');
  } catch {
    return false;
  }
}

function rewriteMediaOrButtonUrl(targetUrl, cloudflareBase, forceReplaceDomain = true) {
  const target = String(targetUrl || '').trim();
  const cfValid = validPublicUrl(cloudflareBase);
  if (!target || !cfValid) return target;

  if (target.startsWith('/')) {
    return cfValid + target;
  }

  try {
    const parsed = new URL(target);
    if (!['http:', 'https:'].includes(parsed.protocol)) return target;
    const cf = new URL(cfValid);
    const host = parsed.hostname.toLowerCase();
    const isMediaPath = /^\/(dl|stream|watch|remux)\//.test(parsed.pathname);
    if (forceReplaceDomain || isMediaPath || host.endsWith('.trycloudflare.com') || host.endsWith('.vercel.app')) {
      parsed.protocol = cf.protocol;
      parsed.host = cf.host;
      return parsed.toString();
    }
  } catch {}
  return target;
}

function normalizeAndRewritePageGroups(groups, cloudflareBase) {
  if (!Array.isArray(groups)) return [];
  let watchIdx = 0;
  let dlIdx = 0;
  const out = [];
  for (const group of groups) {
    if (!group || typeof group !== 'object') continue;
    const box = String(group.box || 'watch').trim().toLowerCase() === 'download' ? 'download' : 'watch';
    const defaultGName = box === 'watch' ? `Watch Group ${++watchIdx}` : `Download Group ${++dlIdx}`;
    const name = String(group.name || '').trim() || defaultGName;
    const rawButtons = Array.isArray(group.buttons) ? group.buttons : [];
    const buttons = [];
    for (let i = 0; i < rawButtons.length; i++) {
      const btn = rawButtons[i];
      if (!btn || typeof btn !== 'object') continue;
      let bName = String(btn.name || '').trim();
      let bUrl = String(btn.url || '').trim();
      if (!bName && !bUrl) continue;
      if (!bName) bName = `${box === 'watch' ? 'Watch' : 'Download'} ${i + 1}`;
      if (bUrl && !/^(https?:\/\/|\/)/i.test(bUrl)) bUrl = 'https://' + bUrl;
      if (bUrl) {
        buttons.push({
          name: bName,
          url: rewriteMediaOrButtonUrl(bUrl, cloudflareBase, true),
        });
      }
    }
    out.push({ box, name, buttons });
  }
  return out;
}

function serializeCatalogDoc(doc, cloudflareBase) {
  if (!doc || typeof doc !== 'object') return {};
  const copy = {};
  for (const [k, v] of Object.entries(doc)) {
    if (k === '_id') continue;
    if (v instanceof Date) copy[k] = v.toISOString();
    else if (typeof v === 'number' && !Number.isFinite(v)) copy[k] = null;
    else copy[k] = v;
  }
  copy.groups = normalizeAndRewritePageGroups(copy.groups || [], cloudflareBase);
  return copy;
}

async function rewriteAllStoredCatalogButtons(db, cloudflareBase) {
  const cfValid = validPublicUrl(cloudflareBase);
  if (!cfValid) return 0;
  let updated = 0;
  try {
    const pages = db.collection('catalog_pages');
    const docs = await pages.find({ 'groups.0': { $exists: true } }, { projection: { _id: 1, groups: 1 } }).toArray();
    for (const doc of docs) {
      const oldGroups = Array.isArray(doc.groups) ? doc.groups : [];
      const newGroups = normalizeAndRewritePageGroups(oldGroups, cfValid);
      if (JSON.stringify(oldGroups) !== JSON.stringify(newGroups)) {
        await pages.updateOne(
          { _id: doc._id },
          { $set: { groups: newGroups, updated_at: new Date() } },
        );
        updated += 1;
      }
    }
  } catch (err) {
    console.warn('Could not rewrite stored catalog buttons:', err?.message || err);
  }
  return updated;
}

async function getSettingsState(db, req = null) {
  const runtime = (await db.collection('site_settings').findOne({ _id: 'runtime' }).catch(() => null)) || {};
  const rows = await db
    .collection('settings')
    .find(
      { key: { $in: ['base_url', 'backend_url', 'cloudflare_url', 'vercel_url', 'website_urls', 'tmdb_api_key', 'admin_panel_token', 'panel_username', 'panel_password_salt', 'panel_password_hash'] } },
      { projection: { key: 1, value: 1 } },
    )
    .toArray()
    .catch(() => []);

  const map = Object.fromEntries(rows.map((r) => [r.key, r.value]));
  const rawWebsiteUrls = Array.isArray(map.website_urls)
    ? map.website_urls.map(String)
    : map.website_urls
    ? [String(map.website_urls)]
    : [];
  const websiteUrls = rawWebsiteUrls.filter((u) => !isTryCloudflareUrl(u));

  const cfCandidates = [
    map.cloudflare_url,
    map.backend_url,
    map.base_url,
    process.env.BACKEND_URL,
    process.env.CLOUDFLARE_URL,
    process.env.BASE_URL,
  ]
    .map(validPublicUrl)
    .filter(Boolean);

  const cloudflareUrl =
    cfCandidates.find((u) => isTryCloudflareUrl(u)) ||
    cfCandidates.find((u) => !isVercelUrl(u)) ||
    cfCandidates[0] ||
    '';

  const hostUrl =
    req && req.headers?.host
      ? validPublicUrl(`https://${String(req.headers.host).split(',')[0].trim()}`)
      : null;

  const vercelUrl =
    validPublicUrl(map.vercel_url) ||
    validPublicUrl(process.env.WEBSITE_URL) ||
    (process.env.VERCEL_PROJECT_PRODUCTION_URL
      ? validPublicUrl(`https://${process.env.VERCEL_PROJECT_PRODUCTION_URL}`)
      : null) ||
    (hostUrl && !isTryCloudflareUrl(hostUrl) ? hostUrl : null) ||
    '';

  return {
    downloads_enabled: runtime.downloads_enabled !== false,
    streaming_enabled: runtime.streaming_enabled !== false,
    built_in_player_enabled: runtime.built_in_player_enabled !== false,
    auto_player_redirect: runtime.auto_player_redirect === true,
    access_mode: runtime.access_mode || 'all',
    base_url: cloudflareUrl || validPublicUrl(map.base_url) || vercelUrl || '',
    backend_url: cloudflareUrl || validPublicUrl(map.backend_url) || '',
    cloudflare_url: cloudflareUrl,
    vercel_url: vercelUrl,
    website_urls: websiteUrls,
    _raw_map: map,
  };
}

async function syncVercelWebsiteUrls(req, db) {
  try {
    const candidates = [
      req.headers.host ? `https://${String(req.headers.host).split(',')[0].trim()}` : '',
      process.env.VERCEL_PROJECT_PRODUCTION_URL ? `https://${process.env.VERCEL_PROJECT_PRODUCTION_URL}` : '',
      process.env.VERCEL_URL ? `https://${process.env.VERCEL_URL}` : '',
      process.env.WEBSITE_URL || '',
    ];
    const urls = [...new Set(candidates.map(validPublicUrl).filter((u) => u && !isTryCloudflareUrl(u)))];
    const settings = db.collection('settings');
    const existing = await settings.findOne({ key: 'website_urls' }, { projection: { value: 1 } });
    const currentList = Array.isArray(existing?.value)
      ? existing.value.map(String).filter((u) => !isTryCloudflareUrl(u))
      : [];
    const merged = [...new Set([...currentList, ...urls])];
    if (merged.length) {
      await settings
        .updateOne({ key: 'website_urls' }, { $set: { value: merged, updated_at: new Date() } }, { upsert: true })
        .catch(() => {});
    }
    const productionCandidate =
      process.env.WEBSITE_URL ||
      (process.env.VERCEL_PROJECT_PRODUCTION_URL
        ? `https://${process.env.VERCEL_PROJECT_PRODUCTION_URL}`
        : req.headers.host
        ? `https://${String(req.headers.host).split(',')[0].trim()}`
        : '');
    const production = validPublicUrl(productionCandidate);
    if (production && !isTryCloudflareUrl(production)) {
      await settings
        .updateOne({ key: 'vercel_url' }, { $set: { value: production, updated_at: new Date() } }, { upsert: true })
        .catch(() => {});
    }
  } catch {}
}

async function ensureDefaultCategories(db) {
  try {
    const col = db.collection('catalog_categories');
    const count = await col.estimatedDocumentCount().catch(() => 0);
    if (count === 0) {
      const now = new Date();
      await col.updateOne({ slug: 'movies' }, { $setOnInsert: { name: 'Movies', slug: 'movies', created_at: now } }, { upsert: true });
      await col.updateOne({ slug: 'web-series' }, { $setOnInsert: { name: 'Web Series', slug: 'web-series', created_at: now } }, { upsert: true });
    }
  } catch {}
}

function getRequestPath(req) {
  let incoming;
  try {
    incoming = new URL(req.url || '/', 'https://vercel.invalid');
  } catch {
    return { pathname: '/', search: '', searchParams: new URLSearchParams() };
  }

  const searchParams = new URLSearchParams(incoming.search);
  let pathname = '';

  if (searchParams.has('__path')) {
    const rewrittenPath = String(searchParams.get('__path') || '/').trim();
    searchParams.delete('__path');
    pathname = rewrittenPath.startsWith('/') ? rewrittenPath : '/' + rewrittenPath;
  } else {
    const parts = req.query && req.query.path;
    if (Array.isArray(parts) && parts.length) {
      const segments = parts
        .flatMap((p) => String(p).split('/'))
        .filter(Boolean);
      if (segments.length === 1 && segments[0] === 'index') {
        pathname = '/';
      } else if (segments[0] === 'api') {
        pathname = '/' + segments.join('/');
      } else if (API_SUBPATHS.has(segments[0])) {
        pathname = '/api/' + segments.join('/');
      } else {
        pathname = '/' + segments.join('/');
      }
    } else if (typeof parts === 'string' && parts.trim()) {
      const clean = parts.trim().replace(/^\/+/, '');
      const firstSeg = clean.split('/')[0];
      if (clean === 'index') pathname = '/';
      else if (clean.startsWith('api/')) pathname = '/' + clean;
      else if (API_SUBPATHS.has(firstSeg)) pathname = '/api/' + clean;
      else pathname = '/' + clean;
    } else {
      pathname = incoming.pathname || '/';
    }
  }

  if (pathname === '/api/index' || pathname === '/index') {
    pathname = '/';
  } else if (pathname.startsWith('/api/api/')) {
    pathname = '/api/' + pathname.slice('/api/api/'.length);
  } else if (pathname.startsWith('/api/')) {
    const sub = pathname.slice(5).split('/')[0];
    if (!API_SUBPATHS.has(sub)) {
      pathname = pathname.slice(4);
    }
  }

  if (pathname.length > 1) pathname = pathname.replace(/\/+$/, '');
  const qs = searchParams.toString();
  return {
    pathname: pathname || '/',
    search: qs ? '?' + qs : '',
    searchParams,
  };
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    if (req.body && typeof req.body === 'object' && !Buffer.isBuffer(req.body)) {
      return resolve(req.body);
    }
    const chunks = [];
    let total = 0;
    req.on('data', (chunk) => {
      total += chunk.length;
      if (total > 2 * 1024 * 1024) {
        reject(new Error('Request body too large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8').trim();
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch {
        reject(new Error('Invalid JSON body'));
      }
    });
    req.on('error', reject);
  });
}

function timingSafeEqualStr(a, b) {
  const bufA = Buffer.from(String(a ?? ''), 'utf8');
  const bufB = Buffer.from(String(b ?? ''), 'utf8');
  if (bufA.length !== bufB.length) return false;
  return crypto.timingSafeEqual(bufA, bufB);
}

async function verifyAdminAuth(req, db) {
  const auth = String(req.headers.authorization || '');
  if (!auth.toLowerCase().startsWith('basic ')) return false;
  let decoded = '';
  try {
    decoded = Buffer.from(auth.slice(6).trim(), 'base64').toString('utf8');
  } catch {
    return false;
  }
  const sep = decoded.indexOf(':');
  if (sep === -1) return false;
  const username = decoded.slice(0, sep);
  const password = decoded.slice(sep + 1);

  const rows = await db
    .collection('settings')
    .find(
      { key: { $in: ['panel_username', 'panel_password_salt', 'panel_password_hash', 'admin_panel_token'] } },
      { projection: { key: 1, value: 1 } },
    )
    .toArray()
    .catch(() => []);
  const map = Object.fromEntries(rows.map((r) => [r.key, r.value]));
  const expectedUser = map.panel_username ? String(map.panel_username) : 'admin';
  if (!timingSafeEqualStr(username, expectedUser)) return false;

  if (map.panel_password_salt && map.panel_password_hash) {
    try {
      const saltBuf = Buffer.from(String(map.panel_password_salt), 'hex');
      const digest = crypto.pbkdf2Sync(Buffer.from(password, 'utf8'), saltBuf, 310000, 32, 'sha256').toString('hex');
      return timingSafeEqualStr(digest, String(map.panel_password_hash));
    } catch {
      return false;
    }
  }

  const token = String(process.env.ADMIN_PANEL_TOKEN || map.admin_panel_token || '').trim();
  if (token && timingSafeEqualStr(password, token)) return true;
  return false;
}

function sendUnauthorized(res) {
  res.statusCode = 401;
  res.setHeader('WWW-Authenticate', 'Basic realm="HITLER-YT-Panel"');
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.end(JSON.stringify({ detail: 'Invalid panel username or password' }));
}

function renderBaseLayout({ title = 'HITLER YT', headExtra = '', content = '' }) {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="theme-color" content="#0a0a0a">
  <meta name="description" content="HITLER YT · Movies & Series">
  <title>${escapeHtml(title)}</title>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
  <style>
    :root{
      color-scheme:dark;
      --bg:#0a0a0a;
      --bg2:#111;
      --card:#141414;
      --line:#2a2a2a;
      --text:#f5f5f5;
      --muted:#9ca3af;
      --yellow:#facc15;
      --yellow-dark:#eab308;
      --red:#dc2626;
      --red-dark:#b91c1c;
      --accent:#facc15;
      --radius:10px;
    }
    *{box-sizing:border-box;margin:0;padding:0}
    html{scroll-behavior:smooth}
    body{
      margin:0;
      background:var(--bg);
      color:var(--text);
      font:500 14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
      min-height:100vh;
    }
    h1,h2,h3,h4,strong,b,.topbar-logo{font-weight:850}
    a{color:inherit;text-decoration:none}
    button,input,select{font:inherit}
    .wrap{width:min(100% - 16px,1100px);margin:auto}
    .topbar{
      min-height:calc(2cm + 20px);
      padding:10px 0;
      display:flex;
      align-items:center;
      background:#0d0d0d;
      border-bottom:1px solid #222;
      position:sticky;
      top:0;
      z-index:50;
    }
    .topbar-inner{
      width:min(100% - 16px,1100px);
      margin:auto;
      display:flex;
      flex-wrap:wrap;
      align-items:center;
      justify-content:space-between;
      gap:12px;
    }
    .topbar-logo{
      display:inline-flex;
      align-items:center;
      gap:12px;
      height:2cm;
      min-height:2cm;
      width:6cm;
      min-width:6cm;
      font-family:Inter,"Arial Black",Arial,Helvetica,sans-serif;
      font-weight:950;
      font-size:34px;
      letter-spacing:1px;
      line-height:1;
      text-transform:uppercase;
      white-space:nowrap;
      text-shadow:0 3px 22px rgba(250,204,21,.22);
    }
    .topbar-logo img{
      width:2cm;
      height:2cm;
      min-width:2cm;
      min-height:2cm;
      border-radius:14px;
      object-fit:cover;
      border:2.5px solid rgba(250,204,21,.8);
      box-shadow:0 0 22px rgba(250,204,21,.28);
      flex-shrink:0;
    }
    .topbar-logo .brand-name{
      display:inline-flex;
      align-items:center;
      height:2cm;
      min-width:calc(4cm - 12px);
    }
    .topbar-logo span{color:var(--yellow);font-weight:1000}
    .topbar-logo .brand-yt{color:#fff;font-weight:950;margin-left:3px}
    .topbar-links{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
    .topbar-links a{
      color:var(--muted);font-size:13px;padding:6px 10px;border-radius:6px;
      transition:color .15s,background .15s;
    }
    .topbar-links a:hover{color:#fff;background:#1a1a1a}
    .btn-yellow{
      display:inline-flex;align-items:center;justify-content:center;gap:6px;
      background:var(--yellow);color:#111;font-weight:800;font-size:13px;
      padding:9px 16px;border-radius:8px;border:none;cursor:pointer;
      box-shadow:0 2px 12px rgba(250,204,21,.35);
      transition:transform .15s,box-shadow .15s,background .15s;
    }
    .btn-yellow:hover{background:var(--yellow-dark);transform:translateY(-1px);box-shadow:0 4px 16px rgba(250,204,21,.45)}
    .btn-yellow:active{transform:translateY(0)}
    .search-bar{
      display:flex;gap:8px;align-items:center;
      background:#141414;border:1px solid #2a2a2a;border-radius:12px;
      padding:4px 4px 4px 14px;margin:16px 0 12px;
      transition:border-color .2s,box-shadow .2s;
    }
    .search-bar:focus-within{border-color:rgba(250,204,21,.45);box-shadow:0 0 0 3px rgba(250,204,21,.12)}
    .search-bar input{
      flex:1;min-width:0;background:transparent;border:none;outline:none;
      color:#fff;font-size:14px;padding:11px 0;
    }
    .search-bar input::placeholder{color:#6b7280}
    .search-bar button{
      background:var(--yellow);color:#111;border:none;border-radius:9px;
      padding:10px 18px;font-weight:800;cursor:pointer;font-size:13px;
      transition:background .15s,transform .15s;
    }
    .search-bar button:hover{background:var(--yellow-dark);transform:translateY(-1px)}
    .poster-grid{
      display:grid;
      grid-template-columns:repeat(auto-fill,minmax(140px,1fr));
      gap:14px;
      margin-top:18px;
    }
    @media(min-width:640px){
      .poster-grid{grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:16px}
    }
    .poster-card{
      background:#111;border:1px solid #222;border-radius:10px;overflow:hidden;
      transition:transform .18s,border-color .18s,box-shadow .18s;
    }
    .poster-card:hover{
      transform:translateY(-4px);
      border-color:rgba(250,204,21,.45);
      box-shadow:0 10px 28px rgba(0,0,0,.55);
    }
    .poster-card img{
      width:100%;aspect-ratio:2/3;object-fit:cover;display:block;background:#1a1a1a;
    }
    .poster-card .info{padding:10px 10px 12px}
    .poster-card .title{
      font-weight:700;font-size:13px;line-height:1.3;
      display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
      margin-bottom:4px;
    }
    .poster-card .meta{
      font-size:11px;color:#9ca3af;line-height:1.35;
      display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;
    }
    .trending-row{
      display:flex;gap:10px;overflow-x:auto;padding:8px 0 12px;
      scrollbar-width:thin;scrollbar-color:#333 transparent;
    }
    .trending-row::-webkit-scrollbar{height:6px}
    .trending-row::-webkit-scrollbar-thumb{background:#333;border-radius:3px}
    .trending-item{
      flex:0 0 110px;border-radius:8px;overflow:hidden;border:1px solid #222;
      background:#111;
    }
    .trending-item img{width:100%;aspect-ratio:2/3;object-fit:cover;display:block}
    .section-head{
      display:flex;justify-content:space-between;align-items:center;
      margin:22px 0 12px;gap:12px;
    }
    .section-head h2{font-size:17px;font-weight:800}
    .chip{
      display:inline-flex;align-items:center;gap:5px;
      background:#1a1a1a;border:1px solid #333;border-radius:20px;
      padding:5px 12px;font-size:12px;color:#d1d5db;cursor:pointer;
      transition:border-color .15s,color .15s;
    }
    .chip:hover,.chip.active{border-color:var(--yellow);color:var(--yellow)}
    .cat-nav{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
    main{padding:14px 0 80px}
    .footer{
      text-align:center;color:#555;font-size:12px;padding:24px 0 60px;
      border-top:1px solid #1f1f1f;
    }
    .muted{color:var(--muted)}
    .card{background:var(--card);border:1px solid var(--line);border-radius:12px}
    .btn{
      display:inline-flex;align-items:center;justify-content:center;
      min-height:40px;padding:0 14px;border:1px solid #333;border-radius:9px;
      background:#1a1a1a;color:#fff;cursor:pointer;
    }
    .btn.accent{background:#2563eb;border-color:#3b82f6;color:#fff}
    .btn:hover{border-color:var(--yellow);color:var(--yellow)}
    @media(max-width:600px){
      body{font-size:14px}
      .wrap{width:min(100% - 20px,1100px)}
      .topbar-links{gap:2px}
      .topbar-links a{padding:6px 7px;font-size:12px}
      .poster-grid{grid-template-columns:repeat(auto-fill,minmax(125px,1fr));gap:12px}
      .topbar-logo{font-size:30px;height:2cm;min-height:2cm;width:6cm;min-width:6cm}
      .topbar-logo img{width:2cm;height:2cm;min-width:2cm;min-height:2cm}
      .section-head h2{font-size:15px}
    }
  </style>
  ${headExtra}

</head>
<body>
<header class="topbar">
  <div class="topbar-inner">
    <a class="topbar-logo" href="/" title="HITLER YT">
      <img src="/static/images/logo.jpg" alt="HITLER YT logo" loading="eager">
      <div class="brand-name"><span>HITLER</span><span class="brand-yt">YT</span></div>
    </a>
    <div class="topbar-links">
      <a href="/">Home</a>
      <a href="/?category=movies">Movies</a>
      <a href="/?category=web-series">Series</a>
      <a href="https://t.me/HiTLERYT" target="_blank" rel="noopener">Telegram</a>
    </div>
  </div>
</header>
<main>
  <div class="wrap">
    ${content}
  </div>
</main>
<footer class="footer">
  <a href="https://t.me/HiTLERYT" target="_blank" rel="noopener"><strong>HITLER YT</strong> · @hitleryt</a>
</footer>

</body>
</html>`;
}

function pagePosterPath(p) {
  if (!p) return '/static/images/poster.jpg';
  return String(p).startsWith('http') ? p : `https://image.tmdb.org/t/p/w500${p}`;
}

function renderIndexHtml(initialItems, initialCategories, queryStr = '') {
  const content = `
<div id="trending-row" class="trending-row" style="display:none"></div>

<form class="search-bar" action="/search" method="get">
  <i class="fa-solid fa-magnifying-glass" style="color:#6b7280;font-size:14px"></i>
  <input name="q" value="${escapeHtml(queryStr)}" placeholder="Search movies & series..." autocomplete="off">
  <button type="submit">Search</button>
</form>

<div class="section-head" id="catalog">
  <h2>Latest Releases</h2>
  <span class="chip" id="result-count">Loading…</span>
</div>
<div class="cat-nav" id="category-nav"></div>

<div class="poster-grid" id="catalog-grid">
  <p class="muted" style="grid-column:1/-1;padding:40px 0;text-align:center">Loading titles…</p>
</div>

<script>
window.__INITIAL_CATALOG__ = ${safeJsonForScript(initialItems)};
window.__INITIAL_CATEGORIES__ = ${safeJsonForScript(initialCategories)};
(async () => {
  const q = new URLSearchParams(location.search).get('q') || '';
  let category = new URLSearchParams(location.search).get('category') || '';

  const nav = document.getElementById('category-nav');
  const makeChip = (label, slug) => {
    const b = document.createElement('button');
    b.className = 'chip' + (category === slug ? ' active' : '');
    b.textContent = label;
    b.onclick = () => {
      category = slug;
      const url = new URL(location.href);
      if (slug) url.searchParams.set('category', slug); else url.searchParams.delete('category');
      if (q) url.searchParams.set('q', q); else url.searchParams.delete('q');
      history.replaceState(null, '', url);
      load();
      nav.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
      b.classList.add('active');
    };
    return b;
  };

  function renderCategories(catList) {
    nav.innerHTML = '';
    nav.append(makeChip('All', ''));
    const seen = new Set();
    for (const c of (catList || [])) {
      if (!c || !c.slug || seen.has(c.slug)) continue;
      seen.add(c.slug);
      nav.append(makeChip(c.name || c.slug, c.slug));
    }
  }

  if (Array.isArray(window.__INITIAL_CATEGORIES__) && window.__INITIAL_CATEGORIES__.length) {
    renderCategories(window.__INITIAL_CATEGORIES__);
  } else {
    renderCategories([{ name: 'Movies', slug: 'movies' }, { name: 'Web Series', slug: 'web-series' }]);
  }

  try {
    const catRes = await fetch('/api/categories', { cache: 'no-store' });
    if (catRes.ok) {
      const cats = await catRes.json();
      if (Array.isArray(cats.items) && cats.items.length) {
        renderCategories(cats.items);
      }
    }
  } catch (e) {}

  function qualityLine(p) {
    const type = (p.media_type || 'movie').toLowerCase();
    const parts = ['WEB-DL', '[Hindi]', '1080p 720p & 480p', '[x264/HEVC]'];
    if (type === 'tv' || type === 'series') parts.push('| [Series]');
    else parts.push('| Full Movie');
    if (p.overview && p.overview.length > 10) {
      const short = p.overview.replace(/\\s+/g, ' ').trim().slice(0, 60);
      if (short.length > 15) return short + (p.overview.length > 60 ? '…' : '');
    }
    return parts.join(' ');
  }

  function renderItems(items) {
    const el = document.getElementById('catalog-grid');
    const countEl = document.getElementById('result-count');
    el.innerHTML = '';
    const list = Array.isArray(items) ? items : [];
    countEl.textContent = list.length ? list.length + ' titles' : '0 titles';

    const trend = document.getElementById('trending-row');
    if (!list.length) {
      trend.style.display = 'none';
      el.innerHTML = '<p class="muted" style="grid-column:1/-1;padding:40px 0;text-align:center">No titles found. Publish movies or series via /search in the Telegram bot or /panel.</p>';
      return;
    }

    if (!q && list.length) {
      trend.style.display = 'flex';
      trend.innerHTML = '';
      list.slice(0, 10).forEach(p => {
        const a = document.createElement('a');
        a.className = 'trending-item';
        a.href = '/title/' + encodeURIComponent(p.slug);
        const img = document.createElement('img');
        img.loading = 'lazy';
        img.src = p.poster_path?.startsWith('http') ? p.poster_path : (p.poster_path ? 'https://image.tmdb.org/t/p/w300' + p.poster_path : '/static/images/poster.jpg');
        img.alt = p.title || 'Poster';
        a.append(img);
        trend.append(a);
      });
    } else {
      trend.style.display = 'none';
    }

    for (const p of list) {
      const a = document.createElement('a');
      a.className = 'poster-card';
      a.href = '/title/' + encodeURIComponent(p.slug);
      const img = document.createElement('img');
      img.loading = 'lazy';
      img.src = p.poster_path?.startsWith('http') ? p.poster_path : (p.poster_path ? 'https://image.tmdb.org/t/p/w500' + p.poster_path : '/static/images/poster.jpg');
      img.alt = p.title || 'Poster';
      const info = document.createElement('div');
      info.className = 'info';
      const t = document.createElement('div');
      t.className = 'title';
      t.textContent = (p.title || 'Untitled') + (p.release_date ? ' (' + (p.release_date.slice(0, 4) || '') + ')' : '');
      const m = document.createElement('div');
      m.className = 'meta';
      m.textContent = qualityLine(p);
      info.append(t, m);
      a.append(img, info);
      el.append(a);
    }
  }

  function filterInitialItems() {
    let list = Array.isArray(window.__INITIAL_CATALOG__) ? window.__INITIAL_CATALOG__ : [];
    if (category) {
      list = list.filter(item => Array.isArray(item.category_slugs) && item.category_slugs.includes(category));
    }
    if (q) {
      const needle = q.toLowerCase();
      list = list.filter(item =>
        String(item.title || '').toLowerCase().includes(needle) ||
        String(item.overview || '').toLowerCase().includes(needle)
      );
    }
    return list;
  }

  if (Array.isArray(window.__INITIAL_CATALOG__)) {
    renderItems(filterInitialItems());
  }

  async function load() {
    try {
      const params = new URLSearchParams();
      if (q) params.set('q', q);
      if (category) params.set('category', category);
      const qs = params.toString();
      const r = await fetch('/api/catalog' + (qs ? '?' + qs : ''), { cache: 'no-store' });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        if (Array.isArray(window.__INITIAL_CATALOG__)) {
          renderItems(filterInitialItems());
          return;
        }
        throw new Error(d.message || d.detail || ('Catalog API returned HTTP ' + r.status));
      }
      if (Array.isArray(d.items)) {
        if (!q && !category) window.__INITIAL_CATALOG__ = d.items;
        renderItems(d.items);
      } else {
        renderItems(filterInitialItems());
      }
    } catch (e) {
      if (Array.isArray(window.__INITIAL_CATALOG__)) {
        renderItems(filterInitialItems());
        return;
      }
      const message = (e && e.message) ? e.message : 'Catalog request failed';
      const grid = document.getElementById('catalog-grid');
      const safeMessage = String(message).replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));
      grid.innerHTML = '<div style="grid-column:1/-1;padding:38px 16px;text-align:center;background:#141414;border:1px solid #333;border-radius:14px"><strong style="display:block;font-size:17px;color:#fff;margin-bottom:8px">Catalog temporarily unavailable</strong><p class="muted" style="margin-bottom:14px">' + safeMessage + '</p><button class="btn-yellow" type="button" id="catalog-retry">Try again</button></div>';
      document.getElementById('catalog-retry')?.addEventListener('click', load);
      document.getElementById('result-count').textContent = 'Unavailable';
    }
  }
  await load();
})();
</script>`;
  return renderBaseLayout({ title: 'HITLER YT · Movies & Series', content });
}

function renderSearchHtml({ results, query, typeFilter, yearFilter, genreFilter, categoryFilter, categories, genres }) {
  const yearOptions = [];
  for (let y = 2026; y > 1990; y--) {
    yearOptions.push(`<option value="${y}" ${String(yearFilter) === String(y) ? 'selected' : ''}>${y}</option>`);
  }
  const genreSelect = genres.length
    ? `<select name="genre" class="chip" style="appearance:auto;cursor:pointer;padding:7px 12px" onchange="this.form.submit()">
        <option value="">All Genres</option>
        ${genres.map((g) => `<option value="${escapeHtml(g)}" ${genreFilter === g ? 'selected' : ''}>${escapeHtml(g)}</option>`).join('')}
      </select>`
    : '';
  const catSelect = categories.length
    ? `<select name="category" class="chip" style="appearance:auto;cursor:pointer;padding:7px 12px" onchange="this.form.submit()">
        <option value="">All Categories</option>
        ${categories.map((c) => `<option value="${escapeHtml(c.slug)}" ${categoryFilter === c.slug ? 'selected' : ''}>${escapeHtml(c.name)}</option>`).join('')}
      </select>`
    : '';

  const cardsHtml = results.length
    ? results
        .map((p) => {
          const poster = pagePosterPath(p.poster_path);
          const year = p.release_date ? ` (${String(p.release_date).slice(0, 4)})` : '';
          const mediaType = p.media_type ? `${escapeHtml(String(p.media_type).toUpperCase())} · ` : '';
          const genreText = Array.isArray(p.genres) && p.genres.length ? escapeHtml(p.genres.slice(0, 2).join(', ')) : 'WEB-DL · 1080p 720p &amp; 480p';
          return `<a class="poster-card" href="/title/${encodeURIComponent(p.slug)}">
            <img src="${escapeHtml(poster)}" alt="${escapeHtml(p.title)}" loading="lazy">
            <div class="info">
              <div class="title">${escapeHtml(p.title)}${escapeHtml(year)}</div>
              <div class="meta">${mediaType}${genreText}</div>
            </div>
          </a>`;
        })
        .join('')
    : `<p class="muted" style="grid-column:1/-1;padding:48px 0;text-align:center">
        No titles matching your filters were found.<br>
        <a href="/search" style="color:var(--yellow);margin-top:10px;display:inline-block">Clear all filters</a>
      </p>`;

  const content = `
<form class="search-bar" action="/search" method="get" style="margin:10px 0 6px">
  <i class="fa-solid fa-magnifying-glass" style="color:#6b7280;font-size:14px"></i>
  <input name="q" value="${escapeHtml(query)}" placeholder="Search here..." autocomplete="off" maxlength="100">
  <button type="submit">Search</button>
</form>

<form action="/search" method="get" class="filters" style="margin:14px 0 6px">
  ${query ? `<input type="hidden" name="q" value="${escapeHtml(query)}">` : ''}
  <div class="cat-nav" style="margin-bottom:10px">
    <select name="type" class="chip" style="appearance:auto;cursor:pointer;padding:7px 12px" onchange="this.form.submit()">
      <option value="">All Types</option>
      <option value="movie" ${typeFilter === 'movie' ? 'selected' : ''}>Movies</option>
      <option value="tv" ${typeFilter === 'tv' ? 'selected' : ''}>Web Series</option>
    </select>
    <select name="year" class="chip" style="appearance:auto;cursor:pointer;padding:7px 12px" onchange="this.form.submit()">
      <option value="">All Years</option>
      ${yearOptions.join('')}
    </select>
    ${genreSelect}
    ${catSelect}
    <button type="submit" class="btn-yellow" style="padding:7px 14px;font-size:12px">Apply</button>
    ${query || typeFilter || yearFilter || genreFilter || categoryFilter ? '<a href="/search" class="chip">Clear</a>' : ''}
  </div>
</form>

<div class="section-head" id="catalog">
  <h2>${query ? `Results for “${escapeHtml(query)}”` : 'Browse Library'}</h2>
  <span class="chip">${results.length} titles</span>
</div>

<div class="poster-grid">${cardsHtml}</div>`;

  return renderBaseLayout({
    title: `${query ? `Search: ${query}` : 'Search'} · HITLER YT`,
    content,
  });
}

function renderTitleHtml(page, cloudflareUrl) {
  const headExtra = `<style>
    .detail{display:grid;grid-template-columns:minmax(160px,260px) 1fr;gap:24px;align-items:start}
    .detail img{width:100%;border-radius:12px;border:1px solid #333;box-shadow:0 12px 40px #000a}
    .detail h1{font-size:clamp(24px,5vw,42px);line-height:1.1;margin:0 0 10px;font-weight:800}
    .meta{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}
    .meta span{background:#1a1a1a;border:1px solid #333;padding:5px 11px;border-radius:20px;color:#d1d5db;font-size:12px}
    .eyebrow{color:var(--yellow);font-weight:700;font-size:12px;letter-spacing:1px;margin-bottom:6px}
    .tools{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
    .tools .btn-yellow{font-size:13px;padding:10px 16px}
    .link-box{background:linear-gradient(145deg,#111827,#0b1220);border:1px solid #334155;border-radius:14px;padding:16px;margin-top:18px;box-shadow:0 12px 30px #0005}
    .link-box h2{font-size:18px;margin:0 0 12px;display:flex;align-items:center;gap:9px}
    .link-box.watch h2{color:#60a5fa}.link-box.download h2{color:#34d399}
    .group-block{background:#0b1728;border:1px solid #263b56;border-radius:10px;padding:12px;margin-top:10px}
    .group-block h3{font-size:14px;margin:0 0 10px;color:#e5e7eb}
    @media(max-width:650px){.detail{grid-template-columns:1fr}.detail img{max-width:200px}}
  </style>`;

  const poster = pagePosterPath(page.poster_path);
  const allGroups = Array.isArray(page.groups) ? page.groups : [];
  const boxes = [
    ['watch', 'Watch', '▶'],
    ['download', 'Download', '⬇'],
  ];

  const boxesHtml = boxes
    .map(([boxKey, boxTitle, boxIcon]) => {
      const boxGroups = allGroups.filter((g) => (g?.box || 'watch') === boxKey);
      const groupsContent = boxGroups.length
        ? boxGroups
            .map((group) => {
              const buttons = Array.isArray(group.buttons) ? group.buttons : [];
              const btnsHtml = buttons
                .map(
                  (btn) =>
                    `<a class="btn-yellow cf-media-btn" href="${escapeHtml(btn.url || '#')}" data-raw-url="${escapeHtml(btn.url || '#')}" rel="noopener" target="_blank">${escapeHtml(btn.name || 'Link')}</a>`,
                )
                .join('');
              return `<div class="group-block">
                <h3>${escapeHtml(group.name || '')}</h3>
                <div class="tools">${btnsHtml}</div>
              </div>`;
            })
            .join('')
        : `<p class="muted" style="margin:0;font-size:13px">No ${escapeHtml(boxTitle.toLowerCase())} links added yet.</p>`;

      return `<section class="link-box ${boxKey}" aria-label="${escapeHtml(boxTitle)} links">
        <h2><span aria-hidden="true">${boxIcon}</span>${escapeHtml(boxTitle)}</h2>
        ${groupsContent}
      </section>`;
    })
    .join('');

  const genresHtml =
    Array.isArray(page.genres) && page.genres.length
      ? `<div class="meta">${page.genres.map((g) => `<span>${escapeHtml(g)}</span>`).join('')}</div>`
      : '';

  const content = `
<article class="detail">
  <img src="${escapeHtml(poster)}" alt="${escapeHtml(page.title)} poster">
  <div>
    <div class="eyebrow">HITLER YT · ${escapeHtml(String(page.media_type || 'movie').toUpperCase())}</div>
    <h1>${escapeHtml(page.title)}</h1>
    <div class="meta">
      <span>${escapeHtml(page.release_date || 'Release date unavailable')}</span>
      <span>★ ${escapeHtml(page.rating ?? 'N/A')}</span>
    </div>
    <p class="muted" style="margin:10px 0 14px;line-height:1.5">${escapeHtml(page.overview || '')}</p>
    ${genresHtml}
    ${page.imdb_id ? `<p class="muted" style="margin-top:8px">IMDb: ${escapeHtml(page.imdb_id)}</p>` : ''}
    ${page.tagline ? `<p style="margin-top:8px"><em>${escapeHtml(page.tagline)}</em></p>` : ''}
    ${boxesHtml}
  </div>
</article>
<script>
(async () => {
  let cfBase = ${safeJsonForScript(cloudflareUrl || '')};
  function rewriteToCloudflare(rawHref, baseOrigin) {
    if (!rawHref || !baseOrigin) return rawHref;
    const cleanBase = String(baseOrigin).trim().replace(/\\/+$/, '');
    if (!cleanBase.startsWith('http://') && !cleanBase.startsWith('https://')) return rawHref;
    if (rawHref.startsWith('/')) return cleanBase + rawHref;
    try {
      const u = new URL(rawHref, window.location.origin);
      const cf = new URL(cleanBase);
      if (u.protocol === 'http:' || u.protocol === 'https:') {
        u.protocol = cf.protocol;
        u.host = cf.host;
        return u.toString();
      }
    } catch (_) {}
    return rawHref;
  }
  function applyCloudflareButtons(baseOrigin) {
    if (!baseOrigin) return;
    document.querySelectorAll('a.cf-media-btn').forEach(a => {
      const raw = a.getAttribute('data-raw-url') || a.getAttribute('href') || '';
      const updated = rewriteToCloudflare(raw, baseOrigin);
      if (updated) a.setAttribute('href', updated);
    });
  }
  if (cfBase) applyCloudflareButtons(cfBase);
  try {
    const r = await fetch('/api/site-settings', { cache: 'no-store' });
    if (r.ok) {
      const s = await r.json();
      const latestCf = s.cloudflare_url || s.backend_url || cfBase;
      if (latestCf) applyCloudflareButtons(latestCf);
    }
  } catch (_) {}
})();
</script>`;

  return renderBaseLayout({
    title: `${page.title || 'Title'} · HITLER YT`,
    headExtra,
    content,
  });
}

function renderPanelHtml(settings) {
  const headExtra = `<style>
    .admin-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
    .admin-card{padding:18px}
    .switch{display:flex;justify-content:space-between;align-items:center;padding:12px 0;border-bottom:1px solid #1e3d64}
    .switch input{accent-color:#3b82f6;width:20px;height:20px}
    .admin-card input,.admin-card select{width:100%;padding:11px;background:#091a31;border:1px solid #28558b;color:white;border-radius:10px;margin:6px 0}
    .result{padding:12px;border:1px solid #244c7c;border-radius:10px;margin:10px 0;background:#071426}
    #tmdb-results .result,#categories .result{cursor:pointer}
    #tmdb-results .result:hover{background:#10294a}
    #page-search{font-weight:700}
    .admin-card h2,.admin-card h3{font-weight:900;letter-spacing:.2px}
    .admin-card button{font-weight:800}
    .box-panel{border:1px solid #244c7c;border-radius:14px;padding:16px;background:#061120}
    .box-panel.watch-panel{border-color:#2563eb}
    .box-panel.download-panel{border-color:#10b981}
    .group-card{border:1px solid #2b5485;border-radius:12px;padding:12px;margin:12px 0;background:#0b1c33}
    .group-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
    .btn-row{border:1px solid #1e3a5f;border-radius:10px;padding:10px;margin-top:8px;background:#071324}
    .field-label{display:block;font-size:12px;font-weight:700;color:#93c5fd;margin-top:6px}
    @media(max-width:600px){
      .admin-card{padding:13px}
      .admin-grid{grid-template-columns:1fr}
      .admin-card input,.admin-card select,.admin-card button{min-height:44px}
    }
  </style>`;

  const switches = [
    ['downloads_enabled', 'Downloads'],
    ['streaming_enabled', 'Streaming'],
    ['built_in_player_enabled', 'Built-in player'],
    ['auto_player_redirect', 'Automatic player redirect'],
  ]
    .map(
      ([key, label]) =>
        `<label class="switch">${escapeHtml(label)}<input type="checkbox" data-key="${key}" ${settings[key] ? 'checked' : ''}></label>`,
    )
    .join('');

  const content = `<h1>HITLER YT · Control Center</h1>
<p class="muted">Manage website and playback settings. Changes are saved to MongoDB.</p>

<div class="admin-grid">
  <section class="card admin-card">
    <h2>Bot & Player</h2>
    <div id="switches">${switches}</div>
    <label class="field-label">Access permissions</label>
    <select id="access_mode">
      <option value="all" ${settings.access_mode === 'all' ? 'selected' : ''}>All users</option>
      <option value="admins" ${settings.access_mode === 'admins' ? 'selected' : ''}>Admins only</option>
      <option value="owners" ${settings.access_mode === 'owners' ? 'selected' : ''}>Owners only</option>
    </select>
    <p id="save-status" class="muted">Changes save automatically.</p>
  </section>

  <section class="card admin-card">
    <h2>🌐 Website & Cloudflare Tunnel URLs</h2>
    <label class="field-label">Cloudflare Try URL (Download, Stream & Button Links)</label>
    <input id="base-url" type="url" placeholder="https://name.trycloudflare.com">
    <label class="field-label">Backend Tunnel URL (Cloudflare → VPS)</label>
    <input id="backend-url" type="url" placeholder="https://name.trycloudflare.com">
    <label class="field-label">Vercel Website URL (Catalog Website)</label>
    <input id="vercel-url" type="url" readonly placeholder="Detected on first Vercel visit">
    <small class="muted" id="website-urls"></small>
    <button class="btn accent" type="button" onclick="saveUrls()" style="margin-top:8px;width:100%">Save & Rewrite Button URLs</button>
    <p id="url-status" class="muted" style="margin-top:6px">All download links, stream links, and title-page button URLs automatically use your Cloudflare trycloudflare.com URL.</p>
  </section>

  <section class="card admin-card">
    <h2>TMDB title search</h2>
    <input id="tmdb-q" placeholder="Search movie or series">
    <button class="btn accent" type="button" onclick="searchTMDB()" style="width:100%">Search TMDB</button>
    <div id="tmdb-results"></div>
  </section>

  <section class="card admin-card">
    <h2>Categories</h2>
    <input id="cat-name" placeholder="New category name">
    <button class="btn accent" type="button" onclick="addCategory()" style="width:100%">Create category</button>
    <div id="categories"></div>
  </section>
</div>

<section class="card admin-card" style="margin-top:16px">
  <h2>Page editor · groups & buttons</h2>
  <p class="muted">Select a page below, then add or edit unlimited groups and buttons in the <strong>Watch box</strong> and <strong>Download box</strong>. Every group name and button can be freely edited.</p>

  <input id="page-search" placeholder="Search by page name or paste /title/page-link" oninput="searchPages()">
  <select id="page-select" onchange="loadPageEditor()"><option value="">Choose a page…</option></select>
  <button class="btn" type="button" onclick="openPastedPage()">Open pasted page link</button>

  <div class="admin-grid" style="margin-top:10px">
    <div>
      <label class="field-label">Title</label>
      <input id="edit-title" placeholder="Title">
      <label class="field-label">Description</label>
      <input id="edit-overview" placeholder="Description">
      <label class="field-label">Poster URL or TMDB poster path</label>
      <input id="edit-poster" placeholder="Poster URL or TMDB poster path">
    </div>
    <div>
      <label class="field-label">Release Date</label>
      <input id="edit-date" placeholder="Release date (YYYY-MM-DD)">
      <label class="field-label">Rating</label>
      <input id="edit-rating" placeholder="Rating (e.g. 8.5)">
      <label class="field-label">Type</label>
      <select id="edit-type"><option value="movie">Movie</option><option value="tv">Web Series</option></select>
      <label class="field-label">Categories (comma-separated slugs)</label>
      <input id="edit-cats" placeholder="movies,web-series">
    </div>
  </div>

  <div class="admin-grid" style="margin:16px 0;align-items:start">
    <section class="box-panel watch-panel">
      <h3 style="color:#60a5fa">▶ WATCH BOX GROUPS</h3>
      <p class="muted" style="font-size:12px;margin:4px 0 8px">Add groups to the Watch box (e.g. Watch 1080p, Episode 1–10, Hindi Dub).</p>
      <label class="field-label">New Watch Group Name</label>
      <input id="new-watch-group-name" placeholder="Enter Watch group name (e.g. Watch Online 1080p)">
      <div class="group-actions">
        <button class="btn accent" type="button" onclick="addGroup('watch')">＋ Add Watch Group</button>
        <button class="btn" type="button" onclick="emptyBox('watch')">Empty Watch Box</button>
      </div>
      <div id="watch-groups-container" style="margin-top:12px"></div>
    </section>

    <section class="box-panel download-panel">
      <h3 style="color:#34d399">⬇ DOWNLOAD BOX GROUPS</h3>
      <p class="muted" style="font-size:12px;margin:4px 0 8px">Add groups to the Download box (e.g. Download 1080p, 720p, 480p).</p>
      <label class="field-label">New Download Group Name</label>
      <input id="new-download-group-name" placeholder="Enter Download group name (e.g. Direct Download 1080p)">
      <div class="group-actions">
        <button class="btn accent" type="button" onclick="addGroup('download')">＋ Add Download Group</button>
        <button class="btn" type="button" onclick="emptyBox('download')">Empty Download Box</button>
      </div>
      <div id="download-groups-container" style="margin-top:12px"></div>
    </section>
  </div>

  <div class="group-actions" style="margin-top:12px">
    <button class="btn accent" type="button" onclick="savePageEditor()">💾 Save Page Changes</button>
    <button class="btn" type="button" onclick="deleteCurrentPage()">🗑️ Delete Page</button>
  </div>
  <p id="edit-status" class="muted" style="margin-top:8px;font-weight:700"></p>
</section>

<script>
async function api(url, opts = {}) {
  const r = await fetch(url, {
    ...opts,
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
  });
  if (!r.ok) {
    const payload = await r.json().catch(() => ({}));
    let errText = payload.detail || payload.message || payload.error || r.statusText || ('HTTP ' + r.status);
    if (Array.isArray(errText)) {
      errText = errText.map(e => (e && (e.msg || e.message)) ? e.msg || e.message : JSON.stringify(e)).join('; ');
    } else if (typeof errText === 'object') {
      errText = JSON.stringify(errText);
    }
    throw new Error(String(errText));
  }
  return r.json();
}

const status = document.getElementById('save-status');

async function loadUrls() {
  try {
    const d = await api('/api/admin/settings');
    document.getElementById('base-url').value = d.cloudflare_url || d.base_url || '';
    document.getElementById('backend-url').value = d.backend_url || d.cloudflare_url || '';
    document.getElementById('vercel-url').value = d.vercel_url || '';
    document.getElementById('website-urls').textContent =
      'Registered Vercel Website URLs: ' + ((d.website_urls || []).join(' · ') || 'Waiting for first Vercel visit');
  } catch (e) {
    document.getElementById('url-status').textContent = e.message;
  }
}

async function saveUrls() {
  const box = document.getElementById('url-status');
  try {
    const baseVal = document.getElementById('base-url').value.trim();
    const backVal = document.getElementById('backend-url').value.trim() || baseVal;
    const d = await api('/api/admin/settings', {
      method: 'PUT',
      body: JSON.stringify({ base_url: baseVal, backend_url: backVal, cloudflare_url: backVal || baseVal }),
    });
    document.getElementById('base-url').value = d.cloudflare_url || d.base_url || '';
    document.getElementById('backend-url').value = d.backend_url || d.cloudflare_url || '';
    document.getElementById('vercel-url').value = d.vercel_url || '';
    document.getElementById('website-urls').textContent =
      'Registered Vercel Website URLs: ' + ((d.website_urls || []).join(' · ') || 'Waiting for first Vercel visit');
    box.textContent = 'Saved to MongoDB! Download links, stream links, and title-page button URLs now use this Cloudflare URL.';
    if (document.getElementById('page-select').value) {
      await loadPageEditor();
    }
  } catch (e) {
    box.textContent = e.message;
  }
}

async function saveSettings() {
  const data = { access_mode: document.getElementById('access_mode').value };
  document.querySelectorAll('[data-key]').forEach(x => (data[x.dataset.key] = x.checked));
  try {
    await api('/api/admin/settings', { method: 'PUT', body: JSON.stringify(data) });
    status.textContent = 'Saved successfully.';
  } catch (e) {
    status.textContent = e.message;
  }
}

document.querySelectorAll('[data-key],#access_mode').forEach(x => x.addEventListener('change', saveSettings));

async function searchTMDB() {
  const q = document.getElementById('tmdb-q').value.trim();
  if (!q) return;
  const d = await api('/api/admin/tmdb-search?q=' + encodeURIComponent(q));
  const box = document.getElementById('tmdb-results');
  box.innerHTML = '';
  d.items.forEach(x => {
    const b = document.createElement('div');
    b.className = 'result';
    b.textContent = x.title + ' · ' + (x.release_date || '');
    b.onclick = () => createPage(x);
    box.append(b);
  });
}

async function createPage(x) {
  const defaultCat = x.media_type === 'tv' ? 'web-series' : 'movies';
  const created = await api('/api/admin/pages', {
    method: 'POST',
    body: JSON.stringify({
      title: x.title,
      overview: x.overview,
      poster_path: x.poster_path || '',
      release_date: x.release_date,
      rating: x.rating,
      media_type: x.media_type,
      tmdb_id: x.id,
      category_ids: [defaultCat],
      groups: [],
    }),
  });
  await loadPageList();
  if (created && created.slug) {
    document.getElementById('page-select').value = created.slug;
    await loadPageEditor();
  }
  alert('Page created and loaded in the editor below!');
}

async function loadPageList() {
  await searchPages();
  const requested = new URLSearchParams(location.search).get('page');
  if (requested) {
    const sel = document.getElementById('page-select');
    if ([...sel.options].some(o => o.value === requested)) {
      sel.value = requested;
      await loadPageEditor();
    }
  }
}

let pageSearchTimer;
async function searchPages() {
  clearTimeout(pageSearchTimer);
  return new Promise(resolve => {
    pageSearchTimer = setTimeout(async () => {
      try {
        let q = document.getElementById('page-search').value.trim();
        const match = q.match(/\\/title\\/([a-z0-9-]+)/i);
        if (match) q = match[1];
        const d = await api('/api/admin/pages' + (q ? '?q=' + encodeURIComponent(q) : ''));
        const sel = document.getElementById('page-select');
        const current = sel.value;
        sel.innerHTML = '<option value="">Choose a page…</option>';
        d.items.forEach(p => {
          const o = document.createElement('option');
          o.value = p.slug;
          o.textContent = p.title + ' · ' + p.slug;
          sel.append(o);
        });
        if (current && [...sel.options].some(o => o.value === current)) sel.value = current;
        if (match && d.items.length === 1) {
          sel.value = d.items[0].slug;
          await loadPageEditor();
        }
      } catch (e) {
        document.getElementById('edit-status').textContent = e.message;
      } finally {
        resolve();
      }
    }, 180);
  });
}

async function openPastedPage() {
  const raw = document.getElementById('page-search').value.trim();
  const match = raw.match(/\\/title\\/([a-z0-9-]+)/i);
  const slug = match ? match[1] : raw;
  const sel = document.getElementById('page-select');
  if ([...sel.options].some(o => o.value === slug)) {
    sel.value = slug;
    await loadPageEditor();
    return;
  }
  try {
    const p = await api('/api/admin/pages/' + encodeURIComponent(slug));
    await searchPages();
    sel.value = p.slug;
    await loadPageEditor();
  } catch (e) {
    document.getElementById('edit-status').textContent = 'Page not found. Search by its title or paste its /title/ link.';
  }
}

let currentGroups = [];

async function loadPageEditor() {
  const slug = document.getElementById('page-select').value;
  if (!slug) return;
  const p = await api('/api/admin/pages/' + encodeURIComponent(slug));
  document.getElementById('edit-title').value = p.title || '';
  document.getElementById('edit-overview').value = p.overview || '';
  document.getElementById('edit-poster').value = p.poster_path || '';
  document.getElementById('edit-date').value = p.release_date || '';
  document.getElementById('edit-rating').value = p.rating ?? '';
  document.getElementById('edit-type').value = p.media_type || 'movie';
  document.getElementById('edit-cats').value = (p.category_slugs || []).join(',');
  currentGroups = (p.groups || []).map(g => ({
    box: g.box === 'download' ? 'download' : 'watch',
    name: g.name || '',
    buttons: Array.isArray(g.buttons) ? g.buttons.map(b => ({ name: b.name || '', url: b.url || '' })) : [],
  }));
  renderGroups();
}

function moveGroupWithinBox(globalIdx, direction) {
  const targetBox = currentGroups[globalIdx]?.box || 'watch';
  const sameBoxIndices = [];
  currentGroups.forEach((g, idx) => {
    if ((g.box || 'watch') === targetBox) sameBoxIndices.push(idx);
  });
  const pos = sameBoxIndices.indexOf(globalIdx);
  const swapPos = pos + direction;
  if (pos === -1 || swapPos < 0 || swapPos >= sameBoxIndices.length) return;
  const otherGlobalIdx = sameBoxIndices[swapPos];
  const tmp = currentGroups[globalIdx];
  currentGroups[globalIdx] = currentGroups[otherGlobalIdx];
  currentGroups[otherGlobalIdx] = tmp;
  renderGroups();
}

function renderGroups() {
  const watchHost = document.getElementById('watch-groups-container');
  const downloadHost = document.getElementById('download-groups-container');
  watchHost.innerHTML = '';
  downloadHost.innerHTML = '';

  const watchIndices = [];
  const downloadIndices = [];
  currentGroups.forEach((g, idx) => {
    if ((g.box || 'watch') === 'download') downloadIndices.push(idx);
    else watchIndices.push(idx);
  });

  if (!watchIndices.length) {
    watchHost.innerHTML = '<p class="muted" style="font-size:12px">No Watch groups yet. Enter a name above and click "＋ Add Watch Group".</p>';
  }
  if (!downloadIndices.length) {
    downloadHost.innerHTML = '<p class="muted" style="font-size:12px">No Download groups yet. Enter a name above and click "＋ Add Download Group".</p>';
  }

  currentGroups.forEach((g, gi) => {
    const boxType = (g.box || 'watch') === 'download' ? 'download' : 'watch';
    g.box = boxType;
    const host = boxType === 'download' ? downloadHost : watchHost;
    const boxList = boxType === 'download' ? downloadIndices : watchIndices;
    const posInBox = boxList.indexOf(gi);

    const card = document.createElement('div');
    card.className = 'group-card';

    const nameLabel = document.createElement('label');
    nameLabel.className = 'field-label';
    nameLabel.textContent = (boxType === 'watch' ? '▶ Watch' : '⬇ Download') + ' Group Name (editable):';
    card.append(nameLabel);

    const nameInput = document.createElement('input');
    nameInput.type = 'text';
    nameInput.value = g.name || '';
    nameInput.placeholder = boxType === 'watch' ? 'e.g. Watch 1080p / Episode 1' : 'e.g. Download 1080p / 720p';
    nameInput.oninput = () => {
      g.name = nameInput.value;
    };
    card.append(nameInput);

    const boxLabel = document.createElement('label');
    boxLabel.className = 'field-label';
    boxLabel.textContent = 'Group Box Location:';
    card.append(boxLabel);

    const boxSelect = document.createElement('select');
    boxSelect.innerHTML = '<option value="watch">▶ Watch Box</option><option value="download">⬇ Download Box</option>';
    boxSelect.value = boxType;
    boxSelect.onchange = () => {
      g.box = boxSelect.value;
      renderGroups();
      document.getElementById('edit-status').textContent =
        'Moved "' + (g.name || 'Group') + '" to ' + (g.box === 'watch' ? 'Watch' : 'Download') + ' box. Click Save Page Changes to publish.';
    };
    card.append(boxSelect);

    const actions = document.createElement('div');
    actions.className = 'group-actions';

    const switchBtn = document.createElement('button');
    switchBtn.className = 'btn';
    switchBtn.type = 'button';
    switchBtn.textContent = boxType === 'watch' ? '⇄ Move to Download Box' : '⇄ Move to Watch Box';
    switchBtn.onclick = () => {
      g.box = boxType === 'watch' ? 'download' : 'watch';
      renderGroups();
      document.getElementById('edit-status').textContent =
        'Moved "' + (g.name || 'Group') + '" to ' + (g.box === 'watch' ? 'Watch' : 'Download') + ' box. Click Save Page Changes to publish.';
    };
    actions.append(switchBtn);

    const copyBtn = document.createElement('button');
    copyBtn.className = 'btn';
    copyBtn.type = 'button';
    copyBtn.textContent = boxType === 'watch' ? '📋 Copy to Download Box' : '📋 Copy to Watch Box';
    copyBtn.onclick = () => {
      const targetBox = boxType === 'watch' ? 'download' : 'watch';
      const clonedButtons = (g.buttons || []).map(b => ({ name: b.name || '', url: b.url || '' }));
      currentGroups.push({
        box: targetBox,
        name: g.name || (targetBox === 'watch' ? 'Watch Group' : 'Download Group'),
        buttons: clonedButtons,
      });
      renderGroups();
      document.getElementById('edit-status').textContent =
        'Copied "' + (g.name || 'Group') + '" to ' + (targetBox === 'watch' ? 'Watch' : 'Download') + ' box.';
    };
    actions.append(copyBtn);

    const upBtn = document.createElement('button');
    upBtn.className = 'btn';
    upBtn.type = 'button';
    upBtn.textContent = '↑ Up';
    upBtn.disabled = posInBox <= 0;
    upBtn.onclick = () => moveGroupWithinBox(gi, -1);
    actions.append(upBtn);

    const downBtn = document.createElement('button');
    downBtn.className = 'btn';
    downBtn.type = 'button';
    downBtn.textContent = '↓ Down';
    downBtn.disabled = posInBox === -1 || posInBox >= boxList.length - 1;
    downBtn.onclick = () => moveGroupWithinBox(gi, 1);
    actions.append(downBtn);

    const delBtn = document.createElement('button');
    delBtn.className = 'btn';
    delBtn.type = 'button';
    delBtn.textContent = '🗑️ Delete Group';
    delBtn.onclick = () => {
      currentGroups.splice(gi, 1);
      renderGroups();
      document.getElementById('edit-status').textContent = 'Group deleted locally. Click Save Page Changes to update MongoDB.';
    };
    actions.append(delBtn);

    card.append(actions);

    const buttons = g.buttons || (g.buttons = []);
    const btnContainer = document.createElement('div');
    buttons.forEach((bt, bi) => {
      const row = document.createElement('div');
      row.className = 'btn-row';

      const bNameLabel = document.createElement('label');
      bNameLabel.className = 'field-label';
      bNameLabel.textContent = 'Button #' + (bi + 1) + ' Name:';
      const bn = document.createElement('input');
      bn.type = 'text';
      bn.placeholder = boxType === 'watch' ? 'e.g. Watch 1080p / Episode 1' : 'e.g. Download 1080p (1.4 GB)';
      bn.value = bt.name || '';
      bn.oninput = () => {
        bt.name = bn.value;
      };

      const bUrlLabel = document.createElement('label');
      bUrlLabel.className = 'field-label';
      bUrlLabel.textContent = 'Button #' + (bi + 1) + ' Link URL:';
      const url = document.createElement('input');
      url.type = 'text';
      url.placeholder = boxType === 'watch' ? 'https://.../watch/CODE or /watch/CODE' : 'https://.../dl/CODE or /dl/CODE';
      url.value = bt.url || '';
      url.oninput = () => {
        bt.url = url.value;
      };

      const removeBtn = document.createElement('button');
      removeBtn.className = 'btn';
      removeBtn.type = 'button';
      removeBtn.style.marginTop = '6px';
      removeBtn.textContent = '✕ Remove Button #' + (bi + 1);
      removeBtn.onclick = () => {
        buttons.splice(bi, 1);
        renderGroups();
      };

      row.append(bNameLabel, bn, bUrlLabel, url, removeBtn);
      btnContainer.append(row);
    });
    card.append(btnContainer);

    const addBtn = document.createElement('button');
    addBtn.className = 'btn accent';
    addBtn.type = 'button';
    addBtn.style.marginTop = '10px';
    addBtn.style.width = '100%';
    addBtn.textContent = '＋ Add Button to "' + (g.name || 'Group') + '"';
    addBtn.onclick = () => {
      const nextNum = buttons.length + 1;
      const defaultBtnName = (boxType === 'watch' ? 'Watch ' : 'Download ') + nextNum;
      buttons.push({ name: defaultBtnName, url: '' });
      renderGroups();
    };
    card.append(addBtn);

    host.append(card);
  });
}

function addGroup(box = 'watch') {
  const slug = document.getElementById('page-select').value;
  if (!slug) {
    alert('Please select or create a page first before adding groups.');
    return;
  }
  const inputId = box === 'download' ? 'new-download-group-name' : 'new-watch-group-name';
  const nameInputEl = document.getElementById(inputId);
  const customName = nameInputEl ? nameInputEl.value.trim() : '';
  const count = currentGroups.filter(g => (g.box || 'watch') === box).length + 1;
  const defaultName = (box === 'download' ? 'Download Group ' : 'Watch Group ') + count;
  const finalName = customName || defaultName;
  if (nameInputEl) nameInputEl.value = '';

  currentGroups.push({
    box: box === 'download' ? 'download' : 'watch',
    name: finalName,
    buttons: [{ name: box === 'download' ? 'Direct Download' : 'Watch Online', url: '' }],
  });
  renderGroups();
  document.getElementById('edit-status').textContent =
    'Added "' + finalName + '" to the ' + (box === 'download' ? 'Download' : 'Watch') + ' box. Add button links and click Save Page Changes.';
}

function emptyBox(box) {
  const label = box === 'watch' ? 'Watch' : 'Download';
  const count = currentGroups.filter(g => (g.box || 'watch') === box).length;
  if (!count) {
    document.getElementById('edit-status').textContent = label + ' box is already empty.';
    return;
  }
  if (!confirm('Remove all ' + count + ' group(s) from the ' + label + ' box?')) return;
  currentGroups = currentGroups.filter(g => (g.box || 'watch') !== box);
  renderGroups();
  document.getElementById('edit-status').textContent = label + ' box cleared. Click Save Page Changes to publish.';
}

async function savePageEditor() {
  const slug = document.getElementById('page-select').value;
  if (!slug) {
    alert('Please choose a page to save.');
    return;
  }
  try {
    const cleanedGroups = currentGroups.map((g, idx) => {
      const box = (g.box || 'watch') === 'download' ? 'download' : 'watch';
      const name = (g.name || '').trim() || ((box === 'download' ? 'Download Group ' : 'Watch Group ') + (idx + 1));
      const buttons = (g.buttons || [])
        .map((b, bi) => ({
          name: (b.name || '').trim() || ((box === 'download' ? 'Download ' : 'Watch ') + (bi + 1)),
          url: (b.url || '').trim(),
        }))
        .filter(b => b.url.length > 0);
      return { box, name, buttons };
    });

    await api('/api/admin/pages/' + encodeURIComponent(slug), {
      method: 'PUT',
      body: JSON.stringify({
        title: document.getElementById('edit-title').value.trim(),
        overview: document.getElementById('edit-overview').value,
        poster_path: document.getElementById('edit-poster').value.trim(),
        release_date: document.getElementById('edit-date').value.trim(),
        rating: document.getElementById('edit-rating').value ? Number(document.getElementById('edit-rating').value) : null,
        media_type: document.getElementById('edit-type').value,
        category_ids: document.getElementById('edit-cats').value.split(',').map(x => x.trim()).filter(Boolean),
        groups: cleanedGroups,
      }),
    });
    await loadPageEditor();
    document.getElementById('edit-status').textContent = '✅ Saved to shared MongoDB! Watch & Download groups and Cloudflare button URLs are now live.';
  } catch (e) {
    document.getElementById('edit-status').textContent = '❌ Save failed: ' + e.message;
  }
}

async function deleteCurrentPage() {
  const slug = document.getElementById('page-select').value;
  if (!slug) return;
  if (!confirm('Permanently delete this catalog page from shared MongoDB?')) return;
  try {
    await api('/api/admin/pages/' + encodeURIComponent(slug), { method: 'DELETE' });
    document.getElementById('edit-status').textContent = 'Page deleted from shared MongoDB.';
    document.getElementById('page-select').value = '';
    currentGroups = [];
    renderGroups();
    await loadPageList();
  } catch (e) {
    document.getElementById('edit-status').textContent = e.message;
  }
}

async function addCategory() {
  const name = document.getElementById('cat-name').value.trim();
  if (!name) return;
  await api('/api/admin/categories', { method: 'POST', body: JSON.stringify({ name }) });
  document.getElementById('cat-name').value = '';
  loadCategories();
}

async function loadCategories() {
  const d = await api('/api/admin/categories');
  const box = document.getElementById('categories');
  box.innerHTML = '';
  d.items.forEach(x => {
    const row = document.createElement('div');
    row.className = 'result';
    row.textContent = x.name + ' (' + x.slug + ')';
    if (!['movies', 'web-series'].includes(x.slug)) {
      const b = document.createElement('button');
      b.className = 'btn';
      b.style.marginLeft = '8px';
      b.textContent = 'Delete';
      b.onclick = async () => {
        await api('/api/admin/categories/' + x.slug, { method: 'DELETE' });
        loadCategories();
      };
      row.append(' ', b);
    }
    box.append(row);
  });
}

loadCategories();
loadPageList();
loadUrls();
</script>`;

  return renderBaseLayout({
    title: 'Admin Panel · HITLER YT',
    headExtra,
    content,
  });
}

function renderWatchHtml(file, settings, streamBaseUrl) {
  const cfOrigin = validPublicUrl(streamBaseUrl) || settings.cloudflare_url || settings.backend_url || settings.base_url || '';
  const sizeMb = file.file_size ? (Number(file.file_size) / (1024 * 1024)).toFixed(2) : '0.00';
  const dlHref = cfOrigin ? `${cfOrigin}/dl/${encodeURIComponent(file.short_code)}` : `/dl/${encodeURIComponent(file.short_code)}`;

  const headExtra = `<style>
    ${!settings.built_in_player_enabled ? '.player-shell,.card.info{display:none!important}' : ''}
    ${!settings.streaming_enabled ? '.player-shell{display:none!important}' : ''}
    .player-shell{position:relative;border-radius:20px;padding:2px;overflow:hidden;background:#0a0a14;box-shadow:0 8px 40px rgba(0,0,0,.55),0 0 28px rgba(59,130,246,.18)}
    .player-box{background:#000;border-radius:18px;overflow:hidden;position:relative;z-index:1}
    video{display:block;width:100%;max-height:72vh;background:#000;aspect-ratio:16/9}
    .info{padding:18px 16px}
    .meta{display:flex;flex-wrap:wrap;gap:10px;margin-top:8px;color:var(--muted);font-size:13px}
    .meta span{background:rgba(59,130,246,.08);border:1px solid rgba(59,130,246,.2);padding:3px 10px;border-radius:20px;font-size:12px}
    .tools{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}
    .player-panel{display:none}.player-panel.active{display:block}
    .player-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:8px}
    .pbtn{min-height:46px;border:1px solid var(--line);border-radius:12px;background:linear-gradient(180deg,#14142c,#0e0e20);color:#fff;display:flex;align-items:center;justify-content:center;text-align:center;padding:10px;cursor:pointer;font-size:13px}
    .copy-ok{font-size:12px;color:#69e69a;min-height:18px;margin-top:6px}
    .filename{font-weight:700;word-break:break-word;font-size:16px}
    .side .card{padding:16px}
    @media(min-width:900px){.watch-grid{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:16px}.side{align-self:start;position:sticky;top:72px}}
  </style>`;

  const content = `
<div class="watch-grid">
  <section>
    <div class="player-shell">
      <div class="player-box">
        <video id="player" controls playsinline preload="auto" crossorigin="anonymous">
          <source id="video-source" src="" type="video/mp4">
          Your browser cannot play this media.
        </video>
      </div>
    </div>
    <div class="card info" style="margin-top:12px">
      <div class="filename">${escapeHtml(file.filename || 'Media')}</div>
      <div class="meta">
        <span>${escapeHtml(sizeMb)} MB</span>
        <span>${escapeHtml(file.access_count || 0)} views</span>
      </div>
      <div class="tools">
        ${settings.downloads_enabled ? `<a class="btn accent" id="direct-dl-btn" href="${escapeHtml(dlHref)}" download>⬇ Download (Chrome)</a><a class="btn" id="idm-dl-btn" href="#">⚡ 1DM Download</a>` : ''}
        <button class="btn" type="button" onclick="copyDownload()">Copy Download Link</button>
        <button class="btn" type="button" onclick="copyStream()">Copy Stream URL</button>
      </div>
      <div id="copy-status" class="copy-ok"></div>
    </div>
  </section>
  <aside class="side">
    <div class="card">
      <b>Open in player / downloader</b>
      <p class="muted" style="font-size:12px;margin:6px 0 14px">Android buttons use explicit VIEW intents via your Cloudflare URL.</p>
      <div id="mobile" class="player-panel active"><div class="player-grid" id="mobile-grid"></div></div>
    </div>
  </aside>
</div>
<script>
const SHORT_CODE = ${safeJsonForScript(file.short_code)};
const STREAM_ORIGIN = (${safeJsonForScript(cfOrigin)} || window.location.origin).replace(/\\/+$/, '');
const STREAM_URL = STREAM_ORIGIN + '/stream/' + SHORT_CODE;
const DL_URL = STREAM_ORIGIN + '/dl/' + SHORT_CODE;
const PLAYER_MIME = ${safeJsonForScript(file.mime_type || 'video/mp4')};
const INTENT_MIME = (!PLAYER_MIME || PLAYER_MIME === 'application/octet-stream') ? 'video/*' : PLAYER_MIME;
const player = document.getElementById('player');
if (player) {
  player.src = STREAM_URL;
  player.load();
}
function idmIntent(url) {
  const u = new URL(url);
  const scheme = u.protocol.replace(':','');
  return 'intent://' + u.host + u.pathname + u.search + '#Intent;scheme=' + scheme + ';action=android.intent.action.VIEW;package=idm.internet.download.manager;S.browser_fallback_url=' + encodeURIComponent(url) + ';end';
}
const dlBtn = document.getElementById('direct-dl-btn');
if (dlBtn) dlBtn.href = DL_URL;
const idmBtn = document.getElementById('idm-dl-btn');
if (idmBtn) idmBtn.href = idmIntent(DL_URL);
function androidIntent(url, pkg, mime) {
  const u = new URL(url);
  const scheme = u.protocol.replace(':','');
  return 'intent://' + u.host + u.pathname + u.search + '#Intent;scheme=' + scheme + ';action=android.intent.action.VIEW;type=' + (mime || 'video/*') + ';package=' + pkg + ';S.browser_fallback_url=' + encodeURIComponent(url) + ';end';
}
function hvStreamIntent(url) {
  const encoded = encodeURIComponent(url);
  return 'intent://play?url=' + encoded + '#Intent;scheme=hvstream;package=com.hvstreamplayer.app;S.browser_fallback_url=' + encoded + ';end';
}
function addPlayer(gridId, label, href) {
  const a = document.createElement('a');
  a.className = 'pbtn';
  a.textContent = label;
  a.href = href;
  if (!href.startsWith('intent:')) { a.target = '_blank'; a.rel = 'noopener'; }
  document.getElementById(gridId)?.appendChild(a);
}
[['1DM Download', 'idm'],['HV Stream', null],['PLAYit', 'com.playit.videoplayer'],['VLC', 'org.videolan.vlc'],['MX Player', 'com.mxtech.videoplayer.ad'],['Just Player', 'com.brouken.player']].forEach(([n, p]) => {
  addPlayer('mobile-grid', n, p === 'idm' ? idmIntent(DL_URL) : (n === 'HV Stream' ? hvStreamIntent(STREAM_URL) : androidIntent(STREAM_URL, p, INTENT_MIME)));
});
async function copyStream() {
  try {
    await navigator.clipboard.writeText(STREAM_URL);
    document.getElementById('copy-status').textContent = 'Stream URL copied.';
  } catch (e) {
    window.prompt('Copy stream URL:', STREAM_URL);
  }
}
async function copyDownload() {
  try {
    await navigator.clipboard.writeText(DL_URL);
    document.getElementById('copy-status').textContent = 'Download link copied (paste in Chrome or 1DM).';
  } catch (e) {
    window.prompt('Copy download URL:', DL_URL);
  }
}
</script>`;

  return renderBaseLayout({
    title: `${file.filename || 'Watch'} · HITLER YT`,
    headExtra,
    content,
  });
}


const SHORTENER_HOST = 'hitler-yt.vercel.app';
function requestHost(req) {
  return String(req.headers['x-forwarded-host'] || req.headers.host || '').split(',')[0].trim().toLowerCase().split(':')[0];
}
function isShortenerHost(host) {
  const configured = String(process.env.VERCEL_PROJECT_PRODUCTION_URL || process.env.VERCEL_URL || '').toLowerCase().replace(/^https?:\/\//, '').split('/')[0].split(':')[0];
  return host === SHORTENER_HOST || host === `www.${SHORTENER_HOST}` || Boolean(configured && (host === configured || host === `www.${configured}`)) || host.endsWith('.vercel.app');
}
function isProtectedTitlePath(path) {
  return typeof path === 'string' && path.startsWith('/title/') && path.length > 7 && !path.startsWith('//');
}
function parseCookies(req) {
  const out = {};
  for (const part of String(req.headers.cookie || '').split(';')) {
    const i = part.indexOf('=');
    if (i > 0) out[part.slice(0, i).trim()] = decodeURIComponent(part.slice(i + 1).trim());
  }
  return out;
}
async function readFormBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(Buffer.from(chunk));
  return new URLSearchParams(Buffer.concat(chunks).toString('utf8'));
}
function sendKeyPage(res, target, error = '') {
  const safeTarget = escapeHtml(target);
  const encodedTarget = encodeURIComponent(target);
  const safeError = error ? `<p style="color:#f87171;margin-top:14px">${escapeHtml(error)}</p>` : '';
  sendHtml(res, 200, `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Key Verification · HITLER YT</title><style>*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#090d18;color:#f8fafc;font:15px system-ui;padding:20px}main{width:min(420px,100%);padding:28px;border:1px solid #263449;border-radius:18px;background:#111827}h1{font-size:22px}p{color:#aab7ca;font-size:14px}input,button,a{width:100%;display:block;padding:13px;margin-top:12px;border-radius:10px;font:inherit}input{background:#0b1220;border:1px solid #334155;color:white}button{border:0;background:#38bdf8;color:#06111b;font-weight:800}a{text-align:center;color:#e2e8f0;border:1px solid #334155;text-decoration:none}</style></head><body><main><h1>🔐 Verify access key</h1><p>This title page is protected. Enter a valid key or generate one through the shortener.</p><form method="POST" action="/verify-key"><input type="hidden" name="target" value="${safeTarget}"><input name="key" placeholder="Enter access key" required autocomplete="off"><button>Open title page</button></form><a href="/get-key-start?target=${encodedTarget}">Get access key</a>${safeError}</main></body></html>`);
}
async function createVercelShortUrl(provider, destination) {
  // The provider URL is the API endpoint (for example, https://shortlinkgenerator.onrender.com/api).
  // Build the exact documented request: ?api=KEY&url=DESTINATION&format=json.
  const endpoint = new URL(String(provider.url || '').trim());
  endpoint.searchParams.set('api', String(provider.api_key || '').trim());
  endpoint.searchParams.set('url', String(destination));
  endpoint.searchParams.set('format', 'json');
  const response = await fetch(endpoint.toString(), {
    signal: AbortSignal.timeout(25000),
    redirect: 'follow',
    headers: { Accept: 'application/json, text/plain, */*' },
  });
  const body = await response.text();
  if (!response.ok) throw new Error(`Shortener returned HTTP ${response.status}`);

  let data;
  try { data = JSON.parse(body); } catch { data = body; }
  const candidates = [];
  const urlFields = new Set([
    'shortenedurl', 'shorturl', 'short_url', 'shortened_url', 'shortlink',
    'short_link', 'shortenedlink', 'shortened_link', 'shortened', 'short',
    'shortenedurl', 'shortened_link_url', 'shortened_url_link', 'url_short',
    'link', 'url', 'result',
  ]);
  const collect = (value, depth = 0) => {
    if (depth > 6 || value == null) return;
    if (typeof value === 'string') {
      candidates.push(value);
      // Accept URLs embedded in labels such as `Short URL: https://...`.
      const embedded = value.match(/https?:\/\/[^\s\"'<>\]]+/gi) || [];
      candidates.push(...embedded.map((item) => item.replace(/[),.;]+$/, '')));
      // Some APIs wrap the actual URL in a JSON-encoded string.
      if ((value.startsWith('{') || value.startsWith('['))) {
        try { collect(JSON.parse(value), depth + 1); } catch {}
      }
      return;
    }
    if (Array.isArray(value)) {
      for (const item of value) collect(item, depth + 1);
      return;
    }
    if (typeof value === 'object') {
      for (const [key, item] of Object.entries(value)) {
        if (urlFields.has(key.toLowerCase())) collect(item, depth + 1);
      }
      // Also inspect nested `data`, `result`, `response`, and `shortener` envelopes.
      for (const [key, item] of Object.entries(value)) {
        if (item && typeof item === 'object' && !urlFields.has(key.toLowerCase())) collect(item, depth + 1);
      }
    }
  };
  collect(data);
  for (const value of candidates) {
    try {
      const candidate = String(value).trim().replace(/^['"`]|['"`]$/g, '');
      const parsed = new URL(candidate);
      if (['http:', 'https:'].includes(parsed.protocol) && parsed.hostname) return parsed.toString();
    } catch {}
  }
  const apiMessage = data && typeof data === 'object'
    ? String(data.message || data.error || data.detail || '').slice(0, 180)
    : '';
  const diagnostic = typeof body === 'string' ? body.replace(/\s+/g, ' ').slice(0, 240) : '';
  throw new Error(apiMessage || `Shortener API returned no valid URL. Provider response: ${diagnostic || '(empty response)'}`);
}

export default async function handler(req, res) {
  try {
    if (req.method === 'OPTIONS') {
      res.setHeader('Access-Control-Allow-Origin', '*');
      res.setHeader('Access-Control-Allow-Methods', 'GET,HEAD,POST,PUT,PATCH,DELETE,OPTIONS');
      res.setHeader('Access-Control-Allow-Headers', 'Range, Content-Type, Authorization, Accept, Origin');
      res.statusCode = 204;
      res.end();
      return;
    }

    const { pathname, search, searchParams } = getRequestPath(req);

    if (pathname === '/robots.txt') {
      const body = 'User-agent: *\nAllow: /\n';
      res.statusCode = 200;
      res.setHeader('Content-Type', 'text/plain; charset=utf-8');
      res.setHeader('Cache-Control', 'public, max-age=300');
      res.setHeader('X-Content-Type-Options', 'nosniff');
      res.setHeader('Content-Length', Buffer.byteLength(body));
      res.end(req.method === 'HEAD' ? undefined : body);
      return;
    }


    if (pathname === '/api/health' || pathname === '/health') {
      try {
        const db = await getDatabase();
        await db.command({ ping: 1 });
        const state = await getSettingsState(db, req);
        sendJson(res, 200, {
          status: 'ok',
          database: 'connected',
          database_name: db.databaseName,
          backend_configured: Boolean(state.cloudflare_url),
          cloudflare_url: state.cloudflare_url,
          vercel_url: state.vercel_url,
          deployment: 'vercel-website',
        });
      } catch (error) {
        sendJson(res, 503, {
          status: 'degraded',
          database: 'unavailable',
          message: 'Check MONGODB_URI, Atlas network access, and database permissions in Vercel.',
          diagnostic: String(error?.message || error).slice(0, 300),
        });
      }
      return;
    }

    let db;
    try {
      db = await getDatabase();
      await syncVercelWebsiteUrls(req, db);
      await ensureDefaultCategories(db);
    } catch (error) {
      console.error('MongoDB connection error in Vercel:', error);
      if (pathname === '/api/catalog') {
        sendJson(res, 503, {
          error: 'Shared MongoDB unavailable',
          message: 'Set MONGODB_URI in Vercel Environment Variables to the same MongoDB Atlas URI used by your VPS bot.',
          items: [],
        });
        return;
      }
      if (pathname === '/api/categories') {
        sendJson(res, 200, {
          items: [
            { name: 'Movies', slug: 'movies' },
            { name: 'Web Series', slug: 'web-series' },
          ],
        });
        return;
      }
      sendJson(res, 503, {
        error: 'Shared database configuration unavailable',
        message: 'Set MONGODB_URI in Vercel to the exact same MongoDB database used by the VPS bot.',
        diagnostic: String(error?.message || error).slice(0, 300),
      });
      return;
    }

    const state = await getSettingsState(db, req);
    const cloudflareUrl = state.cloudflare_url;
    const currentHost = requestHost(req);

    // Shortener/access-key workflow is hosted on Vercel so it protects the actual title pages.
    if (isShortenerHost(currentHost) && pathname === '/get-key-start' && req.method === 'GET') {
      const target = searchParams.get('target') || '';
      if (!isProtectedTitlePath(target)) { sendJson(res, 400, { error: 'Shortener is available only for /title/* pages' }); return; }
      const cfg = await db.collection('site_settings').findOne({ _id: 'shortener_config' }) || {};
      const providers = (cfg.shorteners || []).filter((x) => x && x.url && x.api_key);
      if (!cfg.enabled || !providers.length) { sendJson(res, 503, { error: 'No active shortener configured' }); return; }
      const provider = providers[Math.floor(Math.random() * providers.length)];
      const token = crypto.randomBytes(24).toString('base64url');
      const key = crypto.randomBytes(9).toString('base64url');
      const now = Math.floor(Date.now() / 1000);
      const expiry = Math.max(60, Math.min(86400, Number(cfg.expiry_seconds) || 300));
      const origin = `https://${currentHost}`;
      const landing = `${origin}/get-key/${token}`;
      await db.collection('access_keys').insertOne({ token, key, target, created_at: now, expires_at: now + expiry });
      try { const shortUrl = await createVercelShortUrl(provider, landing); res.statusCode = 302; res.setHeader('Location', shortUrl); res.end(); }
      catch (error) { await db.collection('access_keys').deleteOne({ token }); sendJson(res, 502, { error: 'Shortener API failed', detail: String(error.message || error).slice(0, 160) }); }
      return;
    }
    if (isShortenerHost(currentHost) && pathname.startsWith('/get-key/') && req.method === 'GET') {
      const token = pathname.slice('/get-key/'.length);
      const rec = await db.collection('access_keys').findOne({ token, expires_at: { $gt: Math.floor(Date.now()/1000) } });
      if (!rec) { sendHtml(res, 410, '<h1>Key request expired</h1><p>Return to the title page and request a new key.</p>'); return; }
      const key = escapeHtml(rec.key); const target = escapeHtml(rec.target);
      sendHtml(res, 200, `<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Your access key</title><style>body{background:#090d18;color:white;font:16px system-ui;display:grid;place-items:center;min-height:100vh}main{background:#111827;padding:28px;border-radius:16px;width:min(400px,85vw)}input,button,a{box-sizing:border-box;width:100%;padding:13px;margin-top:12px;border-radius:9px}input{background:#0b1220;color:white;border:1px solid #334155}button{background:#38bdf8;border:0;font-weight:bold}a{display:block;text-align:center;color:white}</style></head><body><main><h2>✅ Your key is ready</h2><p>Copy the key, then continue to your title page.</p><input id="k" readonly value="${key}"><button onclick="navigator.clipboard.writeText(document.getElementById('k').value).then(()=>location.href='/key-copied/${encodeURIComponent(token)}')">Copy key & continue</button><a href="${target}">Return to title page</a></main></body></html>`);
      return;
    }
    if (isShortenerHost(currentHost) && pathname.startsWith('/key-copied/') && req.method === 'GET') {
      const token = pathname.slice('/key-copied/'.length);
      const now = Math.floor(Date.now()/1000);
      const rec = await db.collection('access_keys').findOne({ token, expires_at: { $gt: now } });
      if (!rec) { sendJson(res, 410, { error: 'Key expired' }); return; }
      res.statusCode = 303; res.setHeader('Set-Cookie', `site_access=${encodeURIComponent(token)}; Max-Age=${Math.max(1,rec.expires_at-now)}; Path=/; HttpOnly; Secure; SameSite=Lax`); res.setHeader('Location', rec.target); res.end(); return;
    }
    if (isShortenerHost(currentHost) && pathname === '/verify-key' && req.method === 'POST') {
      const form = await readFormBody(req); const target = form.get('target') || ''; const key = form.get('key') || '';
      if (!isProtectedTitlePath(target)) { sendJson(res, 400, { error: 'Invalid title target' }); return; }
      const now = Math.floor(Date.now()/1000);
      const rec = await db.collection('access_keys').findOne({ key, target, expires_at: { $gt: now } });
      if (!rec) { sendKeyPage(res, target, 'Invalid, expired, or page-specific key.'); return; }
      res.statusCode = 303; res.setHeader('Set-Cookie', `site_access=${encodeURIComponent(rec.token)}; Max-Age=${Math.max(1,rec.expires_at-now)}; Path=/; HttpOnly; Secure; SameSite=Lax`); res.setHeader('Location', target); res.end(); return;
    }

    // Gate only GET/HEAD title routes. Homepage, search, watch, dl, stream and all APIs bypass.
    if (isShortenerHost(currentHost) && isProtectedTitlePath(pathname) && ['GET','HEAD'].includes(req.method)) {
      const cfg = await db.collection('site_settings').findOne({ _id: 'shortener_config' }) || {};
      if (cfg.enabled) {
        const target = pathname + search;
        const token = parseCookies(req).site_access || '';
        const now = Math.floor(Date.now()/1000);
        const authorized = token && await db.collection('access_keys').findOne({ token, target, expires_at: { $gt: now } });
        if (!authorized) { sendKeyPage(res, target); return; }
      }
    }

    if (req.method === 'GET' && pathname === '/api/catalog') {
      const query = { published: true };
      const category = (searchParams.get('category') || '').trim().slice(0, 60);
      const q = (searchParams.get('q') || '').trim().slice(0, 100);
      if (category) query.category_slugs = category;
      if (q) {
        const safe = q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        query.$or = [
          { title: { $regex: safe, $options: 'i' } },
          { overview: { $regex: safe, $options: 'i' } },
          { genres: { $regex: safe, $options: 'i' } },
        ];
      }
      const rawItems = await db
        .collection('catalog_pages')
        .find(query, { projection: { _id: 0 } })
        .sort({ created_at: -1 })
        .limit(100)
        .toArray();
      const items = rawItems.map((item) => serializeCatalogDoc(item, cloudflareUrl));
      sendJson(res, 200, { items, cloudflare_url: cloudflareUrl });
      return;
    }

    if (req.method === 'GET' && pathname === '/api/categories') {
      const rawItems = await db
        .collection('catalog_categories')
        .find({}, { projection: { _id: 0 } })
        .sort({ name: 1 })
        .toArray();
      const items = rawItems.length
        ? rawItems.map((c) => ({ name: c.name, slug: c.slug }))
        : [
            { name: 'Movies', slug: 'movies' },
            { name: 'Web Series', slug: 'web-series' },
          ];
      sendJson(res, 200, { items });
      return;
    }

    if (req.method === 'GET' && pathname === '/api/site-settings') {
      const { _raw_map, ...publicState } = state;
      sendJson(res, 200, publicState);
      return;
    }

    if (req.method === 'GET' && pathname.startsWith('/api/public/title/')) {
      let slug;
      try {
        slug = decodeURIComponent(pathname.slice('/api/public/title/'.length));
      } catch {
        sendJson(res, 400, { error: 'Invalid title slug' });
        return;
      }
      const page = await db.collection('catalog_pages').findOne({ slug, published: true }, { projection: { _id: 0 } });
      if (!page) sendJson(res, 404, { error: 'Title not found' });
      else sendJson(res, 200, serializeCatalogDoc(page, cloudflareUrl));
      return;
    }

    if (pathname.startsWith('/api/admin/')) {
      if (!(await verifyAdminAuth(req, db))) {
        sendUnauthorized(res);
        return;
      }

      if (pathname === '/api/admin/settings' && req.method === 'GET') {
        const { _raw_map, ...publicState } = state;
        sendJson(res, 200, publicState);
        return;
      }

      if (pathname === '/api/admin/settings' && req.method === 'PUT') {
        const body = await readJsonBody(req);
        const runtimeUpdates = {};
        for (const k of ['downloads_enabled', 'streaming_enabled', 'built_in_player_enabled', 'auto_player_redirect']) {
          if (typeof body[k] === 'boolean') runtimeUpdates[k] = body[k];
        }
        if (['all', 'admins', 'owners'].includes(body.access_mode)) {
          runtimeUpdates.access_mode = body.access_mode;
        }
        if (Object.keys(runtimeUpdates).length) {
          await db.collection('site_settings').updateOne({ _id: 'runtime' }, { $set: runtimeUpdates }, { upsert: true });
        }

        const now = new Date();
        let cfTarget = null;
        for (const key of ['base_url', 'backend_url', 'cloudflare_url']) {
          if (typeof body[key] === 'string' && body[key].trim()) {
            const normalized = validPublicUrl(body[key]);
            if (!normalized) {
              sendJson(res, 400, { detail: `${key} must be a valid public HTTP(S) URL` });
              return;
            }
            await db.collection('settings').updateOne(
              { key },
              { $set: { value: normalized, updated_at: now } },
              { upsert: true },
            );
            if (key === 'base_url') {
              if (!isTryCloudflareUrl(normalized)) {
                await db.collection('settings').updateOne(
                  { key: 'website_urls' },
                  { $addToSet: { value: normalized }, $set: { updated_at: now } },
                  { upsert: true },
                );
              }
              if (isTryCloudflareUrl(normalized)) {
                cfTarget = normalized;
                await db.collection('settings').updateOne({ key: 'backend_url' }, { $set: { value: normalized, updated_at: now } }, { upsert: true });
                await db.collection('settings').updateOne({ key: 'cloudflare_url' }, { $set: { value: normalized, updated_at: now } }, { upsert: true });
              }
            } else {
              cfTarget = normalized;
              await db.collection('settings').updateOne({ key: 'backend_url' }, { $set: { value: normalized, updated_at: now } }, { upsert: true });
              await db.collection('settings').updateOne({ key: 'cloudflare_url' }, { $set: { value: normalized, updated_at: now } }, { upsert: true });
            }
          }
        }

        if (cfTarget) {
          await rewriteAllStoredCatalogButtons(db, cfTarget);
        }

        const updatedState = await getSettingsState(db, req);
        const { _raw_map, ...publicState } = updatedState;
        sendJson(res, 200, publicState);
        return;
      }

      if (pathname === '/api/admin/categories' && req.method === 'GET') {
        const items = await db.collection('catalog_categories').find({}, { projection: { _id: 0 } }).sort({ name: 1 }).toArray();
        sendJson(res, 200, { items });
        return;
      }

      if (pathname === '/api/admin/categories' && req.method === 'POST') {
        const body = await readJsonBody(req);
        const name = String(body.name || '').trim();
        const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
        if (!slug) {
          sendJson(res, 400, { detail: 'Invalid category name' });
          return;
        }
        if (await db.collection('catalog_categories').findOne({ slug })) {
          sendJson(res, 409, { detail: 'Category already exists' });
          return;
        }
        await db.collection('catalog_categories').insertOne({ name, slug, created_at: new Date() });
        sendJson(res, 200, { name, slug });
        return;
      }

      if (pathname.startsWith('/api/admin/categories/') && req.method === 'DELETE') {
        const slug = decodeURIComponent(pathname.slice('/api/admin/categories/'.length));
        if (['movies', 'web-series'].includes(slug)) {
          sendJson(res, 400, { detail: 'Default categories cannot be deleted' });
          return;
        }
        await db.collection('catalog_categories').deleteOne({ slug });
        sendJson(res, 200, { ok: true });
        return;
      }

      if (pathname === '/api/admin/tmdb-search' && req.method === 'GET') {
        const q = (searchParams.get('q') || '').trim();
        const mediaType = searchParams.get('media_type') || 'multi';
        const tmdbKey = String(process.env.TMDB_API_KEY || state._raw_map?.tmdb_api_key || '').trim();
        if (!tmdbKey) {
          sendJson(res, 503, { detail: 'TMDB_API_KEY is not configured' });
          return;
        }
        const tmdbUrl = new URL(`https://api.themoviedb.org/3/search/${encodeURIComponent(mediaType)}`);
        tmdbUrl.searchParams.set('api_key', tmdbKey);
        tmdbUrl.searchParams.set('query', q);
        tmdbUrl.searchParams.set('include_adult', 'false');
        const r = await fetch(tmdbUrl.toString());
        if (!r.ok) {
          sendJson(res, 502, { detail: `TMDB search failed with HTTP ${r.status}` });
          return;
        }
        const payload = await r.json();
        const items = (payload.results || []).slice(0, 20).map((x) => ({
          id: x.id,
          media_type: x.media_type || (mediaType !== 'multi' ? mediaType : 'movie'),
          title: x.title || x.name,
          overview: x.overview || '',
          poster_path: x.poster_path || '',
          release_date: x.release_date || x.first_air_date || '',
          rating: x.vote_average,
        }));
        sendJson(res, 200, { items });
        return;
      }

      if (pathname === '/api/admin/pages' && req.method === 'GET') {
        const q = (searchParams.get('q') || '').trim().slice(0, 100);
        const query = q
          ? {
              $or: [
                { title: { $regex: q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), $options: 'i' } },
                { slug: { $regex: q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), $options: 'i' } },
              ],
            }
          : {};
        const rows = await db.collection('catalog_pages').find(query, { projection: { _id: 0 } }).sort({ created_at: -1 }).limit(300).toArray();
        sendJson(res, 200, { items: rows.map((r) => serializeCatalogDoc(r, cloudflareUrl)) });
        return;
      }

      if (pathname === '/api/admin/pages' && req.method === 'POST') {
        const body = await readJsonBody(req);
        const title = String(body.title || '').trim();
        if (!title) {
          sendJson(res, 400, { detail: 'Title is required' });
          return;
        }
        let slug = title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || crypto.randomBytes(5).toString('hex');
        const base = slug;
        let i = 2;
        while (await db.collection('catalog_pages').findOne({ slug })) {
          slug = `${base}-${i++}`;
        }
        const categoryIds = Array.isArray(body.category_ids) ? body.category_ids.map(String).filter(Boolean) : [];
        const groups = normalizeAndRewritePageGroups(Array.isArray(body.groups) ? body.groups : [], cloudflareUrl);
        const now = new Date();
        const doc = {
          slug,
          title,
          overview: String(body.overview || ''),
          poster_path: String(body.poster_path || ''),
          release_date: String(body.release_date || ''),
          rating: body.rating !== null && body.rating !== undefined && body.rating !== '' ? Number(body.rating) : null,
          media_type: body.media_type === 'tv' ? 'tv' : 'movie',
          category_ids: categoryIds,
          category_slugs: categoryIds.length ? categoryIds : [body.media_type === 'tv' ? 'web-series' : 'movies'],
          groups,
          tmdb_id: body.tmdb_id || null,
          imdb_id: String(body.imdb_id || ''),
          genres: Array.isArray(body.genres) ? body.genres : [],
          runtime: body.runtime || null,
          published: true,
          created_at: now,
          updated_at: now,
        };
        await db.collection('catalog_pages').insertOne(doc);
        const webBase = state.vercel_url || '';
        sendJson(res, 200, { slug, url: `${webBase}/title/${slug}` });
        return;
      }

      if (pathname.startsWith('/api/admin/pages/')) {
        const slug = decodeURIComponent(pathname.slice('/api/admin/pages/'.length));
        if (req.method === 'GET') {
          const page = await db.collection('catalog_pages').findOne({ slug }, { projection: { _id: 0 } });
          if (!page) sendJson(res, 404, { detail: 'Page not found' });
          else sendJson(res, 200, serializeCatalogDoc(page, cloudflareUrl));
          return;
        }
        if (req.method === 'PUT') {
          const body = await readJsonBody(req);
          const categoryIds = Array.isArray(body.category_ids) ? body.category_ids.map(String).filter(Boolean) : [];
          const groups = normalizeAndRewritePageGroups(Array.isArray(body.groups) ? body.groups : [], cloudflareUrl);
          const updateDoc = {
            title: String(body.title || '').trim(),
            overview: String(body.overview || ''),
            poster_path: String(body.poster_path || ''),
            release_date: String(body.release_date || ''),
            rating: body.rating !== null && body.rating !== undefined && body.rating !== '' ? Number(body.rating) : null,
            media_type: body.media_type === 'tv' ? 'tv' : 'movie',
            category_ids: categoryIds,
            category_slugs: categoryIds,
            groups,
            updated_at: new Date(),
          };
          const result = await db.collection('catalog_pages').updateOne({ slug }, { $set: updateDoc });
          if (!result.matchedCount) sendJson(res, 404, { detail: 'Page not found' });
          else sendJson(res, 200, { ok: true, groups });
          return;
        }
        if (req.method === 'DELETE') {
          const result = await db.collection('catalog_pages').deleteOne({ slug });
          if (!result.deletedCount) sendJson(res, 404, { detail: 'Page not found' });
          else sendJson(res, 200, { ok: true, deleted: slug });
          return;
        }
      }
    }

    if (req.method === 'GET' && pathname === '/') {
      const [rawItems, rawCats] = await Promise.all([
        db.collection('catalog_pages').find({ published: true }, { projection: { _id: 0 } }).sort({ created_at: -1 }).limit(100).toArray(),
        db.collection('catalog_categories').find({}, { projection: { _id: 0 } }).sort({ name: 1 }).toArray(),
      ]);
      const initialItems = rawItems.map((item) => serializeCatalogDoc(item, cloudflareUrl));
      const initialCategories = rawCats.length
        ? rawCats.map((c) => ({ name: c.name, slug: c.slug }))
        : [
            { name: 'Movies', slug: 'movies' },
            { name: 'Web Series', slug: 'web-series' },
          ];
      sendHtml(res, 200, renderIndexHtml(initialItems, initialCategories, searchParams.get('q') || ''));
      return;
    }

    if (req.method === 'GET' && pathname === '/search') {
      const q = (searchParams.get('q') || '').trim().slice(0, 100);
      const rawType = (searchParams.get('type') || '').trim();
      const typeFilter = ['movie', 'tv'].includes(rawType) ? rawType : '';
      const yearFilter = (searchParams.get('year') || '').trim().slice(0, 4);
      const genreFilter = (searchParams.get('genre') || '').trim().slice(0, 40);
      const categoryFilter = (searchParams.get('category') || '').trim().slice(0, 40);

      const query = { published: true };
      if (typeFilter === 'tv') query.media_type = { $in: ['tv', 'series', 'web-series'] };
      else if (typeFilter === 'movie') query.media_type = 'movie';
      if (categoryFilter) query.category_slugs = categoryFilter;
      if (/^\d{4}$/.test(yearFilter)) query.release_date = { $regex: `^${yearFilter}` };
      if (genreFilter) query.genres = { $regex: genreFilter.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), $options: 'i' };
      if (q) {
        const safe = q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        query.$or = [
          { title: { $regex: safe, $options: 'i' } },
          { overview: { $regex: safe, $options: 'i' } },
          { genres: { $regex: safe, $options: 'i' } },
        ];
      }

      const [rawRows, cats, genreDocs] = await Promise.all([
        db.collection('catalog_pages').find(query, { projection: { _id: 0 } }).sort({ created_at: -1 }).limit(100).toArray(),
        db.collection('catalog_categories').find({}, { projection: { _id: 0 } }).sort({ name: 1 }).toArray(),
        db.collection('catalog_pages').find({ published: true, genres: { $exists: true } }, { projection: { genres: 1 } }).toArray(),
      ]);
      const allGenres = new Set();
      for (const d of genreDocs) {
        for (const g of d.genres || []) {
          if (typeof g === 'string' && g.trim()) allGenres.add(g.trim());
        }
      }
      sendHtml(
        res,
        200,
        renderSearchHtml({
          results: rawRows.map((r) => serializeCatalogDoc(r, cloudflareUrl)),
          query: q,
          typeFilter,
          yearFilter,
          genreFilter,
          categoryFilter,
          categories: cats,
          genres: [...allGenres].sort(),
        }),
      );
      return;
    }

    if (req.method === 'GET' && pathname.startsWith('/title/')) {
      const slug = decodeURIComponent(pathname.slice('/title/'.length));
      const rawPage = await db.collection('catalog_pages').findOne({ slug, published: true }, { projection: { _id: 0 } });
      if (!rawPage) {
        sendHtml(
          res,
          404,
          renderBaseLayout({
            title: 'Title Not Found · HITLER YT',
            content: '<p class="muted" style="padding:48px 0;text-align:center">Title not found. <a href="/" style="color:var(--yellow)">Back to Home</a></p>',
          }),
        );
        return;
      }
      const page = serializeCatalogDoc(rawPage, cloudflareUrl);
      sendHtml(res, 200, renderTitleHtml(page, cloudflareUrl));
      return;
    }

    if (req.method === 'GET' && (pathname === '/panel' || pathname === '/admin/panel')) {
      if (!(await verifyAdminAuth(req, db))) {
        sendUnauthorized(res);
        return;
      }
      sendHtml(res, 200, renderPanelHtml(state));
      return;
    }

    const requestPath = pathname + search;
    if (
      pathname.startsWith('/stream/') ||
      pathname.startsWith('/dl/') ||
      pathname.startsWith('/remux/') ||
      pathname.startsWith('/api/tracks/')
    ) {
      if (!cloudflareUrl) {
        sendJson(res, 503, {
          error: 'Cloudflare tunnel URL is not configured yet',
          message: 'Wait a few seconds for the VPS Cloudflare Quick Tunnel to auto-sync to MongoDB, or set it via /baseurl.',
        });
        return;
      }
      res.writeHead(307, { Location: `${cloudflareUrl}${requestPath}` });
      res.end();
      return;
    }

    if ((req.method === 'GET' || req.method === 'HEAD') && pathname.startsWith('/watch/')) {
      const shortCode = decodeURIComponent(pathname.slice('/watch/'.length));
      if (cloudflareUrl && !isVercelUrl(cloudflareUrl)) {
        res.writeHead(307, { Location: `${cloudflareUrl}${requestPath}` });
        res.end();
        return;
      }
      const fileData = await db.collection('files').findOne({ short_code: shortCode });
      if (!fileData) {
        sendJson(res, 404, { detail: 'Link not found' });
        return;
      }
      sendHtml(res, 200, renderWatchHtml(fileData, state, cloudflareUrl));
      return;
    }

    if (!cloudflareUrl || isVercelUrl(cloudflareUrl)) {
      sendJson(res, 404, { error: 'Not found', path: pathname });
      return;
    }

    const targetUrl = new URL(`${cloudflareUrl}${requestPath}`);
    const transport = targetUrl.protocol === 'https:' ? https : http;
    const headers = {
      accept: req.headers.accept || '*/*',
      'user-agent': req.headers['user-agent'] || 'HITLER-YT-Vercel-Gateway',
      host: targetUrl.host,
      'x-forwarded-host': req.headers.host || '',
      'x-forwarded-proto': 'https',
    };
    if (req.headers.authorization) headers.authorization = req.headers.authorization;
    if (req.headers['content-type']) headers['content-type'] = req.headers['content-type'];
    if (req.headers.range) headers.range = req.headers.range;

    const clientReq = transport.request(targetUrl, { method: req.method, headers }, (backendRes) => {
      const responseHeaders = { ...backendRes.headers };
      responseHeaders['cache-control'] = 'no-store, no-cache, must-revalidate, max-age=0';
      res.writeHead(backendRes.statusCode || 502, responseHeaders);
      backendRes.pipe(res);
    });
    clientReq.setTimeout(25000, () => clientReq.destroy(new Error('Backend request timed out')));
    clientReq.on('error', (error) => {
      if (!res.headersSent) {
        sendJson(res, 502, {
          error: 'Bad Gateway',
          message: 'Could not connect to the Cloudflare tunnel backend.',
          detail: String(error?.message || error),
        });
      } else {
        res.destroy(error);
      }
    });
    if (['POST', 'PUT', 'PATCH'].includes(req.method)) req.pipe(clientReq);
    else clientReq.end();
  } catch (fatalError) {
    console.error('Unhandled error in Vercel handler:', fatalError);
    if (!res.headersSent) {
      sendJson(res, 503, {
        error: 'Service temporarily unavailable',
        message: String(fatalError?.message || 'Unexpected error in Vercel handler'),
      });
    }
  }
}
