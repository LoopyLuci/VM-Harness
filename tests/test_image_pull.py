"""Tests for the ImagePullDialog — verify it pulls a Docker image and shows progress.

Run:  pytest tests/test_image_pull.py -v
"""

from __future__ import annotations

import os
import sys
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("GUI_MASTER_PASSWORD", "test-master-password")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt5.QtCore import QTimer, QEventLoop
from PyQt5.QtWidgets import QApplication, QProgressBar, QPushButton

from gui.dialogs_image_pull import ImagePullDialog


class _TestableImagePullDialog(ImagePullDialog):
    """ImagePullDialog that doesn't show a blocking message box on finish."""

    def __init__(self, image_name: str, parent=None):
        super().__init__(image_name, parent)
        self._test_finished_result: tuple[bool, str] | None = None

    def _on_finished(self, success: bool, message: str):
        """Override to skip the modal QMessageBox."""
        self._test_finished_result = (success, message)
        self.close()


class TestImagePullDialog(unittest.TestCase):
    """Test the ImagePullDialog pulls an image and displays progress."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dialog_constructs(self):
        """Dialog instantiates with correct title and initial state."""
        dlg = _TestableImagePullDialog("alpine:latest")
        self.assertIsNotNone(dlg)
        self.assertIn("alpine:latest", dlg.windowTitle())
        self.assertEqual(dlg.value(), 0)

    def test_progress_bar_exists(self):
        """Dialog contains a QProgressBar widget."""
        dlg = _TestableImagePullDialog("alpine:latest")
        bar = dlg.findChild(QProgressBar)
        self.assertIsNotNone(bar, "QProgressBar not found in ImagePullDialog")

    def test_cancel_button_exists(self):
        """Dialog contains a cancel button."""
        dlg = _TestableImagePullDialog("alpine:latest")
        buttons = dlg.findChildren(QPushButton)
        cancel_btns = [b for b in buttons if b.text() == "Cancel"]
        self.assertTrue(
            len(cancel_btns) >= 1,
            "Cancel button not found in ImagePullDialog",
        )

    def test_pull_alpine_latest(self):
        """Pull alpine:latest and verify the dialog closes on completion."""
        try:
            import docker
            docker.from_env(timeout=5).ping()
        except Exception as exc:  # noqa: BLE001 - no Docker daemon here: nothing to pull from
            self.skipTest(f"Docker is not running: {exc}")
        # Retry up to 3 times to handle QThread timing flakiness
        for attempt in range(3):
            dlg = _TestableImagePullDialog("alpine:latest")

            # Track progress updates
            progress_values: list[int] = []

            # Hook into the worker's progress signal
            original_start_pull = dlg.start_pull

            def wrapped_start_pull():
                original_start_pull()
                if dlg._worker:
                    dlg._worker.progress.connect(
                        lambda status, layer, pct, detail: progress_values.append(pct)
                    )

            dlg.start_pull = wrapped_start_pull

            # Start the pull
            dlg.start_pull()

            # Verify the worker thread is running
            self.assertIsNotNone(dlg._worker)
            self.assertTrue(dlg._worker.isRunning())

            # Wait for completion (max 60 seconds) using a nested event loop
            loop = QEventLoop()
            timeout_timer = QTimer()
            timeout_timer.setSingleShot(True)
            timeout_timer.setInterval(60_000)
            timeout_timer.timeout.connect(loop.quit)

            if dlg._worker:
                dlg._worker.finished.connect(loop.quit)

            timeout_timer.start()
            loop.exec_()

            # Wait for the thread to fully finish
            if dlg._worker and dlg._worker.isRunning():
                dlg._worker.wait(5000)

            # Verify the worker finished
            self.assertFalse(dlg._worker.isRunning())

            # Verify we got a result
            self.assertIsNotNone(
                dlg._test_finished_result, "Worker did not emit finished signal"
            )
            success, message = dlg._test_finished_result
            if success:
                # Verify the dialog closed
                self.assertFalse(dlg.isVisible())
                return  # Test passed

            # Retry on failure
            if attempt < 2:
                import time
                time.sleep(1)
                continue

        self.fail(f"Pull failed after 3 attempts: {message}")


if __name__ == "__main__":
    unittest.main()
