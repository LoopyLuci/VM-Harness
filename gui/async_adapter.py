"""Async Adapter Bridge — lets GUI panels call async backends synchronously.

Usage:
    from gui.async_adapter import AsyncAdapter

    adapter = AsyncAdapter()
    containers = adapter.docker.list_containers()
    adapter.docker.start_container("my-container")
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import time
import threading
from typing import Any


class AsyncAdapter:
    """Adapts async backend methods to synchronous calls for GUI usage.

    Uses a background event loop thread to run coroutines without blocking
    the Qt main thread.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._loop = None
        self._thread = None
        self._docker = None
        self._kubernetes = None
        self._podman = None
        self._qemu = None
        self._vmware = None
        self._vbox = None
        self._failures: dict = {}
        self._start_loop()

    def _start_loop(self):
        """Start the background event loop."""
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def _run_async(self, coro, timeout: float = 15):
        """Run a coroutine in the background event loop and return the result."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"backend call did not finish within {timeout:.0f}s")

    # A backend that failed to connect is not tried again for a while: the panels poll on timers, and retrying a
    # dead Docker daemon or Kubernetes cluster on every tick froze the window.
    RETRY_AFTER_S = 30.0

    def _connect(self, attr: str, make, connect_name: str = "connect"):
        failed_at, error = self._failures.get(attr, (0.0, None))
        if error is not None and time.monotonic() - failed_at < self.RETRY_AFTER_S:
            raise RuntimeError(f"{attr} unavailable (retrying in {self.RETRY_AFTER_S - (time.monotonic() - failed_at):.0f}s): {error}")
        backend = make()
        try:
            self._run_async(getattr(backend, connect_name)())
        except Exception as e:
            self._failures[attr] = (time.monotonic(), e)
            raise
        self._failures.pop(attr, None)
        setattr(self, attr, backend)
        return backend

    @property
    def docker(self) -> "_DockerAdapter":
        """Get Docker adapter."""
        if self._docker is None:
            from vm_harness.container.docker.backend import DockerBackend
            self._connect("_docker", DockerBackend, "connect")
        return _DockerAdapter(self._docker, self._run_async)

    @property
    def kubernetes(self) -> "_KubernetesAdapter":
        """Get Kubernetes adapter."""
        if self._kubernetes is None:
            from vm_harness.container.kubernetes.backend import KubernetesBackend
            self._connect("_kubernetes", KubernetesBackend, "connect")
        return _KubernetesAdapter(self._kubernetes, self._run_async)

    @property
    def podman(self) -> "_PodmanAdapter":
        """Get Podman adapter."""
        if self._podman is None:
            from vm_harness.container.podman.backend import PodmanBackend
            self._connect("_podman", PodmanBackend, "connect")
        return _PodmanAdapter(self._podman, self._run_async)

    @property
    def qemu(self) -> "_QEMUAdapter":
        """Get QEMU adapter."""
        if self._qemu is None:
            from vm_harness.hypervisor.qemu.backend import QEMUBackend
            self._connect("_qemu", QEMUBackend, "initialize")
        return _QEMUAdapter(self._qemu, self._run_async)

    @property
    def vmware(self) -> "_VMwareAdapter":
        """Get VMware adapter."""
        if self._vmware is None:
            from vm_harness.hypervisor.vmware.backend import VMwareBackend
            self._connect("_vmware", VMwareBackend, "initialize")
        return _VMwareAdapter(self._vmware, self._run_async)

    @property
    def vbox(self) -> "_VirtualBoxAdapter":
        """Get VirtualBox adapter."""
        if self._vbox is None:
            from vm_harness.hypervisor.virtualbox.backend import VirtualBoxBackend
            self._connect("_vbox", VirtualBoxBackend, "initialize")
        return _VirtualBoxAdapter(self._vbox, self._run_async)


class _DockerAdapter:
    """Synchronous adapter for Docker backend."""

    def __init__(self, backend, run_async):
        self._backend = backend
        self._run = run_async

    def list_containers(self, all: bool = True) -> list:
        containers = self._run(self._backend.list_containers(all=all))
        # Convert Container objects to dicts for GUI consumption
        result = []
        for c in containers:
            if hasattr(c, 'name'):
                result.append({
                    "name": c.name,
                    "image": str(c.image) if hasattr(c, 'image') else "",
                    "status": c.status if hasattr(c, 'status') else "",
                    "ports": str(c.ports) if hasattr(c, 'ports') else "",
                })
            elif isinstance(c, dict):
                result.append(c)
        return result

    def get_container(self, container_id: str) -> Any:
        return self._run(self._backend.get_container(container_id))

    def get_stats(self, container_id: str) -> dict:
        """Get real-time stats for a container."""
        stats = self._run(self._backend.get_stats(container_id))
        if hasattr(stats, 'to_dict'):
            return stats.to_dict()
        if isinstance(stats, dict):
            return stats
        return {"error": str(stats)}

    def get_logs(self, container_id: str, tail: int = 100) -> str:
        """Get container logs."""
        return self._run(self._backend.get_logs(container_id, tail=tail))

    def create_container(self, config: dict) -> Any:
        from vm_harness.container.backend import ContainerConfig
        if isinstance(config, dict):
            config = ContainerConfig(
                image=config.get("image", "alpine:latest"),
                command=config.get("command"),
                name=config.get("name"),
            )
        return self._run(self._backend.create_container(config))

    def get_stats(self, container_id: str) -> dict:
        stats = self._run(self._backend.get_stats(container_id))
        if hasattr(stats, 'to_dict'):
            return stats.to_dict()
        if isinstance(stats, dict):
            return stats
        return {"error": str(stats)}

    def get_logs(self, container_id: str, tail: int = 100) -> str:
        return self._run(self._backend.get_logs(container_id, tail=tail))

    def start_container(self, container_id: str) -> None:
        return self._run(self._backend.start_container(container_id))

    def stop_container(self, container_id: str, timeout: int = 10) -> None:
        return self._run(self._backend.stop_container(container_id, timeout=timeout))

    def restart_container(self, container_id: str, timeout: int = 10) -> None:
        return self._run(self._backend.restart_container(container_id, timeout=timeout))

    def remove_container(self, container_id: str, force: bool = False) -> None:
        return self._run(self._backend.remove_container(container_id, force=force))

    def get_logs(self, container_id: str) -> str:
        return self._run(self._backend.get_logs(container_id))

    def inspect_container(self, container_id: str) -> dict:
        return self._run(self._backend.inspect_container(container_id))

    def list_images(self) -> list:
        images = self._run(self._backend.list_images())
        result = []
        for img in images:
            if hasattr(img, 'tags'):
                result.append({
                    "repository": img.tags[0] if img.tags else "",
                    "tag": "",
                    "size": str(getattr(img, 'size', '')),
                    "created": str(getattr(img, 'created', '')),
                })
            elif isinstance(img, dict):
                result.append(img)
        return result

    def pull_image(self, image_name: str) -> str:
        return self._run(self._backend.pull_image(image_name))

    def remove_image(self, image_id: str, force: bool = False) -> None:
        return self._run(self._backend.remove_image(image_id, force=force))

    def exec_command(self, container_id: str, command: str) -> str:
        return self._run(self._backend.exec_command(container_id, command))


class _KubernetesAdapter:
    """Synchronous adapter for Kubernetes backend."""

    def __init__(self, backend, run_async):
        self._backend = backend
        self._run = run_async

    def list_pods(self, namespace: str = "default") -> list:
        return self._run(self._backend.list_pods(namespace=namespace))

    def list_all_pods(self) -> list:
        return self._run(self._backend.list_all_pods())

    def list_services(self, namespace: str = "default") -> list:
        return self._run(self._backend.list_services(namespace=namespace))

    def list_deployments(self, namespace: str = "default") -> list:
        return self._run(self._backend.list_deployments(namespace=namespace))

    def list_nodes(self) -> list:
        nodes = self._run(self._backend.list_nodes())
        result = []
        for n in nodes:
            if isinstance(n, dict):
                result.append(n)
            elif hasattr(n, 'metadata'):
                result.append({
                    "name": n.metadata.name,
                    "status": "Ready",
                    "age": str(n.metadata.creation_timestamp),
                })
        return result

    def list_namespaces(self) -> list:
        return self._run(self._backend.list_namespaces())

    def apply_manifest(self, manifest: dict) -> Any:
        return self._run(self._backend.apply_manifest(manifest))

    def delete_resource(self, kind: str, name: str, namespace: str = "default") -> None:
        return self._run(self._backend.delete_resource(kind, name, namespace=namespace))

    def exec_in_pod(self, pod_name: str, command: list, namespace: str = "default") -> str:
        return self._run(self._backend.exec_in_pod(pod_name, command, namespace=namespace))

    def get_pod_logs(self, pod_name: str, namespace: str = "default") -> str:
        return self._run(self._backend.get_pod_logs(pod_name, namespace=namespace))


class _PodmanAdapter:
    """Synchronous adapter for Podman backend."""

    def __init__(self, backend, run_async):
        self._backend = backend
        self._run = run_async

    def list_containers(self) -> list:
        return self._run(self._backend.list_containers())

    def create_container(self, config: Any = None, **kwargs: Any) -> Any:
        from vm_harness.container.backend import ContainerConfig
        if config is None:
            config = ContainerConfig(**kwargs)
        elif isinstance(config, dict):
            config = ContainerConfig(
                image=config.get("image", "alpine:latest"),
                command=config.get("command"),
                name=config.get("name"),
            )
        return self._run(self._backend.create_container(config))

    def start_container(self, container_id: str) -> None:
        return self._run(self._backend.start_container(container_id))

    def stop_container(self, container_id: str) -> None:
        return self._run(self._backend.stop_container(container_id))

    def remove_container(self, container_id: str) -> None:
        return self._run(self._backend.remove_container(container_id))


class _QEMUAdapter:
    """Synchronous adapter for QEMU backend."""

    def __init__(self, backend, run_async):
        self._backend = backend
        self._run = run_async

    def list_vms(self) -> list:
        vms = self._run(self._backend.list_vms())
        # list_vms() returns list[str] (names) — convert to dicts
        result = []
        for vm in vms:
            if isinstance(vm, str):
                result.append({"name": vm, "guest_os": "", "state": "unknown", "ip_address": ""})
            elif isinstance(vm, dict):
                result.append(vm)
        return result

    def create_vm(self, config: Any) -> str:
        from vm_harness.hypervisor.backend import VMConfig
        if isinstance(config, dict):
            config = VMConfig(
                name=config.get("name", "vm"),
                ram_mb=config.get("ram_mb", 2048),
                cpus=config.get("cpus", 2),
                disk_size_gb=config.get("disk_size_gb", 20),
                disk_format=config.get("disk_format", "qcow2"),
            )
        return self._run(self._backend.create_vm(config))

    def destroy_vm(self, name: str) -> None:
        return self._run(self._backend.destroy_vm(name))

    def start_vm(self, name: str, headless: bool = False) -> None:
        return self._run(self._backend.start_vm(name, headless=headless))

    def stop_vm(self, name: str, force: bool = False) -> None:
        return self._run(self._backend.stop_vm(name, force=force))

    def get_status(self, name: str) -> str:
        status = self._run(self._backend.get_status(name))
        if hasattr(status, 'state'):
            state = status.state
            if hasattr(state, 'value'):
                return state.value
            return str(state)
        if isinstance(status, str):
            return status
        return "unknown"

    def pause_vm(self, name: str) -> None:
        return self._run(self._backend.pause_vm(name))

    def resume_vm(self, name: str) -> None:
        return self._run(self._backend.resume_vm(name))

    def reset_vm(self, name: str) -> None:
        return self._run(self._backend.reset_vm(name))

    def query_status(self) -> dict:
        return self._run(self._backend.query_status())


class _VMwareAdapter:
    """Synchronous adapter for VMware backend."""

    def __init__(self, backend, run_async):
        self._backend = backend
        self._run = run_async

    def list_vms(self) -> list:
        return self._run(self._backend.list_vms())

    def power_on(self, vm_name: str) -> None:
        return self._run(self._backend.power_on(vm_name))

    def power_off(self, vm_name: str) -> None:
        return self._run(self._backend.power_off(vm_name))

    def suspend(self, vm_name: str) -> None:
        return self._run(self._backend.suspend(vm_name))

    def reset(self, vm_name: str) -> None:
        return self._run(self._backend.reset(vm_name))

    def create_snapshot(self, vm_name: str, snapshot_name: str) -> None:
        return self._run(self._backend.create_snapshot(vm_name, snapshot_name))

    def list_snapshots(self, vm_name: str) -> list:
        return self._run(self._backend.list_snapshots(vm_name))


class _VirtualBoxAdapter:
    """Synchronous adapter for VirtualBox backend."""

    def __init__(self, backend, run_async):
        self._backend = backend
        self._run = run_async

    def list_vms(self) -> list:
        return self._run(self._backend.list_vms())

    def start_vm(self, vm_name: str) -> None:
        return self._run(self._backend.start_vm(vm_name))

    def stop_vm(self, vm_name: str, force: bool = False) -> None:
        return self._run(self._backend.stop_vm(vm_name, force=force))

    def pause_vm(self, vm_name: str) -> None:
        return self._run(self._backend.pause_vm(vm_name))

    def reset_vm(self, vm_name: str) -> None:
        return self._run(self._backend.reset_vm(name=vm_name))

    def get_status(self, vm_name: str) -> str:
        status = self._run(self._backend.get_status(vm_name))
        if hasattr(status, 'state') and hasattr(status.state, 'value'):
            return status.state.value
        if hasattr(status, 'value'):
            return status.value
        if isinstance(status, str):
            return status
        return str(status)


# Global singleton
_adapter = None


def get_adapter() -> AsyncAdapter:
    """Get the global async adapter singleton."""
    global _adapter
    if _adapter is None:
        _adapter = AsyncAdapter()
    return _adapter
