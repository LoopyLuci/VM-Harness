"""The window's side of remote control: it attaches to the hub and carries out gui.* operations.

A background thread keeps a WebSocket to the hub's ``/v1/gui/attach`` (reconnecting if the hub restarts). Each call
that arrives is handed to the Qt thread through a signal, run there (Qt widgets may only be touched on their own
thread) and answered with the result or the error. See ``vm_harness/control/gui_ops.py`` for the operations.

Widgets get stable ids: the objectName when it has one, otherwise ``<Class>#<n>`` counted in a fixed walk order of
the panel, so an id from ``gui.inspect`` still names the same widget in the next call.
"""
from __future__ import annotations

import asyncio
import base64
import inspect as pyinspect
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Optional

from PyQt5.QtCore import QBuffer, QByteArray, QDate, QDateTime, QEvent, QIODevice, QObject, QPoint, Qt, QTime, \
    pyqtSignal, pyqtSlot
from PyQt5.QtGui import QKeySequence
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import (QAbstractButton, QAbstractItemView, QAbstractSlider, QAbstractSpinBox, QApplication,
                             QCheckBox, QComboBox, QDateEdit, QDateTimeEdit, QDialog, QDoubleSpinBox, QGroupBox, QLabel,
                             QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                             QRadioButton, QSpinBox, QTabBar, QTableView, QTableWidget, QTabWidget, QTextEdit,
                             QTimeEdit, QToolButton, QTreeView, QTreeWidget, QWidget)

log = logging.getLogger("vmharness.gui.automation")


class GuiError(Exception):
    pass


# ---- describing widgets -----------------------------------------------------------------------------------------------
INTERESTING = (QAbstractButton, QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox, QAbstractSlider,
               QAbstractItemView, QTabBar, QTabWidget, QLabel, QProgressBar, QGroupBox)


def _kind(w: QWidget) -> str:
    for cls, name in ((QCheckBox, "checkbox"), (QRadioButton, "radio"), (QPushButton, "button"),
                      (QToolButton, "button"), (QLineEdit, "text"), (QPlainTextEdit, "textarea"),
                      (QTextEdit, "textarea"), (QDateTimeEdit, "datetime"), (QSpinBox, "number"),
                      (QDoubleSpinBox, "number"), (QComboBox, "combo"), (QAbstractSlider, "slider"),
                      (QTableView, "table"), (QTreeView, "tree"), (QAbstractItemView, "list"), (QTabBar, "tabs"),
                      (QTabWidget, "tabs"), (QProgressBar, "progress"), (QGroupBox, "group"), (QLabel, "label")):
        if isinstance(w, cls):
            return name
    return type(w).__name__


def _text(w: QWidget) -> str:
    for attr in ("text", "toPlainText", "title", "currentText"):
        fn = getattr(w, attr, None)
        if callable(fn):
            try:
                v = fn()
                if isinstance(v, str):
                    return v
            except TypeError:
                continue
    return ""


def _value(w: QWidget) -> Any:
    if isinstance(w, QAbstractButton) and w.isCheckable():
        return w.isChecked()
    if isinstance(w, (QSpinBox, QDoubleSpinBox, QAbstractSlider, QProgressBar)):
        return w.value()
    if isinstance(w, QDateTimeEdit):
        return w.dateTime().toString(Qt.ISODate)
    if isinstance(w, QComboBox):
        return w.currentText()
    if isinstance(w, (QLineEdit,)):
        return w.text() if w.echoMode() == QLineEdit.Normal else ("***" if w.text() else "")
    if isinstance(w, (QTextEdit, QPlainTextEdit)):
        return w.toPlainText()[:4000]
    if isinstance(w, QTabWidget):
        return w.tabText(w.currentIndex())
    if isinstance(w, QTabBar):
        return w.tabText(w.currentIndex())
    return None


def _label_for(w: QWidget) -> str:
    """The QLabel that names a field (buddy, or the label just before it in its layout)."""
    parent = w.parentWidget()
    if parent is None:
        return ""
    for lbl in parent.findChildren(QLabel, options=Qt.FindDirectChildrenOnly):
        if lbl.buddy() is w:
            return lbl.text()
    layout = parent.layout()
    if layout is not None:
        idx = layout.indexOf(w)
        if idx > 0:
            prev = layout.itemAt(idx - 1).widget()
            if isinstance(prev, QLabel):
                return prev.text()
    return ""


def _items(w: QWidget, limit: int = 200) -> Optional[list]:
    if isinstance(w, QComboBox):
        return [w.itemText(i) for i in range(min(w.count(), limit))]
    if isinstance(w, QListWidget):
        return [w.item(i).text() for i in range(min(w.count(), limit))]
    if isinstance(w, QTabWidget):
        return [w.tabText(i) for i in range(w.count())]
    if isinstance(w, QTabBar):
        return [w.tabText(i) for i in range(w.count())]
    return None


def _table(w: QWidget, max_rows: int = 200) -> Optional[dict]:
    model = getattr(w, "model", lambda: None)()
    if model is None or isinstance(w, (QComboBox,)):
        return None
    if isinstance(w, QTreeWidget):
        rows = []

        def walk(item, depth=0):
            if len(rows) >= max_rows:
                return
            rows.append({"depth": depth, "cells": [item.text(c) for c in range(item.columnCount())]})
            for i in range(item.childCount()):
                walk(item.child(i), depth + 1)
        for i in range(w.topLevelItemCount()):
            walk(w.topLevelItem(i))
        headers = [w.headerItem().text(c) for c in range(w.columnCount())] if w.headerItem() else []
        return {"columns": headers, "rows": rows, "total": len(rows)}
    cols = model.columnCount()
    headers = [str(model.headerData(c, Qt.Horizontal) or "") for c in range(cols)]
    total = model.rowCount()
    rows = []
    for r in range(min(total, max_rows)):
        rows.append([str(model.data(model.index(r, c)) or "") for c in range(cols)])
    sel = []
    sm = getattr(w, "selectionModel", lambda: None)()
    if sm is not None:
        sel = sorted({i.row() for i in sm.selectedIndexes()})
    return {"columns": headers, "rows": rows, "total": total, "selected_rows": sel}


class Automation(QObject):
    """Runs gui.* operations against a MainWindow. Every method runs on the Qt thread."""

    event = pyqtSignal(dict)

    def __init__(self, window: Any) -> None:
        super().__init__(window)
        self.window = window

    # ---- panels ------------------------------------------------------------------------------------------------------
    def _labels(self) -> dict[str, str]:
        return {name: label for label, _icon, name in getattr(self.window.sidebar, "PANELS", [])}

    def _current_name(self) -> str:
        cur = self.window.panel_stack.currentWidget()
        return next((n for n, p in self.window.panels.items() if p is cur), "")

    def _panel(self, name: Optional[str]) -> tuple[str, QWidget]:
        if not name:
            name = self._current_name()
        if name in self.window.panels:
            return name, self.window.panels[name]
        # accept the sidebar label too ("Snapshots", "VM Control")
        for n, label in self._labels().items():
            if label.lower() == str(name).lower() and n in self.window.panels:
                return n, self.window.panels[n]
        raise GuiError(f"no panel {name!r}; panels: {', '.join(self.window.panels)}")

    def _walk(self, root: QWidget, include_hidden: bool = False) -> list[tuple[str, QWidget]]:
        """(stable id, widget) for every interesting widget under root, in a fixed order."""
        counts: dict[str, int] = {}
        out: list[tuple[str, QWidget]] = []

        def visit(w: QWidget) -> None:
            for child in w.findChildren(QWidget, options=Qt.FindDirectChildrenOnly):
                if isinstance(child, INTERESTING):
                    cls = type(child).__name__
                    counts[cls] = counts.get(cls, 0) + 1
                    wid = child.objectName() or f"{cls}#{counts[cls]}"
                    if include_hidden or child.isVisibleTo(root):
                        out.append((wid, child))
                # the tabs of a QTabWidget are reached through it; the inner QTabBar would duplicate them
                if not isinstance(child, (QAbstractItemView, QComboBox, QAbstractSpinBox)):
                    visit(child)
        visit(root)
        return out

    def _describe(self, wid: str, w: QWidget, root: QWidget) -> dict:
        d: dict[str, Any] = {"id": wid, "kind": _kind(w), "text": _text(w)[:300], "enabled": w.isEnabled(),
                             "visible": w.isVisible()}
        tip = w.toolTip()
        if tip:
            d["tooltip"] = tip[:200]
        label = _label_for(w)
        if label:
            d["label"] = label
        v = _value(w)
        if v is not None:
            d["value"] = v
        items = _items(w)
        if items is not None:
            d["items"] = items
        if isinstance(w, (QTableView, QTreeView, QListWidget)):
            t = _table(w, 20)
            if t:
                d["table"] = t
        if isinstance(w, QLineEdit) and w.placeholderText():
            d["placeholder"] = w.placeholderText()
        if isinstance(w, QAbstractButton) and w.isCheckable():
            d["checkable"] = True
        return d

    def _resolve(self, target: Optional[dict]) -> tuple[str, QWidget, QWidget]:
        target = dict(target or {})
        panel_name, root = self._panel(target.get("panel"))
        widgets = self._walk(root, include_hidden=True)
        if target.get("id"):
            for wid, w in widgets:
                if wid == target["id"]:
                    return wid, w, root
            raise GuiError(f"no widget {target['id']!r} on panel {panel_name}")
        if target.get("text"):
            want = str(target["text"]).strip().lower()
            exact = [(wid, w) for wid, w in widgets if _text(w).strip().lower().replace("&", "") == want
                     or _label_for(w).strip().lower().rstrip(":") == want.rstrip(":")]
            loose = exact or [(wid, w) for wid, w in widgets if want in _text(w).lower() or want in _label_for(w).lower()]
            visible = [x for x in loose if x[1].isVisible()] or loose
            # A label names a field: prefer the field it labels over the label itself.
            fields = [x for x in visible if not isinstance(x[1], QLabel)] or visible
            if fields:
                return fields[0][0], fields[0][1], root
            raise GuiError(f"nothing showing {target['text']!r} on panel {panel_name}")
        raise GuiError("target needs an id or a text")

    # ---- operations ------------------------------------------------------------------------------------------------------
    def op_state(self) -> dict:
        w = self.window
        return {"visible": w.isVisible(), "minimized": w.isMinimized(), "maximized": w.isMaximized(),
                "active": w.isActiveWindow(), "width": w.width(), "height": w.height(),
                "panel": self._current_name(), "status": getattr(w, "status_label", None) and w.status_label.text(),
                "title": w.windowTitle(), "dialogs": len(self._dialogs())}

    def op_window(self, action: str, width: int = 0, height: int = 0) -> dict:
        w = self.window
        if action == "show":
            w.show()
        elif action == "hide":
            w.hide()
        elif action in ("raise", "restore"):
            w.showNormal() if w.isMinimized() or action == "restore" and not w.isMaximized() else w.show()
            w.raise_()
            w.activateWindow()
        elif action == "minimize":
            w.showMinimized()
        elif action == "maximize":
            w.showMaximized()
        elif action == "resize":
            if width <= 0 or height <= 0:
                raise GuiError("resize needs width and height")
            w.showNormal()
            w.resize(width, height)
        else:
            raise GuiError(f"unknown action {action!r}")
        QApplication.processEvents()
        return self.op_state()

    def op_panels(self) -> list:
        labels = self._labels()
        cur = self._current_name()
        return [{"name": n, "label": labels.get(n, n), "current": n == cur, "class": type(p).__name__}
                for n, p in self.window.panels.items()]

    def op_open(self, panel: str) -> dict:
        name, _ = self._panel(panel)
        if hasattr(self.window.sidebar, "_select_panel"):
            self.window.sidebar._select_panel(name)
        else:
            self.window._switch_panel(name)
        QApplication.processEvents()
        return {"panel": name, "current": self._current_name() == name}

    def op_inspect(self, panel: str = "", include_hidden: bool = False, max_widgets: int = 400) -> dict:
        name, root = self._panel(panel)
        widgets = self._walk(root, include_hidden)
        return {"panel": name, "label": self._labels().get(name, name), "count": len(widgets),
                "widgets": [self._describe(wid, w, root) for wid, w in widgets[:max_widgets]],
                "truncated": len(widgets) > max_widgets}

    def op_find(self, query: str, panel: str = "") -> list:
        q = query.lower()
        names = [self._panel(panel)[0]] if panel else list(self.window.panels)
        out = []
        for n in names:
            root = self.window.panels[n]
            for wid, w in self._walk(root, include_hidden=True):
                hay = f"{wid} {_kind(w)} {_text(w)} {_label_for(w)} {w.toolTip()}".lower()
                if q in hay:
                    out.append({"panel": n, **self._describe(wid, w, root)})
                    if len(out) >= 100:
                        return out
        return out

    def op_read(self, target: dict, max_rows: int = 200) -> dict:
        wid, w, root = self._resolve(target)
        d = self._describe(wid, w, root)
        if isinstance(w, (QTableView, QTreeView, QListWidget)):
            d["table"] = _table(w, max_rows)
        if isinstance(w, (QTextEdit, QPlainTextEdit)):
            d["value"] = w.toPlainText()[:100_000]
        return d

    def op_click(self, target: dict, double: bool = False) -> dict:
        wid, w, root = self._resolve(target)
        if not w.isEnabled():
            raise GuiError(f"{wid} is disabled")
        if isinstance(w, QAbstractButton):
            w.click()
            if double:
                w.click()
        else:
            (QTest.mouseDClick if double else QTest.mouseClick)(w, Qt.LeftButton, Qt.NoModifier, w.rect().center())
        QApplication.processEvents()
        return {"clicked": wid, "after": self._describe(wid, w, root)}

    def op_set(self, target: dict, value: Any) -> dict:
        wid, w, root = self._resolve(target)
        if not w.isEnabled():
            raise GuiError(f"{wid} is disabled")
        if isinstance(w, QAbstractButton) and w.isCheckable():
            if bool(value) != w.isChecked():
                w.click()
        elif isinstance(w, QLineEdit):
            w.setText(str(value))
            w.editingFinished.emit()
        elif isinstance(w, (QTextEdit, QPlainTextEdit)):
            w.setPlainText(str(value))
        elif isinstance(w, QDateTimeEdit):
            if isinstance(w, QDateEdit):
                w.setDate(QDate.fromString(str(value), Qt.ISODate))
            elif isinstance(w, QTimeEdit):
                w.setTime(QTime.fromString(str(value), Qt.ISODate))
            else:
                w.setDateTime(QDateTime.fromString(str(value), Qt.ISODate))
        elif isinstance(w, QSpinBox):
            w.setValue(int(value))
        elif isinstance(w, QDoubleSpinBox):
            w.setValue(float(value))
        elif isinstance(w, QAbstractSlider):
            w.setValue(int(value))
        elif isinstance(w, QComboBox):
            idx = w.findText(str(value), Qt.MatchFixedString)
            if idx < 0 and w.isEditable():
                w.setEditText(str(value))
            elif idx < 0:
                raise GuiError(f"{wid} has no item {value!r}; items: {_items(w)}")
            else:
                w.setCurrentIndex(idx)
        else:
            raise GuiError(f"{wid} ({_kind(w)}) cannot be set; use gui.click or gui.select")
        QApplication.processEvents()
        return {"set": wid, "after": self._describe(wid, w, root)}

    def op_select(self, target: dict, item: Any) -> dict:
        wid, w, root = self._resolve(target)
        if isinstance(w, QComboBox):
            return self.op_set(target, item if isinstance(item, str) else w.itemText(int(item)))
        if isinstance(w, (QTabWidget, QTabBar)):
            names = _items(w) or []
            idx = int(item) if not isinstance(item, str) else next(
                (i for i, t in enumerate(names) if t.replace("&", "").lower() == item.lower()), -1)
            if idx < 0 or idx >= len(names):
                raise GuiError(f"{wid} has no tab {item!r}; tabs: {names}")
            w.setCurrentIndex(idx)
        elif isinstance(w, QListWidget):
            matches = [i for i in range(w.count()) if (w.item(i).text() == item if isinstance(item, str) else i == int(item))]
            if not matches:
                raise GuiError(f"{wid} has no item {item!r}")
            w.setCurrentRow(matches[0])
            w.itemClicked.emit(w.item(matches[0]))
        elif isinstance(w, QTreeWidget):
            found = w.findItems(str(item), Qt.MatchExactly | Qt.MatchRecursive)
            if not found:
                raise GuiError(f"{wid} has no item {item!r}")
            w.setCurrentItem(found[0])
            w.itemClicked.emit(found[0], 0)
        elif isinstance(w, QAbstractItemView):
            model = w.model()
            row = int(item) if not isinstance(item, str) else next(
                (r for r in range(model.rowCount()) for c in range(model.columnCount())
                 if str(model.data(model.index(r, c)) or "") == item), -1)
            if row < 0 or row >= model.rowCount():
                raise GuiError(f"{wid} has no row {item!r}")
            w.selectRow(row) if hasattr(w, "selectRow") else w.setCurrentIndex(model.index(row, 0))
            if isinstance(w, QTableWidget):
                w.cellClicked.emit(row, 0)
        else:
            raise GuiError(f"{wid} ({_kind(w)}) has nothing to select")
        QApplication.processEvents()
        return {"selected": item, "after": self._describe(wid, w, root)}

    def op_type(self, text: str, target: Optional[dict] = None, enter: bool = False) -> dict:
        w = self._resolve(target)[1] if target else (QApplication.focusWidget() or self.window)
        w.setFocus()
        QTest.keyClicks(w, text)
        if enter:
            QTest.keyClick(w, Qt.Key_Return)
        QApplication.processEvents()
        return {"typed": len(text), "into": type(w).__name__}

    def op_key(self, keys: str, target: Optional[dict] = None) -> dict:
        w = self._resolve(target)[1] if target else (QApplication.focusWidget() or self.window)
        seq = QKeySequence(keys)
        if seq.isEmpty():
            raise GuiError(f"not a key: {keys!r}")
        for i in range(seq.count()):
            combo = seq[i]
            QTest.keyClick(w, Qt.Key(combo & ~Qt.KeyboardModifierMask), Qt.KeyboardModifiers(combo & Qt.KeyboardModifierMask))
        QApplication.processEvents()
        return {"pressed": keys}

    def op_screenshot(self, panel: str = "", target: Optional[dict] = None, max_width: int = 1600) -> dict:
        if target:
            w = self._resolve(target)[1]
        elif panel:
            w = self._panel(panel)[1]
        else:
            w = self.window
        pix = w.grab()
        if max_width and pix.width() > max_width:
            pix = pix.scaledToWidth(max_width, Qt.SmoothTransformation)
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.WriteOnly)
        pix.save(buf, "PNG")
        return {"format": "png", "width": pix.width(), "height": pix.height(),
                "base64": base64.b64encode(bytes(data)).decode("ascii")}

    def op_methods(self, panel: str) -> list:
        _, p = self._panel(panel)
        out = []
        for name, fn in pyinspect.getmembers(type(p), pyinspect.isfunction):
            if name.startswith("_") or fn.__qualname__.split(".")[0] in ("QWidget", "QObject", "QFrame"):
                continue
            if not fn.__module__.startswith("gui"):
                continue
            try:
                sig = str(pyinspect.signature(fn)).replace("(self, ", "(").replace("(self)", "()")
            except (TypeError, ValueError):
                sig = "(...)"
            out.append({"method": name, "signature": sig, "doc": (pyinspect.getdoc(fn) or "").split("\n")[0][:200]})
        return out

    def op_invoke(self, panel: str, method: str, args: Optional[list] = None, kwargs: Optional[dict] = None) -> Any:
        _, p = self._panel(panel)
        if method.startswith("_"):
            raise GuiError("only public methods can be invoked")
        fn = getattr(p, method, None)
        if not callable(fn) or not (getattr(fn, "__module__", None) or "").startswith("gui"):
            raise GuiError(f"panel {panel} has no method {method!r}; see gui.methods")
        result = fn(*(args or []), **(kwargs or {}))
        QApplication.processEvents()
        try:
            json.dumps(result)
            return result
        except TypeError:
            return repr(result)[:2000]

    def op_messages(self) -> list:
        out = []
        for d in self._dialogs():
            buttons = [b.text().replace("&", "") for b in d.findChildren(QAbstractButton) if b.isVisible()]
            text = d.text() if isinstance(d, QMessageBox) else " ".join(
                l.text() for l in d.findChildren(QLabel) if l.isVisible() and l.text())[:2000]
            out.append({"title": d.windowTitle(), "text": text, "buttons": buttons, "class": type(d).__name__})
        return out

    def op_dialog(self, button: str, index: int = 0) -> dict:
        dialogs = self._dialogs()
        if not dialogs or index >= len(dialogs):
            raise GuiError("no dialog is open")
        d = dialogs[index]
        for b in d.findChildren(QAbstractButton):
            if b.isVisible() and b.text().replace("&", "").lower() == button.lower():
                b.click()
                QApplication.processEvents()
                return {"clicked": button, "dialog": d.windowTitle()}
        raise GuiError(f"the dialog has no button {button!r}")

    def op_wait(self, target: dict, text: str = "", enabled: Optional[bool] = None, timeout_s: float = 10.0) -> dict:
        deadline = time.time() + min(timeout_s, 120)
        last = ""
        while time.time() < deadline:
            try:
                wid, w, root = self._resolve(target)
                ok = (not text or text.lower() in (_text(w) + " " + str(_value(w) or "")).lower()) and \
                     (enabled is None or w.isEnabled() == enabled)
                if ok:
                    return {"ok": True, "widget": self._describe(wid, w, root)}
            except GuiError as e:
                last = str(e)
            QApplication.processEvents()
            time.sleep(0.05)
        raise GuiError(f"timed out after {timeout_s}s" + (f" ({last})" if last else ""))

    def _dialogs(self) -> list:
        return [w for w in QApplication.topLevelWidgets() if isinstance(w, QDialog) and w.isVisible()]

    # ---- dispatch ---------------------------------------------------------------------------------------------------------
    def run(self, op_id: str, args: dict) -> Any:
        fn = getattr(self, "op_" + op_id.split(".", 1)[1], None)
        if fn is None:
            raise GuiError(f"the window does not know {op_id}")
        return fn(**args)


class HubLink(QObject):
    """Keeps the window attached to the hub. Calls arrive on a worker thread and run on the Qt thread."""

    _call = pyqtSignal(str, str, dict)
    attached = pyqtSignal(bool)

    def __init__(self, window: Any, *, start_hub: bool = True) -> None:
        super().__init__(window)
        self.automation = Automation(window)
        self.start_hub = start_hub
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws: Any = None
        self._stop = threading.Event()
        self._call.connect(self._run_call, Qt.QueuedConnection)
        self._thread = threading.Thread(target=self._thread_main, name="vmh-hub-link", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._loop and self._ws is not None:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)

    @pyqtSlot(str, str, dict)
    def _run_call(self, rid: str, op_id: str, args: dict) -> None:
        try:
            reply = {"type": "reply", "id": rid, "ok": True, "result": self.automation.run(op_id, args)}
        except (GuiError, TypeError, ValueError) as e:
            reply = {"type": "reply", "id": rid, "ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            log.exception("gui op %s failed", op_id)
            reply = {"type": "reply", "id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"}
        if self._loop and self._ws is not None:
            asyncio.run_coroutine_threadsafe(self._ws.send_str(json.dumps(reply, default=str)), self._loop)

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._run())
        finally:
            self._loop.close()

    async def _run(self) -> None:
        import aiohttp
        from vm_harness.control.hub import hub_alive
        delay = 1.0
        while not self._stop.is_set():
            info = hub_alive(timeout=1.5)
            if not info and self.start_hub:
                try:
                    from vm_harness.control.client import start_hub
                    info = await asyncio.to_thread(start_hub, 30.0)
                except Exception as e:  # noqa: BLE001
                    log.warning("could not start the hub: %s", e)
            if info:
                url = info["url"].replace("http", "ws", 1) + f"/v1/gui/attach?pid={os.getpid()}"
                try:
                    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {info['token']}"}) as s:
                        async with s.ws_connect(url, heartbeat=20, max_msg_size=64 * 2**20) as ws:
                            self._ws = ws
                            await ws.send_json({"type": "hello", "pid": os.getpid(), "title": "VM-Harness",
                                                "panels": len(self.automation.window.panels)})
                            self.attached.emit(True)
                            delay = 1.0
                            async for msg in ws:
                                if msg.type != aiohttp.WSMsgType.TEXT:
                                    continue
                                data = json.loads(msg.data)
                                if data.get("type") == "call":
                                    self._call.emit(data["id"], data["op"], data.get("args") or {})
                                elif data.get("type") == "refused":
                                    log.info("hub refused: %s", data.get("reason"))
                                    self._stop.wait(30)
                except Exception as e:  # noqa: BLE001
                    log.debug("hub link: %s", e)
                finally:
                    self._ws = None
                    self.attached.emit(False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)
