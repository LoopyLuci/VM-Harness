"""VM Switcher Panel — manage multiple VMs with sidebar integration and QMP bridge switching.

Features:
- VM switching in sidebar
- Resource allocation limits per VM
- QMP bridge connection per selected VM
- Dashboard integration for at-a-glance view
"""

from __future__ import annotations

from pathlib import Path

from gui.theme import T
from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QMessageBox, QListWidget, QListWidgetItem, QInputDialog, QFileDialog,
    QFrame, QGridLayout, QSpinBox, QGroupBox, QFormLayout, QSizePolicy,
)

from gui.widgets import Card, StatusIndicator, SectionHeader
from gui.multi_vm import MultiVMManager, GLOBAL_MAX_RAM_MB, GLOBAL_MAX_CPUS
from gui.vm_cloner import CloneDialog, TemplateManagerDialog, TemplateManager, QEMU_IMG_DEFAULT


#: Per-probe budget. Long enough to cross a LAN hop, short enough that two
#: unresponsive ports do not leave the panel "checking…" for a minute.
REACHABILITY_TIMEOUT = 1.5


class _ReachabilityWorker(QThread):
    """Probes a VM's port forwards off the GUI thread.

    ``QThread`` rather than a thread pool because these are one-shot bursts tied
    to a button press, and the result has to come back as a Qt signal to be
    delivered on the GUI thread. Probing inline would freeze the whole window for
    ``timeout`` per unreachable port.
    """

    done = pyqtSignal(dict)          # {"qmp": probe_result_dict, ...}

    def __init__(self, parent, forwards: list, timeout: float = REACHABILITY_TIMEOUT) -> None:
        super().__init__(parent)
        self._forwards = list(forwards)
        self._timeout = timeout

    def run(self) -> None:
        """Do the connects and emit. Never raises: a failed probe is a result."""
        from vm_harness.cluster.network import ReachabilityVerifier

        verifier = ReachabilityVerifier(timeout=self._timeout)
        results: dict = {}
        for forward in self._forwards:
            try:
                results[forward.purpose] = verifier.check_forward(
                    forward, timeout=self._timeout
                ).to_dict()
            except Exception as exc:  # noqa: BLE001 - one bad port must not lose the others
                results[forward.purpose] = {
                    "host": forward.host, "port": forward.port,
                    "reachability": "refused", "latency_ms": 0.0,
                    "checked_at": 0.0, "cached": False,
                    "detail": str(exc)[:200],
                    "provenance": "unknown",
                }
        self.done.emit(results)


class VMSwitcherPanel(QWidget):
    """Switch between multiple VMs and manage resource allocation."""

    vm_changed = pyqtSignal(str)  # Emits VM name when switched
    vm_added = pyqtSignal(str)
    vm_removed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._manager = MultiVMManager()
        self._active_vm: str | None = None
        self._reach_worker: "_ReachabilityWorker | None" = None
        self.setStyleSheet("background: " + T.BG_PRIMARY + ";")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # ── Header ─────────────────────────────────────────────────────────────
        header = QWidget()
        hl = QHBoxLayout(header)
        hl.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Virtual Machine Switcher")
        title.setStyleSheet(
            f"color: {T.TEXT_PRIMARY}; font-size: 16px; font-weight: bold;"
        )
        hl.addWidget(title)
        hl.addStretch()

        # Active VM indicator
        active_label_text = QLabel("Active: ")
        active_label_text.setStyleSheet(f"color: {T.TEXT_SECONDARY}; font-size: 12px;")
        hl.addWidget(active_label_text)
        self._active_label = QLabel("None")
        self._active_label.setStyleSheet(
            f"color: {T.BRAND}; font-weight: bold; font-size: 13px;"
        )
        hl.addWidget(self._active_label)
        layout.addWidget(header)

        # ── VM List ────────────────────────────────────────────────────────────
        list_card = Card("Available Virtual Machines")
        layout.addWidget(list_card)

        self._vm_list = QListWidget()
        self._vm_list.setStyleSheet(
            f"QListWidget {{ background: {T.BG_SECONDARY}; color: {T.TEXT_PRIMARY};"
            f" border: 1px solid {T.BG_TERTIARY}; border-radius: 6px; font-size: 12px; padding: 4px; }}"
            f"QListWidget::item {{ padding: 6px 8px; border-bottom: 1px solid {T.BG_TERTIARY}; }}"
            f"QListWidget::item:selected {{ background: {T.BRAND}; color: white; }}"
        )
        self._vm_list.currentItemChanged.connect(self._on_selection_changed)
        list_card.content_layout.addWidget(self._vm_list)

        # ── Action Buttons ─────────────────────────────────────────────────────
        btn_row = QWidget()
        bl = QHBoxLayout(btn_row)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(8)

        add_btn = QPushButton("Add VM")
        add_btn.setFixedSize(80, 32)
        add_btn.setStyleSheet(
            f"QPushButton {{ background: {T.STATUS_RUNNING}; border: none; border-radius: 6px;"
            f" color: white; font-size: 12px; font-weight: 600; }}"
            "QPushButton:hover { background: #16a34a; }"
        )
        add_btn.clicked.connect(self._add_vm)
        bl.addWidget(add_btn)

        switch_btn = QPushButton("Switch To")
        switch_btn.setFixedSize(80, 32)
        switch_btn.setStyleSheet(
            f"QPushButton {{ background: {T.BRAND}; border: none; border-radius: 6px;"
            f" color: white; font-size: 12px; font-weight: 600; }}"
            f"QPushButton:hover {{ background: {T.BRAND_HOVER}; }}"
        )
        switch_btn.clicked.connect(self._switch_vm)
        bl.addWidget(switch_btn)

        remove_btn = QPushButton("Remove")
        remove_btn.setFixedSize(70, 32)
        remove_btn.setStyleSheet(
            f"QPushButton {{ background: {T.STATUS_STOPPED}; border: none; border-radius: 6px;"
            f" color: white; font-size: 12px; font-weight: 600; }}"
            "QPushButton:hover { background: #dc2626; }"
        )
        remove_btn.clicked.connect(self._remove_vm)
        bl.addWidget(remove_btn)

        start_btn = QPushButton("Start")
        start_btn.setFixedSize(60, 32)
        start_btn.setStyleSheet(
            f"QPushButton {{ background: {T.SUCCESS}; border: none; border-radius: 6px;"
            f" color: white; font-size: 12px; font-weight: 600; }}"
            "QPushButton:hover { background: #16a34a; }"
        )
        start_btn.clicked.connect(self._start_selected)
        bl.addWidget(start_btn)

        stop_btn = QPushButton("Stop")
        stop_btn.setFixedSize(60, 32)
        stop_btn.setStyleSheet(
            f"QPushButton {{ background: {T.ERROR}; border: none; border-radius: 6px;"
            f" color: white; font-size: 12px; font-weight: 600; }}"
            "QPushButton:hover { background: #dc2626; }"
        )
        stop_btn.clicked.connect(self._stop_selected)
        bl.addWidget(stop_btn)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.setFixedSize(70, 32)
        refresh_btn.setStyleSheet(
            f"QPushButton {{ background: {T.BG_SECONDARY}; border: 1px solid {T.BG_TERTIARY};"
            f" border-radius: 6px; color: {T.TEXT_SECONDARY}; font-size: 12px; }}"
            f"QPushButton:hover {{ background: {T.BG_TERTIARY}; }}"
        )
        refresh_btn.clicked.connect(self.refresh)
        bl.addWidget(refresh_btn)

        clone_btn = QPushButton("Clone")
        clone_btn.setFixedSize(70, 32)
        clone_btn.setStyleSheet(
            f"QPushButton {{ background: {T.ACCENT}; border: none; border-radius: 6px;"
            f" color: white; font-size: 12px; font-weight: 600; }}"
            "QPushButton:hover { background: #4B5563; }"
        )
        clone_btn.clicked.connect(self._clone_vm)
        bl.addWidget(clone_btn)

        templates_btn = QPushButton("Templates")
        templates_btn.setFixedSize(80, 32)
        templates_btn.setStyleSheet(
            f"QPushButton {{ background: {T.BRAND}; border: none; border-radius: 6px;"
            f" color: white; font-size: 12px; font-weight: 600; }}"
            f"QPushButton:hover {{ background: {T.BRAND_HOVER}; }}"
        )
        templates_btn.clicked.connect(self._open_templates)
        bl.addWidget(templates_btn)

        bl.addStretch()
        layout.addWidget(btn_row)

        # ── Selected VM Details ────────────────────────────────────────────────
        details_card = Card("VM Details & Resource Limits")
        layout.addWidget(details_card)

        self._details_layout = QFormLayout()
        self._details_layout.setSpacing(6)
        details_card.content_layout.addLayout(self._details_layout)

        self._detail_name = QLabel("—")
        self._detail_name.setStyleSheet(f"color: {T.TEXT_PRIMARY}; font-size: 12px; font-weight: bold;")
        self._detail_status = QLabel("—")
        self._detail_status.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 11px;")
        self._detail_qmp = QLabel("—")
        self._detail_qmp.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 11px;")
        self._detail_ssh = QLabel("—")
        self._detail_ssh.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 11px;")
        self._detail_ram = QLabel("—")
        self._detail_ram.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 11px;")
        self._detail_cpus = QLabel("—")
        self._detail_cpus.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 11px;")

        self._details_layout.addRow("Name:", self._detail_name)
        self._details_layout.addRow("Status:", self._detail_status)
        self._details_layout.addRow("QMP URI:", self._detail_qmp)
        self._details_layout.addRow("SSH URI:", self._detail_ssh)
        self._details_layout.addRow("RAM:", self._detail_ram)
        self._details_layout.addRow("vCPUs:", self._detail_cpus)

        # ── Resource Limits Editor ─────────────────────────────────────────────
        limits_card = Card("Resource Limits")
        layout.addWidget(limits_card)

        limits_grid = QGridLayout()
        limits_grid.setSpacing(8)
        limits_grid.setContentsMargins(0, 0, 0, 0)

        # Max RAM
        ram_label = QLabel("Max RAM (MB):")
        ram_label.setStyleSheet(f"color: {T.TEXT_SECONDARY}; font-size: 11px;")
        self._ram_spin = QSpinBox()
        self._ram_spin.setRange(512, GLOBAL_MAX_RAM_MB)
        self._ram_spin.setSingleStep(512)
        self._ram_spin.setValue(4096)
        self._ram_spin.setStyleSheet(
            f"QSpinBox {{ background: {T.BG_PRIMARY}; color: {T.TEXT_PRIMARY};"
            f" border: 1px solid {T.BG_TERTIARY}; border-radius: 4px; }}"
        )
        limits_grid.addWidget(ram_label, 0, 0)
        limits_grid.addWidget(self._ram_spin, 0, 1)

        # Max CPUs
        cpu_label = QLabel("Max vCPUs:")
        cpu_label.setStyleSheet(f"color: {T.TEXT_SECONDARY}; font-size: 11px;")
        self._cpu_spin = QSpinBox()
        self._cpu_spin.setRange(1, GLOBAL_MAX_CPUS)
        self._cpu_spin.setValue(2)
        self._cpu_spin.setStyleSheet(
            f"QSpinBox {{ background: {T.BG_PRIMARY}; color: {T.TEXT_PRIMARY};"
            f" border: 1px solid {T.BG_TERTIARY}; border-radius: 4px; }}"
        )
        limits_grid.addWidget(cpu_label, 1, 0)
        limits_grid.addWidget(self._cpu_spin, 1, 1)

        # Priority
        prio_label = QLabel("Priority:")
        prio_label.setStyleSheet(f"color: {T.TEXT_SECONDARY}; font-size: 11px;")
        self._prio_spin = QSpinBox()
        self._prio_spin.setRange(1, 10)
        self._prio_spin.setValue(5)
        self._prio_spin.setToolTip("1 = highest priority, 10 = lowest")
        self._prio_spin.setStyleSheet(
            f"QSpinBox {{ background: {T.BG_PRIMARY}; color: {T.TEXT_PRIMARY};"
            f" border: 1px solid {T.BG_TERTIARY}; border-radius: 4px; }}"
        )
        limits_grid.addWidget(prio_label, 2, 0)
        limits_grid.addWidget(self._prio_spin, 2, 1)

        # Apply button
        apply_btn = QPushButton("Apply Limits")
        apply_btn.setFixedSize(100, 28)
        apply_btn.setStyleSheet(
            f"QPushButton {{ background: {T.BRAND}; border: none; border-radius: 4px;"
            f" color: white; font-size: 11px; }}"
            f"QPushButton:hover {{ background: {T.BRAND_HOVER}; }}"
        )
        apply_btn.clicked.connect(self._apply_limits)
        limits_grid.addWidget(apply_btn, 3, 0, 1, 2)

        limits_card.content_layout.addLayout(limits_grid)

        # ── Network Reachability ─────────────────────────────────────────────────
        # The ports below are *modelled* from config: nothing has connected to
        # them, so every row starts as "not checked". Only a real TCP connect
        # turns one into reachable/unreachable, and those runs on a worker thread
        # because each one can sit for the whole timeout.
        net_card = Card("Network Reachability")
        layout.addWidget(net_card)

        net_grid = QGridLayout()
        net_grid.setSpacing(8)
        net_grid.setContentsMargins(0, 0, 0, 0)
        net_grid.setColumnStretch(1, 1)

        self._net_port_labels: dict[str, QLabel] = {}
        for row, (key, caption) in enumerate((("qmp", "QMP"), ("ssh", "SSH"))):
            lbl = QLabel(caption)
            lbl.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 11px;")
            net_grid.addWidget(lbl, row, 0)
            val = QLabel("—")
            val.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 12px;")
            net_grid.addWidget(val, row, 1)
            self._net_port_labels[key] = val

        check_btn = QPushButton("Check Reachability")
        check_btn.setFixedHeight(28)
        check_btn.setCursor(Qt.PointingHandCursor)
        check_btn.setToolTip(
            "Connect to this VM's QMP and SSH ports and report what answered"
        )
        check_btn.setStyleSheet(
            f"QPushButton {{ background: {T.BG_SECONDARY}; color: {T.TEXT_SECONDARY};"
            f" border: 1px solid {T.BG_TERTIARY}; border-radius: 4px; font-size: 11px; }}"
            f"QPushButton:hover {{ color: {T.TEXT_PRIMARY}; }}"
        )
        check_btn.clicked.connect(self._check_reachability)
        net_grid.addWidget(check_btn, 2, 0, 1, 2)
        self._net_check_btn = check_btn

        self._net_limitation = QLabel("")
        self._net_limitation.setWordWrap(True)
        self._net_limitation.setStyleSheet(
            f"color: {T.WARNING}; font-size: 11px; padding: 6px 10px;"
            f" border-radius: 4px; background: {T.WARNING_BG};"
        )
        self._net_limitation.hide()

        net_card.content_layout.addLayout(net_grid)
        net_card.content_layout.addWidget(self._net_limitation)

        # ── Status ─────────────────────────────────────────────────────────────
        self._status = QLabel("No VM selected")
        self._status.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 11px;")
        layout.addWidget(self._status)

        # ── Auto-refresh timer ──────────────────────────────────────────────────
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(5000)

        self._show_host_limitation()
        self.refresh()

    def refresh(self) -> None:
        """Refresh the VM list and details."""
        # Poll status
        self._manager.poll_status()
        self._manager.cleanup_exited()

        # clear() destroys the current item, so the selection must be carried
        # across the rebuild or every action button silently dies. refresh()
        # runs on a 5s timer, which made this look like the buttons were
        # intermittently broken: select a VM, wait a moment, and Switch To,
        # Start or Remove would all claim no VM was selected. The details pane
        # went with it, because clear() also fires currentItemChanged(None).
        previously_selected = self._get_selected_name()

        self._vm_list.blockSignals(True)
        self._vm_list.clear()
        restore_to: QListWidgetItem | None = None
        for vm_name in self._manager.list_vms():
            config = self._manager.get_vm(vm_name)
            item = QListWidgetItem(vm_name)
            is_running = self._manager.is_running(vm_name)

            if is_running:
                item.setText(f"● {vm_name}")
                item.setForeground(Qt.green)
            else:
                item.setText(f"○ {vm_name}")
                item.setForeground(Qt.gray)

            # Store actual name in UserRole
            item.setData(Qt.UserRole, vm_name)
            self._vm_list.addItem(item)

            if vm_name == previously_selected:
                restore_to = item

            # Reselect active VM
            if self._active_vm == vm_name:
                self._vm_list.setCurrentItem(item)

        # Restore the user's selection, then let the normal handler repopulate
        # the details. Signals were blocked so the intermediate empty state did
        # not blank the pane.
        self._vm_list.blockSignals(False)
        if restore_to is not None:
            self._vm_list.setCurrentItem(restore_to)
        else:
            # Nothing to restore: either nothing was selected, or the selected
            # VM no longer exists. Either way the details must not keep showing
            # a VM that is not in the list.
            self._clear_details()

    def _get_selected_name(self) -> str | None:
        """Get the actual VM name of the currently selected item."""
        item = self._vm_list.currentItem()
        if item:
            return item.data(Qt.UserRole) or item.text().lstrip("● ○ ")
        return None

    def _on_selection_changed(self, current, _previous):
        """Update detail panel when selection changes."""
        if not current:
            self._clear_details()
            return

        name = current.data(Qt.UserRole) or current.text().lstrip("● ○ ")
        config = self._manager.get_vm(name)
        if config:
            self._detail_name.setText(name)
            status = self._manager.get_status(name)
            self._detail_status.setText(status)
            self._detail_status.setStyleSheet(
                f"color: {'#22c55e' if status == 'running' else '#f59e0b' if status == 'paused' else '#64748b'}; font-size: 11px;"
            )
            self._detail_qmp.setText(self._manager.get_qmp_uri(name) or "—")
            self._detail_ssh.setText(self._manager.get_ssh_uri(name) or "—")
            # The reachability rows describe whichever VM is selected, so showing
            # the previous VM's results under a new name would misattribute them.
            self._reset_reachability_rows()
            self._detail_ram.setText(f"{config.ram_mb} MB (max: {config.resource_limits.max_ram_mb} MB)")
            self._detail_cpus.setText(f"{config.cpus} (max: {config.resource_limits.max_cpus})")

            # Update spin boxes
            self._ram_spin.setValue(config.resource_limits.max_ram_mb)
            self._cpu_spin.setValue(config.resource_limits.max_cpus)
            self._prio_spin.setValue(config.resource_limits.priority)

    def _show_host_limitation(self) -> None:
        """Show what this host cannot do at the network layer, once, at start-up.

        Asked rather than hardcoded: the reason is host-dependent, and a machine
        with different capabilities should not be told it cannot bridge.
        """
        try:
            from vm_harness.cluster import layer2_capability

            can_l2, reason = layer2_capability()
        except Exception:  # noqa: BLE001 - a probe failure must not break the panel
            return
        if not can_l2:
            self._show_l2_limitation(reason)

    # ── Reachability ─────────────────────────────────────────────────────────

    def _selected_forwards(self) -> list:
        """The selected VM's port forwards, modelled from its config.

        Returns [] when nothing is selected or the config has no ports. The
        forwards are inferred, never observed -- only :meth:`_check_reachability`
        can promote them, and until then the rows say so.
        """
        name = self._get_selected_name()
        if not name:
            return []
        config = self._manager.get_vm(name)
        if config is None:
            return []
        from vm_harness.cluster import vm_forwards

        qmp = vm_forwards(
            name,
            qmp_port=int(getattr(config, "qmp_port", 0) or 0),
            host=str(getattr(config, "qmp_host", "127.0.0.1") or "127.0.0.1"),
        )
        # The SSH forward lives on its own host in the config; reusing the QMP host
        # would probe the wrong machine whenever the two differ.
        ssh = vm_forwards(
            name,
            ssh_port=int(getattr(config, "ssh_port", 0) or 0),
            host=str(getattr(config, "ssh_host", "127.0.0.1") or "127.0.0.1"),
        )
        return qmp + ssh

    def _check_reachability(self) -> None:
        """Connect to the selected VM's forwards and report each answer.

        Deliberately asynchronous. A probe that cannot be reached holds its socket
        for the whole timeout, and doing that on the Qt thread freezes the window
        for every other panel as well -- so the connects run on a worker thread and
        the results come back through a queued signal.

        The three outcomes stay distinct in the UI: `refused` means the host
        answered no (nothing listening), `timeout` means nothing answered at all.
        They have different causes and the user needs to see which one happened.
        """
        name = self._get_selected_name()
        forwards = self._selected_forwards()
        if not name:
            QMessageBox.warning(self, "Warning", "Select a VM first")
            return
        if not forwards:
            for label in self._net_port_labels.values():
                label.setText("no forward configured")
            return

        self._net_check_btn.setEnabled(False)
        for label in self._net_port_labels.values():
            label.setText("checking…")
            label.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 12px;")

        worker = _ReachabilityWorker(self, list(forwards))
        self._reach_worker = worker
        worker.done.connect(self._on_reachability_done)
        worker.finished.connect(lambda: setattr(self, "_reach_worker", None))
        worker.start()

    def _on_reachability_done(self, results: dict) -> None:
        """Render probe results.

        ``results`` maps a port name to the probe's dict. A port with no entry was
        not probed, which is shown as `not checked` rather than left blank: an empty
        row and an unchecked row are not the same claim.
        """
        self._net_check_btn.setEnabled(True)
        colors = {
            "reachable": T.SUCCESS,
            "refused": T.ERROR,
            "timeout": T.WARNING,
        }
        for key, label in self._net_port_labels.items():
            result = results.get(key)
            if result is None:
                label.setText("not checked")
                label.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 12px;")
                continue
            state = result["reachability"]
            if state == "reachable":
                text = f"{result['host']}:{result['port']} — reachable ({result['latency_ms']:.0f} ms)"
            else:
                text = f"{result['host']}:{result['port']} — {state}: {result['detail']}"
            label.setText(text)
            label.setStyleSheet(f"color: {colors.get(state, T.TEXT_MUTED)}; font-size: 12px;")
        self._status.setText("Reachability checked (measured, not inferred)")

    def _show_l2_limitation(self, reason: str) -> None:
        """State the layer-2 impossibility in the panel that suggests topology.

        Better here than in a log nobody reads: the reason a container and a VM
        cannot share a segment on this host is structural, and finding that out
        before building the topology is the whole point.
        """
        if not reason:
            self._net_limitation.hide()
            return
        self._net_limitation.setText(f"⚠️ {reason}")
        self._net_limitation.show()

    def _reset_reachability_rows(self) -> None:
        """Put the reachability rows back to "not checked".

        A stale result is worse than none: it reads as a fact about the selected VM
        when it is really a fact about whichever VM was selected a minute ago.
        """
        for label in self._net_port_labels.values():
            label.setText("not checked")
            label.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 12px;")

    def _clear_details(self):
        """Clear detail panel."""
        self._detail_name.setText("—")
        self._detail_status.setText("—")
        self._detail_qmp.setText("—")
        self._detail_ssh.setText("—")
        self._detail_ram.setText("—")
        self._detail_cpus.setText("—")

    def _add_vm(self):
        """Add a new VM."""
        name, ok = QInputDialog.getText(self, "Add VM", "VM name:")
        if not ok or not name.strip():
            return

        disk_path, _ = QFileDialog.getOpenFileName(
            self, "Select Disk Image", "",
            "Disk Images (*.qcow2 *.img *.vmdk *.raw);;All Files (*)"
        )
        if not disk_path:
            return

        config = {
            "vm_name": name.strip(),
            "disk_path": disk_path,
            "cpus": self._cpu_spin.value(),
            "ram_mb": self._ram_spin.value(),
            "max_ram_mb": self._ram_spin.value(),
            "max_cpus": self._cpu_spin.value(),
            "priority": self._prio_spin.value(),
            "display": "sdl",
            "qemu_binary": r"C:\Program Files\qemu\qemu-system-x86_64.exe",
        }

        success, msg = self._manager.add_vm(name.strip(), config)
        if success:
            self.refresh()
            self.vm_added.emit(name.strip())
        self._status.setText(msg)

    def _switch_vm(self):
        """Switch to selected VM — updates QMP bridge context."""
        name = self._get_selected_name()
        if not name:
            QMessageBox.warning(self, "Warning", "Select a VM first")
            return

        self._active_vm = name
        self._active_label.setText(name)
        self.vm_changed.emit(name)
        self._status.setText(f"Active VM: {name}")

    def _remove_vm(self):
        """Remove selected VM."""
        name = self._get_selected_name()
        if not name:
            QMessageBox.warning(self, "Warning", "Select a VM first")
            return

        reply = QMessageBox.question(
            self, "Confirm Remove",
            f"Remove VM '{name}'? Configuration will be deleted.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            success, msg = self._manager.remove_vm(name)
            if success:
                if self._active_vm == name:
                    self._active_vm = None
                    self._active_label.setText("None")
                self.refresh()
                self.vm_removed.emit(name)
            self._status.setText(msg)

    def _start_selected(self):
        """Start the selected VM."""
        name = self._get_selected_name()
        if not name:
            QMessageBox.warning(self, "Warning", "Select a VM first")
            return

        success, msg = self._manager.start_vm(name)
        self.refresh()
        self._status.setText(msg)

    def _stop_selected(self):
        """Stop the selected VM."""
        name = self._get_selected_name()
        if not name:
            QMessageBox.warning(self, "Warning", "Select a VM first")
            return

        success, msg = self._manager.stop_vm(name)
        self.refresh()
        self._status.setText(msg)

    def _apply_limits(self):
        """Apply resource limits to selected VM."""
        name = self._get_selected_name()
        if not name:
            QMessageBox.warning(self, "Warning", "Select a VM first")
            return

        updates = {
            "max_ram_mb": self._ram_spin.value(),
            "max_cpus": self._cpu_spin.value(),
            "priority": self._prio_spin.value(),
            "resource_limits": {
                "max_ram_mb": self._ram_spin.value(),
                "max_cpus": self._cpu_spin.value(),
                "priority": self._prio_spin.value(),
            }
        }
        success, msg = self._manager.update_vm(name, updates)
        self.refresh()
        self._status.setText(msg)

    def get_manager(self) -> MultiVMManager:
        """Get the MultiVMManager instance."""
        return self._manager

    def get_active_vm(self) -> str | None:
        """Get the currently active VM name."""
        return self._active_vm

    def set_active_vm(self, name: str) -> None:
        """Set the active VM from external (e.g., dashboard selection)."""
        self._active_vm = name
        self._active_label.setText(name)

    # ── Clone & Template handlers ──────────────────────────────────────────────

    def _clone_vm(self):
        """Open the clone dialog for the selected VM's disk."""
        name = self._get_selected_name()
        if not name:
            QMessageBox.warning(self, "Warning", "Select a VM first")
            return

        config = self._manager.get_vm(name)
        if not config or not config.disk_path:
            QMessageBox.warning(
                self, "Warning",
                f"VM '{name}' has no disk image assigned",
            )
            return

        dlg = CloneDialog(
            self,
            source_disk=config.disk_path,
            template_manager=TemplateManager(),
            qemu_img=QEMU_IMG_DEFAULT,
        )
        dlg.exec_()

    def _open_templates(self):
        """Open the template manager dialog."""
        dlg = TemplateManagerDialog(self, TemplateManager())
        dlg.exec_()
