"""Tests for gui/vm_cloner.py — VM Cloner and Template System."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Setup paths
PROJECT_DIR = Path(__file__).resolve().parent.parent
GUI_DIR = PROJECT_DIR / "gui"
SRC_DIR = PROJECT_DIR / "src"
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(GUI_DIR))
sys.path.insert(0, str(SRC_DIR))

# Must import after path setup
from gui.vm_cloner import (
    VMCloner,
    TemplateManager,
    TemplateMetadata,
    CloneWorker,
    TEMPLATES_DIR,
    QEMU_IMG_DEFAULT,
)


def _make_fake_qemu_img(tmp_dir):
    """Create a fake qemu-img executable for testing.
    
    Creates a Python script that simulates qemu-img, and returns the
    path to an executable wrapper that can be used as VMCloner.qemu_img.
    """
    fake_path = tmp_dir / "qemu-img-fake.py"
    script = '''import sys, shutil

def main():
    if len(sys.argv) < 2:
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "create":
        dest = sys.argv[-1]
        open(dest, 'w').close()
        sys.exit(0)
    elif cmd == "convert":
        args = sys.argv[2:]
        non_flags = [a for a in args if not a.startswith('-')]
        if len(non_flags) >= 2:
            src = non_flags[-2]
            dest = non_flags[-1]
            shutil.copy2(src, dest)
        sys.exit(0)
    elif cmd == "info":
        print('{"format": "qcow2", "size": 1024, "actual-size": 1048576}')
        sys.exit(0)
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
'''
    fake_path.write_text(script)
    
    if sys.platform == "win32":
        # Create a .bat wrapper for Windows
        bat_path = tmp_dir / "qemu-img.bat"
        bat_path.write_text(f'@"{sys.executable}" "{fake_path}" %*\n')
        return str(bat_path)
    else:
        # Make the Python script executable on Unix
        fake_path.chmod(0o755)
        return str(fake_path)


# ═══════════════════════════════════════════════════════════════════════════════
# Test TemplateMetadata
# ═══════════════════════════════════════════════════════════════════════════════

class TestTemplateMetadata:
    """Tests for TemplateMetadata dataclass."""

    def test_default_creation(self):
        """Should create with default values."""
        meta = TemplateMetadata(name="test-template")
        assert meta.name == "test-template"
        assert meta.description == ""
        assert meta.os_type == "linux"
        assert meta.ram_mb == 4096
        assert meta.cpus == 2
        assert meta.disk_format == "qcow2"
        assert meta.tags == []

    def test_to_dict(self):
        """Should serialize to dict."""
        meta = TemplateMetadata(
            name="test",
            description="A test template",
            os_type="windows",
            ram_mb=8192,
            cpus=4,
            source_vm="original-vm",
            tags=["dev", "test"],
        )
        data = meta.to_dict()
        assert data["name"] == "test"
        assert data["description"] == "A test template"
        assert data["os_type"] == "windows"
        assert data["ram_mb"] == 8192
        assert data["cpus"] == 4
        assert data["source_vm"] == "original-vm"
        assert data["tags"] == ["dev", "test"]
        assert "date_created" in data
        assert "date_updated" in data

    def test_from_dict(self):
        """Should deserialize from dict."""
        data = {
            "name": "test",
            "description": "desc",
            "os_type": "bsd",
            "ram_mb": 2048,
            "cpus": 1,
            "disk_format": "qcow2",
            "date_created": "2024-01-01T00:00:00",
            "date_updated": "2024-01-02T00:00:00",
            "source_vm": "src",
            "tags": ["tag1"],
        }
        meta = TemplateMetadata.from_dict(data)
        assert meta.name == "test"
        assert meta.description == "desc"
        assert meta.os_type == "bsd"
        assert meta.ram_mb == 2048
        assert meta.cpus == 1

    def test_from_dict_partial(self):
        """Should handle partial dict with defaults."""
        data = {"name": "partial"}
        meta = TemplateMetadata.from_dict(data)
        assert meta.name == "partial"
        assert meta.os_type == "linux"
        assert meta.ram_mb == 4096

    def test_timestamps_auto_generated(self):
        """Should auto-generate timestamps if not provided."""
        meta = TemplateMetadata(name="test")
        assert meta.date_created != ""
        assert meta.date_updated != ""

    def test_timestamps_preserved(self):
        """Should preserve provided timestamps."""
        meta = TemplateMetadata(
            name="test",
            date_created="2024-01-01T00:00:00",
            date_updated="2024-06-15T12:00:00",
        )
        assert meta.date_created == "2024-01-01T00:00:00"
        assert meta.date_updated == "2024-06-15T12:00:00"


# ═══════════════════════════════════════════════════════════════════════════════
# Test TemplateManager
# ═══════════════════════════════════════════════════════════════════════════════

class TestTemplateManager:
    """Tests for TemplateManager class."""

    @pytest.fixture
    def tmp_templates_dir(self, tmp_path):
        return tmp_path / "templates"

    @pytest.fixture
    def manager(self, tmp_templates_dir):
        return TemplateManager(tmp_templates_dir)

    def test_init_creates_directory(self, tmp_templates_dir):
        assert not tmp_templates_dir.exists()
        TemplateManager(tmp_templates_dir)
        assert tmp_templates_dir.exists()
        assert tmp_templates_dir.is_dir()

    def test_templates_dir_property(self, manager, tmp_templates_dir):
        assert manager.templates_dir == tmp_templates_dir

    def test_save_template(self, manager):
        vm_config = {"vm_name": "test-vm", "ram_mb": 4096, "cpus": 2}
        path = manager.save_template(
            name="test-template",
            vm_config=vm_config,
            description="A test",
            os_type="linux",
            ram_mb=4096,
            cpus=2,
            tags=["test"],
        )
        assert path.exists()
        assert path.suffix == ".json"

        with open(path) as f:
            data = json.load(f)
        assert "metadata" in data
        assert "vm_config" in data
        assert data["metadata"]["name"] == "test-template"
        assert data["vm_config"]["vm_name"] == "test-vm"

    def test_load_template(self, manager):
        vm_config = {"vm_name": "myvm", "ram_mb": 2048}
        manager.save_template("mytemplate", vm_config)
        data = manager.load_template("mytemplate")
        assert data["vm_config"]["vm_name"] == "myvm"
        assert data["metadata"]["name"] == "mytemplate"

    def test_load_template_not_found(self, manager):
        with pytest.raises(FileNotFoundError):
            manager.load_template("nonexistent")

    def test_list_templates(self, manager):
        manager.save_template("tmpl1", {"vm_name": "vm1"})
        manager.save_template("tmpl2", {"vm_name": "vm2"})
        templates = manager.list_templates()
        assert len(templates) == 2
        names = [t.name for t in templates]
        assert "tmpl1" in names
        assert "tmpl2" in names

    def test_list_templates_empty(self, manager):
        assert manager.list_templates() == []

    def test_get_template(self, manager):
        manager.save_template(
            "test", {"vm_name": "vm1"}, description="desc",
            os_type="windows", ram_mb=8192, cpus=4
        )
        meta, config = manager.get_template("test")
        assert isinstance(meta, TemplateMetadata)
        assert meta.name == "test"
        assert meta.description == "desc"
        assert meta.os_type == "windows"
        assert meta.ram_mb == 8192
        assert meta.cpus == 4
        assert config["vm_name"] == "vm1"

    def test_delete_template(self, manager):
        manager.save_template("to-delete", {"vm_name": "vm"})
        assert manager.template_exists("to-delete")
        assert manager.delete_template("to-delete")
        assert not manager.template_exists("to-delete")

    def test_delete_template_not_found(self, manager):
        assert not manager.delete_template("nonexistent")

    def test_template_exists(self, manager):
        assert not manager.template_exists("missing")
        manager.save_template("exists", {"vm_name": "vm"})
        assert manager.template_exists("exists")

    def test_update_metadata(self, manager):
        manager.save_template("test", {"vm_name": "vm"}, description="old")
        assert manager.update_metadata("test", description="new", ram_mb=1024)
        meta, _ = manager.get_template("test")
        assert meta.description == "new"
        assert meta.ram_mb == 1024

    def test_update_metadata_not_found(self, manager):
        assert not manager.update_metadata("nonexistent", description="x")

    def test_save_template_with_metadata_object(self, manager):
        meta = TemplateMetadata(
            name="test", description="desc", os_type="bsd",
            ram_mb=1024, cpus=1, tags=["tag1"],
        )
        manager.save_template("test", {"vm_name": "vm"}, metadata=meta)
        loaded_meta, _ = manager.get_template("test")
        assert loaded_meta.os_type == "bsd"
        assert loaded_meta.tags == ["tag1"]

    def test_list_templates_skips_corrupt(self, manager, tmp_templates_dir):
        manager.save_template("good", {"vm_name": "vm"})
        corrupt_file = tmp_templates_dir / "corrupt.json"
        corrupt_file.write_text("not valid json{{{")
        templates = manager.list_templates()
        assert len(templates) == 1
        assert templates[0].name == "good"


# ═══════════════════════════════════════════════════════════════════════════════
# Test VMCloner
# ═══════════════════════════════════════════════════════════════════════════════

class TestVMCloner:
    """Tests for VMCloner class."""

    @pytest.fixture
    def tmp_dir(self, tmp_path):
        return tmp_path

    @pytest.fixture
    def fake_qemu_img(self, tmp_dir):
        return _make_fake_qemu_img(tmp_dir)

    def test_init_default(self):
        cloner = VMCloner()
        assert cloner.qemu_img == QEMU_IMG_DEFAULT

    def test_init_custom_path(self, fake_qemu_img):
        cloner = VMCloner(fake_qemu_img)
        assert cloner.qemu_img == fake_qemu_img

    def test_is_running_initially_false(self, fake_qemu_img):
        cloner = VMCloner(fake_qemu_img)
        assert not cloner.is_running

    def test_linked_clone_blocking(self, fake_qemu_img, tmp_dir):
        base = tmp_dir / "base.qcow2"
        base.write_bytes(b"fake qcow2 data")
        cloner = VMCloner(fake_qemu_img)
        new_path = str(tmp_dir / "new.qcow2")
        success, result = cloner.linked_clone(str(base), new_path, blocking=True)
        assert success
        assert result == new_path

    def test_full_clone_blocking(self, fake_qemu_img, tmp_dir):
        source = tmp_dir / "source.qcow2"
        source.write_bytes(b"source data")
        cloner = VMCloner(fake_qemu_img)
        dest_path = str(tmp_dir / "dest.qcow2")
        success, result = cloner.full_clone(str(source), dest_path, blocking=True)
        assert success
        assert result == dest_path

    def test_linked_clone_missing_source(self, fake_qemu_img, tmp_dir):
        cloner = VMCloner(fake_qemu_img)
        success, msg = cloner.linked_clone(
            str(tmp_dir / "nonexistent.qcow2"),
            str(tmp_dir / "new.qcow2"),
            blocking=True,
        )
        assert not success
        assert "not found" in msg.lower()

    def test_full_clone_missing_source(self, fake_qemu_img, tmp_dir):
        cloner = VMCloner(fake_qemu_img)
        success, msg = cloner.full_clone(
            str(tmp_dir / "nonexistent.qcow2"),
            str(tmp_dir / "dest.qcow2"),
            blocking=True,
        )
        assert not success
        assert "not found" in msg.lower()

    def test_get_disk_info(self, fake_qemu_img, tmp_dir):
        disk = tmp_dir / "test.qcow2"
        disk.write_bytes(b"data")
        cloner = VMCloner(fake_qemu_img)
        info = cloner.get_disk_info(str(disk))
        assert "format" in info or "error" in info

    def test_progress_callback(self, fake_qemu_img, tmp_dir):
        base = tmp_dir / "base.qcow2"
        base.write_bytes(b"data")
        progress_calls = []
        cloner = VMCloner(fake_qemu_img, lambda pct, msg: progress_calls.append((pct, msg)))
        cloner.linked_clone(str(base), str(tmp_dir / "new.qcow2"), blocking=True)
        assert len(progress_calls) > 0

    def test_async_clone(self, fake_qemu_img, tmp_dir, qtbot):
        base = tmp_dir / "base.qcow2"
        base.write_bytes(b"data")
        cloner = VMCloner(fake_qemu_img)
        success, msg = cloner.linked_clone(str(base), str(tmp_dir / "new.qcow2"))
        assert success
        assert "started" in msg.lower()
        qtbot.waitUntil(lambda: not cloner.is_running, timeout=30000)  # qemu-img under a loaded machine can take a while

    def test_async_while_running_raises(self, fake_qemu_img, tmp_dir, qtbot):
        base = tmp_dir / "base.qcow2"
        base.write_bytes(b"data")
        cloner = VMCloner(fake_qemu_img)
        cloner.linked_clone(str(base), str(tmp_dir / "new.qcow2"))
        with pytest.raises(RuntimeError, match="already in progress"):
            cloner.linked_clone(str(base), str(tmp_dir / "new2.qcow2"))
        qtbot.waitUntil(lambda: not cloner.is_running, timeout=30000)  # qemu-img under a loaded machine can take a while


# ═══════════════════════════════════════════════════════════════════════════════
# Test CloneWorker (QThread)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCloneWorker:
    """Tests for CloneWorker QThread."""

    @pytest.fixture
    def fake_qemu_img(self, tmp_path):
        return _make_fake_qemu_img(tmp_path)

    def test_linked_clone_worker(self, fake_qemu_img, tmp_path, qtbot):
        base = tmp_path / "base.qcow2"
        base.write_bytes(b"fake data")
        dest = str(tmp_path / "new.qcow2")
        worker = CloneWorker(fake_qemu_img, "linked", str(base), dest)
        worker.start()
        qtbot.waitUntil(lambda: not worker.isRunning(), timeout=30000)  # qemu-img under a loaded machine can take a while
        assert os.path.exists(dest)

    def test_full_clone_worker(self, fake_qemu_img, tmp_path, qtbot):
        source = tmp_path / "source.qcow2"
        source.write_bytes(b"source data")
        dest = str(tmp_path / "dest.qcow2")
        worker = CloneWorker(fake_qemu_img, "full", str(source), dest)
        worker.start()
        qtbot.waitUntil(lambda: not worker.isRunning(), timeout=30000)  # qemu-img under a loaded machine can take a while
        assert os.path.exists(dest)

    def test_worker_progress_signals(self, fake_qemu_img, tmp_path, qtbot):
        base = tmp_path / "base.qcow2"
        base.write_bytes(b"data")
        dest = str(tmp_path / "new.qcow2")
        worker = CloneWorker(fake_qemu_img, "linked", str(base), dest)
        progress_values = []
        worker.progress.connect(lambda pct, msg: progress_values.append(pct))
        worker.run()  # on this thread: the signals arrive at once (no dependence on the event loop's state)
        assert len(progress_values) > 0

    def test_worker_finished_ok_signal(self, fake_qemu_img, tmp_path, qtbot):
        base = tmp_path / "base.qcow2"
        base.write_bytes(b"data")
        dest = str(tmp_path / "new.qcow2")
        worker = CloneWorker(fake_qemu_img, "linked", str(base), dest)
        results = []
        worker.finished_ok.connect(lambda path: results.append(path))
        worker.run()  # on this thread: the signals arrive at once (no dependence on the event loop's state)
        assert len(results) == 1
        assert results[0] == dest

    def test_worker_failed_signal(self, fake_qemu_img, tmp_path, qtbot):
        worker = CloneWorker(fake_qemu_img, "linked", "/nonexistent/path.qcow2", str(tmp_path / "dest.qcow2"))
        errors = []
        worker.failed.connect(lambda msg: errors.append(msg))
        worker.run()  # on this thread: the signals arrive at once (no dependence on the event loop's state)
        assert len(errors) > 0

    def test_worker_unknown_operation(self, fake_qemu_img, tmp_path, qtbot):
        base = tmp_path / "base.qcow2"
        base.write_bytes(b"data")
        worker = CloneWorker(fake_qemu_img, "unknown_op", str(base), str(tmp_path / "dest.qcow2"))
        errors = []
        worker.failed.connect(lambda msg: errors.append(msg))
        worker.run()  # on this thread: the signals arrive at once (no dependence on the event loop's state)
        assert any("unknown" in e.lower() for e in errors)


# ═══════════════════════════════════════════════════════════════════════════════
# Test Integration
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntegration:
    """Integration tests for the full clone + template workflow."""

    @pytest.fixture
    def fake_qemu_img(self, tmp_path):
        return _make_fake_qemu_img(tmp_path)

    def test_save_template_then_clone(self, fake_qemu_img, tmp_path):
        tm = TemplateManager(tmp_path / "templates")
        cloner = VMCloner(fake_qemu_img)
        base_disk = tmp_path / "base.qcow2"
        base_disk.write_bytes(b"base vm data")

        vm_config = {
            "vm_name": "base-vm",
            "disk_path": str(base_disk),
            "ram_mb": 4096,
            "cpus": 2,
        }
        tm.save_template(
            "base-template",
            vm_config,
            description="Base template",
            os_type="linux",
            ram_mb=4096,
            cpus=2,
        )

        meta, config = tm.get_template("base-template")
        assert config["vm_name"] == "base-vm"

        new_disk = str(tmp_path / "cloned.qcow2")
        success, result = cloner.linked_clone(config["disk_path"], new_disk, blocking=True)
        assert success
        assert os.path.exists(new_disk)

    def test_full_workflow(self, fake_qemu_img, tmp_path):
        tm = TemplateManager(tmp_path / "templates")
        cloner = VMCloner(fake_qemu_img)

        for i in range(3):
            vm_config = {
                "vm_name": f"vm-{i}",
                "ram_mb": 2048 * (i + 1),
                "cpus": i + 1,
            }
            tm.save_template(
                f"template-{i}",
                vm_config,
                description=f"Template {i}",
                os_type=["linux", "windows", "bsd"][i],
            )

        templates = tm.list_templates()
        assert len(templates) == 3

        meta, config = tm.get_template("template-0")
        source = tmp_path / "source.qcow2"
        source.write_bytes(b"data")
        config["disk_path"] = str(source)

        dest = str(tmp_path / "cloned.qcow2")
        success, _ = cloner.full_clone(str(source), dest, blocking=True)
        assert success

        tm.delete_template("template-1")
        assert len(tm.list_templates()) == 2

    def test_templates_dir_constant(self):
        assert TEMPLATES_DIR.name == ".templates"
        assert TEMPLATES_DIR.parent == PROJECT_DIR


# ═══════════════════════════════════════════════════════════════════════════════
# Test Edge Cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    """Edge case tests."""

    def test_template_name_with_spaces(self, tmp_path):
        tm = TemplateManager(tmp_path / "templates")
        tm.save_template("my template", {"vm_name": "vm"})
        assert tm.template_exists("my template")
        meta, _ = tm.get_template("my template")
        assert meta.name == "my template"

    def test_template_overwrite(self, tmp_path):
        tm = TemplateManager(tmp_path / "templates")
        tm.save_template("test", {"vm_name": "v1"}, description="first")
        tm.save_template("test", {"vm_name": "v2"}, description="second")
        meta, config = tm.get_template("test")
        assert config["vm_name"] == "v2"
        assert meta.description == "second"

    def test_empty_tags(self, tmp_path):
        tm = TemplateManager(tmp_path / "templates")
        tm.save_template("test", {"vm_name": "vm"}, tags=[])
        meta, _ = tm.get_template("test")
        assert meta.tags == []

    def test_cloner_with_callback(self, tmp_path):
        calls = []
        cloner = VMCloner(progress_callback=lambda p, m: calls.append((p, m)))
        assert cloner._progress_cb is not None

    def test_template_metadata_equality(self):
        m1 = TemplateMetadata(name="test", ram_mb=4096)
        m2 = TemplateMetadata(name="test", ram_mb=4096)
        assert m1 == m2
