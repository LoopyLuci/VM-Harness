"""Drive the real VMConsolePanel against a live bridge and report what it does.

The panel's own GUI hides why a stream never starts: it sits on
"authenticating" and shows an empty viewport. This runs the same widget with
its internals printed, so the failing step is visible.
"""
from __future__ import annotations

import os
import sys

REPO = r"C:\Projects\VM-Harness"
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "src"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt5.QtWidgets import QApplication  # noqa: E402

URL = os.environ.get("PROBE_URL", "ws://127.0.0.1:8445/ws/stream")
TOKEN = os.environ.get("PROBE_TOKEN", "")
VM = os.environ.get("PROBE_VM", "omarchy")


def main() -> int:
    app = QApplication([])
    from gui.panels_vm_console import VMConsolePanel

    panel = VMConsolePanel()
    panel._token_input.setText(TOKEN)
    panel.resize(1200, 760)
    panel.show()
    app.processEvents()

    print(f"url={URL}\nvm={VM}\ntoken_len={len(TOKEN)}", flush=True)

    # Drive the panel's own connect path.
    panel._url_input.setText(URL)
    # Pick the VM through the combo so the panel takes its normal path.
    if VM in [panel._vm_combo.itemText(i) for i in range(panel._vm_combo.count())]:
        panel._vm_combo.setCurrentText(VM)
    else:
        panel._local_vms = list(panel._local_vms) + [VM]
        panel._refresh_vm_combo()
        panel._vm_combo.setCurrentText(VM)
    app.processEvents()
    print("selected vm:", panel._vm_combo.currentText(), flush=True)
    panel._connect()
    app.processEvents()

    import time

    for i in range(24):
        time.sleep(1)
        app.processEvents()
        if panel._frames_received:
            break

    print("--- panel internals ---", flush=True)
    print("connected      :", panel._connected, flush=True)
    print("_authenticated :", panel._authenticated, flush=True)
    print("frames_received:", panel._frames_received, flush=True)
    print("bytes_received :", panel._bytes_received, flush=True)
    print("pixmap_item    :", panel._pixmap_item is not None, flush=True)
    print("server_vms     :", panel._server_vms, flush=True)
    print("acked          :", panel._acked_fps, panel._acked_input_enabled, flush=True)
    err = getattr(panel, "_error_label", None)
    if err is not None:
        print("error_label    :", err.text(), flush=True)
    print("status         :", getattr(panel, "_status_label", None) and panel._status_label.text(), flush=True)
    try:
        panel._disconnect()
    except Exception as exc:  # noqa: BLE001
        print("disconnect error:", exc, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())