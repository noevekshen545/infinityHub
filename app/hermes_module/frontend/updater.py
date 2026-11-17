"""Safe updater for Hermes backend in Infinity Isolated."""
import os
import shutil
import sys
import time
from pathlib import Path
try:
    import swallow                      # внутри приложения: след в общий лог
except ImportError:
    # модуль умеет работать и вне приложения — тогда следа нет, но и падения нет
    class swallow:
        @staticmethod
        def note(*a, **k):
            pass

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "update"
DST = ROOT / "backend"
BACKUPS = ROOT / "backups"

# Папки/файлы, которые исключаются из обновления (остаются в backend как есть)
EXCLUDE_DIRS = {
    "__pycache__", ".git", "node_modules", "out",
    "hermes_agent.egg-info", ".github", "website"
}
EXCLUDE_EXTS = {".pyc", ".pyo"}

# ==============================================================================
# Локальные патчи Infinity — применяются ПОСЛЕ копирования из update/.
# Каждый патч идемпотентен: если в файле уже есть маркер, он пропускается.
# Если нужный фрагмент не найден (новая версия его изменила) — выводится
# предупреждение, но обновление не прерывается.
# ==============================================================================
PATCHES = [
    (
        "gateway/platforms/api_server.py",
        "assistant.reasoning",
        [
            (
                "reasoning-stream",
                '''        def _delta(delta: str) -> None:
            if delta:
                events.enqueue("assistant.delta", {"message_id": message_id, "delta": delta})

        def _tool_progress(event_type: str, tool_name: str | None = None, preview: str | None = None, args=None, **kwargs) -> None:
            if event_type == "reasoning.available":
                events.enqueue("tool.progress", {"message_id": message_id, "tool_name": tool_name or "_thinking", "delta": preview or ""})''',
                '''        got_reasoning = [False]  # Infinity: живой поток рассуждений уже показан -> снапшот не дублируем

        def _delta(delta: str) -> None:
            if delta:
                events.enqueue("assistant.delta", {"message_id": message_id, "delta": delta})

        def _reasoning(delta: str) -> None:
            # Infinity: настоящий стрим рассуждений (reasoning_content) -> в чат отдельным событием.
            if delta:
                got_reasoning[0] = True
                events.enqueue("assistant.reasoning", {"message_id": message_id, "delta": delta})

        def _tool_progress(event_type: str, tool_name: str | None = None, preview: str | None = None, args=None, **kwargs) -> None:
            if event_type == "reasoning.available":
                if got_reasoning[0]:
                    return
                events.enqueue("tool.progress", {"message_id": message_id, "tool_name": tool_name or "_thinking", "delta": preview or ""})''',
            ),
            (
                "reasoning-wire",
                '''                result, usage = await self._run_agent(
                    conversation_history=history, stream_delta_callback=_delta,
                    tool_progress_callback=_tool_progress, interim_assistant_callback=_commentary,
                    active_run_id=run_id, approval_notify_callback=approval_notify,
                    approval_session_key=run_id, **ctx["run_kwargs"])''',
                '''                result, usage = await self._run_agent(
                    conversation_history=history, stream_delta_callback=_delta,
                    reasoning_callback=_reasoning,
                    tool_progress_callback=_tool_progress, interim_assistant_callback=_commentary,
                    active_run_id=run_id, approval_notify_callback=approval_notify,
                    approval_session_key=run_id, **ctx["run_kwargs"])''',
            ),
        ],
    ),
    (
        "gateway/platforms/api_server.py",
        "session_truncate",
        [
            (
                "truncate-feature-flag",
                '''    "session_chat_streaming": True, "session_fork": True, "session_model_lock": True,
    "reasoning_streaming": True,''',
                '''    "session_chat_streaming": True, "session_fork": True, "session_truncate": True,
    "session_model_lock": True,
    "reasoning_streaming": True,''',
            ),
            (
                "truncate-capability",
                '''    ("session_fork", ("POST", "/api/sessions/{session_id}/fork")),
    ("session_chat", ("POST", "/api/sessions/{session_id}/chat")),''',
                '''    ("session_fork", ("POST", "/api/sessions/{session_id}/fork")),
    ("session_truncate", ("POST", "/api/sessions/{session_id}/truncate")),
    ("session_chat", ("POST", "/api/sessions/{session_id}/chat")),''',
            ),
            (
                "truncate-route",
                '''            ("POST", "/api/sessions/{session_id}/fork", self._handle_fork_session),
            ("POST", "/api/sessions/{session_id}/chat", self._handle_session_chat),''',
                '''            ("POST", "/api/sessions/{session_id}/fork", self._handle_fork_session),
            ("POST", "/api/sessions/{session_id}/truncate", self._handle_truncate_session),
            ("POST", "/api/sessions/{session_id}/chat", self._handle_session_chat),''',
            ),
            (
                "truncate-handler",
                '''        fork = await asyncio.to_thread(db.get_session, fork_id) or {"id": fork_id, "parent_session_id": source_id}
        return web.json_response({"object": "hermes.session", "session": self._session_response(fork)}, status=201)

    async def _prepare_session_chat(self, request: web.Request) -> tuple:''',
                '''        fork = await asyncio.to_thread(db.get_session, fork_id) or {"id": fork_id, "parent_session_id": source_id}
        return web.json_response({"object": "hermes.session", "session": self._session_response(fork)}, status=201)

    @_require_auth
    async def _handle_truncate_session(self, request: web.Request) -> web.Response:
        """POST /api/sessions/{session_id}/truncate — keep the target message, archive everything after.

        Rewind-style soft delete (``active = 0``), so the dropped tail stays recoverable. Used by the
        web UI's "rewind to this message": older context must leave the model's transcript too, not
        just the rendered chat. Refused while a live turn holds the session (409).
        """
        session_id = request.match_info["session_id"]
        _, err = await self._get_existing_session_or_404(session_id)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        try:
            message_id = int(body.get("after_message_id"))
        except (TypeError, ValueError):
            return _error_response("after_message_id must be an integer", 400, code="invalid_message_id")
        if message_id <= 0:
            return _error_response("after_message_id must be positive", 400, code="invalid_message_id")
        db = await self._ensure_session_db_async()
        resolved_id = await asyncio.to_thread(db.resolve_resume_session_id, session_id)
        try:
            result = await asyncio.to_thread(db.truncate_after_message, resolved_id, message_id)
        except ValueError as exc:
            return _error_response(str(exc), 404, code="message_not_found")
        except RuntimeError as exc:
            return _error_response(str(exc), 409, code="session_busy")
        session = await asyncio.to_thread(db.get_session, resolved_id) or {}
        return web.json_response({
            "object": "hermes.session.truncated", "session_id": resolved_id,
            "truncated_count": result["truncated_count"], "new_head_id": result["new_head_id"],
            "message_count": session.get("message_count")})

    async def _prepare_session_chat(self, request: web.Request) -> tuple:''',
            ),
        ],
    ),
    (
        "gateway/session_api_turn.py",
        "Infinity: the session SSE gets clarify prompts too",
        [
            (
                "clarify-sink",
                '''    def sink(event_type, prompt):
        if event_type == 'approval.request':
            choices = prompt.get('choices') or ()
            approval_notify_callback({
                **{k: v for k, v in prompt.items() if k not in ('kind', 'prompt_id', 'choices')},
                'request_id': prompt['prompt_id'], 'allow_session': 'session' in choices,
                'allow_permanent': 'always' in choices})''',
                '''    def sink(event_type, prompt):
        # Infinity: the session SSE gets clarify prompts too, not only approvals — otherwise a
        # clarify tool call blocks the turn with nothing visible or answerable in the web chat.
        choices = prompt.get('choices') or ()
        payload = {k: v for k, v in prompt.items() if k not in ('kind', 'prompt_id', 'choices')}
        payload['request_id'] = prompt.get('prompt_id')
        if event_type == 'approval.request':
            payload.update({'allow_session': 'session' in choices, 'allow_permanent': 'always' in choices})
        elif event_type == 'clarify.request':
            payload['choices'] = list(choices)
        payload['_control'] = event_type
        approval_notify_callback(payload)''',
            ),
        ],
    ),
    (
        "gateway/platforms/api_server.py",
        "Infinity: the same sink carries clarify prompts",
        [
            (
                "clarify-notify",
                '''        def _approval_notify(approval_data: dict[str, Any]) -> None:
            event = _approval_request_event(run_id, approval_data, message_id=message_id)
            self._set_run_status(run_id, "waiting_for_approval", last_event="approval.request", approval=event)
            events.enqueue("approval.request", event)  # executor thread -> loop hop inside
        return _approval_notify''',
                '''        def _approval_notify(approval_data: dict[str, Any]) -> None:
            # Infinity: the same sink carries clarify prompts and both settled events, so the web
            # chat can render and answer a clarify tool call instead of blocking until its timeout.
            from gateway.platforms.api_server_runs import _run_event
            control = str(approval_data.get("_control") or "approval.request")
            data = {k: v for k, v in approval_data.items() if k != "_control"}
            if control == "clarify.request":
                choices = list(data.pop("choices", []) or [])
                event = _run_event(run_id, "clarify.request", message_id=message_id,
                                   choices=choices, **data)
                self._set_run_status(run_id, "waiting_for_approval",
                                     last_event="clarify.request", clarify=event)
                events.enqueue("clarify.request", event)
                return
            if control in ("approval.settled", "clarify.settled"):
                event = _run_event(run_id, control, message_id=message_id, **data)
                if (self._run_statuses.get(run_id) or {}).get("status") == "waiting_for_approval":
                    self._set_run_status(run_id, "running", last_event=control)
                events.enqueue(control, event)
                return
            event = _approval_request_event(run_id, data, message_id=message_id)
            self._set_run_status(run_id, "waiting_for_approval", last_event="approval.request", approval=event)
            events.enqueue("approval.request", event)  # executor thread -> loop hop inside
        return _approval_notify''',
            ),
        ],
    ),
    (
        "hermes_state_messages.py",
        "def truncate_after_message",
        [
            (
                "truncate-after-message",
                '''    def rewind_to_message(self, session_id: str, target_message_id: int, *, preserve_compaction_handoff: bool = False,''',
                '''    def truncate_after_message(self, session_id: str, message_id: int) -> dict[str, Any]:
        """Soft-delete (``active = 0``) every message AFTER *message_id*, keeping the target.

        The UI's "rewind to this message" cut: the target stays in the transcript (and in the
        model's context next turn), everything below it leaves both. Rewind-style archiving —
        not DELETE — so the dropped turns stay on disk with ``active = 0`` and can be recovered.
        Refuses while a live turn lease or a compression lock is held (same guards as
        :meth:`rewind_to_message`); resets the session counters so the next turn sees the
        shortened transcript. Returns ``{"truncated_count", "new_head_id", "target_message"}``.
        """
        def _do(conn):
            self._check_transcript_write_guards(
                conn, session_id, None, reject_active_turn_lease=True, reject_active_compression_lock=True)
            row = conn.execute(
                "SELECT * FROM messages WHERE id = ? AND session_id = ?", (message_id, session_id)).fetchone()
            if row is None:
                raise ValueError(f"message {message_id} not found in session {session_id}")
            target_row = dict(row)
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM messages WHERE session_id = ? AND id > ? AND active = 1",
                (session_id, message_id)).fetchall()]
            if ids:
                conn.execute(f"UPDATE messages SET active = 0 WHERE id IN ({_placeholders(ids)})", ids)
            conn.execute(
                "UPDATE sessions SET rewind_count = COALESCE(rewind_count, 0) + 1 WHERE id = ?", (session_id,))
            message_count, tool_call_count = self._active_transcript_counts(conn, session_id)
            conn.execute(f"{_SET_COUNTERS_SQL} WHERE id = ?", (message_count, tool_call_count, session_id))
            head_id = conn.execute(
                "SELECT MAX(id) FROM messages WHERE session_id = ? AND active = 1", (session_id,)).fetchone()[0]
            return target_row, ids, head_id
        target_row, truncated, new_head_id = self._execute_write(_do)
        target_row["content"] = self._decode_content(target_row.get("content"))
        return {"truncated_count": len(truncated), "new_head_id": new_head_id,
                "target_message": target_row}

    def rewind_to_message(self, session_id: str, target_message_id: int, *, preserve_compaction_handoff: bool = False,''',
            ),
        ],
    ),
    (
        "tools/skills_tool.py",
        "include_editorial: bool = False",
        [
            (
                "skills-include-editorial",
                '''def find_all_skills(*, skip_disabled: bool = False) -> list[dict[str, Any]]:''',
                '''def find_all_skills(*, skip_disabled: bool = False,
                    include_editorial: bool = False) -> list[dict[str, Any]]:''',
            ),
        ],
    ),
    (
        "gateway/platforms/api_server.py",
        "title-too-long-400",
        [
            (
                "title-too-long-400",
                '''            if title is not None:
                clean_title = db.sanitize_title(str(title))''',
                '''            if title is not None:
                try:
                    clean_title = db.sanitize_title(str(title))
                except ValueError as exc:  # Infinity: title-too-long-400 — длинный заголовок это 400, а не 500
                    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                    return None, f"title:{exc}"''',
            ),
        ],
    ),
]


def apply_local_patches() -> int:
    """Применить локальные патчи Infinity к обновлённым файлам. Возвращает число правок."""
    applied = 0
    print("[*] Применяю локальные патчи Infinity (reasoning-стрим, truncate и clarify сессии, include_editorial, длинный заголовок)…")
    for rel, marker, changes in PATCHES:
        path = DST / rel
        if not path.exists():
            print(f"    [!] Файл не найден, патч пропущен: {rel}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"    [!] Не удалось прочитать {rel}: {e}")
            continue
        if marker in text:
            print(f"    [=] уже применён: {rel} ({marker})")
            continue
        for name, old, new in changes:
            if old in text:
                text = text.replace(old, new, 1)
                applied += 1
                print(f"    [+] патч «{name}» -> {rel}")
            else:
                print(f"    [!] фрагмент «{name}» не найден в {rel} — обновите патч под новую версию")
        if marker in text:
            try:
                path.write_text(text, encoding="utf-8")
            except Exception as e:
                print(f"    [!] Не удалось записать {rel}: {e}")
    return applied


def is_running():
    import socket
    for port in (8642, 9120):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            try:
                s.connect(("127.0.0.1", port))
                return True
            except OSError:
                swallow.note('updater.is_running:L148')
                pass
    return False


def get_version(folder: Path):
    pt = folder / "pyproject.toml"
    if pt.exists():
        try:
            for line in pt.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip().startswith("version"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            swallow.note('updater.get_version:L161')
            pass
    # Фолбэк на PKG-INFO внутри egg-info
    for egg in folder.glob("*.egg-info"):
        pkg = egg / "PKG-INFO"
        if pkg.exists():
            try:
                for line in pkg.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("Version:"):
                        return line.split(":", 1)[1].strip()
            except Exception:
                swallow.note('updater.get_version:L172')
                pass
    return "неизвестно"


def backup_backend():
    ts = time.strftime("%Y%m%d-%H%M%S")
    bdir = BACKUPS / f"hermes-{ts}"
    print(f"[*] Создаю резервную копию backend -> {bdir}...")
    bdir.mkdir(parents=True, exist_ok=True)

    copied = 0
    for root, dirs, files in os.walk(DST):
        rel_root = Path(root).relative_to(DST)
        if any(part in EXCLUDE_DIRS for part in rel_root.parts):
            dirs.clear()
            continue
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]

        target_root = bdir / rel_root
        target_root.mkdir(parents=True, exist_ok=True)
        for f in files:
            if any(f.endswith(ext) for ext in EXCLUDE_EXTS):
                continue
            sf = Path(root) / f
            df = target_root / f
            try:
                shutil.copy2(sf, df)
                copied += 1
            except Exception as e:
                print(f"    [!] Ошибка бэкапа {sf.name}: {e}")

    print(f"[OK] Резервная копия готова ({copied} файлов)")
    return bdir


def copy_update():
    print(f"[*] Копирую обновлённые файлы из update/ в backend/...")
    copied = 0
    updated = 0
    skipped = 0

    for root, dirs, files in os.walk(SRC):
        rel_root = Path(root).relative_to(SRC)
        if any(part in EXCLUDE_DIRS for part in rel_root.parts):
            dirs.clear()
            continue
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]

        target_root = DST / rel_root
        target_root.mkdir(parents=True, exist_ok=True)

        for f in files:
            if any(f.endswith(ext) for ext in EXCLUDE_EXTS):
                continue
            sf = Path(root) / f
            df = target_root / f
            try:
                if not df.exists():
                    shutil.copy2(sf, df)
                    copied += 1
                else:
                    # Проверяем, изменился ли файл
                    s_stat = sf.stat()
                    d_stat = df.stat()
                    if s_stat.st_mtime > d_stat.st_mtime or s_stat.st_size != d_stat.st_size:
                        shutil.copy2(sf, df)
                        updated += 1
                    else:
                        skipped += 1
            except Exception as e:
                print(f"    [!] Ошибка копирования {sf.name}: {e}")

    print(f"[OK] Обновление завершено: скопировано новых: {copied}, обновлено: {updated}, без изменений: {skipped}")


def verify_backend():
    print("[*] Проверка импортов и целостности обновлённого backend...")
    sys.path.insert(0, str(DST))
    # 1) Синтаксис патченных файлов
    for rel in sorted({rel for rel, _m, _c in PATCHES}):
        path = DST / rel
        if path.exists():
            try:
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
                print(f"[OK] синтаксис: {rel}")
            except SyntaxError as e:
                print(f"[!] СИНТАКТИЧЕСКАЯ ОШИБКА в {rel}: {e}")
                return False
    # 2) Импорты ключевых модулей
    try:
        import hermes_cli.main  # noqa: F401
        import gateway.run  # noqa: F401
        import gateway.platforms.api_server  # noqa: F401
        import agent.turn_response_intake  # noqa: F401
        print("[OK] Модули hermes_cli.main / gateway.run / api_server импортируются")
        return True
    except Exception as e:
        print(f"[!] Ошибка проверки: {e}")
        return False


def main():
    force = "--force" in sys.argv
    dry = "--dry" in sys.argv

    print("=" * 64)
    print("  Обновление Hermes Backend (из update/ в backend/)")
    print("=" * 64)

    if not SRC.exists() or not (SRC / "hermes_cli" / "main.py").exists():
        print("[ERROR] В папке update/ не найдены исходники Hermes!")
        print("Положите новую версию Hermes в папку update/ и запустите скрипт снова.")
        sys.exit(1)

    v_old = get_version(DST)
    v_new = get_version(SRC)
    print(f"Текущая версия:  {v_old}")
    print(f"Новая версия:    {v_new}")
    print()

    if is_running() and not force:
        print("[!] ВНИМАНИЕ: Порты 8642 или 9120 заняты — приложение Infinity запущено!")
        print("Пожалуйста, закройте Infinity перед обновлением файлов ядра.")
        print("(или запустите с флагом --force, если хотите продолжить)")
        sys.exit(2)

    if dry:
        print("[DRY-RUN] Режим симуляции, файлы не изменены.")
        sys.exit(0)

    # 1. Бэкап
    bdir = backup_backend()

    # 2. Обновление
    copy_update()

    # 2.5. Локальные патчи Infinity (reasoning-стрим, truncate/rewind сессии)
    apply_local_patches()

    # 3. Проверка
    ok = verify_backend()
    print("=" * 64)
    if ok:
        print("УСПЕХ! Hermes backend успешно обновлен.")
        print(f"Версия изменена: {v_old} -> {v_new}")
        print(f"Резервная копия сохранена в: {bdir}")
        print("Пользовательские настройки (.hermes-portable/) НЕ затронуты.")
        print("Теперь можно запустить: start_infinity.bat")
    else:
        print("[!] Внимание: при проверке импортов возникли ошибки.")
        print(f"Вы можете восстановить предыдущую версию из: {bdir}")
    print("=" * 64)


if __name__ == "__main__":
    main()