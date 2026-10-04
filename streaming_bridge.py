"""Per-VM frame + input bridge: QEMU framebuffer in, keystrokes and pointer out.

Run: ``python streaming_bridge.py``

What this serves
----------------
A GUI (or the Android client) opens one WebSocket per VM and receives that VM's
framebuffer as JPEG frames, and sends keystrokes and pointer events back into
that same VM.

    ws://127.0.0.1:8445/ws/stream

Wire protocol (text JSON in both directions, plus binary frames out)::

    client -> {"type":"config","vm":"win11","quality":80,"fps":30,
               "width":1280,"height":800,"input_enabled":true}
    client -> {"type":"subscribe","vm":"win11"}
    client -> {"type":"input","input_type":"key","key":"a","pressed":true}
    client -> {"type":"input","input_type":"mouse_move","x":640,"y":400}
    client -> {"type":"input","input_type":"mouse_click","button":"left","pressed":true}
    client -> {"type":"input","input_type":"scroll","dx":0,"dy":-1}
    client -> {"type":"ping","time":1712345678901}
    client -> {"type":"stats_request"}

    server -> BINARY  raw JPEG bytes: no header, no length prefix, no metadata
    server -> {"type":"config_ack","quality":80,"fps":30,"width":1280,"height":800}
    server -> {"type":"pong","time":1712345678901}
    server -> {"type":"stats","frames_sent":120,"bytes_sent":18347264,"fps":29.8}
    server -> {"type":"vm_list","vms":["win11","ubuntu"]}
    server -> {"type":"error","message":"..."}

Robustness rules the GUI depends on
-----------------------------------
* An unknown ``type`` is ignored. A client one version ahead of the server must
  not lose its connection over a feature the server does not know.
* Malformed JSON produces ``{"type":"error"}`` and the connection stays open. A
  typo must not turn into a reconnect loop.
* A capture or input failure produces ``{"type":"error"}`` too, and the frame
  loop keeps running: a VM that is momentarily paused degrades to a frozen
  picture rather than a dead socket.

Security
--------
The listener binds ``127.0.0.1`` only (``VMHARNESS_BRIDGE_HOST`` overrides it,
and you should not unless you have put authentication in front of it) *and*
every WebSocket must present a token before a single frame or keystroke moves.
See :class:`TokenAuthenticator`. There is no unauthenticated mode; the worst
case is that a token is generated at startup and printed to the log.

What this used to be, and why it is not any more
-----------------------------------------------
An earlier version of this file captured the *host* Windows desktop with
``PIL.ImageGrab`` and injected into the *host* desktop with ``enigo``/``ctypes``,
while binding ``0.0.0.0:8445`` with no authentication and advertising
``CONTINUUM_SERVER = "127.0.0.1:4433"`` that it never connected to. That is a
remote desktop for the host machine wearing a VM tool's name. There is no
``CONTINUUM_SERVER`` here and no ``aioquic``: this speaks QMP and WebSocket, and
that is the whole protocol.

VM registry
-----------
:data:`VMRegistry` reads ``~/.qemu-mcp/vms/*.json`` -- the directory
``vm_harness.hypervisor.qemu.backend`` (``DEFAULT_VMS_DIR``) uses, and the same
one its ``QEMUBackend.screenshot()`` reads ``management_port`` from. Chosen over
``gui.multi_vm.MultiVMManager`` for two reasons:

1. It is the store the QEMU *launcher* writes. ``QEMUBackend._build_qemu_args``
   puts ``-qmp tcp:127.0.0.1:<management_port>`` on the command line from that
   file, so a VM's QMP endpoint is knowable without guessing. ``MultiVMManager``
   keeps its own configs in ``~/.qemu-mcp/vm-configs/`` and its own ``qmp_port``
   counter, which is a *different* number for the same VM.
2. ``gui.multi_vm`` imports PyQt5 and is driven from the Qt event loop. A
   headless asyncio bridge importing a Qt module to enumerate VMs is a deadlock
   waiting to happen, and it would make the bridge unusable without a display.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import queue
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

# This file is a top-level script next to ``src/``, so ``pip install -e .`` may
# not have run. Make ``vm_harness`` importable before reaching for it rather than
# failing with a bare ModuleNotFoundError.
_SRC_DIR = Path(__file__).resolve().parent / "src"
if (_SRC_DIR / "vm_harness" / "__init__.py").is_file() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from aiohttp import WSMsgType, web

from vm_harness.guest_input import (
    DEFAULT_KEY_DELAY_SEC,
    _CONTROL_KEYS,
    GuestKeyboard,
    UnsupportedKeyError,
    key_for,
)
from vm_harness.qmp_client import QMPClient

try:  # Pillow is only needed to transcode PNG -> JPEG; without it, capture fails.
    from PIL import Image
except ImportError:  # pragma: no cover - exercised only on a broken install
    Image = None  # type: ignore[assignment]


log = logging.getLogger("continuum.bridge")


# ── Configuration ──────────────────────────────────────────────────────────────

#: Loopback by default. This listener hands out keystrokes for every VM on the
#: host; a wildcard bind made that reachable from the network with no credential.
BRIDGE_WS_HOST = "127.0.0.1"
BRIDGE_WS_PORT = 8445

HOST_ENV = "VMHARNESS_BRIDGE_HOST"
PORT_ENV = "VMHARNESS_BRIDGE_PORT"
TOKEN_ENV = "VMHARNESS_BRIDGE_TOKEN"
VMS_DIR_ENV = "VMHARNESS_VMS_DIR"
#: QEMU ``id=`` of the ``usb-tablet`` pointer events are routed to. Left unset by
#: default so QEMU picks the VM's only pointing device; set it once the tablet is
#: launched with an explicit id (``-device usb-tablet,id=tablet0``).
TABLET_DEVICE_ENV = "VMHARNESS_BRIDGE_TABLET_DEVICE"

#: Where VM configs live. Same directory as ``hypervisor.qemu.backend.DEFAULT_VMS_DIR``,
#: duplicated here so a non-default install can be pointed at with the env var.
DEFAULT_VMS_DIR = Path.home() / ".qemu-mcp" / "vms"

DEFAULT_QUALITY = 85
MIN_QUALITY = 1
MAX_QUALITY = 100
DEFAULT_FPS = 30
MIN_FPS = 1
#: Smallest remainder worth waiting out. Below this a subscriber is effectively
#: due, so it blocks on the frame queue instead of spinning on a timer that
#: cannot win the race against a screendump.
_MIN_PACING_WAIT_SEC = 0.005
#: A screendump is a socket round trip plus a PNG decode plus a JPEG encode.
#: Asking for more than this cannot make frames arrive faster, only make the
#: server busy; 60 matches the Rust sidecar on 8446 so both honour one config.
MAX_FPS = 60
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
MIN_DIMENSION = 1
MAX_WIDTH = 3840
MAX_HEIGHT = 2160

#: QEMU's absolute pointing device axes are signed 16-bit, so a pixel coordinate
#: maps onto 0..32767. Same constant as the Rust sidecar's ``capture_qmp``.
TABLET_AXIS_MAX = 32767.0

AUTH_TIMEOUT_SEC = 10.0
MAX_WS_MESSAGE = 1024 * 1024
#: QEMU answers ``screendump`` before the file is necessarily complete, so poll
#: rather than read once. Same shape as ``QEMUBackend.screenshot``.
SCREENDUMP_ATTEMPTS = 20
SCREENDUMP_POLL_SEC = 0.05

#: Suppresses the console window Windows would otherwise flash for every
#: ``docker exec``. Only meaningful on Windows; ignored elsewhere.
CREATE_NO_WINDOW = 0x08000000

_LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)

_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_HMP_KEY_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


def _clamp(value: int, low: int, high: int) -> int:
    return low if value < low else high if value > high else value


def _as_int(value: Any) -> Optional[int]:
    """Coerce a JSON value to int, or None if it is not a number.

    ``bool`` is rejected on purpose: ``True`` is an int in Python, and a client
    that sent a boolean where a pixel count belongs sent a bug.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


# ── Authentication ─────────────────────────────────────────────────────────────

class TokenAuthenticator:
    """Decides whether a presented token may open a control session.

    Two sources, both already established in this repository:

    * an ``APIKeyRegistry`` (``vm_harness.api_server``) when one is handed in --
      the same object ``api/routes/streaming.py`` reads out of
      ``request.app["api_key_registry"]`` and asks ``.authenticate(key)``, so a
      key paired for the REST API also opens a stream;
    * one bootstrap token, from ``VMHARNESS_BRIDGE_TOKEN`` or generated at
      startup and logged.

    Comparison is constant time. There is no bypass and no "trust the source
    IP": an unauthenticated socket receives no frames, no keystrokes and no
    error detail beyond "unauthorized".
    """

    def __init__(self, token: str, api_key_registry: Any = None) -> None:
        self._token = token
        self._registry = api_key_registry

    @property
    def token(self) -> str:
        return self._token

    @classmethod
    def create(cls, api_key_registry: Any = None, env: Optional[Dict[str, str]] = None) -> "TokenAuthenticator":
        """Build an authenticator from the environment, generating a token if unset."""
        env = os.environ if env is None else env
        configured = (env.get(TOKEN_ENV) or "").strip()
        if configured:
            return cls(configured, api_key_registry)
        generated = secrets.token_urlsafe(32)
        log.warning(
            "%s is not set; generated a one-off bridge token for this process. "
            "Clients must send {\"type\":\"auth\",\"key\":\"%s\"} (or "
            "?token=...) as the first WebSocket message. Set %s to a fixed value "
            "if you need clients that survive a restart.",
            TOKEN_ENV, generated, TOKEN_ENV,
        )
        return cls(generated, api_key_registry)

    def authenticate(self, secret: Any) -> bool:
        if not isinstance(secret, str) or not secret:
            return False
        if self._registry is not None:
            with contextlib.suppress(Exception):
                if self._registry.authenticate(secret):
                    return True
        return secrets.compare_digest(secret, self._token)


# ── VM registry ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VMTarget:
    """One streamable VM: a name and the QMP endpoint that reaches it."""
    name: str
    qmp_uri: str
    password: str = ""


class VMRegistry:
    """Real per-VM enumeration over ``~/.qemu-mcp/vms/*.json``.

    See the module docstring for why this directory and not ``MultiVMManager``.
    """

    def __init__(self, vms_dir: Optional[os.PathLike[str] | str] = None,
                 env: Optional[Dict[str, str]] = None) -> None:
        env = os.environ if env is None else env
        configured = vms_dir or env.get(VMS_DIR_ENV)
        self._vms_dir = Path(configured) if configured else DEFAULT_VMS_DIR

    @property
    def vms_dir(self) -> Path:
        return self._vms_dir

    def _read(self, path: Path) -> Optional[dict]:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def list_targets(self) -> List[VMTarget]:
        """Every VM in the registry that has a reachable QMP endpoint."""
        targets: List[VMTarget] = []
        if not self._vms_dir.is_dir():
            return targets
        for config_file in sorted(self._vms_dir.glob("*.json")):
            target = self._target_for(config_file)
            if target is not None:
                targets.append(target)
        return targets

    def list_vms(self) -> List[str]:
        """VM names, sorted, for ``{"type":"vm_list"}``."""
        return [target.name for target in self.list_targets()]

    def get(self, name: Any) -> Optional[VMTarget]:
        """Look up one VM by name. Path traversal is not possible: the name is
        matched against the enumerated set, never joined onto the directory."""
        if not isinstance(name, str) or not name:
            return None
        for target in self.list_targets():
            if target.name == name:
                return target
        return None

    def _target_for(self, config_file: Path) -> Optional[VMTarget]:
        data = self._read(config_file)
        if data is None:
            return None
        name = data.get("name") or config_file.stem
        if not isinstance(name, str) or not name:
            return None
        password = data.get("management_password") or ""
        if not isinstance(password, str):
            password = ""

        # A unix-socket QMP endpoint, when one is configured, wins over a port:
        # QEMU's own ``-qmp unix:...`` has no TCP form to fall back to.
        socket_path = data.get("qmp_socket") or data.get("qmp_socket_path")
        if isinstance(socket_path, str) and socket_path:
            return VMTarget(name, f"unix:{socket_path}", password)

        port = _as_int(data.get("management_port"))
        if port is None or port <= 0:
            # Listed but not streamable: the config exists, no monitor is wired
            # up. Excluded from ``vm_list`` so a client cannot pick it and then
            # be told it is unreachable.
            return None
        host = data.get("management_host") or "127.0.0.1"
        if not isinstance(host, str) or not host:
            host = "127.0.0.1"
        return VMTarget(name, f"tcp:{host}:{port}", password)


# ── Input injection (QMP) ──────────────────────────────────────────────────────

def to_axis(pixel: int, extent: int) -> float:
    """Map a pixel coordinate onto QEMU's absolute tablet axis.

    Clamped, not wrapped: a pointer dragged past the edge of the widget belongs
    at the edge of the guest screen, and wrapping sends it to the opposite
    corner. An extent of 1 pixel or less has no meaningful mapping, so it returns
    the axis minimum rather than dividing by ~0.
    """
    if extent <= 1:
        return 0.0
    last = extent - 1
    return (float(_clamp(pixel, 0, last)) / float(last)) * TABLET_AXIS_MAX


def _abs_event(axis: str, value: float) -> dict:
    # QEMU's input-send-event rejects a float here: "Invalid parameter type for
    # 'events[0].data.value', expected: integer". to_axis does fractional
    # scaling to keep pointer motion smooth, so the float has to be rounded at
    # the boundary rather than at the call site -- and it has to be rounded at
    # all, or every mouse event fails and the pointer silently never moves.
    return {"type": "abs", "data": {"axis": axis, "value": int(round(value))}}


def _btn_event(button: str, down: bool) -> dict:
    return {"type": "btn", "data": {"down": down, "button": button}}


def _wheel_event(axis: str, value: int) -> dict:
    return {"type": "wheel", "data": {"axis": axis, "value": abs(value)}}


class UnknownKeyError(ValueError):
    """A key name that is neither a character nor a known control key."""


class QMPInputInjector:
    """Drives one guest's keyboard and pointer over QMP.

    Keys go through HMP's ``sendkey``::

        {"execute":"human-monitor-command",
         "arguments":{"command-line":"sendkey shift-a"}}

    which needs nothing on the guest's command line because QEMU's PS/2
    keyboard is always present. The key names themselves come from
    ``vm_harness.guest_input`` -- ``key_for`` for characters and ``_CONTROL_KEYS``
    for named keys -- because that module has already decided that an unmappable
    character must be a loud error rather than a silently dropped keystroke. A
    password typed one character short is a failed login and a lockout counter,
    not a visible bug.

    Pointer input cannot go through HMP and needs QMP's own protocol::

        {"execute":"input-send-event","arguments":{"events":[
          {"type":"abs","data":{"axis":"x","value":16384.0}},
          {"type":"abs","data":{"axis":"y","value":8192.0}}]}}
        {"execute":"input-send-event","arguments":{"events":[
          {"type":"btn","data":{"down":true,"button":"left"}}]}}
        {"execute":"input-send-event","arguments":{"events":[
          {"type":"wheel","data":{"axis":"up","value":3}}]}}

    REQUIREMENT: the guest must be launched with ``-device usb-tablet``.
    ``input-send-event`` with ``type:"abs"`` is only meaningful for a device that
    declares absolute axes; a PS/2 mouse declares relative ones, so QEMU drops
    the events without reporting an error and the pointer simply never moves.
    The device flag belongs to the QEMU launcher, not to this file.
    """

    def __init__(
        self,
        vm_name: str,
        get_client: Callable[[], Awaitable[Any]],
        tablet_device: Optional[str] = None,
        key_delay_sec: float = DEFAULT_KEY_DELAY_SEC,
    ) -> None:
        self._vm = vm_name
        self._get_client = get_client
        self._tablet_device = tablet_device
        self._key_delay = key_delay_sec
        self._keyboard: Optional[GuestKeyboard] = None
        # Keystroke pacing only holds if consecutive keys really are consecutive,
        # and QMP serialises per socket -- not per caller -- so a per-VM lock is
        # what makes the 50ms guarantee true rather than aspirational.
        self._key_lock = asyncio.Lock()
        # Pointer moves are fire-and-forget at up to the client's mouse polling
        # rate; coalescing to the newest position stops a backlog building up
        # behind a slow QMP round trip.
        self._move_lock = asyncio.Lock()
        self._move_pending: Optional[Tuple[int, int, int, int]] = None
        self._move_task: Optional[asyncio.Task] = None

    def forget_moves(self) -> None:
        """Drop a queued pointer position, e.g. when a client disconnects."""
        self._move_pending = None

    @property
    def vm_name(self) -> str:
        return self._vm

    @property
    def tablet_device(self) -> Optional[str]:
        return self._tablet_device

    # -- keys ---------------------------------------------------------------

    @staticmethod
    def resolve_key(name: Any) -> str:
        """Resolve a client key to an HMP ``sendkey`` name.

        Accepts a single character ("A", "!", " ") or a key name ("ret",
        "escape", "f5", "shift-a"). Raises rather than guessing for anything
        else, for the reason given in the class docstring.
        """
        if not isinstance(name, str) or not name:
            raise UnknownKeyError("empty key name")
        if len(name) == 1:
            # key_for raises UnsupportedKeyError for anything outside printable
            # ASCII, which is exactly the behaviour wanted here.
            return key_for(name)
        lowered = name.lower()
        if lowered in _CONTROL_KEYS:
            return _CONTROL_KEYS[lowered]
        if _HMP_KEY_NAME_RE.match(lowered):
            # Already an HMP key name ("f5", "alt-f4", "shift-a"). Passed
            # through so QEMU can reject it if it does not exist, rather than
            # this module growing a second, divergent key table.
            return lowered
        raise UnknownKeyError(
            f"unknown key {name!r}: send a single character or a key name "
            f"such as 'ret', 'escape' or 'f5'"
        )

    async def key(self, name: Any) -> None:
        """Send one keystroke and wait out the pacing delay.

        ``sendkey`` presses *and* releases in a single call, so a key *release*
        has nothing left to send -- see :meth:`ClientSession._handle_input`,
        which drops it rather than typing the character twice.
        """
        sendkey_name = self.resolve_key(name)
        async with self._key_lock:
            client = await self._get_client()
            keyboard = GuestKeyboard(client, key_delay_sec=self._key_delay)
            await keyboard.press(sendkey_name)

    # -- pointer ------------------------------------------------------------

    async def send_input_events(self, events: List[dict]) -> None:
        """Issue one ``input-send-event``, optionally pinned to the tablet."""
        if not events:
            return
        args: Dict[str, Any] = {"events": events}
        if self._tablet_device:
            args["device"] = self._tablet_device
        client = await self._get_client()
        await client.send("input-send-event", args)

    async def mouse_move(self, x: int, y: int, width: int, height: int) -> None:
        """Move the guest pointer, coalescing to the newest position.

        Both axes travel in one command: a second round trip would draw a
        diagonal twitch in a guest that reads both axes per poll.
        """
        self._move_pending = (int(x), int(y), max(MIN_DIMENSION, int(width)),
                             max(MIN_DIMENSION, int(height)))
        if self._move_task is None or self._move_task.done():
            self._move_task = asyncio.get_running_loop().create_task(self._drain_moves())

    async def _drain_moves(self) -> None:
        async with self._move_lock:
            while self._move_pending is not None:
                x, y, width, height = self._move_pending
                self._move_pending = None
                with contextlib.suppress(Exception):
                    await self.send_input_events([
                        _abs_event("x", to_axis(x, width)),
                        _abs_event("y", to_axis(y, height)),
                    ])

    async def mouse_click(self, button: str, pressed: bool) -> None:
        """Press or release a pointer button.

        Unlike keys, QEMU's ``btn`` events really are directional, so ``pressed``
        is honoured instead of being folded into a synthetic down+up pair.
        """
        name = button.lower() if isinstance(button, str) else "left"
        if name not in ("left", "right", "middle"):
            raise ValueError(f"unknown mouse button {button!r}")
        await self.send_input_events([_btn_event(name, bool(pressed))])

    async def scroll(self, dx: int, dy: int) -> None:
        """Scroll by ``(dx, dy)`` detents. Positive scrolls up / right.

        QEMU's wheel axis is directional *and* signed, so the sign rides on the
        axis name and the value stays a magnitude. Folding the sign into the
        value too scrolls the wrong way on guests that read the axis as the
        direction.
        """
        events: List[dict] = []
        if dy:
            events.append(_wheel_event("up" if dy > 0 else "down", dy))
        if dx:
            events.append(_wheel_event("right" if dx > 0 else "left", dx))
        await self.send_input_events(events)

    async def close(self) -> None:
        if self._move_task is not None and not self._move_task.done():
            self._move_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._move_task


# ── Frame capture (QMP screendump) ────────────────────────────────────────────

def encode_jpeg(image: Any, quality: int, width: int, height: int) -> bytes:
    """Transcode a captured framebuffer to JPEG at the requested size.

    Resized exactly, so a client that asked for 1280x800 gets 1280x800 pixels
    and maps its own coordinates with the same numbers it was given in
    ``config_ack``. A frame already at the requested size is not resized at all.
    """
    if Image is None:  # pragma: no cover - broken install
        raise RuntimeError("Pillow is required to encode frames (pip install Pillow)")
    frame = image
    if frame.size != (width, height):
        frame = frame.resize((width, height), _LANCZOS)
    if frame.mode != "RGB":
        frame = frame.convert("RGB")
    buffer = io.BytesIO()
    # optimize=False: it costs a second entropy-coding pass per frame for a few
    # percent of size, at up to 60 frames per second per client.
    frame.save(buffer, format="JPEG", quality=_clamp(quality, MIN_QUALITY, MAX_QUALITY))
    return buffer.getvalue()


@dataclass(frozen=True)
class Frame:
    """One captured framebuffer, shared read-only by every subscriber."""
    image: Any
    seq: int
    captured_at: float


class JpegEncoder:
    """Serialises transcoding per VM.

    Every subscriber resizes the *same* ``Image`` object, and Pillow's decoder
    state on an image is not re-entrant, so concurrent resizes of one frame from
    several client threads are a real hazard rather than a theoretical one. A
    millisecond-scale lock is cheaper than the alternative -- one screendump per
    client -- which is the whole reason this class exists.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def encode(self, image: Any, quality: int, width: int, height: int) -> bytes:
        with self._lock:
            return encode_jpeg(image, quality, width, height)


class VMStreamSource:
    """One capture and one injector per VM, shared by every client watching it.

    A ``screendump`` is a socket round trip plus a PNG decode; doing one per
    client is what made the previous "one capture, N senders" design wrong in
    the other direction. Here N clients on one VM share a single QMP connection,
    a single capture loop and a single decoded frame, and each client transcodes
    that frame at its own quality and size. Per-client config is therefore
    genuinely per client -- it is never written back onto shared state, so the
    last client's config cannot change what anyone else sees.

    The one piece of shared state is the *capture rate*, which is the fastest
    subscriber's fps. A shared capture can only run once; each subscriber then
    paces its own sends down to what it asked for.
    """

    def __init__(
        self,
        target: VMTarget,
        client_factory: Optional[Callable[[str], Any]] = None,
        capture_fps: int = MAX_FPS,
        tablet_device: Optional[str] = None,
    ) -> None:
        self.target = target
        self._client_factory = client_factory or (lambda uri: QMPClient(uri))
        self._capture_fps_cap = _clamp(capture_fps, MIN_FPS, MAX_FPS)
        self._client: Any = None
        self._client_lock = asyncio.Lock()
        # One screendump in flight at a time. The temp filename is unique per
        # capture anyway, but overlapping screendumps on one QMP socket would
        # still serialise inside QMPClient and only make the queue longer.
        self._capture_lock = asyncio.Lock()
        # Subscriber queue -> the fps that queue asked for. Derived from the
        # whole subscriber set rather than written by whichever client spoke
        # last: that is the difference between "the fastest viewer sets the
        # capture rate" and "the last client's config wins for everyone".
        self._subscribers: Dict[asyncio.Queue, int] = {}
        self._encoder = JpegEncoder()
        self._pump: Optional[asyncio.Task] = None
        self._seq = 0
        self.injector = QMPInputInjector(target.name, self.client, tablet_device)

    # -- QMP connection -----------------------------------------------------

    async def client(self) -> Any:
        """The VM's QMP client, connecting on first use.

        One connection per VM for both capture and input: a VM is reached with
        one handshake rather than two, and QMP's own requirement is that a
        socket is used by one caller at a time, which its internal lock provides.
        """
        client = self._client
        if client is not None and getattr(client, "is_connected", True):
            return client
        async with self._client_lock:
            client = self._client
            if client is not None and getattr(client, "is_connected", True):
                return client
            new_client = self._client_factory(self.target.qmp_uri)
            password = self.target.password or None
            if password and hasattr(new_client, "password"):
                new_client.password = password
            await new_client.connect()
            self._client = new_client
            log.info("QMP connected for VM %s at %s", self.target.name, self.target.qmp_uri)
            return new_client

    async def _discard_client(self) -> None:
        async with self._client_lock:
            client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()

    # -- capture ------------------------------------------------------------

    async def capture_image(self) -> Any:
        """One ``screendump``, decoded and ready to hand to N subscribers.

        QEMU writes the file itself, so the path must be one QEMU can reach on
        this host. Unique per capture (``mkstemp``) because two VMs -- or the
        same VM on two bridges -- must not write the same file, and deleted
        immediately after reading so a long-lived bridge does not accumulate
        framebuffers in %TEMP%.
        """
        if Image is None:  # pragma: no cover - broken install
            raise RuntimeError("Pillow is required to capture frames (pip install Pillow)")
        async with self._capture_lock:
            client = await self.client()
            handle, filename = tempfile.mkstemp(
                prefix=f"vmharness-{self.target.name}-{os.getpid()}-", suffix=".png"
            )
            os.close(handle)
            path = Path(filename)
            try:
                await client.send("screendump", {"filename": filename, "format": "png"})
                # QEMU answers before the file is necessarily complete.
                for _ in range(SCREENDUMP_ATTEMPTS):
                    if path.exists() and path.stat().st_size > 0:
                        break
                    await asyncio.sleep(SCREENDUMP_POLL_SEC)
                else:
                    raise RuntimeError(
                        f"screendump for VM {self.target.name} produced no data at {filename}"
                    )
                data = path.read_bytes()
            finally:
                with contextlib.suppress(OSError):
                    path.unlink()

            image = Image.open(io.BytesIO(data))
            # Fully decode here, once, so N subscribers resizing the result in
            # N threads never race Pillow's lazy decoder state.
            image.load()
            return image

    async def capture_frame(self) -> Frame:
        image = await self.capture_image()
        self._seq += 1
        return Frame(image=image, seq=self._seq, captured_at=time.monotonic())

    # -- subscribers --------------------------------------------------------

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def encoder(self) -> JpegEncoder:
        return self._encoder

    def new_queue(self) -> asyncio.Queue:
        """A fresh, latest-wins subscriber queue for this source."""
        return asyncio.Queue(maxsize=1)

    def subscribe(self, queue: asyncio.Queue, fps: int = MAX_FPS) -> None:
        """Attach a subscriber queue, starting the capture loop if it is idle.

        The loop is started and stopped with demand: a VM nobody is watching
        costs no screendumps.
        """
        self._subscribers[queue] = _clamp(int(fps), MIN_FPS, MAX_FPS)
        if self._pump is None or self._pump.done():
            self._pump = asyncio.get_running_loop().create_task(self._run())

    def resubscribe(self, queue: asyncio.Queue, fps: int) -> None:
        """Record a new rate for an already attached subscriber."""
        self._subscribers[queue] = _clamp(int(fps), MIN_FPS, MAX_FPS)

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.pop(queue, None)
        if not self._subscribers and self._pump is not None and not self._pump.done():
            self._pump.cancel()
            self._pump = None

    def _publish(self, kind: str, payload: Any) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                # Latest frame wins. Keeping a backlog would deliver frames long
                # after they were captured -- the "re-send the cached frame every
                # tick" behaviour this replaced.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait((kind, payload))

    def target_fps(self) -> int:
        """Capture rate: the fastest subscriber, never above the cap.

        A single shared capture cannot run at once per client, so it runs at the
        rate the most impatient viewer needs; everyone slower than that gets
        fewer frames and paces their own sends.
        """
        if not self._subscribers:
            return MIN_FPS
        return max(MIN_FPS, min(max(self._subscribers.values()), self._capture_fps_cap))

    async def _run(self) -> None:
        while self._subscribers:
            started = time.monotonic()
            try:
                frame = await self.capture_frame()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("capture failed for VM %s: %s", self.target.name, exc)
                # Report and keep the loop alive: a paused VM should freeze the
                # picture, not disconnect every client watching it.
                self._publish("error", f"capture failed: {exc}")
                if not getattr(self._client, "is_connected", False):
                    await self._discard_client()
            else:
                self._publish("frame", frame)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.0, (1.0 / self.target_fps()) - elapsed))

    async def close(self) -> None:
        if self._pump is not None and not self._pump.done():
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump
        self._pump = None
        self._subscribers.clear()
        await self.injector.close()
        await self._discard_client()


class FrameSubscriber:
    """One client's view of a :class:`VMStreamSource`.

    Pulls shared frames off a latest-wins queue and sends each one at most once,
    at the rate that client asked for. Frames that arrive while this client is
    still inside its pacing interval are *not* sent late: they are superseded by
    the newer frame that is already queued. Re-sending a stale picture at the
    client's frame rate makes a slow capture look like a fast one.
    """

    def __init__(
        self,
        source: VMStreamSource,
        send: Callable[[bytes], Awaitable[None]],
        on_error: Callable[[str], Awaitable[None]],
        fps: int = DEFAULT_FPS,
        quality: int = DEFAULT_QUALITY,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
    ) -> None:
        self.source = source
        self._send = send
        self._on_error = on_error
        self.fps = _clamp(int(fps), MIN_FPS, MAX_FPS)
        self.quality = _clamp(int(quality), MIN_QUALITY, MAX_QUALITY)
        self.width = max(MIN_DIMENSION, int(width))
        self.height = max(MIN_DIMENSION, int(height))
        self.queue = source.new_queue()
        self._task: Optional[asyncio.Task] = None
        self._last_send: Optional[float] = None

    @property
    def interval(self) -> float:
        return 1.0 / float(self.fps)

    def configure(self, fps: int, quality: int, width: int, height: int) -> None:
        self.fps = _clamp(int(fps), MIN_FPS, MAX_FPS)
        self.quality = _clamp(int(quality), MIN_QUALITY, MAX_QUALITY)
        self.width = max(MIN_DIMENSION, int(width))
        self.height = max(MIN_DIMENSION, int(height))
        # Let the shared capture know this viewer now wants a different rate, so
        # the capture rate tracks the subscriber set instead of the last speaker.
        self.source.resubscribe(self.queue, self.fps)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self.run())

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        self.source.unsubscribe(self.queue)

    async def run(self) -> None:
        while True:
            # How long until this client is allowed its next frame.
            #
            # When that time has already passed the next frame is due *now*, so
            # block until one exists rather than waiting out a remainder of
            # zero. Passing timeout=0 to wait_for is the trap: it raises
            # TimeoutError before the inner queue.get() ever gets to run, and
            # because _last_send only advances on a successful send, every
            # later iteration recomputes the same zero. The result is a busy
            # loop that spins hundreds of thousands of times a second and never
            # delivers another frame -- measured at 189k timeouts in 6s, with
            # the client stuck on frame 1. Clamping the remainder to zero and
            # treating it as "wait for zero" is what made a high requested fps
            # deliver exactly one frame.
            wait_for: Optional[float] = None
            if self._last_send is not None:
                wait_for = self.interval - (time.monotonic() - self._last_send)
                # Due now, or so close that timing out would just churn: a
                # requested fps above what the capture can actually deliver
                # otherwise spends thousands of iterations per second timing out
                # on sub-millisecond remainders while waiting for a screendump
                # that takes tens of milliseconds.
                if wait_for <= _MIN_PACING_WAIT_SEC:
                    wait_for = None  # due now: block on the queue instead
            try:
                if wait_for is None:
                    kind, payload = await self.queue.get()
                else:
                    kind, payload = await asyncio.wait_for(
                        self.queue.get(), timeout=wait_for
                    )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                continue
            if kind == "error":
                with contextlib.suppress(Exception):
                    await self._on_error(str(payload))
                continue
            frame: Frame = payload
            try:
                jpeg = await asyncio.to_thread(
                    self.source.encoder.encode, frame.image, self.quality, self.width, self.height
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("transcode failed for VM %s: %s", self.source.target.name, exc)
                continue
            await self._send(jpeg)
            self._last_send = time.monotonic()


# ── Container terminal (persistent docker exec) ───────────────────────────────

class DockerExecSession:
    """One long-lived ``docker exec <container> /bin/sh`` process.

    Every command the client sends is written into that one shell and the output
    is read back off a queue fed by a reader thread, delimited by a marker the
    shell prints itself. The previous implementation shelled out to a *new*
    ``subprocess.run(["docker","exec",...])`` per message with a 30 second
    timeout: a new container exec for every keystroke of an interactive session,
    which is both slow and a much wider door than a shell someone already holds.
    """

    def __init__(self, container_name: str, shell: str = "/bin/sh") -> None:
        self.container_name = container_name
        self.shell = shell
        self.process: Optional[subprocess.Popen] = None
        self._running = False
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._reader: Optional[threading.Thread] = None

    async def start(self) -> None:
        """Start the exec process. The blocking Popen happens off-loop."""
        loop = asyncio.get_running_loop()
        self.process = await loop.run_in_executor(None, self._spawn)
        self._running = True
        self._reader = threading.Thread(
            target=self._read_output, name=f"docker-exec-{self.container_name}", daemon=True
        )
        self._reader.start()
        log.info("docker exec session opened for %s", self.container_name)

    def _spawn(self) -> subprocess.Popen:
        return subprocess.Popen(
            ["docker", "exec", "-i", self.container_name, self.shell],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            universal_newlines=True,
            # Without this every command flashes a console window on Windows.
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    def _read_output(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                if not self._running:
                    break
                self._queue.put(line)
        except (ValueError, OSError):
            pass
        finally:
            self._running = False

    @property
    def is_running(self) -> bool:
        return self._running and self.process is not None and self.process.poll() is None

    async def write(self, line: str) -> None:
        """Write one line to the shell. Never blocks the event loop."""
        process = self.process
        if process is None or process.stdin is None:
            raise RuntimeError(f"docker exec session for {self.container_name} is not open")
        payload = (line + "\n").encode("utf-8", errors="replace")
        await asyncio.to_thread(_write_all, process.stdin, payload)

    async def _drain(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    async def run_command(self, command: str, timeout: float = 15.0) -> str:
        """Run ``command`` in the live shell and return its output.

        The marker is printed *by the shell*, not guessed at client side: a
        command that prints something resembling the marker cannot terminate the
        read early, because the marker is a fresh uuid each time.

        Output collection polls a queue with a bounded wait, so the event loop
        is never parked on a blocking read.
        """
        if not self.is_running:
            raise RuntimeError(f"docker exec session for {self.container_name} is not running")
        await self._drain()
        marker = f"__vmharness_{uuid.uuid4().hex}__"
        await self.write(f"{command}; printf '\\n{marker}\\n'")

        lines: List[str] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                lines.append(f"[timed out after {timeout:g}s]\n")
                break
            try:
                line = await asyncio.to_thread(self._queue.get, True, min(0.1, remaining))
            except queue.Empty:
                continue
            if line.strip() == marker:
                break
            lines.append(line)
        return "".join(lines)

    async def stop(self) -> None:
        self._running = False
        process, self.process = self.process, None
        if process is None:
            return
        with contextlib.suppress(Exception):
            process.terminate()
        # Reap the child without blocking the loop past a couple of seconds.
        for _ in range(20):
            if process.poll() is not None:
                break
            await asyncio.sleep(0.1)
        if process.poll() is None:
            with contextlib.suppress(Exception):
                process.kill()


def _write_all(stream: Any, payload: bytes) -> None:
    stream.write(payload)
    stream.flush()


class ExecSessionManager:
    """Keeps one :class:`DockerExecSession` per container."""

    def __init__(self) -> None:
        self._sessions: Dict[str, DockerExecSession] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, container_name: str) -> DockerExecSession:
        async with self._lock:
            session = self._sessions.get(container_name)
            if session is None or not session.is_running:
                if session is not None:
                    await session.stop()
                session = DockerExecSession(container_name)
                await session.start()
                self._sessions[container_name] = session
            return session

    async def run_command(self, container_name: str, command: str) -> str:
        session = await self.get_or_create(container_name)
        return await session.run_command(command)

    async def close(self, container_name: str) -> None:
        async with self._lock:
            session = self._sessions.pop(container_name, None)
        if session is not None:
            await session.stop()

    async def close_all(self) -> None:
        for name in list(self._sessions):
            await self.close(name)


# ── WebSocket server ──────────────────────────────────────────────────────────

@dataclass
class ClientSession:
    """Per-connection state. Nothing in here is shared with other clients."""

    client_id: str
    ws: web.WebSocketResponse
    quality: int = DEFAULT_QUALITY
    fps: int = DEFAULT_FPS
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    input_enabled: bool = True
    vm: Optional[str] = None
    source: Optional[VMStreamSource] = None
    subscriber: Optional[FrameSubscriber] = None
    frames_sent: int = 0
    bytes_sent: int = 0
    sent_at: List[float] = field(default_factory=list)
    # aiohttp writes to the transport with an await in the middle, so two
    # coroutines sending concurrently can interleave half a frame each. Every
    # send in this module goes through this lock.
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    input_tasks: Set[asyncio.Task] = field(default_factory=set)

    @property
    def fps_actual(self) -> float:
        if len(self.sent_at) < 2:
            return 0.0
        span = self.sent_at[-1] - self.sent_at[0]
        return round((len(self.sent_at) - 1) / span, 1) if span > 0 else 0.0


class StreamingBridge:
    """The aiohttp application: one authenticated stream per client, one VM."""

    def __init__(
        self,
        registry: Optional[VMRegistry] = None,
        authenticator: Optional[TokenAuthenticator] = None,
        api_key_registry: Any = None,
        client_factory: Optional[Callable[[str], Any]] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        vms_dir: Optional[os.PathLike[str] | str] = None,
        exec_sessions: Optional[ExecSessionManager] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        env = os.environ if env is None else env
        self.registry = registry or VMRegistry(vms_dir=vms_dir, env=env)
        self.auth = authenticator or TokenAuthenticator.create(api_key_registry, env)
        self._client_factory = client_factory
        self.host = host if host is not None else (env.get(HOST_ENV) or BRIDGE_WS_HOST)
        self.port = port if port is not None else _as_int(env.get(PORT_ENV)) or BRIDGE_WS_PORT
        self._tablet_device = env.get(TABLET_DEVICE_ENV) or None
        self.exec_sessions = exec_sessions or ExecSessionManager()
        self.clients: Dict[str, ClientSession] = {}
        self.sources: Dict[str, VMStreamSource] = {}
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._running = False

    # -- lifecycle ----------------------------------------------------------

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/ws/stream", self._handle_ws)
        app.router.add_get("/terminal/{container}", self._handle_terminal_ws)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/vms", self._handle_vms)
        return app

    async def start(self) -> None:
        """Bind and begin serving. Returns; it does not block the caller."""
        app = self.build_app()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        self._running = True
        log.info(
            "streaming bridge listening on ws://%s:%d/ws/stream (token required)", self.host, self.port
        )

    async def serve_forever(self) -> None:
        while self._running:
            await asyncio.sleep(1.0)

    async def stop(self) -> None:
        self._running = False
        for session in list(self.clients.values()):
            self._detach(session)
            with contextlib.suppress(Exception):
                await session.ws.close()
        self.clients.clear()
        for source in list(self.sources.values()):
            await source.close()
        self.sources.clear()
        await self.exec_sessions.close_all()
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- per-VM sources -----------------------------------------------------

    def source_for(self, target: VMTarget) -> VMStreamSource:
        """The one capture+injector for ``target``, created on first use."""
        source = self.sources.get(target.name)
        if source is None:
            source = VMStreamSource(
                target, client_factory=self._client_factory,
                tablet_device=self._tablet_device,
            )
            self.sources[target.name] = source
        return source

    # -- stream endpoint ----------------------------------------------------

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30.0, autoping=True, max_msg_size=MAX_WS_MESSAGE)
        await ws.prepare(request)
        session = ClientSession(
            client_id=request.query.get("client_id") or f"client_{uuid.uuid4().hex[:8]}",
            ws=ws,
        )

        # ── Authentication gate ────────────────────────────────────────────
        # Nothing above this line has subscribed to a frame source or touched an
        # injector, and nothing below it runs until a token has been accepted.
        # A token may arrive as ``?token=`` (for clients that cannot send a first
        # message) or as an auth message; both go through the same check.
        token = request.query.get("token", "")
        if token and self.auth.authenticate(token):
            await self._send_json(session, {"type": "auth_ok"})
        else:
            if not await self._authenticate(session, request):
                return ws

        # Only an authenticated client is ever visible to the rest of the server.
        self.clients[session.client_id] = session
        log.info("stream client connected: %s", session.client_id)
        try:
            await self._run_stream(session)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("stream client %s failed: %s", session.client_id, exc)
        finally:
            self.clients.pop(session.client_id, None)
            self._detach(session)
            log.info("stream client disconnected: %s", session.client_id)
        return ws

    async def _authenticate(self, session: ClientSession, request: web.Request) -> bool:
        """Require the first message to be an auth message. False == hang up."""
        try:
            msg = await asyncio.wait_for(session.ws.receive(), timeout=AUTH_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            await self._send_json(session, {
                "type": "error",
                "message": 'authentication required: send {"type":"auth","key":"<token>"} first',
            })
            await session.ws.close()
            return False
        if msg.type != WSMsgType.TEXT:
            await self._send_json(session, {
                "type": "error",
                "message": 'authentication must be a JSON text message: '
                           '{"type":"auth","key":"<token>"}',
            })
            await session.ws.close()
            return False
        try:
            payload = json.loads(msg.data)
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, dict) or payload.get("type") != "auth":
            await self._send_json(session, {
                "type": "error",
                "message": 'authentication required: the first message must be '
                           '{"type":"auth","key":"<token>"}',
            })
            await session.ws.close()
            return False
        if not self.auth.authenticate(payload.get("key")):
            peer = "unknown"
            with contextlib.suppress(Exception):
                peer = request.transport.get_extra_info("peername")[0]  # type: ignore[union-attr]
            log.warning("rejected unauthenticated stream client from %s", peer)
            await self._send_json(session, {"type": "error", "message": "unauthorized"})
            await session.ws.close()
            return False
        await self._send_json(session, {"type": "auth_ok"})
        return True

    async def _run_stream(self, session: ClientSession) -> None:
        async for msg in session.ws:
            if msg.type == WSMsgType.TEXT:
                await self._handle_message(session, msg.data)
            elif msg.type == WSMsgType.ERROR:
                log.warning("stream client %s socket error: %s", session.client_id, session.ws.exception())
                break
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                break

    async def _handle_message(self, session: ClientSession, data: str) -> None:
        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            # Stay open: a malformed frame is a client bug, and hanging up turns
            # a typo into a reconnect loop.
            await self._send_error(session, f"malformed JSON: {data[:120]!r}")
            return
        if not isinstance(message, dict):
            await self._send_error(session, "message must be a JSON object")
            return

        msg_type = message.get("type")
        if msg_type == "config":
            await self._handle_config(session, message)
        elif msg_type == "subscribe":
            await self._attach(session, message.get("vm"))
        elif msg_type == "input":
            self._handle_input(session, message)
        elif msg_type == "ping":
            await self._send_json(session, {"type": "pong", "time": message.get("time", 0)})
        elif msg_type == "stats_request":
            await self._send_json(session, {
                "type": "stats",
                "frames_sent": session.frames_sent,
                "bytes_sent": session.bytes_sent,
                "fps": session.fps_actual,
            })
        elif msg_type == "vm_list":
            await self._send_json(session, {"type": "vm_list", "vms": self.registry.list_vms()})
        else:
            # Unknown types are ignored, not errors: a client a version ahead of
            # the server must not lose its connection over a feature the server
            # does not implement.
            log.debug("ignoring unrecognised message type %r from %s", msg_type, session.client_id)

    async def _handle_config(self, session: ClientSession, message: dict) -> None:
        """Apply per-client config. Nothing here touches another session."""
        violation: Optional[str] = None
        values = {}
        for field, default, low, high in (
            ("quality", DEFAULT_QUALITY, MIN_QUALITY, MAX_QUALITY),
            ("fps", DEFAULT_FPS, MIN_FPS, MAX_FPS),
            ("width", DEFAULT_WIDTH, MIN_DIMENSION, MAX_WIDTH),
            ("height", DEFAULT_HEIGHT, MIN_DIMENSION, MAX_HEIGHT),
        ):
            if field not in message:
                values[field] = getattr(session, field)
                continue
            number = _as_int(message[field])
            if number is None:
                violation = f"config.{field} must be an integer, got {message[field]!r}"
                break
            if number < 0:
                # A negative size is not a preference to clamp, it is a client
                # that disagrees with itself about the shape of its own payload.
                violation = f"config.{field} must not be negative, got {number}"
                break
            values[field] = max(low, min(high, number))

        if message.get("input_enabled") is not None:
            session.input_enabled = bool(message["input_enabled"])

        if violation is None:
            session.quality = values["quality"]
            session.fps = values["fps"]
            session.width = values["width"]
            session.height = values["height"]
            if session.subscriber is not None:
                # Only this client's transcoder changes. The shared capture keeps
                # running at its own rate and nobody else's view moves.
                session.subscriber.configure(
                    session.fps, session.quality, session.width, session.height
                )

        # Acknowledge first, complain second: the client has already applied what
        # it asked for locally and must not be left waiting on a config_ack. On a
        # violation the ack carries the values still in force, so it is honest
        # about what the server is doing rather than pretending the bad values
        # took effect.
        await self._send_json(session, {
            "type": "config_ack",
            "quality": session.quality,
            "fps": session.fps,
            "width": session.width,
            "height": session.height,
        })
        if violation is not None:
            await self._send_error(session, violation)

        vm = message.get("vm")
        if isinstance(vm, str) and vm:
            await self._attach(session, vm)

    async def _attach(self, session: ClientSession, vm: Any) -> None:
        """Point this client at a VM, starting a shared capture if it is idle."""
        target = self.registry.get(vm)
        if target is None:
            await self._send_error(session, f"unknown VM {vm!r}; known VMs are {self.registry.list_vms()}")
            return
        source = self.source_for(target)
        if session.subscriber is not None:
            await session.subscriber.stop()
            session.subscriber = None
        subscriber = FrameSubscriber(
            source,
            send=lambda jpeg: self._send_frame(session, jpeg),
            on_error=lambda message: self._send_error(session, message),
            fps=session.fps,
            quality=session.quality,
            width=session.width,
            height=session.height,
        )
        session.source = source
        session.subscriber = subscriber
        session.vm = target.name
        source.subscribe(subscriber.queue, subscriber.fps)
        subscriber.start()
        log.info("client %s attached to VM %s", session.client_id, target.name)

    def _detach(self, session: ClientSession) -> None:
        for task in list(session.input_tasks):
            task.cancel()
        session.input_tasks.clear()
        subscriber = session.subscriber
        session.subscriber = None
        if subscriber is not None:
            # Cancels the subscriber task and, importantly, drops this queue from
            # the source: a source nobody is watching stops capturing.
            try:
                asyncio.get_running_loop().create_task(subscriber.stop())
            except RuntimeError:  # pragma: no cover - interpreter shutdown
                subscriber.source.unsubscribe(subscriber.queue)
        if session.source is not None:
            session.source.injector.forget_moves()
        session.source = None
        session.vm = None

    def _handle_input(self, session: ClientSession, message: dict) -> None:
        """Translate and queue one input event.

        Never awaited inline: QMP is a socket round trip, and blocking the
        reader loop on it would also delay this client's pongs.
        """
        if session.source is None:
            self._spawn_input(
                session,
                self._send_error(session, "input received before a subscribe: nothing to inject into"),
            )
            return
        if not session.input_enabled:
            self._spawn_input(session, self._send_error(
                session, "input is disabled for this session (config.input_enabled was false)"))
            return

        injector = session.source.injector
        input_type = message.get("input_type")
        input_type = input_type.lower() if isinstance(input_type, str) else ""

        # `sendkey` presses and releases atomically, so a key release has nothing
        # left to send. Treated as a tap it would type every character twice for a
        # client that sends press/release pairs.
        if input_type == "key" and message.get("pressed") is False:
            return

        if input_type == "key":
            coro = injector.key(message.get("key"))
        elif input_type == "mouse_move":
            coro = injector.mouse_move(
                _as_int(message.get("x")) or 0,
                _as_int(message.get("y")) or 0,
                session.width,
                session.height,
            )
        elif input_type == "mouse_click":
            coro = injector.mouse_click(
                message.get("button", "left"), bool(message.get("pressed", True))
            )
        elif input_type == "scroll":
            coro = injector.scroll(
                _as_int(message.get("dx")) or 0, _as_int(message.get("dy")) or 0
            )
        else:
            coro = self._send_error(session, f"unsupported input_type {input_type!r}")
        self._spawn_input(session, coro)

    def _spawn_input(self, session: ClientSession, coro: Awaitable[Any]) -> None:
        task = asyncio.get_running_loop().create_task(self._run_input(session, coro))
        session.input_tasks.add(task)
        task.add_done_callback(session.input_tasks.discard)

    async def _run_input(self, session: ClientSession, coro: Awaitable[Any]) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except (UnsupportedKeyError, UnknownKeyError, ValueError) as exc:
            await self._send_error(session, str(exc))
        except Exception as exc:
            log.warning("input failed for client %s: %s", session.client_id, exc)
            await self._send_error(session, f"input failed: {exc}")

    # -- sends --------------------------------------------------------------

    async def _send_json(self, session: ClientSession, payload: dict) -> None:
        async with session.send_lock:
            with contextlib.suppress(ConnectionError, RuntimeError):
                await session.ws.send_str(json.dumps(payload))

    async def _send_error(self, session: ClientSession, message: str) -> None:
        await self._send_json(session, {"type": "error", "message": message})

    async def _send_frame(self, session: ClientSession, jpeg: bytes) -> None:
        async with session.send_lock:
            try:
                await session.ws.send_bytes(jpeg)
            except (ConnectionError, RuntimeError):
                return
        session.frames_sent += 1
        session.bytes_sent += len(jpeg)
        session.sent_at.append(time.monotonic())
        if len(session.sent_at) > 60:
            del session.sent_at[:-60]

    # -- terminal endpoint --------------------------------------------------

    async def _handle_terminal_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Interactive shell in a container, over one persistent ``docker exec``.

        Client sends ``{"command":"ls -la"}``; server replies
        ``{"output":"..."}``. The same token gate as the stream applies: an
        unauthenticated ``docker exec`` next to an authenticated frame stream
        would make the frame stream's authentication decorative.
        """
        container = request.match_info.get("container", "")
        if not _CONTAINER_NAME_RE.match(container or ""):
            return web.json_response({"error": "invalid container name"}, status=400)

        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=MAX_WS_MESSAGE)
        await ws.prepare(request)

        session = ClientSession(
            client_id=f"terminal_{container}_{uuid.uuid4().hex[:6]}", ws=ws
        )
        token = request.query.get("token", "")
        if not (token and self.auth.authenticate(token)):
            if not await self._authenticate(session, request):
                return ws

        log.info("terminal WebSocket connected for %s", container)
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    await self._send_json(session, {"error": "malformed JSON"})
                    continue
                command = payload.get("command", "") if isinstance(payload, dict) else ""
                if not isinstance(command, str) or not command.strip():
                    await self._send_json(session, {"error": "missing 'command'"})
                    continue
                try:
                    output = await self.exec_sessions.run_command(container, command)
                except Exception as exc:
                    await self._send_json(session, {"error": str(exc)})
                    continue
                await self._send_json(session, {"output": output})
        finally:
            log.info("terminal WebSocket disconnected for %s", container)
        return ws

    # -- plain HTTP endpoints -----------------------------------------------

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "clients": len(self.clients),
            "vms": self.registry.list_vms(),
            "streams": len(self.sources),
            "running": self._running,
        })

    async def _handle_vms(self, request: web.Request) -> web.Response:
        return web.json_response({
            "vms": [
                {"name": target.name, "qmp_uri": target.qmp_uri}
                for target in self.registry.list_targets()
            ]
        })


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    bridge = StreamingBridge()
    await bridge.start()
    try:
        await bridge.serve_forever()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await bridge.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
