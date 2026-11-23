"""A2A 1.0 server and client adapters for the VM-Harness operation catalog."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import socket
from typing import Any, Optional
from uuid import uuid4

import httpx
import uvicorn
from a2a.client import ClientFactory
from a2a.client.client import ClientConfig
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    Message,
    Part,
    Role,
    SecurityRequirement,
    SecurityScheme,
    SendMessageRequest,
    StringList,
    Task,
    TaskState,
    TaskStatus,
)
from google.protobuf.json_format import MessageToDict
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

log = logging.getLogger("vmharness.protocols.a2a")


class _BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path == "/.well-known/agent-card.json":
            return await call_next(request)
        authorization = request.headers.get("Authorization", "")
        supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not supplied or not secrets.compare_digest(supplied, self._token):
            return JSONResponse(
                {"error": "missing or wrong bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)


class VMHarnessA2AExecutor(AgentExecutor):
    """Run one structured VM-Harness operation as an A2A task."""

    def __init__(self, hub: Any) -> None:
        self._hub = hub

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.task_id or not context.context_id:
            raise ValueError("A2A request did not include task and context IDs")

        if context.message is None:
            raise ValueError("A2A request did not include a message")
        await event_queue.enqueue_event(
            Task(
                id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                history=[context.message],
            )
        )
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()
        try:
            operation, args = _operation_request(context)
            result = await self._hub.call(operation, args, client="a2a")
            output = json.dumps({"operation": operation, "result": result}, default=str)
            await updater.add_artifact(parts=[Part(text=output)], name="result", last_chunk=True)
            await updater.complete()
        except asyncio.CancelledError:
            await updater.cancel()
            raise
        except Exception as exc:
            log.exception("A2A operation failed")
            message = updater.new_agent_message(parts=[Part(text=f"Operation failed: {exc}")])
            await updater.add_artifact(parts=[Part(text=str(exc))], name="error", last_chunk=True)
            await updater.failed(message=message)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.task_id or not context.context_id:
            raise ValueError("A2A cancellation did not include task and context IDs")
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


def _operation_request(context: RequestContext) -> tuple[str, dict[str, Any]]:
    message = context.message
    if message is None:
        raise ValueError("send a JSON object with operation and args")

    payload: Any = None
    for part in message.parts:
        if part.HasField("data"):
            payload = MessageToDict(part.data, preserving_proto_field_name=True)
            break
    if payload is None:
        text = context.get_user_input().strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("send JSON such as {\"operation\":\"vm.list\",\"args\":{}}") from exc

    if not isinstance(payload, dict):
        raise ValueError("request must be a JSON object")
    extra = set(payload) - {"operation", "args"}
    if extra:
        raise ValueError(f"unknown request field(s): {', '.join(sorted(extra))}")
    operation = payload.get("operation")
    args = payload.get("args", {})
    if not isinstance(operation, str) or not operation.strip():
        raise ValueError("operation must be a non-empty string")
    if not isinstance(args, dict):
        raise ValueError("args must be a JSON object")
    return operation.strip(), args


def build_agent_card(jsonrpc_url: str, version: str = "0.3.0") -> AgentCard:
    """Build the public discovery card for the VM-Harness A2A endpoint."""
    return AgentCard(
        name="VM-Harness",
        description=(
            "Execute VM-Harness operations through A2A. Send a JSON text part with "
            "{\"operation\":\"vm.list\",\"args\":{}}. Authenticated callers can execute "
            "operations that mutate or delete host and guest resources."
        ),
        supported_interfaces=[
            AgentInterface(url=jsonrpc_url, protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        version=version,
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        security_schemes={
            "bearerAuth": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="bearer")
            )
        },
        security_requirements=[SecurityRequirement(schemes={"bearerAuth": StringList(list=[])})],
        default_input_modes=["text", "application/json"],
        default_output_modes=["text"],
        skills=[
            AgentSkill(
                id="invoke-operation",
                name="Invoke a VM-Harness operation",
                description="Run one catalog operation with a JSON object of arguments.",
                tags=["virtual-machines", "containers", "host-control"],
                examples=['{"operation":"vm.list","args":{}}'],
                input_modes=["text", "application/json"],
                output_modes=["text"],
            )
        ],
    )


def build_a2a_app(hub: Any, base_url: str, token: str, version: str = "0.3.0") -> Starlette:
    """Build the A2A JSON-RPC server app, using the hub token for authenticated calls."""
    card = build_agent_card(f"{base_url.rstrip('/')}/a2a/jsonrpc", version=version)
    handler = DefaultRequestHandler(
        agent_executor=VMHarnessA2AExecutor(hub),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    app = Starlette(
        routes=[
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, rpc_url="/a2a/jsonrpc"),
        ],
        middleware=[Middleware(_BearerAuthMiddleware, token=token)],
    )
    return app


class A2AServer:
    """Own a loopback-first A2A HTTP server inside the hub process."""

    def __init__(
        self,
        hub: Any,
        token: str,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        advertised_url: str = "",
        version: str = "0.3.0",
    ) -> None:
        self.hub = hub
        self.token = token
        self.host = host
        self.port = port
        self.advertised_url = advertised_url.rstrip("/")
        self.version = version
        self.url = ""
        self._server: Optional[uvicorn.Server] = None
        self._task: Optional[asyncio.Task] = None
        self._socket: Optional[socket.socket] = None

    async def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
            sock.listen(128)
            sock.setblocking(False)
            self.port = int(sock.getsockname()[1])
            if self.advertised_url:
                self.url = self.advertised_url
            else:
                display_host = "127.0.0.1" if self.host in ("0.0.0.0", "") else self.host
                self.url = f"http://{display_host}:{self.port}"
            app = build_a2a_app(self.hub, self.url, self.token, version=self.version)
            config = uvicorn.Config(app, host=self.host, port=self.port, log_config=None, log_level="warning")
            self._server = uvicorn.Server(config)
            self._socket = sock
            self._task = asyncio.create_task(self._server.serve(sockets=[sock]), name="vmharness-a2a")
            await self._wait_started()
        except Exception:
            sock.close()
            if self._task is not None and not self._task.done():
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
            raise

    async def _wait_started(self) -> None:
        if self._server is None or self._task is None:
            raise RuntimeError("A2A server was not initialized")
        for _ in range(500):
            if self._task.done():
                await self._task
                raise RuntimeError("A2A server stopped before it became ready")
            if self._server.started:
                return
            await asyncio.sleep(0.01)
        raise TimeoutError("A2A server did not start within 5 seconds")

    async def stop(self) -> None:
        if self._server is None or self._task is None:
            return
        self._server.should_exit = True
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except asyncio.TimeoutError:
            self._server.force_exit = True
            await asyncio.wait_for(self._task, timeout=5)
        finally:
            self._server = None
            self._task = None
            self._socket = None


async def discover_agent(base_url: str, token: str = "", timeout: float = 10.0) -> dict[str, Any]:
    """Fetch an A2A agent card from the standard well-known endpoint."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
        response = await client.get(f"{base_url.rstrip('/')}/.well-known/agent-card.json")
        response.raise_for_status()
        card = response.json()
    if not isinstance(card, dict) or not card.get("name"):
        raise ValueError("the endpoint did not return a valid A2A Agent Card")
    return card


async def send_agent_message(
    base_url: str,
    text: str,
    token: str = "",
    timeout: float = 600.0,
) -> list[dict[str, Any]]:
    """Send a text message to a remote A2A agent using the official client SDK."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(headers=headers, timeout=timeout) as http_client:
        factory = ClientFactory(ClientConfig(httpx_client=http_client, streaming=False))
        client = await factory.create_from_url(base_url)
        request = SendMessageRequest(
            message=Message(message_id=uuid4().hex, role=Role.ROLE_USER, parts=[Part(text=text)])
        )
        events: list[dict[str, Any]] = []
        async for event in client.send_message(request):
            events.append(MessageToDict(event, preserving_proto_field_name=False))
        await client.close()
    return events


async def send_operation(
    base_url: str,
    operation: str,
    args: Optional[dict[str, Any]] = None,
    token: str = "",
    timeout: float = 600.0,
) -> list[dict[str, Any]]:
    """Send a structured VM-Harness operation request to an A2A agent."""
    payload = json.dumps({"operation": operation, "args": args or {}})
    return await send_agent_message(base_url, payload, token=token, timeout=timeout)
