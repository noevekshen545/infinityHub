#!/usr/bin/env python3
"""Тесты надёжности сохранения данных профилей.

Закрывают регрессии, из-за которых настройки, приложения, расширения и
данные соцсети пропадали после перезапуска сервера:

  1. Неатомарная запись  — обычный open(path, "w") обрубал файл, и при
     убийстве процесса на середине json.dump оставался обрывок, который
     при загрузке молча уводил систему на дефолты.
  2. Порча файла        — повреждённый JSON откатывался на _DEFAULTS, и
     пользователь видел «настройки слетели», хотя файл лежал рядом.
  3. Гонка записи       — два параллельных save_frontend_state перетирали
     друг друга (фронт шлёт их через sendBeacon, не дожидаясь ответа).
  4. Осиротение профилей— битый profiles_index.json приводил к созданию
     нового профиля 'default', а настоящие профили оставались позабыты.

Запуск:
    venv\\Scripts\\python.exe -m pytest app/tests/test_persistence.py -v
    venv\\Scripts\\python.exe app/tests/test_persistence.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _IsolatedProfiles(unittest.TestCase):
    """Базовый класс: подменяем DATA_DIR на временный каталог."""

    def setUp(self):
        import profiles_module as pm
        self.pm = pm
        self.tmp = tempfile.mkdtemp(prefix="infinity_profiles_")

        self._saved = (
            pm.DATA_DIR, pm.PROFILES_DIR, pm.INDEX_PATH,
            pm._index_cache, pm._index_cache_key, dict(pm._frontend_state_cache),
        )
        pm.DATA_DIR = self.tmp
        pm.PROFILES_DIR = os.path.join(self.tmp, "profiles")
        pm.INDEX_PATH = os.path.join(pm.PROFILES_DIR, "profiles_index.json")
        pm._ensure_dir()
        pm._load_index_cache_reset()
        with pm._frontend_state_lock:
            pm._frontend_state_cache.clear()
        pm._write_locks.clear()

    def tearDown(self):
        import profiles_module as pm
        (pm.DATA_DIR, pm.PROFILES_DIR, pm.INDEX_PATH,
         pm._index_cache, pm._index_cache_key, _) = self._saved
        with pm._frontend_state_lock:
            pm._frontend_state_cache.clear()
            pm._frontend_state_cache.update(self._saved[5])
        pm._write_locks.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------
    def _make_profile(self, pid="default", name="Общедоступный"):
        pm = self.pm
        pm._setup_profile_data_dirs(pid)
        cfg_path = pm._profile_config_path(pid)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"id": pid, "books_dir": "E:/books"}, f, ensure_ascii=False)
        pm._save_index({"profiles": [{"id": pid, "name": name,
                                      "password_hash": "", "created": 0.0,
                                      "modified": 0.0}], "active": pid})
        return pid

    def _read(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


class TestAtomicWrite(_IsolatedProfiles):
    """Запись должна быть атомарной: старый файл цел до os.replace."""

    def test_no_tmp_left_behind(self):
        path = os.path.join(self.tmp, "config.json")
        self.assertTrue(self.pm._atomic_write_json(path, {"a": 1}))
        self.assertEqual(self._read(path), {"a": 1})
        leftovers = [f for f in os.listdir(self.tmp) if ".tmp-" in f]
        self.assertEqual(leftovers, [], "остался временный файл: %s" % leftovers)

    def test_previous_content_survives_failed_write(self):
        """Если запись падает, старый файл НЕ должен остаться обрезанным."""
        path = os.path.join(self.tmp, "config.json")
        self.pm._atomic_write_json(path, {"books_dir": "E:/books"})
        original = self._read(path)
        # Объект, который не сериализуется — запись обязана провалиться
        boom = {"books_dir": "E:/books", "bad": object()}
        self.assertFalse(self.pm._atomic_write_json(path, boom))
        # Главное: файл цел и содержит ПРЕЖНИЕ данные, а не пустоту/обрывок
        self.assertEqual(self._read(path), original)

    def test_unicode_paths_preserved(self):
        path = os.path.join(self.tmp, "config.json")
        payload = {"books_dir": "E:\\Книги\\fb2", "name": "Общедоступный"}
        self.pm._atomic_write_json(path, payload)
        self.assertEqual(self._read(path), payload)


class TestCorruptQuarantine(_IsolatedProfiles):
    """Битый файл должен откладываться, а не молча уходить в дефолты."""

    def test_corrupt_config_is_quarantined_not_lost(self):
        pid = self._make_profile()
        path = self.pm._profile_config_path(pid)
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"books_dir": "E:/bo')  # обрывок, как после убийства

        self.assertIsNone(self.pm.get_active_config())

        self.assertFalse(os.path.exists(path), "битый файл остался на месте")
        quarantined = [f for f in os.listdir(self.pm._profile_path(pid))
                       if ".corrupt-" in f]
        self.assertEqual(len(quarantined), 1, "ожидался один карантин-файл")
        # И содержимое доступно для ручного восстановления
        with open(os.path.join(self.pm._profile_path(pid), quarantined[0]),
                  "r", encoding="utf-8") as f:
            self.assertIn("E:/bo", f.read())

    def test_corrupt_frontend_state_is_quarantined(self):
        pid = self._make_profile()
        path = self.pm._profile_frontend_state_path(pid)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(self.pm.load_frontend_state(pid), {})
        self.assertFalse(os.path.exists(path))
        self.assertTrue(any(".corrupt-" in f
                            for f in os.listdir(self.pm._profile_path(pid))))

    def test_bom_is_tolerated(self):
        """Windows-редакторы пишут BOM — он не должен ломать загрузку."""
        pid = self._make_profile()
        path = self.pm._profile_config_path(pid)
        with open(path, "w", encoding="utf-8-sig") as f:
            json.dump({"books_dir": "E:/books"}, f, ensure_ascii=False)
        self.assertIsNotNone(self.pm.get_active_config())


class TestProfileSurvival(_IsolatedProfiles):
    """Битый индекс не должен приводить к созданию нового профиля."""

    def test_corrupt_index_does_not_orphan_real_profiles(self):
        pid = self._make_profile("default")
        self.pm.save_active_config({"books_dir": "E:/books",
                                    "apps_dirs": ["E:/apps"]})

        # Повреждаем индекс так, как это делает убийство процесса
        with open(self.pm.INDEX_PATH, "w", encoding="utf-8") as f:
            f.write('{"profiles": [{"id": "def')

        index = self.pm.init_profiles()
        self.assertNotIn("error", index,
                         "init_profiles не должен сообщать о повреждении индекса")
        self.assertTrue(index.get("profiles"),
                        "профильный индекс не восстановился")

        # Главное: НАСТРОЙКИ НА МЕСТЕ — профиль не заменён новым default
        cfg = self.pm.get_active_config()
        self.assertIsNotNone(cfg, "конфиг профиля потерян")
        self.assertEqual(cfg.get("books_dir"), "E:/books")
        self.assertEqual(cfg.get("apps_dirs"), ["E:/apps"])

    def test_index_rebuilt_from_profile_dirs(self):
        pid = self._make_profile("default")
        self.pm.save_active_config({"books_dir": "E:/books"})

        os.remove(self.pm.INDEX_PATH)
        index = self.pm.init_profiles()
        ids = [p["id"] for p in index.get("profiles", [])]
        self.assertIn(pid, ids, "индекс не пересобран из каталогов профилей")
        self.assertEqual(
            self.pm.get_active_config().get("books_dir"), "E:/books",
            "конфиг профиля не должен теряться при пересборке индекса")

    def test_first_run_creates_profile_normally(self):
        """Без битого файла поведение не изменилось — профиль создаётся."""
        index = self.pm.init_profiles(source_config={"backend": "llamacpp"})
        self.assertTrue(index.get("profiles"))
        self.assertEqual(index["active"], "default")


class TestCrossProfileLeak(_IsolatedProfiles):
    """Профиль не должен писать в data-каталог другого профиля.

    Исторически проверка шла только по extensions_dir/plugins_dir,
    поэтому apps_dir/html_dir/photos_dir/stats_dir могли указывать в
    каталог соседнего профиля: приложения одного профиля оказывались
    в другом, а при удалении профиля пропадали у обоих.
    """

    def _two_profiles(self):
        pm = self.pm
        for pid, name in (("default", "Общедоступный"), ("admin", "Администратор")):
            pm._setup_profile_data_dirs(pid)
            cfg_path = pm._profile_config_path(pid)
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"id": pid}, f, ensure_ascii=False)
        pm._save_index({"profiles": [
            {"id": "default", "name": "Общедоступный", "password_hash": "",
             "created": 0.0, "modified": 0.0},
            {"id": "admin", "name": "Администратор", "password_hash": "",
             "created": 0.0, "modified": 0.0},
        ], "active": "default"})
        return "default", "admin"

    def test_foreign_paths_are_redirected(self):
        default, _admin = self._two_profiles()
        pm = self.pm
        cfg_path = pm._profile_config_path(default)
        # Имитируем утечку: профиль default пишет в каталоги admin
        leaked = {}
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for key in ("apps_dir", "html_dir", "photos_dir", "stats_dir"):
            cfg[key] = os.path.join(pm._profile_path("admin"), "data",
                                    pm.PROFILE_DATA_DIRS[key])
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)

        pm.ensure_profile_dir_map()

        fixed = pm.get_profile_config(default)
        # Ожидаемый путь строим от РЕАЛЬНОГО PROFILES_DIR, а не от
        # захардкоженной строки "data/profiles" — иначе тест был бы
        # привязан к имени каталога и падал бы на другой установке.
        own = os.path.join(pm.PROFILES_DIR, "default", "data").replace("\\", "/")
        for key in ("apps_dir", "html_dir", "photos_dir", "stats_dir"):
            got = os.path.normpath(fixed.get(key, "")).replace("\\", "/")
            self.assertIsNone(pm._path_in_other_profile(got, default),
                              "%s всё ещё указывает в чужой профиль" % key)
            self.assertTrue(got.startswith(own + "/"),
                            "%s = %s, ожидалось начало %s" % (key, got, own))

    def test_shared_legacy_dir_is_not_touched(self):
        """data/notes — общий legacy-каталог, его перенос утащил бы данные."""
        default, _admin = self._two_profiles()
        pm = self.pm
        cfg_path = pm._profile_config_path(default)
        shared = os.path.join(pm.DATA_DIR, "notes")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["notes_dir"] = shared
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)

        pm.ensure_profile_dir_map()

        self.assertEqual(pm.get_profile_config(default)["notes_dir"], shared,
                         "общий каталог заметок нельзя перенаправлять")

    def test_save_active_config_sanitizes_foreign_paths(self):
        """Главная причина возврата утечки: глобальный конфиг в памяти.

        Вызывающие (network_module, /api/index/settings и др.) передают в
        save_active_config ГЛОБАЛЬНЫЙ server.CFG, собранный при загрузке.
        Если он вырос из конфига профиля admin, то сохранение в активный
        профиль default снова протаскивало пути admin — и ensure_profile_dir_map
        чинил это только до первого сохранения.
        """
        default, admin = self._two_profiles()
        pm = self.pm
        # Глобальный конфиг сервера — с путями профиля admin
        server_cfg = {
            "network": {"tor": True},
            "extensions_dir": os.path.join(pm._profile_path(admin), "data", "extensions"),
            "apps_dir": os.path.join(pm._profile_path(admin), "data", "apps"),
            "stats_dir": os.path.join(pm._profile_path(admin), "data", "stats"),
        }
        pm.save_active_config(server_cfg)

        saved = pm.get_profile_config(default)
        for key in ("extensions_dir", "apps_dir", "stats_dir"):
            self.assertIsNone(pm._path_in_other_profile(saved[key], default),
                              "%s утек в профиль %s" % (key, admin))
        # Посторонние ключи сохраняются как обычно
        self.assertEqual(saved["network"], {"tor": True})

    def test_own_paths_are_left_alone(self):
        default, _admin = self._two_profiles()
        pm = self.pm
        # Заполняем ВСЕ data-ключи собственными путями: функция не должна
        # менять уже корректный конфиг (она лишь дозаполняет пустые ключи).
        cfg_path = pm._profile_config_path(default)
        data_dirs = pm._setup_profile_data_dirs(default)
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.update(data_dirs)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)

        before = pm.get_profile_config(default)
        pm.ensure_profile_dir_map()
        after = pm.get_profile_config(default)
        self.assertEqual(before, after,
                         "корректный конфиг должен остаться нетронутым")


class TestSettingsSurviveRestart(_IsolatedProfiles):
    """Настройки из UI должны переживать «перезапуск»."""

    def test_private_paths_persist(self):
        pid = self._make_profile()
        self.assertTrue(self.pm.set_profile_private_paths(pid, {
            "private_books_dir": "E:/mybooks",
            "private_zim_dirs": ["E:/zim1", "E:/zim2"],
        }))
        cfg = self.pm.get_profile_config(pid)
        self.assertEqual(cfg["private_books_dir"], "E:/mybooks")
        self.assertEqual(cfg["private_zim_dirs"], ["E:/zim1", "E:/zim2"])

    def test_frontend_state_persists(self):
        pid = self._make_profile()
        self.assertTrue(self.pm.save_frontend_state(pid, {
            "desktop_apps": [{"id": "calc"}],
            "infinity_theme": "ocean",
            "pinned_apps": ["calc"],
        }))
        # "Перезапуск" = свежее чтение с диска
        self.pm._load_index_cache_reset()
        with self.pm._frontend_state_lock:
            self.pm._frontend_state_cache.clear()
        state = self.pm.load_frontend_state(pid)
        self.assertEqual(state["infinity_theme"], "ocean")
        self.assertEqual(state["desktop_apps"], [{"id": "calc"}])

    def test_partial_save_does_not_erase_other_keys(self):
        pid = self._make_profile()
        self.pm.save_frontend_state(pid, {"infinity_theme": "forest",
                                          "pinned_apps": ["a"]})
        # Фоновое сохранение из desktop.html присылает только один ключ
        self.pm.save_frontend_state(pid, {"infinity_theme": "midnight"})
        state = self.pm.load_frontend_state(pid)
        self.assertEqual(state["infinity_theme"], "midnight")
        self.assertEqual(state["pinned_apps"], ["a"],
                         "частичное сохранение стёрло посторонний ключ")

    def test_unknown_keys_are_not_persisted(self):
        """Белый список FRONTEND_KEYS должен продолжать работать."""
        pid = self._make_profile()
        self.pm.save_frontend_state(pid, {"infinity_theme": "ocean",
                                         "evil_key": "x"})
        self.assertNotIn("evil_key", self.pm.load_frontend_state(pid))
class TestConcurrentSaves(_IsolatedProfiles):
    """Параллельные сохранения не должны терять изменения."""

    def test_parallel_frontend_state_saves_lose_nothing(self):
        pid = self._make_profile()
        # Ключи берём из реального белого списка FRONTEND_KEYS — иначе
        # они будут отфильтрованы и тест проверял бы не то.
        keys = [k for k in self.pm.FRONTEND_KEYS][:40]
        errors = []

        def worker(key):
            try:
                for _ in range(5):
                    self.pm.save_frontend_state(pid, {key: key})
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(k,)) for k in keys]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        self.assertEqual(errors, [])
        state = self.pm.load_frontend_state(pid)
        # Без блокировки часть ключей терялась: каждый поток читал файл,
        # которого ещё не видел предыдущий, и перетирал его целиком.
        missing = [k for k in keys if state.get(k) != k]
        self.assertEqual(missing, [],
                         "потеряны ключи из параллельных сохранений: %s" % missing[:10])

    def test_parallel_config_saves_keep_file_valid(self):
        """Файл должен остаться валидным JSON при ConcurrentModification."""
        pid = self._make_profile()
        errors = []

        def worker(n):
            try:
                for _ in range(10):
                    self.pm.save_active_config({"books_dir": "E:/b%d" % n,
                                                "n": n})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(errors, [])
        cfg = self.pm.get_active_config()
        self.assertIsNotNone(cfg, "config.json остался нечитаемым")
        self.assertIn("books_dir", cfg)


if __name__ == "__main__":
    unittest.main(verbosity=2)