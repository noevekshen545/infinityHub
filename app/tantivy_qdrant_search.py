#!/usr/bin/env python3
"""
Tantivy (BM25) + Qdrant (vector search) — альтернатива Whoosh + FAISS.

Tantivy — Rust BM25 поисковик (гораздо быстрее Whoosh).
Qdrant — векторная база данных (замена FAISS, с персистентностью).

Использует LM Studio для embeddings (jina-embeddings-v4-text-retrieval).
"""

import os
import json
import time
import hashlib
import logging
from collections import defaultdict
import urllib.request
import urllib.error
import swallow

try:
    import tantivy
    TANTIVY_AVAILABLE = True
except ImportError:
    swallow.note('tantivy_qdrant_search.<module>:L24')
    TANTIVY_AVAILABLE = False

try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import (
        Distance, VectorParams, PointStruct, Filter, FieldCondition,
        MatchValue, SearchRequest
    )
    QDRANT_AVAILABLE = True
except ImportError:
    swallow.note('tantivy_qdrant_search.<module>:L35')
    QDRANT_AVAILABLE = False

from config_module import get_config, get_api_key

_CFG = get_config()
_INDEX_CFG = _CFG.get("index", {})

def _get_base_url():
    _cfg = get_config()
    _backend = _cfg.get("backend", "lmstudio")
    if _backend == "llamacpp":
        return _cfg.get("llama_cpp", {}).get("url", "http://localhost:8080")
    return _cfg.get("lm_studio", {}).get("url", "http://localhost:1234")

LM_STUDIO_BASE_URL = _get_base_url()
LM_STUDIO_EMBED_URL = LM_STUDIO_BASE_URL + "/v1/embeddings"
LM_STUDIO_EMBED_MODEL = _CFG.get("lm_studio", {}).get("embed_model", "jina-embeddings-v4-text-retrieval")
LM_STUDIO_API_KEY = get_api_key(_CFG)

TANTIVY_INDEX_DIR = os.path.expanduser(_INDEX_CFG.get("tantivy_dir", "~/.infinity/tantivy_index"))
QDRANT_DIR = os.path.expanduser(_INDEX_CFG.get("qdrant_dir", "~/.infinity/qdrant"))
QDRANT_DIMENSION = 1024  # Jina v4 = 1024 (запасной HTTP-путь)

# ── Локальная модель эмбеддингов (ONNX) ───────────────────────────
# Раньше get_embeddings() умел ТОЛЬКО HTTP-запрос к <base>/v1/embeddings.
# Но base — это llama-server (:8080), а он эмбеддингов не отдаёт:
#   POST /v1/embeddings -> 501 Not Implemented
# Ошибка глоталась, возвращался пустой вектор, и Qdrant оставался с
# vector_count = 0 — векторная индексация не работала вообще.
#
# Теперь основной путь — локальный ONNX через onnxruntime: модель уже
# лежит в models\fastembed, работает офлайн и не требует внешнего
# сервиса. HTTP оставлен как запасной вариант.
_ONNX_CACHE_DIR = "models/fastembed"
_ONNX_BLOB_PREFIX = "models--qdrant--paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
_ONNX_MAX_LEN = 512

_onnx_session = None
_onnx_tokenizer = None
_onnx_ready = False
_onnx_failed = False


def _find_onnx_snapshot():
    """Найти распакованный snapshot модели в кэше fastembed."""
    try:
        snaps = os.path.join(_ONNX_CACHE_DIR, _ONNX_BLOB_PREFIX, "snapshots")
        if not os.path.isdir(snaps):
            return ""
        for name in sorted(os.listdir(snaps), reverse=True):
            cand = os.path.join(snaps, name)
            if os.path.exists(os.path.join(cand, "model_optimized.onnx")):
                return cand
    except Exception:
        swallow.note('tantivy_qdrant_search._find_onnx_snapshot:L90')
        pass
    return ""


def _init_onnx():
    """Один раз поднять ONNX-сессию и токенизатор. False — не вышло."""
    global _onnx_session, _onnx_tokenizer, _onnx_ready, _onnx_failed
    if _onnx_ready or _onnx_failed:
        return _onnx_ready
    snap = _find_onnx_snapshot()
    if not snap:
        _onnx_failed = True
        log.warning("Эмбеддинги: локальная ONNX-модель не найдена в %s",
                    _ONNX_CACHE_DIR)
        return False
    try:
        import onnxruntime as ort
        from tokenizers import Tokenizer
        sess = ort.InferenceSession(os.path.join(snap, "model_optimized.onnx"),
                                    providers=["CPUExecutionProvider"])
        tok = Tokenizer.from_file(os.path.join(snap, "tokenizer.json"))
        _onnx_session, _onnx_tokenizer = sess, tok
        _onnx_ready = True
        log.info("Эмбеддинги: локальная ONNX-модель загружена (%s)", snap)
        return True
    except Exception as e:  # noqa: BLE001
        _onnx_failed = True
        log.warning("Эмбеддинги: ONNX не поднялась: %s", e)
        return False


def _onnx_embed(texts):
    """Локальные эмбеддинги: mean pooling по маске + L2-нормализация."""
    try:
        import numpy as np
    except Exception:
        swallow.note('tantivy_qdrant_search._onnx_embed:L127')
        return None
    if not _init_onnx():
        return None
    try:
        _onnx_tokenizer.enable_truncation(max_length=_ONNX_MAX_LEN)
        _onnx_tokenizer.enable_padding()
        vectors = []
        batch_size = 16          # пачками, чтобы не жечь память
        for i in range(0, len(texts), batch_size):
            encs = _onnx_tokenizer.encode_batch(texts[i:i + batch_size])
            ids = np.array([e.ids for e in encs], dtype=np.int64)
            mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if len(_onnx_session.get_inputs()) >= 3:
                feed["token_type_ids"] = np.zeros_like(ids)
            out = _onnx_session.run(None, feed)[0]       # [batch, seq, dim]
            m = mask[..., None].astype(np.float32)
            summed = (out * m).sum(axis=1)                # mean pooling
            norms = np.linalg.norm(summed, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            vectors.extend((summed / norms).tolist())
        return vectors
    except Exception as e:  # noqa: BLE001
        log.warning("Эмбеддинги ONNX: ошибка вычисления: %s", e)
        return None


def get_embedding_dimension():
    """Размерность вектора: локальная модель 384, Jina v4 — 1024."""
    if _init_onnx():
        return 384
    return QDRANT_DIMENSION

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s')
log = logging.getLogger("tantivy_qdrant")


# ===================== EMBEDDINGS (LM Studio) =====================

def get_embeddings(texts, model=None):
    """Эмбеддинги: сначала локальная ONNX-модель, затем HTTP.

    Локальный путь добавлен потому, что единственный прежний вариант
    (HTTP к <base>/v1/embeddings) на llama-server отдаёт 501, ошибка
    глоталась, и Qdrant оставался пустым (vector_count = 0).
    """
    if not texts:
        return []
    if isinstance(texts, str):
        texts = [texts]

    # 1) Локальная ONNX — работает офлайн, без внешнего сервиса.
    local = _onnx_embed(list(texts))
    if local and len(local) == len(texts):
        return local

    # 2) Запасной HTTP-путь (LM Studio или сервис с эмбеддингами).
    model = model or LM_STUDIO_EMBED_MODEL
    # LM Studio принимает один текст или список
    payloads = []
    
    # LM Studio v4 API поддерживает input: list[str]
    payload = {
        "model": model,
        "input": texts,
        "encoding_format": "float"
    }
    
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            LM_STUDIO_EMBED_URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {LM_STUDIO_API_KEY}"} if LM_STUDIO_API_KEY else {})
            }
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode())
        
        embeddings = []
        for item in result.get("data", []):
            embeddings.append(item.get("embedding", []))
        
        return embeddings
    except Exception as e:
        log.error(f"Embedding error: {e}")
        return [[] for _ in texts]


# ===================== TANTIVY (BM25) =====================

class TantivyIndex:
    """Tantivy BM25 поисковый индекс."""

    def __init__(self, index_dir=None):
        self.index_dir = index_dir or TANTIVY_INDEX_DIR
        os.makedirs(self.index_dir, exist_ok=True)
        self.index = None
        self.schema = None
        self._load_or_create()

    def _build_schema(self):
        """Создаёт схему Tantivy."""
        builder = tantivy.SchemaBuilder()
        builder.add_text_field("doc_id", stored=True)
        builder.add_text_field("source", stored=True)
        builder.add_text_field("title", stored=True)
        builder.add_text_field("content", stored=False)  # Только для поиска
        builder.add_text_field("preview", stored=True)
        builder.add_text_field("file", stored=True)
        builder.add_integer_field("index_num", stored=True)
        builder.add_float_field("created", stored=True)
        return builder.build()

    def _load_or_create(self):
        """Загружает или создаёт индекс."""
        if not TANTIVY_AVAILABLE:
            return

        try:
            self.schema = self._build_schema()
            # Tantivy 0.25: всегда создаём новый индекс
            # reuse=True не работает корректно
            self.index = tantivy.Index(self.schema, path=self.index_dir)
            log.info(f"Tantivy индекс создан/загружен: {self.index_dir}")
        except Exception as e:
            log.error(f"Tantivy load error: {e}")
            import shutil
            try:
                if os.path.exists(self.index_dir):
                    shutil.rmtree(self.index_dir)
                os.makedirs(self.index_dir, exist_ok=True)
                self.schema = self._build_schema()
                self.index = tantivy.Index(self.schema, path=self.index_dir)
                log.warning(f"Tantivy: повреждённый индекс удалён и создан заново: {self.index_dir}")
            except Exception as e2:
                log.error(f"Tantivy re-create error: {e2}")
                self.index = None

    def rebuild(self, docs):
        """Пересоздаёт индекс."""
        if not TANTIVY_AVAILABLE or not docs:
            return 0

        self.schema = self._build_schema()
        # Удаляем старый индекс. Раньше здесь стоял голый shutil.rmtree,
        # и пересборка падала на Windows с
        #   PermissionError: [WinError 32] ... 'embeddings.db' занят
        # потому что каталог индекса общий с кэшем эмбеддингов (SQLite).
        # Теперь удаляем с повторами и не роняем пересборку из-за Windows.
        import shutil
        import time
        if os.path.exists(self.index_dir):
            for attempt in range(5):
                try:
                    shutil.rmtree(self.index_dir)
                    break
                except PermissionError as e:
                    if attempt == 4:
                        log.warning("Tantivy rebuild: не удалось удалить %s: %s "
                                    "— очищаю содержимое", self.index_dir, e)
                        try:
                            for name in os.listdir(self.index_dir):
                                fp = os.path.join(self.index_dir, name)
                                try:
                                    if os.path.isdir(fp):
                                        shutil.rmtree(fp, ignore_errors=True)
                                    else:
                                        os.remove(fp)
                                except OSError:
                                    swallow.note('tantivy_qdrant_search.rebuild:L300')
                                    pass
                        except OSError:
                            swallow.note('tantivy_qdrant_search.rebuild:L303')
                            pass
                        break
                    time.sleep(0.3)
        os.makedirs(self.index_dir, exist_ok=True)
        
        self.index = tantivy.Index(self.schema, path=self.index_dir)
        writer = self.index.writer(50_000_000)  # 50MB
        
        count = 0
        for doc in docs:
            try:
                tantivy_doc = tantivy.Document(
                    doc_id=doc.get("doc_id", ""),
                    source=doc.get("source", "unknown"),
                    title=doc.get("title", "")[:500],
                    content=doc.get("content", doc.get("content_original", ""))[:10000],
                    preview=doc.get("preview", "")[:500],
                    file=doc.get("file", ""),
                    index_num=doc.get("index_num", 0),
                    created=float(doc.get("created", 0)),
                )
                writer.add_document(tantivy_doc)
                count += 1
            except Exception as e:
                log.warning(f"Tantivy add doc error: {e}")
        
        writer.commit()
        # tantivy не переоткрывает читателя сам: без reload поиск и doc_count
        # до конца жизни процесса видят старый (пустой) коммит.
        try:
            self.index.reload()
        except Exception as e:
            log.warning(f"Tantivy reload error: {e}")
        log.info(f"Tantivy: {count} документов проиндексировано")
        return count

    def search(self, query, limit=50, filter_sources=None):
        """BM25 поиск через Tantivy."""
        if not TANTIVY_AVAILABLE or self.index is None:
            return []

        try:
            searcher = self.index.searcher()
            from tantivy import Occur

            words = query.lower().split()
            sub_queries = []
            for word in words:
                # Term query для title (с boost)
                sub_queries.append((Occur.Should, tantivy.Query.term_query(self.schema, "title", word)))
                # Term query для content
                sub_queries.append((Occur.Should, tantivy.Query.term_query(self.schema, "content", word)))

            if not sub_queries:
                return []

            query_obj = tantivy.Query.boolean_query(sub_queries)
            results = searcher.search(query_obj, limit=limit)
            hits = []

            for score, addr in results.hits:
                doc = searcher.doc(addr)
                hit = {
                    "doc_id": doc.get_first("doc_id") or "",
                    "source": doc.get_first("source") or "unknown",
                    "title": doc.get_first("title") or "",
                    "preview": doc.get_first("preview") or "",
                    "file": doc.get_first("file") or "",
                    "index_num": int(doc.get_first("index_num") or 0),
                    "score": score,
                    "created": float(doc.get_first("created") or 0),
                }
                hits.append(hit)

            return hits
        except Exception as e:
            log.error(f"Tantivy search error: {e}")
            import traceback
            traceback.print_exc()
            return []

    def bm25_search(self, query, limit=50, filter_sources=None):
        """Адаптер для совместимости с Whoosh interface."""
        return self.search(query, limit=limit, filter_sources=filter_sources)

    def doc_count(self):
        """Число документов в Tantivy-индексе (для статистики)."""
        if not TANTIVY_AVAILABLE or self.index is None:
            return 0
        try:
            # tantivy 0.22+ отдаёт num_docs полем-int, старые версии — методом.
            docs = self.index.searcher().num_docs
            return docs() if callable(docs) else int(docs)
        except Exception:
            swallow.note('tantivy_qdrant_search.doc_count:L390')
            return 0

    def clear(self):
        """Очистить Tantivy индекс."""
        if not TANTIVY_AVAILABLE:
            return
        import shutil
        if os.path.exists(self.index_dir):
            shutil.rmtree(self.index_dir)
        os.makedirs(self.index_dir, exist_ok=True)
        self.index = None
        self._load_or_create()


# ===================== QDRANT (Vector Search) =====================

class QdrantVectorStore:
    """Qdrant векторное хранилище (замена FAISS)."""

    def __init__(self, persist_dir=None, dimension=None):
        self.persist_dir = persist_dir or QDRANT_DIR
        # Размерность берём у реально используемой модели эмбеддингов,
        # а не из константы: локальная ONNX даёт 384, Jina v4 — 1024.
        # Несовпадение размерностей приводило бы к отказу индексации.
        self.dimension = dimension or get_embedding_dimension()
        self.client = None
        self.collection_name = "search_embeddings"
        os.makedirs(self.persist_dir, exist_ok=True)
        self._init_client()

    def _init_client(self):
        """Инициализирует Qdrant client."""
        if not QDRANT_AVAILABLE:
            return
        
        try:
            # Единый бэкенд через qdrant_module: локальный режим Qdrant
            # допускает только ОДИН клиент на storage-папку.
            import qdrant_module as _qm
            self.client = _qm.get_client()
            if self.client is None:
                log.warning("QdrantVectorStore: общий бэкенд недоступен — клиент не создан")
                return
            
            # Создаём коллекцию если нет
            collections = self.client.get_collections().collections
            exists = any(c.name == self.collection_name for c in collections)
            
            if not exists:
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(
                        size=self.dimension,
                        distance=Distance.COSINE,
                    ),
                )
                log.info(f"Qdrant коллекция создана: {self.collection_name} (dim={self.dimension})")
            else:
                log.info(f"Qdrant коллекция загружена: {self.collection_name}")
        except Exception as e:
            log.error(f"Qdrant init error: {e}")
            self.client = None

    def rebuild(self, doc_ids, vectors):
        """Пересоздаёт векторный индекс."""
        if not QDRANT_AVAILABLE or not self.client:
            return 0
        
        try:
            # Удаляем старые точки
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=Filter(must=[]),  # Все точки
            )
            
            # Добавляем новые
            points = []
            for i, (doc_id, vector) in enumerate(zip(doc_ids, vectors)):
                if not vector:
                    continue
                points.append(PointStruct(
                    id=i,
                    payload={"doc_id": doc_id},
                    vector=vector,
                ))
            
            if points:
                self.client.upsert(
                    collection_name=self.collection_name,
                    points=points,
                )
            
            log.info(f"Qdrant: {len(points)} векторов загружено")
            return len(points)
        except Exception as e:
            log.error(f"Qdrant rebuild error: {e}")
            return 0

    def search(self, query_vector, k=50):
        """Векторный поиск через Qdrant."""
        if not QDRANT_AVAILABLE or not self.client or not query_vector:
            return []
        
        try:
            # Qdrant client 1.17+: используем query_points
            results = self.client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                limit=k,
            )
            
            hits = []
            for r in results.points:
                hits.append((r.payload["doc_id"], r.score))
            
            return hits
        except Exception as e:
            log.error(f"Qdrant search error: {e}")
            return []


# ===================== FUZZY QUERY =====================

    def _preprocess_fuzzy_query(query):
        import re
        parts = re.split(r'("[^"]*")', query)
        result = []
        for part in parts:
            if part.startswith('"') and part.endswith('"'):
                result.append(part)
            else:
                tokens = part.split()
                fuzzy_tokens = []
                for token in tokens:
                    if '~' in token or len(token) <= 5 or not re.match(r'^[a-zA-Zа-яА-ЯёЁ0-9]+$', token):
                        fuzzy_tokens.append(token)
                    else:
                        distance = 1 if len(token) <= 8 else 2
                        fuzzy_tokens.append(f"{token}~{distance}")
                result.append(' '.join(fuzzy_tokens))
        return ' '.join(result)



