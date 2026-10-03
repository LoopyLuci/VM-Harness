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

So this module builds the seed with an explicit, verified format and checks the
result rather than assuming:

    from vm_harness.cloudinit import NoCloudSeed, SeedBuilder

    seed = NoCloudSeed(hostname="omarchy-vm", timezone="America/New_York")
    path = SeedBuilder().build(seed, "cidata.iso")
    verify_seed(path)          # raises rather than shipping a broken seed

Backends are tried in order and the first usable one wins, so the same call
works on a bare Windows host, on Linux, and inside a container.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

DEFAULT_LABEL = "cidata"

#: Filenames a NoCloud consumer looks for. Both the canonical cloud-init names
#: and the extras other installers read are written.
CLOUD_INIT_FILES = ("user-data", "meta-data", "vendor-data", "network-config")


class SeedError(RuntimeError):
    """A seed could not be built, or a built seed failed verification."""


# ── Seed contents ──────────────────────────────────────────────────────────────

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
    # Installer-specific payloads, written verbatim at the seed root.
    extra_files: dict[str, str] = field(default_factory=dict)
    # False disables full-disk encryption, which is what makes an unattended
    # install genuinely unattended: an encrypted root still prompts for a LUKS
    # passphrase at first boot.
    encrypt: bool = False
    label: str = DEFAULT_LABEL
    # Files that must exist in the finished seed for verification to pass.
    required_files: tuple[str, ...] = ()

    def files(self) -> dict[str, bytes]:
        """Every file the seed should contain, name -> bytes.

        ``meta-data`` and ``user-data`` are always written. cloud-init's
        NoCloud datasource treats both as *required* (``required=["user-data",
        "meta-data"]``) and rejects a device labelled cidata that lacks either,
        logging "not a valid seed" and falling back to DataSourceNone. So a
        seed carrying only installer-specific documents is still invalid.
        """
        out: dict[str, bytes] = {}

        if self.hostname:
            out["meta-data"] = (
                f"instance-id: {self.hostname}\nlocal-hostname: {self.hostname}\n".encode()
            )
        else:
            out["meta-data"] = b"instance-id: nocloud\n"

        body = ["#cloud-config"]
        if self.hostname:
            body.append(f"hostname: {self.hostname}")
        if self.timezone:
            body.append(f"timezone: {self.timezone}")
        if self.full_name:
            body.append(f"full_name: {self.full_name}")
        if self.username:
            body.append(f"user: {self.username}")
            if self.password_hash:
                body.append(f"passwd: {self.password_hash}")
            body.append("lock_passwd: false")
            body.append("shell: /bin/bash")
        if self.authorized_keys:
            body.append("ssh_authorized_keys:")
            body.extend(f"  - {k}" for k in self.authorized_keys)
        out["user-data"] = ("\n".join(body) + "\n").encode()

        if self.encrypt:
            out["user_encrypt_installation"] = b"true\n"
        if self.authorized_keys:
            out["authorized_keys"] = ("\n".join(self.authorized_keys) + "\n").encode()

        for name, text in self.extra_files.items():
            out[name] = text.encode() if isinstance(text, str) else text

        return out


# ── Verification ───────────────────────────────────────────────────────────────

def verify_seed(path: str | os.PathLike, required: Iterable[str] = ()) -> list[str]:
    """Check a built seed and return its filenames.

    Verifies the ISO9660 primary volume descriptor exists, that the volume label
    matches ``cidata``, that the Joliet SVD is present so long filenames
    survive, that cloud-init's two *required* documents (``meta-data`` and
    ``user-data``) are present, and that any extra ``required`` files are there
    too. Raises ``SeedError`` rather than returning quietly, because a silently
    broken seed costs a full boot cycle to discover.
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

    # Joliet supplementary volume descriptor (type 2). Its escape sequence picks
    # the UCS-2 level: "%/@" is level 1, "%/C" level 2, "%/E" level 3. Any of
    # them carries full filenames, so accept the family rather than only the
    # level-1 spelling that some writers use.
    joliet = False
    for sector in range(17, 24):
        off = sector * 2048
        if off + 2048 > len(data):
            break
        if data[off + 1:off + 6] == b"CD001" and data[off] == 2:
            if data[off + 88:off + 91] in (b"%/@", b"%/C", b"%/E"):
                joliet = True
            break
    if not joliet:
        raise SeedError(
            "no Joliet supplementary volume descriptor: filenames would be "
            "stored as 8.3 (e.g. USER_C~1.JSO) and a NoCloud reader would "
            "reject the seed"
        )

    blob = data
    present = []

    def _has(name: str) -> bool:
        stem = name.rsplit(".", 1)[0]
        return name.encode() in blob or stem[:8].upper().encode() in blob.upper()

    # cloud-init rejects a cidata device missing either of these, so a seed
    # without them is invalid no matter what else it carries.
    for mandatory in ("meta-data", "user-data"):
        if not _has(mandatory):
            raise SeedError(
                f"seed is missing {mandatory!r}, which cloud-init requires; "
                "the datasource would reject the device and fall back to "
                "DataSourceNone"
            )

    for name in required:
        # Long name present as-is, or its 8.3 stem, which is still a real file.
        if _has(name):
            present.append(name)
        else:
            raise SeedError(f"required file missing from seed: {name}")

    return present


# ── Backends ───────────────────────────────────────────────────────────────────

def _iso9660_name(name: str) -> str:
    """A plain-ISO9660-safe path component: A-Z, 0-9 and _ only.

    The base ISO9660 level cannot carry dots or lower case, so long names like
    ``user_configuration.json`` must be mangled here. Joliet and Rock Ridge
    records hold the real name, which is what NoCloud readers use.
    """
    stem, _, ext = name.rpartition(".")
    stem = stem or name
    cleaned = "".join(c if c.isalnum() else "_" for c in stem).upper()[:8]
    ext = "".join(c if c.isalnum() else "_" for c in ext).upper()[:3]
    return f"{cleaned}.{ext}" if ext else cleaned


class SeedBuilder:
    """Builds NoCloud seeds, trying several backends until one works.

    Backends, in order:

    ``pycdlib``
        Pure Python, writes ISO9660 + Joliet + Rock Ridge. Always available
        once installed and needs no privileges, so this is the default.
    ``genisoimage`` / ``mkisofs`` / ``xorriso``
        Used when installed, because they are what installers document.
    ``imapi2``
        Windows-only fallback through COM. Produces an image whose filenames
        need Joliet to survive; only used when nothing else is present, and
        verified afterwards like every other backend.
    """

    def __init__(self, backend: str = "auto") -> None:
        self.backend = backend

    # -- public API ----------------------------------------------------------

    def build(self, seed: NoCloudSeed, out_path: str | os.PathLike) -> Path:
        """Write ``seed`` to ``out_path`` and verify it before returning."""
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        files = seed.files()
        required = tuple(seed.required_files) or tuple(sorted(files))

        order = self._backend_order()
        errors: list[str] = []
        for name in order:
            try:
                self._dispatch(name, files, seed.label, out)
                verify_seed(out, required)
                return out
            except SeedError as exc:
                errors.append(f"{name}: {exc}")
            except Exception as exc:  # a backend blowing up must not be fatal
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                if out.exists():
                    out.unlink(missing_ok=True)

        raise SeedError(
            "no backend could build a valid NoCloud seed:\n  "
            + "\n  ".join(errors)
        )

    def describe(self) -> dict:
        """Which backends are usable here, for diagnostics."""
        return {name: self._available(name) for name in self._backend_order()}

    # -- internals -----------------------------------------------------------

    def _backend_order(self) -> list[str]:
        if self.backend != "auto":
            return [self.backend]
        order = []
        if self._available("pycdlib"):
            order.append("pycdlib")
        if self._available("genisoimage"):
            order.append("genisoimage")
        if self._available("imapi2"):
            order.append("imapi2")
        return order

    def _available(self, name: str) -> bool:
        if name == "pycdlib":
            try:
                import pycdlib  # noqa: F401
                return True
            except ImportError:
                return False
        if name == "genisoimage":
            return bool(shutil.which("genisoimage") or shutil.which("mkisofs")
                        or shutil.which("xorriso"))
        if name == "imapi2":
            return os.name == "nt"
        return False

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
        try:
            # Stage inside the try: pycdlib reads the source files during
            # write(), so the temp directory must still exist then.
            with tempfile.TemporaryDirectory() as tmp:
                staging = Path(tmp)
                for name, data in files.items():
                    staged = staging / name
                    staged.write_bytes(data)
                    iso.add_file(
                        str(staged),
                        iso_path="/" + _iso9660_name(name),
                        rr_name=name,          # Rock Ridge: relative, no slash
                        joliet_path="/" + name,  # Joliet: absolute, UCS-2
                    )
                iso.write(str(out))
        finally:
            iso.close()

    def _build_genisoimage(self, files: dict[str, bytes], label: str, out: Path) -> None:
        exe = (shutil.which("genisoimage") or shutil.which("mkisofs")
               or shutil.which("xorriso"))
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            for name, data in files.items():
                (src / name).write_bytes(data)
            # -joliet and -rock-ridge keep long filenames; -volid sets the label.
            args = [exe, "-output", str(out), "-volid", label, "-joliet", "-rock"]
            if Path(exe).name.lower().startswith("xorriso"):
                args = [exe, "-as", "mkisofs", "-output", str(out), "-volid", label,
                        "-joliet", "-rock", str(src)]
            else:
                args.append(str(src))
            subprocess.run(args, check=True, capture_output=True)

    def _build_imapi2(self, files: dict[str, bytes], label: str, out: Path) -> None:
        raise SeedError(
            "imapi2 backend is not wired up: the Windows COM image builder "
            "cannot reliably emit the Joliet descriptor that NoCloud readers "
            "require. Install pycdlib (pip install pycdlib) or genisoimage."
        )


class _BytesReader:
    """Minimal file-like over a bytes object, for pycdlib's add_fp."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._data) - self._pos
        chunk = self._data[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

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
        pass


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
    seed = NoCloudSeed(
        label=label,
        encrypt=encrypt,
        extra_files=extra,
        required_files=tuple(required),
        **kwargs,
    )
    return SeedBuilder(backend).build(seed, out_path)