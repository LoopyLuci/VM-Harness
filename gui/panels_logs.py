"""Logs panel — application, QMP, and SSH log viewers.

Tabbed interface with timestamped, color-coded log entries.
Filter by level, clear, export to file, and auto-scroll support.
"""

from __future__ import annotations

from gui.theme import T
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QCheckBox,
    QComboBox,
    QTextBrowser,
    QMessageBox,
    QFileDialog,
    QSizePolicy, QTextEdit,
)

from gui.widgets import Card, LogEntry, TextInput


class LogsPanel(QWidget):
    """Tabbed log viewer for application, QMP, and SSH logs."""

    LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    LEVEL_COLORS = {
        "DEBUG": "#64748b",
        "INFO": "#38bdf8",
        "WARNING": "#f59e0b",
        "ERROR": "#ef4444",
        "CRITICAL": "#dc2626",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: #0f172a;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # ── Toolbar ────────────────────────────────────────────────────────────
        toolbar = QWidget()
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(8)

        # Log source selector
        log_src_label = QLabel("Log Source:")
        log_src_label.setStyleSheet("color: #cbd5e1; font-size: 12px;")
        log_src_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        toolbar_layout.addWidget(log_src_label)

        self.log_source_combo = QComboBox()
        self.log_source_combo.addItems(["Application", "QMP", "SSH"])
        self.log_source_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.log_source_combo.setStyleSheet("""
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
        toolbar_layout.addWidget(self.log_source_combo)

        toolbar_layout.addStretch()

        # Level filter
        level_label = QLabel("Show levels:")
        level_label.setStyleSheet("color: #cbd5e1; font-size: 12px;")
        toolbar_layout.addWidget(level_label)

        self.level_filters = {}
        for level in self.LEVELS:
            cb = QCheckBox(level)
            cb.setStyleSheet("color: #94a3b8; font-size: 11px;")
            cb.setChecked(True)
            cb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.level_filters[level] = cb
            toolbar_layout.addWidget(cb)

        toolbar_layout.addStretch()

        # Actions
        clear_btn = QPushButton("Clear")
        clear_btn.setFixedHeight(28)
        clear_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #ef4444;
                border: none;
                font-size: 11px;
            }
            QPushButton:hover { color: #dc2626; }
        """)
        clear_btn.clicked.connect(self._clear_logs)
        toolbar_layout.addWidget(clear_btn)

        export_btn = QPushButton("Export")
        export_btn.setFixedHeight(28)
        export_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #64748b;
                border: none;
                font-size: 11px;
            }
            QPushButton:hover { color: #94a3b8; }
        """)
        export_btn.clicked.connect(self._export_logs)
        toolbar_layout.addWidget(export_btn)

        layout.addWidget(toolbar)

        # ── Log Tabs ───────────────────────────────────────────────────────────
        self.log_tabs = QTabWidget()
        self.log_tabs.setStyleSheet("""
            QTabWidget::pane {
                background: #0f172a;
                border: 1px solid #334155;
                border-radius: 8px;
            }
            QTabBar::tab {
                background: #1e293b;
                color: #64748b;
                border: 1px solid #334155;
                border-bottom: none;
                padding: 6px 14px;
                font-size: 12px;
                min-width: 70px;
            }
            QTabBar::tab:selected {
                background: #1e3a5f;
                color: #60a5fa;
                border-color: #3b82f6;
            }
        """)
        layout.addWidget(self.log_tabs)

        # Tab 1: Application Log
        app_log_tab = QWidget()
        app_log_tab.setStyleSheet("background: #0f172a;")
        app_log_layout = QVBoxLayout(app_log_tab)
        app_log_layout.setContentsMargins(0, 0, 0, 0)
        app_log_layout.setSpacing(0)

        self.app_log_view = QTextBrowser()
        self.app_log_view.setReadOnly(True)
        self.app_log_view.setStyleSheet("""
            QTextBrowser {
                background: #0f172a;
                color: #e2e8f0;
                border: none;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 12px;
                padding: 8px;
            }
        """)
        app_log_layout.addWidget(self.app_log_view)
        self.log_tabs.addTab(app_log_tab, "Application")

        # Tab 2: QMP Log
        qmp_log_tab = QWidget()
        qmp_log_tab.setStyleSheet("background: #0f172a;")
        qmp_log_layout = QVBoxLayout(qmp_log_tab)
        qmp_log_layout.setContentsMargins(0, 0, 0, 0)
        qmp_log_layout.setSpacing(0)

        self.qmp_log_view = QTextBrowser()
        self.qmp_log_view.setReadOnly(True)
        self.qmp_log_view.setStyleSheet("""
            QTextBrowser {
                background: #0f172a;
                color: #e2e8f0;
                border: none;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 12px;
                padding: 8px;
            }
        """)
        qmp_log_layout.addWidget(self.qmp_log_view)
        self.log_tabs.addTab(qmp_log_tab, "QMP")

        # Tab 3: SSH Log
        ssh_log_tab = QWidget()
        ssh_log_tab.setStyleSheet("background: #0f172a;")
        ssh_log_layout = QVBoxLayout(ssh_log_tab)
        ssh_log_layout.setContentsMargins(0, 0, 0, 0)
        ssh_log_layout.setSpacing(0)

        self.ssh_log_view = QTextBrowser()
        self.ssh_log_view.setReadOnly(True)
        self.ssh_log_view.setStyleSheet("""
            QTextBrowser {
                background: #0f172a;
                color: #e2e8f0;
                border: none;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 12px;
                padding: 8px;
            }
        """)
        ssh_log_layout.addWidget(self.ssh_log_view)
        self.log_tabs.addTab(ssh_log_tab, "SSH")

        # ── Status Bar ─────────────────────────────────────────────────────────
        self.status_label = QLabel("Showing Application log — 0 entries")
        self.status_label.setStyleSheet("color: #64748b; font-size: 11px;")
        layout.addWidget(self.status_label)

        # Seed with sample logs
        self._seed_sample_logs()

    def _seed_sample_logs(self):
        """Add sample log entries for demonstration."""
        sample_app = [
            ("INFO", "VM-Harness GUI started"),
            ("INFO", "Loading configuration from .env"),
            ("INFO", "QMP client initialized (127.0.0.1:4444)"),
            ("INFO", "SSH client initialized (127.0.0.1:2222)"),
            ("INFO", "Credential store loaded — 0 entries"),
            ("DEBUG", "Settings: VM_RAM_MB=16384, VM_CPUS=8"),
            ("INFO", "All systems operational"),
            ("INFO", "Dashboard panel loaded"),
            ("INFO", "VM Control panel loaded"),
            ("INFO", "Guest Terminal panel loaded"),
        ]

        sample_qmp = [
            ("INFO", "QMP connection established"),
            ("INFO", "QMP capabilities registered"),
            ("DEBUG", "Querying VM status..."),
            ("INFO", "VM status: stopped"),
            ("DEBUG", "QMCP command: query-status"),
            ("DEBUG", "QMCP command: query-chardev"),
        ]

        sample_ssh = [
            ("INFO", "SSH connection initialized"),
            ("DEBUG", "Loading host keys..."),
            ("INFO", "SSH auth method: password"),
            ("DEBUG", "Connection timeout: 30s"),
            ("INFO", "Keepalive interval: 30s"),
        ]

        self._add_logs_to_view(self.app_log_view, sample_app)
        self._add_logs_to_view(self.qmp_log_view, sample_qmp)
        self._add_logs_to_view(self.ssh_log_view, sample_ssh)

        self._update_status()

    def _add_logs_to_view(self, view: QTextEdit, logs: list[tuple[str, str]]):
        """Add log entries to a text view."""
        for level, message in logs:
            color = self.LEVEL_COLORS.get(level, "#94a3b8")
            from datetime import datetime
            ts = datetime.now().strftime("%H:%M:%S")
            escaped = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            view.append(f"<span style=\"color:{color}\">[{ts}] [{level}] {escaped}</span>")

    def _update_status(self):
        """Update the status label."""
        source = self.log_source_combo.currentText()
        view = self._get_current_view()
        count = view.toPlainText().count("\n") + 1 if view.toPlainText() else 0
        self.status_label.setText(f"Showing {source} log — {count} entries")

    def _get_current_view(self) -> QTextEdit:
        """Get the currently active log view."""
        idx = self.log_tabs.currentIndex()
        if idx == 0:
            return self.app_log_view
        elif idx == 1:
            return self.qmp_log_view
        else:
            return self.ssh_log_view

    def _clear_logs(self):
        """Clear the current log view."""
        view = self._get_current_view()
        view.clear()
        self._update_status()

    def _export_logs(self):
        """Export the current log view to a file."""
        from datetime import datetime
        view = self._get_current_view()
        source = self.log_source_combo.currentText()
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Logs", f"{source.lower()}_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            "Text Files (*.txt)",
        )
        if path:
            view.document().toPlainText()  # Just get text
            view_text = view.toPlainText()
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"# {source} Log Export — {datetime.now().isoformat()}\n")
                f.write("#" + "=" * 60 + "\n\n")
                f.write(view_text)
            QMessageBox.information(self, "Exported", f"Logs exported to:\n{path}")
