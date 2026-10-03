"""VMware and VirtualBox Panel — full VM lifecycle for both hypervisors.

Integrates with vm_harness.hypervisor.vmware.backend and vm_harness.hypervisor.virtualbox.backend.
Shows VMs from both providers with unified controls and provider-specific actions.
"""

from __future__ import annotations

import time

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QTabWidget, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QCheckBox, QInputDialog,
    QGroupBox, QTextBrowser,
)

from gui.theme import T
from gui.widgets import Card, StatusIndicator, SectionHeader


class VMwareVBoxPanel(QWidget):
    """VMware and VirtualBox unified management panel."""

    provider_vm_action_requested = pyqtSignal(str, str, str)  # provider, action, vm_name

    def __init__(self, parent=None):
        super().__init__(parent)
        self._vmware_backend = None
        self._vbox_backend = None
        self._current_provider = "vmware"
        self.setStyleSheet("background: " + T.BG_PRIMARY + ";")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignTop)

        # ── Backend Status ──────────────────────────────────────────────────
        status_card = Card("Hypervisor Status")
        status_card.setFixedHeight(50)
        layout.addWidget(status_card)

        status_row = QWidget()
        sr_layout = QHBoxLayout(status_row)
        sr_layout.setContentsMargins(0, 0, 0, 0)
        sr_layout.setSpacing(12)

        self._vmware_status = StatusIndicator(QColor("#ef4444"))
        sr_layout.addWidget(self._vmware_status)
        sr_layout.addWidget(QLabel("VMware"))

        self._vbox_status = StatusIndicator(QColor("#ef4444"))
        sr_layout.addWidget(self._vbox_status)
        sr_layout.addWidget(QLabel("VirtualBox"))

        sr_layout.addStretch()

        self._btn_refresh = QPushButton("Refresh")
        self._btn_refresh.setStyleSheet(
            f"background: {T.BRAND}; color: {T.TEXT_PRIMARY}; border: none;"
            "border-radius: 6px; padding: 6px 16px; font-weight: bold;"
        )
        self._btn_refresh.clicked.connect(self._refresh)
        sr_layout.addWidget(self._btn_refresh)

        status_card.content_layout.addWidget(status_row)

        # ── Tabs ────────────────────────────────────────────────────────────
        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(
            f"QTabWidget::pane {{ background: {T.BG_SECONDARY}; border: 1px solid {T.BG_TERTIARY}; border-radius: 8px; }}"
            f"QTabBar::tab {{ background: {T.BG_TERTIARY}; color: {T.TEXT_MUTED}; padding: 8px 20px; border-radius: 6px 6px 0 0; margin-right: 4px; }}"
            f"QTabBar::tab:selected {{ background: {T.BRAND}; color: {T.TEXT_PRIMARY}; }}"
        )
        layout.addWidget(self._tabs)

        self._tabs.addTab(self._build_vmware_tab(), "VMware")
        self._tabs.addTab(self._build_virtualbox_tab(), "VirtualBox")
        self._tabs.currentChanged.connect(self._on_tab_changed)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh)
        self._refresh_timer.start(15000)

    def _build_vmware_tab(self) -> QWidget:
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)
        tab_layout.setSpacing(8)

        toolbar = QWidget()
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(0, 0, 0, 0)
        tb_layout.setSpacing(8)

        self._vmware_filter = QComboBox()
        self._vmware_filter.addItems(["All", "Powered On", "Powered Off", "Suspended"])
        self._vmware_filter.currentTextChanged.connect(self._load_vmware)
        tb_layout.addWidget(self._vmware_filter)

        tb_layout.addStretch()

        self._vmware_start = QPushButton("Power On")
        self._vmware_start.clicked.connect(lambda: self._vmware_action("power_on"))
        self._vmware_stop = QPushButton("Power Off")
        self._vmware_stop.clicked.connect(lambda: self._vmware_action("power_off"))
        self._vmware_suspend = QPushButton("Suspend")
        self._vmware_suspend.clicked.connect(lambda: self._vmware_action("suspend"))
        self._vmware_reset = QPushButton("Reset")
        self._vmware_reset.clicked.connect(lambda: self._vmware_action("reset"))
        self._vmware_snapshot = QPushButton("Snapshot")
        self._vmware_snapshot.clicked.connect(lambda: self._vmware_action("snapshot"))

        for btn in [self._vmware_start, self._vmware_stop, self._vmware_suspend, self._vmware_reset, self._vmware_snapshot]:
            btn.setStyleSheet(
                f"background: {T.BG_TERTIARY}; color: {T.TEXT_PRIMARY}; border: 1px solid {T.BG_TERTIARY};"
                "border-radius: 6px; padding: 4px 12px;"
            )
            tb_layout.addWidget(btn)

        tab_layout.addWidget(toolbar)

        self._vmware_table = QTableWidget()
        self._vmware_table.setColumnCount(4)
        self._vmware_table.setHorizontalHeaderLabels(["Name", "Guest OS", "State", "IP Address"])
        self._vmware_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._vmware_table.setStyleSheet(
            f"QTableWidget {{ background: {T.BG_SECONDARY}; color: {T.TEXT_PRIMARY}; border: none; }}"
            f"QHeaderView::section {{ background: {T.BG_TERTIARY}; color: {T.TEXT_MUTED}; padding: 6px; border: none; }}"
        )
        tab_layout.addWidget(self._vmware_table)

        # Snapshot section
        snap_card = Card("VMware Snapshots")
        snap_layout = QVBoxLayout()
        snap_layout.setContentsMargins(0, 0, 0, 0)
        self._vmware_snapshot_list = QTextBrowser()
        self._vmware_snapshot_list.setStyleSheet(f"background: {T.BG_TERTIARY}; color: {T.TEXT_PRIMARY}; border: none;")
        snap_layout.addWidget(self._vmware_snapshot_list)
        snap_card.content_layout.addLayout(snap_layout)
        tab_layout.addWidget(snap_card)

        return tab

    def _build_virtualbox_tab(self) -> QWidget:
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)
        tab_layout.setSpacing(8)

        toolbar = QWidget()
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(0, 0, 0, 0)
        tb_layout.setSpacing(8)

        self._vbox_filter = QComboBox()
        self._vbox_filter.addItems(["All", "Running", "Powered Off", "Paused"])
        self._vbox_filter.currentTextChanged.connect(self._load_vbox)
        tb_layout.addWidget(self._vbox_filter)

        tb_layout.addStretch()

        self._vbox_start = QPushButton("Start")
        self._vbox_start.clicked.connect(lambda: self._vbox_action("start"))
        self._vbox_stop = QPushButton("Stop")
        self._vbox_stop.clicked.connect(lambda: self._vbox_action("stop"))
        self._vbox_pause = QPushButton("Pause")
        self._vbox_pause.clicked.connect(lambda: self._vbox_action("pause"))
        self._vbox_reset = QPushButton("Reset")
        self._vbox_reset.clicked.connect(lambda: self._vbox_action("reset"))

        for btn in [self._vbox_start, self._vbox_stop, self._vbox_pause, self._vbox_reset]:
            btn.setStyleSheet(
                f"background: {T.BG_TERTIARY}; color: {T.TEXT_PRIMARY}; border: 1px solid {T.BG_TERTIARY};"
                "border-radius: 6px; padding: 4px 12px;"
            )
            tb_layout.addWidget(btn)

        tab_layout.addWidget(toolbar)

        self._vbox_table = QTableWidget()
        self._vbox_table.setColumnCount(4)
        self._vbox_table.setHorizontalHeaderLabels(["Name", "OS Type", "State", "Memory"])
        self._vbox_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._vbox_table.setStyleSheet(
            f"QTableWidget {{ background: {T.BG_SECONDARY}; color: {T.TEXT_PRIMARY}; border: none; }}"
            f"QHeaderView::section {{ background: {T.BG_TERTIARY}; color: {T.TEXT_MUTED}; padding: 6px; border: none; }}"
        )
        tab_layout.addWidget(self._vbox_table)

        return tab

    def _on_tab_changed(self, index):
        self._current_provider = "vmware" if index == 0 else "virtualbox"
        self._refresh()

    def showEvent(self, event):
        super().showEvent(event)
        # Refresh when shown (the timer skips hidden panels), after the switch has painted.
        from PyQt5.QtCore import QTimer as _QTimer
        _QTimer.singleShot(0, self._refresh)

    def _refresh(self):
        if not self.isVisible():
            return  # a hidden panel does not poll its backends (that froze the window when one was down)
        self._check_backends()
        self._load_vmware()
        self._load_vbox()

    def _check_backends(self):
        from gui.async_adapter import get_adapter
        try:
            adapter = get_adapter()
            adapter.vmware.list_vms()
            self._vmware_status.set_status(True, True)
        except Exception:
            self._vmware_status.set_status(False, False)

        try:
            from gui.async_adapter import get_adapter
            adapter = get_adapter()
            adapter.vbox.list_vms()
            self._vbox_status.set_status(True, True)
        except Exception:
            self._vbox_status.set_status(False, False)

    def _load_vmware(self):
        self._vmware_table.setRowCount(0)
        try:
            from gui.async_adapter import get_adapter
            adapter = get_adapter()
            vms = adapter.vmware.list_vms()
            self._vmware_table.setRowCount(len(vms))
            for i, vm in enumerate(vms):
                self._vmware_table.setItem(i, 0, QTableWidgetItem(vm.get("name", "")))
                self._vmware_table.setItem(i, 1, QTableWidgetItem(vm.get("guest_os", "")))
                self._vmware_table.setItem(i, 2, QTableWidgetItem(vm.get("state", "")))
                self._vmware_table.setItem(i, 3, QTableWidgetItem(vm.get("ip_address", "")))
        except Exception as e:
            self._vmware_table.setRowCount(1)
            self._vmware_table.setItem(0, 0, QTableWidgetItem(f"Error: {e}"))

    def _load_vbox(self):
        self._vbox_table.setRowCount(0)
        try:
            from gui.async_adapter import get_adapter
            adapter = get_adapter()
            vms = adapter.vbox.list_vms()
            self._vbox_table.setRowCount(len(vms))
            for i, vm in enumerate(vms):
                self._vbox_table.setItem(i, 0, QTableWidgetItem(vm.get("name", "")))
                self._vbox_table.setItem(i, 1, QTableWidgetItem(vm.get("os_type", "")))
                self._vbox_table.setItem(i, 2, QTableWidgetItem(vm.get("state", "")))
                self._vbox_table.setItem(i, 3, QTableWidgetItem(vm.get("memory", "")))
        except Exception as e:
            self._vbox_table.setRowCount(1)
            self._vbox_table.setItem(0, 0, QTableWidgetItem(f"Error: {e}"))

    def _vmware_action(self, action):
        row = self._vmware_table.currentRow()
        if row < 0:
            QMessageBox.warning(self, "No Selection", "Please select a VM first.")
            return
        name = self._vmware_table.item(row, 0).text()
        from gui.async_adapter import get_adapter
        adapter = get_adapter()
        if action == "power_on":
            adapter.vmware.power_on(name)
        elif action == "power_off":
            adapter.vmware.power_off(name)
        elif action == "suspend":
            adapter.vmware.suspend(name)
        elif action == "reset":
            adapter.vmware.reset(name)
        elif action == "snapshot":
            adapter.vmware.create_snapshot(name, f"snapshot_{int(time.time())}")
        self.provider_vm_action_requested.emit("vmware", action, name)

    def _vbox_action(self, action):
        row = self._vbox_table.currentRow()
        if row < 0:
            QMessageBox.warning(self, "No Selection", "Please select a VM first.")
            return
        name = self._vbox_table.item(row, 0).text()
        from gui.async_adapter import get_adapter
        adapter = get_adapter()
        if action == "start":
            adapter.vbox.start_vm(name)
        elif action == "stop":
            adapter.vbox.stop_vm(name)
        elif action == "pause":
            adapter.vbox.pause_vm(name)
        elif action == "reset":
            adapter.vbox.reset_vm(name)
        self.provider_vm_action_requested.emit("virtualbox", action, name)
