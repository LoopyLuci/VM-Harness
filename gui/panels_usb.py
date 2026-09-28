"""USB & Device Management Panel — enhanced passthrough UI.

Features:
    1. QTableWidget for USB device listing
    2. WMI enumeration (Windows Management Instrumentation)
    3. Attach/Detach buttons wired to QMP device_add/device_del
    4. Hotplug via QMP
    5. Filter/search box
    6. Config persistence (JSON file)

All enumeration and QMP calls are done with graceful degradation:
if WMI is unavailable (e.g. on Linux, or import failure), the panel
falls back to a static sample list.  If no QMP bridge is set, the
buttons log to the status label.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Callable, Dict, List, Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gui.theme import T
from gui.widgets import Card

logger = logging.getLogger("vmharness.usb_panel")

# Default config path — beside this file for simplicity.
_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".qemu-mcp")
_CONFIG_FILE = os.path.join(_CONFIG_DIR, "usb_config.json")


# ═══════════════════════════════════════════════════════════════════════════════
# USB device dataclass (lightweight)
# ═══════════════════════════════════════════════════════════════════════════════

class USBDevice:
    """Represents a single USB device on the host.

    Attributes:
        vendor_id:  4-char hex vendor ID (e.g. "046d")
        product_id: 4-char hex product ID (e.g. "c52b")
        serial:     Device serial number or unique string
        bus:        Bus number as string
        device:     Device address on bus
        vendor_name: Human-readable vendor string
        product_name: Human-readable product string
        assigned:   True if currently attached to a VM
    """

    def __init__(
        self,
        vendor_id: str = "",
        product_id: str = "",
        serial: str = "",
        bus: str = "",
        device: str = "",
        vendor_name: str = "",
        product_name: str = "",
        assigned: bool = False,
    ):
        self.vendor_id = vendor_id.lower()
        self.product_id = product_id.lower()
        self.serial = serial
        self.bus = bus
        self.device = device
        self.vendor_name = vendor_name or "Unknown"
        self.product_name = product_name or "Unknown"
        self.assigned = assigned

    def to_dict(self) -> Dict[str, str]:
        return {
            "vendor_id": self.vendor_id,
            "product_id": self.product_id,
            "serial": self.serial,
            "bus": self.bus,
            "device": self.device,
            "vendor_name": self.vendor_name,
            "product_name": self.product_name,
            "assigned": "Yes" if self.assigned else "No",
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "USBDevice":
        return cls(
            vendor_id=str(d.get("vendor_id", "")),
            product_id=str(d.get("product_id", "")),
            serial=str(d.get("serial", "")),
            bus=str(d.get("bus", "")),
            device=str(d.get("device", "")),
            vendor_name=str(d.get("vendor_name", "")),
            product_name=str(d.get("product_name", "")),
            assigned=d.get("assigned", False) in (True, "Yes", "true", "1"),
        )

    @property
    def qmp_host_addr(self) -> str:
        """Return hostbus,hostaddr or hostvendor,hostproduct string for QMP."""
        if self.bus and self.device:
            return f"hostbus={self.bus},hostaddr={self.device}"
        return f"hostvendor=0x{self.vendor_id},hostproduct=0x{self.product_id}"

    def matches_filter(self, text: str) -> bool:
        """Return True if the filter text matches any field (case-insensitive)."""
        if not text:
            return True
        needle = text.lower()
        haystack = (
            f"{self.vendor_id} {self.product_id} {self.serial} "
            f"{self.bus} {self.device} {self.vendor_name} {self.product_name}"
        ).lower()
        return needle in haystack

    def __repr__(self) -> str:
        return (
            f"USBDevice({self.vendor_id}:{self.product_id} "
            f"{self.vendor_name} {self.product_name})"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# WMI enumeration helper
# ═══════════════════════════════════════════════════════════════════════════════

def enumerate_usb_devices_wmi() -> List[USBDevice]:
    """Enumerate USB devices via WMI (Windows only).

    Uses the ``wmi`` Python package if available; falls back to a
    PowerShell invocation if only ``win32com`` is installed.

    Returns an empty list if WMI is not available.
    """
    devices: List[USBDevice] = []

    # Try the wmi pip package first (most common)
    try:
        import wmi  # type: ignore

        c = wmi.WMI()
        # Query PnPEntity for USB devices
        for item in c.Win32_PnPEntity():
            if item.Name and "USB" in (item.Name or "").upper():
                vid, pid, serial = _parse_device_id(str(item.DeviceID or ""))
                devices.append(
                    USBDevice(
                        vendor_id=vid,
                        product_id=pid,
                        serial=serial or str(item.PNPDeviceID or ""),
                        vendor_name=str(item.Manufacturer or ""),
                        product_name=str(item.Name or ""),
                        bus="",
                        device="",
                    )
                )
        # Also grab USB controller devices for bus/addr info
        for ctrl in c.Win32_USBControllerDevice():
            dependent = getattr(ctrl, "Dependent", None)
            antecedent = getattr(ctrl, "Antecedent", None)
            # Dependent is the actual device; Antecedent is the controller.
            # We don't parse these deeply — they're supplementary.
            _ = dependent, antecedent  # noqa: F841  (avoid unused warning)

        if devices:
            return devices
    except ImportError:
        logger.debug("wmi package not available, trying fallback")
    except Exception as e:
        logger.warning("WMI enumeration failed: %s", e)

    # Fallback: use PowerShell to query USB devices via CIM
    try:
        import subprocess

        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_PnPEntity | "
                "Where-Object { $_.DeviceID -match 'USB' } | "
                "Select-Object DeviceID,Name,Manufacturer | ConvertTo-Json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            try:
                data = json.loads(result.stdout)
                if isinstance(data, dict):
                    data = [data]
                for item in data:
                    vid, pid, serial = _parse_device_id(
                        str(item.get("DeviceID", ""))
                    )
                    devices.append(
                        USBDevice(
                            vendor_id=vid,
                            product_id=pid,
                            serial=serial,
                            vendor_name=str(item.get("Manufacturer", "")),
                            product_name=str(item.get("Name", "")),
                            bus="",
                            device="",
                        )
                    )
            except json.JSONDecodeError:
                pass
        if devices:
            return devices
    except Exception as e:
        logger.warning("PowerShell USB enumeration failed: %s", e)

    return devices


def _parse_device_id(device_id: str):
    """Parse a Windows PnP DeviceID into (vendor_id, product_id, serial).

    Example input: USB\\VID_046D&PID_C52B\\12345678
    """
    vid = ""
    pid = ""
    serial = ""
    parts = device_id.split("\\")
    if len(parts) >= 2:
        id_parts = parts[1].split("&")
        for p in id_parts:
            if p.upper().startswith("VID_"):
                vid = p[4:8]
            elif p.upper().startswith("PID_"):
                pid = p[4:8]
        if len(parts) >= 3:
            serial = parts[2]
    return vid, pid, serial


# ═══════════════════════════════════════════════════════════════════════════════
# Config persistence
# ═══════════════════════════════════════════════════════════════════════════════

def load_usb_config(path: str = None) -> Dict[str, Any]:
    """Load persisted USB config from disk.
    
    Args:
        path: Optional config file path. Uses default if not provided.

    Returns a dict with keys:
        - ``favorites``: list of device dicts the user pinned
        - ``auto_attach``: list of device IDs that auto-attach on VM start
        - ``filter_history``: list of previous search strings
    """
    if path is None:
        path = _CONFIG_FILE
    default: Dict[str, Any] = {
        "favorites": [],
        "auto_attach": [],
        "filter_history": [],
    }
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Merge with defaults so missing keys are safe
        for k, v in default.items():
            data.setdefault(k, v)
        return data
    except FileNotFoundError:
        return default
    except Exception as e:
        logger.warning("Failed to load USB config: %s", e)
        return default


def save_usb_config(config: Dict[str, Any], path: str = None) -> bool:
    """Save USB config to disk. Returns True on success."""
    if path is None:
        path = _CONFIG_FILE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        return True
    except Exception as e:
        logger.warning("Failed to save USB config: %s", e)
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Sample fallback devices (used when WMI is unavailable)
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# Main Panel
# ═══════════════════════════════════════════════════════════════════════════════

class USBDevicePanel(QWidget):
    """USB device passthrough and management panel.

    Parameters
    ----------
    parent : QWidget or None
        Parent widget.
    qmp_command_callback : callable or None
        If provided, Attach/Detach actions call this with a raw QMP command
        dict, e.g. ``{"execute": "device_add", "arguments": {...}}``.
        When None, the actions are logged but not sent.
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        qmp_command_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        super().__init__(parent)
        self._qmp_callback = qmp_command_callback
        self.setStyleSheet("background: " + T.BG_PRIMARY + ";")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        header = QWidget()
        hl = QHBoxLayout(header)
        hl.setContentsMargins(0, 0, 0, 0)
        title = QLabel("USB & Device Management")
        title.setStyleSheet(
            "color: " + T.TEXT_PRIMARY + "; font-size: 16px; font-weight: bold;"
        )
        hl.addWidget(title)
        hl.addStretch()
        layout.addWidget(header)

        tabs = QTabWidget()
        tabs.setStyleSheet(
            "QTabWidget::pane { border: 1px solid " + T.BG_TERTIARY + "; border-radius: 6px; }"
            "QTabBar::tab { background: " + T.BG_SECONDARY + "; color: " + T.TEXT_SECONDARY + ";"
            " padding: 8px 16px; margin-right: 2px; border-radius: 4px 4px 0 0; }"
            "QTabBar::tab:selected { background: " + T.BRAND + "; color: white; }"
        )
        tabs.addTab(self._usb_devices_tab(), "USB Devices")
        tabs.addTab(self._pci_devices_tab(), "PCI Passthrough")
        tabs.addTab(self._tpm_tab(), "TPM / Secure Boot")
        layout.addWidget(tabs)

        # Internal state
        self._all_devices: List[USBDevice] = []
        self._filtered_devices: List[USBDevice] = []
        self._config = load_usb_config()

        # Populate devices synchronously so tests and UI both get data immediately
        self._refresh_devices()

    # ── USB Devices tab ─────────────────────────────────────────────────────────

    def _usb_devices_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        # ── Search / filter bar ─────────────────────────────────────────────
        search_row = QWidget()
        sl = QHBoxLayout(search_row)
        sl.setContentsMargins(0, 0, 0, 0)
        search_label = QLabel("Search:")
        search_label.setStyleSheet(
            "color: " + T.TEXT_SECONDARY + "; font-size: 12px;"
        )
        sl.addWidget(search_label)

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText(
            "Filter by vendor, product, serial, bus, device..."
        )
        self._search_input.setStyleSheet(
            "background: " + T.BG_PRIMARY + ";"
            "border: 1px solid " + T.BG_TERTIARY + ";"
            "border-radius: 4px;"
            "color: " + T.TEXT_PRIMARY + ";"
            "padding: 6px 10px;"
            "font-size: 12px;"
        )
        self._search_input.textChanged.connect(self._on_filter_changed)
        sl.addWidget(self._search_input, stretch=1)

        clear_btn = QPushButton("Clear")
        clear_btn.setFixedSize(60, 28)
        clear_btn.setStyleSheet(
            "background: " + T.BG_TERTIARY + ";"
            "border: none;"
            "border-radius: 4px;"
            "color: " + T.TEXT_PRIMARY + ";"
            "font-size: 11px;"
        )
        clear_btn.clicked.connect(lambda: self._search_input.clear())
        sl.addWidget(clear_btn)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.setFixedSize(70, 28)
        refresh_btn.setStyleSheet(
            "background: " + T.BRAND + ";"
            "border: none;"
            "border-radius: 4px;"
            "color: white;"
            "font-size: 11px;"
            "font-weight: 600;"
        )
        refresh_btn.clicked.connect(self._refresh_devices)
        sl.addWidget(refresh_btn)

        layout.addWidget(search_row)

        # ── Device table ────────────────────────────────────────────────────
        self._usb_table = QTableWidget()
        self._usb_table.setColumnCount(8)
        self._usb_table.setHorizontalHeaderLabels(
            ["Vendor", "Product", "Serial", "Bus", "Device", "Vendor Name", "Product Name", "Assigned"]
        )
        self._usb_table.horizontalHeader().setStretchLastSection(True)
        self._usb_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents
        )
        self._usb_table.setStyleSheet(
            "QTableWidget { background: " + T.BG_SECONDARY + "; color: " + T.TEXT_PRIMARY + ";"
            " border: 1px solid " + T.BG_TERTIARY + "; border-radius: 6px; font-size: 12px; }"
            "QHeaderView::section { background: " + T.BG_TERTIARY + "; color: " + T.TEXT_SECONDARY + ";"
            " padding: 6px; border: none; }"
            "QTableWidget::item:selected { background: #1e3a5f; color: " + T.BRAND + "; }"
        )
        self._usb_table.setAlternatingRowColors(True)
        self._usb_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._usb_table.setSelectionMode(QTableWidget.SingleSelection)
        self._usb_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self._usb_table)

        # ── Status label ────────────────────────────────────────────────────
        self._status_label = QLabel("Ready")
        self._status_label.setStyleSheet(
            "color: " + T.TEXT_MUTED + "; font-size: 11px;"
        )
        layout.addWidget(self._status_label)

        # ── Action buttons ──────────────────────────────────────────────────
        btn_row = QWidget()
        bl = QHBoxLayout(btn_row)
        bl.setContentsMargins(0, 0, 0, 0)

        self._attach_btn = QPushButton("Attach to VM")
        self._attach_btn.setFixedSize(120, 32)
        self._attach_btn.setStyleSheet(
            "background: " + T.STATUS_RUNNING + "; border: none; border-radius: 6px;"
            " color: white; font-size: 12px; font-weight: 600;"
        )
        self._attach_btn.clicked.connect(self._on_attach)
        bl.addWidget(self._attach_btn)

        self._detach_btn = QPushButton("Detach")
        self._detach_btn.setFixedSize(90, 32)
        self._detach_btn.setStyleSheet(
            "background: " + T.STATUS_STOPPED + "; border: none; border-radius: 6px;"
            " color: white; font-size: 12px; font-weight: 600;"
        )
        self._detach_btn.clicked.connect(self._on_detach)
        bl.addWidget(self._detach_btn)

        bl.addStretch()

        self._auto_attach_cb = QCheckBox("Auto-attach on VM start")
        self._auto_attach_cb.setStyleSheet(
            "color: " + T.TEXT_SECONDARY + "; font-size: 12px;"
        )
        self._auto_attach_cb.toggled.connect(self._on_auto_attach_toggled)
        bl.addWidget(self._auto_attach_cb)

        layout.addWidget(btn_row)

        return page

    # ── PCI Passthrough tab (unchanged) ────────────────────────────────────────

    def _pci_devices_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        self._pci_table = QTableWidget()
        self._pci_table.setColumnCount(5)
        self._pci_table.setHorizontalHeaderLabels(
            ["Address", "Vendor", "Device", "Class", "Status"]
        )
        self._pci_table.horizontalHeader().setStretchLastSection(True)
        self._pci_table.setStyleSheet(
            "QTableWidget { background: " + T.BG_SECONDARY + "; color: " + T.TEXT_PRIMARY + ";"
            " border: 1px solid " + T.BG_TERTIARY + "; border-radius: 6px; font-size: 12px; }"
            "QHeaderView::section { background: " + T.BG_TERTIARY + "; color: " + T.TEXT_SECONDARY + ";"
            " padding: 6px; border: none; }"
        )

        pci_devices = [
            ["00:02.0", "Intel", "HD Graphics 630", "VGA", "Host"],
            ["01:00.0", "NVIDIA", "RTX 3080", "3D Controller", "Available"],
            ["02:00.0", "Intel", "NVMe SSD", "Storage", "Host"],
        ]
        self._pci_table.setRowCount(len(pci_devices))
        for i, dev in enumerate(pci_devices):
            for j, val in enumerate(dev):
                self._pci_table.setItem(i, j, QTableWidgetItem(val))

        layout.addWidget(self._pci_table)

        btn_row = QWidget()
        bl = QHBoxLayout(btn_row)
        bl.setContentsMargins(0, 0, 0, 0)
        passthrough_btn = QPushButton("Passthrough to VM")
        passthrough_btn.setFixedSize(150, 32)
        passthrough_btn.setStyleSheet(
            "background: " + T.STATUS_RUNNING + "; border: none; border-radius: 6px;"
            " color: white; font-size: 12px; font-weight: 600;"
        )
        bl.addWidget(passthrough_btn)
        release_btn = QPushButton("Release")
        release_btn.setFixedSize(90, 32)
        release_btn.setStyleSheet(
            "background: " + T.STATUS_STOPPED + "; border: none; border-radius: 6px;"
            " color: white; font-size: 12px; font-weight: 600;"
        )
        bl.addWidget(release_btn)
        bl.addStretch()
        layout.addWidget(btn_row)
        return page

    # ── TPM tab (unchanged) ────────────────────────────────────────────────────

    def _tpm_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        card = Card("TPM & Secure Boot Configuration")
        layout.addWidget(card)

        form_widget = QWidget()
        fl = QVBoxLayout(form_widget)

        self._tpm_enable = QCheckBox("Enable vTPM 2.0 (required for Windows 11)")
        self._tpm_enable.setStyleSheet(
            "color: " + T.TEXT_SECONDARY + "; font-size: 12px;"
        )
        fl.addWidget(self._tpm_enable)

        self._secure_boot = QCheckBox("Enable UEFI Secure Boot")
        self._secure_boot.setStyleSheet(
            "color: " + T.TEXT_SECONDARY + "; font-size: 12px;"
        )
        fl.addWidget(self._secure_boot)

        card.content_layout.addWidget(form_widget)
        layout.addStretch()
        return page

    # ── Device enumeration ─────────────────────────────────────────────────────

    def _refresh_devices(self) -> None:
        """Re-enumerate USB devices and repopulate the table."""
        devices = enumerate_usb_devices_wmi()
        if not devices:
            # Never show made-up devices: an agent (or a person) could try to pass one through to a VM.
            self._status_label.setText(
                "No USB devices found (Windows WMI did not answer, or nothing is plugged in)"
            )
        else:
            self._status_label.setText(
                f"Found {len(devices)} USB device(s)"
            )

        # Restore assigned state from config
        auto_attach_ids = set(self._config.get("auto_attach", []))
        for dev in devices:
            dev_id = f"{dev.vendor_id}:{dev.product_id}:{dev.serial}"
            if dev_id in auto_attach_ids:
                dev.assigned = True

        self._all_devices = devices
        self._apply_filter()

    def _apply_filter(self) -> None:
        """Filter the device list based on the search input and repopulate the table."""
        text = self._search_input.text()
        self._filtered_devices = [
            d for d in self._all_devices if d.matches_filter(text)
        ]
        self._populate_table(self._filtered_devices)

    def _populate_table(self, devices: List[USBDevice]) -> None:
        """Fill the QTableWidget with the given device list."""
        self._usb_table.setRowCount(len(devices))
        for i, dev in enumerate(devices):
            values = [
                dev.vendor_id,
                dev.product_id,
                dev.serial,
                dev.bus,
                dev.device,
                dev.vendor_name,
                dev.product_name,
                "Yes" if dev.assigned else "No",
            ]
            for j, val in enumerate(values):
                item = QTableWidgetItem(val)
                item.setData(Qt.UserRole, dev)  # store reference
                self._usb_table.setItem(i, j, item)

    # ── Filter callback ─────────────────────────────────────────────────────────

    def _on_filter_changed(self, text: str) -> None:
        """Called when the search input changes."""
        self._apply_filter()

    # ── Attach / Detach ─────────────────────────────────────────────────────────

    def _selected_device(self) -> Optional[USBDevice]:
        """Return the currently selected USBDevice, or None."""
        row = self._usb_table.currentRow()
        if row < 0 or row >= len(self._filtered_devices):
            return None
        return self._filtered_devices[row]

    def _on_attach(self) -> None:
        """Attach the selected device to the VM via QMP device_add."""
        dev = self._selected_device()
        if dev is None:
            self._status_label.setText("No device selected")
            return

        if dev.assigned:
            self._status_label.setText(f"{dev.product_name} already attached")
            return

        # Build QMP command for usb-host device hotplug
        qmp_cmd: Dict[str, Any] = {
            "execute": "device_add",
            "arguments": {
                "driver": "usb-host",
                "vendorid": int(dev.vendor_id, 16) if dev.vendor_id else 0,
                "productid": int(dev.product_id, 16) if dev.product_id else 0,
                "bus": "usb.0",
                "id": f"usb-host-{dev.vendor_id}-{dev.product_id}",
            },
        }
        # Include serial if available
        if dev.serial:
            qmp_cmd["arguments"]["serial"] = dev.serial

        if self._qmp_callback is not None:
            try:
                self._qmp_callback(qmp_cmd)
                dev.assigned = True
                self._apply_filter()
                self._status_label.setText(
                    f"Attached {dev.vendor_name} {dev.product_name}"
                )
            except Exception as e:
                self._status_label.setText(f"Attach failed: {e}")
                logger.error("QMP attach failed: %s", e)
        else:
            logger.info("QMP attach (no callback): %s", qmp_cmd)
            dev.assigned = True
            self._apply_filter()
            self._status_label.setText(
                f"Attached {dev.vendor_name} {dev.product_name} (no QMP)"
            )

    def _on_detach(self) -> None:
        """Detach the selected device from the VM via QMP device_del."""
        dev = self._selected_device()
        if dev is None:
            self._status_label.setText("No device selected")
            return

        if not dev.assigned:
            self._status_label.setText(f"{dev.product_name} not attached")
            return

        qmp_cmd: Dict[str, Any] = {
            "execute": "device_del",
            "arguments": {
                "id": f"usb-host-{dev.vendor_id}-{dev.product_id}",
            },
        }

        if self._qmp_callback is not None:
            try:
                self._qmp_callback(qmp_cmd)
                dev.assigned = False
                self._apply_filter()
                self._status_label.setText(
                    f"Detached {dev.vendor_name} {dev.product_name}"
                )
            except Exception as e:
                self._status_label.setText(f"Detach failed: {e}")
                logger.error("QMP detach failed: %s", e)
        else:
            logger.info("QMP detach (no callback): %s", qmp_cmd)
            dev.assigned = False
            self._apply_filter()
            self._status_label.setText(
                f"Detached {dev.vendor_name} {dev.product_name} (no QMP)"
            )

    def _on_auto_attach_toggled(self, checked: bool) -> None:
        """Persist auto-attach preference for the selected device."""
        dev = self._selected_device()
        if dev is None:
            return
        dev_id = f"{dev.vendor_id}:{dev.product_id}:{dev.serial}"
        auto_attach = self._config.setdefault("auto_attach", [])
        if checked and dev_id not in auto_attach:
            auto_attach.append(dev_id)
        elif not checked and dev_id in auto_attach:
            auto_attach.remove(dev_id)
        save_usb_config(self._config)

    # ── Public API ──────────────────────────────────────────────────────────────

    def set_qmp_callback(
        self, callback: Optional[Callable[[Dict[str, Any]], None]]
    ) -> None:
        """Set or replace the QMP command callback."""
        self._qmp_callback = callback

    def get_all_devices(self) -> List[USBDevice]:
        """Return the full (unfiltered) device list."""
        return list(self._all_devices)

    def get_filtered_devices(self) -> List[USBDevice]:
        """Return the currently filtered device list."""
        return list(self._filtered_devices)

    def set_filter(self, text: str) -> None:
        """Programmatically set the filter text."""
        self._search_input.setText(text)

    def save_config(self) -> bool:
        """Explicitly save the current config to disk."""
        return save_usb_config(self._config)
