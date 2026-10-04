"""Performance test: switch through all panels, measure memory, detect timer leaks.

Run with:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_performance.py -v
"""
from __future__ import annotations

import gc
import os
import sys
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "*.debug=false")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import psutil  # noqa: E402
from PyQt5.QtCore import QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402


def _get_rss_mb() -> float:
    """Return current process RSS in megabytes."""
    return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)


def _count_active_timers(widget) -> int:
    """Recursively count QTimer children that are active."""
    count = 0
    for child in widget.findChildren(QTimer):
        if child.isActive():
            count += 1
    return count


class TestPanelPerformance(unittest.TestCase):
    """Switch through all panels and check for memory leaks / slow rendering."""

    SLOW_PANEL_THRESHOLD_S = 1.0
    MEMORY_LEAK_THRESHOLD_MB = 100.0

    @classmethod
    def setUpClass(cls):
        cls._owns_app = QApplication.instance() is None
        cls.app = QApplication.instance() or QApplication(sys.argv)
        cls.app.setQuitOnLastWindowClosed(False)

    @classmethod
    def tearDownClass(cls):
        """Only quit a QApplication this class created.

        This used to call quit() unconditionally. Under pytest the QApplication
        is shared across the whole session, and quitting it breaks every GUI
        test that runs afterwards: newly created widgets stop getting valid
        geometry, so mapFromScene and layout return nonsense. That is why 27
        console-panel tests passed on their own and failed in the full suite.

        It was also not doing what the docstring claimed. quit() only affects a
        running event loop, and there is none here; the actual protection against
        lingering timers is closing the windows, which _cleanup_window does.
        """
        if cls._owns_app:
            cls.app.quit()

    def test_panel_switching_performance(self):
        """Main performance test: cycle through all panels, measure time & memory."""
        from gui.main_window import MainWindow

        # ── Create MainWindow ────────────────────────────────────────────────
        window = MainWindow()
        self.addCleanup(self._cleanup_window, window)

        panel_names = list(window.panels.keys())
        # The panel_list in main_window.py defines 33 panels.
        # (The "34th" is the SparklineWidget tested separately in test_all_panels.py.)
        self.assertGreaterEqual(
            len(panel_names),
            33,
            f"Expected at least 33 panels, found {len(panel_names)}: {panel_names}",
        )

        # ── Baseline memory (after window creation) ─────────────────────────
        gc.collect()
        time.sleep(0.1)
        rss_before = _get_rss_mb()

        # ── Switch through all panels ────────────────────────────────────────
        slow_panels: list[tuple[str, float]] = []
        switch_times: dict[str, float] = {}

        for name in panel_names:
            t0 = time.perf_counter()
            window._switch_panel(name)
            # Process events so the panel actually renders
            self.app.processEvents()
            elapsed = time.perf_counter() - t0
            switch_times[name] = elapsed

            if elapsed > self.SLOW_PANEL_THRESHOLD_S:
                slow_panels.append((name, elapsed))

        # ── Memory after full cycle ──────────────────────────────────────────
        gc.collect()
        time.sleep(0.1)
        rss_after = _get_rss_mb()
        rss_growth = rss_after - rss_before

        # ── Timer leak check ─────────────────────────────────────────────────
        # Count active timers on the main window (includes panel timers)
        active_timers = _count_active_timers(window)

        # ── Report ───────────────────────────────────────────────────────────
        print("\n" + "=" * 70)
        print("PANEL SWITCHING PERFORMANCE REPORT")
        print("=" * 70)
        print(f"Total panels: {len(panel_names)}")
        print(f"RSS before: {rss_before:.1f} MB")
        print(f"RSS after:  {rss_after:.1f} MB")
        print(f"RSS growth: {rss_growth:+.1f} MB")
        print(f"Active timers: {active_timers}")
        print("-" * 70)

        # Sort by switch time descending
        sorted_times = sorted(switch_times.items(), key=lambda x: x[1], reverse=True)
        print("Top 10 slowest panel switches:")
        for name, t in sorted_times[:10]:
            marker = " [SLOW]" if t > self.SLOW_PANEL_THRESHOLD_S else ""
            print(f"  {name:30s} {t*1000:8.1f} ms{marker}")

        if slow_panels:
            print(f"\nSLOW PANELS (>{self.SLOW_PANEL_THRESHOLD_S}s):")
            for name, t in slow_panels:
                print(f"  {name}: {t:.3f}s")
        else:
            print(f"\nNo panels exceeded {self.SLOW_PANEL_THRESHOLD_S}s threshold.")

        print("=" * 70)

        # ── Assertions ───────────────────────────────────────────────────────
        # Memory leak: RSS growth should be reasonable
        self.assertLess(
            rss_growth,
            self.MEMORY_LEAK_THRESHOLD_MB,
            f"Memory leak detected: RSS grew by {rss_growth:.1f} MB "
            f"(threshold: {self.MEMORY_LEAK_THRESHOLD_MB} MB)",
        )

        # Timer leak: with 34 panels loaded, we expect some active timers
        # (container stats, k8s tree, terminal, telemetry, etc.) but not
        # an unbounded number. A reasonable upper bound is ~100.
        self.assertLess(
            active_timers,
            100,
            f"Possible timer leak: {active_timers} active timers "
            f"(expected < 100 for 34 panels)",
        )

        # No panel should take more than 5 seconds (hard fail)
        for name, t in switch_times.items():
            self.assertLess(
                t,
                5.0,
                f"Panel '{name}' took {t:.3f}s to switch (max 5s)",
            )

    def test_panel_switching_idempotent(self):
        """Switching to the same panel twice should not leak memory or timers."""
        from gui.main_window import MainWindow

        window = MainWindow()
        self.addCleanup(self._cleanup_window, window)

        # Baseline after window creation
        gc.collect()
        time.sleep(0.1)
        rss_before = _get_rss_mb()

        # Switch to dashboard 5 times
        for _ in range(5):
            window._switch_panel("dashboard")
            self.app.processEvents()

        gc.collect()
        time.sleep(0.1)
        rss_after = _get_rss_mb()
        rss_growth = rss_after - rss_before

        self.assertLess(
            rss_growth,
            30.0,
            f"Memory grew by {rss_growth:.1f} MB after 5 switches to same panel",
        )

    def test_rapid_panel_switching(self):
        """Rapidly switch through all panels 3 times — check for degradation."""
        from gui.main_window import MainWindow

        window = MainWindow()
        self.addCleanup(self._cleanup_window, window)

        panel_names = list(window.panels.keys())

        cycle_times: list[float] = []
        for cycle in range(3):
            t0 = time.perf_counter()
            for name in panel_names:
                window._switch_panel(name)
                self.app.processEvents()
            elapsed = time.perf_counter() - t0
            cycle_times.append(elapsed)

        print(f"\nRapid switching cycles: {[f'{t:.3f}s' for t in cycle_times]}")

        # Each cycle should complete in < 30 seconds
        for i, t in enumerate(cycle_times):
            self.assertLess(
                t,
                30.0,
                f"Cycle {i+1} took {t:.3f}s (max 30s for 34 panels)",
            )

        # Cycle 3 should not be more than 3x slower than cycle 1
        if cycle_times[0] > 0:
            ratio = cycle_times[2] / cycle_times[0]
            self.assertLess(
                ratio,
                3.0,
                f"Cycle 3 was {ratio:.1f}x slower than cycle 1 "
                f"({cycle_times[2]:.3f}s vs {cycle_times[0]:.3f}s) — possible leak",
            )

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _cleanup_window(window):
        """Properly clean up a MainWindow instance."""
        try:
            window.close()
            window.deleteLater()
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
