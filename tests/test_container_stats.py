"""Tests for the ContainerStatsPanel detail view.

Verifies:
- Table structure (2 columns: Container, Status)
- Detail view contains CPU and memory sparklines
- Selecting a container populates CPU/memory labels with stats data

Run:  pytest tests/test_container_stats.py -v
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("GUI_MASTER_PASSWORD", "test-master-password")

# Ensure the project root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt5.QtWidgets import QApplication, QTableWidgetItem  # noqa: E402


def _make_mock_adapter(containers: list[dict], stats: dict) -> MagicMock:
    """Build a mock adapter that returns the given containers and stats."""
    adapter = MagicMock()
    adapter.docker.list_containers.return_value = containers
    adapter.docker.get_stats.return_value = stats
    return adapter


class TestContainerStatsPanel(unittest.TestCase):
    """Test the ContainerStatsPanel UI and detail-view behavior."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        # Default mock data — one running container with known stats
        self.containers = [
            {"name": "test-container-1", "status": "running"},
            {"name": "test-container-2", "status": "exited"},
        ]
        self.stats_data = {
            "cpu_percent": "42%",
            "memory_usage": "512 MB",
            "status": "running",
            "uptime": "2h 15m",
            "image": "ubuntu:latest",
        }
        self.mock_adapter = _make_mock_adapter(self.containers, self.stats_data)
        # Patch get_adapter for the entire test lifetime (the panel calls it lazily)
        patcher = patch("gui.async_adapter.get_adapter", return_value=self.mock_adapter)
        self.mock_get_adapter = patcher.start()
        self.addCleanup(patcher.stop)

    def _build_panel(self):
        """Instantiate the panel (patch already active from setUp)."""
        from gui.panels_container_stats import ContainerStatsPanel
        panel = ContainerStatsPanel()
        # Panels load their data when shown (a hidden panel does not poll its backends).
        panel.show()
        from PyQt5.QtWidgets import QApplication
        QApplication.processEvents()
        return panel

    # ── Test 1: Panel constructs and table has correct structure ───────────

    def test_table_has_two_columns(self):
        panel = self._build_panel()
        self.assertEqual(panel._table.columnCount(), 2)

    def test_table_headers(self):
        panel = self._build_panel()
        header_model = panel._table.model()
        self.assertEqual(header_model.headerData(0, 1, 0), "Container")  # Qt.Horizontal=1
        self.assertEqual(header_model.headerData(1, 1, 0), "Status")

    # ── Test 2: Detail view contains sparklines ────────────────────────────

    def test_detail_view_has_cpu_sparkline(self):
        panel = self._build_panel()
        self.assertIsNotNone(panel._cpu_sparkline)

    def test_detail_view_has_memory_sparkline(self):
        panel = self._build_panel()
        self.assertIsNotNone(panel._mem_sparkline)

    # ── Test 3: Selecting a container populates CPU/memory labels ──────────

    def test_selecting_container_updates_cpu_label(self):
        panel = self._build_panel()
        # The table should be populated with our mock containers
        self.assertGreater(panel._table.rowCount(), 0)

        # Directly invoke the detail loader (more reliable than signal emission)
        panel._load_container_detail("test-container-1")

        # The CPU label should reflect the mock stats
        self.assertIn("42", panel._cpu_label.text())

    def test_selecting_container_updates_memory_label(self):
        panel = self._build_panel()
        panel._load_container_detail("test-container-1")

        self.assertIn("512", panel._mem_label.text())

    def test_selecting_container_updates_detail_info(self):
        panel = self._build_panel()
        panel._load_container_detail("test-container-1")

        detail_text = panel._detail_info.text()
        self.assertIn("test-container-1", detail_text)
        self.assertIn("running", detail_text)

    # ── Test 4: Sparkline receives data after selection ────────────────────

    def test_cpu_sparkline_receives_data(self):
        panel = self._build_panel()
        panel._load_container_detail("test-container-1")

        # The sparkline's internal data list should have grown
        self.assertGreater(len(panel._cpu_sparkline._data), 0)

    def test_memory_sparkline_receives_data(self):
        panel = self._build_panel()
        panel._load_container_detail("test-container-1")

        self.assertGreater(len(panel._mem_sparkline._data), 0)

    # ── Test 5: Numeric stats (no % suffix) also work ──────────────────────

    def test_numeric_stats_values(self):
        """get_stats may return plain numbers — the panel should handle both."""
        numeric_stats = {
            "cpu_percent": 15,
            "memory_usage": 128,
            "status": "running",
            "uptime": "1h",
            "image": "alpine",
        }
        self.mock_adapter.docker.get_stats.return_value = numeric_stats

        panel = self._build_panel()
        panel._load_container_detail("test-container-1")

        self.assertIn("15", panel._cpu_label.text())
        self.assertIn("128", panel._mem_label.text())


if __name__ == "__main__":
    unittest.main()
