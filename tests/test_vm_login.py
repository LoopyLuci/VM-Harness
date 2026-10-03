"""Tests for the VM console credentials dialog, and signing in with them.

The behaviour worth pinning is the update-on-resave path. `CredentialStore.add`
documents that it updates an existing name but returns it untouched in
practice (credential_store.py:186), so a password change routed through it
would report success, keep the old secret, and fail later at a login prompt
with nothing on screen to explain why.

The login tests assert on the exact keystroke sequence. A login that types the
wrong thing fails silently, and the only symptom is a screen that never
changes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


class FakeQMP:
    """Records human-monitor commands and writes screendump files."""

    def __init__(self, frame: bytes | None = None):
        self.commands: list[str] = []
        self._frame = frame
        self._n = 0

    async def send(self, cmd, args=None):
        args = args or {}
        if cmd == "screendump":
            self._n += 1
            payload = self._frame if self._frame is not None else f"frame{self._n}".encode()
            Path(args["filename"]).write_bytes(payload)
            return {"return": {}}
        self.commands.append(args["command-line"])
        return {"return": {}}


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Point the credential store at a temp dir with its own master key."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("GUI_MASTER_PASSWORD", raising=False)
    return tmp_path


@pytest.fixture
def dialog(qtbot, isolated_store):
    from gui.dialogs_vm_login import VMLoginCredentialsDialog

    widget = VMLoginCredentialsDialog()
    qtbot.addWidget(widget)
    return widget


@pytest.fixture
def no_popups(monkeypatch):
    """Silence the modal message boxes the dialog raises on save."""
    monkeypatch.setattr("gui.dialogs_vm_login.QMessageBox.information", lambda *a, **k: None)
    monkeypatch.setattr("gui.dialogs_vm_login.QMessageBox.warning", lambda *a, **k: None)
    monkeypatch.setattr("gui.dialogs_vm_login.QMessageBox.critical", lambda *a, **k: None)


class TestSaveAndLoad:
    def test_round_trip(self, isolated_store):
        from gui.dialogs_vm_login import load_vm_login, save_vm_login

        assert load_vm_login() is None
        save_vm_login("omarchy-vm", "S3cret!Pass#2026")
        assert load_vm_login() == ("omarchy-vm", "S3cret!Pass#2026")

    def test_resaving_updates_the_password(self, isolated_store):
        """The regression: a changed password must replace the old one."""
        from gui.dialogs_vm_login import load_vm_login, save_vm_login

        save_vm_login("omarchy-vm", "first-password")
        save_vm_login("omarchy-vm", "second-password")
        assert load_vm_login() == ("omarchy-vm", "second-password")

    def test_password_is_encrypted_at_rest(self, isolated_store):
        from gui.dialogs_vm_login import save_vm_login

        save_vm_login("omarchy-vm", "PlaintextCanary123!")
        raw = next(isolated_store.rglob("credentials.json")).read_text()
        assert "PlaintextCanary123!" not in raw
        assert json.loads(raw)["credentials"]

    def test_status_never_leaks_the_password(self, isolated_store):
        from gui.dialogs_vm_login import save_vm_login, vm_login_status

        save_vm_login("omarchy-vm", "PlaintextCanary123!")
        status = vm_login_status()
        assert "PlaintextCanary123!" not in status
        assert "omarchy-vm" in status

    def test_empty_store_reports_not_set(self, isolated_store):
        from gui.dialogs_vm_login import vm_login_status

        assert vm_login_status() == "(not set)"

    def test_corrupt_payload_raises_instead_of_returning_garbage(self, isolated_store):
        from gui.credential_store import CredentialStore
        from gui.dialogs_vm_login import VM_LOGIN_CREDENTIAL, load_vm_login

        store = CredentialStore()
        store.add(VM_LOGIN_CREDENTIAL, "password", "not json at all", "")
        with pytest.raises(RuntimeError, match="malformed"):
            load_vm_login()


class TestDialog:
    def test_constructs(self, dialog):
        assert dialog.windowTitle() == "VM Console Login"

    def test_password_is_not_prefilled(self, isolated_store):
        """Echoing a stored secret back into a visible field undoes the point."""
        from gui.dialogs_vm_login import VMLoginCredentialsDialog, save_vm_login

        save_vm_login("omarchy-vm", "stored-secret")
        reopened = VMLoginCredentialsDialog()
        assert reopened.username_input.text() == "omarchy-vm"
        assert reopened.password_input.text() == ""

    def test_mismatched_confirmation_is_not_saved(self, dialog, isolated_store, no_popups):
        from gui.dialogs_vm_login import load_vm_login

        dialog.username_input.setText("omarchy-vm")
        dialog.password_input.setText("one")
        dialog.confirm_input.setText("two")
        dialog._save()
        assert load_vm_login() is None

    def test_valid_save_persists(self, dialog, isolated_store, no_popups):
        from gui.dialogs_vm_login import VMLoginCredentialsDialog, load_vm_login

        dialog.username_input.setText("omarchy-vm")
        dialog.password_input.setText("pw-123")
        dialog.confirm_input.setText("pw-123")
        dialog._save()
        assert load_vm_login() == ("omarchy-vm", "pw-123")
        assert VMLoginCredentialsDialog().username_input.text() == "omarchy-vm"

    def test_username_is_required(self, dialog, isolated_store, no_popups):
        from gui.dialogs_vm_login import load_vm_login

        dialog.username_input.setText("")
        dialog.password_input.setText("pw")
        dialog.confirm_input.setText("pw")
        dialog._save()
        assert load_vm_login() is None


class TestPanelWiring:
    def test_vm_control_panel_exposes_the_dialog(self, qtbot, isolated_store):
        """The button is the only way a user reaches this from the GUI."""
        from gui.dialogs_vm_login import VMLoginCredentialsDialog
        from gui.panels_vm_control import VMControlPanel

        panel = VMControlPanel()
        qtbot.addWidget(panel)
        assert panel.console_login_btn.text().startswith("Console Login")

        opened: list[object] = []
        import gui.dialogs_vm_login as module

        original = module.VMLoginCredentialsDialog

        class _Spy(original):
            def __init__(self, parent=None):
                super().__init__(parent)
                opened.append(self)

            def exec_(self):
                return 0  # cancel immediately

        module.VMLoginCredentialsDialog = _Spy
        try:
            panel._on_console_login()
        finally:
            module.VMLoginCredentialsDialog = original
        assert len(opened) == 1


class TestAutologin:
    def test_sends_username_then_tab_then_password_then_enter(self, tmp_path):
        from vm_harness.autologin import ConsoleLogin, login

        client = FakeQMP()
        asyncio_run(login(client, ConsoleLogin("ab", "cd", settle_sec=0, shot_dir=tmp_path)))
        assert client.commands == [
            "sendkey a", "sendkey b", "sendkey tab",
            "sendkey c", "sendkey d", "sendkey ret",
        ]

    def test_can_skip_the_username(self, tmp_path):
        """A greeter showing only a password box must not receive a username."""
        from vm_harness.autologin import ConsoleLogin, login

        client = FakeQMP()
        asyncio_run(
            login(
                client,
                ConsoleLogin("ignored", "pw", type_username=False, settle_sec=0, shot_dir=tmp_path),
            )
        )
        assert client.commands == ["sendkey p", "sendkey w", "sendkey ret"]

    def test_reports_an_unchanged_screen(self, tmp_path):
        """Identical frames mean the prompt redrew itself: the login failed."""
        from vm_harness.autologin import ConsoleLogin, login

        client = FakeQMP(frame=b"identical")
        result = asyncio_run(
            login(client, ConsoleLogin("u", "p", settle_sec=0, shot_dir=tmp_path))
        )
        assert result["screen_changed"] is False

    def test_reports_a_changed_screen(self, tmp_path):
        from vm_harness.autologin import ConsoleLogin, login

        result = asyncio_run(
            login(FakeQMP(), ConsoleLogin("u", "p", settle_sec=0, shot_dir=tmp_path))
        )
        assert result["screen_changed"] is True

    def test_survives_a_failed_screendump(self, tmp_path):
        """Losing the evidence must not abort a login that may have worked."""

        class _NoShots(FakeQMP):
            async def send(self, cmd, args=None):
                if cmd == "screendump":
                    return {"return": {}}  # creates no file
                return await super().send(cmd, args)

        from vm_harness.autologin import ConsoleLogin, login

        result = asyncio_run(
            login(_NoShots(), ConsoleLogin("u", "p", settle_sec=0, shot_dir=tmp_path))
        )
        assert result["screen_changed"] is None


def asyncio_run(coro):
    """Drive an async test body, so these need no per-test asyncio plumbing."""
    import asyncio

    return asyncio.run(coro)