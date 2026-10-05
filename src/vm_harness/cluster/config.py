"""Cluster configuration: the nodes, what they can do, and how to reach them.

A node is a machine that may run VMs and containers for this installation. The
config is what the *operator* says about those machines; it is not evidence that
they exist. Nothing here is verified on load, which is why every field that a
running node could contradict lives in the health model instead (see
:mod:`vm_harness.cluster.health`).

    from vm_harness.cluster.config import ClusterConfig, NodeSpec
    cfg = ClusterConfig(nodes=[NodeSpec(id="lab-1", host="10.0.0.5",
                                        capabilities=("vm", "qmp"))])
    cfg.save(path)          # atomic: temp file + replace
    ClusterConfig.load(path)

Persistence is a write to a sibling temp file followed by ``os.replace``, which
is atomic on Windows and POSIX. ``gui/atomic_state.py`` does the same thing but
through a WAL keyed on a fixed state directory, and importing it from ``src`` to
hold a cluster config would invert the layering (``src`` does not depend on
``gui``); the eleven lines are cheaper than that dependency.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Capabilities this build understands. A node claiming anything else is not an
#: error -- it is forward compatibility -- but the scheduler will refuse to place
#: work on an unknown capability only if it was actually required.
KNOWN_CAPABILITIES: frozenset[str] = frozenset({
    "vm",            # can run a QEMU guest
    "container",     # can run a Docker/Podman container
    "qmp",           # exposes a reachable QMP socket
    "ssh",           # exposes a forwarded SSH port
    "k8s",           # has a working kubeconfig
    "gpu",           # has a passthrough-capable GPU
    "l2-bridge",     # can build a layer-2 bridge between guests (NOT this host)
})

DEFAULT_NODE_TIMEOUT = 5.0


class ConfigError(Exception):
    """The cluster config on disk is not usable as written."""


@dataclass(frozen=True)
class NodeSpec:
    """One machine, as the operator described it.

    ``capabilities`` and ``capacity_slots`` are *claims*. The registry turns
    claims into observed health; until a node has sent a heartbeat, every one of
    these fields is unconfirmed.
    """

    id: str
    host: str
    #: 0 means "unknown", not "unlimited": see :meth:`capacity_known`.
    capacity_slots: int = 0
    capabilities: frozenset[str] = frozenset()
    labels: dict[str, str] = field(default_factory=dict)
    #: How to reach this node's agent. ``tcp`` today; the scheme is stored rather
    #: than assumed so a second transport does not need a format change.
    scheme: str = "tcp"
    agent_port: int = 0
    heartbeat_timeout_sec: float = DEFAULT_NODE_TIMEOUT

    def capacity_known(self) -> bool:
        """Whether a slot count was actually declared.

        ``0`` is deliberately ambiguous-free: an undeclared capacity is
        ``False`` here, so the scheduler will not place work on the strength of a
        number nobody set.
        """
        return self.capacity_slots > 0

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "host": self.host,
            "capacity_slots": self.capacity_slots,
            "capabilities": sorted(self.capabilities),
            "labels": dict(self.labels),
            "scheme": self.scheme,
            "agent_port": self.agent_port,
            "heartbeat_timeout_sec": self.heartbeat_timeout_sec,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NodeSpec":
        try:
            node_id = str(data["id"])
            host = str(data["host"])
        except KeyError as exc:
            raise ConfigError(f"node is missing required field {exc}") from exc
        slots = int(data.get("capacity_slots", 0))
        if slots < 0:
            raise ConfigError(f"node {node_id!r}: capacity_slots cannot be negative")
        port = int(data.get("agent_port", 0))
        if not 0 <= port <= 65535:
            raise ConfigError(f"node {node_id!r}: agent_port {port} is out of range")
        timeout = float(data.get("heartbeat_timeout_sec", DEFAULT_NODE_TIMEOUT))
        if timeout <= 0:
            raise ConfigError(f"node {node_id!r}: heartbeat_timeout_sec must be > 0")
        return cls(
            id=node_id,
            host=host,
            capacity_slots=slots,
            capabilities=frozenset(str(c) for c in data.get("capabilities") or ()),
            labels={str(k): str(v) for k, v in (data.get("labels") or {}).items()},
            scheme=str(data.get("scheme", "tcp")),
            agent_port=port,
            heartbeat_timeout_sec=timeout,
        )


@dataclass
class ClusterConfig:
    """The whole cluster, as configured."""

    nodes: list[NodeSpec] = field(default_factory=list)
    #: This installation's own node id, used to label local endpoints.
    local_node: str = ""
    #: Version of the file format, so a future migration has something to switch on.
    version: int = 1

    def node(self, node_id: str) -> NodeSpec | None:
        """The named node, or None."""
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def add(self, spec: NodeSpec) -> None:
        """Add or replace a node by id.

        Replacing rather than appending keeps a config editable: re-adding a
        node the operator corrected must not leave two entries with one id, which
        would make every lookup by id ambiguous.
        """
        for i, existing in enumerate(self.nodes):
            if existing.id == spec.id:
                self.nodes[i] = spec
                return
        self.nodes.append(spec)

    def remove(self, node_id: str) -> bool:
        """Drop a node. False when there was nothing to drop."""
        before = len(self.nodes)
        self.nodes = [n for n in self.nodes if n.id != node_id]
        return len(self.nodes) != before

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "local_node": self.local_node,
            "nodes": [n.to_dict() for n in self.nodes],
        }

    def save(self, path: str | os.PathLike[str]) -> Path:
        """Write the config atomically.

        A cluster config truncated by a crash mid-write would leave the operator
        with a half-file and no way to tell which half was real, so the temp file
        is flushed to disk before it replaces the original.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        return target

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "ClusterConfig":
        """Read a config, or return an empty one when the file is absent.

        A missing file is an empty cluster, not an error: a single-node
        installation has no reason to have one. A file that exists but cannot be
        parsed *is* an error -- silently starting with no nodes would look
        exactly like losing the cluster.
        """
        target = Path(path)
        try:
            raw = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls()
        except OSError as exc:
            raise ConfigError(f"cannot read {target}: {exc}") from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ConfigError(f"{target} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{target} must contain a JSON object")
        nodes = [NodeSpec.from_dict(n) for n in (data.get("nodes") or [])]
        seen: set[str] = set()
        for node in nodes:
            if node.id in seen:
                raise ConfigError(f"duplicate node id {node.id!r} in {target}")
            seen.add(node.id)
        return cls(nodes=nodes, local_node=str(data.get("local_node", "")),
                   version=int(data.get("version", 1)))