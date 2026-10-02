"""Enhanced UI components for the VM-Harness GUI.

All widgets consume design tokens from gui.theme (T) for consistent
styling across the application.  New widgets: Toast, StatCard,
SectionHeader, Badge, Timeline, ProgressBar.

Backward-compatible with existing panels: Card, StatusIndicator,
IconButton, TextInput, PasswordInput, TerminalOutput, TelemetryChart,
LogEntry, CredentialTreeItem, FileTree all preserved.
StatusIndicator       Animated connection/VM status dot
IconButton            Push button with icon and text, themed
Card                  Rounded card container with title
TextInput             Styled single-line text input
PasswordInput         Password field with show/hide toggle
TerminalOutput        Read-only terminal with colored output
TelemetryChart        Real-time matplotlib line chart
    CredentialTreeItem    Tree item for stored credentials
    FileTree              File browser tree for guest filesystem
    Toast                 Auto-dismissing notification toast
    StatCard              Compact stat display (label + value + trend)
    SectionHeader         Panel section divider with title
    ProgressBar           Themed progress bar
    Badge                 Small colored badge/label
    Timeline              Vertical timeline for event sequences

"""


from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal, QTimer, QSize, QPoint, QRect
from PyQt5.QtGui import (
    QColor, QIcon, QPainter, QPalette, QFont, QFontMetrics,
    QPainterPath, QBrush, QPen,
)
from PyQt5.QtWidgets import (
    QWidget, QLabel, QPushButton, QFrame, QLineEdit, QTextEdit,
    QPlainTextEdit, QComboBox, QCheckBox, QSpinBox, QTreeWidget,
    QTreeWidgetItem, QHeaderView, QScrollArea, QGroupBox, QTabWidget,
    QMessageBox, QInputDialog, QFileDialog, QApplication,
    QHBoxLayout, QVBoxLayout, QGridLayout, QSizePolicy,
)
from typing import Optional, List, Dict, Any
import pathlib
import datetime

from gui.theme import T, dark_palette, card_style, card_title_style, \
    primary_label_style, secondary_label_style, muted_label_style, \
    input_style, button_green_style, button_red_style, button_blue_style, \
    button_ghost_style, button_bordered_style, lifecycle_btn_style, \
    sidebar_btn_style, status_bar_style, tab_bar_style, tree_style, \
    list_style, progress_style, combo_style, spinbox_style, checkbox_style, \
    text_browser_style, dialog_style, splitter_style, title_bar_style, \
    sidebar_style, panel_bg_style


# -- Status Indicator --------------------------------------------------------------

# -- Status Indicator --------------------------------------------------------------

class StatusIndicator(QWidget):
    """Animated colored dot showing connection/VM status.

    Emits color_changed when the status color changes.
    Supports a pulsing animation when running and connected.
    """

    color_changed = pyqtSignal(QColor)

    def __init__(self, color: QColor = QColor(T.DOT_OFFLINE), parent=None):
        super().__init__(parent)
        self._color = color
        self._pulse_enabled = False
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(800)
        self._pulse_timer.timeout.connect(self._pulse)
        self._pulse_phase = 0.0
        self.setFixedSize(12, 12)
        self.setCursor(Qt.PointingHandCursor)

    def set_status(self, running: bool, connected: bool = False):
        if running and connected:
            self._color = QColor(T.DOT_RUNNING_CONNECTED)
            self._pulse_enabled = True
        elif running:
            self._color = QColor(T.DOT_RUNNING)
            self._pulse_enabled = True
        elif connected:
            self._color = QColor(T.DOT_CONNECTED)
            self._pulse_enabled = False
        else:
            self._color = QColor(T.DOT_OFFLINE)
            self._pulse_enabled = False
        if self._pulse_enabled:
            if not self._pulse_timer.isActive():
                self._pulse_timer.start()
        else:
            self._pulse_timer.stop()
            self._pulse_phase = 0.0
        self.update()
        self.color_changed.emit(self._color)

    def _pulse(self):
        self._pulse_phase = (self._pulse_phase + 1) % 4
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)

        if self._pulse_enabled and self._pulse_timer.isActive:
            brightness = 0.6 + 0.4 * (0.5 + 0.5 * __import__("math").sin(
                self._pulse_phase * __import__("math").pi))
            c = self._color
            pulse_color = QColor(
                int(c.red() * brightness),
                int(c.green() * brightness),
                int(c.blue() * brightness),
            )
            # Glow
            painter.setBrush(QColor(pulse_color.red(), pulse_color.green(),
                                     pulse_color.blue(), 40))
            painter.drawEllipse(-3, -3, self.width() + 6, self.height() + 6)
            # Core
            painter.setBrush(pulse_color)
            painter.drawEllipse(0, 0, self.width() - 1, self.height() - 1)
        else:
            painter.setBrush(self._color)
            painter.drawEllipse(0, 0, self.width() - 1, self.height() - 1)

    def sizeHint(self) -> QSize:
        return QSize(12, 12)


# -- Toast Notification ------------------------------------------------------------

class Toast(QFrame):
    """Auto-dismissing notification toast.

    Shows a message with an icon type (success, warning, error, info)
    for a configurable duration, then fades out and hides itself.
    """

    dismissed = pyqtSignal()

    def __init__(self, message: str, kind: str = "info", duration: int = 3000,
                 parent=None):
        super().__init__(parent)
        self._message = message
        self._kind = kind
        self._duration = duration
        self._fade_timer = QTimer(self)
        self._fade_timer.setSingleShot(True)
        self._fade_timer.timeout.connect(self._fade_out)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        self.setFrameStyle(QFrame.StyledPanel | QFrame.Plain)
        self.setStyleSheet("background: #1e293b; border-radius: 8px; padding: 12px 16px;")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._build_ui()
        self.show()
        self._fade_timer.start(duration)

    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        icon_map = {
            "success": "\u2714",
            "warning": "\u26A0",
            "error": "\u2716",
            "info": "\u2139",
        }
        icon_color_map = {
            "success": T.SUCCESS,
            "warning": T.WARNING,
            "error": T.ERROR,
            "info": T.INFO,
        }

        icon_label = QLabel(icon_map.get(self._kind, "\u2139"))
        icon_label.setStyleSheet(
            f"color: {icon_color_map.get(self._kind, T.INFO)};"
            "font-size: 16px; font-weight: bold;"
        )
        icon_label.setFixedSize(20, 20)
        layout.addWidget(icon_label, alignment=Qt.AlignVCenter)

        msg_label = QLabel(self._message)
        msg_label.setStyleSheet(
            f"color: {T.TEXT_PRIMARY};"
            "font-size: 12px;"
        )
        msg_label.setWordWrap(True)
        layout.addWidget(msg_label, stretch=1)

        layout.addStretch()

        self.setLayout(layout)

    def _fade_out(self):
        self._fade_timer.stop()
        self.hide()


# -- Stat Card ---------------------------------------------------------------------

class StatCard(QFrame):
    """Compact statistics card: label, value, optional trend indicator.

    Used in dashboards to show metrics like CPU, RAM, disk usage etc.
    """

    def __init__(self, label: str, value: str = "—",
                 color: str = T.TEXT_PRIMARY, trend: str = "",
                 parent=None):
        super().__init__(parent)
        self.setFrameStyle(QFrame.StyledPanel | QFrame.Raised)
        self.setStyleSheet(card_style())
        self.setFixedHeight(60)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        label_l = QLabel(label)
        label_l.setStyleSheet(
            f"color: {T.TEXT_SECONDARY};"
            f"font-size: {T.FS_SM}px;"
        )
        layout.addWidget(label_l)

        value_l = QLabel(value)
        value_l.setStyleSheet(
            f"color: {color};"
            f"font-size: {T.FS_XL}px;"
            "font-weight: 600;"
        )
        layout.addWidget(value_l, stretch=1)

        if trend:
            trend_l = QLabel(trend)
            trend_color = T.SUCCESS if "↑" in trend or "↓" in trend else T.TEXT_MUTED
            trend_l.setStyleSheet(
                f"color: {trend_color};"
                f"font-size: {T.FS_SM}px;"
            )
            layout.addWidget(trend_l)

        layout.addStretch()
        self._value_label = value_l
        self._trend_label = trend_l if trend else None

    def set_value(self, value: str, color: str = T.TEXT_PRIMARY):
        self._value_label.setText(value)
        self._value_label.setStyleSheet(
            f"color: {color};"
            f"font-size: {T.FS_XL}px;"
            "font-weight: 600;"
        )

    def set_trend(self, trend: str):
        if self._trend_label:
            self._trend_label.setText(trend)
            tc = T.SUCCESS if "\u2191" in trend or "\u2193" in trend else T.TEXT_MUTED
            self._trend_label.setStyleSheet(f"color: {tc}; font-size: {T.FS_SM}px;")


# -- Section Header ---------------------------------------------------------------

class SectionHeader(QFrame):
    """A panel section divider with a title and optional subtitle."""

    def __init__(self, title: str, subtitle: str = "",
                 parent=None):
        super().__init__(parent)
        self.setFixedHeight(32)
        self.setStyleSheet(
            f"background: {T.BG_SECONDARY};"
            f"border-bottom: 1px solid {T.BG_TERTIARY};"
            "border-radius: 0;"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(8)

        title_l = QLabel(title)
        title_l.setStyleSheet(
            f"color: {T.TEXT_PRIMARY};"
            f"font-size: {T.FS_LG}px;"
            "font-weight: 600;"
        )
        layout.addWidget(title_l)

        if subtitle:
            sub_l = QLabel(subtitle)
            sub_l.setStyleSheet(
                f"color: {T.TEXT_MUTED};"
                f"font-size: {T.FS_SM}px;"
            )
            layout.addWidget(sub_l)

        layout.addStretch()


# -- Badge -------------------------------------------------------------------------

class Badge(QFrame):
    """Small colored badge/label for status or category display."""

    def __init__(self, text: str, color: str = T.BRAND,
                 text_color: str = "#ffffff", parent=None):
        super().__init__(parent)
        self.setFixedHeight(20)
        self.setStyleSheet(
            f"background: {color};"
            f"color: {text_color};"
            f"border-radius: {T.R_FULL}px;"
            f"font-size: {T.FS_SM}px;"
            "font-weight: 600;"
            "padding: 0 8px;"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {text_color};"
            f"font-size: {T.FS_SM}px;"
            "font-weight: 600;"
        )
        layout.addWidget(lbl)
        self.setAlignment(Qt.AlignCenter)

    def set_color(self, color: str):
        self.setStyleSheet(
            f"background: {color};"
            "color: #ffffff;"
            f"border-radius: {T.R_FULL}px;"
            f"font-size: {T.FS_SM}px;"
            "font-weight: 600;"
            "padding: 0 8px;"
        )


# -- Timeline ---------------------------------------------------------------------

class Timeline(QFrame):
    """Vertical timeline for displaying sequenced events."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {T.BG_PRIMARY}; border: none;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._items: List[QLabel] = []

    def add_event(self, time: str, message: str, color: str = T.TEXT_SECONDARY):
        event_frame = QFrame()
        event_frame.setStyleSheet("background: transparent; border: none;")
        event_layout = QHBoxLayout(event_frame)
        event_layout.setContentsMargins(0, 4, 0, 4)
        event_layout.setSpacing(8)

        dot = QLabel("\u25CF")
        dot.setStyleSheet(f"color: {color}; font-size: 8px;")
        dot.setFixedSize(8, 8)
        event_layout.addWidget(dot, alignment=Qt.AlignTop)

        content = QLabel(f"<b>{time}</b>  {message}")
        content.setStyleSheet(
            f"color: {T.TEXT_PRIMARY};"
            f"font-size: {T.FS_MD}px;"
        )
        content.setWordWrap(True)
        event_layout.addWidget(content, stretch=1)

        event_layout.addStretch()
        self.layout().addWidget(event_frame)
        self._items.append(content)

    def clear(self):
        for item in self._items:
            item.deleteLater()
        self._items.clear()


# -- Enhanced Card ----------------------------------------------------------------

class Card(QFrame):
    """A rounded card container for grouping related content.

    Uses theme tokens for consistent styling.  Provides a title label
    and a content_layout for adding child widgets.
    """

    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setFrameStyle(QFrame.StyledPanel | QFrame.Raised)
        self.setStyleSheet(card_style())
        self.setMinimumHeight(60)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(T.LG, T.MD, T.LG, T.MD)
        layout.setSpacing(T.MD)

        if title:
            self.title_label = QLabel(title)
            self.title_label.setStyleSheet(card_title_style())
            layout.addWidget(self.title_label)

        self.content_layout = QVBoxLayout()
        self.content_layout.setSpacing(T.SM)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.content_layout)

    def add_widget(self, widget: QWidget):
        self.content_layout.addWidget(widget)

    def add_widgets(self, *widgets: QWidget):
        for w in widgets:
            self.content_layout.addWidget(w)

    def add_row(self, label: str, widget: QWidget, tooltip: str = ""):
        row = QFrame()
        row.setStyleSheet("background: transparent; border: none;")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(T.SM)

        lbl = QLabel(label)
        lbl.setStyleSheet(f"color: {T.TEXT_SECONDARY}; font-size: {T.FS_MD}px;")
        lbl.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        if tooltip:
            lbl.setToolTip(tooltip)
        row_layout.addWidget(lbl)

        if hasattr(widget, 'setStyleSheet'):
            widget.setStyleSheet(
                f"color: {T.TEXT_PRIMARY};"
                f"background: {T.BG_PRIMARY};"
                f"border: 1px solid {T.BG_TERTIARY};"
                f"border-radius: {T.R_SM}px;"
                "padding: 4px 8px;"
            )
        row_layout.addWidget(widget)
        row_layout.addStretch()
        self.content_layout.addWidget(row)


# -- Text Input Field -------------------------------------------------------------

class TextInput(QLineEdit):
    """Styled single-line text input using theme tokens."""

    def __init__(self, placeholder: str = "", parent=None):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setStyleSheet(input_style())


# -- Password Input ---------------------------------------------------------------

class PasswordInput(QWidget):
    """Password field with show/hide toggle."""

    def __init__(self, placeholder: str = "", parent=None):
        super().__init__(parent)
        self._input = QLineEdit(self)
        self._input.setPlaceholderText(placeholder)
        self._input.setEchoMode(QLineEdit.Password)
        self._input.setStyleSheet(input_style())

        self._toggle = QPushButton("\U0001F441", self)
        self._toggle.setFixedSize(28, 28)
        self._toggle.setCursor(Qt.PointingHandCursor)
        self._toggle.setStyleSheet(
            "QPushButton {"
            "  background: transparent;"
            "  border: none;"
            f"  color: {T.TEXT_SECONDARY};"
            "  font-size: 14px;"
            "}"
            "QPushButton:hover {"
            f"  color: {T.TEXT_PRIMARY};"
            "}"
        )
        self._toggle.clicked.connect(self._toggle_visibility)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._input)
        layout.addWidget(self._toggle)

    def _toggle_visibility(self):
        if self._input.echoMode() == QLineEdit.Password:
            self._input.setEchoMode(QLineEdit.Normal)
            self._toggle.setText("\U0001F648")
        else:
            self._input.setEchoMode(QLineEdit.Password)
            self._toggle.setText("\U0001F441")

    def text(self) -> str:
        return self._input.text()

    def setText(self, text: str):
        self._input.setText(text)

    def setPlaceholderText(self, text: str):
        self._input.setPlaceholderText(text)

    def setFocus(self, focus: bool = True):
        if focus:
            self._input.setFocus()


# -- Icon Button ------------------------------------------------------------------

class IconButton(QPushButton):
    """Themed push button with icon path and optional text."""

    def __init__(self, icon_path: Optional[str] = None, text: str = "",
                 parent=None):
        super().__init__(parent)
        self._icon_path = icon_path
        self.setText(text)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            "IconButton {"
            f"  background: {T.BG_SECONDARY};"
            f"  border: 1px solid {T.BG_TERTIARY};"
            f"  border-radius: {T.R_MD}px;"
            f"  color: {T.TEXT_PRIMARY};"
            f"  padding: 8px 14px;"
            f"  font-size: {T.FS_LG}px;"
            "  min-height: 36px;"
            "}"
            "IconButton:hover {"
            f"  background: {T.BG_TERTIARY};"
            f"  border-color: {T.BRAND};"
            "}"
            "IconButton:pressed {"
            f"  background: {T.BG_TERTIARY};"
            "}"
            "IconButton:disabled {"
            f"  background: {T.BG_SECONDARY};"
            f"  color: {T.TEXT_MUTED};"
            f"  border-color: {T.BG_TERTIARY};"
            "}"
            "IconButton:checked {"
            f"  background: {T.BRAND};"
            f"  border-color: {T.BRAND_HOVER};"
            "  color: white;"
            "}"
        )

        if icon_path:
            try:
                icon = QIcon(icon_path)
                if not icon.isNull():
                    self.setIcon(icon)
                    self.setIconSize(QSize(20, 20))
            except (RuntimeError, OSError):
                pass  # Icon load failed — use text-only button

    def set_icon(self, icon: QIcon):
        self.setIcon(icon)
        self.setIconSize(QSize(20, 20))

    def sizeHint(self) -> QSize:
        sz = super().sizeHint()
        return QSize(max(sz.width(), 100), max(sz.height(), 36))


# -- Terminal Output Widget -------------------------------------------------------

class TerminalOutput(QPlainTextEdit):
    """Read-only terminal output with monospace font and colored lines."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setStyleSheet(
            "QPlainTextEdit {"
            "  background: " + T.BG_PRIMARY + ";"
            "  color: " + T.TEXT_PRIMARY + ";"
            "  border: 1px solid " + T.BG_TERTIARY + ";"
            "  border-radius: " + str(T.R_SM) + "px;"
            "  font-family: 'Consolas', 'Courier New', monospace;"
            "  font-size: " + str(T.FS_LG) + "px;"
            "  padding: 8px;"
            "}"
            "QPlainTextEdit:focus { border-color: " + T.BRAND + "; }"
        )
        self._timestamp_format = "%H:%M:%S"

    def append_line(self, text: str, level: str = "INFO"):
        from datetime import datetime
        ts = datetime.now().strftime(self._timestamp_format)
        colors = {
            "INFO": T.TEXT_PRIMARY,
            "ERROR": T.ERROR,
            "WARNING": T.WARNING,
            "COMMAND": T.INFO,
            "OUTPUT": T.CHART_RAM,
        }
        color = colors.get(level, T.TEXT_PRIMARY)
        escaped = (text.replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;"))
        self.appendHtml(
            f'<span style="color:{color}">[{ts}] {escaped}</span>')

    def append_command(self, command: str):
        self.append_line(command, "COMMAND")

    def append_output(self, output: str):
        for line in output.split("\n"):
            if line.strip():
                self.append_line(line, "OUTPUT")

    def clear_terminal(self):
        self.clear()


# -- Telemetry Chart --------------------------------------------------------------

class TelemetryChart(QWidget):
    """Real-time line chart using matplotlib, embedded in PyQt5."""

    def __init__(self, title: str = "", value_label: str = "",
                 parent=None):
        super().__init__(parent)
        self._title = title
        self._value_label = value_label
        self._data: List[float] = []
        self._max_points = 60
        self._color = T.CHART_CPU

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        try:
            self.figure = __import__("matplotlib.figure").figure.Figure(
                figsize=(4, 2.5), dpi=100)
            self.canvas = __import__(
                "matplotlib.backends.backend_qtagg",
                fromlist=["FigureCanvasQTAgg"]).FigureCanvasQTAgg(self.figure)
            layout.addWidget(self.canvas)
            self._ax = self.figure.add_subplot(111)
            self._ax.set_facecolor(T.BG_PRIMARY)
            self._ax.set_title(title, color=T.TEXT_SECONDARY,
                                fontsize=10, pad=8)
            self._ax.tick_params(colors=T.TEXT_MUTED, labelsize=8)
            self._ax.spines["bottom"].set_color(T.BG_TERTIARY)
            self._ax.spines["left"].set_color(T.BG_TERTIARY)
            self._ax.spines["top"].set_visible(False)
            self._ax.spines["right"].set_visible(False)
            self._ax.set_ylabel(value_label, color=T.TEXT_MUTED, fontsize=8)
            self._ax.grid(True, alpha=0.2, color=T.BG_TERTIARY)
            self._line, = self._ax.plot([], [], color=self._color,
                                         linewidth=1.5)
            self._ax.set_xlim(0, self._max_points)
            self._ax.set_ylim(0, 100)
            self._matplotlib_available = True
        except Exception as e:
            # Graceful fallback when matplotlib is not available
            # (e.g., frozen PyInstaller build missing matplotlib bundle)
            import logging
            logging.getLogger("vmharness.gui").warning(
                "matplotlib unavailable (%s) — using placeholder chart", e)
            self._matplotlib_available = False
            placeholder = QWidget(self)
            placeholder.setLayout(QVBoxLayout())
            label = QLabel(
                f"Telemetry chart unavailable\n({value_label})\n"
                "(matplotlib missing from frozen build)"
            )
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet("color: #64748b; font-size: 10px;")
            placeholder.layout().addWidget(label)
            layout.addWidget(placeholder)

        self.setLayout(layout)

    def update_data(self, value: float):
        if getattr(self, '_matplotlib_available', True):
            self._data.append(value)
            if len(self._data) > self._max_points:
                self._data = self._data[-self._max_points:]
            xs = list(range(len(self._data)))
            self._line.set_data(xs, self._data)
            if len(self._data) > 1:
                self._ax.set_xlim(0, len(self._data) - 1)
                lo = min(self._data) * 0.8
                hi = max(self._data) * 1.1
                if lo == hi:
                    lo, hi = 0, 1
                self._ax.set_ylim(lo, hi)
            self.canvas.draw_idle()

    def clear(self):
        if getattr(self, '_matplotlib_available', True):
            self._data = []
            self._line.set_data([], [])
            self.canvas.draw_idle()

    def set_color(self, color: str):
        if getattr(self, '_matplotlib_available', True):
            self._color = color
            self._line.set_color(color)
            self.canvas.draw_idle()

    def get_ydata(self) -> List[float]:
        return list(self._data)

    def export_to_png(self, path: str):
        """Export the current chart to a PNG file."""
        if getattr(self, '_matplotlib_available', True):
            self.figure.savefig(path, dpi=150,
                                facecolor=T.BG_PRIMARY,
                                edgecolor='none',
                                bbox_inches='tight')


# -- Log Entry Widget -------------------------------------------------------------

class LogEntry(QFrame):
    """A single color-coded log entry row."""

    def __init__(self, timestamp: str, level: str, message: str,
                 parent=None):
        super().__init__(parent)
        self.setFixedHeight(22)
        self.setStyleSheet("background: transparent; border: none;")

        levels = {
            "DEBUG": T.TEXT_MUTED,
            "INFO": T.INFO,
            "WARNING": T.WARNING,
            "ERROR": T.ERROR,
            "CRITICAL": "#dc2626",
        }
        color = levels.get(level.upper(), T.TEXT_SECONDARY)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(8)

        ts_l = QLabel(timestamp)
        ts_l.setStyleSheet(
            f"color: {color};"
            f"font-size: {T.FS_SM}px;"
            "font-family: monospace;"
        )
        ts_l.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(ts_l)

        level_l = QLabel(level)
        level_l.setStyleSheet(
            f"color: {color};"
            f"font-size: {T.FS_SM}px;"
            "font-weight: bold;"
        )
        level_l.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(level_l)

        msg_l = QLabel(message)
        msg_l.setStyleSheet(
            f"color: {T.TEXT_PRIMARY};"
            f"font-size: {T.FS_MD}px;"
            "font-family: monospace;"
        )
        msg_l.setWordWrap(True)
        layout.addWidget(msg_l)
        layout.addStretch()


# -- Credential Tree Item ---------------------------------------------------------

class CredentialTreeItem(QTreeWidgetItem):
    """A tree item representing a stored credential."""

    TYPE_COLORS = {
        "password": T.CRED_PASSWORD,
        "ssh_key": T.CRED_SSH_KEY,
        "api_key": T.CRED_API_KEY,
        "qmp_pass": T.CRED_QMP_PASS,
        "other": T.CRED_OTHER,
    }

    def __init__(self, cred_id: str, name: str, cred_type: str,
                 description: str = "", parent=None):
        super().__init__(parent)
        self.cred_id = cred_id
        self.setText(0, name)
        color = self.TYPE_COLORS.get(cred_type, T.CRED_OTHER)
        self.setText(1, cred_type)
        self.setText(2, description[:50] or "\u2014")
        self.setIcon(1, QIcon())


# -- File Browser Tree ------------------------------------------------------------

class FileTree(QTreeWidget):
    """Simple file browser tree for the guest filesystem."""

    file_selected = pyqtSignal(str)

    def __init__(self, root_path: str = "/"):
        super().__init__()
        self.root_path = pathlib.Path(root_path)
        self.setHeaderLabels(["Name", "Size", "Modified"])
        self.header().setSectionResizeMode(
            0, QHeaderView.Stretch)
        self.header().setSectionResizeMode(
            1, QHeaderView.ResizeToContents)
        self.header().setSectionResizeMode(
            2, QHeaderView.ResizeToContents)
        self.setAnimated(True)
        self.setRootIsDecorated(True)
        self.setAlternatingRowColors(True)
        self.setStyleSheet(tree_style())
        self.itemDoubleClicked.connect(self._on_double_click)

    def _on_double_click(self, item: QTreeWidgetItem, column: int):
        path = item.data(0, Qt.UserRole)
        if path:
            self.file_selected.emit(path)

    def populate(self, files: List[Dict[str, Any]],
                 root_path: str = "/"):
        self.clear()
        root = QTreeWidgetItem(self, [root_path, "", ""])
        root.setData(0, Qt.UserRole, root_path)
        root.setExpanded(True)
        for f in files:
            child = QTreeWidgetItem(
                root,
                [f["name"], f"{f.get('size', '')}",
                 f.get("mtime", "")],
            )
            child.setData(0, Qt.UserRole,
                          f.get("path",
                                f"{root_path}/{f['name']}"))
            if f.get("type") == "dir":
                child.setFlags(
                    child.flags() | Qt.ItemIsAutoTristate)
            else:
                child.setFlags(child.flags())
        self.addTopLevelItem(root)
        self.expandAll()


# -- Progress Bar -----------------------------------------------------------------

class ProgressBar(QFrame):
    """Thin themed progress bar indicator."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(4)
        self.setStyleSheet(progress_style())
        self._bar = QProgressBar(self)
        self._bar.setMaximum(0)
        self._bar.setTextVisible(False)
        self._bar.setStyleSheet(progress_style())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._bar)

    def show(self):
        self._bar.show()
        super().show()

    def hide(self):
        self._bar.hide()
        super().hide()

    def set_visible(self, visible: bool):
        if visible:
            self.show()
        else:
            self.hide()

    def set_range(self, min_val: int, max_val: int):
        self._bar.setMinimum(min_val)
        self._bar.setMaximum(max_val)

    def setValue(self, value: int):
        self._bar.setValue(value)


# ── Application-wide defaults ────────────────────────────────────────────────────

def apply_global_theme(app=None):
    """Apply the dark theme to the QApplication palette."""
    from gui.theme import dark_palette
    if app is None:
        app = QApplication.instance()
    if app is None:
        return
    app.setApplicationName("VM-Harness")
    app.setApplicationVersion("1.0.0")
    app.setOrganizationName("VM-Harness")
    if app.style().objectName() != "Fusion":
        app.setStyle("Fusion")
    app.setPalette(dark_palette())
