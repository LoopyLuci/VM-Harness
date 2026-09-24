"""VM-Harness GUI — single-instance, self-managing, crash-proof entry point."""

from __future__ import annotations

import os
import sys
import signal
import atexit
import logging
import argparse
import time

# ═══════════════════════════════════════════════════════════════════════════════
# Debug logging — use fixed absolute path
# ═══════════════════════════════════════════════════════════════════════════════
_debug_path = r"C:\Projects\QEMU-MCP\logs\debug.log"
os.makedirs(r"C:\Projects\QEMU-MCP\logs", exist_ok=True)

def _log_debug(msg: str):
    try:
        with open(_debug_path, 'a') as _f:
            _f.write(f"PID {os.getpid()}: {msg}\n")
    except Exception:
        pass

_log_debug(f"LOADED v2, file={__file__}")

# ═══════════════════════════════════════════════════════════════════════════════
# CRITICAL: Suppress ALL CLI console windows on Windows
# ═══════════════════════════════════════════════════════════════════════════════
if sys.platform == "win32":
    _CREATE_NO_WINDOW = 0x08000000
    _SW_HIDE = 0
    
    import subprocess
    
    _orig_popen_init = subprocess.Popen.__init__
    def _popen_init_noconsole(self, *args, **kwargs):
        if "creationflags" not in kwargs:
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        if "startupinfo" not in kwargs:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = _SW_HIDE
            kwargs["startupinfo"] = si
        return _orig_popen_init(self, *args, **kwargs)
    subprocess.Popen.__init__ = _popen_init_noconsole
    
    _orig_run = subprocess.run
    def _run_noconsole(*args, **kwargs):
        if "creationflags" not in kwargs:
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        if "startupinfo" not in kwargs:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = _SW_HIDE
            kwargs["startupinfo"] = si
        return _orig_run(*args, **kwargs)
    subprocess.run = _run_noconsole

# ═══════════════════════════════════════════════════════════════════════════════
# Logging
# ═══════════════════════════════════════════════════════════════════════════════
script_dir = os.path.dirname(os.path.abspath(__file__))
project_dir = os.path.dirname(script_dir)
log_dir = os.path.join(project_dir, "logs")
os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(log_dir, "vmharness.log")),
    ],
)
logger = logging.getLogger("vmharness.gui")

# ═══════════════════════════════════════════════════════════════════════════════
# Single-instance lock — file-based with PID check
# ═══════════════════════════════════════════════════════════════════════════════
_LOCK_DIR = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'VM-Harness')
_LOCK_FILE = os.path.join(_LOCK_DIR, 'instance.lock')


def _is_pid_running(pid: int) -> bool:
    """Check if a Windows process with given PID is alive."""
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False


def _acquire_single_instance() -> bool:
    """Acquire single-instance lock via file-based PID check."""
    global _lock_file
    
    os.makedirs(_LOCK_DIR, exist_ok=True)
    
    # Check if lock file exists and if the PID in it is still running
    if os.path.exists(_LOCK_FILE):
        try:
            with open(_LOCK_FILE, 'r') as f:
                old_pid = int(f.read().strip())
            if _is_pid_running(old_pid):
                _log_debug(f"Instance already running (PID {old_pid})")
                return False
            else:
                _log_debug(f"Stale lock from PID {old_pid} (not running)")
        except (ValueError, IOError):
            pass
    
    # Create/overwrite lock file with our PID
    try:
        with open(_LOCK_FILE, 'w') as f:
            f.write(str(os.getpid()))
        _log_debug(f"Lock acquired (PID {os.getpid()})")
        return True
    except IOError as e:
        _log_debug(f"Failed to create lock: {e}")
        return False


def _release_lock():
    """Release the single-instance lock."""
    try:
        if os.path.exists(_LOCK_FILE):
            os.remove(_LOCK_FILE)
            _log_debug("Lock released")
    except Exception as e:
        _log_debug(f"Failed to release lock: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    """Main entry point — single-instance enforced."""
    
    # Enforce single instance BEFORE anything else
    if not _acquire_single_instance():
        _log_debug("Exiting due to existing instance")
        # Force immediate exit
        os._exit(0)
        return

    _log_debug("Starting GUI")

    # Parse args
    parser = argparse.ArgumentParser(description="VM-Harness GUI")
    parser.add_argument("--config", type=str, default=None, help="Path to .env file")
    parser.add_argument("--master-pass", type=str, default=None, dest="master_password")
    parser.add_argument("--headless", action="store_true", default=False)
    parser.add_argument("--platform", type=str, default=None, help="Qt platform override")
    parser.add_argument("--dev", action="store_true", default=False, help="Enable hot-reload dev mode")
    args = parser.parse_args()

    # Apply CLI overrides
    if args.config:
        os.environ["VM_MCP_ENV_FILE"] = os.path.abspath(args.config)
    if args.master_password:
        os.environ["GUI_MASTER_PASSWORD"] = args.master_password
    if args.platform:
        os.environ["QT_QPA_PLATFORM"] = args.platform
    elif args.headless:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    if args.dev:
        os.environ["VM_HARNESS_DEV"] = "1"

    # Handle signals for clean shutdown
    def signal_handler(signum, frame):
        logger.info("Received signal %d — shutting down", signum)
        _release_lock()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Register atexit cleanup
    atexit.register(_release_lock)

    # Run GUI
    logger.info("Starting VM-Harness GUI")
    from PyQt5.QtWidgets import QApplication
    from gui.main_window import MainWindow
    
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    
    try:
        exit_code = app.exec_()
    except KeyboardInterrupt:
        exit_code = 0
    
    # Cleanup
    _release_lock()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
