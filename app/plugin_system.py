"""
Plugin System Module
Hot-reload плагины для расширения возможностей агента
"""

import os
import sys
import json
import time
import importlib
import logging
import shutil
import threading
from typing import Dict, List, Optional, Any, Callable
from pathlib import Path

logger = logging.getLogger("infinity")

# Директория
AGENT_BASE = os.path.expanduser("~/.infinity/asio_agent")
PLUGINS_DIR = os.path.join(AGENT_BASE, "plugins")
os.makedirs(PLUGINS_DIR, exist_ok=True)

# Добавляем в sys.path
if PLUGINS_DIR not in sys.path:
    sys.path.insert(0, PLUGINS_DIR)

_manager_lock = threading.Lock()
_MANAGERS: Dict[str, "PluginManager"] = {}


def get_manager() -> "PluginManager":
    """Глобальный менеджер плагинов (кэшируется по каталогу)."""
    with _manager_lock:
        mgr = _MANAGERS.get(PLUGINS_DIR)
        if mgr is None:
            mgr = PluginManager()
            _MANAGERS[PLUGINS_DIR] = mgr
        return mgr


def get_manager_for(base_dir) -> "PluginManager":
    """Менеджер для конкретного каталога без смены глобального PLUGINS_DIR.

    Используется админ-панелью для управления плагинами любого профиля,
    не трогая каталог текущего.
    """
    base_dir = os.path.normpath(base_dir)
    prev = PLUGINS_DIR
    try:
        if base_dir != PLUGINS_DIR:
            set_base_dir(base_dir)
        return get_manager()
    finally:
        if PLUGINS_DIR != prev:
            set_base_dir(prev)


def set_base_dir(base_dir=None):
    """Перенаправить каталог плагинов (пер-профильный)."""
    global PLUGINS_DIR
    if base_dir is None:
        base_dir = os.path.join(AGENT_BASE, "plugins")
    base_dir = os.path.normpath(base_dir)
    if base_dir != PLUGINS_DIR:
        os.makedirs(base_dir, exist_ok=True)
        if PLUGINS_DIR in sys.path:
            sys.path.remove(PLUGINS_DIR)
        if base_dir not in sys.path:
            sys.path.insert(0, base_dir)
        PLUGINS_DIR = base_dir


class PluginMetadata:
    """Метаданные плагина."""
    
    def __init__(
        self,
        name: str,
        version: str,
        description: str,
        author: str = "",
        entry_point: str = "main",
        hooks: List[str] = None,
        dependencies: List[str] = None,
        enabled: bool = True,
        **extra,
    ):
        self.name = name
        self.version = version
        self.description = description
        self.author = author
        self.entry_point = entry_point
        self.hooks = hooks or []
        self.dependencies = dependencies or []
        
        # Флаг, заявленный самим плагином. Раньше параметр enabled не
        # принимался, и плагин вида PluginMetadata(..., enabled=True)
        # падал с TypeError и не загружался. Храним отдельно от рантайм-
        # поля self.enabled, которое выставляет загрузчик.
        self.declared_enabled = bool(enabled)
        self.extra = dict(extra)

        # Runtime
        self.enabled = False
        self.loaded = False
        self.instance: Optional[Any] = None
        self.load_time: Optional[float] = None
        self.error: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "entry_point": self.entry_point,
            "hooks": self.hooks,
            "dependencies": self.dependencies,
            "enabled": self.enabled,
            "loaded": self.loaded,
            "error": self.error
        }


class PluginManager:
    """Менеджер плагинов."""
    
    def __init__(self):
        self.plugins: Dict[str, PluginMetadata] = {}
        self.hooks: Dict[str, List[Callable]] = {}
        self._load_plugins()
    
    def _load_plugins(self):
        """Загружает все плагины из директории."""
        if not os.path.exists(PLUGINS_DIR):
            return
        
        for item in os.listdir(PLUGINS_DIR):
            item_path = os.path.join(PLUGINS_DIR, item)
            
            # Python файл плагин
            if item.endswith(".py") and not item.startswith("_"):
                self._load_plugin_file(item_path)
            
            # Директория плагина
            elif os.path.isdir(item_path) and os.path.exists(os.path.join(item_path, "plugin.json")):
                self._load_plugin_dir(item_path)
    
    def _load_plugin_file(self, filepath: str):
        """Загружает плагин из файла."""
        try:
            module_name = Path(filepath).stem
            spec = importlib.util.spec_from_file_location(module_name, filepath)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            
            # Ищем метаданные
            metadata = getattr(module, "PLUGIN_METADATA", None)
            if not metadata:
                metadata = PluginMetadata(
                    name=module_name,
                    version="1.0.0",
                    description=getattr(module, "__doc__", "")
                )
            
            plugin = PluginMetadata(**metadata.to_dict() if hasattr(metadata, 'to_dict') else metadata.__dict__)
            plugin.instance = module
            plugin.loaded = True
            plugin.load_time = time.time()
            # Начальное состояние берём из флага, объявленного плагином.
            # Дальше им можно управлять через enable_plugin/disable_plugin,
            # которые перезаписывают это поле и сохраняют его на диск.
            plugin.enabled = bool(getattr(plugin, "declared_enabled", True))
            
            self.plugins[plugin.name] = plugin
            
            # Регистрируем hooks
            for hook_name in plugin.hooks:
                if hook_name not in self.hooks:
                    self.hooks[hook_name] = []
                
                hook_func = getattr(module, f"on_{hook_name}", None)
                if hook_func:
                    self.hooks[hook_name].append(hook_func)
            
            logger.info(f"Plugin loaded: {plugin.name} v{plugin.version}")
            
        except Exception as e:
            logger.error(f"Failed to load plugin {filepath}: {e}")
    
    def _load_plugin_dir(self, dirpath: str):
        """Загружает плагин из директории."""
        try:
            with open(os.path.join(dirpath, "plugin.json"), "r", encoding="utf-8") as f:
                config = json.load(f)
            
            entry_point = config.get("entry_point", "main.py")
            entry_path = os.path.join(dirpath, entry_point)
            
            if os.path.exists(entry_path):
                self._load_plugin_file(entry_path)
        except Exception as e:
            logger.error(f"Failed to load plugin dir {dirpath}: {e}")
    
    def enable_plugin(self, name: str) -> bool:
        """Включает плагин."""
        if name in self.plugins:
            self.plugins[name].enabled = True
            self._save_plugin_state()
            return True
        return False
    
    def disable_plugin(self, name: str) -> bool:
        """Отключает плагин."""
        if name in self.plugins:
            self.plugins[name].enabled = False
            self._save_plugin_state()
            return True
        return False
    
    def reload_plugin(self, name: str) -> bool:
        """Перезагружает плагин (hot-reload)."""
        if name not in self.plugins:
            return False
        
        plugin = self.plugins[name]
        
        try:
            # Unload hooks
            for hook_name in plugin.hooks:
                if hook_name in self.hooks:
                    self.hooks[hook_name] = [
                        h for h in self.hooks[hook_name]
                        if h.__module__ != plugin.instance.__name__
                    ]
            
            # Reload module
            if plugin.instance and hasattr(plugin.instance, "__file__"):
                importlib.reload(plugin.instance)
                plugin.loaded = True
                plugin.load_time = time.time()
                plugin.error = None
                
                # Re-register hooks
                for hook_name in plugin.hooks:
                    if hook_name not in self.hooks:
                        self.hooks[hook_name] = []
                    
                    hook_func = getattr(plugin.instance, f"on_{hook_name}", None)
                    if hook_func:
                        self.hooks[hook_name].append(hook_func)
                
                logger.info(f"Plugin reloaded: {name}")
                return True
        except Exception as e:
            plugin.error = str(e)
            logger.error(f"Failed to reload plugin {name}: {e}")
            return False
        
        return False
    
    def execute_hook(self, hook_name: str, **kwargs) -> List[Any]:
        """Выполняет hook для всех включённых плагинов."""
        results = []
        
        if hook_name not in self.hooks:
            return results
        
        for hook_func in self.hooks[hook_name]:
            try:
                # Проверяем enabled статус плагина
                module_name = hook_func.__module__
                plugin = next(
                    (p for p in self.plugins.values() if p.instance and p.instance.__name__ == module_name),
                    None
                )
                
                if plugin and plugin.enabled:
                    result = hook_func(**kwargs)
                    results.append(result)
            except Exception as e:
                logger.error(f"Hook {hook_name} execution failed: {e}")
        
        return results

    def get_plugin_list(self) -> List[Dict[str, Any]]:
        """Получает список всех плагинов."""
        return [plugin.to_dict() for plugin in self.plugins.values()]

    def delete_plugin(self, name: str) -> bool:
        """Удаляет плагин (файл или директорию) из каталога."""
        if name not in self.plugins:
            return False
        plugin = self.plugins.pop(name)
        if plugin.instance and hasattr(plugin.instance, "__file__"):
            filepath = os.path.abspath(plugin.instance.__file__)
            if os.path.exists(filepath):
                try:
                    if filepath.startswith(os.path.abspath(PLUGINS_DIR) + os.sep):
                        os.remove(filepath)
                        return True
                except Exception as e:
                    logger.error(f"Failed to delete plugin file {filepath}: {e}")
        dirpath = os.path.join(PLUGINS_DIR, name)
        if os.path.isdir(dirpath):
            try:
                shutil.rmtree(dirpath)
                return True
            except Exception as e:
                logger.error(f"Failed to delete plugin dir {dirpath}: {e}")
        return False

    def _save_plugin_state(self):
        """Сохраняет состояние плагинов."""
        state_file = os.path.join(PLUGINS_DIR, "plugins_state.json")
        try:
            state = {
                name: {"enabled": plugin.enabled}
                for name, plugin in self.plugins.items()
            }
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save plugin state: {e}")


# Available hooks:
# - before_message: before processing user message
# - after_message: after agent response
# - before_action: before executing action
# - after_action: after executing action
# - on_error: when error occurs
# - on_session_start: new session created
# - on_session_end: session closed
