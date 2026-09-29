"""WSL Backend — implements HypervisorBackend for Windows Subsystem for Linux.

WSL provides lightweight VM-based Linux environments on Windows. This backend
manages WSL distributions through wsl.exe.

Config keys:
    wsl_path: Path to wsl.exe (default: C:\\Windows\\System32\\wsl.exe)
    default_distro: Default distribution name
    default_user: Default user for exec operations
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

DEFAULT_WSL_PATH = r"C:\Windows\System32\wsl.exe"


# ── WSLBackend ────────────────────────────────────────────────────────────────

class WSLBackend(HypervisorBackend):
    """Windows Subsystem for Linux (WSL) backend.

    Manages WSL distributions through wsl.exe. Each WSL distribution is treated
    as a lightweight VM. Supports WSL2 only (WSL1 lacks proper VM isolation).
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._wsl_path = self._config.get("wsl_path", DEFAULT_WSL_PATH)
        self._default_distro = self._config.get("default_distro", "Ubuntu")
        self._default_user = self._config.get("default_user", "root")
        self._vms_dir = Path(self._config.get("vms_dir", str(Path.home() / ".qemu-mcp" / "wsl-vms")))
        self._vms_dir.mkdir(parents=True, exist_ok=True)

    @property
    def default_name(self) -> str:
        return "wsl"

    @property
    def display_name(self) -> str:
        return "Windows Subsystem for Linux (WSL)"

    @property
    def version(self) -> str:
        try:
            result = subprocess.run(
                [self._wsl_path, "--version"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                return result.stdout.strip().split("\n")[0]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return "unknown"

    @property
    def is_available(self) -> bool:
        return os.name == "nt" and os.path.isfile(self._wsl_path)

    @property
    def supported_features(self) -> set[str]:
        return {
            "create", "destroy", "start", "stop", "status", "list",
            "exec", "export", "import", "console", "guest_agent",
        }

    # ── Initialization ───────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Initialize the WSL backend."""
        if not self.is_available:
            raise BackendNotAvailableError(
                f"wsl.exe not found: {self._wsl_path}"
            )
        # Check WSL version
        result = await _proc.run(
            [self._wsl_path, "--status"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            raise BackendNotAvailableError("WSL is not properly installed")
        await super().initialize()

    async def shutdown(self) -> None:
        """Clean up resources."""
        await super().shutdown()

    # ── Discovery ────────────────────────────────────────────────────────────

    async def list_vms(self) -> list[str]:
        """List all WSL distributions."""
        result = await _proc.run(
            [self._wsl_path, "--list", "--quiet"],
            capture_output=True, text=True, timeout=10
        )
        vms = []
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                name = line.strip()
                if name and not name.startswith("Windows"):
                    vms.append(name)
        return vms

    # ── VM Lifecycle ─────────────────────────────────────────────────────────

    async def create_vm(self, config: VMConfig) -> str:
        """Create a new WSL distribution by importing a rootfs tarball.

        The disk_path field should point to a .tar.gz rootfs tarball.
        """
        if await self.find_vm(config.name):
            raise VMAlreadyRunningError(f"VM '{config.name}' already exists")

        install_path = str(Path(self._default_disk_dir()) / config.name)

        if config.disk_path and os.path.isfile(config.disk_path):
            # Import from tarball
            await _proc.run(
                [self._wsl_path, "--import", config.name, install_path, config.disk_path],
                check=True, capture_output=True, text=True, timeout=120
            )
        else:
            # Install default distribution and rename
            await _proc.run(
                [self._wsl_path, "--install", "-d", self._default_distro, "--no-launch"],
                check=True, capture_output=True, text=True, timeout=300
            )
            # Note: renaming a WSL distro is not directly supported; this is best-effort

        # Save metadata
        vm_config = {
            "name": config.name,
            "description": config.description,
            "install_path": install_path,
            "created_at": datetime.now().isoformat(),
        }
        config_path = self._vms_dir / f"{config.name}.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(vm_config, f, indent=2)

        logger.info("Created WSL distribution '%s'", config.name)
        return config.name

    async def destroy_vm(self, name: str) -> None:
        """Unregister a WSL distribution."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        # Terminate first
        try:
            await _proc.run(
                [self._wsl_path, "--terminate", name],
                capture_output=True, text=True, timeout=10
            )
        except Exception:
            pass

        await _proc.run(
            [self._wsl_path, "--unregister", name],
            check=True, capture_output=True, text=True, timeout=30
        )

        # Remove metadata
        config_path = self._vms_dir / f"{name}.json"
        if config_path.exists():
            config_path.unlink()

        logger.info("Destroyed WSL distribution '%s'", name)

    async def start_vm(self, name: str, headless: bool = False) -> None:
        """Start a WSL distribution (launch a background process)."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        # WSL is always running as a lightweight VM; we just verify it works
        result = await _proc.run(
            [self._wsl_path, "-d", name, "-e", "echo", "hello"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise HypervisorError(f"Failed to start WSL distribution '{name}': {result.stderr}")
        logger.info("Started WSL distribution '%s'", name)

    async def stop_vm(self, name: str, force: bool = False) -> None:
        """Terminate a WSL distribution."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        await _proc.run(
            [self._wsl_path, "--terminate", name],
            check=True, capture_output=True, text=True, timeout=10
        )
        logger.info("Stopped WSL distribution '%s'", name)

    async def pause_vm(self, name: str) -> None:
        """Pause a WSL distribution (not supported)."""
        raise OperationNotSupportedError("WSL does not support pause/resume")

    async def resume_vm(self, name: str) -> None:
        """Resume a WSL distribution (not supported)."""
        raise OperationNotSupportedError("WSL does not support pause/resume")

    async def reset_vm(self, name: str) -> None:
        """Restart a WSL distribution."""
        await self.stop_vm(name, force=True)
        await self.start_vm(name)

    async def reboot_vm(self, name: str, graceful: bool = True) -> None:
        """Reboot a WSL distribution."""
        await self.stop_vm(name, force=not graceful)
        await self.start_vm(name)

    # ── Status / Info ────────────────────────────────────────────────────────

    async def distributions(self) -> dict[str, dict[str, Any]]:
        """name -> {state, version, default} from `wsl -l -v`, which reads state without starting anything."""
        result = await _proc.run([self._wsl_path, "--list", "--verbose"], timeout=15)
        out: dict[str, dict[str, Any]] = {}
        for line in result.stdout.splitlines()[1:]:
            default = line.lstrip().startswith("*")
            parts = line.replace("*", " ", 1).split()
            if len(parts) >= 3:
                out[parts[0]] = {"state": parts[1], "version": int(parts[2]) if parts[2].isdigit() else 0, "default": default}
        return out

    async def get_status(self, name: str) -> VMStatus:
        """Get the status of a WSL distribution. A stopped distribution is not started to answer."""
        distros = await self.distributions()
        if name not in distros:
            raise VMNotFoundError(f"VM '{name}' not found")
        status = VMStatus(name=name, backend_name=self.default_name)
        state = distros[name]["state"].lower()
        status.state = {"running": VMState.RUNNING, "stopped": VMState.STOPPED, "installing": VMState.STARTING,
                        "converting": VMState.STARTING, "uninstalling": VMState.STOPPING}.get(state, VMState.UNKNOWN)
        if status.state == VMState.RUNNING:
            try:
                mem = await _proc.run([self._wsl_path, "-d", name, "-e", "free", "-m"], timeout=10)
                lines = mem.stdout.strip().splitlines()
                if mem.returncode == 0 and len(lines) > 1:
                    parts = lines[1].split()
                    status.ram_allocated_mb = int(parts[1])
                    status.ram_usage_mb = int(parts[2])
            except Exception:
                pass
        return status

    async def get_config(self, name: str) -> VMConfig | None:
        """Get the configuration of a WSL distribution."""
        if not await self.find_vm(name):
            return None
        return VMConfig(name=name)

    # ── Console / Exec ───────────────────────────────────────────────────────

    async def get_console(self, name: str) -> VMConsole:
        """Get console access details for WSL."""
        console = VMConsole()
        console.protocol = "wsl"
        console.command = f"wsl -d {name}"
        return console

    async def guest_exec(self, name: str, command: str,
                         args: list[str] | None = None,
                         env: dict[str, str] | None = None,
                         timeout: int = 30,
                         capture_output: bool = True) -> dict[str, Any]:
        """Execute a command inside the WSL distribution."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        wsl_args = [self._wsl_path, "-d", name, "-e", command]
        if args:
            wsl_args.extend(args)

        result = await _proc.run(
            wsl_args, capture_output=capture_output, text=True, timeout=timeout
        )
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "timed_out": False,
        }

    async def guest_info(self, name: str) -> VMGuestInfo:
        """Get guest OS information from WSL."""
        info = VMGuestInfo()

        # Get hostname
        result = await _proc.run(
            [self._wsl_path, "-d", name, "-e", "hostname"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            info.hostname = result.stdout.strip()

        # Get OS info
        result = await _proc.run(
            [self._wsl_path, "-d", name, "-e", "uname", "-a"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            info.os_name = result.stdout.strip()

        # Get IP
        result = await _proc.run(
            [self._wsl_path, "-d", name, "-e", "hostname", "-I"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            info.ip_addresses = [{"ip": ip, "family": "ipv4"} for ip in result.stdout.strip().split()]

        return info

    async def guest_file_read(self, name: str, path: str,
                              offset: int = 0,
                              max_bytes: int = 65536) -> bytes:
        """Read a file from inside WSL."""
        result = await _proc.run(
            [self._wsl_path, "-d", name, "-e", "cat", path],
            capture_output=True, text=False, timeout=10
        )
        return result.stdout if result.returncode == 0 else b""

    async def guest_file_write(self, name: str, path: str,
                               data: bytes, offset: int = 0) -> None:
        """Write a file to WSL."""
        # Use tee for binary-safe writing
        result = await _proc.run(
            [self._wsl_path, "-d", name, "-e", "tee", path],
            input=data, capture_output=True, timeout=10
        )

    # ── Snapshots ────────────────────────────────────────────────────────────

    async def list_snapshots(self, name: str) -> list[VMSnapshot]:
        """WSL does not support snapshots."""
        return []

    async def create_snapshot(self, name: str, snapshot_name: str,
                              description: str = "",
                              include_memory: bool = False) -> VMSnapshot:
        """WSL does not support snapshots."""
        raise OperationNotSupportedError("WSL does not support snapshots")

    async def restore_snapshot(self, name: str, snapshot_name: str) -> None:
        """WSL does not support snapshots."""
        raise OperationNotSupportedError("WSL does not support snapshots")

    async def delete_snapshot(self, name: str, snapshot_name: str) -> None:
        """WSL does not support snapshots."""
        raise OperationNotSupportedError("WSL does not support snapshots")

    # ── Import / Export ─────────────────────────────────────────────────────

    async def export_vm(self, name: str, output_path: str,
                        format: str = "qcow2") -> None:
        """Export a WSL distribution as a tarball."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        await _proc.run(
            [self._wsl_path, "--export", name, output_path],
            check=True, capture_output=True, text=True, timeout=120
        )

    async def import_vm(self, input_path: str,
                        new_name: str | None = None) -> str:
        """Import a WSL distribution from a tarball."""
        if not new_name:
            new_name = Path(input_path).stem

        install_path = str(Path(self._default_disk_dir()) / new_name)
        await _proc.run(
            [self._wsl_path, "--import", new_name, install_path, input_path],
            check=True, capture_output=True, text=True, timeout=120
        )

        # Save metadata
        vm_config = {
            "name": new_name,
            "install_path": install_path,
            "created_at": datetime.now().isoformat(),
        }
        config_path = self._vms_dir / f"{new_name}.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(vm_config, f, indent=2)

        return new_name

    # ── Metrics ──────────────────────────────────────────────────────────────

    async def get_metrics(self, name: str) -> VMMetrics:
        """Get real-time metrics for WSL."""
        metrics = VMMetrics(timestamp=datetime.now().isoformat())

        # CPU and memory
        result = await _proc.run(
            [self._wsl_path, "-d", name, "-e", "cat", "/proc/meminfo"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            meminfo = {}
            for line in result.stdout.split("\n"):
                if ":" in line:
                    key, val = line.split(":", 1)
                    meminfo[key.strip()] = int(val.split()[0])
            metrics.ram_usage_mb = meminfo.get("MemTotal", 0) // 1024
            metrics.ram_available_mb = meminfo.get("MemAvailable", 0) // 1024

        return metrics

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _default_disk_dir(self) -> str:
        """Get the default disk directory for WSL installs."""
        default = str(Path.home() / "WSL")
        os.makedirs(default, exist_ok=True)
        return default
