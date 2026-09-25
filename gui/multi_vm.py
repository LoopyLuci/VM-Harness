"""Multi-VM Orchestration — manage multiple QEMU VMs with resource tracking.

This module provides:
- VMConfig: Configuration for a single VM (name, disk path, QMP port, SSH port, RAM, CPUs, status)
- MultiVMManager: Orchestrates multiple VMs with resource allocation limits, status tracking,
  and QMP port auto-allocation.
- ResourceLimits: Defines per-VM and global resource constraints.
- VMSummary: Snapshot of a VM's current state for dashboard display.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
CREATE_NO_WINDOW = 0x08000000
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

VM_CONFIGS_DIR = Path.home() / ".qemu-mcp" / "vm-configs"
VM_CONFIGS_DIR.mkdir(parents=True, exist_ok=True)

# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_QEMU_BINARY = r"C:\Program Files\qemu\qemu-system-x86_64.exe"
DEFAULT_RAM_MB = 4096
DEFAULT_CPUS = 2
DEFAULT_QMP_PORT_BASE = 4444
DEFAULT_SSH_PORT_BASE = 2222
DEFAULT_DISK_FORMAT = "qcow2"

# Global resource limits — apply across ALL VMs
GLOBAL_MAX_RAM_MB = 65536      # 64 GB total across all VMs
GLOBAL_MAX_CPUS = 32           # 32 vCPUs total
GLOBAL_MAX_VMS = 16            # Maximum number of managed VMs


@dataclass
class ResourceLimits:
    """Resource allocation limits for a single VM."""
    max_ram_mb: int = DEFAULT_RAM_MB
    max_cpus: int = DEFAULT_CPUS
    max_disk_gb: int = 256
    priority: int = 5            # 1 (highest) – 10 (lowest)


@dataclass
class VMSummary:
    """At-a-glance summary of a VM's state for dashboard display."""
    name: str
    status: str = "stopped"     # stopped | running | paused | suspended | error | starting | stopping
    pid: int | None = None
    cpus: int = 0
    ram_mb: int = 0
    disk_path: str = ""
    disk_used_gb: float = 0.0
    disk_total_gb: float = 0.0
    qmp_port: int = 0
    ssh_port: int = 0
    uptime_seconds: int = 0
    ip_address: str = ""
    cpu_usage: float = 0.0
    ram_usage_mb: int = 0
    last_error: str = ""
    started_at: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "pid": self.pid,
            "cpus": self.cpus,
            "ram_mb": self.ram_mb,
            "disk_path": self.disk_path,
            "disk_used_gb": self.disk_used_gb,
            "disk_total_gb": self.disk_total_gb,
            "qmp_port": self.qmp_port,
            "ssh_port": self.ssh_port,
            "uptime_seconds": self.uptime_seconds,
            "ip_address": self.ip_address,
            "cpu_usage": self.cpu_usage,
            "ram_usage_mb": self.ram_usage_mb,
            "last_error": self.last_error,
            "started_at": self.started_at,
        }


class VMConfig:
    """Full configuration for a single VM instance.

    Tracks name, disk path, QMP port, SSH port, RAM, CPUs, status,
    resource limits, and runtime metadata.
    """

    def __init__(self, name: str, config: dict[str, Any] | None = None):
        self.name = name
        cfg = config or {}

        # Core identifiers
        self.vm_name: str = cfg.get("vm_name", name)
        self.description: str = cfg.get("description", "")
        self.notes: str = cfg.get("notes", "")

        # QEMU paths
        self.qemu_binary: str = cfg.get("qemu_binary", DEFAULT_QEMU_BINARY)
        self.disk_path: str = cfg.get("disk_path", "")
        self.iso_path: str | None = cfg.get("iso_path", None)
        self.additional_drives: list[str] = cfg.get("additional_drives", [])

        # Resource allocation
        self.ram_mb: int = int(cfg.get("ram_mb", DEFAULT_RAM_MB))
        self.cpus: int = int(cfg.get("cpus", DEFAULT_CPUS))
        self.resource_limits = ResourceLimits(
            max_ram_mb=int(cfg.get("max_ram_mb", self.ram_mb)),
            max_cpus=int(cfg.get("max_cpus", self.cpus)),
            max_disk_gb=int(cfg.get("max_disk_gb", 256)),
            priority=int(cfg.get("priority", 5)),
        )

        # Network / management ports
        self.qmp_port: int = int(cfg.get("qmp_port", 0))
        self.ssh_port: int = int(cfg.get("ssh_port", 0))
        self.qmp_host: str = cfg.get("qmp_host", "127.0.0.1")
        self.ssh_host: str = cfg.get("ssh_host", "127.0.0.1")
        self.ssh_username: str = cfg.get("ssh_username", "vmuser")

        # Display
        self.display: str = cfg.get("display", "sdl")
        self.enable_gl: bool = cfg.get("enable_gl", True)
        self.vga: str = cfg.get("vga", "virtio")

        # Boot options
        self.boot_order: str = cfg.get("boot_order", "cd")  # c=hd, d=cdrom, n=network
        self.auto_eject_iso: bool = cfg.get("auto_eject_iso", True)

        # Status (runtime)
        self.status: str = cfg.get("status", "stopped")
        self.pid: int | None = cfg.get("pid", None)
        self.started_at: str = cfg.get("started_at", "")
        self.uptime_seconds: int = int(cfg.get("uptime_seconds", 0))
        self.last_error: str = cfg.get("last_error", "")
        self.cpu_usage: float = float(cfg.get("cpu_usage", 0.0))
        self.ram_usage_mb: int = int(cfg.get("ram_usage_mb", 0))

        # QMP settings
        self.qmp_password: str | None = cfg.get("qmp_password", None)
        self.qmp_socket_path: str | None = cfg.get("qmp_socket_path", None)

        # Additional QEMU args
        self.extra_args: list[str] = cfg.get("extra_args", [])

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> VMConfig:
        """Create a VMConfig from a deserialized JSON dict."""
        return cls(name, data)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSON persistence."""
        return {
            "vm_name": self.vm_name,
            "description": self.description,
            "notes": self.notes,
            "qemu_binary": self.qemu_binary,
            "disk_path": self.disk_path,
            "iso_path": self.iso_path,
            "additional_drives": self.additional_drives,
            "ram_mb": self.ram_mb,
            "cpus": self.cpus,
            "max_ram_mb": self.resource_limits.max_ram_mb,
            "max_cpus": self.resource_limits.max_cpus,
            "max_disk_gb": self.resource_limits.max_disk_gb,
            "priority": self.resource_limits.priority,
            "qmp_port": self.qmp_port,
            "ssh_port": self.ssh_port,
            "qmp_host": self.qmp_host,
            "ssh_host": self.ssh_host,
            "ssh_username": self.ssh_username,
            "display": self.display,
            "enable_gl": self.enable_gl,
            "vga": self.vga,
            "boot_order": self.boot_order,
            "auto_eject_iso": self.auto_eject_iso,
            "status": self.status,
            "pid": self.pid,
            "started_at": self.started_at,
            "uptime_seconds": self.uptime_seconds,
            "last_error": self.last_error,
            "cpu_usage": self.cpu_usage,
            "ram_usage_mb": self.ram_usage_mb,
            "qmp_password": self.qmp_password,
            "qmp_socket_path": self.qmp_socket_path,
            "extra_args": self.extra_args,
        }

    def get_summary(self) -> VMSummary:
        """Produce a VMSummary snapshot for dashboard display."""
        disk_used = 0.0
        disk_total = 0.0
        if self.disk_path and os.path.exists(self.disk_path):
            try:
                size = os.path.getsize(self.disk_path)
                disk_total = size / (1024 ** 3)
                disk_used = disk_total
            except OSError:
                pass

        return VMSummary(
            name=self.name,
            status=self.status,
            pid=self.pid,
            cpus=self.cpus,
            ram_mb=self.ram_mb,
            disk_path=self.disk_path,
            disk_used_gb=round(disk_used, 2),
            disk_total_gb=round(disk_total, 2),
            qmp_port=self.qmp_port,
            ssh_port=self.ssh_port,
            uptime_seconds=self.uptime_seconds,
            cpu_usage=self.cpu_usage,
            ram_usage_mb=self.ram_usage_mb,
            last_error=self.last_error,
            started_at=self.started_at,
            notes=self.notes,
        )


class MultiVMManager:
    """Orchestrate multiple QEMU VM configurations and runtime instances.

    Features:
    - Add/remove VM configurations
    - Start/stop VM processes (QEMU subprocesses)
    - Track per-VM status (running, stopped, paused, error)
    - Resource allocation limits (per-VM and global caps)
    - QMP/SSH port auto-allocation to avoid conflicts
    - Dashboard summary for all VMs at a glance
    - Status polling for runtime metrics
    """

    def __init__(self):
        self._configs: dict[str, VMConfig] = {}
        self._running: dict[str, subprocess.Popen] = {}
        self._next_qmp_port: int = DEFAULT_QMP_PORT_BASE
        self._next_ssh_port: int = DEFAULT_SSH_PORT_BASE
        self._load_configs()

    # ── Config persistence ─────────────────────────────────────────────────────

    def _load_configs(self):
        """Load all VM configs from disk."""
        for config_file in sorted(VM_CONFIGS_DIR.glob("*.json")):
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                name = config_file.stem
                self._configs[name] = VMConfig.from_dict(name, data)
            except Exception as e:
                # Log but don't crash on corrupt configs
                print(f"[MultiVM] Warning: failed to load {config_file}: {e}")

    def _save_config(self, name: str):
        """Persist a VM config to disk."""
        if name not in self._configs:
            return
        config_file = VM_CONFIGS_DIR / f"{name}.json"
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(self._configs[name].to_dict(), f, indent=2)

    def _delete_config_file(self, name: str):
        """Remove config file from disk."""
        config_file = VM_CONFIGS_DIR / f"{name}.json"
        if config_file.exists():
            config_file.unlink()

    # ── Port allocation ───────────────────────────────────────────────────────

    def _is_port_in_use(self, port: int) -> bool:
        """Check if a TCP port is currently in use on localhost."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            result = s.connect_ex(("127.0.0.1", port))
            return result == 0

    def _allocate_qmp_port(self) -> int:
        """Find the next available QMP port."""
        port = self._next_qmp_port
        while self._is_port_in_use(port):
            port += 1
            if port > 65000:
                raise RuntimeError("No available QMP ports in range")
        self._next_qmp_port = port + 1
        return port

    def _allocate_ssh_port(self) -> int:
        """Find the next available SSH forwarded port."""
        port = self._next_ssh_port
        while self._is_port_in_use(port):
            port += 1
            if port > 65000:
                raise RuntimeError("No available SSH ports in range")
        self._next_ssh_port = port + 1
        return port

    # ── CRUD operations ───────────────────────────────────────────────────────

    def add_vm(
        self,
        name: str,
        config: dict[str, Any],
        auto_allocate_ports: bool = True,
    ) -> tuple[bool, str]:
        """Add a new VM configuration.

        Args:
            name: Unique identifier for the VM.
            config: Dict with VM settings (disk_path, ram_mb, cpus, etc.)
            auto_allocate_ports: If True, auto-assign QMP and SSH ports.

        Returns:
            (success, message) tuple.
        """
        if not name or not name.strip():
            return False, "VM name cannot be empty"

        name = name.strip()
        if not re.match(r"^[a-zA-Z0-9_\-]+$", name):
            return False, "VM name must contain only letters, numbers, hyphens, underscores"

        if name in self._configs:
            return False, f"VM '{name}' already exists"

        if len(self._configs) >= GLOBAL_MAX_VMS:
            return False, f"Maximum number of VMs ({GLOBAL_MAX_VMS}) reached"

        # Check global resource limits
        total_ram = sum(v.ram_mb for v in self._configs.values()) + int(config.get("ram_mb", DEFAULT_RAM_MB))
        if total_ram > GLOBAL_MAX_RAM_MB:
            return False, f"Adding this VM would exceed global RAM limit ({GLOBAL_MAX_RAM_MB} MB)"

        total_cpus = sum(v.cpus for v in self._configs.values()) + int(config.get("cpus", DEFAULT_CPUS))
        if total_cpus > GLOBAL_MAX_CPUS:
            return False, f"Adding this VM would exceed global CPU limit ({GLOBAL_MAX_CPUS})"

        # Create config
        vm_config = VMConfig(name, config)

        # Allocate ports
        if auto_allocate_ports:
            if vm_config.qmp_port == 0:
                try:
                    vm_config.qmp_port = self._allocate_qmp_port()
                except RuntimeError as e:
                    return False, str(e)
            if vm_config.ssh_port == 0:
                try:
                    vm_config.ssh_port = self._allocate_ssh_port()
                except RuntimeError as e:
                    return False, str(e)

        # Validate disk path
        if vm_config.disk_path and not os.path.exists(vm_config.disk_path):
            return False, f"Disk path does not exist: {vm_config.disk_path}"

        self._configs[name] = vm_config
        self._save_config(name)
        return True, f"VM '{name}' added successfully"

    def remove_vm(self, name: str, force: bool = False) -> tuple[bool, str]:
        """Remove a VM configuration and stop it if running.

        Args:
            name: VM name to remove.
            force: If True, kill the VM if it's running instead of graceful stop.

        Returns:
            (success, message) tuple.
        """
        if name not in self._configs:
            return False, f"VM '{name}' not found"

        if name in self._running:
            if not force:
                return False, f"VM '{name}' is running. Stop it first or use force=True."
            self.stop_vm(name)

        del self._configs[name]
        self._delete_config_file(name)
        return True, f"VM '{name}' removed"

    def update_vm(self, name: str, updates: dict[str, Any]) -> tuple[bool, str]:
        """Update a VM configuration.

        If the VM is running, only certain fields can be changed (marked as 'hot').
        Returns (success, message).
        """
        if name not in self._configs:
            return False, f"VM '{name}' not found"

        vm = self._configs[name]
        running = name in self._running

        # Fields that can be changed while running
        hot_fields = {"notes", "description", "cpu_usage", "ram_usage_mb",
                      "status", "uptime_seconds", "pid", "started_at", "last_error"}

        # Fields that map to ResourceLimits
        limit_fields = {"max_ram_mb", "max_cpus", "max_disk_gb", "priority"}

        for key, value in updates.items():
            if running and key not in hot_fields and key not in limit_fields:
                continue
            if hasattr(vm, key):
                setattr(vm, key, value)
            # Also update ResourceLimits when limit fields are set
            if key in limit_fields and hasattr(vm.resource_limits, key):
                setattr(vm.resource_limits, key, value)

        self._save_config(name)
        return True, f"VM '{name}' updated"

    def get_vm(self, name: str) -> VMConfig | None:
        """Get a VM configuration by name."""
        return self._configs.get(name)

    def list_vms(self) -> list[str]:
        """List all VM names, sorted alphabetically."""
        return sorted(self._configs.keys())

    def list_running_vms(self) -> list[str]:
        """List names of currently running VMs."""
        return [name for name in self._configs if name in self._running]

    def get_config(self, name: str) -> dict[str, Any] | None:
        """Get a VM config as a plain dict."""
        vm = self._configs.get(name)
        return vm.to_dict() if vm else None

    # ── VM lifecycle ─────────────────────────────────────────────────────────

    def start_vm(self, name: str) -> tuple[bool, str]:
        """Start a VM by launching QEMU subprocess.

        Args:
            name: VM name to start.

        Returns:
            (success, message) tuple.
        """
        config = self._configs.get(name)
        if not config:
            return False, f"VM '{name}' not found"

        if name in self._running and self._running[name].poll() is None:
            return False, f"VM '{name}' is already running"

        args = self._build_qemu_args(config)
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self._running[name] = proc
            config.status = "running"
            config.pid = proc.pid
            config.started_at = datetime.now().isoformat()
            config.last_error = ""
            self._save_config(name)
            return True, f"VM '{name}' started (PID: {proc.pid})"
        except Exception as e:
            config.status = "error"
            config.last_error = str(e)
            self._save_config(name)
            return False, f"Failed to start VM '{name}': {e}"

    def stop_vm(self, name: str, graceful: bool = True) -> tuple[bool, str]:
        """Stop a running VM.

        Args:
            name: VM name to stop.
            graceful: If True, try graceful termination first.

        Returns:
            (success, message) tuple.
        """
        if name not in self._running:
            # Check if it's marked running but no process
            if name in self._configs:
                self._configs[name].status = "stopped"
                self._configs[name].pid = None
                self._save_config(name)
            return False, f"VM '{name}' is not running"

        proc = self._running[name]
        if graceful:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        else:
            proc.kill()
            proc.wait(timeout=5)

        del self._running[name]
        config = self._configs.get(name)
        if config:
            config.status = "stopped"
            config.pid = None
            config.uptime_seconds = 0
            self._save_config(name)

        return True, f"VM '{name}' stopped"

    def pause_vm(self, name: str) -> tuple[bool, str]:
        """Pause a running VM (suspend CPU)."""
        if not self.is_running(name):
            return False, f"VM '{name}' is not running"
        config = self._configs.get(name)
        if config:
            config.status = "paused"
            self._save_config(name)
        return True, f"VM '{name}' paused (use QMP stop command)"

    def resume_vm(self, name: str) -> tuple[bool, str]:
        """Resume a paused VM."""
        config = self._configs.get(name)
        if not config:
            return False, f"VM '{name}' not found"
        if config.status != "paused":
            return False, f"VM '{name}' is not paused (current: {config.status})"
        config.status = "running"
        self._save_config(name)
        return True, f"VM '{name}' resumed (use QMP cont command)"

    def reset_vm(self, name: str) -> tuple[bool, str]:
        """Reset (warm reboot) a running VM."""
        if not self.is_running(name):
            return False, f"VM '{name}' is not running"
        return True, f"VM '{name}' reset (use QMP system_reset)"

    # ── Status queries ────────────────────────────────────────────────────────

    def is_running(self, name: str) -> bool:
        """Check if a VM process is currently running."""
        if name not in self._running:
            return False
        proc = self._running[name]
        if proc.poll() is not None:
            # Process exited
            config = self._configs.get(name)
            if config:
                config.status = "stopped"
                config.pid = None
                self._save_config(name)
            del self._running[name]
            return False
        return True

    def get_pid(self, name: str) -> int | None:
        """Get PID of running VM, or None if not running."""
        if not self.is_running(name):
            return None
        return self._running[name].pid

    def get_status(self, name: str) -> str:
        """Get the current status string for a VM."""
        if name in self._configs:
            if self.is_running(name):
                return self._configs[name].status  # May be "running" or "paused"
            return "stopped"
        return "unknown"

    def get_uptime(self, name: str) -> int:
        """Get uptime in seconds for a running VM."""
        config = self._configs.get(name)
        if not config or not config.started_at:
            return 0
        try:
            started = datetime.fromisoformat(config.started_at)
            delta = datetime.now() - started
            return int(delta.total_seconds())
        except (ValueError, TypeError):
            return 0

    def get_summary(self, name: str) -> VMSummary | None:
        """Get a VMSummary for a single VM."""
        config = self._configs.get(name)
        if not config:
            return None
        summary = config.get_summary()
        if self.is_running(name):
            summary.uptime_seconds = self.get_uptime(name)
        return summary

    def get_all_summaries(self) -> list[VMSummary]:
        """Get VMSummary for all VMs — for dashboard display."""
        summaries = []
        for name in self._configs:
            s = self.get_summary(name)
            if s:
                summaries.append(s)
        return sorted(summaries, key=lambda s: s.name)

    def get_total_resources(self) -> dict[str, Any]:
        """Get aggregate resource usage across all VMs."""
        total_ram = sum(v.ram_mb for v in self._configs.values() if v.status == "running")
        total_cpus = sum(v.cpus for v in self._configs.values() if v.status == "running")
        total_disk = sum(v.resource_limits.max_disk_gb for v in self._configs.values())
        running_count = len([v for v in self._configs.values() if v.status == "running"])

        return {
            "total_vms": len(self._configs),
            "running_vms": running_count,
            "total_ram_allocated": sum(v.ram_mb for v in self._configs.values()),
            "total_ram_active": total_ram,
            "total_cpus_allocated": sum(v.cpus for v in self._configs.values()),
            "total_cpus_active": total_cpus,
            "total_disk_gb": total_disk,
            "max_vms": GLOBAL_MAX_VMS,
            "max_ram_mb": GLOBAL_MAX_RAM_MB,
            "max_cpus": GLOBAL_MAX_CPUS,
            "ram_usage_pct": round((total_ram / GLOBAL_MAX_RAM_MB) * 100, 1) if GLOBAL_MAX_RAM_MB else 0,
            "cpu_usage_pct": round((total_cpus / GLOBAL_MAX_CPUS) * 100, 1) if GLOBAL_MAX_CPUS else 0,
        }

    # ── QEMU command building ─────────────────────────────────────────────────

    def _build_qemu_args(self, config: VMConfig) -> list[str]:
        """Build QEMU command line from a VMConfig."""
        ram_mb = str(config.ram_mb)
        vcpus = str(config.cpus)
        qmp_port = str(config.qmp_port)
        ssh_port = str(config.ssh_port)

        args = [
            config.qemu_binary,
            "-machine", "q35",
            "-smp", vcpus,
            "-m", ram_mb,
            "-accel", "whpx,kernel-irqchip=off" if os.name == "nt" else "tcg",
            "-cpu", "qemu64",
            "-drive", "if=pflash,format=raw,readonly=on,"
                      "file=C:/Program Files/qemu/share/edk2-x86_64-code.fd",
            "-netdev", f"user,id=net0,hostfwd=tcp::{ssh_port}-:22",
            "-device", "virtio-net-pci,netdev=net0",
            "-drive", f"if=virtio,format={DEFAULT_DISK_FORMAT},file={config.disk_path}",
            "-vga", config.vga,
            "-qmp", f"tcp:127.0.0.1:{qmp_port},server,nowait",
            "-name", config.vm_name,
        ]

        # Display backend
        if config.display == "spice":
            args.extend(["-display", "spice"])
            spice_port = getattr(config, 'spice_port', 0)
            if spice_port:
                args.extend(["-spice", f"port={spice_port},disable-ticketing,streaming-video=all"])
            else:
                args.extend(["-spice", "port=0,disable-ticketing,streaming-video=all"])
        elif config.display == "vnc":
            vnc_port = getattr(config, 'vnc_port', 0)
            if vnc_port:
                args.extend(["-vnc", f":{vnc_port-5900}"])
            else:
                args.extend(["-vnc", ":0"])
            args.extend(["-display", "none"])
        elif config.display == "sdl":
            args.extend(["-display", "sdl"])
        else:
            args.extend(["-display", config.display])

        # Optional ISO
        if config.iso_path and os.path.exists(config.iso_path):
            args.extend([
                "-drive", f"if=ide,media=cdrom,file={config.iso_path}",
                "-boot", config.boot_order,
            ])

        # Additional drives
        for i, drive in enumerate(config.additional_drives):
            if drive and os.path.exists(drive):
                args.extend(["-drive", f"if=virtio,format=qcow2,file={drive},index={i+1}"])

        # Guest agent removed — causes named-pipe bind errors on some Windows builds
        # If needed, can be re-added with Unix socket or fixed named-pipe path
        # if os.name == "nt":
        #     args.extend(["-chardev", ...])
        #     args.extend(["-device", ...])
        #     args.extend(["-device", ...])
        # else:
        #     ...

        # Extra args
        if config.extra_args:
            args.extend(config.extra_args)

        return args

    # ── QMP bridge helper ─────────────────────────────────────────────────────

    def get_qmp_uri(self, name: str) -> str | None:
        """Get the QMP connection URI for a VM."""
        config = self._configs.get(name)
        if not config:
            return None
        if config.qmp_socket_path:
            return f"unix:{config.qmp_socket_path}"
        return f"tcp:{config.qmp_host}:{config.qmp_port}"

    def get_ssh_uri(self, name: str) -> str | None:
        """Get the SSH connection string for a VM."""
        config = self._configs.get(name)
        if not config:
            return None
        return f"{config.ssh_username}@{config.ssh_host}:{config.ssh_port}"

    # ── Status polling (called periodically from GUI timer) ────────────────────

    def poll_status(self):
        """Update status of all managed VMs. Called by timer."""
        for name in list(self._configs.keys()):
            was_running = self._configs[name].status == "running"
            is_running = self.is_running(name)

            if was_running and not is_running:
                self._configs[name].status = "stopped"
                self._configs[name].pid = None
                self._save_config(name)
            elif is_running and self._configs[name].status != "paused":
                self._configs[name].status = "running"
                self._configs[name].uptime_seconds = self.get_uptime(name)

    def cleanup_exited(self):
        """Remove exited processes from tracking."""
        for name in list(self._running.keys()):
            self.is_running(name)  # This cleans up if process exited

    # ── Import / Export ───────────────────────────────────────────────────────

    def export_config(self, name: str, path: str) -> tuple[bool, str]:
        """Export a VM config to an external JSON file."""
        config = self._configs.get(name)
        if not config:
            return False, f"VM '{name}' not found"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(config.to_dict(), f, indent=2)
            return True, f"Config exported to {path}"
        except Exception as e:
            return False, f"Export failed: {e}"

    def import_config(self, path: str, new_name: str | None = None) -> tuple[bool, str]:
        """Import a VM config from a JSON file."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            name = new_name or data.get("vm_name", Path(path).stem)
            if name in self._configs:
                return False, f"VM '{name}' already exists"
            return self.add_vm(name, data)
        except Exception as e:
            return False, f"Import failed: {e}"

    # ── Statistics ─────────────────────────────────────────────────────────────

    def get_stats(self) -> dict[str, Any]:
        """Get comprehensive stats for all VMs."""
        summaries = self.get_all_summaries()
        running = [s for s in summaries if s.status == "running"]
        paused = [s for s in summaries if s.status == "paused"]
        stopped = [s for s in summaries if s.status == "stopped"]
        errors = [s for s in summaries if s.status == "error"]

        return {
            "total": len(summaries),
            "running": len(running),
            "paused": len(paused),
            "stopped": len(stopped),
            "error": len(errors),
            "total_ram_mb": sum(s.ram_mb for s in running),
            "total_cpus": sum(s.cpus for s in running),
            "total_disk_gb": sum(s.disk_total_gb for s in summaries),
            "vms": [s.to_dict() for s in summaries],
        }
