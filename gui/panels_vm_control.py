"""VM Control Panel — full VM lifecycle management with multi-VM QMP bridge integration.

Provides Start, Stop, Reset, Suspend, Resume, Eject ISO controls
wired to the MultiVMQMPBridge for context-aware VM operations.
Switches context based on the currently selected VM.
"""

from __future__ import annotations

from gui.theme import T
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QComboBox,
    QGridLayout,
    QProgressBar,
    QMessageBox,
    QSpinBox,
    QCheckBox,
    QSizePolicy,
    QFrame,
)

from gui.widgets import Card, StatusIndicator, SectionHeader, StatCard
from gui.multi_vm_qmp_bridge import MultiVMQMPBridge
from gui.multi_vm import MultiVMManager


class VMControlPanel(QWidget):
    """Full VM lifecycle control panel with multi-VM QMP integration.

    Context switches based on the selected VM — displays VM-specific config
    and routes QMP commands to the correct VM bridge.
    """

    vm_action_requested = pyqtSignal(str, str)  # action, vm_name

    def __init__(self, parent=None):
        super().__init__(parent)
        self._multi_qmp: MultiVMQMPBridge | None = None
        self._manager: MultiVMManager | None = None
        self._active_vm: str | None = None
        self._bridge_error: str = ""
        self.setStyleSheet("background: " + T.BG_PRIMARY + ";")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignTop)

        # ── Active VM Context Header ───────────────────────────────────────────
        context_card = Card("Active VM Context")
        context_card.setFixedHeight(60)
        layout.addWidget(context_card)

        context_row = QWidget()
        cr_layout = QHBoxLayout(context_row)
        cr_layout.setContentsMargins(0, 0, 0, 0)
        cr_layout.setSpacing(8)

        self._context_dot = StatusIndicator(QColor(T.TEXT_MUTED))
        cr_layout.addWidget(self._context_dot, alignment=Qt.AlignVCenter)

        self._context_label = QLabel("No VM selected")
        self._context_label.setStyleSheet(
            f"color: {T.TEXT_MUTED}; font-size: 12px;"
        )
        cr_layout.addWidget(self._context_label)
        cr_layout.addStretch()

        self._context_vm_label = QLabel("")
        self._context_vm_label.setStyleSheet(
            f"color: {T.BRAND}; font-weight: bold; font-size: 12px;"
        )
        cr_layout.addWidget(self._context_vm_label)

        context_card.content_layout.addWidget(context_row)

        # ── Hardware Acceleration ─────────────────────────────────────────────────
        accel_card = Card("Hardware Acceleration")
        layout.addWidget(accel_card)

        accel_row = QWidget()
        accel_row_layout = QHBoxLayout(accel_row)
        accel_row_layout.setContentsMargins(0, 0, 0, 0)
        accel_row_layout.setSpacing(12)

        self._accel_check = QCheckBox("Enable WHPX Acceleration")
        self._accel_check.setChecked(True)
        self._accel_check.setStyleSheet(
            f"QCheckBox {{ color: {T.TEXT_PRIMARY}; font-size: 12px; }}"
            f"QCheckBox::indicator:checked {{ background: {T.ACCENT}; border-color: {T.ACCENT}; }}"
        )
        self._accel_check.toggled.connect(self._on_accel_toggled)
        accel_row_layout.addWidget(self._accel_check)

        self._accel_status = StatusIndicator(QColor(T.SUCCESS))
        self._accel_status.setFixedSize(10, 10)
        accel_row_layout.addWidget(self._accel_status, alignment=Qt.AlignVCenter)

        self._accel_status_label = QLabel("WHPX Active")
        self._accel_status_label.setStyleSheet(f"color: {T.SUCCESS}; font-size: 11px;")
        accel_row_layout.addWidget(self._accel_status_label)

        accel_row_layout.addStretch()
        accel_card.content_layout.addWidget(accel_row)

        # Accel warning label (shown when disabled)
        self._accel_warning = QLabel("")
        self._accel_warning.setStyleSheet(
            f"color: {T.WARNING}; font-size: 11px; background: {T.WARNING_BG};"
            f" padding: 6px 10px; border-radius: 4px;"
        )
        self._accel_warning.setWordWrap(True)
        self._accel_warning.hide()
        accel_card.content_layout.addWidget(self._accel_warning)

        # ── QMP Connection ──────────────────────────────────────────────────────────
        conn_card = Card("QMP Connection")
        layout.addWidget(conn_card)

        conn_row = QWidget()
        conn_row_layout = QHBoxLayout(conn_row)
        conn_row_layout.setContentsMargins(0, 0, 0, 0)
        conn_row_layout.setSpacing(12)

        self.qmp_status = StatusIndicator(QColor(T.TEXT_MUTED))
        conn_row_layout.addWidget(self.qmp_status, alignment=Qt.AlignVCenter)

        self.conn_info = QLabel("Disconnected — QMP not available")
        self.conn_info.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 12px;")
        conn_row_layout.addWidget(self.conn_info)
        conn_row_layout.addStretch()

        # Console login credentials. Sits next to the QMP connection because
        # using them needs one: keystrokes reach the guest over QMP.
        self.console_login_btn = QPushButton("Login…")
        self.console_login_btn.setFixedHeight(32)
        self.console_login_btn.setCursor(Qt.PointingHandCursor)
        self.console_login_btn.setToolTip(
            "Select a VM to store its Username and Password"
        )
        self.console_login_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.TEXT_SECONDARY};"
            f" border: 1px solid {T.BG_TERTIARY}; border-radius: 4px;"
            f" font-size: 12px; padding: 0 16px; }}"
            f"QPushButton:hover {{ color: {T.TEXT_PRIMARY}; }}"
        )
        conn_row_layout.addWidget(self.console_login_btn)

        self.connect_btn = QPushButton("Connect to QMP")
        self.connect_btn.setFixedHeight(32)
        self.connect_btn.setStyleSheet(
            f"QPushButton {{ background: {T.BRAND}; color: white; border: none;"
            f" border-radius: 4px; font-size: 12px; padding: 0 16px; }}"
            f"QPushButton:hover {{ background: {T.BRAND_HOVER}; }}"
            f"QPushButton:disabled {{ background: {T.BRAND}20; color: {T.TEXT_MUTED}; }}"
        )
        conn_row_layout.addWidget(self.connect_btn)
        conn_card.content_layout.addWidget(conn_row)

        # ── VM Lifecycle Controls ──────────────────────────────────────────────
        life_card = Card("VM Lifecycle Control")
        layout.addWidget(life_card)

        btn_row = QWidget()
        btn_row_layout = QHBoxLayout(btn_row)
        btn_row_layout.setContentsMargins(0, 0, 0, 0)
        btn_row_layout.setSpacing(10)

        buttons_spec = [
            ("Start", "🟢", T.SUCCESS, "Start the virtual machine"),
            ("Stop", "⏹", T.ERROR, "Gracefully stop the VM"),
            ("Reset", "🔄", T.WARNING, "Reset the VM (warm reboot)"),
            ("Suspend", "⏸", T.BRAND, "Suspend the VM to disk"),
            ("Resume", "▶", T.SUCCESS, "Resume a suspended VM"),
            ("Eject ISO", "💿", T.INFO, "Eject the boot ISO"),
        ]

        self._lifecycle_btns = {}
        for label, icon, color, tooltip in buttons_spec:
            btn = QPushButton(f"{icon}  {label}")
            btn.setFixedHeight(40)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(tooltip)
            btn.setStyleSheet(
                f"QPushButton {{ background: {color}; color: white; border: none;"
                f" border-radius: 6px; font-size: 13px; font-weight: 600; padding: 0 12px; }}"
                f"QPushButton:hover {{ background: {self._darker(color)}; }}"
                f"QPushButton:disabled {{ background: {color}20; color: {T.TEXT_MUTED}; }}"
            )
            btn_row_layout.addWidget(btn)
            self._lifecycle_btns[label] = btn

        btn_row_layout.addStretch()
        life_card.content_layout.addWidget(btn_row)

        # ── VM Configuration Display (context-aware) ────────────────────────────
        config_card = Card("VM Configuration")
        layout.addWidget(config_card)

        config_grid = QGridLayout()
        config_grid.setContentsMargins(0, 0, 0, 0)
        config_grid.setSpacing(8)
        config_grid.setColumnStretch(1, 1)

        self._config_labels: dict[str, QLabel] = {}
        config_fields = [
            "VM Name", "Status", "RAM", "vCPUs", "Disk Image",
            "QMP Port", "SSH Port", "QMP URI", "SSH URI",
        ]

        for i, field_name in enumerate(config_fields):
            row = i // 3
            col = (i % 3) * 2
            lbl = QLabel(field_name)
            lbl.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 11px;")
            lbl.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            config_grid.addWidget(lbl, row, col)

            val = QLabel("—")
            val.setStyleSheet(f"color: {T.TEXT_PRIMARY}; font-size: 12px;")
            config_grid.addWidget(val, row, col + 1)
            self._config_labels[field_name] = val

        config_card.content_layout.addLayout(config_grid)

        # ── Progress ───────────────────────────────────────────────────────────
        self.progress = QProgressBar()
        self.progress.setFixedHeight(4)
        self.progress.setStyleSheet(
            f"QProgressBar {{ background: {T.BG_SECONDARY}; border: none; text-align: center; }}"
            f"QProgressBar::chunk {{ background: {T.BRAND}; border-radius: 2px; }}"
        )
        self.progress.setMaximum(0)
        self.progress.hide()
        layout.addWidget(self.progress)

        # ── Info log ───────────────────────────────────────────────────────────
        self.info_label = QLabel("Select a VM to control it.")
        self.info_label.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 12px;")
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        # ── Wire buttons ───────────────────────────────────────────────────────
        self.connect_btn.clicked.connect(self._on_connect)
        self.console_login_btn.clicked.connect(self._on_console_login)
        self._lifecycle_btns["Start"].clicked.connect(self._on_start)
        self._lifecycle_btns["Stop"].clicked.connect(self._on_stop)
        self._lifecycle_btns["Reset"].clicked.connect(self._on_reset)
        self._lifecycle_btns["Suspend"].clicked.connect(self._on_suspend)
        self._lifecycle_btns["Resume"].clicked.connect(self._on_resume)
        self._lifecycle_btns["Eject ISO"].clicked.connect(self._on_eject)

        # ── Status timer ───────────────────────────────────────────────────────
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._pulse_connection)
        self._pulse_timer.start(5000)

    def _on_console_login(self) -> None:
        """Open the dialog that stores this VM's Username and Password.

        Passes the VM explicitly. The dialog used to be parented to the panel
        and asked for a generic "Console username" without saying which machine
        it was for, and stored one global pair shared by every VM.
        """
        from gui.dialogs_vm_login import VMLoginCredentialsDialog

        if not self._active_vm:
            QMessageBox.information(
                self, "No VM Selected",
                "Select a VM in the VM Switcher first — these credentials are "
                "stored per VM.",
            )
            return

        config = self._manager.get_vm(self._active_vm) if self._manager else None
        dialog = VMLoginCredentialsDialog(
            vm_name=self._active_vm,
            config=config,
            status=self._manager.get_status(self._active_vm) if self._manager else "",
            qmp_uri=(self._manager.get_qmp_uri(self._active_vm) or "") if self._manager else "",
            ssh_uri=(self._manager.get_ssh_uri(self._active_vm) or "") if self._manager else "",
            parent=self,
        )
        if dialog.exec_():
            self._refresh_credential_summary()

    def _refresh_credential_summary(self) -> None:
        """Keep the button caption telling the user whose login it manages."""
        from gui.dialogs_vm_login import vm_login_status

        if not hasattr(self, "console_login_btn"):
            return
        if not self._active_vm:
            self.console_login_btn.setText("Login…")
            self.console_login_btn.setToolTip("Select a VM to store its login credentials")
            return
        self.console_login_btn.setText(f"Login: {self._active_vm}")
        self.console_login_btn.setToolTip(vm_login_status(self._active_vm))
        if hasattr(self, "info_label"):
            self.info_label.setText(
                f"{self._active_vm}: {vm_login_status(self._active_vm)}"
            )

    def set_multi_qmp_bridge(self, bridge: MultiVMQMPBridge) -> None:
        """Set the multi-VM QMP bridge."""
        self._multi_qmp = bridge
        bridge.connected.connect(self._on_bridge_connected)
        bridge.error.connect(self._on_bridge_error)
        bridge.active_vm_changed.connect(self._on_active_vm_changed)
        bridge.command_result.connect(self._on_command_result)

    def set_manager(self, manager: MultiVMManager) -> None:
        """Set the MultiVMManager for config access."""
        self._manager = manager

    def switch_to_vm(self, vm_name: str) -> None:
        """Switch the control panel context to a specific VM."""
        self._active_vm = vm_name

        # Build the bridge for this VM from the shared manager. MultiVMQMPBridge
        # is constructed per VM (it takes a name and a QMP URI), so there is no
        # single instance to hand around -- previously the panel was given a
        # single-VM QMPBridge that had none of the methods called here.
        self._rebuild_bridge(vm_name)

        self._update_context_display()
        self._update_config_display()
        self._update_button_states()
        self._refresh_credential_summary()

    def _rebuild_bridge(self, vm_name: str) -> None:
        from gui.multi_vm_qmp_bridge import MultiVMQMPBridge

        if self._multi_qmp:
            try:
                self._multi_qmp.stop()
            except Exception:  # noqa: BLE001 - a stale bridge must not block switching
                pass
            self._multi_qmp = None

        if not self._manager:
            return
        uri = self._manager.get_qmp_uri(vm_name)
        if not uri:
            return
        try:
            self._multi_qmp = MultiVMQMPBridge(vm_name, uri)
        except Exception as exc:  # noqa: BLE001
            self._bridge_error = str(exc)

    def _update_context_display(self):
        """Update the context header with current VM info."""
        if not self._active_vm:
            self._context_label.setText("No VM selected")
            self._context_label.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 12px;")
            self._context_vm_label.setText("")
            self._context_dot.set_status(False, False)
            return

        self._context_label.setText("Controlling:")
        self._context_label.setStyleSheet(f"color: {T.TEXT_SECONDARY}; font-size: 12px;")
        self._context_vm_label.setText(self._active_vm)

        if self._multi_qmp and self._multi_qmp.active_vm == self._active_vm:
            bridge = self._multi_qmp.get_bridge(self._active_vm)
            if bridge and bridge.is_connected:
                self._context_dot.set_status(True, True)
            else:
                self._context_dot.set_status(False, False)

    def _update_config_display(self):
        """Update the config display panel with current VM's settings."""
        if not self._active_vm or not self._manager:
            for label in self._config_labels.values():
                label.setText("—")
            return

        config = self._manager.get_vm(self._active_vm)
        if not config:
            return

        status = self._manager.get_status(self._active_vm)

        self._config_labels["VM Name"].setText(config.vm_name)
        self._config_labels["Status"].setText(status)
        self._config_labels["Status"].setStyleSheet(
            f"color: {'#22c55e' if status == 'running' else '#f59e0b' if status == 'paused' else '#64748b'}; font-size: 12px;"
        )
        self._config_labels["RAM"].setText(f"{config.ram_mb} MB")
        self._config_labels["vCPUs"].setText(str(config.cpus))
        self._config_labels["Disk Image"].setText(
            config.disk_path if config.disk_path else "—"
        )
        self._config_labels["QMP Port"].setText(str(config.qmp_port))
        self._config_labels["SSH Port"].setText(str(config.ssh_port))
        self._config_labels["QMP URI"].setText(self._manager.get_qmp_uri(self._active_vm) or "—")
        self._config_labels["SSH URI"].setText(self._manager.get_ssh_uri(self._active_vm) or "—")

    def _update_button_states(self):
        """Enable/disable buttons based on VM status."""
        if not self._active_vm or not self._manager:
            for btn in self._lifecycle_btns.values():
                btn.setEnabled(False)
            return

        status = self._manager.get_status(self._active_vm)
        is_running = status == "running"
        is_paused = status == "paused"
        is_stopped = status == "stopped"

        self._lifecycle_btns["Start"].setEnabled(is_stopped or is_paused)
        self._lifecycle_btns["Stop"].setEnabled(is_running or is_paused)
        self._lifecycle_btns["Reset"].setEnabled(is_running)
        self._lifecycle_btns["Suspend"].setEnabled(is_running)
        self._lifecycle_btns["Resume"].setEnabled(is_paused)
        self._lifecycle_btns["Eject ISO"].setEnabled(is_running)

    # ── QMP Command Handlers ──────────────────────────────────────────────────

    def _on_connect(self):
        """Connect to the active VM's QMP."""
        if not self._multi_qmp:
            self._show_info("QMP bridge not initialized", success=False)
            return
        if not self._active_vm:
            self._show_info("No VM selected", success=False)
            return
        self.connect_btn.setEnabled(False)
        self.connect_btn.setText("Connecting...")
        self.conn_info.setText("Connecting to QMP...")
        self.conn_info.setStyleSheet(f"color: {T.WARNING}; font-size: 12px;")
        self._multi_qmp.connect()

    def _on_start(self):
        """Start the active VM."""
        if self._manager and self._active_vm:
            success, msg = self._manager.start_vm(self._active_vm)
            self._show_info(msg, success)
            self.vm_action_requested.emit("start", self._active_vm)

    def _on_stop(self):
        """Stop the active VM."""
        if self._multi_qmp:
            self._multi_qmp.system_powerdown()
            self._show_info("Stopping VM...", success=True)

    def _on_reset(self):
        """Reset the active VM."""
        if self._multi_qmp:
            self._multi_qmp.system_reset()
            self._show_info("Resetting VM...", success=True)

    def _on_suspend(self):
        """Suspend the active VM."""
        if self._multi_qmp:
            self._multi_qmp.stop_vm()
            self._show_info("Suspending VM...", success=True)

    def _on_resume(self):
        """Resume the active VM."""
        if self._multi_qmp:
            self._multi_qmp.cont()
            self._show_info("Resuming VM...", success=True)

    def _on_eject(self):
        """Eject CD-ROM on the active VM."""
        if self._multi_qmp:
            self._multi_qmp.eject_cdrom()
            self._show_info("Ejecting CD-ROM...", success=True)

    # ── Bridge Callbacks ─────────────────────────────────────────────────────

    def _on_bridge_connected(self, vm_name: str, connected: bool):
        """Handle bridge connection state change."""
        if vm_name != self._active_vm:
            return
        if connected:
            self.qmp_status.set_status(running=True, connected=True)
            self.connect_btn.setEnabled(False)
            self.connect_btn.setText("Connected")
            self.connect_btn.setStyleSheet(
                f"QPushButton {{ background: {T.SUCCESS}; color: white; border: none;"
                f" border-radius: 4px; font-size: 12px; padding: 0 16px; }}"
            )
            self.conn_info.setText(f"Connected to QMP — {vm_name}")
            self.conn_info.setStyleSheet(f"color: {T.SUCCESS}; font-size: 12px;")
            self._context_dot.set_status(True, True)
        else:
            self.qmp_status.set_status(running=False, connected=False)
            self.connect_btn.setEnabled(True)
            self.connect_btn.setText("Connect to QMP")
            self.conn_info.setText("Disconnected — QMP not available")
            self.conn_info.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 12px;")
            self._context_dot.set_status(False, False)

    def _on_bridge_error(self, vm_name: str, message: str):
        """Handle bridge error."""
        if vm_name == self._active_vm:
            self.info_label.setText(f"QMP: {message}")
            self.info_label.setStyleSheet(f"color: {T.ERROR}; font-size: 12px;")

    def _on_active_vm_changed(self, vm_name: str):
        """Handle active VM change in the bridge."""
        if vm_name == self._active_vm:
            self._update_context_display()

    def _on_command_result(self, vm_name: str, result: dict):
        """Handle QMP command result."""
        if vm_name == self._active_vm:
            ret = result.get("return", "ok")
            self.info_label.setText(f"Result: {ret}")
            self.info_label.setStyleSheet(f"color: {T.INFO}; font-size: 12px;")

    def _show_info(self, message: str, success: bool = True):
        """Display an info message."""
        color = T.SUCCESS if success else T.ERROR
        self.info_label.setText(message)
        self.info_label.setStyleSheet(f"color: {color}; font-size: 12px;")

    def _on_accel_toggled(self, enabled: bool):
        """Handle acceleration toggle — update status display and warning."""
        if enabled:
            self._accel_status.set_status(True, False)
            self._accel_status_label.setText("WHPX Active")
            self._accel_status_label.setStyleSheet(f"color: {T.SUCCESS}; font-size: 11px;")
            self._accel_warning.hide()
        else:
            self._accel_status.set_status(False, False)
            self._accel_status_label.setText("TCG (Software) — Slow")
            self._accel_status_label.setStyleSheet(f"color: {T.WARNING}; font-size: 11px;")
            self._accel_warning.setText(
                "⚠️ Hardware acceleration disabled — VM will run in TCG (software) mode, "
                "which is significantly slower and not recommended for production workloads."
            )
            self._accel_warning.show()

    def _pulse_connection(self):
        """Keep connection indicator alive."""
        pass

    @staticmethod
    def _darker(hex_color: str) -> str:
        """Darken a hex color by ~15%."""
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        r = max(0, int(r * 0.85))
        g = max(0, int(g * 0.85))
        b = max(0, int(b * 0.85))
        return f"#{r:02x}{g:02x}{b:02x}"
