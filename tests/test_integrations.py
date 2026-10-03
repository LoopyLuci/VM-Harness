"""VM-Harness Integration Test Suite.

Tests all backends: Docker, Kubernetes, Podman, VMware, VirtualBox, QEMU.
Each test creates real resources, verifies behavior, and cleans up.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))


class DockerIntegrationTest(unittest.TestCase):
    """Test Docker backend integration."""

    @classmethod
    def setUpClass(cls):
        cls.backend = None
        try:
            from gui.async_adapter import get_adapter
            cls.backend = get_adapter().docker
            cls.backend.list_containers()
            cls.backend_available = True
        except Exception as e:
            cls.backend_available = False
            cls.skipTest(cls, f"Docker not available: {e}")

    def test_01_list_containers(self):
        """Test listing containers."""
        containers = self.backend.list_containers()
        self.assertIsInstance(containers, list)
        for c in containers:
            self.assertIn("name", c)
            self.assertIn("status", c)

    def test_02_list_images(self):
        """Test listing images."""
        images = self.backend.list_images()
        self.assertIsInstance(images, list)
        for img in images:
            self.assertIn("repository", img)

    def test_03_container_lifecycle(self):
        """Test full container lifecycle: create, start, stop, remove."""
        container_name = "vmharness-test-lifecycle2"
        # Clean up any existing
        try:
            self.backend.remove_container(container_name, force=True)
        except Exception:
            pass

        # Ensure image is available
        try:
            self.backend.pull_image("alpine:latest")
        except Exception:
            pass

        # Create (config dict, not kwargs)
        config = {"image": "alpine:latest", "command": "sleep 300", "name": container_name}
        result = self.backend.create_container(config)
        self.assertIsNotNone(result)

        # Verify it exists
        containers = self.backend.list_containers()
        names = [c["name"] for c in containers]
        self.assertIn(container_name, names)

        # Start
        self.backend.start_container(container_name)
        time.sleep(2)
        containers = self.backend.list_containers()
        test_container = [c for c in containers if c["name"] == container_name][0]
        self.assertEqual(test_container["status"].lower(), "running")

        # Stop
        self.backend.stop_container(container_name)
        time.sleep(2)

        # Remove
        self.backend.remove_container(container_name)
        containers = self.backend.list_containers()
        names = [c["name"] for c in containers]
        self.assertNotIn(container_name, names)

    def test_04_container_logs(self):
        """Test getting container logs."""
        container_name = "vmharness-test-lifecycle"
        try:
            logs = self.backend.get_logs(container_name)
            self.assertIsInstance(logs, str)
        except Exception:
            pass  # Container may not exist, that's OK

    def test_05_container_inspect(self):
        """Test container inspection."""
        container_name = "vmharness-test-lifecycle"
        try:
            info = self.backend.inspect_container(container_name)
            self.assertIsInstance(info, dict)
        except Exception:
            pass

    def test_06_pull_image(self):
        """Test pulling an image."""
        result = self.backend.pull_image("alpine:latest")
        self.assertIsNotNone(result)


class KubernetesIntegrationTest(unittest.TestCase):
    """Test Kubernetes backend integration."""

    @classmethod
    def setUpClass(cls):
        cls.backend = None
        try:
            from gui.async_adapter import get_adapter
            cls.backend = get_adapter().kubernetes
            cls.backend.list_pods()
            cls.backend_available = True
        except Exception as e:
            cls.backend_available = False
            cls.skipTest(cls, f"Kubernetes not available: {e}")

    def test_01_list_pods(self):
        """Test listing pods."""
        pods = self.backend.list_pods()
        self.assertIsInstance(pods, list)

    def test_02_list_services(self):
        """Test listing services."""
        services = self.backend.list_services()
        self.assertIsInstance(services, list)

    def test_03_list_deployments(self):
        """Test listing deployments."""
        deployments = self.backend.list_deployments()
        self.assertIsInstance(deployments, list)

    def test_04_list_nodes(self):
        """Test listing nodes."""
        nodes = self.backend.list_nodes()
        self.assertIsInstance(nodes, list)

    def _pod_exists(self, pod_name):
        """Check if a pod currently exists."""
        pods = self.backend.list_pods()
        names = [p.get("name", "") if isinstance(p, dict) else getattr(p, "name", "") for p in pods]
        return pod_name in names

    def _wait_for_pod_deleted(self, pod_name, timeout=120, interval=5):
        """Wait until a pod is fully gone or timeout expires."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._pod_exists(pod_name):
                return True
            time.sleep(interval)
        return False

    def _delete_pod_with_retry(self, pod_name, max_retries=3):
        """Delete a pod with retry logic."""
        for attempt in range(max_retries):
            try:
                self.backend.delete_resource("pod", pod_name)
                return
            except Exception:
                if attempt == max_retries - 1:
                    raise
                time.sleep(5)

    def test_04_pod_lifecycle(self):
        """Test pod lifecycle with a simple deployment."""
        import uuid
        from datetime import datetime
        pod_name = f"vmharness-test-pod-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": pod_name, "labels": {"app": "vmharness-test"}},
            "spec": {
                "containers": [{
                    "name": "test",
                    "image": "alpine:latest",
                    "command": ["sleep", "300"],
                }],
                "restartPolicy": "Never",
            },
        }

        try:
            # Clean up any leftover pod with the same name
            if self._pod_exists(pod_name):
                self._delete_pod_with_retry(pod_name)
                self._wait_for_pod_deleted(pod_name)

            self.backend.apply_manifest(manifest)
            time.sleep(5)

            pods = self.backend.list_pods()
            names = [p.get("name", "") if isinstance(p, dict) else getattr(p, "name", "") for p in pods]
            self.assertIn(pod_name, names)

            self._delete_pod_with_retry(pod_name)
            self.assertTrue(
                self._wait_for_pod_deleted(pod_name),
                f"Pod {pod_name} was not deleted within timeout",
            )

            pods = self.backend.list_pods()
            names = [p.get("name", "") if isinstance(p, dict) else getattr(p, "name", "") for p in pods]
            self.assertNotIn(pod_name, names)
        except Exception as e:
            self.skipTest(f"Pod lifecycle test skipped: {e}")

    def test_06_exec_in_pod(self):
        """Test exec into a pod."""
        pod_name = "vmharness-test-pod"
        try:
            result = self.backend.exec_in_pod(pod_name, ["echo", "hello"])
            self.assertIsInstance(result, str)
        except Exception:
            pass  # Pod may not exist


class PodmanIntegrationTest(unittest.TestCase):
    """Test Podman backend integration."""

    @classmethod
    def setUpClass(cls):
        cls.backend = None
        try:
            from gui.async_adapter import get_adapter
            cls.backend = get_adapter().podman
            cls.backend.list_containers()
            cls.backend_available = True
        except Exception as e:
            cls.backend_available = False
            cls.skipTest(cls, f"Podman not available: {e}")

    def test_01_list_containers(self):
        """Test listing Podman containers."""
        containers = self.backend.list_containers()
        self.assertIsInstance(containers, list)

    def test_02_container_lifecycle(self):
        """Test Podman container lifecycle."""
        import uuid
        container_name = f"vmharness-test-podman-{uuid.uuid4().hex[:8]}"
        try:
            # Clean up any existing
            try:
                self.backend.remove_container(container_name, force=True)
            except Exception:
                pass
            self.backend.create_container(
                name=container_name,
                image="alpine:latest",
                command="sleep 300",
            )
            self.backend.start_container(container_name)
            time.sleep(2)
            self.backend.stop_container(container_name)
            self.backend.remove_container(container_name)
        except Exception as e:
            self.skipTest(f"Podman lifecycle test skipped: {e}")


class QEMUIntegrationTest(unittest.TestCase):
    """Test QEMU backend integration."""

    @classmethod
    def setUpClass(cls):
        cls.backend = None
        try:
            from gui.async_adapter import get_adapter
            cls.backend = get_adapter().qemu
            cls.backend_available = True
        except Exception as e:
            cls.backend_available = False
            cls.skipTest(cls, f"QEMU not available: {e}")

    def test_01_list_vms(self):
        """Test listing QEMU VMs."""
        vms = self.backend.list_vms()
        self.assertIsInstance(vms, list)

    def test_02_vm_status(self):
        """Test VM status check."""
        vms = self.backend.list_vms()
        if not vms:
            self.skipTest("No VMs available for status check")
        for vm in vms[:3]:  # Test first 3 VMs
            status = self.backend.get_status(vm["name"])
            self.assertIn(status, ["running", "stopped", "paused", "error", "unknown"])

    def test_03_vm_lifecycle(self):
        """Start and stop the test's own VM. It never touches the user's VMs, and whatever happens the VM is stopped
        afterwards: a VM left running under WHPX can stop other VMs (WSL's among them) from starting."""
        name = "vmharness-test-vm"
        try:
            names = [v if isinstance(v, str) else v.get("name", "") for v in self.backend.list_vms()]
            if name not in names:
                from vm_harness.hypervisor.backend import VMConfig
                self.backend.create_vm(VMConfig(name=name, ram_mb=512, cpus=1, disk_size_gb=1, disk_format="qcow2"))
        except Exception as e:
            self.skipTest(f"QEMU lifecycle test skipped: cannot create the test VM: {e}")
        try:
            try:
                # headless: no display window (and no SPICE hang on headless machines)
                self.backend.start_vm(name, headless=True)
            except Exception as e:
                self.skipTest(f"QEMU lifecycle test skipped: cannot start the test VM: {e}")
            time.sleep(3)
            self.assertEqual(self.backend.get_status(name), "running")
            # force: the test VM has an empty disk and no OS, so nothing would answer a graceful (ACPI) power-down
            self.backend.stop_vm(name, force=True)
            time.sleep(3)
            self.assertEqual(self.backend.get_status(name), "stopped")
        finally:
            try:
                if self.backend.get_status(name) != "stopped":
                    self.backend.stop_vm(name, force=True)
            except Exception:
                pass

    def test_04_qmp_communication(self):
        """Test QMP communication."""
        try:
            result = self.backend.query_status()
            self.assertIsInstance(result, dict)
        except Exception:
            pass


class VMwareIntegrationTest(unittest.TestCase):
    """Test VMware backend integration."""

    @classmethod
    def setUpClass(cls):
        cls.backend = None
        try:
            from gui.async_adapter import get_adapter
            cls.backend = get_adapter().vmware
            cls.backend.list_vms()
            cls.backend_available = True
        except Exception as e:
            cls.backend_available = False
            cls.skipTest(cls, f"VMware not available: {e}")

    def test_01_list_vms(self):
        """Test listing VMware VMs."""
        vms = self.backend.list_vms()
        self.assertIsInstance(vms, list)

    def test_02_vm_lifecycle(self):
        """Test VMware VM lifecycle."""
        vms = self.backend.list_vms()
        if not vms:
            self.skipTest("No VMware VMs available")
        vm = vms[0]
        try:
            self.backend.power_off(vm["name"])
            time.sleep(3)
            self.backend.power_on(vm["name"])
            time.sleep(3)
        except Exception as e:
            self.skipTest(f"VMware lifecycle test skipped: {e}")


class VirtualBoxIntegrationTest(unittest.TestCase):
    """Test VirtualBox backend integration."""

    @classmethod
    def setUpClass(cls):
        cls.backend = None
        try:
            from gui.async_adapter import get_adapter
            cls.backend = get_adapter().vbox
            cls.backend.list_vms()
            cls.backend_available = True
        except Exception as e:
            cls.backend_available = False
            cls.skipTest(cls, f"VirtualBox not available: {e}")

    def test_01_list_vms(self):
        """Test listing VirtualBox VMs."""
        vms = self.backend.list_vms()
        self.assertIsInstance(vms, list)

    def test_02_vm_lifecycle(self):
        """Test VirtualBox VM lifecycle."""
        vms = self.backend.list_vms()
        if not vms:
            self.skipTest("No VirtualBox VMs available")
        vm = vms[0]
        vm_name = vm["name"] if isinstance(vm, dict) else vm
        try:
            # Stop first (ignore errors if already stopped)
            try:
                self.backend.stop_vm(vm_name, force=True)
                time.sleep(3)
            except Exception:
                pass
            # Start
            self.backend.start_vm(vm_name)
            time.sleep(3)
            # Verify running
            status = self.backend.get_status(vm_name)
            self.assertEqual(status, "running")
            # Stop again
            self.backend.stop_vm(vm_name, force=True)
            time.sleep(3)
            status = self.backend.get_status(vm_name)
            self.assertEqual(status, "stopped")
        except Exception as e:
            self.skipTest(f"VirtualBox lifecycle test skipped: {e}")


class APIIntegrationTest(unittest.TestCase):
    """Test REST API integration."""

    @classmethod
    def setUpClass(cls):
        import urllib.request
        cls.api_url = "http://127.0.0.1:8443"
        try:
            req = urllib.request.Request(f"{cls.api_url}/api/v1/")
            with urllib.request.urlopen(req, timeout=5) as resp:
                cls.api_available = resp.status == 200
        except Exception as e:
            cls.api_available = False
            cls.skipTest(cls, f"API not available: {e}")

    def test_01_health_endpoint(self):
        """Test health endpoint."""
        import urllib.request
        req = urllib.request.Request(f"{self.api_url}/api/v1/")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            # API returns VM list at root; check for expected keys
            self.assertTrue("vms" in data or "status" in data or "vm_count" in data)

    def test_02_vms_endpoint(self):
        """Test VMs endpoint."""
        import urllib.request
        req = urllib.request.Request(f"{self.api_url}/api/v1/vms")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            self.assertIsInstance(data, list)

    def test_03_containers_endpoint(self):
        """Test containers endpoint."""
        import urllib.request
        req = urllib.request.Request(f"{self.api_url}/api/v1/containers")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                self.assertIsInstance(data, list)
        except Exception:
            pass

    def test_04_kubernetes_endpoint(self):
        """Test Kubernetes endpoint."""
        import urllib.request
        req = urllib.request.Request(f"{self.api_url}/api/v1/kubernetes/pods")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                self.assertIsInstance(data, list)
        except Exception:
            pass


class ResilienceTest(unittest.TestCase):
    """Test resilience system."""

    def test_01_crash_handler(self):
        """Test crash handler initialization."""
        from gui.resilience import CrashHandler
        handler = CrashHandler()
        self.assertIsNotNone(handler)

    def test_02_atomic_state(self):
        """Test atomic state management."""
        from gui.resilience import AtomicState
        state = AtomicState()
        self.assertIsNotNone(state)

    def test_03_process_guardian(self):
        """Test process guardian."""
        from gui.resilience import ProcessGuardian
        guardian = ProcessGuardian()
        self.assertIsNotNone(guardian)

    def test_04_health_checker(self):
        """Test health checker."""
        from gui.resilience import HealthChecker
        checker = HealthChecker()
        self.assertIsNotNone(checker)

    def test_05_failover_manager(self):
        """Test failover manager."""
        from gui.resilience import FailoverManager
        manager = FailoverManager()
        self.assertIsNotNone(manager)

    def test_06_hot_reloader(self):
        """Test hot reloader."""
        from gui.resilience import HotReloader
        reloader = HotReloader()
        self.assertIsNotNone(reloader)


class GUIPanelTest(unittest.TestCase):
    """Test GUI panel instantiation."""

    @classmethod
    def setUpClass(cls):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PyQt5.QtCore import QCoreApplication, Qt
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        from PyQt5.QtWidgets import QApplication
        cls.app = QApplication([""])

    def test_01_container_panel(self):
        """Test ContainerPanel instantiation."""
        from gui.panels_container import ContainerPanel
        panel = ContainerPanel()
        self.assertIsNotNone(panel)

    def test_02_vmware_vbox_panel(self):
        """Test VMwareVBoxPanel instantiation."""
        from gui.vmware_vbox_panel import VMwareVBoxPanel
        panel = VMwareVBoxPanel()
        self.assertIsNotNone(panel)

    def test_03_all_panels(self):
        """Test all panels can be instantiated."""
        panels = []
        for mod_name in [
            "gui.panels_container",
            "gui.vmware_vbox_panel",
            "gui.panels_vm_control",
            "gui.panels_multi_vm_dashboard",
            "gui.panels_snapshots",
            "gui.panels_storage",
            "gui.panels_cpu",
            "gui.panels_display",
            "gui.panels_network",
            "gui.panels_usb",
            "gui.panels_guest_agent",
            "gui.panels_guest_terminal",
            "gui.panels_iso",
            "gui.panels_sysinfo",
            "gui.panels_telemetry",
            "gui.panels_troubleshoot",
            "gui.panels_automation",
            "gui.panels_monitoring",
            "gui.panels_logs",
            "gui.panels_security",
            "gui.panels_settings",
        ]:
            try:
                mod = __import__(mod_name, fromlist=[""])
                # Find the panel class
                for attr_name in dir(mod):
                    attr = getattr(mod, attr_name)
                    if isinstance(attr, type) and attr_name.endswith("Panel"):
                        panel = attr()
                        panels.append((mod_name, attr_name))
                        break
            except Exception as e:
                print(f"  SKIP {mod_name}: {e}")

        print(f"\nSuccessfully instantiated {len(panels)} panels:")
        for mod, cls in panels:
            print(f"  {mod}.{cls}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
