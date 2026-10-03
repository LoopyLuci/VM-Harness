"""Pytest configuration and global state reset fixtures.

This conftest resets mutable module-level globals between test files so
that test ordering does not cause spurious failures.  Without this,
tests in separate files can interfere through shared singletons
(AuditLogger, ProviderStore, MetricsStore, etc.).
"""

from __future__ import annotations

import gc
import importlib
import sys
from pathlib import Path

import pytest

# Ensure the src directory is importable from any test file
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


# ── Modules whose mutable singletons should be reset between test files ──────────

_MODULES_TO_RESET = [
    "gui.audit_log",        # module-level audit_log singleton + _ensure_audit_log_parent state
    "gui.metrics_store",    # module-level metrics_store singleton
    "gui.metrics_alerts",   # module-level alerts manager
    "gui.provider_store",   # module-level provider store singleton
    "gui.api_providers",    # API providers registry state
    "gui.ssh_bridge",       # class-level defaults (not instance state — safe)
    "gui.qmp_bridge",       # class-level defaults (not instance state — safe)
    "gui.multi_vm_qmp_bridge",  # class-level defaults
    "gui.multi_vm",         # MultiVMManager state
    "gui.snapshot_scheduler",   # SnapshotScheduler state
    "gui.iso_manager",      # ISOManager state
    "gui.atomic_state",     # AtomicState state
    "gui.hot_reloader",     # HotReloader file watchers
    "gui.theme",            # theme state (Theme.current_theme, etc.)
    "gui.widgets",          # widget style cache
    "gui.chat_engine",      # ChatEngine class-level state
    "gui.qmp_extractor",    # QMP extractor state
    "gui.panels_vm_control",    # VMControlPanel references
    "gui.panels_vm_switcher",   # VM switcher state
    "gui.panels_multi_vm_dashboard",  # dashboard state
    "gui.panels_settings",      # settings panel config
    "gui.panels_guest_terminal",    # terminal state
    "gui.panels_telemetry",     # telemetry state
    "gui.panels_iso",       # ISO panel state
    "gui.panels_usb",       # USB panel state
    "gui.panels_network",   # network panel state
    "gui.panels_network_editor",   # network editor state
    "gui.panels_security",  # security panel state
    "gui.panels_logs",      # logs panel state
    "gui.panels_chat",      # chat panel state
    "gui.panels",           # dashboard panel state
    "gui.main_window",      # MainWindow class refs
    "gui.api_providers",    # API providers
    "gui.provider_store",   # provider store
    "gui.credential_store", # credential store (no module-level singleton, but reset anyway)
    "gui.dialogs_vm_login", # VM console login dialog + its save/load helpers
    "gui.qmp_extractor",    # QMP command extractor
    # NOTE: gui.vm_cloner is intentionally excluded — it defines
    # TemplateMetadata as a @dataclass that tests use with isinstance().
    # Reloading the module between tests breaks isinstance() checks.
    # TemplateManager instances are already isolated via per-test fixtures.
]


@pytest.fixture(scope="function", autouse=True)
def reset_module_state(request):
    """Reset mutable global state in known modules before each test function.

    This prevents test-ordering sensitivity where one test's mutations to
    module-level singletons leak into another test in a different file.
    """
    # Remember the windows that exist before the test: those belong to longer-lived (class/module) fixtures and
    # must survive; only what this test opened is closed afterwards.
    before = _top_level_ids()
    yield
    # ── Teardown: reload modules that carry mutable global state ──────────────
    # We reload after each test function so the next test starts with a
    # clean module.  Modules that don't have mutable globals are harmless
    # to reload (they re-execute their top-level code).
    for mod_name in _MODULES_TO_RESET:
        if mod_name in sys.modules:
            try:
                importlib.reload(sys.modules[mod_name])
            except Exception:
                # Some modules may fail to reload if they reference
                # already-garbage-collected Qt objects; that's fine.
                pass

    # Close what the test left open. Top-level windows and matplotlib canvases otherwise live on, and pytest-qt's event
    # processing after every later test redraws all of them: a serial run grew slower with every GUI test until one
    # timed out inside matplotlib's draw loop.
    _close_leftover_widgets(before)

    # Force garbage collection to clean up any detached Qt objects
    gc.collect()


def _top_level_ids() -> set[int]:
    qtw = sys.modules.get("PyQt5.QtWidgets")
    app = qtw.QApplication.instance() if qtw else None
    if app is None:
        return set()
    return {id(w) for w in app.topLevelWidgets()}


def _close_leftover_widgets(keep: set[int]) -> None:
    mpl = sys.modules.get("matplotlib.pyplot")
    if mpl is not None:
        try:
            mpl.close("all")
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
    qtw = sys.modules.get("PyQt5.QtWidgets")
    if qtw is None:
        return
    app = qtw.QApplication.instance()
    if app is None:
        return
    from PyQt5.QtCore import QTimer
    for w in app.topLevelWidgets():
        if id(w) in keep:
            continue
        # Close it and stop its timers, but do not delete it: some top-level widgets (menus, dialogs) are owned by
        # longer-lived windows, and deleting them under their owner crashes Qt. A closed widget with no running timer
        # costs nothing in later tests.
        try:
            for t in w.findChildren(QTimer):
                t.stop()
            w.close()
        except RuntimeError:  # already deleted on the C++ side
            pass


@pytest.fixture(scope="session", autouse=True)
def setup_offscreen_qt():
    """Ensure QT_QPA_PLATFORM=offscreen for the entire session."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
