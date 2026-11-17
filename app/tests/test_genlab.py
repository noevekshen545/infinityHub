#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты модуля GenLab (AI-генератор на ComfyUI).

Что проверяется:
  1. config      — портативный резолв путей к ComfyUI и к хранилищу;
  2. graph       — валидация API-промпта (в том числе против живой схемы ComfyUI,
                   если она доступна; иначе — против вшитой выжимки);
  3. workflows   — каждый пресет собирает граф без исключений;
  4. store       — SQLite: ассеты, теги, избранное, статистика;
  5. imaging     — маски, композитинг слоёв, правки на CPU;
  6. api         — контракт эндпоинтов на поднятом uvicorn (TestClient).

    python -m pytest app/tests/test_genlab.py -v
"""

import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from genlab import catalog, config as gconfig, graph, imaging, store, workflows  # noqa: E402
from genlab import service as genlab_service  # noqa: E402
from genlab.api import router as genlab_router  # noqa: E402

# ComfyUI весит несколько гигабайт и в репозиторий не входит. Тесты, которым
# нужен установленный ComfyUI, пропускаются, а не падают.
requires_comfy = pytest.mark.skipif(
    not gconfig.comfy_available(),
    reason="ComfyUI is not included in this repository",
)


# ──────────────────────────────────────────────────────────────────────────────
# Фикстуры
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def object_info():
    """Схема нод ComfyUI, если он запущен. Иначе — None (часть тестов пропустится)."""
    try:
        info = graph.get_object_info(max_age=5.0)
        return info if info else None
    except Exception:
        return None


@pytest.fixture()
def temp_store(tmp_path, monkeypatch):
    """Изолированная БД и каталоги, чтобы тесты не трогали данные профиля."""
    root = tmp_path / "genlab"
    output = root / "output"
    output.mkdir(parents=True, exist_ok=True)
    db_file = root / "genlab.db"
    monkeypatch.setattr(gconfig, "storage_root", lambda: str(root))
    monkeypatch.setattr(gconfig, "output_dir", lambda: str(output))
    monkeypatch.setattr(gconfig, "db_path", lambda: str(db_file))
    store._initialized = False
    store._db_path = None
    yield root
    store._initialized = False
    store._db_path = None


@pytest.fixture(autouse=True)
def _restore_genlab_settings():
    """Ни один тест не должен оставлять след в пользовательском конфиге.

    Настройки GenLab лежат в config.json активного профиля: без отката тест,
    поменявший comfy_root или models_dir, испортил бы конфигурацию реальной
    установки — как уже случилось при первом прогоне.
    """
    before = _read_genlab_section()
    yield
    _write_genlab_section(before)
    gconfig.invalidate()


def _read_genlab_section():
    try:
        from profiles_module import get_active_config
        cfg = get_active_config() or {}
        return dict(cfg.get("genlab") or {})
    except Exception:
        return None


def _write_genlab_section(section):
    if section is None:
        return
    try:
        from profiles_module import get_active_config, save_active_config
        cfg = get_active_config() or {}
        cfg["genlab"] = section
        save_active_config(cfg)
        import config_module
        config_module.reload_config()
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# 1. Конфигурация и пути
# ──────────────────────────────────────────────────────────────────────────────

class TestConfig:
    @requires_comfy
    def test_comfy_root_points_to_installed_layout(self):
        root = gconfig.comfy_root()
        assert root.endswith(gconfig.COMFY_DIRNAME), root
        assert os.path.isfile(os.path.join(root, "ComfyUI", "main.py"))
        assert os.path.isfile(os.path.join(root, "python_embeded", "python.exe"))

    @requires_comfy
    def test_comfy_available(self):
        assert gconfig.comfy_available() is True

    def test_base_url_from_settings(self):
        cfg = gconfig.get_settings()
        assert gconfig.comfy_base_url() == "http://%s:%s" % (cfg["host"], cfg["port"])

    def test_storage_root_under_profile_data(self):
        path = gconfig.storage_root()
        assert path.endswith("genlab")
        assert os.path.isabs(path)

    def test_settings_defaults_present(self):
        settings = gconfig.get_settings()
        for key in gconfig.DEFAULTS:
            assert key in settings, key

    def test_expand_path_handles_portable_token(self, monkeypatch):
        import config_module
        monkeypatch.setattr(config_module, "_root", lambda: os.path.abspath("/opt/infinity"))
        expanded = gconfig.expand_path("${ROOT}/ComfyUI_windows_portable")
        assert expanded.endswith(gconfig.COMFY_DIRNAME)
        assert "${ROOT}" not in expanded


# ──────────────────────────────────────────────────────────────────────────────
# 1b. Пользовательские папки ComfyUI и моделей
# ──────────────────────────────────────────────────────────────────────────────

class TestCustomPaths:
    """Модуль ничего не скачивает, но умеет читать чужую папку с весами."""

    def test_default_models_dir_is_inside_comfy(self):
        assert gconfig.models_dir() == os.path.join(
            gconfig.comfy_root(), "ComfyUI", "models")
        assert gconfig.models_dir_is_custom() is False

    def test_custom_models_dir_wins(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gconfig, "get_settings",
                            lambda: dict(gconfig.DEFAULTS, models_dir=str(tmp_path)))
        assert gconfig.models_dir() == str(tmp_path)
        assert gconfig.models_dir_is_custom() is True

    def test_extra_args_empty_without_custom_dir(self, monkeypatch):
        monkeypatch.setattr(gconfig, "get_settings", lambda: dict(gconfig.DEFAULTS))
        assert gconfig.comfy_extra_args() == []

    def test_extra_args_point_to_generated_yaml(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gconfig, "get_settings",
                            lambda: dict(gconfig.DEFAULTS, models_dir=str(tmp_path)))
        args = gconfig.comfy_extra_args()
        assert args[0] == "--extra-model-paths-config"
        assert os.path.isfile(args[1])

    def test_yaml_lists_every_model_folder(self, monkeypatch, tmp_path):
        import yaml
        monkeypatch.setattr(gconfig, "get_settings",
                            lambda: dict(gconfig.DEFAULTS, models_dir=str(tmp_path)))
        path = gconfig.write_extra_model_paths()
        data = yaml.safe_load(open(path, encoding="utf-8"))
        conf = data["genlab"]
        assert conf["is_default"] is True
        for folder in gconfig.MODEL_FOLDERS:
            assert folder in conf, folder
            assert os.path.isabs(conf[folder]), conf[folder]
        # Подпапки созданы, чтобы ComfyUI не падал на пустой папке
        assert os.path.isdir(os.path.join(str(tmp_path), "checkpoints"))

    def test_yaml_is_loadable_by_comfyui_loader(self, monkeypatch, tmp_path):
        """Проверка настоящим загрузчиком ComfyUI, а не только парсером YAML."""
        comfy = os.path.join(gconfig.comfy_root(), "ComfyUI")
        if not os.path.isfile(os.path.join(comfy, "utils", "extra_config.py")):
            pytest.skip("нет исходников ComfyUI")
        sys.path.insert(0, comfy)
        try:
            import folder_paths
            from utils import extra_config
        except ImportError as e:
            pytest.skip("ComfyUI не импортируется: %s" % e)
        monkeypatch.setattr(gconfig, "get_settings",
                            lambda: dict(gconfig.DEFAULTS, models_dir=str(tmp_path)))
        path = gconfig.write_extra_model_paths()
        extra_config.load_extra_path_config(path)
        found = [os.path.normcase(p) for p in
                 folder_paths.folder_names_and_paths["checkpoints"][0]]
        want = os.path.normcase(os.path.join(str(tmp_path), "checkpoints"))
        assert want in found, "ComfyUI не добавил внешнюю папку"

    def test_models_roots_includes_both(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gconfig, "get_settings",
                            lambda: dict(gconfig.DEFAULTS, models_dir=str(tmp_path)))
        roots = catalog.models_roots()
        assert len(roots) == 2
        assert str(tmp_path) in roots

    def test_models_roots_dedupes_same_path(self, monkeypatch):
        standard = os.path.join(gconfig.comfy_root(), "ComfyUI", "models")
        monkeypatch.setattr(gconfig, "get_settings",
                            lambda: dict(gconfig.DEFAULTS, models_dir=standard))
        assert len(catalog.models_roots()) == 1

    def test_snapshot_reports_paths(self):
        snap = catalog.snapshot()
        assert "models_dir" in snap and "models_roots" in snap
        assert snap["downloads_enabled"] is False
        assert "не скачивает" in snap["note"].lower() or "не скачиваем" in snap["note"].lower()


# ──────────────────────────────────────────────────────────────────────────────
# 1c. Кастом-паки ComfyUI
# ──────────────────────────────────────────────────────────────────────────────

class TestPacks:
    def test_registry_is_known_and_unique(self):
        from genlab import packs
        ids = [p["id"] for p in packs.PACKS]
        assert len(ids) == len(set(ids))
        assert all(p["repo"].startswith("https://github.com/") for p in packs.PACKS)
        assert all(p.get("detect") for p in packs.PACKS), "у пака должен быть маркер нод"

    def test_status_shape(self):
        from genlab import packs
        rows = packs.status()
        assert rows
        for row in rows:
            assert row["state"] in ("active", "absent", "disabled", "broken",
                                    "unknown", "needs_restart"), row
            assert row["id"] and row["title"] and row["why"]

    def test_hidden_packs_not_shown_but_installable(self):
        from genlab import packs
        shown = {p["id"] for p in packs.status()}
        hidden = [p["id"] for p in packs.PACKS if p.get("hidden")]
        assert hidden, "ожидались скрытые зависимости"
        for pack_id in hidden:
            assert pack_id not in shown
            assert packs.is_known(pack_id)

    def test_capabilities_reports_core_nodes(self):
        """Ядро ComfyUI 0.39 даёт Canny/SAM3/позу/интерполяцию без паков."""
        from genlab import packs
        caps = packs.capabilities()
        if caps.get("canny") is None:
            pytest.skip("ComfyUI не запущен — схема нод недоступна")
        for key in ("canny", "sam3", "pose_core", "remove_bg",
                    "frame_interpolation", "text_generate"):
            assert caps.get(key) is True, key

    def test_nothing_broken_after_install(self):
        """Ни один установленный пак не должен остаться в состоянии broken."""
        from genlab import packs
        bad = [p for p in packs.status() if p["state"] == "broken"]
        assert not bad, [p["id"] for p in bad]

    def test_disable_enable_roundtrip_is_non_destructive(self, monkeypatch, tmp_path):
        """Отключение — переименование, а не удаление."""
        from genlab import packs
        nodes = tmp_path / "custom_nodes"
        pack = nodes / "DemoPack"
        pack.mkdir(parents=True)
        (pack / "__init__.py").write_text("", encoding="utf-8")
        monkeypatch.setattr(packs, "custom_nodes_dir", lambda: str(nodes))

        ok, message = packs.disable_pack("DemoPack")
        assert ok, message
        assert not pack.exists()
        assert (nodes / "DemoPack.disabled").is_dir()
        assert (nodes / "DemoPack.disabled" / "__init__.py").is_file()

        ok, message = packs.enable_pack("DemoPack")
        assert ok, message
        assert pack.is_dir()
        assert not (nodes / "DemoPack.disabled").exists()

    def test_remove_only_touches_our_own_packs(self, monkeypatch, tmp_path):
        from genlab import packs, config as g
        nodes = tmp_path / "custom_nodes"
        foreign = nodes / "UserPack"
        foreign.mkdir(parents=True)
        ours = nodes / "OurPack"
        ours.mkdir(parents=True)
        (ours / packs.MARKER).write_text("x", encoding="utf-8")
        monkeypatch.setattr(packs, "custom_nodes_dir", lambda: str(nodes))
        monkeypatch.setattr(gconfig, "app_root", lambda: str(tmp_path))

        ok, message = packs.remove_pack("UserPack")
        assert ok is False
        assert foreign.is_dir(), "чужой пакет удалён — так нельзя"

        packs.PACK_BY_ID["OurPack"] = {"id": "OurPack", "title": "", "repo": "",
                                       "why": "", "detect": [], "required": False}
        ok, message = packs.remove_pack("OurPack")
        assert ok, message
        assert not ours.is_dir()
        backups = os.path.join(str(tmp_path), "_backups", "genlab_packs")
        assert os.path.isdir(backups) and os.listdir(backups)

    def test_unknown_pack_rejected(self):
        from genlab import packs
        assert packs.is_known("такого-нет") is False
        assert packs.on_disk("такого-нет") is False

    @requires_comfy
    def test_constraints_pin_torch(self):
        """requirements.txt пакетов не должен утащить за собой torch."""
        from genlab import packs
        path = packs._constraints_file()
        assert path and os.path.isfile(path), "python_embeded недоступен"
        text = open(path, encoding="utf-8").read()
        assert "torch==" in text
        assert "torchvision==" in text

    def test_api_packs(self, client):
        data = client.get("/api/genlab/packs").json()
        assert data["packs"]
        assert isinstance(data["missing_required"], list)
        assert "capabilities" in data
        assert client.post("/api/genlab/packs/нет-такого/disable",
                           json={}).status_code == 404


# ──────────────────────────────────────────────────────────────────────────────
# 1d. Управление процессом ComfyUI
# ──────────────────────────────────────────────────────────────────────────────

class TestProcessControl:
    def test_pid_file_roundtrip(self):
        from genlab import service
        service._write_pid(123456)
        assert service._read_pid() == 123456
        service._clear_pid()
        assert service._read_pid() is None

    def test_dead_pid_is_not_owned(self, monkeypatch):
        from genlab import service
        service._write_pid(999999)
        try:
            assert service.owned_pid() is None
        finally:
            service._clear_pid()

    def test_port_busy_matches_reality(self):
        from genlab import service
        import socket
        cfg = gconfig.get_settings()
        host, port = cfg["host"], int(cfg["port"])
        with socket.socket() as probe:
            probe.settimeout(1.5)
            reachable = probe.connect_ex((host, port)) == 0
        assert service.port_busy() == reachable

    def test_status_reports_process_ownership(self):
        from genlab import service
        st = service.status()
        assert "process" in st and "adopted" in st
        assert set(st["process"]) >= {"running", "pid", "owned"}

    def test_start_does_not_spawn_duplicate(self):
        """Повторный start() не должен плодить второй процесс на том же порту."""
        from genlab import service
        if not service.port_busy():
            pytest.skip("ComfyUI сейчас не запущен")
        before = service.owned_pid()
        ok, message = service.start()
        assert ok
        assert service.owned_pid() in (before, None) or service.is_adopted()
        assert service.port_busy()


# ──────────────────────────────────────────────────────────────────────────────
# 2. Реестр пресетов и сборка графов
# ──────────────────────────────────────────────────────────────────────────────

SAMPLE_PARAMS = {
    "image.text2img": {"prompt": "кот"},
    "image.img2img": {"image": "cat.png", "prompt": "кот"},
    "edit.inpaint": {"image": "cat.png", "mask": "mask.png", "prompt": "кот"},
    "edit.outpaint": {"image": "cat.png", "prompt": "кот"},
    "edit.upscale": {"image": "cat.png"},
    "edit.detail": {"image": "cat.png"},
    "edit.controlnet": {"image": "cat.png", "control_net": "canny.pth", "prompt": "кот"},
    "edit.instruct": {"image": "cat.png", "prompt": "зимой"},
    "edit.removebg": {"image": "cat.png", "model": "u2net.pth"},
    "video.text2video": {"prompt": "лес"},
    "video.image2video": {"image": "cat.png", "prompt": "лес"},
    "video.interpolate": {"video": "clip.mp4", "model": "rife.pth"},
    "video.concat": {"video1": "a.mp4", "video2": "b.mp4"},
    "video.trim": {"video": "a.mp4"},
    "video.frames2video": {"frames": "shot_00001.png", "count": 4},
    "audio.tts": {"text": "привет"},
    "audio.music": {"prompt": "synthwave"},
    "audio.process": {"audio": "a.wav", "equalizer": True, "trim": True},
    "audio.merge": {"audio1": "a.wav", "audio2": "b.wav", "mode": "add"},
    "audio.sfx": {"prompt": "дождь"},
    "audio.repaint": {"audio": "a.wav", "start": 1, "end": 5, "prompt": "pop"},
    "model3d.hunyuan": {"image": "cat.png"},
    "model3d.trellis2": {"image": "cat.png"},
    "model3d.remesh": {"model": "cat.glb", "remesh": True, "preview": True},
}

# Пресеты, чьи ноды живут в необязательных комьюнити-паках. Без установленного
# пака схема ComfyUI этих нод не знает — график всё равно должен собираться,
# но проверку против живой схемы для них смягчаем.
OPTIONAL_PACK_PRESETS = {"audio.repaint"}


class TestRegistry:
    def test_every_group_has_presets(self):
        groups = workflows.groups()
        assert [g["id"] for g in groups] == ["image", "edit", "video", "audio", "model3d"]
        assert all(g["count"] > 0 for g in groups)

    def test_preset_metadata_complete(self):
        for preset in workflows.all_presets():
            assert preset["title"], preset["id"]
            assert preset["description"], preset["id"]
            assert preset["cost"] in ("low", "medium", "high"), preset["id"]
            assert preset["outputs"], preset["id"]
            for field in preset["fields"]:
                assert field["name"], preset["id"]
                assert field["type"] in presets_field_types(), preset["id"]

    def test_all_sample_presets_registered(self):
        known = {p["id"] for p in workflows.all_presets()}
        missing = set(SAMPLE_PARAMS) - known
        assert not missing, "нет пресетов: %s" % missing


def presets_field_types():
    from genlab.workflows import Field
    return Field.TYPES


class TestGraphBuilding:
    @pytest.mark.parametrize("preset_id", sorted(SAMPLE_PARAMS))
    def test_preset_builds_graph(self, preset_id):
        built = workflows.get(preset_id)(SAMPLE_PARAMS[preset_id])
        graph_map = built["graph"]
        assert isinstance(graph_map, dict) and graph_map
        for node_id, node in graph_map.items():
            assert node["class_type"], node_id
            assert isinstance(node["inputs"], dict), node_id

    def test_graph_has_output_node(self):
        for preset_id in ("image.text2img", "audio.process", "model3d.hunyuan"):
            built = workflows.get(preset_id)(SAMPLE_PARAMS[preset_id])
            classes = {n["class_type"] for n in built["graph"].values()}
            assert any(c.startswith("Save") for c in classes), preset_id

    def test_links_point_to_existing_nodes(self):
        for preset_id, params in SAMPLE_PARAMS.items():
            built = workflows.get(preset_id)(params)
            graph_map = built["graph"]
            for node_id, node in graph_map.items():
                for value in node["inputs"].values():
                    if graph._is_link(value):
                        assert str(value[0]) in graph_map, \
                            "%s: %s -> %s не существует" % (preset_id, node_id, value[0])

    def test_sequence_names(self):
        from genlab.workflows.video import _sequence_names
        names = _sequence_names("shot_00001.png", 3)
        assert names == ["shot_00001.png", "shot_00002.png", "shot_00003.png"]

    def test_sequence_names_requires_digits(self):
        from genlab.workflows.video import _sequence_names
        with pytest.raises(ValueError):
            _sequence_names("frame.png", 3)


class TestGraphValidation:
    def test_detects_unknown_node(self):
        problems = graph.validate_graph(
            {"1": {"class_type": "NoSuchNode", "inputs": {}}},
            object_info={"KSampler": {"input": {"required": {}}, "output": ["LATENT"]}},
        )
        assert any(p["code"] == "unknown_node" for p in problems)

    def test_detects_missing_required_input(self):
        info = {"Loader": {"input": {"required": {"ckpt_name": ["COMBO", {}]}},
                           "output": ["MODEL"]}}
        problems = graph.validate_graph({"1": {"class_type": "Loader", "inputs": {}}},
                                        object_info=info, check_models=False)
        assert any(p["code"] == "missing_input" for p in problems)

    def test_detects_broken_link(self):
        info = {
            "A": {"input": {"required": {}}, "output": ["IMAGE"]},
            "B": {"input": {"required": {"x": ["IMAGE", {}]}}, "output": []},
        }
        problems = graph.validate_graph({"1": {"class_type": "A", "inputs": {}},
                                         "2": {"class_type": "B", "inputs": {"x": ["9", 0]}}},
                                        object_info=info, check_models=False)
        assert any(p["code"] == "broken_link" for p in problems)

    def test_detects_bad_slot(self):
        info = {
            "A": {"input": {"required": {}}, "output": ["IMAGE"]},
            "B": {"input": {"required": {"x": ["IMAGE", {}]}}, "output": []},
        }
        problems = graph.validate_graph({"1": {"class_type": "A", "inputs": {}},
                                         "2": {"class_type": "B", "inputs": {"x": ["1", 5]}}},
                                        object_info=info, check_models=False)
        assert any(p["code"] == "bad_slot" for p in problems)

    def test_detects_missing_output_node(self):
        info = {"A": {"input": {"required": {}}, "output": ["IMAGE"]}}
        problems = graph.validate_graph({"1": {"class_type": "A", "inputs": {}}},
                                        object_info=info, check_models=False)
        assert any(p["code"] == "no_output_node" for p in problems)

    def test_value_range_warning(self):
        info = {"K": {"input": {"required": {"steps": ["INT", {"min": 1, "max": 10}]}},
                      "output": []}}
        problems = graph.validate_graph({"1": {"class_type": "K", "inputs": {"steps": 99}}},
                                        object_info=info, check_models=False)
        assert any(p["code"] == "above_max" for p in problems)

    @pytest.mark.skipif(not object_info, reason="ComfyUI не запущен")
    @pytest.mark.parametrize("preset_id", sorted(SAMPLE_PARAMS))
    def test_live_schema_has_no_structural_errors(self, object_info, preset_id):
        """Против живой схемы ComfyUI не должно быть ошибок структуры графа.

        Ошибки вида «не выбран чекпоинт» допустимы — моделей у пользователя нет.
        """
        built = workflows.get(preset_id)(SAMPLE_PARAMS[preset_id])
        problems = graph.validate_graph(built["graph"], object_info=object_info)
        structural = [p for p in problems
                      if p["severity"] == "error"
                      and p["code"] not in ("missing_input", "model_missing")]
        if (preset_id in OPTIONAL_PACK_PRESETS and structural
                and all(p["code"] == "unknown_node" for p in structural)):
            pytest.skip("нужен необязательный комьюнити-пак (напр. ComfyUI_ACE-Step)")
        assert not structural, "%s: %s" % (preset_id, [p["message"] for p in structural])


# ──────────────────────────────────────────────────────────────────────────────
# 3. Хранилище
# ──────────────────────────────────────────────────────────────────────────────

def _png(path, color=(255, 0, 0), size=(64, 48)):
    from PIL import Image
    img = Image.new("RGB", size, color)
    img.save(path, "PNG")
    return str(path)


class TestStore:
    def test_add_and_get_asset(self, temp_store):
        path = _png(temp_store / "a.png")
        asset = store.add_asset(name="a.png", path=path, media_type="image",
                                prompt="кот", seed=42)
        assert asset and asset["id"]
        fetched = store.get_asset(asset["id"])
        assert fetched["name"] == "a.png"
        assert fetched["prompt"] == "кот"
        assert fetched["seed"] == 42
        assert fetched["exists"] is True
        assert fetched["url"].endswith("/file")

    def test_add_asset_rejects_missing_file(self, temp_store):
        assert store.add_asset(name="x.png", path=str(temp_store / "nope.png")) is None

    def test_list_assets_filters_by_type(self, temp_store):
        _png(temp_store / "a.png")
        with open(temp_store / "clip.mp4", "wb") as fh:
            fh.write(b"\x00" * 16)
        store.add_asset(name="a.png", path=_png(temp_store / "b.png"))
        store.add_asset(name="clip.mp4", path=str(temp_store / "clip.mp4"))
        only_video = store.list_assets(media_type="video")
        assert only_video["total"] == 1
        assert only_video["assets"][0]["name"] == "clip.mp4"

    def test_favorite_toggle(self, temp_store):
        asset = store.add_asset(name="a.png", path=_png(temp_store / "a.png"))
        assert asset["favorite"] is False
        assert store.toggle_favorite(asset["id"])["favorite"] is True
        assert store.toggle_favorite(asset["id"])["favorite"] is False

    def test_tags(self, temp_store):
        asset = store.add_asset(name="a.png", path=_png(temp_store / "a.png"))
        store.add_tag(asset["id"], "Пейзаж")
        assert "пейзаж" in store.tags_of(asset["id"])
        assert any(t["tag"] == "пейзаж" for t in store.all_tags())
        store.remove_tag(asset["id"], "пейзаж")
        assert store.tags_of(asset["id"]) == []

    def test_search_assets(self, temp_store):
        store.add_asset(name="a.png", path=_png(temp_store / "a.png"), prompt="закат")
        store.add_asset(name="b.png", path=_png(temp_store / "b.png"), prompt="кот")
        found = store.list_assets(search="закат")
        assert found["total"] == 1

    def test_delete_asset(self, temp_store):
        path = _png(temp_store / "a.png")
        asset = store.add_asset(name="a.png", path=path)
        assert store.delete_asset(asset["id"], remove_file=True) is True
        assert store.get_asset(asset["id"]) is None
        assert not os.path.isfile(path)

    def test_stats(self, temp_store):
        store.add_asset(name="a.png", path=_png(temp_store / "a.png"))
        stats = store.stats()
        assert stats["total"] == 1
        assert stats["by_type"]["image"]["count"] == 1

    def test_layers(self, temp_store):
        path = _png(temp_store / "layer.png")
        store.save_layer("s1", path, kind="image", z_index=1)
        store.save_layer("s1", path, kind="image", z_index=0)
        layers = store.list_layers("s1")
        assert len(layers) == 2
        assert layers[0]["z_index"] == 0
        store.clear_layers("s1")
        assert store.list_layers("s1") == []

    def test_guess_media_type(self):
        assert store.guess_media_type("a.PNG") == "image"
        assert store.guess_media_type("a.mp3") == "audio"
        assert store.guess_media_type("a.mp4") == "video"
        assert store.guess_media_type("a.glb") == "model3d"
        assert store.guess_media_type("a.bin") == "file"

    def test_export_zip(self, temp_store):
        store.add_asset(name="a.png", path=_png(temp_store / "a.png"))
        store.add_asset(name="b.png", path=_png(temp_store / "b.png"))
        result = store.export_zip()
        assert result["count"] == 2
        assert os.path.isfile(result["zip"])


# ──────────────────────────────────────────────────────────────────────────────
# 4. Обработка изображений (CPU)
# ──────────────────────────────────────────────────────────────────────────────

class TestImaging:
    def test_thumbnail(self, temp_store):
        src = _png(temp_store / "big.png", size=(800, 600))
        dst = str(temp_store / "thumb.webp")
        assert imaging.make_thumbnail(src, dst, 64) is True
        assert os.path.isfile(dst)

    def test_mask_polygon(self, temp_store):
        result = imaging.build_mask({"width": 64, "height": 64,
                                     "points": [[0, 0], [32, 0], [32, 32], [0, 32]]})
        assert os.path.isfile(result["path"])
        from PIL import Image
        mask = Image.open(result["path"]).convert("L")
        assert mask.getpixel((10, 10)) == 255
        assert mask.getpixel((50, 50)) == 0

    def test_mask_requires_polygon(self, temp_store):
        with pytest.raises(imaging.ImageError):
            imaging.build_mask({"width": 64, "height": 64, "points": [[0, 0]]})

    def test_mask_brush(self, temp_store):
        result = imaging.build_mask({"width": 64, "height": 64, "mode": "brush",
                                     "brush": {"x": 32, "y": 32, "r": 10}})
        from PIL import Image
        mask = Image.open(result["path"]).convert("L")
        assert mask.getpixel((32, 32)) == 255

    def test_adjust_crop_and_rotate(self, temp_store):
        asset = store.add_asset(name="a.png", path=_png(temp_store / "a.png", size=(100, 80)))
        out = imaging.adjust({"asset_id": asset["id"],
                              "crop": {"x": 10, "y": 10, "w": 40, "h": 30},
                              "rotate": 90})
        assert (out["width"], out["height"]) == (30, 40)

    def test_adjust_rejects_empty_crop(self, temp_store):
        asset = store.add_asset(name="a.png", path=_png(temp_store / "a.png", size=(40, 40)))
        with pytest.raises(imaging.ImageError):
            imaging.adjust({"asset_id": asset["id"],
                            "crop": {"x": 100, "y": 100, "w": 10, "h": 10}})

    def test_compose_two_layers(self, temp_store):
        base = store.add_asset(name="base.png", path=_png(temp_store / "base.png",
                                                          (255, 255, 255), (64, 64)))
        top = store.add_asset(name="top.png", path=_png(temp_store / "top.png",
                                                        (255, 0, 0), (16, 16)))
        out = imaging.compose({"layers": [
            {"asset_id": base["id"]},
            {"asset_id": top["id"], "x": 10, "y": 10},
        ]})
        from PIL import Image
        img = Image.open(out["path"]).convert("RGB")
        assert img.getpixel((12, 12)) == (255, 0, 0)
        assert img.getpixel((40, 40)) == (255, 255, 255)

    def test_compose_without_layers(self, temp_store):
        with pytest.raises(imaging.ImageError):
            imaging.compose({"layers": []})

    def test_compose_all_hidden(self, temp_store):
        asset = store.add_asset(name="a.png", path=_png(temp_store / "a.png"))
        with pytest.raises(imaging.ImageError):
            imaging.compose({"layers": [{"asset_id": asset["id"], "visible": False}]})

    def test_missing_asset_raises(self, temp_store):
        with pytest.raises(imaging.ImageError):
            imaging.compose({"layers": [{"asset_id": "нет-такого"}]})


# ──────────────────────────────────────────────────────────────────────────────
# 5. Каталог моделей
# ──────────────────────────────────────────────────────────────────────────────

class TestCatalog:
    def test_snapshot_shape(self):
        snap = catalog.snapshot()
        assert "checkpoints" in snap["folders"]
        assert set(snap["readiness"]) == {"image", "edit", "video", "audio", "model3d"}
        assert snap["downloads_enabled"] is False

    def test_readiness_marks_edit_and_audio_ready(self):
        ready = catalog.readiness()
        assert ready["edit"]["ready"] is True     # апскейл/инпейнт-детализация без моделей
        assert ready["audio"]["ready"] is True    # обработка звука — без моделей

    def test_format_bytes(self):
        assert catalog.format_bytes(0) == "0 Б"
        assert catalog.format_bytes(2048).startswith("2.0 КБ")

    def test_models_cache_is_per_folder(self, monkeypatch):
        """Общий слот перезаписывался каждым вызовом — кэш не попадал никогда."""
        graph.invalidate_caches()
        calls = []

        class _Cli:
            def models(self, folder=None):
                calls.append(folder)
                return ["%s.safetensors" % folder]

        monkeypatch.setattr(genlab_service, "client", lambda: _Cli())
        monkeypatch.setattr(genlab_service, "fast_unreachable", lambda: False)
        assert graph.get_models(folder="checkpoints") == ["checkpoints.safetensors"]
        assert graph.get_models(folder="vae") == ["vae.safetensors"]
        # Повторные обращения обеих папок обязаны попасть в кэш.
        assert graph.get_models(folder="checkpoints") == ["checkpoints.safetensors"]
        assert graph.get_models(folder="vae") == ["vae.safetensors"]
        assert calls == ["checkpoints", "vae"]
        graph.invalidate_caches()

    def test_unreachable_comfy_does_not_retry_every_folder(self, monkeypatch):
        """Каталог обходит 11 папок: отказ должен запоминаться, а не повторяться."""
        graph.invalidate_caches()
        probes = []
        monkeypatch.setattr(genlab_service, "fast_unreachable",
                            lambda: probes.append(1) or True)
        assert catalog.folders_state()["vae"]["count"] == 0
        assert len(probes) <= 2, probes
        graph.invalidate_caches()


# ──────────────────────────────────────────────────────────────────────────────
# 6. API-контракт
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(genlab_router)
    static_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "static")
    if os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
    with TestClient(app) as c:
        yield c


class TestApi:
    @pytest.fixture(autouse=True)
    def _isolate_store(self, temp_store):
        """API-тесты не должны писать в реальную БД профиля.

        Без изоляции загрузка файла и обход галереи оставляли мусор в данных
        пользователя и падали, если рядом крутился живой сервер с той же БД.
        """
        yield temp_store

    def test_page_served(self, client):
        res = client.get("/genlab")
        assert res.status_code == 200
        assert "GenLab" in res.text

    def test_status(self, client):
        res = client.get("/api/genlab/status")
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert "comfy_root" in data and "devices" in data

    def test_presets(self, client):
        data = client.get("/api/genlab/presets").json()
        assert len(data["presets"]) >= 20
        assert len(data["groups"]) == 5

    def test_preset_by_id(self, client):
        data = client.get("/api/genlab/presets/image.text2img").json()
        assert data["preset"]["id"] == "image.text2img"
        assert data["preset"]["fields"]

    def test_preset_unknown(self, client):
        assert client.get("/api/genlab/presets/nope.nope").status_code == 404

    def test_validate_reports_missing_input_without_http_error(self, client):
        res = client.post("/api/genlab/validate",
                          json={"preset": "image.img2img", "params": {}})
        assert res.status_code == 200
        data = res.json()
        assert data["ok_problems"] is False
        assert data["problems"][0]["code"] == "builder"

    def test_validate_structurally_valid_graph(self, client):
        res = client.post("/api/genlab/validate",
                          json={"preset": "edit.detail", "params": {"image": "a.png"}})
        data = res.json()
        assert data["ok_problems"] is True
        assert len(data["nodes"]) >= 2

    def test_validate_unknown_preset(self, client):
        assert client.post("/api/genlab/validate",
                           json={"preset": "x"}).status_code == 404

    def test_jobs_listing(self, client):
        data = client.get("/api/genlab/jobs").json()
        assert isinstance(data["jobs"], list)

    def test_job_not_found(self, client):
        assert client.get("/api/genlab/jobs/нет").status_code == 404

    def test_settings_roundtrip(self, client):
        data = client.get("/api/genlab/settings").json()
        assert "comfy_root_resolved" in data["settings"]
        assert client.post("/api/genlab/settings",
                           json={"settings": {"неизвестно": 1}}).status_code == 422

    def test_settings_reject_unknown_keys(self, client):
        res = client.post("/api/genlab/settings", json={"settings": {"порнография": 1}})
        assert res.status_code == 422

    def test_upload_rejects_wrong_extension(self, client):
        res = client.post("/api/genlab/upload",
                          files={"file": ("virus.exe", b"MZ", "application/octet-stream")})
        assert res.status_code == 400

    def test_upload_and_gallery_roundtrip(self, client):
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("RGB", (24, 24), (10, 120, 200)).save(buf, "PNG")
        res = client.post("/api/genlab/upload",
                          files={"file": ("тест.png", buf.getvalue(), "image/png")})
        assert res.status_code == 200
        data = res.json()
        assert data["name"] == "тест.png"
        asset_id = data["asset"]["id"]

        listed = client.get("/api/genlab/assets").json()
        assert listed["total"] >= 1

        file_res = client.get("/api/genlab/assets/%s/file" % asset_id)
        assert file_res.status_code == 200
        assert file_res.headers.get("accept-ranges") == "bytes"

        thumb = client.get("/api/genlab/assets/%s/preview" % asset_id)
        assert thumb.status_code == 200

        client.post("/api/genlab/assets/%s" % asset_id, json={"action": "favorite"})
        assert client.post("/api/genlab/assets/%s" % asset_id,
                           json={"action": "delete"}).json()["deleted"] is True

    def test_asset_not_found(self, client):
        assert client.get("/api/genlab/assets/нет/file").status_code == 404

    def test_mask_upload_is_not_registered_in_gallery(self, client):
        """Маска — рабочий файл редактора, в галерее ей делать нечего."""
        from PIL import Image
        import io
        before = client.get("/api/genlab/assets").json()["assets"]
        buf = io.BytesIO()
        Image.new("L", (16, 16), 255).save(buf, "PNG")
        res = client.post("/api/genlab/upload",
                          files={"file": ("mask.png", buf.getvalue(), "image/png")},
                          data={"kind": "mask"})
        assert res.status_code == 200
        assert res.json()["asset"] is None
        after = client.get("/api/genlab/assets").json()["assets"]
        assert len(after) == len(before)

    def test_stage_asset_returns_comfy_name(self, client):
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("RGB", (20, 20), (200, 40, 90)).save(buf, "PNG")
        up = client.post("/api/genlab/upload",
                         files={"file": ("src.png", buf.getvalue(), "image/png")})
        asset_id = up.json()["asset"]["id"]
        res = client.post("/api/genlab/assets/%s/stage" % asset_id)
        if res.status_code == 502:
            pytest.skip("ComfyUI не принимает файлы: %s" % res.json().get("error"))
        assert res.status_code == 200, res.text
        assert res.json()["comfy_name"]

    def test_stage_unknown_asset(self, client):
        assert client.post("/api/genlab/assets/нет-такого/stage").status_code == 404

    def test_job_output_dedup_key(self, client, tmp_path):
        """Повторный сбор одного задания не должен плодить дубли ассетов:
        collect_job_outputs спрашивает _job_output_saved перед регистрацией."""
        from PIL import Image
        out = tmp_path / "out.png"
        Image.new("RGB", (8, 8), (1, 2, 3)).save(str(out), "PNG")
        asset = store.add_asset("out.png", str(out), job_id="job-dup-1", origin="test")
        assert asset is not None
        assert store._job_output_saved("job-dup-1", "out.png") is True
        assert store._job_output_saved("job-dup-1", "другой.png") is False
        assert store._job_output_saved("job-dup-2", "out.png") is False
        store.delete_asset(asset["id"], remove_file=False)

    @requires_comfy
    def test_paths_endpoint_reports_roots(self, client):
        data = client.get("/api/genlab/paths").json()
        assert data["comfy_ok"] is True
        assert os.path.isfile(data["comfy_main"])
        assert isinstance(data["models_roots"], list) and data["models_roots"]

    def test_paths_reject_bad_comfy_root(self, client):
        res = client.post("/api/genlab/paths",
                          json={"comfy_root": r"C:\куда-то-не-туда"})
        assert res.status_code == 422
        assert "main.py" in res.json()["error"]

    def test_paths_reject_file_as_models_dir(self, client, tmp_path):
        a_file = tmp_path / "weights.safetensors"
        a_file.write_bytes(b"x")
        res = client.post("/api/genlab/paths", json={"models_dir": str(a_file)})
        assert res.status_code == 422

    def test_paths_accept_custom_models_dir_and_creates_subfolders(self, client, tmp_path):
        target = str(tmp_path / "внешние модели")
        res = client.post("/api/genlab/paths", json={"models_dir": target})
        assert res.status_code == 200, res.text
        assert res.json()["models_dir_is_custom"] is True
        assert os.path.isdir(os.path.join(target, "checkpoints"))
        assert os.path.isdir(os.path.join(target, "diffusion_models"))
        # Возвращаем как было — тесты не должны оставлять изменённый конфиг.
        back = client.post("/api/genlab/paths", json={"models_dir": ""})
        assert back.status_code == 200
        assert back.json()["models_dir_is_custom"] is False

    def test_paths_nothing_to_save(self, client):
        assert client.post("/api/genlab/paths", json={}).status_code == 422

    def test_paths_treat_standard_models_dir_as_default(self, client):
        """Явно указанная стандартная папка = то же, что пустое поле.

        Иначе в конфиг запеклась бы буква диска, и сборка перестала бы
        переноситься.
        """
        standard = os.path.join(gconfig.comfy_root(), "ComfyUI", "models")
        res = client.post("/api/genlab/paths", json={"models_dir": standard})
        assert res.status_code == 200
        assert res.json()["models_dir_is_custom"] is False
        assert gconfig.get_settings().get("models_dir", "") == ""

    def test_paths_treat_autodetected_comfy_root_as_auto(self, client):
        detected = gconfig.detect_comfy_root()
        res = client.post("/api/genlab/paths", json={"comfy_root": detected})
        assert res.status_code == 200
        data = client.get("/api/genlab/paths").json()
        assert data["comfy_root_is_custom"] is False

    def test_paths_keep_genuinely_custom_comfy_root(self, client):
        """Свой путь ComfyUI должен сохраниться, а не схлопнуться в автопоиск."""
        detected = gconfig.detect_comfy_root()
        assert os.path.normcase(gconfig.comfy_root()) == os.path.normcase(detected)
        client.post("/api/genlab/paths", json={"comfy_root": detected})
        assert client.get("/api/genlab/paths").json()["comfy_root_is_custom"] is False

        # Другой валидный путь (копия установки) должен сохраниться как свой.
        other = os.path.join(gconfig.app_root(), "ComfyUI_windows_portable")
        if os.path.normcase(other) == os.path.normcase(detected):
            pytest.skip("альтернативной установки ComfyUI нет")
        res = client.post("/api/genlab/paths", json={"comfy_root": other})
        assert res.status_code == 200
        assert client.get("/api/genlab/paths").json()["comfy_root_is_custom"] is True

    def test_compose_endpoint(self, client, tmp_path):
        from PIL import Image
        import io
        ids = []
        for i, color in enumerate([(255, 255, 255), (0, 255, 0)]):
            buf = io.BytesIO()
            Image.new("RGB", (32, 32), color).save(buf, "PNG")
            res = client.post("/api/genlab/upload",
                              files={"file": ("layer%d.png" % i, buf.getvalue(), "image/png")})
            ids.append(res.json()["asset"]["id"])
        res = client.post("/api/genlab/edit/compose",
                          json={"layers": [{"asset_id": ids[0]},
                                           {"asset_id": ids[1], "x": 4, "y": 4}]})
        assert res.status_code == 200
        assert res.json()["asset"]["id"]
        for asset_id in ids:
            client.post("/api/genlab/assets/%s" % asset_id, json={"action": "delete"})

    def test_compose_without_layers(self, client):
        assert client.post("/api/genlab/edit/compose",
                           json={"layers": []}).status_code == 422

    def test_static_assets_exist(self, client):
        for path in ("/static/genlab/index.html", "/static/genlab/genlab.css",
                     "/static/genlab/genlab.js"):
            assert client.get(path).status_code == 200, path

    def test_editor_offers_every_image_edit_mode(self, client):
        """Редактор области должен уметь все режимы, что есть в бэкенде."""
        js = client.get("/static/genlab/editor.js").text
        for preset in ("edit.inpaint", "edit.outpaint", "edit.upscale",
                       "edit.controlnet", "edit.instruct", "edit.removebg"):
            assert preset in js, preset

    def test_audio_editor_wires_repaint_preset(self, client):
        js = client.get("/static/genlab/audio.js").text
        assert "audio.repaint" in js


# ──────────────────────────────────────────────────────────────────────────────
# 7. Регистрация в приложении
# ──────────────────────────────────────────────────────────────────────────────

class TestWiring:
    """Модуль обязан быть подключён ДО catch-all иначе запросы уйдут в legacy."""

    def test_router_declares_genlab_paths(self):
        paths = {r.path for r in genlab_router.routes}
        assert "/genlab" in paths
        assert "/api/genlab/status" in paths
        assert "/api/genlab/generate" in paths

    def test_websocket_route_registered(self):
        kinds = {getattr(r, "path", "") for r in genlab_router.routes}
        assert "/api/genlab/ws" in kinds

    def test_router_registered_before_legacy_catchall(self):
        """GenLab подключается раньше legacy catch-all, иначе его маршруты мертвы."""
        main_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "main.py")
        with open(main_py, encoding="utf-8-sig") as fh:
            source = fh.read()
        include = source.find("from genlab.api import router")
        assert include != -1, "GenLab не подключён в main.py"
        # Сам catch-all переехал в роутер legacy-моста: он подаётся последним,
        # поэтому важна только очерёдность его подключения.
        catchall = source.find("from routes.legacy_collaboration import router")
        assert catchall != -1, "legacy catch-all не найден"
        assert include < catchall, "include_router после catch-all — маршруты не сработают"

    def test_middleware_knows_genlab_paths(self):
        """Диспетчер решает по белому списку — GenLab должен быть в нём."""
        dispatch_py = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "route_dispatch.py")
        with open(dispatch_py, encoding="utf-8-sig") as fh:
            source = fh.read()
        assert "'/api/genlab'" in source, "нет префикса API в route_dispatch"
        assert "'/genlab'" in source, "нет страницы в route_dispatch"


# ──────────────────────────────────────────────────────────────────────────────
# 8. Гигиена кода
# ──────────────────────────────────────────────────────────────────────────────

class TestCodeHygiene:
    def test_files_are_utf8_without_bom(self):
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "genlab")
        for root, _dirs, files in os.walk(base):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                with open(path, "rb") as fh:
                    data = fh.read()
                assert not data.startswith(b"\xef\xbb\xbf"), "BOM в %s" % path
                text = data.decode("utf-8")
                # Признак двойного кодирования кириллицы (PowerShell Get/Set-Content)
                assert "\u0420\u0401" not in text, "мохибейк в %s" % path

    def test_all_modules_importable(self):
        import importlib
        for name in ("config", "comfy_client", "service", "graph", "store",
                     "catalog", "imaging", "api"):
            importlib.import_module("genlab.%s" % name)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))