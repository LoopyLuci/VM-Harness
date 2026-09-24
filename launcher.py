"""VM-Harness Single-Instance Launcher.

Enforces single instance by checking for the main window by title.
If found, brings it to front and exits.
If not found, launches the main EXE and waits.
"""

import ctypes
import os
import subprocess
import sys
import time

WINDOW_TITLE = "VM-Harness"
EXE_NAME = "VM-Harness.exe"


def find_existing_window():
    """Find existing VM-Harness window by title."""
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, WINDOW_TITLE)
    return hwnd


def bring_to_front(hwnd):
    """Bring existing window to foreground."""
    user32 = ctypes.windll.user32
    # Show and restore window
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)


def acquire_mutex():
    """Acquire a Windows named mutex to prevent race conditions."""
    kernel32 = ctypes.windll.kernel32
    mutex_name = "Global\\VM-Harness-Launcher"
    handle = kernel32.CreateMutexW(None, True, mutex_name)
    error = kernel32.GetLastError()
    if error == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    return True


def release_mutex():
    """Release the launcher mutex."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenMutexW(0x00100000, False, "Global\\VM-Harness-Launcher")
    if handle:
        kernel32.CloseHandle(handle)


def main():
    # Fast path: check for existing window
    hwnd = find_existing_window()
    if hwnd:
        bring_to_front(hwnd)
        return 0

    # Slow path: launch the EXE
    script_dir = os.path.dirname(os.path.abspath(__file__))
    exe_path = os.path.join(script_dir, EXE_NAME)

    if not os.path.exists(exe_path):
        # Show error message
        ctypes.windll.user32.MessageBoxW(
            0,
            f"Error: {EXE_NAME} not found in {script_dir}",
            "VM-Harness Launcher",
            0x10,  # MB_ICONERROR
        )
        return 1

    # Launch the EXE and wait
    try:
        proc = subprocess.Popen([exe_path] + sys.argv[1:])
        proc.wait()
        return proc.returncode
    except Exception as e:
        ctypes.windll.user32.MessageBoxW(
            0,
            f"Failed to launch {EXE_NAME}: {e}",
            "VM-Harness Launcher",
            0x10,  # MB_ICONERROR
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
