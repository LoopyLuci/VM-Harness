"""Hyper-V Backend — implements HypervisorBackend using PowerShell cmdlets.

Supports Microsoft Hyper-V on Windows (client and server). Uses PowerShell
to invoke Hyper-V cmdlets for all VM operations.

Config keys:
    powershell_path: Path to powershell.exe
    default_vhd_path: Default directory for VHD/VHDX files
    default_switch: Default virtual switch name
    default_ram_mb: Default RAM allocation
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
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

DEFAULT_POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def ps_quote(value: Any) -> str:
    """A PowerShell single-quoted string literal: a name with a quote in it can neither break nor extend a command."""
    return "'" + str(value).replace("'", "''") + "'"


def _ps_args(command: str) -> list[str]:
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; " + command]


def _run_ps_sync(command: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return _proc.run_sync(_ps_args(command), timeout=timeout)


async def _run_ps(command: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a PowerShell command off the event loop. A failing cmdlet raises HypervisorError with its message."""
    result = await _proc.run(_ps_args(command), timeout=timeout)
    if result.returncode != 0:
        text = (result.stderr or result.stdout).strip()
        message = text.splitlines()[0][:500] if text else f"PowerShell exited {result.returncode}"
        if "unable to find a virtual machine" in text:
            raise VMNotFoundError(message)
        raise HypervisorError(message)
    return result


# ── HyperVBackend ──────────────────────────────────────────────────────────────

HYPERV_STATES = {
    "Off": VMState.STOPPED, "Running": VMState.RUNNING, "Paused": VMState.PAUSED, "Saved": VMState.SUSPENDED,
    "FastSaved": VMState.SUSPENDED, "Starting": VMState.STARTING, "Stopping": VMState.STOPPING,
    "Saving": VMState.STOPPING, "FastSaving": VMState.STOPPING, "Pausing": VMState.PAUSED,
    "Resuming": VMState.STARTING, "Reset": VMState.STARTING, "Other": VMState.UNKNOWN,
}


class HyperVBackend(HypervisorBackend):
    """Microsoft Hyper-V backend using PowerShell cmdlets.

    Manages Hyper-V VMs through PowerShell. Requires Windows Pro/Enterprise/Education
    with Hyper-V enabled.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._powershell_path = self._config.get("powershell_path", DEFAULT_POWERSHELL)
        self._default_vhd_path = self._config.get("default_vhd_path", str(Path.home() / "Hyper-V" / "Virtual Hard Disks"))
        self._default_switch = self._config.get("default_switch", "Default Switch")
        self._vms_dir = Path(self._config.get("vms_dir", str(Path.home() / ".qemu-mcp" / "hyperv-vms")))
        self._vms_dir.mkdir(parents=True, exist_ok=True)

    @property
    def default_name(self) -> str:
        return "hyperv"

    @property
    def display_name(self) -> str:
        return "Microsoft Hyper-V"

    @property
    def version(self) -> str:
        try:
            result = _run_ps_sync("(Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V).State")
            if "Enabled" in result.stdout:
                return "hyperv-enabled"
        except Exception:
            pass
        return "unknown"

    @property
    def is_available(self) -> bool:
        if os.name != "nt":
            return False
        try:
            result = _run_ps_sync("Get-Command Get-VM -ErrorAction SilentlyContinue")
            return result.returncode == 0 and "Get-VM" in result.stdout
        except Exception:
            return False

    @property
    def supported_features(self) -> set[str]:
        return {
            "create", "destroy", "start", "stop", "pause", "resume",
            "reset", "reboot", "status", "list", "snapshots",
            "rdp", "console", "exec", "network", "import", "export",
            "metrics", "set_resource_limits",
        }

    # ── Initialization ───────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Initialize the Hyper-V backend."""
        if not self.is_available:
            raise BackendNotAvailableError(
                "Hyper-V is not available. Ensure Windows Pro/Enterprise with Hyper-V enabled."
            )
        os.makedirs(self._default_vhd_path, exist_ok=True)
        await super().initialize()

    async def shutdown(self) -> None:
        """Clean up resources."""
        await super().shutdown()

    # ── Discovery ────────────────────────────────────────────────────────────

    async def list_vms(self) -> list[str]:
        """List all Hyper-V VMs."""
        result = await _run_ps("Get-VM | Select-Object -ExpandProperty Name")
        if result.returncode == 0:
            return [line.strip() for line in result.stdout.split("\n") if line.strip()]
        return []

    # ── VM Lifecycle ─────────────────────────────────────────────────────────

    async def create_vm(self, config: VMConfig) -> str:
        """Create a new Hyper-V VM."""
        if await self.find_vm(config.name):
            raise VMAlreadyRunningError(f"VM {ps_quote(config.name)} already exists")

        vm_dir = Path(self._default_vhd_path) / config.name
        vm_dir.mkdir(parents=True, exist_ok=True)
        vhdx_path = str(vm_dir / f"{config.name}.vhdx")

        # Create VHDX
        if config.disk_size_gb > 0:
            ps_cmd = f"New-VHD -Path {ps_quote(vhdx_path)} -SizeBytes {config.disk_size_gb}GB -Dynamic"
            await _run_ps(ps_cmd)

        # Create VM
        ps_cmd = (
            f"New-VM -Name {ps_quote(config.name)} "
            f"-MemoryStartupBytes {config.ram_mb}MB "
            f"-Generation 2 "
            f"-NewVHDPath {ps_quote(vhdx_path)} "
            f"-NewVHDSizeBytes {config.disk_size_gb * 1024 * 1024 * 1024} "
            f"-SwitchName {ps_quote(self._default_switch)}"
        )
        await _run_ps(ps_cmd)

        # Configure CPUs
        if config.cpus > 1:
            await _run_ps(f"Set-VMProcessor -VMName {ps_quote(config.name)} -Count {config.cpus}")

        # Configure firmware
        if config.boot_firmware == "uefi":
            await _run_ps(f"Set-VMFirmware -VMName {ps_quote(config.name)} -EnableSecureBoot On")
        else:
            await _run_ps(f"Set-VMFirmware -VMName {ps_quote(config.name)} -EnableSecureBoot Off")

        # Mount ISO
        if config.iso_path:
            ps_cmd = f"Add-VMDvdDrive -VMName {ps_quote(config.name)} -Path {ps_quote(config.iso_path)}"
            await _run_ps(ps_cmd)

        # Save metadata
        vm_config = {
            "name": config.name,
            "description": config.description,
            "vhdx_path": vhdx_path,
            "ram_mb": config.ram_mb,
            "cpus": config.cpus,
            "switch": self._default_switch,
            "created_at": datetime.now().isoformat(),
        }
        config_path = self._vms_dir / f"{config.name}.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(vm_config, f, indent=2)

        logger.info("Created Hyper-V VM '%s'", config.name)
        return config.name

    async def destroy_vm(self, name: str) -> None:
        """Delete a Hyper-V VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM {ps_quote(name)} not found")

        # Stop if running
        try:
            await _run_ps(f"Stop-VM -Name {ps_quote(name)} -TurnOff -Force", timeout=30)
        except Exception:
            pass

        await _run_ps(f"Remove-VM -Name {ps_quote(name)} -Force", timeout=30)

        # Remove metadata
        config_path = self._vms_dir / f"{name}.json"
        if config_path.exists():
            config_path.unlink()

        logger.info("Destroyed Hyper-V VM '%s'", name)

    async def start_vm(self, name: str, headless: bool = False) -> None:
        """Start a Hyper-V VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM {ps_quote(name)} not found")

        await _run_ps(f"Start-VM -Name {ps_quote(name)}", timeout=30)
        logger.info("Started Hyper-V VM '%s'", name)

    async def stop_vm(self, name: str, force: bool = False) -> None:
        """Stop a Hyper-V VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM {ps_quote(name)} not found")

        if force:
            await _run_ps(f"Stop-VM -Name {ps_quote(name)} -TurnOff -Force", timeout=30)
        else:
            await _run_ps(f"Stop-VM -Name {ps_quote(name)} -Save", timeout=30)
        logger.info("Stopped Hyper-V VM '%s'", name)

    async def pause_vm(self, name: str) -> None:
        """Pause a Hyper-V VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM {ps_quote(name)} not found")
        await _run_ps(f"Suspend-VM -Name {ps_quote(name)}", timeout=30)

    async def resume_vm(self, name: str) -> None:
        """Resume a paused Hyper-V VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM {ps_quote(name)} not found")
        await _run_ps(f"Resume-VM -Name {ps_quote(name)}", timeout=30)

    async def reset_vm(self, name: str) -> None:
        """Reset a Hyper-V VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM {ps_quote(name)} not found")
        await _run_ps(f"Restart-VM -Name {ps_quote(name)} -Force", timeout=30)

    async def reboot_vm(self, name: str, graceful: bool = True) -> None:
        """Reboot a Hyper-V VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM {ps_quote(name)} not found")
        await _run_ps(f"Restart-VM -Name {ps_quote(name)} -Force", timeout=30)

    async def shutdown_guest(self, name: str, timeout: int = 30) -> None:
        """Gracefully shut down the Hyper-V guest."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM {ps_quote(name)} not found")
        await _run_ps(f"Stop-VM -Name {ps_quote(name)} -Force", timeout=timeout)

    # ── Status / Info ────────────────────────────────────────────────────────

    async def get_status(self, name: str) -> VMStatus:
        """Get the status of a Hyper-V VM (state as its name, so it never depends on the enum's numbering)."""
        result = await _run_ps(
            f"Get-VM -Name {ps_quote(name)} | Select-Object Name, @{{n='State';e={{$_.State.ToString()}}}}, "
            f"@{{n='Status';e={{[string]$_.Status}}}}, CPUUsage, MemoryAssigned, MemoryStartup, ProcessorCount, "
            f"@{{n='UptimeSeconds';e={{[int]$_.Uptime.TotalSeconds}}}} | ConvertTo-Json -Compress"
        )
        status = VMStatus(name=name, backend_name=self.default_name)
        try:
            vm_data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return status
        status.state = HYPERV_STATES.get(str(vm_data.get("State", "")), VMState.UNKNOWN)
        status.last_error = "" if vm_data.get("Status") in ("Operating normally", "", None) else str(vm_data.get("Status"))
        status.cpu_usage_pct = float(vm_data.get("CPUUsage") or 0)
        status.ram_usage_mb = int(vm_data.get("MemoryAssigned") or 0) // (1024 * 1024)
        status.ram_allocated_mb = int(vm_data.get("MemoryStartup") or 0) // (1024 * 1024)
        status.cpus_allocated = int(vm_data.get("ProcessorCount") or 0)
        status.uptime_seconds = int(vm_data.get("UptimeSeconds") or 0)
        return status

    async def get_config(self, name: str) -> VMConfig | None:
        """Get the configuration of a Hyper-V VM."""
        if not await self.find_vm(name):
            return None

        result = await _run_ps(
            f"Get-VM -Name {ps_quote(name)} | Select-Object Name, ProcessorCount, "
            f"MemoryStartup, MemoryMinimum, MemoryMaximum | ConvertTo-Json"
        )
        if result.returncode == 0:
            try:
                vm_data = json.loads(result.stdout)
                return VMConfig(
                    name=name,
                    ram_mb=vm_data.get("MemoryStartup", 0) // (1024 * 1024),
                    cpus=vm_data.get("ProcessorCount", 1),
                )
            except json.JSONDecodeError:
                pass
        return None

    # ── Display / Console ────────────────────────────────────────────────────

    async def get_display(self, name: str) -> VMDisplay:
        """Get display connection details for Hyper-V."""
        display = VMDisplay()
        display.display_type = VMDisplayType.RDP
        display.host = "127.0.0.1"
        display.port = 3389
        display.uri = "hyperv-vmconnect"
        return display

    async def get_console(self, name: str) -> VMConsole:
        """Get console access for Hyper-V."""
        console = VMConsole()
        console.protocol = "hyperv-vmconnect"
        console.command = f"vmconnect localhost {name}"
        return console

    # ── Guest Agent / Exec ───────────────────────────────────────────────────

    async def guest_exec(self, name: str, command: str,
                         args: list[str] | None = None,
                         env: dict[str, str] | None = None,
                         timeout: int = 30,
                         capture_output: bool = True) -> dict[str, Any]:
        """Run a command in a Windows guest through PowerShell Direct (no network needed).

        Needs the guest's credentials: ``guest_username`` / ``guest_password`` in the backend config. The command and
        its arguments are passed as data, never pasted into the script, so they cannot change what runs."""
        user = self._config.get("guest_username")
        password = self._config.get("guest_password")
        if not user or password is None:
            raise OperationNotSupportedError("PowerShell Direct needs guest_username and guest_password for this VM")
        payload = json.dumps({"command": command, "args": list(args or []), "env": dict(env or {})})
        script = (
            f"$p = ConvertFrom-Json {ps_quote(payload)}; "
            f"$cred = New-Object PSCredential({ps_quote(user)}, (ConvertTo-SecureString {ps_quote(password)} -AsPlainText -Force)); "
            f"$r = Invoke-Command -VMName {ps_quote(name)} -Credential $cred -ArgumentList $p -ScriptBlock {{ "
            f"param($p); foreach ($k in $p.env.PSObject.Properties) {{ Set-Item -Path ('env:' + $k.Name) -Value $k.Value }}; "
            f"$out = & $p.command @($p.args) 2>&1 | Out-String; @{{ exit = $LASTEXITCODE; out = $out }} }}; "
            f"$r | ConvertTo-Json -Compress"
        )
        try:
            result = await _run_ps(script, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"exit_code": -1, "stdout": "", "stderr": "timed out", "timed_out": True}
        data = json.loads(result.stdout or "{}")
        return {"exit_code": int(data.get("exit") or 0), "stdout": data.get("out") or "", "stderr": "", "timed_out": False}

    async def guest_info(self, name: str) -> VMGuestInfo:
        """Get guest info via PowerShell Direct."""
        info = VMGuestInfo()

        result = await _run_ps(
            f"Get-VM -Name {ps_quote(name)} | Select-Object NetworkAdapters | "
            f"ForEach-Object {{ $_.NetworkAdapters.IPAddresses }}"
        )
        if result.returncode == 0:
            info.ip_addresses = [{"ip": ip, "family": "ipv4"} for ip in result.stdout.split() if ip]

        return info

    # ── Snapshots ────────────────────────────────────────────────────────────

    async def list_snapshots(self, name: str) -> list[VMSnapshot]:
        """List all snapshots for a Hyper-V VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM {ps_quote(name)} not found")

        result = await _run_ps(
            f"Get-VMSnapshot -VMName {ps_quote(name)} | Select-Object Name, @{{n='CreationTime';e={{$_.CreationTime.ToString('o')}}}}, ParentSnapshotName | ConvertTo-Json"
        )
        snapshots = []
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                if isinstance(data, dict):
                    data = [data]
                for snap in data:
                    snapshots.append(VMSnapshot(
                        name=snap.get("Name", ""),
                        created_at=snap.get("CreationTime", ""),
                        parent=snap.get("ParentSnapshotName", ""),
                    ))
            except json.JSONDecodeError:
                pass
        return snapshots

    async def create_snapshot(self, name: str, snapshot_name: str,
                              description: str = "",
                              include_memory: bool = False) -> VMSnapshot:
        """Create a Hyper-V snapshot."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM {ps_quote(name)} not found")

        await _run_ps(f"Checkpoint-VM -Name {ps_quote(name)} -SnapshotName {ps_quote(snapshot_name)}", timeout=60)
        return VMSnapshot(
            name=snapshot_name,
            description=description,
            created_at=datetime.now().isoformat(),
        )

    async def restore_snapshot(self, name: str, snapshot_name: str) -> None:
        """Restore a Hyper-V snapshot."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM {ps_quote(name)} not found")
        await _run_ps(f"Restore-VMSnapshot -VMName {ps_quote(name)} -Name {ps_quote(snapshot_name)} -Confirm:$false", timeout=60)

    async def delete_snapshot(self, name: str, snapshot_name: str) -> None:
        """Delete a Hyper-V snapshot."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM {ps_quote(name)} not found")
        await _run_ps(f"Remove-VMSnapshot -VMName {ps_quote(name)} -Name {ps_quote(snapshot_name)} -Confirm:$false", timeout=60)

    # ── Network ──────────────────────────────────────────────────────────────

    async def list_network_interfaces(self, name: str) -> list[VMNetwork]:
        """List network interfaces for a Hyper-V VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM {ps_quote(name)} not found")

        result = await _run_ps(
            f"Get-VMNetworkAdapter -VMName {ps_quote(name)} | Select-Object Name, "
            f"MacAddress, SwitchName, IPAddresses, Status | ConvertTo-Json"
        )
        interfaces = []
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                if isinstance(data, dict):
                    data = [data]
                for nic in data:
                    interfaces.append(VMNetwork(
                        name=nic.get("Name", ""),
                        mac_address=nic.get("MacAddress", ""),
                        mode=VMNetworkMode.NAT,
                        ip_address=nic.get("IPAddresses", [""])[0] if nic.get("IPAddresses") else "",
                        connected=nic.get("Status") == "Ok",
                        adapter_type="synthetic",
                    ))
            except (json.JSONDecodeError, TypeError):
                pass
        return interfaces

    # ── Import / Export ─────────────────────────────────────────────────────

    async def export_vm(self, name: str, output_path: str,
                        format: str = "qcow2") -> None:
        """Export a Hyper-V VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM {ps_quote(name)} not found")
        await _run_ps(f"Export-VM -Name {ps_quote(name)} -Path {ps_quote(output_path)}", timeout=120)

    async def import_vm(self, input_path: str,
                        new_name: str | None = None) -> str:
        """Import a Hyper-V VM."""
        name_param = f"-VMName {ps_quote(new_name)}" if new_name else ""
        await _run_ps(f"Import-VM -Path {ps_quote(input_path)} {name_param}", timeout=120)
        return new_name or "imported-vm"

    # ── Resource Limits ─────────────────────────────────────────────────────

    async def set_resource_limits(self, name: str,
                                 max_ram_mb: int = 0,
                                 max_cpus: int = 0,
                                 cpu_shares: int = 0,
                                 io_bandwidth_mbps: int = 0) -> None:
        """Set resource limits for a Hyper-V VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM {ps_quote(name)} not found")

        if max_ram_mb > 0:
            await _run_ps(
                f"Set-VMMemory -VMName {ps_quote(name)} -MaximumBytes {max_ram_mb}MB"
            )
        if max_cpus > 0:
            await _run_ps(f"Set-VMProcessor -VMName {ps_quote(name)} -Count {max_cpus}")
        if cpu_shares > 0:
            await _run_ps(f"Set-VMProcessor -VMName {ps_quote(name)} -RelativeWeight {cpu_shares}")

    # ── Metrics ──────────────────────────────────────────────────────────────

    async def get_metrics(self, name: str) -> VMMetrics:
        """Get real-time metrics for a Hyper-V VM."""
        result = await _run_ps(
            f"Get-VM -Name {ps_quote(name)} | Select-Object CPUUsage, MemoryAssigned, "
            f"MemoryDemand, Uptime | ConvertTo-Json"
        )
        metrics = VMMetrics(timestamp=datetime.now().isoformat())
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                metrics.cpu_usage_pct = data.get("CPUUsage", 0)
                metrics.ram_usage_mb = data.get("MemoryDemand", 0) // (1024 * 1024)
                metrics.ram_available_mb = data.get("MemoryAssigned", 0) // (1024 * 1024)
            except json.JSONDecodeError:
                pass
        return metrics

    # ── Cloning ──────────────────────────────────────────────────────────────

    async def clone_vm(self, name: str, new_name: str,
                       linked: bool = False,
                       snapshots: bool = False) -> str:
        """Clone a Hyper-V VM."""
        if not await self.find_vm(name):
            raise VMNotFoundError(f"VM {ps_quote(name)} not found")

        # Export, then import a copy with a new id under the new name
        export_path = Path(self._default_vhd_path) / "exports" / new_name
        export_path.mkdir(parents=True, exist_ok=True)
        await self.export_vm(name, str(export_path))
        await _run_ps(
            f"$vmcx = Get-ChildItem -Path {ps_quote(str(export_path))} -Recurse -Filter *.vmcx | Select-Object -First 1; "
            f"$vm = Import-VM -Path $vmcx.FullName -Copy -GenerateNewId "
            f"-VirtualMachinePath {ps_quote(str(Path(self._default_vhd_path) / new_name))} "
            f"-VhdDestinationPath {ps_quote(str(Path(self._default_vhd_path) / new_name))}; "
            f"Rename-VM -VM $vm -NewName {ps_quote(new_name)}",
            timeout=1800,
        )
        return new_name
