"""QMP Data Extractor — real VM information via QMP.

Provides typed access to QMP commands for GUI panels.
"""

from __future__ import annotations

import logging
import json
from typing import Any, Optional

logger = logging.getLogger("qemu-mcp.extractor")


class QMPExtractor:
    """Extract real VM data from QMP."""

    def __init__(self, host: str = "127.0.0.1", port: int = 4444):
        self._host = host
        self._port = port

    async def _send(self, cmd: str, args: dict | None = None) -> dict:
        """Send a QMP command and return the response."""
        from vm_harness.setup import QMPClient
        client = QMPClient(f"tcp:{self._host}:{self._port}")
        await client.connect()
        result = await client.send(cmd, args)
        await client.disconnect()
        return result

    async def get_status(self) -> dict[str, Any]:
        """Get VM running status."""
        try:
            result = await self._send("query-status")
            return result.get("return", {})
        except Exception as e:
            logger.error("Failed to get status: %s", e)
            return {}

    async def get_name(self) -> str:
        """Get VM name."""
        try:
            result = await self._send("query-name")
            return result.get("return", {}).get("name", "unknown")
        except (OSError, TimeoutError):
            return "unknown"

    async def get_uuid(self) -> str:
        """Get VM UUID."""
        try:
            result = await self._send("query-uuid")
            return result.get("return", {}).get("UUID", "")
        except (OSError, TimeoutError):
            return ""

    async def get_cpus(self) -> list[dict]:
        """Get CPU information."""
        try:
            result = await self._send("query-cpus")
            return result.get("return", [])
        except (OSError, TimeoutError):
            return []

    async def get_memory(self) -> dict[str, Any]:
        """Get memory information."""
        try:
            result = await self._send("query-memory")
            return result.get("return", {})
        except (OSError, TimeoutError):
            return {}

    async def get_balloon(self) -> dict[str, Any]:
        """Get balloon memory info."""
        try:
            result = await self._send("query-balloon")
            return result.get("return", {})
        except (OSError, TimeoutError):
            return {}

    async def get_version(self) -> dict[str, Any]:
        """Get QEMU version."""
        try:
            result = await self._send("query-version")
            return result.get("return", {})
        except (OSError, TimeoutError, json.JSONDecodeError):
            return {}

    async def get_kvm(self) -> bool:
        """Check if KVM is enabled."""
        try:
            result = await self._send("query-kvm")
            return result.get("return", {}).get("enabled", False)
        except (OSError, TimeoutError, json.JSONDecodeError):
            return False

    async def get_all(self) -> dict[str, Any]:
        """Get all available VM information."""
        status = await self.get_status()
        name = await self.get_name()
        uuid = await self.get_uuid()
        cpus = await self.get_cpus()
        version = await self.get_version()
        kvm = await self.get_kvm()

        return {
            "name": name,
            "uuid": uuid,
            "status": status.get("status", "unknown"),
            "running": status.get("running", False),
            "singlestep": status.get("singlestep", False),
            "cpus": len(cpus),
            "qemu_version": version.get("qemu", {}),
            "kvm_enabled": kvm,
        }
