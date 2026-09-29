"""
VM-Harness Desktop API Server — local HTTP + WebSocket server for remote Android client access.

Runs INSIDE the existing VM-Harness process, sharing the same QMPBridge, SSHBridge,
CredentialStore, AuditLogger, Settings, Providers, and ChatEngine. No duplication.

Bound to the Tailscale interface IP (100.x.y.z) with source-IP filtering on
100.0.0.0/8 — no LAN or localhost exposure. All traffic is encrypted by Tailscale's
WireGuard tunnels; the API uses plain HTTP inside the tunnel (TLS is optional §10.6).

Requires: aiohttp (already a dependency in pyproject.toml), qrcode (installed separately).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import platform
import struct
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine, Mapping, List, Dict

import aiohttp
from aiohttp import web

from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature

# ── logging ──────────────────────────────────────────────────────────────────

log = logging.getLogger("vmharness.api")

# ── constants ─────────────────────────────────────────────────────────────────

TAILSCALE_IPV4_PREFIX = (100, 0, 0, 0)
TAILSCALE_IPV4_MASK = 8  # 100.0.0.0/8
API_KEY_HEADER = "X-API-Key"
PAIRING_TOKEN_VERSION = "v1"
PAIRING_PURPOSE = "mobile-pairing"
PAIRING_TOKEN_TTL_SECONDS = 86400  # 24h for QR codes; manual entry can be perpetual
API_SERVER_PORT = 8443


# ── Tailscale detection ───────────────────────────────────────────────────────

def _tailscale_ipv4_int() -> int | None:
    """Return the desktop's Tailscale IPv4 as an integer, or None."""
    try:
        import psutil
    except ImportError:
        return None
    for nic, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family == getattr(os, "AF_INET", 2) or addr.family == 2:  # AF_INET
                parts = addr.address.split(".")
                if len(parts) == 4:
                    first = int(parts[0])
                    if first == TAILSCALE_IPV4_PREFIX[0]:
                        b = bytes(int(p) for p in parts)
                        return struct.unpack("!I", b)[0]
    return None


def get_tailscale_info() -> dict[str, Any] | None:
    """Detect Tailscale state: IP, MagicDNS suffix, tailnet name.

    Returns None if Tailscale is not running or the CLI is unavailable.
    """
    info: dict[str, Any] = {"running": False}

    tailscale_ip_int = _tailscale_ipv4_int()
    if tailscale_ip_int is not None:
        parts = [
            str((tailscale_ip_int >> (8 * i)) & 0xFF)
            for i in range(3, -1, -1)
        ]
        info["ip"] = ".".join(parts)
        info["running"] = True

    # Try the tailscale CLI for MagicDNS + tailnet (best effort)
    try:
        import subprocess

        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            if isinstance(data, dict):
                ips = data.get("TailscaleIPs") or []
                if ips and not info.get("ip"):
                    info["ip"] = ips[0]
                    info["running"] = True
                info["magic_dns"] = data.get("MagicDNSSuffix") or ""
                info["tailnet"] = data.get("Tailnet") or ""
                info["hostname"] = data.get("Hostname") or platform.node()
    except (ImportError, FileNotFoundError, subprocess.TimeoutExpired,
            json.JSONDecodeError, OSError):
        pass  # CLI not available — use psutil-only detection above

    if not info.get("running"):
        return None
    return info


# ── machine fingerprint ───────────────────────────────────────────────────────

_MACHINE_ID_CACHE: Path | None = None


def compute_machine_id(store_dir: Path) -> str:
    """Stable, privacy-hashed machine fingerprint.

    Combines hostname + MAC + platform, hashes with SHA-256, returns
    'sha256:<32-hex-chars>'. Cached on disk so it survives restarts.
    """
    global _MACHINE_ID_CACHE
    _MACHINE_ID_CACHE = store_dir / ".vmharness_machine_id"
    if _MACHINE_ID_CACHE.exists():
        return _MACHINE_ID_CACHE.read_text().strip()

    parts = [
        platform.node(),            # hostname
        str(uuid.getnode()),        # MAC (stable per NIC)
        platform.machine(),         # e.g. 'AMD64'
        platform.system(),          # e.g. 'Windows'
    ]
    raw = "|".join(parts).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:32]
    result = f"sha256:{digest}"
    try:
        _MACHINE_ID_CACHE.write_text(result)
    except OSError:
        pass  # best effort cache
    return result


# ── Ed25519 signing key pair (persisted in credential store dir) ──────────────

_SIGNING_KEY_FILE = ".vmharness_signing_key"


def _load_or_generate_signing_key(key_dir: Path) -> ed25519.Ed25519PrivateKey:
    """Load the persisted Ed25519 signing key, or generate + persist a new one."""
    if isinstance(key_dir, str):
        key_dir = Path(key_dir)
    key_file = key_dir / _SIGNING_KEY_FILE
    if key_file.exists():
        try:
            raw = key_file.read_bytes()
            private_key = ed25519.Ed25519PrivateKey.from_private_bytes(raw)
            # verify it's usable
            private_key.public_key()
            return private_key
        except (ValueError, OSError):
            pass  # corrupt — regenerate

    private_key = ed25519.Ed25519PrivateKey.generate()
    try:
        key_file.write_bytes(private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        key_file.chmod(0o600)  # owner-read-only
    except OSError:
        pass  # best effort — key still works in memory
    return private_key


def get_signing_public_key_bytes(key_dir: Path) -> bytes:
    """Return the Ed25519 public key as raw bytes (for baking into the Android app)."""
    private_key = _load_or_generate_signing_key(key_dir)
    pub = private_key.public_key()
    return pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


# ── pairing token ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PairingPayload:
    """Content of a mobile pairing token — signed by the desktop's Ed25519 key."""
    version: str = PAIRING_TOKEN_VERSION
    purpose: str = PAIRING_PURPOSE
    host: str = ""            # MagicDNS name, e.g. 'omnarchy-vm.tail123.ts.net'
    ip: str = ""              # Tailscale IPv4, e.g. '100.123.45.67'
    tailnet: str = ""         # tailnet name
    machine_id: str = ""     # 'sha256:abc...'
    display_name: str = ""   # human-readable desktop name
    created: int = 0         # Unix timestamp
    expires: int = 0         # Unix timestamp (0 = perpetual)
    secret: str = ""         # random 256-bit base64url secret (the API key)


def generate_pairing_token(
    signing_key: ed25519.Ed25519PrivateKey,
    host: str,
    ip: str,
    tailnet: str,
    machine_id: str,
    display_name: str = "",
    ttl_seconds: int = PAIRING_TOKEN_TTL_SECONDS,
) -> tuple[str, PairingPayload]:
    """Generate a signed pairing token. Returns (token_string, payload)."""
    now = int(time.time())
    secret = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")

    payload = PairingPayload(
        host=host,
        ip=ip,
        tailnet=tailnet,
        machine_id=machine_id,
        display_name=display_name or host,
        created=now,
        expires=now + ttl_seconds if ttl_seconds > 0 else 0,
        secret=secret,
    )
    payload_json = json.dumps(
        {
            "v": payload.version,
            "p": payload.purpose,
            "h": payload.host,
            "i": payload.ip,
            "t": payload.tailnet,
            "m": payload.machine_id,
            "n": payload.display_name,
            "c": payload.created,
            "e": payload.expires,
            "s": payload.secret,
        },
        separators=(",", ":"),
    )
    payload_bytes = payload_json.encode("utf-8")
    signature = signing_key.sign(payload_bytes)
    sig_b64 = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    pay_b64 = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
    token = f"{sig_b64}.{pay_b64}"
    return token, payload


def verify_pairing_token(
    token: str,
    public_key: ed25519.Ed25519PublicKey,
) -> PairingPayload | None:
    """Verify a pairing token's signature and validity. Returns payload or None."""
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        sig = base64.urlsafe_b64decode(parts[0] + "==")
        pay = base64.urlsafe_b64decode(parts[1] + "==")
    except (ValueError, IndexError):
        return None

    try:
        public_key.verify(sig, pay)
    except InvalidSignature:
        return None

    try:
        raw = json.loads(pay.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    # Reconstruct PairingPayload from compact JSON
    now = int(time.time())
    payload = PairingPayload(
        version=raw.get("v", ""),
        purpose=raw.get("p", ""),
        host=raw.get("h", ""),
        ip=raw.get("i", ""),
        tailnet=raw.get("t", ""),
        machine_id=raw.get("m", ""),
        display_name=raw.get("n", ""),
        created=raw.get("c", 0),
        expires=raw.get("e", 0),
        secret=raw.get("s", ""),
    )

    # Validate
    if payload.version != PAIRING_TOKEN_VERSION:
        return None
    if payload.purpose != PAIRING_PURPOSE:
        return None
    if not payload.host or "." not in payload.host:
        return None
    if not payload.secret:
        return None
    # Tailscale IP range
    try:
        ip_parts = [int(x) for x in payload.ip.split(".")]
        if len(ip_parts) != 4 or ip_parts[0] != TAILSCALE_IPV4_PREFIX[0]:
            return None
    except (ValueError, AttributeError):
        return None
    # Expiry
    if payload.expires > 0 and payload.expires < now:
        return None
    # Not created in the future
    if payload.created > now + 60:
        return None

    return payload


# ── API key storage (in-memory registry, backed by credential_store on desktop) ──

class APIKeyRegistry:
    """Tracks authorized API keys and their metadata.

    On the desktop, this wraps the CredentialStore — keys are stored as
    Fernet-encrypted credentials. For the standalone API server, an in-memory
    dict is used (paired with the desktop's credential store at integration time).
    """

    def __init__(self) -> None:
        self._keys: dict[str, dict[str, Any]] = {}  # secret_hash -> metadata
        self._by_id: dict[str, str] = {}            # key_id -> secret_hash

    def _hash(self, secret: str) -> str:
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    def register(self, payload: PairingPayload, source_ip: str = "") -> str:
        """Register a pairing payload's secret as an authorized API key.
        Returns a key_id for audit/logging.
        """
        secret_hash = self._hash(payload.secret)
        key_id = str(uuid.uuid4())
        self._keys[secret_hash] = {
            "key_id": key_id,
            "secret": payload.secret,  # stored — in production, Fernet-encrypt
            "host": payload.host,
            "ip": payload.ip,
            "tailnet": payload.tailnet,
            "machine_id": payload.machine_id,
            "display_name": payload.display_name,
            "created": payload.created,
            "source_ip": source_ip,
        }
        self._by_id[key_id] = secret_hash
        return key_id

    def authenticate(self, secret: str) -> dict[str, Any] | None:
        """Return metadata for a valid API key, or None."""
        hash_val = self._hash(secret)
        meta = self._keys.get(hash_val)
        if meta is None:
            return None
        return dict(meta)  # copy

    def revoke(self, key_id: str) -> bool:
        """Revoke a key by its key_id. Returns True if found + revoked."""
        secret_hash = self._by_id.pop(key_id, None)
        if secret_hash is None:
            return False
        self._keys.pop(secret_hash, None)
        return True

    def revoke_by_machine_id(self, machine_id: str) -> list[str]:
        """Revoke all keys for a given machine_id. Returns list of revoked key_ids."""
        revoked: list[str] = []
        to_remove: list[str] = []
        for secret_hash, meta in self._keys.items():
            if meta.get("machine_id") == machine_id:
                to_remove.append(secret_hash)
        for secret_hash in to_remove:
            key_id = self._by_id.get(secret_hash)
            if key_id:
                revoked.append(key_id)
            self._keys.pop(secret_hash, None)
            self._by_id.pop(secret_hash, None)
        return revoked

    def list_keys(self) -> list[dict[str, Any]]:
        """Return metadata for all registered keys (secrets masked)."""
        result: list[dict[str, Any]] = []
        for meta in self._keys.values():
            masked = dict(meta)
            masked["secret"] = masked["secret"][:4] + "..." + masked["secret"][-4:]
            result.append(masked)
        return result


# ── request context ────────────────────────────────────────────────────────────

@dataclass
class RequestContext:
    """Extracted request context for an API call."""
    api_key: str
    api_key_meta: dict[str, Any] | None
    source_ip: str
    vm_name: str | None  # extracted from path if applicable


# ── audit hook ────────────────────────────────────────────────────────────────

AuditLoggerCompatible = Callable[[str, str, str, str, str, str, str], Coroutine[Any, Any, None]]
# Signature: (event, details_json, source_ip, user, vm_name, status, error)


# ── API server ────────────────────────────────────────────────────────────────

class QMCMApiServer:
    """Local HTTP + WebSocket API server for the VM-Harness Android companion app.

    Integrate by calling `run()` after your bridges are initialized, passing
    references to the live objects. The server runs until cancelled.
    """

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = API_SERVER_PORT,
        tailscale_only: bool = True,
        signing_key_dir: Path | None = None,
        credential_store_dir: Path | None = None,
        audit_logger: AuditLoggerCompatible | None = None,
        # Live object references (set after init via connect_* methods)
        qmp_bridge: Any = None,
        ssh_bridge: Any = None,
        multi_vm_bridge: Any = None,
        credential_store: Any = None,
        settings: Any = None,
        providers: Any = None,
        chat_engine: Any = None,
        vm_cloner: Any = None,
        snapshot_scheduler: Any = None,
        iso_manager: Any = None,
        metrics_store: Any = None,
        process_guardian: Any = None,
    ) -> None:
        self._host = host
        self._port = port
        self._tailscale_only = tailscale_only
        self._signing_key_dir = signing_key_dir or Path.cwd()
        self._credential_store_dir = credential_store_dir or Path.cwd()
        self._audit_logger = audit_logger

        # Ed25519 signing key (loaded/persisted in signing_key_dir)
        self._signing_key = _load_or_generate_signing_key(self._signing_key_dir)
        self._public_key = self._signing_key.public_key()

        # API key registry
        self._api_keys = APIKeyRegistry()

        # Live objects — set by the integrator after init
        self.qmp_bridge = qmp_bridge
        self.ssh_bridge = ssh_bridge
        self.multi_vm_bridge = multi_vm_bridge
        self.credential_store = credential_store
        self.settings = settings
        self.providers = providers
        self.chat_engine = chat_engine
        self.vm_cloner = vm_cloner
        self.snapshot_scheduler = snapshot_scheduler
        self.iso_manager = iso_manager
        self.metrics_store = metrics_store
        self.process_guardian = process_guardian

        # aiohttp app — built lazily in run()
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._tailscale_info: dict[str, Any] | None = None

    # ── property helpers ──────────────────────────────────────────────────────

    def _require(self, obj: Any, name: str) -> Any:
        if obj is None:
            raise RuntimeError(f"API server not connected to {name} — call connect_{name}() first")
        return obj

    # ── connect_* (call after init, before run) ───────────────────────────────

    def connect_qmp_bridge(self, bridge: Any) -> None:
        self.qmp_bridge = bridge

    def connect_ssh_bridge(self, bridge: Any) -> None:
        self.ssh_bridge = bridge

    def connect_multi_vm_bridge(self, bridge: Any) -> None:
        self.multi_vm_bridge = bridge

    def connect_credential_store(self, store: Any) -> None:
        self.credential_store = store

    def connect_settings(self, settings: Any) -> None:
        self.settings = settings

    def connect_providers(self, providers: Any) -> None:
        self.providers = providers

    def connect_chat_engine(self, engine: Any) -> None:
        self.chat_engine = engine

    def connect_vm_cloner(self, cloner: Any) -> None:
        self.vm_cloner = cloner

    def connect_snapshot_scheduler(self, scheduler: Any) -> None:
        self.snapshot_scheduler = scheduler

    def connect_iso_manager(self, manager: Any) -> None:
        self.iso_manager = manager

    def connect_metrics_store(self, store: Any) -> None:
        self.metrics_store = store

    def connect_process_guardian(self, guardian: Any) -> None:
        self.process_guardian = guardian

    # ── pairing ───────────────────────────────────────────────────────────────

    def generate_pairing_token(
        self,
        host: str | None = None,
        ip: str | None = None,
        ttl_seconds: int = PAIRING_TOKEN_TTL_SECONDS,
        display_name: str = "",
    ) -> tuple[str, PairingPayload, dict[str, Any]]:
        """Generate a new pairing token using current Tailscale info.

        Returns (token_string, payload, tailscale_info).
        Uses detected Tailscale info if host/ip not provided.
        """
        tailscale_info = self._tailscale_info
        if tailscale_info is None:
            tailscale_info = get_tailscale_info()
            self._tailscale_info = tailscale_info
        if tailscale_info is None:
            raise RuntimeError("Tailscale is not running — cannot generate pairing token")

        use_host = host or tailscale_info.get("magic_dns", "") or tailscale_info.get("ip", "")
        use_ip = ip or tailscale_info.get("ip", "")
        use_tailnet = tailscale_info.get("tailnet", "")
        machine_id = compute_machine_id(self._credential_store_dir)
        use_display = display_name or tailscale_info.get("hostname", "") or use_host

        token, payload = generate_pairing_token(
            self._signing_key,
            host=use_host,
            ip=use_ip,
            tailnet=use_tailnet,
            machine_id=machine_id,
            display_name=use_display,
            ttl_seconds=ttl_seconds,
        )
        return token, payload, tailscale_info

    def verify_token(self, token: str) -> PairingPayload | None:
        """Verify a pairing token (standalone — no network involved)."""
        return verify_pairing_token(token, self._public_key)

    def register_pairing(self, payload: PairingPayload, source_ip: str = "") -> str:
        """Register a verified pairing payload as an authorized API key."""
        return self._api_keys.register(payload, source_ip)

    def authenticate_api_key(self, secret: str) -> dict[str, Any] | None:
        """Check an API key and return its metadata."""
        return self._api_keys.authenticate(secret)

    def revoke_key(self, key_id: str) -> bool:
        return self._api_keys.revoke(key_id)

    def revoke_by_machine_id(self, machine_id: str) -> list[str]:
        return self._api_keys.revoke_by_machine_id(machine_id)

    def list_api_keys(self) -> list[dict[str, Any]]:
        return self._api_keys.list_keys()

    def get_public_key_bytes(self) -> bytes:
        """Return the Ed25519 public key (raw bytes) — for baking into the Android app."""
        return get_signing_public_key_bytes(self._signing_key_dir)

    def get_public_key_pem(self) -> str:
        """Return the Ed25519 public key as PEM (for download / display)."""
        pub = self._public_key
        return pub.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")

    # ── IP filtering ──────────────────────────────────────────────────────────

    def _is_tailscale_ip(self, ip: str) -> bool:
        """Check if an IP is in the Tailscale 100.0.0.0/8 range."""
        try:
            parts = ip.split(".")
            if len(parts) != 4:
                return False
            first = int(parts[0])
            return first == TAILSCALE_IPV4_PREFIX[0]
        except (ValueError, AttributeError):
            return False

    async def _get_client_ip(self, request: web.Request) -> str:
        """Extract the client IP from a request, preferring X-Forwarded-For."""
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        peername = request.transport.get_extra_info("peername")
        if peername:
            return peername[0]
        return "unknown"

    async def _require_tailscale_or_auth(
        self, request: web.Request
    ) -> RequestContext:
        """Middleware-style: check Tailscale IP or valid API key.

        For pairing endpoint, allow any source (the token itself is the auth).
        For all other endpoints, require a valid API key AND a Tailscale source IP
        (when tailscale_only is True).
        """
        client_ip = await self._get_client_ip(request)
        api_key = request.headers.get(API_KEY_HEADER, "")

        # Pairing endpoint is open (token auth happens inside)
        if request.path.startswith("/api/v1/auth/pair"):
            return RequestContext(api_key="", api_key_meta=None, source_ip=client_ip, vm_name=None)

        # Verify API key
        meta = self._api_keys.authenticate(api_key) if api_key else None

        if self._tailscale_only and not self._is_tailscale_ip(client_ip):
            if meta is None:
                raise web.HTTPForbidden(
                    text=json.dumps({
                        "error": "forbidden",
                        "detail": "Requests must come from a Tailscale IP (100.0.0.0/8) "
                                 "or include a valid X-API-Key header.",
                    }),
                    content_type="application/json",
                )

        # Extract vm_name from path for audit purposes
        vm_name: str | None = None
        parts = request.path.split("/")
        for i, p in enumerate(parts):
            if p == "vms" and i + 1 < len(parts):
                vm_name = parts[i + 1]
                break

        return RequestContext(
            api_key=api_key,
            api_key_meta=meta,
            source_ip=client_ip,
            vm_name=vm_name,
        )

    async def _audit(self, ctx: RequestContext, event: str, details: str,
                     status: str = "ok", error: str = "") -> None:
        """Log an API call via the audit logger if connected."""
        if self._audit_logger is None:
            return
        try:
            await self._audit_logger(
                event=event,
                details_json=details,
                source_ip=ctx.source_ip,
                user="api",
                vm_name=ctx.vm_name or "",
                status=status,
                error=error,
            )
        except Exception:
            log.exception("API audit logging failed")

    # ── route handlers ────────────────────────────────────────────────────────

    # -- auth --

    async def _handle_pair(self, request: web.Request) -> web.Response:
        """POST /api/v1/auth/pair — accept a pairing token, register the API key."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, aiohttp.ContentTypeError):
            body = {}
        token = (body.get("token") or body.get("key") or "").strip()
        if not token:
            raise web.HTTPBadRequest(text=json.dumps({"error": "missing_token"}))

        payload = self.verify_token(token)
        if payload is None:
            raise web.HTTPForbidden(text=json.dumps({"error": "invalid_token"}))

        client_ip = await self._get_client_ip(request)
        key_id = self.register_pairing(payload, client_ip)

        await self._audit(
            RequestContext(api_key="", api_key_meta=None, source_ip=client_ip, vm_name=None),
            event="pairing_success",
            details=json.dumps({
                "host": payload.host,
                "machine_id": payload.machine_id,
                "key_id": key_id,
            }),
        )

        return web.json_response({
            "paired": True,
            "key_id": key_id,
            "host": payload.host,
            "ip": payload.ip,
            "tailnet": payload.tailnet,
            "machine_id": payload.machine_id,
            "display_name": payload.display_name,
            "created": payload.created,
            "expires": payload.expires,
        })

    async def _handle_verify(self, request: web.Request) -> web.Response:
        """GET /api/v1/auth/verify — check the current API key is valid."""
        ctx = await self._require_tailscale_or_auth(request)
        if ctx.api_key_meta is None:
            raise web.HTTPUnauthorized(text=json.dumps({"error": "invalid_api_key"}))

        tailscale_info = self._tailscale_info or get_tailscale_info() or {}
        return web.json_response({
            "valid": True,
            "desktop_version": getattr(self.settings, "version", "0.1.0") if self.settings else "0.1.0",
            "tailscale_ip": tailscale_info.get("ip", ""),
            "machine_id": compute_machine_id(self._credential_store_dir),
            "display_name": ctx.api_key_meta.get("display_name", ""),
        })

    async def _handle_revoke(self, request: web.Request) -> web.Response:
        """POST /api/v1/auth/revoke — revoke the current API key (called from Android)."""
        ctx = await self._require_tailscale_or_auth(request)
        if ctx.api_key_meta is None:
            raise web.HTTPUnauthorized(text=json.dumps({"error": "invalid_api_key"}))
        # Revoke by key_id
        key_id = ctx.api_key_meta.get("key_id", "")
        revoked = self._api_keys.revoke(key_id)
        if not revoked:
            raise web.HTTPNotFound(text=json.dumps({"error": "key_not_found"}))
        await self._audit(ctx, "auth_revoke", json.dumps({"key_id": key_id}), "ok")
        return web.json_response({"revoked": True, "key_id": key_id})

    async def _handle_public_key(self, request: web.Request) -> web.Response:
        """GET /api/v1/auth/public-key — download the desktop's Ed25519 public key (PEM)."""
        return web.Response(
            text=self.get_public_key_pem(),
            content_type="application/x-pem-file",
            headers={"Content-Disposition": "attachment; filename=vmharness_public_key.pem"},
        )

    # -- dashboard + VMs --

    async def _handle_dashboard(self, request: web.Request) -> web.Response:
        """GET /api/v1/ — dashboard summary."""
        ctx = await self._require_tailscale_or_auth(request)

        # Gather info from connected bridges
        vms: list[dict[str, Any]] = []
        try:
            qmp = self.qmp_bridge
            if qmp is not None and hasattr(qmp, "get_status"):
                status = qmp.get_status()
                vms.append({"name": "default", "status": status})
        except Exception as e:
            log.debug("Dashboard VM list fallback: %s", e)

        # Host metrics if metrics_store connected
        metrics: dict[str, Any] = {}
        if self.metrics_store is not None and hasattr(self.metrics_store, "get_summary"):
            try:
                metrics = self.metrics_store.get_summary()
            except Exception:
                pass

        await self._audit(ctx, "dashboard_view", json.dumps({}), "ok")
        return web.json_response({
            "vms": vms,
            "metrics": metrics,
            "tailscale_ip": (self._tailscale_info or {}).get("ip", ""),
            "machine_id": compute_machine_id(self._credential_store_dir),
        })

    # -- the hub: every VM on every hypervisor ------------------------------------------------------------------
    async def _hub_call(self, op: str, args: dict | None = None, timeout: float = 300.0) -> Any:
        """Run a VM-Harness operation through the hub (started if needed), off the event loop."""
        from vm_harness.control.client import HubClient

        def go() -> Any:
            return HubClient.connect(start=True, client_name="phone").call(op, dict(args or {}), timeout=timeout)
        return await asyncio.to_thread(go)

    @staticmethod
    def _hub_error(exc: Exception) -> web.HTTPException:
        code = getattr(exc, "code", "") or ""
        body = json.dumps({"error": str(exc), "code": code})
        if code in ("not_found", "vm_not_found") or "not found" in str(exc).lower():
            return web.HTTPNotFound(text=body, content_type="application/json")
        if code in ("bad_arguments", "invalid_arguments"):
            return web.HTTPBadRequest(text=body, content_type="application/json")
        return web.HTTPBadGateway(text=body, content_type="application/json")

    @staticmethod
    def _vm_summary(v: dict) -> dict:
        st = v.get("status") or {}
        return {
            "name": v.get("name", ""), "status": v.get("state") or st.get("state", "unknown"), "backend": v.get("backend", ""),
            "qmpUri": st.get("management_uri", ""), "sshUri": "", "ram": int(st.get("ram_allocated_mb") or 0),
            "vcpus": int(st.get("cpus_allocated") or 0), "disk": int(st.get("disk_allocated_gb") or 0),
            "uptime": f"{int(st.get('uptime_seconds') or 0)}s", "lastStarted": st.get("started_at", ""),
            "cpuPercent": st.get("cpu_usage_pct", 0), "ramUsedMb": st.get("ram_usage_mb", 0),
        }

    # -- snapshots (through the hub) --------------------------------------------------------------------------------
    async def _handle_snapshots_list(self, request: web.Request) -> web.Response:
        """GET /api/v1/vms/{name}/snapshots"""
        ctx = await self._require_tailscale_or_auth(request)
        vm_name = request.match_info["name"]
        try:
            snaps = await self._hub_call("vm.snapshot.list", {"name": vm_name})
        except Exception as e:  # noqa: BLE001
            raise self._hub_error(e) from e
        await self._audit(ctx, "snapshot_list", json.dumps({"vm": vm_name}), "ok")
        return web.json_response([{"name": x.get("name", ""), "vmName": vm_name, "created": x.get("created_at", ""),
                                   "sizeBytes": int(x.get("size_bytes") or 0), "current": bool(x.get("is_current"))}
                                  for x in snaps or []])

    async def _handle_snapshot_create(self, request: web.Request) -> web.Response:
        """POST /api/v1/vms/{name}/snapshots  {name, description?}"""
        ctx = await self._require_tailscale_or_auth(request)
        vm_name = request.match_info["name"]
        try:
            body = await request.json()
        except (json.JSONDecodeError, aiohttp.ContentTypeError):
            body = {}
        snap = str(body.get("name", "")).strip() or f"snapshot-{int(time.time())}"
        try:
            await self._hub_call("vm.snapshot.create", {"name": vm_name, "snapshot_name": snap,
                                                        "description": str(body.get("description", ""))})
        except Exception as e:  # noqa: BLE001
            await self._audit(ctx, "snapshot_create", json.dumps({"vm": vm_name, "snapshot": snap}), "error", str(e))
            raise self._hub_error(e) from e
        await self._audit(ctx, "snapshot_create", json.dumps({"vm": vm_name, "snapshot": snap}), "ok")
        return web.json_response({"vm": vm_name, "action": "create_snapshot", "status": "ok", "detail": f"Snapshot '{snap}' created"})

    async def _handle_snapshot_restore(self, request: web.Request) -> web.Response:
        """POST /api/v1/vms/{name}/snapshots/{snapshot_name}/restore"""
        ctx = await self._require_tailscale_or_auth(request)
        vm_name = request.match_info["name"]
        snap = request.match_info["snapshot_name"]
        try:
            await self._hub_call("vm.snapshot.restore", {"name": vm_name, "snapshot_name": snap})
        except Exception as e:  # noqa: BLE001
            await self._audit(ctx, "snapshot_restore", json.dumps({"vm": vm_name, "snapshot": snap}), "error", str(e))
            raise self._hub_error(e) from e
        await self._audit(ctx, "snapshot_restore", json.dumps({"vm": vm_name, "snapshot": snap}), "ok")
        return web.json_response({"vm": vm_name, "action": "restore_snapshot", "status": "ok", "detail": f"Snapshot '{snap}' restored"})

    async def _handle_vms_list(self, request: web.Request) -> web.Response:
        """GET /api/v1/vms: every VM on every hypervisor the hub reaches."""
        ctx = await self._require_tailscale_or_auth(request)
        try:
            vms = [self._vm_summary(v) for v in await self._hub_call("vm.list", {})]
        except Exception as e:  # noqa: BLE001 - the hub is not reachable: fall back to the window's own bridge
            log.warning("vm.list through the hub failed (%s); using the desktop bridge", e)
            vms = []
            if self.multi_vm_bridge is not None and hasattr(self.multi_vm_bridge, "list_vms"):
                try:
                    vms = self.multi_vm_bridge.list_vms()
                except Exception as e2:  # noqa: BLE001
                    log.debug("multi_vm_bridge.list_vms failed: %s", e2)
        await self._audit(ctx, "vms_list", json.dumps({}), "ok" if vms else "empty")
        return web.json_response(vms)

    async def _handle_vm_detail(self, request: web.Request) -> web.Response:
        """GET /api/v1/vms/{name}: state, configuration, network interfaces."""
        ctx = await self._require_tailscale_or_auth(request)
        vm_name = request.match_info["name"]
        try:
            status, config = await asyncio.gather(self._hub_call("vm.status", {"name": vm_name}),
                                                  self._hub_call("vm.config", {"name": vm_name}))
        except Exception as e:  # noqa: BLE001
            await self._audit(ctx, "vm_detail", json.dumps({"vm": vm_name}), "error", str(e))
            raise self._hub_error(e) from e
        config = config or {}
        detail = {
            "name": vm_name, "status": status.get("state", "unknown"),
            "config": {k: (v if isinstance(v, str) else json.dumps(v)) for k, v in config.items() if not isinstance(v, (bytes, bytearray))},
            "networkInterfaces": [{k: str(v) for k, v in n.items()} for n in status.get("network_interfaces") or [] if isinstance(n, dict)],
            "blockDevices": [], "qmpUri": status.get("management_uri", ""), "sshUri": "",
            "cpuPercent": status.get("cpu_usage_pct", 0), "ramUsedMb": status.get("ram_usage_mb", 0),
            "ramAllocatedMb": status.get("ram_allocated_mb", 0), "uptimeSeconds": status.get("uptime_seconds", 0),
        }
        await self._audit(ctx, "vm_detail", json.dumps({"vm": vm_name}), "ok")
        return web.json_response(detail)

    # -- VM lifecycle actions --

    async def _handle_vm_action(self, request: web.Request) -> web.Response:
        """POST /api/v1/vms/{name}/{action}: start, stop, powerdown, reset, reboot, pause, resume, eject."""
        ctx = await self._require_tailscale_or_auth(request)
        vm_name = request.match_info["name"]
        action = request.match_info["action"]
        ops = {
            "start": ("vm.start", {"headless": True}), "stop": ("vm.stop", {"force": True}),
            "powerdown": ("vm.stop", {"force": False}), "shutdown": ("vm.shutdown_guest", {}),
            "reset": ("vm.reset", {}), "reboot": ("vm.reboot", {}), "pause": ("vm.pause", {}),
            "resume": ("vm.resume", {}), "eject": ("vm.cdrom.eject", {}),
        }
        if action not in ops:
            raise web.HTTPBadRequest(text=json.dumps({"error": f"unknown_action: {action}", "actions": sorted(ops)}),
                                     content_type="application/json")
        op, extra = ops[action]
        try:
            await self._hub_call(op, {"name": vm_name, **extra})
        except Exception as e:  # noqa: BLE001
            await self._audit(ctx, f"vm_{action}", json.dumps({"vm": vm_name}), "error", str(e))
            raise self._hub_error(e) from e
        await self._audit(ctx, f"vm_{action}", json.dumps({"vm": vm_name}), "ok")
        return web.json_response({"vm": vm_name, "action": action, "status": "ok", "detail": f"{op} done"})

    # -- QMP console (WebSocket) --

    async def _handle_qmp_websocket(self, request: web.Request) -> web.Response:
        """GET /api/v1/vms/{name}/qmp — WebSocket for raw QMP console."""
        ctx = await self._require_tailscale_or_auth(request)
        vm_name = request.match_info["name"]

        ws = web.WebSocketResponse()
        await ws.prepare(request)

        qmp = self._require(self.qmp_bridge, "qmp_bridge")

        # Auth via first message
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "auth":
                    if not self._api_keys.authenticate(data.get("key", "")):
                        await ws.send_json({"type": "error", "data": "unauthorized"})
                        await ws.close()
                        return ws
                    await ws.send_json({"type": "auth_ok"})
                    continue
                if data.get("type") == "command":
                    cmd = data.get("command", "")
                    try:
                        result = qmp.send_command(cmd)
                        await ws.send_json({"type": "response", "data": result})
                    except Exception as e:
                        await ws.send_json({"type": "error", "data": str(e)})
            elif msg.type == aiohttp.WSMsgType.ERROR:
                break

        return ws

    # -- SSH terminal (WebSocket) --

    async def _handle_ssh_terminal(self, request: web.Request) -> web.Response:
        """GET /api/v1/vms/{name}/terminal — WebSocket for SSH guest terminal."""
        ctx = await self._require_tailscale_or_auth(request)
        vm_name = request.match_info["name"]
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        ssh = self._require(self.ssh_bridge, "ssh_bridge")
        authenticated = False

        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if not authenticated:
                    if data.get("type") == "auth":
                        if self._api_keys.authenticate(data.get("key", "")):
                            authenticated = True
                            await ws.send_json({"type": "auth_ok"})
                        else:
                            await ws.send_json({"type": "error", "data": "unauthorized"})
                            await ws.close()
                            return ws
                    continue
                if authenticated:
                    msg_type = data.get("type", "")
                    if msg_type == "stdin":
                        # In the real implementation, route stdin to the SSH session
                        # For now, echo acknowledgment
                        await ws.send_json({"type": "stdout", "data": f"[terminal] received: {data.get('data', '')[:100]}"})
                    elif msg_type == "command":
                        cmd = data.get("command", "")
                        try:
                            result = ssh.run_command(cmd)
                            await ws.send_json({"type": "command_result", "data": result})
                        except Exception as e:
                            await ws.send_json({"type": "error", "data": str(e)})
            elif msg.type == aiohttp.WSMsgType.ERROR:
                break

        return ws

    # -- SSH command (REST) --

    async def _handle_ssh_command(self, request: web.Request) -> web.Response:
        """POST /api/v1/vms/{name}/ssh/command — run a single SSH command."""
        ctx = await self._require_tailscale_or_auth(request)
        vm_name = request.match_info["name"]
        try:
            body = await request.json()
        except (json.JSONDecodeError, aiohttp.ContentTypeError):
            body = {}
        command = (body.get("command") or "").strip()
        if not command:
            raise web.HTTPBadRequest(text=json.dumps({"error": "missing_command"}))

        ssh = self._require(self.ssh_bridge, "ssh_bridge")
        try:
            result = ssh.run_command(command)
            await self._audit(ctx, "ssh_command", json.dumps({"vm": vm_name, "command": command[:200]}),
                              "ok")
            return web.json_response({"vm": vm_name, "command": command, "result": result})
        except Exception as e:
            await self._audit(ctx, "ssh_command", json.dumps({"vm": vm_name, "command": command[:200]}),
                              "error", str(e))
            raise web.HTTPInternalServerError(text=json.dumps({"error": str(e)}))

    # -- metrics --

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        """GET /api/v1/metrics: this host's CPU, memory, disk and network, measured now."""
        ctx = await self._require_tailscale_or_auth(request)

        def measure() -> dict[str, Any]:
            import psutil
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage(os.path.abspath(os.sep))
            net = psutil.net_io_counters()
            gib = 1024 ** 3
            return {"cpuPercent": psutil.cpu_percent(interval=0.2), "ramUsed": round(mem.used / gib, 2),
                    "ramTotal": round(mem.total / gib, 2), "diskUsed": round(disk.used / gib, 1),
                    "diskTotal": round(disk.total / gib, 1), "networkRx": float(net.bytes_recv),
                    "networkTx": float(net.bytes_sent), "timestamp": int(time.time())}
        metrics = await asyncio.to_thread(measure)
        await self._audit(ctx, "metrics_view", json.dumps({}), "ok")
        return web.json_response(metrics)

    # -- settings --

    async def _handle_settings_get(self, request: web.Request) -> web.Response:
        """GET /api/v1/settings — all desktop settings."""
        ctx = await self._require_tailscale_or_auth(request)
        if self.settings is None:
            raise web.HTTPInternalServerError(text=json.dumps({"error": "settings not connected"}))
        try:
            # pydantic BaseSettings — dump as dict
            if hasattr(self.settings, "model_dump"):
                data = self.settings.model_dump(mode="json", exclude={"secrets", "credentials"})
            elif hasattr(self.settings, "dict"):
                data = self.settings.dict(exclude={"secrets", "credentials"})
            else:
                data = {"note": "settings object does not support serialization"}
        except Exception as e:
            data = {"error": str(e)}
        await self._audit(ctx, "settings_view", json.dumps({}), "ok")
        return web.json_response(data)

    async def _handle_settings_put(self, request: web.Request) -> web.Response:
        """PUT /api/v1/settings — update settings (partial)."""
        ctx = await self._require_tailscale_or_auth(request)
        try:
            body = await request.json()
        except (json.JSONDecodeError, aiohttp.ContentTypeError):
            raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_json"}))
        if self.settings is None:
            raise web.HTTPInternalServerError(text=json.dumps({"error": "settings not connected"}))
        # Best-effort patch — adapt to the actual settings object
        try:
            for key, value in body.items():
                if hasattr(self.settings, key):
                    setattr(self.settings, key, value)
            await self._audit(ctx, "settings_update", json.dumps({"keys": list(body.keys())}), "ok")
            return web.json_response({"updated": list(body.keys())})
        except Exception as e:
            await self._audit(ctx, "settings_update", json.dumps({"error": str(e)}), "error", str(e))
            raise web.HTTPInternalServerError(text=json.dumps({"error": str(e)}))

    # -- credentials --

    async def _handle_credentials_list(self, request: web.Request) -> web.Response:
        """GET /api/v1/credentials — list credentials (no secrets)."""
        ctx = await self._require_tailscale_or_auth(request)
        cs = self._require(self.credential_store, "credential_store")
        try:
            creds = cs.list_all()
            result = [
                {
                    "name": c.name,
                    "type": c.type,
                    "description": c.description or "",
                    "created": c.created.isoformat() if hasattr(c, "created") else "",
                    "last_used": c.last_used.isoformat() if hasattr(c, "last_used") else "",
                }
                for c in (creds if isinstance(creds, list) else [])
            ]
        except Exception as e:
            result = [{"error": str(e)}]
        await self._audit(ctx, "credentials_list", json.dumps({}), "ok")
        return web.json_response(result)

    async def _handle_credential_detail(self, request: web.Request) -> web.Response:
        """GET /api/v1/credentials/{name} — credential detail (value decrypted)."""
        ctx = await self._require_tailscale_or_auth(request)
        name = request.match_info["name"]
        cs = self._require(self.credential_store, "credential_store")
        try:
            cred = cs.get(name)
            if cred is None:
                raise web.HTTPNotFound(text=json.dumps({"error": "credential_not_found"}))
            result = {
                "name": cred.name,
                "type": cred.type,
                "description": cred.description or "",
                "created": cred.created.isoformat() if hasattr(cred, "created") else "",
                "last_used": cred.last_used.isoformat() if hasattr(cred, "last_used") else "",
                "value": cred.value,  # decrypted — only returned on explicit request
            }
        except Exception as e:
            raise web.HTTPInternalServerError(text=json.dumps({"error": str(e)}))
        await self._audit(ctx, "credential_view", json.dumps({"credential": name}), "ok")
        return web.json_response(result)

    # -- audit log --

    async def _handle_audit(self, request: web.Request) -> web.Response:
        """GET /api/v1/security/audit?limit=: what was changed, by whom (VM-Harness's hash-chained audit log)."""
        ctx = await self._require_tailscale_or_auth(request)
        try:
            limit = max(1, min(1000, int(request.query.get("limit", "200"))))
        except ValueError:
            limit = 200
        try:
            rows = await self._hub_call("audit.query", {"limit": limit})
        except Exception as e:  # noqa: BLE001
            raise self._hub_error(e) from e
        entries = []
        for i, r in enumerate(rows or []):
            args = r.get("args") if isinstance(r.get("args"), dict) else {}
            ts = r.get("ts")
            entries.append({
                "id": i, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)) if isinstance(ts, (int, float)) else str(ts or ""),
                "event": r.get("operation", ""), "details": json.dumps(args)[:500], "sourceIp": "", "user": r.get("client", ""),
                "vmName": str(args.get("name", "")), "status": "ok" if r.get("ok") else "error", "error": r.get("error") or "",
            })
        await self._audit(ctx, "audit_view", json.dumps({"limit": limit}), "ok")
        return web.json_response(entries)

    # -- logs --

    async def _handle_logs(self, request: web.Request) -> web.Response:
        """GET /api/v1/logs?lines=&level=: the newest lines of VM-Harness's hub log, parsed."""
        ctx = await self._require_tailscale_or_auth(request)
        try:
            want = max(1, min(2000, int(request.query.get("lines", "200"))))
        except ValueError:
            want = 200
        level = request.query.get("level", "").upper()
        home = Path(os.environ.get("VMH_HOME") or Path.home() / ".vmharness")
        pattern = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:,\d+)?) (\w+) ([\w.]+): (.*)$")

        def read() -> tuple[list[dict], bool]:
            path = home / "hub.log"
            if not path.is_file():
                return [], False
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 512 * 1024))
                lines = f.read().decode("utf-8", errors="replace").splitlines()
            out: list[dict] = []
            for line in lines:
                m = pattern.match(line)
                if m:
                    out.append({"timestamp": m.group(1), "level": m.group(2), "logger": m.group(3), "message": m.group(4)})
                elif out:  # a traceback or wrapped line belongs to the entry above
                    out[-1]["message"] += "\n" + line
            if level:
                out = [e for e in out if e["level"] == level]
            return out[-want:], len(out) > want
        logs, more = await asyncio.to_thread(read)
        await self._audit(ctx, "logs_view", json.dumps({"lines": want}), "ok")
        return web.json_response({"logs": logs, "hasMore": more})

    # -- snapshots --

    # ── route table ───────────────────────────────────────────────────────────

    def _build_app(self) -> web.Application:
        """Build the aiohttp Application with all routes."""
        app = web.Application()

        # JSON error handler
        @web.middleware
        async def json_error_middleware(request: web.Request, handler: Callable) -> web.StreamResponse:
            try:
                return await handler(request)
            except web.HTTPException as exc:
                exc.content_type = "application/json"
                if isinstance(exc.text, bytes):
                    try:
                        exc.text = exc.text.decode("utf-8")
                    except Exception:
                        pass
                # Ensure JSON body
                if not exc.text or not exc.text.startswith("{"):
                    exc.text = json.dumps({"error": exc.reason or "http_error"})
                raise

        app.middlewares.append(json_error_middleware)

        # Auth + Tailscale-IP filter middleware (applied to all non-pairing routes)
        @web.middleware
        async def api_auth_middleware(request: web.Request, handler: Callable) -> web.StreamResponse:
            if request.path.startswith("/api/v1/auth/pair"):
                return await handler(request)
            if request.path.startswith("/api/v1/auth/public-key"):
                return await handler(request)
            ctx = await self._require_tailscale_or_auth(request)
            # Attach ctx to request for handlers to use
            request._vmharness_ctx = ctx
            return await handler(request)

        app.middlewares.append(api_auth_middleware)

        # Routes
        app.router.add_post("/api/v1/auth/pair", self._handle_pair)
        app.router.add_get("/api/v1/auth/verify", self._handle_verify)
        app.router.add_post("/api/v1/auth/revoke", self._handle_revoke)
        app.router.add_get("/api/v1/auth/public-key", self._handle_public_key)

        app.router.add_get("/api/v1/", self._handle_dashboard)
        app.router.add_get("/api/v1/vms", self._handle_vms_list)
        app.router.add_get("/api/v1/vms/{name}", self._handle_vm_detail)
        # Specific routes first: /vms/{name}/{action} would otherwise swallow "snapshots".
        app.router.add_get("/api/v1/vms/{name}/snapshots", self._handle_snapshots_list)
        app.router.add_post("/api/v1/vms/{name}/snapshots", self._handle_snapshot_create)
        app.router.add_post("/api/v1/vms/{name}/snapshots/{snapshot_name}/restore", self._handle_snapshot_restore)
        app.router.add_post("/api/v1/vms/{name}/{action}", self._handle_vm_action)
        app.router.add_get("/api/v1/vms/{name}/qmp", self._handle_qmp_websocket)
        app.router.add_get("/api/v1/vms/{name}/terminal", self._handle_ssh_terminal)
        app.router.add_post("/api/v1/vms/{name}/ssh/command", self._handle_ssh_command)
        # Note: snapshots, qmp routes registered by subclass override

        app.router.add_get("/api/v1/metrics", self._handle_metrics)
        app.router.add_get("/api/v1/settings", self._handle_settings_get)
        app.router.add_put("/api/v1/settings", self._handle_settings_put)

        app.router.add_get("/api/v1/credentials", self._handle_credentials_list)
        app.router.add_get("/api/v1/credentials/{name}", self._handle_credential_detail)

        app.router.add_get("/api/v1/security/audit", self._handle_audit)
        app.router.add_get("/api/v1/logs", self._handle_logs)

        # Health check (no auth)
        async def health(request: web.Request) -> web.Response:
            tailscale_info = self._tailscale_info or get_tailscale_info() or {}
            return web.json_response({
                "status": "ok",
                "tailscale_running": bool(tailscale_info),
                "tailscale_ip": tailscale_info.get("ip", ""),
                "port": self._port,
            })

        app.router.add_get("/health", health)

        return app

    # ── run ────────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start the API server. Runs until cancelled (CancelledError)."""
        self._tailscale_info = get_tailscale_info()
        if self._tailscale_only and self._tailscale_info is None:
            log.warning(
                "Tailscale is not running — API server starting anyway "
                "(tailscale_only=%s). Android client will not be able to connect.",
                self._tailscale_only,
            )

        self._app = self._build_app()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        # Bind to Tailscale IP if available, otherwise 0.0.0.0
        bind_host = self._host
        if self._tailscale_only and self._tailscale_info and self._tailscale_info.get("ip"):
            bind_host = self._tailscale_info["ip"]
            log.info("API server binding to Tailscale IP: %s:%s", bind_host, self._port)
        else:
            log.info("API server binding to %s:%s", bind_host, self._port)

        ssl_ctx = getattr(self, '_ssl_context', None)
        self._site = web.TCPSite(self._runner, bind_host, self._port, ssl_context=ssl_ctx)
        await self._site.start()
        log.info("API server listening on %s:%s", bind_host, self._port)

        # Print pairing info to stdout (useful for headless mode)
        try:
            token, payload, info = self.generate_pairing_token()
            import qrcode
            qr = qrcode.make(f"vmharness://pair?key={token}")
            # Print text summary
            print("\n" + "=" * 60)
            print("  VM-Harness Mobile Pairing")
            print(f"  Desktop: {payload.display_name}")
            print(f"  Host:    {payload.host}")
            print(f"  IP:      {payload.ip}")
            print(f"  Tailnet: {payload.tailnet}")
            print(f"  Machine: {payload.machine_id}")
            print(f"  Token TTL: {payload.expires - payload.created}s" if payload.expires > 0 else "  Token: perpetual")
            print("=" * 60)
            print("  Pairing URI (QR / manual):")
            print(f"  vmharness://pair?key={token}")
            print("=" * 60)
            print("  (Connect your Android app via Tailscale, then scan QR or enter the URI above)")
            print("-" * 60)
        except Exception as e:
            log.debug("Could not print pairing info: %s", e)

        # Keep running until cancelled
        try:
            await asyncio.Future()  # blocks forever
        except asyncio.CancelledError:
            log.info("API server shutting down")
            raise

    async def shutdown(self) -> None:
        """Gracefully shut down the API server."""
        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()
        log.info("API server stopped")


# ── convenience factory ────────────────────────────────────────────────────────

def create_api_server(
    *,
    host: str = "0.0.0.0",
    port: int = API_SERVER_PORT,
    tailscale_only: bool = True,
    signing_key_dir: Path | None = None,
    credential_store_dir: Path | None = None,
) -> QMCMApiServer:
    """Create a VM-Harness API server instance (pre-init, no live object connections)."""
    return QMCMApiServer(
        host=host,
        port=port,
        tailscale_only=tailscale_only,
        signing_key_dir=signing_key_dir,
        credential_store_dir=credential_store_dir,
    )
