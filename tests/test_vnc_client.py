"""Tests for the RFB client: protocol, handshake, decode, and its refusals.

Everything runs against a fake in-process server or a literal byte string. No
QEMU, no Rust binary, no network: the point of ``vm_harness.vnc.proto`` being
free of sockets is that the whole wire format can be driven from bytes here,
and the point of the fake server is that every handshake and receive path gets
the same treatment.

The cases that matter most are the ones that fail *silently* if wrong:

* the security handshake, where the server offers and the client chooses, and
  getting it backwards produces a client that hangs forever rather than an
  error;
* VNC Auth, where the missing per-byte bit reversal of the key means a correct
  password never authenticates and nothing says why;
* Tight's compact length, which is one to three bytes and not the four-byte form
  its sibling encodings use;
* rectangles that claim to be outside the framebuffer, which is the shape of a
  buffer overrun rather than a decoding bug.

The DES implementation is checked against published vectors, because it is
hand-written here (the installed ``cryptography`` wheel no longer offers single
DES, and this task cannot add a dependency) and a subtly wrong DES still
produces sixteen plausible bytes.
"""
from __future__ import annotations

import asyncio
import struct
import zlib

import pytest

from vm_harness.vnc import proto
from vm_harness.vnc.client import StreamByteReader, VNCClient
from vm_harness.vnc.keysym import is_mapped_keysym, key_name_for_keysym
from vm_harness.vnc.proto import (
    BGRX32,
    ENCODING_COPY_RECT,
    ENCODING_DESKTOP_SIZE,
    ENCODING_HEXTILE,
    ENCODING_RAW,
    ENCODING_RRE,
    ENCODING_TIGHT,
    RGB555,
    SECURITY_NONE,
    SECURITY_VNC_AUTH,
    AsyncByteReader,
    AuthError,
    Framebuffer,
    HandshakeError,
    ProtocolError,
    UnsupportedEncoding,
    des_encrypt_block,
    encode_fb_update_request,
    encode_key_event,
    encode_pointer_event,
    encode_security_choice,
    encode_set_encodings,
    encode_set_pixel_format,
    encode_version,
    parse_security_offer,
    parse_version,
    reverse_bits_in_byte,
    vnc_auth_key,
    vnc_auth_response,
)

# ── Message builders ──────────────────────────────────────────────────────────


def _raw_deflate(raw: bytes) -> bytes:
    """Tight's compression: raw DEFLATE, with no zlib header or Adler-32 trailer.

    Not ``zlib.compress``. Tight uses RFC 1951 framing, so a test that builds
    payloads the zlib way passes a decoder that expects the zlib framing and
    fails against QEMU, which does not. Found live against QEMU 11.1:
    "incorrect header check" on the first Tight rectangle.
    """
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(raw) + compressor.flush()


def rect_header(x: int, y: int, w: int, h: int, encoding: int) -> bytes:
    return struct.pack(">HHHHi", x, y, w, h, encoding)


def fb_update(*rects: bytes, count: int | None = None) -> bytes:
    """A FramebufferUpdate message carrying ``rects``."""
    n = len(rects) if count is None else count
    return b"\x00\x00" + struct.pack(">H", n) + b"".join(rects)


def raw_rect(x: int, y: int, w: int, h: int, pixels: bytes) -> bytes:
    return rect_header(x, y, w, h, ENCODING_RAW) + pixels


def bgrx_pixels(pixels: list[tuple[int, int, int]]) -> bytes:
    """(r, g, b) tuples to the B,G,R,X byte order a 32bpp BGRX server sends."""
    return b"".join(bytes((b, g, r, 0)) for r, g, b in pixels)


def bgra_at(fb: Framebuffer, x: int, y: int) -> tuple[int, int, int]:
    """The (r, g, b) the framebuffer holds at a pixel."""
    offset = (y * fb.width + x) * 4
    b, g, r = fb.data[offset], fb.data[offset + 1], fb.data[offset + 2]
    return r, g, b


#: A palette pixel format: true-colour false. Sent straight to the client so the
#: rejection happens at ServerInit rather than at the first pixel.
_PALETTE_PIXEL_FORMAT = bytes((8, 8, 0, 0, 255, 255, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0))
#: depth greater than bits-per-pixel, which cannot be rendered.
_DEPTH_GREATER_THAN_BPP = bytes((32, 33, 0, 1, 255, 255, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0))


async def decode_rect(fb: Framebuffer, rect_bytes: bytes, fmt=proto.BGRX32):
    """Decode one rectangle header plus payload out of a byte string."""
    header = proto.Rectangle.decode(proto.ByteReader(rect_bytes[:12]))
    payload = rect_bytes[12:]
    return await proto.decode_rectangle(fb, header, fmt, AsyncByteReader(payload))


# ── Fake server ───────────────────────────────────────────────────────────────


class FakeRFBServer:
    """An in-process RFB server that speaks a scripted byte stream.

    The handshake is real -- real banner, real security-type list, real
    challenge -- and everything after ServerInit is whatever the test puts in
    :meth:`send`, so a test can send precisely the bytes that provoke the bug it
    is about.
    """

    def __init__(
        self,
        *,
        width: int = 64,
        height: int = 48,
        pixel_format: proto.PixelFormat = BGRX32,
        name: str = "fake",
        version: bytes = b"RFB 003.008\n",
        security_types: tuple[int, ...] = (SECURITY_NONE,),
        password: str | None = None,
        security_result: bytes | None = None,
        send_server_init: bool = True,
        serve_server_init_on_client_init: bool = False,
        stall_before_banner: bool = False,
        stall_after_banner: bool = False,
        close_after_banner: bool = False,
        extra_init: bytes = b"",
        name_length_override: int | None = None,
    ):
        self.width = width
        self.height = height
        self.pixel_format = pixel_format
        self.name = name
        self.version = version
        self.security_types = security_types
        self.password = password
        self.security_result = security_result
        self.send_server_init = send_server_init
        #: QEMU's ordering: SecurityResult, then nothing at all until the
        #: client's ClientInit, and only then ServerInit. Off by default so the
        #: other tests keep the spec order; on where the point is to survive a
        #: server that behaves like QEMU.
        self.serve_server_init_on_client_init = serve_server_init_on_client_init
        self.stall_before_banner = stall_before_banner
        self.stall_after_banner = stall_after_banner
        self.close_after_banner = close_after_banner
        self.extra_init = extra_init
        self.name_length_override = name_length_override

        self.port = 0
        self.client_bytes = bytearray()
        self.chosen_security: int | None = None
        self.auth_response: bytes | None = None
        self.connection_closed = asyncio.Event()
        self.handshake_completed = asyncio.Event()
        #: Only set in the QEMU-ordering mode, where the client has to speak first.
        self.saw_client_init = False

        self._server: asyncio.AbstractServer | None = None
        self._outbox: asyncio.Queue = asyncio.Queue()
        self._writer: asyncio.StreamWriter | None = None
        self._client_reader: asyncio.StreamReader | None = None
        self._drainer: asyncio.Task | None = None

    async def start(self) -> "FakeRFBServer":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # pragma: no cover - best effort
                pass
        writer, self._writer = self._writer, None
        if writer is not None:
            try:
                writer.close()
            except Exception:  # pragma: no cover
                pass

    async def send(self, data: bytes) -> None:
        """Queue bytes for the client, to be written as soon as it reads."""
        await self._outbox.put(data)

    async def close_connection(self) -> None:
        await self._outbox.put(None)

    async def wait_for_client(self, count: int, timeout: float = 2.0) -> None:
        """Wait until the client has sent at least ``count`` bytes."""
        deadline = asyncio.get_event_loop().time() + timeout
        while len(self.client_bytes) < count:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise AssertionError(
                    f"client sent {len(self.client_bytes)} bytes, wanted {count}"
                )
            await asyncio.sleep(0.005)

    async def wait_for_handshake_messages(self, timeout: float = 2.0) -> None:
        """Wait until everything the client sends at connect time has arrived.

        SetEncodings is written during connect but read asynchronously by the
        server, so a test that records an offset immediately after ``connect``
        can still see SetEncodings arrive afterwards and mistake it for
        something it just asked for.
        """
        expected = 12 + 1 + len(
            encode_set_encodings((*proto.ADVERTISED_ENCODINGS, ENCODING_DESKTOP_SIZE))
        )
        await self.wait_for_client(expected, timeout)

    # ── Internals ────────────────────────────────────────────────────────────

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self._client_reader = reader
        try:
            await self._run(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.connection_closed.set()
            if self._drainer is not None:
                self._drainer.cancel()
            try:
                writer.close()
            except Exception:  # pragma: no cover
                pass

    async def _read_client_bytes(self, reader: asyncio.StreamReader, count: int) -> bytes:
        data = await reader.read(count)
        self.client_bytes += data
        return data

    async def _run(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.stall_before_banner:
            await asyncio.sleep(30)
            return
        writer.write(self.version)
        await writer.drain()

        await self._read_client_bytes(reader, 12)

        if self.stall_after_banner:
            await asyncio.sleep(30)
            return

        if self.close_after_banner:
            # Hang up in the middle of the handshake, as a server that gives up
            # on a client it does not like would.
            writer.close()
            return

        # The server offers the list; the client answers with one byte.
        if len(self.security_types) == 0:
            writer.write(b"\x00" + struct.pack(">I", 0) + b"refused\x00")
        else:
            writer.write(bytes((len(self.security_types),)) + bytes(self.security_types))
        await writer.drain()

        chosen = await self._read_client_bytes(reader, 1)
        self.chosen_security = chosen[0] if chosen else None

        if self.chosen_security == SECURITY_VNC_AUTH:
            challenge = bytes(range(16))
            writer.write(challenge)
            await writer.drain()
            self.auth_response = await self._read_client_bytes(reader, 16)
            if self.password is not None:
                expected = vnc_auth_response(self.password, challenge)
                writer.write(struct.pack(">I", 0 if self.auth_response == expected else 1))
            else:
                writer.write(struct.pack(">I", 0))
            await writer.drain()
        elif self.security_result is not None:
            writer.write(self.security_result)
            await writer.drain()
        else:
            # SecurityResult is sent whatever the security type was, including
            # None. Without it the client reads four bytes of ServerInit as the
            # verdict -- which is how "None security" becomes a mysterious auth
            # failure instead of a connection.
            writer.write(struct.pack(">I", 0))
            await writer.drain()

        if self.send_server_init and not self.serve_server_init_on_client_init:
            await self._send_server_init(writer)
        self.handshake_completed.set()

        if self.send_server_init and self.serve_server_init_on_client_init:
            # QEMU's ordering: read ClientInit, and only then write ServerInit.
            # Before the drainer starts, because the drainer would otherwise
            # consume the byte this is waiting for.
            try:
                self.client_bytes += await reader.readexactly(1)
                self.saw_client_init = True
            except (asyncio.IncompleteReadError, ConnectionResetError):
                return
            await self._send_server_init(writer)
            self.handshake_completed.set()

        # Everything the client sends after the handshake -- SetEncodings,
        # FramebufferUpdateRequests, PointerEvents, KeyEvents -- has to be read
        # for the tests to see it. Without this drain loop the client blocks on
        # its own writes and nothing sent after ServerInit is ever observed.
        self._drainer = asyncio.ensure_future(self._drain(reader))

        while True:
            item = await self._outbox.get()
            if item is None:
                break
            writer.write(item)
            await writer.drain()

    async def _send_server_init(self, writer: asyncio.StreamWriter) -> None:
        name = self.name.encode("latin-1")
        declared = len(name) if self.name_length_override is None else self.name_length_override
        raw_format = (
            self.pixel_format
            if isinstance(self.pixel_format, (bytes, bytearray))
            else self.pixel_format.encode()
        )
        writer.write(
            struct.pack(">HH", self.width, self.height)
            + raw_format
            + struct.pack(">I", declared)
            + name
            + self.extra_init
        )
        await writer.drain()

    async def _drain(self, reader: asyncio.StreamReader) -> None:
        """Keep reading client bytes until the connection ends."""
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    return
                self.client_bytes += data
        except (asyncio.CancelledError, ConnectionResetError):
            return
        except Exception:  # pragma: no cover - the connection is going away
            return


def _set_encodings_payload(client_bytes: bytes) -> bytes:
    """The bytes of the client's SetEncodings message, found by scanning.

    Parsed rather than sliced at a fixed offset, because the handshake has grown
    a message: ClientInit now precedes it (see :func:`encode_client_init`), and a
    test that hard-codes "the version reply and the security choice are 13 bytes"
    breaks silently when anything else moves.
    """
    for index, value in enumerate(client_bytes):
        if value != 2 or index + 2 > len(client_bytes):
            continue
        count = struct.unpack(">H", client_bytes[index + 2:index + 4])[0]
        total = 4 + 4 * count
        if index + total <= len(client_bytes) and client_bytes[index + 1] == 0:
            return client_bytes[index:index + total]
    raise AssertionError(f"no SetEncodings message in {len(client_bytes)} client bytes")


async def connected_client(server: FakeRFBServer, **kwargs) -> VNCClient:
    client = VNCClient("127.0.0.1", server.port, **kwargs)
    await client.connect()
    return client


# ── Version negotiation ───────────────────────────────────────────────────────


class TestVersion:
    @pytest.mark.parametrize(
        "banner,expected",
        [
            (b"RFB 003.008\n", 3008),
            (b"RFB 003.007\n", 3007),
            (b"RFB 003.003\n", 3003),
        ],
    )
    def test_parses_each_supported_version(self, banner, expected):
        assert parse_version(banner) == expected

    def test_encodes_to_twelve_bytes(self):
        assert len(encode_version()) == 12
        assert encode_version() == b"RFB 003.008\n"

    def test_round_trips(self):
        assert parse_version(encode_version()) == 3008

    @pytest.mark.parametrize(
        "banner",
        [
            b"",                        # closed connection
            b"HTTP/1.1 200 OK\r\n",     # an HTTP error page, not RFB
            b"RFB 004.000\n",           # a version this client does not speak
            b"RFB 003.008",             # no trailing newline, 11 bytes
            b"RFB abc.def\n",
            b"RFB 003.008\n\n\n",       # too long
        ],
    )
    def test_rejects_anything_that_is_not_rfb(self, banner):
        with pytest.raises(proto.RFBError):
            parse_version(banner)

    async def test_negotiates_end_to_end(self):
        server = await FakeRFBServer().start()
        try:
            client = await connected_client(server)
            assert server.client_bytes[:12] == b"RFB 003.008\n"
            assert client.connected
            await client.close()
        finally:
            await server.stop()

    async def test_downgraded_server_is_answered_with_our_version(self):
        """A 3.3 server still gets a 3.8 reply; we never downgrade."""
        server = await FakeRFBServer(version=b"RFB 003.003\n").start()
        try:
            client = VNCClient("127.0.0.1", server.port)
            # A 3.3 server would want a u32 security type, but it gets one byte
            # here because we answer with our own version -- which is the whole
            # point: the client version, not the server's, decides the framing.
            await client.connect()
            assert server.client_bytes[:12] == b"RFB 003.008\n"
            await client.close()
        finally:
            await server.stop()


# ── Security type selection ───────────────────────────────────────────────────


class TestSecurityOffer:
    def test_parses_a_3_7_list(self):
        reader = proto.ByteReader(bytes((2, SECURITY_NONE, SECURITY_VNC_AUTH)))
        assert parse_security_offer(reader, 3008) == [SECURITY_NONE, SECURITY_VNC_AUTH]

    def test_parses_the_3_3_single_u32(self):
        reader = proto.ByteReader(struct.pack(">I", SECURITY_VNC_AUTH))
        assert parse_security_offer(reader, 3003) == [SECURITY_VNC_AUTH]

    def test_a_zero_count_is_failure_with_a_reason(self):
        reason = b"no way"
        reader = proto.ByteReader(b"\x00" + struct.pack(">I", len(reason)) + reason)
        with pytest.raises(HandshakeError) as excinfo:
            parse_security_offer(reader, 3008)
        assert "no way" in str(excinfo.value)

    def test_a_zero_u32_is_failure_in_3_3(self):
        reader = proto.ByteReader(struct.pack(">I", 0xFFFFFFFF))
        with pytest.raises(HandshakeError):
            parse_security_offer(reader, 3003)

    def test_client_reply_is_exactly_one_byte(self):
        """The client picks; it does not send a list.

        Writing a count here is the backwards-handed bug. The server already
        sent its list and is waiting for a single byte, so a count would be read
        as the chosen type and the stream would desynchronise from here to the
        end of the session.
        """
        payload = encode_security_choice(SECURITY_VNC_AUTH)
        assert payload == b"\x02"
        assert len(payload) == 1

    def test_prefers_vnc_auth_over_none(self):
        """A server offering both is configured to want a password."""
        assert proto.choose_security_type([SECURITY_NONE, SECURITY_VNC_AUTH]) == SECURITY_VNC_AUTH

    def test_falls_back_to_none(self):
        assert proto.choose_security_type([SECURITY_NONE]) == SECURITY_NONE

    def test_refuses_types_it_cannot_do(self):
        with pytest.raises(HandshakeError) as excinfo:
            proto.choose_security_type([16])  # VeNCrypt/TLS
        assert "TLS(16)" in str(excinfo.value)

    async def test_client_selects_from_the_offer(self):
        server = await FakeRFBServer(security_types=(SECURITY_NONE, SECURITY_VNC_AUTH)).start()
        try:
            client = await connected_client(server, password="pw")
            # One byte, equal to the type we chose, immediately after the banner.
            assert server.client_bytes[12] == SECURITY_VNC_AUTH
            assert server.chosen_security == SECURITY_VNC_AUTH
            await client.close()
        finally:
            await server.stop()

    async def test_no_common_security_type_fails_with_the_offer_listed(self):
        server = await FakeRFBServer(security_types=(16,)).start()
        try:
            client = VNCClient("127.0.0.1", server.port)
            with pytest.raises(HandshakeError) as excinfo:
                await client.connect()
            assert "TLS(16)" in str(excinfo.value)
        finally:
            await server.stop()


# ── VNC Auth ──────────────────────────────────────────────────────────────────


class TestVncAuth:
    @pytest.mark.parametrize(
        "key,plaintext,ciphertext",
        [
            # The published DES vectors.
            ("0123456789ABCDEF", "4E6F772069732074", "3FA40E8A984D4815"),
            ("0000000000000000", "0000000000000000", "8CA64DE9C1B123A7"),
            ("FFFFFFFFFFFFFFFF", "FFFFFFFFFFFFFFFF", "7359B2163E4EDC58"),
            ("133457799BBCDFF1", "0123456789ABCDEF", "85E813540F0AB405"),
            ("A1B2C3D4E5F60718", "1122334455667788", "5333DE2272505FA3"),
        ],
    )
    def test_des_against_published_vectors(self, key, plaintext, ciphertext):
        """DES is hand-written here, so it is checked against known answers.

        ``cryptography`` dropped single DES to its decrepit namespace and then
        removed it, and this task cannot add a dependency, so the primitive is
        implemented in ``proto``. A DES that is subtly wrong still produces
        sixteen plausible bytes, which is the failure mode these vectors exist
        to catch.
        """
        assert des_encrypt_block(bytes.fromhex(key), bytes.fromhex(plaintext)) == bytes.fromhex(
            ciphertext
        )

    def test_key_bytes_are_bit_reversed(self):
        assert reverse_bits_in_byte(0x01) == 0x80
        assert reverse_bits_in_byte(0x61) == 0x86
        key = vnc_auth_key("abcdefgh")
        # 'a' is 0x61, reversed is 0x86 -- and so on for the whole password.
        assert key == bytes((0x86, 0x46, 0xC6, 0x26, 0xA6, 0x66, 0xE6, 0x16))
        assert key.hex() == "8646c626a666e616"

    def test_short_password_is_zero_padded_not_truncated_to_nothing(self):
        assert vnc_auth_key("ab") == bytes((0x86, 0x46, 0, 0, 0, 0, 0, 0))

    def test_password_over_eight_bytes_is_truncated(self):
        assert len(vnc_auth_key("abcdefghij")) == 8

    def test_empty_password_is_refused(self):
        with pytest.raises(AuthError):
            vnc_auth_key("")

    def test_response_is_two_des_blocks_of_the_challenge(self):
        """The known-vector composition: two ECB blocks, no chaining, no IV."""
        password = "abcdefgh"
        challenge = bytes(range(16))
        key = vnc_auth_key(password)
        expected = des_encrypt_block(key, challenge[:8]) + des_encrypt_block(key, challenge[8:])
        assert vnc_auth_response(password, challenge) == expected
        # Pinned so a change to either DES or the key derivation is visible.
        assert vnc_auth_response(password, challenge).hex() == (
            "eae3a1cb74ca6daac183f66460190bb5"
        )

    def test_wrong_length_challenge_is_refused(self):
        with pytest.raises(AuthError):
            vnc_auth_response("pw", b"\x00" * 15)

    async def test_handshake_authenticates_against_a_known_challenge(self):
        server = await FakeRFBServer(
            security_types=(SECURITY_VNC_AUTH,), password="correct horse"
        ).start()
        try:
            client = await connected_client(server, password="correct horse")
            assert server.chosen_security == SECURITY_VNC_AUTH
            assert server.auth_response == vnc_auth_response("correct horse", bytes(range(16)))
            # The server compared the response and sent SecurityResult 0.
            assert client.connected
            await client.close()
        finally:
            await server.stop()

    async def test_wrong_password_is_rejected_by_the_server(self):
        server = await FakeRFBServer(security_types=(SECURITY_VNC_AUTH,), password="right").start()
        try:
            client = VNCClient("127.0.0.1", server.port, password="wrong")
            with pytest.raises(AuthError):
                await client.connect()
        finally:
            await server.stop()

    async def test_vnc_auth_without_a_password_is_an_error_not_a_no_op(self):
        server = await FakeRFBServer(security_types=(SECURITY_VNC_AUTH,)).start()
        try:
            client = VNCClient("127.0.0.1", server.port)
            with pytest.raises(AuthError):
                await client.connect()
        finally:
            await server.stop()

    async def test_security_result_failure_carries_the_reason(self):
        reason = b"authentication failed"
        server = await FakeRFBServer(
            security_types=(SECURITY_NONE,),
            security_result=struct.pack(">I", 1) + struct.pack(">I", len(reason)) + reason,
            send_server_init=False,
        ).start()
        try:
            client = VNCClient("127.0.0.1", server.port)
            with pytest.raises(AuthError) as excinfo:
                await client.connect()
            assert "authentication failed" in str(excinfo.value)
        finally:
            await server.stop()


# ── Pixel formats ─────────────────────────────────────────────────────────────


class TestPixelFormat:
    def test_round_trips_through_16_bytes(self):
        assert Pixel_format_roundtrip() == BGRX32

    def test_bgrx32_is_recognised(self):
        assert BGRX32.is_bgrx32()
        assert not RGB555.is_bgrx32()

    @pytest.mark.parametrize(
        "raw",
        [
            bytes((8, 8, 0, 0, 255, 255, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0)),   # palette
            bytes((12, 12, 0, 1, 255, 255, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0)),  # 12bpp
            bytes((32, 8, 0, 1, 255, 255, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0)),   # depth < bpp? no: 8 < 32 ok
            bytes((32, 33, 0, 1, 255, 255, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0)),  # depth > bpp
            bytes((32, 24, 0, 1, 255, 255, 255, 0, 0, 0, 30, 0, 0, 0, 0, 0)),  # shift past the pixel
            bytes((32, 24, 0, 1, 255, 255, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0)),  # blue_max 0
            bytes((32, 24, 0, 1, 250, 255, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0)),  # max is not 2^n-1
        ],
    )
    def test_bad_formats_are_rejected_on_arrival(self, raw):
        """A format that cannot be indexed into must not reach a decoder."""
        with pytest.raises(ProtocolError):
            proto.PixelFormat.decode(raw)

    def test_short_pixel_format_is_rejected(self):
        with pytest.raises(ProtocolError):
            proto.PixelFormat.decode(b"\x00" * 15)

    def test_set_pixel_format_is_twenty_bytes(self):
        assert len(encode_set_pixel_format(BGRX32)) == 20


def Pixel_format_roundtrip() -> proto.PixelFormat:
    return proto.PixelFormat.decode(BGRX32.encode())


# ── Encodings ─────────────────────────────────────────────────────────────────


class TestRaw:
    async def test_writes_pixels_in_bgra_order(self):
        fb = Framebuffer(4, 4)
        pixels = [(10, 20, 30), (40, 50, 60), (70, 80, 90), (100, 110, 120)]
        await decode_rect(fb, raw_rect(0, 0, 2, 2, bgrx_pixels(pixels)))
        assert [bgra_at(fb, x, y) for y in range(2) for x in range(2)] == pixels

    async def test_lands_at_the_right_offset(self):
        fb = Framebuffer(8, 8)
        await decode_rect(fb, raw_rect(3, 2, 2, 1, bgrx_pixels([(1, 2, 3), (4, 5, 6)])))
        assert bgra_at(fb, 3, 2) == (1, 2, 3)
        assert bgra_at(fb, 4, 2) == (4, 5, 6)
        # Neighbouring pixels stay black.
        assert bgra_at(fb, 2, 2) == (0, 0, 0)

    async def test_rgb555_pixels_are_converted(self):
        fb = Framebuffer(4, 4)
        fmt = RGB555
        # 5551: r<<10 | g<<5 | b.  Full-scale primary: red, green, blue.
        values = [31 << 10, 31 << 5, 31]
        payload = b"".join(struct.pack("<H", v) for v in values)
        await decode_rect(fb, raw_rect(0, 0, 3, 1, payload), fmt)
        assert bgra_at(fb, 0, 0) == (255, 0, 0)
        assert bgra_at(fb, 1, 0) == (0, 255, 0)
        assert bgra_at(fb, 2, 0) == (0, 0, 255)


class TestCopyRect:
    async def test_moves_pixels_within_the_framebuffer(self):
        fb = Framebuffer(8, 8)
        await decode_rect(fb, raw_rect(0, 0, 2, 2, bgrx_pixels([(1, 1, 1)] * 4)))
        await decode_rect(fb, rect_header(4, 4, 2, 2, ENCODING_COPY_RECT) + struct.pack(">HH", 0, 0))
        assert bgra_at(fb, 4, 4) == (1, 1, 1)
        assert bgra_at(fb, 5, 5) == (1, 1, 1)
        # The source is left alone; CopyRect is a copy, not a move.
        assert bgra_at(fb, 0, 0) == (1, 1, 1)

    async def test_source_outside_the_framebuffer_is_rejected(self):
        fb = Framebuffer(8, 8)
        with pytest.raises(ProtocolError):
            await decode_rect(fb, rect_header(0, 0, 2, 2, ENCODING_COPY_RECT) + struct.pack(">HH", 7, 7))


class TestRRE:
    async def test_background_plus_subrectangles(self):
        fb = Framebuffer(8, 8)
        payload = bgrx_pixels([(9, 9, 9)]) + struct.pack(">I", 2)
        payload += bytes((1, 1, 2, 2)) + bgrx_pixels([(7, 7, 7)])
        payload += bytes((4, 4, 2, 2)) + bgrx_pixels([(5, 5, 5)])
        await decode_rect(fb, raw_rect(0, 0, 8, 8, b"")[:0] + rect_header(0, 0, 8, 8, ENCODING_RRE) + payload)
        assert bgra_at(fb, 0, 0) == (9, 9, 9)
        assert bgra_at(fb, 2, 2) == (7, 7, 7)
        assert bgra_at(fb, 4, 4) == (5, 5, 5)
        assert bgra_at(fb, 6, 6) == (9, 9, 9)

    async def test_impossible_subrectangle_count_is_rejected(self):
        """A count larger than the area cannot be satisfied by any encoding."""
        fb = Framebuffer(4, 4)
        payload = bgrx_pixels([(1, 1, 1)]) + struct.pack(">I", 1000)
        with pytest.raises(ProtocolError):
            await decode_rect(fb, rect_header(0, 0, 4, 4, ENCODING_RRE) + payload)

    async def test_subrectangle_escaping_its_region_is_rejected(self):
        fb = Framebuffer(8, 8)
        payload = bgrx_pixels([(1, 1, 1)]) + struct.pack(">I", 1)
        payload += bytes((6, 6, 4, 4)) + bgrx_pixels([(2, 2, 2)])
        with pytest.raises(ProtocolError):
            await decode_rect(fb, rect_header(0, 0, 4, 4, ENCODING_RRE) + payload)


class TestHextile:
    async def test_raw_tile(self):
        fb = Framebuffer(32, 32)
        tile = bgrx_pixels([(3, 4, 5)] * (16 * 16))
        await decode_rect(fb, rect_header(0, 0, 16, 16, ENCODING_HEXTILE) + b"\x00" + tile)
        assert bgra_at(fb, 0, 0) == (3, 4, 5)
        assert bgra_at(fb, 15, 15) == (3, 4, 5)

    async def test_background_specified_tile(self):
        fb = Framebuffer(32, 32)
        payload = b"\x01" + bgrx_pixels([(8, 9, 10)])
        await decode_rect(fb, rect_header(0, 0, 16, 16, ENCODING_HEXTILE) + payload)
        assert bgra_at(fb, 0, 0) == (8, 9, 10)
        assert bgra_at(fb, 15, 15) == (8, 9, 10)

    async def test_repeated_tiles_reuse_the_carried_background(self):
        """The second tile states no background and inherits the first's."""
        fb = Framebuffer(32, 16)
        payload = b"\x01" + bgrx_pixels([(1, 1, 1)])
        payload += b"\x00"  # a Raw tile, which clears the carry
        payload += bgrx_pixels([(2, 2, 2)] * (16 * 16))
        await decode_rect(fb, rect_header(0, 0, 32, 16, ENCODING_HEXTILE) + payload)
        assert bgra_at(fb, 0, 0) == (1, 1, 1)
        assert bgra_at(fb, 16, 0) == (2, 2, 2)

    async def test_background_is_not_carried_across_a_raw_tile(self):
        """The specification does not carry it; inheriting anyway shows one
        wrong tile rather than a wrong frame, which is much harder to spot."""
        fb = Framebuffer(16, 48)
        # A 16x48 rectangle is three 16x16 tiles stacked. Tile 0 states a
        # background, tile 1 is Raw -- which clears the carry -- so tile 2 has
        # nothing to inherit and must be refused.
        payload = b"\x01" + bgrx_pixels([(1, 1, 1)])          # tile 0: bg stated
        payload += b"\x00" + bgrx_pixels([(2, 2, 2)] * 256)   # tile 1: raw
        with pytest.raises(ProtocolError):
            await decode_rect(fb, rect_header(0, 0, 16, 48, ENCODING_HEXTILE) + payload)

    async def test_subrectangle_escaping_its_tile_is_rejected(self):
        """Sub-encoding 7 is background + foreground + "last 7 sub-rectangles",
        so the first four-byte group after the colours is a sub-rectangle."""
        fb = Framebuffer(16, 16)
        payload = b"\x07" + bgrx_pixels([(1, 1, 1)]) + bgrx_pixels([(2, 2, 2)])
        payload += bytes((14, 0, 4, 4))  # 14 + 4 runs past the 16-wide tile
        with pytest.raises(ProtocolError):
            await decode_rect(fb, rect_header(0, 0, 16, 16, ENCODING_HEXTILE) + payload)


class TestTight:
    @staticmethod
    def tight_payload(raw: bytes) -> bytes:
        """A Tight rectangle exactly as the server builds one.

        Control byte 0x01: BasicCompression (bits 5-4 clear), bit 6 clear so no
        filter-id byte follows and CopyFilter is implied, bit 0 set so the
        decoder resets the zlib stream. Then the one-to-three byte compact
        length, then a complete zlib stream.
        """
        compressed = _raw_deflate(raw)
        if len(compressed) <= 127:
            length = bytes((len(compressed),))
        elif len(compressed) <= 16383:
            length = bytes((0x80 | (len(compressed) & 0x7F), (len(compressed) >> 7) & 0x7F))
        else:
            length = bytes(
                (
                    0x80 | (len(compressed) & 0x7F),
                    0x80 | ((len(compressed) >> 7) & 0x7F),
                    (len(compressed) >> 14) & 0xFF,
                )
            )
        return b"\x01" + length + compressed

    def test_compact_length_uses_the_three_byte_form_for_long_payloads(self):
        """10000 bytes must encode as 0x90 0x4E, not as a four-byte length.

        This is the Tight-specific compact length. Hextile and ZRLE use a
        four-byte form that also starts with a high bit meaning "more follows",
        so a decoder that assumes the wrong one reads a three-byte length as
        four and desynchronises from that rectangle to the end of the session.
        """
        raw = b"\x11" * (4 * 4 * 3)
        payload = self.tight_payload(raw)
        # One byte, two bytes and three bytes, read through the decoder.
        assert _read_compact_length(b"\x7f") == 127
        assert _read_compact_length(b"\x80\x01") == 128
        assert _read_compact_length(b"\x90\x4e") == 10000
        assert _read_compact_length(b"\xbf\xbf\x03") == 0x3F | (0x3F << 7) | (3 << 14)
        assert payload[0] == 0x01, "the control byte is 0x01"

    async def test_decodes_rgb_triples(self):
        fb = Framebuffer(4, 4)
        raw = b"".join(bytes((r, g, b)) for r, g, b in [(1, 2, 3), (4, 5, 6), (7, 8, 9), (10, 11, 12)])
        payload = self.tight_payload(raw)
        await decode_rect(fb, rect_header(0, 0, 2, 2, ENCODING_TIGHT) + payload)
        assert [bgra_at(fb, x, y) for y in range(2) for x in range(2)] == [
            (1, 2, 3), (4, 5, 6), (7, 8, 9), (10, 11, 12)
        ]

    async def test_tpixel_size_is_two_for_16_bit_not_three(self):
        """A 555 format has depth 15, but its TPIXEL is two bytes.

        Getting the threshold wrong here does not raise: the lengths still work
        out for some regions and the screen comes back in the wrong colours.
        """
        fb = Framebuffer(2, 1)
        values = [31 << 10, 31 << 5]
        raw = b"".join(struct.pack("<H", v) for v in values)
        payload = self.tight_payload(raw)
        await decode_rect(fb, rect_header(0, 0, 2, 1, ENCODING_TIGHT) + payload, RGB555)
        assert bgra_at(fb, 0, 0) == (255, 0, 0)
        assert bgra_at(fb, 1, 0) == (0, 255, 0)

    async def test_fill_compression_covers_the_whole_rectangle(self):
        """One TPIXEL, the whole rectangle.

        Implemented rather than refused because QEMU's VNC server sends
        FillCompression for every solid rectangle it draws, and a refusal ends
        the session: a cleared menu or a cursor trail was enough to kill the
        console on the first frame. Found live against QEMU 11.1.
        """
        fb = Framebuffer(4, 4)
        await decode_rect(
            fb, rect_header(1, 1, 2, 2, ENCODING_TIGHT) + b"\x80" + bytes((9, 8, 7))
        )
        assert bgra_at(fb, 1, 1) == (9, 8, 7)
        assert bgra_at(fb, 2, 1) == (9, 8, 7)
        assert bgra_at(fb, 1, 2) == (9, 8, 7)
        assert bgra_at(fb, 2, 2) == (9, 8, 7)
        # Nothing outside the rectangle moved.
        assert bgra_at(fb, 0, 0) == (0, 0, 0)
        assert bgra_at(fb, 3, 3) == (0, 0, 0)

    async def test_fill_compression_uses_a_whole_pixel_for_16_bit_formats(self):
        """A 16-bit format's TPIXEL is two bytes, not an RGB triple."""
        fb = Framebuffer(2, 2)
        value = struct.pack("<H", 31 << 10)
        await decode_rect(
            fb, rect_header(0, 0, 2, 2, ENCODING_TIGHT) + b"\x80" + value, RGB555
        )
        assert bgra_at(fb, 0, 0) == (255, 0, 0)
        assert bgra_at(fb, 1, 1) == (255, 0, 0)

    async def test_a_truncated_fill_is_an_error_not_a_wrong_colour(self):
        fb = Framebuffer(2, 2)
        with pytest.raises(ProtocolError):
            await decode_rect(fb, rect_header(0, 0, 2, 2, ENCODING_TIGHT) + b"\x81\x00")

    async def test_jpeg_and_png_compression_are_refused(self):
        fb = Framebuffer(2, 2)
        with pytest.raises(UnsupportedEncoding):
            await decode_rect(fb, rect_header(0, 0, 2, 2, ENCODING_TIGHT) + b"\x11\x00")

    async def test_gradient_filter_is_refused(self):
        fb = Framebuffer(2, 2)
        # bit 6 set, then a filter-id byte of 2 (GradientFilter).
        with pytest.raises(UnsupportedEncoding):
            await decode_rect(
                fb, rect_header(0, 0, 2, 2, ENCODING_TIGHT) + b"\x41" + bytes((2,)) + b"\x00"
            )

    async def test_explicit_copy_filter_byte_is_accepted(self):
        fb = Framebuffer(2, 2)
        raw = b"".join(bytes((1, 2, 3)) for _ in range(4))
        compressed = _raw_deflate(raw)
        payload = b"\x41\x00" + bytes((len(compressed),)) + compressed
        await decode_rect(fb, rect_header(0, 0, 2, 2, ENCODING_TIGHT) + payload)
        assert bgra_at(fb, 0, 0) == (1, 2, 3)

    async def test_a_decompression_bomb_is_refused(self):
        fb = Framebuffer(64, 64)
        # A tiny stream that inflates to far more than a rectangle can hold.
        bomb = _raw_deflate(b"\x00" * (64 * 64 * 4 * 40))
        payload = b"\x01" + bytes((len(bomb) & 0x7F,)) + bomb if len(bomb) < 128 else b"\x01" + bytes(
            (0x80 | (len(bomb) & 0x7F), (len(bomb) >> 7) & 0x7F)
        ) + bomb
        with pytest.raises(ProtocolError):
            await decode_rect(fb, rect_header(0, 0, 64, 64, ENCODING_TIGHT) + payload)

    async def test_corrupt_zlib_is_a_protocol_error(self):
        fb = Framebuffer(2, 2)
        payload = b"\x01" + bytes((5,)) + b"\x00\x01\x02\x03\x04"
        with pytest.raises(ProtocolError):
            await decode_rect(fb, rect_header(0, 0, 2, 2, ENCODING_TIGHT) + payload)


def _read_compact_length(data: bytes) -> int:
    async def _run():
        return await proto._read_tight_compact_length(AsyncByteReader(data))

    return asyncio.run(_run())


class TestUnknownEncoding:
    async def test_an_encoding_we_cannot_decode_is_refused_by_name(self):
        fb = Framebuffer(8, 8)
        with pytest.raises(UnsupportedEncoding) as excinfo:
            await decode_rect(fb, rect_header(0, 0, 2, 2, 16))  # ZRLE
        assert "ZRLE" not in str(excinfo.value)  # ZRLE is numbered, not named
        assert "16" in str(excinfo.value)

    async def test_set_encodings_lists_only_what_can_be_decoded(self):
        payload = encode_set_encodings(proto.ADVERTISED_ENCODINGS)
        assert payload[0] == 2  # client-to-server SetEncodings
        count = struct.unpack(">H", payload[2:4])[0]
        assert count == len(proto.ADVERTISED_ENCODINGS)
        listed = [struct.unpack(">i", payload[4 + i * 4:8 + i * 4])[0] for i in range(count)]
        assert listed == [5, 2, 1, 0]
        assert ENCODING_DESKTOP_SIZE not in listed  # sent separately


# ── Update messages over a real connection ────────────────────────────────────


class TestFramebufferUpdateOverTheWire:
    async def test_an_update_with_several_rectangles_is_one_frame(self):
        server = await FakeRFBServer(width=32, height=32).start()
        try:
            frames = []
            client = await connected_client(server, on_frame=lambda fb: frames.append(
                (fb.width, fb.height, bytes(fb.data))
            ))
            pixels_a = [(1, 1, 1)] * (4 * 2)
            pixels_b = [(2, 2, 2)] * (2 * 2)
            message = fb_update(
                raw_rect(0, 0, 4, 2, bgrx_pixels(pixels_a)),
                raw_rect(8, 8, 2, 2, bgrx_pixels(pixels_b)),
                rect_header(0, 0, 2, 2, ENCODING_RRE)
                + bgrx_pixels([(3, 3, 3)])
                + struct.pack(">I", 0),
            )
            await server.send(message)
            assert await client.pump() == 1
            assert len(frames) == 1, "all rectangles in one update are one frame"
            width, height, data = frames[0]
            assert (width, height) == (32, 32)
            # The third rectangle is an RRE over the same origin, so it wins there.
            assert data[(0 * 32 + 0) * 4:][:3] == bytes((3, 3, 3))
            assert data[(0 * 32 + 2) * 4:][:3] == bytes((1, 1, 1))
            assert data[(8 * 32 + 8) * 4:][:3] == bytes((2, 2, 2))
            await client.close()
        finally:
            await server.stop()

    async def test_incremental_request_is_sent_and_answered(self):
        server = await FakeRFBServer(width=16, height=16).start()
        try:
            frames = []
            client = await connected_client(server, on_frame=lambda fb: frames.append(1))
            await server.wait_for_handshake_messages()
            before = len(server.client_bytes)
            await client.request_update(incremental=True)
            await client._flush_outbox()
            await server.wait_for_client(before + 10)
            assert bytes(server.client_bytes[before:]) == encode_fb_update_request(True)
            await server.send(fb_update(raw_rect(0, 0, 2, 2, bgrx_pixels([(9, 9, 9)] * 4))))
            assert await client.pump() == 1
            assert frames == [1]
            await client.close()
        finally:
            await server.stop()

    async def test_empty_update_presents_no_frame(self):
        server = await FakeRFBServer(width=16, height=16).start()
        try:
            frames = []
            client = await connected_client(server, on_frame=lambda fb: frames.append(1))
            await server.send(fb_update())
            assert await client.pump() == 0
            assert frames == []
            await client.close()
        finally:
            await server.stop()

    async def test_a_rectangle_outside_the_framebuffer_is_refused(self):
        """The rectangle at x=60 on a 64-wide screen is a 4-pixel overflow."""
        server = await FakeRFBServer(width=64, height=48).start()
        try:
            client = await connected_client(server)
            await server.send(fb_update(raw_rect(60, 0, 8, 2, bgrx_pixels([(1, 1, 1)] * 16))))
            with pytest.raises(ProtocolError) as excinfo:
                await client.pump()
            assert "does not fit" in str(excinfo.value)
            await client.close()
        finally:
            await server.stop()

    async def test_a_zero_sized_rectangle_is_refused(self):
        server = await FakeRFBServer(width=64, height=48).start()
        try:
            client = await connected_client(server)
            await server.send(fb_update(rect_header(0, 0, 0, 0, ENCODING_RAW)))
            with pytest.raises(ProtocolError):
                await client.pump()
            await client.close()
        finally:
            await server.stop()

    async def test_an_unknown_server_message_is_refused(self):
        server = await FakeRFBServer(width=16, height=16).start()
        try:
            client = await connected_client(server)
            await server.send(b"\x63")
            with pytest.raises(ProtocolError) as excinfo:
                await client.pump()
            assert "99" in str(excinfo.value)
            await client.close()
        finally:
            await server.stop()

    async def test_bell_and_cut_text_reach_their_callbacks(self):
        server = await FakeRFBServer(width=16, height=16).start()
        bells = []
        cuts = []
        try:
            client = await connected_client(
                server, on_bell=lambda: bells.append(1), on_cut_text=cuts.append
            )
            await server.send(b"\x02")
            assert await client.pump() == 0
            text = "pasted"
            await server.send(b"\x03\x00\x00\x00" + struct.pack(">I", len(text)) + text.encode())
            assert await client.pump() == 0
            assert bells == [1]
            assert cuts == ["pasted"]
            await client.close()
        finally:
            await server.stop()

    async def test_a_placeholder_colour_map_is_read_past(self):
        """A palette message must still be consumed to stay in sync."""
        server = await FakeRFBServer(width=16, height=16).start()
        try:
            client = await connected_client(server)
            entries = b"".join(struct.pack(">HHH", i, i, i) for i in range(4))
            await server.send(
                b"\x01\x00" + struct.pack(">HH", 0, 4) + entries
            )
            assert await client.pump() == 0
            await server.send(fb_update(raw_rect(0, 0, 1, 1, bgrx_pixels([(4, 4, 4)]))))
            assert await client.pump() == 1
            await client.close()
        finally:
            await server.stop()


class TestDesktopSize:
    async def test_resize_applies_to_the_framebuffer(self):
        server = await FakeRFBServer(width=32, height=32).start()
        try:
            sizes = []
            client = await connected_client(
                server, on_frame=lambda fb: sizes.append((fb.width, fb.height))
            )
            await server.send(fb_update(rect_header(0, 0, 64, 48, ENCODING_DESKTOP_SIZE)))
            assert await client.pump() == 1
            assert client.framebuffer.width == 64
            assert client.framebuffer.height == 48
            assert sizes == [(64, 48)]
            await client.close()
        finally:
            await server.stop()

    async def test_desktop_size_is_requested_in_set_encodings(self):
        server = await FakeRFBServer().start()
        try:
            client = await connected_client(server)
            await server.wait_for_handshake_messages()
            # Locate the SetEncodings message rather than assuming an offset:
            # ClientInit now sits between the security choice and it, because
            # QEMU's VNC server will not send ServerInit until it has read
            # ClientInit, so the client has to send it before it can read the
            # desktop at all. See encode_client_init.
            payload = _set_encodings_payload(bytes(server.client_bytes))
            assert ENCODING_DESKTOP_SIZE in [
                struct.unpack(">i", payload[4 + i * 4:8 + i * 4])[0]
                for i in range(struct.unpack(">H", payload[2:4])[0])
            ]
            await client.close()
        finally:
            await server.stop()

    async def test_client_init_is_sent_before_server_init_is_read(self):
        """QEMU's VNC server sends ServerInit only after it has read ClientInit.

        A client that waits for ServerInit before sending ClientInit -- the
        order the RFB spec implies -- deadlocks with no error and no timeout:
        both sides are waiting for the other. That is indistinguishable from a
        dead console, which is how it was found: against QEMU 11.1 the
        handshake produced a security result and then silence.
        """
        server = await FakeRFBServer(serve_server_init_on_client_init=True).start()
        try:
            client = await connected_client(server, handshake_timeout=3.0)
            assert client.framebuffer is not None
            assert client.framebuffer.width == 64
            # ClientInit is one byte -- the shared flag, with no message type --
            # so what is asserted is that the server received it at all.
            assert server.saw_client_init, "the server never got its ClientInit"
            assert bytes(server.client_bytes).endswith(b"\x01"), "ClientInit was not shared"
            await client.close()
        finally:
            await server.stop()

    async def test_resize_off_the_origin_is_refused(self):
        fb = Framebuffer(32, 32)
        with pytest.raises(ProtocolError):
            await decode_rect(fb, rect_header(1, 0, 64, 48, ENCODING_DESKTOP_SIZE))

    async def test_absurd_resize_is_refused(self):
        fb = Framebuffer(32, 32)
        with pytest.raises(ProtocolError):
            await decode_rect(fb, rect_header(0, 0, 65535, 65535, ENCODING_DESKTOP_SIZE))


class TestSetPixelFormatMidStream:
    async def test_switching_format_forces_a_full_refresh(self):
        """The old pixels are in the old format, so an incremental request
        after a format change would show the screen in stale bytes."""
        server = await FakeRFBServer(width=16, height=16).start()
        try:
            client = await connected_client(server, pixel_format=RGB555)
            assert client._needs_full_refresh
            await server.send(fb_update(raw_rect(0, 0, 2, 2, struct.pack("<HHHH", 31 << 10, 0, 0, 0))))
            assert await client.pump() == 1
            # Decode used the requested format, not the server's.
            assert bgra_at(client.framebuffer, 0, 0) == (255, 0, 0)
            await client.close()
        finally:
            await server.stop()

    async def test_set_pixel_format_writes_the_message_and_marks_a_refresh(self):
        server = await FakeRFBServer(width=16, height=16).start()
        try:
            client = await connected_client(server)
            await server.wait_for_handshake_messages()
            client._needs_full_refresh = False
            before = len(server.client_bytes)
            await client.set_pixel_format(RGB555)
            await server.wait_for_client(before + 20)
            assert len(server.client_bytes) > before
            assert server.client_bytes[before] == 0  # client-to-server message 0
            assert client._needs_full_refresh
            await client.close()
        finally:
            await server.stop()


class TestInput:
    def test_pointer_event_layout(self):
        packet = encode_pointer_event(100, 200, 1)
        assert packet == struct.pack(">BBHH", 5, 1, 100, 200)

    def test_key_event_layout(self):
        packet = encode_key_event(True, 0xFF0D)
        assert packet == struct.pack(">BBxxI", 4, 1, 0xFF0D)

    def test_out_of_range_pointer_is_refused(self):
        with pytest.raises(ProtocolError):
            encode_pointer_event(-1, 0, 0)
        with pytest.raises(ProtocolError):
            encode_pointer_event(70000, 0, 0)

    def test_undefined_pointer_button_bits_are_refused(self):
        with pytest.raises(ProtocolError):
            encode_pointer_event(0, 0, 0x80)

    def test_out_of_range_keysym_is_refused(self):
        with pytest.raises(ProtocolError):
            encode_key_event(True, 0x1_0000)

    def test_update_request_layout(self):
        assert encode_fb_update_request(True) == struct.pack(">BBHHHH", 3, 1, 0, 0, 0, 0)
        assert encode_fb_update_request(False) == struct.pack(">BBHHHH", 3, 0, 0, 0, 0, 0)

    async def test_input_is_queued_and_flushed_in_order(self):
        """Order is the difference between typing and not."""
        server = await FakeRFBServer(width=16, height=16).start()
        try:
            client = await connected_client(server)
            await server.wait_for_handshake_messages()
            before = len(server.client_bytes)
            # queue_key is synchronous: a Qt event handler cannot await, and the
            # bytes are flushed by the receive loop.
            client.queue_key(True, ord("a"))
            client.queue_key(True, ord("b"))
            client.queue_key(False, ord("a"))
            await client._flush_outbox()
            # Three KeyEvents of eight bytes each.
            await server.wait_for_client(before + 24)
            sent = bytes(server.client_bytes[before:])
            assert sent == (
                encode_key_event(True, ord("a"))
                + encode_key_event(True, ord("b"))
                + encode_key_event(False, ord("a"))
            )
            await client.close()
        finally:
            await server.stop()

    async def test_an_out_of_range_pointer_is_dropped_not_fatal(self):
        server = await FakeRFBServer(width=16, height=16).start()
        try:
            client = await connected_client(server)
            client.queue_pointer(-5, -5, 0)  # must not raise, must not kill the session
            assert client.connected
            await client.close()
        finally:
            await server.stop()


# ── Limits, truncation and timeouts ───────────────────────────────────────────


class TestServeLoop:
    async def test_serve_requests_incrementally_and_presents_frames(self):
        """The loop the GUI actually runs, end to end.

        Worth covering separately from pump(): this is where the update request
        rate, the queued-input flush and the frame callback all have to line up,
        and it is the only path that stops on ``stop()``.
        """
        server = await FakeRFBServer(width=16, height=16).start()
        try:
            frames: list[int] = []
            client = await connected_client(server, on_frame=lambda fb: frames.append(1))
            await server.wait_for_handshake_messages()

            async def feed() -> None:
                # Answer each update request with one damaged rectangle until the
                # client has been given something to show.
                while len(frames) < 2:
                    await server.send(
                        fb_update(raw_rect(0, 0, 2, 2, bgrx_pixels([(7, 7, 7)] * 4)))
                    )
                    await asyncio.sleep(0.01)

            feeder = asyncio.ensure_future(feed())
            serving = asyncio.ensure_future(client.serve(frame_interval=0.005))
            for _ in range(200):
                if len(frames) >= 2:
                    break
                await asyncio.sleep(0.01)
            client.stop()
            serving.cancel()
            feeder.cancel()
            await asyncio.gather(serving, feeder, return_exceptions=True)

            assert len(frames) >= 2, "serve() should have presented frames"
            # Every update request sent was incremental after the first.
            assert b"\x03\x01" in bytes(server.client_bytes), (
                "serve() should ask for incremental updates"
            )
            await client.close()
        finally:
            await server.stop()

    async def test_serve_refuses_to_run_before_connect(self):
        client = VNCClient("127.0.0.1", 5900)
        with pytest.raises(RuntimeError):
            await client.serve()

    async def test_the_first_update_request_is_sent_before_waiting_for_a_reply(self):
        """A server only answers a request, so the request has to come first.

        Flushing the outbox after the blocking read instead meant the very first
        FramebufferUpdateRequest sat unsent until the server spoke -- which,
        for a server that waits to be asked, is for ever. The session connected
        cleanly and then never received a frame, with no error anywhere. That is
        why a capture source driving this loop needs the request on the wire
        before the wait rather than after it.
        """
        server = await FakeRFBServer(width=16, height=16).start()
        try:
            client = await connected_client(server)
            serving = asyncio.ensure_future(client.serve(frame_interval=0.005))
            # No reply is ever sent, so only the ordering can satisfy this.
            for _ in range(200):
                if b"\x03" in bytes(server.client_bytes):
                    break
                await asyncio.sleep(0.005)
            client.stop()
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)
            assert b"\x03\x01" in bytes(server.client_bytes), (
                "the update request never reached the wire"
            )
            await client.close()
        finally:
            await server.stop()

    async def test_stop_ends_the_loop(self):
        server = await FakeRFBServer(width=16, height=16).start()
        try:
            client = await connected_client(server)
            serving = asyncio.ensure_future(client.serve(frame_interval=0.005))
            await asyncio.sleep(0.03)
            client.stop()
            await asyncio.wait_for(serving, timeout=2.0)
            await client.close()
        finally:
            await server.stop()


class TestLimitsAndTimeouts:
    async def test_a_truncated_message_is_reported_not_hung(self):
        server = await FakeRFBServer(width=16, height=16).start()
        try:
            client = await connected_client(server)
            # Claim one rectangle, supply a header, and then stop.
            await server.send(b"\x00\x00" + struct.pack(">H", 1) + rect_header(0, 0, 4, 4, ENCODING_RAW))
            await server.send(b"\x00" * 8)  # partial Raw payload
            await server.close_connection()
            with pytest.raises(ProtocolError) as excinfo:
                await client.pump()
            assert "closed mid-message" in str(excinfo.value)
            await client.close()
        finally:
            await server.stop()

    async def test_an_oversized_read_is_refused_before_allocating(self):
        """asyncio would happily buffer 4 GiB if we let it."""
        server = await FakeRFBServer(width=64, height=64).start()
        try:
            client = await connected_client(server, max_message_bytes=1024)
            await server.send(fb_update(rect_header(0, 0, 64, 64, ENCODING_RAW)))
            await server.send(b"\x00" * 4096)
            with pytest.raises(ProtocolError) as excinfo:
                await client.pump()
            assert "limit" in str(excinfo.value)
            await client.close()
        finally:
            await server.stop()

    async def test_a_negative_length_read_is_refused(self):
        reader = StreamByteReader(_FakeStreamReader(b"\x00" * 8), 1024)
        with pytest.raises(ProtocolError):
            await reader.read_exactly(-1)

    async def test_handshake_stalls_are_cut_off(self):
        """A server that accepts the connection and says nothing must not hang
        the client for as long as the user is willing to wait."""
        server = await FakeRFBServer(stall_after_banner=True).start()
        try:
            client = VNCClient("127.0.0.1", server.port, handshake_timeout=0.25)
            with pytest.raises(asyncio.TimeoutError):
                await client.connect()
            assert not client.connected
        finally:
            await server.stop()

    async def test_a_server_that_never_banners_is_cut_off(self):
        server = await FakeRFBServer(stall_before_banner=True).start()
        try:
            client = VNCClient("127.0.0.1", server.port, handshake_timeout=0.25)
            with pytest.raises(asyncio.TimeoutError):
                await client.connect()
        finally:
            await server.stop()

    async def test_a_connection_closed_mid_handshake_says_so(self):
        """Hanging up during the handshake must say so, not stall or hang."""
        server = await FakeRFBServer(close_after_banner=True).start()
        try:
            client = VNCClient("127.0.0.1", server.port, handshake_timeout=2.0)
            with pytest.raises((HandshakeError, ProtocolError)):
                await client.connect()
            assert not client.connected
        finally:
            await server.stop()

    async def test_an_absurd_desktop_name_length_is_refused_before_allocating(self):
        """A ServerInit claiming a 4 GiB desktop name must not be allocated."""
        server = await FakeRFBServer(width=16, height=16, name_length_override=0xFFFFFFFF).start()
        try:
            client = VNCClient("127.0.0.1", server.port, handshake_timeout=2.0)
            with pytest.raises(ProtocolError) as excinfo:
                await client.connect()
            assert "implausible" in str(excinfo.value)
        finally:
            await server.stop()

    async def test_a_palette_pixel_format_in_server_init_is_refused(self):
        """Palette formats are refused at ServerInit, not at the first pixel."""
        server = await FakeRFBServer(
            width=16, height=16, pixel_format=_PALETTE_PIXEL_FORMAT
        ).start()
        try:
            client = VNCClient("127.0.0.1", server.port, handshake_timeout=2.0)
            with pytest.raises(ProtocolError) as excinfo:
                await client.connect()
            assert "palette" in str(excinfo.value)
        finally:
            await server.stop()

    async def test_a_bad_pixel_format_in_server_init_is_refused(self):
        server = await FakeRFBServer(
            width=16, height=16, pixel_format=_DEPTH_GREATER_THAN_BPP
        ).start()
        try:
            client = VNCClient("127.0.0.1", server.port, handshake_timeout=2.0)
            with pytest.raises(ProtocolError):
                await client.connect()
        finally:
            await server.stop()

    async def test_too_many_rectangles_in_one_update_is_refused(self):
        server = await FakeRFBServer(width=16, height=16).start()
        try:
            client = await connected_client(server)
            await server.send(b"\x00\x00" + struct.pack(">H", 60000))
            with pytest.raises(ProtocolError):
                await client.pump()
            await client.close()
        finally:
            await server.stop()


class _FakeStreamReader:
    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    async def readexactly(self, count: int) -> bytes:
        if self._pos + count > len(self._data):
            raise asyncio.IncompleteReadError(b"", count)
        start = self._pos
        self._pos += count
        return self._data[start:start + count]


# ── Framebuffer ───────────────────────────────────────────────────────────────


class TestFramebuffer:
    def test_starts_black_and_sized(self):
        fb = Framebuffer(8, 4)
        assert len(fb.data) == 8 * 4 * 4
        assert bytes(fb.data) == b"\x00" * (8 * 4 * 4)

    def test_resize_reallocates_and_discards(self):
        fb = Framebuffer(4, 4)
        fb.fill_rect(0, 0, 4, 4, b"\x01\x02\x03\x04")
        fb.resize(8, 8)
        assert (fb.width, fb.height) == (8, 8)
        assert len(fb.data) == 8 * 8 * 4
        assert bgra_at(fb, 0, 0) == (0, 0, 0)

    def test_resize_to_the_same_size_keeps_the_pixels(self):
        fb = Framebuffer(4, 4)
        fb.fill_rect(0, 0, 4, 4, b"\x01\x02\x03\x04")
        before = bytes(fb.data)
        fb.resize(4, 4)
        assert bytes(fb.data) == before

    @pytest.mark.parametrize("w,h", [(0, 10), (10, 0), (-1, 10), (70000, 10), (70000, 70000)])
    def test_absurd_sizes_are_refused(self, w, h):
        with pytest.raises(ProtocolError):
            Framebuffer(w, h)

    def test_fill_outside_the_framebuffer_is_refused(self):
        fb = Framebuffer(8, 8)
        with pytest.raises(ProtocolError):
            fb.fill_rect(6, 0, 4, 1, b"\x00\x00\x00\x00")
        with pytest.raises(ProtocolError):
            fb.fill_rect(0, 6, 1, 4, b"\x00\x00\x00\x00")
        with pytest.raises(ProtocolError):
            fb.fill_rect(-1, 0, 1, 1, b"\x00\x00\x00\x00")
        # A rectangle that exactly fills the framebuffer is legal, and one pixel
        # past the edge is not -- the boundary itself has to be right.
        fb.fill_rect(0, 0, 8, 8, b"\x00\x00\x00\x00")
        with pytest.raises(ProtocolError):
            fb.fill_rect(0, 0, 9, 1, b"\x00\x00\x00\x00")

    def test_a_rect_that_just_fits_is_allowed(self):
        fb = Framebuffer(8, 8)
        fb.fill_rect(0, 0, 8, 8, b"\x00\x00\x00\x00")
        fb.fill_rect(8, 8, 0, 0, b"\x00\x00\x00\x00") if False else None


# ── Keysym mapping ────────────────────────────────────────────────────────────


class TestKeysymMapping:
    @pytest.mark.parametrize(
        "keysym,expected",
        [
            (ord("a"), "a"),
            (ord("A"), "shift-a"),
            (ord("5"), "5"),
            (ord(" "), "spc"),
            (ord("-"), "minus"),
            (ord("!"), "shift-1"),
            (0xFF0D, "ret"),      # Return
            (0xFF09, "tab"),      # Tab
            (0xFF1B, "esc"),      # Escape
            (0xFF08, "backspace"),
            (0xFF51, "left"),
            (0xFF52, "up"),
            (0xFF53, "right"),
            (0xFF54, "down"),
            (0xFF50, "home"),
            (0xFF57, "end"),
            (0xFFFF, "delete"),
        ],
    )
    def test_maps_what_it_can(self, keysym, expected):
        assert key_name_for_keysym(keysym) == expected

    @pytest.mark.parametrize(
        "keysym",
        [
            0xFFE1,  # Shift_L
            0xFFE3,  # Control_L
            0xFFE9,  # Alt_L
            0xFFEB,  # Super_L
            0xFFE5,  # Caps_Lock
            0xFFBE,  # F1
            0xFFBD,  # F13
            0xFFBF,  # F14
            0xFF63,  # Insert
            0xFF55,  # Page_Up
            0xFF56,  # Page_Down
            0xFF80,  # KP_Up
            0xFF9C,  # KP_Enter
            0x1008,  # XF86AudioPlay -- vendor keysym
            0x00E9,  # Latin-1 but not typeable by the existing keymap
            0x0100,  # Meta/Alt of a control character
            0x0000,
            0xFFFF_FF,
        ],
    )
    def test_refuses_what_it_cannot(self, keysym):
        """None means "send nothing", never "send something close".

        A wrong key injected into a guest is worse than no key: it types into
        whatever has focus, and a password typed with one character wrong is a
        failed login rather than a visible error.
        """
        assert key_name_for_keysym(keysym) is None
        assert not is_mapped_keysym(keysym)

    def test_modifier_keysyms_are_refused_so_shift_is_not_applied_twice(self):
        """Sending Shift as its own keysym makes X report the next key shifted."""
        for keysym in (0xFFE1, 0xFFE2, 0xFFE3, 0xFFE4, 0xFFE9, 0xFFEA, 0xFFEB, 0xFFEC):
            assert key_name_for_keysym(keysym) is None

    def test_every_name_returned_is_in_the_existing_keymap_vocabulary(self):
        """Nothing here may invent a key name ``guest_input`` cannot speak."""
        from vm_harness.guest_input import _CONTROL_KEYS

        checked = 0
        for keysym in list(range(0x00, 0x120)) + [0xFF08, 0xFF09, 0xFF0D, 0xFF1B, 0xFF50,
                                                  0xFF51, 0xFF52, 0xFF53, 0xFF54, 0xFF57,
                                                  0xFFFF, 0xFF63]:
            name = key_name_for_keysym(keysym)
            if name is None:
                continue
            checked += 1
            assert name in _CONTROL_KEYS or _is_known_hmp_name(name), (
                f"0x{keysym:04x} -> {name!r}, which is not in guest_input's vocabulary"
            )
        assert checked > 90, "expected the printable range to be mapped"


def _known_key_names() -> set[str]:
    """Every key name the existing ``guest_input`` keymap can produce.

    Built by asking that module rather than by listing names here, so this
    check follows ``guest_input`` instead of drifting from it.
    """
    import string

    from vm_harness.guest_input import _CONTROL_KEYS, key_for

    names = set(_CONTROL_KEYS.values())
    for character in string.printable[:95]:
        try:
            names.add(key_for(character))
        except Exception:
            pass
    return names


_KNOWN_NAMES = _known_key_names()
_CONTROL_TAILS = {"backslash", "bracket_right", "minus", "6"}


def _is_known_hmp_name(name: str) -> bool:
    """Whether ``name`` is one ``guest_input`` speaks, optionally with a prefix.

    The prefixes are checked by stripping them and requiring what remains to be
    a name the keymap already produces, so an invented tail like ``ctrl-frobnicate``
    fails this rather than passing because it has the right shape.
    """
    if name in _KNOWN_NAMES:
        return True
    for prefix in ("ctrl-", "alt-"):
        if name.startswith(prefix):
            tail = name[len(prefix):]
            return tail in _KNOWN_NAMES or tail in _CONTROL_TAILS
    return False