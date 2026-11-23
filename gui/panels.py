"""Dashboard panel — VM status overview and quick actions.

Shows a comprehensive status card with VM state, resource usage,
quick action buttons, and recent activity log.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gui.theme import T
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QListWidget, QListWidgetItem,
)

from gui.widgets import Card, StatusIndicator
from gui.qmp_extractor import QMPExtractor

if TYPE_CHECKING:
    from gui.multi_vm import MultiVMManager

log = logging.getLogger("vmharness.gui.dashboard")


class _VMActionWorker(QThread):
    completed = pyqtSignal(str, str, bool, str)

    def __init__(
        self,
        action: str,
        vm_name: str,
        manager: MultiVMManager,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.action = action
        self.vm_name = vm_name
        self.manager = manager

    def run(self) -> None:
        try:
            if self.action == "start":
                ok, message = self.manager.start_vm(self.vm_name)
            elif self.action == "stop":
                ok, message = self.manager.stop_vm(self.vm_name)
            else:
                raise ValueError(f"unsupported VM action: {self.action}")
        except Exception as exc:
            log.exception("Dashboard VM %s failed for %s", self.action, self.vm_name)
            self.completed.emit(self.action, self.vm_name, False, str(exc))
        else:
            self.completed.emit(self.action, self.vm_name, ok, message)


class DashboardPanel(QWidget):
    """Main dashboard — VM status overview and quick actions."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._qmp_bridge = None
        self._vm_manager: MultiVMManager | None = None
        self._active_vm_name: str | None = None
        self._vm_action_worker: _VMActionWorker | None = None
        self._vm_state = "unknown"
        self._vm_running = False
        self._vm_pid = "—"
        self._vm_ram = "—"
        self._vm_cpus = "—"
        self._vm_disk_used = "—"
        self._vm_disk_total = "—"
        self._vm_uptime = "0:00:00"
        self.setStyleSheet("background: " + T.BG_PRIMARY + ";")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # ── VM Status Card ─────────────────────────────────────────────────────
        status_card = Card("Virtual Machine Status", self)
        status_card.setFixedHeight(180)
        layout.addWidget(status_card)

        status_row = QWidget()
        status_row_layout = QHBoxLayout(status_row)
        status_row_layout.setContentsMargins(0, 0, 0, 0)
        status_row_layout.setSpacing(12)

        self.status_dot = StatusIndicator()
        self.status_dot.set_status(False)
        status_row_layout.addWidget(self.status_dot, alignment=Qt.AlignVCenter)

        state_layout = QVBoxLayout()
        state_layout.setSpacing(2)
        state_label = QLabel("Stopped")
        state_label.setStyleSheet("color: " + T.TEXT_SECONDARY + "; font-size: 14px; font-weight: 600;")
        state_label.setFixedHeight(20)
        state_layout.addWidget(state_label)
        self._state_label = state_label

        vm_name_label = QLabel("No VM selected")
        vm_name_label.setStyleSheet("color: " + T.TEXT_MUTED + "; font-size: 12px;")
        state_layout.addWidget(vm_name_label)
        self._vm_name_label = vm_name_label
        state_layout.addStretch()

        status_row_layout.addLayout(state_layout)
        status_row_layout.addStretch()

        stats_row = QWidget()
        stats_row_layout = QHBoxLayout(stats_row)
        stats_row_layout.setContentsMargins(0, 0, 0, 0)
        stats_row_layout.setSpacing(16)

        self._stat_labels: dict[str, QLabel] = {}

        for label_text, value, color in [
            ("PID", "—", T.TEXT_MUTED),
            ("RAM", "8.0 GB", T.CHART_CPU),
            ("vCPUs", "4", T.TEXT_ACCENT),
            ("Disk", "6.2 GB", T.STATUS_RUNNING),
            ("Uptime", "0:00:00", T.STATUS_PAUSED),
        ]:
            stat = QWidget()
            sl = QHBoxLayout(stat)
            sl.setContentsMargins(0, 0, 0, 0)
            sl.setSpacing(4)
            lbl = QLabel(label_text)
            lbl.setStyleSheet("color: " + T.TEXT_MUTED + "; font-size: 11px;")
            val = QLabel(value)
            val.setStyleSheet("color: " + color + "; font-size: 13px; font-weight: 600;")
            sl.addWidget(lbl)
            sl.addWidget(val)
            stats_row_layout.addWidget(stat)
            self._stat_labels[label_text] = val

        stats_row_layout.addStretch()
        status_card.add_widget(status_row)
        status_card.add_widget(stats_row)
        status_card.content_layout.addStretch()

        # ── Quick Actions ──────────────────────────────────────────────────────
        actions_card = Card("Quick Actions")
        layout.addWidget(actions_card)

        actions_row = QWidget()
        actions_row_layout = QHBoxLayout(actions_row)
        actions_row_layout.setContentsMargins(0, 0, 0, 0)
        actions_row_layout.setSpacing(12)

        self.start_btn = QPushButton("▶  Start VM")
        self.start_btn.setFixedSize(140, 40)
        self.start_btn.setStyleSheet(
            "QPushButton { background: " + T.STATUS_RUNNING + "; border: none; border-radius: 8px;"
            " color: white; font-size: 13px; font-weight: 600; }"
            "QPushButton:hover { background: #16a34a; }"
            "QPushButton:disabled { background: #22c55e20; color: " + T.TEXT_MUTED + "; }"
        )
        self.start_btn.clicked.connect(self._on_start_vm)
        actions_row_layout.addWidget(self.start_btn)

        self.stop_btn = QPushButton("■  Stop VM")
        self.stop_btn.setFixedSize(140, 40)
        self.stop_btn.setStyleSheet(
            "QPushButton { background: " + T.STATUS_STOPPED + "; border: none; border-radius: 8px;"
            " color: white; font-size: 13px; font-weight: 600; }"
            "QPushButton:hover { background: #dc2626; }"
            "QPushButton:disabled { background: #ef444420; color: " + T.TEXT_MUTED + "; }"
        )
        self.stop_btn.clicked.connect(self._on_stop_vm)
        actions_row_layout.addWidget(self.stop_btn)

        self.reset_btn = QPushButton("↻  Reset VM")
        self.reset_btn.setFixedSize(140, 40)
        self.reset_btn.setStyleSheet(
            "QPushButton { background: " + T.STATUS_PAUSED + "; border: none; border-radius: 8px;"
            " color: white; font-size: 13px; font-weight: 600; }"
            "QPushButton:hover { background: #d97706; }"
            "QPushButton:disabled { background: #f59e0b20; color: " + T.TEXT_MUTED + "; }"
        )
        self.reset_btn.clicked.connect(self._on_reset_vm)
        actions_row_layout.addWidget(self.reset_btn)
        actions_row_layout.addStretch()
        actions_card.add_widget(actions_row)

        # ── Recent Activity ─────────────────────────────────────────────────────
        activity_card = Card("Recent Activity")
        layout.addWidget(activity_card)

        self.activity_list = QListWidget()
        self.activity_list.setStyleSheet(
            "QListWidget { background: " + T.BG_PRIMARY + "; color: " + T.TEXT_PRIMARY + ";"
            " border: none; font-size: 12px; padding: 4px; }"
            "QListWidget::item { padding: 4px 8px; border-bottom: 1px solid " + T.BG_TERTIARY + "; }"
            "QListWidget::item:selected { background: " + T.BG_SECONDARY + "; }"
        )
        self.activity_list.setMaximumHeight(150)
        activity_card.add_widget(self.activity_list)

        layout.addStretch()

    def set_qmp_bridge(self, bridge: Any) -> None:
        """Connect to the QMP bridge."""
        self._qmp_bridge = bridge
        if bridge:
            bridge.vm_status.connect(self._on_vm_status)
            bridge.connected.connect(self._on_connected)
            bridge.error.connect(self._on_error)

    def set_vm_manager(self, manager: MultiVMManager) -> None:
        """Use the window's shared VM manager for lifecycle actions."""
        self._vm_manager = manager

    def set_active_vm(self, vm_name: str | None) -> None:
        """Set the selected VM used by dashboard lifecycle actions."""
        self._active_vm_name = vm_name
        self._vm_name_label.setText(vm_name or "No VM selected")

    def _on_vm_status(self, status: dict):
        """Update dashboard with live VM status."""
        if not status:
            return
        if "return" in status:
            status = status["return"]
        if "status" in status and isinstance(status["status"], dict):
            status = status["status"]
        running = status.get("running", False) if isinstance(status, dict) else False
        self._vm_running = running
        self.status_dot.set_status(running)

        state_text = "Running" if running else "Paused" if (isinstance(status, dict) and status.get("status") == "paused") else "Stopped"
        self._state_label.setText(state_text)
        self._state_label.setStyleSheet(
            "color: " + (T.STATUS_RUNNING if running else T.STATUS_PAUSED) + ";"
            " font-size: 14px; font-weight: 600;"
        )

        if running:
            self.start_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            self.reset_btn.setEnabled(True)
        else:
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.reset_btn.setEnabled(False)

        # Try to get PID from QEMU process
        try:
            import subprocess
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq qemu-system-x86_64.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and "qemu-system-x86_64.exe" in result.stdout:
                lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
                if lines:
                    parts = lines[0].split(",")
                    if len(parts) >= 2:
                        pid = parts[1].strip('"')
                        self._vm_pid = pid
                        self._stat_labels["PID"].setText(pid)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass  # tasklist failed — VM info unavailable

    def _on_connected(self, connected: bool):
        """Handle QMP connect/disconnect."""
        if not connected:
            self.status_dot.set_status(False)
            self._state_label.setText("Disconnected")

    def _on_error(self, message: str):
        """Handle QMP errors."""
        pass

    def _on_start_vm(self):
        """Start the selected VM through the authenticated hub."""
        self._run_vm_action("start")

    def _on_stop_vm(self):
        """Gracefully stop the selected VM through the authenticated hub."""
        self._run_vm_action("stop")

    def _run_vm_action(self, action: str) -> None:
        if not self._active_vm_name:
            self.add_activity(f"VM {action} failed: select a VM in VM Switcher first")
            return
        if self._vm_manager is None:
            self.add_activity(f"VM {action} failed: VM manager is unavailable")
            return
        if self._vm_action_worker is not None and self._vm_action_worker.isRunning():
            self.add_activity("VM action already in progress")
            return

        vm_name = self._active_vm_name
        self.add_activity(f"VM {action} requested: {vm_name}")
        worker = _VMActionWorker(action, vm_name, self._vm_manager, self)
        worker.completed.connect(self._on_vm_action_completed)
        worker.finished.connect(self._on_vm_action_worker_finished)
        self._vm_action_worker = worker
        worker.start()

    def _on_vm_action_completed(self, action: str, vm_name: str, ok: bool, message: str) -> None:
        if ok:
            self.add_activity(message or f"VM {action}: {vm_name}")
        else:
            self.add_activity(f"VM {action} failed for {vm_name}: {message}")

    def _on_vm_action_worker_finished(self) -> None:
        worker = self._vm_action_worker
        self._vm_action_worker = None
        if worker is not None:
            worker.deleteLater()

    def _on_reset_vm(self):
        """Reset VM via QMP bridge (requires running VM)."""
        if self._qmp_bridge:
            self._qmp_bridge.system_reset()
            self.add_activity("VM reset requested")

    def add_activity(self, message: str) -> None:
        """Add an activity log entry."""
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        item = QListWidgetItem(f"[{ts}] {message}")
        self.activity_list.insertItem(0, item)
        if self.activity_list.count() > 20:
            self.activity_list.takeItem(self.activity_list.count() - 1)
