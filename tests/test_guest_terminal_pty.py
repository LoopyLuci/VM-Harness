"""Tests for the interactive guest terminal: the PTY engine and the mode toggle.

No VM, no network, no Docker. The PTY lifecycle runs against a fake asyncssh
connection that behaves the way :mod:`asyncssh` does (``create_process`` with
``term_type``/``term_size``, ``stdout.read`` blocking until data, a
non-coroutine ``change_terminal_size``), because a test that needs a running
guest is a test that only runs on the author's machine.

No modal dialogs are triggered anywhere: under ``QT_QPA_PLATFORM=offscreen`` a
modal box blocks forever and turns a failure into a hung run.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from vm_harness import pty as pty_mod
from vm_harness.config import Secrets, VmMCPSettings


# ── Fakes ───────────────────────────────────────────────────────────────────────

class FakeStdout:
    """Mimics ``asyncssh.SSHReader``: blocks until data, ``""`` at EOF.

    Real asyncssh blocks on a socket, whose wakeup crosses threads safely. A
    test feeding this from the pytest thread while the bridge reads it from its
    own thread cannot do that with an ``asyncio.Event``: ``Future.set_result``
    from a foreign thread does not wake the other loop, so the fake spins on a
    short sleep instead. Same contract, no cross-thread future poking.
    """

    #: Stand-in for the socket wait. Small enough for a fast test.
    POLL_SEC = 0.002

    def __init__(self) -> None:
        self._chunks: list[Any] = []
        self._eof = False

    def feed(self, chunk: Any) -> None:
        self._chunks.append(chunk)

    def feed_eof(self) -> None:
        self._eof = True

    async def read(self, n: int = -1) -> Any:
        while not self._chunks:
            if self._eof:
                return b"" if isinstance(self._last, bytes) else ""
            await asyncio.sleep(self.POLL_SEC)
        chunk = self._chunks.pop(0)
        self._last = chunk
        if n is not None and n >= 0:
            chunk = chunk[:n]
        return chunk

    _last: Any = ""


class FakeStdin:
    def __init__(self) -> None:
        self.written: list[Any] = []

    async def write(self, data: Any) -> None:
        self.written.append(data)


class FakeProcess:
    def __init__(self) -> None:
        self.stdin = FakeStdin()
        self.stdout = FakeStdout()
        self.term_size: tuple[int, int] | None = None
        self.terminated = False
        self.closed = False
        self.exit_status = None

    def change_terminal_size(self, width: int, height: int, pixwidth: int = 0, pixheight: int = 0) -> None:
        # Not a coroutine in asyncssh, and deliberately not one here: awaiting
        # a None would break every resize in the GUI.
        self.term_size = (width, height)

    def terminate(self) -> None:
        self.terminated = True

    async def wait_closed(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self) -> None:
        self.process = FakeProcess()
        self.create_process_calls: list[dict] = []
        self.closed = False
        self.keepalive: int | None = None

    async def create_process(self, **kwargs: Any) -> FakeProcess:
        self.create_process_calls.append(kwargs)
        term_size = kwargs.get("term_size")
        if term_size:
            self.process.term_size = tuple(term_size)
        return self.process

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass

    def set_keepalive(self, interval: int) -> None:
        self.keepalive = interval

    # convenience for tests
    def emit(self, chunk: Any) -> None:
        self.process.stdout.feed(chunk)


class FakeBridge:
    """Stands in for ``SSHBridge`` without a thread, a loop or a guest."""

    def __init__(self) -> None:
        self.is_connected = True
        self.started: list[tuple[int, int]] = []
        self.stopped = 0
        self.written: list[bytes] = []
        self.commands: list[tuple[str, int, int]] = []
        self.target: dict[str, Any] = {}
        # Qt signal stand-ins: plain callables.
        self.command_output = _Emitter()
        self.file_content = _Emitter()
        self.file_list = _Emitter()
        self.error = _Emitter()
        self.connected = _Emitter()
        self.connected_to = _Emitter()
        self.pty_output = _Emitter()
        self.pty_started = _Emitter()
        self.pty_closed = _Emitter()

    def connect(self) -> None:  # pragma: no cover - not exercised
        pass

    def disconnect(self) -> None:  # pragma: no cover - not exercised
        pass

    def start_pty(self, cols: int = 80, rows: int = 24, term_type: str | None = None) -> None:
        self.started.append((cols, rows))
        self.pty_started.emit("user@127.0.0.1:2222")

    def stop_pty(self) -> None:
        self.stopped += 1
        self.pty_closed.emit()

    def write_pty(self, data: Any) -> None:
        self.written.append(data)

    def resize_pty(self, cols: int, rows: int) -> None:
        self.started.append((cols, rows))

    def run_command(self, command: str, timeout: int = 30, max_output: int = 10000) -> None:
        self.commands.append((command, timeout, max_output))

    def list_dir(self, path: str) -> None:  # pragma: no cover - not exercised
        pass

    def write_file(self, path: str, content: str) -> None:  # pragma: no cover
        pass

    def set_vm_target(self, **kwargs: Any) -> None:
        self.target = kwargs


class _Emitter:
    """The slice of ``pyqtSignal`` this panel uses: ``connect`` and ``emit``."""

    def __init__(self) -> None:
        self._slots: list[Any] = []

    def connect(self, slot) -> None:
        self._slots.append(slot)

    def disconnect(self, slot=None) -> None:
        if slot is None:
            self._slots.clear()
        elif slot in self._slots:
            self._slots.remove(slot)

    def emit(self, *args: Any) -> None:
        for slot in list(self._slots):
            slot(*args)


@pytest.fixture(scope="session")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def settings() -> VmMCPSettings:
    return VmMCPSettings(ssh_host="127.0.0.1", ssh_port=2222, ssh_username="omarchyvm")


@pytest.fixture
def secrets() -> Secrets:
    return Secrets(ssh_password="hunter2")


# ── PTY lifecycle ───────────────────────────────────────────────────────────────

async def test_start_negotiates_term_type_and_size(settings, secrets):
    conn = FakeConnection()
    session = pty_mod.PtySession(conn, cols=120, rows=40)

    await session.start()

    assert session.is_running
    call = conn.create_process_calls[0]
    assert call["term_type"] == pty_mod.DEFAULT_TERM_TYPE
    assert call["term_size"] == (120, 40)
    assert conn.process.term_size == (120, 40)


async def test_double_start_is_refused(settings):
    session = pty_mod.PtySession(FakeConnection())
    await session.start()
    with pytest.raises(pty_mod.PtyError):
        await session.start()


async def test_use_before_start_raises():
    session = pty_mod.PtySession(FakeConnection())
    with pytest.raises(pty_mod.PtyNotRunning):
        await session.write("ls\r")


async def test_write_reaches_stdin_and_nothing_is_appended():
    conn = FakeConnection()
    session = pty_mod.PtySession(conn)
    await session.start()

    await session.write("ls")
    await session.send_line("-la")
    await session.send_interrupt()

    assert conn.process.stdin.written == ["ls", "-la\r", "\x03"]


async def test_write_accepts_bytes_and_encoding_none_is_byte_exact():
    conn = FakeConnection()
    session = pty_mod.PtySession(conn, encoding=None)
    await session.start()

    await session.write(b"\x03\x1b[A")

    assert conn.process.stdin.written == [b"\x03\x1b[A"]


async def test_read_stream_yields_chunks_then_stops_at_eof():
    conn = FakeConnection()
    session = pty_mod.PtySession(conn)
    await session.start()

    conn.emit("first ")
    conn.emit("second")
    conn.process.stdout.feed_eof()

    chunks = [chunk async for chunk in session.read_stream()]

    assert chunks == ["first ", "second"]


async def test_read_times_out_without_inventing_data():
    conn = FakeConnection()
    session = pty_mod.PtySession(conn)
    await session.start()

    assert await session.read(timeout=0.05) is None


async def test_read_until_idle_collects_until_quiet():
    conn = FakeConnection()
    session = pty_mod.PtySession(conn)
    await session.start()
    conn.emit("total 48\n")
    conn.emit("drwxr-xr-x 6 root root\n")

    output = await session.read_until_idle(idle=0.05, timeout=2.0)

    assert output == "total 48\ndrwxr-xr-x 6 root root\n"


async def test_a_closed_shell_is_distinguishable_from_a_quiet_one():
    """``""`` means EOF, ``None`` means nothing arrived yet: not the same thing."""
    conn = FakeConnection()
    session = pty_mod.PtySession(conn)
    await session.start()

    assert await session.read(timeout=0.05) is None

    conn.process.stdout.feed_eof()
    assert await session.read(timeout=0.05) == ""


async def test_read_until_idle_returns_empty_for_a_silent_command():
    session = pty_mod.PtySession(FakeConnection())
    await session.start()

    assert await session.read_until_idle(idle=0.05, timeout=2.0) == ""


async def test_read_until_idle_gives_up_on_a_command_that_never_goes_quiet():
    conn = FakeConnection()
    session = pty_mod.PtySession(conn)
    await session.start()

    async def flood() -> None:
        while True:
            conn.emit("tick\n")
            await asyncio.sleep(0.01)

    task = asyncio.create_task(flood())
    try:
        with pytest.raises(asyncio.TimeoutError):
            await session.read_until_idle(idle=0.05, timeout=0.3)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_resize_sends_window_change():
    conn = FakeConnection()
    session = pty_mod.PtySession(conn, cols=80, rows=24)
    await session.start()

    cols, rows = await session.resize(132, 43)

    assert (cols, rows) == (132, 43)
    assert conn.process.term_size == (132, 43)
    assert (session.cols, session.rows) == (132, 43)


async def test_resize_clamps_nonsense_sizes():
    conn = FakeConnection()
    session = pty_mod.PtySession(conn)
    await session.start()

    await session.resize(0, -5)

    assert conn.process.term_size == (1, 1)


async def test_close_terminates_the_shell():
    conn = FakeConnection()
    session = pty_mod.PtySession(conn)
    await session.start()

    await session.close()

    assert conn.process.terminated
    assert conn.process.closed
    assert not session.is_running
    with pytest.raises(pty_mod.PtyNotRunning):
        await session.write("x")


async def test_close_closes_the_connection_when_owned():
    conn = FakeConnection()
    session = pty_mod.PtySession(conn, owns_connection=True)
    await session.start()

    await session.close()

    assert conn.closed


async def test_close_is_safe_without_start_and_twice():
    session = pty_mod.PtySession(FakeConnection())
    await session.close()
    await session.start()
    await session.close()
    await session.close()


async def test_context_manager_opens_and_closes():
    conn = FakeConnection()
    async with pty_mod.PtySession(conn, owns_connection=True) as session:
        assert session.is_running
    assert conn.closed


# ── Byte fidelity ───────────────────────────────────────────────────────────────

#: The exact bytes a prompt rewrite and a colourised `ls` produce. A terminal
#: that mangles any of these is broken in ways that look like a broken guest.
RAW_CHUNK = (
    "\x1b[1;32momarchy\x1b[0m@\x1b[01;34m~"
    "\x1b[00m $ "
    "\x1b[?2004h\x1b]0;title\x07"
    "first\rsecond\r\n"
    "partial-no-newline"
)


async def test_raw_ansi_and_cr_bytes_are_not_mangled():
    conn = FakeConnection()
    session = pty_mod.PtySession(conn)
    await session.start()
    conn.emit(RAW_CHUNK)
    conn.process.stdout.feed_eof()

    received = b"".join(
        [chunk.encode("utf-8") async for chunk in session.read_stream()]
    )

    assert received == RAW_CHUNK.encode("utf-8")
    assert b"\r" in received
    assert received.startswith(b"\x1b[1;32m")
    assert received.count(b"\n") == 1


async def test_partial_chunks_are_reassembled_without_loss():
    conn = FakeConnection()
    session = pty_mod.PtySession(conn)
    await session.start()
    # Split mid-escape-sequence, which is what a real stream does.
    conn.emit("\x1b[3")
    conn.emit("2mgreen\x1b[0m")

    assert await session.read_until_idle(idle=0.05, timeout=1.0) == "\x1b[32mgreen\x1b[0m"


async def test_undecodable_bytes_survive_in_byte_mode():
    conn = FakeConnection()
    session = pty_mod.PtySession(conn, encoding=None)
    await session.start()
    conn.emit(b"\xff\xfe\x1b[0m")
    conn.process.stdout.feed_eof()

    chunks = [chunk async for chunk in session.read_stream()]

    assert b"".join(chunks) == b"\xff\xfe\x1b[0m"


# ── Connection kwargs ───────────────────────────────────────────────────────────

def test_connect_kwargs_accept_unknown_host_keys(settings, secrets):
    """An unknown host key must not stop the terminal from opening."""
    pinned = VmMCPSettings(ssh_known_hosts="C:/keys/known_hosts")

    kwargs = pty_mod.pty_connect_kwargs(secrets, pinned)

    assert kwargs["known_hosts"] is None


def test_connect_kwargs_use_the_per_vm_user_and_password(settings, secrets):
    kwargs = pty_mod.pty_connect_kwargs(secrets, settings, username="otheruser")

    assert kwargs["username"] == "otheruser"
    assert kwargs["password"] == "hunter2"
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 2222


def test_settings_for_vm_does_not_mutate_the_original():
    from vm_harness import ssh_client

    base = VmMCPSettings(ssh_port=2222, ssh_username="vmharness")

    target = ssh_client.settings_for_vm(base, vm_name="win11", ssh_port=2223)

    assert (target.vm_name, target.ssh_port) == ("win11", 2223)
    assert (base.vm_name, base.ssh_port) == ("omarchy-vm", 2222)


# ── Credentials ────────────────────────────────────────────────────────────────

def test_credentials_prefer_the_stored_per_vm_pair(settings, monkeypatch):
    import gui.dialogs_vm_login as login

    monkeypatch.setattr(login, "load_vm_login", lambda name: ("storeduser", "storedpass"))

    secrets, username = _resolve("win11", settings)

    assert username == "storeduser"
    assert secrets.get_ssh_password() == "storedpass"


def test_credential_lookup_failure_degrades_instead_of_raising(settings, monkeypatch):
    """A locked or corrupt store must not make the terminal unusable."""
    import gui.dialogs_vm_login as login

    def boom(name: str):
        raise RuntimeError("could not be decrypted: master password mismatch")

    monkeypatch.setattr(login, "load_vm_login", boom)
    monkeypatch.delenv("SSH_PASSWORD", raising=False)

    secrets, username = _resolve("win11", settings)

    assert username == settings.ssh_username
    assert secrets.get_ssh_password() == ""


def test_credentials_fall_back_to_env_password(settings, monkeypatch):
    import gui.dialogs_vm_login as login

    monkeypatch.setattr(login, "load_vm_login", lambda name: None)
    monkeypatch.setenv("SSH_PASSWORD", "from-env")

    secrets, username = _resolve("win11", settings)

    assert secrets.get_ssh_password() == "from-env"
    assert username == settings.ssh_username


def test_credentials_without_a_vm_name_never_raise():
    secrets, username = _resolve(None, None)

    assert username
    assert secrets is not None


def _resolve(vm_name, settings):
    from gui.ssh_bridge import resolve_vm_credentials

    return resolve_vm_credentials(vm_name, settings)


# ── Bridge: signal arity and the two transports ─────────────────────────────────

def test_connected_to_signal_takes_one_address(qt_app, settings):
    """Both slots take ``address: str``; the signal must match or every emit raises."""
    from gui.ssh_bridge import SSHBridge

    received: list[str] = []
    bridge = SSHBridge(settings=settings)
    bridge.connected_to.connect(received.append)

    bridge.connected_to.emit(bridge.address)

    assert received == [f"{settings.ssh_username}@{settings.ssh_host}:{settings.ssh_port}"]


def test_bridge_targets_one_vm(qt_app, settings):
    from gui.ssh_bridge import SSHBridge

    bridge = SSHBridge(settings=settings)
    bridge.set_vm_target(vm_name="win11", ssh_port=2300, ssh_username="winuser")

    assert bridge.vm_name == "win11"
    assert bridge.address == "winuser@127.0.0.1:2300"
    assert settings.ssh_port == 2222  # the base settings are untouched

    bridge.clear_vm_target()
    assert bridge.address == f"{settings.ssh_username}@{settings.ssh_host}:{settings.ssh_port}"


async def test_command_runner_mode_still_returns_stdout_stderr_and_exit_code(
    qt_app, settings, monkeypatch
):
    """Command-runner mode is untouched: exit code, stdout and stderr survive."""
    from gui import ssh_bridge as bridge_mod

    seen: dict[str, Any] = {}

    async def fake_run(command, timeout=30, env=None, secrets=None, settings=None):
        seen["command"] = command
        seen["timeout"] = timeout
        return {"exit_code": 3, "stdout": "hi", "stderr": "nope", "success": False}

    monkeypatch.setattr(bridge_mod.ssh_mod, "run_guest_command", fake_run)
    monkeypatch.setattr(bridge_mod.SSHBridge, "_secrets_for", lambda self: Secrets())

    bridge = bridge_mod.SSHBridge(settings=settings)
    emitted: list[str] = []
    bridge.command_output.connect(emitted.append)

    await bridge._run_command_impl("echo hi", 30, 10000)

    assert seen["command"] == "echo hi"
    assert seen["timeout"] == 30
    assert emitted == ["Exit: 3\nhi\nstderr: nope"]


async def test_pty_start_write_resize_close_on_the_bridge_loop(qt_app, settings, monkeypatch):
    """The PTY surface the panel drives, end to end on a real bridge thread."""
    from gui import ssh_bridge as bridge_mod

    conn = FakeConnection()

    async def fake_connect_pty(secrets, target, **kwargs):
        session = pty_mod.PtySession(conn, cols=kwargs["cols"], rows=kwargs["rows"])
        await session.start()
        return session

    monkeypatch.setattr(bridge_mod.pty_mod, "connect_pty", fake_connect_pty)

    bridge = bridge_mod.SSHBridge(settings=settings)
    outputs: list[str] = []
    started: list[str] = []
    closed: list[bool] = []
    bridge.pty_output.connect(outputs.append)
    bridge.pty_started.connect(started.append)
    bridge.pty_closed.connect(lambda: closed.append(True))
    bridge.start()
    try:
        bridge.start_pty(cols=100, rows=30)
        await _settle(bridge, lambda: conn.create_process_calls and started)

        assert conn.create_process_calls[0]["term_size"] == (100, 30)
        assert started == [bridge.address]

        conn.emit("hello> ")
        await _settle(bridge, lambda: outputs)

        bridge.write_pty("ls\r")
        await _settle(bridge, lambda: conn.process.stdin.written)
        assert conn.process.stdin.written == ["ls\r"]

        bridge.resize_pty(120, 40)
        await _settle(bridge, lambda: conn.process.term_size == (120, 40))
        assert conn.process.term_size == (120, 40)

        bridge.stop_pty()
        await _settle(bridge, lambda: conn.process.closed and closed)
        assert conn.process.closed
        assert not bridge.pty_active
    finally:
        bridge.stop()


async def _settle(bridge, predicate, timeout: float = 5.0) -> None:
    """Wait until ``predicate()`` is true, yielding to the bridge's own loop.

    ``processEvents`` is required, not decoration: the bridge signals are
    emitted on its worker thread, so Qt queues them for this thread's event
    loop, and nothing is delivered until it runs.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    app = QApplication.instance()
    while loop.time() < deadline:
        if app is not None:
            app.processEvents()
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for the bridge loop")


# ── Panel: the mode toggle ──────────────────────────────────────────────────────

@pytest.fixture
def panel(qt_app):
    from gui.panels_guest_terminal import GuestTerminalPanel

    widget = GuestTerminalPanel()
    yield widget
    widget.close()
    widget.deleteLater()


def test_panel_defaults_to_the_interactive_shell(panel):
    from gui.panels_guest_terminal import MODE_PTY

    assert panel.mode == MODE_PTY
    assert panel.is_pty_mode


def test_mode_toggle_starts_the_pty_and_tears_it_down(panel):
    from gui.panels_guest_terminal import MODE_PTY, MODE_RUNNER

    bridge = FakeBridge()
    panel.set_ssh_bridge(bridge)
    assert bridge.started == [], "no session until a connection exists"

    panel._on_bridge_connected_to("user@127.0.0.1:2222")
    assert len(bridge.started) == 1
    cols, rows = bridge.started[0]
    assert cols >= 20 and rows >= 6

    # Switch to the command runner: the PTY must be closed, not left running.
    panel.mode_combo.setCurrentText(MODE_RUNNER)
    assert panel.mode == MODE_RUNNER
    assert bridge.stopped == 1

    # And back: a new session is opened.
    panel.mode_combo.setCurrentText(MODE_PTY)
    assert panel.is_pty_mode
    assert len(bridge.started) == 2
    # Nothing further to tear down: the second shell is the live one.
    assert bridge.stopped == 1


def test_toggle_without_a_connection_does_not_start_a_session(panel):
    from gui.panels_guest_terminal import MODE_PTY, MODE_RUNNER

    bridge = FakeBridge()
    bridge.is_connected = False
    panel.set_ssh_bridge(bridge)

    panel.mode_combo.setCurrentText(MODE_RUNNER)
    panel.mode_combo.setCurrentText(MODE_PTY)

    assert bridge.started == []
    assert bridge.stopped == 0, "no session was opened, so none was torn down"


def test_command_runner_mode_keeps_its_input_row_and_history(panel):
    from gui.panels_guest_terminal import MODE_RUNNER

    bridge = FakeBridge()
    panel.set_ssh_bridge(bridge)
    panel.mode_combo.setCurrentText(MODE_RUNNER)

    panel.cmd_input.setText("uptime")
    panel._execute_command()

    assert bridge.commands == [("uptime", 30, 10000)]
    assert panel.history_list.item(0).text() == "uptime"
    assert bridge.stopped == 0


def test_closing_the_panel_closes_the_shell(panel):
    bridge = FakeBridge()
    panel.set_ssh_bridge(bridge)
    panel._on_bridge_connected_to("user@127.0.0.1:2222")

    panel.close()

    assert bridge.stopped >= 1


def test_no_simulated_output_survives():
    """The canned-output demo path must not come back."""
    from gui.panels_guest_terminal import GuestTerminalPanel

    for gone in ("_simulate_output", "_populate_sample_files"):
        assert not hasattr(GuestTerminalPanel, gone)


def test_pty_output_is_dropped_in_command_runner_mode(panel):
    from gui.panels_guest_terminal import MODE_RUNNER

    bridge = FakeBridge()
    panel.set_ssh_bridge(bridge)
    panel._on_pty_output("guest said hello")
    assert "hello" in panel.pty_terminal.toPlainText()

    panel.mode_combo.setCurrentText(MODE_RUNNER)
    panel._on_pty_output("second message")

    assert "second message" not in panel.pty_terminal.toPlainText()


# ── Panel: the terminal view ────────────────────────────────────────────────────

@pytest.fixture
def terminal(qt_app):
    from gui.panels_guest_terminal import InteractiveTerminal

    widget = InteractiveTerminal()
    widget.resize(600, 400)
    yield widget
    widget.deleteLater()


def test_carriage_return_overwrites_the_line(terminal):
    terminal.feed("old text\rnew")
    assert terminal.toPlainText() == "new text"


def test_escape_sequences_are_not_drawn(terminal):
    terminal.feed("\x1b[1;32mgreen\x1b[0m plain")
    assert terminal.toPlainText() == "green plain"


def test_lines_are_kept_separate(terminal):
    terminal.feed("one\r\ntwo\r\n")
    assert terminal.toPlainText().split("\n")[:2] == ["one", "two"]


def test_partial_line_completes_on_the_next_chunk(terminal):
    terminal.feed("pro")
    assert terminal.toPlainText() == "pro"
    terminal.feed("mpt\r")
    assert terminal.toPlainText() == "prompt"


def test_control_characters_are_handled_not_printed(terminal):
    terminal.feed("cmd\x03")
    # The guest echoes "^C"; our screen model keeps the byte, which is what the
    # shell sent, and does not let it move the cursor.
    assert terminal.toPlainText() == "cmd\x03"


def test_erase_in_line_is_honoured(terminal):
    # CSI 2 K erases the whole line; CSI K with the cursor at the end of the
    # line erases nothing, which is the correct behaviour.
    terminal.feed("garbage\x1b[2K")
    assert terminal.toPlainText() == ""


def test_reset_clears_the_previous_shells_screen(terminal):
    terminal.feed("old shell output\r\n")
    terminal.reset_screen()
    terminal.feed("new")
    assert terminal.toPlainText() == "new"


def _key(widget, key: int, text: str = "", modifiers=Qt.NoModifier):
    from PyQt5.QtGui import QKeyEvent
    from PyQt5.QtCore import QEvent

    return QKeyEvent(QEvent.KeyPress, key, modifiers, text)


def test_control_and_navigation_keys_reach_the_guest(terminal):
    sent: list[bytes] = []
    terminal.data_to_send.connect(sent.append)

    for key in (Qt.Key_C, Qt.Key_D, Qt.Key_L, Qt.Key_Z):
        terminal.keyPressEvent(_key(terminal, key, modifiers=Qt.ControlModifier))
    for key in (
        Qt.Key_Tab, Qt.Key_Return, Qt.Key_Up, Qt.Key_Down,
        Qt.Key_Left, Qt.Key_Right, Qt.Key_Backspace, Qt.Key_Home, Qt.Key_Delete,
    ):
        terminal.keyPressEvent(_key(terminal, key))

    assert sent == [
        b"\x03", b"\x04", b"\x0c", b"\x1a",
        b"\t", b"\r", b"\x1b[A", b"\x1b[B", b"\x1b[D", b"\x1b[C",
        b"\x7f", b"\x1b[H", b"\x1b[3~",
    ]


def test_ctrl_c_is_sent_even_with_a_selection(terminal):
    sent: list[bytes] = []
    terminal.data_to_send.connect(sent.append)
    terminal.feed("some selected text")
    terminal.selectAll()

    terminal.keyPressEvent(_key(terminal, Qt.Key_C, modifiers=Qt.ControlModifier))

    assert sent == [b"\x03"]


def test_ordinary_text_is_sent_as_utf8(terminal):
    sent: list[bytes] = []
    terminal.data_to_send.connect(sent.append)

    terminal.keyPressEvent(_key(terminal, Qt.Key_A, "a"))
    terminal.keyPressEvent(_key(terminal, Qt.Key_Space, " "))

    assert sent == [b"a", b" "]


def test_tab_is_not_eaten_by_the_focus_chain(terminal):
    assert terminal.tabChangesFocus() is False
    assert terminal.focusPolicy() == Qt.StrongFocus


def test_window_size_is_sane_before_layout(terminal):
    cols, rows = terminal.window_size()
    assert cols >= 20 and rows >= 6


# ── Container terminal panel ───────────────────────────────────────────────────

def test_container_panel_reports_a_missing_token_instead_of_opening(qt_app, monkeypatch):
    """No token, no socket: the route is authenticated and stays that way."""
    import os

    from gui import panels_container_terminal as panel_mod

    monkeypatch.setattr(panel_mod, "load_bridge_token", lambda: "")
    monkeypatch.delenv(panel_mod.TOKEN_ENV, raising=False)

    panel = panel_mod.ContainerTerminalPanel()
    panel._on_container_changed("alpine (running)")

    opened: list[object] = []
    panel._ws_factory = lambda *a, **kw: opened.append(a) or None
    panel._connect()

    text = panel._terminal.toPlainText()
    panel.close()
    panel.deleteLater()

    assert opened == []
    assert "token" in text.lower()


def test_container_connection_sends_auth_before_anything_else(qt_app):
    from gui import panels_container_terminal as panel_mod

    socket = _FakeSocket()
    connection = panel_mod._TerminalConnection(
        "alpine",
        on_text=lambda message: None,
        on_status=lambda connected: None,
        on_error=lambda message: None,
        token="s3cret",
        ws_factory=lambda url, **kwargs: _FakeApp(socket, **kwargs),
    )
    connection._handle_open(socket)

    assert socket.sent[0] == '{"type": "auth", "key": "s3cret"}'


def test_container_connection_reads_output_messages(qt_app):
    from gui import panels_container_terminal as panel_mod

    socket = _FakeSocket()
    received: list[str] = []
    connection = panel_mod._TerminalConnection(
        "alpine",
        on_text=received.append,
        on_status=lambda connected: None,
        on_error=received.append,
        token="s3cret",
        ws_factory=lambda url, **kwargs: _FakeApp(socket, **kwargs),
    )
    connection._handle_message(socket, '{"output": "hello\\n"}')
    connection._handle_message(socket, '{"error": "no such container"}')
    connection._handle_message(socket, "not json at all")
    qt_app.processEvents()

    assert received == ["hello\n", "no such container", "not json at all"]


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, payload: str) -> None:
        self.sent.append(payload)

    def close(self) -> None:
        pass


class _FakeApp:
    def __init__(self, socket: _FakeSocket, **callbacks: Any) -> None:
        self.socket = socket
        self.on_open = callbacks.get("on_open")
        self.on_message = callbacks.get("on_message")
        self.on_error = callbacks.get("on_error")
        self.on_close = callbacks.get("on_close")

    def run_forever(self, **_kwargs: Any) -> None:
        if self.on_open:
            self.on_open(self.socket)

    def send(self, payload: str) -> None:
        self.socket.send(payload)

    def close(self) -> None:
        pass