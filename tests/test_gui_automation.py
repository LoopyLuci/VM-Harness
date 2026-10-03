"""Remote control of the real window (gui/automation.py), offscreen: every gui.* operation against a MainWindow."""
from __future__ import annotations

import base64
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from gui.automation import Automation, GuiError
from vm_harness.control.gui_ops import GUI_OP_IDS


@pytest.fixture(scope="module")
def auto():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from gui.main_window import MainWindow
    w = MainWindow()
    w.show()
    app.processEvents()
    yield Automation(w)
    w.hide()


def test_every_declared_operation_is_implemented(auto):
    for op in GUI_OP_IDS:
        assert hasattr(auto, "op_" + op.split(".", 1)[1]), op


def test_panels_and_open(auto):
    panels = auto.run("gui.panels", {})
    names = {p["name"] for p in panels}
    assert {"dashboard", "wizard", "snapshots", "logs"} <= names
    assert auto.run("gui.open", {"panel": "Create VM"})["panel"] == "wizard"   # sidebar label works too
    assert auto.run("gui.state", {})["panel"] == "wizard"
    with pytest.raises(GuiError, match="no panel"):
        auto.run("gui.open", {"panel": "nowhere"})


def test_inspect_set_click_walks_the_wizard(auto):
    auto.run("gui.open", {"panel": "wizard"})
    widgets = auto.run("gui.inspect", {"panel": "wizard"})["widgets"]
    name_field = next(w for w in widgets if w.get("label", "").startswith("VM Name"))
    assert name_field["kind"] == "text"
    out = auto.run("gui.set", {"target": {"text": "VM Name"}, "value": "made-by-test"})
    assert out["after"]["value"] == "made-by-test"
    step = next(w for w in widgets if w["kind"] == "progress")["value"]
    auto.run("gui.click", {"target": {"text": "Next →"}})
    after = auto.run("gui.inspect", {"panel": "wizard"})["widgets"]
    assert next(w for w in after if w["kind"] == "progress")["value"] == step + 1
    auto.run("gui.click", {"target": {"text": "← Back"}})


def test_find_read_and_errors(auto):
    hits = auto.run("gui.find", {"query": "next", "panel": "wizard"})
    assert hits and hits[0]["kind"] == "button"
    with pytest.raises(GuiError, match="nothing showing"):
        auto.run("gui.click", {"target": {"panel": "wizard", "text": "no such button"}})
    with pytest.raises(GuiError, match="cannot be set"):
        auto.run("gui.set", {"target": {"panel": "wizard", "text": "Next →"}, "value": 1})


def test_screenshot_is_a_png(auto):
    shot = auto.run("gui.screenshot", {"max_width": 640})
    data = base64.b64decode(shot["base64"])
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and shot["width"] == 640


def test_methods_and_invoke_only_public_panel_methods(auto):
    methods = {m["method"] for m in auto.run("gui.methods", {"panel": "snapshots"})}
    assert "refresh" in methods and not any(m.startswith("_") for m in methods)
    with pytest.raises(GuiError, match="only public"):
        auto.run("gui.invoke", {"panel": "snapshots", "method": "_secret"})
    with pytest.raises(GuiError, match="no method"):
        auto.run("gui.invoke", {"panel": "snapshots", "method": "setParent"})


def test_window_actions(auto):
    assert auto.run("gui.window", {"action": "resize", "width": 1200, "height": 800})["width"] == 1200
    with pytest.raises(GuiError):
        auto.run("gui.window", {"action": "explode"})
    assert auto.run("gui.messages", {}) == []
