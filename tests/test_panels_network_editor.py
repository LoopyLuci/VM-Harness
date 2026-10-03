"""Tests for Network Config Editor panel.

Run with: pytest tests/test_panels_network_editor.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Setup paths
PROJECT_DIR = Path(__file__).resolve().parent.parent
GUI_DIR = PROJECT_DIR / "gui"
SRC_DIR = PROJECT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(PROJECT_DIR))

# Ensure offscreen for headless testing
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt5.QtWidgets import QApplication

from gui.panels_network_editor import (
    NetworkConfigEditor,
    TopologyDiagram,
    PortForwardDialog,
    generate_mac,
    validate_mac,
    NETWORK_MODES,
    ADAPTER_TYPES,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def app():
    """Create QApplication for tests."""
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def editor(app):
    """Create a fresh NetworkConfigEditor instance."""
    editor = NetworkConfigEditor()
    editor.show()
    yield editor
    editor.close()


# ── Test Category 1: Initialization ─────────────────────────────────────────

class TestEditorInit:
    """Test that the editor initializes correctly."""

    def test_editor_instantiates(self, editor):
        """Editor should create without error."""
        assert editor is not None

    def test_editor_is_widget(self, editor):
        """Editor should be a QWidget."""
        from PyQt5.QtWidgets import QWidget
        assert isinstance(editor, QWidget)

    def test_default_mode_is_nat(self, editor):
        """Default mode should be NAT."""
        assert editor._mode_combo.currentText() == "NAT"

    def test_default_adapter_is_virtio(self, editor):
        """Default adapter should be virtio-net-pci."""
        assert editor._adapter_combo.currentText() == "virtio-net-pci"

    def test_default_adapter_count_is_1(self, editor):
        """Default adapter count should be 1."""
        assert editor._adapter_count.value() == 1

    def test_default_port_forwards_empty(self, editor):
        """No port forwards by default."""
        assert len(editor._port_forwards) == 0

    def test_default_bandwidth_disabled(self, editor):
        """Bandwidth limiting should be disabled by default."""
        assert not editor._bw_enable.isChecked()

    def test_default_bandwidth_zero(self, editor):
        """Bandwidth values should be 0 by default."""
        assert editor._bw_inbound.value() == 0
        assert editor._bw_outbound.value() == 0

    def test_default_mac_is_valid(self, editor):
        """Default MAC address should be valid."""
        assert validate_mac(editor._mac_input.text())

    def test_mode_combo_has_all_modes(self, editor):
        """Mode combo should have all 4 network modes."""
        items = [editor._mode_combo.itemText(i) for i in range(editor._mode_combo.count())]
        assert items == NETWORK_MODES

    def test_adapter_combo_has_all_types(self, editor):
        """Adapter combo should have all adapter types."""
        items = [editor._adapter_combo.itemText(i) for i in range(editor._adapter_combo.count())]
        assert items == ADAPTER_TYPES


# ── Test Category 2: Network Mode ───────────────────────────────────────────

class TestNetworkMode:
    """Test network mode selection."""

    def test_select_bridged_mode(self, editor):
        """Should be able to select Bridged mode."""
        editor._mode_combo.setCurrentText("Bridged")
        assert editor._mode_combo.currentText() == "Bridged"

    def test_select_hostonly_mode(self, editor):
        """Should be able to select Host-only mode."""
        editor._mode_combo.setCurrentText("Host-only")
        assert editor._mode_combo.currentText() == "Host-only"

    def test_select_internal_mode(self, editor):
        """Should be able to select Internal mode."""
        editor._mode_combo.setCurrentText("Internal")
        assert editor._mode_combo.currentText() == "Internal"

    def test_select_nat_mode(self, editor):
        """Should be able to select NAT mode."""
        editor._mode_combo.setCurrentText("NAT")
        assert editor._mode_combo.currentText() == "NAT"

    def test_mode_change_updates_diagram(self, editor):
        """Changing mode should update the diagram."""
        editor._mode_combo.setCurrentText("Bridged")
        assert editor._diagram._mode == "Bridged"

    def test_mode_change_updates_options(self, editor):
        """Changing mode should update mode-specific options."""
        editor._mode_combo.setCurrentText("Bridged")
        # Bridge interface should be visible
        assert editor._bridge_iface is not None


# ── Test Category 3: Port Forwarding ────────────────────────────────────────

class TestPortForwarding:
    """Test port forwarding table operations."""

    def test_add_port_forward(self, editor):
        """Adding a port forward should add to the list."""
        rule = {
            "name": "SSH",
            "protocol": "TCP",
            "host_port": 2222,
            "guest_port": 22,
            "guest_ip": "",
        }
        editor._port_forwards.append(rule)
        editor._refresh_pf_table()
        assert len(editor._port_forwards) == 1
        assert editor._pf_table.rowCount() == 1

    def test_add_multiple_port_forwards(self, editor):
        """Adding multiple port forwards should work."""
        rules = [
            {"name": "SSH", "protocol": "TCP", "host_port": 2222, "guest_port": 22, "guest_ip": ""},
            {"name": "HTTP", "protocol": "TCP", "host_port": 8080, "guest_port": 80, "guest_ip": ""},
            {"name": "DNS", "protocol": "UDP", "host_port": 5353, "guest_port": 53, "guest_ip": ""},
        ]
        editor._port_forwards.extend(rules)
        editor._refresh_pf_table()
        assert len(editor._port_forwards) == 3
        assert editor._pf_table.rowCount() == 3

    def test_delete_port_forward(self, editor):
        """Deleting a port forward should remove it."""
        rule = {"name": "SSH", "protocol": "TCP", "host_port": 2222, "guest_port": 22, "guest_ip": ""}
        editor._port_forwards.append(rule)
        editor._refresh_pf_table()
        del editor._port_forwards[0]
        editor._refresh_pf_table()
        assert len(editor._port_forwards) == 0
        assert editor._pf_table.rowCount() == 0

    def test_port_forward_table_columns(self, editor):
        """Port forward table should have 5 columns."""
        assert editor._pf_table.columnCount() == 5

    def test_port_forward_table_headers(self, editor):
        """Port forward table should have correct headers."""
        headers = [editor._pf_table.horizontalHeaderItem(i).text() for i in range(5)]
        assert headers == ["Name", "Protocol", "Host Port", "Guest Port", "Guest IP"]

    def test_port_forward_updates_diagram(self, editor):
        """Adding port forwards should update diagram."""
        rule = {"name": "SSH", "protocol": "TCP", "host_port": 2222, "guest_port": 22, "guest_ip": ""}
        editor._port_forwards.append(rule)
        editor._refresh_pf_table()
        assert len(editor._diagram._port_forwards) == 1


# ── Test Category 4: MAC Configuration ──────────────────────────────────────

class TestMacConfiguration:
    """Test MAC address configuration."""

    def test_generate_mac_returns_valid(self, editor):
        """generate_mac() should return a valid MAC."""
        mac = generate_mac()
        assert validate_mac(mac)

    def test_generate_mac_unique(self, editor):
        """Each generated MAC should be unique."""
        macs = {generate_mac() for _ in range(100)}
        assert len(macs) == 100

    def test_validate_mac_valid(self, editor):
        """validate_mac should accept valid MACs."""
        assert validate_mac("52:54:00:12:34:56")
        assert validate_mac("00:00:00:00:00:00")
        assert validate_mac("FF:FF:FF:FF:FF:FF")

    def test_validate_mac_invalid(self, editor):
        """validate_mac should reject invalid MACs."""
        assert not validate_mac("not-a-mac")
        assert not validate_mac("52:54:00:12:34")
        assert not validate_mac("52:54:00:12:34:56:78")
        assert not validate_mac("GG:54:00:12:34:56")
        assert not validate_mac("")

    def test_mac_input_validation(self, editor):
        """MAC input should validate on change."""
        editor._mac_input.setText("invalid")
        assert "Invalid" in editor._mac_status.text()

        editor._mac_input.setText("52:54:00:12:34:56")
        assert "Valid" in editor._mac_status.text()

    def test_generate_button(self, editor):
        """Generate button should create a new MAC."""
        old_mac = editor._mac_input.text()
        editor._generate_mac()
        new_mac = editor._mac_input.text()
        assert validate_mac(new_mac)
        # Very unlikely to be the same (MAC generation uses random bytes)
        assert new_mac != old_mac

    def test_mac_updates_diagram(self, editor):
        """Changing MAC should update diagram."""
        editor._mac_input.setText("52:54:00:aa:bb:cc")
        assert editor._diagram._mac == "52:54:00:aa:bb:cc"


# ── Test Category 5: Adapter Selection ───────────────────────────────────────

class TestAdapterSelection:
    """Test adapter type selection."""

    def test_select_e1000(self, editor):
        """Should be able to select e1000 adapter."""
        editor._adapter_combo.setCurrentText("e1000")
        assert editor._adapter_combo.currentText() == "e1000"

    def test_select_rtl8139(self, editor):
        """Should be able to select rtl8139 adapter."""
        editor._adapter_combo.setCurrentText("rtl8139")
        assert editor._adapter_combo.currentText() == "rtl8139"

    def test_adapter_change_updates_diagram(self, editor):
        """Changing adapter should update diagram."""
        editor._adapter_combo.setCurrentText("e1000")
        assert editor._diagram._adapter == "e1000"

    def test_adapter_change_updates_info(self, editor):
        """Changing adapter should update info text."""
        editor._adapter_combo.setCurrentText("e1000")
        assert "Intel PRO/1000" in editor._adapter_info.text()

    def test_adapter_count_range(self, editor):
        """Adapter count should be between 1 and 8."""
        assert editor._adapter_count.minimum() == 1
        assert editor._adapter_count.maximum() == 8

    def test_adapter_count_set_value(self, editor):
        """Should be able to set adapter count."""
        editor._adapter_count.setValue(4)
        assert editor._adapter_count.value() == 4


# ── Test Category 6: Bandwidth Limits ───────────────────────────────────────

class TestBandwidthLimits:
    """Test bandwidth limit configuration."""

    def test_bandwidth_disabled_by_default(self, editor):
        """Bandwidth should be disabled by default."""
        assert not editor._bw_enable.isChecked()

    def test_enable_bandwidth(self, editor):
        """Enabling bandwidth should enable inputs."""
        editor._bw_enable.setChecked(True)
        assert editor._bw_inbound.isEnabled()
        assert editor._bw_outbound.isEnabled()
        assert editor._bw_burst.isEnabled()

    def test_disable_bandwidth(self, editor):
        """Disabling bandwidth should disable inputs."""
        editor._bw_enable.setChecked(True)
        editor._bw_enable.setChecked(False)
        assert not editor._bw_inbound.isEnabled()
        assert not editor._bw_outbound.isEnabled()
        assert not editor._bw_burst.isEnabled()

    def test_set_bandwidth_values(self, editor):
        """Should be able to set bandwidth values."""
        editor._bw_enable.setChecked(True)
        editor._bw_inbound.setValue(1000)
        editor._bw_outbound.setValue(500)
        editor._bw_burst.setValue(2000)
        assert editor._bw_inbound.value() == 1000
        assert editor._bw_outbound.value() == 500
        assert editor._bw_burst.value() == 2000

    def test_bandwidth_updates_diagram(self, editor):
        """Setting bandwidth should update diagram."""
        editor._bw_inbound.setValue(500)
        editor._bw_outbound.setValue(250)
        assert editor._diagram._bandwidth_in == 500
        assert editor._diagram._bandwidth_out == 250


# ── Test Category 7: Topology Diagram ───────────────────────────────────────

class TestTopologyDiagram:
    """Test the topology diagram widget."""

    def test_diagram_instantiates(self, app):
        """Diagram should create without error."""
        diagram = TopologyDiagram()
        assert diagram is not None

    def test_diagram_set_mode(self, app):
        """Setting mode should update diagram."""
        diagram = TopologyDiagram()
        diagram.set_mode("Bridged")
        assert diagram._mode == "Bridged"

    def test_diagram_set_adapter(self, app):
        """Setting adapter should update diagram."""
        diagram = TopologyDiagram()
        diagram.set_adapter("e1000")
        assert diagram._adapter == "e1000"

    def test_diagram_set_mac(self, app):
        """Setting MAC should update diagram."""
        diagram = TopologyDiagram()
        diagram.set_mac("52:54:00:aa:bb:cc")
        assert diagram._mac == "52:54:00:aa:bb:cc"

    def test_diagram_set_bandwidth(self, app):
        """Setting bandwidth should update diagram."""
        diagram = TopologyDiagram()
        diagram.set_bandwidth(1000, 500)
        assert diagram._bandwidth_in == 1000
        assert diagram._bandwidth_out == 500

    def test_diagram_set_port_forwards(self, app):
        """Setting port forwards should update diagram."""
        diagram = TopologyDiagram()
        forwards = [{"name": "SSH", "host_port": 2222}]
        diagram.set_port_forwards(forwards)
        assert len(diagram._port_forwards) == 1

    def test_diagram_paint_event(self, app):
        """Diagram should paint without error."""
        diagram = TopologyDiagram()
        diagram.resize(400, 200)
        diagram.show()
        # Render it for real: grab() runs paintEvent into a pixmap.
        diagram.set_port_forwards([{"host_port": 2222, "guest_port": 22, "protocol": "tcp"}])
        pix = diagram.grab()
        assert not pix.isNull() and pix.width() == 400


# ── Test Category 8: Port Forward Dialog ────────────────────────────────────

class TestPortForwardDialog:
    """Test the port forward dialog."""

    def test_dialog_instantiates(self, app):
        """Dialog should create without error."""
        dialog = PortForwardDialog()
        assert dialog is not None

    def test_dialog_get_rule(self, app):
        """Dialog should return rule data."""
        dialog = PortForwardDialog()
        dialog._name.setText("Test")
        dialog._protocol.setCurrentText("UDP")
        dialog._host_port.setValue(8080)
        dialog._guest_port.setValue(80)
        dialog._guest_ip.setText("10.0.2.15")
        rule = dialog.get_rule()
        assert rule["name"] == "Test"
        assert rule["protocol"] == "UDP"
        assert rule["host_port"] == 8080
        assert rule["guest_port"] == 80
        assert rule["guest_ip"] == "10.0.2.15"

    def test_dialog_prefill(self, app):
        """Dialog should pre-fill with existing rule."""
        existing = {
            "name": "SSH",
            "protocol": "TCP",
            "host_port": 2222,
            "guest_port": 22,
            "guest_ip": "10.0.2.15",
        }
        dialog = PortForwardDialog(rule=existing)
        rule = dialog.get_rule()
        assert rule["name"] == "SSH"
        assert rule["host_port"] == 2222


# ── Test Category 9: Config Get/Set ─────────────────────────────────────────

class TestConfigGetSet:
    """Test configuration get/set."""

    def test_get_config(self, editor):
        """get_config should return a dict with all fields."""
        config = editor.get_config()
        assert "mode" in config
        assert "adapter" in config
        assert "mac" in config
        assert "adapter_count" in config
        assert "port_forwards" in config
        assert "bandwidth" in config
        assert "mode_options" in config

    def test_get_config_mode(self, editor):
        """get_config mode should match selection."""
        editor._mode_combo.setCurrentText("Bridged")
        config = editor.get_config()
        assert config["mode"] == "Bridged"

    def test_get_config_adapter(self, editor):
        """get_config adapter should match selection."""
        editor._adapter_combo.setCurrentText("e1000")
        config = editor.get_config()
        assert config["adapter"] == "e1000"

    def test_get_config_mac(self, editor):
        """get_config MAC should match input."""
        editor._mac_input.setText("52:54:00:aa:bb:cc")
        config = editor.get_config()
        assert config["mac"] == "52:54:00:aa:bb:cc"

    def test_get_config_bandwidth(self, editor):
        """get_config bandwidth should match values."""
        editor._bw_enable.setChecked(True)
        editor._bw_inbound.setValue(1000)
        editor._bw_outbound.setValue(500)
        config = editor.get_config()
        assert config["bandwidth"]["enabled"] is True
        assert config["bandwidth"]["inbound_kbps"] == 1000
        assert config["bandwidth"]["outbound_kbps"] == 500

    def test_get_config_port_forwards(self, editor):
        """get_config port_forwards should match list."""
        editor._port_forwards = [
            {"name": "SSH", "protocol": "TCP", "host_port": 2222, "guest_port": 22, "guest_ip": ""},
        ]
        config = editor.get_config()
        assert len(config["port_forwards"]) == 1

    def test_set_config(self, editor):
        """set_config should update panel state."""
        config = {
            "mode": "Bridged",
            "adapter": "e1000",
            "mac": "52:54:00:aa:bb:cc",
            "adapter_count": 2,
            "port_forwards": [{"name": "SSH", "protocol": "TCP", "host_port": 2222, "guest_port": 22, "guest_ip": ""}],
            "bandwidth": {"enabled": True, "inbound_kbps": 1000, "outbound_kbps": 500, "burst_kb": 2000},
        }
        editor.set_config(config)
        assert editor._mode_combo.currentText() == "Bridged"
        assert editor._adapter_combo.currentText() == "e1000"
        assert editor._mac_input.text() == "52:54:00:aa:bb:cc"
        assert editor._adapter_count.value() == 2
        assert len(editor._port_forwards) == 1
        assert editor._bw_enable.isChecked()
        assert editor._bw_inbound.value() == 1000

    def test_set_config_partial(self, editor):
        """set_config with partial config should only update specified fields."""
        config = {"mode": "Internal"}
        editor.set_config(config)
        assert editor._mode_combo.currentText() == "Internal"
        # Other fields should remain unchanged
        assert editor._adapter_combo.currentText() == "virtio-net-pci"


# ── Test Category 10: Mode Options ──────────────────────────────────────────

class TestModeOptions:
    """Test mode-specific options."""

    def test_nat_options(self, editor):
        """NAT mode should show IPv4/IPv6 forwarding options."""
        editor._mode_combo.setCurrentText("NAT")
        config = editor.get_config()
        assert "ipv4_forward" in config["mode_options"]
        assert "ipv6_forward" in config["mode_options"]

    def test_bridged_options(self, editor):
        """Bridged mode should show bridge interface option."""
        editor._mode_combo.setCurrentText("Bridged")
        config = editor.get_config()
        assert "bridge_iface" in config["mode_options"]

    def test_hostonly_options(self, editor):
        """Host-only mode should show network option."""
        editor._mode_combo.setCurrentText("Host-only")
        config = editor.get_config()
        assert "network" in config["mode_options"]

    def test_internal_options(self, editor):
        """Internal mode should show name option."""
        editor._mode_combo.setCurrentText("Internal")
        config = editor.get_config()
        assert "name" in config["mode_options"]


# ── Test Category 11: Integration ───────────────────────────────────────────

class TestIntegration:
    """Integration tests for the full editor workflow."""

    def test_full_config_workflow(self, editor):
        """Test a complete configuration workflow."""
        # Set mode
        editor._mode_combo.setCurrentText("NAT")
        # Set adapter
        editor._adapter_combo.setCurrentText("virtio-net-pci")
        # Set MAC
        editor._mac_input.setText("52:54:00:12:34:56")
        # Add port forwards
        editor._port_forwards = [
            {"name": "SSH", "protocol": "TCP", "host_port": 2222, "guest_port": 22, "guest_ip": ""},
            {"name": "HTTP", "protocol": "TCP", "host_port": 8080, "guest_port": 80, "guest_ip": ""},
        ]
        editor._refresh_pf_table()
        # Enable bandwidth
        editor._bw_enable.setChecked(True)
        editor._bw_inbound.setValue(5000)
        editor._bw_outbound.setValue(2000)
        # Get config
        config = editor.get_config()
        assert config["mode"] == "NAT"
        assert config["adapter"] == "virtio-net-pci"
        assert config["mac"] == "52:54:00:12:34:56"
        assert len(config["port_forwards"]) == 2
        assert config["bandwidth"]["enabled"] is True
        assert config["bandwidth"]["inbound_kbps"] == 5000

    def test_config_roundtrip(self, editor):
        """Test that config can be set and retrieved."""
        # Set a config
        original = {
            "mode": "Bridged",
            "adapter": "e1000",
            "mac": "52:54:00:aa:bb:cc",
            "adapter_count": 3,
            "port_forwards": [{"name": "Test", "protocol": "UDP", "host_port": 9999, "guest_port": 99, "guest_ip": "10.0.2.15"}],
            "bandwidth": {"enabled": True, "inbound_kbps": 100, "outbound_kbps": 50, "burst_kb": 500},
        }
        editor.set_config(original)
        # Get it back
        result = editor.get_config()
        assert result["mode"] == original["mode"]
        assert result["adapter"] == original["adapter"]
        assert result["mac"] == original["mac"]
        assert result["adapter_count"] == original["adapter_count"]
        assert len(result["port_forwards"]) == len(original["port_forwards"])
        assert result["bandwidth"]["enabled"] == original["bandwidth"]["enabled"]

    def test_diagram_reflects_config(self, editor):
        """Diagram should reflect the current configuration."""
        editor._mode_combo.setCurrentText("Bridged")
        editor._adapter_combo.setCurrentText("e1000")
        editor._mac_input.setText("52:54:00:aa:bb:cc")
        editor._port_forwards = [{"name": "SSH", "host_port": 2222}]
        editor._refresh_pf_table()
        editor._bw_inbound.setValue(1000)
        editor._bw_outbound.setValue(500)

        assert editor._diagram._mode == "Bridged"
        assert editor._diagram._adapter == "e1000"
        assert editor._diagram._mac == "52:54:00:aa:bb:cc"
        assert len(editor._diagram._port_forwards) == 1
        assert editor._diagram._bandwidth_in == 1000
        assert editor._diagram._bandwidth_out == 500


# ── Test Category 12: Edge Cases ────────────────────────────────────────────

class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_empty_port_forwards(self, editor):
        """Empty port forwards should work."""
        editor._port_forwards = []
        editor._refresh_pf_table()
        assert editor._pf_table.rowCount() == 0

    def test_max_adapter_count(self, editor):
        """Max adapter count should be 8."""
        editor._adapter_count.setValue(8)
        assert editor._adapter_count.value() == 8

    def test_min_adapter_count(self, editor):
        """Min adapter count should be 1."""
        editor._adapter_count.setValue(1)
        assert editor._adapter_count.value() == 1

    def test_large_bandwidth(self, editor):
        """Large bandwidth values should work."""
        editor._bw_enable.setChecked(True)
        editor._bw_inbound.setValue(100000)
        assert editor._bw_inbound.value() == 100000

    def test_mac_case_insensitive(self, editor):
        """MAC validation should be case insensitive."""
        assert validate_mac("52:54:00:AA:BB:CC")
        assert validate_mac("52:54:00:aa:bb:cc")
        assert validate_mac("52:54:00:Aa:Bb:Cc")

    def test_port_forward_with_ip(self, editor):
        """Port forward with guest IP should work."""
        rule = {"name": "SSH", "protocol": "TCP", "host_port": 2222, "guest_port": 22, "guest_ip": "10.0.2.15"}
        editor._port_forwards.append(rule)
        editor._refresh_pf_table()
        assert editor._pf_table.item(0, 4).text() == "10.0.2.15"

    def test_port_forward_without_ip(self, editor):
        """Port forward without guest IP should work."""
        rule = {"name": "SSH", "protocol": "TCP", "host_port": 2222, "guest_port": 22, "guest_ip": ""}
        editor._port_forwards.append(rule)
        editor._refresh_pf_table()
        assert editor._pf_table.item(0, 4).text() == ""
