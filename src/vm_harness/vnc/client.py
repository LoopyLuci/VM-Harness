"""An asyncio RFB client: connect, authenticate, decode, present frames.

The point of using RFB instead of polling ``screendump`` is that the server
decides what changed. This client therefore does two things a screendump poll
cannot: it asks for *incremental* updates so an idle desktop costs nothing, and
it decodes the rectangle encodings the server chose so that what crosses the
wire is the damaged region rather than the whole screen.

What this does not do is make the guest's framebuffer update faster. The guest
still redraws at whatever rate the guest redraws; a CPU-bound guest still
produces one frame per redraw. The win is bandwidth and decode cost, and it is
worth being precise about that, because "VNC is faster" invites the wrong
expectation.

## Frame presentation

The renderer wants whole frames; the wire delivers rectangles, possibly split
across several messages. :meth:`VNCClient.pump` completes an update and calls
``on_frame`` once per update, after the last of its rectangles, which is what
makes an incremental update show up as a frame that changed rather than a
screen that redrew.

## Blocking

Nothing in the receive path awaits a lock, a callback or another task, so a
renderer that is slow to run cannot stall the decoder and back the socket up.
Input methods (:meth:`pointer`, :meth:`key`) queue bytes, and the receive loop
flushes them. That is a deliberate trade: a keystroke waits for one loop
iteration, which is microseconds, in exchange for never having two coroutines
write to the same socket.

## Errors

Every length read from the wire is bounded before it is allocated or indexed
(see ``MAX_*`` in :mod:`vm_harness.vnc.proto`). A malformed message raises
:class:`ProtocolError`; because the stream position is then unknown, that ends
the session rather than being retried. A session that cannot be trusted cannot
be resynchronised, and RFB has no resynchronisation message.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional, Sequence, Union

from loguru import logger

from vm_harness.vnc.proto import (
    ADVERTISED_ENCODINGS,
    AuthError,
    ENCODING_DESKTOP_SIZE,
    SUPPORTED_ENCODINGS,
    SERVER_BELL,
    SERVER_FRAMEBUFFER_UPDATE,
    SERVER_SERVER_CUT_TEXT,
    SERVER_SET_COLOUR_MAP_ENTRIES,
    ByteReader,
    Framebuffer,
    HandshakeError,
    HIGHEST_SUPPORTED_VERSION,
    PixelFormat,
    ProtocolError,
    Rectangle,
    SECURITY_NONE,
    SECURITY_VNC_AUTH,
    SECURITY_PREFERENCE,
    ServerInit,
    VNC_AUTH_CHALLENGE_LEN,
    VERSION_3_8,
    VERSION_WITH_SECURITY_LIST,
    check_desktop_size,
    check_fb_update_padding,
    choose_security_type,
    decode_rectangle,
    decode_set_colour_map_entries,
    encode_fb_update_request,
    encode_key_event,
    encode_pointer_event,
    encode_security_choice,
    encode_client_init,
    encode_set_encodings,
    encode_set_pixel_format,
    encode_version,
    parse_security_offer,
    parse_version,
    read_server_cut_text,
    version_text,
    vnc_auth_response,
)

#: A whole negotiation must not hang forever. Generous, because a loaded host
#: starting a VM can be slow, but bounded: without this a server that accepts
#: the connection and then says nothing leaves a task pending for as long as the
#: user is willing to wait, which is indistinguishable from a hang.
DEFAULT_HANDSHAKE_TIMEOUT = 10.0

#: Nothing in a handshake is close to this. It exists so a peer claiming an
#: enormous version banner, security list or desktop name cannot make the client
#: buffer without limit before the size checks get a chance to run.
MAX_HANDSHAKE_FIELD_BYTES = 64 * 1024

#: Bounds one read. A Raw rectangle at 4K is 33 MiB, so this is generous for
#: anything real and small enough that a bogus length cannot exhaust memory.
DEFAULT_MAX_MESSAGE_BYTES = 256 * 1024 * 1024

#: A rect count beyond this is not a screen's worth of damage.
MAX_RECTANGLES_PER_UPDATE = 4096

#: How often to ask "has anything changed?". This is an update *request* rate,
#: not a capture rate: the server replies only when it has damage to send, so a
#: higher number costs a little idle CPU and buys nothing.
DEFAULT_FRAME_REQUEST_INTERVAL = 0.05

FrameCallback = Callable[[Framebuffer], Union[Awaitable[None], None]]


class StreamByteReader:
    """:class:`vm_harness.vnc.proto.AsyncByteReader` over an asyncio stream.

    The byte budget is the important part. asyncio's own ``readexactly`` will
    buffer whatever it is asked for, so a peer claiming a four-byte length of
    4 GB becomes a 4 GB allocation unless the limit is applied here.
    """

    def __init__(self, reader: asyncio.StreamReader, max_message_bytes: int):
        self._reader = reader
        self._max = max_message_bytes

    async def read_exactly(self, count: int) -> bytes:
        if count < 0:
            raise ProtocolError(f"cannot read {count} bytes")
        if count > self._max:
            raise ProtocolError(f"message claims {count} bytes, over the {self._max} byte limit")
        try:
            return await self._reader.readexactly(count)
        except asyncio.IncompleteReadError as exc:
            raise ProtocolError(
                f"connection closed mid-message: wanted {count} bytes, got {len(exc.partial)}"
            ) from exc

    async def read_u8(self) -> int:
        return (await self.read_exactly(1))[0]

    async def read_u16(self) -> int:
        return int.from_bytes(await self.read_exactly(2), "big")

    async def read_i32(self) -> int:
        return int.from_bytes(await self.read_exactly(4), "big", signed=True)

    async def read_u32(self) -> int:
        return int.from_bytes(await self.read_exactly(4), "big")


class VNCClient:
    """An RFB client bound to one connection.

    Call :meth:`connect`, then :meth:`serve` from one task. ``serve`` returns on
    a clean close and raises on a protocol or authentication failure.
    """

    def __init__(
        self,
        host: str,
        port: int = 5900,
        *,
        password: Optional[str] = None,
        pixel_format: Optional[PixelFormat] = None,
        # What goes on the wire is what this client can actually decode, and it is
        # deliberately narrower than the decoder's full repertoire: see
        # ADVERTISED_ENCODINGS for why Tight is decoded but not offered.
        encodings: Sequence[int] = ADVERTISED_ENCODINGS,
        handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        on_frame: Optional[FrameCallback] = None,
        on_bell: Optional[Callable[[], None]] = None,
        on_cut_text: Optional[Callable[[str], None]] = None,
        log: object = logger,
    ):
        self.host = host
        self.port = port
        self._password = password
        self._pixel_format = pixel_format
        self._encodings = tuple(encodings)
        self._handshake_timeout = handshake_timeout
        self._max_message_bytes = max_message_bytes
        self.on_frame = on_frame
        self.on_bell = on_bell
        self.on_cut_text = on_cut_text
        self._log = log

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._source: Optional[StreamByteReader] = None

        self.framebuffer: Optional[Framebuffer] = None
        self.server_init: Optional[ServerInit] = None
        self.desktop_name = ""
        self.connected = False

        self._version = HIGHEST_SUPPORTED_VERSION
        self._outbox = bytearray()
        self._pending_resize: Optional[tuple[int, int]] = None
        #: Set when the framebuffer's contents are stale and a full, non
        #: incremental update must be requested to replace them.
        self._needs_full_refresh = False
        self._stop = False
        self._stalled = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> ServerInit:
        """Open the connection and complete the whole handshake.

        Every phase runs under one timeout. A failure at any phase closes the
        socket on the way out, so a caller that retries does not leak a
        half-open connection.
        """
        try:
            return await asyncio.wait_for(self._handshake(), self._handshake_timeout)
        except BaseException:
            await self.close()
            raise

    async def _handshake(self) -> ServerInit:
        self._log.info("connecting to RFB server {}:{}", self.host, self.port)
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        self._source = StreamByteReader(self._reader, self._max_message_bytes)

        await self._negotiate_version()
        security_type = await self._negotiate_security()
        await self._authenticate(security_type)
        # ClientInit before ServerInit, not after.
        #
        # QEMU's VNC server sends SecurityResult and then waits: it does not
        # write ServerInit until it has read ClientInit. Waiting for ServerInit
        # first -- what the spec's order implies -- is a handshake in which
        # neither side ever speaks again, so it looks exactly like a dead
        # console. Verified against QEMU 11.1.
        #
        # A server that follows the spec is unaffected: it writes ServerInit
        # when it gets there regardless, and reads this same byte from the
        # stream. Nothing here depends on the two crossing in a particular
        # direction, because ClientInit only ever travels client to server.
        await self._write_now(encode_client_init(shared=True))
        server_init = await self._read_server_init()
        self.server_init = server_init
        self.desktop_name = server_init.name
        self.framebuffer = Framebuffer(server_init.width, server_init.height)

        # Ask for our own pixel format only if the server's is not already one
        # this client decodes fastest. Otherwise let it choose: it has already
        # said what it produces, and overriding costs a full refresh.
        if self._pixel_format is not None and server_init.pixel_format != self._pixel_format:
            await self.set_pixel_format(self._pixel_format)

        self._writer.write(encode_set_encodings((*self._encodings, ENCODING_DESKTOP_SIZE)))
        await self._writer.drain()
        self.connected = True
        self._log.info(
            "RFB connected to {!r}: {}x{}, {}-bit, encodings={}",
            server_init.name,
            server_init.width,
            server_init.height,
            server_init.pixel_format.bits_per_pixel,
            [e for e in self._encodings],
        )
        return server_init

    async def _negotiate_version(self) -> None:
        banner = await self._read_bounded(12)
        server_version = parse_version(banner)
        self._log.debug("server offered RFB {}", version_text(server_version))
        # Always answer with our own highest supported version.
        self._version = HIGHEST_SUPPORTED_VERSION
        assert self._writer is not None
        self._writer.write(encode_version(self._version))
        await self._writer.drain()

    async def _negotiate_security(self) -> int:
        """The server offers a list; the client answers with one byte.

        Sending our own list here is the well-known way to desynchronise a
        3.7/3.8 handshake, because the server is not reading one.
        """
        offered = await self._read_security_offer()
        self._log.debug("server offered security types {}", offered)
        security_type = choose_security_type(offered, SECURITY_PREFERENCE)
        assert self._writer is not None
        self._writer.write(encode_security_choice(security_type))
        await self._writer.drain()
        return security_type

    async def _read_security_offer(self) -> list[int]:
        """Read the variable-length security-type offer.

        The framing differs between 3.3 (one u32) and 3.7+ (a u8 count then that
        many bytes), so the version is needed to know how much to read before
        handing the bytes to ``proto`` to parse.
        """
        assert self._reader is not None
        if self._version >= VERSION_WITH_SECURITY_LIST:
            head = await self._read_bounded(1)
            count = head[0]
            body = await self._read_bounded(count) if count else b""
            return parse_security_offer(ByteReader(head + body), self._version)
        head = await self._read_bounded(4)
        return parse_security_offer(ByteReader(head), self._version)

    async def _authenticate(self, security_type: int) -> None:
        if security_type == SECURITY_NONE:
            self._log.info("security type None: no authentication")
        elif security_type == SECURITY_VNC_AUTH:
            if not self._password:
                raise AuthError("server requires VNC Auth but no password was configured")
            challenge = await self._read_bounded(VNC_AUTH_CHALLENGE_LEN)
            assert self._writer is not None
            self._writer.write(vnc_auth_response(self._password, challenge))
            await self._writer.drain()
            self._log.info("answered the VNC Auth challenge")
        else:  # pragma: no cover - choose_security_type cannot return these
            raise HandshakeError(f"no authentication is implemented for security type {security_type}")
        await self._read_security_result()

    async def _read_security_result(self) -> None:
        """Read SecurityResult, and the reason string if there is one.

        The reason has to be read from the connection rather than from the four
        bytes of the verdict: it follows in the stream, and a reader bounded to
        those four bytes can only ever report "<no reason given>" -- which is
        the one case where the user most needs to be told why.
        """
        assert self._source is not None
        if await self._source.read_u32() == 0:
            return
        reason = ""
        if self._version >= VERSION_3_8:
            # The reason is required by 3.8, but a peer that omits it should not
            # turn a clear "wrong password" into an obscure protocol error --
            # and we already know authentication failed either way.
            try:
                length = await self._source.read_u32()
                if length <= MAX_HANDSHAKE_FIELD_BYTES:
                    reason = (
                        await self._source.read_exactly(length)
                    ).decode("utf-8", errors="replace")
            except ProtocolError:
                reason = ""
        raise AuthError(reason or "the server rejected authentication (it sent no reason)")

    async def _read_server_init(self) -> ServerInit:
        width = int.from_bytes(await self._read_bounded(2), "big")
        height = int.from_bytes(await self._read_bounded(2), "big")
        check_desktop_size(width, height)
        pixel_format = PixelFormat.decode(await self._read_bounded(16))
        name_length = int.from_bytes(await self._read_bounded(4), "big")
        name = (await self._read_bounded(name_length)).decode("latin-1")
        return ServerInit(width, height, pixel_format, name)

    async def _read_bounded(self, count: int) -> bytes:
        """Read exactly ``count`` bytes during the handshake, bounded."""
        if count > MAX_HANDSHAKE_FIELD_BYTES:
            raise ProtocolError(f"handshake field of {count} bytes is implausible")
        assert self._reader is not None
        try:
            return await self._reader.readexactly(count)
        except asyncio.IncompleteReadError as exc:
            raise HandshakeError(
                f"server closed the connection during the handshake "
                f"(wanted {count} bytes, got {len(exc.partial)})"
            ) from exc

    async def _write_now(self, payload: bytes) -> None:
        """Write handshake bytes straight out.

        During the handshake there is no receive loop to batch through, so the
        outbox is bypassed: a handshake message that sits in a buffer waiting for
        a loop that has not started yet is a handshake that never completes.
        """
        assert self._writer is not None
        self._writer.write(payload)
        await self._writer.drain()

    # ── Receive loop ──────────────────────────────────────────────────────────

    async def serve(self, frame_interval: float = DEFAULT_FRAME_REQUEST_INTERVAL) -> None:
        """Request frames and dispatch them until the connection closes.

        This is the only task that touches the receive path, and it is the task
        whose lifetime the caller owns. It returns on a clean close and raises
        on a protocol or authentication failure.
        """
        if not self.connected:
            raise RuntimeError("serve() called before connect()")
        interval = max(0.005, frame_interval)
        while not self._stop:
            await self.request_update(incremental=not self._needs_full_refresh)
            self._needs_full_refresh = False
            # The request must be on the wire *before* the wait, not after it.
            # An RFB server only ever sends an update in answer to a
            # FramebufferUpdateRequest, and pump() below blocks until it sends
            # one -- so flushing on the way out meant the first request sat in
            # the outbox for ever, an idle guest never answered it, and the
            # client sat waiting for a message that could not come. Nothing
            # timed out and nothing was logged: a console that connects and then
            # never shows a frame.
            await self._flush_outbox()
            await asyncio.sleep(interval)
            try:
                await self.pump()
            except ProtocolError:
                # A stop() closes the transport, so the receive this was waiting
                # in raises rather than hanging. That is the normal way out and
                # not an error; anything else still propagates.
                if self._stop:
                    break
                raise
            # Anything queued while the receive was in flight -- a keystroke, a
            # pointer move -- goes out now rather than at the next tick.
            await self._flush_outbox()

    async def pump(self) -> int:
        """Read what the server has sent, dispatching each complete frame.

        Returns the number of frames presented. Reads exactly one message and
        never waits for more than that, so a silent server cannot stop queued
        input from being flushed.
        """
        if self._source is None or not self.connected:
            return 0
        message_type = await self._source.read_u8()
        if message_type == SERVER_FRAMEBUFFER_UPDATE:
            return await self._handle_fb_update()
        if message_type == SERVER_SET_COLOUR_MAP_ENTRIES:
            await decode_set_colour_map_entries(self._source)
            return 0
        if message_type == SERVER_BELL:
            if self.on_bell is not None:
                self.on_bell()
            return 0
        if message_type == SERVER_SERVER_CUT_TEXT:
            text = await read_server_cut_text(self._source)
            if self.on_cut_text is not None:
                self.on_cut_text(text)
            return 0
        raise ProtocolError(f"unknown server message type {message_type}")

    async def _handle_fb_update(self) -> int:
        assert self._source is not None and self.framebuffer is not None
        check_fb_update_padding(await self._source.read_exactly(1))
        count = await self._source.read_u16()
        if count > MAX_RECTANGLES_PER_UPDATE:
            raise ProtocolError(f"FramebufferUpdate claims {count} rectangles")
        pixel_format = self._current_pixel_format()
        for _ in range(count):
            rect = Rectangle(
                await self._source.read_u16(),
                await self._source.read_u16(),
                await self._source.read_u16(),
                await self._source.read_u16(),
                await self._source.read_i32(),
            )
            resize = await decode_rectangle(self.framebuffer, rect, pixel_format, self._source)
            if resize is not None:
                self._pending_resize = (resize.width, resize.height)
        if self._pending_resize is not None:
            width, height = self._pending_resize
            self._pending_resize = None
            self.framebuffer.resize(width, height)
            # The renderer has a QImage wrapped around the old buffer, so a
            # resize must be presented even when no pixel changed.
            self._needs_full_refresh = True
        if count:
            await self._present_frame()
        return 1 if count else 0

    async def _present_frame(self) -> None:
        if self.on_frame is None or self.framebuffer is None:
            return
        result = self.on_frame(self.framebuffer)
        if asyncio.iscoroutine(result):
            await result

    # ── Output ────────────────────────────────────────────────────────────────

    async def _flush_outbox(self) -> None:
        if not self._outbox or self._writer is None:
            return
        payload = bytes(self._outbox)
        self._outbox.clear()
        self._writer.write(payload)
        await self._writer.drain()

    def queue(self, payload: bytes) -> None:
        """Queue client bytes for the next flush.

        Deliberately synchronous: the GUI's Qt event handlers must not block on
        the asyncio loop.
        """
        if len(payload) + len(self._outbox) > self._max_message_bytes:
            self._log.warning("dropping {} queued input bytes: outbox full", len(payload))
            return
        self._outbox += payload

    def queue_pointer(self, x: int, y: int, button_mask: int) -> None:
        """Queue a PointerEvent. Synchronous, for calling from a Qt event handler.

        A Qt event handler cannot await, and blocking the GUI thread on the
        asyncio loop would deadlock whenever the loop were already waiting on
        something. Queueing keeps the handler instant; the receive loop writes
        the bytes within one iteration.
        """
        try:
            self.queue(encode_pointer_event(x, y, button_mask))
        except ProtocolError:
            # A pointer position outside the framebuffer is a bug in the
            # caller's coordinate mapping, not a reason to drop the session.
            self._log.debug("dropping out-of-range pointer event ({}, {})", x, y)

    def queue_key(self, down: bool, keysym: int) -> None:
        """Queue a KeyEvent. Synchronous; see :meth:`queue_pointer`."""
        try:
            self.queue(encode_key_event(down, keysym))
        except ProtocolError:
            self._log.debug("dropping key event for out-of-range keysym {:#x}", keysym)

    async def pointer(self, x: int, y: int, button_mask: int) -> None:
        """Queue a PointerEvent: absolute position plus a button bitmask."""
        self.queue_pointer(x, y, button_mask)

    async def key(self, down: bool, keysym: int) -> None:
        """Queue a KeyEvent.

        The keysym is mapped to a guest key name by the caller; see
        :mod:`vm_harness.vnc.keysym`. This layer sends what it is given and
        performs no mapping of its own.
        """
        self.queue_key(down, keysym)

    async def request_update(self, incremental: bool) -> None:
        """Queue a FramebufferUpdateRequest."""
        self.queue(encode_fb_update_request(incremental))

    async def set_pixel_format(self, fmt: PixelFormat) -> None:
        """Switch pixel format mid-session.

        The existing framebuffer contents become meaningless -- they are encoded
        in the old format -- so the session is marked for a full refresh.
        Sending the message without doing that shows a screen of garbage until
        the next damage happens to cover everything.
        """
        if self._writer is None:
            raise RuntimeError("set_pixel_format() called before connect()")
        self._writer.write(encode_set_pixel_format(fmt))
        await self._writer.drain()
        self._pixel_format = fmt
        self._needs_full_refresh = True

    def _current_pixel_format(self) -> PixelFormat:
        if self._pixel_format is not None:
            return self._pixel_format
        assert self.server_init is not None
        return self.server_init.pixel_format

    # ── Teardown ──────────────────────────────────────────────────────────────

    def stop(self) -> None:
        """Ask :meth:`serve` to return.

        Closing the transport is what makes this prompt. ``serve`` spends most
        of its time blocked in a read waiting for the server to say something,
        and setting a flag alone would only take effect once that read
        completed -- which, for an idle desktop, is never. A GUI has to be able
        to disconnect a VM without waiting on it.

        The read then raises, which ``serve`` recognises as a stop rather than a
        protocol failure.
        """
        self._stop = True
        writer = self._writer
        if writer is not None:
            try:
                writer.close()
            except Exception:  # pragma: no cover - best effort
                pass

    async def close(self) -> None:
        self._stop = True
        self.connected = False
        writer, self._writer = self._writer, None
        self._source = None
        self._reader = None
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except (OSError, RuntimeError):  # pragma: no cover - best effort teardown
            pass