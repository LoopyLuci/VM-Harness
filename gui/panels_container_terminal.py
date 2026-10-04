"""Container Terminal Panel — in-browser terminal via WebSocket.

Provides a Portainer-like terminal for Docker containers. Connects to
``ws://127.0.0.1:8445/terminal/{container_name}``, the persistent
``docker exec`` session in :mod:`streaming_bridge`.

Threading
---------
The socket lives on a worker thread (:class:`_TerminalConnection`) and every
callback hops onto the Qt thread with ``QTimer.singleShot(0, ...)``. This panel
used to call ``websocket.create_connection(...)`` with a 10 second timeout
*directly on the Qt thread* — so the window froze for ten seconds whenever
anything was slow — and then polled ``ws.recv()`` from a 100 ms ``QTimer``,
swallowing every exception, so it could neither tell a timeout from a
disconnect nor notice an error at all. ``WebSocketApp`` runs its own receive
loop with proper socket timeouts and heartbeats on the worker thread; the event
loop is never blocked and nothing is swallowed.

Authentication
--------------
``/terminal/{container}`` is behind the *same* token gate as the frame stream
(``streaming_bridge.TokenAuthenticator``). There is deliberately no
unauthenticated path: an unauthenticated ``docker exec`` sitting next to an
authenticated frame stream would make the frame stream's authentication
decorative. So the first message on the wire is always
``{"type":"auth","key":...}`` and the token comes from the same places the VM
console panel reads it: the saved credential, then ``VMHARNESS_BRIDGE_TOKEN``.
A missing token is reported here rather than leaving the user staring at a
socket that closed itself.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QTextBrowser, QLineEdit,
)

from gui.theme import T
from gui.widgets import Card, StatusIndicator

#: CredentialStore entry the VM console panel also uses, so one saved token
#: serves both bridges.
TOKEN_CREDENTIAL_NAME = "Streaming bridge token"

#: Env var ``streaming_bridge`` reads its token from.
TOKEN_ENV = "VMHARNESS_BRIDGE_TOKEN"

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 8445

#: Heartbeat, so a silently dropped bridge is noticed.
PING_INTERVAL_SEC = 30
PING_TIMEOUT_SEC = 10


def load_bridge_token() -> str:
    """The token to present, preferring a saved credential over the env var.

    Never logged, never echoed into the terminal view.
    """
    try:
        from gui.credential_store import CredentialStore

        store = CredentialStore()
        credential = store.get(TOKEN_CREDENTIAL_NAME)
        if credential is not None and credential.value:
            return credential.value.strip()
    except Exception:  # noqa: BLE001 - a locked store must not break the panel
        pass
    import os

    return (os.environ.get(TOKEN_ENV) or "").strip()


class _TerminalConnection:
    """One WebSocket to ``/terminal/<container>``, owned by one thread.

    ``WebSocketApp`` runs the receive loop; the panel never calls ``recv()``.
    Every callback is invoked on the socket thread, and each one hands straight
    over to the Qt thread -- a Qt widget touched from a foreign thread is a
    crash that happens on someone else's machine.
    """

    def __init__(
        self,
        container: str,
        on_text: Callable[[str], None],
        on_status: Callable[[bool], None],
        on_error: Callable[[str], None],
        token: str,
        host: str = BRIDGE_HOST,
        port: int = BRIDGE_PORT,
        ws_factory: Any = None,
    ) -> None:
        self.container = container
        self._on_text = on_text
        self._on_status = on_status
        self._on_error = on_error
        self._token = token
        self.url = f"ws://{host}:{port}/terminal/{container}"
        self._ws_factory = ws_factory
        self._ws: Any = None
        self._thread: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._authenticated = False

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        import websocket  # noqa: F401  (presence checked by the caller)

        factory = self._ws_factory or websocket.WebSocketApp
        self._ws = factory(
            self.url,
            on_open=self._handle_open,
            on_message=self._handle_message,
            on_error=self._handle_error,
            on_close=self._handle_close,
        )
        self._thread = threading.Thread(
            target=self._run_forever, name=f"container-terminal-{self.container}", daemon=True
        )
        self._thread.start()

    def _run_forever(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            ws.run_forever(ping_interval=PING_INTERVAL_SEC, ping_timeout=PING_TIMEOUT_SEC)
        except TypeError:
            # Older websocket-client without ping arguments.
            try:
                ws.run_forever()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 - run_forever reports via callbacks
            pass

    def close(self) -> None:
        ws, self._ws = self._ws, None
        with self._send_lock:
            self._authenticated = False
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    @property
    def is_open(self) -> bool:
        return self._ws is not None

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    # -- sending -----------------------------------------------------------

    def send_command(self, command: str) -> bool:
        """Send one command. Returns False when the socket is not usable."""
        with self._send_lock:
            ws = self._ws
            if ws is None:
                return False
            try:
                ws.send(json.dumps({"command": command}))
            except Exception as exc:  # noqa: BLE001
                self._error(f"Send failed: {exc}")
                return False
        return True

    # -- callbacks (socket thread) ----------------------------------------

    def _handle_open(self, ws) -> None:
        self._ws = ws
        # Auth goes out here, on this thread, before anything else. A main-thread
        # hop would leave a window in which a command could overtake the
        # handshake, and the bridge hangs up on that.
        if not self._token:
            self._error(
                f"No bridge token. Set {TOKEN_ENV}, or save it in the VM console panel."
            )
            self._close_quietly()
            return
        try:
            ws.send(json.dumps({"type": "auth", "key": self._token}))
        except Exception as exc:  # noqa: BLE001
            self._error(f"Authentication could not be sent: {exc}")
            self._close_quietly()
            return
        self._status(True)

    def _handle_message(self, ws, message) -> None:
        if isinstance(message, (bytes, bytearray)):
            self._text(bytes(message).decode("utf-8", "replace"))
            return
        try:
            payload = json.loads(message)
        except (TypeError, json.JSONDecodeError):
            # A malformed frame from the bridge is still output the user asked
            # for; dropping it silently would look like a hung container.
            self._text(str(message))
            return
        if not isinstance(payload, dict):
            self._text(str(payload))
            return
        if payload.get("type") == "auth_ok":
            self._authenticated = True
            self._text(f"Authenticated. Interactive session open in {self.container}.")
            return
        if payload.get("type") == "error" or "error" in payload:
            self._error(str(payload.get("message") or payload.get("error")))
            return
        if "output" in payload:
            self._text(str(payload.get("output", "")))
            return
        self._text(json.dumps(payload))

    def _handle_error(self, ws, error) -> None:
        self._error(str(error))

    def _handle_close(self, ws, close_status_code=None, close_msg=None) -> None:
        self._status(False)
        detail = close_msg or ""
        if detail:
            self._text(f"Connection closed ({close_status_code}): {detail}")
        else:
            self._text(f"Connection closed ({close_status_code})")

    # -- hop to the Qt thread ---------------------------------------------

    def _text(self, message: str) -> None:
        QTimer.singleShot(0, lambda: self._on_text(message))

    def _error(self, message: str) -> None:
        QTimer.singleShot(0, lambda: self._on_error(message))

    def _status(self, connected: bool) -> None:
        QTimer.singleShot(0, lambda: self._on_status(connected))

    def _close_quietly(self) -> None:
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass


class ContainerTerminalPanel(QWidget):
    """In-browser terminal for Docker containers via WebSocket."""

    terminal_event = pyqtSignal(str, str)  # container_name, event_type

    def __init__(self, parent=None):
        super().__init__(parent)
        self._adapter = None
        self._connection: _TerminalConnection | None = None
        self._current_container: str | None = None
        self._ws_factory = None  # injection point for tests
        self.setStyleSheet("background: " + T.BG_PRIMARY + ";")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignTop)

        # ── Connection Status ──────────────────────────────────────────────
        status_card = Card("Terminal Connection")
        status_card.setFixedHeight(50)
        layout.addWidget(status_card)

        status_row = QWidget()
        sr_layout = QHBoxLayout(status_row)
        sr_layout.setContentsMargins(0, 0, 0, 0)
        sr_layout.setSpacing(12)

        self._status_indicator = StatusIndicator(QColor("#ef4444"))
        sr_layout.addWidget(self._status_indicator)
        sr_layout.addWidget(QLabel("WebSocket"))

        self._container_combo = QComboBox()
        self._container_combo.setMinimumWidth(200)
        self._container_combo.currentTextChanged.connect(self._on_container_changed)
        sr_layout.addWidget(self._container_combo)

        sr_layout.addStretch()

        self._btn_connect = QPushButton("Connect")
        self._btn_connect.setStyleSheet(
            f"background: {T.BRAND}; color: {T.TEXT_PRIMARY}; border: none;"
            "border-radius: 6px; padding: 6px 16px; font-weight: bold;"
        )
        self._btn_connect.clicked.connect(self._connect)
        sr_layout.addWidget(self._btn_connect)

        self._btn_disconnect = QPushButton("Disconnect")
        self._btn_disconnect.setStyleSheet(
            f"background: {T.BG_TERTIARY}; color: {T.TEXT_PRIMARY}; border: 1px solid {T.BG_TERTIARY};"
            "border-radius: 6px; padding: 6px 16px;"
        )
        self._btn_disconnect.clicked.connect(self._disconnect)
        sr_layout.addWidget(self._btn_disconnect)

        self._btn_clear = QPushButton("Clear")
        self._btn_clear.setStyleSheet(
            f"background: {T.BG_TERTIARY}; color: {T.TEXT_PRIMARY}; border: 1px solid {T.BG_TERTIARY};"
            "border-radius: 6px; padding: 6px 16px;"
        )
        self._btn_clear.clicked.connect(self._clear_terminal)
        sr_layout.addWidget(self._btn_clear)

        status_card.content_layout.addWidget(status_row)

        # ── Terminal Output ────────────────────────────────────────────────
        self._terminal = QTextBrowser()
        self._terminal.setFont(QFont("Consolas", 10))
        self._terminal.setStyleSheet(
            f"QTextBrowser {{ background: #0d1117; color: #c9d1d9; border: 1px solid {T.BG_TERTIARY}; border-radius: 8px; padding: 8px; }}"
        )
        self._terminal.setMinimumHeight(300)
        layout.addWidget(self._terminal)

        # ── Command Input ──────────────────────────────────────────────────
        input_row = QWidget()
        input_layout = QHBoxLayout(input_row)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(8)

        self._prompt_label = QLabel("$")
        self._prompt_label.setStyleSheet(f"color: {T.BRAND}; font-family: Consolas; font-size: 12px;")
        input_layout.addWidget(self._prompt_label)

        self._cmd_input = QLineEdit()
        self._cmd_input.setStyleSheet(
            f"QLineEdit {{ background: #0d1117; color: #c9d1d9; border: 1px solid {T.BG_TERTIARY}; border-radius: 6px; padding: 8px; font-family: Consolas; font-size: 12px; }}"
        )
        self._cmd_input.setPlaceholderText("Type command and press Enter...")
        self._cmd_input.returnPressed.connect(self._send_command)
        self._cmd_input.setEnabled(False)
        input_layout.addWidget(self._cmd_input)

        self._btn_send = QPushButton("Send")
        self._btn_send.setStyleSheet(
            f"background: {T.BRAND}; color: {T.TEXT_PRIMARY}; border: none;"
            "border-radius: 6px; padding: 6px 16px; font-weight: bold;"
        )
        self._btn_send.clicked.connect(self._send_command)
        self._btn_send.setEnabled(False)
        input_layout.addWidget(self._btn_send)

        layout.addWidget(input_row)

        # ── Quick Commands ─────────────────────────────────────────────────
        quick_card = Card("Quick Commands")
        layout.addWidget(quick_card)

        quick_row = QWidget()
        quick_layout = QHBoxLayout(quick_row)
        quick_layout.setContentsMargins(0, 0, 0, 0)
        quick_layout.setSpacing(8)

        for cmd in ["ls", "pwd", "ps aux", "df -h", "free -m", "uname -a", "cat /etc/os-release"]:
            btn = QPushButton(cmd)
            btn.setStyleSheet(
                f"background: {T.BG_TERTIARY}; color: {T.TEXT_PRIMARY}; border: 1px solid {T.BG_TERTIARY};"
                "border-radius: 6px; padding: 4px 12px;"
            )
            btn.clicked.connect(lambda checked, c=cmd: self._run_quick_command(c))
            quick_layout.addWidget(btn)

        quick_card.content_layout.addWidget(quick_row)

        # ── Container List Refresh ─────────────────────────────────────────
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._load_containers)
        self._refresh_timer.start(10000)

        self._load_containers()

    # ── Container list ────────────────────────────────────────────────────

    def _load_containers(self):
        """Load container list."""
        try:
            from gui.async_adapter import get_adapter
            adapter = get_adapter()
            containers = adapter.docker.list_containers()
            current = self._current_container
            self._container_combo.blockSignals(True)
            self._container_combo.clear()
            for c in containers:
                name = c.get("name", "")
                status = c.get("status", "")
                self._container_combo.addItem(f"{name} ({status})")
            self._container_combo.blockSignals(False)
            if current:
                self._on_container_changed(
                    next(
                        (self._container_combo.itemText(i)
                         for i in range(self._container_combo.count())
                         if self._container_combo.itemText(i).split("(")[0].strip() == current),
                        f"{current} (unknown)",
                    )
                )
        except Exception as exc:  # noqa: BLE001 - the daemon may be down
            # Reported, not hidden: an empty dropdown with no explanation is
            # indistinguishable from "no containers".
            self._append_line(f"Could not list containers: {exc}")

    def _on_container_changed(self, text: str):
        """Handle container selection change."""
        if "(" in text:
            self._current_container = text.split("(")[0].strip()
        else:
            self._current_container = text.strip()

    # ── Connection ─────────────────────────────────────────────────────────

    def _connect(self):
        """Open a WebSocket terminal for the selected container.

        Everything that can block happens on the connection's worker thread.
        A modal error box is not used: under the offscreen platform a modal
        dialog blocks forever, which turns a connection failure into a hung
        test run.
        """
        if not self._current_container:
            self._append_line("Select a container first.")
            return
        if self._connection is not None and self._connection.is_open:
            self._append_line("Already connected.")
            return

        try:
            import websocket  # noqa: F401
        except ImportError:
            self._append_line(
                "websocket-client is not installed; install it to use the "
                "container terminal (pip install websocket-client)."
            )
            return

        token = load_bridge_token()
        if not token:
            self._append_line(
                f"No bridge token found. Set ${TOKEN_ENV} or save one in the "
                f"VM console panel — /terminal requires the same token as the "
                f"frame stream."
            )
            return

        self._disconnect(quiet=True)
        self._connection = _TerminalConnection(
            self._current_container,
            on_text=self._append_line,
            on_status=self._on_socket_status,
            on_error=self._on_socket_error,
            token=token,
            ws_factory=self._ws_factory,
        )
        self._append_line(f"Connecting to {self._current_container}…")
        try:
            self._connection.open()
        except Exception as exc:  # noqa: BLE001
            self._connection = None
            self._on_socket_status(False)
            self._append_line(f"Connection failed: {exc}")
            return
        self.terminal_event.emit(self._current_container, "connecting")

    def _disconnect(self, quiet: bool = False):
        """Close the WebSocket and its worker thread."""
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass
            if not quiet:
                self._terminal.append("Disconnected")
        self._on_socket_status(False)
        if not quiet and self._current_container:
            self.terminal_event.emit(self._current_container, "disconnected")

    def _on_socket_status(self, connected: bool):
        self._status_indicator.set_status(bool(connected))
        self._cmd_input.setEnabled(bool(connected))
        self._btn_send.setEnabled(bool(connected))

    def _on_socket_error(self, message: str):
        self._append_line(f"Error: {message}")

    def _append_line(self, message: str):
        self._terminal.append(message)

    # ── Commands ───────────────────────────────────────────────────────────

    def _send_command(self):
        """Send a command to the container's shell."""
        if not self._current_container:
            return
        cmd = self._cmd_input.text()
        if not cmd:
            return
        connection = self._connection
        if connection is None or not connection.is_open:
            self._append_line("Not connected")
            return

        self._terminal.append(f"$ {cmd}")
        self._cmd_input.clear()
        connection.send_command(cmd)
        self.terminal_event.emit(self._current_container, "command")

    def _run_quick_command(self, cmd: str):
        """Run a quick command."""
        self._cmd_input.setText(cmd)
        self._send_command()

    def _clear_terminal(self):
        """Clear terminal output."""
        self._terminal.clear()

    # ── Teardown ───────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        self._refresh_timer.stop()
        self._disconnect(quiet=True)
        super().closeEvent(event)

    def hideEvent(self, event) -> None:
        # A worker thread reading a socket nobody is watching is a leak with a
        # heartbeat; the panel being hidden is the cue to stop it.
        if not self.isVisible():
            self._disconnect(quiet=True)
        super().hideEvent(event)