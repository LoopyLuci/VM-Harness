"""The control plane: catalog generation, argument handling, the hub's HTTP API and auth, the audit chain, the MCP
protocol, and gui.* calls forwarded to an attached window. No hypervisor is needed: a fake backend stands in."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from enum import Enum

import aiohttp
import pytest

from vm_harness.control import schema as S
from vm_harness.control.audit import AuditLog, redact
from vm_harness.control.catalog import Catalog, OperationError
from vm_harness.control.engine import Engine
from vm_harness.control.hub import Hub
from vm_harness.control.mcp_server import McpHandler, tool_name
from vm_harness.control.ops import VM_METHODS, build_catalog


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VMH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("VMH_TOKEN", raising=False)
    return tmp_path / "home"


# ---- schema ---------------------------------------------------------------------------------------------------------
class Color(str, Enum):
    RED = "red"
    BLUE = "blue"


@dataclass
class Box:
    name: str
    size: int = 1
    color: Color = Color.RED
    tags: list[str] = field(default_factory=list)


def sample(box: Box, count: int = 2, note: str | None = None, flag: bool = False) -> dict:
    """Do a thing with a box"""
    return {"box": box, "count": count, "note": note, "flag": flag}


def test_signature_schema_from_type_hints():
    s = S.signature_schema(sample)
    assert s["required"] == ["box"]
    assert s["properties"]["count"] == {"type": "integer", "default": 2}
    assert s["properties"]["flag"]["type"] == "boolean"
    box = s["properties"]["box"]
    assert box["properties"]["color"] == {"type": "string", "enum": ["red", "blue"], "default": "red"}
    assert box["required"] == ["name"]


def test_bind_converts_json_to_python_types():
    kw = S.bind(sample, {"box": {"name": "b", "color": "blue"}, "count": "3", "flag": "true"})
    assert kw["box"] == Box(name="b", color=Color.BLUE) and kw["count"] == 3 and kw["flag"] is True


def test_bind_reports_unknown_and_missing_arguments():
    with pytest.raises(ValueError, match="unknown argument"):
        S.bind(sample, {"box": {"name": "b"}, "colour": 1})
    with pytest.raises(ValueError, match="missing required argument: box"):
        S.bind(sample, {})
    with pytest.raises(ValueError, match="unknown field"):
        S.bind(sample, {"box": {"name": "b", "nope": 1}})


def test_to_json_handles_dataclasses_enums_and_bytes():
    out = S.to_json({"b": Box("x"), "raw": b"\x00\x01"})
    assert out["b"]["color"] == "red" and out["raw"] == {"base64": "AAE=", "bytes": 2}


# ---- catalog --------------------------------------------------------------------------------------------------------
def test_catalog_covers_every_hypervisor_method():
    from vm_harness.hypervisor.backend import HypervisorBackend
    cat = build_catalog(Engine())
    public = {n for n in vars(HypervisorBackend) if not n.startswith("_") and callable(getattr(HypervisorBackend, n))}
    lifecycle = {"initialize", "shutdown", "probe_availability", "list_vms", "find_vm", "stream_metrics",
                 "on_vm_started", "on_vm_stopped", "on_vm_error"}
    missing = public - lifecycle - set(VM_METHODS)
    assert not missing, f"backend methods with no operation: {missing}"
    for method, (op_id, *_rest) in VM_METHODS.items():
        assert op_id in cat.ids()


def test_destructive_operations_are_marked():
    cat = build_catalog(Engine())
    for op_id in ("vm.destroy", "vm.snapshot.delete", "vm.snapshot.restore", "container.remove", "image.remove",
                  "docker.prune", "iso.delete", "k8s.delete_pod"):
        op = cat.get(op_id)
        assert op.destructive and op.mutating, op_id
    assert not cat.get("vm.status").mutating


def test_unknown_operation_suggests_close_ones():
    cat = build_catalog(Engine())
    with pytest.raises(OperationError, match="did you mean"):
        cat.get("vm.stat")


# ---- a hub with a fake hypervisor --------------------------------------------------------------------------------
class FakeHV:
    display_name = "Fake"
    version = "1.0"
    supported_features = {"start"}

    def __init__(self):
        self.vms = {"alpha": "stopped"}

    async def list_vms(self):
        return list(self.vms)

    async def start_vm(self, name, headless=False):
        self.vms[name] = "running"

    async def get_status(self, name):
        from vm_harness.hypervisor.backend import VMState, VMStatus
        return VMStatus(name=name, state=VMState(self.vms[name]), backend_name="qemu")


@pytest.fixture
def fake_engine(monkeypatch):
    engine = Engine()
    fake = FakeHV()

    async def hypervisor(name):
        if name != "qemu":
            raise OperationError(f"{name} unavailable", code="unavailable", status=409)
        return fake

    async def available():
        return ["qemu"]

    monkeypatch.setattr(engine, "hypervisor", hypervisor)
    monkeypatch.setattr(engine, "available_hypervisors", available)
    return engine, fake


async def _hub(engine):
    hub = Hub(port=0, engine=engine, token="t" * 40)
    await hub.start()
    return hub


def test_hub_calls_audits_and_refuses_without_token(fake_engine):
    engine, fake = fake_engine

    async def run():
        hub = await _hub(engine)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(hub.url + "/v1/health") as r:
                    assert (await r.json())["ok"]
                async with s.post(hub.url + "/v1/call/vm.list", json={}) as r:
                    assert r.status == 401
                h = {"Authorization": "Bearer " + "t" * 40, "X-VMH-Client": "pytest"}
                async with s.post(hub.url + "/v1/call/vm.start", json={"name": "alpha"}, headers=h) as r:
                    assert (await r.json())["ok"]
                async with s.post(hub.url + "/v1/call/vm.list", json={}, headers=h) as r:
                    vms = (await r.json())["result"]
                assert vms[0]["name"] == "alpha" and vms[0]["state"] == "running"
                async with s.post(hub.url + "/v1/call/vm.start", json={"name": "ghost"}, headers=h) as r:
                    body = await r.json()
                    assert r.status == 404 and body["code"] == "not_found"
                async with s.post(hub.url + "/v1/call/vm.start", json={"nme": "x"}, headers=h) as r:
                    assert (await r.json())["code"] == "bad_arguments"
                async with s.get(hub.url + "/v1/openapi.json", headers=h) as r:
                    spec = await r.json()
                    assert "/v1/call/vm.start" in spec["paths"]
        finally:
            await hub.stop()
        entries = hub.audit.query()
        assert [e["operation"] for e in entries][:2] == ["vm.start", "vm.start"]
        assert entries[-1]["client"] == "pytest" and entries[-1]["ok"]
        assert hub.audit.verify()[0]

    asyncio.run(run())


def test_hub_writes_and_removes_its_discovery_file(fake_engine, isolated_home):
    engine, _ = fake_engine

    async def run():
        hub = await _hub(engine)
        data = json.loads((isolated_home / "control.json").read_text())
        assert data["url"] == hub.url and data["token"] == "t" * 40
        assert data["a2a_url"] == hub.a2a_url and hub.a2a_url.startswith("http://127.0.0.1:")
        async with aiohttp.ClientSession() as session:
            async with session.get(hub.url + "/v1/health") as response:
                assert (await response.json())["a2a_url"] == hub.a2a_url
        await hub.stop()
        assert not (isolated_home / "control.json").exists()

    asyncio.run(run())


def test_gui_calls_are_forwarded_to_the_attached_window(fake_engine):
    engine, _ = fake_engine

    async def run():
        hub = await _hub(engine)
        h = {"Authorization": "Bearer " + "t" * 40}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(hub.url + "/v1/call/gui.panels", json={}, headers=h) as r:
                    assert (await r.json())["code"] == "gui_not_attached"
                ws = await s.ws_connect(hub.url.replace("http", "ws") + "/v1/gui/attach?pid=1", headers=h)

                async def window():
                    async for msg in ws:
                        data = json.loads(msg.data)
                        if data["op"] == "gui.open":
                            await ws.send_json({"type": "reply", "id": data["id"], "ok": True,
                                                "result": {"panel": data["args"]["panel"]}})
                        else:
                            await ws.send_json({"type": "reply", "id": data["id"], "ok": False, "error": "nope"})
                task = asyncio.create_task(window())
                for _ in range(50):
                    if hub.gui.attached:
                        break
                    await asyncio.sleep(0.02)
                async with s.post(hub.url + "/v1/call/gui.open", json={"panel": "snapshots"}, headers=h) as r:
                    assert (await r.json())["result"] == {"panel": "snapshots"}
                async with s.post(hub.url + "/v1/call/gui.click", json={"target": {"id": "x"}}, headers=h) as r:
                    assert (await r.json())["error"] == "nope"
                await ws.close()
                task.cancel()
        finally:
            await hub.stop()

    asyncio.run(run())


# ---- audit ----------------------------------------------------------------------------------------------------------
def test_audit_chain_detects_edits(tmp_path):
    log = AuditLog(tmp_path)
    log.record("vm.start", {"name": "a"}, client="t", ok=True)
    log.record("vm.stop", {"name": "a", "password": "hunter2"}, client="t", ok=True)
    assert log.verify()[0]
    assert "hunter2" not in log.path.read_text()
    lines = log.path.read_text().splitlines()
    lines[0] = lines[0].replace('"a"', '"b"')
    log.path.write_text("\n".join(lines) + "\n")
    ok, why = log.verify()
    assert not ok and "line 1" in why


def test_redact_hides_secret_looking_keys():
    assert redact({"guest_password": "x", "name": "vm", "nested": {"api_key": "k"}}) == \
        {"guest_password": "***", "name": "vm", "nested": {"api_key": "***"}}


# ---- MCP ----------------------------------------------------------------------------------------------------------
def test_mcp_handler_lists_and_calls_tools():
    ops = [{"id": "vm.start", "group": "vm", "summary": "Start", "params": {"type": "object"}, "mutating": True,
            "destructive": False, "needs": ""},
           {"id": "vm.destroy", "group": "vm", "summary": "Delete", "params": {"type": "object"}, "mutating": True,
            "destructive": True, "needs": ""}]
    calls = []

    async def call(op, args):
        calls.append((op, args))
        if op == "vm.destroy":
            raise OperationError("no such VM")
        return {"ok": 1}

    h = McpHandler(ops, call)

    async def run():
        init = await h.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-06-18"}})
        assert init["result"]["protocolVersion"] == "2025-06-18"
        assert await h.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
        tools = (await h.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))["result"]["tools"]
        names = {t["name"] for t in tools}
        assert {"vm_start", "vm_destroy", "vmh_call"} <= names
        destroy = next(t for t in tools if t["name"] == "vm_destroy")
        assert destroy["annotations"]["destructiveHint"] and "DESTRUCTIVE" in destroy["description"]
        ok = (await h.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                              "params": {"name": "vm_start", "arguments": {"name": "a"}}}))["result"]
        assert not ok["isError"] and calls[-1] == ("vm.start", {"name": "a"})
        bad = (await h.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                               "params": {"name": "vm_destroy", "arguments": {}}}))["result"]
        assert bad["isError"] and "no such VM" in bad["content"][0]["text"]
        via = (await h.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                               "params": {"name": "vmh_call", "arguments": {"operation": "vm.start"}}}))["result"]
        assert not via["isError"]
        unknown = await h.handle({"jsonrpc": "2.0", "id": 6, "method": "nope"})
        assert unknown["error"]["code"] == -32601

    asyncio.run(run())


def test_mcp_tool_names_are_valid():
    for op in build_catalog(Engine()).ids():
        name = tool_name(op)
        assert name and len(name) <= 64 and all(c.isalnum() or c in "_-" for c in name)


def test_compact_mode_offers_three_tools():
    h = McpHandler([{"id": "vm.start", "group": "vm", "summary": "", "params": {}, "mutating": True}],
                   lambda o, a: None, mode="compact")
    assert [t["name"] for t in h.list_tools()] == ["vmh_operations", "vmh_describe", "vmh_call"]


# ---- A2A and ACP -----------------------------------------------------------------------------------------------
def test_a2a_card_authentication_and_operation_round_trip():
    from vm_harness.protocols.a2a import A2AServer, discover_agent, send_operation

    class FakeHub:
        async def call(self, operation, args, client="local"):
            return {"operation": operation, "args": args, "client": client}

    async def run():
        server = A2AServer(FakeHub(), "t" * 40)
        await server.start()
        try:
            card = await discover_agent(server.url)
            assert card["name"] == "VM-Harness"
            assert card["securitySchemes"]["bearerAuth"]["httpAuthSecurityScheme"]["scheme"] == "bearer"
            async with aiohttp.ClientSession() as session:
                async with session.post(server.url + "/a2a/jsonrpc", json={}) as response:
                    assert response.status == 401
            events = await send_operation(server.url, "vm.list", {"limit": 1}, token="t" * 40)
            task = events[0]["task"]
            result = json.loads(task["artifacts"][0]["parts"][0]["text"])
            assert task["status"]["state"] == "TASK_STATE_COMPLETED"
            assert result["result"]["client"] == "a2a"
        finally:
            await server.stop()

    asyncio.run(run())


def test_acp_requires_one_time_permission_for_mutating_operations(monkeypatch):
    from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse
    import vm_harness.protocols.acp as acp

    spec = {"id": "vm.start", "summary": "Start VM", "mutating": True, "destructive": False}
    calls = []

    class FakeConnection:
        def __init__(self, outcome):
            self.outcome = outcome
            self.updates = []
            self.options = []

        async def session_update(self, session_id, update):
            self.updates.append(update)

        async def request_permission(self, session_id, tool_call, options):
            self.options = options
            return RequestPermissionResponse(outcome=self.outcome)

    monkeypatch.setattr(acp, "_find_operation", lambda operation: spec)
    monkeypatch.setattr(acp, "_call_operation", lambda operation, args: calls.append((operation, args)) or {"ok": True})

    async def run():
        denied_connection = FakeConnection(DeniedOutcome())
        denied_agent = acp.VMHarnessACPAgent()
        denied_agent.on_connect(denied_connection)
        denied = await denied_agent._dispatch("session", "/call vm.start {\"name\":\"lab\"}")
        assert "Permission denied" in denied
        assert not calls
        assert {option.kind for option in denied_connection.options} == {"allow_once", "reject_once"}
        assert denied_connection.updates[-1].status == "failed"

        allowed_connection = FakeConnection(AllowedOutcome(optionId="allow-once"))
        allowed_agent = acp.VMHarnessACPAgent()
        allowed_agent.on_connect(allowed_connection)
        result = await allowed_agent._dispatch("session", "/call vm.start {\"name\":\"lab\"}")
        assert calls == [("vm.start", {"name": "lab"})]
        assert '"ok": true' in result
        assert allowed_connection.updates[-1].status == "completed"

    asyncio.run(run())


def test_acp_operation_request_validation():
    from vm_harness.protocols.acp import _parse_call

    assert _parse_call('/call vm.list {"limit":1}') == ("vm.list", {"limit": 1})
    assert _parse_call('{"operation":"vm.list"}') == ("vm.list", {})
    with pytest.raises(ValueError, match="JSON object"):
        _parse_call("/call vm.start []")


def test_acp_stdio_handshake_session_and_help_round_trip():
    import os
    import sys
    from pathlib import Path

    from acp import PROTOCOL_VERSION, spawn_agent_process
    from acp.schema import AgentMessageChunk, Implementation, TextContentBlock

    root = Path(__file__).resolve().parents[1]
    updates = []

    class Client:
        async def session_update(self, session_id, update, **kwargs):
            if isinstance(update, AgentMessageChunk) and isinstance(update.content, TextContentBlock):
                updates.append(update.content.text)

        async def request_permission(self, session_id, tool_call, options, **kwargs):
            from acp.schema import DeniedOutcome, RequestPermissionResponse

            return RequestPermissionResponse(outcome=DeniedOutcome())

    async def run():
        env = dict(os.environ)
        paths = [str(root / "src"), str(root)]
        if env.get("PYTHONPATH"):
            paths.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(paths)
        async with spawn_agent_process(
            Client(),
            sys.executable,
            "-m",
            "vm_harness",
            "acp",
            cwd=root,
            env=env,
        ) as (connection, _process):
            initialized = await connection.initialize(
                protocol_version=PROTOCOL_VERSION,
                client_info=Implementation(name="test", version="1"),
            )
            session = await connection.new_session(cwd=str(root))
            response = await connection.prompt(
                session_id=session.sessionId,
                prompt=[TextContentBlock(text="/help")],
            )
            assert initialized.protocolVersion == PROTOCOL_VERSION
            assert response.stopReason == "end_turn"

    asyncio.run(run())
    assert any("VM-Harness ACP commands" in update for update in updates)


def test_a2a_cli_uses_local_hub_credentials_and_structured_arguments(monkeypatch, capsys):
    import vm_harness.__main__ as cli
    import vm_harness.protocols.a2a as a2a
    from vm_harness.control.client import HubClient

    calls = []

    class FakeHub:
        token = "local-token"

        def health(self):
            return {"a2a_url": "http://127.0.0.1:1234"}

    async def send(base_url, text, token=""):
        calls.append((base_url, json.loads(text), token))
        return [{"ok": True}]

    monkeypatch.setattr(HubClient, "connect", classmethod(lambda cls, **kwargs: FakeHub()))
    monkeypatch.setattr(a2a, "send_agent_message", send)
    assert cli._a2a(["send", "--operation", "vm.list", "--json", '{"limit":1}']) == 0
    assert calls == [
        ("http://127.0.0.1:1234", {"operation": "vm.list", "args": {"limit": 1}}, "local-token")
    ]
    assert json.loads(capsys.readouterr().out) == [{"ok": True}]
