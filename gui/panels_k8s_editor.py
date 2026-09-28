"""Kubernetes YAML Editor Panel — view, edit, apply, delete manifests.

Portainer-equivalent Kubernetes management with full YAML editing.
"""

from __future__ import annotations

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QTreeWidget, QTreeWidgetItem, QPlainTextEdit,
    QMessageBox, QInputDialog, QSplitter, QTabWidget,
)

from gui.theme import T
from gui.widgets import Card, StatusIndicator


class KubernetesEditorPanel(QWidget):
    """Kubernetes YAML editor and resource manager."""

    k8s_event = pyqtSignal(str, str)  # event_type, message

    def __init__(self, parent=None):
        super().__init__(parent)
        self._adapter = None
        self.setStyleSheet("background: " + T.BG_PRIMARY + ";")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignTop)

        # ── Status ─────────────────────────────────────────────────────────
        status_card = Card("Kubernetes Status")
        status_card.setFixedHeight(50)
        layout.addWidget(status_card)

        status_row = QWidget()
        sr_layout = QHBoxLayout(status_row)
        sr_layout.setContentsMargins(0, 0, 0, 0)
        sr_layout.setSpacing(12)

        self._status_indicator = StatusIndicator(QColor("#ef4444"))
        sr_layout.addWidget(self._status_indicator)
        sr_layout.addWidget(QLabel("Cluster"))

        self._namespace_combo = QComboBox()
        self._namespace_combo.setMinimumWidth(150)
        sr_layout.addWidget(self._namespace_combo)

        sr_layout.addStretch()

        self._btn_refresh = QPushButton("Refresh")
        self._btn_refresh.setStyleSheet(
            f"background: {T.BRAND}; color: {T.TEXT_PRIMARY}; border: none;"
            "border-radius: 6px; padding: 6px 16px; font-weight: bold;"
        )
        self._btn_refresh.clicked.connect(self._refresh)
        sr_layout.addWidget(self._btn_refresh)

        status_card.content_layout.addWidget(status_row)

        # ── Splitter ───────────────────────────────────────────────────────
        splitter = QSplitter(Qt.Horizontal)

        # ── Resource Tree ──────────────────────────────────────────────────
        tree_widget = QWidget()
        tree_layout = QVBoxLayout(tree_widget)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_layout.setSpacing(8)

        tree_header = QLabel("Resources")
        tree_header.setStyleSheet(f"color: {T.TEXT_PRIMARY}; font-weight: bold;")
        tree_layout.addWidget(tree_header)

        self._resource_tree = QTreeWidget()
        self._resource_tree.setHeaderLabels(["Name", "Type", "Status"])
        self._resource_tree.setStyleSheet(
            f"QTreeWidget {{ background: {T.BG_SECONDARY}; color: {T.TEXT_PRIMARY}; border: none; }}"
            f"QTreeWidget::item {{ padding: 4px; }}"
        )
        self._resource_tree.itemDoubleClicked.connect(self._on_resource_selected)
        tree_layout.addWidget(self._resource_tree)

        splitter.addWidget(tree_widget)

        # ── YAML Editor ────────────────────────────────────────────────────
        editor_widget = QWidget()
        editor_layout = QVBoxLayout(editor_widget)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(8)

        editor_header = QLabel("YAML Manifest")
        editor_header.setStyleSheet(f"color: {T.TEXT_PRIMARY}; font-weight: bold;")
        editor_layout.addWidget(editor_header)

        self._yaml_editor = QPlainTextEdit()
        self._yaml_editor.setStyleSheet(
            f"QPlainTextEdit {{ background: #0d1117; color: #c9d1d9; border: 1px solid {T.BG_TERTIARY}; border-radius: 8px; padding: 8px; font-family: Consolas; font-size: 12px; }}"
        )
        self._yaml_editor.setPlaceholderText("Select a resource to view its YAML, or write a new manifest here...")
        editor_layout.addWidget(self._yaml_editor)

        # Action buttons
        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(8)

        self._btn_apply = QPushButton("Apply")
        self._btn_apply.setStyleSheet(
            f"background: #22c55e; color: white; border: none;"
            "border-radius: 6px; padding: 6px 16px; font-weight: bold;"
        )
        self._btn_apply.clicked.connect(self._apply_manifest)
        btn_layout.addWidget(self._btn_apply)

        self._btn_delete = QPushButton("Delete")
        self._btn_delete.setStyleSheet(
            f"background: #ef4444; color: white; border: none;"
            "border-radius: 6px; padding: 6px 16px; font-weight: bold;"
        )
        self._btn_delete.clicked.connect(self._delete_resource)
        btn_layout.addWidget(self._btn_delete)

        self._btn_validate = QPushButton("Validate")
        self._btn_validate.setStyleSheet(
            f"background: {T.BG_TERTIARY}; color: {T.TEXT_PRIMARY}; border: 1px solid {T.BG_TERTIARY};"
            "border-radius: 6px; padding: 6px 16px;"
        )
        self._btn_validate.clicked.connect(self._validate_yaml)
        btn_layout.addWidget(self._btn_validate)

        self._btn_new = QPushButton("New")
        self._btn_new.setStyleSheet(
            f"background: {T.BG_TERTIARY}; color: {T.TEXT_PRIMARY}; border: 1px solid {T.BG_TERTIARY};"
            "border-radius: 6px; padding: 6px 16px;"
        )
        self._btn_new.clicked.connect(self._new_manifest)
        btn_layout.addWidget(self._btn_new)

        editor_layout.addWidget(btn_row)

        splitter.addWidget(editor_widget)

        layout.addWidget(splitter)

        # ── Auto-refresh ───────────────────────────────────────────────────
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh)
        self._refresh_timer.start(15000)

        self._refresh()

    def showEvent(self, event):
        super().showEvent(event)
        # Refresh when shown (the timer skips hidden panels), after the switch has painted.
        from PyQt5.QtCore import QTimer as _QTimer
        _QTimer.singleShot(0, self._refresh)

    def _refresh(self):
        """Refresh all Kubernetes resources."""
        if not self.isVisible():
            return  # a hidden panel does not poll its backends (that froze the window when one was down)
        try:
            from gui.async_adapter import get_adapter
            adapter = get_adapter()
            adapter.kubernetes.list_pods()
            self._status_indicator.set_status(True)
            self._load_resources()
            self._load_namespaces()
        except Exception:
            self._status_indicator.set_status(False)

    def _load_namespaces(self):
        """Load namespace list."""
        try:
            from gui.async_adapter import get_adapter
            adapter = get_adapter()
            namespaces = adapter.kubernetes.list_namespaces()
            self._namespace_combo.clear()
            for ns in namespaces:
                name = ns if isinstance(ns, str) else ns.get("name", "")
                self._namespace_combo.addItem(name)
        except Exception:
            pass

    def _load_resources(self):
        """Load all resources into the tree."""
        self._resource_tree.clear()
        try:
            from gui.async_adapter import get_adapter
            adapter = get_adapter()

            # Pods
            pods = adapter.kubernetes.list_pods()
            for pod in pods:
                item = QTreeWidgetItem(self._resource_tree)
                name = pod.get("name", "") if isinstance(pod, dict) else getattr(pod, "name", "")
                item.setText(0, name)
                item.setText(1, "Pod")
                item.setText(2, pod.get("status", "") if isinstance(pod, dict) else str(getattr(pod, "status", "")))

            # Deployments
            deployments = adapter.kubernetes.list_deployments()
            for dep in deployments:
                item = QTreeWidgetItem(self._resource_tree)
                name = dep.get("name", "") if isinstance(dep, dict) else getattr(dep, "name", "")
                item.setText(0, name)
                item.setText(1, "Deployment")
                item.setText(2, "")

            # Services
            services = adapter.kubernetes.list_services()
            for svc in services:
                item = QTreeWidgetItem(self._resource_tree)
                name = svc.get("name", "") if isinstance(svc, dict) else getattr(svc, "name", "")
                item.setText(0, name)
                item.setText(1, "Service")
                item.setText(2, "")

        except Exception as e:
            item = QTreeWidgetItem(self._resource_tree)
            item.setText(0, f"Error: {e}")

    def _on_resource_selected(self, item: QTreeWidgetItem, column: int):
        """Load selected resource YAML into editor."""
        name = item.text(0)
        kind = item.text(1)
        try:
            from gui.async_adapter import get_adapter
            adapter = get_adapter()
            if kind == "Pod":
                import json
                pods = adapter.kubernetes.list_pods()
                for pod in pods:
                    pod_name = pod.get("name", "") if isinstance(pod, dict) else getattr(pod, "name", "")
                    if pod_name == name:
                        self._yaml_editor.setPlainText(json.dumps(pod, indent=2, default=str))
                        break
        except Exception as e:
            self._yaml_editor.setPlainText(f"# Error loading resource: {e}")

    def _apply_manifest(self):
        """Apply the YAML manifest to the cluster."""
        yaml_text = self._yaml_editor.toPlainText()
        if not yaml_text.strip():
            QMessageBox.warning(self, "Empty Manifest", "Please enter a YAML manifest.")
            return

        try:
            import json
            manifest = json.loads(yaml_text)
            from gui.async_adapter import get_adapter
            adapter = get_adapter()
            adapter.kubernetes.apply_manifest(manifest)
            QMessageBox.information(self, "Applied", "Manifest applied successfully.")
            self.k8s_event.emit("apply", "Manifest applied")
            self._refresh()
        except Exception as e:
            QMessageBox.critical(self, "Apply Failed", f"Failed to apply manifest: {e}")

    def _delete_resource(self):
        """Delete selected resource."""
        item = self._resource_tree.currentItem()
        if not item:
            QMessageBox.warning(self, "No Selection", "Please select a resource to delete.")
            return

        name = item.text(0)
        kind = item.text(1)
        kind_map = {"Pod": "pod", "Deployment": "deployment", "Service": "service"}
        kind_lower = kind_map.get(kind, kind.lower())

        reply = QMessageBox.question(
            self, "Confirm Delete",
            f"Are you sure you want to delete {kind} '{name}'?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        try:
            from gui.async_adapter import get_adapter
            adapter = get_adapter()
            adapter.kubernetes.delete_resource(kind_lower, name)
            QMessageBox.information(self, "Deleted", f"{kind} '{name}' deleted.")
            self.k8s_event.emit("delete", f"{kind} '{name}' deleted")
            self._refresh()
        except Exception as e:
            QMessageBox.critical(self, "Delete Failed", f"Failed to delete: {e}")

    def _validate_yaml(self):
        """Validate YAML syntax."""
        yaml_text = self._yaml_editor.toPlainText()
        try:
            import json
            json.loads(yaml_text)
            QMessageBox.information(self, "Valid", "YAML syntax is valid.")
        except json.JSONDecodeError as e:
            QMessageBox.warning(self, "Invalid", f"YAML syntax error: {e}")

    def _new_manifest(self):
        """Create a new manifest template."""
        template = """apiVersion: v1
kind: Pod
metadata:
  name: my-pod
  labels:
    app: my-app
spec:
  containers:
  - name: my-container
    image: alpine:latest
    command: ["sleep", "300"]
  restartPolicy: Never
"""
        self._yaml_editor.setPlainText(template)
