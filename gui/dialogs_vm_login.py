"""Per-VM credentials, labelled for the machine they will be used on.

This started as a context-free "Console Login" popup that asked for a
"Console username" and a "Console password" and stored one global pair. That
was wrong three times over: it did not say which machine the credentials were
for, "Console" is not what the field is, and one global pair cannot serve
several VMs without silently signing into the wrong one. It now takes the VM,
shows that VM's actual configuration, and stores the pair under that VM's
name.

Fields are labelled Username and Password. The target is named in the title,
in a header line, and again on every row of the detail table, because the
question being answered is always "sign in to *this* machine", not "sign in
to something".

Credentials are per VM: `vm-login/<vm name>`. A saved entry for one VM can
never satisfy a login for another.
"""
from __future__ import annotations

import json
from typing import Any

from loguru import logger
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gui.credential_store import CredentialStore
from gui.theme import T
from gui.widgets import PasswordInput, TextInput


def credential_name(vm_name: str) -> str:
    """Store key for one VM's credentials."""
    return f"vm-login/{vm_name}"


def save_vm_login(vm_name: str, username: str, password: str, description: str = "") -> str:
    """Persist credentials for `vm_name`, replacing any previous pair.

    Uses update-then-add rather than CredentialStore.add() alone. add()
    documents that it updates the value when a name already exists, but in
    practice it returns the existing entry untouched (credential_store.py:186),
    so saving a changed password through it would report success and keep the
    old one -- and the next login would fail for no visible reason.
    """
    store = CredentialStore()
    payload = json.dumps({"username": username, "password": password, "vm": vm_name})
    note = description or f"Login for VM '{vm_name}'"
    name = credential_name(vm_name)
    if store.get(name) is not None:
        store.update(name, payload, note)
    else:
        store.add(name, "password", payload, note)
    logger.info("saved credentials for VM '{}' (user '{}')", vm_name, username)
    return name


def load_vm_login(vm_name: str) -> tuple[str, str] | None:
    """Return (username, password) for `vm_name`, or None if nothing is stored.

    Callable with no GUI and no human present, which is the point of storing it.
    """
    store = CredentialStore()
    cred = store.get(credential_name(vm_name))
    if cred is None:
        return None
    # get() substitutes a placeholder rather than raising when the key is wrong.
    if cred.value == "[decryption failed]":
        raise RuntimeError(
            f"credentials for '{vm_name}' could not be decrypted: the master "
            "password or .master_key does not match the ones they were saved with"
        )
    try:
        data = json.loads(cred.value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"credentials for '{vm_name}' are malformed ({exc}); re-enter them"
        ) from exc
    username = str(data.get("username", ""))
    password = str(data.get("password", ""))
    if not username or not password:
        return None
    return username, password


def stored_vm_names() -> list[str]:
    """Every VM that has credentials saved, for a picker or an audit."""
    return sorted(
        c.name.split("/", 1)[1]
        for c in CredentialStore().list_all()
        if c.name.startswith("vm-login/")
    )


def vm_login_status(vm_name: str | None) -> str:
    """Human-readable state for a panel. Never includes the password."""
    if not vm_name:
        return "No VM selected"
    try:
        creds = load_vm_login(vm_name)
    except RuntimeError as exc:
        return f"Unreadable: {exc}"
    if creds is None:
        return f"Not saved for '{vm_name}'"
    return f"Saved for {vm_name} as '{creds[0]}'"


def vm_detail_rows(vm_name: str, config: Any, status: str, qmp_uri: str, ssh_uri: str) -> list[tuple[str, str]]:
    """The VM's real configuration, as (label, value) rows to display.

    Everything here is read from the live MultiVMManager. Guessing at these
    values is what made the original dialog useless: it could not tell you
    which machine it was about to sign in to.
    """
    def val(getter, default: str = "—") -> str:
        try:
            out = getter()
        except Exception:  # noqa: BLE001 - a display row must never raise
            return default
        if out is None or out == "":
            return default
        return str(out)

    rows: list[tuple[str, str]] = [
        ("Name", val(lambda: getattr(config, "vm_name", None) or vm_name)),
        ("Status", val(lambda: status, "unknown")),
        ("Description", val(lambda: getattr(config, "description", None))),
        ("QEMU binary", val(lambda: getattr(config, "qemu_binary", None))),
        ("Machine", val(lambda: getattr(config, "machine_type", None))),
        ("vCPUs", val(lambda: getattr(config, "cpus", None))),
        ("RAM", val(lambda: f"{getattr(config, 'ram_mb', None)} MB")),
        ("Disk", val(lambda: getattr(config, "disk_path", None))),
        ("ISO", val(lambda: getattr(config, "iso_path", None), "none attached")),
        ("Boot order", val(lambda: getattr(config, "boot_order", None))),
        ("Firmware", val(lambda: getattr(config, "boot_firmware", None))),
        ("Display", val(lambda: getattr(config, "display", None))),
        ("VGA", val(lambda: getattr(config, "vga", None))),
        ("QMP URI", val(lambda: qmp_uri or None)),
        ("SSH URI", val(lambda: ssh_uri or None)),
        ("SSH user (config)", val(lambda: getattr(config, "ssh_username", None))),
        ("QMP PID", val(lambda: getattr(config, "pid", None), "not running")),
    ]
    return rows


class VMLoginCredentialsDialog(QDialog):
    """Enter and save the Username and Password for one specific VM."""

    def __init__(self, vm_name: str, config: Any = None, status: str = "",
                 qmp_uri: str = "", ssh_uri: str = "", parent=None):
        super().__init__(parent)
        self.vm_name = vm_name
        self.setWindowTitle(f"Login — {vm_name}")
        self.setMinimumWidth(560)
        self.setStyleSheet(f"QDialog {{ background: {T.BG_PRIMARY}; }}")

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        heading = QLabel(f"Username and password for <b>{vm_name}</b>")
        heading.setTextFormat(Qt.RichText)
        heading.setStyleSheet(f"color: {T.TEXT_PRIMARY}; font-size: 14px; font-weight: 600;")
        layout.addWidget(heading)

        sub = QLabel(
            "These are the credentials for this machine only. They are stored "
            "encrypted, and are never shown again after saving."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 11px;")
        layout.addWidget(sub)

        # ── VM details ───────────────────────────────────────────────────────
        detail_box = QGroupBox(f"Target: {vm_name}")
        detail_box.setStyleSheet(
            f"QGroupBox {{ color: {T.TEXT_SECONDARY}; font-size: 12px;"
            f" border: 1px solid {T.BG_TERTIARY}; border-radius: 4px; margin-top: 8px; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; }}"
        )
        grid = QGridLayout(detail_box)
        grid.setSpacing(4)
        grid.setColumnStretch(1, 1)
        rows = vm_detail_rows(vm_name, config, status, qmp_uri, ssh_uri)
        for i, (label, value) in enumerate(rows):
            key = QLabel(f"{label}:")
            key.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 11px;")
            val = QLabel(value)
            val.setStyleSheet(f"color: {T.TEXT_PRIMARY}; font-size: 11px;")
            val.setWordWrap(False)
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            grid.addWidget(key, i, 0)
            grid.addWidget(val, i, 1)
        layout.addWidget(detail_box)

        # ── Credentials ──────────────────────────────────────────────────────
        form = QFormLayout()
        form.setSpacing(10)

        self.username_input = TextInput("Username")
        form.addRow("Username", self.username_input)

        self.password_input = PasswordInput("Password")
        form.addRow("Password", self.password_input)

        self.confirm_input = PasswordInput("Repeat the password")
        form.addRow("Confirm password", self.confirm_input)
        layout.addLayout(form)

        self.status_label = QLabel(vm_login_status(vm_name))
        self.status_label.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 11px;")
        layout.addWidget(self.status_label)

        self._prefill()

        # ── Buttons ──────────────────────────────────────────────────────────
        btn_row = QWidget()
        btn_row_layout = QHBoxLayout(btn_row)
        btn_row_layout.setContentsMargins(0, 0, 0, 0)
        btn_row_layout.setSpacing(8)

        self.save_btn = QPushButton("Save")
        self.save_btn.setFixedHeight(32)
        self.save_btn.setStyleSheet(
            f"QPushButton {{ background: {T.SUCCESS}; color: white; border: none;"
            f" border-radius: 4px; font-size: 12px; font-weight: 600; padding: 0 20px; }}"
            f"QPushButton:hover {{ background: #16a34a; }}"
        )
        self.save_btn.clicked.connect(self._save)

        self.clear_btn = QPushButton("Forget")
        self.clear_btn.setFixedHeight(32)
        self.clear_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.TEXT_SECONDARY};"
            f" border: 1px solid {T.BG_TERTIARY}; border-radius: 4px; font-size: 12px;"
            f" padding: 0 16px; }}"
            f"QPushButton:hover {{ color: {T.TEXT_PRIMARY}; }}"
        )
        self.clear_btn.clicked.connect(self._clear)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedHeight(32)
        cancel_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.TEXT_MUTED};"
            f" border: none; font-size: 12px; padding: 0 16px; }}"
            f"QPushButton:hover {{ color: {T.TEXT_SECONDARY}; }}"
        )
        cancel_btn.clicked.connect(self.reject)

        btn_row_layout.addWidget(self.save_btn)
        btn_row_layout.addWidget(self.clear_btn)
        btn_row_layout.addStretch()
        btn_row_layout.addWidget(cancel_btn)
        layout.addWidget(btn_row)

    def _prefill(self) -> None:
        """Show the saved username and the VM's configured SSH user.

        Never the password: echoing a stored secret into a visible field is the
        exposure the encryption exists to prevent.
        """
        try:
            creds = load_vm_login(self.vm_name)
        except RuntimeError:
            creds = None
        if creds:
            self.username_input.setText(creds[0])

    def _clear(self) -> None:
        store = CredentialStore()
        name = credential_name(self.vm_name)
        if store.get(name) is None:
            QMessageBox.information(
                self, "Nothing Stored", f"No saved login for '{self.vm_name}'."
            )
            return
        confirm = QMessageBox.question(
            self,
            "Forget Saved Login",
            f"Delete the stored username and password for '{self.vm_name}'?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm == QMessageBox.Yes:
            store.delete(name)
            self.username_input.clear()
            self.password_input.setText("")
            self.confirm_input.setText("")
            self.status_label.setText(f"Not saved for '{self.vm_name}'")
            QMessageBox.information(
                self, "Forgotten", f"Stored login for '{self.vm_name}' deleted."
            )

    def _save(self) -> None:
        username = self.username_input.text().strip()
        password = self.password_input.text()
        confirm = self.confirm_input.text()

        if not username:
            QMessageBox.warning(self, "Validation Error", "Username is required.")
            return
        if not password:
            QMessageBox.warning(self, "Validation Error", "Password is required.")
            return
        if password != confirm:
            # Caught here because the alternative is a password that silently
            # fails at the login prompt, with nothing on screen to explain it.
            QMessageBox.warning(self, "Validation Error", "The two passwords do not match.")
            return

        try:
            save_vm_login(self.vm_name, username, password)
        except Exception as exc:  # noqa: BLE001
            logger.error("failed to save credentials for {}: {}", self.vm_name, exc)
            QMessageBox.critical(
                self, "Error", f"Failed to save credentials for '{self.vm_name}': {exc}"
            )
            return

        QMessageBox.information(
            self, "Saved", f"Username and password saved for '{self.vm_name}'."
        )
        self.accept()