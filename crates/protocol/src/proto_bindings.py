# VM-Harness protocol message bindings (complete)
# Protocol version: 3.0.0

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Any
import json

# ── VmConfig (vm.proto) ──────────────────────────────────────────────────────────
@dataclass
class DiskFormat:
    QCOW2 = "Qcow2"
    RAW = "Raw"
    VMDK = "Vmdk"
    VDI = "Vdi"

@dataclass
class NetworkMode:
    NAT = "Nat"
    BRIDGE = "Bridge"
    USER = "User"
    NONE = "None"

@dataclass
class DisplayType:
    SDL = "Sdl"
    VNC = "Vnc"
    SPICE = "Spice"
    GTK = "Gtk"
    NONE = "None"

@dataclass
class VmConfig:
    vm_id: str = ""
    name: str = ""
    memory_mb: int = 1024
    cpus: int = 2
    disk_path: str = ""
    disk_format: str = "Qcow2"
    network_mode: str = "Nat"
    display_type: str = "Sdl"
    enable_kvm: bool = False
    qmp_addr: str = "127.0.0.1:4444"
    ssh_port: int = 22
    extra_args: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    iso_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["extra_args"] = d.get("extra_args") or []
        d["tags"] = d.get("tags") or []
        return {k: v for k, v in d.items() if v is not None}
    @classmethod
    def from_dict(cls, data): return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
    def to_json(self) -> str: return json.dumps(self.to_dict())
    @classmethod
    def from_json(cls, s): return cls.from_dict(json.loads(s))
    def validate(self) -> List[str]:
        errors = []
        if not self.name: errors.append("name is required")
        if self.memory_mb < 128: errors.append("memory_mb must be at least 128")
        if self.cpus < 1: errors.append("cpus must be at least 1")
        if not self.disk_path and not self.iso_path: errors.append("disk_path or iso_path required")
        return errors

# ── VmMetrics (telemetry.proto) ─────────────────────────────────────────────────
@dataclass
class VmMetrics:
    vm_id: str = ""
    cpu_percent: float = 0.0
    memory_used_mb: int = 0
    memory_total_mb: int = 0
    disk_read_bytes: int = 0
    disk_write_bytes: int = 0
    net_rx_bytes: int = 0
    net_tx_bytes: int = 0
    uptime_seconds: int = 0

    def to_dict(self) -> Dict[str, Any]: return asdict(self)
    @classmethod
    def from_dict(cls, data): return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
    def to_json(self) -> str: return json.dumps(self.to_dict())
    @classmethod
    def from_json(cls, s): return cls.from_dict(json.loads(s))

# ── PairingToken (pairing.proto) ─────────────────────────────────────────────────
@dataclass
class PairingToken:
    device_id: str = ""
    token: str = ""
    status: str = "pending"
    expires_at: str = ""

    def to_dict(self) -> Dict[str, Any]: return asdict(self)
    @classmethod
    def from_dict(cls, data): return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
    def to_json(self) -> str: return json.dumps(self.to_dict())
    @classmethod
    def from_json(cls, s): return cls.from_dict(json.loads(s))
    def is_valid(self) -> bool: return self.status == "paired" and self.expires_at != ""

class ChatMessage:
    """Chat message for AI conversation protocol."""

    def __init__(self, id: str = "", role: str = "user", content: str = "", tool_call_id: str = "", name: str = "", parent_id: str = ""):
        self.id = id
        self.role = role
        self.content = content
        self.tool_call_id = tool_call_id
        self.name = name
        self.parent_id = parent_id

    def to_dict(self):
        return {
            "id": self.id, "role": self.role, "content": self.content,
            "tool_call_id": self.tool_call_id, "name": self.name, "parent_id": self.parent_id
        }

    @classmethod
    def from_dict(cls, data: dict):
        return cls(**data)

    def to_json(self):
        import json
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, json_str: str):
        import json
        data = json.loads(json_str)
        return cls.from_dict(data)

    def validate(self) -> list:
        errors = []
        valid_roles = ["user", "assistant", "system", "tool"]
        if self.role not in valid_roles:
            errors.append(f"Invalid role: {self.role}. Must be one of {valid_roles}")
        if not self.content and self.role not in ["tool"]:
            errors.append("ChatMessage requires content")
        if self.tool_call_id and self.role != "tool":
            errors.append("tool_call_id should only be set for tool messages")
        return errors


class AuditEvent:
    """Audit event for security logging."""

    def __init__(self, id: str = "", audit_type: str = "auth", source: str = "", timestamp: str = "", user: str = ""):
        self.id = id
        self.audit_type = audit_type
        self.source = source
        self.timestamp = timestamp
        self.user = user

    def to_dict(self) -> dict:
        return {"id": self.id, "audit_type": self.audit_type, "source": self.source, "timestamp": self.timestamp, "user": self.user}

    @classmethod
    def from_dict(cls, data: dict):
        return cls(**data)

    def to_json(self) -> str:
        import json
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, json_str: str):
        import json
        data = json.loads(json_str)
        return cls.from_dict(data)

    def validate(self) -> list:
        errors = []
        valid_types = ["auth", "vm_lifecycle", "file_access", "pairing"]
        if self.audit_type not in valid_types:
            errors.append(f"Invalid audit_type: {self.audit_type}. Must be one of {valid_types}")
        if not self.id:
            errors.append("AuditEvent requires id")
        if not self.timestamp:
            errors.append("AuditEvent requires timestamp")
        return errors

