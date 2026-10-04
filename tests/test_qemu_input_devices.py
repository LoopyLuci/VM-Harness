"""Every QEMU guest gets an absolute pointing device and an explicit keyboard.

QMP ``input-send-event`` with ``type: "abs"`` is only meaningful for a device that declares
absolute axes. Without ``-device usb-tablet`` QEMU falls back to a relative PS/2 mouse, drops
the absolute events without reporting an error, and the remote pointer simply never moves --
remote mouse control fails with no error anywhere. Keystrokes worked only through QEMU's
implicit, machine-created PS/2 keyboard, which is undocumented and cannot be addressed.

Both devices are configurable (``usb_tablet`` / ``virtio_keyboard``, default on) because a few
guests and drivers dislike a tablet, and both command-line builders emit them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gui.multi_vm import (
    KEYBOARD_DEVICE_ID as GUI_KEYBOARD_DEVICE_ID,
)
from gui.multi_vm import (
    KEYBOARD_MODEL as GUI_KEYBOARD_MODEL,
)
from gui.multi_vm import (
    TABLET_DEVICE_ID as GUI_TABLET_DEVICE_ID,
)
from gui.multi_vm import (
    TABLET_MODEL as GUI_TABLET_MODEL,
)
from gui.multi_vm import MultiVMManager, VMConfig
from vm_harness.hypervisor.qemu import backend as qb


# ── Helpers ────────────────────────────────────────────────────────────────────


def _devices(args: list[str]) -> list[str]:
    """The values of every ``-device`` flag on the command line."""
    return [args[i + 1] for i, a in enumerate(args) if a == "-device"]


def _core_backend(tmp_path: Path) -> qb.QEMUBackend:
    """A backend that points at a stub QEMU; nothing is launched, args are only built."""
    qemu = tmp_path / "qemu-system-x86_64.exe"
    qemu.write_bytes(b"")
    return qb.QEMUBackend({"qemu_binary": str(qemu), "qemu_img": str(qemu), "vms_dir": str(tmp_path / "vms")})


def _core_args(tmp_path: Path, **config: object) -> list[str]:
    return _core_backend(tmp_path)._build_qemu_args({"name": "vm", "management_port": 4444, **config})


@pytest.fixture()
def gui_manager(monkeypatch, tmp_path):
    """A MultiVMManager that persists into tmp_path instead of the user's home."""
    import gui.multi_vm as multi_vm

    monkeypatch.setattr(multi_vm, "VM_CONFIGS_DIR", tmp_path / "vm-configs")
    (tmp_path / "vm-configs").mkdir(parents=True, exist_ok=True)
    return MultiVMManager()


def _gui_config(**overrides: object) -> VMConfig:
    defaults: dict = {
        "disk_path": "C:/vms/test.qcow2",
        "ram_mb": 4096,
        "cpus": 2,
        "qmp_port": 4444,
        "ssh_port": 2222,
        "display": "sdl",
    }
    defaults.update(overrides)
    return VMConfig("gui-vm", defaults)


# ── Core backend (src/vm_harness/hypervisor/qemu/backend.py) ────────────────────


def test_core_guest_gets_a_usb_tablet_with_a_stable_id(tmp_path):
    args = _core_args(tmp_path)
    devices = _devices(args)
    assert f"{qb.TABLET_MODEL},id={qb.TABLET_DEVICE_ID}" in devices
    assert qb.TABLET_DEVICE_ID == "tablet0", "the streaming bridge pins this id"
    # q35 has no USB controller on its PCI bus: without -usb before it QEMU refuses to
    # start with "No 'usb-bus' bus found for device 'usb-tablet'".
    assert args.index("-usb") < args.index(f"{qb.TABLET_MODEL},id={qb.TABLET_DEVICE_ID}")


def test_core_no_usb_flag_when_the_tablet_is_disabled(tmp_path):
    """-usb only exists to carry the tablet; without it the flag would be dead weight."""
    assert "-usb" not in _core_args(tmp_path, usb_tablet=False)


def test_core_usb_flag_not_added_when_extra_args_already_has_a_tablet(tmp_path):
    args = _core_args(tmp_path, extra_args=["-device", "usb-tablet,bus=usb1.0,id=mytable"])
    assert "-usb" not in args, "the hand-written tablet brings its own controller"


def test_core_guest_gets_an_explicit_keyboard(tmp_path):
    devices = _devices(_core_args(tmp_path))
    assert f"{qb.KEYBOARD_MODEL},id={qb.KEYBOARD_DEVICE_ID}" in devices


def test_core_guest_gets_the_input_devices_when_headless(tmp_path):
    """A headless VM is the streaming-bridge case: abs events there need the tablet most."""
    devices = _devices(_core_args(tmp_path, display_type="headless"))
    assert f"{qb.TABLET_MODEL},id={qb.TABLET_DEVICE_ID}" in devices
    assert f"{qb.KEYBOARD_MODEL},id={qb.KEYBOARD_DEVICE_ID}" in devices


def test_core_tablet_can_be_disabled(tmp_path):
    devices = _devices(_core_args(tmp_path, usb_tablet=False))
    assert not any(d.startswith(qb.TABLET_MODEL) for d in devices)
    assert f"{qb.KEYBOARD_MODEL},id={qb.KEYBOARD_DEVICE_ID}" in devices, "only the tablet was disabled"


def test_core_keyboard_can_be_disabled(tmp_path):
    devices = _devices(_core_args(tmp_path, virtio_keyboard=False))
    assert not any(d.startswith(qb.KEYBOARD_MODEL) for d in devices)
    assert f"{qb.TABLET_MODEL},id={qb.TABLET_DEVICE_ID}" in devices, "only the keyboard was disabled"


def test_core_both_input_devices_can_be_disabled(tmp_path):
    devices = _devices(_core_args(tmp_path, usb_tablet=False, virtio_keyboard=False))
    assert not any(d.startswith((qb.TABLET_MODEL, qb.KEYBOARD_MODEL)) for d in devices)


@pytest.mark.parametrize(
    "extra",
    [
        ["-device", "usb-tablet,id=mytable"],
        ["-device=usb-tablet,id=mytable"],
    ],
)
def test_core_does_not_duplicate_a_hand_written_tablet(tmp_path, extra):
    """extra_args is the escape hatch: a user who already passes one gets exactly one."""
    args = _core_args(tmp_path, extra_args=extra)
    # Both "-device x" and "-device=x" spellings count, so the command line is scanned whole.
    assert len([a for a in args if qb.TABLET_MODEL in a]) == 1
    assert f"{qb.KEYBOARD_MODEL},id={qb.KEYBOARD_DEVICE_ID}" in _devices(args)


def test_core_does_not_duplicate_a_hand_written_keyboard(tmp_path):
    args = _core_args(tmp_path, extra_args=["-device", "usb-kbd,id=mykbd"])
    assert [d for d in _devices(args) if d.startswith("usb-kbd")] == ["usb-kbd,id=mykbd"]


def test_core_extra_args_for_an_unrelated_device_are_untouched(tmp_path):
    args = _core_args(tmp_path, extra_args=["-device", "virtio-serial-pci,id=mine"])
    devices = _devices(args)
    assert "virtio-serial-pci,id=mine" in devices
    assert f"{qb.TABLET_MODEL},id={qb.TABLET_DEVICE_ID}" in devices


# ── GUI builder (gui/multi_vm.py) ───────────────────────────────────────────────


def test_gui_guest_gets_a_usb_tablet_with_a_stable_id(gui_manager):
    args = gui_manager._build_qemu_args(_gui_config())
    devices = _devices(args)
    assert f"{GUI_TABLET_MODEL},id={GUI_TABLET_DEVICE_ID}" in devices
    assert GUI_TABLET_DEVICE_ID == qb.TABLET_DEVICE_ID, "both builders must agree on the id"
    # q35 has no USB controller on its PCI bus: without -usb before it QEMU refuses to
    # start with "No 'usb-bus' bus found for device 'usb-tablet'".
    assert args.index("-usb") < args.index(f"{GUI_TABLET_MODEL},id={GUI_TABLET_DEVICE_ID}")


def test_gui_guest_gets_an_explicit_keyboard(gui_manager):
    devices = _devices(gui_manager._build_qemu_args(_gui_config()))
    assert f"{GUI_KEYBOARD_MODEL},id={GUI_KEYBOARD_DEVICE_ID}" in devices


def test_gui_input_devices_can_be_disabled(gui_manager):
    args = gui_manager._build_qemu_args(_gui_config(usb_tablet=False, virtio_keyboard=False))
    devices = _devices(args)
    assert not any(d.startswith((GUI_TABLET_MODEL, GUI_KEYBOARD_MODEL)) for d in devices)
    assert "-usb" not in args


def test_gui_tablet_and_keyboard_are_independently_disableable(gui_manager):
    devices = _devices(gui_manager._build_qemu_args(_gui_config(usb_tablet=False)))
    assert not any(d.startswith(GUI_TABLET_MODEL) for d in devices)
    assert f"{GUI_KEYBOARD_MODEL},id={GUI_KEYBOARD_DEVICE_ID}" in devices

    devices = _devices(gui_manager._build_qemu_args(_gui_config(virtio_keyboard=False)))
    assert not any(d.startswith(GUI_KEYBOARD_MODEL) for d in devices)
    assert f"{GUI_TABLET_MODEL},id={GUI_TABLET_DEVICE_ID}" in devices


def test_gui_does_not_duplicate_a_hand_written_tablet(gui_manager):
    args = gui_manager._build_qemu_args(_gui_config(extra_args=["-device", "usb-tablet,id=mytable"]))
    assert [d for d in _devices(args) if d.startswith(GUI_TABLET_MODEL)] == ["usb-tablet,id=mytable"]


def test_gui_spice_display_no_longer_passes_display_spice(gui_manager):
    """QEMU rejects "-display spice": "Parameter 'type' does not accept value 'spice'."""
    args = gui_manager._build_qemu_args(_gui_config(display="spice"))
    displays = [args[i + 1] for i, a in enumerate(args) if a == "-display"]
    assert "spice" not in displays
    assert displays == ["none"]
    assert any(a == "-spice" for a in args)


def test_gui_input_devices_survive_a_config_save_and_reload(gui_manager):
    """to_dict()/from_dict() round trip: a field missing from to_dict() is lost silently,
    and the VM would come back with the tablet the user explicitly turned off."""
    original = _gui_config(usb_tablet=False, virtio_keyboard=False)
    assert original.to_dict()["usb_tablet"] is False
    assert original.to_dict()["virtio_keyboard"] is False

    reloaded = VMConfig.from_dict("gui-vm", original.to_dict())
    assert reloaded.usb_tablet is False
    assert reloaded.virtio_keyboard is False

    # Same defaults, still on: the round trip must not turn them off either.
    defaults = _gui_config()
    back = VMConfig.from_dict("gui-vm", defaults.to_dict())
    assert back.usb_tablet is True
    assert back.virtio_keyboard is True


def test_gui_input_devices_are_on_by_default_for_a_bare_config():
    bare = VMConfig("bare")
    assert bare.usb_tablet is True
    assert bare.virtio_keyboard is True
    assert bare.to_dict()["usb_tablet"] is True
    assert bare.to_dict()["virtio_keyboard"] is True


def test_gui_config_round_trip_keeps_the_flags_off_in_the_built_args(gui_manager):
    """End to end: save with the devices off, reload, and the args still respect that."""
    saved = _gui_config(usb_tablet=False, virtio_keyboard=False).to_dict()
    reloaded = VMConfig.from_dict("gui-vm", saved)
    devices = _devices(gui_manager._build_qemu_args(reloaded))
    assert not any(d.startswith((GUI_TABLET_MODEL, GUI_KEYBOARD_MODEL)) for d in devices)


def test_gui_add_vm_persists_the_input_device_flags(gui_manager, tmp_path):
    """The manager writes to_dict() to disk, so a saved flag must reappear on reload."""
    disk = tmp_path / "saved.qcow2"
    disk.write_bytes(b"")
    success, msg = gui_manager.add_vm("saved-vm", {
        "disk_path": str(disk),
        "usb_tablet": False,
        "virtio_keyboard": True,
    })
    assert success, msg

    reloaded = MultiVMManager()
    config = reloaded.get_config("saved-vm")
    assert config is not None
    assert config["usb_tablet"] is False
    assert config["virtio_keyboard"] is True