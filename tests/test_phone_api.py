"""The Android app's API (api_server.py) answers from the hub: every VM on every hypervisor by name, real actions,
snapshots, audit and logs. Before this it served one QMP bridge regardless of the VM named, and snapshot, audit and
log routes answered with placeholders or 404."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from vm_harness import api_server as api

VMS = [{"name": "omarchy", "backend": "qemu", "state": "running",
        "status": {"state": "running", "ram_allocated_mb": 8192, "cpus_allocated": 4, "uptime_seconds": 42,
                   "management_uri": "tcp:127.0.0.1:4444", "cpu_usage_pct": 3.5, "ram_usage_mb": 613}},
       {"name": "win11", "backend": "hyperv", "state": "stopped", "status": {}}]


def _server(tmp_path: Path, calls: list, fail: set[str] = frozenset()) -> api.QMCMApiServer:
    s = api.QMCMApiServer(tailscale_only=False, signing_key_dir=tmp_path / "keys", credential_store_dir=tmp_path / "creds")

    async def ok_auth(request):  # the pairing/auth layer has its own tests; here every caller is authorized
        return api.RequestContext(api_key="unused", api_key_meta=None, source_ip="100.64.0.2", vm_name=None)

    async def no_audit(*a, **k):
        return None

    async def hub(op: str, args: dict | None = None, timeout: float = 300.0) -> Any:
        calls.append((op, dict(args or {})))
        if op in fail:
            raise RuntimeError(f"VM '{(args or {}).get('name')}' not found")
        return {
            "vm.list": VMS,
            "vm.status": VMS[0]["status"],
            "vm.config": {"name": "omarchy", "ram_mb": 8192, "disk_path": "C:/vm/disk.qcow2"},
            "vm.snapshot.list": [{"name": "clean", "created_at": "2026-09-29T10:00:00", "size_bytes": 1024, "is_current": True}],
            "audit.query": [{"ts": 1790690000.0, "operation": "vm.start", "args": {"name": "omarchy"}, "client": "phone", "ok": True}],
        }.get(op, {})

    s._require_tailscale_or_auth = ok_auth
    s._audit = no_audit
    s._hub_call = hub
    return s


def _run(tmp_path, fn, fail: set[str] = frozenset()):
    calls: list = []

    async def go():
        s = _server(tmp_path, calls, fail)
        async with TestClient(TestServer(s._build_app())) as c:
            return await fn(c)
    return asyncio.run(go()), calls


def test_every_vm_is_listed_with_its_backend_and_resources(tmp_path):
    async def fn(c):
        r = await c.get("/api/v1/vms")
        return r.status, await r.json()
    (status, vms), _ = _run(tmp_path, fn)
    assert status == 200
    assert [v["name"] for v in vms] == ["omarchy", "win11"]
    assert vms[0] | {} == vms[0] and vms[0]["status"] == "running" and vms[0]["backend"] == "qemu"
    assert (vms[0]["ram"], vms[0]["vcpus"], vms[0]["uptime"]) == (8192, 4, "42s")


def test_actions_go_to_the_named_vm(tmp_path):
    async def fn(c):
        out = []
        for action in ("start", "stop", "powerdown", "pause", "resume", "reset"):
            r = await c.post(f"/api/v1/vms/win11/{action}", json={})
            out.append((action, r.status, (await r.json())["status"]))
        bad = await c.post("/api/v1/vms/win11/explode", json={})
        out.append(("explode", bad.status, None))
        return out
    out, calls = _run(tmp_path, fn)
    assert all(s == 200 and st == "ok" for a, s, st in out if a != "explode")
    assert out[-1][1] == 400
    assert calls == [("vm.start", {"name": "win11", "headless": True}), ("vm.stop", {"name": "win11", "force": True}),
                     ("vm.stop", {"name": "win11", "force": False}), ("vm.pause", {"name": "win11"}),
                     ("vm.resume", {"name": "win11"}), ("vm.reset", {"name": "win11"})]


def test_a_failed_action_is_reported_not_claimed(tmp_path):
    async def fn(c):
        r = await c.post("/api/v1/vms/ghost/start", json={})
        return r.status, await r.text()
    (status, text), _ = _run(tmp_path, fn, fail={"vm.start"})
    assert status == 404 and "not found" in text


def test_snapshots_are_listed_created_and_restored(tmp_path):
    async def fn(c):
        listed = await (await c.get("/api/v1/vms/omarchy/snapshots")).json()
        created = await c.post("/api/v1/vms/omarchy/snapshots", json={"name": "before-update"})
        restored = await c.post("/api/v1/vms/omarchy/snapshots/clean/restore", json={})
        return listed, created.status, restored.status
    (listed, created, restored), calls = _run(tmp_path, fn)
    assert listed == [{"name": "clean", "vmName": "omarchy", "created": "2026-09-29T10:00:00", "sizeBytes": 1024, "current": True}]
    assert (created, restored) == (200, 200)
    assert ("vm.snapshot.create", {"name": "omarchy", "snapshot_name": "before-update", "description": ""}) in calls
    assert ("vm.snapshot.restore", {"name": "omarchy", "snapshot_name": "clean"}) in calls


def test_detail_audit_logs_and_metrics_are_real(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "hub.log").write_text("2026-09-29 10:00:00,001 INFO vmharness.hub: started\n"
                                  "2026-09-29 10:00:01,002 ERROR vmharness.qemu: boom\nTraceback line\n", encoding="utf-8")
    monkeypatch.setenv("VMH_HOME", str(home))

    async def fn(c):
        detail = await (await c.get("/api/v1/vms/omarchy")).json()
        audit = await (await c.get("/api/v1/security/audit")).json()
        logs = await (await c.get("/api/v1/logs")).json()
        errors = await (await c.get("/api/v1/logs?level=ERROR")).json()
        metrics = await (await c.get("/api/v1/metrics")).json()
        return detail, audit, logs, errors, metrics
    (detail, audit, logs, errors, metrics), _ = _run(tmp_path, fn)
    assert detail["status"] == "running" and detail["config"]["disk_path"] == "C:/vm/disk.qcow2" and detail["ramUsedMb"] == 613
    assert audit[0]["event"] == "vm.start" and audit[0]["vmName"] == "omarchy" and audit[0]["status"] == "ok"
    assert [e["level"] for e in logs["logs"]] == ["INFO", "ERROR"] and logs["logs"][1]["message"].endswith("Traceback line")
    assert [e["message"].split("\n")[0] for e in errors["logs"]] == ["boom"]
    assert metrics["ramTotal"] > 0 and metrics["diskTotal"] > 0 and metrics["timestamp"] > 0
    json.dumps(metrics)
