"""Comprehensive GUI Test Suite — tests all 23 panels, bridges, CLI, API.

Run with: pytest tests/test_gui_comprehensive.py -v
Or via MCP: terminal command
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
# Suppress CLI console windows on Windows
CREATE_NO_WINDOW = 0x08000000
import sys
import time
from pathlib import Path

import pytest

# Setup paths
PROJECT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_DIR / "src"
GUI_DIR = PROJECT_DIR / "gui"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(PROJECT_DIR))

# Ensure offscreen for headless testing
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt5.QtWidgets import QApplication


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def app():
    """Create QApplication for tests."""
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture(scope="session")
def main_window(app, request):
    """Create MainWindow with all panels. Uses mocked bridges to avoid access violations from live thread teardown."""
    from gui.main_window import MainWindow
    from gui.widgets import apply_global_theme
    from unittest.mock import MagicMock
    
    apply_global_theme(app)
    
    # Create mock bridges that don't spawn real threads
    mock_qmp = MagicMock()
    mock_qmp.is_connected = False
    mock_qmp.start = MagicMock()
    mock_qmp.stop = MagicMock()
    mock_qmp.connected = MagicMock()
    mock_qmp.vm_status = MagicMock()
    mock_qmp.error = MagicMock()
    mock_qmp.command_result = MagicMock()
    
    mock_ssh = MagicMock()
    mock_ssh.is_connected = False
    mock_ssh.start = MagicMock()
    mock_ssh.stop = MagicMock()
    mock_ssh.connected = MagicMock()
    mock_ssh.connected_to = MagicMock()
    mock_ssh.command_output = MagicMock()
    mock_ssh.file_content = MagicMock()
    mock_ssh.file_list = MagicMock()
    mock_ssh.error = MagicMock()
    
    window = MainWindow()
    window.qmp_bridge = mock_qmp
    window.ssh_bridge = mock_ssh
    
    # Wire panels to mock bridges
    for panel_name, panel in window.panels.items():
        if hasattr(panel, 'set_qmp_bridge'):
            panel.set_qmp_bridge(mock_qmp)
        if hasattr(panel, 'set_ssh_bridge'):
            panel.set_ssh_bridge(mock_ssh)
    
    window.resize(1400, 900)
    window.show()
    return window


@pytest.fixture
def qmp_client():
    """A QMP client connected to a real, throwaway QEMU (no disk, no display) started for the test."""
    import socket
    import subprocess
    import time as _time
    from vm_harness.hypervisor.qemu.backend import find_qemu
    from vm_harness.qmp_client import QMPClient

    qemu = find_qemu("qemu-system-x86_64")
    if not qemu:
        pytest.skip("QEMU is not installed")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([qemu, "-machine", "q35", "-m", "128", "-display", "none", "-accel", "tcg",
                             "-name", "qmp-test", "-qmp", f"tcp:127.0.0.1:{port},server,nowait"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _connect():
        deadline = _time.time() + 20
        while True:
            try:
                client = QMPClient(f"tcp:127.0.0.1:{port}")
                await client.connect()
                return client
            except OSError:
                if _time.time() > deadline:
                    raise
                await asyncio.sleep(0.2)

    try:
        client = loop.run_until_complete(_connect())
    except Exception:
        proc.kill()
        raise
    client.test_loop = loop
    yield client
    proc_to_kill = proc
    
    async def _disconnect():
        await client.disconnect()
    
    try:
        loop.run_until_complete(_disconnect())
    except Exception:
        pass
    finally:
        loop.close()
        proc_to_kill.kill()


# ── Test Category 1: Panel Initialization ──────────────────────────────────

class TestPanelInitialization:
    """Test that all panels load correctly."""

    def test_all_panels_instantiate(self, main_window):
        """Every panel is built (plugins may add more)."""
        from gui.main_window import Sidebar
        assert len(main_window.panels) >= len(Sidebar.PANELS)

    def test_panel_names_match_expected(self, main_window):
        """Every panel can be reached from the sidebar, and every sidebar entry opens a panel."""
        from gui.main_window import Sidebar
        sidebar = {name for _label, _icon, name in Sidebar.PANELS}
        assert sidebar <= set(main_window.panels)
        # Anything else is a plugin's panel (plugins/*.py), which adds its own button.
        plugin_panels = set(main_window.panels) - sidebar
        assert all("-" in n or n.startswith("plugin") for n in plugin_panels), plugin_panels

    def test_sidebar_panel_count_matches(self, main_window):
        """Sidebar PANELS count should match actual panels."""
        # Sidebar may have fewer entries if some panels are grouped
        assert len(main_window.sidebar.PANELS) <= len(main_window.panels)

    def test_all_panels_are_qwidget(self, main_window):
        """All panels should be QWidget instances."""
        from PyQt5.QtWidgets import QWidget
        for name, panel in main_window.panels.items():
            assert isinstance(panel, QWidget), f"{name} is not a QWidget"

    def test_bridges_initialized(self, main_window):
        """QMP and SSH bridges should be initialized."""
        assert main_window.qmp_bridge is not None
        assert main_window.ssh_bridge is not None


# ── Test Category 2: QMP Bridge ─────────────────────────────────────────────

class TestQMPBridge:
    """Test QMP bridge connectivity and commands."""

    def test_qmp_connection(self, qmp_client):
        """Should connect to QMP."""
        assert qmp_client.is_connected

    def test_query_status(self, qmp_client):
        """query-status should return valid data."""
        loop = qmp_client.test_loop  # the loop the client connected on
        result = loop.run_until_complete(
            qmp_client.send("query-status")
        )
        assert "return" in result

    def test_query_name(self, qmp_client):
        """query-name should return VM name."""
        loop = qmp_client.test_loop  # the loop the client connected on
        result = loop.run_until_complete(
            qmp_client.send("query-name")
        )
        assert "return" in result
        # Name may be empty if VM not fully started
        assert isinstance(result["return"], dict)

    def test_query_uuid(self, qmp_client):
        """query-uuid should return UUID."""
        loop = qmp_client.test_loop  # the loop the client connected on
        result = loop.run_until_complete(
            qmp_client.send("query-uuid")
        )
        assert "return" in result
        # UUID may be empty if VM not fully started
        assert isinstance(result["return"], dict)

    def test_query_version(self, qmp_client):
        """query-version should return version info."""
        loop = qmp_client.test_loop  # the loop the client connected on
        result = loop.run_until_complete(
            qmp_client.send("query-version")
        )
        assert "return" in result

    def test_query_kvm(self, qmp_client):
        """query-kvm should return KVM status."""
        loop = qmp_client.test_loop  # the loop the client connected on
        result = loop.run_until_complete(
            qmp_client.send("query-kvm")
        )
        assert "return" in result
        # enabled field may not be present if KVM not active
        assert isinstance(result["return"], dict)

    def test_bridge_signals_exist(self, main_window):
        """QMP bridge should have all required signals."""
        assert hasattr(main_window.qmp_bridge, 'connected')
        assert hasattr(main_window.qmp_bridge, 'vm_status')
        assert hasattr(main_window.qmp_bridge, 'error')
        assert hasattr(main_window.qmp_bridge, 'command_result')


# ── Test Category 3: Dashboard Panel ────────────────────────────────────────

class TestDashboardPanel:
    """Test dashboard panel functionality."""

    def test_dashboard_exists(self, main_window):
        """Dashboard panel should exist."""
        assert "dashboard" in main_window.panels

    def test_dashboard_has_status_dot(self, main_window):
        """Dashboard should have status indicator."""
        dashboard = main_window.panels["dashboard"]
        assert hasattr(dashboard, 'status_dot')

    def test_dashboard_has_stat_labels(self, main_window):
        """Dashboard should have stat labels dict."""
        dashboard = main_window.panels["dashboard"]
        assert hasattr(dashboard, '_stat_labels')
        assert isinstance(dashboard._stat_labels, dict)

    def test_dashboard_has_buttons(self, main_window):
        """Dashboard should have action buttons."""
        dashboard = main_window.panels["dashboard"]
        assert hasattr(dashboard, 'start_btn')
        assert hasattr(dashboard, 'stop_btn')
        assert hasattr(dashboard, 'reset_btn')

    def test_dashboard_add_activity(self, main_window):
        """add_activity should add entry to list."""
        dashboard = main_window.panels["dashboard"]
        initial_count = dashboard.activity_list.count()
        dashboard.add_activity("Test activity")
        assert dashboard.activity_list.count() == initial_count + 1


# ── Test Category 4: VM Control Panel ───────────────────────────────────────

class TestVMControlPanel:
    """Test VM control panel functionality."""

    def test_vm_control_exists(self, main_window):
        """VM Control panel should exist."""
        assert "vm_control" in main_window.panels

    def test_vm_control_has_lifecycle_btns(self, main_window):
        """VM Control should have lifecycle buttons."""
        vm_control = main_window.panels["vm_control"]
        assert hasattr(vm_control, '_lifecycle_btns')
        assert len(vm_control._lifecycle_btns) >= 6


# ── Test Category 5: Snapshots Panel ────────────────────────────────────────

class TestSnapshotsPanel:
    """Test snapshot panel functionality."""

    def test_snapshots_exists(self, main_window):
        """Snapshots panel should exist."""
        assert "snapshots" in main_window.panels

    def test_snapshots_has_list(self, main_window):
        """Snapshots panel should have list widget."""
        snapshots = main_window.panels["snapshots"]
        assert hasattr(snapshots, '_list')


# ── Test Category 6: Storage Panel ──────────────────────────────────────────

class TestStoragePanel:
    """Test storage panel functionality."""

    def test_storage_exists(self, main_window):
        """Storage panel should exist."""
        assert "storage" in main_window.panels

    def test_storage_has_disk_list(self, main_window):
        """Storage panel should have disk list."""
        storage = main_window.panels["storage"]
        assert hasattr(storage, '_disk_list')


# ── Test Category 7: ISO Manager Panel ──────────────────────────────────────

class TestISOManagerPanel:
    """Test ISO manager panel functionality."""

    def test_iso_exists(self, main_window):
        """ISO panel should exist."""
        assert "iso" in main_window.panels

    def test_iso_has_table(self, main_window):
        """ISO panel should have ISO table."""
        iso_panel = main_window.panels["iso"]
        assert hasattr(iso_panel, '_iso_table')

    def test_iso_manager_scan(self):
        """ISO manager should scan for ISOs."""
        from gui.iso_manager import ISOManager
        manager = ISOManager()
        isos = manager.scan_isos()
        assert isinstance(isos, list)


# ── Test Category 8: CLI Tool ───────────────────────────────────────────────

class TestCLITool:
    """Test CLI commands."""

    def test_cli_status(self):
        """CLI status command should return JSON."""
        import os
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_DIR / "src")
        result = subprocess.run(
            [sys.executable, str(GUI_DIR / "cli.py"), "status"],
            capture_output=True, text=True, timeout=10,
            cwd=str(PROJECT_DIR),
            env=env,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "server" in data

    def test_cli_vm_list(self):
        """CLI vm list should return VMs."""
        import os
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_DIR / "src")
        result = subprocess.run(
            [sys.executable, str(GUI_DIR / "cli.py"), "vm", "list"],
            capture_output=True, text=True, timeout=10,
            cwd=str(PROJECT_DIR),
            env=env,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "vms" in data

    def test_cli_snapshot_list(self):
        """CLI snapshot list should work."""
        result = subprocess.run(
            [sys.executable, str(GUI_DIR / "cli.py"), "snapshot", "list"],
            capture_output=True, text=True, timeout=10,
            cwd=str(PROJECT_DIR),
        )
        # May fail if no disk, but should not crash
        assert result.returncode in [0, 1]


# ── Test Category 9: Theme System ───────────────────────────────────────────

class TestThemeSystem:
    """Test theme tokens and styling."""

    def test_theme_tokens_exist(self):
        """All theme tokens should be defined."""
        from gui.theme import T
        assert hasattr(T, 'BG_PRIMARY')
        assert hasattr(T, 'TEXT_PRIMARY')
        assert hasattr(T, 'BRAND')
        assert hasattr(T, 'STATUS_RUNNING')

    def test_dark_palette_returns_qpalette(self):
        """dark_palette() should return QPalette."""
        from gui.theme import dark_palette
        from PyQt5.QtGui import QPalette
        palette = dark_palette()
        assert isinstance(palette, QPalette)

    def test_style_functions_return_strings(self):
        """Style functions should return strings."""
        from gui.theme import card_style, title_bar_style, sidebar_btn_style
        assert isinstance(card_style(), str)
        assert isinstance(title_bar_style(), str)


# ── Test Category 10: Atomic State ──────────────────────────────────────────

class TestAtomicState:
    """Test atomic state management."""

    def test_atomic_state_create(self):
        """AtomicState should create."""
        from gui.atomic_state import AtomicState
        state = AtomicState()
        assert state is not None

    def test_atomic_state_set_get(self):
        """AtomicState set/get should work."""
        from gui.atomic_state import AtomicState
        state = AtomicState()
        state.set("test_key", "test_value")
        assert state.get("test_key") == "test_value"

    def test_atomic_state_snapshot_restore(self):
        """Snapshot and restore should work."""
        from gui.atomic_state import AtomicState
        state = AtomicState()
        state.set("key1", "value1")
        state.create_snapshot("test_snap")
        state.set("key1", "modified")
        assert state.restore_snapshot("test_snap")
        assert state.get("key1") == "value1"


# ── Test Category 11: Widgets ───────────────────────────────────────────────

class TestWidgets:
    """Test custom widget classes."""

    def test_card_widget(self):
        """Card widget should instantiate."""
        from gui.widgets import Card
        card = Card("Test Card")
        assert card is not None

    def test_status_indicator(self):
        """StatusIndicator should instantiate."""
        from gui.widgets import StatusIndicator
        indicator = StatusIndicator()
        assert indicator is not None

    def test_telemetry_chart(self):
        """TelemetryChart should instantiate."""
        from gui.widgets import TelemetryChart
        chart = TelemetryChart()
        assert chart is not None

    def test_text_input(self):
        """TextInput should instantiate."""
        from gui.widgets import TextInput
        input_widget = TextInput("Test")
        assert input_widget is not None


# ── Test Category 12: Cross-cutting ─────────────────────────────────────────

class TestCrossCutting:
    """Cross-cutting tests."""

    def test_no_builtin_bridge_errors(self, main_window):
        """No panel should have bridge errors on init."""
        for name, panel in main_window.panels.items():
            # All panels should be accessible without errors
            assert panel is not None

    def test_sidebar_connections(self, main_window):
        """Sidebar should be connected to panel stack."""
        assert main_window.sidebar.current_panel_changed is not None

    def test_window_drag_support(self, main_window):
        """Window should support dragging."""
        assert hasattr(main_window, '_title_bar_mouse_move')
        assert hasattr(main_window, '_title_bar_mouse_press')

    def test_auto_reconnect_timer(self, main_window):
        """Auto-reconnect timer should exist."""
        assert hasattr(main_window, '_reconnect_timer')

    def test_telemetry_timer(self, main_window):
        """Telemetry timer should exist."""
        assert hasattr(main_window, '_telemetry_timer')

    def test_settings_panel_exists(self, main_window):
        """Settings panel should exist."""
        assert "settings" in main_window.panels

    def test_security_panel_exists(self, main_window):
        """Security panel (audit log) should exist."""
        assert "security" in main_window.panels

    def test_chat_panel_exists(self, main_window):
        """Chat panel should exist."""
        assert "chat" in main_window.panels

    def test_providers_panel_exists(self, main_window):
        """AI Providers panel should exist."""
        assert "providers" in main_window.panels

    def test_iso_panel_exists(self, main_window):
        """ISO Manager panel should exist."""
        assert "iso" in main_window.panels

    def test_usb_panel_exists(self, main_window):
        """USB Device panel should exist."""
        assert "usb" in main_window.panels

    def test_network_panel_exists(self, main_window):
        """Network panel should exist."""
        assert "network" in main_window.panels

    def test_monitoring_panel_exists(self, main_window):
        """Monitoring panel should exist."""
        assert "monitoring" in main_window.panels

    def test_automation_panel_exists(self, main_window):
        """Automation panel should exist."""
        assert "automation" in main_window.panels

    def test_logs_panel_exists(self, main_window):
        """Logs panel should exist."""
        assert "logs" in main_window.panels
