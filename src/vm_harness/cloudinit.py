"""Build cloud-init NoCloud seeds so a VM can configure itself unattended.

A NoCloud seed is a small filesystem carrying the guest's answers, labelled
``cidata`` (or ``CIDATA``). Anything that reads one — Omarchy's installer,
cloud-init, Packer, Proxmox — attaches it as a second drive and skips its
interactive setup.

Getting this wrong fails quietly and expensively. An ISO built with only
ISO9660 stores filenames in 8.3 form (``USER_C~1.JSO``); readers that do not
consult Joliet then see an unrecognisable seed and fall back to
``DataSourceNone``, so the VM boots to a wizard that nobody is there to answer.
That failure is invisible unless you read the guest console.

So this module builds the seed with an explicit, verified format and checks
the result rather than assuming:

    from vm_harness.cloudinit import NoCloudSeed, SeedBuilder

    seed = NoCloudSeed(hostname="omarchy-vm", timezone="America/New_York")
    path = SeedBuilder().build(seed, "cidata.iso")
    verify_seed(path)          # raises rather than shipping a broken seed

Backends are tried in order and the first usable one wins, so the same call
works on a bare Windows host, on Linux, and inside a container.

Verification is an *independent* parse: it walks the ISO9660 directory
records of the finished image and looks for the filenames there. It does not
ask pycdlib whether pycdlib's output is good, and it does not grep the raw
bytes for a name that might equally be file *content*.

Installers have their own idea of what must be on the volume. That is
described by an :class:`InstallerContract` rather than by each caller
hand-rolling a list:

    from vm_harness.cloudinit import NoCloudSeed, OMARCHY_CONTRACT, SeedBuilder

    seed = NoCloudSeed(
        hostname="omarchy-vm",
        extra_files={"user_configuration.json": ..., "user_credentials.json": ...},
        contract=OMARCHY_CONTRACT,
    )
    SeedBuilder().build(seed, "cidata.iso")   # contract verified too

``OMARCHY_CONTRACT`` encodes what the ISO's own loader demands:
``user_configuration.json`` plus *either* ``user_credentials.json`` *or* a
zero-byte ``defer-provisioning`` marker. Setting ``defer_provisioning=True``
on the seed writes that marker and ships no credentials.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

DEFAULT_LABEL = "cidata"

#: The label a NoCloud consumer mounts by. Omarchy's loader matches exactly
#: these two spellings, case-sensitively (boot/omarchy-cidata-load:33-36), and
#: cloud-init itself looks for ``cidata``/``CIDATA``, so nothing else is worth
#: writing.
ACCEPTED_LABELS = ("cidata", "CIDATA")

#: Filenames a NoCloud consumer looks for. Both the canonical cloud-init names
#: and the extras other installers read are written.
CLOUD_INIT_FILES = ("user-data", "meta-data", "vendor-data", "network-config")

#: cloud-init's NoCloud datasource declares ``required=["user-data",
#: "meta-data"]`` and rejects a cidata device missing either, logging "not a
#: valid seed" and falling back to DataSourceNone.
CLOUD_INIT_REQUIRED = ("meta-data", "user-data")

#: ESC sequences that mark a supplementary volume descriptor as Joliet.
#: "%/@" is UCS-2 level 1, "%/C" level 2, "%/E" level 3. Any of them carries
#: long filenames, so the family is accepted rather than one spelling.
JOLIET_ESCAPES = (b"%/@", b"%/C", b"%/E")

SECTOR = 2048
PVD_SECTOR = 16

#: How far past the PVD to look for a Joliet SVD.
#:
#: ECMA-119 puts the volume descriptors in consecutive logical sectors
#: starting at 16 and ending with the type-255 terminator. Every writer this
#: module supports (pycdlib, genisoimage/mkisofs/xorriso) writes exactly one
#: supplementary descriptor, immediately after the PVD — pycdlib's Joliet SVD
#: lands at sector 17 and the terminator at 18 — because that is the cheapest
#: layout to produce and every reader handles it. Seven sectors of slack is
#: therefore several times more than any of them needs, and leaves room for a
#: writer that puts a Rock Ridge ("RR"/"RX") SVD ahead of the Joliet one.
#:
#: The window is a *search* window, not a proof: the scan stops at the
#: terminator, and if no Joliet SVD turns up inside it, verification fails
#: loudly rather than shipping an image whose names are 8.3 only.
SVD_SEARCH_END = 24

#: Longest seed filename this module will write. A Joliet directory record
#: name field is one byte of length, so the real ceiling is 255 bytes; 64
#: keeps names clear of the truncation that makes 8.3 collisions possible.
MAX_NAME_CHARS = 64

# ── Installer documents ───────────────────────────────────────────────────────
# Names the Omarchy installer reads off the cidata volume. They are plain data
# here: extra_files can write any of them without special-casing, and any of
# them can be omitted. The *builder* only knows the four it can derive from
# first-class fields.

FILE_CONFIG = "user_configuration.json"
FILE_CREDENTIALS = "user_credentials.json"
FILE_DEFER = "defer-provisioning"
FILE_FULL_NAME = "user_full_name.txt"
FILE_EMAIL = "user_email_address.txt"
FILE_ENCRYPT = "user_encrypt_installation.txt"
FILE_AUTHORIZED_KEYS = "authorized_keys"
FILE_TAILSCALE = "tailscale_authkey"


class SeedError(RuntimeError):
    """A seed could not be built, or a built seed failed verification."""


# ── Installer contracts ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class InstallerContract:
    """What a given installer insists is on the cidata volume.

    An installer is described by three things:

    ``required``
        Names that must be there, unconditionally.
    ``alternatives``
        Groups where *one* member is enough — Omarchy accepts credentials
        *or* the defer marker, never neither.
    ``optional``
        Names it will read if present. Listed so callers can introspect the
        contract, never enforced.

    ``credentials``/``defer_marker`` name the pair that conflicts: shipping
    deferred provisioning *and* a credentials file with users in it is a
    contradiction the installer resolves by silently dropping the users, so
    :meth:`SeedBuilder.build` refuses it instead.
    """

    name: str
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    alternatives: tuple[tuple[str, ...], ...] = ()
    credentials: Optional[str] = None
    defer_marker: Optional[str] = None

    def hard_required(self) -> tuple[str, ...]:
        """``required`` minus anything an alternatives group can stand in for."""
        stand_ins = {name for group in self.alternatives for name in group}
        return tuple(name for name in self.required if name not in stand_ins)

    def missing(self, present: Iterable[str]) -> list[str]:
        """Human-readable reasons this contract is *not* satisfied.

        Empty list means satisfied. Kept separate from raising so callers can
        assert on it in tests without exception handling.
        """
        have = set(present)
        problems = [f"{self.name}: missing required file {name!r}"
                    for name in self.hard_required() if name not in have]
        for group in self.alternatives:
            if not any(name in have for name in group):
                joined = " or ".join(repr(n) for n in group)
                problems.append(
                    f"{self.name}: none of {joined} is present; the installer "
                    "rejects the seed and falls back to its wizard"
                )
        return problems


#: ``user_configuration.json`` is always required; ``user_credentials.json``
#: is required unless the defer marker is present (cidata-load:52). The
#: alternatives group is what carries that exception.
OMARCHY_REQUIRED = (FILE_CONFIG, FILE_CREDENTIALS)
OMARCHY_OPTIONAL = (FILE_FULL_NAME, FILE_EMAIL, FILE_ENCRYPT,
                    FILE_AUTHORIZED_KEYS, FILE_TAILSCALE, FILE_DEFER)
OMARCHY_ALTERNATIVES = ((FILE_CREDENTIALS, FILE_DEFER),)

OMARCHY_CONTRACT = InstallerContract(
    name="omarchy",
    required=OMARCHY_REQUIRED,
    optional=OMARCHY_OPTIONAL,
    alternatives=OMARCHY_ALTERNATIVES,
    credentials=FILE_CREDENTIALS,
    defer_marker=FILE_DEFER,
)


# ── Seed contents ─────────────────────────────────────────────────────────────

@dataclass
class NoCloudSeed:
    """The guest's answers, in a form a NoCloud consumer can read.

    ``extra_files`` carries installer-specific documents (Omarchy wants
    ``user_configuration.json``, ``user_credentials.json`` and friends) which
    are written alongside the standard cloud-init names.
    """

    hostname: Optional[str] = None
    timezone: Optional[str] = None
    username: Optional[str] = None
    password_hash: Optional[str] = None
    full_name: Optional[str] = None
    email: Optional[str] = None
    authorized_keys: list[str] = field(default_factory=list)
    # Installer-specific payloads, written verbatim at the seed root. Values
    # are str (encoded UTF-8) or bytes, so binary documents are fine.
    extra_files: dict[str, str | bytes] = field(default_factory=dict)
    # False disables full-disk encryption, which is what makes an unattended
    # install genuinely unattended: an encrypted root still prompts for a LUKS
    # passphrase at first boot.
    encrypt: bool = False
    label: str = DEFAULT_LABEL
    # Files that must exist in the finished seed for verification to pass.
    required_files: tuple[str, ...] = ()
    # Optional installer contract. When set, its required files (and
    # alternatives) are verified in addition to ``required_files``, and the
    # "every file present" default is dropped in favour of the contract.
    contract: Optional[InstallerContract] = None
    # Write the zero-byte ``defer-provisioning`` marker and ship no
    # credentials: the documented flow where the install stops short of
    # creating an account and the guest provisions itself on first boot.
    defer_provisioning: bool = False

    def files(self) -> dict[str, bytes]:
        """Every file the seed should contain, name -> bytes.

        ``meta-data`` and ``user-data`` are always written. cloud-init's
        NoCloud datasource treats both as *required* (``required=["user-data",
        "meta-data"]``) and rejects a device labelled cidata that lacks either,
        logging "not a valid seed" and falling back to DataSourceNone. So a
        seed carrying only installer-specific documents is still invalid.
        """
        out: dict[str, bytes] = {}

        hostname = _plain_field(self.hostname, "hostname")
        if hostname:
            out["meta-data"] = (
                f"instance-id: {hostname}\nlocal-hostname: {hostname}\n".encode()
            )
        else:
            out["meta-data"] = b"instance-id: nocloud\n"

        full_name = _plain_field(self.full_name, "full_name")
        username = _plain_field(self.username, "username")
        body = ["#cloud-config"]
        if hostname:
            body.append(f"hostname: {hostname}")
        timezone = _plain_field(self.timezone, "timezone")
        if timezone:
            body.append(f"timezone: {timezone}")
        if full_name:
            body.append(f"full_name: {full_name}")
        if username:
            body.append(f"user: {username}")
            if self.password_hash:
                body.append(f"passwd: {self.password_hash}")
            body.append("lock_passwd: false")
            body.append("shell: /bin/bash")
        if self.authorized_keys:
            body.append("ssh_authorized_keys:")
            body.extend(f"  - {k}" for k in self.authorized_keys)
        out["user-data"] = ("\n".join(body) + "\n").encode()

        # Documents an installer reads directly off the volume, as opposed to
        # the cloud-config above. Without these the fields below are accepted
        # and silently dropped.
        if full_name:
            out[FILE_FULL_NAME] = (full_name + "\n").encode()
        email = _plain_field(self.email, "email")
        if email:
            out[FILE_EMAIL] = (email + "\n").encode()
        if self.encrypt:
            out[FILE_ENCRYPT] = b"true\n"
        if self.authorized_keys:
            out[FILE_AUTHORIZED_KEYS] = ("\n".join(self.authorized_keys) + "\n").encode()

        # A marker, not a document: the loader only tests that the path exists,
        # so it is written zero bytes (AUTINSTALL-CONTRACT.md §1).
        if self.defer_provisioning:
            out[FILE_DEFER] = b""

        # extra_files last, so a caller can always override a derived document
        # with the exact bytes the installer expects.
        for name, text in self.extra_files.items():
            out[name] = text.encode() if isinstance(text, str) else text

        return out


def _plain_field(value, field_name: str) -> str:
    """Reject anything that would corrupt the YAML these fields are written into.

    A newline in ``hostname`` lets a caller inject arbitrary ``user-data``
    keys; a colon-space ends the key. Both fail at the guest, not here.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SeedError(f"{field_name} must be a string, got {type(value).__name__}")
    if "\n" in value or "\r" in value or "\x00" in value:
        raise SeedError(
            f"{field_name} contains a newline or NUL, which cannot be written "
            "into meta-data/user-data"
        )
    return value


# ── Image parsing (independent of whatever wrote the image) ───────────────────

def _directory_entries(data: bytes, record: bytes) -> list[bytes]:
    """Every directory record in the directory a 34-byte record points at.

    ``record`` is a directory record taken from a volume descriptor (bytes
    156-189 of the PVD or SVD). Its extent is assumed contiguous, which holds
    for every image this module builds and for every mkisofs image.
    """
    extent = int.from_bytes(record[2:6], "little")
    size = int.from_bytes(record[10:14], "little")
    if extent <= 0 or size <= 0:
        return []
    out: list[bytes] = []
    for sector in range(extent, extent + max(1, -(-size // SECTOR))):
        base = sector * SECTOR
        if base + SECTOR > len(data):
            break
        # Records never straddle a logical block; a zero length byte means the
        # rest of the block is padding.
        pos = base
        while pos < base + SECTOR:
            length = data[pos]
            if length == 0 or pos + length > base + SECTOR:
                break
            out.append(data[pos:pos + length])
            pos += length
    return out


def _iso_identifier(record: bytes) -> Optional[str]:
    """The record's ISO9660 identifier: 8.3, ASCII, version suffix removed."""
    name_len = record[32]
    if name_len <= 1:
        return None  # "." or ".."
    return record[33:33 + name_len].decode("ascii", "replace").split(";")[0]


def _record_names(record: bytes, joliet: bool) -> set[str]:
    """Every real filename one directory record can be read under.

    Three names can hide in one record: the ISO9660 identifier (8.3, ASCII, no
    version in level 3), the Joliet identifier (UCS-2 big endian) and a Rock
    Ridge ``NM`` alternate name. All three are collected so a reader using any
    of them is satisfied.
    """
    name_len = record[32]
    raw = bytes(record[33:33 + name_len])
    names: set[str] = set()

    # Rock Ridge NM entries belong to the system-use area, which starts after
    # the name and its pad byte. pycdlib writes them there but declares an
    # xattr length of 0, so the declared length cannot be trusted and the whole
    # rest of the record is scanned instead — never the name bytes themselves,
    # which could otherwise spell "NM" by accident.
    name_end = 33 + name_len + (name_len % 2)
    pos = name_end
    while True:
        pos = record.find(b"NM", pos, len(record))
        if pos < 0 or pos + 4 > len(record):
            break
        length = record[pos + 2]
        if length >= 6 and record[pos + 3] == 1 and pos + length <= len(record):
            names.add(record[pos + 5:pos + length].decode("utf-8", "replace"))
            break
        pos += 2

    if name_len == 1:
        return names  # "." or ".."
    if joliet:
        names.add(raw.decode("utf-16-be", "replace"))
    else:
        names.add(raw.decode("ascii", "replace").split(";")[0])
    return names


def _find_joliet(data: bytes) -> Optional[int]:
    """Byte offset of the Joliet SVD, or None.

    Scans every descriptor slot in the search window instead of stopping at
    the first supplementary one: a Rock Ridge-only SVD ("RR"/"RX") ahead of
    the Joliet descriptor would otherwise be mistaken for it.
    """
    for sector in range(PVD_SECTOR + 1, SVD_SEARCH_END):
        off = sector * SECTOR
        if off + SECTOR > len(data):
            break
        if data[off + 1:off + 6] != b"CD001":
            continue
        descriptor_type = data[off]
        if descriptor_type == 255:
            break  # volume descriptor set terminator
        if descriptor_type == 2 and data[off + 88:off + 91] in JOLIET_ESCAPES:
            return off
    return None


# ── Verification ───────────────────────────────────────────────────────────────

def verify_seed(path: str | os.PathLike, required: Iterable[str] = (),
                *, contract: Optional[InstallerContract] = None) -> list[str]:
    """Check a built seed and return the filenames it was asked to contain.

    Verifies the ISO9660 primary volume descriptor exists, that the volume label
    matches ``cidata``, that the Joliet SVD is present so long filenames
    survive, that cloud-init's two *required* documents (``meta-data`` and
    ``user-data``) are present as *real directory entries*, that the ISO9660
    layer holds no two files under one 8.3 name, and that any extra
    ``required`` files (plus every ``contract`` requirement) are there too.

    Names are looked for in the image's directory records, so a file that is
    only *mentioned* — in its own directory entry for a different file, or in
    the text of another file's payload — does not count. Raises ``SeedError``
    rather than returning quietly, because a silently broken seed costs a full
    boot cycle to discover.
    """
    p = Path(path)
    if not p.exists():
        raise SeedError(f"seed not found: {p}")
    data = p.read_bytes()
    if len(data) < 2048 * 18:
        raise SeedError(f"seed too small to be an ISO image: {p} ({len(data)} bytes)")

    # Primary Volume Descriptor: type 1 and 'CD001' at sector 16.
    if data[16 * 2048 + 1:16 * 2048 + 6] != b"CD001":
        raise SeedError("no ISO9660 primary volume descriptor at sector 16")
    if data[16 * 2048] != 1:
        raise SeedError("sector 16 is not a primary volume descriptor")

    label = data[16 * 2048 + 40:16 * 2048 + 72].decode("ascii", "replace").strip()
    if label.upper() != DEFAULT_LABEL.upper():
        raise SeedError(f"volume label is {label!r}, expected {DEFAULT_LABEL!r}")

    joliet_off = _find_joliet(data)
    if joliet_off is None:
        raise SeedError(
            "no Joliet supplementary volume descriptor: filenames would be "
            "stored as 8.3 (e.g. USER_C~1.JSO) and a NoCloud reader would "
            "reject the seed"
        )

    # The Joliet names are what a NoCloud reader actually looks up. The ISO9660
    # layer is parsed too, to prove the image is unambiguous for a reader that
    # only speaks 8.3.
    iso_records = _directory_entries(data, data[16 * 2048 + 156:16 * 2048 + 190])
    joliet_records = _directory_entries(data, data[joliet_off + 156:joliet_off + 190])

    iso_names = [n for n in (_iso_identifier(r) for r in iso_records) if n]
    duplicates = sorted({n for n in iso_names if iso_names.count(n) > 1})
    if duplicates:
        raise SeedError(
            f"two seed files share the ISO9660 name(s) {duplicates}: the 8.3 "
            "layer cannot tell them apart, so a reader that does not use "
            "Joliet or Rock Ridge sees an ambiguous seed"
        )

    names: set[str] = set()
    for record in joliet_records:
        names |= _record_names(record, joliet=True)
    for record in iso_records:
        names |= _record_names(record, joliet=False)
    names.discard("")
    folded = {n.casefold() for n in names}

    def _has(name: str) -> bool:
        return name in names or name.casefold() in folded

    # cloud-init rejects a cidata device missing either of these, so a seed
    # without them is invalid no matter what else it carries.
    for mandatory in CLOUD_INIT_REQUIRED:
        if not _has(mandatory):
            raise SeedError(
                f"seed is missing {mandatory!r}, which cloud-init requires; "
                "the datasource would reject the device and fall back to "
                "DataSourceNone"
            )

    present: list[str] = []
    for name in required:
        if _has(name):
            present.append(name)
        else:
            raise SeedError(f"required file missing from seed: {name}")

    if contract is not None:
        problems = contract.missing(names)
        if problems:
            raise SeedError("; ".join(problems))
        present.extend(name for name in contract.hard_required() if name not in present)
        for group in contract.alternatives:
            present.extend(name for name in group
                           if name in names and name not in present)

    return present


# ── Backends ───────────────────────────────────────────────────────────────────

def _iso9660_name(name: str) -> str:
    """A plain-ISO9660-safe path component: A-Z, 0-9 and _ only.

    The base ISO9660 level cannot carry dots or lower case, so long names like
    ``user_configuration.json`` must be mangled here. Joliet and Rock Ridge
    records hold the real name, which is what NoCloud readers use.

    Truncation to 8+3 is lossy and two names can therefore mangle to the same
    string (``user_configuration.json`` and ``user_configs.json`` both become
    ``USER_CON.JSO``). pycdlib emits both records anyway, which is an invalid
    directory, so :func:`SeedBuilder._preflight` rejects the pair up front.

    Non-ASCII is *not* folded into the name: ``str.isalnum`` is true for ``é``
    and for Arabic-Indic digits, and those are not ISO9660 d-characters.
    """
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    cleaned = "".join(c if c.isascii() and c.isalnum() else "_" for c in stem).upper()[:8]
    ext = "".join(c if c.isascii() and c.isalnum() else "_" for c in ext).upper()[:3]
    return f"{cleaned}.{ext}" if ext else cleaned


def _iso9660_collisions(names: Iterable[str]) -> list[tuple[str, str]]:
    """Pairs of names that mangle to the same ISO9660 identifier."""
    seen: dict[str, str] = {}
    clashes: list[tuple[str, str]] = []
    for name in names:
        mangled = _iso9660_name(name)
        previous = seen.get(mangled)
        if previous is not None:
            clashes.append((previous, name))
        else:
            seen[mangled] = name
    return clashes


class SeedBuilder:
    """Builds NoCloud seeds, trying several backends until one works.

    Backends, in order:

    ``pycdlib``
        Pure Python, writes ISO9660 + Joliet + Rock Ridge. Always available
        once installed and needs no privileges, so this is the default.
    ``genisoimage`` / ``mkisofs`` / ``xorriso``
        Used when installed, because they are what installers document.
    ``imapi2``
        Windows-only fallback through COM. It is *not wired up*: the COM image
        builder cannot reliably emit the Joliet descriptor NoCloud readers
        require, so it is reported unavailable with that reason rather than
        tried and failed. Naming it explicitly still raises the guidance
        error instead of "unknown backend".
    """

    #: Auto order is a fixed, finite list, deduplicated in ``_backend_order``.
    AUTO_ORDER = ("pycdlib", "genisoimage", "imapi2")

    def __init__(self, backend: str = "auto") -> None:
        self.backend = backend

    # -- public API ----------------------------------------------------------

    def build(self, seed: NoCloudSeed, out_path: str | os.PathLike) -> Path:
        """Write ``seed`` to ``out_path`` and verify it before returning.

        A failed attempt leaves nothing behind: a half-written image at
        ``out_path`` is exactly the kind of file that gets attached to a VM by
        mistake.
        """
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        files = _as_bytes(seed.files())
        self._preflight(files, seed.label, seed.contract)
        required = self._requirements(seed, files)

        order = self._backend_order()
        if not order:
            detail = "; ".join(
                f"{name}: {status['reason']}"
                for name, status in self._report().items()
            )
            raise SeedError(f"no usable NoCloud seed backend on this host ({detail})")

        errors: list[str] = []
        for name in order:
            try:
                self._dispatch(name, files, seed.label, out)
                verify_seed(out, required, contract=seed.contract)
            except SeedError as exc:
                errors.append(f"{name}: {exc}")
                _discard(out)
            except Exception as exc:  # a backend blowing up must not be fatal
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                _discard(out)
            else:
                return out

        raise SeedError(
            "no backend could build a valid NoCloud seed:\n  "
            + "\n  ".join(errors)
        )

    def describe(self, detailed: bool = False) -> dict:
        """Which backends are usable here, for diagnostics.

        Every known backend is listed, including the ones that are not usable,
        so the answer to "why did it pick that?" is visible. By default each
        maps to a bool; ``detailed=True`` maps to ``{"available", "reason"}``.
        """
        report = self._report()
        if detailed:
            return report
        return {name: bool(status["available"]) for name, status in report.items()}

    # -- internals -----------------------------------------------------------

    def _report(self) -> dict[str, dict]:
        """Per-backend availability with the reason either way."""
        if self.backend != "auto" and self.backend not in self.AUTO_ORDER:
            return {self.backend: {"available": False,
                                   "reason": f"unknown backend {self.backend!r}"}}
        return {name: self._status(name) for name in self.AUTO_ORDER}

    def _status(self, name: str) -> dict:
        if name == "pycdlib":
            try:
                import pycdlib
            except ImportError:
                return {"available": False,
                        "reason": "pycdlib is not installed (pip install pycdlib)"}
            return {"available": True, "reason": f"pycdlib {_pycdlib_version(pycdlib)}"}
        if name == "genisoimage":
            exe = _genisoimage_exe()
            if exe:
                return {"available": True, "reason": exe}
            return {"available": False,
                    "reason": "no genisoimage, mkisofs or xorriso on PATH"}
        if name == "imapi2":
            # load-bearing: this backend raises by design, so reporting it
            # available would put a guaranteed failure in the fallback order.
            return {
                "available": False,
                "reason": "not wired up: the Windows COM image builder cannot "
                          "reliably emit the Joliet descriptor NoCloud requires; "
                          "install pycdlib or genisoimage instead",
            }
        return {"available": False, "reason": f"unknown backend {name!r}"}

    def _backend_order(self) -> list[str]:
        """The finite, duplicate-free list of backends to try."""
        if self.backend != "auto":
            return [self.backend]
        order = [name for name in self.AUTO_ORDER if self._available(name)]
        return list(dict.fromkeys(order))

    def _available(self, name: str) -> bool:
        return bool(self._status(name)["available"])

    def _requirements(self, seed: NoCloudSeed, files: dict[str, bytes]) -> tuple[str, ...]:
        """Which filenames the finished image must contain.

        Explicit ``required_files`` first, then the contract's hard
        requirements. With neither, every file is required — a seed that built
        fewer files than asked for is a bug worth hearing about.
        """
        names: list[str] = []
        if seed.contract is not None:
            names.extend(seed.contract.hard_required())
        names.extend(seed.required_files or (sorted(files) if seed.contract is None else ()))
        out: list[str] = []
        for name in names:
            if name not in out:
                out.append(name)
        return tuple(out)

    def _preflight(self, files: dict[str, bytes], label: str,
                   contract: Optional[InstallerContract] = None) -> None:
        """Reject a seed that cannot be built correctly, before a backend runs.

        Everything here is a failure that would otherwise be silent or would
        only surface as a backend crash with no idea which input caused it.
        """
        if not files:
            raise SeedError(
                "refusing to build an empty seed: a cidata volume with no files "
                "would be rejected by every consumer (and cloud-init requires "
                "meta-data and user-data)"
            )

        if not isinstance(label, str) or not label:
            raise SeedError(f"volume label must be a non-empty string, got {label!r}")
        if label.upper() not in {accepted.upper() for accepted in ACCEPTED_LABELS}:
            raise SeedError(
                f"volume label is {label!r}; NoCloud consumers mount by label, "
                f"so it must be {' or '.join(repr(n) for n in ACCEPTED_LABELS)} "
                "(in either case). Anything else builds an image nothing will "
                "ever read."
            )

        for name, payload in files.items():
            if not isinstance(name, str) or not name:
                raise SeedError(f"seed filename must be a non-empty string, got {name!r}")
            if len(name) > MAX_NAME_CHARS:
                raise SeedError(
                    f"seed filename {name!r} is {len(name)} characters, over the "
                    f"{MAX_NAME_CHARS}-character limit; it would be truncated "
                    "on the ISO and could collide with another file"
                )
            if "/" in name or "\\" in name or name in (".", ".."):
                raise SeedError(
                    f"seed filename {name!r} must be a single path component at "
                    "the volume root, not a path"
                )
            if any(ch < " " or ch == "\x7f" for ch in name):
                raise SeedError(
                    f"seed filename {name!r} contains a control character"
                )
            if not isinstance(payload, bytes):
                raise SeedError(
                    f"seed payload for {name!r} must be bytes, got "
                    f"{type(payload).__name__}"
                )

        folded: dict[str, str] = {}
        for name in files:
            previous = folded.get(name.casefold())
            if previous is not None:
                raise SeedError(
                    f"seed filenames {previous!r} and {name!r} differ only in "
                    "case; Joliet and the guest filesystem cannot hold both"
                )
            folded[name.casefold()] = name

        clashes = _iso9660_collisions(files)
        if clashes:
            pairs = ", ".join(f"{a!r} and {b!r} both become "
                              f"{_iso9660_name(a)!r}" for a, b in clashes)
            raise SeedError(
                f"ISO9660 8.3 name collision: {pairs}. The Joliet and Rock "
                "Ridge names would survive, but the ISO9660 layer would hold "
                "two entries under one name, which is an invalid directory and "
                "ambiguous to any reader that does not use those extensions. "
                "Rename one of the files."
            )

        for mandatory in CLOUD_INIT_REQUIRED:
            if mandatory not in files:
                raise SeedError(
                    f"seed is missing {mandatory!r}, which cloud-init requires"
                )

        if contract is not None:
            self._check_defer_conflict(files, contract)

    @staticmethod
    def _check_defer_conflict(files: dict[str, bytes],
                             contract: InstallerContract) -> None:
        """Refuse a deferred seed that also ships users.

        Deferred provisioning strips every user from the credentials file and
        keeps only ``encryption_password``
        (AUTINSTALL-CONTRACT.md §3.4). A seed carrying both therefore installs
        an OS with no account and no error, which is the worst possible
        outcome for an unattended install — so it is refused here, and the
        legitimate case (credentials carrying just the LUKS passphrase) is
        still expressible.
        """
        if not (contract.defer_marker and contract.credentials):
            return
        if contract.defer_marker not in files:
            return
        payload = files.get(contract.credentials)
        if payload is None:
            return
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return  # not JSON; the installer will complain, not silently strip
        users = document.get("users") if isinstance(document, dict) else None
        if users:
            raise SeedError(
                f"{contract.name}: the {contract.defer_marker!r} marker is set "
                f"but {contract.credentials!r} carries {len(users)} user(s); "
                "deferred provisioning strips them, so the install would "
                "finish with no account. Drop the users (keep "
                "encryption_password) or drop the marker."
            )

    def _dispatch(self, name: str, files: dict[str, bytes], label: str, out: Path) -> None:
        if name == "pycdlib":
            self._build_pycdlib(files, label, out)
        elif name == "genisoimage":
            self._build_genisoimage(files, label, out)
        elif name == "imapi2":
            self._build_imapi2(files, label, out)
        else:
            raise SeedError(f"unknown backend {name!r}")

    def _build_pycdlib(self, files: dict[str, bytes], label: str, out: Path) -> None:
        import pycdlib

        iso = pycdlib.PyCdlib()
        # interchange_level=3 + joliet=3 + Rock Ridge is what makes long
        # filenames readable. The base ISO9660 layer still gets 8.3 names
        # because that is all its character set allows.
        iso.new(interchange_level=3, joliet=3, rock_ridge='1.09', vol_ident=label)
        readers: list[_BytesReader] = []
        try:
            for name, data in files.items():
                reader = _BytesReader(data)
                # pycdlib keeps the handle and reads from it during write(), so
                # it has to outlive the add_fp() call.
                readers.append(reader)
                iso.add_fp(
                    reader, len(data),
                    iso_path="/" + _iso9660_name(name),
                    rr_name=name,          # Rock Ridge: relative, no slash
                    joliet_path="/" + name,  # Joliet: absolute, UCS-2
                )
            iso.write(str(out))
        finally:
            iso.close()
            for reader in readers:
                reader.close()

    def _build_genisoimage(self, files: dict[str, bytes], label: str, out: Path) -> None:
        exe = _genisoimage_exe()
        if not exe:
            raise SeedError("genisoimage/mkisofs/xorriso is not on PATH")

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            for name, data in files.items():
                (src / name).write_bytes(data)
            # -joliet and -rock keep long filenames; -volid sets the label.
            if Path(exe).name.lower().startswith("xorriso"):
                args = [exe, "-as", "mkisofs", "-output", str(out), "-volid", label,
                        "-joliet", "-rock", str(src)]
            else:
                args = [exe, "-output", str(out), "-volid", label, "-joliet",
                        "-rock", str(src)]
            subprocess.run(args, check=True, capture_output=True)

    def _build_imapi2(self, files: dict[str, bytes], label: str, out: Path) -> None:
        raise SeedError(
            "imapi2 backend is not wired up: the Windows COM image builder "
            "cannot reliably emit the Joliet descriptor that NoCloud readers "
            "require. Install pycdlib (pip install pycdlib) or genisoimage."
        )


def _genisoimage_exe() -> Optional[str]:
    return (shutil.which("genisoimage") or shutil.which("mkisofs")
            or shutil.which("xorriso"))


def _pycdlib_version(module) -> str:
    """Installed version, for the diagnostics line only."""
    try:
        import importlib.metadata

        return importlib.metadata.version("pycdlib")
    except Exception:
        return getattr(module, "__version__", "unknown")


def _discard(path: Path) -> None:
    """Drop a failed build's output so nothing half-built can be shipped."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _as_bytes(files: dict) -> dict[str, bytes]:
    """Normalise payloads to bytes once, so no backend re-guesses types."""
    out: dict[str, bytes] = {}
    for name, payload in files.items():
        if isinstance(payload, str):
            out[name] = payload.encode()
        elif isinstance(payload, (bytes, bytearray, memoryview)):
            out[name] = bytes(payload)
        else:
            raise SeedError(
                f"seed payload for {name!r} must be str or bytes, got "
                f"{type(payload).__name__}"
            )
    return out


class _BytesReader(io.RawIOBase):
    """Read-only file-like over an in-memory payload, for pycdlib's add_fp.

    ``io.RawIOBase`` because pycdlib rejects anything that does not look like
    a binary file object (``pycdlib.utils.file_object_supports_binary``).
    """

    def __init__(self, data: bytes) -> None:
        super().__init__()
        self._data = data
        self._pos = 0

    # -- io plumbing ---------------------------------------------------------

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._data) - self._pos
        chunk = self._data[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def readinto(self, buf) -> int:
        chunk = self.read(len(buf))
        buf[:len(chunk)] = chunk
        return len(chunk)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            self._pos = offset
        elif whence == os.SEEK_CUR:
            self._pos += offset
        elif whence == os.SEEK_END:
            self._pos = len(self._data) + offset
        return self._pos

    def tell(self) -> int:
        return self._pos

    def close(self) -> None:
        self._pos = 0
        super().close()


# ── Convenience ────────────────────────────────────────────────────────────────

def build_seed(out_path: str | os.PathLike, **kwargs) -> Path:
    """Build a NoCloud seed in one call.

    ``build_seed("cidata.iso", hostname="omarchy-vm", timezone="UTC")``
    """
    backend = kwargs.pop("backend", "auto")
    label = kwargs.pop("label", DEFAULT_LABEL)
    encrypt = kwargs.pop("encrypt", False)
    required = kwargs.pop("required_files", ())
    extra = kwargs.pop("extra_files", {})
    contract = kwargs.pop("contract", None)
    seed = NoCloudSeed(
        label=label,
        encrypt=encrypt,
        extra_files=extra,
        required_files=tuple(required),
        contract=contract,
        **kwargs,
    )
    return SeedBuilder(backend).build(seed, out_path)
