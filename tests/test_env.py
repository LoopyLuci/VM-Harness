"""Tests for host environment probing.

The point of ``vm_harness.env`` is that a backend never guesses about the
machine it runs on. These tests use fakes rather than the real host so the
suite behaves identically on Windows, Linux and macOS.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vm_harness import env  # noqa: E402


class FakeCompleted:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


@pytest.fixture(autouse=True)
def _clear_cache():
    env.host_capabilities.cache_clear()
    yield
    env.host_capabilities.cache_clear()


def _host(monkeypatch, system: str, **attrs) -> object:
    """Force host_info() to report a given platform."""
    defaults = dict(
        release="test",
        machine="x86_64",
        python="3.11.0",
        is_windows=(system == "Windows"),
        is_linux=(system == "Linux"),
        is_darwin=(system == "Darwin"),
    )
    defaults.update(attrs)
    info = env.HostInfo(system=system, **defaults)
    monkeypatch.setattr(env, "host_info", lambda: info)
    return info


class TestHostInfo:
    def test_platform_flags(self):
        info = env.HostInfo("Linux", "6.1", "x86_64", "3.11", False, True, False)
        assert info.is_linux and not info.is_windows and info.is_posix

    def test_windows_is_not_posix(self):
        info = env.HostInfo("Windows", "10", "AMD64", "3.11", True, False, False)
        assert not info.is_posix


class TestCPUInfo:
    def test_falls_back_to_logical_cores(self):
        cpu = env.CPUInfo("x", physical_cores=None, logical_cores=8, virtualization=True)
        assert cpu.logical_or_physical() == 8

    def test_prefers_logical_when_known(self):
        """Hyperthreaded hosts want logical cores for vCPU sizing."""
        cpu = env.CPUInfo("x", physical_cores=6, logical_cores=12, virtualization=True)
        assert cpu.logical_or_physical() == 12

    def test_never_reports_zero_cores(self):
        cpu = env.CPUInfo("x", physical_cores=None, logical_cores=None, virtualization=False)
        assert cpu.logical_or_physical() == 1

    def test_unknown_virtualization_stays_none(self, monkeypatch):
        """An unknown answer must stay None, not silently become False."""
        monkeypatch.setattr(env, "cpu_info", lambda: env.CPUInfo("x", 4, 8, None))
        assert env.cpu_info().virtualization is None


class TestAccelerators:
    def test_always_includes_tcg(self, monkeypatch):
        monkeypatch.setattr(env, "_qemu_accels", lambda b: [])
        monkeypatch.setattr(env, "_host_accel_ready", lambda a: False)
        _host(monkeypatch, "Linux")
        assert env.supported_accelerators("qemu") == ["tcg"]

    def test_kvm_chosen_on_linux_when_dev_kvm_present(self, monkeypatch):
        monkeypatch.setattr(env, "_qemu_accels", lambda b: ["kvm", "tcg"])
        monkeypatch.setattr(env, "_host_accel_ready", lambda a: a == "kvm")
        _host(monkeypatch, "Linux")
        assert env.supported_accelerators("qemu")[0] == "kvm"

    def test_kvm_never_chosen_on_windows(self, monkeypatch):
        monkeypatch.setattr(env, "_qemu_accels", lambda b: ["kvm", "tcg"])
        monkeypatch.setattr(env, "_host_accel_ready", lambda a: True)
        _host(monkeypatch, "Windows")
        accels = env.supported_accelerators("qemu")
        assert "kvm" not in accels

    def test_whpx_not_used_when_host_refuses(self, monkeypatch):
        """WHPX offered by the binary is not enough; the host must allow it."""
        monkeypatch.setattr(env, "_qemu_accels", lambda b: ["whpx", "tcg"])
        monkeypatch.setattr(env, "_host_accel_ready", lambda a: False)
        _host(monkeypatch, "Windows")
        assert env.supported_accelerators("qemu") == ["tcg"]

    def test_accel_not_in_binary_is_dropped(self, monkeypatch):
        """A QEMU build without WHPX must not be passed the flag."""
        monkeypatch.setattr(env, "_qemu_accels", lambda b: ["tcg"])
        monkeypatch.setattr(env, "_host_accel_ready", lambda a: True)
        _host(monkeypatch, "Windows")
        assert "whpx" not in env.supported_accelerators("qemu")

    def test_hvf_on_darwin(self, monkeypatch):
        monkeypatch.setattr(env, "_qemu_accels", lambda b: ["hvf", "tcg"])
        monkeypatch.setattr(env, "_host_accel_ready", lambda a: True)
        _host(monkeypatch, "Darwin")
        assert env.supported_accelerators("qemu")[0] == "hvf"

    def test_missing_binary_yields_tcg_only(self, monkeypatch):
        """No QEMU at all means TCG only, not a crash."""
        import vm_harness.hypervisor.qemu.backend as qemu_backend

        # env imports shutil and find_qemu into its own namespace, so patch
        # the reference it actually uses rather than the stdlib module.
        monkeypatch.setattr(qemu_backend, "find_qemu", lambda *a, **k: None)
        monkeypatch.setattr(env.shutil, "which", lambda *a, **k: None)
        _host(monkeypatch, "Windows")
        assert env.supported_accelerators(None) == ["tcg"]

    def test_parses_accel_help_output(self, monkeypatch):
        text = "Accelerators supported in QEMU binary:\ntcg\nwhpx\n"
        monkeypatch.setattr(env.os.path, "isfile", lambda p: True)
        monkeypatch.setattr(
            env._proc, "run_sync", lambda *a, **k: FakeCompleted(text)
        )
        assert env._qemu_accels("/bin/qemu") == ["tcg", "whpx"]

    def test_failed_probe_yields_no_accels(self, monkeypatch):
        monkeypatch.setattr(env.os.path, "isfile", lambda p: True)
        monkeypatch.setattr(
            env._proc, "run_sync", lambda *a, **k: FakeCompleted("", 1)
        )
        assert env._qemu_accels("/bin/qemu") == []

    def test_probe_exception_is_swallowed(self, monkeypatch):
        monkeypatch.setattr(env.os.path, "isfile", lambda p: True)

        def boom(*a, **k):
            raise OSError("nope")

        monkeypatch.setattr(env._proc, "run_sync", boom)
        assert env._qemu_accels("/bin/qemu") == []

    def test_missing_binary_is_not_probed(self, monkeypatch):
        """A binary that isn't there must short-circuit before running it."""
        def fail(*a, **k):
            raise AssertionError("must not invoke a missing binary")

        monkeypatch.setattr(env.os.path, "isfile", lambda p: False)
        monkeypatch.setattr(env._proc, "run_sync", fail)
        assert env._qemu_accels("/bin/qemu") == []

    def test_kvm_requires_dev_kvm(self, monkeypatch):
        _host(monkeypatch, "Linux")
        monkeypatch.setattr(env.os.path, "exists", lambda p: False)
        assert env._host_accel_ready("kvm") is False

    def test_unknown_accel_is_not_ready(self, monkeypatch):
        assert env._host_accel_ready("nvmm") is False


class TestHostAccelReadyWindows:
    def test_whpx_requires_hypervisor_present(self, monkeypatch):
        _host(monkeypatch, "Windows")
        monkeypatch.setattr(env, "_windows_hypervisor_present", lambda: False)
        assert env._host_accel_ready("whpx") is False

    def test_whpx_ok_when_hypervisor_present(self, monkeypatch):
        _host(monkeypatch, "Windows")
        monkeypatch.setattr(env, "_windows_hypervisor_present", lambda: True)
        assert env._host_accel_ready("whpx") is True

    def test_override_env_forces_true(self, monkeypatch):
        _host(monkeypatch, "Windows")
        monkeypatch.setenv("VMH_ASSUME_WHPX", "1")
        assert env._windows_hypervisor_present() is True

    def test_parses_cim_true(self, monkeypatch):
        monkeypatch.delenv("VMH_ASSUME_WHPX", raising=False)
        monkeypatch.setattr(
            env._proc, "find_tool", lambda *a, **k: "powershell.exe"
        )
        monkeypatch.setattr(
            env._proc, "run_sync", lambda *a, **k: FakeCompleted("True\n")
        )
        assert env._windows_hypervisor_present() is True

    def test_parses_cim_false(self, monkeypatch):
        monkeypatch.delenv("VMH_ASSUME_WHPX", raising=False)
        monkeypatch.setattr(
            env._proc, "find_tool", lambda *a, **k: "powershell.exe"
        )
        monkeypatch.setattr(
            env._proc, "run_sync", lambda *a, **k: FakeCompleted("False\n")
        )
        assert env._windows_hypervisor_present() is False


class TestSizing:
    def _caps(self, logical=12, ram=32768):
        return env.HostCapabilities(
            host=_host_info_stub(),
            cpu=env.CPUInfo("test", 6, logical, True),
            ram_mb=ram,
            accelerators=["whpx", "tcg"],
        )

    def test_cpu_leaves_host_headroom(self):
        caps = self._caps(logical=12)
        assert caps.suggest_cpu_count() == 10

    def test_cpu_never_exceeds_request(self):
        caps = self._caps(logical=12)
        assert caps.suggest_cpu_count(6) == 6

    def test_cpu_clamps_oversized_request(self):
        caps = self._caps(logical=4)
        assert caps.suggest_cpu_count(64) == 2

    def test_cpu_never_returns_zero(self):
        caps = self._caps(logical=1)
        assert caps.suggest_cpu_count(8) >= 1

    def test_ram_caps_to_fraction(self):
        caps = self._caps(ram=32768)
        assert caps.suggest_ram_mb(65536) == 16384

    def test_ram_honours_smaller_request(self):
        caps = self._caps(ram=32768)
        assert caps.suggest_ram_mb(8192) == 8192

    def test_ram_falls_back_when_unknown(self):
        caps = self._caps(ram=None)
        assert caps.suggest_ram_mb(8192) == 8192


class TestCapabilities:
    def test_accelerated_flag(self):
        caps = env.HostCapabilities(
            host=_host_info_stub(),
            cpu=env.CPUInfo("x", 6, 12, True),
            ram_mb=32768,
            accelerators=["whpx", "tcg"],
        )
        assert caps.accelerated and caps.usable_accelerator == "whpx"

    def test_tcg_only_is_not_accelerated(self):
        caps = env.HostCapabilities(
            host=_host_info_stub(),
            cpu=env.CPUInfo("x", 6, 12, True),
            ram_mb=32768,
            accelerators=["tcg"],
        )
        assert not caps.accelerated

    def test_to_dict_is_serialisable(self):
        import json

        caps = env.HostCapabilities(
            host=_host_info_stub(),
            cpu=env.CPUInfo("x", 6, 12, True, False),
            ram_mb=32768,
            accelerators=["kvm", "tcg"],
        )
        payload = caps.to_dict()
        json.dumps(payload)  # must not raise
        assert payload["usable_accelerator"] == "kvm"
        assert payload["cpu"]["logical_cores"] == 12

    def test_notes_explain_missing_acceleration(self, monkeypatch):
        monkeypatch.setattr(env, "_qemu_accels", lambda b: [])
        monkeypatch.setattr(env, "_host_accel_ready", lambda a: False)
        monkeypatch.setattr(env, "cpu_info", lambda: env.CPUInfo("x", 6, 12, True))
        monkeypatch.setattr(env, "total_ram_mb", lambda: 32768)
        _host(monkeypatch, "Linux")
        caps = env.host_capabilities()
        assert any("kvm" in n for n in caps.notes)

    def test_describe_mentions_accelerator(self, monkeypatch):
        monkeypatch.setattr(env, "_qemu_accels", lambda b: ["whpx", "tcg"])
        monkeypatch.setattr(env, "_host_accel_ready", lambda a: a == "whpx")
        monkeypatch.setattr(env, "cpu_info", lambda: env.CPUInfo("x", 6, 12, True))
        monkeypatch.setattr(env, "total_ram_mb", lambda: 32768)
        _host(monkeypatch, "Windows")
        out = env.describe()
        assert "whpx" in out
        assert "Host" in out


class TestStorage:
    def test_free_space_positive(self, tmp_path):
        free = env.free_space_mb(tmp_path)
        assert free is not None and free > 0

    def test_free_space_on_missing_path_walks_up(self, tmp_path):
        assert env.free_space_mb(tmp_path / "nope" / "deeper") is not None

    def test_free_space_bad_path_returns_none(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("no")
        monkeypatch.setattr(env.shutil, "disk_usage", boom)
        assert env.free_space_mb("/definitely/not/here") is None


def _host_info_stub():
    return env.HostInfo("Windows", "10", "AMD64", "3.11", True, False, False)