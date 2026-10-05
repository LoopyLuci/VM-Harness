"""VM and container topology, reachability, and cluster placement.

What this package is for
------------------------
Two questions that are easy to answer badly on a Windows host:

* "Can I reach this VM's QMP port?" -- answered by an actual TCP connect
  (:mod:`vm_harness.cluster.network`), never by reading the config.
* "Which machine should run this?" -- answered by capability and *observed*
  capacity, or not at all (:mod:`vm_harness.cluster.scheduler`).

What it is deliberately not
---------------------------
It does not build layer-2 connectivity. On this host there is no bridge to build:
WHPX guests and Docker Desktop (WSL2) containers share no segment, and the Linux
helpers that would join them do not exist here.
:func:`vm_harness.cluster.network.layer2_capability` says so rather than
pretending.

The organizing idea throughout is :class:`~vm_harness.cluster.model.Provenance`:
a field that was inferred from a config file is labelled as inferred, and a field
nobody could determine is ``unknown``. Silence from a node is never reported as
``down``.
"""
from __future__ import annotations

from vm_harness.cluster.config import (
    KNOWN_CAPABILITIES,
    ClusterConfig,
    ConfigError,
    NodeSpec,
)
from vm_harness.cluster.health import (
    DEFAULT_POLICY,
    HealthPolicy,
    HealthTracker,
    Heartbeat,
    NodeHealth,
    NodeHealthRecord,
)
from vm_harness.cluster.model import (
    Endpoint,
    EndpointKind,
    Link,
    LinkKind,
    PortForward,
    Provenance,
    Topology,
)
from vm_harness.cluster.network import (
    DEFAULT_TIMEOUT,
    ProbeResult,
    Reachability,
    ReachabilityCache,
    ReachabilityVerifier,
    container_api_forward,
    layer2_capability,
    vm_forwards,
)
from vm_harness.cluster.registry import CapacityError, NodeEntry, NodeRegistry
from vm_harness.cluster.scheduler import (
    DEFAULT_FAILOVER,
    FailoverPolicy,
    Placement,
    PlacementError,
    RetryPolicy,
    Scheduler,
    WorkRequest,
)

__all__ = [
    "DEFAULT_FAILOVER",
    "DEFAULT_POLICY",
    "DEFAULT_TIMEOUT",
    "KNOWN_CAPABILITIES",
    "CapacityError",
    "ClusterConfig",
    "ConfigError",
    "Endpoint",
    "EndpointKind",
    "FailoverPolicy",
    "HealthPolicy",
    "HealthTracker",
    "Heartbeat",
    "Link",
    "LinkKind",
    "NodeEntry",
    "NodeHealth",
    "NodeHealthRecord",
    "NodeRegistry",
    "NodeSpec",
    "Placement",
    "PlacementError",
    "PortForward",
    "ProbeResult",
    "Provenance",
    "Reachability",
    "ReachabilityCache",
    "ReachabilityVerifier",
    "RetryPolicy",
    "Scheduler",
    "Topology",
    "WorkRequest",
    "container_api_forward",
    "layer2_capability",
    "vm_forwards",
]