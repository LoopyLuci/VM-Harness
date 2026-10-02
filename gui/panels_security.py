"""Security panel — encrypted credential vault management.

Provides a secure interface for adding, editing, deleting, and
viewing credentials (passwords, API keys, SSH keys, QMP passwords).
Uses Fernet encryption; values are never exposed in plaintext
outside the add/edit dialog.
"""

from __future__ import annotations

from gui.theme import T
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QIcon, QBrush
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QGroupBox,
    QTreeWidget,
    QTreeWidgetItem,
    QHeaderView,
    QSplitter,
    QMessageBox,
    QDialog,
    QFormLayout,
    QLineEdit,
    QTextEdit,
    QComboBox,
    QSpinBox,
    QCheckBox,
    QSizePolicy,
    QInputDialog,
    QFileDialog,
)

from gui.widgets import Card, TextInput, PasswordInput, CredentialTreeItem
from gui.credential_store import CredentialStore


# ── Credential Dialog ───────────────────────────────────────────────────────────

class CredentialDialog(QDialog):
    """Dialog for adding or editing a credential."""

    def __init__(self, parent=None, cred_id: str | None = None):
        super().__init__(parent)
        self.cred_id = cred_id
        self.setWindowTitle("Add Credential" if cred_id is None else "Edit Credential")
        self.setMinimumWidth(480)
        self.setStyleSheet("""
            QDialog {
                background: #0f172a;
            }
            QLabel {
                color: #cbd5e1;
                font-size: 12px;
            }
        """)

        layout = QFormLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        # Name
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("e.g., Production SSH Key")
        self.name_input.setStyleSheet("""
            QLineEdit {
                background: #0f172a;
                border: 1px solid #334155;
                border-radius: 4px;
                color: #e2e8f0;
                padding: 6px 10px;
                font-size: 13px;
            }
            QLineEdit:focus { border-color: #3b82f6; }
        """)
        layout.addRow("Name *", self.name_input)

        # Type
        self.type_combo = QComboBox()
        self.type_combo.addItems(["password", "ssh_key", "api_key", "qmp_pass", "other"])
        self.type_combo.setStyleSheet("""
            QComboBox {
                background: #0f172a;
                color: #e2e8f0;
                border: 1px solid #334155;
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 12px;
            }
            QComboBox:hover { border-color: #3b82f6; }
        """)
        layout.addRow("Type", self.type_combo)

        # Value
        self.value_input = QLineEdit()
        self.value_input.setPlaceholderText("Enter the secret value...")
        self.value_input.setEchoMode(QLineEdit.Password)
        self.value_input.setStyleSheet("""
            QLineEdit {
                background: #0f172a;
                border: 1px solid #334155;
                border-radius: 4px;
                color: #e2e8f0;
                padding: 6px 10px;
                font-size: 13px;
            }
            QLineEdit:focus { border-color: #3b82f6; }
        """)
        layout.addRow("Secret Value *", self.value_input)

        # Description
        self.desc_input = QLineEdit()
        self.desc_input.setPlaceholderText("Optional description for reference...")
        self.desc_input.setStyleSheet("""
            QLineEdit {
                background: #0f172a;
                border: 1px solid #334155;
                border-radius: 4px;
                color: #e2e8f0;
                padding: 6px 10px;
                font-size: 13px;
            }
            QLineEdit:focus { border-color: #3b82f6; }
        """)
        layout.addRow("Description", self.desc_input)

        # Buttons
        btn_row = QWidget()
        btn_row_layout = QHBoxLayout(btn_row)
        btn_row_layout.setContentsMargins(0, 0, 0, 0)
        btn_row_layout.setSpacing(8)

        self.save_btn = QPushButton("Save")
        self.save_btn.setFixedHeight(32)
        self.save_btn.setStyleSheet("""
            QPushButton {
                background: #22c55e;
                color: white;
                border: none;
                border-radius: 4px;
                font-size: 12px;
                font-weight: 600;
                padding: 0 20px;
            }
            QPushButton:hover { background: #16a34a; }
        """)
        btn_row_layout.addWidget(self.save_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedHeight(32)
        cancel_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #64748b;
                border: none;
                font-size: 12px;
            }
            QPushButton:hover { color: #94a3b8; }
        """)
        cancel_btn.clicked.connect(self.reject)
        btn_row_layout.addWidget(cancel_btn)

        btn_row_layout.addStretch()
        layout.addRow(btn_row)

        # Load existing if editing
        if cred_id:
            self.setWindowTitle("Edit Credential")
            self.load_credential(cred_id)

        self.save_btn.clicked.connect(self._save)

    def load_credential(self, cred_id: str) -> None:
        """Load credential data into the form."""
        store = CredentialStore()
        entry = store.get(cred_id)
        if entry:
            self.name_input.setText(entry.name)
            self.type_combo.setCurrentText(entry.credential_type)
            # Show value only if the user confirms
            from PyQt5.QtWidgets import QMessageBox
            resp = QMessageBox.question(
                self, "Show Secret",
                f"Show the current value of '{entry.name}'?\n\nWarning: This will briefly expose the secret in this dialog.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if resp == QMessageBox.Yes:
                self.value_input.setText(entry["value"])
                self.value_input.setEchoMode(QLineEdit.Normal)
            self.desc_input.setText(entry.get("description", ""))

    def _save(self):
        """Save the credential."""
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Validation Error", "Name is required.")
            return

        value = self.value_input.text().strip()
        if not value:
            QMessageBox.warning(self, "Validation Error", "Secret value is required.")
            return

        store = CredentialStore()
        try:
            if self.cred_id:
                store.update(self.cred_id, value, self.desc_input.text().strip())
            else:
                store.add(name, self.type_combo.currentText(), value, self.desc_input.text().strip())
            QMessageBox.information(self, "Success", "Credential saved securely.")
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save credential: {e}")


# ── Security Panel ──────────────────────────────────────────────────────────────

class SecurityPanel(QWidget):
    """Encrypted credential vault management panel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: #0f172a;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # ── Stats Bar ──────────────────────────────────────────────────────────
        stats_card = Card("")
        stats_card.setFixedHeight(40)
        layout.addWidget(stats_card)

        stats_row = QWidget()
        stats_row_layout = QHBoxLayout(stats_row)
        stats_row_layout.setContentsMargins(0, 0, 0, 0)
        stats_row_layout.setSpacing(16)

        self.count_label = QLabel("0 credentials stored")
        self.count_label.setStyleSheet("color: #94a3b8; font-size: 12px;")
        stats_row_layout.addWidget(self.count_label)

        self.types_label = QLabel("Types: password, ssh_key, api_key, qmp_pass")
        self.types_label.setStyleSheet("color: #64748b; font-size: 11px;")
        stats_row_layout.addWidget(self.types_label)

        stats_row_layout.addStretch()
        stats_card.content_layout.addWidget(stats_row)
        stats_card.content_layout.addStretch()

        # ── Split View: List + Detail ──────────────────────────────────────────
        splitter = QSplitter(Qt.Horizontal, self)
        splitter.setStyleSheet("background: #0f172a;")

        # Credential list
        list_card = Card("Stored Credentials")
        list_inner = QWidget()
        list_inner_layout = QVBoxLayout(list_inner)
        list_inner_layout.setContentsMargins(0, 0, 0, 0)
        list_inner_layout.setSpacing(0)

        self.cred_tree = QTreeWidget()
        self.cred_tree.setHeaderLabels(["Name", "Type", "Description", "Updated"])
        self.cred_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.cred_tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.cred_tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.cred_tree.setAlternatingRowColors(True)
        self.cred_tree.setAnimated(True)
        self.cred_tree.setStyleSheet("""
            QTreeWidget {
                background: #0f172a;
                color: #e2e8f0;
                border: 1px solid #334155;
                border-radius: 4px;
                font-size: 12px;
                padding: 4px;
            }
            QTreeWidget::item {
                padding: 4px 6px;
            }
            QTreeWidget::item:selected { background: #1e3a5f; color: #60a5fa; }
            QTreeWidget::item:hover { background: #1e293b; }
            QHeaderView::section {
                background: #1e293b;
                color: #94a3b8;
                border: 1px solid #334155;
                padding: 4px;
                font-size: 11px;
                font-weight: bold;
            }
        """)
        self.cred_tree.itemDoubleClicked.connect(self._on_credential_click)
        list_inner_layout.addWidget(self.cred_tree)
        list_card.content_layout.addWidget(list_inner)
        splitter.addWidget(list_card)

        # Detail / action panel
        detail_card = Card("Credential Details")
        detail_inner = QWidget()
        detail_inner_layout = QVBoxLayout(detail_inner)
        detail_inner_layout.setContentsMargins(0, 0, 0, 0)
        detail_inner_layout.setSpacing(8)

        self.detail_name = QLabel("Select a credential to view details")
        self.detail_name.setStyleSheet("color: #64748b; font-size: 12px; font-style: italic;")
        self.detail_name.setWordWrap(True)
        detail_inner_layout.addWidget(self.detail_name)

        self.detail_type_label = QLabel("Type: —")
        self.detail_type_label.setStyleSheet("color: #94a3b8; font-size: 11px;")
        detail_inner_layout.addWidget(self.detail_type_label)

        self.detail_desc_label = QLabel("Description: —")
        self.detail_desc_label.setStyleSheet("color: #94a3b8; font-size: 11px;")
        self.detail_desc_label.setWordWrap(True)
        detail_inner_layout.addWidget(self.detail_desc_label)

        self.detail_updated_label = QLabel("Updated: —")
        self.detail_updated_label.setStyleSheet("color: #64748b; font-size: 11px;")
        detail_inner_layout.addWidget(self.detail_updated_label)

        # Value display (masked)
        self.detail_value_label = QLabel("Value: [redacted — use Edit to view]")
        self.detail_value_label.setStyleSheet("color: #ef4444; font-size: 11px; font-family: monospace;")
        detail_inner_layout.addWidget(self.detail_value_label)

        detail_inner_layout.addStretch()

        # Action buttons
        detail_actions = QWidget()
        detail_actions_layout = QHBoxLayout(detail_actions)
        detail_actions_layout.setContentsMargins(0, 0, 0, 0)
        detail_actions_layout.setSpacing(8)

        self.edit_btn = QPushButton("✏ Edit")
        self.edit_btn.setFixedHeight(28)
        self.edit_btn.setStyleSheet("""
            QPushButton {
                background: #f59e0b;
                color: white;
                border: none;
                border-radius: 4px;
                font-size: 11px;
                padding: 0 10px;
            }
            QPushButton:hover { background: #d97706; }
        """)
        detail_actions_layout.addWidget(self.edit_btn)

        self.delete_btn = QPushButton("🗑 Delete")
        self.delete_btn.setFixedHeight(28)
        self.delete_btn.setStyleSheet("""
            QPushButton {
                background: #ef4444;
                color: white;
                border: none;
                border-radius: 4px;
                font-size: 11px;
                padding: 0 10px;
            }
            QPushButton:hover { background: #dc2626; }
        """)
        detail_actions_layout.addWidget(self.delete_btn)

        detail_actions_layout.addStretch()

        self.copy_value_btn = QPushButton("📋 Copy Value (masked)")
        self.copy_value_btn.setFixedHeight(28)
        self.copy_value_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #64748b;
                border: 1px solid #334155;
                border-radius: 4px;
                font-size: 11px;
                padding: 0 10px;
            }
            QPushButton:hover { color: #94a3b8; border-color: #475569; }
        """)
        detail_actions_layout.addWidget(self.copy_value_btn)

        detail_inner_layout.addWidget(detail_actions)
        detail_card.content_layout.addWidget(detail_inner)
        splitter.addWidget(detail_card)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)

        # ── Add Credential Button ──────────────────────────────────────────────
        add_card = Card("")
        add_card.setFixedHeight(50)
        layout.addWidget(add_card)

        add_row = QWidget()
        add_row_layout = QHBoxLayout(add_row)
        add_row_layout.setContentsMargins(0, 0, 0, 0)
        add_row_layout.setSpacing(12)

        self.add_btn = QPushButton("＋ Add Credential")
        self.add_btn.setFixedHeight(32)
        self.add_btn.setStyleSheet("""
            QPushButton {
                background: #3b82f6;
                color: white;
                border: none;
                border-radius: 4px;
                font-size: 13px;
                font-weight: 600;
                padding: 0 20px;
            }
            QPushButton:hover { background: #2563eb; }
        """)
        add_row_layout.addWidget(self.add_btn)

        search_input = QLineEdit()
        search_input.setPlaceholderText("Search credentials...")
        search_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        search_input.setStyleSheet("""
            QLineEdit {
                background: #0f172a;
                border: 1px solid #334155;
                border-radius: 4px;
                color: #e2e8f0;
                padding: 6px 10px;
                font-size: 12px;
            }
            QLineEdit:focus { border-color: #3b82f6; }
        """)
        add_row_layout.addWidget(search_input)

        add_row_layout.addStretch()

        clear_all_btn = QPushButton("Clear All")
        clear_all_btn.setFixedHeight(28)
        clear_all_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #ef4444;
                border: none;
                font-size: 11px;
            }
            QPushButton:hover { color: #dc2626; }
        """)
        add_row_layout.addWidget(clear_all_btn)

        add_card.content_layout.addWidget(add_row)
        add_card.content_layout.addStretch()

        # Connections
        self.add_btn.clicked.connect(self._add_credential)
        self.edit_btn.clicked.connect(self._edit_credential)
        self.delete_btn.clicked.connect(self._delete_credential)
        clear_all_btn.clicked.connect(self._clear_all)

        # Timers
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_list)
        self._refresh_timer.start(5000)

        self._selected_cred_id: str | None = None
        self._refresh_list()

    def _refresh_list(self):
        """Refresh the credential list from the store."""
        store = CredentialStore()
        self.cred_tree.clear()

        for entry in store.list_all():
            item = CredentialTreeItem(entry.id, entry.name, entry.credential_type, entry.description)
            item.setText(3, entry.updated[:19] if entry.updated else "—")
            item.setData(0, Qt.UserRole, entry.id)
            # Color the type column
            type_colors = {
                "password": "#f59e0b",
                "ssh_key": "#8b5cf6",
                "api_key": "#22c55e",
                "qmp_pass": "#3b82f6",
                "other": "#94a3b8",
            }
            color = QColor(type_colors.get(entry.credential_type, "#94a3b8"))
            item.setForeground(1, QBrush(color))
            self.cred_tree.addTopLevelItem(item)

        self.count_label.setText(f"{store.count} credentials stored")

        if self._selected_cred_id:
            self._show_detail(self._selected_cred_id)

    def _show_detail(self, cred_id: str):
        """Show details of a selected credential."""
        store = CredentialStore()
        entry = store.get(cred_id)
        if not entry:
            return

        self._selected_cred_id = cred_id
        self.detail_name.setText(entry.name)
        self.detail_name.setStyleSheet("color: #e2e8f0; font-size: 13px; font-weight: 600;")
        self.detail_type_label.setText(f"Type: {entry.credential_type}")
        self.detail_desc_label.setText(f"Description: {entry.description or '—'}")
        self.detail_updated_label.setText(f"Updated: {entry.updated[:19] if entry.updated else '—'}")

        # Show masked value
        masked = self._mask_value(entry.value)
        self.detail_value_label.setText(f"Value: {masked}")
        self.detail_value_label.setStyleSheet("color: #f59e0b; font-size: 11px; font-family: monospace;")

    def _mask_value(self, value: str) -> str:
        """Mask a secret value for display."""
        if len(value) <= 4:
            return "***"
        return value[:2] + "***" + value[-2:]

    def _on_credential_click(self, item: QTreeWidgetItem, column: int):
        """Handle double-click on a credential."""
        cred_id = item.data(0, Qt.UserRole)
        if cred_id:
            self._show_detail(cred_id)

    def _add_credential(self):
        """Open dialog to add a new credential."""
        dialog = CredentialDialog(self)
        if dialog.exec_():
            self._refresh_list()

    def _edit_credential(self):
        """Open dialog to edit the selected credential."""
        if not self._selected_cred_id:
            QMessageBox.information(self, "No Selection", "Select a credential from the list first.")
            return
        dialog = CredentialDialog(self, self._selected_cred_id)
        if dialog.exec_():
            self._refresh_list()

    def _delete_credential(self):
        """Delete the selected credential."""
        if not self._selected_cred_id:
            QMessageBox.information(self, "No Selection", "Select a credential from the list first.")
            return

        resp = QMessageBox.question(
            self, "Confirm Delete",
            f"Delete credential '{self.detail_name.text()}'?\n\nThis action cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if resp == QMessageBox.Yes:
            store = CredentialStore()
            try:
                store.delete(self._selected_cred_id)
                self._refresh_list()
                self.detail_name.setText("Select a credential to view details")
                self.detail_name.setStyleSheet("color: #64748b; font-size: 12px; font-style: italic;")
                self.detail_type_label.setText("Type: —")
                self.detail_desc_label.setText("Description: —")
                self.detail_updated_label.setText("Updated: —")
                self.detail_value_label.setText("Value: [redacted]")
                QMessageBox.information(self, "Deleted", "Credential deleted.")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to delete: {e}")

    def _clear_all(self):
        """Clear all credentials."""
        resp = QMessageBox.question(
            self, "Confirm Clear All",
            "Remove ALL stored credentials?\n\nThis action cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if resp == QMessageBox.Yes:
            store = CredentialStore()
            store.clear()
            self._refresh_list()
            self.detail_name.setText("Select a credential to view details")
            self.detail_name.setStyleSheet("color: #64748b; font-size: 12px; font-style: italic;")
            self.detail_type_label.setText("Type: —")
            self.detail_desc_label.setText("Description: —")
            self.detail_updated_label.setText("Updated: —")
            self.detail_value_label.setText("Value: [redacted]")
            QMessageBox.information(self, "Cleared", "All credentials cleared.")
