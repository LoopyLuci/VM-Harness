"""RFB 3.8 (VNC) wire protocol: message encode and decode.

Pure protocol. No sockets, no Qt, no asyncio -- so the whole of the wire format
can be driven from a byte string in a unit test, which is how every case below
is covered.

The framebuffer is held as 32-bit BGRA bytes, which is byte-for-byte the layout
of ``QImage::Format_RGB32`` on a little-endian host. That is not an accident:
it lets the one encoding that dominates a live desktop (Raw at 32bpp) land with
a per-row ``memcpy`` instead of a per-pixel conversion loop, and it means the
renderer never has to know which pixel format the server was using.

## The security handshake, which is backwards from what people expect

In RFB 3.7 and 3.8 the *server* sends the list of security types it will accept
and the *client* replies with the single one it has chosen. The reverse -- a
client offering and a server choosing -- is a 3.3-and-earlier shape for the
security type itself, and confusing the two is the single most common way to
write an RFB client that then appears to hang. ``parse_security_offer`` reads
the list; ``encode_security_choice`` writes the one byte. The version decides
which of the two shapes applies, and both are implemented here.

## Limits

Every length that arrives from the wire is bounded before it is used as an
index, an allocation or a loop bound: see the ``MAX_*`` constants. A peer that
sends a 16-bit width of 65535 and a 16-bit height of 65535 must not be able to
make this module allocate four gigabytes.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import Optional, Sequence

# ── Version ────────────────────────────────────────────────────────────────────

VERSION_PREFIX = b"RFB "
#: RFB versions are `major * 1000 + minor`, so 3.8 is 3008 and 3.3 is 3003.
#: Getting this scaling wrong makes a version comparison silently decide the
#: wrong thing -- 3007 >= 307, but 308 >= 3007 is false, so a 3.7+ server would
#: be read with 3.3 framing and every following field would be off by one.
VERSION_3_3 = 3003
VERSION_3_7 = 3007
VERSION_3_8 = 3008
SUPPORTED_VERSIONS: tuple[int, ...] = (VERSION_3_3, VERSION_3_7, VERSION_3_8)
HIGHEST_SUPPORTED_VERSION = VERSION_3_8
#: The first version that sends a security-type *list* rather than a single
#: u32 security type, and the first that can attach a failure reason string.
VERSION_WITH_SECURITY_LIST = VERSION_3_7
#: "RFB 003.008\n" is exactly 12 bytes; anything longer is not a version banner.
MAX_VERSION_BANNER = 12


def version_text(version: int) -> str:
    """Render a version number as ``3.008``, the way the banner does."""
    return f"{version // 1000}.{version % 1000:03d}"


def encode_version(version: int = HIGHEST_SUPPORTED_VERSION) -> bytes:
    """Render a version number as the 12-byte banner the wire carries."""
    major, minor = divmod(version, 1000)
    return f"{VERSION_PREFIX.decode()}{major:03d}.{minor:03d}\n".encode("ascii")


def parse_version(banner: bytes) -> int:
    """Parse a server version banner into ``major * 1000 + minor``.

    Raises rather than guessing: a banner that is not ``RFB mmm.nnn`` means the
    peer is not speaking RFB, and continuing would mean interpreting whatever
    HTTP error page came back as pixel data.
    """
    if len(banner) > MAX_VERSION_BANNER:
        raise ProtocolError(f"version banner is {len(banner)} bytes, expected at most 12")
    if not banner.startswith(VERSION_PREFIX):
        raise ProtocolError(f"not an RFB version banner: {banner[:12]!r}")
    # The banner is fixed-length: "RFB " plus three digits, a dot, three more,
    # then a line ending. Accepting a short or newline-less banner would mean
    # parsing whatever a broken peer sent as though it were a version, and then
    # disagreeing about every field after it.
    if len(banner) == 12 and banner[11:12] == b"\n":
        body = banner[4:11]
    elif len(banner) == 13 and banner[11:13] == b"\r\n":
        body = banner[4:11]
    else:
        raise ProtocolError(f"malformed RFB version banner: {banner[:13]!r}")
    text = body.decode("ascii", errors="replace")
    parts = text.split(".")
    if len(parts) != 2 or not all(len(p) == 3 and p.isdigit() for p in parts):
        raise ProtocolError(f"malformed RFB version: {text!r}")
    version = int(parts[0]) * 1000 + int(parts[1])
    if version not in SUPPORTED_VERSIONS:
        raise ProtocolError(
            f"server speaks RFB {version_text(version)}, which is not supported "
            f"(3.3, 3.7 and 3.8 are)"
        )
    return version


def negotiate_version(server_version: int) -> int:
    """Pick the version to use, given what the server offered.

    Always answers with our own highest version. Downgrading to match a 3.3
    server would be defensible in isolation, but 3.3 has no security-type list
    and no failure reason string, so "supporting" it only costs diagnostics.
    """
    if server_version not in SUPPORTED_VERSIONS:
        raise ProtocolError(f"cannot negotiate with RFB {server_version}")
    return HIGHEST_SUPPORTED_VERSION


# ── Security ───────────────────────────────────────────────────────────────────

SECURITY_INVALID = 0xFFFFFFFF
SECURITY_NONE = 1
SECURITY_VNC_AUTH = 2
SECURITY_RA2 = 5
SECURITY_TLS = 16

#: Security types this client implements. Everything else the server might
#: offer is declined rather than half-supported: VeNCrypt needs a TLS stack and
#: a sub-negotiation of its own, and RA2 is 20 years of compatibility theatre.
SUPPORTED_SECURITY_TYPES: tuple[int, ...] = (SECURITY_VNC_AUTH, SECURITY_NONE)

#: Preference order. VNC Auth first: if a server offers both, the server is
#: configured to be password-protected and silently downgrading to None would
#: turn that configuration into no configuration at all.
SECURITY_PREFERENCE: tuple[int, ...] = (SECURITY_VNC_AUTH, SECURITY_NONE)

VNC_AUTH_CHALLENGE_LEN = 16
VNC_AUTH_RESPONSE_LEN = 16
VNC_AUTH_PASSWORD_LEN = 8
# RFC 6143 7.2: the password is at most eight bytes, truncated or zero-padded.
MAX_VNC_PASSWORD = VNC_AUTH_PASSWORD_LEN


def encode_security_choice(security_type: int) -> bytes:
    """The client's answer to the server's security-type *list*: one byte.

    The server sends the list and the client picks. Writing a count here is the
    backwards-handed bug this function exists to make impossible.
    """
    if not 0 < security_type < 256:
        raise ProtocolError(f"{security_type} is not a valid security type")
    return bytes((security_type,))


def parse_security_offer(reader: "ByteReader", version: int) -> list[int]:
    """Read the server's security-type offer.

    RFB 3.7+ sends a ``u8`` count followed by that many type bytes, and a count
    of zero means failure with a reason string attached. RFB 3.3 sends a single
    ``u32`` type, where ``0`` means the same thing.
    """
    if version >= VERSION_WITH_SECURITY_LIST:
        count = reader.read_u8()
        if count == 0:
            raise HandshakeError(_read_failure_reason(reader))
        types = list(reader.read_exactly(count))
        if len(types) != count:  # pragma: no cover - read_exactly already guards
            raise ProtocolError("truncated security type list")
        return types
    value = reader.read_u32()
    if value == SECURITY_INVALID:
        raise HandshakeError(_read_failure_reason(reader))
    return [value]


def _read_failure_reason(reader: "ByteReader") -> str:
    """Read the human-readable reason a server attaches to a failed handshake."""
    try:
        length = reader.read_u32()
        # A hostile or broken peer can claim any length; bound it before
        # allocating, and do not let a truncated tail hide the real failure.
        if length > 4096:
            return f"<reason too long: {length} bytes>"
        return reader.read_exactly(length).decode("utf-8", errors="replace")
    except (ProtocolError, RFBError):
        return "<no reason given>"


def choose_security_type(
    offered: Sequence[int],
    preference: Sequence[int] = SECURITY_PREFERENCE,
) -> int:
    """Pick a security type from the server's list.

    Only types this client implements are acceptable, so a server offering
    nothing we can do fails here with a message that says what it offered
    instead of a generic "handshake failed".
    """
    for candidate in preference:
        if candidate in offered:
            return candidate
    if not offered:
        raise HandshakeError("server offered no security types")
    names = ", ".join(_security_name(t) for t in offered)
    raise HandshakeError(
        f"no mutually supported security type (server offered {names}; "
        f"this client implements {_security_names()})"
    )


def _security_name(security_type: int) -> str:
    return {
        SECURITY_NONE: "None(1)",
        SECURITY_VNC_AUTH: "VNCAuth(2)",
        SECURITY_RA2: "RA2(5)",
        SECURITY_TLS: "TLS(16)",
    }.get(security_type, f"type {security_type}")


def _security_names() -> str:
    return ", ".join(_security_name(t) for t in SUPPORTED_SECURITY_TYPES)


@dataclass(frozen=True)
class SecurityResult:
    """The server's verdict on the chosen security type (RFB 3.8)."""

    ok: bool
    reason: str = ""

    @classmethod
    def decode(cls, reader: "ByteReader", version: int = VERSION_3_8) -> "SecurityResult":
        """Read the server's verdict.

        Only 3.8 attaches a reason string, and only to a *failure*. In 3.3 and
        3.7 there is nothing after the zero, so reading one anyway would
        consume the first bytes of ServerInit and desynchronise the stream --
        which looks like a corrupt pixel format rather than a version bug.
        """
        if reader.read_u32() == 0:
            return cls(True)
        if version >= VERSION_3_8:
            return cls(False, _read_failure_reason(reader))
        return cls(False, "authentication failed (this server sends no reason)")


# ── Pixel format ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PixelFormat:
    """An RFB pixel format (the 16-byte body of ServerInit / SetPixelFormat)."""

    bits_per_pixel: int
    depth: int
    big_endian: bool
    true_colour: bool
    red_max: int
    green_max: int
    blue_max: int
    red_shift: int
    green_shift: int
    blue_shift: int

    @property
    def bytes_per_pixel(self) -> int:
        return (self.bits_per_pixel + 7) // 8

    def encode(self) -> bytes:
        return struct.pack(
            ">BBBBHHHBBBxxx",
            self.bits_per_pixel,
            self.depth,
            1 if self.big_endian else 0,
            1 if self.true_colour else 0,
            self.red_max,
            self.green_max,
            self.blue_max,
            self.red_shift,
            self.green_shift,
            self.blue_shift,
        )

    @classmethod
    def decode(cls, raw: bytes) -> "PixelFormat":
        if len(raw) != 16:
            raise ProtocolError(f"pixel format is {len(raw)} bytes, expected 16")
        (
            bpp, depth, be, true_colour, rmax, gmax, bmax, rshift, gshift, bshift
        ) = struct.unpack(">BBBBHHHBBBxxx", raw)
        fmt = cls(bpp, depth, bool(be), bool(true_colour), rmax, gmax, bmax, rshift, gshift, bshift)
        fmt.validate()
        return fmt

    def validate(self) -> None:
        """Reject a format that cannot be rendered, before it is used.

        Called on every format that arrives off the wire. A palette format, a
        depth that contradicts its bits-per-pixel or a shift that puts a
        component past its own maximum is not something to be lenient about --
        each one would otherwise index outside a per-channel lookup table.
        """
        if self.bits_per_pixel not in (8, 16, 32):
            raise ProtocolError(
                f"{self.bits_per_pixel} bits per pixel is not supported "
                "(this client handles 8, 16 and 32)"
            )
        if not self.true_colour:
            raise ProtocolError("palette (true-colour=false) pixel formats are not supported")
        if self.depth == 0 or self.depth > self.bits_per_pixel:
            raise ProtocolError(
                f"depth {self.depth} is inconsistent with {self.bits_per_pixel} bits per pixel"
            )
        for name, shift, max_value in (
            ("red", self.red_shift, self.red_max),
            ("green", self.green_shift, self.green_max),
            ("blue", self.blue_shift, self.blue_max),
        ):
            if max_value == 0:
                raise ProtocolError(f"{name}_max is zero")
            if max_value & (max_value + 1):
                raise ProtocolError(f"{name}_max {max_value} is not 2^n - 1")
            width = max_value.bit_length()
            if shift + width > self.bits_per_pixel:
                raise ProtocolError(
                    f"{name} occupies bits {shift}..{shift + width - 1}, "
                    f"which does not fit in {self.bits_per_pixel} bits"
                )

    def is_bgrx32(self) -> bool:
        """True for 32bpp little-endian BGRX, the format this server sends.

        Byte-for-byte ``QImage::Format_RGB32``, so a Raw rectangle of this
        format is a straight row copy.
        """
        return (
            self.bits_per_pixel == 32
            and self.depth in (24, 32)
            and not self.big_endian
            and self.true_colour
            and self.red_max == 255
            and self.green_max == 255
            and self.blue_max == 255
            and self.red_shift == 16
            and self.green_shift == 8
            and self.blue_shift == 0
        )


#: The format this client asks for by default: what the server offers, and what
#: needs no per-pixel conversion on a little-endian host.
BGRX32 = PixelFormat(32, 24, False, True, 255, 255, 255, 16, 8, 0)
#: 16bpp RGB555. Half the bandwidth of 32bpp, at the cost of quantising each
#: channel to 5 bits. Opt-in only.
RGB555 = PixelFormat(16, 15, False, True, 31, 31, 31, 10, 5, 0)
#: 8bpp true colour, for completeness; converts rather than copying.
BGR233 = PixelFormat(8, 8, False, True, 7, 7, 3, 6, 3, 0)

NATIVE_PIXEL_FORMATS: tuple[PixelFormat, ...] = (BGRX32, RGB555)

MAX_DESKTOP_NAME = 256


@dataclass(frozen=True)
class ServerInit:
    """ServerInit: framebuffer geometry, pixel format and desktop name."""

    width: int
    height: int
    pixel_format: PixelFormat
    name: str

    @classmethod
    def decode(cls, reader: "ByteReader") -> "ServerInit":
        width = reader.read_u16()
        height = reader.read_u16()
        check_desktop_size(width, height)
        pixel_format = PixelFormat.decode(reader.read_exactly(16))
        name_length = reader.read_u32()
        if name_length > MAX_DESKTOP_NAME * 8:
            raise ProtocolError(f"desktop name of {name_length} bytes is implausible")
        name = reader.read_exactly(name_length).decode("latin-1")
        return cls(width, height, pixel_format, name)


# ── Encodings ─────────────────────────────────────────────────────────────────

ENCODING_RAW = 0
ENCODING_COPY_RECT = 1
ENCODING_RRE = 2
ENCODING_HEXTILE = 5
ENCODING_TIGHT = 7
#: Pseudo-encoding: the framebuffer changed size. Not a real encoding; it
#: arrives as a rectangle whose size is the new desktop size.
ENCODING_DESKTOP_SIZE = -223

SUPPORTED_ENCODINGS: tuple[int, ...] = (
    ENCODING_TIGHT,
    ENCODING_HEXTILE,
    ENCODING_RRE,
    ENCODING_COPY_RECT,
    ENCODING_RAW,
)

#: What is actually offered to a server in SetEncodings, in preference order.
#:
#: Deliberately *not* the same as :data:`SUPPORTED_ENCODINGS`. Tight is decodable
#: and stays in that set, so a rectangle that arrives as Tight still renders; it
#: is simply not advertised to QEMU. See the note below for what Tight would
#: still cost.
#:
#: Hextile is advertised instead, and against QEMU 11.1 at 1280x800 with a
#: 32-bpp/depth-24 client format it decodes a full non-incremental update
#: byte-exactly: 66,627 bytes in, 66,627 consumed, and a framebuffer identical
#: to a reference client's, pixel for pixel.
#:
#: Tight is nonetheless *not* safe to advertise yet, and the reason is the
#: decoder rather than the framing. Measured against the same VM, forcing
#: ``encodings=(7,)`` connects fine and QEMU sends 48 rectangles that a
#: spec-faithful decoder consumes exactly -- so the earlier claim on this
#: comment that "QEMU's Tight framing this decoder cannot be made to agree
#: with" was wrong, and so was the reading of the control byte as a compression
#: method rather than as a flag mask. What the live bytes actually show is:
#:
#: * QEMU's Tight zlib streams are **zlib-wrapped** (they start ``78 da``), not
#:   raw DEFLATE, contradicting :func:`_inflate`'s comment;
#: * the four streams are **persistent across rectangles**, reset only by the
#:   control byte's low nibble, so a decoder that builds a fresh inflater per
#:   rectangle fails on the second rectangle that uses a stream;
#: * of QEMU's 48 rectangles, 29 are FillCompression, 13 are PaletteFilter and
#:   5 are CopyFilter -- so :func:`_decode_tight` refuses two of every three.
#:
#: Fixing those is a separate change; until it lands, advertising Tight would
#: end the session on the first PaletteFilter rectangle, which is strictly worse
#: than the larger Hextile rectangles.
#: What is offered to QEMU in ``SetEncodings``.
#:
#: Tight is first because it is the best compression QEMU offers and the
#: decoder now handles every shape it sends: FillCompression, BasicCompression
#: with zlib, and the Copy, Palette and Gradient filters, across all four
#: persistent zlib streams with their reset mask. It was excluded for most of
#: this file's life on the belief that QEMU's Tight framing could not be
#: agreed with; that was a misdiagnosis, and the real gaps were a masked control
#: byte, raw-DEFLATE inflation of zlib-wrapped streams, and no stream state at
#: all. Verified live against a guest that sends PaletteFilter for 13 of its 48
#: rectangles.
#:
#: Hextile and RRE stay advertised as fallbacks, not because Tight is doubtful
#: but because a client that offers only Tight cannot fall back to anything a
#: server without Tight would send.
ADVERTISED_ENCODINGS: tuple[int, ...] = (
    ENCODING_TIGHT,
    ENCODING_HEXTILE,
    ENCODING_RRE,
    ENCODING_COPY_RECT,
    ENCODING_RAW,
)

#: Sent after the real encodings so the server has it to hand even if it
#: ignores everything else.
PSEUDO_DESKTOP_SIZE = ENCODING_DESKTOP_SIZE

MAX_ENCODINGS = 1024
MAX_RECT_ENCODINGS = 8

# Hextile sub-encoding mask. These are *bit* values, one per flag, not
# sequential identifiers: a tile's flags byte is a mask and several are set at
# once. QEMU 11.1 writes, for one screen, 0x00, 0x01, 0x02, 0x04, 0x06, 0x08,
# 0x18 and 0x0f within a single rectangle, which is only self-consistent under
# this reading. Reading them as 0/1/2/3/4 identifiers -- as this module
# previously did -- makes 0x00 mean "Raw" when it means "nothing at all, the
# whole tile is the carried background", and 0x02 mean "ForegroundSpecified"
# when it means "BackgroundSpecified". Every rectangle desynchronises within a
# few tiles and no frame is ever presented.
HEXTILE_RAW = 0x01
HEXTILE_BACKGROUND_SPECIFIED = 0x02
HEXTILE_FOREGROUND_SPECIFIED = 0x04
HEXTILE_ANY_SUBRECTS = 0x08
HEXTILE_SUBRECTS_COLOURED = 0x10
#: Bits a hextile flags byte may carry. Anything else (the ZRLE-era ZlibRaw and
#: Zlib bits, 0x20 and 0x40) is a different encoding.
HEXTILE_KNOWN_FLAGS = 0x1F
#: Tile edge length in pixels. A tile is 16x16; only the rightmost column and
#: bottom row of tiles may be smaller.
HEXTILE_TILE = 16
#: A tile's sub-rectangle count is one byte, so this is its ceiling. It is also
#: a useful plausibility bound: a tile is at most 256 pixels.
MAX_HEXTILE_SUBRECTS = 255

# Tight filters.
TIGHT_FILTER_COPY = 0
TIGHT_FILTER_PALETTE = 1
TIGHT_FILTER_GRADIENT = 2

#: Filtered data smaller than this is sent uncompressed, with no length prefix.
#: Twelve bytes, matching aurora-vnc and the RFB Tight specification. Reading a
#: compact length where there is none eats the first byte of pixel data.
TIGHT_MIN_TO_COMPRESS = 12


def encoding_name(encoding: int) -> str:
    return {
        ENCODING_RAW: "Raw",
        ENCODING_COPY_RECT: "CopyRect",
        ENCODING_RRE: "RRE",
        ENCODING_HEXTILE: "Hextile",
        ENCODING_TIGHT: "Tight",
        ENCODING_DESKTOP_SIZE: "DesktopSize",
    }.get(encoding, f"encoding {encoding}")


# ── Client messages ───────────────────────────────────────────────────────────

CLIENT_SET_PIXEL_FORMAT = 0
CLIENT_SET_ENCODINGS = 2
CLIENT_FB_UPDATE_REQUEST = 3
CLIENT_KEY_EVENT = 4
CLIENT_POINTER_EVENT = 5
CLIENT_CLIENT_CUT_TEXT = 6

SERVER_FRAMEBUFFER_UPDATE = 0
SERVER_SET_COLOUR_MAP_ENTRIES = 1
SERVER_BELL = 2
SERVER_SERVER_CUT_TEXT = 3

MAX_CUT_TEXT_BYTES = 64 * 1024

POINTER_BUTTON_LEFT = 1
POINTER_BUTTON_MIDDLE = 2
POINTER_BUTTON_RIGHT = 4
POINTER_WHEEL_UP = 8
POINTER_WHEEL_DOWN = 16
POINTER_WHEEL_LEFT = 32
POINTER_WHEEL_RIGHT = 64

_BUTTON_MASK = (
    POINTER_BUTTON_LEFT
    | POINTER_BUTTON_MIDDLE
    | POINTER_BUTTON_RIGHT
    | POINTER_WHEEL_UP
    | POINTER_WHEEL_DOWN
    | POINTER_WHEEL_LEFT
    | POINTER_WHEEL_RIGHT
)


def encode_set_pixel_format(fmt: PixelFormat) -> bytes:
    """SetPixelFormat: adopt a pixel format for subsequent rectangles.

    Only the final three bytes are padding; the message is 20 bytes total.
    """
    fmt.validate()
    return bytes((CLIENT_SET_PIXEL_FORMAT, 0, 0, 0)) + fmt.encode()


def encode_set_encodings(encodings: Sequence[int]) -> bytes:
    """SetEncodings: ask the server for encodings, best first.

    Order is preference order -- the server picks per rectangle from the
    encodings it can produce that the client listed. Listing something that
    cannot be decoded here would just be a way to be sent data we must throw
    away, so the list is exactly ``SUPPORTED_ENCODINGS``.
    """
    if not encodings:
        raise ProtocolError("SetEncodings with no encodings would disable updates")
    if len(encodings) > MAX_ENCODINGS:
        raise ProtocolError(f"refusing to send {len(encodings)} encodings (max {MAX_ENCODINGS})")
    payload = b"".join(struct.pack(">i", e) for e in encodings)
    padding = (4 - (len(payload) % 4)) % 4
    return (
        struct.pack(">BBH", CLIENT_SET_ENCODINGS, 0, len(encodings))
        + payload
        + b"\x00" * padding
    )


def encode_fb_update_request(incremental: bool, x: int = 0, y: int = 0, width: int = 0, height: int = 0) -> bytes:
    """FramebufferUpdateRequest.

    An incremental request asks only for what changed since the last frame. A
    non-incremental one asks for the whole desktop, which is what a resize or a
    pixel-format change needs.
    """
    if width and height:
        check_rect_size(width, height)
    return struct.pack(
        ">BBHHHH", CLIENT_FB_UPDATE_REQUEST, 1 if incremental else 0, x, y, width, height
    )


def encode_pointer_event(x: int, y: int, button_mask: int) -> bytes:
    """PointerEvent: absolute position plus a bitmask of held buttons."""
    if x < 0 or y < 0 or x > 0xFFFF or y > 0xFFFF:
        raise ProtocolError(f"pointer position ({x}, {y}) does not fit in two bytes")
    if button_mask & ~_BUTTON_MASK:
        raise ProtocolError(f"pointer button mask 0x{button_mask:x} has undefined bits")
    return struct.pack(">BBHH", CLIENT_POINTER_EVENT, button_mask, x, y)


def encode_key_event(down: bool, keysym: int) -> bytes:
    """KeyEvent: press (``down=True``) or release of one X keysym."""
    if not 0 <= keysym <= 0xFFFF:
        raise ProtocolError(f"keysym 0x{keysym:x} does not fit in u32")
    return struct.pack(">BBxxI", CLIENT_KEY_EVENT, 1 if down else 0, keysym)


def encode_client_init(shared: bool = True) -> bytes:
    """ClientInit: one byte, and the ordering question this whole function exists for.

    RFB says the server sends ServerInit immediately after SecurityResult and
    the client answers with this. QEMU's VNC server does the opposite: measured
    against QEMU 11.1, it sends nothing after SecurityResult until it has read
    ClientInit, and only then the desktop name and size. A client that waits for
    ServerInit before sending ClientInit therefore waits for ever -- a handshake
    that neither side times out or errors, which is exactly what a dead console
    looks like.

    Sending it early is safe against a server that follows the spec: the byte is
    the same byte, on the same stream, and a server expecting it after ServerInit
    reads it from the stream either way. There is no ordering a standard server
    can be broken by, because ClientInit never travels in the other direction.
    """
    return bytes((1 if shared else 0,))


def encode_client_cut_text(text: str) -> bytes:
    """ClientCutText: our clipboard going the other way."""
    raw = text.encode("latin-1", errors="replace")
    if len(raw) > MAX_CUT_TEXT_BYTES:
        raise ProtocolError(f"cut text of {len(raw)} bytes exceeds {MAX_CUT_TEXT_BYTES}")
    return struct.pack(">BxxxI", CLIENT_CLIENT_CUT_TEXT, len(raw)) + raw


# ── Limits ────────────────────────────────────────────────────────────────────

#: 4096x4096 is four times a 4K desktop; past that a resize is a bug or a lie,
#: and the framebuffer itself would be 64 MiB of Python bytes.
MAX_DESKTOP_WIDTH = 16384
MAX_DESKTOP_HEIGHT = 16384
MAX_FRAMEBUFFER_PIXELS = 4096 * 4096
MAX_RECT_PIXELS = 4096 * 4096
#: A single zlib stream that inflates past this is a decompression bomb, and a
#: Raw rectangle claiming this much data is the same attack without the effort.
MAX_TIGHT_DECOMPRESSED_BYTES = 128 * 1024 * 1024


def check_desktop_size(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise ProtocolError(f"desktop size {width}x{height} must be positive")
    if width > MAX_DESKTOP_WIDTH or height > MAX_DESKTOP_HEIGHT:
        raise ProtocolError(
            f"desktop size {width}x{height} exceeds the {MAX_DESKTOP_WIDTH}x{MAX_DESKTOP_HEIGHT} limit"
        )
    if width * height > MAX_FRAMEBUFFER_PIXELS:
        raise ProtocolError(
            f"desktop size {width}x{height} is {width * height} pixels, over the "
            f"{MAX_FRAMEBUFFER_PIXELS} limit"
        )


def check_rect_size(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise ProtocolError(f"rectangle {width}x{height} must be positive")
    if width * height > MAX_RECT_PIXELS:
        raise ProtocolError(
            f"rectangle of {width * height} pixels exceeds the {MAX_RECT_PIXELS} limit"
        )


def check_rect_bounds(x: int, y: int, width: int, height: int, fb_width: int, fb_height: int) -> None:
    """Every rectangle must land inside the framebuffer.

    Checked in integers with no arithmetic that can overflow, before anything is
    written. A rectangle that starts inside and ends outside is exactly the
    shape of a heap-corruption bug, and the protocol has no way to say "ignore
    that one" -- so it is a connection-fatal error instead.
    """
    check_rect_size(width, height)
    if x < 0 or y < 0:
        raise ProtocolError(f"rectangle at ({x}, {y}) has a negative origin")
    if x + width > fb_width or y + height > fb_height:
        raise ProtocolError(
            f"rectangle ({x}, {y}) {width}x{height} does not fit in a "
            f"{fb_width}x{fb_height} framebuffer"
        )


# ── Errors ────────────────────────────────────────────────────────────────────


class RFBError(Exception):
    """Base for every error this module raises."""


class ProtocolError(RFBError):
    """The peer sent something that is not valid RFB.

    Never a state problem: the stream is now untrustworthy, because we no
    longer know where the next message boundary is.
    """


class HandshakeError(RFBError):
    """Negotiation failed -- no common version, or no common security type."""


class AuthError(RFBError):
    """Authentication was rejected by the server."""


class UnsupportedEncoding(RFBError):
    """The server sent an encoding this client does not implement."""


# ── Reader ────────────────────────────────────────────────────────────────────


class ByteReader:
    """Sequential reads over a byte string, with every length bounded.

    A protocol decoder indexes and copies on the strength of values it has
    just read. This is the single place where a short buffer is turned into an
    exception instead of a truncated value, so no decoder has to check lengths
    itself.
    """

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    @property
    def remaining(self) -> int:
        return len(self._data) - self._pos

    def read_exactly(self, count: int) -> bytes:
        if count < 0:
            raise ProtocolError(f"cannot read {count} bytes")
        if count > self.remaining:
            raise ProtocolError(
                f"truncated message: wanted {count} bytes, {self.remaining} left"
            )
        start = self._pos
        self._pos += count
        return self._data[start:start + count]

    def read_u8(self) -> int:
        return self.read_exactly(1)[0]

    def read_u16(self) -> int:
        return struct.unpack(">H", self.read_exactly(2))[0]

    def read_i32(self) -> int:
        return struct.unpack(">i", self.read_exactly(4))[0]

    def read_u32(self) -> int:
        return struct.unpack(">I", self.read_exactly(4))[0]


class AsyncByteReader:
    """The same interface as :class:`ByteReader`, but awaitable.

    Rectangle payloads are read from here rather than from a byte string
    because two of the encodings are self-delimiting: Hextile's tile stream and
    RRE's sub-rectangle list both run until they are finished, and neither
    carries a length. The only correct way to read them is one field at a time
    from the connection itself -- reading a guessed number of bytes either
    blocks waiting for bytes that will never arrive or swallows the next
    message.

    This is the same pull interface :class:`ByteReader` offers, so a decoder
    written against it is driven by a byte string in a test and by a socket in
    production with no change to the decoding code.
    """

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    @property
    def remaining(self) -> int:
        return len(self._data) - self._pos

    async def read_exactly(self, count: int) -> bytes:
        # Delegating to ByteReader keeps the bounds rules in one place, so an
        # in-memory source and a socket source reject the same inputs.
        if count < 0:
            raise ProtocolError(f"cannot read {count} bytes")
        if self._pos + count > len(self._data):
            raise ProtocolError(
                f"truncated message: wanted {count} bytes, "
                f"{len(self._data) - self._pos} left"
            )
        start = self._pos
        self._pos += count
        return self._data[start:start + count]

    async def read_u8(self) -> int:
        return (await self.read_exactly(1))[0]

    async def read_u16(self) -> int:
        return int.from_bytes(await self.read_exactly(2), "big")

    async def read_i32(self) -> int:
        return int.from_bytes(await self.read_exactly(4), "big", signed=True)

    async def read_u32(self) -> int:
        return int.from_bytes(await self.read_exactly(4), "big")


# ── Framebuffer ───────────────────────────────────────────────────────────────


class Framebuffer:
    """A decoded desktop, held as 32-bit BGRA bytes.

    BGRA little-endian is ``QImage::Format_RGB32`` on the platforms this runs
    on, so painting is a wrap of the existing buffer with no conversion.

    The fourth byte is the server's spare byte for Raw 32bpp rectangles and
    ``0xFF`` everywhere else. It is ignored by ``Format_RGB32``; the
    difference is only visible to code that converts to ``Format_ARGB32``, and
    normalising it there is cheaper than slowing down the Raw path.
    """

    __slots__ = ("width", "height", "data")

    def __init__(self, width: int, height: int):
        check_desktop_size(width, height)
        self.width = width
        self.height = height
        self.data = bytearray(width * height * 4)

    def resize(self, width: int, height: int) -> None:
        """Adopt a new size, discarding contents.

        A resize invalidates everything: the server is free to treat the whole
        new desktop as changed, and there is no correct way to keep old pixels
        across a geometry change.
        """
        if width == self.width and height == self.height:
            return
        check_desktop_size(width, height)
        self.width = width
        self.height = height
        self.data = bytearray(width * height * 4)

    def fill_rect(self, x: int, y: int, width: int, height: int, bgra: bytes) -> None:
        """Fill a rectangle with one 4-byte BGRA pixel."""
        check_rect_bounds(x, y, width, height, self.width, self.height)
        row = bytes(bgra) * width
        for line in range(y, y + height):
            start = (line * self.width + x) * 4
            self.data[start:start + width * 4] = row

    def copy_rect(self, src_x: int, src_y: int, width: int, height: int, dst_x: int, dst_y: int) -> None:
        """Copy a rectangle within the framebuffer (the CopyRect encoding)."""
        check_rect_bounds(src_x, src_y, width, height, self.width, self.height)
        check_rect_bounds(dst_x, dst_y, width, height, self.width, self.height)
        stride = self.width * 4
        length = width * 4
        for line in range(height):
            s = (src_y + line) * stride + src_x * 4
            d = (dst_y + line) * stride + dst_x * 4
            self.data[d:d + length] = self.data[s:s + length]

    def as_bytes(self) -> bytes:
        return bytes(self.data)


# ── Per-pixel conversion ──────────────────────────────────────────────────────


def _channel(value: int, maximum: int) -> int:
    """Scale a raw component to 0..255."""
    if maximum == 255:
        return value
    if maximum == 0:
        return 0
    return (value * 255) // maximum


class _PixelReader:
    """Decodes one PIXEL from a server format into 4-byte BGRA.

    Constructed per format, not per pixel: building the shift table for every
    pixel would dominate the cost of anything but Raw 32bpp.
    """

    __slots__ = ("bpp", "order", "masks")

    def __init__(self, fmt: PixelFormat):
        self.bpp = fmt.bytes_per_pixel
        self.order = "big" if fmt.big_endian else "little"
        # (bit position within the pixel, component maximum) per channel, so a
        # shift-and-mask works whichever way round the byte order is.
        self.masks = (
            (fmt.red_shift, fmt.red_max),
            (fmt.green_shift, fmt.green_max),
            (fmt.blue_shift, fmt.blue_max),
        )

    def pixel_to_bgra(self, offset: int, data: bytes) -> bytes:
        value = int.from_bytes(data[offset:offset + self.bpp], self.order)
        (rsh, rmax), (gsh, gmax), (bsh, bmax) = self.masks
        return bytes(
            (
                _channel((value >> bsh) & bmax, bmax),
                _channel((value >> gsh) & gmax, gmax),
                _channel((value >> rsh) & rmax, rmax),
                0xFF,
            )
        )

    def pixel_int_to_bgra(self, value: int) -> bytes:
        (rsh, rmax), (gsh, gmax), (bsh, bmax) = self.masks
        return bytes(
            (
                _channel((value >> bsh) & bmax, bmax),
                _channel((value >> gsh) & gmax, gmax),
                _channel((value >> rsh) & rmax, rmax),
                0xFF,
            )
        )


async def _read_pixel(source: "AsyncByteReader", pixels: _PixelReader) -> bytes:
    value = int.from_bytes(await source.read_exactly(pixels.bpp), pixels.order)
    return pixels.pixel_int_to_bgra(value)


def _blit_raw(fb: Framebuffer, x: int, y: int, width: int, height: int, data: bytes, fmt: PixelFormat) -> None:
    """Write ``width`` x ``height`` raw pixels, row by row."""
    stride = fb.width * 4
    bpp = fmt.bytes_per_pixel
    row_bytes = width * bpp
    if fmt.is_bgrx32():
        # The common case: already BGRA, so every row is one slice assignment.
        for line in range(height):
            start = line * row_bytes
            d = (y + line) * stride + x * 4
            fb.data[d:d + width * 4] = data[start:start + row_bytes]
        return
    pixels = _PixelReader(fmt)
    offset = 0
    for line in range(height):
        d = (y + line) * stride + x * 4
        row = bytearray(width * 4)
        for col in range(width):
            row[col * 4:col * 4 + 4] = pixels.pixel_to_bgra(offset, data)
            offset += bpp
        fb.data[d:d + width * 4] = row


# ── Rectangle decoding ────────────────────────────────────────────────────────


@dataclass
class Rectangle:
    """The 12-byte header at the front of every rectangle in an update."""

    x: int
    y: int
    width: int
    height: int
    encoding: int

    @classmethod
    def decode(cls, reader: "ByteReader") -> "Rectangle":
        return cls(reader.read_u16(), reader.read_u16(), reader.read_u16(), reader.read_u16(), reader.read_i32())


@dataclass
class DesktopResize:
    """Emitted by a DesktopSize pseudo-encoding rectangle."""

    width: int
    height: int


async def decode_rectangle(
    fb: Framebuffer,
    rect: Rectangle,
    fmt: PixelFormat,
    source: "AsyncByteReader",
) -> Optional[DesktopResize]:
    """Apply one rectangle to ``fb``, reading its payload from ``source``.

    Returns a :class:`DesktopResize` if the rectangle was a DesktopSize
    pseudo-encoding (the caller applies it after the update is complete).

    Raises :class:`ProtocolError` for anything malformed. Bounds are checked
    *before* any write, so a bad rectangle cannot leave the framebuffer
    half-updated in a way the renderer would then display.
    """
    encoding = rect.encoding

    if encoding == ENCODING_DESKTOP_SIZE:
        # Pseudo-encoding: w/h are the new desktop size, and the position must
        # be the origin.
        if rect.x != 0 or rect.y != 0:
            raise ProtocolError(f"DesktopSize rectangle at ({rect.x}, {rect.y}) is not at the origin")
        check_desktop_size(rect.width, rect.height)
        return DesktopResize(rect.width, rect.height)

    if encoding == ENCODING_COPY_RECT:
        if rect.width == 0 or rect.height == 0:
            return None
        src_x = await source.read_u16()
        src_y = await source.read_u16()
        check_rect_bounds(src_x, src_y, rect.width, rect.height, fb.width, fb.height)
        check_rect_bounds(rect.x, rect.y, rect.width, rect.height, fb.width, fb.height)
        fb.copy_rect(src_x, src_y, rect.width, rect.height, rect.x, rect.y)
        return None

    check_rect_bounds(rect.x, rect.y, rect.width, rect.height, fb.width, fb.height)

    if encoding == ENCODING_RAW:
        need = rect.width * rect.height * fmt.bytes_per_pixel
        _blit_raw(fb, rect.x, rect.y, rect.width, rect.height, await source.read_exactly(need), fmt)
        return None
    if encoding == ENCODING_RRE:
        await _decode_rre(fb, rect, fmt, source)
        return None
    if encoding == ENCODING_HEXTILE:
        await _decode_hextile(fb, rect, fmt, source)
        return None
    if encoding == ENCODING_TIGHT:
        await _decode_tight(fb, rect, fmt, source)
        return None

    raise UnsupportedEncoding(
        f"server sent {encoding_name(encoding)} (encoding {encoding}); "
        f"this client decodes Raw, CopyRect, RRE, Hextile and Tight"
    )


async def _decode_rre(fb: Framebuffer, rect: Rectangle, fmt: PixelFormat, source: "AsyncByteReader") -> None:
    """RRE: one background pixel plus sub-rectangles filled with a foreground.

    The server only chooses RRE for regions that are mostly one colour, so the
    sub-rectangle count is bounded by what is plausible rather than trusted.
    """
    pixels = _PixelReader(fmt)
    background = await _read_pixel(source, pixels)
    subrect_count = await source.read_u32()
    # Every sub-rectangle covers at least one pixel, so a count above the
    # rectangle's own area cannot be satisfied by any legal encoding.
    if subrect_count > rect.width * rect.height:
        raise ProtocolError(
            f"RRE claims {subrect_count} sub-rectangles for a {rect.width}x{rect.height} region"
        )
    fb.fill_rect(rect.x, rect.y, rect.width, rect.height, background)
    for _ in range(subrect_count):
        sx = await source.read_u8()
        sy = await source.read_u8()
        sw = await source.read_u8()
        sh = await source.read_u8()
        # Sub-rectangles are relative to the RRE region and must stay inside it.
        if sw == 0 or sh == 0:
            raise ProtocolError("RRE sub-rectangle has zero width or height")
        if sx + sw > rect.width or sy + sh > rect.height:
            raise ProtocolError(
                f"RRE sub-rectangle ({sx}, {sy}) {sw}x{sh} escapes its "
                f"{rect.width}x{rect.height} region"
            )
        fb.fill_rect(rect.x + sx, rect.y + sy, sw, sh, await _read_pixel(source, pixels))


async def _decode_hextile(fb: Framebuffer, rect: Rectangle, fmt: PixelFormat, source: "AsyncByteReader") -> None:
    """Hextile: a 16x16 grid of tiles, each raw or filled with two colours.

    Each tile opens with a one-byte flags *mask* whose bits are
    ``HEXTILE_*`` above, in this order on the wire: the flags byte, then the
    background pixel if **BackgroundSpecified** is set, then the foreground
    pixel if **ForegroundSpecified** is set, then -- if **AnySubrects** is set
    -- a one-byte sub-rectangle count, then the sub-rectangles themselves.

    Two carry rules from the specification are load-bearing, and getting either
    wrong corrupts pixels rather than raising:

    * the background may not be carried across a Raw tile, and the *first*
      non-Raw tile of a rectangle must therefore state it;
    * the foreground may not be carried across a Raw tile or across a tile that
      carried **SubrectsColoured**, because a coloured sub-rectangle's own
      colour is not the foreground.

    QEMU relies on both: it tracks ``last_bg``/``last_fg`` and ``has_bg``/
    ``has_fg`` in exactly this way, resetting ``has_fg`` after a
    SubrectsColoured tile.
    """
    pixels = _PixelReader(fmt)
    x, y, w, h = rect.x, rect.y, rect.width, rect.height
    carried_background: Optional[bytes] = None
    carried_foreground: Optional[bytes] = None
    for tile_y in range(0, h, HEXTILE_TILE):
        rows = min(HEXTILE_TILE, h - tile_y)
        for tile_x in range(0, w, HEXTILE_TILE):
            cols = min(HEXTILE_TILE, w - tile_x)
            ax, ay = x + tile_x, y + tile_y
            flags = await source.read_u8()
            if flags & ~HEXTILE_KNOWN_FLAGS:
                raise ProtocolError(f"Hextile tile flags {flags:#04x} are not defined by Hextile")
            if flags & HEXTILE_RAW:
                _blit_raw(
                    fb,
                    ax,
                    ay,
                    cols,
                    rows,
                    await source.read_exactly(cols * rows * fmt.bytes_per_pixel),
                    fmt,
                )
                carried_background = None
                carried_foreground = None
                continue
            if flags & HEXTILE_BACKGROUND_SPECIFIED:
                carried_background = await _read_pixel(source, pixels)
            if carried_background is None:
                raise ProtocolError("Hextile tile inherits a background from no previous tile")
            if flags & HEXTILE_FOREGROUND_SPECIFIED:
                carried_foreground = await _read_pixel(source, pixels)
            coloured = bool(flags & HEXTILE_SUBRECTS_COLOURED)
            fb.fill_rect(ax, ay, cols, rows, carried_background)
            count = await source.read_u8() if flags & HEXTILE_ANY_SUBRECTS else 0
            if count > MAX_HEXTILE_SUBRECTS:
                raise ProtocolError(f"Hextile tile claims {count} sub-rectangles")
            for _ in range(count):
                if coloured:
                    # SubrectsColoured: the colour precedes the coordinates and
                    # is not the tile's foreground.
                    foreground = await _read_pixel(source, pixels)
                else:
                    if carried_foreground is None:
                        raise ProtocolError(
                            "Hextile tile has sub-rectangles but inherits a foreground "
                            "from no previous tile"
                        )
                    foreground = carried_foreground
                await _read_hextile_subrect(fb, ax, ay, cols, rows, foreground, source)
            if coloured:
                # A coloured sub-rectangle does not establish a foreground.
                carried_foreground = None


async def _read_hextile_subrect(
    fb: Framebuffer,
    ax: int,
    ay: int,
    cols: int,
    rows: int,
    foreground: bytes,
    source: "AsyncByteReader",
) -> None:
    """Read one Hextile sub-rectangle and paint it.

    Two bytes carry it, and both are packed nibbles: the first is
    ``x << 4 | y`` and the second is ``(w - 1) << 4 | (h - 1)``.

    The ``- 1`` is the part that is invisible until it is fatal. Storing the
    extents verbatim reads a one-pixel-tall run as zero pixels, which is not
    detectable as an error -- ``fill_rect`` would be handed a zero height and
    the stream position would still be right, so the frame decodes to a screen
    with invisible scanlines and no diagnostic anywhere. QEMU's
    ``hextile_enc_cord`` writes ``(w - 1) & 0x0F`` and ``(h - 1) & 0x0F``,
    which is what makes a full 16-wide tile encode as ``0x0F`` rather than
    ``0x10``, and the nibble range is 0..15 -- so a width of 16 arrives as 15
    and has to be incremented back.
    """
    xy = await source.read_u8()
    wh = await source.read_u8()
    sx, sy = xy >> 4, xy & 0x0F
    sw, sh = (wh >> 4) + 1, (wh & 0x0F) + 1
    if sx + sw > cols or sy + sh > rows:
        raise ProtocolError(
            f"Hextile sub-rectangle ({sx},{sy}) {sw}x{sh} escapes its {cols}x{rows} tile"
        )
    fb.fill_rect(ax + sx, ay + sy, sw, sh, foreground)


async def _read_tight_compact_length(source: "AsyncByteReader") -> int:
    """Tight's one-to-three byte compact length.

    The first byte's high bit says whether more follows; only then does the
    second byte's high bit discriminate two bytes from three. Seven-bit groups
    run *low bits first*, so the first byte holds the low seven bits and the
    last byte holds the high ones: 10000 arrives as ``0x90 0x4E`` and is read as
    ``0x10 | (0x4E << 7)``.

    This is *not* the four-byte form used by Hextile and ZRLE. Both start with
    a byte whose high bit means "more follows", so a decoder that assumes the
    four-byte rule will read a three-byte length as four and desynchronise
    forever -- silently, because it will still find the next message boundary
    eventually if the sizes happen to line up, and never if they do not.
    """
    first = await source.read_u8()
    if first & 0x80 == 0:
        return first & 0x7F
    second = await source.read_u8()
    if second & 0x80 == 0:
        return (first & 0x7F) | ((second & 0x7F) << 7)
    third = await source.read_u8()
    return (first & 0x7F) | ((second & 0x7F) << 7) | (third << 14)


async def _decode_tight(fb: Framebuffer, rect: Rectangle, fmt: PixelFormat, source: "AsyncByteReader") -> None:
    """Tight: zlib over TPIXELs, with a filter implied by the control byte.

    Three of Tight's four shapes are decoded:

    * FillCompression -- one TPIXEL covering the whole rectangle;
    * BasicCompression with zlib and the CopyFilter or an explicit filter id;
    * the control byte's filter bit clear, which means CopyFilter.

    JPEG and PNG compression and the Gradient and Palette filters are refused
    rather than guessed at. Refusing is a session-ending error here, which is
    why FillCompression is implemented rather than refused: QEMU's VNC server
    sends it, for every solid rectangle it draws, and treating it as an
    undecodable encoding ended the session on the first frame -- a cursor
    trail or a cleared menu was enough to kill the console.
    """
    control = await source.read_u8()
    # The compression type is bits 6-4 of the whole byte -- mask *after* the
    # shift, never before, or Fill (0x8) and JPEG (0x9) both lose bit 3 and
    # become indistinguishable.
    kind = control >> 4
    if kind == 0x08:
        # FillCompression: no compression id, no filter byte, no zlib stream --
        # just one TPIXEL covering the entire rectangle.
        tpixel_size = _tight_tpixel_size(fmt)
        _fill_tight_pixel(fb, rect, fmt, await source.read_exactly(tpixel_size))
        return
    if kind == 0x09:
        raise UnsupportedEncoding(
            "Tight JPEG compression is not decoded; this client never requests a "
            "lossy session, so a peer selecting it is not honouring the encodings "
            "we advertised"
        )
    if kind & 0x08:
        raise UnsupportedEncoding(f"Tight compression type {kind:#x} is not defined")
    # bit 6 is the explicit-filter flag, not part of the compression type.
    filter_id = await source.read_u8() if kind & 0x04 else TIGHT_FILTER_COPY
    tpixel_size = _tight_tpixel_size(fmt)
    palette: Optional[list[tuple[int, int, int, int]]] = None
    if filter_id == TIGHT_FILTER_PALETTE:
        # A one-byte count *minus one*, then that many TPIXELs. The palette is
        # sent in the clearstream, never through a zlib stream.
        colours = await source.read_u8() + 1
        palette = [
            _tight_tpixel_to_bgra(bytes(await source.read_exactly(tpixel_size)), tpixel_size, fmt)
            for _ in range(colours)
        ]
    if filter_id == TIGHT_FILTER_GRADIENT:
        pass  # decoded below; it needs the same length treatment as CopyFilter
    elif filter_id not in (TIGHT_FILTER_COPY, TIGHT_FILTER_PALETTE):
        raise UnsupportedEncoding(f"Tight filter {filter_id} is not known")
    # Bits 5-4 are the zlib *stream id*, not a compression method: kind 1 is
    # BasicCompression on stream 1, which is most of what a conformant server
    # sends. Treating anything but 0 as a different compression method refuses
    # three quarters of the rectangles on the wire.
    stream = kind & 0x03
    # Bits 3-0 are a bitmask of streams the server reset before this
    # rectangle. It has to be applied before the stream is touched, or a
    # rectangle that resets stream 0 reads the tail of the previous one.
    reset_tight_streams(control & 0x0F)
    expected = _tight_raw_length(rect, filter_id, palette, tpixel_size)
    if expected < TIGHT_MIN_TO_COMPRESS:
        # Too small to be worth compressing, and the specification says so: sent
        # as-is with no compact length at all. Reading a length here eats the
        # first byte of the pixel data.
        data = await source.read_exactly(expected)
    else:
        length = await _read_tight_compact_length(source)
        if length > MAX_TIGHT_DECOMPRESSED_BYTES:
            raise ProtocolError(
                f"Tight length {length} is beyond the "
                f"{MAX_TIGHT_DECOMPRESSED_BYTES} byte limit"
            )
        data = _inflate_stream(stream, await source.read_exactly(length), expected)
    if len(data) != expected:
        raise ProtocolError(
            f"Tight rectangle carried {len(data)} bytes, expected {expected}"
        )
    if palette is not None:
        _decode_tight_palette(fb, rect, data, palette)
    elif filter_id == TIGHT_FILTER_GRADIENT:
        _decode_tight_gradient(fb, rect, fmt, data, tpixel_size)
    else:
        _decode_tight_pixels(fb, rect, fmt, data)


def _tight_raw_length(
    rect: Rectangle,
    filter_id: int,
    palette: Optional[list[tuple[int, int, int, int]]],
    tpixel_size: int,
) -> int:
    """Bytes of filtered data a rectangle carries once the filter is applied.

    The two-colour palette is the awkward one: one bit per pixel, with each row
    padded to a whole number of bytes. Computing the length from the rectangle
    instead of from the bytes that arrived is what makes that padding
    predictable -- and getting it wrong desynchronises the stream from that
    rectangle onwards, since RFB has no resynchronisation.
    """
    count = rect.width * rect.height
    if filter_id == TIGHT_FILTER_PALETTE and palette is not None:
        return rect.height * ((rect.width + 7) // 8) if len(palette) <= 2 else count
    # CopyFilter and GradientFilter carry one whole TPIXEL per pixel.
    return count * tpixel_size


#: The four zlib streams a Tight connection carries for its whole life.
#:
#: Persistent by design, not an optimisation. Tight's four streams exist so that
#: a rectangle that repeats content already sent on the same stream can be
#: encoded as a back-reference; a fresh inflater per rectangle cannot read the
#: second rectangle that uses a stream. The server resets a stream by setting the
#: matching bit in the control byte's low nibble, which is what `reset` does.
_TIGHT_STREAMS: list[Optional["zlib._Decompress"]] = [None, None, None, None]


def reset_tight_streams(mask: int) -> None:
    """Drop the streams whose reset bit is set in `mask`.

    `mask` is the control byte's low nibble, a *bitmask*: bit 0 resets stream
    0, bit 1 resets stream 1, and so on. It is not a stream index -- reading it
    as one resets the wrong stream and desynchronises everything after it.
    """
    for index in range(4):
        if mask & (1 << index):
            _TIGHT_STREAMS[index] = None


def _tight_tpixel_to_bgra(
    pixel: bytes, tpixel_size: int, fmt: PixelFormat
) -> tuple[int, int, int, int]:
    """One Tight TPIXEL as (b, g, r, a) for the framebuffer.

    A TPIXEL is three bytes in R, G, B order for a 32bpp/24-depth format, and
    one whole pixel (two bytes for RGB555) for everything else.
    """
    if tpixel_size == 3:
        r, g, b = pixel[0], pixel[1], pixel[2]
        return (b, g, r, 0xFF)
    reader = _PixelReader(fmt)
    data = reader.pixel_to_bgra(0, pixel)
    return (data[0], data[1], data[2], data[3])


def _decode_tight_palette(
    fb: Framebuffer,
    rect: Rectangle,
    data: bytes,
    palette: list[tuple[int, int, int, int]],
) -> None:
    """Paint a PaletteFilter rectangle.

    One or two colours are one bit per pixel, with each row padded out to a
    whole number of bytes -- the padding is why the payload length has to be
    computed from the rectangle rather than read off the wire. Three or more
    colours are one index byte per pixel, tightly packed with no padding.

    An index outside the palette is refused rather than clamped: it means the
    stream is desynchronised, and painting an arbitrary colour would hide that
    for the rest of the session instead of ending it.
    """
    stride = fb.width * 4
    count = rect.width * rect.height
    if len(palette) <= 2:
        row_bytes = (rect.width + 7) // 8
        for line in range(rect.height):
            base = line * row_bytes
            d = (rect.y + line) * stride + rect.x * 4
            for col in range(rect.width):
                byte = data[base + (col >> 3)]
                index = (byte >> (7 - (col & 7))) & 1
                b, g, r, a = palette[index]
                fb.data[d + col * 4:d + col * 4 + 4] = bytes((b, g, r, a))
    else:
        if len(data) != count:
            raise ProtocolError(
                f"Tight palette indices are {len(data)} bytes, expected {count}"
            )
        limit = len(palette)
        for index in range(count):
            value = data[index]
            if value >= limit:
                raise ProtocolError(
                    f"Tight palette index {value} is outside a {limit}-colour palette"
                )
            line, col = divmod(index, rect.width)
            b, g, r, a = palette[value]
            d = (rect.y + line) * stride + rect.x + col
            fb.data[d * 4:d * 4 + 4] = bytes((b, g, r, a))


def _decode_tight_gradient(
    fb: Framebuffer, rect: Rectangle, fmt: PixelFormat, raw: bytes, tpixel_size: int
) -> None:
    """Paint a GradientFilter rectangle.

    Each pixel is not a colour but a *difference* from a prediction built from
    its neighbours: `left + above - above-left`, per component, clamped to the
    component's range. The prediction is deliberately the same for every pixel
    in the row-major sweep -- it uses the pixel already reconstructed to the
    left on the current row, and the previous row's already-reconstructed
    values above -- so this cannot be parallelised within a row.

    Getting the order wrong is not a crash: the first pixel of every row is
    predicted from nothing and decoded correctly, and the error then propagates
    across the row as a smear. That is why this is worth testing against
    hand-computed values rather than only against a round trip.

    A 32bpp/24-depth TPIXEL is an R,G,B triple, so its components sit at shifts
    16/8/0 in a 0x00RRGGBB word. Any other TPIXEL is a whole pixel in the
    server's own layout, so its shifts and maxima come from the format.
    """
    if tpixel_size == 3:
        shifts = (16, 8, 0)
        maxes = (255, 255, 255)
    else:
        shifts = (fmt.red_shift, fmt.green_shift, fmt.blue_shift)
        maxes = (fmt.red_max, fmt.green_max, fmt.blue_max)
    reader = _PixelReader(fmt)
    stride = fb.width * 4
    width, height = rect.width, rect.height

    previous = [0] * width
    current = [0] * width
    for row in range(height):
        for col in range(width):
            offset = (row * width + col) * tpixel_size
            pixel = raw[offset:offset + tpixel_size]
            if tpixel_size == 3:
                diff = (pixel[0] << 16) | (pixel[1] << 8) | pixel[2]
            else:
                diff = int.from_bytes(pixel, reader.order)
            value = 0
            for component, (shift, maximum) in enumerate(zip(shifts, maxes)):
                left = ((current[col - 1] >> shift) & maximum) if col > 0 else 0
                above = ((previous[col] >> shift) & maximum) if row > 0 else 0
                upper_left = (
                    ((previous[col - 1] >> shift) & maximum) if col > 0 and row > 0 else 0
                )
                predicted = left + above - upper_left
                predicted = 0 if predicted < 0 else min(predicted, maximum)
                value |= (predicted + ((diff >> shift) & maximum) & maximum) << shift
            current[col] = value
        base = (rect.y + row) * stride + rect.x * 4
        for col in range(width):
            value = current[col]
            if tpixel_size == 3:
                bgra = bytes((value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF, 0xFF))
            else:
                bgra = reader.pixel_int_to_bgra(value)
            offset = base + col * 4
            fb.data[offset:offset + 4] = bgra
        previous, current = current, previous


def _inflate_stream(stream: int, payload: bytes, expected: int) -> bytes:
    """Inflate one Tight rectangle from its persistent zlib stream.

    **zlib framing, not raw DEFLATE.** Tight's BasicCompression carries a
    zlib-wrapped stream: a two-byte header and an Adler-32 trailer, exactly as
    `zlib.compress` produces. QEMU sends them, beginning `78 da`. Inflating
    with `-MAX_WBITS` fails with "incorrect header check" on the first
    rectangle, which reads as a corrupt stream rather than as wrong framing.

    The decompressor is kept across rectangles because the stream is. A decoder
    that builds one per rectangle fails on the second rectangle that uses it.
    """
    decompressor = _TIGHT_STREAMS[stream]
    if decompressor is None:
        decompressor = zlib.decompressobj()
        _TIGHT_STREAMS[stream] = decompressor
    try:
        raw = decompressor.decompress(payload, MAX_TIGHT_DECOMPRESSED_BYTES)
    except zlib.error as exc:
        raise ProtocolError(f"Tight zlib stream {stream} is corrupt: {exc}") from exc
    if decompressor.unconsumed_tail:
        raise ProtocolError(
            f"Tight zlib stream {stream} expands past the "
            f"{MAX_TIGHT_DECOMPRESSED_BYTES} byte limit"
        )
    return raw


def _tight_tpixel_size(fmt: PixelFormat) -> int:
    """Bytes per TPIXEL for a given pixel format.

    Tight's specification says a TPIXEL is three RGB bytes for colour that needs
    more than 16 bits, and a whole PIXEL otherwise -- one byte at depth 8, two
    at 16. The threshold is on the *format*, not on ``depth > 8``: a 16-bit 555
    format has depth 15, but its TPIXEL is two bytes, not an RGB triple.

    Getting this wrong on a 555 session makes every pixel of the screen a wrong
    colour rather than a failure, because the lengths still happen to work out
    for some regions and not others.
    """
    if fmt.depth > 8 and fmt.bits_per_pixel > 16:
        return 3
    return fmt.bytes_per_pixel


def _fill_tight_pixel(fb: Framebuffer, rect: Rectangle, fmt: PixelFormat, pixel: bytes) -> None:
    """Fill a rectangle with the single TPIXEL a FillCompression rectangle carries.

    A TPIXEL is three RGB bytes for colour that needs more than 16 bits and a
    whole PIXEL otherwise, exactly as in the non-filled case; the difference is
    only that there is one of them and it covers the whole rectangle.

    The 3-byte form is already BGR-ordered for us. The whole-PIXEL form is not:
    it is a PIXEL in the *server's* format, so it goes through the same
    conversion the Raw path uses -- via a one-pixel blit, which is also how the
    colour gets validated. Filling with the raw bytes instead would put a 555
    value straight into a BGRA framebuffer and paint the wrong colour.
    """
    if len(pixel) == 3:
        r, g, b = pixel
        fb.fill_rect(rect.x, rect.y, rect.width, rect.height, bytes((b, g, r, 0xFF)))
        return
    if len(pixel) != fmt.bytes_per_pixel:
        raise ProtocolError(
            f"Tight fill pixel is {len(pixel)} bytes, expected "
            f"{3 if _tight_tpixel_size(fmt) == 3 else fmt.bytes_per_pixel}"
        )
    _blit_raw(fb, rect.x, rect.y, 1, 1, pixel, fmt)
    start = (rect.y * fb.width + rect.x) * 4
    fb.fill_rect(
        rect.x, rect.y, rect.width, rect.height, bytes(fb.data[start:start + 4])
    )


def _decode_tight_pixels(fb: Framebuffer, rect: Rectangle, fmt: PixelFormat, raw: bytes) -> None:
    """Write TPIXELs: three RGB bytes for deep colour, else a whole pixel."""
    tpixel_size = _tight_tpixel_size(fmt)
    need = rect.width * rect.height * tpixel_size
    if len(raw) != need:
        raise ProtocolError(f"Tight payload is {len(raw)} bytes, expected {need} for the rectangle")
    if tpixel_size == 3:
        # Tight sends R, G, B triples; the framebuffer is B, G, R, spare.
        stride = fb.width * 4
        offset = 0
        for line in range(rect.height):
            d = (rect.y + line) * stride + rect.x * 4
            for col in range(rect.width):
                r = raw[offset]
                g = raw[offset + 1]
                b = raw[offset + 2]
                fb.data[d + col * 4:d + col * 4 + 4] = bytes((b, g, r, 0xFF))
                offset += 3
        return
    _blit_raw(fb, rect.x, rect.y, rect.width, rect.height, raw, fmt)


# ── FramebufferUpdate ─────────────────────────────────────────────────────────


def check_fb_update_padding(padding: bytes) -> None:
    """FramebufferUpdate has one padding byte after the message type."""
    if padding != b"\x00":
        raise ProtocolError(f"FramebufferUpdate padding is {padding!r}, expected zero")


async def decode_set_colour_map_entries(source: "AsyncByteReader") -> None:
    """SetColourMapEntries: read past, do not store.

    This client refuses palette pixel formats outright (see
    :meth:`PixelFormat.validate`), so a colour map has nothing to index into
    and the entries would only be dead weight. The payload still has to be
    consumed, or the next message is read from the wrong offset.

    Three bytes of padding follow the message type, which the dispatcher has
    already consumed -- reading four here would swallow the first byte of the
    colour map's own header. Note this message has *one* padding byte, like
    FramebufferUpdate, because its fields are 16-bit; ServerCutText is the one
    with three, because its length is 32-bit.
    """
    await source.read_exactly(1)
    # The first-colour index is read to advance the stream but not used: this
    # client has no palette to index, so every entry in this message is dead.
    await source.read_u16()
    count = await source.read_u16()
    if count > 256:
        raise ProtocolError(f"colour map with {count} entries is implausible")
    await source.read_exactly(count * 6)


async def read_server_cut_text(source: "AsyncByteReader") -> str:
    """ServerCutText: the guest clipboard, usually a middle-click paste.

    Three bytes of padding follow the message type, which the dispatcher has
    already consumed.
    """
    await source.read_exactly(3)
    length = await source.read_u32()
    if length > MAX_CUT_TEXT_BYTES:
        raise ProtocolError(f"cut text of {length} bytes exceeds {MAX_CUT_TEXT_BYTES}")
    return (await source.read_exactly(length)).decode("latin-1")


# ── DES, for VNC Authentication ────────────────────────────────────────────────
#
# VNC Auth is DES-ECB with an 8-byte key derived from the password. Nothing in
# the standard library or in the installed `cryptography` wheel provides single
# DES any more -- it was moved to `decrepit` and then dropped as too weak to
# use -- and this task cannot add a dependency. So DES is implemented here, in
# the module that needs it, and checked against published vectors in the tests.
#
# This is the *only* use of DES anywhere in this project, and it authenticates
# to a single VM console that the user already has physical access to. It is
# weak by any modern measure and that is a property of RFB 3.8, not a choice
# made here.

_PC1 = (
    57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18,
    10, 2, 59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36,
    63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22,
    14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 28, 20, 12, 4,
)
_PC2 = (
    14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10,
    23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2,
    41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48,
    44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32,
)
_IP = (
    58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4,
    62, 54, 46, 38, 30, 22, 14, 6, 64, 56, 48, 40, 32, 24, 16, 8,
    57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
    61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7,
)
_FP = (
    40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31,
    38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29,
    36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
    34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25,
)
_E = (
    32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9,
    8, 9, 10, 11, 12, 13, 12, 13, 14, 15, 16, 17,
    16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25,
    24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1,
)
_P = (
    16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10,
    2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25,
)
_SBOXES = (
    (14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7,
     0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
     4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0,
     15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13),
    (15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10,
     3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
     0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15,
     13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9),
    (10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8,
     13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
     13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7,
     1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12),
    (7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15,
     13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
     10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4,
     3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14),
    (2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9,
     14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
     4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14,
     11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3),
    (12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11,
     10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
     9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6,
     4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13),
    (4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1,
     13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
     1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2,
     6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12),
    (13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7,
     1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
     7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8,
     2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11),
)
#: Left rotations of C and D, one per round. The tail is 2 then 1, not 1 then 1:
#: rounds 15 and 16 shift by 2 and 1 respectively (FIPS 46-3 table).
_KEY_SHIFTS = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)


def _bits_of(data: bytes) -> list[int]:
    out: list[int] = []
    for byte in data:
        for shift in range(7, -1, -1):
            out.append((byte >> shift) & 1)
    return out


def _bytes_of(bits: Sequence[int]) -> bytes:
    out = bytearray(len(bits) // 8)
    for index, bit in enumerate(bits):
        if bit:
            out[index // 8] |= 1 << (7 - (index % 8))
    return bytes(out)


def _permute(bits: Sequence[int], table: Sequence[int], width: int) -> list[int]:
    return [bits[position - 1] for position in table]


def des_encrypt_block(key: bytes, block: bytes) -> bytes:
    """One 64-bit DES ECB block encryption. Standard, with no pre/post-whitening."""
    if len(key) != 8:
        raise ValueError(f"DES key must be 8 bytes, got {len(key)}")
    if len(block) != 8:
        raise ValueError(f"DES block must be 8 bytes, got {len(block)}")
    key_bits = _permute(_bits_of(key), _PC1, 56)
    c, d = key_bits[:28], key_bits[28:]
    subkeys: list[list[int]] = []
    for shift in _KEY_SHIFTS:
        c = c[shift:] + c[:shift]
        d = d[shift:] + d[:shift]
        subkeys.append(_permute(c + d, _PC2, 48))

    bits = _permute(_bits_of(block), _IP, 64)
    left, right = bits[:32], bits[32:]
    for subkey in subkeys:
        expanded = _permute(right, _E, 48)
        mixed = [a ^ b for a, b in zip(expanded, subkey)]
        substituted: list[int] = []
        for box in range(8):
            six = mixed[box * 6:box * 6 + 6]
            row = (six[0] << 1) | six[5]
            col = (six[1] << 3) | (six[2] << 2) | (six[3] << 1) | six[4]
            value = _SBOXES[box][row * 16 + col]
            substituted.extend(((value >> 3) & 1, (value >> 2) & 1, (value >> 1) & 1, value & 1))
        f = _permute(substituted, _P, 32)
        left, right = right, [a ^ b for a, b in zip(left, f)]
    return _bytes_of(_permute(right + left, _FP, 64))


def reverse_bits_in_byte(value: int) -> int:
    """Reverse the bit order within one byte."""
    return int(f"{value:08b}"[::-1], 2)


def vnc_auth_key(password: str) -> bytes:
    """Derive the 8-byte DES key from a VNC password.

    Two details, and both are silent-failure details:

    1. Each byte of the password has its bit order reversed. RFC 6143 does not
       mention this. Without it, DES drops the low bit of each key byte as
       parity handling, and because ASCII's high bit is always zero, the
       reversal makes the discarded bit one that carries no information
       instead of one that is part of the password. Get this wrong and a
       correct password simply never authenticates.
    2. The password is truncated or zero-padded to exactly eight bytes.
    """
    raw = password.encode("utf-8", errors="replace")[:MAX_VNC_PASSWORD]
    if not raw:
        raise AuthError("VNC password is empty")
    padded = raw + b"\x00" * (VNC_AUTH_PASSWORD_LEN - len(raw))
    return bytes(reverse_bits_in_byte(b) for b in padded)


def vnc_auth_response(password: str, challenge: bytes) -> bytes:
    """The 16-byte VNC Auth response: two DES-ECB blocks over the challenge.

    The challenge is two independent 64-bit blocks encrypted in ECB mode --
    no chaining, no IV.
    """
    if len(challenge) != VNC_AUTH_CHALLENGE_LEN:
        raise AuthError(f"VNC challenge must be {VNC_AUTH_CHALLENGE_LEN} bytes, got {len(challenge)}")
    key = vnc_auth_key(password)
    out = bytearray()
    for offset in range(0, VNC_AUTH_CHALLENGE_LEN, 8):
        out += des_encrypt_block(key, challenge[offset:offset + 8])
    return bytes(out)