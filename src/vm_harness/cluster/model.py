"""What a VM, a container and a cluster node *are*, as data.

The distinction this module exists to enforce: **what was measured** versus
**what was inferred from configuration**. A configured port is not a listening
socket, and a VM listed in a config file is not a running guest. Presenting an
inference as an observation is worse than reporting ``unknown``, because the
user stops looking.

Every field that could be wrong carries a :class:`Provenance`, so a caller can
refuse to act on a guess:

    OBSERVED   measured just now (a TCP connect completed, a heartbeat arrived)
    REPORTED   asserted by a live runtime (Docker's inspect output, QMP's answer)
    INFERRED   derived from configuration, never checked
    UNKNOWN    we could not determine it, and are not going to pretend otherwise

    from vm_harness.cluster.model import Endpoint, EndpointKind, Provenance
    Endpoint(id="alpha", kind=EndpointKind.VM, state="running",
             state_provenance=Provenance.INFERRED)   # a guess, labelled as one
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EndpointKind(str, Enum):
    """What sort of thing this endpoint is.

    ``NODE`` is a whole machine in the cluster; ``HOST`` is this machine seen as
    the local endpoint of a port forward. The distinction matters when a link is
    described, because "reachable" means different things for each.
    """

    VM = "vm"
    CONTAINER = "container"
    NODE = "node"
    HOST = "host"


class Provenance(str, Enum):
    """How much we actually know about a field."""

    OBSERVED = "observed"      # measured here, just now
    REPORTED = "reported"      # stated by a live runtime we queried
    INFERRED = "inferred"      # read out of configuration; never checked
    UNKNOWN = "unknown"        # not determined, and left that way

    @property
    def trustworthy(self) -> bool:
        """True only for facts that came from something we actually talked to."""
        return self in (Provenance.OBSERVED, Provenance.REPORTED)


class LinkKind(str, Enum):
    """How two endpoints are related."""

    #: host:port -> guest:port. The only relationship this host can really offer.
    PORT_FORWARD = "port_forward"
    #: Both ends on one segment. Only claimed when something observed it.
    L2 = "l2"
    #: Known by configuration only (e.g. a declared bridge name that no helper built).
    DECLARED = "declared"


@dataclass(frozen=True)
class PortForward:
    """A host-side listening socket that leads somewhere else.

    ``provenance`` on a forward is almost always :attr:`Provenance.INFERRED`:
    the port is in the config, nobody has connected to it. Only after
    :func:`vm_harness.cluster.network.verify_forward` does it become
    :attr:`Provenance.OBSERVED`.
    """

    name: str
    host: str
    port: int
    #: What the forward is *for*, e.g. "qmp", "ssh", "docker-api".
    purpose: str = ""
    provenance: Provenance = Provenance.INFERRED
    note: str = ""

    def target(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass
class Endpoint:
    """One VM or container, with everything we know and how we know it."""

    id: str
    kind: EndpointKind
    #: The node id this endpoint runs on. Empty for the local host.
    node: str = ""
    #: Addresses we have heard of. ``addresses_provenance`` says whether anyone checked.
    addresses: list[str] = field(default_factory=list)
    addresses_provenance: Provenance = Provenance.UNKNOWN
    ports: list[PortForward] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    #: running | paused | stopped | unknown. Never a default of "running".
    state: str = "unknown"
    state_provenance: Provenance = Provenance.UNKNOWN
    notes: list[str] = field(default_factory=list)

    def forward(self, name: str) -> PortForward | None:
        """The named port forward, or None. Looked up rather than indexed so a
        missing forward is an ordinary result, not a KeyError in a UI paint."""
        for p in self.ports:
            if p.name == name:
                return p
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "node": self.node,
            "addresses": list(self.addresses),
            "addresses_provenance": self.addresses_provenance.value,
            "ports": [
                {"name": p.name, "host": p.host, "port": p.port, "purpose": p.purpose,
                 "provenance": p.provenance.value, "note": p.note}
                for p in self.ports
            ],
            "labels": dict(self.labels),
            "state": self.state,
            "state_provenance": self.state_provenance.value,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class Link:
    """A relationship between two endpoints, with its own provenance.

    A :attr:`LinkKind.L2` link is only ever created from an observation. On this
    host no such link can be, which is exactly why it is worth having the type.
    """

    source: str
    target: str
    kind: LinkKind
    detail: str = ""
    provenance: Provenance = Provenance.INFERRED

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "target": self.target, "kind": self.kind.value,
                "detail": self.detail, "provenance": self.provenance.value}


@dataclass
class Topology:
    """The endpoints and the links between them, plus what we could not establish.

    ``limitations`` is not decoration. On Windows with WHPX there is no way to
    bridge a Docker Desktop container to a VM at layer 2, and a topology view
    that quietly omitted that would invite the user to assume it works.
    """

    endpoints: list[Endpoint] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    #: Plain statements of what this host cannot do, in the user's terms.
    limitations: list[str] = field(default_factory=list)

    def endpoint(self, endpoint_id: str) -> Endpoint | None:
        """The named endpoint, or None."""
        for e in self.endpoints:
            if e.id == endpoint_id:
                return e
        return None

    def endpoints_on(self, node: str) -> list[Endpoint]:
        """Everything running on one node."""
        return [e for e in self.endpoints if e.node == node]

    def to_dict(self) -> dict[str, Any]:
        return {"endpoints": [e.to_dict() for e in self.endpoints],
                "links": [l.to_dict() for l in self.links],
                "limitations": list(self.limitations)}


def endpoint(
    endpoint_id: str,
    kind: EndpointKind,
    **kwargs: Any,
) -> Endpoint:
    """Build an :class:`Endpoint` with the safe defaults already applied."""
    return Endpoint(id=endpoint_id, kind=kind, **kwargs)