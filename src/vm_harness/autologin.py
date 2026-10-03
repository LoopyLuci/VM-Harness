"""Signing in to a guest console on the harness's behalf.

An unattended install ends at a login prompt, and a Linux guest that has only
ever been watched through a serial log cannot be checked any further: the
desktop either came up or it did not, and the only evidence is a framebuffer
nobody is reading. Typing a username and password at that prompt needs the
credentials to be readable without a human present, which is what
`gui/dialogs_vm_login.py` stores.

This module does the typing. It takes the credentials as arguments and knows
nothing about where they were stored, so the secret stays out of the
operation catalog -- ops are dispatched, logged and exposed over MCP and HTTP,
and a password is the last thing that should be riding along in an argument
dict. Read the credential in the caller, pass it here.

Signing in is reported, never assumed. The only way to know a login worked is
to look at the screen afterwards, so `login()` screendumps before and after and
returns both paths. Compare them: identical frames mean the password was
rejected and the prompt simply redrew itself.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from vm_harness.guest_input import GuestKeyboard, screendump
from vm_harness.qmp_client import QMPClient

# Long enough for a desktop to start appearing, short enough that a wedged
# login is reported rather than waited on. Measured on the Omarchy VM: the
# greeter takes a few seconds to hand off and Hyprland several more.
DEFAULT_SETTLE_SEC = 25.0


@dataclass
class ConsoleLogin:
    """What to type, and what to expect when it lands."""

    username: str
    password: str
    # Press Tab first to reach the password field. Greeters vary: some
    # pre-select the username, some want it typed, some show only a password
    # box for the single account they know about.
    type_username: bool = True
    settle_sec: float = DEFAULT_SETTLE_SEC
    shot_dir: Path = field(default_factory=lambda: Path("vm"))


async def _shot(client: QMPClient, path: Path) -> Path | None:
    try:
        return await screendump(client, path)
    except Exception as exc:  # noqa: BLE001 - evidence is best-effort
        # A missing screenshot must not abort a login that may well have
        # worked; it only means the caller loses its before/after comparison.
        logger.warning("could not capture {}: {}", path.name, exc)
        return None


async def login(client: QMPClient, creds: ConsoleLogin, stamp: str = "") -> dict:
    """Type `creds` at the guest's login prompt and capture the result.

    Returns before/after screendump paths plus whether the screen changed, so
    the caller can decide whether the login actually took rather than trusting
    a keystroke count.
    """
    suffix = f"-{stamp}" if stamp else ""
    before = await _shot(client, creds.shot_dir / f"login-before{suffix}.ppm")

    keyboard = GuestKeyboard(client)
    if creds.type_username:
        await keyboard.type_text(creds.username)
        await keyboard.press("tab")
    await keyboard.type_text(creds.password)
    await keyboard.press("ret")

    await asyncio.sleep(creds.settle_sec)
    after = await _shot(client, creds.shot_dir / f"login-after{suffix}.ppm")

    changed: bool | None = None
    if before and after and before.exists() and after.exists():
        # Identical bytes means the prompt redrew itself: the login failed.
        changed = before.read_bytes() != after.read_bytes()

    logger.info(
        "console login attempted for '{}': screen_changed={}",
        creds.username,
        changed,
    )
    return {
        "username": creds.username,
        "before": str(before) if before else None,
        "after": str(after) if after else None,
        "screen_changed": changed,
        "settle_sec": creds.settle_sec,
    }