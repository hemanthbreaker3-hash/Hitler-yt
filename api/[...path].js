import crypto from 'node:crypto';
import http from 'node:http';
import https from 'node:https';
import { MongoClient } from 'mongodb';

// Disable request parsing so range requests and streamed media bodies pass through untouched.
// Disable the platform response-size warning for streamed video responses.
export const config = { api: { bodyParser: false, responseLimit: false }, maxDuration: 300 };

const API_SUBPATHS = new Set([
  'catalog',
  'categories',
  'site-settings',
  'public',
  'health',
  'admin',
  'tracks',
  'views', // Playback view registration endpoint on the Python backend.
]);

let mongoClientPromise = null;
let resolvedDbName = null;

function getMongoUri() {
  return String(
    process.env.MONGODB_URI ||
    process.env.MONGO_URL ||
    process.env.MONGO_URI ||
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
  if (!uri) throw new Error('MONGODB_URI is not configured');
  if (process.env.CF_PAGES === '1') {
    const client = new MongoClient(uri, {
      serverSelectionTimeoutMS: 8000,
      connectTimeoutMS: 8000,
      maxPoolSize: 4,
    });
    return client.connect();
  }
  if (!mongoClientPromise) {
    const client = new MongoClient(uri, {
      serverSelectionTimeoutMS: 8000,
      connectTimeoutMS: 8000,
      maxPoolSize: 25, minPoolSize: 2, maxIdleTimeMS: 30000,
    });
    mongoClientPromise = client.connect().catch((error) => {
      mongoClientPromise = null;
      throw error;
    });
  }
  return mongoClientPromise;
}

function dbCandidateNames() {
  const envName = String(process.env.DATABASE_NAME || process.env.MONGODB_DB_NAME || process.env.DB_NAME || process.env.MONGO_DB_NAME || '').trim();
  const uriName = extractDbNameFromUri(getMongoUri());
  return [...new Set([envName, uriName, 'tg_media_bot', 'file2link'].filter(Boolean))];
}

async function getDatabase() {
  const client = await getMongoClient();
  if (resolvedDbName) return client.db(resolvedDbName);

  const candidates = dbCandidateNames();
  const primary = candidates[0] || 'tg_media_bot';

  try {
    // Prefer a DB that already has shortener_config or catalog data from the bot.
    let best = null;
    let bestScore = -1;
    for (const candidate of candidates) {
      const db = client.db(candidate);
      const [pages, settings, files, users, shortener] = await Promise.all([
        db.collection('catalog_pages').estimatedDocumentCount().catch(() => 0),
        db.collection('site_settings').estimatedDocumentCount().catch(() => 0),
        db.collection('files').estimatedDocumentCount().catch(() => 0),
        db.collection('users').estimatedDocumentCount().catch(() => 0),
        db.collection('site_settings').findOne({ _id: 'shortener_config' }).catch(() => null),
      ]);
      const score =
        (pages > 0 ? 8 : 0) +
        (settings > 0 ? 4 : 0) +
        (files > 0 ? 2 : 0) +
        (users > 0 ? 1 : 0) +
        (shortener && shortener.enabled ? 16 : shortener ? 6 : 0);
      if (score > bestScore) {
        bestScore = score;
        best = candidate;
      }
    }
    if (best && bestScore > 0) {
      resolvedDbName = best;
      return client.db(best);
    }
  } catch {}

  resolvedDbName = primary;
  return client.db(primary);
}

function isShortenerEnabled(cfg) {
  if (!cfg || typeof cfg !== 'object') return false;
  const v = cfg.enabled;
  return v === true || v === 1 || v === '1' || String(v).toLowerCase() === 'true';
}

/** Load shortener_config from the active DB, then fall back across candidate DBs. */
async function loadShortenerConfig(db) {
  try {
    const local = await db.collection('site_settings').findOne({ _id: 'shortener_config' });
    if (local && isShortenerEnabled(local)) return local;
    if (local) {
      // Keep disabled local config if nothing better is found later
    }
    const client = await getMongoClient();
    for (const name of dbCandidateNames()) {
      try {
        if (resolvedDbName && name === resolvedDbName && local) continue;
        const alt = await client.db(name).collection('site_settings').findOne({ _id: 'shortener_config' });
        if (alt && isShortenerEnabled(alt)) return alt;
        if (!local && alt) return alt;
      } catch {}
    }
    return local || { enabled: false, shorteners: [], expiry_seconds: 300 };
  } catch {
    return { enabled: false, shorteners: [], expiry_seconds: 300 };
  }
}

function sendJson(res, status, payload) {
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0');
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.end(JSON.stringify(payload));
}

function sendHtml(res, status, html, cacheControl = 'no-store, no-cache, must-revalidate, max-age=0') {
  res.statusCode = status;
  res.setHeader('Content-Type', 'text/html; charset=utf-8');
  res.setHeader('Cache-Control', cacheControl);
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
    return host === 'ngrok-free.app' || host.endsWith('.ngrok-free.app') ||
    host === 'ngrok-free.dev' || host.endsWith('.ngrok-free.dev') ||
    host === 'ngrok.io' || host.endsWith('.ngrok.io') ||
    host === 'trycloudflare.com' || host.endsWith('.trycloudflare.com');
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
    if (forceReplaceDomain || isMediaPath || host.endsWith('.trycloudflare.com') || host.endsWith('.ngrok-free.app') || host.endsWith('.ngrok-free.dev') || host.endsWith('.ngrok.io') || host.endsWith('.vercel.app')) {
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
  let wbLabel = '';
  for (const g of copy.groups) {
    if (!g || typeof g !== 'object') continue;
    const box = String(g.box || 'watch').trim().toLowerCase();
    if (box && box !== 'watch') continue;
    const btns = (Array.isArray(g.buttons) ? g.buttons : [])
      .map(b => String((b && (b.name || b.text)) || '').trim())
      .filter(Boolean);
    if (btns.length) {
      wbLabel = btns.join(' . ');
      break;
    }
  }
  copy.watch_buttons_label = wbLabel;
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
      { key: { $in: ['base_url', 'backend_url', 'ngrok_url', 'cloudflare_url', 'vercel_url', 'website_urls', 'tmdb_api_key', 'admin_panel_token', 'panel_username', 'panel_password_salt', 'panel_password_hash'] } },
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
    map.ngrok_url,
    map.ngrok_url,
    map.cloudflare_url,
    map.backend_url,
    map.base_url,
    process.env.BACKEND_URL,
    process.env.NGROK_URL,
    process.env.NGROK_URL,
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

  const fsUser = String(runtime.filestore_bot_username || '').trim().replace(/^@+/, '');
  return {
    downloads_enabled: runtime.downloads_enabled !== false,
    streaming_enabled: runtime.streaming_enabled !== false,
    built_in_player_enabled: runtime.built_in_player_enabled !== false,
    auto_player_redirect: runtime.auto_player_redirect === true,
    access_mode: runtime.access_mode || 'all',
    filestore_bot_username: fsUser,
    base_url: cloudflareUrl || validPublicUrl(map.base_url) || vercelUrl || '',
    backend_url: cloudflareUrl || validPublicUrl(map.backend_url) || '',
    ngrok_url: cloudflareUrl,
    ngrok_url: cloudflareUrl,
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


/** Fill missing landscape backdrop_path from TMDB for existing catalog pages (throttled). */
let _backdropBackfillAt = 0;
async function ensureTmdbBackdrops(db, tmdbKeyOverride) {
  const now = Date.now();
  if (now - _backdropBackfillAt < 10 * 60 * 1000) return; // at most every 10 minutes
  _backdropBackfillAt = now;
  let tmdbKey = String(tmdbKeyOverride || process.env.TMDB_API_KEY || '').trim();
  if (!tmdbKey) {
    try {
      const doc = await db.collection('settings').findOne({ key: 'tmdb_api_key' });
      tmdbKey = String((doc && doc.value) || '').trim();
    } catch (_) {}
  }
  if (!tmdbKey) return;
  try {
    const missing = await db.collection('catalog_pages').find(
      {
        $or: [
          { backdrop_path: { $exists: false } },
          { backdrop_path: null },
          { backdrop_path: '' },
        ],
      },
      { projection: { slug: 1, tmdb_id: 1, media_type: 1, title: 1, poster_path: 1 } }
    ).limit(60).toArray();
    if (!missing.length) return;
    for (const row of missing) {
      let tid = row.tmdb_id;
      const slug = row.slug || '';
      const rawTitle = String(row.title || '').trim();
      const mt = String(row.media_type || 'movie').toLowerCase();
      let mediaType = (mt === 'tv' || mt === 'series' || mt === 'web-series') ? 'tv' : 'movie';
      try {
        let data = null;
        if (tid) {
          let r = await fetch(
            'https://api.themoviedb.org/3/' + mediaType + '/' + tid + '?api_key=' + encodeURIComponent(tmdbKey) + '&language=en-US',
            { signal: AbortSignal.timeout(8000) }
          );
          if (!r.ok) {
            mediaType = mediaType === 'tv' ? 'movie' : 'tv';
            r = await fetch(
              'https://api.themoviedb.org/3/' + mediaType + '/' + tid + '?api_key=' + encodeURIComponent(tmdbKey) + '&language=en-US',
              { signal: AbortSignal.timeout(8000) }
            );
          }
          if (r.ok) data = await r.json();
        } else if (rawTitle) {
          const cleanTitle = rawTitle.replace(/\s*[\(\[](?:19|20)\d{2}[\)\]]/g, '').trim();
          const sr = await fetch(
            'https://api.themoviedb.org/3/search/multi?api_key=' + encodeURIComponent(tmdbKey) + '&query=' + encodeURIComponent(cleanTitle || rawTitle) + '&language=en-US',
            { signal: AbortSignal.timeout(8000) }
          );
          if (sr.ok) {
            const sj = await sr.json();
            const results = (Array.isArray(sj.results) ? sj.results : []).filter(x => x && (x.media_type === 'movie' || x.media_type === 'tv'));
            const hit = results.find(x => x.backdrop_path) || results[0];
            if (hit) { data = hit; tid = hit.id; }
          }
        }
        if (!data) continue;
        let backdrop = data.backdrop_path || '';
        try {
          const mtImg = (data.media_type === 'tv' || mediaType === 'tv') ? 'tv' : 'movie';
          const ir = await fetch('https://api.themoviedb.org/3/' + mtImg + '/' + tid + '/images?api_key=' + encodeURIComponent(tmdbKey) + '&include_image_language=en,hi,ja,ko,te,ta,null', { signal: AbortSignal.timeout(5000) });
          if (ir.ok) {
            const ij = await ir.json();
            const bds = Array.isArray(ij.backdrops) ? ij.backdrops : [];
            const enBd = bds.find(x => x && x.file_path && x.iso_639_1 === 'en') || bds.find(x => x && x.file_path && x.iso_639_1);
            if (enBd && enBd.file_path) backdrop = enBd.file_path;
          }
        } catch (_) {}
        const poster = data.poster_path || '';
        if (!backdrop && !poster) continue;
        const patch = { updated_at: new Date() };
        if (backdrop) patch.backdrop_path = backdrop;
        if (tid && !row.tmdb_id) patch.tmdb_id = Number(tid) || tid;
        if (poster && !row.poster_path) patch.poster_path = poster;
        if (slug) {
          await db.collection('catalog_pages').updateOne({ slug }, { $set: patch }).catch(() => {});
        } else if (tid) {
          const tidNum = Number(tid);
          const tidQuery = Number.isFinite(tidNum)
            ? { $or: [{ tmdb_id: tidNum }, { tmdb_id: String(tidNum) }, { tmdb_id: tid }] }
            : { tmdb_id: tid };
          await db.collection('catalog_pages').updateMany(tidQuery, { $set: patch }).catch(() => {});
        }
      } catch (_) {}
    }
  } catch (_) {}
}

async function ensureDefaultCategories(db) {
  try {
    const col = db.collection('catalog_categories');
    const count = await col.estimatedDocumentCount().catch(() => 0);
    if (count === 0) {
      const now = new Date();
      await col.updateOne({ slug: 'movies' }, { $setOnInsert: { name: 'Movies', slug: 'movies', created_at: now } }, { upsert: true });
      await col.updateOne({ slug: 'series' }, { $setOnInsert: { name: 'Series', slug: 'series', created_at: now } }, { upsert: true });
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
  res.setHeader('WWW-Authenticate', 'Basic realm="HITLER YT-Panel"');
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.end(JSON.stringify({ detail: 'Invalid panel username or password' }));
}

function renderBaseLayout({ title = 'HITLER YT', headExtra = '', content = '' }) {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="theme-color" content="#07090e">
  <meta name="description" content="HITLER YT — discover anime, movies and series.">
  <meta name="color-scheme" content="dark">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700;800&family=Outfit:wght@500;600;700;800;900&display=swap" rel="stylesheet">
  <title>${escapeHtml(title)}</title>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
  <style>
    :root{
      color-scheme:dark;
      --bg:#07090e;--bg-elevated:#0e121b;--bg-card:#121722;--bg-card-hover:#181f2e;
      --line:rgba(255,255,255,.08);--line-strong:rgba(255,255,255,.16);
      --text:#f5f6fa;--muted:#9aa3b5;--accent:#8b5cf6;--accent-2:#22d3ee;
      --green:#22c55e;--red:#ef4444;--accent-soft:rgba(139,92,246,.16);--cyan:#22d3ee;--prime:#22c55e;--danger:#fb7185;
      --radius:16px;--radius-sm:10px;--shadow:0 20px 50px rgba(0,0,0,.48);
      --font:"DM Sans",system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      --font-display:"Outfit","DM Sans",system-ui,sans-serif;--max:1340px;--pad:4vw;
    }
    *{box-sizing:border-box;margin:0;padding:0}
    html{scroll-behavior:smooth}
    body{margin:0;min-height:100vh;color:var(--text);font:500 15px/1.55 var(--font);
      background:radial-gradient(ellipse 70% 40% at 12% -10%,rgba(139,92,246,.14),transparent 58%),
        radial-gradient(ellipse 55% 35% at 92% 0%,rgba(34,211,238,.09),transparent 55%),var(--bg);
      -webkit-font-smoothing:antialiased}
    h1,h2,h3,h4,.brand-word{font-family:var(--font-display);font-weight:800;letter-spacing:-.02em}
    a{color:inherit;text-decoration:none} button,input,select{font:inherit} img{max-width:100%;display:block}
    .wrap{width:min(100% - 28px,var(--max));margin-inline:auto}
    .topbar{position:sticky;top:0;z-index:80;backdrop-filter:blur(20px) saturate(1.3);-webkit-backdrop-filter:blur(20px) saturate(1.3);
      background:rgba(7,9,14,.84);border-bottom:1px solid var(--line)}
    .topbar-inner{width:min(100% - 28px,var(--max));margin:auto;min-height:68px;display:flex;flex-wrap:wrap;
      align-items:center;justify-content:space-between;gap:12px;padding:8px 0}
    .topbar-logo{display:inline-flex;align-items:center;gap:10px;transition:transform .2s ease}
    .topbar-logo:hover{transform:translateY(-1px)}
    .topbar-logo img{display:none!important}
    .brand-lockup{display:inline-flex;align-items:center;gap:10px;max-width:6cm;min-height:1.1cm;max-height:2cm}
    .brand-mark{position:relative;width:32px;height:40px;display:grid;place-items:center;flex:0 0 auto}
    .brand-mark::before{content:"";position:absolute;inset:3px 10px;border-left:2px solid var(--accent);border-right:2px solid var(--accent-2);transform:skewY(-12deg);opacity:.9}
    .brand-mark span{position:relative;font-family:var(--font-display);font-weight:900;font-size:10px;letter-spacing:.16em;
      writing-mode:vertical-rl;text-orientation:mixed;transform:rotate(180deg);
      background:linear-gradient(180deg,var(--accent-2),var(--accent));-webkit-background-clip:text;background-clip:text;color:transparent}
    .brand-word{font-family:var(--font-display);font-size:clamp(.9rem,2.1vw,1.25rem);line-height:1.1;letter-spacing:.20em;font-weight:800;text-transform:uppercase;
      background:linear-gradient(100deg,#fff 8%,#dcd2ff 50%,#22d3ee 92%);-webkit-background-clip:text;background-clip:text;color:transparent;white-space:nowrap}
    .topbar-links{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
    .topbar-links a{padding:8px 15px;border-radius:999px;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);
      border:1px solid transparent;transition:color .18s,background .18s,border-color .18s,transform .18s}
    .topbar-links a:hover{color:var(--text);background:rgba(255,255,255,.06);border-color:var(--line)}
    .search-bar{display:flex;align-items:center;gap:12px;background:linear-gradient(160deg,rgba(19,25,38,.94),rgba(12,16,25,.94));
      border:1px solid var(--line-strong);border-radius:18px;padding:6px 8px 6px 18px;margin:18px 0 16px;
      box-shadow:0 12px 32px rgba(0,0,0,.32);transition:border-color .2s,box-shadow .2s}
    .search-bar:focus-within{border-color:rgba(34,211,238,.55);box-shadow:0 0 0 4px rgba(34,211,238,.12),0 12px 32px rgba(0,0,0,.36)}
    .search-bar i{color:var(--accent-2);font-size:16px}
    .search-bar input{flex:1;min-width:0;background:transparent;border:0;outline:0;color:var(--text);font-size:15px;padding:11px 0;font-weight:500}
    .search-bar input::placeholder{color:#6d768a}
    .search-bar button{border:0;border-radius:13px;cursor:pointer;padding:10px 18px;font-weight:800;font-size:13px;
      background:linear-gradient(125deg,var(--accent),var(--accent-2));color:#061019;transition:filter .15s,transform .15s}
    .search-bar button:hover{filter:brightness(1.08);transform:translateY(-1px)}
    .section-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:22px 0 14px}
    .section-head h2{font-size:clamp(1.1rem,2.2vw,1.38rem);display:flex;align-items:center;gap:10px;font-weight:900}
    .section-head h2::before{content:"";width:4px;height:1.05em;border-radius:4px;background:linear-gradient(180deg,var(--accent),var(--accent-2))}
    .chip,.cat-nav .chip,select.chip{display:inline-flex;align-items:center;justify-content:center;gap:6px;background:var(--bg-card);border:1px solid var(--line);
      border-radius:999px;padding:9px 16px;font-size:12px;font-weight:700;color:var(--muted);cursor:pointer;
      transition:border-color .15s,color .15s,background .15s,transform .15s}
    .chip:hover,select.chip:hover{color:var(--text);border-color:var(--line-strong);transform:translateY(-1px)}
    .chip.active,.cat-nav .chip.active{color:#061019;border-color:transparent;background:linear-gradient(125deg,var(--accent),var(--accent-2));box-shadow:0 6px 20px rgba(34,211,238,.2)}
    .cat-nav{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 18px}
    .poster-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:8px}
    @media(min-width:680px){.poster-grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}}
    @media(min-width:1080px){.poster-grid{grid-template-columns:repeat(4,minmax(0,1fr));gap:18px}}
    .poster-card{position:relative;overflow:hidden;border-radius:14px;background:var(--bg-card);border:1px solid var(--line);
      box-shadow:0 10px 26px rgba(0,0,0,.32);transition:transform .22s cubic-bezier(.2,.8,.2,1),border-color .22s,box-shadow .22s;display:flex;flex-direction:column}
    .poster-card:hover{transform:translateY(-4px);border-color:rgba(34,211,238,.45);box-shadow:0 18px 38px rgba(0,0,0,.48),0 0 22px rgba(139,92,246,.16)}
    .poster-card .card-media{position:relative;overflow:hidden;aspect-ratio:16/9;background:#0e131e}
    .poster-card img{width:100%;height:100%;aspect-ratio:16/9;object-fit:cover;background:#0e131e;transition:transform .35s ease}
    .poster-card:hover img{transform:scale(1.05)}
    .poster-card .play-hint{position:absolute;inset:0;display:grid;place-items:center;background:linear-gradient(180deg,transparent 35%,rgba(7,9,14,.55));opacity:0;transition:opacity .22s;z-index:1}
    .poster-card:hover .play-hint{opacity:1}
    .poster-card .play-hint span{width:44px;height:44px;border-radius:50%;background:linear-gradient(135deg,var(--accent),var(--accent-2));display:grid;place-items:center;color:#061019;font-size:15px;box-shadow:0 8px 22px rgba(0,0,0,.45)}
    .poster-card .badge-row{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
    .poster-card .badge{font-size:10px;font-weight:800;padding:3px 8px;border-radius:6px;letter-spacing:.03em}
    .poster-card .badge-genre{background:linear-gradient(130deg,var(--accent),var(--accent-2));color:#061019}
    .poster-card .badge-rating{background:rgba(34,197,94,.16);color:#4ade80;border:1px solid rgba(34,197,94,.35)}
    .poster-card .info{padding:11px 12px 13px;display:flex;flex-direction:column;gap:3px;flex:1}
    .poster-card .title{font-family:var(--font-display);font-weight:700;font-size:clamp(13px,2.2vw,15px);line-height:1.28;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
    .poster-card .meta{font-size:12px;color:var(--muted);line-height:1.3}
    main{padding:8px 0 88px}
    .footer{text-align:center;color:#6f788c;font-size:12px;padding:24px 0 44px;border-top:1px solid var(--line);margin-top:32px}
    .footer strong{color:var(--muted);font-family:var(--font-display)}
    .muted{color:var(--muted)}
    .btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:42px;padding:0 16px;border-radius:11px;
      border:1px solid var(--line-strong);background:var(--bg-card);color:var(--text);cursor:pointer;font-weight:700;font-size:13px}
    .btn:hover{border-color:rgba(34,211,238,.5);transform:translateY(-1px)}
    .btn-yellow,.btn.accent{background:linear-gradient(125deg,var(--accent),var(--accent-2));color:#061019;border:0;font-weight:800;border-radius:11px;padding:10px 16px}
    .card{background:linear-gradient(165deg,rgba(19,24,36,.95),rgba(11,15,23,.95));border:1px solid var(--line);border-radius:var(--radius);box-shadow:0 14px 36px rgba(0,0,0,.3)}
    .related-pager{display:flex;align-items:center;justify-content:center;gap:8px;margin:22px 0;flex-wrap:wrap}
    .related-pager[hidden]{display:none!important}
    .related-pager button{background:var(--bg-card);border:1px solid var(--line-strong);color:var(--text);border-radius:10px;padding:8px 13px;font:700 13px var(--font);cursor:pointer}
    .related-pager button:disabled{opacity:.4;cursor:not-allowed}
    .related-pager button.active{background:linear-gradient(125deg,var(--accent),var(--accent-2));color:#061019;border-color:transparent}
    @media(max-width:600px){
      .wrap,.topbar-inner{width:min(100% - 20px,var(--max))}
      .topbar-links a{padding:7px 11px;font-size:11px}.brand-word{font-size:.82rem;letter-spacing:.14em}.brand-mark{width:24px;height:32px}
    }
    #bg-portrait-wrap{position:fixed;inset:0;z-index:-2;pointer-events:none;overflow:hidden;background:#07090e}
    .bg-portrait-layer{position:absolute;inset:0;background-size:cover;background-position:center 18%;opacity:0;transition:opacity 1.2s ease;filter:blur(2px) saturate(1.18);transform:scale(1.03)}
    .bg-portrait-layer.active{opacity:.24}
    #bg-portrait-scrim{position:fixed;inset:0;z-index:-1;pointer-events:none;background:radial-gradient(ellipse 75% 45% at 15% 0%,rgba(139,92,246,.22),transparent 60%),radial-gradient(ellipse 65% 40% at 88% 10%,rgba(59,130,246,.18),transparent 58%),radial-gradient(ellipse 60% 40% at 50% 100%,rgba(239,68,68,.12),transparent 60%),linear-gradient(180deg,rgba(7,9,14,.76),rgba(7,9,14,.88) 55%,rgba(7,9,14,.96))}

    @media(max-width:600px){
      .topbar-inner{gap:8px!important;padding:8px 0!important}
      .topbar-links{width:100%;justify-content:flex-start;overflow-x:auto;flex-wrap:nowrap;scrollbar-width:none;-webkit-overflow-scrolling:touch;padding-bottom:2px}
      .topbar-links::-webkit-scrollbar{display:none}
      .topbar-links a{flex:0 0 auto;white-space:nowrap}
    }


    /* Centered CineXPro-style header with project branding */
    .topbar-inner{flex-direction:column!important;justify-content:center!important;align-items:center!important;gap:8px!important;padding:12px 0 10px!important;text-align:center}
    .topbar-logo{justify-content:center!important}
    .brand-lockup{flex-direction:column!important;align-items:center!important;gap:4px!important;max-width:none!important;max-height:none!important}
    .brand-mark{display:none!important}
    .brand-word{font-size:clamp(1.35rem,4.5vw,1.9rem)!important;letter-spacing:.14em!important;line-height:1.05!important}
    .brand-sub{font-size:10px;font-weight:700;letter-spacing:.26em;text-transform:uppercase;color:var(--muted);opacity:.88}
    .topbar-links{justify-content:center!important;width:auto!important}

    /* Reference-matched 16:9 landscape card with overlaid badges & clean title/year below */
    .cat-nav{justify-content:center!important;gap:10px!important;margin:0 auto 20px!important;max-width:680px}
    .cat-nav .chip{padding:10px 20px!important;font-size:13px!important;background:#131722!important;border:1px solid rgba(255,255,255,.12)!important}
    .cat-nav .chip.active{background:linear-gradient(125deg,#3b82f6,#8b5cf6)!important;color:#fff!important;box-shadow:0 6px 22px rgba(139,92,246,.38)!important}
    .poster-card{background:transparent!important;border:0!important;box-shadow:none!important;border-radius:0!important}
    .poster-card:hover{transform:translateY(-3px)!important;box-shadow:none!important}
    .poster-card .card-media{border-radius:14px!important;border:1px solid rgba(255,255,255,.12)!important;box-shadow:0 10px 24px rgba(0,0,0,.42)!important}
    .poster-card:hover .card-media{border-color:rgba(139,92,246,.6)!important;box-shadow:0 14px 30px rgba(59,130,246,.22)!important}
    .poster-card .card-badges{position:absolute;left:8px;right:8px;bottom:8px;z-index:3;display:flex;align-items:center;gap:6px;flex-wrap:wrap;pointer-events:none}
    .poster-card .badge-quality{background:rgba(7,9,14,.84);color:#f8fafc;border:1px solid rgba(255,255,255,.24);border-radius:10px;padding:3px 8px;font-size:clamp(8.5px,2vw,10px);font-weight:800;letter-spacing:.03em;line-height:1.25;white-space:normal;word-break:break-word;max-width:100%;backdrop-filter:blur(6px)}
    .poster-card .badge-genre{background:linear-gradient(125deg,#3b82f6,#8b5cf6);color:#fff;border-radius:999px;padding:3px 10px;font-size:10px;font-weight:800;letter-spacing:.04em;box-shadow:0 4px 12px rgba(59,130,246,.32)}
    .poster-card .info{padding:8px 2px 2px!important;background:transparent!important}
    .poster-card .title{font-size:14px!important;font-weight:700!important;color:#f8fafc!important;margin-bottom:2px!important}
    .poster-card .meta{font-size:12px!important;color:#94a3b8!important}

    /* Clean, larger responsive catalog cards across all devices */
    .poster-grid{
      display:grid!important;
      grid-template-columns:repeat(2,minmax(0,1fr))!important;
      gap:12px!important;
      margin-top:10px!important;
    }
    @media(min-width:600px){.poster-grid{grid-template-columns:repeat(2,minmax(0,1fr))!important;gap:18px!important}}
    @media(min-width:1024px){.poster-grid{grid-template-columns:repeat(2,minmax(0,1fr))!important;gap:22px!important}}
    @media(max-width:599px){
      .poster-card .card-top-badges{top:7px;left:7px;right:7px;gap:4px}
      .poster-card .badge-quality,.poster-card .badge-rating{padding:3px 6px;font-size:9.5px}
      .poster-card .info{padding:10px 10px 11px!important;gap:6px!important}
      .poster-card .title{font-size:13px!important}
      .poster-card .meta{font-size:11px!important}
      .poster-card .badge-genre{padding:2px 7px;font-size:9.5px}
    }
    .poster-card{
      position:relative!important;overflow:hidden!important;border-radius:16px!important;
      background:linear-gradient(165deg,rgba(18,24,38,.96),rgba(10,14,24,.98))!important;
      border:1px solid rgba(255,255,255,.1)!important;
      box-shadow:0 12px 30px rgba(0,0,0,.42)!important;
      display:flex!important;flex-direction:column!important;
      transition:transform .22s cubic-bezier(.22,1,.36,1),border-color .22s,box-shadow .22s!important;
    }
    .poster-card:hover{
      transform:translateY(-4px)!important;
      border-color:rgba(34,211,238,.5)!important;
      box-shadow:0 20px 42px rgba(0,0,0,.55),0 0 24px rgba(139,92,246,.18)!important;
    }
    .poster-card .card-media{
      position:relative!important;overflow:hidden!important;aspect-ratio:16/9!important;
      background:#0e131e!important;border:0!important;border-radius:0!important;box-shadow:none!important;
    }
    .poster-card .card-media::after{
      content:"";position:absolute;inset:0;pointer-events:none;z-index:1;
      background:linear-gradient(180deg,rgba(7,9,14,.32) 0%,transparent 34%,transparent 62%,rgba(10,14,24,.72) 100%);
    }
    .poster-card img{width:100%;height:100%;aspect-ratio:16/9;object-fit:cover;background:#0e131e;transition:transform .35s ease}
    .poster-card:hover img{transform:scale(1.05)}
    .poster-card .card-top-badges{
      position:absolute;top:10px;left:10px;right:10px;z-index:3;
      display:flex;align-items:center;justify-content:space-between;gap:8px;pointer-events:none;
    }
    .poster-card .badge-quality{
      background:rgba(7,9,14,.84);color:#f8fafc;border:1px solid rgba(255,255,255,.22);
      border-radius:8px;padding:4px 9px;font-size:11px;font-weight:800;letter-spacing:.05em;
      backdrop-filter:blur(8px);white-space:nowrap;
    }
    .poster-card .badge-rating{
      background:rgba(7,9,14,.84);color:#fbbf24;border:1px solid rgba(251,191,36,.38);
      border-radius:8px;padding:4px 9px;font-size:11px;font-weight:800;letter-spacing:.02em;
      backdrop-filter:blur(8px);white-space:nowrap;margin-left:auto;
    }
    .poster-card .info{
      padding:13px 15px 15px!important;display:flex!important;flex-direction:column!important;gap:8px!important;flex:1!important;background:transparent!important;
    }
    .poster-card .title{
      font-family:var(--font-display)!important;font-weight:700!important;
      font-size:clamp(14.5px,2.2vw,16.5px)!important;line-height:1.3!important;color:#f8fafc!important;
      display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;margin:0!important;
    }
    .poster-card .card-meta-row{
      display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-top:auto;
    }
    .poster-card .meta{font-size:12.5px!important;font-weight:600;color:#94a3b8!important;line-height:1.3;white-space:nowrap}
    .poster-card .badge-genre{
      background:linear-gradient(125deg,#3b82f6,#8b5cf6);color:#fff;border-radius:999px;
      padding:3px 10px;font-size:10.5px;font-weight:800;letter-spacing:.04em;
      box-shadow:0 4px 12px rgba(59,130,246,.28);white-space:nowrap;
    }


    /* Polished Watch UI */
  .p4u-stage{border:1px solid rgba(99,102,241,.28)!important;box-shadow:0 24px 60px rgba(0,0,0,.68),0 0 0 1px rgba(255,255,255,.04) inset!important}
  .p4u-below-bar{background:rgba(15,20,32,.78);border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:8px 10px;backdrop-filter:blur(12px)}
  .p4u-below-bar .p4u-ep-pill,.p4u-below-bar .p4u-up-btn{transition:transform .15s,background .15s,border-color .15s}
  .p4u-below-bar .p4u-ep-pill:hover:not(:disabled),.p4u-below-bar .p4u-up-btn:hover{transform:translateY(-1px);border-color:rgba(59,130,246,.55);background:rgba(30,41,66,.92)}
  .watch-info-card,.watch-side-card{background:linear-gradient(160deg,rgba(18,23,36,.95),rgba(11,15,24,.98))!important;border:1px solid rgba(255,255,255,.09)!important;box-shadow:0 14px 36px rgba(0,0,0,.42)}
  .watch-meta-pills span{background:rgba(59,130,246,.1)!important;border-color:rgba(59,130,246,.24)!important;color:#dbeafe!important;font-weight:700}
  .audio-track-select{transition:border-color .15s}
  .audio-track-select:focus,.audio-track-select:hover{border-color:#60a5fa}
</style>
  ${headExtra}
</head>
<body>
<div id="bg-portrait-wrap" aria-hidden="true">
  <div class="bg-portrait-layer" id="bgLayerA"></div>
  <div class="bg-portrait-layer" id="bgLayerB"></div>
</div>
<div id="bg-portrait-scrim" aria-hidden="true"></div>
<header class="topbar">
  <div class="topbar-inner">
    <a class="topbar-logo" href="/" title="HITLER YT Home">
      <span class="brand-lockup">
        <span class="brand-mark" aria-hidden="true"><span>HYT</span></span>
        <span class="brand-word">HITLER YT</span><span class="brand-sub">MOVIES · SERIES · BEYOND LIMITS</span>
      </span>
    </a>
    <nav class="topbar-links" aria-label="Primary">
      <a href="/">Home</a>
      <a href="/search">Search</a>
      <a href="/?category=movies">Movies</a>
      <a href="/?category=series">Series</a>
    </nav>
  </div>
</header>
<main class="wrap">${content}</main>
<footer class="footer wrap"><strong>HITLER YT</strong> · Stream &amp; download with speed and clarity</footer>
<script>
(function initLatestPortraitBackground(){
  const layerA = document.getElementById('bgLayerA');
  const layerB = document.getElementById('bgLayerB');
  if (!layerA || !layerB) return;
  const toPoster = (p) => (!p ? '' : (String(p).startsWith('http') ? p : ('https://image.tmdb.org/t/p/w780' + p)));
  const dayOf = (item) => {
    const raw = item && (item.created_at || item.updated_at || item.published_at || '');
    const m = String(raw).match(/^(\d{4}-\d{2}-\d{2})/);
    return m ? m[1] : '';
  };
  function startRotation(items) {
    if (!Array.isArray(items) || !items.length) return;
    const withPoster = items.filter(x => x && x.poster_path);
    if (!withPoster.length) return;
    const days = withPoster.map(dayOf).filter(Boolean).sort().reverse();
    let pool = withPoster;
    if (days.length) {
      const latestDay = days[0];
      const sameDay = withPoster.filter(x => dayOf(x) === latestDay);
      if (sameDay.length) pool = sameDay;
    } else {
      pool = withPoster.slice(0, 6);
    }
    const urls = [...new Set(pool.map(x => toPoster(x.poster_path)).filter(Boolean))];
    if (!urls.length) return;
    let useA = true, lastUrl = '';
    const showNext = () => {
      let pick = urls[0];
      if (urls.length > 1) {
        const candidates = urls.filter(u => u !== lastUrl);
        pick = candidates[Math.floor(Math.random() * candidates.length)] || urls[0];
      }
      lastUrl = pick;
      const active = useA ? layerA : layerB;
      const other = useA ? layerB : layerA;
      active.style.backgroundImage = 'url("' + pick.replace(/"/g, '\\"') + '")';
      active.classList.add('active');
      other.classList.remove('active');
      useA = !useA;
    };
    showNext();
    if (urls.length > 1) setInterval(showNext, 6000);
  }
  if (Array.isArray(window.__INITIAL_CATALOG__) && window.__INITIAL_CATALOG__.length) {
    startRotation(window.__INITIAL_CATALOG__);
  } else {
    fetch('/api/catalog', { cache: 'no-store' })
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        const list = d && (Array.isArray(d.items) ? d.items : (Array.isArray(d) ? d : []));
        if (list && list.length) startRotation(list);
      })
      .catch(() => {});
  }
})();
</script>
</body>
</html>`;
}

function pagePosterPath(p) {
  if (!p) return '/static/images/poster.jpg';
  return String(p).startsWith('http') ? p : `https://image.tmdb.org/t/p/w780${p}`;
}

function renderIndexHtml(initialItems, initialCategories, queryStr = '') {
  const content = `
<form class="search-bar" action="/search" method="get">
  <i class="fa-solid fa-magnifying-glass" aria-hidden="true"></i>
  <input name="q" value="${escapeHtml(queryStr)}" placeholder="Search your favourite content…" autocomplete="off">
  <button type="submit">Search</button>
</form>

<div class="section-head" id="catalog">
  <h2>Catalog</h2>
  <span class="chip" id="result-count">Loading…</span>
</div>
<div class="cat-nav" id="category-nav"></div>

<div class="poster-grid" id="catalog-grid">
  <p class="muted" style="grid-column:1/-1;padding:40px 0;text-align:center">Loading titles…</p>
</div>
<div class="related-pager catalog-pager" id="catalogPager" hidden></div>

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
    renderCategories([{ name: 'Movies', slug: 'movies' }, { name: 'Series', slug: 'series' }]);
  }

  // Refresh categories in background without blocking initial catalog paint
  fetch('/api/categories', { cache: 'no-store' })
    .then(r => r.ok ? r.json() : null)
    .then(cats => { if (cats && Array.isArray(cats.items) && cats.items.length) renderCategories(cats.items); })
    .catch(() => {});

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

  
  // Fill missing landscape backdrops from TMDB for all published home grid cards
  async function fillMissingBackdrops() {
    const imgs = Array.from(document.querySelectorAll('#catalog-grid img[data-needs-backdrop="1"]'));
    if (!imgs.length) return;
    const BATCH = 6;
    for (let i = 0; i < imgs.length; i += BATCH) {
      await Promise.all(imgs.slice(i, i + BATCH).map(async (img) => {
        const id = img.dataset.tmdbId || '';
        const title = img.dataset.title || '';
        const slug = img.dataset.slug || '';
        const mt = (img.dataset.mediaType || 'movie').toLowerCase() === 'tv' ? 'tv' : 'movie';
        if (!id && !title) return;
        try {
          const qs = new URLSearchParams({ type: mt });
          if (id) qs.set('id', id);
          if (title) qs.set('title', title);
          if (slug) qs.set('slug', slug);
          const r = await fetch('/api/tmdb-backdrop?' + qs.toString(), { cache: 'force-cache' });
          if (!r.ok) return;
          const d = await r.json();
          if (d && d.backdrop_path) {
            img.src = d.backdrop_path.startsWith('http') ? d.backdrop_path : ('https://image.tmdb.org/t/p/w1280' + d.backdrop_path);
            img.removeAttribute('data-needs-backdrop');
            if (Array.isArray(window.__INITIAL_CATALOG__)) {
              const hit = window.__INITIAL_CATALOG__.find(x => x && x.slug === slug);
              if (hit) hit.backdrop_path = d.backdrop_path;
            }
          }
        } catch (e) {}
      }));
    }
  }

  function renderItems(items) {
    const el = document.getElementById('catalog-grid');
    const countEl = document.getElementById('result-count');
    el.innerHTML = '';
    const list = Array.isArray(items) ? items : [];
    countEl.textContent = list.length ? list.length + ' titles' : '0 titles';

    if (!list.length) {
      el.innerHTML = '<p class="muted" style="grid-column:1/-1;padding:40px 0;text-align:center">No titles found. Publish movies or series from the bot or the panel.</p>';
      return;
    }

    const pager = document.getElementById('catalogPager');
    const PER_PAGE = 12;
    let catalogPage = 1;
    const maxPages = Math.max(1, Math.ceil(list.length / PER_PAGE));
    const drawPage = () => {
      el.innerHTML = '';
      for (const p of list.slice((catalogPage - 1) * PER_PAGE, catalogPage * PER_PAGE)) {
      const a = document.createElement('a');
      a.className = 'poster-card';
      a.href = '/title/' + encodeURIComponent(p.slug);
      const img = document.createElement('img');
      img.loading = 'lazy';
      img.decoding = 'async';
      // Landscape on home from TMDB backdrop (w1280); portrait poster only as last fallback
      function tmdbImg(path, size) {
        if (!path) return '';
        if (String(path).startsWith('http')) return path;
        return 'https://image.tmdb.org/t/p/' + size + path;
      }
      let homeSrc = tmdbImg(p.backdrop_path, 'w1280');
      if (!homeSrc && (p.tmdb_id || p.title)) {
        img.dataset.needsBackdrop = '1';
        if (p.tmdb_id) img.dataset.tmdbId = String(p.tmdb_id);
        if (p.title) img.dataset.title = String(p.title);
        if (p.slug) img.dataset.slug = String(p.slug);
        img.dataset.mediaType = String(p.media_type || 'movie');
      }
      if (!homeSrc) homeSrc = tmdbImg(p.poster_path, 'w780') || '/static/images/poster.jpg';
      img.src = homeSrc;
      img.alt = p.title || 'Poster';
      const media = document.createElement('div');
      media.className = 'card-media';
      const play = document.createElement('div'); play.className = 'play-hint'; play.innerHTML = '<span>▶</span>';
      const topBadges = document.createElement('div');
      topBadges.className = 'card-top-badges';
      const qPill = document.createElement('span'); qPill.className = 'badge-quality'; qPill.textContent = '1080P';
      topBadges.appendChild(qPill);
      const rVal = Number(p && p.rating);
      if (Number.isFinite(rVal) && rVal > 0) {
        const rPill = document.createElement('span'); rPill.className = 'badge-rating'; rPill.textContent = '★ ' + rVal.toFixed(1);
        topBadges.appendChild(rPill);
      }
      media.append(img, play, topBadges);
      const info = document.createElement('div');
      info.className = 'info';
      const t = document.createElement('div');
      t.className = 'title';
      t.textContent = p.title || 'Untitled';
      const metaRow = document.createElement('div');
      metaRow.className = 'card-meta-row';
      const m = document.createElement('div');
      m.className = 'meta';
      const year = (p.release_date || p.first_air_date || '').toString().slice(0, 4);
      const kind = String(p.media_type || '').toLowerCase() === 'tv' ? 'SERIES' : 'MOVIE';
      m.textContent = [year, kind].filter(Boolean).join(' · ');
      metaRow.appendChild(m);
      if (Array.isArray(p.genres) && p.genres.length) {
        const g = document.createElement('span'); g.className = 'badge-genre'; g.textContent = String(p.genres[0]).toUpperCase();
        metaRow.appendChild(g);
      }
      info.append(t, metaRow);
      a.append(media, info);
      el.append(a);
      }
    if (pager) {
      pager.hidden = maxPages <= 1;
      pager.innerHTML = '';
      if (maxPages > 1) {
        const make = (label, page, disabled = false, active = false) => {
          const b = document.createElement('button');
          b.type = 'button'; b.textContent = label; b.disabled = disabled;
          if (active) b.classList.add('active');
          b.onclick = () => { catalogPage = page; drawPage(); document.getElementById('catalog')?.scrollIntoView({behavior:'smooth',block:'start'}); };
          pager.appendChild(b);
        };
        const dots = () => {
          const s = document.createElement('span');
          s.textContent = '…';
          s.style.cssText = 'color:var(--muted);padding:0 4px;font-weight:700';
          pager.appendChild(s);
        };
        make('‹', catalogPage - 1, catalogPage <= 1);
        const nums = new Set([1, maxPages, catalogPage - 1, catalogPage, catalogPage + 1].filter(n => n >= 1 && n <= maxPages));
        const sorted = Array.from(nums).sort((x, y) => x - y);
        let prev = 0;
        for (const n of sorted) {
          if (prev && n - prev > 1) dots();
          make(String(n), n, false, n === catalogPage);
          prev = n;
        }
        make('›', catalogPage + 1, catalogPage >= maxPages);
      }
    }
    };
    drawPage();
    fillMissingBackdrops();
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
          const imgUrl = p.backdrop_path
            ? (String(p.backdrop_path).startsWith('http') ? p.backdrop_path : `https://image.tmdb.org/t/p/w1280${p.backdrop_path}`)
            : pagePosterPath(p.poster_path);
          const year = p.release_date ? String(p.release_date).slice(0, 4) : '';
          const mediaType = p.media_type ? escapeHtml(String(p.media_type).toUpperCase()) : '';
          const metaLine = [year, mediaType].filter(Boolean).join(' · ');
          const genreBadge = Array.isArray(p.genres) && p.genres.length ? `<span class="badge badge-genre">${escapeHtml(String(p.genres[0]).toUpperCase())}</span>` : '';
          return `<a class="poster-card" href="/title/${encodeURIComponent(p.slug)}">
            <div class="card-media">
              <img src="${escapeHtml(imgUrl)}" alt="${escapeHtml(p.title)}" loading="lazy">
              <div class="play-hint"><span>▶</span></div>
            </div>
            <div class="info">
              <div class="title">${escapeHtml(p.title)}</div>
              <div class="meta">${metaLine}</div>
              ${genreBadge ? `<div class="badge-row">${genreBadge}</div>` : ''}
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


function extractWatchShortCode(url) {
  if (!url || typeof url !== 'string') return null;
  const raw = url.trim();
  if (!raw) return null;
  let path = raw;
  try {
    if (raw.includes('://') || raw.startsWith('/')) {
      const u = new URL(raw.startsWith('/') ? `https://x.invalid${raw}` : raw);
      path = u.pathname || raw;
    }
  } catch (_) {
    path = raw;
  }
  const re = /(?:^|\/)watch\/([A-Za-z0-9_-]{3,64})(?:\/|$|\?|#)/i;
  let m = path.match(re);
  if (m) return m[1];
  m = raw.match(re);
  return m ? m[1] : null;
}

function filestoreDeepLink(username, shortCode) {
  const uname = String(username || '').trim().replace(/^@+/, '');
  const code = String(shortCode || '').trim();
  return `https://t.me/${uname}?start=file_${code}`;
}

/**
 * Mirror Watch-box groups only. Button URLs must be /watch/<code>.
 * Dedupes by short_code to avoid duplicate Telegram buttons/groups.
 */
function buildTelegramGroupsFromWatch(groups, filestoreUsername) {
  const uname = String(filestoreUsername || '').trim().replace(/^@+/, '');
  if (!uname || !Array.isArray(groups) || !groups.length) return [];
  const out = [];
  const seenGlobal = new Set();
  for (const g of groups) {
    if (!g || typeof g !== 'object') continue;
    const box = String(g.box || 'watch').trim().toLowerCase();
    if (box && box !== 'watch') continue;
    const buttonsOut = [];
    const seenInGroup = new Set();
    const buttons = Array.isArray(g.buttons) ? g.buttons : [];
    for (const b of buttons) {
      if (!b || typeof b !== 'object') continue;
      const bname = String(b.name || '').trim();
      const burl = String(b.url || '').trim();
      if (!bname || !burl) continue;
      const code = extractWatchShortCode(burl);
      if (!code) continue;
      if (seenInGroup.has(code) || seenGlobal.has(code)) continue;
      seenInGroup.add(code);
      seenGlobal.add(code);
      buttonsOut.push({ name: bname, url: filestoreDeepLink(uname, code) });
    }
    if (buttonsOut.length) {
      out.push({
        box: 'telegram',
        name: String(g.name || 'Watch').trim() || 'Watch',
        buttons: buttonsOut,
      });
    }
  }
  return out;
}

function renderTitleHtml(page, cloudflareUrl, filestoreUsername = "") {
  const headExtra = `<link rel="preconnect" href="https://cdn.jsdelivr.net" crossorigin><link rel="dns-prefetch" href="https://cdn.jsdelivr.net"><link rel="preload" as="script" href="https://cdn.jsdelivr.net/npm/movi-player@0.4.1/dist/element.global.js" crossorigin><style>
    .detail{display:grid;grid-template-columns:minmax(160px,280px) 1fr;gap:28px;align-items:start;margin-top:12px}
    .detail img{width:100%;border-radius:16px;border:1px solid var(--line);box-shadow:var(--shadow)}
    .detail h1{font-size:clamp(1.5rem,4vw,2.35rem);line-height:1.1;margin:0 0 12px}
    .meta{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}
    .meta span{background:var(--bg-card);border:1px solid var(--line);padding:6px 12px;border-radius:999px;color:var(--muted);font-size:12px;font-weight:600}
    .eyebrow{color:var(--accent);font-weight:800;font-size:11px;letter-spacing:.14em;text-transform:uppercase;margin-bottom:8px;font-family:var(--font-display)}
    .tools{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
    .tools .btn-yellow{font-size:13px;padding:10px 16px;border-radius:999px}
    .link-box{background:linear-gradient(155deg,rgba(18,21,31,.98),rgba(10,12,18,.98));border:1px solid var(--line);border-radius:16px;padding:18px;margin-top:18px;box-shadow:0 12px 40px rgba(0,0,0,.25)}
    .link-box h2{font-size:1.05rem;margin:0 0 12px;display:flex;align-items:center;gap:10px}
    .link-box.watch h2{color:#7dd3fc}.link-box.download h2{color:#6ee7b7}.link-box.telegram h2{color:#5eead4}
    .link-box.telegram{border-color:rgba(61,214,198,.22);background:linear-gradient(155deg,#0c1924,#0a121c)}
    .link-box .box-hint{color:var(--muted);font-size:12px;margin:-4px 0 12px;line-height:1.4}
    .group-block{background:rgba(0,0,0,.25);border:1px solid var(--line);border-radius:12px;padding:14px;margin-top:10px}
    .season-selector{display:flex;flex-wrap:wrap;gap:9px;margin:0 0 14px;padding:10px;border:1px solid rgba(255,255,255,.1);border-radius:16px;background:rgba(8,9,13,.75)}
    .season-selector button{border:1px solid rgba(255,255,255,.1);background:#171820;color:#c8c6d1;border-radius:999px;padding:9px 15px;font-weight:750;cursor:pointer}
    .season-selector button.active{background:linear-gradient(120deg,#e8d9ff,#d8b77e);color:#17151b;border:0}

    .group-block h3{font-size:14px;margin:0 0 10px;color:var(--text);font-weight:700}
    .tools a[href*="t.me/"]{background:linear-gradient(135deg,#0ea5e9,#2563eb)!important;color:#fff;border:0}
    .groups-pager{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:14px;padding-top:12px;border-top:1px solid var(--line)}
    .groups-pager .pg-btn{background:var(--bg-card);border:1px solid var(--line);color:var(--text);border-radius:8px;padding:8px 12px;font:600 13px var(--font);cursor:pointer}
    .groups-pager .pg-btn:disabled{opacity:.4;cursor:not-allowed}
    .groups-pager .pg-info{color:var(--muted);font-size:13px;min-width:90px;text-align:center}
    .groups-pager .pg-jump{display:flex;align-items:center;gap:6px;margin-left:auto}
    .groups-pager .pg-jump label{color:var(--muted);font-size:12px}
    .groups-pager .pg-jump input{width:64px;background:var(--bg);border:1px solid var(--line);color:#fff;border-radius:8px;padding:8px 10px;font:600 13px var(--font);text-align:center}
    .groups-pager .pg-jump button{background:var(--accent);border:0;color:#0b0c10;border-radius:8px;padding:8px 12px;font:700 13px var(--font);cursor:pointer}
    .auto-open-banner{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:14px 16px;margin:12px 0 0;border-radius:14px;background:linear-gradient(135deg,rgba(139,92,246,.12),rgba(37,99,235,.1));border:1px solid rgba(139,92,246,.28);color:var(--text)}
    .auto-open-banner .spinner{width:22px;height:22px;border-radius:50%;border:2px solid rgba(255,255,255,.2);border-top-color:#8b5cf6;animation:title-spin .7s linear infinite}
    @keyframes title-spin{to{transform:rotate(360deg)}}
    .auto-open-banner a{color:#22d3ee;font-weight:700}
    @media(max-width:650px){.detail{grid-template-columns:1fr}.detail img{max-width:220px}.groups-pager .pg-jump{margin-left:0;width:100%}}
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
      let groupsContent;
      if (!boxGroups.length) {
        groupsContent = `<p class="muted groups-empty" style="margin:0;font-size:13px">No ${escapeHtml(boxTitle.toLowerCase())} links added yet.</p>`;
      } else {
        groupsContent = boxGroups
          .map((group, idx) => {
            const buttons = Array.isArray(group.buttons) ? group.buttons : [];
            const btnsHtml = buttons
              .map(
                (btn, bi) =>
                  `<a class="btn-yellow cf-media-btn${boxKey === 'watch' ? ' watch-btn' : ''}" href="${escapeHtml(btn.url || '#')}" data-raw-url="${escapeHtml(btn.url || '#')}" data-group-index="${idx}" data-button-index="${bi}" rel="noopener"${boxKey === 'watch' ? '' : ' target="_blank"'}>${escapeHtml(btn.name || 'Link')}</a>`,
              )
              .join('');
            return `<div class="group-block" data-group-index="${idx}" data-group-name="${escapeHtml(group.name || '')}">
                <h3>${escapeHtml(group.name || '')}</h3>
                <div class="tools">${btnsHtml}</div>
              </div>`;
          })
          .join('');
      }
      const pagerHtml =
        boxGroups.length > 5
          ? `<div class="groups-pager" data-box="${boxKey}" data-total="${boxGroups.length}" data-per-page="5">
        <button type="button" class="pg-btn pg-prev" aria-label="Previous page">← Prev</button>
        <span class="pg-info">Page <span class="pg-cur">1</span> / <span class="pg-max">1</span></span>
        <button type="button" class="pg-btn pg-next" aria-label="Next page">Next →</button>
        <div class="pg-jump">
          <label for="pg-jump-${boxKey}">Go to</label>
          <input id="pg-jump-${boxKey}" class="pg-input" type="number" min="1" inputmode="numeric" placeholder="#">
          <button type="button" class="pg-go">Go</button>
        </div>
      </div>`
          : '';

      return `<section class="link-box ${boxKey}" aria-label="${escapeHtml(boxTitle)} links" data-box="${boxKey}">
        <h2><span aria-hidden="true">${boxIcon}</span>${escapeHtml(boxTitle)}</h2>
        ${boxKey === 'watch' ? '<div class="season-selector" id="seasonSelector" hidden aria-label="Available seasons"></div>' : ''}
        <div class="groups-list" data-box="${boxKey}">${groupsContent}</div>
        ${pagerHtml}
      </section>`;
    })
    .join('');

  const tgGroups = buildTelegramGroupsFromWatch(allGroups, filestoreUsername);
  let telegramHtml = '';
  if (tgGroups.length) {
    const tgContent = tgGroups.map((group, idx) => {
      const buttons = Array.isArray(group.buttons) ? group.buttons : [];
      const btnsHtml = buttons.map((btn) =>
        `<a class="btn-yellow" href="${escapeHtml(btn.url || '#')}" rel="noopener" target="_blank">${escapeHtml(btn.name || 'Link')}</a>`
      ).join('');
      return `<div class="group-block" data-group-index="${idx}">
          <h3>${escapeHtml(group.name || '')}</h3>
          <div class="tools">${btnsHtml}</div>
        </div>`;
    }).join('');
    const tgPager = tgGroups.length > 5
      ? `<div class="groups-pager" data-box="telegram" data-total="${tgGroups.length}" data-per-page="5">
        <button type="button" class="pg-btn pg-prev" aria-label="Previous page">← Prev</button>
        <span class="pg-info">Page <span class="pg-cur">1</span> / <span class="pg-max">1</span></span>
        <button type="button" class="pg-btn pg-next" aria-label="Next page">Next →</button>
        <div class="pg-jump">
          <label for="pg-jump-telegram">Go to</label>
          <input id="pg-jump-telegram" class="pg-input" type="number" min="1" inputmode="numeric" placeholder="#">
          <button type="button" class="pg-go">Go</button>
        </div>
      </div>`
      : '';
    telegramHtml = `<section class="link-box telegram" aria-label="Telegram links" data-box="telegram">
        <h2><span aria-hidden="true">✈</span>Get in Telegram</h2>
        <p class="box-hint">Get file in Telegram</p>
        <div class="groups-list" data-box="telegram">${tgContent}</div>
        ${tgPager}
      </section>`;
  }

  const watchBoxGroups = allGroups.filter((g) => (g?.box || 'watch') === 'watch');
  const hasFirstWatch = watchBoxGroups.some((g) => Array.isArray(g.buttons) && g.buttons[0] && g.buttons[0].url);
  const autoBanner = hasFirstWatch
    ? `<div class="auto-open-banner" id="autoOpenBanner">
      <div class="spinner" aria-hidden="true"></div>
      <div>
        <strong>Opening built-in player…</strong>
        <div class="muted" style="font-size:12px;margin-top:2px">First Watch group · first quality</div>
      </div>
      <a href="#" id="manualWatchLink">Open manually</a>
    </div>`
    : '';

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
    ${autoBanner}
    ${boxesHtml}
    ${telegramHtml}
  </div>
</article>
<script>
(function(){
  const TITLE_SLUG = ${safeJsonForScript(page.slug || '')};
  function extractWatchCode(url) {
    if (!url) return null;
    try {
      const m = String(url).match(/(?:^|\\/)watch\\/([A-Za-z0-9_-]{3,64})(?:\\/|$|\\?|#)/i);
      return m ? m[1] : null;
    } catch (_) { return null; }
  }
  function buildWatchHref(rawUrl, groupIndex, buttonIndex) {
    const code = extractWatchCode(rawUrl);
    if (!code) return rawUrl;
    const params = new URLSearchParams();
    if (TITLE_SLUG) params.set('title', TITLE_SLUG);
    params.set('g', String(groupIndex || 0));
    params.set('b', String(buttonIndex || 0));
    return '/watch/' + code + '?' + params.toString();
  }
  document.querySelectorAll('a.watch-btn').forEach(a => {
    const raw = a.getAttribute('data-raw-url') || a.getAttribute('href') || '';
    const gi = parseInt(a.getAttribute('data-group-index') || '0', 10) || 0;
    const bi = parseInt(a.getAttribute('data-button-index') || '0', 10) || 0;
    const href = buildWatchHref(raw, gi, bi);
    a.setAttribute('href', href);
    a.removeAttribute('target');
  });
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
      document.querySelectorAll('a.cf-media-btn:not(.watch-btn)').forEach(a => {
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
  const firstBtn = document.querySelector('a.watch-btn[data-group-index="0"][data-button-index="0"]')
    || document.querySelector('a.watch-btn');
  if (firstBtn) {
    const dest = firstBtn.getAttribute('href');
    const manual = document.getElementById('manualWatchLink');
    if (manual) manual.setAttribute('href', dest);
    const params = new URLSearchParams(window.location.search);
    const skip = params.get('stay') === '1' || params.get('noredir') === '1';
    // Light-speed: only auto-open when a single watch group exists; multi-group stays interactive
    // Always open first watch group / first button instantly.
    // All groups remain selectable in the /watch/ side panel.
    if (!skip && dest) {
      window.location.replace(dest);
    } else {
      const banner = document.getElementById('autoOpenBanner');
      if (banner) banner.style.display = 'none';
    }
  }
  
  // Seasons from group names (S01E01 etc). Only real seasons. EXTRA for non-S/E groups.
  (function setupSeasonNavigation(){
    var list=document.querySelector('.groups-list[data-box="watch"]');
    var selector=document.getElementById('seasonSelector');
    if(!list||!selector) return;
    var blocks=Array.prototype.slice.call(list.querySelectorAll('.group-block'));
    if(blocks.length<=1) return;
    var parsed=[], nonEpisode=[];
    function parseEpisode(name) {
      const text = String(name || '').replace(/[._]+/g, ' ').trim();
      // NO backslash escapes — survives template-literal embedding on Vercel
      let m = text.match(/S([0-9]{1,3})[ ]*E([0-9]{1,4})/i);
      if (m) return { season: Number(m[1]), episode: Number(m[2]) };
      m = text.match(/([0-9]{1,3})[ ]*[xX][ ]*([0-9]{1,4})/);
      if (m) return { season: Number(m[1]), episode: Number(m[2]) };
      m = text.match(/Season[ ]*([0-9]{1,3})[ ]*(?:Episode|Ep)[ ]*([0-9]{1,4})/i);
      if (m) return { season: Number(m[1]), episode: Number(m[2]) };
      m = text.match(/S([0-9]{1,3})[ ._-]+(?:Episode|Ep)[ ]*([0-9]{1,4})/i);
      if (m) return { season: Number(m[1]), episode: Number(m[2]) };
      m = text.match(/(?:Episode|Ep)[ ]*([0-9]{1,4})/i);
      if (m) return { season: 1, episode: Number(m[1]) };
      // glued forms already covered by S01E01 first pattern
      return null;
    }
    blocks.forEach(function(block){
      var name=block.getAttribute('data-group-name')||(block.querySelector('h3')&&block.querySelector('h3').textContent)||'';
      var ep=parseEpisode(name);
      if(ep && Number.isFinite(ep.season) && Number.isFinite(ep.episode)){
        block.dataset.season=String(ep.season);block.dataset.episode=String(ep.episode);
        parsed.push({block:block,season:ep.season,episode:ep.episode,name:name});
      } else nonEpisode.push(block);
    });
    var watchPager=document.querySelector('.groups-pager[data-box="watch"]');
    if(parsed.length){
      parsed.sort(function(a,b){return a.season-b.season||a.episode-b.episode;});
      var merged=[];
      parsed.forEach(function(item){
        var key=item.season+'-'+item.episode,existing=merged.find(function(x){return x.key===key;});
        if(!existing){merged.push({key:key,season:item.season,episode:item.episode,block:item.block});return;}
        var target=existing.block.querySelector('.tools'),source=item.block.querySelector('.tools');
        if(target&&source){ Array.prototype.slice.call(source.querySelectorAll('a')).forEach(function(a){ target.appendChild(a.cloneNode(true)); }); }
        item.block.style.display='none';
      });
      selector.hidden=false;
      if(watchPager) watchPager.style.display='none';
      var seasons=Array.from(new Set(merged.map(function(x){return x.season;}))).sort(function(a,b){return a-b;});
      var selected=seasons[0];
      seasons.forEach(function(season){
        var count=merged.filter(function(x){return x.season===season;}).length;
        var b=document.createElement('button'); b.type='button'; b.dataset.season=String(season);
        b.textContent='Season '+season+' ('+count+' episodes)';
        b.addEventListener('click',function(){selected=season;render();});
        selector.appendChild(b);
      });
      function render(){
        selector.querySelectorAll('button').forEach(function(b){b.classList.toggle('active',Number(b.dataset.season)===selected);});
        nonEpisode.forEach(function(b){b.style.display='none';});
        merged.forEach(function(x){x.block.style.display=x.season===selected?'':'none'; list.appendChild(x.block);});
      }
      render();
      // EXTRA section for non-episode groups
      if(nonEpisode.length){
        var extra=document.createElement('div'); extra.className='extra-groups'; extra.style.marginTop='16px';
        var h=document.createElement('h3'); h.textContent='EXTRA / Other groups'; h.style.fontSize='14px'; h.style.marginBottom='8px';
        extra.appendChild(h);
        nonEpisode.forEach(function(b){ b.style.display=''; extra.appendChild(b); });
        list.parentNode.appendChild(extra);
      }
      return;
    }
    // No S/E labels: group picker
    if(nonEpisode.length>1){
      selector.hidden=false; selector.setAttribute('aria-label','Available groups');
      if(watchPager) watchPager.style.display='none';
      var selected=0;
      nonEpisode.forEach(function(block,i){
        var b=document.createElement('button'); b.type='button'; b.dataset.group=String(i);
        b.textContent=((block.querySelector('h3')&&block.querySelector('h3').textContent)||('Group '+(i+1))).trim();
        b.addEventListener('click',function(){selected=i;render();});
        selector.appendChild(b);
      });
      function render(){
        selector.querySelectorAll('button').forEach(function(b){b.classList.toggle('active',Number(b.dataset.group)===selected);});
        nonEpisode.forEach(function(b,i){b.style.display=i===selected?'':'none';});
      }
      render();
    }
  })();

  var PER=5;
  function setupBox(boxKey){
    var list=document.querySelector('.groups-list[data-box="'+boxKey+'"]');
    var pager=document.querySelector('.groups-pager[data-box="'+boxKey+'"]');
    if(!list) return;
    var blocks=Array.prototype.slice.call(list.querySelectorAll('.group-block'));
    if(blocks.length<=PER){ if(pager) pager.style.display='none'; return; }
    if(pager) pager.style.display='flex';
    var page=1;
    var max=Math.max(1,Math.ceil(blocks.length/PER));
    var curEl=pager&&pager.querySelector('.pg-cur');
    var maxEl=pager&&pager.querySelector('.pg-max');
    var prevBtn=pager&&pager.querySelector('.pg-prev');
    var nextBtn=pager&&pager.querySelector('.pg-next');
    var input=pager&&pager.querySelector('.pg-input');
    var goBtn=pager&&pager.querySelector('.pg-go');
    if(maxEl) maxEl.textContent=String(max);
    if(input){ input.max=String(max); input.min='1'; }
    function render(){
      page=Math.min(Math.max(1,page),max);
      var start=(page-1)*PER, end=start+PER;
      blocks.forEach(function(el,i){ el.style.display=(i>=start&&i<end)?'':'none'; });
      if(curEl) curEl.textContent=String(page);
      if(prevBtn) prevBtn.disabled=page<=1;
      if(nextBtn) nextBtn.disabled=page>=max;
      if(input) input.value='';
    }
    function goTo(n){
      var num=parseInt(n,10);
      if(!isFinite(num)||num<1) return;
      page=num; render();
    }
    if(prevBtn) prevBtn.addEventListener('click',function(){ page-=1; render(); });
    if(nextBtn) nextBtn.addEventListener('click',function(){ page+=1; render(); });
    if(goBtn&&input){
      goBtn.addEventListener('click',function(){ goTo(input.value); });
      input.addEventListener('keydown',function(e){ if(e.key==='Enter'){ e.preventDefault(); goTo(input.value); } });
      input.addEventListener('change',function(){ goTo(input.value); });
    }
    render();
  }
  setupBox('watch');
  setupBox('download');
  setupBox('telegram');
})();
</script>`;

  return renderBaseLayout({
    title: `${page.title || 'Title'} · HITLER YT`,
    headExtra,
    content,
  });
}

function renderPanelHtml(settings) {
  const headExtra = `<link rel="preconnect" href="https://cdn.jsdelivr.net" crossorigin><link rel="dns-prefetch" href="https://cdn.jsdelivr.net"><link rel="preload" as="script" href="https://cdn.jsdelivr.net/npm/movi-player@0.4.1/dist/element.global.js" crossorigin><style>
    .admin-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
    .admin-card{padding:18px}
    .switch{display:flex;justify-content:space-between;align-items:center;padding:12px 0;border-bottom:1px solid #1e3d64}
    .switch input{accent-color:#8b5cf6;width:20px;height:20px}
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
    <h2>🌐 Website & ngrok Tunnel URLs</h2>
    <label class="field-label">ngrok URL (Download, Stream & Button Links)</label>
    <input id="base-url" type="url" placeholder="https://your-ngrok-domain.ngrok-free.app">
    <label class="field-label">Backend Tunnel URL (ngrok → VPS)</label>
    <input id="backend-url" type="url" placeholder="https://your-ngrok-domain.ngrok-free.app">
    <label class="field-label">Vercel Website URL (Catalog Website)</label>
    <input id="vercel-url" type="url" readonly placeholder="Detected on first Vercel visit">
    <small class="muted" id="website-urls"></small>
    <button class="btn accent" type="button" onclick="saveUrls()" style="margin-top:8px;width:100%">Save & Rewrite Button URLs</button>
    <p id="url-status" class="muted" style="margin-top:6px">All download links, stream links, and title-page button URLs automatically use your ngrok URL.</p>
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
      <h3 style="color:#22d3ee">▶ WATCH BOX GROUPS</h3>
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
    box.textContent = 'Saved to MongoDB! Download links, stream links, and title-page button URLs now use this ngrok URL.';
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
      backdrop_path: x.backdrop_path || '',
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
    document.getElementById('edit-status').textContent = '✅ Saved to shared MongoDB! Watch & Download groups and ngrok button URLs are now live.';
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

function renderWatchHtml(file, settings, streamBaseUrl, extra = {}) {
  const cfOrigin = validPublicUrl(streamBaseUrl) || settings.cloudflare_url || settings.backend_url || settings.base_url || '';
  const sizeMb = file.file_size ? (Number(file.file_size) / (1024 * 1024)).toFixed(2) : '0.00';
  const downloadsEnabled = settings.downloads_enabled !== false;
  const fsUser = String(extra.filestoreUsername || settings.filestore_bot_username || '').replace(/^@+/, '');
  const dlHref = downloadsEnabled
    ? (cfOrigin ? `${cfOrigin}/dl/${encodeURIComponent(file.short_code)}` : `/dl/${encodeURIComponent(file.short_code)}`)
    : (fsUser ? `https://t.me/${fsUser}?start=file_${encodeURIComponent(file.short_code)}` : (cfOrigin ? `${cfOrigin}/dl/${encodeURIComponent(file.short_code)}` : `/dl/${encodeURIComponent(file.short_code)}`));

  const watchGroups = Array.isArray(extra.watchGroups) ? extra.watchGroups : [];
  const groupIndex = Number.isFinite(extra.groupIndex) ? Math.max(0, extra.groupIndex) : 0;
  const buttonIndex = Number.isFinite(extra.buttonIndex) ? Math.max(0, extra.buttonIndex) : 0;
  const titleSlug = String(extra.titleSlug || '');
  const posterUrl = String(extra.posterUrl || '');
  const groupName = (watchGroups[groupIndex] && watchGroups[groupIndex].name) || '';
  const curBtnName = (watchGroups[groupIndex] && watchGroups[groupIndex].buttons && watchGroups[groupIndex].buttons[buttonIndex] && watchGroups[groupIndex].buttons[buttonIndex].name) || 'Auto';

  const headExtra = `<link rel="preconnect" href="https://cdn.jsdelivr.net" crossorigin><link rel="dns-prefetch" href="https://cdn.jsdelivr.net"><link rel="preload" as="script" href="https://cdn.jsdelivr.net/npm/movi-player@0.4.1/dist/element.global.js" crossorigin><style>
  /* Play4u /watch/tt38984184 two-column theater layout */
  .watch-grid{display:grid;grid-template-columns:minmax(0,1fr) 310px;gap:18px;margin-top:14px;align-items:start}
  @media(max-width:960px){.watch-grid{grid-template-columns:1fr}}
  .p4u-stage{
    position:relative;width:100%;aspect-ratio:16/9;background:#000;
    border-radius:16px;overflow:hidden;border:1px solid rgba(255,255,255,.12);
    box-shadow:0 20px 50px rgba(0,0,0,.55);user-select:none;-webkit-tap-highlight-color:transparent;
  }
  .p4u-stage:fullscreen,.p4u-stage:-webkit-full-screen,.p4u-stage.is-pseudo-fs{
    position:fixed!important;inset:0!important;z-index:9999!important;
    width:100vw!important;height:100dvh!important;aspect-ratio:auto!important;
    border-radius:0!important;border:0!important;margin:0!important;
  }
  body.p4u-fs-lock{overflow:hidden!important}
  .watch-info-card{
    background:var(--bg-card,#121722);border:1px solid var(--line,rgba(255,255,255,.08));
    border-radius:16px;padding:18px;margin-top:14px;
  }
  .watch-info-card h1{font-size:clamp(1.15rem,2.6vw,1.55rem);margin-bottom:8px;word-break:break-word}
  .watch-meta-pills{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 12px}
  .watch-meta-pills span{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);padding:4px 11px;border-radius:999px;font-size:12px;color:#cbd5e1}
  .watch-tools{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
  .watch-side{display:flex;flex-direction:column;gap:14px}
  .watch-side-card{background:var(--bg-card,#121722);border:1px solid var(--line,rgba(255,255,255,.08));border-radius:16px;padding:16px}
  .watch-side-card h3{font-size:14px;margin-bottom:10px;color:#fff}
  .ext-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
  .ext-btn{display:flex;align-items:center;justify-content:center;padding:10px;border-radius:10px;background:#161d2c;border:1px solid rgba(255,255,255,.12);color:#fff;font-size:12px;font-weight:700;text-decoration:none}
  .ext-btn:hover{border-color:#3b82f6;background:#1c2538}
  .p4u-backdrop {
    position: absolute; inset: 0;
    background-size: cover; background-position: center;
    filter: blur(18px) brightness(0.45) saturate(1.2);
    transform: scale(1.08);
    z-index: 0;
    pointer-events: none;
    transition: opacity .35s ease;
  }
  .p4u-stage.is-playing .p4u-backdrop { opacity: 0; }

  .p4u-stage movi-player {
    position: relative;
    z-index: 1;
    width: 100%;
    height: 100%;
    display: block;
    background: transparent;
    object-fit: contain;
  }
  movi-player.player-fit-cover, movi-player.player-fit-cover::part(video) { object-fit: cover !important; }
  movi-player.player-fit-contain, movi-player.player-fit-contain::part(video) { object-fit: contain !important; }
  video#nativeVideo.player-fit-cover { object-fit: cover !important; }
  video#nativeVideo.player-fit-contain { object-fit: contain !important; }

  /* Show Movi-Player's built-in controls (audio/subtitles/seek/speed) when visible; hide on tap or Hide Controls */
  #playerWrapper:not(.controls-hidden) movi-player::part(chrome),
  #playerWrapper:not(.controls-hidden) movi-player::part(controls),
  #playerWrapper:not(.controls-hidden) movi-player::part(bottom-controls),
  #playerWrapper:not(.controls-hidden) movi-player::part(control-bar),
  #playerWrapper:not(.controls-hidden) movi-player::part(bottom-bar),
  #playerWrapper:not(.controls-hidden) movi-player::part(progress-container) {
    opacity: 1 !important;
    visibility: visible !important;
    pointer-events: auto !important;
  }
  .p4u-below-bar {
    display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap;
    margin-top: 10px;
  }
  .p4u-below-bar .p4u-ep-pill, .p4u-below-bar .p4u-up-btn {
    padding: 8px 14px; font-size: 12px; border-radius: 10px;
  }
  @media (max-width: 600px) {
    .p4u-top { padding: 8px 10px 12px !important; }
    .p4u-circle-btn, .p4u-back-btn { width: 32px !important; height: 32px !important; }
    .p4u-below-bar { gap: 6px; }
    .p4u-below-bar .p4u-ep-pill, .p4u-below-bar .p4u-up-btn { padding: 7px 10px; font-size: 11px; }
  }

  /* Center Play Triangle */
  .p4u-center-play {
    position: absolute; z-index: 15; left: 50%; top: 50%; transform: translate(-50%, -50%);
    width: 72px; height: 72px; border: 0; background: transparent; color: #fff;
    display: flex; align-items: center; justify-content: center; cursor: pointer;
    filter: drop-shadow(0 6px 24px rgba(0,0,0,.75));
    transition: transform .18s ease, opacity .2s ease;
  }
  .p4u-center-play:hover { transform: translate(-50%, -50%) scale(1.1); }
  .p4u-center-play svg { width: 52px; height: 52px; fill: #fff; }
  .p4u-stage.is-playing .p4u-center-play { opacity: 0; pointer-events: none; }

  /* Loading Spinner */
  .p4u-loading {
    position: absolute; inset: 0; z-index: 14; display: none;
    align-items: center; justify-content: center; pointer-events: none;
    background: rgba(0,0,0,.25);
  }
  .p4u-loading.active { display: flex; }
  .p4u-spinner {
    width: 44px; height: 44px; border-radius: 50%;
    border: 3px solid rgba(255,255,255,.18); border-top-color: #3b82f6;
    animation: p4u-spin .75s linear infinite;
  }
  @keyframes p4u-spin { to { transform: rotate(360deg); } }

  /* Top Overlay */
  .p4u-top {
    position: absolute; top: 0; left: 0; right: 0; z-index: 30;
    padding: max(14px, env(safe-area-inset-top)) max(16px, env(safe-area-inset-right)) 28px max(16px, env(safe-area-inset-left));
    background: linear-gradient(180deg, rgba(0,0,0,.85) 0%, rgba(0,0,0,.4) 65%, transparent 100%);
    display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;
    transition: opacity .22s ease, visibility .22s ease;
  }
  .p4u-title-wrap { display: flex; align-items: flex-start; gap: 10px; min-width: 0; flex: 1; }
  .p4u-back-btn {
    width: 38px; height: 38px; border-radius: 50%; border: 1px solid rgba(255,255,255,.16);
    background: rgba(15,18,28,.75); color: #fff; display: inline-flex; align-items: center; justify-content: center;
    flex: 0 0 auto; cursor: pointer; backdrop-filter: blur(10px); text-decoration: none;
  }
  .p4u-back-btn:hover { background: rgba(59,130,246,.35); border-color: rgba(59,130,246,.6); }
  .p4u-titles { min-width: 0; }
  .p4u-title {
    font-family: var(--font-display, system-ui); font-size: clamp(14px, 2.4vw, 18px);
    font-weight: 800; color: #fff; line-height: 1.25;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .p4u-sub {
    font-size: 12px; color: #9ca3af; margin-top: 2px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .p4u-sub .p4u-lang { color: #38bdf8; font-weight: 600; }

  .p4u-top-actions { display: flex; align-items: center; gap: 8px; flex: 0 0 auto; flex-wrap: wrap; justify-content: flex-end; }
  .p4u-circle-btn {
    width: 38px; height: 38px; border-radius: 50%; border: 1px solid rgba(255,255,255,.16);
    background: rgba(15,18,28,.78); color: #f8fafc; display: inline-flex; align-items: center; justify-content: center;
    cursor: pointer; backdrop-filter: blur(10px); transition: background .15s, border-color .15s, transform .12s;
  }
  .p4u-circle-btn:hover { background: rgba(139,92,246,.35); border-color: rgba(139,92,246,.7); transform: scale(1.05); }
  .p4u-circle-btn svg { width: 17px; height: 17px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }

  /* Bottom Overlay */
  .p4u-bottom {
    position: absolute; left: 0; right: 0; bottom: 0; z-index: 30;
    padding: 36px max(16px, env(safe-area-inset-right)) max(14px, env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left));
    background: linear-gradient(0deg, rgba(0,0,0,.92) 0%, rgba(0,0,0,.62) 60%, transparent 100%);
    display: flex; flex-direction: column; gap: 10px;
    transition: opacity .22s ease, visibility .22s ease;
  }

  /* Episode Prev/Next Pills */
  .p4u-ep-nav { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
  .p4u-ep-pill {
    padding: 7px 14px; border-radius: 8px; border: 1px solid rgba(255,255,255,.18);
    background: rgba(15,18,28,.76); color: #fff; font-size: 12px; font-weight: 700;
    cursor: pointer; backdrop-filter: blur(8px); transition: background .15s, border-color .15s;
  }
  .p4u-ep-pill:hover:not(:disabled) { background: rgba(59,130,246,.3); border-color: rgba(59,130,246,.6); }
  .p4u-ep-pill:disabled { opacity: .42; cursor: not-allowed; color: #9ca3af; }

  /* Progress / Scrubber */
  .p4u-progress-row { display: flex; align-items: center; gap: 10px; }
  .p4u-time { font-size: 11px; font-weight: 700; color: #cbd5e1; min-width: 42px; font-variant-numeric: tabular-nums; }
  .p4u-time.right { text-align: right; }
  .p4u-scrub {
    position: relative; flex: 1; height: 6px; border-radius: 999px;
    background: rgba(255,255,255,.22); cursor: pointer; touch-action: none;
  }
  .p4u-scrub-fill {
    position: absolute; left: 0; top: 0; bottom: 0; width: 0%;
    border-radius: 999px; background: linear-gradient(90deg, #3b82f6, #8b5cf6);
  }
  .p4u-scrub-thumb {
    position: absolute; top: 50%; left: 0%; transform: translate(-50%, -50%);
    width: 14px; height: 14px; border-radius: 50%; background: #fff;
    box-shadow: 0 0 8px rgba(59,130,246,.8);
  }

  /* Bottom Control Row */
  .p4u-bar { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
  .p4u-bar-left, .p4u-bar-right { display: flex; align-items: center; gap: 10px; }
  .p4u-icon-btn {
    border: 0; background: transparent; color: #fff; width: 34px; height: 34px;
    display: inline-flex; align-items: center; justify-content: center; cursor: pointer;
    border-radius: 8px; transition: background .15s;
  }
  .p4u-icon-btn:hover { background: rgba(255,255,255,.12); }
  .p4u-icon-btn svg { width: 20px; height: 20px; fill: currentColor; }
  .p4u-vol-range { width: 72px; accent-color: #3b82f6; cursor: pointer; }
  @media (max-width: 540px) { .p4u-vol-range { display: none; } }

  .p4u-up-btn {
    border: 1px solid rgba(255,255,255,.16); background: rgba(15,18,28,.75); color: #fff;
    padding: 6px 12px; border-radius: 8px; font-size: 12px; font-weight: 800; letter-spacing: .04em;
    display: inline-flex; align-items: center; gap: 5px; cursor: pointer; transition: background .15s, border-color .15s;
  }
  .p4u-up-btn:hover { background: rgba(139,92,246,.32); border-color: rgba(139,92,246,.65); }
  .p4u-up-btn.is-active { background: rgba(59,130,246,.3); border-color: rgba(59,130,246,.75); color: #dbeafe; }

  /* Controls Hidden on Tap or Button */
  .p4u-stage.controls-hidden .p4u-top,
  .p4u-stage.controls-hidden .p4u-bottom,
  .p4u-stage.controls-hidden .p4u-center-play,
  .p4u-stage.controls-hidden .p4u-pop {
    opacity: 0 !important;
    visibility: hidden !important;
    pointer-events: none !important;
    display: none !important;
  }

  /* Upward / Stage Popups (Quality/Switch, External Player) */
  .p4u-pop {
    position: absolute; z-index: 60; min-width: 210px; max-width: min(300px, 88vw);
    max-height: min(58dvh, 340px); overflow-y: auto;
    background: rgba(12,15,24,.97); border: 1px solid rgba(139,92,246,.42);
    border-radius: 14px; padding: 6px; display: none; flex-direction: column; gap: 2px;
    box-shadow: 0 18px 40px rgba(0,0,0,.7); backdrop-filter: blur(14px);
  }
  .p4u-pop.open { display: flex; }
  .p4u-pop.quality-pop { right: 16px; bottom: 14px; }
  .p4u-pop.external-pop { right: 16px; top: 60px; }
  .p4u-pop .pop-title { font-size: 10px; color: #93c5fd; padding: 6px 10px 4px; text-transform: uppercase; font-weight: 800; letter-spacing: .06em; }
  .p4u-pop button, .p4u-pop a {
    display: block; width: 100%; text-align: left; border: 0; background: transparent;
    color: #f1f5f9; padding: 9px 11px; border-radius: 9px; font-size: 13px; font-weight: 600;
    cursor: pointer; text-decoration: none;
  }
  .p4u-pop button:hover, .p4u-pop a:hover { background: rgba(59,130,246,.22); }
  .p4u-pop button.active-opt { background: rgba(59,130,246,.2); color: #93c5fd; }

  /* Allow taps on empty top/bottom gradient areas to toggle controls; only buttons capture clicks */
  .p4u-top, .p4u-bottom { pointer-events: none !important; }
  .p4u-top button, .p4u-top a, .p4u-bottom button, .p4u-bottom input, .p4u-bottom select, .p4u-bottom .p4u-scrub { pointer-events: auto !important; }
  @media (max-width: 600px) {
    .p4u-top { padding: 8px 10px 14px !important; }
    .p4u-bottom { padding: 18px 10px 8px !important; gap: 6px !important; }
    .p4u-circle-btn, .p4u-back-btn { width: 32px !important; height: 32px !important; }
    .p4u-ep-pill { padding: 4px 10px !important; font-size: 11px !important; }
  }

  /* Inline Episodes / Seasons & EXTRA side/below card */
  .side-eps{display:flex;flex-direction:column;gap:10px;max-height:540px}
  .side-season-bar{display:flex;flex-wrap:wrap;gap:6px}
  .side-season-bar button{border:1px solid rgba(255,255,255,.15);background:#151b29;color:#cbd5e1;border-radius:999px;padding:6px 12px;font-size:11px;font-weight:800;cursor:pointer}
  .side-season-bar button.active{background:linear-gradient(125deg,#3b82f6,#8b5cf6);color:#fff;border-color:transparent}
  .side-ep-tools{display:flex;flex-direction:column;gap:8px}
  .side-ep-tools select,.side-ep-tools input{width:100%;min-height:38px;border:1px solid rgba(255,255,255,.14);border-radius:10px;background:#0b0e16;color:#fff;padding:8px 10px;font-size:12px;outline:none}
  .side-ep-list{display:flex;flex-direction:column;gap:6px;overflow-y:auto;max-height:320px;padding-right:2px}
  .side-ep-btn{display:flex;align-items:center;gap:8px;padding:9px 11px;border-radius:10px;background:#141a28;border:1px solid rgba(255,255,255,.08);color:#e2e8f0;text-decoration:none;font-size:12px;font-weight:700}
  .side-ep-btn:hover{border-color:rgba(139,92,246,.5);background:#192133}
  .side-ep-btn.active{border-color:#3b82f6;background:rgba(59,130,246,.2);color:#fff}
  .side-ep-btn .ep-tag{background:rgba(139,92,246,.25);color:#c4b5fd;padding:2px 7px;border-radius:6px;font-size:10px;font-weight:800;flex:0 0 auto}
  .side-ep-btn .ep-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .side-ep-count{font-size:11px;color:#9ca3af}
  .audio-track-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:10px}
  .audio-track-select{background:#0b0e16;color:#fff;border:1px solid rgba(59,130,246,.35);border-radius:10px;padding:8px 12px;font-size:12px;font-weight:700;min-width:190px;outline:none}
  .audio-track-status{font-size:12px;color:#93c5fd}

  .toast {
    position: fixed; bottom: 84px; left: 50%; transform: translateX(-50%);
    background: rgba(12,16,26,.95); border: 1px solid rgba(59,130,246,.45);
    color: #fff; padding: 9px 16px; border-radius: 10px; font-size: 13px;
    opacity: 0; pointer-events: none; transition: opacity .2s; z-index: 10050;
  }
  .toast.active { opacity: 1; }
  
  /* Mobile Vertical: full edge-to-edge player + touch-scrollable popups */
  @media (max-width: 768px) {
    .watch-grid { margin-top: 8px; gap: 12px; }
    .p4u-stage:not(:fullscreen):not(:-webkit-full-screen):not(.is-pseudo-fs) {
      width: calc(100% + 28px) !important;
      max-width: none !important;
      margin-left: -14px !important;
      margin-right: -14px !important;
      border-radius: 0 !important;
      border-left: 0 !important;
      border-right: 0 !important;
    }
    @media (max-width: 600px) {
      .p4u-stage:not(:fullscreen):not(:-webkit-full-screen):not(.is-pseudo-fs) {
        width: calc(100% + 20px) !important;
        margin-left: -10px !important;
        margin-right: -10px !important;
      }
    }
    .p4u-stage:not(:fullscreen):not(:-webkit-full-screen):not(.is-pseudo-fs) .p4u-pop.open {
      position: fixed !important;
      left: 12px !important;
      right: 12px !important;
      bottom: 14px !important;
      top: auto !important;
      width: auto !important;
      max-width: none !important;
      max-height: 68dvh !important;
      overflow-y: auto !important;
      -webkit-overflow-scrolling: touch;
      touch-action: pan-y;
      overscroll-behavior: contain;
      z-index: 10020 !important;
    }
    .p4u-stage.is-pseudo-fs .p4u-pop.open,
    .p4u-stage:fullscreen .p4u-pop.open,
    .p4u-stage:-webkit-full-screen .p4u-pop.open {
      max-height: calc(100dvh - 80px) !important;
      overflow-y: auto !important;
      -webkit-overflow-scrolling: touch;
      touch-action: pan-y;
      overscroll-behavior: contain;
    }
    .audio-track-row { width: 100%; }
    .audio-track-select { flex: 1 1 180px; min-width: 0; max-width: 100%; }
  }
  /* Mobile Horizontal (Landscape): fit entire player in viewport */
  @media (max-height: 520px) and (orientation: landscape) {
    .p4u-stage:not(:fullscreen):not(:-webkit-full-screen):not(.is-pseudo-fs) {
      aspect-ratio: auto !important;
      height: 82dvh !important;
      max-height: 82dvh !important;
    }
  }
  .p4u-pop {
    -webkit-overflow-scrolling: touch;
    touch-action: pan-y;
    overscroll-behavior: contain;
  }

  /* Polished Watch UI */
  .p4u-stage{border:1px solid rgba(99,102,241,.28)!important;box-shadow:0 24px 60px rgba(0,0,0,.68),0 0 0 1px rgba(255,255,255,.04) inset!important}
  .p4u-below-bar{background:rgba(15,20,32,.78);border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:8px 10px;backdrop-filter:blur(12px)}
  .p4u-below-bar .p4u-ep-pill,.p4u-below-bar .p4u-up-btn{transition:transform .15s,background .15s,border-color .15s}
  .p4u-below-bar .p4u-ep-pill:hover:not(:disabled),.p4u-below-bar .p4u-up-btn:hover{transform:translateY(-1px);border-color:rgba(59,130,246,.55);background:rgba(30,41,66,.92)}
  .watch-info-card,.watch-side-card{background:linear-gradient(160deg,rgba(18,23,36,.95),rgba(11,15,24,.98))!important;border:1px solid rgba(255,255,255,.09)!important;box-shadow:0 14px 36px rgba(0,0,0,.42)}
  .watch-meta-pills span{background:rgba(59,130,246,.1)!important;border-color:rgba(59,130,246,.24)!important;color:#dbeafe!important;font-weight:700}
  .audio-track-select{transition:border-color .15s}
  .audio-track-select:focus,.audio-track-select:hover{border-color:#60a5fa}
  .p4u-stage video#nativeVideo {
    position: relative; z-index: 1; width: 100%; height: 100%;
    display: none; background: #000; object-fit: contain;
  }
  .p4u-stage.use-native movi-player { display: none !important; }
  .p4u-stage.use-native video#nativeVideo { display: block !important; }
  .p4u-stage:not(.use-native) .p4u-bottom.native-only { display: none !important; }
  .p4u-stage:not(.use-native) .p4u-center-play { display: none !important; }
  .p4u-speed-sel {
    background: rgba(15,18,28,.85); color: #fff; border: 1px solid rgba(255,255,255,.18);
    border-radius: 8px; padding: 4px 8px; font-size: 11px; font-weight: 700; cursor: pointer; outline: none;
  }
</style>`;

  const content = `
<div class="watch-grid">
  <div>
    <div class="p4u-stage" id="playerWrapper">
  ${posterUrl ? `<div class="p4u-backdrop" style="background-image:url('${escapeHtml(posterUrl)}')"></div>` : ''}
  <movi-player id="moviPlayer" playsinline controls preload="auto" spinnerdelay="800" fallback="native" subtitlepicker fastseek resume persist theme="dark" themecolor="#3b82f6" tabindex="0"></movi-player>
  <video id="nativeVideo" playsinline preload="metadata" crossorigin="anonymous"></video>
  <button type="button" class="p4u-center-play" id="centerPlayBtn" aria-label="Play or Pause">
    <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
  </button>
  <div class="p4u-loading" id="playerLoading"><div class="p4u-spinner"></div></div>

  <div class="p4u-top">
    <div class="p4u-title-wrap">
      <a class="p4u-back-btn" href="${titleSlug ? '/title/' + encodeURIComponent(titleSlug) : '/'}" title="Back" aria-label="Back">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5"/><path d="M12 19l-7-7 7-7"/></svg>
      </a>
      <div class="p4u-titles">
        <div class="p4u-title">${escapeHtml(file.filename || 'Media')}${groupName ? ' · ' + escapeHtml(groupName) : ''}</div>
        <div class="p4u-sub">${escapeHtml(sizeMb)} MB · <span class="p4u-lang">${escapeHtml(curBtnName)}</span></div>
      </div>
    </div>
    <div class="p4u-top-actions">
      <button type="button" class="p4u-circle-btn" id="pcTopSwitchBtn" title="Switch Quality / Source" aria-label="Switch Quality or Source"><svg viewBox="0 0 24 24"><path d="M16 3h5v5"/><path d="M4 20L21 3"/><path d="M21 16v5h-5"/><path d="M15 15l6 6"/><path d="M4 4l5 5"/></svg></button>
      <button type="button" class="p4u-circle-btn" id="pcLaptopBtn" title="External Player" aria-label="External Player"><svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M2 20h20"/></svg></button>
      <button type="button" class="p4u-circle-btn" id="pcDownloadBtn" title="Download" aria-label="Download"><svg viewBox="0 0 24 24"><path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M5 21h14"/></svg></button>
      <button type="button" class="p4u-circle-btn" id="pcFitBtn" title="Fit / Fill" aria-label="Fit or Fill"><svg viewBox="0 0 24 24"><path d="M4 8V4h4M16 4h4v4M20 16v4h-4M8 20H4v-4"/></svg></button>
      <button type="button" class="p4u-circle-btn" id="pcRetryBtn" title="Retry" aria-label="Retry"><svg viewBox="0 0 24 24"><path d="M20 7v5h-5"/><path d="M4.8 9a8 8 0 0 1 13.4-2L20 12M4 17v-5h5"/><path d="M19.2 15a8 8 0 0 1-13.4 2L4 12"/></svg></button>
      <button type="button" class="p4u-circle-btn" id="pcShareBtn" title="Share" aria-label="Share"><svg viewBox="0 0 24 24"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg></button>
      <button type="button" class="p4u-circle-btn" id="pcTopHideBtn" title="Hide Controls" aria-label="Hide Controls"><svg viewBox="0 0 24 24"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/></svg></button>
      <button type="button" class="p4u-circle-btn" id="pcTopFsBtn" title="Fullscreen" aria-label="Fullscreen"><svg id="topFsIcon" viewBox="0 0 24 24"><path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"/></svg></button>
    </div>
  </div>
  <div class="p4u-pop quality-pop" id="switchPop" role="menu"><div class="pop-title">Switch Quality / Stream</div><div id="switchPopList"></div></div>
  <div class="p4u-pop external-pop" id="laptopPop" role="menu"><div class="pop-title">Open in External Player</div><div id="laptopPopList"></div></div>

  <div class="p4u-bottom native-only" id="builtInBar">
    <div class="p4u-progress-row">
      <span class="p4u-time" id="curTimeLabel">0:00</span>
      <div class="p4u-scrub" id="scrubTrack" role="slider" aria-label="Seek">
        <div class="p4u-scrub-fill" id="scrubFill"></div>
        <div class="p4u-scrub-thumb" id="scrubThumb"></div>
      </div>
      <span class="p4u-time right" id="durTimeLabel">0:00</span>
    </div>
    <div class="p4u-bar">
      <div class="p4u-bar-left">
        <button type="button" class="p4u-icon-btn" id="playPauseBtn" aria-label="Play/Pause"><svg id="playPauseIcon" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></button>
        <button type="button" class="p4u-icon-btn" id="seekBackBtn" title="-10s" aria-label="Rewind 10 seconds" style="font-size:11px;font-weight:800">-10s</button>
        <button type="button" class="p4u-icon-btn" id="seekFwdBtn" title="+10s" aria-label="Forward 10 seconds" style="font-size:11px;font-weight:800">+10s</button>
        <button type="button" class="p4u-icon-btn" id="muteBtn" aria-label="Mute/Unmute"><svg viewBox="0 0 24 24"><path d="M11 5L6 9H2v6h4l5 4V5z"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07" fill="none" stroke="currentColor" stroke-width="2"/></svg></button>
        <input type="range" class="p4u-vol-range" id="volRange" min="0" max="1" step="0.05" value="1" aria-label="Volume">
      </div>
      <div class="p4u-bar-right">
        <select class="p4u-speed-sel" id="speedSelect" aria-label="Playback speed">
          <option value="0.5">0.5x</option>
          <option value="0.75">0.75x</option>
          <option value="1" selected>1x</option>
          <option value="1.25">1.25x</option>
          <option value="1.5">1.5x</option>
          <option value="2">2x</option>
        </select>
        <button type="button" class="p4u-icon-btn" id="pcFullscreenBtn" aria-label="Fullscreen"><svg id="fsIcon" viewBox="0 0 24 24"><path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"/></svg></button>
      </div>
    </div>
  </div>
    </div>
    <div class="p4u-below-bar">
      <button type="button" class="p4u-ep-pill" id="prevGroupBtn" disabled>← Previous</button>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        ${watchGroups.length > 1 ? '<button type="button" class="p4u-up-btn" id="episodesDrawerBtn">^ EPISODES</button>' : ''}
        <button type="button" class="p4u-up-btn" id="toggleEngineBtn" title="Active: Player (tap to switch to Built-in HTML5 Player)">🎬 Player</button>
        <button type="button" class="p4u-up-btn" id="pcSwitchBtn"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 3h5v5"/><path d="M4 20L21 3"/><path d="M21 16v5h-5"/><path d="M15 15l6 6"/><path d="M4 4l5 5"/></svg> Switch</button>
        <button type="button" class="p4u-up-btn" id="pcHideCtrlBtn" aria-pressed="false" title="Hide or show player overlay controls">👁 Hide Controls</button>
      </div>
      <button type="button" class="p4u-ep-pill" id="nextGroupBtn" disabled>Next Episode →</button>
    </div>
    ${watchGroups.length > 1 ? `<div class="watch-side-card side-eps" id="sideEpsCard" style="margin-top:14px"><h3>Episodes / Seasons</h3><div class="side-season-bar" id="sideSeasonBar"></div><div class="side-ep-tools"><select id="seasonFilter" aria-label="Select season"><option value="all">All Seasons</option></select><input id="episodeSearch" type="search" placeholder="Search EP…"></div><div class="side-ep-list" id="sideEpList"></div><div class="side-ep-count" id="episodeCount"></div></div>` : ''}
    <div class="watch-info-card">
      <h1>${escapeHtml(file.filename || 'Media')}</h1>
      <div class="watch-meta-pills">
        <span>${escapeHtml(sizeMb)} MB</span>
        <span>${escapeHtml(file.access_count || 0)} views</span>
        ${groupName ? `<span>${escapeHtml(groupName)}</span>` : ''}
      </div>
      <div class="watch-tools">
        ${downloadsEnabled ? `<a class="btn btn-accent" id="infoDlBtn" href="${escapeHtml(dlHref)}" download>⬇ Download</a>` : ''}
        ${fsUser ? `<a class="btn" id="tgBotBtn" href="https://t.me/${encodeURIComponent(fsUser)}?start=file_${encodeURIComponent(file.short_code)}" target="_blank" rel="noopener" style="background:linear-gradient(135deg,#0ea5e9,#2563eb);color:#fff;border:0">✈ Get in Telegram</a>` : (!downloadsEnabled ? `<a class="btn btn-accent" id="infoDlBtn" href="${escapeHtml(dlHref)}" target="_blank" rel="noopener">⬇ Download</a>` : '')}
        <button type="button" class="btn" onclick="navigator.clipboard?.writeText(location.href).then(()=>showToast('Watch link copied!'))">Copy Watch Link</button>
        <button type="button" class="btn" onclick="navigator.clipboard?.writeText(STREAM_URL).then(()=>showToast('Stream URL copied!'))">Copy Stream URL</button>
      </div>
    </div>
  </div>
  <aside class="watch-side">
    <div class="watch-side-card">
      <h3>Open in External Player</h3>
      <div class="ext-grid" id="sideExtGrid"></div>
    </div>
  </aside>
</div>
<div class="toast" id="toast"><span id="toast-message">Done</span></div>
<script>
const SHORT_CODE = ${safeJsonForScript(file.short_code)};
let STREAM_ORIGIN = (${safeJsonForScript(cfOrigin)} || window.location.origin).replace(/\\/+$/, '');
const PLAY_ORIGIN = window.location.origin;
const PROXY_STREAM_URL = PLAY_ORIGIN + '/stream/' + SHORT_CODE;
const DIRECT_STREAM_URL = STREAM_ORIGIN + '/stream/' + SHORT_CODE;
const HAS_DIRECT_TUNNEL = /^https:\\/\\//i.test(STREAM_ORIGIN) && !STREAM_ORIGIN.includes('vercel.app') && !STREAM_ORIGIN.includes('pages.dev');
const IS_SAME_VPS_HOST = !PLAY_ORIGIN.includes('vercel.app') && !PLAY_ORIGIN.includes('pages.dev');
const STREAM_URL = IS_SAME_VPS_HOST ? PROXY_STREAM_URL : (HAS_DIRECT_TUNNEL ? DIRECT_STREAM_URL : PROXY_STREAM_URL);
const FILE_NAME = ${safeJsonForScript(file.filename || 'video.mkv')};
const EXT_FILE_PART = '/' + encodeURIComponent(FILE_NAME || 'video.mkv');
const EXTERNAL_STREAM_URL = (IS_SAME_VPS_HOST ? PROXY_STREAM_URL : DIRECT_STREAM_URL) + EXT_FILE_PART;
const DOWNLOADS_ENABLED = ${safeJsonForScript(downloadsEnabled)};
const FILESTORE_BOT = ${safeJsonForScript(fsUser)};
const DL_URL = DOWNLOADS_ENABLED
  ? (PLAY_ORIGIN + '/dl/' + SHORT_CODE)
  : (FILESTORE_BOT ? ('https://t.me/' + FILESTORE_BOT + '?start=file_' + SHORT_CODE) : (PLAY_ORIGIN + '/dl/' + SHORT_CODE));
const REMUX_URL = (IS_SAME_VPS_HOST ? PLAY_ORIGIN : (HAS_DIRECT_TUNNEL ? STREAM_ORIGIN : PLAY_ORIGIN)) + '/remux/' + SHORT_CODE;
const PLAYER_MIME = ${safeJsonForScript(file.mime_type || 'video/mp4')};
const INTENT_MIME = (!PLAYER_MIME || PLAYER_MIME === 'application/octet-stream') ? 'video/*' : PLAYER_MIME;
const WATCH_GROUPS = ${safeJsonForScript(watchGroups)};
const TITLE_SLUG = ${safeJsonForScript(titleSlug)};
const GROUP_INDEX = ${safeJsonForScript(groupIndex)};
const BUTTON_INDEX = ${safeJsonForScript(buttonIndex)};

const wrapper = document.getElementById('playerWrapper');
const mp = document.getElementById('moviPlayer');
const nv = document.getElementById('nativeVideo');
const loadingEl = document.getElementById('playerLoading');
let useNativeEngine = false;
let isRemuxMode = false;
let currentAudioTrackIdx = 0;

function activeEl() {
  return useNativeEngine ? nv : mp;
}

function showToast(msg) {
  const t = document.getElementById('toast');
  const m = document.getElementById('toast-message');
  if (m) m.textContent = msg || 'Done';
  t?.classList.add('active');
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => t?.classList.remove('active'), 2400);
}
function showLoading() { loadingEl?.classList.add('active'); }
function hideLoading() { loadingEl?.classList.remove('active'); }
function fmtTime(sec) {
  sec = Math.max(0, Math.floor(Number(sec) || 0));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return h > 0 ? \`\${h}:\${String(m).padStart(2,'0')}:\${String(s).padStart(2,'0')}\` : \`\${m}:\${String(s).padStart(2,'0')}\`;
}

function extractWatchCode(url) {
  if (!url) return null;
  const s = String(url);
  const m = s.match(/(?:^|\\/)watch\\/([A-Za-z0-9_-]{3,64})(?:\\/|$|\\?|#)/i);
  if (m) return m[1];
  const q = s.match(/[?&]path=([A-Za-z0-9_-]{3,64})(?:&|$|#)/i);
  return q ? q[1] : null;
}
function preferredButtonForGroup(gi) {
  const g = WATCH_GROUPS[gi];
  const btns = (g && g.buttons) || [];
  if (!btns.length) return 0;
  return BUTTON_INDEX < btns.length ? BUTTON_INDEX : 0;
}
function watchHrefFor(gi, bi) {
  const g = WATCH_GROUPS[gi];
  if (!g || !g.buttons || !g.buttons[bi]) return null;
  const code = extractWatchCode(g.buttons[bi].url);
  if (!code) return g.buttons[bi].url;
  const p = new URLSearchParams();
  if (TITLE_SLUG) p.set('title', TITLE_SLUG);
  p.set('g', String(gi));
  p.set('b', String(bi));
  return '/watch/' + code + '?' + p.toString();
}
function goToGroupButton(gi, bi) {
  const href = watchHrefFor(gi, bi);
  if (href) window.location.href = href;
}

function forceUnmuteActive() {
  const el = activeEl();
  if (!el) return;
  try {
    el.muted = false;
    el.removeAttribute('muted');
    if (!el.volume || el.volume === 0) el.volume = 1;
    if (el.shadowRoot) {
      el.shadowRoot.querySelectorAll('video, audio').forEach(v => {
        v.muted = false;
        v.removeAttribute('muted');
        if (!v.volume || v.volume === 0) v.volume = 1;
      });
    }
    const vr = document.getElementById('volRange');
    if (vr) vr.value = String(el.volume || 1);
  } catch (_) {}
}
function forceUnmuteMovi() { forceUnmuteActive(); }

function loadStreamOnActive(url, resumeAt, toastMsg) {
  const el = activeEl();
  if (!el) return;
  const t = Number.isFinite(resumeAt) ? resumeAt : (Number(el.currentTime) || 0);
  showLoading();
  const onReady = () => {
    if (t > 1) { try { el.currentTime = t; } catch (_) {} }
    forceUnmuteActive();
    hideLoading();
    const p = el.play && el.play();
    if (p && p.catch) p.catch(() => { syncPlayUI(); hideLoading(); });
  };
  el.addEventListener('loadedmetadata', onReady, { once: true });
  el.addEventListener('error', () => { hideLoading(); }, { once: true });
  el.src = url;
  if (typeof el.load === 'function') el.load();
  if (toastMsg) showToast(toastMsg);
}

function switchPlayerEngine(toNative, sourceUrl) {
  const prev = activeEl();
  const curT = Number(prev && prev.currentTime) || 0;
  const curRate = Number(prev && prev.playbackRate) || Number(document.getElementById('speedSelect')?.value) || 1;
  try { prev && prev.pause && prev.pause(); } catch (_) {}
  useNativeEngine = !!toNative;
  wrapper.classList.toggle('use-native', useNativeEngine);
  const toggleBtn = document.getElementById('toggleEngineBtn');
  if (toggleBtn) {
    toggleBtn.textContent = useNativeEngine ? '🎬 Built-in Player' : '🎬 Player';
    toggleBtn.title = useNativeEngine
      ? 'Active: Built-in HTML5 Player (tap to switch to Player)'
      : 'Active: Player (tap to switch to Built-in HTML5 Player)';
  }
  const target = activeEl();
  const defaultUrl = isRemuxMode ? (REMUX_URL + '?audio=' + currentAudioTrackIdx + '&aac=1') : STREAM_URL;
  const srcToPlay = sourceUrl || (prev && (prev.currentSrc || prev.src)) || defaultUrl;
  if (target) {
    showLoading();
    const onReady = () => {
      if (curT > 1) { try { target.currentTime = curT; } catch (_) {} }
      try { target.playbackRate = curRate; } catch (_) {}
      forceUnmuteActive();
      hideLoading();
      (typeof target.play === 'function' ? target.play() : null)?.catch(() => { syncPlayUI(); hideLoading(); });
    };
    target.addEventListener('loadedmetadata', onReady, { once: true });
    target.src = srcToPlay;
    if (typeof target.load === 'function') target.load();
    (typeof target.play === 'function' ? target.play() : null)?.catch(() => { syncPlayUI(); hideLoading(); });
  }
  syncPlayUI();
  showToast(useNativeEngine ? 'Switched to Built-in HTML5 Player' : 'Switched to Player');
}

document.getElementById('toggleEngineBtn')?.addEventListener('click', (e) => {
  e.stopPropagation();
  switchPlayerEngine(!useNativeEngine);
});

// Hide / Show Controls (both tap on empty video area AND explicit Hide/Show Controls buttons)
(function setupTapToggle(){
  let visible = true;
  const belowHideBtn = document.getElementById('pcHideCtrlBtn');
  const topHideBtn = document.getElementById('pcTopHideBtn');

  function syncMoviShadow(show) {
    try {
      const sr = mp && mp.shadowRoot;
      if (!sr) return;
      let st = sr.getElementById('amz-hide-controls-style');
      if (!show) {
        if (!st) {
          st = document.createElement('style');
          st.id = 'amz-hide-controls-style';
          st.textContent = '[part*="chrome"],[part*="controls"],[part*="bottom-bar"],[part*="control-bar"],[part*="progress-container"]{opacity:0!important;visibility:hidden!important;pointer-events:none!important}';
          sr.appendChild(st);
        }
      } else if (st) {
        st.remove();
      }
    } catch (_) {}
  }

  function setVisible(on) {
    visible = !!on;
    wrapper.classList.toggle('controls-hidden', !visible);
    syncMoviShadow(visible);
    if (belowHideBtn) {
      belowHideBtn.textContent = visible ? '👁 Hide Controls' : '👁 Show Controls';
      belowHideBtn.setAttribute('aria-pressed', String(!visible));
      belowHideBtn.classList.toggle('is-active', !visible);
    }
    if (topHideBtn) {
      topHideBtn.title = visible ? 'Hide Controls' : 'Show Controls';
      topHideBtn.setAttribute('aria-label', visible ? 'Hide Controls' : 'Show Controls');
    }
    if (!visible) {
      document.getElementById('switchPop')?.classList.remove('open');
      document.getElementById('laptopPop')?.classList.remove('open');
    }
  }

  window.togglePlayerControls = function (force) {
    const next = typeof force === 'boolean' ? force : !visible;
    setVisible(next);
    return visible;
  };

  belowHideBtn?.addEventListener('click', (e) => {
    e.stopPropagation();
    setVisible(!visible);
  });
  topHideBtn?.addEventListener('click', (e) => {
    e.stopPropagation();
    setVisible(!visible);
  });

  wrapper.addEventListener('pointerdown', () => { forceUnmuteActive(); }, { once: true, capture: true });
  wrapper.addEventListener('click', (e) => {
    if (!visible) {
      e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
      setVisible(true);
      return;
    }
    const path = (typeof e.composedPath === 'function') ? e.composedPath() : [e.target];
    const hitControl = path.some(node => {
      if (!node || node.nodeType !== 1 || !node.matches) return false;
      return node.matches('button, a, input, select, [role="button"], [role="menuitem"], [role="option"], [role="slider"], [role="menu"], [role="listbox"], [part*="control"], [part*="bar"], [part*="progress"], [part*="menu"], [part*="slider"], .p4u-pop, .p4u-top, .p4u-bottom');
    });
    if (hitControl) return;
    const rect = wrapper.getBoundingClientRect();
    if (e.clientY && rect.height > 140 && (e.clientY > rect.bottom - 88 || e.clientY < rect.top + 64)) return;
    setVisible(false);
  }, true);
})();

// Play/Pause, Seek, Speed, Volume, Fullscreen for both engines
const centerPlayBtn = document.getElementById('centerPlayBtn');
const playPauseBtn = document.getElementById('playPauseBtn');
const playPauseIcon = document.getElementById('playPauseIcon');
const muteBtn = document.getElementById('muteBtn');
const volRange = document.getElementById('volRange');
const scrubTrack = document.getElementById('scrubTrack');
const scrubFill = document.getElementById('scrubFill');
const scrubThumb = document.getElementById('scrubThumb');
const curTimeLabel = document.getElementById('curTimeLabel');
const durTimeLabel = document.getElementById('durTimeLabel');

function togglePlay() {
  const el = activeEl();
  if (!el) return;
  if (el.paused) {
    forceUnmuteActive();
    const p = el.play(); if (p && p.catch) p.catch(() => {});
  } else {
    el.pause();
  }
}
centerPlayBtn?.addEventListener('click', (e) => { e.stopPropagation(); togglePlay(); });
playPauseBtn?.addEventListener('click', (e) => { e.stopPropagation(); togglePlay(); });
document.getElementById('seekBackBtn')?.addEventListener('click', (e) => {
  e.stopPropagation();
  const el = activeEl();
  if (el) try { el.currentTime = Math.max(0, (Number(el.currentTime) || 0) - 10); } catch (_) {}
});
document.getElementById('seekFwdBtn')?.addEventListener('click', (e) => {
  e.stopPropagation();
  const el = activeEl();
  if (el) try { el.currentTime = Math.min(Number(el.duration) || 1e9, (Number(el.currentTime) || 0) + 10); } catch (_) {}
});
document.getElementById('speedSelect')?.addEventListener('change', (e) => {
  const el = activeEl();
  const rate = Number(e.target.value) || 1;
  if (el) {
    try { el.playbackRate = rate; } catch (_) {}
    try { el.shadowRoot?.querySelectorAll('video').forEach(v => { v.playbackRate = rate; }); } catch (_) {}
  }
  showToast('Speed: ' + rate + 'x');
});

function syncPlayUI() {
  const el = activeEl();
  const playing = el && !el.paused && !el.ended;
  wrapper.classList.toggle('is-playing', playing);
  if (playPauseIcon) {
    playPauseIcon.innerHTML = playing
      ? '<rect x="6" y="5" width="4" height="14"/><rect x="14" y="5" width="4" height="14"/>'
      : '<path d="M8 5v14l11-7z"/>';
  }
}

[mp, nv].forEach(el => {
  if (!el) return;
  el.addEventListener('play', () => { if (el === activeEl()) syncPlayUI(); });
  el.addEventListener('playing', () => { if (el === activeEl()) { syncPlayUI(); hideLoading(); } });
  el.addEventListener('pause', () => { if (el === activeEl()) syncPlayUI(); });
  el.addEventListener('waiting', () => { if (el === activeEl()) showLoading(); });
  el.addEventListener('canplay', () => { if (el === activeEl()) hideLoading(); });
  el.addEventListener('timeupdate', () => {
    if (el !== activeEl()) return;
    const cur = Number(el.currentTime) || 0;
    const dur = Number(el.duration) || 0;
    if (curTimeLabel) curTimeLabel.textContent = fmtTime(cur);
    if (durTimeLabel) durTimeLabel.textContent = fmtTime(dur);
    const pct = dur > 0 ? Math.min(100, (cur / dur) * 100) : 0;
    if (scrubFill) scrubFill.style.width = pct + '%';
    if (scrubThumb) scrubThumb.style.left = pct + '%';
  });
});

function seekFromEvent(clientX) {
  const el = activeEl();
  if (!el || !scrubTrack) return;
  const rect = scrubTrack.getBoundingClientRect();
  const dur = Number(el.duration) || 0;
  if (!dur || rect.width <= 0) return;
  const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
  try { el.currentTime = ratio * dur; } catch (_) {}
}
let dragging = false;
scrubTrack?.addEventListener('pointerdown', (e) => { dragging = true; scrubTrack.setPointerCapture(e.pointerId); seekFromEvent(e.clientX); });
scrubTrack?.addEventListener('pointermove', (e) => { if (dragging) seekFromEvent(e.clientX); });
scrubTrack?.addEventListener('pointerup', () => { dragging = false; });

muteBtn?.addEventListener('click', (e) => {
  e.stopPropagation();
  const el = activeEl();
  if (!el) return;
  el.muted = !el.muted;
  if (volRange) volRange.value = el.muted ? '0' : String(el.volume || 1);
});
volRange?.addEventListener('input', () => {
  const el = activeEl();
  if (!el) return;
  const v = Number(volRange.value);
  el.volume = v;
  el.muted = v === 0;
});

// Keyboard shortcuts
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && wrapper.classList.contains('is-pseudo-fs')) {
    e.preventDefault();
    exitPseudoFullscreen();
    return;
  }
  if (e.target && /^(INPUT|TEXTAREA|SELECT)$/i.test(e.target.tagName)) return;
  const el = activeEl();
  if (!el) return;
  const k = (e.key || '').toLowerCase();
  if (k === ' ' || k === 'k') { e.preventDefault(); togglePlay(); }
  else if (k === 'arrowleft') { e.preventDefault(); try { el.currentTime = Math.max(0, (Number(el.currentTime) || 0) - 10); } catch (_) {} }
  else if (k === 'arrowright') { e.preventDefault(); try { el.currentTime = Math.min(Number(el.duration) || 1e9, (Number(el.currentTime) || 0) + 10); } catch (_) {} }
  else if (k === 'm') { e.preventDefault(); el.muted = !el.muted; showToast(el.muted ? 'Muted' : 'Unmuted'); }
  else if (k === 'f') { e.preventDefault(); toggleFullscreen(); }
});

// Top & Bottom Action Buttons
const swBtn = document.getElementById('pcSwitchBtn');
const topSwBtn = document.getElementById('pcTopSwitchBtn');
const swPop = document.getElementById('switchPop');
const swList = document.getElementById('switchPopList');
const lpBtn = document.getElementById('pcLaptopBtn');
const lpPop = document.getElementById('laptopPop');
const lpList = document.getElementById('laptopPopList');

function renderSwitchMenu() {
  if (!swList) return;
  swList.innerHTML = '';
  const g = WATCH_GROUPS[GROUP_INDEX];
  const btns = (g && Array.isArray(g.buttons)) ? g.buttons : [];
  btns.forEach((b, i) => {
    const el = document.createElement('button');
    el.type = 'button';
    if (i === BUTTON_INDEX) el.className = 'active-opt';
    el.textContent = (b.name || ('Source ' + (i + 1))) + (i === BUTTON_INDEX ? ' ✓' : '');
    el.onclick = (e) => {
      e.stopPropagation();
      swPop.classList.remove('open');
      if (i !== BUTTON_INDEX) goToGroupButton(GROUP_INDEX, i);
    };
    swList.appendChild(el);
  });

  const aacBtn = document.createElement('button');
  aacBtn.type = 'button';
  if (isRemuxMode) aacBtn.className = 'active-opt';
  aacBtn.textContent = isRemuxMode ? '🔊 AAC Remux Active ✓' : '🔊 Fix Audio (AAC Remux)';
  aacBtn.onclick = (e) => {
    e.stopPropagation();
    swPop.classList.remove('open');
    isRemuxMode = true;
    loadStreamOnActive(REMUX_URL + '?audio=' + currentAudioTrackIdx + '&aac=1', null, 'Switched to AAC audio stream');
    renderSwitchMenu();
  };
  swList.appendChild(aacBtn);

  if (isRemuxMode) {
    const origBtn = document.createElement('button');
    origBtn.type = 'button';
    origBtn.textContent = '↺ Original Direct Stream';
    origBtn.onclick = (e) => {
      e.stopPropagation();
      swPop.classList.remove('open');
      isRemuxMode = false;
      loadStreamOnActive(STREAM_URL, null, 'Switched back to original stream');
      renderSwitchMenu();
    };
    swList.appendChild(origBtn);
  }

  const engBtn = document.createElement('button');
  engBtn.type = 'button';
  engBtn.textContent = useNativeEngine ? '🎬 Switch to Player (Movi)' : '🎬 Switch to Built-in HTML5 Player';
  engBtn.onclick = (e) => {
    e.stopPropagation();
    swPop.classList.remove('open');
    switchPlayerEngine(!useNativeEngine);
    renderSwitchMenu();
  };
  swList.appendChild(engBtn);
}

function toggleSwitchPop(e) {
  e.stopPropagation();
  lpPop?.classList.remove('open');
  if (wrapper.classList.contains('controls-hidden') && typeof window.togglePlayerControls === 'function') {
    window.togglePlayerControls(true);
  }
  renderSwitchMenu();
  swPop?.classList.toggle('open');
}
swBtn?.addEventListener('click', toggleSwitchPop);
topSwBtn?.addEventListener('click', toggleSwitchPop);
renderSwitchMenu();

function androidIntent(url, pkg, mime) {
  const u = new URL(url);
  return \`intent://\${u.host}\${u.pathname}\${u.search}#Intent;scheme=\${u.protocol.replace(':','')};action=android.intent.action.VIEW;type=\${mime||'video/*'};package=\${pkg};S.title=\${encodeURIComponent(FILE_NAME || 'Video')};S.browser_fallback_url=\${encodeURIComponent(url)};end\`;
}

(function initExternalPlayers(){
  const ua = navigator.userAgent || '';
  const isAndroid = /Android/i.test(ua);
  const isIOS = /iPhone|iPad|iPod/i.test(ua);
  const encodedUrl = encodeURIComponent(EXTERNAL_STREAM_URL);
  const players = [];

  if (isAndroid) {
    players.push(
      ['VLC', androidIntent(EXTERNAL_STREAM_URL, 'org.videolan.vlc', INTENT_MIME)],
      ['MX Player', androidIntent(EXTERNAL_STREAM_URL, 'com.mxtech.videoplayer.ad', INTENT_MIME)],
      ['MX Player Pro', androidIntent(EXTERNAL_STREAM_URL, 'com.mxtech.videoplayer.pro', INTENT_MIME)],
      ['PLAYit', androidIntent(EXTERNAL_STREAM_URL, 'com.playit.videoplayer', INTENT_MIME)],
      ['nPlayer', androidIntent(EXTERNAL_STREAM_URL, 'com.newin.nplayer.pro', INTENT_MIME)]
    );
  } else if (isIOS) {
    players.push(
      ['VLC (iOS)', 'vlc-x-callback://x-callback-url/stream?url=' + encodedUrl],
      ['nPlayer', 'nplayer-' + EXTERNAL_STREAM_URL],
      ['Infuse', 'infuse://x-callback-url/play?url=' + encodedUrl]
    );
  } else {
    players.push(
      ['VLC Desktop', 'vlc://' + EXTERNAL_STREAM_URL],
      ['PotPlayer', 'potplayer://' + EXTERNAL_STREAM_URL],
      ['IINA (Mac)', 'iina://weblink?url=' + encodedUrl]
    );
  }

  function addExtItem(label, href, onClick) {
    const a = document.createElement('a');
    a.href = href || '#';
    a.textContent = label;
    if (onClick) a.onclick = (e) => { e.preventDefault(); onClick(); };
    lpList?.appendChild(a);

    const b = document.createElement('a');
    b.className = 'ext-btn';
    b.href = href || '#';
    b.textContent = label;
    if (onClick) b.onclick = (e) => { e.preventDefault(); onClick(); };
    document.getElementById('sideExtGrid')?.appendChild(b);
  }

  players.forEach(([label, href]) => addExtItem(label, href));
  addExtItem('Download .M3U', '#m3u', () => {
    const m3u = \`#EXTM3U\\n#EXTINF:-1,\${FILE_NAME || 'Stream'}\\n\${EXTERNAL_STREAM_URL}\\n\`;
    const blob = new Blob([m3u], { type: 'audio/x-mpegurl' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = (FILE_NAME || 'stream').replace(/\\.[^/.]+$/, '') + '.m3u';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    showToast('M3U playlist downloaded — open with VLC/MPV');
  });
  addExtItem('Copy Stream URL', '#copy', () => {
    navigator.clipboard?.writeText(EXTERNAL_STREAM_URL).then(() => showToast('External Stream URL copied!'));
  });
})();

lpBtn?.addEventListener('click', (e) => {
  e.stopPropagation();
  swPop?.classList.remove('open');
  lpPop?.classList.toggle('open');
});

document.getElementById('pcDownloadBtn')?.addEventListener('click', (e) => {
  e.stopPropagation();
  const a = document.createElement('a'); a.href = DL_URL;
  if (DOWNLOADS_ENABLED) a.setAttribute('download', ''); else a.target = '_blank';
  a.rel = 'noopener'; document.body.appendChild(a); a.click(); a.remove();
  showToast(DOWNLOADS_ENABLED ? 'Download started' : 'Opening Telegram bot…');
});

document.getElementById('pcFitBtn')?.addEventListener('click', (e) => {
  e.stopPropagation();
  const el = activeEl();
  if (!el) return;
  const cover = el.classList.toggle('player-fit-cover');
  el.classList.toggle('player-fit-contain', !cover);
  try {
    el.style.objectFit = cover ? 'cover' : 'contain';
    el.shadowRoot?.querySelectorAll('video, canvas').forEach(v => { v.style.objectFit = cover ? 'cover' : 'contain'; });
  } catch (_) {}
  showToast(cover ? 'Fill mode' : 'Fit mode');
});

document.getElementById('pcRetryBtn')?.addEventListener('click', (e) => {
  e.stopPropagation();
  stallRetries = 0;
  recoveryInFlight = false;
  clearStallWatch();
  if (!useNativeEngine && !customElements.get('movi-player')) {
    switchPlayerEngine(true, STREAM_URL);
    return;
  }
  const url = isRemuxMode ? (REMUX_URL + '?audio=' + currentAudioTrackIdx + '&aac=1') : STREAM_URL;
  loadStreamOnActive(url, null, 'Retrying stream…');
});

document.getElementById('pcShareBtn')?.addEventListener('click', (e) => {
  e.stopPropagation();
  if (navigator.share) {
    navigator.share({ title: document.title, url: location.href }).catch((err) => {
      if (err && err.name === 'AbortError') return;
      navigator.clipboard?.writeText(location.href).then(() => showToast('Link copied!'));
    });
  } else if (navigator.clipboard) {
    navigator.clipboard.writeText(location.href).then(() => showToast('Link copied!'));
  }
});

// Fullscreen with safe Promise handling + CSS pseudo-fullscreen fallback
const fsBtn = document.getElementById('pcFullscreenBtn');
const topFsBtn = document.getElementById('pcTopFsBtn');
const fsIcon = document.getElementById('fsIcon');
const topFsIcon = document.getElementById('topFsIcon');

function isFullscreenActive() {
  return !!(document.fullscreenElement || document.webkitFullscreenElement || wrapper.classList.contains('is-pseudo-fs'));
}
function syncFullscreenUI() {
  const isFull = isFullscreenActive();
  const path = isFull
    ? '<path d="M9 4v5H4M15 4v5h5M20 15h-5v5M4 15h5v5"/>'
    : '<path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"/>';
  if (fsIcon) fsIcon.innerHTML = path;
  if (topFsIcon) topFsIcon.innerHTML = path;
  if (topFsBtn) topFsBtn.title = isFull ? 'Exit Fullscreen' : 'Fullscreen';
  if (fsBtn) fsBtn.title = isFull ? 'Exit Fullscreen' : 'Fullscreen';
}
function enterPseudoFullscreen() {
  wrapper.classList.add('is-pseudo-fs');
  document.body.classList.add('p4u-fs-lock');
  syncFullscreenUI();
}
function exitPseudoFullscreen() {
  wrapper.classList.remove('is-pseudo-fs');
  document.body.classList.remove('p4u-fs-lock');
  syncFullscreenUI();
}
function toggleFullscreen() {
  const nativeActive = document.fullscreenElement || document.webkitFullscreenElement;
  if (nativeActive) {
    const exitFn = document.exitFullscreen || document.webkitExitFullscreen;
    try {
      Promise.resolve(exitFn && exitFn.call(document)).catch(() => exitPseudoFullscreen());
    } catch (_) {
      exitPseudoFullscreen();
    }
    return;
  }
  if (wrapper.classList.contains('is-pseudo-fs')) {
    exitPseudoFullscreen();
    return;
  }
  const reqFn = wrapper.requestFullscreen || wrapper.webkitRequestFullscreen;
  if (reqFn) {
    const fsTimeout = setTimeout(() => {
      if (!document.fullscreenElement && !document.webkitFullscreenElement) {
        enterPseudoFullscreen();
      }
    }, 250);
    try {
      Promise.resolve(reqFn.call(wrapper)).then(() => {
        clearTimeout(fsTimeout);
        if (!document.fullscreenElement && !document.webkitFullscreenElement) {
          enterPseudoFullscreen();
        }
      }).catch(() => {
        clearTimeout(fsTimeout);
        enterPseudoFullscreen();
      });
    } catch (_) {
      clearTimeout(fsTimeout);
      enterPseudoFullscreen();
    }
  } else {
    enterPseudoFullscreen();
  }
}
fsBtn?.addEventListener('click', (e) => { e.stopPropagation(); toggleFullscreen(); });
topFsBtn?.addEventListener('click', (e) => { e.stopPropagation(); toggleFullscreen(); });
document.addEventListener('fullscreenchange', syncFullscreenUI);
document.addEventListener('webkitfullscreenchange', syncFullscreenUI);

// Prev / Next Episode
const prevBtn = document.getElementById('prevGroupBtn');
const nextBtn = document.getElementById('nextGroupBtn');
if (WATCH_GROUPS.length) {
  if (prevBtn) { prevBtn.disabled = GROUP_INDEX <= 0; prevBtn.onclick = () => { if (GROUP_INDEX > 0) goToGroupButton(GROUP_INDEX - 1, preferredButtonForGroup(GROUP_INDEX - 1)); }; }
  if (nextBtn) { nextBtn.disabled = GROUP_INDEX >= WATCH_GROUPS.length - 1; nextBtn.onclick = () => { if (GROUP_INDEX < WATCH_GROUPS.length - 1) goToGroupButton(GROUP_INDEX + 1, preferredButtonForGroup(GROUP_INDEX + 1)); }; }
}

// Episodes scroll button (exits fullscreen first if active)
document.getElementById('episodesDrawerBtn')?.addEventListener('click', (e) => {
  e.stopPropagation();
  if (isFullscreenActive()) toggleFullscreen();
  setTimeout(() => {
    document.getElementById('sideEpsCard')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, 80);
});
document.addEventListener('click', (e) => {
  const t = e.target;
  if (t?.closest && (t.closest('.p4u-pop') || t.closest('#pcSwitchBtn') || t.closest('#pcTopSwitchBtn') || t.closest('#pcLaptopBtn'))) return;
  swPop?.classList.remove('open');
  lpPop?.classList.remove('open');
});

(function initEpisodesSidePanel(){
  const list = document.getElementById('sideEpList');
  const seasonSelect = document.getElementById('seasonFilter');
  const search = document.getElementById('episodeSearch');
  const count = document.getElementById('episodeCount');
  const seasonBar = document.getElementById('sideSeasonBar');
  const card = document.getElementById('sideEpsCard');
  if (!list || !Array.isArray(WATCH_GROUPS) || WATCH_GROUPS.length <= 1) {
    if (card) card.style.display = 'none';
    return;
  }
  if (card) card.style.display = '';
  function parseEp(name) {
    const t = String(name || '').replace(/[._]+/g, ' ').trim();
    let m = t.match(/S0*([0-9]{1,3})[ -]*E(?:p|pisode)?[ ]*0*([0-9]{1,4})/i);
    if (m) return { s: Number(m[1]), e: Number(m[2]) };
    m = t.match(/0*([0-9]{1,3})[ ]*[xX][ ]*0*([0-9]{1,4})/);
    if (m) return { s: Number(m[1]), e: Number(m[2]) };
    m = t.match(/Season[ ]*0*([0-9]{1,3})[ -]*(?:Episode|Ep|E)[ ]*0*([0-9]{1,4})/i);
    if (m) return { s: Number(m[1]), e: Number(m[2]) };
    if (/Season[ ]*0*[0-9]{1,3}/i.test(t) && !/(?:Episode|Ep|E)[ ]*0*[0-9]{1,4}/i.test(t)) return null;
    m = t.match(/(?:Episode|Ep)[ ]*0*([0-9]{1,4})/i);
    if (m) return { s: 1, e: Number(m[1]) };
    return null;
  }
  const parsedRows = [];
  const extraRows = [];
  WATCH_GROUPS.forEach((g, idx) => {
    const p = parseEp(g && g.name);
    if (p && Number.isFinite(p.s) && p.s > 0 && Number.isFinite(p.e) && p.e > 0) parsedRows.push({ g, idx, p });
    else extraRows.push({ g, idx, p: null });
  });
  parsedRows.sort((a, b) => a.p.s - b.p.s || a.p.e - b.p.e || a.idx - b.idx);
  const seasons = [...new Set(parsedRows.map(r => r.p.s))].sort((a, b) => a - b);
  const useSeasons = seasons.length > 0;
  const curRow = parsedRows.find(r => r.idx === GROUP_INDEX);
  let selected = useSeasons ? (curRow ? String(curRow.p.s) : String(seasons[0])) : 'extra';

  if (seasonSelect) {
    seasonSelect.innerHTML = '';
    if (useSeasons) {
      const allOpt = document.createElement('option');
      allOpt.value = 'all'; allOpt.textContent = 'All Seasons';
      seasonSelect.appendChild(allOpt);
      seasons.forEach(s => {
        const o = document.createElement('option');
        o.value = String(s);
        const n = parsedRows.filter(r => r.p.s === s).length;
        o.textContent = 'Season ' + s + ' (' + n + ' eps)';
        seasonSelect.appendChild(o);
      });
    }
    if (extraRows.length) {
      const o = document.createElement('option');
      o.value = 'extra';
      o.textContent = useSeasons ? ('EXTRA (' + extraRows.length + ' groups)') : ('All Groups (' + extraRows.length + ')');
      seasonSelect.appendChild(o);
    }
  }

  function setSeasonUI(val) {
    selected = val;
    if (seasonSelect) seasonSelect.value = val;
    if (seasonBar) seasonBar.querySelectorAll('button').forEach(b => b.classList.toggle('active', b.dataset.season === val));
  }

  if (seasonBar) {
    seasonBar.innerHTML = '';
    if (useSeasons) {
      seasons.forEach(s => {
        const n = parsedRows.filter(r => r.p.s === s).length;
        const b = document.createElement('button'); b.type = 'button'; b.dataset.season = String(s);
        b.textContent = 'S' + s + ' (' + n + ')';
        b.onclick = () => { setSeasonUI(String(s)); render(); };
        seasonBar.appendChild(b);
      });
    }
    if (extraRows.length) {
      const b = document.createElement('button'); b.type = 'button'; b.dataset.season = 'extra';
      b.textContent = useSeasons ? ('EXTRA (' + extraRows.length + ')') : ('Groups (' + extraRows.length + ')');
      b.onclick = () => { setSeasonUI('extra'); render(); };
      seasonBar.appendChild(b);
    }
  }
  setSeasonUI(selected);

  function render() {
    const q = (search?.value || '').trim().toLowerCase();
    let rows = selected === 'extra' ? extraRows : (selected === 'all' ? parsedRows.concat(extraRows) : parsedRows.filter(r => String(r.p.s) === selected));
    if (q) rows = rows.filter(r => String(r.g.name || '').toLowerCase().includes(q));
    list.innerHTML = '';
    rows.forEach(r => {
      const a = document.createElement('a');
      a.className = 'side-ep-btn' + (r.idx === GROUP_INDEX ? ' active' : '');
      a.href = watchHrefFor(r.idx, preferredButtonForGroup(r.idx)) || '#';
      const tag = document.createElement('span'); tag.className = 'ep-tag';
      tag.textContent = r.p ? ('S' + String(r.p.s).padStart(2,'0') + ' · EP ' + String(r.p.e).padStart(2,'0')) : 'EXTRA';
      const nm = document.createElement('span'); nm.className = 'ep-name';
      nm.textContent = r.g.name || ('Group ' + (r.idx + 1));
      a.append(tag, nm);
      list.appendChild(a);
    });
    if (count) count.textContent = rows.length + ' ' + (selected === 'extra' ? 'group' : 'episode') + (rows.length === 1 ? '' : 's');
  }
  search?.addEventListener('input', render);
  if (seasonSelect) seasonSelect.onchange = () => { setSeasonUI(seasonSelect.value || 'all'); render(); };
  render();
})();


// Smooth Movi-Player v0.4.1 + Built-in HTML5 fallback playback, resume position & stall recovery
let stallRetries = 0, lastProgressAt = Date.now(), recoveryInFlight = false, stallTimer = null, usedBackendFallback = false, triedNativeRemux = false;
const BACKEND_STREAM_URL = (DIRECT_STREAM_URL !== STREAM_URL) ? DIRECT_STREAM_URL : PROXY_STREAM_URL;
const RESUME_KEY = 'ddstream:pos:' + SHORT_CODE;
let pendingResume = 0;
try { pendingResume = parseFloat(localStorage.getItem(RESUME_KEY)) || 0; } catch (_) {}

function savePosition() {
  try {
    const el = activeEl();
    const t = Number(el && el.currentTime);
    if (t && isFinite(t) && t > 2) localStorage.setItem(RESUME_KEY, String(t));
  } catch (_) {}
}
setInterval(savePosition, 3000);
window.addEventListener('beforeunload', savePosition);

function clearStallWatch() { if (stallTimer) { clearTimeout(stallTimer); stallTimer = null; } }
function scheduleStallRecovery() {
  clearStallWatch();
  const el = activeEl();
  if (!el || el.paused || el.ended || recoveryInFlight) return;
  const conn = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
  const eff = String((conn && conn.effectiveType) || '').toLowerCase();
  const waitMs = (conn && conn.saveData) || eff === '2g' || eff === 'slow-2g' ? 22000 : 15000;
  stallTimer = setTimeout(() => {
    const curEl = activeEl();
    if (!curEl || curEl.paused || curEl.ended) return;
    if (Date.now() - lastProgressAt < waitMs) { scheduleStallRecovery(); return; }
    if (stallRetries >= 2) {
      if (!usedBackendFallback && BACKEND_STREAM_URL !== STREAM_URL) {
        usedBackendFallback = true;
        stallRetries = 0;
        showToast('Switching to direct stream for smoother playback…');
        recoverStream(BACKEND_STREAM_URL);
      } else if (!useNativeEngine) {
        stallRetries = 0;
        switchPlayerEngine(true, BACKEND_STREAM_URL);
      } else {
        hideLoading();
        showToast('Buffering slow — tap Retry or use External Player');
      }
      return;
    }
    stallRetries += 1;
    recoverStream(usedBackendFallback ? BACKEND_STREAM_URL : STREAM_URL);
  }, waitMs);
}

function recoverStream(url) {
  const el = activeEl();
  if (!el || recoveryInFlight) return;
  recoveryInFlight = true;
  const t = Number(el.currentTime) || 0;
  showLoading();
  try {
    el.src = url;
    if (typeof el.load === 'function') el.load();
    const restore = () => {
      recoveryInFlight = false;
      if (t > 1) { try { el.currentTime = t; } catch (_) {} }
      forceUnmuteActive();
      (typeof el.play === 'function' ? el.play() : null)?.catch(() => {});
    };
    el.addEventListener('loadedmetadata', restore, { once: true });
    setTimeout(() => { if (recoveryInFlight) { recoveryInFlight = false; hideLoading(); } }, 12000);
  } catch (_) {
    recoveryInFlight = false;
    hideLoading();
  }
}

[mp, nv].forEach(el => {
  if (!el) return;
  el.addEventListener('timeupdate', () => { if (el === activeEl()) { lastProgressAt = Date.now(); if (!el.paused && !recoveryInFlight) scheduleStallRecovery(); } });
  el.addEventListener('playing', () => { if (el === activeEl()) { stallRetries = 0; lastProgressAt = Date.now(); hideLoading(); scheduleStallRecovery(); } });
  el.addEventListener('waiting', () => { if (el === activeEl()) { showLoading(); if (!recoveryInFlight) scheduleStallRecovery(); } });
  el.addEventListener('stalled', () => { if (el === activeEl()) { showLoading(); if (!recoveryInFlight) scheduleStallRecovery(); } });
  el.addEventListener('pause', () => { if (el === activeEl()) clearStallWatch(); });
  el.addEventListener('ended', () => { if (el === activeEl()) clearStallWatch(); });
  el.addEventListener('error', () => {
    if (el !== activeEl()) return;
    clearStallWatch();
    if (!usedBackendFallback && BACKEND_STREAM_URL !== STREAM_URL) {
      usedBackendFallback = true;
      showToast('Trying direct backup stream…');
      recoverStream(BACKEND_STREAM_URL);
    } else if (!useNativeEngine) {
      switchPlayerEngine(true, STREAM_URL);
    } else if (!triedNativeRemux) {
      triedNativeRemux = true;
      isRemuxMode = true;
      showToast('Remuxing stream for browser compatibility…');
      recoverStream(REMUX_URL + '?audio=' + currentAudioTrackIdx + '&aac=1');
    } else {
      hideLoading();
      showToast('Stream error — tap Retry or use External Player');
    }
  });
  el.addEventListener('loadedmetadata', () => {
    if (el === activeEl() && pendingResume > 5) {
      try { el.currentTime = pendingResume; } catch (_) {}
      pendingResume = 0;
    }
  }, { once: true });
});

// Register playback view once
(function wireViews(){
  let reg = false;
  const key = 'amz-play-view-' + SHORT_CODE;
  let tok = '';
  try {
    tok = localStorage.getItem(key) || ((crypto.randomUUID ? crypto.randomUUID() : 'v-' + Date.now()) + '-visitor');
    localStorage.setItem(key, tok);
  } catch (_) { tok = 'v-' + Date.now() + '-visitor'; }
  const send = () => {
    if (reg) return; reg = true;
    fetch('/api/views/' + encodeURIComponent(SHORT_CODE), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ visitor_token: tok })
    }).catch(() => {});
  };
  [mp, nv].forEach(el => {
    el?.addEventListener('play', send, { once: true });
    el?.addEventListener('playing', send, { once: true });
  });
})();

// Boot Movi-Player with automatic Built-in HTML5 <video> fallback
function loadScript(src) { return new Promise((res, rej) => { const s = document.createElement('script'); s.src = src; s.async = true; s.onload = res; s.onerror = rej; document.head.appendChild(s); }); }
(async () => {
  showLoading();
  try {
    try { await loadScript('https://cdn.jsdelivr.net/npm/movi-player@0.4.1/dist/element.global.js'); }
    catch (_) {
      try { await loadScript('https://unpkg.com/movi-player@0.4.1/dist/element.global.js'); }
      catch (__) { await loadScript('https://cdn.jsdelivr.net/npm/movi-player/dist/element.global.js'); }
    }
    await customElements.whenDefined('movi-player');
    mp.muted = false;
    mp.removeAttribute('muted');
    mp.volume = 1;
    let gotMeta = false;
    const onReady = () => { gotMeta = true; hideLoading(); };
    mp.addEventListener('loadedmetadata', onReady, { once: true });
    mp.addEventListener('loadeddata', onReady, { once: true });
    mp.addEventListener('canplay', onReady, { once: true });
    mp.src = STREAM_URL;
    if (typeof mp.load === 'function') mp.load();
    setTimeout(() => {
      if (!gotMeta && !useNativeEngine) {
        if (!usedBackendFallback && BACKEND_STREAM_URL && BACKEND_STREAM_URL !== STREAM_URL) {
          usedBackendFallback = true;
          mp.src = BACKEND_STREAM_URL;
          if (typeof mp.load === 'function') mp.load();
          mp.muted = false;
          (typeof mp.play === 'function' ? mp.play() : null)?.catch(() => { syncPlayUI(); hideLoading(); });
        } else {
          hideLoading();
        }
      }
    }, 6500);
    (typeof mp.play === 'function' ? mp.play() : null)?.catch(() => {
      try { mp.pause(); } catch (_) {}
      mp.muted = false;
      syncPlayUI();
      hideLoading();
    });
  } catch (_) {
    switchPlayerEngine(true, STREAM_URL);
  }
})();
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
  if (typeof path !== 'string') return false;
  // Normalize: drop query already handled; trim trailing slash except root
  let p = path.split('?')[0].split('#')[0].trim();
  if (p.length > 1 && p.endsWith('/')) p = p.slice(0, -1);
  return p.startsWith('/title/') && p.length > 7 && !p.startsWith('//');
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
  sendHtml(res, 200, `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Key Verification · HITLER YT</title><style>*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#090d18;color:#f8fafc;font:15px system-ui;padding:20px}main{width:min(420px,100%);padding:28px;border:1px solid #263449;border-radius:18px;background:#111827}h1{font-size:22px}p{color:#aab7ca;font-size:14px}input,button,a{width:100%;display:block;padding:13px;margin-top:12px;border-radius:10px;font:inherit}input{background:#0b1220;border:1px solid #334155;color:white}button{border:0;background:#38bdf8;color:#06111b;font-weight:800;cursor:pointer}a{text-align:center;color:#e2e8f0;border:1px solid #334155;text-decoration:none}</style></head><body><main><h1>🔐 Verify access key</h1><p>Title pages are protected. One valid key unlocks <b>all pages</b> for the admin-configured time (e.g. 4 hours). Enter a key or get a new one.</p><form method="POST" action="/verify-key"><input type="hidden" name="target" value="${safeTarget}"><input name="key" placeholder="Enter access key" required autocomplete="off"><button type="submit">Open title page</button></form><a href="/get-key-start?target=${encodedTarget}">🔑 Get Key</a>${safeError}</main></body></html>`);
}
function isJunkShortUrl(parsed, destination, providerHost) {
  const host = String(parsed.hostname || '').toLowerCase();
  const path = String(parsed.pathname || '').toLowerCase();
  const href = parsed.toString().toLowerCase();

  // Hard rejects: SVG namespaces, static assets, docs, self-destination
  const blockedHosts = new Set([
    'www.w3.org', 'w3.org', 'schema.org', 'example.com', 'example.org',
    'localhost', '127.0.0.1',
  ]);
  if (blockedHosts.has(host)) return true;
  if (host.endsWith('.w3.org')) return true;
  if (path.includes('/2000/svg') || path.endsWith('.svg') || path.endsWith('.ico')) return true;
  if (path.endsWith('.png') || path.endsWith('.jpg') || path.endsWith('.jpeg') || path.endsWith('.gif') || path.endsWith('.webp') || path.endsWith('.css') || path.endsWith('.js') || path.endsWith('.map')) return true;
  if (path.includes('favicon')) return true;
  if (href.includes('xmlns') || href.includes('/svg')) return true;

  // Never treat the landing/destination URL as the short link
  try {
    const dest = new URL(String(destination));
    if (parsed.origin === dest.origin && parsed.pathname === dest.pathname) return true;
    if (path.includes('/get-key')) return true;
  } catch {}

  // Reject if hostname is empty or protocol is not http(s)
  if (!host || !['http:', 'https:'].includes(parsed.protocol)) return true;
  return false;
}

function scoreShortUrlCandidate(parsed, providerHost) {
  const host = String(parsed.hostname || '').toLowerCase();
  const path = String(parsed.pathname || '/');
  let score = 10;
  // Prefer same domain family as the configured shortener API host
  if (providerHost) {
    const ph = providerHost.toLowerCase();
    if (host === ph || host.endsWith('.' + ph) || ph.endsWith('.' + host)) score += 50;
    // Common shortener: API on onrender/com, short domain different
    if (ph.includes('shortlink') && host.includes('short')) score += 20;
  }
  // Prefer short paths typical of shorteners (/abc12)
  if (path.length > 1 && path.length <= 40 && !path.includes('.')) score += 15;
  if (path.split('/').filter(Boolean).length <= 2) score += 5;
  // Prefer https
  if (parsed.protocol === 'https:') score += 3;
  return score;
}



function normalizePlainDestination(destination) {
  let dest = String(destination || '').trim();
  if (!dest) throw new Error('Destination URL is empty');
  // Fully decode if already percent-encoded (avoid double-encoding for Arolinks)
  for (let i = 0; i < 3; i++) {
    try {
      const decoded = decodeURIComponent(dest);
      if (decoded === dest) break;
      dest = decoded;
    } catch { break; }
  }
  dest = dest.replace(/\\\//g, '/').trim();
  if (!/^https?:\/\//i.test(dest)) dest = 'https://' + dest.replace(/^\/+/, '');
  let parsed;
  try { parsed = new URL(dest); } catch {
    throw new Error(`Destination URL is invalid (not a plain absolute URL): ${dest.slice(0, 120)}`);
  }
  if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) {
    throw new Error(`Destination URL is invalid: ${dest.slice(0, 120)}`);
  }
  // Return canonical plain URL (no extra encoding)
  return parsed.toString();
}

async function createVercelShortUrl(provider, destination) {
  // Filestore/Arolinks-style shortlink generator.
  // Critical: pass a PLAIN absolute destination URL; encode it only once in the query string.
  // Arolinks returns {"status":"error","message":["URL is invalid."]} if url is double-encoded or malformed.
  const domainRaw = String(provider.url || '').trim().replace(/\/+$/, '');
  const apiKey = String(provider.api_key || '').trim();
  if (!domainRaw || !apiKey) throw new Error('Shortener domain and API key are required');

  const plainDest = normalizePlainDestination(destination);

  let domain = domainRaw;
  if (!/^https?:\/\//i.test(domain)) domain = 'https://' + domain;
  // Prefer HTTPS for providers
  domain = domain.replace(/^http:\/\//i, 'https://');

  let parsedApi;
  try { parsedApi = new URL(domain); } catch { throw new Error('Invalid shortener API endpoint'); }
  if (!parsedApi.hostname) throw new Error('Invalid shortener API endpoint');

  const providerHost = parsedApi.hostname;
  let basePath = (parsedApi.pathname || '').replace(/\/+$/, '') || '';
  const existingQuery = new URLSearchParams(parsedApi.searchParams);

  // Build GET URLs with single encoding of the plain destination
  const buildEndpoint = (pathSuffix, extra = {}) => {
    const u = new URL(parsedApi.origin);
    u.pathname = pathSuffix || '/';
    for (const [k, v] of existingQuery.entries()) {
      if (k === 'api' || k === 'url' || k === 'format') continue;
      u.searchParams.set(k, v);
    }
    u.searchParams.set('api', apiKey);
    u.searchParams.set('url', plainDest); // URLSearchParams encodes once
    if (extra.format) u.searchParams.set('format', extra.format);
    return u.toString();
  };

  const endpoints = [];
  if (basePath.endsWith('/api') || basePath.endsWith('/st')) {
    endpoints.push(buildEndpoint(basePath));
    endpoints.push(buildEndpoint(basePath, { format: 'json' }));
  } else {
    for (const suffix of ['/api', '/st', basePath || '/']) {
      endpoints.push(buildEndpoint(suffix === '' ? '/' : suffix));
      endpoints.push(buildEndpoint(suffix === '' ? '/' : suffix, { format: 'json' }));
    }
  }
  const unique = [...new Set(endpoints)].slice(0, 8);

  const findShortUrl = (payload, depth = 0) => {
    if (depth > 3 || !payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
    if (payload.status === 'error' || payload.success === false) return null;
    const keys = [
      'shortenedUrl', 'shortened_url', 'short_url', 'short',
      'shortlink', 'shortLink', 'link', 'url', 'output', 'result',
    ];
    for (const key of keys) {
      const candidate = payload[key];
      if (typeof candidate === 'string' && /^https?:\/\//i.test(candidate.trim())) {
        return candidate.trim().replace(/\\\//g, '/');
      }
    }
    for (const key of ['data', 'result', 'shortener', 'response']) {
      const found = findShortUrl(payload[key], depth + 1);
      if (found) return found;
    }
    return null;
  };

  const extractApiError = (data, raw) => {
    if (data && typeof data === 'object') {
      let msg = data.message || data.error || data.detail || data.msg;
      if (Array.isArray(msg)) msg = msg.filter(Boolean).join('; ');
      if (msg) return String(msg).slice(0, 200);
      if (data.status === 'error') return 'Provider status=error (URL may be invalid or blocked)';
    }
    if (raw && raw.length < 300 && !raw.includes('<')) return raw.slice(0, 200);
    return null;
  };

  let lastError = 'No valid short URL returned';
  for (const apiUrl of unique) {
    try {
      const response = await fetch(apiUrl, {
        signal: AbortSignal.timeout(20000),
        redirect: 'manual',
        headers: {
          Accept: 'application/json, text/plain, */*',
          'User-Agent': 'Mozilla/5.0 (compatible; HITLER YT-Shortener/1.0)',
        },
      });
      const raw = (await response.text() || '').trim();
      let data = null;
      try { data = JSON.parse(raw); } catch {}

      // Surface provider error JSON clearly
      if (data && (data.status === 'error' || data.success === false || (data.shortenedUrl === '' && data.message))) {
        const apiErr = extractApiError(data, raw);
        lastError = apiErr || 'Provider rejected the URL';
        // Try next endpoint variant; if all fail, throw with this message
        continue;
      }

      let shortUrl = null;
      if (data && typeof data === 'object') shortUrl = findShortUrl(data);
      if (!shortUrl && /^https?:\/\//i.test(raw) && !raw.includes('<html') && !raw.includes('<!')) {
        shortUrl = raw.split(/\r?\n/)[0].trim().replace(/\\\//g, '/');
      }
      if (!shortUrl) {
        lastError = extractApiError(data, raw) || `Empty/invalid response (HTTP ${response.status})`;
        continue;
      }

      let parsedShort;
      try { parsedShort = new URL(shortUrl); } catch {
        lastError = `Invalid short URL: ${String(shortUrl).slice(0, 80)}`;
        continue;
      }
      if (isJunkShortUrl(parsedShort, plainDest, providerHost)) {
        lastError = `Rejected junk URL: ${parsedShort.toString().slice(0, 80)}`;
        continue;
      }
      return parsedShort.toString();
    } catch (err) {
      lastError = String(err && err.message ? err.message : err);
      continue;
    }
  }

  throw new Error(`Shortener API failed: ${lastError}. Destination used: ${plainDest.slice(0, 120)}`);
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
          ngrok_url: state.cloudflare_url,
        ngrok_url: state.cloudflare_url,
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
      ensureTmdbBackdrops(db).catch(() => {});
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
            { name: 'Series', slug: 'series' },
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
    // Prefer DB-stored TMDB key for landscape backfill
    ensureTmdbBackdrops(db, state._raw_map?.tmdb_api_key || process.env.TMDB_API_KEY).catch(() => {});
    const currentHost = requestHost(req);

    // Shortener/access-key workflow is hosted on Vercel so it protects the actual title pages.
    // Get Key must always run on the Vercel website host and redirect the browser
    // straight to the provider's generated shortlink (original short URL).
    if (pathname === '/get-key-start' && req.method === 'GET') {
      const target = searchParams.get('target') || '';
      if (!isProtectedTitlePath(target)) { sendJson(res, 400, { error: 'Shortener is available only for /title/* pages' }); return; }
      const cfg = await loadShortenerConfig(db);
      const providers = (cfg.shorteners || []).filter((x) => x && x.url && x.api_key);
      if (!isShortenerEnabled(cfg) || !providers.length) {
        sendHtml(res, 503, `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Shortener offline</title></head><body style="background:#090d18;color:#fff;font:16px system-ui;display:grid;place-items:center;min-height:100vh"><main style="max-width:420px;padding:24px"><h1>No shortener configured</h1><p>Enable shorteners in the bot with /shorteners and add at least one provider API URL + key.</p><a href="${escapeHtml(target)}" style="color:#38bdf8">Back to page</a></main></body></html>`);
        return;
      }
      const provider = providers[Math.floor(Math.random() * providers.length)];
      const token = crypto.randomBytes(24).toString('base64url');
      const key = crypto.randomBytes(9).toString('base64url');
      const now = Math.floor(Date.now() / 1000);
      const expiry = Math.max(60, Math.min(86400, Number(cfg.expiry_seconds) || 300));
      // Prefer stable public website origin (plain HTTPS URL for Arolinks)
      const vercelFromEnv = String(process.env.VERCEL_PROJECT_PRODUCTION_URL || process.env.VERCEL_URL || '')
        .replace(/^https?:\/\//i, '')
        .split('/')[0]
        .trim();
      const stateVercel = String(state.vercel_url || '').replace(/^https?:\/\//i, '').split('/')[0].trim();
      const publicHost = stateVercel || vercelFromEnv || currentHost;
      const origin = `https://${publicHost}`.replace(/\/+$/, '');
      const landing = `${origin}/get-key/${token}`;
      // Site-wide key: valid for every /title page until expires_at
      await db.collection('access_keys').insertOne({
        token,
        key,
        target,
        scope: 'site',
        created_at: now,
        expires_at: now + expiry,
      });
      try {
        // Shortlink API: https://provider/api?api=KEY&url=LANDING&format=json
        const shortUrl = await createVercelShortUrl(provider, landing);
        // Final safety: never redirect to junk assets / SVG namespaces
        let safe = false;
        try {
          const parsed = new URL(shortUrl);
          safe = !isJunkShortUrl(parsed, landing, new URL(String(provider.url || 'https://invalid.local')).hostname);
        } catch {}
        if (!safe) {
          throw new Error(`Provider returned an invalid short URL: ${String(shortUrl).slice(0, 120)}`);
        }
        res.statusCode = 302;
        res.setHeader('Location', shortUrl);
        res.setHeader('Cache-Control', 'no-store');
        res.end();
      } catch (error) {
        const detail = String(error.message || error).slice(0, 220);
        sendHtml(res, 502, `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Shortener error</title></head><body style="background:#090d18;color:#fff;font:16px system-ui;display:grid;place-items:center;min-height:100vh"><main style="max-width:440px;padding:24px"><h1>Shortener API failed</h1><p>${escapeHtml(detail)}</p><p>Check /shorteners provider URL (must be the API endpoint) and API key.</p><p>Example API URL: <code style="color:#38bdf8">https://shortlinkgenerator.onrender.com/api</code></p><p>You can open the key page directly:</p><a href="${escapeHtml(landing)}" style="color:#38bdf8">Open key page</a><br><br><a href="${escapeHtml(target)}" style="color:#aab7ca">Back to title</a></main></body></html>`);
      }
      return;
    }
    if (pathname.startsWith('/get-key/') && req.method === 'GET') {
      const token = pathname.slice('/get-key/'.length);
      const nowTs = Math.floor(Date.now() / 1000);
      const rec = await db.collection('access_keys').findOne({ token, expires_at: { $gt: nowTs } });
      if (!rec) { sendHtml(res, 410, '<h1>Key request expired</h1><p>Return to the title page and request a new key.</p>'); return; }
      const key = escapeHtml(rec.key);
      const target = escapeHtml(rec.target);
      const remaining = Math.max(0, rec.expires_at - nowTs);
      let remainLabel = `${remaining}s`;
      if (remaining >= 3600) remainLabel = `${Math.floor(remaining / 3600)}h ${Math.floor((remaining % 3600) / 60)}m`;
      else if (remaining >= 60) remainLabel = `${Math.floor(remaining / 60)} minutes`;
      sendHtml(res, 200, `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Your access key</title><style>body{background:#090d18;color:white;font:16px system-ui;display:grid;place-items:center;min-height:100vh;margin:0;padding:20px}main{background:#111827;padding:28px;border-radius:16px;width:min(400px,90vw);border:1px solid #263449}h2{margin:0 0 8px}p{color:#aab7ca;font-size:14px}input,button,a{box-sizing:border-box;width:100%;padding:13px;margin-top:12px;border-radius:9px;font:inherit}input{background:#0b1220;color:white;border:1px solid #334155;text-align:center;font-weight:700;letter-spacing:.4px}button{background:#38bdf8;border:0;font-weight:800;color:#06111b;cursor:pointer}a{display:block;text-align:center;color:#e2e8f0;text-decoration:none;border:1px solid #334155}.hint{color:#a3e635;font-size:13px;margin-top:14px;text-align:center}</style></head><body><main><h2>✅ Your key is ready</h2><p>This key unlocks <b>all title pages</b> on the site. Copy it, then continue.</p><input id="k" readonly value="${key}"><button id="copyBtn">📋 Copy Key &amp; Continue</button><a href="${target}">Open requested page</a><p class="hint">Valid for ~${remainLabel} · works on every /title page</p></main><script>document.getElementById('copyBtn').addEventListener('click',async()=>{const field=document.getElementById('k');try{await navigator.clipboard.writeText(field.value);location.href='/key-copied/${encodeURIComponent(token)}';}catch(e){field.select();alert('Select and copy the key, then open the page.');}});</script></body></html>`);
      return;
    }
    if (pathname.startsWith('/key-copied/') && req.method === 'GET') {
      const token = pathname.slice('/key-copied/'.length);
      const now = Math.floor(Date.now()/1000);
      const rec = await db.collection('access_keys').findOne({ token, expires_at: { $gt: now } });
      if (!rec) { sendJson(res, 410, { error: 'Key expired' }); return; }
      res.statusCode = 303; res.setHeader('Set-Cookie', `site_access=${encodeURIComponent(token)}; Max-Age=${Math.max(1,rec.expires_at-now)}; Path=/; HttpOnly; Secure; SameSite=Lax`); res.setHeader('Location', rec.target); res.end(); return;
    }
    if (pathname === '/verify-key' && req.method === 'POST') {
      const form = await readFormBody(req); const target = form.get('target') || ''; const key = form.get('key') || '';
      if (!isProtectedTitlePath(target)) { sendJson(res, 400, { error: 'Invalid title target' }); return; }
      const now = Math.floor(Date.now()/1000);
      // Site-wide: one valid key unlocks every title page until expiry
      const rec = await db.collection('access_keys').findOne({ key, expires_at: { $gt: now } });
      if (!rec) { sendKeyPage(res, target, 'Invalid or expired key. Use Get Key to generate a new one.'); return; }
      res.statusCode = 303;
      res.setHeader('Set-Cookie', `site_access=${encodeURIComponent(rec.token)}; Max-Age=${Math.max(1, rec.expires_at - now)}; Path=/; HttpOnly; Secure; SameSite=Lax`);
      res.setHeader('Location', target);
      res.end();
      return;
    }

    // Gate only GET/HEAD title routes. Homepage, search, watch, dl, stream and all APIs bypass.
    // One valid site_access cookie unlocks ALL /title/* pages for the configured expiry window.
    if (isProtectedTitlePath(pathname) && ['GET', 'HEAD'].includes(req.method)) {
      const cfg = await loadShortenerConfig(db);
      if (isShortenerEnabled(cfg)) {
        const target = pathname + (search || '');
        const token = parseCookies(req).site_access || '';
        const now = Math.floor(Date.now() / 1000);
        let authorized = false;
        if (token) {
          // Check access_keys in active DB and alternates (bot may write to another name)
          authorized = Boolean(await db.collection('access_keys').findOne({ token, expires_at: { $gt: now } }));
          if (!authorized) {
            try {
              const client = await getMongoClient();
              for (const name of dbCandidateNames()) {
                if (resolvedDbName && name === resolvedDbName) continue;
                const rec = await client.db(name).collection('access_keys').findOne({ token, expires_at: { $gt: now } });
                if (rec) { authorized = true; break; }
              }
            } catch {}
          }
        }
        if (!authorized) {
          sendKeyPage(res, target);
          return;
        }
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
            { name: 'Series', slug: 'series' },
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

      
      if (pathname === '/api/tmdb-backdrop' && req.method === 'GET') {
        const id = String(url.searchParams.get('id') || '').trim();
        const titleParam = String(url.searchParams.get('title') || '').trim();
        const slugParam = String(url.searchParams.get('slug') || '').trim();
        const mediaType = String(url.searchParams.get('type') || 'movie').toLowerCase() === 'tv' ? 'tv' : 'movie';
        if ((!id || !/^\d+$/.test(id)) && !titleParam) return json(res, 400, { error: 'id or title required' });
        const tmdbKey = String(process.env.TMDB_API_KEY || state._raw_map?.tmdb_api_key || '').trim();
        if (!tmdbKey) return json(res, 503, { error: 'TMDB not configured' });
        try {
          if (slugParam) {
            const bySlug = await db.collection('catalog_pages').findOne({ slug: slugParam }, { projection: { backdrop_path: 1, poster_path: 1, tmdb_id: 1 } });
            if (bySlug && bySlug.backdrop_path) {
              return json(res, 200, { backdrop_path: bySlug.backdrop_path, poster_path: bySlug.poster_path || '', tmdb_id: bySlug.tmdb_id || null });
            }
          }
          if (id && /^\d+$/.test(id)) {
            const idNum = Number(id);
            const idQuery = { $or: [{ tmdb_id: idNum }, { tmdb_id: String(idNum) }, { tmdb_id: id }] };
            const page = await db.collection('catalog_pages').findOne(idQuery, { projection: { backdrop_path: 1, poster_path: 1 } });
            if (page && page.backdrop_path) {
              return json(res, 200, { backdrop_path: page.backdrop_path, poster_path: page.poster_path || '', tmdb_id: idNum });
            }
          }
          let data = null;
          let resolvedId = (id && /^\d+$/.test(id)) ? Number(id) : null;
          if (resolvedId) {
            let r = await fetch(
              'https://api.themoviedb.org/3/' + mediaType + '/' + resolvedId + '?api_key=' + encodeURIComponent(tmdbKey) + '&language=en-US',
              { signal: AbortSignal.timeout(10000) }
            );
            if (!r.ok) {
              const other = mediaType === 'tv' ? 'movie' : 'tv';
              r = await fetch(
                'https://api.themoviedb.org/3/' + other + '/' + resolvedId + '?api_key=' + encodeURIComponent(tmdbKey) + '&language=en-US',
                { signal: AbortSignal.timeout(10000) }
              );
            }
            if (r.ok) data = await r.json();
          } else if (titleParam) {
            const cleanTitle = titleParam.replace(/\s*[\(\[](?:19|20)\d{2}[\)\]]/g, '').trim();
            const sr = await fetch(
              'https://api.themoviedb.org/3/search/multi?api_key=' + encodeURIComponent(tmdbKey) + '&query=' + encodeURIComponent(cleanTitle || titleParam) + '&language=en-US',
              { signal: AbortSignal.timeout(10000) }
            );
            if (sr.ok) {
              const sj = await sr.json();
              const results = (Array.isArray(sj.results) ? sj.results : []).filter(x => x && (x.media_type === 'movie' || x.media_type === 'tv'));
              const hit = results.find(x => x.backdrop_path) || results[0];
              if (hit) { data = hit; resolvedId = hit.id || null; }
            }
          }
          if (!data) return json(res, 502, { error: 'TMDB fetch failed' });
          let backdrop = data.backdrop_path || '';
          try {
            const imgRes = await fetch('https://api.themoviedb.org/3/' + (data.media_type === 'tv' || mediaType === 'tv' ? 'tv' : 'movie') + '/' + (resolvedId || id) + '/images?api_key=' + encodeURIComponent(tmdbKey) + '&include_image_language=en,hi,ja,ko,te,ta,null', { signal: AbortSignal.timeout(5000) });
            if (imgRes.ok) {
              const imgJson = await imgRes.json();
              const bds = Array.isArray(imgJson.backdrops) ? imgJson.backdrops : [];
              const titled = bds.find(b => b && b.file_path && b.iso_639_1 === 'en') || bds.find(b => b && b.file_path && b.iso_639_1);
              if (titled && titled.file_path) backdrop = titled.file_path;
            }
          } catch (_) {}
          const poster = data.poster_path || '';
          if (backdrop || poster || resolvedId) {
            const patch = { updated_at: new Date() };
            if (backdrop) patch.backdrop_path = backdrop;
            if (poster) patch.poster_path = poster;
            if (resolvedId) patch.tmdb_id = resolvedId;
            if (slugParam) {
              await db.collection('catalog_pages').updateOne({ slug: slugParam }, { $set: patch }).catch(() => {});
            } else if (resolvedId) {
              await db.collection('catalog_pages').updateMany({ $or: [{ tmdb_id: resolvedId }, { tmdb_id: String(resolvedId) }] }, { $set: patch }).catch(() => {});
            }
          }
          return json(res, 200, { backdrop_path: backdrop, poster_path: poster, tmdb_id: resolvedId });
        } catch (e) {
          return json(res, 502, { error: 'TMDB error' });
        }
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
          backdrop_path: x.backdrop_path || '',
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
          backdrop_path: String(body.backdrop_path || ''),
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
            backdrop_path: String(body.backdrop_path || ''),
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
        db.collection('catalog_pages').find({ published: true }, { projection: { _id: 0, overview: 0 } }).sort({ created_at: -1 }).limit(1000).toArray(),
        db.collection('catalog_categories').find({}, { projection: { _id: 0 } }).sort({ name: 1 }).toArray(),
      ]);
      const initialItems = rawItems.map((item) => { const s = serializeCatalogDoc(item, cloudflareUrl); delete s.groups; return s; });
      const initialCategories = rawCats.length
        ? rawCats.map((c) => ({ name: c.name, slug: c.slug }))
        : [
            { name: 'Movies', slug: 'movies' },
            { name: 'Series', slug: 'series' },
          ];
      sendHtml(res, 200, renderIndexHtml(initialItems, initialCategories, searchParams.get('q') || ''), 'public, s-maxage=30, stale-while-revalidate=180');
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
        db.collection('catalog_pages').find(query, { projection: { _id: 0 } }).sort({ created_at: -1 }).limit(1000).toArray(),
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
      // Final gate: never serve title HTML when shortener is enabled without a valid key
      {
        const cfg = await loadShortenerConfig(db);
        if (isShortenerEnabled(cfg)) {
          const target = pathname + (search || '');
          const token = parseCookies(req).site_access || '';
          const now = Math.floor(Date.now() / 1000);
          let authorized = false;
          if (token) {
            authorized = Boolean(await db.collection('access_keys').findOne({ token, expires_at: { $gt: now } }));
            if (!authorized) {
              try {
                const client = await getMongoClient();
                for (const name of dbCandidateNames()) {
                  if (resolvedDbName && name === resolvedDbName) continue;
                  const rec = await client.db(name).collection('access_keys').findOne({ token, expires_at: { $gt: now } });
                  if (rec) { authorized = true; break; }
                }
              } catch {}
            }
          }
          if (!authorized) {
            sendKeyPage(res, target);
            return;
          }
        }
      }
      let slug = decodeURIComponent(pathname.slice('/title/'.length));
      if (slug.endsWith('/')) slug = slug.slice(0, -1);
      let rawPage = await db.collection('catalog_pages').findOne({ slug, published: true }, { projection: { _id: 0 } });
      if (!rawPage) {
        // case-insensitive / alternate DB fallback for the page itself
        rawPage = await db.collection('catalog_pages').findOne(
          { slug: { $regex: `^${slug.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`, $options: 'i' }, published: true },
          { projection: { _id: 0 } },
        );
      }
      if (!rawPage) {
        try {
          const client = await getMongoClient();
          for (const name of dbCandidateNames()) {
            if (resolvedDbName && name === resolvedDbName) continue;
            rawPage = await client.db(name).collection('catalog_pages').findOne({ slug, published: true }, { projection: { _id: 0 } });
            if (rawPage) break;
          }
        } catch {}
      }
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
      const settingsState = await getSettingsState(db, req);
      const fsUser = (settingsState.filestore_bot_username || '').trim().replace(/^@+/, '');
      sendHtml(res, 200, renderTitleHtml(page, cloudflareUrl, fsUser));
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

    // Same-origin media proxy (do NOT 307 to ngrok — browser hits interstitial HTML and movi-player fails).
    // URL structure stays /stream|/dl|/remux|/api/tracks — body is proxied from backend with skip header.
    if (
      pathname.startsWith('/stream/') ||
      pathname.startsWith('/dl/') ||
      pathname.startsWith('/remux/') ||
      pathname.startsWith('/api/tracks/')
    ) {
      if (req.method === 'OPTIONS') {
        res.statusCode = 204;
        res.setHeader('Access-Control-Allow-Origin', '*');
        res.setHeader('Access-Control-Allow-Methods', 'GET, HEAD, OPTIONS');
        res.setHeader('Access-Control-Allow-Headers', 'Range, Content-Type, Accept, Origin, ngrok-skip-browser-warning');
        res.setHeader('Access-Control-Expose-Headers', 'Content-Length, Content-Range, Accept-Ranges, Content-Disposition, Content-Type');
        res.setHeader('Access-Control-Max-Age', '86400');
        res.end();
        return;
      }
      if (!cloudflareUrl || isVercelUrl(cloudflareUrl)) {
        sendJson(res, 503, {
          error: 'ngrok tunnel URL is not configured yet',
          message: 'Wait a few seconds for the VPS ngrok tunnel to auto-sync to MongoDB, or set it via /baseurl.',
        });
        return;
      }
      const targetUrl = new URL(`${cloudflareUrl}${requestPath}`);
      const transport = targetUrl.protocol === 'https:' ? https : http;
      const headers = {
        accept: req.headers.accept || '*/*',
        'user-agent': req.headers['user-agent'] || 'HITLER YT-Vercel-Gateway',
        host: targetUrl.host,
        'x-forwarded-host': req.headers.host || '',
        'x-forwarded-proto': 'https',
        'ngrok-skip-browser-warning': '1',
      };
      if (req.headers.range) headers.range = req.headers.range;
      if (req.headers['if-range']) headers['if-range'] = req.headers['if-range'];
      if (req.headers['content-type']) headers['content-type'] = req.headers['content-type'];

      const clientReq = transport.request(targetUrl, { method: req.method === 'HEAD' ? 'HEAD' : 'GET', headers }, (backendRes) => {
        const responseHeaders = { ...backendRes.headers };
        const status = backendRes.statusCode || 502;
        const ctype = String(responseHeaders['content-type'] || responseHeaders['Content-Type'] || '').toLowerCase();
        const ngrokErr = responseHeaders['ngrok-error-code'] || responseHeaders['Ngrok-Error-Code'] || '';
        // Never pipe ngrok interstitial / error HTML into the video player
        if (ngrokErr || status === 403 || status === 429 || (ctype.includes('text/html') && status >= 400)) {
          backendRes.resume();
          const msg = ngrokErr === 'ERR_NGROK_725'
            ? 'VPS tunnel bandwidth limit reached (ngrok). Upgrade ngrok plan or wait for monthly reset, then streams will work again.'
            : (ngrokErr
              ? ('Media tunnel error: ' + ngrokErr + '. Check VPS ngrok is online and not rate-limited.')
              : 'Media backend returned an error page instead of video. Check VPS/ngrok tunnel.');
          sendJson(res, status === 403 || status === 429 ? status : 502, {
            error: 'Media backend unavailable',
            message: msg,
            ngrok_error: ngrokErr || null,
          });
          return;
        }
        // Normalize CORS for browser / movi-player
        responseHeaders['access-control-allow-origin'] = '*';
        responseHeaders['access-control-expose-headers'] = 'Content-Length, Content-Range, Accept-Ranges, Content-Disposition, Content-Type';
        responseHeaders['access-control-allow-methods'] = 'GET, HEAD, OPTIONS';
        responseHeaders['cache-control'] = 'no-store, no-cache, must-revalidate, max-age=0';
        delete responseHeaders['content-encoding'];
        delete responseHeaders['transfer-encoding'];
        // Avoid redirect loops if backend somehow redirects
        delete responseHeaders['location'];
        res.writeHead(status, responseHeaders);
        if (req.method === 'HEAD') {
          res.end();
          backendRes.resume();
          return;
        }
        backendRes.pipe(res);
      });
      clientReq.on('error', (error) => {
        if (!res.headersSent) {
          sendJson(res, 502, {
            error: 'Bad Gateway',
            message: 'Could not connect to media backend.',
            detail: String(error?.message || error),
          });
        } else {
          res.destroy(error);
        }
      });
      clientReq.end();
      return;
    }

    if ((req.method === 'GET' || req.method === 'HEAD') && pathname.startsWith('/watch/')) {
      // Pathname from getRequestPath has no query; short code only.
      // Button URLs from the bot are already full /watch/<code> links — we only need the code.
      let shortCodeRaw = pathname.slice('/watch/'.length);
      if (shortCodeRaw.includes('/')) shortCodeRaw = shortCodeRaw.split('/')[0];
      // Strip accidental query fragments if any proxy left them on path
      shortCodeRaw = shortCodeRaw.split('?')[0].split('#')[0];
      let shortCode = '';
      try { shortCode = decodeURIComponent(shortCodeRaw); } catch { shortCode = shortCodeRaw; }
      shortCode = String(shortCode || '').trim();
      if (!shortCode) {
        sendJson(res, 404, { detail: 'Link not found' });
        return;
      }
      const fileData = await db.collection('files').findOne({ short_code: shortCode });
      if (!fileData) {
        sendJson(res, 404, { detail: 'Link not found' });
        return;
      }
      if (fileData.external_url) {
        try {
          let pr = await fetch(fileData.external_url, { method: 'HEAD', redirect: 'follow', signal: AbortSignal.timeout(6000) });
          if (pr.status === 403 || pr.status === 405 || pr.status === 501) {
            pr = await fetch(fileData.external_url, { method: 'GET', headers: { Range: 'bytes=0-0' }, redirect: 'follow', signal: AbortSignal.timeout(6000) });
          }
          if (!pr.ok) {
            sendHtml(res, 410, renderBaseLayout({
              title: 'Watch Link Dead',
              content: '<div class="page-hero"><h1>Watch Link Dead</h1><p>The source download link for this file is offline or removed (HTTP ' + pr.status + ').</p></div>',
            }));
            return;
          }
        } catch (_) {
          sendHtml(res, 410, renderBaseLayout({
            title: 'Watch Link Dead',
            content: '<div class="page-hero"><h1>Watch Link Dead</h1><p>The source download link for this file is unreachable.</p></div>',
          }));
          return;
        }
      }

      // Title context for prev/next, switch, related groups (works on Vercel website)
      // Use searchParams from getRequestPath — NOT a missing `url` variable.
      let titleSlug = String(searchParams.get('title') || '').trim();
      let groupIndex = parseInt(searchParams.get('g') || '0', 10);
      let buttonIndex = parseInt(searchParams.get('b') || '0', 10);
      if (!Number.isFinite(groupIndex) || groupIndex < 0) groupIndex = 0;
      if (!Number.isFinite(buttonIndex) || buttonIndex < 0) buttonIndex = 0;

      let watchGroups = [];
      let posterUrl = '';
      // Resolve title page: explicit ?title=slug, else find by short_code in watch button URLs
      let resolvedPage = null;
      try {
        if (titleSlug) {
          resolvedPage = await db.collection('catalog_pages').findOne({ slug: titleSlug, published: true });
        }
        if (!resolvedPage && shortCode) {
          // Match any catalog page whose watch button URL contains this short code
          const codeRe = shortCode.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
          resolvedPage = await db.collection('catalog_pages').findOne({
            published: true,
            'groups.buttons.url': { $regex: codeRe, $options: 'i' },
          });
          if (!resolvedPage) {
            // Broader scan (limited) for rewritten / absolute URLs
            const candidates = await db.collection('catalog_pages').find(
              { published: true, 'groups.0': { $exists: true } },
              { projection: { slug: 1, groups: 1, poster_path: 1, title: 1 } }
            ).limit(1000).toArray();
            for (const cand of candidates) {
              const groups = Array.isArray(cand.groups) ? cand.groups : [];
              const hit = groups.some((g) => (g.buttons || []).some((b) => String((b && b.url) || '').includes(shortCode)));
              if (hit) { resolvedPage = cand; break; }
            }
          }
        }
        if (resolvedPage) {
          const page = serializeCatalogDoc(resolvedPage, cloudflareUrl || '');
          if (!titleSlug && page.slug) titleSlug = String(page.slug);
          posterUrl = pagePosterPath(page.poster_path || '');
          const allGroups = Array.isArray(page.groups) ? page.groups : [];
          for (const g of allGroups) {
            const box = String((g && g.box) || 'watch').trim().toLowerCase();
            // Treat missing/empty box as watch (same as title page)
            if (box && box !== 'watch') continue;
            const buttons = [];
            for (const btn of (g && g.buttons) || []) {
              if (!btn || typeof btn !== 'object') continue;
              const u = String(btn.url || btn.link || '').trim();
              const name = String(btn.name || btn.text || 'Watch').trim() || 'Watch';
              if (u) buttons.push({ name, url: u });
            }
            if (buttons.length) {
              watchGroups.push({
                name: String((g && g.name) || 'Watch Group').trim() || 'Watch Group',
                buttons,
              });
            }
          }
          if (groupIndex >= watchGroups.length) groupIndex = 0;
          if (watchGroups[groupIndex] && buttonIndex >= (watchGroups[groupIndex].buttons || []).length) {
            buttonIndex = 0;
          }
        }
      } catch (e) {
        console.warn('watch title context failed', e);
      }

      const fsUser = String(state.filestore_bot_username || '').replace(/^@+/, '');
      // Render watch HTML on Vercel; stream/dl still use cloudflare/backend origin in the page.
      sendHtml(
        res,
        200,
        renderWatchHtml(fileData, state, cloudflareUrl, {
          titleSlug,
          groupIndex,
          buttonIndex,
          watchGroups,
          posterUrl,
          filestoreUsername: fsUser,
        }),
        'public, s-maxage=30, stale-while-revalidate=180',
      );
      return;
    }

    // Media proxy: same-origin /stream|/dl|/remux for movi-player multi-audio (DDStream pattern)
    const isMediaProxy = /^\/(stream|dl|remux)\//.test(pathname);
    if (req.method === 'OPTIONS' && isMediaProxy) {
      res.statusCode = 204;
      res.setHeader('Access-Control-Allow-Origin', '*');
      res.setHeader('Access-Control-Allow-Methods', 'GET, HEAD, OPTIONS');
      res.setHeader('Access-Control-Allow-Headers', 'Range, Content-Type, Accept, Origin, ngrok-skip-browser-warning');
      res.setHeader('Access-Control-Expose-Headers', 'Content-Length, Content-Range, Accept-Ranges, Content-Disposition, Content-Type');
      res.setHeader('Access-Control-Max-Age', '86400');
      res.end();
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
      'user-agent': req.headers['user-agent'] || 'HITLER YT-Vercel-Gateway',
      host: targetUrl.host,
      'x-forwarded-host': req.headers.host || '',
      'x-forwarded-proto': 'https',
      // Skip ngrok free browser interstitial for media fetches
      'ngrok-skip-browser-warning': '1',
    };
    if (req.headers.authorization) headers.authorization = req.headers.authorization;
    if (req.headers['content-type']) headers['content-type'] = req.headers['content-type'];
    if (req.headers.range) headers.range = req.headers.range;

    // Long timeout for media; streaming can take a while
    const mediaTimeout = isMediaProxy ? 0 : 25000;

    const clientReq = transport.request(targetUrl, { method: req.method, headers }, (backendRes) => {
      const responseHeaders = { ...backendRes.headers };
      responseHeaders['cache-control'] = 'no-store, no-cache, must-revalidate, max-age=0';
      responseHeaders['access-control-allow-origin'] = '*';
      responseHeaders['access-control-expose-headers'] = 'Content-Length, Content-Range, Accept-Ranges, Content-Disposition, Content-Type';
      responseHeaders['access-control-allow-methods'] = 'GET, HEAD, OPTIONS';
      // Avoid compressed body issues with range streaming
      delete responseHeaders['content-encoding'];
      res.writeHead(backendRes.statusCode || 502, responseHeaders);
      backendRes.pipe(res);
    });
    if (mediaTimeout > 0) {
      clientReq.setTimeout(mediaTimeout, () => clientReq.destroy(new Error('Backend request timed out')));
    }
    clientReq.on('error', (error) => {
      if (!res.headersSent) {
        sendJson(res, 502, {
          error: 'Bad Gateway',
          message: 'Could not connect to the ngrok tunnel backend.',
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
