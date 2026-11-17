#!/usr/bin/env python3
'''
qdrant_module - interfeis k lokalnomu Qdrant dlya Infinity.

Ispolzuetsya:
  - kak bekend vektornoy pamyati chata (kollekciya chat_memory)
  - kak semanticheskiy indeks dlya search all i AI-chata (search_all_semantic)
  - kak proizvolnoe hranilische kollekciy, napolnyaemoe polzovatelem
    (celikom iz proektnyh istochnikov ILI vyborochno, ILI zagruzkoy s PK).

Rezhim: vstroennyy lokalnyy QdrantClient(path=...). Otdelnyy put,
chtoby ne konfliktovat s lokom poiskovogo dvizhka.
'''
import os
import re
import json
import time
import uuid
import logging
import threading
import concurrent.futures
import urllib.request
import urllib.parse
from typing import List, Dict, Any, Optional

NL = chr(10)
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s')
logger = logging.getLogger('qdrant_module')

# ===================== ZAVISIMOSTI =====================
try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import (Distance, VectorParams, PointStruct,
                                      Filter, FieldCondition, MatchValue)
    QDRANT_AVAILABLE = True
except Exception as e:
    QDRANT_AVAILABLE = False
    logger.warning('qdrant_client nedostupen: %s' % e)

def _cfg():
    # Базовый конфиг (может быть профильным и отбрасывать секцию 'qdrant')
    base = {}
    try:
        from config_module import get_config, _deep_merge
        base = get_config() or {}
    except Exception:
        base = {}
    # Секция 'qdrant' — канонический источник настроек векторной БД.
    # Профильная загрузка (config_module.load_config) может её не включить,
    # поэтому дочитываем qdrant напрямую из config.json рядом с модулем.
    try:
        _local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        if os.path.exists(_local):
            with open(_local, encoding='utf-8-sig') as _f:
                _disk = json.load(_f)
            _dq = _disk.get('qdrant')
            if _dq:
                _merged = dict(base)
                try:
                    _merged['qdrant'] = _deep_merge(base.get('qdrant', {}) or {}, _dq)
                except Exception:
                    _merged['qdrant'] = _dq
                base = _merged
    except Exception:
        pass
    return base


def _cfg_for(profile_id: str = ''):
    """Конфиг профиля для чтения источников; без профиля — прежний глобальный."""
    pid = str(profile_id or '').strip()
    if pid:
        try:
            import profiles_module
            cfg = profiles_module.get_effective_config(pid)
            if cfg:
                return cfg
        except Exception as e:
            logger.warning('profile cfg %s: %s' % (pid, e))
    return _cfg()


# ===================== KONFIGURACIYA =====================
def get_qdrant_dir() -> str:
    cfg = _cfg().get('qdrant', {})
    path = cfg.get('local_path') or '~/.infinity/qdrant_module'
    return os.path.expanduser(path)

def get_dimension() -> int:
    return int(_qdrant_cfg().get('dimension', _cfg().get('index', {}).get('dimension', 384)))

def _qdrant_cfg() -> Dict[str, Any]:
    return _cfg().get('qdrant', {})


# ===================== SCOPE: ОБЩАЯ / ЛИЧНАЯ БАЗА =====================
# У каждого профиля приложения своя коллекция. Выбор «общая или личная»
# хранится в конфиге профиля (qdrant.scope) и по умолчанию — общая:
# переписки и индексы, наработанные до появления выбора, не должны
# исчезнуть у пользователя.
def profile_scope(profile: str) -> str:
    """'shared' | 'personal' из конфига профиля приложения."""
    pid = str(profile or '').strip()
    if pid:
        try:
            import profiles_module
            cfg = profiles_module.get_profile_config(pid) or {}
            scope = str(((cfg.get('qdrant') or {}).get('scope') or 'shared')).lower()
            if scope == 'personal':
                return 'personal'
        except Exception as e:
            logger.debug('profile_scope(%s): %s' % (pid, e))
    return 'shared'


def personal_collection(profile: str) -> str:
    """Имя личной коллекции профиля (одинаковое для индексации и поиска)."""
    safe = re.sub(r'[^A-Za-z0-9_-]+', '_', str(profile or 'default')).strip('_')[:48]
    return 'personal_' + (safe or 'default')


def shared_collection() -> str:
    """Коллекция «общей» базы.

    У активного внешнего движка может быть своя коллекция (поле collection
    в ~/.infinity/qdrant_engines.json). Раньше оно сохранялось, но нигде не
    читалось: выбор кастомного движка не менял хранилище, и поиск продолжал
    ходить в локальную search_all_semantic.
    """
    try:
        eng = get_active_engine()
        name = str((eng or {}).get('collection') or '').strip()
        if name:
            return name
    except Exception as e:
        logger.debug('shared_collection engine: %s' % e)
    return _qdrant_cfg().get('search_all_collection') or 'search_all_semantic'


def collection_for(profile: str, scope: Optional[str] = None) -> str:
    """Коллекция для операций от имени профиля.

    scope явно (выбор в меню у кнопки) важнее настройки профиля.
    """
    sc = str(scope or '').strip().lower()
    if sc not in ('shared', 'personal'):
        sc = profile_scope(profile)
    if sc == 'personal':
        return personal_collection(profile)
    return shared_collection()

def get_embedding_backend() -> str:
    '''fastembed (lokalno, bez vneshnih LLM) ili lmstudio.'''
    return _qdrant_cfg().get('embedding_backend', 'fastembed').lower()

def get_embed_model() -> str:
    '''Model dlya fastembed (po umolchaniyu mulitiyazychnyy).

    Defolt 'intfloat/multilingual-e5-small' ne voshodyashchij v spisok modeley
    ustanovlennogo fastembed (v versii v etoy sredeke ih vsego 37), poetomu
    zagruzka padaла i vektory ne schitalis'. Est' real'no skachannaya
    lokal'naya model' — imenno ee ukazyvaem po umolchaniyu.'''
    return _qdrant_cfg().get('embed_model') or 'qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q'

def get_embed_device() -> str:
    '''Ustroystvo dlya ehmbeddingov: 'cpu' ili 'gpu' (CUDA).'''
    return str(_qdrant_cfg().get('embed_device', 'cpu')).strip().lower() or 'cpu'

def cuda_available() -> bool:
    '''Dostupen li CUDAExecutionProvider v tekuschem okruzhenii (onnxruntime).'''
    try:
        import onnxruntime
        return 'CUDAExecutionProvider' in getattr(onnxruntime, 'get_available_providers', lambda: [])()
    except Exception:
        return False

_embed_dim_cache = None

def get_embed_dim() -> int:
    '''Razmernost vektora tekuschego embedding-bekenda.'''
    global _embed_dim_cache
    if _embed_dim_cache:
        return _embed_dim_cache
    if get_embedding_backend() == 'fastembed':
        try:
            m = _fastembed_model()
            v = list(m.embed(['test']))[0]
            _embed_dim_cache = len(v)
            return _embed_dim_cache
        except Exception as e:
            logger.warning('fastembed dim detect failed: %s' % e)
    return get_dimension()

_fe_model = None
_fe_lock = threading.Lock()
_fe_last_fail = 0.0  # время последней неудачи загрузки модели (backoff против вечных ретраев сети)

# Пул для тяжёлого эмбеддинга: encode выполняется в отдельном потоке,
# чтобы не блокировать обработчик запросов (сервер не «замораживается»).
# Один воркер — fastembed-модель не thread-safe при параллельном создании.
_EMBED_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='fastembed')
_EMBED_STATE = {
    'loaded': False,        # модель загружена в память
    'busy': False,          # идёт ли сейчас эмбеддинг
    'last_op': '',          # что делаем (запрос / индексация)
    'current_text': '',     # сниппет обрабатываемого текста (для запроса — сам запрос)
    'processed': 0,         # обработано элементов в текущем пакете
    'total': 0,             # размер текущего пакета
    'last_error': None,     # последняя ошибка эмбеддинга
    'last_duration': 0.0,   # длительность последней операции, сек
    'device': 'cpu',        # запрошенное устройство (cpu/gpu)
}

def get_embed_cache_dir() -> str:
    '''Papka dlya lokalnogo kesha fastembed-modeli (postoyannaya, v proekte).

    Poe umolchaniyu ischetsya neskolko mest, beretsya pervoe NEPUSTOE:
    1) qdrant.embed_cache iz config.json (yavnoe ukazanie),
    2) <katalog modulya>/models/fastembed,
    3) <koren proekta>/models/fastembed.

    Do pravki bralsya tol'ko (2), a model pol'zovatel kladet v (3) — v (2)
    katalog sozdavalsya pustym, _model_present() videl "net modeli",
    fastembed sdaval po tryuke, stand 5 minut ne mog povtorit' popytku,
    i vektoryvyy poisk ne rabotal voobshche (vector_count = 0).'''
    cfg = _qdrant_cfg()
    explicit = cfg.get('embed_cache')
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    if explicit:
        candidates.append(os.path.expanduser(explicit))
    candidates.append(os.path.join(here, 'models', 'fastembed'))
    # <корень проекта> = на два уровня выше каталога модуля (app/qdrant_module.py).
    candidates.append(os.path.join(os.path.dirname(here), 'models', 'fastembed'))
    first = None
    for p in candidates:
        if first is None:
            first = p
        try:
            os.makedirs(p, exist_ok=True)
        except Exception:
            continue
        # Пустой кэш бесполезен — ищем тот, где реально что-то лежит.
        try:
            if os.listdir(p):
                return p
        except OSError:
            pass
    return first or candidates[0]

def _local_onnx_model():
    """Прямой запуск скачанной ONNX-модели через onnxruntime.

    Нужно потому, что установленный fastembed содержит всего 37 моделей,
    и 'qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q' среди них нет:
    TextEmbedding() падает с «is not supported in TextEmbedding».
    Но на диске лежит полный комплект (model_optimized.onnx + tokenizer.json),
    поэтому считаем эмбеддинги сами: mean pooling по маске + L2-нормализация.

    Возвращает объект с методом .embed(list[str]) -> list[list[float]]
    либо None, если ONNX-модель не найдена.
    """
    cache = get_embed_cache_dir()
    repo = 'models--qdrant--paraphrase-multilingual-MiniLM-L12-v2-onnx-Q'
    snaps = os.path.join(cache, repo, 'snapshots')
    onnx_dir = None
    try:
        if os.path.isdir(snaps):
            for name in sorted(os.listdir(snaps), reverse=True):
                cand = os.path.join(snaps, name)
                if os.path.exists(os.path.join(cand, 'model_optimized.onnx')):
                    onnx_dir = cand
                    break
    except OSError:
        pass
    if not onnx_dir:
        return None
    try:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
    except Exception as e:  # noqa: BLE001
        logger.warning('local onnx: нет зависимостей: %s' % e)
        return None

    class _LocalOnnxEmbedder:
        dim = 384

        def __init__(self):
            self.sess = ort.InferenceSession(
                os.path.join(onnx_dir, 'model_optimized.onnx'),
                providers=['CPUExecutionProvider'])
            self.tok = Tokenizer.from_file(
                os.path.join(onnx_dir, 'tokenizer.json'))
            self.tok.enable_truncation(max_length=512)
            self.tok.enable_padding()
            self._np = np

        def embed(self, texts):
            texts = list(texts)
            out_all = []
            for i in range(0, len(texts), 16):
                encs = self.tok.encode_batch(texts[i:i + 16])
                ids = self._np.array([e.ids for e in encs], dtype=self._np.int64)
                mask = self._np.array([e.attention_mask for e in encs],
                                      dtype=self._np.int64)
                feed = {'input_ids': ids, 'attention_mask': mask}
                if len(self.sess.get_inputs()) >= 3:
                    feed['token_type_ids'] = self._np.zeros_like(ids)
                out = self.sess.run(None, feed)[0]
                m = mask[..., None].astype(self._np.float32)
                summed = (out * m).sum(axis=1)
                norms = self._np.linalg.norm(summed, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                out_all.extend((summed / norms).tolist())
            return out_all

    try:
        emb = _LocalOnnxEmbedder()
        logger.info('local onnx: модель загружена из %s' % onnx_dir)
        return emb
    except Exception as e:  # noqa: BLE001
        logger.warning('local onnx: не загрузилась: %s' % e)
        return None


def _fastembed_model():
    """Singleton: модель создаётся ОДИН раз. Раньше новый TextEmbedding
    создавался на каждый вызов embed_texts - при первом использовании это
    качало ~120МБ onnx из сети и блокировало обработчик запроса на минуты.
    Model gruzitsya iz lokalnogo keshа (cache_dir); pri embed_local_only=True
    set ne lezet v set (offlayn)."""
    global _fe_model, _fe_last_fail
    if _fe_model is not None:
        return _fe_model
    # Backoff: если модель не загрузилась (нет сети/нет файлов) — не ретраим
    # каждую секунду (это вешало обработчики запросов), ждём 5 минут
    if time.time() - _fe_last_fail < 300:
        raise RuntimeError("fastembed model unavailable (recent load failed; offline?)")
    with _fe_lock:
        if _fe_model is None:
            try:
                from fastembed import TextEmbedding
                # Строго офлайн, если модель уже есть в кеше (вручную скачана),
                # либо если явно включён режим embed_local_only в config.json.
                present = _model_present(get_embed_cache_dir(), get_embed_model())
                local_only = bool(_qdrant_cfg().get('embed_local_only', False)) or present
                device = get_embed_device()
                if device == 'gpu':
                    providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
                else:
                    providers = ['CPUExecutionProvider']
                logger.info('fastembed: loading model %s (cache=%s, local_only=%s, present=%s, device=%s) ...' % (get_embed_model(), get_embed_cache_dir(), local_only, present, device))
                try:
                    _fe_model = TextEmbedding(model_name=get_embed_model(), cache_dir=get_embed_cache_dir(), local_files_only=local_only, providers=providers)
                except TypeError:
                    # Старые версии fastembed не принимают аргумент providers
                    _fe_model = TextEmbedding(model_name=get_embed_model(), cache_dir=get_embed_cache_dir(), local_files_only=local_only)
                _EMBED_STATE['loaded'] = True
                _EMBED_STATE['device'] = device
                logger.info('fastembed: model ready (device=%s)' % device)
            except Exception as e:
                # TextEmbedding не знает эту модель (в установленной версии
                # их всего 37). Пробуем напрямую через onnxruntime — комплект
                # файлов на диске полный. Без этого эмбеддинги не считались
                # и Qdrant оставался пустым.
                logger.warning('fastembed: %s — пробуем прямой ONNX' % e)
                _fe_model = _local_onnx_model()
                if _fe_model is None:
                    _fe_last_fail = time.time()
                    raise RuntimeError("fastembed load failed: %s" % e)
                _EMBED_STATE['loaded'] = True
                _EMBED_STATE['device'] = 'cpu'
                logger.info('local onnx: model ready (device=cpu)')
    return _fe_model

def warmup_embedding_backend():
    """Progrev embedding-bekenda (vyzivat v fone pri starte servera)."""
    try:
        if get_embedding_backend() == 'fastembed':
            _fastembed_model()
        logger.info('embedding backend warmup done')
    except Exception as e:
        logger.warning('embedding warmup failed: %s' % e)

def _embed_url() -> str:
    c = _cfg()
    backend = c.get('backend', 'lmstudio')
    if backend == 'llamacpp':
        base = c.get('llama_cpp', {}).get('url', 'http://localhost:8080')
    else:
        base = c.get('lm_studio', {}).get('url', 'http://localhost:1234')
    return base.strip().rstrip('/') + '/v1/embeddings'

def _embed_model() -> str:
    return _cfg().get('lm_studio', {}).get('embed_model') or ''


# ===================== KLIENT =====================
_client = None
_client_key = None
# Защита от "шторма создания клиентов": локальный режим Qdrant допускает
# только ОДИН клиент на storage-папку (flock). Раньше параллельные потоки
# одновременно пытались создать клиента — часть падала с ошибкой
# "Storage folder ... is already accessed by another instance" и повторяла
# попытки каждые несколько секунд, фризя сервер. Теперь создание под локом,
# а после неудачи — cooldown, чтобы не спамить захватами блокировки.
_client_lock = threading.Lock()
_client_fail_ts = 0.0
_CLIENT_FAIL_COOLDOWN = 30.0
ENGINES_FILE = os.path.expanduser('~/.infinity/qdrant_engines.json')


def get_engines():
    try:
        if os.path.exists(ENGINES_FILE):
            data = json.load(open(ENGINES_FILE, encoding='utf-8'))
            return data.get('engines', []) or []
    except Exception as e:
        logger.warning('get_engines error: %s' % e)
    return []


def get_active_engine():
    try:
        if os.path.exists(ENGINES_FILE):
            data = json.load(open(ENGINES_FILE, encoding='utf-8'))
            active = data.get('active') or ''
            for e in (data.get('engines') or []):
                if e.get('name') == active:
                    return e
    except Exception as e:
        logger.warning('get_active_engine error: %s' % e)
    return None


def get_client():
    global _client, _client_key, _client_fail_ts
    if not QDRANT_AVAILABLE:
        return None
    eng = get_active_engine()
    if eng:
        key = 'engine:' + eng.get('url', '') + '|' + (eng.get('api_key') or '')
        with _client_lock:
            if _client is not None and _client_key == key:
                return _client
            # Cooldown: после недавней неудачи не долбим сервер повторно
            if time.time() - _client_fail_ts < _CLIENT_FAIL_COOLDOWN:
                return None
            try:
                _client = QdrantClient(url=eng.get('url'), api_key=eng.get('api_key') or None)
                _client_key = key
                logger.info('Qdrant client (engine %s): %s' % (eng.get('name'), eng.get('url')))
            except Exception as e:
                logger.error('Qdrant engine init error: %s' % e)
                _client = None
                _client_key = None
                _client_fail_ts = time.time()
            return _client
    key = 'local:' + get_qdrant_dir()
    with _client_lock:
        if _client is not None and _client_key == key:
            return _client
        if time.time() - _client_fail_ts < _CLIENT_FAIL_COOLDOWN:
            return None
        try:
            os.makedirs(get_qdrant_dir(), exist_ok=True)
            _client = QdrantClient(path=get_qdrant_dir())
            _client_key = key
            logger.info('Qdrant client init: %s' % get_qdrant_dir())
        except Exception as e:
            logger.error('Qdrant init error: %s' % e)
            _client = None
            _client_key = None
            _client_fail_ts = time.time()
    return _client

def ensure_collection(name: str, dim: Optional[int] = None) -> bool:
    client = get_client()
    if not client:
        return False
    dim = dim or get_embed_dim()
    try:
        exists = any(c.name == name for c in client.get_collections().collections)
        if exists:
            info = client.get_collection(name)
            actual = info.config.params.vectors.size
            if actual != dim:
                logger.warning('Qdrant %s dim mismatch %s->%s, recreate' % (name, actual, dim))
                client.delete_collection(name)
                exists = False
        if not exists:
            client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
            logger.info('Qdrant collection created: %s dim=%s' % (name, dim))
        return True
    except Exception as e:
        logger.error('ensure_collection error: %s' % e)
        return False


# ===================== EMBEDDINGI =====================
_LAST_EMBED_ERROR = None   # последняя причина неудачи эмбеддингов (для UI/health)

def get_last_embed_error():
    return _LAST_EMBED_ERROR

def _run_embed(texts, batch_size=32):
    '''Rabochaya funkciya: encode vypolnyaetsya v otdelnom potoke (_EMBED_POOL),
    chtoby ne blokirovat osnovnoy obrabotchik zaprosov. Obnovlyaet _EMBED_STATE.'''
    m = _fastembed_model()
    out = []
    total = len(texts)
    for i in range(0, total, batch_size):
        chunk = texts[i:i + batch_size]
        for vec in m.embed(chunk):
            out.append(list(vec))
        _EMBED_STATE['processed'] = min(i + batch_size, total)
    return out

def embed_texts(texts: List[str], batch_size: int = 32, op_label: Optional[str] = None) -> List[Optional[List[float]]]:
    '''Poluchit embeddingi. Bekend: fastembed (lokalno) ili LM Studio.'''
    global _LAST_EMBED_ERROR
    if not texts:
        return []
    if get_embedding_backend() == 'fastembed':
        # Тяжёлый encode — в отдельный поток; сервер не блокируется.
        _EMBED_STATE['busy'] = True
        _EMBED_STATE['last_op'] = op_label or ('эмбеддинг %d текстов' % len(texts))
        _EMBED_STATE['current_text'] = (texts[0][:120] if texts else '')
        _EMBED_STATE['total'] = len(texts)
        _EMBED_STATE['processed'] = 0
        _EMBED_STATE['last_error'] = None
        t0 = time.time()
        try:
            out = _EMBED_POOL.submit(_run_embed, list(texts), batch_size).result(timeout=3600)
            _LAST_EMBED_ERROR = None
            return out
        except Exception as e:
            _EMBED_STATE['last_error'] = 'fastembed: %s' % e
            _LAST_EMBED_ERROR = 'fastembed: %s' % e
            logger.warning('fastembed error: %s (fallback na LM Studio)' % e)
        finally:
            _EMBED_STATE['busy'] = False
            _EMBED_STATE['last_duration'] = round(time.time() - t0, 3)
            _EMBED_STATE['current_text'] = ''
    # Fallback / yavnyy LM Studio
    url = _embed_url()
    model = _embed_model()
    out = []
    batch_err = None
    for i in range(0, len(texts), batch_size):
        chunk = texts[i:i + batch_size]
        try:
            payload = json.dumps({
                'model': model,
                'input': [t[:4000] for t in chunk],
                'encoding_format': 'float',
            }).encode()
            req = urllib.request.Request(url, data=payload,
                                         headers={'Content-Type': 'application/json'})
            api_key = _cfg().get('lm_studio', {}).get('api_key') or _cfg().get('api_key')
            if api_key:
                req.add_header('Authorization', 'Bearer %s' % api_key)
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read().decode())
            emb = result.get('data') or []
            for item in emb:
                out.append(item.get('embedding'))
        except Exception as e:
            # Понятная диагностика: без этого «не добавляет файлы» не объяснить
            body = ''
            try:
                body = getattr(e, 'read')().decode('utf-8', 'replace')[:200]
            except Exception:
                pass
            batch_err = (f'{url}: {type(e).__name__}: {e}' +
                         (f' — {body}' if body else '') +
                         (' (модель эмбеддингов не загружена?)' if model == '' else ''))
            logger.warning('embed error [%s]: %s' % (url, batch_err))
            out.extend([None] * len(chunk))
    if batch_err:
        _LAST_EMBED_ERROR = batch_err
    elif _LAST_EMBED_ERROR is None:
        _LAST_EMBED_ERROR = None
    return out

def embed_query(text: str) -> Optional[List[float]]:
    res = embed_texts([text], op_label='эмбеддинг поискового запроса')
    return res[0] if res else None


def embed_texts_submit(texts: List[str], batch_size: int = 32, op_label: str = None):
    '''Fire-and-forget: zapustit эмбеддинг в пule i vernut Future.
    UI mozhet oprachivat get_embed_state(), poka busy=True.'''
    if not texts:
        return None

    def _wrap():
        _EMBED_STATE['busy'] = True
        _EMBED_STATE['last_op'] = op_label or ('эмбеддинг %d текстов' % len(texts))
        _EMBED_STATE['current_text'] = (texts[0][:120] if texts else '')
        _EMBED_STATE['total'] = len(texts)
        _EMBED_STATE['processed'] = 0
        _EMBED_STATE['last_error'] = None
        t0 = time.time()
        try:
            return _run_embed(list(texts), batch_size)
        except Exception as e:
            _EMBED_STATE['last_error'] = 'fastembed: %s' % e
            raise
        finally:
            _EMBED_STATE['busy'] = False
            _EMBED_STATE['last_duration'] = round(time.time() - t0, 3)
            _EMBED_STATE['current_text'] = ''

    return _EMBED_POOL.submit(_wrap)


def get_embed_state() -> Dict[str, Any]:
    return dict(_EMBED_STATE)


# ===================== ZAGRUZKA MODELI (UI-knopka) =====================
_EMBED_DL = {
    'running': False, 'progress': None, 'downloaded': 0, 'total': 0,
    'stage': '', 'error': None, 'done': False, 'model': '',
}
_EMBED_DL_THREAD = None


def _dir_size(path: str) -> int:
    try:
        total = 0
        for root, _, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total
    except Exception:
        return 0


def _resolve_fastembed_info(model: str):
    '''Nayti zapisi o modeli v fastembed (dlya priblizitelnogo razmera).'''
    try:
        from fastembed import list_supported_models
        for m in list_supported_models():
            name = (m.get('model') or m.get('desc') or '')
            if name == model:
                return m
    except Exception:
        pass
    return None


def _model_present(cache: str, model: str) -> bool:
    try:
        import glob as _g
        return len(_g.glob(os.path.join(cache, '**', '*.onnx'), recursive=True)) > 0
    except Exception:
        return False


def _save_qdrant_cfg(patch: Dict[str, Any]) -> bool:
    '''Obnovit sekciyu 'qdrant' v config.json rodom s moduem.'''
    try:
        fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        data = {}
        if os.path.exists(fp):
            with open(fp, encoding='utf-8-sig') as f:
                data = json.load(f)
        dq = data.get('qdrant') or {}
        dq.update(patch)
        data['qdrant'] = dq
        with open(fp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.warning('save qdrant cfg error: %s' % e)
        return False


def set_embed_local_only(val: bool):
    _save_qdrant_cfg({'embed_local_only': bool(val)})


def embed_info():
    cache = get_embed_cache_dir()
    fa = False
    try:
        import fastembed  # noqa
        fa = True
    except Exception:
        fa = False
    st = get_embed_state()
    return {
        'model': get_embed_model(),
        'backend': get_embedding_backend(),
        'cache_dir': cache,
        'local_only': _qdrant_cfg().get('embed_local_only', False),
        'present': _model_present(cache, get_embed_model()),
        'fastembed_available': fa,
        'last_error': get_last_embed_error(),
        'device': get_embed_device(),
        'cuda_available': cuda_available(),
        'loaded': st.get('loaded', False),
        'busy': st.get('busy', False),
        'last_op': st.get('last_op', ''),
        'current_text': st.get('current_text', ''),
        'processed': st.get('processed', 0),
        'total': st.get('total', 0),
        'last_duration': st.get('last_duration', 0.0),
        'state': st,
    }


def download_embed_model() -> bool:
    '''Zapusk fonovoy zagruzki modeli. Vozvrashchaet False, esli uzhe idet.'''
    global _EMBED_DL_THREAD
    if _EMBED_DL.get('running'):
        return False
    t = threading.Thread(target=_download_embed_model_worker, daemon=True)
    t.start()
    _EMBED_DL_THREAD = t
    return True


def get_embed_download_status():
    return dict(_EMBED_DL)


def _download_embed_model_worker():
    global _fe_model
    import time
    model = get_embed_model()
    cache = get_embed_cache_dir()
    info = _resolve_fastembed_info(model)
    approx = (float((info or {}).get('size_in_GB') or 0.5)) * 1_000_000_000
    _EMBED_DL.update({
        'running': True, 'progress': 0, 'downloaded': 0, 'total': int(approx),
        'stage': 'Подготовка…', 'error': None, 'done': False, 'model': model,
    })
    monitor = [True]

    def _monitor():
        while monitor[0] and _EMBED_DL['running']:
            try:
                sz = _dir_size(cache)
                _EMBED_DL['downloaded'] = sz
                if approx:
                    _EMBED_DL['progress'] = min(99, int(100 * sz / approx))
            except Exception:
                pass
            time.sleep(0.7)

    mt = threading.Thread(target=_monitor, daemon=True)
    mt.start()
    try:
        from fastembed import TextEmbedding
        _EMBED_DL['stage'] = 'Скачивание модели %s …' % model
        # realnaya zagruzka + pervichnaya proverka (fastembed sam ka4aet
        # nujnyy HF-repozitoriy v cache_dir, poetomu fayly budut korrektnye)
        TextEmbedding(model_name=model, cache_dir=cache, local_files_only=False).embed(['проверка'])
        _EMBED_DL['stage'] = 'Модель загружена, переключаю в офлайн-режим…'
        _fe_model = None
        set_embed_local_only(True)
        _fastembed_model()  # proverka zagruzki oflayn (local_files_only=True)
        _EMBED_DL['progress'] = 100
        _EMBED_DL['stage'] = 'Готово'
        _EMBED_DL['done'] = True
    except Exception as e:
        _EMBED_DL['error'] = str(e)
        _EMBED_DL['stage'] = 'Ошибка загрузки'
    finally:
        monitor[0] = False
        _EMBED_DL['running'] = False


# ===================== CHANKIROVANIE =====================
def chunk_text(text: str, size: int = 1000, overlap: int = 100) -> List[str]:
    if not text:
        return []
    text = ' '.join(text.split())
    if len(text) <= size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap
    return chunks


# ===================== POLYCHENIE TEKSTA ELEMENTA =====================
def _parse_file_param(identifier: str) -> str:
    if identifier and identifier.startswith('/') and '?' in identifier:
        m = re.search(r'[?&]file=([^&]+)', identifier)
        if m:
            return m.group(1)
    return identifier

def _read_file_text(path: str) -> str:
    try:
        if path.endswith('.pdf'):
            try:
                from pypdf import PdfReader
                r = PdfReader(path)
                return NL.join((p.extract_text() or '') for p in r.pages)
            except Exception:
                return ''
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()
    except Exception as e:
        logger.warning('read file error %s: %s' % (path, e))
        return ''

def get_item_text(source: str, identifier: str, profile_id: str = '') -> str:
    '''Polnyy tekst elementa po istochniku i identifikatoru.'''
    source = (source or '').lower()
    if identifier:
        identifier = urllib.parse.unquote(identifier)
    try:
        if source == 'book':
            rel_raw = _parse_file_param(identifier)
            if isinstance(rel_raw, str) and rel_raw.startswith('collection://'):
                cid = rel_raw.split('://', 1)[1] if '://' in rel_raw else rel_raw
                try:
                    from modules.collection_module import get_book_content as _coll_book_content
                    _cid = int(cid) if str(cid).lstrip('-').isdigit() else cid
                    _data = _coll_book_content(_cid)
                    if isinstance(_data, dict):
                        _chapters = _data.get('chapters') or []
                        _text = _data.get('full_text') or '\n\n'.join((c.get('text') or '') for c in _chapters)
                        return _text or ''
                except Exception as e:
                    logger.warning('collection book text error %s: %s' % (rel_raw, e))
                return ''
            from server import get_book_content
            rel = rel_raw
            books_dir = _cfg_for(profile_id).get('books_dir', '')
            fp = rel
            if books_dir and not os.path.isabs(rel):
                fp = os.path.join(os.path.expanduser(books_dir), rel)
            if not os.path.exists(fp):
                fp = os.path.expanduser(rel)
            content = get_book_content(fp) if os.path.exists(fp) else ''
            if isinstance(content, dict):
                content = content.get('full_text') or content.get('text') or ''
            return content or ''
        if source == 'note':
            rel = _parse_file_param(identifier)
            notes_dir = _cfg_for(profile_id).get('notes_dir', '')
            notes_dir = os.path.expanduser(notes_dir) if notes_dir else ''
            candidates = []
            if rel:
                candidates.append(rel)
                if not rel.lower().endswith('.json'):
                    candidates.append(rel + '.json')
            if notes_dir:
                if rel:
                    candidates.append(os.path.join(notes_dir, rel))
                    if not rel.lower().endswith('.json'):
                        candidates.append(os.path.join(notes_dir, rel + '.json'))
                base = os.path.basename(rel)
                if base:
                    if not base.lower().endswith('.json'):
                        base = base + '.json'
                    for root, _, files_ in os.walk(notes_dir):
                        if base in files_:
                            candidates.append(os.path.join(root, base))
                            break
            fp = None
            for c in candidates:
                c = os.path.expanduser(c)
                if os.path.exists(c):
                    fp = c
                    break
            if fp:
                try:
                    data = json.load(open(fp, encoding='utf-8'))
                    return data.get('content') or data.get('text') or ''
                except Exception:
                    return _read_file_text(fp)
            return ''
        if source == 'ai':
            rel = _parse_file_param(identifier)
            ai_dir = _cfg_for(profile_id).get('ai_dir', '')
            fp = rel
            if ai_dir and not os.path.isabs(rel):
                fp = os.path.join(os.path.expanduser(ai_dir), rel)
            if not os.path.exists(fp):
                fp = os.path.expanduser(rel)
            if os.path.exists(fp):
                try:
                    data = json.load(open(fp, encoding='utf-8'))
                    q = data.get('question') or data.get('prompt') or ''
                    a = data.get('answer') or data.get('response') or ''
                    return ('Vopros: %s' % q) + (NL + NL) + ('Otvet: %s' % a)
                except Exception:
                    return _read_file_text(fp)
            return ''
        if source in ('zim', 'news', 'html', 'article'):
            import search_all_sources as _sas
            return _sas.get_article_content(identifier, source, profile_id) or ''
        if source == 'pc':
            fp = os.path.expanduser(_parse_file_param(identifier))
            return _read_file_text(fp)
    except Exception as e:
        logger.warning('get_item_text error %s/%s: %s' % (source, identifier, e))
    return ''


# ===================== PROSMOTR ISTOCHNIKOV =====================
def _id_from_url(url: str) -> str:
    if url and '?' in url:
        m = re.search(r'[?&]file=([^&]+)', url)
        if m:
            return m.group(1)
    return url

def browse_source(source: str, q: str = '', list_all: bool = False,
                  limit: int = 100, offset: int = 0,
                  source_id: str = None, profile_id: str = '') -> List[Dict[str, Any]]:
    '''Vernut spisok elementov istochnika dlya vybora (pick-list).'''
    source = (source or '').lower()
    results = []
    try:
        import search_all_sources as sas
        cfg = _cfg_for(profile_id)
        if source == 'books':
            if q:
                results = sas.search_books(q, limit, profile_id)
            elif list_all:
                bf = os.path.expanduser('~/.infinity/book_index.json')
                if os.path.exists(bf):
                    data = json.load(open(bf, encoding='utf-8'))
                    books = data.get('books', [])
                    pid = str(profile_id or '').strip()
                    if not pid:
                        # Общий индекс содержит книги всех профилей; пустой
                        # profile_id — это гость, а не «показать всё».
                        try:
                            import profiles_module as _PM
                            pid = _PM.get_default_profile_id() or ''
                        except Exception:
                            swallow.note('qdrant_module.browse_source:L951')
                            pid = ''
                    if pid:
                        bd = sas.get_dirs(pid)
                        roots = [bd.get('books_dir') or ''] + list(bd.get('books_dirs') or [])
                        books = [b for b in books if sas._under_any_root(
                            b.get('path') or b.get('rel_path') or '',
                            [r for r in roots if r])]
                    else:
                        books = []
                    for b in books[offset:offset + limit]:
                        rel = b.get('rel_path', '')
                        title = b.get('title', '')
                        author = b.get('author', '')
                        if author:
                            title = '%s - %s' % (title, author)
                        results.append({
                            'title': title,
                            'id': rel,
                            'url': '/books?file=%s' % rel,
                            'preview': 'Format: %s' % str(b.get('format', '')).upper(),
                            'source': 'book',
                        })
        elif source == 'notes':
            if q:
                results = sas.search_notes(q, limit, profile_id)
            elif list_all:
                notes_dir = os.path.expanduser(cfg.get('notes_dir', ''))
                if notes_dir and os.path.isdir(notes_dir):
                    files = []
                    for root, _, files_ in os.walk(notes_dir):
                        for fn in files_:
                            if fn.endswith('.json'):
                                files.append(os.path.join(root, fn))
                    for fp in files[offset:offset + limit]:
                        try:
                            d = json.load(open(fp, encoding='utf-8'))
                            title = d.get('title') or d.get('name') or os.path.basename(fp)
                        except Exception:
                            title = os.path.basename(fp)
                        rel = os.path.relpath(fp, notes_dir)
                        results.append({
                            'title': title,
                            'id': rel,
                            'url': '/notes?file=%s' % rel,
                            'preview': '',
                            'source': 'note',
                        })
        elif source == 'ai':
            if q:
                results = sas.search_ai_answers(q, limit, profile_id)
            elif list_all:
                ai_dir = os.path.expanduser(cfg.get('ai_dir', ''))
                if ai_dir and os.path.isdir(ai_dir):
                    fns = [f for f in os.listdir(ai_dir)
                           if f.startswith('ai_') and f.endswith('.json')]
                    for fn in fns[offset:offset + limit]:
                        results.append({
                            'title': fn,
                            'id': fn,
                            'url': '/ai_answers?file=%s' % fn,
                            'preview': '',
                            'source': 'ai',
                        })
        elif source == 'html':
            if q:
                results = sas.search_html(q, limit, profile_id)
            elif list_all:
                for hd in cfg.get('html_viewer_dirs', []):
                    hdir = os.path.expanduser(hd)
                    if not os.path.isdir(hdir):
                        continue
                    files = []
                    for root, _, files_ in os.walk(hdir):
                        for fn in files_:
                            if fn.endswith(('.html', '.htm')):
                                files.append(os.path.join(root, fn))
                    for fp in files[offset:offset + limit]:
                        rel = os.path.relpath(fp, hdir)
                        results.append({
                            'title': os.path.basename(fp),
                            'id': rel,
                            'url': '/html_viewer?path=%s' % rel,
                            'preview': '',
                            'source': 'html',
                        })
        elif source == 'zim':
            if q:
                results = sas.search_zim(q, limit, profile_id)
            elif list_all:
                if source_id:
                    try:
                        for a in list_zim_articles(source_id, limit):
                            results.append({'id': a['id'], 'title': a['title'], 'url': a['id'], 'source': 'zim'})
                    except Exception as e:
                        logger.warning('zim list_all: %s' % e)
                else:
                    logger.info('zim list_all bez source_id ne podderzhivaetsya')
        elif source == 'news':
            if q:
                results = sas.search_news(q, limit)
            elif list_all:
                try:
                    import news_module
                    arts = news_module.get_articles(query='', limit=min(int(limit), 500))
                    for a in (arts or []):
                        aid = a.get('url') or a.get('id') or ''
                        if aid:
                            results.append({'id': aid, 'title': a.get('title', ''), 'url': aid, 'source': 'news'})
                except Exception as e:
                    logger.warning('news list_all: %s' % e)
    except Exception as e:
        logger.warning('browse_source error %s: %s' % (source, e))
    # Нормализация: браузер и индексатор ожидают поле 'id'.
    # search_* возвращают 'url', но не 'id' — заполняем из url/path,
    # иначе при индексации id оказывается пустым ("zim/: пустой текст").
    norm = []
    for r in results:
        if not isinstance(r, dict):
            continue
        rid = r.get('id') or r.get('url') or r.get('path') or ''
        norm.append({
            'id': rid,
            'title': r.get('title', '') or '',
            'url': r.get('url') or rid,
            'preview': r.get('preview', '') or '',
            'source': r.get('source') or r.get('type') or source,
        })
    return norm


def list_zim_articles(zim_path, limit=2000):
    '''Список статей ZIM для индексации (id=url для get_article_content).

    Установленный python-libzim не умеет get_entry_by_id/итерацию по архиву,
    поэтому статьи собираем через SuggestionSearcher: перебираем однобуквенные
    префиксы и постранично выгружаем пути результатов. Пути резолвим в заголовки
    через get_entry_by_path, оставляя только text/html статьи.
    '''
    out = []
    seen = set()
    try:
        from libzim.reader import Archive
        from libzim.suggestion import SuggestionSearcher
        from urllib.parse import quote as _q
        arch = Archive(zim_path)
        fname = os.path.basename(zim_path)
        base = '/zim/view?file=%s&name=%s&path=' % (zim_path, _q(fname))

        def _add(path_str):
            if len(out) >= int(limit):
                return
            p = str(path_str)
            if not p or p in seen:
                return
            try:
                e = arch.get_entry_by_path(p)
                title = str(e.title or '')
                mt = ''
                try:
                    mt = str(getattr(getattr(e, 'item', None), 'mimetype', '') or '')
                except Exception:
                    pass
                if mt and not mt.startswith('text/html'):
                    return
                if not title:
                    title = p.rsplit('/', 1)[-1]
            except Exception:
                return
            seen.add(p)
            out.append({'id': base + _q(p, safe=''), 'title': title})

        ss = SuggestionSearcher(arch)
        alphabet = ([chr(c) for c in range(ord('a'), ord('z') + 1)]
                    + [chr(c) for c in range(ord('A'), ord('Z') + 1)]
                    + [str(d) for d in range(10)]
                    + list('абвгдеёжзийклмнопрстуфхцчшщъыьэюя')
                    + list('АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ'))
        for pref in alphabet:
            if len(out) >= int(limit):
                break
            try:
                rs = ss.suggest(pref)
                est = rs.getEstimatedMatches()
            except Exception:
                continue
            got = 0
            step = 500
            while got < est and len(out) < int(limit):
                got_batch = 0
                try:
                    for pth in rs.getResults(got, step):
                        _add(pth)
                        got_batch += 1
                except Exception:
                    break
                if got_batch == 0:
                    break
                got += got_batch
        return out
    except Exception as e:
        logger.warning('list_zim_articles error %s: %s' % (zim_path, e))
        return out


# ===================== INDEKSACIYA =====================
INDEX_PROGRESS = {'running': False, 'done': 0, 'total': 0, 'collection': '', 'errors': 0, 'started': 0, 'last_error': None}

def _index_one(collection: str, source: str, item_id: str, title: str, text: str, extra: Dict[str, Any] = None) -> int:
    '''Razbit na chanki, embed, upsert. Vozvrashchaet chislo chankov.'''
    client = get_client()
    if not client:
        return 0
    text = text or ''
    if len(text) > 200000:
        text = text[:200000]
    chunks = chunk_text(text)
    if not chunks:
        return 0
    # BUGFIX: раньше было `vectors = [v for v in embed_texts(chunks) if v]`
    # и затем chunks[i] — при частичных сбоях эмбеддингов индексы
    # сдвигались и текст попадал не в свой вектор (порча данных).
    # Теперь чанк и его вектор идут строго парой.
    pairs = [(c, v) for c, v in zip(chunks, embed_texts(chunks)) if v]
    if not pairs:
        logger.warning('_index_one: net vektorov dlya %s/%s (%s)' % (
            source, item_id, get_last_embed_error() or 'embeddings backend недоступен'))
        return 0
    dim = len(pairs[0][1])
    if not ensure_collection(collection, dim=dim):
        return 0
    points = []
    for i, (chunk, vec) in enumerate(pairs):
        pid = str(uuid.uuid4())
        payload = {
            'source': source,
            'item_id': item_id,
            'title': title,
            'text': chunk,
            'chunk_index': i,
            'collection': collection,
        }
        if extra:
            payload.update(extra)
        points.append(PointStruct(
            id=pid,
            vector=vec,
            payload=payload,
        ))
    if points:
        client.upsert(collection_name=collection, points=points)
    return len(points)

def add_texts(collection: str, items: List[Dict[str, Any]],
              on_progress=None) -> Dict[str, Any]:
    '''items: [{source, id, title, text, extra?}]. Vozvrashchaet statistiku.

    on_progress(done, total, errors) — опциональный колбэк прогресса.
    '''
    indexed = 0
    chunks = 0
    errors = 0
    total = len(items)
    done = 0
    last_error = None
    for it in items:
        try:
            n = _index_one(collection, it.get('source', ''), it.get('id', ''),
                           it.get('title', ''), it.get('text', ''), it.get('extra'))
            if n > 0:
                indexed += 1
                chunks += n
            else:
                errors += 1
                if last_error is None:
                    last_error = get_last_embed_error() or \
                        f'{it.get("source", "")}/{it.get("id", "")}: пустой текст или нет векторов'
        except Exception as e:
            logger.warning('add_texts error: %s' % e)
            errors += 1
            if last_error is None:
                last_error = str(e)
        done += 1
        if on_progress:
            try:
                on_progress(done, total, errors)
            except Exception:
                pass
    return {'indexed_items': indexed, 'chunks': chunks, 'errors': errors,
            'last_error': last_error}

def index_single(collection: str, source: str, item_id: str,
                title: Optional[str] = None, text: Optional[str] = None,
                extra: Optional[Dict[str, Any]] = None,
                profile_id: str = '') -> Dict[str, Any]:
    '''Proindeksirovat odin element po istochniku i id.
    Esli text=None - tekst izvlekaetsya avtomaticheski cherez get_item_text.'''
    source = (source or '').lower()
    if item_id:
        item_id = urllib.parse.unquote(item_id)
    if text is None:
        text = get_item_text(source, item_id, profile_id)
    if not text or not text.strip():
        return {'indexed_items': 0, 'chunks': 0, 'errors': 1, 'empty': True}
    res = add_texts(collection, [{'source': source, 'id': item_id,
                                  'title': title or item_id, 'text': text,
                                  'extra': extra}])
    res['empty'] = False
    return res


def index_items(collection: str, source: str = 'books',
                items: Optional[List[Dict[str, Any]]] = None,
                whole_source: bool = False,
                paths: Optional[List[str]] = None,
                profile_id: str = '') -> Dict[str, Any]:
    '''Proindeksirovat vybrannoe.
    - source=pc + paths: zagruzka s PK
    - whole_source=True: ves istochnik
    - items: vyborochnye elementy [{id, title}]
    '''
    source = (source or '').lower()
    work = []
    INDEX_PROGRESS['running'] = True
    INDEX_PROGRESS['done'] = 0
    INDEX_PROGRESS['total'] = 0
    INDEX_PROGRESS['errors'] = 0
    INDEX_PROGRESS['collection'] = collection
    INDEX_PROGRESS['started'] = time.time()
    if source == 'pc':
        for p in (paths or []):
            p = urllib.parse.unquote(p)
            fp = os.path.expanduser(p)
            work.append({'source': 'pc', 'id': p, 'title': os.path.basename(p),
                         'text': _read_file_text(fp)})
    elif whole_source:
        for it in browse_source(source, list_all=True, limit=100000,
                                profile_id=profile_id):
            iid = urllib.parse.unquote(it.get('id', it.get('url', '')))
            text = get_item_text(source, iid, profile_id)
            work.append({'source': source, 'id': iid,
                         'title': it.get('title', ''), 'text': text})
    else:
        for it in (items or []):
            iid = urllib.parse.unquote(it.get('id') or it.get('url') or '')
            text = get_item_text(source, iid, profile_id)
            work.append({'source': source, 'id': iid,
                         'title': it.get('title', ''), 'text': text})
    INDEX_PROGRESS['total'] = len(work)
    def _progress(done, total_n, errors):
        INDEX_PROGRESS['done'] = done
        INDEX_PROGRESS['total'] = total_n
        INDEX_PROGRESS['errors'] = errors
    result = add_texts(collection, work, on_progress=_progress)
    INDEX_PROGRESS['running'] = False
    INDEX_PROGRESS['last_error'] = result.get('last_error')
    return result


# ===================== POISK I KONTEXT =====================
def search(collection: str, query: str, k: int = 10) -> List[Dict[str, Any]]:
    client = get_client()
    if not client or not query:
        return []
    try:
        vec = embed_query(query)
        if not vec:
            return []
        res = client.query_points(collection_name=collection, query=vec, limit=k)
        out = []
        for p in res.points:
            out.append({'id': p.id, 'score': float(p.score),
                        'payload': p.payload})
        return out
    except Exception as e:
        logger.warning('search error: %s' % e)
        return []

def search_filtered(collection: str, query: str, k: int = 10,
                    must: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    '''search() с обязательным фильтром по payload (например, source=file).

    Личная коллекция профиля смешивает заметки, файлы и прочее; без фильтра
    поиск по ней не может ответить «только файлы файлового менеджера».'''
    client = get_client()
    if not client or not query:
        return []
    try:
        vec = embed_query(query)
        if not vec:
            return []
        qfilter = None
        if must:
            try:
                qfilter = Filter(must=[
                    FieldCondition(key=str(key), match=MatchValue(value=value))
                    for key, value in must.items()])
            except Exception as e:
                logger.warning('search_filtered filter: %s' % e)
                qfilter = None
        kwargs = {'collection_name': collection, 'query': vec, 'limit': k}
        if qfilter is not None:
            kwargs['query_filter'] = qfilter
        res = client.query_points(**kwargs)
        out = []
        for p in res.points:
            out.append({'id': p.id, 'score': float(p.score),
                        'payload': p.payload})
        return out
    except Exception as e:
        logger.warning('search_filtered error: %s' % e)
        return []


def retrieve_context(query: str, collection: str, k: int = 5,
                     max_chars: int = 4000) -> str:
    '''Sobrat tekst top-k rezultatov dlya konteksta AI-chata.'''
    results = search(collection, query, k)
    parts = []
    total = 0
    for r in results:
        txt = (r.get('payload') or {}).get('text', '')
        title = (r.get('payload') or {}).get('title', '')
        if txt:
            block = ('[%s] %s' % (title, txt)) if title else txt
            if total + len(block) > max_chars:
                block = block[:max(0, max_chars - total)]
            parts.append(block)
            total += len(block)
        if total >= max_chars:
            break
    return NL.join(parts)

def augment_semantic_prompt(question: str, base_prompt: str = '',
                            profile: str = 'default') -> str:
    '''Dobavit semanticheskiy kontekst iz Qdrant v system_prompt dlya AI-chata.

    Как и векторный режим /search-all, голосовой чат смотрит в общую базу,
    личную базу профиля и векторы файлового менеджера. Раньше бралась только
    одна коллекция (по умолчанию общая), поэтому личные материалы профиля в
    промпт не попадали вовсе.
    '''
    try:
        cfg = _qdrant_cfg()
        if not cfg.get('ai_chat_enabled', False):
            return base_prompt
        pid = str(profile or 'default')
        shared = collection_for(pid, 'shared')
        personal = collection_for(pid, 'personal')
        groups = [search(shared, question, 5)]
        if personal and personal != shared:
            try:
                groups.append([h for h in search(personal, question, 5)
                               if (h.get('payload') or {}).get('source') != 'file'])
            except Exception as e:
                logger.debug('augment_semantic_prompt personal: %s' % e)
        try:
            import module_flags as _mf
            from modules import file_index as _fi
            if _mf.enabled_for('file_index', pid) and _fi.search_ready(pid) \
                    and _fi.get_settings(pid).get('vector'):
                groups.append(search_filtered(personal, question, 5,
                                              {'source': 'file'}))
        except Exception as e:
            logger.debug('augment_semantic_prompt files: %s' % e)
        # Чередуем источники, чтобы общая база не вытеснила личные фрагменты
        # по лимиту символов (та же причина, что и в _qdrant_chat_context).
        queues = [list(g or []) for g in groups]
        seen = set()
        parts, total = [], 0
        while any(queues) and total < 4000:
            for q in queues:
                while q:
                    hit = q.pop(0)
                    key = hit.get('id') or ''
                    if key in seen:
                        continue
                    seen.add(key)
                    payload = hit.get('payload') or {}
                    txt = str(payload.get('text') or '').strip()
                    if not txt:
                        continue
                    title = str(payload.get('title') or '')
                    block = ('[%s] %s' % (title, txt)) if title else txt
                    block = block[:1200]
                    if total + len(block) > 4000:
                        block = block[:max(0, 4000 - total)]
                    parts.append(block)
                    total += len(block)
                    break
        ctx = NL.join(parts)
        if not ctx:
            return base_prompt
        tag = 'Vot relevantnye semanticheskie fragmenty iz vektornoy bazy Qdrant (search_all_semantic):'
        if base_prompt:
            return base_prompt + NL + NL + tag + NL + ctx
        return tag + NL + ctx
    except Exception as e:
        logger.warning('augment_semantic_prompt error: %s' % e)
        return base_prompt


def _collection_count(info) -> int:
    """Версия-независимый подсчёт векторов в коллекции Qdrant.

    qdrant-client 1.18+ переименовал поле CollectionInfo: vectors_count
    отсутствует, есть points_count / indexed_vectors_count. Пробуем по
    порядку, чтобы не падать AttributeError на любых версиях.
    """
    for attr in ("points_count", "indexed_vectors_count", "vectors_count"):
        v = getattr(info, attr, None)
        if v:
            return int(v)
    return 0


# ===================== UPRAVLENIE KOLLEKCIYAMI =====================
def list_collections() -> List[Dict[str, Any]]:
    client = get_client()
    if not client:
        return []
    try:
        out = []
        for c in client.get_collections().collections:
            try:
                info = client.get_collection(c.name)
                cnt = _collection_count(info)
            except Exception:
                cnt = -1
            out.append({'name': c.name, 'count': cnt})
        return out
    except Exception as e:
        logger.warning('list_collections error: %s' % e)
        return []

def collection_info(name: str) -> Dict[str, Any]:
    client = get_client()
    if not client:
        return {'name': name, 'exists': False}
    try:
        info = client.get_collection(name)
        return {
            'name': name,
            'exists': True,
            'count': _collection_count(info),
            'dimension': info.config.params.vectors.size,
            'distance': str(info.config.params.vectors.distance),
        }
    except Exception:
        return {'name': name, 'exists': False}

def count(name: str) -> int:
    info = collection_info(name)
    return info.get('count', 0) if info.get('exists') else 0


def collection_source_stats(name: str, limit: int = 200000) -> Dict[str, Any]:
    '''Sveraznuy statistiku kolekcii: skolko chankov po source, skolko unikalnyh
    istochnikov (item_id) v kazhdom source.'''
    client = get_client()
    if not client:
        return {'name': name, 'exists': False, 'total_points': 0, 'by_source': {}}
    total = 0
    by_source = {}
    seen_items = set()
    try:
        limit = int(limit)
    except Exception:
        limit = 200000
    offset = None
    while total < limit:
        try:
            # scroll возвращает (points, next_offset); with_payload=True, без векторов (быстрее)
            batch, offset = client.scroll(
                collection_name=name,
                limit=1000,
                with_vectors=False,
                with_payload=True,
                offset=offset,
            )
        except Exception as e:
            logger.warning('collection_source_stats scroll error: %s' % e)
            break
        if not batch:
            break
        for pt in batch:
            total += 1
            payload = pt.payload or {}
            src = payload.get('source') or 'unknown'
            item_id = payload.get('item_id') or payload.get('title') or ''
            st = by_source.setdefault(src, {'chunks': 0, 'items': set()})
            st['chunks'] += 1
            if item_id:
                kk = src + '::' + str(item_id)
                if kk not in seen_items:
                    seen_items.add(kk)
                    st['items'].add(item_id)
        if offset is None:
            break
        if total >= limit:
            break
    # convert sets to counts; also resolve friendly labels
    friendly = {
        'book': 'Книги', 'books': 'Книги', 'note': 'Заметки', 'notes': 'Заметки',
        'zim': 'Вики (ZIM)', 'news': 'Новости', 'ai': 'Ответы AI', 'ai_answer': 'Ответы AI',
        'html': 'HTML', 'pdf': 'PDF', 'pc': 'Файлы с ПК', 'file': 'Файлы',
        'chat': 'Чат-память', 'memory': 'Память', 'collection': 'Коллекции',
    }
    by_source_out = {}
    for src, st in by_source.items():
        by_source_out[src] = {
            'chunks': st['chunks'],
            'items': len(st['items']),
            'label': friendly.get(src, src),
        }
    return {
        'name': name,
        'exists': True,
        'total_points': total,
        'by_source': by_source_out,
    }

def clear(name: str) -> bool:
    client = get_client()
    if not client:
        return False
    try:
        client.delete_collection(name)
        ensure_collection(name)
        return True
    except Exception as e:
        logger.warning('clear error: %s' % e)
        return False

def delete_collection(name: str) -> bool:
    client = get_client()
    if not client:
        return False
    try:
        client.delete_collection(name)
        return True
    except Exception as e:
        logger.warning('delete_collection error: %s' % e)
        return False

def delete_points(name: str, ids: List[str]) -> bool:
    client = get_client()
    if not client or not ids:
        return False
    try:
        # batching: bolshie spiski (ochistka dublikatov) ne dolzhny valit odin zapros
        B = 500
        for i in range(0, len(ids), B):
            client.delete(collection_name=name,
                          points_selector=list(ids[i:i + B]))
        return True
    except Exception as e:
        logger.warning('delete_points error: %s' % e)
        return False

def delete_points_by_filter(name: str, must: Optional[Dict[str, Any]] = None) -> bool:
    '''Udalit tochki po tochnym znacheniyam payload (AND).

    Nuzhno dlya pereindeksacii stranicy wiki ili fajla: starye chanki udalyayutsya
    do dobavleniya novyh, i dublikaty ne kopятся. FilterSelector rabotaet i na
    lokalnom, i na vneshnem dvizhke.
    '''
    client = get_client()
    if not client or not must:
        return False
    try:
        from qdrant_client.models import Filter as QFilter, FieldCondition, MatchValue, FilterSelector
        conditions = [
            FieldCondition(key=str(k), match=MatchValue(value=v))
            for k, v in must.items() if v is not None
        ]
        if not conditions:
            return False
        client.delete(collection_name=name,
                      points_selector=FilterSelector(filter=QFilter(must=conditions)))
        return True
    except Exception as e:
        logger.warning('delete_points_by_filter error: %s' % e)
        return False

def health() -> Dict[str, Any]:
    eng = get_active_engine()
    d = {
        'available': QDRANT_AVAILABLE,
        # Режим раньше всегда писался 'local', даже когда активен внешний
        # движок из настроек — статус в UI вводил в заблуждение.
        'mode': 'remote' if eng else 'local',
        'engine': (eng.get('name') if eng else None),
        'url': (eng.get('url') if eng else None),
        'path': (None if eng else (get_qdrant_dir() if QDRANT_AVAILABLE else None)),
        'collection': shared_collection(),
        'collections': list_collections() if QDRANT_AVAILABLE else [],
        'embedding_backend': get_embedding_backend(),
        'embed_url': _embed_url(),
        'fastembed_available': None,
        'last_embed_error': get_last_embed_error(),
    }
    try:
        from fastembed import TextEmbedding  # noqa: F401
        d['fastembed_available'] = True
    except Exception as fe:
        d['fastembed_available'] = False
        if not d['last_embed_error']:
            d['last_embed_error'] = f'fastembed не установлен ({fe}); fallback на {_embed_url()}'
    return d
