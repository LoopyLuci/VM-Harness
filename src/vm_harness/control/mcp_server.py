"""VM-Harness as an MCP server: every catalog operation is an MCP tool.

    python -m vm_harness mcp                       # stdio, for Claude Desktop, ABP, Cursor, VS Code...
    python -m vm_harness mcp --tools compact       # 3 tools (search, describe, call) instead of one per operation
    python -m vm_harness mcp --groups vm,gui,host  # only some groups

The hub also serves the same protocol over HTTP at ``POST /mcp`` (Streamable HTTP, JSON responses).

It talks to the running hub (and starts one if none is running), so VMs, the audit log and the GUI window are shared
with every other client: a tool call from an MCP client can open the VM-Harness window and drive it.

The protocol is implemented here directly (JSON-RPC 2.0: initialize, tools/list, tools/call, ping) rather than through
the MCP SDK, whose server API changed incompatibly between major versions; tools are all this server offers.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
from typing import Any, Awaitable, Callable, Optional

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "vm-harness", "title": "VM-Harness", "version": "0.3.0"}
INSTRUCTIONS = (
    "VM-Harness controls virtual machines (QEMU, VirtualBox, VMware, Hyper-V, WSL, KVM), containers (Docker, Podman, "
    "Kubernetes, Compose) and its own desktop window. Start with host_backends to see what works on this machine and "
    "vm_list for the VMs. gui_launch opens the window; gui_panels, gui_inspect, gui_click, gui_set and gui_screenshot "
    "drive it. Tools marked destructive delete or overwrite something that cannot be recovered.")

CallFn = Callable[[str, dict], Awaitable[Any]]


def tool_name(op_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", op_id)[:64]


class McpHandler:
    """Answers MCP JSON-RPC messages using a list of operations and a function that runs one."""

    def __init__(self, operations: list[dict], call: CallFn, *, mode: str = "all",
                 groups: Optional[set[str]] = None) -> None:
        self.ops = [o for o in operations if not groups or o["group"] in groups]
        self.by_tool = {tool_name(o["id"]): o for o in self.ops}
        self.call_op = call
        self.mode = mode

    # ---- tools ---------------------------------------------------------------------------------------------------------
    def _tool(self, op: dict) -> dict:
        schema = dict(op.get("params") or {"type": "object"})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        flags = []
        if op.get("destructive"):
            flags.append("DESTRUCTIVE: deletes or overwrites something that cannot be recovered.")
        if op.get("needs") == "gui":
            flags.append("Needs the VM-Harness window (gui_launch opens it).")
        return {"name": tool_name(op["id"]), "title": op["id"],
                "description": " ".join([op.get("summary") or op["id"], *flags]).strip(),
                "inputSchema": schema,
                "annotations": {"title": op["id"], "readOnlyHint": not op.get("mutating"),
                                "destructiveHint": bool(op.get("destructive")), "idempotentHint": not op.get("mutating"),
                                "openWorldHint": op["group"] in ("iso", "image")}}

    def _meta_tools(self) -> list[dict]:
        groups = sorted({o["group"] for o in self.ops})
        return [
            {"name": "vmh_operations", "description": "Search VM-Harness operations (groups: " + ", ".join(groups) + ")",
             "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "group": {"type": "string"}}},
             "annotations": {"readOnlyHint": True}},
            {"name": "vmh_describe", "description": "An operation's arguments (JSON Schema) and whether it changes anything",
             "inputSchema": {"type": "object", "properties": {"operation": {"type": "string"}}, "required": ["operation"]},
             "annotations": {"readOnlyHint": True}},
            {"name": "vmh_call", "description": "Run any VM-Harness operation by id with its arguments",
             "inputSchema": {"type": "object", "properties": {"operation": {"type": "string"},
                                                              "args": {"type": "object"}}, "required": ["operation"]},
             "annotations": {"readOnlyHint": False, "destructiveHint": True}},
        ]

    def list_tools(self) -> list[dict]:
        if self.mode == "compact":
            return self._meta_tools()
        return [self._tool(o) for o in self.ops] + self._meta_tools()

    async def call_tool(self, name: str, args: dict) -> dict:
        try:
            if name == "vmh_operations":
                q = (args.get("query") or "").lower().split()
                found = [{"id": o["id"], "summary": o["summary"], "mutating": o["mutating"]} for o in self.ops
                         if (not args.get("group") or o["group"] == args["group"])
                         and all(w in f"{o['id']} {o['summary']}".lower() for w in q)]
                result: Any = found
            elif name == "vmh_describe":
                op = next((o for o in self.ops if o["id"] == args.get("operation")), None)
                if op is None:
                    raise ValueError(f"no operation {args.get('operation')!r}")
                result = op
            elif name == "vmh_call":
                result = await self.call_op(str(args.get("operation", "")), dict(args.get("args") or {}))
            elif name in self.by_tool:
                result = await self.call_op(self.by_tool[name]["id"], args)
            else:
                raise ValueError(f"unknown tool {name!r}")
        except Exception as e:  # noqa: BLE001 - tool errors are reported to the model, not as protocol errors
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
        content = [{"type": "text", "text": json.dumps(result, indent=1, default=str)[:200_000]}]
        image = _png_of(result)
        if image:
            content.insert(0, {"type": "image", "data": image, "mimeType": "image/png"})
            content[1]["text"] = json.dumps({k: v for k, v in result.items() if k != "base64"}, default=str)
        return {"content": content, "isError": False,
                **({"structuredContent": result} if isinstance(result, dict) and not image else {})}

    # ---- JSON-RPC ------------------------------------------------------------------------------------------------------
    async def handle(self, msg: dict) -> Optional[dict]:
        """One JSON-RPC message in, its response out (None for notifications)."""
        mid = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}
        if mid is None:
            return None            # notifications (initialized, cancelled, ...) need no answer
        try:
            if method == "initialize":
                asked = params.get("protocolVersion", PROTOCOL_VERSIONS[0])
                result: Any = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                               "capabilities": {"tools": {"listChanged": False}},
                               "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.list_tools()}
            elif method == "tools/call":
                result = await self.call_tool(params.get("name", ""), params.get("arguments") or {})
            elif method in ("resources/list", "resources/templates/list"):
                result = {"resources": []} if method == "resources/list" else {"resourceTemplates": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        except Exception as e:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def _png_of(result: Any) -> Optional[str]:
    if isinstance(result, dict) and result.get("format") == "png" and isinstance(result.get("base64"), str):
        return result["base64"]
    return None


async def serve_stdio(handler: McpHandler) -> None:
    """Newline-delimited JSON-RPC over stdin/stdout. Requests run concurrently; writes are serialized."""
    loop = asyncio.get_running_loop()
    # A thread reads stdin: on Windows an anonymous pipe cannot be registered with the proactor's IOCP.
    lines: asyncio.Queue[bytes] = asyncio.Queue()

    def pump() -> None:
        for raw in sys.stdin.buffer:
            loop.call_soon_threadsafe(lines.put_nowait, raw)
        loop.call_soon_threadsafe(lines.put_nowait, b"")

    threading.Thread(target=pump, name="mcp-stdin", daemon=True).start()
    out_lock = asyncio.Lock()
    out = sys.stdout.buffer

    async def respond(msg: dict) -> None:
        reply = await handler.handle(msg)
        if reply is not None:
            async with out_lock:
                out.write((json.dumps(reply, default=str) + "\n").encode())
                out.flush()

    tasks: set[asyncio.Task] = set()
    while True:
        line = await lines.get()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            async with out_lock:
                out.write(b'{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"parse error"}}\n')
                out.flush()
            continue
        for m in (msg if isinstance(msg, list) else [msg]):
            t = asyncio.create_task(respond(m))
            tasks.add(t)
            t.add_done_callback(tasks.discard)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    from vm_harness.control.client import HubClient

    ap = argparse.ArgumentParser(prog="vm-harness mcp")
    ap.add_argument("--tools", choices=["all", "compact"], default="all")
    ap.add_argument("--groups", default="", help="comma-separated operation groups to offer (default: all)")
    a = ap.parse_args(argv)
    hub = HubClient.connect(start=True, client_name="mcp")
    ops = hub.operations()

    async def call(op: str, args: dict) -> Any:
        return await asyncio.to_thread(hub.call, op, args)

    handler = McpHandler(ops, call, mode=a.tools, groups={g for g in a.groups.split(",") if g} or None)
    asyncio.run(serve_stdio(handler))
    return 0
