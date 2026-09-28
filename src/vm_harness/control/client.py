"""A client for the hub: finds it from ``<home>/control.json`` (or VMH_URL + VMH_TOKEN), starts it if asked, calls it.

    from vm_harness.control.client import HubClient
    hub = HubClient.connect(start=True)          # start the hub in the background if none is running
    hub.call("vm.list")
    hub.call("vm.start", name="Kali")
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from vm_harness.control.hub import hub_alive, read_discovery


class HubError(Exception):
    def __init__(self, message: str, code: str = "error", status: int = 0) -> None:
        super().__init__(message)
        self.code, self.status = code, status


def start_hub(wait_s: float = 30.0) -> dict:
    """Start `python -m vm_harness serve` detached and wait until it answers. Returns its discovery record."""
    found = hub_alive()
    if found:
        return found
    root = Path(__file__).resolve().parents[3]
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root)])}
    flags = (0x00000008 | 0x00000200 | 0x08000000) if os.name == "nt" else 0
    log_dir = Path(os.environ.get("VMH_HOME") or Path.home() / ".vmharness")
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / "hub.log", "ab") as out:
        subprocess.Popen([str(windowless if windowless.exists() else exe), "-m", "vm_harness", "serve"],
                         cwd=str(root), env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                         creationflags=flags, start_new_session=os.name != "nt")
    deadline = time.time() + wait_s
    while time.time() < deadline:
        found = hub_alive(timeout=1.0)
        if found:
            return found
        time.sleep(0.3)
    raise HubError(f"the hub did not start within {wait_s:.0f}s (see {log_dir / 'hub.log'})", code="unavailable")


class HubClient:
    def __init__(self, url: str, token: str, *, client_name: str = "python") -> None:
        self.url, self.token, self.client_name = url.rstrip("/"), token, client_name

    @classmethod
    def connect(cls, *, start: bool = False, client_name: str = "python") -> "HubClient":
        if os.environ.get("VMH_URL") and os.environ.get("VMH_TOKEN"):
            return cls(os.environ["VMH_URL"], os.environ["VMH_TOKEN"], client_name=client_name)
        d = hub_alive() or (start_hub() if start else None)
        if not d:
            raise HubError("no VM-Harness hub is running (start one with: python -m vm_harness serve)",
                           code="unavailable")
        return cls(d["url"], d["token"], client_name=client_name)

    def _request(self, method: str, path: str, body: Any = None, timeout: float = 600.0) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
            "X-VMH-Client": self.client_name})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read() or b"{}")
            except ValueError:
                payload = {}
            raise HubError(payload.get("error") or f"HTTP {e.code}", code=payload.get("code", "error"), status=e.code)
        except urllib.error.URLError as e:
            raise HubError(f"the hub at {self.url} is not reachable: {e.reason}", code="unavailable")

    def call(self, op: str, args: Optional[dict] = None, /, timeout: float = 600.0, **kwargs: Any) -> Any:
        return self._request("POST", f"/v1/call/{op}", {**(args or {}), **kwargs}, timeout=timeout)["result"]

    def operations(self, query: str = "", group: str = "") -> list[dict]:
        q = "?" + urllib.parse.urlencode({k: v for k, v in (("q", query), ("group", group)) if v}) if (query or group) else ""
        return self._request("GET", "/v1/operations" + q)["operations"]

    def health(self) -> dict:
        return self._request("GET", "/v1/health", timeout=5)

