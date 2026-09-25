"""GUI smoke tests — verify all panels, widgets, credential store, and bridges construct.

These are constructor-level smoke tests: they verify the GUI can be assembled
without runtime errors.  They do NOT require a display (headless) or a running
QEMU instance.

Run:  pytest tests/test_gui.py -v
"""

from __future__ import annotations

import os
import sys
import datetime

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("GUI_MASTER_PASSWORD", "test-master-password")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def app(qtbot):
    """Create a QApplication for GUI tests."""
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ── Widget construction tests ─────────────────────────────────────────────────


class TestWidgets:
    """Verify each reusable widget constructs without error."""

    def test_status_indicator(self, qtbot, app):
        from gui.widgets import StatusIndicator
        from PyQt5.QtGui import QColor

        dot = StatusIndicator(QColor("#22c55e"))
        assert dot is not None
        qtbot.addWidget(dot)

    def test_card(self, qtbot, app):
        from gui.widgets import Card

        card = Card("Test Card Title")
        assert card is not None
        assert card.title_label.text() == "Test Card Title"
        qtbot.addWidget(card)

    def test_telemetry_chart(self, qtbot, app):
        from gui.widgets import TelemetryChart

        chart = TelemetryChart("Test Metric", "Usage %")
        assert chart is not None
        qtbot.addWidget(chart)

    def test_terminal_output(self, qtbot, app):
        from gui.widgets import TerminalOutput

        term = TerminalOutput()
        assert term is not None
        qtbot.addWidget(term)

    def test_file_tree(self, qtbot, app):
        from gui.widgets import FileTree

        tree = FileTree("/")
        assert tree is not None
        qtbot.addWidget(tree)


# ── Panel construction tests ──────────────────────────────────────────────────


class TestPanels:
    """Verify each panel constructs without error (no QMP/SSH needed)."""

    def test_dashboard_panel(self, qtbot, app):
        from gui.panels import DashboardPanel

        panel = DashboardPanel()
        assert panel is not None
        assert panel.status_dot is not None
        assert panel.activity_list is not None
        qtbot.addWidget(panel)

    def test_vm_control_panel(self, qtbot, app):
        from gui.panels_vm_control import VMControlPanel

        panel = VMControlPanel()
        assert panel is not None
        assert panel.connect_btn is not None
        assert "Start" in panel._lifecycle_btns
        assert panel._lifecycle_btns["Start"] is not None
        qtbot.addWidget(panel)

    def test_guest_terminal_panel(self, qtbot, app):
        from gui.panels_guest_terminal import GuestTerminalPanel

        panel = GuestTerminalPanel()
        assert panel is not None
        assert panel.terminal is not None
        assert panel.cmd_input is not None
        assert panel.file_tree is not None
        qtbot.addWidget(panel)

    def test_telemetry_panel(self, qtbot, app):
        from gui.panels_telemetry import TelemetryPanel

        panel = TelemetryPanel()
        assert panel is not None
        assert panel.cpu_chart is not None
        assert panel.ram_chart is not None
        assert panel.disk_chart is not None
        assert panel.net_chart is not None
        assert panel.host_cpu is not None
        assert panel.host_ram is not None
        assert panel.host_disk is not None
        qtbot.addWidget(panel)

    def test_settings_panel(self, qtbot, app, monkeypatch, tmp_path):
        from gui.panels_settings import SettingsPanel

        # Set up env so SettingsPanel can load .env
        store_dir = tmp_path / "qcmcp"
        store_dir.mkdir()
        cred_path = store_dir / "credentials.json"
        master_key_path = store_dir / ".master_key"
        monkeypatch.setenv("GUI_STORE_PATH", str(cred_path))
        monkeypatch.setenv("GUI_MASTER_KEY_PATH", str(master_key_path))
        monkeypatch.setenv("GUI_MASTER_PASSWORD", "test-secret-123")

        panel = SettingsPanel()
        assert panel is not None
        assert panel.tabs is not None
        assert panel.tabs.count() >= 6  # QEMU, VM, Display, Network, Logging, Auth
        qtbot.addWidget(panel)

    def test_security_panel(self, qtbot, app, monkeypatch, tmp_path):
        from gui.panels_security import SecurityPanel

        # Set up a temp credential store path so SecurityPanel doesn't fail
        store_dir = tmp_path / "qcmcp"
        store_dir.mkdir()
        cred_path = store_dir / "credentials.json"
        master_key_path = store_dir / ".master_key"
        monkeypatch.setenv("GUI_STORE_PATH", str(cred_path))
        monkeypatch.setenv("GUI_MASTER_KEY_PATH", str(master_key_path))
        monkeypatch.setenv("GUI_MASTER_PASSWORD", "test-secret-123")

        panel = SecurityPanel()
        assert panel is not None
        assert panel.cred_tree is not None
        qtbot.addWidget(panel)

    def test_logs_panel(self, qtbot, app):
        from gui.panels_logs import LogsPanel

        panel = LogsPanel()
        assert panel is not None
        assert panel.log_tabs is not None
        qtbot.addWidget(panel)


# ── Credential store tests ────────────────────────────────────────────────────


@pytest.fixture
def store_fixture(tmp_path, monkeypatch):
    """Create an isolated credential store for each test."""
    from gui.credential_store import CredentialStore

    store_dir = tmp_path / "qcmcp"
    store_dir.mkdir(parents=True, exist_ok=True)
    cred_path = store_dir / "credentials.json"
    key_path = store_dir / ".master_key"

    # Set env vars for Fernet key derivation
    monkeypatch.setenv("GUI_MASTER_PASSWORD", "test-secret-123")
    monkeypatch.setenv("GUI_MASTER_KEY_PATH", str(key_path))

    store = CredentialStore(store_path=str(cred_path))
    assert store is not None
    return store


class TestCredentialStore:
    """Verify CredentialStore CRUD operations.

    The store writes to a temp directory so tests are isolated."""

    def test_add_credential(self, store_fixture):
        """Adding a credential returns a name and persists it."""
        name = store_fixture.add(
            name="Test API Key",
            credential_type="api_key",
            value="sk-test-abc123",
            description="A test API key",
        )
        assert name == "Test API Key"
        creds = store_fixture.list_all()
        assert len(creds) == 1
        assert creds[0].name == "Test API Key"
        assert creds[0].credential_type == "api_key"

    def test_get_credential(self, store_fixture):
        """get returns the credential or None."""
        store_fixture.add(name="X", credential_type="password", value="hidden", description="")
        cred = store_fixture.get("X")
        assert cred is not None
        assert cred.value == "hidden"

    def test_get_unknown_returns_none(self, store_fixture):
        assert store_fixture.get("nonexistent") is None

    def test_update_credential(self, store_fixture):
        """update changes value and description."""
        store_fixture.add(name="U", credential_type="api_key", value="old", description="old desc")
        store_fixture.update(name="U", new_value="new-val", new_description="new desc")
        cred = store_fixture.get("U")
        assert cred.value == "new-val"
        assert cred.description == "new desc"

    def test_delete_credential(self, store_fixture):
        """delete removes the credential."""
        store_fixture.add(name="D", credential_type="password", value="x", description="")
        store_fixture.delete("D")
        assert store_fixture.get("D") is None
        assert len(store_fixture.list_all()) == 0

    def test_delete_unknown_is_noop(self, store_fixture):
        """Deleting a nonexistent name does not raise."""
        store_fixture.delete("no-such-credential")

    def test_duplicate_name_is_rejected(self, store_fixture):
        """Adding the same name twice returns the existing name without duplication."""
        store_fixture.add(name="dup", credential_type="api_key", value="v1", description="")
        result = store_fixture.add(name="dup", credential_type="api_key", value="v2", description="")
        assert result == "dup"
        creds = store_fixture.list_all()
        assert len(creds) == 1
        assert creds[0].value == "v1"  # original preserved

    def test_list_all_returns_all(self, store_fixture):
        """list_all returns all credentials."""
        store_fixture.add(name="a", credential_type="api_key", value="1", description="")
        store_fixture.add(name="b", credential_type="password", value="2", description="")
        store_fixture.add(name="c", credential_type="token", value="3", description="")
        creds = store_fixture.list_all()
        assert len(creds) == 3
        names = [c.name for c in creds]
        assert "a" in names and "b" in names and "c" in names

    def test_search_by_name(self, store_fixture):
        """search filters by name substring (case-insensitive)."""
        store_fixture.add(name="Alpha Key", credential_type="api_key", value="a", description="")
        store_fixture.add(name="Beta Token", credential_type="token", value="b", description="")
        store_fixture.add(name="Gamma Pass", credential_type="password", value="c", description="")
        results = store_fixture.search("alpha")
        assert len(results) == 1
        assert results[0].name == "Alpha Key"

    def test_search_no_match_returns_empty(self, store_fixture):
        assert store_fixture.search("zzz-not-there") == []

    def test_clear_all(self, store_fixture):
        """clear_all removes all credentials after user confirms."""
        store_fixture.add(name="x", credential_type="api_key", value="1", description="")
        store_fixture.add(name="y", credential_type="password", value="2", description="")
        store_fixture.clear_all(confirmed=True)
        assert store_fixture.list_all() == []

    def test_clear_all_requires_confirmation(self, store_fixture):
        """clear_all without confirmation is a no-op."""
        store_fixture.add(name="x", credential_type="api_key", value="1", description="")
        store_fixture.clear_all(confirmed=False)
        assert len(store_fixture.list_all()) == 1

    def test_credential_has_all_fields(self, store_fixture):
        """Each credential record has the expected fields."""
        store_fixture.add(
            name="Full",
            credential_type="api_key",
            value="secret-value",
            description="A full credential",
        )
        cred = store_fixture.get("Full")
        assert cred.name == "Full"
        assert cred.credential_type == "api_key"
        assert cred.value == "secret-value"
        assert cred.description == "A full credential"
        assert isinstance(cred.updated, str)


# ── MainWindow construction test ──────────────────────────────────────────────


class TestMainWindow:
    """Verify MainWindow constructs with all panels and bridges."""

    def test_main_window_constructs(self, qtbot, app, monkeypatch):
        """MainWindow assembles with bridges. Uses mocked config so no real QEMU/SSH."""
        import os
        tmp = os.environ.get("TEST_TMP_DIR")
        if tmp and os.path.isdir(tmp):
            from pathlib import Path
            monkeypatch.setenv("QEMU_BINARY", str(Path(tmp) / "qemu.exe"))
            monkeypatch.setenv("VM_DISK_PATH", str(Path(tmp) / "disk.qcow2"))
            monkeypatch.setenv("SSH_HOST", "127.0.0.1")
            monkeypatch.setenv("QMP_HOST", "127.0.0.1")
            monkeypatch.setenv("QMP_PORT", "5555")
            # Override SSH password so SSHBridge authenticates with mock creds
            monkeypatch.setenv("SSH_PASSWORD", "mock-pass")

        from gui.main_window import MainWindow

        win = MainWindow()
        assert win is not None
        assert win.width() == 1400
        assert win.height() == 900
        assert len(win.panels) == 34
        assert win.qmp_bridge is not None
        assert win.ssh_bridge is not None
        qtbot.addWidget(win)


# ── Bridge construction tests ─────────────────────────────────────────────────


class TestBridges:
    """Verify QMPBridge and SSHBridge construct without a running QEMU/SSH."""

    def test_qmp_bridge_constructs(self):
        from gui.qmp_bridge import QMPBridge
        from vm_harness.config import VmMCPSettings

        settings = VmMCPSettings()
        bridge = QMPBridge(settings=settings)
        assert bridge is not None
        assert bridge._settings is settings

    def test_ssh_bridge_constructs(self):
        from gui.ssh_bridge import SSHBridge
        from vm_harness.config import VmMCPSettings

        settings = VmMCPSettings()
        bridge = SSHBridge(settings=settings)
        assert bridge is not None
        assert bridge._settings is settings


# ── Hardware Acceleration tests ──────────────────────────────────────────────


class TestHardwareAcceleration:
    """Verify Hardware Acceleration toggle, status, settings, and persistence."""

    def test_vm_control_has_accel_checkbox(self, qtbot, app):
        """VM Control panel exposes a WHPX acceleration checkbox."""
        from gui.panels_vm_control import VMControlPanel

        panel = VMControlPanel()
        assert panel is not None
        assert panel._accel_check is not None
        assert panel._accel_check.text() == "Enable WHPX Acceleration"
        # Default state: enabled (checked)
        assert panel._accel_check.isChecked() is True
        qtbot.addWidget(panel)

    def test_vm_control_accel_status_indicator(self, qtbot, app):
        """VM Control panel shows a status indicator for acceleration."""
        from gui.panels_vm_control import VMControlPanel

        panel = VMControlPanel()
        assert panel._accel_status is not None
        assert panel._accel_status_label is not None
        # Default: WHPX Active
        assert panel._accel_status_label.text() == "WHPX Active"
        qtbot.addWidget(panel)

    def test_vm_control_accel_toggle_updates_status(self, qtbot, app):
        """Toggling acceleration checkbox updates status label and indicator."""
        from gui.panels_vm_control import VMControlPanel

        panel = VMControlPanel()
        qtbot.addWidget(panel)

        # Initially enabled
        assert panel._accel_status_label.text() == "WHPX Active"

        # Disable acceleration
        panel._accel_check.setChecked(False)
        assert panel._accel_status_label.text() == "TCG (Software) — Slow"
        # Warning should be not hidden (visible property depends on parent visibility)
        assert not panel._accel_warning.isHidden()
        assert "Hardware acceleration disabled" in panel._accel_warning.text()

        # Re-enable acceleration
        panel._accel_check.setChecked(True)
        assert panel._accel_status_label.text() == "WHPX Active"
        assert panel._accel_warning.isHidden()

    def test_vm_control_accel_warning_text(self, qtbot, app):
        """Warning message mentions TCG mode and performance impact."""
        from gui.panels_vm_control import VMControlPanel

        panel = VMControlPanel()
        qtbot.addWidget(panel)

        panel._accel_check.setChecked(False)
        warning_text = panel._accel_warning.text()
        assert "TCG" in warning_text
        assert "slower" in warning_text.lower() or "significantly slower" in warning_text.lower()

    def test_settings_has_accel_mode_combo(self, qtbot, app, monkeypatch, tmp_path):
        """Settings panel has an acceleration mode combo box."""
        from gui.panels_settings import SettingsPanel

        store_dir = tmp_path / "qcmcp"
        store_dir.mkdir()
        cred_path = store_dir / "credentials.json"
        master_key_path = store_dir / ".master_key"
        monkeypatch.setenv("GUI_STORE_PATH", str(cred_path))
        monkeypatch.setenv("GUI_MASTER_KEY_PATH", str(master_key_path))
        monkeypatch.setenv("GUI_MASTER_PASSWORD", "test-secret-123")

        panel = SettingsPanel()
        assert panel.accel_mode_combo is not None
        # Verify all three modes are available
        items = [panel.accel_mode_combo.itemText(i) for i in range(panel.accel_mode_combo.count())]
        assert "whpx" in items
        assert "haxm" in items
        assert "tcg" in items
        qtbot.addWidget(panel)

    def test_settings_tcg_shows_warning(self, qtbot, app, monkeypatch, tmp_path):
        """Selecting TCG mode in settings shows a warning."""
        from gui.panels_settings import SettingsPanel

        store_dir = tmp_path / "qcmcp"
        store_dir.mkdir()
        cred_path = store_dir / "credentials.json"
        master_key_path = store_dir / ".master_key"
        monkeypatch.setenv("GUI_STORE_PATH", str(cred_path))
        monkeypatch.setenv("GUI_MASTER_KEY_PATH", str(master_key_path))
        monkeypatch.setenv("GUI_MASTER_PASSWORD", "test-secret-123")

        panel = SettingsPanel()
        qtbot.addWidget(panel)

        # Initially no warning (whpx is default)
        assert panel.accel_warning.isHidden()

        # Select TCG
        panel.accel_mode_combo.setCurrentText("tcg")
        assert not panel.accel_warning.isHidden()
        assert "software emulation" in panel.accel_warning.text().lower() or "extremely slow" in panel.accel_warning.text().lower()

        # Switch back to whpx — warning hides
        panel.accel_mode_combo.setCurrentText("whpx")
        assert panel.accel_warning.isHidden()

    def test_settings_haxm_no_warning(self, qtbot, app, monkeypatch, tmp_path):
        """Selecting HAXM mode does not show a warning."""
        from gui.panels_settings import SettingsPanel

        store_dir = tmp_path / "qcmcp"
        store_dir.mkdir()
        cred_path = store_dir / "credentials.json"
        master_key_path = store_dir / ".master_key"
        monkeypatch.setenv("GUI_STORE_PATH", str(cred_path))
        monkeypatch.setenv("GUI_MASTER_KEY_PATH", str(master_key_path))
        monkeypatch.setenv("GUI_MASTER_PASSWORD", "test-secret-123")

        panel = SettingsPanel()
        qtbot.addWidget(panel)

        panel.accel_mode_combo.setCurrentText("haxm")
        assert panel.accel_warning.isHidden()

    def test_config_acceleration_persistence(self, monkeypatch):
        """vm_acceleration setting persists through env var."""
        from vm_harness.config import VmMCPSettings

        # Set env var before creating settings
        monkeypatch.setenv("VM_ACCELERATION", "haxm")
        settings = VmMCPSettings()
        assert settings.vm_acceleration == "haxm"

    def test_config_acceleration_default(self):
        """Default acceleration mode is whpx."""
        from vm_harness.config import VmMCPSettings

        settings = VmMCPSettings()
        assert settings.vm_acceleration == "whpx"

    def test_settings_save_accel_value(self, qtbot, app, monkeypatch, tmp_path):
        """Settings panel combo reflects the selected acceleration mode."""
        from gui.panels_settings import SettingsPanel

        store_dir = tmp_path / "qcmcp"
        store_dir.mkdir()
        cred_path = store_dir / "credentials.json"
        master_key_path = store_dir / ".master_key"
        monkeypatch.setenv("GUI_STORE_PATH", str(cred_path))
        monkeypatch.setenv("GUI_MASTER_KEY_PATH", str(master_key_path))
        monkeypatch.setenv("GUI_MASTER_PASSWORD", "test-secret-123")

        panel = SettingsPanel()
        qtbot.addWidget(panel)

        # Change acceleration to haxm and verify combo reflects it
        panel.accel_mode_combo.setCurrentText("haxm")
        assert panel.accel_mode_combo.currentText() == "haxm"

        # Change to tcg and verify
        panel.accel_mode_combo.setCurrentText("tcg")
        assert panel.accel_mode_combo.currentText() == "tcg"
        # Warning text should be set (isVisible depends on parent being shown)
        assert "software emulation" in panel.accel_warning.text().lower() or "extremely slow" in panel.accel_warning.text().lower()
        assert not panel.accel_warning.isHidden()

        # Change back to whpx
        panel.accel_mode_combo.setCurrentText("whpx")
        assert panel.accel_warning.isHidden()

    def test_accel_checkbox_uses_accent_color(self, qtbot, app):
        """Accent color is applied to the checkbox indicator when checked."""
        from gui.panels_vm_control import VMControlPanel
        from gui.theme import T

        panel = VMControlPanel()
        qtbot.addWidget(panel)

        # The checkbox stylesheet should reference T.ACCENT
        stylesheet = panel._accel_check.styleSheet()
        assert T.ACCENT in stylesheet
