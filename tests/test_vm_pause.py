"""Regression tests: nothing may pause a VM without an explicit, scoped action.

The incident these guard against
-------------------------------
The Omarchy QEMU became ``{"status": "paused", "running": false}`` twice, hours
apart, with nobody touching anything. The console kept painting frames --
``screendump`` answers a stopped guest with its last framebuffer indefinitely --
while every ``input-send-event`` failed with "VM not running". That combination
is the whole signature of the bug, and it has a habit of being read as "something
paused the VM" when the only honest reading is "something stopped the guest
executing, and the console cannot tell you what".

Three properties are asserted here, in the three files that could plausibly
violate them:

1. **No pause without an explicit action.** The console panel holds no QMP
   connection and must never put a run-state-changing message on the wire; the
   streaming bridge must refuse every such message instead of quietly ignoring
   it; the VM control panel's ``stop`` is reachable only from its own button.

2. **A pause is scoped to the VM that was selected at the time.** A click on
   Suspend for one VM must not act on whichever VM happens to be selected by the
   time the command is dispatched, and a run-state reply about a VM that is no
   longer on screen must not be displayed as if it were about the current one.

3. **A failed status query reports "unknown", never "paused".** Not being able to
   ask is not the same answer as being told the guest stopped, and collapsing the
   two is what turns a QMP hiccup into a story about a VM that halted by itself.

Deliberately self-contained: the fakes below are local rather than imported from
``test_streaming_bridge``/``test_vm_console_panel`` so that a change to another
test file's helpers cannot quietly weaken these assertions.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _candidate in (PROJECT_ROOT, PROJECT_ROOT / "src"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import pytest
from PyQt5.QtCore import QObject, pyqtSignal

import streaming_bridge as sb
from gui.panels_vm_console import VMConsolePanel
from gui.panels_vm_control import VMControlPanel


#: QMP commands that halt (or restart) guest execution. A bridge or a watching
#: panel that emits any of these is a bug, whatever the reason.
HALTING_QMP_COMMANDS = frozenset({
    "stop",
    "cont",
    "system_stop",
    "system_reset",
    "system_powerdown",
    "system_wakeup",
    "inject-nmi",
    "pvpanic",
    "qom-set",
})


# ═════════════════════════════════════════════════════════════════════════════
# 1. The streaming bridge refuses to change a guest's run state
# ═════════════════════════════════════════════════════════════════════════════


class RecordingQMPClient:
    """QMP double that records commands and can be told to fail ``query-status``.

    ``screendump`` writes a real PNG so the capture path is genuinely exercised,
    matching what the bridge does in production.
    """

    def __init__(self, uri: str, status: Any = None, fail: bool = False) -> None:
        self.uri = uri
        self.commands: List[Tuple[str, Optional[dict]]] = []
        self.is_connected = False
        self.status = status
        self.fail = fail
        self._png = _png_bytes()

    async def connect(self) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False

    async def send(self, cmd: str, args: Optional[dict] = None) -> Dict[str, Any]:
        self.commands.append((cmd, args))
        if cmd == "screendump":
            Path(args["filename"]).write_bytes(self._png)  # type: ignore[index]
            return {}
        if cmd == "query-status":
            if self.fail:
                raise RuntimeError("QMP error: connection lost")
            return {} if self.status is None else {"return": self.status}
        return {}

    def sent(self, cmd: str) -> List[dict]:
        return [args for name, args in self.commands if name == cmd]


def _png_bytes(width: int = 64, height: int = 48) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 10, 10)).save(buffer, format="PNG")
    return buffer.getvalue()


class StubRegistry:
    def __init__(self, names: Tuple[str, ...] = ("vm-a", "vm-b")) -> None:
        self._targets = {
            name: sb.VMTarget(name, f"tcp:127.0.0.1:{4444 + i}", "")
            for i, name in enumerate(names)
        }

    def list_targets(self) -> List[sb.VMTarget]:
        return [self._targets[name] for name in sorted(self._targets)]

    def list_vms(self) -> List[str]:
        return sorted(self._targets)

    def get(self, name: Any) -> Optional[sb.VMTarget]:
        return self._targets.get(name) if isinstance(name, str) else None


class BridgeHarness:
    """A live bridge on a loopback port with recording QMP behind it."""

    def __init__(self, status: Any = None, fail: bool = False) -> None:
        self.clients: Dict[str, RecordingQMPClient] = {}
        self.registry = StubRegistry()
        self.bridge = sb.StreamingBridge(
            registry=self.registry,  # type: ignore[arg-type]
            authenticator=sb.TokenAuthenticator("pause-regression-token"),
            host="127.0.0.1",
            port=0,
            client_factory=self._make_client,
        )
        self._status = status
        self._fail = fail
        self._server = None
        self.base_url = ""

    def _make_client(self, uri: str) -> RecordingQMPClient:
        client = RecordingQMPClient(uri, status=self._status, fail=self._fail)
        self.clients[uri] = client
        return client

    async def start(self) -> None:
        from aiohttp.test_utils import TestServer

        self._server = TestServer(self.bridge.build_app())
        await self._server.start_server()
        self.base_url = f"http://127.0.0.1:{self._server.port}"

    async def stop(self) -> None:
        await self.bridge.stop()
        if self._server is not None:
            await self._server.close()
            self._server = None

    def qmp(self, vm: str) -> RecordingQMPClient:
        return self.clients[self.registry.get(vm).qmp_uri]  # type: ignore[union-attr]


async def _open_bridge(status: Any = None, fail: bool = False) -> Tuple[BridgeHarness, Any]:
    """Start a bridge and an authenticated WebSocket, and hand back both."""
    import aiohttp

    harness = BridgeHarness(status=status, fail=fail)
    await harness.start()
    session = aiohttp.ClientSession()
    ws = await session.ws_connect(f"{harness.base_url}/ws/stream")
    await ws.send_str(json.dumps({
        "type": "auth", "key": "pause-regression-token",
    }))
    assert (await ws.receive_json())["type"] == "auth_ok"
    return harness, (ws, session)


async def _close_bridge(harness: BridgeHarness, opened: Tuple[Any, Any]) -> None:
    ws, session = opened
    try:
        await ws.close()
    finally:
        await session.close()
        await harness.stop()


async def _recv_json(ws, timeout: float = 5.0) -> dict:
    import aiohttp

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        assert remaining > 0, "no JSON message arrived in time"
        msg = await asyncio.wait_for(ws.receive(), remaining)
        if msg.type == aiohttp.WSMsgType.BINARY:
            continue
        assert msg.type == aiohttp.WSMsgType.TEXT, msg.type
        return json.loads(msg.data)


@pytest.mark.parametrize("msg_type", sorted(sb._RUN_STATE_MESSAGE_TYPES))
async def test_bridge_refuses_every_run_state_message(msg_type: str):
    """Every message that would halt or resume a guest is answered, not ignored.

    Silently ignoring it would leave the client believing the guest was stopped
    when it is running, which is the same confusion this whole file is about.
    """
    harness, opened = await _open_bridge()
    try:
        ws, _ = opened
        await ws.send_str(json.dumps({"type": msg_type, "vm": "vm-a"}))
        reply = await _recv_json(ws)
        assert reply["type"] == "error"
        assert "run state" in reply["message"]
        assert msg_type in reply["message"]
        # Stronger than "sent nothing": the guest's QMP socket was never even
        # opened, so there was no connection on which to have sent anything.
        assert harness.clients == {}
    finally:
        await _close_bridge(harness, opened)


async def test_bridge_never_sends_a_halting_qmp_command():
    """Subscribing, streaming and driving input must not stop the guest."""
    harness, opened = await _open_bridge(status={"status": "running", "running": True})
    try:
        ws, _ = opened
        await ws.send_str(json.dumps({"type": "subscribe", "vm": "vm-a"}))
        await asyncio.sleep(0.2)
        await ws.send_str(json.dumps({
            "type": "input", "input_type": "key", "key": "a", "pressed": True,
        }))
        await ws.send_str(json.dumps({"type": "vm_state", "vm": "vm-a"}))
        await _recv_json(ws, timeout=3.0)

        for client in harness.clients.values():
            issued = {name for name, _ in client.commands}
            assert issued & HALTING_QMP_COMMANDS == set()
        # Human-monitor-command is the escape hatch a "just forward it" bridge
        # would leave open; the only HMP this bridge uses is `sendkey`.
        hmp_lines = [
            args["command-line"]
            for client in harness.clients.values()
            for name, args in client.commands
            if name == "human-monitor-command" and args
        ]
        assert all(line.startswith("sendkey ") for line in hmp_lines), hmp_lines
        # vm-a really was being streamed, so the assertion above is not vacuous.
        assert any(
            name == "screendump"
            for client in harness.clients.values()
            for name, _ in client.commands
        )
    finally:
        await _close_bridge(harness, opened)


async def test_bridge_reports_the_run_state_qmp_actually_says():
    harness, opened = await _open_bridge(status={"status": "paused", "running": False})
    try:
        ws, _ = opened
        await ws.send_str(json.dumps({"type": "vm_state", "vm": "vm-a"}))
        assert await _recv_json(ws) == {
            "type": "vm_state", "vm": "vm-a", "state": "paused", "running": False,
        }
    finally:
        await _close_bridge(harness, opened)


async def test_bridge_vm_state_defaults_to_the_subscribed_vm():
    """The reply always names its machine, so a verdict cannot drift onto
    whichever picture happens to be on screen."""
    harness, opened = await _open_bridge(status={"status": "running", "running": True})
    try:
        ws, _ = opened
        await ws.send_str(json.dumps({"type": "subscribe", "vm": "vm-b"}))
        await asyncio.sleep(0.2)
        await ws.send_str(json.dumps({"type": "vm_state"}))
        reply = await _recv_json(ws, timeout=3.0)
        assert reply["type"] == "vm_state"
        assert reply["vm"] == "vm-b"
    finally:
        await _close_bridge(harness, opened)


async def test_bridge_reports_unknown_when_query_status_raises():
    """A failed query is "unknown". It is emphatically not "paused"."""
    harness, opened = await _open_bridge(fail=True)
    try:
        ws, _ = opened
        await ws.send_str(json.dumps({"type": "vm_state", "vm": "vm-a"}))
        reply = await _recv_json(ws)
        assert reply == {
            "type": "vm_state", "vm": "vm-a", "state": "unknown", "running": None,
        }
    finally:
        await _close_bridge(harness, opened)


async def test_bridge_reports_unknown_when_the_reply_has_no_status():
    """A reply that says nothing about the run state is not a "paused"."""
    harness, opened = await _open_bridge(status=None)
    try:
        ws, _ = opened
        await ws.send_str(json.dumps({"type": "vm_state", "vm": "vm-a"}))
        reply = await _recv_json(ws)
        assert reply == {
            "type": "vm_state", "vm": "vm-a", "state": "unknown", "running": None,
        }
    finally:
        await _close_bridge(harness, opened)


async def test_bridge_vm_state_for_an_unknown_vm_is_an_error_not_a_state():
    harness, opened = await _open_bridge()
    try:
        ws, _ = opened
        await ws.send_str(json.dumps({"type": "vm_state", "vm": "ghost"}))
        reply = await _recv_json(ws)
        assert reply["type"] == "error"
        assert "ghost" in reply["message"]
    finally:
        await _close_bridge(harness, opened)


async def test_run_state_helper_is_unknown_on_every_failure_shape():
    """Unit-level: the helper itself never invents a state."""
    for reply in ({}, {"return": {}}, {"return": None}, {"return": {"running": False}},
                  {"return": {"status": ""}}, {"return": {"status": 7}}):
        client = RecordingQMPClient("tcp:127.0.0.1:1", status=None)
        client.status = None

        async def fake_send(cmd, args=None, _reply=reply):
            if cmd == "query-status":
                return _reply
            return {}

        client.send = fake_send  # type: ignore[assignment]
        source = sb.VMStreamSource(sb.VMTarget("vm-a", "tcp:127.0.0.1:1", ""))
        source._client = client
        client.is_connected = True
        assert await source.run_state() == {"state": "unknown", "running": None}, reply


# ═════════════════════════════════════════════════════════════════════════════
# 2. The console panel never pauses the VM it is watching
# ═════════════════════════════════════════════════════════════════════════════


class FakeWebSocket:
    """Records everything the panel puts on the wire."""

    instances: List["FakeWebSocket"] = []

    def __init__(self, url, on_open=None, on_message=None, on_error=None,
                 on_close=None):
        self.url = url
        self.sent: List[dict] = []
        self.closed = False
        self._on_open = on_open
        self._on_message = on_message
        FakeWebSocket.instances.append(self)

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def run_forever(self, **_kwargs) -> None:
        if self._on_open is not None:
            self._on_open(self)

    def close(self) -> None:
        self.closed = True

    def types(self) -> List[str]:
        return [message.get("type") for message in self.sent]

    def of_type(self, kind: str) -> List[dict]:
        return [m for m in self.sent if m.get("type") == kind]

    def deliver(self, payload: dict) -> None:
        self._on_message(self, json.dumps(payload))


@pytest.fixture
def console(qtbot, tmp_path, monkeypatch):
    import gui.panels_vm_console as module

    monkeypatch.setattr(module, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.delenv(module.TOKEN_ENV, raising=False)
    monkeypatch.setattr(VMConsolePanel, "_load_token", staticmethod(lambda: ""))
    monkeypatch.setattr(VMConsolePanel, "_store_token", lambda self, token: None)

    class StubManager:
        def list_vms(self):
            return ["win11", "ubuntu"]

        def get_vm(self, name):
            return {"name": name}

        def get_qmp_uri(self, name):
            return "tcp:127.0.0.1:5555"

        def get_status(self, name):
            return "running"

    import gui.multi_vm

    monkeypatch.setattr(gui.multi_vm, "MultiVMManager", StubManager)

    FakeWebSocket.instances.clear()
    widget = VMConsolePanel()
    widget._ws_factory = FakeWebSocket
    widget._token_input.setText("pause-regression-token")
    widget.resize(1200, 760)
    qtbot.addWidget(widget)
    widget.show()
    _pump(qtbot, 50)
    yield widget
    try:
        widget._disconnect()
    except Exception:  # noqa: BLE001
        pass
    widget.close()


def _pump(qtbot, ms: int = 40) -> None:
    import time

    from PyQt5.QtWidgets import QApplication

    deadline = time.monotonic() + ms / 1000.0
    while True:
        QApplication.processEvents()
        if time.monotonic() >= deadline:
            return
        time.sleep(0.005)


def _pump_until(qtbot, predicate, timeout_ms: int = 2000) -> None:
    import time

    from PyQt5.QtWidgets import QApplication

    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition never became true")


def _go_live(qtbot, panel: VMConsolePanel, vm: str = "win11") -> FakeWebSocket:
    panel._populate_vms([vm])
    panel.set_vm(vm)
    panel._connect()
    socket = FakeWebSocket.instances[-1]
    _pump_until(qtbot, lambda: panel._connected)
    panel._apply_control_message({"type": "auth_ok"})
    _pump(qtbot, 30)
    return socket


def test_console_starts_with_an_unknown_guest_state(console):
    """Nothing is known about the guest until QMP says so."""
    assert console._guest_state == "unknown"
    assert console._guest_state_label.text() == "guest: unknown"


def test_console_pause_button_sends_nothing(qtbot, console):
    """The Pause button freezes this window and touches no VM.

    It is the one control on a panel that is merely watching a VM, so it must
    be incapable of changing that VM's run state -- not merely unlikely to.
    """
    socket = _go_live(qtbot, console)
    socket.sent.clear()

    console._toggle_pause()
    assert console._paused is True

    assert not (set(socket.types()) & sb._RUN_STATE_MESSAGE_TYPES)
    assert socket.of_type("vm_state") == []
    assert socket.sent == []


def test_console_pause_button_is_refused_while_disconnected(qtbot, console):
    """A pause asked for with no stream behind it is refused, not half-applied."""
    assert console._connected is False
    console._btn_pause.setEnabled(True)  # force the click past the disabled state

    console._toggle_pause()

    assert console._paused is False
    assert console._btn_pause.text() == "Pause"


def test_console_never_puts_a_run_state_message_on_the_wire(qtbot, console):
    """Exercise every control and every input path; the wire stays clean.

    Broader than the button test on purpose: this is the property that actually
    matters, that no code path in this panel can halt a guest, and it is cheap to
    assert over the whole surface rather than one handler at a time.
    """
    from PyQt5.QtCore import QEvent, QPoint, Qt
    from PyQt5.QtGui import QKeyEvent

    socket = _go_live(qtbot, console)

    console._fps_spin.setValue(9)
    console._input_check.setChecked(False)
    console._input_check.setChecked(True)
    console._send_ping()
    console._refresh()
    console._save_screenshot  # not invoked: it opens a modal dialog
    console._handle_frame(_jpeg(320, 200))
    console._send_mouse_move(QPoint(10, 10))
    console._send_mouse_click(QPoint(10, 10), "left", True)
    console._send_scroll(0, 120)
    console.send_key("a")
    console._stats_timer.setInterval(1)
    _pump(qtbot, 60)
    console._check_health()
    _pump(qtbot, 20)

    assert socket.types()  # the panel really did talk
    assert not (set(socket.types()) & sb._RUN_STATE_MESSAGE_TYPES), socket.types()

    # And the panel owns no QMP client at all, so there is nothing to stop a VM.
    assert not hasattr(console, "_qmp")
    assert not hasattr(console, "_qmp_client")


def test_console_shows_the_state_qmp_reported(qtbot, console):
    socket = _go_live(qtbot, console)

    socket.deliver({"type": "vm_state", "vm": "win11", "state": "running",
                    "running": True})
    _pump(qtbot, 30)
    assert console._guest_state == "running"
    assert console._guest_state_label.text() == "guest: running"

    socket.deliver({"type": "vm_state", "vm": "win11", "state": "paused",
                    "running": False})
    _pump(qtbot, 30)
    assert console._guest_state_label.text() == "guest: paused"


@pytest.mark.parametrize("payload", [
    {"type": "vm_state", "vm": "win11", "state": "unknown", "running": None},
    {"type": "vm_state", "vm": "win11"},
    {"type": "vm_state", "vm": "win11", "state": ""},
    {"type": "vm_state", "vm": "win11", "state": None},
    {"type": "vm_state", "vm": "win11", "state": "something-new-in-qemu-9"},
])
def test_console_never_renders_an_unverified_state_as_paused(qtbot, console, payload):
    """Anything short of QMP saying "paused" renders as unknown."""
    socket = _go_live(qtbot, console)
    socket.deliver(payload)
    _pump(qtbot, 30)

    assert console._guest_state == "unknown"
    assert "paused" not in console._guest_state_label.text()
    assert console._guest_state_label.text() == "guest: unknown"


def test_console_drops_a_run_state_reply_for_another_vm(qtbot, console):
    """A verdict is only ever shown next to the machine it describes.

    The reply can easily arrive after the user has switched VMs: it was asked
    for before the switch and answered after it. Showing it would put "guest:
    paused" under a picture of a different machine -- the exact shape of the
    original report.
    """
    socket = _go_live(qtbot, console)
    socket.deliver({"type": "vm_state", "vm": "win11", "state": "running",
                    "running": True})
    _pump(qtbot, 30)

    console.set_vm("win11")
    console._apply_control_message({"type": "vm_state", "vm": "ubuntu",
                                    "state": "paused", "running": False})
    _pump(qtbot, 30)

    assert console.selected_vm() == "win11"
    assert console._guest_state == "running"
    assert "paused" not in console._guest_state_label.text()


def test_console_forgets_the_verdict_when_the_vm_changes(qtbot, console):
    socket = _go_live(qtbot, console)
    socket.deliver({"type": "vm_state", "vm": "win11", "state": "paused",
                    "running": False})
    _pump(qtbot, 30)
    assert console._guest_state == "paused"

    console._populate_vms(["win11", "ubuntu"])
    console.set_vm("ubuntu")
    _pump(qtbot, 30)

    assert console._guest_state == "unknown"
    assert "paused" not in console._guest_state_label.text()


def test_console_forgets_the_verdict_when_it_disconnects(qtbot, console):
    socket = _go_live(qtbot, console)
    socket.deliver({"type": "vm_state", "vm": "win11", "state": "paused",
                    "running": False})
    _pump(qtbot, 30)

    console._handle_disconnected()
    _pump(qtbot, 20)

    assert console._guest_state == "unknown"
    assert "paused" not in console._guest_state_label.text()


def _jpeg(width: int, height: int) -> bytes:
    from PyQt5.QtCore import QBuffer
    from PyQt5.QtGui import QColor, QImage

    image = QImage(width, height, QImage.Format_RGB32)
    image.fill(QColor("#204080"))
    buffer = QBuffer()
    buffer.open(QBuffer.ReadWrite)
    assert image.save(buffer, "JPEG", 90)
    return bytes(buffer.data())


# ═════════════════════════════════════════════════════════════════════════════
# 3. VM control panel: `stop` is scoped to the VM selected at press time
# ═════════════════════════════════════════════════════════════════════════════


class StubPerVMBridge:
    """One VM's QMP session. Records every lifecycle command it is handed."""

    def __init__(self, name: str, connected: bool = True) -> None:
        self.vm_name = name
        self.is_connected = connected
        self.commands: List[str] = []


class StubMultiVMQMPBridge(QObject):
    """Stands in for ``MultiVMQMPBridge`` with the same target-resolution shape.

    ``stop_vm``/``cont`` resolve the VM from ``active_vm`` when they are *called*,
    which is the behaviour that makes an unscoped pause dangerous, so the double
    reproduces it rather than hiding it.
    """

    connected = pyqtSignal(str, bool)
    vm_status = pyqtSignal(str, dict)
    error = pyqtSignal(str, str)
    command_result = pyqtSignal(str, dict)
    active_vm_changed = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.active_vm: Optional[str] = None
        self.bridges: Dict[str, StubPerVMBridge] = {}
        self.stops: List[str] = []
        self.conts: List[str] = []

    def add(self, vm: str, connected: bool = True) -> StubPerVMBridge:
        per_vm = StubPerVMBridge(vm, connected)
        self.bridges[vm] = per_vm
        return per_vm

    def get_bridge(self, vm: str) -> Optional[StubPerVMBridge]:
        return self.bridges.get(vm)

    def stop_vm(self) -> None:
        if not self.active_vm:
            return
        per_vm = self.bridges.get(self.active_vm)
        if per_vm is not None and per_vm.is_connected:
            self.stops.append(self.active_vm)
            per_vm.commands.append("stop")

    def cont(self) -> None:
        if not self.active_vm:
            return
        per_vm = self.bridges.get(self.active_vm)
        if per_vm is not None and per_vm.is_connected:
            self.conts.append(self.active_vm)
            per_vm.commands.append("cont")


@pytest.fixture
def control(qtbot):
    panel = VMControlPanel()
    qtbot.addWidget(panel)
    bridge = StubMultiVMQMPBridge()
    panel.set_multi_qmp_bridge(bridge)
    panel._multi_qmp = bridge
    return panel, bridge


def test_control_suspend_does_nothing_without_a_selected_vm(control):
    panel, bridge = control
    bridge.add("win11")

    panel._on_suspend()

    assert bridge.stops == []
    assert "no VM selected" in panel.info_label.text()


def test_control_suspend_is_scoped_to_the_vm_selected_at_press_time(control):
    """A Suspend aimed at one VM must not act on the VM selected later.

    ``stop`` is silent and unrecoverable-looking: the guest keeps answering
    ``screendump`` while refusing input, so a pause that lands on the wrong
    machine is invisible from the console and reads as that machine having halted
    on its own.
    """
    panel, bridge = control
    bridge.add("win11")
    bridge.add("ubuntu")
    bridge.active_vm = "win11"
    panel._active_vm = "win11"

    # The user presses Suspend for win11...
    pressed_for = panel._active_vm
    # ...and the window's VM selection moves before the command is dispatched.
    bridge.active_vm = "ubuntu"
    panel._active_vm = "ubuntu"

    panel._on_suspend(pressed_for)

    assert bridge.stops == []
    assert bridge.get_bridge("ubuntu").commands == []  # type: ignore[union-attr]
    assert "refused" in panel.info_label.text()
    assert "win11" in panel.info_label.text()


def test_control_suspend_reaches_the_selected_vm_when_nothing_moved(control):
    panel, bridge = control
    bridge.add("win11")
    bridge.active_vm = "win11"
    panel._active_vm = "win11"

    panel._on_suspend("win11")

    assert bridge.stops == ["win11"]
    assert bridge.get_bridge("win11").commands == ["stop"]  # type: ignore[union-attr]
    assert "Suspending win11" in panel.info_label.text()


def test_control_suspend_is_refused_without_a_live_qmp_session(control):
    panel, bridge = control
    bridge.add("win11", connected=False)
    bridge.active_vm = "win11"
    panel._active_vm = "win11"

    panel._on_suspend("win11")

    assert bridge.stops == []
    assert "refused" in panel.info_label.text()
    assert "no QMP connection" in panel.info_label.text()


def test_control_suspend_is_refused_when_the_bridge_drives_another_vm(control):
    panel, bridge = control
    bridge.add("win11")
    bridge.add("ubuntu")
    bridge.active_vm = "ubuntu"
    panel._active_vm = "win11"

    panel._on_suspend("win11")

    assert bridge.stops == []
    assert "driving ubuntu" in panel.info_label.text()


def test_control_resume_is_scoped_the_same_way(control):
    """``cont`` to the wrong VM leaves the paused one frozen -- the same
    confusion, from the other direction."""
    panel, bridge = control
    bridge.add("win11")
    bridge.add("ubuntu")
    bridge.active_vm = "win11"
    panel._active_vm = "win11"

    pressed_for = panel._active_vm
    bridge.active_vm = "ubuntu"
    panel._active_vm = "ubuntu"

    panel._on_resume(pressed_for)

    assert bridge.conts == []
    assert "refused" in panel.info_label.text()

    panel._on_resume("ubuntu")
    assert bridge.conts == ["ubuntu"]


def test_control_suspend_button_captures_the_vm_at_press_time(control):
    """The wiring itself must capture the VM when the button is pressed.

    Connecting the handler to a bare ``_on_suspend`` would let the target be
    resolved at dispatch time instead, which is the bug in test form.
    """
    panel, bridge = control
    bridge.add("win11")
    bridge.active_vm = "win11"
    panel._active_vm = "win11"

    panel._lifecycle_btns["Suspend"].click()

    assert bridge.stops == ["win11"]
