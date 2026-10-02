#!/usr/bin/env python3
"""
Standalone PyInstaller build script for VM-Harness.

Writes build.spec (with hidden imports, Qt plugins, QEMU binaries, .env exclusion)
then invokes PyInstaller to produce a single-file Windows GUI executable.

Usage:
    python scripts/build_pyinstaller.py          # build
    python scripts/build_pyinstaller.py --clean   # remove dist/ + build/ first
    python scripts/build_pyinstaller.py --spec   # write build.spec only, no build
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
# Suppress CLI console windows on Windows
CREATE_NO_WINDOW = 0x08000000
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
ENTRY = PROJECT / "gui" / "__main__.py"
QEMU_DIR = Path(r"C:\Program Files/qemu")
SPEC = PROJECT / "build.spec"
DIST_EXE = PROJECT / "dist" / "VM-Harness.exe"

META = {
    "name": "VM-Harness",
    "version": "0.1.0",
}

# ── hidden imports ──────────────────────────────────────────────────────────
HIDDEN_IMPORTS = [
    # PyQt5 core / gui — often missed by analysis
    "PyQt5",
    "PyQt5.QtCore",
    "PyQt5.QtGui",
    "PyQt5.QtWidgets",
    "PyQt5.QtNetwork",
    "PyQt5.QtWebSockets",
    "PyQt5.QtWebChannel",
    "PyQt5.QtMultimedia",
    "PyQt5.QtMultimediaWidgets",
    "sip",
    # cryptography — native / hazmat bindings
    "cryptography",
    "cryptography.hazmat",
    "cryptography.hazmat.primitives",
    "cryptography.hazmat.primitives.asymmetric",
    "cryptography.hazmat.primitives.serialization",
    "cryptography.hazmat.primitives.traditional_ciphers",
    "cryptography.fernet",
    "cryptography.utils",
    "cryptography.x509",
    "cryptography.hazmat.bindings._openssl",
    "cryptography.hazmat.bindings._rust",
    # aiohttp — submodules frequently missed
    "aiohttp",
    "aiohttp._websocket",
    "aiohttp._http_parser",
    "aiohttp._web_protocol",
    "aiohttp.client",
    "aiohttp.server",
    "aiohttp.helpers",
    "aiohttp.payload",
    "aiohttp.streams",
    "aiohttp.signals",
    "aiohttp.web",
    "aiohttp.web_protocol",
    "aiohttp.web_request",
    "aiohttp.web_response",
    "aiohttp.web_ws",
    "aiohttp.http_exceptions",
    "aiohttp.http_parser",
    "aiohttp.log",
    "aiohttp.errors",
    "aiohttp.multipart",
    # asyncssh — submodules
    "asyncssh",
    "asyncssh._ssh",
    "asyncssh.auth",
    "asyncssh.connection",
    "asyncssh.forward",
    "asyncssh.misc",
    "asyncssh.sftp",
    "asyncssh.sftp_client",
    "asyncssh.sftp_server",
    "asyncssh.ssh_agent",
    # matplotlib — backends + C extensions
    "matplotlib",
    "matplotlib.backends.backend_qt5agg",
    "matplotlib.backends.backend_agg",
    "matplotlib.backends._backend_agg",
    "matplotlib._qhull",
    "matplotlib._image",
    "matplotlib._mathtext",
    "matplotlib._contour",
    "matplotlib._tight_bbox",
    "matplotlib.font_manager",
    "matplotlib.textpath",
    "matplotlib.scale",
    "matplotlib.transforms",
    "matplotlib.backends._qt_helpers",
    # mcp package
    "mcp",
    "mcp.server",
    "mcp.server.models",
    "mcp.server.auth",
    "mcp.server.sessions",
    "mcp.server.stdio",
    "mcp.shared",
    "mcp.shared.memory",
    "mcp.types",
    "mcp.client",
    "mcp.client.session",
    "mcp.exceptions",
    # stdlib modules that PyInstaller sometimes misses on Windows
    "psutil",
    "pydantic",
    "pydantic_settings",
    "python_dotenv",
    "loguru",
    "pytest",
    "_ctypes",
]

# ── Qt platform plugins that must accompany the exe on Windows ─────────────
# Collected from the venv's PyQt5/Qt5/plugins tree; omit anything that isn't
# on disk so this list stays correct across PyQt5 versions.
def collect_qt_plugins(plugins_root: Path) -> list[tuple[Path, Path]]:
    """Return (absolute_source, relative_dest_under_qt_plugins) pairs."""
    dest_root = Path("qt_plugins")
    out: list[tuple[Path, Path]] = []
    if not plugins_root.exists():
        return out
    for path in sorted(plugins_root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(plugins_root)
            out.append((path, dest_root / rel))
    return out


def collect_qemu_binaries(qemu_dir: Path) -> list[tuple[Path, Path]]:
    """Return (absolute_source, dest_under_qemu/) pairs for the two required binaries."""
    out: list[tuple[Path, Path]] = []
    if not qemu_dir.exists():
        return out
    for name in ("qemu-system-x86_64.exe", "qemu-img.exe"):
        src = qemu_dir / name
        if src.exists():
            out.append((src, Path("qemu") / name))
    return out


def _pyi_path(p: Path) -> str:
    """Return a Python string literal for a Windows path safe in a .spec file.

    PyInstaller's spec parser runs Python on the spec text, so the literal
    must be a valid Python string.  Single backslashes are fine in a normal
    Python string — ``C:\\Path`` — so we just escape each backslash once.
    """
    raw = str(p)
    # escape backslashes once: \ -> \\
    return raw.replace("\\", "\\\\")


def write_spec(
    spec_path: Path,
    entry: str,
    hidden: list[str],
    qemu_data: list[tuple[Path, Path]],
    qt_data: list[tuple[Path, Path]],
    name: str,
    version: str,
    binaries: list[tuple[Path, Path]] | None = None,
) -> None:
    """Emit a ready-to-run build.spec."""
    lines: list[str] = []
    lines.append("# -*- mode: python ; coding: utf-8 -*-")
    lines.append(f"# Auto-generated by {__file__} — re-run that script to refresh.")
    lines.append("")
    lines.append("block_cipher = None")
    lines.append("")
    lines.append("# ── Analysis ──────────────────────────────────────────────────────────")
    lines.append("a = Analysis(")
    lines.append(f"    [{chr(34)}{entry}{chr(34)}],")
    lines.append("")
    lines.append("    pathex=[],")
    lines.append("")
    # binaries: Python DLL for the frozen EXE + any other .dll/.so/.dylib files
    if binaries:
        lines.append("    binaries=[")
        for src, dest in binaries:
            lines.append(f"        ({str(src)!r}, {str(dest)!r}),")
        lines.append("    ],")
    else:
        lines.append("    binaries=[],")
    lines.append("")

    # datas: QEMU binaries first, then Qt plugins
    if qemu_data or qt_data:
        lines.append("    datas=[")
        for src, dest in qemu_data:
            lines.append(f"        ({str(src)!r}, {str(dest)!r}),")
        for src, dest in qt_data:
            lines.append(f"        ({str(src)!r}, {str(dest)!r}),")
        lines.append("    ],")
    else:
        lines.append("    datas=[],")
    lines.append("")

    lines.append("    hiddenimports=[")
    for h in hidden:
        lines.append(f"        {h!r},")
    lines.append("    ],")
    lines.append("")
    lines.append("    hookspath=[],")
    lines.append("    hooksconfig={},")
    lines.append("")
    lines.append("    runtime_hooks=[],")
    lines.append("    excludes=[")
    lines.append("        # .env files carry secrets & user-specific config.")
    lines.append("        # They are loaded at runtime from the user config dir,")
    lines.append("        # never bundled inside the frozen executable.")
    lines.append('        "*.env",')
    lines.append('        ".env",')
    lines.append('        "config.env",')
    lines.append("    ],")
    lines.append("")
    lines.append("    win_no_prefer_redirects=False,")
    lines.append("    win_private_assemblies=False,")
    lines.append("    cipher=block_cipher,")
    lines.append("    noarchive=False,")
    lines.append(")")
    lines.append("")
    lines.append("# ── PYZ ──────────────────────────────────────────────────────────────")
    lines.append("pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)")
    lines.append("")
    lines.append("# ── EXE (single-file, no console for GUI app) ──────────────────────")
    lines.append("exe = EXE(")
    lines.append("    pyz,")
    lines.append("    a.scripts,")
    lines.append("    [],")
    lines.append("    exclude_binaries=False,")
    lines.append(f"    name={name!r},")
    lines.append("    debug=False,")
    lines.append("    bootloader_ignore_signals=False,")
    lines.append("    strip=False,")
    lines.append("    upx=True,")
    lines.append("    console=False,")
    lines.append("    disable_windowed_traceback=False,")
    lines.append("    target_arch=None,")
    lines.append("    codesign_identity=None,")
    lines.append("    entitlements_file=None,")
    lines.append(")")
    lines.append("")
    lines.append("# ── post-build sanity check ──────────────────────────────────────────")
    lines.append("import os, sys")
    lines.append("# __file__ is not defined when PyInstaller execs the spec; use cwd")
    lines.append(f"target = os.path.join(os.getcwd(), 'dist', '{name}.exe')")
    lines.append("if not os.path.exists(target):")
    lines.append("    print('ERROR: build produced no exe at', target)")
    lines.append("    sys.exit(1)")
    lines.append("print()")
    lines.append("print('BUILD OK  -->', target)")
    lines.append("print('size     :', f'{os.path.getsize(target):,} bytes')")

    spec_path.write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Build VM-Harness single-file Windows executable")
    ap.add_argument("--clean", action="store_true", help="remove dist/ and build/ before building")
    ap.add_argument("--spec", action="store_true", help="write build.spec and exit without building")
    args = ap.parse_args()

    # Locate python311.dll — required for the frozen EXE to run.
    # Prefer the venv DLL (already copied), fall back to system Python install.
    dll_sources = [
        Path(sys.prefix) / "python311.dll",
        Path(r"C:\Users\Server\AppData\Local\Programs\Python\Python311\python311.dll"),
    ]
    python_dll: Path | None = None
    # Ensure .pyd binary embedded for frozen EXE loader
    for candidate in dll_sources:
        if candidate.exists():
            python_dll = candidate
            print(f"[python] DLL found: {candidate}")
            break
    if python_dll is None:
        print("[warn] python311.dll not found — EXE will fail to launch without it", file=sys.stderr)

    binaries: list[tuple[Path, Path]] = []
    if python_dll is not None:
        binaries.append((python_dll, Path(".")))
    # Rust supervisor .pyd - must be embedded in frozen EXE
    pyd_src = PROJECT / "target/release/vmharness_supervisor.pyd"
    if pyd_src.exists():
        binaries.append((pyd_src, Path("_internal") / "vmharness_supervisor.pyd"))
    qt_plugins: list[tuple[Path, Path]] = []
    try:
        from PyQt5.QtCore import QLibraryInfo

        plugins_root = Path(QLibraryInfo.location(QLibraryInfo.PluginsPath))
        qt_plugins = collect_qt_plugins(plugins_root)
        print(f"[qt] plugins dir : {plugins_root}")
        print(f"[qt] files found : {len(qt_plugins)}")
    except Exception as e:
        print(f"[warn] could not locate Qt plugins ({e})", file=sys.stderr)

    # ── locate QEMU binaries ─────────────────────────────────────────────────
    qemu_binaries = collect_qemu_binaries(QEMU_DIR)
    if QEMU_DIR.exists():
        print(f"[qemu] dir       : {QEMU_DIR}")
        print(f"[qemu] binaries  : {len(qemu_binaries)}")
    else:
        print(f"[warn] QEMU dir not found at {QEMU_DIR}", file=sys.stderr)

    # ── write spec ───────────────────────────────────────────────────────────
    write_spec(
        spec_path=SPEC,
        entry=str(ENTRY),
        hidden=HIDDEN_IMPORTS,
        qemu_data=qemu_binaries,
        qt_data=qt_plugins,
        name=META["name"],
        version=META["version"],
        binaries=binaries,
    )
    print(f"[spec] written   : {SPEC}")

    if args.spec:
        print("Done (--spec).")
        return

    # ── clean (optional) ────────────────────────────────────────────────────
    if args.clean:
        for d in (PROJECT / "dist", PROJECT / "build"):
            if d.exists():
                print(f"[clean] {d}")
                shutil.rmtree(d)

    # ── invoke PyInstaller ───────────────────────────────────────────────────
    print(f"\nRunning: pyinstaller {SPEC}")
    res = subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(SPEC)],
        cwd=str(PROJECT),
        check=False,
    )
    if res.returncode != 0:
        print(f"\nPyInstaller exited with code {res.returncode}", file=sys.stderr)
        sys.exit(res.returncode)

    # ── verify ───────────────────────────────────────────────────────────────
    if DIST_EXE.exists():
        size_mb = DIST_EXE.stat().st_size / (1024 * 1024)
        print(f"\nBUILD OK : {DIST_EXE}  ({size_mb:.2f} MB)")
    else:
        print(f"\nBUILD FAILED : {DIST_EXE} not found", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
