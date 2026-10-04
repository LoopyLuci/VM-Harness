"""SSH client for interacting with the guest VM.

All SSH operations use asyncssh and are authenticated using credentials
loaded from the Secrets object (never exposed to tool callers).  The
Secrets object provides the password or private key internally; tools
only pass command/ path arguments.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from pathlib import Path

import asyncssh

from vm_harness.config import Secrets, VmMCPSettings

logger = logging.getLogger(__name__)

import threading

# ── Connection cache ────────────────────────────────────────────────────────────

_connection: asyncssh.SSHClientConnection | None = None
_connection_lock = threading.Lock()


# ── Connection management ───────────────────────────────────────────────────────

async def _connect(secrets: Secrets, settings: VmMCPSettings) -> asyncssh.SSHClientConnection:
    """Establish or return a cached SSH connection to the guest."""
    global _connection

    if _connection is not None:
        # Check if the connection is still alive
        try:
            if not _connection.is_closed():
                return _connection
        except Exception:
            pass
        _connection = None

    kwargs = settings.ssh_connect_kwargs(secrets)

    # ``known_hosts`` comes from settings and defaults to ``None``, which for
    # asyncssh means "do not verify the host key". That is deliberate for the
    # same reason as in ``vm_harness.pty``: the guest is a throwaway local VM
    # behind a QEMU NAT forward whose key changes on every reinstall. An
    # interactive terminal that refuses to open after a rebuild is useless
    # precisely when it is needed. Set ``ssh_known_hosts`` to pin it if a
    # deployment wants the strict behaviour back.
    if settings.ssh_known_hosts:
        kwargs["known_hosts"] = settings.ssh_known_hosts
    else:
        kwargs["known_hosts"] = None

    # Log attempt without exposing credentials
    ssh_desc = "key" if secrets.get_ssh_private_key() else "password"
    logger.info("Connecting to SSH %s on %s:%d (%s auth)", ssh_desc, settings.ssh_host, settings.ssh_port, settings.ssh_username)

    try:
        _connection = await asyncssh.connect(**kwargs)
        logger.info("SSH connected to %s@%s:%d", settings.ssh_username, settings.ssh_host, settings.ssh_port)
        if settings.ssh_keepalive_sec > 0:
            _connection.set_keepalive(settings.ssh_keepalive_sec)
        return _connection
    except asyncssh.PermissionDenied as e:
        raise RuntimeError(f"SSH authentication failed for {settings.ssh_username}@{settings.ssh_host}:{settings.ssh_port}") from e
    except Exception as e:
        raise RuntimeError(f"SSH connection failed: {e}") from e


async def _close() -> None:
    """Close the cached SSH connection."""
    global _connection
    if _connection is not None:
        try:
            _connection.close()
            await _connection.wait_closed()
        except Exception:
            pass
        _connection = None


async def disconnect() -> None:
    """Close the cached connection. Public alias for :func:`_close`.

    Exists because the GUI disconnect path called a name that did not exist:
    ``SSHBridge._disconnect_impl`` raised ``AttributeError`` on every Disconnect
    click, the generic handler turned it into an error signal, and the socket
    stayed open. A disconnect that cannot disconnect is worse than none.
    """
    await _close()


def settings_for_vm(
    base: VmMCPSettings,
    *,
    vm_name: str | None = None,
    ssh_host: str | None = None,
    ssh_port: int | None = None,
    ssh_username: str | None = None,
) -> VmMCPSettings:
    """A copy of ``base`` aimed at one specific VM.

    ``SSHBridge`` used to hold exactly one ``VmMCPSettings`` for the whole
    application, so with several VMs running every command went to whichever one
    the settings file happened to describe. Each VM in ``MultiVMManager`` has its
    own forwarded SSH port and user, so the target has to travel with the
    operation rather than being global state.
    """
    updates: dict[str, Any] = {}
    if vm_name:
        updates["vm_name"] = vm_name
    if ssh_host:
        updates["ssh_host"] = ssh_host
    if ssh_port:
        updates["ssh_port"] = int(ssh_port)
    if ssh_username:
        updates["ssh_username"] = ssh_username
    if not updates:
        return base
    try:
        return base.model_copy(update=updates)
    except AttributeError:  # pragma: no cover - a non-pydantic settings object
        for key, value in updates.items():
            setattr(base, key, value)
        return base


# ── Public helpers ──────────────────────────────────────────────────────────────

async def run_guest_command(
    command: str,
    timeout: int = 30,
    env: dict[str, str] | None = None,
    secrets: Secrets | None = None,
    settings: VmMCPSettings | None = None,
) -> dict[str, Any]:
    """Execute a shell command on the guest VM via SSH.

    Returns:
        dict with keys: exit_code, stdout, stderr, success
    """
    if secrets is None:
        from vm_harness.config import Secrets
        secrets = Secrets()
    if settings is None:
        from vm_harness.config import VmMCPSettings
        settings = VmMCPSettings()

    conn = await _connect(secrets, settings)

    try:
        process = await conn.execute(
            command,
            timeout=timeout,
            env=env,
            term_type="dumb",  # No terminal — non-interactive only
        )
        stdout, stderr = await process.wait()
        exit_code = process.exit_status

        return {
            "exit_code": exit_code if exit_code is not None else -1,
            "stdout": stdout.rstrip("\n") if stdout else "",
            "stderr": stderr.rstrip("\n") if stderr else "",
            "success": exit_code == 0,
        }
    except asyncio.TimeoutError:
        return {"exit_code": -1, "stdout": "", "stderr": f"Command timed out after {timeout}s", "success": False}
    except asyncssh.ConnectionLost as e:
        raise RuntimeError(f"SSH connection lost during command: {e}") from e


async def read_guest_file(
    path: str,
    max_bytes: int = 1048576,
    secrets: Secrets | None = None,
    settings: VmMCPSettings | None = None,
) -> tuple[str, str]:
    """Read a text file from the guest via SFTP.

    Returns (content, encoding).
    """
    if secrets is None:
        from vm_harness.config import Secrets
        secrets = Secrets()
    if settings is None:
        from vm_harness.config import VmMCPSettings
        settings = VmMCPSettings()

    conn = await _connect(secrets, settings)
    sftp = await conn.start_sftp_client()

    try:
        async with sftp.open(path, "r") as f:
            data = await f.read(max_bytes)
        return data, f.encoding or "utf-8"
    finally:
        await sftp.close()


async def write_guest_file(
    path: str,
    content: str,
    encoding: str = "utf-8",
    secrets: Secrets | None = None,
    settings: VmMCPSettings | None = None,
) -> int:
    """Write content to a file on the guest via SFTP.

    Creates parent directories if needed.  Returns bytes written.
    """
    if secrets is None:
        from vm_harness.config import Secrets
        secrets = Secrets()
    if settings is None:
        from vm_harness.config import VmMCPSettings
        settings = VmMCPSettings()

    conn = await _connect(secrets, settings)
    sftp = await conn.start_sftp_client()

    try:
        # Ensure parent directory exists
        parent = str(Path(path).parent)
        if parent and parent != "/":
            try:
                await sftp.mkdir(parent, exist_ok=True)
            except FileExistsError:
                pass

        async with sftp.open(path, "w", encoding=encoding) as f:
            await f.write(content)
        return len(content.encode(encoding))
    finally:
        await sftp.close()


async def list_guest_directory(
    path: str,
    detail: bool = True,
    secrets: Secrets | None = None,
    settings: VmMCPSettings | None = None,
) -> list[dict[str, Any]]:
    """List directory contents on the guest via SFTP.

    Returns a list of dicts: {name, type, size, mtime}.
    """
    if secrets is None:
        from vm_harness.config import Secrets
        secrets = Secrets()
    if settings is None:
        from vm_harness.config import VmMCPSettings
        settings = VmMCPSettings()

    conn = await _connect(secrets, settings)
    sftp = await conn.start_sftp_client()

    try:
        entries: list[dict[str, Any]] = []
        async for entry in sftp.listdir(path, flatten=False):
            info = await sftp.stat(entry)
            entries.append({
                "name": entry.basename,
                "type": "dir" if info.st_mode & 0o040000 else "file",
                "size": info.st_size,
                "mtime": info.st_mtime,
            })
        return entries
    finally:
        await sftp.close()


async def remove_guest_path(
    path: str,
    recursive: bool = False,
    secrets: Secrets | None = None,
    settings: VmMCPSettings | None = None,
) -> dict[str, Any]:
    """Remove a file or directory from the guest via SFTP.

    Returns info dict about the removed path, or raises on error.
    """
    if secrets is None:
        from vm_harness.config import Secrets
        secrets = Secrets()
    if settings is None:
        from vm_harness.config import VmMCPSettings
        settings = VmMCPSettings()

    conn = await _connect(secrets, settings)
    sftp = await conn.start_sftp_client()

    try:
        stat_result = await sftp.stat(path)
        if stat_result.st_mode & 0o040000 and recursive:
            # Remove directory recursively using shell rm
            conn2 = await _connect(secrets, settings)
            process = await conn2.execute(f"rm -rf {path}")
            await process.wait()
            return {"type": "dir", "size": stat_result.st_size}
        elif stat_result.st_mode & 0o040000:
            await sftp.rmdir(path)
        else:
            await sftp.remove(path)
        return {"type": "dir" if stat_result.st_mode & 0o040000 else "file", "size": stat_result.st_size}
    finally:
        await sftp.close()
