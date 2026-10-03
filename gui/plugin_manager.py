"""Plugin discovery and lifecycle management for VM-Harness.

PluginManager scans one or more plugin directories for Python files that
contain VMHarnessPlugin subclasses, loads them, and provides access to
the instantiated plugins.

Usage:
    from gui.plugin_manager import PluginManager

    pm = PluginManager()
    pm.discover_plugins()
    pm.load_all()

    for panel_plugin in pm.get_panels():
        widget = panel_plugin.create_panel(parent_window)
        # ... add to QStackedWidget
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

from gui.plugin import (
    BackendPlugin,
    MiddlewarePlugin,
    PanelPlugin,
    PluginContext,
    PluginMetadata,
    VMHarnessPlugin,
)

logger = logging.getLogger("vmharness.plugin_manager")


class PluginManager:
    """Discovers, loads, and manages VM-Harness plugins.

    Plugin directories are scanned for ``*.py`` files.  Each file is imported
    and inspected for subclasses of :class:`VMHarnessPlugin`.  Valid plugin
    classes are instantiated and tracked.

    Attributes:
        _plugins: Mapping of plugin name → loaded plugin instance.
        _plugin_dirs: List of directories to scan for plugins.
        _discovered: Mapping of plugin name → (class, module_path) tuples found
            during the most recent :meth:`discover_plugins` call.
    """

    PLUGIN_ENTRY_POINT = "vmharness.plugins"

    def __init__(self, plugin_dirs: Optional[List[Path]] = None):
        self._plugins: Dict[str, VMHarnessPlugin] = {}
        self._plugin_dirs = plugin_dirs or self._default_plugin_dirs()
        self._discovered: Dict[str, Tuple[Type[VMHarnessPlugin], Path]] = {}

    # ── Discovery ─────────────────────────────────────────────────────────

    @staticmethod
    def _default_plugin_dirs() -> List[Path]:
        """Return the default plugin search paths."""
        dirs: List[Path] = []

        # 1. User-level plugins
        user_plugins = Path.home() / ".vmharness" / "plugins"
        dirs.append(user_plugins)

        # 2. Project-level plugins (next to the gui/ package)
        gui_dir = Path(__file__).resolve().parent
        project_plugins = gui_dir.parent / "plugins"
        dirs.append(project_plugins)

        return dirs

    def discover_plugins(self) -> List[PluginMetadata]:
        """Scan plugin directories and return metadata for all discovered plugins.

        Each ``.py`` file in every plugin directory is imported and inspected
        for :class:`VMHarnessPlugin` subclasses.  Duplicate plugin names are
        silently skipped (first-found wins).

        Returns:
            List of PluginMetadata for every discovered plugin class.
        """
        self._discovered.clear()
        result: List[PluginMetadata] = []

        for plugin_dir in self._plugin_dirs:
            if not plugin_dir.exists():
                continue

            for py_file in sorted(plugin_dir.glob("*.py")):
                if py_file.name.startswith("_"):
                    continue  # skip __init__.py, _private.py, etc.

                try:
                    self._scan_file(py_file, result)
                except Exception as exc:
                    logger.warning(
                        "Failed to scan plugin file %s: %s", py_file, exc
                    )

        return result

    def _scan_file(self, py_file: Path, result: List[PluginMetadata]) -> None:
        """Import a single .py file and register any plugin classes found."""
        module_name = f"_vmharness_plugin_{py_file.stem}"

        spec = importlib.util.spec_from_file_location(module_name, str(py_file))
        if spec is None or spec.loader is None:
            return

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if not issubclass(obj, VMHarnessPlugin):
                continue
            if obj is VMHarnessPlugin:
                continue
            if issubclass(obj, (PanelPlugin, BackendPlugin, MiddlewarePlugin)):
                # Only register concrete leaf classes, not the ABCs themselves
                if inspect.isabstract(obj):
                    continue

            # Try to get metadata without instantiating (metadata is a property
            # on the class, but we need an instance).  We instantiate here.
            try:
                instance = obj()
                meta = instance.metadata
            except Exception as exc:
                logger.warning(
                    "Skipping plugin class %s in %s: %s", _name, py_file, exc
                )
                continue

            if meta.name in self._discovered:
                logger.debug(
                    "Duplicate plugin name '%s' in %s — skipping", meta.name, py_file
                )
                continue

            self._discovered[meta.name] = (obj, py_file)
            result.append(meta)
            logger.info(
                "Discovered plugin: %s v%s (%s) from %s",
                meta.name, meta.version, meta.category, py_file.name,
            )

    # ── Load / Unload ─────────────────────────────────────────────────────

    def load_plugin(self, name: str) -> Optional[VMHarnessPlugin]:
        """Load and initialize a plugin by name.

        The plugin must have been discovered first (via :meth:`discover_plugins`).
        If already loaded, returns the existing instance.

        Args:
            name: The plugin name (from PluginMetadata.name).

        Returns:
            The loaded plugin instance, or None if not found.
        """
        if name in self._plugins:
            return self._plugins[name]

        if name not in self._discovered:
            logger.warning("Plugin '%s' not discovered — call discover_plugins() first", name)
            return None

        plugin_cls, py_file = self._discovered[name]

        try:
            instance = plugin_cls()
        except Exception as exc:
            logger.error("Failed to instantiate plugin '%s': %s", name, exc)
            return None

        # Build a minimal PluginContext — the caller can enrich it later
        context = PluginContext()

        try:
            # initialize() is async — run it in a temporary event loop if needed
            loop = self._get_or_create_loop()
            loop.run_until_complete(instance.initialize(context))
        except Exception as exc:
            logger.error("Failed to initialize plugin '%s': %s", name, exc)
            return None

        self._plugins[name] = instance
        logger.info("Loaded plugin: %s", name)
        return instance

    def load_all(self) -> List[VMHarnessPlugin]:
        """Load and initialize all discovered plugins.

        Returns:
            List of successfully loaded plugin instances.
        """
        loaded: List[VMHarnessPlugin] = []
        for name in list(self._discovered.keys()):
            plugin = self.load_plugin(name)
            if plugin is not None:
                loaded.append(plugin)
        return loaded

    def unload_plugin(self, name: str) -> None:
        """Unload and clean up a plugin.

        Calls the plugin's ``shutdown()`` coroutine and removes it from
        the loaded plugins dict.

        Args:
            name: The plugin name to unload.
        """
        if name not in self._plugins:
            return

        instance = self._plugins.pop(name)
        try:
            loop = self._get_or_create_loop()
            loop.run_until_complete(instance.shutdown())
        except Exception as exc:
            logger.warning("Error during shutdown of plugin '%s': %s", name, exc)

        logger.info("Unloaded plugin: %s", name)

    def unload_all(self) -> None:
        """Unload all loaded plugins."""
        for name in list(self._plugins.keys()):
            self.unload_plugin(name)

    # ── Accessors ─────────────────────────────────────────────────────────

    def get_panels(self) -> List[PanelPlugin]:
        """Get all loaded panel plugins."""
        return [p for p in self._plugins.values() if isinstance(p, PanelPlugin)]

    def get_backends(self) -> List[BackendPlugin]:
        """Get all loaded backend plugins."""
        return [p for p in self._plugins.values() if isinstance(p, BackendPlugin)]

    def get_middleware(self) -> List[MiddlewarePlugin]:
        """Get all loaded middleware plugins."""
        return [p for p in self._plugins.values() if isinstance(p, MiddlewarePlugin)]

    def get_plugin(self, name: str) -> Optional[VMHarnessPlugin]:
        """Get a loaded plugin by name."""
        return self._plugins.get(name)

    @property
    def loaded_plugins(self) -> Dict[str, VMHarnessPlugin]:
        """Return a copy of the loaded plugins dict."""
        return dict(self._plugins)

    @property
    def discovered_plugins(self) -> Dict[str, PluginMetadata]:
        """Return metadata for all discovered plugins."""
        return {
            name: cls().metadata for name, (cls, _path) in self._discovered.items()
        }

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _get_or_create_loop():
        """Get the current event loop or create a new one."""
        try:
            return asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop


__all__ = ["PluginManager"]
