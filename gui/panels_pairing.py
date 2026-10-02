"""Pairing & Federation panel — manage mobile pairing and desktop-to-desktop federation.

Features:
- Generate pairing QR code for mobile app
- Display/copy pairing URI
- List active API keys (paired devices)
- Revoke access
- Add remote desktop (federation)
- List connected desktops
"""

from __future__ import annotations

from typing import Any

import io
import time
from pathlib import Path

from gui.theme import T
from gui.widgets import Card, TextInput
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QPixmap, QImage
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QGroupBox,
    QGridLayout,
    QLineEdit,
    QComboBox,
    QCheckBox,
    QTabWidget,
    QMessageBox,
    QFileDialog,
    QSizePolicy,
    QScrollArea,
    QFrame,
    QInputDialog,
)


class PairingPanel(QWidget):
    """Manage pairing and federation."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: #0f172a;")
        self._server = None  # Reference to QMCMApiServer
        self._pairing_timer = QTimer()
        self._pairing_timer.timeout.connect(self._refresh_keys)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Header
        header = QLabel("Pairing & Federation")
        header.setStyleSheet(f"color: {T.TEXT_PRIMARY}; font-size: 18px; font-weight: bold;")
        layout.addWidget(header)

        subtitle = QLabel("Pair mobile devices and connect to remote VM-Harness desktops.")
        subtitle.setStyleSheet(f"color: {T.TEXT_SECONDARY}; font-size: 12px;")
        layout.addWidget(subtitle)

        # Tab widget
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet("""
            QTabWidget::pane {
                background: #0f172a;
                border: 1px solid #334155;
                border-radius: 8px;
            }
            QTabBar::tab {
                background: #1e293b;
                color: #64748b;
                border: 1px solid #334155;
                border-bottom: none;
                padding: 8px 16px;
                font-size: 12px;
                min-width: 80px;
            }
            QTabBar::tab:selected {
                background: #1e3a5f;
                color: #60a5fa;
                border-color: #3b82f6;
            }
            QTabBar::tab:!selected:hover {
                background: #1e293b;
                color: #94a3b8;
            }
        """)
        layout.addWidget(self.tabs)

        # ── Mobile Pairing Tab ──────────────────────────────────────────────
        mobile_tab = QWidget()
        mobile_tab.setStyleSheet("background: #0f172a;")
        mobile_layout = QVBoxLayout(mobile_tab)
        mobile_layout.setContentsMargins(12, 12, 12, 12)
        mobile_layout.setSpacing(12)

        # QR Code card
        qr_card = Card("Pair Mobile Device")
        mobile_layout.addWidget(qr_card)

        qr_help = QLabel(
            "Scan this QR code with the VM-Harness mobile app to pair automatically.\n"
            "Or copy the pairing URI below and paste it into the mobile app."
        )
        qr_help.setStyleSheet(f"color: {T.TEXT_SECONDARY}; font-size: 11px;")
        qr_help.setWordWrap(True)
        qr_card.content_layout.addWidget(qr_help)

        # QR image
        self.qr_label = QLabel()
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setFixedSize(200, 200)
        self.qr_label.setStyleSheet("background: #1e293b; border-radius: 8px;")
        qr_card.content_layout.addWidget(self.qr_label, alignment=Qt.AlignCenter)

        # URI display
        uri_row = QWidget()
        uri_row_layout = QHBoxLayout(uri_row)
        uri_row_layout.setContentsMargins(0, 0, 0, 0)
        uri_row_layout.setSpacing(8)
        self.uri_input = QLineEdit()
        self.uri_input.setReadOnly(True)
        self.uri_input.setStyleSheet(f"""
            QLineEdit {{
                background: #1e293b;
                color: {T.TEXT_SECONDARY};
                border: 1px solid #334155;
                border-radius: 4px;
                padding: 6px 10px;
                font-size: 11px;
                font-family: monospace;
            }}
        """)
        uri_row_layout.addWidget(self.uri_input)
        copy_btn = QPushButton("Copy")
        copy_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        copy_btn.setStyleSheet("""
            QPushButton {
                background: #3b82f6;
                color: white;
                border: none;
                border-radius: 4px;
                font-size: 11px;
                padding: 6px 12px;
            }
            QPushButton:hover { background: #2563eb; }
        """)
        copy_btn.clicked.connect(self._copy_uri)
        uri_row_layout.addWidget(copy_btn)
        qr_card.content_layout.addWidget(uri_row)

        # Generate button
        gen_row = QWidget()
        gen_row_layout = QHBoxLayout(gen_row)
        gen_row_layout.setContentsMargins(0, 0, 0, 0)
        gen_row_layout.setSpacing(8)
        gen_btn = QPushButton("Generate New Pairing Code")
        gen_btn.setFixedHeight(32)
        gen_btn.setStyleSheet("""
            QPushButton {
                background: #7c3aed;
                color: white;
                border: none;
                border-radius: 4px;
                font-size: 12px;
                font-weight: 600;
                padding: 0 16px;
            }
            QPushButton:hover { background: #6d28d9; }
        """)
        gen_btn.clicked.connect(self._generate_pairing)
        gen_row_layout.addWidget(gen_btn)
        gen_row_layout.addStretch()
        qr_card.content_layout.addWidget(gen_row)

        # Status
        self.pairing_status = QLabel("")
        self.pairing_status.setStyleSheet("color: #64748b; font-size: 11px;")
        qr_card.content_layout.addWidget(self.pairing_status)

        mobile_layout.addStretch()
        self.tabs.addTab(mobile_tab, "Mobile Pairing")

        # ── Paired Devices Tab ───────────────────────────────────────────────
        devices_tab = QWidget()
        devices_tab.setStyleSheet("background: #0f172a;")
        devices_layout = QVBoxLayout(devices_tab)
        devices_layout.setContentsMargins(12, 12, 12, 12)
        devices_layout.setSpacing(12)

        devices_card = Card("Paired Devices")
        devices_layout.addWidget(devices_card)

        devices_help = QLabel(
            "Devices that have been paired with this desktop. Revoking a device will immediately disconnect it."
        )
        devices_help.setStyleSheet(f"color: {T.TEXT_SECONDARY}; font-size: 11px;")
        devices_help.setWordWrap(True)
        devices_card.content_layout.addWidget(devices_help)

        # Device list
        self.devices_container = QWidget()
        self.devices_container_layout = QVBoxLayout(self.devices_container)
        self.devices_container_layout.setContentsMargins(0, 0, 0, 0)
        self.devices_container_layout.setSpacing(4)
        devices_card.content_layout.addWidget(self.devices_container)

        # Refresh button
        refresh_row = QWidget()
        refresh_row_layout = QHBoxLayout(refresh_row)
        refresh_row_layout.setContentsMargins(0, 0, 0, 0)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.setFixedHeight(28)
        refresh_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #64748b;
                border: 1px solid #334155;
                border-radius: 4px;
                font-size: 11px;
                padding: 0 12px;
            }
            QPushButton:hover { color: #94a3b8; border-color: #475569; }
        """)
        refresh_btn.clicked.connect(self._refresh_keys)
        refresh_row_layout.addWidget(refresh_btn)
        refresh_row_layout.addStretch()
        devices_card.content_layout.addWidget(refresh_row)

        devices_layout.addStretch()
        self.tabs.addTab(devices_tab, "Paired Devices")

        # ── Federation Tab ───────────────────────────────────────────────────
        fed_tab = QWidget()
        fed_tab.setStyleSheet("background: #0f172a;")
        fed_layout = QVBoxLayout(fed_tab)
        fed_layout.setContentsMargins(12, 12, 12, 12)
        fed_layout.setSpacing(12)

        # Add remote desktop
        add_card = Card("Add Remote Desktop")
        fed_layout.addWidget(add_card)

        add_help = QLabel(
            "Connect to another VM-Harness desktop instance over the network.\n"
            "Enter the remote desktop's pairing URI to establish a federated connection."
        )
        add_help.setStyleSheet(f"color: {T.TEXT_SECONDARY}; font-size: 11px;")
        add_help.setWordWrap(True)
        add_card.content_layout.addWidget(add_help)

        # URI input
        fed_uri_row = QWidget()
        fed_uri_row_layout = QHBoxLayout(fed_uri_row)
        fed_uri_row_layout.setContentsMargins(0, 0, 0, 0)
        fed_uri_row_layout.setSpacing(8)
        fed_uri_label = QLabel("Remote URI:")
        fed_uri_label.setStyleSheet("color: #cbd5e1; font-size: 12px;")
        fed_uri_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        fed_uri_row_layout.addWidget(fed_uri_label)
        self.fed_uri_input = TextInput("vmharness://pair?key=...")
        self.fed_uri_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        fed_uri_row_layout.addWidget(self.fed_uri_input)
        fed_uri_row_layout.addStretch()
        add_card.content_layout.addWidget(fed_uri_row)

        # Add button
        fed_btn_row = QWidget()
        fed_btn_row_layout = QHBoxLayout(fed_btn_row)
        fed_btn_row_layout.setContentsMargins(0, 0, 0, 0)
        fed_add_btn = QPushButton("Add Remote Desktop")
        fed_add_btn.setFixedHeight(32)
        fed_add_btn.setStyleSheet("""
            QPushButton {
                background: #7c3aed;
                color: white;
                border: none;
                border-radius: 4px;
                font-size: 12px;
                font-weight: 600;
                padding: 0 16px;
            }
            QPushButton:hover { background: #6d28d9; }
        """)
        fed_add_btn.clicked.connect(self._add_remote_desktop)
        fed_btn_row_layout.addWidget(fed_add_btn)
        fed_btn_row_layout.addStretch()
        add_card.content_layout.addWidget(fed_btn_row)

        # Connected desktops
        connected_card = Card("Connected Desktops")
        fed_layout.addWidget(connected_card)

        self.fed_container = QWidget()
        self.fed_container_layout = QVBoxLayout(self.fed_container)
        self.fed_container_layout.setContentsMargins(0, 0, 0, 0)
        self.fed_container_layout.setSpacing(4)
        connected_card.content_layout.addWidget(self.fed_container)

        fed_layout.addStretch()
        self.tabs.addTab(fed_tab, "Federation")

        # Auto-refresh every 10s
        self._pairing_timer.start(10000)

    def set_server(self, server: Any) -> None:
        """Set the API server reference for token generation."""
        self._server = server
        self._generate_pairing()
        self._refresh_keys()

    def _generate_pairing(self):
        """Generate a new pairing token and display QR code."""
        try:
            from vm_harness.api_server import QMCMApiServer
            import qrcode

            if self._server is None:
                # Try to create a temporary server instance for token generation
                try:
                    from vm_harness.api_server import _load_or_generate_signing_key
                    key_dir = Path(".")
                    signing_key = _load_or_generate_signing_key(key_dir)
                    server = QMCMApiServer(
                        host="0.0.0.0", port=8443, tailscale_only=False,
                        signing_key_dir=key_dir,
                    )
                    self._server = server
                except Exception as e:
                    self.pairing_status.setText(f"⚠️ Cannot generate token: {e}")
                    self.pairing_status.setStyleSheet("color: #ef4444; font-size: 11px;")
                    return

            token, payload, info = self._server.generate_pairing_token()
            uri = f"vmharness://pair?key={token}"
            self.uri_input.setText(uri)

            # Generate QR code
            qr = qrcode.QRCode(version=1, box_size=6, border=2)
            qr.add_data(uri)
            qr.make(fit=True)
            img = qr.make_image(fill_color="#7c3aed", back_color="#1e293b")
            img_bytes = io.BytesIO()
            img.save(img_bytes, format="PNG")
            img_bytes.seek(0)

            qimage = QImage.fromData(img_bytes.read())
            pixmap = QPixmap.fromImage(qimage).scaled(
                180, 180, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            self.qr_label.setPixmap(pixmap)

            self.pairing_status.setText(
                f"✓ Pairing code generated — expires in {int((payload.exp - time.time()) / 60)} minutes"
            )
            self.pairing_status.setStyleSheet("color: #22c55e; font-size: 11px;")

        except Exception as e:
            self.pairing_status.setText(f"⚠️ Error: {e}")
            self.pairing_status.setStyleSheet("color: #ef4444; font-size:11px;")

    def _copy_uri(self):
        """Copy pairing URI to clipboard."""
        uri = self.uri_input.text().strip()
        if uri:
            from PyQt5.QtWidgets import QApplication
            QApplication.clipboard().setText(uri)
            self.pairing_status.setText("✓ Copied to clipboard")
            self.pairing_status.setStyleSheet("color: #22c55e; font-size: 11px;")

    def _refresh_keys(self):
        """Refresh the list of paired devices."""
        # Clear existing
        while self.devices_container_layout.count():
            item = self.devices_container_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if self._server is None:
            label = QLabel("Server not available")
            label.setStyleSheet("color: #64748b; font-size: 11px; font-style: italic;")
            self.devices_container_layout.addWidget(label)
            return

        try:
            keys = self._server.list_api_keys()
            if not keys:
                label = QLabel("No devices paired yet")
                label.setStyleSheet("color: #64748b; font-size: 11px; font-style: italic;")
                self.devices_container_layout.addWidget(label)
                return

            for key_info in keys:
                row = QWidget()
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(8, 4, 8, 4)
                row_layout.setSpacing(8)
                row.setStyleSheet("background: #1e293b; border-radius: 4px;")

                # Device info
                name = key_info.get("display_name", "Unknown")
                machine_id = key_info.get("machine_id", "unknown")[:12]
                created = key_info.get("created_at", "")
                if created:
                    from datetime import datetime
                    try:
                        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        created = dt.strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        pass

                info_text = f"<b>{name}</b><br><span style='color:#64748b;font-size:10px;'>{machine_id} · {created}</span>"
                info_label = QLabel(info_text)
                info_label.setStyleSheet("color: #e2e8f0; font-size: 11px;")
                row_layout.addWidget(info_label, stretch=1)

                # Revoke button
                revoke_btn = QPushButton("Revoke")
                revoke_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                revoke_btn.setStyleSheet("""
                    QPushButton {
                        background: #ef4444;
                        color: white;
                        border: none;
                        border-radius: 4px;
                        font-size: 10px;
                        padding: 4px 8px;
                    }
                    QPushButton:hover { background: #dc2626; }
                """)
                key_id = key_info.get("key_id", "")
                revoke_btn.clicked.connect(lambda checked, kid=key_id: self._revoke_key(kid))
                row_layout.addWidget(revoke_btn)

                self.devices_container_layout.addWidget(row)

        except Exception as e:
            label = QLabel(f"Error loading keys: {e}")
            label.setStyleSheet("color: #ef4444; font-size: 11px;")
            self.devices_container_layout.addWidget(label)

    def _revoke_key(self, key_id: str):
        """Revoke an API key."""
        if not key_id:
            return
        reply = QMessageBox.question(
            self, "Revoke Access",
            "Are you sure you want to revoke access for this device?\n"
            "It will be immediately disconnected.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            try:
                self._server.revoke_key(key_id)
                self._refresh_keys()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to revoke: {e}")

    def _add_remote_desktop(self):
        """Add a remote desktop via pairing URI."""
        uri = self.fed_uri_input.text().strip()
        if not uri or uri == "vmharness://pair?key=...":
            QMessageBox.warning(self, "Invalid URI", "Please enter a valid vmharness:// pairing URI.")
            return

        # Parse the URI
        if not uri.startswith("vmharness://pair?key="):
            QMessageBox.warning(self, "Invalid URI", "URI must start with vmharness://pair?key=")
            return

        token = uri.replace("vmharness://pair?key=", "").strip()

        # Save to config
        try:
            config_path = Path(".federation.json")
            import json
            fed_data = {}
            if config_path.exists():
                fed_data = json.loads(config_path.read_text())

            # Verify token
            if self._server is None:
                QMessageBox.warning(self, "Error", "Server not available")
                return

            payload = self._server.verify_token(token)
            if payload is None:
                QMessageBox.warning(self, "Invalid Token", "Could not verify the pairing token.")
                return

            # Register
            api_key = self._server.register_pairing(payload)

            # Save to federation config
            remote_id = payload.machine_id or payload.host
            fed_data[remote_id] = {
                "host": payload.host,
                "display_name": payload.display_name,
                "api_key": api_key,
                "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            config_path.write_text(json.dumps(fed_data, indent=2))

            QMessageBox.information(
                self, "Success",
                f"Connected to {payload.display_name}!\n"
                f"Host: {payload.host}"
            )
            self.fed_uri_input.clear()
            self._refresh_federation()

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to add remote desktop: {e}")

    def _refresh_federation(self):
        """Refresh the list of connected desktops."""
        while self.fed_container_layout.count():
            item = self.fed_container_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        try:
            config_path = Path(".federation.json")
            if not config_path.exists():
                label = QLabel("No remote desktops configured")
                label.setStyleSheet("color: #64748b; font-size: 11px; font-style: italic;")
                self.fed_container_layout.addWidget(label)
                return

            import json
            fed_data = json.loads(config_path.read_text())
            if not fed_data:
                label = QLabel("No remote desktops configured")
                label.setStyleSheet("color: #64748b; font-size: 11px; font-style: italic;")
                self.fed_container_layout.addWidget(label)
                return

            for remote_id, info in fed_data.items():
                row = QWidget()
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(8, 4, 8, 4)
                row_layout.setSpacing(8)
                row.setStyleSheet("background: #1e293b; border-radius: 4px;")

                name = info.get("display_name", "Unknown")
                host = info.get("host", "unknown")
                added = info.get("added_at", "")

                info_text = f"<b>{name}</b><br><span style='color:#64748b;font-size:10px;'>{host} · {added}</span>"
                info_label = QLabel(info_text)
                info_label.setStyleSheet("color: #e2e8f0; font-size: 11px;")
                row_layout.addWidget(info_label, stretch=1)

                # Remove button
                remove_btn = QPushButton("Remove")
                remove_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                remove_btn.setStyleSheet("""
                    QPushButton {
                        background: #ef4444;
                        color: white;
                        border: none;
                        border-radius: 4px;
                        font-size: 10px;
                        padding: 4px 8px;
                    }
                    QPushButton:hover { background: #dc2626; }
                """)
                remove_btn.clicked.connect(lambda checked, rid=remote_id: self._remove_remote(rid))
                row_layout.addWidget(remove_btn)

                self.fed_container_layout.addWidget(row)

        except Exception as e:
            label = QLabel(f"Error: {e}")
            label.setStyleSheet("color: #ef4444; font-size: 11px;")
            self.fed_container_layout.addWidget(label)

    def _remove_remote(self, remote_id: str):
        """Remove a remote desktop."""
        reply = QMessageBox.question(
            self, "Remove Desktop",
            f"Remove connection to {remote_id}?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            try:
                config_path = Path(".federation.json")
                import json
                if config_path.exists():
                    fed_data = json.loads(config_path.read_text())
                    fed_data.pop(remote_id, None)
                    config_path.write_text(json.dumps(fed_data, indent=2))
                self._refresh_federation()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to remove: {e}")


class PairingManager:
    """Simple pairing token manager for protocol server."""

    def __init__(self):
        self._tokens: dict = {}

    def generate_token(self, device_id: str) -> str:
        import secrets
        token = secrets.token_urlsafe(32)
        self._tokens[token] = {"device_id": device_id, "valid": True}
        return token

    def verify_token(self, device_id: str, token: str) -> dict:
        if token in self._tokens:
            stored = self._tokens[token]
            if stored.get("device_id") == device_id:
                return {"valid": True, "device_id": device_id, "token": token}
        return {"valid": False}
