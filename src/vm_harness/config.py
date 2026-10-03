"""Configuration management for vm-mcp.

Pydantic v2 BaseSettings for all config, loaded from .env + env vars.
Secrets are in a separate Secrets class so they're never serialized
into tool results or log output.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ── VmMCPSettings ───────────────────────────────────────────────────────────────

class VmMCPSettings(BaseSettings):
    """All configuration for the vm-mcp server.

    Loaded from .env file (if present) and environment variables.
    All fields have defaults so the server can run for testing without
    a .env file.  Production deployments MUST override the defaults.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # ── Identity ───────────────────────────────────────────────────────────────

    server_name: str = Field(default="vm-mcp", description="MCP server name")
    server_version: str = Field(default="0.1.0", description="Server version")

    # ── QEMU paths ─────────────────────────────────────────────────────────────

    qemu_binary: str = Field(
        default=r"C:\Program Files\qemu\qemu-system-x86_64.exe",
        description="Path to qemu-system-x86_64.exe",
    )
    qmp_host: str = Field(default="127.0.0.1", description="QMP listen address")
    qmp_port: int = Field(default=4444, description="QMP listen port", ge=1, le=65535)
    qmp_socket_path: str | None = Field(
        default=None, description="QMP Unix socket path (takes precedence)"
    )

    # ── VM definition ──────────────────────────────────────────────────────────

    vm_name: str = Field(default="omarchy-vm", description="QEMU VM name")
    vm_disk_path: str = Field(
        default=r"C:\Users\Server\Virtual Machines\omarchy-vm\disk.qcow2",
        description="Path to VM disk image",
    )
    vm_iso_path: str | None = Field(
        default=None,
        description="Path to Omarchy ISO (optional — VM disk has OS installed)",
    )
    vm_ram_mb: int = Field(default=8192, description="VM RAM in MB", ge=512, le=131072)
    vm_cpus: int = Field(default=4, description="Number of vCPUs", ge=1, le=128)

    # ── Display ────────────────────────────────────────────────────────────────

    vm_display: str = Field(default="sdl", description="QEMU display: sdl, gtk, none")
    vm_gl: bool = Field(default=True, description="Enable OpenGL in display")

    # ── Hardware Acceleration ──────────────────────────────────────────────────

    vm_acceleration: str = Field(
        default="whpx",
        description="Hardware acceleration mode: whpx, haxm, tcg (software)",
    )

    # ── SSH (guest access) ─────────────────────────────────────────────────────

    ssh_host: str = Field(default="127.0.0.1", description="Guest SSH hostname")
    ssh_port: int = Field(default=2222, description="Guest SSH port (QEMU user-mode NAT forwards host 2222 to guest 22)", ge=1, le=65535)
    ssh_username: str = Field(default="vmharness", description="Guest SSH username")
    ssh_timeout_sec: int = Field(default=15, description="SSH timeout in seconds", ge=1)
    ssh_keepalive_sec: int = Field(default=30, description="SSH keepalive interval", ge=0)
    ssh_known_hosts: str | None = Field(default=None, description="Path to known_hosts")

    # ── QMP auth ───────────────────────────────────────────────────────────────

    qmp_password: str | None = Field(default=None, description="QMP password")

    # ── Logging ────────────────────────────────────────────────────────────────

    log_level: str = Field(default="INFO", description="Logging level")
    log_format: str = Field(
        default="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        description="Python logging format",
    )
    log_file: str | None = Field(default=None, description="Optional log file path")

    # ── Transport ──────────────────────────────────────────────────────────────

    transport: str = Field(default="stdio", description="Transport: stdio, sse")
    sse_host: str = Field(default="127.0.0.1", description="SSE bind host")
    sse_port: int = Field(default=8080, description="SSE bind port", ge=1, le=65535)

    # ── Auth ───────────────────────────────────────────────────────────────────

    auth_enabled: bool = Field(default=False, description="Enable authentication")
    auth_type: str = Field(default="api_key", description="Auth type: api_key, jwt")
    auth_api_key: str | None = Field(default=None, description="API key for auth")

    # ── Post-install ───────────────────────────────────────────────────────────

    auto_eject_iso: bool = Field(default=True, description="Auto-eject ISO after boot")

    # ── Validators ─────────────────────────────────────────────────────────────

    @field_validator("vm_disk_path", "vm_iso_path", "qemu_binary", "ssh_known_hosts", "log_file")
    @classmethod
    def expand_path(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return os.path.expandvars(os.path.expanduser(v))

    def qmp_uri(self) -> str:
        """Return the QMP connection URI."""
        if self.qmp_socket_path:
            return f"unix:{self.qmp_socket_path}"
        return f"tcp:{self.qmp_host}:{self.qmp_port}"

    def ssh_connect_kwargs(self, secrets: "Secrets") -> dict[str, Any]:
        """Build kwargs for asyncssh.connect()."""
        kwargs: dict[str, Any] = {
            "host": self.ssh_host,
            "port": self.ssh_port,
            "username": self.ssh_username,
            "client_version": f"vm-mcp/{self.server_version}",
            "known_hosts": self.ssh_known_hosts,
        }
        if secrets.get_ssh_private_key():
            kwargs["client_keys"] = [secrets.get_ssh_private_key()]
        elif secrets.get_ssh_password():
            kwargs["password"] = secrets.get_ssh_password()
        return kwargs


# ── Backward compat alias ───────────────────────────────────────────────────────

VmConfig = VmMCPSettings  # type: ignore[assignment]


# ── Secrets ─────────────────────────────────────────────────────────────────────

class Secrets:
    """Holds sensitive values.  Never serialized into tool results or logs.

    Three-layer isolation:
      1. .env file on disk (chmod 600 in production)
      2. Secrets object in memory (never serialized, never logged)
      3. Tool functions receive only what they need via dependency injection
    """

    def __init__(
        self,
        ssh_password: str | None = None,
        ssh_private_key: str | None = None,
        qmp_password: str | None = None,
        auth_api_key: str | None = None,
    ):
        self._ssh_password = ssh_password or ""
        self._ssh_private_key = ssh_private_key or ""
        self._qmp_password = qmp_password or ""
        self._auth_api_key = auth_api_key or ""

    @classmethod
    def from_env(cls) -> Secrets:
        """Load secrets from environment variables."""
        return cls(
            ssh_password=os.getenv("SSH_PASSWORD"),
            ssh_private_key=os.getenv("SSH_PRIVATE_KEY"),
            qmp_password=os.getenv("QMP_PASSWORD"),
            auth_api_key=os.getenv("AUTH_API_KEY"),
        )

    @classmethod
    def from_dotenv(cls, path: str | Path = ".env") -> Secrets:
        """Load secrets from a .env file."""
        from dotenv import dotenv_values

        vals = dotenv_values(path)
        return cls(
            ssh_password=vals.get("SSH_PASSWORD"),
            ssh_private_key=vals.get("SSH_PRIVATE_KEY"),
            qmp_password=vals.get("QMP_PASSWORD"),
            auth_api_key=vals.get("AUTH_API_KEY"),
        )

    def mask(self) -> str:
        """Return a redacted summary for logging."""
        parts: list[str] = []
        if self._ssh_password:
            parts.append("ssh_password=***")
        if self._ssh_private_key:
            parts.append("ssh_private_key=***")
        if self._qmp_password:
            parts.append("qmp_password=***")
        if self._auth_api_key:
            parts.append("auth_api_key=***")
        return "Secrets(" + ", ".join(parts) + ")" if parts else "Secrets()"

    def has_ssh_creds(self) -> bool:
        return bool(self._ssh_password or self._ssh_private_key)

    def has_any_secret(self) -> bool:
        return any([self._ssh_password, self._ssh_private_key, self._qmp_password, self._auth_api_key])

    def get_ssh_password(self) -> str:
        """Return SSH password for internal use only.  Never exposed to tools."""
        return self._ssh_password

    def get_ssh_private_key(self) -> str | None:
        """Return SSH private key for internal use only.  Never exposed to tools."""
        return self._ssh_private_key

    def get_qmp_password(self) -> str | None:
        """Return QMP password for internal use only.  Never exposed to tools."""
        return self._qmp_password

    def get_auth_api_key(self) -> str | None:
        """Return API key for internal use only.  Never exposed to tools."""
        return self._auth_api_key
