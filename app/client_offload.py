#!/usr/bin/env python3
"""Клиентский офлоад: настройки, реестр устройств-помощников и одобрение.

Идея: часть обработки можно делать не на сервере, а в браузере пользователя
(Web Worker, OffscreenCanvas, WebCrypto). Сервер при этом не тратит CPU и
потоки. Всё выключено по умолчанию и включается администратором.

Правила безопасности (осознанно жёсткие):
  * никакого произвольного кода — только белый список чистых задач
    (см. VALID_TASKS) с фиксированной семантикой;
  * устройство-помощник подключается только после явного согласия
    пользователя на этом устройстве (баннер в client_offload.js);
  * согласие и отметка «уведомление уже показывали» хранятся на сервере и
    привязаны к идентификатору устройства, а не к localStorage: очистка
    хранилища, режим инкогнито или другой браузер на том же телефоне не
    заставляют спрашивать заново;
  * decentral.notify.enabled — единственная кнопка «показывать уведомления о
    распределении нагрузки»; выключена значит ни баннера, ни плашки о статусе
    ни на одном устройстве, а само решение принимается по флагу
    helpers.require_consent;
  * на машине, которая крутит сервер, уведомлений нет никогда: окно
    приложения обращается к себе по loopback или по своему же адресу, и
    спрашивать у администратора разрешение на помощь его собственному
    процессору бессмысленно;
  * при helpers.require_approval новое устройство не получает задач,
    пока администратор не одобрит его в панели «Нагрузка»;
  * лимиты: число устройств, задач в сутки на устройство, размер payload;
  * помощники по умолчанию могут брать только свои же задачи
    (allow_foreign=false) — чужие запросы не уходят без явного согласия;
  * в админке есть «стоп-кран»: helpers.enabled=false отключает всё разом.

Реестр устройств и суточные квоты хранятся на диске
(data/client_offload_helpers.json), поэтому одобрения и счётчики переживают
перезапуск сервера. Сами WebSocket-подключения — в памяти процесса.
"""

import os
import json
import logging
import re
import time
import ipaddress
import threading
import urllib.parse
import uuid
import swallow

logger = logging.getLogger("infinity")

def _data_dir():
    env = os.environ.get("INFINITY_DATA_DIR")
    if env:
        return os.path.abspath(env)
    try:
        from config_module import _root
        return os.path.join(_root(), "data")
    except Exception:
        swallow.note('client_offload._data_dir:L57')
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


CONFIG_PATH = os.path.join(_data_dir(), "client_offload.json")
REGISTRY_PATH = os.path.join(_data_dir(), "client_offload_helpers.json")

VALID_TASKS = (
    # изображения (OffscreenCanvas / createImageBitmap)
    "image_resize",      # уменьшить изображение до max-стороны (JPEG)
    "image_thumbnail",   # миниатюра 256px (JPEG)
    "image_webp",        # конвертация в WebP с качеством
    "image_meta",        # размеры и тип картинки без перекодирования
    # хеши (WebCrypto)
    "hash_sha256",
    "hash_sha1",
    "hash_batch",        # пакет строк/буферов одним вызовом
    # текст
    "text_extract",      # вытащить плоский текст из HTML
    "html_to_text",      # HTML → текст с сохранением абзацев
    "word_stats",        # символы/слова/уникальные/строки/предложения
    "text_snippet",      # подсветка найденных фрагментов
    "search_filter",     # фильтрация списка по строке
    # данные
    "csv_to_json",       # CSV (с кавычками) → массив объектов
    "json_pretty",       # форматирование JSON
    "base64_encode",     # UTF-8 → base64
    "base64_decode",     # base64 → UTF-8
    "gzip_compress",     # CompressionStream('gzip')
    "gzip_decompress",
    # Расширение набора: всё это чистые операции, результат которых сервер
    # может проверить независимо (длина хеша, формат и размеры картинки,
    # валидность JSON). Тяжёлые задачи вроде OCR сюда не входят: для них в
    # браузере нет встроенного вычислителя, а тащить в сборку WASM-модуль
    # размером с приложение — отдельное решение, а не строчка в списке.
    #
    # HMAC здесь сознательно нет: считать подпись у помощника можно только
    # передав ему секретный ключ, а смысл HMAC именно в том, что ключ никуда
    # не уходит. То же касается любой задачи с паролем или токеном в payload.
    "hash_sha512",       # WebCrypto SHA-512
    "json_minify",       # разобрать и сжать JSON без пробелов
    "json_validate",     # валидность JSON и его тип
    "image_reencode",    # перекодировать без изменения размера
    "image_rotate",      # поворот на 90/180/270°
    "image_flip",        # отражение по горизонтали/вертикали
    "image_grayscale",   # перевод в оттенки серого
)

DEFAULT_HELPER_TASKS = [
    "hash_sha256", "hash_sha1", "hash_batch", "hash_sha512",
    "image_thumbnail", "image_meta",
    "text_extract", "word_stats", "json_minify", "json_validate",
    "gzip_compress", "gzip_decompress",
]

DEFAULTS = {
    "enabled": False,
    "tasks": {t: True for t in VALID_TASKS},
    "helpers": {
        "enabled": False,
        "require_consent": True,
        # Включено — новое устройство ждёт одобрения администратора,
        # выключено — подключается сразу (как раньше).
        "require_approval": True,
        "allow_foreign": False,
        "max_devices": 2,
        "daily_limit_per_device": 500,
        "max_payload_kb": 8192,
        "tasks": list(DEFAULT_HELPER_TASKS),
    },
    # Децентрализация: каждая функция — отдельный рубильник, выключенный по
    # умолчанию. Выключенный рубильник обязан означать ровно прежний путь:
    # код спрашивает флаг в точке решения, а не только в интерфейсе.
    "decentral": {
        # Сервер раздаёт помощникам НАСТОЯЩИЕ задачи (а не тест из админки).
        # Отдельно от helpers.enabled: помощник может подключаться, но не
        # получать чужие данные, пока это не включено.
        "dispatch": {
            "enabled": False,
            # Чувствительные задачи (файлы пользователя) — чужим устройствам.
            "sensitive_to_foreign": False,
            "result_cache": True,
            "max_inflight": 2,
            "timeout_ms": 8000,
            "retry_peers": 1,
            # Помощник, который подряд не отвечает или присылает мусор,
            # ставится на паузу: иначе каждая следующая задача сначала ждёт
            # его таймаут, и скан фототеки замедляется чужой неполадкой.
            # 0 — не ставить на паузу никогда.
            "pause_after": 3,
            "pause_minutes": 10,
            # Кэш результатов чистых задач: повторный одинаковый payload не
            # требует ни пересылки, ни счёта. Кэш живёт в памяти сервера и
            # ограничен по числу записей и байтам, чтобы большая фототека не
            # съела память.
            "cache_ttl_s": 600,
            "cache_max_entries": 64,
            "cache_max_mb": 8,
            # Остановка раздачи, если она стабильно не экономит время: путь
            # через сеть длиннее локального счёта. Выключение автоматическое,
            # но обратимое: сброс статистики или смена настроек снимает стоп.
            "auto_stop_negative": True,
            "economy_min_samples": 20,
            # Авторежим по нагрузке: заменяет собой ручной рубильник, пока
            # держится давление. Выключен по умолчанию и не включает ни
            # офлоад, ни помощников — только разрешает раздачу на время.
            "auto": {
                "enabled": False,
                "cpu_on": 60,
                "cpu_off": 40,
                "lag_on_ms": 150,
                "lag_off_ms": 60,
                "hold_s": 30,
                "sample_s": 2,
            },
        },
        # Автономное чтение: локальная копия данных на устройстве, работающая
        # при выключенном сервере. Поштучно по модулям — децентрализуем
        # модули, а не приложение скопом.
        "offline": {
            "enabled": False,
            "modules": {"news": False, "notes": False, "books": False,
                        "feed": False},
            "storage_mb": 20,
            # on_open — тянуть снапшот при открытии страницы,
            # interval — тем же таймером по расписанию, пока модуль открыт,
            # manual — только кнопкой на странице модуля.
            "fetch": "on_open",
            "interval_min": 15,
            # Офлайн-черновики заметок: запись на сервер, а не чтение копии,
            # поэтому своим флагом и по умолчанию выключена.
            "drafts": False,
        },
        # Узлы и отказоустойчивость. Самое дорогое, поэтому каждый шаг — своим
        # флагом: остановиться можно на любом. discovery=false =>
        # широковещательного трафика нет вообще. write_mode=off =>
        # мульти-мастер-запись не появляется сама.
        "mesh": {
            "identity": False,
            "discovery": False,
            "failover": False,
            "failover_trust": "verified",
            "failover_node": "",
            "write_mode": "off",
            "sync_modules": [],
            # Частота обращений к соседям. Нижняя граница — минута: проход
            # читает каталог заметок и статьи новостей целиком, и крутить его
            # каждые несколько секунд значит грузить машину, которая и так
            # разворачивает этот модуль ради экономии.
            "sync_interval_s": 300,
        },
        # Инференс децентрализуется хуже всего: большая модель живёт на PC.
        # off — как сейчас; queue — копить запросы до подъёма сервера;
        # local_small — маленькая модель на устройстве (качество ниже).
        # Границы очереди нужны в дефолтах, иначе первое включение режима
        # читало бы их из ниоткуда: inference_queue сам подставляет то же,
        # но конфиг обязан показывать реальные значения панели.
        "inference": {"fallback": "off", "queue_ttl_s": 1800,
                      "queue_poll_s": 20, "queue_max_jobs": 100,
                      "queue_max_bytes": 2097152},
        # Уведомления на устройствах о том, что часть работы берёт на себя
        # помощник. Отдельно от helpers.enabled: рубильник помощников решает,
        # может ли устройство считать; этот рубильник решает, показывать ли
        # ему баннер согласия и плашки статуса. Выключен по умолчанию:
        # человек, включивший офлоад, не обязан каждый раз читать про него.
        "notify": {"enabled": False},
        # Защищённый вход для телефонов. Офлайн-копия читается только там, где
        # браузер разрешает service worker, а разрешает он его лишь в защищённом
        # контексте: https или localhost. В локальной сети по http://IP:порт
        # контекст не защищён, и никакой офлайн на телефоне не появится, сколько
        # бы копий ни было включено. Этот рубильник поднимает отдельный
        # HTTPS-порт перед тем же приложением; по умолчанию выключен, потому что
        # включает прослушивание сети и выпуск локального сертификата.
        "tls": {"enabled": False, "port": 8443},
        # HTTP/2-вход. Отдельный рубильник, а не часть tls.enabled, потому что
        # решают разные вещи: tls — чтобы телефону хватало защищённого контекста
        # для service worker, h2 — чтобы браузер не упирался в потолок шести
        # соединений на origin. Включённый h2 поднимает третий слушатель поверх
        # того же приложения, поэтому по умолчанию выключен.
        "h2": {"enabled": False, "port": 8444},
        # Сетка вычислений. Разумный смысл имеет только один вариант: сосед
        # считает про СВОЕЙ копии и получает от нас запрос, а не данные.
        # Раздача наших данных на чужие процессоры не окупается (см.
        # compute_grid.py — там цифры), поэтому fan-out по умолчанию выключен,
        # а список соседей отдельно проверяется на «это наша сеть».
        "grid": {
            "fanout_search": False,
            "peers": [],
            "peer_timeout_ms": 1500,
            "max_peers": 4,
            "merge_limit": 60,
        },
    },
}

_cache = {"key": None, "cfg": None}
_lock = threading.Lock()


def _file_key():
    try:
        st = os.stat(CONFIG_PATH)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        swallow.note('client_offload._file_key:L261')
        return None


def _merge(base, patch):
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    key = _file_key()
    with _lock:
        if _cache["cfg"] is not None and key == _cache["key"]:
            return _merge(DEFAULTS, _cache["cfg"])
        cfg = {}
        if key:
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    cfg = raw
            except Exception:
                swallow.note('client_offload.load:L288')
                cfg = {}
        cfg = _merge(DEFAULTS, cfg)
        _cache["cfg"], _cache["key"] = cfg, key
        return _merge(DEFAULTS, cfg)


def _int(value, default=0):
    try:
        return int(value)
    except Exception:
        swallow.note('client_offload._int:L299')
        return default


def _bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


OFFLINE_MODULES = ("news", "notes", "books", "feed")
OFFLINE_FETCH_MODES = ("on_open", "interval", "manual")
MESH_WRITE_MODES = ("off", "lww", "crdt")
# Что умеет реплицировать mesh_sync. Список обязан совпадать с тем, что
# модуль реально обходит: пропустить сюда имя задачи офлоада означало бы
# рубильник, который ничего не делает, но выглядит включённым.
MESH_SYNC_MODULES = ("notes", "news")
# Кому можно отдать роль сервера, когда свой сервер не поднялся:
# verified — только узлам с проверенной подписью анонса, any — любому из
# списка соседей, manual — ровно одному, выбранному в панели.
MESH_FAILOVER_TRUST = ("verified", "any", "manual")
INFERENCE_MODES = ("off", "queue", "local_small")
CONSENT_ANSWERS = ("yes", "no")
# Уведомления, которые обязаны появиться ровно один раз на устройство:
# 'banner' — спрос на согласие, 'pending'/'approved'/'rejected' — статус в
# реестре, 'error' — неполадка подключения помощника. Отметки хранятся здесь,
# а не в браузере, поэтому «один раз» значит один раз для устройства, а не для
# каждой вкладки, окна или дня его хранения. Срок переспроса не предусмотрен
# сознательно: снятая отметка — дело администратора, а не будильник, который
# напоминает устройству о чужом решении.
NOTICE_KINDS = ("banner", "pending", "approved", "rejected", "error")


def _in(value, allowed, default):
    return value if value in allowed else default


# Имя сети анонсов: два разных собрания на одной подсети не должны видеть
# друг друга, поэтому группа попадает в пакет и сверяется при приёме.
_DISCOVERY_GROUP_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")


def _clean_group(raw):
    s = str(raw or "").strip()
    return s if _DISCOVERY_GROUP_RE.match(s) else "infinity"


# ── Адреса соседних нод ─────────────────────────────────────────────────────
# Санитайзер живёт здесь, а не в compute_grid, потому что именно save()
# отвечает за то, что попадает в конфиг: значение, пропущенное sanitiser'ом,
# потом читают все, и проверять его на каждой записи было бы ловушкой.

PEER_SCHEMES = ("http", "https")
MAX_PEERS = 8


def clean_peer_url(raw):
    """Нормализовать адрес соседней ноды или вернуть None.

    Соседу уходит текст поискового запроса пользователя, поэтому адрес не
    может быть любым: разрешены loopback и сети организации, и только корень
    сайта. Публичный IP или чужой домен означали бы, что одна опечатка в
    настройках превращает сервер в прокси и уводит запросы наружу. Именам не
    верим: хост, который сегодня в приватной сети, завтра может резолвиться
    на публичный адрес, поэтому только литеральный IP (плюс localhost).
    """
    s = str(raw or "").strip()
    if not s or len(s) > 200:
        return None
    try:
        p = urllib.parse.urlsplit(s if "://" in s else "http://" + s)
        port = p.port
    except ValueError:
        swallow.note('client_offload.clean_peer_url:L374')
        return None
    if p.scheme not in PEER_SCHEMES or p.username or p.password:
        return None
    if p.path not in ("", "/") or p.query or p.fragment:
        return None
    host = (p.hostname or "").lower().rstrip(".")
    if not host:
        return None
    if host != "localhost":
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            swallow.note('client_offload.clean_peer_url:L387')
            return None
        if ip.is_multicast or ip.is_unspecified:
            return None
        if not (ip.is_loopback or ip.is_private or ip.is_link_local):
            return None
    if port is not None and not (1 <= port <= 65535):
        return None
    # Литеральный IPv6 в URL живёт в скобках, а urlsplit срезает их с hostname:
    # без возврата на месте получился бы адрес «http://::1:8281», который ни
    # один клиент не откроет.
    netloc = "[%s]" % host if ":" in host else host
    return "%s://%s" % (p.scheme, netloc if port is None else "%s:%d" % (netloc, port))


def _clean_peers(value):
    """Список адресов соседей: только прошедшие проверку, без дублей."""
    items = value if isinstance(value, (list, tuple)) else []
    out = []
    for raw in items:
        u = clean_peer_url(raw)
        if u and u not in out:
            out.append(u)
        if len(out) >= MAX_PEERS:
            break
    return out


def _blk(x):
    """Дочерний блок конфига: не-словрь превращается в пустой блок.

    Без этого патч вида {"decentral": {"notify": 5}} не отбрасывался, а ронял
    сохранение с AttributeError, то есть один кривой POST превращал настройку
    рубильника в 500.
    """
    return x if isinstance(x, dict) else {}


def _clean_auto(a):
    """Пороги авторежима раздачи.

    Порог включения обязан быть строго выше порога выключения: при
    перепутанных местами значениях «горячо» и «прохладно» наступают
    одновременно, и раздача начала бы дёргаться каждые hold_s. Нижним
    порог придвигается к верхнему, а не наоборот: включение важнее, и
    оно не должно зависеть от опечатки в число.
    """
    a = _blk(a)
    cpu_on = max(1, min(100, _int(a.get("cpu_on"), 60)))
    cpu_off = max(0, min(99, _int(a.get("cpu_off"), 40)))
    if cpu_off >= cpu_on:
        cpu_off = max(0, cpu_on - 1)
    lag_on = max(1, min(10000, _int(a.get("lag_on_ms"), 150)))
    lag_off = max(0, min(9999, _int(a.get("lag_off_ms"), 60)))
    if lag_off >= lag_on:
        lag_off = max(0, lag_on - 1)
    return {
        "enabled": _bool(a.get("enabled")),
        "cpu_on": cpu_on,
        "cpu_off": cpu_off,
        "lag_on_ms": lag_on,
        "lag_off_ms": lag_off,
        "hold_s": max(5, min(3600, _int(a.get("hold_s"), 30))),
        "sample_s": max(1, min(30, _int(a.get("sample_s"), 2))),
    }


def _clean_decentral(d):
    """Санитизация блока децентрализации: только известные ключи, только
    допустимые значения. Неизвестный ключ из патча просто исчезает."""
    d = _blk(d)
    dsp = _blk(d.get("dispatch"))
    off = _blk(d.get("offline"))
    mesh = _blk(d.get("mesh"))
    inf = _blk(d.get("inference"))
    grd = _blk(d.get("grid"))
    ntf = _blk(d.get("notify"))
    tls = _blk(d.get("tls"))
    h2 = _blk(d.get("h2"))
    mods = _blk(off.get("modules"))
    return {
        "dispatch": {
            "enabled": _bool(dsp.get("enabled")),
            "sensitive_to_foreign": _bool(dsp.get("sensitive_to_foreign")),
            "result_cache": _bool(dsp.get("result_cache"), True),
            "max_inflight": max(1, min(8, _int(dsp.get("max_inflight"), 2))),
            "timeout_ms": max(500, min(120000, _int(dsp.get("timeout_ms"), 8000))),
            "retry_peers": max(0, min(4, _int(dsp.get("retry_peers"), 1))),
            "pause_after": max(0, min(20, _int(dsp.get("pause_after"), 3))),
            "pause_minutes": max(1, min(240, _int(dsp.get("pause_minutes"), 10))),
            "cache_ttl_s": max(10, min(86400, _int(dsp.get("cache_ttl_s"), 600))),
            "cache_max_entries": max(0, min(512, _int(dsp.get("cache_max_entries"), 64))),
            "cache_max_mb": max(0, min(256, _int(dsp.get("cache_max_mb"), 8))),
            "auto_stop_negative": _bool(dsp.get("auto_stop_negative"), True),
            "economy_min_samples": max(1, min(10000, _int(dsp.get("economy_min_samples"), 20))),
            "auto": _clean_auto(dsp.get("auto")),
        },
        "offline": {
            "enabled": _bool(off.get("enabled")),
            "modules": {m: _bool(mods.get(m)) for m in OFFLINE_MODULES},
            "storage_mb": max(1, min(2048, _int(off.get("storage_mb"), 20))),
            "fetch": _in(str(off.get("fetch") or ""), OFFLINE_FETCH_MODES, "on_open"),
            # Ниже минуты интервал превратился бы в опрос сервера в фоне,
            # выше суток — в настройку, которую никто не дождётся.
            "interval_min": max(1, min(1440, _int(off.get("interval_min"), 15))),
            # Без этой строки payload из админки потерял бы флаг при сохранении:
            # санитайзер возвращает только перечисленные поля.
            "drafts": _bool(off.get("drafts")),
        },
        "mesh": {
            "identity": _bool(mesh.get("identity")),
            # Второй уровень: включённый identity заводит ключ и просит
            # подтверждение, но никого не отсеивает. Отсеивает только
            # required — и только когда включён сам identity.
            "identity_required": _bool(mesh.get("identity_required")),
            "discovery": _bool(mesh.get("discovery")),
            # Под настройки анонсов: без них патч из админки терял бы порт и
            # группу, а mesh_discovery каждый раз стартовал бы с дефолтов.
            "discovery_port": max(1024, min(65533, _int(mesh.get("discovery_port"), 8445))),
            "discovery_group": _clean_group(mesh.get("discovery_group")),
            "discovery_interval_s": max(5, min(3600, _int(mesh.get("discovery_interval_s"), 15))),
            "discovery_ttl_s": max(15, min(3600, _int(mesh.get("discovery_ttl_s"), 90))),
            "failover": _bool(mesh.get("failover")),
            # Доверие при перехвате: санитайзер обязан хранить выбор, иначе
            # каждое сохранение панели сбрасывало бы его в «verified».
            "failover_trust": _in(str(mesh.get("failover_trust") or ""),
                                  MESH_FAILOVER_TRUST, "verified"),
            # Ручной выбор — это id узла или его адрес; оболочка сравнивает
            # с ними кандидатов из анонсов и списка соседей.
            "failover_node": str(mesh.get("failover_node") or "").strip()[:120],
            "write_mode": _in(str(mesh.get("write_mode") or ""), MESH_WRITE_MODES, "off"),
            "sync_modules": sorted({str(m)[:40] for m in (mesh.get("sync_modules") or [])
                                    if str(m) in MESH_SYNC_MODULES}),
            # Границы те же, что применяет mesh_sync: панель и живой модуль
            # не должны расходиться, иначе показанное интервалом было бы одно,
            # а поток крутился бы с другим.
            "sync_interval_s": max(60, min(86400, _int(mesh.get("sync_interval_s"), 300))),
        },
        "inference": {"fallback": _in(str(inf.get("fallback") or ""),
                                     INFERENCE_MODES, "off"),
                      # Те же границы, что применяет inference_queue: конфиг и
                      # живой модуль не должны расходиться, иначе панель
                      # показывает одно, а поток стартует с другим.
                      "queue_ttl_s": max(60, min(86400, _int(inf.get("queue_ttl_s"), 1800))),
                      # Пол не меньше 15 с: проверка движка — это TCP-контакт с
                      # таймаутом, и крутить его каждую секунду на машине,
                      # которая и так задыхается, значит мешать самому себе.
                      "queue_poll_s": max(15, min(3600, _int(inf.get("queue_poll_s"), 20))),
                      "queue_max_jobs": max(1, min(500, _int(inf.get("queue_max_jobs"), 100))),
                      "queue_max_bytes": max(65536, min(32 * 1024 * 1024,
                                                        _int(inf.get("queue_max_bytes"),
                                                             2 * 1024 * 1024)))},
        "notify": {"enabled": _bool(ntf.get("enabled"))},
        "tls": {"enabled": _bool(tls.get("enabled")),
                "port": max(1024, min(65533, _int(tls.get("port"), 8443)))},
        "h2": {"enabled": _bool(h2.get("enabled")),
               "port": max(1024, min(65533, _int(h2.get("port"), 8444)))},
        "grid": {
            "fanout_search": _bool(grd.get("fanout_search")),
            "peers": _clean_peers(grd.get("peers")),
            "peer_timeout_ms": max(500, min(8000, _int(grd.get("peer_timeout_ms"), 1500))),
            "max_peers": max(1, min(MAX_PEERS, _int(grd.get("max_peers"), 4))),
            "merge_limit": max(5, min(300, _int(grd.get("merge_limit"), 60))),
        },
    }


def save(patch):
    """Слить патч с текущим конфигом, провалидировать, записать атомарно."""
    cur = load()
    merged = _merge(cur, patch if isinstance(patch, dict) else {})

    merged["enabled"] = bool(merged.get("enabled"))
    tasks = merged.get("tasks") or {}
    merged["tasks"] = {t: bool(tasks.get(t, True)) for t in VALID_TASKS}

    h = merged.get("helpers") or {}
    out_h = {
        "enabled": _bool(h.get("enabled")),
        "require_consent": _bool(h.get("require_consent"), True),
        "require_approval": _bool(h.get("require_approval"), True),
        "allow_foreign": _bool(h.get("allow_foreign")),
        "max_devices": max(0, min(16, _int(h.get("max_devices"), 2))),
        "daily_limit_per_device": max(0, min(100000, _int(h.get("daily_limit_per_device"), 500))),
        "max_payload_kb": max(64, min(65536, _int(h.get("max_payload_kb"), 8192))),
        "tasks": [t for t in (h.get("tasks") or []) if t in VALID_TASKS],
    }
    merged["helpers"] = out_h
    merged["decentral"] = _clean_decentral(merged.get("decentral"))

    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_PATH)
    with _lock:
        _cache["cfg"], _cache["key"] = None, None
    return merged


def enabled():
    try:
        return bool(load().get("enabled"))
    except Exception:
        swallow.note('client_offload.enabled:L593')
        return False


def _helpers_cfg(cfg=None):
    return (cfg or load()).get("helpers") or {}


# ── Рубильники децентрализации ─────────────────────────────────────────────
# Флаг спрашивается там, где принимается решение, а не только в интерфейсе:
# галочка, которую код не проверяет, — декорация.

def decentral():
    return load().get("decentral") or {}


def dflag(path, default=False):
    """Значение decentral по адресу вида 'dispatch.enabled'."""
    node = decentral()
    for part in str(path).split("."):
        if not isinstance(node, dict):
            return default
        node = node.get(part)
        if node is None:
            return default
    return node


def dispatch_manual():
    """Ручной рубильник: раздавать задачи всегда, независимо от нагрузки.

    Требует трёх включённых вещей сразу: общего офлоада, помощников и
    отдельного рубильника dispatch — потому что это уже передача чужих
    файлов на другое устройство.
    """
    cfg = load()
    h = _helpers_cfg(cfg)
    return (bool(cfg.get("enabled")) and bool(h.get("enabled"))
            and _bool(dflag("dispatch.enabled")))


def dispatch_enabled():
    """Разрешена ли раздача сейчас: вручную или авторежимом по нагрузке.

    Авторежим не расширяет права: он лишь заменяет собой ручной рубильник,
    пока сервер под давлением. Все проверки устройств, согласий, квот и
    чувствительных задач остаются теми же и выполняются в pick_devices.
    """
    if dispatch_manual():
        return True
    try:
        import load_watch
        return bool(load_watch.auto_active())
    except Exception:
        swallow.note('client_offload.dispatch_enabled:L647')
        return False


def decentral_off():
    """Явно заполненный блок «всё выключено».

    Патч обязан быть полным, потому что save() сливает конфиг глубоко:
    modules={} оставил бы прежние включённые модулы, и после отзыва
    offline.enabled=true молча вернул бы старый список.
    """
    off = _clean_decentral({})
    return off


def offline_enabled(module):
    """Разрешено ли держать локальную копию данных модуля на устройстве."""
    if not _bool(dflag("offline.enabled")):
        return False
    mods = (decentral().get("offline") or {}).get("modules") or {}
    return _bool(mods.get(module))


def public_config(device_id=None, is_local=False):
    """То, что можно отдать любому клиенту (без админских лимитов).

    device_id нужен затем, чтобы клиент узнал о СВОЁМ решении и о СВОИХ
    показанных уведомлениях из ответа сервера: без этого память браузера —
    localStorage, а его чистят, и в инкогнито его нет.
    """
    cfg = load()
    h = _helpers_cfg(cfg)
    off = (cfg.get("decentral") or {}).get("offline") or {}
    out = {
        "enabled": bool(cfg.get("enabled")),
        "tasks": {t: bool(v) for t, v in (cfg.get("tasks") or {}).items() if v},
        "helpers_enabled": bool(h.get("enabled")),
        "helper_tasks": [t for t in (h.get("tasks") or []) if t in VALID_TASKS],
        "require_consent": bool(h.get("require_consent", True)),
        "require_approval": bool(h.get("require_approval", True)),
        "max_payload_kb": int(h.get("max_payload_kb", 8192)),
        "notify_enabled": _bool(dflag("notify.enabled")),
        # Это окно открыто на той же машине, что и сервер: помощник из него
        # не нужен, и уведомлений из него тоже не нужно.
        "is_local": bool(is_local),
        # Клиент обязан спрашивать эти флаги при каждой загрузке страницы:
        # выключенный рубильник значит, что локальные копии не пишутся и
        # старые копии на устройстве больше не используются.
        "decentral": {
            "offline": {
                "enabled": bool(off.get("enabled")),
                "modules": {m: bool((off.get("modules") or {}).get(m))
                            for m in OFFLINE_MODULES},
                "storage_mb": int(off.get("storage_mb", 20)),
                "fetch": str(off.get("fetch") or "on_open"),
                "interval_min": int(off.get("interval_min") or 15),
                "drafts": bool(off.get("drafts")),
            },
        },
    }
    out.update(client_state(device_id))
    return out


# ── Реестр устройств-помощников (на диске) ─────────────────────────────────

_registry = {"version": 1, "devices": {}}
_registry_loaded = False
_sockets = {}              # device_id -> WebSocket, только в памяти
_helpers_lock = threading.RLock()
_last_persist = 0.0
PERSIST_MIN_INTERVAL = 10.0


def _today():
    return time.strftime("%Y-%m-%d")


def _load_registry():
    """Вызывать под _helpers_lock."""
    global _registry, _registry_loaded
    if _registry_loaded:
        return _registry
    devices = {}
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict) and isinstance(raw.get("devices"), dict):
            devices = {str(k)[:80]: v for k, v in raw["devices"].items()
                       if isinstance(v, dict)}
    except Exception:
        swallow.note('client_offload._load_registry:L738')
        devices = {}
    for rec in devices.values():
        if rec.get("state") not in ("pending", "approved", "rejected"):
            rec["state"] = "approved"
        rec.setdefault("jobs", 0)
        rec.setdefault("errors", 0)
        rec.setdefault("day", _today())
        rec.setdefault("day_jobs", 0)
        rec.setdefault("fail_streak", 0)
        # Сглаженная задержка полного пути до этого помощника: по ней сервер
        # выбирает устройство и подрезает таймаут, вместо одного общего 8 с.
        rec.setdefault("lat_ewma", 0.0)
    _registry = {"version": 1, "devices": devices}
    _registry_loaded = True
    return _registry


def _save_registry(force=False):
    """Вызывать под _helpers_lock. Тормозим запись, чтобы не дёргать диск."""
    global _last_persist
    now = time.time()
    if not force and (now - _last_persist) < PERSIST_MIN_INTERVAL:
        return
    _last_persist = now
    try:
        os.makedirs(os.path.dirname(REGISTRY_PATH), exist_ok=True)
        tmp = REGISTRY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_registry, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, REGISTRY_PATH)
    except Exception:
        swallow.note('client_offload._save_registry:L772')
        pass


def _approved_ids(devices):
    return [did for did, rec in devices.items() if rec.get("state") == "approved"]


def _pending_count(devices):
    return sum(1 for rec in devices.values() if rec.get("state") == "pending")


def _clean_device_id(raw):
    """Идентификатор устройства в каноническом виде либо None.

    Никакого «придумать uuid, если пусто»: анонимная запись, по которой нельзя
    ничего найти, смысл имеет только в WS-подключении, а отметки согласия и
    показанных уведомлений обязаны относиться к конкретному устройству.
    """
    did = re.sub(r"[^A-Za-z0-9_.:-]", "", str(raw or "").strip())[:80]
    return did or None


_own_addrs = {"set": None, "at": 0.0}
_OWN_ADDRS_TTL = 60.0


def _own_address_set():
    """Адреса самого сервера (кэшируются на минуту: список интерфейсов не
    меняется от запроса к запросу, а psutil недёшев на каждом хите)."""
    now = time.time()
    if _own_addrs["set"] is not None and (now - _own_addrs["at"]) < _OWN_ADDRS_TTL:
        return _own_addrs["set"]
    addrs = {"127.0.0.1", "::1"}
    try:
        import psutil
        import socket as _sock
        families = (getattr(_sock, "AF_INET", 2), getattr(_sock, "AF_INET6", 23))
        for entries in (psutil.net_if_addrs() or {}).values():
            for e in entries or []:
                if getattr(e, "family", None) not in families:
                    continue      # AF_LINK — это MAC интерфейса, не адрес
                a = str(getattr(e, "address", "") or "").split("%")[0].strip()
                if a:
                    addrs.add(a)
    except Exception:
        swallow.note('client_offload._own_address_set:L818')
        try:
            import socket
            addrs.update(socket.gethostbyname_ex(socket.gethostname())[2])
        except Exception:
            swallow.note('client_offload._own_address_set:L823')
            pass
    _own_addrs["set"], _own_addrs["at"] = addrs, now
    return addrs


def is_own_address(ip):
    """Запрос пришёл с машины, которая и есть сервер.

    Окно приложения открывается по 127.0.0.1 или по собственному адресу
    локального интерфейса — в обоих случаях это та же коробка. Телефон по
    своему адресу сюда не попадает, поэтому «не спрашивать и не уведомлять»
    работает только там, где и так ничего не кому отдавать.
    """
    s = str(ip or "").strip()
    if not s:
        return False
    if s in _own_address_set():
        return True
    try:
        return ipaddress.ip_address(s).is_loopback
    except Exception:
        swallow.note('client_offload.is_own_address:L845')
        return False


def register_helper(ws, device_id, info=None):
    """Зарегистрировать WS-подключение.

    Возвращает (device_id, error, state). При require_approval новое
    устройство получает state="pending": подключение живёт, но задач не
    получает, пока админ не одобрит его. None в error — успех.

    Согласие больше не на слово: «consent=1» в URL клиент мог прислать любой,
    поэтому сервер смотрит на отметку, оставленную ответом на баннер (или
    снятым требованием спроса), и привязана она к идентификатору устройства.
    """
    cfg = load()
    h = _helpers_cfg(cfg)
    if not cfg.get("enabled") or not h.get("enabled"):
        return None, "офлоад выключен", None
    did = _clean_device_id(device_id) or uuid.uuid4().hex[:16]
    info = info or {}
    ip = str(info.get("ip") or "")
    tasks = [t for t in (info.get("tasks") or [])
             if t in VALID_TASKS and t in (h.get("tasks") or [])]
    limit = int(h.get("max_devices") or 0)
    need_approval = _bool(h.get("require_approval", True), True)
    need_consent = _bool(h.get("require_consent", True), True)
    if limit <= 0:
        return None, "лимит устройств равен 0 (помощники запрещены)", None
    if is_own_address(ip):
        return None, "это сам сервер: отдавать задачи самому себе бессмысленно", "local"

    with _helpers_lock:
        reg = _load_registry()
        devices = reg["devices"]
        rec = devices.get(did)
        if rec and rec.get("state") == "rejected":
            return None, "устройство отклонено администратором", "rejected"

        consent = (rec or {}).get("consent")
        if need_consent and consent != "yes":
            return None, "устройство не подтвердило согласие", "no_consent"

        approved = _approved_ids(devices)
        if rec and rec.get("state") == "approved":
            state = "approved"
        elif need_approval:
            state = "pending"
        else:
            state = "approved"

        if state == "approved" and limit and did not in approved and len(approved) >= limit:
            return None, "достигнут лимит устройств", None
        if state == "pending" and limit and _pending_count(devices) >= (limit + 8):
            return None, "слишком много устройств ожидают одобрения", None

        now = time.time()
        if not rec:
            rec = {
                "id": did, "first_seen": now, "jobs": 0, "errors": 0,
                "day": _today(), "day_jobs": 0, "approved_at": None,
            }
            devices[did] = rec
        rec.update({
            "name": str(info.get("name") or rec.get("name") or "")[:120],
            "ua": str(info.get("ua") or rec.get("ua") or "")[:300],
            "ip": ip[:64] or str(rec.get("ip") or ""),
            "tasks": tasks,
            "last_seen": now,
            "state": state,
        })
        # Спрос не требуется — согласие администратор выдал заранее, и на
        # устройстве его тоже ни о чём спрашивать не надо.
        if not need_consent:
            rec["consent"] = "yes"
            rec["consent_at"] = rec.get("consent_at") or now
        if state == "approved" and not rec.get("approved_at"):
            rec["approved_at"] = now
        if rec.get("day") != _today():
            rec["day"], rec["day_jobs"] = _today(), 0
        _sockets[did] = ws
        _save_registry(force=True)
    return did, None, state


def unregister_helper(device_id):
    with _helpers_lock:
        _sockets.pop(device_id, None)
        rec = _load_registry()["devices"].get(device_id)
        if rec:
            rec["last_seen"] = time.time()
            _save_registry()


def touch_helper(device_id):
    with _helpers_lock:
        rec = _load_registry()["devices"].get(device_id)
        if rec:
            rec["last_seen"] = time.time()


def note_identity(device_id, state):
    """Запомнить результат проверки mesh.identity для устройства.

    Отметка живёт в реестре рядом с одобрением и квотами, а не в отдельном
    файле: панель смотрит на один список устройств, и «проверено / не
    проверено» обязано быть видно там же, где «одобрено».
    """
    did = str(device_id or "")[:80]
    value = str(state or "unsigned")[:24]
    with _helpers_lock:
        rec = _load_registry()["devices"].get(did)
        if not rec:
            return False
        rec["identity"] = value
        rec["identity_at"] = time.time()
        _save_registry(force=True)
        return True


def note_latency(device_id, ms):
    """Сгладить задержку полного пути помощника (EWMA).

    Нужна для двух решений: кого выбрать, когда помощников несколько, и
    сколько ждать ответа именно от этого устройства. Первый замер берётся
    как есть: у нового помощника ещё нет истории.
    """
    try:
        value = max(0.0, float(ms))
    except Exception:
        swallow.note('client_offload.note_latency:L975')
        return
    if not value:
        return
    with _helpers_lock:
        rec = _load_registry()["devices"].get(device_id)
        if not rec:
            return
        prev = float(rec.get("lat_ewma") or 0.0)
        rec["lat_ewma"] = round(value if not prev else prev * 0.7 + value * 0.3, 1)
        _save_registry()


def consume_quota(device_id):
    """Списать одну задачу; False — лимит на сутки исчерпан или не одобрено."""
    cfg = load()
    limit = int(_helpers_cfg(cfg).get("daily_limit_per_device") or 0)
    with _helpers_lock:
        rec = _load_registry()["devices"].get(device_id)
        if not rec or rec.get("state") != "approved":
            return False
        today = _today()
        if rec.get("day") != today:
            rec["day"], rec["day_jobs"] = today, 0
        if limit and int(rec.get("day_jobs") or 0) >= limit:
            return False
        rec["day_jobs"] = int(rec.get("day_jobs") or 0) + 1
        rec["jobs"] = int(rec.get("jobs") or 0) + 1
        rec["last_seen"] = time.time()
        _save_registry()
        return True


def helper_event(device_id, ok=True, why=None):
    """Отметить исход задачи у помощника.

    Успех сбрасывает серию отказов; серия из pause_after отказов ставит
    помощника на паузу на pause_minutes. Смысл не в наказании устройства, а в
    очереди: зависший телефон иначе съедает таймаут на каждой задаче, а скан
    фототеки идёт последовательно и ждёт его ответа.

    Возвращает время конца паузы (0 — паузы нет).
    """
    with _helpers_lock:
        rec = _load_registry()["devices"].get(device_id)
        if not rec:
            return 0
        now = time.time()
        if ok:
            had_pause = bool(rec.get("paused_until"))
            rec["fail_streak"] = 0
            rec.pop("last_error", None)
            # Устройство ответило — канал жив, держать паузу больше незачем.
            rec.pop("paused_until", None)
            rec.pop("paused_at", None)
            _save_registry(force=had_pause)
            return 0
        rec["errors"] = int(rec.get("errors") or 0) + 1
        rec["fail_streak"] = int(rec.get("fail_streak") or 0) + 1
        rec["last_error"] = str(why or "")[:200]
        d = decentral().get("dispatch") or {}
        limit = max(0, _int(d.get("pause_after"), 3))
        minutes = max(1, _int(d.get("pause_minutes"), 10))
        if limit and rec["fail_streak"] >= limit and not float(rec.get("paused_until") or 0) > now:
            rec["paused_until"] = now + minutes * 60
            rec["paused_at"] = now
            logger.warning("[offload] помощник %s на паузе %d мин: %d отказов подряд (%s)",
                           device_id, minutes, rec["fail_streak"], rec["last_error"])
        _save_registry(force=True)
        return float(rec.get("paused_until") or 0)


def resume_helper(device_id):
    """Снять паузу и обнулить серию отказов (кнопка в панели)."""
    with _helpers_lock:
        rec = _load_registry()["devices"].get(device_id)
        if rec is None:
            return None
        rec["fail_streak"] = 0
        rec.pop("paused_until", None)
        rec.pop("paused_at", None)
        rec.pop("last_error", None)
        _save_registry(force=True)
        return rec


def _public_record(did, rec, now):
    limit = int(_helpers_cfg().get("daily_limit_per_device") or 0)
    paused_until = float(rec.get("paused_until") or 0)
    return {
        "id": did,
        "name": rec.get("name") or "",
        "ua": rec.get("ua") or "",
        "ip": rec.get("ip") or "",
        "tasks": list(rec.get("tasks") or []),
        "state": rec.get("state") or "approved",
        "approved": rec.get("state") == "approved",
        "pending": rec.get("state") == "pending",
        "online": did in _sockets and (now - float(rec.get("last_seen") or 0)) < 90,
        "first_seen": rec.get("first_seen") or 0,
        "last_seen": rec.get("last_seen") or 0,
        "jobs": int(rec.get("jobs") or 0),
        "errors": int(rec.get("errors") or 0),
        "day_jobs": int(rec.get("day_jobs") or 0),
        "daily_limit": limit,
        "approved_at": rec.get("approved_at"),
        "consent": (rec.get("consent")
                    if rec.get("consent") in CONSENT_ANSWERS else None),
        "consent_at": rec.get("consent_at"),
        "notices_shown": sorted(k for k, v in (rec.get("notices") or {}).items() if v),
        # Пауза после серии отказов: панель обязана объяснять, почему живой
        # помощник перестал получать задачи, и давать снять паузу вручную.
        "paused": paused_until > now,
        "paused_until": paused_until if paused_until > now else 0,
        "fail_streak": int(rec.get("fail_streak") or 0),
        "last_error": rec.get("last_error") or "",
        "lat_ewma": round(float(rec.get("lat_ewma") or 0.0), 1),
        # Отметка mesh.identity: «verified» устройство подтвердило подпись,
        # «unsigned» — не подтверждало вовсе. Сам секрет здесь не появляется.
        "identity": str(rec.get("identity") or "unsigned")[:24],
    }


def list_helpers(include_rejected=True):
    now = time.time()
    out = []
    with _helpers_lock:
        for did, rec in _load_registry()["devices"].items():
            if not include_rejected and rec.get("state") == "rejected":
                continue
            out.append(_public_record(did, rec, now))
    out.sort(key=lambda x: (x["state"] != "pending", -x["first_seen"]))
    return out


def pending_helpers():
    return [h for h in list_helpers() if h["pending"]]


def get_helper_ws(device_id):
    with _helpers_lock:
        return _sockets.get(device_id)


def helper_state(device_id):
    with _helpers_lock:
        rec = _load_registry()["devices"].get(device_id)
        return rec.get("state") if rec else None


# ── Согласие и отметки «уведомление уже показывали» ────────────────────────
# Оба живут в реестре на сервере и привязаны к идентификатору устройства.
# Хранилище браузера для этого не годится: его чистят, в инкогнито его нет, а
# в двух разных браузерах на одном телефоне это две разные записи — и устройство
# каждый раз становится «новым», которому надо всё рассказать заново.


def _seen_notice(notices, kind):
    """Показывали ли это уведомление устройству."""
    return bool((notices or {}).get(kind))


def _ensure_record(did):
    """Запись устройства под _helpers_lock; создаёт, если её ещё нет.

    Нужно потому, что согласие приходит ДО первого WS-подключения: без этого
    ответа отвечать было бы некуда. Мест в реестре ограничено тем же лимитом
    устройств, что и подключения, плюс запас на «ответили, но не пришли».
    """
    devices = _load_registry()["devices"]
    rec = devices.get(did)
    if rec:
        return rec
    limit = int(_helpers_cfg().get("max_devices") or 0)
    if limit and len(devices) >= limit + 8:
        return None
    rec = {
        "id": did, "first_seen": time.time(), "jobs": 0, "errors": 0,
        "day": _today(), "day_jobs": 0, "approved_at": None,
        # Пока устройство не подключалось — состояние то же, которое
        # выставил бы register_helper: не одобрять его в обход одобрения.
        "state": "pending" if _bool(_helpers_cfg().get("require_approval", True), True)
        else "approved",
    }
    devices[did] = rec
    return rec


def consent_of(device_id):
    """'yes' | 'no' | None — ответ устройства на вопрос о помощи."""
    did = _clean_device_id(device_id)
    if not did:
        return None
    with _helpers_lock:
        rec = _load_registry()["devices"].get(did)
        c = (rec or {}).get("consent")
    return c if c in CONSENT_ANSWERS else None


def set_consent(device_id, answer, name="", ua="", ip=""):
    """Запомнить ответ по идентификатору устройства.

    None — не сохранено (пустой идентификатор, неизвестный ответ, реестр
    переполнен вымышленными устройствами).
    """
    did = _clean_device_id(device_id)
    ans = str(answer or "").strip().lower()
    if not did or ans not in CONSENT_ANSWERS:
        return None
    with _helpers_lock:
        rec = _ensure_record(did)
        if not rec:
            return None
        rec["consent"] = ans
        rec["consent_at"] = time.time()
        if name:
            rec["name"] = str(name)[:120]
        if ua:
            rec["ua"] = str(ua)[:300]
        if ip:
            rec["ip"] = str(ip)[:64]
            rec["local"] = is_own_address(ip)
        _save_registry(force=True)
        return _public_record(did, rec, time.time())


def mark_notice(device_id, kind):
    """Отметить уведомление как показанное: каждое — ровно один раз."""
    did = _clean_device_id(device_id)
    k = str(kind or "")
    if not did or k not in NOTICE_KINDS:
        return False
    with _helpers_lock:
        rec = _ensure_record(did)
        if not rec:
            return False
        notices = dict(rec.get("notices") or {})
        notices[k] = True
        rec["notices"] = notices
        _save_registry(force=True)
        return True


def reset_notices(device_id, kind=None):
    """Снять отметки показанных уведомлений (устройство или одно из них)."""
    did = _clean_device_id(device_id)
    if not did:
        return False
    with _helpers_lock:
        rec = _load_registry()["devices"].get(did)
        if not rec:
            return False
        notices = rec.get("notices") or {}
        if kind and str(kind) in NOTICE_KINDS:
            notices.pop(str(kind), None)
        else:
            notices = {}
        rec["notices"] = notices
        _save_registry(force=True)
        return True


def client_state(device_id):
    """Всё, что клиент обязан узнать перед тем, как что-то показать.

    Политика целиком здесь, а не в JS: решение «спрашивать или нет» и «этому
    устройству уже рассказывали» живёт на сервере, поэтому переключение
    браузеров, чистка хранилища и перезапуск приложения ничего не сбрасывают.
    """
    did = _clean_device_id(device_id)
    with _helpers_lock:
        rec = _load_registry()["devices"].get(did) if did else None
        consent = (rec or {}).get("consent")
        notices = dict((rec or {}).get("notices") or {})
    seen = {k: _seen_notice(notices, k) for k in NOTICE_KINDS}
    ask = (_bool(dflag("notify.enabled")) and consent not in CONSENT_ANSWERS
           and not seen["banner"] and not is_own_address((rec or {}).get("ip")))
    return {
        "device": did or "",
        "state": (rec or {}).get("state") if rec else None,
        "consent": consent if consent in CONSENT_ANSWERS else None,
        "notice": seen,
        "ask_banner": bool(ask),
    }


def approve_helper(device_id):
    with _helpers_lock:
        rec = _load_registry()["devices"].get(device_id)
        if not rec:
            return None
        rec["state"] = "approved"
        rec["approved_at"] = rec.get("approved_at") or time.time()
        _save_registry(force=True)
        return _public_record(device_id, rec, time.time())


def reject_helper(device_id):
    with _helpers_lock:
        rec = _load_registry()["devices"].get(device_id)
        if not rec:
            return None
        rec["state"] = "rejected"
        rec["approved_at"] = None
        _save_registry(force=True)
        return _public_record(device_id, rec, time.time())


def remove_helper(device_id):
    with _helpers_lock:
        devices = _load_registry()["devices"]
        rec = devices.pop(device_id, None)
        if rec is not None:
            _save_registry(force=True)
        return rec is not None


def can_dispatch(device_id):
    cfg = load()
    h = _helpers_cfg(cfg)
    if not cfg.get("enabled") or not h.get("enabled"):
        return False
    with _helpers_lock:
        rec = _load_registry()["devices"].get(device_id)
        if not rec or rec.get("state") != "approved":
            return False
        return device_id in _sockets
