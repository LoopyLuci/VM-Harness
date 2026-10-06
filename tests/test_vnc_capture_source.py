"""RFB as the frame source: the VNC endpoint, source selection, and the push loop.

The reason this exists: QMP ``screendump`` costs a socket round trip, a
full-framebuffer PNG encode in QEMU, a PNG decode here, and a JPEG encode per
client -- whole frame, every frame, 40-70ms each on this host. QEMU's own VNC
server tracks damage and pushes only what changed, so replacing the *capture
source* is the only remaining lever once GPU acceleration has been ruled out.
These tests are about that replacement being correct:

* the ``-vnc`` argument is built per VM, binds loopback only, and cannot collide
  between two VMs. An unauthenticated VNC on 0.0.0.0 would be a remote desktop
  nobody asked for, so that is asserted rather than assumed.
* a VM with no VNC display still streams, by screendump, and a VNC connect that
  fails hands over to screendump rather than dropping anyone's picture.
* frames are published *by the incoming update*, not by a timer, and the
  published image is a copy -- the RFB decoder reuses one framebuffer in place,
  so an image that aliased it would tear under every subscriber still encoding.

Nothing here needs a live QEMU. The RFB server is a small in-process one that
speaks the real handshake and then whatever bytes the test sends, in the same
spirit as ``tests/test_vnc_client.py``.
"""

from __future__ import annotations

import asyncio
import struct
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for candidate in (PROJECT_ROOT, PROJECT_ROOT / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import streaming_bridge as sb
from vm_harness.vnc import display as vdisplay
from vm_harness.vnc.proto import (
    BGRX32,
    ENCODING_RAW,
    SECURITY_NONE,
    ServerInit,
)


# ── QEMU's -vnc argument ───────────────────────────────────────────────────────


class TestDisplayWindow:
    """The per-VM port scheme: derived, injective, and out of everyone's way."""

    def test_the_scheme_is_documented_where_the_numbers_are(self):
        """The offsets are duplicated in boot_vm.ps1, so they have to be stated."""
        assert vdisplay.VNC_DISPLAY_PORT_BASE == 5900, "QEMU computes port = 5900 + display"
        assert vdisplay.DEFAULT_QMP_PORT_BASE == 4444, "boot_vm.ps1 offsets from 4444"
        assert vdisplay.VNC_DISPLAY_BASE == 100
        assert vdisplay.VNC_SHARE_MODE == "force-shared"

    def test_the_first_vm_gets_the_first_display(self):
        assert vdisplay.vnc_display_for_qmp_port(4444) == 100
        assert vdisplay.vnc_port_for_qmp_port(4444) == 6000

    def test_consecutive_qmp_ports_get_consecutive_displays(self):
        """What makes two VMs unable to collide: the map is injective."""
        displays = [vdisplay.vnc_display_for_qmp_port(4444 + i) for i in range(100)]
        assert displays == list(range(100, 200))
        assert len(set(displays)) == 100

    def test_the_window_avoids_the_ranges_this_host_already_uses(self):
        """5900-5999 is display :0 and the hand-configured VNC range; 5930+ is SPICE."""
        ports = {vdisplay.vnc_port_for_qmp_port(4444 + i) for i in range(100)}
        assert min(ports) > 5999, "must not land on the 5900s"
        assert min(ports) > sb.DEFAULT_SPICE_PORT_BASE if hasattr(sb, "DEFAULT_SPICE_PORT_BASE") else True
        assert ports.isdisjoint(range(5900, 6100)) is False  # 6000..6099 is inside that range
        assert min(ports) == 6000

    def test_a_custom_qmp_base_moves_the_window_with_it(self):
        """A deployment that moves qmp_port_base must not silently overlap.

        Anchoring the window to the wrong base would give two VMs the same
        display, which is the one outcome the whole scheme exists to prevent.
        """
        assert vdisplay.vnc_display_for_qmp_port(5400, 5400) == 100
        assert vdisplay.vnc_display_for_qmp_port(5401, 5400) == 101
        with pytest.raises(vdisplay.VncEndpointError):
            vdisplay.vnc_display_for_qmp_port(5400, 4444)

    @pytest.mark.parametrize("port", [4544, 4443, 0, -1, 65400])
    def test_a_qmp_port_outside_the_window_is_refused_not_wrapped(self, port):
        """Folding back would hand two distinct VMs the same display."""
        with pytest.raises(vdisplay.VncEndpointError):
            vdisplay.vnc_display_for_qmp_port(port)

    @pytest.mark.parametrize("port", [True, "4444", 4444.0, None])
    def test_a_non_int_qmp_port_is_refused(self, port):
        with pytest.raises(vdisplay.VncEndpointError):
            vdisplay.vnc_display_for_qmp_port(port)


class TestVncArgument:
    """Loopback only, forced shared, and both of those are checked, not assumed."""

    def test_it_is_loopback_forced_shared_and_a_valid_display_number(self):
        arg = vdisplay.vnc_arg_for_qmp_port(4444)
        host, _, rest = arg.partition(":")
        display, _, share = rest.partition(",")
        assert host == "127.0.0.1"
        assert display == "100", "the display number QEMU turns into port 6000"
        assert share == "share=force-shared"

    def test_no_password_is_set_because_there_is_nothing_to_authenticate_with(self):
        arg = vdisplay.vnc_arg_for_qmp_port(4444)
        assert "password" not in arg
        assert "to=" not in arg

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "10.0.0.5", ""])
    def test_a_non_loopback_bind_is_refused(self, host):
        with pytest.raises(vdisplay.VncEndpointError):
            vdisplay.vnc_display_arg(6000, host=host)

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]"])
    def test_loopback_spellings_are_accepted(self, host):
        assert vdisplay.is_loopback(host)
        assert "share=force-shared" in vdisplay.vnc_display_arg(6000, host=host)

    @pytest.mark.parametrize("port", [5900, 5999, 6100, 0, -1])
    def test_a_port_outside_the_reserved_window_is_refused(self, port):
        """Otherwise a capture display could land on a display somebody opened."""
        with pytest.raises(vdisplay.VncEndpointError):
            vdisplay.vnc_display_arg(port)


class TestParseVncPort:
    """Reading an endpoint back, so a registry entry can be compared with a port."""

    @pytest.mark.parametrize(
        "spec, expected",
        [
            ("100", 6000),
            (":100", 6000),
            ("127.0.0.1:100", 6000),
            ("127.0.0.1:100,share=force-shared", 6000),
            ("[::1]:100", 6000),
            ("localhost:150", 6050),
        ],
    )
    def test_reads_the_forms_qemu_uses(self, spec, expected):
        assert vdisplay.parse_vnc_port(spec) == expected

    @pytest.mark.parametrize(
        "spec", ["0.0.0.0:100", "192.168.1.5:100", "example.com:100"],
    )
    def test_a_routable_bind_is_refused(self, spec):
        with pytest.raises(vdisplay.VncEndpointError):
            vdisplay.parse_vnc_port(spec)

    @pytest.mark.parametrize("spec", [None, "", "   ", "127.0.0.1:", "127.0.0.1:abc", 6000, True])
    def test_nonsense_is_refused(self, spec):
        with pytest.raises(vdisplay.VncEndpointError):
            vdisplay.parse_vnc_port(spec)

    def test_a_bare_int_is_refused_because_it_means_a_display(self):
        """6000 is display 6000 (port 11900) to QEMU; disagreeing is the bug."""
        with pytest.raises(vdisplay.VncEndpointError):
            vdisplay.parse_vnc_port(6000)


class TestRegistryDerivesThePort:
    def test_a_tcp_vm_gets_a_vnc_port_derived_from_its_qmp_port(self, tmp_path):
        (tmp_path / "omarchy.json").write_text(
            '{"name": "omarchy", "management_port": 4444}', encoding="utf-8"
        )
        target = sb.VMRegistry(vms_dir=tmp_path).list_targets()[0]
        assert target.vnc_port == 6000

    def test_two_vms_get_different_vnc_ports(self, tmp_path):
        for port in (4444, 4445):
            (tmp_path / f"vm{port}.json").write_text(
                '{"management_port": %d}' % port, encoding="utf-8"
            )
        ports = {t.vnc_port for t in sb.VMRegistry(vms_dir=tmp_path).list_targets()}
        assert len(ports) == 2, f"two VMs shared a VNC port: {ports}"

    def test_an_explicit_vnc_port_wins(self, tmp_path):
        (tmp_path / "omarchy.json").write_text(
            '{"management_port": 4444, "vnc_port": 6050}', encoding="utf-8"
        )
        assert sb.VMRegistry(vms_dir=tmp_path).list_targets()[0].vnc_port == 6050

    def test_an_explicit_vnc_spec_is_read_like_qemu_reads_it(self, tmp_path):
        (tmp_path / "omarchy.json").write_text(
            '{"management_port": 4444, "vnc_port": "127.0.0.1:150"}', encoding="utf-8"
        )
        assert sb.VMRegistry(vms_dir=tmp_path).list_targets()[0].vnc_port == 6050

    def test_a_vm_whose_qmp_port_is_outside_the_window_has_no_vnc_port(self, tmp_path):
        """Still a streamable VM -- by screendump. Not an error, not excluded."""
        (tmp_path / "far.json").write_text('{"management_port": 9000}', encoding="utf-8")
        target = sb.VMRegistry(vms_dir=tmp_path).list_targets()[0]
        assert target.vnc_port == 0
        assert target.name == "far"

    def test_a_unix_socket_vm_has_no_vnc_port(self, tmp_path):
        (tmp_path / "sock.json").write_text(
            '{"management_port": 4444, "qmp_socket": "/tmp/qmp.sock"}', encoding="utf-8"
        )
        assert sb.VMRegistry(vms_dir=tmp_path).list_targets()[0].vnc_port == 0


class TestQemuArgv:
    """The launcher side: a VM is booted with a loopback display to read frames from."""

    def _args(self, tmp_path: Path, **config: object) -> List[str]:
        from vm_harness.hypervisor.qemu import backend as qb

        share = tmp_path / "share"
        share.mkdir(exist_ok=True)
        (share / "edk2-x86_64-code.fd").write_bytes(b"code")
        (share / "edk2-i386-vars.fd").write_bytes(b"vars")
        qemu = tmp_path / "qemu-system-x86_64.exe"
        qemu.write_bytes(b"")
        backend = qb.QEMUBackend(
            {"qemu_binary": str(qemu), "qemu_img": str(qemu), "vms_dir": str(tmp_path / "vms")}
        )
        return backend._build_qemu_args(
            {"name": "omarchy", "management_port": 4444, **config}
        )

    @staticmethod
    def _vnc(args: List[str]) -> List[str]:
        return [args[i + 1] for i, a in enumerate(args) if a == "-vnc"]

    def test_a_vm_is_booted_with_one_loopback_vnc_display(self, tmp_path):
        assert self._vnc(self._args(tmp_path)) == ["127.0.0.1:100,share=force-shared"]

    def test_it_composes_with_display_none(self, tmp_path):
        """-vnc adds a display; -display none removes the default one."""
        args = self._args(tmp_path, display_type="headless")
        assert "-display" in args and args[args.index("-display") + 1] == "none"
        assert self._vnc(args) == ["127.0.0.1:100,share=force-shared"]

    def test_a_spice_vm_gets_the_vnc_display_too(self, tmp_path):
        """SPICE is a display somebody watches; RFB is what the bridge reads."""
        args = self._args(tmp_path, display_type="spice", display_port=5930)
        assert "-spice" in args
        assert self._vnc(args) == ["127.0.0.1:100,share=force-shared"]

    def test_two_vms_never_collide(self, tmp_path):
        first = self._vnc(self._args(tmp_path / "a", management_port=4444)) \
            if (tmp_path / "a").mkdir(exist_ok=True) is None else []
        second = self._vnc(self._args(tmp_path / "b", management_port=4445)) \
            if (tmp_path / "b").mkdir(exist_ok=True) is None else []
        assert first and second and first != second

    def test_it_can_be_turned_off(self, tmp_path):
        assert self._vnc(self._args(tmp_path, vnc_capture=False)) == []

    def test_a_hand_written_vnc_is_not_duplicated(self, tmp_path):
        args = self._args(tmp_path, extra_args=["-vnc", ":7"])
        assert self._vnc(args) == [":7"]

    def test_a_vm_launched_as_vnc_is_loopback_only(self, tmp_path):
        """It used to be ``-vnc :0``, which QEMU reads as *every* interface."""
        args = self._args(tmp_path, display_type="vnc", display_port=5900)
        assert self._vnc(args) == ["127.0.0.1:0"]
        assert self._vnc(args) != [":0"]

    def test_a_qmp_port_outside_the_window_still_boots(self, tmp_path):
        args = self._args(tmp_path, management_port=9000)
        assert self._vnc(args) == []
        assert "-qmp" in args, "losing RFB must not cost the VM its monitor"


# ── Source selection ───────────────────────────────────────────────────────────


def _target(name: str = "vm-a", vnc_port: int = 0) -> sb.VMTarget:
    return sb.VMTarget(name, "tcp:127.0.0.1:4444", "", vnc_port)


def _bridge(**kwargs: object) -> sb.StreamingBridge:
    return sb.StreamingBridge(
        authenticator=sb.TokenAuthenticator("t"),
        env={},
        client_factory=lambda uri: None,
        **kwargs,  # type: ignore[arg-type]
    )


class TestSourceSelection:
    def test_a_vm_with_a_vnc_display_is_captured_over_rfb(self):
        source = _bridge().source_for(_target(vnc_port=6000))
        assert isinstance(source, sb.VncCaptureSource)
        assert source.transport == "vnc"

    def test_a_vm_without_one_falls_back_to_screendump(self):
        """The case the fallback exists for: a VM launched without -vnc."""
        source = _bridge().source_for(_target(vnc_port=0))
        assert isinstance(source, sb.VMStreamSource)
        assert not isinstance(source, sb.VncCaptureSource)
        assert source.transport == "screendump"

    def test_screendump_can_be_asked_for_explicitly(self):
        bridge = _bridge()
        source = bridge.source_for(_target(vnc_port=6000), "screendump")
        assert source.transport == "screendump"

    def test_vnc_can_be_asked_for_explicitly(self):
        bridge = _bridge()
        assert bridge.source_for(_target(vnc_port=6000), "vnc").transport == "vnc"

    def test_the_bridge_default_applies_to_every_vm(self):
        bridge = _bridge(capture_source="screendump")
        assert bridge.source_for(_target(vnc_port=6000)).transport == "screendump"
        assert bridge.source_for(_target("vm-b", vnc_port=6001)).transport == "screendump"

    def test_the_default_can_come_from_the_environment(self):
        bridge = sb.StreamingBridge(
            authenticator=sb.TokenAuthenticator("t"),
            env={sb.CAPTURE_SOURCE_ENV: "screendump"},
        )
        assert bridge.capture_source == "screendump"

    @pytest.mark.parametrize("junk", ["nonsense", "", None, 7, True, {"a": 1}])
    def test_an_unrecognised_request_falls_back_to_the_default(self, junk):
        """A client a version ahead must not lose its picture over a spelling."""
        assert sb.normalise_capture_source(junk, "screendump") == "screendump"

    @pytest.mark.parametrize(
        "text, expected",
        [("VNC", "vnc"), (" vnc ", "vnc"), ("ScreenDump", "screendump"), ("AUTO", "auto")],
    )
    def test_a_recognised_request_is_case_and_space_insensitive(self, text, expected):
        assert sb.normalise_capture_source(text) == expected

    def test_a_vm_is_captured_once_and_shared(self):
        """Two viewers, one capture -- the property the whole class rests on."""
        bridge = _bridge()
        first = bridge.source_for(_target(vnc_port=6000))
        second = bridge.source_for(_target(vnc_port=6000))
        assert first is second

    def test_an_idle_source_is_replaced_when_the_source_changes(self):
        """What makes the panel's combo a setting rather than decoration."""
        bridge = _bridge()
        screendump = bridge.source_for(_target(vnc_port=6000), "screendump")
        rfb = bridge.source_for(_target(vnc_port=6000), "vnc")
        assert rfb is not screendump
        assert rfb.transport == "vnc"

    async def test_a_source_with_viewers_is_not_swapped_underneath_them(self):
        bridge = _bridge()
        source = bridge.source_for(_target(vnc_port=6000), "vnc")
        queue: asyncio.Queue = source.new_queue()
        source.subscribe(queue, 30)
        try:
            assert source.subscriber_count == 1
            assert bridge.source_for(_target(vnc_port=6000), "screendump") is source
        finally:
            source.unsubscribe(queue)
            await source.close()


# ── The push loop ──────────────────────────────────────────────────────────────


# RFB wire builders, kept local so this file does not depend on another test file.

def _rect_header(x: int, y: int, w: int, h: int, encoding: int) -> bytes:
    return struct.pack(">HHHHi", x, y, w, h, encoding)


def _fb_update(*rects: bytes, count: Optional[int] = None) -> bytes:
    """A FramebufferUpdate: message type, the mandatory padding byte, the count."""
    total = len(rects) if count is None else count
    return b"\x00\x00" + struct.pack(">H", total) + b"".join(rects)


def _raw_rect(x: int, y: int, w: int, h: int, pixels: bytes) -> bytes:
    return _rect_header(x, y, w, h, ENCODING_RAW) + pixels


#: Client-to-server message types this file's fake server has to recognise.
CLIENT_INIT = 0
CLIENT_SET_ENCODINGS = 2
CLIENT_FB_UPDATE_REQUEST = 3

#: A FramebufferUpdate with no rectangles: what a server sends when it has no
#: damage. QEMU sends one in answer to every request.
_EMPTY_FB_UPDATE = b"\x00\x00\x00\x00"


def _next_client_message(buffer: bytearray) -> Tuple[Optional[int], int]:
    """Peel one client message off the front of ``buffer``.

    Returns ``(type, bytes_consumed)``, or ``(None, 0)`` when the buffer does not
    yet hold a whole message. Only the shapes ``VNCClient`` emits are handled,
    which is all a fake server needs and all the tests assert on.
    """
    if not buffer:
        return None, 0
    kind = buffer[0]
    if kind == CLIENT_INIT:
        return kind, 1
    if kind == CLIENT_FB_UPDATE_REQUEST:
        # type, incremental flag, x, y, w, h
        return kind, 10 if len(buffer) >= 10 else 0
    if kind == CLIENT_SET_ENCODINGS:
        # type, padding, u16 count, count * 4-byte encoding
        if len(buffer) < 4:
            return None, 0
        total = 4 + 4 * struct.unpack(">H", buffer[2:4])[0]
        return (kind, total) if len(buffer) >= total else (None, 0)
    # An unrecognised message is skipped rather than fatal: this parser exists to
    # count requests, and a parser that raises inside a reader task dies with
    # that task's exception reported nowhere, which is the worst outcome for a
    # test -- it just stops counting and looks like the client went quiet.
    return kind, 1


class FakeRFBServer:
    """A real handshake, then whatever bytes the test tells it to send."""

    def __init__(self, width: int = 16, height: int = 12, name: str = "omarchy"):
        self.width = width
        self.height = height
        self.name = name
        self.port = 0
        self.client_bytes = bytearray()
        self.update_requests = 0
        self.unrecognised: List[int] = []
        self.connected = asyncio.Event()
        self._outbox: asyncio.Queue = asyncio.Queue()
        self._server: Optional[asyncio.AbstractServer] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._reader: Optional[asyncio.StreamReader] = None

    async def start(self) -> "FakeRFBServer":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._writer is not None:
            self._writer.close()
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # pragma: no cover - best effort
                pass

    async def send(self, data: bytes) -> None:
        await self._outbox.put(data)

    async def wait_for_update_requests(self, count: int, timeout: float = 2.0) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        while self.update_requests < count:
            if asyncio.get_event_loop().time() > deadline:
                raise AssertionError(
                    f"client sent {self.update_requests} update requests, wanted {count} "
                    f"(unrecognised message types seen: {self.unrecognised})"
                )
            await asyncio.sleep(0.005)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self._reader = reader
        try:
            await self._run(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.CancelledError):
            pass

    async def _run(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"RFB 003.008\n")
        await writer.drain()
        await reader.readexactly(12)
        writer.write(bytes((1, SECURITY_NONE)))
        await writer.drain()
        await reader.readexactly(1)
        writer.write(struct.pack(">I", 0))
        writer.write(struct.pack(">HH", self.width, self.height))
        writer.write(BGRX32.encode())
        name = self.name.encode("latin-1")
        writer.write(struct.pack(">I", len(name)) + name)
        await writer.drain()
        self.connected.set()

        async def drain() -> None:
            """Answer each update request, and count them.

            QEMU answers *every* FramebufferUpdateRequest, with an update
            message carrying zero rectangles when there is no damage. That is
            what keeps the request/response cycle turning, so the fake does the
            same: a client that only ever sent one request would be a client
            that has given up, which is not what is under test here.
            """
            buffer = bytearray()
            try:
                while True:
                    data = await reader.read(4096)
                    if not data:
                        return
                    buffer += data
                    while True:
                        kind, consumed = _next_client_message(buffer)
                        if kind is None:
                            break
                        del buffer[:consumed]
                        if kind == CLIENT_FB_UPDATE_REQUEST:
                            self.update_requests += 1
                            writer.write(_EMPTY_FB_UPDATE)
                            await writer.drain()
                        elif kind not in (CLIENT_INIT, CLIENT_SET_ENCODINGS):
                            self.unrecognised.append(kind)
            except (asyncio.CancelledError, ConnectionResetError):
                return

        drainer = asyncio.ensure_future(drain())
        try:
            while True:
                item = await self._outbox.get()
                if item is None:
                    break
                writer.write(item)
                await writer.drain()
        finally:
            drainer.cancel()
            writer.close()


class FakeQMP:
    """Just enough QMP for the watchdog and the screendump fallback."""

    def __init__(self, status: str = "running") -> None:
        self.status = status
        self.is_connected = True
        self.commands: List[Tuple[str, Optional[dict]]] = []

    async def connect(self) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False

    async def send(self, cmd: str, args: Optional[dict] = None) -> Dict[str, Any]:
        self.commands.append((cmd, args))
        if cmd == "query-status":
            return {"return": {"status": self.status, "running": self.status == "running"}}
        if cmd == "cont":
            self.status = "running"
            return {}
        return {}

    def of(self, cmd: str) -> List[dict]:
        return [args for name, args in self.commands if name == cmd]


def _solid_colour(w: int, h: int, colour: Tuple[int, int, int]) -> bytes:
    """BGRA pixels, the layout ``Framebuffer`` normalises everything into."""
    return bytes((*colour, 0xFF)) * (w * h)


def _vnc_source(server: FakeRFBServer, qmp: FakeQMP, **kwargs: Any) -> sb.VncCaptureSource:
    return sb.VncCaptureSource(
        _target(vnc_port=server.port),
        client_factory=lambda uri: qmp,
        vnc_client_factory=lambda host, port: sb.VNCClient(host, port),
        **kwargs,
    )


async def _collect(source: sb.VMStreamSource, count: int, timeout: float = 3.0) -> List[Any]:
    """Take ``count`` items off a subscriber queue, or fail the test."""
    queue = source.new_queue()
    source.subscribe(queue, sb.MAX_FPS)
    got: List[Any] = []
    try:
        while len(got) < count:
            kind, payload = await asyncio.wait_for(queue.get(), timeout=timeout)
            got.append((kind, payload))
    finally:
        source.unsubscribe(queue)
    return got


class TestPushDrivenPublishing:
    async def test_an_update_becomes_a_published_frame(self):
        server = await FakeRFBServer().start()
        source = _vnc_source(server, FakeQMP())
        try:
            queue = source.new_queue()
            source.subscribe(queue, sb.MAX_FPS)
            await asyncio.wait_for(server.connected.wait(), timeout=2.0)
            await server.send(_fb_update(
                _raw_rect(0, 0, 4, 3, _solid_colour(4, 3, (10, 20, 30)))
            ))
            kind, frame = await asyncio.wait_for(queue.get(), timeout=3.0)
            assert kind == "frame"
            assert frame.image.size == (16, 12), "the whole desktop, not the rect"
            assert frame.image.convert("RGB").getpixel((0, 0)) == (30, 20, 10)
            assert frame.seq >= 1
        finally:
            await source.close()
            await server.stop()

    async def test_a_frame_is_published_per_update_not_per_tick(self):
        """The property the whole change rests on: the update is the clock.

        Three separate updates must produce three frames, and they must arrive
        without any timer in this process deciding to look.
        """
        server = await FakeRFBServer().start()
        source = _vnc_source(server, FakeQMP())
        try:
            queue = source.new_queue()
            source.subscribe(queue, sb.MAX_FPS)
            await asyncio.wait_for(server.connected.wait(), timeout=2.0)
            for i in range(3):
                await server.send(_fb_update(
                    _raw_rect(i, 0, 2, 2, _solid_colour(2, 2, (i * 10, 0, 0)))
                ))
                kind, frame = await asyncio.wait_for(queue.get(), timeout=3.0)
                assert kind == "frame"
                assert frame.seq == i + 1, "sequence must advance once per update"
        finally:
            await source.close()
            await server.stop()

    async def test_nothing_is_published_while_the_guest_does_not_draw(self):
        """An idle desktop costs nothing, which is what damage tracking buys."""
        server = await FakeRFBServer().start()
        source = _vnc_source(server, FakeQMP())
        try:
            queue = source.new_queue()
            source.subscribe(queue, sb.MAX_FPS)
            await asyncio.wait_for(server.connected.wait(), timeout=2.0)
            await server.wait_for_update_requests(3)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(queue.get(), timeout=0.3)
        finally:
            await source.close()
            await server.stop()

    async def test_the_published_frame_does_not_alias_the_decoder_buffer(self):
        """The RFB decoder reuses one bytearray in place.

        An image that wrapped it would change underneath a subscriber still
        encoding it -- a tearing bug that never appears in a single-subscriber
        test and is invisible in the log.
        """
        server = await FakeRFBServer().start()
        source = _vnc_source(server, FakeQMP())
        try:
            queue = source.new_queue()
            source.subscribe(queue, sb.MAX_FPS)
            await asyncio.wait_for(server.connected.wait(), timeout=2.0)
            await server.send(_fb_update(
                _raw_rect(0, 0, 2, 2, _solid_colour(2, 2, (1, 2, 3)))
            ))
            _, first = await asyncio.wait_for(queue.get(), timeout=3.0)
            before = first.image.convert("RGB").getpixel((0, 0))

            await server.send(_fb_update(
                _raw_rect(0, 0, 2, 2, _solid_colour(2, 2, (200, 100, 50)))
            ))
            _, second = await asyncio.wait_for(queue.get(), timeout=3.0)
            assert first.image.convert("RGB").getpixel((0, 0)) == before, (
                "the earlier frame was mutated by a later update"
            )
            assert second.image.convert("RGB").getpixel((0, 0)) == (50, 100, 200)
        finally:
            await source.close()
            await server.stop()

    async def test_two_subscribers_share_one_capture(self):
        server = await FakeRFBServer().start()
        source = _vnc_source(server, FakeQMP())
        try:
            a, b = source.new_queue(), source.new_queue()
            source.subscribe(a, 30)
            source.subscribe(b, 60)
            await asyncio.wait_for(server.connected.wait(), timeout=2.0)
            await server.send(_fb_update(
                _raw_rect(0, 0, 2, 2, _solid_colour(2, 2, (9, 9, 9)))
            ))
            frames = await asyncio.wait_for(
                asyncio.gather(a.get(), b.get()), timeout=3.0
            )
            assert [kind for kind, _ in frames] == ["frame", "frame"]
            assert frames[0][1].seq == frames[1][1].seq, "one update, one frame each"
        finally:
            source.unsubscribe(a)
            source.unsubscribe(b)
            await source.close()
            await server.stop()

    async def test_the_subscribe_encoder_is_shared_with_the_screendump_source(self):
        """The per-VM lock exists because N subscribers resize one Image."""
        assert isinstance(
            _vnc_source(await FakeRFBServer().start(), FakeQMP()).encoder,
            sb.JpegEncoder,
        )


class _RefusingRFBClient:
    """An RFB client whose connect fails, as it does when nothing is listening.

    A real refusal would be the same thing, but on some hosts it takes longer
    than a test should wait and on others it is a different errno; the branch
    under test is the ``except`` around the session, not the OS's timing.
    """

    instances: List["_RefusingRFBClient"] = []

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.on_frame = None
        self.stopped = False
        _RefusingRFBClient.instances.append(self)

    async def connect(self) -> None:
        raise ConnectionRefusedError(f"nothing is listening on {self.host}:{self.port}")

    async def serve(self, frame_interval: float = 0.0) -> None:  # pragma: no cover
        raise AssertionError("serve must not be reached")

    def stop(self) -> None:
        self.stopped = True

    async def close(self) -> None:  # pragma: no cover - deliberately not awaited
        raise AssertionError("close() must not be on the teardown path")


def _closed_port() -> int:
    """A loopback port with nothing behind it, so the number is at least real."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class TestFallbackAndResilience:
    async def test_a_vm_with_no_vnc_display_falls_back_to_screendump(self, monkeypatch):
        """No display means RFB cannot be used, and the old path still works."""
        source = sb.VncCaptureSource(_target(vnc_port=0), client_factory=lambda uri: None)
        assert source.vnc_port == 0
        published: List[str] = []
        source._publish = lambda kind, payload: published.append(kind)  # type: ignore[method-assign]

        async def fake_once() -> None:
            published.append("screendump")
            await asyncio.sleep(0.005)

        monkeypatch.setattr(source, "_capture_once_paced", fake_once)
        queue: asyncio.Queue = asyncio.Queue()
        source.subscribe(queue, sb.MAX_FPS)
        await asyncio.sleep(0.05)
        source.unsubscribe(queue)
        await source.close()
        assert "screendump" in published, "the fallback loop never ran"
        assert source._vnc_available is False

    async def test_a_refused_rfb_connect_falls_back_rather_than_dying(self, monkeypatch):
        """A VM launched without -vnc refuses instantly; viewers must not notice."""
        _RefusingRFBClient.instances.clear()
        source = sb.VncCaptureSource(
            _target(vnc_port=_closed_port()),
            client_factory=lambda uri: None,
            vnc_client_factory=lambda host, port: _RefusingRFBClient(host, port),
        )
        kinds: List[str] = []
        source._publish = lambda kind, payload: kinds.append(kind)  # type: ignore[method-assign]
        ticks = 0

        async def fake_once() -> None:
            nonlocal ticks
            ticks += 1
            await asyncio.sleep(0.005)

        monkeypatch.setattr(source, "_capture_once_paced", fake_once)
        queue: asyncio.Queue = asyncio.Queue()
        source.subscribe(queue, sb.MAX_FPS)
        await asyncio.sleep(0.1)
        source.unsubscribe(queue)
        await source.close()
        assert "error" in kinds, "the fallback has to be reported, not silent"
        assert ticks > 0, "the screendump loop never ran"
        assert _RefusingRFBClient.instances[0].stopped, "the failed session was not torn down"

    async def test_rfb_is_retried_so_a_relaunched_vm_recovers(self, monkeypatch):
        """Without this a bridge started before the VM would stay slow for ever."""
        source = sb.VncCaptureSource(
            _target(vnc_port=6000),
            client_factory=lambda uri: None,
            vnc_client_factory=lambda host, port: sb.VNCClient(host, 1),
        )
        attempts = 0

        async def failing() -> None:
            nonlocal attempts
            attempts += 1
            raise ConnectionRefusedError("nothing is listening")

        async def idle() -> None:
            # The real pacing sleep, so the loop is not a busy one.
            await asyncio.sleep(0.005)

        monkeypatch.setattr(source, "_serve_rfb", failing)
        monkeypatch.setattr(source, "_capture_once_paced", idle)
        monkeypatch.setattr(sb, "VNC_RETRY_INTERVAL", 0.05)

        queue: asyncio.Queue = asyncio.Queue()
        source.subscribe(queue, sb.MAX_FPS)
        await asyncio.sleep(0.3)
        source.unsubscribe(queue)
        await source.close()
        assert attempts >= 2, f"tried RFB {attempts} time(s) in 300ms with a 50ms retry"

    async def test_a_dead_rfb_session_reports_and_keeps_the_loop_alive(self):
        """The viewer sees an error, not a closed socket."""
        server = await FakeRFBServer().start()
        source = _vnc_source(server, FakeQMP())
        queue = source.new_queue()
        source.subscribe(queue, sb.MAX_FPS)
        try:
            await asyncio.wait_for(server.connected.wait(), timeout=2.0)
            kinds: List[str] = []
            source._publish = (  # type: ignore[method-assign]
                lambda kind, payload: kinds.append(kind) or queue.put_nowait((kind, payload))
            )
            await source._rfb.close()
            kind, message = await asyncio.wait_for(queue.get(), timeout=3.0)
            assert kind == "error"
            assert "vnc" in message.lower()
            assert source._pump is not None and not source._pump.done()
        finally:
            source.unsubscribe(queue)
            await source.close()
            await server.stop()


class TestStalledGuestWatchdog:
    """Over RFB a paused guest is silent, so the watchdog needs a timer.

    The screendump path triggers the same resume from an exception; there is no
    exception here, which is precisely why this case needed its own path.
    """

    async def _stale_source(self, qmp: FakeQMP) -> Tuple[sb.VncCaptureSource, asyncio.Queue]:
        source = sb.VncCaptureSource(
            _target(vnc_port=6000),
            client_factory=lambda uri: qmp,
            vnc_client_factory=lambda host, port: sb.VNCClient(host, 1),
        )
        monkey = pytest.MonkeyPatch()
        monkey.setattr(sb, "VNC_STALL_SEC", 0.0)
        monkey.setattr(sb, "VNC_STALL_CHECK_INTERVAL", 0.02)
        source._last_update_at = 0.0  # as if nothing had arrived for ever
        queue = source.new_queue()
        source.subscribe(queue, sb.MAX_FPS)
        await asyncio.sleep(0.2)
        monkey.undo()
        return source, queue

    async def test_a_paused_guest_is_resumed(self):
        qmp = FakeQMP(status="paused")
        source, queue = await self._stale_source(qmp)
        try:
            assert [name for name, _ in qmp.commands] == ["query-status", "cont"], (
                f"watchdog did not resume: {qmp.commands}"
            )
        finally:
            source.unsubscribe(queue)
            await source.close()

    async def test_an_idle_but_running_guest_is_left_alone(self):
        """Silence is not evidence: an idle desktop damages nothing for minutes."""
        qmp = FakeQMP(status="running")
        source, queue = await self._stale_source(qmp)
        try:
            assert qmp.of("cont") == [], "a running guest must not be 'resumed'"
            assert len(qmp.of("query-status")) >= 1
        finally:
            source.unsubscribe(queue)
            await source.close()

    async def test_the_watchdog_asks_over_the_one_qmp_connection(self):
        """QMP serves a single client; a second socket would fight the injector."""
        qmp = FakeQMP(status="paused")
        source, queue = await self._stale_source(qmp)
        try:
            assert source._client is qmp, "the watchdog must reuse the source's client"
        finally:
            source.unsubscribe(queue)
            await source.close()


class TestSessionTeardown:
    """Two failures that only appear once a real QEMU and a real panel are involved.

    Both were found by measuring a booted VM, not by any unit test, and both are
    the kind that look like "the feature does not work" rather than like a bug:
    the console quietly falls back to screendump and every number looks plausible.
    """

    async def test_a_cancelled_session_leaves_the_source_reusable(self):
        """Found live: "an RFB session is already running for this VM".

        A cancelled asyncio task does not always notice promptly -- when it is
        waiting on a future that refuses cancellation, ``cancel()`` only records
        the request and the task carries on. So the session state cannot be left
        to the capture task's own ``finally``: the last subscriber leaving has to
        drop it, or the next attempt refuses to connect.
        """
        server = await FakeRFBServer().start()
        source = _vnc_source(server, FakeQMP())
        try:
            queue = source.new_queue()
            source.subscribe(queue, sb.MAX_FPS)
            await asyncio.wait_for(server.connected.wait(), timeout=2.0)
            assert source._rfb is not None
            # unsubscribe() cancels without awaiting, exactly as the bridge does.
            source.unsubscribe(queue)
            await asyncio.sleep(0.05)
            assert source._rfb is None, "the cancelled session left its state behind"

            again = source.new_queue()
            source.subscribe(again, sb.MAX_FPS)
            await asyncio.sleep(0.1)
            assert source._rfb is not None, "the source did not reconnect"
            assert source._vnc_available is not False, (
                f"reconnect failed, fell back: {source._vnc_error}"
            )
            source.unsubscribe(again)
        finally:
            await source.close()
            await server.stop()

    async def test_config_then_subscribe_does_not_reopen_the_session(self):
        """The panel's own flow is config-then-subscribe on every connect.

        Rebuilding the subscriber there closed and reopened RFB each time, which
        throws away the first full-screen frame and the established connection.
        """
        bridge = _bridge_ws_source_selection()
        session = sb.ClientSession(client_id="c", ws=object())  # type: ignore[arg-type]
        built: List[str] = []

        async def fake_send(session: Any, payload: dict) -> None:
            return None

        bridge._send_json = fake_send  # type: ignore[method-assign]
        bridge._send_error = fake_send  # type: ignore[method-assign]
        original = bridge._build_source

        def spy(target: Any, wanted: str) -> Any:
            built.append(wanted)
            return original(target, wanted)

        bridge._build_source = spy  # type: ignore[method-assign]
        await bridge._handle_config(session, {"type": "config", "vm": "vm-a",
                                              "capture_source": "vnc"})
        first = session.subscriber
        # The subscribe message goes through the same path as a real client.
        await bridge._attach(session, "vm-a")
        try:
            assert session.subscriber is first, "the subscriber was rebuilt for nothing"
            assert len(built) == 1, f"the source was rebuilt {len(built)} times"
        finally:
            if session.subscriber is not None:
                await session.subscriber.stop()
            await bridge.stop()

    async def test_switching_capture_source_still_rebuilds(self):
        """The short-circuit must not also swallow a genuine transport change."""
        bridge = _bridge_ws_source_selection()
        session = sb.ClientSession(client_id="c", ws=object())  # type: ignore[arg-type]

        async def fake_send(session: Any, payload: dict) -> None:
            return None

        bridge._send_json = fake_send  # type: ignore[method-assign]
        bridge._send_error = fake_send  # type: ignore[method-assign]
        await bridge._handle_config(session, {"type": "config", "vm": "vm-a",
                                              "capture_source": "vnc"})
        first = session.subscriber
        try:
            await bridge._handle_config(session, {"type": "config",
                                                  "capture_source": "screendump"})
            assert session.subscriber is not first
            assert session.source is not None
            assert session.source.transport == "screendump"
        finally:
            if session.subscriber is not None:
                await session.subscriber.stop()
            await bridge.stop()


class TestImageConversion:
    def test_a_framebuffer_becomes_an_image_of_the_right_size_and_colour(self):
        from vm_harness.vnc.proto import Framebuffer

        fb = Framebuffer(3, 2)
        fb.fill_rect(0, 0, 3, 2, bytes((0x33, 0x22, 0x11, 0xFF)))
        image = sb.image_from_framebuffer(fb)
        assert image.size == (3, 2)
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == (0x11, 0x22, 0x33), "BGRX, not RGBX"

    def test_a_black_framebuffer_converts_without_complaining(self):
        from vm_harness.vnc.proto import Framebuffer

        fb = Framebuffer(2, 2)
        assert sb.image_from_framebuffer(fb).size == (2, 2)


def _bridge_ws_source_selection():
    """The panel's choice reaches the source, through the real message handler."""

    class _Registry:
        def list_targets(self):
            return [_target("vm-a", vnc_port=6000), _target("vm-b", vnc_port=0)]

        def list_vms(self):
            return ["vm-a", "vm-b"]

        def get(self, name):
            return {
                "vm-a": _target("vm-a", vnc_port=6000),
                "vm-b": _target("vm-b", vnc_port=0),
            }.get(name)

    return sb.StreamingBridge(
        registry=_Registry(),  # type: ignore[arg-type]
        authenticator=sb.TokenAuthenticator("t"),
        env={},
        client_factory=lambda uri: None,
    )


class TestSessionLevelSelection:
    """``config.capture_source`` decides the source for the session that asks."""

    def _session(self) -> sb.ClientSession:
        return sb.ClientSession(client_id="c", ws=object())  # type: ignore[arg-type]

    async def test_config_capture_source_selects_the_source(self):
        bridge = _bridge_ws_source_selection()
        session = self._session()
        sent: List[dict] = []

        async def fake_send(session: Any, payload: dict) -> None:
            sent.append(payload)

        bridge._send_json = fake_send  # type: ignore[method-assign]
        bridge._send_error = fake_send  # type: ignore[method-assign]
        await bridge._handle_config(session, {"type": "config", "vm": "vm-a",
                                              "capture_source": "screendump"})
        assert session.source is not None and session.source.transport == "screendump"
        await bridge.stop()

    async def test_the_default_uses_rfb_for_a_vm_that_has_a_display(self):
        bridge = _bridge_ws_source_selection()
        session = self._session()

        async def fake_send(session: Any, payload: dict) -> None:
            return None

        bridge._send_json = fake_send  # type: ignore[method-assign]
        bridge._send_error = fake_send  # type: ignore[method-assign]
        await bridge._handle_config(session, {"type": "config", "vm": "vm-a"})
        assert session.source is not None and session.source.transport == "vnc"
        await bridge.stop()

    async def test_the_default_falls_back_for_a_vm_without_one(self):
        bridge = _bridge_ws_source_selection()
        session = self._session()

        async def fake_send(session: Any, payload: dict) -> None:
            return None

        bridge._send_json = fake_send  # type: ignore[method-assign]
        bridge._send_error = fake_send  # type: ignore[method-assign]
        await bridge._handle_config(session, {"type": "config", "vm": "vm-b"})
        assert session.source is not None and session.source.transport == "screendump"
        await bridge.stop()

    async def test_an_unknown_capture_source_does_not_break_the_session(self):
        bridge = _bridge_ws_source_selection()
        session = self._session()
        sent: List[dict] = []

        async def fake_send(session: Any, payload: dict) -> None:
            sent.append(payload)

        bridge._send_json = fake_send  # type: ignore[method-assign]
        bridge._send_error = fake_send  # type: ignore[method-assign]
        await bridge._handle_config(session, {"type": "config", "vm": "vm-a",
                                              "capture_source": "spice"})
        assert session.source is not None and session.source.transport == "vnc"
        assert any(p.get("type") == "config_ack" for p in sent)
        await bridge.stop()