"""Проверки флагов мини-модулей и привязок по профилям.

Флаг и сочетание клавиш живут в конфиге профиля, поэтому админка пишет их
выбранному профилю, а не тому, кто случайно обслуживается сервером сейчас.
Проверки читают реальный файл тестового профиля, а запись подменяют: живые
config.json и profiles_index.json не должны меняться от запуска теста.

    python tools/module_flags_check.py
"""

import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
sys.path.insert(0, APP)

PID = "zz-flagcheck"
failures = []
checks = 0


def check(name, condition, detail=""):
    global checks
    checks += 1
    if not condition:
        failures.append("%s — %s" % (name, detail))
        print("  FAIL  %s  %s" % (name, detail))
    else:
        print("  ok    %s" % name)


def make_profile(profile_id, config):
    directory = os.path.join(ROOT, "data", "profiles", profile_id)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)
    return directory


class FakeProfiles:
    """Профили подменены: запись идёт в память, а не в настоящий индекс.

    save_profile_config в настоящем модуле требует, чтобы профиль был в
    profiles_index.json, — тестовый каталог там не числится, и проверка
    писала бы в память вместо отказа. Право на создание файла профиля тест
    проверяет отдельно: без индекса оно и должно отказывать.
    """

    def __init__(self, configs, active="admin"):
        self.configs = dict(configs)
        self.saved = {}
        self.active = active

    def get_active_id(self):
        return self.active

    def get_profile_config(self, profile_id):
        return json.loads(json.dumps(self.configs.get(profile_id))) \
            if profile_id in self.configs else None

    def profile_exists(self, profile_id):
        return profile_id in self.configs

    def save_profile_config(self, profile_id, config):
        if profile_id not in self.configs:
            return False
        self.saved[profile_id] = config
        self.configs[profile_id] = config
        return True


class FakeConfig:
    """config_module для активного профиля: сохранение в память, без диска.

    Чтение для непустого profile_id идёт через подменённые профили — секция
    modules читается именно оттуда, и без load_config_for подмена была бы
    половинчатой: запись проверялась бы памятью, а чтение — настоящим диском.
    """

    def __init__(self, config, profiles=None):
        self.config = json.loads(json.dumps(config))
        self.profiles = profiles
        self.reloaded = 0

    def get_config(self):
        return json.loads(json.dumps(self.config))

    def load_config_for(self, profile_id):
        if self.profiles is not None:
            return self.profiles.get_profile_config(profile_id)
        return self.get_config()

    def save_config(self, config):
        self.config = json.loads(json.dumps(config))

    def reload_config(self):
        self.reloaded += 1
        return self.get_config()


def install(fake_profiles, fake_config):
    """Подменить профили и конфиг на время проверки."""
    fake_config.profiles = fake_profiles
    sys.modules["profiles_module"] = fake_profiles
    sys.modules["config_module"] = fake_config


def main():
    import module_flags

    print("чтение флагов профиля")
    make_profile(PID, {"modules": {"automator": {"enabled": True}},
                       "server": {"port": 8281}})
    flags = module_flags.all_flags_for(PID)
    check("флаг профиля прочитан из его файла",
          flags["automator"]["enabled"] is True, str(flags))
    check("неизвестный ключ читается выключенным",
          flags["automator"]["agent_run"] is False and
          flags["screenshots"]["ocr"] is False, str(flags))
    check("enabled_for и flag_for того же профиля",
          module_flags.enabled_for("automator", PID) and
          not module_flags.flag_for("automator", "record", PID))
    check("выключенный профиль остаётся выключенным",
          not module_flags.enabled_for("screenshots", PID))
    shutil.rmtree(os.path.join(ROOT, "data", "profiles", PID), ignore_errors=True)

    print("запись флага чужому профилю")
    base = {"modules": {"screenshots": {"enabled": True}},
            "hotkeys": {"screenshot.full": "ctrl+alt+s"},
            "ui": {"theme": "nt"}}
    fp = FakeProfiles({PID: base, "admin": {"modules": {}}}, active="admin")
    fc = FakeConfig({"modules": {}, "hotkeys": {}})
    install(fp, fc)

    saved = module_flags.set_flag_for("automator", "enabled", True, PID)
    written = fp.saved.get(PID, {})
    check("флаг записан выбранному профилю",
          saved["profile"] == PID and
          written.get("modules", {}).get("automator", {}).get("enabled") is True,
          str(saved))
    check("чужие секции конфига не тронуты",
          written.get("hotkeys") == base["hotkeys"] and
          written.get("ui") == base["ui"] and
          written.get("modules", {}).get("screenshots") == base["modules"]["screenshots"],
          str(written))
    check("дефолты конфига не переехали в файл профиля",
          "server" not in written and "search" not in written, str(sorted(written)))

    module_flags.set_flag_for("automator", "enabled", False, PID)
    check("выключение читается тем же профилем",
          module_flags.all_flags_for(PID)["automator"]["enabled"] is False)

    try:
        module_flags.set_flag_for("nope", "enabled", True, PID)
        check("неизвестный модуль отклонён", False, "исключения не было")
    except ValueError as e:
        check("неизвестный модуль отклонён", "Неизвестный модуль" in str(e), str(e))
    try:
        module_flags.set_flag_for("automator", "nope", True, PID)
        check("неизвестный флаг отклонён", False, "исключения не было")
    except ValueError as e:
        check("неизвестный флаг отклонён", "Неизвестный флаг" in str(e), str(e))
    try:
        module_flags.set_flag_for("automator", "enabled", True, "zz-нет-такого")
        check("профиля нет — записи не было", False, "исключения не было")
    except ValueError as e:
        check("профиля нет — записи не было",
              "Профиль не найден" in str(e) and "zz-нет-такого" not in fp.saved, str(e))

    print("запись флага активному профилю")
    fp2 = FakeProfiles({"admin": {"modules": {}, "hotkeys": {"a": "b"}}}, active="admin")
    fc2 = FakeConfig({"modules": {}, "hotkeys": {"a": "b"}})
    install(fp2, fc2)
    saved = module_flags.set_flag("screenshots", "ocr", True)
    check("активный профиль пишет через save_config",
          fc2.config.get("modules", {}).get("screenshots", {}).get("ocr") is True and
          saved["profile"] == "admin", str(fc2.config))
    check("кэш конфига перезапущен после записи", fc2.reloaded == 1, str(fc2.reloaded))
    check("чужой профиль не тронут активным", PID not in fp2.saved and
          fp2.configs["admin"].get("hotkeys") == {"a": "b"}, str(fp2.configs))

    print("привязки клавиш по профилю")
    import routes.hotkeys as HK
    fp3 = FakeProfiles({PID: {"hotkeys": {"screenshot.full": "ctrl+alt+s"},
                              "modules": {"screenshots": {"enabled": True}}}},
                       active="admin")
    install(fp3, FakeConfig({"modules": {}, "hotkeys": {}}))
    HK._save_hotkey("automator.toggle", "ctrl+alt+q", PID)
    keys = fp3.saved.get(PID, {}).get("hotkeys", {})
    check("привязка записана выбранному профилю",
          keys.get("automator.toggle") == "ctrl+alt+q", str(keys))
    check("прочие привязки профиля сохранены",
          keys.get("screenshot.full") == "ctrl+alt+s", str(keys))
    check("секция modules профиля не испарилась",
          fp3.saved[PID].get("modules", {}).get("screenshots", {}).get("enabled") is True,
          str(fp3.saved))
    HK._save_hotkey("automator.toggle", "", PID)
    check("снятие override убирает ключ",
          "automator.toggle" not in fp3.saved.get(PID, {}).get("hotkeys", {}),
          str(fp3.saved))
    fc3 = FakeConfig({"modules": {}, "hotkeys": {}})
    install(FakeProfiles({"admin": {}}, active="admin"), fc3)
    HK._save_hotkey("screenshot.window", "ctrl+alt+w", "admin")
    check("активному профилю пишет общий конфиг",
          fc3.config.get("hotkeys", {}).get("screenshot.window") == "ctrl+alt+w" and
          fc3.reloaded == 1, str(fc3.config))

    print("\n checks: %d, падений: %d" % (checks, len(failures)))
    for f in failures:
        print("  ! " + f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
