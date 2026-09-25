"""Main Window for VM-Harness GUI.

Frameless window with custom title bar, sidebar navigation,
panel content area, and status bar.  Provides the overall
application layout and panel switching.
"""

from __future__ import annotations

import sys
import os
import json
import pathlib
from pathlib import Path

from PyQt5.QtCore import Qt, QSize, QTimer, QRect, QPoint, pyqtSignal
from PyQt5.QtGui import QIcon, QPalette, QColor, QPainter, QFont, QPixmap, QBitmap, QPainterPath
from PyQt5.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QFrame,
    QStackedWidget,
    QStatusBar,
    QSpacerItem,
    QSizePolicy,
    QMessageBox,
    QSystemTrayIcon,
    QMenu,
    QAction,
)

# Ensure project root and src are on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from vm_harness.config import VmMCPSettings, Secrets
from vm_harness.qmp_client import QMPClient
from vm_harness.setup import start_vm, stop_vm
from gui.widgets import (
    StatusIndicator,
    IconButton,
    Card,
    TextInput,
    PasswordInput,
    TerminalOutput,
    TelemetryChart,
    LogEntry,
    CredentialTreeItem,
    FileTree,
    title_bar_style,
    button_ghost_style,
    button_red_style,
)
from gui.theme import T
from gui.credential_store import CredentialStore
from gui.plugin_manager import PluginManager


# ── Title Bar ───────────────────────────────────────────────────────────────────

class TitleBar(QWidget):
    """Themed frameless window title bar with status indicator."""

    window_state_changed = pyqtSignal(int)

    def __init__(self, parent: QMainWindow):
        super().__init__(parent)
        self._parent = parent
        self.setMinimumHeight(36)
        self.setMaximumHeight(48)
        self.setStyleSheet(title_bar_style())
        layout = QHBoxLayout(self)
        layout.setContentsMargins(T.SM, 0, T.SM, 0)
        layout.setSpacing(T.SM)

        # Brand icon
        icon_label = QLabel()
        icon_label.setMinimumSize(24, 24)
        icon_label.setMaximumSize(36, 36)
        icon_label.setStyleSheet(
            "background: " + T.BRAND + ";"
            "border-radius: " + str(T.R_SM) + "px;"
        )
        icon_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(icon_label)

        # App name
        title_label = QLabel("VM-Harness")
        title_label.setStyleSheet(
            "color: " + T.TEXT_PRIMARY + ";"
            "font-size: " + str(T.FS_LG) + "px; "
            "font-weight: bold;"
        )
        title_label.setMinimumHeight(20)
        title_label.setMaximumHeight(32)
        layout.addWidget(title_label)

        layout.addStretch()

        # Status indicator (themed)
        self.status_dot = StatusIndicator()
        layout.addWidget(self.status_dot)

        self._status_text = QLabel("Disconnected")
        self._status_text.setStyleSheet(
            "color: " + T.TEXT_MUTED + ";"
            "font-size: " + str(T.FS_SM) + "px;"
        )
        layout.addWidget(self._status_text)

        layout.addStretch()

        # Window controls
        self._btn_min = QPushButton("\u2014")
        self._btn_min.setMinimumSize(36, 24)
        self._btn_min.setMaximumSize(48, 32)
        self._btn_min.setCursor(Qt.PointingHandCursor)
        self._btn_min.setStyleSheet(button_ghost_style())
        self._btn_min.clicked.connect(lambda: self._parent.showMinimized())
        layout.addWidget(self._btn_min)

        self._btn_max = QPushButton("\u25A1")
        self._btn_max.setMinimumSize(36, 24)
        self._btn_max.setMaximumSize(48, 32)
        self._btn_max.setCursor(Qt.PointingHandCursor)
        self._btn_max.setStyleSheet(button_ghost_style())
        self._btn_max.clicked.connect(self._toggle_maximize)
        layout.addWidget(self._btn_max)

        self._btn_close = QPushButton("\u2715")
        self._btn_close.setMinimumSize(36, 24)
        self._btn_close.setMaximumSize(48, 32)
        self._btn_close.setCursor(Qt.PointingHandCursor)
        self._btn_close.setStyleSheet(button_red_style())
        self._btn_close.clicked.connect(self._parent.close)
        layout.addWidget(self._btn_close)

    def _toggle_maximize(self):
        if self._parent.isMaximized():
            self._parent.showNormal()
            self._btn_max.setText("□")
        else:
            self._parent.showMaximized()
            self._btn_max.setText("❐")

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self._parent.move(self._parent.pos() + event.globalPos() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPos() - self._parent.frameGeometry().topLeft()
        super().mousePressEvent(event)


# ── Sidebar ─────────────────────────────────────────────────────────────────────

class Sidebar(QWidget):
    """Navigation sidebar with icon buttons."""

    current_panel_changed = pyqtSignal(str)

    PANELS = [
        ("Dashboard", "📊", "dashboard"),
        ("VM Switcher", "🔄", "vm_switcher"),
        ("VM Control", "🖥️", "vm_control"),
        ("Guest Terminal", "💻", "guest_terminal"),
        ("Guest Agent", "🤖", "guest_agent"),
        ("Telemetry", "📈", "telemetry"),
        ("QMP System Info", "📋", "sysinfo"),
        ("QMP Console", "🔧", "qmp_console"),
        ("Snapshots", "📸", "snapshots"),
        ("ISO Manager", "💿", "iso"),
        ("Create VM", "➕", "wizard"),
        ("Storage", "💾", "storage"),
        ("CPU/Memory", "🧠", "cpu"),
        ("Display", "🖥️", "display"),
        ("Advanced QEMU", "⚡", "qemu"),
        ("USB/Devices", "🔌", "usb"),
        ("Network", "🌐", "network"),
        ("Monitoring", "📊", "monitoring"),
        ("Containers", "🐳", "containers"),
        ("Container Terminal", "⌨️", "container_terminal"),
        ("Container Stats", "📈", "container_stats"),
        ("K8s Editor", "☸️", "k8s_editor"),
        ("K8s Tree", "🌳", "k8s_tree"),
        ("VM Console", "🖥️", "vm_console"),
        ("VMware/VBox", "🖥️", "vmware_vbox"),
        ("Settings", "⚙️", "settings"),
        ("Security", "🔒", "security"),
        ("Automation", "🤖", "automation"),
        ("Troubleshoot", "🔍", "troubleshoot"),
        ("AI Chat", "💬", "chat"),
        ("AI Providers", "🤖", "providers"),
        ("Pairing", "🔗", "pairing"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current = "dashboard"
        self.setMinimumWidth(160)
        self.setMaximumWidth(280)
        self.setStyleSheet("""
            Sidebar {
                background: #0f172a;
                border-right: 1px solid #334155;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        for label, icon, name in self.PANELS:
            btn = QPushButton(f"{icon}  {label}")
            btn.setObjectName(f"sidebar_{name}")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setCheckable(True)
            btn.setMinimumHeight(36)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    color: #94a3b8;
                    border: none;
                    border-radius: 6px;
                    font-size: 13px;
                    padding: 0 8px;
                }}
                QPushButton:hover {{
                    background: #1e293b;
                    color: #e2e8f0;
                }}
                QPushButton:checked {{
                    background: #1e3a5f;
                    color: #60a5fa;
                }}
                #{btn.objectName()}:hover {{
                    background: #1e293b;
                    color: #e2e8f0;
                }}
            """)
            btn.clicked.connect(lambda checked, n=name: self._select_panel(n))
            layout.addWidget(btn)

        layout.addStretch()

        # Version label
        ver_label = QLabel("v2.0.0")
        ver_label.setStyleSheet("color: #475569; font-size: 10px; text-align: center;")
        ver_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(ver_label)

    def _select_panel(self, name: str):
        self._current = name
        for label, icon, panel_name in self.PANELS:
            btn = self.findChild(QPushButton, f"sidebar_{panel_name}")
            if btn:
                btn.setChecked(panel_name == name)
        self.current_panel_changed.emit(name)

    def add_panel_button(self, label: str, icon: str, name: str):
        """Dynamically add a sidebar button for a plugin panel."""
        self.PANELS.append((label, icon, name))

        # Find the layout — it's the first QVBoxLayout child
        layout = self.layout()
        if layout is None:
            return

        # Insert before the stretch and version label
        insert_index = layout.count() - 1  # before the version label
        if insert_index < 0:
            insert_index = layout.count()

        btn = QPushButton(f"{icon}  {label}")
        btn.setObjectName(f"sidebar_{name}")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setCheckable(True)
        btn.setMinimumHeight(36)
        btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: #94a3b8;
                border: none;
                border-radius: 6px;
                font-size: 13px;
                padding: 0 8px;
            }}
            QPushButton:hover {{
                background: #1e293b;
                color: #e2e8f0;
            }}
            QPushButton:checked {{
                background: #1e3a5f;
                color: #60a5fa;
            }}
            #{btn.objectName()}:hover {{
                background: #1e293b;
                color: #e2e8f0;
            }}
        """)
        btn.clicked.connect(lambda checked, n=name: self._select_panel(n))
        layout.insertWidget(insert_index, btn)


# ── Main Window ─────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    """VM-Harness main application window."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("VM-Harness")
        self.setMinimumSize(1024, 640)
        self.resize(1400, 900)
        # Set native resize behavior for frameless window
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint
            | Qt.WindowCloseButtonHint
            | Qt.CustomizeWindowHint
        )
        # Allow native resize from OS-level (drag via grips + native borders)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WA_NoSystemBackground, False)
        self.setMouseTracking(True)
        self.setStyleSheet("""
            QMainWindow {
                background: #0f172a;
            }
        """)

        # ── State ──────────────────────────────────────────────────────────────
        self.settings = VmMCPSettings()
        self.secrets = Secrets.from_env()
        self.secrets_dotenv = Secrets.from_dotenv()
        if self.secrets_dotenv.has_any_secret():
            self.secrets = self.secrets_dotenv

        self.qmp_client: QMPClient | None = None
        self.credential_store = CredentialStore()

        self._drag_pos: QPoint = QPoint()

        # ── System Tray ──────────────────────────────────────────────────────
        self._setup_system_tray()

        # ── Central widget ─────────────────────────────────────────────────────
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Title bar
        self.title_bar = TitleBar(self)
        main_layout.addWidget(self.title_bar)

        # Main content area
        content = QWidget()
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        # Sidebar
        self.sidebar = Sidebar(self)
        content_layout.addWidget(self.sidebar)

        # Panel stack
        self.panel_stack = QStackedWidget(self)
        self.panel_stack.setStyleSheet("""
            QStackedWidget {
                background: #0f172a;
            }
        """)
        content_layout.addWidget(self.panel_stack)
        content_layout.setStretchFactor(self.panel_stack, 1)

        main_layout.addWidget(content)

        # Status bar
        self.status_bar = QStatusBar(self)
        self.status_bar.setStyleSheet("""
            QStatusBar {
                background: #0f172a;
                color: #64748b;
                border-top: 1px solid #334155;
                font-size: 12px;
            }
        """)
        self.status_label = QLabel("Ready")
        self.status_bar.addPermanentWidget(self.status_label)
        main_layout.addWidget(self.status_bar)

        # ── QMP Bridge (background async → PyQt5 signals) ─────────────────────
        from gui.qmp_bridge import QMPBridge
        self.qmp_bridge = QMPBridge(settings=self.settings)
        self.qmp_bridge.start()
        self.qmp_bridge.connected.connect(self._on_qmp_connected)
        self.qmp_bridge.vm_status.connect(self._on_vm_status)
        self.qmp_bridge.error.connect(self._on_qmp_error)
        self.qmp_bridge.command_result.connect(self._on_qmp_command)
        self.qmp_client = None  # legacy compat

        # ── SSH Bridge ────────────────────────────────────────────────────────
        from gui.ssh_bridge import SSHBridge
        self.ssh_bridge = SSHBridge(settings=self.settings)
        self.ssh_bridge.start()
        self.ssh_bridge.connected.connect(self._on_ssh_connected)
        self.ssh_bridge.command_output.connect(self._on_ssh_command_output)
        self.ssh_bridge.file_content.connect(self._on_ssh_file_content)
        self.ssh_bridge.file_list.connect(self._on_ssh_file_list)
        self.ssh_bridge.error.connect(self._on_ssh_error)
        self.ssh_bridge.connected_to.connect(self._on_ssh_connected_to)

        # ── Plugin Manager ────────────────────────────────────────────────────
        self.plugin_manager = PluginManager()

        # ── Build panels ───────────────────────────────────────────────────────
        self._build_panels()

        # ── API server for pairing ─────────────────────────────────────────────
        self._api_server = None
        self._init_api_server()

        # ── Wire pairing panel to API server ──────────────────────────────────
        if "pairing" in self.panels and self._api_server:
            self.panels["pairing"].set_server(self._api_server)

        # ── Sidebar connection ─────────────────────────────────────────────────
        self.sidebar.current_panel_changed.connect(self._switch_panel)

        # ── Window drag support ────────────────────────────────────────────────
        self.title_bar.mouseMoveEvent = self._title_bar_mouse_move
        self.title_bar.mousePressEvent = self._title_bar_mouse_press

        # ── Telemetry timer ────────────────────────────────────────────────────
        self._telemetry_timer = QTimer(self)
        # Don't connect — telemetry is handled by TelemetryPanel's own timer
        self._telemetry_timer.start(2000)

        # ── Auto-reconnect timer (polls until QMP/SSH come back) ───────────────
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.timeout.connect(self._try_auto_reconnect)
        self._reconnect_timer.start(5000)

        # ── Build panels ───────────────────────────────────────────────────────
        self._update_status_indicators()

        # ── DPI scaling ────────────────────────────────────────────────────────
        self._apply_dpi_scaling()

        # ── Resize grips ───────────────────────────────────────────────────────
        self._setup_resize_grips()

    def _init_api_server(self):
        """Initialize API server for pairing token generation."""
        try:
            from pathlib import Path
            from vm_harness.api_server import QMCMApiServer, _load_or_generate_signing_key

            # Signing key is at PROJECT_ROOT/.vmharness_signing_key
            key_dir = Path(".")
            signing_key = _load_or_generate_signing_key(key_dir)
            self._api_server = QMCMApiServer(
                host="0.0.0.0",
                port=8443,
                tailscale_only=False,
                signing_key_dir=key_dir,
            )
        except Exception as e:
            # Non-fatal: pairing panel will show error
            import logging
            logging.getLogger("vmharness.gui").warning(f"API server init failed: {e}")

    # ── System Tray ──────────────────────────────────────────────────────────

    def _setup_system_tray(self):
        """Set up system tray icon. Fails gracefully in headless/offscreen environments."""
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                import logging
                logging.getLogger("vmharness.gui").info("System tray not available — skipping tray icon")
                return
            self._setup_system_tray_impl()
        except Exception as e:
            import logging
            logging.getLogger("vmharness.gui").warning("System tray init failed: %s — continuing without tray", e)

    def _setup_system_tray_impl(self):
        """Create system tray icon and context menu."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon = None
            return

        # Create tray icon
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setToolTip("VM-Harness — Running")

        # Create icon programmatically (purple circle with "V")
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor("#7c3aed"))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(4, 4, 56, 56)
        painter.setPen(QColor("#ffffff"))
        painter.setFont(QFont("Arial", 28, QFont.Bold))
        painter.drawText(pixmap.rect(), Qt.AlignCenter, "V")
        painter.end()
        self.tray_icon.setIcon(QIcon(pixmap))

        # Create context menu
        tray_menu = QMenu()
        action_restore = QAction("Show VM-Harness", self)
        action_restore.triggered.connect(self._restore_from_tray)
        tray_menu.addAction(action_restore)
        tray_menu.addSeparator()
        action_quit = QAction("Quit", self)
        action_quit.triggered.connect(self._quit_from_tray)
        tray_menu.addAction(action_quit)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._tray_activated)
        self.tray_icon.show()

    def _restore_from_tray(self):
        """Restore window from system tray."""
        self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def _tray_activated(self, reason):
        """Handle tray icon activation — only restore on double-click."""
        # Explicitly ignore all activation reasons except DoubleClick
        # On Windows, setContextMenu() can cause single-click to emit Trigger
        if reason == QSystemTrayIcon.DoubleClick:
            self._restore_from_tray()
        # All other reasons (Trigger, MiddleClick, Context, Unknown) → ignore
        # Do NOT call any action for single-click

    def _quit_from_tray(self):
        """Quit from tray — properly clean up."""
        self._save_state()
        if self.tray_icon:
            self.tray_icon.hide()
        # Stop timers
        if hasattr(self, '_telemetry_timer'):
            self._telemetry_timer.stop()
        if hasattr(self, '_reconnect_timer'):
            self._reconnect_timer.stop()
        QApplication.quit()

    def closeEvent(self, event):
        """Clean up on close — minimize to tray."""
        self._save_state()
        if hasattr(self, 'plugin_manager'):
            self.plugin_manager.unload_all()
        if hasattr(self, '_grips'):
            for grip in self._grips:
                grip.releaseMouse()
                grip.deleteLater()
            self._grips.clear()
        # Minimize to tray
        if self.tray_icon and self.tray_icon.isVisible():
            event.ignore()
            self.hide()
            self.tray_icon.showMessage(
                "VM-Harness",
                "Minimized to tray. Click tray icon to restore.",
                QSystemTrayIcon.Information,
                3000,
            )
        else:
            event.accept()

    def _title_bar_mouse_press(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()

    def _title_bar_mouse_move(self, event):
        if event.buttons() & Qt.LeftButton:
            self.move(self.pos() + event.globalPos() - self._drag_pos)

    def _build_panels(self):
        """Create all panels and add them to the stacked widget."""
        from gui.panels_vm_switcher import VMSwitcherPanel
        from gui.panels import DashboardPanel
        from gui.panels_vm_control import VMControlPanel
        from gui.panels_guest_terminal import GuestTerminalPanel
        from gui.panels_guest_agent import GuestAgentPanel
        from gui.panels_telemetry import TelemetryPanel
        from gui.panels_qmp_console import QMPConsolePanel
        from gui.panels_sysinfo import QemuSystemInfoPanel
        from gui.panels_snapshots import SnapshotPanel
        from gui.panels_iso import ISOManagerPanel
        from gui.panels_wizard import VMCreationWizard
        from gui.panels_storage import StoragePanel
        from gui.panels_usb import USBDevicePanel
        from gui.panels_network import NetworkPanel
        from gui.panels_monitoring import MonitoringPanel
        from gui.panels_cpu import CPUControlPanel
        from gui.panels_display import DisplayPanel
        from gui.panels_qemu import AdvancedQEmuPanel
        from gui.panels_automation import AutomationPanel
        from gui.panels_troubleshoot import TroubleshootPanel
        from gui.panels_chat import ChatPanel
        from gui.panels_providers import AIProvidersPanel
        from gui.panels_settings import SettingsPanel
        from gui.panels_pairing import PairingPanel
        from gui.panels_security import SecurityPanel
        from gui.panels_logs import LogsPanel
        from gui.panels_container import ContainerPanel
        from gui.panels_container_terminal import ContainerTerminalPanel
        from gui.panels_container_stats import ContainerStatsPanel
        from gui.panels_k8s_editor import KubernetesEditorPanel
        from gui.panels_k8s_tree import KubernetesTreePanel
        from gui.panels_vm_console import VMConsolePanel
        from gui.vmware_vbox_panel import VMwareVBoxPanel

        self.panels: dict[str, QWidget] = {}

        panel_list = [
            (DashboardPanel, "dashboard"),
            (VMSwitcherPanel, "vm_switcher"),
            (VMControlPanel, "vm_control"),
            (GuestTerminalPanel, "guest_terminal"),
            (GuestAgentPanel, "guest_agent"),
            (TelemetryPanel, "telemetry"),
            (QMPConsolePanel, "qmp_console"),
            (QemuSystemInfoPanel, "sysinfo"),
            (SnapshotPanel, "snapshots"),
            (ISOManagerPanel, "iso"),
            (VMCreationWizard, "wizard"),
            (StoragePanel, "storage"),
            (USBDevicePanel, "usb"),
            (NetworkPanel, "network"),
            (CPUControlPanel, "cpu"),
            (DisplayPanel, "display"),
            (AdvancedQEmuPanel, "qemu"),
            (AutomationPanel, "automation"),
            (TroubleshootPanel, "troubleshoot"),
            (ChatPanel, "chat"),
            (AIProvidersPanel, "providers"),
            (MonitoringPanel, "monitoring"),
            (SettingsPanel, "settings"),
            (PairingPanel, "pairing"),
            (SecurityPanel, "security"),
            (LogsPanel, "logs"),
            (ContainerPanel, "containers"),
            (ContainerTerminalPanel, "container_terminal"),
            (ContainerStatsPanel, "container_stats"),
            (KubernetesEditorPanel, "k8s_editor"),
            (KubernetesTreePanel, "k8s_tree"),
            (VMConsolePanel, "vm_console"),
            (VMwareVBoxPanel, "vmware_vbox"),
        ]

        for panel_cls, name in panel_list:
            panel = panel_cls(self)
            self.panels[name] = panel
            self.panel_stack.addWidget(panel)

        # ── Wire bridges to panels ────────────────────────────────────────────
        if "dashboard" in self.panels:
            self.panels["dashboard"].set_qmp_bridge(self.qmp_bridge)
        if "vm_control" in self.panels:
            self.panels["vm_control"].set_multi_qmp_bridge(self.qmp_bridge)
        if "guest_terminal" in self.panels:
            self.panels["guest_terminal"].set_ssh_bridge(self.ssh_bridge)
        if "guest_agent" in self.panels:
            self.panels["guest_agent"].set_qmp_bridge(self.qmp_bridge)
            self.panels["guest_agent"].set_ssh_bridge(self.ssh_bridge)
        if "telemetry" in self.panels:
            self.panels["telemetry"].set_qmp_bridge(self.qmp_bridge)
            self.panels["telemetry"].set_ssh_bridge(self.ssh_bridge)
        if "qmp_console" in self.panels:
            self.panels["qmp_console"].set_qmp_bridge(self.qmp_bridge)

        # ── Load plugin panels ───────────────────────────────────────────────
        self._load_plugin_panels()

        self._switch_panel("dashboard")

    def _switch_panel(self, name: str):
        if name in self.panels:
            self.panel_stack.setCurrentWidget(self.panels[name])
            self.status_label.setText(f"Panel: {name.replace('_', ' ').title()}")

    def _load_plugin_panels(self):
        """Discover and load plugin panels from the plugin manager."""
        from gui.plugin import PanelPlugin

        # Discover available plugins
        discovered = self.plugin_manager.discover_plugins()
        if not discovered:
            return

        # Load all discovered plugins
        self.plugin_manager.load_all()

        # Create panels for each PanelPlugin
        for plugin in self.plugin_manager.get_panels():
            meta = plugin.metadata
            panel_name = meta.name

            # Skip if a panel with this name already exists
            if panel_name in self.panels:
                continue

            try:
                panel_widget = plugin.create_panel(self)
                self.panels[panel_name] = panel_widget
                self.panel_stack.addWidget(panel_widget)

                # Add to sidebar
                label = meta.description or panel_name.replace("_", " ").title()
                icon = "🔌"
                self.sidebar.add_panel_button(label, icon, panel_name)

                # Wire bridges if the panel supports them
                if hasattr(panel_widget, "set_qmp_bridge"):
                    panel_widget.set_qmp_bridge(self.qmp_bridge)
                if hasattr(panel_widget, "set_ssh_bridge"):
                    panel_widget.set_ssh_bridge(self.ssh_bridge)
                if hasattr(panel_widget, "set_multi_qmp_bridge"):
                    panel_widget.set_multi_qmp_bridge(self.qmp_bridge)

            except Exception as e:
                import logging
                logging.getLogger("vmharness.gui").warning(
                    f"Failed to create panel for plugin '{panel_name}': {e}"
                )

    def _apply_dpi_scaling(self):
        """Apply DPI-aware scaling to all child widgets."""
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QApplication

        app = QApplication.instance()
        if not app:
            return

        # Get primary screen DPI
        screen = app.primaryScreen()
        if not screen:
            return

        dpi = screen.logicalDotsPerInch()
        scale = dpi / 96.0  # 96 DPI is the reference

        # Clamp to reasonable range
        scale = max(0.8, min(scale, 2.0))

        # Apply to all child widgets
        for widget in self.findChildren(QWidget):
            # Scale font sizes
            font = widget.font()
            base_size = font.pointSizeF()
            if base_size > 0:
                font.setPointSizeF(base_size * scale)
                widget.setFont(font)

            # Scale icons
            if hasattr(widget, 'iconSize') and hasattr(widget, 'setIconSize'):
                current = widget.iconSize()
                if hasattr(current, 'width') and hasattr(current, 'height'):
                    from PyQt5.QtCore import QSize
                    widget.setIconSize(QSize(int(current.width() * scale), int(current.height() * scale)))

    def resizeEvent(self, event):
        """Handle window resize events."""
        super().resizeEvent(event)
        self._apply_dpi_scaling()
        self._update_grips()

    def _update_grips(self):
        """Reposition resize grips when window is resized."""
        if not hasattr(self, '_grips') or len(self._grips) < 8:
            return
        w, h = self.width(), self.height()
        g = 20  # grip size
        # Order: top-left, top-right, bottom-left, bottom-right,
        #        top-center, bottom-center, left-center, right-center
        positions = [
            (0, 0),
            (w - g, 0),
            (0, h - g),
            (w - g, h - g),
            (w // 2 - g // 2, 0),
            (w // 2 - g // 2, h - g),
            (0, h // 2 - g // 2),
            (w - g, h // 2 - g // 2),
        ]
        for grip, (x, y) in zip(self._grips[:8], positions):
            grip.setGeometry(x, y, g, g)
            grip.raise_()  # Keep grips above content

    def _setup_resize_grips(self):
        """Add invisible resize grip widgets at corners and edges."""
        self._grips = []
        grip_size = 20
        edges = [
            (0, 0, grip_size, grip_size),                          # top-left
            (self.width() - grip_size, 0, grip_size, grip_size),   # top-right
            (0, self.height() - grip_size, grip_size, grip_size),   # bottom-left
            (self.width() - grip_size, self.height() - grip_size, grip_size, grip_size),  # bottom-right
            (self.width() // 2 - grip_size // 2, 0, grip_size, grip_size),   # top-center
            (self.width() // 2 - grip_size // 2, self.height() - grip_size, grip_size, grip_size),  # bottom-center
            (0, self.height() // 2 - grip_size // 2, grip_size, grip_size),   # left-center
            (self.width() - grip_size, self.height() // 2 - grip_size // 2, grip_size, grip_size),  # right-center
        ]
        cursors = [
            Qt.SizeFDiagCursor, Qt.SizeFDiagCursor,
            Qt.SizeFDiagCursor, Qt.SizeFDiagCursor,
            Qt.SizeVerCursor, Qt.SizeVerCursor,
            Qt.SizeHorCursor, Qt.SizeHorCursor,
        ]
        for i, (x, y, w, h) in enumerate(edges):
            grip = QWidget(self)
            grip.setGeometry(x, y, w, h)
            grip.setStyleSheet("background: transparent;")
            grip.setCursor(cursors[i])
            grip.mousePressEvent = lambda e, g=grip, idx=i: self._grip_press(e, g, idx)
            grip.mouseMoveEvent = lambda e, g=grip, idx=i: self._grip_move(e, g, idx)
            grip.mouseReleaseEvent = lambda e, g=grip: self._grip_release(e, g)
            self._grips.append(grip)

    def _grip_press(self, event, grip, edge_idx):
        if event.button() == Qt.LeftButton:
            self._grip_start_pos = event.globalPos()
            self._grip_start_geometry = self.geometry()
            self._grip_edge_idx = edge_idx
            grip.grabMouse()

    def _grip_move(self, event, grip, edge_idx):
        if not hasattr(self, '_grip_start_pos') or self._grip_edge_idx != edge_idx:
            return
        delta = event.globalPos() - self._grip_start_pos
        geom = self._grip_start_geometry

        new_x, new_y, new_w, new_h = geom.x(), geom.y(), geom.width(), geom.height()

        # Right edge: adjust width
        if edge_idx in (1, 7):
            new_w = max(self.minimumWidth(), geom.width() + delta.x())
        # Left edge: adjust x and width
        if edge_idx in (0, 6):
            new_w = max(self.minimumWidth(), geom.width() - delta.x())
            new_x = geom.x() + delta.x()
        # Bottom edge: adjust height
        if edge_idx in (3, 5):
            new_h = max(self.minimumHeight(), geom.height() + delta.y())
        # Top edge: adjust y and height
        if edge_idx in (0, 4):
            new_h = max(self.minimumHeight(), geom.height() - delta.y())
            new_y = geom.y() + delta.y()

        self.setGeometry(new_x, new_y, new_w, new_h)

    def _grip_release(self, event, grip):
        self._grip_start_pos = None
        self._grip_start_geometry = None
        self._grip_edge_idx = None
        grip.releaseMouse()

    def _update_status_indicators(self):
        """Update the title bar status dot and text."""
        if self.qmp_bridge and self.qmp_bridge.is_connected:
            self.title_bar.status_dot.set_status(running=True, connected=True)
        elif self.ssh_bridge and self.ssh_bridge.is_connected:
            self.title_bar.status_dot.set_status(running=False, connected=True)
        else:
            self.title_bar.status_dot.set_status(running=False, connected=False)

    # ── QMP Bridge Callbacks ──────────────────────────────────────────────────

    def _on_qmp_connected(self, connected: bool):
        self._update_status_indicators()
        if connected:
            self.status_label.setText("Connected to QMP")
            self.status_label.setStyleSheet("color: #22c55e; font-size: 12px;")
            # Refresh status immediately
            self.qmp_bridge.get_status()
        else:
            self.status_label.setText("QMP connection failed")
            self.status_label.setStyleSheet("color: #ef4444; font-size: 12px;")

    def _on_vm_status(self, status: dict):
        """Update dashboard with VM status from QMP."""
        panel = self.panels.get("dashboard")
        if panel and hasattr(panel, "update_vm_status"):
            panel.update_vm_status(status)

    def _on_qmp_error(self, message: str):
        self.status_label.setText(f"QMP: {message}")
        self.status_label.setStyleSheet("color: #ef4444; font-size: 12px;")

    def _on_qmp_command(self, result: dict):
        """Handle QMP command result."""
        self.status_label.setText(f"QMP command: {result.get('return', result.get('error', 'ok'))}")
        self.status_label.setStyleSheet("color: #38bdf8; font-size: 12px;")

    # ── SSH Bridge Callbacks ─────────────────────────────────────────────────

    def _on_ssh_connected(self, connected: bool):
        if connected:
            self.status_label.setText("Connected to guest SSH")
            self.status_label.setStyleSheet("color: #22c55e; font-size: 12px;")
        else:
            self.status_label.setText("SSH connection failed")
            self.status_label.setStyleSheet("color: #ef4444; font-size: 12px;")

    def _on_ssh_connected_to(self, address: str):
        self.status_label.setText(f"SSH connected to {address}")
        self.status_label.setStyleSheet("color: #22c55e; font-size: 12px;")

    def _on_ssh_command_output(self, output: str):
        """Send command output to guest terminal."""
        panel = self.panels.get("guest_terminal")
        if panel and hasattr(panel, "append_command_output"):
            panel.append_command_output(output)

    def _on_ssh_file_content(self, content: str):
        """Send file content to guest terminal."""
        panel = self.panels.get("guest_terminal")
        if panel and hasattr(panel, "append_file_content"):
            panel.append_file_content(content)

    def _on_ssh_file_list(self, files: list):
        """Send file listing to guest terminal."""
        panel = self.panels.get("guest_terminal")
        if panel and hasattr(panel, "populate_files"):
            panel.populate_files(files)

    def _on_ssh_error(self, message: str):
        panel = self.panels.get("guest_terminal")
        if panel and hasattr(panel, "append_error"):
            panel.append_error(message)

    def _try_auto_reconnect(self):
        """Periodic auto-reconnect: if bridges are running but not connected,
        retry connecting.  Called every 5 seconds by _reconnect_timer.
        Only reconnects if the bridge was previously connected (not on first
        start, to avoid grabbing the sole QMP connection during tests)."""
        if self.qmp_bridge and not self.qmp_bridge.is_connected:
            if self.qmp_bridge._ever_connected:
                self.qmp_bridge.connect()
        if self.ssh_bridge and not self.ssh_bridge.is_connected:
            self.ssh_bridge.connect_ssh()

    def _save_state(self):
        """Persist window geometry and panel selection for next launch."""
        try:
            state_dir = pathlib.Path.home() / ".local" / "share" / "vmharness"
            state_dir.mkdir(parents=True, exist_ok=True)
            state_file = state_dir / "window_state.json"
            active_name = None
            for name, panel in self.panels.items():
                if self.panel_stack.currentWidget() is panel:
                    active_name = name
                    break
            state = {
                "geometry": self.saveGeometry().data().hex(),
                "window_state": self.saveState().data().hex(),
                "active_panel": active_name,
            }
            state_file.write_text(json.dumps(state), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass  # Never let state save crash the app — best-effort persistence

    # closeEvent is defined above in _setup_system_tray section (minimizes to tray)


# ── Application Entry ───────────────────────────────────────────────────────────

def main():
    """Run the VM-Harness GUI application."""
    from PyQt5.QtWidgets import QApplication
    from gui.theme import dark_palette

    app = QApplication(sys.argv)
    app.setApplicationName("VM-Harness")
    app.setApplicationVersion("2.0.0")
    app.setOrganizationName("VM-Harness")

    # Apply global dark theme
    app.setStyle("Fusion")
    app.setPalette(dark_palette())

    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
