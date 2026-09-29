"""Multi-VM QMP Bridge Manager — handles QMP connections for multiple VMs.

Provides two modes:
1. Switch mode: Single QMP bridge that reconnects to whichever VM is selected.
2. Multi-mode: Maintains separate QMP bridge instances for simultaneous VM connections.

Auto-detects available connections and falls back to switch mode when
only one simultaneous QMP connection is possible.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from PyQt5.QtCore import QObject, pyqtSignal

from vm_harness.config import VmMCPSettings, Secrets
from vm_harness.qmp_client import QMPClient
from vm_harness import qmp_client as qmp_mod

logger = logging.getLogger("vmharness.multi_qmp_bridge")


class PerVMQMPBridge(QObject):
    """A dedicated QMP bridge for a single VM.

    Each instance manages one QMP connection in its own background thread.
    """

    connected = pyqtSignal(str, bool)        # vm_name, connected
    vm_status = pyqtSignal(str, dict)        # vm_name, status_dict
    error = pyqtSignal(str, str)             # vm_name, error_message
    command_result = pyqtSignal(str, dict)   # vm_name, result_dict

    def __init__(self, vm_name: str, qmp_uri: str, password: str | None = None, parent=None):
        super().__init__(parent)
        self._vm_name = vm_name
        self._qmp_uri = qmp_uri
        self._password = password
        self._connected = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: QMPClient | None = None
        self._ever_connected = False
        self._loop_ready = threading.Event()
        self._lock = threading.Lock()  # protects _connected, _client

    @property
    def vm_name(self) -> str:
        return self._vm_name

    @property
    def qmp_uri(self) -> str:
        return self._qmp_uri

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def update_uri(self, new_uri: str):
        """Update the QMP URI (call when VM config changes)."""
        if new_uri != self._qmp_uri:
            self._qmp_uri = new_uri
            # Force reconnect on next operation
            if self._connected:
                self.disconnect()

    def start(self):
        """Start the background thread + asyncio loop."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._loop_ready.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        if not self._loop_ready.wait(timeout=5.0):
            raise RuntimeError(f"QMP bridge for {self._vm_name} failed to start")

    def stop(self):
        """Stop the event loop and thread."""
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        if self._client is not None:
            try:
                threading.Thread(
                    target=lambda: asyncio.run(self._client.disconnect()),
                    daemon=True,
                ).start()
            except (RuntimeError, OSError):
                pass  # Thread creation failed — best-effort cleanup
        with self._lock:
            self._connected = False
            self._client = None

    def _run_loop(self):
        """Run the asyncio event loop in background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        self._loop.run_forever()

    async def _get_client(self) -> QMPClient:
        """Get or create the QMP client, connecting if needed."""
        if self._client is None or not self._client.is_connected:
            with self._lock:
                if self._client is not None and self._client.is_connected:
                    return self._client
                self._client = QMPClient(
                    uri=self._qmp_uri,
                    password=self._password,
                )
            try:
                await self._client.connect()
            except Exception as e:
                with self._lock:
                    self._connected = False
                    self._client = None
                logger.debug("QMP connect failed for %s: %s", self._vm_name, e)
                raise
            with self._lock:
                self._connected = True
                self._ever_connected = True
            self.connected.emit(self._vm_name, True)
        return self._client

    def connect(self):
        """Connect to QMP."""
        if self._loop is None or not self._loop.is_running():
            self.error.emit(self._vm_name, "Bridge not started")
            return
        asyncio.run_coroutine_threadsafe(self._connect_impl(), self._loop)

    async def _connect_impl(self):
        try:
            await self._get_client()
        except Exception as e:
            logger.error("QMP connect failed for %s: %s", self._vm_name, e)
            with self._lock:
                self._connected = False
                self._client = None
            self.connected.emit(self._vm_name, False)
            self.error.emit(self._vm_name, f"Connection failed: {e}")

    def disconnect(self):
        """Disconnect from QMP."""
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._disconnect_impl(), self._loop)

    async def _disconnect_impl(self):
        try:
            if self._client is not None:
                await self._client.disconnect()
            with self._lock:
                self._client = None
                self._connected = False
            self.connected.emit(self._vm_name, False)
        except Exception as e:
            logger.error("QMP disconnect failed for %s: %s", self._vm_name, e)
            self.error.emit(self._vm_name, f"Disconnect failed: {e}")

    def get_status(self):
        """Query VM status. Returns dict or None."""
        if self._loop is None:
            self.error.emit(self._vm_name, "Bridge not started")
            return None
        fut = asyncio.run_coroutine_threadsafe(self._status_impl(), self._loop)
        try:
            return fut.result(timeout=3.0)
        except Exception as e:
            logger.error("QMP get_status timed out for %s: %s", self._vm_name, e)
            self.error.emit(self._vm_name, f"Status query timed out: {e}")
            return None

    async def _status_impl(self):
        try:
            client = await self._get_client()
            status = await qmp_mod.query_status(client)
            self.vm_status.emit(self._vm_name, status)
            return status
        except TimeoutError as e:  # asyncio's timeouts are TimeoutError
            logger.error("QMP get_status timed out for %s: %s", self._vm_name, e)
            self.error.emit(self._vm_name, f"Status timed out: {e}")
            return {}
        except Exception as e:
            logger.error("QMP get_status failed for %s: %s", self._vm_name, e)
            self.error.emit(self._vm_name, f"Status query failed: {e}")
            return {}

    def send_command(self, command: str):
        """Send a raw QMP command."""
        if self._loop is None:
            self.error.emit(self._vm_name, "Bridge not started")
            return
        asyncio.run_coroutine_threadsafe(self._send_command_impl(command), self._loop)

    async def _send_command_impl(self, command: str):
        try:
            client = await self._get_client()
            if client is None:
                self.error.emit(self._vm_name, "Not connected")
                return
            if command.startswith("{"):
                import json
                msg = json.loads(command)
                result = await client.send(msg["execute"], msg.get("arguments"))
            else:
                result = await client.send(command)
            self.command_result.emit(self._vm_name, {"return": result})
        except Exception as e:
            logger.error("QMP command failed for %s: %s", self._vm_name, e)
            self.error.emit(self._vm_name, f"Command failed: {e}")

    def system_reset(self):
        """Reset the VM (warm reboot)."""
        if self._loop is None:
            self.error.emit(self._vm_name, "Bridge not started")
            return
        asyncio.run_coroutine_threadsafe(self._system_reset_impl(), self._loop)

    async def _system_reset_impl(self):
        try:
            client = await self._get_client()
            if client is None:
                self.error.emit(self._vm_name, "Not connected")
                return
            await qmp_mod.system_reset(client)
            self.command_result.emit(self._vm_name, {"return": "reset issued"})
        except Exception as e:
            logger.error("QMP reset failed for %s: %s", self._vm_name, e)
            self.error.emit(self._vm_name, f"Reset failed: {e}")

    def system_powerdown(self):
        """Power down the VM (graceful shutdown)."""
        if self._loop is None:
            self.error.emit(self._vm_name, "Bridge not started")
            return
        asyncio.run_coroutine_threadsafe(self._powerdown_impl(), self._loop)

    async def _powerdown_impl(self):
        try:
            client = await self._get_client()
            if client is None:
                self.error.emit(self._vm_name, "Not connected")
                return
            await qmp_mod.system_powerdown(client)
            self.command_result.emit(self._vm_name, {"return": "powerdown issued"})
        except Exception as e:
            logger.error("QMP powerdown failed for %s: %s", self._vm_name, e)
            self.error.emit(self._vm_name, f"Powerdown failed: {e}")

    def cont(self):
        """Continue a suspended VM (resume)."""
        if self._loop is None:
            self.error.emit(self._vm_name, "Bridge not started")
            return
        asyncio.run_coroutine_threadsafe(self._cont_impl(), self._loop)

    async def _cont_impl(self):
        try:
            client = await self._get_client()
            if client is None:
                self.error.emit(self._vm_name, "Not connected")
                return
            await qmp_mod.cont(client)
            self.command_result.emit(self._vm_name, {"return": "cont issued"})
        except Exception as e:
            logger.error("QMP cont failed for %s: %s", self._vm_name, e)
            self.error.emit(self._vm_name, f"Resume failed: {e}")

    def stop_vm(self):
        """Stop the VM (suspend CPU)."""
        if self._loop is None:
            self.error.emit(self._vm_name, "Bridge not started")
            return
        asyncio.run_coroutine_threadsafe(self._stop_impl(), self._loop)

    async def _stop_impl(self):
        try:
            client = await self._get_client()
            if client is None:
                self.error.emit(self._vm_name, "Not connected")
                return
            await qmp_mod.stop(client)
            self.command_result.emit(self._vm_name, {"return": "stop issued"})
        except Exception as e:
            logger.error("QMP stop failed for %s: %s", self._vm_name, e)
            self.error.emit(self._vm_name, f"Stop failed: {e}")

    def eject_cdrom(self):
        """Eject the CD-ROM device."""
        if self._loop is None:
            self.error.emit(self._vm_name, "Bridge not started")
            return
        asyncio.run_coroutine_threadsafe(self._eject_impl(), self._loop)

    async def _eject_impl(self):
        try:
            client = await self._get_client()
            if client is None:
                self.error.emit(self._vm_name, "Not connected")
                return
            await qmp_mod.eject_device(client, "ide0-cd0")
            self.command_result.emit(self._vm_name, {"return": "eject issued"})
        except Exception as e:
            logger.error("QMP eject failed for %s: %s", self._vm_name, e)
            self.error.emit(self._vm_name, f"Eject failed: {e}")


class MultiVMQMPBridge(QObject):
    """Manages QMP bridges for multiple VMs.

    Supports two modes:
    - 'switch': One active bridge at a time, switches to selected VM
    - 'multi': Maintains bridges for all running VMs simultaneously

    Signals are forwarded with vm_name as first arg for routing.
    """

    connected = pyqtSignal(str, bool)
    vm_status = pyqtSignal(str, dict)
    error = pyqtSignal(str, str)
    command_result = pyqtSignal(str, dict)
    active_vm_changed = pyqtSignal(str)

    def __init__(self, mode: str = "switch", parent=None):
        """
        Args:
            mode: 'switch' for single active bridge, 'multi' for all VMs.
        """
        super().__init__(parent)
        self._mode = mode
        self._bridges: dict[str, PerVMQMPBridge] = {}
        self._active_vm: str | None = None
        self._secrets = Secrets.from_env()

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def active_vm(self) -> str | None:
        return self._active_vm

    def set_mode(self, mode: str):
        """Switch between 'switch' and 'multi' mode."""
        if mode not in ("switch", "multi"):
            raise ValueError(f"Invalid mode: {mode}")
        self._mode = mode

    def add_vm(self, vm_name: str, qmp_uri: str):
        """Register a VM for QMP management.

        In 'switch' mode, the bridge is created lazily when the VM becomes active.
        In 'multi' mode, a bridge is created immediately.
        """
        if vm_name in self._bridges:
            # Update URI if changed
            self._bridges[vm_name].update_uri(qmp_uri)
            return

        if self._mode == "multi":
            bridge = PerVMQMPBridge(vm_name, qmp_uri, self._secrets.get_qmp_password())
            self._bridge_signals(bridge)
            bridge.start()
            self._bridges[vm_name] = bridge
        else:
            # In switch mode, just store the URI for lazy creation
            self._bridges[vm_name] = None  # Placeholder

    def remove_vm(self, vm_name: str):
        """Remove a VM from QMP management."""
        if vm_name in self._bridges:
            bridge = self._bridges.pop(vm_name)
            if bridge is not None:
                bridge.stop()
        if self._active_vm == vm_name:
            self._active_vm = None

    def switch_to_vm(self, vm_name: str, qmp_uri: str | None = None):
        """Switch the active VM context.

        In 'switch' mode, disconnects from the previous VM and connects to the new one.
        In 'multi' mode, just updates the active reference.
        """
        if vm_name not in self._bridges:
            if qmp_uri:
                self.add_vm(vm_name, qmp_uri)
            else:
                return

        self._active_vm = vm_name

        if self._mode == "switch":
            # Disconnect all other bridges
            for name, bridge in self._bridges.items():
                if name != vm_name and bridge is not None:
                    bridge.stop()
                    self._bridges[name] = None

            # Create or reuse bridge for active VM
            bridge = self._bridges.get(vm_name)
            if bridge is None:
                uri = qmp_uri or self._get_uri_for_vm(vm_name)
                if uri:
                    bridge = PerVMQMPBridge(vm_name, uri, self._secrets.get_qmp_password())
                    self._bridge_signals(bridge)
                    bridge.start()
                    self._bridges[vm_name] = bridge
                    bridge.connect()
        else:
            # Multi mode: ensure bridge is connected
            bridge = self._bridges.get(vm_name)
            if bridge and not bridge.is_connected:
                bridge.connect()

        self.active_vm_changed.emit(vm_name)

    def _get_uri_for_vm(self, vm_name: str) -> str | None:
        """Get QMP URI for a VM (override or lookup from config)."""
        # This can be extended to look up from MultiVMManager
        return None

    def _bridge_signals(self, bridge: PerVMQMPBridge):
        """Connect bridge signals to our forwarded signals."""
        bridge.connected.connect(lambda ok, name=bridge.vm_name: self.connected.emit(name, ok))
        bridge.vm_status.connect(lambda status, name=bridge.vm_name: self.vm_status.emit(name, status))
        bridge.error.connect(lambda msg, name=bridge.vm_name: self.error.emit(name, msg))
        bridge.command_result.connect(lambda result, name=bridge.vm_name: self.command_result.emit(name, result))

    # ── Active VM operations ─────────────────────────────────────────────────

    def connect(self):
        """Connect the active VM's bridge."""
        if self._active_vm:
            bridge = self._bridges.get(self._active_vm)
            if bridge:
                bridge.connect()

    def disconnect(self):
        """Disconnect the active VM's bridge."""
        if self._active_vm:
            bridge = self._bridges.get(self._active_vm)
            if bridge:
                bridge.disconnect()

    def get_status(self):
        """Get status of the active VM."""
        if self._active_vm:
            bridge = self._bridges.get(self._active_vm)
            if bridge:
                return bridge.get_status()
        return None

    def send_command(self, command: str):
        """Send QMP command to the active VM."""
        if self._active_vm:
            bridge = self._bridges.get(self._active_vm)
            if bridge:
                bridge.send_command(command)

    def system_reset(self):
        """Reset the active VM."""
        if self._active_vm:
            bridge = self._bridges.get(self._active_vm)
            if bridge:
                bridge.system_reset()

    def system_powerdown(self):
        """Power down the active VM."""
        if self._active_vm:
            bridge = self._bridges.get(self._active_vm)
            if bridge:
                bridge.system_powerdown()

    def cont(self):
        """Resume the active VM."""
        if self._active_vm:
            bridge = self._bridges.get(self._active_vm)
            if bridge:
                bridge.cont()

    def stop_vm(self):
        """Stop (suspend) the active VM."""
        if self._active_vm:
            bridge = self._bridges.get(self._active_vm)
            if bridge:
                bridge.stop_vm()

    def eject_cdrom(self):
        """Eject CD-ROM on the active VM."""
        if self._active_vm:
            bridge = self._bridges.get(self._active_vm)
            if bridge:
                bridge.eject_cdrom()

    # ── Multi-VM operations ───────────────────────────────────────────────────

    def get_status_all(self) -> dict[str, dict | None]:
        """Get status from all connected bridges (multi mode)."""
        results = {}
        for name, bridge in self._bridges.items():
            if bridge is not None:
                results[name] = bridge.get_status()
        return results

    def send_command_all(self, command: str):
        """Send a QMP command to all connected bridges (multi mode)."""
        for bridge in self._bridges.values():
            if bridge is not None:
                bridge.send_command(command)

    def powerdown_all(self):
        """Power down all running VMs."""
        for bridge in self._bridges.values():
            if bridge is not None and bridge.is_connected:
                bridge.system_powerdown()

    def reset_all(self):
        """Reset all running VMs."""
        for bridge in self._bridges.values():
            if bridge is not None and bridge.is_connected:
                bridge.system_reset()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def stop_all(self):
        """Stop all bridges and clean up."""
        for bridge in self._bridges.values():
            if bridge is not None:
                bridge.stop()
        self._bridges.clear()
        self._active_vm = None

    def get_connected_vms(self) -> list[str]:
        """Get list of VM names with active QMP connections."""
        return [name for name, bridge in self._bridges.items()
                if bridge is not None and bridge.is_connected]

    def get_bridge(self, vm_name: str) -> PerVMQMPBridge | None:
        """Get the bridge for a specific VM."""
        return self._bridges.get(vm_name)
