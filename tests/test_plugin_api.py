"""Tests for the plugin API (PluginManager, PanelPlugin, etc.).

Covers plugin discovery, loading, panel creation, and unloading.
Run with: python -m pytest tests/test_plugin_api.py -v
   or:   python tests/test_plugin_api.py
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

# Ensure QT_QPA_PLATFORM is offscreen before any Qt imports
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PyQt5.QtWidgets import QApplication, QWidget

from gui.plugin import PanelPlugin, VMHarnessPlugin
from gui.plugin_manager import PluginManager

import pytest

# Plugins are imported by module name into the running process, so these tests depend on what ran before them on
# the same worker: keep every plugin test on one xdist worker (the pipeline runs with --dist loadgroup).
pytestmark = pytest.mark.xdist_group("plugins")

# Skip the entire test class if no plugins are discovered
def _plugins_available():
    pm = PluginManager()
    return len(pm.discover_plugins()) > 0


@unittest.skipUnless(_plugins_available(), "No plugins found in plugins/ directory")
class TestPluginAPI(unittest.TestCase):
    """Test plugin discovery, loading, panel creation, and unloading."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_01_discover_plugins(self):
        """PluginManager discovers plugins from the plugins/ directory."""
        pm = PluginManager()
        discovered = pm.discover_plugins()

        self.assertGreater(
            len(discovered), 0,
            "No plugins found — expected at least the example panel"
        )

        names = [m.name for m in discovered]
        self.assertIn("example-panel", names)

    def test_02_load_all_plugins(self):
        """PluginManager loads all discovered plugins."""
        pm = PluginManager()
        pm.discover_plugins()
        loaded = pm.load_all()

        self.assertGreater(len(loaded), 0)
        for plugin in loaded:
            self.assertIsInstance(plugin, VMHarnessPlugin)

    def test_03_example_panel_is_panel_plugin(self):
        """The example-panel plugin is loaded and is a PanelPlugin."""
        pm = PluginManager()
        pm.discover_plugins()
        pm.load_all()

        plugin = pm.get_plugin("example-panel")
        self.assertIsNotNone(plugin, "example-panel plugin not loaded")
        self.assertIsInstance(plugin, PanelPlugin)
        self.assertIsInstance(plugin, VMHarnessPlugin)

    def test_04_create_panel_returns_qwidget(self):
        """PanelPlugin.create_panel returns a QWidget instance."""
        pm = PluginManager()
        pm.discover_plugins()
        pm.load_all()

        plugin = pm.get_plugin("example-panel")
        self.assertIsNotNone(plugin)

        panel = plugin.create_panel(None)
        self.assertIsNotNone(panel)
        self.assertIsInstance(panel, QWidget)

        panel.deleteLater()

    def test_05_get_panels_filters_correctly(self):
        """get_panels() returns only PanelPlugin instances."""
        pm = PluginManager()
        pm.discover_plugins()
        pm.load_all()

        panels = pm.get_panels()
        self.assertGreater(len(panels), 0)
        for p in panels:
            self.assertIsInstance(p, PanelPlugin)

    def test_06_unload_plugin(self):
        """unload_plugin removes the plugin from loaded plugins."""
        pm = PluginManager()
        pm.discover_plugins()
        pm.load_all()

        self.assertIsNotNone(pm.get_plugin("example-panel"))

        pm.unload_plugin("example-panel")

        self.assertIsNone(pm.get_plugin("example-panel"))

    def test_07_unload_all(self):
        """unload_all removes every loaded plugin."""
        pm = PluginManager()
        pm.discover_plugins()
        pm.load_all()

        self.assertGreater(len(pm.loaded_plugins), 0)

        pm.unload_all()

        self.assertEqual(len(pm.loaded_plugins), 0)

    def test_08_load_plugin_unknown_returns_none(self):
        """Loading a plugin that was never discovered returns None."""
        pm = PluginManager()
        result = pm.load_plugin("nonexistent-plugin")
        self.assertIsNone(result)

    def test_09_no_plugins_directory(self):
        """PluginManager works when no plugins directory exists."""
        pm = PluginManager(plugin_dirs=[Path("/nonexistent/path")])
        discovered = pm.discover_plugins()
        self.assertEqual(len(discovered), 0)

        loaded = pm.load_all()
        self.assertEqual(len(loaded), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
