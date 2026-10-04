"""Tests for streaming_bridge: the per-VM frame + input server.

Everything here runs against a fake QMP client and a fake VM registry. No QEMU,
no Docker, no display, no network beyond a loopback TestServer, so these tests
say something about the bridge's own logic -- auth, clamping, per-VM isolation,
QMP command shapes -- rather than about whether a VM happens to be booted.

The QMP command sequences asserted here are the contract with the guest:

    screendump   {"execute":"screendump","arguments":{"filename":...,"format":"png"}}
    key          {"execute":"human-monitor-command",
                  "arguments":{"command-line":"sendkey <name>"}}
    mouse move   {"execute":"input-send-event","arguments":{"events":[abs x, abs y]}}
    mouse click  {"execute":"input-send-event","arguments":{"events":[btn down/up]}}
    scroll       {"execute":"input-send-event","arguments":{"events":[wheel up/down]}}
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for candidate in (PROJECT_ROOT, PROJECT_ROOT / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import aiohttp
from aiohttp.test_utils import TestServer
from PIL import Image

import streaming_bridge as sb


# ── Fakes ─────────────────────────────────────────────────────────────────────

TOKEN = "test-token-0123456789"


def make_png(width: int = 64, height: int = 48, colour: tuple = (12, 200, 90)) -> bytes:
    """A real PNG, so the capture path exercises Pillow's decoder."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeQMPClient:
    """Stands in for ``vm_harness.qmp_client.QMPClient``.

    Records every command and answers ``screendump`` by writing real PNG bytes,
    so the bridge's temp-file dance and its PNG decode are genuinely executed.
    """

    def __init__(self, uri: str, width: int = 64, height: int = 48,
                 colour: tuple = (12, 200, 90)) -> None:
        self.uri = uri
        self.commands: List[Tuple[str, Optional[dict]]] = []
        self.is_connected = False
        self.screendumps = 0
        self._png = make_png(width, height, colour)

    async def connect(self) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False

    async def send(self, cmd: str, args: Optional[dict] = None) -> Dict[str, Any]:
        self.commands.append((cmd, args))
        if cmd == "screendump":
            self.screendumps += 1
            Path(args["filename"]).write_bytes(self._png)  # type: ignore[index]
        return {}

    def of(self, cmd: str) -> List[dict]:
        return [args for name, args in self.commands if name == cmd]

    def clear(self) -> None:
        self.commands.clear()


class FakeRegistry:
    """A ``VMRegistry`` stand-in with a fixed set of streamable VMs."""

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


class FakeExecSession:
    """``ExecSessionManager`` stand-in: records commands, returns canned output."""

    def __init__(self, output: str = "hello\n") -> None:
        self.commands: List[Tuple[str, str]] = []
        self._output = output
        self.closed: List[str] = []

    async def run_command(self, container: str, command: str) -> str:
        self.commands.append((container, command))
        return self._output

    async def close_all(self) -> None:
        self.closed.append("all")


class Harness:
    """A running bridge on a loopback port, with fake QMP behind it."""

    def __init__(self, registry: Optional[FakeRegistry] = None,
                 exec_sessions: Optional[FakeExecSession] = None) -> None:
        self.clients: Dict[str, FakeQMPClient] = {}
        self.registry = registry or FakeRegistry()
        self.exec_sessions = exec_sessions or FakeExecSession()
        self.bridge = sb.StreamingBridge(
            registry=self.registry,  # type: ignore[arg-type]
            authenticator=sb.TokenAuthenticator(TOKEN),
            host="127.0.0.1",
            port=0,
            client_factory=self._make_client,
            exec_sessions=self.exec_sessions,  # type: ignore[arg-type]
        )
        self._server: Optional[TestServer] = None
        self.base_url = ""

    def _make_client(self, uri: str) -> FakeQMPClient:
        client = FakeQMPClient(uri)
        self.clients[uri] = client
        return client

    async def start(self) -> None:
        self._server = TestServer(self.bridge.build_app())
        await self._server.start_server()
        self.base_url = f"http://127.0.0.1:{self._server.port}"

    async def stop(self) -> None:
        await self.bridge.stop()
        if self._server is not None:
            await self._server.close()
            self._server = None

    def qmp(self, vm: str) -> FakeQMPClient:
        uri = self.registry.get(vm).qmp_uri  # type: ignore[union-attr]
        return self.clients[uri]


@pytest.fixture
async def harness() -> Harness:
    running = Harness()
    await running.start()
    try:
        yield running
    finally:
        await running.stop()


# ── helpers ───────────────────────────────────────────────────────────────────

@contextlib.asynccontextmanager
async def stream(base_url: str, *, path: str = "/ws/stream", auth: bool = True,
                 token: str = TOKEN):
    """Open a WebSocket, authenticated by default, with its session cleaned up."""
    session = aiohttp.ClientSession()
    ws = await session.ws_connect(f"{base_url}{path}")
    try:
        if auth:
            await ws.send_str(json.dumps({"type": "auth", "key": token}))
            assert (await ws.receive_json())["type"] == "auth_ok"
        yield ws
    finally:
        with contextlib.suppress(Exception):
            await ws.close()
        await session.close()


async def recv_json(ws: aiohttp.ClientWebSocketResponse, timeout: float = 5.0) -> dict:
    """Next JSON text message, stepping over interleaved JPEG frames.

    Frames and control messages share one socket, so a client that is subscribed
    cannot assume the next thing it reads is JSON -- which is exactly the
    behaviour a real client has to cope with too.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        assert remaining > 0, "no JSON message arrived in time"
        msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
        if msg.type == aiohttp.WSMsgType.BINARY:
            continue
        assert msg.type == aiohttp.WSMsgType.TEXT, f"expected JSON text, got {msg.type}"
        return json.loads(msg.data)


async def recv_frame(ws: aiohttp.ClientWebSocketResponse, timeout: float = 5.0) -> bytes:
    """Next BINARY message, skipping any interleaved JSON (e.g. a capture error)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        assert remaining > 0, "no frame arrived in time"
        msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
        if msg.type == aiohttp.WSMsgType.BINARY:
            return msg.data
        assert msg.type == aiohttp.WSMsgType.TEXT, f"unexpected {msg.type}: {msg.data!r}"


async def send_json(ws: aiohttp.ClientWebSocketResponse, payload: dict) -> dict:
    await ws.send_str(json.dumps(payload))
    return await recv_json(ws)


def jpeg_size(data: bytes) -> Tuple[int, int]:
    return Image.open(io.BytesIO(data)).size


async def subscribe(ws: aiohttp.ClientWebSocketResponse, vm: str) -> None:
    await ws.send_str(json.dumps({"type": "subscribe", "vm": vm}))
    # Give the shared capture loop a moment to attach before asserting on QMP.
    await asyncio.sleep(0.15)


# ── config ────────────────────────────────────────────────────────────────────

async def test_config_is_clamped_and_acked(harness: Harness):
    """Out-of-range values are clamped, not rejected: a GUI asking for 1000 fps
    wants a fast stream, not an error dialog."""
    async with stream(harness.base_url) as ws:
        ack = await send_json(ws, {
            "type": "config", "vm": "vm-a",
            "quality": 5000, "fps": 1000, "width": 0, "height": 0,
            "input_enabled": True,
        })
    assert ack == {
        "type": "config_ack",
        "quality": sb.MAX_QUALITY,
        "fps": sb.MAX_FPS,
        "width": 1,
        "height": 1,
    }


async def test_config_ack_echoes_valid_values_unchanged(harness: Harness):
    async with stream(harness.base_url) as ws:
        ack = await send_json(ws, {
            "type": "config", "vm": "vm-a",
            "quality": 70, "fps": 15, "width": 800, "height": 600,
        })
    assert ack == {"type": "config_ack", "quality": 70, "fps": 15, "width": 800, "height": 600}


async def test_partial_config_keeps_the_other_fields(harness: Harness):
    async with stream(harness.base_url) as ws:
        first = await send_json(ws, {"type": "config", "vm": "vm-a", "quality": 40})
        second = await send_json(ws, {"type": "config", "vm": "vm-a", "fps": 12})
    assert first["quality"] == 40 and first["fps"] == sb.DEFAULT_FPS
    assert second["quality"] == 40, "an unmentioned field must not reset"
    assert second["fps"] == 12


async def test_config_can_disable_input(harness: Harness):
    async with stream(harness.base_url) as ws:
        await send_json(ws, {"type": "config", "vm": "vm-a", "input_enabled": False})
        await subscribe(ws, "vm-a")
        harness.qmp("vm-a").clear()
        await ws.send_str(json.dumps({"type": "input", "input_type": "key",
                                      "key": "a", "pressed": True}))
        err = await recv_json(ws)
    assert err["type"] == "error" and "disabled" in err["message"]
    assert harness.qmp("vm-a").of("human-monitor-command") == []


@pytest.mark.parametrize("bad", [-1, -1920])
async def test_negative_width_is_an_error_and_keeps_the_socket_open(harness: Harness, bad: int):
    """A negative size is a type violation, not a preference to clamp."""
    async with stream(harness.base_url) as ws:
        await ws.send_str(json.dumps({"type": "config", "vm": "vm-a",
                                      "width": bad, "height": 720}))
        messages = [await recv_json(ws), await recv_json(ws)]
        assert "config_ack" in [m["type"] for m in messages], "the client is never left waiting"
        error = next(m for m in messages if m["type"] == "error")
        assert "width" in error["message"] and str(bad) in error["message"]
        ack = next(m for m in messages if m["type"] == "config_ack")
        assert ack["width"] != bad, "a rejected value must not be applied"

        # Still open: a ping gets a pong.
        assert await send_json(ws, {"type": "ping", "time": 7}) == {"type": "pong", "time": 7}
        assert not ws.closed


async def test_non_numeric_config_field_is_an_error(harness: Harness):
    async with stream(harness.base_url) as ws:
        await ws.send_str(json.dumps({"type": "config", "vm": "vm-a", "fps": "thirty"}))
        messages = [await recv_json(ws), await recv_json(ws)]
        error = next(m for m in messages if m["type"] == "error")
        assert "integer" in error["message"]


# ── robustness ────────────────────────────────────────────────────────────────

async def test_unknown_message_type_is_ignored_without_dropping_connection(harness: Harness):
    async with stream(harness.base_url) as ws:
        await ws.send_str(json.dumps({"type": "teleport", "x": 1}))
        await ws.send_str(json.dumps({"type": "something_new", "nested": {"a": 1}}))
        assert await send_json(ws, {"type": "ping", "time": 1}) == {"type": "pong", "time": 1}
        assert not ws.closed


async def test_malformed_json_errors_and_keeps_the_socket_open(harness: Harness):
    async with stream(harness.base_url) as ws:
        await ws.send_str("{not json at all")
        err = await recv_json(ws)
        assert err["type"] == "error" and "malformed JSON" in err["message"]
        assert await send_json(ws, {"type": "ping", "time": 99}) == {"type": "pong", "time": 99}
        assert not ws.closed


async def test_json_array_is_rejected_with_an_error_not_a_crash(harness: Harness):
    async with stream(harness.base_url) as ws:
        await ws.send_str("[1, 2, 3]")
        err = await recv_json(ws)
        assert err["type"] == "error" and "JSON object" in err["message"]
        assert await send_json(ws, {"type": "ping", "time": 3}) == {"type": "pong", "time": 3}


# ── authentication ────────────────────────────────────────────────────────────

async def test_unauthenticated_client_gets_no_frames_and_no_input(harness: Harness):
    """The whole point: no token, no framebuffer and no keystrokes."""
    session = aiohttp.ClientSession()
    try:
        ws = await session.ws_connect(f"{harness.base_url}/ws/stream")
        # Everything an unauthenticated client might try, before and after the
        # rejection, must achieve nothing.
        await ws.send_str(json.dumps({"type": "subscribe", "vm": "vm-a"}))
        await ws.send_str(json.dumps({"type": "input", "input_type": "key",
                                      "key": "a", "pressed": True}))
        await ws.send_str(json.dumps({"type": "input", "input_type": "mouse_click",
                                      "button": "left", "pressed": True}))
        message = await asyncio.wait_for(ws.receive(), timeout=5)
        assert message.type == aiohttp.WSMsgType.TEXT
        error = json.loads(message.data)
        assert error["type"] == "error" and "auth" in error["message"].lower()
        # ... and then it is hung up on, so nothing else ever arrives.
        with contextlib.suppress(asyncio.TimeoutError):
            closing = await asyncio.wait_for(ws.receive(), timeout=5)
            assert closing.type in (
                aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING,
            )
    finally:
        await session.close()

    # No VM was ever contacted: no screendump, no sendkey, no input-send-event.
    assert harness.clients == {}, "an unauthenticated client reached a VM"
    assert harness.bridge.clients == {}


async def test_wrong_token_is_rejected(harness: Harness):
    session = aiohttp.ClientSession()
    try:
        ws = await session.ws_connect(f"{harness.base_url}/ws/stream")
        await ws.send_str(json.dumps({"type": "auth", "key": "not-the-token"}))
        msg = await asyncio.wait_for(ws.receive(), timeout=5)
        assert json.loads(msg.data) == {"type": "error", "message": "unauthorized"}
        closing = await asyncio.wait_for(ws.receive(), timeout=5)
        assert closing.type in (
            aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING,
        )
    finally:
        await session.close()
    assert harness.clients == {}


async def test_empty_token_is_rejected(harness: Harness):
    session = aiohttp.ClientSession()
    try:
        ws = await session.ws_connect(f"{harness.base_url}/ws/stream")
        await ws.send_str(json.dumps({"type": "auth", "key": ""}))
        msg = await asyncio.wait_for(ws.receive(), timeout=5)
        assert json.loads(msg.data)["message"] == "unauthorized"
    finally:
        await session.close()


async def test_non_auth_first_message_is_refused(harness: Harness):
    session = aiohttp.ClientSession()
    try:
        ws = await session.ws_connect(f"{harness.base_url}/ws/stream")
        await ws.send_str(json.dumps({"type": "subscribe", "vm": "vm-a"}))
        msg = await asyncio.wait_for(ws.receive(), timeout=5)
        assert json.loads(msg.data)["type"] == "error"
    finally:
        await session.close()
    assert harness.clients == {}, "a subscribe before auth must not reach a VM"


async def test_token_may_arrive_as_a_query_parameter(harness: Harness):
    session = aiohttp.ClientSession()
    try:
        ws = await session.ws_connect(f"{harness.base_url}/ws/stream?token={TOKEN}")
        assert (await ws.receive_json())["type"] == "auth_ok"
        await ws.send_str(json.dumps({"type": "ping", "time": 5}))
        assert json.loads((await ws.receive()).data) == {"type": "pong", "time": 5}
    finally:
        await session.close()


async def test_api_key_registry_keys_are_accepted():
    """A key paired for the REST API also opens a stream: the same registry the
    API's own WebSocket routes consult."""

    class Registry:
        def authenticate(self, secret: str) -> Optional[dict]:
            return {"key_id": "k1"} if secret == "paired-key" else None

    auth = sb.TokenAuthenticator("bridge-token", Registry())
    assert auth.authenticate("paired-key")
    assert auth.authenticate("bridge-token")
    assert not auth.authenticate("wrong")
    assert not auth.authenticate("")
    assert not auth.authenticate(None)


def test_generated_token_is_used_when_env_is_unset():
    auth = sb.TokenAuthenticator.create(env={})
    assert auth.token
    assert auth.authenticate(auth.token)
    # An explicit environment must not let an ambient one leak in.
    assert not auth.authenticate(os.environ.get(sb.TOKEN_ENV, "definitely-wrong"))


def test_configured_env_token_wins():
    assert sb.TokenAuthenticator.create(env={sb.TOKEN_ENV: "from-env"}).token == "from-env"


# ── frames ────────────────────────────────────────────────────────────────────

async def test_authenticated_client_receives_jpeg_frames(harness: Harness):
    async with stream(harness.base_url) as ws:
        await subscribe(ws, "vm-a")
        frame = await recv_frame(ws)
    # Raw JPEG, no header and no length prefix.
    assert frame[:2] == b"\xff\xd8"
    assert frame[-2:] == b"\xff\xd9"
    assert jpeg_size(frame) == (sb.DEFAULT_WIDTH, sb.DEFAULT_HEIGHT)
    assert harness.qmp("vm-a").screendumps >= 1


async def test_frames_are_delivered_at_the_requested_size(harness: Harness):
    async with stream(harness.base_url) as ws:
        await send_json(ws, {"type": "config", "vm": "vm-a", "width": 160, "height": 120})
        await subscribe(ws, "vm-a")
        frame = await recv_frame(ws)
    assert jpeg_size(frame) == (160, 120)


async def test_one_capture_is_shared_by_two_clients_on_one_vm(harness: Harness):
    """N clients on one VM share the capture: the expensive part happens once."""
    async with stream(harness.base_url) as a, stream(harness.base_url) as b:
        await subscribe(a, "vm-a")
        await subscribe(b, "vm-a")
        await recv_frame(a)
        await recv_frame(b)
        source = harness.bridge.sources["vm-a"]
        assert source.subscriber_count == 2
        # One QMP connection for both clients, not one each.
        assert len(harness.clients) == 1
        assert harness.qmp("vm-a").screendumps >= 1


async def test_capture_stops_when_the_last_client_leaves(harness: Harness):
    async with stream(harness.base_url) as ws:
        await subscribe(ws, "vm-a")
        await recv_frame(ws)
    await asyncio.sleep(0.3)
    assert harness.bridge.sources["vm-a"].subscriber_count == 0


async def test_unknown_vm_is_reported_with_the_known_names(harness: Harness):
    async with stream(harness.base_url) as ws:
        await ws.send_str(json.dumps({"type": "subscribe", "vm": "nope"}))
        err = await recv_json(ws)
        assert err["type"] == "error"
        assert "nope" in err["message"]
        assert "vm-a" in err["message"] and "vm-b" in err["message"]
        assert await send_json(ws, {"type": "ping", "time": 1}) == {"type": "pong", "time": 1}


async def test_screendump_uses_a_unique_png_filename_and_cleans_up(harness: Harness):
    async with stream(harness.base_url) as ws:
        await subscribe(ws, "vm-a")
        await recv_frame(ws)
        await recv_frame(ws)

    calls = harness.qmp("vm-a").of("screendump")
    assert len(calls) >= 2
    assert all(call["format"] == "png" for call in calls)
    names = [call["filename"] for call in calls]
    assert len(set(names)) == len(names), "capture filenames must be unique"
    assert all(not Path(name).exists() for name in names), "temp files must be deleted"


# ── per-VM isolation ──────────────────────────────────────────────────────────

async def test_config_for_vm_a_does_not_affect_vm_b(harness: Harness):
    async with stream(harness.base_url) as a, stream(harness.base_url) as b:
        await send_json(a, {"type": "config", "vm": "vm-a",
                            "quality": 10, "width": 96, "height": 72, "fps": 5})
        await send_json(b, {"type": "config", "vm": "vm-b",
                            "quality": 95, "width": 320, "height": 240, "fps": 30})
        await subscribe(a, "vm-a")
        await subscribe(b, "vm-b")
        frame_a = await recv_frame(a)
        frame_b = await recv_frame(b)

        # Each client sees the size *it* asked for, from its own VM.
        assert jpeg_size(frame_a) == (96, 72)
        assert jpeg_size(frame_b) == (320, 240)

        # Per-session config was never written onto shared state.
        by_vm = {session.vm: session for session in harness.bridge.clients.values()}
        assert len(by_vm) == 2
        assert (by_vm["vm-a"].quality, by_vm["vm-a"].width) == (10, 96)
        assert (by_vm["vm-b"].quality, by_vm["vm-b"].width) == (95, 320)
        assert by_vm["vm-a"].source is not by_vm["vm-b"].source

    # Each VM was captured through its own QMP connection.
    assert harness.qmp("vm-a") is not harness.qmp("vm-b")
    assert harness.qmp("vm-a").screendumps >= 1
    assert harness.qmp("vm-b").screendumps >= 1


async def test_input_goes_only_to_the_vm_the_client_is_watching(harness: Harness):
    async with stream(harness.base_url) as a, stream(harness.base_url) as b:
        await subscribe(a, "vm-a")
        await subscribe(b, "vm-b")
        harness.qmp("vm-a").clear()
        harness.qmp("vm-b").clear()
        await a.send_str(json.dumps({"type": "input", "input_type": "key",
                                     "key": "A", "pressed": True}))
        await b.send_str(json.dumps({"type": "input", "input_type": "key",
                                     "key": "b", "pressed": True}))
        await asyncio.sleep(0.5)

    sent_a = [args["command-line"] for args in harness.qmp("vm-a").of("human-monitor-command")]
    sent_b = [args["command-line"] for args in harness.qmp("vm-b").of("human-monitor-command")]
    assert sent_a == ["sendkey shift-a"]
    assert sent_b == ["sendkey b"]


async def test_input_before_subscribe_is_rejected(harness: Harness):
    async with stream(harness.base_url) as ws:
        await ws.send_str(json.dumps({"type": "input", "input_type": "key",
                                      "key": "a", "pressed": True}))
        err = await recv_json(ws)
        assert err["type"] == "error" and "before a subscribe" in err["message"]
    assert harness.clients == {}


# ── QMP input command shapes ──────────────────────────────────────────────────

async def test_key_becomes_a_human_monitor_sendkey(harness: Harness):
    async with stream(harness.base_url) as ws:
        await subscribe(ws, "vm-a")
        harness.qmp("vm-a").clear()
        for key in ("a", "A", "!", " ", "ret", "Escape"):
            await ws.send_str(json.dumps({"type": "input", "input_type": "key",
                                          "key": key, "pressed": True}))
        await asyncio.sleep(1.0)

    sent = [args["command-line"] for args in harness.qmp("vm-a").of("human-monitor-command")]
    for key, resolved in (("a", "a"), ("A", "shift-a"), ("!", "shift-1"),
                          (" ", "spc"), ("ret", "ret"), ("Escape", "esc")):
        assert f"sendkey {resolved}" in sent, f"{key!r} -> {resolved}: got {sent}"


async def test_key_release_sends_nothing(harness: Harness):
    """``sendkey`` presses and releases atomically, so treating a release as a tap
    types every character twice for a client that sends press/release pairs."""
    async with stream(harness.base_url) as ws:
        await subscribe(ws, "vm-a")
        harness.qmp("vm-a").clear()
        await ws.send_str(json.dumps({"type": "input", "input_type": "key",
                                      "key": "a", "pressed": False}))
        await asyncio.sleep(0.3)
    assert harness.qmp("vm-a").of("human-monitor-command") == []


async def test_keystrokes_are_paced(harness: Harness):
    """50ms between keystrokes: guests drop input that arrives faster than they
    poll, and the symptom is a password one character short."""
    async with stream(harness.base_url) as ws:
        await subscribe(ws, "vm-a")
        client = harness.qmp("vm-a")
        original = client.send
        loop = asyncio.get_running_loop()
        times: List[float] = []

        async def timed(cmd: str, args: Optional[dict] = None) -> Dict[str, Any]:
            if cmd == "human-monitor-command":
                times.append(loop.time())
            return await original(cmd, args)

        client.send = timed  # type: ignore[method-assign]
        for char in "abc":
            await ws.send_str(json.dumps({"type": "input", "input_type": "key",
                                          "key": char, "pressed": True}))
        await asyncio.sleep(0.8)

    assert len(times) == 3, times
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(gap >= sb.DEFAULT_KEY_DELAY_SEC * 0.9 for gap in gaps), gaps


async def test_unmappable_key_is_an_error_not_a_silent_drop(harness: Harness):
    async with stream(harness.base_url) as ws:
        await subscribe(ws, "vm-a")
        await ws.send_str(json.dumps({"type": "input", "input_type": "key",
                                      "key": "é", "pressed": True}))
        err = await recv_json(ws)
    assert err["type"] == "error" and "U+00E9" in err["message"]


def test_key_resolution_covers_printable_ascii_and_rejects_the_rest():
    """The keymap is reused from vm_harness.guest_input, not reimplemented."""
    for code in range(0x20, 0x7F):
        name = sb.QMPInputInjector.resolve_key(chr(code))
        assert name and " " not in name, f"{chr(code)!r} -> {name!r}"
    assert sb.QMPInputInjector.resolve_key("ENTER") == "ret"
    assert sb.QMPInputInjector.resolve_key("f5") == "f5"
    assert sb.QMPInputInjector.resolve_key("shift-a") == "shift-a"
    for bad in ("", None, "with space", "é", 7, "!!"):
        with pytest.raises((sb.UnknownKeyError, sb.UnsupportedKeyError)):
            sb.QMPInputInjector.resolve_key(bad)


async def test_mouse_move_sends_one_abs_event_pair(harness: Harness):
    async with stream(harness.base_url) as ws:
        await send_json(ws, {"type": "config", "vm": "vm-a", "width": 800, "height": 600})
        await subscribe(ws, "vm-a")
        harness.qmp("vm-a").clear()
        await ws.send_str(json.dumps({"type": "input", "input_type": "mouse_move",
                                      "x": 400, "y": 300}))
        await asyncio.sleep(0.4)

    calls = harness.qmp("vm-a").of("input-send-event")
    assert len(calls) == 1, "both axes travel in one command, not two round trips"
    assert calls[0]["events"] == [
        {"type": "abs", "data": {"axis": "x", "value": round(sb.to_axis(400, 800))}},
        {"type": "abs", "data": {"axis": "y", "value": round(sb.to_axis(300, 600))}},
    ]


async def test_abs_axis_values_are_integers(harness: Harness):
    """QEMU rejects a float in events[].data.value: "Invalid parameter type for
    'events[0].data.value', expected: integer". Every pointer event failed
    against a real guest until this was found on a live VM, so it is asserted
    on the wire format rather than on the helper's own output.

    The previous version of this compared the emitted event against
    to_axis(), which returns the float -- so it passed while the guest silently
    ignored every mouse movement. Assert the JSON type, not agreement with the
    function that caused the bug.
    """
    async with stream(harness.base_url) as ws:
        await send_json(ws, {"type": "config", "vm": "vm-a", "width": 800, "height": 600})
        await subscribe(ws, "vm-a")
        harness.qmp("vm-a").clear()
        # Coordinates chosen so the scaled value is genuinely fractional.
        for x, y in ((1, 1), (400, 300), (799, 599), (123, 457)):
            await ws.send_str(json.dumps({"type": "input", "input_type": "mouse_move",
                                          "x": x, "y": y}))
            await asyncio.sleep(0.15)

    calls = harness.qmp("vm-a").of("input-send-event")
    assert calls, "no pointer events reached QMP"
    seen = 0
    for call in calls:
        for event in call["events"]:
            if event["type"] != "abs":
                continue
            seen += 1
            value = event["data"]["value"]
            assert isinstance(value, int), (
                f"axis value must be int for QEMU, got {type(value).__name__}: {value!r}"
            )
            assert not isinstance(value, bool)
            assert 0 <= value <= sb.TABLET_AXIS_MAX
    assert seen == 8, f"expected 8 axis events across 4 moves, saw {seen}"


def test_axis_mapping_clamps_rather_than_wrapping():
    assert sb.to_axis(0, 800) == 0.0
    assert sb.to_axis(799, 800) == sb.TABLET_AXIS_MAX
    assert sb.to_axis(-5000, 800) == 0.0, "past the left edge must clamp, not wrap"
    assert sb.to_axis(10_000_000, 800) == sb.TABLET_AXIS_MAX
    assert sb.to_axis(10, 0) == 0.0, "a zero-width surface would divide by zero"
    assert sb.to_axis(10, 1) == 0.0


@pytest.mark.parametrize("button", ["left", "right", "middle"])
async def test_mouse_click_sends_a_directional_btn_event(harness: Harness, button: str):
    async with stream(harness.base_url) as ws:
        await subscribe(ws, "vm-a")
        harness.qmp("vm-a").clear()
        for pressed in (True, False):
            await ws.send_str(json.dumps({
                "type": "input", "input_type": "mouse_click",
                "button": button, "pressed": pressed,
            }))
        await asyncio.sleep(0.5)

    events = [e for call in harness.qmp("vm-a").of("input-send-event") for e in call["events"]]
    assert events == [
        {"type": "btn", "data": {"down": True, "button": button}},
        {"type": "btn", "data": {"down": False, "button": button}},
    ]


async def test_unknown_mouse_button_is_an_error(harness: Harness):
    async with stream(harness.base_url) as ws:
        await subscribe(ws, "vm-a")
        await ws.send_str(json.dumps({"type": "input", "input_type": "mouse_click",
                                      "button": "thumb", "pressed": True}))
        err = await recv_json(ws)
    assert err["type"] == "error" and "thumb" in err["message"]


async def test_scroll_puts_the_sign_on_the_axis_name(harness: Harness):
    async with stream(harness.base_url) as ws:
        await subscribe(ws, "vm-a")
        harness.qmp("vm-a").clear()
        for dx, dy in ((0, -1), (0, 3), (-2, 0)):
            await ws.send_str(json.dumps({"type": "input", "input_type": "scroll",
                                          "dx": dx, "dy": dy}))
        await asyncio.sleep(0.6)

    events = [e for call in harness.qmp("vm-a").of("input-send-event") for e in call["events"]]
    assert events == [
        {"type": "wheel", "data": {"axis": "down", "value": 1}},
        {"type": "wheel", "data": {"axis": "up", "value": 3}},
        {"type": "wheel", "data": {"axis": "left", "value": 2}},
    ]


async def test_unsupported_input_type_is_an_error(harness: Harness):
    async with stream(harness.base_url) as ws:
        await subscribe(ws, "vm-a")
        await ws.send_str(json.dumps({"type": "input", "input_type": "teleport", "x": 5}))
        err = await recv_json(ws)
    assert err["type"] == "error" and "teleport" in err["message"]


async def test_tablet_device_is_pinned_when_configured():
    """``-device usb-tablet,id=tablet0`` needs the events addressed to it; with no
    id configured the field is omitted and QEMU picks the only pointing device."""
    registry = FakeRegistry(("vm-a",))
    bridge = sb.StreamingBridge(
        registry=registry,  # type: ignore[arg-type]
        authenticator=sb.TokenAuthenticator(TOKEN),
        client_factory=FakeQMPClient,
        host="127.0.0.1", port=0,
        env={sb.TABLET_DEVICE_ENV: "tablet0"},
    )
    try:
        source = bridge.source_for(registry.get("vm-a"))  # type: ignore[arg-type]
        assert source.injector.tablet_device == "tablet0"
        client = FakeQMPClient(source.target.qmp_uri)
        source.injector._get_client = lambda: _ready(client)  # type: ignore[assignment]
        await source.injector.send_input_events(
            [{"type": "btn", "data": {"down": True, "button": "left"}}]
        )
        assert client.of("input-send-event") == [{
            "events": [{"type": "btn", "data": {"down": True, "button": "left"}}],
            "device": "tablet0",
        }]
    finally:
        await bridge.stop()


async def test_tablet_device_is_omitted_when_not_configured():
    registry = FakeRegistry(("vm-a",))
    bridge = sb.StreamingBridge(
        registry=registry,  # type: ignore[arg-type]
        authenticator=sb.TokenAuthenticator(TOKEN),
        client_factory=FakeQMPClient,
        host="127.0.0.1", port=0, env={},
    )
    try:
        source = bridge.source_for(registry.get("vm-a"))  # type: ignore[arg-type]
        assert source.injector.tablet_device is None
        client = FakeQMPClient(source.target.qmp_uri)
        source.injector._get_client = lambda: _ready(client)  # type: ignore[assignment]
        await source.injector.scroll(0, 1)
        assert client.of("input-send-event") == [{
            "events": [{"type": "wheel", "data": {"axis": "up", "value": 1}}],
        }]
    finally:
        await bridge.stop()


async def _ready(client: FakeQMPClient) -> FakeQMPClient:
    return client


# ── stats, vm_list, health ────────────────────────────────────────────────────

async def test_stats_report_frames_bytes_and_fps(harness: Harness):
    async with stream(harness.base_url) as ws:
        await subscribe(ws, "vm-a")
        await recv_frame(ws)
        stats = await send_json(ws, {"type": "stats_request"})
    assert set(stats) == {"type", "frames_sent", "bytes_sent", "fps"}
    assert stats["frames_sent"] >= 1
    assert stats["bytes_sent"] > 0
    assert stats["fps"] >= 0.0


async def test_vm_list_is_reported(harness: Harness):
    async with stream(harness.base_url) as ws:
        assert await send_json(ws, {"type": "vm_list"}) == {
            "type": "vm_list", "vms": ["vm-a", "vm-b"],
        }


async def test_health_and_vms_endpoints(harness: Harness):
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{harness.base_url}/health") as response:
            health = await response.json()
        assert health["status"] == "ok"
        assert health["vms"] == ["vm-a", "vm-b"]
        async with http.get(f"{harness.base_url}/vms") as response:
            listing = await response.json()
    assert [vm["name"] for vm in listing["vms"]] == ["vm-a", "vm-b"]
    assert listing["vms"][0]["qmp_uri"].startswith("tcp:127.0.0.1:")


# ── VM registry ───────────────────────────────────────────────────────────────

def test_registry_enumerates_real_vm_configs(tmp_path: Path):
    (tmp_path / "alpha.json").write_text(json.dumps({
        "name": "alpha", "management_port": 4444, "management_password": "s3cret",
    }), encoding="utf-8")
    (tmp_path / "beta.json").write_text(json.dumps({
        "name": "beta", "management_port": 4455,
    }), encoding="utf-8")
    (tmp_path / "gamma.json").write_text(json.dumps({"name": "gamma"}), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")

    registry = sb.VMRegistry(vms_dir=tmp_path)

    assert registry.list_vms() == ["alpha", "beta"]
    alpha = registry.get("alpha")
    assert alpha is not None
    assert alpha.qmp_uri == "tcp:127.0.0.1:4444"
    assert alpha.password == "s3cret"
    beta = registry.get("beta")
    assert beta is not None and beta.qmp_uri == "tcp:127.0.0.1:4455"
    # A VM with no monitor cannot be streamed; a corrupt file is skipped.
    assert registry.get("gamma") is None
    assert registry.get("broken") is None


def test_registry_prefers_a_unix_socket_endpoint(tmp_path: Path):
    (tmp_path / "sock.json").write_text(json.dumps({
        "name": "sock", "management_port": 4444, "qmp_socket": "/tmp/qmp.sock",
    }), encoding="utf-8")
    target = sb.VMRegistry(vms_dir=tmp_path).get("sock")
    assert target is not None and target.qmp_uri == "unix:/tmp/qmp.sock"


def test_registry_name_lookup_is_not_a_path_traversal_vector(tmp_path: Path):
    (tmp_path / "real.json").write_text(
        json.dumps({"name": "real", "management_port": 4444}), encoding="utf-8")
    sibling = tmp_path.parent / "secret.json"
    sibling.write_text(json.dumps({"name": "secret", "management_port": 9999}), encoding="utf-8")
    try:
        registry = sb.VMRegistry(vms_dir=tmp_path)
        assert registry.get("../../secret") is None
        assert registry.get("secret") is None
        assert registry.get("real") is not None
    finally:
        sibling.unlink()


def test_registry_on_a_missing_directory_is_empty(tmp_path: Path):
    assert sb.VMRegistry(vms_dir=tmp_path / "nope").list_vms() == []


def test_container_names_that_could_smuggle_arguments_are_rejected():
    assert sb._CONTAINER_NAME_RE.match("web-01")
    for bad in ("../etc", "a;rm -rf /", "-x", "a b", "", "a" * 200):
        assert not sb._CONTAINER_NAME_RE.match(bad), bad


# ── container terminal ────────────────────────────────────────────────────────

async def test_terminal_requires_the_token(harness: Harness):
    session = aiohttp.ClientSession()
    try:
        ws = await session.ws_connect(f"{harness.base_url}/terminal/my-container")
        await ws.send_str(json.dumps({"command": "ls"}))
        msg = await asyncio.wait_for(ws.receive(), timeout=5)
        assert json.loads(msg.data)["type"] == "error"
        # The socket is then closed, so the command is never run.
        closing = await asyncio.wait_for(ws.receive(), timeout=5)
        assert closing.type in (
            aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING,
        )
    finally:
        await session.close()
    assert harness.exec_sessions.commands == []


async def test_terminal_runs_commands_through_the_session_manager(harness: Harness):
    async with stream(harness.base_url, path="/terminal/x") as ws:
        await ws.send_str(json.dumps({"command": "ls -la"}))
        assert await recv_json(ws) == {"output": "hello\n"}
    assert harness.exec_sessions.commands == [("x", "ls -la")]


async def test_terminal_rejects_a_bad_container_name(harness: Harness):
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{harness.base_url}/terminal/..%2Fetc") as response:
            assert response.status in (400, 404)
    assert harness.exec_sessions.commands == []


# ── persistence of the exec session ───────────────────────────────────────────

def test_docker_exec_suppresses_console_windows(monkeypatch: pytest.MonkeyPatch):
    """``CREATE_NO_WINDOW`` has to actually reach Popen, or every keystroke of an
    interactive session flashes a console window on Windows."""
    captured: Dict[str, Any] = {}

    class FakePopen:
        def __init__(self, cmd: list, **kwargs: Any) -> None:
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs

    monkeypatch.setattr(sb.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(sb.os, "name", "nt")
    session = sb.DockerExecSession("my-container")
    session._spawn()
    assert captured["kwargs"]["creationflags"] == sb.CREATE_NO_WINDOW
    assert captured["cmd"][:2] == ["docker", "exec"]
    assert "-i" in captured["cmd"], "an interactive shell needs stdin kept open"
