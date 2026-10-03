"""QMP (QEMU Machine Protocol) client — low-level VM control via QMP socket.

Provides a thin async wrapper around the QMP JSON protocol.  QMP is the
out-of-band monitor interface that QEMU exposes when started with
-qmp socket,server,nowait.

Example:
    client = QMPClient("tcp:127.0.0.1:4444")
    await client.connect()
    status = await client.send("query-status")
    await client.disconnect()
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── QMP Client ──────────────────────────────────────────────────────────────────

class QMPClient:
    """Thin async wrapper around the QEMU QMP JSON protocol.

    QMP handshake:
      1. Connect to the socket (TCP or Unix domain)
      2. Send {"execute": "qmp_capabilities"} to enable command processing
      3. Send commands: {"execute": "<cmd>", "arguments": {...}}
      4. Read responses: {"return": {...}} or {"event": ..., "data": {...}}

    QEMU must be started with -qmp socket,server,nowait for this to work.
    """

    def __init__(self, uri: str, password: str | None = None, timeout_sec: float = 10.0):
        """Initialize a QMP client.

        Args:
            uri: Connection URI, e.g. "tcp:127.0.0.1:4444" or "unix:/tmp/qmp.sock"
            password: Optional QMP password (not commonly used)
            timeout_sec: Read timeout for each response
        """
        self.uri = uri
        self._password = password
        self._timeout = timeout_sec
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        # One command at a time on the socket: QMP answers in order, so two callers must not interleave.
        self._lock = asyncio.Lock()
        # Asynchronous events (STOP, RESUME, SHUTDOWN, ...) that arrived while waiting for an answer, newest last.
        self.events: "collections.deque[dict[str, Any]]" = collections.deque(maxlen=500)

    async def connect(self) -> None:
        """Connect to QMP and complete the handshake.

        Raises:
            RuntimeError: If connection or handshake fails.
        """
        if self.uri.startswith("unix:"):
            path = self.uri[6:]
            self._reader, self._writer = await asyncio.open_unix_connection(path)
        else:
            # Parse tcp:host:port — host may contain colons (IPv6) or dots (IPv4)
            # Format: tcp:HOST:PORT
            rest = self.uri[4:]  # strip "tcp:"
            last_colon = rest.rfind(":")
            host = rest[:last_colon]
            port = int(rest[last_colon + 1:])
            self._reader, self._writer = await asyncio.open_connection(host, port)

        self._connected = True

        # QMP handshake: read greeting, then enable command processing
        await self._read_greeting()
        await self.send("qmp_capabilities")

        logger.info("QMP connected: %s", self.uri)

    async def _read_greeting(self) -> dict[str, Any]:
        """Read the QMP greeting message sent by QEMU on connect.

        QEMU sends a JSON greeting like:
            {"QMP": {"version": {...}, "capabilities": [...]}}
        before accepting any commands.  This must be consumed first.
        """
        assert self._reader is not None
        data = await asyncio.wait_for(
            self._reader.readuntil(b"\n"),
            timeout=self._timeout,
        )
        if not data:
            raise RuntimeError("QMP connection closed while reading greeting")
        greeting = json.loads(data.decode())
        if "QMP" not in greeting:
            raise RuntimeError(f"Invalid QMP greeting: {greeting}")
        logger.debug("QMP greeting received")
        return greeting

    async def send(self, cmd: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a QMP command and return the parsed response.

        Args:
            cmd: QMP command name (e.g. "query-status")
            args: Optional command arguments dict

        Returns:
            Parsed JSON response dict (typically {"return": {...}})

        Raises:
            RuntimeError: If not connected or QMP returns an error.
        """
        if not self._connected:
            raise RuntimeError("QMP not connected")

        message: dict[str, Any] = {"execute": cmd}
        if args:
            message["arguments"] = args

        payload = json.dumps(message) + "\n"
        assert self._writer is not None
        async with self._lock:
            try:
                self._writer.write(payload.encode())
                await self._writer.drain()
                return await self._read_response()
            except (ConnectionError, asyncio.IncompleteReadError, OSError):
                self._connected = False
                raise

    async def _read_response(self) -> dict[str, Any]:
        """Read the answer to the command just sent, setting aside any events QEMU sends first."""
        assert self._reader is not None
        while True:
            data = await asyncio.wait_for(
                self._reader.readuntil(b"\n"),
                timeout=self._timeout,
            )
            if not data:
                raise RuntimeError("QMP connection closed while reading response")
            response = json.loads(data.decode())
            if "event" not in response:
                break
            self.events.append(response)
        if "error" in response:
            raise RuntimeError(
                f"QMP error: {response['error'].get('desc', 'unknown error')}"
            )
        return response

    async def disconnect(self) -> None:
        """Close the QMP connection."""
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        self._connected = False
        logger.info("QMP disconnected: %s", self.uri)

    @property
    def is_connected(self) -> bool:
        """Whether the QMP connection is active."""
        return self._connected


# ── Helper: connect with retries ────────────────────────────────────────────────

async def connect_qmp_with_retry(
    uri: str,
    max_retries: int = 10,
    retry_delay: float = 0.5,
    **kwargs: Any,
) -> QMPClient:
    """Connect to QMP, retrying on failure.

    QEMU can take a few seconds to open the QMP socket after startup.
    This function retries the connection until it succeeds or max_retries
    is exhausted.

    Args:
        uri: QMP connection URI
        max_retries: Maximum number of connection attempts
        retry_delay: Seconds between retries
        **kwargs: Extra args passed to QMPClient

    Returns:
        Connected QMPClient

    Raises:
        RuntimeError: If connection fails after all retries.
    """
    for attempt in range(1, max_retries + 1):
        try:
            client = QMPClient(uri, **kwargs)
            await client.connect()
            return client
        except (ConnectionRefusedError, FileNotFoundError, OSError, RuntimeError) as e:
            logger.debug("QMP connection attempt %d/%d failed: %s", attempt, max_retries, e)
            if attempt < max_retries:
                await asyncio.sleep(retry_delay)
            else:
                raise RuntimeError(
                    f"QMP connection failed after {max_retries} attempts: {e}"
                ) from e


# ── Common QMP commands (convenience wrappers) ──────────────────────────────────

async def query_status(client: QMPClient) -> dict[str, Any]:
    """Get the VM status: running, paused, shutdown, etc."""
    return await client.send("query-status")


async def system_reset(client: QMPClient) -> dict[str, Any]:
    """Hard reset the VM (equivalent to pressing the reset button)."""
    return await client.send("system_reset")


async def system_powerdown(client: QMPClient) -> dict[str, Any]:
    """Send ACPI power button event to the guest (graceful shutdown)."""
    return await client.send("system_powerdown")


async def eject_device(client: QMPClient, device: str = "ide0-cd0") -> dict[str, Any]:
    """Eject a removable device (e.g. the installer ISO)."""
    return await client.send("eject", {"device": device})


async def query_block(client: QMPClient) -> dict[str, Any]:
    """Get block device information (disks, CDs, etc.)."""
    return await client.send("query-block")


async def query_bootindex(client: QMPClient) -> dict[str, Any]:
    """Get the current boot index / boot order."""
    return await client.send("query-bootindex")


async def stop(client: QMPClient) -> dict[str, Any]:
    """Stop CPU execution (suspend the VM)."""
    return await client.send("stop")


async def cont(client: QMPClient) -> dict[str, Any]:
    """Continue CPU execution (resume a suspended VM)."""
    return await client.send("cont")
