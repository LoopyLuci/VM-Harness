"""RFB (VNC) client: protocol, connection, and keysym mapping.

This package is the client half of the RFB server in the Continuum transport.
Where the old path polled QMP ``screendump`` over a WebSocket and shipped a whole
JPEG frame per poll, this decodes rectangles: the server sends what changed and
the client inflates only that.

Layers, each usable on its own:

``proto``
    The wire format, with no sockets and no Qt. Every message can be built and
    taken apart from a byte string, which is how the tests drive it.
``client``
    The asyncio connection: handshake, VNC Auth, the receive loop, and input.
``keysym``
    X keysyms to the key names :mod:`vm_harness.guest_input` already speaks.

The renderer lives outside this package, in ``gui.widgets_vnc``, and depends on
Qt; nothing in here imports Qt, so the protocol stays testable headless.

A note on expectations: this reduces bandwidth and decode cost, not the rate at
which the guest draws. A guest that redraws at 15fps redraws at 15fps over RFB
too.
"""
from __future__ import annotations

from vm_harness.vnc.client import DEFAULT_FRAME_REQUEST_INTERVAL, VNCClient
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
    SUPPORTED_ENCODINGS,
    SUPPORTED_SECURITY_TYPES,
    AuthError,
    Framebuffer,
    HandshakeError,
    PixelFormat,
    ProtocolError,
    RFBError,
    ServerInit,
    UnsupportedEncoding,
)

__all__ = [
    "AuthError",
    "BGRX32",
    "DEFAULT_FRAME_REQUEST_INTERVAL",
    "ENCODING_COPY_RECT",
    "ENCODING_DESKTOP_SIZE",
    "ENCODING_HEXTILE",
    "ENCODING_RAW",
    "ENCODING_RRE",
    "ENCODING_TIGHT",
    "Framebuffer",
    "HandshakeError",
    "PixelFormat",
    "ProtocolError",
    "RGB555",
    "RFBError",
    "SUPPORTED_ENCODINGS",
    "SUPPORTED_SECURITY_TYPES",
    "ServerInit",
    "UnsupportedEncoding",
    "VNCClient",
    "is_mapped_keysym",
    "key_name_for_keysym",
]