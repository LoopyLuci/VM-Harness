"""Guest Terminal panel — a real SSH terminal, plus the old command runner.

Two modes, one toggle
---------------------
**Interactive shell (PTY)** opens a genuine pseudo-terminal on the guest through
:meth:`gui.ssh_bridge.SSHBridge.start_pty` (see :mod:`vm_harness.pty`).
Keystrokes go to the guest as bytes and whatever the guest sends back is drawn
here: prompts, tab completion, Ctrl-C, colours, ``top``, ``vim``.

**Command runner** is what this panel always used to be: type a command, get
stdout, stderr and an exit code, with the history list. That path uses
``conn.execute`` with ``term_type="dumb"`` and is genuinely better for scripting
one-off commands, which is why it was kept rather than replaced.

Switching modes tears the PTY session down first — the shell, its SSH connection
and the reader task — because a half-closed PTY keeps a shell running on the
guest for as long as the panel is open.

What is not here
----------------
There is no simulated output. This panel used to carry a hard-coded table of
canned ``ls``/``df``/``uptime`` responses behind ``_simulate_output``, and the
file browser used to seed itself with a fake ``/home/omarchyvm`` listing when no
bridge was attached. Both made a broken terminal look like a working one, which
is worse than an empty panel: you cannot tell which one you are looking at.
"""

from __future__ import annotations

from typing import Any

from gui.theme import T
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QTextCursor
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QPlainTextEdit,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QComboBox,
    QCheckBox,
    QSpinBox,
    QGroupBox,
    QMessageBox,
    QFileDialog,
    QSizePolicy,
    QTreeWidgetItem,
)

from gui.widgets import Card, TerminalOutput, FileTree, TextInput

#: Mode labels. The combo box is the only place these strings are chosen, and
#: :meth:`GuestTerminalPanel._on_mode_changed` compares against them, so they
#: are constants rather than inline literals.
MODE_PTY = "Interactive shell (PTY)"
MODE_RUNNER = "Command runner"

#: Fallback geometry when the widget has not been laid out yet, and the floor
#: for a resize negotiation. A guest asked for a 0x0 window wraps every line.
MIN_PTY_COLS = 20
MIN_PTY_ROWS = 6

#: Bounded scrollback. A ``top`` left running repaints faster than anyone can
#: read; unbounded growth is a memory leak with a blinking cursor.
MAX_TERMINAL_LINES = 5000

#: Cap on a single line's length. Guest output can contain a very long line
#: (a minified file, a base64 blob) and the widget has to lay it out.
MAX_PENDING_CHARS = 8192


# ── Interactive terminal view ───────────────────────────────────────────────────

class InteractiveTerminal(TerminalOutput):
    """Writable terminal view driven by a live guest PTY.

    A sibling of :class:`gui.widgets.TerminalOutput`, not a subclass of it in
    spirit: that one is a read-only log with timestamped, coloured lines and it
    escapes every character. A terminal has to display the guest's bytes as they
    are, so this class keeps its own small screen model instead of using
    ``append_line``.

    What the screen model does, and why it has to:

    ``\\r``
        A carriage return on its own. Prompts and every full-screen program
        redraw with it. Treating ``\\r\\n`` as one line break leaves the old
        line on screen, which is why the raw text is not simply split on
        newlines here.
    ANSI escape sequences
        Colour, cursor moves, ``ESC[K``. Dropped, not printed: an unhandled CSI
        sequence is literal ``^[`` garbage in the widget. This is a *renderer*
        interpreting control characters, which is a different thing from the
        transport mangling them — :mod:`vm_harness.pty` hands the bytes over
        untouched and this class decides what to draw with them.

    Known limitation: this is not a full terminal emulator. Screen addressing
    (cursor-up, absolute positioning) is approximated by treating incoming text
    as a stream, so a program like ``vim`` is usable but not correct. A real
    emulator is a dependency this panel does not get to add.
    """

    #: Bytes for the control chords a terminal owns. Ctrl-C must reach the
    #: guest even when text is selected: that is the whole point of it.
    _CTRL_BYTES = {
        Qt.Key_A: b"\x01",
        Qt.Key_C: b"\x03",
        Qt.Key_D: b"\x04",
        Qt.Key_E: b"\x05",
        Qt.Key_K: b"\x0b",
        Qt.Key_L: b"\x0c",
        Qt.Key_P: b"\x10",
        Qt.Key_R: b"\x12",
        Qt.Key_U: b"\x15",
        Qt.Key_W: b"\x17",
        Qt.Key_Z: b"\x1a",
    }

    #: Arrows, function keys and the editing keys, in the encodings a VT100 and
    #: a Linux console agree on.
    _ESCAPE_BYTES = {
        Qt.Key_Up: b"\x1b[A",
        Qt.Key_Down: b"\x1b[B",
        Qt.Key_Right: b"\x1b[C",
        Qt.Key_Left: b"\x1b[D",
        Qt.Key_Home: b"\x1b[H",
        Qt.Key_End: b"\x1b[F",
        Qt.Key_Insert: b"\x1b[2~",
        Qt.Key_Delete: b"\x1b[3~",
        Qt.Key_PageUp: b"\x1b[5~",
        Qt.Key_PageDown: b"\x1b[6~",
        Qt.Key_Return: b"\r",
        Qt.Key_Enter: b"\r",
        Qt.Key_Backspace: b"\x7f",
        Qt.Key_Tab: b"\t",
        Qt.Key_Escape: b"\x1b",
        Qt.Key_F1: b"\x1bOP",
        Qt.Key_F2: b"\x1bOQ",
        Qt.Key_F3: b"\x1bOR",
        Qt.Key_F4: b"\x1bOS",
        Qt.Key_F5: b"\x1b[15~",
        Qt.Key_F6: b"\x1b[17~",
        Qt.Key_F7: b"\x1b[18~",
        Qt.Key_F8: b"\x1b[19~",
        Qt.Key_F9: b"\x1b[20~",
        Qt.Key_F10: b"\x1b[21~",
        Qt.Key_F11: b"\x1b[23~",
        Qt.Key_F12: b"\x1b[24~",
    }

    #: Final bytes of a CSI sequence, per ECMA-48.
    _CSI_FINAL = set("ABCDEFGHJKSTfhilmnprsu`")

    #: What the user pressed, exactly as a local terminal would send it.
    data_to_send = pyqtSignal(bytes)

    def __init__(self, parent=None, max_lines: int = MAX_TERMINAL_LINES):
        super().__init__(parent)
        # Read-only display, writable *input*: letting the user type into the
        # document would edit a screen the guest owns and desynchronise it.
        # keyPressEvent still receives every key, which is where the bytes go.
        self.setReadOnly(True)
        self.setFocusPolicy(Qt.StrongFocus)
        # Qt's default is to hand Tab to the focus chain. In a terminal Tab is
        # completion, and swallowing it into a widget switch is the classic way
        # to build a terminal that is not interactive.
        self.setTabChangesFocus(False)
        self.setMaximumBlockCount(max_lines)
        # No reflow: a terminal line is as wide as the guest made it. Word wrap
        # would split one logical line into several document blocks, and the
        # block model above depends on one block being one line.
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setPlaceholderText("Interactive shell — press Connect, then type.")
        self._reset_screen()

    # -- Screen model ---------------------------------------------------------

    def _reset_screen(self) -> None:
        self.clear()
        self._state = "normal"
        self._pending = ""
        self._col = 0
        self._csi_params = ""

    def reset_screen(self) -> None:
        """Clear the view and forget the half-drawn line.

        Called when a PTY session is (re)opened: the old screen belongs to a
        shell that no longer exists, and a leftover pending line would be
        prepended to the new shell's first prompt.
        """
        self._reset_screen()

    def feed(self, chunk: str | bytes) -> None:
        """Draw one chunk of guest output.

        Takes the string exactly as :mod:`vm_harness.pty` produced it. A chunk
        may end mid-line or mid-escape-sequence; both are held in the screen
        state and completed by the next chunk rather than being flushed early.
        """
        if not chunk:
            return
        if isinstance(chunk, (bytes, bytearray)):
            chunk = bytes(chunk).decode("utf-8", "replace")
        for char in chunk:
            self._consume(char)
        self._render_pending()

    def _consume(self, char: str) -> None:
        state = self._state
        if state == "normal":
            if char == "\x1b":
                self._state = "esc"
                self._csi_params = ""
            elif char == "\r":
                # A carriage return moves the cursor; it does not erase. The
                # line is overwritten by whatever the guest sends next, which is
                # exactly why clamping here would delete the prompt.
                self._col = 0
            elif char == "\n":
                self._flush_line()
            elif char == "\b":
                self._col = max(0, self._col - 1)
            elif char in ("\x07", "\x00"):
                pass  # bell, NUL padding
            else:
                self._insert(char)
        elif state == "esc":
            # "[" introduces CSI, "]" OSC, anything else is a two-byte sequence
            # (charset selection, RIS) that carries nothing to draw.
            self._state = "csi" if char == "[" else ("osc" if char == "]" else "normal")
        elif state == "csi":
            if char in self._CSI_FINAL:
                if char == "K":
                    self._erase_line(self._csi_params)
                self._state = "normal"
            elif char in "0123456789;":
                self._csi_params += char
            elif not ("\x20" <= char <= "\x3f"):
                self._state = "normal"  # not a parameter byte: sequence over
        elif state == "osc":
            if char == "\x07":
                self._state = "normal"
            elif char == "\x1b":
                self._state = "osc_esc"
        elif state == "osc_esc":
            self._state = "osc" if char == "\\" else "normal"

    def _insert(self, char: str) -> None:
        if self._col < len(self._pending):
            # Overwriting in place: a redraw that rewrites a line does not have
            # to wait for the rest of the line to be short enough to append.
            self._pending = (
                self._pending[: self._col] + char + self._pending[self._col + 1:]
            )
        else:
            self._pending += " " * (self._col - len(self._pending)) + char
        self._col += 1
        if len(self._pending) > MAX_PENDING_CHARS:
            self._pending = self._pending[:MAX_PENDING_CHARS]
            self._col = min(self._col, MAX_PENDING_CHARS)

    def _clamp_line(self) -> None:
        if self._col < len(self._pending):
            self._pending = self._pending[: self._col]

    def _erase_line(self, params: str) -> None:
        """CSI K: erase part of the current line.

        This is the one place the line really is erased, unlike CR and BS,
        which only move the cursor.
        """
        mode = params.split(";")[0] if params else "0"
        if mode in ("", "0"):
            self._clamp_line()
        elif mode == "1":
            self._pending = " " * min(self._col, len(self._pending)) + self._pending[self._col:]
        elif mode == "2":
            self._pending = ""
            self._col = 0

    def _flush_line(self) -> None:
        """Complete the current line and start a new one.

        The invariant this class keeps: the document's last block is always the
        line being drawn, and every block before it is finished output. So a
        newline writes the pending text into the last block and then adds
        another block for the next line -- appending instead would leave the
        completed line as the last block and draw the new line over it.
        """
        self._write_pending()
        cursor = QTextCursor(self.document())
        cursor.movePosition(QTextCursor.End)
        cursor.insertBlock()
        self._pending = ""
        self._col = 0

    def _write_pending(self) -> None:
        """Put the pending line into the document's last block."""
        doc = self.document()
        block = doc.lastBlock()
        if block.text() == self._pending:
            return
        # Positioned by offset rather than via ``QTextCursor(block)``: a
        # QTextBlock handle for the last block is not reliably the block that
        # cursor starts in, and the edit lands on the wrong line -- which looks
        # exactly like a prompt being rewritten.
        cursor = QTextCursor(doc)
        cursor.setPosition(block.position())
        cursor.setPosition(
            block.position() + block.length() - 1,
            QTextCursor.KeepAnchor,
        )
        cursor.insertText(self._pending)

    def _render_pending(self) -> None:
        self._write_pending()
        self._keep_cursor_visible()

    def _keep_cursor_visible(self) -> None:
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.setTextCursor(cursor)

    # -- Keyboard -------------------------------------------------------------

    def keyPressEvent(self, event) -> None:
        """Turn a key press into the bytes a local terminal would send."""
        key = event.key()
        mods = event.modifiers()
        payload: bytes | None = None

        if mods & Qt.ControlModifier:
            if key in self._CTRL_BYTES:
                payload = self._CTRL_BYTES[key]
            elif mods & Qt.ShiftModifier and key == Qt.Key_C and self.textCursor().hasSelection():
                # Ctrl+Shift+C with a selection is a local copy, and only then.
                super().keyPressEvent(event)
                return
        elif key in self._ESCAPE_BYTES:
            payload = self._ESCAPE_BYTES[key]
        elif key >= Qt.Key_Space and event.text():
            payload = event.text().encode("utf-8")

        if payload:
            self.data_to_send.emit(payload)
            event.accept()
            return
        super().keyPressEvent(event)

    def cell_size(self) -> tuple[int, int]:
        """``(width, height)`` of one character cell, in pixels."""
        metrics = self.fontMetrics()
        try:
            width = metrics.horizontalAdvance("W")
        except AttributeError:  # Qt < 5.11
            width = metrics.width("W")
        return max(1, width), max(1, metrics.height())

    def window_size(self) -> tuple[int, int]:
        """The guest's window size in character cells, for ``term_size``."""
        width_px, height_px = self.cell_size()
        viewport = self.viewport()
        cols = viewport.width() // width_px if viewport.width() else MIN_PTY_COLS
        rows = viewport.height() // height_px if viewport.height() else MIN_PTY_ROWS
        return max(MIN_PTY_COLS, cols), max(MIN_PTY_ROWS, rows)


# ── Panel ───────────────────────────────────────────────────────────────────────

class GuestTerminalPanel(QWidget):
    """SSH terminal (PTY or command runner) + file browser for one guest VM."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: #0f172a;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # ── Connection Bar ────────────────────────────────────────────────────
        conn_card = Card("SSH Connection")
        layout.addWidget(conn_card)

        conn_row = QWidget()
        conn_row_layout = QHBoxLayout(conn_row)
        conn_row_layout.setContentsMargins(0, 0, 0, 0)
        conn_row_layout.setSpacing(12)

        self.ssh_status = QLabel("Not connected")
        self.ssh_status.setStyleSheet("color: #ef4444; font-size: 12px;")
        conn_row_layout.addWidget(self.ssh_status)

        self.connect_btn = QPushButton("Connect to Guest")
        self.connect_btn.setFixedHeight(28)
        self.connect_btn.setStyleSheet("""
            QPushButton {
                background: #3b82f6;
                color: white;
                border: none;
                border-radius: 4px;
                font-size: 12px;
                padding: 0 12px;
            }
            QPushButton:hover { background: #2563eb; }
        """)
        conn_row_layout.addWidget(self.connect_btn)

        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.setFixedHeight(28)
        self.disconnect_btn.setStyleSheet("""
            QPushButton {
                background: #ef4444;
                color: white;
                border: none;
                border-radius: 4px;
                font-size: 12px;
                padding: 0 12px;
            }
            QPushButton:hover { background: #dc2626; }
        """)
        conn_row_layout.addWidget(self.disconnect_btn)

        # ── Mode toggle ───────────────────────────────────────────────────────
        mode_label = QLabel("Mode:")
        mode_label.setStyleSheet("color: #94a3b8; font-size: 12px;")
        conn_row_layout.addWidget(mode_label)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem(MODE_PTY)
        self.mode_combo.addItem(MODE_RUNNER)
        self.mode_combo.setToolTip(
            "Interactive shell (PTY) gives a real shell in the guest: prompts, "
            "Tab completion, Ctrl-C, colours.\n"
            "Command runner runs one command and reports its exit code."
        )
        self.mode_combo.setStyleSheet("""
            QComboBox {
                background: #0f172a;
                border: 1px solid #334155;
                border-radius: 4px;
                color: #e2e8f0;
                padding: 4px 8px;
                font-size: 12px;
            }
            QComboBox QAbstractItemView {
                background: #0f172a; color: #e2e8f0; selection-background-color: #3b82f6;
            }
        """)
        conn_row_layout.addWidget(self.mode_combo)

        conn_row_layout.addStretch()
        conn_card.content_layout.addWidget(conn_row)
        conn_card.content_layout.addStretch()

        # ── Split View: Terminal + File Browser ───────────────────────────────
        splitter = QSplitter(Qt.Horizontal, self)
        splitter.setStyleSheet("background: #0f172a;")
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)

        # ── Terminal Panel ─────────────────────────────────────────────────────
        self.term_card = Card("Terminal")
        term_inner = QWidget()
        term_inner_layout = QVBoxLayout(term_inner)
        term_inner_layout.setContentsMargins(0, 0, 0, 0)
        term_inner_layout.setSpacing(0)

        # Command-runner view (read-only log, as before).
        self.terminal = TerminalOutput()
        term_inner_layout.addWidget(self.terminal)

        # Interactive PTY view. Both live in the layout; the mode toggle shows
        # exactly one of them.
        self.pty_terminal = InteractiveTerminal()
        term_inner_layout.addWidget(self.pty_terminal)

        # Command input row
        self.cmd_row = QWidget()
        cmd_row_layout = QHBoxLayout(self.cmd_row)
        cmd_row_layout.setContentsMargins(0, 0, 0, 0)
        cmd_row_layout.setSpacing(8)

        self.cmd_input = QLineEdit()
        self.cmd_input.setPlaceholderText("Enter command (e.g., ls -la, uptime, df -h)...")
        self.cmd_input.setStyleSheet("""
            QLineEdit {
                background: #0f172a;
                border: 1px solid #334155;
                border-radius: 4px;
                color: #e2e8f0;
                padding: 6px 10px;
                font-size: 13px;
                font-family: 'Consolas', monospace;
            }
            QLineEdit:focus { border-color: #3b82f6; }
        """)
        self.cmd_input.returnPressed.connect(self._execute_command)
        self.cmd_input.installEventFilter(self)
        cmd_row_layout.addWidget(self.cmd_input, stretch=1)

        self.send_btn = QPushButton("▶ Send")
        self.send_btn.setFixedHeight(28)
        self.send_btn.setStyleSheet("""
            QPushButton {
                background: #22c55e;
                color: white;
                border: none;
                border-radius: 4px;
                font-size: 12px;
                padding: 0 12px;
            }
            QPushButton:hover { background: #16a34a; }
        """)
        self.send_btn.clicked.connect(self._execute_command)
        cmd_row_layout.addWidget(self.send_btn)

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setFixedHeight(28)
        self.clear_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #64748b;
                border: none;
                font-size: 12px;
            }
            QPushButton:hover { color: #94a3b8; }
        """)
        self.clear_btn.clicked.connect(self._clear_terminal)
        cmd_row_layout.addWidget(self.clear_btn)

        term_inner_layout.addWidget(self.cmd_row)
        self.term_card.content_layout.addWidget(term_inner)
        splitter.addWidget(self.term_card)

        # ── File Browser Panel ─────────────────────────────────────────────────
        file_card = Card("Guest Files")
        file_inner = QWidget()
        file_inner_layout = QVBoxLayout(file_inner)
        file_inner_layout.setContentsMargins(0, 0, 0, 0)
        file_inner_layout.setSpacing(8)

        file_toolbar = QWidget()
        file_toolbar_layout = QHBoxLayout(file_toolbar)
        file_toolbar_layout.setContentsMargins(0, 0, 0, 0)
        file_toolbar_layout.setSpacing(8)

        self.nav_path = QLabel("/home/omarchyvm")
        self.nav_path.setStyleSheet("color: #38bdf8; font-size: 12px; font-family: monospace;")
        self.nav_path.setFixedHeight(20)
        file_toolbar_layout.addWidget(self.nav_path)

        file_toolbar_layout.addStretch()

        self.upload_btn = QPushButton("⬆ Upload")
        self.upload_btn.setFixedHeight(24)
        self.upload_btn.setStyleSheet("""
            QPushButton {
                background: #334155;
                color: #e2e8f0;
                border: 1px solid #475569;
                border-radius: 4px;
                font-size: 11px;
                padding: 0 8px;
            }
            QPushButton:hover { background: #475569; }
        """)
        file_toolbar_layout.addWidget(self.upload_btn)

        file_toolbar_layout.addStretch()

        self.refresh_files_btn = QPushButton("↻ Refresh")
        self.refresh_files_btn.setFixedHeight(24)
        self.refresh_files_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #64748b;
                border: none;
                font-size: 11px;
            }
            QPushButton:hover { color: #94a3b8; }
        """)
        file_toolbar_layout.addWidget(self.refresh_files_btn)

        file_inner_layout.addWidget(file_toolbar)

        self.file_tree = FileTree()
        self.file_tree.setMinimumWidth(280)
        self.file_tree.setMaximumWidth(400)
        file_inner_layout.addWidget(self.file_tree)

        file_card.content_layout.addWidget(file_inner)
        splitter.addWidget(file_card)

        layout.addWidget(splitter)

        # ── Command History ────────────────────────────────────────────────────
        history_card = Card("Command History")
        layout.addWidget(history_card)

        self.history_list = QListWidget()
        self.history_list.setStyleSheet("""
            QListWidget {
                background: #0f172a;
                color: #94a3b8;
                border: none;
                font-size: 11px;
                font-family: monospace;
                max-height: 80px;
            }
            QListWidget::item { padding: 2px 4px; }
        """)
        # Intentionally empty. It used to be seeded with canned commands, which
        # made a guest that had never run anything look like it had.
        self.history_list.itemDoubleClicked.connect(
            lambda item: self.cmd_input.setText(item.text())
        )
        history_card.content_layout.addWidget(self.history_list)
        history_card.content_layout.addStretch()

        # ── Status Message ────────────────────────────────────────────────────
        self.status_label = QLabel("Ready — connect to a guest to begin.")
        self.status_label.setStyleSheet("color: #64748b; font-size: 12px;")
        layout.addWidget(self.status_label)

        # Connections
        self.connect_btn.clicked.connect(self._on_connect)
        self.disconnect_btn.clicked.connect(self._on_disconnect)
        self.upload_btn.clicked.connect(self._on_upload)
        self.refresh_files_btn.clicked.connect(self._refresh_files)
        self.file_tree.file_selected.connect(self._on_file_select)
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)
        self.pty_terminal.data_to_send.connect(self._on_pty_input)

        # SSH bridge
        self._ssh_bridge = None
        #: Real commands this panel has run, newest first. Drives the history
        #: list and the Up/Down recall in the command field.
        self._command_history: list[str] = []
        self._history_cursor = -1
        #: True between requesting a PTY session and hearing it close.
        self._pty_active = False

        self._apply_mode(MODE_PTY, start_session=False)

    # ── Mode toggle ───────────────────────────────────────────────────────────

    @property
    def mode(self) -> str:
        return self.mode_combo.currentText()

    @property
    def is_pty_mode(self) -> bool:
        return self.mode == MODE_PTY

    def _on_mode_changed(self, mode: str) -> None:
        """Tear the old session down before the new mode is shown.

        Order matters: the PTY is closed first, so switching away from an
        interactive shell does not leave a live shell and its SSH connection on
        the guest while the panel is showing a command runner.
        """
        self._apply_mode(mode, start_session=True)

    def _apply_mode(self, mode: str, start_session: bool) -> None:
        self._teardown_pty()
        pty = mode == MODE_PTY
        self.pty_terminal.setVisible(pty)
        self.terminal.setVisible(not pty)
        self.cmd_row.setVisible(not pty)
        title = getattr(self.term_card, "title_label", None)
        if title is not None:
            title.setText("Terminal — interactive shell" if pty else "Terminal — command runner")
        if pty:
            self.pty_terminal.reset_screen()
            self.pty_terminal.setFocus()
            # The combo box keeps focus after a click; without this the first
            # keystrokes of a new session would be lost to the widget that was
            # used to pick the mode.
            QTimer.singleShot(0, self.pty_terminal.setFocus)
            if start_session and self._ssh_bridge is not None and self._ssh_bridge.is_connected:
                self._start_pty()
        else:
            self.cmd_input.setFocus()

    def _start_pty(self) -> None:
        """Open the interactive shell at the current widget size."""
        if self._ssh_bridge is None:
            self.status_label.setText("Connect to the guest first.")
            self.status_label.setStyleSheet("color: #f59e0b; font-size: 12px;")
            return
        cols, rows = self.pty_terminal.window_size()
        self.status_label.setText(f"Opening interactive shell ({cols}x{rows})…")
        self.status_label.setStyleSheet("color: #f59e0b; font-size: 12px;")
        self.pty_terminal.setFocus()
        self._pty_active = True
        self._ssh_bridge.start_pty(cols=cols, rows=rows)

    def _teardown_pty(self) -> None:
        """Close the PTY session, its connection and its reader task.

        Guarded by :attr:`_pty_active` so a mode switch that never had a
        session does not announce a disconnect that did not happen.
        """
        if self._pty_active and self._ssh_bridge is not None:
            self._ssh_bridge.stop_pty()
        self._pty_active = False
        self.pty_terminal.reset_screen()

    def _on_pty_started(self, address: str) -> None:
        self._pty_active = True
        self.status_label.setText(f"Interactive shell on {address}")
        self.status_label.setStyleSheet("color: #22c55e; font-size: 12px;")
        self.pty_terminal.setFocus()

    def _on_pty_closed(self) -> None:
        """The shell ended — requested or by the guest. Never an error."""
        self._pty_active = False
        self.pty_terminal.reset_screen()

    def _on_pty_input(self, data: bytes) -> None:
        if self._ssh_bridge is not None:
            self._ssh_bridge.write_pty(data)

    def _on_pty_output(self, chunk: str) -> None:
        if self.is_pty_mode:
            self.pty_terminal.feed(chunk)

    def resizeEvent(self, event) -> None:
        """Re-negotiate the guest window size when the view changes shape."""
        super().resizeEvent(event)
        if not self.is_pty_mode or self._ssh_bridge is None:
            return
        cols, rows = self.pty_terminal.window_size()
        self._ssh_bridge.resize_pty(cols, rows)

    def _clear_terminal(self) -> None:
        if self.is_pty_mode:
            self.pty_terminal.reset_screen()
        else:
            self.terminal.clear_terminal()

    # ── Connection ────────────────────────────────────────────────────────────

    def _on_connect(self):
        """Connect to guest SSH via the SSH bridge."""
        if not self._ssh_bridge:
            self.terminal.append_line("SSH bridge not available", "ERROR")
            return
        self.connect_btn.setEnabled(False)
        self.connect_btn.setText("Connecting...")
        self.status_label.setText("Connecting to guest SSH...")
        self.status_label.setStyleSheet("color: #f59e0b; font-size: 12px;")
        self._ssh_bridge.connect()

    def _on_disconnect(self):
        """Disconnect from guest SSH."""
        self._teardown_pty()
        if self._ssh_bridge:
            self._ssh_bridge.disconnect()
        self.ssh_status.setText("Disconnected")
        self.ssh_status.setStyleSheet("color: #ef4444; font-size: 12px;")
        self.status_label.setText("Disconnected from guest.")
        self.status_label.setStyleSheet("color: #64748b; font-size: 12px;")
        self.terminal.append_line("SSH connection closed.", "INFO")

    # ── Command runner ────────────────────────────────────────────────────────

    def _execute_command(self):
        """Execute a command on the guest via SSH bridge (command-runner mode)."""
        cmd = self.cmd_input.text().strip()
        if not cmd:
            return
        if not self._ssh_bridge:
            self.terminal.append_line("SSH not connected — cannot execute commands", "ERROR")
            return

        self.terminal.append_command(cmd)
        self.cmd_input.clear()
        self._ssh_bridge.run_command(cmd, timeout=30, max_output=10000)

        # Add to history
        self._remember_command(cmd)

    def _remember_command(self, cmd: str) -> None:
        self._command_history.insert(0, cmd)
        del self._command_history[50:]
        self._history_cursor = -1
        self.history_list.insertItem(0, cmd)
        while self.history_list.count() > 50:
            self.history_list.takeItem(self.history_list.count() - 1)

    def eventFilter(self, obj, event):
        """Up/Down walks the command history in the command field."""
        if obj is self.cmd_input and event.type() == event.KeyPress:
            key = event.key()
            if key in (Qt.Key_Up, Qt.Key_Down):
                self._recall_history(-1 if key == Qt.Key_Up else 1)
                return True
        return super().eventFilter(obj, event)

    def _recall_history(self, direction: int) -> None:
        if not self._command_history:
            return
        if self._history_cursor == -1:
            if direction > 0:
                return
            self._history_cursor = 0
        else:
            self._history_cursor += direction
            if self._history_cursor < 0:
                self._history_cursor = 0
            elif self._history_cursor >= len(self._command_history):
                self._history_cursor = len(self._command_history)
        if self._history_cursor >= len(self._command_history):
            self.cmd_input.clear()
            self._history_cursor = len(self._command_history)
        else:
            self.cmd_input.setText(self._command_history[self._history_cursor])

    # ── File browser ──────────────────────────────────────────────────────────

    def _on_file_select(self, path: str):
        """Handle file double-click in tree."""
        if path.endswith("/"):
            self.nav_path.setText(path.rstrip("/"))
            self._ssh_bridge.list_dir(path)
        else:
            self.terminal.append_line(f"Selected: {path}", "OUTPUT")
            self.terminal.append_line("Use Download button to retrieve this file.", "INFO")

    def _refresh_files(self):
        """List guest directory via SSH bridge."""
        if not self._ssh_bridge:
            # No fake listing. A tree full of invented files is indistinguishable
            # from a real one once it is on screen.
            self.status_label.setText("Connect to a guest to browse its files.")
            self.status_label.setStyleSheet("color: #f59e0b; font-size: 12px;")
            return
        self._ssh_bridge.list_dir(self.nav_path.text() or "/home/omarchyvm")

    def _on_upload(self):
        """Upload a file to the guest via SSH bridge."""
        path, _ = QFileDialog.getOpenFileName(self, "Upload File to Guest", "", "All Files (*)")
        if path:
            self.terminal.append_line(f"Uploading: {path}...", "COMMAND")
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    content = fh.read()
                guest_path = "/home/omarchyvm/" + path.split("/")[-1]
                self._ssh_bridge.write_file(guest_path, content)
                self.terminal.append_line(f"Upload complete: {path} → {guest_path}", "OUTPUT")
            except Exception as e:
                self.terminal.append_line(f"Upload failed: {e}", "ERROR")

    def set_ssh_bridge(self, bridge: Any) -> None:
        """Connect to SSH bridge for real commands and file operations."""
        self._ssh_bridge = bridge
        bridge.command_output.connect(self._on_command_output)
        bridge.file_content.connect(self._on_file_content)
        bridge.file_list.connect(self._on_file_list)
        bridge.error.connect(self._on_ssh_error)
        bridge.connected.connect(self._on_bridge_connected)
        bridge.connected_to.connect(self._on_bridge_connected_to)
        bridge.pty_output.connect(self._on_pty_output)
        bridge.pty_started.connect(self._on_pty_started)
        bridge.pty_closed.connect(self._on_pty_closed)
        self._apply_mode(self.mode, start_session=False)

    def set_vm_target(
        self,
        vm_name: str | None = None,
        ssh_host: str | None = None,
        ssh_port: int | None = None,
        ssh_username: str | None = None,
    ) -> None:
        """Aim this panel — and the shared bridge — at one named VM."""
        if self._ssh_bridge is None:
            return
        self._teardown_pty()
        self._ssh_bridge.set_vm_target(
            vm_name=vm_name,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            ssh_username=ssh_username,
        )

    def _on_bridge_connected(self, connected: bool):
        if connected:
            self.ssh_status.setText("Connected")
            self.ssh_status.setStyleSheet("color: #22c55e; font-size: 12px;")
            self.connect_btn.setEnabled(False)
            self.connect_btn.setText("Connected")
        else:
            self.ssh_status.setText("Connection failed")
            self.ssh_status.setStyleSheet("color: #ef4444; font-size: 12px;")
            self.connect_btn.setEnabled(True)
            self.connect_btn.setText("Connect to Guest")

    def _on_bridge_connected_to(self, address: str):
        self.ssh_status.setText(f"Connected to {address}")
        self.ssh_status.setStyleSheet("color: #22c55e; font-size: 12px;")
        self.connect_btn.setEnabled(False)
        self.connect_btn.setText("Connected")
        self.status_label.setText(f"SSH connected to {address}")
        self.status_label.setStyleSheet("color: #22c55e; font-size: 12px;")
        self.terminal.append_line(f"SSH connection established to {address}", "INFO")
        self._refresh_files()
        if self.is_pty_mode:
            self._start_pty()

    def _on_ssh_error(self, message: str):
        self.terminal.append_line(f"ERROR: {message}", "ERROR")
        self.status_label.setText(f"Error: {message}")
        self.status_label.setStyleSheet("color: #ef4444; font-size: 12px;")

    def _on_command_output(self, output: str):
        """Handle command output from SSH bridge."""
        # Output is formatted as "Exit: N\nstdout\nstderr: ..."
        self.terminal.append_output(output)

    # ``MainWindow`` routes the bridge's output signals here by name, with a
    # ``hasattr`` guard. These three entry points are what that guard is looking
    # for; without them the window's routing is a silent no-op.

    def append_command_output(self, output: str) -> None:
        self._on_command_output(output)

    def append_file_content(self, content: str) -> None:
        self._on_file_content(content)

    def populate_files(self, files: list) -> None:
        self._populate_file_tree(files)

    def _on_file_content(self, content: str):
        self.terminal.append_output(content)

    def _on_file_list(self, files: list):
        self._populate_file_tree(files)

    def _populate_file_tree(self, files: list):
        """Populate file tree from SSH directory listing."""
        self.file_tree.clear()
        for f in files:
            path = f.get("path", f.get("name", ""))
            name = f.get("name", path.split("/")[-1])
            size = f.get("size", "")
            mtime = f.get("mtime", "")
            is_dir = f.get("type") == "dir"
            child = QTreeWidgetItem(self.file_tree, [name, str(size), mtime])
            child.setData(0, Qt.UserRole, path)
            if is_dir:
                child.setFlags(child.flags() | Qt.ItemIsAutoTristate)
        self.file_tree.expandAll()

    def closeEvent(self, event) -> None:
        """Never leave a guest shell running because a panel was closed."""
        self._teardown_pty()
        super().closeEvent(event)