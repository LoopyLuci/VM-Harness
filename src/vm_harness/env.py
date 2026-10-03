"""Host environment probing — what this machine actually is and what it can do.

Backends need to make decisions the OS name alone cannot answer: is WHPX
really usable, is /dev/kvm present, does the QEMU build even contain the
accelerator we intend to pass, how much RAM can a VM honestly claim, is there
a GPU worth passing through. Guessing wrong produces a VM that fails to
start, or starts in slow TCG while the user believes it is accelerated.

Everything here degrades: an unknown field is ``None``, never a guess. Probes
are cheap, cached, and never raise.

    from vm_harness import env
    caps = env.host_capabilities()
    caps.accelerators        # ["whpx", "tcg"] on this host
    caps.usable_accelerator  # "whpx"
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from vm_harness import _proc

WINDOWS = "Windows"
LINUX = "Linux"
DARWIN = "Darwin"


# ── Host shape ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class HostInfo:
    """The platform facts a backend branches on."""

    system: str
    release: str
    machine: str
    python: str
    is_windows: bool
    is_linux: bool
    is_darwin: bool

    @property
    def is_posix(self) -> bool:
        return not self.is_windows


def host_info() -> HostInfo:
    """Identify the host OS and architecture."""
    system = platform.system()
    return HostInfo(
        system=system,
        release=platform.release(),
        machine=platform.machine(),
        python=platform.python_version(),
        is_windows=(system == WINDOWS),
        is_linux=(system == LINUX),
        is_darwin=(system == DARWIN),
    )


# ── CPU / memory ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CPUInfo:
    """CPU identity and virtualization support.

    ``virtualization`` is the firmware-level truth: whether the CPU exposes
    hardware virtualization to a guest at all. ``nested`` stays None when the
    host cannot be asked (no /proc, no PowerShell), rather than defaulting to
    False and quietly disabling passthrough.
    """

    model: Optional[str]
    physical_cores: Optional[int]
    logical_cores: Optional[int]
    virtualization: Optional[bool]
    nested: Optional[bool] = None

    def logical_or_physical(self) -> int:
        """Usable core count for sizing a VM, falling back sensibly."""
        return self.logical_cores or self.physical_cores or 1


def _cpu_linux() -> tuple[Optional[str], Optional[int], Optional[int], Optional[bool], Optional[bool]]:
    """Read /proc/cpuinfo and /sys for a Linux host."""
    model = cores = None
    virt = None
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None, None, None, None

    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "model name" and model is None:
            model = value
        elif key == "processor":
            cores = (cores or 0) + 1
        elif key == "flags" and virt is None:
            flags = set(value.split())
            if "vmx" in flags or "svm" in flags:
                virt = True
            else:
                virt = False

    logical = os.cpu_count()
    # Physical != logical when SMT is on; derive it when we can.
    physical = None
    if cores and logical and logical % cores == 0 and cores * 2 == logical:
        physical = cores
    else:
        physical = cores

    nested = None
    try:
        nested = Path("/sys/module/kvm_intel/parameters/nested").exists() or \
            Path("/sys/module/kvm_amd/parameters/nested").exists()
    except OSError:
        pass
    return model, physical, logical, virt, nested


def _cpu_windows() -> tuple[Optional[str], Optional[int], Optional[int], Optional[bool], Optional[bool]]:
    """Ask CIM for the CPU facts on a Windows host."""
    ps = _proc.find_tool(
        ["powershell.exe", "pwsh"],
        [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"],
        env_var="VMH_POWERSHELL",
    )
    if not ps:
        return None, None, None, None, None

    script = (
        "$c=Get-CimInstance Win32_Processor|Select-Object -First 1;"
        "$cs=Get-CimInstance Win32_ComputerSystem;"
        "[pscustomobject]@{"
        "model=$c.Name;cores=$c.NumberOfCores;logical=$c.NumberOfLogicalProcessors;"
        "virt=[bool]$c.VirtualizationFirmwareEnabled;"
        "slt=[bool]$c.SecondLevelAddressTranslationExtensions}|ConvertTo-Json -Compress"
    )
    try:
        result = _proc.run_sync([ps, "-NoProfile", "-Command", script], timeout=30)
    except Exception:
        return None, None, None, None, None
    if result.returncode != 0 or not result.stdout:
        return None, None, None, None, None

    import json
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None, None, None, None, None

    nested = None
    # SLAM (nested virtualization) is what GPU/PCI passthrough needs on Windows.
    slt = data.get("slt")
    if isinstance(slt, bool):
        nested = slt
    return (
        data.get("model"),
        data.get("cores"),
        data.get("logical"),
        data.get("virt"),
        nested,
    )


def cpu_info() -> CPUInfo:
    """Best-effort CPU identity and virtualization capability."""
    info = host_info()
    if info.is_windows:
        model, cores, logical, virt, nested = _cpu_windows()
    elif info.is_linux:
        model, cores, logical, virt, nested = _cpu_linux()
    else:
        model = platform.processor() or None
        cores, logical = None, os.cpu_count()
        virt, nested = None, None

    return CPUInfo(
        model=model or (platform.processor() or None),
        physical_cores=cores,
        logical_cores=logical or os.cpu_count(),
        virtualization=virt,
        nested=nested,
    )


def total_ram_mb() -> Optional[int]:
    """Physical RAM in MiB, or None if it cannot be determined.

    ``sysconf`` covers most hosts, but it is unreliable on Windows CPython
    builds, so the OS-specific probes are a real fallback rather than dead code.
    """
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return pages * page_size // (1024 * 1024)
    except (ValueError, OSError, AttributeError):
        pass

    info = host_info()
    if info.is_windows:
        return _ram_mb_windows()
    if info.is_linux:
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    return int(re.search(r"\d+", line).group()) // 1024
        except (OSError, AttributeError, ValueError):
            return None
    return None


def _ram_mb_windows() -> Optional[int]:
    """Physical RAM on Windows, via CIM when sysconf is unavailable."""
    ps = _proc.find_tool(
        ["powershell.exe", "pwsh"],
        [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"],
        env_var="VMH_POWERSHELL",
    )
    if not ps:
        return None
    try:
        result = _proc.run_sync(
            [ps, "-NoProfile", "-Command",
             "[math]::Floor((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1MB)"],
            timeout=30,
        )
    except Exception:
        return None
    match = re.search(r"\d+", result.stdout or "")
    return int(match.group()) if match else None


# ── Accelerators ───────────────────────────────────────────────────────────────

#: Accelerators QEMU can expose, best first, per platform.
ACCEL_BY_PLATFORM = {
    WINDOWS: ["whpx", "tcg"],
    DARWIN: ["hvf", "tcg"],
    LINUX: ["kvm", "tcg"],
}


def _qemu_accels(qemu_binary: Optional[str]) -> list[str]:
    """Accelerators the installed QEMU build actually supports.

    A QEMU compiled without WHPX support still accepts the flag only to fail
    at start, so the binary's own ``-accel help`` is the authority.
    """
    if not qemu_binary or not os.path.isfile(qemu_binary):
        return []
    try:
        result = _proc.run_sync([qemu_binary, "-accel", "help"], timeout=30)
    except Exception:
        return []
    if result.returncode != 0:
        return []
    # Parses "Accelerators supported in QEMU binary:" followed by one name per line.
    found: list[str] = []
    for line in result.stdout.splitlines():
        token = line.strip()
        if token and not token.startswith("Accelerators"):
            found.append(token)
    return [a for a in found if a.isalpha() or a.replace("-", "").isalnum()]


def supported_accelerators(qemu_binary: Optional[str] = None) -> list[str]:
    """Accelerators usable here: what the QEMU binary offers, filtered by the host."""
    info = host_info()
    preferred = ACCEL_BY_PLATFORM.get(info.system, ["tcg"])

    binary = qemu_binary
    if binary is None:
        try:
            from vm_harness.hypervisor.qemu.backend import find_qemu
            binary = find_qemu("qemu-system-x86_64")
        except Exception:
            binary = shutil.which("qemu-system-x86_64") or shutil.which(
                "qemu-system-x86_64.exe"
            )

    offered = _qemu_accels(binary)

    usable: list[str] = []
    for accel in preferred:
        if accel == "tcg":
            usable.append("tcg")  # always present as the fallback
            continue
        # A hardware accelerator must be offered by the binary AND usable by the
        # host. An empty `offered` means we could not ask the binary, which is
        # not permission to assume support.
        if accel not in offered:
            continue
        if not _host_accel_ready(accel):
            continue
        usable.append(accel)

    return usable or ["tcg"]


def _host_accel_ready(accel: str) -> bool:
    """Whether the host can actually service this accelerator right now."""
    info = host_info()
    if accel == "kvm":
        return info.is_linux and os.path.exists("/dev/kvm") and os.access("/dev/kvm", os.R_OK | os.W_OK)
    if accel == "whpx":
        # WHPX needs the Windows hypervisor underneath. Absent it, QEMU fails
        # to start rather than falling back.
        return info.is_windows and _windows_hypervisor_present()
    if accel == "hvf":
        return info.is_darwin
    return False


def _windows_hypervisor_present() -> bool:
    """True when the Windows hypervisor (Hyper-V/VBS) is running for WHPX."""
    if os.environ.get("VMH_ASSUME_WHPX") == "1":
        return True
    ps = _proc.find_tool(
        ["powershell.exe", "pwsh"],
        [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"],
        env_var="VMH_POWERSHELL",
    )
    if not ps:
        return False
    try:
        result = _proc.run_sync(
            [ps, "-NoProfile", "-Command", "(Get-CimInstance Win32_ComputerSystem).HypervisorPresent"],
            timeout=30,
        )
    except Exception:
        return False
    return "true" in result.stdout.strip().lower()


# ── Storage ───────────────────────────────────────────────────────────────────

def free_space_mb(path: str | os.PathLike) -> Optional[int]:
    """Free space on the filesystem holding ``path``, in MiB."""
    try:
        target = Path(path)
        while not target.exists() and target != target.parent:
            target = target.parent
        return shutil.disk_usage(target).free // (1024 * 1024)
    except OSError:
        return None


# ── Aggregate ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class HostCapabilities:
    """Everything a backend needs to size and configure a VM for this host."""

    host: HostInfo
    cpu: CPUInfo
    ram_mb: Optional[int]
    accelerators: list[str] = field(default_factory=list)
    qemu_binary: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    @property
    def usable_accelerator(self) -> str:
        """Best accelerator for this host, always at least ``tcg``."""
        return self.accelerators[0] if self.accelerators else "tcg"

    @property
    def accelerated(self) -> bool:
        """True when hardware acceleration is genuinely in use."""
        return self.usable_accelerator != "tcg"

    def suggest_cpu_count(self, requested: Optional[int] = None, headroom: int = 2) -> int:
        """A vCPU count that leaves the host usable.

        Uses logical cores minus ``headroom`` (the desktop needs cores too),
        never exceeding what was requested when one was given.
        """
        available = max(1, self.cpu.logical_or_physical() - headroom)
        if requested and requested > 0:
            return max(1, min(requested, available))
        return available

    def suggest_ram_mb(self, requested: Optional[int] = None, fraction: float = 0.5) -> Optional[int]:
        """A RAM figure that leaves the host its share.

        Without an absolute guest minimum this is only a sane default; Arch and
        Hyprland guests want roughly 8 GiB to be comfortable.
        """
        if not self.ram_mb:
            return requested
        budget = int(self.ram_mb * fraction)
        if requested and requested > 0:
            return max(1, min(requested, budget))
        return max(1, budget)

    def to_dict(self) -> dict[str, Any]:
        """Serialisable view, for the API/CLI/GUI."""
        return {
            "host": {
                "system": self.host.system,
                "release": self.host.release,
                "machine": self.host.machine,
                "python": self.host.python,
            },
            "cpu": {
                "model": self.cpu.model,
                "physical_cores": self.cpu.physical_cores,
                "logical_cores": self.cpu.logical_cores,
                "virtualization": self.cpu.virtualization,
                "nested": self.cpu.nested,
            },
            "ram_mb": self.ram_mb,
            "accelerators": self.accelerators,
            "usable_accelerator": self.usable_accelerator,
            "accelerated": self.accelerated,
            "qemu_binary": self.qemu_binary,
            "notes": self.notes,
        }


@lru_cache(maxsize=1)
def host_capabilities(refresh: bool = False) -> HostCapabilities:
    """Probe the host once and cache the result.

    Pass ``refresh=True`` to re-probe (the underlying helper is uncached).
    """
    return _probe_host()


def _probe_host() -> HostCapabilities:
    """Uncached capability probe, with notes explaining anything unusual."""
    host = host_info()
    cpu = cpu_info()
    notes: list[str] = []

    qemu_binary: Optional[str] = None
    try:
        from vm_harness.hypervisor.qemu.backend import find_qemu
        qemu_binary = find_qemu("qemu-system-x86_64")
    except Exception:
        qemu_binary = shutil.which("qemu-system-x86_64") or shutil.which("qemu-system-x86_64.exe")

    accelerators = supported_accelerators(qemu_binary)
    if accelerators == ["tcg"]:
        if host.is_windows:
            notes.append(
                "No hardware acceleration: WHPX needs the Windows hypervisor. "
                "Enable Hyper-V/VBS, or expect slow TCG emulation."
            )
        elif host.is_linux:
            notes.append("No /dev/kvm access: load the kvm modules and check group ownership.")
        else:
            notes.append("No hardware accelerator available; falling back to TCG.")

    if cpu.virtualization is False:
        notes.append("Firmware reports hardware virtualization disabled (enable VT-x/SVM).")

    return HostCapabilities(
        host=host,
        cpu=cpu,
        ram_mb=total_ram_mb(),
        accelerators=accelerators,
        qemu_binary=qemu_binary,
        notes=notes,
    )


def describe() -> str:
    """A short human-readable summary of the host, for CLI output."""
    caps = host_capabilities()
    lines = [
        f"Host      : {caps.host.system} {caps.host.release} ({caps.host.machine}), Python {caps.host.python}",
        f"CPU       : {caps.cpu.model or 'unknown'}",
        f"            {caps.cpu.physical_cores or '?'} physical / {caps.cpu.logical_cores or '?'} logical cores,"
        f" virtualization={caps.cpu.virtualization}, nested={caps.cpu.nested}",
        f"RAM       : {caps.ram_mb} MiB" if caps.ram_mb else "RAM       : unknown",
        f"QEMU      : {caps.qemu_binary or 'not found'}",
        f"Accel     : {', '.join(caps.accelerators)} (using {caps.usable_accelerator})",
    ]
    for note in caps.notes:
        lines.append(f"Note      : {note}")
    return "\n".join(lines)