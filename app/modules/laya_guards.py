import swallow
"""Проверки внешнего текста движком laya: включена ли классификация книг
и есть ли в тексте попытки инъекции инструкций.

Перенесено из server_backup.py без изменения тел. Обе функции сами импортируют
laya_client в try/except и при любой ошибке возвращают безобидное значение (False
или None) - состояние монолита им не нужно, поэтому _sb здесь не используется.
"""


def _laya_books_enabled():
    """Доступен ли движок laya для классификации книг (и включена ли фича)."""
    try:
        import laya_client
    except Exception:
        swallow.note('laya_guards._laya_books_enabled:L15')
        return False
    try:
        return bool(laya_client.feature("books_classify"))
    except Exception:
        swallow.note('laya_guards._laya_books_enabled:L20')
        return False

def _laya_guard_context(text):
    """Проверка внешнего текста на попытки инструкций (движок laya).

    Возвращает None, если движок выключен, недоступен или ошибся: тогда
    контекст остаётся обычным. Никогда не бросает — ask_article_question
    должен работать и без laya.
    """
    try:
        import laya_client
    except Exception:
        swallow.note('laya_guards._laya_guard_context:L33')
        return None
    try:
        if not (text or "").strip():
            return None
        return laya_client.guard_injection(text)
    except Exception:
        swallow.note('laya_guards._laya_guard_context:L40')
        return None
