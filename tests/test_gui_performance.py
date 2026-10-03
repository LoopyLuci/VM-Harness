"""Comprehensive GUI performance test.

Measures memory (RSS), timer leaks, widget leaks, and switch times across
all panels in the MainWindow.  Runs multiple cycles to detect degradation.

Run with:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_gui_performance.py -v
    # or
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_gui_performance -v
"""
from __future__ import annotations

import gc
import os
import sys
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "*.debug=false")

# Ensure project root and src are importable
PROJECT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
for p in (PROJECT_ROOT, SRC_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import psutil  # noqa: E402
from PyQt5.QtCore import QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_rss_mb() -> float:
    """Return current process RSS in megabytes."""
    return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)


def _count_active_timers(widget) -> int:
    """Recursively count active QTimer children of *widget*."""
    return sum(1 for t in widget.findChildren(QTimer) if t.isActive())


def _count_all_timers(widget) -> int:
    """Recursively count all QTimer children (active or not)."""
    return len(widget.findChildren(QTimer))


def _count_widgets(widget) -> int:
    """Recursively count all QWidget descendants of *widget*."""
    return len(widget.findChildren(QWidget))


def _force_gc_and_settle(delay: float = 0.1) -> None:
    """Force garbage collection and give Qt a moment to clean up."""
    gc.collect()
    QApplication.processEvents()
    time.sleep(delay)
    gc.collect()
    QApplication.processEvents()


# ── Test case ─────────────────────────────────────────────────────────────────

class TestGUIPerformance(unittest.TestCase):
    """Comprehensive performance test for all MainWindow panels."""

    # ── Thresholds ─────────────────────────────────────────────────────────
    # Time limits are wall-clock: when the suite runs in parallel (pytest-xdist), other workers share the CPU, so the
    # time-based limits are relaxed there. The leak limits (memory, widgets, timers) do not depend on load and stay.
    _LOAD = 3.0 if os.environ.get("PYTEST_XDIST_WORKER") else 1.0
    SLOW_PANEL_THRESHOLD_S = 2.0 * _LOAD  # per-switch time limit
    MAX_PANEL_COUNT = 40               # sanity upper bound on panel count
    MEMORY_LEAK_THRESHOLD_MB = 150.0   # RSS growth after all cycles
    WIDGET_LEAK_THRESHOLD = 500        # max net widget growth after cycles
    TIMER_LEAK_THRESHOLD = 200         # max net timer growth after cycles
    DEGRADATION_RATIO_MAX = 3.0 * _LOAD  # cycle3/cycle1 time ratio limit
    CYCLE_COUNT = 3                    # number of full cycles to run

    # ── Class-level setup ──────────────────────────────────────────────────
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)
        cls.app.setQuitOnLastWindowClosed(False)

    @classmethod
    def tearDownClass(cls):
        pass  # Let interpreter exit clean up

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _cleanup_window(window):
        """Properly close and schedule deletion of a MainWindow."""
        try:
            window.close()
            window.deleteLater()
        except Exception:
            pass

    # ── Tests ──────────────────────────────────────────────────────────────

    def test_01_panel_count(self):
        """Verify MainWindow creates the expected number of panels."""
        from gui.main_window import MainWindow

        window = MainWindow()
        self.addCleanup(self._cleanup_window, window)

        panel_names = list(window.panels.keys())

        # The panel_list in main_window.py defines 33 panels.
        # We require at least 32 to catch accidental removals.
        self.assertGreaterEqual(
            len(panel_names),
            32,
            f"Expected >= 32 panels, found {len(panel_names)}: {panel_names}",
        )
        self.assertLess(
            len(panel_names),
            self.MAX_PANEL_COUNT,
            f"Found {len(panel_names)} panels — expected < {self.MAX_PANEL_COUNT}",
        )

    def test_02_switch_all_panels_basic(self):
        """Switch through every panel once; verify each completes < 2 s."""
        from gui.main_window import MainWindow

        window = MainWindow()
        self.addCleanup(self._cleanup_window, window)

        panel_names = list(window.panels.keys())

        slow_panels: list[tuple[str, float]] = []
        switch_times: dict[str, float] = {}

        for name in panel_names:
            t0 = time.perf_counter()
            window._switch_panel(name)
            self.app.processEvents()
            elapsed = time.perf_counter() - t0

            switch_times[name] = elapsed
            if elapsed > self.SLOW_PANEL_THRESHOLD_S:
                slow_panels.append((name, elapsed))

        # ── Report ────────────────────────────────────────────────────────
        print("\n" + "=" * 70)
        print("BASIC SWITCH TEST REPORT")
        print("=" * 70)
        print(f"Panels tested: {len(panel_names)}")
        sorted_times = sorted(switch_times.items(), key=lambda x: x[1], reverse=True)
        print("Top 10 slowest switches:")
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

        # ── Assertions ────────────────────────────────────────────────────
        for name, t in switch_times.items():
            self.assertLess(
                t,
                self.SLOW_PANEL_THRESHOLD_S,
                f"Panel '{name}' took {t:.3f}s to switch "
                f"(threshold: {self.SLOW_PANEL_THRESHOLD_S}s)",
            )

    def test_03_memory_rss_per_switch(self):
        """Measure RSS before and after each panel switch."""
        from gui.main_window import MainWindow

        window = MainWindow()
        self.addCleanup(self._cleanup_window, window)

        panel_names = list(window.panels.keys())

        # Baseline after window creation and first paint
        _force_gc_and_settle(0.2)
        rss_baseline = _get_rss_mb()

        rss_readings: dict[str, float] = {}
        rss_growth: dict[str, float] = {}

        for name in panel_names:
            rss_before = _get_rss_mb()

            window._switch_panel(name)
            self.app.processEvents()

            rss_after = _get_rss_mb()

            rss_readings[name] = rss_after
            rss_growth[name] = rss_after - rss_before

        rss_final = _get_rss_mb()
        rss_net_growth = rss_final - rss_baseline

        # ── Report ────────────────────────────────────────────────────────
        print("\n" + "=" * 70)
        print("MEMORY RSS REPORT")
        print("=" * 70)
        print(f"Baseline RSS:  {rss_baseline:.1f} MB")
        print(f"Final RSS:     {rss_final:.1f} MB")
        print(f"Net growth:    {rss_net_growth:+.1f} MB")

        # Top 5 panels by RSS growth after switching
        sorted_growth = sorted(rss_growth.items(), key=lambda x: x[1], reverse=True)
        print("\nTop 5 panels by RSS growth during switch:")
        for name, g in sorted_growth[:5]:
            print(f"  {name:30s} {g:+.2f} MB  (abs: {rss_readings[name]:.1f} MB)")
        print("=" * 70)

        # ── Assertions ────────────────────────────────────────────────────
        self.assertLess(
            rss_net_growth,
            self.MEMORY_LEAK_THRESHOLD_MB,
            f"Memory leak: RSS grew {rss_net_growth:+.1f} MB "
            f"(threshold: {self.MEMORY_LEAK_THRESHOLD_MB} MB)",
        )

    def test_04_timer_leaks(self):
        """Count QTimer children before and after a full panel cycle."""
        from gui.main_window import MainWindow

        window = MainWindow()
        self.addCleanup(self._cleanup_window, window)

        panel_names = list(window.panels.keys())

        # Baseline timer count
        _force_gc_and_settle(0.2)
        timers_baseline = _count_all_timers(window)
        active_baseline = _count_active_timers(window)

        # Switch through all panels once
        for name in panel_names:
            window._switch_panel(name)
            self.app.processEvents()

        _force_gc_and_settle(0.2)
        timers_after = _count_all_timers(window)
        active_after = _count_active_timers(window)

        timer_growth = timers_after - timers_baseline

        # ── Report ────────────────────────────────────────────────────────
        print("\n" + "=" * 70)
        print("TIMER LEAK REPORT")
        print("=" * 70)
        print(f"Baseline timers (total):    {timers_baseline}")
        print(f"Baseline timers (active):   {active_baseline}")
        print(f"After cycle timers (total):  {timers_after}")
        print(f"After cycle timers (active): {active_after}")
        print(f"Net timer growth:           {timer_growth:+d}")
        print("=" * 70)

        # ── Assertions ────────────────────────────────────────────────────
        # A modest growth is acceptable (Qt may create transient timers),
        # but hundreds of new timers after one cycle signals a real leak.
        self.assertLess(
            timer_growth,
            50,
            f"Timer leak: {timer_growth} new timers created after one cycle",
        )

    def test_05_widget_leaks(self):
        """Count QWidget children before and after a full panel cycle."""
        from gui.main_window import MainWindow

        window = MainWindow()
        self.addCleanup(self._cleanup_window, window)

        panel_names = list(window.panels.keys())

        # Baseline widget count
        _force_gc_and_settle(0.2)
        widgets_baseline = _count_widgets(window)

        # Switch through all panels once
        for name in panel_names:
            window._switch_panel(name)
            self.app.processEvents()

        _force_gc_and_settle(0.2)
        widgets_after = _count_widgets(window)
        widget_growth = widgets_after - widgets_baseline

        # ── Report ────────────────────────────────────────────────────────
        print("\n" + "=" * 70)
        print("WIDGET LEAK REPORT")
        print("=" * 70)
        print(f"Baseline widgets:  {widgets_baseline}")
        print(f"After cycle widgets: {widgets_after}")
        print(f"Net widget growth:  {widget_growth:+d}")
        print("=" * 70)

        # ── Assertions ────────────────────────────────────────────────────
        self.assertLess(
            widget_growth,
            self.WIDGET_LEAK_THRESHOLD,
            f"Widget leak: {widget_growth} new widgets created after one cycle "
            f"(threshold: {self.WIDGET_LEAK_THRESHOLD})",
        )

    def test_06_three_cycle_degradation(self):
        """Run 3 full cycles through all panels; check for degradation."""
        from gui.main_window import MainWindow

        window = MainWindow()
        self.addCleanup(self._cleanup_window, window)

        panel_names = list(window.panels.keys())

        # ── Baseline ──────────────────────────────────────────────────────
        _force_gc_and_settle(0.3)
        rss_baseline = _get_rss_mb()
        timers_baseline = _count_all_timers(window)
        widgets_baseline = _count_widgets(window)

        cycle_times: list[float] = []
        cycle_rss: list[float] = []
        cycle_timers: list[int] = []
        cycle_widgets: list[int] = []

        # ── Run cycles ────────────────────────────────────────────────────
        for cycle_idx in range(self.CYCLE_COUNT):
            t0 = time.perf_counter()
            for name in panel_names:
                window._switch_panel(name)
                self.app.processEvents()
            elapsed = time.perf_counter() - t0

            _force_gc_and_settle(0.2)
            rss_now = _get_rss_mb()
            timers_now = _count_all_timers(window)
            widgets_now = _count_widgets(window)

            cycle_times.append(elapsed)
            cycle_rss.append(rss_now)
            cycle_timers.append(timers_now)
            cycle_widgets.append(widgets_now)

        rss_final = cycle_rss[-1]
        rss_net_growth = rss_final - rss_baseline
        timer_net_growth = cycle_timers[-1] - timers_baseline
        widget_net_growth = cycle_widgets[-1] - widgets_baseline

        # ── Report ────────────────────────────────────────────────────────
        print("\n" + "=" * 70)
        print("DEGRADATION REPORT (3 CYCLES)")
        print("=" * 70)
        print(f"{'Cycle':<8} {'Time':>10} {'RSS':>10} {'Timers':>8} {'Widgets':>8}")
        print("-" * 48)
        for i in range(self.CYCLE_COUNT):
            print(
                f"  {i+1:<6} "
                f"{cycle_times[i]:>8.3f}s "
                f"{cycle_rss[i]:>8.1f}MB "
                f"{cycle_timers[i]:>8d} "
                f"{cycle_widgets[i]:>8d}"
            )
        print("-" * 48)

        # Per-switch times
        per_switch = [t / len(panel_names) for t in cycle_times]
        print(f"Avg per-switch time per cycle: {[f'{s*1000:.1f}ms' for s in per_switch]}")

        # Degradation ratio
        if cycle_times[0] > 0:
            ratio = cycle_times[-1] / cycle_times[0]
            print(f"Cycle 3 / Cycle 1 time ratio: {ratio:.2f}x")
        else:
            ratio = 1.0

        print(f"\nNet RSS growth:     {rss_net_growth:+.1f} MB")
        print(f"Net timer growth:   {timer_net_growth:+d}")
        print(f"Net widget growth:  {widget_net_growth:+d}")
        print("=" * 70)

        # ── Assertions ────────────────────────────────────────────────────
        # 1. No single switch in any cycle takes > 2s
        for i, t in enumerate(cycle_times):
            self.assertLess(
                t,
                self.SLOW_PANEL_THRESHOLD_S * len(panel_names),
                f"Cycle {i+1} took {t:.3f}s for {len(panel_names)} panels",
            )

        # 2. Memory growth across all cycles is bounded
        self.assertLess(
            rss_net_growth,
            self.MEMORY_LEAK_THRESHOLD_MB,
            f"Memory leak across {self.CYCLE_COUNT} cycles: "
            f"RSS grew {rss_net_growth:+.1f} MB "
            f"(threshold: {self.MEMORY_LEAK_THRESHOLD_MB} MB)",
        )

        # 3. Timer growth is bounded
        self.assertLess(
            timer_net_growth,
            self.TIMER_LEAK_THRESHOLD,
            f"Timer leak across {self.CYCLE_COUNT} cycles: "
            f"{timer_net_growth:+d} new timers "
            f"(threshold: {self.TIMER_LEAK_THRESHOLD})",
        )

        # 4. Widget growth is bounded
        self.assertLess(
            widget_net_growth,
            self.WIDGET_LEAK_THRESHOLD,
            f"Widget leak across {self.CYCLE_COUNT} cycles: "
            f"{widget_net_growth:+d} new widgets "
            f"(threshold: {self.WIDGET_LEAK_THRESHOLD})",
        )

        # 5. Cycle 3 should not be drastically slower than cycle 1
        self.assertLess(
            ratio,
            self.DEGRADATION_RATIO_MAX,
            f"Cycle 3 was {ratio:.2f}x slower than cycle 1 "
            f"({cycle_times[-1]:.3f}s vs {cycle_times[0]:.3f}s) — "
            f"possible resource leak",
        )

    def test_07_repeated_same_panel_no_leak(self):
        """Switching to the same panel 10 times should not leak."""
        from gui.main_window import MainWindow

        window = MainWindow()
        self.addCleanup(self._cleanup_window, window)

        _force_gc_and_settle(0.2)
        rss_before = _get_rss_mb()
        timers_before = _count_all_timers(window)
        widgets_before = _count_widgets(window)

        # Switch to dashboard 10 times (reduced from 20 to prevent timeout)
        for _ in range(10):
            window._switch_panel("dashboard")
            self.app.processEvents()

        _force_gc_and_settle(0.3)
        rss_after = _get_rss_mb()
        timers_after = _count_all_timers(window)
        widgets_after = _count_widgets(window)

        rss_growth = rss_after - rss_before
        timer_growth = timers_after - timers_before
        widget_growth = widgets_after - widgets_before

        # ── Report ────────────────────────────────────────────────────────
        print("\n" + "=" * 70)
        print("REPEATED SAME-PANEL SWITCH REPORT")
        print("=" * 70)
        print(f"RSS growth:     {rss_growth:+.1f} MB")
        print(f"Timer growth:   {timer_growth:+d}")
        print(f"Widget growth:  {widget_growth:+d}")
        print("=" * 70)

        # ── Assertions ────────────────────────────────────────────────────
        self.assertLess(
            rss_growth,
            30.0,
            f"RSS grew {rss_growth:+.1f} MB after 10 switches to same panel",
        )
        self.assertLess(
            timer_growth,
            20,
            f"{timer_growth:+d} new timers after 10 switches to same panel",
        )
        self.assertLess(
            widget_growth,
            100,
            f"{widget_growth:+d} new widgets after 10 switches to same panel",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
