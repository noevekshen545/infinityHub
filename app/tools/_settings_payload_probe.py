"""Живая проверка Настроек: каждое поле формы доезжает до payload /api/config/save.

Почему так, а не POST на живой сервер: тот путь пишет реальный профиль.
Страница отдаётся локальным сервером из app/ (шаблон читается с диска —
[main.py] кэширует PAGE_SETTINGS при старте, поэтому живой 8281 может отдавать
устаревшую разметку), а все /api/** перехватываются: GET /api/config отдаёт
фикстуру, POST /api/config/save перехватывается и не уходит на сервер.

Что доказывается: для каждого поля задаётся уникальное значение, нажимается
«Сохранить», и в перехваченном payload оно обязано появиться по своему
пути в конфиге.

Разметка сворачивается через `i18n.render(..., "ru")`: с 05.11 шаблон хранит
ключи перевода, а серверного слоя на этом стенде нет — для ru-профиля слой
делает ровно то же самое, байт в байт.
"""
import json
import os
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from playwright.sync_api import sync_playwright

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)
import i18n  # noqa: E402
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_settings_payload.json")
PORT = 8477
BASE = "http://127.0.0.1:%d" % PORT

CFG = {
    "backend": "lmstudio",
    "lm_studio": {
        "url": "http://localhost:1234", "model": "m1", "embed_model": "e1",
        "api_key": "", "timeout": 120, "context_tokens_default": 16000,
        "max_tokens_default": 4096, "temperature_default": 0.7,
        "model_translator": "t1", "model_search_chat": "s1", "model_ask_chat": "a1",
        "model_enhance_query": "q1", "model_fix_errors": "f1", "model_summarize": "z1",
    },
    "llama_cpp": {"url": "http://localhost:8080", "port": 8080},
    "custom": {"url": "", "api_key": "", "model": ""},
    "custom_providers": [], "custom_active": "",
    "data_dirs": [], "books_dir": "", "books_dirs": [], "books_db_path": "",
    "notes_dir": "", "ai_dir": "", "memory_dir": "", "chat_dir": "",
    "attachments_dir": "", "plugins_dir": "", "zim_dirs": [],
    "html_viewer_dirs": [], "apps_dirs": [], "media": {},
    "ui": {"theme": "dark", "language": "ru"},
    "speech": {"tts_engine": "auto", "stt_engine": "local",
               "tts": {}, "stt": {"local_model": "small", "local_language": "ru"}},
    "auth": {"enabled": False, "username": "admin", "has_password": True, "has_token": False},
    "search": {"limit": 50, "bm25_limit": 50, "faiss_limit": 50,
               "use_embeddings": False, "cross_encoder_enabled": True,
               "spell_correction_enabled": False, "recency_boost_enabled": True},
    "ai": {},
    "research": {"research_model": "gemma-3-4b-it", "final_model": "",
                 "iterations": 15, "max_final_tokens": 20000},
    "stats": {"enabled": True, "notifications_enabled": True,
              "ai_recommendations": False, "max_notifications_per_day": 3},
    "is_public_profile": False, "public_paths_enabled": {}, "public_paths": {}, "own_paths": {},
}

# Галки «показывать данные общедоступного профиля» читаются своим маршрутом:
# GET /api/config — белый список ключей, нового поля в нём нет. Значения
# фикстуры намеренно смешанные, чтобы OFF и ON проверялись разными случаями.
PUBDATA = {"profile_id": "probe", "is_public": False,
           "categories": ["notes", "chat", "ai_answers", "attachments",
                          "extensions", "bookmarks"],
           "flags": {"notes": True, "chat": False, "ai_answers": True,
                     "attachments": False, "extensions": True, "bookmarks": False}}

# Чекбоксы карточки: (id, путь в payload, что кликаем, что обязано приехать).
# Категория без клика проходит как есть — так видно, что словарь уезжает
# целиком и снятые галки не теряются. extensions на странице отключён
# (расширения ещё не читают каталог профиля), поэтому он обязан остаться
# таким, каким пришёл с сервера.
CHECKS = [
    ("pubData_notes", ("public_data_enabled", "notes"), True, "false"),
    ("pubData_chat", ("public_data_enabled", "chat"), True, "true"),
    ("pubData_ai_answers", ("public_data_enabled", "ai_answers"), False, "true"),
    ("pubData_attachments", ("public_data_enabled", "attachments"), False, "false"),
    ("pubData_bookmarks", ("public_data_enabled", "bookmarks"), False, "false"),
    ("pubData_extensions", ("public_data_enabled", "extensions"), False, "true"),
]

# id поля -> путь в конфиге, по которому значение обязано появиться,
# и значение, которое надо ввести.
CASES = [
    ("maxTokens", ("lm_studio", "max_tokens_default"), "3210"),
    ("temperature", ("lm_studio", "temperature_default"), "0.35"),
    ("contextTokensDefault", ("lm_studio", "context_tokens_default"), "12345"),
    ("lmStudioTimeout", ("lm_studio", "timeout"), "77"),
    ("modelAskChat", ("lm_studio", "model_ask_chat"), "ask-x"),
    ("modelSearchChat", ("lm_studio", "model_search_chat"), "search-x"),
    ("modelTranslate", ("lm_studio", "model_translator"), "trans-x"),
    ("modelEnhanceQuery", ("lm_studio", "model_enhance_query"), "enh-x"),
    ("modelFixErrors", ("lm_studio", "model_fix_errors"), "fix-x"),
    ("modelSummarize", ("lm_studio", "model_summarize"), "sum-x"),
    ("researchIterations", ("research", "iterations"), "7"),
    ("maxFinalTokens", ("research", "max_final_tokens"), "9999"),
    ("searchLimit", ("search", "limit"), "42"),
    ("maxNotifsPerDay", ("stats", "max_notifications_per_day"), "5"),
    ("sttModel", ("speech", "stt", "local_model"), ""),
    ("sttLanguage", ("speech", "stt", "local_language"), "auto"),
    # --- всё остальное, что рисует страница: раньше провернялась только
    # численная половина, а «поле есть, но не сохраняется» живёт как раз в
    # строковых путях, каталогах и секции речи.
    ("llmBackend", ("backend",), "lmstudio"),
    ("lmStudioUrl", ("lm_studio", "url"), "http://probe:1234"),
    ("lmStudioApiKey", ("lm_studio", "api_key"), "probe-key"),
    ("lmModel", ("lm_studio", "model"), "probe-model"),
    ("lmEmbedModel", ("lm_studio", "embed_model"), "probe-embed"),
    ("llamacppUrl", ("llama_cpp", "url"), "http://probe:8080"),
    ("llamacppPort", ("llama_cpp", "port"), "8081"),
    ("customUrl", ("custom", "url"), "http://probe-custom/v1"),
    ("customApiKey", ("custom", "api_key"), "probe-custom-key"),
    ("customModel", ("custom", "model"), "probe-custom-model"),
    ("dataDirs", ("data_dirs",), "E:/probe/data1, E:/probe/data2"),
    ("notesDir", ("notes_dir",), "E:/probe/notes"),
    ("aiDir", ("ai_dir",), "E:/probe/ai"),
    ("memoryDir", ("memory_dir",), "E:/probe/memory"),
    ("chatDir", ("chat_dir",), "E:/probe/chat"),
    ("attachmentsDir", ("attachments_dir",), "E:/probe/attach"),
    ("pluginsDir", ("plugins_dir",), "E:/probe/plugins"),
    ("booksDbPath", ("books_db_path",), "E:/probe/collection.db"),
    # Каталог-секция: профиль в фикстуре обычный (is_public_profile: false),
    # поэтому свои пути уходят как private_*, а списки — списками.
    ("booksDir", ("private_books_dir",), "E:/probe/books"),
    ("zimDirs", ("private_zim_dirs",), "E:/probe/zim1, E:/probe/zim2"),
    ("htmlViewerDirs", ("private_html_viewer_dirs",), "E:/probe/html"),
    ("appsDirs", ("private_apps_dirs",), "E:/probe/apps"),
    ("musicDirs", ("private_music_dirs",), "E:/probe/music"),
    ("videoDirs", ("private_video_dirs",), "E:/probe/video"),
    ("photoDirs", ("private_photos_dirs",), "E:/probe/photo"),
    ("uiTheme", ("ui", "theme"), "probe-theme"),
    ("uiLanguage", ("ui", "language"), "en"),
    ("ttsEngine", ("speech", "tts_engine"), "silero"),
    ("ttsSileroSpeaker", ("speech", "tts", "silero_speaker"), "baya"),
    ("ttsPiperVoice", ("speech", "tts", "piper_voice"), "probe-piper"),
    ("ttsQwen3Url", ("speech", "tts", "qwen3_url"), "http://probe:7860"),
    ("ttsQwen3Speaker", ("speech", "tts", "qwen3_speaker"), "probe-speaker"),
    ("ttsRate", ("speech", "tts", "rate"), "1.25"),
    ("ttsPitch", ("speech", "tts", "pitch"), "0.5"),
    ("sttEngine", ("speech", "stt_engine"), "openai"),
    ("sttOpenaiBaseUrl", ("speech", "stt", "openai_base_url"), "http://probe-stt/v1"),
    ("sttOpenaiKey", ("speech", "stt", "openai_api_key"), "probe-stt-key"),
    ("sttOpenaiModel", ("speech", "stt", "openai_model"), "probe-whisper"),
    ("bm25Limit", ("search", "bm25_limit"), "43"),
    ("faissLimit", ("search", "faiss_limit"), "44"),
    ("researchModel", ("research", "research_model"), "probe-research"),
    ("finalModel", ("research", "final_model"), "probe-final"),
]

# Часть полей страница пересобирает сама, поэтому ожидаемое значение в payload
# отличается от введённого.
EXPECTED = {
    # Поле порта перестраивает URL, сохраняя хост: введено http://probe:8080,
    # порт 8081 из следующего случая.
    "llamacppUrl": "http://probe:8081",
    # Адрес провайдера нормализуют к базе без /v1 — сервер дописывает
    # /v1/chat/completions сам (app/config_module.py:486).
    "customUrl": "http://probe-custom",
}

# Переключатели — не .value, а наличие класса `on`; значение обязано уйти
# булевым. В фикстуре их состояние тоже задано явно, чтобы «вкл» и «выкл»
# проверялись разными случаями.
TOGGLES = [
    ("toggleEmbed", ("search", "use_embeddings"), "true"),
    ("toggleCrossEncoder", ("search", "cross_encoder_enabled"), "false"),
    ("toggleSpell", ("search", "spell_correction_enabled"), "true"),
    ("toggleRecency", ("search", "recency_boost_enabled"), "false"),
    ("toggleStatsEnabled", ("stats", "enabled"), "true"),
    ("toggleNotifsEnabled", ("stats", "notifications_enabled"), "false"),
    ("toggleAiRecs", ("stats", "ai_recommendations"), "true"),
    ("toggleSelectionTTS", ("speech", "selection_tts_enabled"), "false"),
    ("toggleVoiceInput", ("speech", "voice_input_enabled"), "true"),
]

# UI-настройки профиля: идут не в /api/config/save, а во frontend_state
# (/api/profiles/save-state). Проверяется, что слайдер реально доезжает туда.
STATE_CASES = [
    ("deskPanelOpacity", "desk_panel_transparency", "33"),
    ("deskMenuOpacity", "desk_menu_transparency", "44"),
    ("deskIconScale", "desk_icon_scale", "110"),
    ("deskTextScale", "desk_text_scale", "120"),
]



class H(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/settings"):
            p = os.path.join(APP, "templates", "page_settings.html")
            # Шаблон с 05.11 хранит ключи перевода; на стенде слоя нет, поэтому
            # сворачиваем токены тем же кодом, что и сервер для ru-профиля.
            with open(p, encoding="utf-8") as f:
                body = i18n.render(f.read(), "ru").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()

    def log_message(self, *a):
        pass


def dig(cfg, path):
    cur = cfg
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return "<missing>"
        cur = cur[k]
    return cur


def as_str(got):
    """Сравнение по строке: списки формы приходят массивами, булевы значения
    переключателей — bool, числа cfgNum приводит к int."""
    if isinstance(got, bool):
        return str(got).lower()
    if isinstance(got, list):
        return ", ".join(str(x) for x in got)
    return "" if got is None else str(got).strip()


def main():
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), partial(H, directory=APP))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    saved = []
    states = []
    master = {}
    clicked = False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()

            def handle(route):
                url = route.request.url
                if "/api/profiles/save-state" in url:
                    try:
                        states.append(json.loads(route.request.post_data or "{}"))
                    except Exception as e:
                        states.append({"__parse_error__": str(e)})
                    route.fulfill(status=200, content_type="application/json",
                                  body='{"ok": true}')
                    return
                if "/api/config/save" in url:
                    try:
                        saved.append(json.loads(route.request.post_data or "{}"))
                    except Exception as e:
                        saved.append({"__parse_error__": str(e)})
                    route.fulfill(status=200, content_type="application/json",
                                  body='{"ok": true, "applied_live": true}')
                    return
                if url.rstrip("/").endswith("/api/config"):
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps({"config": CFG}, ensure_ascii=False))
                    return
                if "/api/profiles/public-data" in url:
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps(PUBDATA, ensure_ascii=False))
                    return
                route.fulfill(status=200, content_type="application/json", body="{}")

            page.route("**/api/**", handle)
            page.goto(BASE + "/settings", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            # sendBeacon не проходит через route()-перехват, а UI-настройки
            # уходят именно им: заменяем на fetch, чтобы save-state был виден.
            page.evaluate("""() => {
                navigator.sendBeacon = (u, b) => {
                    fetch(u, {method: 'POST', body: b,
                              headers: {'Content-Type': 'application/json'}});
                    return true;
                };
            }""")

            for fid, _path, val in CASES:
                el = page.query_selector("#" + fid)
                if el is None:
                    print("НЕТ ПОЛЯ:", fid)
                    continue
                # Не fill(): часть полей лежит на неактивных вкладках
                # (display:none), а saveConfig() читает .value без проверки
                # видимости.
                el.evaluate("""(e, v) => {
                    if (e.tagName === 'SELECT' && ![...e.options].some(o => o.value === v)) {
                        const o = document.createElement('option');
                        o.value = v; o.textContent = v; e.appendChild(o);
                    }
                    e.value = v;
                    e.dispatchEvent(new Event('input', {bubbles: true}));
                    e.dispatchEvent(new Event('change', {bubbles: true}));
                }""", val)

            for fid, _path, want in TOGGLES:
                el = page.query_selector("#" + fid)
                if el is None:
                    print("НЕТ ПЕРЕКЛЮЧАТЕЛЯ:", fid)
                    continue
                # Переключатель страницы — это div с классом `on`, а не чекбокс:
                # saveConfig читает classList.contains('on').
                el.evaluate("(e, on) => e.classList.toggle('on', on === 'true')", want)

            for fid, _path, do_click, _want in CHECKS:
                el = page.query_selector("#" + fid)
                if el is None:
                    print("НЕТ ЧЕКБОКСА:", fid)
                    continue
                if not do_click:
                    continue
                # Не el.click(): карточка лежит на неактивной вкладке
                # (display:none), а обработчик висит на событии change.
                el.evaluate("""(e) => {
                    e.checked = !e.checked;
                    e.dispatchEvent(new Event('change', {bubbles: true}));
                }""")

            for fid, _key, val in STATE_CASES:
                el = page.query_selector("#" + fid)
                if el is None:
                    print("НЕТ СЛАЙДЕРА:", fid)
                    continue
                el.evaluate("""(e, v) => {
                    e.value = v;
                    e.dispatchEvent(new Event('input', {bubbles: true}));
                    e.dispatchEvent(new Event('change', {bubbles: true}));
                }""", val)

            clicked = page.evaluate("""() => {
                const b = [...document.querySelectorAll('button')]
                  .find(x => /Сохранить настройки/i.test(x.textContent));
                if (b) { b.click(); return true; }
                return false;
            }""")
            page.wait_for_timeout(2500)
            # Мастер-галка проверяется уже после «Сохранить»: она трогает все
            # чекбоксы, а payload к этому моменту перехвачен.
            master = page.evaluate("""() => {
                const m = document.querySelector('#pubDataAll');
                if (!m) return {found: false};
                const kinds = ['notes','chat','ai_answers','attachments',
                               'bookmarks','extensions'];
                const snap = () => {
                    const o = {};
                    for (const k of kinds) {
                        const b = document.querySelector('#pubData_' + k);
                        o[k] = !!b && b.checked;
                    }
                    return o;
                };
                const set = (v) => {
                    m.checked = v;
                    m.dispatchEvent(new Event('change', {bubbles: true}));
                    return snap();
                };
                const off = set(false), on = set(true);
                return {found: true, off: off, on: on, kinds: kinds,
                        extDisabled: !!document.querySelector('#pubData_extensions').disabled};
            }""")
            browser.close()
    finally:
        httpd.shutdown()

    if not saved:
        print("ПЕРЕХВАТ НЕ СРАБОТАЛ (кнопка найдена: %s)" % clicked)
        return 1

    payload = saved[-1].get("config", saved[-1])
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    fails = []
    for fid, path, val in list(CASES) + list(TOGGLES):
        got = dig(payload, path)
        exp = EXPECTED.get(fid, val)
        ok = as_str(got) == (exp.lower() if isinstance(got, bool) else exp)
        print(("OK   " if ok else "FAIL ") + fid + "  " + ".".join(path)
              + "  ожидалось=" + repr(exp) + "  пришло=" + repr(got))
        if not ok:
            fails.append(fid)

    for fid, path, _clicked, want in CHECKS:
        got = dig(payload, path)
        ok = as_str(got) == want
        print(("OK   " if ok else "FAIL ") + fid + "  " + ".".join(path)
              + "  ожидалось=" + repr(want) + "  пришло=" + repr(got))
        if not ok:
            fails.append(fid)

    enabled = [k for k in master.get("kinds", []) if k != "extensions"]
    ext_keep = PUBDATA["flags"]["extensions"]
    off_ok = bool(enabled) and all(master["off"][k] is False for k in enabled)
    on_ok = bool(enabled) and all(master["on"][k] is True for k in enabled)
    # Отключённую категорию мастер не трогает: включённая им галка обещала бы
    # то, что сервер ещё не читает (расширения живут общим каталогом).
    ext_ok = (master.get("extDisabled") is True
              and master.get("off", {}).get("extensions") == ext_keep
              and master.get("on", {}).get("extensions") == ext_keep)
    if not master.get("found"):
        print("FAIL pubDataAll  мастер-галка на странице не найдена")
        fails.append("pubDataAll")
    else:
        print(("OK   " if (off_ok and on_ok and ext_ok) else "FAIL ")
              + "pubDataAll  включает и снимает все активные категории, "
                "отключённую не трогает")
        if not (off_ok and on_ok and ext_ok):
            fails.append("pubDataAll")

    state = {}
    for s in states:
        state.update(s.get("frontend_state") or {})
    if not states:
        print("BAD  save-state не перехвачен — UI-настройки не проверялись")
        fails.append("save-state")
    for fid, key, val in STATE_CASES:
        got = state.get(key)
        ok = as_str(got) == val
        print(("OK   " if ok else "FAIL ") + fid + "  frontend_state." + key
              + "  ожидалось=" + repr(val) + "  пришло=" + repr(got))
        if not ok:
            fails.append(fid)

    for bad in ("auth", "ai"):
        present = bad in payload
        print(("BAD  " if present else "OK   ") + "секция '" + bad
              + "' в payload отсутствует")
        if present:
            fails.append("section:" + bad)

    total = len(CASES) + len(TOGGLES) + len(STATE_CASES) + len(CHECKS) + 3
    print("ИТОГ: провалов %d из %d" % (len(fails), total))
    return 0 if not fails else 2


if __name__ == "__main__":
    sys.exit(main())
