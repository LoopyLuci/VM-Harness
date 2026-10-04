"""Tests for VMSwitcherPanel.

The selection-preservation test is the important one. refresh() runs on a 5s
timer and rebuilds the list from scratch, and QListWidget.clear() destroys the
current item. Before this was fixed, selecting a VM and then waiting a moment
made every action button -- Switch To, Start, Stop, Remove, Clone -- report
"Select a VM first", and the details pane went blank. It looked like the buttons
were intermittently broken rather than like a timer eating the selection.
"""
from __future__ import annotations

import pytest
from PyQt5.QtWidgets import QApplication


@pytest.fixture
def app():
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    return instance


class _StubManager:
    """Stands in for MultiVMManager without touching the filesystem or VMs."""

    def __init__(self, names=("alpha", "beta", "gamma")):
        self._names = list(names)
        self.poll_count = 0

    # -- API used by the panel -------------------------------------------
    def list_vms(self):
        return list(self._names)

    def get_vm(self, name):
        class _Limits:
            max_ram_mb = 4096
            max_cpus = 2
            priority = 5
            max_disk_gb = 64

        class _Cfg:
            ram_mb = 4096
            cpus = 2
            resource_limits = _Limits()

        return _Cfg()

    def is_running(self, name):
        return False

    def get_status(self, name):
        return "stopped"

    def get_qmp_uri(self, name):
        return f"tcp://127.0.0.1:4444"

    def get_ssh_uri(self, name):
        return f"vmuser@127.0.0.1:2222"

    def poll_status(self):
        self.poll_count += 1

    def cleanup_exited(self):
        pass


@pytest.fixture
def switcher(qtbot, app, monkeypatch):
    import gui.panels_vm_switcher as module

    stub = _StubManager()
    monkeypatch.setattr(module, "MultiVMManager", lambda: stub)
    panel = module.VMSwitcherPanel()
    qtbot.addWidget(panel)
    # Replace the manager with the stub and stop the 5s timer from firing
    # mid-test; the tests drive refresh() explicitly.
    panel._manager = stub
    panel._timer.stop()
    panel.refresh()
    return panel


def _select(panel, name):
    for i in range(panel._vm_list.count()):
        item = panel._vm_list.item(i)
        if item.data(0x0100) == name or item.text().strip() == name:
            panel._vm_list.setCurrentItem(item)
            return True
    return False


class TestSelectionSurvivesRefresh:
    def test_refresh_keeps_the_selected_vm(self, switcher):
        assert _select(switcher, "beta"), "could not select beta"
        assert switcher._get_selected_name() == "beta"

        for _ in range(3):
            switcher.refresh()

        assert (
            switcher._get_selected_name() == "beta"
        ), "refresh() dropped the selection; every action button would then refuse"

    def test_refresh_keeps_the_details_populated(self, switcher):
        _select(switcher, "gamma")
        assert switcher._detail_name.text() == "gamma"

        switcher.refresh()

        assert switcher._detail_name.text() == "gamma", (
            "details pane went blank after refresh even though the VM is still there"
        )

    def test_switch_to_works_after_a_refresh(self, switcher, monkeypatch):
        """The user-visible symptom: Switch To must not claim nothing is selected."""
        from PyQt5.QtWidgets import QMessageBox

        warnings: list[str] = []
        monkeypatch.setattr(
            QMessageBox, "warning", lambda *a, **k: warnings.append(a[-1])
        )
        _select(switcher, "alpha")
        switcher.refresh()

        switcher._switch_vm()

        assert not warnings, f"Switch To refused after refresh: {warnings}"
        assert switcher._active_vm == "alpha"

    def test_selection_is_dropped_when_the_vm_disappears(self, switcher):
        """A VM removed elsewhere must not leave a phantom selection."""
        _select(switcher, "beta")
        switcher._manager._names.remove("beta")

        switcher.refresh()

        assert switcher._get_selected_name() is None
        assert switcher._detail_name.text() != "beta", (
            "details still name a VM that is no longer in the list"
        )

    def test_no_selection_stays_unselected(self, switcher):
        switcher.refresh()
        assert switcher._get_selected_name() is None


class TestSwitchTo:
    def test_emits_vm_changed(self, switcher):
        seen: list[str] = []
        switcher.vm_changed.connect(seen.append)
        _select(switcher, "gamma")

        switcher._switch_vm()

        assert seen == ["gamma"]
        assert switcher._active_label.text() == "gamma"

    def test_warns_when_nothing_selected(self, switcher, monkeypatch):
        from PyQt5.QtWidgets import QMessageBox

        warnings: list[str] = []
        monkeypatch.setattr(
            QMessageBox, "warning", lambda *a, **k: warnings.append(a[-1])
        )
        switcher._vm_list.clearSelection()
        switcher._vm_list.setCurrentItem(None)

        switcher._switch_vm()

        assert warnings == ["Select a VM first"]