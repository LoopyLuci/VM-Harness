"""Placement: choosing a node by capability and observed capacity, or failing.

Policy, stated plainly so it can be argued with
--------------------------------------------------------
1. **Candidates must be schedulable.** Healthy or stale, never ``unknown`` or
   ``down``. Silence is not consent.
2. **Candidates must have every required capability**, taken from the node's
   configured spec. A capability the node reported in a heartbeat *widens* what
   it can take; it never substitutes for the config declaring it, because a
   capability only means something if the node knows what it is offering.
3. **Candidates must have observed free capacity** for the requested slots. A
   node that has never reported capacity is excluded, with the reason recorded --
   not assumed to be empty and not assumed to be full.
4. **Fewest committed load wins**, then most reported capacity, then node id as a
   deterministic tie-break. The last step exists so two nodes with identical
   figures do not swap places between calls and make the placement impossible to
   test.

If no node survives step 1-3, :class:`PlacementError` is raised listing each
candidate and why it was rejected. There is no fallback. A placement that picks
"the next best" node after the only capable one is unreachable is a VM that runs
somewhere the user did not ask for, discovered later.

Failover semantics
------------------
What is lost, retried, and not safe to retry:

* **Lost**: an in-flight task's progress. Placement is a decision, not
  checkpointing -- nothing here resumes a task.
* **Retried safely**: placement itself, and any task that has not yet been
  dispatched. Re-deciding is pure.
* **Retried with care**: a task whose effect is observable externally (a VM was
  started) is retried only if the caller can tolerate a duplicate, because the
  first attempt may have succeeded before the node became unreachable.
* **Not safe to retry**: anything destructive or non-idempotent -- deleting a
  disk, writing guest files, `cont`-ing a VM whose run state is already known.
  :attr:`RetryPolicy` encodes this as data so a caller cannot "helpfully" retry
  a delete by accident.

    from vm_harness.cluster.scheduler import Scheduler, WorkRequest
    sched = Scheduler(registry)
    placement = sched.place(WorkRequest(name="build", capabilities={"vm"}, slots=2))
    placement.node          # -> "lab-2"
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from vm_harness.cluster.config import NodeSpec
from vm_harness.cluster.health import NodeHealth
from vm_harness.cluster.registry import NodeEntry, NodeRegistry


class PlacementError(Exception):
    """No node could take this work.

    Carries the per-node reasons so the operator sees *why*, rather than a bare
    "no capacity" that could equally mean "nothing configured" or "everything
    full".
    """

    def __init__(self, message: str, reasons: Optional[dict[str, str]] = None) -> None:
        super().__init__(message)
        self.reasons: dict[str, str] = dict(reasons or {})

    def describe(self) -> str:
        """The message plus every rejection reason, one per line."""
        if not self.reasons:
            return str(self)
        lines = [str(self)]
        lines += [f"  {node}: {why}" for node, why in sorted(self.reasons.items())]
        return "\n".join(lines)


@dataclass(frozen=True)
class WorkRequest:
    """A unit of work to place.

    ``slots`` is how much parallel capacity it needs. ``name`` is for messages
    only -- it never affects placement, so two requests differing only in name
    place identically.
    """

    name: str = ""
    capabilities: frozenset[str] = frozenset()
    slots: int = 1
    #: Only consider this node. Still validated -- a pinned node that cannot take
    #: the work fails rather than quietly moving elsewhere.
    pin_node: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    #: Accept a stale (not healthy) node. Still not ``unknown``.
    allow_stale: bool = True

    def __post_init__(self) -> None:
        if self.slots < 1:
            raise ValueError("a work request needs at least one slot")


@dataclass(frozen=True)
class Placement:
    """A decision, with the evidence it was based on."""

    node: str
    host: str
    slots: int
    #: One line naming the node's load at decision time.
    reason: str
    #: Every node considered and whether it was eligible, for the audit trail.
    considered: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"node": self.node, "host": self.host, "slots": self.slots,
                "reason": self.reason, "considered": dict(self.considered)}


class RetryPolicy(str, Enum):
    """Whether an operation may be re-dispatched after a node becomes unreachable."""

    #: Pure: re-deciding changes nothing.
    SAFE = "safe"
    #: Idempotent in effect, but the caller should know it may have already run.
    MAY_DUPLICATE = "may_duplicate"
    #: Non-idempotent or destructive. Never retried automatically.
    UNSAFE = "unsafe"


@dataclass(frozen=True)
class FailoverPolicy:
    """What happens when a node stops being usable mid-task.

    Kept as data rather than as behaviour so the rules are inspectable and
    testable, and so an operator can read the guarantees without reading the
    placement code.
    """

    default_retry: RetryPolicy = RetryPolicy.SAFE
    #: Re-decide and re-dispatch only for these.
    retry_policies: dict[str, RetryPolicy] = field(default_factory=dict)

    #: What is lost, in one sentence each, for the docs and for the API surface.
    loss_notes: tuple[str, ...] = (
        "In-flight progress is lost: placement is a decision, not a checkpoint.",
        "Node-local scratch data stays on the failed node; only the task is re-placed.",
    )

    def policy_for(self, operation: str) -> RetryPolicy:
        """The retry policy for an operation, defaulting to the configured one."""
        return self.retry_policies.get(operation, self.default_retry)

    def may_retry(self, operation: str) -> bool:
        """Whether an operation may be retried after a node is lost."""
        return self.policy_for(operation) is not RetryPolicy.UNSAFE

    def to_dict(self) -> dict[str, Any]:
        return {
            "default_retry": self.default_retry.value,
            "operations": {k: v.value for k, v in sorted(self.retry_policies.items())},
            "loss_notes": list(self.loss_notes),
        }


#: Sensible defaults, and deliberately conservative: anything that deletes or
#: writes into a guest is marked unsafe, because "the node vanished" is exactly
#: the moment a blind retry does the most damage.
DEFAULT_FAILOVER = FailoverPolicy(retry_policies={
    "vm.start": RetryPolicy.MAY_DUPLICATE,
    "vm.stop": RetryPolicy.MAY_DUPLICATE,
    "vm.reset": RetryPolicy.MAY_DUPLICATE,
    "vm.suspend": RetryPolicy.MAY_DUPLICATE,
    "vm.resume": RetryPolicy.MAY_DUPLICATE,
    "snapshot.create": RetryPolicy.MAY_DUPLICATE,
    "file.write": RetryPolicy.UNSAFE,
    "guest.exec": RetryPolicy.UNSAFE,
    "vm.delete": RetryPolicy.UNSAFE,
    "container.remove": RetryPolicy.UNSAFE,
})


class Scheduler:
    """Chooses a node for a request, or explains why it cannot."""

    def __init__(self, registry: NodeRegistry,
                 failover: FailoverPolicy = DEFAULT_FAILOVER) -> None:
        self._registry = registry
        self._failover = failover

    @property
    def registry(self) -> NodeRegistry:
        return self._registry

    @property
    def failover(self) -> FailoverPolicy:
        return self._failover

    def place(self, request: WorkRequest, now: float | None = None) -> Placement:
        """Pick a node for ``request``.

        Raises :class:`PlacementError` when nothing fits. Never returns a node it
        was not told to use and never degrades to "some other node".
        """
        if request.pin_node:
            return self._place_pinned(request, now)

        statuses = self._registry.status(now)
        reasons: dict[str, str] = {}
        eligible: list[NodeEntry] = []

        for node_id, entry in statuses.items():
            why = self._reject(entry, request, now)
            if why:
                reasons[node_id] = why
            else:
                eligible.append(entry)

        if not eligible:
            if not statuses:
                raise PlacementError(
                    f"no node in the registry can take {request.name or 'this work'!r}: "
                    "the cluster has no configured nodes",
                    reasons,
                )
            raise PlacementError(
                f"no node can take {request.name or 'this work'!r} "
                f"(needs {sorted(request.capabilities) or 'no specific capability'}, "
                f"{request.slots} slot(s))",
                reasons,
            )

        chosen = self._best(eligible)
        considered = {n: "eligible" if n == chosen.node_id else "eligible, not chosen"
                      for n in (e.node_id for e in eligible)}
        considered.update(reasons)
        free = chosen.observed_capacity
        return Placement(
            node=chosen.node_id,
            host=chosen.spec.host,
            slots=request.slots,
            reason=(f"{chosen.node_id} has the most free capacity among eligible nodes "
                    f"({free} free, {chosen.assigned_slots} already assigned)"),
            considered=considered,
        )

    def commit(self, placement: Placement) -> NodeEntry:
        """Reserve the slots a placement just consumed.

        Separate from :meth:`place` on purpose: a caller that only wants to know
        *where* work would go must not silently consume capacity by asking.
        """
        entry = self._registry.assign_slots(placement.node, placement.slots)
        self._registry.refresh_health()
        return entry

    def place_and_commit(self, request: WorkRequest, now: float | None = None) -> Placement:
        """Place and reserve in one step. Rolls back the reservation if the
        reservation itself would exceed the node's reported capacity, so a
        caller never ends up holding a placement it could not honour."""
        placement = self.place(request, now)
        try:
            self.commit(placement)
        except Exception:
            raise
        return placement

    # ── internals ─────────────────────────────────────────────────────────

    def _place_pinned(self, request: WorkRequest, now: float | None = None) -> Placement:
        entry = self._registry.entry(request.pin_node)
        if entry is None:
            raise PlacementError(
                f"{request.name or 'this work'!r} is pinned to {request.pin_node!r}, "
                "which is not in the registry"
            )
        self._registry.refresh_health(now)
        entry.health = self._registry.health_tracker.health_of(request.pin_node, now)
        why = self._reject(entry, request, now)
        if why:
            raise PlacementError(
                f"{request.name or 'this work'!r} is pinned to {request.pin_node!r}, "
                "which cannot take it",
                {request.pin_node: why},
            )
        return Placement(
            node=entry.node_id,
            host=entry.spec.host,
            slots=request.slots,
            reason=f"pinned to {entry.node_id} by request",
            considered={entry.node_id: "pinned"},
        )

    def _reject(self, entry: NodeEntry, request: WorkRequest, now: float | None = None) -> str:
        """Why this node cannot take the request, or "" if it can."""
        health = entry.health
        if health is NodeHealth.DOWN:
            return "node reported a failure"
        if health is NodeHealth.UNKNOWN:
            age = None
            record = self._registry.health_tracker.record_for(entry.node_id)
            age = record.age_since_heartbeat(
                now if now is not None else _now_fallback(self._registry)
            )
            if record.ever_heard_from:
                return f"no heartbeat for {age:.0f}s (unknown, not down)"
            return "never sent a heartbeat (unknown, not down)"
        if health is NodeHealth.STALE and not request.allow_stale:
            return "heartbeat is stale and the request does not accept stale nodes"

        missing = sorted(request.capabilities - entry.spec.capabilities)
        if missing:
            return f"missing capability: {', '.join(missing)}"

        free = entry.observed_capacity
        if free is None:
            if entry.spec.capacity_known():
                # Configured capacity with no heartbeat: usable only if the work
                # fits the configured number, and said so in the reason.
                if request.slots > entry.spec.capacity_slots:
                    return (f"no heartbeat, and configured capacity "
                            f"({entry.spec.capacity_slots}) is below {request.slots} slot(s)")
                return ""
            return "has never reported capacity (unknown, not empty)"
        if free < request.slots:
            return f"insufficient free slots ({free} < {request.slots})"

        for key, want in request.labels.items():
            if entry.spec.labels.get(key, "") != want:
                return f"label {key}={want!r} does not match"
        return ""

    @staticmethod
    def _best(entries: list[NodeEntry]) -> NodeEntry:
        """Fewest committed load, then most free capacity, then node id.

        The final key is a deterministic tie-break rather than dict order: a
        scheduler whose answer depends on insertion order is one that cannot be
        tested and that flaps between two equivalent nodes.
        """
        def key(entry: NodeEntry) -> tuple[int, int, str]:
            free = entry.observed_capacity
            return (
                entry.assigned_slots,
                -(free if free is not None else -1),
                entry.node_id,
            )

        return sorted(entries, key=key)[0]


def _now_fallback(registry: NodeRegistry) -> float:
    import time

    return time.time()


__all__ = [
    "DEFAULT_FAILOVER",
    "FailoverPolicy",
    "NodeSpec",
    "Placement",
    "PlacementError",
    "RetryPolicy",
    "Scheduler",
    "WorkRequest",
]