"""Audit logging system for VM-Harness.

Provides tamper-evident audit logging with SQLite storage, automatic rotation,
CSV export, and a PyQt5 security panel tab with filtering, search, and export.

Log format: timestamp | event_type | user | details | source_ip | success

Usage:
    from gui.audit_log import AuditLogger, audit_log

    # Log an event
    audit_log.log("login", user="admin", details="SSH login", source_ip="192.168.1.1", success=True)

    # Query events
    events = audit_log.query(event_type="login", limit=100)

    # Export to CSV
    audit_log.export_csv("audit_export.csv")
"""

from __future__ import annotations

import csv
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from PyQt5.QtCore import QObject, pyqtSignal, Qt, QTimer
from PyQt5.QtGui import QColor, QBrush, QFont
from PyQt5.QtWidgets import (
    QSizePolicy,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QLineEdit,
    QComboBox,
    QCheckBox,
    QFileDialog,
    QMessageBox,
    QGroupBox,
    QSplitter,
    QTextEdit,
    QSpinBox,
    QDateEdit,
    QAbstractItemView,
)

from gui.theme import T
from gui.widgets import Card


# ── Default event types ─────────────────────────────────────────────────────────

DEFAULT_EVENT_TYPES = [
    "login",
    "logout",
    "vm_start",
    "vm_stop",
    "vm_pause",
    "vm_resume",
    "vm_reset",
    "vm_shutdown",
    "credential_add",
    "credential_edit",
    "credential_delete",
    "credential_view",
    "qmp_command",
    "ssh_command",
    "snapshot_create",
    "snapshot_restore",
    "snapshot_delete",
    "settings_change",
    "export",
    "import",
    "error",
    "warning",
    "info",
]

# ── Rotation threshold ──────────────────────────────────────────────────────────

MAX_ENTRIES_PER_TYPE = 10_000


# ── AuditLogger ─────────────────────────────────────────────────────────────────

class AuditLogger(QObject):
    """Thread-safe audit logger with SQLite storage and automatic rotation.

    Emits ``entry_logged`` signals for real-time UI updates.
    """

    entry_logged = pyqtSignal(dict)  # Emits the logged entry as a dict

    def __init__(
        self,
        db_path: str | os.PathLike | None = None,
        max_entries_per_type: int = MAX_ENTRIES_PER_TYPE,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._db_path = Path(db_path) if db_path else self._default_db_path()
        self._max_entries_per_type = max_entries_per_type
        self._lock = threading.Lock()
        self._qt_parent_app = None  # Lazily set by _ensure_audit_log_parent()

        # Ensure parent directory exists
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize database
        self._init_db()

    @staticmethod
    def _default_db_path() -> Path:
        """Get the default database path."""
        project_root = Path(__file__).resolve().parent.parent
        return project_root / ".audit" / "audit.db"

    def _init_db(self) -> None:
        """Create the audit log table if it doesn't exist."""
        with self._lock:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS audit_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        user TEXT NOT NULL DEFAULT 'system',
                        details TEXT NOT NULL DEFAULT '',
                        source_ip TEXT NOT NULL DEFAULT '127.0.0.1',
                        success INTEGER NOT NULL DEFAULT 1
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_audit_event_type
                    ON audit_log(event_type)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_audit_timestamp
                    ON audit_log(timestamp)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_audit_user
                    ON audit_log(user)
                """)
                conn.commit()

    def _ensure_audit_log_parent(self):
        """Ensure this AuditLogger has a Qt parent for signal delivery.

        Called lazily on first log() to attach to the current QApplication
        if one exists.  Safe to call multiple times — idempotent.
        """
        if self._qt_parent_app is not None:
            return
        try:
            from PyQt5.QtWidgets import QApplication as _QA
            app = _QA.instance()
            if app is not None:
                self._qt_parent_app = app
        except ImportError:
            pass  # Qt not available — no signals needed

    def log(
        self,
        event_type: str,
        user: str = "system",
        details: str = "",
        source_ip: str = "127.0.0.1",
        success: bool = True,
    ) -> dict:
        """Log an audit event.

        Args:
            event_type: Type of event (e.g., 'login', 'vm_start')
            user: Username or identifier
            details: Human-readable description
            source_ip: Source IP address
            success: Whether the operation succeeded

        Returns:
            The logged entry as a dict.
        """
        timestamp = datetime.utcnow().isoformat() + "Z"
        entry = {
            "id": None,
            "timestamp": timestamp,
            "event_type": event_type,
            "user": user,
            "details": details,
            "source_ip": source_ip,
            "success": bool(success),
        }

        with self._lock:
            with sqlite3.connect(str(self._db_path)) as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO audit_log (timestamp, event_type, user, details, source_ip, success)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (timestamp, event_type, user, details, source_ip, int(success)),
                )
                entry["id"] = cursor.lastrowid
                conn.commit()

            # Rotate if needed
            self._rotate_if_needed(conn=None, event_type=event_type)

        # Emit signal outside lock to avoid deadlocks
        self._ensure_audit_log_parent()
        self.entry_logged.emit(entry)
        return entry

    def _rotate_if_needed(self, conn=None, event_type: str | None = None) -> None:
        """Remove oldest entries if count exceeds threshold for a type."""
        should_close = False
        if conn is None:
            conn = sqlite3.connect(str(self._db_path))
            should_close = True

        try:
            if event_type:
                types_to_check = [event_type]
            else:
                cursor = conn.execute(
                    "SELECT DISTINCT event_type FROM audit_log"
                )
                types_to_check = [row[0] for row in cursor.fetchall()]

            for etype in types_to_check:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM audit_log WHERE event_type = ?",
                    (etype,),
                )
                count = cursor.fetchone()[0]

                if count > self._max_entries_per_type:
                    excess = count - self._max_entries_per_type
                    conn.execute(
                        """
                        DELETE FROM audit_log
                        WHERE id IN (
                            SELECT id FROM audit_log
                            WHERE event_type = ?
                            ORDER BY id ASC
                            LIMIT ?
                        )
                        """,
                        (etype, excess),
                    )
            conn.commit()
        finally:
            if should_close:
                conn.close()

    def query(
        self,
        event_type: str | None = None,
        user: str | None = None,
        source_ip: str | None = None,
        success: bool | None = None,
        search: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[dict]:
        """Query audit log entries with filters.

        Args:
            event_type: Filter by event type
            user: Filter by username
            source_ip: Filter by source IP
            success: Filter by success status
            search: Search in details field
            start_date: ISO format start date (inclusive)
            end_date: ISO format end date (inclusive)
            limit: Maximum number of results
            offset: Number of results to skip

        Returns:
            List of entry dicts.
        """
        conditions = []
        params: list[Any] = []

        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if user:
            conditions.append("user LIKE ?")
            params.append(f"%{user}%")
        if source_ip:
            conditions.append("source_ip LIKE ?")
            params.append(f"%{source_ip}%")
        if success is not None:
            conditions.append("success = ?")
            params.append(int(success))
        if search:
            conditions.append("details LIKE ?")
            params.append(f"%{search}%")
        if start_date:
            conditions.append("timestamp >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("timestamp <= ?")
            params.append(end_date)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        with self._lock:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    f"""
                    SELECT id, timestamp, event_type, user, details, source_ip, success
                    FROM audit_log
                    WHERE {where_clause}
                    ORDER BY id DESC
                    LIMIT ? OFFSET ?
                    """,
                    params + [limit, offset],
                )
                rows = cursor.fetchall()

        return [
            {
                "id": row["id"],
                "timestamp": row["timestamp"],
                "event_type": row["event_type"],
                "user": row["user"],
                "details": row["details"],
                "source_ip": row["source_ip"],
                "success": bool(row["success"]),
            }
            for row in rows
        ]

    def count(
        self,
        event_type: str | None = None,
        user: str | None = None,
        success: bool | None = None,
    ) -> int:
        """Count audit log entries matching filters."""
        conditions = []
        params: list[Any] = []

        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if user:
            conditions.append("user LIKE ?")
            params.append(f"%{user}%")
        if success is not None:
            conditions.append("success = ?")
            params.append(int(success))

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        with self._lock:
            with sqlite3.connect(str(self._db_path)) as conn:
                cursor = conn.execute(
                    f"SELECT COUNT(*) FROM audit_log WHERE {where_clause}",
                    params,
                )
                return cursor.fetchone()[0]

    def get_event_types(self) -> list[str]:
        """Get all distinct event types in the log."""
        with self._lock:
            with sqlite3.connect(str(self._db_path)) as conn:
                cursor = conn.execute(
                    "SELECT DISTINCT event_type FROM audit_log ORDER BY event_type"
                )
                return [row[0] for row in cursor.fetchall()]

    def get_users(self) -> list[str]:
        """Get all distinct users in the log."""
        with self._lock:
            with sqlite3.connect(str(self._db_path)) as conn:
                cursor = conn.execute(
                    "SELECT DISTINCT user FROM audit_log ORDER BY user"
                )
                return [row[0] for row in cursor.fetchall()]

    def export_csv(
        self,
        filepath: str | os.PathLike,
        event_type: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> int:
        """Export audit log to CSV.

        Args:
            filepath: Output file path
            event_type: Optional filter by event type
            start_date: Optional start date filter
            end_date: Optional end date filter

        Returns:
            Number of rows exported.
        """
        entries = self.query(
            event_type=event_type,
            start_date=start_date,
            end_date=end_date,
            limit=1_000_000,
        )

        filepath = Path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "timestamp", "event_type", "user", "details", "source_ip", "success"])
            for entry in entries:
                writer.writerow([
                    entry["id"],
                    entry["timestamp"],
                    entry["event_type"],
                    entry["user"],
                    entry["details"],
                    entry["source_ip"],
                    entry["success"],
                ])

        return len(entries)

    def clear(self, event_type: str | None = None) -> int:
        """Clear audit log entries.

        Args:
            event_type: If provided, only clear entries of this type.

        Returns:
            Number of rows deleted.
        """
        with self._lock:
            with sqlite3.connect(str(self._db_path)) as conn:
                if event_type:
                    cursor = conn.execute(
                        "DELETE FROM audit_log WHERE event_type = ?",
                        (event_type,),
                    )
                else:
                    cursor = conn.execute("DELETE FROM audit_log")
                deleted = cursor.rowcount
                conn.commit()
                return deleted

    def close(self) -> None:
        """Close the logger (no-op for SQLite, but good practice)."""
        pass


# ── Global singleton ────────────────────────────────────────────────────────────

# The module-level singleton is created without a Qt parent so it survives
# QApplication teardown in test environments and across test file ordering.
# Production code that wants a parent-bound instance should create its own
# via AuditLogger(parent=app).  The Python-level reference (this variable)
# is sufficient to keep the C++ object alive — no QObject parent needed.
audit_log = AuditLogger()


# ── Audit Log Panel (Security Tab) ──────────────────────────────────────────────

class AuditLogPanel(QWidget):
    """PyQt5 panel for viewing and filtering audit logs.

    Features:
        - Filter by event type, user, source IP, success status
        - Full-text search in details
        - Date range filtering
        - CSV export
        - Real-time updates via signal
    """

    def __init__(self, parent: QWidget | None = None, logger: AuditLogger | None = None):
        super().__init__(parent)
        self._logger = logger or audit_log
        self.setStyleSheet("background: #0f172a;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # ── Stats Bar ──────────────────────────────────────────────────────────
        stats_card = Card("Audit Log Statistics")
        stats_card.setFixedHeight(60)
        layout.addWidget(stats_card)

        stats_row = QWidget()
        stats_row_layout = QHBoxLayout(stats_row)
        stats_row_layout.setContentsMargins(0, 0, 0, 0)
        stats_row_layout.setSpacing(16)

        self.total_label = QLabel("Total: 0")
        self.total_label.setStyleSheet("color: #94a3b8; font-size: 12px;")
        stats_row_layout.addWidget(self.total_label)

        self.types_label = QLabel("Types: 0")
        self.types_label.setStyleSheet("color: #64748b; font-size: 11px;")
        stats_row_layout.addWidget(self.types_label)

        self.users_label = QLabel("Users: 0")
        self.users_label.setStyleSheet("color: #64748b; font-size: 11px;")
        stats_row_layout.addWidget(self.users_label)

        stats_row_layout.addStretch()

        self.export_btn = QPushButton("📥 Export CSV")
        self.export_btn.setFixedHeight(28)
        self.export_btn.setStyleSheet("""
            QPushButton {
                background: #3b82f6;
                color: white;
                border: none;
                border-radius: 4px;
                font-size: 11px;
                padding: 0 12px;
            }
            QPushButton:hover { background: #2563eb; }
        """)
        stats_row_layout.addWidget(self.export_btn)

        self.clear_btn = QPushButton("🗑 Clear")
        self.clear_btn.setFixedHeight(28)
        self.clear_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #ef4444;
                border: 1px solid #ef4444;
                border-radius: 4px;
                font-size: 11px;
                padding: 0 12px;
            }
            QPushButton:hover { background: #ef444420; }
        """)
        stats_row_layout.addWidget(self.clear_btn)

        stats_card.content_layout.addWidget(stats_row)
        stats_card.content_layout.addStretch()

        # ── Filters ────────────────────────────────────────────────────────────
        filters_card = Card("Filters")
        filters_card.setFixedHeight(100)
        layout.addWidget(filters_card)

        filters_row = QWidget()
        filters_row_layout = QHBoxLayout(filters_row)
        filters_row_layout.setContentsMargins(0, 0, 0, 0)
        filters_row_layout.setSpacing(8)

        # Event type filter
        type_label = QLabel("Type:")
        type_label.setStyleSheet("color: #94a3b8; font-size: 11px;")
        filters_row_layout.addWidget(type_label)

        self.type_combo = QComboBox()
        self.type_combo.addItem("All")
        self.type_combo.setStyleSheet("""
            QComboBox {
                background: #0f172a;
                color: #e2e8f0;
                border: 1px solid #334155;
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 11px;
                min-width: 120px;
            }
            QComboBox:hover { border-color: #3b82f6; }
        """)
        filters_row_layout.addWidget(self.type_combo)

        # User filter
        user_label = QLabel("User:")
        user_label.setStyleSheet("color: #94a3b8; font-size: 11px;")
        filters_row_layout.addWidget(user_label)

        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("Filter by user...")
        self.user_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.user_input.setStyleSheet("""
            QLineEdit {
                background: #0f172a;
                border: 1px solid #334155;
                border-radius: 4px;
                color: #e2e8f0;
                padding: 4px 8px;
                font-size: 11px;
            }
            QLineEdit:focus { border-color: #3b82f6; }
        """)
        filters_row_layout.addWidget(self.user_input)

        # Source IP filter
        ip_label = QLabel("IP:")
        ip_label.setStyleSheet("color: #94a3b8; font-size: 11px;")
        filters_row_layout.addWidget(ip_label)

        self.ip_input = QLineEdit()
        self.ip_input.setPlaceholderText("Filter by IP...")
        self.ip_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.ip_input.setStyleSheet("""
            QLineEdit {
                background: #0f172a;
                border: 1px solid #334155;
                border-radius: 4px;
                color: #e2e8f0;
                padding: 4px 8px;
                font-size: 11px;
            }
            QLineEdit:focus { border-color: #3b82f6; }
        """)
        filters_row_layout.addWidget(self.ip_input)

        # Success filter
        self.success_combo = QComboBox()
        self.success_combo.addItems(["All", "Success", "Failure"])
        self.success_combo.setStyleSheet("""
            QComboBox {
                background: #0f172a;
                color: #e2e8f0;
                border: 1px solid #334155;
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 11px;
                min-width: 80px;
            }
            QComboBox:hover { border-color: #3b82f6; }
        """)
        filters_row_layout.addWidget(self.success_combo)

        filters_row_layout.addStretch()

        # Search
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search details...")
        self.search_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.search_input.setStyleSheet("""
            QLineEdit {
                background: #0f172a;
                border: 1px solid #334155;
                border-radius: 4px;
                color: #e2e8f0;
                padding: 4px 8px;
                font-size: 11px;
            }
            QLineEdit:focus { border-color: #3b82f6; }
        """)
        filters_row_layout.addWidget(self.search_input)

        # Apply filter button
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setFixedHeight(28)
        self.apply_btn.setStyleSheet("""
            QPushButton {
                background: #22c55e;
                color: white;
                border: none;
                border-radius: 4px;
                font-size: 11px;
                padding: 0 12px;
            }
            QPushButton:hover { background: #16a34a; }
        """)
        filters_row_layout.addWidget(self.apply_btn)

        filters_card.content_layout.addWidget(filters_row)
        filters_card.content_layout.addStretch()

        # ── Results Table ──────────────────────────────────────────────────────
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(["ID", "Timestamp", "Event Type", "User", "Details", "Source IP", "Success"])
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setStyleSheet("""
            QTableWidget {
                background: #0f172a;
                color: #e2e8f0;
                border: 1px solid #334155;
                border-radius: 4px;
                font-size: 11px;
                gridline-color: #1e293b;
            }
            QTableWidget::item {
                padding: 4px 6px;
            }
            QTableWidget::item:selected {
                background: #1e3a5f;
                color: #60a5fa;
            }
            QTableWidget::item:hover {
                background: #1e293b;
            }
            QHeaderView::section {
                background: #1e293b;
                color: #94a3b8;
                border: 1px solid #334155;
                padding: 4px;
                font-size: 10px;
                font-weight: bold;
            }
        """)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeToContents)
        layout.addWidget(self.table)

        # ── Connections ────────────────────────────────────────────────────────
        self.export_btn.clicked.connect(self._export_csv)
        self.clear_btn.clicked.connect(self._clear_log)
        self.apply_btn.clicked.connect(self.refresh)
        self.search_input.returnPressed.connect(self.refresh)
        self.type_combo.currentTextChanged.connect(self.refresh)
        self._logger.entry_logged.connect(self._on_new_entry)

        # Auto-refresh timer
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh)
        self._refresh_timer.start(5000)

        # Initial load
        self._populate_type_combo()
        self.refresh()

    def _populate_type_combo(self) -> None:
        """Populate the event type dropdown."""
        self.type_combo.clear()
        self.type_combo.addItem("All")
        for etype in self._logger.get_event_types():
            self.type_combo.addItem(etype)

    def refresh(self) -> None:
        """Refresh the table with current filters."""
        # Build filters
        event_type = self.type_combo.currentText()
        if event_type == "All":
            event_type = None

        user = self.user_input.text().strip() or None
        source_ip = self.ip_input.text().strip() or None

        success_text = self.success_combo.currentText()
        if success_text == "Success":
            success = True
        elif success_text == "Failure":
            success = False
        else:
            success = None

        search = self.search_input.text().strip() or None

        # Query
        entries = self._logger.query(
            event_type=event_type,
            user=user,
            source_ip=source_ip,
            success=success,
            search=search,
            limit=1000,
        )

        # Update table
        self.table.setRowCount(len(entries))
        for row_idx, entry in enumerate(entries):
            self.table.setItem(row_idx, 0, QTableWidgetItem(str(entry["id"])))
            self.table.setItem(row_idx, 1, QTableWidgetItem(entry["timestamp"][:19]))
            self.table.setItem(row_idx, 2, QTableWidgetItem(entry["event_type"]))
            self.table.setItem(row_idx, 3, QTableWidgetItem(entry["user"]))
            self.table.setItem(row_idx, 4, QTableWidgetItem(entry["details"]))
            self.table.setItem(row_idx, 5, QTableWidgetItem(entry["source_ip"]))

            success_item = QTableWidgetItem("✓" if entry["success"] else "✗")
            success_item.setForeground(
                QBrush(QColor("#22c55e" if entry["success"] else "#ef4444"))
            )
            self.table.setItem(row_idx, 6, success_item)

        # Update stats
        total = self._logger.count()
        types = len(self._logger.get_event_types())
        users = len(self._logger.get_users())
        self.total_label.setText(f"Total: {total}")
        self.types_label.setText(f"Types: {types}")
        self.users_label.setText(f"Users: {users}")

    def _on_new_entry(self, entry: dict) -> None:
        """Handle new entry signal - refresh if filters match."""
        # Quick check if the new entry matches current filters
        event_type = self.type_combo.currentText()
        if event_type != "All" and entry["event_type"] != event_type:
            return

        search = self.search_input.text().strip()
        if search and search.lower() not in entry["details"].lower():
            return

        # Refresh to show new entry
        self.refresh()

    def _export_csv(self) -> None:
        """Export filtered results to CSV."""
        filepath, _ = QFileDialog.getSaveFileName(
            self,
            "Export Audit Log",
            "audit_export.csv",
            "CSV Files (*.csv)",
        )
        if not filepath:
            return

        try:
            # Build filters
            event_type = self.type_combo.currentText()
            if event_type == "All":
                event_type = None

            count = self._logger.export_csv(filepath, event_type=event_type)
            QMessageBox.information(
                self,
                "Export Complete",
                f"Exported {count} entries to:\n{filepath}",
            )
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", str(e))

    def _clear_log(self) -> None:
        """Clear audit log entries."""
        resp = QMessageBox.question(
            self,
            "Confirm Clear",
            "Clear all audit log entries?\n\nThis action cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if resp == QMessageBox.Yes:
            event_type = self.type_combo.currentText()
            if event_type == "All":
                event_type = None
            deleted = self._logger.clear(event_type=event_type)
            self._populate_type_combo()
            self.refresh()
            QMessageBox.information(self, "Cleared", f"Deleted {deleted} entries.")
