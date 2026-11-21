from __future__ import annotations
import os, re, secrets
from datetime import datetime, timezone
from urllib.parse import urlparse
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from app.database.connection import (
    db,
    settings,
    settings_col,
    get_active_cloudflare_url,
    get_active_website_url,
)
from app.admin.auth import require_panel_admin
from app.utils.helpers import (
    is_trycloudflare_url,
    normalize_public_base_url,
    rewrite_page_groups_to_cloudflare,
    extract_watch_short_code,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(__import__('pathlib').Path(__file__).resolve().parent / 'templates'))
pages = db.catalog_pages
categories = db.catalog_categories
site_settings = db.site_settings


class SettingsUpdate(BaseModel):
    downloads_enabled: bool | None = None
    streaming_enabled: bool | None = None
    built_in_player_enabled: bool | None = None
    auto_player_redirect: bool | None = None
    access_mode: str | None = Field(default=None, pattern='^(all|admins|owners)$')
    backend_url: str | None = Field(default=None, max_length=500)
    base_url: str | None = Field(default=None, max_length=500)
    cloudflare_url: str | None = Field(default=None, max_length=500)
    filestore_bot_username: str | None = Field(default=None, max_length=64)


class CategoryInput(BaseModel):
    name: str = Field(min_length=1, max_length=50)


class PageInput(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    overview: str = Field(default='', max_length=10000)
    poster_path: str = ''
    backdrop_path: str = ''
    release_date: str = ''
    rating: float | None = None
    media_type: str = 'movie'
    category_ids: list[str] = []
    groups: list[dict] = []
    tmdb_id: int | None = None
    imdb_id: str = ''
    genres: list[str] = []
    runtime: int | None = None

    @field_validator('groups', mode='before')
    @classmethod
    def validate_groups(cls, groups):
        if not isinstance(groups, list):
            return []
        normalized_groups = []
        watch_idx = 0
        dl_idx = 0
        for group in groups:
            if not isinstance(group, dict):
                continue
            box = str(group.get('box') or 'watch').strip().lower()
            if box not in ('watch', 'download'):
                box = 'watch'
            if box == 'watch':
                watch_idx += 1
                default_gname = f'Watch Group {watch_idx}'
            else:
                dl_idx += 1
                default_gname = f'Download Group {dl_idx}'
            gname = str(group.get('name') or '').strip() or default_gname
            raw_buttons = group.get('buttons') if isinstance(group.get('buttons'), list) else []
            clean_buttons = []
            for b_i, button in enumerate(raw_buttons):
                if not isinstance(button, dict):
                    continue
                bname = str(button.get('name') or '').strip()
                target = str(button.get('url') or '').strip()
                if not bname and not target:
                    continue
                if not bname:
                    bname = f"{'Watch' if box == 'watch' else 'Download'} {b_i + 1}"
                if target and not target.startswith(('http://', 'https://', '/')):
                    target = 'https://' + target
                if target:
                    parsed = urlparse(target)
                    if parsed.scheme and parsed.scheme not in ('http', 'https'):
                        raise ValueError(f'Invalid URL scheme in button "{bname}"')
                    clean_buttons.append({'name': bname, 'url': target})
            normalized_groups.append({'box': box, 'name': gname, 'buttons': clean_buttons})
        return normalized_groups


async def admin_auth(_: bool = Depends(require_panel_admin)):
    return True


def serialize_catalog_doc(doc: dict, cloudflare_base: str | None = None) -> dict:
    """Safely serialize a MongoDB catalog_pages document for JSON/templates and rewrite all button URLs to Cloudflare."""
    if not isinstance(doc, dict):
        return {}
    out = {}
    for k, v in doc.items():
        if k == '_id':
            continue
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, float) and (v != v or v in (float('inf'), float('-inf'))):
            out[k] = None
        else:
            out[k] = v
    out['groups'] = rewrite_page_groups_to_cloudflare(out.get('groups', []), cloudflare_base)
    return out


async def rewrite_all_stored_catalog_buttons(cloudflare_base: str | None) -> int:
    """Update all stored button URLs in catalog_pages so their domain uses the active ngrok URL."""
    if not cloudflare_base:
        return 0
    try:
        cf_base = normalize_public_base_url(cloudflare_base)
    except ValueError:
        return 0
    updated_count = 0
    cursor = pages.find({'groups.0': {'$exists': True}}, {'_id': 1, 'slug': 1, 'groups': 1})
    async for doc in cursor:
        old_groups = doc.get('groups') or []
        new_groups = rewrite_page_groups_to_cloudflare(old_groups, cf_base)
        if new_groups != old_groups:
            await pages.update_one(
                {'_id': doc['_id']},
                {'$set': {'groups': new_groups, 'updated_at': datetime.now(timezone.utc)}},
            )
            updated_count += 1
    return updated_count


async def get_settings():
    doc = await site_settings.find_one({'_id': 'runtime'}) or {}
    rows = await settings_col.find(
        {'key': {'$in': ['base_url', 'backend_url', 'ngrok_url', 'cloudflare_url', 'vercel_url', 'website_urls']}},
        {'key': 1, 'value': 1},
    ).to_list(10)
    urls = {x['key']: x.get('value', '') for x in rows if isinstance(x, dict) and 'key' in x}
    cf_url = await get_active_cloudflare_url() or ''
    web_url = await get_active_website_url() or ''
    website_urls = urls.get('website_urls', [])
    if not isinstance(website_urls, list):
        website_urls = [str(website_urls)] if website_urls else []
    website_urls = [u for u in website_urls if not is_trycloudflare_url(str(u))]
    fs_user = (doc.get('filestore_bot_username') or '').strip().lstrip('@')
    return {
        'downloads_enabled': doc.get('downloads_enabled', True),
        'streaming_enabled': doc.get('streaming_enabled', True),
        'built_in_player_enabled': doc.get('built_in_player_enabled', True),
        'auto_player_redirect': doc.get('auto_player_redirect', False),
        'access_mode': doc.get('access_mode', 'all'),
        'filestore_bot_username': fs_user,
        'base_url': cf_url or urls.get('base_url') or settings.BASE_URL,
        'backend_url': cf_url or urls.get('backend_url') or (settings.BACKEND_URL or ''),
        'ngrok_url': cf_url,
        'cloudflare_url': cf_url,
        'vercel_url': urls.get('vercel_url') or os.getenv('WEBSITE_URL', '') or (web_url if not is_trycloudflare_url(web_url) else ''),
        'website_urls': website_urls,
    }



async def backfill_tmdb_backdrops(limit: int = 80) -> int:
    """On startup: fetch TMDB landscape backdrops for catalog pages missing them."""
    key = getattr(settings, 'TMDB_API_KEY', None) or ''
    if not key:
        try:
            doc = await settings_col.find_one({'key': 'tmdb_api_key'}, {'value': 1})
            key = (doc or {}).get('value') or ''
        except Exception:
            key = ''
    if not key:
        return 0
    try:
        cursor = pages.find(
            {
                'tmdb_id': {'$exists': True, '$ne': None},
                '$or': [
                    {'backdrop_path': {'$exists': False}},
                    {'backdrop_path': None},
                    {'backdrop_path': ''},
                ],
            },
            {'tmdb_id': 1, 'media_type': 1, 'title': 1},
        ).limit(limit)
        rows = await cursor.to_list(limit)
    except Exception:
        return 0
    if not rows:
        return 0
    updated = 0
    import logging
    log = logging.getLogger(__name__)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            for row in rows:
                tid = row.get('tmdb_id')
                if not tid:
                    continue
                mt = str(row.get('media_type') or 'movie').lower()
                media_type = 'tv' if mt in ('tv', 'series', 'web-series') else 'movie'
                try:
                    r = await client.get(
                        f'https://api.themoviedb.org/3/{media_type}/{int(tid)}',
                        params={'api_key': key, 'language': 'en-US'},
                    )
                    if r.status_code != 200:
                        # try the other type once
                        other = 'movie' if media_type == 'tv' else 'tv'
                        r = await client.get(
                            f'https://api.themoviedb.org/3/{other}/{int(tid)}',
                            params={'api_key': key, 'language': 'en-US'},
                        )
                    if r.status_code != 200:
                        continue
                    data = r.json()
                    backdrop = data.get('backdrop_path') or ''
                    poster = data.get('poster_path') or ''
                    if not backdrop and not poster:
                        continue
                    patch = {'updated_at': datetime.now(timezone.utc)}
                    if backdrop:
                        patch['backdrop_path'] = backdrop
                    if poster:
                        # only fill poster if missing
                        existing = await pages.find_one({'tmdb_id': tid}, {'poster_path': 1})
                        if not (existing or {}).get('poster_path'):
                            patch['poster_path'] = poster
                    await pages.update_many({'tmdb_id': tid}, {'$set': patch})
                    updated += 1
                except Exception as e:
                    log.debug('backdrop backfill %s: %s', tid, e)
                    continue
    except Exception as e:
        log.warning('backfill_tmdb_backdrops failed: %s', e)
        return updated
    if updated:
        log.info('TMDB landscape backfill: updated %s pages', updated)
    return updated


async def seed_categories():
    await pages.create_index('slug', unique=True)
    await categories.create_index('slug', unique=True)
    defaults = [
        ('Movies', 'movies'),
        ('Series', 'series'),
    ]
    for name, slug in defaults:
        await categories.update_one(
            {'slug': slug},
            {'$setOnInsert': {'name': name, 'slug': slug, 'created_at': datetime.now(timezone.utc)}},
            upsert=True,
        )


@router.get('/api/site-settings')
async def public_settings():
    return await get_settings()


@router.get('/api/categories')
async def public_categories():
    try:
        raw = await categories.find({}, {'_id': 0}).sort('name', 1).to_list(None)
        items = []
        for c in raw:
            item = dict(c)
            if isinstance(item.get('created_at'), datetime):
                item['created_at'] = item['created_at'].isoformat()
            items.append(item)
        if not items:
            items = [
                {'name': 'Movies', 'slug': 'movies'},
                {'name': 'Series', 'slug': 'series'},
            ]
        return {'items': items}
    except Exception:
        return {
            'items': [
                {'name': 'Movies', 'slug': 'movies'},
                {'name': 'Series', 'slug': 'series'},
            ]
        }


@router.get('/api/catalog')
async def catalog(category: str | None = None, q: str | None = None):
    query: dict = {'published': True}
    if category and category.strip():
        query['category_slugs'] = category.strip()[:60]
    if q and q.strip():
        safe = re.escape(q.strip()[:100])
        query['$or'] = [
            {'title': {'$regex': safe, '$options': 'i'}},
            {'overview': {'$regex': safe, '$options': 'i'}},
            {'genres': {'$regex': safe, '$options': 'i'}},
        ]
    try:
        cf_base = await get_active_cloudflare_url()
        rows = await pages.find(query, {'_id': 0}).sort('created_at', -1).to_list(100)
        items = [serialize_catalog_doc(row, cf_base) for row in rows]
        return {'items': items, 'cloudflare_url': cf_base or ''}
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception('Catalog API query failed')
        raise HTTPException(status_code=503, detail=f'Catalog database unavailable: {type(exc).__name__}') from exc


@router.get('/api/public/title/{slug}')
async def public_title(slug: str):
    cf_base = await get_active_cloudflare_url()
    page = await pages.find_one({'slug': slug, 'published': True}, {'_id': 0})
    if not page:
        raise HTTPException(404, 'Title not found')
    return serialize_catalog_doc(page, cf_base)


@router.get('/search', response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str = '',
    type: str = '',
    year: str = '',
    genre: str = '',
    category: str = '',
):
    q = (q or '').strip()[:100]
    type_filter = type if type in ('', 'movie', 'tv') else ''
    year = (year or '').strip()[:4]
    genre = (genre or '').strip()[:40]
    category = (category or '').strip()[:40]

    query: dict = {'published': True}
    if type_filter:
        if type_filter == 'tv':
            query['media_type'] = {'$in': ['tv', 'series', 'web-series']}
        else:
            query['media_type'] = 'movie'
    if category:
        query['category_slugs'] = category
    if year and year.isdigit():
        query['release_date'] = {'$regex': f'^{year}'}
    if genre:
        query['genres'] = {'$regex': re.escape(genre), '$options': 'i'}
    if q:
        safe = re.escape(q)
        query['$or'] = [
            {'title': {'$regex': safe, '$options': 'i'}},
            {'overview': {'$regex': safe, '$options': 'i'}},
            {'genres': {'$regex': safe, '$options': 'i'}},
        ]

    cf_base = await get_active_cloudflare_url()
    raw_rows = await pages.find(query, {'_id': 0}).sort('created_at', -1).to_list(100)
    rows = [serialize_catalog_doc(r, cf_base) for r in raw_rows]
    cats = await categories.find({}, {'_id': 0}).sort('name', 1).to_list(None)

    all_genres: set[str] = set()
    async for doc in pages.find({'published': True, 'genres': {'$exists': True}}, {'genres': 1}):
        for g in (doc.get('genres') or []):
            if isinstance(g, str) and g.strip():
                all_genres.add(g.strip())

    return templates.TemplateResponse(
        request=request,
        name='search.html',
        context={
            'request': request,
            'results': rows,
            'query': q,
            'type_filter': type_filter,
            'year_filter': year,
            'genre_filter': genre,
            'category_filter': category,
            'categories': cats or [],
            'genres': sorted(all_genres),
        },
    )


def build_telegram_groups_from_watch(groups: list | None, filestore_username: str | None) -> list[dict]:
    """
    Mirror Watch-box groups into one Telegram box.
    Only /watch/<code> button URLs become FileStore deep links.
    Dedupes by short_code so the same media is not listed twice.
    """
    if not filestore_username or not groups:
        return []
    uname = str(filestore_username).strip().lstrip('@')
    if not uname:
        return []
    from app.utils.helpers import extract_watch_short_code, filestore_deep_link

    out = []
    seen_codes_global: set[str] = set()
    for g in groups:
        if not isinstance(g, dict):
            continue
        box = str(g.get('box') or 'watch').strip().lower()
        # Watch box only (legacy missing box counts as watch)
        if box not in ('watch', ''):
            continue
        buttons_out = []
        seen_in_group: set[str] = set()
        for b in (g.get('buttons') or []):
            if not isinstance(b, dict):
                continue
            bname = str(b.get('name') or '').strip()
            burl = str(b.get('url') or '').strip()
            if not bname or not burl:
                continue
            code = extract_watch_short_code(burl)
            if not code:
                continue
            if code in seen_in_group or code in seen_codes_global:
                continue
            seen_in_group.add(code)
            seen_codes_global.add(code)
            buttons_out.append({
                'name': bname,
                'url': filestore_deep_link(uname, code),
            })
        if buttons_out:
            gname = str(g.get('name') or '').strip() or 'Watch'
            out.append({
                'box': 'telegram',
                'name': gname,
                'buttons': buttons_out,
            })
    return out


@router.get('/title/{slug}', response_class=HTMLResponse)
async def title_page(request: Request, slug: str):
    raw_page = await pages.find_one({'slug': slug, 'published': True}, {'_id': 0})
    if not raw_page:
        raise HTTPException(404, 'Title not found')
    cf_base = await get_active_cloudflare_url() or settings.BASE_URL.rstrip('/')
    page = serialize_catalog_doc(raw_page, cf_base)
    # Aggregate persisted view counts from this title's Watch links only.
    watch_codes = set()
    for group in (page.get('groups') or []):
        if str((group or {}).get('box') or 'watch').strip().lower() not in ('watch', ''):
            continue
        for button in (group or {}).get('buttons') or []:
            code = extract_watch_short_code((button or {}).get('url'))
            if code:
                watch_codes.add(code)
    page['view_count'] = 0
    if watch_codes:
        rows = await db.files.find({'short_code': {'$in': list(watch_codes)}}, {'access_count': 1}).to_list(None)
        page['view_count'] = sum(int(row.get('access_count') or 0) for row in rows)
    settings_doc = await get_settings()
    fs_user = (settings_doc.get('filestore_bot_username') or '').strip().lstrip('@')
    telegram_groups = build_telegram_groups_from_watch(page.get('groups') or [], fs_user) if fs_user else []
    return templates.TemplateResponse(
        request=request,
        name='title.html',
        context={
            'request': request,
            'page': page,
            'cloudflare_url': cf_base,
            'filestore_bot_username': fs_user,
            'telegram_groups': telegram_groups,
        },
    )


@router.get('/panel', response_class=HTMLResponse)
@router.get('/admin/panel', response_class=HTMLResponse)
async def panel(request: Request, _: bool = Depends(admin_auth)):
    return templates.TemplateResponse(
        request=request,
        name='admin/panel.html',
        context={'request': request, 'settings': await get_settings()},
    )


@router.get('/api/admin/settings')
async def read_admin_settings(_: bool = Depends(admin_auth)):
    return await get_settings()


@router.put('/api/admin/settings')
async def update_settings(data: SettingsUpdate, _: bool = Depends(admin_auth)):
    values = data.model_dump(exclude_none=True)
    url_values = {key: values.pop(key) for key in ('base_url', 'backend_url', 'cloudflare_url') if key in values}
    if values:
        await site_settings.update_one({'_id': 'runtime'}, {'$set': values}, upsert=True)

    now = datetime.now(timezone.utc)
    cf_target: str | None = None
    for key, candidate in url_values.items():
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            candidate = normalize_public_base_url(candidate)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        await settings_col.update_one(
            {'key': key},
            {'$set': {'value': candidate, 'updated_at': now}},
            upsert=True,
        )
        if key == 'base_url':
            if not is_trycloudflare_url(candidate):
                await settings_col.update_one(
                    {'key': 'website_urls'},
                    {'$addToSet': {'value': candidate}, '$set': {'updated_at': now}},
                    upsert=True,
                )
            settings.BASE_URL = candidate
            os.environ['BASE_URL'] = candidate
            if is_trycloudflare_url(candidate):
                cf_target = candidate
                settings.BACKEND_URL = candidate
                await settings_col.update_one(
                    {'key': 'backend_url'},
                    {'$set': {'value': candidate, 'updated_at': now}},
                    upsert=True,
                )
                await settings_col.update_one(
                    {'key': 'cloudflare_url'},
                    {'$set': {'value': candidate, 'updated_at': now}},
                    upsert=True,
                )
        elif key in ('backend_url', 'cloudflare_url'):
            settings.BACKEND_URL = candidate
            cf_target = candidate
            await settings_col.update_one(
                {'key': 'backend_url'},
                {'$set': {'value': candidate, 'updated_at': now}},
                upsert=True,
            )
            await settings_col.update_one(
                {'key': 'cloudflare_url'},
                {'$set': {'value': candidate, 'updated_at': now}},
                upsert=True,
            )

    if cf_target:
        await rewrite_all_stored_catalog_buttons(cf_target)

    return await get_settings()


@router.get('/api/admin/categories')
async def list_categories(_: bool = Depends(admin_auth)):
    return await public_categories()


@router.post('/api/admin/categories')
async def add_category(data: CategoryInput, _: bool = Depends(admin_auth)):
    name = data.name.strip()
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    if not slug:
        raise HTTPException(400, 'Invalid category name')
    if await categories.find_one({'slug': slug}):
        raise HTTPException(409, 'Category already exists')
    await categories.insert_one({'name': name, 'slug': slug, 'created_at': datetime.now(timezone.utc)})
    return {'name': name, 'slug': slug}


@router.delete('/api/admin/categories/{slug}')
async def remove_category(slug: str, _: bool = Depends(admin_auth)):
    if slug in ('movies', 'web-series'):
        raise HTTPException(400, 'Default categories cannot be deleted')
    await categories.delete_one({'slug': slug})
    return {'ok': True}




@router.get('/api/tmdb-backdrop')
async def tmdb_backdrop(id: int, type: str = 'movie'):
    """Return TMDB backdrop for home landscape cards; persist onto matching catalog pages."""
    media_type = 'tv' if str(type).lower() == 'tv' else 'movie'
    # Prefer already stored
    id_query = {'$or': [{'tmdb_id': id}, {'tmdb_id': str(id)}, {'tmdb_id': int(id) if str(id).isdigit() else id}]}
    page = await pages.find_one(id_query, {'backdrop_path': 1, 'poster_path': 1})
    if page and page.get('backdrop_path'):
        return {'backdrop_path': page['backdrop_path'], 'poster_path': page.get('poster_path') or ''}
    key = settings.TMDB_API_KEY
    if not key:
        doc = await settings_col.find_one({'key': 'tmdb_api_key'}, {'value': 1})
        key = (doc or {}).get('value') or ''
    if not key:
        raise HTTPException(503, 'TMDB_API_KEY is not configured')
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f'https://api.themoviedb.org/3/{media_type}/{id}',
                params={'api_key': key, 'language': 'en-US'},
            )
            if r.status_code != 200:
                other = 'movie' if media_type == 'tv' else 'tv'
                r = await client.get(
                    f'https://api.themoviedb.org/3/{other}/{id}',
                    params={'api_key': key, 'language': 'en-US'},
                )
        if r.status_code != 200:
            raise HTTPException(502, 'TMDB fetch failed')
        data = r.json()
        backdrop = data.get('backdrop_path') or ''
        poster = data.get('poster_path') or ''
        if backdrop:
            await pages.update_many(
                id_query,
                {'$set': {'backdrop_path': backdrop, 'updated_at': datetime.now(timezone.utc)}},
            )
        return {'backdrop_path': backdrop, 'poster_path': poster}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, 'TMDB error')

@router.get('/api/admin/tmdb-search')
async def tmdb_search(q: str, media_type: str = 'multi', _: bool = Depends(admin_auth)):
    key = settings.TMDB_API_KEY
    if not key:
        doc = await settings_col.find_one({'key': 'tmdb_api_key'}, {'value': 1})
        key = str(doc.get('value', '')).strip() if doc else ''
    if not key:
        raise HTTPException(503, 'TMDB_API_KEY is not configured')
    if media_type not in ('movie', 'tv', 'multi'):
        raise HTTPException(400, 'Invalid media type')
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f'https://api.themoviedb.org/3/search/{media_type}',
            params={'api_key': key, 'query': q, 'include_adult': 'false'},
        )
        r.raise_for_status()
        payload = r.json()
    return {
        'items': [
            {
                'id': x['id'],
                'media_type': x.get('media_type', media_type if media_type != 'multi' else 'movie'),
                'title': x.get('title') or x.get('name'),
                'overview': x.get('overview', ''),
                'poster_path': x.get('poster_path'),
                'backdrop_path': x.get('backdrop_path'),
                'release_date': x.get('release_date') or x.get('first_air_date', ''),
                'rating': x.get('vote_average'),
            }
            for x in payload.get('results', [])[:20]
        ]
    }


@router.get('/api/admin/pages')
async def list_pages(q: str | None = None, _: bool = Depends(admin_auth)):
    query = {}
    if q and q.strip():
        query = {
            '$or': [
                {'title': {'$regex': re.escape(q.strip()[:100]), '$options': 'i'}},
                {'slug': {'$regex': re.escape(q.strip()[:100]), '$options': 'i'}},
            ]
        }
    cf_base = await get_active_cloudflare_url()
    rows = await pages.find(query, {'_id': 0}).sort('created_at', -1).to_list(300)
    return {'items': [serialize_catalog_doc(r, cf_base) for r in rows]}


@router.get('/api/admin/pages/{slug}')
async def get_page(slug: str, _: bool = Depends(admin_auth)):
    page = await pages.find_one({'slug': slug}, {'_id': 0})
    if not page:
        raise HTTPException(404, 'Page not found')
    cf_base = await get_active_cloudflare_url()
    return serialize_catalog_doc(page, cf_base)


@router.post('/api/admin/pages')
async def create_page(data: PageInput, _: bool = Depends(admin_auth)):
    slug = re.sub(r'[^a-z0-9]+', '-', data.title.lower()).strip('-') or secrets.token_hex(5)
    base = slug
    i = 2
    while await pages.find_one({'slug': slug}):
        slug = f'{base}-{i}'
        i += 1
    cats = await categories.find({'slug': {'$in': data.category_ids}}).to_list(100)
    cf_base = await get_active_cloudflare_url()
    doc = data.model_dump()
    doc['groups'] = rewrite_page_groups_to_cloudflare(doc.get('groups', []), cf_base)
    doc.update(
        {
            'slug': slug,
            'category_slugs': [x['slug'] for x in cats] if cats else [x for x in data.category_ids if x],
            'published': True,
            'created_at': datetime.now(timezone.utc),
        }
    )
    if data.tmdb_id and settings.TMDB_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                endpoint = 'tv' if data.media_type == 'tv' else 'movie'
                detail = await client.get(
                    f'https://api.themoviedb.org/3/{endpoint}/{data.tmdb_id}',
                    params={
                        'api_key': settings.TMDB_API_KEY,
                        'append_to_response': 'external_ids,credits,images,content_ratings,release_dates',
                        'language': 'en-US',
                    },
                )
                detail.raise_for_status()
                meta = detail.json()
            doc['genres'] = [g.get('name', '') for g in meta.get('genres', []) if g.get('name')]
            doc['imdb_id'] = (meta.get('external_ids') or {}).get('imdb_id') or meta.get('imdb_id', '')
            doc['runtime'] = meta.get('runtime') or (meta.get('episode_run_time') or [None])[0]
            doc['tagline'] = meta.get('tagline') or doc.get('tagline') or ''
            if meta.get('vote_average') is not None:
                doc['rating'] = round(float(meta['vote_average']), 1)
            if meta.get('vote_count') is not None:
                doc['vote_count'] = int(meta['vote_count'])
            if not doc.get('overview') and meta.get('overview'):
                doc['overview'] = meta.get('overview')
            if not doc.get('poster_path') and meta.get('poster_path'):
                doc['poster_path'] = meta['poster_path']
            if not doc.get('backdrop_path') and meta.get('backdrop_path'):
                doc['backdrop_path'] = meta['backdrop_path']
            if not doc.get('release_date'):
                doc['release_date'] = meta.get('release_date') or meta.get('first_air_date') or ''
            # Top cast (up to 8) for title page UI
            cast = (meta.get('credits') or {}).get('cast') or []
            if cast:
                doc['cast'] = [
                    {
                        'name': c.get('name') or '',
                        'character': c.get('character') or '',
                        'profile_path': c.get('profile_path') or '',
                    }
                    for c in cast[:8]
                    if c.get('name')
                ]
        except Exception:
            pass
    doc['updated_at'] = doc['created_at']
    await pages.insert_one(doc)
    web_base = (await get_active_website_url()) or settings.BASE_URL.rstrip('/')
    return {'slug': slug, 'url': f"{web_base.rstrip('/')}/title/{slug}"}


@router.put('/api/admin/pages/{slug}')
async def edit_page(slug: str, data: PageInput, _: bool = Depends(admin_auth)):
    cats = await categories.find({'slug': {'$in': data.category_ids}}).to_list(100)
    cf_base = await get_active_cloudflare_url()
    doc = data.model_dump()
    doc['groups'] = rewrite_page_groups_to_cloudflare(doc.get('groups', []), cf_base)
    doc['category_slugs'] = [x['slug'] for x in cats] if cats else [x for x in data.category_ids if x]
    doc['updated_at'] = datetime.now(timezone.utc)
    result = await pages.update_one({'slug': slug}, {'$set': doc})
    if not result.matched_count:
        raise HTTPException(404, 'Page not found')
    return {'ok': True, 'groups': doc['groups']}


@router.delete('/api/admin/pages/{slug}')
async def delete_page(slug: str, _: bool = Depends(admin_auth)):
    result = await pages.delete_one({'slug': slug})
    if not result.deleted_count:
        raise HTTPException(404, 'Page not found')
    return {'ok': True, 'deleted': slug}
