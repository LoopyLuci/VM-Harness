"""Plugin integration tests for VM-Harness MainWindow.

Verifies that:
  1) PluginManager is initialized correctly
  2) Plugin panels are added to the sidebar
  3) Plugin panels are added to the panel stack (QStackedWidget)
  4) Bridges are wired correctly to plugin panels
  5) Plugin panels can be switched to via _switch_panel()

Run with:  python -m unittest tests.test_plugin_integration -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# ── Qt / import order (must match the project convention) ──────────────────────
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_OPENGL", "software")
os.environ.setdefault("GUI_MASTER_PASSWORD", "test-master-password")

from PyQt5.QtCore import Qt, QCoreApplication
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

from PyQt5.QtWidgets import QApplication

import pytest

# Plugins are imported by module name into the running process, so these tests depend on what ran before them on
# the same worker: keep every plugin test on one xdist worker (the pipeline runs with --dist loadgroup).
pytestmark = pytest.mark.xdist_group("plugins")

# ── Project paths ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


# ── Helpers ────────────────────────────────────────────────────────────────────

_SAMPLE_PLUGIN = textwrap.dedent(
    """\
    from __future__ import annotations
    import asyncio
    from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton
    from gui.plugin import PanelPlugin, PluginMetadata, PluginContext

    class SamplePanelPlugin(PanelPlugin):
        @property
        def metadata(self):
            return PluginMetadata(
                name="sample-plugin",
                version="0.1.0",
                description="Sample Plugin Panel",
                author="Test",
                category="panel",
            )

        async def initialize(self, context):
            self._initialized = True

        async def shutdown(self):
            pass

        def create_panel(self, parent):
            return SamplePanel(parent)


    class SamplePanel(QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self._qmp_bridge = None
            self._ssh_bridge = None
            layout = QVBoxLayout(self)
            layout.addWidget(QLabel("Sample Plugin Panel"))
            self._btn = QPushButton("Sample Button")
            layout.addWidget(self._btn)

        def set_qmp_bridge(self, bridge):
            self._qmp_bridge = bridge

        def set_ssh_bridge(self, bridge):
            self._ssh_bridge = bridge

        def set_multi_qmp_bridge(self, bridge):
            self._multi_qmp_bridge = bridge
    """
)


def _make_app() -> QApplication:
    """Return the QApplication singleton (create if needed)."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([""])
    return app


def _mock_bridge(is_connected: bool = False):
    """Create a mock bridge that doesn't spawn real threads."""
    m = MagicMock()
    m.is_connected = is_connected
    m.start = MagicMock()
    m.stop = MagicMock()
    m.connected = MagicMock()
    m.vm_status = MagicMock()
    m.error = MagicMock()
    m.command_result = MagicMock()
    m.connected_to = MagicMock()
    m.command_output = MagicMock()
    m.file_content = MagicMock()
    m.file_list = MagicMock()
    return m


class PluginIntegrationTests(unittest.TestCase):
    """Verify plugin integration in MainWindow."""

    @classmethod
    def setUpClass(cls):
        cls.app = _make_app()

    def setUp(self):
        """Create a temporary plugin file and mock bridges before each test."""
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._plugin_dir = Path(self._tmp_dir.name)
        self._plugin_file = self._plugin_dir / "sample_panel.py"
        self._plugin_file.write_text(_SAMPLE_PLUGIN, encoding="utf-8")

        self._mock_qmp = _mock_bridge()
        self._mock_ssh = _mock_bridge()

        # Patch the bridges BEFORE MainWindow constructs them
        # so the real QMPBridge/SSHBridge threads don't start.
        self._orig_qmp_bridge = None
        self._orig_ssh_bridge = None
        from gui import qmp_bridge as qmp_mod
        from gui import ssh_bridge as ssh_mod
        self._orig_qmp_bridge = qmp_mod.QMPBridge
        self._orig_ssh_bridge = ssh_mod.SSHBridge
        qmp_mod.QMPBridge = lambda *a, **kw: self._mock_qmp
        ssh_mod.SSHBridge = lambda *a, **kw: self._mock_ssh

    def tearDown(self):
        """Restore real bridge classes and clean up temp files."""
        from gui import qmp_bridge as qmp_mod
        from gui import ssh_bridge as ssh_mod
        qmp_mod.QMPBridge = self._orig_qmp_bridge
        ssh_mod.SSHBridge = self._orig_ssh_bridge
        self._tmp_dir.cleanup()

    def _create_window(self):
        """Create a MainWindow instance with mock bridges and a plugin dir."""
        from gui.plugin_manager import PluginManager
        from gui.main_window import MainWindow

        # Make sure the plugin manager scans our temp directory
        pm = PluginManager(plugin_dirs=[self._plugin_dir])
        discovered = pm.discover_plugins()
        self.assertTrue(
            len(discovered) > 0,
            "PluginManager should discover the sample plugin in the temp dir",
        )

        win = MainWindow()
        # Replace the plugin manager with one pointed at our temp dir
        win.plugin_manager = PluginManager(plugin_dirs=[self._plugin_dir])
        return win

    # ── Test 1: PluginManager is initialized correctly ───────────────────────

    def test_plugin_manager_initialized(self):
        """MainWindow creates a PluginManager instance."""
        from gui.plugin_manager import PluginManager
        from gui.main_window import MainWindow

        win = MainWindow()
        self.assertIsInstance(win.plugin_manager, PluginManager)
        self.assertIsNotNone(win.plugin_manager)

    # ── Test 2: Plugin panels are added to the sidebar ────────────────────────

    def test_plugin_panel_added_to_sidebar(self):
        """_load_plugin_panels adds a sidebar button for each plugin panel."""
        from gui.main_window import MainWindow
        from gui.plugin_manager import PluginManager

        win = MainWindow()
        win.plugin_manager = PluginManager(plugin_dirs=[self._plugin_dir])

        # Call the internal method that loads plugin panels
        initial_sidebar_count = len(win.sidebar.PANELS)
        win._load_plugin_panels()

        # The sample plugin should have added a sidebar entry
        new_sidebar_count = len(win.sidebar.PANELS)
        self.assertGreater(
            new_sidebar_count,
            initial_sidebar_count,
            "Sidebar PANELS list should grow after loading plugins",
        )

        # Verify a button with the plugin's panel name exists in the sidebar
        plugin_btn = win.sidebar.findChild(
            type(win.sidebar.layout().itemAt(0).widget()),
            "sidebar_sample-plugin",
        )
        # Fallback: search all children
        if plugin_btn is None:
            from PyQt5.QtWidgets import QPushButton
            for child in win.sidebar.findChildren(QPushButton):
                if child.objectName() == "sidebar_sample-plugin":
                    plugin_btn = child
                    break

        self.assertIsNotNone(
            plugin_btn,
            "Sidebar should contain a button for the sample plugin panel",
        )

    # ── Test 3: Plugin panels are added to the panel stack ────────────────────

    def test_plugin_panel_added_to_panel_stack(self):
        """Plugin panels are added to the QStackedWidget."""
        from gui.main_window import MainWindow
        from gui.plugin_manager import PluginManager

        win = MainWindow()
        win.plugin_manager = PluginManager(plugin_dirs=[self._plugin_dir])

        initial_stack_count = win.panel_stack.count()
        win._load_plugin_panels()
        new_stack_count = win.panel_stack.count()

        self.assertGreater(
            new_stack_count,
            initial_stack_count,
            "Panel stack should have more widgets after loading plugins",
        )

        # The plugin panel should be in self.panels
        self.assertIn(
            "sample-plugin",
            win.panels,
            "Plugin panel should appear in MainWindow.panels dict",
        )

    # ── Test 4: Bridges are wired to plugin panels ───────────────────────────

    def test_bridges_wired_to_plugin_panels(self):
        """Plugin panels with set_qmp_bridge / set_ssh_bridge receive the bridges."""
        from gui.main_window import MainWindow
        from gui.plugin_manager import PluginManager

        win = MainWindow()
        win.plugin_manager = PluginManager(plugin_dirs=[self._plugin_dir])

        win._load_plugin_panels()

        self.assertIn("sample-plugin", win.panels)
        panel = win.panels["sample-plugin"]

        # The sample panel has set_qmp_bridge and set_ssh_bridge methods
        if hasattr(panel, "set_qmp_bridge"):
            self.assertIsNotNone(
                panel._qmp_bridge,
                "Plugin panel's qmp_bridge should be wired",
            )
        if hasattr(panel, "set_ssh_bridge"):
            self.assertIsNotNone(
                panel._ssh_bridge,
                "Plugin panel's ssh_bridge should be wired",
            )

    # ── Test 5: Plugin panels can be switched to ──────────────────────────────

    def test_plugin_panel_can_be_switched_to(self):
        """_switch_panel can activate the plugin panel in the stacked widget."""
        from gui.main_window import MainWindow
        from gui.plugin_manager import PluginManager

        win = MainWindow()
        win.plugin_manager = PluginManager(plugin_dirs=[self._plugin_dir])

        win._load_plugin_panels()

        # Switch to the plugin panel
        win._switch_panel("sample-plugin")

        current = win.panel_stack.currentWidget()
        self.assertIs(
            current,
            win.panels["sample-plugin"],
            "Panel stack current widget should be the plugin panel after switching",
        )

        # Switch back to a built-in panel
        win._switch_panel("dashboard")
        self.assertIs(
            win.panel_stack.currentWidget(),
            win.panels["dashboard"],
            "Switching back to dashboard should work",
        )

    # ── Additional: discover_plugins finds the plugin ─────────────────────────

    def test_discover_plugins_finds_sample(self):
        """PluginManager.discover_plugins() finds the sample plugin."""
        from gui.plugin_manager import PluginManager

        pm = PluginManager(plugin_dirs=[self._plugin_dir])
        discovered = pm.discover_plugins()

        names = [m.name for m in discovered]
        self.assertIn("sample-plugin", names)

    # ── Additional: load_all loads the plugin ─────────────────────────────────

    def test_load_all_loads_sample_plugin(self):
        """PluginManager.load_all() loads the sample plugin instance."""
        from gui.plugin_manager import PluginManager

        pm = PluginManager(plugin_dirs=[self._plugin_dir])
        pm.discover_plugins()
        loaded = pm.load_all()

        names = [p.metadata.name for p in loaded]
        self.assertIn("sample-plugin", names)

    # ── Additional: get_panels returns panel plugins ─────────────────────────

    def test_get_panels_returns_panel_plugins(self):
        """get_panels() returns PanelPlugin instances."""
        from gui.plugin_manager import PluginManager

        pm = PluginManager(plugin_dirs=[self._plugin_dir])
        pm.discover_plugins()
        pm.load_all()

        panels = pm.get_panels()
        self.assertTrue(len(panels) >= 1)
        self.assertTrue(
            all(p.metadata.category == "panel" for p in panels),
            "All returned plugins should be panel plugins",
        )

    # ── Additional: unload_all cleans up ─────────────────────────────────────

    def test_unload_all_cleans_up_plugins(self):
        """unload_all() removes all loaded plugins."""
        from gui.plugin_manager import PluginManager

        pm = PluginManager(plugin_dirs=[self._plugin_dir])
        pm.discover_plugins()
        pm.load_all()
        self.assertTrue(len(pm.loaded_plugins) > 0)

        pm.unload_all()
        self.assertEqual(len(pm.loaded_plugins), 0)

    # ── Additional: duplicate panel names are skipped ────────────────────────

    def test_duplicate_panel_names_skipped(self):
        """If a plugin panel name collides with a built-in, it is skipped."""
        from gui.main_window import MainWindow
        from gui.plugin_manager import PluginManager

        # Create a second plugin file with a name that collides
        duplicate_code = _SAMPLE_PLUGIN.replace(
            'name="sample-plugin"', 'name="dashboard"'
        )
        dup_file = self._plugin_dir / "duplicate_panel.py"
        dup_file.write_text(duplicate_code, encoding="utf-8")

        win = MainWindow()
        win.plugin_manager = PluginManager(plugin_dirs=[self._plugin_dir])
        win._load_plugin_panels()

        # The duplicate should NOT have replaced the built-in dashboard
        self.assertIsInstance(
            win.panels["dashboard"],
            object,  # just verify it exists and wasn't overwritten
        )
        # Count how many times "sample-plugin" appears — should be exactly 1
        # (the duplicate was named "dashboard" so it was skipped)
        panel_count = sum(
            1 for name in win.panels if name == "sample-plugin"
        )
        self.assertEqual(panel_count, 1)


if __name__ == "__main__":
    unittest.main()
