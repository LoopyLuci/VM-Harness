"""
QWebChannel Bridge — connects Python VM-Harness to TypeScript/Web UI.
Wires the Rust supervisor, telemetry buffer, and web components together.
"""

from PyQt5.QtCore import QObject, pyqtSlot, pyqtSignal, QVariant, QByteArray
from PyQt5.QtWebChannel import QWebChannel
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEnginePage


class VmHarnessWebBridge(QObject):
    """Python-side QWebChannel object exposed as VmHarnessBridge in JS."""

    telemetry_updated = pyqtSignal(str)  # vm_name
    vm_state_changed = pyqtSignal(str, str)  # vm_name, state

    def __init__(self, parent=None):
        super().__init__(parent)
        self.supervisor = None  # Wired at init time

    @pyqtSlot(str, result=str)
    def startVm(self, vm_name: str) -> str:
        if self.supervisor:
            try:
                result = self.supervisor.start()
                self.vm_state_changed.emit(vm_name, "running")
                return result
            except Exception as e:
                return f"error: {e}"
        return "started (stub)"

    @pyqtSlot(str, result=str)
    def stopVm(self, vm_name: str) -> str:
        if self.supervisor:
            try:
                result = self.supervisor.stop()
                self.vm_state_changed.emit(vm_name, "stopped")
                return result
            except Exception as e:
                return f"error: {e}"
        return "stopped (stub)"

    @pyqtSlot(str, result=dict)
    def getVmState(self, vm_name: str) -> dict:
        if self.supervisor:
            # The supervisor exposes state() -> dict
            try:
                return self.supervisor.state()
            except:
                return {"name": vm_name, "state": "unknown"}
        return {"name": vm_name, "state": "stopped"}


class WebBridgeEngine(QWebEngineView):
    """Embedded Chromium that loads the TypeScript/Web UI."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # Create web channel and register Python bridge
        self.channel = QWebChannel()
        self.bridge_obj = VmHarnessWebBridge()
        self.channel.registerObject("VmHarnessBridge", self.bridge_obj)

        # Create page and inject web channel
        page = QWebEnginePage(self)
        page.setWebChannel(self.channel)
        self.setPage(page)

        # Load the compiled web build
        import os
        web_dir = os.path.join(os.path.dirname(__file__), "..", "..", "web", "dist")
        if not os.path.exists(web_dir):
            web_dir = os.path.join(os.path.dirname(__file__), "..", "..", "web")
        index_path = os.path.join(web_dir, "index.html")
        if os.path.exists(index_path):
            self.load(os.path.join(web_dir, "index.html"))
        else:
            # Fallback: load empty page with bridge available
            self.setHtml("<html><body><h2>VM-Harness Web Bridge Ready</h2>"
                        "<script>console.log('Bridge:', window.VmHarnessBridge);</script>"
                        "</body></html>")

    def wire_supervisor(self, supervisor):
        """Wire Rust supervisor to the web bridge."""
        self.bridge_obj.supervisor = supervisor
