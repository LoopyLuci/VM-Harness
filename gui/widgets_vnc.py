"""The Qt widget that renders a decoded RFB framebuffer and sends input.

Kept apart from :mod:`vm_harness.vnc` on purpose. That package is protocol and
must stay importable with no Qt present; this module is the only part that
knows about painting, and the only part that knows about focus.

## Repainting without copying

:class:`vm_harness.vnc.proto.Framebuffer` holds its pixels in a ``bytearray``
that the decoders mutate in place. PyQt5's ``QImage`` wraps a bytearray through
the buffer protocol without copying it, so the image sees new pixels as soon as
the framebuffer changes. That is what makes this cheap: the ``QImage`` is built
once per framebuffer (and rebuilt only when the desktop is resized, because
that allocates a new buffer) and a changed frame is a bare ``update()``.

The cost when that is *not* possible is a full-framebuffer copy per frame -- at
1080p, 8 MiB a frame -- which is the main thing this design avoids.

## Keys Qt would rather keep

The widget overrides ``event()`` rather than only ``keyPressEvent``, and
consumes what it handles, for two reasons that both show up as real bugs if
missed:

* ``Tab`` moves focus. A VNC client that lets it do that can never type a tab
  into the guest, and after the first one the keyboard has left the widget
  entirely, so nothing typed afterwards reaches the VM.
* ``Space`` and some other keys activate the focused button underneath.

So the widget takes strong focus, turns off the input method hint (otherwise Qt
tries to compose input that belongs to the guest), and consumes the keys it
handles. Callers that need an escape hatch can listen for
:attr:`VNCView.keyRejected`.
"""
from __future__ import annotations

from typing import Optional, Tuple

from loguru import logger
from PyQt5.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt5.QtGui import QImage, QKeyEvent, QPainter
from PyQt5.QtWidgets import QSizePolicy, QWidget

from vm_harness.vnc.client import VNCClient
from vm_harness.vnc.keysym import (
    ALT_KEYSYM_OFFSET,
    CONTROL_KEYSYM_FIRST,
    CONTROL_KEYSYM_LAST,
    KEYSYM_LATIN1_FIRST,
    KEYSYM_LATIN1_LAST,
    key_name_for_keysym,
)
from vm_harness.vnc.proto import (
    POINTER_BUTTON_LEFT,
    POINTER_BUTTON_MIDDLE,
    POINTER_BUTTON_RIGHT,
    POINTER_WHEEL_DOWN,
    POINTER_WHEEL_UP,
    Framebuffer,
)

# Qt keys that are not reachable from ``event.text()``, mapped to X keysyms.
_QT_KEY_TO_KEYSYM: dict[int, int] = {
    Qt.Key_Backspace: 0xFF08,
    Qt.Key_Tab: 0xFF09,
    Qt.Key_Return: 0xFF0D,
    Qt.Key_Enter: 0xFF0D,
    Qt.Key_Insert: 0xFF63,
    Qt.Key_Delete: 0xFFFF,
    Qt.Key_Escape: 0xFF1B,
    Qt.Key_Home: 0xFF50,
    Qt.Key_Left: 0xFF51,
    Qt.Key_Up: 0xFF52,
    Qt.Key_Right: 0xFF53,
    Qt.Key_Down: 0xFF54,
    Qt.Key_End: 0xFF57,
}


class VNCView(QWidget):
    """Displays an RFB framebuffer and forwards mouse and keyboard input."""

    #: x, y, button mask -- ready to hand to ``encode_pointer_event``.
    pointerEvent = pyqtSignal(int, int, int)
    #: keysym, is_down. Emitted for keys the guest could be given.
    keyEvent = pyqtSignal(int, bool)
    #: Emitted for a key this client refuses to send, so a caller can show a
    #: hint instead of the user concluding the VM is unresponsive.
    keyRejected = pyqtSignal(int)
    #: Emitted when the framebuffer size changes, carrying (width, height).
    framebufferResized = pyqtSignal(int, int)

    def __init__(self, client: Optional[VNCClient] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._client = client
        self._fb: Optional[Framebuffer] = None
        self._image: Optional[QImage] = None
        self._content_rect = QRect()
        self._button_mask = 0
        self._placeholder = "waiting for the RFB server..."

        self.setMinimumSize(160, 120)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # Strong focus so the widget can actually receive keys, and the input
        # method off so Qt does not try to compose text that is not its.
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_InputMethodEnabled, False)
        self.setMouseTracking(True)
        self.setAutoFillBackground(False)

    # ── Framebuffer ──────────────────────────────────────────────────────────

    def set_client(self, client: Optional[VNCClient]) -> None:
        """Attach a client, or ``None`` to detach."""
        self._client = client

    def clear(self, placeholder: str = "no frame") -> None:
        """Drop the framebuffer and show a message instead."""
        self._fb = None
        self._image = None
        self._placeholder = placeholder
        self._content_rect = QRect()
        self.update()

    def set_framebuffer(self, fb: Framebuffer) -> None:
        """Adopt a decoded framebuffer.

        Called from the client's ``on_frame``. Cheap when the framebuffer is
        the same object as last time, which is the normal case: the pixels are
        already in the wrapped ``QImage``, so all that is left is a repaint.
        """
        if fb is self._fb and fb.width == self._fb.width and fb.height == self._fb.height:
            self.update()
            return
        previous = (self._fb.width, self._fb.height) if self._fb is not None else None
        self._fb = fb
        # A resize replaces the framebuffer's bytearray, so the QImage has to be
        # rebuilt to point at the new buffer.
        self._image = QImage(
            fb.data, fb.width, fb.height, fb.width * 4, QImage.Format_RGB32
        )
        if previous != (fb.width, fb.height):
            self.framebufferResized.emit(fb.width, fb.height)
        self.update()

    def framebuffer_size(self) -> Optional[Tuple[int, int]]:
        if self._fb is None:
            return None
        return (self._fb.width, self._fb.height)

    # ── Painting ─────────────────────────────────────────────────────────────

    def _update_content_rect(self) -> QRect:
        if self._fb is None:
            return QRect()
        scale = min(
            self.width() / self._fb.width,
            self.height() / self._fb.height,
        )
        if scale <= 0:
            return QRect()
        # keepAspectRatio, centred. Integral and at least 1px so a very small
        # widget still shows something rather than nothing.
        width = max(1, int(self._fb.width * scale))
        height = max(1, int(self._fb.height * scale))
        x = (self.width() - width) // 2
        y = (self.height() - height) // 2
        return QRect(x, y, width, height)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().window())
        if self._image is None or self._fb is None:
            painter.setPen(self.palette().windowText().color())
            painter.drawText(self.rect(), Qt.AlignCenter, self._placeholder)
            return
        self._content_rect = self._update_content_rect()
        if self._content_rect.isEmpty():
            return
        # The QImage wraps the framebuffer's live buffer, so it must be painted
        # before any decode mutates it again. drawImage reads it synchronously
        # here, which is all the ordering guarantee needed.
        target = self._content_rect
        if target.size() == self._image.size():
            painter.drawImage(target.topLeft(), self._image)
            return
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawImage(target, self._image)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._content_rect = self._update_content_rect()
        self.update()

    # ── Pointer input ────────────────────────────────────────────────────────

    def _framebuffer_coordinates(self, point: QPoint) -> Optional[Tuple[int, int]]:
        """Map a widget position to a framebuffer pixel.

        Returns ``None`` when the pointer is in the letterbox, so a click on the
        margin does not jump the guest cursor to a corner.
        """
        if self._fb is None or self._content_rect.isEmpty():
            return None
        if not self._content_rect.contains(point):
            return None
        x = int((point.x() - self._content_rect.x()) * self._fb.width / self._content_rect.width())
        y = int((point.y() - self._content_rect.y()) * self._fb.height / self._content_rect.height())
        x = max(0, min(self._fb.width - 1, x))
        y = max(0, min(self._fb.height - 1, y))
        return x, y

    def _send_pointer(self, point: QPoint) -> None:
        position = self._framebuffer_coordinates(point)
        if position is None:
            return
        x, y = position
        self.pointerEvent.emit(x, y, self._button_mask)
        client = self._client
        if client is not None:
            client.queue_pointer(x, y, self._button_mask)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._send_pointer(event.pos())

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        bit = _button_bit(event.button())
        if bit is None:
            return
        # A press outside the desktop still moves the pointer there, matching
        # what a real X server does when you click past the edge of a screen.
        self._button_mask |= bit
        self._send_pointer(event.pos())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        bit = _button_bit(event.button())
        if bit is None:
            return
        self._send_pointer(event.pos())
        self._button_mask &= ~bit
        self._send_pointer(event.pos())
        event.accept()

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Wheel as a momentary PointerEvent bit, per the specification.

        RFB has no wheel message: a scroll is a pointer event with the wheel bit
        set, and the bit must not be left set or the guest scrolls forever. So it
        is set for exactly one event.
        """
        if self._fb is None or self._content_rect.isEmpty():
            return
        steps = event.angleDelta().y() or event.angleDelta().x()
        if steps == 0:
            return
        up = steps > 0
        position = self._framebuffer_coordinates(event.pos())
        if position is None:
            return
        x, y = position
        bits = []
        # Three wheel "clicks" per detent is what X clients conventionally send.
        for _ in range(max(1, abs(steps) // 120)):
            bits.append(POINTER_WHEEL_UP if up else POINTER_WHEEL_DOWN)
        for bit in bits:
            self.pointerEvent.emit(x, y, self._button_mask | bit)
            client = self._client
            if client is not None:
                client.queue_pointer(x, y, self._button_mask | bit)
        event.accept()

    def focusOutEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Release any held buttons when focus is lost.

        Without this, Alt-Tab away while a button is down leaves the guest
        believing the button is still held, and the next click behaves as a
        drag from wherever the pointer landed.
        """
        if self._button_mask:
            self._button_mask = 0
            last = self._framebuffer_coordinates(self.mapFromGlobal(self.cursor().pos()))
            if last is not None:
                self.pointerEvent.emit(last[0], last[1], 0)
        super().focusOutEvent(event)

    # ── Keyboard input ───────────────────────────────────────────────────────

    def focusNextPrevChild(self, reason) -> bool:
        """Refuse to let Tab move focus.

        This is Qt's own hook for it, and using it keeps Tab out of the focus
        system entirely: a VNC client that lets Tab through can never type a tab
        into the guest, and after the first one the keyboard focus has left this
        widget, so nothing typed afterwards reaches the VM.

        Returning ``False`` from ``event()`` instead looks equivalent and is not:
        an unhandled Tab is consumed *after* the focus machinery has already run,
        so focus still moves. (It also crashes Qt outright when the widget has
        mouse tracking enabled, which this one does.)
        """
        return False

    def keyPressEvent(self, ev: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        self._handle_key(ev, True)
        ev.accept()

    def keyReleaseEvent(self, ev: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        self._handle_key(ev, False)
        ev.accept()

    def _handle_key(self, ev: QKeyEvent, down: bool) -> None:
        keysym = self._keysym_for_event(ev)
        if keysym is None:
            if down:
                # Say so rather than silently eating it: a Super+C that does
                # nothing looks identical to a broken VM.
                self.keyRejected.emit(int(ev.key()))
                logger.debug("refusing to send Qt key {} with modifiers {}", ev.key(), int(ev.modifiers()))
            return
        self.keyEvent.emit(keysym, down)
        client = self._client
        if client is not None:
            client.queue_key(down, keysym)

    def _keysym_for_event(self, ev: QKeyEvent) -> Optional[int]:
        """The keysym to send for this key event, or ``None`` to send nothing.

        ``None`` is the important case. When a combination cannot be
        represented, sending the unmodified base key would be worse than sending
        nothing: ``Shift+Tab`` would arrive as a Tab, and ``Super+C`` as a plain
        ``c`` that types into whatever has focus. So an unrepresentable
        combination is refused whole.
        """
        modifiers = ev.modifiers()

        # Super/Windows has no keysym-offset convention a server can act on, so
        # there is nothing correct to send for a combination that includes it.
        if modifiers & Qt.MetaModifier:
            return None

        text = ev.text()
        base: Optional[int] = None
        # "from_text" means the keysym came from event.text(), which is where Qt
        # already encoded Shift (as the shifted character) and Ctrl (as a 0x01-0x1A
        # code point). Tracking that is what distinguishes "the combination is
        # already in these keysym" from "the combination got lost".
        from_text = False
        if len(text) == 1:
            code = ord(text)
            if KEYSYM_LATIN1_FIRST <= code <= KEYSYM_LATIN1_LAST:
                base = code
                from_text = True
            elif CONTROL_KEYSYM_FIRST <= code <= CONTROL_KEYSYM_LAST:
                base = code
                from_text = True

        if base is None:
            base = _QT_KEY_TO_KEYSYM.get(int(ev.key()))

        if base is None:
            return None

        # Ctrl held but not carried by the keysym: Ctrl+Tab, Ctrl+arrows and
        # friends have no representation here, and sending the bare key would be
        # sending a different key.
        if modifiers & Qt.ControlModifier and not (from_text and base <= CONTROL_KEYSYM_LAST):
            return None

        # Shift is only expressible for printable keys, where Qt has handed us
        # the already-shifted character. Shift+Tab does not survive.
        if modifiers & Qt.ShiftModifier and not (from_text and KEYSYM_LATIN1_FIRST <= base <= KEYSYM_LATIN1_LAST):
            return None

        if modifiers & Qt.AltModifier:
            base += ALT_KEYSYM_OFFSET

        # Final gate: only send a keysym this client has a guest key name for.
        if key_name_for_keysym(base) is None:
            return None
        return base


def _button_bit(button) -> Optional[int]:
    """The RFB pointer bit for a Qt mouse button, or ``None``."""
    if button == Qt.LeftButton:
        return POINTER_BUTTON_LEFT
    if button == Qt.RightButton:
        return POINTER_BUTTON_RIGHT
    if button == Qt.MiddleButton:
        return POINTER_BUTTON_MIDDLE
    return None