"""VMware Backend — implements HypervisorBackend using VMware vmrun CLI.

Supports VMware Workstation (Windows/Linux) and VMware Fusion (macOS).
Uses the ``vmrun`` command-line tool for all VM operations.

Config keys:
    vmrun_path: Path to vmrun executable (auto-detected if not set)
    vms_dir: Directory for VM inventory (default: ~/.qemu-mcp/vmware-vms)
    default_disk_dir: Default directory for new VMDK files
    default_network: Default network mode (nat, bridged, hostonly)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
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

# ── Default paths per platform ─────────────────────────────────────────────────

if platform.system() == "Windows":
    DEFAULT_VMRUN = r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe"
    DEFAULT_VMX_DIR = str(Path.home() / "Documents" / "Virtual Machines")
elif platform.system() == "Darwin":
    DEFAULT_VMRUN = "/Applications/VMware Fusion.app/Contents/Library/vmrun"
    DEFAULT_VMX_DIR = str(Path.home() / "Documents" / "Virtual Machines")
else:  # Linux
    DEFAULT_VMRUN = "/usr/bin/vmrun"
    DEFAULT_VMX_DIR = str(Path.home() / "vmware")


def find_vmrun() -> str | None:
    """Locate vmrun: ``$VMH_VMRUN``, then PATH, then the usual install places.

    VMware ships vmrun inside the application bundle on macOS and under
    several Program Files roots on Windows, so PATH is consulted before the
    hardcoded fallbacks.
    """
    exe = "vmrun.exe" if os.name == "nt" else "vmrun"
    candidates = [
        Path(d) / exe
        for d in (
            DEFAULT_VMRUN,
            r"C:\Program Files (x86)\VMware\VMware Workstation",
            r"C:\Program Files\VMware\VMware Workstation",
            "/Applications/VMware Fusion.app/Contents/Library",
            "/usr/bin",
            "/usr/local/bin",
        )
    ]
    return _proc.find_tool(["vmrun"], candidates, env_var="VMH_VMRUN")


# ── VMwareBackend ─────────────────────────────────────────────────────────────

class VMwareBackend(HypervisorBackend):
    """VMware hypervisor backend using vmrun CLI.

    This backend manages VMware VMs through the vmrun command-line tool,
    which provides full lifecycle control for VMware Workstation and Fusion.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._vmrun_path = (
            self._config.get("vmrun_path")
            or find_vmrun()
            or DEFAULT_VMRUN
        )
        self._vms_dir = Path(self._config.get("vms_dir", str(Path.home() / ".qemu-mcp" / "vmware-vms")))
        self._vms_dir.mkdir(parents=True, exist_ok=True)
        self._default_disk_dir = self._config.get("default_disk_dir", DEFAULT_VMX_DIR)
        self._default_network = self._config.get("default_network", "nat")

    @property
    def default_name(self) -> str:
        return "vmware"

    @property
    def display_name(self) -> str:
        return "VMware Workstation"

    @property
    def version(self) -> str:
        try:
            result = subprocess.run(
                [self._vmrun_path, "list"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                return "vmware-vmrun"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return "unknown"

    @property
    def is_available(self) -> bool:
        return os.path.isfile(self._vmrun_path)

    @property
    def supported_features(self) -> set[str]:
        return {
            "create", "destroy", "start", "stop", "pause", "resume",
            "reset", "reboot", "status", "list", "snapshots",
            "guest_agent", "rdp", "metrics", "exec", "clone",
            "import", "export", "network",
        }

    # ── Initialization ───────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to VMware Workstation/Fusion."""
        await self.initialize()

    async def initialize(self) -> None:
        """Initialize the VMware backend."""
        if not self.is_available:
            raise BackendNotAvailableError(
                f"vmrun not found: {self._vmrun_path}"
            )
        await super().initialize()

    async def shutdown(self) -> None:
        """Clean up resources."""
        await super().shutdown()

    # ── Discovery ────────────────────────────────────────────────────────────

    async def list_vms(self) -> list[str]:
        """List all VM names from the inventory."""
        vms = []
        for config_file in sorted(self._vms_dir.glob("*.json")):
            vms.append(config_file.stem)
        return vms

    # ── VM Lifecycle ─────────────────────────────────────────────────────────

    async def create_vm(self, config: VMConfig) -> str:
        """Create a new VMware VM."""
        if await self.find_vm(config.name):
            raise VMAlreadyRunningError(f"VM '{config.name}' already exists")

        vmx_dir = Path(self._default_disk_dir) / config.name
        vmx_dir.mkdir(parents=True, exist_ok=True)
        vmx_path = str(vmx_dir / f"{config.name}.vmx")
        vmdk_path = str(vmx_dir / f"{config.name}.vmdk")

        # Create VMDK disk
        if config.disk_size_gb > 0:
            await self._create_vmdk(vmdk_path, config.disk_size_gb)

        # Build VMX file
        vmx_content = self._build_vmx(config, vmdk_path)
        with open(vmx_path, "w", encoding="utf-8") as f:
            f.write(vmx_content)

        # Save metadata
        vm_config = {
            "name": config.name,
            "description": config.description,
            "vmx_path": vmx_path,
            "vmdk_path": vmdk_path,
            "ram_mb": config.ram_mb,
            "cpus": config.cpus,
            "network_mode": config.network_mode.value,
            "created_at": datetime.now().isoformat(),
        }
        config_path = self._vms_dir / f"{config.name}.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(vm_config, f, indent=2)

        logger.info("Created VMware VM '%s' at %s", config.name, vmx_path)
        return config.name

    async def destroy_vm(self, name: str) -> None:
        """Delete a VMware VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        config = await self._load_vm_config(name)
        vmx_path = config.get("vmx_path", "")

        # Stop if running
        try:
            await self.stop_vm(name, force=True)
        except Exception:
            pass

        # Remove VM files
        if vmx_path:
            vmx_dir = Path(vmx_path).parent
            if vmx_dir.exists():
                shutil.rmtree(vmx_dir, ignore_errors=True)

        # Remove metadata
        config_path = self._vms_dir / f"{name}.json"
        if config_path.exists():
            config_path.unlink()

        logger.info("Destroyed VMware VM '%s'", name)

    async def start_vm(self, name: str, headless: bool = False) -> None:
        """Start a VMware VM."""
        config = await self._require_vm(name)
        vmx_path = config["vmx_path"]

        if headless:
            await _proc.run(
                [self._vmrun_path, "start", vmx_path, "nogui"],
                check=True, capture_output=True, text=True, timeout=30
            )
        else:
            await _proc.run(
                [self._vmrun_path, "start", vmx_path],
                check=True, capture_output=True, text=True, timeout=30
            )
        logger.info("Started VMware VM '%s'", name)

    async def stop_vm(self, name: str, force: bool = False) -> None:
        """Stop a running VMware VM."""
        config = await self._require_vm(name)
        vmx_path = config["vmx_path"]

        mode = "hard" if force else "soft"
        await _proc.run(
            [self._vmrun_path, "stop", vmx_path, mode],
            check=True, capture_output=True, text=True, timeout=30
        )
        logger.info("Stopped VMware VM '%s'", name)

    async def pause_vm(self, name: str) -> None:
        """Pause a running VMware VM."""
        config = await self._require_vm(name)
        vmx_path = config["vmx_path"]
        await _proc.run(
            [self._vmrun_path, "pause", vmx_path],
            check=True, capture_output=True, text=True, timeout=30
        )

    async def resume_vm(self, name: str) -> None:
        """Resume a paused VMware VM."""
        config = await self._require_vm(name)
        vmx_path = config["vmx_path"]
        await _proc.run(
            [self._vmrun_path, "unpause", vmx_path],
            check=True, capture_output=True, text=True, timeout=30
        )

    async def reset_vm(self, name: str) -> None:
        """Hard reset a VMware VM."""
        config = await self._require_vm(name)
        vmx_path = config["vmx_path"]
        await _proc.run(
            [self._vmrun_path, "reset", vmx_path, "hard"],
            check=True, capture_output=True, text=True, timeout=30
        )

    async def reboot_vm(self, name: str, graceful: bool = True) -> None:
        """Reboot a VMware VM."""
        config = await self._require_vm(name)
        vmx_path = config["vmx_path"]
        mode = "soft" if graceful else "hard"
        await _proc.run(
            [self._vmrun_path, "reset", vmx_path, mode],
            check=True, capture_output=True, text=True, timeout=30
        )

    async def shutdown_guest(self, name: str, timeout: int = 30) -> None:
        """Gracefully shut down the guest via VMware Tools."""
        config = await self._require_vm(name)
        vmx_path = config["vmx_path"]
        await _proc.run(
            [self._vmrun_path, "stop", vmx_path, "soft"],
            check=True, capture_output=True, text=True, timeout=timeout
        )

    # ── Status / Info ────────────────────────────────────────────────────────

    async def get_status(self, name: str) -> VMStatus:
        """Get the status of a VMware VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        config = await self._load_vm_config(name)
        status = VMStatus(name=name, backend_name=self.default_name)

        # Check if running via vmrun list
        running = await self._get_running_vmx_paths()
        vmx_path = config.get("vmx_path", "")

        if vmx_path in running:
            status.state = VMState.RUNNING
        else:
            status.state = VMState.STOPPED

        status.ram_allocated_mb = config.get("ram_mb", 0)
        status.cpus_allocated = config.get("cpus", 0)
        return status

    async def get_config(self, name: str) -> VMConfig | None:
        """Get the configuration of a VMware VM."""
        if not await self.find_vm(name):
            return None
        return await self._load_vm_config(name)

    # ── Display ──────────────────────────────────────────────────────────────

    async def get_display(self, name: str) -> VMDisplay:
        """Get display connection details."""
        config = await self._require_vm(name)
        display = VMDisplay()
        display.display_type = VMDisplayType.RDP
        display.host = "127.0.0.1"
        display.port = 3389
        display.uri = "vmware-vmx"
        return display

    # ── Guest Agent / Exec ───────────────────────────────────────────────────

    async def guest_exec(self, name: str, command: str,
                         args: list[str] | None = None,
                         env: dict[str, str] | None = None,
                         timeout: int = 30,
                         capture_output: bool = True) -> dict[str, Any]:
        """Execute a command inside the VMware guest via VMware Tools."""
        config = await self._require_vm(name)
        vmx_path = config["vmx_path"]

        cmd_args = [command] + (args or [])
        result = await _proc.run(
            [self._vmrun_path, "runProgramInGuest", vmx_path,
             "-noWait", "-activeWindow", "-interactive"] + cmd_args,
            capture_output=capture_output, text=True, timeout=timeout
        )
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "timed_out": False,
        }

    async def guest_info(self, name: str) -> VMGuestInfo:
        """Get guest info via VMware Tools."""
        config = await self._require_vm(name)
        vmx_path = config["vmx_path"]
        result = await _proc.run(
            [self._vmrun_path, "getGuestIPAddress", vmx_path],
            capture_output=True, text=True, timeout=10
        )
        info = VMGuestInfo()
        info.ip_addresses = [{"ip": result.stdout.strip(), "family": "ipv4"}] if result.returncode == 0 else []
        return info

    async def guest_file_read(self, name: str, path: str,
                              offset: int = 0,
                              max_bytes: int = 65536) -> bytes:
        """Read a file from inside the VMware guest."""
        config = await self._require_vm(name)
        vmx_path = config["vmx_path"]
        result = await _proc.run(
            [self._vmrun_path, "CopyFileFromGuestToHost", vmx_path, path, "/tmp/vmware_guest_file"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            with open("/tmp/vmware_guest_file", "rb") as f:
                return f.read()
        return b""

    async def guest_file_write(self, name: str, path: str,
                               data: bytes, offset: int = 0) -> None:
        """Write a file to the VMware guest."""
        config = await self._require_vm(name)
        vmx_path = config["vmx_path"]
        # Write to temp file then copy
        tmp_path = f"/tmp/vmware_host_file_{Path(path).name}"
        with open(tmp_path, "wb") as f:
            f.write(data)
        await _proc.run(
            [self._vmrun_path, "CopyFileFromHostToGuest", vmx_path, tmp_path, path],
            check=True, capture_output=True, text=True, timeout=10
        )

    # ── Snapshots ────────────────────────────────────────────────────────────

    async def list_snapshots(self, name: str) -> list[VMSnapshot]:
        """List all snapshots for a VMware VM."""
        config = await self._require_vm(name)
        vmx_path = config["vmx_path"]
        result = await _proc.run(
            [self._vmrun_path, "listSnapshots", vmx_path],
            capture_output=True, text=True, timeout=10
        )
        snapshots = []
        if result.returncode == 0:
            for line in result.stdout.split("\n")[1:]:  # Skip header
                snap_name = line.strip()
                if snap_name:
                    snapshots.append(VMSnapshot(name=snap_name))
        return snapshots

    async def create_snapshot(self, name: str, snapshot_name: str,
                              description: str = "",
                              include_memory: bool = False) -> VMSnapshot:
        """Create a VMware snapshot."""
        config = await self._require_vm(name)
        vmx_path = config["vmx_path"]
        await _proc.run(
            [self._vmrun_path, "snapshot", vmx_path, snapshot_name],
            check=True, capture_output=True, text=True, timeout=30
        )
        return VMSnapshot(
            name=snapshot_name,
            description=description,
            created_at=datetime.now().isoformat(),
        )

    async def restore_snapshot(self, name: str, snapshot_name: str) -> None:
        """Restore a VMware snapshot."""
        config = await self._require_vm(name)
        vmx_path = config["vmx_path"]
        await _proc.run(
            [self._vmrun_path, "revertToSnapshot", vmx_path, snapshot_name],
            check=True, capture_output=True, text=True, timeout=30
        )

    async def delete_snapshot(self, name: str, snapshot_name: str) -> None:
        """Delete a VMware snapshot."""
        config = await self._require_vm(name)
        vmx_path = config["vmx_path"]
        await _proc.run(
            [self._vmrun_path, "deleteSnapshot", vmx_path, snapshot_name],
            check=True, capture_output=True, text=True, timeout=30
        )

    # ── Cloning ──────────────────────────────────────────────────────────────

    async def clone_vm(self, name: str, new_name: str,
                       linked: bool = False,
                       snapshots: bool = False) -> str:
        """Clone a VMware VM."""
        config = await self._require_vm(name)
        vmx_path = config["vmx_path"]
        new_dir = Path(self._default_disk_dir) / new_name
        new_dir.mkdir(parents=True, exist_ok=True)
        new_vmx = str(new_dir / f"{new_name}.vmx")

        clone_mode = "linked" if linked else "full"
        await _proc.run(
            [self._vmrun_path, "clone", vmx_path, new_vmx, clone_mode],
            check=True, capture_output=True, text=True, timeout=120
        )

        # Save metadata for clone
        new_config = dict(config)
        new_config["name"] = new_name
        new_config["vmx_path"] = new_vmx
        new_config["created_at"] = datetime.now().isoformat()
        config_path = self._vms_dir / f"{new_name}.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(new_config, f, indent=2)

        return new_name

    # ── Network ──────────────────────────────────────────────────────────────

    async def list_network_interfaces(self, name: str) -> list[VMNetwork]:
        """List network interfaces for a VMware VM."""
        config = await self._require_vm(name)
        return [VMNetwork(
            name="ethernet0",
            mode=VMNetworkMode(config.get("network_mode", "nat")),
            connected=True,
            adapter_type="e1000e",
        )]

    # ── Internal helpers ─────────────────────────────────────────────────────

    async def _load_vm_config(self, name: str) -> dict[str, Any]:
        """Load VM config from disk."""
        config_path = self._vms_dir / f"{name}.json"
        if not config_path.exists():
            raise VMNotFoundError(f"VM '{name}' config not found")
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    async def _require_vm(self, name: str) -> dict[str, Any]:
        """Load VM config, raising if not found."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")
        return await self._load_vm_config(name)

    async def _get_running_vmx_paths(self) -> set[str]:
        """Get the set of currently running VMX paths."""
        result = await _proc.run(
            [self._vmrun_path, "list"],
            capture_output=True, text=True, timeout=10
        )
        paths = set()
        if result.returncode == 0:
            for line in result.stdout.split("\n")[1:]:
                line = line.strip()
                if line and line.endswith(".vmx"):
                    paths.add(line)
        return paths

    async def _create_vmdk(self, path: str, size_gb: int) -> None:
        """Create a VMDK disk using vmware-vdiskmanager or qemu-img fallback."""
        vdisk_manager = str(Path(self._vmrun_path).parent / "vmware-vdiskmanager.exe")
        if os.path.isfile(vdisk_manager):
            await _proc.run(
                [vdisk_manager, "-c", "-s", f"{size_gb}GB", "-a", "lsilogic",
                 "-t", "0", path],
                check=True, capture_output=True, text=True, timeout=60
            )
        else:
            # Fallback: use qemu-img
            try:
                await _proc.run(
                    ["qemu-img", "create", "-f", "vmdk", path, f"{size_gb}G"],
                    check=True, capture_output=True, text=True, timeout=30
                )
            except (FileNotFoundError, subprocess.CalledProcessError):
                # Last resort: sparse file
                with open(path, "wb") as f:
                    f.seek(size_gb * 1024 * 1024 * 1024 - 1)
                    f.write(b"\0")

    def _build_vmx(self, config: VMConfig, vmdk_path: str) -> str:
        """Build VMX file content."""
        network_mode = config.network_mode.value if config.network_mode else self._default_network
        mac = config.mac_address or "00:0c:29:xx:xx:xx"

        return f""".encoding = "UTF-8"
displayName = "{config.name}"
guestOS = "ubuntu-64"
memsize = "{config.ram_mb}"
numvcpus = "{config.cpus}"

scsi0.virtualDev = "lsilogic"
scsi0:0.fileName = "{Path(vmdk_path).name}"
scsi0:0.present = "TRUE"

ethernet0.virtualDir = "vmxnet3"
ethernet0.addressType = "static"
ethernet0.address = "{mac}"
ethernet0.present = "TRUE"
ethernet0.connectionType = "{network_mode}"

pciBridge0.present = "TRUE"
pciBridge4.present = "TRUE"
pciBridge4.virtualDev = "pcieRootPort"
pciBridge4.functions = "8"

tools.syncTime = "TRUE"
tools.upgrade.policy = "manual"

firmware = "{'efi' if config.boot_firmware == 'uefi' else 'bios'}"

priority.grabbed = "normal"
priority.ungrabbed = "normal"
powerType.powerOff = "soft"
powerType.powerOn = "soft"
powerType.suspend = "soft"
powerType.reset = "soft"
"""
