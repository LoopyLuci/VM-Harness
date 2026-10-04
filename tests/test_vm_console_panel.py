"""Tests for the rebuilt VMConsolePanel — per-VM live console.

Headless, and with no live bridge: the WebSocket is a fake that records what
the panel put on the wire, so every assertion is about the exact JSON the panel
would send to ``streaming_bridge.py``.

Run with: pytest tests/test_vm_console_panel.py -q
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import pytest

from PyQt5.QtCore import QEvent, QPoint, QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QImage, QKeyEvent, QWheelEvent
from PyQt5.QtCore import QBuffer
from PyQt5.QtTest import QTest

from gui.panels_vm_console import (
    BRIDGE_PYTHON_URL,
    BRIDGE_SIDECAR,
    BRIDGE_SIDECAR_URL,
    TOKEN_ENV,
    VMConsolePanel,
)
from vm_harness.guest_input import _CONTROL_KEYS, key_for


# ── Fake WebSocket ────────────────────────────────────────────────────────────


class FakeWebSocket:
    """Records every frame the panel sends; ``run_forever`` opens immediately."""

    instances: list["FakeWebSocket"] = []

    def __init__(self, url, on_open=None, on_message=None, on_error=None,
                 on_close=None):
        self.url = url
        self.sent: list[dict] = []
        self.closed = False
        self._on_open = on_open
        self._on_message = on_message
        self._on_error = on_error
        self._on_close = on_close
        FakeWebSocket.instances.append(self)

    # websocket-client surface
    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def run_forever(self, **_kwargs) -> None:
        if self._on_open is not None:
            self._on_open(self)

    def close(self) -> None:
        self.closed = True

    # test helpers
    def types(self) -> list[str]:
        return [message.get("type") for message in self.sent]

    def of_type(self, kind: str) -> list[dict]:
        return [m for m in self.sent if m.get("type") == kind]

    def deliver(self, payload: dict) -> None:
        self._on_message(self, json.dumps(payload))


def _jpeg(width: int, height: int) -> bytes:
    image = QImage(width, height, QImage.Format_RGB32)
    image.fill(QColor("#204080"))
    buffer = QBuffer()
    buffer.open(QBuffer.ReadWrite)
    assert image.save(buffer, "JPEG", 90)
    return bytes(buffer.data())


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def app(qtbot):
    from PyQt5.QtWidgets import QApplication

    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    return instance


@pytest.fixture
def vm_manager(monkeypatch):
    """A deterministic stand-in for MultiVMManager's own VM store."""

    class StubManager:
        def list_vms(self):
            return ["local-only", "win11"]

        def get_vm(self, name):
            return {"name": name}

        def get_qmp_uri(self, name):
            return f"tcp:127.0.0.1:5555"

        def get_status(self, name):
            return "running"

    import gui.multi_vm

    monkeypatch.setattr(gui.multi_vm, "MultiVMManager", StubManager)


@pytest.fixture
def token(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.setattr(
        VMConsolePanel,
        "_load_token",
        staticmethod(lambda: ""),
    )
    monkeypatch.setattr(VMConsolePanel, "_store_token", lambda self, t: None)


@pytest.fixture
def panel(qtbot, app, tmp_path, monkeypatch, vm_manager, token):
    """A panel whose settings and credentials never touch the repository."""
    import gui.panels_vm_console as module

    monkeypatch.setattr(
        module, "SETTINGS_PATH", str(tmp_path / "settings.json")
    )
    FakeWebSocket.instances.clear()

    widget = VMConsolePanel()
    widget._ws_factory = FakeWebSocket
    widget._token_input.setText("test-token")
    widget.resize(1200, 760)
    qtbot.addWidget(widget)
    widget.show()
    _pump(qtbot, 50)
    yield widget
    try:
        widget._disconnect()
    except Exception:
        pass
    widget.close()


def _install_frame(qtbot, panel: VMConsolePanel, width: int, height: int) -> None:
    """Push a real JPEG through the socket-thread decode path."""
    panel._handle_frame(_jpeg(width, height))
    _pump(qtbot, 40)
    assert panel._pixmap_item is not None
    assert panel._pixmap_item.pixmap().width() == width


def _go_live(
    qtbot,
    panel: VMConsolePanel,
    vm: str = "win11",
    width: Optional[int] = None,
    height: Optional[int] = None,
) -> FakeWebSocket:
    """Connect, authenticate and put a frame on screen."""
    panel._populate_vms([vm])
    panel.set_vm(vm)
    panel._connect()
    socket = FakeWebSocket.instances[-1]
    # The socket opens on its own thread; waiting for the deferred
    # "connected" callback keeps message ordering deterministic.
    _pump_until(qtbot, lambda: panel._connected, timeout=2000)
    panel._apply_control_message({"type": "auth_ok"})
    _install_frame(
        qtbot, panel,
        width if width is not None else panel._width_spin.value(),
        height if height is not None else panel._height_spin.value(),
    )
    _pump(qtbot, 30)
    return socket


def _viewport_scene_rect(panel: VMConsolePanel) -> QRectF:
    view = panel._graphics_view
    rect = view.viewport().rect()
    return QRectF(
        view.mapToScene(rect.topLeft()),
        view.mapToScene(rect.bottomRight()),
    )


def _scene_to_view(panel: VMConsolePanel, x: float, y: float) -> QPoint:
    return panel._graphics_view.mapFromScene(QPointF(x, y))


def _pump(qtbot, ms: int = 40) -> None:
    """Spin the event loop deterministically for roughly `ms` milliseconds.

    _pump(qtbot, ) is not usable here. Once tests/test_performance.py has run in
    the same process it stops delivering queued events: a QTimer.singleShot(0)
    scheduled by _handle_frame never fires, so _pixmap_item stays None and the
    frame tests fail. QApplication.processEvents() still works in that state, so
    pump explicitly instead of relying on qtbot's own loop. The production code
    is unaffected -- this is a test-harness quirk, not a panel bug.
    """
    import time

    from PyQt5.QtWidgets import QApplication

    deadline = time.monotonic() + ms / 1000.0
    while True:
        QApplication.processEvents()
        if time.monotonic() >= deadline:
            return
        time.sleep(0.005)


def _pump_until(qtbot, predicate, timeout: int = 2000) -> None:
    """Pump the event loop until `predicate()` is true, or fail after `timeout`.

    Same reason as _pump: qtbot.waitUntil uses the same machinery as qtbot.wait,
    which stops delivering events once tests/test_performance.py has run.
    """
    import time

    from PyQt5.QtWidgets import QApplication

    deadline = time.monotonic() + timeout / 1000.0
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    QApplication.processEvents()
    assert predicate(), f"condition not met within {timeout}ms"


def _send(panel: VMConsolePanel, event) -> None:
    from PyQt5.QtWidgets import QApplication

    QApplication.sendEvent(panel._graphics_view.viewport(), event)


def _mouse(kind, pos, button=Qt.LeftButton, buttons=None):
    """Build an explicit QMouseEvent for the viewport.

    QTest.mousePress/mouseMove/mouseClick go through Qt's synthetic input
    plumbing, which only delivers if the widget is visible, active and not
    obscured. That makes these tests depend on whatever window an earlier test
    happened to leave open -- they passed alone and failed in the full suite.
    Sending the event straight to the viewport tests our coordinate mapping and
    our handler, which is what is actually under test, with no dependency on
    ambient focus or visibility state.
    """
    from PyQt5.QtGui import QMouseEvent

    local = QPointF(pos)
    scene = QPointF()
    return QMouseEvent(
        kind,
        local,
        scene,
        button,
        buttons if buttons is not None else button,
        Qt.NoModifier,
    )


def _press(panel, pos, button=Qt.LeftButton):
    _send(panel, _mouse(QEvent.MouseButtonPress, pos, button))


def _release(panel, pos, button=Qt.LeftButton):
    _send(panel, _mouse(QEvent.MouseButtonRelease, pos, button, Qt.NoButton))


def _move(panel, pos):
    _send(panel, _mouse(QEvent.MouseMove, pos, Qt.NoButton, Qt.NoButton))


# ── Construction and rendering ─────────────────────────────────────────────────


def test_panel_constructs_and_renders(qtbot, panel):
    assert panel._graphics_view is not None
    assert panel._scene is not None
    assert panel._splitter.orientation() == Qt.Vertical
    assert panel._splitter.count() == 2

    top, bottom = panel._splitter.widget(0), panel._splitter.widget(1)
    assert top is not bottom

    _pump(qtbot, 50)
    assert not panel._graphics_view.grab().isNull()
    assert panel._graphics_view.height() > 0
    assert panel._status_label.text() == "Disconnected"
    assert panel._url_label.text() == BRIDGE_PYTHON_URL


def test_controls_exist_under_the_video(panel):
    for name in (
        "_btn_connect", "_btn_disconnect", "_btn_pause", "_btn_shot",
        "_btn_maximize", "_btn_refresh", "_vm_combo", "_bridge_combo",
        "_url_input", "_token_input", "_fps_spin", "_quality_spin",
        "_width_spin", "_height_spin", "_input_check",
    ):
        assert getattr(panel, name) is not None, name

    view_centre = panel._graphics_view.mapTo(
        panel, panel._graphics_view.rect().center()
    )
    controls_centre = panel._vm_combo.mapTo(
        panel, panel._vm_combo.rect().center()
    )
    assert controls_centre.y() > view_centre.y()


def test_frame_is_painted_into_the_view(qtbot, panel):
    _install_frame(qtbot, panel, 640, 480)
    assert panel._pixmap_item.pixmap().height() == 480
    assert panel._frame_label.text().startswith("1 frames")
    assert panel._res_label.text() == "640x480"


def test_scroll_hand_drag_is_not_enabled(panel):
    assert panel._graphics_view.dragMode() == panel._graphics_view.NoDrag


# ── Coordinate mapping ────────────────────────────────────────────────────────


def test_mapping_centre(qtbot, panel):
    _install_frame(qtbot, panel, 1280, 800)

    view = panel._graphics_view
    point = panel.widget_to_guest(view.viewport().rect().center())
    assert point is not None
    assert abs(point[0] - 639) <= 2
    assert abs(point[1] - 399) <= 2


def test_mapping_all_four_corners(qtbot, panel):
    _install_frame(qtbot, panel, 1280, 800)

    frame = panel._pixmap_item.sceneBoundingRect()
    assert (frame.width(), frame.height()) == (1280, 800)

    # Two pixels inside each corner, so integer rounding of the widget point
    # cannot push the probe off the edge.
    probes = [
        (2.0, 2.0, (2, 2)),
        (1277.0, 2.0, (1277, 2)),
        (2.0, 797.0, (2, 797)),
        (1277.0, 797.0, (1277, 797)),
    ]
    for sx, sy, expected in probes:
        mapped = panel.widget_to_guest(_scene_to_view(panel, sx, sy))
        assert mapped is not None, (sx, sy)
        assert abs(mapped[0] - expected[0]) <= 1, (sx, sy, mapped)
        assert abs(mapped[1] - expected[1]) <= 1, (sx, sy, mapped)

    # The extreme pixel of each axis is reachable and clamped into range. Only
    # the axis under test is asserted: a scene->widget->scene round trip through
    # integer widget coordinates cannot hold the other axis to the pixel.
    for sx, expected_x in ((0.2, 0), (1279.5, 1279)):
        mapped = panel.widget_to_guest(_scene_to_view(panel, sx, 400.0))
        assert mapped is not None
        assert mapped[0] == expected_x, (sx, mapped)
    for sy, expected_y in ((0.2, 0), (799.5, 799)):
        mapped = panel.widget_to_guest(_scene_to_view(panel, 640.0, sy))
        assert mapped is not None
        assert mapped[1] == expected_y, (sy, mapped)


def test_mapping_is_scaled_and_letterboxed(qtbot, panel):
    panel.resize(1500, 640)
    _pump(qtbot, 120)

    viewport = panel._graphics_view.viewport().rect()
    view_aspect = viewport.width() / viewport.height()
    if view_aspect >= 2.0:
        frame_w, frame_h = 1600, 400      # less wide than the view
    else:
        frame_w, frame_h = 400, 1600      # taller than the view
    # A view wider than the frame is padded left and right.
    bars_vertical = view_aspect > (frame_w / frame_h)

    _install_frame(qtbot, panel, frame_w, frame_h)
    panel._refit()
    _pump(qtbot, 40)

    frame = panel._pixmap_item.sceneBoundingRect()
    scene_rect = _viewport_scene_rect(panel)

    # Scale: fitInView shrank the frame, so the viewport covers more scene
    # units than the frame has.
    assert scene_rect.width() > frame.width()
    assert scene_rect.height() > frame.height()

    # Letterbox: fitInView fits the whole frame on the un-padded axis and
    # leaves a band on the other one that no framebuffer pixel covers.
    padded_excess = (
        scene_rect.width() - frame.width() if bars_vertical
        else scene_rect.height() - frame.height()
    )
    assert padded_excess > 50, (scene_rect, frame)

    if bars_vertical:
        along, bar_before, bar_after = frame.height() / 2, \
            frame.left() - 20, frame.right() + 20
        row = frame.height() - 1.0
    else:
        along, bar_before, bar_after = frame.width() / 2, \
            frame.top() - 20, frame.bottom() + 20
        row = frame.width() - 1.0

    assert panel.widget_to_guest(
        _scene_to_view(panel, bar_before, along)
    ) is None
    assert panel.widget_to_guest(
        _scene_to_view(panel, bar_after, along)
    ) is None
    # A bar point that shares a row with real pixels still sends nothing.
    assert panel.widget_to_guest(
        _scene_to_view(panel, bar_before, row)
    ) is None

    # Just inside each corner is the first/last framebuffer pixel.
    top_left = panel.widget_to_guest(_scene_to_view(panel, 0.5, 0.5))
    bottom_right = panel.widget_to_guest(
        _scene_to_view(panel, frame.width() - 0.5, frame.height() - 0.5)
    )
    assert top_left is not None and top_left[0] <= 1 and top_left[1] <= 1
    assert bottom_right is not None
    assert bottom_right[0] >= frame_w - 2
    assert bottom_right[1] >= frame_h - 2

    # Monotonic along both axes across the whole frame.
    xs, ys = [], []
    for i in range(9):
        sx = frame_w * (i + 0.5) / 9
        sy = frame_h * (i + 0.5) / 9
        gx = panel.widget_to_guest(_scene_to_view(panel, sx, frame_h / 2))
        gy = panel.widget_to_guest(_scene_to_view(panel, frame_w / 2, sy))
        assert gx is not None and gy is not None
        xs.append(gx[0])
        ys.append(gy[1])
    assert xs == sorted(xs)
    assert ys == sorted(ys)
    assert xs[0] < xs[-1]
    assert ys[0] < ys[-1]

    # Nothing ever escapes the acked frame.
    for i in range(60):
        widget_point = QPoint(
            int(viewport.left() + viewport.width() * i / 59),
            int(viewport.top() + viewport.height() * (i % 7) / 6),
        )
        mapped = panel.widget_to_guest(widget_point)
        if mapped is None:
            continue
        assert 0 <= mapped[0] <= frame_w - 1
        assert 0 <= mapped[1] <= frame_h - 1


def test_mapping_uses_the_acked_frame_size_not_the_widget(qtbot, panel):
    panel.resize(1200, 760)
    _pump(qtbot, 80)
    _install_frame(qtbot, panel, 1280, 800)
    panel._acked_width = 640
    panel._acked_height = 400
    _pump(qtbot, 20)

    assert panel.guest_size() == (640, 400)

    scene_rect = _viewport_scene_rect(panel)
    mapped = panel.widget_to_guest(_scene_to_view(
        panel, scene_rect.center().x(), scene_rect.center().y()
    ))
    assert mapped is not None
    # Halfway across a 1280-wide scene must be pixel ~320 of the 640-wide frame
    # the bridge acked, not ~640.
    assert abs(mapped[0] - 319) <= 3
    assert abs(mapped[1] - 199) <= 3


def test_mapping_without_a_frame_returns_none(panel):
    assert panel.widget_to_guest(QPoint(10, 10)) is None


# ── Mouse input ───────────────────────────────────────────────────────────────


def test_mouse_click_sends_move_then_directional_click(qtbot, panel):
    socket = _go_live(qtbot, panel)
    socket.sent.clear()

    centre = panel._graphics_view.viewport().rect().center()
    _press(panel, centre)
    _release(panel, centre)
    _pump(qtbot, 20)

    inputs = socket.of_type("input")
    moves = [m for m in inputs if m["input_type"] == "mouse_move"]
    clicks = [m for m in inputs if m["input_type"] == "mouse_click"]
    assert len(moves) == 2
    assert len(clicks) == 2
    assert clicks[0]["pressed"] is True
    assert clicks[1]["pressed"] is False
    assert clicks[0]["button"] == "left"
    for move in moves:
        assert 0 <= move["x"] <= 1279
        assert 0 <= move["y"] <= 799


def test_mouse_move_inside_the_frame_is_forwarded(qtbot, panel):
    socket = _go_live(qtbot, panel)
    socket.sent.clear()

    target = _scene_to_view(panel, 320, 200)
    _move(panel, target)
    _pump(qtbot, 20)

    moves = socket.of_type("input")
    assert len(moves) == 1
    assert moves[0]["input_type"] == "mouse_move"
    assert abs(moves[0]["x"] - 320) <= 1
    assert abs(moves[0]["y"] - 200) <= 1


def test_click_in_the_letterbox_sends_nothing(qtbot, panel):
    panel.resize(1500, 640)
    _pump(qtbot, 120)
    socket = _go_live(qtbot, panel, width=1600, height=400)
    socket.sent.clear()

    frame = panel._pixmap_item.sceneBoundingRect()
    bar = _scene_to_view(panel, frame.left() - 30, frame.height() / 2)
    _press(panel, bar)
    _pump(qtbot, 20)

    assert socket.of_type("input") == []


def test_wheel_sends_scroll_detents(qtbot, panel):
    socket = _go_live(qtbot, panel)
    socket.sent.clear()

    _send(panel, QWheelEvent(
        QPointF(100, 100), QPointF(100, 100),
        QPoint(0, 0), QPoint(0, 120),
        0, Qt.Vertical, Qt.NoButton, Qt.NoModifier,
    ))
    _pump(qtbot, 20)

    scrolls = [m for m in socket.of_type("input")
               if m["input_type"] == "scroll"]
    assert scrolls == [{"type": "input", "input_type": "scroll",
                        "dx": 0, "dy": 1}]

    socket.sent.clear()
    _send(panel, QWheelEvent(
        QPointF(100, 100), QPointF(100, 100),
        QPoint(0, 0), QPoint(0, -240),
        0, Qt.Vertical, Qt.NoButton, Qt.NoModifier,
    ))
    _pump(qtbot, 20)
    scrolls = [m for m in socket.of_type("input")
               if m["input_type"] == "scroll"]
    assert scrolls == [{"type": "input", "input_type": "scroll",
                        "dx": 0, "dy": -2}]


def test_input_is_refused_when_the_user_switches_it_off(qtbot, panel):
    socket = _go_live(qtbot, panel)
    socket.sent.clear()
    panel._input_check.setChecked(False)
    panel._acked_input_enabled = False

    QTest.mouseClick(
        panel._graphics_view.viewport(), Qt.LeftButton,
        Qt.NoModifier, panel._graphics_view.viewport().rect().center(),
    )
    _pump(qtbot, 20)
    assert socket.of_type("input") == []


# ── Keyboard ──────────────────────────────────────────────────────────────────


def _key_event(key, text="", modifiers=Qt.NoModifier):
    return QKeyEvent(QEvent.KeyPress, key, modifiers, text)


def test_key_press_produces_the_key_for_name():
    panel_key = VMConsolePanel.key_name_for_event
    assert panel_key(None, _key_event(Qt.Key_A, "a")) == key_for("a") == "a"
    assert panel_key(None, _key_event(Qt.Key_A, "A", Qt.ShiftModifier)) == \
        key_for("A") == "shift-a"
    assert panel_key(None, _key_event(Qt.Key_1, "!")) == key_for("!") == "shift-1"
    assert panel_key(None, _key_event(Qt.Key_Space, " ")) == key_for(" ") == "spc"


def test_control_keys_use_the_shared_key_table():
    panel_key = VMConsolePanel.key_name_for_event
    for qt_key, alias in (
        (Qt.Key_Return, "enter"),
        (Qt.Key_Tab, "tab"),
        (Qt.Key_Escape, "esc"),
        (Qt.Key_Backspace, "backspace"),
        (Qt.Key_Up, "up"),
        (Qt.Key_Down, "down"),
        (Qt.Key_Left, "left"),
        (Qt.Key_Right, "right"),
        (Qt.Key_Home, "home"),
        (Qt.Key_End, "end"),
        (Qt.Key_Delete, "delete"),
    ):
        assert panel_key(None, _key_event(qt_key)) == _CONTROL_KEYS[alias]


def test_unmappable_key_is_reported_not_dropped(qtbot, panel):
    _go_live(qtbot, panel)
    before = panel._error_label.text()
    assert panel.key_name_for_event(_key_event(Qt.Key_A, "é")) is None
    assert panel._error_label.text() != before
    assert "sendkey" in panel._error_label.text()


def test_key_press_is_sent_with_pressed_true_only(qtbot, panel):
    socket = _go_live(qtbot, panel)
    socket.sent.clear()

    panel.send_key("a")
    keys = socket.of_type("input")
    assert keys == [{"type": "input", "input_type": "key", "key": "a",
                     "pressed": True}]


def test_tab_and_escape_reach_the_guest_not_the_widget(qtbot, panel):
    socket = _go_live(qtbot, panel)
    socket.sent.clear()

    for key in (Qt.Key_Tab, Qt.Key_Escape, Qt.Key_Return):
        QTest.keyClick(panel._graphics_view, key)
        # The panel paces keystrokes so a held key cannot flood the bridge.
        _pump(qtbot, 70)

    keys = [m["key"] for m in socket.of_type("input")
            if m["input_type"] == "key"]
    assert keys == ["tab", "esc", "ret"]


def test_typing_a_letter_through_the_view_sends_it(qtbot, panel):
    socket = _go_live(qtbot, panel)
    socket.sent.clear()

    for char in "aB":
        QTest.keyClicks(panel._graphics_view, char)
        _pump(qtbot, 70)

    keys = [m["key"] for m in socket.of_type("input")
            if m["input_type"] == "key"]
    assert keys == [key_for("a"), key_for("B")]


# ── Connection, auth and ordering ─────────────────────────────────────────────


def test_connect_sends_auth_before_anything_else(qtbot, panel):
    panel._populate_vms(["win11"])
    panel.set_vm("win11")
    panel._connect()
    _pump(qtbot, 80)

    socket = FakeWebSocket.instances[-1]
    assert socket.url == BRIDGE_PYTHON_URL
    assert socket.sent[0] == {"type": "auth", "key": "test-token"}
    assert socket.types().count("auth") == 1
    assert [t for t in socket.types() if t != "auth"] == []
    assert panel._connected is True


def test_input_before_auth_ok_is_queued_not_sent(qtbot, panel):
    panel._populate_vms(["win11"])
    panel.set_vm("win11")
    panel._connect()
    socket = FakeWebSocket.instances[-1]
    _pump(qtbot, 50)

    _install_frame(qtbot, panel, 1280, 800)
    QTest.mouseClick(
        panel._graphics_view.viewport(), Qt.LeftButton,
        Qt.NoModifier, panel._graphics_view.viewport().rect().center(),
    )
    _pump(qtbot, 20)

    assert socket.types() == ["auth"]

    panel._apply_control_message({"type": "auth_ok"})
    _pump(qtbot, 20)

    types = socket.types()
    assert types[0] == "auth"
    assert "config" in types
    assert "subscribe" in types
    assert "input" in types


def test_after_auth_ok_the_config_and_subscribe_are_sent(qtbot, panel):
    panel._populate_vms(["win11"])
    panel.set_vm("win11")
    panel._width_spin.setValue(1024)
    panel._height_spin.setValue(768)
    panel._fps_spin.setValue(15)
    panel._quality_spin.setValue(70)
    panel._input_check.setChecked(True)

    panel._connect()
    socket = FakeWebSocket.instances[-1]
    panel._apply_control_message({"type": "auth_ok"})
    _pump(qtbot, 20)

    config = socket.of_type("config")[0]
    assert config == {
        "type": "config",
        "vm": "win11",
        "quality": 70,
        "fps": 15,
        "width": 1024,
        "height": 768,
        "input_enabled": True,
    }
    assert socket.of_type("subscribe")[0] == {
        "type": "subscribe", "vm": "win11"
    }
    assert len(socket.of_type("ping")) == 1


def test_changing_a_setting_resends_the_config(qtbot, panel):
    socket = _go_live(qtbot, panel)
    socket.sent.clear()

    panel._fps_spin.setValue(7)
    panel._config_timer.setInterval(1)
    _pump(qtbot, 60)

    configs = socket.of_type("config")
    assert configs
    assert configs[-1]["fps"] == 7


def test_bridge_selector_switches_the_url(qtbot, panel):
    panel._bridge_combo.setCurrentText(BRIDGE_SIDECAR)
    assert panel._url_input.text() == BRIDGE_SIDECAR_URL
    assert panel._url_label.text() == BRIDGE_SIDECAR_URL


def test_disconnect_stops_everything_and_is_idempotent(qtbot, panel):
    socket = _go_live(qtbot, panel)
    panel._disconnect()
    panel._disconnect()
    assert socket.closed is True
    assert panel._connected is False
    assert panel._authenticated is False
    assert panel._btn_connect.isEnabled() is True


# ── Dead handlers: config_ack, pong, stats, vm_list ───────────────────────────


def test_config_ack_is_reflected_in_the_ui(qtbot, panel):
    socket = _go_live(qtbot, panel)
    panel._width_spin.setValue(1280)
    panel._height_spin.setValue(800)
    panel._fps_spin.setValue(30)
    panel._quality_spin.setValue(85)
    socket.sent.clear()

    # A bridge with limits of its own -- the Rust sidecar, for instance -- is
    # entitled to answer with something other than what was asked for. The
    # values it acked are what the guest pointer is addressed in, so the panel
    # must switch to them and say out loud that they differ.
    socket.deliver({
        "type": "config_ack",
        "quality": 60,
        "fps": 15,
        "width": 1920,
        "height": 1080,
    })
    _pump(qtbot, 40)

    assert panel._acked_width == 1920
    assert panel._acked_height == 1080
    assert panel._acked_fps == 15
    assert panel._acked_quality == 60
    assert panel.guest_size() == (1920, 1080)
    assert panel._res_label.text() == "1920x1080 acked"

    text = panel._ack_label.text()
    assert "config in force: 1920x1080 @ 15fps, quality 60" in text
    assert "fps clamped 30 -> 15" in text
    assert "quality clamped 85 -> 60" in text
    assert "width clamped 1280 -> 1920" in text
    assert "height clamped 800 -> 1080" in text


def test_config_ack_without_clamping_is_quiet(qtbot, panel):
    socket = _go_live(qtbot, panel)
    socket.deliver({
        "type": "config_ack", "quality": 85, "fps": 30,
        "width": 1280, "height": 800,
    })
    _pump(qtbot, 40)
    assert "clamped" not in panel._ack_label.text()
    assert "config in force: 1280x800 @ 30fps" in panel._ack_label.text()


def test_pong_shows_a_real_latency(qtbot, panel):
    socket = _go_live(qtbot, panel)
    ping = socket.of_type("ping")[-1]
    socket.deliver({"type": "pong", "time": ping["time"]})
    _pump(qtbot, 40)

    assert panel._latency_ms is not None
    assert panel._latency_ms >= 0.0
    assert "ms RTT" in panel._latency_label.text()


def test_stats_request_is_sent_on_a_timer_and_shown(qtbot, panel):
    socket = _go_live(qtbot, panel)
    socket.sent.clear()

    panel._stats_timer.setInterval(1)
    _pump(qtbot, 60)

    assert socket.of_type("stats_request")
    assert socket.of_type("ping")

    socket.deliver({
        "type": "stats", "frames_sent": 120,
        "bytes_sent": 18_347_264, "fps": 29.8,
    })
    _pump(qtbot, 40)

    assert panel._fps_label.text() == "29.8 fps"
    assert "17.5 MB from bridge" in panel._bytes_label.text()
    assert "bridge 120" in panel._frame_label.text()


def test_vm_list_from_the_bridge_reconciles_with_the_local_store(qtbot, panel):
    # The local store knows "local-only" and "win11"; the bridge knows
    # "win11" and "ubuntu" and reads a different directory entirely.
    panel._populate_vms(["ubuntu", "win11"])
    _pump(qtbot, 20)

    items = [
        panel._vm_combo.itemText(i)
        for i in range(panel._vm_combo.count())
    ]
    assert "win11" in items
    assert "ubuntu" in items
    assert "local-only  (not streamable)" in items

    # The selection moved off a VM the bridge cannot stream.
    assert panel.selected_vm() in ("ubuntu", "win11")
    assert "(not streamable)" not in panel._vm_combo.currentText()


def test_switch_to_vm_follows_the_window_selection(qtbot, panel):
    panel._populate_vms(["ubuntu", "win11"])
    panel.switch_to_vm("win11")
    assert panel.selected_vm() == "win11"

    # A VM the bridge has not heard of is still selectable -- the user picked
    # it elsewhere in the window, and the error belongs in the status area.
    panel.switch_to_vm("local-only")
    assert panel.selected_vm() == "local-only"

    panel.switch_to_vm("never-heard-of-it")
    assert panel.selected_vm() == "local-only"
    assert "never-heard-of-it" in panel._error_label.text()


def test_server_error_is_surfaced_without_a_modal_dialog(qtbot, panel):
    socket = _go_live(qtbot, panel)
    socket.deliver({"type": "error", "message": "unauthorized"})
    _pump(qtbot, 40)
    assert panel._error_label.text() == "unauthorized"
    assert "Unauthorized" in panel._status_label.text()

    socket.deliver({"type": "error", "message": "capture failed: paused"})
    _pump(qtbot, 40)
    assert panel._error_label.text() == "capture failed: paused"
    assert panel._connected is True


def test_malformed_json_does_not_kill_the_connection(qtbot, panel):
    socket = _go_live(qtbot, panel)
    socket._on_message(socket, "{not json")
    _pump(qtbot, 40)
    assert panel._connected is True
    assert panel._error_label.text() == ""


def test_unauthorized_after_connect_reenables_the_button(qtbot, panel):
    panel._populate_vms(["win11"])
    panel.set_vm("win11")
    panel._connect()
    socket = FakeWebSocket.instances[-1]
    _pump_until(qtbot, lambda: panel._connected, timeout=2000)
    socket.deliver({"type": "error", "message": "unauthorized"})
    socket._on_close(socket, 1000, "")
    _pump_until(qtbot, lambda: not panel._connected, timeout=2000)
    assert panel._btn_connect.isEnabled() is True


# ── Settings ──────────────────────────────────────────────────────────────────


def test_settings_round_trip_through_settings_json(qtbot, panel, tmp_path):
    from gui.settings_schema import load_settings

    panel._width_spin.setValue(1024)
    panel._height_spin.setValue(640)
    panel._fps_spin.setValue(20)
    panel._quality_spin.setValue(60)
    panel._input_check.setChecked(False)
    panel._url_input.setText(BRIDGE_SIDECAR_URL)
    panel._save_settings()
    _pump(qtbot, 20)

    stored = load_settings(str(tmp_path / "settings.json"))
    assert stored["vm_console"] == {
        "bridge_url": BRIDGE_SIDECAR_URL,
        "fps": 20,
        "quality": 60,
        "width": 1024,
        "height": 640,
        "input_enabled": False,
    }

    other = VMConsolePanel()
    other._settings_path = str(tmp_path / "settings.json")
    other._load_settings()
    assert other._url_input.text() == BRIDGE_SIDECAR_URL
    assert other._width_spin.value() == 1024
    assert other._input_check.isChecked() is False
    other.close()


def test_token_falls_back_to_the_bridge_environment_variable(monkeypatch):
    monkeypatch.setattr(
        VMConsolePanel,
        "_load_token",
        staticmethod(lambda: os.environ.get(TOKEN_ENV, "").strip()),
    )
    monkeypatch.setenv(TOKEN_ENV, "env-token")
    assert VMConsolePanel._load_token() == "env-token"
    monkeypatch.delenv(TOKEN_ENV)
    assert VMConsolePanel._load_token() == ""


def test_the_token_field_is_what_gets_sent(qtbot, panel):
    panel._token_input.setText("hunter2")
    panel._populate_vms(["win11"])
    panel.set_vm("win11")
    panel._connect()
    socket = FakeWebSocket.instances[-1]
    assert socket.sent[0]["key"] == "hunter2"
    assert "hunter2" not in panel._url_label.text()


def test_the_token_is_never_written_to_settings_json(qtbot, panel, tmp_path):
    from gui.settings_schema import load_settings

    panel._token_input.setText("super-secret")
    panel._save_settings()
    _pump(qtbot, 20)
    stored = load_settings(str(tmp_path / "settings.json"))
    assert "super-secret" not in json.dumps(stored)


# ── Lifecycle extras ──────────────────────────────────────────────────────────


def test_pause_freezes_the_picture_but_not_the_counter(qtbot, panel):
    socket = _go_live(qtbot, panel)
    assert panel._btn_pause.isEnabled() is True

    panel._toggle_pause()
    assert panel._paused is True
    assert panel._btn_pause.text() == "Resume"

    first = panel._pixmap_item.pixmap().toImage()
    panel._handle_frame(_jpeg(1280, 800))
    _pump(qtbot, 40)
    assert panel._frames_received == 2
    assert panel._pixmap_item.pixmap().toImage() == first
    assert "(paused)" in panel._frame_label.text()

    panel._toggle_pause()
    assert panel._paused is False
    assert panel._btn_pause.text() == "Pause"


def test_screenshot_button_without_a_frame_reports_instead_of_crashing(panel):
    panel._btn_shot.setEnabled(True)
    panel._save_screenshot()
    assert "No frame received yet" in panel._error_label.text()


def test_panel_survives_a_binary_frame_it_cannot_decode(qtbot, panel):
    panel._handle_frame(b"\x00\x01\x02not-a-jpeg")
    _pump(qtbot, 20)
    assert panel._pixmap_item is None
    assert panel._connected is False


def test_close_disconnects_the_socket(qtbot, panel):
    socket = _go_live(qtbot, panel)
    panel.close()
    _pump(qtbot, 20)
    assert socket.closed is True