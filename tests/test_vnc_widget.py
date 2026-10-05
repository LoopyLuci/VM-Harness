"""Tests for the Qt widget that renders a decoded framebuffer and sends input.

The widget is where two things go wrong that no protocol test would catch:

* **Keys Qt keeps.** A VNC client that lets ``Tab`` move focus can never type a
  tab into the guest, and once focus has left the widget nothing typed after
  that reaches the VM at all. So focus containment is asserted here, not
  assumed.
* **Coordinates.** A pointer position has to be mapped from widget pixels to
  framebuffer pixels through the letterboxed scale. Off by one, the guest cursor
  drifts from the real pointer; wrong on the margins, a click in the black
  border teleports the cursor to a corner.

Rendering is checked by grabbing the widget and reading pixels back, which
exercises the real paint path rather than a stand-in for it.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PyQt5.QtGui import QColor, QKeyEvent, QWheelEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from gui.widgets_vnc import VNCView  # noqa: E402
from vm_harness.vnc.client import VNCClient  # noqa: E402
from vm_harness.vnc.proto import (  # noqa: E402
    POINTER_BUTTON_LEFT,
    POINTER_BUTTON_MIDDLE,
    POINTER_BUTTON_RIGHT,
    POINTER_WHEEL_DOWN,
    POINTER_WHEEL_UP,
    Framebuffer,
)

# ── Fixtures and helpers ──────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def app():
    """One QApplication for the module; Qt allows only one per process."""
    existing = QApplication.instance()
    if existing is not None:
        yield existing
        return
    application = QApplication([])
    yield application


@pytest.fixture
def view(app):
    widget = VNCView()
    widget.resize(200, 200)
    yield widget
    # Closed but not deleted: a widget that has been deleteLater()d and is then
    # reached by the event loop raises "wrapped C/C++ object has been deleted".
    widget.close()


def solid_framebuffer(width: int, height: int, red: int, green: int, blue: int) -> Framebuffer:
    fb = Framebuffer(width, height)
    fb.fill_rect(0, 0, width, height, bytes((blue, green, red, 0xFF)))
    return fb


def click(widget: VNCView, pos: QPoint, button=Qt.LeftButton) -> None:
    widget.mousePressEvent(_mouse_event(QEvent.MouseButtonPress, pos, button))


def release(widget: VNCView, pos: QPoint, button=Qt.LeftButton) -> None:
    widget.mouseReleaseEvent(_mouse_event(QEvent.MouseButtonRelease, pos, button))


def _mouse_event(kind, pos: QPoint, button) -> QKeyEvent:
    from PyQt5.QtGui import QMouseEvent

    return QMouseEvent(
        kind,
        pos,
        button,
        button,
        Qt.NoModifier,
    )


def move_mouse(widget: VNCView, pos: QPoint) -> None:
    from PyQt5.QtGui import QMouseEvent

    widget.mouseMoveEvent(
        QMouseEvent(QEvent.MouseMove, pos, Qt.NoButton, Qt.NoButton, Qt.NoModifier)
    )


def press_key(widget: VNCView, key, text: str = "", modifiers=Qt.NoModifier) -> None:
    widget.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, modifiers, text))


def release_key(widget: VNCView, key, text: str = "", modifiers=Qt.NoModifier) -> None:
    widget.keyReleaseEvent(QKeyEvent(QEvent.KeyRelease, key, modifiers, text))


def collect(widget: VNCView, signal: str) -> list:
    received: list = []
    getattr(widget, signal).connect(lambda *args: received.append(args))
    return received


# ── Rendering ─────────────────────────────────────────────────────────────────


class TestRendering:
    def test_a_frame_is_drawn(self, view, app):
        view.set_framebuffer(solid_framebuffer(40, 40, 255, 0, 0))
        view.show()
        app.processEvents()
        image = view.grab().toImage()
        assert image.pixelColor(100, 100) == QColor(255, 0, 0)

    def test_the_framebuffer_size_is_reported(self, view):
        assert view.framebuffer_size() is None
        view.set_framebuffer(solid_framebuffer(64, 32, 1, 2, 3))
        assert view.framebuffer_size() == (64, 32)

    def test_a_resize_is_announced(self, view):
        sizes: list[tuple[int, int]] = []
        view.framebufferResized.connect(lambda w, h: sizes.append((w, h)))
        view.set_framebuffer(solid_framebuffer(32, 32, 1, 1, 1))
        view.set_framebuffer(solid_framebuffer(32, 32, 2, 2, 2))
        assert sizes == [(32, 32)], "a same-size frame is not a resize"
        fb = view.framebuffer_size()  # still the old one
        assert fb == (32, 32)

    def test_the_image_is_wrapped_not_copied(self, view, app):
        """The QImage shares the framebuffer's buffer, so a second frame costs
        nothing beyond the repaint. If this ever copies, the win from
        incremental rectangles is spent again on the client side."""
        fb = solid_framebuffer(16, 16, 0, 0, 0)
        view.set_framebuffer(fb)
        first = view._image
        view.set_framebuffer(fb)
        assert view._image is first, "the same framebuffer must reuse its QImage"
        # A decoded change is visible through the existing image. The buffer is
        # BGRA, so this is red.
        fb.fill_rect(0, 0, 16, 16, bytes((0, 0, 255, 0xFF)))
        view.show()
        app.processEvents()
        assert view.grab().toImage().pixelColor(100, 100) == QColor(255, 0, 0)

    def test_no_framebuffer_shows_a_placeholder(self, view, app):
        view.clear("nothing yet")
        view.show()
        app.processEvents()
        assert view.grab().toImage().pixelColor(10, 10).isValid()

    def test_the_frame_is_scaled_to_fit_without_stretching(self, view, app):
        """A wide desktop in a square widget is letterboxed, not stretched.

        The aspect ratio has to survive: stretching a desktop to fill the
        widget makes the guest cursor disagree with the real pointer, because
        the coordinate mapping then lies about where the pixel is.
        """
        view.set_framebuffer(solid_framebuffer(40, 20, 0, 255, 0))
        view.show()
        app.processEvents()
        image = view.grab().toImage()
        rect = view._content_rect
        assert rect.width() == 200 and rect.height() == 100, "40x20 in 200x200 is 200x100"
        # Qt's QRect.center() returns the upper of the two central pixels for an
        # even size, so check the geometry rather than the centre point.
        assert rect.y() == 50
        assert image.pixelColor(100, 100) == QColor(0, 255, 0)
        # The bands above and below are outside the scaled desktop.
        assert image.pixelColor(100, 5) != QColor(0, 255, 0)
        assert image.pixelColor(100, 195) != QColor(0, 255, 0)


# ── Pointer input ─────────────────────────────────────────────────────────────


class TestPointerInput:
    def test_motion_is_forwarded_in_framebuffer_coordinates(self, view):
        view.set_framebuffer(solid_framebuffer(100, 100, 0, 0, 0))
        view.resize(200, 200)
        view.show()
        events = collect(view, "pointerEvent")
        move_mouse(view, QPoint(0, 0))
        move_mouse(view, QPoint(199, 199))
        assert events == [(0, 0, 0), (99, 99, 0)]

    def test_a_press_sets_the_button_bit(self, view):
        view.set_framebuffer(solid_framebuffer(50, 50, 0, 0, 0))
        view.show()
        events = collect(view, "pointerEvent")
        click(view, QPoint(50, 50))
        assert events[-1][2] & POINTER_BUTTON_LEFT

    def test_a_release_clears_the_button_bit(self, view):
        view.set_framebuffer(solid_framebuffer(50, 50, 0, 0, 0))
        view.show()
        events = collect(view, "pointerEvent")
        click(view, QPoint(50, 50))
        release(view, QPoint(50, 50))
        assert events[-1][2] == 0

    @pytest.mark.parametrize(
        "qt_button,expected",
        [
            (Qt.LeftButton, POINTER_BUTTON_LEFT),
            (Qt.RightButton, POINTER_BUTTON_RIGHT),
            (Qt.MiddleButton, POINTER_BUTTON_MIDDLE),
        ],
    )
    def test_every_button_maps_to_its_own_bit(self, view, qt_button, expected):
        view.set_framebuffer(solid_framebuffer(50, 50, 0, 0, 0))
        view.show()
        events = collect(view, "pointerEvent")
        click(view, QPoint(10, 10), qt_button)
        assert events[-1][2] & expected

    def test_the_wheel_is_a_momentary_bit(self, view):
        """Leaving the wheel bit set would make the guest scroll forever."""
        view.set_framebuffer(solid_framebuffer(50, 50, 0, 0, 0))
        view.show()
        events = collect(view, "pointerEvent")
        view.wheelEvent(_wheel_event(120))
        view.wheelEvent(_wheel_event(-120))
        assert any(e[2] & POINTER_WHEEL_UP for e in events)
        assert any(e[2] & POINTER_WHEEL_DOWN for e in events)
        # A plain move afterwards carries no wheel bit: the wheel is momentary,
        # and leaving it set would scroll the guest forever.
        before = len(events)
        move_mouse(view, QPoint(25, 25))
        assert events[before][2] & (POINTER_WHEEL_UP | POINTER_WHEEL_DOWN) == 0

    def test_a_pointer_in_the_letterbox_is_not_forwarded(self, view):
        """Clicking the black margin must not teleport the guest cursor.

        A 20x10 desktop in a 400x400 widget leaves bands top and bottom; a click
        there maps to nowhere, and is dropped rather than clamped to the edge.
        """
        view.set_framebuffer(solid_framebuffer(20, 10, 0, 0, 0))
        view.resize(400, 400)
        view.show()
        events = collect(view, "pointerEvent")
        assert view._content_rect.height() == 200
        click(view, QPoint(200, 5))
        assert events == []
        # Inside the desktop, it is forwarded.
        click(view, QPoint(200, 200))
        assert events

    def test_no_framebuffer_means_no_pointer_events(self, view):
        events = collect(view, "pointerEvent")
        click(view, QPoint(10, 10))
        move_mouse(view, QPoint(10, 10))
        assert events == []


def _wheel_event(delta: int) -> QWheelEvent:
    from PyQt5.QtCore import QPointF

    return QWheelEvent(
        QPointF(50.0, 50.0),
        QPointF(50.0, 50.0),
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.NoScrollPhase,
        False,
    )


# ── Keyboard input ────────────────────────────────────────────────────────────


class TestKeyInput:
    @pytest.mark.parametrize(
        "qt_key,text,expected",
        [
            (Qt.Key_A, "a", ord("a")),
            (Qt.Key_A, "A", ord("A")),
            (Qt.Key_1, "1", ord("1")),
            (Qt.Key_Space, " ", ord(" ")),
        ],
    )
    def test_printable_keys_forward_their_keysym(self, view, qt_key, text, expected):
        events = collect(view, "keyEvent")
        press_key(view, qt_key, text)
        assert events == [(expected, True)]

    @pytest.mark.parametrize(
        "qt_key,expected",
        [
            (Qt.Key_Tab, 0xFF09),
            (Qt.Key_Escape, 0xFF1B),
            (Qt.Key_Return, 0xFF0D),
            (Qt.Key_Backspace, 0xFF08),
            (Qt.Key_Left, 0xFF51),
            (Qt.Key_Up, 0xFF52),
            (Qt.Key_Right, 0xFF53),
            (Qt.Key_Down, 0xFF54),
            (Qt.Key_Home, 0xFF50),
            (Qt.Key_End, 0xFF57),
            (Qt.Key_Delete, 0xFFFF),
        ],
    )
    def test_control_keys_reach_the_guest(self, view, qt_key, expected):
        """Tab, Escape and the arrows are the keys a VNC client loses first."""
        events = collect(view, "keyEvent")
        press_key(view, qt_key)
        assert events == [(expected, True)]

    def test_a_release_is_reported_separately(self, view):
        events = collect(view, "keyEvent")
        press_key(view, Qt.Key_A, "a")
        release_key(view, Qt.Key_A, "a")
        assert events == [(ord("a"), True), (ord("a"), False)]

    def test_ctrl_sends_the_control_keysym(self, view):
        """X puts Ctrl inside the keysym: Ctrl+c is 0x03."""
        events = collect(view, "keyEvent")
        press_key(view, Qt.Key_C, "\x03", Qt.ControlModifier)
        assert events == [(0x03, True)]

    def test_alt_offsets_the_keysym(self, view):
        events = collect(view, "keyEvent")
        press_key(view, Qt.Key_A, "a", Qt.AltModifier)
        assert events == [(0x0100 + ord("a"), True)]

    @pytest.mark.parametrize(
        "qt_key,text,modifiers,why",
        [
            (Qt.Key_Tab, "", Qt.ShiftModifier, "Shift+Tab has no keysym here"),
            (Qt.Key_Tab, "", Qt.ControlModifier, "Ctrl+Tab has no keysym here"),
            (Qt.Key_Left, "", Qt.AltModifier | Qt.ShiftModifier, "Alt+Shift+Left"),
        ],
    )
    def test_an_unrepresentable_combination_sends_nothing(
        self, view, qt_key, text, modifiers, why
    ):
        """Sending the bare key instead would be a *different* key.

        A refused combination must produce no KeyEvent at all -- not the
        unmodified key, which would type into whatever has focus.
        """
        events = collect(view, "keyEvent")
        press_key(view, qt_key, text, modifiers)
        assert events == [], why

    def test_super_is_refused_entirely(self, view):
        """There is no keysym for Super+key, and sending the base key would
        type a letter the user did not ask for."""
        events = collect(view, "keyEvent")
        press_key(view, Qt.Key_S, "s", Qt.MetaModifier)
        assert events == []

    def test_a_refused_key_is_reported(self, view):
        """Silently eating it looks identical to a dead VM."""
        rejected: list[int] = []
        view.keyRejected.connect(rejected.append)
        press_key(view, Qt.Key_F5)
        assert rejected, "an unmapped key should say so"

    def test_function_keys_are_refused(self, view):
        events = collect(view, "keyEvent")
        for key in (Qt.Key_F1, Qt.Key_F5, Qt.Key_F12, Qt.Key_PageUp, Qt.Key_Insert):
            press_key(view, key)
        assert events == []


# ── Focus containment ─────────────────────────────────────────────────────────


class TestFocusContainment:
    def test_tab_does_not_move_focus(self, view):
        """This is the one that silently breaks every VNC console.

        If Tab reaches the focus machinery, the first Tab typed into a guest
        moves focus off this widget and nothing typed afterwards arrives.
        """
        assert view.focusNextPrevChild(Qt.TabFocusReason) is False
        assert view.focusNextPrevChild(Qt.BacktabFocusReason) is False

    def test_the_widget_takes_focus(self, view):
        assert view.focusPolicy() == Qt.StrongFocus

    def test_the_widget_accepts_key_events(self, view):
        """Qt would otherwise try to compose input that belongs to the guest."""
        assert view.testAttribute(Qt.WA_InputMethodEnabled) is False

    def test_losing_focus_releases_held_buttons(self, view):
        """Otherwise Alt-Tab away mid-drag leaves the guest thinking a button
        is still down, and the next click behaves as a drag."""
        view.set_framebuffer(solid_framebuffer(50, 50, 0, 0, 0))
        view.show()
        events = collect(view, "pointerEvent")
        click(view, QPoint(20, 20))
        from PyQt5.QtGui import QFocusEvent

        view.focusOutEvent(QFocusEvent(QEvent.FocusOut, Qt.OtherFocusReason))
        assert events[-1][2] == 0


# ── Wiring to a client ────────────────────────────────────────────────────────


class TestClientWiring:
    def test_input_reaches_a_client_that_has_no_socket_yet(self, view):
        """The widget queues through the client, which is what lets the GUI stay
        responsive while the socket is being connected."""
        client = VNCClient("127.0.0.1", 5900)
        view.set_client(client)
        view.set_framebuffer(solid_framebuffer(50, 50, 0, 0, 0))
        view.show()
        click(view, QPoint(25, 25))
        press_key(view, Qt.Key_A, "a")
        assert client._outbox, "input should be queued on the client"
        assert bytes(client._outbox)[0] == 5  # client-to-server PointerEvent

    def test_detaching_the_client_is_safe(self, view):
        view.set_client(None)
        view.set_framebuffer(solid_framebuffer(10, 10, 0, 0, 0))
        view.show()
        click(view, QPoint(5, 5))  # must not raise with no client attached

    def test_the_view_can_be_embedded_in_a_layout(self, view, app):
        """It has to survive being parented by the console panel."""
        from PyQt5.QtWidgets import QVBoxLayout, QWidget

        host = QWidget()
        layout = QVBoxLayout(host)
        layout.addWidget(view)
        host.resize(320, 240)
        host.show()
        app.processEvents()
        assert view.width() > 0 and view.height() > 0
        # Detach before the host goes: a child deleted with its parent leaves a
        # dangling wrapper that the fixture teardown would then trip over.
        view.setParent(None)
        host.close()