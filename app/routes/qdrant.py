"""Qdrant API router - upravlenie kollekciyami, indeksaciya i semanticheskiy poisk."""
import os
import json
import time
import uuid
import hashlib
import threading
import logging
import urllib.parse
from typing import List, Optional

from fastapi import APIRouter, Request, UploadFile, File, Query
from fastapi.responses import JSONResponse

import qdrant_module as qm

router = APIRouter()
logger = logging.getLogger('infinity')

INBOX = os.path.expanduser('~/.infinity/qdrant_inbox')


def _ok(data):
    return JSONResponse(data)


def _err(msg, code=400):
    return JSONResponse({'error': msg}, status_code=code)


def _profile(request=None) -> str:
    """Профиль приложения из cookie запроса (для личной коллекции)."""
    if request is None:
        return 'default'
    try:
        import profiles_module
        return profiles_module.request_profile(request) or 'default'
    except Exception:
        return 'default'


def _coll(request=None, collection=None, scope=None) -> str:
    """Коллекция: явная > профильная настройка (общая/личная)."""
    if collection:
        return collection
    return qm.collection_for(_profile(request), scope)


@router.get('/api/qdrant/scope')
def get_scope(request: Request):
    """Текущий выбор профиля: общая или личная база."""
    profile = _profile(request)
    return _ok({
        'profile': profile,
        'scope': qm.profile_scope(profile),
        'shared_collection': qm.collection_for(profile, 'shared'),
        'personal_collection': qm.collection_for(profile, 'personal'),
    })


@router.post('/api/qdrant/scope')
async def set_scope(request: Request):
    """Сохранить выбор «общая/личная» в конфиг профиля."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    scope = str(body.get('scope') or '').strip().lower()
    if scope not in ('shared', 'personal'):
        return _err('scope: shared или personal')
    profile = _profile(request)
    try:
        import profiles_module
        cfg = profiles_module.get_profile_config(profile)
        if not isinstance(cfg, dict):
            return _err('конфиг профиля не найден', 404)
        q = dict(cfg.get('qdrant') or {})
        q['scope'] = scope
        cfg['qdrant'] = q
        ok = profiles_module.save_profile_config(profile, cfg)
    except Exception as e:
        return _err(str(e), 500)
    return _ok({'ok': bool(ok), 'profile': profile, 'scope': qm.profile_scope(profile),
                'collection': qm.collection_for(profile)})


@router.get('/api/qdrant/status')
def status():
    return _ok(qm.health())


@router.get('/api/qdrant/embed-info')
def embed_info_ep():
    """Informaciya o lokalnoy embedding-modeli (fastembed)."""
    return _ok(qm.embed_info())


@router.post('/api/qdrant/download-model')
def download_model_ep():
    """Zapustit fonovuyu zagruzku embedding-modeli."""
    try:
        from fastembed import TextEmbedding  # noqa: F401
    except Exception:
        return _err('fastembed не установлен в среде сервера', 500)
    ok = qm.download_embed_model()
    if not ok:
        return _err('Загрузка уже выполняется')
    return _ok({'accepted': True})


@router.get('/api/qdrant/download-model/status')
def download_model_status_ep():
    """Progress zagruzki modeli (opros iz UI)."""
    return _ok(qm.get_embed_download_status())


@router.post('/api/qdrant/embed-config')
async def embed_config_ep(request: Request):
    """Sohranit nastroyski embedding-modeli (model/cache/offline)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    patch = {}
    if body.get('embed_model'):
        patch['embed_model'] = str(body['embed_model']).strip()
    if body.get('embed_cache'):
        patch['embed_cache'] = str(body['embed_cache']).strip()
    if 'embed_local_only' in body:
        patch['embed_local_only'] = bool(body['embed_local_only'])
    if body.get('embed_device'):
        dv = str(body['embed_device']).strip().lower()
        if dv not in ('cpu', 'gpu'):
            dv = 'cpu'
        patch['embed_device'] = dv
        qm._EMBED_STATE['device'] = dv
    if patch:
        qm._save_qdrant_cfg(patch)
        qm._fe_model = None  # prinuditelno perezagruzit model pri sled. ispolzovanii
    return _ok({'ok': True, 'config': qm.embed_info()})


def _active_name():
    try:
        if os.path.exists(qm.ENGINES_FILE):
            d = json.load(open(qm.ENGINES_FILE, encoding='utf-8'))
            return d.get('active') or ''
    except Exception:
        pass
    return ''


def _save_engines(engines, active):
    d = {'engines': engines, 'active': active}
    try:
        os.makedirs(os.path.dirname(qm.ENGINES_FILE), exist_ok=True)
        json.dump(d, open(qm.ENGINES_FILE, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning('save engines error: %s' % e)


@router.get('/api/qdrant/engines')
def get_engines_ep():
    return _ok({'engines': qm.get_engines(), 'active': _active_name()})


@router.post('/api/qdrant/engines')
async def save_engine(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = (body.get('name') or '').strip()
    url = (body.get('url') or '').strip()
    if not name or not url:
        return _err('name i url obyazatelny')
    engines = qm.get_engines()
    for e in engines:
        if e.get('name') == name:
            e['url'] = url
            e['api_key'] = body.get('api_key') or ''
            e['collection'] = body.get('collection') or ''
            break
    else:
        engines.append({'name': name, 'url': url,
                        'api_key': body.get('api_key') or '',
                        'collection': body.get('collection') or ''})
    active = name if body.get('active') is True else _active_name()
    _save_engines(engines, active)
    return _ok({'ok': True, 'engines': engines, 'active': active})


@router.delete('/api/qdrant/engines')
async def delete_engine(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = (body.get('name') or '').strip()
    engines = [e for e in qm.get_engines() if e.get('name') != name]
    active = _active_name()
    if active == name:
        active = ''
    _save_engines(engines, active)
    return _ok({'ok': True, 'engines': engines, 'active': active})


@router.post('/api/qdrant/engines/test')
async def test_engine(request: Request):
    '''Proverka podklyucheniya k vneshnemu Qdrant (bez sohraneniya).'''
    try:
        body = await request.json()
    except Exception:
        body = {}
    url = (body.get('url') or '').strip()
    api_key = body.get('api_key') or None
    if not url:
        return _err('no url')
    if not qm.QDRANT_AVAILABLE:
        return _err('qdrant_client не установлен', 500)
    try:
        client = qm.QdrantClient(url=url, api_key=api_key, timeout=6)
        cols = client.get_collections()
        names = [c.name for c in (cols.collections or [])]
        try:
            client.close()
        except Exception:
            pass
        return _ok({'ok': True, 'collections': names, 'count': len(names)})
    except Exception as e:
        return _ok({'ok': False, 'error': str(e)})


@router.get('/api/qdrant/collections')
def collections():
    return _ok({'collections': qm.list_collections()})


@router.get('/api/qdrant/collection/{name}')
def collection_info(name: str):
    return _ok(qm.collection_info(name))


@router.get('/api/qdrant/stats')
def collection_stats(request: Request, name: str = Query(''), scope: str = Query(''),
                     limit: int = Query(200000)):
    coll = _coll(request, name, scope)
    return _ok(qm.collection_source_stats(coll, limit=limit))


@router.post('/api/qdrant/create')
async def create(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = body.get('collection') or body.get('name')
    dim = body.get('dimension')
    if not name:
        return _err('no collection name')
    ok = qm.ensure_collection(name, dim)
    return _ok({'ok': ok, 'collection': name})


@router.post('/api/qdrant/clear')
async def clear(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = body.get('collection')
    if not name:
        return _err('no collection')
    return _ok({'ok': qm.clear(name), 'collection': name})


@router.post('/api/qdrant/delete')
async def delete(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = body.get('collection')
    ids = body.get('ids')
    if not name:
        return _err('no collection')
    if ids:
        return _ok({'ok': qm.delete_points(name, ids)})
    return _ok({'ok': qm.delete_collection(name)})


@router.post('/api/qdrant/search')
async def search(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = _coll(request, body.get('collection'), body.get('scope'))
    query = body.get('query', '')
    k = int(body.get('k', 10))
    return _ok({'results': qm.search(name, query, k), 'collection': name})


@router.get('/api/qdrant/graph')
def knowledge_graph(request: Request, collection: str = Query(''), scope: str = Query(''),
                    limit: int = Query(300)):
    """Граф знаний: узлы — проиндексированные документы, рёбра связывают
    документы одного типа. Показывает, что реально лежит в коллекции."""
    name = _coll(request, collection, scope)
    try:
        client = qm.get_client()
        if client is None:
            return _err('qdrant клиент недоступен', 503)
        limit = max(10, min(int(limit), 1000))
        points = []
        offset = None
        while len(points) < limit:
            try:
                batch, offset = client.scroll(
                    collection_name=name,
                    limit=min(100, limit - len(points)),
                    offset=offset,
                    with_payload=True,
                    with_vectors=True,
                )
            except Exception:
                batch, offset = [], None
            if not batch:
                break
            points.extend(batch)
            if offset is None:
                break

        # Счётчик точек в локальном режиме Qdrant может быть недоступен
        # (vectors_count = None / исключение) — берём реальное число из scroll.
        try:
            total = int((client.count(collection_name=name)).count)
        except Exception:
            total = len(points)

        if not points:
            return _ok({'nodes': [], 'edges': [], 'types': {}, 'total': total,
                        'shown': 0, 'collection': name})

        # Группируем чанки по документу (заголовку): один узел = один документ,
        # а не один чанк. Рёбра = семантическая близость между документами.
        from collections import defaultdict as _dd
        import numpy as _np
        docs = _dd(list)   # node_id -> [(vector, payload, ptype, title)]
        for p in points:
            pl = p.payload or {}
            ptype = str(pl.get('source') or pl.get('type') or 'other')
            title = str(pl.get('title') or pl.get('name') or pl.get('id') or p.id)[:90]
            node_id = '%s::%s' % (ptype, title)
            v = getattr(p, 'vector', None)
            if isinstance(v, dict):
                vs = list(v.values())
                v = vs[0] if vs else None
            docs[node_id].append((v, pl, ptype, title))

        # Ограничиваем число документов для производительности (по числу чанков)
        doc_items = sorted(docs.items(), key=lambda kv: -len(kv[1]))[:80]

        nodes, types, centroids = [], {}, {}
        for node_id, lst in doc_items:
            ptype = lst[0][2]
            title = lst[0][3]
            types[ptype] = types.get(ptype, 0) + 1
            vs = [v for (v, _, _, _) in lst if v]
            if vs:
                try:
                    centroids[node_id] = _np.mean(_np.asarray(vs, dtype=float), axis=0)
                except Exception:
                    centroids[node_id] = None
            nodes.append({'id': node_id, 'title': title, 'type': ptype})

        # Семантические рёбра: косинусная близость центроидов документов
        edges = []
        valid = [n['id'] for n in nodes if centroids.get(n['id']) is not None]
        if len(valid) >= 2:
            try:
                M = _np.vstack([_np.asarray(centroids[t], dtype=float) for t in valid])
                M = M / (_np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
                S = M @ M.T
                thr = 0.28
                for i in range(len(valid)):
                    for j in range(i + 1, len(valid)):
                        if S[i, j] >= thr:
                            edges.append([valid[i], valid[j]])
                # гарантируем хотя бы связь с ближайшим соседом
                if not edges:
                    for i in range(len(valid)):
                        j = int(_np.argmax(S[i]))
                        if j != i:
                            edges.append([valid[i], valid[j]])
                if len(edges) > 400:
                    edges = edges[:400]
            except Exception as e:
                logger.warning('graph similarity error: %s' % e)
                edges = []

        return _ok({'nodes': nodes, 'edges': edges, 'types': types,
                    'total': total, 'shown': len(nodes), 'collection': name})
    except Exception as e:
        logger.warning('knowledge graph error: %s' % e)
        return _err(str(e), 500)


@router.get('/api/qdrant/browse')
def browse(request: Request, source: str = Query(''), q: str = Query(''),
           list_all: bool = Query(False), limit: int = Query(100),
           offset: int = Query(0)):
    return _ok({'items': qm.browse_source(source, q, list_all, limit, offset,
                                          profile_id=_profile(request))})


@router.post('/api/qdrant/upload')
async def upload(files: List[UploadFile] = File(...)):
    os.makedirs(INBOX, exist_ok=True)
    paths = []
    for f in files:
        dest = os.path.join(INBOX, f.filename)
        content = await f.read()
        with open(dest, 'wb') as out:
            out.write(content)
        paths.append(dest)
    return _ok({'paths': paths, 'count': len(paths)})


@router.post('/api/qdrant/index')
async def index(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    # Индексация целых источников должна идти в ту же базу, что и поиск:
    # выбранная профилем (общая/личная) либо явно переданная.
    collection = _coll(request, body.get('collection'), body.get('scope'))
    source = body.get('source', 'books')
    items = body.get('items')
    whole = body.get('whole_source', False)
    paths = body.get('paths')
    pid = _profile(request)

    def run():
        try:
            qm.index_items(collection, source, items, whole, paths,
                           profile_id=pid)
        except Exception as e:
            logger.warning('index thread error: %s' % e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return _ok({'accepted': True, 'collection': collection, 'source': source})


@router.get('/api/qdrant/index-status')
def index_status():
    return _ok(qm.INDEX_PROGRESS)


@router.post('/api/qdrant/add')
async def add(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    source = body.get('source')
    item_id = body.get('id') or body.get('item_id')
    title = body.get('title')
    text = body.get('text')
    collection = _coll(request, body.get('collection'), body.get('scope'))
    extra = body.get('extra')
    if not source or not item_id:
        return _err('source i id obyazatelny')
    if source == 'wiki':
        # Wiki-страницы читает только тот, кому они видны (приватная — владелец,
        # публичная — любой профиль); текст достаём под профилем запроса.
        pid = _profile(request)
        try:
            import module_flags
            if not module_flags.enabled_for('user_wiki', pid):
                return _err('Модуль пользовательских вики выключен', 403)
            from modules import userwiki_store as _uw
            wiki_id, _, slug = str(item_id).partition(':')
            page = _uw.get_page(pid, wiki_id, slug)
        except PermissionError as e:
            return _err(str(e), 403)
        except FileNotFoundError as e:
            return _err(str(e), 404)
        except Exception as e:
            return _err(str(e), 500)
        if text is None:
            text = page.get('content') or ''
        if not title:
            title = (page.get('page') or {}).get('title') or slug
        # Приватная вика не имеет права попасть в общую коллекцию, даже если
        # клиент прислал scope=shared: иначе её текст увидят другие профили.
        # Чужую personal-коллекцию тоже не принимаем: коллекция выбирается из
        # двух допустимых, а не из произвольной строки в теле запроса.
        if page.get('visibility') != 'public':
            collection = qm.personal_collection(pid)
        elif collection not in (qm.shared_collection(), qm.personal_collection(pid)):
            collection = qm.shared_collection()
        extra = dict(extra or {})
        # Идентификаторы пишем серверные: по ним семантическая выдача сверяет
        # видимость, и клиент не должен иметь возможности их подменить.
        extra['wiki_id'] = page.get('wiki_id') or wiki_id
        extra['wiki_owner'] = page.get('owner') or pid
        extra['source_scope'] = ('shared' if page.get('visibility') == 'public'
                                 else 'personal')
    elif source == 'file':
        # Файлы индексируются только в личную базу: это приватная функция
        # профиля, и общая коллекция не должна узнать ни путь, ни текст.
        pid = _profile(request)
        try:
            import module_flags
            if not module_flags.enabled_for('file_index', pid):
                return _err('Модуль индексации файлов выключен', 403)
            from modules import file_index as _fi
            if not _fi.search_ready(pid):
                return _err('Индексация файлов выключена в настройках', 403)
            path = str(item_id)
            content = _fi.read_indexed(pid, path)
        except Exception as e:
            return _err(str(e), 500)
        if text is None:
            text = content or ''
        if not title:
            title = os.path.basename(path)
        collection = qm.personal_collection(pid)
        extra = dict(extra or {})
        # Путь — серверный (проверен read_indexed), а не из тела запроса.
        extra['path'] = path
        extra['source_scope'] = 'personal'
    try:
        res = qm.index_single(collection, source, item_id, title, text, extra,
                              profile_id=_profile(request))
        return _ok({'ok': True, 'result': res, 'collection': collection,
                    'scope': 'personal' if collection == qm.personal_collection(_profile(request)) else 'shared'})
    except Exception as e:
        return _err(str(e), 500)


@router.post('/api/qdrant/add-source')
async def add_source(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    source = (body.get('source') or '').lower()
    sid = body.get('id') or body.get('item_id') or ''
    title = body.get('title') or ''
    collection = _coll(request, body.get('collection'), body.get('scope'))
    scope_name = 'personal' if collection == qm.personal_collection(_profile(request)) else 'shared'
    pid = _profile(request)
    if not source:
        return _err('source obyazatelen')
    if source in ('wiki', 'file'):
        # Массовая индексация в обход проверок доступа недопустима: вики
        # индексируются своим модулем (public→shared, private→personal), а
        # файлы — только в личную коллекцию из своей настройки.
        return _err('Источник индексируется через свой модуль', 400)
    if source in ('zim', 'news'):
        if source == 'zim':
            items = qm.list_zim_articles(sid, 2000) if sid else []
        else:
            items = qm.browse_source('news', list_all=True,
                                     profile_id=_profile(request))
            items = [{'id': (it.get('id') or it.get('url') or ''), 'title': it.get('title', '')}
                     for it in items]
        if not items:
            return _ok({'ok': True, 'started': False, 'total': 0, 'empty': True,
                        'collection': collection, 'scope': scope_name})
        def _run():
            try:
                qm.index_items(collection, source, items=items, profile_id=pid)
            except Exception as e:
                qm.logger.warning('add-source bg error: %s' % e)
        threading.Thread(target=_run, daemon=True).start()
        return _ok({'ok': True, 'started': True, 'total': len(items),
                    'collection': collection, 'scope': scope_name})
    res = qm.index_single(collection, source, sid, title=title,
                          profile_id=_profile(request))
    return _ok({'ok': True, 'result': res, 'collection': collection, 'scope': scope_name})


# ===================== BLOKNOT =====================
NOTES_DIR = os.path.join(os.path.expanduser('~/.infinity'), 'qdrant_notes')


def _notes_dir(profile_id: str = '') -> str:
    """Каталог блокнота: у каждого профиля свой подкаталог.

    Раньше файлы лежали общей кучей, и список заметок одного профиля видел
    заметки всех остальных. Старые файлы из корня остаются за общедоступным
    профилем (default), чтобы ничего не потерять.
    """
    pid = str(profile_id or '').strip() or 'default'
    d = os.path.join(NOTES_DIR, pid)
    os.makedirs(d, exist_ok=True)
    return d


def _legacy_notes_dir() -> str:
    return NOTES_DIR


@router.post('/api/qdrant/note')
async def save_note(request: Request):
    '''Sohranit tekstovuyu zametku (bloknot) i srazu proindeksirovat eyo.'''
    try:
        body = await request.json()
    except Exception:
        body = {}
    title = (body.get('title') or '').strip() or 'Без названия'
    text = (body.get('text') or '').strip()
    collection = _coll(request, body.get('collection'), body.get('scope'))
    if not text:
        return _err('Текст заметки пуст')
    nid = 'note_%s' % uuid.uuid4().hex[:12]
    pid = _profile(request)
    # 1) sohranyaem fajl (chtoby zametka zhila ne tolko v vektore)
    nd = _notes_dir(pid)
    fp = os.path.join(nd, nid + '.json')
    rec = {'id': nid, 'title': title, 'text': text, 'created': time.time(),
           'profile': pid}
    try:
        with open(fp, 'w', encoding='utf-8') as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning('note save error: %s' % e)
    # 2) indeksiruem
    try:
        res = qm.index_single(collection, 'note', nid, title=title, text=text)
        if res.get('errors') and not res.get('chunks'):
            return _err('Заметка сохранена, но не проиндексирована: %s' %
                        (res.get('last_error') or 'нет векторов'), 500)
    except Exception as e:
        return _err('Заметка сохранена, но индексация упала: %s' % e, 500)
    return _ok({'ok': True, 'id': nid, 'title': title,
                'chunks': res.get('chunks', 0), 'collection': collection})


@router.get('/api/qdrant/notes')
def list_notes(request: Request, collection: str = Query(''), scope: str = Query('')):
    '''Spisok sohranyonnyh zametok (fajly + nalichie v vektore).'''
    coll = _coll(request, collection, scope)
    pid = _profile(request)
    out = []
    nd = _notes_dir(pid)
    dirs = [nd]
    if pid == 'default':
        dirs.append(_legacy_notes_dir())
    for src_dir in dirs:
        try:
            for fn in sorted(os.listdir(src_dir), reverse=True):
                if not fn.endswith('.json'):
                    continue
                try:
                    with open(os.path.join(src_dir, fn), encoding='utf-8') as f:
                        d = json.load(f)
                    out.append({'id': d.get('id') or fn[:-5],
                                'title': d.get('title', ''),
                                'text': (d.get('text') or '')[:200],
                                'created': d.get('created', 0)})
                except Exception:
                    continue
        except Exception as e:
            logger.warning('notes list error: %s' % e)
    # proveryaem nalichie v vektornoy kollekcii
    indexed_ids = set()
    try:
        client = qm.get_client()
        if client:
            offset = None
            while True:
                batch, offset = client.scroll(
                    collection_name=coll, limit=1000,
                    with_vectors=False, with_payload=True, offset=offset)
                if not batch:
                    break
                for pt in batch:
                    pl = pt.payload or {}
                    if pl.get('source') == 'note' and pl.get('item_id'):
                        indexed_ids.add(str(pl.get('item_id')))
                if offset is None:
                    break
    except Exception as e:
        logger.warning('notes indexed check error: %s' % e)
    for n in out:
        n['indexed'] = n['id'] in indexed_ids
    return _ok({'notes': out, 'collection': coll})


@router.delete('/api/qdrant/note/{note_id}')
def delete_note(request: Request, note_id: str, collection: str = Query(''), scope: str = Query('')):
    '''Udalit zametku (fajl + tochki iz kollekcii).'''
    coll = _coll(request, collection, scope)
    nid = note_id.strip()
    if not nid:
        return _err('no id')
    # udalyaem tochki
    point_ids = []
    try:
        client = qm.get_client()
        if client:
            offset = None
            while True:
                batch, offset = client.scroll(
                    collection_name=coll, limit=1000,
                    with_vectors=False, with_payload=True, offset=offset)
                if not batch:
                    break
                for pt in batch:
                    pl = pt.payload or {}
                    if pl.get('source') == 'note' and str(pl.get('item_id')) == nid:
                        point_ids.append(pt.id)
                if offset is None:
                    break
            if point_ids:
                qm.delete_points(coll, point_ids)
    except Exception as e:
        logger.warning('note delete points error: %s' % e)
    # udalyaem fajl
    fp = os.path.join(_notes_dir(), nid + '.json')
    removed = False
    try:
        if os.path.exists(fp):
            os.remove(fp)
            removed = True
    except Exception as e:
        logger.warning('note file delete error: %s' % e)
    return _ok({'ok': True, 'file_removed': removed, 'points_removed': len(point_ids)})


# ===================== DUBLIKATY =====================
@router.get('/api/qdrant/duplicates')
def duplicates(collection: str = Query(''), limit: int = Query(200000)):
    '''Nayti dubli v kollekcii: gruppirovka po (source,item_id) i po heshu teksta.'''
    coll = collection or qm._qdrant_cfg().get('search_all_collection') or 'search_all_semantic'
    client = qm.get_client()
    if not client:
        return _err('Qdrant недоступен', 500)
    try:
        limit = int(limit)
    except Exception:
        limit = 200000
    by_item = {}          # (source,item_id) -> {points, title, chunks, dup_ids}
    by_text = {}          # hash(normalized text) -> {points, sample}
    item_seen = {}        # (source,item_id) -> {text_hash: pervaya tochka}
    total = 0
    try:
        offset = None
        while total < limit:
            batch, offset = client.scroll(
                collection_name=coll, limit=1000,
                with_vectors=False, with_payload=True, offset=offset)
            if not batch:
                break
            for pt in batch:
                total += 1
                pl = pt.payload or {}
                src = str(pl.get('source') or 'unknown')
                iid = str(pl.get('item_id') or '')
                title = str(pl.get('title') or iid or pt.id)[:120]
                txt = str(pl.get('text') or '')
                norm = ' '.join(txt.lower().split())
                h = hashlib.sha1(norm.encode('utf-8')).hexdigest()[:16] if norm else ''
                by_item.setdefault((src, iid), {
                    'source': src, 'item_id': iid, 'title': title,
                    'point_ids': [], 'chunks': 0, 'dup_ids': []})
                by_item[(src, iid)]['point_ids'].append(pt.id)
                by_item[(src, iid)]['chunks'] += 1
                if h:
                    seen = item_seen.setdefault((src, iid), {})
                    if h in seen:
                        # povtornyy chank s tem zhe tekstom vnutri odnogo itema
                        by_item[(src, iid)]['dup_ids'].append(pt.id)
                    else:
                        seen[h] = pt.id
                    by_text.setdefault(h, {'sample': norm[:160], 'points': []})
                    by_text[h]['points'].append(pt.id)
            if offset is None:
                break
    except Exception as e:
        return _err('scroll error: %s' % e, 500)
    # gruppa A: nastoyaschie dubli — odin i tot zhe (source,item_id) s POVTERNYMI
    # chankami (obychnyy mnogochankovyy element — ne dubl, t.k. chanki unikalny)
    dup_items = []
    for (src, iid), d in by_item.items():
        if d.get('dup_ids') and src != 'note':
            dup_items.append({
                'type': 'item',
                'source': src, 'item_id': iid, 'title': d['title'],
                'chunks': d['chunks'],
                'point_ids': d['point_ids'],
                'dup_ids': d.get('dup_ids', []),
                'dup_count': len(d.get('dup_ids', [])),
            })
    # gruppa B: identichnyy tekst v raznyh itemah (naprimer, zametka sohranena 2 raza)
    seen_points = set()
    for d in dup_items:
        seen_points.update(d['point_ids'])
    # bezopasnyy spisok udaleniya: tolko izbytochnye kopii, pervye vhozhdeniya sohranyayutsya
    safe_ids = []
    for d in dup_items:
        safe_ids.extend(d['dup_ids'])
    chunk_dupes = len(safe_ids)
    dup_texts = []
    text_dupes = 0
    for h, d in by_text.items():
        pts = [p for p in d['points'] if p not in seen_points]
        if len(pts) > 1:
            dup_texts.append({
                'type': 'text',
                'sample': d['sample'],
                'chunks': len(d['points']),
                'point_ids': d['points'],
            })
            # odinakovyy tekst v raznyh itemah: udalyaem vse kopii krome pervoy
            safe_ids.extend(pts[1:])
            text_dupes += len(pts) - 1
    dup_items.sort(key=lambda x: -x['chunks'])
    dup_texts.sort(key=lambda x: -x['chunks'])
    return _ok({'collection': coll, 'total_points': total,
                'duplicate_items': dup_items, 'duplicate_texts': dup_texts,
                'safe_delete_ids': safe_ids,
                'safe_counts': {'chunk_dupes': chunk_dupes,
                                'text_dupes': text_dupes,
                                'total': len(safe_ids)}})


# ===================== STATUS DOBAVLENO =====================
@router.post('/api/qdrant/added')
async def added_check(request: Request):
    '''Kakie iz peredannyh item_id uzhe est v kollekcii.
    body: {collection?, source?, ids:[...]} -> {added:[...]}'''
    try:
        body = await request.json()
    except Exception:
        body = {}
    coll = _coll(request, body.get('collection'), body.get('scope'))
    # Проверяем и выбранную коллекцию, и общую: элемент мог быть добавлен
    # до смены настройки, и кнопка не должна снова предлагать его добавить.
    colls = [coll]
    shared = qm.collection_for(_profile(request), 'shared')
    if shared not in colls:
        colls.append(shared)
    source = (body.get('source') or '').strip().lower()
    ids = body.get('ids') or []
    if not ids:
        return _ok({'added': []})
    want = {str(i) for i in ids}
    # payload item_id hranitsya URL-dekodirovannym (index_single delal unquote),
    # poetomu sravnivaem i syroy, i dekodirovannyy vid
    raw_by_decoded = {}
    try:
        import urllib.parse as _up
        for i in ids:
            raw_by_decoded.setdefault(_up.unquote(str(i)), str(i))
    except Exception:
        pass
    found = set()
    try:
        client = qm.get_client()
        if client:
            for coll_name in colls:
                offset = None
                while True:
                    batch, offset = client.scroll(
                        collection_name=coll_name, limit=1000,
                        with_vectors=False, with_payload=True, offset=offset)
                    if not batch:
                        break
                    for pt in batch:
                        pl = pt.payload or {}
                        iid = str(pl.get('item_id') or '')
                        hit = None
                        if iid in want:
                            hit = iid
                        elif iid in raw_by_decoded:
                            hit = raw_by_decoded[iid]
                        if hit is not None:
                            if source and str(pl.get('source') or '').lower() != source:
                                continue
                            found.add(hit)
                    if offset is None or len(found) == len(want):
                        break
                if len(found) == len(want):
                    break
    except Exception as e:
        logger.warning('added check error: %s' % e)
    return _ok({'added': sorted(found), 'collections': colls})


# ===================== KOLLEKCII KNIG =====================
@router.get('/api/qdrant/book-collections')
def book_collections():
    '''Spisok kollekcij modulya knig (serii i zhanry) dlya vybora pri indeksacii.'''
    out = []
    try:
        from modules import collection_module as cm
        try:
            for s in (cm.get_series(page=1, page_size=500) or []):
                sid = s.get('id') if isinstance(s, dict) else s
                name = s.get('name', '') if isinstance(s, dict) else str(s)
                if sid:
                    out.append({'kind': 'series', 'id': str(sid),
                                'name': ('Серия: %s' % name) if name else str(sid)})
        except Exception as e:
            logger.warning('series list error: %s' % e)
        try:
            for g in (cm.get_genres() or []):
                gid = g.get('id') if isinstance(g, dict) else g
                name = (g.get('name') or g.get('code') or '') if isinstance(g, dict) else str(g)
                if gid:
                    out.append({'kind': 'genre', 'id': str(gid),
                                'name': ('Жанр: %s' % name) if name else str(gid)})
        except Exception as e:
            logger.warning('genres list error: %s' % e)
    except Exception as e:
        logger.warning('book collections list error: %s' % e)
    return _ok({'collections': out})


@router.get('/api/qdrant/book-collection-items')
def book_collection_items(kind: str = Query('series'), cid: str = Query(''),
                          limit: int = Query(500)):
    '''Spisok knig vnutri kollekcii (seriya ili zhanr).'''
    try:
        from modules import collection_module as cm
        real = int(cid) if str(cid).lstrip('-').isdigit() else cid
        if kind == 'genre':
            data = cm.get_genre_books(real, page=1, page_size=limit)
        else:
            data = cm.get_series_books(real)
        if isinstance(data, dict):
            data = data.get('books') or data.get('items') or []
        items = []
        for b in (data or [])[:limit]:
            if isinstance(b, dict):
                bid = b.get('id') or b.get('book_id') or ''
                title = b.get('title') or b.get('name') or str(bid)
                author = b.get('author') or b.get('author_name') or ''
                if author:
                    title = '%s — %s' % (title, author)
                items.append({'id': str(bid), 'title': title})
            else:
                items.append({'id': str(b), 'title': str(b)})
        return _ok({'items': items, 'collection': cid, 'kind': kind})
    except Exception as e:
        logger.warning('book collection items error: %s' % e)
        return _err(str(e), 500)


def _hit(ident, score, payload):
    return {'id': ident, 'score': float(score), 'payload': payload}


def _media_hits(pid: str, query: str, k: int, kinds=None) -> list:
    """Медиа-индекс профиля: документы (текст/OCR), картинки, аудио, видео.

    kinds сужает выдачу: «Медиа» запрашивает только картинки/аудио/видео, а
    документы показываются в «Файловом менеджере».
    """
    out = {}
    try:
        import module_flags as _mf
        if not _mf.enabled_for('media_index', pid):
            return []
        from modules import media_index as _mi
        if not _mi.search_ready(pid):
            return []
        for m in _mi.search_semantic(pid, query, k):
            path = m.get('path') or ''
            kind = m.get('type') or m.get('source') or 'document'
            if kinds is not None and kind not in kinds:
                continue
            url = m.get('url') or ''
            key = url or path
            out[key] = _hit('media:' + (key or kind), float(m.get('score') or 0) * 100, {
                'source': 'media', 'kind': kind, 'type': kind,
                'item_id': m.get('item_id') or path,
                'title': m.get('title') or '',
                'text': m.get('preview') or '',
                'url': url, 'thumb': m.get('thumb') or '',
                'path': path, 'frame': m.get('frame') or 0,
                'lex': False,
            })
    except Exception as e:
        logger.warning('media semantic: %s' % e)
    # Лексический запас: смысловой вектор может промахнуться по точному слову
    # в названии/OCR, а текстовая ветка индекса его знает.
    try:
        import search_all_sources as sas
        for r in sas.search_media(pid, query, k):
            url = r.get('url') or ''
            path = r.get('path') or ''
            kind = r.get('type') or 'document'
            if kinds is not None and kind not in kinds:
                continue
            key = url or path
            prev = out.get(key)
            score = float(r.get('score') or 0)
            if prev:
                prev['payload']['lex'] = True
                if prev['score'] >= score:
                    continue
            out[key] = _hit('media:' + (key or 'item'), score, {
                'source': 'media', 'kind': kind,
                'type': kind,
                'item_id': r.get('item_id') or path,
                'title': r.get('title') or '', 'text': r.get('preview') or '',
                'url': url, 'thumb': r.get('thumb') or '',
                'path': path, 'frame': 0,
                'lex': True,
            })
    except Exception as e:
        logger.warning('media lexical: %s' % e)
    return list(out.values())


def _file_hits(pid: str, query: str, k: int) -> list:
    """Всё, что лежит в файловом менеджере: текстовый индекс + векторы файлов.

    Медиа-часть файлового менеджера добавляет _media_hits — вызывающий склеивает
    их, чтобы фильтр «Файловый менеджер» видел и картинки, и видео, и текст.
    """
    out = {}
    try:
        import search_all_sources as sas
        for r in sas.search_files(pid, query, k):
            url = r.get('url') or ''
            path = r.get('path') or ''
            out[url or path] = _hit('file:' + (url or path), r.get('score') or 0, {
                'source': 'file', 'kind': 'file', 'type': 'file',
                'title': r.get('title') or '', 'text': r.get('preview') or '',
                'url': url, 'thumb': '', 'path': path, 'frame': 0,
                'lex': True,
            })
    except Exception as e:
        logger.warning('file lexical: %s' % e)
    try:
        import module_flags as _mf
        from modules import file_index as _fi
        if _mf.enabled_for('file_index', pid) and _fi.search_ready(pid) \
                and _fi.get_settings(pid).get('vector'):
            coll = qm.personal_collection(pid)
            for hit in qm.search_filtered(coll, query, k, {'source': 'file'}):
                p = hit.get('payload') or {}
                path = p.get('path') or p.get('item_id') or ''
                directory, name = os.path.split(path)
                url = ('/filemanager?path=' + urllib.parse.quote(directory) +
                       '&file=' + urllib.parse.quote(name))
                prev = out.get(url or path)
                score = float(hit.get('score') or 0) * 100
                if prev:
                    if prev.get('payload', {}).get('lex'):
                        continue
                    if prev['score'] >= score:
                        continue
                out[url or path] = _hit('file:' + (url or path), score, {
                    'source': 'file', 'kind': 'file', 'type': 'file',
                    'title': p.get('title') or name,
                    'text': (p.get('text') or '')[:300],
                    'url': url, 'thumb': '', 'path': path, 'frame': 0,
                    'lex': False,
                })
    except Exception as e:
        logger.warning('file vector: %s' % e)
    return list(out.values())


def _track_hits(pid: str, query: str, k: int) -> list:
    """Медиатека плеера: музыка и видео, включая распознанные тексты.

    Названия/исполнители ищутся как раньше; дополнительно сюда попадают
    тексты песен и субтитры из media_transcripts (наполняет opt-in Whisper),
    поэтому «слова песни» и «реплика из фильма» находят сам файл.
    """
    import base64
    import difflib
    out = []
    try:
        import media_module as mm
        clause, params = mm._visibility_clause('music', pid)
        where = (' WHERE ' + clause) if clause else ''
        tracks = mm._get_all_music_tracks(where, params)
        ql = query.strip().lower()

        # search_tracks пропускает последовательное совпадение букв (>=0.8),
        # из-за чего «космос» тянул случайные треки, где эти буквы просто
        # встречаются по порядку. Для выдачи оставляем подстроку или близкое
        # по difflib название/исполнителя.
        def _ratio(a, b):
            return difflib.SequenceMatcher(None, a, b).ratio()

        fuzzy_on = mm.media_fuzzy.enabled()

        for t in mm.search_tracks(query, tracks):
            title = t.get('title') or ''
            artist = t.get('artist') or ''
            album = t.get('album') or ''
            filepath = t.get('filepath') or ''
            if not filepath:
                continue
            hay = ('%s %s %s' % (title, artist, album)).lower()
            if fuzzy_on:
                # Порог по difflib выше отбрасывал ровно те находки, ради которых
                # нечёткость и включена: транслит и опечатку SequenceMatcher не
                # считает близкими. Оценка берётся из того же матчера, что и у
                # страницы, поэтому порядок выдачи не расходится со списком.
                s = mm.media_fuzzy.score_fields(ql, [(hay, 0.95), (title, 1.0),
                                                     (artist, 0.9), (album, 0.8)])
                if s <= 0:
                    continue
                score = round(60.0 + 40.0 * s, 1)
            elif ql and ql in hay:
                score = 100.0
            else:
                # Короткие запросы («игра») нечётко совпадают с чужими
                # исполнителями («ВИА ГРА») — для них смотрим только название.
                best = _ratio(ql, title.lower())
                if len(ql) >= 5:
                    best = max(best, _ratio(ql, artist.lower()),
                               _ratio(ql, album.lower()))
                if best < 0.75:
                    continue
                score = round(60.0 + 35.0 * best, 1)
            enc = base64.urlsafe_b64encode(
                filepath.encode('utf-8')).decode('ascii').rstrip('=')
            thumb = ''
            if t.get('has_cover'):
                thumb = '/api/media/cover/' + str(t.get('id') or '')
            out.append(_hit('music:' + str(t.get('id') or filepath), score, {
                'source': 'music', 'kind': 'audio', 'type': 'audio',
                'title': ('%s — %s' % (artist, title)) if artist else title,
                'text': ' · '.join(x for x in (album, t.get('genre') or '') if x),
                'url': '/api/media/stream/' + enc, 'thumb': thumb,
                'path': filepath, 'frame': 0,
                'track_id': str(t.get('id') or ''),
                'duration': float(t.get('duration') or 0),
            }))
            if len(out) >= k:
                break
        clause_v, params_v = mm._visibility_clause('video', pid)
        where_v = (' WHERE ' + clause_v) if clause_v else ''
        ql_v = query.strip().lower()
        for v in mm._get_all_video_files(where_v, params_v):
            title = v.get('title') or v.get('filename') or ''
            filepath = v.get('filepath') or ''
            if not filepath:
                continue
            hay = ('%s %s' % (title, v.get('overview') or v.get('description') or '')).lower()
            if ql_v not in hay:
                continue
            enc = base64.urlsafe_b64encode(
                filepath.encode('utf-8')).decode('ascii').rstrip('=')
            out.append(_hit('video:' + filepath, 90.0, {
                'source': 'music', 'kind': 'video', 'type': 'video',
                'title': title, 'text': (v.get('overview') or '')[:300],
                'url': '/api/media/stream/' + enc, 'thumb': '',
                'path': filepath, 'frame': 0,
            }))
            if len(out) >= k * 2:
                break

        seen = {(h.get('payload') or {}).get('path') or '' for h in out}
        for tr in mm.search_transcripts(query, pid, k):
            filepath = tr.get('path') or ''
            if not filepath or filepath in seen:
                continue
            seen.add(filepath)
            enc = base64.urlsafe_b64encode(
                filepath.encode('utf-8')).decode('ascii').rstrip('=')
            is_video = tr.get('kind') == 'video'
            thumb = ''
            if is_video and tr.get('has_poster'):
                thumb = '/api/media/poster/' + str(tr.get('item_id') or '')
            elif not is_video and tr.get('has_cover'):
                thumb = '/api/media/cover/' + str(tr.get('item_id') or '')
            out.append(_hit(
                ('video:' if is_video else 'music:') + str(tr.get('item_id') or filepath),
                min(90.0, float(tr.get('score') or 0)), {
                    'source': 'music', 'kind': 'video' if is_video else 'audio',
                    'type': 'video' if is_video else 'audio',
                    'title': tr.get('title') or filepath,
                    'text': (tr.get('text') or '')[:300],
                    'url': '/api/media/stream/' + enc, 'thumb': thumb,
                    'path': filepath, 'frame': 0,
                    'track_id': '' if is_video else str(tr.get('item_id') or ''),
                    'duration': float(tr.get('duration') or 0),
                }))
        try:
            import module_flags
            if module_flags.enabled_for('media_mood', pid):
                for mt in mm.search_mood_tracks(query, pid, k):
                    filepath = mt.get('path') or ''
                    if not filepath or filepath in seen:
                        continue
                    seen.add(filepath)
                    enc = base64.urlsafe_b64encode(
                        filepath.encode('utf-8')).decode('ascii').rstrip('=')
                    thumb = ''
                    if mt.get('has_cover'):
                        thumb = '/api/media/cover/' + str(mt.get('item_id') or '')
                    out.append(_hit(
                        'music:' + str(mt.get('item_id') or filepath),
                        min(95.0, float(mt.get('score') or 0)), {
                            'source': 'music', 'kind': 'audio', 'type': 'audio',
                            'title': mt.get('title') or filepath,
                            'text': mt.get('text') or '',
                            'url': '/api/media/stream/' + enc, 'thumb': thumb,
                            'path': filepath, 'frame': 0,
                            'track_id': str(mt.get('item_id') or ''),
                            'duration': float(mt.get('duration') or 0),
                            'mood': mt.get('mood') or '',
                        }))
        except Exception as e:
            logger.warning('media moods: %s' % e)
        out.sort(key=lambda x: -(x.get('score') or 0))
        return out[:k * 2]
    except Exception as e:
        logger.warning('media library: %s' % e)
    return out


def _merge_hits(*groups) -> list:
    merged = {}
    order = []
    for group in groups:
        for hit in group or []:
            key = hit.get('id') or ''
            if key in merged:
                if hit.get('score', 0) > merged[key].get('score', 0):
                    merged[key] = hit
                continue
            merged[key] = hit
            order.append(key)
    return [merged[k] for k in order]


def _interleave_hits(*groups) -> list:
    """Чередовать группы по одному, сохраняя ранг внутри каждой.

    Для AI-чата важен сам факт присутствия личного источника в контексте:
    когда общая коллекция отдаёт k фрагментов, файлы файлового менеджера
    шли последними и обрезались по лимиту символов. Здесь результат общей
    базы, личной базы, медиа и файлов попадает в верх выдачи по кругу.
    """
    queues = [list(g or []) for g in groups]
    seen = set()
    out = []
    while any(queues):
        for q in queues:
            while q:
                hit = q.pop(0)
                key = hit.get('id') or ''
                if key in seen:
                    continue
                seen.add(key)
                out.append(hit)
                break
    return out


def _personal_hits(pid: str, query: str, k: int) -> list:
    """Личная коллекция профиля без файловых векторов.

    Файлы файлового менеджера приходят отдельной веткой _file_hits с
    лексическим запасом и превью; здесь нужны приватные вики/заметки/доки.
    """
    try:
        hit_list = qm.search(qm.personal_collection(pid), query, k)
    except Exception as e:
        logger.warning('personal semantic: %s' % e)
        return []
    return [h for h in hit_list
            if (h.get('payload') or {}).get('source') != 'file']


def _rank_mixed(hits: list) -> list:
    """Ранжирование «Файлового менеджера»: разные виды несравнимы по числу.

    Оценки картинок приходят из мультимодальной модели, текста — из текстовой,
    поэтому сортировка по сырому числу ставит вперёд случайные картинки.
    Сначала идут лексические совпадения (имя/OCR/текст), затем виды чередуются
    по одному, чтобы ни один не занимал всю выдачу.
    """
    best = {}
    for hit in hits or []:
        payload = hit.get('payload') or {}
        key = os.path.normcase(payload.get('path') or hit.get('id') or '')
        prev = best.get(key)
        if prev is None:
            best[key] = hit
            continue
        winner, loser = ((hit, prev) if hit.get('score', 0) > prev.get('score', 0)
                         else (prev, hit))
        wp = dict(winner.get('payload') or {})
        lp = loser.get('payload') or {}
        # Картинка/аудио/видео несут превью и плеер — при дубле пути оставляем
        # их вид, даже если текстовая ветка дала большую оценку.
        if lp.get('kind') in ('image', 'audio', 'video') and \
                wp.get('kind') in ('file', 'document', None):
            wp['kind'] = lp['kind']
            wp['type'] = lp.get('type') or lp['kind']
            for field in ('thumb', 'url', 'frame'):
                if not wp.get(field) and lp.get(field):
                    wp[field] = lp[field]
        if lp.get('lex'):
            wp['lex'] = True
        if not wp.get('text') and lp.get('text'):
            wp['text'] = lp['text']
        winner['payload'] = wp
        best[key] = winner

    lexical, semantic = [], {}
    for hit in best.values():
        payload = hit.get('payload') or {}
        if payload.get('lex'):
            lexical.append(hit)
            continue
        # Вектор без лексического попадания: отсекаем шум и отрицательные
        # косинусы, иначе в хвост выдачи лезут файлы с оценкой 3 из 100.
        if float(hit.get('score') or 0) < 20.0:
            continue
        kind = payload.get('kind') or payload.get('type') or 'file'
        semantic.setdefault(kind, []).append(hit)
    lexical.sort(key=lambda x: -(x.get('score') or 0))
    rest = []
    for items in semantic.values():
        items.sort(key=lambda x: -(x.get('score') or 0))
        rest.extend(items)
    rest.sort(key=lambda x: -(x.get('score') or 0))
    out = list(lexical)
    # Не больше двух подряд одного вида: сильные совпадения идут по оценке,
    # но пачка картинок не может вытеснить документы и наоборот.
    last_kinds = []
    while rest:
        pick = 0
        for i, hit in enumerate(rest):
            kind = (hit.get('payload') or {}).get('kind') or 'file'
            if last_kinds.count(kind) < 2:
                pick = i
                break
        hit = rest.pop(pick)
        kind = (hit.get('payload') or {}).get('kind') or 'file'
        last_kinds = (last_kinds + [kind])[-2:]
        out.append(hit)
    return out


@router.post('/api/search-all/semantic')
async def search_all_semantic(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    query = (body.get('query') or '').strip()
    k = int(body.get('k', 10))
    explicit = body.get('collection') or ''
    source = (body.get('source') or 'all').lower()
    collection = (explicit or
                  qm._qdrant_cfg().get('search_all_collection') or 'search_all_semantic')
    if not query:
        return _err('no query')
    if explicit:
        return _ok({'results': qm.search(explicit, query, k),
                    'collection': collection, 'source': source})
    pid = _profile(request)
    if source == 'media':
        results = _merge_hits(_media_hits(pid, query, k,
                                          {'image', 'audio', 'video'}),
                              _track_hits(pid, query, k))
    elif source == 'file':
        # Фильтр «Файловый менеджер» — всё, что пользователь положил в его
        # папки: текст, документы, картинки, аудио, видео.
        results = _rank_mixed(_merge_hits(_file_hits(pid, query, k),
                                          _media_hits(pid, query, k)))
    else:
        # Запрос «все источники» отвечает и на общую, и на личную базу
        # профиля, а файлы/медиа добавляются своими ветками. Группы чередуем:
        # иначе контекст AI-чата начинался с общей базы и личные файлы
        # обрезались лимитом символов.
        results = _interleave_hits(
            qm.search(collection, query, k),
            _personal_hits(pid, query, k),
            _media_hits(pid, query, k),
            _file_hits(pid, query, k),
        )
    return _ok({'results': results, 'collection': collection, 'source': source})
