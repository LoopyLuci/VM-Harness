"""Cluster config, health, placement -- and the bridge fix that started this.

The health tests are the important ones. The whole point of the package is that
silence is not failure: a node that stops heartbeating becomes ``unknown`` and
stays there, and the scheduler will not put work on it. A test that only checked
"healthy node gets picked" would pass just as happily against a scheduler that
treated every quiet node as a dead one and quietly re-placed its work.

Placement failure is tested as loudly as placement success, because the failure
mode this guards against -- falling back to some other node without saying so --
is invisible from the happy path.

No cluster, VM or node is required: the registry, tracker and scheduler all take
their clock and their observations as arguments.
"""
from __future__ import annotations

import time

import pytest
from PyQt5.QtCore import QObject, pyqtSignal

from vm_harness.cluster import (
    ClusterConfig,
    ConfigError,
    HealthPolicy,
    NodeHealth,
    NodeRegistry,
    NodeSpec,
    PlacementError,
    Scheduler,
    WorkRequest,
)
from vm_harness.cluster.scheduler import DEFAULT_FAILOVER, RetryPolicy


# ── fakes ─────────────────────────────────────────────────────────────────

class Clock:
    """A clock the test advances by hand.

    Staleness is the entire subject here, so nothing may read the wall clock
    implicitly -- otherwise a test would either sleep or be flaky.
    """

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


def node(node_id: str, *, slots: int = 4, caps: tuple[str, ...] = ("vm", "qmp"),
         host: str | None = None, **kwargs) -> NodeSpec:
    return NodeSpec(id=node_id, host=host or f"10.0.0.{node_id[-1] if node_id[-1].isdigit() else 1}",
                    capacity_slots=slots, capabilities=frozenset(caps), **kwargs)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def registry(clock) -> NodeRegistry:
    cfg = ClusterConfig(nodes=[node("lab-1"), node("lab-2")], local_node="lab-1")
    return NodeRegistry(cfg, clock=clock)


# ── the bridge bug: a lifecycle command must reach the intended VM ───────

class FakePerVMBridge(QObject):
    """Records what a MultiVMQMPBridge is asked to do, per VM.

    The bug this guards against was silent: the panel constructed
    ``MultiVMQMPBridge(vm_name, uri)`` against a signature of
    ``(mode="switch", parent=None)``, so the VM name landed in ``mode``, the URI
    landed in ``parent``, ``active_vm`` was never set, and Suspend / Stop / Reset /
    Resume quietly did nothing while the panel reported success. A test asserting
    "the button was clickable" would not have caught that; the assertion has to be
    that the command reached the named VM.

    A ``QObject`` with the same signals as the real bridge, because
    ``MultiVMQMPBridge`` forwards them and a plain object would break the
    construction under test rather than the behaviour.
    """

    connected = pyqtSignal(str, bool)
    vm_status = pyqtSignal(str, dict)
    error = pyqtSignal(str, str)
    command_result = pyqtSignal(str, dict)

    def __init__(self, vm_name: str, qmp_uri: str, password=None, parent=None):
        super().__init__(parent)
        self.vm_name = vm_name
        self.qmp_uri = qmp_uri
        self.commands: list[str] = []
        self._connected = True

    @property
    def is_connected(self) -> bool:
        """Reported connected, so the panel's stale-target guard is satisfied.

        The real bridge flips this from a background thread; here it is simply
        true, which is the state the panel requires before it will issue a
        run-state command.
        """
        return self._connected

    def start(self): pass
    def stop(self): pass
    def connect(self): pass
    def disconnect(self): pass

    def system_reset(self): self.commands.append("reset")
    def system_powerdown(self): self.commands.append("powerdown")
    def stop_vm(self): self.commands.append("stop")
    def cont(self): self.commands.append("cont")
    def eject_cdrom(self): self.commands.append("eject")

    def get_status(self):
        return {"state": "running"}


@pytest.fixture
def control_panel(qtbot, monkeypatch):
    """A VMControlPanel wired to a stub manager, with the QMP layer faked out.

    Only the QMP bridge is faked. The panel itself, its buttons and its handlers
    are the real ones -- replacing them would test the fake.
    """
    import gui.multi_vm_qmp_bridge as bridge_mod
    import gui.panels_vm_control as panel_mod
    from PyQt5.QtWidgets import QApplication

    if QApplication.instance() is None:
        QApplication([])

    monkeypatch.setattr(bridge_mod, "PerVMQMPBridge", FakePerVMBridge)

    class StubManager:
        def get_qmp_uri(self, name):
            return f"tcp://127.0.0.1:{4444 if name == 'alpha' else 5555}"

        def get_ssh_uri(self, name):
            return f"vmuser@127.0.0.1:{2222 if name == 'alpha' else 2322}"

        def get_vm(self, name):
            class Cfg:
                vm_name = name
                ram_mb = 2048
                cpus = 2
                disk_path = ""
                qmp_port = 4444
                ssh_port = 2222
            return Cfg()

        def get_status(self, name):
            return "running"

        def start_vm(self, name):
            return True, "started"

    panel = panel_mod.VMControlPanel()
    qtbot.addWidget(panel)
    panel.set_manager(StubManager())
    return panel


class TestBridgeConstructionFix:
    """A lifecycle command must reach the VM that was selected."""

    def test_the_bridge_is_built_in_switch_mode_not_with_a_vm_name(self, control_panel):
        """The original bug in one assertion.

        ``MultiVMQMPBridge(mode=...)`` took the VM name as its mode, so nothing was
        ever registered and ``active_vm`` stayed None.
        """
        control_panel.switch_to_vm("alpha")
        assert control_panel._multi_qmp is not None
        assert control_panel._multi_qmp.mode == "switch"
        assert control_panel._multi_qmp.active_vm == "alpha"

    def test_switching_suspends_the_selected_vm(self, control_panel):
        """Suspend must reach the VM named at press time."""
        control_panel.switch_to_vm("alpha")
        control_panel._on_suspend("alpha")
        bridge = control_panel._multi_qmp.get_bridge("alpha")
        assert bridge is not None
        assert "stop" in bridge.commands

    def test_suspend_reaches_the_right_vm_and_not_another(self, control_panel):
        """The failure this bug caused: a command aimed at nothing at all.

        Asserting the command landed on 'alpha' is only meaningful if 'beta' was in
        play and got nothing.
        """
        control_panel.switch_to_vm("alpha")
        control_panel.switch_to_vm("beta")
        control_panel._on_suspend("beta")

        assert control_panel._multi_qmp.get_bridge("alpha") is None, (
            "the panel kept a live bridge for a VM that is no longer selected"
        )
        beta_bridge = control_panel._multi_qmp.get_bridge("beta")
        assert beta_bridge is not None
        assert beta_bridge.commands == ["stop"]

    def test_stop_reaches_the_selected_vm(self, control_panel):
        control_panel.switch_to_vm("alpha")
        control_panel._on_stop()
        assert "powerdown" in control_panel._multi_qmp.get_bridge("alpha").commands

    def test_reset_reaches_the_selected_vm(self, control_panel):
        control_panel.switch_to_vm("alpha")
        control_panel._on_reset()
        assert "reset" in control_panel._multi_qmp.get_bridge("alpha").commands

    def test_resume_reaches_the_selected_vm(self, control_panel):
        control_panel.switch_to_vm("alpha")
        control_panel._on_resume("alpha")
        assert "cont" in control_panel._multi_qmp.get_bridge("alpha").commands

    def test_the_bridge_carries_the_selected_vm_uri(self, control_panel):
        """Two VMs, two URIs: the wrong one being used would talk to the wrong VM."""
        control_panel.switch_to_vm("alpha")
        assert control_panel._multi_qmp.get_bridge("alpha").qmp_uri.endswith("4444")
        control_panel.switch_to_vm("beta")
        assert control_panel._multi_qmp.get_bridge("beta").qmp_uri.endswith("5555")

    def test_a_selection_made_after_the_click_is_refused_not_re_pointed(self, control_panel):
        """The stale-target guard: refuse rather than guess.

        Retargeting silently is what made the original failure hard to see, so the
        refusal is asserted here too.
        """
        control_panel.switch_to_vm("alpha")
        control_panel.switch_to_vm("beta")
        control_panel._on_suspend("alpha")  # pressed while alpha was active
        assert control_panel._multi_qmp.get_bridge("beta").commands == []
        assert "refused" in control_panel.info_label.text()


# ── cluster config ────────────────────────────────────────────────────────

class TestClusterConfig:
    def test_config_round_trips_through_disk(self, tmp_path):
        cfg = ClusterConfig(nodes=[node("lab-1", caps=("vm", "qmp"))], local_node="lab-1")
        path = cfg.save(tmp_path / "cluster.json")
        loaded = ClusterConfig.load(path)
        assert [n.id for n in loaded.nodes] == ["lab-1"]
        assert loaded.local_node == "lab-1"
        assert loaded.nodes[0].has("qmp")

    def test_a_missing_file_is_an_empty_cluster_not_an_error(self, tmp_path):
        """A single-node install has no reason to have written a file."""
        assert ClusterConfig.load(tmp_path / "nope.json").nodes == []

    def test_a_corrupt_file_raises_rather_than_looking_empty(self, tmp_path):
        """Silently starting with no nodes looks exactly like losing the cluster."""
        path = tmp_path / "cluster.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ConfigError):
            ClusterConfig.load(path)

    def test_a_duplicate_node_id_is_rejected(self, tmp_path):
        """Two entries with one id would make every lookup by id ambiguous."""
        path = tmp_path / "cluster.json"
        path.write_text(
            '{"version":1,"nodes":[{"id":"a","host":"h"},{"id":"a","host":"h2"}]}',
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="duplicate"):
            ClusterConfig.load(path)

    def test_a_negative_capacity_is_rejected_on_load(self, tmp_path):
        path = tmp_path / "cluster.json"
        path.write_text('{"version":1,"nodes":[{"id":"a","host":"h","capacity_slots":-1}]}',
                        encoding="utf-8")
        with pytest.raises(ConfigError, match="negative"):
            ClusterConfig.load(path)

    def test_saving_leaves_no_temp_file_behind(self, tmp_path):
        """The temp file is how the write is made atomic; leaving it would confuse
        the next load."""
        cfg = ClusterConfig(nodes=[node("lab-1")])
        path = cfg.save(tmp_path / "cluster.json")
        assert list(tmp_path.iterdir()) == [path]

    def test_adding_the_same_node_twice_replaces_it(self):
        cfg = ClusterConfig()
        cfg.add(node("lab-1", host="10.0.0.1"))
        cfg.add(node("lab-1", host="10.0.0.9"))
        assert len(cfg.nodes) == 1
        assert cfg.nodes[0].host == "10.0.0.9"

    def test_zero_capacity_means_unknown_not_unlimited(self):
        """A capacity nobody set is not an infinite amount of capacity."""
        assert not node("lab-1", slots=0).capacity_known()

    def test_health_thresholds_must_be_ordered(self):
        """An unknown_after below healthy_after would declare a heartbeating node
        unknowable, which is a nonsense state the config should not allow."""
        with pytest.raises(ValueError):
            HealthPolicy(healthy_after=60.0, unknown_after=10.0)


# ── health: silence is not down ──────────────────────────────────────────

class TestHealth:
    def test_a_node_that_never_spoke_is_unknown_not_down(self, registry):
        """The single most important assertion in this file.

        Configuring a node does not make it exist. Reporting it as down would send
        an operator to fix a machine that is probably fine.
        """
        assert registry.health_tracker.health_of("lab-1", now=1000.0) is NodeHealth.UNKNOWN

    def test_a_fresh_heartbeat_is_healthy(self, clock, registry):
        registry.record_heartbeat("lab-1", reported_slots=4, at=clock.now)
        assert registry.health_tracker.health_of("lab-1", now=clock.now) is NodeHealth.HEALTHY

    def test_a_silent_node_goes_stale_before_it_goes_unknown(self, clock, registry):
        """Stale is the intermediate state: still heard from once, quiet now."""
        registry.record_heartbeat("lab-1", reported_slots=4, at=clock.now)
        clock.advance(20.0)
        assert registry.health_tracker.health_of("lab-1", now=clock.now) is NodeHealth.STALE

    def test_a_long_silent_node_becomes_unknown_never_down(self, clock, registry):
        """Silence for over unknown_after is 'lost track of', not 'failed'.

        A frozen process, a severed network and a paused node are indistinguishable
        from silence, so this must not collapse to DOWN.
        """
        registry.record_heartbeat("lab-1", at=clock.now)
        clock.advance(120.0)
        assert registry.health_tracker.health_of("lab-1", now=clock.now) is NodeHealth.UNKNOWN

    def test_an_observed_failure_after_a_heartbeat_is_down(self, clock, registry):
        """DOWN is reserved for a node we reached for and did not hear.

        That is a much stronger claim than 'no news', and it is the only one worth
        alerting on.
        """
        registry.record_heartbeat("lab-1", at=clock.now)
        clock.advance(1.0)
        registry.record_failure("lab-1", "connection refused", at=clock.now)
        assert registry.health_tracker.health_of("lab-1", now=clock.now) is NodeHealth.DOWN

    def test_a_heartbeat_clears_an_outstanding_failure(self, clock, registry):
        """Otherwise a recovered node stays down forever behind a stale record."""
        registry.record_heartbeat("lab-1", at=clock.now)
        registry.record_failure("lab-1", "refused", at=clock.now)
        clock.advance(1.0)
        registry.record_heartbeat("lab-1", at=clock.now)
        assert registry.health_tracker.health_of("lab-1", now=clock.now) is NodeHealth.HEALTHY

    def test_a_missed_heartbeat_is_not_a_failure(self, clock, registry):
        """note_missed counts silence. It must never manufacture a DOWN."""
        registry.record_heartbeat("lab-1", at=clock.now)
        for _ in range(20):
            registry.note_missed("lab-1")
        assert registry.health_tracker.health_of("lab-1", now=clock.now) is NodeHealth.HEALTHY

    def test_health_is_recomputed_on_read_not_stored_stale(self, clock, registry):
        """No background timer means no interval at which the answer is wrong."""
        registry.record_heartbeat("lab-1", at=clock.now)
        assert registry.health_of("lab-1", now=clock.now) is NodeHealth.HEALTHY
        clock.advance(200.0)
        assert registry.health_of("lab-1", now=clock.now) is NodeHealth.UNKNOWN

    def test_unknown_nodes_are_listed_separately(self, clock, registry):
        registry.record_heartbeat("lab-1", at=clock.now)
        assert registry.unknown(now=clock.now) == ["lab-2"]

    def test_a_heartbeat_from_an_unknown_node_is_refused(self, registry):
        """Otherwise a misconfigured agent can add itself to the cluster."""
        with pytest.raises(KeyError):
            registry.record_heartbeat("rogue", reported_slots=99)

    def test_status_counts_every_state(self, clock, registry):
        registry.record_heartbeat("lab-1", at=clock.now)
        registry.record_heartbeat("lab-2", at=clock.now)
        counts = registry.to_dict(now=clock.now)["counts"]
        assert counts["healthy"] == 2
        assert counts["unknown"] == 0

    def test_reported_capacity_shrinks_as_work_is_assigned(self, clock, registry):
        registry.record_heartbeat("lab-1", reported_slots=4, at=clock.now)
        assert registry.entry("lab-1").observed_capacity == 4
        registry.assign_slots("lab-1", 2)
        assert registry.entry("lab-1").observed_capacity == 2

    def test_capacity_cannot_be_over_assigned(self, clock, registry):
        """Promising capacity the node never offered is worse than refusing."""
        registry.record_heartbeat("lab-1", reported_slots=2, at=clock.now)
        with pytest.raises(Exception):
            registry.assign_slots("lab-1", 5)


# ── the scheduler: picks a capable node, or fails loudly ─────────────────

class TestScheduler:
    def test_it_places_on_a_healthy_capable_node(self, clock, registry):
        registry.record_heartbeat("lab-1", reported_slots=4, at=clock.now)
        registry.record_heartbeat("lab-2", reported_slots=4, at=clock.now)
        placement = Scheduler(registry).place(
            WorkRequest(name="build", capabilities={"vm"}, slots=1), now=clock.now)
        # lab-1 and lab-2 are equivalent, so the tie-break decides: lowest id first.
        assert placement.node == "lab-1"
        assert placement.host == registry.entry("lab-1").spec.host

    def test_it_picks_the_node_with_the_most_free_capacity(self, clock, registry):
        registry.record_heartbeat("lab-1", reported_slots=2, at=clock.now)
        registry.record_heartbeat("lab-2", reported_slots=8, at=clock.now)
        placement = Scheduler(registry).place(
            WorkRequest(capabilities={"vm"}, slots=2), now=clock.now)
        assert placement.node == "lab-2"

    def test_it_skips_a_node_missing_the_capability(self, clock, registry):
        registry.add_node(node("lab-3", caps=("container",)))
        for nid in ("lab-1", "lab-2", "lab-3"):
            registry.record_heartbeat(nid, reported_slots=8, at=clock.now)
        placement = Scheduler(registry).place(
            WorkRequest(capabilities={"vm"}), now=clock.now)
        assert placement.node in ("lab-1", "lab-2")
        assert "missing capability" in placement.considered["lab-3"]

    def test_it_refuses_a_node_it_has_never_heard_from(self, clock, registry):
        """An unknown node has unknown capacity, which is not the same as empty."""
        registry.record_heartbeat("lab-1", reported_slots=4, at=clock.now)
        placement = Scheduler(registry).place(WorkRequest(capabilities={"vm"}), now=clock.now)
        assert placement.node == "lab-1"
        assert "never sent a heartbeat" in placement.considered["lab-2"]

    def test_it_refuses_a_node_that_went_quiet(self, clock, registry):
        registry.record_heartbeat("lab-1", at=clock.now)
        registry.record_heartbeat("lab-2", reported_slots=8, at=clock.now)
        clock.advance(120.0)  # both fall silent
        with pytest.raises(PlacementError) as exc:
            Scheduler(registry).place(WorkRequest(capabilities={"vm"}), now=clock.now)
        assert "unknown, not down" in exc.value.describe()

    def test_it_fails_loudly_when_nothing_fits(self, clock, registry):
        """No capability anywhere: refuse, and say why for every node.

        A silent fallback here would put the work on a machine the user did not
        choose, and they would find out when they went looking for it.
        """
        for nid in ("lab-1", "lab-2"):
            registry.record_heartbeat(nid, reported_slots=8, at=clock.now)
        with pytest.raises(PlacementError) as exc:
            Scheduler(registry).place(
                WorkRequest(name="k8s-job", capabilities={"k8s"}), now=clock.now)
        described = exc.value.describe()
        assert "lab-1" in described and "lab-2" in described
        assert "missing capability: k8s" in described

    def test_it_does_not_fall_back_to_another_node_when_the_pinned_one_cannot(self, clock, registry):
        """A pin is a constraint, not a hint.

        lab-2 is pinned and cannot take the work while lab-1 can. Falling back to
        lab-1 would run the work somewhere the caller did not choose, so this fails.
        """
        registry.add_node(node("lab-2", caps=("container",)))
        for nid in ("lab-1", "lab-2"):
            registry.record_heartbeat(nid, reported_slots=8, at=clock.now)
        with pytest.raises(PlacementError) as exc:
            Scheduler(registry).place(
                WorkRequest(capabilities={"vm"}, pin_node="lab-2"), now=clock.now)
        assert "pinned" in str(exc.value)
        assert "missing capability" in exc.value.reasons["lab-2"]

    def test_it_respects_a_capable_pin(self, clock, registry):
        registry.record_heartbeat("lab-2", reported_slots=8, at=clock.now)
        placement = Scheduler(registry).place(
            WorkRequest(capabilities={"vm"}, pin_node="lab-2"), now=clock.now)
        assert placement.node == "lab-2"

    def test_an_empty_registry_fails_with_a_clear_message(self, clock):
        empty = NodeRegistry(ClusterConfig(), clock=clock)
        with pytest.raises(PlacementError, match="no configured nodes"):
            Scheduler(empty).place(WorkRequest(capabilities={"vm"}), now=clock.now)

    def test_placing_alone_does_not_consume_capacity(self, clock, registry):
        """Asking where work would go must not quietly reserve it."""
        for nid in ("lab-1", "lab-2"):
            registry.record_heartbeat(nid, reported_slots=4, at=clock.now)
        sched = Scheduler(registry)
        first = sched.place(WorkRequest(capabilities={"vm"}), now=clock.now)
        second = sched.place(WorkRequest(capabilities={"vm"}), now=clock.now)
        assert first.node == second.node == "lab-1"
        assert registry.entry("lab-1").assigned_slots == 0

    def test_committing_reserves_the_slots(self, clock, registry):
        for nid in ("lab-1", "lab-2"):
            registry.record_heartbeat(nid, reported_slots=4, at=clock.now)
        sched = Scheduler(registry)
        placement = sched.place(WorkRequest(capabilities={"vm"}, slots=2), now=clock.now)
        sched.commit(placement)
        assert registry.entry(placement.node).assigned_slots == 2

    def test_considered_records_every_node_it_looked_at(self, clock, registry):
        """The audit trail: a placement should be explainable after the fact."""
        registry.record_heartbeat("lab-1", reported_slots=4, at=clock.now)
        placement = Scheduler(registry).place(WorkRequest(capabilities={"vm"}), now=clock.now)
        assert set(placement.considered) == {"lab-1", "lab-2"}

    def test_a_work_request_needs_at_least_one_slot(self):
        with pytest.raises(ValueError):
            WorkRequest(slots=0)

    def test_label_constraints_are_honoured(self, clock, registry):
        registry.add_node(node("lab-3", labels={"gpu": "true"}))
        for nid in ("lab-1", "lab-2", "lab-3"):
            registry.record_heartbeat(nid, reported_slots=8, at=clock.now)
        placement = Scheduler(registry).place(
            WorkRequest(capabilities={"vm"}, labels={"gpu": "true"}), now=clock.now)
        assert placement.node == "lab-3"


# ── failover semantics, stated as data ───────────────────────────────────

class TestFailoverPolicy:
    def test_a_decision_is_safe_to_repeat(self):
        assert DEFAULT_FAILOVER.may_retry("placement")

    def test_a_lifecycle_command_may_duplicate(self):
        """The first attempt may have succeeded before the node went away, so the
        caller has to be told rather than quietly handed a retry."""
        assert DEFAULT_FAILOVER.policy_for("vm.start") is RetryPolicy.MAY_DUPLICATE

    def test_destructive_work_is_never_retried(self):
        """A node vanishing is exactly when a blind retry does the most damage."""
        for op in ("vm.delete", "file.write", "guest.exec", "container.remove"):
            assert DEFAULT_FAILOVER.policy_for(op) is RetryPolicy.UNSAFE
            assert not DEFAULT_FAILOVER.may_retry(op)

    def test_an_unknown_operation_falls_back_to_the_default(self):
        assert DEFAULT_FAILOVER.policy_for("something.new") is RetryPolicy.SAFE

    def test_what_is_lost_is_stated(self):
        assert any("lost" in note.lower() for note in DEFAULT_FAILOVER.loss_notes)