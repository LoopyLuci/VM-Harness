"""The GUI's operations: what an attached VM-Harness window lets the hub (and so the API, MCP and ABP) do to it.

The hub registers these in the catalog and forwards each call to the window over its attach socket; the window runs
it on the Qt thread (``gui/automation.py``). This module has no Qt import so the hub can load it without a display.

Widgets are addressed by a *target*: ``{"panel": "snapshots", "id": "btn_create"}`` where ``id`` is the widget's
objectName or the stable id ``gui.inspect`` reports (``QPushButton#3``), or ``{"panel": ..., "text": "Create"}`` to
find a widget by its visible text or label. ``panel`` defaults to the panel on screen.
"""
from __future__ import annotations

TARGET = {
    "type": "object",
    "description": "Which widget: {panel?, id} or {panel?, text}. panel defaults to the one on screen.",
    "properties": {"panel": {"type": "string"}, "id": {"type": "string"}, "text": {"type": "string"}},
}


def _p(props: dict | None = None, required: list[str] | None = None) -> dict:
    out: dict = {"type": "object", "properties": props or {}}
    if required:
        out["required"] = required
    return out


GUI_OPS: list[dict] = [
    {"id": "gui.state", "summary": "The window: visible, minimized, size, the panel on screen, status bar text",
     "params": _p()},
    {"id": "gui.window", "summary": "Show, hide, raise, minimize, maximize, restore or resize the window",
     "params": _p({"action": {"type": "string", "enum": ["show", "hide", "raise", "minimize", "maximize", "restore",
                                                          "resize"]},
                   "width": {"type": "integer"}, "height": {"type": "integer"}}, ["action"]), "mutating": True},
    {"id": "gui.panels", "summary": "Every panel (page) in the window, its sidebar label, and which is on screen",
     "params": _p()},
    {"id": "gui.open", "summary": "Switch the window to a panel", "params": _p({"panel": {"type": "string"}}, ["panel"]),
     "mutating": True},
    {"id": "gui.inspect", "summary": "The widgets on a panel: buttons, fields, lists, tables, their ids, text, values "
                                     "and whether they are enabled", "params": _p({
        "panel": {"type": "string"}, "include_hidden": {"type": "boolean", "default": False},
        "max_widgets": {"type": "integer", "default": 400}})},
    {"id": "gui.find", "summary": "Widgets whose text, label, id or type contains the query",
     "params": _p({"query": {"type": "string"}, "panel": {"type": "string"}}, ["query"])},
    {"id": "gui.read", "summary": "Everything one widget shows: its text, value, items, selection or table rows",
     "params": _p({"target": TARGET, "max_rows": {"type": "integer", "default": 200}}, ["target"])},
    {"id": "gui.click", "summary": "Click a button, checkbox, radio button, tab or list item",
     "params": _p({"target": TARGET, "double": {"type": "boolean", "default": False}}, ["target"]), "mutating": True},
    {"id": "gui.set", "summary": "Set a field: text, number, checkbox, slider, date or combo box value",
     "params": _p({"target": TARGET, "value": {}}, ["target", "value"]), "mutating": True},
    {"id": "gui.select", "summary": "Select an item in a list, combo box, tree or tab bar (by text or index), or a "
                                    "table row", "params": _p({"target": TARGET, "item": {}}, ["target", "item"]),
     "mutating": True},
    {"id": "gui.type", "summary": "Type text into the focused widget or a target, optionally pressing Enter",
     "params": _p({"text": {"type": "string"}, "target": TARGET, "enter": {"type": "boolean", "default": False}},
                  ["text"]), "mutating": True},
    {"id": "gui.key", "summary": "Press a key or shortcut (e.g. Enter, Escape, Ctrl+S) in the window or a target",
     "params": _p({"keys": {"type": "string"}, "target": TARGET}, ["keys"]), "mutating": True},
    {"id": "gui.screenshot", "summary": "A PNG of the window, one panel, or one widget (base64)",
     "params": _p({"panel": {"type": "string"}, "target": TARGET, "max_width": {"type": "integer", "default": 1600}})},
    {"id": "gui.methods", "summary": "The public methods a panel offers (refresh, start, create...) with their "
                                     "parameters", "params": _p({"panel": {"type": "string"}}, ["panel"])},
    {"id": "gui.invoke", "summary": "Call one of a panel's public methods with arguments",
     "params": _p({"panel": {"type": "string"}, "method": {"type": "string"}, "args": {"type": "array"},
                   "kwargs": {"type": "object"}}, ["panel", "method"]), "mutating": True},
    {"id": "gui.wait", "summary": "Wait until a widget exists, is enabled, or shows some text (up to timeout_s)",
     "params": _p({"target": TARGET, "text": {"type": "string"}, "enabled": {"type": "boolean"},
                   "timeout_s": {"type": "number", "default": 10}}, ["target"])},
    {"id": "gui.messages", "summary": "Dialogs and message boxes open right now, their text and buttons",
     "params": _p()},
    {"id": "gui.dialog", "summary": "Answer an open dialog or message box by clicking one of its buttons",
     "params": _p({"button": {"type": "string"}, "index": {"type": "integer", "default": 0}}, ["button"]),
     "mutating": True},
]

GUI_OP_IDS = {o["id"] for o in GUI_OPS}
