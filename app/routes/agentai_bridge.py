"""Мост к Infinity Agent (Hermes) для мест приложения.

Позволяет отправлять агенту сообщение, не открывая страницу /agentai:
палитра быстрого ввода (Shift+Space в /desktop и в отдельном окне
обёртки) и разбор ошибок в админ-панели.

Агент — это отдельный процесс с собственным API (Hermes gateway на 8642).
Мост не проксирует его HTTP как есть, а держит сессии за клиента: ключ
сессии (`session_key`) хранится у вызывающей страницы в localStorage,
поэтому переписка продолжается после перезагрузки, а разные места
(палитра рабочего стола, диагностика ошибок в админке) не смешивают
историю друг с другом.

Ответ отдаётся SSE в том же формате, что и у агента:
`assistant.delta`, `assistant.reasoning`, `tool.started|completed|failed`,
`assistant.completed`, `run.completed|failed`, `error`, `done`.
"""

import asyncio
import atexit
import json
import logging
import os
import re
import threading
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse, StreamingResponse
import swallow

logger = logging.getLogger("infinity")

router = APIRouter()

# Агент думает долго: таймаут на чтение потока не ставим вовсе,
# иначе соединение рвётся на длинных рассуждениях.
_TIMEOUT = httpx.Timeout(connect=15.0, read=None, write=60.0, pool=15.0)

# session_key -> {"sid": id сессии агента, "title": заголовок, "created": ts}
_SESSIONS: dict = {}
_SESSIONS_LOCK = threading.Lock()

# Ограничение на размер присылаемого текста: палитра не должна уметь
# отправить агенту мегабайт лога.
_MAX_MESSAGE = 64000

# Скриншоты (палитра умеет приложить экран). data: URL проходит в агент как
# image_url — см. _normalize_multimodal_content в gateway/platforms/api_server.py.
_MAX_ATTACH = 4
_MAX_ATTACH_BYTES = 4 * 1024 * 1024


# --------------------------------------------------------------------------
# Модуль агента
# --------------------------------------------------------------------------
def _hermes():
    """Модуль интеграции агента или None, если он недоступен."""
    try:
        from hermes_module.integration import launch  # type: ignore
        return launch
    except Exception as e:
        logger.warning(f"[agent-bridge] модуль агента недоступен: {e}")
        return None


def _gw_base(launch, profile: str = "") -> str:
    """Адрес шлюза агента.

    У шлюза есть два вида адресов: обычный (профиль по умолчанию) и с
    префиксом /p/<профиль>/ — так он обслуживает именованные профили
    (личности агента). У каждого профиля своя база сессий и свой ключ, так
    что переписка разных личностей не смешивается.
    """
    from hermes_module.integration import config  # type: ignore
    base = f"http://{config.GW_HOST}:{config.GW_PORT}"
    if profile and profile != "default":
        base += f"/p/{profile}"
    return base


def _profile_key(launch, profile: str = "") -> str:
    """Ключ API_SERVER_KEY нужного профиля.

    У именованного профиля он свой (в его .env): шлюз проверяет ключ того
    профиля, к которому относится адрес, и ключ профиля по умолчанию на
    чужой профиль не подходит.
    """
    if profile and profile != "default":
        try:
            from hermes_module.integration import personas  # type: ignore
            key = personas.persona_key(profile)
            if key:
                return key
        except Exception as e:
            logger.warning(f"[agent-bridge] ключ профиля {profile} не прочитан: {e}")
    return launch.gateway_key()


_agent_ready_cache = {"at": 0.0}
_agent_probe_lock = asyncio.Lock()
_AGENT_OK_TTL = 30.0


def _agent_ready_fresh() -> bool:
    return bool(_agent_ready_cache["at"]) and (
        time.time() - _agent_ready_cache["at"]) < _AGENT_OK_TTL


async def _wait_agent(timeout: float = 60.0) -> bool:
    """Дождаться, пока агент поднимется, и вернуть, готов ли он.

    Палитра (Shift+Space) открывается сразу после запуска приложения, а
    агент Hermes поднимается десятки секунд. Без этого ожидания запрос
    истории уходил в шлюз, который ещё не слушал, и переписка выглядела
    пустой: пользователь видел только строку ввода, пока не отправлял
    сообщение — тогда агент уже стоял, и всё работало.

    Успех кэшируется на 30 с, и опрос в один момент времени делает один
    запрос: палитра, /api/agentai/sessions и история тянут готовность подряд
    (за сессию в журнале: 84 и 79 запросов, 1 332 и 1 250 с удержания).
    Отрицательный результат НЕ кэшируется: во время старта агента запрос
    обязан ждать столько же, сколько ждал раньше, иначе переписка снова
    станет пустой — тот самый регресс из абзаца выше.
    """
    if _agent_ready_fresh():
        return True
    async with _agent_probe_lock:
        if _agent_ready_fresh():
            return True
        ready = await _probe_agent(timeout)
        if ready:
            _agent_ready_cache["at"] = time.time()
        return ready


async def _probe_agent(timeout: float = 60.0) -> bool:
    launch = _hermes()
    if launch is None:
        return False
    try:
        from hermes_module.integration import config as _cfg  # type: ignore
        if not _cfg.module_enabled():
            return False
    except Exception:
        swallow.note('agentai_bridge._probe_agent:L147')
        pass
    deadline = time.time() + timeout
    # launch.health() ходит в netstat/powershell с таймаутами до 8/25 с.
    # Вызванный прямо в обработчике, он блокировал event loop: на время
    # проверки вставали ВСЕ запросы сервера (страница грузилась 6-10 с).
    # Для ожидания достаточно одного асинхронного HTTP-пинга шлюза.
    try:
        from hermes_module.integration import config as _cfg  # type: ignore
        gw_health = f"http://{_cfg.GW_HOST}:{_cfg.GW_PORT}/health"
    except Exception:
        swallow.note('agentai_bridge._probe_agent:L158')
        return False
    async with httpx.AsyncClient(timeout=httpx.Timeout(2.0)) as client:
        while time.time() < deadline:
            try:
                r = await client.get(gw_health)
                if r.status_code < 500:
                    return True
            except Exception:
                swallow.note('agentai_bridge._probe_agent:L167')
                pass
            await asyncio.sleep(1.5)
    logger.warning("[agent-bridge] агент не поднялся за %.0f c", timeout)
    return False


def _gw_headers(launch, session_key: str, profile: str = "") -> dict:
    """Заголовки gateway: bearer-ключ профиля + scoping по сессии.

    X-Hermes-Session-Key — то, чем агент разделяет переписки. Без него
    все сообщения сливаются в один поток, поэтому ключ всегда свой.
    """
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_profile_key(launch, profile)}",
        "X-Hermes-Session-Key": session_key,
    }


# --------------------------------------------------------------------------
# Реестр сессий
# --------------------------------------------------------------------------
# Карта живёт в памяти процесса, но без диска переписка терялась при
# перезапуске приложения: карта пустела, и мост создавал новую сессию,
# хотя у агента история оставалась (её БД персистентна).
def _registry_path() -> str:
    from hermes_module.integration import config  # type: ignore
    return os.path.join(os.path.dirname(config.STATE_FILE), "agentai_sessions.json")


def _load_registry() -> None:
    global _SESSIONS
    if _SESSIONS:
        return
    try:
        with open(_registry_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            with _SESSIONS_LOCK:
                for k, v in data.items():
                    if isinstance(v, dict) and v.get("sid"):
                        _SESSIONS.setdefault(str(k), v)
    except FileNotFoundError:
        swallow.note('agentai_bridge._load_registry:L211')
        pass
    except Exception as e:
        logger.warning(f"[agent-bridge] реестр сессий не прочитан: {e}")


def _save_registry() -> None:
    try:
        path = _registry_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _SESSIONS_LOCK:
            data = dict(_SESSIONS)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"[agent-bridge] реестр сессий не сохранён: {e}")


# Запись реестра — блокирующая (JSON + os.replace), а вызывалась она прямо с
# event loop на каждом новом диалоге. Пока агент поднимается, клиент жался
# кнопки «отправить» снова и снова, и каждая попытка останавливала все
# параллельные запросы. Теперь изменения только помечаются грязными, а файл
# пишется один раз на окно _REG_FLUSH_WINDOW и в LOW-пуле воркеров.
_REG_FLUSH_WINDOW = 1.5
_REG_FLUSH_LOCK = threading.Lock()
_reg_flush_pending = False


def _save_registry_soon() -> None:
    """Отложенная запись реестра (debounce): пачка правок -> один файл."""
    global _reg_flush_pending
    with _REG_FLUSH_LOCK:
        if _reg_flush_pending:
            return
        _reg_flush_pending = True

    def _later() -> None:
        global _reg_flush_pending
        try:
            time.sleep(_REG_FLUSH_WINDOW)
        finally:
            with _REG_FLUSH_LOCK:
                _reg_flush_pending = False
            _save_registry()

    try:
        from worker_pool import LOW_POOL
        LOW_POOL.submit(_later)
        return
    except Exception:
        swallow.note('agentai_bridge._save_registry_soon:L263')
        pass
    try:
        threading.Thread(target=_later, daemon=True, name="agentai-reg-flush").start()
    except Exception:
        swallow.note('agentai_bridge._save_registry_soon:L268')
        _save_registry()


# Реестр нужен сразу после старта и не должен терять правки при остановке.
try:
    atexit.register(_save_registry)
except Exception:
    swallow.note('agentai_bridge.<module>:L276')
    pass


def _reg_key(profile: str, session_key: str) -> str:
    """Ключ реестра сессий.

    У профиля по умолчанию ключ остаётся прежним — иначе переписка,
    начатая до появления личностей, потеряла бы связь с агентом. У
    именованных профилей к ключу добавляется имя профиля: переписка
    личностей не должна смешиваться.
    """
    if profile and profile != "default":
        return f"{profile}::{session_key}"
    return session_key


def _take_session(profile: str, session_key: str) -> Optional[dict]:
    _load_registry()
    with _SESSIONS_LOCK:
        rec = _SESSIONS.get(_reg_key(profile, session_key))
        return dict(rec) if rec else None


def _put_session(profile: str, session_key: str, sid: str, title: str,
                 app: str = "default") -> None:
    key = _reg_key(profile, session_key)
    with _SESSIONS_LOCK:
        prev = _SESSIONS.get(key) or {}
        _SESSIONS[key] = {
            "sid": sid,
            "title": title or prev.get("title", ""),
            "created": prev.get("created", time.time()),
            "profile": profile or "default",
            # Профиль приложения, от имени которого создана сессия. По нему
            # проверяется, чью переписку разрешено продолжать/удалять.
            "app": app or "default",
        }
    _save_registry_soon()


# --------------------------------------------------------------------------
# Профиль приложения (не личность агента)
# --------------------------------------------------------------------------
# Личность (persona) — это роль агента, а профиль приложения — рабочая
# область пользователя. Раньше мост про профиль приложения не знал: ключ
# сессии у палитры был общий (`quickbar`), поэтому переписка и вызовы API
# одного профиля были доступны другому. Теперь каждая сессия привязана к
# профилю вызывающего, а агенту выдаётся подписанный токен этого профиля,
# с которым его инструменты обращаются к API от правильного имени.
_CTX_PREFIX = "[infinity-profile: "


def _pm():
    try:
        import profiles_module  # type: ignore
        return profiles_module
    except Exception:
        swallow.note('agentai_bridge._pm:L334')
        return None


def _app_profile(obj) -> str:
    """Профиль приложения, от которого пришёл запрос (cookie → токен → default)."""
    pm = _pm()
    if pm is None:
        return "default"
    try:
        return pm.request_profile(obj) or "default"
    except Exception:
        swallow.note('agentai_bridge._app_profile:L346')
        return "default"


def _app_key(app_profile: str, session_key: str) -> str:
    """Ключ сессии с привязкой к профилю приложения.

    У общедоступного профиля ключ прежний: переписка, начатая до появления
    привязки, не должна потеряться. У остальных — со своим префиксом.
    """
    if app_profile and app_profile != "default":
        return f"{app_profile}~{session_key}"
    return session_key


def _app_title(app_profile: str, title: str) -> str:
    """Заголовок сессии агента, различимый между профилями приложения.

    Заголовок — второй ключ восстановления сессии после перезапуска
    (поиск по title). Без пометки профиль мог бы присвоить чужой диалог
    с тем же названием.
    """
    title = (title or "").strip()
    if not title or not app_profile or app_profile == "default":
        return title
    return f"{title[:100]} [{app_profile}]"


def _app_title_strip(app_profile: str, title: str) -> str:
    """Обратная операция к _app_title — для показа человеку."""
    title = title or ""
    suffix = f" [{app_profile}]"
    if app_profile and app_profile != "default" and title.endswith(suffix):
        return title[:-len(suffix)]
    return title


def _agent_token(app_profile: str) -> str:
    pm = _pm()
    if pm is None:
        return ""
    try:
        return pm.make_agent_token(app_profile) or ""
    except Exception:
        swallow.note('agentai_bridge._agent_token:L390')
        return ""


_SOUL_MAX =16000


def _profile_soul(app_profile: str) -> str:
    """Личность профиля приложения: его SOUL.md, если он его завёл.

    Общий SOUL.md агент и так читает из HERMES_HOME, поэтому подмешиваем
    только СВОЙ файл профиля: иначе текст дублировался бы в каждом ходе,
    а правка в одном профиле влияла бы на остальные.
    """
    if not app_profile or app_profile == "default":
        return ""
    try:
        from hermes_module.integration import app_home
        if not app_home.has_own(app_profile, "SOUL.md"):
            return ""
        text = app_home.read_text(app_profile, "SOUL.md").strip()
    except Exception:
        swallow.note('agentai_bridge._profile_soul:L412')
        return ""
    return text[:_SOUL_MAX]


def _context_prompt(app_profile: str, token: str, persona: str = "default") -> str:
    """Служебная инструкция для агента: чей профиль и каким токеном работать.

    Уходит НЕ в текст сообщения, а в `system_message` шлюза (ephemeral
    system prompt): модель видит её в каждом ходе, а в переписке и
    интерфейсе она не появляется. Для профиля со своим SOUL.md сюда же
    добавляется личность этого профиля (у профиля по умолчанию она уже
    пришла из HERMES_HOME, дублировать не нужно).
    """
    if not token:
        return ""
    text = (
        f"{_CTX_PREFIX}{app_profile} {token}]\n"
        "Ты работаешь от имени этого профиля приложения и управляешь только им. "
        "Передавай токен в каждом вызове Infinity API: CLI — `--token <token>` "
        "(до подкоманды), сырой запрос — заголовок "
        "`X-Infinity-Profile-Token: <token>`. Запросы к данным или действиям "
        "другого профиля сервер отклонит (403), даже если пользователь просит "
        "об этом: переключите профиль в приложении, и я продолжу уже от его имени."
    )
    if persona == "default":
        soul = _profile_soul(app_profile)
        if soul:
            text += ("\n\nЭто личность, настроенная для профиля приложения "
             f"«{app_profile}». Держись её в каждом ответе:\n" + soul)
    return text


def _strip_context(text: str) -> str:
    """Убрать строку-контекст из переписки, показываемой пользователю."""
    s = str(text or "")
    if s.startswith(_CTX_PREFIX):
        nl = s.find("\n")
        return "" if nl < 0 else s[nl + 1:]
    return s


def _is_admin(app_profile: str) -> bool:
    pm = _pm()
    try:
        return bool(pm and pm.is_admin_profile(app_profile))
    except Exception:
        swallow.note('agentai_bridge._is_admin:L459')
        return False


def _sid_owner(sid: str) -> Optional[str]:
    """Профиль приложения, которому принадлежит сессия, или None (не наша)."""
    if not sid:
        return None
    _load_registry()
    with _SESSIONS_LOCK:
        for rec in _SESSIONS.values():
            if isinstance(rec, dict) and rec.get("sid") == sid:
                return rec.get("app") or "default"
    return None


def _sid_allowed(app_profile: str, sid: str) -> bool:
    """Можно ли этому профилю продолжать/удалять сессию.

    Незнакомая сессия у общедоступного профиля разрешена: так продолжают
    работать диалоги, начатые до привязки (их нет в реестре) и сессии,
    созданные в интерфейсе агента. Остальным — только свои; админу — любые.
    """
    owner = _sid_owner(sid)
    if owner == app_profile:
        return True
    if owner is None:
        return app_profile == "default" or _is_admin(app_profile)
    return _is_admin(app_profile)


def _owner_map() -> dict:
    """{sid: профиль приложения} для всего реестра — одним чтением."""
    _load_registry()
    with _SESSIONS_LOCK:
        return {str(r.get("sid")): (r.get("app") or "default")
                for r in _SESSIONS.values()
                if isinstance(r, dict) and r.get("sid")}


def _visible_session(app_profile: str, sid: str,
                     owners: Optional[dict] = None) -> bool:
    """Показывать ли сессию в списке этого профиля.

    Переписки, привязанные к другому профилю приложения, скрыты. Сессии без
    владельца (начатые в самом модуле Infinity Agent или до привязки) остаются
    видны только общедоступному профилю и админу — ровно как в _sid_allowed.
    Раньше они были видны всем, и чистый профиль получал чужие диалоги: реестр
    заполняется только при создании через мост, а он был пуст.
    """
    if not sid:
        return False
    owner = (owners if owners is not None else _owner_map()).get(sid)
    if owner is None:
        return app_profile == "default" or _is_admin(app_profile)
    return owner == app_profile


def forget_sid(sid: str) -> None:
    """Убрать сессию из реестра (её удалили в интерфейсе агента)."""
    if not sid:
        return
    with _SESSIONS_LOCK:
        keys = [k for k, v in _SESSIONS.items()
                if isinstance(v, dict) and v.get("sid") == sid]
        for k in keys:
            _SESSIONS.pop(k, None)
    if keys:
        _save_registry_soon()


def _remember_owner(app_profile: str, persona: str, sid: str, title: str) -> None:
    """Запомнить владельца сессии, созданной в обход обычного ключа."""
    if not sid:
        return
    _put_session(persona, _app_key(app_profile, "sid:" + sid), sid, title,
                 app_profile)


async def _find_by_title(client: httpx.AsyncClient, launch, title: str,
                         profile: str = "") -> str:
    """Найти сессию агента по заголовку — после перезапуска приложения.

    Список фильтруется по title как по поисковому запросу, поэтому
    совпадение проверяем точечно: нам нужен именно этот диалог.
    """
    if not title:
        return ""
    try:
        r = await client.get(_gw_base(launch, profile) + "/api/sessions",
                             headers=_gw_headers(launch, "lookup", profile),
                             params={"title": title, "include_hidden": "true", "limit": 20})
        if r.status_code >= 400:
            return ""
        for s in (r.json().get("data") or []):
            if isinstance(s, dict) and (s.get("title") or "") == title:
                return str(s.get("id") or "")
    except Exception as e:
        logger.warning(f"[agent-bridge] поиск сессии по заголовку не удался: {e}")
    return ""



def _gw_error(r) -> str:
    """Текст ошибки gateway: он содержательнее, чем «400 Bad Request»."""
    try:
        d = r.json()
        err = d.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or "")
        if isinstance(err, str):
            return err
        return str(d)[:300]
    except Exception:
        swallow.note('agentai_bridge._gw_error:L573')
        try:
            return r.text[:300]
        except Exception:
            swallow.note('agentai_bridge._gw_error:L577')
            return ""


async def _create_session(client: httpx.AsyncClient, launch, session_key: str,
                         title: str, sid_hint: str = "", profile: str = "") -> tuple:
    """Создать сессию. Возвращает (sid, текст_ошибки).

    Заголовок агента требует уникальный: вторая сессия с тем же
    «Диагностика ошибок» вернула бы invalid_title. Это не конфликт, а
    подсказка: сессия с таким названием уже есть, и она нужна та же самая
    (после перезапуска приложения карта сессий пуста, а переписка — нет).
    Поэтому сначала пробуем забрать её, и только если не получилось —
    создаём без заголовка.
    """
    payload = {}
    if title:
        payload["title"] = title
    if sid_hint:
        payload["id"] = sid_hint
    r = await client.post(_gw_base(launch, profile) + "/api/sessions",
                          headers=_gw_headers(launch, session_key, profile), json=payload)
    if r.status_code >= 400:
        text = _gw_error(r) + " " + (r.text or "")
        # Формат 0.23: «Title '…' is already in use by session <id>».
        taken = re.search(r"already in use by session\s+[\"']?([A-Za-z0-9_.:-]+)",
                          text)
        if taken:
            return taken.group(1), ""
        payload.pop("title", None)
        r = await client.post(_gw_base(launch, profile) + "/api/sessions",
                              headers=_gw_headers(launch, session_key, profile), json=payload)
    if r.status_code >= 400:
        return "", _gw_error(r) or f"gateway ответил {r.status_code}"
    data = r.json()
    sid = ((data.get("session") or {}).get("id")) or data.get("id") or ""
    return sid, ("" if sid else "агент не вернул id сессии")


async def _resolve_sid(client: httpx.AsyncClient, launch, session_key: str,
                       title: str, profile: str = "",
                       app_profile: str = "default") -> str:
    """Возвращает id сессии агента, создавая её при первом обращении.

    Порядок: сохранённая -> живая по сохранённому id -> существующая с тем
    же заголовком -> новая. Второй шаг нужен на случай, когда агент
    перезапускался между запросами, третий — когда перезапускалось наше
    приложение и реестр пуст, а история у агента осталась.
    """
    rec = _take_session(profile, session_key)
    if rec and rec.get("sid"):
        try:
            r = await client.get(_gw_base(launch, profile) + f"/api/sessions/{rec['sid']}",
                                 headers=_gw_headers(launch, session_key, profile))
            if r.status_code < 400:
                return rec["sid"]
        except Exception:
            swallow.note('agentai_bridge._resolve_sid:L632')
            pass  # сеть моргнула — пробуем восстановить сессию иначе

        # Наш id больше не живёт у агента: попробуем тот же диалог по заголовку.
        found = await _find_by_title(client, launch, rec.get("title", ""), profile)
        if found:
            _put_session(profile, session_key, found, rec.get("title", ""), app_profile)
            return found

    if title:
        found = await _find_by_title(client, launch, title, profile)
        if found:
            _put_session(profile, session_key, found, title, app_profile)
            return found

    sid, err = await _create_session(client, launch, session_key, title,
                                    (rec or {}).get("sid", ""), profile)
    if not sid:
        # sid из прошлого запуска мог быть занят — пробуем без него.
        sid, err2 = await _create_session(client, launch, session_key, title, "", profile)
        if not sid:
            raise RuntimeError(err or err2)
    _put_session(profile, session_key, sid, title, app_profile)
    return sid


def _sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _chat_payload(message: str, attachments) -> dict:
    """Тело для /chat/stream: текст или multimodal-части со скриншотами.

    Агент принимает OpenAI-форму content (text + image_url с data: URL), но
    текстовый запрос сворачивает в строку — поэтому без вложений отправляем
    ровно строку, как раньше.
    """
    parts = []
    if message:
        parts.append({"type": "text", "text": message})
    for a in (attachments or [])[:_MAX_ATTACH]:
        if not isinstance(a, dict):
            continue
        url = str(a.get("data_url") or "")
        if not url.startswith("data:image/"):
            continue
        if len(url) > _MAX_ATTACH_BYTES:
            continue
        parts.append({"type": "image_url", "image_url": {"url": url}})
    if len(parts) <= 1 or not any(p["type"] == "image_url" for p in parts):
        return {"message": message}
    return {"message": parts}


# --------------------------------------------------------------------------
# Публикация диалога в интерфейс агента
# --------------------------------------------------------------------------
# Сайдбар агента рисуется ИСКЛЮЧИТЕЛЬНО из его собственного состояния
# (assets/js/04-render.js читает state.sessions), GET /api/sessions он не
# вызывает и «подключить существующую сессию» в нём нет. Поэтому диалог
# из палитры или из админки там появляется только как запись в состоянии
# рабочей области — оно лежит на диске и отдаётся через
# /api/workspace/state (общий для обоих origin источник истины).
def _local_session_id(session_key: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]", "_", session_key.lower())[:40]
    return "s_" + slug


def _agent_ui_base() -> str:
    from hermes_module.integration import config  # type: ignore
    return f"http://127.0.0.1:{config.UI_PORT}"


async def publish_to_agent_ui(launch, session_key: str, title: str,
                              sid: str, messages: list) -> bool:
    """Записать/обновить наш диалог в состоянии рабочей области агента.

    read-modify-write целиком: состояние пишет и сам интерфейс агента
    (огонь-и-забыл), поэтому перечитываем его прямо перед записью.
    Стабильный id сессии означает, что потерянная публикация
    восстановится на следующем ходе, а не породит дубликат.
    """
    if not sid or not title:
        return False
    base = _agent_ui_base()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0)) as c:
            r = await c.get(base + "/api/workspace/state")
            state = r.json() if r.status_code < 400 and r.text.strip() else {}
            if not isinstance(state, dict):
                state = {}
            sessions = state.get("sessions")
            if not isinstance(sessions, list):
                sessions = []

            now_ms = int(time.time() * 1000)
            local_id = _local_session_id(session_key)
            # model/mode интерфейс агента берёт из настроек новой сессии;
            # для чужой берём значения любой существующей, чтобы диалог
            # можно было продолжить прямо в интерфейсе агента.
            proto = next((s for s in sessions if isinstance(s, dict)), {}) or {}

            msgs = []
            for i, m in enumerate(messages[-60:]):
                role = "user" if m.get("role") == "user" else "agent"
                content = str(m.get("content") or "")
                if not content:
                    continue
                msgs.append({
                    "id": f"m_{local_id}_{i}",
                    "ts": int(float(m.get("ts") or 0) * 1000) or (now_ms - (len(messages) - i) * 1000),
                    "status": "done",
                    "role": role,
                    "kind": "text",
                    "content": content,
                })

            existing = next((s for s in sessions if isinstance(s, dict) and s.get("id") == local_id), None)
            session = {
                "id": local_id,
                "title": title,
                "projectId": None, "folderId": None,
                "pinned": False, "archived": False, "locked": False,
                "tags": [], "bookmarks": [], "reactions": {},
                "createdAt": (existing or {}).get("createdAt") or now_ms,
                "updatedAt": now_ms,
                "model": proto.get("model", ""), "mode": proto.get("mode", ""),
                "messages": msgs,
                "costIn": 0, "costOut": 0,
                # Ключевой момент: с backendId интерфейс агента продолжит ЭТУ
                # сессию (ensureBackend возвращает её, не создавая новую).
                "backendId": sid,
            }
            if existing is not None:
                sessions[sessions.index(existing)] = session
            else:
                sessions.insert(0, session)

            state["sessions"] = sessions
            r2 = await c.post(base + "/api/workspace/state", json=state)
            if r2.status_code >= 400:
                logger.warning(f"[agent-bridge] публикация в агент не записана: {r2.status_code}")
                return False
            return True
    except Exception as e:
        # Публикация не должна ломать чат: диалог всё равно работает.
        logger.warning(f"[agent-bridge] публикация в интерфейс агента не удалась: {e}")
        return False


# Задачи живут в переменной: без ссылки event loop имеет право собрать
# задачу до её завершения.
_PENDING: set = set()


def _spawn(coro):
    task = asyncio.ensure_future(coro)
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
    return task


async def publish_after_turn(launch, session_key: str, title: str, sid: str) -> None:
    """Опубликовать диалог в интерфейсе агента после ответа.

    Историю берём у агента: она авторитетна и уже включает только что
    законченный ход, так что пересобирать её из чанков не нужно.
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
            msgs = await _session_messages(client, launch, sid, session_key)
            await publish_to_agent_ui(launch, session_key, title, sid, msgs)
    except Exception as e:
        logger.warning(f"[agent-bridge] публикация после ответа не удалась: {e}")


# --------------------------------------------------------------------------
# Роут
# --------------------------------------------------------------------------
@router.get("/api/agentai/profile-context")
def agentai_profile_context(request: Request):
    """Профиль приложения вызывающего и подписанный токен для инструментов.

    Палитра получает контекст внутри /api/agentai/ask. Интерфейс агента
    (/agentai) ходит в шлюз напрямую, поэтому внедрённый в его HTML скрипт
    (integration/bridge.py) берёт токен здесь и кладёт инструкцию в
    ephemeral system_message каждого хода.
    """
    app_profile = _app_profile(request)
    token = _agent_token(app_profile)
    return JSONResponse({
        "profile": app_profile,
        "token": token,
        "header": "X-Infinity-Profile-Token",
        "prompt": _context_prompt(app_profile, token),
    })


@router.post("/api/agentai/ask")
async def agentai_ask(request: Request):
    """Отправить сообщение агенту и получить ответ потоком (SSE).

    Тело: {"message": str, "session_key": str, "title": str}.
    Ответ: SSE-поток событий агента. Первым событием идёт `session`
    с ключом и id сессии — клиент сохраняет его и при следующем вызове
    передаёт тот же session_key, продолжая ту же переписку.
    """
    launch = _hermes()
    if launch is None:
        return JSONResponse({"error": "Модуль Infinity Agent недоступен"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Некорректный JSON"}, status_code=400)

    message = str(body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "Пустое сообщение"}, status_code=400)
    if len(message) > _MAX_MESSAGE:
        message = message[:_MAX_MESSAGE] + "\n… (сообщение обрезано)"

    # Профиль приложения — из cookie (или токена агента), никогда из тела.
    app_profile = _app_profile(request)
    raw_session_key = str(body.get("session_key") or "default").strip() or "default"
    session_key = _app_key(app_profile, raw_session_key)
    raw_title = str(body.get("title") or "").strip()
    title = _app_title(app_profile, raw_title)
    # Личность агента (профиль Hermes). Пусто или "default" — обычный агент.
    profile = str(body.get("profile") or "default").strip() or "default"
    # Готовая сессия агента: палитра переключается между диалогами, и для
    # этого нужно знать не ключ, а конкретную сессию.
    session_id = str(body.get("session_id") or "").strip()
    if session_id and not _sid_allowed(app_profile, session_id):
        return JSONResponse({"error": "Сессия принадлежит другому профилю"},
                            status_code=403)
    if session_id and _sid_owner(session_id) is None:
        # Кто реально начал работать с бесхозным диалогом — тот его владелец,
        # иначе реестр так и остаётся пустым и список «для всех».
        _remember_owner(app_profile, profile, session_id, title)
    attachments = body.get("attachments")
    if not isinstance(attachments, list):
        attachments = []
    # Контекст профиля — в ephemeral system prompt шлюза: модель видит его
    # в каждом ходе, но в переписке и интерфейсе он не появляется. Токен
    # подписан приложением; инструменты агента (infinity_api.py) передают
    # его заголовком X-Infinity-Profile-Token.
    payload = _chat_payload(message, attachments)
    context = _context_prompt(app_profile, _agent_token(app_profile),
                                 profile)
    if context:
        payload["system_message"] = context

    try:
        launch.start_async_if_needed()
        # Агент мог ещё не подняться — ждём его, иначе запрос уйдёт
        # в шлюз, который не слушает, и переписка выйдет пустой.
        await _wait_agent()
    except Exception as e:
        logger.warning(f"[agent-bridge] не удалось поднять агента: {e}")

    async def event_stream():
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                sid = session_id or await _resolve_sid(client, launch, session_key, title,
                                                      profile, app_profile)
                # Наружу отдаём исходный ключ клиента: наш внутренний префикс
                # профиля — деталь реализации, клиент не должен его накапливать.
                yield _sse("session", {"session_key": raw_session_key, "sid": sid,
                                       "profile": profile, "app_profile": app_profile})

                url = _gw_base(launch, profile) + f"/api/sessions/{sid}/chat/stream"
                async with client.stream("POST", url,
                                        headers=_gw_headers(launch, session_key, profile),
                                        json=payload) as up:
                    if up.status_code >= 400:
                        await up.aread()   # без тела .text/.json недоступны у stream-ответа
                        yield _sse("error", {"message": _gw_error(up) or f"агент ответил {up.status_code}"})
                        yield _sse("done", {})
                        return

                    async for chunk in up.aiter_raw():
                        if chunk:
                            yield chunk

                # Публикация — отдельной задачей: она не должна задерживать
                # закрытие потока, иначе браузер позже узнает об ответе.
                # В общий интерфейс агента попадает только переписка профиля
                # по умолчанию: у остальных профилей приложения она не должна
                # быть видна в чужой панели.
                if profile == "default" and app_profile == "default":
                    _spawn(publish_after_turn(launch, session_key, title, sid))
        except asyncio.CancelledError:
            # Клиент закрыл соединение (Esc, переход на другую страницу).
            raise
        except Exception as e:
            logger.warning(f"[agent-bridge] ошибка запроса к агенту: {e}")
            yield _sse("error", {"message": str(e)})
            yield _sse("done", {})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _session_messages(client: httpx.AsyncClient, launch, sid: str,
                             session_key: str, profile: str = "") -> list:
    """История сессии агента в плоском виде [{role, content}]."""
    # order=oldest — по умолчанию агент отдаёт последние сообщения
    # (новые сверху), а нам для показа нужна прямая речь.
    r = await client.get(
        _gw_base(launch, profile) + f"/api/sessions/{sid}/messages?order=oldest&limit=100",
        headers=_gw_headers(launch, session_key, profile))
    if r.status_code >= 400:
        return []
    data = r.json()
    # Агент отдаёт список в поле data (OpenAI-формат), а не messages.
    items = data.get("data") or data.get("messages") or []
    out = []
    for m in items:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or ""
        if role not in ("user", "assistant"):
            continue
        content = m.get("content")
        if isinstance(content, list):
            bits = []
            for p in content:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    bits.append(str(p.get("text") or ""))
                elif str(p.get("type") or "").endswith("image_url") or "image_url" in p:
                    # Скриншот мы приложить умеем, но показать в тред
                    # палитры его не отдаём — помечаем, что он был.
                    bits.append("[изображение]")
            content = " ".join(bits)
        content = str(content or "").strip()
        if role == "user":
            # Служебная строка профиля не должна появляться в треде палитры.
            content = _strip_context(content).strip()
        if content:
            out.append({"role": role, "content": content[:8000],
                        "ts": m.get("timestamp") or 0})
    return out[-60:]


@router.get("/api/agentai/session/{session_key}")
async def agentai_session(request: Request, session_key: str, title: str = Query(""),
                           profile: str = Query("default"), session_id: str = Query("")):
    """История переписки по ключу — единый источник правды для всех окон.

    Хранить переписку в браузере нельзя: окно рабочего стола и окно
    палитры датируют друг друга. Поэтому читаем её у агента.

    У каждой личности (профиля агента) своя переписка: профиль запрашивается
    параметром profile, иначе вернулась бы история профиля по умолчанию.
    session_id — конкретная сессия агента (палитра переключается между
    диалогами); без него берётся сессия по ключу.
    """
    launch = _hermes()
    if launch is None:
        return JSONResponse({"error": "Модуль Infinity Agent недоступен"}, status_code=503)

    app_profile = _app_profile(request)
    profile = (profile or "default").strip() or "default"
    raw_session_key = (session_key or "").strip() or "default"
    session_key = _app_key(app_profile, raw_session_key)
    rec = _take_session(profile, session_key)
    raw_title = (title or "").strip()
    title = _app_title(app_profile, raw_title) or (rec or {}).get("title", "")
    session_id = (session_id or "").strip()
    if session_id and not _sid_allowed(app_profile, session_id):
        return JSONResponse({"error": "Сессия принадлежит другому профилю"},
                            status_code=403)
    if session_id and _sid_owner(session_id) is None:
        _remember_owner(app_profile, profile, session_id, title)
    ready = True

    try:
        launch.start_async_if_needed()
        # Агент мог ещё не подняться — ждём его, иначе запрос уйдёт
        # в шлюз, который не слушает, и переписка выйдет пустой.
        ready = await _wait_agent()
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            sid = (session_id or "").strip() or (rec or {}).get("sid", "")
            if not sid:
                sid = await _find_by_title(client, launch, title, profile)
                if sid:
                    _put_session(profile, session_key, sid, title, app_profile)
            if not sid:
                return JSONResponse({"session_key": raw_session_key, "messages": [],
                                     "profile": profile, "agent_ready": ready})
            messages = await _session_messages(client, launch, sid, session_key, profile)
            if not messages:
                # id мог устареть после перезапуска агента — один раз ищем заново.
                fresh = await _find_by_title(client, launch, title, profile)
                if fresh and fresh != sid:
                    _put_session(profile, session_key, fresh, title, app_profile)
                    sid = fresh
                    messages = await _session_messages(client, launch, sid, session_key, profile)
    except Exception as e:
        logger.warning(f"[agent-bridge] история недоступна: {e}")
        return JSONResponse({"session_key": raw_session_key, "messages": [],
                             "profile": profile, "agent_ready": False})

    return JSONResponse({"session_key": raw_session_key, "sid": sid,
                         "title": raw_title or _app_title_strip(app_profile, title),
                         "messages": messages, "profile": profile,
                         "agent_ready": ready})


@router.post("/api/agentai/reset")
async def agentai_reset(request: Request):
    """Забыть сессию по ключу — следующий запрос начнёт новую переписку."""
    try:
        body = await request.json()
    except Exception:
        swallow.note('agentai_bridge.agentai_reset:L1047')
        body = {}
    key = str(body.get("session_key") or "").strip()
    if not key:
        return JSONResponse({"ok": False}, status_code=400)
    app_profile = _app_profile(request)
    profile = str(body.get("profile") or "default").strip() or "default"
    with _SESSIONS_LOCK:
        _SESSIONS.pop(_reg_key(profile, _app_key(app_profile, key)), None)
    # Без записи файла «сброс» жил только до перезапуска: реестр возвращал
    # старый sid, и палитра снова продолжала прежнюю переписку.
    _save_registry_soon()
    return JSONResponse({"ok": True, "session_key": key, "profile": profile,
                         "app_profile": app_profile})


# --------------------------------------------------------------------------
# Сессии агента: список, создание, удаление
# --------------------------------------------------------------------------
# Палитра (окно Shift+Space) и оверлей рабочего стола показывают одни и
# те же переписки. Раньше у палитры был ОДИН фиксированный ключ сессии, и
# переключаться между диалогами было нечем. Теперь палитра работает с
# сессиями агента напрямую: список берём у шлюза, а активную храним в
# localStorage — окна Electron и страница /desktop живут на одном origin,
# поэтому видят одно и то же.
@router.get("/api/agentai/sessions")
async def agentai_sessions(request: Request, profile: str = Query("default"),
                           limit: int = Query(50), session_key: str = Query("")):
    """Сессии агента — по профилю (личности).

    В ответе есть current: id сессии, к которой привязан session_key
    клиента (у палитры это «Быстрый запрос»). Она же подсвечивается в
    списке сессий, чтобы видеть, какая переписка сейчас открыта.

    Список у шлюза общий, поэтому каждому профилю приложения показываем
    только его собственные сессии: переписки разных профилей не должны
    смешиваться и в палитре. Наследие до привязки (без владельца) остаётся
    у общедоступного профиля.
    """
    launch = _hermes()
    if launch is None:
        return JSONResponse({"error": "Модуль Infinity Agent недоступен"}, status_code=503)
    app_profile = _app_profile(request)
    profile = (profile or "default").strip() or "default"
    try:
        launch.start_async_if_needed()
        # Агент мог ещё не подняться — ждём его, иначе запрос уйдёт
        # в шлюз, который не слушает, и переписка выйдет пустой.
        await _wait_agent()
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(_gw_base(launch, profile) + "/api/sessions",
                                 headers=_gw_headers(launch, "sessions", profile),
                                 params={"limit": max(1, min(int(limit or 50), 200))})
            if r.status_code >= 400:
                return JSONResponse({"sessions": [], "profile": profile})
            data = r.json()
    except Exception as e:
        logger.warning(f"[agent-bridge] список сессий недоступен: {e}")
        return JSONResponse({"sessions": [], "profile": profile})

    out = []
    for s in (data.get("data") or []):
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "")
        if not sid:
            continue
        out.append({
            "id": sid,
            "title": str(s.get("title") or "") or "Без названия",
            "created": s.get("started_at") or s.get("created_at") or 0,
            "updated": s.get("last_activity_at") or s.get("updated_at") or 0,
            "messages": s.get("message_count") or 0,
        })
    out.sort(key=lambda x: (x.get("updated") or x.get("created") or 0), reverse=True)
    owners = _owner_map()
    out = [s for s in out if _visible_session(app_profile, s["id"], owners)]
    current = ""
    if session_key:
        rec = _take_session(profile, _app_key(app_profile, session_key))
        current = str((rec or {}).get("sid") or "")
    return JSONResponse({"sessions": out, "profile": profile,
                         "app_profile": app_profile, "current": current})


@router.post("/api/agentai/session/new")
async def agentai_session_new(request: Request, profile: str = Query("default")):
    """Создать новую сессию агента (личность — та же)."""
    launch = _hermes()
    if launch is None:
        return JSONResponse({"error": "Модуль Infinity Agent недоступен"}, status_code=503)
    app_profile = _app_profile(request)
    profile = (profile or "default").strip() or "default"
    try:
        body = await request.json()
    except Exception:
        swallow.note('agentai_bridge.agentai_session_new:L1143')
        body = {}
    raw_title = str(body.get("title") or "").strip()[:120]
    title = _app_title(app_profile, raw_title)
    try:
        launch.start_async_if_needed()
        # Агент мог ещё не подняться — ждём его, иначе запрос уйдёт
        # в шлюз, который не слушает, и переписка выйдет пустой.
        await _wait_agent()
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            sid, err = await _create_session(client, launch, "new-session", title, "", profile)
            if not sid:
                return JSONResponse({"error": err or "агент не вернул id сессии"},
                                    status_code=502)
    except Exception as e:
        logger.warning(f"[agent-bridge] сессия не создана: {e}")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)
    # Запоминаем владельца: следующее сообщение придёт с session_id, и
    # проверка «чья сессия» должна знать ответ.
    _remember_owner(app_profile, profile, sid, title)
    return JSONResponse({"ok": True, "id": sid, "title": raw_title,
                         "profile": profile, "app_profile": app_profile})


@router.delete("/api/agentai/session/{sid}")
async def agentai_session_delete(request: Request, sid: str,
                                 profile: str = Query("default")):
    """Удалить сессию агента вместе с её перепиской."""
    launch = _hermes()
    if launch is None:
        return JSONResponse({"error": "Модуль Infinity Agent недоступен"}, status_code=503)
    app_profile = _app_profile(request)
    profile = (profile or "default").strip() or "default"
    if not _sid_allowed(app_profile, sid):
        return JSONResponse({"error": "Сессия принадлежит другому профилю"},
                            status_code=403)
    try:
        launch.start_async_if_needed()
        # Агент мог ещё не подняться — ждём его, иначе запрос уйдёт
        # в шлюз, который не слушает, и переписка выйдет пустой.
        await _wait_agent()
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.delete(_gw_base(launch, profile) + f"/api/sessions/{sid}",
                                    headers=_gw_headers(launch, "sessions", profile))
    except Exception as e:
        logger.warning(f"[agent-bridge] сессия не удалена: {e}")
        return JSONResponse({"error": str(e)[:200]}, status_code=500)
    if r.status_code >= 400 and r.status_code != 404:
        return JSONResponse({"error": _gw_error(r) or "агент не смог удалить сессию"},
                            status_code=502)
    # Убираем и из нашего реестра, чтобы палитра не вернулась к ней.
    forget_sid(sid)
    return JSONResponse({"ok": True, "id": sid, "profile": profile})