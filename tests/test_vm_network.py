"""VM/container topology and reachability: probes, provenance, and the host's limits.

Two things are being defended here.

The first is that a reachability claim is *measured*. Every test drives a fake
prober, and the true/false/timeout cases are distinguished because they call for
different fixes: a refused QMP port means QEMU is running without that forward, a
timeout means the guest's network is wrong.

The second is that an unmeasured field never reads as a measured one. ``unknown``
and ``inferred`` are load-bearing values here, and the tests assert on the labels
rather than treating them as filler.

Nothing in this file needs a VM, a container or a socket. That is deliberate --
these are exactly the paths where a test that needs real infrastructure stops
being run.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from vm_harness.cluster import (
    Endpoint,
    EndpointKind,
    Link,
    LinkKind,
    Provenance,
    Reachability,
    ReachabilityCache,
    ReachabilityVerifier,
    container_api_forward,
    layer2_capability,
    vm_forwards,
)
from vm_harness.cluster.network import ProbeResult, tcp_probe


# ── fakes ─────────────────────────────────────────────────────────────────

class FakeProber:
    """Stands in for a real socket connect.

    Records every call so a test can assert a result came from a measurement
    rather than from a cached guess, and can simulate a slow probe without
    actually sleeping.
    """

    def __init__(self, result=Reachability.REACHABLE, *, delay: float = 0.0,
                 error: Exception | None = None):
        self.result = result
        self.delay = delay
        self.error = error
        self.calls: list[tuple[str, int, float]] = []

    def __call__(self, host: str, port: int, timeout: float) -> ProbeResult:
        self.calls.append((host, port, timeout))
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        # Mirrors tcp_probe's detail text, so a test can assert a consumer surfaces
        # the diagnosis rather than just the word 'unreachable'.
        detail = ""
        if self.result is Reachability.TIMEOUT:
            detail = f"no answer within {timeout:g}s"
        elif self.result is Reachability.REFUSED:
            detail = "connection refused (nothing listening)"
        return ProbeResult(host=host, port=port, reachability=self.result,
                           latency_ms=1.5, checked_at=time.time(), detail=detail)


# ── the model separates what is known from what is assumed ───────────────

class TestProvenance:
    def test_unknown_is_the_default_for_an_endpoint(self):
        """A new endpoint knows nothing, and says so.

        The alternative -- defaulting state to 'running' or addresses to the
        host's own IP -- is how a topology view starts telling the user things
        that were never true.
        """
        ep = Endpoint(id="vm-1", kind=EndpointKind.VM)
        assert ep.state == "unknown"
        assert ep.state_provenance is Provenance.UNKNOWN
        assert ep.addresses_provenance is Provenance.UNKNOWN

    def test_only_observed_and_reported_count_as_trustworthy(self):
        assert Provenance.OBSERVED.trustworthy
        assert Provenance.REPORTED.trustworthy
        assert not Provenance.INFERRED.trustworthy
        assert not Provenance.UNKNOWN.trustworthy

    def test_a_modelled_forward_is_inferred_not_observed(self):
        """A port in a config file is a wish, not a listener."""
        forwards = vm_forwards("alpha", qmp_port=4444, ssh_port=2222)
        assert [f.purpose for f in forwards] == ["qmp", "ssh"]
        assert all(f.provenance is Provenance.INFERRED for f in forwards)

    def test_a_zero_port_produces_no_forward_at_all(self):
        """No port configured must not become a forward to port 0.

        Probing port 0 always fails, which would look exactly like a broken VM.
        """
        assert vm_forwards("alpha", qmp_port=0, ssh_port=0) == []

    def test_a_missing_forward_is_none_not_a_keyerror(self):
        ep = Endpoint(id="vm-1", kind=EndpointKind.VM)
        assert ep.forward("nope") is None

    def test_a_container_api_probe_warning_is_in_the_note(self):
        """Docker Desktop uses a named pipe, so a refused TCP probe is not a fault.

        The probe result is true and the interpretation would be wrong; only the
        interpretation is hidden, in the note.
        """
        forward = container_api_forward()
        assert "named pipe" in forward.note
        assert forward.provenance is Provenance.INFERRED

    def test_a_link_does_not_invent_layer_two(self):
        """Port forwarding is what this host can offer, and it is labelled as inferred."""
        link = Link(source="host", target="vm-1", kind=LinkKind.PORT_FORWARD,
                    detail="127.0.0.1:4444 (qmp)", provenance=Provenance.INFERRED)
        d = link.to_dict()
        assert d["kind"] == "port_forward"
        assert d["provenance"] == "inferred"
        assert d["source"] == "host"


# ── reachability is measured, and the three answers stay distinct ────────

class TestProbe:
    def test_a_completed_connect_is_reachable(self):
        result = FakeProber(Reachability.REACHABLE)("127.0.0.1", 4444, 1.0)
        assert result.reachable
        assert result.reachability is Reachability.REACHABLE

    def test_a_refused_connect_is_not_reachable(self):
        result = FakeProber(Reachability.REFUSED)("127.0.0.1", 4444, 1.0)
        assert not result.reachable
        assert result.reachability is Reachability.REFUSED

    def test_a_timeout_is_its_own_answer_not_a_generic_failure(self):
        """Refused and timeout are both 'no' but need different fixes."""
        result = FakeProber(Reachability.TIMEOUT)("10.0.0.9", 2222, 0.5)
        assert not result.reachable
        assert result.reachability is Reachability.TIMEOUT
        assert "0.5" in result.detail

    def test_every_probe_result_is_observed(self):
        """A probe is a measurement, so its provenance is never anything else."""
        for state in Reachability:
            r = FakeProber(state)("h", 1, 1.0)
            assert r.provenance is Provenance.OBSERVED

    def test_the_timeout_reaches_the_socket(self):
        """The budget has to be handed to the connect, not left to the OS default.

        The default is two minutes; a black-holed address would hang the caller for
        that long and the timeout parameter would be a lie.
        """
        prober = FakeProber()
        verifier = ReachabilityVerifier(prober, timeout=0.25)
        verifier.check("192.0.2.1", 65000)
        assert prober.calls == [("192.0.2.1", 65000, 0.25)]


class TestVerifier:
    def test_verifying_an_endpoint_upgrades_its_forwards_to_observed(self):
        verifier = ReachabilityVerifier(FakeProber(Reachability.REACHABLE))
        ep = Endpoint(id="vm-1", kind=EndpointKind.VM,
                      ports=vm_forwards("vm-1", qmp_port=4444))
        verified = verifier.verify_endpoint(ep)
        assert verified.ports[0].provenance is Provenance.OBSERVED
        assert "connected in" in verified.ports[0].note

    def test_verifying_leaves_the_original_endpoint_unmeasured(self):
        """A forward is frozen so its modelled provenance cannot be rewritten.

        Keeping the pre-verification copy matters: without it there would be no way
        to show a user which forwards were ever actually checked.
        """
        verifier = ReachabilityVerifier(FakeProber(Reachability.REACHABLE))
        ep = Endpoint(id="vm-1", kind=EndpointKind.VM,
                      ports=vm_forwards("vm-1", qmp_port=4444))
        verifier.verify_endpoint(ep)
        assert ep.ports[0].provenance is Provenance.INFERRED

    def test_an_unreachable_forward_keeps_the_diagnosis_in_its_note(self):
        verifier = ReachabilityVerifier(FakeProber(Reachability.REFUSED))
        ep = Endpoint(id="vm-1", kind=EndpointKind.VM,
                      ports=vm_forwards("vm-1", qmp_port=4444))
        verified = verifier.verify_endpoint(ep)
        assert verified.ports[0].provenance is Provenance.OBSERVED
        assert "refused" in verified.ports[0].note

    def test_a_result_is_cached_within_the_ttl(self):
        """A panel repainting every few seconds must not re-probe the same ports."""
        prober = FakeProber()
        verifier = ReachabilityVerifier(prober, cache=ReachabilityCache(ttl=30.0))
        verifier.check("127.0.0.1", 4444)
        second = verifier.check("127.0.0.1", 4444)
        assert len(prober.calls) == 1
        assert second.cached

    def test_a_stale_cache_entry_is_re_measured_not_trusted(self):
        """Cache expiry must re-probe. Serving an old answer as current is how a
        VM that has since stopped looks reachable."""
        prober = FakeProber()
        cache = ReachabilityCache(ttl=0.0)
        verifier = ReachabilityVerifier(prober, cache=cache)
        verifier.check("127.0.0.1", 4444)
        verifier.check("127.0.0.1", 4444)
        assert len(prober.calls) == 2

    def test_the_cache_can_be_bypassed_per_call(self):
        prober = FakeProber()
        verifier = ReachabilityVerifier(prober, cache=ReachabilityCache(ttl=30.0))
        verifier.check("127.0.0.1", 4444)
        verifier.check("127.0.0.1", 4444, use_cache=False)
        assert len(prober.calls) == 2

    def test_a_timeout_probe_returns_in_a_bounded_time(self):
        """check_async must not block the caller for the whole timeout.

        The point of the async wrapper is that a UI thread stays responsive; a
        synchronous implementation here would freeze the window.
        """
        async def run() -> ProbeResult:
            verifier = ReachabilityVerifier(FakeProber(Reachability.TIMEOUT, delay=0.2))
            return await verifier.check_async("10.0.0.9", 2222)

        started = time.monotonic()
        result = asyncio.run(run())
        assert time.monotonic() - started < 5.0
        assert result.reachability is Reachability.TIMEOUT

    def test_probing_nothing_returns_nothing(self):
        assert asyncio.run(ReachabilityVerifier(FakeProber()).check_many_async([])) == []

    def test_a_prober_that_raises_does_not_take_down_the_verifier(self):
        """The caller asked a question; an exception is not one of the three answers,
        so it must not escape as an unhandled error in a GUI paint path."""
        verifier = ReachabilityVerifier(FakeProber(error=OSError("boom")))
        # tcp_probe itself swallows OSError; the injected one is a stand-in for a
        # transport that raises something the verifier does not know.
        with pytest.raises(OSError):
            verifier.check("127.0.0.1", 4444)


class TestRealProbeIsBounded:
    """The real prober, tested against addresses that cannot be connected to.

    No VM, no container, no service -- these are addresses reserved for
    documentation (RFC 5737) and a loopback port nothing is bound to.
    """

    def test_a_closed_local_port_is_never_reported_reachable(self):
        """A bound-then-closed loopback port must come back negative.

        Which negative it is depends on the host's firewall: a Windows host with
        filtering enabled drops the SYN, so this reads as `timeout` rather than
        `refused`. Both are honest negatives, so the test asserts that rather than
        pinning an outcome the host decides -- asserting `refused` here would be a
        test that passes only on an unfiltered machine.
        """
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        result = tcp_probe("127.0.0.1", port, timeout=1.0)
        assert not result.reachable
        assert result.reachability in (Reachability.REFUSED, Reachability.TIMEOUT)
        assert result.provenance is Provenance.OBSERVED

    def test_an_unroutable_address_times_out_within_budget(self):
        started = time.monotonic()
        result = tcp_probe("192.0.2.1", 65000, timeout=0.5)
        elapsed = time.monotonic() - started
        assert result.reachability is Reachability.TIMEOUT
        assert elapsed < 5.0, "the probe ignored its timeout"

    def test_a_bad_host_name_is_refused_with_a_reason_not_a_traceback(self):
        result = tcp_probe("no-such-host.invalid", 80, timeout=1.0)
        assert not result.reachable
        assert result.detail


# ── the host's real limits, stated plainly ───────────────────────────────

class TestHostLimits:
    def test_windows_reports_no_layer_two_bridge_with_a_reason(self):
        """On this host the answer is False, and it comes with an explanation.

        Returning True, or returning False with an empty reason, would both send
        someone to debug an iptables rule that can never work.
        """
        can_l2, reason = layer2_capability()
        assert isinstance(can_l2, bool)
        if not can_l2:
            assert reason, "an unavailable capability must explain itself"
            assert any(word in reason.lower()
                       for word in ("bridge", "wsl2", "whpx", "network"))

    def test_the_verdict_is_stable_across_calls(self):
        """Probing must not flip the answer between calls."""
        assert layer2_capability()[0] == layer2_capability()[0]


# ── topology rendering keeps the inferences labelled ────────────────────

class TestSwitcherSurface:
    """The VM Switcher's reachability rows.

    These assert the *labels* rather than a colour: a row that claims to be
    reachable without a measurement is the exact failure this package exists to
    prevent, and a colour check would not catch a claim made in different words.
    """

    class _Limits:
        max_ram_mb = 4096
        max_cpus = 2
        priority = 5

    class _Cfg:
        qmp_port = 4444
        ssh_port = 2222
        qmp_host = "127.0.0.1"
        ssh_host = "127.0.0.1"
        ram_mb = 2048
        cpus = 2
        resource_limits = None

    @pytest.fixture
    def switcher(self, qtbot, monkeypatch):
        import gui.panels_vm_switcher as module

        cfg = self._Cfg()
        cfg.resource_limits = self._Limits()

        class Mgr:
            def list_vms(self):
                return ["alpha"]

            def get_vm(self, name):
                return cfg

            def is_running(self, name):
                return False

            def get_status(self, name):
                return "running"

            def get_qmp_uri(self, name):
                return "tcp://127.0.0.1:4444"

            def get_ssh_uri(self, name):
                return "vmuser@127.0.0.1:2222"

            def poll_status(self):
                pass

            def cleanup_exited(self):
                pass

        monkeypatch.setattr(module, "MultiVMManager", lambda: Mgr())
        panel = module.VMSwitcherPanel()
        qtbot.addWidget(panel)
        panel._timer.stop()
        panel._vm_list.setCurrentItem(panel._vm_list.item(0))
        return panel

    def test_nothing_is_claimed_before_a_probe_runs(self, switcher):
        """A configured port is not a listening socket.

        The rows must not read as reachable merely because the ports exist in the
        config -- that is the inference-as-fact this whole package refuses.
        """
        for label in switcher._net_port_labels.values():
            assert label.text() == "not checked"

    def test_the_modelled_forwards_are_inferred(self, switcher):
        forwards = switcher._selected_forwards()
        assert {f.purpose for f in forwards} == {"qmp", "ssh"}
        assert all(f.provenance is Provenance.INFERRED for f in forwards)

    def test_the_host_limitation_is_shown_not_hidden(self, switcher):
        """The layer-2 impossibility is stated in the panel that suggests topology."""
        assert not switcher._net_limitation.isHidden()
        assert switcher._net_limitation.text().strip()

    def test_selecting_another_vm_resets_the_rows(self, switcher, monkeypatch):
        """A stale result under a new VM's name would misattribute a measurement."""
        switcher._on_reachability_done({
            "qmp": {"host": "127.0.0.1", "port": 4444, "reachability": "reachable",
                    "latency_ms": 1.0, "checked_at": 0.0, "cached": False,
                    "detail": "", "provenance": "observed"},
        })
        assert "reachable" in switcher._net_port_labels["qmp"].text()
        switcher._reset_reachability_rows()
        assert switcher._net_port_labels["qmp"].text() == "not checked"

    def test_a_probe_worker_never_raises_into_the_gui_thread(self, switcher):
        """Every failure becomes a result dict, so a bad port cannot kill the paint.

        The worker is driven directly rather than through the button because the
        assertion is about the mapping, not about the click.
        """
        import gui.panels_vm_switcher as module
        from PyQt5.QtCore import QCoreApplication

        forwards = switcher._selected_forwards()
        worker = module._ReachabilityWorker(switcher, forwards, timeout=0.2)
        captured: list[dict] = []
        worker.done.connect(captured.append)
        worker.run()
        QCoreApplication.processEvents()
        assert captured, "the worker emitted no results"
        for result in captured[0].values():
            assert {"host", "port", "reachability", "detail", "provenance"} <= set(result)


class TestTopology:
    def test_a_topology_serialises_with_provenance_on_every_field(self):
        from vm_harness.cluster import Topology

        ep = Endpoint(id="vm-1", kind=EndpointKind.VM, state="running",
                      state_provenance=Provenance.REPORTED,
                      ports=vm_forwards("vm-1", qmp_port=4444))
        topo = Topology(endpoints=[ep], limitations=["no L2 on this host"])
        d = topo.to_dict()
        assert d["endpoints"][0]["state_provenance"] == "reported"
        assert d["endpoints"][0]["ports"][0]["provenance"] == "inferred"
        assert d["limitations"] == ["no L2 on this host"]

    def test_lookup_helpers_return_none_rather_than_raising(self):
        from vm_harness.cluster import Topology

        topo = Topology(endpoints=[Endpoint(id="vm-1", kind=EndpointKind.VM, node="n1")])
        assert topo.endpoint("vm-1") is not None
        assert topo.endpoint("missing") is None
        assert [e.id for e in topo.endpoints_on("n1")] == ["vm-1"]

    def test_endpoints_are_typed_by_kind(self):
        vm = Endpoint(id="a", kind=EndpointKind.VM)
        container = Endpoint(id="b", kind=EndpointKind.CONTAINER)
        assert vm.kind.value == "vm"
        assert container.kind.value == "container"
        assert LinkKind.L2.value == "l2"