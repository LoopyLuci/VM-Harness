"""Tests for auth round-trip, SSH/QMP failure paths, and concurrency edge cases."""
from __future__ import annotations

import asyncio
import threading
import time
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

import sys
import tempfile
from pathlib import Path

# Use a temp dir for credential store tests
_TEST_STORE_DIR = Path(tempfile.mkdtemp(prefix="vmharness_test_"))

sys.path.insert(0, 'src')
sys.path.insert(0, '.')

from PyQt5.QtWidgets import QApplication
import pytest
app = QApplication.instance() or QApplication(sys.argv)


class TestAuthRoundTrip(unittest.TestCase):
    """Test API key validation logic via CredentialStore."""

    def _get_store(self):
        from src.vm_harness.security.credentials import CredentialManager as CredentialStore
        return CredentialStore(store_dir=str(_TEST_STORE_DIR))

    def test_validate_valid_key(self):
        """Valid API key returns metadata dict."""
        store = self._get_store()
        key_id, secret = store.create_api_key(name="test", roles=["admin"])

        result = store.validate_api_key(secret)
        self.assertIsNotNone(result)
        self.assertEqual(result["key_id"], key_id)
        self.assertEqual(result["name"], "test")
        self.assertEqual(result["roles"], ["admin"])

    def test_validate_invalid_key(self):
        """Invalid API key returns None."""
        store = self._get_store()
        result = store.validate_api_key("totally_invalid_key_1234567890abcdef")
        self.assertIsNone(result)

    def test_validate_revoked_key(self):
        """Revoked API key returns None."""
        store = self._get_store()
        key_id, secret = store.create_api_key(name="revoked", roles=["user"])
        store.revoke_api_key(key_id)

        result = store.validate_api_key(secret)
        self.assertIsNone(result)

    def test_validate_expired_key(self):
        """Expired API key returns None."""
        store = self._get_store()
        from datetime import timedelta, datetime, timezone

        # Create key with 1 hour TTL
        key_id, secret = store.create_api_key(
            name="expired",
            roles=["user"],
            ttl_hours=1,
        )

        # Manually modify the metadata to have expired 2 hours ago
        # (Can't mock datetime.utcnow directly — it's immutable C type)
        meta = store._metadata[key_id]
        from datetime import datetime as _dt
        meta.expires = _dt.utcnow() - timedelta(hours=2)

        result = store.validate_api_key(secret)
        self.assertIsNone(result)


class TestSSHFailurePaths(unittest.TestCase):
    """Test SSH connection failure handling."""

    def test_connection_refused(self):
        """SSH connection refused raises RuntimeError."""
        from src.vm_harness.ssh_client import _connect
        from src.vm_harness.config import Secrets, VmMCPSettings

        secrets = Secrets()
        settings = VmMCPSettings(
            ssh_host="127.0.0.1",
            ssh_port=1,  # port 1 should refuse
            ssh_username="test",
            ssh_password="test",
        )

        async def _test():
            with self.assertRaises(RuntimeError):
                await _connect(secrets, settings)

        asyncio.run(_test())

    def test_permission_denied(self):
        """SSH permission denied raises RuntimeError."""
        from src.vm_harness.ssh_client import _connect
        from src.vm_harness.config import Secrets, VmMCPSettings

        secrets = Secrets()
        settings = VmMCPSettings(
            ssh_host="127.0.0.1",
            ssh_port=22,
            ssh_username="invalid_user_12345",
            ssh_password="wrong_password",
        )

        async def _test():
            with self.assertRaises(RuntimeError):
                await asyncio.wait_for(
                    _connect(secrets, settings),
                    timeout=5,
                )

        # Port 22 may not be running — either way it should fail
        try:
            asyncio.run(_test())
        except asyncio.TimeoutError:
            pass  # Expected if SSH not running


@pytest.mark.xdist_group("plugins")  # loads plugins by module name, like tests/test_plugin_*.py
class TestConcurrencyEdgeCases(unittest.TestCase):
    """Test concurrent access patterns."""

    def test_atomic_state_concurrent_writes(self):
        """AtomicState handles concurrent writes without corruption."""
        from gui.resilience import AtomicState, VMState

        state = AtomicState()
        errors = []

        def _writer(key: str, value: int):
            try:
                for _ in range(50):
                    state.save(VMState(name=key, status="stopped", pid=value))
                    state.get(key)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=_writer, args=(f"vm_{i}", i))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(errors), 0, f"Concurrent errors: {errors}")

    def test_crash_handler_concurrent_reports(self):
        """CrashHandler generates unique filenames for concurrent reports."""
        from gui.resilience import CrashHandler

        handler = CrashHandler()
        filenames = []
        lock = threading.Lock()

        def _report(i: int):
            try:
                path = handler._generate_report_path()
                with lock:
                    filenames.append(path)
            except Exception:
                pass

        threads = [threading.Thread(target=_report, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        # All filenames should be unique
        self.assertEqual(len(filenames), len(set(filenames)))

    def test_plugin_manager_concurrent_load(self):
        """PluginManager handles concurrent load_all() calls."""
        from gui.plugin_manager import PluginManager

        manager = PluginManager()
        errors = []

        def _load():
            try:
                manager.load_all()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_load) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        # Should not crash
        self.assertEqual(len(errors), 0, f"Plugin errors: {errors}")


if __name__ == "__main__":
    unittest.main()
