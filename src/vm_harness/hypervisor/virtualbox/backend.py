"""VirtualBox Backend — implements HypervisorBackend using VBoxManage CLI.

Supports VirtualBox on Windows, macOS, and Linux using the VBoxManage
command-line interface.

Config keys:
    vboxmanage_path: Path to VBoxManage (auto-detected if not set)
    vms_dir: Directory for VM inventory (default: ~/.qemu-mcp/vbox-vms)
    default_machine_folder: Default VM storage folder
    default_network: Default network mode (nat, bridged, hostonly, intnet)
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
    DEFAULT_VBOXMANAGE = r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
    DEFAULT_MACHINE_FOLDER = str(Path.home() / "VirtualBox VMs")
elif platform.system() == "Darwin":
    DEFAULT_VBOXMANAGE = "/usr/local/bin/VBoxManage"
    DEFAULT_MACHINE_FOLDER = str(Path.home() / "VirtualBox VMs")
else:  # Linux
    DEFAULT_VBOXMANAGE = "/usr/bin/VBoxManage"
    DEFAULT_MACHINE_FOLDER = str(Path.home() / "VirtualBox VMs")


def find_vboxmanage() -> str | None:
    """Locate VBoxManage: ``$VMH_VBOXMANAGE``, then PATH, then the usual install places.

    Distributions and package managers place VirtualBox in different prefixes
    (``/usr/bin``, ``/opt/VirtualBox``, ``/usr/local/bin``), so a single
    hardcoded absolute path is not portable. PATH is consulted first.
    """
    exe = "VBoxManage.exe" if os.name == "nt" else "VBoxManage"
    candidates = [
        Path(d) / exe
        for d in (
            DEFAULT_VBOXMANAGE,
            r"C:\Program Files\Oracle\VirtualBox",
            "/usr/bin",
            "/usr/local/bin",
            "/opt/VirtualBox",
        )
    ]
    return _proc.find_tool(["VBoxManage"], candidates, env_var="VMH_VBOXMANAGE")


# ── VirtualBoxBackend ──────────────────────────────────────────────────────────

class VirtualBoxBackend(HypervisorBackend):
    """VirtualBox hypervisor backend using VBoxManage CLI.

    This backend manages VirtualBox VMs through the VBoxManage command-line
    tool, supporting all major VM lifecycle operations.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._vboxmanage_path = (
            self._config.get("vboxmanage_path")
            or find_vboxmanage()
            or DEFAULT_VBOXMANAGE
        )
        self._vms_dir = Path(self._config.get("vms_dir", str(Path.home() / ".qemu-mcp" / "vbox-vms")))
        self._vms_dir.mkdir(parents=True, exist_ok=True)
        self._default_machine_folder = self._config.get("default_machine_folder", DEFAULT_MACHINE_FOLDER)
        self._default_network = self._config.get("default_network", "nat")

    @property
    def default_name(self) -> str:
        return "virtualbox"

    @property
    def display_name(self) -> str:
        return "VirtualBox"

    @property
    def version(self) -> str:
        try:
            result = subprocess.run(
                [self._vboxmanage_path, "--version"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                return result.stdout.strip().split()[0]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return "unknown"

    @property
    def is_available(self) -> bool:
        return os.path.isfile(self._vboxmanage_path)

    @property
    def supported_features(self) -> set[str]:
        return {
            "create", "destroy", "start", "stop", "pause", "resume",
            "reset", "reboot", "status", "list", "snapshots",
            "guest_agent", "vnc", "metrics", "exec", "clone",
            "import", "export", "network", "disk_resize", "cdrom",
            "usb_passthrough", "screenshots",
        }

    # ── Initialization ───────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to VirtualBox via VBoxManage."""
        await self.initialize()

    async def initialize(self) -> None:
        """Initialize the VirtualBox backend."""
        if not self.is_available:
            raise BackendNotAvailableError(
                f"VBoxManage not found: {self._vboxmanage_path}"
            )
        await super().initialize()

    async def shutdown(self) -> None:
        """Clean up resources."""
        await super().shutdown()

    # ── Discovery ────────────────────────────────────────────────────────────

    async def list_vms(self) -> list[str]:
        """List all VirtualBox VM names."""
        result = await _proc.run(
            [self._vboxmanage_path, "list", "vms"],
            capture_output=True, text=True, timeout=10
        )
        vms = []
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                match = re.match(r'"(.+)"\s+\{(.+)\}', line.strip())
                if match:
                    # A registration whose files are gone is listed as "<inaccessible>": name it by its UUID, which
                    # VBoxManage accepts anywhere a name goes, so it can still be inspected or unregistered.
                    vms.append(match.group(2) if match.group(1) == "<inaccessible>" else match.group(1))
        return vms

    # ── VM Lifecycle ─────────────────────────────────────────────────────────

    async def create_vm(self, config: VMConfig) -> str:
        """Create a new VirtualBox VM."""
        if await self.find_vm(config.name):
            raise VMAlreadyRunningError(f"VM '{config.name}' already exists")

        # Create VM
        await _proc.run(
            [self._vboxmanage_path, "createvm", "--name", config.name,
             "--ostype", "Ubuntu_64", "--register"],
            check=True, capture_output=True, text=True, timeout=30
        )

        # Configure RAM, CPUs
        modify_args = [
            self._vboxmanage_path, "modifyvm", config.name,
            "--memory", str(config.ram_mb),
            "--cpus", str(config.cpus),
            "--vram", "128",
            "--graphicscontroller", "vmsvga",
            "--audio-driver", "none",
        ]
        if config.enable_nested_virt:
            modify_args.extend(["--nested-hw-virt", "on"])
        await _proc.run(modify_args, check=True, capture_output=True, text=True, timeout=10, creationflags=CREATE_NO_WINDOW)

        # Set firmware
        if config.boot_firmware == "uefi":
            await _proc.run(
                [self._vboxmanage_path, "modifyvm", config.name, "--firmware", "efi"],
                check=True, capture_output=True, text=True, timeout=10
            )

        # Set network
        network_mode = config.network_mode.value if config.network_mode else self._default_network
        await _proc.run(
            [self._vboxmanage_path, "modifyvm", config.name,
             "--nic1", network_mode],
            check=True, capture_output=True, text=True, timeout=10
        )

        # Create storage controller
        await _proc.run(
            [self._vboxmanage_path, "storagectl", config.name, "--name", "SATA",
             "--add", "sata", "--controller", "IntelAhci"],
            check=True, capture_output=True, text=True, timeout=10
        )

        # Create disk
        if config.disk_path:
            disk_path = config.disk_path
            if not os.path.isfile(disk_path) and config.disk_size_gb > 0:
                await _proc.run(
                    [self._vboxmanage_path, "createmedium", "disk",
                     "--filename", disk_path, "--size", str(config.disk_size_gb * 1024),
                     "--format", "VDI"],
                    check=True, capture_output=True, text=True, timeout=60
                )
            await _proc.run(
                [self._vboxmanage_path, "storageattach", config.name,
                 "--storagectl", "SATA", "--port", "0", "--device", "0",
                 "--type", "hdd", "--medium", disk_path],
                check=True, capture_output=True, text=True, timeout=10
            )

        # CD-ROM
        await _proc.run(
            [self._vboxmanage_path, "storagectl", config.name, "--name", "IDE",
             "--add", "ide"],
            check=True, capture_output=True, text=True, timeout=10
        )
        if config.iso_path:
            await _proc.run(
                [self._vboxmanage_path, "storageattach", config.name,
                 "--storagectl", "IDE", "--port", "0", "--device", "0",
                 "--type", "dvddrive", "--medium", config.iso_path],
                check=True, capture_output=True, text=True, timeout=10
            )

        # Save metadata
        vm_config = {
            "name": config.name,
            "description": config.description,
            "disk_path": config.disk_path,
            "ram_mb": config.ram_mb,
            "cpus": config.cpus,
            "network_mode": network_mode,
            "created_at": datetime.now().isoformat(),
        }
        config_path = self._vms_dir / f"{config.name}.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(vm_config, f, indent=2)

        logger.info("Created VirtualBox VM '%s'", config.name)
        return config.name

    async def destroy_vm(self, name: str) -> None:
        """Delete a VirtualBox VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        # Power off if running
        try:
            await _proc.run(
                [self._vboxmanage_path, "controlvm", name, "poweroff"],
                capture_output=True, text=True, timeout=10
            )
        except Exception:
            pass

        await _proc.run(
            [self._vboxmanage_path, "unregistervm", name, "--delete"],
            check=True, capture_output=True, text=True, timeout=30
        )

        # Remove metadata
        config_path = self._vms_dir / f"{name}.json"
        if config_path.exists():
            config_path.unlink()

        logger.info("Destroyed VirtualBox VM '%s'", name)

    async def start_vm(self, name: str, headless: bool = False) -> None:
        """Start a VirtualBox VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        mode = "headless" if headless else "gui"
        await _proc.run(
            [self._vboxmanage_path, "startvm", name, "--type", mode],
            check=True, capture_output=True, text=True, timeout=30
        )
        logger.info("Started VirtualBox VM '%s'", name)

    async def stop_vm(self, name: str, force: bool = False) -> None:
        """Stop a running VirtualBox VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        if force:
            await _proc.run(
                [self._vboxmanage_path, "controlvm", name, "poweroff"],
                check=True, capture_output=True, text=True, timeout=30
            )
        else:
            await _proc.run(
                [self._vboxmanage_path, "controlvm", name, "acpipowerbutton"],
                check=True, capture_output=True, text=True, timeout=30
            )
        logger.info("Stopped VirtualBox VM '%s'", name)

    async def pause_vm(self, name: str) -> None:
        """Pause a running VirtualBox VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")
        await _proc.run(
            [self._vboxmanage_path, "controlvm", name, "pause"],
            check=True, capture_output=True, text=True, timeout=10
        )

    async def resume_vm(self, name: str) -> None:
        """Resume a paused VirtualBox VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")
        await _proc.run(
            [self._vboxmanage_path, "controlvm", name, "resume"],
            check=True, capture_output=True, text=True, timeout=10
        )

    async def reset_vm(self, name: str) -> None:
        """Hard reset a VirtualBox VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")
        await _proc.run(
            [self._vboxmanage_path, "controlvm", name, "reset"],
            check=True, capture_output=True, text=True, timeout=10
        )

    async def reboot_vm(self, name: str, graceful: bool = True) -> None:
        """Reboot a VirtualBox VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")
        if graceful:
            await _proc.run(
                [self._vboxmanage_path, "controlvm", name, "acpipowerbutton"],
                check=True, capture_output=True, text=True, timeout=10
            )
        else:
            await _proc.run(
                [self._vboxmanage_path, "controlvm", name, "reset"],
                check=True, capture_output=True, text=True, timeout=10
            )

    async def shutdown_guest(self, name: str, timeout: int = 30) -> None:
        """Gracefully shut down the guest via ACPI."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")
        await _proc.run(
            [self._vboxmanage_path, "controlvm", name, "acpipowerbutton"],
            check=True, capture_output=True, text=True, timeout=timeout
        )

    # ── Status / Info ────────────────────────────────────────────────────────

    async def get_status(self, name: str) -> VMStatus:
        """Get the status of a VirtualBox VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        result = await _proc.run(
            [self._vboxmanage_path, "showvminfo", name, "--machinereadable"],
            capture_output=True, text=True, timeout=10
        )

        status = VMStatus(name=name, backend_name=self.default_name)
        if result.returncode != 0:
            status.state = VMState.ERROR
            status.last_error = (result.stderr or result.stdout).strip().splitlines()[0][:300] if (result.stderr or result.stdout).strip() else "inaccessible"

        if result.returncode == 0:
            vm_info = self._parse_showvminfo(result.stdout)
            state = vm_info.get("VMState", "unknown")
            state_map = {
                "poweroff": VMState.STOPPED,
                "saved": VMState.SUSPENDED,
                "running": VMState.RUNNING,
                "paused": VMState.PAUSED,
                "aborted": VMState.STOPPED,
                "starting": VMState.STARTING,
                "stopping": VMState.STOPPING,
                "saving": VMState.STOPPING,
                "restoring": VMState.RESTORING,
            }
            status.state = state_map.get(state, VMState.UNKNOWN)
            if vm_info.get("VMStateChangeTime") and status.state == VMState.RUNNING:
                status.started_at = vm_info["VMStateChangeTime"]
            status.ram_allocated_mb = int(vm_info.get("memory", 0))
            status.cpus_allocated = int(vm_info.get("cpus", 0))
            status.pid = int(vm_info.get("VMProcessPID", 0)) if "VMProcessPID" in vm_info else 0

        return status

    async def get_config(self, name: str) -> VMConfig | None:
        """Get the configuration of a VirtualBox VM."""
        if not await self.find_vm(name):
            return None
        result = await _proc.run(
            [self._vboxmanage_path, "showvminfo", name, "--machinereadable"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            info = self._parse_showvminfo(result.stdout)
            return VMConfig(
                name=name,
                ram_mb=int(info.get("memory", 0)),
                cpus=int(info.get("cpus", 0)),
            )
        return None

    # ── Display ──────────────────────────────────────────────────────────────

    async def get_display(self, name: str) -> VMDisplay:
        """Get display connection details for a VirtualBox VM."""
        display = VMDisplay()
        display.display_type = VMDisplayType.VNC
        display.host = "127.0.0.1"
        display.port = 5900

        result = await _proc.run(
            [self._vboxmanage_path, "showvminfo", name, "--machinereadable"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            info = self._parse_showvminfo(result.stdout)
            vrde_port = info.get("VRDEport", "")
            if vrde_port:
                display.port = int(vrde_port)

        display.uri = f"vnc://{display.host}:{display.port}"
        return display

    async def screenshot(self, name: str) -> bytes:
        """Capture a screenshot from a VirtualBox VM."""
        result = await _proc.run(
            [self._vboxmanage_path, "controlvm", name, "screenshotpng", "/tmp/vbox_screenshot.png"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and os.path.isfile("/tmp/vbox_screenshot.png"):
            with open("/tmp/vbox_screenshot.png", "rb") as f:
                return f.read()
        return b""

    # ── Guest Agent / Exec ───────────────────────────────────────────────────

    async def guest_exec(self, name: str, command: str,
                         args: list[str] | None = None,
                         env: dict[str, str] | None = None,
                         timeout: int = 30,
                         capture_output: bool = True) -> dict[str, Any]:
        """Execute a command inside the VirtualBox guest."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        guest_args = [self._vboxmanage_path, "guestcontrol", name, "run",
                      "--exe", command, "--username", "vmuser", "--password", "vmpass",
                      "--wait-stdout", "--wait-stderr"]
        if args:
            guest_args.extend(["--"] + args)

        result = await _proc.run(
            guest_args, capture_output=capture_output, text=True, timeout=timeout
        )
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "timed_out": False,
        }

    async def guest_info(self, name: str) -> VMGuestInfo:
        """Get guest info via VirtualBox guest properties."""
        info = VMGuestInfo()
        result = await _proc.run(
            [self._vboxmanage_path, "guestproperty", "get", name, "/VirtualBox/GuestInfo/OS/Name"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            info.os_name = result.stdout.strip().split(": ")[-1] if ": " in result.stdout else ""
        return info

    async def guest_file_read(self, name: str, path: str,
                              offset: int = 0,
                              max_bytes: int = 65536) -> bytes:
        """Read a file from inside the VirtualBox guest."""
        await _proc.run(
            [self._vboxmanage_path, "guestcontrol", name, "copyfrom",
             "--target-directory", "/tmp", path],
            capture_output=True, text=True, timeout=10
        )
        dest = f"/tmp/{Path(path).name}"
        if os.path.isfile(dest):
            with open(dest, "rb") as f:
                return f.read()
        return b""

    async def guest_file_write(self, name: str, path: str,
                               data: bytes, offset: int = 0) -> None:
        """Write a file to the VirtualBox guest."""
        tmp_path = f"/tmp/vbox_host_file_{Path(path).name}"
        with open(tmp_path, "wb") as f:
            f.write(data)
        await _proc.run(
            [self._vboxmanage_path, "guestcontrol", name, "copyto",
             "--target-directory", path, tmp_path],
            check=True, capture_output=True, text=True, timeout=10
        )

    # ── Snapshots ────────────────────────────────────────────────────────────

    async def list_snapshots(self, name: str) -> list[VMSnapshot]:
        """List all snapshots for a VirtualBox VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        result = await _proc.run(
            [self._vboxmanage_path, "snapshot", name, "list", "--machinereadable"],
            capture_output=True, text=True, timeout=10
        )
        snapshots = []
        if result.returncode == 0:
            info = self._parse_showvminfo(result.stdout)
            # Parse snapshot data from machinereadable output
            for key, value in info.items():
                if key.startswith("SnapshotName"):
                    snap_name = value.strip('"')
                    snapshots.append(VMSnapshot(name=snap_name))
        return snapshots

    async def create_snapshot(self, name: str, snapshot_name: str,
                              description: str = "",
                              include_memory: bool = False) -> VMSnapshot:
        """Create a VirtualBox snapshot."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        await _proc.run(
            [self._vboxmanage_path, "snapshot", name, "take", snapshot_name,
             "--description", description],
            check=True, capture_output=True, text=True, timeout=60
        )
        return VMSnapshot(
            name=snapshot_name,
            description=description,
            created_at=datetime.now().isoformat(),
        )

    async def restore_snapshot(self, name: str, snapshot_name: str) -> None:
        """Restore a VirtualBox snapshot."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")
        await _proc.run(
            [self._vboxmanage_path, "snapshot", name, "restore", snapshot_name],
            check=True, capture_output=True, text=True, timeout=60
        )

    async def delete_snapshot(self, name: str, snapshot_name: str) -> None:
        """Delete a VirtualBox snapshot."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")
        await _proc.run(
            [self._vboxmanage_path, "snapshot", name, "delete", snapshot_name],
            check=True, capture_output=True, text=True, timeout=60
        )

    # ── Disk Operations ─────────────────────────────────────────────────────

    async def resize_disk(self, name: str, new_size_gb: int) -> None:
        """Resize a VirtualBox disk."""
        config = await self._load_vm_config(name)
        disk_path = config.get("disk_path", "")
        if not disk_path:
            raise VMNotFoundError(f"No disk path for VM '{name}'")

        # Must be VDI format for resize
        await _proc.run(
            [self._vboxmanage_path, "modifymedium", "disk", disk_path,
             "--resize", str(new_size_gb * 1024)],
            check=True, capture_output=True, text=True, timeout=60
        )

    async def eject_cdrom(self, name: str) -> None:
        """Eject the virtual CD-ROM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")
        await _proc.run(
            [self._vboxmanage_path, "storageattach", name,
             "--storagectl", "IDE", "--port", "0", "--device", "0",
             "--type", "dvddrive", "--medium", "emptydrive"],
            check=True, capture_output=True, text=True, timeout=10
        )

    async def insert_cdrom(self, name: str, iso_path: str) -> None:
        """Insert an ISO into the virtual CD-ROM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")
        await _proc.run(
            [self._vboxmanage_path, "storageattach", name,
             "--storagectl", "IDE", "--port", "0", "--device", "0",
             "--type", "dvddrive", "--medium", iso_path],
            check=True, capture_output=True, text=True, timeout=10
        )

    # ── Cloning ──────────────────────────────────────────────────────────────

    async def clone_vm(self, name: str, new_name: str,
                       linked: bool = False,
                       snapshots: bool = False) -> str:
        """Clone a VirtualBox VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        mode = "--mode" if linked else "--mode"
        link_arg = "machineandchildren" if linked else "all"
        await _proc.run(
            [self._vboxmanage_path, "clonevm", name, "--name", new_name,
             "--register", mode, link_arg],
            check=True, capture_output=True, text=True, timeout=120
        )

        # Save metadata for clone
        config = await self._load_vm_config(name)
        new_config = dict(config)
        new_config["name"] = new_name
        new_config["created_at"] = datetime.now().isoformat()
        config_path = self._vms_dir / f"{new_name}.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(new_config, f, indent=2)

        return new_name

    # ── Network ──────────────────────────────────────────────────────────────

    async def list_network_interfaces(self, name: str) -> list[VMNetwork]:
        """List network interfaces for a VirtualBox VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        result = await _proc.run(
            [self._vboxmanage_path, "showvminfo", name, "--machinereadable"],
            capture_output=True, text=True, timeout=10
        )
        interfaces = []
        if result.returncode == 0:
            info = self._parse_showvminfo(result.stdout)
            for i in range(1, 5):
                nic_key = f"nic{i}"
                if nic_key in info and info[nic_key] != "none":
                    interfaces.append(VMNetwork(
                        name=f"eth{i-1}",
                        mode=VMNetworkMode(info[nic_key]),
                        connected=info.get(f"cableconnected{i}", "off") == "on",
                    ))
        return interfaces

    # ── USB Passthrough ─────────────────────────────────────────────────────

    async def attach_usb(self, name: str, vendor_id: str,
                         product_id: str) -> None:
        """Attach a USB device to a VirtualBox VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")
        # Create USB filter
        await _proc.run(
            [self._vboxmanage_path, "usbfilter", "add", "0", "--target", name,
             "--name", f"USB-{vendor_id}-{product_id}",
             "--vendorid", vendor_id, "--productid", product_id],
            check=True, capture_output=True, text=True, timeout=10
        )

    async def detach_usb(self, name: str, vendor_id: str,
                         product_id: str) -> None:
        """Detach a USB device from a VirtualBox VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")
        await _proc.run(
            [self._vboxmanage_path, "usbfilter", "remove", "0", "--target", name],
            check=True, capture_output=True, text=True, timeout=10
        )

    # ── Import / Export ─────────────────────────────────────────────────────

    async def export_vm(self, name: str, output_path: str,
                        format: str = "qcow2") -> None:
        """Export a VirtualBox VM as OVA."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")
        await _proc.run(
            [self._vboxmanage_path, "export", name, "--output", output_path],
            check=True, capture_output=True, text=True, timeout=120
        )

    async def import_vm(self, input_path: str,
                        new_name: str | None = None) -> str:
        """Import a VirtualBox VM from OVA."""
        args = [self._vboxmanage_path, "import", input_path]
        if new_name:
            args.extend(["--vsys", "0", "--vmname", new_name])
        result = await _proc.run(
            args, capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise HypervisorError(f"Import failed: {result.stderr}")
        return new_name or "imported-vm"

    # ── Internal helpers ─────────────────────────────────────────────────────

    async def _load_vm_config(self, name: str) -> dict[str, Any]:
        """Load VM config from disk."""
        config_path = self._vms_dir / f"{name}.json"
        if not config_path.exists():
            raise VMNotFoundError(f"VM '{name}' config not found")
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _parse_showvminfo(self, output: str) -> dict[str, str]:
        """Parse VBoxManage showvminfo --machinereadable output."""
        result = {}
        for line in output.split("\n"):
            line = line.strip()
            if "=" in line:
                key, _, value = line.partition("=")
                result[key.strip()] = value.strip().strip('"')
        return result
