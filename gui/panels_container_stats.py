"""Container Stats Panel — real-time CPU, memory, network, disk I/O metrics.

Displays live container statistics with sparkline graphs for CPU and memory history.
Uses the Docker stats API via the AsyncAdapter.
"""

from __future__ import annotations

from typing import Any

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
    QProgressBar, QSplitter,
)

from gui.theme import T
from gui.widgets import Card, StatusIndicator, StatCard


class SparklineWidget(QWidget):
    """Simple sparkline chart widget."""

    def __init__(self, color: QColor, max_points: int = 60, parent=None):
        super().__init__(parent)
        self._data: list[float] = []
        self._color = color
        self._max_points = max_points
        self.setMinimumHeight(40)
        self.setMaximumHeight(60)

    def add_value(self, value: float) -> None:
        """Add a new data point."""
        self._data.append(value)
        if len(self._data) > self._max_points:
            self._data.pop(0)
        self.update()

    def paintEvent(self, event: Any) -> None:
        """Draw sparkline."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        pen = QPen(self._color, 1.5)
        painter.setPen(pen)

        if len(self._data) < 2:
            return

        w = self.width()
        h = self.height()
        step = w / (self._max_points - 1)
        max_val = max(self._data) if self._data else 1.0
        if max_val == 0:
            max_val = 1.0

        points = []
        for i, val in enumerate(self._data):
            x = w - (len(self._data) - 1 - i) * step
            y = h - (val / max_val) * (h - 4) - 2
            points.append((x, y))

        for i in range(1, len(points)):
            painter.drawLine(
                int(points[i-1][0]), int(points[i-1][1]),
                int(points[i][0]), int(points[i][1]),
            )


class ContainerStatsPanel(QWidget):
    """Real-time container statistics panel."""

    stats_updated = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._adapter = None
        self._history: dict[str, dict] = {}
        self.setStyleSheet("background: " + T.BG_PRIMARY + ";")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignTop)

        # ── Header Stats ───────────────────────────────────────────────────
        header_row = QWidget()
        header_layout = QHBoxLayout(header_row)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(12)

        self._total_containers = StatCard("Total Containers", "0")
        header_layout.addWidget(self._total_containers)

        self._running_containers = StatCard("Running", "0")
        header_layout.addWidget(self._running_containers)

        layout.addWidget(header_row)

        # ── Main Splitter ──────────────────────────────────────────────────
        splitter = QSplitter(Qt.Vertical)

        # ── Container Table ────────────────────────────────────────────────
        table_card = Card("Containers")
        splitter.addWidget(table_card)

        self._table = QTableWidget()
        self._table.setColumnCount(2)
        self._table.setHorizontalHeaderLabels(["Container", "Status"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._table.setStyleSheet(
            f"QTableWidget {{ background: {T.BG_SECONDARY}; color: {T.TEXT_PRIMARY}; border: none; }}"
            f"QHeaderView::section {{ background: {T.BG_TERTIARY}; color: {T.TEXT_MUTED}; padding: 6px; border: none; }}"
        )
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        table_card.content_layout.addWidget(self._table)

        # ── Detail View ────────────────────────────────────────────────────
        detail_card = Card("Container Detail")
        splitter.addWidget(detail_card)

        detail_widget = QWidget()
        detail_layout = QHBoxLayout(detail_widget)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(12)

        # CPU sparkline
        cpu_widget = QWidget()
        cpu_layout = QVBoxLayout(cpu_widget)
        cpu_layout.setContentsMargins(0, 0, 0, 0)
        cpu_layout.addWidget(QLabel("CPU History"))
        self._cpu_sparkline = SparklineWidget(QColor("#60a5fa"))
        cpu_layout.addWidget(self._cpu_sparkline)
        self._cpu_label = QLabel("0%")
        self._cpu_label.setStyleSheet(f"color: {T.TEXT_PRIMARY}; font-size: 18px; font-weight: bold;")
        cpu_layout.addWidget(self._cpu_label)
        detail_layout.addWidget(cpu_widget)

        # Memory sparkline
        mem_widget = QWidget()
        mem_layout = QVBoxLayout(mem_widget)
        mem_layout.setContentsMargins(0, 0, 0, 0)
        mem_layout.addWidget(QLabel("Memory History"))
        self._mem_sparkline = SparklineWidget(QColor("#a78bfa"))
        mem_layout.addWidget(self._mem_sparkline)
        self._mem_label = QLabel("0 MB")
        self._mem_label.setStyleSheet(f"color: {T.TEXT_PRIMARY}; font-size: 18px; font-weight: bold;")
        mem_layout.addWidget(self._mem_label)
        detail_layout.addWidget(mem_widget)

        # Detail info
        self._detail_info = QLabel("Select a container to view details")
        self._detail_info.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 12px;")
        detail_layout.addWidget(self._detail_info)

        detail_card.content_layout.addWidget(detail_widget)

        layout.addWidget(splitter)

        # ── Controls ───────────────────────────────────────────────────────
        ctrl_row = QWidget()
        ctrl_layout = QHBoxLayout(ctrl_row)
        ctrl_layout.setContentsMargins(0, 0, 0, 0)
        ctrl_layout.setSpacing(8)

        self._btn_refresh = QPushButton("Refresh Now")
        self._btn_refresh.setStyleSheet(
            f"background: {T.BRAND}; color: {T.TEXT_PRIMARY}; border: none;"
            "border-radius: 6px; padding: 6px 16px; font-weight: bold;"
        )
        self._btn_refresh.clicked.connect(self._refresh)
        ctrl_layout.addWidget(self._btn_refresh)

        self._auto_refresh_cb = QLabel("Auto-refresh: ON (2s)")
        self._auto_refresh_cb.setStyleSheet(f"color: {T.TEXT_MUTED};")
        ctrl_layout.addWidget(self._auto_refresh_cb)

        ctrl_layout.addStretch()

        self._status_indicator = StatusIndicator(QColor("#ef4444"))
        ctrl_layout.addWidget(self._status_indicator)
        ctrl_layout.addWidget(QLabel("Docker"))

        layout.addWidget(ctrl_row)

        # ── Refresh Timer ──────────────────────────────────────────────────
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh)
        self._refresh_timer.start(2000)

        self._refresh()

    def showEvent(self, event):
        super().showEvent(event)
        # Refresh when shown (the timer skips hidden panels), after the switch has painted.
        from PyQt5.QtCore import QTimer as _QTimer
        _QTimer.singleShot(0, self._refresh)

    def _refresh(self):
        """Refresh container stats."""
        if not self.isVisible():
            return  # a hidden panel does not poll its backends (that froze the window when one was down)
        try:
            from gui.async_adapter import get_adapter
            adapter = get_adapter()
            containers = adapter.docker.list_containers()
            self._update_table(containers)
            self._update_header(containers)
            self._status_indicator.set_status(True)
        except Exception as e:
            self._status_indicator.set_status(False)
            print(f"Stats refresh error: {e}")

    def _update_header(self, containers: list):
        """Update header statistics."""
        total = len(containers)
        running = sum(1 for c in containers if "running" in c.get("status", "").lower())
        self._total_containers.set_value(str(total))
        self._running_containers.set_value(str(running))

    def _update_table(self, containers: list):
        """Update container table."""
        self._table.setRowCount(len(containers))
        for i, c in enumerate(containers):
            name = c.get("name", "")
            status = c.get("status", "")

            self._table.setItem(i, 0, QTableWidgetItem(name))
            self._table.setItem(i, 1, QTableWidgetItem(status))

    def _on_selection_changed(self):
        """Handle container selection change."""
        row = self._table.currentRow()
        if row < 0:
            return

        name = self._table.item(row, 0).text()
        self._load_container_detail(name)

    def _load_container_detail(self, name: str):
        """Load detailed stats for selected container."""
        try:
            from gui.async_adapter import get_adapter
            adapter = get_adapter()
            stats = adapter.docker.get_stats(name)

            cpu = stats.get("cpu_percent", 0)
            mem = stats.get("memory_usage", 0)

            self._cpu_sparkline.add_value(float(str(cpu).replace("%", "")))
            self._mem_sparkline.add_value(float(str(mem).replace(" MB", "")))

            self._cpu_label.setText(f"{cpu}%")
            self._mem_label.setText(f"{mem} MB")

            self._detail_info.setText(
                f"Container: {name}\n"
                f"Status: {stats.get('status', 'unknown')}\n"
                f"Uptime: {stats.get('uptime', 'unknown')}\n"
                f"Image: {stats.get('image', 'unknown')}"
            )
        except Exception as e:
            self._detail_info.setText(f"Error loading stats: {e}")
