"""A real interactive terminal session over SSH, built on ``asyncssh``.

Why this module exists
----------------------
``ssh_client.run_guest_command`` runs every command through ``conn.execute``
with ``term_type="dumb"``. That is the right call for a *command runner*: no TTY,
no job control, stdout and stderr separable, an exit code you can trust. It is
the wrong call for a terminal, and it is what this repository had as its only
option -- there was no ``create_process``, no ``term_size`` and no
``resize_terminal`` anywhere, so the "Guest Terminal" panel could show output
but was structurally incapable of giving the user a shell. Prompt redraws, tab
completion, Ctrl-C, ``top`` and ``vim`` all require a real pseudo-terminal on
the far end; a dumb pipe has no line discipline to give them.

So this module opens the other kind of session: ``conn.create_process`` with a
terminal type and a window size, and exposes it as start / write / read-stream /
resize / close.

Byte discipline
---------------
A PTY is a byte pipe that happens to be attached to a terminal, and the bytes
carry structure a line-oriented reader destroys:

* ``\\r`` on its own is a carriage return. ``top`` and any shell prompt redraw a
  line with it. A reader that treats every ``\\r\\n`` as one break leaves the
  previous line on screen.
* A read can return a partial line, half an escape sequence, or several redraws
  at once. Nothing here waits for a newline before handing data on.
* Colour, cursor addressing and the like are ANSI escape sequences and must
  arrive intact for the caller's terminal to interpret them.

So :meth:`PtySession.read` returns exactly what asyncssh returned -- same
character or byte count, no strip, no ``\\r\\n`` translation, no re-wrapping --
and :meth:`PtySession.read_stream` yields those chunks unchanged until EOF.
Interpreting escape sequences is the *display* layer's job, and it lives in the
GUI, not here. A test asserts this: feeding a chunk of CR/ANSI-laden bytes back
out returns the identical string.

Host keys
---------
:func:`pty_connect_kwargs` passes ``known_hosts=None``. That is a deliberate
decision, not an oversight. The guests are throwaway local VMs whose SSH host
key is regenerated on every reinstall, and the harness reaches them through a
QEMU user-mode NAT forward on ``127.0.0.1``. Strict host-key checking would make
the terminal unusable precisely when it is most needed -- right after a rebuild,
which is when someone is trying to run a command to find out why the rebuild
did not work. Nothing here trusts a key it has never seen, so there is nothing
to compare against; the connection is loopback and the credential still has to
be right.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import asyncssh
from loguru import logger

from vm_harness.config import Secrets, VmMCPSettings

__all__ = [
    "PtySession",
    "PtyError",
    "PtyNotRunning",
    "DEFAULT_COLS",
    "DEFAULT_ROWS",
    "DEFAULT_TERM_TYPE",
    "connect_pty",
    "pty_connect_kwargs",
]

#: What a widget asks for when it has not been laid out yet. 80x24 is the
#: classic default and is what a guest shell assumes when nothing is negotiated.
DEFAULT_COLS = 80
DEFAULT_ROWS = 24

#: 256 colours plus UTF-8. "dumb" is the term type that produced the
#: non-interactive behaviour this module exists to replace.
DEFAULT_TERM_TYPE = "xterm-256color"

#: Read granularity. Chunks are returned as they arrive; this only bounds how
#: much a single await can hold.
DEFAULT_CHUNK = 4096

#: How long a command-runner read waits for the stream to go quiet before it
#: calls the output finished. A PTY command is not delimited by anything, so
#: "quiet" is the only available end-of-output signal.
DEFAULT_IDLE_SEC = 0.4


class PtyError(RuntimeError):
    """Base class for PTY session failures."""


class PtyNotRunning(PtyError):
    """The session was used before :meth:`PtySession.start`, or after close."""


class PtySession:
    """One interactive shell on the guest, over one SSH channel.

    The object owns the *process*, and optionally the connection: pass
    ``owns_connection=True`` (as :func:`connect_pty` does) and :meth:`close`
    closes the SSH connection too, so stopping a panel cannot leak a socket.
    """

    def __init__(
        self,
        connection: Any,
        *,
        term_type: str = DEFAULT_TERM_TYPE,
        cols: int = DEFAULT_COLS,
        rows: int = DEFAULT_ROWS,
        encoding: str | None = "utf-8",
        errors: str = "replace",
        owns_connection: bool = False,
    ) -> None:
        self._connection = connection
        self._term_type = term_type
        self._cols = max(1, int(cols))
        self._rows = max(1, int(rows))
        # ``encoding=None`` makes asyncssh hand back raw bytes, which is the
        # honest mode for a caller that wants the guest's bytes unchanged.
        self._encoding = encoding
        self._errors = errors
        self._owns_connection = bool(owns_connection)
        self._process: Any = None
        self._closed = False

    # ── State ───────────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        """True between a successful :meth:`start` and :meth:`close`."""
        return self._process is not None and not self._closed

    @property
    def cols(self) -> int:
        return self._cols

    @property
    def rows(self) -> int:
        return self._rows

    @property
    def encoding(self) -> str | None:
        return self._encoding

    @property
    def term_type(self) -> str:
        return self._term_type

    @property
    def process(self) -> Any:
        """The underlying ``asyncssh`` process, for anything not wrapped here."""
        return self._process

    @property
    def exit_status(self) -> int | None:
        """Exit status once the shell has ended, else None."""
        if self._process is None:
            return None
        return getattr(self._process, "exit_status", None)

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def start(self) -> "PtySession":
        """Ask the server for a PTY-backed shell.

        ``term_size`` is the window size negotiation this repository did not do
        anywhere: without it the guest has no idea how wide the terminal is and
        wraps every line at 80 columns.
        """
        if self._process is not None:
            raise PtyError("PTY session already started")
        try:
            self._process = await self._connection.create_process(
                term_type=self._term_type,
                term_size=(self._cols, self._rows),
                encoding=self._encoding,
                errors=self._errors,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as PtyError below
            raise PtyError(f"could not open a PTY session: {exc}") from exc
        self._closed = False
        logger.info(
            "PTY shell opened ({}x{}, term_type={})",
            self._cols, self._rows, self._term_type,
        )
        return self

    async def close(self) -> None:
        """Close the shell, and the connection when this session owns it.

        Safe to call twice and safe to call when :meth:`start` never ran.
        """
        process, self._process = self._process, None
        self._closed = True
        if process is not None:
            try:
                process.terminate()
            except Exception as exc:  # noqa: BLE001 - teardown is best effort
                logger.debug("PTY terminate failed: {}", exc)
            try:
                await process.wait_closed()
            except Exception as exc:  # noqa: BLE001
                logger.debug("PTY wait_closed failed: {}", exc)
        if self._owns_connection and self._connection is not None:
            connection, self._connection = self._connection, None
            try:
                connection.close()
                await connection.wait_closed()
            except Exception as exc:  # noqa: BLE001
                logger.debug("PTY connection close failed: {}", exc)
        logger.info("PTY shell closed")

    async def __aenter__(self) -> "PtySession":
        if self._process is None:
            await self.start()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    # ── Input ───────────────────────────────────────────────────────────────

    async def write(self, data: str | bytes) -> None:
        """Send raw bytes (or text) to the guest's terminal.

        Nothing is appended and nothing is interpreted. A PTY already has a line
        discipline on the far end, so this is the only place a newline belongs:
        callers send ``"\\r"`` for Enter and ``"\\x03"`` for Ctrl-C exactly as a
        local terminal would.
        """
        process = self._require_process()
        if isinstance(data, str):
            payload: Any = data if self._encoding else data.encode("utf-8", "replace")
        else:
            payload = data
            if self._encoding:
                payload = bytes(data).decode("utf-8", "replace")
        try:
            await process.stdin.write(payload)
        except BrokenPipeError as exc:
            raise PtyNotRunning("the guest closed the shell") from exc

    async def send_line(self, line: str) -> None:
        """Write one line and press Enter. The CR matters: a PTY expects CR."""
        await self.write(line + "\r")

    async def send_interrupt(self) -> None:
        """Ctrl-C as the guest's line discipline expects to receive it."""
        await self.write("\x03")

    # ── Output ──────────────────────────────────────────────────────────────

    async def read(self, max_bytes: int = DEFAULT_CHUNK, timeout: float | None = None) -> Any:
        """Read up to ``max_bytes`` of whatever the guest has sent.

        Returns the chunk exactly as asyncssh produced it -- a ``str`` when the
        session has an encoding, ``bytes`` when it does not, and ``""``/``b""``
        at EOF. Nothing is stripped, translated or buffered across calls, so
        ANSI escapes, lone ``\\r`` and partial lines survive intact.

        With ``timeout``, returns ``None`` if nothing arrived in time. That is
        deliberately different from ``""``: EOF is final and a timeout is not,
        and a caller that has to tell a closed shell from a quiet one cannot do
        it if both look like "no data". The timeout bounds the wait for the
        *first* byte only, matching how a terminal behaves -- once data has
        started arriving it is delivered without an artificial break in the
        middle of a line.
        """
        process = self._require_process()
        empty = b"" if self._encoding is None else ""
        try:
            coroutine = process.stdout.read(max_bytes)
            if timeout is None:
                return await coroutine
            return await asyncio.wait_for(coroutine, timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def read_stream(self, max_bytes: int = DEFAULT_CHUNK) -> AsyncIterator[Any]:
        """Yield raw chunks until the guest closes the stream.

        The normal way to drive an interactive view: no newline is required, so a
        prompt that arrives without one still arrives.
        """
        while self.is_running:
            chunk = await self.read(max_bytes)
            if not chunk:
                return
            yield chunk

    async def read_until_idle(
        self,
        idle: float = DEFAULT_IDLE_SEC,
        timeout: float | None = None,
        max_bytes: int = DEFAULT_CHUNK,
    ) -> Any:
        """Collect output until the guest goes quiet for ``idle`` seconds.

        For the command-runner mode: a PTY stream has no end-of-command marker,
        so "no output for ``idle`` seconds" is the honest definition of finished.
        Silence returns whatever arrived, which may be nothing.

        ``timeout`` is a hard cap on a command that is *still producing output*
        when it expires, and raises :class:`asyncio.TimeoutError` rather than
        waiting for a quiet moment that may never come. It does not turn silence
        into an error: a command that prints nothing has finished.
        """
        empty = b"" if self._encoding is None else ""
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        parts: list[Any] = []
        while self.is_running:
            chunk = await self.read(max_bytes, timeout=idle)
            if not chunk:
                break  # EOF ("" / b"") or nothing arrived within `idle` (None)
            parts.append(chunk)
            if deadline is not None and loop.time() >= deadline:
                raise asyncio.TimeoutError(f"output was still arriving after {timeout}s")
        return empty.join(parts)

    # ── Window size ─────────────────────────────────────────────────────────

    async def resize(self, cols: int, rows: int) -> tuple[int, int]:
        """Tell the guest its window changed size (SIGWINCH).

        asyncssh's ``change_terminal_size`` is a plain method, not a coroutine.
        It is awaited nowhere here on purpose; awaiting a None would raise and
        silently break every resize in the GUI.
        """
        process = self._require_process()
        cols = max(1, int(cols))
        rows = max(1, int(rows))
        process.change_terminal_size(cols, rows)
        self._cols, self._rows = cols, rows
        logger.debug("PTY resized to {}x{}", cols, rows)
        return cols, rows

    # ── Internals ───────────────────────────────────────────────────────────

    def _require_process(self) -> Any:
        if self._process is None:
            raise PtyNotRunning("PTY session is not running")
        return self._process


# ── Connecting ─────────────────────────────────────────────────────────────────

def pty_connect_kwargs(
    secrets: Secrets,
    settings: VmMCPSettings,
    *,
    username: str | None = None,
) -> dict[str, Any]:
    """Build ``asyncssh.connect`` kwargs for an interactive session.

    ``known_hosts=None`` is set explicitly, overriding ``settings.ssh_known_hosts``.
    See the module docstring: the guests are throwaway local VMs reached over a
    loopback NAT forward, and their host keys change on every reinstall, so
    strict checking would make the terminal useless exactly when it is needed.
    """
    kwargs = settings.ssh_connect_kwargs(secrets)
    if username:
        kwargs["username"] = username
    kwargs["known_hosts"] = None
    kwargs.setdefault("client_version", "vm-harness-pty/0.1.0")
    return kwargs


async def connect_pty(
    secrets: Secrets,
    settings: VmMCPSettings,
    *,
    username: str | None = None,
    cols: int = DEFAULT_COLS,
    rows: int = DEFAULT_ROWS,
    term_type: str = DEFAULT_TERM_TYPE,
    encoding: str | None = "utf-8",
    timeout: float | None = 15.0,
) -> PtySession:
    """Open a fresh SSH connection and a PTY shell on it.

    The returned session owns the connection: :meth:`PtySession.close` closes
    both, which is what lets a panel be stopped without leaking a socket or
    leaving a shell running on the guest.
    """
    kwargs = pty_connect_kwargs(secrets, settings, username=username)
    kwargs.pop("client_version", None)
    logger.info(
        "Connecting for PTY to {}@{}:{}",
        kwargs.get("username", "?"), kwargs.get("host"), kwargs.get("port"),
    )
    try:
        connection = await asyncssh.connect(**kwargs)
    except asyncssh.PermissionDenied as exc:
        raise PtyError(
            f"SSH authentication failed for {kwargs.get('username')}@{kwargs.get('host')}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise PtyError(f"SSH connection failed: {exc}") from exc

    session = PtySession(
        connection,
        term_type=term_type,
        cols=cols,
        rows=rows,
        encoding=encoding,
        owns_connection=True,
    )
    if settings.ssh_keepalive_sec > 0:
        try:
            connection.set_keepalive(settings.ssh_keepalive_sec)
        except Exception as exc:  # noqa: BLE001 - keepalive is not essential
            logger.debug("keepalive not set: {}", exc)
    try:
        await session.start()
    except Exception:
        await session.close()
        raise
    return session