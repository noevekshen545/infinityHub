"""Infinity web server — serves infinity.html and bridges it to the Hermes gateway."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from aiohttp import web

from . import intercepts as _intercepts
from . import voice_api as _voice_api
from . import pyplugins as _pyplugins
try:
    import swallow                      # внутри приложения: след в общий лог
except ImportError:
    # модуль умеет работать и вне приложения — тогда следа нет, но и падения нет
    class swallow:
        @staticmethod
        def note(*a, **k):
            pass
try:
	from ..integration import app_home as _app_home
except ImportError:
	swallow.note('web_server.<module>:L34')
	from integration import app_home as _app_home

BACKEND_PATH = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_PATH) not in sys.path:
	sys.path.insert(0, str(BACKEND_PATH))

logger = logging.getLogger("infinity.server")

BASE_DIR = Path(__file__).resolve().parent
HTML_PATH = Path(os.getenv("INFINITY_HTML", str(BASE_DIR / "infinity.html")))

# Папка плагинов интерфейса. Сюда агент (Hermes fs tools) пишет .js-файлы,
# фронтенд следит за ней на лету — без перезагрузки страницы.
# backend/plugins НЕ используется: это внутренняя папка самого Hermes.
PLUGINS_DIR = Path(os.getenv("INFINITY_PLUGINS_DIR",
                             str(BASE_DIR.parent / "plugins")))
PLUGIN_EXT = ".js"
PLUGIN_MAX_BYTES = 512 * 1024  # потолок на размер одного .js

# ── Состояние рабочей области агента (сессии, плагины, заметки) ──────
# Раньше оно лежало ТОЛЬКО в браузере: IndexedDB «agent.workspace» или
# localStorage по ключу agent.workspace.v3. Оба хранилища привязаны к
# origin (схема+хост+порт), а интерфейс агента открывается с двух разных
# origin: http://127.0.0.1:9120/ и http://127.0.0.1:8281/agentai/.
# В итоге включённые плагины и заметки «пропали», как только пользователь
# заходил через другой адрес или перезапускал сервер. Теперь состояние
# дублируется на диск и является общим для обоих origin.
WORKSPACE_STATE_FILE = Path(os.getenv(
    "INFINITY_WORKSPACE_STATE",
    str(BASE_DIR.parent / "data" / "workspace_state.json")))
WORKSPACE_STATE_MAX_BYTES = 32 * 1024 * 1024  # 32 МБ


def _workspace_state_path() -> Path:
    """Путь к состоянию с учётом профиля (если он задан)."""
    prof = os.getenv("INFINITY_PROFILE_ID", "").strip()
    if prof:
        return WORKSPACE_STATE_FILE.with_name(
            f"workspace_state_{prof}.json")
    return WORKSPACE_STATE_FILE


def load_workspace_state() -> dict:
    """Прочитать сохранённое состояние. Битый файл -> пустой dict."""
    p = _workspace_state_path()
    try:
        if p.is_file():
            raw = p.read_text(encoding="utf-8")
            if raw.strip():
                data = json.loads(raw)
                if isinstance(data, dict):
                    return data
    except (OSError, ValueError) as e:
        logger.warning("workspace-state: не читается %s: %s", p, e)
    return {}


def save_workspace_state(data: dict) -> None:
    """Атомарно сохранить состояние на диск."""
    if not isinstance(data, dict):
        raise ValueError("workspace state должен быть объектом")
    p = _workspace_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    os.replace(str(tmp), str(p))


async def handle_workspace_state_get(request: web.Request) -> web.Response:
    """GET /api/workspace/state — состояние рабочей области агента."""
    if not _is_loopback(request):
        return web.json_response({"error": "доступ только с этой машины"}, status=403)
    return web.json_response(load_workspace_state())


async def handle_workspace_state_post(request: web.Request) -> web.Response:
    """POST /api/workspace/state — сохранить состояние рабочей области."""
    if not _is_loopback(request):
        return web.json_response({"error": "доступ только с этой машины"}, status=403)
    try:
        raw = await request.read()
    except Exception:
        return web.json_response({"error": "не удалось прочитать тело"}, status=400)
    if len(raw) > WORKSPACE_STATE_MAX_BYTES:
        return web.json_response({"error": "состояние слишком большое"}, status=413)
    try:
        data = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (UnicodeDecodeError, ValueError):
        return web.json_response({"error": "некорректный JSON"}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"error": "ожидался объект"}, status=400)
    try:
        save_workspace_state(data)
    except (OSError, ValueError) as e:
        logger.error("workspace-state: не сохранилось: %s", e)
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"ok": True, "bytes": len(raw),
                              "path": str(_workspace_state_path())})

# Файлы инструкций агента, которые можно править из Настроек → Агент.
# Жёсткий белый список: путь из запроса не принимается, только имя из списка.
# Иначе эндпоинт превратился бы в произвольную запись на диск.
AGENT_FILES_DIR = Path(os.getenv("INFINITY_AGENT_FILES_DIR", str(BASE_DIR.parent / ".hermes-portable")))
AGENT_FILES = {
    "SOUL.md": "Личность и манера агента",
    "USER.md": "Данные о пользователе",
    "AGENTS.md": "Инструкции по работе с проектом",
}
AGENT_FILE_MAX_BYTES = 2 * 1024 * 1024

#: Заголовок интеграционного прокси: профиль приложения вызывающего.
_APP_PROFILE_HEADER = "X-Hermes-App-Profile"
_PROFILE_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def _req_profile(request: web.Request) -> str:
	"""Профиль приложения из заголовка прокси ("" — профиль по умолчанию).

	Имя приходит из cookie Infinity, а не из пути: пропускаем только
	безопасные символы, иначе «../» увёл бы запись из каталога файлов.
	"""
	headers = getattr(request, "headers", None) or {}
	raw = str(headers.get(_APP_PROFILE_HEADER) or "").strip()
	_safe = _PROFILE_ID_RE.sub("", raw)[:64]
	return "" if not _safe or _safe == "default" else _safe


def _agent_base_dir(request: web.Request) -> Path:
	"""Каталог файлов инструкций профиля (у профилей приложения свой)."""
	if _app_home is not None and _req_profile(request):
		try:
			return _app_home.profile_home(_req_profile(request))
		except Exception:
			swallow.note('web_server._agent_base_dir:L168')
			pass
	return AGENT_FILES_DIR


def _agent_effective_file(request: web.Request, name: str) -> Path:
	"""Файл, который реально читается: свой профиля, иначе общий."""
	base = _agent_base_dir(request)
	own = base / name
	if own.is_file():
		return own
	shared = AGENT_FILES_DIR / name
	if shared != own and shared.is_file():
		return shared
	return own


def _agent_file_path(request: web.Request, name: str) -> Path:
	"""Куда писать файл инструкций: свой каталог профиля или общий."""
	return _agent_base_dir(request) / name


def _agent_prompt_path(request: web.Request) -> Path:
	"""Файл системных инструкций профиля (по умолчанию — прежний путь)."""
	pid = _req_profile(request)
	if pid and _app_home is not None:
		try:
			return _app_home.profile_home(pid) / _app_home.PROMPT_FILE
		except Exception:
			swallow.note('web_server._agent_prompt_path:L197')
			pass
	return AGENT_PROMPT_FILE

# Системные инструкции агента. Раньше поле «Системные инструкции» во вкладке
# «Агент» жило только в браузере, поэтому после перезапуска сервера (или на
# другом устройстве) текст выглядел пустым. Теперь он лежит рядом с
# остальными инструкциями и переживает перезапуск.
AGENT_PROMPT_FILE = Path(os.getenv(
    "INFINITY_AGENT_PROMPT", str(BACKEND_PATH / "AGENT_PROMPT.md")))

# Локальные библиотеки интерфейса (Monaco, xterm). Папка frontend/vendor
# кладётся рядом с infinity.html; раздаётся как /vendor/* до catch-all
# прокси, иначе запросы ушли бы на gateway.
VENDOR_DIR = Path(os.getenv("INFINITY_VENDOR_DIR", str(BASE_DIR / "vendor")))

# Стили и скрипты интерфейса, вынесенные из infinity.html в отдельные файлы
# (frontend/assets/css и frontend/assets/js). Раздаётся как /assets/* до
# catch-all прокси, иначе запросы ушли бы на gateway и вернули бы 404.
# ASSETS_NO_STORE=1 отключает кэш браузера — нужно при разработке.
ASSETS_DIR = Path(os.getenv("INFINITY_ASSETS_DIR", str(BASE_DIR / "assets")))
ASSETS_NO_STORE = os.getenv("INFINITY_ASSETS_NO_STORE", "1") not in ("0", "", "no")

FRONT_PORT = int(os.getenv("INFINITY_PORT", "9120"))
GW_HOST = os.getenv("INFINITY_GW_HOST", "127.0.0.1")
GW_PORT = int(os.getenv("INFINITY_GW_PORT", "8642"))
GW_KEY = os.getenv("INFINITY_GW_KEY", "")

# Общая aiohttp-сессия для обратных вызовов на gateway (fs/resolve → gateway cwd).
# Создаётся лениво: aiohttp.ClientSession требует запущенный event loop.
_HTTP = None


def _http() -> aiohttp.ClientSession:
	global _HTTP
	if _HTTP is None or _HTTP.closed:
		_HTTP = aiohttp.ClientSession(
			headers={"Authorization": f"Bearer {GW_KEY}"} if GW_KEY else {})
	return _HTTP

_HOP_HEADERS = {
	"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
	"te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}


def _gw_base() -> str:
	return f"http://{GW_HOST}:{GW_PORT}"


def _gw_headers(request: web.Request) -> dict:
	h = {}
	if GW_KEY:
		h["Authorization"] = f"Bearer {GW_KEY}"
	for name in ("X-Hermes-Session-Id", "X-Hermes-Session-Key"):
		if name in request.headers:
			h[name] = request.headers[name]
	return h


@web.middleware
async def _cors_middleware(request: web.Request, handler):
	if request.method == "OPTIONS":
		resp = web.Response(status=204)
	else:
		try:
			resp = await handler(request)
		except asyncio.TimeoutError:
			# Явный перехват таймаута для возврата корректного статуса 504
			resp = web.json_response(
				{"error": {"message": "Gateway timeout (took too long to respond)",
							"type": "gateway_timeout"}}, status=504)
		except aiohttp.ClientError as e:
			resp = web.json_response(
				{"error": {"message": f"Gateway unreachable: {e}",
							"type": "gateway_unreachable"}}, status=502)
		except Exception as e:
			logger.exception("Infinity handler failed: %s", request.path)
			resp = web.json_response(
				{"error": {"message": str(e), "type": "internal_error"}}, status=500)
			
	resp.headers["Access-Control-Allow-Origin"] = "*"
	resp.headers["Access-Control-Allow-Headers"] = (
		"Authorization, Content-Type, X-Hermes-Session-Id, X-Hermes-Session-Key")
	resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
	return resp


async def handle_index(request: web.Request) -> web.Response:
	if HTML_PATH.exists():
		return web.FileResponse(HTML_PATH, headers={"Cache-Control": "no-store"})
	return web.Response(text="<h1>Infinity</h1><p>infinity.html не найден</p>",
						content_type="text/html", status=404)


async def handle_front_health(request: web.Request) -> web.Response:
	return web.json_response({"ok": True, "service": "infinity", "time": time.time()})


async def handle_favicon(request: web.Request) -> web.Response:
	return web.Response(status=204)


async def handle_commands(request: web.Request) -> web.Response:
	try:
		from hermes_cli.commands import COMMAND_REGISTRY, _is_gateway_available
		try:
			from hermes_cli.commands import _resolve_config_gates
			overrides = _resolve_config_gates()
		except Exception:
			swallow.note('web_server.handle_commands:L306')
			overrides = set()
		commands = []
		for cmd in COMMAND_REGISTRY:
			if not _is_gateway_available(cmd, overrides):
				continue
			commands.append({
				"name": f"/{cmd.name}",
				"description": cmd.description,
				"category": cmd.category,
				"args": cmd.args_hint,
				"aliases": [f"/{a}" for a in cmd.aliases],
				"busy_policy": cmd.busy_policy})
		return web.json_response({"object": "infinity.commands", "data": commands})
	except Exception as e:
		logger.exception("Command registry listing failed")
		return web.json_response({"object": "infinity.commands", "data": [], "error": str(e)})


async def _proxy(request: web.Request, *, stream: bool = False) -> web.StreamResponse:
	url = _gw_base() + request.path
	if request.query_string:
		url += "?" + request.query_string
	body = await request.read() if request.body_exists else None

	session = getattr(request.app, "_infinity_session", None)
	if session is None or session.closed:
		# ПОЛНОСТЬЮ ОТКЛЮЧАЕМ ВСЕ ТАЙМАУТЫ (общий, чтение сокета, подключение)
		timeout_config = aiohttp.ClientTimeout(
			total=None,
			sock_read=None,
			connect=None,
			sock_connect=None
		)
		session = aiohttp.ClientSession(timeout=timeout_config)
		request.app._infinity_session = session

	headers = _gw_headers(request)
	if body is not None:
		headers["Content-Type"] = request.headers.get("Content-Type", "application/json")

	async with session.request(request.method, url, data=body, headers=headers) as up:
		if up.status >= 400:
			logger.warning("Gateway %s %s -> %d", request.method, request.path_qs, up.status)
			
		if not stream:
			data = await up.read()
			skip = _HOP_HEADERS | {"access-control-allow-origin",
									"access-control-allow-headers", "access-control-allow-methods"}
			out_headers = {k: v for k, v in up.headers.items() if k.lower() not in skip}
			return web.Response(status=up.status, body=data, headers=out_headers)

		resp = web.StreamResponse(status=up.status, headers={
			"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
			"X-Accel-Buffering": "no",
			**{k: v for k, v in up.headers.items() if k.lower().startswith("x-hermes")}})
		await resp.prepare(request)
		client_gone = False
		try:
			async for chunk in up.content.iter_any():
				if client_gone:
					continue
				try:
					await resp.write(chunk)
				except (ConnectionResetError, ConnectionAbortedError, OSError):
					# Браузер ушёл (смена модуля / перезагрузка). Ход агента на шлюзе
					# не должен из-за этого оборваться: продолжаем читать upstream,
					# пока он сам не закроется. Вернувшись, фронт переподключится и
					# получит весь ход из буфера шлюза.
					swallow.note('web_server._proxy:L371')
					client_gone = True
		except (ConnectionResetError, ConnectionAbortedError, asyncio.CancelledError):
			swallow.note('web_server._proxy:L378')
			pass
		if not client_gone:
			try:
				await resp.write_eof()
			except Exception:
				swallow.note('web_server._proxy:L384')
				pass
		return resp


def _merge_voice_cfg(base: Optional[dict], extra: Any) -> Optional[dict]:
	"""Склеить две конфигурации движка; None, если в итоге нечего слать."""
	out = dict(base or {})
	if isinstance(extra, dict):
		out.update(extra)
	return _voice_api._clean(out) if out.get("url") else None


def _voice_cfg(request: web.Request, kind: str) -> Optional[dict]:
	"""Достать конфигурацию пользовательского движка из заголовка запроса.

	Фронт шлёт её в заголовке X-Voice-Engine-<kind> как JSON. Так конфигурация
	не живёт на сервере вообще: ключ API не покидает браузер, а после
	перезагрузки страницы её просто нет — и голос возвращается к локальному
	Silero/Whisper. Пустой или сломанный заголовок = «движок не задан».
	"""
	raw = request.headers.get("X-Voice-Engine-%s" % kind, "")
	if not raw:
		return None
	try:
		cfg = json.loads(raw)
	except Exception:
		swallow.note('web_server._voice_cfg:L411')
		return None
	return _voice_api._clean(cfg) if isinstance(cfg, dict) and cfg.get("url") else None


async def handle_stt(request: web.Request) -> web.Response:
	audio: bytes = b""
	ctype = request.headers.get("Content-Type", "")
	if ctype.startswith("multipart/"):
		reader = await request.multipart()
		async for part in reader:
			if part.name in ("audio", "file"):
				audio = await part.read(decode=False)
				break
	else:
		audio = await request.read()
	if not audio:
		return web.json_response({"error": "no audio"}, status=400)
	# Пользовательский движок распознавания. Конфигурацию присылает фронт
	# в заголовке: она нигде не хранится и не переживает перезагрузку.
	cfg = _voice_cfg(request, "stt")
	if cfg:
		try:
			text = await asyncio.to_thread(
				_voice_api.stt, audio, cfg,
				request.query.get("lang", "ru"), ctype.split(";")[0] or "audio/webm")
			return web.json_response({"text": text, "engine": "custom"})
		except _voice_api.VoiceEngineError as e:
			logger.warning("Custom STT failed: %s", e)
			return web.json_response({"error": str(e)}, status=502)
	try:
		from frontend import voice
		text = await voice.transcribe(audio,
			lang=request.query.get("lang", "ru"),
			model=(request.query.get("model") or "").strip() or None)
		return web.json_response({"text": text, "engine": "local"})
	except Exception as e:
		logger.exception("STT failed")
		return web.json_response({"error": str(e)}, status=500)


async def handle_tts(request: web.Request) -> web.Response:
	try:
		body = await request.json()
	except Exception:
		swallow.note('web_server.handle_tts:L456')
		body = {}
	text = str(body.get("text", "")).strip()
	if not text:
		return web.json_response({"error": "no text"}, status=400)
	# Пользовательский движок синтеза. Конфигурацию присылает фронт в теле
	# запроса (ключ "engine") либо заголовком — как и для распознавания.
	cfg = _voice_cfg(request, "tts")
	if isinstance(body, dict) and isinstance(body.get("engine"), dict):
		cfg = _merge_voice_cfg(cfg, body["engine"])
	if cfg:
		try:
			data, ctype = await asyncio.to_thread(_voice_api.tts, text, cfg)
			return web.Response(body=data,
			                    content_type=ctype or "audio/mpeg",
			                    headers={"X-Voice-Engine": "custom"})
		except _voice_api.VoiceEngineError as e:
			logger.warning("Custom TTS failed: %s", e)
			return web.json_response({"error": str(e)}, status=502)
	try:
		from frontend import voice
		wav = await voice.synthesize(text)  # уже async (to_thread внутри)
		if not wav:
			return web.json_response({"error": "synthesis failed"}, status=500)
		return web.Response(body=wav, content_type="audio/wav")
	except Exception as e:
		logger.exception("TTS failed")
		return web.json_response({"error": str(e)}, status=500)


async def handle_voice_check(request: web.Request) -> web.Response:
	"""POST /api/voice/check {kind, ...cfg} — проверить пользовательский движок.

	Отдельный маршрут нужен, чтобы кнопка «Проверить» в настройках не жгла
	настоящий голосовой ввод: она шлёт пустой/пробный запрос и просто
	показывает ответ движка.
	"""
	try:
		body = await request.json()
	except Exception:
		swallow.note('web_server.handle_voice_check:L496')
		body = {}
	kind = str(body.get("kind", "tts")).lower()
	if kind not in ("stt", "tts"):
		return web.json_response({"error": "kind must be stt or tts"}, status=400)
	cfg = _merge_voice_cfg(None, body.get("cfg") if isinstance(body.get("cfg"), dict) else body)
	# Без URL слать нечего. Без этой проверки tts пытался собрать адрес из
	# пустой строки и падал с невнятным «unknown url type: '/v1/audio/speech'».
	if not cfg:
		return web.json_response({"ok": False, "kind": kind,
		                          "error": "Укажите адрес движка (URL)"}, status=200)
	try:
		if kind == "stt":
			# Короткий тихий WAV: проверяем, что движок принял и обработал аудио.
			silence = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00" \
			         b"@\x1f\x00\x00\x80>\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
			text = await asyncio.to_thread(_voice_api.stt, silence, cfg, "ru", "audio/wav")
			return web.json_response({"ok": True, "kind": kind, "detail": (text or "(пусто)").strip()})
		data, ctype = await asyncio.to_thread(_voice_api.tts, "Проверка связи", cfg)
		return web.json_response({"ok": True, "kind": kind,
		                          "detail": "%s, %d байт" % (ctype or "audio", len(data))})
	except _voice_api.VoiceEngineError as e:
		return web.json_response({"ok": False, "kind": kind, "error": str(e)}, status=200)


async def handle_stt_status(request: web.Request) -> web.Response:
	"""Диагностика локального Whisper: движок, модель, наличие весов."""
	try:
		from frontend import voice
		return web.json_response(voice.stt_status())
	except Exception as e:
		logger.exception("STT status failed")
		return web.json_response({"error": str(e)}, status=500)


def _hermes_homes() -> list:
	"""Кандидаты папок конфигурации: локальная .hermes-portable (её задаёт
	start_infinity.bat) и HERMES_HOME из окружения — если он указывает в другое место."""
	homes = [Path(__file__).resolve().parent.parent / ".hermes-portable"]
	env = os.getenv("HERMES_HOME")
	if env:
		p = Path(env)
		if p not in homes:
			homes.append(p)
	return homes


def _read_kv(path: Path) -> dict:
	"""Простые KEY=VALUE из .env-файла (кавычки снимаем)."""
	out: dict = {}
	if not path.exists():
		return out
	try:
		for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
			line = line.strip()
			if not line or line.startswith("#") or "=" not in line:
				continue
			k, v = line.split("=", 1)
			out[k.strip()] = v.strip().strip('"').strip("'")
	except Exception as e:  # noqa: BLE001
		logger.warning("direct config: %s не читается: %s", path.name, e)
	return out


def _load_yaml(path: Path) -> dict:
	"""YAML-конфиг как dict; при любой ошибке — пустой dict (лог без падения)."""
	try:
		import yaml
		data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
		return data if isinstance(data, dict) else {}
	except Exception as e:  # noqa: BLE001
		logger.warning("direct config: %s не читается: %s", path, e)
		return {}


async def handle_direct_config(request: web.Request) -> web.Response:
	"""Параметры модели для режима «Диалог» — прямой чат в обход агента Hermes.

	Читает .hermes-portable/config.yaml (model.base_url) и ключ из .env. Браузер
	ходит к модели напрямую: llama.cpp отдаёт CORS для 127.0.0.1, поэтому прокси
	не нужен и агентный цикл (инструменты, память, роутинг) не задействуется.
	"""
	base_url, model, key, api_mode = "", "", "", "chat_completions"
	for home in _hermes_homes():
		cfg = _load_yaml(home / "config.yaml")
		model_cfg = cfg.get("model") or {}
		cand = str(model_cfg.get("base_url") or "").rstrip("/")
		if not cand:
			continue
		env = _read_kv(home / ".env")
		base_url = cand
		model = str(model_cfg.get("default") or model_cfg.get("model") or "")
		key = (env.get("QWEN_API_KEY") or env.get("CUSTOM_API_KEY")
			   or env.get("OPENAI_API_KEY") or "")
		api_mode = str(model_cfg.get("api_mode") or "chat_completions")
		break
	base_url = (os.getenv("INFINITY_DIRECT_BASE_URL") or base_url).rstrip("/")
	return web.json_response({
		"base_url": base_url,
		"model": model,
		"key": key,
		"api_mode": api_mode,
	})

# ---------------------------------------------------------------------------
# КРОН-ЗАДАЧИ АГЕНТА
# Роуты шлюза живут на FastAPI, а эта сборка фронта общается с aiohttp-сервером,
# поэтому /api/cron/* монтируем здесь: вызываем тот же инструмент
# tools.cronjob_tools.cronjob, что и агент, — единый источник правды.
# ---------------------------------------------------------------------------

def _cron_home() -> Optional[Path]:
	"""Каталог .hermes-portable с данными крона (jobs.json, executions.db)."""
	for home in _hermes_homes():
		if (home / "cron").is_dir():
			return home
	return None


def _cron_call(action: str, **kwargs) -> Dict[str, Any]:
	"""Один вызов cronjob-инструмента. Ошибки не пробрасываем наружу."""
	try:
		from tools.cronjob_tools import cronjob
		out = cronjob(action=action, **kwargs)
		if isinstance(out, str):
			return json.loads(out)
		return out if isinstance(out, dict) else {"success": False, "error": "bad result"}
	except Exception as e:  # noqa: BLE001
		logger.warning("cron %s failed: %s", action, e)
		return {"success": False, "error": str(e) or type(e).__name__}


async def handle_cron_list(request: web.Request) -> web.Response:
	"""GET /api/cron/jobs — задачи + краткая сводка (сколько активно/сломано).

	?include_disabled=1 — вместе с задачами на паузе. Без этого параметра
	cron.jobs.list_jobs отбрасывает выключенные, и из интерфейса нельзя было бы
	возобновить задание, которого не видно (так теряли доступ к своим же паузам).
	"""
	inc = request.query.get("include_disabled", "").lower() in ("1", "true", "yes", "on")
	data = _cron_call("list", include_disabled=inc)
	jobs = data.get("jobs") or []
	active = [j for j in jobs if j.get("enabled") and j.get("state") != "completed"]
	return web.json_response({
		"success": bool(data.get("success", True)),
		"jobs": jobs,
		"gateway_running": data.get("gateway_running"),
		"active": len(active),
		"error": data.get("error"),
	})


async def handle_cron_action(request: web.Request) -> web.Response:
	"""POST /api/cron/{action} — create|update|pause|resume|remove|run|cancel."""
	action = request.match_info.get("action", "")
	allowed = {"create", "update", "pause", "resume", "remove", "run", "cancel", "list"}
	if action not in allowed:
		return web.json_response({"error": f"unknown action: {action}"}, status=400)
	try:
		body = await request.json()
	except Exception:
		swallow.note('web_server.handle_cron_action:L657')
		body = {}
	if not isinstance(body, dict):
		return web.json_response({"error": "Expected JSON object"}, status=400)
	# Ключи, которые принимает инструмент: отсекаем мусор из формы.
	allowed_keys = {
		"action", "job_id", "prompt", "schedule", "name", "skills", "skill",
		"model", "provider", "base_url", "deliver", "failure_deliver", "repeat",
		"paused", "paused_reason", "pinned", "workdir", "script", "no_agent",
		"reasoning_effort", "enabled_toolsets",
		# reason — пояснение к cancel/pause в журнале задания.
		"reason",
		# include_disabled нужен списку: без него paused-задания не видны
		# и их нечем возобновить из интерфейса.
		"include_disabled",
	}
	# workdir="" — штатный способ снять привязку задания к папке (update_job
	# трактует пустое значение как clear), поэтому его не выбрасываем.
	payload = {k: v for k, v in body.items()
		  if k in allowed_keys and (v not in ("", None) or (k == "workdir" and v == ""))}
	# action передаём позиционным аргументом _cron_call, поэтому из payload
	# убираем — иначе будет «multiple values for argument».
	payload.pop("action", None)
	if action != "create" and not payload.get("job_id"):
		return web.json_response({"error": "job_id is required"}, status=400)
	if action == "create" and not (payload.get("schedule") and payload.get("prompt")):
		return web.json_response({"error": "schedule and prompt are required"}, status=400)
	data = _cron_call(action, **payload)
	return web.json_response(data, status=200 if data.get("success") else 400)


async def handle_cron_runs(request: web.Request) -> web.Response:
	"""GET /api/cron/runs — история выполнений и инциденты из executions.db."""
	home = _cron_home()
	if home is None:
		return web.json_response({"success": False, "error": "каталог cron не найден", "runs": [], "incidents": []})
	db = home / "cron" / "executions.db"
	if not db.is_file():
		return web.json_response({"success": True, "runs": [], "incidents": []})
	job_id = request.query.get("job_id") or ""
	limit = max(1, min(int(request.query.get("limit") or 30), 200))
	names: Dict[str, str] = {}
	try:
		import json as _json
		jobs_file = home / "cron" / "jobs.json"
		if jobs_file.is_file():
			for j in (_json.loads(jobs_file.read_text(encoding="utf-8")) or {}).get("jobs") or []:
				if j.get("id"):
					names[j["id"]] = str(j.get("name") or j["id"])
	except Exception:  # noqa: BLE001
		swallow.note('web_server.handle_cron_runs:L705')
		names = {}
	runs: List[Dict[str, Any]] = []
	incidents: List[Dict[str, Any]] = []
	try:
		import sqlite3
		conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
		conn.row_factory = sqlite3.Row
		where = " WHERE job_id = ?" if job_id else ""
		args: List[Any] = [job_id] if job_id else []
		for r in conn.execute(
			f"SELECT job_id, status, started_at, finished_at, error, delivery_outcome "
			f"FROM executions{where} ORDER BY COALESCE(started_at,'') DESC LIMIT ?",
			(*args, limit),
		):
			runs.append({
				"job_id": r["job_id"],
				"job_name": names.get(r["job_id"], r["job_id"]),
				"status": r["status"],
				"started_at": r["started_at"],
				"finished_at": r["finished_at"],
				"error": r["error"],
				"delivery": r["delivery_outcome"],
			})
		for r in conn.execute(
			"SELECT job_id, state, failure_type, first_seen_at, last_seen_at, error "
			"FROM cron_incidents ORDER BY last_seen_at DESC LIMIT 20"
		):
			incidents.append({
				"job_id": r["job_id"],
				"job_name": names.get(r["job_id"], r["job_id"]),
				"state": r["state"],
				"failure_type": r["failure_type"],
				"first_seen_at": r["first_seen_at"],
				"last_seen_at": r["last_seen_at"],
				"error": r["error"],
			})
		conn.close()
	except Exception as e:  # noqa: BLE001
		logger.warning("cron history read failed: %s", e)
		return web.json_response({"success": False, "error": str(e), "runs": [], "incidents": []})
	return web.json_response({"success": True, "runs": runs, "incidents": incidents})

# ---------------------------------------------------------------------------
# НАСТРОЙКИ АГЕНТА (config.yaml шлюза)
# Вкладка «Настройки → Агент» показывает часть параметров Hermes.
# Отдаём только значения по «белому списку»: браузер не может записать любые пути в конфиг — иначе любая
# конфигурация станет не доступной через управление.
# ---------------------------------------------------------------------------

# ключ -> (подпись, тип, допустимые значения | None)
AGENT_CFG_FIELDS = [
	("model.provider", "Провайдер модели", "text", None),
	("model.api_mode", "Режим обмена", "choice", ["chat_completions", "responses", "anthropic"]),
	("model.base_url", "Адрес сервера модели", "text", None),
	("agent.service_tier", "Уровень обслуживания", "choice", ["auto", "default", "flex", "scale"]),
	("agent.reasoning_effort", "Глубина размышления", "choice", ["minimal", "low", "medium", "high"]),
	("delegation.reasoning_effort", "Глубина размышления делегации", "choice", ["minimal", "low", "medium", "high"]),
	# Варианты не фиксированы: стратегии живут ключами delegation.backends в config.yaml,
	# поэтому список дополняется из файла в _cfg_field_options.
	("delegation.backend", "Стратегия делегации", "choice", ["default"]),
	("compression.threshold", "Порог сжатия контекста", "float", None),
	("memory.provider", "Провайдер памяти", "text", None),
	("memory.memory_char_limit", "Лимит памяти агента", "int", None),
	("memory.user_char_limit", "Лимит памяти пользователя", "int", None),
	("approvals.mode", "Режим подтверждения", "choice", ["off", "on", "suggest"]),
	("sessions.auto_prune", "Автоочистка старых сессий", "bool", None),
	("sessions.retention_days", "Хранить сессии (дней)", "int", None),
	("sessions.auto_archive", "Автоархив старых сессий", "bool", None),
	("sessions.auto_archive_days", "Архив после скольких дней", "int", None),
	("browser.backend", "Движок браузера", "text", None),
	("computer_use.backend", "Движок управления компютером", "text", None),
	("display.pet.enabled", "Пимотец (виджет в интерфейсе)", "bool", None),
]

AGENT_CFG_KEYS = {k for k, _n, _t, _o in AGENT_CFG_FIELDS}


def _cfg_get(data: Dict[str, Any], dotted: str, default: Any = None) -> Any:
	"""Обращение по точечной ключу: 'agent.pet.enabled' -> data['agent']['pet']['enabled']."""
	cur: Any = data
	for part in dotted.split("."):
		if not isinstance(cur, dict) or part not in cur:
			return default
		cur = cur[part]
	return cur


def _cfg_set(data: Dict[str, Any], dotted: str, value: Any) -> None:
	"""Запись по точечному ключу, создав недущающие вложения."""
	parts = dotted.split(".")
	cur = data
	for part in parts[:-1]:
		nxt = cur.get(part)
		if not isinstance(nxt, dict):
			nxt = {}
			cur[part] = nxt
		cur = nxt
	cur[parts[-1]] = value


def _cfg_coerce(raw: Any, kind: str, options: Optional[List[str]]) -> Any:
	"""Приводит значение к типу поля; неверный тип — ошибка (значение не подходит)."""
	if kind == "bool":
		if isinstance(raw, bool):
			return raw
		return str(raw).strip().lower() in ("1", "true", "yes", "on", "да")
	if kind == "int":
		try:
			return int(str(raw).strip())
		except (TypeError, ValueError):
			raise ValueError(f"не число: {raw}")
	if kind == "float":
		try:
			return float(str(raw).strip())
		except (TypeError, ValueError):
			raise ValueError(f"не число: {raw}")
	if kind == "choice":
		s = str(raw).strip()
		if options and s not in options:
			raise ValueError(f"допустимо: {s} ({', '.join(options)})")
		return s
	return str(raw)


def _cfg_field_options(key: str, options: Optional[List[str]], data: Dict[str, Any]) -> Optional[List[str]]:
	"""Допустимые значения поля. Для delegation.backend их нельзя зашить в константу:
	стратегии — это ключи delegation.backends в config.yaml, и пользователь добавляет их сам.
	Без этого список выбора показывал бы только «default», а сохранение отвергало свою стратегию."""
	if key == "delegation.backend":
		extra = _cfg_get(data, "delegation.backends", {})
		names = ["default"]
		if isinstance(extra, dict):
			names.extend(sorted(str(n) for n in extra))
		return names
	return options


def _agent_config_file() -> Optional[Path]:
	for home in _hermes_homes():
		if (home / "config.yaml").is_file():
			return home / "config.yaml"
	return None


async def handle_agent_config(request: web.Request) -> web.Response:
	"""GET /api/agent-config — текущие значения шлюза из config.yaml."""
	path = _agent_config_file()
	if path is None:
		return web.json_response({"success": False, "error": "config.yaml не найден", "values": {}, "fields": []})
	data = _load_yaml(path)
	values = {k: _cfg_get(data, k) for k, _n, _t, _o in AGENT_CFG_FIELDS}
	fields = [{"key": k, "label": n, "type": ty,
	           "options": _cfg_field_options(k, o, data)} for k, n, ty, o in AGENT_CFG_FIELDS]
	return web.json_response({"success": True, "values": values, "fields": fields, "path": str(path)})


async def handle_agent_config_save(request: web.Request) -> web.Response:
	"""POST /api/agent-config {values:{путь: значение}} — запись выбранные параметры в config.yaml.

	Принимаем видел, пишем копию файла, и пишем перезаписываем только обязанленные ключи.
	"""
	path = _agent_config_file()
	if path is None:
		return web.json_response({"success": False, "error": "config.yaml не найден"}, status=404)
	try:
		body = await request.json()
	except Exception:
		return web.json_response({"error": "Expected JSON body"}, status=400)
	values = body.get("values") if isinstance(body, dict) else None
	if not isinstance(values, dict) or not values:
		return web.json_response({"error": "values is required"}, status=400)
	rejected = sorted(set(values) - AGENT_CFG_KEYS)
	if rejected:
		return web.json_response({"error": f"недопустимые параметры: {', '.join(rejected)}"}, status=400)
	data = _load_yaml(path)
	clean: Dict[str, Any] = {}
	for key, raw in values.items():
		kind = next(ty for k, _n, ty, _o in AGENT_CFG_FIELDS if k == key)
		opts = next(op for k, _n, _ty, op in AGENT_CFG_FIELDS if k == key)
		opts = _cfg_field_options(key, opts, data)
		try:
			clean[key] = _cfg_coerce(raw, kind, opts)
		except ValueError as e:
			return web.json_response({"error": f"{key}: {e}"}, status=400)
	for key, value in clean.items():
		_cfg_set(data, key, value)
	try:
		import yaml
		text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
	except Exception as e:  # noqa: BLE001
		return web.json_response({"error": f"YAML: {e}"}, status=500)
	bak = path.with_suffix(".yaml.bak")
	try:
		if path.is_file():
			bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
		tmp = path.with_name(f".{path.name}.cfg-tmp-{os.getpid()}")
		tmp.write_text(text, encoding="utf-8")
		os.replace(tmp, path)
	except OSError as e:
		return web.json_response({"error": f"не удалось сохранить: {e}"}, status=500)
	return web.json_response({"success": True, "updated": sorted(clean), "backup": str(bak)})


# ---------------------------------------------------------------------------
# ПЕРЕЗАПУСК ШЛЮЗА
# Настройки из config.yaml применяются только после рестарта. Шлюз — отдельный
# процесс, который поднимает runner.py; сам фронтенд перезапускать не нужно,
# поэтому страница не перезагрузится и открытые вкладки останутся живы.
# ---------------------------------------------------------------------------

# Флаг против параллельных перезапусков: два запроса подряд погасили бы шлюз
# дважды, и второй стартовал бы поверх ещё не умершего.
_gw_restart_busy = False


def _gateway_owner_proc():
	"""Popen шлюза, если он запущен ИЗ ЭТОГО процесса, иначе None.

	При запуске через `python -m frontend.runner` модуль исполняется как
	__main__, и в sys.modules лежит именно этот экземпляр. Импортировать
	frontend.runner заново нельзя: импорт создал бы второй экземпляр с
	_gateway_proc = None, и мы бы решили, что шлюз чужой.
	"""
	mod = sys.modules.get("__main__")
	proc = getattr(mod, "_gateway_proc", None) if mod else None
	if proc is not None:
		return mod, proc
	return None, None


async def _gw_health_ok(timeout: float) -> bool:
	"""Ждём /health шлюза. Свой вариант вместо runner._wait_health: тот
	блокирующий (urllib + time.sleep) и в event loop встал бы."""
	deadline = time.time() + timeout
	url = f"{_gw_base()}/health"
	while time.time() < deadline:
		try:
			async with _http().get(url, timeout=aiohttp.ClientTimeout(total=3)) as r:
				if r.status == 200:
					return True
		except Exception:  # noqa: BLE001
			swallow.note('web_server._gw_health_ok:L927')
			pass
		await asyncio.sleep(1)
	return False


async def _stop_gateway_owned() -> Optional[str]:
	"""Остановить шлюз. None — остановлен/уже стоял, иначе текст ошибки.

	Сначала свой Popen (когда web UI поднят через frontend.runner), иначе —
	через main.py: в desktop-сборке шлюз принадлежит основному процессу.
	"""
	mod, proc = _gateway_owner_proc()
	if proc is None:
		_, err = await _engine_control("stop", timeout=120)
		if err:
			return "Шлюз не остановлен: " + err
		for _ in range(40):
			if not await _gw_health_now():
				return None
			await asyncio.sleep(0.5)
		return "Шлюз не остановился — операция отменена"
	if proc.poll() is not None:
		return None
	try:
		proc.terminate()
		proc.wait(timeout=20)
	except subprocess.TimeoutExpired:
		swallow.note('web_server._stop_gateway_owned:L1030')
		proc.kill()
		try:
			proc.wait(timeout=5)
		except subprocess.TimeoutExpired:
			return "Шлюз не завершился — операция отменена"
	free = getattr(mod, "_port_free", None)
	for _ in range(40):
		if callable(free) and free(GW_PORT):
			return None
		await asyncio.sleep(0.5)
	return f"Порт {GW_PORT} всё ещё занят — операция отменена"


async def handle_gateway_restart(request: web.Request) -> web.Response:
	"""POST /api/gateway/restart — перезапуск шлюза с применением config.yaml.

	Если шлюз остановлен (после обновления Hermes), та же кнопка запускает его.
	"""
	global _gw_restart_busy
	if _gw_restart_busy:
		return web.json_response({"error": "Перезапуск уже идёт"}, status=409)
	mod, proc = _gateway_owner_proc()
	if proc is None:
		# Desktop-сборка: шлюзом владеет main.py. Просим его остановить
		# (чужой процесс по-прежнему не трогаем — main.py проверяет владельца
		# порта) и запускаем заново.
		_gw_restart_busy = True
		try:
			was_running = await _gw_health_now()
			_, err = await _engine_control("stop", timeout=120)
			if err:
				return web.json_response({"error": "Не остановился: " + err}, status=409)
			for _ in range(40):
				if not await _gw_health_now():
					break
				await asyncio.sleep(0.5)
			data, err = await _engine_control("start", timeout=200)
			if err:
				return web.json_response({"error": "Не запустился: " + err}, status=500)
			if not await _gw_health_ok(30):
				return web.json_response(
					{"error": "Шлюз запущен, но не отвечает. Смотрите "
					          ".hermes-portable/logs/"}, status=500)
			return web.json_response({"success": True,
			                          "pid": (data or {}).get("pid"),
			                          "started": not was_running})
		finally:
			_gw_restart_busy = False
	_gw_restart_busy = True
	try:
		started = proc.poll() is not None
		if started:
			logger.info("gateway: запуск после остановки (pid=%s)", proc.pid)
		else:
			logger.info("gateway: перезапуск по запросу из интерфейса (pid=%s)", proc.pid)
			err = await _stop_gateway_owned()
			if err:
				return web.json_response({"error": err}, status=409)
		# Порт освобождается не мгновенно: иначе новый процесс упадёт с
		# «address already in use».
		free = getattr(mod, "_port_free", None)
		for _ in range(60):
			if free(GW_PORT) if callable(free) else False:
				break
			await asyncio.sleep(0.5)
		new = mod.start_gateway()
		if new is None:
			return web.json_response(
				{"error": "Шлюз не стартовал: порт всё ещё занят. Проверьте "
				           "процессы на порту %d." % GW_PORT}, status=500)
		if not await _gw_health_ok(90):
			return web.json_response(
				{"error": "Шлюз запущен, но не отвечает. Смотрите "
				           ".hermes-portable/logs/"}, status=500)
		return web.json_response({"success": True, "pid": new.pid, "started": started})
	except Exception as e:  # noqa: BLE001
		logger.exception("gateway restart failed")
		return web.json_response({"error": f"не удалось: {e}"}, status=500)
	finally:
		_gw_restart_busy = False


# ---------------------------------------------------------------------------
# ОБНОВЛЕНИЕ HERMES С GITHUB RELEASES
# Логика — во frontend/hermes_update.py (её не затирают обновления backend).
# Здесь только HTTP-обвязка: loopback, проверка занятости шлюза, остановка
# перед заменой файлов и ручной запуск после — автоматически не поднимаем.
# ---------------------------------------------------------------------------
from . import hermes_update as _hermes_update  # noqa: E402


def _hu_forbidden(request: web.Request) -> Optional[web.Response]:
	if not _is_loopback(request):
		return web.json_response({"error": "только с этой машины"}, status=403)
	return None


async def _gw_health_now() -> bool:
	try:
		async with _http().get(f"{_gw_base()}/health",
		                       timeout=aiohttp.ClientTimeout(total=2)) as r:
			return r.status == 200
	except Exception:  # noqa: BLE001
		swallow.note('web_server._gw_health_now:L1102')
		return False


def _main_base() -> str:
	"""Адрес основного приложения (main.py), которое владеет шлюзом.

	В desktop-сборке web UI агента — отдельный процесс, и его Popen шлюза
	пуст. Остановка/запуск идут через main.py: у него настоящий _gw_proc.
	"""
	port = os.getenv("INFINITY_MAIN_PORT") or "8281"
	mount = (os.getenv("AGENTAI_INT_MOUNT") or os.getenv("HERMES_INT_MOUNT")
	         or "/agentai").rstrip("/")
	return f"http://127.0.0.1:{port}{mount}"


async def _engine_control(action: str, timeout: float = 150.0) -> tuple:
	"""POST {main}/api/agent-engine/{stop|start}. Возвращает (data, ошибка)."""
	url = f"{_main_base()}/api/agent-engine/{action}"
	try:
		async with _http().post(url, headers={"X-Infinity-Engine": "1"},
		                        timeout=aiohttp.ClientTimeout(total=timeout)) as r:
			data = await r.json(content_type=None)
			status = r.status
	except Exception as e:  # noqa: BLE001
		swallow.note('web_server._engine_control:L1148')
		return None, f"основное приложение не ответило: {e}"
	if not isinstance(data, dict):
		return None, f"неожиданный ответ основного приложения (HTTP {status})"
	if status != 200 or not data.get("ok"):
		return None, str(data.get("error") or f"HTTP {status}")
	return data, ""


async def _gw_idle_state() -> tuple:
	"""Можно ли менять файлы ядра: шлюз не должен выполнять ход."""
	try:
		async with _http().get(f"{_gw_base()}/health/detailed",
		                       timeout=aiohttp.ClientTimeout(total=5)) as r:
			if r.status != 200:
				return False, f"шлюз ответил {r.status} — операция отменена"
			data = await r.json()
	except Exception:  # noqa: BLE001
		swallow.note('web_server._gw_idle_state:L1113')
		return True, ""
	active = int(((data.get("api_server") or {}).get("active_runs")) or 0)
	if data.get("gateway_busy") or active:
		return False, "агент выполняет ход — дождитесь завершения"
	return True, ""


async def handle_hermes_update_state(request: web.Request) -> web.Response:
	err = _hu_forbidden(request)
	if err:
		return err
	data = _hermes_update.snapshot()
	mod, proc = _gateway_owner_proc()
	data["gateway_owned"] = proc is not None
	data["gateway_running"] = (proc is not None and proc.poll() is None) or await _gw_health_now()
	return web.json_response({"success": True, **data})


async def handle_hermes_update_config(request: web.Request) -> web.Response:
	err = _hu_forbidden(request)
	if err:
		return err
	try:
		body = await request.json()
	except Exception:  # noqa: BLE001
		swallow.note('web_server.handle_hermes_update_config:L1137')
		return web.json_response({"error": "Expected JSON body"}, status=400)
	if not isinstance(body, dict):
		return web.json_response({"error": "Expected JSON object"}, status=400)
	try:
		cfg = _hermes_update.set_config({
			k: body[k] for k in ("enabled", "auto_install", "check_interval_h") if k in body
		})
	except ValueError as e:
		return web.json_response({"error": str(e)}, status=400)
	return web.json_response({"success": True, "config": cfg})


async def handle_hermes_update_check(request: web.Request) -> web.Response:
	err = _hu_forbidden(request)
	if err:
		return err
	if not _hermes_update.get_config().get("enabled"):
		return web.json_response({"error": "Обновления выключены рубильником"}, status=400)
	try:
		res = await asyncio.to_thread(_hermes_update.check, True)
	except Exception as e:  # noqa: BLE001
		return web.json_response({"error": str(e) or type(e).__name__}, status=502)
	return web.json_response({"success": True, **res})


async def handle_hermes_update_download(request: web.Request) -> web.Response:
	err = _hu_forbidden(request)
	if err:
		return err
	tag = None
	try:
		body = await request.json()
		if isinstance(body, dict):
			tag = str(body.get("tag") or "").strip() or None
	except Exception:  # noqa: BLE001
		swallow.note('web_server.handle_hermes_update_download:L1167')
	try:
		res = await asyncio.to_thread(_hermes_update.download, tag)
	except Exception as e:  # noqa: BLE001
		return web.json_response({"error": str(e) or type(e).__name__}, status=400)
	return web.json_response({"success": True, **res})


async def _hu_prepare_install() -> Optional[str]:
	cfg = _hermes_update.get_config()
	if not cfg.get("enabled"):
		return "Обновления выключены рубильником"
	if not _hermes_update.snapshot().get("staged"):
		return "Нет скачанной версии — сначала «Скачать»"
	idle, why = await _gw_idle_state()
	if not idle:
		return why
	return await _stop_gateway_owned()


async def handle_hermes_update_install(request: web.Request) -> web.Response:
	err = _hu_forbidden(request)
	if err:
		return err
	force = False
	try:
		body = await request.json()
		force = bool(body.get("force")) if isinstance(body, dict) else False
	except Exception:  # noqa: BLE001
		swallow.note('web_server.handle_hermes_update_install:L1192')
	stop_err = await _hu_prepare_install()
	if stop_err:
		return web.json_response({"error": stop_err}, status=409)
	try:
		res = await asyncio.to_thread(_hermes_update.install_staged, force)
	except Exception as e:  # noqa: BLE001
		return web.json_response({"error": str(e) or type(e).__name__,
		                          "gateway_running": False}, status=500)
	return web.json_response({"success": True, **res, "gateway_running": False})


async def handle_hermes_update_rollback(request: web.Request) -> web.Response:
	err = _hu_forbidden(request)
	if err:
		return err
	try:
		body = await request.json()
		name = str((body or {}).get("name") or "")
	except Exception:  # noqa: BLE001
		swallow.note('web_server.handle_hermes_update_rollback:L1209')
		return web.json_response({"error": "Expected JSON body"}, status=400)
	if not _hermes_update.get_config().get("enabled"):
		return web.json_response({"error": "Обновления выключены рубильником"}, status=400)
	stop_err = await _stop_gateway_owned()
	if stop_err:
		return web.json_response({"error": stop_err}, status=409)
	try:
		res = await asyncio.to_thread(_hermes_update.rollback, name)
	except Exception as e:  # noqa: BLE001
		return web.json_response({"error": str(e) or type(e).__name__}, status=400)
	return web.json_response({"success": True, **res, "gateway_running": False})


_HU_TASK = None


async def _hermes_update_scheduler() -> None:
	"""Автопроверка/автоустановка. Рубильник и режим — в hermes_update.json."""
	await asyncio.sleep(90)  # не мешаем холодному старту приложения
	while True:
		try:
			cfg = _hermes_update.get_config()
			if cfg.get("enabled") and not _hermes_update.is_busy():
				interval = float(cfg.get("check_interval_h") or 24) * 3600
				if time.time() - float(cfg.get("last_check") or 0) >= interval:
					res = await asyncio.to_thread(_hermes_update.check, True)
					if res.get("newer"):
						tag = str((res.get("latest") or {}).get("tag") or "")
						_hermes_update.log(f"найден релиз {tag}")
						if cfg.get("auto_install") and tag:
							st = _hermes_update.snapshot().get("staged")
							if not st or st.get("tag") != tag:
								await asyncio.to_thread(_hermes_update.download, tag)
							idle, why = await _gw_idle_state()
							if not idle:
								_hermes_update.log(f"автоустановка отложена: {why}")
							else:
								stop_err = await _stop_gateway_owned()
								if stop_err:
									_hermes_update.log(f"автоустановка отменена: {stop_err}")
								else:
									await asyncio.to_thread(_hermes_update.install_staged)
		except Exception as e:  # noqa: BLE001
			swallow.note('web_server._hermes_update_scheduler:L1252')
			try:
				_hermes_update.log(f"планировщик: {type(e).__name__}: {e}")
			except Exception:  # noqa: BLE001
				pass
		await asyncio.sleep(1800)


async def _start_hermes_update_task(app: web.Application) -> None:
	global _HU_TASK
	if _HU_TASK is None or _HU_TASK.done():
		_HU_TASK = asyncio.create_task(_hermes_update_scheduler())


def _resolve_typed_any(root_dir: str, name: str, types: Optional[list],
		kind: str = "any") -> Optional[str]:
	"""Ищем файл или папку по имени в типичных местах.

	Возвращает абсолютный путь или None. Никогда не бросает исключений.
	``kind``: ``file`` | ``dir`` | ``any`` (по умолчанию).
	"""
	name = name.strip().strip('"').strip()
	if not name:
		return None
	if kind not in ("file", "dir", "any"):
		kind = "any"

	def _ok(p: str) -> bool:
		try:
			if kind == "dir":
				return os.path.isdir(p)
			if kind == "file":
				return os.path.isfile(p)
			return os.path.exists(p)
		except OSError:
			swallow.note('web_server._ok:L1007')
			return False

	cands = []
	if len(name) > 1 and (name[1] == ":" or name.startswith("\\\\")):
		cands.append(name)
	cands.extend([
		os.path.join(root_dir or "", name),
		os.path.join(os.path.expanduser("~"), "Desktop", name),
		os.path.join(os.path.expanduser("~"), "Downloads", name),
		os.path.join(os.path.expanduser("~"), "Documents", name),
		os.path.join(os.path.expanduser("~"), "Pictures", name),
		os.path.join(os.path.expanduser("~"), name),
	])
	exts = [t if t.startswith(".") else "." + t for t in (types or [])]
	search_dirs = [d for d in (root_dir or "",
		os.path.join(os.path.expanduser("~"), "Desktop"),
		os.path.join(os.path.expanduser("~"), "Downloads"),
		os.path.join(os.path.expanduser("~"), "Documents"),
		os.path.join(os.path.expanduser("~"), "Pictures")) if d]
	if exts and kind != "dir":
		# «resume» → «resume.pdf» в тех же папках
		for d in search_dirs:
			for ext in exts:
				cands.append(os.path.join(d, name + ext))
	if kind != "dir":
		# последний резорт: сканируем папки (по расширению для файлов, по имени для любых)
		for d in search_dirs:
			try:
				for n in os.listdir(d):
					if name.lower() in n.lower() and (not exts or os.path.splitext(n)[1].lower() in exts):
						p = os.path.join(d, n)
						if _ok(p):
							return os.path.abspath(p)
			except OSError:
				swallow.note('web_server._resolve_typed_any:L1042')
				continue
	else:
		# папка: ищем совпадение по имени среди подкаталогов типичных мест
		for d in search_dirs:
			try:
				for n in os.listdir(d):
					p = os.path.join(d, n)
					if n.lower() == name.lower() and os.path.isdir(p):
						return os.path.abspath(p)
			except OSError:
				swallow.note('web_server._resolve_typed_any:L1053')
				continue
	for c in cands:
		try:
			if c and _ok(c):
				return os.path.abspath(c)
		except OSError:
			swallow.note('web_server._resolve_typed_any:L1060')
			continue
	return None


async def handle_fs_resolve(request: web.Request) -> web.Response:
	"""Автопоиск полных путей для файлов, прикреплённых из браузера.

	Браузеры не отдают абсолютный путь из <input type="file"> и drag&drop,
	поэтому бэкенд ищет файл по имени в рабочей директории проекта и типичных
	пользовательских папках. Путь нужен только агенту — в чате он не показывается.
	"""
	try:
		body = await request.json()
	except Exception:
		return web.json_response({"error": "invalid json"}, status=400)
	names = body.get("names") or []
	if isinstance(names, str):
		names = [names]
	kind = str(body.get("kind") or "any").strip().lower() or "any"
	root = str(body.get("root") or "").strip()
	if root and not os.path.isdir(root):
		root = ""
	if not root:
		# Рабочая директория активной сессии на gateway — лучший кандидат для поиска
		try:
			async with _http().get(
					_gw_base() + "/api/gateway/status",
					headers=_gw_headers(request)) as r:
				if r.status == 200:
					st = await r.json(content_type=None)
					cwd = (st.get("data") or {}).get("cwd") or st.get("cwd")
					if cwd and os.path.isdir(str(cwd)):
						root = str(cwd)
		except Exception:
			logger.debug("gateway cwd fetch failed", exc_info=True)
	types = body.get("types") if isinstance(body.get("types"), list) else []
	out = {}
	for raw in names[:20]:
		name = str(raw or "").strip()
		if not name:
			continue
		out[name] = _resolve_typed_any(root, name, types, kind=kind)
	return web.json_response({"paths": out})


async def handle_fs_pick(request: web.Request) -> web.Response:
	"""Резервный вариант: нативный диалог выбора файлов (tkinter).

	Запускается только если автопоиск не нашёл файл. Открывается короткоживущее
	скрытое окно; возвращает абсолютные пути выбранных файлов.
	"""
	try:
		body = await request.json()
	except Exception:
		swallow.note('web_server.handle_fs_pick:L1115')
		body = {}
	title = str(body.get("title") or "Выберите файлы для агента")
	types = body.get("types") if isinstance(body.get("types"), list) else []
	patterns = [("Все файлы", "*.*")] if not types else [("*." + t.lstrip("."), "*." + t.lstrip(".")) for t in types] + [("Все файлы", "*.*")]
	paths = []
	try:
		await asyncio.get_running_loop().run_in_executor(None, _pick_sync, title, patterns, paths)
	except Exception as e:
		logger.exception("fs/pick dialog failed")
		return web.json_response({"error": str(e)}, status=500)
	return web.json_response({"paths": paths})


def _pick_sync(title: str, patterns: list, out: list) -> None:
	"""Диалог выбора выполняется в отдельном потоке и в отдельном процессе
	tkinter (повторная инициализация Tk в одном процессе ломает очередь событий)."""
	try:
		if getattr(sys, "frozen", False):
			py = sys.executable
		else:
			py = sys.executable or "python"
		code = (
			"import tkinter as tk, tkinter.filedialog as fd, json, sys\n"
			"r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
			"paths = fd.askopenfilenames(parent=r, title=%r, filetypes=%r)\n"
			"r.destroy()\n"
			"sys.stdout.write(json.dumps(list(paths)))\n" % (title, patterns)
		)
		res = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=300)
		data = json.loads(res.stdout.strip() or "[]")
		if isinstance(data, list):
			out.extend([str(p) for p in data if p])
	except Exception:
		logger.exception("tkinter pick subprocess failed")


# ============================================================
#  ФАЙЛОВЫЙ API НАШЕГО СЕРВЕРА (/api/fs/list, /api/fs/read-text)
#  Панель «Файлы проекта» и превью вложений обращаются сюда напрямую —
#  роуты зарегистрированы ДО catch-all прокси, поэтому запросы не уходят
#  на gateway (где в этой сборке /api/fs/* отсутствуют → был 404).
#  ============================================================

# Куда искать файл, если путь не открылся как есть: рабочая папка
# шлюза, папки проектов и сам каталог приложения. Агент часто
# упоминает «style.css» или путь относительно проекта — раньше это
# давало ENOENT на только что созданном файле.
_FS_SEARCH_ROOTS: List[Path] = []


def _fs_search_roots() -> List[Path]:
    """Папки для поиска файла по имени."""
    if _FS_SEARCH_ROOTS:
        return _FS_SEARCH_ROOTS
    roots: List[Path] = []
    env = os.getenv("INFINITY_FS_ROOTS")
    if env:
        roots.extend(Path(p) for p in env.split(os.pathsep) if p.strip())
    roots.append(BASE_DIR)
    roots.append(BASE_DIR.parent)
    seen, out = set(), []
    for r in roots:
        try:
            rp = r.resolve()
        except OSError:
            swallow.note('web_server._fs_search_roots:L1181')
            continue
        if rp in seen or not rp.is_dir():
            continue
        seen.add(rp)
        out.append(rp)
    _FS_SEARCH_ROOTS.extend(out)
    return _FS_SEARCH_ROOTS


def _fs_resolve(raw: str) -> Optional[Path]:
    """Найти файл: сначала как есть, затем по имени в рабочих папках.

    Глубину поиска ограничиваем (по умолчанию 4) и пропускаем тяжёлые
    каталоги, иначе запрос по имени «index.js» сканировал бы пол-диска.
    """
    p = Path(raw)
    try:
        if p.is_file():
            return p
    except OSError:
        swallow.note('web_server._fs_resolve:L1202')
        pass
    name = p.name
    if not name:
        return None
    max_depth = int(os.getenv("INFINITY_FS_SEARCH_DEPTH", "4"))
    skip = ("node_modules", "venv", "__pycache__", ".git", "models", "dist", "build")
    for root in _fs_search_roots():
        stack = [(root, 0)]
        while stack:
            cur, depth = stack.pop()
            try:
                entries = list(os.scandir(cur))
            except OSError:
                swallow.note('web_server._fs_resolve:L1216')
                continue
            for e in entries:
                try:
                    if e.is_file() and e.name == name:
                        return Path(e.path)
                    if e.is_dir() and not e.name.startswith(".") \
                            and depth + 1 < max_depth and e.name not in skip:
                        stack.append((Path(e.path), depth + 1))
                except OSError:
                    swallow.note('web_server._fs_resolve:L1226')
                    continue
    return None



def _fs_cwd_candidate(raw: str) -> Optional[Path]:
	"""Существующий каталог из настройки ``terminal.cwd`` или пустая строка."""
	raw = (raw or "").strip().strip('"')
	if not raw or raw in {".", "auto", "cwd"}:
		return None
	try:
		p = Path(os.path.expanduser(raw))
		return p if p.is_dir() else None
	except OSError:
		swallow.note('web_server._fs_cwd_candidate:L1241')
		return None


def _fs_default_cwd() -> str:
	"""Рабочая директория агента — то же, что отдаёт шлюз.

	Раньше этот адрес проксировался на gateway, а в установленной сборке
	маршрута ``/api/fs/default-cwd`` там нет: панель файлов при каждом
	открытии получала 404 и молча работала без рабочей папки (в консоли —
	предупреждение, в интерфейсе — пустой путь).

	Порядок: явный cwd из состояния рабочей области → TERMINAL_CWD →
	config.yaml (terminal.cwd) → каталог, из которого запущен UI-сервер
	(так же поступает и шлюз).
	"""
	try:
		state = load_workspace_state()
	except Exception:
		swallow.note('web_server._fs_default_cwd:L1260')
		state = {}
	for key in ("cwd", "gatewayCwd"):
		found = _fs_cwd_candidate(str(state.get(key) or ""))
		if found is not None:
			return str(found)
	found = _fs_cwd_candidate(os.getenv("TERMINAL_CWD", ""))
	if found is not None:
		return str(found)
	try:
		import yaml
		cfg = yaml.safe_load((AGENT_FILES_DIR / "config.yaml").read_text(
			encoding="utf-8")) or {}
		found = _fs_cwd_candidate(
			str((cfg.get("terminal") or {}).get("cwd") or ""))
		if found is not None:
			return str(found)
	except Exception:
		swallow.note('web_server._fs_default_cwd:L1278')
		pass
	return str(Path.cwd())


def _fs_git_branch(cwd: str) -> str:
	"""Текущая ветка git или пустая строка (быстро, без падений)."""
	creationflags = 0
	try:
		from hermes_cli._subprocess_compat import windows_hide_flags
		creationflags = windows_hide_flags()
	except Exception:
		swallow.note('web_server._fs_git_branch:L1290')
		pass
	try:
		p = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
						   cwd=cwd, capture_output=True, timeout=2,
						   creationflags=creationflags)
	except (OSError, subprocess.SubprocessError):
		swallow.note('web_server._fs_git_branch:L1297')
		return ""
	if p.returncode != 0:
		return ""
	return _decode_console(p.stdout or b"").strip()


async def handle_fs_default_cwd(request: web.Request) -> web.Response:
	"""GET /api/fs/default-cwd → {cwd, branch}.

	Фронтенд (API.fs.defaultCWD) ждёт именно эти поля; ``cwd`` используется
	как рабочая папка для панели файлов и как база для новых проектов.
	"""
	cwd = await asyncio.get_running_loop().run_in_executor(
		None, _fs_default_cwd)
	branch = await asyncio.get_running_loop().run_in_executor(
		None, _fs_git_branch, cwd)
	return web.json_response({"cwd": cwd, "branch": branch})


async def handle_fs_list(request: web.Request) -> web.Response:
	"""Листинг каталога средствами Python (os.scandir).

	GET /api/fs/list?path=...  →  {entries:[{name, path, isDirectory}], error?}
	Папки идут первыми, сортировка по имени, скрытые файлы пропускаются.
	Ошибки возвращаются в поле error (в стиле errno: ENOENT, EACCES...).
	"""
	raw = request.query.get("path", "").strip().strip('"')
	if not raw:
		return web.json_response({"entries": [], "error": "ENOENT"})
	target = raw
	if len(target) > 1 and (target[1] == ":" or target.startswith("\\\\")):
		target = os.path.abspath(target)
	if not os.path.isdir(target):
		return web.json_response({"entries": [], "error": "ENOENT"})
	try:
		entries = []
		with os.scandir(target) as scan:
			for entry in scan:
				try:
					if entry.name.startswith("."):
						continue
					entries.append({
						"name": entry.name,
						"path": os.path.abspath(entry.path),
						"isDirectory": entry.is_dir(follow_symlinks=False),
					})
				except OSError:
					swallow.note('web_server.handle_fs_list:L1345')
					continue
		entries.sort(key=lambda it: (not it["isDirectory"], it["name"].lower()))
		return web.json_response({"entries": entries})
	except PermissionError:
		return web.json_response({"entries": [], "error": "EACCES"})
	except NotADirectoryError:
		return web.json_response({"entries": [], "error": "ENOTDIR"})
	except FileNotFoundError:
		return web.json_response({"entries": [], "error": "ENOENT"})
	except OSError as e:
		return web.json_response({"entries": [], "error": str(e) or "read-error"})


_FS_WRITE_MAX_BYTES = 8 * 1024 * 1024


async def handle_fs_write_text(request: web.Request) -> web.Response:
	"""POST /api/fs/write-text {path, content} — запись текстового файла.

	Нужен панели просмотра/правки файлов и кнопке «Принять изменения» в diff.
	Пишем через временный файл + os.replace, чтобы падение не обрезало оригинал
	(та же схема, что в hermes_cli/web_routers/files.py).
	"""
	try:
		raw_body = await request.read()
	except Exception:
		return web.json_response({"error": "Expected JSON body"}, status=400)
	try:
		# Клиент может не указать charset — по умолчанию aiohttp берёт latin-1,
		# и русский текст в content превратился бы в «?». Пробуем UTF-8 первым.
		body = json.loads(raw_body.decode("utf-8"))
	except Exception:
		try:
			body = json.loads(raw_body.decode("utf-8-sig"))
		except Exception:
			try:
				body = await request.json()
			except Exception:
				return web.json_response({"error": "Expected JSON body"}, status=400)
	if not isinstance(body, dict):
		return web.json_response({"error": "Expected JSON object"}, status=400)
	raw = str(body.get("path") or "").strip().strip('"')
	if not raw:
		return web.json_response({"error": "path is required"}, status=400)
	content = body.get("content")
	if content is None:
		content = ""
	if not isinstance(content, str):
		return web.json_response({"error": "content must be a string"}, status=400)
	if len(content.encode("utf-8")) > _FS_WRITE_MAX_BYTES:
		return web.json_response({"error": "Content too large"}, status=413)

	target = Path(raw)
	try:
		if target.is_dir():
			return web.json_response({"error": "Path points to a directory"}, status=400)
		if target.exists() and not target.is_file():
			return web.json_response({"error": "Only regular files can be written"}, status=400)
		if not target.parent.is_dir():
			return web.json_response({"error": "Parent directory does not exist"}, status=400)
	except OSError as e:
		return web.json_response({"error": str(e) or "Invalid path"}, status=400)

	tmp = target.with_name(f".{target.name}.inf-tmp-{os.getpid()}")
	try:
		tmp.write_text(content, encoding="utf-8")
		os.replace(tmp, target)
	except PermissionError:
		tmp.unlink(missing_ok=True)
		return web.json_response({"error": "File is not writable"}, status=403)
	except OSError as e:
		tmp.unlink(missing_ok=True)
		return web.json_response({"error": f"Could not write file: {e}"}, status=500)
	logger.info("fs: записан %s (%d байт)", target, len(content.encode("utf-8")))
	return web.json_response({"ok": True, "path": str(target),
	                          "byteSize": len(content.encode("utf-8"))})


_MIME_BY_EXT = {
	".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
	".webp": "image/webp", ".bmp": "image/bmp", ".svg": "image/svg+xml", ".ico": "image/x-icon",
	".avif": "image/avif", ".pdf": "application/pdf", ".zip": "application/zip",
	".json": "application/json", ".html": "text/html", ".css": "text/css",
	".js": "text/javascript", ".txt": "text/plain", ".md": "text/markdown",
}


async def handle_fs_read_data_url(request: web.Request) -> web.Response:
	"""GET /api/fs/read-data-url?path=... — картинка в виде data: URL (для просмотра)."""
	raw = request.query.get("path", "").strip().strip('"')
	if not raw or not os.path.isfile(raw):
		return web.json_response({"error": "ENOENT"}, status=404)
	try:
		size = os.path.getsize(raw)
		if size > 32 * 1024 * 1024:
			return web.json_response({"error": "File too large"}, status=413)
		with open(raw, "rb") as fh:
			data = fh.read()
		mime = _MIME_BY_EXT.get(os.path.splitext(raw)[1].lower(), "application/octet-stream")
		return web.json_response({
			"dataUrl": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
			"path": os.path.abspath(raw), "byteSize": size, "mimeType": mime})
	except PermissionError:
		return web.json_response({"error": "EACCES"}, status=403)
	except OSError as e:
		return web.json_response({"error": str(e)}, status=500)


async def handle_fs_download(request: web.Request) -> web.Response:
	"""GET /api/fs/download?path=... — отдать файл целиком (для кнопки «Скачать»)."""
	raw = request.query.get("path", "").strip().strip('"')
	if not raw or not os.path.isfile(raw):
		return web.json_response({"error": "ENOENT"}, status=404)
	try:
		return web.FileResponse(raw, headers={"Cache-Control": "no-store"})
	except PermissionError:
		return web.json_response({"error": "EACCES"}, status=403)
	except OSError as e:
		return web.json_response({"error": str(e)}, status=500)


async def handle_fs_git_root(request: web.Request) -> web.Response:
	"""GET /api/fs/git-root?from_path=... — корень git-репозитория или {} .

	Нужен сравнению с версией в Git: /api/git/file-diff в gateway требует
	короткий путь относительно корня, а не абсолютный.
	"""
	raw = request.query.get("from_path", "").strip().strip('"')
	start = Path(raw) if raw else BASE_DIR
	try:
		start = start if start.is_dir() else start.parent
	except OSError:
		swallow.note('web_server.handle_fs_git_root:L1478')
		start = BASE_DIR
	for cand in [start, *start.parents]:
		if (cand / ".git").exists():
			return web.json_response({"root": str(cand), "path": str(cand)})
	return web.json_response({})


async def handle_git_file_diff(request: web.Request) -> web.Response:
	"""GET /api/git/file-diff?path=<корень>&file=<путь внутри> → {diff: str}.

	Сравнение рабочего дерева с HEAD. Держим этот роут локально: gateway этой
	сборки отдаёт на него 404, а панели «Принять изменения» он необходим.
	Неотслеживаемый файл показываем целиком как добавленный.
	"""
	root = request.query.get("path", "").strip().strip('"')
	rel = request.query.get("file", "").strip().strip('"')
	if not root or not rel:
		return web.json_response({"error": "path and file are required"}, status=400)
	base = Path(root)
	if not (base / ".git").exists():
		return web.json_response({"error": "Not a git repository"}, status=400)
	target = (base / rel).resolve()
	# Не выпускаем за пределы репозитория
	try:
		target.relative_to(base.resolve())
	except ValueError:
		return web.json_response({"error": "Path outside repository"}, status=400)

	def _git(*args: str) -> str:
		creationflags = 0
		try:
			from hermes_cli._subprocess_compat import windows_hide_flags
			creationflags = windows_hide_flags()
		except Exception:
			swallow.note('web_server._git:L1513')
			pass
		try:
			p = subprocess.run(["git", *args], cwd=str(base), capture_output=True,
							   timeout=30, creationflags=creationflags)
		except (OSError, subprocess.SubprocessError) as e:
			logger.warning("git %s: %s", " ".join(args), e)
			return ""
		return _decode_console(p.stdout or b"")

	rel_posix = rel.replace("\\", "/")
	out = _git("diff", "HEAD", "--", rel_posix)
	if out.strip():
		return web.json_response({"diff": out})
	# Файл новый (или репозиторий без коммитов) — показываем как добавленный
	tracked = _git("ls-files", "--error-unmatch", "--", rel_posix)
	if target.is_file() and not tracked.strip():
		try:
			body = target.read_text(encoding="utf-8", errors="replace")
		except OSError as e:
			return web.json_response({"error": str(e)}, status=500)
		lines = body.splitlines()
		out = [f"diff --git a/{rel_posix} b/{rel_posix}", "new file mode 100644",
		       "--- /dev/null", f"+++ b/{rel_posix}", f"@@ -0,0 +1,{len(lines)} @@"]
		out += [f"+{ln}" for ln in lines]
		return web.json_response({"diff": "\n".join(out) + "\n"})
	return web.json_response({"diff": ""})


async def handle_fs_watch(request: web.Request) -> web.StreamResponse:
	"""GET /api/fs/watch?path=... — следим за файлом и сообщаем об изменениях.

	SSE-поток (как /api/plugins/watch): раз в ~800 мс сверяем mtime и размер,
	и как только агент (или другая программа) изменил файл — шлём событие
	changed. Фронт по нему обновляет открытую панель просмотра/правки.
	Когда пользователь закрывает панель — соединение закрывается.
	"""
	raw = request.query.get("path", "").strip().strip('"')
	if not raw or not os.path.isfile(raw):
		return web.json_response({"error": "ENOENT"}, status=404)
	resp = web.StreamResponse(status=200, headers={
		"Content-Type": "text/event-stream",
		"Cache-Control": "no-cache",
		"Connection": "keep-alive",
		"X-Accel-Buffering": "no",
	})
	await resp.prepare(request)

	def _stamp():
		try:
			st = os.stat(raw)
			return (st.st_mtime_ns, st.st_size)
		except OSError:
			swallow.note('web_server._stamp:L1566')
			return None

	last = _stamp()
	hello = json.dumps({"type": "hello", "path": os.path.abspath(raw)})
	await resp.write(b"data: " + hello.encode("utf-8") + b"\n\n")
	beats = 0
	try:
		while True:
			await asyncio.sleep(0.8)
			now = _stamp()
			# Файл удалён или перезаписан — сообщаем, клиент сам переподключится
			if now is None:
				payload = json.dumps({"type": "gone"})
				await resp.write(b"data: " + payload.encode("utf-8") + b"\n\n")
				return resp
			if now != last:
				last = now
				payload = json.dumps({"type": "changed", "mtime": now[0], "size": now[1]})
				await resp.write(b"data: " + payload.encode("utf-8") + b"\n\n")
				beats = 0
			else:
				beats += 1
				if beats >= 25:            # ~20 секунд — держим соединение живым
					beats = 0
					await resp.write(b"data: " + json.dumps({"type": "ping"}).encode() + b"\n\n")
	except (ConnectionResetError, ConnectionAbortedError, asyncio.CancelledError):
		swallow.note('web_server.handle_fs_watch:L1593')
		pass
	except Exception as e:  # noqa: BLE001
		logger.debug("fs watch закрыт: %s", e)
	return resp


_FS_TEXT_MAX_BYTES = 1 * 1024 * 1024  # 1 МБ — потолок для передачи содержимого

# Текстовые расширения: для них агенту отдаём СОДЕРЖИМОЕ файла
_FS_TEXT_EXTS = {
	".txt", ".md", ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".html", ".htm",
	".css", ".scss", ".xml", ".yml", ".yaml", ".csv", ".ini", ".cfg", ".conf",
	".log", ".sh", ".bat", ".ps1", ".sql", ".rs", ".go", ".java", ".c", ".cpp",
	".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt", ".toml", ".env", ".vue",
}


def _fs_is_text_file(path: str) -> bool:
	return os.path.splitext(path)[1].lower() in _FS_TEXT_EXTS


async def handle_fs_read_text(request: web.Request) -> web.Response:
	"""Чтение текстового файла с диска (для передачи содержимого агенту).

	GET /api/fs/read-text?path=...  →  {text, path, truncated, binary}

	Путь ищется не как есть: агент часто упоминает файл относительно
	рабочей папки («style.css») или по имени без папки. Раньше такой путь
	проходил os.path.isfile() мимо и пользователь получал ENOENT на
	файле, который агент только что создал. Поэтому при промахе ищем
	тот же файл по имени в рабочей папке и папках проектов.
	"""
	raw = request.query.get("path", "").strip().strip('"')
	if not raw:
		return web.json_response({"error": "Не указан путь"}, status=400)
	target = _fs_resolve(raw)
	if not target:
		return web.json_response(
			{"error": "Файл не найден", "path": raw,
			 "hint": "Проверьте путь: возможно, агент указал имя файла "
						"без папки или с другой рабочей директорией."},
			status=404)
	raw = str(target)
	try:
		size = os.path.getsize(raw)
		if size > _FS_TEXT_MAX_BYTES:
			with open(raw, "r", encoding="utf-8", errors="replace") as fh:
				data = fh.read(_FS_TEXT_MAX_BYTES)
			return web.json_response({
				"text": data, "path": os.path.abspath(raw),
				"truncated": True, "binary": False})
		with open(raw, "r", encoding="utf-8", errors="replace") as fh:
			data = fh.read()
		# грубая проверка на бинарность: >30% не-печатных символов
		sample = data[:4096]
		binary = bool(sample) and sum(1 for ch in sample if ord(ch) < 9 or (13 < ord(ch) < 32)) / len(sample) > 0.3
		return web.json_response({
			"text": data, "path": os.path.abspath(raw),
			"truncated": False, "binary": binary})
	except PermissionError:
		return web.json_response({"error": "Нет доступа к файлу"}, status=403)
	except OSError as e:
		return web.json_response({"error": str(e)}, status=500)


async def handle_plugins_dir_pick(request: web.Request) -> web.Response:
	"""GET /api/plugins/dir?pick=1 — системный диалог выбора папки.

	Возвращает {dir, pick}: pick пуст, если пользователь отменил. Диалог
	показываем только по явному запросу, иначе обычный GET /api/plugins/dir
	блокировал бы интерфейс системным окном при каждой перерисовке.
	"""
	if not _is_loopback(request):
		return web.json_response({"error": "Доступ только с этой машины"}, status=403)
	dir_now = str(PLUGINS_DIR)
	if str(request.query.get("pick") or "") not in ("1", "true", "yes"):
		return web.json_response({"dir": dir_now, "pick": ""})
	# Диалог блокирующий — уводим с серверного потока
	paths: list = []
	try:
		await asyncio.to_thread(_pick_dir_sync, "Папка плагинов", paths)
	except Exception:
		logger.exception("plugins/dir: диалог выбора папки не открылся")
	picked = str(paths[0]).strip() if paths else ""
	return web.json_response({"dir": dir_now, "pick": picked})



# ==========================================================
#  ПЛАГИНЫ ИНТЕРФЕЙСА (/api/plugins/*)
#
#  Папка PLUGINS_DIR — источник правды для плагинов с диска. Агент
#  (Hermes fs tools) пишет туда .js, фронтенд подхватывает изменения
#  через SSE без перезагрузки страницы.
#
#  Роуты зарегистрированы ДО catch-all прокси (см. build_app), иначе
#  запросы ушли бы на gateway, где этих эндпоинтов нет.
# ==========================================================


async def handle_plugins_dir_set(request: web.Request) -> web.Response:
	"""POST /api/plugins/dir {dir} — сменить папку плагинов.

	Принимаем только существующий каталог. Проверка обязательна: сюда
	попадает агент, и папка задаётся из интерфейса — лишняя запись могла бы
	увести watcher и загрузчик .py/.js в произвольное место.
	"""
	if not _is_loopback(request):
		return web.json_response({"error": "Доступ только с этой машины"}, status=403)
	try:
		body = await request.json()
	except Exception:
		swallow.note('web_server.handle_plugins_dir_set:L1706')
		body = {}
	raw = str(body.get("dir") or "").strip().strip('"')
	if not raw:
		return web.json_response({"error": "Папка не указана"}, status=400)
	p = Path(os.path.expanduser(os.path.expandvars(raw)))
	try:
		p = p.resolve()
	except OSError as e:
		return web.json_response({"error": "Некорректный путь: %s" % e}, status=400)
	if not p.is_dir():
		return web.json_response({"error": "Это не папка: %s" % p}, status=400)
	try:
		# Пробуем создать служебный файл: если прав нет — поймём сразу.
		probe = p / ".infinity-write-test"
		probe.write_text("ok", encoding="utf-8")
		probe.unlink(missing_ok=True)
	except OSError as e:
		return web.json_response({"error": "В папку нельзя писать: %s" % e}, status=400)
	global PLUGINS_DIR
	old = PLUGINS_DIR
	PLUGINS_DIR = p
	_py_load_disabled()
	_pyp = _py_reload()
	logger.info("Папка плагинов сменена: %s -> %s", old, p)
	return web.json_response({
		"ok": True, "dir": str(p), "old": str(old),
		"files": _scan_plugins(), "py": _pyp,
	})


def _plugin_path(name: str) -> Optional[Path]:
	"""Безопасное разрешение имени .js-файла внутри PLUGINS_DIR.

	Имя отклоняется целиком, а не «очищается»: иначе «../evil.js» молча
	превратился бы в «evil.js» и запрос прошёл бы с кодом 200 вместо 400.
	Плагин — только файл верхнего уровня папки плагинов, без подкаталогов.
	"""
	raw = str(name or "").strip().strip('"')
	if not raw or "/" in raw or "\\" in raw:
		return None
	raw = raw.strip()
	if raw.startswith(".") or not raw.lower().endswith(PLUGIN_EXT):
		return None
	try:
		root = PLUGINS_DIR.resolve()
		target = (PLUGINS_DIR / raw).resolve()
	except OSError:
		swallow.note('web_server._plugin_path:L1754')
		return None
	if target.parent != root:          # подстраховка от симлинков и «..»
		return None
	return target


def _plugin_hash(text: str) -> str:
	"""Короткий стабильный хеш кода — по нему фронт видит, что файл изменился."""
	import hashlib
	return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


# ==============================================================
#  PY-ПЛАГИНЫ (frontend/pyplugins.py)
#  Лежат в той же папке PLUGINS_DIR, но с расширением .py: код
#  исполняется на сервере, поэтому доступ только с этой машины.
# ==============================================================

PYPLUGIN_EXT = ".py"
_PY_DISABLED_FILE = ".pyplugins-disabled.json"
_py_disabled: set = set()


def _py_disabled_path() -> Path:
	return PLUGINS_DIR / _PY_DISABLED_FILE


def _py_stem(name: str) -> str:
	"""Имя плагина без расширения.

	Именно в таком виде load_all() ждёт выключенные плагины (fn[:-3]), поэтому
	в _py_disabled держим stem. Раньше туда клали имя с расширением — и
	выключение плагина молча не работало: проверка всегда проходила.
	"""
	return str(name or "").strip()[:-len(PYPLUGIN_EXT)] \
		if str(name or "").lower().endswith(PYPLUGIN_EXT) else str(name or "").strip()


def _py_disabled_files() -> list:
	"""Выключенные плагины для показа в UI — с расширением, как на диске."""
	return sorted(n + PYPLUGIN_EXT for n in _py_disabled)


def _py_load_disabled() -> None:
	"""Какие .py выключены пользователем. Хранится рядом с плагинами."""
	_py_disabled.clear()
	try:
		raw = json.loads(_py_disabled_path().read_text(encoding="utf-8"))
		if isinstance(raw, list):
			_py_disabled.update(str(x) for x in raw if isinstance(x, str))
	except (OSError, ValueError):
		swallow.note('web_server._py_load_disabled:L1806')
		pass


def _py_save_disabled() -> None:
	try:
		PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
		_py_disabled_path().write_text(
			json.dumps(sorted(_py_disabled), ensure_ascii=False, indent=1),
			encoding="utf-8")
	except OSError as e:
		logger.warning("pyplugins: не удалось сохранить список выключенных: %s", e)


def _py_path(name: str) -> Optional[Path]:
	"""Безопасное разрешение имени .py внутри PLUGINS_DIR (как _plugin_path).

	Имя отклоняется целиком, а не очищается: иначе «../evil.py» превратился
	бы в «evil.py» и запрос прошёл бы с кодом 200 вместо 400.
	"""
	raw = str(name or "").strip().strip('"')
	if not raw or "/" in raw or "\\" in raw:
		return None
	raw = raw.strip()
	if raw.startswith(".") or not raw.lower().endswith(PYPLUGIN_EXT):
		return None
	try:
		root = PLUGINS_DIR.resolve()
		target = (PLUGINS_DIR / raw).resolve()
	except OSError:
		swallow.note('web_server._py_path:L1836')
		return None
	if target.parent != root:          # подстраховка от симлинков и «..»
		return None
	return target


def _py_reload(disabled: Optional[set] = None) -> dict:
	"""Перечитать папку .py целиком. Возвращает сводку для UI."""
	res = _pyplugins.load_all(
		PLUGINS_DIR, disabled if disabled is not None else _py_disabled)
	res["dir"] = str(PLUGINS_DIR)
	return res


async def handle_pyplugins_list(request: web.Request) -> web.Response:
	"""GET /api/pyplugins/list[?with=1] — загруженные .py и метаданные."""
	if not _is_loopback(request):
		return web.json_response({"error": "Доступ только с этой машины"}, status=403)
	with_code = str(request.query.get("with") or "") in ("1", "true", "yes")
	return web.json_response({
		"dir": str(PLUGINS_DIR),
		"plugins": _pyplugins.list_info(with_code),
		"commands": _pyplugins.commands_list(),
		"events": _pyplugins.events_list(),
		"disabled": _py_disabled_files(),
	})


async def handle_pyplugins_read(request: web.Request) -> web.Response:
	"""GET /api/pyplugins/read?name=foo.py — исходник плагина."""
	if not _is_loopback(request):
		return web.json_response({"error": "Доступ только с этой машины"}, status=403)
	target = _py_path(request.query.get("name", ""))
	if target is None or not target.is_file():
		return web.json_response({"error": "Файл не найден"}, status=404)
	try:
		text = target.read_text(encoding="utf-8", errors="replace")
	except OSError as e:
		return web.json_response({"error": str(e)}, status=500)
	return web.json_response({
		"name": target.name, "code": text,
		"hash": _plugin_hash(text), "path": str(target),
	})


async def handle_pyplugins_write(request: web.Request) -> web.Response:
	"""POST /api/pyplugins/write {name, code} — записать .py атомарно."""
	if not _is_loopback(request):
		return web.json_response({"error": "Доступ только с этой машины"}, status=403)
	try:
		body = await request.json()
	except Exception:
		return web.json_response({"error": "Некорректный JSON"}, status=400)
	target = _py_path(body.get("name", ""))
	if target is None:
		return web.json_response({"error": "Нужно имя файла .py"}, status=400)
	code = body.get("code")
	if not isinstance(code, str) or not code.strip():
		return web.json_response({"error": "Код не задан"}, status=400)
	if len(code.encode("utf-8")) > _pyplugins.MAX_BYTES:
		return web.json_response(
			{"error": "Файл больше %d КБ" % (_pyplugins.MAX_BYTES // 1024)}, status=400)
	# Синтаксис проверяем ДО записи: битый файл бесполезен, а ошибка
	# показывается сразу, а не при следующей перезагрузке папки.
	try:
		compile(code, target.name, "exec")
	except SyntaxError as e:
		return web.json_response(
			{"error": "Синтаксическая ошибка: строка %s — %s" % (e.lineno, e.msg)},
			status=400)
	tmp = target.with_suffix(PYPLUGIN_EXT + ".tmp")
	try:
		PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
		tmp.write_text(code, encoding="utf-8")
		os.replace(str(tmp), str(target))
	except OSError as e:
		tmp.unlink(missing_ok=True)
		return web.json_response({"error": "Не удалось записать: %s" % e}, status=500)
	# Новый/изменённый плагин сразу включаем — как и с .js
	_py_disabled.discard(_py_stem(target.name))
	_py_save_disabled()
	res = _py_reload()
	logger.info("pyplugins: записан %s (%d Б)", target.name, len(code.encode("utf-8")))
	return web.json_response({"ok": True, "file": target.name, "reload": res})


async def handle_pyplugins_delete(request: web.Request) -> web.Response:
	"""POST /api/pyplugins/delete {name} — удалить .py с диска."""
	if not _is_loopback(request):
		return web.json_response({"error": "Доступ только с этой машины"}, status=403)
	try:
		body = await request.json()
	except Exception:
		swallow.note('web_server.handle_pyplugins_delete:L1930')
		body = {}
	target = _py_path(body.get("name", ""))
	if target is None or not target.is_file():
		return web.json_response({"error": "Файл не найден"}, status=404)
	try:
		target.unlink()
	except OSError as e:
		return web.json_response({"error": "Не удалось удалить: %s" % e}, status=500)
	_py_disabled.discard(_py_stem(target.name))
	_py_save_disabled()
	res = _py_reload()
	logger.info("pyplugins: удалён %s", target.name)
	return web.json_response({"ok": True, "file": target.name, "reload": res})


async def handle_pyplugins_toggle(request: web.Request) -> web.Response:
	"""POST /api/pyplugins/toggle {name, enabled} — включить/выключить плагин."""
	if not _is_loopback(request):
		return web.json_response({"error": "Доступ только с этой машины"}, status=403)
	try:
		body = await request.json()
	except Exception:
		swallow.note('web_server.handle_pyplugins_toggle:L1953')
		body = {}
	target = _py_path(body.get("name", ""))
	if target is None or not target.is_file():
		return web.json_response({"error": "Файл не найден"}, status=404)
	enabled = bool(body.get("enabled"))
	stem = _py_stem(target.name)
	if enabled:
		_py_disabled.discard(stem)
	else:
		_py_disabled.add(stem)
	_py_save_disabled()
	return web.json_response({"ok": True, "file": target.name,
	                         "enabled": enabled, "reload": _py_reload()})


async def handle_pyplugins_reload(request: web.Request) -> web.Response:
	"""POST /api/pyplugins/reload — перечитать папку .py целиком."""
	if not _is_loopback(request):
		return web.json_response({"error": "Доступ только с этой машины"}, status=403)
	return web.json_response({"ok": True, "reload": _py_reload()})


async def handle_pyplugins_command(request: web.Request) -> web.Response:
	"""POST /api/pyplugins/command {name, arg} — выполнить команду плагина."""
	if not _is_loopback(request):
		return web.json_response({"error": "Доступ только с этой машины"}, status=403)
	try:
		body = await request.json()
	except Exception:
		swallow.note('web_server.handle_pyplugins_command:L1983')
		body = {}
	res = await _pyplugins.run_command(str(body.get("name") or ""),
	                                   str(body.get("arg") or ""))
	return web.json_response(res, status=200 if res.get("ok") else 400)


async def handle_pyplugins_emit(request: web.Request) -> web.Response:
	"""POST /api/pyplugins/emit {event, payload} — разослать событие.

	Событие приходит из интерфейса. Плагин может переписать payload —
	так же, как в JS-плагинах.
	"""
	if not _is_loopback(request):
		return web.json_response({"error": "Доступ только с этой машины"}, status=403)
	try:
		body = await request.json()
	except Exception:
		swallow.note('web_server.handle_pyplugins_emit:L2001')
		body = {}
	event = str(body.get("event") or "").strip()
	if not event:
		return web.json_response({"error": "Событие не указано"}, status=400)
	out = await _pyplugins.emit(event, body.get("payload"))
	return web.json_response({"ok": True, "payload": out,
	                          "toasts": _pyplugins._drain_toasts()})


async def handle_pyplugins_run(request: web.Request) -> web.Response:
	"""GET /api/pyplugins/run/<путь>?a=1 — свой эндпоинт плагина."""
	if not _is_loopback(request):
		return web.json_response({"error": "Доступ только с этой машины"}, status=403)
	name = str(request.match_info.get("path") or "").strip()
	res = await _pyplugins.run_route(name, dict(request.query))
	if res.get("error"):
		return web.json_response(res, status=400)
	return web.json_response(res)


async def handle_pyplugins_logs(request: web.Request) -> web.Response:
	"""GET /api/pyplugins/logs — журнал print() и ошибок плагинов."""
	if not _is_loopback(request):
		return web.json_response({"error": "Доступ только с этой машины"}, status=403)
	return web.json_response({
		"logs": list(_pyplugins.LOGS)[-100:],
		"errors": dict(_pyplugins.ERRORS),
	})


def _plugin_meta(path: Path, with_code: bool = False) -> Optional[dict]:
	"""Метаданные одного .js: имя, размер, mtime_ns, хеш (и код — по запросу)."""
	try:
		st = path.stat()
		if not path.is_file():
			return None
	except OSError:
		swallow.note('web_server._plugin_meta:L2039')
		return None
	item = {
		"name": path.name,
		"size": int(st.st_size),
		"mtime": float(st.st_mtime),
		"mtimeNs": int(st.st_mtime_ns),
		"path": str(path),
	}
	if with_code:
		try:
			data = path.read_bytes()
		except OSError:
			swallow.note('web_server._plugin_meta:L2052')
			return None
		text = data.decode("utf-8", errors="replace")
		item["code"] = text
		item["truncated"] = len(data) > PLUGIN_MAX_BYTES
		item["hash"] = _plugin_hash(text)
	return item


def _scan_plugins(with_code: bool = False) -> list:
	"""Список всех .js в папке плагинов, отсортированный по имени."""
	if not PLUGINS_DIR.is_dir():
		return []
	out = []
	try:
		with os.scandir(PLUGINS_DIR) as scan:
			for entry in scan:
				if not entry.name.lower().endswith(PLUGIN_EXT):
					continue
				if entry.name.startswith("."):
					continue
				meta = _plugin_meta(Path(entry.path), with_code)
				if meta:
					out.append(meta)
	except PermissionError:
		logger.warning("plugins: нет доступа к %s", PLUGINS_DIR)
	except OSError as e:
		logger.warning("plugins: %s не читается: %s", PLUGINS_DIR, e)
	out.sort(key=lambda it: it["name"].lower())
	return out


async def handle_plugins_dir(request: web.Request) -> web.Response:
	"""GET /api/plugins/dir — путь к папке плагинов (его же получает агент)."""
	PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
	files = _scan_plugins()
	return web.json_response({
		"dir": str(PLUGINS_DIR),
		"exists": PLUGINS_DIR.is_dir(),
		"count": len(files),
		"names": [f["name"] for f in files],
	})


async def handle_subagents_list(request: web.Request) -> web.Response:
	"""GET /api/subagents — снимок субагентов (делегаций) и состояния их работы.

	Данные берутся прямо у движка делегаций Hermes, который ведёт реестр
	живых и недавно завершённых субагентов. Отдаём только поля, безопасные
	для показа в интерфейсе: тексты целей, статусы, счётчики вызовов и
	время. Секреты и колбэки наружу не уходят.
	"""
	try:
		from tools.async_delegation import list_async_delegations
		items = await asyncio.to_thread(list_async_delegations)
	except Exception as e:  # движок мог не подгрузиться — это не ошибка сервера
		logger.debug("subagents недоступны: %s", e)
		return web.json_response({"items": [], "available": False, "error": str(e)})

	def slim(d: dict) -> dict:
		# Копируем только известные безопасные поля: у записи движка есть
		# служебные ключи и функции, которые нельзя отдавать в JSON.
		return {k: d.get(k) for k in (
			"delegation_id", "goal", "goals", "status", "role", "model",
			"summary", "error", "exit_reason", "dispatched_at", "completed_at",
			"duration_seconds", "api_calls", "is_batch", "task_indexes",
			"children_activity", "seconds_since_progress", "seconds_since_activity",
			"toolsets", "parent_session_id", "origin_session_id",
		) if k in d}

	now = time.time()
	out = []
	for d in items:
		row = slim(d if isinstance(d, dict) else {})
		# Считаем «сколько идёт» прямо здесь: dispatched_at/completed_at —
		# unix-время, а в интерфейсе нужна живая длительность.
		try:
			ts = d.get("dispatched_at") if isinstance(d, dict) else None
			if ts:
				end = d.get("completed_at") or now
				row["elapsed"] = round(max(0.0, float(end) - float(ts)), 1)
		except (TypeError, ValueError):
			swallow.note('web_server.handle_subagents_list:L2134')
			pass
		out.append(row)
	out.sort(key=lambda r: (str(r.get("status")), r.get("dispatched_at") or 0))
	return web.json_response({"items": out, "available": True, "count": len(out)})


async def handle_plugins_list(request: web.Request) -> web.Response:
	"""GET /api/plugins/list[?with=1] — метаданные всех .js (с кодом при with=1)."""
	PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
	with_code = (request.query.get("with") or "") in ("1", "true", "yes")
	return web.json_response({"dir": str(PLUGINS_DIR),
	                          "files": _scan_plugins(with_code)})


async def handle_plugins_read(request: web.Request) -> web.Response:
	"""GET /api/plugins/read?name=foo.js — содержимое одного плагина."""
	target = _plugin_path(request.query.get("name", ""))
	if target is None or not target.is_file():
		return web.json_response({"error": "ENOENT"}, status=404)
	try:
		data = target.read_bytes()
	except PermissionError:
		return web.json_response({"error": "EACCES"}, status=403)
	except OSError as e:
		return web.json_response({"error": str(e)}, status=500)
	text = data.decode("utf-8", errors="replace")
	return web.json_response({
		"name": target.name, "path": str(target), "code": text,
		"hash": _plugin_hash(text), "truncated": len(data) > PLUGIN_MAX_BYTES,
	})


async def handle_plugins_readme(request: web.Request) -> web.Response:
	"""GET /api/plugins/readme — документация для агента (README.txt).

	Имя файла здесь НЕ принимается из запроса: путь зашит в коде, поэтому
	обхода папки через параметр не существует в принципе. Основной
	/api/plugins/read намеренно пропускает всё, что не .js.
	"""
	target = PLUGINS_DIR / "README.txt"
	if not target.is_file():
		return web.json_response({"error": "ENOENT"}, status=404)
	try:
		text = target.read_text(encoding="utf-8", errors="replace")
	except PermissionError:
		return web.json_response({"error": "EACCES"}, status=403)
	except OSError as e:
		return web.json_response({"error": str(e)}, status=500)
	return web.json_response({"name": "README.txt", "path": str(target), "code": text})


async def handle_plugins_write(request: web.Request) -> web.Response:
	"""POST /api/plugins/write {name, code} — атомарная запись .js."""
	try:
		body = await request.json()
	except Exception:
		return web.json_response({"error": "invalid json"}, status=400)
	target = _plugin_path(body.get("name", ""))
	if target is None:
		return web.json_response({"error": "bad name"}, status=400)
	code = body.get("code")
	if not isinstance(code, str) or not code.strip():
		return web.json_response({"error": "empty code"}, status=400)
	if len(code.encode("utf-8")) > PLUGIN_MAX_BYTES:
		return web.json_response({"error": "too large"}, status=413)
	PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
	tmp = target.with_name(target.name + ".tmp")
	try:
		# Пишем байтами с newline-контролем: в текстовом режиме Windows
		# превратил бы \n в \r\n, и хеш файла не совпал бы с хешем кода
		# в браузере — плагин перезагружался бы впустую при каждой синхронизации.
		with open(tmp, "wb") as fh:
			fh.write(code.encode("utf-8"))
		os.replace(tmp, target)          # атомарно: фронт не прочитает обрезок
	except PermissionError:
		return web.json_response({"error": "EACCES"}, status=403)
	except OSError as e:
		try:
			if tmp.exists():
				tmp.unlink()
		except OSError:
			swallow.note('web_server.handle_plugins_write:L2216')
			pass
		return web.json_response({"error": str(e)}, status=500)
	logger.info("plugins: записан %s (%d Б)", target.name, len(code.encode("utf-8")))
	# Раньше здесь не было return — обработчик завершался None, и aiohttp
	# отдавал 500. Файл при этом записывался, но интерфейс считал сохранение
	# неудачным, из-за чего созданный агентом плагин не появлялся в списке.
	return web.json_response({"ok": True, "name": target.name,
	                          "path": str(target),
	                          "hash": _plugin_hash(code)})


async def handle_agent_prompt(request: web.Request) -> web.Response:
	"""GET/POST /api/agent/prompt — системные инструкции агента.

	Хранятся в файле, а не в настройках браузера: иначе после перезапуска
	сервера поле выглядело пустым, хотя агент продолжал работать со старым
	текстом. Возвращаем ещё и размер, чтобы интерфейс понимал, что файл есть.
	"""
	if not _is_loopback(request):
		return web.json_response({"error": "Доступ только с этой машины"}, status=403)
	path = _agent_prompt_path(request)
	if request.method == "GET":
		read_path = path
		if not read_path.is_file() and _req_profile(request):
			read_path = _agent_effective_file(request, "AGENT_PROMPT.md")
		if not read_path.is_file():
			return web.json_response({"content": "", "exists": False,
			 "path": str(path)})
		try:
			text = read_path.read_text(encoding="utf-8", errors="replace")
		except OSError as e:
			return web.json_response({"error": str(e)}, status=500)
		return web.json_response({"content": text, "exists": True,
		 "path": str(path)})

	try:
		body = await request.json()
	except Exception:
		return web.json_response({"error": "Некорректный JSON"}, status=400)
	content = body.get("content")
	if not isinstance(content, str):
		return web.json_response({"error": "content должен быть строкой"}, status=400)
	raw = content.encode("utf-8")
	if len(raw) > AGENT_FILE_MAX_BYTES:
		return web.json_response(
			{"error": "Текст больше %d КБ" % (AGENT_FILE_MAX_BYTES // 1024)}, status=400)

	def _write() -> dict:
		path.parent.mkdir(parents=True, exist_ok=True)
		if path.is_file():
			try:
				shutil.copy2(path, str(path) + ".bak")
			except OSError:
				logger.warning("agent-prompt: не удалось сделать .bak")
		tmp = path.with_suffix(".md.tmp")
		tmp.write_bytes(raw)
		os.replace(str(tmp), str(path))
		return {"ok": True, "size": len(raw), "path": str(path)}

	try:
		res = await asyncio.to_thread(_write)
	except OSError as e:
		logger.error("agent-prompt: запись не удалась: %s", e)
		return web.json_response({"error": f"Не удалось записать: {e}"}, status=500)
	logger.info("agent-prompt: сохранено %d Б", res["size"])
	return web.json_response(res)

	return web.json_response({"ok": True, "file": _plugin_meta(target, with_code=True)})


async def handle_plugins_delete(request: web.Request) -> web.Response:
	"""POST /api/plugins/delete {name} — удалить .js с диска."""
	try:
		body = await request.json()
	except Exception:
		return web.json_response({"error": "invalid json"}, status=400)
	target = _plugin_path(body.get("name", ""))
	if target is None:
		return web.json_response({"error": "bad name"}, status=400)
	if not target.is_file():
		return web.json_response({"error": "ENOENT"}, status=404)
	try:
		target.unlink()
	except PermissionError:
		return web.json_response({"error": "EACCES"}, status=403)
	except OSError as e:
		return web.json_response({"error": str(e)}, status=500)
	logger.info("plugins: удалён %s", target.name)
	return web.json_response({"ok": True, "name": target.name})


async def handle_plugins_open(request: web.Request) -> web.Response:
	"""POST /api/plugins/open — открыть папку плагинов в проводнике ОС."""
	PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
	try:
		if os.name == "nt":
			os.startfile(str(PLUGINS_DIR))
		elif os.uname().sysname == "Darwin":
			subprocess.Popen(["open", str(PLUGINS_DIR)])
		else:
			subprocess.Popen(["xdg-open", str(PLUGINS_DIR)])
	except Exception as e:  # noqa: BLE001
		logger.warning("plugins: папку не открыть: %s", e)
		return web.json_response({"error": str(e), "dir": str(PLUGINS_DIR)}, status=500)
	return web.json_response({"ok": True, "dir": str(PLUGINS_DIR)})


# ── Наблюдение за папкой: SSE-поток событий ────────────────────────────────
# Фоновая задача раз в 1.5 с сканирует папку и публикует дельту в очередь.
# Подписчики (обычно одна вкладка) получают SSE «changed» и дёргают /list.
_PLUGIN_QUEUE: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=64)


async def _plugin_watcher() -> None:
	"""Фоновая задача: следит за PLUGINS_DIR и публикует дельты."""
	prev: dict = {}
	while True:
		try:
			cur = {f["name"]: (f["mtimeNs"], f["size"]) for f in _scan_plugins()}
			if prev and cur != prev:     # первое сканирование — не дельта
				added = [n for n in cur if n not in prev]
				changed = [n for n in cur if n in prev and cur[n] != prev[n]]
				removed = [n for n in prev if n not in cur]
				if added or changed or removed:
					ev = {"type": "changed", "added": added,
						  "changed": changed, "removed": removed}
					try:
						_PLUGIN_QUEUE.put_nowait(ev)
					except asyncio.QueueFull:
						swallow.note('web_server._plugin_watcher:L2346')
						pass          # фронт догонит через /list
			prev = cur
		except asyncio.CancelledError:
			raise
		except Exception as e:  # noqa: BLE001
			logger.warning("plugins watcher: %s", e)
		await asyncio.sleep(1.5)


async def handle_plugins_watch(request: web.Request) -> web.StreamResponse:
	"""GET /api/plugins/watch — SSE: hello / changed / ping."""
	resp = web.StreamResponse(status=200, headers={
		"Content-Type": "text/event-stream",
		"Cache-Control": "no-cache",
		"Connection": "keep-alive",
		"X-Accel-Buffering": "no",
	})
	await resp.prepare(request)
	try:
		PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
		hello = json.dumps({"type": "hello", "dir": str(PLUGINS_DIR)})
		await resp.write(b"data: " + hello.encode() + b"\n\n")
		while True:
			try:
				payload = json.dumps(await asyncio.wait_for(_PLUGIN_QUEUE.get(), timeout=20))
			except asyncio.TimeoutError:
				swallow.note('web_server.handle_plugins_watch:L2373')
				payload = json.dumps({"type": "ping"})   # держим соединение живым
			await resp.write(b"data: " + payload.encode() + b"\n\n")
	except (ConnectionResetError, ConnectionAbortedError, asyncio.CancelledError):
		swallow.note('web_server.handle_plugins_watch:L2377')
		pass
	except Exception as e:  # noqa: BLE001
		logger.debug("plugins watch закрыт: %s", e)
	return resp


async def handle_fs_pick_dir(request: web.Request) -> web.Response:
	"""Резервный вариант для папок: нативный диалог выбора каталога (tkinter).

	Вызывается фронтендом, когда автопоиск (/api/fs/resolve, kind=dir) не нашёл
	папку по имени. Возвращает абсолютный путь выбранной папки или пустой результат
	при отмене.
	"""
	try:
		body = await request.json()
	except Exception:
		swallow.note('web_server.handle_fs_pick_dir:L2394')
		body = {}
	title = str(body.get("title") or "Выберите папку")
	paths = []
	try:
		await asyncio.get_running_loop().run_in_executor(None, _pick_dir_sync, title, paths)
	except Exception as e:
		logger.exception("fs/pick-dir dialog failed")
		return web.json_response({"error": str(e)}, status=500)
	return web.json_response({"path": paths[0] if paths else None})


def _pick_dir_sync(title: str, out: list) -> None:
	"""Диалог выбора папки в отдельном процессе tkinter (как в _pick_sync)."""
	try:
		py = sys.executable or "python"
		code = (
			"import tkinter as tk, tkinter.filedialog as fd, json, sys\n"
			"r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
			"p = fd.askdirectory(parent=r, title=%r)\n"
			"r.destroy()\n"
			"sys.stdout.write(json.dumps([p] if p else []))\n" % (title,)
		)
		res = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=300)
		data = json.loads(res.stdout.strip() or "[]")
		if isinstance(data, list):
			out.extend([str(p) for p in data if p])
	except Exception:
		logger.exception("tkinter pick-dir subprocess failed")


# ── Консольные команды из чата («!команда») ──────────────────────────────
# Выполняются локально на этой машине — как «bang»-режим CLI Hermes
# (hermes_cli/bang_shell.py): модель не вызывается, ход не тратится, но
# команда проходит тот же гейт approval, что и терминальный инструмент.
EXEC_MAX_OUT = int(os.getenv("INFINITY_EXEC_MAX_OUT", "200000"))
EXEC_TIMEOUT_MAX = 600


def _is_loopback(request: web.Request) -> bool:
	"""True только для запросов с этой же машины (127.0.0.1 / ::1)."""
	peer = request.transport.get_extra_info("peername") if request.transport else None
	host = str(peer[0]) if isinstance(peer, (tuple, list)) and peer else ""
	return host in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")


def _exec_guards(cmd: str) -> dict:
	"""Гейт терминального инструмента Hermes: blocklist, deny-правила, tirith."""
	try:
		from tools.terminal_tool import _check_all_guards
	except Exception as e:
		logger.warning("Exec: гейт недоступен (%s) — команда разрешена", e)
		return {"approved": True, "message": None}
	try:
		return _check_all_guards(cmd, "local", has_host_access=False)
	except Exception as e:
		logger.warning("Exec: гейт упал (%s) — команда разрешена", e)
		return {"approved": True, "message": None}


def _exec_env() -> dict:
	"""Окружение для команды с вырезанными секретами (как в bang-shell CLI)."""
	try:
		from tools.environments.local import build_subprocess_env
		return build_subprocess_env()
	except Exception:
		swallow.note('web_server._exec_env:L2460')
		return os.environ.copy()


def _decode_console(raw: bytes) -> str:
	"""bytes → текст: utf-8 → cp866 (русская консоль Windows) → cp1251."""
	for enc in ("utf-8", "cp866", "cp1251"):
		try:
			return raw.decode(enc)
		except UnicodeDecodeError:
			swallow.note('web_server._decode_console:L2470')
			continue
	return raw.decode("utf-8", errors="replace")


def _exec_run(cmd: str, cwd: str, timeout: int) -> dict:
	"""Выполнить команду, вернуть {code, out, dur, truncated}. Исключений не бросает."""
	creationflags = 0
	try:
		from hermes_cli._subprocess_compat import windows_hide_flags
		creationflags = windows_hide_flags()
	except Exception:
		swallow.note('web_server._exec_run:L2482')
		pass
	t0 = time.time()
	code = 0
	try:
		proc = subprocess.run(cmd, shell=True, cwd=cwd, env=_exec_env(),
						stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
						timeout=timeout, creationflags=creationflags)
		out = _decode_console(proc.stdout or b"")
		code = int(proc.returncode or 0)
	except subprocess.TimeoutExpired as e:
		swallow.note('web_server._exec_run:L2493', e)
		out = _decode_console(e.stdout or b"") + f"\n[!] команда остановлена по таймауту {timeout} с"
		code = 124
	except Exception as e:
		swallow.note('web_server._exec_run:L2497', e)
		out = f"[!] не удалось выполнить: {e}"
		code = 127
	truncated = len(out) > EXEC_MAX_OUT
	if truncated:
		out = out[:EXEC_MAX_OUT] + "\n… [вывод усечён]"
	return {"code": code, "out": out, "dur": round(time.time() - t0, 2), "truncated": truncated}


async def handle_exec(request: web.Request) -> web.Response:
	"""POST /api/exec {cmd, cwd?, timeout?, force?} — выполнить команду из чата."""
	if not _is_loopback(request):
		return web.json_response({"error": "Команды доступны только с этой машины"}, status=403)
	try:
		body = await request.json()
	except Exception:
		swallow.note('web_server.handle_exec:L2513')
		body = {}
	if not isinstance(body, dict):
		body = {}
	cmd = str(body.get("cmd") or "").strip()
	if not cmd:
		return web.json_response({"error": "Пустая команда"}, status=400)
	try:
		timeout = max(1, min(EXEC_TIMEOUT_MAX, int(body.get("timeout") or 60)))
	except (TypeError, ValueError):
		swallow.note('web_server.handle_exec:L2523')
		timeout = 60
	cwd = str(body.get("cwd") or "").strip()
	if not cwd or not Path(cwd).is_dir():
		cwd = str(BASE_DIR.parent)
	if not body.get("force"):
		gate = await asyncio.to_thread(_exec_guards, cmd)
		if not gate.get("approved", True):
			return web.json_response({
				"approved": False,
				"message": gate.get("message") or "Команда требует подтверждения",
				"cmd": cmd, "cwd": cwd}, status=409)
	res = await asyncio.to_thread(_exec_run, cmd, cwd, timeout)
	res.update({"ok": res["code"] == 0, "cmd": cmd, "cwd": cwd})
	logger.info("Exec: %r → код %s за %.2f c (cwd=%s)", cmd, res["code"], res["dur"], cwd)
	return web.json_response(res)


async def handle_intercept_run(request: web.Request) -> web.Response:
    """POST /api/intercept/run {code, cwd?, timeout?, args?} — выполнить скрипт команды.

    Скрипт запускается от имени сервера, поэтому доступ разрешён только с этой
    машины: те же правила, что у /api/exec.
    """
    if not _is_loopback(request):
        return web.json_response({"error": "Перехватчики доступны только с этой машины"}, status=403)
    try:
        body = await request.json()
    except Exception:
        swallow.note('web_server.handle_intercept_run:L2552')
        body = {}
    job, err = _intercepts.validate(body)
    if err:
        return web.json_response({"error": err}, status=400)
    res = await asyncio.to_thread(
        _intercepts.run_intercept, job["code"], job["cwd"], job["timeout"], job["args"])
    res.update({"ok": res["code"] == 0, "cwd": job["cwd"]})
    logger.info("Intercept: код %s за %.2f c", res["code"], res["dur"])
    return web.json_response(res)


async def handle_intercept_read(request: web.Request) -> web.Response:
    """POST /api/intercept/read {path} — прочитать файл скрипта, указанный в перехватчике.

    Отдаёт содержимое файла наружу, поэтому доступ только с этой машины и только
    для разрешённых расширений (см. _intercepts.INTERCEPT_FILE_EXT).
    """
    if not _is_loopback(request):
        return web.json_response({"error": "Чтение скриптов доступно только с этой машины"}, status=403)
    try:
        body = await request.json()
    except Exception:
        swallow.note('web_server.handle_intercept_read:L2575')
        body = {}
    path = str(body.get("path") or "").strip()
    if not path:
        return web.json_response({"error": "Путь не указан"}, status=400)
    res = await asyncio.to_thread(_intercepts.read_script, path)
    if res.get("error"):
        return web.json_response({"error": res["error"]}, status=400)
    return web.json_response(res)


async def handle_tools_local(request: web.Request) -> web.Response:
    """GET /api/tools/local — инструменты и навыки прямо с диска.

    Резерв для случая, когда шлюз не отдал /v1/toolsets: показывать
    пользователю хотя бы то, что физически лежит в папках backend/tools
    и backend/skills. Источник правды — тот же код, что и у агента.
    """
    if not _is_loopback(request):
        return web.json_response({"error": "Доступ только с этой машины"}, status=403)
    tools_dir = BACKEND_PATH / "tools"
    skills_dir = BACKEND_PATH / "skills"
    tools: List[dict] = []
    skills: List[dict] = []
    try:
        with os.scandir(tools_dir) as scan:
            for e in scan:
                if e.is_file() and e.name.endswith(".py") and not e.name.startswith("_"):
                    stem = e.name[:-3]
                    if stem.endswith("_tool"):
                        stem = stem[:-5]          # read_file_tool -> read_file
                    tools.append({"name": stem, "file": e.name,
                                  "desc": "Модуль %s в backend/tools" % e.name})
    except OSError as ex:
        logger.warning("tools/local: не прочитать %s: %s", tools_dir, ex)
    try:
        with os.scandir(skills_dir) as scan:
            for e in scan:
                if e.is_dir() and not e.name.startswith("."):
                    desc = ""
                    skill_file = Path(e.path) / "SKILL.md"
                    try:                       # берём первую строку-описание
                        with skill_file.open(encoding="utf-8") as fh:
                            for line in fh:
                                if line.strip():
                                    desc = line.strip()[:200]
                                    break
                    except OSError:
                        swallow.note('web_server.handle_tools_local:L2623')
                        pass
                    skills.append({"name": e.name, "desc": desc,
                                   "path": str(Path(e.path))})
    except OSError as ex:
        logger.warning("tools/local: не прочитать %s: %s", skills_dir, ex)
    return web.json_response({
        "tools": sorted(tools, key=lambda x: x["name"]),
        "skills": sorted(skills, key=lambda x: x["name"]),
        "tools_dir": str(tools_dir), "skills_dir": str(skills_dir),
    })


async def handle_intercept_status(request: web.Request) -> web.Response:
    """GET /api/intercept/status — что и с какими ограничениями можно запускать."""
    return web.json_response({
        "enabled": True,
        "loopback_only": True,
        "langs": ["js", "python"],
        "timeout_max": _intercepts.INTERCEPT_TIMEOUT_MAX,
        "max_out": _intercepts.INTERCEPT_MAX_OUT,
    })


async def handle_agent_files(request: web.Request) -> web.Response:
    """GET /api/agent/files — список файлов инструкций и их содержимое.

    Отдаём только файлы из AGENT_FILES: имя приходит из этого же словаря,
    путь из запроса не используется. Запись наружу не уходит — сервер
    доступен лишь с этой машины (см. _is_loopback).
    """
    if not _is_loopback(request):
        return web.json_response({"error": "Доступ только с этой машины"}, status=403)
    items = []
    for name, desc in AGENT_FILES.items():
        path = _agent_effective_file(request, name)
        try:
            size = path.stat().st_size if path.is_file() else 0
            exists = path.is_file()
        except OSError:
            swallow.note('web_server.handle_agent_files:L2663')
            size, exists = 0, False
        items.append({"name": name, "desc": desc, "exists": exists,
                      "size": size, "path": str(path)})
    return web.json_response({"dir": str(_agent_base_dir(request)),
                      "profile": _req_profile(request), "files": items})


async def handle_agent_file_read(request: web.Request) -> web.Response:
    """GET /api/agent/file?name=SOUL.md — содержимое одного файла инструкций."""
    if not _is_loopback(request):
        return web.json_response({"error": "Доступ только с этой машины"}, status=403)
    name = str(request.query.get("name") or "")
    if name not in AGENT_FILES:
        return web.json_response({"error": f"Файл {name!r} редактировать нельзя"}, status=400)
    path = _agent_effective_file(request, name)
    if not path.is_file():
        return web.json_response({"name": name, "exists": False, "content": ""})
    try:
        data = path.read_bytes()
    except OSError as e:
        return web.json_response({"error": f"Не удалось прочитать: {e}"}, status=500)
    text = data.decode("utf-8", errors="replace")
    return web.json_response({
        "name": name, "exists": True, "path": str(path),
        "content": text, "truncated": len(data) > AGENT_FILE_MAX_BYTES,
    })


async def handle_agent_file_write(request: web.Request) -> web.Response:
    """POST /api/agent/file {name, content} — записать файл инструкций.

    Пишем атомарно (через временный файл с заменой), чтобы обрыв записи
    не оставил агента без инструкций. Перед заменой кладём копию .bak.
    """
    if not _is_loopback(request):
        return web.json_response({"error": "Доступ только с этой машины"}, status=403)
    try:
        body = await request.json()
    except Exception:
        swallow.note('web_server.handle_agent_file_write:L2703')
        body = {}
    name = str(body.get("name") or "")
    if name not in AGENT_FILES:
        return web.json_response({"error": f"Файл {name!r} редактировать нельзя"}, status=400)
    content = body.get("content")
    if not isinstance(content, str):
        return web.json_response({"error": "content должен быть строкой"}, status=400)
    raw = content.encode("utf-8")
    if len(raw) > AGENT_FILE_MAX_BYTES:
        return web.json_response(
            {"error": f"Файл больше {AGENT_FILE_MAX_BYTES // 1024} КБ"}, status=400)
    path = _agent_file_path(request, name)

    def _write() -> dict:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():                      # страховка от неудачной правки
            try:
                shutil.copy2(path, str(path) + ".bak")
            except OSError:
                logger.warning("agent-files: не удалось сделать .bak для %s", name)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(raw)
        os.replace(str(tmp), str(path))
        return {"ok": True, "name": name, "path": str(path), "size": len(raw)}

    try:
        res = await asyncio.to_thread(_write)
    except OSError as e:
        logger.error("agent-files: запись %s не удалась: %s", name, e)
        return web.json_response({"error": f"Не удалось записать: {e}"}, status=500)
    logger.info("agent-files: записан %s (%d Б)", name, res["size"])
    return web.json_response(res)


async def handle_exec_status(request: web.Request) -> web.Response:
	"""GET /api/exec/status — доступность консоли в чате."""
	return web.json_response({
		"enabled": True,
		"loopback_only": True,
		"shell": "cmd.exe" if os.name == "nt" else "/bin/sh",
		"cwd_default": str(BASE_DIR.parent),
		"timeout_max": EXEC_TIMEOUT_MAX,
		"max_out": EXEC_MAX_OUT,
		"platform": os.name})


def build_app() -> web.Application:
	app = web.Application(middlewares=[_cors_middleware], client_max_size=64 * 1024 * 1024)

	async def _close(a):
		s = getattr(a, "_infinity_session", None)
		if s and not s.closed:
			await s.close()

	async def _close_http(a):
		if _HTTP is not None and not _HTTP.closed:
			await _HTTP.close()

	async def _warm_stt(a):
		"""Прогрев локального Whisper в фоне: первый голосовой запрос не ждёт загрузки."""
		try:
			from frontend import voice
			info = await voice.warmup()
			logger.info("STT готова: %s", info)
		except Exception as e:
			logger.warning("STT warmup не удался: %s", e)

	async def _plugins_dir(a):
		"""Папка плагинов создаётся сразу: агент должен писать плагины «вслепую»."""
		try:
			PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
			logger.info("Папка плагинов: %s", PLUGINS_DIR)
		except Exception as e:
			logger.warning("Папку плагинов не создать (%s): %s", PLUGINS_DIR, e)
		# PY-плагины живут в той же папке, но исполняются на сервере, поэтому
		# загружаем их только здесь: до явного старта сервера .py не трогаем.
		try:
			_pyplugins.set_store_path(PLUGINS_DIR / ".pyplugins-store.json")
			_py_load_disabled()
			_pyp = _py_reload()
			loaded = ", ".join(_pyp.get("loaded") or []) or "нет"
			errs = _pyp.get("errors") or {}
			logger.info("PY-плагины: %s%s", loaded,
			            ("; ошибки: " + ", ".join(errs)) if errs else "")
		except Exception as e:
			logger.warning("PY-плагины не загрузились: %s", e)

	async def _start_watcher(a):
		a["_plugin_task"] = asyncio.create_task(_plugin_watcher())

	async def _stop_watcher(a):
		task = a.get("_plugin_task")
		if task and not task.done():
			task.cancel()
			try:
				await task
			except (asyncio.CancelledError, Exception):  # noqa: BLE001
				swallow.note('web_server._stop_watcher:L2801')
				pass

	app.on_startup.append(_plugins_dir)
	app.on_startup.append(_start_watcher)
	app.on_cleanup.append(_stop_watcher)
	app.on_startup.append(_warm_stt)
	app.on_startup.append(_start_hermes_update_task)
	app.on_cleanup.append(_close)
	app.on_cleanup.append(_close_http)

	app.router.add_get("/", handle_index)
	app.router.add_get("/infinity.html", handle_index)
	app.router.add_get("/api/health", handle_front_health)
	app.router.add_get("/api/commands", handle_commands)
	app.router.add_get("/favicon.ico", handle_favicon)

	# Локальный ИИ-движок llama.cpp (/api/ai-engine/*). Модуль опционален:
	# если его нет или он сломан, интерфейс просто не покажет вкладку.
	try:
		from . import ai_engine_api as _ai_engine_api
		_ai_engine_api.register_routes(app)
		app.on_startup.append(_ai_engine_api.autostart_on_boot)
	except Exception:
		logger.warning("ai-engine: модуль недоступен — вкладка настроек будет пустой", exc_info=True)

	# Локальные библиотеки (Monaco, xterm) — обычная статика, без токена.
	if VENDOR_DIR.is_dir():
		app.router.add_static("/vendor", VENDOR_DIR, append_version=False)
	else:
		logger.warning("vendor: папка %s не найдена — Monaco/xterm будут недоступны", VENDOR_DIR)

	# Стили и скрипты из frontend/assets. Тоже до catch-all.
	# Кэш отключаем по умолчанию: иначе после правки CSS/JS браузер продолжит
	# отдавать старую версию из собственного кэша и правки будут «не видны».
	if ASSETS_DIR.is_dir():
		@web.middleware
		async def _assets_no_store(request, handler):
			resp = await handler(request)
			# Только для /assets: правки CSS/JS должны быть видны сразу.
			if ASSETS_NO_STORE and request.path.startswith("/assets/"):
				resp.headers["Cache-Control"] = "no-store, must-revalidate"
				resp.headers["Pragma"] = "no-cache"
				resp.headers["Expires"] = "0"
			return resp

		app.router.add_static("/assets", ASSETS_DIR, append_version=False)
		app.middlewares.append(_assets_no_store)
	else:
		logger.warning("assets: папка %s не найдена — интерфейс будет без стилей и скриптов",
					   ASSETS_DIR)

	app.router.add_post("/api/voice/stt", handle_stt)
	app.router.add_post("/api/voice/tts", handle_tts)
	app.router.add_get("/api/voice/stt/status", handle_stt_status)
	app.router.add_post("/api/voice/check", handle_voice_check)
	app.router.add_get("/api/subagents", handle_subagents_list)
	app.router.add_get("/api/direct/config", handle_direct_config)
	# Крон-задачи агента: в этой сборке шлюза /api/cron/* отвечает 404, поэтому
	# монтируем свои обработчики поверх того же cronjob-инструмента.
	# Настройки шлюза для вкладки «Агент»: читаем и пишем config.yaml.
	app.router.add_get("/api/agent-config", handle_agent_config)
	app.router.add_post("/api/agent-config", handle_agent_config_save)
	app.router.add_get("/api/cron/jobs", handle_cron_list)
	app.router.add_get("/api/cron/runs", handle_cron_runs)
	app.router.add_post("/api/cron/{action}", handle_cron_action)
	# Перезапуск шлюза — чтобы настройки config.yaml вступили в силу.
	app.router.add_post("/api/gateway/restart", handle_gateway_restart)
	# Обновление Hermes с GitHub Releases (рубильник OFF по умолчанию).
	app.router.add_get("/api/hermes-update", handle_hermes_update_state)
	app.router.add_post("/api/hermes-update/config", handle_hermes_update_config)
	app.router.add_post("/api/hermes-update/check", handle_hermes_update_check)
	app.router.add_post("/api/hermes-update/download", handle_hermes_update_download)
	app.router.add_post("/api/hermes-update/install", handle_hermes_update_install)
	app.router.add_post("/api/hermes-update/rollback", handle_hermes_update_rollback)
	app.router.add_post("/api/audio/transcribe", handle_stt)  # alias, используется фронтом
	app.router.add_post("/api/fs/resolve", handle_fs_resolve)
	app.router.add_post("/api/fs/pick", handle_fs_pick)
	app.router.add_post("/api/fs/pick-dir", handle_fs_pick_dir)
	app.router.add_get("/api/fs/list", handle_fs_list)
	app.router.add_get("/api/fs/read-text", handle_fs_read_text)
	# Панель просмотра/правки файлов и сравнение с Git. Gateway этой сборки
	# может не знать эти роуты, поэтому держим их локально.
	app.router.add_post("/api/fs/write-text", handle_fs_write_text)
	app.router.add_get("/api/fs/read-data-url", handle_fs_read_data_url)
	app.router.add_get("/api/fs/download", handle_fs_download)
	app.router.add_get("/api/fs/git-root", handle_fs_git_root)
	# Рабочая папка агента: фронтенд спрашивает её при каждом открытии
	# панели файлов. В шлюзе этой сборки маршрута нет, поэтому отвечаем сами.
	app.router.add_get("/api/fs/default-cwd", handle_fs_default_cwd)
	# Наблюдение за файлом: панель просмотра обновляется по мере правки агентом
	app.router.add_get("/api/fs/watch", handle_fs_watch)
	# Сравнение с Git держим локально: в gateway этой сборки 404 на этот роут,
	# а кнопке «Принять изменения» он нужен.
	app.router.add_get("/api/git/file-diff", handle_git_file_diff)
	app.router.add_post("/api/exec", handle_exec)
	app.router.add_get("/api/exec/status", handle_exec_status)

	# Перехватчики сообщений (Настройки → Команды). Скрипт выполняется на сервере,
	# поэтому те же ограничения, что у /api/exec: только loopback.
	app.router.add_post("/api/intercept/run", handle_intercept_run)
	app.router.add_get("/api/intercept/status", handle_intercept_status)
	app.router.add_post("/api/intercept/read", handle_intercept_read)

	# Файлы инструкций агента (Настройки → Агент): SOUL.md, USER.md, AGENTS.md.
	# Только loopback и только имена из AGENT_FILES — произвольный путь не принимаем.
	app.router.add_get("/api/agent/files", handle_agent_files)
	app.router.add_get("/api/agent/file", handle_agent_file_read)
	app.router.add_post("/api/agent/file", handle_agent_file_write)
	# Системные инструкции — в файле, а не в настройках браузера
	app.router.add_get("/api/agent/prompt", handle_agent_prompt)
	app.router.add_post("/api/agent/prompt", handle_agent_prompt)

	# Состояние рабочей области на диске. Нужно, потому что состояние
	# агента (сессии, включённые плагины, заметки) больше не зависит от
	# origin браузера: /agentai и прямой :9120 — разные хранилища.
	app.router.add_get("/api/workspace/state", handle_workspace_state_get)
	app.router.add_post("/api/workspace/state", handle_workspace_state_post)

	# Плагины интерфейса: список/чтение/запись/удаление + SSE-наблюдение.
	# Регистрируются здесь, до catch-all — иначе ушли бы на gateway.
	app.router.add_get("/api/plugins/dir", handle_plugins_dir)
	app.router.add_get("/api/plugins/dir-pick", handle_plugins_dir_pick)
	app.router.add_get("/api/plugins/list", handle_plugins_list)
	app.router.add_get("/api/plugins/read", handle_plugins_read)
	app.router.add_get("/api/plugins/readme", handle_plugins_readme)
	app.router.add_get("/api/plugins/watch", handle_plugins_watch)
	app.router.add_post("/api/plugins/write", handle_plugins_write)
	app.router.add_post("/api/plugins/delete", handle_plugins_delete)
	app.router.add_post("/api/plugins/open", handle_plugins_open)
	app.router.add_post("/api/plugins/dir", handle_plugins_dir_set)

	# PY-плагины: исполняются на сервере, поэтому только loopback.
	app.router.add_get("/api/pyplugins/list", handle_pyplugins_list)
	app.router.add_get("/api/pyplugins/read", handle_pyplugins_read)
	app.router.add_get("/api/pyplugins/logs", handle_pyplugins_logs)
	app.router.add_post("/api/pyplugins/write", handle_pyplugins_write)
	app.router.add_post("/api/pyplugins/delete", handle_pyplugins_delete)
	app.router.add_post("/api/pyplugins/toggle", handle_pyplugins_toggle)
	app.router.add_post("/api/pyplugins/reload", handle_pyplugins_reload)
	app.router.add_post("/api/pyplugins/command", handle_pyplugins_command)
	app.router.add_post("/api/pyplugins/emit", handle_pyplugins_emit)
	app.router.add_get("/api/pyplugins/run/{path}", handle_pyplugins_run)

	# Резервный список инструментов/навыков с диска — на случай, когда
	# шлюз не отдаёт /v1/toolsets.
	app.router.add_get("/api/tools/local", handle_tools_local)

	app.router.add_route("*", "/api/sessions/{sid}/chat/stream",
						lambda r: _proxy(r, stream=True))
	app.router.add_route("*", "/api/sessions/{sid}/chat",
						lambda r: _proxy(r))
	# Возврат в чат после ухода с модуля/перезагрузки: узнать о живом ходе и
	# подключиться к нему заново (SSE с повтором событий с начала).
	app.router.add_get("/api/sessions/{sid}/active-run",
					   lambda r: _proxy(r))
	app.router.add_route("*", "/api/sessions/{sid}/stream",
						lambda r: _proxy(r, stream=True))
	app.router.add_route("*", "/api/{tail:.*}", lambda r: _proxy(r))
	app.router.add_route("*", "/v1/{tail:.*}", lambda r: _proxy(r))
	return app


def run() -> None:
	logging.basicConfig(level=logging.INFO,
						format="%(asctime)s %(name)s %(levelname)s %(message)s")
	logger.info("Infinity frontend on http://127.0.0.1:%d (gateway: %s)", FRONT_PORT, _gw_base())
	web.run_app(build_app(), host="0.0.0.0", port=FRONT_PORT, print=None)


if __name__ == "__main__":
	run()