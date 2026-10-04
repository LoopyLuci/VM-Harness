"""SSH bridge — async SSH client → PyQt5 signals, via threading.Thread.

Two kinds of session live here, and they are genuinely different transports:

``run_command``
    The command runner. One ``conn.execute`` per command, ``term_type="dumb"``,
    stdout and stderr kept apart and an exit code you can trust. This is what
    the panel's history, the guest agent and the chat engine's tool calls use.

PTY sessions (``start_pty`` / ``write_pty`` / ``resize_pty`` / ``stop_pty``)
    A real interactive shell: ``conn.create_process`` with a terminal type and a
    window size, so prompts, tab completion, Ctrl-C, ``top`` and colours work.
    See :mod:`vm_harness.pty`.

The asyncssh connection cache in :mod:`vm_harness.ssh_client` is keyed by
nothing, so a PTY session deliberately does **not** use it: :func:`connect_pty`
opens its own connection and hands ownership to the session, and closing the
panel closes both. A cached shared connection plus a long-lived PTY is how you
end up with a shell whose reader nobody is servicing.

Targeting
---------
One bridge used to hold one ``VmMCPSettings`` for the whole application, so
every command went to whatever single VM the settings file described.
:meth:`SSHBridge.set_vm_target` re-points it at one named VM; the credentials
come from the per-VM store in :mod:`gui.dialogs_vm_login`.

Threading
---------
Uses ``threading.Thread`` (NOT QThread) so the asyncio loop runs synchronously
in the worker thread — no Qt signal race. Every public method here marshals
onto that loop with ``run_coroutine_threadsafe``; nothing touches an asyncio
primitive from the Qt thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from typing import Any

from PyQt5.QtCore import QObject, pyqtSignal

from vm_harness.config import VmMCPSettings, Secrets
from vm_harness import pty as pty_mod
from vm_harness import ssh_client as ssh_mod

logger = logging.getLogger("vmharness.ssh_bridge")


def resolve_vm_credentials(
    vm_name: str | None,
    settings: VmMCPSettings | None = None,
) -> tuple[Secrets, str | None]:
    """Credentials for one VM, preferring the per-VM store.

    Order: the Fernet-encrypted ``vm-login/<vm name>`` entry written by
    :mod:`gui.dialogs_vm_login`, then the environment's ``SSH_PASSWORD``, and
    for the username the stored one, else ``settings.ssh_username``.

    Every failure here degrades to the configured default instead of raising.
    A terminal that refuses to open because the credential store is locked, or
    because that entry is corrupt, is a terminal you cannot fix from the panel
    you are trying to fix it from. The password is never logged, never put in
    the returned username and never written anywhere new.

    Returns ``(secrets, username)``. The username is never None: with nothing
    stored it is ``settings.ssh_username`` (or the default from settings), so
    the caller always knows which account it is about to use.
    """
    username: str | None = None
    password = ""
    private_key: str | None = None

    if vm_name:
        try:
            from gui.dialogs_vm_login import load_vm_login

            stored = load_vm_login(vm_name)
        except Exception as exc:  # noqa: BLE001 - degraded, not fatal
            logger.warning(
                "stored credentials for VM %r are unusable (%s: %s); "
                "falling back to the configured SSH user",
                vm_name, type(exc).__name__, exc,
            )
            stored = None
        if stored:
            username, password = stored

    env_secrets = Secrets.from_env()
    if not password:
        password = env_secrets.get_ssh_password() or ""
    if not password:
        private_key = env_secrets.get_ssh_private_key()

    if not username:
        base = settings if settings is not None else VmMCPSettings()
        username = getattr(base, "ssh_username", "") or None

    logger.info(
        "resolved credentials for VM %r (user %r, %s)",
        vm_name, username, "key" if private_key else ("password" if password else "agent/unknown"),
    )
    return Secrets(ssh_password=password or None, ssh_private_key=private_key), username


def apply_target(
    settings: VmMCPSettings,
    *,
    vm_name: str | None = None,
    ssh_host: str | None = None,
    ssh_port: int | None = None,
    ssh_username: str | None = None,
) -> VmMCPSettings:
    """``settings`` re-aimed at one VM, leaving the original untouched."""
    return ssh_mod.settings_for_vm(
        settings,
        vm_name=vm_name,
        ssh_host=ssh_host,
        ssh_port=ssh_port,
        ssh_username=ssh_username,
    )


# ── SSH bridge exceptions ──────────────────────────────────────────────────────

class SSHBridgeError(Exception):
    """Base exception for SSH bridge failures."""
    pass

class SSHConnectionError(SSHBridgeError):
    """SSH connection failed or was lost."""
    pass

class SSHTimeoutError(SSHBridgeError):
    """SSH operation timed out."""
    pass

class SSHAuthError(SSHBridgeError):
    """SSH authentication failed."""
    pass


class SSHBridge(QObject):
    """Wraps SSH module-level functions for PyQt5 GUI usage.

    Uses ``threading.Thread`` (NOT QThread) so the asyncio loop runs
    synchronously in the worker thread — no Qt signal race.
    """

    # ── Signals ──────────────────────────────────────────────────────────────
    command_output = pyqtSignal(str)
    connected = pyqtSignal(bool)
    #: ``user@host:port`` of the VM that was reached.
    #:
    #: ONE argument, not three. It was declared ``pyqtSignal(str, int, str)``
    #: and emitted with three values while *both* connected slots
    #: (``MainWindow._on_ssh_connected_to`` and
    #: ``GuestTerminalPanel._on_bridge_connected_to``) take a single address
    #: string. PyQt raised inside the emit on every successful connection, so
    #: the one signal that reported success was the one that always blew up.
    #: The arity was fixed here, at the source, rather than by editing the two
    #: slots — one of which is not this file's to change.
    connected_to = pyqtSignal(str)
    error = pyqtSignal(str)
    file_content = pyqtSignal(str)
    file_list = pyqtSignal(list)
    #: Raw chunks from a PTY session, exactly as the guest sent them.
    pty_output = pyqtSignal(str)
    #: The PTY shell is open, carrying the ``user@host:port`` address.
    pty_started = pyqtSignal(str)
    #: The PTY session is gone: closed on request, or the guest hung up.
    pty_closed = pyqtSignal()

    # ── Constructor ──────────────────────────────────────────────────────────

    def __init__(self, settings, parent=None, vm_name: str | None = None):
        super().__init__(parent)
        self._base_settings = settings
        self._settings = settings
        self._env_secrets = Secrets.from_env()
        self._connected = False
        self._connecting = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ever_connected = False
        self._loop_ready = threading.Event()
        self._lock = threading.Lock()  # protects _connected, _connecting
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 30.0
        self._reconnect_attempts = 0
        self._should_reconnect = True
        # ── PTY session state (owned by the bridge loop thread) ──────────────
        self._vm_name = vm_name
        self._vm_host: str | None = None
        self._vm_port: int | None = None
        self._vm_user: str | None = None
        self._pty_session: Any = None
        self._pty_task: asyncio.Task | None = None
        self._pty_cols = pty_mod.DEFAULT_COLS
        self._pty_rows = pty_mod.DEFAULT_ROWS

    # ── Targeting ────────────────────────────────────────────────────────────

    @property
    def vm_name(self) -> str | None:
        """The VM this bridge talks to, or None for the configured default."""
        return self._vm_name

    @property
    def settings(self):
        """The effective settings: the base ones, re-aimed at the target VM."""
        return self._settings

    @property
    def address(self) -> str:
        """``user@host:port`` for the current target."""
        return (
            f"{self._settings.ssh_username}"
            f"@{self._settings.ssh_host}:{self._settings.ssh_port}"
        )

    def set_vm_target(
        self,
        vm_name: str | None = None,
        ssh_host: str | None = None,
        ssh_port: int | None = None,
        ssh_username: str | None = None,
    ) -> None:
        """Point this bridge at one specific VM.

        A bridge used to hold a single global ``VmMCPSettings``; with several
        VMs running that meant every command went to whichever one the settings
        file described. The base settings are kept untouched so switching back
        to the default target is exact.
        """
        self._vm_name = vm_name
        self._vm_host = ssh_host
        self._vm_port = ssh_port
        self._vm_user = ssh_username
        self._settings = apply_target(
            self._base_settings,
            vm_name=vm_name,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            ssh_username=ssh_username,
        )
        logger.info("SSH bridge target is now %s", self.address)

    def clear_vm_target(self) -> None:
        """Drop the per-VM override and go back to the base settings."""
        self.set_vm_target()

    def set_vm_from_config(self, vm_name: str, config: Any) -> None:
        """Point at a VM described by a ``MultiVMManager`` config object."""
        self.set_vm_target(
            vm_name=vm_name,
            ssh_host=getattr(config, "ssh_host", None),
            ssh_port=getattr(config, "ssh_port", None),
            ssh_username=getattr(config, "ssh_username", None),
        )

    def _credentials(self) -> tuple[Secrets, str | None]:
        secrets, username = resolve_vm_credentials(self._vm_name, self._settings)
        if username and self._settings.ssh_username != username:
            self._settings = ssh_mod.settings_for_vm(
                self._settings, vm_name=self._vm_name, ssh_username=username
            )
        return secrets, username

    def _secrets_for(self) -> Secrets:
        """Credentials for the current target.

        Re-read per operation rather than cached in ``__init__`` so that a
        password saved in the login dialog applies to the next command without
        restarting the application.
        """
        if not self._vm_name:
            return self._env_secrets
        secrets, _ = resolve_vm_credentials(self._vm_name, self._settings)
        return secrets

    # ── Thread lifecycle ─────────────────────────────────────────────────────

    def start(self):
        """Start the background thread + asyncio loop."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._loop_ready.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        if not self._loop_ready.wait(timeout=5.0):
            raise RuntimeError("SSH bridge event loop failed to start within 5s")

    def stop(self):
        """Stop the event loop and thread."""
        # Close any interactive shell first: the loop stopping underneath a live
        # PTY leaves the guest's shell running and its socket in a state nobody
        # can close, because the only handle to it lived in the loop.
        if self._pty_session is not None and self._loop is not None and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._stop_pty_impl(), self._loop).result(timeout=3.0)
            except Exception as e:  # noqa: BLE001 - shutdown is best effort
                logger.debug("PTY shutdown during stop() failed: %s", e)
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._pty_session = None
        with self._lock:
            self._connected = False
            self._connecting = False

    def _run_loop(self):
        """Run the asyncio event loop — executes inside the worker thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        self._loop.run_forever()

    # ── Connection state ─────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    # ── Public API (called from GUI thread) ────────────────────────────────

    def connect_ssh(self):
        """Connect to the guest SSH server."""
        if self._loop is None or not self._loop.is_running():
            self.error.emit("SSH bridge not started")
            return
        with self._lock:
            if self._connecting:
                return
            self._connecting = True
        asyncio.run_coroutine_threadsafe(self._connect_impl(), self._loop)

    def connect(self):
        """Alias for connect_ssh() for panel compatibility."""
        self.connect_ssh()

    async def _connect_impl(self):
        max_attempts = 30
        for attempt in range(max_attempts):
            try:
                secrets, username = self._credentials()
                if username:
                    self._settings = ssh_mod.settings_for_vm(
                        self._settings, vm_name=self._vm_name, ssh_username=username
                    )
                await ssh_mod._connect(secrets, self._settings)
                with self._lock:
                    self._connected = True
                    self._ever_connected = True
                    self._connecting = False
                    self._reconnect_delay = 1.0
                    self._reconnect_attempts = 0
                self.connected.emit(True)
                self.connected_to.emit(self.address)
                self._should_reconnect = True
                return
            except SSHAuthError as e:
                logger.error("SSH auth failed: %s", e)
                with self._lock:
                    self._connected = False
                    self._connecting = False
                self.connected.emit(False)
                self.error.emit(f"SSH authentication failed: {e}")
                self._should_reconnect = False
                return
            except SSHTimeoutError as e:
                logger.error("SSH connection timed out: %s", e)
                with self._lock:
                    self._connected = False
                    self._connecting = False
                self.connected.emit(False)
                self.error.emit(f"SSH connection timed out: {e}")
                self._should_reconnect = False
                return
            except SSHConnectionError as e:
                logger.debug("SSH attempt %d/%d failed: %s", attempt + 1, max_attempts, e)
                with self._lock:
                    self._connected = False
                    self._connecting = False
                if not self._should_reconnect:
                    self.connected.emit(False)
                    self.error.emit(f"SSH connection failed: {e}")
                    return
                # Exponential backoff
                wait = min(self._reconnect_delay, self._max_reconnect_delay)
                await asyncio.sleep(wait)
                self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)
            except Exception as e:
                logger.error("SSH connect failed: %s", e)
                with self._lock:
                    self._connected = False
                    self._connecting = False
                self.connected.emit(False)
                self.error.emit(f"SSH connection failed: {e}")
                self._should_reconnect = False
                return

    def disconnect_ssh(self):
        """Disconnect from the guest SSH server."""
        with self._lock:
            self._should_reconnect = False
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._disconnect_impl(), self._loop)

    def disconnect(self):
        """Alias for disconnect_ssh() for panel compatibility."""
        self.disconnect_ssh()

    async def _disconnect_impl(self):
        try:
            await self._stop_pty_impl()
            await ssh_mod.disconnect()
            with self._lock:
                self._connected = False
                self._connecting = False
            self.connected.emit(False)
        except SSHConnectionError as e:
            logger.error("SSH disconnect failed (connection error): %s", e)
            self.error.emit(f"SSH disconnect failed: {e}")
        except Exception as e:
            logger.error("SSH disconnect failed: %s", e)
            self.error.emit(f"SSH disconnect failed: {e}")

    def run_command(self, command: str, timeout: int = 30, max_output: int = 10000):
        """Run a command on the guest via SSH."""
        if self._loop is None:
            self.error.emit("SSH bridge not started")
            return
        asyncio.run_coroutine_threadsafe(self._run_command_impl(command, timeout, max_output), self._loop)

    async def _run_command_impl(self, command: str, timeout: int, max_output: int):
        try:
            result = await asyncio.wait_for(
                ssh_mod.run_guest_command(
                    command,
                    timeout=timeout,
                    secrets=self._secrets_for(),
                    settings=self._settings,
                ),
                timeout=timeout + 5,
            )
            output = f"Exit: {result['exit_code']}\n{result['stdout']}"
            if result['stderr']:
                output += f"\nstderr: {result['stderr']}"
            self.command_output.emit(output)
        except asyncio.TimeoutError:
            logger.error("SSH command timed out after %ds", timeout)
            self.error.emit(f"Command timed out after {timeout}s")
        except SSHAuthError as e:
            logger.error("SSH auth error during command: %s", e)
            self.error.emit(f"SSH authentication failed: {e}")
        except SSHConnectionError as e:
            logger.error("SSH connection lost during command: %s", e)
            self.error.emit(f"SSH connection lost: {e}")
        except Exception as e:
            logger.error("SSH command failed: %s", e)
            self.error.emit(f"Command failed: {e}")

    # ── Interactive PTY session ──────────────────────────────────────────────

    @property
    def pty_active(self) -> bool:
        """True while an interactive shell is open on the guest."""
        return self._pty_session is not None

    def start_pty(
        self,
        cols: int = pty_mod.DEFAULT_COLS,
        rows: int = pty_mod.DEFAULT_ROWS,
        term_type: str = pty_mod.DEFAULT_TERM_TYPE,
    ) -> None:
        """Open an interactive shell and start streaming its output."""
        if self._loop is None or not self._loop.is_running():
            self.error.emit("SSH bridge not started")
            return
        self._pty_cols = max(1, int(cols))
        self._pty_rows = max(1, int(rows))
        asyncio.run_coroutine_threadsafe(
            self._start_pty_impl(term_type), self._loop
        )

    async def _start_pty_impl(self, term_type: str) -> None:
        if self._pty_session is not None:
            return
        try:
            secrets, username = self._credentials()
            session = await pty_mod.connect_pty(
                secrets,
                self._settings,
                username=username,
                cols=self._pty_cols,
                rows=self._pty_rows,
                term_type=term_type,
            )
        except pty_mod.PtyError as e:
            logger.error("PTY session failed: %s", e)
            self.error.emit(f"Interactive shell unavailable: {e}")
            self.pty_closed.emit()
            return
        except Exception as e:  # noqa: BLE001
            logger.error("PTY session failed: %s", e)
            self.error.emit(f"Interactive shell unavailable: {e}")
            self.pty_closed.emit()
            return
        self._pty_session = session
        self.pty_started.emit(self.address)
        self._pty_task = asyncio.get_running_loop().create_task(self._pump_pty(session))

    async def _pump_pty(self, session) -> None:
        """Forward guest output verbatim until the stream ends.

        ``pty_output`` carries the chunk exactly as asyncssh produced it: ANSI
        escapes, lone carriage returns and partial lines are not stripped or
        re-wrapped here. Rendering them is the view's problem, and a layer that
        cannot faithfully carry the bytes has nowhere to put that burden.
        """
        try:
            async for chunk in session.read_stream():
                self.pty_output.emit(chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace"))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("PTY stream failed: %s", e)
            self.error.emit(f"Interactive shell lost: {e}")
        finally:
            self._pty_task = None
            if self._pty_session is session:
                # The guest hung up on its own. Nothing else holds a handle to
                # this session, so close it here or the SSH connection and the
                # guest's shell stay alive with nobody reading them.
                self._pty_session = None
                with contextlib.suppress(Exception):
                    await session.close()
            self.pty_closed.emit()

    def write_pty(self, data: str | bytes) -> None:
        """Send keystrokes to the guest shell. Nothing is appended."""
        if self._loop is None:
            self.error.emit("SSH bridge not started")
            return
        asyncio.run_coroutine_threadsafe(self._write_pty_impl(data), self._loop)

    async def _write_pty_impl(self, data: str | bytes) -> None:
        session = self._pty_session
        if session is None:
            return
        try:
            await session.write(data)
        except pty_mod.PtyError as e:
            logger.error("PTY write failed: %s", e)
            self.error.emit(f"Interactive shell input failed: {e}")

    def resize_pty(self, cols: int, rows: int) -> None:
        """Tell the guest its window changed size."""
        self._pty_cols = max(1, int(cols))
        self._pty_rows = max(1, int(rows))
        if self._loop is None or self._pty_session is None:
            return
        asyncio.run_coroutine_threadsafe(self._resize_pty_impl(cols, rows), self._loop)

    async def _resize_pty_impl(self, cols: int, rows: int) -> None:
        session = self._pty_session
        if session is None:
            return
        try:
            await session.resize(cols, rows)
        except Exception as e:  # noqa: BLE001 - a failed SIGWINCH is not fatal
            logger.debug("PTY resize failed: %s", e)

    def stop_pty(self) -> None:
        """Close the interactive shell. Idempotent; safe from the Qt thread."""
        if self._loop is None or self._pty_session is None:
            self._pty_session = None
            return
        asyncio.run_coroutine_threadsafe(self._stop_pty_impl(), self._loop)

    async def _stop_pty_impl(self) -> None:
        """Close the session and its connection, then stop the pump.

        The session owns the SSH connection, so this closes the socket too: a
        PTY that is left open keeps a shell running on the guest forever.
        """
        session, self._pty_session = self._pty_session, None
        task, self._pty_task = self._pty_task, None
        if session is not None:
            try:
                await session.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("PTY close failed: %s", e)
            self.pty_closed.emit()
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def read_file(self, path: str):
        """Read a file from the guest via SSH."""
        if self._loop is None:
            self.error.emit("SSH bridge not started")
            return
        asyncio.run_coroutine_threadsafe(self._read_file_impl(path), self._loop)

    async def _read_file_impl(self, path: str):
        try:
            content, enc = await ssh_mod.read_guest_file(path, secrets=self._secrets_for(), settings=self._settings)
            self.file_content.emit(content)
        except SSHConnectionError as e:
            logger.error("SSH connection lost during read: %s", e)
            self.error.emit(f"SSH connection lost: {e}")
        except Exception as e:
            logger.error("SSH read_file failed: %s", e)
            self.error.emit(f"Read file failed: {e}")

    def write_file(self, path: str, content: str):
        """Write a file to the guest via SSH."""
        if self._loop is None:
            self.error.emit("SSH bridge not started")
            return
        asyncio.run_coroutine_threadsafe(
            self._write_file_impl(path, content), self._loop
        )

    async def _write_file_impl(self, path: str, content: str):
        try:
            await ssh_mod.write_guest_file(path, content, secrets=self._secrets_for(), settings=self._settings)
            self.command_output.emit(f"Written: {path}")
        except SSHConnectionError as e:
            logger.error("SSH connection lost during write: %s", e)
            self.error.emit(f"SSH connection lost: {e}")
        except Exception as e:
            logger.error("SSH write_file failed: %s", e)
            self.error.emit(f"Write file failed: {e}")

    def list_dir(self, path: str):
        """List a directory on the guest via SSH."""
        if self._loop is None:
            self.error.emit("SSH bridge not started")
            return
        asyncio.run_coroutine_threadsafe(self._list_dir_impl(path), self._loop)

    async def _list_dir_impl(self, path: str):
        try:
            entries = await ssh_mod.list_guest_directory(path, secrets=self._secrets_for(), settings=self._settings)
            self.file_list.emit(entries)
        except SSHConnectionError as e:
            logger.error("SSH connection lost during list: %s", e)
            self.error.emit(f"SSH connection lost: {e}")
        except Exception as e:
            logger.error("SSH list_dir failed: %s", e)
            self.error.emit(f"List dir failed: {e}")

    def remove_file(self, path: str):
        """Remove a file on the guest via SSH."""
        if self._loop is None:
            self.error.emit("SSH bridge not started")
            return
        asyncio.run_coroutine_threadsafe(self._remove_file_impl(path), self._loop)

    async def _remove_file_impl(self, path: str):
        try:
            await ssh_mod.remove_guest_path(path, secrets=self._secrets_for(), settings=self._settings)
            self.command_output.emit(f"Removed: {path}")
        except SSHConnectionError as e:
            logger.error("SSH connection lost during remove: %s", e)
            self.error.emit(f"SSH connection lost: {e}")
        except Exception as e:
            logger.error("SSH remove_file failed: %s", e)
            self.error.emit(f"Remove file failed: {e}")


# ── Factory ───────────────────────────────────────────────────────────────────


def create_ssh_bridge(settings) -> SSHBridge:
    """Create and start an SSH bridge."""
    bridge = SSHBridge(settings=settings)
    bridge.start()
    return bridge
