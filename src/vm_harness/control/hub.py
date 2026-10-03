"""The VM-Harness hub: one local service that every client talks to.

    python -m vm_harness serve                 # 127.0.0.1:8765 by default; --port / --host to change

It hosts the engine (the hypervisor and container backends) and serves the catalog over HTTP:

    GET  /v1/health                         no token needed: {ok, version, pid, gui}
    GET  /v1/operations?q=&group=           the catalog
    GET  /v1/operations/{id}
    POST /v1/call/{id}      {args}          run one: {ok, result, duration_s} or {ok: false, error, code}
    GET  /v1/openapi.json                   the same catalog as OpenAPI 3.1
    GET  /v1/events         (WebSocket)     every call, GUI attach/detach, as it happens
    GET  /v1/gui/attach     (WebSocket)     the GUI window registers here and receives gui.* calls
    POST /v1/service/stop

**Finding it.** On start the hub writes ``<home>/control.json`` with its URL, token and pid; clients (the MCP server,
the CLI, the GUI, ABP) read it, so nobody configures a port or copies a token. The token is generated once and kept in
``<home>/token``; ``VMH_TOKEN`` overrides it. Requests carry it as ``Authorization: Bearer ...`` or ``X-VMH-Token``.

**Safety.** It listens on 127.0.0.1 unless told otherwise, every request but /v1/health needs the token, and every
operation that changes something is written to the audit log with who asked.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from aiohttp import WSMsgType, web

from vm_harness.control.audit import AuditLog
from vm_harness.control.catalog import Catalog, OperationError
from vm_harness.control.engine import Engine, home
from vm_harness.control.gui_ops import GUI_OPS
from vm_harness.control.ops import build_catalog

log = logging.getLogger("vmharness.hub")

VERSION = "0.3.0"
DEFAULT_PORT = 8765
MAX_BODY = 64 * 2**20


def load_token() -> str:
    if os.environ.get("VMH_TOKEN"):
        return os.environ["VMH_TOKEN"]
    f = home() / "token"
    try:
        tok = f.read_text(encoding="utf-8").strip()
        if len(tok) >= 32:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(32)
    f.write_text(tok, encoding="utf-8")
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass
    return tok


def discovery_path() -> Path:
    return home() / "control.json"


def read_discovery() -> Optional[dict]:
    try:
        return json.loads(discovery_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class GuiLink:
    """The attached GUI window, if any: gui.* calls go to it and come back matched by request id."""

    def __init__(self) -> None:
        self.ws: Optional[web.WebSocketResponse] = None
        self.info: dict[str, Any] = {}
        self._pending: dict[str, asyncio.Future] = {}

    @property
    def attached(self) -> bool:
        return self.ws is not None and not self.ws.closed

    async def call(self, op_id: str, args: dict, timeout: float = 60.0) -> Any:
        if not self.attached:
            raise OperationError("the VM-Harness window is not open; call gui.launch first", code="gui_not_attached",
                                 status=409)
        rid = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self.ws.send_json({"type": "call", "id": rid, "op": op_id, "args": args})  # type: ignore[union-attr]
            reply = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise OperationError(f"the window did not answer {op_id} within {timeout:.0f}s", code="timeout", status=504)
        finally:
            self._pending.pop(rid, None)
        if not reply.get("ok"):
            raise OperationError(reply.get("error") or "the window reported an error", code=reply.get("code", "gui_error"))
        return reply.get("result")

    def resolve(self, msg: dict) -> None:
        fut = self._pending.get(msg.get("id", ""))
        if fut and not fut.done():
            fut.set_result(msg)

    def detach(self) -> None:
        self.ws = None
        self.info = {}
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result({"ok": False, "error": "the window closed", "code": "gui_not_attached"})
        self._pending.clear()


class Hub:
    def __init__(self, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT, engine: Optional[Engine] = None,
                 token: Optional[str] = None, write_discovery: bool = True) -> None:
        self.host, self.port = host, port
        self.engine = engine or Engine()
        self.audit = AuditLog(home() / "audit")
        self.catalog: Catalog = build_catalog(self.engine, self.audit)
        self.token = token or load_token()
        self.gui = GuiLink()
        self.subscribers: set[web.WebSocketResponse] = set()
        self.started = time.time()
        self.write_discovery = write_discovery
        self._runner: Optional[web.AppRunner] = None
        self._stop = asyncio.Event()
        self._mcp_handler: Any = None
        self._add_service_ops()
        self._add_gui_ops()

    # ---- operations only the hub can provide ---------------------------------------------------------------------
    def _add_service_ops(self) -> None:
        cat = self.catalog

        @cat.op("service.info", group="service")
        async def info() -> dict:
            """The hub: version, pid, uptime, address, whether the window is attached, operation counts"""
            return {"version": VERSION, "pid": os.getpid(), "uptime_s": round(time.time() - self.started),
                    "url": self.url, "gui_attached": self.gui.attached, "gui": self.gui.info,
                    "operations": len(cat.ids()), "groups": cat.groups(), "home": str(home())}

        @cat.op("service.operations", group="service")
        async def operations(query: str = "", group: str = "") -> list:
            """Search the catalog: operation ids, summaries and argument schemas"""
            return [o.describe() for o in cat.search(query, group)]

    def _add_gui_ops(self) -> None:
        cat = self.catalog
        for spec in GUI_OPS:
            def make(op_id: str = spec["id"]):
                async def forward(**args: Any) -> Any:
                    return await self.gui.call(op_id, args)
                return forward
            handler = make()
            # The schema is the GUI's own; arguments pass through untouched.
            cat.add(spec["id"], handler, group="gui", summary=spec["summary"], params=spec["params"],
                    mutating=spec.get("mutating", False), needs="gui")
            cat.get(spec["id"]).handler = _passthrough(handler)

        @cat.op("gui.launch", group="gui", mutating=True)
        async def launch(wait_s: float = 60.0, show: bool = True) -> dict:
            """Open the VM-Harness window (if it is not open) and wait until it is attached"""
            if self.gui.attached:
                if show:
                    await self.gui.call("gui.window", {"action": "restore"})
                return {"attached": True, "already_open": True, **self.gui.info}
            root = Path(__file__).resolve().parents[3]
            exe = Path(sys.executable)
            windowless = exe.with_name("pythonw.exe")
            env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root), str(root / "src")])}
            flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # DETACHED_PROCESS | NEW_PROCESS_GROUP
            subprocess.Popen([str(windowless if windowless.exists() else exe), "-m", "gui"], cwd=str(root), env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=flags, start_new_session=os.name != "nt")
            deadline = time.time() + wait_s
            while time.time() < deadline:
                if self.gui.attached:
                    return {"attached": True, "already_open": False, **self.gui.info}
                await asyncio.sleep(0.5)
            raise OperationError(f"the window did not attach within {wait_s:.0f}s", code="timeout", status=504)

        @cat.op("gui.status", group="gui")
        async def status() -> dict:
            """Whether the VM-Harness window is open and attached to this hub"""
            return {"attached": self.gui.attached, **self.gui.info}

    # ---- calling (shared by HTTP and in-process callers) -----------------------------------------------------------
    async def call(self, op_id: str, args: dict, *, client: str = "local") -> Any:
        op = self.catalog.get(op_id)
        t = time.perf_counter()
        try:
            result = await self.catalog.call(op.id, args)
        except Exception as e:
            dt = time.perf_counter() - t
            if op.mutating:
                self.audit.record(op.id, args, client=client, ok=False, error=str(e), duration_s=dt)
            await self._publish({"type": "call", "op": op.id, "ok": False, "error": str(e)[:300], "client": client,
                                 "duration_s": round(dt, 3)})
            raise
        dt = time.perf_counter() - t
        if op.mutating:
            self.audit.record(op.id, args, client=client, ok=True, duration_s=dt)
        await self._publish({"type": "call", "op": op.id, "ok": True, "client": client, "duration_s": round(dt, 3),
                             "mutating": op.mutating})
        return result

    async def _publish(self, event: dict) -> None:
        event.setdefault("ts", time.time())
        dead = []
        for ws in self.subscribers:
            try:
                await ws.send_json(event)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.subscribers.discard(ws)

    # ---- HTTP --------------------------------------------------------------------------------------------------------
    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{host}:{self.port}"

    def _authorized(self, request: web.Request) -> bool:
        given = request.headers.get("X-VMH-Token", "")
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            given = auth[7:]
        if not given:
            given = request.query.get("token", "")   # WebSockets from a browser cannot set headers
        return bool(given) and secrets.compare_digest(given, self.token)

    @web.middleware
    async def _middleware(self, request: web.Request, handler):
        if request.path != "/v1/health" and not self._authorized(request):
            return web.json_response({"ok": False, "error": "missing or wrong token", "code": "unauthorized"},
                                     status=401)
        try:
            return await handler(request)
        except OperationError as e:
            return web.json_response({"ok": False, "error": str(e), "code": e.code}, status=e.status)
        except web.HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("request failed: %s", request.path)
            return web.json_response({"ok": False, "error": f"{type(e).__name__}: {e}", "code": "internal"}, status=500)

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._middleware], client_max_size=MAX_BODY)
        app.router.add_get("/v1/health", self._health)
        app.router.add_get("/v1/operations", self._operations)
        app.router.add_get("/v1/operations/{op}", self._operation)
        app.router.add_post("/v1/call/{op}", self._call)
        app.router.add_get("/v1/openapi.json", self._openapi)
        app.router.add_get("/v1/events", self._events)
        app.router.add_get("/v1/gui/attach", self._gui_attach)
        app.router.add_post("/v1/service/stop", self._service_stop)
        app.router.add_post("/mcp", self._mcp)
        app.router.add_get("/mcp", self._mcp_get)
        return app

    async def _health(self, _r: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "vm-harness", "version": VERSION, "pid": os.getpid(),
                                  "gui": self.gui.attached})

    async def _operations(self, r: web.Request) -> web.Response:
        ops = self.catalog.search(r.query.get("q", ""), r.query.get("group", ""))
        return web.json_response({"ok": True, "count": len(ops), "groups": self.catalog.groups(),
                                  "operations": [o.describe() for o in ops]})

    async def _operation(self, r: web.Request) -> web.Response:
        return web.json_response({"ok": True, "operation": self.catalog.get(r.match_info["op"]).describe()})

    async def _call(self, r: web.Request) -> web.Response:
        try:
            args = await r.json() if r.can_read_body else {}
        except ValueError:
            raise OperationError("the body must be a JSON object of arguments")
        if not isinstance(args, dict):
            raise OperationError("the body must be a JSON object of arguments")
        client = r.headers.get("X-VMH-Client", "api")[:60]
        t = time.perf_counter()
        try:
            result = await self.call(r.match_info["op"], args, client=client)
        except (ValueError, TypeError) as e:
            raise OperationError(str(e), code="bad_arguments")
        return web.json_response({"ok": True, "result": result, "duration_s": round(time.perf_counter() - t, 3)},
                                 dumps=lambda o: json.dumps(o, default=str))

    async def _openapi(self, _r: web.Request) -> web.Response:
        paths: dict[str, Any] = {}
        for op in self.catalog.all():
            paths[f"/v1/call/{op.id}"] = {"post": {
                "operationId": op.id.replace(".", "_"), "summary": op.summary, "tags": [op.group],
                "requestBody": {"required": bool(op.params.get("required")),
                                "content": {"application/json": {"schema": op.params}}},
                "responses": {"200": {"description": "ok"}},
                "x-mutating": op.mutating, "x-destructive": op.destructive}}
        return web.json_response({"openapi": "3.1.0", "info": {"title": "VM-Harness", "version": VERSION},
                                  "servers": [{"url": self.url}],
                                  "components": {"securitySchemes": {"token": {"type": "http", "scheme": "bearer"}}},
                                  "security": [{"token": []}], "paths": paths})

    async def _events(self, r: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(r)
        self.subscribers.add(ws)
        await ws.send_json({"type": "hello", "version": VERSION, "gui": self.gui.attached})
        try:
            async for _ in ws:
                pass
        finally:
            self.subscribers.discard(ws)
        return ws

    async def _gui_attach(self, r: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_BODY)
        await ws.prepare(r)
        if self.gui.attached:
            await ws.send_json({"type": "refused", "reason": "another window is already attached"})
            await ws.close()
            return ws
        self.gui.ws = ws
        self.gui.info = {"pid": int(r.query.get("pid", 0) or 0), "attached_at": time.time()}
        await self._publish({"type": "gui", "attached": True})
        self._write_discovery()
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("type") == "reply":
                        self.gui.resolve(data)
                    elif data.get("type") == "hello":
                        self.gui.info.update({k: v for k, v in data.items() if k != "type"})
                    elif data.get("type") == "event":
                        await self._publish({"type": "gui_event", **{k: v for k, v in data.items() if k != "type"}})
        finally:
            if self.gui.ws is ws:
                self.gui.detach()
                await self._publish({"type": "gui", "attached": False})
                self._write_discovery()
        return ws

    async def _mcp(self, r: web.Request) -> web.Response:
        """MCP over Streamable HTTP (JSON responses): the same tools as `vm-harness mcp`, served by the hub itself."""
        from vm_harness.control.mcp_server import McpHandler

        if self._mcp_handler is None:
            async def call(op: str, args: dict) -> Any:
                return await self.call(op, args, client="mcp-http")
            self._mcp_handler = McpHandler([o.describe() for o in self.catalog.all()], call)
        try:
            body = await r.json()
        except ValueError:
            return web.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}},
                                     status=400)
        msgs = body if isinstance(body, list) else [body]
        replies = [x for x in [await self._mcp_handler.handle(m) for m in msgs] if x is not None]
        if not replies:
            return web.Response(status=202)
        return web.json_response(replies if isinstance(body, list) else replies[0], dumps=lambda o: json.dumps(o, default=str))

    async def _mcp_get(self, _r: web.Request) -> web.Response:
        return web.Response(status=405, text="this MCP endpoint answers POST requests only (no server-sent stream)")

    async def _service_stop(self, _r: web.Request) -> web.Response:
        asyncio.get_running_loop().call_later(0.2, self._stop.set)
        return web.json_response({"ok": True, "stopping": True})

    # ---- running -------------------------------------------------------------------------------------------------------
    def _write_discovery(self) -> None:
        if not self.write_discovery:
            return
        data = {"url": self.url, "token": self.token, "pid": os.getpid(), "version": VERSION,
                "started": self.started, "gui": self.gui.attached}
        tmp = discovery_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(discovery_path())

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        if self.port == 0:
            self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self._write_discovery()
        log.info("VM-Harness hub on %s (%d operations)", self.url, len(self.catalog.ids()))

    async def stop(self) -> None:
        for ws in list(self.subscribers):
            await ws.close()
        if self.gui.ws is not None:
            await self.gui.ws.close()
        if self._runner:
            await self._runner.cleanup()
        await self.engine.close()
        d = read_discovery()
        if self.write_discovery and d and d.get("pid") == os.getpid():
            discovery_path().unlink(missing_ok=True)

    async def serve_forever(self) -> None:
        await self.start()
        try:
            await self._stop.wait()
        finally:
            await self.stop()


def _passthrough(handler):
    """GUI operations take whatever arguments their schema allows; the window validates them."""
    async def call(**kwargs: Any) -> Any:
        return await handler(**kwargs)
    import inspect
    call.__signature__ = inspect.Signature([inspect.Parameter("kwargs", inspect.Parameter.VAR_KEYWORD)])  # type: ignore[attr-defined]
    return call


def hub_alive(timeout: float = 2.0) -> Optional[dict]:
    """The running hub's discovery record, if a hub answers at that address; else None."""
    import urllib.request
    d = read_discovery()
    if not d:
        return None
    try:
        with urllib.request.urlopen(d["url"] + "/v1/health", timeout=timeout) as r:
            health = json.loads(r.read())
        return d if health.get("ok") and health.get("pid") == d.get("pid") else None
    except Exception:  # noqa: BLE001
        return None


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="vm-harness serve")
    ap.add_argument("--host", default=os.environ.get("VMH_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("VMH_PORT", DEFAULT_PORT)))
    ap.add_argument("--log-level", default="INFO")
    a = ap.parse_args(argv)
    logging.basicConfig(level=a.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    existing = hub_alive()
    if existing:
        print(f"a VM-Harness hub is already running at {existing['url']} (pid {existing['pid']})")
        return 0
    hub = Hub(host=a.host, port=a.port)
    try:
        asyncio.run(hub.serve_forever())
    except KeyboardInterrupt:
        pass
    return 0
