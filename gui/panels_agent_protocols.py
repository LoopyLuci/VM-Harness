"""A2A HTTP and ACP stdio clients for connecting to agent peers."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from acp import PROTOCOL_VERSION, spawn_agent_process
from acp.schema import (
    AgentMessageChunk,
    AllowedOutcome,
    DeniedOutcome,
    Implementation,
    PermissionOption,
    RequestPermissionResponse,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
)
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from gui.theme import T
from vm_harness.control.hub import hub_alive
from vm_harness.protocols.a2a import discover_agent, send_agent_message

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _A2AWorker(QThread):
    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, action: str, url: str, token: str, message: str = "") -> None:
        super().__init__()
        self.action = action
        self.url = url
        self.token = token
        self.message = message
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            asyncio.set_event_loop(loop)
            self._task = loop.create_task(self._request())
            loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        finally:
            loop.close()

    async def _request(self) -> None:
        try:
            if self.action == "discover":
                result = await discover_agent(self.url, token=self.token)
            else:
                result = await send_agent_message(
                    self.url, self.message, token=self.token, timeout=60.0
                )
            self.succeeded.emit(json.dumps(result, indent=2, default=str))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failed.emit(str(exc))

    def cancel_request(self) -> None:
        if self._loop is not None and self._task is not None:
            self._loop.call_soon_threadsafe(self._task.cancel)


class _ACPClientCallbacks:
    def __init__(self, worker: "_ACPClientWorker") -> None:
        self.worker = worker

    async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
        if isinstance(update, AgentMessageChunk) and isinstance(update.content, TextContentBlock):
            self.worker.output.emit(update.content.text)
        elif isinstance(update, ToolCallStart):
            self.worker.output.emit(f"Operation requested: {update.title} (pending)")
        elif isinstance(update, ToolCallProgress):
            label = update.title or update.name or update.toolCallId
            self.worker.output.emit(f"Operation {label}: {update.status or 'updated'}")

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **kwargs: object,
    ) -> RequestPermissionResponse:
        permission_id = uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self.worker._pending_permissions[permission_id] = (loop, future)
        can_allow_once = any(option.kind == "allow_once" for option in options)
        title = tool_call.title or tool_call.name or "ACP agent operation"
        self.worker.permission_requested.emit(
            permission_id, title, can_allow_once, tool_call.kind == "delete"
        )
        try:
            approved = await future
        finally:
            self.worker._pending_permissions.pop(permission_id, None)
        if approved and can_allow_once:
            option = next(option for option in options if option.kind == "allow_once")
            return RequestPermissionResponse(outcome=AllowedOutcome(optionId=option.optionId))
        return RequestPermissionResponse(outcome=DeniedOutcome())


class _ACPClientWorker(QThread):
    ready = pyqtSignal(str)
    output = pyqtSignal(str)
    failed = pyqtSignal(str)
    permission_requested = pyqtSignal(str, str, bool, bool)
    completed = pyqtSignal()

    def __init__(self, command: str, arguments: list[str], cwd: str) -> None:
        super().__init__()
        self.command = command
        self.arguments = arguments
        self.cwd = cwd
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[str | None] | None = None
        self._pending_permissions: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Future[bool]]] = {}

    def run(self) -> None:
        try:
            asyncio.run(self._run_agent())
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.completed.emit()

    async def _run_agent(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        env = dict(os.environ)
        paths = [str(PROJECT_ROOT / "src"), str(PROJECT_ROOT)]
        if env.get("PYTHONPATH"):
            paths.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(paths)
        callbacks = _ACPClientCallbacks(self)
        async with spawn_agent_process(
            callbacks,
            self.command,
            *self.arguments,
            cwd=self.cwd,
            env=env,
        ) as (connection, process):
            await connection.initialize(
                protocol_version=PROTOCOL_VERSION,
                client_info=Implementation(name="VM-Harness", version="0.3.0"),
            )
            session = await connection.new_session(cwd=self.cwd)
            self.ready.emit(f"Connected to ACP agent; session {session.sessionId}")
            while True:
                try:
                    message = await asyncio.wait_for(self._queue.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    if process.returncode is not None:
                        raise RuntimeError(f"ACP agent exited with code {process.returncode}")
                    continue
                if message is None:
                    return
                await connection.prompt(
                    session_id=session.sessionId,
                    prompt=[TextContentBlock(text=message)],
                )

    def send_prompt(self, text: str) -> None:
        if self._loop is None or self._queue is None or not self.isRunning():
            raise RuntimeError("ACP agent is not connected")
        self._loop.call_soon_threadsafe(self._queue.put_nowait, text)

    def resolve_permission(self, permission_id: str, approved: bool) -> None:
        pending = self._pending_permissions.get(permission_id)
        if pending is None:
            return
        loop, future = pending
        loop.call_soon_threadsafe(self._resolve_future, future, approved)

    @staticmethod
    def _resolve_future(future: asyncio.Future[bool], approved: bool) -> None:
        if not future.done():
            future.set_result(approved)

    def stop_agent(self) -> None:
        for permission_id in list(self._pending_permissions):
            self.resolve_permission(permission_id, False)
        if self._loop is not None and self._queue is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)


class AgentProtocolsPanel(QWidget):
    """Discover and message A2A agents, or launch and use a local ACP agent."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._a2a_worker: _A2AWorker | None = None
        self._acp_worker: _ACPClientWorker | None = None
        self.setStyleSheet(f"background: {T.BG_PRIMARY}; color: {T.TEXT_PRIMARY};")
        layout = QVBoxLayout(self)
        title = QLabel("Agent Protocols")
        title.setStyleSheet(f"color: {T.TEXT_PRIMARY}; font-size: 16px; font-weight: bold;")
        layout.addWidget(title)
        self._tabs = QTabWidget()
        layout.addWidget(self._tabs)
        self._tabs.addTab(self._build_a2a_tab(), "A2A")
        self._tabs.addTab(self._build_acp_tab(), "ACP")

    def _build_a2a_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self._a2a_url = QLineEdit()
        self._a2a_url.setPlaceholderText("http://127.0.0.1:port")
        self._a2a_token = QLineEdit()
        self._a2a_token.setEchoMode(QLineEdit.Password)
        self._a2a_token.setPlaceholderText("Optional bearer token")
        form.addRow("Agent URL", self._a2a_url)
        form.addRow("Bearer token", self._a2a_token)
        layout.addLayout(form)
        actions = QHBoxLayout()
        local_hub = QPushButton("Use Local Hub")
        local_hub.clicked.connect(self._use_local_hub)
        actions.addWidget(local_hub)
        self._a2a_discover = QPushButton("Discover")
        self._a2a_discover.clicked.connect(self._discover_a2a)
        actions.addWidget(self._a2a_discover)
        actions.addStretch()
        layout.addLayout(actions)
        self._a2a_message = QLineEdit()
        self._a2a_message.setPlaceholderText('Message or JSON request, e.g. {"operation":"vm.list","args":{}}')
        layout.addWidget(self._a2a_message)
        send = QPushButton("Send A2A Message")
        send.clicked.connect(self._send_a2a)
        layout.addWidget(send)
        self._a2a_output = QTextEdit()
        self._a2a_output.setReadOnly(True)
        layout.addWidget(self._a2a_output)
        return page

    def _build_acp_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self._acp_command = QLineEdit(sys.executable)
        self._acp_arguments = QLineEdit('["-m", "vm_harness", "acp"]')
        self._acp_cwd = QLineEdit(str(PROJECT_ROOT))
        form.addRow("Executable", self._acp_command)
        form.addRow("Arguments (JSON list)", self._acp_arguments)
        form.addRow("Working directory", self._acp_cwd)
        layout.addLayout(form)
        actions = QHBoxLayout()
        self._acp_connect = QPushButton("Launch ACP Agent")
        self._acp_connect.clicked.connect(self._connect_acp)
        actions.addWidget(self._acp_connect)
        self._acp_disconnect = QPushButton("Disconnect")
        self._acp_disconnect.setEnabled(False)
        self._acp_disconnect.clicked.connect(self._disconnect_acp)
        actions.addWidget(self._acp_disconnect)
        actions.addStretch()
        layout.addLayout(actions)
        self._acp_input = QLineEdit()
        self._acp_input.setPlaceholderText("/ops vm.list, /help, or a supported agent prompt")
        self._acp_input.returnPressed.connect(self._send_acp)
        layout.addWidget(self._acp_input)
        send = QPushButton("Send ACP Prompt")
        send.clicked.connect(self._send_acp)
        layout.addWidget(send)
        self._acp_output = QTextEdit()
        self._acp_output.setReadOnly(True)
        layout.addWidget(self._acp_output)
        return page

    def _start_a2a_request(self, action: str, message: str = "") -> None:
        if self._a2a_worker is not None and self._a2a_worker.isRunning():
            return
        url = self._a2a_url.text().strip()
        if not url:
            QMessageBox.warning(self, "A2A", "Enter an A2A agent URL.")
            return
        self._a2a_worker = _A2AWorker(action, url, self._a2a_token.text(), message)
        self._a2a_worker.succeeded.connect(self._a2a_output.append)
        self._a2a_worker.failed.connect(lambda error: self._a2a_output.append(f"Error: {error}"))
        self._a2a_worker.finished.connect(self._a2a_request_finished)
        self._a2a_worker.start()

    def _discover_a2a(self) -> None:
        self._start_a2a_request("discover")

    def _use_local_hub(self) -> None:
        discovery = hub_alive()
        if not discovery:
            QMessageBox.warning(self, "A2A", "The VM-Harness hub is not running.")
            return
        url = discovery.get("a2a_url", "")
        if not url:
            QMessageBox.warning(self, "A2A", "The running hub did not publish an A2A endpoint.")
            return
        self._a2a_url.setText(url)
        self._a2a_token.setText(discovery["token"])

    def _send_a2a(self) -> None:
        message = self._a2a_message.text().strip()
        if not message:
            QMessageBox.warning(self, "A2A", "Enter a message or JSON operation request.")
            return
        self._start_a2a_request("send", message)

    def _a2a_request_finished(self) -> None:
        if self._a2a_worker is not None:
            self._a2a_worker.deleteLater()
            self._a2a_worker = None

    def _connect_acp(self) -> None:
        if self._acp_worker is not None and self._acp_worker.isRunning():
            return
        command = self._acp_command.text().strip()
        cwd = self._acp_cwd.text().strip()
        try:
            arguments = json.loads(self._acp_arguments.text())
        except json.JSONDecodeError as exc:
            QMessageBox.warning(self, "ACP", f"Arguments must be a JSON list: {exc}")
            return
        if not command or not isinstance(arguments, list) or not all(isinstance(arg, str) for arg in arguments):
            QMessageBox.warning(self, "ACP", "Enter an executable and a JSON list of string arguments.")
            return
        if not Path(cwd).is_dir():
            QMessageBox.warning(self, "ACP", f"Working directory does not exist: {cwd}")
            return
        self._acp_output.append(f"Launching ACP agent: {command}")
        self._acp_connect.setEnabled(False)
        self._acp_worker = _ACPClientWorker(command, arguments, cwd)
        self._acp_worker.ready.connect(self._acp_output.append)
        self._acp_worker.output.connect(self._acp_output.append)
        self._acp_worker.failed.connect(lambda error: self._acp_output.append(f"Error: {error}"))
        self._acp_worker.permission_requested.connect(self._approve_acp_operation)
        self._acp_worker.completed.connect(self._acp_worker_finished)
        self._acp_worker.start()

    def _send_acp(self) -> None:
        message = self._acp_input.text().strip()
        if not message or self._acp_worker is None:
            return
        try:
            self._acp_worker.send_prompt(message)
        except RuntimeError as exc:
            self._acp_output.append(f"Error: {exc}")
            return
        self._acp_output.append(f"> {message}")
        self._acp_input.clear()

    def _approve_acp_operation(
        self, permission_id: str, title: str, can_allow_once: bool, destructive: bool
    ) -> None:
        approved = False
        if can_allow_once:
            warning = "This operation is destructive and may be irreversible.\n\n" if destructive else ""
            answer = QMessageBox.question(
                self,
                "ACP operation approval",
                f"{warning}The ACP agent requests permission to run:\n\n{title}\n\nAllow this operation once?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            approved = answer == QMessageBox.Yes
        self._acp_output.append(
            f"{'Approved once' if approved else 'Denied'}: {title}"
        )
        if self._acp_worker is not None:
            self._acp_worker.resolve_permission(permission_id, approved)

    def _disconnect_acp(self) -> None:
        if self._acp_worker is not None:
            self._acp_output.append("Disconnecting ACP agent.")
            self._acp_disconnect.setEnabled(False)
            self._acp_worker.stop_agent()

    def _acp_worker_finished(self) -> None:
        self._acp_connect.setEnabled(True)
        self._acp_disconnect.setEnabled(False)
        if self._acp_worker is not None:
            self._acp_worker.deleteLater()
            self._acp_worker = None

    def shutdown(self) -> None:
        """Stop an active ACP child before the application exits."""
        a2a_worker = self._a2a_worker
        if a2a_worker is not None and a2a_worker.isRunning():
            a2a_worker.cancel_request()
            a2a_worker.wait(3000)
        worker = self._acp_worker
        if worker is not None and worker.isRunning():
            worker.stop_agent()
            worker.wait(3000)
