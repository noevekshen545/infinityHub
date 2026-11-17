"""Личные файлы агента у профилей приложения.

Профили приложения (cookie ``infinity_profile``) раньше делили один набор
файлов агента: ``SOUL.md``, ``USER.md``, ``AGENTS.md`` и системные инструкции.
Правка личности в одном профиле меняла агента во всех остальных.

Теперь у каждого профиля свой каталог ``<HERMES_HOME>/app-profiles/<id>/``.
Файл, которого в этом каталоге нет, читается из профиля по умолчанию —
профиль «наследует» инструкции, пока их не изменили, и правка в одном
профиле не задевает остальные. У профиля по умолчанию путь прежний
(``HERMES_HOME``), поэтому его уже сохранённые данные не «теряются».
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from . import config
try:
    import swallow                      # внутри приложения: след в общий лог
except ImportError:
    # модуль умеет работать и вне приложения — тогда следа нет, но и падения нет
    class swallow:
        @staticmethod
        def note(*a, **k):
            pass

#: Имена файлов инструкций, которые разрешено править из интерфейса.
AGENT_FILES = ("SOUL.md", "USER.md", "AGENTS.md")
#: Системные инструкции агента (вкладка «Агент»).
PROMPT_FILE = "AGENT_PROMPT.md"
ALLOWED = frozenset(AGENT_FILES + (PROMPT_FILE,))

#: id профиля приходит из cookie/заголовка — в путь пускаем только безопасное.
_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def safe_id(profile_id: str) -> str:
	"""Безопасное имя каталога профиля; пусто — профиль по умолчанию."""
	safe = _SAFE_RE.sub("_", str(profile_id or ""))[:64]
	return "" if not safe or safe == "default" else safe


def default_home() -> Path:
	return Path(config.HOME_DIR)


def profile_home(profile_id: str) -> Path:
	"""Домашний каталог агента для профиля приложения."""
	safe = safe_id(profile_id)
	return default_home() / "app-profiles" / safe if safe else default_home()


def has_own(profile_id: str, name: str) -> bool:
	"""True, если профиль уже завёл свой файл (иначе читается общий)."""
	if name not in ALLOWED or not safe_id(profile_id):
		return False
	try:
		return (profile_home(profile_id) / name).is_file()
	except OSError:
		swallow.note('app_home.has_own:L62')
		return False


def read_text(profile_id: str, name: str, fallback: Optional[Path] = None) -> str:
	"""Содержимое файла профиля с наследованием от профиля по умолчанию."""
	if name not in ALLOWED:
		raise ValueError(f"файл {name!r} не разрешён")
	candidates = []
	if safe_id(profile_id):
		candidates.append(profile_home(profile_id) / name)
	candidates.append(default_home() / name)
	if fallback is not None:
		candidates.append(Path(fallback))
	seen = set()
	for path in candidates:
		key = str(path).lower()
		if key in seen:
			continue
		seen.add(key)
		try:
			if path.is_file():
				return path.read_text(encoding="utf-8", errors="replace")
		except OSError:
			swallow.note('app_home.read_text:L86')
			continue
	return ""


def path_for_write(profile_id: str, name: str) -> Path:
	"""Куда писать файл: свой каталог профиля или общий для default."""
	if name not in ALLOWED:
		raise ValueError(f"файл {name!r} не разрешён")
	return profile_home(profile_id) / name
