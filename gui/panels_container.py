"""Container Panel — Docker, Kubernetes, and Podman management.

Provides tabs for:
- Containers: list, start, stop, restart, logs, exec, inspect
- Images: list, pull, remove
- Kubernetes: pods, services, deployments, nodes
- Podman: container and pod management
"""

from __future__ import annotations

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QGridLayout, QProgressBar, QCheckBox, QSpinBox,
    QSizePolicy, QFrame, QTextBrowser, QTreeWidget, QTreeWidgetItem,
    QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
    QInputDialog,
)

from gui.theme import T
from gui.widgets import Card, StatusIndicator, SectionHeader, StatCard


class ContainerPanel(QWidget):
    """Container management panel for Docker, Kubernetes, and Podman."""

    container_action_requested = pyqtSignal(str, str)  # action, container_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._docker_backend = None
        self._kube_backend = None
        self._podman_backend = None
        self.setStyleSheet("background: " + T.BG_PRIMARY + ";")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignTop)

        # ── Backend Status ──────────────────────────────────────────────────
        status_card = Card("Backend Status")
        status_card.setFixedHeight(50)
        layout.addWidget(status_card)

        status_row = QWidget()
        sr_layout = QHBoxLayout(status_row)
        sr_layout.setContentsMargins(0, 0, 0, 0)
        sr_layout.setSpacing(12)

        self._docker_status = StatusIndicator(QColor("#ef4444"))
        sr_layout.addWidget(self._docker_status)
        sr_layout.addWidget(QLabel("Docker"))

        self._kube_status = StatusIndicator(QColor("#ef4444"))
        sr_layout.addWidget(self._kube_status)
        sr_layout.addWidget(QLabel("Kubernetes"))

        self._podman_status = StatusIndicator(QColor("#ef4444"))
        sr_layout.addWidget(self._podman_status)
        sr_layout.addWidget(QLabel("Podman"))

        sr_layout.addStretch()

        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.setStyleSheet(
            f"background: {T.BRAND}; color: {T.TEXT_PRIMARY}; border: none;"
            "border-radius: 6px; padding: 6px 16px; font-weight: bold;"
        )
        self._refresh_btn.clicked.connect(self._refresh)
        sr_layout.addWidget(self._refresh_btn)

        status_card.content_layout.addWidget(status_row)

        # ── Tab Widget ──────────────────────────────────────────────────────
        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(
            f"QTabWidget::pane {{ background: {T.BG_SECONDARY}; border: 1px solid {T.BG_TERTIARY}; border-radius: 8px; }}"
            f"QTabBar::tab {{ background: {T.BG_TERTIARY}; color: {T.TEXT_MUTED}; padding: 8px 20px; border-radius: 6px 6px 0 0; margin-right: 4px; }}"
            f"QTabBar::tab:selected {{ background: {T.BRAND}; color: {T.TEXT_PRIMARY}; }}"
        )
        layout.addWidget(self._tabs)

        # ── Containers Tab ──────────────────────────────────────────────────
        self._tabs.addTab(self._build_containers_tab(), "Containers")

        # ── Images Tab ──────────────────────────────────────────────────────
        self._tabs.addTab(self._build_images_tab(), "Images")

        # ── Kubernetes Tab ──────────────────────────────────────────────────
        self._tabs.addTab(self._build_kubernetes_tab(), "Kubernetes")

        # ── Podman Tab ──────────────────────────────────────────────────────
        self._tabs.addTab(self._build_podman_tab(), "Podman")

        # Auto-refresh timer
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh)
        self._refresh_timer.start(10000)

    def _build_containers_tab(self) -> QWidget:
        """Build the containers list tab."""
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)
        tab_layout.setSpacing(8)

        # Toolbar
        toolbar = QWidget()
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(0, 0, 0, 0)
        tb_layout.setSpacing(8)

        self._container_filter = QComboBox()
        self._container_filter.addItems(["All", "Running", "Stopped", "Paused"])
        self._container_filter.currentTextChanged.connect(self._refresh)
        tb_layout.addWidget(self._container_filter)

        tb_layout.addStretch()

        self._btn_start = QPushButton("Start")
        self._btn_start.clicked.connect(lambda: self._container_action("start"))
        self._btn_stop = QPushButton("Stop")
        self._btn_stop.clicked.connect(lambda: self._container_action("stop"))
        self._btn_restart = QPushButton("Restart")
        self._btn_restart.clicked.connect(lambda: self._container_action("restart"))
        self._btn_logs = QPushButton("Logs")
        self._btn_logs.clicked.connect(lambda: self._container_action("logs"))
        self._btn_exec = QPushButton("Exec")
        self._btn_exec.clicked.connect(lambda: self._container_action("exec"))
        self._btn_remove = QPushButton("Remove")
        self._btn_remove.clicked.connect(lambda: self._container_action("remove"))

        for btn in [self._btn_start, self._btn_stop, self._btn_restart, self._btn_logs, self._btn_exec, self._btn_remove]:
            btn.setStyleSheet(
                f"background: {T.BG_TERTIARY}; color: {T.TEXT_PRIMARY}; border: 1px solid {T.BG_TERTIARY};"
                "border-radius: 6px; padding: 4px 12px;"
            )
            tb_layout.addWidget(btn)

        tab_layout.addWidget(toolbar)

        # Container table
        self._container_table = QTableWidget()
        self._container_table.setColumnCount(5)
        self._container_table.setHorizontalHeaderLabels(["Name", "Image", "Status", "Ports", "Actions"])
        self._container_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._container_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._container_table.setStyleSheet(
            f"QTableWidget {{ background: {T.BG_SECONDARY}; color: {T.TEXT_PRIMARY}; border: none; }}"
            f"QHeaderView::section {{ background: {T.BG_TERTIARY}; color: {T.TEXT_MUTED}; padding: 6px; border: none; }}"
        )
        tab_layout.addWidget(self._container_table)

        return tab

    def _build_images_tab(self) -> QWidget:
        """Build the images list tab."""
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)
        tab_layout.setSpacing(8)

        # Toolbar
        toolbar = QWidget()
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(0, 0, 0, 0)
        tb_layout.setSpacing(8)

        self._image_filter = QComboBox()
        self._image_filter.addItems(["All", "Local", "Remote"])
        tb_layout.addWidget(self._image_filter)

        tb_layout.addStretch()

        self._btn_pull = QPushButton("Pull Image")
        self._btn_pull.clicked.connect(self._pull_image)
        self._btn_remove_image = QPushButton("Remove")
        self._btn_remove_image.clicked.connect(lambda: self._image_action("remove"))

        for btn in [self._btn_pull, self._btn_remove_image]:
            btn.setStyleSheet(
                f"background: {T.BG_TERTIARY}; color: {T.TEXT_PRIMARY}; border: 1px solid {T.BG_TERTIARY};"
                "border-radius: 6px; padding: 4px 12px;"
            )
            tb_layout.addWidget(btn)

        tab_layout.addWidget(toolbar)

        # Image table
        self._image_table = QTableWidget()
        self._image_table.setColumnCount(4)
        self._image_table.setHorizontalHeaderLabels(["Repository", "Tag", "Size", "Created"])
        self._image_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._image_table.setStyleSheet(
            f"QTableWidget {{ background: {T.BG_SECONDARY}; color: {T.TEXT_PRIMARY}; border: none; }}"
            f"QHeaderView::section {{ background: {T.BG_TERTIARY}; color: {T.TEXT_MUTED}; padding: 6px; border: none; }}"
        )
        tab_layout.addWidget(self._image_table)

        return tab

    def _build_kubernetes_tab(self) -> QWidget:
        """Build the Kubernetes tab."""
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)
        tab_layout.setSpacing(8)

        # Toolbar
        toolbar = QWidget()
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(0, 0, 0, 0)
        tb_layout.setSpacing(8)

        self._kube_filter = QComboBox()
        self._kube_filter.addItems(["All Namespaces", "default", "kube-system", "kube-public"])
        tb_layout.addWidget(self._kube_filter)

        tb_layout.addStretch()

        self._btn_apply = QPushButton("Apply YAML")
        self._btn_apply.clicked.connect(self._apply_yaml)
        self._btn_delete = QPushButton("Delete")
        self._btn_delete.clicked.connect(self._delete_resource)

        for btn in [self._btn_apply, self._btn_delete]:
            btn.setStyleSheet(
                f"background: {T.BG_TERTIARY}; color: {T.TEXT_PRIMARY}; border: 1px solid {T.BG_TERTIARY};"
                "border-radius: 6px; padding: 4px 12px;"
            )
            tb_layout.addWidget(btn)

        tab_layout.addWidget(toolbar)

        # Resource tree
        self._kube_tree = QTreeWidget()
        self._kube_tree.setHeaderLabels(["Name", "Type", "Status", "Age"])
        self._kube_tree.setStyleSheet(
            f"QTreeWidget {{ background: {T.BG_SECONDARY}; color: {T.TEXT_PRIMARY}; border: none; }}"
            f"QTreeWidget::item {{ padding: 4px; }}"
        )
        tab_layout.addWidget(self._kube_tree)

        return tab

    def _build_podman_tab(self) -> QWidget:
        """Build the Podman tab."""
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)
        tab_layout.setSpacing(8)

        # Toolbar
        toolbar = QWidget()
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(0, 0, 0, 0)
        tb_layout.setSpacing(8)

        self._podman_filter = QComboBox()
        self._podman_filter.addItems(["All", "Running", "Stopped"])
        tb_layout.addWidget(self._podman_filter)

        tb_layout.addStretch()

        self._btn_start_podman = QPushButton("Start")
        self._btn_start_podman.clicked.connect(lambda: self._podman_action("start"))
        self._btn_stop_podman = QPushButton("Stop")
        self._btn_stop_podman.clicked.connect(lambda: self._podman_action("stop"))

        for btn in [self._btn_start_podman, self._btn_stop_podman]:
            btn.setStyleSheet(
                f"background: {T.BG_TERTIARY}; color: {T.TEXT_PRIMARY}; border: 1px solid {T.BG_TERTIARY};"
                "border-radius: 6px; padding: 4px 12px;"
            )
            tb_layout.addWidget(btn)

        tab_layout.addWidget(toolbar)

        # Podman table
        self._podman_table = QTableWidget()
        self._podman_table.setColumnCount(4)
        self._podman_table.setHorizontalHeaderLabels(["Name", "Image", "Status", "Ports"])
        self._podman_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._podman_table.setStyleSheet(
            f"QTableWidget {{ background: {T.BG_SECONDARY}; color: {T.TEXT_PRIMARY}; border: none; }}"
            f"QHeaderView::section {{ background: {T.BG_TERTIARY}; color: {T.TEXT_MUTED}; padding: 6px; border: none; }}"
        )
        tab_layout.addWidget(self._podman_table)

        return tab

    def showEvent(self, event):
        super().showEvent(event)
        # Refresh when shown (the timer skips hidden panels), after the switch has painted.
        from PyQt5.QtCore import QTimer as _QTimer
        _QTimer.singleShot(0, self._refresh)

    def _refresh(self):
        """Refresh all container data."""
        if not self.isVisible():
            return  # a hidden panel does not poll its backends (that froze the window when one was down)
        self._check_backends()
        self._load_containers()
        self._load_images()
        self._load_kubernetes()
        self._load_podman()

    def _check_backends(self):
        """Check which backends are available."""
        from gui.async_adapter import get_adapter
        try:
            adapter = get_adapter()
            adapter.docker.list_containers()
            self._docker_status.set_status(True)
        except Exception:
            self._docker_status.set_status(False)

        try:
            from gui.async_adapter import get_adapter
            adapter = get_adapter()
            adapter.kubernetes.list_pods()
            self._kube_status.set_status(True)
        except Exception:
            self._kube_status.set_status(False)

        try:
            from gui.async_adapter import get_adapter
            adapter = get_adapter()
            adapter.podman.list_containers()
            self._podman_status.set_status(True)
        except Exception:
            self._podman_status.set_status(False)

    def _load_containers(self):
        """Load container list from Docker."""
        self._container_table.setRowCount(0)
        try:
            from gui.async_adapter import get_adapter
            adapter = get_adapter()
            containers = adapter.docker.list_containers()
            self._container_table.setRowCount(len(containers))
            for i, c in enumerate(containers):
                self._container_table.setItem(i, 0, QTableWidgetItem(c.get("name", "")))
                self._container_table.setItem(i, 1, QTableWidgetItem(c.get("image", "")))
                self._container_table.setItem(i, 2, QTableWidgetItem(c.get("status", "")))
                self._container_table.setItem(i, 3, QTableWidgetItem(c.get("ports", "")))
        except Exception as e:
            self._container_table.setRowCount(1)
            self._container_table.setItem(0, 0, QTableWidgetItem(f"Error: {e}"))

    def _load_images(self):
        """Load image list from Docker."""
        self._image_table.setRowCount(0)
        try:
            from gui.async_adapter import get_adapter
            adapter = get_adapter()
            images = adapter.docker.list_images()
            self._image_table.setRowCount(len(images))
            for i, img in enumerate(images):
                self._image_table.setItem(i, 0, QTableWidgetItem(img.get("repository", "")))
                self._image_table.setItem(i, 1, QTableWidgetItem(img.get("tag", "")))
                self._image_table.setItem(i, 2, QTableWidgetItem(img.get("size", "")))
                self._image_table.setItem(i, 3, QTableWidgetItem(img.get("created", "")))
        except Exception as e:
            self._image_table.setRowCount(1)
            self._image_table.setItem(0, 0, QTableWidgetItem(f"Error: {e}"))

    def _load_kubernetes(self):
        """Load Kubernetes resources."""
        self._kube_tree.clear()
        try:
            from gui.async_adapter import get_adapter
            adapter = get_adapter()
            pods = adapter.kubernetes.list_pods()
            for pod in pods:
                item = QTreeWidgetItem(self._kube_tree)
                item.setText(0, pod.get("name", ""))
                item.setText(1, "Pod")
                item.setText(2, pod.get("status", ""))
                item.setText(3, pod.get("age", ""))
        except Exception as e:
            item = QTreeWidgetItem(self._kube_tree)
            item.setText(0, f"Error: {e}")

    def _load_podman(self):
        """Load Podman containers."""
        self._podman_table.setRowCount(0)
        try:
            from gui.async_adapter import get_adapter
            adapter = get_adapter()
            containers = adapter.podman.list_containers()
            self._podman_table.setRowCount(len(containers))
            for i, c in enumerate(containers):
                self._podman_table.setItem(i, 0, QTableWidgetItem(c.get("name", "")))
                self._podman_table.setItem(i, 1, QTableWidgetItem(c.get("image", "")))
                self._podman_table.setItem(i, 2, QTableWidgetItem(c.get("status", "")))
                self._podman_table.setItem(i, 3, QTableWidgetItem(c.get("ports", "")))
        except Exception as e:
            self._podman_table.setRowCount(1)
            self._podman_table.setItem(0, 0, QTableWidgetItem(f"Error: {e}"))

    def _container_action(self, action: str):
        """Execute container action."""
        row = self._container_table.currentRow()
        if row < 0:
            QMessageBox.warning(self, "No Selection", "Please select a container first.")
            return
        name = self._container_table.item(row, 0).text()
        from gui.async_adapter import get_adapter
        adapter = get_adapter()
        try:
            if action == "start":
                adapter.docker.start_container(name)
            elif action == "stop":
                adapter.docker.stop_container(name)
            elif action == "restart":
                adapter.docker.restart_container(name)
            elif action == "logs":
                from gui.dialogs_container_logs import ContainerLogsDialog
                dlg = ContainerLogsDialog(container_name=name, parent=self)
                dlg.exec_()
            elif action == "exec":
                adapter.docker.exec_command(name, "/bin/sh")
            elif action == "remove":
                adapter.docker.remove_container(name)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Container action failed: {e}")
        self.container_action_requested.emit(action, name)

    def _image_action(self, action: str):
        """Execute image action."""
        row = self._image_table.currentRow()
        if row < 0:
            QMessageBox.warning(self, "No Selection", "Please select an image first.")
            return

    def _pull_image(self):
        """Pull a Docker image."""
        name, ok = QInputDialog.getText(self, "Pull Image", "Image name:")
        if ok and name:
            pass

    def _apply_yaml(self):
        """Apply Kubernetes YAML."""
        pass

    def _delete_resource(self):
        """Delete Kubernetes resource."""
        pass

    def _podman_action(self, action: str):
        """Execute Podman action."""
        row = self._podman_table.currentRow()
        if row < 0:
            QMessageBox.warning(self, "No Selection", "Please select a container first.")
            return
