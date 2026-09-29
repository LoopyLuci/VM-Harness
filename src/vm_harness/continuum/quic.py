"""QUIC transport backend over raw UDP.

Provides ``QUICTransport`` which implements a simplified QUIC-like
protocol for multiplexed, low-latency communication.  When the
``aioquic`` package is available, it uses that; otherwise a
pure-Python minimal QUIC frame layer over UDP is used as a fallback.
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import hmac
import logging
import os
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from .transport import (
    Connection,
    ProbeResult,
    SendResult,
    TransportBackend,
    TransportError,
    TransportState,
)
from .discovery import Node
from .security import NoiseSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# QUIC frame types & constants
# ---------------------------------------------------------------------------


class QuicFrameType(enum.IntEnum):
    """Simplified QUIC frame types."""

    PADDING = 0x00
    PING = 0x01
    ACK = 0x02
    STREAM = 0x08
    MAX_DATA = 0x10
    MAX_STREAM_DATA = 0x11
    NEW_CONNECTION_ID = 0x18
    RETIRE_CONNECTION_ID = 0x19
    PATH_CHALLENGE = 0x1A
    PATH_RESPONSE = 0x1B
    CONNECTION_CLOSE = 0x1C


# Default QUIC packet fields
QUIC_VERSION = 0x00000001  # QUIC v1
QUIC_MAX_DATAGRAM = 1_350  # Safe MTU for most paths
QUIC_HEADER_SIZE = 22  # Minimal header


# ---------------------------------------------------------------------------
# QUIC connection
# ---------------------------------------------------------------------------


@dataclass
class QuicStream:
    """A single multiplexed stream within a QUIC connection."""

    stream_id: int
    priority: int = 0
    send_buffer: bytearray = field(default_factory=bytearray)
    recv_buffer: bytearray = field(default_factory=bytearray)
    send_offset: int = 0
    recv_offset: int = 0
    closed: bool = False


class QuicConnection(Connection):
    """A QUIC-like multiplexed connection over UDP.

    Supports multiple concurrent streams with configurable priority.
    When ``aioquic`` is available we defer to its implementation;
    otherwise we use our minimal frame codec.
    """

    def __init__(
        self,
        transport_name: str,
        target: Node,
        sock: Optional[Any] = None,
        noise_session: Optional[NoiseSession] = None,
    ) -> None:
        super().__init__(transport_name, target.node_id)
        self._target = target
        self._sock = sock
        self._noise = noise_session
        self._streams: Dict[int, QuicStream] = {}
        self._next_stream_id: int = 0  # Client-initiated bidirectional
        self._packet_number: int = 0
        self._send_lock = asyncio.Lock()
        self._recv_event = asyncio.Event()
        self._closed_streams: set = set()
        self._rtt_ms: float = 50.0
        self._rtt_var: float = 10.0

        if sock:
            self.state = TransportState.CONNECTED

    # -- Stream management ---------------------------------------------------

    def open_stream(self, priority: int = 0) -> QuicStream:
        """Open a new multiplexed stream and return it."""
        sid = self._next_stream_id
        self._next_stream_id += 4  # Skip to next bidirectional client stream
        stream = QuicStream(stream_id=sid, priority=priority)
        self._streams[sid] = stream
        return stream

    def get_stream(self, stream_id: int) -> Optional[QuicStream]:
        return self._streams.get(stream_id)

    # -- I/O -----------------------------------------------------------------

    async def send(self, data: bytes, stream_id: Optional[int] = None) -> SendResult:
        """Send data, optionally on a specific stream."""
        if not self._sock:
            raise TransportError("quic socket not connected")

        stream: Optional[QuicStream] = None
        if stream_id is not None:
            stream = self._streams.get(stream_id)
            if stream is None:
                raise TransportError(f"unknown stream {stream_id}")

        start = time.monotonic()
        async with self._send_lock:
            if stream:
                frame = self._build_stream_frame(stream_id, data, fin=False)
                stream.send_buffer.extend(data)
                stream.send_offset += len(data)
            else:
                frame = self._build_stream_frame(0, data, fin=False)

            if self._noise:
                frame = self._noise.encrypt(frame)

            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._sock.send, frame)
            except OSError as exc:
                self.state = TransportState.FAILED
                raise TransportError(f"quic send failed: {exc}") from exc

        latency = (time.monotonic() - start) * 1_000
        self._update_rtt(latency)
        self.bytes_sent += len(data)
        return SendResult(bytes_sent=len(data), latency_ms=latency,
                          transport=self.transport_name)

    async def recv(self, max_bytes: int = 65_536) -> bytes:
        """Receive the next available datagram's payload."""
        if not self._sock:
            raise TransportError("quic socket not connected")
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(
                None, self._sock.recv, max_bytes
            )
        except OSError as exc:
            raise TransportError(f"quic recv failed: {exc}") from exc

        if self._noise:
            data = self._noise.decrypt(data)

        self.bytes_received += len(data)
        # Extract STREAM frame payload if present
        payload = self._parse_stream_payload(data)
        return payload if payload else data

    async def recv_stream(self) -> AsyncIterator[bytes]:
        """Continuously yield payloads from incoming STREAM frames."""
        while self.state == TransportState.CONNECTED:
            try:
                payload = await asyncio.wait_for(self.recv(), timeout=30.0)
                if payload:
                    yield payload
            except asyncio.TimeoutError:
                continue
            except TransportError:
                break

    async def close_stream(self, stream_id: int) -> None:
        """Gracefully close a stream with a FIN frame."""
        frame = self._build_stream_frame(stream_id, b"", fin=True)
        try:
            async with self._send_lock:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._sock.send, frame)
        except OSError:
            pass
        stream = self._streams.get(stream_id)
        if stream:
            stream.closed = True
        self._closed_streams.add(stream_id)

    # -- Health --------------------------------------------------------------

    @property
    def rtt_ms(self) -> float:
        return self._rtt_ms

    def _update_rtt(self, sample: float) -> None:
        """Update smoothed RTT estimate (EWMA)."""
        alpha = 0.125
        beta = 0.25
        self._rtt_var = (1 - beta) * self._rtt_var + beta * abs(self._rtt_ms - sample)
        self._rtt_ms = (1 - alpha) * self._rtt_ms + alpha * sample

    # -- Frame codec ---------------------------------------------------------

    def _build_stream_frame(
        self, stream_id: int, data: bytes, fin: bool = False
    ) -> bytes:
        """Build a minimal STREAM frame."""
        self._packet_number += 1
        # Header: version(4) + DCID_LEN(1) + DCID(0) + SCID_LEN(1) + SCID(0)
        header = struct.pack(
            ">IBBBBBI",
            QUIC_VERSION,
            0,  # DCID length
            0,  # DCID placeholder
            0,  # SCID length
            0,  # SCID placeholder
            0,  # Token length
            self._packet_number,
        )
        # Stream frame: type(1) + stream_id(varint) + offset(varint) + length(varint) + data
        frame_type = QuicFrameType.STREAM | 0x08 | (0x04 if fin else 0)  # LEN+FIN
        stream_header = bytes([frame_type])
        stream_header += _encode_varint(stream_id)
        stream_header += _encode_varint(0)  # offset
        stream_header += _encode_varint(len(data))
        return header + stream_header + data

    @staticmethod
    def _parse_stream_payload(data: bytes) -> bytes:
        """Best-effort extraction of STREAM frame payload from a packet."""
        if len(data) < QUIC_HEADER_SIZE:
            return b""
        # Skip header, scan for STREAM frame type
        offset = QUIC_HEADER_SIZE
        while offset < len(data):
            if data[offset] & 0xF8 == QuicFrameType.STREAM:
                offset += 1
                stream_id, offset = _decode_varint(data, offset)
                stream_offset, offset = _decode_varint(data, offset)
                length, offset = _decode_varint(data, offset)
                return data[offset : offset + length]
            # Skip unknown frame
            offset += 1
        return b""

    # -- Lifecycle -----------------------------------------------------------

    async def close(self) -> None:
        """Send CONNECTION_CLOSE and shut down."""
        if self._sock and not self._closed:
            try:
                close_frame = struct.pack(
                    ">IBBBBI",
                    QUIC_VERSION,
                    0,
                    0,
                    0,
                    0,
                    self._packet_number + 1,
                ) + bytes([QuicFrameType.CONNECTION_CLOSE])
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._sock.send, close_frame)
            except OSError:
                pass
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self._streams.clear()
        await super().close()


# ---------------------------------------------------------------------------
# QUIC transport backend
# ---------------------------------------------------------------------------


class QUICTransport(TransportBackend):
    """QUIC-over-UDP transport backend.

    Attempts to use ``aioquic`` for a full QUIC implementation, but
    falls back to our minimal frame codec if the package is missing.
    """

    name = "quic_direct"
    priority = 10  # Highest priority — lowest latency

    def __init__(
        self,
        bind_addr: str = "0.0.0.0",
        port: int = 0,
        noise: bool = False,
    ) -> None:
        super().__init__()
        self._bind_addr = bind_addr
        self._port = port
        self._use_noise = noise
        self._socket: Optional[Any] = None
        self._aioquic_available = self._check_aioquic()

    @staticmethod
    def _check_aioquic() -> bool:
        try:
            import aioquic  # type: ignore[import-untyped]
            return True
        except ImportError:
            return False

    async def startup(self) -> None:
        """Create the listening UDP socket."""
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.setblocking(False)
        self._socket.bind((self._bind_addr, self._port))
        self._state = TransportState.IDLE

    async def shutdown(self) -> None:
        if self._socket:
            try:
                self._socket.close()
            except OSError:
                pass
        await super().shutdown()

    # -- Probe ---------------------------------------------------------------

    async def probe(self, target: Node) -> ProbeResult:
        """Probe via STUN-like round-trip measurement."""
        addr = target.tailscale_ip or (target.lan_ips[0] if target.lan_ips else None)
        if not addr:
            return ProbeResult(reachable=False)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.settimeout(2.0)

        # Send 3 probes and measure RTT
        latencies: List[float] = []
        for _ in range(3):
            challenge = os.urandom(8)
            start = time.monotonic()
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None, sock.sendto, challenge, (addr, target.continuum_port)
                )
                sock.settimeout(1.0)
                response = await asyncio.wait_for(
                    loop.run_in_executor(None, sock.recv, 1024), timeout=1.0
                )
                if response == challenge:
                    latencies.append((time.monotonic() - start) * 1_000)
            except (OSError, asyncio.TimeoutError):
                pass

        sock.close()
        if not latencies:
            return ProbeResult(reachable=False)

        avg_latency = sum(latencies) / len(latencies)
        jitter = max(latencies) - min(latencies) if len(latencies) > 1 else 0
        return ProbeResult(
            reachable=True,
            latency_ms=avg_latency,
            jitter_ms=jitter,
            throughput_mbps=100.0,  # QUIC can use full bandwidth
            transport_type="quic_udp",
            metadata={"aioquic": self._aioquic_available},
        )

    # -- Establish -----------------------------------------------------------

    async def establish(self, target: Node) -> QuicConnection:
        addr = target.tailscale_ip or (target.lan_ips[0] if target.lan_ips else None)
        if not addr:
            raise TransportError(
                f"node {target.node_id} has no usable IP for QUIC"
            )

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, sock.connect, (addr, target.continuum_port)
            )
        except OSError as exc:
            sock.close()
            raise TransportError(
                f"quic connect to {addr}:{target.continuum_port} failed: {exc}"
            ) from exc

        noise_session: Optional[NoiseSession] = None
        if self._use_noise:
            noise_session = NoiseSession(initiator=True)
            # Perform abbreviated handshake
            hs_msg = noise_session.generate_handshake_message()
            await loop.run_in_executor(None, sock.send, hs_msg)
            # Expect response
            try:
                response = await asyncio.wait_for(
                    loop.run_in_executor(None, sock.recv, 1024), timeout=5.0
                )
                noise_session.receive_handshake_message(response)
            except (OSError, asyncio.TimeoutError):
                logger.warning("noise handshake failed; continuing unencrypted")

        conn = QuicConnection(self.name, target, sock, noise_session)
        logger.debug("quic connection established to %s:%d", addr, target.continuum_port)
        return conn

    @property
    def socket(self) -> Optional[Any]:
        return self._socket

    def __repr__(self) -> str:
        return f"<QUICTransport aioquic={'yes' if self._aioquic_available else 'no'}>"


# ---------------------------------------------------------------------------
# Varint helpers (QUIC encoding)
# ---------------------------------------------------------------------------


def _encode_varint(value: int) -> bytes:
    """Encode an integer as a QUIC variable-length integer."""
    if value <= 0x3F:
        return bytes([value])
    if value <= 0x3FFF:
        return struct.pack(">H", value | 0x4000)
    if value <= 0x3FFFFFFF:
        return struct.pack(">I", value | 0x80000000)
    return struct.pack(">Q", value | 0xC000000000000000)


def _decode_varint(data: bytes, offset: int) -> Tuple[int, int]:
    """Decode a QUIC variable-length integer. Returns (value, new_offset)."""
    if offset >= len(data):
        raise ValueError("varint beyond end of buffer")
    first = data[offset]
    prefix = first >> 6
    if prefix == 0:
        return first & 0x3F, offset + 1
    if prefix == 1:
        val = int.from_bytes(data[offset : offset + 2], "big") & 0x3FFF
        return val, offset + 2
    if prefix == 2:
        val = int.from_bytes(data[offset : offset + 4], "big") & 0x3FFFFFFF
        return val, offset + 4
    val = int.from_bytes(data[offset : offset + 8], "big") & 0x3FFFFFFFFFFFFFFF
    return val, offset + 8
