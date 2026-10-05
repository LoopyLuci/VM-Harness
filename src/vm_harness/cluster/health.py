"""Node health: four states, one of which is "we do not know".

A cluster has exactly one unforgivable failure mode, and it is not a node going
down -- it is a node going *quiet* and the operator being told it is down. A
frozen process, a severed network and a paused node all look like silence, and
none of them is a failure you can act on. So silence produces
:attr:`NodeHealth.UNKNOWN`, never ``DOWN``.

    HEALTHY   a heartbeat arrived within ``healthy_after``
    STALE     the last heartbeat is older than that, but we still hear from it
    UNKNOWN   no heartbeat has ever arrived, or it aged past ``unknown_after``
    DOWN      a heartbeat was received and then an explicit failure was observed

``DOWN`` is reserved for a node that told us it was fine and then demonstrably
failed -- a refused connection to a node we have heard from. That is a much
stronger claim than "no news", and it is the only one worth alerting on.

The clock is injected. Tests and callers that need to evaluate staleness
deterministically pass their own ``now``; nothing in this module reads the wall
clock implicitly.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class NodeHealth(str, Enum):
    """A node's health, with ``UNKNOWN`` as a first-class answer."""

    HEALTHY = "healthy"
    STALE = "stale"
    UNKNOWN = "unknown"
    DOWN = "down"

    @property
    def schedulable(self) -> bool:
        """Whether the scheduler may place new work here.

        ``UNKNOWN`` is deliberately excluded. Placing work on a node we cannot
        hear from turns "I could not determine the state" into "the work is
        running somewhere I know nothing about", which is the silent fallback
        this whole package exists to avoid.
        """
        return self in (NodeHealth.HEALTHY, NodeHealth.STALE)


@dataclass(frozen=True)
class Heartbeat:
    """A node saying it is alive, with whatever it reported about itself.

    ``reported_slots`` is what the node claims it can take, not what it can take.
    """

    node: str
    at: float
    reported_slots: int = 0
    detail: str = ""

    def age(self, now: float) -> float:
        return max(0.0, now - self.at)


@dataclass
class NodeHealthRecord:
    """Everything the registry knows about one node's health, and when it knew it."""

    node: str
    health: NodeHealth = NodeHealth.UNKNOWN
    last_heartbeat: Optional[Heartbeat] = None
    #: Set when a failure was *observed*, as opposed to merely not seen.
    last_failure: str = ""
    last_failure_at: float = 0.0
    #: Consecutive heartbeat checks that did not arrive.
    missed: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def ever_heard_from(self) -> bool:
        return self.last_heartbeat is not None

    def age_since_heartbeat(self, now: float) -> Optional[float]:
        """Seconds since the last heartbeat, or None if there has never been one."""
        return None if self.last_heartbeat is None else self.last_heartbeat.age(now)

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        moment = time.time() if now is None else now
        age = self.age_since_heartbeat(moment)
        return {
            "node": self.node,
            "health": self.health.value,
            "schedulable": self.health.schedulable,
            "last_heartbeat_at": None if self.last_heartbeat is None else self.last_heartbeat.at,
            "heartbeat_age_sec": None if age is None else round(age, 2),
            "reported_slots": 0 if self.last_heartbeat is None else self.last_heartbeat.reported_slots,
            "missed": self.missed,
            "last_failure": self.last_failure,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class HealthPolicy:
    """Thresholds for the four states.

    ``unknown_after`` must exceed ``healthy_after`` or a node would be declared
    unknowable while still heartbeating; the constructor refuses that ordering
    rather than letting a bad config produce nonsense states.
    """

    healthy_after: float = 15.0
    unknown_after: float = 60.0

    def __post_init__(self) -> None:
        if self.healthy_after <= 0:
            raise ValueError("healthy_after must be > 0")
        if self.unknown_after <= self.healthy_after:
            raise ValueError("unknown_after must be greater than healthy_after")

    def classify(self, record: NodeHealthRecord, now: float) -> NodeHealth:
        """The state a record is in at ``now``.

        An explicit failure observed after the last heartbeat is the only route to
        ``DOWN``: it means we reached for a node we knew was there and it did not
        answer, which is different from never having heard from it at all.
        """
        if record.last_failure and record.last_failure_at > (
            record.last_heartbeat.at if record.last_heartbeat else 0.0
        ):
            return NodeHealth.DOWN
        if record.last_heartbeat is None:
            return NodeHealth.UNKNOWN
        age = record.last_heartbeat.age(now)
        if age <= self.healthy_after:
            return NodeHealth.HEALTHY
        if age <= self.unknown_after:
            return NodeHealth.STALE
        return NodeHealth.UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return {"healthy_after": self.healthy_after, "unknown_after": self.unknown_after}


#: Generous defaults: a heartbeat every few seconds, so 15s of silence is a real
#: signal and a full minute is genuinely "we have lost track of this node".
DEFAULT_POLICY = HealthPolicy()


class HealthTracker:
    """Applies a :class:`HealthPolicy` to records as time passes.

    Recomputed on read rather than mutated by a background timer, so there is no
    interval at which the stored state is wrong, and no thread to synchronise.
    """

    def __init__(self, policy: HealthPolicy = DEFAULT_POLICY,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self._policy = policy
        self._clock = clock or time.time
        self._records: dict[str, NodeHealthRecord] = {}

    @property
    def policy(self) -> HealthPolicy:
        return self._policy

    def record_for(self, node: str) -> NodeHealthRecord:
        """The record for a node, created empty if this is the first we hear of it."""
        if node not in self._records:
            self._records[node] = NodeHealthRecord(node=node)
        return self._records[node]

    def record_heartbeat(self, node: str, at: float | None = None,
                         reported_slots: int = 0, detail: str = "") -> NodeHealthRecord:
        """Record that a node is alive, clearing any outstanding failure.

        A fresh heartbeat clears ``last_failure`` because the node has just
        demonstrated it is up; keeping the old failure would keep it ``DOWN``
        forever behind a node that is plainly working.
        """
        moment = self._clock() if at is None else at
        record = self.record_for(node)
        record.last_heartbeat = Heartbeat(node=node, at=moment,
                                          reported_slots=reported_slots, detail=detail)
        record.last_failure = ""
        record.last_failure_at = 0.0
        record.missed = 0
        return record

    def record_failure(self, node: str, reason: str, at: float | None = None) -> NodeHealthRecord:
        """Record an *observed* failure -- a connect we attempted and did not make.

        Distinct from a heartbeat that failed to arrive. Callers must not use this
        to mean "I did not hear from it"; that is silence, and silence is
        ``UNKNOWN``.
        """
        moment = self._clock() if at is None else at
        record = self.record_for(node)
        record.last_failure = reason
        record.last_failure_at = moment
        record.missed += 1
        return record

    def note_missed(self, node: str) -> NodeHealthRecord:
        """Record a heartbeat that did not arrive. This is not a failure."""
        record = self.record_for(node)
        record.missed += 1
        return record

    def health_of(self, node: str, now: float | None = None) -> NodeHealth:
        """The node's current state."""
        return self._policy.classify(self.record_for(node),
                                      self._clock() if now is None else now)

    def records(self, now: float | None = None) -> list[NodeHealthRecord]:
        """Every record, with ``health`` recomputed as of ``now``."""
        moment = self._clock() if now is None else now
        out: list[NodeHealthRecord] = []
        for node in sorted(self._records):
            record = self._records[node]
            record.health = self._policy.classify(record, moment)
            out.append(record)
        return out

    def healthy(self, now: float | None = None) -> list[str]:
        """Nodes that are healthy, sorted, for a stable display order."""
        return [r.node for r in self.records(now) if r.health is NodeHealth.HEALTHY]

    def schedulable(self, now: float | None = None) -> list[str]:
        """Nodes the scheduler is allowed to use. Excludes ``UNKNOWN`` and ``DOWN``."""
        return [r.node for r in self.records(now) if r.health.schedulable]

    def unknown(self, now: float | None = None) -> list[str]:
        """Nodes we have lost track of. Never reported as down."""
        return [r.node for r in self.records(now) if r.health is NodeHealth.UNKNOWN]

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        moment = self._clock() if now is None else now
        return {
            "policy": self._policy.to_dict(),
            "nodes": [r.to_dict(moment) for r in self.records(moment)],
        }


__all__ = [
    "DEFAULT_POLICY",
    "HealthPolicy",
    "HealthTracker",
    "Heartbeat",
    "NodeHealth",
    "NodeHealthRecord",
]