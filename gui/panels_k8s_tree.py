"""Hierarchical Kubernetes Resource Tree Panel.

Displays Namespace → [Deployments, Pods, Services] with expandable nodes,
color-coded status indicators, and a context menu (delete / write-yaml).
"""

from __future__ import annotations

from typing import Any

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QBrush
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QTreeWidget, QTreeWidgetItem, QPlainTextEdit,
    QMessageBox, QMenu, QAction, QSplitter, QHeaderView,
)

from gui.theme import T
from gui.widgets import Card, StatusIndicator

# ── Status color helpers ────────────────────────────────────────────────────────

def _status_color(status: str) -> str:
    """Map a status string to a hex color."""
    s = (status or "").lower()
    if s in ("running", "active", "ready", "succeeded", "healthy"):
        return T.SUCCESS  # #22c55e
    if s in ("pending", "containercreating", "terminating", "initializing", "progressing", "updating"):
        return T.WARNING  # #f59e0b
    if s in ("error", "failed", "crashloopbackoff", "imagepullbackoff", "evicted", "notready", "unknown"):
        return T.ERROR  # #ef4444
    return T.TEXT_MUTED  # #64748b


def _status_brush(status: str) -> QBrush:
    return QBrush(QColor(_status_color(status)))


def _status_label(status: str) -> str:
    """Return a human-readable label for a status."""
    return (status or "Unknown").title()


# ── Tree item helpers ───────────────────────────────────────────────────────────

def _make_status_dot(color: str) -> str:
    return f'<span style="color:{color};">●</span>'


class KubernetesTreePanel(QWidget):
    """Hierarchical Kubernetes resource tree with status indicators and context menu."""

    k8s_event = pyqtSignal(str, str)  # event_type, message

    def __init__(self, parent=None):
        super().__init__(parent)
        self._adapter = None
        self._namespace_items: dict[str, QTreeWidgetItem] = {}  # ns_name → top-level item
        self._resource_lookup: dict[tuple[str, str], dict] = {}  # (kind, name) → resource dict

        self.setStyleSheet("background: " + T.BG_PRIMARY + ";")
        self._build_ui()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh)
        self._refresh_timer.setInterval(10000)
        self._refresh()

    # ── Visibility-based timer control ───────────────────────────────────────

    def showEvent(self, event: Any) -> None:
        """Start polling when the panel becomes visible."""
        super().showEvent(event)
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()
            self._refresh()

    def hideEvent(self, event: Any) -> None:
        """Stop polling when the panel is hidden."""
        super().hideEvent(event)
        self._refresh_timer.stop()

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignTop)

        # ── Status bar ─────────────────────────────────────────────────────
        status_card = Card("Kubernetes Cluster")
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
        self._namespace_combo.currentTextChanged.connect(self._on_namespace_selected)
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

        # ── Splitter: tree | YAML editor ──────────────────────────────────
        splitter = QSplitter(Qt.Horizontal)

        # ── Tree pane ─────────────────────────────────────────────────────
        tree_widget = QWidget()
        tree_layout = QVBoxLayout(tree_widget)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_layout.setSpacing(8)

        tree_header = QLabel("Resource Hierarchy")
        tree_header.setStyleSheet(f"color: {T.TEXT_PRIMARY}; font-weight: bold;")
        tree_layout.addWidget(tree_header)

        self._resource_tree = QTreeWidget()
        self._resource_tree.setHeaderLabels(["", "Name", "Type", "Status"])
        self._resource_tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._resource_tree.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self._resource_tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._resource_tree.header().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self._resource_tree.setStyleSheet(
            f"QTreeWidget {{ background: {T.BG_SECONDARY}; color: {T.TEXT_PRIMARY}; border: none; }}"
            f"QTreeWidget::item {{ padding: 4px; }}"
            "QTreeWidget::item:selected { background: #1e3a5f; }"
            "QTreeWidget::item:hover { background: " + T.BG_TERTIARY + "; }"
        )
        self._resource_tree.setAlternatingRowColors(True)
        self._resource_tree.setAnimated(True)
        self._resource_tree.setRootIsDecorated(True)
        self._resource_tree.setExpandsOnDoubleClick(True)
        self._resource_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._resource_tree.customContextMenuRequested.connect(self._show_context_menu)
        self._resource_tree.itemDoubleClicked.connect(self._on_resource_double_clicked)
        self._resource_tree.itemClicked.connect(self._on_resource_clicked)
        tree_layout.addWidget(self._resource_tree)

        splitter.addWidget(tree_widget)

        # ── YAML editor pane ──────────────────────────────────────────────
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

        # Editor action buttons
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
        self._btn_delete.clicked.connect(self._delete_current)
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

        splitter.setSizes([320, 480])
        layout.addWidget(splitter)

    # ── Refresh / data loading ───────────────────────────────────────────────

    def _refresh(self):
        """Refresh all Kubernetes resources."""
        if not self.isVisible():
            return  # a hidden panel does not poll its backends (that froze the window when one was down)
        try:
            from gui.async_adapter import get_adapter
            adapter = get_adapter()
            adapter.kubernetes.list_pods()
            self._status_indicator.set_status(True)
            self._load_namespaces()
            self._load_resources()
        except Exception:
            self._status_indicator.set_status(False)

    def _load_namespaces(self):
        """Load namespace list into the combo box."""
        try:
            from gui.async_adapter import get_adapter
            adapter = get_adapter()
            namespaces = adapter.kubernetes.list_namespaces()
            self._namespace_combo.clear()
            for ns in namespaces:
                name = ns if isinstance(ns, str) else ns.get("name", "")
                if name:
                    self._namespace_combo.addItem(name)
        except Exception:
            pass

    def _load_resources(self):
        """Load all resources into the hierarchical tree.

        Structure:
            Namespace
            ├── Deployments
            │   ├── deploy-a  (status dot)
            │   └── deploy-b
            ├── Pods
            │   ├── pod-1  (status dot)
            │   └── pod-2
            └── Services
                ├── svc-a  (status dot)
                └── svc-b
        """
        self._resource_tree.clear()
        self._namespace_items.clear()
        self._resource_lookup.clear()

        try:
            from gui.async_adapter import get_adapter
            adapter = get_adapter()

            # Fetch all resources
            pods = adapter.kubernetes.list_pods()
            deployments = adapter.kubernetes.list_deployments()
            services = adapter.kubernetes.list_services()
            namespaces = adapter.kubernetes.list_namespaces()

            # Determine namespace to show (combo selection or all)
            selected_ns = self._namespace_combo.currentText() if self._namespace_combo.count() else None

            # Build namespace list
            ns_names: list[str] = []
            for ns in namespaces:
                name = ns if isinstance(ns, str) else ns.get("name", "")
                if name and name not in ns_names:
                    ns_names.append(name)

            if selected_ns and selected_ns in ns_names:
                ns_names = [selected_ns]

            # Create namespace nodes
            for ns_name in ns_names:
                ns_item = QTreeWidgetItem(self._resource_tree)
                ns_item.setText(1, ns_name)
                ns_item.setText(2, "Namespace")
                ns_item.setText(3, "Active")
                ns_item.setExpanded(True)
                # Make namespace items bold-ish via foreground color
                for col in range(4):
                    ns_item.setForeground(col, QBrush(QColor(T.TEXT_PRIMARY)))
                self._namespace_items[ns_name] = ns_item

                # Add category sub-nodes
                deploy_cat = self._add_category(ns_item, "Deployments")
                pod_cat = self._add_category(ns_item, "Pods")
                svc_cat = self._add_category(ns_item, "Services")

                # Deployments under this namespace
                for dep in deployments:
                    dep_name = dep.get("name", "") if isinstance(dep, dict) else getattr(dep, "name", "")
                    dep_ns = dep.get("namespace", "default") if isinstance(dep, dict) else getattr(dep, "namespace", "default")
                    if dep_ns and dep_ns != ns_name:
                        continue
                    status = (dep.get("status", "Unknown") if isinstance(dep, dict)
                              else str(getattr(dep, "status", "Unknown")))
                    item = self._make_resource_item(dep_name, "Deployment", status)
                    item.setData(0, Qt.UserRole, {"kind": "deployment", "name": dep_name, "namespace": dep_ns})
                    deploy_cat.addChild(item)
                    self._resource_lookup[("Deployment", dep_name)] = dep

                # Pods under this namespace
                for pod in pods:
                    pod_name = pod.get("name", "") if isinstance(pod, dict) else getattr(pod, "name", "")
                    pod_ns = pod.get("namespace", "default") if isinstance(pod, dict) else getattr(pod, "namespace", "default")
                    if pod_ns and pod_ns != ns_name:
                        continue
                    status = (pod.get("status", "Unknown") if isinstance(pod, dict)
                              else str(getattr(pod, "status", "Unknown")))
                    item = self._make_resource_item(pod_name, "Pod", status)
                    item.setData(0, Qt.UserRole, {"kind": "pod", "name": pod_name, "namespace": pod_ns})
                    pod_cat.addChild(item)
                    self._resource_lookup[("Pod", pod_name)] = pod

                # Services under this namespace
                for svc in services:
                    svc_name = svc.get("name", "") if isinstance(svc, dict) else getattr(svc, "name", "")
                    svc_ns = svc.get("namespace", "default") if isinstance(svc, dict) else getattr(svc, "namespace", "default")
                    if svc_ns and svc_ns != ns_name:
                        continue
                    status = (svc.get("status", "Unknown") if isinstance(svc, dict)
                              else str(getattr(svc, "status", "Unknown")))
                    item = self._make_resource_item(svc_name, "Service", status)
                    item.setData(0, Qt.UserRole, {"kind": "service", "name": svc_name, "namespace": svc_ns})
                    svc_cat.addChild(item)
                    self._resource_lookup[("Service", svc_name)] = svc

        except Exception as e:
            err_item = QTreeWidgetItem(self._resource_tree)
            err_item.setText(1, f"Error: {e}")
            err_item.setForeground(1, QBrush(QColor(T.ERROR)))

    def _add_category(self, parent: QTreeWidgetItem, label: str) -> QTreeWidgetItem:
        """Add a category child node under a namespace."""
        cat = QTreeWidgetItem(parent)
        cat.setText(1, label)
        cat.setText(2, "")
        cat.setText(3, "")
        cat.setExpanded(True)
        for col in range(4):
            cat.setForeground(col, QBrush(QColor(T.TEXT_SECONDARY)))
        return cat

    def _make_resource_item(self, name: str, kind: str, status: str) -> QTreeWidgetItem:
        """Create a leaf resource item with a colored status dot."""
        item = QTreeWidgetItem()
        color = _status_color(status)
        # Colored status dot
        item.setText(0, _make_status_dot(color))
        item.setForeground(0, QBrush(QColor(color)))
        # Name
        item.setText(1, name)
        # Type
        item.setText(2, kind)
        # Status
        item.setText(3, _status_label(status))
        item.setForeground(3, QBrush(QColor(color)))
        return item

    # ── Context menu ─────────────────────────────────────────────────────────

    def _show_context_menu(self, pos):
        """Show right-click context menu for tree items."""
        item = self._resource_tree.itemAt(pos)
        if item is None:
            return

        # Only show for leaf resource items (those with data set)
        data = item.data(0, Qt.UserRole)
        if data is None:
            return

        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background: {T.BG_SECONDARY}; color: {T.TEXT_PRIMARY}; border: 1px solid {T.BG_TERTIARY}; }}"
            "QMenu::item { padding: 6px 20px; }"
            f"QMenu::item:selected {{ background: #1e3a5f; color: {T.BRAND}; }}"
        )

        write_action = QAction("Write YAML", self)
        write_action.triggered.connect(lambda: self._write_yaml(item))
        menu.addAction(write_action)

        menu.addSeparator()

        delete_action = QAction("Delete", self)
        delete_action.triggered.connect(lambda: self._delete_item(item))
        menu.addAction(delete_action)

        menu.exec_(self._resource_tree.viewport().mapToGlobal(pos))

    def _write_yaml(self, item: QTreeWidgetItem):
        """Write the resource's YAML to the editor pane."""
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        kind = data["kind"]
        name = data["name"]
        resource = self._resource_lookup.get((kind, name))
        if resource:
            import json
            self._yaml_editor.setPlainText(json.dumps(resource, indent=2, default=str))
        else:
            self._yaml_editor.setPlainText(f"# Could not find resource: {kind}/{name}\n")

    # ── Tree interaction ─────────────────────────────────────────────────────

    def _on_namespace_selected(self, ns_name: str):
        """Reload tree when namespace filter changes."""
        if ns_name:
            self._load_resources()

    def _on_resource_double_clicked(self, item: QTreeWidgetItem, column: int):
        """Double-click to view resource YAML."""
        data = item.data(0, Qt.UserRole)
        if data:
            self._write_yaml(item)

    def _on_resource_clicked(self, item: QTreeWidgetItem, column: int):
        """Single-click also shows YAML for resource items."""
        data = item.data(0, Qt.UserRole)
        if data:
            self._write_yaml(item)

    # ── Actions ──────────────────────────────────────────────────────────────

    def _delete_current(self):
        """Delete the currently selected tree item."""
        item = self._resource_tree.currentItem()
        if item:
            self._delete_item(item)
        else:
            QMessageBox.warning(self, "No Selection", "Please select a resource to delete.")

    def _delete_item(self, item: QTreeWidgetItem):
        """Delete a resource represented by a tree item."""
        data = item.data(0, Qt.UserRole)
        if not data:
            return

        kind = data["kind"]
        name = data["name"]
        kind_display = kind.capitalize()

        reply = QMessageBox.question(
            self, "Confirm Delete",
            f"Are you sure you want to delete {kind_display} '{name}'?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        try:
            from gui.async_adapter import get_adapter
            adapter = get_adapter()
            adapter.kubernetes.delete_resource(kind, name)
            QMessageBox.information(self, "Deleted", f"{kind_display} '{name}' deleted.")
            self.k8s_event.emit("delete", f"{kind_display} '{name}' deleted")
            self._refresh()
        except Exception as e:
            QMessageBox.critical(self, "Delete Failed", f"Failed to delete: {e}")

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

    def _validate_yaml(self):
        """Validate YAML/JSON syntax in the editor."""
        yaml_text = self._yaml_editor.toPlainText()
        try:
            import json
            json.loads(yaml_text)
            QMessageBox.information(self, "Valid", "YAML syntax is valid.")
        except json.JSONDecodeError as e:
            QMessageBox.warning(self, "Invalid", f"YAML syntax error: {e}")

    def _new_manifest(self):
        """Insert a new manifest template into the editor."""
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
