"""Tests for guest input: the HMP keymap and the typing path.

The keymap is worth this much attention because its failure modes are silent.
A password typed with one character missing, or with every letter capitalised,
produces no error at all -- just a login that fails for an invisible reason.
These tests assert over the whole printable ASCII range rather than a few
samples, since that is where a gap hides.
"""
from __future__ import annotations

import string
from pathlib import Path

import pytest

from vm_harness.guest_input import (
    UnsupportedKeyError,
    key_for,
)


class TestKeyFor:
    def test_every_printable_ascii_character_is_typeable(self):
        """No gaps: a password must be typable without hitting an error.

        An earlier version handled unshifted punctuation but none of the
        shifted symbols, so 21 of 95 characters -- including !@#$%^&* -- raised.
        """
        printable = string.printable.strip()
        missing = [c for c in printable if not _typeable(c)]
        assert missing == [], f"cannot type: {missing!r}"

    def test_lowercase_is_not_shifted(self):
        """`shift-a` types 'A', so a blanket shift prefix uppercases passwords."""
        for ch in string.ascii_lowercase:
            assert key_for(ch) == ch

    def test_uppercase_is_shifted(self):
        for ch in string.ascii_uppercase:
            assert key_for(ch) == f"shift-{ch.lower()}"

    def test_digits_are_unshifted(self):
        for ch in string.digits:
            assert key_for(ch) == ch

    @pytest.mark.parametrize(
        ("char", "expected"),
        [
            (" ", "spc"),
            ("\n", "ret"),
            ("\t", "tab"),
            ("-", "minus"),
            ("_", "shift-minus"),
            ("=", "equal"),
            ("+", "shift-equal"),
            ("!", "shift-1"),
            (")", "shift-0"),
            ("?", "shift-slash"),
            ("~", "shift-grave_accent"),
            ("'", "apostrophe"),
            ('"', "shift-apostrophe"),
        ],
    )
    def test_named_and_shifted_keys(self, char, expected):
        assert key_for(char) == expected

    @pytest.mark.parametrize("char", ["é", "°", "中", "\x00"])
    def test_non_ascii_raises_rather_than_skipping(self, char):
        """A dropped character is a wrong password, not a visible error."""
        with pytest.raises(UnsupportedKeyError):
            key_for(char)

    def test_error_names_the_offending_character(self):
        with pytest.raises(UnsupportedKeyError, match="U\\+00E9"):
            key_for("é")


def _typeable(ch: str) -> bool:
    try:
        key_for(ch)
    except UnsupportedKeyError:
        return False
    return True


class _FakeQMP:
    """Records commands instead of sending them."""

    def __init__(self):
        self.commands: list[dict] = []

    async def send(self, cmd, args=None):
        self.commands.append({"command": cmd, "args": args or {}})
        return {"return": {}}


class TestGuestKeyboard:
    @pytest.mark.asyncio
    async def test_types_each_character_as_a_sendkey(self):
        from vm_harness.guest_input import GuestKeyboard

        client = _FakeQMP()
        kb = GuestKeyboard(client, key_delay_sec=0)
        await kb.type_text("Ab1!")
        lines = [c["args"]["command-line"] for c in client.commands]
        assert lines == ["sendkey shift-a", "sendkey b", "sendkey 1", "sendkey shift-1"]

    @pytest.mark.asyncio
    async def test_goes_through_human_monitor_command(self):
        from vm_harness.guest_input import GuestKeyboard

        client = _FakeQMP()
        await GuestKeyboard(client, key_delay_sec=0).type_text("x")
        assert client.commands[0]["command"] == "human-monitor-command"

    @pytest.mark.asyncio
    async def test_unsupported_character_sends_nothing(self):
        """The whole string is validated before a single key is sent.

        Otherwise a bad character half-typed leaves a partial password in the
        guest's buffer, and the next attempt has to clear it.
        """
        from vm_harness.guest_input import GuestKeyboard

        client = _FakeQMP()
        kb = GuestKeyboard(client, key_delay_sec=0)
        with pytest.raises(UnsupportedKeyError):
            await kb.type_text("good中bad")
        assert client.commands == []

    @pytest.mark.asyncio
    async def test_press_repeats_a_named_key(self):
        from vm_harness.guest_input import GuestKeyboard

        client = _FakeQMP()
        await GuestKeyboard(client, key_delay_sec=0).press("backspace", times=3)
        lines = [c["args"]["command-line"] for c in client.commands]
        assert lines == ["sendkey backspace"] * 3

    @pytest.mark.asyncio
    async def test_press_accepts_key_aliases(self):
        from vm_harness.guest_input import GuestKeyboard

        client = _FakeQMP()
        await GuestKeyboard(client, key_delay_sec=0).press("Enter")
        assert client.commands[0]["args"]["command-line"] == "sendkey ret"


class TestScreendump:
    @pytest.mark.asyncio
    async def test_writes_the_file(self, tmp_path):
        from vm_harness.guest_input import screendump

        class _Writer(_FakeQMP):
            async def send(self, cmd, args=None):
                Path(args["filename"]).write_bytes(b"P6\n1 1\n255\n" + b"\0" * 3)
                return {"return": {}}

        target = tmp_path / "shot.ppm"
        written = await screendump(_Writer(), target)
        assert written.exists()

    @pytest.mark.asyncio
    async def test_raises_when_qemu_creates_nothing(self, tmp_path):
        """QEMU can answer screendump without a file; do not report success."""
        from vm_harness.guest_input import screendump

        target = tmp_path / "missing.ppm"
        with pytest.raises(RuntimeError, match="not created"):
            await screendump(_FakeQMP(), target)

