"""Running host commands for the backends: off the event loop, without console windows, decoded correctly.

Every backend drives a command-line tool (VBoxManage, vmrun, wsl.exe, powershell, qemu-img, virsh, podman). Calling
``subprocess.run`` from an ``async def`` blocks the whole event loop, and with it the API server, the MCP server and the
GUI's bridge, for as long as the tool takes. ``run`` has the same signature and result as ``subprocess.run`` but waits
in a worker thread.

It also fixes two Windows problems every backend had:

* ``wsl.exe`` writes UTF-16LE, which decoded as UTF-8 gives ``"U\\x00b\\x00u..."``; output is decoded as UTF-16 when it
  looks like it (a BOM, or NUL bytes in every other position).
* A console program started from a windowless process flashes a console window; ``CREATE_NO_WINDOW`` is always set.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def decode(data: Optional[bytes]) -> str:
    """Text from a tool's output, whether it wrote UTF-8, UTF-16LE (wsl.exe) or the ANSI code page."""
    if not data:
        return ""
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le", errors="replace")
    sample = data[:200]
    if len(sample) >= 4 and sample[1::2].count(0) >= len(sample[1::2]) * 0.8:
        return data.decode("utf-16-le", errors="replace").replace("\x00", "")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("mbcs" if sys.platform == "win32" else "latin-1", errors="replace")


def run_sync(args: Sequence[str], *, timeout: Optional[float] = 30, check: bool = False, input: Any = None,
             text: bool = True, cwd: Optional[str] = None, env: Optional[dict] = None, capture_output: bool = True,
             **_ignored: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` with the decoding and window fixes above. ``text=False`` returns raw bytes."""
    data = input.encode("utf-8") if isinstance(input, str) else input
    kwargs: dict[str, Any] = {"input": data, "timeout": timeout, "cwd": cwd, "env": env}
    if capture_output:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if data is None:
        kwargs["stdin"] = subprocess.DEVNULL
    if CREATE_NO_WINDOW:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    proc = subprocess.run([str(a) for a in args], **kwargs)
    out, err = proc.stdout, proc.stderr
    if text:
        out, err = decode(out), decode(err)
    result = subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, proc.args, out, err)
    return result


async def run(args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """``run_sync`` in a worker thread, so the event loop keeps serving while the tool runs."""
    return await asyncio.to_thread(run_sync, args, **kwargs)


def find_tool(names: Iterable[str], candidates: Iterable[str | os.PathLike] = (), env_var: Optional[str] = None) -> Optional[str]:
    """The first of: ``$env_var``, each name on PATH, each candidate path that exists. None if none is found."""
    if env_var and os.environ.get(env_var):
        p = Path(os.environ[env_var])
        if p.is_file():
            return str(p)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for c in candidates:
        p = Path(os.path.expandvars(os.path.expanduser(str(c))))
        if p.is_file():
            return str(p)
    return None
