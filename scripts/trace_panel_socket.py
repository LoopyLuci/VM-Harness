"""Trace what the console panel's socket actually receives.

Symptom under investigation: the bridge demonstrably streams (a raw client gets
~150 frames), but the panel records exactly one frame and its repaint never
runs. This counts on_message callbacks separately from the panel's own counter,
so a delivery problem is distinguishable from a handling problem.
"""
from __future__ import annotations

import os
import sys
import time

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
    import websocket
    from gui.panels_vm_console import VMConsolePanel

    counts = {"bin": 0, "txt": 0}
    events: list[tuple] = []

    real_app = websocket.WebSocketApp

    def factory(url, on_open=None, on_message=None, on_error=None, on_close=None, **kw):
        def wrap_msg(ws, msg):
            counts["bin" if isinstance(msg, (bytes, bytearray)) else "txt"] += 1
            return on_message(ws, msg) if on_message else None

        def wrap_err(ws, err):
            events.append(("error", str(err)[:90]))
            return on_error(ws, err) if on_error else None

        def wrap_close(ws, code, msg):
            events.append(("close", code, str(msg)[:60]))
            return on_close(ws, code, msg) if on_close else None

        return real_app(
            url,
            on_open=on_open,
            on_message=wrap_msg,
            on_error=wrap_err,
            on_close=wrap_close,
            **kw,
        )

    panel = VMConsolePanel()
    panel._ws_factory = factory
    panel._token_input.setText(TOKEN)
    panel._url_input.setText(URL)
    names = [panel._vm_combo.itemText(i) for i in range(panel._vm_combo.count())]
    if VM not in names:
        panel._local_vms = list(panel._local_vms) + [VM]
        panel._refresh_vm_combo()
    panel._vm_combo.setCurrentText(VM)
    app.processEvents()

    panel._connect()
    for i in range(20):
        time.sleep(1)
        app.processEvents()
        if i % 5 == 0:
            print(
                f"  t+{i}s  socket_bin={counts['bin']}  socket_txt={counts['txt']}  "
                f"panel_frames={panel._frames_received}  "
                f"queued_flag={panel._frame_update_queued}  "
                f"pixmap={panel._pixmap_item is not None}  "
                f"status={panel._status_label.text()!r}",
                flush=True,
            )

    print(f"FINAL socket_bin={counts['bin']} panel_frames={panel._frames_received}", flush=True)
    print(f"queued_flag={panel._frame_update_queued}", flush=True)
    print(f"last_image={panel._last_image is not None}", flush=True)
    print(f"error_label={panel._error_label.text()!r}", flush=True)
    print(f"events={events}", flush=True)
    try:
        panel._disconnect()
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())