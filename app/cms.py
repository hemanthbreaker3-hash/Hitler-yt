from __future__ import annotations
import os, re, secrets
from datetime import datetime, timezone
from urllib.parse import urlparse
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from app.database.connection import db, settings, settings_col
from app.admin.auth import require_panel_admin

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

class CategoryInput(BaseModel):
    name: str = Field(min_length=1, max_length=50)

class PageInput(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    overview: str = Field(default='', max_length=10000)
    poster_path: str = ''
    release_date: str = ''
    rating: float | None = None
    media_type: str = 'movie'
    category_ids: list[str] = []
    groups: list[dict] = []
    tmdb_id: int | None = None
    imdb_id: str = ''
    genres: list[str] = []
    runtime: int | None = None

    @field_validator('groups')
    @classmethod
    def validate_groups(cls, groups):
        for group in groups:
            if not isinstance(group, dict) or not str(group.get('name', '')).strip(): raise ValueError('Each group needs a name')
            if group.get('box', 'watch') not in ('watch', 'download'): raise ValueError('Group box must be watch or download')
            buttons = group.get('buttons', [])
            if not isinstance(buttons, list): raise ValueError('Invalid group buttons')
            for button in buttons:
                if not isinstance(button, dict) or not str(button.get('name', '')).strip(): raise ValueError('Each button needs a name')
                target = str(button.get('url', '')).strip()
                parsed = urlparse(target)
                if not target or (parsed.scheme and parsed.scheme not in ('http', 'https')) or (parsed.scheme and not parsed.netloc) or (parsed.netloc and not parsed.scheme) or (not parsed.scheme and not target.startswith('/')): raise ValueError('Button URLs must be relative paths or HTTP(S) URLs')
        return groups

async def admin_auth(_: bool = Depends(require_panel_admin)):
    return True

async def get_settings():
    doc = await site_settings.find_one({'_id':'runtime'}) or {}
    urls = {x['key']: x.get('value', '') for x in await settings_col.find({'key': {'$in': ['base_url', 'backend_url', 'vercel_url', 'website_urls']}}, {'key': 1, 'value': 1}).to_list(4)}
    return { 'downloads_enabled': doc.get('downloads_enabled', True), 'streaming_enabled': doc.get('streaming_enabled', True), 'built_in_player_enabled': doc.get('built_in_player_enabled', True), 'auto_player_redirect': doc.get('auto_player_redirect', False), 'access_mode': doc.get('access_mode', 'all'), 'base_url': urls.get('base_url', settings.BASE_URL), 'backend_url': urls.get('backend_url', settings.BACKEND_URL or ''), 'vercel_url': urls.get('vercel_url', os.getenv('WEBSITE_URL', '')), 'website_urls': urls.get('website_urls', []) }

async def seed_categories():
    await pages.create_index('slug', unique=True)
    await categories.create_index('slug', unique=True)
    for name, slug in [('Movies','movies'),('Web Series','web-series')]:
        await categories.update_one({'slug':slug},{'$setOnInsert':{'name':name,'slug':slug,'created_at':datetime.now(timezone.utc)}},upsert=True)

@router.get('/api/site-settings')
async def public_settings(): return await get_settings()

@router.get('/api/categories')
async def public_categories():
    return {'items': await categories.find({}, {'_id':0}).sort('name',1).to_list(None)}

@router.get('/api/catalog')
async def catalog(category: str | None = None, q: str | None = None):
    query = {'published': True}
    if category: query['category_slugs'] = category
    if q: query['title'] = {'$regex': re.escape(q[:100]), '$options': 'i'}
    try:
        rows = await pages.find(query, {'_id': 0}).sort('created_at', -1).to_list(100)
        return {'items': rows}
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception('Catalog API query failed')
        raise HTTPException(status_code=503, detail=f'Catalog database unavailable: {type(exc).__name__}') from exc


@router.get('/search', response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str = '',
    type: str = '',
    year: str = '',
    genre: str = '',
    category: str = '',
):
    """Server-side search matching Movieszoneflix behaviour: title/overview/genres filters + type/year."""
    q = (q or '').strip()[:100]
    type_filter = type if type in ('', 'movie', 'tv') else ''
    year = (year or '').strip()[:4]
    genre = (genre or '').strip()[:40]
    category = (category or '').strip()[:40]

    query: dict = {'published': True}
    if type_filter:
        # map tv -> series-like values stored in media_type
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

    rows = await pages.find(query, {'_id': 0}).sort('created_at', -1).to_list(100)
    cats = await categories.find({}, {'_id': 0}).sort('name', 1).to_list(None)

    # collect unique genres from catalog for filter dropdown
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

@router.get('/title/{slug}', response_class=HTMLResponse)
async def title_page(request: Request, slug: str):
    page = await pages.find_one({'slug':slug,'published':True},{'_id':0})
    if not page: raise HTTPException(404,'Title not found')
    # Canonicalize internal links to the currently configured public domain.
    internal_prefixes = ('/title/', '/watch/', '/stream/', '/dl/', '/remux/', '/panel')
    forwarded_host = request.headers.get('x-forwarded-host', '').split(',')[0].strip()
    forwarded_proto = request.headers.get('x-forwarded-proto', 'https').split(',')[0].strip().lower()
    public_base = settings.BASE_URL.rstrip('/')
    if forwarded_host and forwarded_proto in ('http', 'https') and not any(c in forwarded_host for c in '/\\@\r\n'):
        public_base = f'{forwarded_proto}://{forwarded_host}'
    for group in page.get('groups', []):
        for button in group.get('buttons', []):
            target = str(button.get('url', '')).strip()
            parsed = urlparse(target)
            if parsed.path.startswith(internal_prefixes):
                button['url'] = public_base + parsed.path + (('?' + parsed.query) if parsed.query else '') + (('#' + parsed.fragment) if parsed.fragment else '')
    return templates.TemplateResponse(request=request,name='title.html',context={'request':request,'page':page})

@router.get('/panel', response_class=HTMLResponse)
@router.get('/admin/panel', response_class=HTMLResponse)
async def panel(request: Request, _: bool = Depends(admin_auth)):
    return templates.TemplateResponse(request=request,name='admin/panel.html',context={'request':request,'settings':await get_settings()})

@router.get('/api/admin/settings')
async def read_admin_settings(_: bool = Depends(admin_auth)):
    return await get_settings()


@router.put('/api/admin/settings')
async def update_settings(data: SettingsUpdate, _: bool = Depends(admin_auth)):
    values = data.model_dump(exclude_none=True)
    url_values = {key: values.pop(key) for key in ('base_url', 'backend_url') if key in values}
    if values:
        await site_settings.update_one({'_id':'runtime'},{'$set':values},upsert=True)
    for key, candidate in url_values.items():
        candidate = candidate.strip().rstrip('/')
        parsed = urlparse(candidate)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise HTTPException(400, f'{key} must be a valid public HTTP(S) URL without credentials, query or fragment')
        host = (parsed.hostname or '').lower()
        if host in ('localhost', '127.0.0.1', '0.0.0.0') or host.endswith(('.local', '.internal')):
            raise HTTPException(400, f'{key} must use a publicly reachable hostname')
        await settings_col.update_one({'key':key},{'$set':{'value':candidate,'updated_at':datetime.now(timezone.utc)}},upsert=True)
        if key == 'base_url':
            await settings_col.update_one({'key':'website_urls'},{'$addToSet':{'value':candidate},'$set':{'updated_at':datetime.now(timezone.utc)}},upsert=True)
            settings.BASE_URL = candidate
            os.environ['BASE_URL'] = candidate
        else:
            settings.BACKEND_URL = candidate
    return await get_settings()

@router.get('/api/admin/categories')
async def list_categories(_: bool = Depends(admin_auth)):
    return {'items':await categories.find({}, {'_id':0}).sort('name',1).to_list(None)}

@router.post('/api/admin/categories')
async def add_category(data: CategoryInput, _: bool = Depends(admin_auth)):
    name=data.name.strip(); slug=re.sub(r'[^a-z0-9]+','-',name.lower()).strip('-')
    if not slug: raise HTTPException(400,'Invalid category name')
    if await categories.find_one({'slug':slug}): raise HTTPException(409,'Category already exists')
    await categories.insert_one({'name':name,'slug':slug,'created_at':datetime.now(timezone.utc)})
    return {'name':name,'slug':slug}

@router.delete('/api/admin/categories/{slug}')
async def remove_category(slug: str, _: bool = Depends(admin_auth)):
    if slug in ('movies','web-series'): raise HTTPException(400,'Default categories cannot be deleted')
    await categories.delete_one({'slug':slug}); return {'ok':True}

@router.get('/api/admin/tmdb-search')
async def tmdb_search(q: str, media_type: str = 'multi', _: bool = Depends(admin_auth)):
    key=settings.TMDB_API_KEY
    if not key: raise HTTPException(503,'TMDB_API_KEY is not configured')
    if media_type not in ('movie','tv','multi'): raise HTTPException(400,'Invalid media type')
    async with httpx.AsyncClient(timeout=15) as client:
        r=await client.get(f'https://api.themoviedb.org/3/search/{media_type}',params={'api_key':key,'query':q,'include_adult':'false'})
        r.raise_for_status(); payload=r.json()
    return {'items':[{'id':x['id'],'media_type':x.get('media_type',media_type if media_type!='multi' else 'movie'),'title':x.get('title') or x.get('name'),'overview':x.get('overview',''),'poster_path':x.get('poster_path'),'release_date':x.get('release_date') or x.get('first_air_date',''),'rating':x.get('vote_average')} for x in payload.get('results',[])[:20]]}

@router.get('/api/admin/pages')
async def list_pages(q: str | None = None, _: bool = Depends(admin_auth)):
    query = {}
    if q and q.strip():
        query = {'$or': [{'title': {'$regex': re.escape(q.strip()[:100]), '$options': 'i'}}, {'slug': {'$regex': re.escape(q.strip()[:100]), '$options': 'i'}}]}
    return {'items': await pages.find(query, {'_id':0}).sort('created_at',-1).to_list(300)}

@router.get('/api/admin/pages/{slug}')
async def get_page(slug: str, _: bool = Depends(admin_auth)):
    page = await pages.find_one({'slug':slug}, {'_id':0})
    if not page: raise HTTPException(404,'Page not found')
    return page

@router.post('/api/admin/pages')
async def create_page(data: PageInput, _: bool = Depends(admin_auth)):
    slug=re.sub(r'[^a-z0-9]+','-',data.title.lower()).strip('-') or secrets.token_hex(5)
    base=slug; i=2
    while await pages.find_one({'slug':slug}): slug=f'{base}-{i}'; i+=1
    cats=await categories.find({'slug':{'$in':data.category_ids}}).to_list(100)
    doc=data.model_dump(); doc.update({'slug':slug,'category_slugs':[x['slug'] for x in cats],'published':True,'created_at':datetime.now(timezone.utc)})
    if data.tmdb_id and settings.TMDB_API_KEY:
        async with httpx.AsyncClient(timeout=15) as client:
            endpoint = 'tv' if data.media_type == 'tv' else 'movie'
            detail = await client.get(f'https://api.themoviedb.org/3/{endpoint}/{data.tmdb_id}', params={'api_key':settings.TMDB_API_KEY,'append_to_response':'external_ids'})
            detail.raise_for_status(); meta=detail.json()
        doc['genres']=[g.get('name','') for g in meta.get('genres',[]) if g.get('name')]
        doc['imdb_id']=(meta.get('external_ids') or {}).get('imdb_id') or meta.get('imdb_id','')
        doc['runtime']=meta.get('runtime') or (meta.get('episode_run_time') or [None])[0]
        doc['tagline']=meta.get('tagline','')
    doc['updated_at'] = doc['created_at']
    await pages.insert_one(doc)
    return {'slug':slug,'url':f"{settings.BASE_URL.rstrip('/')}/title/{slug}"}

@router.put('/api/admin/pages/{slug}')
async def edit_page(slug: str, data: PageInput, _: bool = Depends(admin_auth)):
    cats=await categories.find({'slug':{'$in':data.category_ids}}).to_list(100)
    doc=data.model_dump(); doc['category_slugs']=[x['slug'] for x in cats]
    doc['updated_at'] = datetime.now(timezone.utc)
    result=await pages.update_one({'slug':slug},{'$set':doc})
    if not result.matched_count: raise HTTPException(404,'Page not found')
    return {'ok':True}


@router.delete('/api/admin/pages/{slug}')
async def delete_page(slug: str, _: bool = Depends(admin_auth)):
    result = await pages.delete_one({'slug': slug})
    if not result.deleted_count:
        raise HTTPException(404, 'Page not found')
    return {'ok': True, 'deleted': slug}
