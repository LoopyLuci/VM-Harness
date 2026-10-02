"""Headless VM-Harness server — connects MultiVMManager + QMP bridge to the REST API."""

from __future__ import annotations

import argparse
import asyncio
import os
import ssl
import sys
import json
import subprocess
# Suppress CLI console windows on Windows
CREATE_NO_WINDOW = 0x08000000
import time
from pathlib import Path
from typing import Any, Callable, Coroutine

PROJECT_ROOT = Path(__file__).resolve().parent
CERT_FILE = PROJECT_ROOT / "cert.pem"
KEY_FILE = PROJECT_ROOT / "key.pem"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import aiohttp
from aiohttp import web

from gui.multi_vm import MultiVMManager, DEFAULT_QEMU_BINARY
from gui.multi_vm_qmp_bridge import MultiVMQMPBridge
from src.vm_harness.api_server import QMCMApiServer, get_tailscale_info, log


def ensure_tls_certs(cert_path: Path = CERT_FILE, key_path: Path = KEY_FILE) -> tuple[Path, Path]:
    """Ensure TLS certificates exist; generate self-signed if missing."""
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path
    # Import here so --no-tls doesn't require cryptography
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "generate_cert.py")],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Certificate generation failed: {result.stderr}")
    if not cert_path.exists() or not key_path.exists():
        raise RuntimeError("Certificate generation did not produce expected files")
    return cert_path, key_path


def create_ssl_context(cert_path: Path = CERT_FILE, key_path: Path = KEY_FILE) -> ssl.SSLContext:
    """Create an SSLContext with the given cert/key pair."""
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return ssl_ctx


class HeadlessServer(QMCMApiServer):
    """Extended API server wiring MultiVMManager into the REST API."""

    def __init__(self, vm_manager: MultiVMManager, multi_bridge: MultiVMQMPBridge, **kwargs):
        super().__init__(**kwargs)
        self._vm_manager = vm_manager
        self._multi_bridge = multi_bridge

    def _build_app(self):
        """Build app from scratch with all routes, using subclass handlers."""
        app = web.Application()

        @web.middleware
        async def json_error_middleware(request, handler):
            try:
                return await handler(request)
            except web.HTTPException as exc:
                exc.content_type = "application/json"
                if isinstance(exc.text, bytes):
                    try:
                        exc.text = exc.text.decode("utf-8")
                    except:
                        pass
                if not exc.text or not exc.text.startswith("{"):
                    exc.text = json.dumps({"error": exc.reason or "http_error"})
                raise

        app.middlewares.append(json_error_middleware)

        @web.middleware
        async def api_auth_middleware(request, handler):
            if request.path.startswith("/api/v1/auth/pair"):
                return await handler(request)
            if request.path.startswith("/api/v1/auth/public-key"):
                return await handler(request)
            ctx = await self._require_tailscale_or_auth(request)
            request._vmharness_ctx = ctx
            return await handler(request)

        app.middlewares.append(api_auth_middleware)

        # Auth routes
        app.router.add_post("/api/v1/auth/pair", self._handle_pair)
        app.router.add_get("/api/v1/auth/verify", self._handle_verify)
        app.router.add_post("/api/v1/auth/revoke", self._handle_revoke)
        app.router.add_get("/api/v1/auth/public-key", self._handle_public_key)

        # Dashboard
        app.router.add_get("/api/v1/", self._handle_dashboard)

        # VM lifecycle routes (specific BEFORE generic)
        app.router.add_get("/api/v1/vms", self._handle_vms_list)
        app.router.add_post("/api/v1/vms", self._handle_vm_create)
        # Sub-routes BEFORE {name} to prevent shadowing
        app.router.add_get("/api/v1/vms/{name}/snapshots", self._handle_list_snapshots)
        app.router.add_post("/api/v1/vms/{name}/snapshots", self._handle_create_snapshot)
        app.router.add_post("/api/v1/vms/{name}/snapshots/{snapshot_name}/restore", self._handle_restore_snapshot)
        app.router.add_post("/api/v1/vms/{name}/qmp", self._handle_qmp_command)
        # Generic VM routes AFTER sub-routes
        app.router.add_get("/api/v1/vms/{name}", self._handle_vm_detail)
        app.router.add_post("/api/v1/vms/{name}/{action}", self._handle_vm_action)

        # Other routes
        app.router.add_get("/api/v1/metrics", self._handle_metrics)
        app.router.add_get("/api/v1/settings", self._handle_settings_get)
        app.router.add_put("/api/v1/settings", self._handle_settings_put)
        app.router.add_get("/api/v1/credentials", self._handle_credentials_list)
        app.router.add_get("/api/v1/credentials/{name}", self._handle_credential_detail)
        app.router.add_get("/api/v1/security/audit", self._handle_audit)
        app.router.add_get("/api/v1/logs", self._handle_logs)

        # ── Protobuf-compatible endpoint ─────────────────────────────────────────
        # Accepts JSON-serialized protobuf messages and dispatches to VM lifecycle.
        # Protocol version: 3.0.0
        async def proto_handle(request):
            try:
                body = await request.json()
            except:
                return self._json({"error": "invalid_json", "protocol": "3.0.0"}, status=400)

            msg_type = body.get("type", "").lower()
            response = {"protocol": "3.0.0", "status": "ok"}

            if msg_type == "vm_config":
                try:
                    from crates.protocol.src.proto_bindings import VmConfig
                    config = VmConfig.from_dict(body.get("payload", {}))
                    errors = config.validate()
                    if errors:
                        return self._json({"error": "; ".join(errors), "protocol": "3.0.0", "status": "validation_failed"}, status=400)
                    vm_name = config.name or body.get("name", "")
                    if vm_name:
                        ok, msg = self._vm_manager.add_vm(vm_name, {
                            'disk_path': config.disk_path,
                            'ram_mb': config.memory_mb,
                            'cpus': config.cpus,
                            'display': config.display_type,
                        })
                        response["detail"] = msg
                        response["status"] = "ok" if ok else "error"
                    else:
                        response["status"] = "error"
                        response["detail"] = "missing_name"
                except Exception as e:
                    return self._json({"error": str(e), "protocol": "3.0.0", "status": "error"}, status=500)

            elif msg_type == "vm_metrics":
                vm_name = body.get("vm_name", "")
                if vm_name:
                    summary = self._vm_manager.get_summary(vm_name)
                    response["metrics"] = {
                        "vm_id": vm_name,
                        "cpu_percent": summary.cpu_usage if summary else 0.0,
                        "memory_used_mb": summary.ram_usage_mb if summary else 0,
                        "memory_total_mb": summary.ram_mb if summary else 0,
                        "disk_read_bytes": 0,
                        "disk_write_bytes": 0,
                        "net_rx_bytes": 0,
                        "net_tx_bytes": 0,
                        "uptime_seconds": summary.uptime_seconds if summary else 0,
                    } if summary else None
                    if not summary:
                        response["status"] = "error"
                        response["detail"] = "vm_not_found"
                else:
                    all_metrics = []
                    for vm_name in self._vm_manager.list_vms():
                        summary = self._vm_manager.get_summary(vm_name)
                        if summary:
                            all_metrics.append({
                                "vm_id": vm_name,
                                "cpu_percent": summary.cpu_usage,
                                "memory_used_mb": summary.ram_usage_mb,
                                "memory_total_mb": summary.ram_mb,
                                "disk_read_bytes": 0,
                                "disk_write_bytes": 0,
                                "net_rx_bytes": 0,
                                "net_tx_bytes": 0,
                                "uptime_seconds": summary.uptime_seconds,
                            })
                    response["metrics"] = all_metrics

            elif msg_type == "pairing_token":
                device_id = body.get("device_id", "")
                token = body.get("token", "")
                from gui.panels_pairing import PairingManager
                pm = PairingManager()
                result = pm.verify_token(device_id, token)
                response["pairing"] = result
                response["status"] = "ok" if result.get("valid") else "invalid"

            elif msg_type == "chat_message":
                chat_content = body.get("payload", {}).get("content", "")
                response["chat"] = {"content": chat_content, "protocol": "3.0.0"}
                response["status"] = "ok"

            elif msg_type == "audit_event":
                audit_payload = body.get("payload", {})
                audit_id = audit_payload.get("id", "")
                audit_type = audit_payload.get("audit_type", "")
                audit_source = audit_payload.get("source", "")
                audit_ts = audit_payload.get("timestamp", "")
                audit_user = audit_payload.get("user", "")
                response["audit"] = {
                    "id": audit_id,
                    "audit_type": audit_type,
                    "source": audit_source,
                    "timestamp": audit_ts,
                    "user": audit_user,
                    "protocol": "3.0.0",
                }
                response["status"] = "ok"

            else:
                return self._json({"error": f"unknown_msg_type: {msg_type}", "protocol": "3.0.0", "status": "error"}, status=400)

            return self._json(response)

        app.router.add_post("/v1/proto", proto_handle)

        async def health(request):
            return web.json_response({"status": "ok"})

        app.router.add_get("/health", health)

        # ── Container routes ───────────────────────────────────────────────────

        async def container_stats(request):
            """Get real-time container stats from Docker."""
            try:
                import docker
                client = docker.from_env()
                containers = client.containers.list(all=True)
                stats = []
                for c in containers:
                    try:
                        s = c.stats(stream=False)
                        cpu_delta = s["cpu_stats"]["cpu_usage"]["total_usage"] - s["precpu_stats"]["cpu_usage"]["total_usage"]
                        system_delta = s["cpu_stats"]["system_cpu_usage"] - s["precpu_stats"]["system_cpu_usage"]
                        cpu_pct = (cpu_delta / system_delta) * 100.0 if system_delta > 0 else 0.0
                        mem_usage = s["memory_stats"].get("usage", 0)
                        mem_limit = s["memory_stats"].get("limit", 1)
                        mem_pct = (mem_usage / mem_limit) * 100.0 if mem_limit > 0 else 0.0
                        stats.append({
                            "name": c.name,
                            "status": c.status,
                            "cpu_percent": f"{cpu_pct:.1f}%",
                            "memory_usage": f"{mem_usage / 1024 / 1024:.1f} MB",
                            "memory_limit": f"{mem_limit / 1024 / 1024:.1f} MB",
                            "memory_percent": f"{mem_pct:.1f}%",
                            "network_io": "0 B",
                            "disk_io": "0 B",
                        })
                    except Exception:
                        stats.append({"name": c.name, "status": c.status, "cpu_percent": "0%", "memory_usage": "0 MB"})
                return web.json_response({"containers": stats})
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)

        async def container_logs(request):
            """Get container logs."""
            container_name = request.match_info.get("name", "")
            try:
                import docker
                client = docker.from_env()
                container = client.containers.get(container_name)
                logs = container.logs(tail=100).decode("utf-8", errors="replace")
                return web.json_response({"logs": logs})
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)

        async def container_exec(request):
            """Execute command in container."""
            container_name = request.match_info.get("name", "")
            try:
                data = await request.json()
                command = data.get("command", "")
                import docker
                client = docker.from_env()
                container = client.containers.get(container_name)
                result = container.exec_run(command, stdout=True, stderr=True)
                return web.json_response({
                    "exit_code": result.exit_code,
                    "output": result.output.decode("utf-8", errors="replace"),
                })
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)

        app.router.add_get("/api/v1/containers/stats", container_stats)
        app.router.add_get("/api/v1/containers/{name}/logs", container_logs)
        app.router.add_post("/api/v1/containers/{name}/exec", container_exec)

        return app

    # ── VM listing & detail ──────────────────────────────────────────────────

    async def _handle_vms_list(self, request):
        vms = []
        for vm_name in self._vm_manager.list_vms():
            summary = self._vm_manager.get_summary(vm_name)
            vms.append(summary.to_dict() if summary else {"name": vm_name, "status": "unknown"})
        return self._json(vms)

    async def _handle_vm_detail(self, request):
        vm_name = request.match_info.get("name", "")
        if vm_name not in self._vm_manager.list_vms():
            return self._json({"error": "vm_not_found"}, status=404)
        summary = self._vm_manager.get_summary(vm_name)
        return self._json(summary.to_dict() if summary else {"name": vm_name, "status": "unknown"})

    async def _handle_vm_create(self, request):
        try:
            body = await request.json()
        except:
            return self._json({"error": "invalid_json"}, status=400)
        name = body.get("name", "").strip()
        if not name:
            return self._json({"error": "missing_name"}, status=400)
        if name in self._vm_manager.list_vms():
            return self._json({"error": "vm_exists"}, status=409)
        ram_mb = int(body.get("ram_mb", 4096))
        cpus = int(body.get("cpus", 2))
        disk_size_gb = int(body.get("disk_size_gb", 40))
        disk_path = os.path.expanduser(f'~/.qemu-mcp/{name}.qcow2')
        os.makedirs(os.path.dirname(disk_path), exist_ok=True)
        if not os.path.exists(disk_path):
            qemu_img = os.path.join(os.path.dirname(DEFAULT_QEMU_BINARY), 'qemu-img.exe')
            try:
                subprocess.run([qemu_img, 'create', '-f', 'qcow2', disk_path, f'{disk_size_gb}G'], capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
            except Exception as e:
                return self._json({"error": f"disk_create_failed: {e}"}, status=500)
        ok, msg = self._vm_manager.add_vm(name, {'disk_path': disk_path, 'ram_mb': ram_mb, 'cpus': cpus, 'display': 'none'})
        if not ok:
            return self._json({"error": msg}, status=400)
        qmp_uri = self._vm_manager.get_qmp_uri(name)
        if qmp_uri:
            self._multi_bridge.add_vm(name, qmp_uri)
        return self._json({"vm": name, "action": "create", "status": "ok", "detail": msg})

    async def _handle_vm_action(self, request):
        vm_name = request.match_info.get("name", "")
        action = request.match_info.get("action", "")
        if vm_name not in self._vm_manager.list_vms():
            return self._json({"error": "vm_not_found"}, status=404)
        if action == "start":
            if vm_name not in self._vm_manager._running:
                ok, msg = self._vm_manager.start_vm(vm_name)
                return self._json({"vm": vm_name, "action": action, "status": "ok" if ok else "error", "detail": msg})
            return self._json({"vm": vm_name, "action": action, "status": "error", "detail": "VM already running"})
        elif action == "stop":
            bridge = self._multi_bridge.get_bridge(vm_name)
            if bridge and bridge.is_connected:
                bridge.system_powerdown()
                return self._json({"vm": vm_name, "action": action, "status": "ok", "detail": "Powerdown issued via QMP"})
            proc = self._vm_manager._running.get(vm_name)
            if proc:
                ok, msg = self._vm_manager.stop_vm(vm_name)
                return self._json({"vm": vm_name, "action": action, "status": "ok" if ok else "error", "detail": msg})
            ok, msg = self._vm_manager.stop_vm(vm_name)
            return self._json({"vm": vm_name, "action": action, "status": "ok" if ok else "error", "detail": msg})
        elif action == "reset":
            bridge = self._multi_bridge.get_bridge(vm_name)
            if bridge and bridge.is_connected:
                bridge.system_reset()
                return self._json({"vm": vm_name, "action": action, "status": "ok", "detail": "Reset issued"})
            return self._json({"vm": vm_name, "action": action, "status": "error", "detail": "Not connected"})
        elif action == "pause":
            bridge = self._multi_bridge.get_bridge(vm_name)
            if bridge and bridge.is_connected:
                bridge.stop_vm()
                self._vm_manager.pause_vm(vm_name)
                return self._json({"vm": vm_name, "action": action, "status": "ok", "detail": "Paused"})
            return self._json({"vm": vm_name, "action": action, "status": "error", "detail": "Not connected"})
        elif action == "resume":
            bridge = self._multi_bridge.get_bridge(vm_name)
            if bridge and bridge.is_connected:
                bridge.cont()
                self._vm_manager.resume_vm(vm_name)
                return self._json({"vm": vm_name, "action": action, "status": "ok", "detail": "Resumed"})
            return self._json({"vm": vm_name, "action": action, "status": "error", "detail": "Not connected"})
        elif action == "powerdown":
            bridge = self._multi_bridge.get_bridge(vm_name)
            if bridge and bridge.is_connected:
                bridge.system_powerdown()
                return self._json({"vm": vm_name, "action": action, "status": "ok", "detail": "Powerdown issued"})
            return self._json({"vm": vm_name, "action": action, "status": "error", "detail": "Not connected"})
        elif action == "eject":
            bridge = self._multi_bridge.get_bridge(vm_name)
            if bridge and bridge.is_connected:
                return self._json({"vm": vm_name, "action": action, "status": "ok", "detail": "Eject issued"})
            return self._json({"vm": vm_name, "action": action, "status": "error", "detail": "Not connected"})
        return self._json({"error": "unknown_action", "action": action}, status=400)

    async def _handle_list_snapshots(self, request):
        vm_name = request.match_info["name"]
        if vm_name not in self._vm_manager.list_vms():
            return self._json({"error": "vm_not_found"}, status=404)
        config = self._vm_manager.get_vm(vm_name)
        disk_path = config.disk_path if config else ""
        snapshots = []
        if disk_path and os.path.exists(disk_path):
            qemu_img = os.path.join(os.path.dirname(DEFAULT_QEMU_BINARY), 'qemu-img.exe')
            try:
                result = subprocess.run([qemu_img, 'snapshot', '-l', disk_path], capture_output=True, text=True, timeout=10, creationflags=CREATE_NO_WINDOW)
                for line in result.stdout.strip().split('\n'):
                    parts = line.split()
                    if len(parts) >= 2:
                        snapshots.append({"name": parts[1], "vm_name": vm_name, "created": " ".join(parts[2:]) if len(parts) > 2 else "", "size_bytes": 0})
            except:
                pass
        return self._json(snapshots)

    async def _handle_create_snapshot(self, request):
        vm_name = request.match_info["name"]
        try:
            body = await request.json()
        except:
            body = {}
        snap_name = body.get("name", "").strip() or f"snapshot-{int(time.time())}"
        if vm_name not in self._vm_manager.list_vms():
            return self._json({"error": "vm_not_found"}, status=404)
        config = self._vm_manager.get_vm(vm_name)
        disk_path = config.disk_path if config else ""
        if disk_path and os.path.exists(disk_path):
            qemu_img = os.path.join(os.path.dirname(DEFAULT_QEMU_BINARY), 'qemu-img.exe')
            try:
                result = subprocess.run([qemu_img, 'snapshot', '-c', snap_name, disk_path], capture_output=True, text=True, timeout=10, creationflags=CREATE_NO_WINDOW)
                if result.returncode != 0:
                    return self._json({"error": result.stderr}, status=500)
            except Exception as e:
                return self._json({"error": str(e)}, status=500)
        return self._json({"vm": vm_name, "action": "create_snapshot", "status": "ok", "detail": f"Snapshot '{snap_name}' created"})

    async def _handle_restore_snapshot(self, request):
        vm_name = request.match_info["name"]
        snap_name = request.match_info.get("snapshot_name", "")
        if not snap_name:
            return self._json({"error": "missing_snapshot_name"}, status=400)
        if vm_name not in self._vm_manager.list_vms():
            return self._json({"error": "vm_not_found"}, status=404)
        config = self._vm_manager.get_vm(vm_name)
        disk_path = config.disk_path if config else ""
        if disk_path and os.path.exists(disk_path):
            qemu_img = os.path.join(os.path.dirname(DEFAULT_QEMU_BINARY), 'qemu-img.exe')
            try:
                result = subprocess.run([qemu_img, 'snapshot', '-a', snap_name, disk_path], capture_output=True, text=True, timeout=10, creationflags=CREATE_NO_WINDOW)
                if result.returncode != 0:
                    return self._json({"error": result.stderr}, status=500)
            except Exception as e:
                return self._json({"error": str(e)}, status=500)
        return self._json({"vm": vm_name, "action": "restore_snapshot", "status": "ok", "detail": f"Snapshot '{snap_name}' restored"})

    async def _handle_qmp_command(self, request):
        vm_name = request.match_info["name"]
        try:
            body = await request.json()
        except:
            return self._json({"error": "invalid_json"}, status=400)
        command = body.get("command", "").strip()
        if not command:
            return self._json({"error": "missing_command"}, status=400)
        if vm_name not in self._vm_manager.list_vms():
            return self._json({"error": "vm_not_found"}, status=404)
        bridge = self._multi_bridge.get_bridge(vm_name)
        if bridge and bridge.is_connected:
            bridge.send_command(command)
            return self._json({"vm": vm_name, "command": command, "output": "Command sent (async)", "return_code": 0})
        return self._json({"vm": vm_name, "command": command, "output": "VM not connected via QMP", "return_code": -1})

    async def _handle_dashboard(self, request):
        vms = []
        for vm_name in self._vm_manager.list_vms():
            summary = self._vm_manager.get_summary(vm_name)
            if summary:
                vms.append(summary.to_dict())
        return self._json({
            "vms": vms,
            "metrics": {},
            "tailscale_ip": (self._tailscale_info or {}).get("ip", ""),
            "vm_count": len(vms),
            "running_count": len([v for v in vms if v.get("status") == "running"]),
        })

    def _json(self, data, status=200):
        return web.json_response(data, status=status)


def ensure_test_vm(manager: MultiVMManager) -> None:
    if manager.list_vms():
        return
    qemu_bin = DEFAULT_QEMU_BINARY
    if not os.path.exists(qemu_bin):
        import glob
        hits = glob.glob(r'C:\**\qemu-system-x86_64.exe', recursive=True)
        if hits:
            qemu_bin = hits[0]
        else:
            print("QEMU binary not found, skipping test VM creation")
            return
    disk_path = os.path.expanduser('~/.qemu-mcp/test-vm.qcow2')
    os.makedirs(os.path.dirname(disk_path), exist_ok=True)
    if not os.path.exists(disk_path):
        qemu_img = os.path.join(os.path.dirname(qemu_bin), 'qemu-img.exe')
        subprocess.run([qemu_img, 'create', '-f', 'qcow2', disk_path, '1G'], capture_output=True, creationflags=CREATE_NO_WINDOW)
        print(f"Created test disk: {disk_path}")
    ok, msg = manager.add_vm('test-vm', {'qemu_binary': qemu_bin, 'disk_path': disk_path, 'ram_mb': 512, 'cpus': 1, 'display': 'none'})
    print(f"Test VM: {ok} {msg}")


def main():
    parser = argparse.ArgumentParser(description="VM-Harness Headless Server")
    parser.add_argument(
        "--no-tls",
        action="store_true",
        help="Disable TLS (plain HTTP) — for development only",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8443,
        help="Port to listen on (default: 8443)",
    )
    args = parser.parse_args()

    # Set up TLS context
    ssl_ctx = None
    if not args.no_tls:
        try:
            cert_path, key_path = ensure_tls_certs()
            ssl_ctx = create_ssl_context(cert_path, key_path)
            print(f"  TLS enabled  (cert: {cert_path.name}, key: {key_path.name})")
        except Exception as e:
            print(f"  WARNING: Failed to set up TLS: {e}")
            print(f"  Falling back to plain HTTP (use --no-tls to suppress this warning)")
            ssl_ctx = None
    else:
        print("  TLS disabled (--no-tls) — serving plain HTTP")

    vm_manager = MultiVMManager()
    ensure_test_vm(vm_manager)
    multi_bridge = MultiVMQMPBridge(mode="switch")
    for vm_name in vm_manager.list_vms():
        qmp_uri = vm_manager.get_qmp_uri(vm_name)
        if qmp_uri:
            multi_bridge.add_vm(vm_name, qmp_uri)

    server = HeadlessServer(
        vm_manager=vm_manager,
        multi_bridge=multi_bridge,
        host="0.0.0.0",
        port=args.port,
        tailscale_only=False,
        signing_key_dir=PROJECT_ROOT,
    )
    server._ssl_context = ssl_ctx  # used by run()

    scheme = "https" if ssl_ctx else "http"
    print("=" * 60)
    print("  VM-Harness Headless Server")
    print("=" * 60)
    print(f"  Listening:   {scheme}://0.0.0.0:{args.port}")
    print(f"  VMs configured: {vm_manager.list_vms()}")
    for vm in vm_manager.list_vms():
        s = vm_manager.get_summary(vm)
        print(f"    - {vm}: {s.status if s else 'unknown'}")
    print("=" * 60)
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
