"""KVM Backend — implements HypervisorBackend for Linux KVM/libvirt.

Uses libvirt (virsh / Python libvirt module) to manage KVM/QEMU VMs.
Only available on Linux with /dev/kvm present and libvirt installed.

Config keys:
    libvirt_uri: Libvirt connection URI (default: qemu:///system)
    vms_dir: VM inventory directory (default: ~/.qemu-mcp/kvm-vms)
    pool_name: Default storage pool name (default: default)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess

from vm_harness import _proc
# Suppress CLI console windows on Windows
CREATE_NO_WINDOW = 0x08000000
from datetime import datetime
from pathlib import Path
from typing import Any

from vm_harness.hypervisor.backend import (
    BackendNotAvailableError,
    HypervisorBackend,
    HypervisorError,
    OperationNotSupportedError,
    VMAlreadyRunningError,
    VMConfig,
    VMConsole,
    VMDisplay,
    VMDisplayType,
    VMGuestInfo,
    VMMetrics,
    VMNetwork,
    VMNetworkMode,
    VMNotFoundError,
    VMNotRunningError,
    VMSnapshot,
    VMState,
    VMStatus,
)

logger = logging.getLogger(__name__)

DEFAULT_LIBVIRT_URI = "qemu:///system"


def _run_virsh_sync(args: list[str], timeout: int = 30, uri: str = DEFAULT_LIBVIRT_URI) -> subprocess.CompletedProcess:
    return _proc.run_sync(["virsh", "-c", uri] + args, timeout=timeout)


async def _run_virsh(args: list[str], timeout: int = 30, uri: str = DEFAULT_LIBVIRT_URI) -> subprocess.CompletedProcess:
    """Run a virsh command off the event loop."""
    return await _proc.run(["virsh", "-c", uri] + args, timeout=timeout)


# ── KVMBackend ────────────────────────────────────────────────────────────────

class KVMBackend(HypervisorBackend):
    """Linux KVM/libvirt backend.

    Manages VMs through libvirt using virsh commands. Supports full QMP-like
    lifecycle through libvirt's native API.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._libvirt_uri = self._config.get("libvirt_uri", DEFAULT_LIBVIRT_URI)
        self._vms_dir = Path(self._config.get("vms_dir", str(Path.home() / ".qemu-mcp" / "kvm-vms")))
        self._vms_dir.mkdir(parents=True, exist_ok=True)
        self._pool_name = self._config.get("pool_name", "default")

    @property
    def default_name(self) -> str:
        return "kvm"

    @property
    def display_name(self) -> str:
        return "KVM / libvirt"

    @property
    def version(self) -> str:
        try:
            result = _run_virsh_sync(["version"], uri=self._libvirt_uri)
            if result.returncode == 0:
                return result.stdout.split("\n")[0]
        except Exception:
            pass
        return "unknown"

    @property
    def is_available(self) -> bool:
        if platform.system() != "Linux":
            return False
        return os.path.exists("/dev/kvm") and shutil.which("virsh") is not None

    @property
    def supported_features(self) -> set[str]:
        return {
            "create", "destroy", "start", "stop", "pause", "resume",
            "reset", "reboot", "status", "list", "snapshots",
            "spice", "vnc", "metrics", "exec", "clone",
            "console", "guest_agent", "network", "disk_resize",
            "usb_passthrough", "live_migration", "memory_balloon",
            "import", "export",
        }

    # ── Initialization ───────────────────────────────────────────────────────

    async def initialize(self) -> None:
        if not self.is_available:
            raise BackendNotAvailableError(
                "KVM is not available. Ensure /dev/kvm exists and virsh is installed."
            )
        await super().initialize()

    async def shutdown(self) -> None:
        await super().shutdown()

    # ── Discovery ────────────────────────────────────────────────────────────

    async def list_vms(self) -> list[str]:
        result = await _run_virsh(["list", "--all", "--name"])
        if result.returncode == 0:
            return [line.strip() for line in result.stdout.split("\n") if line.strip()]
        return []

    # ── VM Lifecycle ─────────────────────────────────────────────────────────

    async def create_vm(self, config: VMConfig) -> str:
        if await self.find_vm(config.name):
            raise VMAlreadyRunningError(f"VM '{config.name}' already exists")

        # Create disk
        disk_path = config.disk_path or f"/var/lib/libvirt/images/{config.name}.qcow2"
        if config.disk_size_gb > 0 and not os.path.isfile(disk_path):
            await _proc.run(
                ["qemu-img", "create", "-f", config.disk_format or "qcow2",
                 disk_path, f"{config.disk_size_gb}G"],
                check=True, capture_output=True, text=True, timeout=60
            )

        # Build XML
        xml_content = self._build_domain_xml(config, disk_path)
        xml_path = self._vms_dir / f"{config.name}.xml"
        with open(xml_path, "w", encoding="utf-8") as f:
            f.write(xml_content)

        # Define domain
        result = await _proc.run(
            ["virsh", "-c", self._libvirt_uri, "define", str(xml_path)],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise HypervisorError(f"Failed to define VM: {result.stderr}")

        # Save metadata
        vm_config = {
            "name": config.name,
            "description": config.description,
            "disk_path": disk_path,
            "ram_mb": config.ram_mb,
            "cpus": config.cpus,
            "created_at": datetime.now().isoformat(),
        }
        config_path = self._vms_dir / f"{config.name}.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(vm_config, f, indent=2)

        logger.info("Created KVM VM '%s'", config.name)
        return config.name

    async def destroy_vm(self, name: str) -> None:
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        # Stop if running
        try:
            await self.stop_vm(name, force=True)
        except Exception:
            pass

        # Undefine
        result = await _proc.run(
            ["virsh", "-c", self._libvirt_uri, "undefine", name, "--remove-all-storage"],
            capture_output=True, text=True, timeout=30
        )

        # Remove metadata
        config_path = self._vms_dir / f"{name}.json"
        xml_path = self._vms_dir / f"{name}.xml"
        if config_path.exists():
            config_path.unlink()
        if xml_path.exists():
            xml_path.unlink()

        logger.info("Destroyed KVM VM '%s'", name)

    async def start_vm(self, name: str, headless: bool = False) -> None:
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")
        result = await _proc.run(
            ["virsh", "-c", self._libvirt_uri, "start", name],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise HypervisorError(f"Failed to start VM '{name}': {result.stderr}")
        logger.info("Started KVM VM '%s'", name)

    async def stop_vm(self, name: str, force: bool = False) -> None:
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")
        cmd = "destroy" if force else "shutdown"
        result = await _proc.run(
            ["virsh", "-c", self._libvirt_uri, cmd, name],
            capture_output=True, text=True, timeout=30
        )
        logger.info("Stopped KVM VM '%s'", name)

    async def pause_vm(self, name: str) -> None:
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")
        await _proc.run(
            ["virsh", "-c", self._libvirt_uri, "suspend", name],
            capture_output=True, text=True, timeout=30
        )

    async def resume_vm(self, name: str) -> None:
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")
        await _proc.run(
            ["virsh", "-c", self._libvirt_uri, "resume", name],
            capture_output=True, text=True, timeout=30
        )

    async def reset_vm(self, name: str) -> None:
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")
        await _proc.run(
            ["virsh", "-c", self._libvirt_uri, "reset", name],
            capture_output=True, text=True, timeout=30
        )

    async def reboot_vm(self, name: str, graceful: bool = True) -> None:
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")
        cmd = ["reboot"] if graceful else ["reset", "--force"]
        await _proc.run(
            ["virsh", "-c", self._libvirt_uri, cmd[0], name],
            capture_output=True, text=True, timeout=30
        )

    # ── Status / Info ────────────────────────────────────────────────────────

    async def get_status(self, name: str) -> VMStatus:
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        status = VMStatus(name=name, backend_name=self.default_name)
        result = await _proc.run(
            ["virsh", "-c", self._libvirt_uri, "domstate", name],
            capture_output=True, text=True, timeout=10
        )
        state_map = {
            "running": VMState.RUNNING,
            "shut off": VMState.STOPPED,
            "paused": VMState.PAUSED,
            "shutting down": VMState.STOPPING,
        }
        if result.returncode == 0:
            state_str = result.stdout.strip().lower()
            status.state = state_map.get(state_str, VMState.UNKNOWN)

        # Get memory and CPU info
        info_result = await _proc.run(
            ["virsh", "-c", self._libvirt_uri, "dominfo", name],
            capture_output=True, text=True, timeout=10
        )
        for line in info_result.stdout.split("\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip()
                if key == "CPU(s)":
                    status.cpus_allocated = int(value)
                elif key == "Max memory":
                    status.ram_allocated_mb = int(value) // 1024
                elif key == "Used memory":
                    status.ram_usage_mb = int(value) // 1024

        return status

    async def get_config(self, name: str) -> VMConfig | None:
        if not await self.find_vm(name):
            return None
        result = await _proc.run(
            ["virsh", "-c", self._libvirt_uri, "dumpxml", name],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return VMConfig(name=name)
        return None

    # ── Display ──────────────────────────────────────────────────────────────

    async def get_display(self, name: str) -> VMDisplay:
        display = VMDisplay()
        display.display_type = VMDisplayType.SPICE
        display.host = "127.0.0.1"
        display.port = 5900
        display.uri = f"spice://{display.host}:{display.port}"
        return display

    # ── Guest Agent / Exec ───────────────────────────────────────────────────

    async def guest_exec(self, name: str, command: str,
                         args: list[str] | None = None,
                         env: dict[str, str] | None = None,
                         timeout: int = 30,
                         capture_output: bool = True) -> dict[str, Any]:
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        # Use virsh qemu-agent-command
        agent_args = ["qemu-agent-command", name, f"{{ 'command': 'guest-exec', 'arguments': {{ 'path': '{command}', 'arg': {args or []}, 'capture-output': {str(capture_output).lower()} }} }}"]
        result = await _proc.run(
            ["virsh", "-c", self._libvirt_uri] + agent_args,
            capture_output=True, text=True, timeout=timeout
        )
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "timed_out": False,
        }

    # ── Snapshots ────────────────────────────────────────────────────────────

    async def list_snapshots(self, name: str) -> list[VMSnapshot]:
        result = await _proc.run(
            ["virsh", "-c", self._libvirt_uri, "snapshot-list", name],
            capture_output=True, text=True, timeout=10
        )
        snapshots = []
        if result.returncode == 0:
            for line in result.stdout.split("\n")[1:]:
                parts = line.split()
                if len(parts) >= 2:
                    snapshots.append(VMSnapshot(name=parts[0], created_at=parts[1]))
        return snapshots

    async def create_snapshot(self, name: str, snapshot_name: str,
                              description: str = "",
                              include_memory: bool = False) -> VMSnapshot:
        args = ["snapshot-create-as", name, snapshot_name]
        if description:
            args.extend(["--description", description])
        if include_memory:
            args.append("--atomic")
        await _proc.run(
            ["virsh", "-c", self._libvirt_uri] + args,
            capture_output=True, text=True, timeout=60
        )
        return VMSnapshot(
            name=snapshot_name,
            description=description,
            created_at=datetime.now().isoformat(),
        )

    async def restore_snapshot(self, name: str, snapshot_name: str) -> None:
        await _proc.run(
            ["virsh", "-c", self._libvirt_uri, "snapshot-revert",
             name, snapshot_name],
            capture_output=True, text=True, timeout=30
        )

    async def delete_snapshot(self, name: str, snapshot_name: str) -> None:
        await _proc.run(
            ["virsh", "-c", self._libvirt_uri, "snapshot-delete",
             name, snapshot_name],
            capture_output=True, text=True, timeout=30
        )

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _build_domain_xml(self, config: VMConfig, disk_path: str) -> str:
        """Build libvirt domain XML from a VMConfig."""
        return f"""<domain type='kvm'>
  <name>{config.name}</name>
  <memory unit='MiB'>{config.ram_mb}</memory>
  <currentMemory unit='MiB'>{config.ram_mb}</currentMemory>
  <vcpu placement='static'>{config.cpus}</vcpu>
  <os>
    <type arch='x86_64' machine='pc-q35-5.2'>hvm</type>
    <boot dev='hd'/>
  </os>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='{disk_path}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <interface type='network'>
      <source network='default'/>
      <model type='virtio'/>
    </interface>
    <graphics type='spice' port='5900' autoport='yes'/>
  </devices>
</domain>"""
