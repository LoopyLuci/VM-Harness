"""The engine: the one place that owns the hypervisor and container backends for a VM-Harness process.

Backends are created on first use and kept; one that is not installed on this machine is reported as unavailable
(with the reason) instead of failing everything else. Per-backend settings live in ``<home>/backends.json``:

    {"hyperv": {"guest_username": "admin"}, "qemu": {"vms_dir": "E:/VMs/qemu"}, "docker": {"base_url": "..."}}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
from pathlib import Path
from typing import Any, Optional

from vm_harness.control.catalog import OperationError

log = logging.getLogger("vmharness.engine")

HYPERVISORS = ("qemu", "virtualbox", "vmware", "hyperv", "wsl", "kvm")
CONTAINER_ENGINES = ("docker", "podman")


def home() -> Path:
    """Where VM-Harness keeps its state: $VMH_HOME, else ~/.vmharness."""
    p = Path(os.environ.get("VMH_HOME") or Path.home() / ".vmharness")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _hypervisor_class(name: str):
    if name == "qemu":
        from vm_harness.hypervisor.qemu.backend import QEMUBackend as C
    elif name == "virtualbox":
        from vm_harness.hypervisor.virtualbox.backend import VirtualBoxBackend as C
    elif name == "vmware":
        from vm_harness.hypervisor.vmware.backend import VMwareBackend as C
    elif name == "hyperv":
        from vm_harness.hypervisor.hyperv.backend import HyperVBackend as C
    elif name == "wsl":
        from vm_harness.hypervisor.wsl.backend import WSLBackend as C
    elif name == "kvm":
        from vm_harness.hypervisor.kvm.backend import KVMBackend as C
    else:
        raise OperationError(f"unknown hypervisor {name!r}; one of {', '.join(HYPERVISORS)}", code="bad_request")
    return C


class Engine:
    def __init__(self, config_path: Optional[Path] = None) -> None:
        self.config_path = config_path or home() / "backends.json"
        self._hv: dict[str, Any] = {}
        self._hv_errors: dict[str, str] = {}
        self._containers: dict[str, Any] = {}
        self._k8s: Any = None
        self._lock = asyncio.Lock()

    # ---- settings --------------------------------------------------------------------------------------------------
    def settings(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_settings(self, backend: str, values: dict[str, Any]) -> dict[str, Any]:
        data = self.settings()
        current = dict(data.get(backend) or {})
        for k, v in values.items():
            if v is None:
                current.pop(k, None)
            else:
                current[k] = v
        data[backend] = current
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.config_path)
        # The backend is rebuilt with the new settings on next use.
        self._hv.pop(backend, None)
        self._hv_errors.pop(backend, None)
        self._containers.pop(backend, None)
        if backend == "kubernetes":
            self._k8s = None
        return current

    # ---- hypervisors ----------------------------------------------------------------------------------------------
    async def hypervisor(self, name: str) -> Any:
        if name in self._hv:
            return self._hv[name]
        async with self._lock:
            if name in self._hv:
                return self._hv[name]
            cls = _hypervisor_class(name)
            backend = cls(self.settings().get(name) or {})
            # Probing and initializing run host tools synchronously (is_available is a property), so they get a
            # worker thread and a loop of their own: nothing here keeps loop-bound state.
            try:
                available = await asyncio.to_thread(lambda: asyncio.run(backend.probe_availability()))
            except Exception as e:  # noqa: BLE001
                available, why = False, f"{type(e).__name__}: {e}"
            else:
                why = "" if available else f"{backend.display_name} is not installed or not enabled on this machine"
            if not available:
                self._hv_errors[name] = why
                raise OperationError(why, code="unavailable", status=409)
            await asyncio.to_thread(lambda: asyncio.run(backend.initialize()))
            self._hv[name] = backend
            self._hv_errors.pop(name, None)
            return backend

    async def hypervisors(self) -> dict[str, dict[str, Any]]:
        """Every hypervisor this build knows, whether it is usable here, and why not."""
        async def one(name: str) -> tuple[str, dict[str, Any]]:
            try:
                b = await self.hypervisor(name)
                return name, {"available": True, "display_name": b.display_name,
                              "version": await asyncio.to_thread(lambda: b.version),
                              "features": sorted(b.supported_features)}
            except OperationError as e:
                return name, {"available": False, "reason": str(e)}
            except Exception as e:  # noqa: BLE001
                return name, {"available": False, "reason": f"{type(e).__name__}: {e}"}
        return dict(await asyncio.gather(*(one(n) for n in HYPERVISORS)))

    async def available_hypervisors(self) -> list[str]:
        return [n for n, info in (await self.hypervisors()).items() if info["available"]]

    async def locate(self, vm: str, backend: str = "") -> tuple[str, Any]:
        """(backend name, backend) holding VM `vm`: the named backend, else the first available one that has it."""
        if backend:
            return backend, await self.hypervisor(backend)
        for name in await self.available_hypervisors():
            b = await self.hypervisor(name)
            try:
                if vm in await b.list_vms():
                    return name, b
            except Exception as e:  # noqa: BLE001
                log.debug("list_vms on %s failed: %s", name, e)
        raise OperationError(f"no VM named {vm!r} on any hypervisor here; pass backend= to create one",
                             code="not_found", status=404)

    # ---- containers ----------------------------------------------------------------------------------------------
    async def container(self, engine: str = "docker") -> Any:
        if engine not in CONTAINER_ENGINES:
            raise OperationError(f"unknown container engine {engine!r}; one of {', '.join(CONTAINER_ENGINES)}",
                                 code="bad_request")
        if engine in self._containers:
            return self._containers[engine]
        cfg = self.settings().get(engine) or {}
        if engine == "docker":
            from vm_harness.container.docker.backend import DockerBackend as C
        else:
            from vm_harness.container.podman.backend import PodmanBackend as C
        try:
            backend = C(**cfg) if cfg else C()
            await asyncio.wait_for(backend.connect(), 20)
        except Exception as e:  # noqa: BLE001
            raise OperationError(f"{engine} is not reachable: {type(e).__name__}: {e}", code="unavailable", status=409)
        self._containers[engine] = backend
        return backend

    async def kubernetes(self) -> Any:
        if self._k8s is not None:
            return self._k8s
        from vm_harness.container.kubernetes.backend import KubernetesBackend
        cfg = self.settings().get("kubernetes") or {}
        backend = KubernetesBackend(kubeconfig=cfg.get("kubeconfig"), context=cfg.get("context"))
        try:
            await backend.connect()
        except Exception as e:  # noqa: BLE001
            raise OperationError(str(e), code="unavailable", status=409)
        self._k8s = backend
        return backend

    async def container_engines(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for engine in CONTAINER_ENGINES:
            try:
                await self.container(engine)
                out[engine] = {"available": True}
            except OperationError as e:
                out[engine] = {"available": False, "reason": str(e)}
        try:
            await self.kubernetes()
            out["kubernetes"] = {"available": True}
        except OperationError as e:
            out["kubernetes"] = {"available": False, "reason": str(e)}
        return out

    def forget_kubernetes(self) -> None:
        self._k8s = None

    # ---- lifecycle -------------------------------------------------------------------------------------------------
    async def close(self) -> None:
        for b in list(self._containers.values()):
            try:
                await b.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if self._k8s is not None:
            try:
                await self._k8s.disconnect()
            except Exception:  # noqa: BLE001
                pass
        # Hypervisor backends are left alone: VMs keep running when VM-Harness stops (QEMU VMs are detached).
        self._containers.clear()
        self._k8s = None


def host_info() -> dict[str, Any]:
    import psutil
    vm = psutil.virtual_memory()
    info: dict[str, Any] = {
        "os": platform.platform(), "machine": platform.machine(), "hostname": platform.node(),
        "python": platform.python_version(), "cpus_logical": psutil.cpu_count(), "cpus_physical": psutil.cpu_count(False),
        "cpu_percent": psutil.cpu_percent(interval=0.2), "ram_total_mb": vm.total // 2**20,
        "ram_available_mb": vm.available // 2**20, "home": str(home()),
    }
    if os.name == "nt":
        try:
            import ctypes
            info["virtualization_firmware_enabled"] = bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(21))
        except Exception:  # noqa: BLE001
            pass
    elif Path("/dev/kvm").exists():
        info["kvm"] = os.access("/dev/kvm", os.R_OK | os.W_OK)
    return info
