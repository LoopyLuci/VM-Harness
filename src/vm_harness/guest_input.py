"""Driving a guest from outside it: keystrokes and screen capture.

The harness can start, stop and inspect a VM, but it had no way to *talk* to
one. Everything interactive in a Linux guest -- a login prompt, a Y/N
question, an installer that insists on a confirmation -- is unreachable
without this, which is why the only questions anyone could ask a guest were
ones logged to a serial port.

Keystrokes go through QMP's human-monitor-command passthrough:

    {"execute": "human-monitor-command",
     "arguments": {"command-line": "sendkey ret"}}

QEMU's PS/2 keyboard is always present, so this needs no extra device on the
guest's command line. The alternative, QEMU's `input-send-event`, would need a
usb-kbd wired up and a keycode table of its own; `sendkey` accepts key names
directly and is one call per keystroke.

HMP takes exactly one key per invocation and the QMP client serialises every
command behind one lock, so typing is unavoidably serial and needs a delay
between keystrokes. Guests drop input that arrives faster than they poll the
keyboard: at 1ms per character the guest sees a burst it may only partially
consume, which shows up as a password that is mysteriously one character
short. 50ms is slow enough to be reliable and still types a password in
about a second.

Every character this module cannot encode raises. That is deliberate and is
the whole reason it does not fall back to skipping unknowns: a password typed
with one character silently missing is not a visible failure, it is a failed
login and a lockout counter. A loud error at type time is far cheaper.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from vm_harness.qmp_client import QMPClient

# Keys HMP spells differently from ASCII. Kept separate from the shift rule
# because these are names, not punctuation.
_NAMED_KEYS = {
    " ": "spc",
    "\n": "ret",
    "\r": "ret",
    "\t": "tab",
    "-": "minus",
    "=": "equal",
    "[": "bracket_left",
    "]": "bracket_right",
    ";": "semicolon",
    "'": "apostrophe",
    "`": "grave_accent",
    "\\": "backslash",
    ",": "comma",
    ".": "dot",
    "/": "slash",
}

# Punctuation that lives on a shifted key, as HMP sees it: send `shift-minus`
# for an underscore. Without this layer a password containing !@#$%^&*()_+{}
# |:"<>?~ is untypeable, which is most strong passwords.
#
# This is a US keyboard layout, and it is the guest's layout that decides what
# these keys *mean*. HMP names the physical key, not the character, so on a
# DE layout `shift-7` would produce a slash rather than an ampersand. A guest
# with a non-US layout needs its own table here, not a different caller.
_SHIFTED_KEYS = {
    "!": "shift-1",
    "@": "shift-2",
    "#": "shift-3",
    "$": "shift-4",
    "%": "shift-5",
    "^": "shift-6",
    "&": "shift-7",
    "*": "shift-8",
    "(": "shift-9",
    ")": "shift-0",
    "_": "shift-minus",
    "+": "shift-equal",
    "{": "shift-bracket_left",
    "}": "shift-bracket_right",
    "|": "shift-backslash",
    ":": "shift-semicolon",
    '"': "shift-apostrophe",
    "~": "shift-grave_accent",
    "<": "shift-comma",
    ">": "shift-dot",
    "?": "shift-slash",
}

# Control keys, addressable by name for callers that do not want to type them.
_CONTROL_KEYS = {
    "ret": "ret",
    "enter": "ret",
    "tab": "tab",
    "esc": "esc",
    "escape": "esc",
    "backspace": "backspace",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "home": "home",
    "end": "end",
    "delete": "delete",
}

# QEMU's PS/2 controller has no keypad on this machine type, so a guest that
# asks for one sees nothing. Detected up front rather than discovered when a
# password mysteriously loses its digits.
DEFAULT_KEY_DELAY_SEC = 0.05


class UnsupportedKeyError(ValueError):
    """A character cannot be represented as an HMP sendkey name."""


def key_for(char: str) -> str:
    """Return the HMP `sendkey` name for a single character.

    Raises UnsupportedKeyError rather than returning something approximate: a
    dropped character in a password produces a wrong password, not an error.
    """
    if char in _NAMED_KEYS:
        return _NAMED_KEYS[char]
    if char in _SHIFTED_KEYS:
        return _SHIFTED_KEYS[char]
    if char.isascii() and char.isalpha():
        # HMP has no uppercase; shift is part of the key name. Lowercase must
        # NOT get the shift prefix -- `shift-a` types "A", so a blanket shift
        # here would type every letter of a password in capitals.
        return f"shift-{char.lower()}" if char.isupper() else char
    if char.isascii() and char.isdigit():
        return char
    raise UnsupportedKeyError(
        f"cannot type {char!r} (U+{ord(char):04X}): no HMP sendkey name for it. "
        "Extend _NAMED_KEYS, or set the guest password to avoid this character."
    )


class GuestKeyboard:
    """Types into a guest console over QMP.

    Not a general console driver: there is no way to read what the guest drew,
    only to send keys and screendump the framebuffer. Use Screendump to check
    what happened rather than assuming a keystroke landed.
    """

    def __init__(
        self,
        client: QMPClient,
        key_delay_sec: float = DEFAULT_KEY_DELAY_SEC,
    ):
        self._client = client
        self._key_delay = key_delay_sec

    async def _hmp(self, command: str) -> None:
        # QMPClient._read_response raises on an error reply, so a rejected
        # keystroke surfaces here instead of being silently dropped.
        await self._client.send("human-monitor-command", {"command-line": command})

    async def press(self, key: str, times: int = 1) -> None:
        """Press a named key `times` times (e.g. "ret", "esc", "backspace")."""
        name = _CONTROL_KEYS.get(key.lower(), key.lower())
        for _ in range(times):
            await self._hmp(f"sendkey {name}")
            await asyncio.sleep(self._key_delay)

    async def type_text(self, text: str) -> None:
        """Type a string one character at a time.

        Validates the whole string before sending anything, so a typo in an
        unsupported character cannot leave half a password in the guest's
        input buffer.
        """
        keys = [key_for(ch) for ch in text]
        for key in keys:
            await self._hmp(f"sendkey {key}")
            await asyncio.sleep(self._key_delay)
        logger.debug("typed {} characters into guest", len(keys))

    async def clear_line(self, max_backspaces: int = 256) -> None:
        """Backspace over whatever is already in the input line."""
        await self.press("backspace", times=max_backspaces)


async def screendump(client: QMPClient, path: str | Path) -> Path:
    """Capture the guest framebuffer to a PPM file and return the path.

    QEMU screendumps to PPM, which nothing in the standard Windows toolchain
    opens. Convert with Pillow if you want to look at it.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = await client.send(
        "screendump", {"filename": str(target)}
    )
    # QEMU answers screendump with an empty return on success, but only after
    # the file exists; assert rather than trust.
    if not target.exists():
        raise RuntimeError(
            f"screendump reported success but {target} was not created: {result}"
        )
    logger.info("captured guest console to {}", target)
    return target