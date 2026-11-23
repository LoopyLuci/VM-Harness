"""ACP stdio agent and operation client for VM-Harness."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import uuid4

from acp import PROTOCOL_VERSION, run_agent
from acp.exceptions import RequestError
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    AllowedOutcome,
    AvailableCommand,
    AvailableCommandsUpdate,
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    PermissionOption,
    PromptResponse,
    RequestPermissionResponse,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
)

log = logging.getLogger("vmharness.protocols.acp")


class VMHarnessACPAgent:
    """Expose the VM-Harness operation catalog as an ACP stdio agent."""

    def __init__(self) -> None:
        self._connection: Any = None
        self._sessions: set[str] = set()

    def on_connect(self, conn: Any) -> None:
        self._connection = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Any = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        return InitializeResponse(
            protocolVersion=PROTOCOL_VERSION,
            agentCapabilities=AgentCapabilities(loadSession=False),
            agentInfo=Implementation(name="VM-Harness", version="0.3.0"),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        session_id = uuid4().hex
        self._sessions.add(session_id)
        if self._connection is not None:
            await self._connection.session_update(
                session_id,
                AvailableCommandsUpdate(
                    availableCommands=[
                        AvailableCommand(name="ops", description="List or search VM-Harness operations."),
                        AvailableCommand(
                            name="call",
                            description="Invoke an operation: /call vm.list or /call vm.start {\"name\":\"lab\"}.",
                        ),
                        AvailableCommand(name="help", description="Show VM-Harness ACP command help."),
                    ]
                ),
            )
        return NewSessionResponse(sessionId=session_id)

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        if session_id not in self._sessions:
            await self._message(session_id, "Unknown ACP session. Create a new session and try again.")
            return PromptResponse(stopReason="refusal")

        text = "\n".join(block.text for block in prompt if isinstance(block, TextContentBlock)).strip()
        try:
            result = await self._dispatch(session_id, text)
            if result is not None:
                await self._message(session_id, result)
            return PromptResponse(stopReason="end_turn")
        except Exception as exc:
            log.exception("ACP request failed")
            await self._message(session_id, f"Request failed: {exc}")
            return PromptResponse(stopReason="refusal")

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        return None

    async def close_session(self, session_id: str, **kwargs: Any) -> None:
        self._sessions.discard(session_id)

    async def _message(self, session_id: str, text: str) -> None:
        if self._connection is not None:
            await self._connection.session_update(
                session_id, AgentMessageChunk(content=TextContentBlock(text=text))
            )

    async def _dispatch(self, session_id: str, text: str) -> str:
        if text in ("/help", "help"):
            return (
                "VM-Harness ACP commands:\n"
                "  /ops [search terms]  list catalog operations\n"
                "  /call <operation> [JSON args]  run one operation\n"
                "Mutating operations require a one-time approval from the ACP client."
            )
        if text == "/ops" or text.startswith("/ops "):
            query = text[4:].strip()
            operations = await asyncio.to_thread(_operations, query)
            if not operations:
                return "No operations matched."
            return "\n".join(
                f"{'!' if op['destructive'] else ('*' if op['mutating'] else ' ')} "
                f"{op['id']}: {op['summary']}"
                for op in operations
            )
        operation, args = _parse_call(text)
        spec = await asyncio.to_thread(_find_operation, operation)
        tool_call_id = uuid4().hex
        raw_input = {"operation": operation, "args": args}
        await self._connection.session_update(
            session_id,
            ToolCallStart(
                toolCallId=tool_call_id,
                title=f"Run {operation}",
                name=operation,
                kind="delete" if spec["destructive"] else "execute",
                status="pending",
                rawInput=raw_input,
            ),
        )
        if spec["mutating"]:
            try:
                allowed = await self._request_permission(session_id, tool_call_id, spec, raw_input)
            except RequestError as exc:
                await self._connection.session_update(
                    session_id,
                    ToolCallProgress(
                        toolCallId=tool_call_id,
                        status="failed",
                        rawOutput={"error": "permission request failed"},
                    ),
                )
                return f"Permission could not be obtained; {operation} was not run: {exc}"
            if not allowed:
                await self._connection.session_update(
                    session_id,
                    ToolCallProgress(
                        toolCallId=tool_call_id,
                        status="failed",
                        rawOutput={"error": "permission denied"},
                    ),
                )
                return f"Permission denied; {operation} was not run."

        await self._connection.session_update(
            session_id,
            ToolCallProgress(toolCallId=tool_call_id, status="in_progress"),
        )
        try:
            result = await asyncio.to_thread(_call_operation, operation, args)
        except Exception as exc:
            await self._connection.session_update(
                session_id,
                ToolCallProgress(
                    toolCallId=tool_call_id,
                    status="failed",
                    rawOutput={"error": str(exc)},
                ),
            )
            raise
        await self._connection.session_update(
            session_id,
            ToolCallProgress(toolCallId=tool_call_id, status="completed", rawOutput=result),
        )
        return json.dumps({"operation": operation, "result": result}, indent=2, default=str)

    async def _request_permission(
        self,
        session_id: str,
        tool_call_id: str,
        spec: dict[str, Any],
        raw_input: dict[str, Any],
    ) -> bool:
        if self._connection is None:
            return False
        description = "This operation changes VM-Harness resources."
        if spec["destructive"]:
            description = "This operation deletes or overwrites resources and may be irreversible."
        response: RequestPermissionResponse = await self._connection.request_permission(
            session_id,
            ToolCallUpdate(
                toolCallId=tool_call_id,
                title=f"{spec['id']}: {spec['summary']}",
                name=spec["id"],
                kind="delete" if spec["destructive"] else "execute",
                status="pending",
                rawInput=raw_input,
            ),
            [
                PermissionOption(optionId="allow-once", name=f"Allow once — {description}", kind="allow_once"),
                PermissionOption(optionId="reject-once", name="Reject this operation", kind="reject_once"),
            ],
        )
        return isinstance(response.outcome, AllowedOutcome) and response.outcome.optionId == "allow-once"


def _client():
    from vm_harness.control.client import HubClient

    return HubClient.connect(start=True, client_name="acp")


def _operations(query: str) -> list[dict[str, Any]]:
    return _client().operations(query)


def _find_operation(operation: str) -> dict[str, Any]:
    matches = _client().operations(operation)
    for spec in matches:
        if spec["id"] == operation:
            return spec
    raise ValueError(f"unknown VM-Harness operation: {operation}")


def _call_operation(operation: str, args: dict[str, Any]) -> Any:
    return _client().call(operation, args)


def _parse_call(text: str) -> tuple[str, dict[str, Any]]:
    if text.startswith("/call "):
        text = text[6:].strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        operation, separator, raw_args = text.partition(" ")
        if not operation:
            raise ValueError("use /call <operation> [JSON args], /ops, or /help")
        if not separator:
            return operation, {}
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError as exc:
            raise ValueError("operation arguments must be a JSON object") from exc
        if not isinstance(args, dict):
            raise ValueError("operation arguments must be a JSON object")
        return operation, args
    if not isinstance(value, dict):
        raise ValueError("operation request must be a JSON object")
    extra = set(value) - {"operation", "args"}
    if extra:
        raise ValueError(f"unknown request field(s): {', '.join(sorted(extra))}")
    operation = value.get("operation")
    args = value.get("args", {})
    if not isinstance(operation, str) or not operation.strip():
        raise ValueError("operation must be a non-empty string")
    if not isinstance(args, dict):
        raise ValueError("operation args must be a JSON object")
    return operation.strip(), args


def main() -> int:
    asyncio.run(run_agent(VMHarnessACPAgent()))
    return 0
