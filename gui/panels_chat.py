"""Agentic Chat Panel — built-in LLM chat with QEMU tool execution."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from gui.theme import T
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QFont, QTextCursor, QColor
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTextEdit,
    QLineEdit, QComboBox, QSplitter, QFrame, QScrollArea, QSizePolicy,
    QProgressBar, QGroupBox, QGridLayout, QCheckBox, QSpinBox, QTabWidget,
    QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog, QMessageBox,
    QInputDialog, QToolButton, QMenu, QAction,
)

from gui.api_providers import APIProviders
from gui.chat_engine import ChatEngine, ChatMessage, ToolExecutor
from gui.provider_store import ProviderStore
from gui.qmp_bridge import QMPBridge
from gui.ssh_bridge import SSHBridge
from gui.iso_manager import ISOManager


class ExpandableToolWidget(QFrame):
    """A collapsible widget that shows tool call details."""

    def __init__(self, tool_name: str, tool_args: dict, parent=None):
        super().__init__(parent)
        self._tool_name = tool_name
        self._tool_args = tool_args
        self._expanded = False
        self._result_text = ""
        self._build_ui()

    def _build_ui(self):
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame {"
            "  background: " + T.BG_SECONDARY + ";"
            "  border: 1px solid " + T.BG_TERTIARY + ";"
            "  border-radius: 6px;"
            "  margin: 2px 0;"
            "}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        # Header row (always visible)
        header = QHBoxLayout()
        self._toggle_btn = QToolButton()
        self._toggle_btn.setText("▶")
        self._toggle_btn.setStyleSheet(
            "QToolButton { border: none; color: " + T.TEXT_SECONDARY + "; font-size: 10px; }"
        )
        self._toggle_btn.setFixedSize(20, 20)
        self._toggle_btn.clicked.connect(self._toggle)
        header.addWidget(self._toggle_btn)

        # Tool icon/label
        icon = self._get_tool_icon(self._tool_name)
        name_label = QLabel(f"{icon} {self._tool_name}")
        name_label.setStyleSheet(
            "color: " + T.WARNING + "; font-weight: bold; font-size: 11px;"
        )
        header.addWidget(name_label)
        header.addStretch()

        # Status indicator
        self._status_label = QLabel("⏳ running")
        self._status_label.setStyleSheet("color: " + T.TEXT_MUTED + "; font-size: 10px;")
        header.addWidget(self._status_label)
        layout.addLayout(header)

        # Args summary (always visible)
        args_str = ", ".join(f"{k}={v}" for k, v in self._tool_args.items())
        if not args_str:
            args_str = "(no args)"
        self._args_label = QLabel(f"  Args: {args_str[:80]}{'...' if len(args_str) > 80 else ''}")
        self._args_label.setStyleSheet("color: " + T.TEXT_MUTED + "; font-size: 10px; padding-left: 20px;")
        layout.addWidget(self._args_label)

        # Detail section (hidden by default)
        self._detail_widget = QWidget()
        detail_layout = QVBoxLayout(self._detail_widget)
        detail_layout.setContentsMargins(20, 4, 4, 4)
        detail_layout.setSpacing(4)

        # Arguments detail
        args_detail = QLabel(f"<b>Arguments:</b> {self._format_args()}")
        args_detail.setWordWrap(True)
        args_detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        args_detail.setStyleSheet("color: " + T.TEXT_SECONDARY + "; font-size: 10px; font-family: Consolas, monospace;")
        detail_layout.addWidget(args_detail)

        # Result section
        self._result_label = QLabel("")
        self._result_label.setWordWrap(True)
        self._result_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._result_label.setStyleSheet(
            "color: " + T.TEXT_PRIMARY + "; font-size: 10px; font-family: Consolas, monospace;"
            " background: " + T.BG_PRIMARY + "; padding: 6px; border-radius: 4px;"
        )
        detail_layout.addWidget(self._result_label)

        self._detail_widget.hide()
        layout.addWidget(self._detail_widget)

    def _get_tool_icon(self, name: str) -> str:
        icons = {
            "vm_status": "📊",
            "vm_start": "▶️",
            "vm_stop": "⏹️",
            "vm_reset": "🔄",
            "vm_suspend": "⏸️",
            "vm_resume": "▶️",
            "guest_exec": "💻",
            "snapshot_create": "📸",
            "snapshot_list": "📋",
            "snapshot_restore": "⏪",
            "iso_list": "💿",
            "iso_import": "📥",
            "get_usage": "📈",
        }
        return icons.get(name, "🔧")

    def _format_args(self) -> str:
        if not self._tool_args:
            return "{}"
        return str(self._tool_args)

    def _toggle(self):
        self._expanded = not self._expanded
        self._toggle_btn.setText("▼" if self._expanded else "▶")
        self._detail_widget.setVisible(self._expanded)

    def set_result(self, result: str, success: bool = True) -> None:
        """Update the widget with the tool result."""
        self._result_text = result
        self._status_label.setText("✅ done" if success else "❌ failed")
        self._status_label.setStyleSheet(
            "color: " + T.SUCCESS + "; font-size: 10px;" if success
            else "color: " + T.ERROR + "; font-size: 10px;"
        )
        # Truncate long results for display
        display = result[:2000] + ("..." if len(result) > 2000 else "")
        self._result_label.setText(f"<b>Result:</b>\n{display}")


class ChatPanel(QWidget):
    """Built-in agentic chat panel with QEMU tool execution."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._store = ProviderStore()
        self._providers = APIProviders(self._store)
        self._current_response = ""
        self._is_streaming = False
        self._tool_widgets: list[ExpandableToolWidget] = []

        # Create bridges
        self._qmp_bridge: QMPBridge | None = None
        self._ssh_bridge: SSHBridge | None = None
        self._iso_manager = ISOManager()

        # Create tool executor
        self._executor = ToolExecutor(
            qmp_bridge=None,
            ssh_bridge=None,
            iso_manager=self._iso_manager,
        )

        # Create chat engine with executor
        self._engine = ChatEngine(self._providers, self._executor)

        # Connect engine signals
        self._engine.tool_call_started.connect(self._on_tool_started)
        self._engine.tool_call_finished.connect(self._on_tool_finished)

        self.setStyleSheet("background: " + T.BG_PRIMARY + ";")
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Header
        header = QWidget()
        hl = QHBoxLayout(header)
        hl.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Agentic Chat")
        title.setStyleSheet("color: " + T.TEXT_PRIMARY + "; font-size: 16px; font-weight: bold;")
        hl.addWidget(title)
        hl.addStretch()
        self._provider_combo = QComboBox()
        self._provider_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._provider_combo.setStyleSheet(
            "QComboBox { background: " + T.BG_SECONDARY + "; color: " + T.TEXT_PRIMARY + ";"
            " border: 1px solid " + T.BG_TERTIARY + "; border-radius: 4px; padding: 4px; }"
        )
        self._refresh_providers()
        hl.addWidget(QLabel("Provider:"))
        hl.addWidget(self._provider_combo)
        layout.addWidget(header)

        # Splitter for chat and usage
        splitter = QSplitter(Qt.Vertical)
        layout.addWidget(splitter)

        # Chat display area
        self._chat_display = QTextEdit()
        self._chat_display.setReadOnly(True)
        self._chat_display.setFont(QFont("Consolas", 10))
        self._chat_display.setStyleSheet(
            "QTextEdit { background: #0d1117; color: #c9d1d9;"
            " border: 1px solid " + T.BG_TERTIARY + "; border-radius: 6px; padding: 8px; }"
        )
        splitter.addWidget(self._chat_display)

        # Input area
        input_row = QWidget()
        il = QHBoxLayout(input_row)
        il.setContentsMargins(0, 0, 0, 0)
        self._input = QLineEdit()
        self._input.setPlaceholderText("Ask me anything about your VM...")
        self._input.returnPressed.connect(self._send_message)
        self._input.setStyleSheet(
            "QLineEdit { background: " + T.BG_SECONDARY + "; color: " + T.TEXT_PRIMARY + ";"
            " border: 1px solid " + T.BG_TERTIARY + "; border-radius: 6px; padding: 8px; }"
        )
        il.addWidget(self._input)

        self._send_btn = QPushButton("Send")
        self._send_btn.setFixedSize(80, 36)
        self._send_btn.setStyleSheet(
            "QPushButton { background: " + T.BRAND + "; border: none; border-radius: 6px;"
            " color: white; font-size: 12px; font-weight: 600; }"
            "QPushButton:hover { background: " + T.BRAND_HOVER + "; }"
            "QPushButton:disabled { background: #334155; color: " + T.TEXT_MUTED + "; }"
        )
        self._send_btn.clicked.connect(self._send_message)
        il.addWidget(self._send_btn)

        self._clear_btn = QPushButton("Clear")
        self._clear_btn.setFixedSize(70, 36)
        self._clear_btn.setStyleSheet(
            "QPushButton { background: " + T.BG_SECONDARY + "; border: 1px solid " + T.BG_TERTIARY + ";"
            " border-radius: 6px; color: " + T.TEXT_SECONDARY + "; font-size: 12px; }"
            "QPushButton:hover { background: " + T.BG_TERTIARY + "; }"
        )
        self._clear_btn.clicked.connect(self._clear_history)
        il.addWidget(self._clear_btn)

        layout.addWidget(input_row)

        # Welcome message
        self._append_message("system", "Welcome to Agentic Chat! I can help you control your VM, execute commands, manage snapshots, and more.")
        self._append_message("system", "Select a provider above and start chatting. If you haven't configured an API key, go to Settings > AI Providers.")

    def set_qmp_bridge(self, bridge: QMPBridge) -> None:
        """Attach a QMP bridge for VM operations."""
        self._qmp_bridge = bridge
        self._executor.set_qmp_bridge(bridge)

    def set_ssh_bridge(self, bridge: SSHBridge) -> None:
        """Attach an SSH bridge for guest operations."""
        self._ssh_bridge = bridge
        self._executor.set_ssh_bridge(bridge)

    def _refresh_providers(self):
        """Refresh the provider dropdown."""
        self._provider_combo.clear()
        providers = self._store.get_enabled_providers()
        for p in providers:
            self._provider_combo.addItem(p.name)
        if not providers:
            self._provider_combo.addItem("No providers configured")

    def _send_message(self):
        """Send user message to the chat engine."""
        text = self._input.text().strip()
        if not text or self._is_streaming:
            return

        self._input.clear()
        self._is_streaming = True
        self._send_btn.setEnabled(False)
        self._current_response = ""

        # Display user message
        self._append_message("user", text)

        # Get selected provider
        provider_name = self._provider_combo.currentText()
        if provider_name == "No providers configured":
            self._append_message("system", "Error: No API providers configured. Please add an API key in Settings > AI Providers.")
            self._is_streaming = False
            self._send_btn.setEnabled(True)
            return

        # Run async chat
        self._run_chat(text, provider_name)

    def _run_chat(self, text: str, provider: str):
        """Run chat in async event loop."""
        loop = asyncio.new_event_loop()

        async def _chat():
            async for msg in self._engine.send_message(text):
                if msg.role == "assistant":
                    # Stream tokens would go here
                    pass
                elif msg.role == "tool":
                    self._append_message("tool", f"Executing {msg.tool_name}...")

        try:
            loop.run_until_complete(self._engine.send_message(text))
        except Exception as e:
            self._append_message("system", f"Error: {e}")
        finally:
            loop.close()
            self._is_streaming = False
            self._send_btn.setEnabled(True)

    @pyqtSlot(str, dict)
    def _on_tool_started(self, name: str, args: dict):
        """Handle tool execution start — add expandable widget."""
        widget = ExpandableToolWidget(name, args)
        self._tool_widgets.append(widget)
        # Insert widget into chat display
        cursor = self._chat_display.textCursor()
        cursor.movePosition(QTextCursor.End)
        # Add a placeholder for the widget
        self._chat_display.append("")  # spacer
        # Store reference for updating
        self._pending_tool_widget = widget
        self._chat_display.append(f'<div style="margin: 4px 0;">🔧 <b style="color: {T.WARNING};">{name}</b> <span style="color: {T.TEXT_MUTED};">running...</span></div>')

    @pyqtSlot(str, str)
    def _on_tool_finished(self, name: str, result: str):
        """Handle tool execution finish — update the widget."""
        success = not result.startswith("[failed]")
        # Update the last tool widget display
        display_result = result[:500] + ("..." if len(result) > 500 else "")
        color = T.SUCCESS if success else T.ERROR
        status = "✅" if success else "❌"
        self._chat_display.append(
            f'<div style="margin: 2px 0 8px 0; padding: 6px; background: {T.BG_SECONDARY}; border-radius: 4px;">'
            f'{status} <b style="color: {color};">{name}</b>: '
            f'<span style="color: {T.TEXT_SECONDARY}; font-family: Consolas, monospace; font-size: 10px;">{display_result}</span>'
            f'</div>'
        )
        self._chat_display.moveCursor(QTextCursor.End)

    def _append_message(self, role: str, content: str):
        """Append a message to the chat display."""
        timestamp = datetime.now().strftime("%H:%M:%S")

        colors = {
            "user": T.BRAND,
            "assistant": T.SUCCESS,
            "tool": T.WARNING,
            "system": T.TEXT_MUTED,
        }
        color = colors.get(role, T.TEXT_PRIMARY)

        role_labels = {
            "user": "You",
            "assistant": "AI",
            "tool": "Tool",
            "system": "System",
        }
        role_label = role_labels.get(role, role)

        html = f'<p><span style="color: {color}; font-weight: bold;">[{timestamp}] {role_label}:</span> {content}</p>'
        self._chat_display.append(html)

        # Auto-scroll
        self._chat_display.moveCursor(QTextCursor.End)

    def _clear_history(self):
        """Clear chat history."""
        self._chat_display.clear()
        self._engine.clear_history()
        self._tool_widgets.clear()
        self._append_message("system", "Chat history cleared.")
