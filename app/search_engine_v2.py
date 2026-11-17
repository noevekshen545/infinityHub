#!/usr/bin/env python3
"""
Гибридный поисковый движок v3:
Tantivy BM25 + Qdrant ANN + RRF Fusion + Intent Detection + Passage Retrieval
+ fuzzy search + phrase search + cross-encoder reranking + recency boost
+ spell correction (SymSpell) + query expansion (synonyms) + hybrid chunking.

Архитектура:
  Запрос → Spell Correction → Query Expansion (synonyms) → Stemming
         → [BM25 (Tantivy, fuzzy+phrase)] + [Qdrant (Embeddings)]
         → RRF Fusion → Recency Boost → Cross-Encoder Reranking → Результаты

Модель эмбеддингов: jina-embeddings-v4-text-retrieval (LM Studio, dim=1024)
Модель reranking: text-embedding-bge-reranker-v2-m3 (LM Studio)
"""

import os, json, re, math, time, hashlib, sqlite3, logging
from urllib.parse import quote
import urllib.request
import urllib.error
from collections import defaultdict
from functools import lru_cache
import swallow

# ===================== КОНФИГУРАЦИЯ =====================

# Загружаем конфигурацию из config.json
try:
    from config_module import get_config, get_api_key
    _CFG = get_config()
except Exception:
    swallow.note('search_engine_v2.<module>:L31')
    _CFG = {}

_LM_CFG = _CFG.get("lm_studio", {})
_LLAMA_CPP_CFG = _CFG.get("llama_cpp", {})
_INDEX_CFG = _CFG.get("index", {})

def _get_base_url():
    _cfg = get_config()
    _backend = _cfg.get("backend", "lmstudio")
    if _backend == "llamacpp":
        return _cfg.get("llama_cpp", {}).get("url", "http://localhost:8080").strip()
    return _cfg.get("lm_studio", {}).get("url", "http://localhost:1234").strip()

LM_STUDIO_BASE_URL = _get_base_url()
LM_STUDIO_URL = LM_STUDIO_BASE_URL + "/v1/chat/completions"
LM_STUDIO_EMBED_URL = LM_STUDIO_BASE_URL + "/v1/embeddings"
LM_STUDIO_MODEL = _LM_CFG.get("model", "gemma-3-1b-it@q8_0")
LM_STUDIO_EMBED_MODEL = _LM_CFG.get("embed_model", "nomic-ai/nomic-embed-text-v1.5-GGUF")
LM_STUDIO_API_KEY = get_api_key(_CFG) if _CFG else os.environ.get("LM_STUDIO_API_KEY", "")

INDEX_DIR = os.path.expanduser(_INDEX_CFG.get("dir", "~/.infinity/index"))
FAISS_INDEX_PATH = os.path.expanduser(_INDEX_CFG.get("faiss_index_path", "~/.infinity/index/faiss.index"))
FAISS_MAP_PATH = os.path.join(INDEX_DIR, "faiss_map.json")
EMBEDDING_DB_PATH = os.path.expanduser(_INDEX_CFG.get("embedding_db_path", "~/.infinity/index/embeddings.db"))
DIMENSION = _INDEX_CFG.get("dimension", 768)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s')
log = logging.getLogger("search_v3")
# BUGFIX: флаг, чтобы сообщение о недоступном Qdrant-бэкенде писалось один раз,
# а не при каждой пересборке HybridSearch (иначе спамило в лог сотнями строк).
_QDRANT_BACKEND_WARNED = False

# ===================== ЗАВИСИМОСТИ =====================

WHOOSH_AVAILABLE = False
try:
    from whoosh.index import create_in, open_dir, exists_in
    from whoosh.fields import Schema, TEXT, ID, STORED, NUMERIC
    from whoosh.qparser import MultifieldParser, OrGroup
    from whoosh.qparser.plugins import FuzzyTermPlugin, PhrasePlugin
    from whoosh.analysis import RegexTokenizer, StandardAnalyzer
    from whoosh.scoring import BM25F
    from whoosh.query import Term, Or as WhooshOr
    WHOOSH_AVAILABLE = True
except ImportError:
    log.warning("Whoosh не установлен")

import numpy as np
FAISS_AVAILABLE = False
try:
    import faiss
    FAISS_AVAILABLE = True
except ImportError:
    log.warning("FAISS не установлен: pip install faiss-cpu")

# Tantivy — быстрый BM25 поисковик (Rust-based, приоритет над Whoosh)
TANTIVY_AVAILABLE = False
_TANTIVY_INDEX = None
try:
    import tantivy
    TANTIVY_AVAILABLE = True
except ImportError:
    log.warning("Tantivy не установлен: pip install tantivy")

# Qdrant — альтернатива FAISS (приоритет по умолчанию)
QDRANT_AVAILABLE = False
_QDRANT_CLIENT = None
try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams, PointStruct, Filter
    QDRANT_AVAILABLE = True
except ImportError:
    swallow.note('search_engine_v2.<module>:L104')
    pass

NLTK_AVAILABLE = False
try:
    from nltk.stem import PorterStemmer
    _porter = PorterStemmer()
    NLTK_AVAILABLE = True
except ImportError:
    log.warning("NLTK не установлен, английский стемминг отключён")

# pymorphy3 для русской лемматизации (лучше чем стеммер Портера)
PYMORPHY_AVAILABLE = False
try:
    import pymorphy3
    _morph = pymorphy3.MorphAnalyzer()
    PYMORPHY_AVAILABLE = True
except ImportError:
    log.info("pymorphy3 не установлен, используется стеммер Портера для русского (pip install pymorphy3)")

# ===================== RERANKER (LM Studio auto-load + PyTorch fallback) =====================

# Модели для reranking
RERANKER_MODEL_LM_STUDIO = "text-embedding-bge-reranker-v2-m3"
RERANKER_MODEL_PYTORCH = "BAAI/bge-reranker-v2-m3"
RERANKER_MAX_LENGTH = 512

# LLM API URLs
LM_STUDIO_BASE_URL_RERANK = _get_base_url()
LM_STUDIO_EMBED_URL = LM_STUDIO_BASE_URL_RERANK + "/v1/embeddings"
LM_STUDIO_MODELS_URL = LM_STUDIO_BASE_URL_RERANK + "/v1/models"
LM_STUDIO_V1_LOAD = LM_STUDIO_BASE_URL_RERANK + "/v1/internal/model/load"

# Состояние reranker
_LM_RERANKER_AVAILABLE = False
_LM_RERANKER_CHECKED = False
_LM_RERANKER_MODEL_ID = None
_LM_EMBED_AVAILABLE = False
_LM_EMBED_CHECKED = False

# PyTorch reranker модель (fallback)
PYTORCH_RERANKER_AVAILABLE = False
CROSS_ENCODER_AVAILABLE = False  # Alias для обратной совместимости
_PYTORCH_RERANKER_MODEL = None


def _lm_load_model(model_id, timeout=60):
    """Загрузить модель в LM Studio через API (auto-load)."""
    # Способ 1: Через chat completions (auto-load при первом запросе)
    try:
        payload = json.dumps({
            "model": model_id,
            "messages": [{"role": "user", "content": "test"}],
            "max_tokens": 1,
        }).encode()
        req = urllib.request.Request(
            LM_STUDIO_BASE_URL_RERANK + "/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        resp.read()
        log.info(f"Модель загружена через chat completions: {model_id}")
        return True
    except Exception as e:
        log.debug(f"Model load via chat error: {e}")
        return False


def _check_lm_reranker(auto_load=True):
    """Проверяет доступен ли reranker в LM Studio. Если auto_load=True — загружает автоматически."""
    global _LM_RERANKER_AVAILABLE, _LM_RERANKER_CHECKED, _LM_RERANKER_MODEL_ID
    if _LM_RERANKER_CHECKED:
        return _LM_RERANKER_AVAILABLE

    try:
        req = urllib.request.Request(LM_STUDIO_MODELS_URL)
        resp = urllib.request.urlopen(req, timeout=5)
        models = json.loads(resp.read().decode())
        loaded_ids = [m.get("id", "") for m in models.get("data", [])]

        # Ищем reranker среди загруженных
        for mid in loaded_ids:
            mid_lower = mid.lower()
            if "reranker" in mid_lower or "bge-reranker" in mid_lower:
                _LM_RERANKER_AVAILABLE = True
                _LM_RERANKER_MODEL_ID = mid
                log.info(f"✅ LM Studio reranker найден: {mid}")
                _LM_RERANKER_CHECKED = True
                return True

        # Ищем embed модель для fallback
        for mid in loaded_ids:
            mid_lower = mid.lower()
            if "embed" in mid_lower or "jina" in mid_lower or "bge" in mid_lower:
                _LM_EMBED_AVAILABLE = True
                log.info(f"✅ LM Studio embeddings найден: {mid}")
                break

        # Пробуем загрузить reranker автоматически
        if auto_load and not _LM_RERANKER_AVAILABLE:
            log.info(f"⏳ Reranker не найден, загружаю: {RERANKER_MODEL_LM_STUDIO}")
            if _lm_load_model(RERANKER_MODEL_LM_STUDIO):
                _LM_RERANKER_AVAILABLE = True
                _LM_RERANKER_MODEL_ID = RERANKER_MODEL_LM_STUDIO
                log.info(f"✅ LM Studio reranker загружен: {RERANKER_MODEL_LM_STUDIO}")

        _LM_RERANKER_CHECKED = True
    except Exception as e:
        log.debug(f"LM Studio reranker check error: {e}")
        _LM_RERANKER_AVAILABLE = False
        _LM_RERANKER_CHECKED = True

    return _LM_RERANKER_AVAILABLE


def _check_lm_embed():
    """Проверяет доступен ли LM Studio для embeddings."""
    global _LM_EMBED_AVAILABLE, _LM_EMBED_CHECKED
    if _LM_EMBED_CHECKED:
        return _LM_EMBED_AVAILABLE

    try:
        req = urllib.request.Request(LM_STUDIO_MODELS_URL)
        resp = urllib.request.urlopen(req, timeout=5)
        models = json.loads(resp.read().decode())
        for m in models.get("data", []):
            mid = m.get("id", "").lower()
            if "embed" in mid or "reranker" in mid or "bge" in mid or "jina" in mid:
                _LM_EMBED_AVAILABLE = True
                log.info(f"✅ LM Studio embeddings доступен: {m['id']}")
                break
        _LM_EMBED_CHECKED = True
    except Exception as e:
        log.debug(f"LM Studio embeddings недоступен: {e}")
        _LM_EMBED_AVAILABLE = False
        _LM_EMBED_CHECKED = True

    return _LM_EMBED_AVAILABLE


def _check_pytorch_reranker():
    """Проверяет доступен ли PyTorch reranker."""
    global PYTORCH_RERANKER_AVAILABLE, CROSS_ENCODER_AVAILABLE
    try:
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        PYTORCH_RERANKER_AVAILABLE = True
        CROSS_ENCODER_AVAILABLE = True
        return True
    except ImportError:
        log.debug("transformers не установлен, PyTorch reranker недоступен")
        PYTORCH_RERANKER_AVAILABLE = False
        CROSS_ENCODER_AVAILABLE = False
        return False


def _get_pytorch_reranker():
    """Ленивая загрузка PyTorch reranker модели."""
    global _PYTORCH_RERANKER_MODEL
    if _PYTORCH_RERANKER_MODEL is not None:
        return _PYTORCH_RERANKER_MODEL

    if not _check_pytorch_reranker():
        return None

    try:
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        log.info(f"Загрузка PyTorch reranker: {RERANKER_MODEL_PYTORCH}")
        tokenizer = AutoTokenizer.from_pretrained(RERANKER_MODEL_PYTORCH)
        model = AutoModelForSequenceClassification.from_pretrained(RERANKER_MODEL_PYTORCH)
        model.eval()
        _PYTORCH_RERANKER_MODEL = {"tokenizer": tokenizer, "model": model}
        log.info(f"✅ PyTorch reranker загружен: {RERANKER_MODEL_PYTORCH}")
        return _PYTORCH_RERANKER_MODEL
    except Exception as e:
        log.error(f"Ошибка загрузки PyTorch reranker: {e}")
        return None

def _get_lm_embeddings(text, model=RERANKER_MODEL_LM_STUDIO):
    """Получить эмбеддинг текста через LM Studio."""
    try:
        payload = json.dumps({
            "model": model,
            "input": text[:2000],
            "encoding_format": "float"
        }).encode()
        req = urllib.request.Request(
            LM_STUDIO_EMBED_URL,
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
        
        if "data" in result:
            return result["data"][0]["embedding"]
        elif "embedding" in result:
            return result["embedding"]
        return None
    except Exception as e:
        log.debug(f"LM embedding error: {e}")
        return None

def _cosine_similarity(v1, v2):
    """Вычислить cosine similarity между двумя векторами."""
    import math
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    
    dot_product = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    
    if norm1 == 0 or norm2 == 0:
        return 0.0
    
    return dot_product / (norm1 * norm2)

def _lm_rerank_scores(query, documents, model=None):
    """
    Reranking через cosine similarity эмбеддингов LM Studio.
    documents: list of str
    Возвращает list of float scores.
    """
    if not _check_lm_embed():
        return None
    
    model_name = model or RERANKER_MODEL_LM_STUDIO
    
    # Получаем эмбеддинг запроса
    query_emb = _get_lm_embeddings(query, model_name)
    if not query_emb:
        return None
    
    # Получаем эмбеддинги документов и вычисляем similarity
    scores = []
    for doc in documents:
        doc_emb = _get_lm_embeddings(doc[:1000], model_name)
        if doc_emb:
            sim = _cosine_similarity(query_emb, doc_emb)
            scores.append(sim)
        else:
            scores.append(0.0)
    
    if scores and len(scores) == len(documents):
        log.info(f"✅ LM cosine similarity rerank: {len(scores)} документов")
        return scores
    else:
        return None

def _pytorch_rerank_scores(query, documents):
    """
    Reranking через PyTorch reranker модель.
    documents: list of str
    Возвращает list of float scores.
    """
    reranker = _get_pytorch_reranker()
    if not reranker:
        return None
    
    try:
        tokenizer = reranker["tokenizer"]
        model = reranker["model"]
        
        scores = []
        # Обрабатываем батчами для эффективности
        batch_size = 8
        for i in range(0, len(documents), batch_size):
            batch_docs = documents[i:i+batch_size]
            pairs = [(query, doc[:1000]) for doc in batch_docs]
            
            features = tokenizer(pairs, padding=True, truncation=True, return_tensors='pt', max_length=RERANKER_MAX_LENGTH)
            
            import torch
            with torch.no_grad():
                outputs = model(**features)
                batch_scores = outputs.logits.squeeze().tolist()
            
            # Handle single element case
            if isinstance(batch_scores, float):
                batch_scores = [batch_scores]
            
            scores.extend(batch_scores)
        
        if len(scores) == len(documents):
            log.info(f"✅ PyTorch reranker: {len(scores)} документов")
            return scores
        else:
            log.warning(f"PyTorch rerank mismatch: {len(scores)} vs {len(documents)}")
            return None
    except Exception as e:
        log.error(f"PyTorch reranker error: {e}")
        return None

def _normalize_scores(scores):
    """Нормализовать scores в диапазон [0, 1]."""
    if not scores:
        return scores
    min_s = min(scores)
    max_s = max(scores)
    if max_s == min_s:
        return [0.5] * len(scores)
    return [(s - min_s) / (max_s - min_s) for s in scores]

def _lm_rerank(query, documents, model=None):
    """
    Устаревший API - теперь вызывает гибрид.
    """
    return _lm_rerank_scores(query, documents, model)

def rerank_with_cross_encoder(query, passages, top_k=20):
    """
    Гибридный reranking: LM Studio reranker (auto-load) + PyTorch fallback + LM cosine similarity.
    passages: list of dicts с ключом 'content' или 'preview'
    Возвращает отсортированный список passages[:top_k].
    """
    if not passages:
        return passages[:top_k]

    # Собираем тексты для reranking
    texts = []
    for p in passages:
        text = p.get("content_original", p.get("content", p.get("preview", "")))[:1000]
        texts.append(text)

    pytorch_scores = None
    lm_scores = None
    final_scores = None

    # Приоритет 1: LM Studio reranker (auto-loaded)
    _check_lm_reranker(auto_load=True)
    if _LM_RERANKER_AVAILABLE:
        lm_scores = _lm_rerank_scores(query, texts)
        if lm_scores:
            log.info(f"✅ LM Studio reranker: {len(lm_scores)} документов")

    # Приоритет 2: PyTorch reranker (fallback если нет LM)
    if not lm_scores:
        pytorch_scores = _pytorch_rerank_scores(query, texts)

    # Приоритет 3: LM Studio cosine similarity (последний fallback)
    if not lm_scores and not pytorch_scores:
        _check_lm_embed()
        if _LM_EMBED_AVAILABLE:
            lm_scores = _lm_rerank_scores(query, texts)
            if lm_scores:
                log.info(f"✅ LM Studio cosine similarity fallback: {len(lm_scores)} документов")

    # Комбинируем если оба доступны
    if pytorch_scores and lm_scores:
        log.info("🔄 Комбинирование PyTorch + LM Studio rerank scores")
        pytorch_norm = _normalize_scores(pytorch_scores)
        lm_norm = _normalize_scores(lm_scores)
        final_scores = [0.6 * p + 0.4 * l for p, l in zip(pytorch_norm, lm_norm)]
        log.info(f"✅ Гибридный rerank: PyTorch 60% + LM 40%")
    elif lm_scores:
        final_scores = lm_scores
        log.info("✅ LM Studio reranker active")
    elif pytorch_scores:
        final_scores = pytorch_scores
        log.info("✅ PyTorch reranker (без LM)")
    else:
        log.warning("Reranking недоступен, возвращаем исходный порядок")
        return passages[:top_k]

    # Применяем scores и сортируем
    for i, p in enumerate(passages):
        p["rerank_score"] = final_scores[i]

    passages.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
    return passages[:top_k]


# ===================== FUZZY QUERY PREPROCESSING =====================

def _preprocess_fuzzy_query(query):
    """
    Автоматически добавляет fuzzy matching (~1 или ~2) к словам > 5 символов,
    которые не являются частью phrase query (в кавычках) и не содержат ~.
    Расстояние: 1 для слов 6-8 символов, 2 для слов > 8 символов.
    """
    # Разделяем на quoted и unquoted части
    parts = re.split(r'("[^"]*")', query)
    result = []
    for part in parts:
        if part.startswith('"') and part.endswith('"'):
            # Phrase query — не модифицируем
            result.append(part)
        else:
            # Обычные термины — добавляем fuzzy к длинным словам
            tokens = part.split()
            fuzzy_tokens = []
            for token in tokens:
                # Пропускаем если уже есть ~, это спецсимвол или короткое слово
                if '~' in token or len(token) <= 5 or not re.match(r'^[a-zA-Zа-яА-ЯёЁ0-9]+$', token):
                    fuzzy_tokens.append(token)
                else:
                    distance = 1 if len(token) <= 8 else 2
                    fuzzy_tokens.append(f"{token}~{distance}")
            result.append(' '.join(fuzzy_tokens))
    return ''.join(result)


# ===================== SPELL CORRECTION (SymSpell) =====================

SYMSPELL_AVAILABLE = False
try:
    from symspellpy import SymSpell, Verbosity
    SYMSPELL_AVAILABLE = True
except ImportError:
    log.warning("symspellpy не установлен: pip install symspellpy")

_SYMSPELL_EN = None
_SYMSPELL_RU = None
_DICT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dictionaries")


def _get_symspell_en():
    """Ленивая загрузка SymSpell с английским словарём."""
    global _SYMSPELL_EN
    if _SYMSPELL_EN is not None:
        return _SYMSPELL_EN
    if not SYMSPELL_AVAILABLE:
        return None
    try:
        ss = SymSpell(max_dictionary_edit_distance=2, prefix_length=7)
        dict_path = os.path.join(
            os.path.dirname(__import__('symspellpy').__file__),
            "frequency_dictionary_en_82_765.txt"
        )
        if os.path.exists(dict_path):
            ss.load_dictionary(dict_path, term_index=0, count_index=1)
            _SYMSPELL_EN = ss
            log.info(f"SymSpell EN: loaded {ss.word_count} words")
        else:
            log.warning(f"SymSpell EN dictionary not found: {dict_path}")
    except Exception as e:
        log.error(f"SymSpell EN init error: {e}")
    return _SYMSPELL_EN


def _get_symspell_ru():
    """Ленивая загрузка SymSpell с русским словарём."""
    global _SYMSPELL_RU
    if _SYMSPELL_RU is not None:
        return _SYMSPELL_RU
    if not SYMSPELL_AVAILABLE:
        return None
    try:
        ss = SymSpell(max_dictionary_edit_distance=2, prefix_length=7)
        ru_dict_path = os.path.join(_DICT_DIR, "frequency_dictionary_ru.txt")
        if os.path.exists(ru_dict_path):
            ss.load_dictionary(ru_dict_path, term_index=0, count_index=1)
            _SYMSPELL_RU = ss
            log.info(f"SymSpell RU: loaded {ss.word_count} words")
        else:
            log.warning(f"SymSpell RU dictionary not found: {ru_dict_path}")
    except Exception as e:
        log.error(f"SymSpell RU init error: {e}")
    return _SYMSPELL_RU


def _is_russian(word):
    """Проверка: содержит ли слово русские буквы."""
    return bool(re.search(r'[а-яА-ЯёЁ]', word))


def _is_english(word):
    """Проверка: содержит ли слово английские буквы."""
    return bool(re.match(r'^[a-zA-Z]+$', word))


def correct_spelling(query):
    """
    Исправление опечаток через SymSpell.
    Русские слова → SymSpell RU, английские → SymSpell EN.
    Не трогает quoted phrases, спецсимволы, числа.
    """
    if not SYMSPELL_AVAILABLE:
        return query

    parts = re.split(r'("[^"]*")', query)
    corrected_parts = []

    for part in parts:
        if part.startswith('"') and part.endswith('"'):
            corrected_parts.append(part)
            continue

        tokens = re.findall(r'[a-zA-Zа-яА-ЯёЁ0-9]+|[^\w\s]+|\s+', part)
        corrected_tokens = []
        for token in tokens:
            if not re.match(r'[a-zA-Zа-яА-ЯёЁ]+', token) or len(token) <= 2:
                corrected_tokens.append(token)
                continue

            if _is_russian(token):
                ss = _get_symspell_ru()
            elif _is_english(token):
                ss = _get_symspell_en()
            else:
                corrected_tokens.append(token)
                continue

            if ss is None:
                corrected_tokens.append(token)
                continue

            suggestions = ss.lookup(
                token.lower(),
                Verbosity.CLOSEST,
                max_edit_distance=2
            )
            if suggestions and suggestions[0].distance > 0:
                best = suggestions[0].term
                if token[0].isupper():
                    best = best.capitalize()
                corrected_tokens.append(best)
            else:
                corrected_tokens.append(token)

        corrected_parts.append(''.join(corrected_tokens))

    result = ''.join(corrected_parts)
    if result != query:
        log.info(f"Spell correction: '{query}' → '{result}'")
    return result


# ===================== SYNONYM DICTIONARIES =====================

NLTK_WORDNET_AVAILABLE = False
try:
    from nltk.corpus import wordnet as _wordnet
    NLTK_WORDNET_AVAILABLE = True
except ImportError:
    log.warning("NLTK WordNet не доступен")

_RU_SYNONYMS = {
    "программирование": ["кодинг", "разработка", "код", "написание программ"],
    "код": ["программа", "скрипт", "исходник", "программирование"],
    "функция": ["метод", "процедура", "подпрограмма"],
    "переменная": ["переменая"],
    "алгоритм": ["метод", "подход", "способ"],
    "база данных": ["бд", "хранилище", "дб"],
    "сервер": ["хост", "машина", "нода"],
    "клиент": ["пользователь", "юзер"],
    "интерфейс": ["ui", "gui", "интерфейс пользователя"],
    "библиотека": ["пакет", "модуль", "framework"],
    "массив": ["список", "коллекция", "vector"],
    "строка": ["текст", "string"],
    "число": ["integer", "цифра", "значение"],
    "файл": ["документ", "файлик"],
    "папка": ["директория", "каталог"],
    "сеть": ["network", "интернет", "подключение"],
    "протокол": ["стандарт", "спецификация"],
    "ошибка": ["баг", "exception", "проблема", "багфикс"],
    "настройка": ["конфигурация", "config", "параметры"],
    "установка": ["инсталляция", "setup"],
    "обновление": ["апдейт", "upgrade"],
    "безопасность": ["security", "защита"],
    "шифрование": ["encryption", "криптография"],
    "компиляция": ["build", "сборка"],
    "отладка": ["debug", "дебаг"],
    "тестирование": ["test", "qa"],
    "оптимизация": ["ускорение", "performance"],
    "кэш": ["cache", "кэширование"],
    "архитектура": ["структура", "паттерн", "design pattern"],
    "api": ["интерфейс", "endpoint", "rest"],
    "документация": ["док", "мануал", "руководство"],
    "docker": ["контейнер", "контейнеризация"],
    "linux": ["линукс", "ubuntu", "debian"],
    "python": ["питон", "пайтон"],
    "javascript": ["js", "джаваскрипт"],
    "база": ["database", "бд", "db"],
    "процесс": ["задача", "task", "job"],
    "поток": ["thread", "threading"],
    "память": ["memory", "ram", "озу"],
    "процессор": ["cpu", "чип"],
    "диск": ["hdd", "ssd", "storage", "хранилище"],
    "разработка": ["development", "dev", "программирование"],
    "деплой": ["deployment", "развёртывание"],
    "git": ["гит", "version control", "vcs"],
    "запрос": ["query", "request"],
    "ответ": ["response", "reply"],
    "модель": ["моделька", "нейросеть", "ml"],
    "нейросеть": ["neural network", "модель", "ml", "deep learning"],
    "данные": ["data", "информация", "датасет"],
    "обучение": ["training", "learning", "тренировка"],
    "вектор": ["vector", "embedding", "эмбеддинг"],
    "поиск": ["search", "find"],
    "фильтр": ["filter", "отбор"],
    "сортировка": ["sort", "упорядочивание"],
    "класс": ["type", "object"],
    "объект": ["instance", "экземпляр"],
    "метод": ["функция", "procedure"],
    "свойство": ["property", "атрибут", "параметр"],
    "наследование": ["inheritance"],
    "полиморфизм": ["polymorphism"],
    "инкапсуляция": ["encapsulation"],
    "паттерн": ["pattern", "шаблон проектирования"],
    "фреймворк": ["framework", "библиотека"],
    "платформа": ["platform"],
    "приложение": ["app", "application"],
    "веб": ["web", "интернет"],
    "сайт": ["website", "веб-сайт"],
    "страница": ["page"],
    "форма": ["form"],
    "кнопка": ["button"],
    "меню": ["menu"],
    "окно": ["window"],
    "курс": ["tutorial", "обучение"],
    "задача": ["task", "problem", "упражнение"],
    "решение": ["solution", "ответ"],
    "пример": ["example", "образец"],
    "конфиг": ["configuration", "настройка", "settings"],
    "лог": ["log", "журнал"],
    "модуль": ["package", "библиотека"],
    "плагин": ["plugin", "расширение", "addon"],
    "расширение": ["extension", "плагин"],
    "язык": ["language", "lang"],
    "команда": ["command", "cmd"],
    "скрипт": ["script"],
    "автоматизация": ["automation", "scripting"],
    "интеграция": ["integration"],
    "миграция": ["migration"],
    "валидация": ["validation", "проверка"],
    "формат": ["format", "тип"],
    "json": ["джейсон"],
    "xml": ["xml markup"],
    "html": ["html разметка"],
    "css": ["стили", "stylesheet"],
    "sql": ["запрос sql", "structured query language"],
    "http": ["протокол http"],
    "tcp": ["tcp/ip"],
    "ssl": ["tls", "сертификат"],
    "пароль": ["password"],
    "токен": ["token", "ключ"],
    "сессия": ["session"],
    "cookie": ["куки"],
    "логин": ["login", "авторизация"],
    "регистрация": ["signup", "sign up"],
}


_EN_SYNONYMS = {
    "fast": ["quick", "rapid", "speedy"],
    "quick": ["fast", "rapid", "speedy"],
    "big": ["large", "huge", "enormous"],
    "large": ["big", "huge", "enormous"],
    "small": ["tiny", "little", "compact"],
    "tiny": ["small", "little", "compact"],
    "good": ["excellent", "great", "fine"],
    "bad": ["poor", "terrible", "awful"],
    "error": ["bug", "issue", "problem"],
    "bug": ["error", "issue", "defect"],
    "function": ["method", "procedure", "routine"],
    "method": ["function", "procedure", "routine"],
    "variable": ["parameter", "argument", "field"],
    "array": ["list", "vector", "collection"],
    "list": ["array", "collection", "sequence"],
    "object": ["instance", "entity", "item"],
    "class": ["type", "structure", "template"],
    "file": ["document", "record", "entry"],
    "database": ["db", "store", "repository"],
    "server": ["host", "node", "backend"],
    "client": ["consumer", "frontend", "caller"],
    "request": ["call", "query", "invoke"],
    "response": ["reply", "answer", "result"],
    "api": ["interface", "endpoint", "service"],
    "library": ["package", "module", "sdk"],
    "framework": ["library", "platform", "toolkit"],
    "install": ["setup", "deploy", "configure"],
    "setup": ["install", "configure", "initialize"],
    "config": ["configuration", "settings", "options"],
    "build": ["compile", "construct", "create"],
    "deploy": ["release", "publish", "ship"],
    "debug": ["troubleshoot", "diagnose", "fix"],
    "test": ["verify", "validate", "check"],
    "run": ["execute", "start", "launch"],
    "stop": ["halt", "terminate", "end"],
    "start": ["begin", "initiate", "launch"],
    "create": ["make", "build", "generate"],
    "delete": ["remove", "destroy", "erase"],
    "update": ["modify", "change", "edit"],
    "read": ["load", "fetch", "retrieve"],
    "write": ["save", "store", "persist"],
    "send": ["transmit", "dispatch", "push"],
    "receive": ["get", "accept", "pull"],
    "connect": ["link", "join", "attach"],
    "disconnect": ["unlink", "separate", "detach"],
    "search": ["find", "query", "lookup"],
    "find": ["search", "locate", "discover"],
    "sort": ["order", "arrange", "rank"],
    "filter": ["select", "screen", "extract"],
    "map": ["transform", "convert", "translate"],
    "reduce": ["aggregate", "accumulate", "combine"],
    "merge": ["combine", "join", "unite"],
    "split": ["divide", "separate", "break"],
    "parse": ["analyze", "tokenize", "interpret"],
    "format": ["structure", "layout", "style"],
    "validate": ["verify", "check", "confirm"],
    "encode": ["serialize", "convert", "cipher"],
    "decode": ["deserialize", "parse", "decrypt"],
    "cache": ["buffer", "store", "memory"],
    "log": ["record", "trace", "audit"],
    "monitor": ["watch", "observe", "track"],
    "schedule": ["queue", "plan", "dispatch"],
    "trigger": ["fire", "emit", "activate"],
    "handle": ["process", "manage", "treat"],
    "throw": ["raise", "emit", "signal"],
    "catch": ["intercept", "capture", "trap"],
    "retry": ["repeat", "reattempt", "redo"],
    "cancel": ["abort", "revoke", "withdraw"],
    "lock": ["mutex", "guard", "block"],
    "unlock": ["release", "free", "unblock"],
    "sync": ["synchronize", "coordinate", "align"],
    "async": ["asynchronous", "concurrent", "nonblocking"],
    "thread": ["fiber", "worker", "task"],
    "process": ["job", "task", "worker"],
    "memory": ["ram", "storage", "heap"],
    "storage": ["disk", "store", "repository"],
    "input": ["feed", "source", "stdin"],
    "output": ["result", "product", "stdout"],
    "stream": ["flow", "pipe", "channel"],
    "buffer": ["cache", "queue", "pool"],
    "queue": ["fifo", "line", "backlog"],
    "stack": ["lifo", "pile", "pool"],
    "tree": ["hierarchy", "trie", "bst"],
    "graph": ["network", "mesh", "topology"],
    "node": ["vertex", "element", "point"],
    "edge": ["link", "arc", "connection"],
    "key": ["index", "identifier", "id"],
    "hash": ["digest", "checksum", "fingerprint"],
    "encrypt": ["cipher", "encode", "scramble"],
    "decrypt": ["decipher", "decode", "unscramble"],
    "backup": ["copy", "snapshot", "archive"],
    "restore": ["recover", "reload", "revert"],
    "migrate": ["move", "transfer", "port"],
    "scale": ["expand", "grow", "extend"],
    "replicate": ["copy", "duplicate", "clone"],
    "partition": ["shard", "split", "divide"],
    "index": ["catalog", "register", "lookup"],
    "query": ["search", "find", "retrieve"],
    "schema": ["model", "structure", "layout"],
    "benchmark": ["measure", "profile", "test"],
    "profile": ["analyze", "measure", "trace"],
    "optimize": ["improve", "enhance", "tune"],
    "refactor": ["restructure", "reorganize", "rewrite"],
    "abstract": ["generalize", "encapsulate", "hide"],
    "implement": ["build", "create", "develop"],
    "design": ["plan", "architect", "structure"],
    "analyze": ["examine", "study", "investigate"],
    "document": ["describe", "explain", "annotate"],
    "tutorial": ["guide", "lesson", "walkthrough"],
    "guide": ["tutorial", "manual", "handbook"],
    "example": ["sample", "demo", "snippet"],
    "snippet": ["fragment", "piece", "sample"],
    "pattern": ["template", "scheme", "model"],
    "template": ["pattern", "layout", "scaffold"],
    "middleware": ["interceptor", "filter", "layer"],
    "plugin": ["extension", "addon", "module"],
    "extension": ["plugin", "addon", "module"],
    "adapter": ["wrapper", "converter", "bridge"],
    "wrapper": ["adapter", "decorator", "shell"],
    "proxy": ["delegate", "surrogate", "gateway"],
    "singleton": ["single", "unique", "sole"],
    "factory": ["creator", "builder", "generator"],
    "observer": ["watcher", "listener", "subscriber"],
    "strategy": ["policy", "algorithm", "approach"],
    "decorator": ["wrapper", "enhancer", "modifier"],
    "iterator": ["enumerator", "cursor", "scanner"],
    "generator": ["producer", "creator", "builder"],
    "resolver": ["solver", "finder", "handler"],
    "transformer": ["converter", "mapper", "processor"],
    "serializer": ["encoder", "converter", "packer"],
    "deserializer": ["decoder", "parser", "unpacker"],
    "interceptor": ["middleware", "filter", "handler"],
    "initializer": ["setup", "loader", "bootstrap"],
    "controller": ["handler", "manager", "dispatcher"],
    "service": ["provider", "component", "worker"],
    "repository": ["store", "storage", "dao"],
    "component": ["module", "part", "element"],
    "module": ["component", "package", "unit"],
    "package": ["module", "bundle", "library"],
    "docker": ["container", "containerization"],
    "kubernetes": ["k8s", "orchestration"],
    "model": ["neural", "network", "ml"],
    "learning": ["training", "inference"],
    "training": ["learning", "fitting"],
    "inference": ["prediction", "evaluation"],
    "embedding": ["vector", "representation"],
    "pipeline": ["workflow", "chain"],
    "workflow": ["pipeline", "process"],
    "ci": ["continuous", "integration"],
    "cd": ["continuous", "delivery", "deployment"],
    "git": ["version", "control", "vcs"],
    "branch": ["fork", "variant"],
    "merge": ["combine", "integrate"],
    "rebase": ["replay", "rewrite"],
    "commit": ["change", "snapshot"],
    "push": ["upload", "send"],
    "pull": ["download", "fetch", "clone"],
    "linux": ["unix", "gnu", "kernel"],
    "bash": ["shell", "terminal", "console"],
    "terminal": ["console", "shell", "cli"],
    "script": ["program", "automation", "batch"],
    "command": ["cmd", "instruction", "directive"],
    "permission": ["access", "rights", "privilege"],
    "user": ["account", "principal"],
    "group": ["role", "team"],
    "network": ["net", "lan", "wan"],
    "protocol": ["standard", "spec"],
    "port": ["socket", "endpoint"],
    "socket": ["port", "connection"],
    "ssl": ["tls", "certificate", "security"],
    "token": ["key", "credential", "jwt"],
    "session": ["connection", "state"],
    "cookie": ["session", "tracker"],
    "cors": ["cross", "origin", "policy"],
    "middleware": ["interceptor", "filter"],
    "routing": ["router", "routes"],
    "controller": ["handler", "action"],
    "view": ["template", "render"],
    "model": ["schema", "entity"],
    "migration": ["upgrade", "schema"],
    "seed": ["fixture", "sample"],
    "mock": ["stub", "fake", "spy"],
    "stub": ["mock", "fake"],
    "fixture": ["setup", "seed"],
    "assertion": ["check", "expect", "verify"],
    "coverage": ["testing", "report"],
    "lint": ["checker", "analyzer"],
    "format": ["prettier", "beautifier"],
}


def _get_english_synonyms(word):
    """Получить английские синонимы из предвычисленного словаря."""
    w = word.lower()
    if w in _EN_SYNONYMS:
        return _EN_SYNONYMS[w]
    # Обратный поиск
    for key, syns in _EN_SYNONYMS.items():
        if w in syns:
            return [key] + [s for s in syns if s != w]
    return []


def _get_russian_synonyms(word):
    """Получить русские синонимы из предвычисленного словаря."""
    w = word.lower()
    if w in _RU_SYNONYMS:
        return _RU_SYNONYMS[w]
    # Обратный поиск
    for key, syns in _RU_SYNONYMS.items():
        if w in syns:
            return [key] + [s for s in syns if s != w]
    return []


def expand_query_with_synonyms(query, max_synonyms_per_word=2):
    """
    Расширение запроса синонимами.
    Не применяется к stopwords, коротким словам, quoted phrases.
    """
    parts = re.split(r'("[^"]*")', query)
    expanded_terms = set()
    query_words = set(w.lower() for w in re.findall(r'[a-zA-Zа-яА-ЯёЁ]+', query))

    for part in parts:
        if part.startswith('"') and part.endswith('"'):
            continue

        words = re.findall(r'[a-zA-Zа-яА-ЯёЁ]+', part)
        for w in words:
            if w.lower() in _ALL_STOPWORDS or len(w) <= 3:
                continue
            if _is_russian(w):
                syns = _get_russian_synonyms(w)
            elif _is_english(w):
                syns = _get_english_synonyms(w)
            else:
                continue
            # Добавляем только уникальные синонимы, не совпадающие с запросом
            count = 0
            for s in syns:
                if s.lower() not in query_words and s.lower() not in expanded_terms:
                    expanded_terms.add(s.lower())
                    count += 1
                    if count >= max_synonyms_per_word:
                        break

    if expanded_terms:
        return query + ' ' + ' '.join(sorted(expanded_terms))
    return query


# ===================== RECENCY BOOST =====================

def _compute_recency_boost(created_ts):
    """
    Boost = 1.0 + log(days_since_creation + 1) * 0.05
    Более свежие документы получают буст.
    Если timestamp отсутствует или некорректен — возвращает 1.0.
    """
    if not created_ts or created_ts <= 0:
        return 1.0
    now = time.time()
    days = max(0, (now - created_ts) / 86400.0)
    return 1.0 + math.log(days + 1) * 0.05

# ===================== СТЕММИНГ И STOPWORDS =====================

_ALL_STOPWORDS = {
    # Русские
    'и', 'в', 'не', 'на', 'по', 'о', 'с', 'у', 'к', 'но', 'а', 'или', 'же',
    'ли', 'бы', 'то', 'как', 'что', 'кто', 'где', 'когда', 'который', 'какой',
    'этот', 'тот', 'весь', 'все', 'всё', 'всех', 'всем', 'сам', 'себя', 'себе',
    'собой', 'свой', 'свое', 'своя', 'свои', 'своих', 'своим', 'своими',
    'своему', 'своей', 'своего', 'свою', 'так', 'уже', 'ещё', 'еще', 'тоже',
    'также', 'чтобы', 'между', 'после', 'перед', 'через', 'для', 'без', 'при',
    # Английские
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'shall', 'can', 'need', 'to', 'of', 'in',
    'for', 'on', 'with', 'at', 'by', 'from', 'as', 'into', 'through',
    'during', 'before', 'after', 'above', 'below', 'between', 'out', 'off',
    'over', 'under', 'again', 'further', 'then', 'once', 'here', 'there',
    'when', 'where', 'why', 'how', 'all', 'both', 'each', 'few', 'more',
    'most', 'other', 'some', 'such', 'no', 'nor', 'not', 'only', 'own',
    'same', 'so', 'than', 'too', 'very', 'just', 'because', 'but', 'and',
    'or', 'if', 'while', 'about', 'against', 'this', 'that', 'these',
    'those', 'i', 'you', 'he', 'she', 'it', 'we', 'they', 'me', 'him',
    'her', 'us', 'them', 'my', 'your', 'his', 'its', 'our', 'their',
    'what', 'which', 'who', 'whom',
}

# Русский стеммер Портера
_RU_PATTERNS = [
    (re.compile(r'([ая])(в|вши|вшись)$'), 1),
    (re.compile(r'(с[яь])$'), 0),
    (re.compile(r'([аеёиоуыэюя]+)(ее|ие|ые|ое|ими|ыми|ей|ий|ый|ой|ем|им|ым|ом|его|ого|ему|ому|их|ых)$'), 1),
    (re.compile(r'([ая])(ем|нн|вш|ющ|щ)$'), 1),
    (re.compile(r'([ая])(ла|на|ете|йте|ли|й|л|ем|н|ло|но|ет|ют|ны|ть|ешь|нно)$'), 1),
    (re.compile(r'(а|ев|ов|ие|ье|е|иями|ями|ами|еи|ии|и|ией|ей|ой|ий|й|иям|ям|ием|ем|ам|ом|о|у|ах|иях|ях|ы|ь|ию|ью|ю|ия|ья|я)$'), 0),
]
_RU_DERIVATIONAL = re.compile(r'([аеёиоуыэюя]+)([аеёиоуыэюя]+[^аеёиоуыэюя]+)$')


def _ru_stem(word):
    word = word.lower().replace('ё', 'е')
    if word in _ALL_STOPWORDS or len(word) <= 2:
        return word
    m = re.match(r'^([^аеёиоуыэюя]*)([аеёиоуыэюя].*)$', word)
    if not m:
        return word
    prefix, rest = m.groups()
    for pattern, group in _RU_PATTERNS:
        m2 = pattern.search(rest)
        if m2:
            rest = rest[:m2.start(group + 1)]
            break
    if rest.endswith('ь'):
        rest = rest[:-1]
    m3 = _RU_DERIVATIONAL.search(rest)
    if m3 and len(m3.group(2)) >= 2:
        rest = rest[:m3.start(2)]
    return prefix + rest


def _en_stem(word):
    if NLTK_AVAILABLE:
        return _porter.stem(word)
    # Простой fallback: убираем типичные суффиксы
    for suf in ('ing', 'tion', 'ness', 'ment', 'able', 'ible', 'ally', 'ly', 'ed', 'er', 'es', 's'):
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            return word[:-len(suf)]
    return word


# Лемматизация вызывается на каждый токен всего корпуса (до ~143k уникальных
# слов в книжной библиотеке), а чистый regex-стеммер стоит дороже всего.
# Кэш не меняет результат: тот же вход — та же форма слова, индекс не разъезжается.
@lru_cache(maxsize=262144)
def stem_word(word):
    """Стемминг слова: определяет язык автоматически."""
    w = word.lower()
    if w in _ALL_STOPWORDS or len(w) <= 2:
        return None  # Фильтруем stopwords
    if re.match(r'^[a-z]+$', w):
        return _en_stem(w)
    return _ru_stem(w)


def tokenize_and_stem(text):
    """Токенизация + стемминг текста. Возвращает список stemmed токенов."""
    if not text:
        return []
    words = re.findall(r'[a-zA-Zа-яА-ЯёЁ0-9]+', text.lower())
    result = []
    for w in words:
        s = stem_word(w)
        if s:
            result.append(s)
    return result


def stem_text(text):
    """Стемминг/лемматизация текста для индексации (строка)."""
    return ' '.join(tokenize_and_stem(text))


def lemmatize_word(word):
    """Лемматизация одного слова через pymorphy3 или стемминг."""
    if PYMORPHY_AVAILABLE:
        import re
        if re.match(r'^[a-zA-Z]+$', word):
            # Английские слова — стемминг
            if NLTK_AVAILABLE:
                return _porter.stem(word)
            return word
        else:
            # Русские слова — лемматизация
            p = _morph.parse(word.lower())
            if p:
                return p[0].normal_form
            return word.lower()
    else:
        # Fallback на стемминг
        return stem_word(word)


# ===================== HTML CLEANING =====================

def _clean_html(text):
    return re.sub(r'<[^>]+>', ' ', text)


# ===================== EMBEDDING CACHE (SQLite) =====================

class EmbeddingCache:
    """Персистентный кэш эмбеддингов в SQLite с robust обработкой ошибок."""

    def __init__(self, db_path=None, max_retries=3):
        self.db_path = db_path or EMBEDDING_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn = None
        self._max_retries = max_retries
        self._init_db()

    def _init_db(self):
        """Инициализация БД с retry и WAL mode."""
        import random
        for attempt in range(self._max_retries):
            try:
                self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
                # WAL mode — лучшая конкурентная запись
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS embeddings (
                        key TEXT PRIMARY KEY,
                        vector BLOB,
                        dim INTEGER,
                        created REAL
                    )
                """)
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON embeddings(created)")
                self._conn.commit()
                log.info(f"EmbeddingCache: {self.db_path} (WAL mode)")
                return
            except sqlite3.OperationalError as e:
                log.warning(f"EmbeddingCache init attempt {attempt+1}/{self._max_retries}: {e}")
                if attempt < self._max_retries - 1:
                    time.sleep(1 + random.random() * 2)
                else:
                    log.error(f"EmbeddingCache: не удалось инициализировать БД: {e}")
                    self._conn = None

    def _ensure_conn(self):
        """Переподключение если соединение потеряно."""
        if self._conn is None:
            self._init_db()
        return self._conn is not None

    def _exec_with_retry(self, sql, params=(), commit=False):
        """Выполнить SQL с retry при database is locked."""
        if not self._ensure_conn():
            return None
        import random
        for attempt in range(self._max_retries):
            try:
                cursor = self._conn.execute(sql, params)
                if commit:
                    self._conn.commit()
                return cursor
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() or "readonly" in str(e).lower():
                    log.warning(f"SQLite retry {attempt+1}/{self._max_retries}: {e}")
                    if attempt < self._max_retries - 1:
                        time.sleep(0.5 + random.random())
                        if "readonly" in str(e).lower():
                            try:
                                self._conn.close()
                            except Exception:
                                swallow.note('search_engine_v2._exec_with_retry:L1209')
                                pass
                            self._conn = None
                            self._init_db()
                    else:
                        log.error(f"SQLite error после {self._max_retries} попыток: {e}")
                        return None
                else:
                    raise
        return None

    def get(self, key):
        if not self._ensure_conn():
            return None
        try:
            row = self._exec_with_retry(
                "SELECT vector, dim FROM embeddings WHERE key = ?", (key,)
            )
            if row:
                row = row.fetchone()
                if row:
                    return np.frombuffer(row[0], dtype=np.float32).tolist()
        except Exception as e:
            log.debug(f"EmbeddingCache get error: {e}")
        return None

    def put(self, key, vector):
        if not vector:
            return
        arr = np.array(vector, dtype=np.float32)
        try:
            self._exec_with_retry(
                "INSERT OR REPLACE INTO embeddings (key, vector, dim, created) VALUES (?, ?, ?, ?)",
                (key, arr.tobytes(), len(arr), time.time()),
                commit=True
            )
        except Exception as e:
            log.debug(f"EmbeddingCache put error: {e}")

    def get_many(self, keys):
        """Получить несколько эмбеддингов за один запрос."""
        if not keys or not self._ensure_conn():
            return {}
        try:
            placeholders = ','.join('?' * len(keys))
            rows = self._exec_with_retry(
                f"SELECT key, vector, dim FROM embeddings WHERE key IN ({placeholders})", keys
            )
            if rows is None:
                return {}
            result = {}
            for key, blob, dim in rows.fetchall():
                result[key] = np.frombuffer(blob, dtype=np.float32).tolist()
            return result
        except Exception as e:
            log.debug(f"EmbeddingCache get_many error: {e}")
            return {}

    def put_many(self, items):
        """Сохранить несколько эмбеддингов. items = [(key, vector), ...]"""
        if not self._ensure_conn():
            return
        now = time.time()
        data = []
        for key, vector in items:
            if vector:
                arr = np.array(vector, dtype=np.float32)
                data.append((key, arr.tobytes(), len(arr), now))
        if data:
            try:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO embeddings (key, vector, dim, created) VALUES (?, ?, ?, ?)",
                    data
                )
                self._conn.commit()
            except sqlite3.OperationalError as e:
                log.warning(f"EmbeddingCache put_many: {e}")
            except Exception as e:
                log.debug(f"EmbeddingCache put_many error: {e}")

    def count(self):
        if not self._ensure_conn():
            return 0
        try:
            result = self._exec_with_retry("SELECT COUNT(*) FROM embeddings")
            return result.fetchone()[0] if result else 0
        except Exception:
            swallow.note('search_engine_v2.count:L1296')
            return 0

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                swallow.note('search_engine_v2.close:L1304')
                pass
            self._conn = None


# ===================== EMBEDDINGS API =====================

def get_embeddings(texts, model=None):
    """Эмбеддинги: сначала локальная ONNX-модель, затем HTTP.

    Раньше здесь был ТОЛЬКО HTTP к LM_STUDIO_EMBED_URL. Но этим адресом
    в этой сборке является llama-server (:8080), а он эмбеддингов не
    отдаёт: POST /v1/embeddings -> 501 Not Implemented. Ошибка глоталась,
    возвращался пустой вектор, и в логе оставалось
    «Vector: нет векторов для индексации», а vector_count был 0.
    Теперь сначала пробуем локальную модель из qdrant_module.
    """
    if isinstance(texts, str):
        texts = [texts]
    if not texts:
        return []

    # 1) Локальная ONNX-модель (работает офлайн).
    try:
        import qdrant_module as _qm
        if _qm.get_embedding_backend() == 'fastembed':
            mdl = _qm._fastembed_model()
            vecs = mdl.embed(list(texts))
            if vecs and len(vecs) == len(texts):
                out = []
                for emb in vecs:
                    if emb:
                        arr = np.array(emb, dtype=np.float32)
                        norm = np.linalg.norm(arr)
                        if norm > 0:
                            arr = arr / norm
                        out.append(arr.tolist())
                    else:
                        out.append([])
                return out
    except Exception as e:  # noqa: BLE001
        log.warning(f"Локальные эмбеддинги не сработали: {e}")

    # 2) Запасной HTTP-путь.
    try:
        payload = json.dumps({
            "model": model or LM_STUDIO_EMBED_MODEL,
            "input": texts,
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if LM_STUDIO_API_KEY:
            headers["Authorization"] = f"Bearer {LM_STUDIO_API_KEY}"
        req = urllib.request.Request(
            LM_STUDIO_EMBED_URL,
            data=payload,
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            embeddings = []
            for item in data.get("data", []):
                emb = item.get("embedding", [])
                if emb:
                    # Нормализация для cosine similarity через L2
                    arr = np.array(emb, dtype=np.float32)
                    norm = np.linalg.norm(arr)
                    if norm > 0:
                        arr = arr / norm
                    embeddings.append(arr.tolist())
                else:
                    embeddings.append([])
            return embeddings
    except Exception as e:
        log.error(f"Embeddings error: {e}")
        return [[] for _ in texts]


def cosine_similarity(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ===================== FAISS INDEX =====================

class FaissIndex:
    """FAISS ANN индекс для семантического поиска."""

    def __init__(self, dimension=None, index_dir=None):
        self.dimension = dimension or DIMENSION
        self.index_dir = index_dir or INDEX_DIR
        self.index_path = os.path.join(self.index_dir, "faiss.index")
        self.map_path = os.path.join(self.index_dir, "faiss_map.json")
        self.index = None
        self.doc_id_map = {}  # faiss_id -> doc_id
        self.reverse_map = {}  # doc_id -> faiss_id
        self._next_id = 0

    def _create_index(self, n_vectors=0):
        """Создать FAISS индекс. Выбирает тип по количеству векторов."""
        base = faiss.IndexFlatIP(self.dimension)  # Inner Product (для нормализованных = cosine)
        if n_vectors > 10000:
            # IVF для больших коллекций — быстрее, но требует тренировки
            nlist = min(int(math.sqrt(n_vectors)), 256)
            quantizer = faiss.IndexFlatIP(self.dimension)
            ivf = faiss.IndexIVFFlat(quantizer, self.dimension, nlist, faiss.METRIC_INNER_PRODUCT)
            return faiss.IndexIDMap2(ivf)
        else:
            # Flat для малых коллекций — точный поиск
            return faiss.IndexIDMap2(base)

    def build(self, doc_ids, vectors):
        """Построить индекс с нуля."""
        if not FAISS_AVAILABLE or not vectors:
            return 0

        n = len(vectors)
        self.index = self._create_index(n)
        self.doc_id_map = {}
        self.reverse_map = {}
        self._next_id = 0

        arr = np.array(vectors, dtype=np.float32)
        # Нормализация для cosine через Inner Product
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1
        arr = arr / norms

        # Train если IVF
        inner = self.index
        while hasattr(inner, 'index'):
            inner = inner.index
        if hasattr(inner, 'nlist') and not inner.is_trained and n > 100:
            inner.train(arr)

        ids = np.arange(n, dtype=np.int64)
        self.index.add_with_ids(arr, ids)

        for i, doc_id in enumerate(doc_ids):
            self.doc_id_map[i] = doc_id
            self.reverse_map[doc_id] = i
        self._next_id = n

        self.save()
        log.info(f"FAISS: indexed {n} vectors, dim={self.dimension}")
        return n

    def add(self, doc_ids, vectors):
        """Добавить векторы в существующий индекс."""
        if not FAISS_AVAILABLE or not vectors:
            return 0
        if self.index is None:
            return self.build(doc_ids, vectors)

        arr = np.array(vectors, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1
        arr = arr / norms

        start_id = self._next_id
        ids = np.arange(start_id, start_id + len(vectors), dtype=np.int64)

        inner = self.index
        while hasattr(inner, 'index'):
            inner = inner.index
        if hasattr(inner, 'nlist') and not inner.is_trained and len(vectors) > 100:
            inner.train(arr)

        self.index.add_with_ids(arr, ids)

        for i, doc_id in enumerate(doc_ids):
            faiss_id = start_id + i
            self.doc_id_map[faiss_id] = doc_id
            self.reverse_map[doc_id] = faiss_id
        self._next_id += len(vectors)

        self.save()
        return len(vectors)

    def search(self, query_vector, k=50):
        """Поиск ближайших соседей."""
        if not FAISS_AVAILABLE or self.index is None or not query_vector:
            return []

        q = np.array([query_vector], dtype=np.float32)
        norms = np.linalg.norm(q, axis=1, keepdims=True)
        norms[norms == 0] = 1
        q = q / norms

        actual_k = min(k, self.index.ntotal)
        if actual_k == 0:
            return []

        scores, ids = self.index.search(q, actual_k)
        results = []
        for score, fid in zip(scores[0], ids[0]):
            if fid == -1:
                continue
            doc_id = self.doc_id_map.get(int(fid))
            if doc_id:
                results.append((doc_id, float(score)))
        return results

    def save(self):
        """Сохранить индекс на диск."""
        if not FAISS_AVAILABLE or self.index is None:
            return
        os.makedirs(self.index_dir, exist_ok=True)
        faiss.write_index(self.index, self.index_path)
        with open(self.map_path, 'w', encoding='utf-8') as f:
            json.dump({
                "doc_id_map": {str(k): v for k, v in self.doc_id_map.items()},
                "reverse_map": self.reverse_map,
                "next_id": self._next_id,
                "dimension": self.dimension,
            }, f, ensure_ascii=False)

    def load(self):
        """Загрузить индекс с диска."""
        if not FAISS_AVAILABLE:
            return False
        if not os.path.exists(self.index_path):
            return False
        try:
            self.index = faiss.read_index(self.index_path)
            if os.path.exists(self.map_path):
                with open(self.map_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.doc_id_map = {int(k): v for k, v in data.get("doc_id_map", {}).items()}
                self.reverse_map = data.get("reverse_map", {})
                self._next_id = data.get("next_id", 0)
                self.dimension = data.get("dimension", self.dimension)
            log.info(f"FAISS: loaded {self.index.ntotal} vectors")
            return True
        except Exception as e:
            log.error(f"FAISS load error: {e}")
            return False

    def clear(self):
        self.index = None
        self.doc_id_map = {}
        self.reverse_map = {}
        self._next_id = 0
        for p in [self.index_path, self.map_path]:
            if os.path.exists(p):
                os.remove(p)

    @property
    def ntotal(self):
        return self.index.ntotal if self.index else 0


# ===================== WHOOSH ИНДЕКС =====================

class QdrantIndex:
    """Qdrant векторный индекс — альтернатива FAISS."""

    @staticmethod
    def _detect_dimension():
        """Размерность реально используемой модели эмбеддингов.

        Раньше здесь стояла константа DIMENSION (768 по умолчанию), но
        локальная модель paraphrase-multilingual-MiniLM-L12-v2 даёт 384.
        Несовпадение приводило к тому, что индекс оставался пустым.
        """
        try:
            import qdrant_module as _qm
            return int(_qm.get_embed_dim())
        except Exception:
            swallow.note('search_engine_v2._detect_dimension:L1579')
            return DIMENSION

    def __init__(self, dimension=None, index_dir=None):
        self.dimension = dimension if dimension else self._detect_dimension()
        self.index_dir = index_dir or INDEX_DIR
        self.qdrant_dir = os.path.join(self.index_dir, "qdrant")
        self.doc_id_map = {}
        self.reverse_map = {}
        self._next_id = 0
        self._client = None  # Локальный клиент, не глобальный

        if QDRANT_AVAILABLE:
            os.makedirs(self.qdrant_dir, exist_ok=True)
            try:
                # Единый Qdrant-бэкенд: используем qdrant_module (та же
                # инстанция, что страница /qdrant и память AI-чата).
                # Коллекция "vectors" — своя, с памятью чата не конфликтует.
                # Если общий бэкенд недоступен — фолбэк на старый локальный
                # путь index/qdrant (старое поведение, ничего не ломаем).
                self._shared_backend = False
                try:
                    import qdrant_module as _qm
                    _sc = _qm.get_client()
                    if _sc is not None:
                        self._client = _sc
                        self._shared_backend = True
                        log.info("QdrantIndex: единый бэкенд qdrant_module")
                except Exception as _se:
                    log.warning(f"QdrantIndex: общий бэкенд недоступен ({_se})")
                # BUGFIX: не создаём ЛОКАЛЬНЫЙ QdrantClient(path=self.qdrant_dir).
                # Локальный режим Qdrant допускает только один клиент на storage-папку,
                # и второй клиент (даже в том же процессе) падает с ошибкой
                # "Storage folder ... is already accessed by another instance",
                # повторяясь каждые несколько секунд и фризя сервер. Если общий
                # бэкенд недоступен — векторный стор остаётся None (гибридный поиск
                # корректно деградирует, BM25/Tantivy продолжает работать).
                if not self._shared_backend and not _QDRANT_BACKEND_WARNED:
                    _QDRANT_BACKEND_WARNED = True
                    log.warning("QdrantIndex: общий бэкенд недоступен — векторный стор отключён (BM25 работает)")
                collections = self._client.get_collections().collections
                exists = any(c.name == "vectors" for c in collections)
                
                if exists:
                    info = self._client.get_collection("vectors")
                    actual_dim = info.config.params.vectors.size
                    if actual_dim != self.dimension:
                        log.warning(f"Qdrant dimension mismatch: {actual_dim} → {self.dimension}. Пересоздаю...")
                        self._client.delete_collection("vectors")
                        exists = False
                
                if not exists:
                    self._client.create_collection(
                        collection_name="vectors",
                        vectors_config=VectorParams(size=self.dimension, distance=Distance.COSINE),
                    )
                    log.info(f"Qdrant коллекция создана: dim={self.dimension}")
                else:
                    log.info(f"Qdrant коллекция загружена: dim={self.dimension}")
            except Exception as e:
                log.error(f"Qdrant init error: {e}")
                self._client = None

    @property
    def client(self):
        return self._client

    @property
    def ntotal(self):
        if not QDRANT_AVAILABLE or not self.client:
            return 0
        try:
            info = self.client.get_collection("vectors")
            for _a in ("points_count", "indexed_vectors_count", "vectors_count"):
                _v = getattr(info, _a, None)
                if _v:
                    return int(_v)
            return 0
        except Exception:
            swallow.note('search_engine_v2.ntotal:L1658')
            return 0

    def build(self, doc_ids, vectors):
        """Построить индекс с нуля."""
        if not QDRANT_AVAILABLE or not self.client or not vectors:
            return 0
        
        try:
            # Удаляем старые точки
            self.client.delete(collection_name="vectors", points_selector=Filter(must=[]))
            
            points = []
            for i, (doc_id, vector) in enumerate(zip(doc_ids, vectors)):
                if not vector:
                    continue
                points.append(PointStruct(id=i, payload={"doc_id": doc_id}, vector=vector))
            
            if points:
                self.client.upsert(collection_name="vectors", points=points)
            
            self.doc_id_map = {i: doc_id for i, doc_id in enumerate(doc_ids)}
            self.reverse_map = {doc_id: i for i, doc_id in enumerate(doc_ids)}
            self._next_id = len(doc_ids)
            
            log.info(f"Qdrant: indexed {len(points)} vectors, dim={self.dimension}")
            return len(points)
        except Exception as e:
            log.error(f"Qdrant build error: {e}")
            return 0

    def _load_existing_map(self) -> bool:
        """Восстановить doc_id_map из уже существующей коллекции.

        Нужен после рестарта: карты живут только в памяти, и раньше первый
        же add() видел пустой map и вызывал build(), который пересоздавал
        коллекцию только из новых документов — старые векторы терялись.
        """
        if not self.client:
            return False
        try:
            points, _ = self.client.scroll(
                collection_name="vectors", limit=1000000,
                with_payload=True, with_vectors=False,
            )
            for p in points:
                doc_id = (p.payload or {}).get("doc_id")
                if doc_id:
                    self.doc_id_map[p.id] = doc_id
                    self.reverse_map[doc_id] = p.id
            if self.doc_id_map:
                self._next_id = max(self.doc_id_map) + 1
            return bool(self.doc_id_map)
        except Exception as e:
            log.warning(f"Qdrant: не удалось восстановить doc_id_map: {e}")
            return False

    def add(self, doc_ids, vectors):
        """Добавить векторы."""
        if not QDRANT_AVAILABLE or not self.client or not vectors:
            return 0
        if not self.doc_id_map:
            if not self._load_existing_map():
                try:
                    existing = self.client.count(collection_name="vectors", exact=True).count
                except Exception:
                    swallow.note('search_engine_v2.add:L1724')
                    existing = -1
                if existing == 0:
                    return self.build(doc_ids, vectors)
                # Коллекция непустая, но карту восстановить не удалось.
                # Пересборка уничтожила бы данные — лучше пропустить.
                log.warning("Qdrant: коллекция непустая, doc_id_map недоступен — добавление пропущено")
                return 0

        try:
            points = []
            for i, (doc_id, vector) in enumerate(zip(doc_ids, vectors)):
                if not vector:
                    continue
                point_id = self._next_id + i
                points.append(PointStruct(id=point_id, payload={"doc_id": doc_id}, vector=vector))
                self.doc_id_map[point_id] = doc_id
                self.reverse_map[doc_id] = point_id
            
            if points:
                self.client.upsert(collection_name="vectors", points=points)
                self._next_id += len(doc_ids)
            
            return len(doc_ids)
        except Exception as e:
            log.error(f"Qdrant add error: {e}")
            return 0

    def search(self, query_vector, k=50):
        """Поиск ближайших соседей."""
        if not QDRANT_AVAILABLE or not self.client or not query_vector:
            return []
        
        try:
            results = self.client.query_points(
                collection_name="vectors",
                query=query_vector,
                limit=k,
            )
            
            hits = []
            for r in results.points:
                doc_id = r.payload.get("doc_id")
                if doc_id:
                    hits.append((doc_id, r.score))
            return hits
        except Exception as e:
            log.error(f"Qdrant search error: {e}")
            return []

    def save(self):
        """Qdrant сохраняет автоматически."""
        pass

    def load(self):
        """Qdrant загружает автоматически."""
        return QDRANT_AVAILABLE and self.client is not None

    def clear(self):
        """Очистить индекс."""
        if QDRANT_AVAILABLE and self.client:
            try:
                self.client.delete_collection("vectors")
            except Exception:
                swallow.note('search_engine_v2.clear:L1788')
                pass
        self.doc_id_map = {}
        self.reverse_map = {}
        self._next_id = 0


class SearchIndex:
    """Управление Whoosh BM25 индексом."""

    def __init__(self, index_dir=None):
        self.index_dir = index_dir or INDEX_DIR
        os.makedirs(self.index_dir, exist_ok=True)
        # ИСПРАВЛЕНИЕ БАГА: кешируем searcher для переиспользования
        self._searcher_cache = None
        self._searcher_index_version = None
        # StandardAnalyzer поддерживает позиционный индекс для phrase search
        _analyzer = StandardAnalyzer() if 'StandardAnalyzer' in dir() else RegexTokenizer()
        self.schema = Schema(
            doc_id=ID(stored=True, unique=True),
            source=ID(stored=True),
            title_stemmed=TEXT(analyzer=_analyzer, phrase=True, field_boost=3.0),
            title_original=STORED(),
            content_stemmed=TEXT(analyzer=_analyzer, phrase=True),
            content_original=STORED(),
            preview=STORED(),
            full_content=STORED(),
            file=STORED(),
            index_num=STORED(),
            extra=STORED(),
            created=NUMERIC(stored=True, sortable=True),
        )

    def open_or_create(self):
        if exists_in(self.index_dir):
            return open_dir(self.index_dir)
        return create_in(self.index_dir, self.schema)

    def _get_searcher(self):
        """
        ИСПРАВЛЕНИЕ БАГА: Возвращает кешированный searcher если возможно.
        Searcher создаётся заново только если индекс был перестроен.
        """
        if not WHOOSH_AVAILABLE or not exists_in(self.index_dir):
            return None

        # Получаем текущую версию индекса (generation number)
        try:
            idx = open_dir(self.index_dir)
            current_version = idx._generation
        except Exception:
            swallow.note('search_engine_v2._get_searcher:L1839')
            return None

        # Если searcher закеширован и версия совпадает — переиспользуем
        if (self._searcher_cache is not None and
            self._searcher_index_version == current_version):
            return self._searcher_cache

        # Создаём новый searcher и кешируем
        self._searcher_index_version = current_version
        self._searcher_cache = idx.searcher(weighting=BM25F())
        return self._searcher_cache

    def invalidate_searcher(self):
        """Инвалидирует кеш searcher (вызывается после rebuild)."""
        if self._searcher_cache is not None:
            try:
                self._searcher_cache.close()
            except Exception:
                swallow.note('search_engine_v2.invalidate_searcher:L1858')
                pass
            self._searcher_cache = None
            self._searcher_index_version = None

    def rebuild(self, docs):
        # ИСПРАВЛЕНИЕ БАГА: инвалидируем кеш searcher перед перестройкой
        self.invalidate_searcher()
        
        idx = create_in(self.index_dir, self.schema, indexname=None)
        writer = idx.writer(limitmb=256, procs=1)
        count = 0
        for doc in docs:
            try:
                writer.add_document(
                    doc_id=doc.get("doc_id", ""),
                    source=doc.get("source", "unknown"),
                    title_stemmed=doc.get("title", "")[:500],
                    title_original=doc.get("title_original", doc.get("title", ""))[:500],
                    content_stemmed=doc.get("content", "")[:10000],
                    content_original=doc.get("content_original", doc.get("content", ""))[:10000],
                    preview=doc.get("preview", "")[:500],
                    full_content=doc.get("full_content", "")[:10000],
                    file=doc.get("file", ""),
                    index_num=doc.get("index_num", 0),
                    extra=json.dumps(doc.get("extra", {})),
                    created=doc.get("created", 0),
                )
                count += 1
            except Exception:
                swallow.note('search_engine_v2.rebuild:L1888')
                continue
        writer.commit()
        return count

    def bm25_search(self, query, limit=30, filter_sources=None):
        if not WHOOSH_AVAILABLE or not exists_in(self.index_dir):
            return []
        try:
            # ИСПРАВЛЕНИЕ БАГА: используем кешированный searcher
            searcher = self._get_searcher()
            if searcher is None:
                # Fallback: открываем индекс напрямую
                idx = self.open_or_create()
                with idx.searcher(weighting=BM25F()) as s:
                    return self._do_bm25_search(s, query, limit, filter_sources)

            return self._do_bm25_search(searcher, query, limit, filter_sources)
        except Exception as e:
            log.error(f"BM25 error: {e}")
            return []

    def _do_bm25_search(self, searcher, query, limit, filter_sources):
        """Внутренний метод для выполнения BM25 поиска."""
        try:
            # Используем схему напрямую
            parser = MultifieldParser(
                ["title_stemmed", "content_stemmed"],
                self.schema,
                group=OrGroup
            )
            # Task 5: Phrase search plugin
            parser.add_plugin(PhrasePlugin())
            # Task 4: Fuzzy search plugin
            parser.add_plugin(FuzzyTermPlugin())

            # Авто-fuzzy для слов > 5 символов (расстояние 1-2)
            processed_query = _preprocess_fuzzy_query(query)
            q = parser.parse(processed_query)

            # Metadata filter по source
            filter_q = None
            if filter_sources:
                source_terms = [Term("source", s) for s in filter_sources]
                if len(source_terms) == 1:
                    filter_q = source_terms[0]
                else:
                    filter_q = WhooshOr(source_terms)

            results = searcher.search(q, limit=limit, filter=filter_q)
            hits = []
            for r in results:
                extra = json.loads(r.get("extra", "{}"))
                hits.append({
                    "doc_id": r["doc_id"],
                    "source": r["source"],
                    "title": r.get("title_original", ""),
                    "content": r.get("content_original", "")[:500],
                    "preview": r.get("preview", r.get("content_original", "")[:300]),
                    "full_content": r.get("full_content", ""),
                    "file": r.get("file", ""),
                    "index_num": r.get("index_num", 0),
                    "score": r.score,
                    "extra": extra,
                    "created": r.get("created", 0),
                })
            return hits
        except Exception as e:
            log.error(f"BM25 search error: {e}")
            return []

    def clear(self):
        import shutil
        import time
        if not os.path.exists(self.index_dir):
            return
        # Раньше здесь был голый shutil.rmtree(self.index_dir), и пересборка
        # индекса падала на Windows:
        #   PermissionError: [WinError 32] ... 'embeddings.db' занят другим
        #   процессом
        # потому что SQLite-соединение кэша эмбеддингов оставалось открытым
        # в момент удаления каталога. Теперь пробуем закрыть соединения и
        # удаляем с повторами, не роняя пересборку индекса из-за Windows.
        for attempt in range(5):
            try:
                self._close_open_handles()
            except Exception:
                swallow.note('search_engine_v2.clear:L1975')
                pass
            try:
                shutil.rmtree(self.index_dir)
                return
            except PermissionError as e:
                if attempt == 4:
                    # Не смогли — чистим содержимое, оставляя занятые файлы.
                    log.warning("clear(): не удалось удалить %s: %s — "
                                "удаляю содержимое", self.index_dir, e)
                    try:
                        for name in os.listdir(self.index_dir):
                            fp = os.path.join(self.index_dir, name)
                            try:
                                if os.path.isdir(fp):
                                    shutil.rmtree(fp, ignore_errors=True)
                                else:
                                    os.remove(fp)
                            except OSError:
                                swallow.note('search_engine_v2.clear:L1994')
                                pass
                    except OSError:
                        swallow.note('search_engine_v2.clear:L1997')
                        pass
                    return
                time.sleep(0.3)

    def _close_open_handles(self):
        """Закрыть SQLite-соединения, мешающие удалить каталог индекса."""
        for holder in (self, getattr(self, "engine", None)):
            if holder is None:
                continue
            cache = getattr(holder, "embed_cache", None)
            if cache is None:
                continue
            for attr in ("conn", "_conn", "connection", "db"):
                conn = getattr(cache, attr, None)
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        swallow.note('search_engine_v2._close_open_handles:L2016')
                        pass


# ===================== RRF (RECIPROCAL RANK FUSION) =====================

def rrf_fuse(result_lists, k=60, weights=None):
    """
    Reciprocal Rank Fusion.
    result_lists: [(source_name, [(doc_id, score), ...]), ...]
    weights: {source_name: weight}

    Возвращает: {doc_id: {"rrf_score": float, "best_rank": int, "sources": [...]}}
    """
    if weights is None:
        weights = {}
    fused = {}
    for source, results in result_lists:
        w = weights.get(source, 1.0)
        for rank, (doc_id, score) in enumerate(results):
            if doc_id not in fused:
                fused[doc_id] = {"rrf_score": 0.0, "best_rank": rank, "sources": []}
            fused[doc_id]["rrf_score"] += w / (k + rank + 1)
            fused[doc_id]["sources"].append(source)
            if rank < fused[doc_id]["best_rank"]:
                fused[doc_id]["best_rank"] = rank
    return fused


def _compute_rrf_weights(query, intents):
    """
    Динамические веса RRF:
    - Короткий запрос (≤ 3 слов) → boost BM25
    - Вопросительный интент → boost FAISS (семантика важнее)
    """
    words = re.findall(r'[a-zA-Zа-яА-ЯёЁ0-9]+', query)
    w_bm25, w_faiss = 1.0, 1.0

    if len(words) <= 3:
        w_bm25 = 1.5
        w_faiss = 0.7

    if 'question' in intents:
        w_bm25 *= 0.8
        w_faiss *= 1.3

    return {"bm25": w_bm25, "faiss": w_faiss}


# ===================== QUERY PROCESSING =====================

def detect_intent(query):
    q = query.lower().strip()
    intents = []
    if any(q.startswith(w) for w in ['что ', 'как ', 'почему ', 'зачем ', 'когда ', 'где ', 'кто ', 'какой ', 'сколько ', 'чем ', 'кому ', 'кем ', 'о ', 'про ', 'why ', 'what ', 'how ', 'when ', 'where ', 'who ', 'which ']):
        intents.append('question')
    if any(w in q for w in [' vs ', ' или ', ' сравнени', 'difference between', 'compare ']):
        intents.append('comparison')
    if any(w in q for w in ['как сделать', 'как настроить', 'как установить', 'how to', 'инструкци', 'гайд', 'руководств', 'tutorial']):
        intents.append('instruction')
    if any(w in q for w in ['код', 'функци', 'скрипт', 'программ', 'code', 'function', 'script', 'example', 'пример']):
        intents.append('code')
    if any(w in q for w in ['что такое', 'что значит', 'что это', 'определени', 'термин', 'what is', 'definition', 'это ']):
        intents.append('definition')
    if not intents:
        intents.append('general')
    return intents


def get_intent_boost(intents, doc_type):
    boosts = {
        'question': {'zim': 1.3, 'jsonl': 1.0, 'note': 0.8, 'ai_answer': 1.1, 'book': 0.9},
        'comparison': {'zim': 1.2, 'jsonl': 1.2, 'note': 0.7, 'ai_answer': 0.8, 'book': 1.1},
        'instruction': {'zim': 1.1, 'jsonl': 1.3, 'note': 0.9, 'ai_answer': 0.7, 'book': 1.2},
        'code': {'zim': 0.8, 'jsonl': 1.5, 'note': 0.9, 'ai_answer': 0.7, 'book': 1.0},
        'definition': {'zim': 1.5, 'jsonl': 1.0, 'note': 0.7, 'ai_answer': 0.9, 'book': 1.3},
        'general': {'zim': 1.0, 'jsonl': 1.0, 'note': 1.0, 'ai_answer': 1.0, 'book': 1.0},
    }
    boost = 1.0
    for intent in intents:
        b = boosts.get(intent, boosts['general'])
        boost *= b.get(doc_type, 1.0)
    return boost


def expand_query(query):
    """Расширение запроса: синонимы + стемминг + удаление stopwords."""
    # Сначала расширяем синонимами
    expanded = expand_query_with_synonyms(query)
    # Затем стемминг
    tokens = tokenize_and_stem(expanded)
    return ' '.join(sorted(set(tokens)))


# ===================== PASSAGE RETRIEVAL (Hybrid Chunking) =====================

def _split_by_separator(text, separator):
    """Разделить текст по сепаратору."""
    if separator == '':
        return list(text)
    parts = text.split(separator)
    result = []
    for i, part in enumerate(parts):
        if part:
            result.append(part)
        if i < len(parts) - 1 and separator:
            result[-1] = result[-1] + separator if result else separator
    return result


def _merge_small_chunks(chunks, max_chars):
    """Объединить мелкие чанки в более крупные, не превышая max_chars."""
    merged = []
    current = ""
    for chunk in chunks:
        if not chunk:
            continue
        if current and len(current) + len(chunk) + 1 > max_chars:
            merged.append(current)
            current = chunk
        else:
            current = current + chunk if not current else current + chunk
    if current:
        merged.append(current)
    return merged


def split_into_passages(text, max_chars=500, overlap=50):
    """
    Hybrid chunking: RecursiveCharacterTextSplitter.
    Разделяет по \\n\\n → \\n → ". " → символ.
    Вместо фиксированного overlap — рекурсивное деление.
    """
    if not text:
        return []
    if len(text) <= max_chars:
        return [text.strip()] if text.strip() else []

    separators = ['\n\n', '\n', '. ', '; ', ', ', ' ', '']

    def _recursive_split(t, seps):
        if len(t) <= max_chars:
            return [t.strip()] if t.strip() else []

        # Находим первый сепаратор, по которому можно разделить
        chosen_sep = None
        for sep in seps:
            if sep == '':
                chosen_sep = ''
                break
            if sep in t:
                chosen_sep = sep
                break

        if chosen_sep is None:
            chosen_sep = ''

        if chosen_sep == '':
            # Посимвольное деление
            chunks = []
            for i in range(0, len(t), max_chars):
                chunk = t[i:i + max_chars].strip()
                if chunk:
                    chunks.append(chunk)
            return chunks

        # Разделяем по сепаратору
        parts = t.split(chosen_sep)
        chunks = []
        current = ""

        for part in parts:
            candidate = current + chosen_sep + part if current else part
            if len(candidate) <= max_chars:
                current = candidate
            else:
                if current:
                    current_stripped = current.strip()
                    if current_stripped:
                        chunks.append(current_stripped)
                if len(part) > max_chars:
                    # Рекурсивно делим большую часть
                    remaining_seps = seps[seps.index(chosen_sep) + 1:] if chosen_sep in seps else []
                    sub_chunks = _recursive_split(part, remaining_seps)
                    chunks.extend(sub_chunks)
                    current = ""
                else:
                    current = part

        if current:
            current_stripped = current.strip()
            if current_stripped:
                chunks.append(current_stripped)

        return chunks

    passages = _recursive_split(text, separators)

    # Добавляем overlap: последние overlap символов предыдущего чанка
    if overlap > 0 and len(passages) > 1:
        overlapped = [passages[0]]
        for i in range(1, len(passages)):
            prev = passages[i - 1]
            curr = passages[i]
            if len(prev) > overlap:
                prefix = prev[-overlap:]
                # Добавляем пробел если склеиваемые части не имеют разделителя
                if prefix and curr and not prefix[-1].isspace() and not curr[0].isspace():
                    prefix = prefix + ' '
                overlapped.append(prefix + curr)
            else:
                overlapped.append(curr)
        passages = overlapped

    return [p for p in passages if p.strip()]


def score_passage(passage, query_words):
    if not passage or not query_words:
        return 0
    p_lower = passage.lower()
    score = 0
    for w in query_words:
        if w in p_lower:
            score += 2
        elif len(w) > 3 and w[:3] in p_lower:
            score += 1
    density = sum(1 for w in query_words if w in p_lower) / max(len(query_words), 1)
    score += density * 3
    return score


def extract_best_passages(full_text, query, max_passages=3, max_chars_per_passage=500):
    if not full_text:
        return ""
    query_words = [w.lower() for w in re.findall(r'[a-zA-Zа-яА-ЯёЁ0-9]+', query)]
    passages = split_into_passages(full_text, max_chars=max_chars_per_passage)
    scored = [(score_passage(p, query_words), i, p) for i, p in enumerate(passages)]
    scored.sort(key=lambda x: x[0], reverse=True)
    best = scored[:max_passages]
    best.sort(key=lambda x: x[1])
    return '\n...\n'.join(p for _, _, p in best)


# ===================== СБОР ДОКУМЕНТОВ =====================

def collect_documents(dirs, notes_dir=None, ai_dir=None, books_dir=None, zim_dirs=None, html_dirs=None):
    docs = []

    # 1. JSONL
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if not f.endswith(".jsonl"):
                continue
            fp = os.path.join(d, f)
            try:
                with open(fp, encoding="utf-8") as fh:
                    for i, line in enumerate(fh):
                        try:
                            it = json.loads(line)
                            title = it.get("title", "") or it.get("question", "") or ""
                            text = " ".join(str(it.get(k, "")) for k in ["title", "question", "selftext", "text", "body"])
                            clean = _clean_html(text)
                            docs.append({
                                "doc_id": f"jsonl:{fp}:{i}",
                                "source": "jsonl",
                                "title": stem_text(title[:500]),
                                "title_original": title[:500],
                                "content": stem_text(clean[:8000]),
                                "content_original": clean[:8000],
                                "preview": _clean_html(text)[:500],
                                "full_content": _clean_html(text)[:8000],
                                "file": f,
                                "index_num": i + 1,
                                "extra": {"type": "jsonl"},
                            })
                        except Exception:
                            swallow.note('search_engine_v2.collect_documents:L2295')
                            continue
            except Exception:
                swallow.note('search_engine_v2.collect_documents:L2298')
                continue

    # 2. Заметки
    if notes_dir and os.path.isdir(notes_dir):
        for root, _, files in os.walk(notes_dir):
            for fn in files:
                if not fn.endswith(".json") or fn.startswith("_"):
                    continue
                try:
                    with open(os.path.join(root, fn), encoding="utf-8") as fh:
                        art = json.load(fh)
                    text = f"{art.get('title', '')} {art.get('content', '')}"
                    docs.append({
                        "doc_id": f"note:{fn}",
                        "source": "note",
                        "title": stem_text(art.get("title", "")[:500]),
                        "title_original": art.get("title", "")[:500],
                        "content": stem_text(text[:8000]),
                        "content_original": text[:8000],
                        "preview": art.get("content", "")[:500],
                        "full_content": art.get("content", "")[:8000],
                        "file": "notes",
                        "index_num": 0,
                        "extra": {"type": "note", "note_id": art.get("id", "")},
                        "created": art.get("created", 0),
                    })
                except Exception:
                    swallow.note('search_engine_v2.collect_documents:L2326')
                    continue

    # 3. AI ответы
    if ai_dir and os.path.isdir(ai_dir):
        for fn in os.listdir(ai_dir):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(ai_dir, fn), encoding="utf-8") as fh:
                    data = json.load(fh)
                text = f"{data.get('question', '')} {data.get('answer', '')}"
                docs.append({
                    "doc_id": f"ai:{fn}",
                    "source": "ai_answer",
                    "title": stem_text(data.get("question", "")[:500]),
                    "title_original": data.get("question", "")[:500],
                    "content": stem_text(text[:8000]),
                    "content_original": text[:8000],
                    "preview": data.get("answer", "")[:500],
                    "full_content": data.get("answer", "")[:8000],
                    "file": "ai_answers",
                    "index_num": 0,
                    "extra": {"type": "ai_answer"},
                    "created": data.get("created", 0),
                })
            except Exception:
                swallow.note('search_engine_v2.collect_documents:L2353')
                continue

    # 4. Книги — каталогов два и больше (общедоступный + свой у профиля),
    #    параметр принимает и строку, и список: прежние вызовы не меняются.
    for _bd in ([books_dir] if isinstance(books_dir, str) else list(books_dir or [])):
        if _bd and os.path.isdir(_bd):
            _collect_books_fast(_bd, docs)
    # Также индексируем коллекцию Flibusta (SQLite), если есть
    _coll_db_path = _CFG.get("books_db_path", "")
    if _coll_db_path and os.path.exists(_coll_db_path):
        _collect_collection_fast(_coll_db_path, docs)

    # 5. ZIM
    if zim_dirs:
        _collect_zim_fast(zim_dirs, docs)

    # 6. HTML файлы — теперь индексируются вместе с остальными!
    if html_dirs:
        for hd in html_dirs:
            if not os.path.isdir(hd):
                continue
            for root, _, files in os.walk(hd):
                for fn in files:
                    if not fn.lower().endswith(('.html', '.htm')):
                        continue
                    try:
                        fp = os.path.join(root, fn)
                        with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                            content = fh.read()
                        # Извлекаем текст
                        import re
                        text = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL)
                        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
                        text = re.sub(r'<[^>]+>', ' ', text)
                        text = re.sub(r'\s+', ' ', text).strip()
                        # Заголовок
                        title_m = re.search(r'<title[^>]*>([^<]+)</title>', content, re.IGNORECASE)
                        title = title_m.group(1).strip() if title_m else fn
                        docs.append({
                            "doc_id": f"html:{fp}",
                            "source": "html",
                            "title": stem_text(title[:500]),
                            "title_original": title[:500],
                            "content": stem_text(text[:10000]),
                            "content_original": text[:10000],
                            "preview": text[:500],
                            "full_content": text[:10000],
                            "file": fn,
                            "index_num": 0,
                            "extra": {"type": "html", "html_path": fp, "html_dir": hd},
                            "created": os.path.getmtime(fp),
                        })
                    except Exception:
                        swallow.note('search_engine_v2.collect_documents:L2407')
                        continue

    return docs


def _collect_books_fast(books_dir, docs):
    """Сбор документов из книг — индексация по главам/чанкам."""
    BOOK_EXTENSIONS = {'.epub', '.pdf', '.fb2', '.docx', '.txt', '.djvu'}
    MAX_CHUNK_CHARS = 8000  # Размер одного чанка для индексации
    MAX_CHUNKS_PER_BOOK = 50  # Максимум чанков на книгу

    def _split_book_into_chunks(text, book_title, book_file, ext, max_chunks=MAX_CHUNKS_PER_BOOK):
        """Разбить текст книги на чанки по главам/параграфам."""
        if not text or len(text.strip()) < 100:
            return []

        chunks = []
        # Пробуем разбить по заголовкам глав
        chapter_splits = re.split(r'(?m)^(#{1,3}\s+|Глава\s+\d+|Chapter\s+\d+|PART\s+\d+)', text)

        if len(chapter_splits) > 3:
            # Есть заголовки — разбиваем по ним
            current_chapter = ""
            chapter_num = 0
            for part in chapter_splits:
                if re.match(r'^(#{1,3}\s+|Глава\s+\d+|Chapter\s+\d+|PART\s+\d+)', part):
                    if current_chapter.strip():
                        chunks.append((chapter_num, current_chapter.strip()))
                        chapter_num += 1
                    current_chapter = part
                else:
                    current_chapter += part
            if current_chapter.strip() and chapter_num < max_chunks:
                chunks.append((chapter_num, current_chapter.strip()))
        else:
            # Нет заголовков — разбиваем по фиксированному размеру
            for i in range(0, min(len(text), MAX_CHUNK_CHARS * max_chunks), MAX_CHUNK_CHARS):
                chunk = text[i:i + MAX_CHUNK_CHARS].strip()
                if chunk:
                    chunk_label = f"Часть {len(chunks) + 1}"
                    chunks.append((len(chunks), chunk))

        return chunks[:max_chunks]

    # EPUB reader — возвращает все главы
    def _read_epub_full(fp):
        try:
            from ebooklib import epub
            from bs4 import BeautifulSoup
            import ebooklib as _eb
            book = epub.read_epub(fp)
            texts = []
            for item in book.get_items():
                if item.get_type() == _eb.ITEM_DOCUMENT:
                    soup = BeautifulSoup(item.get_content().decode('utf-8', errors='replace'), 'html.parser')
                    t = soup.get_text(separator=' ', strip=True)
                    if t.strip():
                        texts.append(t)
            return ' '.join(texts)
        except Exception:
            swallow.note('search_engine_v2._read_epub_full:L2468')
            return ""

    # PDF reader — все страницы
    def _read_pdf_full(fp):
        try:
            import PyPDF2
            texts = []
            with open(fp, 'rb') as f:
                reader = PyPDF2.PdfReader(f)
                for page in reader.pages:
                    t = page.extract_text()
                    if t:
                        texts.append(t)
            return '\n\n'.join(texts)
        except Exception:
            swallow.note('search_engine_v2._read_pdf_full:L2484')
            return ""

    # FB2 reader — весь текст
    def _read_fb2_full(fp):
        try:
            from lxml import etree
            tree = etree.parse(fp)
            ns = {'fb': 'http://www.gribuser.ru/xml/fictionbook/2.0'}
            texts = []
            for p in tree.iterfind('.//fb:p', ns):
                if p.text:
                    texts.append(p.text)
            return ' '.join(texts)
        except Exception:
            swallow.note('search_engine_v2._read_fb2_full:L2499')
            return ""

    # TXT reader — весь текст
    def _read_txt_full(fp):
        for enc in ['utf-8', 'cp1251', 'latin-1']:
            try:
                with open(fp, 'r', encoding=enc) as f:
                    return f.read()
            except Exception:
                swallow.note('search_engine_v2._read_txt_full:L2509')
                continue
        return ""

    # DOCX reader — весь текст
    def _read_docx_full(fp):
        try:
            import docx as _docx
            d = _docx.Document(fp)
            texts = [p.text for p in d.paragraphs if p.text]
            return '\n\n'.join(texts)
        except Exception:
            swallow.note('search_engine_v2._read_docx_full:L2521')
            return ""

    # DjVu reader — через djvutxt subprocess
    def _read_djvu_full(fp):
        try:
            import subprocess
            result = subprocess.run(
                ['djvutxt', fp],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            return ""
        except Exception:
            swallow.note('search_engine_v2._read_djvu_full:L2536')
            return ""

    readers = {
        '.epub': _read_epub_full,
        '.pdf': _read_pdf_full,
        '.fb2': _read_fb2_full,
        '.txt': _read_txt_full,
        '.docx': _read_docx_full,
        '.djvu': _read_djvu_full,
    }

    try:
        books_processed = 0
        books_errors = 0
        import compression_module as _cz
        for root, _, _files in os.walk(books_dir):
            # list_logical отдаёт имена без .zst; сжатые книги читаются через
            # временную распаковку, чтобы индексация не «теряла» библиотеку.
            for fn, _real, _packed in _cz.list_logical(root):
                ext = os.path.splitext(fn)[1].lower()
                if ext not in BOOK_EXTENSIONS:
                    continue
                fp = os.path.join(root, fn)
                if os.path.isdir(fp):
                    continue
                reader = readers.get(ext)
                if not reader:
                    continue
                try:
                    with _cz.temp_file(fp) as real:
                        full_text = reader(real)
                    if not full_text or len(full_text.strip()) < 100:
                        continue

                    book_title = os.path.splitext(fn)[0]
                    chunks = _split_book_into_chunks(full_text, book_title, fn, ext)

                    if not chunks:
                        # Fallback: один документ
                        docs.append({
                            "doc_id": f"book:{fp}",
                            "source": "book",
                            "title": stem_text(book_title[:500]),
                            "title_original": book_title[:500],
                            "content": stem_text(full_text[:MAX_CHUNK_CHARS]),
                            "content_original": full_text[:MAX_CHUNK_CHARS],
                            "preview": _clean_html(full_text)[:500],
                            "full_content": _clean_html(full_text)[:MAX_CHUNK_CHARS],
                            "file": fn,
                            "index_num": 0,
                            "extra": {"type": "book", "format": ext[1:], "chapter": "Полная книга"},
                        })
                    else:
                        # Каждый чанк — отдельный документ
                        for ch_num, ch_text in chunks:
                            ch_title = ch_text[:200].split('\n')[0].strip()
                            if len(ch_title) > 100:
                                ch_title = ch_title[:100] + '...'
                            docs.append({
                                "doc_id": f"book:{fp}:ch{ch_num}",
                                "source": "book",
                                "title": stem_text(f"{book_title} — {ch_title}"[:500]),
                                "title_original": f"{book_title} — {ch_title}"[:500],
                                "content": stem_text(ch_text[:MAX_CHUNK_CHARS]),
                                "content_original": ch_text[:MAX_CHUNK_CHARS],
                                "preview": _clean_html(ch_text)[:500],
                                "full_content": _clean_html(ch_text)[:MAX_CHUNK_CHARS],
                                "file": fn,
                                "index_num": ch_num,
                                "extra": {"type": "book", "format": ext[1:], "chapter": ch_num + 1, "book_title": book_title},
                            })
                    books_processed += 1
                    if books_processed % 10 == 0:
                        log.info(f"Книги: обработано {books_processed} файлов, в индексе {len(docs)} документов")
                except Exception as e:
                    books_errors += 1
                    log.warning(f"Ошибка индексации книги {fn}: {e}")
                    continue
        log.info(f"Книги завершены: {books_processed} файлов, {len(docs)} документов, {books_errors} ошибок")
    except Exception as e:
        log.error(f"Критическая ошибка индексации книг: {e}")


def _collect_collection_fast(collection_db_path, docs):
    """Сбор документов из SQLite коллекции (Flibusta) — индексация книг из БД."""
    if not collection_db_path or not os.path.exists(collection_db_path):
        log.warning(f"БД коллекции не найдена: {collection_db_path}")
        return
    try:
        import sqlite3
        conn = sqlite3.connect(collection_db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT b.id, b.title, b.description, b.lang, b.year,
                   GROUP_CONCAT(DISTINCT a.full_name, ', ') as authors
            FROM books b
            LEFT JOIN book_authors ba ON ba.book_id = b.id
            LEFT JOIN authors a ON a.id = ba.author_id
            GROUP BY b.id
            ORDER BY b.id
        """).fetchall()
        conn.close()
        if not rows:
            log.info("Коллекция пуста, нечего индексировать")
            return
        for row in rows:
            title = row['title'] or ''
            authors = row['authors'] or ''
            description = row['description'] or ''
            book_id = row['id']
            text = f"{title} {authors} {description}"
            if len(text.strip()) < 20:
                continue
            clean = _clean_html(text)
            doc_title = f"{title} — {authors}" if authors else title
            docs.append({
                "doc_id": f"collection:{book_id}",
                "source": "book",
                "title": stem_text(doc_title[:500]),
                "title_original": doc_title[:500],
                "content": stem_text(clean[:8000]),
                "content_original": clean[:8000],
                "preview": _clean_html(description)[:500] if description else clean[:500],
                "full_content": clean[:8000],
                "file": f"collection_{book_id}",
                "index_num": book_id,
                "extra": {"type": "book", "subtype": "collection", "collection_id": book_id},
                "created": 0,
            })
        log.info(f"Коллекция: добавлено {len(docs)} книг из БД")
    except Exception as e:
        log.error(f"Ошибка индексации коллекции: {e}")


def _collect_zim_fast(zim_dirs, docs):
    """Сбор документов из ZIM файлов через search API."""
    try:
        from libzim.reader import Archive
        from libzim.search import Query, Searcher
    except ImportError:
        log.warning("libzim не установлен, ZIM индексация пропущена")
        return

    # Расширенный набор поисковых терминов для максимального покрытия статей
    SEARCH_TERMS = [
        # Русские частые слова
        'и', 'в', 'на', 'что', 'как', 'это', 'не', 'по', 'о', 'с', 'для', 'от', 'за', 'из',
        'а', 'но', 'или', 'при', 'до', 'он', 'она', 'они', 'мы', 'вы', 'быть', 'стать',
        'его', 'её', 'их', 'мой', 'твой', 'свой', 'весь', 'каждый', 'который',
        'один', 'два', 'три', 'четыре', 'пять', 'год', 'время', 'человек', 'жизнь',
        # Английские частые слова
        'a', 'the', 'and', 'of', 'to', 'in', 'is', 'for', 'with', 'that', 'it', 'on', 'at',
        'by', 'from', 'as', 'was', 'are', 'been', 'have', 'has', 'had', 'do', 'does', 'did',
        'will', 'would', 'could', 'should', 'may', 'might', 'can', 'shall',
        # Технические термины
        'python', 'linux', 'data', 'web', 'api', 'computer', 'software', 'science', 'math',
        'history', 'biology', 'chemistry', 'physics', 'geography', 'language', 'literature',
        # Буквы и цифры
        'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M',
        'N', 'O', 'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y', 'Z',
        '1', '2', '3', '4', '5', '6', '7', '8', '9', '0',
    ]
    MAX_RESULTS_PER_TERM = 2000

    # Счётчик для логирования
    total_zim_docs = 0

    for d in zim_dirs:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if not f.lower().endswith(".zim"):
                continue
            fp = os.path.join(d, f)
            try:
                archive = Archive(fp)
                zim_file_docs = 0
                seen_paths = set()
                searcher = Searcher(archive)

                log.info(f"ZIM [{f}]: article_count={archive.article_count}, has_fulltext={archive.has_fulltext_index}, has_title={archive.has_title_index}")

                # Используем search API для нахождения статей
                for q_term in SEARCH_TERMS:
                    try:
                        q = Query().set_query(q_term)
                        search = searcher.search(q)
                        est_matches = search.getEstimatedMatches()
                        if est_matches == 0:
                            continue
                        
                        for path in search.getResults(0, min(est_matches, MAX_RESULTS_PER_TERM)):
                            if path in seen_paths:
                                continue
                            seen_paths.add(path)
                            
                            try:
                                entry = archive.get_entry_by_path(path)
                                if entry.is_redirect:
                                    continue
                                    
                                item = entry.get_item()
                                raw = bytes(item.content).decode("utf-8", errors="replace")
                                clean = re.sub(r'<[^>]+>', ' ', raw)
                                clean = re.sub(r'\s+', ' ', clean).strip()
                                
                                if clean and len(clean) >= 100:
                                    docs.append({
                                        "doc_id": f"zim:{fp}:{path}",
                                        "source": "zim",
                                        "title": stem_text(entry.title[:500]),
                                        "title_original": entry.title[:500],
                                        "content": stem_text(clean[:8000]),
                                        "content_original": clean[:8000],
                                        "preview": clean[:500],
                                        "full_content": clean[:8000],
                                        "file": f,
                                        "index_num": 0,
                                        "extra": {"type": "zim", "zim_path": path, "zim_file": f},
                                    })
                                    zim_file_docs += 1
                                    
                                    # Логирование прогресса каждые 1000 статей
                                    if zim_file_docs % 1000 == 0:
                                        log.info(f"ZIM [{f}]: обработано {zim_file_docs} статей...")
                            except Exception:
                                swallow.note('search_engine_v2._collect_zim_fast:L2763')
                                continue
                    except Exception:
                        swallow.note('search_engine_v2._collect_zim_fast:L2766')
                        continue

                total_zim_docs += zim_file_docs
                log.info(f"ZIM [{f}]: проиндексировано {zim_file_docs} статей (уникальных путей: {len(seen_paths)})")

            except Exception as e:
                log.error(f"ZIM ошибка обработки файла {f}: {e}")
                continue

    if total_zim_docs > 0:
        log.info(f"ZIM индексация завершена: всего {total_zim_docs} статей из всех ZIM файлов")
    else:
        log.warning("ZIM индексация не нашла статей — проверьте файлы и libzim")


# ===================== ГИБРИДНЫЙ ПОИСК v3 =====================

class HybridSearch:
    """
    Гибридный поиск v3:
    Tantivy BM25 + Qdrant ANN + RRF Fusion + Intent Detection + Passage Retrieval
    """

    def __init__(self, index_dir=None, embed_model=None):
        self.index_dir = index_dir or INDEX_DIR
        self.embed_model = embed_model or LM_STUDIO_EMBED_MODEL
        # BM25: Tantivy (быстрый, приоритет) → Whoosh (fallback)
        if TANTIVY_AVAILABLE:
            from tantivy_qdrant_search import TantivyIndex
            self.bm25_index = TantivyIndex(self.index_dir)
            log.info("BM25 engine: Tantivy (fast)")
        elif WHOOSH_AVAILABLE:
            self.bm25_index = SearchIndex(self.index_dir)
            log.info("BM25 engine: Whoosh (fallback)")
        else:
            self.bm25_index = None
            log.warning("BM25 engine: none")
        # Vector: Qdrant (приоритет) → FAISS (fallback)
        if QDRANT_AVAILABLE:
            # Размерность не передаём: QdrantIndex сам возьмёт её у реально
            # используемой модели эмбеддингов (сейчас 384, а константа
            # DIMENSION = 768 от Jina/e5 — с ней индекс оставался пустым).
            self.vector_index = QdrantIndex(index_dir=self.index_dir)
            log.info("Vector store: Qdrant")
        elif FAISS_AVAILABLE:
            self.vector_index = FaissIndex(DIMENSION, self.index_dir)
            log.info("Vector store: FAISS (fallback)")
        else:
            self.vector_index = None
            log.warning("Vector store: none")
        self.embed_cache = EmbeddingCache()
        self._doc_store = {}  # doc_id -> doc data (для финальной выдачи)

    def rebuild(self, docs):
        """Обёртка для rebuild_index."""
        return self.rebuild_index(docs)

    def rebuild_index(self, docs):
        """Полная перестройка BM25 + Vector индексов."""
        log.info(f"Перестройка индекса: {len(docs)} документов")

        # Сохраняем документы для быстрого доступа
        self._doc_store = {d["doc_id"]: d for d in docs}

        # 1. BM25 (Tantivy или Whoosh)
        bm25_count = 0
        if self.bm25_index:
            bm25_count = self.bm25_index.rebuild(docs)
            log.info(f"BM25: {bm25_count} документов")

        # 2. Vector (Qdrant или FAISS) — собираем эмбеддинги
        #
        # Ключ кэша завязан на содержимое, а не только на doc_id: у заметки или
        # книги, отредактированных на месте, идентификатор не меняется, и раньше
        # их векторы навсегда оставались от прежнего текста.
        doc_ids = []
        vectors = []
        pending = []          # (позиция в vectors, ключ, текст)
        for doc in docs:
            text = f"{doc.get('title', '')} {doc.get('content_original', doc.get('full_content', doc.get('preview', '')))}"[:8000]
            key = "doc:" + hashlib.md5(
                (doc["doc_id"] + "\0" + text).encode("utf-8", "replace")).hexdigest()
            doc_ids.append(doc["doc_id"])
            vectors.append(None)
            pending.append((len(doc_ids) - 1, key, text))

        # Кэш читаем пачками по 500 ключей: SELECT на каждый документ на
        # десятках тысяч чанков стоил дороже самого моделирования.
        todo = []
        for i in range(0, len(pending), 500):
            chunk = pending[i:i + 500]
            found = self.embed_cache.get_many([key for _, key, _ in chunk])
            for idx, key, text in chunk:
                cached = found.get(key)
                if cached:
                    vectors[idx] = cached
                else:
                    todo.append((idx, key, text))

        # Одинаковые тексты (повторяющиеся чанки, дубли записей) кодируем один раз.
        missing = {}
        for idx, key, text in todo:
            missing.setdefault(key, [text, []])[1].append(idx)
        cache_keys = [(key, val[0], val[1]) for key, val in missing.items()]
        if len(cache_keys) != len(todo):
            log.info(f"Эмбеддинги: {len(todo)} промахов кэша свелись к "
                     f"{len(cache_keys)} уникальным текстам")

        # Получаем недостающие эмбеддинги батчами
        batch_size = 64
        for i in range(0, len(cache_keys), batch_size):
            batch = cache_keys[i:i + batch_size]
            texts = [item[1] for item in batch]
            embeds = get_embeddings(texts, self.embed_model)
            to_cache = []
            for (key, _text, idxs), emb in zip(batch, embeds):
                if emb:
                    for idx in idxs:
                        vectors[idx] = emb
                    to_cache.append((key, emb))
            if to_cache:
                self.embed_cache.put_many(to_cache)

        # Фильтруем пустые
        valid_ids = [did for did, v in zip(doc_ids, vectors) if v]
        valid_vecs = [v for v in vectors if v]

        if self.vector_index and valid_vecs:
            vector_count = self.vector_index.build(valid_ids, valid_vecs)
            log.info(f"Vector: {vector_count} векторов")
        else:
            log.warning("Vector: нет векторов для индексации")

        return bm25_count

    def search(self, query, limit=20, use_embeddings=None, bm25_limit=None, faiss_limit=None, filter_sources=None):
        """
        Полный гибридный поиск с RRF.

        0. Spell correction (SymSpell)
        1. Intent detection
        2. Query expansion (synonyms + stemming)
        3. BM25 search (Whoosh) + metadata filtering
        4. FAISS semantic search
        5. RRF fusion
        6. Intent boosting
        7. Recency boost
        8. Cross-encoder reranking (top-20)
        9. Passage extraction (hybrid chunking)
        """
        # Настройки раздела «Поиск» читаем в момент запроса: файл конфига
        # перечитывается при каждом сохранении, а значения по умолчанию в
        # сигнатуре замораживали бы их до перезапуска.
        try:
            _s = get_config().get("search") or {}
        except Exception:
            swallow.note('search_engine_v2.search:L2923')
            _s = {}
        if use_embeddings is None:
            use_embeddings = _s.get("use_embeddings", True) is not False
        if bm25_limit is None:
            bm25_limit = int(_s.get("bm25_limit") or 50)
        if faiss_limit is None:
            faiss_limit = int(_s.get("faiss_limit") or 50)
        _spell_on = _s.get("spell_correction_enabled", True) is not False
        _recency_on = _s.get("recency_boost_enabled", True) is not False
        _rerank_on = _s.get("cross_encoder_enabled", True) is not False

        # 0. Spell correction
        corrected = correct_spelling(query) if _spell_on else query
        if corrected != query:
            log.info(f"Spell correction: '{query}' → '{corrected}'")

        intents = detect_intent(corrected)
        expanded = expand_query(corrected)
        log.info(f"Query: '{corrected}' | Expanded: '{expanded}' | Intents: {intents}")

        # --- BM25 ---
        bm25_results = self.bm5_search_safe(expanded, limit=bm25_limit, filter_sources=filter_sources)

        # --- FAISS ---
        faiss_results = []
        if use_embeddings and (FAISS_AVAILABLE or QDRANT_AVAILABLE):
            faiss_results = self._faiss_search_safe(query, limit=faiss_limit)

        # --- RRF Fusion ---
        rrf_weights = _compute_rrf_weights(query, intents)
        if bm25_results and faiss_results:
            # Подготавливаем списки для RRF
            bm25_list = [(r["doc_id"], r["score"]) for r in bm25_results]
            faiss_list = [(r["doc_id"], r["score"]) for r in faiss_results]

            fused = rrf_fuse(
                [("bm25", bm25_list), ("faiss", faiss_list)],
                k=60,
                weights=rrf_weights
            )

            # Собираем результаты
            all_docs = {}
            for r in bm25_results:
                all_docs[r["doc_id"]] = r
            for r in faiss_results:
                if r["doc_id"] not in all_docs:
                    all_docs[r["doc_id"]] = r

            results = []
            for doc_id, fuse_data in fused.items():
                doc = all_docs.get(doc_id, {})
                doc_type = doc.get("extra", {}).get("type", doc.get("source", "jsonl"))
                intent_boost = get_intent_boost(intents, doc_type)
                # Task 7: Recency boost
                recency_boost = _compute_recency_boost(doc.get("created", 0)) if _recency_on else 1.0

                results.append({
                    **doc,
                    "rrf_score": fuse_data["rrf_score"],
                    "hybrid_score": fuse_data["rrf_score"] * intent_boost * recency_boost,
                    "sources": fuse_data["sources"],
                })

            results.sort(key=lambda x: x["hybrid_score"], reverse=True)

        elif bm25_results:
            # Только BM25
            for r in bm25_results:
                doc_type = r.get("extra", {}).get("type", r.get("source", "jsonl"))
                boost = get_intent_boost(intents, doc_type)
                recency_boost = _compute_recency_boost(r.get("created", 0)) if _recency_on else 1.0
                r["hybrid_score"] = r["score"] * boost * recency_boost
                r["rrf_score"] = r["score"]
                r["sources"] = ["bm25"]
            bm25_results.sort(key=lambda x: x["hybrid_score"], reverse=True)
            results = bm25_results

        elif faiss_results:
            # Только FAISS
            for r in faiss_results:
                doc_type = r.get("extra", {}).get("type", r.get("source", "unknown"))
                boost = get_intent_boost(intents, doc_type)
                recency_boost = _compute_recency_boost(r.get("created", 0)) if _recency_on else 1.0
                r["hybrid_score"] = r["score"] * boost * recency_boost
                r["rrf_score"] = r["score"]
                r["sources"] = ["faiss"]
            faiss_results.sort(key=lambda x: x["hybrid_score"], reverse=True)
            results = faiss_results
        else:
            results = []

        # Task 6: Cross-encoder reranking top-20
        rerank_pool_size = min(20, len(results))
        if rerank_pool_size > 0 and CROSS_ENCODER_AVAILABLE and _rerank_on:
            reranked = rerank_with_cross_encoder(query, results[:rerank_pool_size], top_k=rerank_pool_size)
            # ИСПРАВЛЕНИЕ БАГА: reranked уже отсортирован по rerank_score.
            # Нужно объединить с остальными и отсортировать по финальному score
            remaining = results[rerank_pool_size:]
            # Присваиваем rerank_score=0 для оставшихся документов
            for r in remaining:
                if "rerank_score" not in r:
                    r["rerank_score"] = 0.0
            results = reranked + remaining
            # ФИНАЛЬНАЯ СОРТИРОВКА по rerank_score (если есть) или hybrid_score
            results.sort(key=lambda x: x.get("rerank_score", x.get("hybrid_score", 0)), reverse=True)

        # Passage extraction
        results = self._add_passages(results[:limit], query)
        return results[:limit]

    def bm5_search_safe(self, query, limit=50, filter_sources=None):
        """BM25 с fallback на расширенный запрос."""
        results = self.bm25_index.bm25_search(query, limit=limit, filter_sources=filter_sources)
        if not results and query != expand_query(query):
            results = self.bm25_index.bm25_search(expand_query(query), limit=limit, filter_sources=filter_sources)
        return results

    def _faiss_search_safe(self, query, limit=50):
        """Безопасный поиск через FAISS."""
        if self.vector_index.ntotal == 0:
            return []

        query_vec = self._get_query_embedding(query)
        if not query_vec:
            return []

        faiss_hits = self.vector_index.search(query_vec, k=limit)
        results = []
        for doc_id, score in faiss_hits:
            doc = self._doc_store.get(doc_id, {})
            if not doc:
                doc = {"doc_id": doc_id, "source": "unknown"}
            results.append({
                **doc,
                "score": score,
            })
        return results

    def _get_query_embedding(self, query):
        key = f"q:{hashlib.md5(query.encode()).hexdigest()}"
        cached = self.embed_cache.get(key)
        if cached:
            return cached
        embeds = get_embeddings(query, self.embed_model)
        if embeds and embeds[0]:
            self.embed_cache.put(key, embeds[0])
            return embeds[0]
        return []

    def _add_passages(self, results, query):
        for r in results:
            full = r.get("full_content", "")
            if full and len(full) > 500:
                r["best_passage"] = extract_best_passages(full, query, max_passages=2, max_chars_per_passage=400)
            else:
                r["best_passage"] = r.get("preview", "")
        return results

    def clear_all(self):
        """Полная очистка всех индексов."""
        self.bm25_index.clear()
        self.vector_index.clear()
        self.embed_cache.close()
        if os.path.exists(EMBEDDING_DB_PATH):
            os.remove(EMBEDDING_DB_PATH)
        self.embed_cache = EmbeddingCache()
        self._doc_store = {}

    def get_stats(self):
        """Статистика индексов — все компоненты с статусом."""
        # Проверяем reranker с auto-load
        _check_lm_reranker(auto_load=True)
        _check_pytorch_reranker()
        # Лениво загружаем словари SymSpell
        if SYMSPELL_AVAILABLE:
            _get_symspell_ru()
            _get_symspell_en()

        # Определяем активный BM25
        bm25_engine = "none"
        bm25_status = "inactive"
        if TANTIVY_AVAILABLE:
            bm25_engine = "tantivy"
            bm25_status = "active" if hasattr(self, 'bm25_index') and self.bm25_index is not None else "inactive"
        elif WHOOSH_AVAILABLE:
            bm25_engine = "whoosh"
            try:
                bm25_status = "active" if exists_in(self.index_dir) else "inactive"
            except Exception:
                swallow.note('search_engine_v2.get_stats:L3114')
                bm25_status = "inactive"

        # Определяем активный векторный поиск
        vector_engine = "none"
        vector_status = "inactive"
        if QDRANT_AVAILABLE:
            vector_engine = "qdrant"
            vector_status = "active" if hasattr(self.vector_index, 'client') and self.vector_index.client else "inactive"
        elif FAISS_AVAILABLE:
            vector_engine = "faiss"
            vector_status = "active" if hasattr(self.vector_index, 'ntotal') and self.vector_index.ntotal > 0 else "inactive"

        # LM Studio embeddings статус
        lm_embed_status = "active" if _LM_EMBED_AVAILABLE else "inactive"

        # Cross-encoder (PyTorch fallback)
        cross_encoder_loaded = PYTORCH_RERANKER_AVAILABLE
        cross_encoder_status = "active" if PYTORCH_RERANKER_AVAILABLE else "inactive"

        return {
            "bm25": {
                "engine": bm25_engine,
                "status": bm25_status,
                "whoosh_available": WHOOSH_AVAILABLE,
                "tantivy_available": TANTIVY_AVAILABLE,
            },
            "vector": {
                "engine": vector_engine,
                "status": vector_status,
                "vectors": self.vector_index.ntotal if hasattr(self.vector_index, 'ntotal') else 0,
                "dimension": self.vector_index.dimension if hasattr(self.vector_index, 'dimension') else 0,
                "qdrant_available": QDRANT_AVAILABLE,
                "faiss_available": FAISS_AVAILABLE,
            },
            "embedding_model": self.embed_model,
            "cross_encoder": {
                "available": PYTORCH_RERANKER_AVAILABLE,
                "status": cross_encoder_status,
                "model": RERANKER_MODEL_PYTORCH,
                "loaded": cross_encoder_loaded,
            },
            "lm_reranker": {
                "available": _LM_RERANKER_AVAILABLE or _LM_EMBED_AVAILABLE,
                "status": "active" if (_LM_RERANKER_AVAILABLE or _LM_EMBED_AVAILABLE) else "inactive",
                "reranker_model": _LM_RERANKER_MODEL_ID or RERANKER_MODEL_LM_STUDIO,
                "embeddings_model": self.embed_model,
                "url": LM_STUDIO_BASE_URL_RERANK,
                "auto_load": True,
            },
            "fuzzy_search": True,
            "phrase_search": True,
            "recency_boost": True,
            "spell_correction": {
                "available": SYMSPELL_AVAILABLE,
                "en_loaded": _SYMSPELL_EN is not None,
                "ru_loaded": _SYMSPELL_RU is not None,
            },
            "query_expansion": {
                "synonyms": True,
                "en_synonyms_count": len(_EN_SYNONYMS),
                "ru_synonyms_count": len(_RU_SYNONYMS),
            },
            "hybrid_chunking": True,
            "embed_cache": self.embed_cache.count(),
            "pymorphy_available": PYMORPHY_AVAILABLE,
            "nltk_available": NLTK_AVAILABLE,
        }


# ===================== УТИЛИТЫ =====================

def format_search_results(results, max_results=10):
    parts = []
    for r in results[:max_results]:
        stype = r.get("source", "unknown")
        preview = r.get("best_passage", r.get("preview", ""))
        if stype == "zim":
            parts.append(f"[ZIM: {r.get('title', '')}]\n{preview}")
        elif stype == "note":
            parts.append(f"[Заметка: {r.get('title', '')}]\n{preview}")
        elif stype == "ai_answer":
            parts.append(f"[AI память: {r.get('title', '')}]\n{preview}")
        elif stype == "book":
            parts.append(f"[Книга: {r.get('file', '')}]\n{preview}")
        else:
            parts.append(f"[{r.get('file', '')} #{r.get('index_num', '')}] {r.get('title', '')}: {preview}")
    return "\n\n".join(parts)


# ===================== ИНКРЕМЕНТАЛЬНАЯ ИНДЕКСАЦИЯ =====================

_META_FILE = os.path.join(INDEX_DIR, "index_meta.json")


def _get_file_mtimes(dirs):
    """Собрать mtime всех файлов в директориях."""
    mtimes = {}
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for fn in files:
                fp = os.path.join(root, fn)
                try:
                    mtimes[fp] = os.path.getmtime(fp)
                except Exception:
                    swallow.note('search_engine_v2._get_file_mtimes:L3221')
                    pass
    return mtimes


def _load_meta():
    """Загрузить метаданные индекса."""
    if os.path.exists(_META_FILE):
        try:
            with open(_META_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            swallow.note('search_engine_v2._load_meta:L3233')
            pass
    return {"mtimes": {}, "last_indexed": 0, "doc_count": 0}


def _save_meta(meta):
    """Сохранить метаданные индекса."""
    os.makedirs(INDEX_DIR, exist_ok=True)
    with open(_META_FILE, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def detect_changes(dirs, notes_dir=None, ai_dir=None, books_dir=None, zim_dirs=None, html_dirs=None):
    """
    Определить изменённые файлы с момента последней индексации.

    Returns:
        dict: {
            "new": [file_paths],
            "modified": [file_paths],
            "deleted": [file_paths],
            "unchanged": [file_paths],
            "need_full_rebuild": bool
        }
    """
    meta = _load_meta()
    old_mtimes = meta.get("mtimes", {})
    current_mtimes = _get_file_mtimes(dirs)

    # Добавляем заметки, AI-ответы, книги. Книг бывает два каталога
    # (общедоступный + свой у профиля) — параметр принимает список.
    _book_dirs = ([books_dir] if isinstance(books_dir, str) else list(books_dir or []))
    for extra_dir in [notes_dir, ai_dir] + _book_dirs:
        if extra_dir and os.path.isdir(extra_dir):
            current_mtimes.update(_get_file_mtimes([extra_dir]))

    # ИСПРАВЛЕНИЕ БАГА: добавляем ZIM и HTML файлы в отслеживание
    if zim_dirs:
        for d in zim_dirs:
            if os.path.isdir(d):
                for f in os.listdir(d):
                    if f.lower().endswith('.zim'):
                        fp = os.path.join(d, f)
                        try:
                            current_mtimes[fp] = os.path.getmtime(fp)
                        except Exception:
                            swallow.note('search_engine_v2.detect_changes:L3279')
                            pass

    if html_dirs:
        for d in html_dirs:
            if os.path.isdir(d):
                for root, _, files in os.walk(d):
                    for fn in files:
                        if fn.lower().endswith(('.html', '.htm')):
                            fp = os.path.join(root, fn)
                            try:
                                current_mtimes[fp] = os.path.getmtime(fp)
                            except Exception:
                                swallow.note('search_engine_v2.detect_changes:L3292')
                                pass

    new_files = []
    modified_files = []
    deleted_files = []
    unchanged_files = []

    for fp, mtime in current_mtimes.items():
        if fp not in old_mtimes:
            new_files.append(fp)
        elif old_mtimes[fp] != mtime:
            modified_files.append(fp)
        else:
            unchanged_files.append(fp)

    for fp in old_mtimes:
        if fp not in current_mtimes:
            deleted_files.append(fp)

    # Если изменений слишком много — лучше полная перестройка
    total_changes = len(new_files) + len(modified_files) + len(deleted_files)
    total_files = len(current_mtimes)
    need_full_rebuild = total_changes > total_files * 0.5 if total_files > 0 else False

    return {
        "new": new_files,
        "modified": modified_files,
        "deleted": deleted_files,
        "unchanged": unchanged_files,
        "need_full_rebuild": need_full_rebuild,
        "total_files": total_files,
        "changes": total_changes,
    }


def incremental_rebuild(hs, dirs, notes_dir=None, ai_dir=None, books_dir=None, zim_dirs=None, html_dirs=None):
    """
    Инкрементальная перестройка индекса.
    Если изменений мало — обновляет только изменённые документы.
    Если много — делает полную перестройку.

    Returns:
        dict: {"action": "incremental"|"full", "docs_added": N, "docs_removed": N, "total_docs": N}
    """
    # ИСПРАВЛЕНИЕ БАГА: передаём zim_dirs и html_dirs в detect_changes
    changes = detect_changes(dirs, notes_dir, ai_dir, books_dir, zim_dirs, html_dirs)

    if changes["need_full_rebuild"]:
        log.info(f"Много изменений ({changes['changes']}/{changes['total_files']}), полная перестройка")
        docs = collect_documents(dirs, notes_dir=notes_dir, ai_dir=ai_dir, books_dir=books_dir, zim_dirs=zim_dirs, html_dirs=html_dirs)
        count = hs.rebuild_index(docs)
        meta = _load_meta()
        meta["mtimes"] = _get_file_mtimes(dirs)
        meta["last_indexed"] = time.time()
        meta["doc_count"] = len(docs)
        _save_meta(meta)
        return {"action": "full", "docs_added": count, "docs_removed": 0, "total_docs": count}

    if not changes["new"] and not changes["modified"] and not changes["deleted"]:
        return {"action": "none", "docs_added": 0, "docs_removed": 0, "total_docs": len(hs._doc_store)}

    log.info(f"Инкрементальное обновление: +{len(changes['new'])} ~{len(changes['modified'])} -{len(changes['deleted'])}")

    # Собираем документы только из изменённых файлов
    # Для простоты — пересобираем все документы и обновляем индекс
    # (Whoosh не поддерживает частичное удаление по doc_id без перестройки)
    docs = collect_documents(dirs, notes_dir=notes_dir, ai_dir=ai_dir, books_dir=books_dir, zim_dirs=zim_dirs, html_dirs=html_dirs)
    count = hs.rebuild_index(docs)

    # Обновляем метаданные
    meta = _load_meta()
    meta["mtimes"] = _get_file_mtimes(dirs)
    meta["last_indexed"] = time.time()
    meta["doc_count"] = len(docs)
    _save_meta(meta)

    return {"action": "incremental", "docs_added": count, "docs_removed": len(changes["deleted"]), "total_docs": count}

