"""QEMU boot arguments: UEFI VMs get a variable store of their own (so boot entries persist), and the system disk
boots first unless an ISO is meant to (installing). Found when an existing Omarchy (Limine, UEFI) disk stopped at the
firmware's shell instead of booting."""
from __future__ import annotations

from pathlib import Path

from vm_harness.hypervisor.qemu import backend as qb


def _backend(tmp_path: Path, monkeypatch) -> qb.QEMUBackend:
    fw = tmp_path / "share"
    fw.mkdir()
    (fw / "edk2-x86_64-code.fd").write_bytes(b"code")
    (fw / "edk2-i386-vars.fd").write_bytes(b"vars-template")
    qemu = tmp_path / "qemu-system-x86_64.exe"
    qemu.write_bytes(b"")
    return qb.QEMUBackend({"qemu_binary": str(qemu), "qemu_img": str(qemu), "vms_dir": str(tmp_path / "vms")})


def _pairs(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


def test_uefi_gets_a_persistent_variable_store(tmp_path, monkeypatch):
    b = _backend(tmp_path, monkeypatch)
    args = b._build_qemu_args({"name": "omarchy", "boot_firmware": "uefi", "disk_path": str(tmp_path / "d.qcow2"),
                               "management_port": 4444})
    drives = _pairs(args, "-drive")
    assert any("if=pflash" in d and "readonly=on" in d and "code.fd" in d for d in drives)
    vars_file = tmp_path / "vms" / "omarchy" / "efivars.fd"
    assert vars_file.read_bytes() == b"vars-template"
    assert any("if=pflash" in d and str(vars_file) in d and "readonly" not in d for d in drives)
    # The store is the VM's own: a second start keeps what the firmware wrote.
    vars_file.write_bytes(b"boot entries")
    b._build_qemu_args({"name": "omarchy", "boot_firmware": "uefi", "management_port": 4444})
    assert vars_file.read_bytes() == b"boot entries"


def test_the_disk_boots_first_unless_installing(tmp_path, monkeypatch):
    b = _backend(tmp_path, monkeypatch)
    iso = tmp_path / "install.iso"
    iso.write_bytes(b"")
    disk = str(tmp_path / "d.qcow2")
    args = b._build_qemu_args({"name": "vm", "disk_path": disk, "iso_path": str(iso), "management_port": 4444})
    devices = _pairs(args, "-device")
    assert "virtio-blk-pci,drive=disk0,bootindex=0" in devices
    assert "ide-cd,drive=cd0,bootindex=1" in devices
    args = b._build_qemu_args({"name": "vm", "disk_path": disk, "iso_path": str(iso), "boot_order": ["cdrom", "hd"],
                               "management_port": 4444})
    devices = _pairs(args, "-device")
    assert "virtio-blk-pci,drive=disk0,bootindex=1" in devices and "ide-cd,drive=cd0,bootindex=0" in devices
    assert "-boot" not in args, "UEFI ignores -boot; bootindex decides"
