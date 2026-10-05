"""X keysyms to the key names ``vm_harness.guest_input`` already speaks.

RFB sends `X keysyms <https://www.cl.cam.ac.uk/~mgk25/ucs/keysymdef.html>`_;
QEMU's ``sendkey`` (and therefore :class:`vm_harness.guest_input.GuestKeyboard`)
speaks a different vocabulary -- ``ret``, ``esc``, ``minus``, ``shift-minus``.
This module is the translation, and it is deliberately thin.

There is already a keymap in this project, in :mod:`vm_harness.guest_input`, and
its tables are the ones that get typed into real guests. So nothing here
re-encodes ASCII or punctuation: printable keysyms are handed to
``guest_input.key_for()`` and non-printable ones to the same
``_CONTROL_KEYS`` names. If that module's vocabulary changes, this follows it.

## Why unknown keysyms return ``None``

``key_for()`` raises rather than guess, because a password typed with one
character silently wrong is a failed login and a lockout counter rather than a
visible error. That reasoning applies here too, with more force: this runs on
every keystroke in an interactive session, where a wrong guess is a wrong
keystroke in a shell, not one password. There is no partial credit for
pressing the right key most of the time. So an unmapped keysym is reported as
unmapped and **no KeyEvent is sent at all**, rather than being rounded to the
nearest ASCII letter.

Not mapped, deliberately:

* Modifiers (Shift, Control, Alt, Meta/Super, CapsLock, NumLock). RFB carries
  the modifier's *state* in the keysym stream, but sending Shift as its own
  KeyEvent makes X report every letter as shifted -- so typing ``a`` arrives at
  the guest as ``A``. Modifier keys are therefore never sent; the guest sees
  the shifted keysym directly.
* Function keys, keypad keys, navigation beyond Home/End, and media keys. HMP
  names exist for some of these, but none are in the vocabulary
  ``guest_input`` validates, so nothing here can produce a name that is known
  to be accepted. Note in particular that :mod:`vm_harness.guest_input`
  documents that this QEMU machine type has no keypad, so keypad keysyms are
  refused rather than sent into the void.
* Dead keys, Unicode keysyms above U+00FF, and anything in the private-use
  area. These have no meaning without the server's keyboard map, which RFB does
  not carry.

## Known server-side limitation

The server this client talks to (``continuum-transport``'s
``rfb::server::keysym_to_sendkey``) accepts only Latin-1 printable keysyms and
its twelve-entry ``NAMED_KEYSYMS`` table. It therefore rejects the control- and
Alt-composite keysyms mapped below, and logs them as unsupported. The mappings
here are kept because they are the defined X11 conventions and the correct HMP
names, so they start working the moment that server's table grows -- and so a
client built against this one reports precisely which keys the peer will refuse
rather than sending something and seeing nothing happen.
"""
from __future__ import annotations

from typing import Optional

from vm_harness.guest_input import UnsupportedKeyError, _CONTROL_KEYS, key_for

# ── Latin-1 printable keysyms ──────────────────────────────────────────────────
#
# In Latin-1 the keysym for a printable character *is* its code point, so
# 0x20..0x7E needs no table of any kind: the keysym is the character, and
# ``key_for`` already knows how to spell every printable ASCII character.
KEYSYM_LATIN1_FIRST = 0x20
KEYSYM_LATIN1_LAST = 0x7E

# ── Control and navigation keysyms ─────────────────────────────────────────────
#
# Values from keysymdef.h. Each maps to a name that appears in
# ``guest_input._CONTROL_KEYS`` or passes through it unchanged, so the resulting
# name is one the existing typing path already accepts.
_KEYSYMS_TO_CONTROL_NAMES: dict[int, str] = {
    0xFF08: "backspace",   # XK_BackSpace
    0xFF09: "tab",         # XK_Tab
    0xFF0D: "ret",         # XK_Return
    0xFF1B: "esc",         # XK_Escape
    0xFF50: "home",        # XK_Home
    0xFF51: "left",        # XK_Left
    0xFF52: "up",          # XK_Up
    0xFF53: "right",       # XK_Right
    0xFF54: "down",        # XK_Down
    0xFF57: "end",         # XK_End
    0xFFFF: "delete",      # XK_Delete
    0xFF8D: "ret",         # XK_KP_Enter -- the guest's keypad has no enter of
                           # its own, and `ret` is the one that always exists.
}

#: Control keysyms above this are in the function-key / keypad / vendor range,
#: where the mapping to HMP names is a guess rather than a correspondence.
KEYSYM_FUNCTION_FIRST = 0xFFBE

#: Modifier keysyms. Listed only so the refusal is documented rather than
#: accidental: sending any of these would make X report the following key with
#: the modifier applied.
_MODIFIER_KEYSYMS = frozenset(
    {
        0xFFE1,  # Shift_L
        0xFFE2,  # Shift_R
        0xFFE3,  # Control_L
        0xFFE4,  # Control_R
        0xFFE5,  # Caps_Lock
        0xFFE6,  # Shift_Lock
        0xFFE7,  # Meta_L
        0xFFE8,  # Meta_R
        0xFFE9,  # Alt_L
        0xFFEA,  # Alt_R
        0xFFEB,  # Super_L
        0xFFEC,  # Super_R
        0xFFED,  # Hyper_L
        0xFFEE,  # Hyper_R
    }
)


#: X's Meta (Alt) keysyms are the base keysym with this offset added, so
#: ``Alt+a`` is 0x0161 rather than a separate Meta_L press. (keysymdef.h.)
ALT_KEYSYM_OFFSET = 0x0100
ALT_KEYSYM_FIRST = 0x0100 + KEYSYM_LATIN1_FIRST
ALT_KEYSYM_LAST = 0x0100 + KEYSYM_LATIN1_LAST

#: Control combinations live *inside* the keysym in X: ``Ctrl+c`` is 0x03.
CONTROL_KEYSYM_FIRST = 0x01
CONTROL_KEYSYM_LAST = 0x1A
#: Ctrl+\, Ctrl+], Ctrl+^, Ctrl+_ sit just above the letters.
_CONTROL_PUNCTUATION = {
    0x1C: "ctrl-backslash",
    0x1D: "ctrl-bracket_right",
    0x1E: "ctrl-6",  # caret is shift-6 on a US layout, so it is Ctrl+6
    0x1F: "ctrl-minus",
}

#: Control codes that X also spells as a named key. Preferred over the
#: ``ctrl-<letter>`` reading, because Tab is Tab and Ctrl+I is a different thing
#: that happens to share a code point.
_CONTROL_CODE_PREFERRED_NAMES = {
    0x08: "backspace",  # also Ctrl+H
    0x09: "tab",        # also Ctrl+I
    0x0D: "ret",        # also Ctrl+M
    0x1B: "esc",        # also Ctrl+[
}


def key_name_for_keysym(keysym: int) -> Optional[str]:
    """The ``guest_input`` key name for an X keysym, or ``None`` if unmapped.

    The returned string is exactly what :meth:`GuestKeyboard.press` accepts as a
    key name, so it can be used as a lookup key there without further checking.

    ``None`` means "no KeyEvent should be sent for this keysym". It is not an
    error: it is the answer for every modifier, function key and Unicode
    keysym outside the range this client claims to handle.
    """
    if keysym in _MODIFIER_KEYSYMS:
        return None
    control_name = _KEYSYMS_TO_CONTROL_NAMES.get(keysym)
    if control_name is not None:
        # Only ever return a name the existing keymap accepts. If a name here
        # ever stops being valid, the test suite says so rather than a guest
        # silently mistyping a password.
        if control_name in _CONTROL_KEYS:
            return control_name
        return None
    if KEYSYM_LATIN1_FIRST <= keysym <= KEYSYM_LATIN1_LAST:
        return _name_for_character(chr(keysym))
    if CONTROL_KEYSYM_FIRST <= keysym <= CONTROL_KEYSYM_LAST:
        preferred = _CONTROL_CODE_PREFERRED_NAMES.get(keysym)
        if preferred is not None:
            return preferred
        # 0x01 is Ctrl+A, so the letter is one below the code point.
        return f"ctrl-{chr(ord('a') + keysym - 1)}"
    if keysym in _CONTROL_PUNCTUATION:
        return _CONTROL_PUNCTUATION[keysym]
    if ALT_KEYSYM_FIRST <= keysym <= ALT_KEYSYM_LAST:
        base = _name_for_character(chr(keysym - ALT_KEYSYM_OFFSET))
        return None if base is None else f"alt-{base}"
    return None


def _name_for_character(character: str) -> Optional[str]:
    """The existing keymap's name for a character, or ``None`` if it has none."""
    try:
        return key_for(character)
    except UnsupportedKeyError:
        # Unreachable while guest_input types all of printable ASCII, but a gap
        # there must not become a wrong key here.
        return None


def is_mapped_keysym(keysym: int) -> bool:
    """Whether a KeyEvent will actually be sent for this keysym."""
    return key_name_for_keysym(keysym) is not None


__all__ = ["key_name_for_keysym", "is_mapped_keysym"]