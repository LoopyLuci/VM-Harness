"""The catalog: every operation VM-Harness can perform, described once and served everywhere.

The HTTP API, the MCP server, the CLI and ABP all read this one list, so a capability exists in all of them or none.
Each operation has an id (``vm.start``), a group, a one-line summary, a JSON Schema for its arguments, whether it
changes anything (``mutating``: those are audited and, in ABP, need approval) and whether it is ``destructive``
(deletes or overwrites something that cannot be recovered).
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from vm_harness.control import schema as S


class OperationError(Exception):
    """An operation failed in a way worth showing to the caller as-is."""

    def __init__(self, message: str, *, code: str = "error", status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class Operation:
    id: str
    group: str
    summary: str
    params: dict[str, Any]
    handler: Callable[..., Any]
    mutating: bool = False
    destructive: bool = False
    needs: str = ""                     # "gui" when it needs the GUI attached, a backend name when it needs that backend
    long_running: bool = False
    tags: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        return {"id": self.id, "group": self.group, "summary": self.summary, "params": self.params,
                "mutating": self.mutating, "destructive": self.destructive, "needs": self.needs,
                "long_running": self.long_running, "tags": self.tags}


class Catalog:
    def __init__(self) -> None:
        self._ops: dict[str, Operation] = {}

    # ---- registration ------------------------------------------------------------------------------------------
    def add(self, op_id: str, handler: Callable[..., Any], *, group: str = "", summary: str = "",
            params: Optional[dict] = None, mutating: bool = False, destructive: bool = False, needs: str = "",
            long_running: bool = False, tags: Optional[list[str]] = None) -> Operation:
        if op_id in self._ops:
            raise ValueError(f"operation {op_id!r} registered twice")
        op = Operation(
            id=op_id, group=group or op_id.split(".")[0], summary=summary or S.first_line(handler.__doc__),
            params=params if params is not None else S.signature_schema(handler), handler=handler,
            mutating=mutating or destructive, destructive=destructive, needs=needs, long_running=long_running,
            tags=list(tags or []))
        self._ops[op_id] = op
        return op

    def op(self, op_id: str, **kw: Any) -> Callable[[Callable], Callable]:
        """Decorator form of ``add``: the function's signature and docstring become the operation's schema and summary."""
        def wrap(fn: Callable) -> Callable:
            self.add(op_id, fn, **kw)
            return fn
        return wrap

    # ---- lookup ------------------------------------------------------------------------------------------------
    def get(self, op_id: str) -> Operation:
        op = self._ops.get(op_id) or self._ops.get(op_id.replace("_", "."))
        if op is None:
            close = [o for o in self._ops if op_id.split(".")[0] in o][:8]
            raise OperationError(f"no operation {op_id!r}" + (f"; did you mean: {', '.join(close)}" if close else ""),
                                 code="not_found", status=404)
        return op

    def all(self) -> list[Operation]:
        return sorted(self._ops.values(), key=lambda o: o.id)

    def ids(self) -> list[str]:
        return sorted(self._ops)

    def search(self, text: str = "", group: str = "") -> list[Operation]:
        words = [w for w in text.lower().split() if w]
        out = []
        for op in self.all():
            if group and op.group != group:
                continue
            hay = f"{op.id} {op.summary} {' '.join(op.tags)}".lower()
            if all(w in hay for w in words):
                out.append(op)
        return out

    def groups(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for op in self._ops.values():
            counts[op.group] = counts.get(op.group, 0) + 1
        return dict(sorted(counts.items()))

    # ---- calling -----------------------------------------------------------------------------------------------
    async def call(self, op_id: str, args: Optional[dict[str, Any]] = None) -> Any:
        """Run an operation with JSON arguments and return a JSON-ready result."""
        op = self.get(op_id)
        kwargs = S.bind(op.handler, dict(args or {}))
        result = op.handler(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return S.to_json(result)

