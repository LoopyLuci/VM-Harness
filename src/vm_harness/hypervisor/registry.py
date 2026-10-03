"""HypervisorRegistry — auto-detect and register available hypervisor backends.

The registry scans the host system for installed hypervisors (QEMU, VMware,
VirtualBox, WSL, Hyper-V, KVM) and selects the best available backend.
It also supports explicit registration and backend selection.

Usage:
    registry = HypervisorRegistry()
    await registry.auto_detect()
    backend = registry.get_best_backend()
    async with backend:
        await backend.start_vm("my-vm")
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import platform
import shutil
import subprocess
from typing import Any, Type

from vm_harness.hypervisor.backend import (
    BackendNotAvailableError,
    HypervisorBackend,
)

logger = logging.getLogger(__name__)


# ── Default binary paths ──────────────────────────────────────────────────────

DEFAULT_QEMU_PATH = r"C:\Program Files\qemu\qemu-system-x86_64.exe"
DEFAULT_QEMU_IMG_PATH = r"C:\Program Files\qemu\qemu-img.exe"
DEFAULT_VMWARE_PATH = r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe"
DEFAULT_VBOX_PATH = r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
DEFAULT_WSL_PATH = r"C:\Windows\System32\wsl.exe"
DEFAULT_POWERSHELL_PATH = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
DEFAULT_KVM_PATH = "/usr/bin/qemu-system-x86_64"
DEFAULT_VIRSH_PATH = "/usr/bin/virsh"


# ── Registry ──────────────────────────────────────────────────────────────────

class HypervisorRegistry:
    """Registry of available hypervisor backends.

    The registry holds a mapping of backend names to (backend_class, config) tuples.
    Call ``auto_detect()`` to scan the host for available backends, then use
    ``get_best_backend()`` to get the highest-priority backend for the current host.

    Backends are prioritized as follows:
    1. QEMU (if explicitly configured or binary found)
    2. VMware (if vmrun is found)
    3. VirtualBox (if VBoxManage is found)
    4. WSL (if wsl.exe is found — Windows only)
    5. Hyper-V (if PowerShell and Hyper-V module found — Windows only)
    6. KVM (if /dev/kvm exists — Linux only)
    """

    def __init__(self, extra_backends: dict[str, Type[HypervisorBackend]] | None = None) -> None:
        """Initialize an empty registry.

        Args:
            extra_backends: Dict mapping backend names to backend classes
                           to register in addition to the built-in ones.
        """
        self._backends: dict[str, tuple[Type[HypervisorBackend], dict[str, Any]]] = {}
        self._explicit_backend: str | None = None
        self._detected: dict[str, bool] = {}

        # Register built-in backends
        self._register_defaults()

        # Register extra backends
        if extra_backends:
            for name, cls in extra_backends.items():
                self.register(name, cls)

    def _register_defaults(self) -> None:
        """Register the built-in backends with lazy imports to avoid hard deps.

        Imports are attempted one at a time and each successfully imported
        class is registered immediately, so a backend whose module fails to
        import on this platform never prevents the others from registering.
        """
        # We use lazy imports so each backend module only loads when needed.
        # This avoids importing platform-specific modules on the wrong OS.
        self._backend_factories: dict[str, Type[HypervisorBackend]] = {}

        candidates = (
            ("qemu", "vm_harness.hypervisor.qemu.backend", "QEMUBackend"),
            ("vmware", "vm_harness.hypervisor.vmware.backend", "VMwareBackend"),
            ("virtualbox", "vm_harness.hypervisor.virtualbox.backend", "VirtualBoxBackend"),
            ("wsl", "vm_harness.hypervisor.wsl.backend", "WSLBackend"),
            ("hyperv", "vm_harness.hypervisor.hyperv.backend", "HyperVBackend"),
            ("kvm", "vm_harness.hypervisor.kvm.backend", "KVMBackend"),
        )

        for name, module_path, class_name in candidates:
            try:
                module = importlib.import_module(module_path)
                cls = getattr(module, class_name)
            except (ImportError, AttributeError) as e:
                logger.debug("Backend '%s' not importable: %s", name, e)
                continue
            self._backend_factories[name] = cls
            self.register(name, cls)

    # ── Public API ───────────────────────────────────────────────────────────

    def register(
        self,
        name: str,
        backend_class: Type[HypervisorBackend],
        config: dict[str, Any] | None = None,
        *,
        override: bool = False,
    ) -> None:
        """Register a backend class.

        Args:
            name: Unique backend name (e.g. "qemu", "vmware").
            backend_class: Backend class (must subclass HypervisorBackend).
            config: Optional default config passed to the backend constructor.
            override: If True, replace an existing registration.

        Raises:
            ValueError: If name is already registered and override is False.
        """
        if name in self._backends and not override:
            raise ValueError(f"Backend '{name}' is already registered. Use override=True to replace.")
        self._backends[name] = (backend_class, config or {})

    def unregister(self, name: str) -> None:
        """Remove a registered backend."""
        self._backends.pop(name, None)
        self._explicit_backend = None if self._explicit_backend == name else self._explicit_backend

    def set_preferred(self, name: str) -> None:
        """Explicitly set the preferred backend name.

        When set, ``get_best_backend()`` returns this backend if it's available.
        """
        if name not in self._backends:
            raise ValueError(f"Backend '{name}' is not registered")
        self._explicit_backend = name

    def list_backends(self) -> list[str]:
        """List names of all registered backends."""
        return list(self._backends.keys())

    def list_available(self) -> list[str]:
        """List names of backends that passed auto-detect."""
        return [name for name, detected in self._detected.items() if detected]

    async def auto_detect(self, preferred: str | None = None) -> dict[str, bool]:
        """Scan the host for all registered backends.

        For each registered backend, checks whether its prerequisites are met
        (binary installed, daemon running, etc.) and updates the registry.

        Args:
            preferred: If set, short-circuit after confirming this backend.

        Returns:
            Dict mapping backend name to availability (True/False).
        """
        self._detected = {}

        # Priority order for auto-detect (OS-aware)
        detect_order = self._get_detect_order()

        for name in detect_order:
            if name not in self._backends:
                continue
            cls, _ = self._backends[name]
            try:
                backend = cls()
                detected = await backend.probe_availability()
            except Exception as e:
                logger.debug("Backend '%s' probe failed: %s", name, e)
                detected = False
            self._detected[name] = detected

            if preferred and name == preferred and detected:
                # Found the preferred backend — stop scanning
                break

        return self._detected

    def _get_detect_order(self) -> list[str]:
        """Get the order to scan backends based on the current platform."""
        system = platform.system().lower()

        if system == "windows":
            return ["qemu", "vmware", "virtualbox", "hyperv", "wsl", "kvm"]
        elif system == "linux":
            return ["kvm", "qemu", "virtualbox", "vmware"]
        elif system == "darwin":
            return ["qemu", "virtualbox", "vmware"]
        else:
            return list(self._backends.keys())

    def get_best_backend(self) -> HypervisorBackend:
        """Get the highest-priority available backend.

        Priority order:
        1. Explicitly preferred backend (set via set_preferred()).
        2. QEMU (if available).
        3. VMware (if available).
        4. VirtualBox (if available).
        5. WSL (Windows only).
        6. Hyper-V (Windows only).
        7. KVM (Linux only).

        Returns:
            An initialized HypervisorBackend instance.

        Raises:
            BackendNotAvailableError: If no backend is available.
        """
        # 1. Check explicit preference
        if self._explicit_backend:
            return self.get_backend(self._explicit_backend)

        # 2. Auto-detect if not done yet
        if not self._detected:
            raise BackendNotAvailableError(
                "No auto_detect() call has been made yet. "
                "Call auto_detect() before get_best_backend()."
            )

        # 3. Priority order
        priority = ["qemu", "vmware", "virtualbox", "hyperv", "wsl", "kvm"]
        for name in priority:
            if self._detected.get(name) and name in self._backends:
                return self.get_backend(name)

        raise BackendNotAvailableError(
            "No hypervisor backend is available on this system. "
            "Available backends: " + ", ".join(self.list_available()) or "none"
        )

    def get_backend(self, name: str | None = None, config: dict[str, Any] | None = None) -> HypervisorBackend:
        """Get a specific backend by name, or the best available if name is None.

        Args:
            name: Backend name (e.g. "qemu"). None for auto-select.
            config: Optional config to override the registered config.

        Returns:
            A HypervisorBackend instance (not yet initialized).

        Raises:
            ValueError: If name is not registered.
        """
        if name is None:
            return self.get_best_backend()

        if name not in self._backends:
            raise ValueError(
                f"Backend '{name}' is not registered. "
                f"Registered: {list(self._backends.keys())}"
            )

        cls, default_config = self._backends[name]
        merged_config = {**default_config, **(config or {})}
        return cls(merged_config)

    def get_backend_class(self, name: str) -> Type[HypervisorBackend]:
        """Get the backend class for a given name.

        Raises:
            ValueError: If name is not registered.
        """
        if name not in self._backends:
            raise ValueError(f"Backend '{name}' is not registered")
        return self._backends[name][0]

    def info(self) -> dict[str, Any]:
        """Get registry info as a dict."""
        return {
            "registered": list(self._backends.keys()),
            "detected": self._detected,
            "preferred": self._explicit_backend,
            "platform": platform.system(),
            "available": self.list_available(),
        }


# ── Convenience functions ──────────────────────────────────────────────────────

async def auto_detect(preferred: str | None = None) -> HypervisorBackend:
    """Auto-detect and return the best available backend.

    Convenience wrapper around HypervisorRegistry.

    Args:
        preferred: If set, prefer this backend if available.

    Returns:
        An initialized HypervisorBackend instance.
    """
    registry = HypervisorRegistry()
    await registry.auto_detect(preferred=preferred)
    backend = registry.get_best_backend()
    await backend.initialize()
    return backend


def get_backend(name: str, config: dict[str, Any] | None = None) -> HypervisorBackend:
    """Get a specific backend by name.

    Args:
        name: Backend name (e.g. "qemu", "vmware").
        config: Optional backend-specific config.

    Returns:
        A HypervisorBackend instance (not yet initialized).
    """
    registry = HypervisorRegistry()
    return registry.get_backend(name, config)


# ── Module-level default registry ──────────────────────────────────────────────

_default_registry: HypervisorRegistry | None = None


def get_default_registry() -> HypervisorRegistry:
    """Get or create the default module-level registry."""
    global _default_registry
    if _default_registry is None:
        _default_registry = HypervisorRegistry()
    return _default_registry
