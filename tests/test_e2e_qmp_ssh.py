"""E2E tests — QMP + SSH bridges against a live QEMU instance.

Requires:
- QEMU running with: -qmp tcp:127.0.0.1:4444,server,nowait
- SSH forwarding: -netdev user,hostfwd=tcp:127.0.0.1:2222-:22
- SSH server running inside guest (e.g., dropbear/sshd on port 22)

Run: pytest tests/test_e2e_qmp_ssh.py -v -s
"""

from __future__ import annotations

import asyncio
import socket
import time
import pytest

# Ensure offscreen for any Qt imports
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"


def _qmp_port_open() -> bool:
    """True only if QEMU's QMP answers on 4444: it greets with {"QMP": ...}. Another program on the port (a Java
    service here) must not make these tests run against it."""
    try:
        with socket.create_connection(("127.0.0.1", 4444), timeout=2) as s:
            s.settimeout(2)
            return b'"QMP"' in s.recv(4096)
    except OSError:
        return False


@pytest.fixture(scope="module")
def settings():
    from vm_harness.config import VmMCPSettings

    s = VmMCPSettings()
    return s


# ── QMP integration tests (single event loop per test) ───────────────────────


class TestQMPBridgeLive:
    """End-to-end QMP tests against running QEMU.

    Each test uses a single asyncio.run() so the reader/writer stay on
    the same event loop.  Skipped when QEMU is not running.
    """

    @pytest.mark.skipif(
        not _qmp_port_open(), reason="QMP port 4444 not open — QEMU not running"
    )
    def test_qmp_query_status(self, settings):
        from vm_harness.qmp_client import QMPClient
        from vm_harness import qmp_client as qmp_mod

        async def _run():
            client = QMPClient(uri=settings.qmp_uri())
            await client.connect()
            try:
                status = await qmp_mod.query_status(client)
                assert isinstance(status, dict)
                assert "return" in status
                machines = await client.send("query-machines")
                assert isinstance(machines, dict)
                assert "return" in machines
            finally:
                await client.disconnect()

        asyncio.run(_run())

    @pytest.mark.skipif(
        not _qmp_port_open(), reason="QMP port 4444 not open — QEMU not running"
    )
    def test_qmp_system_reset(self, settings):
        from vm_harness.qmp_client import QMPClient
        from vm_harness import qmp_client as qmp_mod

        async def _run():
            client = QMPClient(uri=settings.qmp_uri())
            await client.connect()
            try:
                await qmp_mod.system_reset(client)
            finally:
                await client.disconnect()

        asyncio.run(_run())

    @pytest.mark.skipif(
        not _qmp_port_open(), reason="QMP port 4444 not open — QEMU not running"
    )
    def test_qmp_stop_cont(self, settings):
        from vm_harness.qmp_client import QMPClient
        from vm_harness import qmp_client as qmp_mod

        async def _run():
            client = QMPClient(uri=settings.qmp_uri())
            await client.connect()
            try:
                await qmp_mod.stop(client)
                await qmp_mod.cont(client)
            finally:
                await client.disconnect()

        asyncio.run(_run())

    @pytest.mark.skipif(
        not _qmp_port_open(), reason="QMP port 4444 not open — QEMU not running"
    )
    def test_qmp_eject_device(self, settings):
        from vm_harness.qmp_client import QMPClient
        from vm_harness import qmp_client as qmp_mod

        async def _run():
            client = QMPClient(uri=settings.qmp_uri())
            await client.connect()
            try:
                try:
                    await qmp_mod.eject_device(client, "ide0-cd0")
                except RuntimeError:
                    pass  # Expected when no CD-ROM present
            finally:
                await client.disconnect()

        asyncio.run(_run())


# ── QMP Bridge (PyQt5 signal wrapper) test ────────────────────────────────────


class TestQMPBridgeSignals:
    """Verify QMPBridge emits signals correctly.

    Skipped when QEMU is not running."""