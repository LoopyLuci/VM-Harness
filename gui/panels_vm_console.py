"""VM Console Streaming Panel — a real, per-VM live console.

Shows one QEMU VM's framebuffer, and sends that VM's pointer and keyboard
input back into it, over the same WebSocket the frames arrive on.

Layout
------
A vertical :class:`QSplitter` puts the video on top and the controls plus every
setting that changes the stream underneath it, so the user can size the two
independently.  The controls are not decorative: each one is wired to the
``config`` message the bridge actually serves, and the values the bridge clamps
back are shown next to what was asked for.

Wire protocol (``streaming_bridge.py`` and Continuum's ``ws_sidecar.rs``)
-------------------------------------------------------------------------
The bridge REQUIRES authentication.  The first message on a new socket is
``{"type":"auth","key":<token>}``; nothing else may precede it, and there is no
unauthenticated path.  Everything after that::

    {"type":"config","vm":<name>,"quality":<int>,"fps":<int>,
     "width":<int>,"height":<int>,"input_enabled":<bool>}
    {"type":"subscribe","vm":<name>}
    {"type":"input","input_type":"key","key":<name>,"pressed":<bool>}
    {"type":"input","input_type":"mouse_move","x":<int>,"y":<int>}
    {"type":"input","input_type":"mouse_click","button":<str>,"pressed":<bool>}
    {"type":"input","input_type":"scroll","dx":<int>,"dy":<int>}
    {"type":"ping","time":<ms>}
    {"type":"stats_request"}

Server to client: BINARY frames are raw JPEG with no header and no metadata;
TEXT frames are JSON -- ``auth_ok``, ``config_ack``, ``pong``, ``stats``,
``vm_list`` and ``error``.  ``config_ack`` echoes the values actually in force
after clamping, and those -- not the values requested -- are what the
coordinates sent to the guest are expressed in, because the bridge divides the
guest pixel by exactly the width and height it acked.

Ordering guarantee
------------------
:meth:`VMConsolePanel._send_json` refuses to put anything on the wire before the
server has answered ``auth``, and queues it instead.  Without that queue a
keystroke between "socket opened" and "auth accepted" would be a protocol error
that costs the whole connection.

Widget coordinates to guest pixels
----------------------------------
The frames are scaled and centred by ``fitInView``, and the acked frame size is
usually not the size of the widget, so a click's position cannot be recovered by
scaling the mouse position by some guessed ratio.  It is recovered from the view
itself:

1. ``QGraphicsView.mapToScene(pos)`` applies the inverse of whatever transform
   the view currently has, which is exactly the ``fitInView`` scale plus the
   centring offset plus any scroll offset from panning.  Doing the arithmetic by
   hand would have to re-derive all three, and would be wrong the moment the
   frame was letterboxed.
2. The scene point is tested against ``QGraphicsPixmapItem.sceneBoundingRect()``.
   The item is placed at the origin with its natural pixel size, so scene units
   are framebuffer pixels 1:1.  A point outside that rect is in the letterbox
   area -- no framebuffer pixel is under it, so nothing is sent rather than a
   clamped-to-the-edge click the user did not ask for.
3. ``(scene - rect.topleft()) / rect.size() * frame_size`` converts to guest
   pixels and is clamped to ``0 .. frame_size - 1``.  The clamp is belt and
   braces: step 2 already guarantees containment, and floating point rounding at
   the right edge can still land on ``frame_size``.

Because step 1 is an affine map with positive determinant, increasing widget
coordinates map to increasing guest coordinates, which is what makes a pointer
track the cursor rather than mirror it.

Threading
---------
The WebSocket client runs on a daemon thread.  Callbacks never touch Qt widgets
directly: they decode a frame into a :class:`QImage` (which is safe off the GUI
thread) and hand over with ``QTimer.singleShot(0, ...)``, and every control
message is marshalled the same way.  No dialog is ever opened from a callback,
and nothing blocks the event loop.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PyQt5.QtCore import QEvent, QPoint, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QImage, QPainter, QPixmap
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from gui.settings_schema import load_settings, save_settings
from gui.theme import (
    T,
    checkbox_style,
    combo_style,
    input_style,
    spinbox_style,
    splitter_style,
)
from gui.widgets import Card, StatusIndicator
from vm_harness.guest_input import (
    _CONTROL_KEYS,
    UnsupportedKeyError,
    key_for,
)

#: Where non-secret console preferences live, alongside the rest of the GUI's
#: settings.  The bridge token is deliberately NOT here -- it is a credential,
#: and goes through :class:`gui.credential_store.CredentialStore` instead.
SETTINGS_PATH = "gui/settings.json"
SETTINGS_KEY = "vm_console"

#: CredentialStore entry name for the stream bridge token.
TOKEN_CREDENTIAL_NAME = "Streaming bridge token"

#: Env var the bridge reads its token from, and therefore the first place this
#: panel looks.
TOKEN_ENV = "VMHARNESS_BRIDGE_TOKEN"

#: Bridges this panel knows about.  The Python bridge is the reference server;
#: the Rust sidecar implements the same protocol on 8446.
BRIDGE_PYTHON_URL = "ws://127.0.0.1:8445/ws/stream"
BRIDGE_SIDECAR_URL = "ws://127.0.0.1:8446/ws/stream"

BRIDGE_PYTHON = "Python bridge (streaming_bridge.py :8445)"
BRIDGE_SIDECAR = "Rust sidecar (continuum :8446)"
BRIDGE_CUSTOM = "Custom URL..."

MIN_FPS = 1
MAX_FPS = 60
MIN_QUALITY = 1
MAX_QUALITY = 100
MIN_DIMENSION = 64
MAX_WIDTH = 3840
MAX_HEIGHT = 2160

#: Modifier that turns a drag into view panning instead of pointer movement.
PAN_MODIFIER = Qt.ControlModifier

#: Minimum gap between two keystrokes sent from this panel.  The bridge paces
#: `sendkey` at 50ms behind a per-VM lock, so an unbounded flood of auto-repeats
#: does not type faster -- it queues tasks that keep typing long after the key
#: was released.
KEY_THROTTLE_SEC = 0.04

STATS_INTERVAL_MS = 2000
STALE_FRAME_SEC = 15.0

#: Qt key code -> the name to look up in ``_CONTROL_KEYS``.
_CONTROL_KEY_BY_QT: Dict[int, str] = {
    Qt.Key_Shift: "shift",
    Qt.Key_Control: "ctrl",
    Qt.Key_Alt: "alt",
    Qt.Key_Meta: "meta",
}

#: Qt key code -> the alias ``_CONTROL_KEYS`` knows.
_CONTROL_ALIAS_BY_QT: Dict[int, str] = {
    Qt.Key_Return: "enter",
    Qt.Key_Enter: "enter",
    Qt.Key_Tab: "tab",
    Qt.Key_Escape: "esc",
    Qt.Key_Backspace: "backspace",
    Qt.Key_Up: "up",
    Qt.Key_Down: "down",
    Qt.Key_Left: "left",
    Qt.Key_Right: "right",
    Qt.Key_Home: "home",
    Qt.Key_End: "end",
    Qt.Key_Delete: "delete",
}

_MOUSE_BUTTON_NAMES = {
    Qt.LeftButton: "left",
    Qt.RightButton: "right",
    Qt.MiddleButton: "middle",
}


def _as_int(value: Any, default: int, low: int, high: int) -> int:
    """Coerce a JSON number to an int within bounds, or return ``default``."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(low, min(high, value))
    if isinstance(value, float) and value.is_integer():
        return max(low, min(high, int(value)))
    return default


class VMConsolePanel(QWidget):
    """Live per-VM console: frames in, pointer and keyboard out."""

    stats_updated = pyqtSignal(dict)
    vm_list_changed = pyqtSignal(list)
    status_message = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)

        self._ws = None
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_factory: Optional[Any] = None
        self._connected = False
        self._connecting = False
        self._authenticated = False
        self._paused = False
        self._panning = False

        self._last_frame_time: float = 0.0
        self._frames_received: int = 0
        self._bytes_received: int = 0
        self._frame_width: int = 0
        self._frame_height: int = 0
        self._acked_width: int = 0
        self._acked_height: int = 0
        self._acked_quality: int = 0
        self._acked_fps: int = 0
        self._acked_input_enabled: bool = True
        self._last_image: Optional[QImage] = None
        self._frame_update_queued = False
        self._frame_queued_at = 0.0
        # Frames arrive on the socket thread, so the repaint is bounced onto the
        # GUI thread with singleShot. _frame_update_queued collapses a burst of
        # frames into one repaint -- but if that callback is ever dropped, the
        # flag stays True and every later frame returns early, so the picture
        # freezes for good while the counters keep climbing. Anything that can
        # lose a queued call needs a way back: after this long, the next frame
        # repaints synchronously instead of trusting the queued one.
        self._frame_queue_timeout_sec = 1.0
        self._latency_ms: Optional[float] = None
        self._ping_sent_at: Dict[int, float] = {}

        self._server_vms: Optional[List[str]] = None
        self._local_vms: List[str] = []
        self._active_vm = ""
        self._send_lock = threading.Lock()
        self._pending: List[Dict[str, Any]] = []
        self._last_key_sent = 0.0
        self._loading_settings = False

        self._settings_path = SETTINGS_PATH

        self.setStyleSheet("background: " + T.BG_PRIMARY + ";")
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        root.addWidget(self._build_header())

        self._splitter = QSplitter(Qt.Vertical, self)
        self._splitter.setStyleSheet(splitter_style())
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setStretchFactor(0, 3)
        self._splitter.setStretchFactor(1, 2)
        self._build_video_pane()
        self._build_control_pane()
        root.addWidget(self._splitter, stretch=1)

        root.addWidget(self._build_status_row())

        self._health_timer = QTimer(self)
        self._health_timer.setInterval(5000)
        self._health_timer.timeout.connect(self._check_health)
        self._health_timer.start()

        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(STATS_INTERVAL_MS)
        self._stats_timer.timeout.connect(self._request_stats)

        self._config_timer = QTimer(self)
        self._config_timer.setSingleShot(True)
        self._config_timer.setInterval(250)
        self._config_timer.timeout.connect(self._send_config)

        self._load_settings()
        self._refresh_local_vms()
        self._refresh_url_label()
        self._set_placeholder("Disconnected - choose a VM and press Connect")
        self._apply_control_enabled_state()

    # ── Construction ─────────────────────────────────────────────────────────

    def _build_header(self) -> QWidget:
        header = QWidget()
        hl = QHBoxLayout(header)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(8)

        title = QLabel("VM Console")
        title.setStyleSheet(
            f"color: {T.TEXT_PRIMARY}; font-size: 16px; font-weight: bold;"
        )
        hl.addWidget(title)
        hl.addStretch()

        self._vm_label = QLabel("no VM selected")
        self._vm_label.setStyleSheet(
            f"color: {T.TEXT_ACCENT}; font-size: 12px; font-weight: 600;"
        )
        hl.addWidget(self._vm_label)

        self._frame_label = QLabel("0 frames")
        self._frame_label.setStyleSheet(
            f"color: {T.TEXT_MUTED}; font-size: 11px;"
        )
        hl.addWidget(self._frame_label)

        return header

    def _build_video_pane(self) -> None:
        display_card = Card("Console Output")
        self._splitter.addWidget(display_card)

        self._scene = QGraphicsScene(self)
        self._pixmap_item: Optional[QGraphicsPixmapItem] = None
        self._placeholder_text: Optional[Any] = None

        self._graphics_view = QGraphicsView(self._scene)
        self._graphics_view.setRenderHints(
            QPainter.SmoothPixmapTransform | QPainter.Antialiasing
        )
        self._graphics_view.setFrameShape(QFrame.NoFrame)
        self._graphics_view.setStyleSheet(
            f"QGraphicsView {{"
            f"  background: {T.BG_SECONDARY};"
            f"  border: 1px solid {T.BG_TERTIARY};"
            f"  border-radius: 6px;"
            f"}}"
        )
        self._graphics_view.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding
        )
        self._graphics_view.setMinimumHeight(240)
        self._graphics_view.setAlignment(Qt.AlignCenter)
        self._graphics_view.setMouseTracking(True)
        self._graphics_view.setFocusPolicy(Qt.StrongFocus)
        self._graphics_view.setToolTip(
            "Click and type to drive the guest.\n"
            "Hold Ctrl and drag to pan the view instead of moving the pointer.\n"
            "The scroll wheel scrolls inside the guest."
        )
        self._graphics_view.viewport().setMouseTracking(True)

        # No ScrollHandDrag: with it set, a press-drag pans the view and the
        # guest pointer never moves, which reads as "the VM ignores my mouse".
        self._graphics_view.setDragMode(QGraphicsView.NoDrag)
        # Both objects, not just the view. QAbstractScrollArea re-sends mouse
        # and resize events from its viewport up to the view but does NOT do so
        # for wheel events, so a filter on the view alone would never see the
        # scroll wheel.
        self._graphics_view.installEventFilter(self)
        self._graphics_view.viewport().installEventFilter(self)

        display_card.content_layout.addWidget(self._graphics_view, stretch=1)

    def _build_control_pane(self) -> None:
        card = Card("Controls & Settings")
        card.setMinimumHeight(150)
        self._splitter.addWidget(card)

        self._status_indicator = StatusIndicator(QColor(T.DOT_OFFLINE))
        self._status_label = QLabel("Disconnected")
        self._status_label.setStyleSheet(
            f"color: {T.STATUS_STOPPED}; font-size: 12px;"
        )

        self._btn_connect = self._make_button("Connect", T.BRAND, T.BRAND_HOVER)
        self._btn_connect.clicked.connect(self._connect)

        self._btn_disconnect = self._make_button(
            "Disconnect", T.ERROR, "#dc2626", disabled_bg=T.ERROR_BG
        )
        self._btn_disconnect.setEnabled(False)
        self._btn_disconnect.clicked.connect(self._disconnect)

        self._btn_pause = self._ghost_button("Pause")
        self._btn_pause.setToolTip(
            "Stop painting new frames. The stream keeps running and the frame "
            "counter keeps climbing; only the picture freezes."
        )
        self._btn_pause.clicked.connect(self._toggle_pause)

        self._btn_shot = self._ghost_button("Screenshot")
        self._btn_shot.setToolTip("Save the current frame as a PNG file")
        self._btn_shot.clicked.connect(self._save_screenshot)

        self._btn_maximize = self._ghost_button("Maximize")
        self._btn_maximize.setToolTip("Show the console full screen")
        self._btn_maximize.clicked.connect(self._toggle_maximize)

        self._btn_refresh = self._ghost_button("Refresh")
        self._btn_refresh.setToolTip(
            "Re-send the current config, re-subscribe to the VM and ping the "
            "bridge"
        )
        self._btn_refresh.clicked.connect(self._refresh)

        row1 = QWidget()
        r1 = QHBoxLayout(row1)
        r1.setContentsMargins(0, 0, 0, 0)
        r1.setSpacing(10)
        r1.addWidget(self._status_indicator, alignment=Qt.AlignVCenter)
        r1.addWidget(self._status_label)
        r1.addStretch()
        for button in (
            self._btn_connect,
            self._btn_disconnect,
            self._btn_pause,
            self._btn_shot,
            self._btn_refresh,
            self._btn_maximize,
        ):
            r1.addWidget(button)
        card.content_layout.addWidget(row1)

        self._vm_combo = QComboBox()
        self._vm_combo.setStyleSheet(combo_style())
        self._vm_combo.currentTextChanged.connect(self._on_vm_changed)
        self._vm_combo.setToolTip(
            "Which VM to stream. The list is reconciled from the bridge's own "
            "registry (~/.qemu-mcp/vms) and the GUI's VM store "
            "(~/.qemu-mcp/vm-configs), which are different directories and can "
            "disagree. Only a VM the bridge lists has a QMP endpoint to capture "
            "from, so that list wins; the rest are shown marked."
        )

        self._bridge_combo = QComboBox()
        self._bridge_combo.setStyleSheet(combo_style())
        self._bridge_combo.addItems([BRIDGE_PYTHON, BRIDGE_SIDECAR, BRIDGE_CUSTOM])
        self._bridge_combo.currentTextChanged.connect(self._on_bridge_changed)

        self._url_input = QLineEdit(BRIDGE_PYTHON_URL)
        self._url_input.setStyleSheet(input_style())
        self._url_input.setFont(QFont("Consolas", 10))
        self._url_input.editingFinished.connect(self._on_url_edited)

        self._token_input = QLineEdit()
        self._token_input.setStyleSheet(input_style())
        self._token_input.setFont(QFont("Consolas", 10))
        self._token_input.setEchoMode(QLineEdit.Password)
        self._token_input.setPlaceholderText(f"defaults to ${TOKEN_ENV}")
        self._token_input.editingFinished.connect(self._on_token_edited)
        self._token_input.setToolTip(
            'Sent as {"type":"auth","key":...}. Leave empty to fall back to '
            f"${TOKEN_ENV}. Saved encrypted, never in settings.json."
        )

        self._show_token = QCheckBox("Show")
        self._show_token.setStyleSheet(checkbox_style())
        self._show_token.toggled.connect(self._on_show_token)

        row2 = QWidget()
        r2 = QHBoxLayout(row2)
        r2.setContentsMargins(0, 0, 0, 0)
        r2.setSpacing(8)
        r2.addWidget(QLabel("VM"))
        r2.addWidget(self._vm_combo, stretch=1)
        r2.addWidget(QLabel("Bridge"))
        r2.addWidget(self._bridge_combo, stretch=1)
        r2.addWidget(self._url_input, stretch=2)
        r2.addWidget(QLabel("Token"))
        r2.addWidget(self._token_input, stretch=1)
        r2.addWidget(self._show_token)
        card.content_layout.addWidget(row2)

        self._fps_spin = self._make_spin(MIN_FPS, MAX_FPS)
        self._quality_spin = self._make_spin(MIN_QUALITY, MAX_QUALITY)
        self._width_spin = self._make_spin(MIN_DIMENSION, MAX_WIDTH, step=64)
        self._height_spin = self._make_spin(MIN_DIMENSION, MAX_HEIGHT, step=64)
        self._input_check = QCheckBox("Send input to the guest")
        self._input_check.setStyleSheet(checkbox_style())
        self._input_check.setToolTip(
            "Cleared, the bridge refuses every key, click, move and scroll "
            "event for this session. Useful when someone else is driving the "
            "same VM."
        )
        for spin in (self._fps_spin, self._quality_spin,
                     self._width_spin, self._height_spin):
            spin.valueChanged.connect(self._on_config_field_changed)
        self._input_check.toggled.connect(self._on_config_field_changed)

        row3 = QWidget()
        r3 = QHBoxLayout(row3)
        r3.setContentsMargins(0, 0, 0, 0)
        r3.setSpacing(8)
        r3.addWidget(QLabel("FPS"))
        r3.addWidget(self._fps_spin)
        r3.addWidget(QLabel("JPEG quality"))
        r3.addWidget(self._quality_spin)
        r3.addWidget(QLabel("Frame"))
        r3.addWidget(self._width_spin)
        r3.addWidget(QLabel("x"))
        r3.addWidget(self._height_spin)
        r3.addWidget(self._input_check)
        r3.addStretch()
        card.content_layout.addWidget(row3)

        self._ack_label = QLabel("config: not sent yet")
        self._ack_label.setStyleSheet(
            f"color: {T.TEXT_MUTED}; font-size: 11px;"
            f" font-family: Consolas, monospace;"
        )
        self._ack_label.setWordWrap(True)
        card.content_layout.addWidget(self._ack_label)

        self._error_label = QLabel("")
        self._error_label.setStyleSheet(
            f"color: {T.ERROR}; font-size: 11px; font-family: Consolas, monospace;"
        )
        self._error_label.setWordWrap(True)
        card.content_layout.addWidget(self._error_label)

    def _build_status_row(self) -> QWidget:
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(16)

        self._url_label = QLabel(BRIDGE_PYTHON_URL)
        self._url_label.setStyleSheet(
            f"color: {T.TEXT_MUTED}; font-size: 11px;"
            f" font-family: Consolas, monospace;"
        )
        self._url_label.setToolTip("The endpoint currently in force")
        rl.addWidget(self._url_label)

        self._res_label = QLabel("-")
        self._res_label.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 11px;")
        self._res_label.setToolTip(
            "Frame size in force. The bridge scales the framebuffer to the size "
            "it acks, and the coordinate mapping uses the acked numbers."
        )
        rl.addWidget(self._res_label)

        self._fps_label = QLabel("-")
        self._fps_label.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 11px;")
        self._fps_label.setToolTip("Frames per second reported by the bridge")
        rl.addWidget(self._fps_label)

        self._bytes_label = QLabel("-")
        self._bytes_label.setStyleSheet(
            f"color: {T.TEXT_MUTED}; font-size: 11px;"
        )
        self._bytes_label.setToolTip("Bytes the bridge reports it has sent")
        rl.addWidget(self._bytes_label)

        self._latency_label = QLabel("-")
        self._latency_label.setStyleSheet(
            f"color: {T.TEXT_MUTED}; font-size: 11px;"
        )
        self._latency_label.setToolTip("Round trip time of the last ping")
        rl.addWidget(self._latency_label)

        rl.addStretch()

        self._time_label = QLabel("")
        self._time_label.setStyleSheet(
            f"color: {T.TEXT_MUTED}; font-size: 11px;"
        )
        rl.addWidget(self._time_label)

        return row

    # ── Widget factories ─────────────────────────────────────────────────────

    def _make_button(
        self,
        text: str,
        color: str,
        hover: str,
        disabled_bg: str = "",
    ) -> QPushButton:
        button = QPushButton(text)
        button.setFixedHeight(32)
        button.setCursor(Qt.PointingHandCursor)
        off = disabled_bg or (color + "20")
        button.setStyleSheet(
            f"QPushButton {{ background: {color}; color: white; border: none;"
            f" border-radius: 6px; font-size: 12px; font-weight: 600;"
            f" padding: 0 16px; }}"
            f"QPushButton:hover {{ background: {hover}; }}"
            f"QPushButton:disabled {{ background: {off}; color: {T.TEXT_MUTED}; }}"
        )
        return button

    def _ghost_button(self, text: str) -> QPushButton:
        button = QPushButton(text)
        button.setFixedHeight(32)
        button.setCursor(Qt.PointingHandCursor)
        button.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.TEXT_SECONDARY};"
            f" border: 1px solid {T.BG_TERTIARY}; border-radius: 6px;"
            f" font-size: 12px; padding: 0 14px; }}"
            f"QPushButton:hover {{ color: {T.TEXT_PRIMARY};"
            f" border-color: {T.BRAND}; }}"
            f"QPushButton:disabled {{ color: {T.TEXT_MUTED};"
            f" border-color: {T.BG_TERTIARY}; }}"
        )
        return button

    def _make_spin(self, low: int, high: int, step: int = 1) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setSingleStep(step)
        spin.setAccelerated(True)
        spin.setStyleSheet(spinbox_style())
        return spin

    # ── Settings persistence ─────────────────────────────────────────────────

    def _stored_settings(self) -> Dict[str, Any]:
        data = load_settings(self._settings_path)
        stored = data.get(SETTINGS_KEY)
        return dict(stored) if isinstance(stored, dict) else {}

    def _load_settings(self) -> None:
        """Read persisted preferences, falling back to the bridge's defaults."""
        self._loading_settings = True
        try:
            stored = self._stored_settings()

            url = str(stored.get("bridge_url") or BRIDGE_PYTHON_URL)
            self._url_input.setText(url)
            self._bridge_combo.blockSignals(True)
            self._bridge_combo.setCurrentText(self._bridge_for_url(url))
            self._bridge_combo.blockSignals(False)

            self._fps_spin.setValue(_as_int(stored.get("fps"), 30, MIN_FPS, MAX_FPS))
            self._quality_spin.setValue(
                _as_int(stored.get("quality"), 85, MIN_QUALITY, MAX_QUALITY)
            )
            self._width_spin.setValue(
                _as_int(stored.get("width"), 1280, MIN_DIMENSION, MAX_WIDTH)
            )
            self._height_spin.setValue(
                _as_int(stored.get("height"), 800, MIN_DIMENSION, MAX_HEIGHT)
            )
            self._input_check.setChecked(
                bool(stored.get("input_enabled", True))
            )
            self._token_input.setText(self._load_token())
        finally:
            self._loading_settings = False

    def _save_settings(self) -> None:
        """Persist non-secret preferences, skipping a no-op write."""
        wanted = {
            "bridge_url": self._url_input.text().strip(),
            "fps": self._fps_spin.value(),
            "quality": self._quality_spin.value(),
            "width": self._width_spin.value(),
            "height": self._height_spin.value(),
            "input_enabled": bool(self._input_check.isChecked()),
        }
        if self._stored_settings() == wanted:
            return
        data = load_settings(self._settings_path)
        data[SETTINGS_KEY] = wanted
        try:
            save_settings(data, self._settings_path)
        except OSError as exc:
            self._hint(f"Could not save console settings: {exc}")

    # ── Token handling ───────────────────────────────────────────────────────

    @staticmethod
    def _load_token() -> str:
        """The token to send, preferring an explicitly saved credential.

        Order is stored credential, then ``VMHARNESS_BRIDGE_TOKEN``.  The env
        var is what the bridge itself reads, so an operator who set it never
        has to touch this panel; the credential exists for the case where they
        did not, and the field below it overrides both.
        """
        try:
            from gui.credential_store import CredentialStore

            credential = CredentialStore().get(TOKEN_CREDENTIAL_NAME)
            if credential is not None and credential.value:
                return str(credential.value)
        except Exception:
            pass
        return os.environ.get(TOKEN_ENV, "").strip()

    def _store_token(self, token: str) -> None:
        try:
            from gui.credential_store import CredentialStore

            store = CredentialStore()
            if not token:
                store.delete(TOKEN_CREDENTIAL_NAME)
                return
            description = f"Token for {self._url_input.text().strip()}"
            if store.get(TOKEN_CREDENTIAL_NAME) is not None:
                store.update(TOKEN_CREDENTIAL_NAME, token, description)
            else:
                store.add(
                    name=TOKEN_CREDENTIAL_NAME,
                    credential_type="token",
                    value=token,
                    description=description,
                )
        except Exception as exc:
            self._hint(
                f"Token not saved ({exc}); it stays set for this session only"
            )

    def _current_token(self) -> str:
        return self._token_input.text().strip()

    def _on_token_edited(self) -> None:
        self._store_token(self._current_token())

    def _on_show_token(self, show: bool) -> None:
        self._token_input.setEchoMode(
            QLineEdit.Normal if show else QLineEdit.Password
        )

    # ── URL / bridge handling ────────────────────────────────────────────────

    @staticmethod
    def _bridge_for_url(url: str) -> str:
        if url == BRIDGE_PYTHON_URL:
            return BRIDGE_PYTHON
        if url == BRIDGE_SIDECAR_URL:
            return BRIDGE_SIDECAR
        return BRIDGE_CUSTOM

    def _on_bridge_changed(self, name: str) -> None:
        if self._loading_settings:
            return
        if name == BRIDGE_PYTHON:
            self._url_input.setText(BRIDGE_PYTHON_URL)
        elif name == BRIDGE_SIDECAR:
            self._url_input.setText(BRIDGE_SIDECAR_URL)
        self._refresh_url_label()

    def _on_url_edited(self) -> None:
        self._refresh_url_label()

    def _refresh_url_label(self) -> None:
        self._url_label.setText(self._url_input.text().strip())

    # ── VM selection ─────────────────────────────────────────────────────────

    def _refresh_local_vms(self) -> None:
        """Enumerate what the GUI's own VM store knows, for reconciliation."""
        names: List[str] = []
        try:
            from gui.multi_vm import MultiVMManager

            manager = MultiVMManager()
            names = list(manager.list_vms())
            for name in names:
                try:
                    manager.get_vm(name)
                    manager.get_qmp_uri(name)
                    manager.get_status(name)
                except Exception:
                    pass
        except Exception:
            names = []
        if names != self._local_vms:
            self._local_vms = names
            self._rebuild_vm_combo()

    def _populate_vms(self, server_vms: Optional[List[str]]) -> None:
        """Adopt the bridge's ``vm_list`` as the truth for what is streamable."""
        self._server_vms = list(server_vms) if server_vms is not None else None
        self._rebuild_vm_combo()

    def _rebuild_vm_combo(self) -> None:
        previous = self._vm_combo.currentText().split("  ")[0]
        streamable = set(self._server_vms or [])
        names = sorted(set(self._local_vms) | streamable)

        self._vm_combo.blockSignals(True)
        self._vm_combo.clear()
        for name in names:
            if self._server_vms is not None and name not in streamable:
                self._vm_combo.addItem(f"{name}  (not streamable)")
            else:
                self._vm_combo.addItem(name)
        self._vm_combo.blockSignals(False)

        if self._server_vms is not None and previous not in streamable:
            previous = next((n for n in names if n in streamable), "")
        if previous not in names:
            previous = names[0] if names else ""
        self.set_vm(previous)

    def selected_vm(self) -> str:
        return self._vm_combo.currentText().split("  ")[0].strip()

    def _index_for_vm(self, name: str) -> int:
        """Combo index for ``name``, matching the decorated labels too."""
        index = self._vm_combo.findText(name)
        if index >= 0:
            return index
        for i in range(self._vm_combo.count()):
            if self._vm_combo.itemText(i).split("  ")[0] == name:
                return i
        return -1

    def set_vm(self, name: str) -> None:
        """Select ``name`` in the picker without re-entering the change handler."""
        index = self._index_for_vm(name)
        if index < 0:
            return
        self._vm_combo.blockSignals(True)
        self._vm_combo.setCurrentIndex(index)
        self._vm_combo.blockSignals(False)
        self._on_vm_changed(name)

    def switch_to_vm(self, vm_name: str) -> None:
        """Follow the window-wide active VM selection (``main_window`` hook)."""
        if not vm_name:
            return
        if self._index_for_vm(vm_name) < 0:
            self._refresh_local_vms()
        if self._index_for_vm(vm_name) < 0:
            self._hint(f"{vm_name} is not in the console's VM list")
            return
        self.set_vm(vm_name)

    def _on_vm_changed(self, _text: str) -> None:
        name = self.selected_vm()
        self._vm_label.setText(name or "no VM selected")
        if name == self._active_vm:
            return
        self._active_vm = name
        if self._authenticated:
            self._send_subscribe()

    # ── Configuration messages ───────────────────────────────────────────────

    def requested_config(self) -> Dict[str, Any]:
        vm = self.selected_vm()
        payload: Dict[str, Any] = {
            "type": "config",
            "quality": self._quality_spin.value(),
            "fps": self._fps_spin.value(),
            "width": self._width_spin.value(),
            "height": self._height_spin.value(),
            "input_enabled": bool(self._input_check.isChecked()),
        }
        if vm:
            payload["vm"] = vm
        return payload

    def _on_config_field_changed(self, *_args: Any) -> None:
        if self._loading_settings:
            return
        if self._authenticated:
            self._config_timer.start()
        self._save_settings()

    def _send_config(self) -> None:
        payload = self.requested_config()
        if not self._send_json(payload):
            return
        self._ack_label.setStyleSheet(
            f"color: {T.TEXT_MUTED}; font-size: 11px;"
            f" font-family: Consolas, monospace;"
        )
        self._ack_label.setText(
            f"config requested: {payload['width']}x{payload['height']} @ "
            f"{payload['fps']}fps, quality {payload['quality']}, input "
            f"{'on' if payload['input_enabled'] else 'off'} - awaiting ack"
        )

    def _send_subscribe(self) -> None:
        vm = self.selected_vm()
        if vm:
            self._send_json({"type": "subscribe", "vm": vm})

    def _request_stats(self) -> None:
        if not self._authenticated:
            return
        self._send_json({"type": "stats_request"})
        self._send_ping()

    def _send_ping(self) -> None:
        stamp = int(time.time() * 1000)
        with self._send_lock:
            self._ping_sent_at[stamp] = time.time()
            if len(self._ping_sent_at) > 16:
                for key in sorted(self._ping_sent_at)[:-16]:
                    self._ping_sent_at.pop(key, None)
        self._send_json({"type": "ping", "time": stamp})

    # ── Connection management ────────────────────────────────────────────────

    def _connect(self) -> None:
        if self._connecting or self._connected:
            return

        self._connecting = True
        self._btn_connect.setEnabled(False)
        self._btn_connect.setText("Connecting…")
        self._set_status("Connecting…", T.WARNING)

        try:
            import websocket
        except ImportError:
            self._connecting = False
            self._set_status("websocket-client is not installed", T.ERROR)
            self._apply_control_enabled_state()
            return

        url = self._url_input.text().strip()
        if not url:
            self._connecting = False
            self._set_status("No bridge URL", T.ERROR)
            self._apply_control_enabled_state()
            return

        factory = self._ws_factory or websocket.WebSocketApp
        self._url_label.setText(url)
        self._save_settings()

        try:
            self._ws = factory(
                url,
                on_open=self._on_ws_open,
                on_message=self._on_ws_message,
                on_error=self._on_ws_error,
                on_close=self._on_ws_close,
            )
            self._ws_thread = threading.Thread(
                target=self._run_forever,
                name="vm-console-ws",
                daemon=True,
            )
            self._ws_thread.start()
        except Exception as exc:
            self._connecting = False
            self._ws = None
            self._on_connection_failed(str(exc))

    def _run_forever(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except TypeError:
            try:
                ws.run_forever()
            except Exception:
                pass
        except Exception:
            pass

    def _disconnect(self) -> None:
        ws, self._ws = self._ws, None
        self._ws_thread = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        with self._send_lock:
            self._pending.clear()
            self._authenticated = False
        self._connected = False
        self._connecting = False
        self._stats_timer.stop()
        self._update_disconnected_state()

    def _refresh(self) -> None:
        if not self._authenticated:
            return
        self._send_config()
        self._send_subscribe()
        self._send_ping()

    def _send_json(self, payload: Dict[str, Any]) -> bool:
        """Put one message on the wire, queueing it if auth has not landed.

        The bridge hangs up on any first message that is not ``auth``, so a
        keystroke that raced the handshake would cost the connection.  The queue
        is drained the moment ``auth_ok`` arrives.
        """
        with self._send_lock:
            ws = self._ws
            if ws is None:
                return False
            if not self._authenticated and payload.get("type") != "auth":
                self._pending.append(payload)
                return True
            try:
                ws.send(json.dumps(payload))
            except Exception:
                return False
        return True

    # ── WebSocket callbacks (socket thread) ──────────────────────────────────

    def _on_ws_open(self, ws) -> None:
        """Socket open. Auth goes out here, on this thread, before anything else.

        Sending it inline rather than through the main thread is deliberate: a
        main-thread hop would leave a window in which a queued keystroke could
        overtake it.
        """
        self._ws = ws
        try:
            ws.send(json.dumps({"type": "auth", "key": self._current_token()}))
        except Exception as exc:
            QTimer.singleShot(0, lambda: self._handle_ws_error(str(exc)))
            return
        self._refresh_url_label()
        QTimer.singleShot(0, self._handle_connected)

    def _on_ws_message(self, ws, message) -> None:
        if isinstance(message, (bytes, bytearray)):
            self._handle_frame(bytes(message))
        else:
            self._handle_control_message(message)

    def _on_ws_error(self, ws, error) -> None:
        QTimer.singleShot(0, lambda: self._handle_ws_error(str(error)))

    def _on_ws_close(self, ws, close_status_code=None, close_msg=None) -> None:
        QTimer.singleShot(0, self._handle_disconnected)

    # ── Frame rendering (QImage off-thread, QPixmap on-thread) ───────────────

    def _handle_frame(self, data: bytes) -> None:
        if not data:
            return
        try:
            image = QImage.fromData(data, "JPEG")
        except Exception:
            return
        if image.isNull():
            return

        self._frame_width = image.width()
        self._frame_height = image.height()
        self._frames_received += 1
        self._bytes_received += len(data)
        self._last_frame_time = time.time()

        if self._frame_update_queued:
            # Recover rather than stay frozen: if the queued repaint is older
            # than the timeout it is never going to arrive.
            if (time.time() - self._frame_queued_at) < self._frame_queue_timeout_sec:
                return
            self._frame_update_queued = False
        self._frame_update_queued = True
        self._frame_queued_at = time.time()
        QTimer.singleShot(0, lambda: self._update_pixmap(image))

    def _update_pixmap(self, image: QImage) -> None:
        self._frame_update_queued = False
        self._last_image = image
        self._paint(image)
        self._update_frame_stats()

    def _paint(self, image: QImage) -> None:
        pixmap = QPixmap.fromImage(image)
        if self._pixmap_item is None:
            self._pixmap_item = self._scene.addPixmap(pixmap)
        else:
            self._pixmap_item.setPixmap(pixmap)
        if self._placeholder_text is not None:
            self._scene.removeItem(self._placeholder_text)
            self._placeholder_text = None
        self._refit()

    def _refit(self) -> None:
        item = self._pixmap_item
        if item is None:
            return
        if self._graphics_view.viewport().width() <= 0:
            return
        self._graphics_view.fitInView(item, Qt.KeepAspectRatio)

    def _update_frame_stats(self) -> None:
        self._frame_label.setText(
            f"{self._frames_received} frames"
            + ("  (paused)" if self._paused else "")
        )
        self._res_label.setText(
            f"{self._frame_width}x{self._frame_height}"
            if not self._acked_width
            else f"{self._acked_width}x{self._acked_height} acked"
        )
        self._bytes_label.setText(
            f"{self._bytes_received / (1024 * 1024):.1f} MB received"
        )
        if self._last_frame_time:
            self._time_label.setText(
                time.strftime("%H:%M:%S", time.localtime(self._last_frame_time))
            )

    # ── Control messages ─────────────────────────────────────────────────────

    def _handle_control_message(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(message, dict):
            return
        QTimer.singleShot(0, lambda: self._apply_control_message(message))

    def _apply_control_message(self, message: Dict[str, Any]) -> None:
        kind = message.get("type", "")

        if kind == "auth_ok":
            self._on_auth_ok()
        elif kind == "config_ack":
            self._on_config_ack(message)
        elif kind == "pong":
            self._on_pong(message)
        elif kind == "stats":
            self._on_stats(message)
        elif kind == "vm_list":
            vms = message.get("vms")
            if isinstance(vms, list):
                self._populate_vms([str(v) for v in vms])
                self.vm_list_changed.emit(list(self._server_vms or []))
        elif kind == "error":
            self._on_server_error(str(message.get("message", "error")))

    def _on_auth_ok(self) -> None:
        with self._send_lock:
            self._authenticated = True
            queued, self._pending = self._pending, []
        for payload in queued:
            self._send_json(payload)
        self._connected = True
        self._connecting = False
        self._error_label.clear()
        self._set_status("Authenticated - configuring...", T.STATUS_RUNNING)
        self._apply_control_enabled_state()
        self._send_config()
        self._send_subscribe()
        self._send_ping()
        self._stats_timer.start()
        self._graphics_view.setFocus(Qt.OtherFocusReason)

    def _on_config_ack(self, message: Dict[str, Any]) -> None:
        """The ack carries the values actually in force, after clamping.

        Those -- not the values asked for -- are the framebuffer the guest
        pointer is addressed in, so they are what the coordinate mapping uses.
        A clamp is reported rather than silently applied: a user who asks for
        120fps and gets 60 with no explanation concludes the bridge is broken.
        """
        requested = self.requested_config()
        acked_quality = _as_int(message.get("quality"), 0, 0, 10_000)
        acked_fps = _as_int(message.get("fps"), 0, 0, 10_000)
        acked_width = _as_int(message.get("width"), 0, 0, 10_000)
        acked_height = _as_int(message.get("height"), 0, 0, 10_000)

        self._acked_quality = acked_quality
        self._acked_fps = acked_fps
        self._acked_input_enabled = bool(requested["input_enabled"])
        if acked_width > 0 and acked_height > 0:
            self._acked_width = acked_width
            self._acked_height = acked_height

        self._res_label.setText(f"{acked_width}x{acked_height} acked")

        clamps = []
        for field, asked, granted in (
            ("fps", requested["fps"], acked_fps),
            ("quality", requested["quality"], acked_quality),
            ("width", requested["width"], acked_width),
            ("height", requested["height"], acked_height),
        ):
            if asked != granted:
                clamps.append(f"{field} clamped {asked} -> {granted}")

        text = (
            f"config in force: {acked_width}x{acked_height} @ {acked_fps}fps, "
            f"quality {acked_quality}, input "
            f"{'on' if self._acked_input_enabled else 'off'}"
        )
        if clamps:
            text += "  [" + "; ".join(clamps) + "]"
        self._ack_label.setText(text)
        self._ack_label.setStyleSheet(
            f"color: {T.WARNING if clamps else T.TEXT_MUTED}; font-size: 11px;"
            f" font-family: Consolas, monospace;"
        )
        self._set_status(
            f"Streaming {self.selected_vm() or 'no VM'}", T.STATUS_RUNNING
        )
        self._apply_control_enabled_state()

    def _on_pong(self, message: Dict[str, Any]) -> None:
        stamp = message.get("time")
        sent = None
        if isinstance(stamp, int):
            with self._send_lock:
                sent = self._ping_sent_at.pop(stamp, None)
        if sent is None:
            return
        self._latency_ms = max(0.0, (time.time() - sent) * 1000.0)
        self._latency_label.setText(f"{self._latency_ms:.0f} ms RTT")

    def _on_stats(self, message: Dict[str, Any]) -> None:
        frames = _as_int(message.get("frames_sent"), -1, 0, 10_000_000)
        sent_bytes = _as_int(message.get("bytes_sent"), -1, 0, 10_000_000_000)
        raw_fps = message.get("fps")
        fps = float(raw_fps) if isinstance(raw_fps, (int, float)) else None

        if frames >= 0:
            bridge_part = f"  |  bridge {frames}"
        else:
            bridge_part = ""
        self._frame_label.setText(
            f"{self._frames_received} frames"
            + ("  (paused)" if self._paused else "")
            + bridge_part
        )
        if sent_bytes >= 0:
            self._bytes_label.setText(
                f"{sent_bytes / (1024 * 1024):.1f} MB from bridge"
            )
        if fps is not None:
            self._fps_label.setText(f"{fps:.1f} fps")
        self.stats_updated.emit({
            "frames_sent": frames,
            "bytes_sent": sent_bytes,
            "fps": fps,
        })

    def _on_server_error(self, message: str) -> None:
        self._error_label.setText(message)
        self.status_message.emit(message)
        lowered = message.lower()
        if "unauthorized" in lowered or "authentication" in lowered:
            self._set_status(
                "Unauthorized - set the bridge token in the field below, or in "
                f"${TOKEN_ENV}", T.ERROR,
            )
        self._apply_control_enabled_state()

    # ── Input: widget coordinates to guest pixels ────────────────────────────

    def guest_size(self) -> Tuple[int, int]:
        """The framebuffer size that input must be addressed in.

        Prefers the acked size, because the bridge scales the framebuffer to it
        and then divides the guest pixel by exactly that number.  Falls back to
        the raw frame when no ack has arrived.
        """
        if self._acked_width > 0 and self._acked_height > 0:
            return self._acked_width, self._acked_height
        item = self._pixmap_item
        if item is not None and not item.pixmap().isNull():
            return item.pixmap().width(), item.pixmap().height()
        return self._frame_width, self._frame_height

    def widget_to_guest(self, pos: QPoint) -> Optional[Tuple[int, int]]:
        """Map a point in the view to guest framebuffer pixels.

        Returns ``None`` when the point is not over the frame, which is what the
        letterbox area looks like: there is no pixel there, so nothing is sent.
        See the module docstring for the derivation.
        """
        item = self._pixmap_item
        if item is None or item.pixmap().isNull():
            return None

        width, height = self.guest_size()
        if width <= 0 or height <= 0:
            return None

        scene_pos = self._graphics_view.mapToScene(pos)
        rect = item.sceneBoundingRect()
        if rect.width() <= 0 or rect.height() <= 0:
            return None
        if not rect.contains(scene_pos):
            return None

        x = int(round((scene_pos.x() - rect.left()) / rect.width() * width))
        y = int(round((scene_pos.y() - rect.top()) / rect.height() * height))
        return (
            max(0, min(width - 1, x)),
            max(0, min(height - 1, y)),
        )

    def _input_ready(self) -> bool:
        """True when there is a guest, a frame and permission to drive it.

        Deliberately does NOT require authentication: a click typed while the
        handshake is still in flight is real input, and :meth:`_send_json`
        queues it until ``auth_ok`` rather than throwing it away.
        """
        return (
            self._ws is not None
            and self._acked_input_enabled
            and bool(self.selected_vm())
            and self._pixmap_item is not None
        )

    def can_send_input(self) -> bool:
        """True when input reaches the guest immediately rather than queued."""
        return self._authenticated and self._input_ready()

    def _send_mouse_move(self, pos: QPoint) -> None:
        if not self._input_ready():
            return
        point = self.widget_to_guest(pos)
        if point is None:
            return
        self._send_json({
            "type": "input",
            "input_type": "mouse_move",
            "x": point[0],
            "y": point[1],
        })

    def _send_mouse_click(self, pos: QPoint, button: str, pressed: bool) -> None:
        if not self._input_ready():
            return
        point = self.widget_to_guest(pos)
        if point is None:
            return
        # A click does not move the pointer, so the position goes first.
        self._send_json({
            "type": "input",
            "input_type": "mouse_move",
            "x": point[0],
            "y": point[1],
        })
        self._send_json({
            "type": "input",
            "input_type": "mouse_click",
            "button": button,
            "pressed": pressed,
        })

    def _send_scroll(self, angle_dx: int, angle_dy: int) -> None:
        if not self._input_ready():
            return
        # 120 eighths of a degree per detent is Qt's own convention.
        dx = int(angle_dx / 120)
        dy = int(angle_dy / 120)
        if dx == 0 and dy == 0:
            return
        self._send_json({
            "type": "input",
            "input_type": "scroll",
            "dx": dx,
            "dy": dy,
        })

    # ── Input: keyboard ──────────────────────────────────────────────────────

    def key_name_for_event(self, event: Any) -> Optional[str]:
        """Resolve a ``QKeyEvent`` to the name to put in ``input.key``.

        Returns ``None`` for keys that carry no guest meaning (bare modifiers,
        dead keys) and for anything :func:`key_for` refuses, so a character
        that cannot be typed is reported rather than silently dropped.
        """
        qt_key = event.key()
        modifier_name = _CONTROL_KEY_BY_QT.get(qt_key)
        if modifier_name is not None:
            return _CONTROL_KEYS.get(modifier_name)

        alias = _CONTROL_ALIAS_BY_QT.get(qt_key)
        if alias is not None:
            return _CONTROL_KEYS[alias]

        text = event.text()
        if not text or text[0] in ("\x00", "\x1b"):
            return None
        try:
            return key_for(text[0])
        except UnsupportedKeyError as exc:
            self._hint(str(exc))
            return None

    def send_key(self, name: str) -> bool:
        """Send one keystroke.

        ``sendkey`` is an atomic press+release in HMP, so only the press is
        sent -- a release has nothing left to send, and the bridge drops it
        server-side for exactly that reason.
        """
        if not self._input_ready():
            return False
        now = time.time()
        if now - self._last_key_sent < KEY_THROTTLE_SEC:
            return False
        self._last_key_sent = now
        return self._send_json({
            "type": "input",
            "input_type": "key",
            "key": name,
            "pressed": True,
        })

    # ── Event filter: everything that becomes guest input ────────────────────

    def _set_pan_mode(self, event: Any) -> None:
        panning = bool(event.modifiers() & PAN_MODIFIER)
        self._panning = panning
        self._graphics_view.setDragMode(
            QGraphicsView.ScrollHandDrag if panning else QGraphicsView.NoDrag
        )
        if not panning:
            self._refit()

    def eventFilter(self, obj: Any, event: Any) -> bool:
        if obj not in (self._graphics_view, self._graphics_view.viewport()):
            return super().eventFilter(obj, event)

        kind = event.type()

        if kind == QEvent.MouseMove:
            if not self._panning:
                self._send_mouse_move(event.pos())
            return True

        if kind in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
            if self._panning or event.button() not in _MOUSE_BUTTON_NAMES:
                return False
            self._graphics_view.setFocus(Qt.MouseFocusReason)
            self._send_mouse_click(
                event.pos(),
                _MOUSE_BUTTON_NAMES[event.button()],
                kind == QEvent.MouseButtonPress,
            )
            return True

        if kind == QEvent.Wheel:
            if not self._input_ready():
                return False
            delta = event.angleDelta()
            self._send_scroll(delta.x(), delta.y())
            return True

        if kind == QEvent.KeyPress:
            self._set_pan_mode(event)
            name = self.key_name_for_event(event)
            if name is None:
                return False
            self.send_key(name)
            return True

        if kind == QEvent.KeyRelease:
            self._set_pan_mode(event)
            return False

        if kind == QEvent.Resize:
            self._refit()
            return False

        return super().eventFilter(obj, event)

    def keyPressEvent(self, event: Any) -> None:
        """Keystrokes that reach the panel rather than the view.

        Falling through here means the view does not hold focus.  The guest gets
        the key either way, and returning True for Tab and Escape stops this
        widget from eating them on the way to the view.
        """
        self._set_pan_mode(event)
        name = self.key_name_for_event(event)
        if name is not None:
            self.send_key(name)
            event.accept()
            return
        super().keyPressEvent(event)

    # ── Controls ─────────────────────────────────────────────────────────────

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self._btn_pause.setText("Resume" if self._paused else "Pause")
        if not self._paused and self._last_image is not None \
                and not self._last_image.isNull():
            self._paint(self._last_image)
        self._update_frame_stats()
        if not self._connected:
            self._set_status("Disconnected", T.STATUS_STOPPED)
        elif self._paused:
            self._set_status(
                "Paused - frames still arriving, picture frozen", T.WARNING
            )
        else:
            self._set_status(
                f"Streaming {self.selected_vm() or 'no VM'}", T.STATUS_RUNNING
            )

    def _save_screenshot(self) -> None:
        image = self._last_image
        if image is None or image.isNull():
            self._hint("No frame received yet - nothing to save")
            return
        vm = self.selected_vm() or "vm"
        default_dir = Path.home() / "Pictures"
        if not default_dir.is_dir():
            default_dir = Path.cwd()
        default = str(default_dir / f"{vm}-{time.strftime('%Y%m%d-%H%M%S')}.png")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save console screenshot", default, "PNG image (*.png)"
        )
        if not path:
            return
        try:
            if not image.save(path, "PNG"):
                raise OSError("Qt refused to write the file")
        except OSError as exc:
            self._hint(f"Screenshot failed: {exc}")
            return
        self._hint(f"Saved {path}")

    def _toggle_maximize(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self._btn_maximize.setText("Maximize")
        else:
            self.showFullScreen()
            self._btn_maximize.setText("Restore")
        self._refit()

    def showEvent(self, event: Any) -> None:
        self._refresh_local_vms()
        super().showEvent(event)

    # ── State helpers (main thread only) ─────────────────────────────────────

    def _set_status(self, text: str, color: str) -> None:
        self._status_label.setText(text)
        self._status_label.setStyleSheet(f"color: {color}; font-size: 12px;")

    def _hint(self, message: str) -> None:
        self._error_label.setText(message)
        self.status_message.emit(message)

    def _handle_connected(self) -> None:
        self._connected = True
        self._connecting = False
        if not self._authenticated:
            self._frames_received = 0
            self._bytes_received = 0
            self._acked_width = 0
            self._acked_height = 0

        self._status_indicator.set_status(running=True, connected=False)
        if not self._authenticated:
            self._set_status("Connected - authenticating…", T.WARNING)
        if self._pixmap_item is None:
            self._set_placeholder("Waiting for frames…")
        self._apply_control_enabled_state()

    def _handle_disconnected(self) -> None:
        self._connected = False
        self._connecting = False
        with self._send_lock:
            self._authenticated = False
        self._stats_timer.stop()
        self._update_disconnected_state()

    def _handle_ws_error(self, message: str) -> None:
        self._connected = False
        self._connecting = False
        with self._send_lock:
            self._authenticated = False
        self._stats_timer.stop()
        self._set_status(f"Error: {message[:80]}", T.ERROR)
        self._apply_control_enabled_state()

    def _on_connection_failed(self, message: str) -> None:
        self._connected = False
        self._connecting = False
        self._set_status(f"Failed: {message[:80]}", T.ERROR)
        self._apply_control_enabled_state()

    def _apply_control_enabled_state(self) -> None:
        live = self._connected or self._connecting
        self._btn_connect.setEnabled(not live)
        self._btn_connect.setText(
            "Connecting…" if self._connecting
            else ("Connected" if self._connected else "Connect")
        )
        self._btn_disconnect.setEnabled(live)
        self._btn_refresh.setEnabled(self._authenticated)
        self._btn_pause.setEnabled(self._connected)
        self._btn_shot.setEnabled(self._last_image is not None)
        self._vm_combo.setEnabled(bool(self._vm_combo.count()))

    def _update_disconnected_state(self) -> None:
        self._status_indicator.set_status(running=False, connected=False)
        self._set_status("Disconnected", T.STATUS_STOPPED)
        self._apply_control_enabled_state()
        self._set_placeholder("Disconnected - choose a VM and press Connect")

    def _set_placeholder(self, text: str) -> None:
        self._drop_scene()
        if self._last_image is not None and not self._last_image.isNull():
            self._paint(self._last_image)
        self._placeholder_text = self._scene.addText(text, QFont("Segoe UI", 13))
        self._placeholder_text.setDefaultTextColor(QColor(T.TEXT_MUTED))
        self._placeholder_text.setZValue(10)
        self._placeholder_text.setPos(20, 20)

    def _drop_scene(self) -> None:
        """Empty the scene, dropping the Python references with the items.

        ``QGraphicsScene.clear`` deletes the C++ items; leaving a stale Python
        wrapper behind turns the next ``removeItem`` into a RuntimeError.
        """
        self._scene.clear()
        self._pixmap_item = None
        self._placeholder_text = None

    def _check_health(self) -> None:
        if not self._connected or self._last_frame_time <= 0:
            return
        if time.time() - self._last_frame_time > STALE_FRAME_SEC:
            self._set_status(
                "Stale - no frames for 15s, reconnecting…", T.WARNING
            )
            self._disconnect()

    # ── Teardown ─────────────────────────────────────────────────────────────

    def closeEvent(self, event: Any) -> None:
        self._save_settings()
        self._disconnect()
        super().closeEvent(event)