"""Dialog for the credentials used to log in to a VM's console.

Unattended installs create an account and then stop at a login prompt, and
everything after that -- installing packages, checking that a desktop actually
came up, driving an installer that asks a question -- needs someone to type a
password into a framebuffer nobody is watching. This dialog is where that
password is entered once so it can be typed on demand afterwards.

The value goes into the existing encrypted CredentialStore rather than a new
file, so it is Fernet-encrypted at rest with the same key material and the same
0600 permissions as every other secret the harness keeps.

Two notes on the security posture, because "saved properly" is worth being
precise about:

The store derives its key from GUI_MASTER_PASSWORD if that is set, otherwise
from a random per-installation key written to `.master_key` beside the
credentials file. The second mode is what makes unattended use possible at
all -- an operator or agent can read the credential later without a human
present -- but it also means the key sits on the same disk as the data. It
protects against a credentials file leaking on its own, not against someone
who already has the account. Setting GUI_MASTER_PASSWORD buys real protection
and costs unattended access; that is the user's call, not a default.

Nothing here ever logs or displays the password. It is not echoed into the
GUI status line, not put in a traceback, and not written anywhere except the
encrypted store.
"""
from __future__ import annotations

import json

from loguru import logger
from PyQt5.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QWidget,
)

from gui.credential_store import CredentialStore
from gui.theme import T
from gui.widgets import PasswordInput, TextInput

# One credential, not two: the pair has to stay together, and splitting it
# across two names in a flat namespace invites saving a new username against
# an old password.
VM_LOGIN_CREDENTIAL = "vm-console-login"

_UNSET = "(not set)"


def save_vm_login(username: str, password: str, description: str = "") -> str:
    """Persist VM console credentials, replacing any previous pair.

    Uses update-then-add rather than CredentialStore.add() alone. add()
    documents that it updates the value when a name already exists, but in
    practice it returns the existing entry untouched (credential_store.py:186),
    so saving a changed password through it would report success and keep the
    old one -- and the next login would fail for no visible reason.
    """
    store = CredentialStore()
    payload = json.dumps({"username": username, "password": password})
    note = description or f"Console login for {username}"
    if store.get(VM_LOGIN_CREDENTIAL) is not None:
        store.update(VM_LOGIN_CREDENTIAL, payload, note)
    else:
        store.add(VM_LOGIN_CREDENTIAL, "password", payload, note)
    logger.info("saved VM console credentials for user '{}'", username)
    return VM_LOGIN_CREDENTIAL


def load_vm_login() -> tuple[str, str] | None:
    """Return (username, password), or None if nothing is stored.

    Intentionally callable without a GUI or a human present, since that is the
    point: this is how an unattended run gets the password back out.
    """
    store = CredentialStore()
    cred = store.get(VM_LOGIN_CREDENTIAL)
    if cred is None:
        return None
    # get() substitutes a placeholder rather than raising when the key is wrong.
    if cred.value == "[decryption failed]":
        raise RuntimeError(
            "stored VM credentials could not be decrypted: the master password "
            "or .master_key does not match the ones they were saved with"
        )
    try:
        data = json.loads(cred.value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"stored VM credentials are malformed ({exc}); re-enter them in the GUI"
        ) from exc
    username = str(data.get("username", ""))
    password = str(data.get("password", ""))
    if not username or not password:
        return None
    return username, password


def vm_login_status() -> str:
    """Human-readable state for the panel, never including the password."""
    try:
        creds = load_vm_login()
    except RuntimeError as exc:
        return f"Unreadable: {exc}"
    if creds is None:
        return _UNSET
    return f"Saved for {creds[0]}"


class VMLoginCredentialsDialog(QDialog):
    """Enter and save the VM console username and password."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("VM Console Login")
        self.setMinimumWidth(460)
        self.setStyleSheet(f"QDialog {{ background: {T.BG_PRIMARY}; }}")

        layout = QFormLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        header = QLabel(
            "Credentials for logging in to a VM's console, stored encrypted so "
            "the harness can sign in unattended."
        )
        header.setWordWrap(True)
        header.setStyleSheet(f"color: {T.TEXT_SECONDARY}; font-size: 12px;")
        layout.addRow(header)

        # A username is not a secret, and masking it turns a typo into an
        # invisible one. PasswordInput is only for the two password fields.
        self.username_input = TextInput("Console username")
        layout.addRow("Username *", self.username_input)

        self.password_input = PasswordInput("Console password")
        layout.addRow("Password *", self.password_input)

        self.confirm_input = PasswordInput("Repeat the password")
        layout.addRow("Confirm *", self.confirm_input)

        self.status_label = QLabel(vm_login_status())
        self.status_label.setStyleSheet(f"color: {T.TEXT_MUTED}; font-size: 11px;")
        layout.addRow(self.status_label)

        self._prefill()

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
        layout.addRow(btn_row)

    def _prefill(self) -> None:
        """Show the stored username so it does not have to be retyped.

        The password is deliberately not prefilled: echoing a stored secret
        into a visible field is exactly the exposure the encryption avoids.
        """
        try:
            creds = load_vm_login()
        except RuntimeError:
            creds = None
        if creds:
            self.username_input.setText(creds[0])

    def _clear(self) -> None:
        store = CredentialStore()
        if store.get(VM_LOGIN_CREDENTIAL) is None:
            QMessageBox.information(self, "Nothing Stored", "There is no saved login to forget.")
            return
        confirm = QMessageBox.question(
            self,
            "Forget Saved Login",
            "Delete the stored console username and password?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm == QMessageBox.Yes:
            store.delete(VM_LOGIN_CREDENTIAL)
            self.username_input.clear()
            self.password_input.setText("")
            self.confirm_input.setText("")
            self.status_label.setText(_UNSET)
            QMessageBox.information(self, "Forgotten", "Stored console login deleted.")

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
            save_vm_login(username, password)
        except Exception as exc:
            logger.error("failed to save VM console credentials: {}", exc)
            QMessageBox.critical(self, "Error", f"Failed to save credentials: {exc}")
            return

        QMessageBox.information(
            self, "Saved", f"Console login saved for '{username}'."
        )
        self.accept()