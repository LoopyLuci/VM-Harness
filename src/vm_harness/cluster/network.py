"""Reachability that is actually measured, and never guessed.

A port in a config file is a wish. The only honest answer to "can I reach this?"
is to try, and to say which of the three outcomes it was:

    reachable    a TCP connect completed
    refused      the host answered, negatively (nothing listening / RST)
    timeout      nothing answered within the budget

``refused`` and ``timeout`` are both "no", but they are different diagnoses and a
user debugging a VM needs to tell them apart: a refused QMP port means QEMU is
running without that forward, a timeout means the guest's network is wrong.

Two rules this module holds to:

* **Never block a caller.** Probes run in a thread (:func:`probe_async`), so a UI
  thread can ask without freezing, and every socket carries an explicit timeout.
* **Cache briefly, then admit it.** Results younger than ``cache_ttl`` are
  reused so a 5-second GUI refresh does not re-probe the same ports; beyond that
  the answer is re-measured rather than trusted.
"""
from __future__ import annotations

import asyncio
import dataclasses
import socket
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Optional, Protocol

from vm_harness.cluster.model import Endpoint, PortForward, Provenance

DEFAULT_TIMEOUT = 2.0
DEFAULT_CACHE_TTL = 5.0


class Reachability(str, Enum):
    """The three real outcomes of a probe. There is no ``maybe``."""

    REACHABLE = "reachable"
    REFUSED = "refused"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class ProbeResult:
    """One measurement, with the evidence that produced it."""

    host: str
    port: int
    reachability: Reachability
    latency_ms: float
    #: When the measurement was taken, as a monotonic-ish wall clock reading.
    checked_at: float
    #: True when this came from the cache rather than a fresh connect.
    cached: bool = False
    detail: str = ""

    @property
    def reachable(self) -> bool:
        return self.reachability is Reachability.REACHABLE

    @property
    def provenance(self) -> Provenance:
        """A probe result is always observed -- that is the point of it."""
        return Provenance.OBSERVED

    def to_dict(self) -> dict[str, Any]:
        return {"host": self.host, "port": self.port,
                "reachability": self.reachability.value,
                "latency_ms": round(self.latency_ms, 2),
                "checked_at": self.checked_at, "cached": self.cached,
                "detail": self.detail, "provenance": self.provenance.value}


class Prober(Protocol):
    """Anything that can measure one host:port. Tests supply a fake; production
    supplies :func:`tcp_probe`. Kept as a Protocol so a fake is not a subclass of
    a class whose internals the test then has to match."""

    def __call__(self, host: str, port: int, timeout: float) -> ProbeResult: ...


def tcp_probe(host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> ProbeResult:
    """Try one TCP connect and report what happened.

    ``socket.create_connection`` is given the timeout directly, so a black-holed
    address costs ``timeout`` and not the 2-minute OS default. Everything is
    caught: a DNS failure or an unreachable network is a *result*, not an
    exception, because the caller asked a question whose honest answer is
    "no" (or "we could not tell") rather than a traceback.
    """
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except socket.timeout:
        return ProbeResult(host, port, Reachability.TIMEOUT,
                           (time.monotonic() - started) * 1000.0, time.time(),
                           detail=f"no answer within {timeout:g}s")
    except ConnectionRefusedError:
        return ProbeResult(host, port, Reachability.REFUSED,
                           (time.monotonic() - started) * 1000.0, time.time(),
                           detail="connection refused (nothing listening)")
    except OSError as exc:
        # Name resolution and unreachable-network both land here. Both mean we
        # did not connect, but the reason is worth keeping.
        return ProbeResult(host, port, Reachability.REFUSED,
                           (time.monotonic() - started) * 1000.0, time.time(),
                           detail=str(exc) or exc.__class__.__name__)
    return ProbeResult(host, port, Reachability.REACHABLE,
                       (time.monotonic() - started) * 1000.0, time.time())


class ReachabilityCache:
    """Short-lived memory of probe results.

    Deliberately tiny and in-process. It exists so a panel that repaints every
    few seconds is not opening a socket per port per repaint -- not to paper over
    a changed network, which is why the TTL is seconds and the results stay
    inspectable rather than being consumed.
    """

    def __init__(self, ttl: float = DEFAULT_CACHE_TTL) -> None:
        self._ttl = ttl
        self._entries: dict[tuple[str, int], ProbeResult] = {}

    @property
    def ttl(self) -> float:
        return self._ttl

    def get(self, host: str, port: int, now: float | None = None) -> ProbeResult | None:
        """A fresh-enough cached result, or None.

        ``ttl <= 0`` disables caching entirely. That is a real setting rather than
        a degenerate one -- a caller that wants every answer measured must be able
        to say so -- and it is why the comparison is ``>=``: with a zero TTL, two
        readings taken in the same clock tick would otherwise look identical and
        the second would be served from cache.
        """
        if self._ttl <= 0:
            return None
        entry = self._entries.get((host, port))
        if entry is None:
            return None
        current = time.time() if now is None else now
        if current - entry.checked_at >= self._ttl:
            return None
        return ProbeResult(entry.host, entry.port, entry.reachability, entry.latency_ms,
                           entry.checked_at, cached=True, detail=entry.detail)

    def put(self, result: ProbeResult) -> None:
        self._entries[(result.host, result.port)] = result

    def clear(self) -> None:
        self._entries.clear()

    def size(self) -> int:
        return len(self._entries)


class ReachabilityVerifier:
    """Measures reachability, with a cache in front of a prober.

    ``probe`` is injected so tests never touch a socket, and so a caller with a
    richer transport (an HTTP health endpoint, say) can substitute it without
    this class growing a second code path.
    """

    def __init__(self, prober: Optional[Prober] = None, *,
                 cache: Optional[ReachabilityCache] = None,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self._probe = prober or tcp_probe
        self._cache = cache if cache is not None else ReachabilityCache()
        self._timeout = timeout

    @property
    def cache(self) -> ReachabilityCache:
        return self._cache

    def check(self, host: str, port: int, *, timeout: float | None = None,
              use_cache: bool = True) -> ProbeResult:
        """Measure one host:port, reusing a recent result when there is one."""
        if use_cache:
            cached = self._cache.get(host, port)
            if cached is not None:
                return cached
        result = self._probe(host, port, timeout if timeout is not None else self._timeout)
        self._cache.put(result)
        return result

    def check_forward(self, forward: PortForward, **kwargs: Any) -> ProbeResult:
        """Measure a modelled port forward."""
        return self.check(forward.host, forward.port, **kwargs)

    async def check_async(self, host: str, port: int, **kwargs: Any) -> ProbeResult:
        """Measure off the calling thread.

        Every socket call in this module is blocking. Calling :meth:`check`
        straight from a Qt slot freezes the UI for up to ``timeout`` per port, so
        UI callers come through here and the thread does the waiting.
        """
        return await asyncio.to_thread(self.check, host, port, **kwargs)

    async def check_many_async(self, targets: Iterable[tuple[str, int]],
                               **kwargs: Any) -> list[ProbeResult]:
        """Measure several host:ports concurrently, bounded by ``timeout`` each."""
        pairs = list(targets)
        if not pairs:
            return []
        return list(await asyncio.gather(*(self.check_async(h, p, **kwargs) for h, p in pairs)))

    def verify_endpoint(self, endpoint: Endpoint, **kwargs: Any) -> Endpoint:
        """The endpoint with each forward's provenance upgraded to observed.

        Returns a new endpoint rather than mutating: a :class:`PortForward` is
        frozen on purpose (a forward that quietly rewrote its own provenance
        would make "what we modelled" unrecoverable once measured), and rebuilding
        means the caller keeps the pre-verification topology if they want to show
        the difference.
        """
        verified: list[PortForward] = []
        for forward in endpoint.ports:
            result = self.check(forward.host, forward.port, **kwargs)
            verified.append(PortForward(
                name=forward.name,
                host=forward.host,
                port=forward.port,
                purpose=forward.purpose,
                provenance=result.provenance,
                note=(f"connected in {result.latency_ms:.0f} ms" if result.reachable
                      else f"{result.reachability.value}: {result.detail}"),
            ))
        return dataclasses.replace(endpoint, ports=verified)

    def verify_topology(self, endpoints: list[Endpoint], **kwargs: Any) -> list[Endpoint]:
        """Verified copies of every endpoint's forwards.

        Sequential by design: a burst of parallel connects against a handful of
        local ports helps nobody and makes the per-port timing harder to read.
        """
        return [self.verify_endpoint(endpoint, **kwargs) for endpoint in endpoints]


# ── Forward discovery ────────────────────────────────────────────────────────
# What a VM or container actually exposes here. Every entry below is INFERRED
# until a probe confirms it: these come from configuration, not from a runtime.

DEFAULT_SSH_PORT = 22
DEFAULT_DOCKER_API_PORT = 2375


@dataclass(frozen=True)
class ForwardSpec:
    """One host-side forward, described without claiming it works."""

    name: str
    host: str
    port: int
    purpose: str
    note: str = ""

    def to_forward(self) -> PortForward:
        return PortForward(name=self.name, host=self.host, port=self.port,
                           purpose=self.purpose, provenance=Provenance.INFERRED,
                           note=self.note)


def vm_forwards(name: str, *, qmp_port: int = 0, ssh_port: int = 0,
                host: str = "127.0.0.1",
                display_port: int = 0) -> list[PortForward]:
    """The forwards a VM started by this harness exposes.

    QMP and SSH are the two this codebase actually opens; ``display_port`` is the
    VNC/SPICE port when one was allocated. A zero port means "none configured",
    which produces no forward at all rather than a forward to port 0 -- probing
    port 0 always fails and would look like a broken VM.
    """
    out: list[PortForward] = []
    if qmp_port:
        out.append(PortForward(name=f"{name}:qmp", host=host, port=int(qmp_port),
                               purpose="qmp", provenance=Provenance.INFERRED,
                               note="configured QMP socket"))
    if ssh_port:
        out.append(PortForward(name=f"{name}:ssh", host=host, port=int(ssh_port),
                               purpose="ssh", provenance=Provenance.INFERRED,
                               note="host forwarding to the guest's sshd"))
    if display_port:
        out.append(PortForward(name=f"{name}:display", host=host, port=int(display_port),
                               purpose="display", provenance=Provenance.INFERRED,
                               note="VNC/SPICE listener"))
    return out


def container_api_forward(host: str = "127.0.0.1",
                          port: int = DEFAULT_DOCKER_API_PORT,
                          *, name: str = "docker-api") -> PortForward:
    """The container engine's API socket.

    Note the default host. Docker Desktop's API is a named pipe
    (``npipe:////./pipe/docker_engine``) and is *not* listening on TCP at all, so
    a TCP probe of 127.0.0.1:2375 reports unreachable on a perfectly working
    install. That is why the note says so: the probe result is true and the
    interpretation is wrong, and only one of those should be hidden.
    """
    return PortForward(name=name, host=host, port=int(port), purpose="container-api",
                       provenance=Provenance.INFERRED,
                       note="Docker Desktop usually exposes a named pipe, not TCP; "
                            "a refused probe here does not mean Docker is down")


# ── Host layer-2 reality ─────────────────────────────────────────────────────

def layer2_capability() -> tuple[bool, str]:
    """Whether this host can put a container and a VM on one layer-2 segment.

    It cannot, and the reason is structural rather than a missing package:
    containers run under Docker Desktop (WSL2), VMs run under WHPX. There is no
    shared switch to join, and the helpers that could build one (Linux bridges,
    ``libvirt``, tap, OVS) do not exist on Windows. Returning ``False`` with the
    reason is the point -- pretending otherwise is how someone spends an
    afternoon on an ``iptables`` rule that can never work.
    """
    from vm_harness import env

    caps = env.host_capabilities()
    if caps.host.is_windows:
        return False, (
            "Windows host: WHPX guests and Docker Desktop (WSL2) containers have no "
            "shared layer-2 segment. Use host port forwarding, or put both on the "
            "same third-party network, to connect them."
        )
    if not caps.host.is_linux:
        return False, "Only Linux hosts have the bridge helpers this would need."
    return True, "Linux host: bridge helpers may be available (not verified here)."


async def verify_forwards_async(
    verifier: ReachabilityVerifier,
    forwards: Iterable[PortForward],
    **kwargs: Any,
) -> list[ProbeResult]:
    """Measure a set of forwards off-thread, concurrently."""
    pairs = [(f.host, f.port) for f in forwards]
    return await verifier.check_many_async(pairs, **kwargs)


__all__ = [
    "DEFAULT_CACHE_TTL",
    "DEFAULT_DOCKER_API_PORT",
    "DEFAULT_SSH_PORT",
    "DEFAULT_TIMEOUT",
    "ForwardSpec",
    "Prober",
    "ProbeResult",
    "Reachability",
    "ReachabilityCache",
    "ReachabilityVerifier",
    "container_api_forward",
    "layer2_capability",
    "verify_forwards_async",
    "vm_forwards",
]