"""Tests for hypervisor registry registration and platform-aware discovery.

The registry previously populated ``_backend_factories`` but never
``_backends``, so ``auto_detect()`` skipped every backend and reported an
empty result on every platform. These tests pin that behaviour so it cannot
regress.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vm_harness.hypervisor.backend import BackendNotAvailableError  # noqa: E402
from vm_harness.hypervisor.registry import HypervisorRegistry  # noqa: E402


class TestDefaultRegistration:
    def test_builtins_are_registered(self):
        """Every importable built-in backend must land in ``_backends``."""
        registry = HypervisorRegistry()
        registered = set(registry.list_backends())
        assert "qemu" in registered
        assert registered, "no built-in backends registered"

    def test_factories_match_registrations(self):
        """Each successfully imported factory is actually registered."""
        registry = HypervisorRegistry()
        for name, cls in registry._backend_factories.items():
            assert name in registry._backends, f"{name} imported but not registered"
            assert registry._backends[name][0] is cls

    def test_registration_is_idempotent(self):
        """Two registries agree, so detection does not depend on prior state."""
        assert HypervisorRegistry().list_backends() == HypervisorRegistry().list_backends()

    def test_register_duplicate_requires_override(self):
        registry = HypervisorRegistry()
        with pytest.raises(ValueError):
            registry.register("qemu", registry._backend_factories["qemu"])


class TestAutoDetect:
    @pytest.mark.asyncio
    async def test_detects_every_registered_backend(self):
        """auto_detect must probe everything registered, not skip it."""
        registry = HypervisorRegistry()
        detected = await registry.auto_detect()
        assert set(detected) == set(registry.list_backends())

    @pytest.mark.asyncio
    async def test_detected_values_are_booleans(self):
        registry = HypervisorRegistry()
        detected = await registry.auto_detect()
        assert all(isinstance(v, bool) for v in detected.values())

    @pytest.mark.asyncio
    async def test_get_best_backend_before_detect_raises(self):
        """Asking for a backend without probing must fail loudly, not guess."""
        registry = HypervisorRegistry()
        with pytest.raises(BackendNotAvailableError):
            registry.get_best_backend()

    @pytest.mark.asyncio
    async def test_detect_order_matches_registered(self):
        """The per-OS detect order must only name registered backends."""
        registry = HypervisorRegistry()
        order = registry._get_detect_order()
        assert set(order) <= set(registry.list_backends())

    @pytest.mark.asyncio
    async def test_available_is_subset_of_detected(self):
        registry = HypervisorRegistry()
        await registry.auto_detect()
        assert set(registry.list_available()) <= set(registry.list_backends())


class TestPlatformAwareness:
    def test_windows_only_backends_are_not_available_elsewhere(self, monkeypatch):
        """WSL must not be offered on a non-Windows host.

        ``os.name`` is not patched here: it is a process-wide global that
        pathlib consults, so mutating it corrupts unrelated code (including
        pytest's own reporting) while the test runs. The Linux/macOS detect
        orders are asserted directly instead.
        """
        import vm_harness.hypervisor.registry as registry_mod

        for system in ("Linux", "Darwin"):
            monkeypatch.setattr(registry_mod.platform, "system", lambda s=system: s)
            order = HypervisorRegistry()._get_detect_order()
            assert "wsl" not in order, f"{system} should not offer the WSL backend"
            assert "hyperv" not in order, f"{system} should not offer the Hyper-V backend"

    def test_windows_offers_windows_only_backends(self, monkeypatch):
        """Windows must offer WSL and Hyper-V."""
        import vm_harness.hypervisor.registry as registry_mod

        monkeypatch.setattr(registry_mod.platform, "system", lambda: "Windows")
        order = HypervisorRegistry()._get_detect_order()
        assert "wsl" in order
        assert "hyperv" in order

    def test_linux_prefers_kvm_first(self, monkeypatch):
        """On Linux, hardware-accelerated KVM must be probed before QEMU."""
        import vm_harness.hypervisor.registry as registry_mod

        monkeypatch.setattr(registry_mod.platform, "system", lambda: "Linux")
        order = HypervisorRegistry()._get_detect_order()
        assert order[0] == "kvm"

    def test_kvm_requires_linux_and_dev_kvm(self, monkeypatch):
        """KVM must not claim availability on Windows."""
        import vm_harness.hypervisor.kvm.backend as kvm

        monkeypatch.setattr(kvm.platform, "system", lambda: "Windows")
        assert kvm.KVMBackend().is_available is False

    def test_qemu_found_via_path_when_install_dir_absent(self, monkeypatch):
        """QEMU on PATH must be discovered without the Windows install dir."""
        import vm_harness.hypervisor.qemu.backend as qemu

        monkeypatch.delenv("VMH_QEMU_DIR", raising=False)

        def fake_find_tool(names, candidates=(), env_var=None):
            return "/usr/bin/qemu-system-x86_64" if "qemu-system-x86_64" in names else None

        monkeypatch.setattr(qemu._proc, "find_tool", fake_find_tool)
        found = qemu.find_qemu("qemu-system-x86_64")
        assert found == "/usr/bin/qemu-system-x86_64"

    def test_virtualbox_found_via_path(self, monkeypatch):
        """VirtualBox must be discoverable from PATH, not just fixed paths."""
        import vm_harness.hypervisor.virtualbox.backend as vbox

        def fake_find_tool(names, candidates=(), env_var=None):
            return "/usr/bin/VBoxManage" if "VBoxManage" in names else None

        monkeypatch.setattr(vbox._proc, "find_tool", fake_find_tool)
        assert vbox.find_vboxmanage() == "/usr/bin/VBoxManage"

    def test_vmware_found_via_path(self, monkeypatch):
        import vm_harness.hypervisor.vmware.backend as vmw

        def fake_find_tool(names, candidates=(), env_var=None):
            return "/opt/vmware/bin/vmrun" if "vmrun" in names else None

        monkeypatch.setattr(vmw._proc, "find_tool", fake_find_tool)
        assert vmw.find_vmrun() == "/opt/vmware/bin/vmrun"