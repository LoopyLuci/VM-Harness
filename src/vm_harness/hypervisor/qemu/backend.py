"""QEMU Backend — implements HypervisorBackend using QEMU's QMP protocol.

This backend wraps the existing QMP client and process management code,
exposing it through the unified HypervisorBackend interface while preserving
all QMP/SPICE/VNC functionality.

Features:
- Full QMP lifecycle (start, stop, pause, resume, reset, reboot)
- SPICE/VNC display support with dynamic port allocation
- Guest agent integration (file read/write, command execution)
- Snapshot support via QMP transaction commands
- Live migration (incoming/outgoing QMP migrate)
- USB device passthrough
- Memory ballooning
- Hot-plug CPU and network devices
- Multiple display backends (SPICE, VNC, SDL, GTK, headless)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys

from vm_harness import _proc
import time
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

from vm_harness.hypervisor.backend import (
    BackendNotAvailableError,
    HypervisorBackend,
    HypervisorError,
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
    OperationNotSupportedError,
    VMSnapshot,
    VMState,
    VMStatus,
)

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_QEMU_BINARY = r"C:\Program Files\qemu\qemu-system-x86_64.exe"
DEFAULT_QEMU_IMG = r"C:\Program Files\qemu\qemu-img.exe"
DEFAULT_RAM_MB = 2048
DEFAULT_CPUS = 2
DEFAULT_QMP_PORT_BASE = 4444
DEFAULT_SSH_PORT_BASE = 2222
DEFAULT_SPICE_PORT_BASE = 5930
DEFAULT_VNC_PORT_BASE = 5900
DEFAULT_VMS_DIR = str(Path.home() / ".qemu-mcp" / "vms")

#: QEMU ``id=`` of the USB tablet, the device remote pointer events are addressed to
#: (``QMP input-send-event`` with ``device: "tablet0"``). Must stay stable: the streaming
#: bridge pins it via ``VMHARNESS_BRIDGE_TABLET_DEVICE``.
TABLET_DEVICE_ID = "tablet0"
#: QEMU ``id=`` of the explicit keyboard, so keystrokes have a documented target instead
#: of QEMU's implicit, machine-created PS/2 keyboard.
KEYBOARD_DEVICE_ID = "kbd0"
TABLET_MODEL = "usb-tablet"
KEYBOARD_MODEL = "virtio-keyboard"


def _extra_args_declare_device(extra_args: list[Any], model: str) -> bool:
    """True when the user already passes this device by hand through ``extra_args``.

    Both ``-device usb-tablet`` and ``-device=usb-tablet`` are recognised so a
    hand-written escape hatch is never duplicated by the generated flags.
    """
    for i, arg in enumerate(extra_args):
        text = str(arg)
        if text.startswith("-device=") and text.split("=", 1)[1].split(",", 1)[0].strip() == model:
            return True
        if text == "-device":
            value = str(extra_args[i + 1]) if i + 1 < len(extra_args) else ""
            if value.split(",", 1)[0].strip() == model:
                return True
    return False


def input_device_args(config: dict[str, Any]) -> list[str]:
    """The input devices a VM is launched with: a USB tablet and an explicit keyboard.

    QMP ``input-send-event`` with ``type: "abs"`` is only meaningful for a device that
    declares absolute axes. Without a tablet QEMU falls back to a relative PS/2 mouse,
    drops the absolute events without reporting an error, and the remote pointer simply
    never moves -- the keyboard works only through QEMU's implicit PS/2 keyboard.

    Both are configurable (``usb_tablet`` / ``virtio_keyboard``, default on) because a few
    guests and drivers prefer no tablet, and both are skipped when ``extra_args`` already
    declares them.
    """
    extra_args = config.get("extra_args") or []
    args: list[str] = []
    if config.get("usb_tablet", True) and not _extra_args_declare_device(extra_args, TABLET_MODEL):
        # "-usb" first: q35 and pc ship no USB controller on their PCI bus, and QEMU refuses
        # to start with "No 'usb-bus' bus found for device 'usb-tablet'" without one.
        args.extend(["-usb", "-device", f"{TABLET_MODEL},id={TABLET_DEVICE_ID}"])
    if config.get("virtio_keyboard", True) and not _extra_args_declare_device(extra_args, KEYBOARD_MODEL):
        args.extend(["-device", f"{KEYBOARD_MODEL},id={KEYBOARD_DEVICE_ID}"])
    return args


def find_qemu(tool: str) -> str | None:
    """A QEMU program: $VMH_QEMU_DIR, PATH, then the usual install places (installer, Scoop, Chocolatey, MSYS2)."""
    exe = tool + (".exe" if os.name == "nt" else "")
    dirs = [os.environ.get("VMH_QEMU_DIR", ""), r"C:\Program Files\qemu", r"%USERPROFILE%\scoop\apps\qemu\current",
            r"%ProgramData%\chocolatey\lib\qemu\tools", r"C:\msys64\ucrt64\bin", r"C:\msys64\mingw64\bin",
            "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin"]
    return _proc.find_tool([tool], [Path(os.path.expandvars(d)) / exe for d in dirs if d])


def _preferred_accelerator() -> str:
    """The best accelerator this host can actually use, or '' for none.

    Falls back to the OS-name heuristic if probing is unavailable, so a
    stripped environment still gets a sensible answer rather than no accel.
    """
    try:
        from vm_harness import env

        accelerators = env.supported_accelerators()
    except Exception:
        accelerators = []
    for accel in accelerators:
        if accel != "tcg":
            return accel
    return ""


def _firmware(name: str, qemu_binary: str) -> str | None:
    """An EDK2 firmware file shipped with QEMU (next to the binary, or in share/)."""
    base = Path(qemu_binary).resolve().parent
    for d in (base / "share", base, base.parent / "share" / "qemu", Path("/usr/share/qemu"), Path("/usr/share/OVMF")):
        if (d / name).is_file():
            return str(d / name)
    return None


_USAGE_PROCS: dict[int, Any] = {}


def _process_usage(pid: int, cpus: int) -> tuple[float, int]:
    """(CPU %, resident MB) of a VM's QEMU process, measured on the host.

    QMP has no CPU-usage query (query-cpus-fast carries no times), and query-memory-size-summary is the configured
    size, not what the guest uses; the host's view of the process is the honest number. CPU is a share of the VM's
    own vCPUs (100% = every vCPU busy). The psutil.Process is kept per pid so each reading covers the time since the
    last one; the first reading samples briefly.
    """
    try:
        import psutil
    except ImportError:
        return 0.0, 0
    try:
        proc = _USAGE_PROCS.get(pid)
        if proc is None or not proc.is_running():
            proc = psutil.Process(pid)
            _USAGE_PROCS[pid] = proc
            proc.cpu_percent(None)
            time.sleep(0.2)
        cpu = proc.cpu_percent(None) / max(1, cpus)
        rss = proc.memory_info().rss // (1024 * 1024)
        return round(min(cpu, 100.0), 1), int(rss)
    except (psutil.Error, OSError):
        _USAGE_PROCS.pop(pid, None)
        return 0.0, 0


class _RunningVM:
    """A VM's QEMU process, found again from its run file after this process (or the service) restarts.

    Quacks like the ``subprocess.Popen`` the backend used to keep in memory (``pid``, ``poll``, ``kill``), but is
    tied to the process by pid *and* creation time, so a reused pid is never mistaken for the VM."""

    def __init__(self, pid: int, created: float, run_file: Path) -> None:
        self.pid, self.created, self.run_file = pid, created, run_file

    def _process(self):
        import psutil
        try:
            p = psutil.Process(self.pid)
            return p if abs(p.create_time() - self.created) < 2 and p.status() != psutil.STATUS_ZOMBIE else None
        except psutil.Error:
            return None

    def poll(self) -> int | None:
        return None if self._process() else 0

    def kill(self) -> None:
        p = self._process()
        if p:
            p.kill()


class _ProcessTable:
    """name -> _RunningVM, kept in ``<vms_dir>/<name>/run.json`` so every VM-Harness process sees the same VMs."""

    def __init__(self, vms_dir: Path) -> None:
        self._dir = vms_dir

    def _file(self, name: str) -> Path:
        return self._dir / name / "run.json"

    def get(self, name: str) -> _RunningVM | None:
        f = self._file(name)
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        vm = _RunningVM(int(data["pid"]), float(data["created"]), f)
        if vm.poll() is not None:
            f.unlink(missing_ok=True)
            return None
        return vm

    def info(self, name: str) -> dict:
        try:
            return json.loads(self._file(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None

    def __getitem__(self, name: str) -> _RunningVM:
        vm = self.get(name)
        if vm is None:
            raise KeyError(name)
        return vm

    def __setitem__(self, name: str, proc: Any) -> None:
        import psutil
        f = self._file(name)
        f.parent.mkdir(parents=True, exist_ok=True)
        data = {"pid": proc.pid, "created": psutil.Process(proc.pid).create_time(),
                "started_at": datetime.now().isoformat(), **getattr(proc, "vmh_extra", {})}
        f.write_text(json.dumps(data), encoding="utf-8")

    def __delitem__(self, name: str) -> None:
        self._file(name).unlink(missing_ok=True)

    def keys(self) -> list[str]:
        return [p.parent.name for p in self._dir.glob("*/run.json") if p.parent.name in self]


# ── Helper: find next free port ────────────────────────────────────────────────

def _find_free_port(base: int, max_tries: int = 100) -> int:
    """Find the next free TCP port starting from base."""
    import socket
    for port in range(base, base + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.1)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError(f"No free port found starting from {base}")


# ── QEMUBackend ────────────────────────────────────────────────────────────────

class QEMUBackend(HypervisorBackend):
    """QEMU hypervisor backend using QMP (QEMU Machine Protocol).

    This backend manages QEMU processes directly, communicating with them
    via QMP over TCP or Unix sockets. It supports SPICE, VNC, SDL, GTK,
    and headless display modes.

    Config keys:
        qemu_binary: Path to qemu-system-x86_64.exe (default: auto-detect)
        qemu_img: Path to qemu-img.exe (default: auto-detect)
        vms_dir: Directory for VM config files (default: ~/.qemu-mcp/vms)
        qmp_port_base: Starting port for QMP allocation (default: 4444)
        spice_port_base: Starting port for SPICE (default: 5930)
        vnc_port_base: Starting port for VNC (default: 5900)
        enable_kvm: Enable hardware acceleration if available (default: True)
        default_disk_format: Default disk image format (default: qcow2)
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._qemu_binary = self._config.get("qemu_binary") or find_qemu("qemu-system-x86_64") or DEFAULT_QEMU_BINARY
        self._qemu_img = self._config.get("qemu_img") or find_qemu("qemu-img") or DEFAULT_QEMU_IMG
        self._vms_dir = Path(self._config.get("vms_dir", DEFAULT_VMS_DIR))
        self._vms_dir.mkdir(parents=True, exist_ok=True)
        self._qmp_port_base = self._config.get("qmp_port_base", DEFAULT_QMP_PORT_BASE)
        self._spice_port_base = self._config.get("spice_port_base", DEFAULT_SPICE_PORT_BASE)
        self._vnc_port_base = self._config.get("vnc_port_base", DEFAULT_VNC_PORT_BASE)
        self._enable_kvm = self._config.get("enable_kvm", True)
        self._default_disk_format = self._config.get("default_disk_format", "qcow2")

        # Runtime state
        self._processes = _ProcessTable(self._vms_dir)
        self._qmp_clients: dict[str, Any] = {}  # QMPClient instances
        self._next_qmp_port = self._qmp_port_base
        self._next_spice_port = self._spice_port_base
        self._next_vnc_port = self._vnc_port_base

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def default_name(self) -> str:
        return "qemu"

    @property
    def display_name(self) -> str:
        return "QEMU/KVM"

    @property
    def version(self) -> str:
        try:
            result = subprocess.run(
                [self._qemu_binary, "--version"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return result.stdout.split("\n")[0]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return "unknown"

    @property
    def is_available(self) -> bool:
        return os.path.isfile(self._qemu_binary)

    @property
    def supported_features(self) -> set[str]:
        return {
            "create", "destroy", "start", "stop", "pause", "resume",
            "reset", "reboot", "status", "list", "snapshots",
            "guest_agent", "spice", "vnc", "sdl", "gtk",
            "live_migration", "cold_migration", "usb_passthrough",
            "hotplug_cpu", "hotplug_memory", "memory_balloon",
            "screenshots", "display_password", "file_read", "file_write",
            "exec", "metrics", "console", "network", "disk_resize",
            "cdrom", "import", "export", "clone",
        }

    # ── Initialization ───────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Initialize the QEMU backend."""
        if not self.is_available:
            raise BackendNotAvailableError(
                f"QEMU binary not found: {self._qemu_binary}"
            )
        # Verify qemu-img is available
        if not os.path.isfile(self._qemu_img):
            logger.warning("qemu-img not found at %s — disk operations limited", self._qemu_img)
            self._qemu_img = ""
        await super().initialize()

    async def shutdown(self) -> None:
        """Stop all running VMs and clean up."""
        for name in list(self._processes.keys()):
            try:
                await self.stop_vm(name, force=True)
            except Exception as e:
                logger.warning("Error stopping VM '%s' during shutdown: %s", name, e)
        await super().shutdown()

    # ── Discovery ────────────────────────────────────────────────────────────

    async def list_vms(self) -> list[str]:
        """List all VM names from the vms directory."""
        vms = []
        for config_file in sorted(self._vms_dir.glob("*.json")):
            vms.append(config_file.stem)
        return vms

    # ── VM Lifecycle ─────────────────────────────────────────────────────────

    async def create_vm(self, config: VMConfig) -> str:
        """Create a new QEMU VM.

        Creates the VM configuration file and optionally pre-creates the disk image.
        """
        if await self.find_vm(config.name):
            raise VMAlreadyRunningError(f"VM '{config.name}' already exists")

        vm_dir = self._vms_dir / config.name
        vm_dir.mkdir(parents=True, exist_ok=True)

        # Create disk image if specified
        if config.disk_path and config.disk_size_gb > 0:
            disk_path = config.disk_path
            if not os.path.isfile(disk_path):
                await self._create_disk(disk_path, config.disk_size_gb, config.disk_format)
        elif config.disk_path:
            disk_path = config.disk_path
        else:
            disk_path = str(vm_dir / f"{config.name}.{config.disk_format}")
            if config.disk_size_gb > 0 and not os.path.isfile(disk_path):
                await self._create_disk(disk_path, config.disk_size_gb, config.disk_format)

        # Allocate ports
        qmp_port = config.management_port or _find_free_port(self._next_qmp_port)
        self._next_qmp_port = qmp_port + 1

        spice_port = 0
        vnc_port = 0
        if config.display_type == VMDisplayType.SPICE:
            spice_port = config.display_port or _find_free_port(self._next_spice_port)
            self._next_spice_port = spice_port + 1
        elif config.display_type == VMDisplayType.VNC:
            vnc_port = config.display_port or _find_free_port(self._next_vnc_port)
            self._next_vnc_port = vnc_port + 1

        # Build VM config dict
        vm_config = {
            "name": config.name,
            "description": config.description,
            "tags": config.tags,
            "ram_mb": config.ram_mb,
            "cpus": config.cpus,
            "cores_per_socket": config.cores_per_socket,
            "threads_per_core": config.threads_per_core,
            "disk_path": disk_path,
            "disk_format": config.disk_format,
            "disk_size_gb": config.disk_size_gb,
            "iso_path": config.iso_path,
            "additional_disks": config.additional_disks,
            "network_mode": config.network_mode.value,
            "network_bridge": config.network_bridge,
            "mac_address": config.mac_address,
            "port_forwards": config.port_forwards,
            "display_type": config.display_type.value,
            "display_port": spice_port or vnc_port,
            "display_bind": config.display_bind,
            "display_password": config.display_password,
            "boot_order": config.boot_order,
            "boot_firmware": config.boot_firmware,
            "management_port": qmp_port,
            "management_type": "qmp",
            "management_password": config.management_password,
            "enable_kvm": self._enable_kvm,
            "enable_nested_virt": config.enable_nested_virt,
            "cpu_model": config.cpu_model,
            "machine_type": config.machine_type,
            "extra_args": config.extra_args,
            "custom_data": config.custom_data,
            "created_at": datetime.now().isoformat(),
        }

        # Save config
        config_path = self._vms_dir / f"{config.name}.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(vm_config, f, indent=2)

        logger.info("Created QEMU VM '%s' at %s", config.name, config_path)
        return config.name

    async def destroy_vm(self, name: str) -> None:
        """Delete a VM and its associated files."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        if name in self._processes:
            raise VMAlreadyRunningError(
                f"VM '{name}' is running. Stop it first before destroying."
            )

        # Remove config and VM directory
        config_path = self._vms_dir / f"{name}.json"
        if config_path.exists():
            config_path.unlink()

        vm_dir = self._vms_dir / name
        if vm_dir.exists():
            import shutil
            shutil.rmtree(vm_dir, ignore_errors=True)

        logger.info("Destroyed QEMU VM '%s'", name)

    async def start_vm(self, name: str, headless: bool = False) -> None:
        """Start a QEMU VM process."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        if name in self._processes and self._processes[name].poll() is None:
            raise VMAlreadyRunningError(f"VM '{name}' is already running")

        config = await self._load_vm_config(name)

        # Auto-create disk if missing
        disk_path = config.get("disk_path", "")
        disk_size_gb = config.get("disk_size_gb", 0)
        disk_format = config.get("disk_format", "qcow2")
        if disk_path and disk_size_gb > 0 and not os.path.isfile(disk_path):
            await self._create_disk(disk_path, disk_size_gb, disk_format)

        args = self._build_qemu_args(config, headless)

        try:
            vm_dir = self._vms_dir / name
            vm_dir.mkdir(parents=True, exist_ok=True)
            log_path = vm_dir / "qemu.log"
            with open(log_path, "wb") as log_file:
                # Detached: the VM outlives whichever VM-Harness process started it. Output goes to a file (a pipe
                # nobody reads would fill and freeze QEMU).
                flags = (subprocess.CREATE_NEW_PROCESS_GROUP | _proc.CREATE_NO_WINDOW) if os.name == "nt" else 0
                proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
                                        creationflags=flags, start_new_session=os.name != "nt")
            await asyncio.sleep(0.5)
            if proc.poll() is not None:
                raise HypervisorError(log_path.read_text(encoding="utf-8", errors="replace").strip()[-800:]
                                      or f"QEMU exited {proc.returncode}")
            proc.vmh_extra = {"log": str(log_path), "args": args}
            self._processes[name] = proc
            logger.info("Started QEMU VM '%s' (PID: %d)", name, proc.pid)

            # Wait briefly for QMP to become available
            await self._wait_for_qmp(name, timeout=30)
        except Exception as e:
            if name in self._processes:
                del self._processes[name]
            raise HypervisorError(f"Failed to start VM '{name}': {e}")

    async def stop_vm(self, name: str, force: bool = False) -> None:
        """Stop a running QEMU VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        if name not in self._processes:
            raise VMNotRunningError(f"VM '{name}' is not running")

        if not force:
            # Try graceful shutdown via QMP
            try:
                client = await self._get_qmp_client(name)
                if client and client.is_connected:
                    await client.send("system_powerdown")
                    # Wait for process to exit
                    for _ in range(60):
                        if name not in self._processes:
                            await self._disconnect_qmp(name)
                            return
                        await asyncio.sleep(0.5)
            except Exception as e:
                logger.debug("Graceful shutdown failed for '%s': %s", name, e)

        # Force kill
        proc = self._processes.get(name)
        if proc is not None:
            proc.kill()
            for _ in range(20):
                if proc.poll() is not None:
                    break
                await asyncio.sleep(0.25)
        del self._processes[name]
        await self._disconnect_qmp(name)
        logger.info("Stopped QEMU VM '%s'", name)

    async def pause_vm(self, name: str) -> None:
        """Pause a running QEMU VM."""
        client = await self._require_qmp_client(name)
        await client.send("stop")

    async def resume_vm(self, name: str) -> None:
        """Resume a paused QEMU VM."""
        client = await self._require_qmp_client(name)
        await client.send("cont")

    async def reset_vm(self, name: str) -> None:
        """Hard reset a running QEMU VM."""
        client = await self._require_qmp_client(name)
        await client.send("system_reset")

    async def reboot_vm(self, name: str, graceful: bool = True) -> None:
        """Reboot a running QEMU VM."""
        client = await self._require_qmp_client(name)
        if graceful:
            await client.send("system_reset")
        else:
            await client.send("system_reset")

    async def shutdown_guest(self, name: str, timeout: int = 30) -> None:
        """Gracefully shut down the guest OS via QMP power button."""
        client = await self._require_qmp_client(name)
        await client.send("system_powerdown")

    # ── Status / Info ────────────────────────────────────────────────────────

    async def get_status(self, name: str) -> VMStatus:
        """Get the current status of a QEMU VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM '{name}' not found")

        config = await self._load_vm_config(name)
        status = VMStatus(name=name, backend_name=self.default_name)

        proc = self._processes.get(name)
        if proc is not None:
            status.state = VMState.RUNNING
            status.pid = proc.pid
            status.started_at = self._processes.info(name).get("started_at", "")
            status.uptime_seconds = int(time.time() - proc.created)
        else:
            status.state = VMState.STOPPED

        status.ram_allocated_mb = config.get("ram_mb", 0)
        status.cpus_allocated = config.get("cpus", 0)
        status.management_uri = f"tcp:127.0.0.1:{config.get('management_port', 0)}"
        disk = config.get("disk_path", "")
        if disk and os.path.isfile(disk):
            status.disk_usage_gb = round(os.path.getsize(disk) / 1024 ** 3, 2)
        if status.state == VMState.RUNNING and status.pid:
            status.cpu_usage_pct, status.ram_usage_mb = await asyncio.to_thread(
                _process_usage, status.pid, int(status.cpus_allocated or 1))

        # Get QMP status if running
        if status.state == VMState.RUNNING:
            try:
                client = await self._get_qmp_client(name)
                if client and client.is_connected:
                    result = await client.send("query-status")
                    status_data = result.get("return", {})
                    if status_data.get("status") == "paused":
                        status.state = VMState.PAUSED
                    elif status_data.get("status") == "running":
                        status.state = VMState.RUNNING
            except Exception:
                pass

        return status

    async def get_config(self, name: str) -> VMConfig | None:
        """Get the configuration of a QEMU VM."""
        if not await self.find_vm(name):
            return None
        return await self._load_vm_config(name)

    # ── Metrics ──────────────────────────────────────────────────────────────

    async def get_metrics(self, name: str) -> VMMetrics:
        """Get real-time metrics for a running QEMU VM."""
        client = await self._require_qmp_client(name)
        metrics = VMMetrics(timestamp=datetime.now().isoformat())

        # CPU and memory: measured on the host (QMP cannot say how busy the guest is).
        proc = self._processes.get(name)
        if proc is not None:
            config = await self._load_vm_config(name)
            cpus = int(config.get("cpus", 1) or 1)
            metrics.cpu_usage_pct, metrics.ram_usage_mb = await asyncio.to_thread(_process_usage, proc.pid, cpus)
            metrics.ram_available_mb = max(0, int(config.get("ram_mb", 0)) - metrics.ram_usage_mb)

        try:
            # Query block stats
            result = await client.send("query-blockstats")
            stats = result.get("return", [])
            for dev in stats:
                metrics.disk_read_bytes += dev.get("stats", {}).get("rd_bytes", 0)
                metrics.disk_write_bytes += dev.get("stats", {}).get("wr_bytes", 0)
        except Exception:
            pass

        try:
            # Query network stats
            result = await client.send("query-network")
            net_data = result.get("return", [])
            for iface in net_data:
                metrics.net_rx_bytes += iface.get("rx-bytes", 0)
                metrics.net_tx_bytes += iface.get("tx-bytes", 0)
        except Exception:
            pass

        return metrics

    async def stream_metrics(self, name: str, interval_s: float = 2.0) -> AsyncIterator[VMMetrics]:
        """Stream metrics at the given interval."""
        while True:
            try:
                if name not in self._processes:
                    break
                metrics = await self.get_metrics(name)
                yield metrics
                await asyncio.sleep(interval_s)
            except Exception:
                break

    # ── Display ──────────────────────────────────────────────────────────────

    async def get_display(self, name: str) -> VMDisplay:
        """Get display connection details for a running QEMU VM."""
        config = await self._load_vm_config(name)
        display = VMDisplay()

        display_type = config.get("display_type", "spice")
        display.display_type = VMDisplayType(display_type)
        display.host = config.get("display_bind", "127.0.0.1")
        display.password = config.get("display_password", "")

        if display_type == "spice":
            display.port = config.get("display_port", 0)
            display.uri = f"spice://{display.host}:{display.port}"
        elif display_type == "vnc":
            display.port = config.get("display_port", 0)
            display.uri = f"vnc://{display.host}:{display.port}"
        elif display_type == "sdl":
            display.uri = "sdl:local"
        elif display_type == "gtk":
            display.uri = "gtk:local"

        return display

    async def set_display_password(self, name: str, password: str) -> None:
        """Set the SPICE/VNC display password."""
        client = await self._require_qmp_client(name)
        await client.send("spice_set_passwd", {"password": password})

    async def screenshot(self, name: str) -> bytes:
        """Capture a screenshot from the VM display."""
        client = await self._require_qmp_client(name)
        # QEMU writes the file itself, so the path must be one QEMU can reach: the VM's own folder on this host.
        out = self._vms_dir / name / "screenshot.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.unlink(missing_ok=True)
        await client.send("screendump", {"filename": str(out), "format": "png"})
        for _ in range(20):
            if out.exists() and out.stat().st_size > 0:
                break
            await asyncio.sleep(0.1)
        try:
            return out.read_bytes()
        finally:
            out.unlink(missing_ok=True)

    # ── Console / Terminal ───────────────────────────────────────────────────

    async def get_console(self, name: str) -> VMConsole:
        """Get console access details for a QEMU VM."""
        config = await self._load_vm_config(name)
        console = VMConsole()
        console.protocol = "spice-agent"
        console.host = "127.0.0.1"
        console.port = config.get("management_port", 0)
        console.uri = f"spice://127.0.0.1:{config.get('display_port', 0)}"
        return console

    # ── Guest Agent ──────────────────────────────────────────────────────────

    async def guest_exec(self, name: str, command: str,
                         args: list[str] | None = None,
                         env: dict[str, str] | None = None,
                         timeout: int = 30,
                         capture_output: bool = True) -> dict[str, Any]:
        """Execute a command inside the guest via QMP guest-exec."""
        client = await self._require_qmp_client(name)
        exec_args: dict[str, Any] = {"path": command}
        if args:
            exec_args["arg"] = args
        if env:
            exec_args["env"] = [{"name": k, "value": v} for k, v in env.items()]
        exec_args["capture-output"] = capture_output

        result = await client.send("guest-exec", exec_args)
        pid = result.get("return", {}).get("pid", 0)

        # Wait for completion
        deadline = time.time() + timeout
        while time.time() < deadline:
            status_result = await client.send("guest-exec-status", {"pid": pid})
            status = status_result.get("return", {})
            if status.get("exited", False):
                return {
                    "exit_code": status.get("exitcode", -1),
                    "stdout": status.get("out-data", b"").decode("utf-8", errors="replace") if status.get("out-data") else "",
                    "stderr": status.get("err-data", b"").decode("utf-8", errors="replace") if status.get("err-data") else "",
                    "timed_out": False,
                }
            await asyncio.sleep(0.5)

        return {"exit_code": -1, "stdout": "", "stderr": "", "timed_out": True}

    async def guest_info(self, name: str) -> VMGuestInfo:
        """Get guest OS information via QMP guest-info."""
        client = await self._require_qmp_client(name)
        result = await client.send("guest-info")
        info_data = result.get("return", {})

        info = VMGuestInfo()
        info.hostname = info_data.get("hostname", "")
        info.os_name = info_data.get("os-release", {}).get("name", "")
        info.os_version = info_data.get("os-release", {}).get("version", "")
        info.timezone = info_data.get("timezone", "")
        info.uptime_seconds = info_data.get("uptime", 0)
        return info

    async def guest_file_read(self, name: str, path: str,
                              offset: int = 0,
                              max_bytes: int = 65536) -> bytes:
        """Read a file from inside the guest via QMP guest-file-read."""
        import base64
        client = await self._require_qmp_client(name)
        # Open file
        open_args = {"path": path, "mode": "r"}
        open_result = await client.send("guest-file-open", open_args)
        handle = open_result.get("return", 0)

        # Read data
        read_args = {"handle": handle, "count": max_bytes}
        read_result = await client.send("guest-file-read", read_args)
        data = read_result.get("return", {}).get("buf-b64", "")

        # Close file
        await client.send("guest-file-close", {"handle": handle})

        return base64.b64decode(data) if isinstance(data, str) else data

    async def guest_file_write(self, name: str, path: str,
                               data: bytes, offset: int = 0) -> None:
        """Write data to a file inside the guest via QMP guest-file-write."""
        client = await self._require_qmp_client(name)
        import base64
        encoded = base64.b64encode(data).decode("ascii")

        # Open file
        open_result = await client.send("guest-file-open", {
            "path": path, "mode": "w"
        })
        handle = open_result.get("return", 0)

        # Write data
        await client.send("guest-file-write", {
            "handle": handle, "buf-b64": encoded
        })

        # Close file
        await client.send("guest-file-close", {"handle": handle})

    # ── Snapshots ────────────────────────────────────────────────────────────

    async def list_snapshots(self, name: str) -> list[VMSnapshot]:
        """List all snapshots for a QEMU VM."""
        client = await self._require_qmp_client(name)
        result = await client.send("query-named-block-nodes")
        nodes = result.get("return", [])

        snapshots = []
        for node in nodes:
            if node.get("drv"):
                snapshot_name = node.get("node-name", "")
                if snapshot_name:
                    snapshots.append(VMSnapshot(
                        name=snapshot_name,
                        created_at="",
                        is_current=False,
                    ))
        return snapshots

    async def create_snapshot(self, name: str, snapshot_name: str,
                              description: str = "",
                              include_memory: bool = False) -> VMSnapshot:
        """Create a snapshot of a QEMU VM."""
        client = await self._require_qmp_client(name)

        if include_memory:
            # Full system snapshot (savevm)
            await client.send("human-monitor-command", {
                "command-line": f"savevm {snapshot_name}"
            })
        else:
            # Blockdev snapshot
            await client.send("blockdev-snapshot-sync", {
                "node-name": "drive-virtio-disk0",
                "snapshot-node-name": snapshot_name,
                "snapshot-file": f"/tmp/{snapshot_name}.qcow2",
                "format": "qcow2",
            })

        return VMSnapshot(
            name=snapshot_name,
            description=description,
            created_at=datetime.now().isoformat(),
            state_at_snapshot=VMState.RUNNING,
        )

    async def restore_snapshot(self, name: str, snapshot_name: str) -> None:
        """Restore a QEMU VM to a previous snapshot."""
        client = await self._require_qmp_client(name)
        await client.send("human-monitor-command", {
            "command-line": f"loadvm {snapshot_name}"
        })

    async def delete_snapshot(self, name: str, snapshot_name: str) -> None:
        """Delete a QEMU snapshot."""
        client = await self._require_qmp_client(name)
        await client.send("human-monitor-command", {
            "command-line": f"delvm {snapshot_name}"
        })

    # ── Disk Operations ─────────────────────────────────────────────────────

    async def resize_disk(self, name: str, new_size_gb: int) -> None:
        """Resize a QEMU VM's disk image."""
        config = await self._load_vm_config(name)
        disk_path = config.get("disk_path", "")
        if not disk_path:
            raise VMNotFoundError(f"No disk path for VM '{name}'")

        if name in self._processes:
            raise VMAlreadyRunningError("Cannot resize disk while VM is running")

        if self._qemu_img:
            await _proc.run(
                [self._qemu_img, "resize", disk_path, f"{new_size_gb}G"],
                check=True, capture_output=True, text=True
            )
        else:
            # Use QMP block_resize if VM is stopped but QMP is available
            raise OperationNotSupportedError("qemu-img not available for disk resize")

    async def eject_cdrom(self, name: str) -> None:
        """Eject the virtual CD-ROM media."""
        client = await self._require_qmp_client(name)
        await client.send("eject", {"device": "ide0-cd0"})

    async def insert_cdrom(self, name: str, iso_path: str) -> None:
        """Insert an ISO into the virtual CD-ROM drive."""
        client = await self._require_qmp_client(name)
        await client.send("change", {
            "device": "ide0-cd0",
            "target": iso_path,
            "arg": "raw"
        })

    # ── Networking ───────────────────────────────────────────────────────────

    async def list_network_interfaces(self, name: str) -> list[VMNetwork]:
        """List network interfaces for a QEMU VM."""
        config = await self._load_vm_config(name)
        interfaces = []

        # Primary NIC
        net = VMNetwork(
            name="net0",
            mode=VMNetworkMode(config.get("network_mode", "nat")),
            bridge=config.get("network_bridge", ""),
            mac_address=config.get("mac_address", ""),
            connected=True,
        )
        interfaces.append(net)
        return interfaces

    # ── Migration ────────────────────────────────────────────────────────────

    async def migrate_vm(self, name: str, target_uri: str,
                         live: bool = True,
                         bandwidth_mbps: int = 0) -> None:
        """Migrate a QEMU VM to another host."""
        client = await self._require_qmp_client(name)
        migrate_args: dict[str, Any] = {"uri": target_uri}
        if bandwidth_mbps > 0:
            migrate_args["max-bandwidth"] = bandwidth_mbps * 1024 * 1024

        if live:
            await client.send("migrate", migrate_args)
        else:
            await client.send("migrate", {**migrate_args, "detach": True})

    # ── Import / Export ─────────────────────────────────────────────────────

    async def export_vm(self, name: str, output_path: str,
                        format: str = "qcow2") -> None:
        """Export a QEMU VM's disk image."""
        config = await self._load_vm_config(name)
        disk_path = config.get("disk_path", "")
        if not disk_path:
            raise VMNotFoundError(f"No disk path for VM '{name}'")

        if name in self._processes:
            raise VMAlreadyRunningError("Cannot export while VM is running")

        if self._qemu_img:
            await _proc.run(
                [self._qemu_img, "convert", "-f", config.get("disk_format", "qcow2"),
                 "-O", format, disk_path, output_path],
                check=True, capture_output=True, text=True
            )
        else:
            import shutil
            shutil.copy2(disk_path, output_path)

    async def import_vm(self, input_path: str,
                        new_name: str | None = None) -> str:
        """Import a QEMU VM from a disk image."""
        if not new_name:
            new_name = Path(input_path).stem

        config = VMConfig(
            name=new_name,
            disk_path=input_path,
            disk_format=Path(input_path).suffix.lstrip("."),
        )
        return await self.create_vm(config)

    # ── Cloning ──────────────────────────────────────────────────────────────

    async def clone_vm(self, name: str, new_name: str,
                       linked: bool = False,
                       snapshots: bool = False) -> str:
        """Clone a QEMU VM."""
        config = await self._load_vm_config(name)
        new_config = VMConfig(
            name=new_name,
            ram_mb=config.get("ram_mb", DEFAULT_RAM_MB),
            cpus=config.get("cpus", DEFAULT_CPUS),
            disk_format=config.get("disk_format", "qcow2"),
        )

        # Create the new VM
        await self.create_vm(new_config)

        # Copy or create linked clone of disk
        src_disk = config.get("disk_path", "")
        new_vm_config = await self._load_vm_config(new_name)
        dst_disk = new_vm_config.get("disk_path", "")

        if src_disk and dst_disk:
            if linked:
                if self._qemu_img:
                    await _proc.run(
                        [self._qemu_img, "create", "-f", "qcow2", "-b", src_disk,
                         "-F", config.get("disk_format", "qcow2"), dst_disk],
                        check=True, capture_output=True, text=True
                    )
            else:
                import shutil
                shutil.copy2(src_disk, dst_disk)

        return new_name

    # ── USB Passthrough ─────────────────────────────────────────────────────

    async def attach_usb(self, name: str, vendor_id: str,
                         product_id: str) -> None:
        """Attach a USB device to a QEMU VM."""
        client = await self._require_qmp_client(name)
        await client.send("device_add", {
            "driver": "usb-host",
            "vendorid": vendor_id,
            "productid": product_id,
        })

    async def detach_usb(self, name: str, vendor_id: str,
                         product_id: str) -> None:
        """Detach a USB device from a QEMU VM."""
        client = await self._require_qmp_client(name)
        # Find the device ID
        result = await client.send("query-usb")
        devices = result.get("return", [])
        for dev in devices:
            if (dev.get("vendor-id") == vendor_id and
                    dev.get("product-id") == product_id):
                await client.send("device_del", {"id": dev.get("id", "")})
                break

    # ── Internal helpers ─────────────────────────────────────────────────────

    async def _load_vm_config(self, name: str) -> dict[str, Any]:
        """Load VM config from disk."""
        config_path = self._vms_dir / f"{name}.json"
        if not config_path.exists():
            raise VMNotFoundError(f"VM '{name}' config not found")
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _build_qemu_args(self, config: dict[str, Any], headless: bool = False) -> list[str]:
        """Build QEMU command line from a VM config dict."""
        name = config.get("name", "vm")
        ram_mb = str(config.get("ram_mb", DEFAULT_RAM_MB))
        vcpus = str(config.get("cpus", DEFAULT_CPUS))
        qmp_port = str(config.get("management_port", 0))
        disk_path = config.get("disk_path", "")
        disk_format = config.get("disk_format", "qcow2")
        display_type = config.get("display_type", "spice")
        display_port = config.get("display_port", 0)
        display_password = config.get("display_password", "")
        iso_path = config.get("iso_path", "")
        boot_order = config.get("boot_order", ["hd", "cdrom", "network"])
        mac_address = config.get("mac_address", "")
        extra_args = config.get("extra_args", [])

        args = [
            self._qemu_binary,
        ]
        machine_type = config.get("machine_type", "q35") or "q35"
        args.extend(["-machine", machine_type])
        args.extend(["-smp", vcpus])
        args.extend(["-m", ram_mb])
        # "host"/"max" crash under WHPX ("Unexpected VP exit code 4" on this Windows 10 host); QEMU's default model
        # runs everywhere. KVM on Linux takes "host" for full speed.
        cpu_model = config.get("cpu_model", "") or (
            "host" if sys.platform.startswith("linux") and config.get("enable_kvm", True) else "")
        if cpu_model:
            args.extend(["-cpu", cpu_model])
        args.extend(["-name", name])
        args.extend(["-qmp", f"tcp:127.0.0.1:{qmp_port},server,nowait"])

        # Acceleration: the hardware accelerator if the host really has it, else TCG (slower, always present).
        # Chosen from a live probe of the QEMU binary and the host, not from the OS name: WHPX without the
        # Windows hypervisor makes QEMU fail to start instead of falling back. WHPX also needs
        # kernel-irqchip=off on this class of host. TCG stays in the list as the guaranteed fallback.
        if config.get("enable_kvm", True):
            accel = _preferred_accelerator()
            if accel == "whpx":
                args.extend(["-accel", "whpx,kernel-irqchip=off"])
            elif accel:
                args.extend(["-accel", accel])
        args.extend(["-accel", "tcg"])

        # Firmware. UEFI gets its code (read-only) and a variable store of its own in the VM's folder, so boot
        # entries an installed OS writes (and the firmware's own settings) survive restarts as on real hardware.
        # Without the store the firmware forgets every boot entry and may stop at its shell instead of booting the
        # disk.
        if config.get("boot_firmware", "bios") == "uefi":
            fw = _firmware("edk2-x86_64-code.fd", self._qemu_binary) or _firmware("OVMF_CODE.fd", self._qemu_binary)
            if not fw:
                raise HypervisorError("UEFI firmware (edk2-x86_64-code.fd) not found next to QEMU")
            args.extend(["-drive", f"if=pflash,format=raw,readonly=on,file={fw}"])
            vars_file = self._vms_dir / name / "efivars.fd"
            if not vars_file.is_file():
                template = _firmware("edk2-i386-vars.fd", self._qemu_binary) or _firmware("OVMF_VARS.fd", self._qemu_binary)
                if template:
                    vars_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(template, vars_file)
            if vars_file.is_file():
                args.extend(["-drive", f"if=pflash,format=raw,file={vars_file}"])

        # Network
        net_mode = config.get("network_mode", "nat")
        netdev_args = ["user", "id=net0"]
        if net_mode == "nat":
            for pf in config.get("port_forwards", []):
                host_port = pf.get("host_port", 0)
                guest_port = pf.get("guest_port", 0)
                if host_port and guest_port:
                    netdev_args.append(f"hostfwd=tcp::{host_port}-:{guest_port}")
        args.extend(["-netdev", ",".join(netdev_args)])
        nic_args = ["virtio-net-pci", "netdev=net0"]
        if mac_address:
            nic_args.append(f"mac={mac_address}")
        args.extend(["-device", ",".join(nic_args)])

        # Boot priority: the system disk first, unless the ISO is meant to boot first (installing). bootindex is what
        # UEFI firmware follows (it ignores -boot); SeaBIOS honours it too.
        iso_first = bool(iso_path and os.path.exists(iso_path)) and (
            (boot_order[0] if isinstance(boot_order, list) and boot_order else str(boot_order)[:1]) in ("cdrom", "d"))
        disk_index, iso_index = (1, 0) if iso_first else (0, 1)

        # Disk
        if disk_path:
            args.extend([
                "-drive", f"if=none,id=disk0,format={disk_format},file={disk_path}",
                "-device", f"virtio-blk-pci,drive=disk0,bootindex={disk_index}",
            ])

        # Additional disks
        for i, disk in enumerate(config.get("additional_disks", [])):
            if isinstance(disk, dict):
                disk_file = disk.get("path", "")
                disk_fmt = disk.get("format", "qcow2")
            else:
                disk_file = str(disk)
                disk_fmt = "qcow2"
            if disk_file and os.path.exists(disk_file):
                args.extend([
                    "-drive", f"if=virtio,format={disk_fmt},file={disk_file},index={i+1}"
                ])

        # Display
        if headless or display_type == "headless":
            args.extend(["-display", "none"])
        elif display_type == "spice":
            # QEMU removed "-display spice"; the server is configured with
            # -spice and the guest output is left headless for a client to
            # attach to. Passing "-display spice" now fails with
            # "Parameter 'type' does not accept value 'spice'".
            spice_args = ["disable-ticketing", "streaming-video=all"]
            if display_port:
                spice_args.insert(0, f"port={display_port}")
            if display_password:
                spice_args.append(f"password={display_password}")
            args.extend(["-spice", ",".join(spice_args), "-display", "none"])
        elif display_type == "vnc":
            vnc_port = display_port or 5900
            args.extend(["-vnc", f":{vnc_port - 5900}", "-display", "none"])
        elif display_type == "sdl":
            args.extend(["-display", "sdl"])
        elif display_type == "gtk":
            args.extend(["-display", "gtk"])

        # Input devices: an absolute pointing device (QMP abs events need one) and an
        # explicit keyboard. Without the tablet the remote pointer never moves.
        args.extend(input_device_args(config))

        # ISO
        if iso_path and os.path.exists(iso_path):
            args.extend([
                "-drive", f"if=none,id=cd0,media=cdrom,readonly=on,file={iso_path}",
                "-device", f"ide-cd,drive=cd0,bootindex={iso_index}",
            ])

        # Guest agent
        if os.name == "nt":
            # Use a TCP socket for the guest agent on Windows (named pipes can fail to bind)
            ga_port = _find_free_port(4500)
            args.extend([
                "-chardev", f"socket,host=127.0.0.1,port={ga_port},server=on,wait=off,id=ga0",
                "-device", "virtio-serial-pci",
                "-device", "virtserialport,chardev=ga0,name=org.qemu.guest_agent.0",
            ])
        else:
            ga_socket = self._vms_dir / f"qga-{name}.sock"
            args.extend([
                "-chardev", f"socket,path={ga_socket},server=on,wait=off,id=ga0",
                "-device", "virtio-serial-pci",
                "-device", "virtserialport,chardev=ga0,name=org.qemu.guest_agent.0",
            ])

        # Extra args
        if extra_args:
            args.extend(extra_args)

        return args

    async def _create_disk(self, path: str, size_gb: int, format: str) -> None:
        """Create a disk image using qemu-img."""
        if self._qemu_img:
            await _proc.run(
                [self._qemu_img, "create", "-f", format, path, f"{size_gb}G"],
                check=True, capture_output=True, text=True
            )
        else:
            # Fallback: create a sparse file
            with open(path, "wb") as f:
                f.seek(size_gb * 1024 * 1024 * 1024 - 1)
                f.write(b"\0")

    async def _get_qmp_client(self, name: str) -> Any:
        """Get or create a QMP client for a VM."""
        if name in self._qmp_clients:
            client = self._qmp_clients[name]
            if client.is_connected:
                return client

        # Create new client
        config = await self._load_vm_config(name)
        qmp_port = config.get("management_port", 0)
        uri = f"tcp:127.0.0.1:{qmp_port}"

        # Import QMP client from existing codebase
        try:
            from vm_harness.qmp_client import QMPClient
        except ImportError:
            # Fallback: define a minimal QMP client inline
            QMPClient = _MinimalQMPClient

        client = QMPClient(uri)
        await client.connect()
        self._qmp_clients[name] = client
        return client

    async def _require_qmp_client(self, name: str) -> Any:
        """Get a QMP client, raising if the VM is not running."""
        if name not in self._processes:
            raise VMNotRunningError(f"VM '{name}' is not running")
        return await self._get_qmp_client(name)

    async def _disconnect_qmp(self, name: str) -> None:
        """Disconnect and remove a QMP client."""
        if name in self._qmp_clients:
            try:
                await self._qmp_clients[name].disconnect()
            except Exception:
                pass
            del self._qmp_clients[name]

    async def _wait_for_qmp(self, name: str, timeout: float = 10.0) -> None:
        """Wait for QMP to become available for a VM."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                client = await self._get_qmp_client(name)
                if client and client.is_connected:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.5)
        raise TimeoutError(f"QMP not available for VM '{name}' after {timeout}s")


# ── Minimal QMP Client (fallback) ──────────────────────────────────────────────

class _MinimalQMPClient:
    """Minimal QMP client for when the full vm_mcp.qmp_client is not available."""

    def __init__(self, uri: str, password: str | None = None, timeout_sec: float = 10.0):
        self.uri = uri
        self._password = password
        self._timeout = timeout_sec
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False

    async def connect(self) -> None:
        if self.uri.startswith("unix:"):
            path = self.uri[6:]
            self._reader, self._writer = await asyncio.open_unix_connection(path)
        else:
            rest = self.uri[4:]
            last_colon = rest.rfind(":")
            host = rest[:last_colon]
            port = int(rest[last_colon + 1:])
            self._reader, self._writer = await asyncio.open_connection(host, port)
        self._connected = True
        await self._read_greeting()
        await self.send("qmp_capabilities")

    async def _read_greeting(self) -> dict[str, Any]:
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
        return greeting

    async def send(self, cmd: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._connected:
            raise RuntimeError("QMP not connected")
        message: dict[str, Any] = {"execute": cmd}
        if args:
            message["arguments"] = args
        payload = json.dumps(message) + "\n"
        self._writer.write(payload.encode())
        await self._writer.drain()
        return await self._read_response()

    async def _read_response(self) -> dict[str, Any]:
        data = await asyncio.wait_for(
            self._reader.readuntil(b"\n"),
            timeout=self._timeout,
        )
        response = json.loads(data.decode())
        if "error" in response:
            raise RuntimeError(f"QMP error: {response['error'].get('desc', 'unknown')}")
        return response

    async def disconnect(self) -> None:
        if self._writer:
            self._writer.close()
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected
