"""The node registry: which nodes this installation knows about, and their health.

Two halves that are deliberately not confused with each other:

* :class:`~vm_harness.cluster.config.NodeSpec` -- what the operator *configured*.
  Static, written to disk, trusted as intent.
* :class:`NodeEntry` -- what the registry *knows right now*: the spec plus observed
  health, plus the work already placed on it.

Keeping them apart is what makes "unknown" reportable. A node that is in the
config file has not thereby been heard from, and :meth:`NodeRegistry.status`
says so rather than defaulting to healthy because it was mentioned in a file.

    reg = NodeRegistry(ClusterConfig(nodes=[...]))
    reg.record_heartbeat("lab-1", reported_slots=4)
    reg.status()          # -> {"lab-1": NodeEntry(health=HEALTHY, ...)}
    reg.place_work("lab-1", 1)   # capacity is observed, not declared
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from vm_harness.cluster.config import ClusterConfig, ConfigError, NodeSpec
from vm_harness.cluster.health import (
    DEFAULT_POLICY,
    HealthPolicy,
    HealthTracker,
    NodeHealth,
)


@dataclass
class NodeEntry:
    """A configured node plus everything observed about it."""

    spec: NodeSpec
    health: NodeHealth = NodeHealth.UNKNOWN
    #: Slots actually reported free by the node's most recent heartbeat.
    reported_free_slots: Optional[int] = None
    #: Slots currently assigned by this installation's scheduler.
    assigned_slots: int = 0
    last_error: str = ""

    @property
    def node_id(self) -> str:
        return self.spec.id

    @property
    def observed_capacity(self) -> Optional[int]:
        """Capacity as the node reported it, or None if it has never said.

        ``None`` is not zero. A node that has never reported has an unknown
        capacity, and the scheduler refuses to guess one.
        """
        if self.reported_free_slots is None:
            return None
        return max(0, self.reported_free_slots) - self.assigned_slots

    def has_capability(self, capability: str) -> bool:
        return self.spec.has(capability)

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        return {
            "id": self.node_id,
            "host": self.spec.host,
            "health": self.health.value,
            "schedulable": self.health.schedulable,
            "capabilities": sorted(self.spec.capabilities),
            "configured_capacity": self.spec.capacity_slots,
            "capacity_known": self.spec.capacity_known(),
            "observed_free_slots": self.observed_capacity,
            "assigned_slots": self.assigned_slots,
            "last_error": self.last_error,
            "observed_at": now,
        }


class NodeRegistry:
    """The set of nodes, their health, and what has been placed on them."""

    def __init__(self, config: Optional[ClusterConfig] = None,
                 policy: HealthPolicy = DEFAULT_POLICY,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self._config = config if config is not None else ClusterConfig()
        self._health = HealthTracker(policy=policy, clock=clock)
        self._entries: dict[str, NodeEntry] = {
            spec.id: NodeEntry(spec=spec) for spec in self._config.nodes
        }

    @property
    def config(self) -> ClusterConfig:
        return self._config

    @property
    def health_tracker(self) -> HealthTracker:
        return self._health

    def entry(self, node_id: str) -> NodeEntry | None:
        """The named node, or None. A node that has never been configured is
        ``None`` rather than a synthetic entry -- there is no such thing as a
        node we merely failed to hear from."""
        return self._entries.get(node_id)

    def nodes(self) -> list[NodeEntry]:
        """Every node, sorted by id for a stable display order."""
        return [self._entries[n] for n in sorted(self._entries)]

    def add_node(self, spec: NodeSpec) -> NodeEntry:
        """Add a node to the live registry and the config behind it."""
        self._config.add(spec)
        existing = self._entries.get(spec.id)
        if existing is not None:
            # Keep observed state across a spec edit: the operator correcting a
            # host address does not make the node's heartbeat history false.
            existing.spec = spec
            return existing
        entry = NodeEntry(spec=spec)
        self._entries[spec.id] = entry
        return entry

    def remove_node(self, node_id: str) -> bool:
        """Forget a node entirely, including its health history."""
        if self._config.remove(node_id):
            self._entries.pop(node_id, None)
            return True
        return False

    # ── observations ──────────────────────────────────────────────────────

    def record_heartbeat(self, node_id: str, reported_slots: int = 0,
                         detail: str = "", at: float | None = None) -> NodeEntry:
        """A node reported in. Raises KeyError for a node not in the config.

        Refusing an unknown node is deliberate: silently registering whatever id
        a heartbeat claims would let one misconfigured agent add itself to the
        cluster and start receiving work.
        """
        entry = self._require(node_id)
        self._health.record_heartbeat(node_id, at=at,
                                      reported_slots=reported_slots, detail=detail)
        entry.reported_free_slots = max(0, int(reported_slots))
        entry.last_error = ""
        return entry

    def record_failure(self, node_id: str, reason: str, at: float | None = None) -> NodeEntry:
        """A connect to a node we know about did not succeed."""
        entry = self._require(node_id)
        self._health.record_failure(node_id, reason, at=at)
        entry.last_error = reason
        return entry

    def note_missed(self, node_id: str) -> NodeEntry:
        """A heartbeat did not arrive. Not a failure -- see
        :mod:`vm_harness.cluster.health`."""
        self._require(node_id)
        self._health.note_missed(node_id)
        return self._entries[node_id]

    def release_slots(self, node_id: str, count: int) -> NodeEntry:
        """Give back slots held by work that has finished or been abandoned."""
        entry = self._require(node_id)
        entry.assigned_slots = max(0, entry.assigned_slots - max(0, int(count)))
        return entry

    def assign_slots(self, node_id: str, count: int) -> NodeEntry:
        """Record that slots are held, refusing to exceed observed free capacity.

        Over-assignment would mean promising capacity the node never offered.
        """
        entry = self._require(node_id)
        want = max(0, int(count))
        free = entry.observed_capacity
        if free is not None and want > free:
            raise CapacityError(
                f"node {node_id!r} has {free} free slot(s) reported; {want} requested"
            )
        entry.assigned_slots += want
        return entry

    # ── queries ───────────────────────────────────────────────────────────

    def refresh_health(self, now: float | None = None) -> None:
        """Recompute every entry's health from the policy, as of ``now``."""
        for entry in self._entries.values():
            entry.health = self._health.health_of(entry.node_id, now)

    def status(self, now: float | None = None) -> dict[str, NodeEntry]:
        """Every node with health recomputed. The plain way to ask "what is up"."""
        self.refresh_health(now)
        return {e.node_id: e for e in self.nodes()}

    def schedulable(self, now: float | None = None) -> list[NodeEntry]:
        """Nodes that may receive work: healthy or stale, never unknown or down."""
        return [e for e in self.status(now).values() if e.health.schedulable]

    def health_of(self, node_id: str, now: float | None = None) -> NodeHealth:
        """One node's current health, recomputed."""
        self._require(node_id)
        return self._health.health_of(node_id, now)

    def unknown(self, now: float | None = None) -> list[str]:
        """Configured nodes we have lost track of.

        Enumerated from the config rather than from the tracker's records: a node
        that has never sent a heartbeat has no record, and reporting it nowhere at
        all would hide exactly the case worth showing.
        """
        return [node_id for node_id in sorted(self._entries)
                if self._health.health_of(node_id, now) is NodeHealth.UNKNOWN]

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        moment = time.time() if now is None else now
        statuses = self.status(moment)
        return {
            "local_node": self._config.local_node,
            "nodes": [e.to_dict(moment) for e in statuses.values()],
            "counts": {
                state.value: sum(1 for e in statuses.values() if e.health is state)
                for state in NodeHealth
            },
        }

    def _require(self, node_id: str) -> NodeEntry:
        entry = self._entries.get(node_id)
        if entry is None:
            known = ", ".join(sorted(self._entries)) or "none"
            raise KeyError(f"no node {node_id!r} in the registry (known: {known})")
        return entry


class CapacityError(Exception):
    """A placement would exceed what a node actually reported it could take."""


__all__ = ["CapacityError", "NodeEntry", "NodeRegistry"]